"""Real-process tests for the bounded launch-output reader.

A short Python child process stands in for a Wine game. Nothing Wine-related
runs, and every process writes only to a pipe created by the test.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from minimal_game_launcher.services.launch_manager import _BoundedCapture
from minimal_game_launcher.services.wine_output import MAX_CAPTURE_BYTES


def _child(code: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def _capture(code: str) -> str:
    process = _child(code)
    capture = _BoundedCapture()
    capture.start(process.stdout)
    assert process.wait(timeout=30) == 0
    text = capture.finish(timeout=10)
    process.stdout.close()
    return text


def test_output_larger_than_the_limit_is_drained_without_deadlock() -> None:
    """A child writing far past the cap must still finish and not block."""
    total = MAX_CAPTURE_BYTES * 4

    text = _capture(
        f"import sys\nsys.stdout.write('x' * {total})\nsys.stdout.flush()\n"
    )

    assert len(text) == MAX_CAPTURE_BYTES


def test_many_small_writes_are_all_drained() -> None:
    """Slow, chunked writers are read completely and the child exits cleanly."""
    text = _capture(
        "import sys\n"
        "for index in range(200):\n"
        "    sys.stdout.write(f'line {index}\\n')\n"
        "    sys.stdout.flush()\n"
    )

    assert text.count("line ") == 200
    assert text.splitlines()[-1] == "line 199"


def test_invalid_utf8_is_replaced_instead_of_raising() -> None:
    text = _capture(
        "import sys\n"
        "sys.stdout.buffer.write(b'err:module:import_dll \\xff\\xfe bad\\n')\n"
        "sys.stdout.buffer.flush()\n"
    )

    assert "err:module:import_dll" in text
    assert "\ufffd" in text


def test_no_output_yields_an_empty_string() -> None:
    assert _capture("pass\n") == ""


def test_capture_keeps_the_first_bytes_of_a_large_line() -> None:
    text = _capture(
        "import sys\n"
        f"sys.stdout.write('A' * {MAX_CAPTURE_BYTES + 1000})\n"
        "sys.stdout.flush()\n"
    )

    assert text == "A" * MAX_CAPTURE_BYTES


def _run_main_loop_until(predicate, timeout_ms: int = 20000) -> None:
    from gi.repository import GLib

    loop = GLib.MainLoop()
    state: dict[str, int] = {}

    def check() -> bool:
        if predicate():
            loop.quit()
            for key in ("check", "timeout"):
                source = state.pop(key, None)
                if source is not None:
                    GLib.source_remove(source)
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def timeout() -> bool:
        loop.quit()
        return GLib.SOURCE_REMOVE

    state["check"] = GLib.timeout_add(10, check)
    state["timeout"] = GLib.timeout_add(timeout_ms, timeout)
    loop.run()
    assert not state, "Timed out waiting for the launch to finish"


def _fake_wine(path) -> None:
    """Write a stand-in Wine that fakes wineboot and floods stdout."""
    body = (
        "#!/bin/sh\n"
        'if [ "$1" = "wineboot" ]; then\n'
        '  mkdir -p "$WINEPREFIX/drive_c"\n'
        '  : > "$WINEPREFIX/system.reg"\n'
        "  exit 0\n"
        "fi\n"
        "exec " + sys.executable + " -c "
        "'import sys; sys.stdout.write(\"err:module:import_dll \" + "
        "\"x\" * 200000); sys.stdout.flush(); sys.exit(3)'\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def test_windows_launch_writes_a_bounded_log_and_reports_the_exit_code(
    tmp_path,
) -> None:
    """The full manager path captures real child output and keeps it bounded."""
    from minimal_game_launcher.models import GameDraft, Platform
    from minimal_game_launcher.services.game_manager import GameManager
    from minimal_game_launcher.services.launch_manager import LaunchManager
    from minimal_game_launcher.settings import (
        LauncherSettings,
        SettingsStore,
        WineSource,
    )
    from minimal_game_launcher.database import Database

    database = Database(tmp_path / "library.db")
    manager = GameManager(database, tmp_path / "prefixes")
    try:
        folder = tmp_path / "game"
        folder.mkdir()
        executable = folder / "game.exe"
        executable.write_bytes(b"MZ")
        game = manager.add_game(
            GameDraft(
                name="Flood",
                platform=Platform.WINDOWS,
                executable=str(executable),
            )
        )

        wine = tmp_path / "bin" / "wine"
        _fake_wine(wine)
        store = SettingsStore(tmp_path / "config" / "settings.json")
        store.save(
            LauncherSettings(
                wine_source=WineSource.CUSTOM, custom_wine_path=str(wine)
            )
        )
        state = tmp_path / "state"
        launcher = LaunchManager(
            tmp_path / "prefixes",
            store,
            pidfiles_root=state / "processes",
            poll_interval_ms=10,
            which=lambda _name: None,
        )
        finished: list[int] = []
        launcher.connect(
            "launch-finished",
            lambda _manager, _game_id, code: finished.append(code),
        )
        launcher.connect(
            "launch-failed",
            lambda _manager, _game_id, message: pytest.fail(message),
        )

        assert launcher.launch(game)
        _run_main_loop_until(lambda: bool(finished))
        launcher.shutdown()

        assert finished == [3]
        log = (state / "launch-logs" / f"{game.id}.log").read_bytes()
        header, _, body = log.partition(b"\n")
        assert header == b"exit_status=3"
        assert len(body) == MAX_CAPTURE_BYTES
        assert body.startswith(b"err:module:import_dll ")
        import stat

        mode = stat.S_IMODE(
            (state / "launch-logs" / f"{game.id}.log").stat().st_mode
        )
        assert mode == 0o600
        assert (
            stat.S_IMODE((state / "launch-logs").stat().st_mode) == 0o700
        )
    finally:
        database.close()
