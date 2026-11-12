"""GTK construction and lifecycle tests for the v0.6.0 dialogs.

No tool is started and no prefix is touched: the Wine tools service is a stub
and the dialogs are built with real GTK objects but no main loop.
"""

from __future__ import annotations

import gc
import weakref
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

import pytest  # noqa: E402
from gi.repository import Adw, Gdk, Gtk  # noqa: E402

from minimal_game_launcher.database import Database  # noqa: E402
from minimal_game_launcher.models import (  # noqa: E402
    DisplayMode,
    Game,
    GameDraft,
    Platform,
)
from minimal_game_launcher.services.cover_manager import CoverManager  # noqa: E402
from minimal_game_launcher.services.game_manager import GameManager  # noqa: E402
from minimal_game_launcher.services.wine_tools import (  # noqa: E402
    WineToolResult,
    WineToolsAvailability,
)
from minimal_game_launcher.settings import SettingsStore  # noqa: E402
from minimal_game_launcher.ui.game_editor_dialog import (  # noqa: E402
    _DISPLAY_MODES,
    GameEditorDialog,
)
from minimal_game_launcher.ui.hidden_games_dialog import HiddenGamesDialog  # noqa: E402
from minimal_game_launcher.ui.setup_wizard import SetupWizard  # noqa: E402
from minimal_game_launcher.ui.wine_tools_dialog import WineToolsDialog  # noqa: E402

needs_display = pytest.mark.skipif(
    Gdk.Display.get_default() is None,
    reason="constructing GTK widgets needs a GDK display",
)


def _buttons(root: Gtk.Widget) -> list[Gtk.Button]:
    found: list[Gtk.Button] = []
    child = root.get_first_child()
    while child is not None:
        if isinstance(child, Gtk.Button):
            found.append(child)
        found.extend(_buttons(child))
        child = child.get_next_sibling()
    return found


def _row_titles(root: Gtk.Widget) -> list[str]:
    titles: list[str] = []
    child = root.get_first_child()
    while child is not None:
        if isinstance(child, Adw.ActionRow):
            titles.append(child.get_title())
        titles.extend(_row_titles(child))
        child = child.get_next_sibling()
    return titles


