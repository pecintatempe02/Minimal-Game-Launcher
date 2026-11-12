"""First-run setup wizard.

The wizard only explains the application and records preferences. It performs
no package installation, starts no background service, and does not create or
modify Wine prefixes. Tool detection is a single ``PATH`` lookup when the page
is shown.

Completion is stored in ``settings.json`` as ``setup_completed``. Skipping also
marks the wizard as completed so it does not reappear on every start.
"""

from __future__ import annotations

import shutil
from dataclasses import replace

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, GObject, Gtk  # noqa: E402

from minimal_game_launcher import APP_NAME, APP_VERSION  # noqa: E402
from minimal_game_launcher.errors import DesktopIntegrationError  # noqa: E402
from minimal_game_launcher.services.desktop_integration import (  # noqa: E402
    DesktopEntryState,
    DesktopIntegrationService,
)
from minimal_game_launcher.settings import (  # noqa: E402
    LauncherSettings,
    SettingsStore,
    WinePrefixStrategy,
)
from minimal_game_launcher.ui.lifecycle import DialogSignalOwner  # noqa: E402

_PREFIX_STRATEGIES = (WinePrefixStrategy.PER_GAME, WinePrefixStrategy.SHARED)
_PREFIX_LABELS = ("One prefix per game", "One shared prefix")
_TOOLS = (
    ("wine", "Wine", "wine", "wine64"),
    ("gamemoderun", "GameMode", "gamemoderun"),
    ("mangohud", "MangoHud", "mangohud"),
    ("gamescope", "gamescope", "gamescope"),
    ("winetricks", "Winetricks", "winetricks"),
)


