"""Tests for the user-selected Wine prefix strategy.

Every path lives under ``tmp_path``. No Wine process is started: the launch
manager's runner is replaced by a fake that only creates ``system.reg``.
"""

from __future__ import annotations

import sqlite3
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from minimal_game_launcher.database import SCHEMA_VERSION, Database
from minimal_game_launcher.errors import OwnershipError
from minimal_game_launcher.models import (
    Game,
    GameDraft,
    InstallerStatus,
    Platform,
)
from minimal_game_launcher.services.cleanup import CleanupService
from minimal_game_launcher.services.diagnostics import (
    CheckStatus,
    DiagnosticsService,
)
from minimal_game_launcher.services.game_manager import GameManager
from minimal_game_launcher.services.installer_manager import InstallerManager
from minimal_game_launcher.services.launch_manager import LaunchManager
from minimal_game_launcher.services.ownership import (
    OwnershipService,
    OwnershipStatus,
    ResourceKind,
)
from minimal_game_launcher.settings import (
    LauncherSettings,
    SettingsStore,
    WinePrefixStrategy,
)

NOW = "2026-01-01T00:00:00+00:00"


class _FakeMount:
    def validate_iso(self, iso_path: str | Path) -> Path:
        path = Path(iso_path).expanduser()
        if not path.is_file():
            raise FileNotFoundError(path)
        return path.resolve()


@pytest.fixture
def layout(tmp_path: Path) -> dict[str, Path]:
    data = tmp_path / "data"
    return {
        "data": data,
        "prefixes": data / "prefixes",
        "games": data / "games",
        "installers": data / "installers",
        "settings": tmp_path / "config" / "settings.json",
    }


@pytest.fixture
def store(layout: dict[str, Path]) -> SettingsStore:
    return SettingsStore(layout["settings"])


@pytest.fixture
def ownership(layout: dict[str, Path]) -> OwnershipService:
    return OwnershipService(
        layout["prefixes"], layout["games"], layout["installers"]
    )


@pytest.fixture
def manager(database: Database, layout: dict[str, Path], store: SettingsStore):
    return GameManager(database, layout["prefixes"], settings_store=store)


