"""Main application window and game-library presentation."""

from __future__ import annotations

import logging
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from minimal_game_launcher.errors import LauncherError  # noqa: E402
from minimal_game_launcher.models import (  # noqa: E402
    Game,
    InstallerSession,
    InstallerSourceKind,
)
from minimal_game_launcher.services.cover_manager import CoverManager  # noqa: E402
from minimal_game_launcher.services.cleanup import CleanupService  # noqa: E402
from minimal_game_launcher.services.desktop_integration import (  # noqa: E402
    DesktopIntegrationService,
)
from minimal_game_launcher.services.diagnostics import (  # noqa: E402
    DiagnosticsService,
)
from minimal_game_launcher.services.game_manager import (  # noqa: E402
    SORT_NAME,
    SORT_RECENT,
    GameManager,
)
from minimal_game_launcher.services.installer_manager import (  # noqa: E402
    InstallerManager,
)
from minimal_game_launcher.services.launch_manager import (  # noqa: E402
    LaunchManager,
)
from minimal_game_launcher.services.performance_manager import (  # noqa: E402
    PerformanceManager,
)
from minimal_game_launcher.services.steamgriddb import (  # noqa: E402
    SteamGridDBClient,
)
from minimal_game_launcher.services.wine_tools import (  # noqa: E402
    WineToolsService,
)
from minimal_game_launcher.settings import SettingsStore  # noqa: E402
from minimal_game_launcher.ui.cover import DEFAULT_CACHE  # noqa: E402
from minimal_game_launcher.ui.cover_selection_dialog import (  # noqa: E402
    CoverSelectionDialog,
)
from minimal_game_launcher.ui.cleanup_dialog import (  # noqa: E402
    CleanupDialog,
    OrphanCleanupDialog,
)
from minimal_game_launcher.ui.diagnostics_dialog import (  # noqa: E402
    DiagnosticsDialog,
)
from minimal_game_launcher.ui.game_card import GameCard  # noqa: E402
from minimal_game_launcher.ui.game_details_dialog import GameDetailsDialog  # noqa: E402
from minimal_game_launcher.ui.game_editor_dialog import GameEditorDialog  # noqa: E402
from minimal_game_launcher.ui.hidden_games_dialog import (  # noqa: E402
    HiddenGamesDialog,
)
from minimal_game_launcher.ui.installer_dialog import (  # noqa: E402
    InstallerDialog,
    PendingInstallsDialog,
)
from minimal_game_launcher.ui.settings_dialog import SettingsDialog  # noqa: E402
from minimal_game_launcher.ui.setup_wizard import SetupWizard  # noqa: E402
from minimal_game_launcher.ui.theme import ThemeController  # noqa: E402
from minimal_game_launcher.ui.wine_tools_dialog import WineToolsDialog  # noqa: E402


LOGGER = logging.getLogger(__name__)


