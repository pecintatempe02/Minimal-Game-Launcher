"""Tests for cover validation, caching, replacement, and deletion boundaries."""

from __future__ import annotations

import os
from pathlib import Path

import gi

gi.require_version("GdkPixbuf", "2.0")

from gi.repository import GdkPixbuf  # noqa: E402

import pytest  # noqa: E402

from minimal_game_launcher.errors import (  # noqa: E402
    CoverValidationError,
)
from minimal_game_launcher.services.cover_manager import (  # noqa: E402
    MAX_COVER_BYTES,
    CoverManager,
)


def _png_bytes(width: int = 32, height: int = 32) -> bytes:
    """Create a real PNG so validation uses the installed GdkPixbuf loader."""
    pixbuf = GdkPixbuf.Pixbuf.new(
        GdkPixbuf.Colorspace.RGB, False, 8, width, height
    )
    pixbuf.fill(0xFF0000FF)
    success, buffer = pixbuf.save_to_bufferv("png", [], [])
    assert success
    return bytes(buffer)


@pytest.fixture
def cover_bytes() -> bytes:
    return _png_bytes()


@pytest.fixture
def cover_manager(tmp_path: Path) -> CoverManager:
    return CoverManager(tmp_path / "covers")


def _write_source(tmp_path: Path, data: bytes, name: str = "cover.png") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def test_import_local_copies_into_cache_without_touching_source(
    cover_manager: CoverManager, tmp_path: Path, cover_bytes: bytes
) -> None:
    source = _write_source(tmp_path, cover_bytes)
    original = source.read_bytes()

    cached = cover_manager.import_local("game-123", source)

    cached_path = Path(cached)
    assert cached_path.is_file()
    assert cached_path.parent == cover_manager.covers_root
    assert cached_path.name.startswith("game-123-")
    assert cached_path.suffix == ".png"
    assert cached_path.read_bytes() == cover_bytes
    assert source.is_file()
    assert source.read_bytes() == original
    assert cached != str(source)


def test_missing_local_cover_is_rejected(cover_manager: CoverManager) -> None:
    with pytest.raises(CoverValidationError, match="not found"):
        cover_manager.import_local("game", "/does/not/exist.png")


def test_invalid_image_bytes_are_rejected(
    cover_manager: CoverManager, tmp_path: Path
) -> None:
    source = _write_source(tmp_path, b"not an image at all")

    with pytest.raises(CoverValidationError, match="could not be decoded"):
        cover_manager.import_local("game", source)


def test_too_small_image_is_rejected(
    cover_manager: CoverManager, tmp_path: Path
) -> None:
    source = _write_source(tmp_path, _png_bytes(1, 1))

    with pytest.raises(CoverValidationError, match="too small"):
        cover_manager.import_local("game", source)


def test_oversized_local_file_is_rejected_before_reading(
    cover_manager: CoverManager, tmp_path: Path
) -> None:
    source = _write_source(
        tmp_path, b"\0" * (MAX_COVER_BYTES + 1), "huge.png"
    )

    with pytest.raises(CoverValidationError, match="larger than"):
        cover_manager.inspect_file(source)


def test_empty_bytes_are_rejected(cover_manager: CoverManager) -> None:
    with pytest.raises(CoverValidationError, match="empty"):
        cover_manager.inspect_bytes(b"")


def test_download_content_type_mismatch_is_rejected(
    cover_manager: CoverManager, cover_bytes: bytes
) -> None:
    with pytest.raises(CoverValidationError, match="does not match"):
        cover_manager.store_download("game", cover_bytes, "image/jpeg")


def test_unsupported_download_content_type_is_rejected(
    cover_manager: CoverManager, cover_bytes: bytes
) -> None:
    with pytest.raises(CoverValidationError, match="not a supported"):
        cover_manager.store_download("game", cover_bytes, "image/gif")


def test_download_is_cached_and_source_bytes_are_not_needed(
    cover_manager: CoverManager, cover_bytes: bytes
) -> None:
    cached = cover_manager.store_download("game", cover_bytes, "image/png")

    assert Path(cached).read_bytes() == cover_bytes
    assert cover_manager.is_cached_cover(cached)


def test_replacement_uses_unique_filenames(
    cover_manager: CoverManager, tmp_path: Path, cover_bytes: bytes
) -> None:
    source = _write_source(tmp_path, cover_bytes)

    first = cover_manager.import_local("same-game", source)
    second = cover_manager.import_local("same-game", source)

    assert first != second
    assert Path(first).is_file()
    assert Path(second).is_file()

    # Replacing a cover deletes only the explicitly removed cached file.
    assert cover_manager.remove_cover(first) is True
    assert not Path(first).exists()
    assert Path(second).is_file()


def test_game_id_cannot_escape_the_cover_cache(
    cover_manager: CoverManager, tmp_path: Path, cover_bytes: bytes
) -> None:
    source = _write_source(tmp_path, cover_bytes)

    cached = cover_manager.import_local("../../outside", source)

    cached_path = Path(cached)
    assert cached_path.resolve().is_relative_to(
        cover_manager.covers_root.resolve()
    )
    assert cached_path.parent == cover_manager.covers_root
    assert "/" not in cached_path.name


def test_remove_cover_never_deletes_external_files(
    cover_manager: CoverManager, tmp_path: Path, cover_bytes: bytes
) -> None:
    external = _write_source(tmp_path, cover_bytes)

    assert cover_manager.remove_cover(external) is False
    assert external.is_file()


def test_remove_cover_ignores_paths_outside_the_cache(
    cover_manager: CoverManager, tmp_path: Path
) -> None:
    cover_manager.covers_root.mkdir(parents=True)
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"keep me")

    assert cover_manager.remove_cover(outside) is False
    assert outside.is_file()


def test_remove_cover_accepts_missing_files(
    cover_manager: CoverManager,
) -> None:
    assert cover_manager.remove_cover(None) is False
    missing = cover_manager.covers_root / "missing.png"
    assert cover_manager.remove_cover(missing) is False


def test_cache_files_are_private(
    cover_manager: CoverManager, cover_bytes: bytes
) -> None:
    cached = cover_manager.store_download("game", cover_bytes, "image/png")

    mode = os.stat(cached).st_mode & 0o777
    assert mode == 0o600
