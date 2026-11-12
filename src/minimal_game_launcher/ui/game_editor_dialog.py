"""Add/edit dialog for game metadata and launch configuration.

File selection uses GTK's native ``Gtk.FileDialog``. The dialog only collects
values; all path validation and persistence is delegated to ``GameManager`` so
the same rules apply to add and edit operations. Performance switches are
included here but wrapper construction belongs to ``PerformanceManager``.

Covers selected here are validated by ``CoverManager`` and copied into the
application cache when the form is saved. The original image is only read.
For a new game, the editor reserves the final game ID before saving so the
cached cover filename already matches the persisted record.
"""

from __future__ import annotations

from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib, GObject, Gtk  # noqa: E402

from minimal_game_launcher.errors import LauncherError  # noqa: E402
from minimal_game_launcher.models import (  # noqa: E402
    DisplayMode,
    Game,
    GameDraft,
    Platform,
)
from minimal_game_launcher.paths import covers_directory  # noqa: E402
from minimal_game_launcher.services.cover_manager import CoverManager  # noqa: E402
from minimal_game_launcher.services.game_manager import GameManager  # noqa: E402
from minimal_game_launcher.services.performance_manager import (  # noqa: E402
    PerformanceManager,
)
from minimal_game_launcher.ui.cover import DEFAULT_CACHE, load_cover_texture  # noqa: E402
from minimal_game_launcher.ui.lifecycle import DialogSignalOwner  # noqa: E402

_IMAGE_PATTERNS = (
    "*.png",
    "*.PNG",
    "*.jpg",
    "*.JPG",
    "*.jpeg",
    "*.JPEG",
    "*.webp",
    "*.WEBP",
)
_DISPLAY_MODES = tuple(DisplayMode)
_VSYNC_LABELS = ("Default", "On", "Off")
_FILTER_LABELS = ("Default", "FSR", "NIS", "Linear", "Nearest")


