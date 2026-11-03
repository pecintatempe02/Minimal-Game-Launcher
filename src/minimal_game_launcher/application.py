"""GTK application lifecycle and composition root."""

from __future__ import annotations

import logging
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, Gtk  # noqa: E402

from minimal_game_launcher import APP_ID, APP_NAME  # noqa: E402
from minimal_game_launcher.database import Database  # noqa: E402
from minimal_game_launcher.paths import ensure_data_directory, database_path, prefixes_directory  # noqa: E402
from minimal_game_launcher.services.game_manager import GameManager  # noqa: E402
from minimal_game_launcher.ui.main_window import MainWindow  # noqa: E402

LOGGER = logging.getLogger(__name__)


class MinimalGameLauncherApplication(Adw.Application):
    """Own application startup, shared services, and the main window."""

    def __init__(self) -> None:
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )
        self._database: Database | None = None
        self._manager: GameManager | None = None
        self._window: MainWindow | None = None

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        Gtk.Window.set_default_icon_name(APP_ID)
        self._load_css()

        ensure_data_directory()
        self._database = Database(database_path())
        self._manager = GameManager(self._database, prefixes_directory())

    def do_activate(self) -> None:
        if self._window is None:
            if self._manager is None:
                raise RuntimeError("Application started without a game manager.")
            self._window = MainWindow(self, self._manager)
        self._window.present()

    def do_shutdown(self) -> None:
        if self._database is not None:
            self._database.close()
            self._database = None
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
