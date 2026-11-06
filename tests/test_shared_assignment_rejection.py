"""Shared-prefix assignment must not link to a prefix cleanup removed while it waited.

Every path lives under ``tmp_path``. Wine is never run. Contention is
deterministic: a holder thread owns the lifecycle lock, and the assignment
thread signals when it enters the lock's poll loop (via a module-local stand-in
for ``time``). Only then does the holder act and release. No timing sleeps
decide any outcome.

SQLite connections are thread-bound, so each worker opens its own ``Database``.
"""

from __future__ import annotations

import shutil
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from minimal_game_launcher.database import Database
from minimal_game_launcher.errors import LauncherError, SharedPrefixUnavailableError
from minimal_game_launcher.models import GameDraft, Platform
from minimal_game_launcher.services import prefix_lock
from minimal_game_launcher.services.game_manager import GameManager
from minimal_game_launcher.services.installer_manager import InstallerManager
from minimal_game_launcher.services.ownership import OwnershipService
from minimal_game_launcher.services.prefix_lock import shared_prefix_lifecycle_lock
from minimal_game_launcher.settings import (
    LauncherSettings,
    SettingsStore,
    WinePrefixStrategy,
)

JOIN_TIMEOUT = 10


class _FakeMount:
    def validate_iso(self, iso_path):
        return Path(iso_path).expanduser().resolve()


class _Ctx:
    """Services bound to one Database connection (one thread)."""

    def __init__(self, database: Database, env: dict) -> None:
        self.database = database
        self.manager = GameManager(database, env["prefixes"], settings_store=env["store"])
        self.installers = InstallerManager(
            database, env["ownership"], _FakeMount(), settings_store=env["store"]
        )


@pytest.fixture
def env(tmp_path: Path, database: Database):
    data = tmp_path / "data"
    prefixes = data / "prefixes"
    store = SettingsStore(tmp_path / "config" / "settings.json")
    store.save(LauncherSettings(wine_prefix_strategy=WinePrefixStrategy.SHARED))
    exe = tmp_path / "game.exe"
    exe.write_bytes(b"MZ")
    iso = tmp_path / "disc.iso"
    iso.write_bytes(b"iso")
    return {
        "tmp": tmp_path,
        "prefixes": prefixes,
        "shared": prefixes / ".shared",
        "store": store,
        "ownership": OwnershipService(prefixes, data / "games", data / "installers"),
        "database_path": database.path,
        "exe": exe,
        "iso": iso,
    }


def _make_shared(shared: Path) -> None:
    shared.mkdir(parents=True, exist_ok=True)
    (shared / "system.reg").write_text("x", encoding="utf-8")
    (shared / "drive_c").mkdir(exist_ok=True)


def _draft(env, name: str) -> GameDraft:
    return GameDraft(name=name, platform=Platform.WINDOWS, executable=str(env["exe"]))


def _open_ctx(env) -> _Ctx:
    return _Ctx(Database(env["database_path"]), env)


class _ObservedLock:
    """Wraps the lifecycle thread lock; signals when a caller finds it busy.

    A failed non-blocking acquire proves the holder owns the lock right now, and
    the holder does not release until ``waiting`` is set. That makes the
    contended state deterministic.
    """

    def __init__(self, inner: threading.Lock, waiting: threading.Event) -> None:
        self._inner = inner
        self._waiting = waiting

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        if self._inner.acquire(blocking=False):
            return True
        self._waiting.set()
        if not blocking:
            return False
        return self._inner.acquire(timeout=timeout)

    def release(self) -> None:
        self._inner.release()