def _game(
    *,
    game_id: str = "game-1",
    platform: Platform = Platform.WINDOWS,
    **overrides: object,
) -> Game:
    values: dict[str, object] = {
        "id": game_id,
        "name": "Game",
        "platform": platform,
        "executable": "/games/game.exe",
        "working_directory": "/games",
        "launch_arguments": "",
        "cover_path": None,
        "wine_prefix_path": "/prefixes/game-1",
        "gamemode_enabled": False,
        "mangohud_enabled": False,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    values.update(overrides)
    return Game(**values)  # type: ignore[arg-type]


class StubWineTools:
    def __init__(
        self, *, wine: str | None = "wine", winetricks: str | None = "winetricks"
    ) -> None:
        self.availability = WineToolsAvailability(
            wine=wine, winetricks=winetricks
        )
        self.calls: list[tuple[object, ...]] = []

    def detect(self) -> WineToolsAvailability:
        return self.availability

    def run_winecfg(self, game: Game) -> WineToolResult:
        self.calls.append(("winecfg", game.id))
        return WineToolResult(
            action="Winecfg",
            command=("wine", "winecfg"),
            log_path=Path("/tmp/wine-tools.log"),
            pid=123,
        )

    def run_winetricks(self, game: Game, verbs: str) -> WineToolResult:
        self.calls.append(("winetricks", game.id, verbs))
        return WineToolResult(
            action="Winetricks",
            command=("winetricks", verbs),
            log_path=Path("/tmp/wine-tools.log"),
            pid=124,
        )


@needs_display
def test_hidden_games_dialog_lists_and_restores(tmp_path: Path) -> None:
    database = Database(tmp_path / "library.db")
    manager = GameManager(database, tmp_path / "prefixes")
    try:
        game = manager.add_game(
            GameDraft(
                name="Hidden",
                platform=Platform.LINUX,
                executable="/bin/true",
                show_in_library=False,
            )
        )
        dialog = HiddenGamesDialog(manager)
        changes: list[bool] = []
        dialog.connect("changed", lambda _dialog: changes.append(True))

        assert [row.get_title() for row in dialog._rows] == ["Hidden"]
        assert dialog._show_all.get_visible() is True
        assert dialog._stack.get_visible_child_name() == "list"

        dialog._on_show_clicked(None, game.id)  # type: ignore[arg-type]

        assert manager.get_game(game.id).show_in_library is True  # type: ignore[union-attr]
        assert dialog._rows == []
        assert dialog._stack.get_visible_child_name() == "empty"
        assert changes == [True]
    finally:
        database.close()


@needs_display
def test_hidden_games_dialog_show_all_restores_everything(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "library.db")
    manager = GameManager(database, tmp_path / "prefixes")
    try:
        ids = [
            manager.add_game(
                GameDraft(
                    name=f"Hidden {index}",
                    platform=Platform.LINUX,
                    executable="/bin/true",
                    show_in_library=False,
                )
            ).id
            for index in range(2)
        ]
        dialog = HiddenGamesDialog(manager)

        dialog._on_show_all(None)  # type: ignore[arg-type]

        assert manager.list_hidden_games() == []
        assert all(
            manager.get_game(game_id).show_in_library is True  # type: ignore[union-attr]
            for game_id in ids
        )
    finally:
        database.close()


@needs_display
def test_hidden_games_dialog_is_collected_after_close(tmp_path: Path) -> None:
    database = Database(tmp_path / "library.db")
    manager = GameManager(database, tmp_path / "prefixes")
    try:
        dialog = HiddenGamesDialog(manager)
        weak = weakref.ref(dialog)

        dialog.emit("closed")
        gc.collect()

        assert dialog._rows == []
        assert dialog._group is None
        del dialog
        gc.collect()
        assert weak() is None
    finally:
        database.close()


@needs_display
def test_wine_tools_dialog_disables_missing_tools() -> None:
    dialog = WineToolsDialog(
        _game(),
        StubWineTools(wine=None, winetricks=None),  # type: ignore[arg-type]
    )

    labels = {
        button.get_label(): button for button in _buttons(dialog.get_child())
    }
    assert labels["Open"].get_sensitive() is False
    assert labels["Run"].get_sensitive() is False


@needs_display
def test_wine_tools_dialog_warns_about_a_shared_prefix() -> None:
    dialog = WineToolsDialog(
        _game(wine_prefix_shared=True),
        StubWineTools(),  # type: ignore[arg-type]
    )

    titles = _row_titles(dialog.get_child())
    assert "Shared prefix" in titles
    assert "Wine prefix" in titles


@needs_display
def test_wine_tools_dialog_emits_started_for_winecfg() -> None:
    service = StubWineTools()
    dialog = WineToolsDialog(_game(), service)  # type: ignore[arg-type]
    messages: list[str] = []
    dialog.connect("started", lambda _dialog, message: messages.append(message))

    dialog._on_winecfg_clicked(None)  # type: ignore[arg-type]

    assert service.calls == [("winecfg", "game-1")]
    assert messages == [
        "Winecfg started for Game",
        "Winecfg output: /tmp/wine-tools.log",
    ]


@needs_display
def test_wine_tools_dialog_rejects_empty_verbs() -> None:
    service = StubWineTools()
    dialog = WineToolsDialog(_game(), service)  # type: ignore[arg-type]
    dialog._verbs_row.set_text("   ")

    dialog._on_winetricks_clicked(None)  # type: ignore[arg-type]

    assert service.calls == []
    assert dialog._active_alert is not None


@needs_display
def test_wine_tools_dialog_is_collected_after_close() -> None:
    dialog = WineToolsDialog(_game(), StubWineTools())  # type: ignore[arg-type]
    weak = weakref.ref(dialog)

    dialog.emit("closed")
    gc.collect()

    assert dialog._active_alert is None
    assert dialog._verbs_row is None
    del dialog
    gc.collect()
    assert weak() is None


@needs_display
def test_game_editor_shows_display_gamescope_and_visibility(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "library.db")
    manager = GameManager(database, tmp_path / "prefixes")
    try:
        game = _game(
            display_mode=DisplayMode.VIRTUAL_DESKTOP,
            virtual_desktop_resolution="1280x720",
            gamescope_enabled=True,
            gamescope_resolution="800x600",
            gamescope_output_resolution="1920x1080",
            gamescope_fps_limit=60,
            gamescope_vsync="on",
            gamescope_filter="fsr",
            show_in_library=False,
        )
        window = Gtk.Window()
        try:
            dialog = GameEditorDialog(
                window,
                manager,
                game,
                cover_manager=CoverManager(tmp_path / "covers"),
            )

            assert dialog._display_mode_row.get_selected() == _DISPLAY_MODES.index(
                DisplayMode.VIRTUAL_DESKTOP
            )
            assert dialog._virtual_desktop_row.get_text() == "1280x720"
            assert dialog._gamescope_enabled_row.get_active() is True
            assert dialog._gamescope_row.get_text() == "800x600"
            assert dialog._gamescope_output_row.get_text() == "1920x1080"
            assert dialog._gamescope_fps_row.get_value() == 60
            assert dialog._visible_row.get_active() is False
        finally:
            window.destroy()
    finally:
        database.close()


@needs_display
def test_setup_wizard_is_collected_after_close(tmp_path: Path) -> None:
    wizard = SetupWizard(SettingsStore(tmp_path / "settings.json"))
    weak = weakref.ref(wizard)

    wizard.emit("closed")
    gc.collect()

    assert wizard._stack is None
    assert wizard._strategy_row is None
    del wizard
    gc.collect()
    assert weak() is None


@needs_display
def test_setup_wizard_looks_up_each_tool_once(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[str] = []

    def fake_which(command: str) -> str | None:
        calls.append(command)
        return None

    monkeypatch.setattr(
        "minimal_game_launcher.ui.setup_wizard.shutil.which", fake_which
    )

    SetupWizard(SettingsStore(tmp_path / "settings.json"))

    assert sorted(calls) == [
        "gamemoderun",
        "gamescope",
        "mangohud",
        "wine",
        "wine64",
        "winetricks",
    ], calls
