"""Launcher settings dialog for Wine and optional online cover search.

Windows games can use Wine from ``PATH`` or a user-selected custom executable.
File selection uses the native GTK file chooser. The dialog validates the
custom path's existence and execute permission, then persists only settings;
no Wine process is started from here.

The optional SteamGridDB API key is entered in a password row and stored in a
separate user-private file (mode ``0600``). Leaving it empty removes the file.
The key is never written to the JSON settings file or source code.
"""

from __future__ import annotations

import os
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib, GObject, Gtk  # noqa: E402

from minimal_game_launcher.settings import (  # noqa: E402
    LauncherSettings,
    SettingsStore,
    WineSource,
)


class SettingsDialog(Adw.Dialog):
    """Edit and save the Wine executable preference."""

    __gsignals__ = {
        "saved": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(
        self, parent_window: Gtk.Window, settings_store: SettingsStore
    ) -> None:
        super().__init__()
        self._parent_window = parent_window
        self._settings_store = settings_store
        current = settings_store.load()
        self._wine_source = current.wine_source
        self._custom_wine_path = current.custom_wine_path
        self._active_file_dialog: Gtk.FileDialog | None = None
        self._active_error: Adw.AlertDialog | None = None

        self.set_content_width(600)
        self.set_content_height(560)
        self.set_can_close(True)
        self.set_child(self._build_ui())
        self._refresh_custom_row()

    def _build_ui(self) -> Gtk.Widget:
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="Settings"))

        cancel = Gtk.Button(label="Cancel")
        cancel.add_css_class("flat")
        cancel.connect("clicked", lambda _button: self.close())
        header.pack_start(cancel)

        save = Gtk.Button(label="Save")
        save.add_css_class("suggested-action")
        save.connect("clicked", self._on_save_clicked)
        header.pack_end(save)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)

        page = Adw.PreferencesPage()
        toolbar.set_content(page)

        wine_group = Adw.PreferencesGroup(
            title="Wine",
            description=(
                "Wine is used only for Windows games. Each game has its own "
                "prefix, created on its first launch."
            ),
        )
        page.add(wine_group)

        self._source_row = Adw.ComboRow(title="Wine executable")
        self._source_row.set_model(
            Gtk.StringList.new(["System Wine (PATH)", "Custom executable"])
        )
        self._source_row.set_selected(
            1 if self._wine_source is WineSource.CUSTOM else 0
        )
        self._source_row.connect("notify::selected", self._on_source_changed)
        wine_group.add(self._source_row)

        self._custom_row = Adw.ActionRow(title="Custom Wine executable")
        self._custom_row.set_subtitle_selectable(True)
        self._custom_row.set_activatable(True)
        self._custom_row.connect("activated", self._choose_custom_wine)

        choose = Gtk.Button.new_from_icon_name("folder-open-symbolic")
        choose.set_tooltip_text("Choose custom Wine executable")
        choose.set_valign(Gtk.Align.CENTER)
        choose.connect("clicked", self._choose_custom_wine)
        self._custom_row.add_suffix(choose)
        wine_group.add(self._custom_row)

        cover_group = Adw.PreferencesGroup(
            title="Online cover search",
            description=(
                "Optional. A SteamGridDB key enables cover searches from the "
                "cover dialog. The key is stored in a private file readable "
                "only by your user. Only the search text is sent to "
                "SteamGridDB; executable paths and Wine prefixes never are."
            ),
        )
        page.add(cover_group)

        self._api_key_row = Adw.PasswordEntryRow(title="SteamGridDB API key")
        self._api_key_row.set_text(settings_store.load_api_key() or "")
        cover_group.add(self._api_key_row)

        return toolbar

    def _refresh_custom_row(self) -> None:
        custom = self._wine_source is WineSource.CUSTOM
        self._custom_row.set_sensitive(custom)
        self._custom_row.set_subtitle(
            self._custom_wine_path or "No custom executable selected"
        )

    def _on_source_changed(self, _row: Adw.ComboRow, _pspec: object) -> None:
        self._wine_source = (
            WineSource.CUSTOM
            if self._source_row.get_selected() == 1
            else WineSource.SYSTEM
        )
        self._refresh_custom_row()

    def _set_initial_folder(self, dialog: Gtk.FileDialog) -> None:
        if not self._custom_wine_path:
            return
        path = Path(self._custom_wine_path).expanduser()
        folder = path if path.is_dir() else path.parent
        if folder.is_dir():
            dialog.set_initial_folder(Gio.File.new_for_path(str(folder)))

    def _choose_custom_wine(self, _widget: Gtk.Widget) -> None:
        dialog = Gtk.FileDialog(title="Select Wine executable", modal=True)
        self._set_initial_folder(dialog)
        self._active_file_dialog = dialog
        dialog.open(self._parent_window, None, self._on_custom_wine_selected)

    def _on_custom_wine_selected(
        self, dialog: Gtk.FileDialog, result: Gio.AsyncResult
    ) -> None:
        self._active_file_dialog = None
        try:
            selected = dialog.open_finish(result)
        except GLib.Error as error:
            if not error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                self._show_error(error.message)
            return

        path = selected.get_path()
        if not path:
            self._show_error("Only local Wine executables can be selected.")
            return
        if not Path(path).is_file():
            self._show_error(f"The selected Wine executable was not found: {path}")
            return
        if not os.access(path, os.X_OK):
            self._show_error(
                f"The selected Wine executable is not executable: {path}\n\n"
                "Grant execute permission to the file and try again. The "
                "launcher never changes file permissions automatically."
            )
            return

        self._custom_wine_path = path
        self._wine_source = WineSource.CUSTOM
        self._source_row.set_selected(1)
        self._refresh_custom_row()

    def _on_save_clicked(self, _button: Gtk.Button) -> None:
        if self._wine_source is WineSource.CUSTOM:
            if not self._custom_wine_path:
                self._show_error(
                    "Select a custom Wine executable, or choose System Wine."
                )
                return
            candidate = Path(self._custom_wine_path).expanduser()
            if not candidate.is_file():
                self._show_error(
                    f"The custom Wine executable was not found: {candidate}"
                )
                return
            if not os.access(candidate, os.X_OK):
                self._show_error(
                    f"The custom Wine executable is not executable: {candidate}"
                )
                return
            custom_path = str(candidate)
        else:
            custom_path = self._custom_wine_path

        settings = LauncherSettings(
            wine_source=self._wine_source,
            custom_wine_path=custom_path,
        )
        try:
            self._settings_store.save_api_key(self._api_key_row.get_text())
            self._settings_store.save(settings)
        except OSError as error:
            self._show_error(f"Settings could not be saved: {error}")
            return

        self.emit("saved", settings)
        self.close()

    def _show_error(self, message: str) -> None:
        """Show a short validation message without a Python traceback."""
        dialog = Adw.AlertDialog(heading="Cannot save settings", body=message)
        dialog.add_response("close", "Close")
        dialog.set_default_response("close")
        dialog.set_close_response("close")
        self._active_error = dialog
        dialog.choose(self, None, self._on_error_closed)

    def _on_error_closed(
        self, dialog: Adw.AlertDialog, result: Gio.AsyncResult
    ) -> None:
        self._active_error = None
        try:
            dialog.choose_finish(result)
        except GLib.Error:
            pass