def _contended(env, monkeypatch, action, on_held):
    """Run ``action(ctx)`` on a worker while a holder owns the lifecycle lock.

    ``on_held`` runs on the holder thread while it still owns the lock, after
    the worker has found the lock busy. Returns ``(result, error)``.
    """
    waiting = threading.Event()
    monkeypatch.setattr(
        prefix_lock,
        "_LIFECYCLE_THREAD_LOCK",
        _ObservedLock(threading.Lock(), waiting),
    )
    monkeypatch.setattr(prefix_lock, "LIFECYCLE_LOCK_TIMEOUT", JOIN_TIMEOUT)

    holder_ready = threading.Event()
    release = threading.Event()
    outcome: dict[str, object] = {}

    def hold() -> None:
        with shared_prefix_lifecycle_lock(env["prefixes"]):
            holder_ready.set()
            release.wait(JOIN_TIMEOUT)
            on_held()

    def run() -> None:
        ctx = _open_ctx(env)
        try:
            outcome["result"] = action(ctx)
        except BaseException as error:
            outcome["error"] = error
        finally:
            ctx.database.close()

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    assert holder_ready.wait(JOIN_TIMEOUT)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    assert waiting.wait(JOIN_TIMEOUT), "assignment never began waiting"
    release.set()
    holder.join(JOIN_TIMEOUT)
    worker.join(JOIN_TIMEOUT)
    assert not worker.is_alive() and not holder.is_alive()
    assert "error" in outcome or "result" in outcome, "worker produced nothing"
    return outcome.get("result"), outcome.get("error")


def _remove_shared(env) -> None:
    """Stand-in for cleanup's deletion while it holds the lifecycle lock."""
    shutil.rmtree(env["shared"], ignore_errors=True)


def _games(env):
    db = Database(env["database_path"])
    try:
        return db.list_games()
    finally:
        db.close()


def _sessions(env):
    db = Database(env["database_path"])
    try:
        return db.list_installer_sessions()
    finally:
        db.close()


# --- Without contention ----------------------------------------------------


def test_first_time_shared_assignment_without_contention_is_allowed(env) -> None:
    assert not env["shared"].exists()
    ctx = _open_ctx(env)
    try:
        game = ctx.manager.add_game(_draft(env, "First"))
    finally:
        ctx.database.close()

    assert game.wine_prefix_shared is True
    assert game.wine_prefix_path == str(env["shared"])
    assert not env["shared"].exists()  # the guard never creates .shared


def test_first_time_session_assignment_without_contention_is_allowed(env) -> None:
    assert not env["shared"].exists()
    ctx = _open_ctx(env)
    try:
        session = ctx.installers.create_session(env["iso"], "Fresh")
    finally:
        ctx.database.close()

    assert session.wine_prefix_shared is True
    assert not env["shared"].exists()


# --- Contended, cleanup removed the prefix --------------------------------


def test_contended_add_after_deletion_is_rejected_and_persists_nothing(
    env, monkeypatch
) -> None:
    _make_shared(env["shared"])

    result, error = _contended(
        env,
        monkeypatch,
        lambda ctx: ctx.manager.add_game(_draft(env, "Late")),
        on_held=lambda: _remove_shared(env),
    )

    assert result is None
    assert isinstance(error, SharedPrefixUnavailableError)
    assert _games(env) == []


def test_contended_session_after_deletion_is_rejected_and_persists_nothing(
    env, monkeypatch
) -> None:
    _make_shared(env["shared"])

    result, error = _contended(
        env,
        monkeypatch,
        lambda ctx: ctx.installers.create_session(env["iso"], "Late"),
        on_held=lambda: _remove_shared(env),
    )

    assert result is None
    assert isinstance(error, SharedPrefixUnavailableError)
    assert _sessions(env) == []


def test_contended_update_after_deletion_is_rejected_and_keeps_old_row(
    env, monkeypatch
) -> None:
    ctx = _open_ctx(env)
    try:
        game = ctx.manager.add_game(_draft(env, "Standalone"))
        # Store the game as a non-shared entry, as if created before the strategy.
        ctx.database.update_game(replace(game, wine_prefix_path=None, wine_prefix_shared=False))
    finally:
        ctx.database.close()
    _make_shared(env["shared"])

    result, error = _contended(
        env,
        monkeypatch,
        lambda ctx: ctx.manager.update_game(game.id, _draft(env, "Renamed")),
        on_held=lambda: _remove_shared(env),
    )

    assert result is None
    assert isinstance(error, SharedPrefixUnavailableError)
    stored = _games(env)[0]
    assert stored.name == "Standalone"
    assert stored.wine_prefix_path is None
    assert stored.wine_prefix_shared is False


