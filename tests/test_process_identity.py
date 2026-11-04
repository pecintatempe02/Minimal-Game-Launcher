"""Tests for process identity, pidfiles, reaping, shutdown, and reattachment.

Games in these tests are ``/bin/sleep <n>`` commands spawned by the test
itself, so every child exits on its own and no test ever needs to signal a
game process. Helper processes used only for identity snapshots are the
exception; they are cleaned up by the test that owns them.

No test touches the user's launcher, data, prefixes, or covers.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from minimal_game_launcher.models import Game, Platform
from minimal_game_launcher.services.launch_manager import LaunchManager
from minimal_game_launcher.services.process_identity import (
    ProcessIdentity,
    process_identity,
    read_pidfile,
    remove_pidfile,
    verify_process,
    write_pidfile,
)
from minimal_game_launcher.settings import SettingsStore

SLEEP_SECONDS = 10


def _terminate_and_wait(process: subprocess.Popen) -> None:
    """Clean up a test-owned helper process (never used for game processes)."""
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _wait_gone(pid: int, timeout_s: float = SLEEP_SECONDS + 15) -> bool:
    """Wait for a natural exit, then reap it like init would.

    In production the launcher process exits and orphaned games are reparented
    to init, which reaps them. A pytest process stays alive, so it is the
    parent and must collect the exit status itself for /proc to clear.
    waitpid only collects a status; it never sends a signal.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            raw = Path(f"/proc/{pid}/stat").read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            return True  # entry gone: already reaped or never existed
        state = raw.rsplit(")", 1)[-1].split()[0]
        if state == "Z":
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass
            return True
        time.sleep(0.1)
    return False


