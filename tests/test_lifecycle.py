"""Tests for the signal-ownership lifecycle used by cards and dialogs.

PyGObject keeps a widget alive while one of its child widgets still holds a
handler that references it: ``dialog -> child -> bound method -> dialog``.
These tests assert that disconnecting owned handlers actually makes the
objects collectable, because passing tests alone would not catch the leak.
"""

from __future__ import annotations

import gc
import weakref

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gtk  # noqa: E402

import pytest  # noqa: E402

from minimal_game_launcher.models import Game, Platform  # noqa: E402
from minimal_game_launcher.ui.game_card import GameCard  # noqa: E402
from minimal_game_launcher.ui.game_details_dialog import (  # noqa: E402
    GameDetailsDialog,
)
from minimal_game_launcher.ui.lifecycle import (  # noqa: E402
    DialogSignalOwner,
    SignalOwner,
)

needs_display = pytest.mark.skipif(
    Gdk.Display.get_default() is None,
    reason="constructing GTK widgets needs a GDK display",
)


def _game(game_id: str = "game-1", cover: str | None = None) -> Game:
    return Game(
        id=game_id,
        name=f"Game {game_id}",
        platform=Platform.LINUX,
        executable="/bin/true",
        working_directory="/tmp",
        launch_arguments="",
        cover_path=cover,
        wine_prefix_path=None,
        gamemode_enabled=False,
        mangohud_enabled=False,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )


class _Owner(Gtk.Box, SignalOwner):
    """Minimal owner whose handler references itself through a bound method."""

    def __init__(self) -> None:
        super().__init__()
        self.clicks = 0
        self.button = Gtk.Button(label="click")
        self.connect_owned(self.button, "clicked", self._on_clicked)
        self.append(self.button)

    def _on_clicked(self, _button: Gtk.Button) -> None:
        self.clicks += 1


class _Dialog(Adw.Dialog, DialogSignalOwner):
    """Dialog that follows the real lifecycle pattern."""

    def __init__(self) -> None:
        super().__init__()
        self.releases = 0
        box = Gtk.Box()
        self.button = Gtk.Button(label="click")
        self.connect_owned(self.button, "clicked", self._on_clicked)
        box.append(self.button)
        self.set_child(box)
        self._init_dialog_lifecycle()

    def _on_clicked(self, _button: Gtk.Button) -> None:
        self.close()

    def _release_dialog_references(self) -> None:
        self.releases += 1


# ---------------------------------------------------------------------------
# SignalOwner


@needs_display
def test_connect_owned_keeps_handler_working_until_cleanup() -> None:
    owner = _Owner()
    owner.button.emit("clicked")
    assert owner.clicks == 1

    owner.disconnect_owned_signals()
    owner.button.emit("clicked")
    assert owner.clicks == 1


@needs_display
def test_disconnect_owned_signals_is_idempotent() -> None:
    owner = _Owner()
    owner.disconnect_owned_signals()
    owner.disconnect_owned_signals()
    owner.button.emit("clicked")
    assert owner.clicks == 0


# ---------------------------------------------------------------------------
# GameCard


@needs_display
def test_card_dispose_releases_child_handlers_and_is_idempotent() -> None:
    card = GameCard(_game())
    assert card.disposed is False
    card.set_status("running")
    assert card.running is True

    card.dispose()
    card.dispose()

    assert card.disposed is True
    assert card._launch_button is None
    # Post-disposal status updates must not touch released widgets.
    card.set_status("running")
    assert card.running is True


@needs_display
def test_card_is_collected_after_dispose_and_removal() -> None:
    flow = Gtk.FlowBox()
    card = GameCard(_game())
    flow.append(card)
    weak = weakref.ref(card)

    card.dispose()
    flow.remove_all()
    del card
    gc.collect()

    assert weak() is None


@needs_display
def test_repeated_library_rebuilds_do_not_retain_cards() -> None:
    """Three full rebuilds of a 120-game library must leave nothing behind."""
    flow = Gtk.FlowBox()
    references: list[weakref.ref[GameCard]] = []

    for _round in range(3):
        cards = [GameCard(_game(f"game-{index}")) for index in range(120)]
        for index in range(len(cards)):
            flow.append(cards[index])
        for index in range(len(cards)):
            cards[index].dispose()
        flow.remove_all()
        references.extend(weakref.ref(card) for card in cards)
        del cards
        gc.collect()

    assert [ref() for ref in references if ref() is not None] == []


@needs_display
def test_card_launch_action_still_fires_before_dispose() -> None:
    card = GameCard(_game())
    received: list[Game] = []
    card.connect("launch-requested", lambda _card, game: received.append(game))

    card._launch_button.emit("clicked")

    assert received == [card.game]


# ---------------------------------------------------------------------------
# Dialogs


@needs_display
def test_dialog_releases_references_and_is_collected_after_close() -> None:
    dialog = _Dialog()
    weak = weakref.ref(dialog)

    # Adw emits ``closed`` when the dialog is dismissed; emitting it directly
    # exercises the same lifecycle hook without needing a mapped window.
    dialog.emit("closed")
    gc.collect()

    assert dialog.releases == 1
    del dialog
    gc.collect()
    assert weak() is None


@needs_display
def test_details_dialog_is_collected_after_close() -> None:
    dialog = GameDetailsDialog(_game())
    weak = weakref.ref(dialog)

    dialog.emit("closed")
    del dialog
    gc.collect()

    assert weak() is None
