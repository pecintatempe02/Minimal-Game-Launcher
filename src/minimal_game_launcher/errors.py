"""Typed application errors with messages suitable for the GTK UI."""

from __future__ import annotations


class LauncherError(Exception):
    """Base class for expected application errors."""


class GameValidationError(LauncherError):
    """Raised when submitted game metadata cannot be accepted."""

    def __init__(self, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field


class GameNotFoundError(LauncherError):
    """Raised when a requested library entry no longer exists."""


class LaunchError(LauncherError):
    """Base class for errors that prevent a game process from starting."""


class WineNotFoundError(LaunchError):
    """Raised when system or configured custom Wine cannot be used."""


class PrefixInitializationError(LaunchError):
    """Raised when ``wineboot`` cannot initialize a game's Wine prefix."""


class ProcessLaunchError(LaunchError):
    """Raised when the operating system refuses to create a process."""


class CoverError(LauncherError):
    """Base class for expected cover-management errors."""


class CoverValidationError(CoverError):
    """Raised when an image is not an acceptable cover."""


class CoverCacheError(CoverError):
    """Raised when a validated cover cannot be written to the cache."""


class MetadataProviderError(LauncherError):
    """Base class for optional online metadata failures."""


class SteamGridDBError(MetadataProviderError):
    """Raised for an invalid or failed SteamGridDB response."""


class SteamGridDBAuthError(SteamGridDBError):
    """Raised when SteamGridDB rejects the configured API key."""


class SteamGridDBNotConfiguredError(SteamGridDBError):
    """Raised when online cover search is used without an API key."""


class SteamGridDBOfflineError(SteamGridDBError):
    """Raised when SteamGridDB cannot be reached."""
