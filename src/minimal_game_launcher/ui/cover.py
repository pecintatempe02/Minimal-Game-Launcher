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

LOGGER = logging.getLogger(__name__)

#: Maximum number of decoded textures retained at once. A card-grid texture
#: decodes at 368×492 RGBA ≈ 0.7 MiB, so 64 entries bound the cache to well
#: under 50 MiB in the worst case while comfortably covering a visible card
#: grid plus dialog previews.
DEFAULT_CACHE_LIMIT: Final[int] = 64


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


class CoverTextureCache:
    """Bounded LRU cache for decoded cover textures.

    The cache maps (resolved path, width, height) to a ``Gdk.Texture``
    together with the file identity observed at decode time. It is designed
    for the GTK main thread, where all cover requests originate; a
    re-entrant lock guards the bookkeeping against any re-entrant call made
    from widget callbacks while a request is in flight. Decoding never
    happens implicitly — the caller triggers it via :meth:`load_texture`.
    """

    def __init__(self, *, limit: int = DEFAULT_CACHE_LIMIT) -> None:
        if limit < 1:
            raise ValueError("Cover cache limit must be at least 1 entry.")
        self._limit = limit
        self._entries: OrderedDict[_CacheKey, _CacheEntry] = OrderedDict()
        self._lock = threading.RLock()
        self._loads = 0
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._invalidations = 0

    @property
    def limit(self) -> int:
        """Return the maximum number of cached textures."""
        return self._limit

    def stats(self) -> CoverCacheStats:
        """Return current activity counters."""
        with self._lock:
            return CoverCacheStats(
                loads=self._loads,
                hits=self._hits,
                misses=self._misses,
                evictions=self._evictions,
                invalidations=self._invalidations,
            )

    def size(self) -> int:
        """Return the number of textures held right now."""
        with self._lock:
            return len(self._entries)

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
                self._entries.pop(key, None)
                return None
            # The file may have been replaced while it was decoded. Only cache
            # pixels that provably belong to the version now on disk; recording
            # a fresh identity here would pin old pixels to a new file.
            if self._stat(cover) != identity:
                self._entries.pop(key, None)
                return None
            texture = Gdk.Texture.new_for_pixbuf(pixbuf)
            self._entries[key] = _CacheEntry(
                texture=texture,
                dev=identity.dev,
                ino=identity.ino,
                size=identity.size,
                mtime_ns=identity.mtime_ns,
            )
            self._entries.move_to_end(key)
            self._evict_over_limit()
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
                del self._entries[key]
            if stale:
                self._invalidations += 1
            return bool(stale)

    def clear(self) -> None:
        """Drop every cached texture immediately."""
        with self._lock:
            self._entries.clear()
            self._invalidations += 1

    def _evict_over_limit(self) -> None:
        """Discard least-recently-used entries until within the limit."""
        while len(self._entries) > self._limit:
            self._entries.popitem(last=False)
            self._evictions += 1

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
        """Decode ``cover`` scaled to fit inside ``width``×``height``."""
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


def load_cover_texture_from_bytes(data: bytes) -> Gdk.Texture | None:
    """Decode in-memory image bytes for previews without writing a file.

    Decoded results are deliberately not cached: preview bytes come from an
    explicit user selection and are rarely requested twice.
    """
    loader = GdkPixbuf.PixbufLoader()
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
