"""Tests for the bounded LRU cover texture cache."""

from __future__ import annotations

import gc
import os
import time
import weakref
from pathlib import Path

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Gtk", "4.0")

import pytest  # noqa: E402
from gi.repository import Gdk, GdkPixbuf  # noqa: E402

from minimal_game_launcher.ui.cover import (  # noqa: E402
    DEFAULT_CACHE,
    DEFAULT_CACHE_LIMIT,
    CoverTextureCache,
    load_cover_texture,
    load_cover_texture_from_bytes,
)


@pytest.fixture(autouse=True)
def _reset_default_cache():
    """Keep module-level cache state from leaking between tests."""
    DEFAULT_CACHE.clear()
    yield
    DEFAULT_CACHE.clear()


def _make_png(path: Path, width: int, height: int, color: int) -> Path:
    """Write a real PNG of the requested size and fill colour."""
    pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, width, height)
    pixbuf.fill(color)
    pixbuf.savev(str(path), "png", [], [])
    return path


@pytest.fixture
def png_factory(tmp_path: Path):
    """Return a helper writing uniquely-coloured PNG files into tmp_path."""
    counter = {"n": 0}

    def factory(
        name: str = "cover.png", width: int = 120, height: int = 180
    ) -> Path:
        counter["n"] += 1
        # Distinct colours so each file decodes to distinct pixels.
        color = 0xFF000000 | ((counter["n"] * 37) % 256) << 16 | (counter["n"] * 53 % 256) << 8
        return _make_png(tmp_path / name, width, height, color)

    return factory


# ---------------------------------------------------------------------------
# Reuse


def test_repeated_requests_reuse_cached_texture(png_factory) -> None:
    cache = CoverTextureCache()
    cover = png_factory()
    first = cache.load_texture(cover, 60, 90)
    second = cache.load_texture(cover, 60, 90)
    assert first is second
    stats = cache.stats()
    assert stats.loads == 2
    assert stats.hits == 1
    assert stats.misses == 1
    assert stats.loads == stats.hits + stats.misses


def test_repeated_requests_via_helper_reuse_cached_texture(png_factory) -> None:
    cover = png_factory()
    first = load_cover_texture(str(cover), 60, 90)
    second = load_cover_texture(str(cover), 60, 90)
    assert first is second


def test_library_refresh_rebuild_reuses_cached_textures(png_factory) -> None:
    """Rebuilding several cards at the same sizes must not re-decode."""
    cache = CoverTextureCache()
    covers = [png_factory(f"cover-{index}.png") for index in range(4)]

    # First library build: every card decodes once.
    first_build = [cache.load_texture(cover, 368, 492) for cover in covers]
    assert cache.stats().misses == 4

    # Second build (search debounce / refresh): no decoding at all.
    second_build = [cache.load_texture(cover, 368, 492) for cover in covers]
    stats = cache.stats()
    assert stats.loads == 8
    assert stats.hits == 4
    assert stats.misses == 4
    assert second_build == first_build


def test_hits_move_entries_to_most_recent_position(png_factory) -> None:
    cache = CoverTextureCache(limit=2)
    a = png_factory("a.png")
    b = png_factory("b.png")
    c = png_factory("c.png")

    cache.load_texture(a, 30, 30)
    cache.load_texture(b, 30, 30)
    cache.load_texture(a, 30, 30)  # Refresh A: B becomes least-recently used.
    cache.load_texture(c, 30, 30)  # Must evict B, not A.

    stats = cache.stats()
    assert stats.evictions == 1
    assert cache.size() == 2

    # A survived the eviction; B was evicted and decodes again.
    texture_a = cache.load_texture(a, 30, 30)
    assert cache.stats().hits >= 2
    assert texture_a is not None

    texture_b = cache.load_texture(b, 30, 30)
    assert texture_b is not None
    assert cache.stats().misses == 4  # B was re-decoded after eviction.


# ---------------------------------------------------------------------------
# Dimension independence


def test_different_dimensions_produce_independent_entries(png_factory) -> None:
    cache = CoverTextureCache()
    cover = png_factory()
    card = cache.load_texture(cover, 368, 492)
    preview = cache.load_texture(cover, 160, 208)
    thumbnail = cache.load_texture(cover, 80, 104)
    assert card is not preview
    assert card is not thumbnail
    assert preview is not thumbnail
    assert cache.size() == 3


def test_same_texture_returned_per_dimension_after_interleaving(png_factory) -> None:
    cache = CoverTextureCache()
    cover = png_factory()
    first_card = cache.load_texture(cover, 368, 492)
    cache.load_texture(cover, 160, 208)
    cache.load_texture(cover, 80, 104)
    second_card = cache.load_texture(cover, 368, 492)
    assert first_card is second_card


