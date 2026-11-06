"""Resolve application-owned directories according to XDG conventions.

The library database, launcher settings, per-game Wine prefixes, diagnostic
log, cover cache, optional SteamGridDB key, and theme file are kept in separate
XDG locations. Wine prefixes are located under the data directory but are
created lazily by the launch manager on a Windows game's first launch. Covers
copied into the cache are app-owned; the original user-selected images never
are. The managed game-installation and installer-staging roots are created by
the installer workflow only when needed, never at startup.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from minimal_game_launcher import (
    APP_ID,
    DATA_DIRECTORY_NAME,
    LEGACY_DATA_DIRECTORY_NAME,
    THEME_DIRECTORY_NAME,
)

LOGGER = logging.getLogger(__name__)

#: Directory name used for the one managed shared prefix. It starts with a dot,
#: which no valid game ID may do, so it can never collide with a per-game path.
SHARED_PREFIX_DIRECTORY_NAME = ".shared"


def _xdg_home(environment_variable: str, fallback: Path) -> Path:
    """Return an XDG base directory, falling back to the documented default."""
    configured = os.environ.get(environment_variable)
    return Path(configured).expanduser() if configured else fallback


def _data_home() -> Path:
    """Return the XDG data home directory."""
    return _xdg_home("XDG_DATA_HOME", Path.home() / ".local" / "share")


def data_directory() -> Path:
    """Return the application data directory under ``$XDG_DATA_HOME``."""
    return _data_home() / DATA_DIRECTORY_NAME


def database_path() -> Path:
    """Return the SQLite library path inside the application data directory."""
    return data_directory() / "library.db"


def prefixes_directory() -> Path:
    """Return the root path for per-game Wine prefixes.

    The directory is deliberately not created here. A Windows game can be
    registered before its prefix has ever been initialized, and prefix creation
    belongs to the launch manager.
    """
    return data_directory() / "prefixes"


def shared_prefix_directory() -> Path:
    """Return the stable path of the managed shared Wine prefix."""
    return prefixes_directory() / SHARED_PREFIX_DIRECTORY_NAME


def config_directory() -> Path:
    """Return the application settings directory under ``$XDG_CONFIG_HOME``."""
    return (
        _xdg_home("XDG_CONFIG_HOME", Path.home() / ".config")
        / DATA_DIRECTORY_NAME
    )


def settings_path() -> Path:
    """Return the JSON settings file used for Wine selection."""
    return config_directory() / "settings.json"


def steamgriddb_key_path() -> Path:
    """Return the user-private file containing the optional SteamGridDB key.

    The key is deliberately kept out of the JSON settings file so its
    permissions can be restricted to the current user (mode ``0600``).
    """
    return config_directory() / "steamgriddb.key"


def themes_path() -> Path:
    """Return the standalone theme configuration file.

    Themes live outside the launcher settings directory so the file can be
    edited, versioned, or generated independently of application state.
    """
    return (
        _xdg_home("XDG_CONFIG_HOME", Path.home() / ".config")
        / THEME_DIRECTORY_NAME
        / "themes.ini"
    )


def legacy_theme_path() -> Path:
    """Return the single-theme file written by earlier builds.

    It is read for compatibility and never rewritten; new choices are saved to
    :func:`themes_path` so the user's existing file stays byte-identical.
    """
    return themes_path().with_name("theme.ini")


def covers_directory() -> Path:
    """Return the application-owned cache for copied and downloaded covers.

    The directory is created lazily by ``CoverManager``; the launcher never
    writes into the user's original image directory.
    """
    return data_directory() / "covers"


def games_directory() -> Path:
    """Return the root for MGL-managed game installations.

    A game's managed installation is ``<games>/<game-id>``. The directory is
    created only when the user explicitly chooses the managed destination; the
    installer's default Wine destination needs no game directory.
    """
    return data_directory() / "games"


def installers_directory() -> Path:
    """Return the root for MGL-managed installer staging data.

    A game's staging area is ``<installers>/<game-id>`` and is created when an
    installation reaches staging.
    """
    return data_directory() / "installers"


def applications_directory() -> Path:
    """Return the user-level XDG application directory for desktop entries."""
    return _data_home() / "applications"


def desktop_entry_path() -> Path:
    """Return the canonical user-level desktop entry path for this application."""
    return applications_directory() / f"{APP_ID}.desktop"


def icons_directory() -> Path:
    """Return the user-level scalable hicolor icon directory."""
    return _data_home() / "icons" / "hicolor" / "scalable" / "apps"


def desktop_icon_path() -> Path:
    """Return the canonical user-level icon path for this application."""
    return icons_directory() / f"{APP_ID}.svg"


def system_application_directories() -> tuple[Path, ...]:
    """Return XDG system application directories in precedence order."""
    configured = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    return tuple(
        Path(entry).expanduser() / "applications"
        for entry in configured.split(":")
        if entry.strip()
    )


def state_directory() -> Path:
    """Return the application state directory under ``$XDG_STATE_HOME``."""
    return (
        _xdg_home("XDG_STATE_HOME", Path.home() / ".local" / "state")
        / DATA_DIRECTORY_NAME
    )


def log_path() -> Path:
    """Return the local diagnostic log file path."""
    return state_directory() / "launcher.log"


def ensure_data_directory() -> Path:
    """Create the application data directory and migrate a Phase 2 library.

    Earlier builds used ``minimal-game-launcher`` as the directory name. The
    current directory name follows the Phase 3 prefix location requirement.
    Migration is a one-time copy; the original files are left untouched.
    """
    directory = data_directory()
    directory.mkdir(parents=True, exist_ok=True)
    _migrate_legacy_library(directory)
    return directory


def _migrate_legacy_library(destination: Path) -> None:
    """Copy an old Phase 2 database when no current database exists."""
    target = destination / "library.db"
    if target.exists():
        return

    legacy = _data_home() / LEGACY_DATA_DIRECTORY_NAME / "library.db"
    if not legacy.is_file():
        return

    try:
        # WAL and shared-memory files may contain the most recent transactions,
        # so copy them alongside the main database when present.
        for suffix in ("", "-wal", "-shm"):
            source = Path(f"{legacy}{suffix}")
            if source.is_file():
                shutil.copy2(source, Path(f"{target}{suffix}"))
    except OSError:
        LOGGER.exception("Could not migrate the Phase 2 library from %s", legacy)
        return

    LOGGER.info("Migrated Phase 2 library from %s to %s", legacy, target)
