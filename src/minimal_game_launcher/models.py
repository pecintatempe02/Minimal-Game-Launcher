"""Domain models shared by the database, game manager, and GTK UI."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Platform(StrEnum):
    """Supported launch platforms."""

    LINUX = "linux"
    WINDOWS = "windows"

    @property
    def display_name(self) -> str:
        """Return the label shown in the library and details view."""
        return "Linux Native" if self is Platform.LINUX else "Windows / Wine"


@dataclass(frozen=True, slots=True)
class GameDraft:
    """User-provided values before validation and persistence."""

    name: str
    platform: Platform
    executable: str
    working_directory: str = ""
    launch_arguments: str = ""
    cover_path: str | None = None


@dataclass(frozen=True, slots=True)
class Game:
    """A validated game library entry."""

    id: str
    name: str
    platform: Platform
    executable: str
    working_directory: str
    launch_arguments: str
    cover_path: str | None
    wine_prefix_path: str | None
    created_at: str
    updated_at: str