def _wait_zombie(pid: int, timeout_s: float = 10.0) -> bool:
    """Wait until ``pid`` is an unreaped zombie.

    Unlike :func:`_wait_gone`, this never calls ``waitpid``: the caller keeps
    ownership of the zombie so that code under test (e.g. ``shutdown()``) gets
    the chance to reap it. The test must collect afterwards if the code under
    test fails to do so.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            raw = Path(f"/proc/{pid}/stat").read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            return False  # already fully reaped: no zombie to hand over
        if raw.rsplit(")", 1)[-1].split()[0] == "Z":
            return True
        time.sleep(0.05)
    return False


def _wait_until(predicate, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _make_game(tmp_path: Path, sleep_seconds: int = SLEEP_SECONDS) -> Game:
    """A Linux game that runs ``/bin/sleep`` so children exit by themselves."""
    working_directory = tmp_path / "bin"
    working_directory.mkdir(parents=True, exist_ok=True)
    return Game(
        id="test-sleeper",
        name="Sleeper",
        platform=Platform.LINUX,
        executable="/bin/sleep",
        working_directory=str(working_directory),
        launch_arguments=str(sleep_seconds),
        cover_path=None,
        wine_prefix_path=None,
        gamemode_enabled=False,
        mangohud_enabled=False,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )


def _make_manager(
    tmp_path: Path, popen_factory=subprocess.Popen, **overrides
) -> LaunchManager:
    kwargs = dict(
        popen_factory=popen_factory,
        which=lambda name: "/bin/sleep" if name == "sleep" else None,
        poll_interval_ms=50,
        pidfiles_root=tmp_path / "state" / "processes",
    )
    kwargs.update(overrides)
    return LaunchManager(
        tmp_path / "prefixes",
        SettingsStore(tmp_path / "settings.json"),
        **kwargs,
    )


def _run_main_loop_until(predicate, timeout_ms: int = 5000) -> bool:
    import gi

    gi.require_version("GLib", "2.0")
    from gi.repository import GLib

    loop = GLib.MainLoop()
    state = {"done": False}

    def check() -> bool:
        if predicate():
            state["done"] = True
            loop.quit()
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def timeout() -> bool:
        loop.quit()
        return GLib.SOURCE_REMOVE

    GLib.timeout_add(5, check)
    GLib.timeout_add(timeout_ms, timeout)
    loop.run()
    return state["done"]


# --- identity snapshots ------------------------------------------------------


def test_identity_snapshot_records_pid_and_starttime() -> None:
    process = subprocess.Popen(["sleep", "30"])
    try:
        identity = process_identity(process.pid)
        assert identity is not None
        assert identity.pid == process.pid
        assert identity.starttime
        assert identity.exe and "sleep" in identity.exe
        assert identity.cmdline and "sleep" in identity.cmdline
    finally:
        _terminate_and_wait(process)


def test_identity_snapshot_of_dead_process_is_none() -> None:
    process = subprocess.Popen(["true"])
    process.wait()
    # wait() reaped the child, so its /proc entry is gone.
    assert process_identity(process.pid) is None


def test_identity_rejects_invalid_pids() -> None:
    assert process_identity(0) is None
    assert process_identity(-1) is None


def test_verify_matches_same_process() -> None:
    process = subprocess.Popen(["sleep", "30"])
    try:
        identity = process_identity(process.pid)
        assert identity is not None
        assert verify_process(identity) == "alive"
    finally:
        _terminate_and_wait(process)


def test_verify_reports_reused_starttime() -> None:
    """A different starttime for the same PID means the PID was recycled."""
    process = subprocess.Popen(["sleep", "30"])
    try:
        identity = process_identity(process.pid)
        assert identity is not None
        forged = ProcessIdentity(
            pid=identity.pid,
            starttime=str(int(identity.starttime) + 5000),
            exe=identity.exe,
            cmdline=identity.cmdline,
        )
        assert verify_process(forged) == "reused"
    finally:
        _terminate_and_wait(process)


def test_verify_reports_gone_after_exit() -> None:
    process = subprocess.Popen(["true"])
    process.wait()
    identity = process_identity(process.pid)
    if identity is None:
        return  # fully reaped already
    # An unreaped zombie lingers in /proc with state Z.
    assert verify_process(identity) == "gone"


# --- pidfiles ----------------------------------------------------------------


def test_pidfile_round_trip(tmp_path: Path) -> None:
    process = subprocess.Popen(["sleep", "30"])
    try:
        identity = process_identity(process.pid)
        assert identity is not None
        path = write_pidfile(tmp_path, identity, "game-1")
        assert path is not None
        assert path.stat().st_mode & 0o777 == 0o600
        assert read_pidfile(tmp_path, "game-1") == identity
    finally:
        _terminate_and_wait(process)


def test_pidfile_rejects_unsafe_game_ids(tmp_path: Path) -> None:
    process = subprocess.Popen(["sleep", "30"])
    try:
        identity = process_identity(process.pid)
        assert identity is not None
        for bad_id in ("../escape", "a/b", ".hidden", "", "sp ace"):
            assert write_pidfile(tmp_path, identity, bad_id) is None
            assert read_pidfile(tmp_path, bad_id) is None
    finally:
        _terminate_and_wait(process)


def test_pidfile_survives_malformed_content(tmp_path: Path) -> None:
    (tmp_path / "game-x.json").write_text("not json at all", encoding="utf-8")
    assert read_pidfile(tmp_path, "game-x") is None
    (tmp_path / "game-x.json").write_text(
        '{"pid": "not-an-int", "starttime": "1"}', encoding="utf-8"
    )
    assert read_pidfile(tmp_path, "game-x") is None


def test_remove_pidfile_tolerates_missing_files(tmp_path: Path) -> None:
    remove_pidfile(tmp_path, "never-existed")


# --- launch manager integration ----------------------------------------------


def test_launch_writes_pidfile_and_poll_reaps_on_exit(tmp_path: Path) -> None:
    """A 2-second game exits on its own; the poll loop reaps and cleans up."""
    game = _make_game(tmp_path, sleep_seconds=2)
    manager = _make_manager(tmp_path)
    finished: list[int] = []
    manager.connect(
        "launch-finished", lambda _m, _gid, code: finished.append(code)
    )

    assert manager.launch(game) is True
    assert _run_main_loop_until(lambda: bool(finished), timeout_ms=10_000)
    assert finished == [0]
    manager.shutdown()

    # The pidfile is removed once the exit is observed, and no zombie is left.
    assert not (tmp_path / "state" / "processes" / f"{game.id}.json").exists()


def test_shutdown_leaves_running_game_alone_and_keeps_pidfile(
    tmp_path: Path,
) -> None:
    game = _make_game(tmp_path)
    manager = _make_manager(tmp_path)
    started: list[int] = []
    manager.connect(
        "launch-started", lambda _m, _gid, pid: started.append(pid)
    )

    assert manager.launch(game) is True
    assert _run_main_loop_until(lambda: bool(started))
    pid = started[0]

    manager.shutdown()
    identity = read_pidfile(tmp_path / "state" / "processes", game.id)
    assert identity is not None
    assert identity.pid == pid
    # The launcher never signals its games: the child must still be running.
    assert verify_process(identity) == "alive"
    assert Path(f"/proc/{pid}").exists()

    # Let the game exit naturally so no process outlives the test.
    assert _wait_gone(pid)


def test_duplicate_launch_blocked_while_previous_session_game_runs(
    tmp_path: Path,
) -> None:
    """A pidfile from a previous session blocks a second launch."""
    game = _make_game(tmp_path)
    manager = _make_manager(tmp_path)
    started: list[int] = []
    manager.connect(
        "launch-started", lambda _m, _gid, pid: started.append(pid)
    )
    assert manager.launch(game) is True
    assert _run_main_loop_until(lambda: bool(started))
    manager.shutdown()
    pid = started[0]

    # New session, same pidfiles root: the recorded process still runs.
    second = _make_manager(tmp_path)
    assert second.launch(game) is False
    assert second.reattach(game) is True
    assert second.status(game.id) == "running"
    second.shutdown()

    assert _wait_gone(pid)


def test_stale_pidfile_does_not_block_launch(tmp_path: Path) -> None:
    """A record whose process is gone must be discarded, not honored."""
    game = _make_game(tmp_path)
    root = tmp_path / "state" / "processes"
    forged = ProcessIdentity(pid=999_999, starttime="1", exe=None, cmdline=())
    write_pidfile(root, forged, game.id)

    manager = _make_manager(tmp_path)
    assert manager.reattach(game) is False
    # The stale record was removed so future launches are not blocked.
    assert read_pidfile(root, game.id) is None
    manager.shutdown()


def test_reused_pid_is_treated_as_stale(tmp_path: Path) -> None:
    """A live PID with a different start time must not be adopted."""
    game = _make_game(tmp_path)
    root = tmp_path / "state" / "processes"
    unrelated = subprocess.Popen(["sleep", "30"])
    try:
        identity = process_identity(unrelated.pid)
        assert identity is not None
        tampered = ProcessIdentity(
            pid=identity.pid,
            starttime=str(int(identity.starttime) + 12345),
            exe=identity.exe,
            cmdline=identity.cmdline,
        )
        write_pidfile(root, tampered, game.id)
        manager = _make_manager(tmp_path)
        # The launcher must not claim the unrelated process as its game.
        assert manager.reattach(game) is False
        assert read_pidfile(root, game.id) is None
        manager.shutdown()
    finally:
        _terminate_and_wait(unrelated)


def test_shutdown_reaps_exited_child_before_poll_observes_it(
    tmp_path: Path,
) -> None:
    """A child that exited between polls is reaped at shutdown, not zombied.

    This test is deliberately discriminating. The child is left in zombie
    state, and the test holds a strong reference to the ``Popen`` object:
    CPython's ``Popen.__del__`` would otherwise reap the zombie opportunistically
    when ``shutdown()`` drops the manager's reference, masking a missing
    ``poll()``. With the reference held, only a ``poll()`` inside
    ``shutdown()`` can collect the child, so an implementation that merely
    clears ``_active`` fails the assertions below.
    """
    popens: list[subprocess.Popen] = []

    def recording_factory(*args: object, **kwargs: object) -> subprocess.Popen:
        process = subprocess.Popen(*args, **kwargs)  # type: ignore[arg-type]
        popens.append(process)
        return process

    game = _make_game(tmp_path, sleep_seconds=1)
    manager = _make_manager(
        tmp_path, popen_factory=recording_factory, poll_interval_ms=60_000
    )
    started: list[int] = []
    manager.connect(
        "launch-started", lambda _m, _gid, pid: started.append(pid)
    )
    assert manager.launch(game) is True
    assert _run_main_loop_until(lambda: bool(started))
    pid = started[0]
    assert popens, "factory must have produced the game process"

    # The child exits after ~1 s and becomes pytest's zombie: /proc entry
    # present, state Z, status collected by nobody.
    assert _wait_zombie(pid), "child never reached zombie state"
    assert Path(f"/proc/{pid}/stat").exists()

    try:
        manager.shutdown()  # the only chance to reap: must poll() the child
        # The zombie was collected by shutdown(), so the /proc entry is gone.
        assert not Path(f"/proc/{pid}/stat").exists()
        # Its launch record was removed too: the next session must not
        # reattach to a dead PID.
        assert (
            read_pidfile(tmp_path / "state" / "processes", game.id) is None
        )
    finally:
        # If shutdown() failed to reap, collect the zombie here so the test
        # process does not leak one (collection only, never signaling).
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass


def test_reattach_is_idempotent_and_readonly(tmp_path: Path) -> None:
    game = _make_game(tmp_path)
    manager = _make_manager(tmp_path)
    started: list[int] = []
    manager.connect(
        "launch-started", lambda _m, _gid, pid: started.append(pid)
    )
    assert manager.launch(game) is True
    assert _run_main_loop_until(lambda: bool(started))
    pid = started[0]
    manager.shutdown()

    first = _make_manager(tmp_path)
    assert first.reattach(game) is True
    assert first.reattach(game) is False  # already tracked
    assert first.status(game.id) == "running"
    assert first.is_running(game.id) is True
    # A reattached process is not our child: no Popen object is retained.
    active = first._active[game.id]
    assert active.attached is True
    assert active.process is None
    first.shutdown()

    assert _wait_gone(pid)


def test_pidfile_without_manager_root_disables_tracking(tmp_path: Path) -> None:
    """A manager with no pidfiles root still launches and reaps normally."""
    game = _make_game(tmp_path, sleep_seconds=1)
    manager = _make_manager(tmp_path, pidfiles_root=None)
    finished: list[int] = []
    manager.connect(
        "launch-finished", lambda _m, _gid, code: finished.append(code)
    )
    assert manager.launch(game) is True
    assert _run_main_loop_until(lambda: bool(finished), timeout_ms=10_000)
    manager.shutdown()
    assert finished == [0]


# --- verdict semantics and conservative accessibility -------------------------


def test_verify_distinguishes_absent_from_error(tmp_path: Path) -> None:
    """A truly absent process is ``gone``; the two must not be conflated.

    ``FileNotFoundError`` is the only honest proof of absence, so it maps to
    ``gone``. Every other read failure maps to ``inaccessible``.
    """
    identity = ProcessIdentity(
        pid=os.getpid() + 4096, starttime="1", exe=None, cmdline=()
    )
    # 4096 PIDs above us: almost certainly absent, and *provably* absent if
    # the kernel reports FileNotFoundError. Force the absent branch.
    absent_seen = False
    for candidate in range(identity.pid, identity.pid + 512):
        probe = ProcessIdentity(pid=candidate, starttime="1")
        try:
            Path(f"/proc/{candidate}/stat").read_text(
                encoding="utf-8", errors="replace"
            )
        except FileNotFoundError:
            identity = probe
            absent_seen = True
            break
        except OSError:
            continue
    assert absent_seen, "no absent PID found to exercise the gone branch"
    assert verify_process(identity) == "gone"


def test_verify_inaccessible(monkeypatch: pytest.MonkeyPatch) -> None:
    """A PID entry that exists but cannot be read is ``inaccessible``."""
    recorded = ProcessIdentity(pid=os.getpid(), starttime="0")
    monkeypatch.setattr(
        "minimal_game_launcher.services.process_identity._read_stat_fields",
        lambda pid: (None, False),
    )
    assert verify_process(recorded) == "inaccessible"


def test_read_stat_fields_distinguishes_absent_and_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(None, True) marks absence; (None, False) marks other failures."""
    module = "minimal_game_launcher.services.process_identity"
    from minimal_game_launcher.services import process_identity as pi

    fields, absent = pi._read_stat_fields(999_999_999)
    assert fields is None and absent is True  # extremely unlikely to exist

    monkeypatch.setattr(
        Path,
        "read_text",
        lambda *a, **k: (_ for _ in ()).throw(PermissionError("hidepid")),
    )
    fields, absent = pi._read_stat_fields(os.getpid())
    assert fields is None and absent is False


