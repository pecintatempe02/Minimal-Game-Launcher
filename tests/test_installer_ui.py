"""GTK smoke tests for executable registration in the installer dialog.

The dialog is constructed with real GTK objects but no main loop. Discovery
results are injected directly so the test stays deterministic and performs no
filesystem scanning.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import replace
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

import pytest  # noqa: E402
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from minimal_game_launcher.database import Database  # noqa: E402
from minimal_game_launcher.models import (  # noqa: E402
    InstallerSession,
    InstallerStatus,
)
from minimal_game_launcher.services.executable_discovery import (  # noqa: E402
    ExecutableCandidate,
)
from minimal_game_launcher.services.game_manager import GameManager  # noqa: E402
from minimal_game_launcher.services.installer_manager import (  # noqa: E402
    InstallerManager,
)
from minimal_game_launcher.services.launch_manager import LaunchManager  # noqa: E402
from minimal_game_launcher.services.ownership import OwnershipService  # noqa: E402
from minimal_game_launcher.settings import SettingsStore  # noqa: E402
from minimal_game_launcher.ui.installer_dialog import (  # noqa: E402
    InstallerDialog,
    PendingInstallsDialog,
)


class _FakeMount:
    def validate_iso(self, iso_path: str | Path) -> Path:
        return Path(iso_path).resolve()


def test_candidate_selection_and_cancel_do_not_register(
    tmp_path: Path, database: Database
) -> None:
    if not Gtk.init_check():
        pytest.skip("No GTK display is available.")

    ownership = OwnershipService(
        tmp_path / "prefixes", tmp_path / "games", tmp_path / "installers"
    )
    installers = InstallerManager(database, ownership, _FakeMount())
    games = GameManager(database, tmp_path / "prefixes")
    launch = LaunchManager(
        tmp_path / "prefixes", SettingsStore(tmp_path / "settings.json")
    )
    iso = tmp_path / "disc.iso"
    iso.write_bytes(b"iso")
    session = installers.save_session(
        replace(
            installers.create_session(iso, "Test Game"),
            status=InstallerStatus.INSTALLER_FINISHED,
        )
    )

    dialog = InstallerDialog(
        Gtk.Window(), installers, games, launch, session=None
    )
    try:
        dialog._session = session
        dialog._set_page("finish")
        candidate = ExecutableCandidate(
            path=tmp_path / "Test Game.exe",
            relative_path="drive_c/Program Files/Test Game/Test Game.exe",
            location="Program Files",
            score=100,
            recommended=True,
            reason="Found in Program Files.",
        )
        dialog._on_discovery_ready(session.game_id, [candidate])

        assert len(dialog._discovery_rows) == 1
        row = next(iter(dialog._discovery_rows))
        dialog._discovery_list.select_row(row)
        assert dialog._selected_executable == candidate
        assert dialog._primary_button.get_sensitive() is True

        dialog._on_defer_clicked(None)

        assert database.list_games() == []
        stored = database.get_installer_session(session.game_id)
        assert stored is not None
        assert stored.status is InstallerStatus.INSTALLER_FINISHED
    finally:
        dialog.close()


def test_pending_install_removal_shows_result_and_refreshes(
    tmp_path: Path, database: Database
) -> None:
    if not Gtk.init_check():
        pytest.skip("No GTK display is available.")

    installers = InstallerManager(
        database,
        OwnershipService(
            tmp_path / "prefixes", tmp_path / "games", tmp_path / "installers"
        ),
        _FakeMount(),
    )
    iso = tmp_path / "disc.iso"
    iso.write_bytes(b"iso")
    session = installers.save_session(
        replace(installers.create_session(iso, "Test Game"))
    )
    staging = Path(session.staging_directory)
    (staging / "source").mkdir(parents=True)
    (staging / "source" / "setup.exe").write_bytes(b"MZ")
    prefix = installers.prefix_path(session)
    prefix.mkdir(parents=True)

    dialog = PendingInstallsDialog(installers, is_game_active=lambda _id: False)
    events: list[str] = []
    dialog.connect("removed", lambda _dialog, value: events.append(value.game_id))
    try:
        body = dialog.confirmation_body(session)
        assert str(staging) in body
        assert str(prefix) in body
        assert "ISO image" in body
        assert "Remove session only" in body
        assert "Remove files and session" in body

        report = installers.remove_pending_install(session, authorized=True)
        dialog._on_removed(session, report)

        assert events == [session.game_id]
        assert database.get_installer_session(session.game_id) is None
        assert not staging.exists()
        assert not prefix.exists()
        assert dialog._active_alert is not None
    finally:
        dialog.close()


def _write_executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"MZ")
    return path


def _staged_session(
    tmp_path: Path, installers: InstallerManager
) -> InstallerSession:
    iso = tmp_path / "disc.iso"
    iso.write_bytes(b"iso")
    session = installers.create_session(iso, "Test Game")
    staging = Path(session.staging_directory)
    source = staging / "source"
    source.mkdir(parents=True)
    payload = b"MZ"
    (source / "setup.exe").write_bytes(payload)
    manifest = {
        "version": 1,
        "iso_path": str(iso),
        "files": [
            {
                "path": "setup.exe",
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ],
        "directories": [],
    }
    (staging / ".mgl-staging.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return installers.save_session(
        replace(
            session,
            status=InstallerStatus.STAGED,
            installer_relative_path="setup.exe",
        )
    )


def test_zero_candidates_keeps_manual_picker_and_registers_working_directory(
    tmp_path: Path, database: Database
) -> None:
    if not Gtk.init_check():
        pytest.skip("No GTK display is available.")

    ownership = OwnershipService(
        tmp_path / "prefixes", tmp_path / "games", tmp_path / "installers"
    )
    installers = InstallerManager(database, ownership, _FakeMount())
    games = GameManager(database, tmp_path / "prefixes")
    launch = LaunchManager(
        tmp_path / "prefixes", SettingsStore(tmp_path / "settings.json")
    )
    iso = tmp_path / "disc.iso"
    iso.write_bytes(b"iso")
    session = installers.save_session(
        replace(
            installers.create_session(iso, "Test Game"),
            status=InstallerStatus.INSTALLER_FINISHED,
            message="Installer exited with code 0.",
        )
    )

    dialog = InstallerDialog(
        Gtk.Window(), installers, games, launch, session=None
    )
    errors: list[str] = []
    dialog._show_error = lambda message, **_kwargs: errors.append(message)
    try:
        dialog._session = session
        dialog._set_page("finish")
        dialog._on_discovery_ready(session.game_id, [])

        assert dialog._selected_executable is None
        assert dialog._primary_button.get_sensitive() is False
        assert dialog._manual_button.get_visible() is True
        assert "No likely" in dialog._discovery_status.get_title()
        assert database.list_games() == []
        stored = database.get_installer_session(session.game_id)
        assert stored is not None
        assert stored.status is InstallerStatus.INSTALLER_FINISHED
        assert stored.message == "Installer exited with code 0."

        class _CancelledDialog:
            def open_finish(self, _result: object) -> None:
                raise GLib.Error.new_literal(
                    Gio.io_error_quark(),
                    "cancelled",
                    Gio.IOErrorEnum.CANCELLED,
                )

        dialog._on_game_executable_selected(_CancelledDialog(), None)
        assert errors == []
        assert database.list_games() == []
        assert database.get_installer_session(session.game_id).status is (
            InstallerStatus.INSTALLER_FINISHED
        )

        invalid = tmp_path / "manual.txt"
        invalid.write_text("not a game", encoding="utf-8")
        dialog._register_executable(invalid)
        assert database.list_games() == []
        assert errors and "exe" in errors[-1].lower()

        executable = _write_executable(tmp_path / "manual" / "Test Game.exe")
        dialog._register_executable(executable)

        game = database.get_game(session.game_id)
        assert game is not None
        assert game.executable == str(executable)
        assert game.working_directory == str(executable.parent)
        assert database.get_installer_session(session.game_id).status is (
            InstallerStatus.INSTALLED
        )
    finally:
        dialog.close()


def test_worker_threads_do_not_touch_the_gtk_thread_sqlite_connection(
    tmp_path: Path, database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not Gtk.init_check():
        pytest.skip("No GTK display is available.")

    installers = InstallerManager(
        database,
        OwnershipService(
            tmp_path / "prefixes", tmp_path / "games", tmp_path / "installers"
        ),
        _FakeMount(),
    )
    session = _staged_session(tmp_path, installers)
    dialog = InstallerDialog(
        Gtk.Window(),
        installers,
        GameManager(database, tmp_path / "prefixes"),
        LaunchManager(
            tmp_path / "prefixes", SettingsStore(tmp_path / "settings.json")
        ),
        session=None,
    )
    dialog._session = session

    callbacks: list[str] = []
    monkeypatch.setattr(
        GLib,
        "idle_add",
        lambda function, *args: callbacks.append(function.__name__),
    )
    failures: list[BaseException] = []

    def run_workers() -> None:
        try:
            dialog._resume_worker(session)
            dialog._discovery_worker(session)
        except BaseException as error:  # pragma: no cover - defensive
            failures.append(error)

    try:
        thread = threading.Thread(target=run_workers)
        thread.start()
        thread.join(10)

        assert not thread.is_alive()
        assert failures == []
        assert callbacks == ["_on_resume_ready", "_on_discovery_ready"]
    finally:
        dialog.close()
