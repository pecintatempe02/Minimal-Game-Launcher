"""Validate, cache, and remove cover images owned by the launcher.

Manual selections are copied into the application cover cache; downloaded
covers are written there directly. The cache filename is derived from the game
ID plus a random token, so replacing a cover never overwrites the previous file
and different games cannot collide. Original user-selected images are only
read and are never modified or deleted.

Validation uses GdkPixbuf so the installed image loaders decide whether PNG,
JPEG, or WebP support is available. Besides decoding the image, the manager
checks the byte size and pixel dimensions. Only files inside the cache can be
deleted; legacy external cover references and arbitrary user files are left
untouched.
"""

from __future__ import annotations

import logging
import os
import re
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
        """Decode image bytes and validate format, size, and dimensions."""
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

        loader = GdkPixbuf.PixbufLoader()
        try:
            loader.write(data)
            loader.close()
            pixbuf = loader.get_pixbuf()
            image_format = loader.get_format()
        except GLib.Error as error:
            raise CoverValidationError(
                "The selected cover could not be decoded. Choose a valid "
                "PNG, JPEG, or WebP image supported by the installed loaders."
            ) from error
        finally:
            try:
                loader.close()
            except GLib.Error:
                pass

        if pixbuf is None or image_format is None:
            raise CoverValidationError(
                "The selected cover could not be decoded as an image."
            )

        format_name = image_format.get_name().casefold()
        suffix = _FORMAT_SUFFIXES.get(format_name)
        if suffix is None:
            raise CoverValidationError(
                f"The cover format “{format_name}” is not supported. "
                "Use PNG, JPEG, or WebP."
            )
        if expected_format is not None and expected_format != format_name:
            raise CoverValidationError(
                "The downloaded file's content does not match its declared "
                "image type."
            )

        width = pixbuf.get_width()
        height = pixbuf.get_height()
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

        return ImageInfo(
            width=width,
            height=height,
            format=format_name,
            suffix=suffix,
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
