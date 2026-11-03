"""Persistent launcher preferences and optional API credentials.

Wine selection is stored as JSON outside the SQLite library so that launch
configuration is independent from game records. The optional SteamGridDB API
key is kept in a separate user-private file (mode ``0600``) and may also be
supplied through ``STEAMGRIDDB_API_KEY``. Keys are never hardcoded.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

LOGGER = logging.getLogger(__name__)


class WineSource(StrEnum):
    """How the Wine executable is selected for Windows games."""

    SYSTEM = "system"
    CUSTOM = "custom"


@dataclass(frozen=True, slots=True)
class LauncherSettings:
    """User-selected launch preferences."""

    wine_source: WineSource = WineSource.SYSTEM
    custom_wine_path: str | None = None


class SettingsStore:
    """Load and atomically save launcher settings and the optional API key."""

    def __init__(
        self,
        path: str | Path,
        *,
        api_key_path: str | Path | None = None,
    ) -> None:
        self.path = Path(path).expanduser()
        self.api_key_path = (
            Path(api_key_path).expanduser()
            if api_key_path is not None
            else self.path.parent / "steamgriddb.key"
        )

    def load(self) -> LauncherSettings:
        """Return stored settings, falling back to safe defaults on errors."""
        if not self.path.is_file():
            return LauncherSettings()

        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            LOGGER.exception("Could not read launcher settings from %s", self.path)
            return LauncherSettings()

        if not isinstance(payload, dict):
            LOGGER.warning("Ignoring launcher settings with an invalid structure")
            return LauncherSettings()

        raw_source = payload.get("wine_source", WineSource.SYSTEM.value)
        try:
            wine_source = WineSource(raw_source)
        except (TypeError, ValueError):
            LOGGER.warning("Ignoring unknown Wine source: %r", raw_source)
            wine_source = WineSource.SYSTEM

        raw_custom = payload.get("custom_wine_path")
        custom_wine_path = (
            str(Path(raw_custom).expanduser())
            if isinstance(raw_custom, str) and raw_custom.strip()
            else None
        )
        return LauncherSettings(
            wine_source=wine_source,
            custom_wine_path=custom_wine_path,
        )

    def save(self, settings: LauncherSettings) -> None:
        """Write settings atomically so a crash cannot leave partial JSON."""
        payload = {
            "wine_source": settings.wine_source.value,
            "custom_wine_path": settings.custom_wine_path,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f"{self.path.name}.tmp")
        temporary.write_text(
            json.dumps(payload, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def load_stored_api_key(self) -> str | None:
        """Return only the key saved in the private file, ignoring the environment.

        The settings dialog uses this for its pre-filled value so that saving
        unrelated settings never copies an inherited environment key to disk.
        """
        if not self.api_key_path.is_file():
            return None
        try:
            key = self.api_key_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            LOGGER.exception(
                "Could not read the SteamGridDB API key from %s",
                self.api_key_path,
            )
            return None
        return key or None

    def load_api_key(self) -> str | None:
        """Return the optional SteamGridDB key from its private file or env.

        The private file takes precedence so a user can override an inherited
        environment variable. Missing or unreadable credentials are treated as
        "online search not configured"; they never prevent the app from
        starting.
        """
        stored = self.load_stored_api_key()
        if stored:
            return stored

        environment_key = os.environ.get("STEAMGRIDDB_API_KEY", "").strip()
        return environment_key or None

    def save_api_key(self, api_key: str | None) -> None:
        """Store or remove the key in a mode-``0600`` file.

        Empty input removes the file. The temporary file is created with
        restricted permissions from the start so the key is never briefly
        world-readable.
        """
        key = (api_key or "").strip()
        if not key:
            self.api_key_path.unlink(missing_ok=True)
            return

        self.api_key_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.api_key_path.with_name(
            f"{self.api_key_path.name}.tmp"
        )
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(f"{key}\n")
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.api_key_path)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise
