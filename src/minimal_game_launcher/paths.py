"""Resolve application-owned directories according to XDG conventions.

The database is stored in the XDG data directory. Wine prefixes are *located*
under the data directory but are never created or deleted during library
management; prefix lifecycle belongs to a later launch phase. Cover images are
not managed by the application in this phase: they remain at the path selected
by the user and are never deleted when a library entry is removed.
"""

from __future__ import annotations

import os
from pathlib import Path

from minimal_game_launcher import DATA_DIRECTORY_NAME


def _xdg_home(environment_variable: str, fallback: Path) -> Path:
    """Return an XDG base directory, falling back to the documented default."""
    configured = os.environ.get(environment_variable)
    return Path(configured).expanduser() if configured else fallback


def data_directory() -> Path:
    """Return the application data directory under ``$XDG_DATA_HOME``."""
    return _xdg_home("XDG_DATA_HOME", Path.home() / ".local" / "share") / DATA_DIRECTORY_NAME


def database_path() -> Path:
    """Return the SQLite library path inside the application data directory."""
    return data_directory() / "library.db"


def prefixes_directory() -> Path:
    """Return the root path for future per-game Wine prefixes.

    The directory is deliberately not created here. A Windows game can be
    registered before Wine has ever initialized its prefix.
    """
    return data_directory() / "prefixes"


def ensure_data_directory() -> Path:
    """Create the application data directory and return it."""
    directory = data_directory()
    directory.mkdir(parents=True, exist_ok=True)
    return directory
