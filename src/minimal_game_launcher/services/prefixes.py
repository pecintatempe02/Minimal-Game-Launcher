"""Resolve managed Wine prefix paths from the user's prefix strategy.

Assignments happen only for new games and installer sessions. Existing records
keep the path and shared flag they were created with, so changing the strategy
never moves, merges, copies, or deletes a prefix.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from minimal_game_launcher.errors import SharedPrefixUnavailableError
from minimal_game_launcher.paths import SHARED_PREFIX_DIRECTORY_NAME
from minimal_game_launcher.services.prefix_lock import shared_prefix_lifecycle_lock
from minimal_game_launcher.settings import (
    LauncherSettings,
    SettingsStore,
    WinePrefixStrategy,
)

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PrefixAssignment:
    """A managed prefix path and whether it is the one shared prefix."""

    path: str | None
    shared: bool = False


def shared_prefix_path(prefixes_root: str | Path) -> Path:
    """Return the stable managed shared-prefix path under ``prefixes_root``."""
    return Path(prefixes_root).expanduser() / SHARED_PREFIX_DIRECTORY_NAME


def load_strategy(settings_store: SettingsStore | None) -> WinePrefixStrategy:
    """Return the configured strategy, falling back to per-game on any error."""
    if settings_store is None:
        return WinePrefixStrategy.PER_GAME
    try:
        return settings_store.load().wine_prefix_strategy
    except Exception:
        LOGGER.exception("Could not load the Wine prefix strategy")
        return WinePrefixStrategy.PER_GAME


def assign_prefix(
    prefixes_root: str | Path,
    game_id: str,
    strategy: WinePrefixStrategy | LauncherSettings,
) -> PrefixAssignment:
    """Return the managed prefix a newly created Windows game should use."""
    if isinstance(strategy, LauncherSettings):
        strategy = strategy.wine_prefix_strategy
    if strategy is WinePrefixStrategy.SHARED:
        return PrefixAssignment(str(shared_prefix_path(prefixes_root)), True)
    return PrefixAssignment(
        str(Path(prefixes_root).expanduser() / game_id), False
    )


@contextmanager
def shared_reference_guard(
    prefixes_root: str | Path, shared: bool, *, new_reference: bool = True
) -> Iterator[None]:
    """Serialize a write that may persist a shared-prefix reference.

    Holds the lifecycle lock only for ``shared=True``; per-game writes pass
    through unchanged. Callers must keep the guarded block short: it must not
    call wineboot or any other long-running operation.

    If the lock was contended and ``.shared`` is now missing, cleanup deleted
    the prefix while this caller waited. A ``new_reference`` is refused before
    the caller persists anything. A re-save of a reference already persisted
    (``new_reference=False``) still holds the lock but is not refused, so
    installer progress is never blocked. A missing ``.shared`` without
    contention is a normal first assignment and is allowed; this guard never
    creates it.
    """
    if not shared:
        yield
        return
    root = Path(prefixes_root).expanduser()
    with shared_prefix_lifecycle_lock(root) as contended:
        if new_reference and contended and not shared_prefix_path(root).is_dir():
            raise SharedPrefixUnavailableError(
                "The shared Wine prefix was removed while this change was "
                "waiting for it, so the game was not linked to it. Nothing "
                "was saved. Try again; the shared prefix is created on the "
                "next launch."
            )
        yield


__all__ = [
    "PrefixAssignment",
    "assign_prefix",
    "load_strategy",
    "shared_prefix_path",
    "shared_reference_guard",
]