# ---------------------------------------------------------------------------
# Invalidation on modification / replacement


def test_modified_file_invalidates_cached_entry(png_factory, tmp_path: Path) -> None:
    cache = CoverTextureCache()
    cover = png_factory()
    original = cache.load_texture(cover, 60, 90)

    # Rewrite with a different colour and a guaranteed-new mtime.
    _make_png(cover, 120, 180, 0xFF204080)
    info = os.stat(cover)
    future = time.time() + 5
    os.utime(cover, ns=(int(future * 1e9), int(future * 1e9)))
    assert os.stat(cover).st_mtime_ns > info.st_mtime_ns

    replacement = cache.load_texture(cover, 60, 90)
    assert replacement is not original
    stats = cache.stats()
    assert stats.misses == 2
    assert stats.loads == 2


def test_replaced_file_same_size_still_invalidates(png_factory) -> None:
    """Size can stay identical; the nanosecond mtime exposes the change."""
    cache = CoverTextureCache()
    cover = png_factory(width=120, height=180)
    original = cache.load_texture(cover, 60, 90)
    assert cache.size() == 1

    _make_png(cover, 120, 180, 0xFF00FF00)  # Same codec, same size, new pixels.
    future = time.time() + 5
    os.utime(cover, ns=(int(future * 1e9), int(future * 1e9)))

    replacement = cache.load_texture(cover, 60, 90)
    assert replacement is not original


def test_replaced_file_with_identical_size_and_mtime_is_detected(png_factory) -> None:
    """Device and inode catch a replacement the size/mtime pair cannot.

    ``os.replace`` installs a new inode while size and timestamp can be
    restored exactly; only the inode distinguishes the two versions.
    """
    cache = CoverTextureCache()
    cover = png_factory("inode.png", width=120, height=180)
    original = cache.load_texture(cover, 60, 90)
    assert original is not None

    snapshot = os.stat(cover)
    replacement = cover.parent / "inode-replacement.png"
    replacement.write_bytes(cover.read_bytes())  # identical bytes and size
    os.utime(replacement, ns=(snapshot.st_atime_ns, snapshot.st_mtime_ns))
    os.replace(replacement, cover)

    after = os.stat(cover)
    assert after.st_size == snapshot.st_size
    assert after.st_mtime_ns == snapshot.st_mtime_ns
    assert after.st_ino != snapshot.st_ino

    reloaded = cache.load_texture(cover, 60, 90)
    assert reloaded is not None
    assert reloaded is not original
    assert cache.stats().misses == 2


def test_replacement_during_decode_is_never_cached(png_factory) -> None:
    """A file replaced mid-decode must not pin old pixels to the new version.

    Reviewer sequence: cache version A, replace with B, start decoding B, then
    replace B with C while the decode runs. The decoded B pixels are discarded
    instead of being stored under C's identity, and the next request decodes
    the current file again.
    """
    cache = CoverTextureCache()
    cover = png_factory("race.png", width=120, height=180)

    first = cache.load_texture(cover, 60, 90)
    assert first is not None

    version_b = time.time() + 60
    _make_png(cover, 120, 180, 0xFF101010)  # version B
    os.utime(cover, ns=(int(version_b * 1e9), int(version_b * 1e9)))

    decode_calls = 0
    original_decode = cache._decode

    def decode_b_then_write_c(path, width, height):
        nonlocal decode_calls
        decode_calls += 1
        pixbuf = original_decode(path, width, height)
        version_c = version_b + 60
        _make_png(cover, 120, 180, 0xFF202020)  # version C lands mid-decode
        os.utime(cover, ns=(int(version_c * 1e9), int(version_c * 1e9)))
        return pixbuf

    cache._decode = decode_b_then_write_c
    try:
        raced = cache.load_texture(cover, 60, 90)
    finally:
        cache._decode = original_decode

    assert raced is None
    assert cache.size() == 0

    final = cache.load_texture(cover, 60, 90)
    assert final is not None
    assert final is not first
    assert decode_calls == 1  # only the raced decode used the wrapper
    stats = cache.stats()
    assert stats.loads == stats.hits + stats.misses
    assert stats.hits == 0  # the discarded entry was never served
    assert stats.misses == 3  # A missing, raced B discarded, C decoded fresh
    assert cache.size() == 1


