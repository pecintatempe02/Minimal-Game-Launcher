"""GTK application lifecycle and composition root."""

from __future__ import annotations

import logging
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, Gtk  # noqa: E402

from minimal_game_launcher import APP_ID  # noqa: E402
from minimal_game_launcher.database import Database, open_library  # noqa: E402
from minimal_game_launcher.logging_setup import configure_logging  # noqa: E402
from minimal_game_launcher.paths import (  # noqa: E402
    covers_directory,
    database_path,
    ensure_data_directory,
    legacy_theme_path,
    prefixes_directory,
    settings_path,
    state_directory,
    themes_path,
)
from minimal_game_launcher.services.cover_manager import CoverManager  # noqa: E402
from minimal_game_launcher.services.game_manager import GameManager  # noqa: E402
from minimal_game_launcher.services.launch_manager import LaunchManager  # noqa: E402
from minimal_game_launcher.services.performance_manager import (  # noqa: E402
    PerformanceManager,
)
from minimal_game_launcher.services.steamgriddb import (  # noqa: E402
    SteamGridDBClient,
)
from minimal_game_launcher.settings import SettingsStore  # noqa: E402
from minimal_game_launcher.ui.main_window import MainWindow  # noqa: E402
from minimal_game_launcher.ui.theme import ThemeController  # noqa: E402

LOGGER = logging.getLogger(__name__)


class MinimalGameLauncherApplication(Adw.Application):
    """Own application startup, shared services, and the main window."""

    def __init__(self) -> None:
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )
        self._database: Database | None = None
        self._recovered_library: Path | None = None
        self._manager: GameManager | None = None
        self._launch_manager: LaunchManager | None = None
        self._performance_manager: PerformanceManager | None = None
        self._settings_store: SettingsStore | None = None
        self._cover_manager: CoverManager | None = None
        self._steamgriddb: SteamGridDBClient | None = None
        self._theme_controller: ThemeController | None = None
        self._window: MainWindow | None = None

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        configure_logging()
        LOGGER.info("Starting Minimal Game Launcher")
        Gtk.Window.set_default_icon_name(APP_ID)
        self._load_css()
        # The generated theme provider must come after the static stylesheet so
        # its @define-color overrides win. A missing or invalid themes.ini only
        # falls back to the built-in palette; it never blocks startup.
        self._theme_controller = ThemeController(
            themes_path(), legacy_theme_path()
        )
        self._theme_controller.start()

        ensure_data_directory()
        self._database, self._recovered_library = open_library(database_path())
        self._manager = GameManager(self._database, prefixes_directory())
        self._settings_store = SettingsStore(settings_path())
        self._performance_manager = PerformanceManager()
        self._cover_manager = CoverManager(covers_directory())
        # The client holds no API key: the key is read from local settings on
        # each explicit search so it is never embedded in the composition root.
        self._steamgriddb = SteamGridDBClient()
        # The launch manager reads settings before each Windows launch, so a
        # change in the settings dialog applies without restarting the app.
        # Pidfiles record launched games so a later session can reattach.
        self._launch_manager = LaunchManager(
            prefixes_directory(),
            self._settings_store,
            performance_manager=self._performance_manager,
            pidfiles_root=state_directory() / "processes",
        )

    def do_activate(self) -> None:
        if self._window is None:
            if self._manager is None or self._launch_manager is None:
                raise RuntimeError("Application started without its services.")
            if self._settings_store is None:
                raise RuntimeError("Application started without settings.")
            if self._performance_manager is None:
                raise RuntimeError(
                    "Application started without performance detection."
                )
            if self._cover_manager is None or self._steamgriddb is None:
                raise RuntimeError(
                    "Application started without cover services."
                )
            if self._theme_controller is None:
                raise RuntimeError("Application started without its theme.")
            self._window = MainWindow(
                self,
                self._manager,
                self._launch_manager,
                self._settings_store,
                self._performance_manager,
                self._cover_manager,
                self._steamgriddb,
                self._theme_controller,
                recovered_library=self._recovered_library,
            )
        self._window.present()

    def do_shutdown(self) -> None:
        if self._theme_controller is not None:
            # Remove the CSS provider and cancel the file monitor and any
            # pending reload timer. No library or game state is touched.
            self._theme_controller.shutdown()
            self._theme_controller = None
        if self._launch_manager is not None:
            # Stop main-loop monitoring only. Running games are not terminated
            # and their prefixes remain untouched.
            self._launch_manager.shutdown()
            self._launch_manager = None
        self._performance_manager = None
        if self._database is not None:
            self._database.close()
            self._database = None
        LOGGER.info("Minimal Game Launcher stopped")
        Adw.Application.do_shutdown(self)

    def _load_css(self) -> None:
        """Load the bundled stylesheet if a display is available."""
        display = Gdk.Display.get_default()
        if display is None:
            return
        css_path = Path(__file__).resolve().parent / "ui" / "style.css"
        if not css_path.is_file():
            LOGGER.warning("Stylesheet not found: %s", css_path)
            return
        provider = Gtk.CssProvider()
        provider.load_from_path(str(css_path))
        Gtk.StyleContext.add_provider_for_display(
            display,
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
