"""Inter-process locks for the single shared Wine prefix.

Two locks, each a stable lock file outside the ``.shared`` directory plus a
process-local ``threading.Lock`` (``flock`` alone does not serialize threads
that share one process):

- ``shared_prefix_lock`` (initialization): held by a launch while ``wineboot``
  initializes the shared prefix. Can be held for minutes.
- ``shared_prefix_lifecycle_lock`` (references): held briefly while a shared
  reference is persisted to the database, and while shared-prefix deletion
  checks references and deletes. Never held across ``wineboot``.

Lock order, always outer to inner: lifecycle, then initialization. Only
deletion takes both, and it takes initialization without waiting. Launch takes
only initialization; assignment takes only lifecycle. No code path can hold
initialization and then wait for lifecycle, so the two cannot deadlock.

``flock`` is released by the kernel when the holder exits, so a crash never
leaves a stale lock.
"""

from __future__ import annotations

import fcntl
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from minimal_game_launcher.errors import PrefixInitializationError

#: Initialization wait. Long enough for one wineboot run.
DEFAULT_LOCK_TIMEOUT = 300.0
#: Reference/deletion wait. Short: assignment must not stall the GTK thread for
#: long; a prefix deletion in progress is rare and quick to finish.
LIFECYCLE_LOCK_TIMEOUT = 10.0
LOCK_FILE_NAME = ".shared-prefix.lock"
LIFECYCLE_LOCK_FILE_NAME = ".shared-prefix.lifecycle.lock"

_INIT_THREAD_LOCK = threading.Lock()
_LIFECYCLE_THREAD_LOCK = threading.Lock()


@contextmanager
def shared_prefix_lock(
    prefixes_root: Path | str, timeout: float = DEFAULT_LOCK_TIMEOUT
) -> Iterator[None]:
    """Hold the shared-prefix initialization lock."""
    with _hold(
        Path(prefixes_root) / LOCK_FILE_NAME,
        _INIT_THREAD_LOCK,
        timeout,
        "initializing the shared Wine prefix",
    ):
        yield


@contextmanager
def shared_prefix_lifecycle_lock(
    prefixes_root: Path | str, timeout: float | None = None
) -> Iterator[bool]:
    """Hold the shared-prefix reference/deletion lock.

    Yields ``True`` when the lock was busy on arrival and this caller had to
    wait for the holder, ``False`` when it was free. Callers use this to decide
    whether the state they observed before waiting may have changed.
    """
    with _hold(
        Path(prefixes_root) / LIFECYCLE_LOCK_FILE_NAME,
        _LIFECYCLE_THREAD_LOCK,
        LIFECYCLE_LOCK_TIMEOUT if timeout is None else timeout,
        "updating or deleting the shared Wine prefix",
    ) as contended:
        yield contended


@contextmanager
def _hold(
    lock_path: Path,
    thread_lock: threading.Lock,
    timeout: float,
    purpose: str,
) -> Iterator[bool]:
    deadline = time.monotonic() + timeout
    contended = not thread_lock.acquire(blocking=False)
    if contended and not thread_lock.acquire(timeout=timeout):
        raise _busy_error(lock_path, timeout, purpose)
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    # Another process holds the file lock: this caller waited.
                    contended = True
                    if time.monotonic() >= deadline:
                        raise _busy_error(lock_path, timeout, purpose) from None
                    time.sleep(0.05)
            try:
                yield contended
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
    finally:
        thread_lock.release()


def _busy_error(
    lock_path: Path, timeout: float, purpose: str
) -> PrefixInitializationError:
    return PrefixInitializationError(
        f"Timed out after {timeout:.1f} seconds while {purpose}; another Minimal "
        f"Game Launcher operation holds the lock. Close other launcher windows "
        f"and try again. Lock file: {lock_path}"
    )