def _exe(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.write_bytes(b"MZ")
    return path


def _draft(tmp_path: Path, name: str) -> GameDraft:
    return GameDraft(
        name=name,
        platform=Platform.WINDOWS,
        executable=str(_exe(tmp_path, f"{name}.exe")),
    )


def _use_shared(store: SettingsStore) -> None:
    store.save(LauncherSettings(wine_prefix_strategy=WinePrefixStrategy.SHARED))


def _initialized_prefix(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "system.reg").write_text("initialized", encoding="utf-8")
    (path / "drive_c").mkdir(exist_ok=True)
    return path


class _Wineboot:
    """Fake runner that records wineboot calls and creates system.reg."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, command, **kwargs):
        self.calls.append(kwargs["env"]["WINEPREFIX"])
        _initialized_prefix(Path(kwargs["env"]["WINEPREFIX"]))
        return subprocess.CompletedProcess(command, 0, "", "")


# ----------------------------------------------------------------------
# Settings


def test_default_strategy_is_per_game(layout: dict[str, Path]) -> None:
    store = SettingsStore(layout["settings"])
    assert store.load().wine_prefix_strategy is WinePrefixStrategy.PER_GAME


def test_strategy_persists_and_reloads(layout: dict[str, Path]) -> None:
    store = SettingsStore(layout["settings"])
    store.save(LauncherSettings(wine_prefix_strategy=WinePrefixStrategy.SHARED))

    reloaded = SettingsStore(layout["settings"]).load()
    assert reloaded.wine_prefix_strategy is WinePrefixStrategy.SHARED


def test_unknown_strategy_value_falls_back_to_per_game(
    layout: dict[str, Path],
) -> None:
    layout["settings"].parent.mkdir(parents=True)
    layout["settings"].write_text(
        '{"wine_source": "system", "wine_prefix_strategy": "bogus"}',
        encoding="utf-8",
    )
    loaded = SettingsStore(layout["settings"]).load()
    assert loaded.wine_prefix_strategy is WinePrefixStrategy.PER_GAME


def test_settings_without_strategy_key_load_as_per_game(
    layout: dict[str, Path],
) -> None:
    """A settings file written before this feature must still load."""
    layout["settings"].parent.mkdir(parents=True)
    layout["settings"].write_text(
        '{"wine_source": "custom", "custom_wine_path": "/usr/bin/wine"}',
        encoding="utf-8",
    )
    loaded = SettingsStore(layout["settings"]).load()
    assert loaded.wine_source.value == "custom"
    assert loaded.wine_prefix_strategy is WinePrefixStrategy.PER_GAME


# ----------------------------------------------------------------------
# Assignment


def test_per_game_strategy_assigns_one_prefix_per_game(
    tmp_path: Path, manager: GameManager, layout: dict[str, Path]
) -> None:
    first = manager.add_game(_draft(tmp_path, "Alpha"))
    second = manager.add_game(_draft(tmp_path, "Beta"))

    assert first.wine_prefix_path == str(layout["prefixes"] / first.id)
    assert second.wine_prefix_path == str(layout["prefixes"] / second.id)
    assert not first.wine_prefix_shared
    assert not second.wine_prefix_shared


def test_shared_strategy_assigns_one_shared_prefix_to_multiple_games(
    tmp_path: Path,
    manager: GameManager,
    store: SettingsStore,
    layout: dict[str, Path],
) -> None:
    _use_shared(store)
    first = manager.add_game(_draft(tmp_path, "Alpha"))
    second = manager.add_game(_draft(tmp_path, "Beta"))
    shared = str(layout["prefixes"] / ".shared")

    assert first.wine_prefix_path == shared
    assert second.wine_prefix_path == shared
    assert first.wine_prefix_shared and second.wine_prefix_shared


def test_linux_games_get_no_prefix_under_either_strategy(
    tmp_path: Path, manager: GameManager, store: SettingsStore
) -> None:
    _use_shared(store)
    executable = tmp_path / "native"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    game = manager.add_game(
        GameDraft(name="Native", platform=Platform.LINUX, executable=str(executable))
    )
    assert game.wine_prefix_path is None
    assert not game.wine_prefix_shared


def test_repeated_creation_reuses_shared_prefix_without_reinitializing(
    tmp_path: Path,
    database: Database,
    store: SettingsStore,
    layout: dict[str, Path],
    manager: GameManager,
) -> None:
    _use_shared(store)
    first = manager.add_game(_draft(tmp_path, "Alpha"))
    second = manager.add_game(_draft(tmp_path, "Beta"))
    runner = _Wineboot()
    launcher = LaunchManager(
        layout["prefixes"],
        store,
        run_factory=runner,
        which=lambda _name: "/usr/bin/wine",
    )

    first_path = launcher.ensure_prefix(first, "/usr/bin/wine")
    second_path = launcher.ensure_prefix(second, "/usr/bin/wine")
    third_path = launcher.ensure_prefix(first, "/usr/bin/wine")

    assert first_path == second_path == third_path == str(layout["prefixes"] / ".shared")
    # wineboot runs once; later games reuse the already initialized prefix.
    assert len(runner.calls) == 1


def test_switching_strategy_leaves_existing_prefixes_unchanged(
    tmp_path: Path,
    manager: GameManager,
    store: SettingsStore,
    layout: dict[str, Path],
) -> None:
    existing = manager.add_game(_draft(tmp_path, "Existing"))
    existing_prefix = Path(existing.wine_prefix_path or "")
    _initialized_prefix(existing_prefix)

    _use_shared(store)
    updated = manager.update_game(existing.id, _draft(tmp_path, "Existing"))

    assert updated.wine_prefix_path == str(existing_prefix)
    assert not updated.wine_prefix_shared
    assert (existing_prefix / "system.reg").is_file()
    assert not (layout["prefixes"] / ".shared").exists()


def test_external_prefix_is_never_redirected_to_shared(
    tmp_path: Path,
    manager: GameManager,
    store: SettingsStore,
    layout: dict[str, Path],
) -> None:
    external = _initialized_prefix(tmp_path / "elsewhere" / "prefix")
    game = manager.add_game(_draft(tmp_path, "External"))
    database = manager._database
    database.update_game(
        replace(
            game,
            wine_prefix_path=str(external),
            wine_prefix_shared=False,
        )
    )

    _use_shared(store)
    updated = manager.update_game(game.id, _draft(tmp_path, "External"))

    assert updated.wine_prefix_path == str(external)
    assert not updated.wine_prefix_shared


def test_installer_session_uses_its_assigned_prefix_after_strategy_change(
    tmp_path: Path,
    database: Database,
    ownership: OwnershipService,
    store: SettingsStore,
    layout: dict[str, Path],
) -> None:
    _use_shared(store)
    installers = InstallerManager(
        database, ownership, _FakeMount(), settings_store=store
    )
    iso = tmp_path / "disc.iso"
    iso.write_bytes(b"iso")
    session = installers.create_session(iso, "Installer Game")
    assert session.wine_prefix_shared
    assert installers.prefix_path(session) == layout["prefixes"] / ".shared"

    # Strategy changes after the session exists; resume must keep its prefix.
    store.save(LauncherSettings(wine_prefix_strategy=WinePrefixStrategy.PER_GAME))
    resumed = installers.get_session(session.game_id)
    assert resumed is not None
    assert installers.prefix_path(resumed) == layout["prefixes"] / ".shared"


def test_installer_session_resources_exclude_shared_prefix(
    tmp_path: Path,
    database: Database,
    ownership: OwnershipService,
    store: SettingsStore,
) -> None:
    _use_shared(store)
    installers = InstallerManager(
        database, ownership, _FakeMount(), settings_store=store
    )
    iso = tmp_path / "disc.iso"
    iso.write_bytes(b"iso")
    session = installers.create_session(iso, "Installer Game")

    kinds = [kind for kind, _path in installers.session_resources(session)]
    assert ResourceKind.PREFIX not in kinds
    assert ResourceKind.INSTALLER_SOURCE in kinds


# ----------------------------------------------------------------------
# Cleanup and ownership


def _shared_dir(layout: dict[str, Path]) -> Path:
    return _initialized_prefix(layout["prefixes"] / ".shared")


def test_removing_one_game_keeps_shared_prefix(
    tmp_path: Path,
    database: Database,
    manager: GameManager,
    store: SettingsStore,
    layout: dict[str, Path],
    ownership: OwnershipService,
) -> None:
    _use_shared(store)
    first = manager.add_game(_draft(tmp_path, "Alpha"))
    manager.add_game(_draft(tmp_path, "Beta"))
    shared = _shared_dir(layout)

    cleanup = CleanupService(database, ownership)
    manager.remove_game(first.id)

    assert shared.is_dir()
    assert (shared / "system.reg").is_file()
    # The remaining game still references the shared prefix as one resource.
    target = cleanup.shared_prefix_target()
    assert target.unavailable_reason is not None
    assert "still used" in target.unavailable_reason


def test_shared_deletion_is_blocked_while_referenced(
    tmp_path: Path,
    database: Database,
    manager: GameManager,
    store: SettingsStore,
    layout: dict[str, Path],
    ownership: OwnershipService,
) -> None:
    _use_shared(store)
    game = manager.add_game(_draft(tmp_path, "Alpha"))
    shared = _shared_dir(layout)
    cleanup = CleanupService(database, ownership)

    target = cleanup.shared_prefix_target()
    assert not target.can_delete
    outcome = cleanup.delete_target(target, authorized=True)
    assert not outcome.deleted
    assert outcome.skipped_reason is not None
    assert shared.is_dir()
    assert database.get_game(game.id) is not None


def test_shared_deletion_requires_authorization(
    database: Database,
    layout: dict[str, Path],
    ownership: OwnershipService,
) -> None:
    _shared_dir(layout)
    cleanup = CleanupService(database, ownership)
    target = cleanup.shared_prefix_target()
    with pytest.raises(OwnershipError):
        cleanup.delete_target(target, authorized=False)
    assert (layout["prefixes"] / ".shared").is_dir()


def test_shared_deletion_allowed_after_references_removed(
    tmp_path: Path,
    database: Database,
    manager: GameManager,
    store: SettingsStore,
    layout: dict[str, Path],
    ownership: OwnershipService,
) -> None:
    _use_shared(store)
    game = manager.add_game(_draft(tmp_path, "Alpha"))
    shared = _shared_dir(layout)
    cleanup = CleanupService(database, ownership)
    manager.remove_game(game.id)

    target = cleanup.shared_prefix_target()
    assert target.can_delete, target.unavailable_reason
    outcome = cleanup.delete_target(target, authorized=True)

    assert outcome.deleted
    assert not shared.exists()


def test_shared_deletion_reports_busy_lock_without_deleting(
    tmp_path: Path,
    database: Database,
    manager: GameManager,
    store: SettingsStore,
    layout: dict[str, Path],
    ownership: OwnershipService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A launch holding the shared-prefix lock blocks deletion with a message."""
    from minimal_game_launcher.services import prefix_lock

    _use_shared(store)
    game = manager.add_game(_draft(tmp_path, "Alpha"))
    manager.remove_game(game.id)
    shared = _shared_dir(layout)
    cleanup = CleanupService(database, ownership)
    target = cleanup.shared_prefix_target()
    assert target.can_delete

    # Keep the test fast: the production lifecycle wait is seconds long.
    monkeypatch.setattr(prefix_lock, "LIFECYCLE_LOCK_TIMEOUT", 0.2)
    # Hold lifecycle from another thread: deletion must time out, not block.
    import threading

    held = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with prefix_lock.shared_prefix_lifecycle_lock(layout["prefixes"]):
            held.set()
            release.wait(5)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert held.wait(5)
        outcome = cleanup.delete_target(target, authorized=True)
    finally:
        release.set()
        holder.join()

    assert not outcome.deleted
    assert outcome.skipped_reason is not None
    assert "Timed out" in outcome.skipped_reason
    assert shared.is_dir()


def test_shared_deletion_revalidates_references_after_preview(
    tmp_path: Path,
    database: Database,
    manager: GameManager,
    store: SettingsStore,
    layout: dict[str, Path],
    ownership: OwnershipService,
) -> None:
    """A game assigned to the shared prefix after preview blocks deletion."""
    _use_shared(store)
    game = manager.add_game(_draft(tmp_path, "Alpha"))
    manager.remove_game(game.id)
    shared = _shared_dir(layout)
    cleanup = CleanupService(database, ownership)
    target = cleanup.shared_prefix_target()
    assert target.can_delete

    manager.add_game(_draft(tmp_path, "Late"))
    outcome = cleanup.delete_target(target, authorized=True)

    assert not outcome.deleted
    assert shared.is_dir()


def test_shared_prefix_is_never_an_orphan_candidate_while_referenced(
    tmp_path: Path,
    database: Database,
    manager: GameManager,
    store: SettingsStore,
    layout: dict[str, Path],
    ownership: OwnershipService,
) -> None:
    _use_shared(store)
    manager.add_game(_draft(tmp_path, "Alpha"))
    _shared_dir(layout)
    cleanup = CleanupService(database, ownership)

    scan = cleanup.scan_orphans()
    assert all(
        candidate.kind is not ResourceKind.SHARED_PREFIX
        for candidate in scan.candidates
    )


def test_per_game_prefix_deletion_still_works(
    tmp_path: Path,
    database: Database,
    manager: GameManager,
    layout: dict[str, Path],
    ownership: OwnershipService,
) -> None:
    game = manager.add_game(_draft(tmp_path, "Alpha"))
    prefix = _initialized_prefix(layout["prefixes"] / game.id)
    cleanup = CleanupService(database, ownership)

    targets = cleanup.targets_for_game(game)
    prefix_target = next(t for t in targets if t.kind is ResourceKind.PREFIX)
    assert prefix_target.can_delete
    assert cleanup.delete_target(prefix_target, authorized=True).deleted
    assert not prefix.exists()


def test_targets_for_shared_game_never_include_per_game_prefix(
    tmp_path: Path,
    database: Database,
    manager: GameManager,
    store: SettingsStore,
    layout: dict[str, Path],
    ownership: OwnershipService,
) -> None:
    _use_shared(store)
    game = manager.add_game(_draft(tmp_path, "Alpha"))
    cleanup = CleanupService(database, ownership)
    kinds = [t.kind for t in cleanup.targets_for_game(game)]
    assert ResourceKind.PREFIX not in kinds
    assert ResourceKind.SHARED_PREFIX in kinds


# ----------------------------------------------------------------------
# Diagnostics


def _diagnostic_prefix_check(report):
    return next(check for check in report.checks if check.key == "prefix")


def _service(tmp_path: Path, layout: dict[str, Path], store: SettingsStore):
    return DiagnosticsService(
        layout["prefixes"],
        store,
        data_directory=layout["data"],
        home=tmp_path / "home",
        which=lambda _name: None,
        run=lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "", ""),
        vulkan_library=lambda _name: None,
    )


