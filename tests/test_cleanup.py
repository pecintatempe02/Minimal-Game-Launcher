"""Tests for cleanup previews, orphan detection, and safe deletion.

Every path lives under ``tmp_path``. No real library, prefix, ISO, or game file
is read or modified.
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gtk  # noqa: E402
import pytest  # noqa: E402

from minimal_game_launcher.database import Database  # noqa: E402
from minimal_game_launcher.models import (  # noqa: E402
    Game,
    InstallerSession,
    InstallerStatus,
    Platform,
)
from minimal_game_launcher.services.cleanup import (  # noqa: E402
    CleanupOutcome,
    CleanupReport,
    CleanupService,
)
from minimal_game_launcher.services.game_manager import GameManager  # noqa: E402
from minimal_game_launcher.services.ownership import (  # noqa: E402
    DeletionResult,
    OwnershipService,
    OwnershipStatus,
    ResourceKind,
)
from minimal_game_launcher.ui.cleanup_dialog import (  # noqa: E402
    CleanupDialog,
    CleanupResultDialog,
)

GAME_ID = "game-1"
NOW = "2026-01-01T00:00:00+00:00"

needs_display = pytest.mark.skipif(
    Gdk.Display.get_default() is None,
    reason="constructing GTK widgets needs a GDK display",
)


@pytest.fixture
def roots(tmp_path: Path) -> dict[ResourceKind, Path]:
    """Return the three managed roots under the temporary data directory."""
    return {
        ResourceKind.PREFIX: tmp_path / "data" / "prefixes",
        ResourceKind.INSTALLATION: tmp_path / "data" / "games",
        ResourceKind.INSTALLER_SOURCE: tmp_path / "data" / "installers",
    }


@pytest.fixture
def ownership(roots: dict[ResourceKind, Path]) -> OwnershipService:
    return OwnershipService(
        roots[ResourceKind.PREFIX],
        roots[ResourceKind.INSTALLATION],
        roots[ResourceKind.INSTALLER_SOURCE],
    )


@pytest.fixture
def active() -> set[str]:
    return set()


@pytest.fixture
def cleanup(
    database: Database,
    ownership: OwnershipService,
    active: set[str],
) -> CleanupService:
    return CleanupService(
        database, ownership, is_game_active=active.__contains__
    )


def resource(
    roots: dict[ResourceKind, Path], kind: ResourceKind, game_id: str = GAME_ID
) -> Path:
    path = roots[kind] / game_id
    (path / "nested").mkdir(parents=True)
    (path / "file.bin").write_bytes(b"1234")
    (path / "nested" / "inner.txt").write_text("hi", encoding="utf-8")
    return path


def make_game(
    tmp_path: Path,
    database: Database,
    *,
    game_id: str = GAME_ID,
    platform: Platform = Platform.WINDOWS,
    install: Path | None = None,
    install_owned: bool | None = None,
    prefix: Path | None = None,
    source: Path | None = None,
    source_owned: bool | None = None,
) -> Game:
    executable = tmp_path / f"{game_id}.exe"
    executable.write_bytes(b"MZ")
    if prefix is None and platform is Platform.WINDOWS:
        prefix = tmp_path / "data" / "prefixes" / game_id
    game = Game(
        id=game_id,
        name=f"Game {game_id}",
        platform=platform,
        executable=str(executable),
        working_directory=str(tmp_path),
        launch_arguments="",
        cover_path=None,
        wine_prefix_path=str(prefix) if prefix else None,
        gamemode_enabled=False,
        mangohud_enabled=False,
        created_at=NOW,
        updated_at=NOW,
        install_directory=str(install) if install else None,
        install_directory_owned=(
            install_owned if install_owned is not None else bool(install)
        ),
        installer_source_directory=str(source) if source else None,
        installer_source_owned=(
            source_owned if source_owned is not None else bool(source)
        ),
    )
    database.add_game(game)
    return game


def save_session(
    database: Database,
    *,
    game_id: str = GAME_ID,
    status: InstallerStatus = InstallerStatus.DEFERRED,
    staging: Path | None = None,
    install: Path | None = None,
    install_owned: bool = False,
) -> InstallerSession:
    session = InstallerSession(
        game_id=game_id,
        game_name=f"Game {game_id}",
        iso_path="/tmp/disc.iso",
        staging_directory=str(staging or Path("/tmp/installers") / game_id),
        installer_relative_path="setup.exe",
        install_directory=str(install) if install else None,
        install_directory_owned=install_owned,
        status=status,
        message="test",
        created_at=NOW,
        updated_at=NOW,
    )
    database.save_installer_session(session)
    return session


# ---------------------------------------------------------------------------
# Orphan detection


def test_resources_for_a_valid_game_are_not_orphans(
    tmp_path: Path,
    database: Database,
    cleanup: CleanupService,
    roots: dict[ResourceKind, Path],
) -> None:
    install = resource(roots, ResourceKind.INSTALLATION)
    prefix = resource(roots, ResourceKind.PREFIX)
    source = resource(roots, ResourceKind.INSTALLER_SOURCE)
    make_game(
        tmp_path,
        database,
        install=install,
        prefix=prefix,
        source=source,
    )

    scan = cleanup.scan_orphans()

    assert scan.candidates == ()
    assert scan.issues == ()


def test_deferred_installer_session_resources_are_not_orphans(
    tmp_path: Path,
    database: Database,
    cleanup: CleanupService,
    roots: dict[ResourceKind, Path],
) -> None:
    prefix = resource(roots, ResourceKind.PREFIX)
    source = resource(roots, ResourceKind.INSTALLER_SOURCE)
    save_session(
        database,
        status=InstallerStatus.DEFERRED,
        staging=source,
        install=roots[ResourceKind.INSTALLATION] / GAME_ID,
        install_owned=True,
    )

    scan = cleanup.scan_orphans()

    assert scan.candidates == ()
    assert scan.issues == ()
    assert prefix.exists() and source.exists()


def test_truly_orphaned_resources_are_detected(
    tmp_path: Path,
    database: Database,
    cleanup: CleanupService,
    roots: dict[ResourceKind, Path],
) -> None:
    for kind in roots:
        resource(roots, kind)

    scan = cleanup.scan_orphans()

    assert {target.kind for target in scan.candidates} == set(roots)
    assert {target.game_id for target in scan.candidates} == {GAME_ID}
    assert all(target.can_delete for target in scan.candidates)
    assert all(target.estimate is not None for target in scan.candidates)
    assert scan.issues == ()


def test_external_resources_are_never_orphan_candidates(
    tmp_path: Path,
    cleanup: CleanupService,
    roots: dict[ResourceKind, Path],
) -> None:
    external = tmp_path / "external"
    external.mkdir()
    (external / "keep.txt").write_text("keep", encoding="utf-8")
    roots[ResourceKind.INSTALLATION].mkdir(parents=True)
    (roots[ResourceKind.INSTALLATION] / GAME_ID).symlink_to(
        external, target_is_directory=True
    )

    scan = cleanup.scan_orphans()

    assert scan.candidates == ()
    assert any("symbolic link" in issue.reason for issue in scan.issues)
    assert (external / "keep.txt").exists()


def test_invalid_entry_names_are_reported_separately(
    cleanup: CleanupService, roots: dict[ResourceKind, Path]
) -> None:
    roots[ResourceKind.PREFIX].mkdir(parents=True)
    (roots[ResourceKind.PREFIX] / "id with space").mkdir()

    scan = cleanup.scan_orphans()

    assert scan.candidates == ()
    assert len(scan.issues) == 1
    assert scan.issues[0].kind is ResourceKind.PREFIX


def test_scan_is_bounded(
    database: Database,
    ownership: OwnershipService,
    roots: dict[ResourceKind, Path],
) -> None:
    for game_id in ("a", "b", "c"):
        resource(roots, ResourceKind.PREFIX, game_id)
    service = CleanupService(database, ownership, max_scan_entries=1)

    scan = service.scan_orphans()

    assert len(scan.candidates) == 1
    assert any("scan limit" in issue.reason for issue in scan.issues)


def test_scan_does_not_create_managed_roots(
    cleanup: CleanupService, roots: dict[ResourceKind, Path]
) -> None:
    cleanup.scan_orphans()

    assert not any(path.exists() for path in roots.values())


def test_installed_session_without_a_game_is_not_active_protection(
    database: Database,
    cleanup: CleanupService,
    roots: dict[ResourceKind, Path],
) -> None:
    source = resource(roots, ResourceKind.INSTALLER_SOURCE)
    save_session(
        database,
        status=InstallerStatus.INSTALLED,
        staging=source,
    )

    scan = cleanup.scan_orphans()

    assert [target.kind for target in scan.candidates] == [
        ResourceKind.INSTALLER_SOURCE
    ]


# ---------------------------------------------------------------------------
# Cleanup choices


def test_metadata_only_removal_preserves_every_file(
    tmp_path: Path,
    database: Database,
    roots: dict[ResourceKind, Path],
) -> None:
    install = resource(roots, ResourceKind.INSTALLATION)
    prefix = resource(roots, ResourceKind.PREFIX)
    source = resource(roots, ResourceKind.INSTALLER_SOURCE)
    make_game(
        tmp_path, database, install=install, prefix=prefix, source=source
    )
    manager = GameManager(database, roots[ResourceKind.PREFIX])

    manager.remove_game(GAME_ID)

    assert database.get_game(GAME_ID) is None
    assert install.is_dir() and prefix.is_dir() and source.is_dir()


def test_removing_game_files_preserves_prefix_and_source(
    tmp_path: Path,
    database: Database,
    cleanup: CleanupService,
    roots: dict[ResourceKind, Path],
) -> None:
    install = resource(roots, ResourceKind.INSTALLATION)
    prefix = resource(roots, ResourceKind.PREFIX)
    source = resource(roots, ResourceKind.INSTALLER_SOURCE)
    game = make_game(
        tmp_path, database, install=install, prefix=prefix, source=source
    )
    target = next(
        item
        for item in cleanup.targets_for_game(game)
        if item.kind is ResourceKind.INSTALLATION
    )

    report = cleanup.delete_targets([target], authorized=True)

    assert report.all_deleted
    assert not install.exists()
    assert prefix.is_dir() and source.is_dir()
    assert database.get_game(GAME_ID) is not None


def test_prefix_only_cleanup_preserves_game_files_and_metadata(
    tmp_path: Path,
    database: Database,
    cleanup: CleanupService,
    roots: dict[ResourceKind, Path],
) -> None:
    install = resource(roots, ResourceKind.INSTALLATION)
    prefix = resource(roots, ResourceKind.PREFIX)
    source = resource(roots, ResourceKind.INSTALLER_SOURCE)
    game = make_game(
        tmp_path, database, install=install, prefix=prefix, source=source
    )
    target = next(
        item
        for item in cleanup.targets_for_game(game)
        if item.kind is ResourceKind.PREFIX
    )

    report = cleanup.delete_targets([target], authorized=True)

    assert report.all_deleted
    assert not prefix.exists()
    assert install.is_dir() and source.is_dir()
    assert database.get_game(GAME_ID) == game


def test_installer_source_removal_leaves_iso_and_external_data(
    tmp_path: Path,
    database: Database,
    cleanup: CleanupService,
    roots: dict[ResourceKind, Path],
) -> None:
    install = resource(roots, ResourceKind.INSTALLATION)
    source = resource(roots, ResourceKind.INSTALLER_SOURCE)
    iso = tmp_path / "original.iso"
    iso.write_bytes(b"iso-bytes")
    external = tmp_path / "external-source"
    external.mkdir()
    (external / "keep.bin").write_bytes(b"keep")
    game = make_game(
        tmp_path,
        database,
        install=install,
        prefix=None,
        source=source,
    )
    target = next(
        item
        for item in cleanup.targets_for_game(game)
        if item.kind is ResourceKind.INSTALLER_SOURCE
    )

    report = cleanup.delete_targets([target], authorized=True)

    assert report.all_deleted
    assert not source.exists()
    assert iso.read_bytes() == b"iso-bytes"
    assert (external / "keep.bin").exists()


def test_external_installation_is_not_deletable(
    tmp_path: Path,
    database: Database,
    cleanup: CleanupService,
) -> None:
    external = tmp_path / "external-game"
    external.mkdir()
    (external / "keep.bin").write_bytes(b"keep")
    game = make_game(
        tmp_path,
        database,
        platform=Platform.LINUX,
        install=external,
        install_owned=False,
        prefix=None,
    )
    target = cleanup.targets_for_game(game)[0]

    assert target.status is OwnershipStatus.EXTERNAL
    assert not target.can_delete
    outcome = cleanup.delete_target(target, authorized=True)

    assert outcome.skipped_reason
    assert (external / "keep.bin").exists()


def test_unselected_resources_remain_untouched(
    tmp_path: Path,
    database: Database,
    cleanup: CleanupService,
    roots: dict[ResourceKind, Path],
) -> None:
    install = resource(roots, ResourceKind.INSTALLATION)
    prefix = resource(roots, ResourceKind.PREFIX)
    source = resource(roots, ResourceKind.INSTALLER_SOURCE)
    game = make_game(
        tmp_path, database, install=install, prefix=prefix, source=source
    )
    target = next(
        item
        for item in cleanup.targets_for_game(game)
        if item.kind is ResourceKind.INSTALLATION
    )

    cleanup.delete_targets([target], authorized=True)

    assert prefix.is_dir() and source.is_dir()


def test_orphan_cleanup_removes_only_selected_target(
    tmp_path: Path,
    cleanup: CleanupService,
    roots: dict[ResourceKind, Path],
) -> None:
    first = resource(roots, ResourceKind.PREFIX, "first")
    second = resource(roots, ResourceKind.PREFIX, "second")
    scan = cleanup.scan_orphans()
    target = next(item for item in scan.candidates if item.path.name == "first")

    report = cleanup.delete_targets([target], authorized=True)

    assert report.all_deleted
    assert not first.exists()
    assert second.is_dir()


# ---------------------------------------------------------------------------
# Safety


def test_running_game_blocks_every_target(
    tmp_path: Path,
    database: Database,
    cleanup: CleanupService,
    roots: dict[ResourceKind, Path],
    active: set[str],
) -> None:
    install = resource(roots, ResourceKind.INSTALLATION)
    prefix = resource(roots, ResourceKind.PREFIX)
    source = resource(roots, ResourceKind.INSTALLER_SOURCE)
    game = make_game(
        tmp_path, database, install=install, prefix=prefix, source=source
    )
    active.add(GAME_ID)

    targets = cleanup.targets_for_game(game)

    assert all(not target.can_delete for target in targets)
    assert all("running" in (target.unavailable_reason or "") for target in targets)
    for target in targets:
        outcome = cleanup.delete_target(target, authorized=True)
        assert outcome.skipped_reason
    assert install.is_dir() and prefix.is_dir() and source.is_dir()


def test_active_installer_session_blocks_staging_and_destination(
    tmp_path: Path,
    database: Database,
    cleanup: CleanupService,
    roots: dict[ResourceKind, Path],
) -> None:
    install = resource(roots, ResourceKind.INSTALLATION)
    prefix = resource(roots, ResourceKind.PREFIX)
    source = resource(roots, ResourceKind.INSTALLER_SOURCE)
    game = make_game(
        tmp_path, database, install=install, prefix=prefix, source=source
    )
    session = save_session(
        database,
        status=InstallerStatus.STAGED,
        staging=source,
        install=install,
        install_owned=True,
    )

    targets = cleanup.targets_for_game(game)

    assert all(not target.can_delete for target in targets)
    for target in targets:
        assert cleanup.delete_target(target, authorized=True).skipped_reason
    assert install.is_dir() and prefix.is_dir() and source.is_dir()
    assert database.get_installer_session(GAME_ID) == session


def test_symlink_swap_after_preview_is_refused(
    tmp_path: Path,
    database: Database,
    cleanup: CleanupService,
    roots: dict[ResourceKind, Path],
) -> None:
    install = resource(roots, ResourceKind.INSTALLATION)
    game = make_game(
        tmp_path,
        database,
        platform=Platform.LINUX,
        install=install,
        prefix=None,
    )
    target = cleanup.targets_for_game(game)[0]
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")

    import shutil

    shutil.rmtree(install)
    install.symlink_to(outside, target_is_directory=True)

    outcome = cleanup.delete_target(target, authorized=True)

    assert not outcome.deleted
    assert outcome.skipped_reason
    assert (outside / "keep.txt").exists()


def test_directory_identity_change_after_preview_is_refused(
    tmp_path: Path,
    database: Database,
    cleanup: CleanupService,
    roots: dict[ResourceKind, Path],
) -> None:
    install = resource(roots, ResourceKind.INSTALLATION)
    game = make_game(
        tmp_path,
        database,
        platform=Platform.LINUX,
        install=install,
        prefix=None,
    )
    target = cleanup.targets_for_game(game)[0]

    import shutil

    shutil.rmtree(install)
    install.mkdir()
    (install / "replacement.txt").write_text("new", encoding="utf-8")

    outcome = cleanup.delete_target(target, authorized=True)

    assert not outcome.deleted
    assert "changed after the preview" in (outcome.skipped_reason or "")
    assert (install / "replacement.txt").exists()


def test_path_traversal_never_escapes_the_managed_root(
    tmp_path: Path,
    database: Database,
    cleanup: CleanupService,
    roots: dict[ResourceKind, Path],
) -> None:
    other = resource(roots, ResourceKind.INSTALLATION, "other-game")
    game = make_game(
        tmp_path,
        database,
        platform=Platform.LINUX,
        install=roots[ResourceKind.INSTALLATION] / GAME_ID / ".." / "other-game",
        prefix=None,
    )

    target = cleanup.targets_for_game(game)[0]
    outcome = cleanup.delete_target(target, authorized=True)

    assert target.status is OwnershipStatus.INVALID
    assert not target.can_delete
    assert outcome.skipped_reason
    assert other.is_dir()


def test_game_metadata_change_after_preview_is_refused(
    tmp_path: Path,
    database: Database,
    cleanup: CleanupService,
    roots: dict[ResourceKind, Path],
) -> None:
    install = resource(roots, ResourceKind.INSTALLATION)
    game = make_game(
        tmp_path,
        database,
        platform=Platform.LINUX,
        install=install,
        prefix=None,
    )
    target = cleanup.targets_for_game(game)[0]
    database.update_game(
        replace(
            game,
            install_directory=None,
            install_directory_owned=False,
        )
    )

    outcome = cleanup.delete_target(target, authorized=True)

    assert not outcome.deleted
    assert "no longer references" in (outcome.skipped_reason or "")
    assert install.is_dir()


def test_partial_failure_is_reported_and_record_survives(
    tmp_path: Path,
    database: Database,
    cleanup: CleanupService,
    roots: dict[ResourceKind, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install = resource(roots, ResourceKind.INSTALLATION)
    game = make_game(
        tmp_path,
        database,
        platform=Platform.LINUX,
        install=install,
        prefix=None,
    )
    target = cleanup.targets_for_game(game)[0]

    monkeypatch.setattr(
        cleanup._ownership,
        "delete",
        lambda *_args, **_kwargs: DeletionResult(
            deleted=False, removed_entries=1, errors=("simulated failure",)
        ),
    )

    report = cleanup.delete_targets([target], authorized=True)

    assert not report.all_deleted
    assert report.failed
    assert report.outcomes[0].errors == ("simulated failure",)
    assert report.outcomes[0].remaining is not None
    assert database.get_game(GAME_ID) is not None


def test_symlinked_managed_root_is_refused(
    tmp_path: Path,
    database: Database,
    ownership: OwnershipService,
    roots: dict[ResourceKind, Path],
) -> None:
    real_root = tmp_path / "real-games"
    real = resource(
        {ResourceKind.INSTALLATION: real_root},
        ResourceKind.INSTALLATION,
    )
    roots[ResourceKind.INSTALLATION].parent.mkdir(parents=True, exist_ok=True)
    roots[ResourceKind.INSTALLATION].symlink_to(
        real_root, target_is_directory=True
    )
    game = make_game(
        tmp_path,
        database,
        platform=Platform.LINUX,
        install=roots[ResourceKind.INSTALLATION] / GAME_ID,
        prefix=None,
    )
    cleanup = CleanupService(database, ownership)
    target = cleanup.targets_for_game(game)[0]

    outcome = cleanup.delete_target(target, authorized=True)

    assert not target.can_delete
    assert "symbolic link" in (target.unavailable_reason or "")
    assert outcome.skipped_reason
    assert real.is_dir()


def test_failed_cleanup_leaves_unrelated_records_untouched(
    tmp_path: Path,
    database: Database,
    cleanup: CleanupService,
    roots: dict[ResourceKind, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install = resource(roots, ResourceKind.INSTALLATION)
    game = make_game(
        tmp_path,
        database,
        platform=Platform.LINUX,
        install=install,
        prefix=None,
    )
    other = make_game(
        tmp_path,
        database,
        game_id="other-game",
        platform=Platform.LINUX,
        prefix=None,
    )
    target = cleanup.targets_for_game(game)[0]
    monkeypatch.setattr(
        cleanup._ownership,
        "delete",
        lambda *_args, **_kwargs: DeletionResult(
            deleted=False, removed_entries=0, errors=("simulated failure",)
        ),
    )

    cleanup.delete_targets([target], authorized=True)

    assert database.get_game(GAME_ID) == game
    assert database.get_game("other-game") == other


def test_deletion_requires_authorization(
    tmp_path: Path,
    database: Database,
    cleanup: CleanupService,
    roots: dict[ResourceKind, Path],
) -> None:
    install = resource(roots, ResourceKind.INSTALLATION)
    game = make_game(
        tmp_path,
        database,
        platform=Platform.LINUX,
        install=install,
        prefix=None,
    )
    target = cleanup.targets_for_game(game)[0]

    with pytest.raises(Exception, match="explicit authorization"):
        cleanup.delete_targets([target], authorized=False)

    assert install.is_dir()


# ---------------------------------------------------------------------------
# GTK presentation


@needs_display
def test_cleanup_dialog_disables_unsafe_targets(
    tmp_path: Path,
    database: Database,
    cleanup: CleanupService,
    roots: dict[ResourceKind, Path],
) -> None:
    prefix = resource(roots, ResourceKind.PREFIX)
    external = tmp_path / "external-game"
    external.mkdir()
    game = make_game(
        tmp_path,
        database,
        install=external,
        install_owned=False,
        prefix=prefix,
    )

    dialog = CleanupDialog(None, game, cleanup, GameManager(database, roots[ResourceKind.PREFIX]))

    assert dialog._record_row.get_active() is True
    assert dialog._target_rows[ResourceKind.INSTALLATION].get_sensitive() is False
    assert dialog._target_rows[ResourceKind.INSTALLATION].get_active() is False
    assert "External path" in dialog._target_rows[
        ResourceKind.INSTALLATION
    ].get_subtitle()
    assert dialog._target_rows[ResourceKind.PREFIX].get_sensitive() is True
    assert "MGL-managed" in dialog._target_rows[ResourceKind.PREFIX].get_subtitle()


@needs_display
def test_cleanup_confirmation_reflects_selected_targets(
    tmp_path: Path,
    database: Database,
    cleanup: CleanupService,
    roots: dict[ResourceKind, Path],
) -> None:
    prefix = resource(roots, ResourceKind.PREFIX)
    install = resource(roots, ResourceKind.INSTALLATION)
    game = make_game(
        tmp_path, database, install=install, prefix=prefix
    )
    dialog = CleanupDialog(None, game, cleanup, GameManager(database, roots[ResourceKind.PREFIX]))

    dialog._target_rows[ResourceKind.PREFIX].set_active(True)

    body = dialog.confirmation_body()
    assert "Wine prefix" in body
    assert str(prefix) in body
    assert str(install) not in body
    assert "Library entry" in body


@needs_display
def test_result_dialog_shows_partial_failures(
    tmp_path: Path,
    database: Database,
    cleanup: CleanupService,
    roots: dict[ResourceKind, Path],
) -> None:
    install = resource(roots, ResourceKind.INSTALLATION)
    game = make_game(
        tmp_path,
        database,
        platform=Platform.LINUX,
        install=install,
        prefix=None,
    )
    target = cleanup.targets_for_game(game)[0]
    report = CleanupReport(
        (
            CleanupOutcome(
                target=target,
                deleted=False,
                errors=("permission denied",),
            ),
        )
    )

    dialog = CleanupResultDialog(report, record_selected=True)

    text = dialog.summary_text()
    assert "Library entry kept" in text
    assert "permission denied" in text
