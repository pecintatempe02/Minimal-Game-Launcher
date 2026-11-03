"""Tests for Wine prefix lifecycle, command construction, and process status.

Subprocess creation is replaced with small fakes so tests never run Wine or a
real game. Asynchronous launch signals are exercised through a headless GLib
main loop.
"""

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

import gi
import pytest

gi.require_version("GLib", "2.0")

from gi.repository import GLib  # noqa: E402

from minimal_game_launcher.errors import (  # noqa: E402
    LaunchError,
    PrefixInitializationError,
    WineNotFoundError,
)
from minimal_game_launcher.models import Game, GameDraft, Platform  # noqa: E402
from minimal_game_launcher.services.game_manager import GameManager  # noqa: E402
from minimal_game_launcher.services.launch_manager import (  # noqa: E402
    PREFIX_READY_MARKER,
    LaunchManager,
    prefix_is_initialized,
)
from minimal_game_launcher.settings import (  # noqa: E402
    LauncherSettings,
    SettingsStore,
    WineSource,
)


class FakeRunner:
    """Emulate ``wineboot`` and record its command and environment."""

    def __init__(
        self,
        returncode: int = 0,
        create_prefix: bool = True,
        error: Exception | None = None,
    ) -> None:
        self.returncode = returncode
        self.create_prefix = create_prefix
        self.error = error
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(
        self, command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess:
        self.calls.append((list(command), kwargs))
        if self.error is not None:
            raise self.error

        environment = kwargs["env"]
        assert isinstance(environment, dict)
        prefix = Path(str(environment["WINEPREFIX"]))
        prefix.mkdir(parents=True, exist_ok=True)
        if self.create_prefix:
            (prefix / "system.reg").write_text("fake registry\n", encoding="utf-8")
        return subprocess.CompletedProcess(
            command,
            self.returncode,
            stdout="fake stdout\n",
            stderr="fake stderr\n",
        )


class FakeProcess:
    """A process whose exit is observed after a configurable poll count."""

    def __init__(self, polls_before_exit: int = 1, returncode: int = 0) -> None:
        self.pid = 4321
        self.returncode = returncode
        self.polls_before_exit = polls_before_exit
        self.polls = 0

    def poll(self) -> int | None:
        self.polls += 1
        if self.polls <= self.polls_before_exit:
            return None
        return self.returncode


class FakePopen:
    """Emulate ``subprocess.Popen`` without starting anything."""

    def __init__(
        self,
        process: FakeProcess | None = None,
        error: OSError | None = None,
    ) -> None:
        self.process = process
        self.error = error
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(self, command: list[str], **kwargs: object) -> FakeProcess:
        self.calls.append((list(command), kwargs))
        if self.error is not None:
            raise self.error
        assert self.process is not None
        return self.process


def _no_optional_tools(_name: str) -> str | None:
    """Model a system without GameMode or MangoHud for deterministic tests."""
    return None


def _windows_game(
    manager: GameManager, tmp_path: Path, name: str = "Wine Game"
) -> Game:
    """Register a Windows game without initializing its prefix."""
    folder = tmp_path / "Windows Games" / name
    folder.mkdir(parents=True, exist_ok=True)
    executable = folder / "game.exe"
    executable.write_bytes(b"MZ")
    return manager.add_game(
        GameDraft(name=name, platform=Platform.WINDOWS, executable=str(executable))
    )


def _custom_wine_store(tmp_path: Path, wine_path: Path) -> SettingsStore:
    store = SettingsStore(tmp_path / "config" / "settings.json")
    store.save(
        LauncherSettings(
            wine_source=WineSource.CUSTOM,
            custom_wine_path=str(wine_path),
        )
    )
    return store


@pytest.fixture
def fake_wine(tmp_path: Path) -> Path:
    """Create a path with spaces that is executable but never actually run."""
    path = tmp_path / "Custom Wine" / "wine-custom"
    path.parent.mkdir(parents=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _run_main_loop_until(predicate, timeout_ms: int = 2000) -> None:
    """Run a headless GLib loop until a predicate becomes true."""
    loop = GLib.MainLoop()
    sources: dict[str, int] = {}

    def check() -> bool:
        if predicate():
            loop.quit()
            sources.pop("check", None)
            timeout_id = sources.pop("timeout", None)
            if timeout_id is not None:
                GLib.source_remove(timeout_id)
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def timeout() -> bool:
        sources.pop("timeout", None)
        check_id = sources.pop("check", None)
        if check_id is not None:
            GLib.source_remove(check_id)
        loop.quit()
        return GLib.SOURCE_REMOVE

    sources["check"] = GLib.timeout_add(10, check)
    sources["timeout"] = GLib.timeout_add(timeout_ms, timeout)
    loop.run()
    assert not sources, "Timed out waiting for launch status update"


def test_prefix_creation_uses_wineboot_and_lazy_directory(
    manager: GameManager, tmp_path: Path, fake_wine: Path
) -> None:
    """First Windows launch creates and initializes only that game's prefix."""
    game = _windows_game(manager, tmp_path)
    assert game.wine_prefix_path is not None
    prefix = Path(game.wine_prefix_path)
    assert not prefix.exists()

    runner = FakeRunner()
    launcher = LaunchManager(
        tmp_path / "unused-prefix-root",
        _custom_wine_store(tmp_path, fake_wine),
        run_factory=runner,
        which=_no_optional_tools,
    )
    spec = launcher.prepare_launch(game)

    assert prefix.is_dir()
    assert (prefix / "system.reg").is_file()
    assert (prefix / PREFIX_READY_MARKER).is_file()
    assert prefix_is_initialized(prefix)
    assert len(runner.calls) == 1
    command, kwargs = runner.calls[0]
    assert command == [str(fake_wine), "wineboot", "-u"]
    assert kwargs["cwd"] == game.working_directory
    assert kwargs["env"]["WINEPREFIX"] == str(prefix)  # type: ignore[index]
    assert spec.command[0] == str(fake_wine)
    assert spec.command[1] == game.executable


def test_prefix_reuse_skips_wineboot(
    manager: GameManager, tmp_path: Path, fake_wine: Path
) -> None:
    """An initialized prefix is reused instead of being initialized again."""
    game = _windows_game(manager, tmp_path)
    runner = FakeRunner()
    launcher = LaunchManager(
        tmp_path / "prefixes",
        _custom_wine_store(tmp_path, fake_wine),
        run_factory=runner,
        which=_no_optional_tools,
    )

    launcher.prepare_launch(game)
    assert len(runner.calls) == 1
    launcher.prepare_launch(game)
    assert len(runner.calls) == 1


def test_each_windows_game_uses_a_separate_prefix(
    manager: GameManager, tmp_path: Path, fake_wine: Path
) -> None:
    """Game IDs determine independent prefix directories."""
    first = _windows_game(manager, tmp_path, "First")
    second = _windows_game(manager, tmp_path, "Second")
    assert first.wine_prefix_path != second.wine_prefix_path

    runner = FakeRunner()
    launcher = LaunchManager(
        tmp_path / "prefixes",
        _custom_wine_store(tmp_path, fake_wine),
        run_factory=runner,
        which=_no_optional_tools,
    )
    first_spec = launcher.prepare_launch(first)
    second_spec = launcher.prepare_launch(second)

    assert first_spec.prefix_path != second_spec.prefix_path
    assert first_spec.prefix_path is not None
    assert second_spec.prefix_path is not None
    assert Path(first_spec.prefix_path).name == first.id
    assert Path(second_spec.prefix_path).name == second.id
    assert {call[1]["env"]["WINEPREFIX"] for call in runner.calls} == {  # type: ignore[index]
        first_spec.prefix_path,
        second_spec.prefix_path,
    }


def test_native_game_runs_without_wine_or_prefix(
    manager: GameManager,
    make_executable,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Native games use the executable directly and clear inherited Wine state."""
    executable = make_executable("Native Game")
    game = manager.add_game(
        GameDraft(
            name="Native",
            platform=Platform.LINUX,
            executable=str(executable),
            launch_arguments='--fullscreen --config "save games/config.ini"',
        )
    )

    def fail_lookup(name: str) -> str | None:
        if name in {"wine", "wine64"}:
            raise AssertionError("Native launches must not look up Wine")
        return None

    monkeypatch.setenv("WINEPREFIX", "/should/not/leak")
    launcher = LaunchManager(
        tmp_path / "prefixes",
        SettingsStore(tmp_path / "settings.json"),
        which=fail_lookup,
    )
    spec = launcher.prepare_launch(game)

    assert spec.command == (
        str(executable.resolve()),
        "--fullscreen",
        "--config",
        "save games/config.ini",
    )
    assert spec.prefix_path is None
    assert "WINEPREFIX" not in spec.env
    assert not (tmp_path / "prefixes").exists()


def test_wine_game_command_sets_wineprefix(
    manager: GameManager, tmp_path: Path, fake_wine: Path
) -> None:
    """Wine launches pass the executable and arguments as separate safe args."""
    game = _windows_game(manager, tmp_path)
    launcher = LaunchManager(
        tmp_path / "prefixes",
        _custom_wine_store(tmp_path, fake_wine),
        run_factory=FakeRunner(),
        which=_no_optional_tools,
    )
    spec = launcher.prepare_launch(game)

    assert spec.command == (str(fake_wine), game.executable)
    assert spec.cwd == game.working_directory
    assert spec.env["WINEPREFIX"] == game.wine_prefix_path


def test_system_wine_is_resolved_from_path(
    manager: GameManager, tmp_path: Path, fake_wine: Path
) -> None:
    """System mode resolves Wine from PATH and still uses the game prefix."""
    game = _windows_game(manager, tmp_path)
    launcher = LaunchManager(
        tmp_path / "prefixes",
        SettingsStore(tmp_path / "settings.json"),
        run_factory=FakeRunner(),
        which=lambda name: str(fake_wine) if name == "wine" else None,
    )
    spec = launcher.prepare_launch(game)

    assert spec.command[0] == str(fake_wine)
    assert spec.env["WINEPREFIX"] == game.wine_prefix_path


def test_wine_process_is_spawned_with_wineprefix(
    manager: GameManager, tmp_path: Path, fake_wine: Path
) -> None:
    """The actual child process receives WINEPREFIX and an argument list."""
    game = _windows_game(manager, tmp_path)
    process = FakeProcess(polls_before_exit=0, returncode=0)
    popen = FakePopen(process=process)
    launcher = LaunchManager(
        tmp_path / "prefixes",
        _custom_wine_store(tmp_path, fake_wine),
        run_factory=FakeRunner(),
        popen_factory=popen,
        poll_interval_ms=10,
        which=_no_optional_tools,
    )
    events: list[str] = []
    launcher.connect(
        "launch-finished",
        lambda _manager, _game_id, _code: events.append("finished"),
    )

    assert launcher.launch(game)
    _run_main_loop_until(lambda: bool(events))
    launcher.shutdown()

    assert len(popen.calls) == 1
    command, kwargs = popen.calls[0]
    assert command == [str(fake_wine), game.executable]
    assert kwargs["shell"] is False
    assert kwargs["env"]["WINEPREFIX"] == game.wine_prefix_path  # type: ignore[index]


def test_paths_with_spaces_are_preserved(
    manager: GameManager, tmp_path: Path, fake_wine: Path
) -> None:
    """Spaces and shell metacharacters remain single argument-list elements."""
    folder = tmp_path / "Games & $pecials" / "A Game"
    folder.mkdir(parents=True)
    executable = folder / "game & binary;.exe"
    executable.write_bytes(b"MZ")
    working_directory = tmp_path / "Work Directory With Spaces"
    working_directory.mkdir()
    game = manager.add_game(
        GameDraft(
            name="Spaces",
            platform=Platform.WINDOWS,
            executable=str(executable),
            working_directory=str(working_directory),
            launch_arguments='--path "C:\\Save Games\\slot one"',
        )
    )
    prefix_root = tmp_path / "Prefixes With Spaces"
    game = replace(
        game, wine_prefix_path=str(prefix_root / game.id)
    )
    launcher = LaunchManager(
        prefix_root,
        _custom_wine_store(tmp_path, fake_wine),
        run_factory=FakeRunner(),
        which=_no_optional_tools,
    )
    spec = launcher.prepare_launch(game)

    assert spec.command[0] == str(fake_wine)
    assert spec.command[1] == str(executable.resolve())
    assert spec.command[2:] == ("--path", "C:\\Save Games\\slot one")
    assert spec.cwd == str(working_directory.resolve())
    assert spec.env["WINEPREFIX"] == game.wine_prefix_path
    assert " " in spec.env["WINEPREFIX"]


def test_missing_executable_fails_before_spawn(
    manager: GameManager, make_executable, tmp_path: Path
) -> None:
    """A removed executable produces a readable launch error."""
    game = manager.add_game(
        GameDraft(
            name="Missing",
            platform=Platform.LINUX,
            executable=str(make_executable("missing-game")),
        )
    )
    broken = replace(game, executable=str(tmp_path / "gone" / "game"))
    launcher = LaunchManager(
        tmp_path / "prefixes", SettingsStore(tmp_path / "settings.json")
    )

    with pytest.raises(LaunchError, match="was not found"):
        launcher.prepare_launch(broken)


def test_invalid_working_directory_fails_before_spawn(
    manager: GameManager, make_executable, tmp_path: Path
) -> None:
    """A missing working directory is reported separately from a missing game."""
    game = manager.add_game(
        GameDraft(
            name="Bad Workdir",
            platform=Platform.LINUX,
            executable=str(make_executable()),
        )
    )
    broken = replace(game, working_directory=str(tmp_path / "not-here"))
    launcher = LaunchManager(
        tmp_path / "prefixes", SettingsStore(tmp_path / "settings.json")
    )

    with pytest.raises(LaunchError, match="working directory does not exist"):
        launcher.prepare_launch(broken)


def test_missing_wine_is_reported(
    manager: GameManager, tmp_path: Path
) -> None:
    """System Wine lookup failures identify the missing dependency."""
    game = _windows_game(manager, tmp_path)
    launcher = LaunchManager(
        tmp_path / "prefixes",
        SettingsStore(tmp_path / "settings.json"),
        which=lambda _name: None,
    )

    with pytest.raises(WineNotFoundError, match="Wine is not installed"):
        launcher.prepare_launch(game)


def test_prefix_initialization_failure_is_reported(
    manager: GameManager, tmp_path: Path, fake_wine: Path
) -> None:
    """Non-zero wineboot exits become a readable prefix error."""
    game = _windows_game(manager, tmp_path)
    launcher = LaunchManager(
        tmp_path / "prefixes",
        _custom_wine_store(tmp_path, fake_wine),
        run_factory=FakeRunner(returncode=1, create_prefix=False),
        which=_no_optional_tools,
    )

    with pytest.raises(
        PrefixInitializationError, match="wineboot exited with code 1"
    ):
        launcher.prepare_launch(game)


def test_native_permission_denied_is_reported(
    manager: GameManager, make_executable, tmp_path: Path
) -> None:
    """A non-executable native file is rejected only at launch time."""
    executable = make_executable("permission-game")
    game = manager.add_game(
        GameDraft(
            name="Permission",
            platform=Platform.LINUX,
            executable=str(executable),
        )
    )
    executable.chmod(0o644)
    launcher = LaunchManager(
        tmp_path / "prefixes", SettingsStore(tmp_path / "settings.json")
    )

    with pytest.raises(LaunchError, match="not executable"):
        launcher.prepare_launch(game)


def test_process_creation_failure_is_reported_asynchronously(
    manager: GameManager, make_executable, tmp_path: Path
) -> None:
    """Popen failures become a UI signal instead of a traceback."""
    game = manager.add_game(
        GameDraft(
            name="Popen Failure",
            platform=Platform.LINUX,
            executable=str(make_executable("popen-failure")),
        )
    )
    popen = FakePopen(error=PermissionError(13, "Permission denied"))
    launcher = LaunchManager(
        tmp_path / "prefixes",
        SettingsStore(tmp_path / "settings.json"),
        popen_factory=popen,
        poll_interval_ms=10,
        which=_no_optional_tools,
    )
    events: list[tuple[str, str]] = []
    launcher.connect(
        "launch-failed",
        lambda _manager, game_id, message: events.append((game_id, message)),
    )
    launcher.connect(
        "launch-started",
        lambda *_args: pytest.fail("No process should have been registered"),
    )

    assert launcher.launch(game)
    _run_main_loop_until(lambda: bool(events))
    launcher.shutdown()

    assert events[0][0] == game.id
    assert "could not be created" in events[0][1]
    assert "Permission denied" in events[0][1]
    assert launcher.status(game.id) is None


def test_process_status_updates_from_start_to_finish(
    manager: GameManager, make_executable, tmp_path: Path
) -> None:
    """The manager reports start, running, and exit states via GLib."""
    executable = make_executable("status-game")
    game = manager.add_game(
        GameDraft(
            name="Status Game",
            platform=Platform.LINUX,
            executable=str(executable),
            launch_arguments="--one",
        )
    )
    process = FakeProcess(polls_before_exit=1, returncode=0)
    popen = FakePopen(process=process)
    launcher = LaunchManager(
        tmp_path / "prefixes",
        SettingsStore(tmp_path / "settings.json"),
        popen_factory=popen,
        poll_interval_ms=10,
        which=_no_optional_tools,
    )
    events: list[tuple[str, int]] = []
    launcher.connect(
        "launch-started",
        lambda _manager, game_id, pid: events.append(("started", pid)),
    )
    launcher.connect(
        "launch-finished",
        lambda _manager, game_id, code: events.append(("finished", code)),
    )

    assert launcher.launch(game)
    assert launcher.status(game.id) == "starting"
    _run_main_loop_until(lambda: len(events) >= 2)
    launcher.shutdown()

    assert events == [("started", process.pid), ("finished", 0)]
    assert launcher.status(game.id) is None
    assert len(popen.calls) == 1
    command, kwargs = popen.calls[0]
    assert command == [str(executable.resolve()), "--one"]
    assert kwargs["shell"] is False
    assert kwargs["cwd"] == str(executable.parent.resolve())
    assert kwargs["stdout"] == subprocess.DEVNULL
    assert kwargs["stderr"] == subprocess.DEVNULL
    assert "WINEPREFIX" not in kwargs["env"]  # type: ignore[operator]


class RecordingProcess(FakeProcess):
    """Fake process that records any attempt to signal it."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.signals: list[str] = []

    def terminate(self) -> None:
        self.signals.append("terminate")

    def kill(self) -> None:
        self.signals.append("kill")

    def send_signal(self, sig: int) -> None:
        self.signals.append(f"signal {sig}")


def test_signal_terminated_game_reports_negative_status(
    manager: GameManager, make_executable, tmp_path: Path
) -> None:
    """A game killed by a signal is reported as finished with its signal number."""
    executable = make_executable("killed-game")
    game = manager.add_game(
        GameDraft(name="Killed Game", platform=Platform.LINUX, executable=str(executable))
    )
    process = RecordingProcess(polls_before_exit=0, returncode=-9)
    launcher = LaunchManager(
        tmp_path / "prefixes",
        SettingsStore(tmp_path / "settings.json"),
        popen_factory=FakePopen(process=process),
        poll_interval_ms=10,
        which=_no_optional_tools,
    )
    finished: list[int] = []
    launcher.connect(
        "launch-finished", lambda _manager, _game_id, code: finished.append(code)
    )

    assert launcher.launch(game)
    _run_main_loop_until(lambda: len(finished) >= 1)
    launcher.shutdown()

    assert finished == [-9]
    assert launcher.status(game.id) is None


def test_shutdown_leaves_running_games_untouched(
    manager: GameManager, make_executable, tmp_path: Path
) -> None:
    """Closing the launcher stops monitoring but never signals a game process."""
    executable = make_executable("still-running")
    game = manager.add_game(
        GameDraft(name="Still Running", platform=Platform.LINUX, executable=str(executable))
    )
    process = RecordingProcess(polls_before_exit=10**6, returncode=0)
    launcher = LaunchManager(
        tmp_path / "prefixes",
        SettingsStore(tmp_path / "settings.json"),
        popen_factory=FakePopen(process=process),
        poll_interval_ms=10,
        which=_no_optional_tools,
    )
    started: list[int] = []
    launcher.connect(
        "launch-started", lambda _manager, _game_id, pid: started.append(pid)
    )

    assert launcher.launch(game)
    _run_main_loop_until(lambda: len(started) >= 1)
    assert launcher.is_running(game.id)
    launcher.shutdown()

    assert process.signals == []
    assert launcher.is_running(game.id) is False
