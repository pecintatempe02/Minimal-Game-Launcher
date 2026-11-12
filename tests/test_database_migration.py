"""Atomic schema migration tests: each version commits fully or not at all."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from minimal_game_launcher import database as database_module
from minimal_game_launcher.database import SCHEMA_VERSION, Database

NOW = datetime(2026, 1, 1, tzinfo=UTC).isoformat()

# Schema as it existed at user_version 5 (before the shared-prefix columns).
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


def _make_v5(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(_V5_SCHEMA)
    connection.execute(
        "INSERT INTO games (id, name, platform, executable, working_directory, "
        "created_at, updated_at) VALUES "
        "('old-1', 'Old', 'windows', 'C:/old.exe', '/tmp', ?, ?)",
        (NOW, NOW),
    )
    connection.commit()
    connection.close()


def _version(path: Path) -> int:
    connection = sqlite3.connect(path)
    try:
        return connection.execute("PRAGMA user_version").fetchone()[0]
    finally:
        connection.close()


def _columns(path: Path, table: str) -> set[str]:
    connection = sqlite3.connect(path)
    try:
        return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    finally:
        connection.close()


def test_fresh_database_gets_current_schema(tmp_path: Path) -> None:
    path = tmp_path / "fresh.db"
    database = Database(path)
    database.close()
    assert _version(path) == SCHEMA_VERSION
    assert "wine_prefix_shared" in _columns(path, "games")
    assert "gamescope_resolution" in _columns(path, "games")
    assert "wine_prefix_path" in _columns(path, "installer_sessions")
    assert {
        "show_in_library",
        "display_mode",
        "virtual_desktop_resolution",
        "gamescope_enabled",
        "gamescope_output_resolution",
        "gamescope_fps_limit",
        "gamescope_vsync",
        "gamescope_filter",
        "gamescope_fullscreen",
    } <= _columns(path, "games")


def test_v5_database_migrates_through_v6(tmp_path: Path) -> None:
    path = tmp_path / "v5.db"
    _make_v5(path)
    database = Database(path)
    try:
        game = database.get_game("old-1")
        assert game is not None and game.name == "Old"
    finally:
        database.close()
    assert _version(path) == SCHEMA_VERSION
    assert "wine_prefix_shared" in _columns(path, "games")


def test_failure_after_first_ddl_rolls_back_whole_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "v5.db"
    _make_v5(path)
    # The first statement of v6 is valid; the second is not, so the failure
    # happens after DDL has already run inside the transaction.
    broken = (
        (
            6,
            (
                "ALTER TABLE games ADD COLUMN wine_prefix_shared "
                "INTEGER NOT NULL DEFAULT 0 "
                "CHECK (wine_prefix_shared IN (0, 1))",
                "ALTER TABLE no_such_table ADD COLUMN x INTEGER",
            ),
        ),
    )
    monkeypatch.setattr(database_module, "_MIGRATIONS", broken)

    with pytest.raises(sqlite3.OperationalError):
        Database(path)

    # No partial column, version unchanged, and the database is not damaged.
    assert _version(path) == 5
    assert "wine_prefix_shared" not in _columns(path, "games")


def test_reopen_and_retry_after_cause_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "v5.db"
    _make_v5(path)
    with monkeypatch.context() as patched:
        patched.setattr(
            database_module,
            "_MIGRATIONS",
            ((6, ("ALTER TABLE no_such_table ADD COLUMN x INTEGER",)),),
        )
        with pytest.raises(sqlite3.OperationalError):
            Database(path)
    assert _version(path) == 5

    # Monkeypatch is undone: the real migration now succeeds on retry.
    database = Database(path)
    try:
        game = database.get_game("old-1")
        assert game is not None
        assert game.wine_prefix_shared is False
    finally:
        database.close()
    assert _version(path) == SCHEMA_VERSION


def test_newer_schema_is_still_refused_without_change(tmp_path: Path) -> None:
    path = tmp_path / "future.db"
    connection = sqlite3.connect(path)
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    connection.commit()
    connection.close()
    with pytest.raises(RuntimeError, match="newer than supported"):
        Database(path)
    assert _version(path) == SCHEMA_VERSION + 1


_V6_UPGRADES = """
ALTER TABLE games ADD COLUMN wine_prefix_shared INTEGER NOT NULL DEFAULT 0
    CHECK (wine_prefix_shared IN (0, 1));
ALTER TABLE installer_sessions ADD COLUMN wine_prefix_path TEXT;
ALTER TABLE installer_sessions ADD COLUMN wine_prefix_shared INTEGER NOT NULL
    DEFAULT 0 CHECK (wine_prefix_shared IN (0, 1));