class GameEditorDialog(Adw.Dialog, DialogSignalOwner):
    """Collect and persist one game's metadata.

    ``game`` is ``None`` for creation. The dialog emits ``saved`` with the
    resulting domain object after ``GameManager`` accepts the data.
    """

    __gsignals__ = {
        "saved": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(
        self,
        parent_window: Gtk.Window,
        manager: GameManager,
        game: Game | None = None,
        *,
        performance_manager: PerformanceManager | None = None,
        cover_manager: CoverManager | None = None,
    ) -> None:
        super().__init__()
        self._parent_window = parent_window
        self._manager = manager
        self._game = game
        self._performance_manager = performance_manager or PerformanceManager()
        self._availability = self._performance_manager.detect()
        self._cover_manager = cover_manager or CoverManager(covers_directory())
        self._active_file_dialog: Gtk.FileDialog | None = None
        self._active_error: Adw.AlertDialog | None = None

        self._platform = game.platform if game else Platform.LINUX
        self._executable = game.executable if game else ""
        self._working_directory = game.working_directory if game else ""
        self._stored_cover_path = game.cover_path if game else None
        self._cover_path = self._stored_cover_path
        self._pending_cover_source: Path | None = None
        self._gamemode_enabled = game.gamemode_enabled if game else True
        self._mangohud_enabled = game.mangohud_enabled if game else False
        self._gamescope_resolution = game.gamescope_resolution if game else ""
        self._gamescope_enabled = (
            game.gamescope_active if game else False
        )
        self._gamescope_output_resolution = (
            game.gamescope_output_resolution if game else ""
        )
        self._gamescope_fps_limit = game.gamescope_fps_limit if game else 0
        self._gamescope_vsync = game.gamescope_vsync if game else ""
        self._gamescope_filter = game.gamescope_filter if game else ""
        self._gamescope_fullscreen = (
            game.gamescope_fullscreen if game else True
        )
        self._display_mode = (
            game.display_mode if game else DisplayMode.DEFAULT
        )
        self._virtual_desktop_resolution = (
            game.virtual_desktop_resolution if game else ""
        )
        self._show_in_library = game.show_in_library if game else True

        self.set_content_width(620)
        self.set_content_height(700)
        self.set_can_close(True)
        self.set_child(self._build_ui())
        self._refresh_all_rows()
        self._init_dialog_lifecycle()

    def _build_ui(self) -> Gtk.Widget:
        header = Adw.HeaderBar()
        header.set_title_widget(
            Adw.WindowTitle(title="Edit game" if self._game else "Add game")
        )

        cancel = Gtk.Button(label="Cancel")
        cancel.add_css_class("flat")
        self.connect_owned(cancel, "clicked", self._on_cancel_clicked)
        header.pack_start(cancel)

        save = Gtk.Button(label="Save" if self._game else "Add")
        save.add_css_class("suggested-action")
        self.connect_owned(save, "clicked", self._on_save_clicked)
        header.pack_end(save)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)

        preferences = Adw.PreferencesPage()
        toolbar.set_content(preferences)

        game_group = Adw.PreferencesGroup(title="Game")
        preferences.add(game_group)

        self._name_row = Adw.EntryRow(title="Game name")
        if self._game:
            self._name_row.set_text(self._game.name)
        game_group.add(self._name_row)

        self._platform_row = Adw.ComboRow(title="Platform")
        self._platform_row.set_model(
            Gtk.StringList.new(["Linux Native", "Windows / Wine"])
        )
        self._platform_row.set_selected(
            1 if self._platform is Platform.WINDOWS else 0
        )
        self.connect_owned(
            self._platform_row, "notify::selected", self._on_platform_changed
        )
        game_group.add(self._platform_row)

        self._visible_row = Adw.SwitchRow(
            title="Show in library",
            subtitle=(
                "Hidden games keep all their data and remain reachable from "
                "the hidden-games manager."
            ),
        )
        self._visible_row.set_active(self._show_in_library)
        game_group.add(self._visible_row)

        launch_group = Adw.PreferencesGroup(
            title="Launch configuration",
            description=(
                "Linux entries accept a command found in PATH (for example "
                "flatpak) or an executable path. Arguments are validated with "
                "shell-like quoting, but games are never launched through a "
                "shell."
            ),
        )
        preferences.add(launch_group)

        self._executable_row = Adw.EntryRow(title="Executable or command")
        self._executable_row.set_text(self._executable)
        self.connect_owned(
            self._executable_row, "changed", self._on_executable_changed
        )
        choose_executable = Gtk.Button.new_from_icon_name("folder-open-symbolic")
        choose_executable.set_tooltip_text("Choose executable")
        choose_executable.set_valign(Gtk.Align.CENTER)
        self.connect_owned(choose_executable, "clicked", self._choose_executable)
        self._executable_row.add_suffix(choose_executable)
        launch_group.add(self._executable_row)

        self._working_directory_row = Adw.ActionRow(title="Working directory")
        self._working_directory_row.set_subtitle_selectable(True)
        self._working_directory_row.set_activatable(True)
        self.connect_owned(
            self._working_directory_row,
            "activated",
            self._choose_working_directory,
        )
        reset_directory = Gtk.Button.new_from_icon_name("edit-undo-symbolic")
        reset_directory.set_tooltip_text("Use executable folder")
        reset_directory.set_valign(Gtk.Align.CENTER)
        self.connect_owned(
            reset_directory, "clicked", self._reset_working_directory
        )
        self._working_directory_row.add_suffix(reset_directory)
        choose_directory = Gtk.Button.new_from_icon_name("folder-open-symbolic")
        choose_directory.set_tooltip_text("Choose working directory")
        choose_directory.set_valign(Gtk.Align.CENTER)
        self.connect_owned(
            choose_directory, "clicked", self._choose_working_directory
        )
        self._working_directory_row.add_suffix(choose_directory)
        launch_group.add(self._working_directory_row)

        self._arguments_row = Adw.EntryRow(title="Launch arguments")
        if self._game:
            self._arguments_row.set_text(self._game.launch_arguments)
        launch_group.add(self._arguments_row)

        display_group = Adw.PreferencesGroup(
            title="Display",
            description=(
                "Fullscreen, windowed, and borderless are hints only: the game "
                "decides. The Wine virtual desktop and gamescope are enforced "
                "by the launcher. The host display mode is never changed."
            ),
        )
        preferences.add(display_group)

        self._display_mode_row = Adw.ComboRow(title="Display mode")
        self._display_mode_row.set_model(
            Gtk.StringList.new(
                [mode.display_name for mode in _DISPLAY_MODES]
            )
        )
        self._display_mode_row.set_selected(
            _DISPLAY_MODES.index(self._display_mode)
        )
        self.connect_owned(
            self._display_mode_row,
            "notify::selected",
            self._on_display_mode_changed,
        )
        display_group.add(self._display_mode_row)

        self._virtual_desktop_row = Adw.EntryRow(
            title="Virtual desktop resolution"
        )
        self._virtual_desktop_row.set_text(self._virtual_desktop_resolution)
        self._virtual_desktop_row.set_tooltip_text(
            "For example 1280x720. Used only with the Wine virtual desktop."
        )
        display_group.add(self._virtual_desktop_row)

        environment_group = Adw.PreferencesGroup(
            title="Environment variables",
            description=(
                "Optional. One KEY=VALUE per line. Values are never evaluated "
                "by a shell or written to the log."
            ),
        )
        preferences.add(environment_group)

        self._environment_view = Gtk.TextView()
        self._environment_view.set_monospace(True)
        self._environment_view.set_wrap_mode(Gtk.WrapMode.NONE)
        if self._game and self._game.environment:
            self._environment_view.get_buffer().set_text(
                "\n".join(
                    f"{key}={value}"
                    for key, value in self._game.environment.items()
                )
            )
        environment_scroll = Gtk.ScrolledWindow()
        environment_scroll.set_policy(
            Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC
        )
        environment_scroll.set_min_content_height(110)
        environment_scroll.set_child(self._environment_view)
        environment_scroll.add_css_class("card")
        environment_group.add(environment_scroll)

        performance_group = Adw.PreferencesGroup(
            title="Performance",
            description=(
                "Optional integrations. Missing tools never prevent a game "
                "from starting; the launcher explains any fallback."
            ),
        )
        preferences.add(performance_group)

        self._gamemode_row = Adw.SwitchRow(title="GameMode")
        self._gamemode_row.set_active(self._gamemode_enabled)
        performance_group.add(self._gamemode_row)

        self._mangohud_row = Adw.SwitchRow(title="MangoHud")
        self._mangohud_row.set_active(self._mangohud_enabled)
        performance_group.add(self._mangohud_row)

        # gamescope renders the game at a fixed resolution and upscales it to
        # the display. This is what makes a game with an 800x600 back buffer
        # fill a modern monitor instead of a corner of it.
        self._gamescope_enabled_row = Adw.SwitchRow(
            title="gamescope",
            subtitle=(
                "Runs the game in gamescope. Required for the resolution and "
                "upscaling options below."
            ),
        )
        self._gamescope_enabled_row.set_active(self._gamescope_enabled)
        self.connect_owned(
            self._gamescope_enabled_row,
            "notify::active",
            self._on_gamescope_toggled,
        )
        performance_group.add(self._gamescope_enabled_row)

        self._gamescope_row = Adw.EntryRow(title="Render resolution")
        self._gamescope_row.set_text(self._gamescope_resolution)
        self._gamescope_row.set_tooltip_text(
            "Optional, for example 800x600. gamescope renders the game at "
            "this resolution and scales it to the display."
        )
        performance_group.add(self._gamescope_row)

        self._gamescope_output_row = Adw.EntryRow(
            title="Output resolution"
        )
        self._gamescope_output_row.set_text(
            self._gamescope_output_resolution
        )
        self._gamescope_output_row.set_tooltip_text(
            "Optional, for example 1920x1080. Leave empty to use the display."
        )
        performance_group.add(self._gamescope_output_row)

        self._gamescope_fps_row = Adw.SpinRow.new_with_range(
            0, 1000, 1
        )
        self._gamescope_fps_row.set_title("FPS limit")
        self._gamescope_fps_row.set_subtitle("0 leaves the frame rate unlimited")
        self._gamescope_fps_row.set_value(self._gamescope_fps_limit)
        performance_group.add(self._gamescope_fps_row)

        self._gamescope_vsync_row = Adw.ComboRow(title="VSync")
        self._gamescope_vsync_row.set_model(
            Gtk.StringList.new(list(_VSYNC_LABELS))
        )
        self._gamescope_vsync_row.set_selected(
            {"": 0, "on": 1, "off": 2}.get(self._gamescope_vsync, 0)
        )
        performance_group.add(self._gamescope_vsync_row)

        self._gamescope_filter_row = Adw.ComboRow(title="Upscaler filter")
        self._gamescope_filter_row.set_model(
            Gtk.StringList.new(list(_FILTER_LABELS))
        )
        self._gamescope_filter_row.set_selected(
            {"": 0, "fsr": 1, "nis": 2, "linear": 3, "nearest": 4}.get(
                self._gamescope_filter, 0
            )
        )
        performance_group.add(self._gamescope_filter_row)

        self._gamescope_fullscreen_row = Adw.SwitchRow(
            title="gamescope fullscreen"
        )
        self._gamescope_fullscreen_row.set_active(
            self._gamescope_fullscreen
        )
        performance_group.add(self._gamescope_fullscreen_row)

        cover_group = Adw.PreferencesGroup(
            title="Cover",
            description=(
                "Optional. Selected images are validated and copied into the "
                "launcher cover cache on save. The original file is never "
                "modified."
            ),
        )
        preferences.add(cover_group)

        self._cover_row = Adw.ActionRow(title="Cover image")
        self._cover_row.set_subtitle_selectable(True)

        self._cover_preview = Gtk.Picture()
        self._cover_preview.set_size_request(40, 52)
        self._cover_preview.set_can_shrink(True)
        self._cover_preview.set_content_fit(Gtk.ContentFit.CONTAIN)
        self._cover_preview.set_hexpand(False)
        self._cover_preview.set_vexpand(False)
        self._cover_row.add_suffix(self._cover_preview)

        clear_cover = Gtk.Button.new_from_icon_name("edit-clear-symbolic")
        clear_cover.set_tooltip_text("Remove cover")
        clear_cover.set_valign(Gtk.Align.CENTER)
        self.connect_owned(clear_cover, "clicked", self._clear_cover)
        self._cover_row.add_suffix(clear_cover)

        choose_cover = Gtk.Button.new_from_icon_name("folder-open-symbolic")
        choose_cover.set_tooltip_text("Choose cover image")
        choose_cover.set_valign(Gtk.Align.CENTER)
        self.connect_owned(choose_cover, "clicked", self._choose_cover)
        self._cover_row.add_suffix(choose_cover)
        cover_group.add(self._cover_row)

        return toolbar

    def _refresh_all_rows(self) -> None:
        self._update_platform_labels()
        self._refresh_executable_row()
        self._refresh_working_directory_row()
        self._refresh_performance_rows()
        self._refresh_display_rows()
        self._refresh_cover_row()

    def _update_platform_labels(self) -> None:
        self._executable_row.set_title(
            "Executable (.exe)"
            if self._platform is Platform.WINDOWS
            else "Executable or command"
        )
        self._refresh_executable_row()

    def _refresh_executable_row(self) -> None:
        if self._executable_row.get_text() != self._executable:
            self._executable_row.set_text(self._executable)
        self._executable_row.set_tooltip_text(
            "Type a command available in PATH, or choose an executable file"
            if self._platform is Platform.LINUX
            else "Type or choose the Windows .exe file"
        )

    def _on_executable_changed(self, row: Adw.EntryRow) -> None:
        self._executable = row.get_text()

    def _refresh_working_directory_row(self) -> None:
        if self._working_directory:
            self._working_directory_row.set_subtitle(self._working_directory)
        else:
            self._working_directory_row.set_subtitle(
                "Defaults to the executable's parent directory"
            )

    def _refresh_performance_rows(self) -> None:
        """Describe detected tools and known integration limitations."""
        if self._availability.gamemode_available:
            self._gamemode_row.set_subtitle(
                "gamemoderun found in PATH. Enabled games launch through it."
            )
        else:
            self._gamemode_row.set_subtitle(
                "gamemoderun was not found in PATH. Enabled games will launch "
                "without GameMode."
            )

        if not self._availability.mangohud_available:
            self._mangohud_row.set_subtitle(
                "mangohud was not found in PATH. The overlay cannot be "
                "enabled."
            )
        elif self._platform is Platform.WINDOWS:
            # Do not promise a working overlay: Wine needs a 32-bit Vulkan
            # layer whose presence cannot be verified from the PATH lookup.
            self._mangohud_row.set_subtitle(
                "mangohud found in PATH. Wine games use MANGOHUD=1; the "
                "required 32-bit Vulkan layer is not verified by the launcher."
            )
        else:
            self._mangohud_row.set_subtitle(
                "mangohud found in PATH. Enabled games launch through it."
            )

        if not self._availability.gamescope_available:
            self._gamescope_enabled_row.set_subtitle(
                "gamescope was not found in PATH. Enabling it will prevent "
                "this game from launching until gamescope is installed."
            )
        else:
            self._gamescope_enabled_row.set_subtitle(
                "gamescope found in PATH. The host display mode is not changed."
            )
        self._refresh_gamescope_rows()

    def _refresh_display_rows(self) -> None:
        self._virtual_desktop_row.set_visible(
            self._display_mode is DisplayMode.VIRTUAL_DESKTOP
        )
        self._virtual_desktop_row.set_sensitive(
            self._platform is Platform.WINDOWS
        )

    def _refresh_gamescope_rows(self) -> None:
        enabled = self._gamescope_enabled_row.get_active()
        for row in (
            self._gamescope_row,
            self._gamescope_output_row,
            self._gamescope_fps_row,
            self._gamescope_vsync_row,
            self._gamescope_filter_row,
            self._gamescope_fullscreen_row,
        ):
            row.set_sensitive(enabled)

    def _on_gamescope_toggled(
        self, _row: Adw.SwitchRow, _pspec: object
    ) -> None:
        self._refresh_gamescope_rows()

    def _on_display_mode_changed(
        self, _row: Adw.ComboRow, _pspec: object
    ) -> None:
        index = self._display_mode_row.get_selected()
        if 0 <= index < len(_DISPLAY_MODES):
            self._display_mode = _DISPLAY_MODES[index]
        self._refresh_display_rows()

    def _refresh_cover_row(self) -> None:
        display_path = (
            str(self._pending_cover_source)
            if self._pending_cover_source is not None
            else self._cover_path
        )
        if display_path:
            if self._pending_cover_source is not None:
                self._cover_row.set_subtitle(
                    f"{display_path} (copied to the cache on save)"
                )
            else:
                self._cover_row.set_subtitle(display_path)
            texture = load_cover_texture(display_path, 80, 104)
            self._cover_preview.set_paintable(texture)
            self._cover_preview.set_visible(texture is not None)
        else:
            self._cover_row.set_subtitle("No cover selected")
            self._cover_preview.set_paintable(None)
            self._cover_preview.set_visible(False)

    def _on_platform_changed(self, _row: Adw.ComboRow, _pspec: object) -> None:
        selected = self._platform_row.get_selected()
        self._platform = Platform.WINDOWS if selected == 1 else Platform.LINUX
        if (
            self._platform is Platform.LINUX
            and self._display_mode is DisplayMode.VIRTUAL_DESKTOP
        ):
            self._display_mode = DisplayMode.DEFAULT
            self._display_mode_row.set_selected(
                _DISPLAY_MODES.index(self._display_mode)
            )
        self._update_platform_labels()
        self._refresh_performance_rows()
        self._refresh_display_rows()

    def _set_initial_folder(self, dialog: Gtk.FileDialog, path: str | None) -> None:
        if not path:
            return
        folder = Path(path).expanduser()
        if folder.is_file():
            folder = folder.parent
        if folder.is_dir():
            dialog.set_initial_folder(Gio.File.new_for_path(str(folder)))

    def _file_filters(
        self, name: str, patterns: tuple[str, ...]
    ) -> tuple[Gio.ListStore, Gtk.FileFilter]:
        file_filter = Gtk.FileFilter()
        file_filter.set_name(name)
        for pattern in patterns:
            file_filter.add_pattern(pattern)
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(file_filter)
        return filters, file_filter

    def _choose_executable(self, _widget: Gtk.Widget) -> None:
        dialog = Gtk.FileDialog(title="Select game executable", modal=True)
        if self._platform is Platform.WINDOWS:
            filters, default = self._file_filters(
                "Windows executables (*.exe)", ("*.exe", "*.EXE")
            )
        else:
            filters, default = self._file_filters("All files", ("*",))
        dialog.set_filters(filters)
        dialog.set_default_filter(default)
        self._set_initial_folder(dialog, self._executable)
        self._active_file_dialog = dialog
        dialog.open(self._parent_window, None, self._on_executable_selected)

    def _on_executable_selected(
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
            self._show_error("Only local files can be selected.")
            return

        previous_executable = self._executable
        previous_parent = (
            str(Path(previous_executable).expanduser().parent)
            if previous_executable
            else ""
        )
        self._executable = path
        new_parent = str(Path(path).parent)

        # Preserve a deliberately customized working directory; otherwise keep
        # the default working directory in sync with the chosen executable.
        if not self._working_directory or self._working_directory == previous_parent:
            self._working_directory = new_parent

        if not self._name_row.get_text().strip():
            self._name_row.set_text(Path(path).stem)

        self._refresh_executable_row()
        self._refresh_working_directory_row()

    def _choose_working_directory(self, _widget: Gtk.Widget) -> None:
        dialog = Gtk.FileDialog(title="Select working directory", modal=True)
        self._set_initial_folder(
            dialog, self._working_directory or self._executable
        )
        self._active_file_dialog = dialog
        dialog.select_folder(self._parent_window, None, self._on_working_directory_selected)

    def _on_working_directory_selected(
        self, dialog: Gtk.FileDialog, result: Gio.AsyncResult
    ) -> None:
        self._active_file_dialog = None
        try:
            selected = dialog.select_folder_finish(result)
        except GLib.Error as error:
            if not error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                self._show_error(error.message)
            return

        path = selected.get_path()
        if not path:
            self._show_error("Only local directories can be selected.")
            return
        self._working_directory = path
        self._refresh_working_directory_row()

    def _reset_working_directory(self, _button: Gtk.Button) -> None:
        self._working_directory = (
            GameManager.default_working_directory(self._executable)
            if self._executable
            else ""
        )
        self._refresh_working_directory_row()

    def _choose_cover(self, _button: Gtk.Button) -> None:
        dialog = Gtk.FileDialog(title="Select cover image", modal=True)
        filters, default = self._file_filters(
            "Image files", _IMAGE_PATTERNS
        )
        dialog.set_filters(filters)
        dialog.set_default_filter(default)
        self._set_initial_folder(dialog, self._cover_path or self._executable)
        self._active_file_dialog = dialog
        dialog.open(self._parent_window, None, self._on_cover_selected)

    def _on_cover_selected(
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
            self._show_error("Only local cover files can be selected.")
            return
        try:
            self._cover_manager.inspect_file(path)
        except LauncherError as error:
            self._show_error(str(error))
            return
        if load_cover_texture(path, 80, 104) is None:
            self._show_error(
                "The selected cover could not be previewed. Choose another "
                "PNG, JPEG, or WebP image."
            )
            return
        self._pending_cover_source = Path(path)
        self._cover_path = path
        self._refresh_cover_row()

    def _clear_cover(self, _button: Gtk.Button) -> None:
        self._pending_cover_source = None
        self._cover_path = None
        self._refresh_cover_row()

    def _on_cancel_clicked(self, _button: Gtk.Button) -> None:
        self.close()

    def _release_dialog_references(self) -> None:
        """Drop widget and preview references once the dialog is closed."""
        self._active_file_dialog = None
        self._active_error = None
        preview = getattr(self, "_cover_preview", None)
        if preview is not None:
            preview.set_paintable(None)

    def _on_save_clicked(self, _button: Gtk.Button) -> None:
        game_id = self._game.id if self._game else self._manager.new_game_id()

        cover_path = self._cover_path
        newly_cached_cover: str | None = None
        if self._pending_cover_source is not None:
            try:
                newly_cached_cover = self._cover_manager.import_local(
                    game_id, self._pending_cover_source
                )
            except LauncherError as error:
                self._show_error(str(error))
                return
            cover_path = newly_cached_cover

        draft = GameDraft(
            name=self._name_row.get_text(),
            platform=self._platform,
            executable=self._executable,
            working_directory=self._working_directory,
            launch_arguments=self._arguments_row.get_text(),
            environment=self._environment_text(),
            cover_path=cover_path,
            gamemode_enabled=self._gamemode_row.get_active(),
            mangohud_enabled=self._mangohud_row.get_active(),
            gamescope_resolution=self._gamescope_row.get_text(),
            gamescope_enabled=self._gamescope_enabled_row.get_active(),
            gamescope_output_resolution=(
                self._gamescope_output_row.get_text()
            ),
            gamescope_fps_limit=int(self._gamescope_fps_row.get_value()),
            gamescope_vsync=("", "on", "off")[
                max(0, self._gamescope_vsync_row.get_selected())
            ],
            gamescope_filter=("", "fsr", "nis", "linear", "nearest")[
                max(0, self._gamescope_filter_row.get_selected())
            ],
            gamescope_fullscreen=(
                self._gamescope_fullscreen_row.get_active()
            ),
            display_mode=self._display_mode,
            virtual_desktop_resolution=(
                self._virtual_desktop_row.get_text()
            ),
            show_in_library=self._visible_row.get_active(),
        )
        try:
            if self._game:
                saved = self._manager.update_game(self._game.id, draft)
            else:
                saved = self._manager.add_game(draft, game_id=game_id)
        except LauncherError as error:
            if newly_cached_cover:
                self._cover_manager.remove_cover(newly_cached_cover)
            self._show_error(str(error))
            return
        except OSError as error:
            if newly_cached_cover:
                self._cover_manager.remove_cover(newly_cached_cover)
            self._show_error(f"The game could not be saved: {error}")
            return

        previous_cover = self._stored_cover_path
        if (
            previous_cover
            and previous_cover != saved.cover_path
            and self._cover_manager.is_cached_cover(previous_cover)
        ):
            # Only cache-owned files are deleted. Legacy external references
            # and user-owned originals are left untouched.
            self._cover_manager.remove_cover(previous_cover)
            DEFAULT_CACHE.invalidate(previous_cover)
        if saved.cover_path:
            DEFAULT_CACHE.invalidate(saved.cover_path)

        self.emit("saved", saved)
        self.close()

    def _environment_text(self) -> str:
        """Return the raw multiline environment editor content."""
        buffer = self._environment_view.get_buffer()
        return buffer.get_text(
            buffer.get_start_iter(), buffer.get_end_iter(), True
        )

    def _show_error(self, message: str) -> None:
        """Present validation details without dismissing the form."""
        dialog = Adw.AlertDialog(heading="Cannot save game", body=message)
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
