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
