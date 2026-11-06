"""SQLite persistence for the game library.

All SQL in the application lives in this module. The database stores metadata
only: it never creates, moves, or deletes game executables, installation
directories, Wine prefixes, or cover files.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from minimal_game_launcher.models import (
    Game,
    InstallerSession,
    InstallerSourceKind,
    InstallerStatus,
    Platform,
)

LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = 7
_SQLITE_HEADER = b"SQLite format 3\x00"

#: Full schema for a brand-new database. Each entry is one statement so it can
#: run inside the same explicit transaction as the version stamp.
_SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS games (
        id                TEXT PRIMARY KEY NOT NULL,
        name              TEXT NOT NULL,
        platform          TEXT NOT NULL CHECK (platform IN ('linux', 'windows')),
        executable        TEXT NOT NULL,
        working_directory TEXT NOT NULL,
        launch_arguments  TEXT NOT NULL DEFAULT '',
        cover_path        TEXT,
        wine_prefix_path  TEXT,
        gamemode_enabled  INTEGER NOT NULL DEFAULT 1
                          CHECK (gamemode_enabled IN (0, 1)),
        mangohud_enabled   INTEGER NOT NULL DEFAULT 0
                          CHECK (mangohud_enabled IN (0, 1)),
        environment       TEXT NOT NULL DEFAULT '{}',
        install_directory TEXT,
        install_directory_owned INTEGER NOT NULL DEFAULT 0
                          CHECK (install_directory_owned IN (0, 1)),
        installer_source_directory TEXT,
        installer_source_owned INTEGER NOT NULL DEFAULT 0
                          CHECK (installer_source_owned IN (0, 1)),
        wine_prefix_shared INTEGER NOT NULL DEFAULT 0
                          CHECK (wine_prefix_shared IN (0, 1)),
        created_at        TEXT NOT NULL,
        updated_at        TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS games_name_nocase ON games(name COLLATE NOCASE)",
    "CREATE INDEX IF NOT EXISTS games_created_at ON games(created_at)",
    """
    CREATE TABLE IF NOT EXISTS installer_sessions (
        game_id                   TEXT PRIMARY KEY NOT NULL,
        game_name                 TEXT NOT NULL,
        iso_path                  TEXT NOT NULL,
        staging_directory         TEXT NOT NULL,
        installer_relative_path   TEXT,
        install_directory         TEXT,
        install_directory_owned   INTEGER NOT NULL DEFAULT 0
                                  CHECK (install_directory_owned IN (0, 1)),
        status                    TEXT NOT NULL
                                  CHECK (status IN (
                                      'pending', 'staged', 'installing',
                                      'installer_finished', 'deferred',
                                      'installed', 'failed'
                                  )),
        message                   TEXT NOT NULL DEFAULT '',
        created_at                TEXT NOT NULL,
        updated_at                TEXT NOT NULL,
        wine_prefix_path          TEXT,
        wine_prefix_shared        INTEGER NOT NULL DEFAULT 0
                                  CHECK (wine_prefix_shared IN (0, 1)),
        source_kind               TEXT NOT NULL DEFAULT 'iso'
                                  CHECK (source_kind IN ('iso', 'executable'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS installer_sessions_updated_at "
    "ON installer_sessions(updated_at)",
)

#: Upgrade steps keyed by the version they produce. Each step runs in its own
#: transaction, in order, for every database older than its target. Existing rows
#: are never rewritten; new columns take their documented defaults.
_MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (
        # Phase 4: independent performance toggles.
        2,
        (
            "ALTER TABLE games ADD COLUMN gamemode_enabled "
            "INTEGER NOT NULL DEFAULT 1 "
            "CHECK (gamemode_enabled IN (0, 1))",
            "ALTER TABLE games ADD COLUMN mangohud_enabled "
            "INTEGER NOT NULL DEFAULT 0 "
            "CHECK (mangohud_enabled IN (0, 1))",
        ),
    ),
    (
        # Step 1: optional per-game environment; the default is empty.
        3,
        ("ALTER TABLE games ADD COLUMN environment TEXT NOT NULL DEFAULT '{}'",),
    ),
    (
        # Step 3: ownership metadata. Existing records keep NULL paths and
        # non-owned flags, so nothing on disk is mistaken for MGL-owned data.
        4,
        (
            "ALTER TABLE games ADD COLUMN install_directory TEXT",
            "ALTER TABLE games ADD COLUMN install_directory_owned "
            "INTEGER NOT NULL DEFAULT 0 "
            "CHECK (install_directory_owned IN (0, 1))",
            "ALTER TABLE games ADD COLUMN installer_source_directory TEXT",
            "ALTER TABLE games ADD COLUMN installer_source_owned "
            "INTEGER NOT NULL DEFAULT 0 "
            "CHECK (installer_source_owned IN (0, 1))",
        ),
    ),
    (
        # Step 4: resumable installer sessions in a separate table.
        5,
        (
            """
            CREATE TABLE IF NOT EXISTS installer_sessions (
                game_id                   TEXT PRIMARY KEY NOT NULL,
                game_name                 TEXT NOT NULL,
                iso_path                  TEXT NOT NULL,
                staging_directory         TEXT NOT NULL,
                installer_relative_path   TEXT,
                install_directory         TEXT,
                install_directory_owned   INTEGER NOT NULL DEFAULT 0
                      CHECK (install_directory_owned IN (0, 1)),
                status                    TEXT NOT NULL
                      CHECK (status IN (
                          'pending', 'staged', 'installing',
                          'installer_finished', 'deferred',
                          'installed', 'failed'
                      )),
                message                   TEXT NOT NULL DEFAULT '',
                created_at                TEXT NOT NULL,
                updated_at                TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS installer_sessions_updated_at "
            "ON installer_sessions(updated_at)",
        ),
    ),
    (
        # Shared-prefix metadata. Existing rows keep 0 ("per-game").
        6,
        (
            "ALTER TABLE games ADD COLUMN wine_prefix_shared "
            "INTEGER NOT NULL DEFAULT 0 "
            "CHECK (wine_prefix_shared IN (0, 1))",
            "ALTER TABLE installer_sessions ADD COLUMN wine_prefix_path TEXT",
            "ALTER TABLE installer_sessions ADD COLUMN wine_prefix_shared "
            "INTEGER NOT NULL DEFAULT 0 "
            "CHECK (wine_prefix_shared IN (0, 1))",
        ),
    ),
    (
        # Step 10: standalone installer executables. Existing rows are ISO
        # sessions, which is the documented default.
        7,
        (
            "ALTER TABLE installer_sessions ADD COLUMN source_kind "
            "TEXT NOT NULL DEFAULT 'iso' "
            "CHECK (source_kind IN ('iso', 'executable'))",
        ),
    ),
)


class Database:
    """Own the SQLite connection and all game-library SQL operations."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path)
        try:
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA busy_timeout = 5000")
            self._migrate()
        except BaseException:
            self._connection.close()
            raise



    def _migrate(self) -> None:
        """Create or upgrade the schema using SQLite's user_version marker.

        Every version is one explicit transaction: its DDL, any data change, and
        the ``PRAGMA user_version`` bump commit together or not at all.
        """
        version = self._connection.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"Library database schema {version} is newer than supported "
                f"schema {SCHEMA_VERSION}."
            )
        if version < 1:
            # A fresh database already contains the current columns, so it is
            # stamped with the newest version and no ALTER statements are run.
            self._apply_migration(SCHEMA_VERSION, _SCHEMA)
            return

        for target, statements in _MIGRATIONS:
            if version < target:
                self._apply_migration(target, statements)

    def _apply_migration(
        self, version: int, statements: tuple[str, ...]
    ) -> None:
        """Run one schema version atomically and stamp it in the same commit.

        Python's sqlite3 module in its legacy transaction mode opens implicit
        transactions only for DML, so ``ALTER TABLE`` and ``CREATE TABLE`` would
        otherwise run in autocommit mode. The transaction is therefore started
        explicitly and rolled back as a whole on any failure.
        """
        connection = self._connection
        if connection.in_transaction:
            connection.commit()
        connection.execute("BEGIN IMMEDIATE")
        try:
            for statement in statements:
                connection.execute(statement)
            connection.execute(f"PRAGMA user_version = {version}")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    @staticmethod
    def _to_game(row: sqlite3.Row) -> Game:
        """Map a SQLite row to the immutable domain model."""
        return Game(
            id=row["id"],
            name=row["name"],
            platform=Platform(row["platform"]),
            executable=row["executable"],
            working_directory=row["working_directory"],
            launch_arguments=row["launch_arguments"],
            cover_path=row["cover_path"],
            wine_prefix_path=row["wine_prefix_path"],
            # SQLite has no boolean type; integers are the storage format.
            gamemode_enabled=bool(row["gamemode_enabled"]),
            mangohud_enabled=bool(row["mangohud_enabled"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            environment=_decode_environment(row["environment"]),
            install_directory=row["install_directory"],
            install_directory_owned=bool(row["install_directory_owned"]),
            installer_source_directory=row["installer_source_directory"],
            installer_source_owned=bool(row["installer_source_owned"]),
            wine_prefix_shared=bool(row["wine_prefix_shared"]),
        )

    @staticmethod
    def _to_installer_session(row: sqlite3.Row) -> InstallerSession:
        """Map a SQLite row to the resumable installer-session model."""
        return InstallerSession(
            game_id=row["game_id"],
            game_name=row["game_name"],
            iso_path=row["iso_path"],
            staging_directory=row["staging_directory"],
            installer_relative_path=row["installer_relative_path"],
            install_directory=row["install_directory"],
            install_directory_owned=bool(row["install_directory_owned"]),
            status=InstallerStatus(row["status"]),
            message=row["message"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            wine_prefix_path=row["wine_prefix_path"],
            wine_prefix_shared=bool(row["wine_prefix_shared"]),
            source_kind=InstallerSourceKind(row["source_kind"]),
        )

    def save_installer_session(self, session: InstallerSession) -> None:
        """Insert or replace one installer session by its stable game ID."""
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO installer_sessions (
                    game_id, game_name, iso_path, staging_directory,
                    installer_relative_path, install_directory,
                    install_directory_owned, status, message,
                    created_at, updated_at, wine_prefix_path,
                    wine_prefix_shared, source_kind
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(game_id) DO UPDATE SET
                    game_name = excluded.game_name,
                    iso_path = excluded.iso_path,
                    staging_directory = excluded.staging_directory,
                    installer_relative_path = excluded.installer_relative_path,
                    install_directory = excluded.install_directory,
                    install_directory_owned = excluded.install_directory_owned,
                    status = excluded.status,
                    message = excluded.message,
                    updated_at = excluded.updated_at,
                    wine_prefix_path = excluded.wine_prefix_path,
                    wine_prefix_shared = excluded.wine_prefix_shared,
                    source_kind = excluded.source_kind
                """,
                (
                    session.game_id,
                    session.game_name,
                    session.iso_path,
                    session.staging_directory,
                    session.installer_relative_path,
                    session.install_directory,
                    int(session.install_directory_owned),
                    session.status.value,
                    session.message,
                    session.created_at,
                    session.updated_at,
                    session.wine_prefix_path,
                    int(session.wine_prefix_shared),
                    session.source_kind.value,
                ),
            )

    def get_installer_session(self, game_id: str) -> InstallerSession | None:
        """Return one installer session by game ID, or ``None``."""
        row = self._connection.execute(
            "SELECT * FROM installer_sessions WHERE game_id = ?", (game_id,)
        ).fetchone()
        return self._to_installer_session(row) if row is not None else None

    def list_installer_sessions(self) -> list[InstallerSession]:
        """Return every session, newest first, without filtering by status."""
        rows = self._connection.execute(
            "SELECT * FROM installer_sessions ORDER BY updated_at DESC"
        ).fetchall()
        return [self._to_installer_session(row) for row in rows]

    def delete_installer_session(self, game_id: str) -> bool:
        """Delete only the session row, never staged or installed files."""
        with self._connection:
            cursor = self._connection.execute(
                "DELETE FROM installer_sessions WHERE game_id = ?", (game_id,)
            )
        return cursor.rowcount > 0

    def add_game(self, game: Game) -> None:
        """Insert a validated game record."""
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO games (
                    id, name, platform, executable, working_directory,
                    launch_arguments, cover_path, wine_prefix_path,
                    gamemode_enabled, mangohud_enabled, environment,
                    install_directory, install_directory_owned,
                    installer_source_directory, installer_source_owned,
                    wine_prefix_shared, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    game.id,
                    game.name,
                    game.platform.value,
                    game.executable,
                    game.working_directory,
                    game.launch_arguments,
                    game.cover_path,
                    game.wine_prefix_path,
                    int(game.gamemode_enabled),
                    int(game.mangohud_enabled),
                    json.dumps(game.environment, ensure_ascii=False),
                    game.install_directory,
                    int(game.install_directory_owned),
                    game.installer_source_directory,
                    int(game.installer_source_owned),
                    int(game.wine_prefix_shared),
                    game.created_at,
                    game.updated_at,
                ),
            )

    def update_game(self, game: Game) -> bool:
        """Update a game record; return whether a row existed."""
        with self._connection:
            cursor = self._connection.execute(
                """
                UPDATE games SET
                    name = ?,
                    platform = ?,
                    executable = ?,
                    working_directory = ?,
                    launch_arguments = ?,
                    cover_path = ?,
                    wine_prefix_path = ?,
                    gamemode_enabled = ?,
                    mangohud_enabled = ?,
                    environment = ?,
                    install_directory = ?,
                    install_directory_owned = ?,
                    installer_source_directory = ?,
                    installer_source_owned = ?,
                    wine_prefix_shared = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    game.name,
                    game.platform.value,
                    game.executable,
                    game.working_directory,
                    game.launch_arguments,
                    game.cover_path,
                    game.wine_prefix_path,
                    int(game.gamemode_enabled),
                    int(game.mangohud_enabled),
                    json.dumps(game.environment, ensure_ascii=False),
                    game.install_directory,
                    int(game.install_directory_owned),
                    game.installer_source_directory,
                    int(game.installer_source_owned),
                    int(game.wine_prefix_shared),
                    game.updated_at,
                    game.id,
                ),
            )
        return cursor.rowcount > 0

    def update_cover(
        self, game_id: str, cover_path: str | None, updated_at: str
    ) -> bool:
        """Update only a game's cover reference; return whether a row existed."""
        with self._connection:
            cursor = self._connection.execute(
                "UPDATE games SET cover_path = ?, updated_at = ? WHERE id = ?",
                (cover_path, updated_at, game_id),
            )
        return cursor.rowcount > 0

    def delete_game(self, game_id: str) -> bool:
        """Delete only the library row, never any external file.

        Cover images are user-selected files and may be shared by multiple
        entries, so they are not owned by this database and are never removed.
        """
        with self._connection:
            cursor = self._connection.execute("DELETE FROM games WHERE id = ?", (game_id,))
        return cursor.rowcount > 0

    def get_game(self, game_id: str) -> Game | None:
        """Return one game by ID, or ``None`` if it no longer exists."""
        row = self._connection.execute(
            "SELECT * FROM games WHERE id = ?", (game_id,)
        ).fetchone()
        return self._to_game(row) if row is not None else None

    def list_games(self) -> list[Game]:
        """Return every library entry without applying UI search or sorting."""
        rows = self._connection.execute("SELECT * FROM games").fetchall()
        return [self._to_game(row) for row in rows]

    def close(self) -> None:
        """Close the SQLite connection."""
        self._connection.close()


def _decode_environment(raw: str | None) -> dict[str, str]:
    """Decode a stored environment mapping without crashing the library.

    Values are never logged. A malformed legacy value falls back to an empty
    mapping so the rest of the game record remains usable.
    """
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        LOGGER.warning("Ignoring malformed stored environment for one game")
        return {}
    if not isinstance(decoded, dict):
        LOGGER.warning("Ignoring non-object stored environment for one game")
        return {}
    return {
        str(key): str(value)
        for key, value in decoded.items()
        if isinstance(key, str) and isinstance(value, str)
    }


def _lacks_sqlite_header(path: Path) -> bool:
    """Return True for a non-empty file that does not start with SQLite's header.

    Checked before SQLite opens the file, because a failed open can delete the
    ``-wal`` sidecar on close.
    """
    try:
        with path.open("rb") as handle:
            header = handle.read(len(_SQLITE_HEADER))
    except OSError:
        return False
    return bool(header) and header != _SQLITE_HEADER


def open_library(path: str | Path) -> tuple[Database, Path | None]:
    """Open the library, setting aside a damaged database instead of crashing.

    A non-empty file without SQLite's header is damaged and is set aside before
    SQLite opens it. A ``DatabaseError`` from opening a file that has the header
    (for example page corruption) is also treated as damage. Locked, unreadable,
    or unopenable files raise ``sqlite3.OperationalError`` and are re-raised
    untouched, because moving a file another launcher instance may be using
    would be destructive. A newer schema version raises ``RuntimeError`` and is
    also re-raised.

    Damaged files are renamed with a UTC timestamp suffix, never deleted. The
    ``-wal``/``-shm`` sidecars move with them so a stale write-ahead log is not
    replayed into the new library. For page-level corruption, SQLite may already
    have checkpointed or removed the ``-wal`` during the failed open, so that
    log is not guaranteed to survive. Returns the database and the backup path
    when recovery happened, otherwise ``None``.
    """
    target = Path(path).expanduser()
    if _lacks_sqlite_header(target):
        LOGGER.error("Library database %s is not a SQLite file", target)
        return _set_aside_and_reopen(target)
    try:
        return Database(target), None
    except sqlite3.OperationalError:
        raise
    except sqlite3.DatabaseError:
        LOGGER.exception("Library database %s is damaged", target)
    return _set_aside_and_reopen(target)


def _set_aside_and_reopen(target: Path) -> tuple[Database, Path]:
    """Rename a damaged library and its sidecars, then create an empty library."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    backup = target.with_name(f"{target.name}.damaged-{stamp}")
    for suffix in ("", "-wal", "-shm"):
        source = Path(f"{target}{suffix}")
        if source.exists():
            source.rename(Path(f"{backup}{suffix}"))
    LOGGER.warning("Damaged library moved to %s; starting an empty library", backup)
    return Database(target), backup