def test_write_pidfile_rejects_non_positive_pid(tmp_path: Path) -> None:
    """A record without a usable PID is never written."""
    for pid in (0, -1):
        identity = ProcessIdentity(pid=pid, starttime="1")
        assert write_pidfile(tmp_path, identity, "game-neg") is None
        assert read_pidfile(tmp_path, "game-neg") is None


def test_write_pidfile_never_follows_symlink(tmp_path: Path) -> None:
    """O_NOFOLLOW: a pre-planted symlink is refused, not followed."""
    victim = tmp_path / "victim.txt"
    victim.write_text("precious", encoding="utf-8")
    link = tmp_path / "game-link.json"
    link.symlink_to(victim)
    identity = ProcessIdentity(pid=os.getpid(), starttime="1")
    assert write_pidfile(tmp_path, identity, "game-link") is None
    # The symlink target was not truncated or replaced with JSON.
    assert victim.read_text(encoding="utf-8") == "precious"
    assert link.is_symlink()


def test_from_json_rejects_type_garbage() -> None:
    """Wrong-typed fields make the record unusable instead of trusted."""
    bad_payloads = (
        '{"pid": 1, "starttime": "1", "exe": 5}',
        '{"pid": 1, "starttime": "1", "cmdline": "wine"}',
        '{"pid": 1, "starttime": "1", "cmdline": [1, 2]}',
        '{"pid": 1.5, "starttime": "1"}',
        '{"pid": true, "starttime": "1"}',
        '{"starttime": "1"}',
        "[]",
    )
    for payload in bad_payloads:
        assert ProcessIdentity.from_json(payload) is None


