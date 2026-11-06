"""Validate, cache, and remove cover images owned by the launcher.

Manual selections are copied into the application cover cache; downloaded
covers are written there directly. The cache filename is derived from the game
ID plus a random token, so replacing a cover never overwrites the previous file
and different games cannot collide. Original user-selected images are only
read and are never modified or deleted.

Validation uses GdkPixbuf so the installed image loaders decide whether PNG,
JPEG, or WebP support is available. Dimensions are read from the image header
*before* any pixel buffer is allocated: a 7000×7000 PNG is only ~0.7 MiB on
disk but would decode to ~187 MiB of RGBA, so the width, height, and total
pixel count are checked first and oversized images are rejected without a
decode. Only images that pass those checks are decoded, and local previews are
scaled while decoding rather than afterwards. Only files inside the cache can
be deleted; legacy external cover references and arbitrary user files are left
untouched.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

import gi

gi.require_version("GdkPixbuf", "2.0")

from gi.repository import GdkPixbuf, GLib  # noqa: E402

from minimal_game_launcher.errors import (  # noqa: E402
    CoverCacheError,
    CoverValidationError,
)

LOGGER = logging.getLogger(__name__)

MAX_COVER_BYTES = 10 * 1024 * 1024
MIN_COVER_DIMENSION = 16
MAX_COVER_DIMENSION = 10_000

#: Maximum decoded pixel count. 12 megapixels is 48 MiB of RGBA and still
#: comfortably above any real cover art (a 2000×3000 poster is 6 MP).
MAX_COVER_PIXELS = 12_000_000

#: Longest side used when a workflow only needs a preview rather than the
#: original pixels. Aspect ratio is preserved by the loader.
PREVIEW_MAX_DIMENSION = 512

_FORMAT_SUFFIXES = {
    "png": ".png",
    "jpeg": ".jpg",
    "webp": ".webp",
}
_MIME_FORMATS = {
    "image/png": "png",
    "image/jpeg": "jpeg",
    "image/jpg": "jpeg",
    "image/webp": "webp",
}


@dataclass(frozen=True, slots=True)
class ImageInfo:
    """Validated properties of a cover image."""

    width: int
    height: int
    format: str
    suffix: str
    byte_size: int


class CoverManager:
    """Own the application cover cache and image validation."""

    def __init__(self, covers_root: str | Path) -> None:
        self._covers_root = Path(covers_root).expanduser()

    @property
    def covers_root(self) -> Path:
        """Return the app-owned cover cache directory."""
        return self._covers_root

    def inspect_bytes(
        self,
        data: bytes,
        *,
        content_type: str | None = None,
    ) -> ImageInfo:
        """Validate image bytes without decoding oversized images.

        The header is parsed for the format and dimensions first; only an
        image inside the dimension and pixel limits is decoded, which confirms
        the file is complete and not truncated.
        """
        if not data:
            raise CoverValidationError("The selected cover file is empty.")
        if len(data) > MAX_COVER_BYTES:
            raise CoverValidationError(
                "The cover image is larger than "
                f"{MAX_COVER_BYTES // (1024 * 1024)} MiB."
            )

        expected_format: str | None = None
        if content_type is not None:
            normalized = content_type.split(";", 1)[0].strip().lower()
            expected_format = _MIME_FORMATS.get(normalized)
            if expected_format is None:
                raise CoverValidationError(
                    "The downloaded cover is not a supported image type. "
                    "Expected PNG, JPEG, or WebP."
                )

        format_name, width, height = self._probe(data)
        if expected_format is not None and expected_format != format_name:
            raise CoverValidationError(
                "The downloaded file's content does not match its declared "
                "image type."
            )
        self._validate_dimensions(width, height)

        # Dimensions are acceptable: decode to prove the image is complete and
        # not truncated. The decode is requested at preview size, so callers
        # that only need a preview never hold the original-resolution buffer;
        # formats whose loader cannot scale while decoding (PNG in GdkPixbuf)
        # still allocate the accepted image's buffer transiently.
        pixbuf = self._decode_bounded(data, width, height)
        if pixbuf is None:
            raise CoverValidationError(
                "The selected cover could not be decoded. Choose a valid "
                "PNG, JPEG, or WebP image supported by the installed loaders."
            )

        return ImageInfo(
            width=width,
            height=height,
            format=format_name,
            suffix=_FORMAT_SUFFIXES[format_name],
            byte_size=len(data),
        )

    def inspect_file(self, source: str | Path) -> ImageInfo:
        """Validate a local image without copying or modifying it."""
        path = Path(source).expanduser()
        if not path.is_file():
            raise CoverValidationError(
                f"The selected cover file was not found: {path}"
            )
        try:
            byte_size = path.stat().st_size
        except OSError as error:
            raise CoverValidationError(
                f"The selected cover file could not be read: {path}"
            ) from error
        if byte_size > MAX_COVER_BYTES:
            raise CoverValidationError(
                "The cover image is larger than "
                f"{MAX_COVER_BYTES // (1024 * 1024)} MiB."
            )
        try:
            data = path.read_bytes()
        except OSError as error:
            raise CoverValidationError(
                f"The selected cover file could not be read: {path}"
            ) from error
        return self.inspect_bytes(data)

    def import_local(self, game_id: str, source: str | Path) -> str:
        """Validate and copy a local image into the per-game cover cache."""
        path = Path(source).expanduser()
        info = self.inspect_file(path)
        try:
            data = path.read_bytes()
        except OSError as error:
            raise CoverValidationError(
                f"The selected cover file could not be read: {path}"
            ) from error
        # Reuse the already validated data; the source file is never written.
        return self._write_cache(game_id, data, info.suffix)

    def store_download(
        self,
        game_id: str,
        data: bytes,
        content_type: str,
    ) -> str:
        """Validate and cache a cover downloaded after explicit selection."""
        info = self.inspect_bytes(data, content_type=content_type)
        return self._write_cache(game_id, data, info.suffix)

    def remove_cover(self, cover_path: str | Path | None) -> bool:
        """Delete a cached cover, never an external or user-owned file."""
        if not cover_path:
            return False

        candidate = Path(cover_path).expanduser()
        try:
            resolved_root = self._covers_root.resolve(strict=False)
            resolved = candidate.resolve(strict=False)
        except (OSError, RuntimeError):
            return False

        if not resolved.is_relative_to(resolved_root):
            LOGGER.info(
                "Leaving non-cached cover untouched: %s", candidate
            )
            return False
        if not resolved.is_file():
            return False
        try:
            resolved.unlink()
        except OSError:
            LOGGER.exception("Could not remove cached cover %s", resolved)
            return False
        LOGGER.info("Removed cached cover %s", resolved)
        return True

    def is_cached_cover(self, cover_path: str | Path | None) -> bool:
        """Return whether a path belongs to the application cover cache."""
        if not cover_path:
            return False
        try:
            root = self._covers_root.resolve(strict=False)
            candidate = Path(cover_path).expanduser().resolve(strict=False)
        except (OSError, RuntimeError):
            return False
        return candidate.is_relative_to(root)

    # ------------------------------------------------------------------
    # Header-only probing

    def _probe(self, data: bytes) -> tuple[str, int, int]:
        """Return ``(format, width, height)`` without decoding any pixels.

        ``GdkPixbuf.Pixbuf.get_file_info`` parses only the container header,
        but it needs a path. The bytes are exposed through an anonymous
        in-memory file (``memfd``) so no temporary file is written and no
        pixel buffer is allocated. A small header-only read is all the
        installed loaders need.
        """
        info = probe_image_bytes(data)
        if info is None:
            raise CoverValidationError(
                "The selected cover could not be decoded. Choose a valid "
                "PNG, JPEG, or WebP image supported by the installed loaders."
            )
        format_name, width, height = info
        if format_name not in _FORMAT_SUFFIXES:
            raise CoverValidationError(
                f"The cover format “{format_name}” is not supported. "
                "Use PNG, JPEG, or WebP."
            )
        return format_name, width, height

    def _decode_bounded(
        self, data: bytes, width: int, height: int
    ) -> GdkPixbuf.Pixbuf | None:
        """Decode ``data`` at preview size, or ``None`` when it is unusable."""
        loader = GdkPixbuf.PixbufLoader()
        try:
            # Compute both axes here: this GdkPixbuf rejects -1, and passing
            # equal axes would stretch the image instead of preserving it.
            scale = min(1.0, PREVIEW_MAX_DIMENSION / max(width, height))
            loader.set_size(
                max(1, round(width * scale)),
                max(1, round(height * scale)),
            )
        except (GLib.Error, TypeError):
            pass
        try:
            loader.write(data)
            loader.close()
            return loader.get_pixbuf()
        except GLib.Error:
            return None
        finally:
            try:
                loader.close()
            except GLib.Error:
                pass

    def _validate_dimensions(self, width: int, height: int) -> None:
        """Reject dimensions outside the accepted cover limits."""
        if width < MIN_COVER_DIMENSION or height < MIN_COVER_DIMENSION:
            raise CoverValidationError(
                "The cover image is too small. Choose an image at least "
                f"{MIN_COVER_DIMENSION}×{MIN_COVER_DIMENSION} pixels."
            )
        if width > MAX_COVER_DIMENSION or height > MAX_COVER_DIMENSION:
            raise CoverValidationError(
                "The cover image is too large. Choose an image no larger "
                f"than {MAX_COVER_DIMENSION}×{MAX_COVER_DIMENSION} pixels."
            )
        if width * height > MAX_COVER_PIXELS:
            raise CoverValidationError(
                "The cover image has too many pixels "
                f"({width}×{height} = {width * height:,}). Choose an image "
                f"with at most {MAX_COVER_PIXELS:,} pixels "
                "(for example 3000×4000)."
            )

    def _write_cache(self, game_id: str, data: bytes, suffix: str) -> str:
        """Write bytes atomically with a unique, game-ID-based filename."""
        try:
            self._covers_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as error:
            raise CoverCacheError(
                f"The cover cache directory could not be created: "
                f"{self._covers_root}"
            ) from error

        safe_id = re.sub(r"[^A-Za-z0-9._-]", "_", game_id)[:80] or "game"
        token = uuid.uuid4().hex[:12]
        target = self._covers_root / f"{safe_id}-{token}{suffix}"
        temporary = self._covers_root / f".{target.name}.tmp"

        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                0o600,
            )
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
            os.replace(temporary, target)
        except OSError as error:
            temporary.unlink(missing_ok=True)
            LOGGER.exception("Could not cache cover for game %s", game_id)
            raise CoverCacheError(
                "The cover image could not be written to the application "
                "cache."
            ) from error

        LOGGER.info(
            "Cached cover for game %s at %s (%s bytes)",
            game_id,
            target,
            len(data),
        )
        return str(target)


def _file_info_for_bytes(data: bytes):
    """Return ``(format, width, height)`` from image bytes without decoding.

    Uses ``GdkPixbuf.Pixbuf.get_file_info`` on an anonymous ``memfd`` so the
    header is parsed by the installed loader, with no temporary file on disk
    and no pixel allocation. Falls back to a short-lived temporary file on
    kernels without ``memfd_create``.
    """
    try:
        descriptor = os.memfd_create("mgl-cover-probe", os.MFD_CLOEXEC)
    except (AttributeError, OSError):
        return _file_info_from_tempfile(data)

    try:
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:  # Defensive: never spin on a failed write.
                return None
            view = view[written:]
        return _get_file_info(f"/proc/self/fd/{descriptor}")
    except OSError:
        return _file_info_from_tempfile(data)
    finally:
        os.close(descriptor)


def _file_info_from_tempfile(data: bytes):
    """Fallback header probe for platforms without ``memfd_create``."""
    handle = tempfile.NamedTemporaryFile(prefix="mgl-cover-probe-", delete=False)
    try:
        with handle:
            handle.write(data)
        return _get_file_info(handle.name)
    except OSError:
        return None
    finally:
        try:
            os.unlink(handle.name)
        except OSError:
            pass


def _get_file_info(path: str):
    """Wrap ``Pixbuf.get_file_info``; ``None`` when the header is unusable."""
    try:
        image_format, width, height = GdkPixbuf.Pixbuf.get_file_info(path)
    except (GLib.Error, ValueError):
        return None
    if image_format is None or width <= 0 or height <= 0:
        return None
    return image_format, width, height


def probe_image_bytes(data: bytes) -> tuple[str, int, int] | None:
    """Return ``(format, width, height)`` from a header without decoding.

    Public helper for callers that must reject oversized images before a
    decode allocates their pixel buffer. Returns ``None`` when the header is
    missing or unreadable.
    """
    info = _file_info_for_bytes(data)
    if info is None:
        return None
    image_format, width, height = info
    return image_format.get_name().casefold(), width, height
