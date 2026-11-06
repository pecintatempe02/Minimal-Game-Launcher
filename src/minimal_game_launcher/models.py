"""Domain models shared by the database, game manager, and GTK UI."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import StrEnum


def is_bare_command(executable: str) -> bool:
    """Return whether ``executable`` is a command name rather than a path."""
    return bool(executable) and os.sep not in executable and (
        os.altsep is None or os.altsep not in executable
    )


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
    # Raw multiline ``KEY=VALUE`` text. Parsing and validation belong to
    # ``GameManager``; the draft keeps exactly what the user typed.
    environment: str = ""
    cover_path: str | None = None
    # Performance wrappers are opt-out rather than opt-in for GameMode, matching
    # the Phase 4 default. MangoHud stays opt-in because its overlay changes the
    # rendered output and may be unsupported by some games or graphics APIs.
    gamemode_enabled: bool = True
    mangohud_enabled: bool = False
    # Fixed-resolution upscaling through gamescope, as ``WIDTHxHEIGHT``. Empty
    # means the game is launched without gamescope.
    gamescope_resolution: str = ""


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
    gamemode_enabled: bool
    mangohud_enabled: bool
    created_at: str
    updated_at: str
    environment: dict[str, str] = field(default_factory=dict)
    # Ownership metadata. Paths are stored as references only; the launcher
    # never creates, moves, or deletes them as part of editing a game. The
    # owned flags default to false so a record without explicit metadata is
    # never treated as MGL-owned.
    install_directory: str | None = None
    install_directory_owned: bool = False
    installer_source_directory: str | None = None
    installer_source_owned: bool = False
    #: Whether ``wine_prefix_path`` is the one managed shared prefix. The flag is
    #: explicit ownership metadata; a path is never treated as shared merely
    #: because of its directory name.
    wine_prefix_shared: bool = False
    #: ``WIDTHxHEIGHT`` the game renders at, upscaled to the display by
    #: gamescope. Empty means gamescope is not used for this game.
    gamescope_resolution: str = ""


class InstallerSourceKind(StrEnum):
    """Where an installation session's installer files came from."""

    ISO = "iso"
    EXECUTABLE = "executable"


class InstallerStatus(StrEnum):
    """Persisted state of an installer session."""

    PENDING = "pending"
    STAGED = "staged"
    INSTALLING = "installing"
    INSTALLER_FINISHED = "installer_finished"
    DEFERRED = "deferred"
    INSTALLED = "installed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class InstallerSession:
    """Resumable state for installing one game from an ISO or an executable.

    ``game_id`` is reserved before the game is registered, so staging, the Wine
    prefix, and the final library entry all use the same stable identifier.
    ``staging_directory`` is the MGL-managed installer-source directory, never
    the final installation directory.

    ``iso_path`` keeps its historical column name for compatibility. It stores
    the selected ISO image for :attr:`InstallerSourceKind.ISO` sessions and the
    selected standalone installer executable for
    :attr:`InstallerSourceKind.EXECUTABLE` sessions. New code should read
    :attr:`source_path`.
    """

    game_id: str
    game_name: str
    iso_path: str
    staging_directory: str
    installer_relative_path: str | None
    install_directory: str | None
    install_directory_owned: bool
    status: InstallerStatus
    message: str
    created_at: str
    updated_at: str
    #: Prefix assigned when the session was created. Kept on the session so
    #: resuming never follows a later prefix-strategy change.
    wine_prefix_path: str | None = None
    wine_prefix_shared: bool = False
    #: Source kind added in schema v7. Sessions created before it are ISO.
    source_kind: InstallerSourceKind = InstallerSourceKind.ISO

    @property
    def source_path(self) -> str:
        """Return the selected ISO image or standalone installer executable."""
        return self.iso_path

    @property
    def source_kind_is_iso(self) -> bool:
        """Return whether this session stages from an ISO image."""
        return self.source_kind is InstallerSourceKind.ISO
