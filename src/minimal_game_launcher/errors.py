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
