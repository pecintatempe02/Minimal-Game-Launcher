"""GdkPixbuf-based cover loading helpers shared by GTK widgets.

Covers may be cached files or legacy external references. Loading failures are
treated as a missing cover and fall back to a themed placeholder; the launcher
never edits or deletes user-owned source images.

``load_cover_texture`` serves every decoded cover through a bounded
least-recently-used cache (:class:`CoverTextureCache`) so repeated card
builds, refreshes, and dialog previews reuse one texture per
(path, width, height) instead of re-decoding the file. Entries are validated
against the file's device, inode, size, and nanosecond modification time, so
replaced or edited covers are decoded again on the next request. A texture
whose file changed mid-decode is discarded rather than cached. Entries hold only the
``Gdk.Texture``; evicted textures are released to the garbage collector as
soon as no widget references them.

Cache policy
------------
The cache is bounded by decoded bytes, not by entry count, because one entry
can be 0.7 MiB (a 368×492 card texture) or 80 KiB (a dialog thumbnail). The
default budget is 64 MiB, which covers roughly 90 card textures: enough for a
typical window plus previews without letting a large library pin hundreds of
megabytes. A hard entry cap of 128 is kept as a secondary guard for unusually
small textures.

Eviction drops the *cache's* reference to the least-recently-used texture. A
visible ``Gtk.Picture`` holds its own reference to the paintable it displays,
so eviction never blanks a card that is already on screen; it only means the
next request for that (path, size) decodes the file again. Rebuilding a
library larger than the budget therefore costs one decode per visible cover
per rebuild, never a decode of the whole library per *unchanged* refresh: the
main window reuses cards when the game list is unchanged.

Threading: decoding happens on the GTK main thread, where every cover request
originates (card construction and dialog previews). The bookkeeping lock only
guards the ordered dict and counters, so a re-entrant request from a widget
callback cannot corrupt state. Because decodes never overlap on that thread,
two requests for the same key cannot race into a duplicate decode.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")

from gi.repository import Gdk, GdkPixbuf, GLib  # noqa: E402

from minimal_game_launcher.services.cover_manager import (  # noqa: E402
    MAX_COVER_DIMENSION,
    MAX_COVER_PIXELS,
    probe_image_bytes,
)

LOGGER = logging.getLogger(__name__)

#: Decoded-byte budget for the shared cache. A card texture is 368×492 RGBA
#: ≈ 0.69 MiB, so 64 MiB holds roughly 90 cards: more than one screenful at
#: any sane window size, while a 500-cover library cannot pin ~350 MiB.
DEFAULT_CACHE_BUDGET_BYTES: Final[int] = 64 * 1024 * 1024

#: Secondary hard cap on entry count, for pathological libraries made of very
#: small covers. It is deliberately not a replacement for the byte budget.
DEFAULT_CACHE_LIMIT: Final[int] = 128

#: Bytes per decoded pixel for the RGB(A) textures this cache stores.
_BYTES_PER_PIXEL: Final[int] = 4


def texture_nbytes(texture: Gdk.Texture) -> int:
    """Return the estimated decoded size of ``texture`` in bytes."""
    return texture.get_width() * texture.get_height() * _BYTES_PER_PIXEL


@dataclass(frozen=True, slots=True)
class _CacheKey:
    """Identity of one decoded cover: resolved path and target size."""

    resolved: str
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class _FileIdentity:
    """The observed inode/size/mtime tuple used to detect replacement.

    The device and inode catch replacements that keep the same size and
    modification time (``os.replace`` installs a new inode, so the launcher's
    own cover writes are always visible even with coarse timestamps).
    """

    dev: int | None
    ino: int | None
    size: int | None
    mtime_ns: int | None


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    """One decoded texture plus the file identity recorded at decode time."""

    texture: Gdk.Texture
    nbytes: int
    dev: int | None
    ino: int | None
    size: int | None
    mtime_ns: int | None

    def matches(self, identity: _FileIdentity) -> bool:
        """Return whether the file still looks like the decoded version.

        A dead identity (the file disappeared) never matches, so a file that
        reappears is decoded again instead of serving a stale texture.
        """
        if identity.dev is None or identity.ino is None:
            return False
        return (
            identity.dev == self.dev
            and identity.ino == self.ino
            and identity.size == self.size
            and identity.mtime_ns == self.mtime_ns
        )


@dataclass(frozen=True, slots=True)
class CoverCacheStats:
    """Counters describing cache activity, used by tests and diagnostics."""

    loads: int
    hits: int
    misses: int
    evictions: int
    invalidations: int
    prunes: int = 0
    entries: int = 0
    bytes_cached: int = 0


class CoverTextureCache:
    """Byte-bounded LRU cache for decoded cover textures.

    The cache maps (resolved path, width, height) to a ``Gdk.Texture``
    together with the file identity observed at decode time. It is designed
    for the GTK main thread, where all cover requests originate; a
    re-entrant lock guards the bookkeeping against any re-entrant call made
    from widget callbacks while a request is in flight. Decoding never
    happens implicitly — the caller triggers it via :meth:`load_texture`.
    """

    def __init__(
        self,
        *,
        limit: int = DEFAULT_CACHE_LIMIT,
        budget_bytes: int = DEFAULT_CACHE_BUDGET_BYTES,
    ) -> None:
        if limit < 1:
            raise ValueError("Cover cache limit must be at least 1 entry.")
        if budget_bytes < 1:
            raise ValueError("Cover cache budget must be at least 1 byte.")
        self._limit = limit
        self._budget_bytes = budget_bytes
        self._entries: OrderedDict[_CacheKey, _CacheEntry] = OrderedDict()
        self._bytes = 0
        self._lock = threading.RLock()
        self._loads = 0
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._invalidations = 0
        self._prunes = 0

    @property
    def limit(self) -> int:
        """Return the maximum number of cached textures."""
        return self._limit

    @property
    def budget_bytes(self) -> int:
        """Return the decoded-memory budget in bytes."""
        return self._budget_bytes

    def stats(self) -> CoverCacheStats:
        """Return current activity counters and occupancy."""
        with self._lock:
            return CoverCacheStats(
                loads=self._loads,
                hits=self._hits,
                misses=self._misses,
                evictions=self._evictions,
                invalidations=self._invalidations,
                prunes=self._prunes,
                entries=len(self._entries),
                bytes_cached=self._bytes,
            )

    def size(self) -> int:
        """Return the number of textures held right now."""
        with self._lock:
            return len(self._entries)

    def bytes_cached(self) -> int:
        """Return the estimated decoded memory held by cached textures."""
        with self._lock:
            return self._bytes

    def load_texture(
        self, path: str | Path, width: int, height: int
    ) -> Gdk.Texture | None:
        """Return the decoded texture for ``path`` at ``width``×``height``.

        A cached entry is reused while the file still has the device, inode,
        size, and modification time recorded at decode time; any mismatch
        decodes the file again. Missing, unreadable, or undecodable files
        return ``None``. Failures are not cached: a missing cover may reappear
        and a temporarily unreadable file may become readable, so the next
        request retries. A file replaced while it was being decoded is also
        returned as ``None`` and not cached, so old pixels are never stored
        under the replacement's identity.
        """
        cover = Path(path).expanduser()
        try:
            cover = cover.resolve(strict=False)
        except OSError:  # Unresolvable path; fall back to the raw path.
            LOGGER.debug("Could not resolve cover path %s", path)
        key = _CacheKey(str(cover), width, height)
        identity = self._stat(cover)

        with self._lock:
            self._loads += 1
            entry = self._entries.get(key)
            if entry is not None and entry.matches(identity):
                self._entries.move_to_end(key)
                self._hits += 1
                return entry.texture

        pixbuf = self._decode(cover, width, height)
        with self._lock:
            self._misses += 1
            if pixbuf is None:
                # A decode failure means the entry (if any) described a file
                # that is gone or no longer decodable; drop it so the cache
                # does not hold dead slots.
                self._discard(key)
                return None
            # The file may have been replaced while it was decoded. Only cache
            # pixels that provably belong to the version now on disk; recording
            # a fresh identity here would pin old pixels to a new file.
            if self._stat(cover) != identity:
                self._discard(key)
                return None
            texture = Gdk.Texture.new_for_pixbuf(pixbuf)
            entry = _CacheEntry(
                texture=texture,
                nbytes=texture_nbytes(texture),
                dev=identity.dev,
                ino=identity.ino,
                size=identity.size,
                mtime_ns=identity.mtime_ns,
            )
            # Replacement (same key, new pixels) must not double-count bytes.
            self._discard(key)
            self._entries[key] = entry
            self._bytes += entry.nbytes
            self._entries.move_to_end(key)
            self._evict_over_budget()
            return texture

    def invalidate(self, path: str | Path) -> bool:
        """Drop every cached size variant of ``path``; True when one existed.

        Called when the launcher itself rewrites a cover file, so the next
        request cannot briefly serve the stale texture.
        """
        cover = Path(path).expanduser()
        try:
            resolved = str(cover.resolve(strict=False))
        except OSError:
            resolved = str(cover)
        with self._lock:
            stale = [key for key in self._entries if key.resolved == resolved]
            for key in stale:
                self._discard(key)
            if stale:
                self._invalidations += 1
            return bool(stale)

    def prune_dead(self) -> int:
        """Drop entries whose file is gone or has been replaced.

        Called on library refresh so a deleted or replaced cover does not keep
        an entry (and its decoded memory) alive until that path is requested
        again. Returns the number of dropped entries.
        """
        with self._lock:
            stale = [
                key
                for key, entry in self._entries.items()
                if not entry.matches(self._stat(Path(key.resolved)))
            ]
            for key in stale:
                self._discard(key)
            self._prunes += len(stale)
            return len(stale)

    def clear(self) -> None:
        """Drop every cached texture immediately."""
        with self._lock:
            self._entries.clear()
            self._bytes = 0
            self._invalidations += 1

    def _discard(self, key: _CacheKey) -> bool:
        """Remove one entry and subtract its bytes from the running total."""
        entry = self._entries.pop(key, None)
        if entry is None:
            return False
        self._bytes -= entry.nbytes
        if self._bytes < 0:  # Defensive: accounting must never go negative.
            self._bytes = 0
        return True

    def _evict_over_budget(self) -> None:
        """Discard least-recently-used entries until both bounds are met."""
        while self._entries and (
            len(self._entries) > self._limit
            or self._bytes > self._budget_bytes
        ):
            key, entry = self._entries.popitem(last=False)
            self._bytes -= entry.nbytes
            self._evictions += 1
        if self._bytes < 0:
            self._bytes = 0

    def _stat(self, cover: Path) -> _FileIdentity:
        """Return the file identity, or a dead identity when unavailable."""
        try:
            info = cover.stat()
        except OSError:
            return _FileIdentity(None, None, None, None)
        return _FileIdentity(
            info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns
        )

    def _decode(
        self, cover: Path, width: int, height: int
    ) -> GdkPixbuf.Pixbuf | None:
        """Decode ``cover`` scaled to fit inside ``width``×``height``.

        The header is checked first: GdkPixbuf decodes a PNG at full
        resolution even when asked to scale, so a legacy cover outside the
        validation limits would otherwise allocate its entire pixel buffer.
        Such covers render as the placeholder instead.
        """
        try:
            image_format, source_width, source_height = (
                GdkPixbuf.Pixbuf.get_file_info(str(cover))
            )
        except (GLib.Error, ValueError):
            return None
        if image_format is None:
            return None
        if (
            source_width > MAX_COVER_DIMENSION
            or source_height > MAX_COVER_DIMENSION
            or source_width * source_height > MAX_COVER_PIXELS
        ):
            LOGGER.info(
                "Refusing oversized cover %s (%sx%s)",
                cover,
                source_width,
                source_height,
            )
            return None
        try:
            return GdkPixbuf.Pixbuf.new_from_file_at_scale(
                str(cover), width, height, True
            )
        except (GLib.Error, OSError):
            return None


#: Shared cache used by :func:`load_cover_texture`. One process-wide cache is
#: intentional: the card grid, the editor preview, and the cover dialog all
#: request the same files at similar sizes, so they share decoded textures.
DEFAULT_CACHE = CoverTextureCache()


def load_cover_texture(
    path: str | None,
    width: int = 360,
    height: int = 480,
    *,
    cache: CoverTextureCache | None = DEFAULT_CACHE,
) -> Gdk.Texture | None:
    """Load and scale a cover file, returning ``None`` if it is unusable.

    Results are served through the shared bounded LRU cache. Pass
    ``cache=None`` to bypass caching entirely, or another
    :class:`CoverTextureCache` instance to isolate callers in tests.
    """
    if not path:
        return None
    if cache is None:
        # Preserve the original uncached behaviour.
        cover = Path(path).expanduser()
        pixbuf = _decode_uncached(cover, width, height)
        return None if pixbuf is None else Gdk.Texture.new_for_pixbuf(pixbuf)
    return cache.load_texture(path, width, height)


def _decode_uncached(
    cover: Path, width: int, height: int
) -> GdkPixbuf.Pixbuf | None:
    """Decode ``cover`` without touching any cache."""
    if not cover.is_file():
        return None
    try:
        return GdkPixbuf.Pixbuf.new_from_file_at_scale(
            str(cover), width, height, True
        )
    except (GLib.Error, OSError):
        return None


def load_cover_texture_from_bytes(
    data: bytes,
    *,
    max_width: int | None = None,
    max_height: int | None = None,
) -> Gdk.Texture | None:
    """Decode in-memory image bytes for previews without writing a file.

    Decoded results are deliberately not cached: preview bytes come from an
    explicit user selection and are rarely requested twice. When
    ``max_width``/``max_height`` are given, the loader scales while decoding,
    so a large preview image never allocates its full-resolution buffer.
    """
    info = probe_image_bytes(data)
    if info is None:
        return None
    _, source_width, source_height = info
    if (
        source_width > MAX_COVER_DIMENSION
        or source_height > MAX_COVER_DIMENSION
        or source_width * source_height > MAX_COVER_PIXELS
    ):
        LOGGER.info(
            "Refusing oversized preview image (%sx%s)",
            source_width,
            source_height,
        )
        return None
    loader = GdkPixbuf.PixbufLoader()
    if max_width is not None or max_height is not None:
        try:
            loader.set_size(max_width or -1, max_height or -1)
        except (GLib.Error, TypeError):
            pass
    try:
        loader.write(data)
        loader.close()
        pixbuf = loader.get_pixbuf()
    except GLib.Error:
        return None
    finally:
        try:
            loader.close()
        except GLib.Error:
            pass
    if pixbuf is None:
        return None
    return Gdk.Texture.new_for_pixbuf(pixbuf)
