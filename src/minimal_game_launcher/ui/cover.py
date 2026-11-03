"""GdkPixbuf-based cover loading helpers shared by GTK widgets.

Covers are external files selected by the user. Loading failures are treated
as a missing cover and fall back to a themed placeholder; the launcher never
edits or deletes the source image.
"""

from __future__ import annotations

from pathlib import Path

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")

from gi.repository import Gdk, GdkPixbuf, GLib  # noqa: E402


def load_cover_texture(
    path: str | None, width: int = 360, height: int = 480
) -> Gdk.Texture | None:
    """Load and scale a cover file, returning ``None`` if it is unusable."""
    if not path:
        return None
    cover = Path(path).expanduser()
    if not cover.is_file():
        return None
    try:
        pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
            str(cover), width, height, True
        )
    except (GLib.Error, OSError):
        return None
    return Gdk.Texture.new_for_pixbuf(pixbuf)