class MainWindow(Adw.ApplicationWindow):
    """Responsive library grid with search, sorting, launch, and CRUD actions.

    The window owns presentation state only. Search, sorting, validation,
    persistence, and process management are delegated to their services.
    """

    SEARCH_DEBOUNCE_MS = 150

    def __init__(
        self,
        application: Adw.Application,
        manager: GameManager,
        launch_manager: LaunchManager,
        settings_store: SettingsStore,
        performance_manager: PerformanceManager,
        cover_manager: CoverManager,
        steamgriddb: SteamGridDBClient,
        theme_controller: ThemeController,
        *,
        diagnostics: DiagnosticsService,
        installer_manager: InstallerManager,
        cleanup: CleanupService,
        desktop_integration: DesktopIntegrationService | None = None,
        wine_tools: WineToolsService,
        recovered_library: Path | None = None,
    ) -> None:
        super().__init__(application=application)
        self._manager = manager
        self._launch_manager = launch_manager
        self._settings_store = settings_store
        self._performance_manager = performance_manager
        self._cover_manager = cover_manager
        self._steamgriddb = steamgriddb
        self._theme_controller = theme_controller
        self._diagnostics = diagnostics
        self._installer_manager = installer_manager
        self._desktop_integration = desktop_integration
        self._wine_tools = wine_tools
        self._cleanup = cleanup
        self._active_editor: GameEditorDialog | None = None
        self._active_details: GameDetailsDialog | None = None
        self._active_diagnostics: DiagnosticsDialog | None = None
        self._active_installer: InstallerDialog | None = None
        self._active_pending: PendingInstallsDialog | None = None
        self._active_cleanup: CleanupDialog | None = None
        self._active_orphans: OrphanCleanupDialog | None = None
        self._active_settings: SettingsDialog | None = None
        self._active_cover_dialog: CoverSelectionDialog | None = None
        self._active_wine_tools: WineToolsDialog | None = None
        self._active_hidden: HiddenGamesDialog | None = None
        self._active_wizard: SetupWizard | None = None
        self._active_alert: Adw.AlertDialog | None = None
        self._cards_by_id: dict[str, GameCard] = {}
        self._card_games: tuple[Game, ...] = ()
        self._search_source_id = 0

        self.set_title("Minimal Game Launcher")
        self.set_default_size(1100, 720)
        self.set_content(self._build_ui())

        self._launch_manager.connect("launch-started", self._on_launch_started)
        self._launch_manager.connect("launch-finished", self._on_launch_finished)
        self._launch_manager.connect("launch-failed", self._on_launch_failed)
        self._launch_manager.connect("launch-warning", self._on_launch_warning)

        self._refresh_library()
        self._update_pending_button()
        if recovered_library is not None:
            self._show_error(
                "The library database was damaged and could not be read. "
                "It has been moved aside, and a new empty library was created.\n\n"
                f"Damaged file: {recovered_library}\n\n"
                "Game records in the damaged file are not deleted. Nothing "
                "is removed from disk.",
                heading="Library recovered",
            )
        if not self._settings_store.load().setup_completed:
            # The wizard only records preferences; defer it so the window is
            # already visible when the dialog opens.
            GLib.idle_add(self._open_setup_wizard)

    def _build_ui(self) -> Gtk.Widget:
        header = Adw.HeaderBar()

        self._search_entry = Gtk.SearchEntry()
        self._search_entry.set_placeholder_text("Search games")
        self._search_entry.set_width_chars(34)
        self._search_entry.set_max_width_chars(48)
        self._search_entry.connect("search-changed", self._on_search_changed)
        header.set_title_widget(self._search_entry)

        settings_button = Gtk.Button.new_from_icon_name("emblem-system-symbolic")
        settings_button.set_tooltip_text("Settings")
        settings_button.set_valign(Gtk.Align.CENTER)
        settings_button.connect("clicked", lambda _button: self._open_settings())
        header.pack_end(settings_button)

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

        self._pending_button = Gtk.Button.new_from_icon_name(
            "document-open-recent-symbolic"
        )
        self._pending_button.set_tooltip_text("Pending installs")
        self._pending_button.connect(
            "clicked", lambda _button: self._open_pending_installs()
        )
        header.pack_end(self._pending_button)

        install_button = Gtk.MenuButton()
        install_button.set_icon_name("media-optical-symbolic")
        install_button.set_tooltip_text("Install a game")
        install_button.set_valign(Gtk.Align.CENTER)
        install_menu = Gio.Menu()
        install_menu.append("From ISO image…", "win.install-iso")
        install_menu.append(
            "From installer executable…", "win.install-executable"
        )
        install_button.set_menu_model(install_menu)
        header.pack_end(install_button)

        iso_action = Gio.SimpleAction.new("install-iso", None)
        iso_action.connect(
            "activate",
            lambda _action, _params: self._open_installer(),
        )
        self.add_action(iso_action)
        executable_action = Gio.SimpleAction.new("install-executable", None)
        executable_action.connect(
            "activate",
            lambda _action, _params: self._open_installer(
                source_kind=InstallerSourceKind.EXECUTABLE
            ),
        )
        self.add_action(executable_action)

        cleanup_button = Gtk.Button.new_from_icon_name("edit-clear-all-symbolic")
        cleanup_button.set_tooltip_text("Inspect unused managed data")
        cleanup_button.connect("clicked", lambda _button: self._open_orphans())
        header.pack_end(cleanup_button)

        hidden_button = Gtk.Button.new_from_icon_name("view-reveal-symbolic")
        hidden_button.set_tooltip_text("Hidden games")
        hidden_button.connect(
            "clicked", lambda _button: self._open_hidden_games()
        )
        header.pack_end(hidden_button)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)

        self._stack = Gtk.Stack()
        self._stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._stack.set_transition_duration(120)
        toolbar.set_content(self._stack)

        self._flowbox = Gtk.FlowBox()
        self._flowbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._flowbox.set_activate_on_single_click(False)
        self._flowbox.set_homogeneous(False)
        self._flowbox.set_column_spacing(25)
        self._flowbox.set_row_spacing(25)
        self._flowbox.set_margin_top(20)
        self._flowbox.set_margin_bottom(20)
        self._flowbox.set_margin_start(30)
        self._flowbox.set_margin_end(30)
        self._flowbox.set_valign(Gtk.Align.START)
        self._flowbox.set_halign(Gtk.Align.START)
        self._flowbox.set_max_children_per_line(12)
        self._flowbox.set_min_children_per_line(1)
        self._flowbox.connect("child-activated", self._on_card_activated)

        library_content = Adw.Clamp()
        library_content.set_maximum_size(1100)
        library_content.set_tightening_threshold(800)
        library_content.set_child(self._flowbox)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(library_content)
        self._stack.add_named(scroller, "library")

        self._empty_page = Adw.StatusPage()
        self._empty_page.set_hexpand(True)
        self._empty_page.set_vexpand(True)
        self._stack.add_named(self._empty_page, "empty")

        self._toast_overlay = Adw.ToastOverlay()
        self._toast_overlay.set_child(toolbar)
        return self._toast_overlay

    def _on_search_changed(self, _entry: Gtk.SearchEntry) -> None:
        # Debounce so each keystroke does not rebuild every card and cover.
        if self._search_source_id:
            GLib.source_remove(self._search_source_id)
        self._search_source_id = GLib.timeout_add(
            self.SEARCH_DEBOUNCE_MS, self._on_search_debounced
        )

    def _on_search_debounced(self) -> bool:
        self._search_source_id = 0
        self._refresh_library()
        return GLib.SOURCE_REMOVE

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

        # Rebuilding every card re-decodes every visible cover. When the query
        # returns exactly the same records in the same order, keep the existing
        # cards (and their displayed textures) instead.
        if games and tuple(games) == self._card_games:
            self._refresh_card_statuses(games)
            return

        # Permanently removed cards must release their child-widget handlers
        # before leaving the FlowBox, otherwise the handler cycle keeps the
        # card and its textures alive forever.
        for card in self._cards_by_id.values():
            card.dispose()
        self._flowbox.remove_all()
        self._cards_by_id = {}
        DEFAULT_CACHE.prune_dead()
        for game in games:
            # A game left running by a previous session is adopted read-only:
            # the card shows Running and duplicate launches are blocked, but
            # the launcher never signals or reaps the foreign process.
            if (
                not self._launch_manager.is_active(game.id)
                and self._launch_manager.reattach(game)
            ):
                self._toast(f"{game.name} is still running")
            card = GameCard(game)
            card.connect("launch-requested", self._on_launch_requested)
            card.connect("details-requested", self._on_details_requested)
            card.connect(
                "diagnostics-requested", self._on_diagnostics_requested
            )
            card.connect("edit-requested", self._on_edit_requested)
            card.connect("remove-requested", self._on_remove_requested)
            card.connect(
                "wine-tools-requested", self._on_wine_tools_requested
            )
            card.set_status(self._launch_manager.status(game.id))
            self._cards_by_id[game.id] = card
            self._flowbox.append(card)
        self._card_games = tuple(games)

        if games:
            self._stack.set_visible_child_name("library")
        else:
            self._populate_empty_state(bool(query.strip()))
            self._stack.set_visible_child_name("empty")

    def _refresh_card_statuses(self, games: list[Game]) -> None:
        """Update reused cards in place without rebuilding or re-decoding."""
        self._card_games = tuple(games)
        for game in games:
            card = self._cards_by_id.get(game.id)
            if card is not None:
                card.game = game
                card.set_status(self._launch_manager.status(game.id))

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

    def _on_launch_requested(self, _card: GameCard, game: Game) -> None:
        self._launch_game(game)

    def _on_details_requested(self, _card: GameCard, game: Game) -> None:
        self._show_details(game)

    def _on_diagnostics_requested(self, _card: GameCard, game: Game) -> None:
        self._open_diagnostics(game)

    def _on_edit_requested(self, _card: GameCard, game: Game) -> None:
        self._open_edit_dialog(game)

    def _on_remove_requested(self, _card: GameCard, game: Game) -> None:
        self._confirm_remove(game)

    def _on_wine_tools_requested(self, _card: GameCard, game: Game) -> None:
        self._open_wine_tools(game)

    def _launch_game(self, game: Game) -> None:
        if self._launch_manager.is_active(game.id):
            self._set_card_status(game.id, self._launch_manager.status(game.id))
            self._toast(f"{game.name} is already starting or running")
            return

        if not self._launch_manager.launch(game):
            self._toast(f"{game.name} is already starting or running")
            return

        self._set_card_status(game.id, "starting")
        self._toast(f"Starting {game.name}…")

    def _on_launch_started(
        self, _manager: LaunchManager, game_id: str, _pid: int
    ) -> None:
        self._set_card_status(game_id, "running")
        self._toast(f"Launched {self._game_name(game_id)}")

    def _on_launch_finished(
        self, _manager: LaunchManager, game_id: str, exit_code: int
    ) -> None:
        self._set_card_status(game_id, None)
        name = self._game_name(game_id)
        if exit_code == 0:
            self._toast(f"{name} exited")
        else:
            self._toast(f"{name} exited with code {exit_code}")

    def _on_launch_failed(
        self, _manager: LaunchManager, game_id: str, message: str
    ) -> None:
        self._set_card_status(game_id, None)
        self._show_error(message, heading="Game could not start")

    def _on_launch_warning(
        self, _manager: LaunchManager, _game_id: str, message: str
    ) -> None:
        # Performance integrations are optional. A missing wrapper is reported
        # as a longer-lived warning while the game continues to launch.
        self._toast(message, timeout=8)

    def _set_card_status(self, game_id: str, status: str | None) -> None:
        card = self._cards_by_id.get(game_id)
        if card is not None:
            card.set_status(status)

    def _game_name(self, game_id: str) -> str:
        game = self._manager.get_game(game_id)
        return game.name if game is not None else "The game"

    def _show_details(self, game: Game) -> None:
        dialog = GameDetailsDialog(game)
        dialog.connect("launch-requested", self._on_details_launch_requested)
        dialog.connect(
            "diagnostics-requested",
            self._on_details_diagnostics_requested,
        )
        dialog.connect("cover-requested", self._on_details_cover_requested)
        dialog.connect("edit-requested", self._on_details_edit_requested)
        dialog.connect("remove-requested", self._on_details_remove_requested)
        dialog.connect(
            "wine-tools-requested", self._on_details_wine_tools_requested
        )
        dialog.connect("closed", self._on_details_closed)
        self._active_details = dialog
        dialog.present(self)

    def _on_details_closed(self, dialog: GameDetailsDialog) -> None:
        if self._active_details is dialog:
            self._active_details = None

    def _on_details_cover_requested(
        self, dialog: GameDetailsDialog, game: Game
    ) -> None:
        self._active_details = None
        # The cover dialog is modal as well; wait for the details dialog to
        # finish closing so GTK does not stack two dialogs at once.
        GLib.idle_add(self._open_cover_dialog, game)

    def _on_details_launch_requested(
        self, dialog: GameDetailsDialog, game: Game
    ) -> None:
        # Close the modal details view so launch progress and any error dialog
        # are visible over the library grid.
        self._active_details = None
        dialog.close()
        self._launch_game(game)

    def _on_details_edit_requested(self, dialog: GameDetailsDialog, game: Game) -> None:
        self._active_details = None
        # Let the details dialog finish closing before opening another modal.
        GLib.idle_add(self._open_edit_dialog, game)

    def _on_details_diagnostics_requested(
        self, dialog: GameDetailsDialog, game: Game
    ) -> None:
        self._active_details = None
        GLib.idle_add(self._open_diagnostics, game)

    def _on_details_remove_requested(
        self, dialog: GameDetailsDialog, game: Game
    ) -> None:
        self._active_details = None
        GLib.idle_add(self._confirm_remove, game)

    def _on_details_wine_tools_requested(
        self, dialog: GameDetailsDialog, game: Game
    ) -> None:
        self._active_details = None
        GLib.idle_add(self._open_wine_tools, game)

    def _open_cover_dialog(self, game: Game) -> None:
        # Re-read the game so the dialog sees a cover applied since the details
        # view was opened.
        current = self._manager.get_game(game.id)
        if current is None:
            self._toast("This game is no longer in the library")
            return
        dialog = CoverSelectionDialog(
            self,
            current,
            self._cover_manager,
            self._steamgriddb,
            self._settings_store,
        )
        dialog.connect("cover-selected", self._on_cover_selected)
        dialog.connect("cover-cleared", self._on_cover_cleared)
        dialog.connect("closed", self._on_cover_dialog_closed)
        self._active_cover_dialog = dialog
        dialog.present(self)

    def _on_cover_dialog_closed(self, dialog: CoverSelectionDialog) -> None:
        if self._active_cover_dialog is dialog:
            self._active_cover_dialog = None

    def _on_cover_selected(
        self, dialog: CoverSelectionDialog, cover_path: str
    ) -> None:
        if self._active_cover_dialog is dialog:
            self._active_cover_dialog = None
        game = dialog.game
        previous_cover = game.cover_path
        try:
            updated = self._manager.set_cover(game.id, cover_path)
        except LauncherError as error:
            # The dialog already cached the image; do not leave an orphan when
            # the database write fails.
            self._cover_manager.remove_cover(cover_path)
            self._show_error(str(error), heading="Cover could not be saved")
            return
        except Exception as error:
            self._cover_manager.remove_cover(cover_path)
            self._show_error(
                f"The cover could not be saved: {error}",
                heading="Cover could not be saved",
            )
            return

        # The dialog cached a new file and the previous one is removed below.
        # Drop both from the decoded-texture cache so a rebuilt card cannot
        # reuse a texture decoded from an earlier version of the same path.
        DEFAULT_CACHE.invalidate(cover_path)
        if (
            previous_cover
            and previous_cover != updated.cover_path
            and self._cover_manager.is_cached_cover(previous_cover)
        ):
            self._cover_manager.remove_cover(previous_cover)
            DEFAULT_CACHE.invalidate(previous_cover)

        self._refresh_library()
        self._toast(f"Updated cover for {updated.name}")

    def _on_cover_cleared(self, dialog: CoverSelectionDialog) -> None:
        if self._active_cover_dialog is dialog:
            self._active_cover_dialog = None
        game = dialog.game
        previous_cover = game.cover_path
        try:
            updated = self._manager.set_cover(game.id, None)
        except LauncherError as error:
            self._show_error(str(error), heading="Cover could not be removed")
            return
        except Exception as error:
            self._show_error(
                f"The cover could not be removed: {error}",
                heading="Cover could not be removed",
            )
            return

        if previous_cover and self._cover_manager.is_cached_cover(previous_cover):
            self._cover_manager.remove_cover(previous_cover)
            DEFAULT_CACHE.invalidate(previous_cover)

        self._refresh_library()
        self._toast(f"Removed cover for {updated.name}")

    def _open_diagnostics(self, game: Game) -> None:
        # Re-read so diagnostics see the current stored configuration.
        current = self._manager.get_game(game.id)
        if current is None:
            self._toast("This game is no longer in the library")
            return
        dialog = DiagnosticsDialog(self, current, self._diagnostics)
        dialog.connect("closed", self._on_diagnostics_closed)
        self._active_diagnostics = dialog
        dialog.present(self)

    def _on_diagnostics_closed(self, dialog: DiagnosticsDialog) -> None:
        if self._active_diagnostics is dialog:
            self._active_diagnostics = None

    def _open_wine_tools(self, game: Game) -> None:
        # Re-read so the dialog sees the current prefix assignment.
        current = self._manager.get_game(game.id)
        if current is None:
            self._toast("This game is no longer in the library")
            return
        dialog = WineToolsDialog(current, self._wine_tools)
        dialog.connect("started", self._on_wine_tool_started)
        dialog.connect("closed", self._on_wine_tools_closed)
        self._active_wine_tools = dialog
        dialog.present(self)

    def _on_wine_tool_started(
        self, _dialog: WineToolsDialog, message: str
    ) -> None:
        self._toast(message, timeout=8)

    def _on_wine_tools_closed(self, dialog: WineToolsDialog) -> None:
        if self._active_wine_tools is dialog:
            self._active_wine_tools = None

    def _open_hidden_games(self) -> None:
        dialog = HiddenGamesDialog(self._manager)
        dialog.connect("changed", self._on_hidden_games_changed)
        dialog.connect("closed", self._on_hidden_games_closed)
        self._active_hidden = dialog
        dialog.present(self)

    def _on_hidden_games_changed(self, _dialog: HiddenGamesDialog) -> None:
        self._refresh_library()

    def _on_hidden_games_closed(self, dialog: HiddenGamesDialog) -> None:
        if self._active_hidden is dialog:
            self._active_hidden = None

    def _open_setup_wizard(self) -> None:
        if self._active_wizard is not None:
            self._active_wizard.present(self)
            return
        dialog = SetupWizard(
            self._settings_store,
            desktop_integration=self._desktop_integration,
        )
        dialog.connect("finished", self._on_wizard_finished)
        dialog.connect("closed", self._on_wizard_closed)
        self._active_wizard = dialog
        dialog.present(self)

    def _on_wizard_finished(
        self, _dialog: SetupWizard, _settings: object
    ) -> None:
        self._toast("Setup complete")

    def _on_wizard_closed(self, dialog: SetupWizard) -> None:
        if self._active_wizard is dialog:
            self._active_wizard = None

    def _open_installer(
        self,
        session: InstallerSession | None = None,
        *,
        source_kind: InstallerSourceKind = InstallerSourceKind.ISO,
    ) -> None:
        dialog = InstallerDialog(
            self,
            self._installer_manager,
            self._manager,
            self._launch_manager,
            session=session,
            source_kind=source_kind,
        )
        dialog.connect("installed", self._on_installer_installed)
        dialog.connect("closed", self._on_installer_closed)
        self._active_installer = dialog
        dialog.present(self)
        self._update_pending_button()

    def _open_pending_installs(self) -> None:
        dialog = PendingInstallsDialog(
            self._installer_manager,
            is_game_active=self._launch_manager.is_active,
        )
        dialog.connect("resume-requested", self._on_pending_resume_requested)
        dialog.connect("removed", self._on_pending_removed)
        dialog.connect("closed", self._on_pending_closed)
        self._active_pending = dialog
        dialog.present(self)

    def _on_pending_resume_requested(
        self, dialog: PendingInstallsDialog, session: InstallerSession
    ) -> None:
        if self._active_pending is dialog:
            self._active_pending = None
        GLib.idle_add(self._open_installer, session)

    def _on_pending_removed(
        self, _dialog: PendingInstallsDialog, session: InstallerSession
    ) -> None:
        self._refresh_library()
        self._update_pending_button()
        LOGGER.info("Removed pending install %s", session.game_id)

    def _on_pending_closed(self, dialog: PendingInstallsDialog) -> None:
        if self._active_pending is dialog:
            self._active_pending = None

    def _on_installer_installed(
        self, dialog: InstallerDialog, game: Game
    ) -> None:
        if self._active_installer is dialog:
            self._active_installer = None
        self._refresh_library()
        self._update_pending_button()
        self._toast(f"Installed {game.name}")

    def _on_installer_closed(self, dialog: InstallerDialog) -> None:
        if self._active_installer is dialog:
            self._active_installer = None
        self._update_pending_button()

    def _update_pending_button(self) -> None:
        try:
            pending = bool(self._installer_manager.list_sessions())
        except Exception:
            pending = False
        self._pending_button.set_visible(pending)

    def _open_add_dialog(self) -> None:
        dialog = GameEditorDialog(
            self,
            self._manager,
            performance_manager=self._performance_manager,
            cover_manager=self._cover_manager,
        )
        dialog.connect("saved", self._on_game_saved)
        dialog.connect("closed", self._on_editor_closed)
        self._active_editor = dialog
        dialog.present(self)

    def _open_edit_dialog(self, game: Game) -> None:
        dialog = GameEditorDialog(
            self,
            self._manager,
            game,
            performance_manager=self._performance_manager,
            cover_manager=self._cover_manager,
        )
        dialog.connect("saved", self._on_game_saved)
        dialog.connect("closed", self._on_editor_closed)
        self._active_editor = dialog
        dialog.present(self)

    def _on_editor_closed(self, dialog: GameEditorDialog) -> None:
        # Cancel and window close never emit ``saved``; drop the reference so
        # a dismissed editor is not retained.
        if self._active_editor is dialog:
            self._active_editor = None

    def _on_game_saved(self, dialog: GameEditorDialog, game: Game) -> None:
        if self._active_editor is dialog:
            self._active_editor = None
        self._refresh_library()
        self._toast(f"Saved {game.name}")

    def _open_settings(self) -> None:
        dialog = SettingsDialog(
            self,
            self._settings_store,
            self._theme_controller,
            desktop_integration=self._desktop_integration,
        )
        dialog.connect("saved", self._on_settings_saved)
        dialog.connect("closed", self._on_settings_closed)
        self._active_settings = dialog
        dialog.present(self)

    def _on_settings_closed(self, dialog: SettingsDialog) -> None:
        # Theme changes apply immediately, so the dialog can be dismissed
        # without saving; drop the reference so it is not kept alive.
        if self._active_settings is dialog:
            self._active_settings = None

    def _on_settings_saved(
        self, dialog: SettingsDialog, _settings: object
    ) -> None:
        if self._active_settings is dialog:
            self._active_settings = None
        self._toast("Settings saved")

    def _confirm_remove(self, game: Game) -> None:
        # Removal opens a cleanup dialog whose safe default is the existing
        # metadata-only behavior. Destructive choices are opt-in and list the
        # exact verified targets before anything is deleted.
        current = self._manager.get_game(game.id)
        if current is None:
            self._toast("This game is no longer in the library")
            return
        dialog = CleanupDialog(
            self, current, self._cleanup, self._manager
        )
        dialog.connect("finished", self._on_cleanup_finished)
        dialog.connect("closed", self._on_cleanup_closed)
        self._active_cleanup = dialog
        dialog.present(self)

    def _on_cleanup_closed(self, dialog: CleanupDialog) -> None:
        if self._active_cleanup is dialog:
            self._active_cleanup = None

    def _on_cleanup_finished(
        self, dialog: CleanupDialog, _report: object, _record_removed: bool
    ) -> None:
        if self._active_cleanup is dialog:
            self._active_cleanup = None
        self._refresh_library()
        self._update_pending_button()
        self._toast(f"Cleaned up {dialog.game.name}")

    def _open_orphans(self) -> None:
        dialog = OrphanCleanupDialog(self, self._cleanup)
        dialog.connect("finished", self._on_orphans_finished)
        dialog.connect("closed", self._on_orphans_closed)
        self._active_orphans = dialog
        dialog.present(self)

    def _on_orphans_closed(self, dialog: OrphanCleanupDialog) -> None:
        if self._active_orphans is dialog:
            self._active_orphans = None

    def _on_orphans_finished(
        self, dialog: OrphanCleanupDialog, _report: object
    ) -> None:
        if self._active_orphans is dialog:
            self._active_orphans = None
        self._refresh_library()
        self._update_pending_button()

    def _toast(self, message: str, *, timeout: int = 3) -> None:
        toast = Adw.Toast.new(message)
        toast.set_timeout(timeout)
        self._toast_overlay.add_toast(toast)

    def _show_error(
        self, message: str, *, heading: str = "Something went wrong"
    ) -> None:
        dialog = Adw.AlertDialog(heading=heading, body=message)
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