@pytest.mark.parametrize(
    ("shared", "external", "expected_type"),
    [
        (False, False, "per-game managed prefix"),
        (True, False, "shared managed prefix"),
        (False, True, "external prefix"),
    ],
)
def test_diagnostics_identifies_each_prefix_type(
    tmp_path: Path,
    layout: dict[str, Path],
    store: SettingsStore,
    shared: bool,
    external: bool,
    expected_type: str,
) -> None:
    if external:
        prefix = _initialized_prefix(tmp_path / "outside" / "prefix")
    elif shared:
        prefix = _initialized_prefix(layout["prefixes"] / ".shared")
    else:
        prefix = _initialized_prefix(layout["prefixes"] / "game-1")
    game = Game(
        id="game-1",
        name="Typed",
        platform=Platform.WINDOWS,
        executable=str(_exe(tmp_path, "typed.exe")),
        working_directory=str(tmp_path),
        launch_arguments="",
        cover_path=None,
        wine_prefix_path=str(prefix),
        gamemode_enabled=False,
        mangohud_enabled=False,
        created_at=NOW,
        updated_at=NOW,
        wine_prefix_shared=shared,
    )

    check = _diagnostic_prefix_check(
        _service(tmp_path, layout, store).generate(game)
    )
    assert f"Type: {expected_type}" in check.details
    assert check.status is not CheckStatus.PROBLEM