class SetupWizard(Adw.Dialog, DialogSignalOwner):
    """Explain the launcher and store first-run preferences."""

    __gsignals__ = {
        "finished": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(
        self,
        settings_store: SettingsStore,
        *,
        desktop_integration: DesktopIntegrationService | None = None,
    ) -> None:
        super().__init__()
        self._settings_store = settings_store
        self._desktop_integration = desktop_integration
        self._current = settings_store.load()
        self._prefix_strategy = self._current.wine_prefix_strategy
        self._initial_desktop = (
            desktop_integration.is_enabled()
            if desktop_integration is not None
            else self._current.desktop_entry_enabled is True
        )
        self._desktop_enabled = self._initial_desktop
        self._step = 0
        self._active_error: Adw.AlertDialog | None = None
        self.set_content_width(620)
        self.set_content_height(560)
        self.set_can_close(False)
        self.set_child(self._build_ui())
        self._show_step(0)
        self._init_dialog_lifecycle()

    def _build_ui(self) -> Gtk.Widget:
        header = Adw.HeaderBar()
        header.set_title_widget(
            Adw.WindowTitle(title=f"{APP_NAME} {APP_VERSION}")
        )
        skip = Gtk.Button(label="Skip")
        skip.add_css_class("flat")
        self.connect_owned(skip, "clicked", self._on_skip)
        header.pack_start(skip)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)

        self._stack = Gtk.Stack()
        self._stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT)
        self._stack.set_transition_duration(150)
        toolbar.set_content(self._stack)

        self._stack.add_named(self._welcome_page(), "welcome")
        self._stack.add_named(self._tools_page(), "tools")
        self._stack.add_named(self._preferences_page(), "preferences")
        self._stack.add_named(self._finish_page(), "finish")

        self._back = Gtk.Button(label="Back")
        self.connect_owned(self._back, "clicked", self._on_back)
        self._next = Gtk.Button(label="Continue")
        self._next.add_css_class("suggested-action")
        self.connect_owned(self._next, "clicked", self._on_next)

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bar.set_margin_top(12)
        bar.set_margin_bottom(12)
        bar.set_margin_start(18)
        bar.set_margin_end(18)
        bar.append(self._back)
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        bar.append(spacer)
        bar.append(self._next)
        toolbar.add_bottom_bar(bar)
        return toolbar

    def _page(
        self, title: str, description: str
    ) -> tuple[Adw.PreferencesPage, Adw.PreferencesGroup]:
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(title=title, description=description)
        page.add(group)
        return page, group

    def _welcome_page(self) -> Gtk.Widget:
        page, group = self._page(
            "Welcome",
            "Minimal Game Launcher keeps a local library of Linux and "
            "Windows/Wine games. It does not run a daemon, monitor your "
            "library in the background, or install dependencies.",
        )
        for title, subtitle in (
            (
                "Local only",
                "The library and covers stay on this computer. Online cover "
                "search is optional and uses only the game name.",
            ),
            (
                "Your data is left alone",
                "Removing a game from the library never deletes its files, "
                "prefix, or covers unless you explicitly choose that.",
            ),
            (
                "External Wine prefixes are read-only",
                "MGL creates and modifies only prefixes it manages.",
            ),
        ):
            row = Adw.ActionRow(title=title, subtitle=subtitle)
            group.add(row)
        return page

    def _tools_page(self) -> Gtk.Widget:
        page, group = self._page(
            "Optional tools",
            "Detected once from PATH. MGL never installs or updates these "
            "tools; missing entries only disable the matching feature.",
        )
        for key, label, *commands in _TOOLS:
            found = next(
                (
                    located
                    for command in commands
                    if (located := shutil.which(command))
                ),
                None,
            )
            row = Adw.ActionRow(title=label)
            row.set_subtitle(found or "Not found in PATH")
            icon = Gtk.Image.new_from_icon_name(
                "emblem-ok-symbolic" if found else "dialog-warning-symbolic"
            )
            icon.add_css_class("dim-label" if found else "warning")
            row.add_suffix(icon)
            group.add(row)
        return page

    def _preferences_page(self) -> Gtk.Widget:
        page, group = self._page(
            "Preferences",
            "These can be changed later in Settings.",
        )

        self._strategy_row = Adw.ComboRow(
            title="Wine prefix strategy",
            subtitle="Applies to games and installs added from now on.",
        )
        self._strategy_row.set_model(Gtk.StringList.new(list(_PREFIX_LABELS)))
        self._strategy_row.set_selected(
            _PREFIX_STRATEGIES.index(self._prefix_strategy)
        )
        self.connect_owned(
            self._strategy_row,
            "notify::selected",
            self._on_strategy_changed,
        )
        group.add(self._strategy_row)

        self._desktop_row = Adw.SwitchRow(
            title="Show MGL in the application launcher",
            subtitle=(
                "Creates only MGL's own user-level desktop entry. Fuzzel and "
                "Hyprland configuration is not changed."
            ),
        )
        self._desktop_row.set_active(self._desktop_enabled)
        self.connect_owned(
            self._desktop_row, "notify::active", self._on_desktop_toggled
        )
        group.add(self._desktop_row)
        return page

    def _finish_page(self) -> Gtk.Widget:
        page, group = self._page(
            "Ready",
            "Add your first game from the library window. Diagnostics, "
            "cleanup, and Wine tools are available on demand.",
        )
        row = Adw.ActionRow(
            title="No background services",
            subtitle=(
                "Nothing is scanned until you ask. A running game is left "
                "running when the launcher closes."
            ),
        )
        group.add(row)
        return page

    def _show_step(self, step: int) -> None:
        names = ("welcome", "tools", "preferences", "finish")
        self._step = max(0, min(step, len(names) - 1))
        self._stack.set_visible_child_name(names[self._step])
        self._back.set_sensitive(self._step > 0)
        self._next.set_label(
            "Finish" if self._step == len(names) - 1 else "Continue"
        )

    def _on_back(self, _button: Gtk.Button) -> None:
        self._show_step(self._step - 1)

    def _on_next(self, _button: Gtk.Button) -> None:
        if self._step < 3:
            if self._step == 1:
                # Refresh the tools page in case a tool was installed while the
                # wizard was open. This is still a single on-demand lookup.
                self._stack.remove(
                    self._stack.get_child_by_name("tools")
                )
                self._stack.add_named(self._tools_page(), "tools")
            self._show_step(self._step + 1)
            return
        self._finish()

    def _on_strategy_changed(self, _row: Adw.ComboRow, _pspec: object) -> None:
        index = self._strategy_row.get_selected()
        if 0 <= index < len(_PREFIX_STRATEGIES):
            self._prefix_strategy = _PREFIX_STRATEGIES[index]

    def _on_desktop_toggled(self, _row: Adw.SwitchRow, _pspec: object) -> None:
        self._desktop_enabled = self._desktop_row.get_active()

    def _apply_desktop(self) -> bool:
        if (
            self._desktop_integration is None
            or self._desktop_enabled == self._initial_desktop
        ):
            return True
        try:
            self._desktop_integration.apply(self._desktop_enabled)
        except DesktopIntegrationError as error:
            self._show_error(
                "The desktop entry could not be updated: "
                f"{error}\n\nCheck that $XDG_DATA_HOME/applications is "
                "writable, then try again."
            )
            return False
        return True

    def _settings(self, *, completed: bool) -> LauncherSettings:
        desktop = self._current.desktop_entry_enabled
        if self._desktop_enabled != self._initial_desktop:
            desktop = self._desktop_enabled
        return replace(
            self._current,
            wine_prefix_strategy=self._prefix_strategy,
            desktop_entry_enabled=desktop,
            setup_completed=completed,
        )

    def _finish(self) -> None:
        if not self._apply_desktop():
            return
        settings = self._settings(completed=True)
        try:
            self._settings_store.save(settings)
        except OSError as error:
            self._restore_desktop()
            self._show_error(f"Settings could not be saved: {error}")
            return
        self.emit("finished", settings)
        self.close()

    def _on_skip(self, _button: Gtk.Button) -> None:
        # Skip completes setup without applying side effects. The desktop-entry
        # switch is the only preference with an action, so its toggle is
        # discarded rather than saved while no entry is created.
        settings = replace(
            self._settings(completed=True),
            desktop_entry_enabled=self._current.desktop_entry_enabled,
        )
        try:
            self._settings_store.save(settings)
        except OSError as error:
            self._show_error(f"Settings could not be saved: {error}")
            return
        self.emit("finished", settings)
        self.close()

    def _restore_desktop(self) -> None:
        if self._desktop_integration is None:
            return
        try:
            if self._initial_desktop:
                self._desktop_integration.enable()
            elif self._current.desktop_entry_enabled is not None:
                self._desktop_integration.disable()
            else:
                self._desktop_integration.remove_owned_entry()
        except DesktopIntegrationError:
            pass

    def _show_error(self, message: str) -> None:
        dialog = Adw.AlertDialog(heading="Setup could not be saved", body=message)
        dialog.add_response("close", "Close")
        dialog.set_default_response("close")
        dialog.set_close_response("close")
        self._active_error = dialog
        dialog.choose(self, None, self._on_error_closed)

    def _on_error_closed(
        self, dialog: Adw.AlertDialog, result: object
    ) -> None:
        self._active_error = None
        try:
            dialog.choose_finish(result)
        except GLib.Error:
            pass

    def _release_dialog_references(self) -> None:
        self._active_error = None
        self._stack = None
        self._strategy_row = None
        self._desktop_row = None
        self._back = None
        self._next = None
