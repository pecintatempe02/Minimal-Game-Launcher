"""Concurrency tests for shared-prefix assignment vs. deletion.

Every path lives under ``tmp_path``. Wine is never run: prefixes are plain
directories and the launch runner is a fake. Threads are coordinated with
events so the interleaving is deterministic rather than timing-based.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from minimal_game_launcher.database import Database
from minimal_game_launcher.errors import SharedPrefixUnavailableError
from minimal_game_launcher.models import GameDraft, InstallerStatus, Platform
from minimal_game_launcher.services import prefix_lock
from minimal_game_launcher.services.cleanup import CleanupService
from minimal_game_launcher.services.game_manager import GameManager
from minimal_game_launcher.services.installer_manager import InstallerManager
from minimal_game_launcher.services.ownership import OwnershipService
from minimal_game_launcher.services.prefix_lock import (
    shared_prefix_lifecycle_lock,
)
from minimal_game_launcher.settings import (
    LauncherSettings,
    SettingsStore,
    WinePrefixStrategy,
)

JOIN_TIMEOUT = 10


class _FakeMount:
    def validate_iso(self, iso_path):
        return Path(iso_path).expanduser().resolve()


@pytest.fixture
def env(tmp_path: Path, database: Database):
    data = tmp_path / "data"
    prefixes = data / "prefixes"
    store = SettingsStore(tmp_path / "config" / "settings.json")
    store.save(LauncherSettings(wine_prefix_strategy=WinePrefixStrategy.SHARED))
    ownership = OwnershipService(prefixes, data / "games", data / "installers")
    manager = GameManager(database, prefixes, settings_store=store)
    installers = InstallerManager(database, ownership, _FakeMount(), settings_store=store)
    cleanup = CleanupService(database, ownership)
    shared = prefixes / ".shared"
    shared.mkdir(parents=True)
    (shared / "system.reg").write_text("x", encoding="utf-8")
    (shared / "drive_c").mkdir()
    exe = tmp_path / "game.exe"
    exe.write_bytes(b"MZ")
    return {
        "tmp": tmp_path,
        "prefixes": prefixes,
        "store": store,
        "manager": manager,
        "installers": installers,
        "cleanup": cleanup,
        "shared": shared,
        "exe": exe,
        "database": database,
    }


def _draft(env, name: str) -> GameDraft:
    return GameDraft(name=name, platform=Platform.WINDOWS, executable=str(env["exe"]))


def _run(target, *args) -> threading.Thread:
    thread = threading.Thread(target=target, args=args, daemon=True)
    thread.start()
    return thread


def test_assignment_before_deletion_validation_blocks_deletion(env) -> None:
    """A reference persisted first is seen by deletion, which refuses."""
    manager, cleanup, shared = env["manager"], env["cleanup"], env["shared"]
    target = cleanup.shared_prefix_target()
    assert target.can_delete  # no references yet

    manager.add_game(_draft(env, "First"))  # assignment completes first
    outcome = cleanup.delete_target(target, authorized=True)

    assert not outcome.deleted
    assert shared.is_dir()


def test_deletion_holding_lock_makes_assignment_wait_then_see_no_shared_prefix(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deletion holds lifecycle; assignment waits and only then persists."""
    manager, cleanup, shared, database = (
        env["manager"], env["cleanup"], env["shared"], env["database"],
    )
    monkeypatch.setattr(prefix_lock, "LIFECYCLE_LOCK_TIMEOUT", 5)

    # Make the shared prefix deletable: remove the reference first.
    target = cleanup.shared_prefix_target()
    assert target.can_delete

    entered = threading.Event()
    proceed = threading.Event()
    real_delete = cleanup._ownership.delete

    def paused_delete(*args, **kwargs):
        entered.set()
        proceed.wait(JOIN_TIMEOUT)
        return real_delete(*args, **kwargs)

    monkeypatch.setattr(cleanup._ownership, "delete", paused_delete)

    results: dict[str, object] = {}

    def do_delete() -> None:
        # SQLite connections are thread-bound: the deleter opens its own.
        worker_db = Database(database.path)
        try:
            worker_cleanup = CleanupService(
                worker_db, cleanup._ownership
            )
            worker_target = worker_cleanup.shared_prefix_target()
            results["delete"] = worker_cleanup.delete_target(
                worker_target, authorized=True
            )
        except BaseException as error:
            results["delete_error"] = error
        finally:
            worker_db.close()

    def do_add() -> None:
        # Each thread needs its own SQLite connection.
        worker_db = Database(database.path)
        try:
            worker = GameManager(
                worker_db, env["prefixes"], settings_store=env["store"]
            )
            results["game"] = worker.add_game(_draft(env, "Late"))
        except BaseException as error:
            results["add_error"] = error
        finally:
            worker_db.close()

    # Observe contention deterministically: the adder signals once it finds the
    # lifecycle lock held by deletion. Only then does deletion proceed.
    adder_waiting = threading.Event()
    real_lock = prefix_lock._LIFECYCLE_THREAD_LOCK
    monkeypatch.setattr(
        prefix_lock,
        "_LIFECYCLE_THREAD_LOCK",
        _ObservedLock(real_lock, adder_waiting),
    )

    deleter = _run(do_delete)
    if not entered.wait(JOIN_TIMEOUT):
        proceed.set()
        deleter.join(JOIN_TIMEOUT)
        pytest.fail(f"deletion never reached delete: {results.get('delete')}")
    adder = _run(do_add)
    assert adder_waiting.wait(JOIN_TIMEOUT), "assignment did not contend for the lock"
    # Assignment is still blocked on the lock: nothing has been persisted.
    assert database.list_games() == []

    proceed.set()
    deleter.join(JOIN_TIMEOUT)
    adder.join(JOIN_TIMEOUT)
    assert not deleter.is_alive() and not adder.is_alive()
    assert results["delete"].deleted
    assert not shared.exists()
    # Contended assignment after deletion is refused and persists no reference.
    assert "game" not in results
    assert isinstance(results.get("add_error"), SharedPrefixUnavailableError)
    assert database.list_games() == []


