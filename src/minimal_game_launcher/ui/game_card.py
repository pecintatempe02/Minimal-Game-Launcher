"""Grid card widget for one game entry."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")

from gi.repository import GObject, Gtk, Pango  # noqa: E402

from minimal_game_launcher.models import Game, Platform  # noqa: E402
from minimal_game_launcher.ui.cover import load_cover_texture  # noqa: E402


class GameCard(Gtk.FlowBoxChild):
    """A selectable library card with cover, platform, and status.

    The card exposes signals instead of calling managers directly. This keeps
    the widget free of database and validation concerns.
    """

    __gsignals__ = {
        "launch-requested": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "details-requested": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "edit-requested": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "remove-requested": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    COVER_WIDTH = 184
    COVER_HEIGHT = 246

    def __init__(self, game: Game) -> None:
        super().__init__()
        self.game = game
        self._status: str | None = None
        self._launch_menu_item: Gtk.Button | None = None
        self.add_css_class("game-card-child")
        self.set_child(self._build_content())
        self.set_tooltip_text(
            f"{game.name}\n{game.platform.display_name}\n"
            "Use the play button to launch. Double-click or use the menu for "
            "game details."
        )

    def _build_content(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.add_css_class("game-card")
        box.set_size_request(self.COVER_WIDTH, -1)

        cover = self._build_cover()
        box.append(cover)

        title = Gtk.Label(label=self.game.name)
        title.add_css_class("game-card-title")
        title.set_wrap(True)
        title.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        title.set_lines(2)
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.set_xalign(0.0)
        title.set_tooltip_text(self.game.name)
        box.append(title)

        meta = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        meta.set_valign(Gtk.Align.CENTER)
        platform = Gtk.Label(label=self.game.platform.display_name)
        platform.add_css_class("platform-pill")
        platform.add_css_class(
            "platform-windows"
            if self.game.platform is Platform.WINDOWS
            else "platform-linux"
        )
        meta.append(platform)

        self._running_pill = Gtk.Label(label="Running")
        self._running_pill.add_css_class("running-pill")
        self._running_pill.set_visible(False)
        meta.append(self._running_pill)
        box.append(meta)

        return box

    def _build_cover(self) -> Gtk.Widget:
        overlay = Gtk.Overlay()
        overlay.set_size_request(self.COVER_WIDTH, self.COVER_HEIGHT)

        frame = Gtk.Box()
        frame.add_css_class("cover-frame")
        frame.set_size_request(self.COVER_WIDTH, self.COVER_HEIGHT)
        frame.set_overflow(Gtk.Overflow.HIDDEN)

        texture = load_cover_texture(
            self.game.cover_path, self.COVER_WIDTH * 2, self.COVER_HEIGHT * 2
        )
        if texture is not None:
            picture = Gtk.Picture.new_for_paintable(texture)
            picture.set_content_fit(Gtk.ContentFit.COVER)
            picture.set_size_request(self.COVER_WIDTH, self.COVER_HEIGHT)
            frame.append(picture)
        else:
            frame.add_css_class("cover-placeholder")
            placeholder = Gtk.Box(
                orientation=Gtk.Orientation.VERTICAL,
                spacing=8,
                halign=Gtk.Align.CENTER,
                valign=Gtk.Align.CENTER,
            )
            icon = Gtk.Image.new_from_icon_name("applications-games-symbolic")
            icon.set_pixel_size(52)
            icon.add_css_class("dim-label")
            placeholder.append(icon)
            label = Gtk.Label(label="No cover")
            label.add_css_class("caption")
            label.add_css_class("dim-label")
            placeholder.append(label)
            frame.append(placeholder)

        overlay.set_child(frame)

        menu_box = Gtk.Box(halign=Gtk.Align.END, valign=Gtk.Align.START)
        menu_box.set_margin_top(6)
        menu_box.set_margin_end(6)
        menu_box.append(self._build_menu())
        overlay.add_overlay(menu_box)

        launch_box = Gtk.Box(halign=Gtk.Align.END, valign=Gtk.Align.END)
        launch_box.set_margin_bottom(8)
        launch_box.set_margin_end(8)
        self._launch_button = Gtk.Button.new_from_icon_name(
            "media-playback-start-symbolic"
        )
        self._launch_button.add_css_class("circular")
        self._launch_button.add_css_class("suggested-action")
        self._launch_button.add_css_class("game-launch-button")
        self._launch_button.set_tooltip_text("Launch game")
        self._launch_button.connect(
            "clicked", lambda _button: self.emit("launch-requested", self.game)
        )
        launch_box.append(self._launch_button)
        overlay.add_overlay(launch_box)

        return overlay

    def _build_menu(self) -> Gtk.Widget:
        popover = Gtk.Popover()
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        content.set_margin_top(6)
        content.set_margin_bottom(6)
        content.set_margin_start(6)
        content.set_margin_end(6)

        for label, icon_name, signal, css_class in (
            (
                "Launch game",
                "media-playback-start-symbolic",
                "launch-requested",
                "suggested-action",
            ),
            (
                "Game details",
                "dialog-information-symbolic",
                "details-requested",
                None,
            ),
            ("Edit game", "document-edit-symbolic", "edit-requested", None),
            (
                "Remove from library",
                "user-trash-symbolic",
                "remove-requested",
                "destructive-action",
            ),
        ):
            button = Gtk.Button()
            button.add_css_class("flat")
            if css_class:
                button.add_css_class(css_class)
            button.set_tooltip_text(label)
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            image = Gtk.Image.new_from_icon_name(icon_name)
            image.set_pixel_size(16)
            row.append(image)
            row.append(Gtk.Label(label=label, xalign=0.0))
            button.set_child(row)
            button.connect(
                "clicked", self._on_menu_item_clicked, popover, signal
            )
            if signal == "launch-requested":
                self._launch_menu_item = button
            content.append(button)

        popover.set_child(content)

        menu_button = Gtk.MenuButton()
        menu_button.add_css_class("game-card-menu")
        menu_button.set_icon_name("view-more-symbolic")
        menu_button.set_tooltip_text("Game actions")
        menu_button.set_popover(popover)
        return menu_button

    def _on_menu_item_clicked(
        self, _button: Gtk.Button, popover: Gtk.Popover, signal_name: str
    ) -> None:
        popover.popdown()
        self.emit(signal_name, self.game)

    def set_status(self, status: str | None) -> None:
        """Show ``starting``/``running`` state and disable duplicate launches."""
        if status not in (None, "starting", "running"):
            raise ValueError(f"Unsupported card status: {status}")

        self._status = status
        active = status is not None
        self._running_pill.set_visible(active)
        if status == "starting":
            self._running_pill.set_label("Starting")
        elif status == "running":
            self._running_pill.set_label("Running")

        self._launch_button.set_sensitive(not active)
        if self._launch_menu_item is not None:
            self._launch_menu_item.set_sensitive(not active)

    def set_running(self, running: bool) -> None:
        """Compatibility helper for callers that only know a boolean state."""
        self.set_status("running" if running else None)

    @property
    def running(self) -> bool:
        """Return the card's current active presentation state."""
        return self._status is not None
