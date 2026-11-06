"""Tests for game-ID reservation and cover replacement through GameManager."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import gi

gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, GdkPixbuf, GLib, Gtk  # noqa: E402

import pytest  # noqa: E402

from minimal_game_launcher.errors import GameNotFoundError
from minimal_game_launcher.models import GameDraft, Platform
from minimal_game_launcher.services.cover_manager import CoverManager
from minimal_game_launcher.services.game_manager import GameManager
from minimal_game_launcher.ui.cover import DEFAULT_CACHE, load_cover_texture
from minimal_game_launcher.ui.game_card import GameCard
from minimal_game_launcher.ui.main_window import MainWindow

needs_display = pytest.mark.skipif(
    Gdk.Display.get_default() is None,
    reason="constructing GTK widgets needs a GDK display",
)


def _draft(executable: Path) -> GameDraft:
    return GameDraft(
        name="Cover Game",
        platform=Platform.LINUX,
        executable=str(executable),
    )


def _valid_cover(
    tmp_path: Path,
    name: str = "cover.png",
    *,
    width: int = 32,
    height: int = 32,
) -> Path:
    """Write a decodable PNG accepted by CoverManager."""
    pixbuf = GdkPixbuf.Pixbuf.new(
        GdkPixbuf.Colorspace.RGB, False, 8, width, height
    )
    pixbuf.fill(0xFF0000FF)
    success, buffer = pixbuf.save_to_bufferv("png", [], [])
    assert success
    path = tmp_path / name
    path.write_bytes(bytes(buffer))
    return path


@needs_display
@pytest.mark.parametrize("window_size", [(753, 794), (620, 650)])
def test_game_card_cover_stays_contained_in_its_fixed_frame(
    manager: GameManager,
    make_executable,
    tmp_path: Path,
    window_size: tuple[int, int],
) -> None:
    cover = _valid_cover(tmp_path, width=320, height=120)
    game = manager.add_game(
        GameDraft(
            name="Touhou 15.5 Antinomy of Common Flowers",
            platform=Platform.LINUX,
            executable=str(make_executable()),
            cover_path=str(cover),
        )
    )

    card = GameCard(game)
    content = card.get_child()
    overlay = content.get_first_child()
    frame = overlay.get_child()
    picture = frame.get_first_child().get_next_sibling()

    assert picture.get_content_fit() == Gtk.ContentFit.COVER
    assert card.get_halign() == Gtk.Align.START
    assert card.get_hexpand() is False
    assert content.get_halign() == Gtk.Align.START
    assert picture.get_can_shrink() is True
    assert picture.get_size_request() == (
        GameCard.COVER_WIDTH,
        GameCard.COVER_HEIGHT,
    )
    assert frame.get_size_request() == (
        GameCard.COVER_WIDTH,
        GameCard.COVER_HEIGHT,
    )
    assert all(
        not widget.get_hexpand() and not widget.get_vexpand()
        for widget in (content, overlay, frame, picture)
    )

    second_game = manager.add_game(
        GameDraft(
            name="PlantsVsZombies",
            platform=Platform.WINDOWS,
            executable=str(make_executable("second-game.exe")),
        )
    )
    third_game = manager.add_game(
        GameDraft(
            name="Osu",
            platform=Platform.LINUX,
            executable=str(make_executable("third-game")),
        )
    )

    css = Gtk.CssProvider()
    css.load_from_data(
        b".game-card-child { padding: 0; } .game-card { padding: 8px; }"
    )
    display = Gdk.Display.get_default()
    Gtk.StyleContext.add_provider_for_display(
        display, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )
    flow = Gtk.FlowBox()
    flow.set_homogeneous(False)
    flow.set_column_spacing(0)
    flow.set_row_spacing(0)
    flow.set_valign(Gtk.Align.START)
    flow.set_halign(Gtk.Align.START)
    flow.set_max_children_per_line(12)
    flow.set_min_children_per_line(1)
    cards = [card, GameCard(second_game), GameCard(third_game)]
    for game_card in cards:
        flow.append(game_card)

    library_content = Adw.Clamp()
    library_content.set_maximum_size(1100)
    library_content.set_tightening_threshold(800)
    library_content.set_child(flow)

    scroller = Gtk.ScrolledWindow()
    scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    scroller.set_child(library_content)
    window = Gtk.Window()
    window.set_default_size(*window_size)
    window.set_child(scroller)
    window.present()
    loop = GLib.MainLoop()
    GLib.timeout_add(350, lambda: (loop.quit(), GLib.SOURCE_REMOVE)[1])
    loop.run()
    try:
        assert card.get_width() <= GameCard.COVER_WIDTH + 64
        allocations = [game_card.get_allocation() for game_card in cards]
        assert allocations[1].x - allocations[0].x == allocations[0].width
        assert allocations[2].x - allocations[1].x == allocations[1].width
    finally:
        window.close()
        Gtk.StyleContext.remove_provider_for_display(display, css)


def test_new_game_id_is_unique(manager: GameManager) -> None:
    ids = {manager.new_game_id() for _ in range(20)}

    assert len(ids) == 20


def test_add_game_accepts_a_reserved_id(
    manager: GameManager, make_executable
) -> None:
    executable = make_executable()
    game_id = manager.new_game_id()

    game = manager.add_game(_draft(executable), game_id=game_id)

    assert game.id == game_id
    assert manager.get_game(game_id) == game


def test_set_cover_persists_a_cached_path(
    manager: GameManager,
    make_executable,
    tmp_path: Path,
) -> None:
    game = manager.add_game(_draft(make_executable()))
    cover_manager = CoverManager(tmp_path / "covers")
    cached = cover_manager.import_local(game.id, _valid_cover(tmp_path))

    updated = manager.set_cover(game.id, cached)

    assert updated.cover_path == cached
    assert manager.get_game(game.id).cover_path == cached


def test_set_cover_replaces_and_clears(
    manager: GameManager,
    make_executable,
    tmp_path: Path,
) -> None:
    game = manager.add_game(_draft(make_executable()))
    cover_manager = CoverManager(tmp_path / "covers")
    first = cover_manager.import_local(
        game.id, _valid_cover(tmp_path, "first.png")
    )
    second = cover_manager.import_local(
        game.id, _valid_cover(tmp_path, "second.png")
    )

    manager.set_cover(game.id, first)
    replaced = manager.set_cover(game.id, second)

    assert replaced.cover_path == second
    assert cover_manager.remove_cover(first) is True
    assert Path(second).is_file()

    cleared = manager.set_cover(game.id, None)

    assert cleared.cover_path is None
    assert manager.get_game(game.id).cover_path is None


def test_set_cover_rejects_unknown_games(manager: GameManager) -> None:
    with pytest.raises(GameNotFoundError):
        manager.set_cover("missing", "/tmp/cover.png")


class _WindowStub:
    """Stand-in for the MainWindow parts used by the cover handlers."""

    def __init__(self, manager: GameManager, cover_manager: CoverManager) -> None:
        self._manager = manager
        self._cover_manager = cover_manager
        self._active_cover_dialog = object()
        self.toasts: list[str] = []
        self.errors: list[str] = []

    def _refresh_library(self) -> None:
        pass

    def _toast(self, message: str) -> None:
        self.toasts.append(message)

    def _show_error(self, message: str, heading: str | None = None) -> None:
        self.errors.append(message)


def test_replacing_a_cover_invalidates_only_the_affected_textures(
    manager: GameManager, make_executable, tmp_path: Path
) -> None:
    game = manager.add_game(_draft(make_executable()))
    cover_manager = CoverManager(tmp_path / "covers")
    old_cover = cover_manager.import_local(
        game.id, _valid_cover(tmp_path, "old.png")
    )
    manager.set_cover(game.id, old_cover)
    unrelated = cover_manager.import_local(
        "another-game", _valid_cover(tmp_path, "unrelated.png")
    )

    DEFAULT_CACHE.clear()
    try:
        assert load_cover_texture(old_cover, 60, 90) is not None
        assert load_cover_texture(unrelated, 60, 90) is not None
        assert DEFAULT_CACHE.size() == 2

        new_cover = cover_manager.import_local(
            game.id, _valid_cover(tmp_path, "new.png")
        )
        assert load_cover_texture(new_cover, 60, 90) is not None
        assert DEFAULT_CACHE.size() == 3

        window = _WindowStub(manager, cover_manager)
        dialog = SimpleNamespace(game=manager.get_game(game.id))
        MainWindow._on_cover_selected(window, dialog, new_cover)

        assert window.errors == []
        assert manager.get_game(game.id).cover_path == new_cover
        assert not Path(old_cover).exists()
        # Old and new entries were dropped; the unrelated cover survives.
        assert DEFAULT_CACHE.size() == 1
        assert DEFAULT_CACHE.invalidate(new_cover) is False
        assert load_cover_texture(old_cover, 60, 90) is None
        assert load_cover_texture(unrelated, 60, 90) is not None
    finally:
        DEFAULT_CACHE.clear()


def test_clearing_a_cover_invalidates_the_cached_texture(
    manager: GameManager, make_executable, tmp_path: Path
) -> None:
    game = manager.add_game(_draft(make_executable()))
    cover_manager = CoverManager(tmp_path / "covers")
    old_cover = cover_manager.import_local(
        game.id, _valid_cover(tmp_path, "cleared.png")
    )
    manager.set_cover(game.id, old_cover)

    DEFAULT_CACHE.clear()
    try:
        assert load_cover_texture(old_cover, 60, 90) is not None
        assert DEFAULT_CACHE.size() == 1

        window = _WindowStub(manager, cover_manager)
        dialog = SimpleNamespace(game=manager.get_game(game.id))
        MainWindow._on_cover_cleared(window, dialog)

        assert window.errors == []
        assert manager.get_game(game.id).cover_path is None
        assert not Path(old_cover).exists()
        assert DEFAULT_CACHE.size() == 0
        assert load_cover_texture(old_cover, 60, 90) is None
    finally:
        DEFAULT_CACHE.clear()
