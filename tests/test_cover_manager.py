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


# ---------------------------------------------------------------------------
# Dimension and pixel limits (header checked before any pixel decode)


def _png_header(width: int, height: int) -> bytes:
    """Return a PNG signature and IHDR only, with no pixel data."""
    import struct
    import zlib

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    header = b"IHDR" + ihdr
    # An empty IDAT chunk is enough for the PNG header parser to accept the
    # file; no pixel data is present.
    idat = b"IDAT"
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", len(ihdr))
        + header
        + struct.pack(">I", zlib.crc32(header) & 0xFFFFFFFF)
        + struct.pack(">I", 0)
        + idat
        + struct.pack(">I", zlib.crc32(idat) & 0xFFFFFFFF)
    )


def test_probe_reads_dimensions_from_the_header(cover_manager: CoverManager) -> None:
    assert cover_manager._probe(_png_bytes(64, 96)) == ("png", 64, 96)


def test_oversized_pixel_count_is_rejected_before_decode(
    cover_manager: CoverManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_decode(*_args, **_kwargs):
        raise AssertionError("oversized image must not be decoded")

    monkeypatch.setattr(cover_manager, "_decode_bounded", fail_decode)

    with pytest.raises(CoverValidationError, match="too many pixels"):
        cover_manager.inspect_bytes(_png_header(7000, 7000))


def test_dimension_above_maximum_is_rejected(
    cover_manager: CoverManager,
) -> None:
    from minimal_game_launcher.services.cover_manager import MAX_COVER_DIMENSION

    with pytest.raises(CoverValidationError, match="too large"):
        cover_manager.inspect_bytes(
            _png_header(MAX_COVER_DIMENSION + 1, 32)
        )


def test_wide_image_is_rejected_by_the_pixel_budget(
    cover_manager: CoverManager,
) -> None:
    # 9000x2000 is under the per-side maximum but 18 MP in total.
    with pytest.raises(CoverValidationError, match="too many pixels"):
        cover_manager.inspect_bytes(_png_header(9000, 2000))


def test_tiny_header_only_file_with_huge_dimensions_is_rejected(
    cover_manager: CoverManager,
) -> None:
    data = _png_header(20000, 20000)
    assert len(data) < 1024

    with pytest.raises(CoverValidationError, match="too large"):
        cover_manager.inspect_bytes(data)


def test_dimension_limits_accept_the_boundary(
    cover_manager: CoverManager,
) -> None:
    from minimal_game_launcher.services.cover_manager import (
        MAX_COVER_DIMENSION,
        MAX_COVER_PIXELS,
    )

    # Exactly 12 MP is accepted; one extra pixel row is not.
    cover_manager._validate_dimensions(4000, 3000)
    assert 4000 * 3000 == MAX_COVER_PIXELS
    with pytest.raises(CoverValidationError, match="too many pixels"):
        cover_manager._validate_dimensions(4000, 3001)

    cover_manager._validate_dimensions(MAX_COVER_DIMENSION, 16)
    with pytest.raises(CoverValidationError, match="too large"):
        cover_manager._validate_dimensions(MAX_COVER_DIMENSION + 1, 16)


def test_truncated_image_is_rejected(
    cover_manager: CoverManager,
) -> None:
    data = _png_bytes(64, 64)
    with pytest.raises(CoverValidationError, match="could not be decoded"):
        cover_manager.inspect_bytes(data[: len(data) // 2])


def test_preview_decode_preserves_aspect_ratio(
    cover_manager: CoverManager,
) -> None:
    from minimal_game_launcher.services.cover_manager import (
        PREVIEW_MAX_DIMENSION,
    )

    pixbuf = cover_manager._decode_bounded(_png_bytes(600, 900), 600, 900)
    assert pixbuf is not None
    assert max(pixbuf.get_width(), pixbuf.get_height()) == PREVIEW_MAX_DIMENSION
    assert abs(pixbuf.get_width() / pixbuf.get_height() - 600 / 900) < 0.01


def test_wide_and_tall_images_within_limits_are_accepted(
    cover_manager: CoverManager,
) -> None:
    wide = cover_manager.inspect_bytes(_png_bytes(2000, 100))
    tall = cover_manager.inspect_bytes(_png_bytes(100, 2000))

    assert (wide.width, wide.height) == (2000, 100)
    assert (tall.width, tall.height) == (100, 2000)