def test_non_ascii_comm_does_not_break_snapshot() -> None:
    """A process name with non-ASCII bytes still yields a usable identity."""
    script = "import sys, time; sys.stdout.write('déjà\\n'); sys.stdout.flush(); time.sleep(30)"
    process = subprocess.Popen(
        [sys.executable, "-c", script], stdout=subprocess.PIPE
    )
    try:
        assert process.stdout is not None
        process.stdout.readline()  # the python3.x process is now renamed
        identity = process_identity(process.pid)
        assert identity is not None
        assert identity.pid == process.pid
        assert identity.starttime
    finally:
        _terminate_and_wait(process)


# --- reattachment behavior ----------------------------------------------------


def test_attached_game_exit_is_observed_via_pidfile(tmp_path: Path) -> None:
    """A reattached game's observed exit emits finish 0 and removes the file.

    The foreign process exits naturally (it is a short sleep); the manager
    must notice through pidfile verification alone, never through Popen.
    """
    game = _make_game(tmp_path, sleep_seconds=1)
    root = tmp_path / "state" / "processes"
    manager = _make_manager(tmp_path, poll_interval_ms=50)

    # Forge a record for a real, live sleep that we own but the manager
    # never spawned: read-only reattachment by design.
    process = subprocess.Popen(["sleep", "1"])
    try:
        identity = process_identity(process.pid)
        assert identity is not None
        assert write_pidfile(root, identity, game.id) is not None

        finished: list[int] = []
        manager.connect(
            "launch-finished", lambda _m, _gid, code: finished.append(code)
        )
        assert manager.reattach(game) is True
        # The sleep exits on its own; the poll loop must observe it via the
        # pidfile and clean up, without ever signaling the process.
        assert _run_main_loop_until(
            lambda: bool(finished), timeout_ms=15_000
        )
        assert finished == [0]
        assert read_pidfile(root, game.id) is None
        assert manager.status(game.id) is None
    finally:
        _wait_gone(process.pid)


