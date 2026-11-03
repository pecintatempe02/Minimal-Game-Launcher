"""Persistent launcher preferences, currently Wine executable selection.

Settings are intentionally stored outside the SQLite library so that launch
configuration is independent from game records. The JSON file is read before
each Windows launch, which means changing Wine in the settings dialog takes
effect immediately without restarting the application.
"""

from __future__ import annotations

import json
import logging
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
    """Load and atomically save launcher settings as JSON."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()

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