"""


def _make_v6(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        _V5_SCHEMA.replace("PRAGMA user_version = 5;", "")
    )
    connection.executescript(_V6_UPGRADES)
    connection.execute(
        "INSERT INTO games (id, name, platform, executable, working_directory, "
        "created_at, updated_at) VALUES "
        "('old-1', 'Old', 'windows', 'C:/old.exe', '/tmp', ?, ?)",
        (NOW, NOW),
    )
    connection.execute(
        "INSERT INTO installer_sessions (game_id, game_name, iso_path, "
        "staging_directory, installer_relative_path, install_directory, "
        "install_directory_owned, status, message, created_at, updated_at, "
        "wine_prefix_path, wine_prefix_shared) VALUES "
        "('sess-1', 'Old Game', '/disc/old.iso', '/data/installers/sess-1', "
        "'setup.exe', NULL, 0, 'staged', 'kept', ?, ?, '/prefixes/sess-1', 0)",
        (NOW, NOW),
    )
    connection.execute("PRAGMA user_version = 6")
    connection.commit()
    connection.close()


def test_v6_installer_sessions_migrate_as_iso(tmp_path: Path) -> None:
    path = tmp_path / "v6.db"
    _make_v6(path)

    database = Database(path)
    try:
        session = database.get_installer_session("sess-1")
        assert session is not None
        assert session.source_kind.value == "iso"
        assert session.source_path == "/disc/old.iso"
        assert session.source_kind_is_iso is True
        assert session.staging_directory == "/data/installers/sess-1"
        assert session.installer_relative_path == "setup.exe"
        assert session.message == "kept"
        assert database.get_game("old-1") is not None
    finally:
        database.close()

    assert _version(path) == SCHEMA_VERSION
    assert "source_kind" in _columns(path, "installer_sessions")


_V7_UPGRADES = """
ALTER TABLE installer_sessions ADD COLUMN source_kind TEXT NOT NULL
    DEFAULT 'iso' CHECK (source_kind IN ('iso', 'executable'));
"""

_V8_UPGRADES = """
ALTER TABLE games ADD COLUMN gamescope_resolution TEXT NOT NULL DEFAULT '';
"""


def _make_v8(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        _V5_SCHEMA.replace("PRAGMA user_version = 5;", "")
    )
    connection.executescript(_V6_UPGRADES)
    connection.executescript(_V7_UPGRADES)
    connection.executescript(_V8_UPGRADES)
    connection.execute(
        "INSERT INTO games (id, name, platform, executable, working_directory, "
        "gamescope_resolution, created_at, updated_at) VALUES "
        "('old-1', 'Old', 'windows', 'C:/old.exe', '/tmp', '1024x768', ?, ?)",
        (NOW, NOW),
    )
    connection.execute("PRAGMA user_version = 8")
    connection.commit()
    connection.close()


def _make_v7(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        _V5_SCHEMA.replace("PRAGMA user_version = 5;", "")
    )
    connection.executescript(_V6_UPGRADES)
    connection.executescript(_V7_UPGRADES)
    connection.execute(
        "INSERT INTO games (id, name, platform, executable, working_directory, "
        "wine_prefix_path, created_at, updated_at) VALUES "
        "('old-1', 'Old', 'windows', 'C:/old.exe', '/tmp', "
        "'/prefixes/old-1', ?, ?)",
        (NOW, NOW),
    )
    connection.execute("PRAGMA user_version = 7")
    connection.commit()
    connection.close()


def test_v7_migrates_through_v8_and_v9(tmp_path: Path) -> None:
    """The historical v7 path composes both later migrations in order."""
    path = tmp_path / "v7.db"
    _make_v7(path)

    database = Database(path)
    try:
        game = database.get_game("old-1")
        assert game is not None
        assert game.show_in_library is True
        assert game.display_mode.value == "default"
        assert game.gamescope_resolution == ""
        assert game.gamescope_active is False
        assert game.wine_prefix_path == "/prefixes/old-1"
        assert game.wine_prefix_shared is False
    finally:
        database.close()

    assert _version(path) == SCHEMA_VERSION
    assert {"show_in_library", "gamescope_resolution"} <= _columns(path, "games")


def test_v8_migrates_to_v9_with_legacy_gamescope_behaviour(
    tmp_path: Path,
) -> None:
    """Existing rows stay visible and keep gamescope active via their resolution."""
    path = tmp_path / "v8.db"
    _make_v8(path)

    database = Database(path)
    try:
        game = database.get_game("old-1")
        assert game is not None
        assert game.show_in_library is True
        assert game.display_mode.value == "default"
        assert game.virtual_desktop_resolution == ""
        assert game.gamescope_enabled is False
        assert game.gamescope_active is True
        assert game.gamescope_fullscreen is True
        assert game.gamescope_fps_limit == 0
        assert game.gamescope_output_resolution == ""
    finally:
        database.close()

    assert _version(path) == SCHEMA_VERSION
    assert "show_in_library" in _columns(path, "games")


def test_apply_migration_skips_when_another_process_already_stamped(
    tmp_path: Path,
) -> None:
    """A concurrent migrator that already stamped the version is a no-op."""
    path = tmp_path / "v8.db"
    _make_v8(path)
    database = Database(path)
    try:
        # Re-running the v9 DDL would fail with "duplicate column name" if the
        # version were not re-checked after taking the write lock.
        database._apply_migration(
            9,
            (
                "ALTER TABLE games ADD COLUMN show_in_library "
                "INTEGER NOT NULL DEFAULT 1",
            ),
        )
        assert _version(path) == SCHEMA_VERSION
        assert "show_in_library" in _columns(path, "games")
    finally:
        database.close()


def test_v9_migration_rolls_back_after_partial_ddl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed v9 keeps user_version 8 and adds none of its columns."""
    path = tmp_path / "v8.db"
    _make_v8(path)
    broken = (
        (
            9,
            (
                "ALTER TABLE games ADD COLUMN show_in_library "
                "INTEGER NOT NULL DEFAULT 1 "
                "CHECK (show_in_library IN (0, 1))",
                "ALTER TABLE no_such_table ADD COLUMN x INTEGER",
            ),
        ),
    )
    monkeypatch.setattr(database_module, "_MIGRATIONS", broken)

    with pytest.raises(sqlite3.OperationalError):
        Database(path)

    assert _version(path) == 8
    assert "show_in_library" not in _columns(path, "games")
