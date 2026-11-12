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
    data_directory,
    database_path,
    ensure_data_directory,
    games_directory,
    installers_directory,
    legacy_theme_path,
    log_path,
    prefixes_directory,
    settings_path,
    state_directory,
    themes_path,
)
from minimal_game_launcher.services.cover_manager import CoverManager  # noqa: E402
from minimal_game_launcher.services.cleanup import CleanupService  # noqa: E402
from minimal_game_launcher.services.desktop_integration import (  # noqa: E402
    DesktopIntegrationService,
)
from minimal_game_launcher.services.diagnostics import (  # noqa: E402
    DiagnosticsService,
)
from minimal_game_launcher.services.game_manager import GameManager  # noqa: E402
from minimal_game_launcher.services.installer_manager import (  # noqa: E402
    InstallerManager,
)
from minimal_game_launcher.services.iso_mount import IsoMountService  # noqa: E402
from minimal_game_launcher.services.launch_manager import LaunchManager  # noqa: E402
from minimal_game_launcher.services.ownership import (  # noqa: E402
    OwnershipService,
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
        self._diagnostics: DiagnosticsService | None = None
        self._ownership: OwnershipService | None = None
        self._wine_tools: WineToolsService | None = None
        self._iso_mount: IsoMountService | None = None
        self._installer_manager: InstallerManager | None = None
        self._cleanup: CleanupService | None = None
        self._desktop_integration: DesktopIntegrationService | None = None
        self._theme_controller: ThemeController | None = None
        self._window: MainWindow | None = None

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        configure_logging()
        LOGGER.info("Starting Minimal Game Launcher")
        Gtk.Window.set_default_icon_name(APP_ID)
        self._load_icon()
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
        self._settings_store = SettingsStore(settings_path())
        # Desktop integration is opt-in. A ``None`` preference never touches the
        # filesystem; only an explicit True/False reconciles at startup.
        self._desktop_integration = DesktopIntegrationService()
        try:
            self._desktop_integration.reconcile(
                self._settings_store.load().desktop_entry_enabled
            )
        except Exception:
            LOGGER.exception("Could not reconcile the user desktop entry")
        self._manager = GameManager(
            self._database,
            prefixes_directory(),
            settings_store=self._settings_store,
        )
        self._performance_manager = PerformanceManager()
        self._cover_manager = CoverManager(covers_directory())
        # The client holds no API key: the key is read from local settings on
        # each explicit search so it is never embedded in the composition root.
        self._steamgriddb = SteamGridDBClient()
        # Ownership classification is shared by diagnostics now and by the
        # future installer and cleanup flows. It never creates directories.
        self._ownership = OwnershipService(
            prefixes_directory(),
            games_directory(),
            installers_directory(),
        )
        # Diagnostics are read-only and run only when explicitly requested.
        self._diagnostics = DiagnosticsService(
            prefixes_directory(),
            self._settings_store,
            ownership=self._ownership,
            pidfiles_root=state_directory() / "processes",
            log_path=log_path(),
            data_directory=data_directory(),
            launch_logs_root=state_directory() / "launch-logs",
        )
        # Winecfg and Winetricks are optional and only start on explicit user
        # action. They refuse prefixes the launcher does not manage.
        self._wine_tools = WineToolsService(
            prefixes_directory(),
            self._settings_store,
            ownership=self._ownership,
            state_root=state_directory(),
        )
        # ISO access is mounted read-only through UDisks. Staging and installer
        # session state are managed separately from the final game directory.
        self._iso_mount = IsoMountService()
        self._installer_manager = InstallerManager(
            self._database,
            self._ownership,
            self._iso_mount,
            settings_store=self._settings_store,
        )
        # The launch manager reads settings before each Windows launch, so a
        # change in the settings dialog applies without restarting the app.
        # Pidfiles record launched games so a later session can reattach.
        self._launch_manager = LaunchManager(
            prefixes_directory(),
            self._settings_store,
            performance_manager=self._performance_manager,
            pidfiles_root=state_directory() / "processes",
        )
        # Cleanup previews and orphan scans use the shared ownership service.
        # Running games are detected through the launch manager, and no
        # automatic deletion or background scanning is performed.
        self._cleanup = CleanupService(
            self._database,
            self._ownership,
            is_game_active=self._launch_manager.is_active,
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
            if self._diagnostics is None:
                raise RuntimeError(
                    "Application started without diagnostic services."
                )
            if self._installer_manager is None:
                raise RuntimeError(
                    "Application started without installer services."
                )
            if self._cleanup is None:
                raise RuntimeError(
                    "Application started without cleanup services."
                )
            if self._theme_controller is None:
                raise RuntimeError("Application started without its theme.")
            if self._desktop_integration is None:
                raise RuntimeError(
                    "Application started without desktop integration."
                )
            if self._wine_tools is None:
                raise RuntimeError(
                    "Application started without Wine tool services."
                )
            self._window = MainWindow(
                self,
                self._manager,
                self._launch_manager,
                self._settings_store,
                self._performance_manager,
                self._cover_manager,
                self._steamgriddb,
                self._theme_controller,
                diagnostics=self._diagnostics,
                installer_manager=self._installer_manager,
                cleanup=self._cleanup,
                desktop_integration=self._desktop_integration,
                wine_tools=self._wine_tools,
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
        if self._iso_mount is not None:
            # Release only mounts this application created and verified.
            self._iso_mount.shutdown()
            self._iso_mount = None
        self._installer_manager = None
        self._cleanup = None
        self._desktop_integration = None
        self._wine_tools = None
        self._performance_manager = None
        if self._database is not None:
            self._database.close()
            self._database = None
        LOGGER.info("Minimal Game Launcher stopped")
        Adw.Application.do_shutdown(self)

    def _load_icon(self) -> None:
        """Use the installed theme icon if available, else load the bundled SVG.

        This preserves native GTK integration when the host has the icon
        installed, and falls back to a package-relative file inside an
        AppImage or portable build where the host theme does not know the
        application id.
        """
        display = Gdk.Display.get_default()
        icon_path = Path(__file__).resolve().parent / "ui" / f"{APP_ID}.svg"
        if display is not None:
            theme = Gtk.IconTheme.get_for_display(display)
            if not theme.has_icon(APP_ID) and icon_path.is_file():
                theme.add_search_path(str(icon_path.parent))

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
