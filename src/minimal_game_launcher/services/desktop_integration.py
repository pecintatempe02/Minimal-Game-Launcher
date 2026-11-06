"""Own one user-level XDG desktop entry for Minimal Game Launcher itself.

The runtime service writes exactly one entry, named after :data:`APP_ID`, under
``$XDG_DATA_HOME/applications`` and one matching icon under
``$XDG_DATA_HOME/icons/hicolor/scalable/apps``. It never edits Fuzzel,
Hyprland, or system application directories, and it only removes files that
carry the ``X-MGL-Managed=true`` marker.

A user entry shadows a same-ID system entry. When the user disables the entry
while a system-level copy exists, the user-level file is rewritten as a
``Hidden=true`` override so the packaged entry cannot reappear.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import suppress
from enum import StrEnum
from pathlib import Path

from minimal_game_launcher import APP_ID, APP_NAME
from minimal_game_launcher import paths
from minimal_game_launcher.errors import DesktopIntegrationError

LOGGER = logging.getLogger(__name__)

MANAGED_KEY = "X-MGL-Managed"
MANAGED_MARKER = f"{MANAGED_KEY}=true"
COMMAND_NAME = "minimal-game-launcher"
COMMENT = "Minimal GTK4 game library and launcher"
CATEGORIES = "Game;Utility;"
KEYWORDS = "games;library;wine;launcher;"
_REFRESH_TIMEOUT_SECONDS = 10


class DesktopEntryState(StrEnum):
    """What currently controls the user-visible MGL launcher entry."""

    ABSENT = "absent"
    ENABLED = "enabled"
    DISABLED = "disabled"
    UNOWNED = "unowned"


def quote_exec_argument(value: str) -> str:
    """Quote one argument for the ``Exec=`` key.

    Inside double quotes the specification reserves ``"``, ``\\``, ``$``, and
    ````` as backslash-escaped characters. A literal ``%`` is written as
    ``%%`` because field codes are expanded by the launcher.
    """
    escaped = value.replace("\\", "\\\\")
    for character in ('"', "`", "$"):
        escaped = escaped.replace(character, f"\\{character}")
    escaped = escaped.replace("%", "%%")
    return f'"{escaped}"'


def format_exec(command: list[str]) -> str:
    """Return a desktop-entry ``Exec=`` value for an argument vector."""
    if not command:
        raise DesktopIntegrationError("No launch command could be determined.")
    return " ".join(quote_exec_argument(argument) for argument in command)


def _executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def detect_launch_command() -> list[str]:
    """Return argv that starts MGL without relying on the current directory.

    Order: the running AppImage, the running AppDir's ``AppRun``, the installed
    command, a wrapper beside the current program, then this source checkout's
    Python interpreter and module entry point.
    """
    appimage = os.environ.get("APPIMAGE")
    if appimage:
        candidate = Path(appimage).expanduser()
        if _executable(candidate):
            return [str(candidate.resolve())]

    appdir = os.environ.get("APPDIR")
    if appdir:
        apprun = Path(appdir).expanduser() / "AppRun"
        if _executable(apprun):
            return [str(apprun.resolve())]

    found = shutil.which(COMMAND_NAME)
    if found:
        return [str(Path(found).resolve())]

    if sys.argv and sys.argv[0]:
        beside = Path(sys.argv[0]).expanduser().resolve().parent / COMMAND_NAME
        if _executable(beside):
            return [str(beside)]

    package_root = Path(__file__).resolve().parents[2]
    if (package_root / "minimal_game_launcher" / "__main__.py").is_file():
        env = shutil.which("env") or "/usr/bin/env"
        return [
            env,
            f"PYTHONPATH={package_root}",
            sys.executable,
            "-m",
            "minimal_game_launcher",
        ]

    raise DesktopIntegrationError(
        "Minimal Game Launcher could not determine how it was started. "
        f"Add the {COMMAND_NAME} command to PATH and try again."
    )


