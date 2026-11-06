"""Tests for removing a pending installation and its managed files."""

from __future__ import annotations

import shutil
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from minimal_game_launcher.database import Database
from minimal_game_launcher.errors import InstallerError
from minimal_game_launcher.models import InstallerStatus
from minimal_game_launcher.services.installer_manager import InstallerManager
from minimal_game_launcher.services.ownership import (
    OwnershipService,
    ResourceKind,
)
from minimal_game_launcher.ui.installer_dialog import removal_report_text


class _FakeMount:
    def validate_iso(self, iso_path: str | Path) -> Path:
        path = Path(iso_path).expanduser()
        if not path.is_file():
            raise FileNotFoundError(path)
        return path.resolve()


def _setup(
    tmp_path: Path, database: Database, *, external: bool = False
):
    ownership = OwnershipService(
        tmp_path / "prefixes", tmp_path / "games", tmp_path / "installers"
    )
    manager = InstallerManager(database, ownership, _FakeMount())
    iso = tmp_path / "disc.iso"
    iso.write_bytes(b"iso")
    session = manager.create_session(iso, "Test Game")

    staging = Path(session.staging_directory)
    staging.mkdir(parents=True)
    (staging / "source").mkdir()
    (staging / "source" / "setup.exe").write_bytes(b"MZ")
    prefix = manager.prefix_path(session)
    (prefix / "drive_c" / "Program Files").mkdir(parents=True)
    (prefix / "drive_c" / "Program Files" / "Test Game.exe").write_bytes(b"MZ")

    if external:
        destination = tmp_path / "external"
        destination.mkdir()
        (destination / "Test Game.exe").write_bytes(b"MZ")
        session = replace(
            session,
            install_directory=str(destination),
            install_directory_owned=False,
        )
    else:
        destination = manager.resolve_destination(
            session, managed=True, create=True
        ).path
        assert destination is not None
        (destination / "Test Game.exe").write_bytes(b"MZ")
        session = replace(
            session,
            install_directory=str(destination),
            install_directory_owned=True,
        )

    session = manager.save_session(replace(session, status=InstallerStatus.STAGED))
    return manager, ownership, session, destination


def test_removes_staging_owned_destination_and_prefix(
    tmp_path: Path, database: Database
) -> None:
    manager, ownership, session, destination = _setup(tmp_path, database)
    staging = Path(session.staging_directory)
    prefix = manager.prefix_path(session)

    report = manager.remove_pending_install(session, authorized=True)

    assert report.complete is True
    assert report.session_removed is True
    assert report.failed == ()
    assert not staging.exists()
    assert not destination.exists()
    assert not prefix.exists()
    assert database.get_installer_session(session.game_id) is None
    assert {item.kind for item in report.deleted} == {
        ResourceKind.INSTALLER_SOURCE,
        ResourceKind.PREFIX,
        ResourceKind.INSTALLATION,
    }

    for kind, path in manager.session_resources(session):
        assert not ownership.classify(kind, session.game_id, path).is_managed


def test_missing_files_still_remove_the_session(
    tmp_path: Path, database: Database
) -> None:
    manager, _ownership, session, destination = _setup(tmp_path, database)
    shutil.rmtree(session.staging_directory)
    shutil.rmtree(destination)
    shutil.rmtree(manager.prefix_path(session))

    report = manager.remove_pending_install(session, authorized=True)

    assert report.complete is True
    assert report.session_removed is True
    assert len(report.missing) == 3
    assert report.deleted == ()
    assert report.errors == ()
    assert database.get_installer_session(session.game_id) is None


def test_missing_iso_is_irrelevant_to_removal(
    tmp_path: Path, database: Database
) -> None:
    manager, _ownership, session, _destination = _setup(tmp_path, database)
    Path(session.iso_path).unlink()

    report = manager.remove_pending_install(session, authorized=True)

    assert report.complete is True
    assert database.get_installer_session(session.game_id) is None


def test_external_destination_is_retained(
    tmp_path: Path, database: Database
) -> None:
    manager, _ownership, session, destination = _setup(
        tmp_path, database, external=True
    )
    assert destination is not None
    executable = destination / "Test Game.exe"

    report = manager.remove_pending_install(session, authorized=True)

    assert report.complete is True
    assert [item.path for item in report.retained] == [destination]
    assert executable.read_bytes() == b"MZ"
    assert ResourceKind.INSTALLATION not in {
        item.kind for item in report.deleted
    }
    assert database.get_installer_session(session.game_id) is None


