"""Tests for adding, editing, removing, validating, searching, and sorting."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import minimal_game_launcher.services.game_manager as game_manager_module
from minimal_game_launcher.database import Database
from minimal_game_launcher.errors import GameValidationError
from minimal_game_launcher.models import GameDraft, Platform
from minimal_game_launcher.services.game_manager import (
    SORT_NAME,
    SORT_RECENT,
    GameManager,
)


def _linux_draft(name: str, executable: Path, **overrides) -> GameDraft:
    values = {
        "name": name,
        "platform": Platform.LINUX,
        "executable": str(executable),
    }
    values.update(overrides)
    return GameDraft(**values)


def _windows_draft(name: str, executable: Path, **overrides) -> GameDraft:
    values = {
        "name": name,
        "platform": Platform.WINDOWS,
        "executable": str(executable),
    }
    values.update(overrides)
    return GameDraft(**values)


def test_add_windows_game(manager: GameManager, tmp_path: Path) -> None:
    """Windows registration stores metadata and reserves, but does not create, a prefix."""
    executable = tmp_path / "Some Game" / "game.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"MZ")

    game = manager.add_game(
        _windows_draft(
            "  Some Game  ",
            executable,
            launch_arguments='--windowed --lang "en US"',
        )
    )

    assert game.name == "Some Game"
    assert game.platform is Platform.WINDOWS
    assert game.executable == str(executable.resolve())
    assert game.working_directory == str(executable.parent.resolve())
    assert game.launch_arguments == '--windowed --lang "en US"'
    assert game.wine_prefix_path is not None
    assert Path(game.wine_prefix_path).parent == tmp_path / "prefixes"
    assert not Path(game.wine_prefix_path).exists()

    stored = manager.get_game(game.id)
    assert stored == game


def test_add_linux_native_game(
    manager: GameManager, make_executable, make_cover
) -> None:
    """A Linux executable is stored as a direct native entry."""
    executable = make_executable("native-game")
    cover = make_cover()

    game = manager.add_game(
        _linux_draft(
            "Native Game",
            executable,
            launch_arguments="--fullscreen",
            cover_path=str(cover),
        )
    )

    assert game.platform is Platform.LINUX
    assert game.executable == str(executable.resolve())
    assert game.wine_prefix_path is None
    assert game.cover_path == str(cover)
    assert manager.get_game(game.id) is not None


def test_edit_game_information(
    manager: GameManager, tmp_path: Path, make_cover
) -> None:
    """Editing updates metadata while preserving ID, creation time, and prefix."""
    executable = tmp_path / "edit-me.exe"
    executable.write_bytes(b"MZ")
    original = manager.add_game(_windows_draft("Original", executable))

    custom_directory = tmp_path / "custom-working-directory"
    custom_directory.mkdir()
    cover = make_cover("edited.png")

    updated = manager.update_game(
        original.id,
        _windows_draft(
            "Renamed Game",
            executable,
            working_directory=str(custom_directory),
            launch_arguments="-dx11",
            cover_path=str(cover),
        ),
    )

    assert updated.id == original.id
    assert updated.created_at == original.created_at
    assert updated.name == "Renamed Game"
    assert updated.working_directory == str(custom_directory.resolve())
    assert updated.launch_arguments == "-dx11"
    assert updated.cover_path == str(cover)
    assert updated.wine_prefix_path == original.wine_prefix_path
    assert manager.get_game(original.id) == updated


def test_remove_library_entry_leaves_all_files(
    manager: GameManager, tmp_path: Path, make_cover
) -> None:
    """Removal deletes only the database row and preserves shared cover files."""
    cover = make_cover("shared.png")

    first_executable = tmp_path / "first" / "first.exe"
    first_executable.parent.mkdir()
    first_executable.write_bytes(b"MZ")
    first = manager.add_game(
        _windows_draft("First", first_executable, cover_path=str(cover))
    )
    prefix = Path(first.wine_prefix_path)
    prefix.mkdir(parents=True)
    (prefix / "system.reg").write_text("pretend prefix", encoding="utf-8")

    second_executable = tmp_path / "second" / "second.exe"
    second_executable.parent.mkdir()
    second_executable.write_bytes(b"MZ")
    second = manager.add_game(
        _windows_draft("Second", second_executable, cover_path=str(cover))
    )

    removed = manager.remove_game(first.id)

    assert removed == first
    assert manager.get_game(first.id) is None
    assert first_executable.exists()
    assert first_executable.parent.exists()
    assert prefix.exists()
    assert (prefix / "system.reg").exists()
    assert cover.exists()
    assert manager.get_game(second.id) is not None
    assert manager.get_game(second.id).cover_path == str(cover)


def test_missing_executable_is_rejected(manager: GameManager, tmp_path: Path) -> None:
    """A nonexistent executable fails validation and is not persisted."""
    missing = tmp_path / "does-not-exist"
    with pytest.raises(GameValidationError, match="does not exist"):
        manager.add_game(_linux_draft("Missing", missing))

    assert manager.list_games() == []


def test_invalid_working_directory_is_rejected(
    manager: GameManager, make_executable, tmp_path: Path
) -> None:
    """Missing paths and regular files are both invalid working directories."""
    executable = make_executable()

    with pytest.raises(GameValidationError, match="Working directory does not exist"):
        manager.add_game(
            _linux_draft(
                "Bad Directory",
                executable,
                working_directory=str(tmp_path / "missing-directory"),
            )
        )

    regular_file = tmp_path / "not-a-directory"
    regular_file.write_text("file", encoding="utf-8")
    with pytest.raises(GameValidationError, match="not a directory"):
        manager.add_game(
            _linux_draft(
                "Not Directory",
                executable,
                working_directory=str(regular_file),
            )
        )

    assert manager.list_games() == []


def test_native_executable_permission_is_explained_and_unchanged(
    manager: GameManager, make_executable
) -> None:
    """A non-executable Linux file is rejected without changing its mode."""
    executable = make_executable("not-executable", executable=False)
    mode_before = executable.stat().st_mode

    with pytest.raises(GameValidationError) as error:
        manager.add_game(_linux_draft("Not Executable", executable))

    message = str(error.value)
    assert "not executable" in message
    assert "chmod +x" in message
    assert "never changes file permissions" in message
    assert executable.stat().st_mode == mode_before
    assert manager.list_games() == []


def test_search_and_sorting(
    manager: GameManager, make_executable, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Name search is case-insensitive; both sort modes are deterministic."""
    timestamps = iter(
        [
            "2026-01-01T00:00:00.000001+00:00",
            "2026-01-02T00:00:00.000002+00:00",
            "2026-01-03T00:00:00.000003+00:00",
        ]
    )
    monkeypatch.setattr(game_manager_module, "_utc_now", lambda: next(timestamps))

    manager.add_game(_linux_draft("Zelda", make_executable("zelda")))
    manager.add_game(_linux_draft("Alpha", make_executable("alpha")))
    manager.add_game(_linux_draft("Minecraft", make_executable("minecraft")))

    assert [game.name for game in manager.list_games(sort_by=SORT_NAME)] == [
        "Alpha",
        "Minecraft",
        "Zelda",
    ]
    assert [game.name for game in manager.list_games(sort_by=SORT_RECENT)] == [
        "Minecraft",
        "Alpha",
        "Zelda",
    ]
    assert [game.name for game in manager.list_games("ALP")] == ["Alpha"]
    assert [game.name for game in manager.list_games("in", SORT_NAME)] == [
        "Minecraft"
    ]
    assert manager.list_games("no-such-game") == []


