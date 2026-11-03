"""SQLite persistence for the game library.

All SQL in the application lives in this module. The database stores metadata
only: it never creates, moves, or deletes game executables, installation
directories, Wine prefixes, or cover files.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from minimal_game_launcher.models import Game, Platform

LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = 2
_SQLITE_HEADER = b"SQLite format 3\x00"

_SCHEMA = """
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
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS games_name_nocase ON games(name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS games_created_at ON games(created_at);
"""


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
        """Create or upgrade the schema using SQLite's user_version marker."""
        version = self._connection.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"Library database schema {version} is newer than supported "
                f"schema {SCHEMA_VERSION}."
            )
        if version < 1:
            # A fresh database already contains the current columns, so it is
            # stamped with the newest version and no ALTER statements are run.
            with self._connection:
                self._connection.executescript(_SCHEMA)
                self._connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            return

        if version < 2:
            # Phase 4 adds independent performance toggles. Existing Phase 3
            # rows receive the documented defaults without being rewritten.
            with self._connection:
                self._connection.execute(
                    "ALTER TABLE games ADD COLUMN gamemode_enabled "
                    "INTEGER NOT NULL DEFAULT 1 "
                    "CHECK (gamemode_enabled IN (0, 1))"
                )
                self._connection.execute(
                    "ALTER TABLE games ADD COLUMN mangohud_enabled "
                    "INTEGER NOT NULL DEFAULT 0 "
                    "CHECK (mangohud_enabled IN (0, 1))"
                )
                self._connection.execute(
                    f"PRAGMA user_version = {SCHEMA_VERSION}"
                )

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
        )

    def add_game(self, game: Game) -> None:
        """Insert a validated game record."""
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO games (
                    id, name, platform, executable, working_directory,
                    launch_arguments, cover_path, wine_prefix_path,
                    gamemode_enabled, mangohud_enabled,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