def test_deleted_file_returns_none_and_drops_entry(png_factory) -> None:
    cache = CoverTextureCache()
    cover = png_factory()
    assert cache.load_texture(cover, 60, 90) is not None
    assert cache.size() == 1

    cover.unlink()
    assert cache.load_texture(cover, 60, 90) is None
    assert cache.size() == 0
    # The request after the file vanished is recorded as a miss.
    assert cache.stats().misses == 2


def test_reappearing_file_is_decoded_again(png_factory) -> None:
    cache = CoverTextureCache()
    cover = png_factory()
    original = cache.load_texture(cover, 60, 90)
    cover.unlink()
    assert cache.load_texture(cover, 60, 90) is None
    _make_png(cover, 120, 180, 0xFF101010)
    future = time.time() + 5
    os.utime(cover, ns=(int(future * 1e9), int(future * 1e9)))
    again = cache.load_texture(cover, 60, 90)
    assert again is not None and again is not original


def test_invalidate_drops_all_size_variants(png_factory) -> None:
    cache = CoverTextureCache()
    cover = png_factory()
    cache.load_texture(cover, 60, 90)
    cache.load_texture(cover, 30, 45)
    cache.load_texture(cover, 80, 104)
    assert cache.size() == 3
    assert cache.invalidate(cover) is True
    assert cache.size() == 0


def test_invalidate_unknown_path_is_a_noop(png_factory) -> None:
    cache = CoverTextureCache()
    cover = png_factory()
    cache.load_texture(cover, 60, 90)
    assert cache.invalidate(tmp_absent := cover.parent / "absent.png") is False
    assert cache.size() == 1


def test_clear_drops_everything(png_factory) -> None:
    cache = CoverTextureCache()
    for index in range(3):
        cache.load_texture(png_factory(f"c{index}.png"), 30, 30)
    assert cache.size() == 3
    cache.clear()
    assert cache.size() == 0


# ---------------------------------------------------------------------------
# Bounds and eviction


def test_limit_enforced(png_factory) -> None:
    cache = CoverTextureCache(limit=3)
    covers = [png_factory(f"bounded-{index}.png") for index in range(5)]
    for cover in covers:
        cache.load_texture(cover, 30, 30)
    assert cache.size() == 3
    assert cache.stats().evictions == 2


def test_eviction_follows_lru_order(png_factory) -> None:
    cache = CoverTextureCache(limit=3)
    a = png_factory("lru-a.png")
    b = png_factory("lru-b.png")
    c = png_factory("lru-c.png")
    d = png_factory("lru-d.png")
    cache.load_texture(a, 30, 30)
    cache.load_texture(b, 30, 30)
    cache.load_texture(c, 30, 30)
    # Touching A promotes it, so B is now the least-recently used entry.
    cache.load_texture(a, 30, 30)
    assert cache.stats().hits == 1
    # Loading D must evict B, not A.
    cache.load_texture(d, 30, 30)
    assert cache.stats().evictions == 1
    assert cache.size() == 3
    # B was evicted: requesting it decodes again (miss).
    cache.load_texture(b, 30, 30)
    assert cache.stats().misses == 5
    # A survived because of the earlier promotion: still a hit.
    cache.load_texture(a, 30, 30)
    assert cache.stats().hits == 2


def test_zero_or_negative_limit_is_rejected() -> None:
    with pytest.raises(ValueError):
        CoverTextureCache(limit=0)
    with pytest.raises(ValueError):
        CoverTextureCache(limit=-5)


def test_default_cache_limit_is_sane() -> None:
    assert 32 <= DEFAULT_CACHE_LIMIT <= 128


def test_default_cache_is_used_by_helper(png_factory) -> None:
    cover = png_factory()
    first = load_cover_texture(str(cover), 240, 320)
    assert DEFAULT_CACHE.size() >= 1
    second = load_cover_texture(str(cover), 240, 320)
    assert first is second
    DEFAULT_CACHE.clear()


def test_cache_none_bypasses_cache(png_factory) -> None:
    cover = png_factory()
    first = load_cover_texture(str(cover), 60, 90, cache=None)
    second = load_cover_texture(str(cover), 60, 90, cache=None)
    assert first is not second  # Decoded twice.
    assert DEFAULT_CACHE.size() == 0


# ---------------------------------------------------------------------------
# Invalid and missing images


def test_missing_file_returns_none(png_factory, tmp_path: Path) -> None:
    cache = CoverTextureCache()
    assert cache.load_texture(tmp_path / "absent.png", 60, 90) is None
    assert cache.size() == 0


