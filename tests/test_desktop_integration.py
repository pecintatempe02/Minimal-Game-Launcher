"""User-level XDG desktop entry integration and launch-target detection."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from minimal_game_launcher import APP_ID, paths
from minimal_game_launcher.errors import DesktopIntegrationError
from minimal_game_launcher.services import desktop_integration
from minimal_game_launcher.services.desktop_integration import (
    DesktopEntryState,
    DesktopIntegrationService,
    detect_launch_command,
    format_exec,
    quote_exec_argument,
)
from minimal_game_launcher.settings import LauncherSettings, SettingsStore


def _service(tmp_path: Path, *, command: list[str] | None = None, system=()):
    applications = tmp_path / "data" / "applications"
    icon = tmp_path / "data" / "icons" / "hicolor" / "scalable" / "apps"
    return DesktopIntegrationService(
        entry_path=applications / f"{APP_ID}.desktop",
        icon_path=icon / f"{APP_ID}.svg",
        application_directory=applications,
        system_directories=system,
        launch_command=command or ["/opt/mgl/minimal-game-launcher"],
    )


def _system_entry(tmp_path: Path, text: str = "[Desktop Entry]\nName=System\n") -> Path:
    directory = tmp_path / "system" / "applications"
    directory.mkdir(parents=True)
    (directory / f"{APP_ID}.desktop").write_text(text, encoding="utf-8")
    return directory


def test_paths_follow_xdg_defaults_and_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    monkeypatch.setenv("XDG_DATA_DIRS", "/opt/share:/usr/share")
    assert paths.applications_directory() == tmp_path / "share" / "applications"
    assert paths.desktop_entry_path() == (
        tmp_path / "share" / "applications" / f"{APP_ID}.desktop"
    )
    assert paths.desktop_icon_path() == (
        tmp_path
        / "share"
        / "icons"
        / "hicolor"
        / "scalable"
        / "apps"
        / f"{APP_ID}.svg"
    )
    assert paths.system_application_directories() == (
        Path("/opt/share/applications"),
        Path("/usr/share/applications"),
    )

    monkeypatch.delenv("XDG_DATA_HOME")
    monkeypatch.delenv("XDG_DATA_DIRS")
    assert paths.applications_directory() == (
        Path.home() / ".local" / "share" / "applications"
    )


def test_enable_writes_one_canonical_entry_and_icon(tmp_path: Path) -> None:
    service = _service(tmp_path)

    assert service.enable() is DesktopEntryState.ENABLED

    text = service.entry_path.read_text(encoding="utf-8")
    for expected in (
        "[Desktop Entry]",
        "Type=Application",
        "Name=Minimal Game Launcher",
        f"Icon={APP_ID}",
        "Terminal=false",
        "Categories=Game;Utility;",
        "Keywords=games;library;wine;launcher;",
        "StartupNotify=true",
        f"StartupWMClass={APP_ID}",
        "X-MGL-Managed=true",
        f'Exec="/opt/mgl/minimal-game-launcher"',
    ):
        assert expected in text
    assert "Hidden=true" not in text
    assert service.icon_path.is_file()
    assert [entry.name for entry in service.entry_path.parent.glob("*.desktop")] == [
        f"{APP_ID}.desktop"
    ]


def test_exec_escaping_handles_reserved_characters() -> None:
    assert quote_exec_argument("plain") == '"plain"'
    assert quote_exec_argument("with space") == '"with space"'
    assert quote_exec_argument("100%") == '"100%%"'
    assert quote_exec_argument('say "hi"') == '"say \\"hi\\""'
    assert quote_exec_argument("back\\slash") == '"back\\\\slash"'
    assert quote_exec_argument("$HOME") == '"\\$HOME"'
    assert quote_exec_argument("`id`") == '"\\`id\\`"'
    assert format_exec(["/tmp/My App/mgl", "a%b"]) == (
        '"/tmp/My App/mgl" "a%%b"'
    )
    with pytest.raises(DesktopIntegrationError):
        format_exec([])


def test_repeated_enable_is_idempotent(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.enable()
    before = service.entry_path.stat()

    assert service.enable() is DesktopEntryState.ENABLED
    after = service.entry_path.stat()

    assert after.st_ino == before.st_ino
    assert after.st_mtime_ns == before.st_mtime_ns
    assert not list(service.entry_path.parent.glob(f".{APP_ID}.desktop.*"))


def test_disable_removes_only_the_owned_entry(tmp_path: Path) -> None:
    service = _service(tmp_path)
    unrelated = service.entry_path.parent / "other.desktop"
    service.entry_path.parent.mkdir(parents=True)
    unrelated.write_text("[Desktop Entry]\nName=Other\n", encoding="utf-8")
    service.enable()

    assert service.disable() is DesktopEntryState.ABSENT
    assert not service.entry_path.exists()
    assert unrelated.is_file()
    assert service.icon_path.is_file()  # disabling leaves the icon in place


def test_unowned_entry_is_never_deleted_or_overwritten(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.entry_path.parent.mkdir(parents=True)
    original = "[Desktop Entry]\nType=Application\nName=Not ours\n"
    service.entry_path.write_text(original, encoding="utf-8")

    assert service.state() is DesktopEntryState.UNOWNED
    assert service.disable() is DesktopEntryState.UNOWNED
    assert service.entry_path.read_text(encoding="utf-8") == original

    with pytest.raises(DesktopIntegrationError, match="not managed by MGL"):
        service.enable()
    assert service.entry_path.read_text(encoding="utf-8") == original


def test_disable_creates_hidden_override_for_system_entry(tmp_path: Path) -> None:
    system = _system_entry(tmp_path)
    system_text = (system / f"{APP_ID}.desktop").read_text(encoding="utf-8")
    service = _service(tmp_path, system=(system,))

    service.enable()
    assert service.state() is DesktopEntryState.ENABLED

    assert service.disable() is DesktopEntryState.DISABLED
    hidden = service.entry_path.read_text(encoding="utf-8")
    assert "Hidden=true" in hidden
    assert "X-MGL-Managed=true" in hidden
    assert (system / f"{APP_ID}.desktop").read_text(encoding="utf-8") == system_text

    assert service.enable() is DesktopEntryState.ENABLED
    assert "Hidden=true" not in service.entry_path.read_text(encoding="utf-8")


def test_system_entry_reads_as_enabled_without_a_user_file(tmp_path: Path) -> None:
    system = _system_entry(tmp_path)
    service = _service(tmp_path, system=(system,))
    assert service.state() is DesktopEntryState.ENABLED
    assert service.is_enabled() is True


def test_disable_writes_override_when_only_a_system_entry_exists(
    tmp_path: Path,
) -> None:
    system = _system_entry(tmp_path)
    system_text = (system / f"{APP_ID}.desktop").read_text(encoding="utf-8")
    service = _service(tmp_path, system=(system,))

    assert service.disable() is DesktopEntryState.DISABLED

    text = service.entry_path.read_text(encoding="utf-8")
    assert "Hidden=true" in text
    assert "X-MGL-Managed=true" in text
    assert (system / f"{APP_ID}.desktop").read_text(encoding="utf-8") == system_text
    assert service.is_enabled() is False


def test_unwritable_directory_leaves_previous_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.geteuid() == 0:
        pytest.skip("root bypasses directory permissions")
    service = _service(tmp_path)
    service.enable()
    original = service.entry_path.read_text(encoding="utf-8")

    service.entry_path.parent.chmod(0o500)
    try:
        with pytest.raises(DesktopIntegrationError):
            service.disable()
        assert service.entry_path.read_text(encoding="utf-8") == original
    finally:
        service.entry_path.parent.chmod(0o700)


def test_reconcile_none_never_touches_the_filesystem(tmp_path: Path) -> None:
    service = _service(tmp_path)

    assert service.reconcile(None) is DesktopEntryState.ABSENT
    assert not service.entry_path.parent.exists()
    assert not service.icon_path.parent.exists()


def test_reconcile_true_and_false_follow_the_preference(tmp_path: Path) -> None:
    service = _service(tmp_path)

    assert service.reconcile(True) is DesktopEntryState.ENABLED
    assert service.reconcile(False) is DesktopEntryState.ABSENT
    assert not service.entry_path.exists()


def test_cache_refresh_failure_does_not_invalidate_the_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    monkeypatch.setattr(
        desktop_integration.shutil, "which", lambda _name: "/usr/bin/true"
    )

    def fail(*_args, **_kwargs):
        raise OSError("no database tool")

    monkeypatch.setattr(desktop_integration.subprocess, "run", fail)

    assert service.enable() is DesktopEntryState.ENABLED
    assert service.entry_path.is_file()


def test_launcher_configuration_is_never_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fuzzel = tmp_path / "config" / "fuzzel" / "fuzzel.ini"
    hypr = tmp_path / "config" / "hypr" / "hyprland.conf"
    fuzzel.parent.mkdir(parents=True)
    hypr.parent.mkdir(parents=True)
    fuzzel.write_text("fuzzel\n", encoding="utf-8")
    hypr.write_text("hyprland\n", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    service = _service(tmp_path)
    service.enable()
    service.disable()

    assert fuzzel.read_text(encoding="utf-8") == "fuzzel\n"
    assert hypr.read_text(encoding="utf-8") == "hyprland\n"


# ---------------------------------------------------------------------------
# Launch target detection


def test_detect_appimage_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    appimage = tmp_path / "Minimal Game Launcher.AppImage"
    appimage.write_bytes(b"x")
    appimage.chmod(0o755)
    monkeypatch.setenv("APPIMAGE", str(appimage))
    monkeypatch.setenv("APPDIR", str(tmp_path / "appdir"))

    assert detect_launch_command() == [str(appimage.resolve())]


def test_detect_appdir_when_no_appimage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    appdir = tmp_path / "appdir"
    apprun = appdir / "AppRun"
    apprun.parent.mkdir(parents=True)
    apprun.write_bytes(b"#!/bin/sh\n")
    apprun.chmod(0o755)
    monkeypatch.delenv("APPIMAGE", raising=False)
    monkeypatch.setenv("APPDIR", str(appdir))
    monkeypatch.setattr(desktop_integration.shutil, "which", lambda _name: None)

    assert detect_launch_command() == [str(apprun.resolve())]


def test_detect_installed_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installed = tmp_path / "bin" / "minimal-game-launcher"
    installed.parent.mkdir(parents=True)
    installed.write_bytes(b"#!/bin/sh\n")
    installed.chmod(0o755)
    monkeypatch.delenv("APPIMAGE", raising=False)
    monkeypatch.delenv("APPDIR", raising=False)
    monkeypatch.setattr(
        desktop_integration.shutil, "which", lambda _name: str(installed)
    )

    assert detect_launch_command() == [str(installed.resolve())]


def test_detect_wrapper_beside_the_current_program(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    program = tmp_path / "bin" / "mgl"
    wrapper = program.parent / "minimal-game-launcher"
    program.parent.mkdir(parents=True)
    wrapper.write_bytes(b"#!/bin/sh\n")
    wrapper.chmod(0o755)
    monkeypatch.delenv("APPIMAGE", raising=False)
    monkeypatch.delenv("APPDIR", raising=False)
    monkeypatch.setattr(desktop_integration.shutil, "which", lambda _name: None)
    monkeypatch.setattr(sys, "argv", [str(program)])

    assert detect_launch_command() == [str(wrapper)]


def test_detect_source_checkout_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("APPIMAGE", raising=False)
    monkeypatch.delenv("APPDIR", raising=False)
    monkeypatch.setattr(desktop_integration.shutil, "which", lambda _name: None)
    monkeypatch.setattr(sys, "argv", [str(Path("/nonexistent/mgl"))])

    command = detect_launch_command()

    package_root = Path(desktop_integration.__file__).resolve().parents[2]
    assert command[0].endswith("env")
    assert f"PYTHONPATH={package_root}" in command
    assert command[-3:] == [sys.executable, "-m", "minimal_game_launcher"]
    assert Path(package_root).is_absolute()


# ---------------------------------------------------------------------------
# Settings persistence


def test_desktop_entry_setting_round_trips(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    assert store.load().desktop_entry_enabled is None

    for value in (True, False, None):
        store.save(LauncherSettings(desktop_entry_enabled=value))
        assert store.load().desktop_entry_enabled is value


def test_invalid_desktop_entry_setting_loads_as_none(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text('{"desktop_entry_enabled": "yes"}', encoding="utf-8")
    assert SettingsStore(path).load().desktop_entry_enabled is None