def test_running_installer_is_refused(
    tmp_path: Path, database: Database
) -> None:
    manager, _ownership, session, _destination = _setup(tmp_path, database)

    with pytest.raises(InstallerError, match="still running"):
        manager.remove_pending_install(session, authorized=True, game_running=True)

    assert database.get_installer_session(session.game_id) is not None
    assert Path(session.staging_directory).is_dir()


def test_removal_requires_authorization(
    tmp_path: Path, database: Database
) -> None:
    manager, _ownership, session, _destination = _setup(tmp_path, database)

    with pytest.raises(InstallerError, match="authorization"):
        manager.remove_pending_install(session, authorized=False)

    assert database.get_installer_session(session.game_id) is not None


def test_failure_keeps_the_session_for_retry(
    tmp_path: Path, database: Database
) -> None:
    manager, ownership, session, _destination = _setup(tmp_path, database)

    def fail(*_args, **_kwargs):
        raise OSError("simulated failure")

    ownership.delete = fail  # type: ignore[method-assign]

    report = manager.remove_pending_install(session, authorized=True)

    assert report.complete is False
    assert report.session_removed is False
    assert "simulated failure" in " ".join(report.errors)
    assert database.get_installer_session(session.game_id) is not None


def test_removal_leaves_other_sessions_untouched(
    tmp_path: Path, database: Database
) -> None:
    manager, _ownership, session, _destination = _setup(tmp_path, database)
    iso = tmp_path / "other.iso"
    iso.write_bytes(b"iso")
    other = manager.save_session(manager.create_session(iso, "Other Game"))
    other_staging = Path(other.staging_directory)
    other_staging.mkdir(parents=True)

    report = manager.remove_pending_install(session, authorized=True)

    assert report.complete is True
    assert other_staging.is_dir()
    assert database.get_installer_session(other.game_id) is not None


def test_session_only_removal_keeps_every_file(
    tmp_path: Path, database: Database
) -> None:
    manager, _ownership, session, destination = _setup(tmp_path, database)
    staging = Path(session.staging_directory)
    prefix = manager.prefix_path(session)

    report = manager.remove_pending_install(
        session, authorized=True, delete_files=False
    )

    assert report.complete is True
    assert report.session_removed is True
    assert report.files_kept is True
    assert report.items == ()
    assert staging.is_dir()
    assert destination.is_dir()
    assert prefix.is_dir()
    assert database.get_installer_session(session.game_id) is None


def test_session_only_report_text_says_files_were_kept(
    tmp_path: Path, database: Database
) -> None:
    manager, _ownership, session, _destination = _setup(tmp_path, database)

    report = manager.remove_pending_install(
        session, authorized=True, delete_files=False
    )
    text = removal_report_text(report)

    assert "The pending install record was removed." in text
    assert "Every staged file was kept on disk." in text
    assert "Deleted:" not in text


def test_worker_removal_defers_session_delete_to_caller(
    tmp_path: Path, database: Database
) -> None:
    """The SQLite connection is thread-bound, so a worker must not delete rows."""
    manager, _ownership, session, destination = _setup(tmp_path, database)
    staging = Path(session.staging_directory)
    prefix = manager.prefix_path(session)
    reports: list = []

    def work() -> None:
        reports.append(
            manager.remove_pending_install(
                session, authorized=True, delete_session=False
            )
        )

    thread = threading.Thread(target=work)
    thread.start()
    thread.join()

    report = reports[0]
    assert report.errors == ()
    assert report.session_removed is False
    assert not staging.exists()
    assert not destination.exists()
    assert not prefix.exists()
    assert database.get_installer_session(session.game_id) is not None

    assert manager.discard_pending_session(session) is True
    assert database.get_installer_session(session.game_id) is None


def test_removal_result_text_reports_missing_files(
    tmp_path: Path, database: Database
) -> None:
    manager, _ownership, session, _destination = _setup(tmp_path, database)
    shutil.rmtree(session.staging_directory)

    report = manager.remove_pending_install(session, authorized=True)
    text = removal_report_text(report)

    assert "Already missing" in text
    assert "Missing files did not block removal." in text
    assert "The pending install record was removed." in text