def test_invalid_file_returns_none(png_factory, tmp_path: Path) -> None:
    cache = CoverTextureCache()
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"not an image at all")
    assert cache.load_texture(bad, 60, 90) is None
    assert cache.size() == 0
    # Repeated failure stays safe.
    assert cache.load_texture(bad, 60, 90) is None


def test_invalid_file_replaced_by_valid_one_is_decoded(png_factory, tmp_path: Path) -> None:
    cache = CoverTextureCache()
    bad = tmp_path / "later-valid.png"
    bad.write_bytes(b"garbage")
    assert cache.load_texture(bad, 60, 90) is None
    _make_png(bad, 120, 180, 0xFF304050)
    future = time.time() + 5
    os.utime(bad, ns=(int(future * 1e9), int(future * 1e9)))
    texture = cache.load_texture(bad, 60, 90)
    assert texture is not None


def test_empty_path_is_none() -> None:
    assert load_cover_texture(None) is None
    assert load_cover_texture("") is None


def test_load_cover_texture_from_bytes_decodes_valid_png(tmp_path: Path) -> None:
    png = _make_png(tmp_path / "bytes.png", 120, 180, 0xFF204060)
    texture = load_cover_texture_from_bytes(png.read_bytes())
    assert texture is not None
    assert texture.get_width() == 120
    assert texture.get_height() == 180


def test_load_cover_texture_from_bytes_handles_garbage() -> None:
    assert load_cover_texture_from_bytes(b"definitely not an image") is None


def test_load_cover_texture_from_bytes_handles_empty() -> None:
    assert load_cover_texture_from_bytes(b"") is None


# ---------------------------------------------------------------------------
# Object release


def test_evicted_texture_can_be_released(png_factory) -> None:
    """No cache-side reference survives eviction."""
    cache = CoverTextureCache(limit=1)
    a = png_factory("release-a.png")
    b = png_factory("release-b.png")
    first = cache.load_texture(a, 30, 30)
    weak = weakref.ref(first)
    del first

    cache.load_texture(b, 30, 30)  # Evicts A's entry.

    gc.collect()
    assert weak() is None


def test_cleared_texture_can_be_released(png_factory) -> None:
    cache = CoverTextureCache()
    cover = png_factory()
    texture = cache.load_texture(cover, 30, 30)
    weak = weakref.ref(texture)
    del texture
    cache.clear()
    gc.collect()
    assert weak() is None


def test_invalidate_releases_texture(png_factory) -> None:
    cache = CoverTextureCache()
    cover = png_factory()
    texture = cache.load_texture(cover, 30, 30)
    weak = weakref.ref(texture)
    del texture
    cache.invalidate(cover)
    gc.collect()
    assert weak() is None


# ---------------------------------------------------------------------------
# Thread bookkeeping sanity


def test_stats_are_consistent(png_factory) -> None:
    cache = CoverTextureCache(limit=2)
    a = png_factory("stats-a.png")
    b = png_factory("stats-b.png")
    c = png_factory("stats-c.png")
    cache.load_texture(a, 30, 30)   # miss
    cache.load_texture(a, 30, 30)   # hit
    cache.load_texture(b, 30, 30)   # miss
    cache.load_texture(a, 30, 30)   # hit
    cache.load_texture(c, 30, 30)   # miss + evict a
    stats = cache.stats()
    assert stats.loads == stats.hits + stats.misses
    assert stats.evictions == 1
    assert cache.size() == 2


# ---------------------------------------------------------------------------
# Byte budget and pruning


def test_byte_budget_evicts_until_under_limit(png_factory) -> None:
    # Two 64x64 textures fit; the third must evict the least-recently used.
    budget = 64 * 64 * 4 * 2
    cache = CoverTextureCache(limit=100, budget_bytes=budget)
    for index in range(3):
        cover = png_factory(f"budget-{index}.png", width=64, height=64)
        cache.load_texture(cover, 64, 64)

    stats = cache.stats()
    assert cache.bytes_cached() <= budget
    assert stats.bytes_cached == cache.bytes_cached()
    assert stats.evictions == 1
    assert cache.size() == 2


def test_byte_budget_counts_each_entry(png_factory) -> None:
    cache = CoverTextureCache(budget_bytes=64 * 64 * 4)
    cover = png_factory("accounting.png", width=64, height=64)
    texture = cache.load_texture(cover, 64, 64)

    assert texture is not None
    assert cache.bytes_cached() == 64 * 64 * 4
    assert cache.stats().bytes_cached == 64 * 64 * 4

    cache.invalidate(cover)
    assert cache.bytes_cached() == 0