def test_diagnostics_report_omits_environment_values(
    tmp_path: Path, layout: dict[str, Path], store: SettingsStore
) -> None:
    game = Game(
        id="game-1",
        name="Secret",
        platform=Platform.WINDOWS,
        executable=str(_exe(tmp_path, "secret.exe")),
        working_directory=str(tmp_path),
        launch_arguments="",
        cover_path=None,
        wine_prefix_path=None,
        gamemode_enabled=False,
        mangohud_enabled=False,
        created_at=NOW,
        updated_at=NOW,
        environment={"MY_TOKEN": "hunter2"},
    )
    text = _service(tmp_path, layout, store).generate(game).to_text()
    assert "MY_TOKEN" in text
    assert "hunter2" not in text


# ----------------------------------------------------------------------
# Migration v5 -> v6


_V5_SCHEMA = """
CREATE TABLE games (
    id                TEXT PRIMARY KEY NOT NULL,
    name              TEXT NOT NULL,
    platform          TEXT NOT NULL CHECK (platform IN ('linux', 'windows')),
    executable        TEXT NOT NULL,
    working_directory TEXT NOT NULL,
    launch_arguments  TEXT NOT NULL DEFAULT '',
    cover_path        TEXT,
    wine_prefix_path  TEXT,
    gamemode_enabled  INTEGER NOT NULL DEFAULT 1,
    mangohud_enabled  INTEGER NOT NULL DEFAULT 0,
    environment       TEXT NOT NULL DEFAULT '{}',
    install_directory TEXT,
    install_directory_owned INTEGER NOT NULL DEFAULT 0,
    installer_source_directory TEXT,
    installer_source_owned INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE TABLE installer_sessions (
    game_id                   TEXT PRIMARY KEY NOT NULL,
    game_name                 TEXT NOT NULL,
    iso_path                  TEXT NOT NULL,
    staging_directory         TEXT NOT NULL,
    installer_relative_path   TEXT,
    install_directory         TEXT,
    install_directory_owned   INTEGER NOT NULL DEFAULT 0,
    status                    TEXT NOT NULL,
    message                   TEXT NOT NULL DEFAULT '',
    created_at                TEXT NOT NULL,
    updated_at                TEXT NOT NULL
);
PRAGMA user_version = 5;
"""


