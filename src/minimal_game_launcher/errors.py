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


class OwnershipError(LauncherError):
    """Raised when a resource's ownership or a deletion request is refused."""


class SharedPrefixUnavailableError(LauncherError):
    """Raised when the shared prefix was removed while an assignment waited."""


class InstallerError(LauncherError):
    """Base class for expected ISO-installer failures."""


class IsoMountError(InstallerError):
    """Raised when an ISO cannot be mounted or safely unmounted."""


class InstallerStagingError(InstallerError):
    """Raised when ISO contents cannot be staged or verified."""


class DesktopIntegrationError(LauncherError):
    """Raised when a user-level desktop entry cannot be written or removed."""


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
