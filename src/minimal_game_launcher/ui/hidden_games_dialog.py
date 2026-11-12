"""Manage games hidden from the normal library."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, GObject, Gtk  # noqa: E402

from minimal_game_launcher.errors import LauncherError  # noqa: E402
from minimal_game_launcher.services.game_manager import GameManager  # noqa: E402
from minimal_game_launcher.ui.lifecycle import DialogSignalOwner  # noqa: E402


class HiddenGamesDialog(Adw.Dialog, DialogSignalOwner):
    """List hidden entries and restore them one at a time or all at once.

    Hiding never deletes data. This dialog is the explicit management path that
    keeps hidden games reachable without showing them in the library or search.
    """

    __gsignals__ = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, manager: GameManager) -> None:
        super().__init__()
        self._manager = manager
        self._rows: list[Gtk.Widget] = []
        self._active_error: Adw.AlertDialog | None = None
        self.set_content_width(560)
        self.set_content_height(560)
        self.set_can_close(True)
        self.set_child(self._build_ui())
        self._refresh()
        self._init_dialog_lifecycle()

    def _build_ui(self) -> Gtk.Widget:
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="Hidden games"))

        close = Gtk.Button(label="Close")
        close.add_css_class("flat")
        self.connect_owned(close, "clicked", lambda _b: self.close())
        header.pack_start(close)

        self._show_all = Gtk.Button(label="Show all")
        self.connect_owned(self._show_all, "clicked", self._on_show_all)
        header.pack_end(self._show_all)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)

        self._stack = Gtk.Stack()
        self._page = Adw.PreferencesPage()
        self._stack.add_named(self._page, "list")
        toolbar.set_content(self._stack)

        self._group = Adw.PreferencesGroup(
            title="Hidden games",
            description=(
                "These entries stay in the library database with all their "
                "settings. Nothing is deleted."
            ),
        )
        self._page.add(self._group)

        self._empty = Adw.StatusPage()
        self._empty.set_icon_name("view-reveal-symbolic")
        self._empty.set_title("No hidden games")
        self._empty.set_description(
            "Use the game editor's Show in library switch to hide a game."
        )
        self._empty.set_vexpand(True)
        self._stack.add_named(self._empty, "empty")
        return toolbar

    def _refresh(self) -> None:
        for row in self._rows:
            self._group.remove(row)
        self._rows = []
        games = self._manager.list_hidden_games()
        self._show_all.set_visible(bool(games))
        self._stack.set_visible_child_name("list" if games else "empty")
        for game in games:
            row = Adw.ActionRow(title=game.name, subtitle=game.platform.display_name)
            show = Gtk.Button(label="Show")
            show.set_valign(Gtk.Align.CENTER)
            self.connect_owned(
                show, "clicked", self._on_show_clicked, game.id
            )
            row.add_suffix(show)
            self._group.add(row)
            self._rows.append(row)

    def _on_show_clicked(self, _button: Gtk.Button, game_id: str) -> None:
        self._restore(game_id)

    def _on_show_all(self, _button: Gtk.Button) -> None:
        for game in self._manager.list_hidden_games():
            if not self._restore(game.id, refresh=False):
                break
        self._refresh()

    def _restore(self, game_id: str, *, refresh: bool = True) -> bool:
        try:
            self._manager.set_library_visibility(game_id, True)
        except LauncherError as error:
            self._show_error(str(error))
            return False
        if refresh:
            self._refresh()
        self.emit("changed")
        return True

    def _show_error(self, message: str) -> None:
        dialog = Adw.AlertDialog(heading="Cannot show game", body=message)
        dialog.add_response("close", "Close")
        dialog.set_default_response("close")
        dialog.set_close_response("close")
        self._active_error = dialog
        dialog.choose(self, None, self._on_error_closed)

    def _release_dialog_references(self) -> None:
        self._active_error = None
        self._rows = []
        self._stack = None
        self._group = None
        self._empty = None
        self._show_all = None

    def _on_error_closed(
        self, dialog: Adw.AlertDialog, result: object
    ) -> None:
        self._active_error = None
        try:
            dialog.choose_finish(result)
        except GLib.Error:
            pass
