"""Tests for game-ID reservation and cover replacement through GameManager."""

from __future__ import annotations

from pathlib import Path

import gi

gi.require_version("GdkPixbuf", "2.0")

from gi.repository import GdkPixbuf  # noqa: E402

import pytest  # noqa: E402

from minimal_game_launcher.errors import GameNotFoundError
from minimal_game_launcher.models import GameDraft, Platform
from minimal_game_launcher.services.cover_manager import CoverManager
from minimal_game_launcher.services.game_manager import GameManager


def _draft(executable: Path) -> GameDraft:
    return GameDraft(
        name="Cover Game",
        platform=Platform.LINUX,
        executable=str(executable),
    )


def _valid_cover(tmp_path: Path, name: str = "cover.png") -> Path:
    """Write a decodable 32×32 PNG accepted by CoverManager."""
    pixbuf = GdkPixbuf.Pixbuf.new(
        GdkPixbuf.Colorspace.RGB, False, 8, 32, 32
    )
    pixbuf.fill(0xFF0000FF)
    success, buffer = pixbuf.save_to_bufferv("png", [], [])
    assert success
    path = tmp_path / name
    path.write_bytes(bytes(buffer))
    return path


def test_new_game_id_is_unique(manager: GameManager) -> None:
    ids = {manager.new_game_id() for _ in range(20)}

    assert len(ids) == 20


def test_add_game_accepts_a_reserved_id(
    manager: GameManager, make_executable
) -> None:
    executable = make_executable()
    game_id = manager.new_game_id()

    game = manager.add_game(_draft(executable), game_id=game_id)

    assert game.id == game_id
    assert manager.get_game(game_id) == game


def test_set_cover_persists_a_cached_path(
    manager: GameManager,
    make_executable,
    tmp_path: Path,
) -> None:
    game = manager.add_game(_draft(make_executable()))
    cover_manager = CoverManager(tmp_path / "covers")
    cached = cover_manager.import_local(game.id, _valid_cover(tmp_path))

    updated = manager.set_cover(game.id, cached)

    assert updated.cover_path == cached
    assert manager.get_game(game.id).cover_path == cached


def test_set_cover_replaces_and_clears(
    manager: GameManager,
    make_executable,
    tmp_path: Path,
) -> None:
    game = manager.add_game(_draft(make_executable()))
    cover_manager = CoverManager(tmp_path / "covers")
    first = cover_manager.import_local(
        game.id, _valid_cover(tmp_path, "first.png")
    )
    second = cover_manager.import_local(
        game.id, _valid_cover(tmp_path, "second.png")
    )

    manager.set_cover(game.id, first)
    replaced = manager.set_cover(game.id, second)

    assert replaced.cover_path == second
    assert cover_manager.remove_cover(first) is True
    assert Path(second).is_file()

    cleared = manager.set_cover(game.id, None)

    assert cleared.cover_path is None
    assert manager.get_game(game.id).cover_path is None


def test_set_cover_rejects_unknown_games(manager: GameManager) -> None:
    with pytest.raises(GameNotFoundError):
        manager.set_cover("missing", "/tmp/cover.png")