def test_v5_database_migrates_with_existing_records_unchanged(
    tmp_path: Path,
) -> None:
    path = tmp_path / "library.db"
    connection = sqlite3.connect(path)
    connection.executescript(_V5_SCHEMA)
    connection.execute(
        "INSERT INTO games (id, name, platform, executable, working_directory, "
        "wine_prefix_path, created_at, updated_at) VALUES "
        "('old-1', 'Old', 'windows', 'C:/old.exe', '/tmp', "
        "'/data/prefixes/old-1', ?, ?)",
        (NOW, NOW),
    )
    connection.execute(
        "INSERT INTO installer_sessions (game_id, game_name, iso_path, "
        "staging_directory, status, created_at, updated_at) VALUES "
        "('sess-1', 'Sess', '/iso', '/stage', 'deferred', ?, ?)",
        (NOW, NOW),
    )
    connection.commit()
    connection.close()

    database = Database(path)
    try:
        version = database._connection.execute("PRAGMA user_version").fetchone()[0]
        assert version == SCHEMA_VERSION
        game = database.get_game("old-1")
        assert game is not None
        assert game.wine_prefix_path == "/data/prefixes/old-1"
        assert game.wine_prefix_shared is False
        session = database.get_installer_session("sess-1")
        assert session is not None
        assert session.wine_prefix_path is None
        assert session.wine_prefix_shared is False
        assert session.source_kind.value == "iso"
        assert session.status is InstallerStatus.DEFERRED
    finally:
        database.close()


def test_v6_records_round_trip_shared_flag(
    tmp_path: Path, database: Database
) -> None:
    shared = Game(
        id="shared-1",
        name="Shared",
        platform=Platform.WINDOWS,
        executable="/x/shared.exe",
        working_directory="/x",
        launch_arguments="",
        cover_path=None,
        wine_prefix_path="/data/prefixes/.shared",
        gamemode_enabled=False,
        mangohud_enabled=False,
        created_at=NOW,
        updated_at=NOW,
        wine_prefix_shared=True,
    )
    database.add_game(shared)
    loaded = database.get_game("shared-1")
    assert loaded is not None and loaded.wine_prefix_shared is True


def test_newer_schema_is_still_refused(tmp_path: Path) -> None:
    """A database written by a future schema must not be opened or downgraded."""
    path = tmp_path / "future.db"
    connection = sqlite3.connect(path)
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    connection.commit()
    connection.close()
    with pytest.raises(RuntimeError, match="newer than supported"):
        Database(path)