def test_attached_game_exit_emits_no_launch_started(
    tmp_path: Path,
) -> None:
    """Reattachment is not a launch: no launch-started signal is emitted."""
    game = _make_game(tmp_path)
    root = tmp_path / "state" / "processes"
    process = subprocess.Popen(["sleep", "2"])
    try:
        identity = process_identity(process.pid)
        assert identity is not None
        assert write_pidfile(root, identity, game.id) is not None

        manager = _make_manager(tmp_path)
        started: list[int] = []
        finished: list[int] = []
        manager.connect(
            "launch-started", lambda _m, _gid, pid: started.append(pid)
        )
        manager.connect(
            "launch-finished", lambda _m, _gid, code: finished.append(code)
        )
        assert manager.reattach(game) is True
        manager.shutdown()
        assert started == []  # no false "Launched" signal on reattach
        assert finished == []  # shutdown observes nothing; record is kept
        assert read_pidfile(root, game.id) is not None
    finally:
        _wait_gone(process.pid)


def test_inaccessible_process_is_treated_as_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Conservative mode: unreadable /proc keeps the record and blocks.

    An ``inaccessible`` verdict must (1) keep the pidfile, (2) block a
    duplicate launch, (3) not emit launch-finished, and (4) signal nothing.
    """
    module = "minimal_game_launcher.services.process_identity"
    game = _make_game(tmp_path)
    root = tmp_path / "state" / "processes"
    process = subprocess.Popen(["sleep", "2"])
    try:
        identity = process_identity(process.pid)
        assert identity is not None
        assert write_pidfile(root, identity, game.id) is not None

        # Everything becomes unreadable, as with a hidepid remount.
        monkeypatch.setattr(
            f"{module}._read_stat_fields", lambda pid: (None, False)
        )
        manager = _make_manager(tmp_path, poll_interval_ms=50)
        finished: list[int] = []
        manager.connect(
            "launch-finished", lambda _m, _gid, code: finished.append(code)
        )
        # (2) duplicate-launch prevention treats it as running.
        assert manager.launch(game) is False
        assert manager.reattach(game) is True
        # (3) the poll loop must not report a false exit while unreadable.
        ran = _run_main_loop_until(
            lambda: bool(finished), timeout_ms=500
        )
        assert ran is False and finished == []
        # (1) the record survived the unreadable window.
        assert read_pidfile(root, game.id) is not None
        manager.shutdown()
        assert read_pidfile(root, game.id) is not None
    finally:
        _wait_gone(process.pid)


def test_register_after_shutdown_still_writes_pidfile(
    tmp_path: Path,
) -> None:
    """A spawn racing shutdown leaves a record for the next session."""
    game = _make_game(tmp_path)
    manager = _make_manager(tmp_path)
    process = subprocess.Popen(["sleep", "2"])
    try:
        manager.shutdown()  # the launcher exits before registration
        # _register_process is what a worker's idle callback would invoke.
        manager._register_process(game, process)
        assert manager.status(game.id) is None  # never tracked as active
        identity = read_pidfile(tmp_path / "state" / "processes", game.id)
        assert identity is not None and identity.pid == process.pid
    finally:
        _wait_gone(process.pid)


def test_launch_blocks_on_inaccessible_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """launch() refuses a game whose record is currently unverifiable."""
    module = "minimal_game_launcher.services.process_identity"
    game = _make_game(tmp_path)
    root = tmp_path / "state" / "processes"
    process = subprocess.Popen(["sleep", "2"])
    try:
        identity = process_identity(process.pid)
        assert identity is not None
        assert write_pidfile(root, identity, game.id) is not None
        monkeypatch.setattr(
            f"{module}._read_stat_fields", lambda pid: (None, False)
        )
        manager = _make_manager(tmp_path)
        assert manager.launch(game) is False
        assert read_pidfile(root, game.id) is not None
        manager.shutdown()
    finally:
        _wait_gone(process.pid)