class DesktopIntegrationService:
    """Create, update, or disable the user-level MGL desktop entry."""

    def __init__(
        self,
        *,
        entry_path: str | Path | None = None,
        icon_path: str | Path | None = None,
        icon_source: str | Path | None = None,
        application_directory: str | Path | None = None,
        system_directories: tuple[str | Path, ...] | None = None,
        launch_command: list[str] | None = None,
    ) -> None:
        self.entry_path = Path(
            entry_path if entry_path is not None else paths.desktop_entry_path()
        ).expanduser()
        self.icon_path = Path(
            icon_path if icon_path is not None else paths.desktop_icon_path()
        ).expanduser()
        self.application_directory = Path(
            application_directory
            if application_directory is not None
            else self.entry_path.parent
        ).expanduser()
        self.icon_source = Path(
            icon_source
            if icon_source is not None
            else Path(__file__).resolve().parents[1] / "ui" / f"{APP_ID}.svg"
        ).expanduser()
        self.system_directories = tuple(
            Path(directory).expanduser()
            for directory in (
                system_directories
                if system_directories is not None
                else paths.system_application_directories()
            )
        )
        self._launch_command = launch_command

    # ------------------------------------------------------------------
    # Inspection

    def state(self) -> DesktopEntryState:
        """Return the currently effective state without changing anything."""
        if os.path.lexists(self.entry_path):
            text = self._read_entry()
            if not _is_managed(text):
                return DesktopEntryState.UNOWNED
            return (
                DesktopEntryState.DISABLED
                if _is_hidden(text)
                else DesktopEntryState.ENABLED
            )
        if self._system_entry_exists():
            return DesktopEntryState.ENABLED
        return DesktopEntryState.ABSENT

    def is_enabled(self) -> bool:
        """Return whether the launcher entry is currently visible."""
        return self.state() is DesktopEntryState.ENABLED

    def _read_entry(self) -> str:
        try:
            return self.entry_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            LOGGER.exception("Could not read the desktop entry %s", self.entry_path)
            return ""

    def _system_entry_exists(self) -> bool:
        name = self.entry_path.name
        return any((directory / name).is_file() for directory in self.system_directories)

    # ------------------------------------------------------------------
    # Changes

    def apply(self, enabled: bool) -> DesktopEntryState:
        """Enable or disable the entry and return the resulting state."""
        return self.enable() if enabled else self.disable()

    def reconcile(self, enabled: bool | None) -> DesktopEntryState:
        """Apply an explicit preference; ``None`` leaves the filesystem alone."""
        if enabled is None:
            return self.state()
        return self.apply(enabled)

    def enable(self) -> DesktopEntryState:
        """Ensure the canonical entry exists with the current launch command."""
        if os.path.lexists(self.entry_path):
            current = self._read_entry()
            if not _is_managed(current):
                raise DesktopIntegrationError(
                    "A desktop entry not managed by MGL already exists at "
                    f"{self.entry_path}. Move it away before enabling the "
                    "launcher entry."
                )
        command = self._command()
        text = self._render(command)
        if self._read_entry() != text:
            self._write_entry(text)
        self._install_icon()
        self._refresh_caches()
        return self.state()

    def disable(self) -> DesktopEntryState:
        """Disable the canonical entry without touching unrelated files."""
        if not os.path.lexists(self.entry_path):
            if self._system_entry_exists():
                # A packaged entry would remain visible, so create an owned
                # user-level override that hides it.
                text = self._render(self._command()) + "Hidden=true\n"
                self._write_entry(text)
                self._refresh_caches()
            return self.state()

        current = self._read_entry()
        if not _is_managed(current):
            LOGGER.warning(
                "Leaving unowned desktop entry in place: %s", self.entry_path
            )
            return DesktopEntryState.UNOWNED

        if self._system_entry_exists():
            # A user-level file with the same ID shadows the packaged entry.
            # Keep it as an owned Hidden=true override rather than deleting it.
            if not _is_hidden(current):
                self._write_entry(current.rstrip("\n") + "\nHidden=true\n")
        else:
            try:
                self.entry_path.unlink()
            except OSError as error:
                raise DesktopIntegrationError(
                    f"The desktop entry could not be removed: {self.entry_path}"
                ) from error
        self._refresh_caches()
        return self.state()

    def remove_owned_entry(self) -> DesktopEntryState:
        """Remove the canonical entry only when MGL's marker is present."""
        if not os.path.lexists(self.entry_path):
            return self.state()
        if not _is_managed(self._read_entry()):
            return DesktopEntryState.UNOWNED
        try:
            self.entry_path.unlink()
        except OSError as error:
            raise DesktopIntegrationError(
                f"The desktop entry could not be removed: {self.entry_path}"
            ) from error
        self._refresh_caches()
        return self.state()

    # ------------------------------------------------------------------
    # Rendering and writing

    def _command(self) -> list[str]:
        if self._launch_command is None:
            self._launch_command = detect_launch_command()
        return self._launch_command

    def _render(self, command: list[str]) -> str:
        lines = [
            "[Desktop Entry]",
            "Type=Application",
            "Version=1.5",
            f"Name={APP_NAME}",
            f"Comment={COMMENT}",
            f"Exec={format_exec(command)}",
            f"Icon={APP_ID}",
            "Terminal=false",
            f"Categories={CATEGORIES}",
            f"Keywords={KEYWORDS}",
            "StartupNotify=true",
            f"StartupWMClass={APP_ID}",
            MANAGED_MARKER,
        ]
        return "\n".join(lines) + "\n"

    def _write_entry(self, text: str) -> None:
        self._write_atomic(self.entry_path, text)

    def _install_icon(self) -> None:
        """Copy the bundled icon once; never overwrite an existing icon."""
        if os.path.lexists(self.icon_path) or not self.icon_source.is_file():
            return
        try:
            self.icon_path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(
                prefix=f".{self.icon_path.name}.",
                dir=self.icon_path.parent,
            )
            try:
                with os.fdopen(descriptor, "wb") as destination:
                    with self.icon_source.open("rb") as source:
                        shutil.copyfileobj(source, destination)
                os.chmod(temporary, 0o644)
                os.replace(temporary, self.icon_path)
            except BaseException:
                with suppress(OSError):
                    os.unlink(temporary)
                raise
        except OSError:
            # The entry works without a user icon; packaged icons remain usable.
            LOGGER.warning(
                "Could not install the desktop icon at %s", self.icon_path,
                exc_info=True,
            )

    @staticmethod
    def _write_atomic(path: Path, text: str) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(
                prefix=f".{path.name}.", dir=path.parent
            )
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(text)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(temporary, 0o644)
                os.replace(temporary, path)
            except BaseException:
                with suppress(OSError):
                    os.unlink(temporary)
                raise
        except OSError as error:
            raise DesktopIntegrationError(
                f"The desktop entry could not be written: {path}"
            ) from error

    def _refresh_caches(self) -> None:
        """Best-effort desktop database refresh; failure changes nothing."""
        executable = shutil.which("update-desktop-database")
        if not executable:
            return
        try:
            subprocess.run(
                [executable, "-q", str(self.application_directory)],
                check=False,
                shell=False,
                timeout=_REFRESH_TIMEOUT_SECONDS,
                capture_output=True,
            )
        except (OSError, subprocess.SubprocessError):
            LOGGER.warning("Could not refresh the desktop database", exc_info=True)


def _entry_value(text: str, key: str) -> str | None:
    prefix = f"{key}="
    for line in text.splitlines():
        if line.strip().startswith(prefix):
            return line.strip()[len(prefix):]
    return None


def _is_managed(text: str) -> bool:
    return (_entry_value(text, MANAGED_KEY) or "").strip().lower() == "true"


def _is_hidden(text: str) -> bool:
    return (_entry_value(text, "Hidden") or "").strip().lower() == "true"
