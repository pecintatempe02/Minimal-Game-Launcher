"""Main application window and game-library presentation."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk  # noqa: E402

from minimal_game_launcher.errors import LauncherError  # noqa: E402
from minimal_game_launcher.models import Game  # noqa: E402
from minimal_game_launcher.services.game_manager import (  # noqa: E402
    SORT_NAME,
    SORT_RECENT,
    GameManager,
)
from minimal_game_launcher.ui.game_card import GameCard  # noqa: E402
from minimal_game_launcher.ui.game_details_dialog import GameDetailsDialog  # noqa: E402
from minimal_game_launcher.ui.game_editor_dialog import GameEditorDialog  # noqa: E402


class MainWindow(Adw.ApplicationWindow):
    """Responsive library grid with search, sorting, and CRUD actions.

    The window owns presentation state only. Search, sorting, validation, and
    persistence are delegated to ``GameManager``.
    """

    def __init__(self, application: Adw.Application, manager: GameManager) -> None:
        super().__init__(application=application)
        self._manager = manager
        self._active_editor: GameEditorDialog | None = None
        self._active_details: GameDetailsDialog | None = None
        self._active_alert: Adw.AlertDialog | None = None

        self.set_title("Minimal Game Launcher")
        self.set_default_size(1100, 720)
        self.set_content(self._build_ui())
        self._refresh_library()

    def _build_ui(self) -> Gtk.Widget:
        header = Adw.HeaderBar()

        self._search_entry = Gtk.SearchEntry()
        self._search_entry.set_placeholder_text("Search games")
        self._search_entry.set_width_chars(34)
        self._search_entry.set_max_width_chars(48)
        self._search_entry.connect("search-changed", self._on_search_changed)
        header.set_title_widget(self._search_entry)

        self._sort_dropdown = Gtk.DropDown.new_from_strings(
            ["Name", "Recently added"]
        )
        self._sort_dropdown.set_tooltip_text("Sort games")
        self._sort_dropdown.set_valign(Gtk.Align.CENTER)
        self._sort_dropdown.connect("notify::selected", self._on_sort_changed)
        header.pack_end(self._sort_dropdown)

        add_button = Gtk.Button.new_from_icon_name("list-add-symbolic")
        add_button.set_tooltip_text("Add game")
        add_button.add_css_class("suggested-action")
        add_button.connect("clicked", lambda _button: self._open_add_dialog())
        header.pack_end(add_button)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)

        self._stack = Gtk.Stack()
        self._stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._stack.set_transition_duration(120)
        toolbar.set_content(self._stack)

        self._flowbox = Gtk.FlowBox()
        self._flowbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._flowbox.set_activate_on_single_click(False)
        self._flowbox.set_homogeneous(True)
        self._flowbox.set_column_spacing(18)
        self._flowbox.set_row_spacing(18)
        self._flowbox.set_margin_top(24)
        self._flowbox.set_margin_bottom(24)
        self._flowbox.set_margin_start(24)
        self._flowbox.set_margin_end(24)
        self._flowbox.set_valign(Gtk.Align.START)
        self._flowbox.set_max_children_per_line(12)
        self._flowbox.set_min_children_per_line(1)
        self._flowbox.connect("child-activated", self._on_card_activated)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(self._flowbox)
        self._stack.add_named(scroller, "library")

        self._empty_page = Adw.StatusPage()
        self._empty_page.set_hexpand(True)
        self._empty_page.set_vexpand(True)
        self._stack.add_named(self._empty_page, "empty")

        self._toast_overlay = Adw.ToastOverlay()
        self._toast_overlay.set_child(toolbar)
        return self._toast_overlay

    def _on_search_changed(self, _entry: Gtk.SearchEntry) -> None:
        self._refresh_library()

    def _on_sort_changed(self, _dropdown: Gtk.DropDown, _pspec: object) -> None:
        self._refresh_library()

    def _current_sort(self) -> str:
        return SORT_RECENT if self._sort_dropdown.get_selected() == 1 else SORT_NAME

    def _refresh_library(self) -> None:
        query = self._search_entry.get_text()
        try:
            games = self._manager.list_games(query, self._current_sort())
        except Exception as error:  # Keep the window usable after a DB error.
            self._show_error(f"The library could not be loaded: {error}")
            games = []

        self._flowbox.remove_all()
        for game in games:
            card = GameCard(game)
            card.connect("details-requested", self._on_details_requested)
            card.connect("edit-requested", self._on_edit_requested)
            card.connect("remove-requested", self._on_remove_requested)
            self._flowbox.append(card)

        if games:
            self._stack.set_visible_child_name("library")
        else:
            self._populate_empty_state(bool(query.strip()))
            self._stack.set_visible_child_name("empty")

    def _populate_empty_state(self, has_query: bool) -> None:
        if has_query:
            self._empty_page.set_icon_name("system-search-symbolic")
            self._empty_page.set_title("No matching games")
            self._empty_page.set_description(
                "Try a different search term, or clear the current search."
            )
            button = Gtk.Button(label="Clear search")
            button.add_css_class("suggested-action")
            button.set_halign(Gtk.Align.CENTER)
            button.connect("clicked", lambda _button: self._search_entry.set_text(""))
        else:
            self._empty_page.set_icon_name("applications-games-symbolic")
            self._empty_page.set_title("No games yet")
            self._empty_page.set_description(
                "Add a Linux native game or a Windows game running through Wine."
            )
            button = Gtk.Button(label="Add your first game")
            button.add_css_class("suggested-action")
            button.set_halign(Gtk.Align.CENTER)
            button.connect("clicked", lambda _button: self._open_add_dialog())
        self._empty_page.set_child(button)

    def _on_card_activated(self, _flowbox: Gtk.FlowBox, child: Gtk.FlowBoxChild) -> None:
        if isinstance(child, GameCard):
            self._show_details(child.game)

    def _on_details_requested(self, _card: GameCard, game: Game) -> None:
        self._show_details(game)

    def _on_edit_requested(self, _card: GameCard, game: Game) -> None:
        self._open_edit_dialog(game)

    def _on_remove_requested(self, _card: GameCard, game: Game) -> None:
        self._confirm_remove(game)

    def _show_details(self, game: Game) -> None:
        dialog = GameDetailsDialog(game)
        dialog.connect("edit-requested", self._on_details_edit_requested)
        dialog.connect("remove-requested", self._on_details_remove_requested)
        self._active_details = dialog
        dialog.present(self)

    def _on_details_edit_requested(self, dialog: GameDetailsDialog, game: Game) -> None:
        self._active_details = None
        # Let the details dialog finish closing before opening another modal.
        GLib.idle_add(self._open_edit_dialog, game)

    def _on_details_remove_requested(
        self, dialog: GameDetailsDialog, game: Game
    ) -> None:
        self._active_details = None
        GLib.idle_add(self._confirm_remove, game)

    def _open_add_dialog(self) -> None:
        dialog = GameEditorDialog(self, self._manager)
        dialog.connect("saved", self._on_game_saved)
        self._active_editor = dialog
        dialog.present(self)

    def _open_edit_dialog(self, game: Game) -> None:
        dialog = GameEditorDialog(self, self._manager, game)
        dialog.connect("saved", self._on_game_saved)
        self._active_editor = dialog
        dialog.present(self)

    def _on_game_saved(self, _dialog: GameEditorDialog, game: Game) -> None:
        self._active_editor = None
        self._refresh_library()
        self._toast(f"Saved {game.name}")

    def _confirm_remove(self, game: Game) -> None:
        # Removal is intentionally framed as removing a library entry, not
        # deleting game data. GameManager performs no filesystem operation.
        dialog = Adw.AlertDialog(
            heading="Remove from library?",
            body=(
                f"Remove “{game.name}” from the library?\n\n"
                "Only the library record will be deleted. The game installation, "
                "executable, Wine prefix, and cover files will remain on disk. "
                "Cover files shared with other entries are never deleted."
            ),
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("remove", "Remove")
        dialog.set_response_appearance(
            "remove", Adw.ResponseAppearance.DESTRUCTIVE
        )
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        self._active_alert = dialog
        dialog.choose(self, None, self._on_remove_choice, game)

    def _on_remove_choice(
        self, dialog: Adw.AlertDialog, result: object, game: Game
    ) -> None:
        self._active_alert = None
        try:
            response = dialog.choose_finish(result)
        except GLib.Error:
            return
        if response != "remove":
            return

        try:
            self._manager.remove_game(game.id)
        except LauncherError as error:
            self._show_error(str(error))
            return
        except Exception as error:
            self._show_error(f"The game could not be removed: {error}")
            return

        self._refresh_library()
        self._toast(f"Removed {game.name} from the library")

    def _toast(self, message: str) -> None:
        toast = Adw.Toast.new(message)
        toast.set_timeout(3)
        self._toast_overlay.add_toast(toast)

    def _show_error(self, message: str) -> None:
        dialog = Adw.AlertDialog(heading="Something went wrong", body=message)
        dialog.add_response("close", "Close")
        dialog.set_default_response("close")
        dialog.set_close_response("close")
        self._active_alert = dialog
        dialog.choose(self, None, self._on_error_closed)

    def _on_error_closed(
        self, dialog: Adw.AlertDialog, result: object
    ) -> None:
        self._active_alert = None
        try:
            dialog.choose_finish(result)
        except GLib.Error:
            pass