class _ObservedLock:
    """Signals when a caller finds the lifecycle lock busy (see rejection tests)."""

    def __init__(self, inner, waiting: threading.Event) -> None:
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


def test_installer_session_assignment_follows_same_rule(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    installers, cleanup, shared, database = (
        env["installers"], env["cleanup"], env["shared"], env["database"],
    )
    monkeypatch.setattr(prefix_lock, "LIFECYCLE_LOCK_TIMEOUT", 0.3)
    iso = env["tmp"] / "disc.iso"
    iso.write_bytes(b"iso")

    # A held lifecycle lock blocks session creation within the bounded wait.
    from minimal_game_launcher.errors import PrefixInitializationError

    with shared_prefix_lifecycle_lock(env["prefixes"]):
        with pytest.raises(PrefixInitializationError, match="Timed out"):
            installers.create_session(iso, "Blocked")
    assert database.list_installer_sessions() == []

    # Without a holder, the session persists with the shared reference.
    session = installers.create_session(iso, "Allowed")
    assert session.wine_prefix_shared is True
    assert installers.prefix_path(session) == env["prefixes"] / ".shared"


def test_registration_preserves_assigned_prefix_and_ownership(env) -> None:
    manager = env["manager"]
    game = manager.add_game(_draft(env, "Registered"))
    shared_path = str(env["prefixes"] / ".shared")
    registered = manager.register_installation(
        game.id,
        _draft(env, "Registered"),
        install_directory=str(env["tmp"] / "install"),
        install_directory_owned=True,
        installer_source_directory=None,
        installer_source_owned=False,
        wine_prefix_path=shared_path,
        wine_prefix_shared=True,
    )
    stored = env["database"].get_game(game.id)
    assert stored.wine_prefix_path == shared_path
    assert stored.wine_prefix_shared is True
    assert stored.install_directory_owned is True
    assert registered.wine_prefix_path == shared_path


def test_failed_deletion_keeps_references_and_directory(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, cleanup, shared, database = (
        env["manager"], env["cleanup"], env["shared"], env["database"],
    )
    game = manager.add_game(_draft(env, "Keep"))
    manager.remove_game(game.id)
    target = cleanup.shared_prefix_target()
    assert target.can_delete

    # Revalidation refuses after the lock is taken: a reference appeared.
    manager.add_game(_draft(env, "Appeared"))
    outcome = cleanup.delete_target(target, authorized=True)

    assert not outcome.deleted
    assert shared.is_dir()
    assert any(g.wine_prefix_shared for g in database.list_games())


def test_per_game_assignment_and_deletion_are_independent(env) -> None:
    store, manager, cleanup = env["store"], env["manager"], env["cleanup"]
    store.save(LauncherSettings(wine_prefix_strategy=WinePrefixStrategy.PER_GAME))
    game = manager.add_game(_draft(env, "Solo"))
    assert game.wine_prefix_shared is False

    # A held shared lifecycle lock must not block a per-game write.
    with shared_prefix_lifecycle_lock(env["prefixes"]):
        other = manager.add_game(_draft(env, "Other"))
    assert other.wine_prefix_shared is False

    manager.remove_game(game.id)
    target = cleanup.shared_prefix_target()
    assert target.kind.value == "shared_prefix"
    assert env["shared"].is_dir()  # shared prefix untouched by per-game work


def test_concurrent_assignment_launch_and_cleanup_do_not_deadlock(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Writers, cleanup, and an initialization holder finish within bounds.

    SQLite connections are thread-bound in this application, so each worker
    opens its own ``Database`` on the same file, as a separate caller would.
    """
    monkeypatch.setattr(prefix_lock, "LIFECYCLE_LOCK_TIMEOUT", 5)
    db_path = env["database"].path
    errors: list[BaseException] = []
    lock_root = env["prefixes"]

    def add(index: int) -> None:
        database = Database(db_path)
        try:
            store = env["store"]
            GameManager(database, lock_root, settings_store=store).add_game(
                _draft(env, f"Game {index}")
            )
        except BaseException as error:
            errors.append(error)
        finally:
            database.close()

    def clean() -> None:
        database = Database(db_path)
        try:
            CleanupService(
                database,
                OwnershipService(
                    lock_root, env["tmp"] / "games", env["tmp"] / "installers"
                ),
            ).shared_prefix_target()
        except BaseException as error:
            errors.append(error)
        finally:
            database.close()

    def init_holder() -> None:
        try:
            with prefix_lock.shared_prefix_lock(lock_root, timeout=5):
                threading.Event().wait(0.05)
        except BaseException as error:
            errors.append(error)

    threads = [_run(add, i) for i in range(6)]
    threads += [_run(clean), _run(init_holder)]
    for thread in threads:
        thread.join(JOIN_TIMEOUT)
    assert all(not t.is_alive() for t in threads), "deadlock or hang"
    assert errors == []
    assert len(env["database"].list_games()) == 6
