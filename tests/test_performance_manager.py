"""Tests for GameMode/MangoHud detection and command composition.

No optional tool is executed and no real game is started. ``which`` is injected
so every installed/missing combination is deterministic on any development
machine.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import gi
import pytest

gi.require_version("GLib", "2.0")

from gi.repository import GLib  # noqa: E402

from minimal_game_launcher.models import Game, GameDraft, Platform  # noqa: E402
from minimal_game_launcher.services.game_manager import GameManager  # noqa: E402
from minimal_game_launcher.services.launch_manager import LaunchManager  # noqa: E402
from minimal_game_launcher.services.performance_manager import (  # noqa: E402
    PerformanceAvailability,
    PerformanceManager,
)
from minimal_game_launcher.settings import SettingsStore  # noqa: E402

GAMEMODERUN = "/usr/bin/gamemoderun"
MANGOHUD = "/usr/bin/mangohud"


def _availability(
    gamemoderun: str | None = GAMEMODERUN,
    mangohud: str | None = MANGOHUD,
) -> PerformanceAvailability:
    return PerformanceAvailability(
        gamemoderun=gamemoderun, mangohud=mangohud
    )


def _native_game(
    manager: GameManager, make_executable, **overrides
) -> Game:
    executable = make_executable(f"native-{len(overrides)}")
    values = {
        "name": "Native Game",
        "platform": Platform.LINUX,
        "executable": str(executable),
        "launch_arguments": "--windowed --lang \"en US\"",
    }
    values.update(overrides)
    return manager.add_game(GameDraft(**values))


def _wine_game(manager: GameManager, tmp_path: Path, **overrides) -> Game:
    folder = tmp_path / "Wine Game"
    folder.mkdir(parents=True, exist_ok=True)
    executable = folder / "game.exe"
    executable.write_bytes(b"MZ")
    values = {
        "name": "Wine Game",
        "platform": Platform.WINDOWS,
        "executable": str(executable),
        "launch_arguments": "--fullscreen",
    }
    values.update(overrides)
    return manager.add_game(GameDraft(**values))


def _environment() -> dict[str, str]:
    """Include inherited MangoHud and Wine state to prove per-game control."""
    return {
        "PATH": "/usr/bin",
        "MANGOHUD": "1",
        "WINEPREFIX": "/inherited/prefix",
    }


def test_neither_wrapper_enabled(
    manager: GameManager, make_executable
) -> None:
    """Disabled toggles leave the base command and remove inherited MangoHud."""
    game = _native_game(
        manager,
        make_executable,
        gamemode_enabled=False,
        mangohud_enabled=False,
    )
    plan = PerformanceManager().compose(
        game,
        ("/games/native", "--windowed"),
        _environment(),
        _availability(),
    )

    assert plan.command == ("/games/native", "--windowed")
    assert "MANGOHUD" not in plan.environment
    assert plan.environment["WINEPREFIX"] == "/inherited/prefix"
    assert plan.warnings == ()


def test_gamemode_only_wraps_native_game(
    manager: GameManager, make_executable
) -> None:
    """GameMode wraps the game and stays outside user arguments."""
    game = _native_game(
        manager, make_executable, gamemode_enabled=True, mangohud_enabled=False
    )
    plan = PerformanceManager().compose(
        game, ("/games/native", "--windowed"), _environment(), _availability()
    )

    assert plan.command == (GAMEMODERUN, "/games/native", "--windowed")
    assert "MANGOHUD" not in plan.environment
    assert plan.warnings == ()


def test_mangohud_only_wraps_native_game(
    manager: GameManager, make_executable
) -> None:
    """MangoHud is a wrapper for native games."""
    game = _native_game(
        manager, make_executable, gamemode_enabled=False, mangohud_enabled=True
    )
    plan = PerformanceManager().compose(
        game, ("/games/native", "--windowed"), _environment(), _availability()
    )

    assert plan.command == (MANGOHUD, "/games/native", "--windowed")
    assert "MANGOHUD" not in plan.environment
    assert plan.warnings == ()


def test_both_wrappers_use_correct_order(
    manager: GameManager, make_executable
) -> None:
    """GameMode is outermost; MangoHud wraps the game inside it."""
    game = _native_game(
        manager, make_executable, gamemode_enabled=True, mangohud_enabled=True
    )
    plan = PerformanceManager().compose(
        game, ("/games/native", "--windowed"), _environment(), _availability()
    )

    assert plan.command == (
        GAMEMODERUN,
        MANGOHUD,
        "/games/native",
        "--windowed",
    )
    assert plan.warnings == ()


def test_missing_gamemode_falls_back_with_warning(
    manager: GameManager, make_executable
) -> None:
    """A missing GameMode does not prevent launching and reports why."""
    game = _native_game(
        manager, make_executable, gamemode_enabled=True, mangohud_enabled=False
    )
    plan = PerformanceManager().compose(
        game,
        ("/games/native",),
        _environment(),
        _availability(gamemoderun=None),
    )

    assert plan.command == ("/games/native",)
    assert len(plan.warnings) == 1
    assert "gamemoderun was not found" in plan.warnings[0]


def test_missing_mangohud_falls_back_with_warning(
    manager: GameManager, make_executable
) -> None:
    """A missing MangoHud wrapper is reported for native games."""
    game = _native_game(
        manager, make_executable, gamemode_enabled=False, mangohud_enabled=True
    )
    plan = PerformanceManager().compose(
        game,
        ("/games/native",),
        _environment(),
        _availability(mangohud=None),
    )

    assert plan.command == ("/games/native",)
    assert len(plan.warnings) == 1
    assert "mangohud was not found" in plan.warnings[0]
    assert "MANGOHUD" not in plan.environment


def test_both_missing_launches_unwrapped_with_two_warnings(
    manager: GameManager, make_executable
) -> None:
    """The launcher remains usable when neither optional tool is installed."""
    game = _native_game(
        manager, make_executable, gamemode_enabled=True, mangohud_enabled=True
    )
    plan = PerformanceManager().compose(
        game,
        ("/games/native",),
        _environment(),
        _availability(gamemoderun=None, mangohud=None),
    )

    assert plan.command == ("/games/native",)
    assert len(plan.warnings) == 2
    assert any("gamemoderun" in warning for warning in plan.warnings)
    assert any("mangohud" in warning for warning in plan.warnings)


def test_wine_both_enabled_uses_env_not_mangohud_wrapper(
    manager: GameManager, tmp_path: Path
) -> None:
    """Wine games use MANGOHUD=1 and only GameMode as an executable wrapper."""
    game = _wine_game(
        manager, tmp_path, gamemode_enabled=True, mangohud_enabled=True
    )
    plan = PerformanceManager().compose(
        game,
        ("/usr/bin/wine", str(game.executable), "--fullscreen"),
        _environment(),
        _availability(),
    )

    assert plan.command == (
        GAMEMODERUN,
        "/usr/bin/wine",
        str(game.executable),
        "--fullscreen",
    )
    assert plan.environment["MANGOHUD"] == "1"
    assert plan.environment["WINEPREFIX"] == "/inherited/prefix"
    assert plan.warnings == ()


def test_wine_mangohud_without_wrapper_is_disabled(
    manager: GameManager, tmp_path: Path
) -> None:
    """A missing MangoHud binary disables the Wine overlay and warns."""
    game = _wine_game(
        manager, tmp_path, gamemode_enabled=False, mangohud_enabled=True
    )
    plan = PerformanceManager().compose(
        game,
        ("/usr/bin/wine", str(game.executable)),
        _environment(),
        _availability(mangohud=None),
    )

    assert plan.command == ("/usr/bin/wine", str(game.executable))
    assert "MANGOHUD" not in plan.environment
    assert len(plan.warnings) == 1
    assert "Wine overlay" in plan.warnings[0]
    assert "32-bit Vulkan" in plan.warnings[0]


def test_detection_uses_the_injected_which() -> None:
    """Detection resolves both optional tools from PATH without running them."""
    calls: list[str] = []

    def which(name: str) -> str | None:
        calls.append(name)
        return {"gamemoderun": GAMEMODERUN, "mangohud": MANGOHUD}.get(name)

    availability = PerformanceManager(which=which).detect()

    assert calls == ["gamemoderun", "mangohud"]
    assert availability.gamemoderun == GAMEMODERUN
    assert availability.mangohud == MANGOHUD
    assert availability.gamemode_available
    assert availability.mangohud_available


class FakeProcess:
    """A process that exits on the first poll."""

    pid = 2468

    def poll(self) -> int:
        return 0


class FakePopen:
    """Record the final command and environment passed to Popen."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(self, command: list[str], **kwargs: object) -> FakeProcess:
        self.calls.append((list(command), kwargs))
        return FakeProcess()


