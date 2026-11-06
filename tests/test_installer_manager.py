"""Tests for ISO staging, discovery, destinations, and resumable sessions."""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

import minimal_game_launcher.services.installer_manager as installer_module
from minimal_game_launcher.database import SCHEMA_VERSION, Database
from minimal_game_launcher.errors import (
    InstallerError,
    InstallerStagingError,
    IsoMountError,
    OwnershipError,
)
from minimal_game_launcher.models import (
    GameDraft,
    InstallerSourceKind,
    InstallerStatus,
    Platform,
)
from minimal_game_launcher.services.game_manager import GameManager
from minimal_game_launcher.services.installer_manager import (
    CandidateKind,
    InstallerManager,
)
from minimal_game_launcher.services.iso_mount import MountedIso
from minimal_game_launcher.services.launch_manager import LaunchManager
from minimal_game_launcher.services.ownership import (
    OwnershipService,
    ResourceKind,
)
from minimal_game_launcher.settings import SettingsStore


class _FakeMount:
    """Expose a temporary directory as if it were a mounted ISO."""

    def __init__(self, source: Path, iso: Path) -> None:
        self.source = source
        self.iso = iso
        self.mounted = False
        self.unmounted = False

    def validate_iso(self, iso_path: str | Path) -> Path:
        path = Path(iso_path)
        if not path.is_file():
            raise IsoMountError(f"The ISO image was not found: {path}")
        return path.resolve()

    def mount(self, iso_path: str | Path) -> MountedIso:
        self.mounted = True
        return MountedIso(
            iso_path=Path(iso_path).resolve(),
            loop_device="/dev/loop0",
            mount_point=self.source,
        )

    def unmount(self, mounted: MountedIso) -> None:
        assert mounted.mount_point == self.source
        self.unmounted = True


@dataclass
class _Harness:
    manager: InstallerManager
    ownership: OwnershipService
    mount: _FakeMount
    source: Path
    iso: Path


def _harness(tmp_path: Path, database: Database) -> _Harness:
    source = tmp_path / "mounted"
    source.mkdir()
    iso = tmp_path / "disc.iso"
    iso.write_bytes(b"original-iso-bytes")
    ownership = OwnershipService(
        tmp_path / "prefixes", tmp_path / "games", tmp_path / "installers"
    )
    mount = _FakeMount(source, iso)
    manager = InstallerManager(database, ownership, mount)
    return _Harness(manager, ownership, mount, source, iso)


def _write_source(root: Path) -> None:
    (root / "setup.exe").write_bytes(b"MZ setup")
    (root / "readme.txt").write_text("hello", encoding="utf-8")
    nested = root / "data"
    nested.mkdir()
    (nested / "install.exe").write_bytes(b"MZ install")
    (nested / "game.exe").write_bytes(b"MZ game")
    deep = nested / "deep"
    deep.mkdir()
    (deep / "launcher.exe").write_bytes(b"MZ launcher")
    (root / "install.sh").write_text("#!/bin/sh\n", encoding="utf-8")


def _staged(
    harness: _Harness,
    *,
    iso_hash_before: str | None = None,
):
    session = harness.manager.create_session(harness.iso, "Test Game")
    prepared = harness.manager.prepare(session)
    saved = harness.manager.save_session(prepared)
    if iso_hash_before is not None:
        assert _sha256(harness.iso) == iso_hash_before
    return saved


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def test_stage_copies_full_contents_and_verifies(
    tmp_path: Path, database: Database
) -> None:
    harness = _harness(tmp_path, database)
    _write_source(harness.source)
    iso_hash = _sha256(harness.iso)

    session = _staged(harness, iso_hash_before=iso_hash)
    assert session.status is InstallerStatus.STAGED
    assert session.source_kind is InstallerSourceKind.ISO

    staging = Path(session.staging_directory)
    source = staging / "source"
    assert (source / "setup.exe").read_bytes() == b"MZ setup"
    assert (source / "data" / "deep" / "launcher.exe").read_bytes() == b"MZ launcher"
    assert (source / "readme.txt").read_text(encoding="utf-8") == "hello"
    assert harness.mount.unmounted is True
    summary = harness.manager.verify_staged(session)
    assert summary.file_count == 6
    assert summary.directory_count == 2
    assert summary.reused is True
    assert _sha256(harness.iso) == iso_hash


def test_large_file_is_streamed_and_verified(
    tmp_path: Path, database: Database
) -> None:
    harness = _harness(tmp_path, database)
    payload = b"x" * (5 * 1024 * 1024)
    (harness.source / "large.bin").write_bytes(payload)

    session = _staged(harness)

    staged = Path(session.staging_directory) / "source" / "large.bin"
    assert staged.stat().st_size == len(payload)
    assert hashlib.sha256(staged.read_bytes()).digest() == hashlib.sha256(
        payload
    ).digest()


