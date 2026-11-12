"""Tests for on-demand Winecfg and Winetricks integration.

No real Wine or Winetricks process is started. Ownership, PATH lookup, and
subprocess creation are all injected or isolated in temporary directories.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from minimal_game_launcher.errors import WineToolError
from minimal_game_launcher.models import Game, Platform
from minimal_game_launcher.services.launch_manager import PREFIX_READY_MARKER
from minimal_game_launcher.services.ownership import OwnershipService
from minimal_game_launcher.services.wine_tools import WineToolsService
from minimal_game_launcher.settings import SettingsStore


class FakePopen:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, object]]] = []
        self.pid = 9876

    def __call__(self, command: list[str], **kwargs: object) -> "FakePopen":
        self.calls.append((list(command), kwargs))
        return self


def _game(prefix: Path, *, platform: Platform = Platform.WINDOWS) -> Game:
    return Game(
        id="game-1",
        name="Game",
        platform=platform,
        executable="/games/game.exe",
        working_directory="/games",
        launch_arguments="",
        cover_path=None,
        wine_prefix_path=str(prefix),
        gamemode_enabled=False,
        mangohud_enabled=False,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )


def _prefix(root: Path, game_id: str = "game-1", *, ready: bool = True) -> Path:
    prefix = root / game_id
    prefix.mkdir(parents=True)
    if ready:
        (prefix / "system.reg").write_text("registry\n", encoding="utf-8")
        (prefix / "drive_c").mkdir()
        (prefix / PREFIX_READY_MARKER).write_text("ready\n", encoding="utf-8")
    return prefix


def _tools(
    tmp_path: Path,
    popen: FakePopen,
    *,
    wine: Path | None,
    winetricks: Path | None,
) -> WineToolsService:
    paths = {str(tool): str(path) for tool, path in (("wine", wine), ("winetricks", winetricks)) if path}

    def which(name: str) -> str | None:
        return paths.get(name)

    prefixes = tmp_path / "prefixes"
    return WineToolsService(
        prefixes,
        SettingsStore(tmp_path / "config" / "settings.json"),
        ownership=OwnershipService(
            prefixes,
            tmp_path / "games",
            tmp_path / "installers",
        ),
        state_root=tmp_path / "state",
        which=which,
        popen_factory=popen,
    )


@pytest.fixture
def wine(tmp_path: Path) -> Path:
    path = tmp_path / "bin" / "wine"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def winetricks(tmp_path: Path) -> Path:
    path = tmp_path / "bin" / "winetricks"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_winecfg_starts_with_verified_prefix_and_no_shell(
    tmp_path: Path, wine: Path
) -> None:
    prefix = _prefix(tmp_path / "prefixes")
    popen = FakePopen()
    service = _tools(tmp_path, popen, wine=wine, winetricks=None)

    result = service.run_winecfg(_game(prefix))

    command, kwargs = popen.calls[0]
    assert command == [str(wine), "winecfg"]
    assert kwargs["shell"] is False
    assert kwargs["start_new_session"] is True
    assert kwargs["env"]["WINEPREFIX"] == str(prefix)  # type: ignore[index]
    assert kwargs["env"]["WINE"] == str(wine)  # type: ignore[index]
    assert result.log_path == tmp_path / "state" / "wine-tools" / "game-1.log"
    assert result.log_path.parent.is_dir()


def test_winetricks_validates_verbs_and_passes_them_as_arguments(
    tmp_path: Path, wine: Path, winetricks: Path
) -> None:
    prefix = _prefix(tmp_path / "prefixes")
    popen = FakePopen()
    service = _tools(tmp_path, popen, wine=wine, winetricks=winetricks)

    service.run_winetricks(_game(prefix), "vcrun2019 dotnet48")

    command, _kwargs = popen.calls[0]
    assert command == [str(winetricks), "vcrun2019", "dotnet48"]


@pytest.mark.parametrize(
    "verbs", ["vcrun2019;rm -rf /", "$(id)", "--force", ""]
)
def test_invalid_or_empty_verbs_are_rejected(
    tmp_path: Path, wine: Path, winetricks: Path, verbs: str
) -> None:
    prefix = _prefix(tmp_path / "prefixes")
    popen = FakePopen()
    service = _tools(tmp_path, popen, wine=wine, winetricks=winetricks)

    with pytest.raises(WineToolError):
        service.run_winetricks(_game(prefix), verbs)
    assert popen.calls == []


def test_external_prefix_is_never_modified(
    tmp_path: Path, wine: Path, winetricks: Path
) -> None:
    external = tmp_path / "external-prefix"
    external.mkdir()
    popen = FakePopen()
    service = _tools(tmp_path, popen, wine=wine, winetricks=winetricks)

    with pytest.raises(WineToolError, match="external prefix"):
        service.run_winecfg(_game(external))
    assert popen.calls == []


def test_uninitialized_prefix_is_refused(
    tmp_path: Path, wine: Path
) -> None:
    prefix = _prefix(tmp_path / "prefixes", ready=False)
    popen = FakePopen()
    service = _tools(tmp_path, popen, wine=wine, winetricks=None)

    with pytest.raises(WineToolError, match="not initialized"):
        service.run_winecfg(_game(prefix))
    assert popen.calls == []


def test_missing_winetricks_is_reported(
    tmp_path: Path, wine: Path
) -> None:
    prefix = _prefix(tmp_path / "prefixes")
    popen = FakePopen()
    service = _tools(tmp_path, popen, wine=wine, winetricks=None)

    with pytest.raises(WineToolError, match="not installed"):
        service.run_winetricks(_game(prefix), "vcrun2019")


def test_native_game_is_refused(tmp_path: Path, wine: Path) -> None:
    prefix = _prefix(tmp_path / "prefixes")
    popen = FakePopen()
    service = _tools(tmp_path, popen, wine=wine, winetricks=None)

    with pytest.raises(WineToolError, match="only for Windows"):
        service.run_winecfg(_game(prefix, platform=Platform.LINUX))


def test_detection_reports_optional_tools(
    tmp_path: Path, wine: Path, winetricks: Path
) -> None:
    service = _tools(
        tmp_path, FakePopen(), wine=wine, winetricks=winetricks
    )

    availability = service.detect()

    assert availability.wine == str(wine)
    assert availability.winetricks == str(winetricks)
    assert availability.winetricks_available is True


def test_tool_log_is_created_with_restrictive_permissions(
    tmp_path: Path, wine: Path
) -> None:
    import stat

    prefix = _prefix(tmp_path / "prefixes")
    service = _tools(tmp_path, FakePopen(), wine=wine, winetricks=None)

    result = service.run_winecfg(_game(prefix))

    assert result.log_path is not None
    mode = stat.S_IMODE(result.log_path.stat().st_mode)
    assert mode == 0o600
    assert stat.S_IMODE(result.log_path.parent.stat().st_mode) == 0o700


def test_existing_world_readable_log_is_tightened(
    tmp_path: Path, wine: Path
) -> None:
    import stat

    prefix = _prefix(tmp_path / "prefixes")
    log = tmp_path / "state" / "wine-tools" / "game-1.log"
    log.parent.mkdir(parents=True)
    log.write_text("old\n", encoding="utf-8")
    log.chmod(0o644)
    service = _tools(tmp_path, FakePopen(), wine=wine, winetricks=None)

    service.run_winecfg(_game(prefix))

    assert stat.S_IMODE(log.stat().st_mode) == 0o600


def test_process_start_failure_is_reported(tmp_path: Path, wine: Path) -> None:
    prefix = _prefix(tmp_path / "prefixes")

    def explode(*_args: object, **_kwargs: object):
        raise OSError(13, "Permission denied")

    service = _tools(
        tmp_path, FakePopen(), wine=wine, winetricks=None
    )
    service._popen_factory = explode  # type: ignore[attr-defined]

    with pytest.raises(WineToolError, match="could not be started"):
        service.run_winecfg(_game(prefix))