class FakeWineboot:
    """Initialize enough of a prefix to satisfy the reuse check."""

    def __call__(
        self, command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess:
        prefix = Path(str(kwargs["env"]["WINEPREFIX"]))  # type: ignore[index]
        prefix.mkdir(parents=True, exist_ok=True)
        (prefix / "system.reg").write_text("fake registry\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


def _run_main_loop_until(predicate, timeout_ms: int = 2000) -> None:
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
    assert not sources, "Timed out waiting for launch completion"


def _performance_manager(
    gamemoderun: str | None,
    mangohud: str | None,
) -> PerformanceManager:
    def which(name: str) -> str | None:
        return {"gamemoderun": gamemoderun, "mangohud": mangohud}.get(name)

    return PerformanceManager(which=which)


def test_launch_manager_applies_wrappers_and_wine_environment(
    manager: GameManager, tmp_path: Path
) -> None:
    """The spawn path receives wrappers, MANGOHUD, and the game's WINEPREFIX."""
    game = _wine_game(
        manager, tmp_path, gamemode_enabled=True, mangohud_enabled=True
    )
    popen = FakePopen()
    launcher = LaunchManager(
        tmp_path / "prefixes",
        SettingsStore(tmp_path / "settings.json"),
        performance_manager=_performance_manager(GAMEMODERUN, MANGOHUD),
        run_factory=FakeWineboot(),
        popen_factory=popen,
        poll_interval_ms=10,
        which=lambda _name: "/usr/bin/wine",
    )
    finished: list[int] = []
    launcher.connect(
        "launch-finished",
        lambda _manager, _game_id, code: finished.append(code),
    )

    assert launcher.launch(game)
    _run_main_loop_until(lambda: bool(finished))
    launcher.shutdown()

    command, kwargs = popen.calls[0]
    assert command == [
        GAMEMODERUN,
        "/usr/bin/wine",
        game.executable,
        "--fullscreen",
    ]
    environment = kwargs["env"]
    assert isinstance(environment, dict)
    assert environment["MANGOHUD"] == "1"
    assert environment["WINEPREFIX"] == game.wine_prefix_path
    assert kwargs["shell"] is False


def test_launch_manager_emits_warning_for_missing_gamemode(
    manager: GameManager, make_executable, tmp_path: Path
) -> None:
    """Missing optional tools produce a non-fatal launch warning."""
    game = _native_game(
        manager, make_executable, gamemode_enabled=True, mangohud_enabled=False
    )
    popen = FakePopen()
    launcher = LaunchManager(
        tmp_path / "prefixes",
        SettingsStore(tmp_path / "settings.json"),
        performance_manager=_performance_manager(None, None),
        popen_factory=popen,
        poll_interval_ms=10,
    )
    warnings: list[str] = []
    finished: list[int] = []
    launcher.connect(
        "launch-warning",
        lambda _manager, _game_id, message: warnings.append(message),
    )
    launcher.connect(
        "launch-finished",
        lambda _manager, _game_id, code: finished.append(code),
    )

    assert launcher.launch(game)
    _run_main_loop_until(lambda: bool(finished))
    launcher.shutdown()

    assert popen.calls[0][0] == [game.executable, "--windowed", "--lang", "en US"]
    assert len(warnings) == 1
    assert "gamemoderun was not found" in warnings[0]


def test_disabled_wrappers_skip_detection(
    manager: GameManager, make_executable, tmp_path: Path
) -> None:
    """Games with both toggles off do not consult PATH for optional tools."""
    game = _native_game(
        manager,
        make_executable,
        gamemode_enabled=False,
        mangohud_enabled=False,
    )

    def fail_which(_name: str) -> str | None:
        raise AssertionError("Disabled wrappers must not be detected")

    launcher = LaunchManager(
        tmp_path / "prefixes",
        SettingsStore(tmp_path / "settings.json"),
        performance_manager=PerformanceManager(which=fail_which),
    )
    spec = launcher.prepare_launch(game)

    assert spec.command == (
        game.executable,
        "--windowed",
        "--lang",
        "en US",
    )
    assert spec.warnings == ()
