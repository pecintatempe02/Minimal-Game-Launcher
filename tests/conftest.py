"""Shared pytest fixtures for game-library tests."""

from __future__ import annotations

import base64
import stat
import sys
from pathlib import Path

import pytest

# The project intentionally avoids a pip build backend in this phase.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from minimal_game_launcher.database import Database  # noqa: E402
from minimal_game_launcher.services.game_manager import GameManager  # noqa: E402

# 1x1 transparent PNG used as a cover reference in tests.
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl1sAAAAASUVORK5CYII="
)


@pytest.fixture
def database(tmp_path: Path) -> Database:
    """Create an isolated SQLite library and close it after the test."""
    instance = Database(tmp_path / "library.db")
    yield instance
    instance.close()


@pytest.fixture
def manager(database: Database, tmp_path: Path) -> GameManager:
    """Create a manager whose future prefix root is a temporary directory."""
    return GameManager(database, tmp_path / "prefixes")


@pytest.fixture
def make_executable(tmp_path: Path):
    """Return a helper that creates a Linux-style executable fixture."""

    def factory(name: str = "linux-game", executable: bool = True) -> Path:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755 if executable else 0o644)
        return path

    return factory


@pytest.fixture
def make_cover(tmp_path: Path):
    """Return a helper that creates an external cover file."""

    def factory(name: str = "cover.png") -> Path:
        path = tmp_path / name
        path.write_bytes(PNG_BYTES)
        return path

    return factory