def test_replacing_an_entry_does_not_double_count_bytes(png_factory) -> None:
    cache = CoverTextureCache()
    cover = png_factory("replace-bytes.png", width=64, height=64)
    cache.load_texture(cover, 64, 64)

    _make_png(cover, 64, 64, 0xFF00FF00)
    future = time.time() + 5
    os.utime(cover, ns=(int(future * 1e9), int(future * 1e9)))
    cache.load_texture(cover, 64, 64)

    assert cache.size() == 1
    assert cache.bytes_cached() == 64 * 64 * 4


def test_prune_dead_drops_deleted_and_replaced_entries(png_factory) -> None:
    cache = CoverTextureCache()
    deleted = png_factory("deleted.png")
    replaced = png_factory("replaced.png", width=64, height=64)
    live = png_factory("live.png")

    for cover in (deleted, replaced, live):
        assert cache.load_texture(cover, 64, 64) is not None
    assert cache.size() == 3

    deleted.unlink()
    _make_png(replaced, 64, 64, 0xFF00FF00)
    future = time.time() + 5
    os.utime(replaced, ns=(int(future * 1e9), int(future * 1e9)))

    assert cache.prune_dead() == 2
    assert cache.stats().prunes == 2
    assert cache.size() == 1
    # The live entry is still served from cache.
    assert cache.load_texture(live, 64, 64) is not None
    assert cache.stats().hits == 1


def test_prune_dead_is_a_noop_when_everything_is_live(png_factory) -> None:
    cache = CoverTextureCache()
    cover = png_factory()
    cache.load_texture(cover, 64, 64)

    assert cache.prune_dead() == 0
    assert cache.size() == 1
    assert cache.stats().prunes == 0


def test_large_library_stays_within_budget(png_factory) -> None:
    """160 card-size covers must not pin more than the configured budget."""
    cache = CoverTextureCache(budget_bytes=16 * 1024 * 1024)
    covers = [png_factory(f"large-{index}.png", 184, 246) for index in range(160)]

    for cover in covers:
        cache.load_texture(cover, 368, 492)

    stats = cache.stats()
    assert cache.size() < len(covers)
    assert stats.evictions == len(covers) - cache.size()
    assert cache.bytes_cached() <= 16 * 1024 * 1024


def test_evicted_texture_survives_while_a_widget_displays_it(png_factory) -> None:
    """Eviction only drops the cache's reference, never the visible one."""
    from gi.repository import Gtk

    if Gdk.Display.get_default() is None:
        pytest.skip("constructing GTK widgets needs a GDK display")

    cache = CoverTextureCache(limit=1)
    first = png_factory("visible-a.png", width=64, height=64)
    second = png_factory("visible-b.png", width=64, height=64)

    texture = cache.load_texture(first, 64, 64)
    assert texture is not None
    picture = Gtk.Picture.new_for_paintable(texture)
    del texture

    cache.load_texture(second, 64, 64)  # Evicts the first entry.
    gc.collect()

    # The widget still owns the texture, so eviction cannot blank the card.
    retained = picture.get_paintable()
    assert retained is not None
    assert (retained.get_width(), retained.get_height()) == (64, 64)


# ---------------------------------------------------------------------------
# Oversized images are refused before decoding


def _png_header(width: int, height: int) -> bytes:
    """Return a PNG signature, IHDR, and empty IDAT with no pixel data."""
    import struct
    import zlib

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    header = b"IHDR" + ihdr
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", len(ihdr))
        + header
        + struct.pack(">I", zlib.crc32(header) & 0xFFFFFFFF)
        + struct.pack(">I", 0)
        + b"IDAT"
        + struct.pack(">I", zlib.crc32(b"IDAT") & 0xFFFFFFFF)
    )


def test_oversized_cover_is_rejected_before_decode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    big = tmp_path / "big.png"
    big.write_bytes(_png_header(7000, 7000))
    cache = CoverTextureCache()

    decoded: list[object] = []

    def record_decode(*args, **kwargs):
        decoded.append(args)
        return None

    monkeypatch.setattr(
        GdkPixbuf.Pixbuf, "new_from_file_at_scale", staticmethod(record_decode)
    )

    assert cache.load_texture(big, 368, 492) is None
    assert decoded == []
    assert cache.size() == 0


def test_oversized_preview_bytes_are_rejected_before_loading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import minimal_game_launcher.ui.cover as cover_module

    class _ExplodingLoader:
        def __init__(self) -> None:
            raise AssertionError("oversized preview must not reach the loader")

    monkeypatch.setattr(
        cover_module.GdkPixbuf, "PixbufLoader", _ExplodingLoader
    )

    assert load_cover_texture_from_bytes(_png_header(7000, 7000)) is None
