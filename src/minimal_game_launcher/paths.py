"""Resolve application-owned directories according to XDG conventions.

The library database, launcher settings, per-game Wine prefixes, diagnostic
log, cover cache, optional SteamGridDB key, and theme file are kept in separate
XDG locations. Wine prefixes are located under the data directory but are
created lazily by the launch manager on a Windows game's first launch. Covers
copied into the cache are app-owned; the original user-selected images never
are.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from minimal_game_launcher import (
    DATA_DIRECTORY_NAME,
    LEGACY_DATA_DIRECTORY_NAME,
    THEME_DIRECTORY_NAME,
)

LOGGER = logging.getLogger(__name__)


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
