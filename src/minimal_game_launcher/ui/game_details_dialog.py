"""Read-only game details dialog with edit and removal actions."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GObject, Gtk  # noqa: E402

from minimal_game_launcher.models import Game, Platform  # noqa: E402
from minimal_game_launcher.ui.lifecycle import DialogSignalOwner  # noqa: E402


class GameDetailsDialog(Adw.Dialog, DialogSignalOwner):
    """Show all stored metadata without modifying it.

    Edit and remove requests are emitted to the main window, which owns the
    corresponding dialogs and confirmation flow. Closing the dialog
    disconnects its own handlers and drops its widget references so it can be
    garbage-collected.
    """

    __gsignals__ = {
        "launch-requested": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "diagnostics-requested": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "cover-requested": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "edit-requested": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "remove-requested": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(self, game: Game) -> None:
        super().__init__()
        self._game = game
        self.set_content_width(620)
        self.set_content_height(620)
        self.set_can_close(True)
        self.set_child(self._build_ui())
        self._init_dialog_lifecycle()

    def _add_value_row(
        self, group: Adw.PreferencesGroup, title: str, value: str
    ) -> Adw.ActionRow:
        row = Adw.ActionRow(title=title)
        row.set_subtitle(value or "None")
        row.set_subtitle_selectable(True)
        group.add(row)
        return row

    def _build_ui(self) -> Gtk.Widget:
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="Game details"))
        close = Gtk.Button(label="Close")
        close.add_css_class("flat")
        self.connect_owned(close, "clicked", self._on_close_clicked)
        header.pack_start(close)

        remove = Gtk.Button(label="Remove")
        remove.add_css_class("destructive-action")
        self.connect_owned(remove, "clicked", self._on_remove_clicked)
        header.pack_end(remove)

        edit = Gtk.Button(label="Edit")
        self.connect_owned(edit, "clicked", self._on_edit_clicked)
        header.pack_end(edit)

        diagnose = Gtk.Button(label="Diagnose")
        diagnose.set_tooltip_text("Run read-only diagnostics for this game")
        self.connect_owned(diagnose, "clicked", self._on_diagnostics_clicked)
        header.pack_end(diagnose)

        launch = Gtk.Button(label="Launch")
        launch.add_css_class("suggested-action")
        launch.set_tooltip_text("Launch this game")
        self.connect_owned(launch, "clicked", self._on_launch_clicked)
        header.pack_end(launch)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)

        page = Adw.PreferencesPage()
        toolbar.set_content(page)

        overview = Adw.PreferencesGroup(title="Overview")
        page.add(overview)
        self._add_value_row(overview, "Name", self._game.name)
        self._add_value_row(
            overview, "Platform", self._game.platform.display_name
        )

        launch = Adw.PreferencesGroup(title="Launch configuration")
        page.add(launch)
        self._add_value_row(launch, "Executable", self._game.executable)
        self._add_value_row(
            launch, "Working directory", self._game.working_directory
        )
        self._add_value_row(
            launch,
            "Launch arguments",
            self._game.launch_arguments or "No arguments",
        )

        performance = Adw.PreferencesGroup(title="Performance")
        page.add(performance)
        self._add_value_row(
            performance,
            "GameMode",
            "Enabled" if self._game.gamemode_enabled else "Disabled",
        )
        self._add_value_row(
            performance,
            "MangoHud",
            "Enabled" if self._game.mangohud_enabled else "Disabled",
        )
        self._add_value_row(
            performance,
            "Upscale resolution (gamescope)",
            self._game.gamescope_resolution or "Disabled",
        )

        files = Adw.PreferencesGroup(title="Files")
        page.add(files)
        cover_row = Adw.ActionRow(title="Cover image")
        cover_row.set_subtitle(self._game.cover_path or "No cover")
        cover_row.set_subtitle_selectable(True)
        cover_row.set_activatable(True)
        self.connect_owned(cover_row, "activated", self._on_cover_activated)
        cover_button = Gtk.Button(label="Change")
        cover_button.set_valign(Gtk.Align.CENTER)
        self.connect_owned(cover_button, "clicked", self._on_cover_clicked)
        cover_row.add_suffix(cover_button)
        files.add(cover_row)
        if self._game.platform is Platform.WINDOWS:
            self._add_value_row(
                files,
                "Wine prefix",
                self._game.wine_prefix_path or "Not assigned",
            )

        return toolbar

    def _on_close_clicked(self, _button: Gtk.Button) -> None:
        self.close()

    def _on_cover_activated(self, _row: Adw.ActionRow) -> None:
        self.emit("cover-requested", self._game)

    def _on_cover_clicked(self, _button: Gtk.Button) -> None:
        self.emit("cover-requested", self._game)

    def _on_launch_clicked(self, _button: Gtk.Button) -> None:
        self.emit("launch-requested", self._game)

    def _on_edit_clicked(self, _button: Gtk.Button) -> None:
        self.emit("edit-requested", self._game)
        self.close()

    def _on_diagnostics_clicked(self, _button: Gtk.Button) -> None:
        self.emit("diagnostics-requested", self._game)
        self.close()

    def _on_remove_clicked(self, _button: Gtk.Button) -> None:
        self.emit("remove-requested", self._game)
        self.close()
