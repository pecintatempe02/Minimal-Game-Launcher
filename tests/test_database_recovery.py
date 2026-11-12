"""Tests for library database recovery and failure boundaries."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from minimal_game_launcher.database import SCHEMA_VERSION, Database, open_library


def test_healthy_library_opens_without_recovery(tmp_path: Path) -> None:
    target = tmp_path / "library.db"

    database, backup = open_library(target)
    database.close()

    assert backup is None
    assert target.is_file()


def test_corrupt_library_is_moved_aside_not_deleted(tmp_path: Path) -> None:
    target = tmp_path / "library.db"
    garbage = b"this is not a sqlite database" * 64
    target.write_bytes(garbage)

    database, backup = open_library(target)
    try:
        assert backup is not None
        assert backup.read_bytes() == garbage
        assert backup.parent == tmp_path
        assert backup.name.startswith("library.db.damaged-")
        assert database.list_games() == []
    finally:
        database.close()


def test_stale_wal_sidecars_are_moved_with_the_damaged_file(tmp_path: Path) -> None:
    target = tmp_path / "library.db"
    target.write_bytes(b"not sqlite" * 50)
    wal = Path(f"{target}-wal")
    wal.write_bytes(b"stale wal contents")

    database, backup = open_library(target)
    database.close()

    assert backup is not None
    assert not wal.exists()
    assert Path(f"{backup}-wal").read_bytes() == b"stale wal contents"


def test_recovered_library_is_usable_and_persists(tmp_path: Path) -> None:
    target = tmp_path / "library.db"
    target.write_bytes(b"corrupt" * 100)

    database, _backup = open_library(target)
    database.close()

    reopened = Database(target)
    try:
        assert reopened.list_games() == []
        assert reopened.path == target
    finally:
        reopened.close()


def test_newer_schema_is_not_treated_as_damage(tmp_path: Path) -> None:
    target = tmp_path / "library.db"
    connection = sqlite3.connect(target)
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    connection.commit()
    connection.close()

    with pytest.raises(RuntimeError, match="newer than supported"):
        open_library(target)

    assert target.is_file()
    assert not any(
        path.name.startswith("library.db.damaged-") for path in tmp_path.iterdir()
    )


def test_header_with_damaged_pages_is_moved_aside(tmp_path: Path) -> None:
    target = tmp_path / "library.db"
    damaged = b"SQLite format 3\x00" + b"\xff" * 4096
    target.write_bytes(damaged)

    database, backup = open_library(target)
    database.close()

    assert backup is not None
    assert backup.read_bytes() == damaged


def test_locked_database_is_not_moved(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "library.db"
    Database(target).close()
    original = target.read_bytes()

    def locked(self, path) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(Database, "__init__", locked)

    with pytest.raises(sqlite3.OperationalError, match="locked"):
        open_library(target)

    assert target.read_bytes() == original
    assert not any(
        path.name.startswith("library.db.damaged-") for path in tmp_path.iterdir()
    )


def test_failed_open_closes_connection(tmp_path: Path, monkeypatch) -> None:
    closed: list[bool] = []
    original_connect = sqlite3.connect

    class TrackingConnection:
        def __init__(self, real: sqlite3.Connection) -> None:
            self._real = real

        def __getattr__(self, name: str):
            return getattr(self._real, name)

        def __setattr__(self, name: str, value) -> None:
            if name == "_real":
                object.__setattr__(self, name, value)
            else:
                setattr(self._real, name, value)

        def close(self) -> None:
            closed.append(True)
            self._real.close()

    def connect(*args, **kwargs):
        return TrackingConnection(original_connect(*args, **kwargs))

    monkeypatch.setattr("minimal_game_launcher.database.sqlite3.connect", connect)
    target = tmp_path / "library.db"
    target.write_bytes(b"corrupt" * 100)

    with pytest.raises(sqlite3.DatabaseError):
        Database(target)

    assert closed == [True]


def _locking_connection(original, failures: int) -> type:
    """Wrap a connection so the first ``failures`` WAL switches report a lock."""

    class LockingConnection:
        def __init__(self, real) -> None:
            object.__setattr__(self, "_real", real)
            object.__setattr__(self, "_failures", failures)

        def __getattr__(self, name):
            return getattr(self._real, name)

        def __setattr__(self, name, value) -> None:
            if name in {"_real", "_failures"}:
                object.__setattr__(self, name, value)
            else:
                setattr(self._real, name, value)

        def execute(self, sql, *args):
            if sql.strip().upper().startswith("PRAGMA JOURNAL_MODE"):
                if self._failures > 0:
                    self._failures -= 1
                    raise sqlite3.OperationalError("database is locked")
            return self._real.execute(sql, *args)

    return LockingConnection


def test_transient_wal_lock_is_retried(tmp_path: Path, monkeypatch) -> None:
    """A concurrent opener briefly holding the lock must not fail the open."""
    original_connect = sqlite3.connect
    wrapper = _locking_connection(original_connect, failures=1)
    attempts: list[str] = []

    def connect(*args, **kwargs):
        connection = wrapper(original_connect(*args, **kwargs))
        original_execute = connection.execute

        def counting_execute(sql, *exec_args):
            if sql.strip().upper().startswith("PRAGMA JOURNAL_MODE"):
                attempts.append(sql)
            return original_execute(sql, *exec_args)

        object.__setattr__(connection, "execute", counting_execute)
        return connection

    monkeypatch.setattr("minimal_game_launcher.database.sqlite3.connect", connect)
    monkeypatch.setattr("minimal_game_launcher.database.time.sleep", lambda _s: None)

    database = Database(tmp_path / "library.db")
    try:
        assert database.list_games() == []
    finally:
        database.close()

    assert len(attempts) == 2


def test_non_lock_wal_failure_is_not_retried(tmp_path: Path, monkeypatch) -> None:
    """Real errors (permissions, corruption) must surface immediately."""
    original_connect = sqlite3.connect
    attempts: list[str] = []

    class BrokenConnection:
        def __init__(self, real) -> None:
            object.__setattr__(self, "_real", real)

        def __getattr__(self, name):
            return getattr(self._real, name)

        def __setattr__(self, name, value) -> None:
            if name == "_real":
                object.__setattr__(self, name, value)
            else:
                setattr(self._real, name, value)

        def execute(self, sql, *args):
            if sql.strip().upper().startswith("PRAGMA JOURNAL_MODE"):
                attempts.append(sql)
                raise sqlite3.OperationalError("unable to open database file")
            return self._real.execute(sql, *args)

    monkeypatch.setattr(
        "minimal_game_launcher.database.sqlite3.connect",
        lambda *a, **k: BrokenConnection(original_connect(*a, **k)),
    )

    with pytest.raises(sqlite3.OperationalError, match="unable to open"):
        Database(tmp_path / "library.db")

    assert len(attempts) == 1
