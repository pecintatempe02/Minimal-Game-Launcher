"""Launcher settings dialog for Wine, appearance, and online cover search.

Windows games can use Wine from ``PATH`` or a user-selected custom executable.
File selection uses the native GTK file chooser. The dialog validates the
custom path's existence and execute permission, then persists only settings;
no Wine process is started from here.

Theme and appearance choices apply immediately and are saved to ``themes.ini``
on selection; they do not rebuild the library or reload covers. If the theme
file cannot be written the choice still applies for the session and the error
is reported without dismissing the dialog.

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
from minimal_game_launcher.themes import Appearance  # noqa: E402
from minimal_game_launcher.ui.theme import ThemeController  # noqa: E402

_APPEARANCE_LABELS = ("System", "Dark", "Light")
_APPEARANCE_MODES = (Appearance.SYSTEM, Appearance.DARK, Appearance.LIGHT)


class SettingsDialog(Adw.Dialog):
    """Edit and save the Wine executable preference and the active theme.

    Theme and appearance apply and persist as soon as they are selected, so
    ``Close`` only dismisses the dialog; ``Save`` still stores Wine settings.
    The dialog follows external theme changes while it is open and stops
    listening once it is closed.
    """

    __gsignals__ = {
        "saved": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(
        self,
        parent_window: Gtk.Window,
        settings_store: SettingsStore,
        theme_controller: ThemeController,
    ) -> None:
        super().__init__()
        self._parent_window = parent_window
        self._settings_store = settings_store
        self._theme_controller = theme_controller
        current = settings_store.load()
        self._wine_source = current.wine_source
        self._custom_wine_path = current.custom_wine_path
        self._theme_ids: list[str] = []
        self._appearance_index: int | None = None
        #: Set while the rows are being repopulated, so writing the selection
        #: back into the controller cannot echo as a user change.
        self._syncing = False
        self._theme_handler: int | None = None
        self._active_file_dialog: Gtk.FileDialog | None = None
        self._active_error: Adw.AlertDialog | None = None

        self.set_content_width(600)
        self.set_content_height(560)
        self.set_can_close(True)
        self.set_child(self._build_ui())
        self._refresh_custom_row()
        self._refresh_theme_rows()
        self._theme_handler = theme_controller.connect(
            "changed", self._on_theme_controller_changed
        )
        self.connect("closed", self._on_closed)

    def _build_ui(self) -> Gtk.Widget:
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="Settings"))

        # Theme and appearance are already saved by the time this is pressed.
        close = Gtk.Button(label="Close")
        close.add_css_class("flat")
        close.connect("clicked", lambda _button: self.close())
        header.pack_start(close)

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

        appearance_group = Adw.PreferencesGroup(
            title="Appearance",
            description=(
                "Themes are read from themes.ini in the launcher's config "
                "directory. Choices apply immediately and are saved on "
                "selection."
            ),
        )
        page.add(appearance_group)

        self._theme_row = Adw.ComboRow(title="Theme")
        self._theme_row.set_model(Gtk.StringList.new([]))
        self._theme_row.connect("notify::selected", self._on_theme_changed)
        appearance_group.add(self._theme_row)

        self._appearance_row = Adw.ComboRow(title="Appearance")
        self._appearance_row.set_model(
            Gtk.StringList.new(list(_APPEARANCE_LABELS))
        )
        self._appearance_row.set_subtitle(
            "System follows the desktop setting when one is available"
        )
        self._appearance_row.connect(
            "notify::selected", self._on_appearance_changed
        )
        appearance_group.add(self._appearance_row)

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
        self._api_key_row.set_text(
            self._settings_store.load_stored_api_key() or ""
        )
        cover_group.add(self._api_key_row)

        return toolbar

    def _refresh_theme_rows(self) -> None:
        """Repopulate the theme and appearance rows from the controller.

        Runs under ``_syncing`` so the resulting ``notify::selected`` signals
        are ignored: they describe a refresh, not a user choice, and must never
        be written back over an externally selected theme.
        """
        config = self._theme_controller.config
        previous = self._syncing
        self._syncing = True
        try:
            theme_ids = sorted(
                config.themes,
                key=lambda theme_id: config.themes[theme_id].name.casefold(),
            )
            # Only write to the rows when they are actually out of date. The
            # refresh runs from inside a ``notify::selected`` emission when a
            # pick echoes back through the controller, and re-setting the model
            # or the selection there re-enters AdwComboRow.
            if theme_ids != self._theme_ids:
                self._theme_ids = theme_ids
                self._theme_row.set_model(
                    Gtk.StringList.new(
                        [
                            config.themes[theme_id].name
                            for theme_id in theme_ids
                        ]
                    )
                )
            index = (
                theme_ids.index(config.active)
                if config.active in theme_ids
                else Gtk.INVALID_LIST_POSITION
            )
            if self._theme_row.get_selected() != index:
                self._theme_row.set_selected(index)

            self._appearance_index = _APPEARANCE_MODES.index(config.appearance)
            if self._appearance_row.get_selected() != self._appearance_index:
                self._appearance_row.set_selected(self._appearance_index)
        finally:
            self._syncing = previous

    def _on_theme_controller_changed(self, _controller: ThemeController) -> None:
        """Follow a theme change made outside this dialog."""
        self._refresh_theme_rows()

    def _on_closed(self, _dialog: Adw.Dialog) -> None:
        """Stop listening to the controller once the dialog is gone."""
        if self._theme_handler is not None:
            self._theme_controller.disconnect(self._theme_handler)
            self._theme_handler = None

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

    def _on_theme_changed(self, row: Adw.ComboRow, _pspec: object) -> None:
        if self._syncing:
            return
        index = row.get_selected()
        if not 0 <= index < len(self._theme_ids):
            return
        # The change signal may fire again inside set_active_theme; the guard
        # keeps this from recursing if a future implementation does so.
        self._syncing = True
        try:
            try:
                self._theme_controller.set_active_theme(self._theme_ids[index])
            except OSError as error:
                self._show_error(
                    "The theme was applied for this session but could not be "
                    f"saved: {error}"
                )
        finally:
            self._syncing = False

    def _on_appearance_changed(self, row: Adw.ComboRow, _pspec: object) -> None:
        if self._syncing:
            return
        index = row.get_selected()
        if not 0 <= index < len(_APPEARANCE_MODES):
            return
        self._syncing = True
        try:
            try:
                self._theme_controller.set_appearance(_APPEARANCE_MODES[index])
            except OSError as error:
                self._show_error(
                    "The appearance was applied for this session but could not "
                    f"be saved: {error}"
                )
        finally:
            self._syncing = False

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