def test_existing_staging_directory_is_never_removed(
    tmp_path: Path, database: Database
) -> None:
    harness = _harness(tmp_path, database)
    _write_source(harness.source)
    session = harness.manager.create_session(harness.iso, "Existing")
    staging = Path(session.staging_directory)
    staging.mkdir(parents=True)
    (staging / "keep.txt").write_text("keep", encoding="utf-8")

    with pytest.raises(InstallerStagingError, match="already exists"):
        harness.manager.prepare(session)

    assert (staging / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_symlink_in_source_is_rejected_and_partial_copy_removed(
    tmp_path: Path, database: Database
) -> None:
    harness = _harness(tmp_path, database)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (harness.source / "link").symlink_to(outside)
    session = harness.manager.create_session(harness.iso, "Link")

    with pytest.raises(InstallerStagingError, match="symbolic link"):
        harness.manager.prepare(session)

    assert not Path(session.staging_directory).exists()
    assert outside.read_text(encoding="utf-8") == "secret"


def test_verification_failure_removes_only_new_staging(
    tmp_path: Path, database: Database, monkeypatch
) -> None:
    harness = _harness(tmp_path, database)
    (harness.source / "setup.exe").write_bytes(b"MZ")
    session = harness.manager.create_session(harness.iso, "Broken")
    original = harness.manager._verify_tree

    def fail(*_args, **_kwargs):
        raise InstallerStagingError("simulated verification failure")

    monkeypatch.setattr(harness.manager, "_verify_tree", fail)
    with pytest.raises(InstallerStagingError, match="simulated"):
        harness.manager.prepare(session)
    monkeypatch.setattr(harness.manager, "_verify_tree", original)

    assert not Path(session.staging_directory).exists()


def test_insufficient_space_is_reported_before_copy(
    tmp_path: Path, database: Database, monkeypatch
) -> None:
    harness = _harness(tmp_path, database)
    (harness.source / "large.bin").write_bytes(b"x" * 1024)
    session = harness.manager.create_session(harness.iso, "Space")

    class _Usage:
        free = 0

    monkeypatch.setattr(
        installer_module.shutil, "disk_usage", lambda _path: _Usage()
    )
    with pytest.raises(InstallerStagingError, match="Not enough free disk space"):
        harness.manager.prepare(session)
    assert not Path(session.staging_directory).exists()


def test_discovery_distinguishes_installers_and_relative_paths(
    tmp_path: Path, database: Database
) -> None:
    harness = _harness(tmp_path, database)
    _write_source(harness.source)
    session = _staged(harness)

    candidates = harness.manager.discover(session)
    paths = {candidate.relative_path: candidate for candidate in candidates}

    assert paths["setup.exe"].kind is CandidateKind.INSTALLER
    assert paths["data/install.exe"].kind is CandidateKind.INSTALLER
    assert paths["data/deep/launcher.exe"].kind is CandidateKind.GAME
    assert paths["data/game.exe"].kind is CandidateKind.GAME
    assert paths["install.sh"].kind is CandidateKind.INSTALLER
    assert paths["install.sh"].platform is Platform.LINUX
    assert len({candidate.relative_path for candidate in candidates}) == len(
        candidates
    )


def test_select_installer_rejects_a_game_candidate(
    tmp_path: Path, database: Database
) -> None:
    harness = _harness(tmp_path, database)
    _write_source(harness.source)
    session = _staged(harness)

    with pytest.raises(InstallerError, match="not an installer"):
        harness.manager.select_installer(session, "data/game.exe")

    selected = harness.manager.select_installer(session, "data/install.exe")
    assert selected.installer_relative_path == "data/install.exe"
    game = harness.manager.installer_game(selected)
    assert game.id == session.game_id
    assert game.platform is Platform.WINDOWS
    assert game.executable == str(
        Path(session.staging_directory) / "source" / "data" / "install.exe"
    )
    assert game.wine_prefix_path == str(
        tmp_path / "prefixes" / session.game_id
    )


def test_managed_destination_is_owned_and_external_is_not(
    tmp_path: Path, database: Database
) -> None:
    harness = _harness(tmp_path, database)
    session = harness.manager.create_session(harness.iso, "Dest")

    managed = harness.manager.resolve_destination(
        session, managed=True, create=True
    )
    assert managed.path == tmp_path / "games" / session.game_id
    assert managed.owned is True
    assert harness.ownership.classify(
        ResourceKind.INSTALLATION,
        session.game_id,
        managed.path,
    ).is_managed

    external_path = tmp_path / "external" / "Game"
    external = harness.manager.resolve_destination(
        session, managed=False, external_path=external_path, create=True
    )
    assert external.path == external_path
    assert external.owned is False
    assert harness.ownership.classify(
        ResourceKind.INSTALLATION,
        session.game_id,
        external.path,
    ).status.value == "external"


def test_destination_refuses_overwrite_and_managed_root_confusion(
    tmp_path: Path, database: Database
) -> None:
    harness = _harness(tmp_path, database)
    session = harness.manager.create_session(harness.iso, "Overwrite")

    managed = harness.manager.resolve_destination(
        session, managed=True, create=True
    )
    (managed.path / "existing.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(InstallerError, match="not empty"):
        harness.manager.resolve_destination(session, managed=True, create=True)

    external = tmp_path / "external"
    external.mkdir()
    (external / "existing.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(InstallerError, match="not empty"):
        harness.manager.resolve_destination(
            session, managed=False, external_path=external, create=True
        )

    wrong_managed = tmp_path / "games" / "not-this-id"
    wrong_managed.mkdir(parents=True)
    with pytest.raises(InstallerError, match="exactly the game directory"):
        harness.manager.resolve_destination(
            session, managed=False, external_path=wrong_managed, create=True
        )


def test_destination_refuses_staging_and_iso_paths(
    tmp_path: Path, database: Database
) -> None:
    harness = _harness(tmp_path, database)
    session = harness.manager.create_session(harness.iso, "Unsafe")
    staging = Path(session.staging_directory)
    staging.mkdir(parents=True)

    with pytest.raises(InstallerError, match="installer staging"):
        harness.manager.resolve_destination(
            session,
            managed=False,
            external_path=staging / "inside",
            create=True,
        )
    with pytest.raises(InstallerError, match="cannot be the ISO"):
        harness.manager.resolve_destination(
            session,
            managed=False,
            external_path=harness.iso,
            create=True,
        )


def test_session_survives_reopen_and_resumes_without_recopy(
    tmp_path: Path, database: Database
) -> None:
    harness = _harness(tmp_path, database)
    _write_source(harness.source)
    session = _staged(harness)
    staging = Path(session.staging_directory)
    before = (staging / "source" / "setup.exe").read_bytes()

    reopened = Database(tmp_path / "library.db")
    try:
        manager = InstallerManager(
            reopened,
            harness.ownership,
            _FakeMount(harness.source, harness.iso),
        )
        stored = manager.get_session(session.game_id)
        assert stored is not None
        assert stored.status is InstallerStatus.STAGED
        summary = manager.verify_staged(stored)
        assert summary.reused is True
        assert (staging / "source" / "setup.exe").read_bytes() == before
    finally:
        reopened.close()


def test_corrupt_staged_file_fails_verification_without_deleting(
    tmp_path: Path, database: Database
) -> None:
    harness = _harness(tmp_path, database)
    _write_source(harness.source)
    session = _staged(harness)
    staged = Path(session.staging_directory) / "source" / "setup.exe"
    staged.write_bytes(b"changed")

    with pytest.raises(InstallerStagingError, match="does not match"):
        harness.manager.verify_staged(session)
    assert staged.exists()


def test_installer_runs_through_existing_wine_prefix(
    tmp_path: Path, database: Database
) -> None:
    harness = _harness(tmp_path, database)
    _write_source(harness.source)
    session = _staged(harness)
    selected = harness.manager.save_session(
        harness.manager.select_installer(session, "setup.exe")
    )
    game = harness.manager.installer_game(selected)

    run_calls = []

    def fake_run(arguments, **kwargs):
        run_calls.append(list(arguments))
        # Emulate a successful wineboot that leaves a complete prefix behind.
        wineprefix = Path(kwargs["env"]["WINEPREFIX"])
        (wineprefix / "drive_c").mkdir(parents=True, exist_ok=True)
        (wineprefix / "system.reg").write_text("fake registry\n", encoding="utf-8")
        return subprocess.CompletedProcess(list(arguments), 0, "", "")

    launch_manager = LaunchManager(
        tmp_path / "prefixes",
        SettingsStore(tmp_path / "settings.json"),
        which=lambda name: "/usr/bin/wine" if name == "wine" else None,
        run_factory=fake_run,
    )
    spec = launch_manager.prepare_launch(game)

    prefix = tmp_path / "prefixes" / session.game_id
    assert prefix.is_dir()
    assert spec.prefix_path == str(prefix)
    assert spec.env["WINEPREFIX"] == str(prefix)
    assert spec.command[0] == "/usr/bin/wine"
    assert spec.command[1] == str(
        Path(session.staging_directory) / "source" / "setup.exe"
    )
    assert run_calls[0][:3] == ["/usr/bin/wine", "wineboot", "-u"]


def test_register_installation_persists_ownership(
    tmp_path: Path, database: Database
) -> None:
    harness = _harness(tmp_path, database)
    _write_source(harness.source)
    session = _staged(harness)
    destination = harness.manager.resolve_destination(
        session, managed=True, create=True
    )
    session = harness.manager.save_session(
        replace(
            session,
            install_directory=str(destination.path),
            install_directory_owned=destination.owned,
        )
    )
    executable = destination.path / "game.exe"
    executable.write_bytes(b"MZ game")
    games = GameManager(database, tmp_path / "prefixes")
    draft = GameDraft(
        name=session.game_name,
        platform=Platform.WINDOWS,
        executable=str(executable),
    )

    game = games.register_installation(
        session.game_id,
        draft,
        install_directory=session.install_directory,
        install_directory_owned=session.install_directory_owned,
        installer_source_directory=session.staging_directory,
        installer_source_owned=True,
    )

    assert game.id == session.game_id
    assert game.install_directory_owned is True
    assert game.installer_source_directory == session.staging_directory
    assert game.installer_source_owned is True
    assert database.get_game(session.game_id) == game


def test_finished_and_deferred_sessions_discover_prefix_executables(
    tmp_path: Path, database: Database
) -> None:
    harness = _harness(tmp_path, database)
    _write_source(harness.source)
    session = _staged(harness)
    prefix = tmp_path / "prefixes" / session.game_id
    executable = prefix / "drive_c" / "Program Files" / "Test Game" / "Test Game.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"MZ game")

    finished = harness.manager.save_session(
        replace(session, status=InstallerStatus.INSTALLER_FINISHED)
    )
    candidates = harness.manager.discover_game_executables(finished)
    assert [candidate.path for candidate in candidates] == [executable]
    assert candidates[0].recommended is True

    deferred = harness.manager.save_session(
        replace(finished, status=InstallerStatus.DEFERRED)
    )
    reopened = Database(tmp_path / "library.db")
    try:
        manager = InstallerManager(
            reopened,
            harness.ownership,
            _FakeMount(harness.source, harness.iso),
        )
        stored = manager.get_session(session.game_id)
        assert stored is not None
        assert stored.status is InstallerStatus.DEFERRED
        assert manager.verify_staged(stored).reused is True
        resumed = manager.discover_game_executables(stored)
        assert [candidate.path for candidate in resumed] == [executable]
    finally:
        reopened.close()


def test_database_migrates_v4_and_preserves_existing_game(
    tmp_path: Path,
) -> None:
    path = tmp_path / "step3.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE games (
            id TEXT PRIMARY KEY NOT NULL,
            name TEXT NOT NULL,
            platform TEXT NOT NULL,
            executable TEXT NOT NULL,
            working_directory TEXT NOT NULL,
            launch_arguments TEXT NOT NULL DEFAULT '',
            cover_path TEXT,
            wine_prefix_path TEXT,
            gamemode_enabled INTEGER NOT NULL DEFAULT 1,
            mangohud_enabled INTEGER NOT NULL DEFAULT 0,
            environment TEXT NOT NULL DEFAULT '{}',
            install_directory TEXT,
            install_directory_owned INTEGER NOT NULL DEFAULT 0,
            installer_source_directory TEXT,
            installer_source_owned INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        PRAGMA user_version = 4;
        INSERT INTO games (
            id, name, platform, executable, working_directory,
            created_at, updated_at
        ) VALUES (
            'step3-id', 'Step 3 Game', 'windows', '/games/step3.exe',
            '/games', '2026-01-01T00:00:00+00:00',
            '2026-01-01T00:00:00+00:00'
        );
        """
    )
    connection.commit()
    connection.close()

    database = Database(path)
    try:
        game = database.get_game("step3-id")
        assert game is not None
        assert game.name == "Step 3 Game"
        assert database._connection.execute(
            "PRAGMA user_version"
        ).fetchone()[0] == SCHEMA_VERSION
        assert database._connection.execute(
            "SELECT COUNT(*) FROM installer_sessions"
        ).fetchone()[0] == 0

        reopened = Database(path)
        try:
            assert reopened.get_game("step3-id") == game
        finally:
            reopened.close()
    finally:
        database.close()
