"""Winecfg and Winetricks dialog for one managed Wine prefix.

Both actions are explicit and modify only a prefix that MGL manages. Winetricks
downloads and installs components, so it asks for confirmation and lists the
requested verbs. The launcher never installs anything on its own.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, GObject, Gtk  # noqa: E402

from minimal_game_launcher.errors import WineToolError  # noqa: E402
from minimal_game_launcher.models import Game  # noqa: E402
from minimal_game_launcher.services.wine_tools import WineToolsService  # noqa: E402
from minimal_game_launcher.ui.lifecycle import DialogSignalOwner  # noqa: E402


class WineToolsDialog(Adw.Dialog, DialogSignalOwner):
    """Open Winecfg or run Winetricks for one Windows game."""

    __gsignals__ = {
        "started": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self, game: Game, service: WineToolsService) -> None:
        super().__init__()
        self._game = game
        self._service = service
        self._active_alert: Adw.AlertDialog | None = None
        self._pending_verbs = ""
        self._availability = service.detect()
        self.set_content_width(560)
        self.set_content_height(520)
        self.set_can_close(True)
        self.set_child(self._build_ui())
        self._init_dialog_lifecycle()

    def _build_ui(self) -> Gtk.Widget:
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="Wine tools"))
        close = Gtk.Button(label="Close")
        close.add_css_class("flat")
        self.connect_owned(close, "clicked", lambda _b: self.close())
        header.pack_start(close)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)

        page = Adw.PreferencesPage()
        toolbar.set_content(page)

        group = Adw.PreferencesGroup(
            title=self._game.name,
            description=(
                "These tools change only this game's MGL-managed Wine prefix. "
                "External prefixes are refused."
            ),
        )
        page.add(group)

        prefix = Adw.ActionRow(title="Wine prefix")
        prefix.set_subtitle(self._game.wine_prefix_path or "Not assigned")
        prefix.set_subtitle_selectable(True)
        if self._game.wine_prefix_shared:
            shared = Adw.ActionRow(
                title="Shared prefix",
                subtitle=(
                    "Changes made here affect every game that uses the shared "
                    "Wine prefix."
                ),
            )
            shared.add_css_class("warning")
            group.add(shared)
        group.add(prefix)

        winecfg = Adw.ActionRow(
            title="Winecfg",
            subtitle=(
                "Opens the Wine configuration window for this prefix."
                if self._availability.wine
                else "Wine was not found in PATH."
            ),
        )
        winecfg_button = Gtk.Button(label="Open")
        winecfg_button.set_valign(Gtk.Align.CENTER)
        winecfg_button.set_sensitive(self._availability.wine is not None)
        self.connect_owned(
            winecfg_button, "clicked", self._on_winecfg_clicked
        )
        winecfg.add_suffix(winecfg_button)
        group.add(winecfg)

        winetricks = Adw.ActionRow(
            title="Winetricks",
            subtitle=(
                "Downloads and installs components into this prefix. Requires "
                "network access and can take several minutes."
                if self._availability.winetricks_available
                else "Winetricks was not found in PATH."
            ),
        )
        group.add(winetricks)

        self._verbs_row = Adw.EntryRow(
            title="Winetricks verbs (space separated)"
        )
        self._verbs_row.set_text("vcrun2019")
        self._verbs_row.set_tooltip_text(
            "For example: vcrun2019 dotnet48 d3dx9"
        )
        run = Gtk.Button(label="Run")
        run.set_valign(Gtk.Align.CENTER)
        run.set_sensitive(self._availability.winetricks_available)
        self.connect_owned(run, "clicked", self._on_winetricks_clicked)
        self._verbs_row.add_suffix(run)
        group.add(self._verbs_row)

        note = Adw.ActionRow(
            title="Output",
            subtitle=(
                "Tool output is appended to a log under the launcher state "
                "directory. The launcher does not wait for the tool, so it "
                "cannot report whether the tool succeeded."
            ),
        )
        note.set_subtitle_selectable(True)
        group.add(note)
        return toolbar

    def _on_winecfg_clicked(self, _button: Gtk.Button) -> None:
        try:
            result = self._service.run_winecfg(self._game)
        except WineToolError as error:
            self._show_error(str(error))
            return
        self.emit("started", f"Winecfg started for {self._game.name}")
        if result.log_path is not None:
            self.emit("started", f"Winecfg output: {result.log_path}")

    def _on_winetricks_clicked(self, _button: Gtk.Button) -> None:
        verbs = self._verbs_row.get_text().strip()
        if not verbs:
            self._show_error("Enter at least one Winetricks verb.")
            return
        self._pending_verbs = verbs
        dialog = Adw.AlertDialog(
            heading="Run Winetricks?",
            body=(
                f"This will download and install components into the managed "
                f"Wine prefix for {self._game.name}:\n\n{verbs}\n\n"
                "Only continue if you trust these components. MGL does not "
                "install dependencies automatically."
            ),
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("run", "Run Winetricks")
        dialog.set_response_appearance(
            "run", Adw.ResponseAppearance.SUGGESTED
        )
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        self._active_alert = dialog
        dialog.choose(self, None, self._on_confirm)

    def _on_confirm(
        self, dialog: Adw.AlertDialog, result: object
    ) -> None:
        self._active_alert = None
        try:
            response = dialog.choose_finish(result)
        except GLib.Error:
            return
        if response != "run":
            return
        try:
            result = self._service.run_winetricks(
                self._game, self._pending_verbs
            )
        except WineToolError as error:
            self._show_error(str(error))
            return
        self.emit("started", f"Winetricks started for {self._game.name}")
        if result.log_path is not None:
            self.emit("started", f"Winetricks output: {result.log_path}")

    def _show_error(self, message: str) -> None:
        dialog = Adw.AlertDialog(heading="Wine tool error", body=message)
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

    def _release_dialog_references(self) -> None:
        self._active_alert = None
        self._verbs_row = None
