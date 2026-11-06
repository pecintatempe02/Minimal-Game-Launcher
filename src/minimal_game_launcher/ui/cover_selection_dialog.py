"""Cover selection dialog for local images and optional SteamGridDB search.

Network work (search, thumbnail preview, and the selected cover download) runs
on daemon worker threads and reports back through ``GLib.idle_add`` so the GTK
main loop never blocks. The dialog only emits a cached cover path; the caller
updates SQLite and removes the previous cached file.

SteamGridDB is optional and never queried automatically. The game name is the
only value sent to the provider. Applying a result is an explicit action, and
manual selections copy the file into the cache only when Apply is pressed.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib, GObject, Gtk  # noqa: E402

from minimal_game_launcher.errors import LauncherError  # noqa: E402
from minimal_game_launcher.models import Game  # noqa: E402
from minimal_game_launcher.services.cover_manager import CoverManager  # noqa: E402
from minimal_game_launcher.services.steamgriddb import (  # noqa: E402
    CoverSearchResult,
    SteamGridDBClient,
)
from minimal_game_launcher.settings import SettingsStore  # noqa: E402
from minimal_game_launcher.ui.lifecycle import DialogSignalOwner  # noqa: E402
from minimal_game_launcher.ui.cover import (  # noqa: E402
    load_cover_texture,
    load_cover_texture_from_bytes,
)

LOGGER = logging.getLogger(__name__)

_IMAGE_PATTERNS = ("*.png", "*.jpg", "*.jpeg", "*.webp")


class CoverSelectionDialog(Adw.Dialog, DialogSignalOwner):
    """Choose a cached cover from a local file or an online search result."""

    __gsignals__ = {
        "cover-selected": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "cover-cleared": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(
        self,
        parent_window: Gtk.Window,
        game: Game,
        cover_manager: CoverManager,
        steamgriddb: SteamGridDBClient,
        settings_store: SettingsStore,
    ) -> None:
        super().__init__()
        self._parent_window = parent_window
        self._game = game
        self._cover_manager = cover_manager
        self._steamgriddb = steamgriddb
        self._settings_store = settings_store

        self._selected_local: Path | None = None
        self._selected_result: CoverSearchResult | None = None
        self._results_by_row: dict[Gtk.ListBoxRow, CoverSearchResult] = {}
        self._busy = False
        self._search_token = 0
        self._preview_token = 0
        self._active_file_dialog: Gtk.FileDialog | None = None
        self._active_error: Adw.AlertDialog | None = None

        self.set_content_width(780)
        self.set_content_height(740)
        self.set_can_close(True)
        self.set_child(self._build_ui())
        self._refresh_selection_state()
        self._init_dialog_lifecycle()

    @property
    def game(self) -> Game:
        """Return the game whose cover is being edited."""
        return self._game

    def _build_ui(self) -> Gtk.Widget:
        header = Adw.HeaderBar()
        header.set_title_widget(
            Adw.WindowTitle(
                title="Select cover", subtitle=self._game.name
            )
        )

        cancel = Gtk.Button(label="Cancel")
        cancel.add_css_class("flat")
        self.connect_owned(cancel, "clicked", self._on_cancel_clicked)
        header.pack_start(cancel)

        self._apply_button = Gtk.Button(label="Apply")
        self._apply_button.add_css_class("suggested-action")
        self.connect_owned(
            self._apply_button, "clicked", self._on_apply_clicked
        )
        header.pack_end(self._apply_button)

        self._remove_button = Gtk.Button(label="Remove")
        self._remove_button.add_css_class("destructive-action")
        self._remove_button.set_tooltip_text("Remove the current cover")
        self._remove_button.set_visible(bool(self._game.cover_path))
        self.connect_owned(
            self._remove_button, "clicked", self._on_remove_clicked
        )
        header.pack_end(self._remove_button)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)

        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=18)
        content.set_margin_top(18)
        content.set_margin_bottom(18)
        content.set_margin_start(18)
        content.set_margin_end(18)
        content.set_hexpand(True)
        content.set_vexpand(True)

        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        left.set_hexpand(True)
        content.append(left)

        manual_button = Gtk.Button(label="Choose a local image…")
        manual_button.set_halign(Gtk.Align.START)
        self.connect_owned(manual_button, "clicked", self._choose_local_image)
        left.append(manual_button)
        self._local_button = manual_button

        search_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._search_entry = Gtk.SearchEntry()
        self._search_entry.set_placeholder_text(
            "Search SteamGridDB by game name"
        )
        self._search_entry.set_text(self._game.name)
        self._search_entry.set_hexpand(True)
        self.connect_owned(
            self._search_entry, "activate", self._on_search_activated
        )
        search_row.append(self._search_entry)

        self._search_button = Gtk.Button(label="Search")
        self.connect_owned(
            self._search_button, "clicked", self._on_search_clicked
        )
        search_row.append(self._search_button)
        left.append(search_row)

        privacy_hint = Gtk.Label(
            label=(
                "Online search is optional. Only the text in the search field "
                "is sent to SteamGridDB; executable paths and Wine prefixes "
                "are never uploaded."
            ),
            xalign=0.0,
            wrap=True,
        )
        privacy_hint.add_css_class("dim-label")
        left.append(privacy_hint)

        status_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._spinner = Gtk.Spinner()
        self._spinner.set_visible(False)
        status_row.append(self._spinner)
        self._status_label = Gtk.Label(xalign=0.0, wrap=True)
        self._status_label.set_hexpand(True)
        status_row.append(self._status_label)
        left.append(status_row)

        self._results = Gtk.ListBox()
        self._results.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._results.set_activate_on_single_click(True)
        self.connect_owned(
            self._results, "row-selected", self._on_result_selected
        )
        results_scroller = Gtk.ScrolledWindow()
        results_scroller.set_policy(
            Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC
        )
        results_scroller.set_vexpand(True)
        results_scroller.set_min_content_height(260)
        results_scroller.set_child(self._results)
        left.append(results_scroller)

        preview_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        preview_box.set_size_request(260, -1)
        preview_title = Gtk.Label(label="Preview", xalign=0.0)
        preview_title.add_css_class("heading")
        preview_box.append(preview_title)

        self._preview_stack = Gtk.Stack()
        self._preview_stack.set_vexpand(True)
        self._preview_stack.set_transition_type(
            Gtk.StackTransitionType.CROSSFADE
        )
        empty_preview = Gtk.Label(
            label="Select a local image or a search result to preview it.",
            wrap=True,
            justify=Gtk.Justification.CENTER,
        )
        empty_preview.add_css_class("dim-label")
        self._preview_stack.add_named(empty_preview, "empty")

        self._preview_picture = Gtk.Picture()
        self._preview_picture.set_can_shrink(True)
        self._preview_picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        self._preview_picture.set_hexpand(False)
        self._preview_picture.set_vexpand(False)
        self._preview_stack.add_named(self._preview_picture, "preview")
        self._preview_stack.set_visible_child_name("empty")
        preview_box.append(self._preview_stack)

        content.append(preview_box)
        toolbar.set_content(content)
        return toolbar

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._spinner.set_visible(busy)
        if busy:
            self._spinner.start()
        else:
            self._spinner.stop()
        self._search_entry.set_sensitive(not busy)
        self._search_button.set_sensitive(not busy)
        self._local_button.set_sensitive(not busy)
        self._refresh_selection_state()

    def _refresh_selection_state(self) -> None:
        has_selection = (
            self._selected_local is not None or self._selected_result is not None
        )
        self._apply_button.set_sensitive(has_selection and not self._busy)

    def _set_status(self, message: str, *, error: bool = False) -> None:
        self._status_label.set_text(message)
        if error:
            self._status_label.add_css_class("error")
        else:
            self._status_label.remove_css_class("error")

    def _set_preview(self, texture, status: str | None = None) -> None:
        if texture is None:
            self._preview_picture.set_paintable(None)
            self._preview_stack.set_visible_child_name("empty")
        else:
            self._preview_picture.set_paintable(texture)
            self._preview_stack.set_visible_child_name("preview")
        if status is not None:
            self._set_status(status)

    def _clear_results(self) -> None:
        self._results.unselect_all()
        for row in list(self._results_by_row):
            self._results.remove(row)
        self._results_by_row.clear()
        self._selected_result = None
        self._refresh_selection_state()

    def _on_cancel_clicked(self, _button: Gtk.Button) -> None:
        self.close()

    def _release_dialog_references(self) -> None:
        """Drop preview, result, and callback references after closing."""
        # Late worker callbacks compare these tokens and stop touching widgets.
        self._search_token += 1
        self._preview_token += 1
        self._results_by_row.clear()
        self._selected_local = None
        self._selected_result = None
        self._active_file_dialog = None
        self._active_error = None
        self._preview_picture.set_paintable(None)

    def _on_search_activated(self, _entry: Gtk.SearchEntry) -> None:
        self._start_search()

    def _on_search_clicked(self, _button: Gtk.Button) -> None:
        self._start_search()

    def _start_search(self) -> None:
        if self._busy:
            return
        query = self._search_entry.get_text().strip()
        if not query:
            self._set_status("Enter a game name to search.", error=True)
            return

        api_key = self._settings_store.load_api_key()
        if not api_key:
            self._set_status(
                "Online cover search is not configured. Add a SteamGridDB "
                "API key in Settings, or choose a local image.",
                error=True,
            )
            return

        self._search_token += 1
        token = self._search_token
        # Invalidate any preview that was still loading before the results are
        # replaced; a late preview must not update the new result list.
        self._preview_token += 1
        self._clear_results()
        self._set_preview(None)
        self._set_status("Searching SteamGridDB…")
        self._set_busy(True)

        def work() -> list[CoverSearchResult]:
            return self._steamgriddb.search_covers(query, api_key)

        self._run_async(
            work,
            lambda results: self._finish_search(token, results),
            self._finish_async_error,
        )

    def _finish_search(
        self, token: int, results: list[CoverSearchResult]
    ) -> bool:
        if token != self._search_token:
            return GLib.SOURCE_REMOVE
        self._set_busy(False)
        self._results_by_row.clear()
        for result in results:
            row = Gtk.ListBoxRow()
            box = Gtk.Box(
                orientation=Gtk.Orientation.HORIZONTAL, spacing=12
            )
            box.set_margin_top(8)
            box.set_margin_bottom(8)
            box.set_margin_start(8)
            box.set_margin_end(8)

            title = Gtk.Label(label=result.game_name, xalign=0.0)
            title.set_hexpand(True)
            box.append(title)

            dimensions = Gtk.Label(label=result.dimensions, xalign=1.0)
            dimensions.add_css_class("dim-label")
            box.append(dimensions)
            row.set_child(box)
            row.set_tooltip_text("Select to preview this cover")
            self._results.append(row)
            self._results_by_row[row] = result

        if not results:
            self._set_status(
                "No covers were found for that name. Try a different search."
            )
        else:
            self._set_status(
                f"{len(results)} cover(s) found. Select one to preview it."
            )
        return GLib.SOURCE_REMOVE

    def _on_result_selected(
        self, _listbox: Gtk.ListBox, row: Gtk.ListBoxRow | None
    ) -> None:
        if row is None or row not in self._results_by_row:
            return
        result = self._results_by_row[row]
        self._selected_result = result
        self._selected_local = None
        self._refresh_selection_state()

        self._preview_token += 1
        token = self._preview_token
        self._set_status(f"Loading preview for {result.game_name}…")

        def work() -> tuple[bytes, str]:
            return self._steamgriddb.fetch_image(result.thumb_url)

        def success(payload: tuple[bytes, str]) -> None:
            if token != self._preview_token:
                return
            data, _content_type = payload
            texture = load_cover_texture_from_bytes(data)
            if texture is None:
                self._set_status(
                    "The selected cover preview could not be decoded.",
                    error=True,
                )
                return
            self._set_preview(
                texture,
                f"Selected {result.dimensions} cover for {result.game_name}.",
            )

        def failure(message: str) -> None:
            # Preview failures must not clear the busy state of an unrelated
            # search or download that started after this preview.
            if token != self._preview_token:
                return
            self._set_status(message, error=True)
            LOGGER.warning("Cover preview failed: %s", message)

        self._run_async(work, success, failure)

    def _choose_local_image(self, _button: Gtk.Button) -> None:
        if self._busy:
            return
        dialog = Gtk.FileDialog(
            title="Select cover image", modal=True
        )
        file_filter = Gtk.FileFilter()
        file_filter.set_name("Images (PNG, JPEG, WebP)")
        for pattern in _IMAGE_PATTERNS:
            file_filter.add_pattern(pattern)
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(file_filter)
        dialog.set_filters(filters)
        dialog.set_default_filter(file_filter)
        if self._game.cover_path:
            try:
                initial = Path(self._game.cover_path).expanduser()
                folder = initial if initial.is_dir() else initial.parent
                if folder.is_dir():
                    dialog.set_initial_folder(
                        Gio.File.new_for_path(str(folder))
                    )
            except (OSError, RuntimeError):
                pass
        self._active_file_dialog = dialog
        dialog.open(self._parent_window, None, self._on_local_selected)

    def _on_local_selected(
        self, dialog: Gtk.FileDialog, result: Gio.AsyncResult
    ) -> None:
        self._active_file_dialog = None
        try:
            selected = dialog.open_finish(result)
        except GLib.Error as error:
            if not error.matches(
                Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED
            ):
                self._set_status(error.message, error=True)
            return

        path = selected.get_path()
        if not path:
            self._set_status(
                "Only local image files can be selected.", error=True
            )
            return

        try:
            info = self._cover_manager.inspect_file(path)
        except LauncherError as error:
            self._set_status(str(error), error=True)
            return

        texture = load_cover_texture(path, 360, 480)
        if texture is None:
            self._set_status(
                "The selected cover could not be previewed.", error=True
            )
            return

        self._selected_local = Path(path)
        self._selected_result = None
        self._results.unselect_all()
        self._preview_token += 1
        self._set_preview(
            texture,
            f"Selected local image ({info.width}×{info.height}). "
            "Apply copies it into the launcher cover cache.",
        )
        self._refresh_selection_state()

    def _on_apply_clicked(self, _button: Gtk.Button) -> None:
        if self._busy:
            return

        if self._selected_local is not None:
            try:
                cached = self._cover_manager.import_local(
                    self._game.id, self._selected_local
                )
            except LauncherError as error:
                self._set_status(str(error), error=True)
                return
            self.emit("cover-selected", cached)
            self.close()
            return

        result = self._selected_result
        if result is None:
            return

        self._set_busy(True)
        self._set_status("Downloading the selected cover…")

        def work() -> str:
            data, content_type = self._steamgriddb.fetch_image(result.url)
            return self._cover_manager.store_download(
                self._game.id, data, content_type
            )

        def success(cached: str) -> None:
            self.emit("cover-selected", cached)
            self.close()

        self._run_async(work, success, self._finish_async_error)

    def _on_remove_clicked(self, _button: Gtk.Button) -> None:
        self.emit("cover-cleared")
        self.close()

    def _finish_async_error(self, message: str) -> None:
        self._set_busy(False)
        self._set_status(message, error=True)
        LOGGER.warning("Cover operation failed: %s", message)

    def _run_async(self, work, on_success, on_error) -> None:
        """Run blocking work off the GTK thread and marshal the result back."""
        def runner() -> None:
            try:
                value = work()
            except LauncherError as error:
                GLib.idle_add(on_error, str(error))
            except Exception:
                LOGGER.exception("Unexpected cover operation failure")
                GLib.idle_add(
                    on_error,
                    "The cover operation failed unexpectedly. See the "
                    "launcher log for details.",
                )
            else:
                GLib.idle_add(on_success, value)

        threading.Thread(
            target=runner,
            name="cover-request",
            daemon=True,
        ).start()