def test_performance_toggles_default_and_persist(
    manager: GameManager, make_executable
) -> None:
    """New games use the Phase 4 defaults and both toggles are independently editable."""
    executable = make_executable("performance-game")
    game = manager.add_game(_linux_draft("Performance", executable))

    assert game.gamemode_enabled is True
    assert game.mangohud_enabled is False

    updated = manager.update_game(
        game.id,
        _linux_draft(
            "Performance",
            executable,
            gamemode_enabled=False,
            mangohud_enabled=True,
        ),
    )

    assert updated.gamemode_enabled is False
    assert updated.mangohud_enabled is True
    assert manager.get_game(game.id) == updated


def test_database_migrates_phase3_rows_with_defaults(tmp_path: Path) -> None:
    """Opening a Phase 3 database adds the toggles without losing the row."""
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE games (
            id                TEXT PRIMARY KEY NOT NULL,
            name              TEXT NOT NULL,
            platform          TEXT NOT NULL,
            executable        TEXT NOT NULL,
            working_directory TEXT NOT NULL,
            launch_arguments  TEXT NOT NULL DEFAULT '',
            cover_path        TEXT,
            wine_prefix_path  TEXT,
            created_at        TEXT NOT NULL,
            updated_at        TEXT NOT NULL
        );
        PRAGMA user_version = 1;
        INSERT INTO games (
            id, name, platform, executable, working_directory,
            launch_arguments, cover_path, wine_prefix_path,
            created_at, updated_at
        ) VALUES (
            'legacy-id', 'Legacy Game', 'windows', '/games/legacy.exe',
            '/games', '', NULL, '/prefixes/legacy-id',
            '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'
        );
        """
    )
    connection.commit()
    connection.close()

    database = Database(path)
    try:
        game = database.get_game("legacy-id")
        assert game is not None
        assert game.gamemode_enabled is True
        assert game.mangohud_enabled is False
        version = database._connection.execute("PRAGMA user_version").fetchone()[0]
        assert version == 2
    finally:
        database.close()
