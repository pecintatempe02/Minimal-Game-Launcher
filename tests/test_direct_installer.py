"""Direct standalone Windows installer staging, discovery, and safety tests."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from minimal_game_launcher.database import Database
from minimal_game_launcher.errors import InstallerError, InstallerStagingError
from minimal_game_launcher.models import InstallerSourceKind, InstallerStatus
from minimal_game_launcher.services.installer_manager import (
    CandidateKind,
    InstallerManager,
)
from minimal_game_launcher.services.ownership import (
    OwnershipService,
    ResourceKind,
)
from minimal_game_launcher.settings import (
    LauncherSettings,
    SettingsStore,
    WinePrefixStrategy,
)


class _NoIso:
    """Fail if direct mode ever tries to mount or inspect an ISO."""

    def validate_iso(self, _path):
        raise AssertionError("direct mode must not validate an ISO")

    def mount(self, _path):
        raise AssertionError("direct mode must not mount an ISO")

    def unmount(self, _mounted):
        raise AssertionError("direct mode must not unmount an ISO")


def _paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    return (
        tmp_path / "prefixes",
        tmp_path / "games",
        tmp_path / "installers",
    )


def _manager(
    tmp_path: Path,
    database: Database,
    *,
    settings_store: SettingsStore | None = None,
) -> InstallerManager:
    ownership = OwnershipService(*_paths(tmp_path))
    return InstallerManager(
        database, ownership, _NoIso(), settings_store=settings_store
    )


def _source(tmp_path: Path, name: str = "setup.exe") -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"MZ" + b"\x00" * 64 + b"installer payload")
    path.chmod(0o750)
    return path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _staged(manager: InstallerManager, source: Path, name: str = "Direct Game"):
    session = manager.create_executable_session(source, name)
    prepared = manager.prepare(session)
    return manager.save_session(prepared)


def test_valid_executable_creates_iso_compatible_session(
    tmp_path: Path, database: Database
) -> None:
    source = _source(tmp_path)
    manager = _manager(tmp_path, database)

    session = manager.create_executable_session(source, "Direct Game")

    assert session.source_kind is InstallerSourceKind.EXECUTABLE
    assert session.source_path == str(source.resolve())
    assert session.iso_path == str(source.resolve())
    assert session.status is InstallerStatus.PENDING
    assert Path(session.staging_directory) == (
        tmp_path / "installers" / session.game_id
    )
    assert database.get_installer_session(session.game_id) == session


@pytest.mark.parametrize(
    ("factory", "message"),
    (
        (lambda root: root / "missing.exe", "not found"),
        (lambda root: root / "empty.exe", "empty"),
        (lambda root: root / "wrong.zip", ".exe"),
        (lambda root: root / "plain.exe", "MZ signature"),
    ),
)
def test_invalid_sources_are_rejected(
    tmp_path: Path, database: Database, factory, message: str
) -> None:
    source = factory(tmp_path)
    if source.name == "empty.exe":
        source.write_bytes(b"")
    elif source.name == "wrong.zip":
        source.write_bytes(b"PK")
    elif source.name == "plain.exe":
        source.write_bytes(b"not-a-windows-binary")
    manager = _manager(tmp_path, database)

    with pytest.raises(InstallerError, match=message):
        manager.create_executable_session(source, "Bad")


def test_directory_and_unreadable_sources_are_rejected(
    tmp_path: Path, database: Database
) -> None:
    directory = tmp_path / "folder.exe"
    directory.mkdir()
    manager = _manager(tmp_path, database)
    with pytest.raises(InstallerError, match="not a regular file"):
        manager.create_executable_session(directory, "Bad")

    unreadable = _source(tmp_path, "unreadable.exe")
    unreadable.chmod(0o000)
    if os.access(unreadable, os.R_OK):
        pytest.skip("running as a user that can read mode-000 files")
    with pytest.raises(InstallerError, match="Permission denied"):
        manager.create_executable_session(unreadable, "Bad")


def test_symlink_resolves_to_regular_file_target(
    tmp_path: Path, database: Database
) -> None:
    target = _source(tmp_path, "payload.exe")
    link = tmp_path / "link.exe"
    link.symlink_to(target)
    manager = _manager(tmp_path, database)

    session = manager.create_executable_session(link, "Linked")

    assert session.source_path == str(target.resolve())
    assert session.source_path != str(link)


def test_prepare_copies_only_the_selected_file_and_keeps_original(
    tmp_path: Path, database: Database
) -> None:
    source = _source(tmp_path)
    sibling = tmp_path / "data.cab"
    sibling.write_bytes(b"companion data")
    before_bytes = source.read_bytes()
    before_mode = stat.S_IMODE(source.stat().st_mode)
    manager = _manager(tmp_path, database)

    session = _staged(manager, source)
    staging = Path(session.staging_directory)

    assert session.status is InstallerStatus.STAGED
    assert session.installer_relative_path == "setup.exe"
    assert sorted(p.name for p in (staging / "source").iterdir()) == ["setup.exe"]
    assert (staging / "source" / "setup.exe").read_bytes() == before_bytes
    assert source.read_bytes() == before_bytes
    assert stat.S_IMODE(source.stat().st_mode) == before_mode
    assert sibling.read_bytes() == b"companion data"

    manifest = json.loads(
        (staging / ".mgl-staging.json").read_text(encoding="utf-8")
    )
    assert manifest["source_kind"] == "executable"
    assert manifest["source_path"] == str(source.resolve())
    assert manifest["files"] == [
        {
            "path": "setup.exe",
            "size": len(before_bytes),
            "sha256": _sha256(source),
        }
    ]


def test_verification_survives_reopening_the_session(
    tmp_path: Path, database: Database
) -> None:
    source = _source(tmp_path)
    manager = _manager(tmp_path, database)
    session = _staged(manager, source)

    stored = database.get_installer_session(session.game_id)
    assert stored is not None
    summary = manager.verify_staged(stored)
    assert summary.file_count == 1
    assert summary.reused is True


def test_corruption_is_detected_without_deleting_staged_data(
    tmp_path: Path, database: Database
) -> None:
    source = _source(tmp_path)
    manager = _manager(tmp_path, database)
    session = _staged(manager, source)
    staged = Path(session.staging_directory) / "source" / "setup.exe"
    staged.write_bytes(b"MZ corrupted")

    with pytest.raises(
        InstallerStagingError, match="does not match|incomplete|differs|hash"
    ):
        manager.verify_staged(session)

    assert staged.is_file()
    assert source.is_file()


def test_existing_staging_directory_is_never_overwritten(
    tmp_path: Path, database: Database
) -> None:
    source = _source(tmp_path)
    manager = _manager(tmp_path, database)
    session = manager.create_executable_session(source, "Keep")
    staging = Path(session.staging_directory)
    staging.mkdir(parents=True)
    (staging / "keep.txt").write_text("keep", encoding="utf-8")

    with pytest.raises(InstallerStagingError, match="already exists"):
        manager.prepare(session)

    assert (staging / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert source.is_file()


def test_source_removed_before_staging_fails_cleanly(
    tmp_path: Path, database: Database
) -> None:
    source = _source(tmp_path)
    manager = _manager(tmp_path, database)
    session = manager.create_executable_session(source, "Gone")
    source.unlink()

    with pytest.raises(InstallerError, match="not found"):
        manager.prepare(session)

    assert not Path(session.staging_directory).exists()


def test_source_changing_during_copy_is_rejected(
    tmp_path: Path, database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source(tmp_path)
    manager = _manager(tmp_path, database)
    session = manager.create_executable_session(source, "Moving")
    original = manager._copy_installer_file

    def changing_copy(source_path: Path, destination: Path) -> str:
        digest = original(source_path, destination)
        source_path.write_bytes(b"MZ changed while copying")
        return digest

    monkeypatch.setattr(manager, "_copy_installer_file", changing_copy)

    with pytest.raises(InstallerStagingError, match="changed while it was copied"):
        manager.prepare(session)

    assert not Path(session.staging_directory).exists()


def test_source_inside_managed_roots_is_rejected(
    tmp_path: Path, database: Database
) -> None:
    manager = _manager(tmp_path, database)
    for root_name in ("installers", "games", "prefixes"):
        root = tmp_path / root_name / "nested"
        root.mkdir(parents=True)
        rogue = root / "setup.exe"
        rogue.write_bytes(b"MZ payload")
        with pytest.raises(InstallerError, match="MGL-managed"):
            manager.create_executable_session(rogue, "Rogue")


def test_direct_discovery_returns_the_selected_staged_file(
    tmp_path: Path, database: Database
) -> None:
    source = _source(tmp_path, "my-installer.exe")
    manager = _manager(tmp_path, database)
    session = _staged(manager, source, "Selected")
    staged_dir = Path(session.staging_directory) / "source"
    # A helper beside the selection must not change the answer.
    (staged_dir / "helper.exe").write_bytes(b"MZ helper")

    candidates = manager.discover(session)

    assert [candidate.relative_path for candidate in candidates] == [
        "my-installer.exe"
    ]
    assert candidates[0].kind is CandidateKind.INSTALLER
    assert "not check whether it is safe" in candidates[0].reason


def test_installer_execution_points_at_staged_copy(
    tmp_path: Path, database: Database
) -> None:
    source = _source(tmp_path)
    manager = _manager(tmp_path, database)
    session = _staged(manager, source)

    game = manager.installer_game(session)

    assert Path(game.executable) == (
        Path(session.staging_directory) / "source" / "setup.exe"
    )
    assert Path(game.executable) != source
    assert game.wine_prefix_path == session.wine_prefix_path


def test_shared_prefix_is_assigned_for_direct_sessions(
    tmp_path: Path, database: Database
) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    store.save(
        LauncherSettings(wine_prefix_strategy=WinePrefixStrategy.SHARED)
    )
    manager = _manager(tmp_path, database, settings_store=store)

    session = manager.create_executable_session(_source(tmp_path), "Shared")

    assert session.wine_prefix_shared is True
    assert Path(session.wine_prefix_path).name == ".shared"


def test_registration_uses_the_managed_game_directory(
    tmp_path: Path, database: Database
) -> None:
    manager = _manager(tmp_path, database)
    session = _staged(manager, _source(tmp_path))

    destination = manager.resolve_destination(
        session, managed=True, create=True
    )
    resolved = replace(
        session,
        install_directory=str(destination.path),
        install_directory_owned=destination.owned,
    )
    staged_game = Path(destination.path) / "game.exe"
    draft = manager.installation_draft(resolved, staged_game)

    assert Path(destination.path) == tmp_path / "games" / session.game_id
    assert draft.executable == str(staged_game)
    assert destination.owned is True


def test_pending_removal_never_touches_the_original_source(
    tmp_path: Path, database: Database
) -> None:
    source = _source(tmp_path)
    manager = _manager(tmp_path, database)
    session = _staged(manager, source)

    report = manager.remove_pending_install(session, authorized=True)

    assert report.complete
    assert source.is_file()
    assert not Path(session.staging_directory).exists()
    assert database.get_installer_session(session.game_id) is None


def test_session_only_removal_preserves_staged_files(
    tmp_path: Path, database: Database
) -> None:
    source = _source(tmp_path)
    manager = _manager(tmp_path, database)
    session = _staged(manager, source)

    manager.remove_pending_install(
        session, authorized=True, delete_files=False
    )

    assert source.is_file()
    assert (
        Path(session.staging_directory) / "source" / "setup.exe"
    ).is_file()
    assert database.get_installer_session(session.game_id) is None


def test_direct_mode_never_enters_the_iso_preparation_path(
    tmp_path: Path, database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path, database)
    session = manager.create_executable_session(_source(tmp_path), "Direct only")
    monkeypatch.setattr(
        manager,
        "_prepare_iso",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("ISO preparation must not run")
        ),
    )

    prepared = manager.prepare(session)

    assert prepared.source_kind is InstallerSourceKind.EXECUTABLE
    assert prepared.status is InstallerStatus.STAGED


def test_failed_direct_prepare_keeps_session_resumable(
    tmp_path: Path, database: Database
) -> None:
    source = _source(tmp_path)
    manager = _manager(tmp_path, database)
    session = manager.create_executable_session(source, "Resume")

    with pytest.raises(InstallerError):
        manager.prepare(replace(session, iso_path=str(tmp_path / "gone.exe")))

    assert database.get_installer_session(session.game_id).status is (
        InstallerStatus.PENDING
    )
    assert source.is_file()
