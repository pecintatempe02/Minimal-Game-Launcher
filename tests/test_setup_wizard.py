"""Tests for first-run setup completion persistence.

The wizard only writes settings. No tool is installed and no prefix is created.
"""

from __future__ import annotations

from pathlib import Path

from minimal_game_launcher.services.desktop_integration import DesktopEntryState
from minimal_game_launcher.settings import (
    LauncherSettings,
    SettingsStore,
    WinePrefixStrategy,
)
from minimal_game_launcher.ui.setup_wizard import SetupWizard


def _store(tmp_path: Path) -> SettingsStore:
    return SettingsStore(tmp_path / "config" / "settings.json")


def test_skip_marks_setup_completed_and_keeps_other_preferences(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.save(
        LauncherSettings(
            wine_prefix_strategy=WinePrefixStrategy.SHARED,
            desktop_entry_enabled=True,
        )
    )
    wizard = SetupWizard(store)
    finished: list[LauncherSettings] = []
    wizard.connect("finished", lambda _dialog, settings: finished.append(settings))

    wizard._on_skip(None)

    stored = store.load()
    assert stored.setup_completed is True
    assert stored.wine_prefix_strategy is WinePrefixStrategy.SHARED
    assert stored.desktop_entry_enabled is True
    assert finished == [stored]


def test_finishing_saves_the_selected_prefix_strategy(tmp_path: Path) -> None:
    store = _store(tmp_path)
    wizard = SetupWizard(store)
    wizard._strategy_row.set_selected(1)

    wizard._finish()

    stored = store.load()
    assert stored.setup_completed is True
    assert stored.wine_prefix_strategy is WinePrefixStrategy.SHARED


def test_skip_does_not_apply_or_persist_the_desktop_toggle(
    tmp_path: Path,
) -> None:
    """Skip must not create an entry while leaving the stored value unchanged."""

    class RecordingDesktop:
        def __init__(self) -> None:
            self.calls: list[object] = []

        def is_enabled(self) -> bool:
            return False

        def apply(self, enabled: bool) -> object:
            self.calls.append(("apply", enabled))
            return DesktopEntryState.DISABLED

        def enable(self) -> object:
            self.calls.append("enable")
            return DesktopEntryState.ENABLED

        def disable(self) -> object:
            self.calls.append("disable")
            return DesktopEntryState.DISABLED

        def remove_owned_entry(self) -> object:
            self.calls.append("remove")
            return DesktopEntryState.ABSENT

    store = _store(tmp_path)
    store.save(LauncherSettings(desktop_entry_enabled=False))
    desktop = RecordingDesktop()
    wizard = SetupWizard(store, desktop_integration=desktop)  # type: ignore[arg-type]
    wizard._desktop_row.set_active(True)

    wizard._on_skip(None)

    stored = store.load()
    assert stored.setup_completed is True
    assert stored.desktop_entry_enabled is False
    assert desktop.calls == []


def test_finishing_applies_the_desktop_toggle(tmp_path: Path) -> None:
    class RecordingDesktop:
        def __init__(self) -> None:
            self.calls: list[object] = []

        def is_enabled(self) -> bool:
            return False

        def apply(self, enabled: bool) -> object:
            self.calls.append(("apply", enabled))
            return DesktopEntryState.ENABLED

        def enable(self) -> object:
            self.calls.append("enable")
            return DesktopEntryState.ENABLED

        def disable(self) -> object:
            self.calls.append("disable")
            return DesktopEntryState.DISABLED

        def remove_owned_entry(self) -> object:
            self.calls.append("remove")
            return DesktopEntryState.ABSENT

    store = _store(tmp_path)
    store.save(LauncherSettings(desktop_entry_enabled=False))
    desktop = RecordingDesktop()
    wizard = SetupWizard(store, desktop_integration=desktop)  # type: ignore[arg-type]
    wizard._desktop_row.set_active(True)

    wizard._finish()

    stored = store.load()
    assert stored.desktop_entry_enabled is True
    assert desktop.calls == [("apply", True)]