def test_contended_registration_of_new_shared_reference_is_rejected(
    env, monkeypatch
) -> None:
    ctx = _open_ctx(env)
    try:
        game = ctx.manager.add_game(_draft(env, "Reg"))
        ctx.database.update_game(replace(game, wine_prefix_path=None, wine_prefix_shared=False))
    finally:
        ctx.database.close()
    _make_shared(env["shared"])

    result, error = _contended(
        env,
        monkeypatch,
        lambda ctx: ctx.manager.register_installation(
            game.id,
            _draft(env, "Reg"),
            install_directory=None,
            install_directory_owned=False,
            installer_source_directory=None,
            installer_source_owned=False,
            wine_prefix_path=str(env["shared"]),
            wine_prefix_shared=True,
        ),
        on_held=lambda: _remove_shared(env),
    )

    assert result is None
    assert isinstance(error, SharedPrefixUnavailableError)
    assert _games(env)[0].wine_prefix_shared is False


# --- Contended, .shared still present -------------------------------------


def test_contended_add_with_shared_present_succeeds(env, monkeypatch) -> None:
    _make_shared(env["shared"])

    result, error = _contended(
        env,
        monkeypatch,
        lambda ctx: ctx.manager.add_game(_draft(env, "Waited")),
        on_held=lambda: None,
    )

    assert error is None
    assert result.wine_prefix_shared is True
    assert [g.id for g in _games(env)] == [result.id]


def test_contended_session_with_shared_present_succeeds(env, monkeypatch) -> None:
    _make_shared(env["shared"])

    result, error = _contended(
        env,
        monkeypatch,
        lambda ctx: ctx.installers.create_session(env["iso"], "Waited"),
        on_held=lambda: None,
    )

    assert error is None
    assert result.wine_prefix_shared is True
    assert len(_sessions(env)) == 1


# --- Re-save of an already-persisted reference is never refused ------------


def test_session_resave_of_persisted_reference_is_not_refused(env, monkeypatch) -> None:
    _make_shared(env["shared"])
    ctx = _open_ctx(env)
    try:
        session = ctx.installers.create_session(env["iso"], "Resume")
    finally:
        ctx.database.close()
    _remove_shared(env)  # the persisted reference now points at a missing prefix

    result, error = _contended(
        env,
        monkeypatch,
        lambda ctx: ctx.installers.save_session(replace(session, message="progress")),
        on_held=lambda: None,
    )

    assert error is None
    assert result.message == "progress"
    assert _sessions(env)[0].message == "progress"


# --- Per-game assignment is unaffected -------------------------------------


def test_per_game_assignment_unaffected_by_held_lock_and_missing_shared(
    env, monkeypatch
) -> None:
    """Per-game writes never take the lifecycle lock, so a held lock and a
    missing .shared do not affect them."""
    env["store"].save(LauncherSettings(wine_prefix_strategy=WinePrefixStrategy.PER_GAME))
    assert not env["shared"].exists()

    holder_ready = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with shared_prefix_lifecycle_lock(env["prefixes"]):
            holder_ready.set()
            release.wait(JOIN_TIMEOUT)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    try:
        assert holder_ready.wait(JOIN_TIMEOUT)
        ctx = _open_ctx(env)
        try:
            game = ctx.manager.add_game(_draft(env, "Solo"))
        finally:
            ctx.database.close()
    finally:
        release.set()
        holder.join(JOIN_TIMEOUT)

    assert game.wine_prefix_shared is False
    assert game.wine_prefix_path == str(env["prefixes"] / game.id)


# --- Domain error for the UI ----------------------------------------------


def test_rejection_is_launcher_error_with_actionable_message(env, monkeypatch) -> None:
    _make_shared(env["shared"])

    _, error = _contended(
        env,
        monkeypatch,
        lambda ctx: ctx.manager.add_game(_draft(env, "Msg")),
        on_held=lambda: _remove_shared(env),
    )

    assert isinstance(error, LauncherError)
    message = str(error)
    assert "removed while this change was waiting" in message
    assert "Nothing was saved" in message
    assert "Try again" in message
