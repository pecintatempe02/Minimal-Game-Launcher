"""Tests for the shared-prefix inter-process lock."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from minimal_game_launcher.errors import PrefixInitializationError
from minimal_game_launcher.services.prefix_lock import (
    LOCK_FILE_NAME,
    shared_prefix_lock,
)

REPO_SRC = Path(__file__).resolve().parents[1] / "src"


def test_lock_file_lives_outside_shared_prefix(tmp_path: Path) -> None:
    root = tmp_path / "prefixes"
    with shared_prefix_lock(root):
        assert (root / LOCK_FILE_NAME).is_file()
        assert not (root / ".shared").exists()


def test_threads_are_serialized(tmp_path: Path) -> None:
    root = tmp_path / "prefixes"
    inside = 0
    peak = 0
    guard = threading.Lock()

    def worker() -> None:
        nonlocal inside, peak
        with shared_prefix_lock(root):
            with guard:
                inside += 1
                peak = max(peak, inside)
            time.sleep(0.02)
            with guard:
                inside -= 1

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert peak == 1


_HOLDER = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path
    from minimal_game_launcher.services.prefix_lock import shared_prefix_lock
    with shared_prefix_lock(Path(sys.argv[1])):
        print("held", flush=True)
        time.sleep(float(sys.argv[2]))
    """
)


def _env() -> dict[str, str]:
    return {**os.environ, "PYTHONPATH": str(REPO_SRC), "PYTHONDONTWRITEBYTECODE": "1"}


def test_second_process_waits_for_first(tmp_path: Path) -> None:
    root = tmp_path / "prefixes"
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(root), "1.0"],
        stdout=subprocess.PIPE,
        text=True,
        env=_env(),
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "held"
        start = time.monotonic()
        with shared_prefix_lock(root, timeout=10):
            waited = time.monotonic() - start
        # The holder keeps the lock for about one second; we must have waited.
        assert waited >= 0.5
    finally:
        holder.wait(timeout=10)


def test_lock_is_released_when_holder_crashes(tmp_path: Path) -> None:
    root = tmp_path / "prefixes"
    crash = textwrap.dedent(
        """
        import os, sys
        from pathlib import Path
        from minimal_game_launcher.services.prefix_lock import shared_prefix_lock
        cm = shared_prefix_lock(Path(sys.argv[1]))
        cm.__enter__()
        os._exit(3)  # hard exit: no context-manager cleanup runs
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", crash, str(root)],
        env=_env(),
        timeout=30,
        check=False,
    )
    assert result.returncode == 3
    with shared_prefix_lock(root, timeout=5):
        pass


def test_bounded_wait_raises_actionable_error(tmp_path: Path) -> None:
    root = tmp_path / "prefixes"
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(root), "5.0"],
        stdout=subprocess.PIPE,
        text=True,
        env=_env(),
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "held"
        with pytest.raises(PrefixInitializationError, match="Timed out") as info:
            with shared_prefix_lock(root, timeout=0.3):
                pass
        assert LOCK_FILE_NAME in str(info.value)
    finally:
        holder.wait(timeout=30)
