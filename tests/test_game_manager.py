"""Tests for adding, editing, removing, validating, searching, and sorting."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

import minimal_game_launcher.services.game_manager as game_manager_module
from minimal_game_launcher.database import Database, SCHEMA_VERSION
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
        assert game.environment == {}
        # Step 3 metadata defaults to conservative, non-owned values.
        assert game.install_directory is None
        assert game.install_directory_owned is False
        assert game.installer_source_directory is None
        assert game.installer_source_owned is False
        assert game.wine_prefix_path == "/prefixes/legacy-id"
        version = database._connection.execute("PRAGMA user_version").fetchone()[0]
        assert version == SCHEMA_VERSION
    finally:
        database.close()


def test_database_migrates_v2_rows_and_keeps_environment_empty(
    tmp_path: Path,
) -> None:
    """A Phase 4 database gains the environment column without losing a row."""
    path = tmp_path / "phase4.db"
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
            gamemode_enabled  INTEGER NOT NULL DEFAULT 1,
            mangohud_enabled  INTEGER NOT NULL DEFAULT 0,
            created_at        TEXT NOT NULL,
            updated_at        TEXT NOT NULL
        );
        PRAGMA user_version = 2;
        INSERT INTO games (
            id, name, platform, executable, working_directory,
            launch_arguments, cover_path, wine_prefix_path,
            gamemode_enabled, mangohud_enabled, created_at, updated_at
        ) VALUES (
            'phase4-id', 'Phase 4 Game', 'linux', '/games/phase4',
            '/games', '--fullscreen', NULL, NULL,
            0, 1, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'
        );
        """
    )
    connection.commit()
    connection.close()

    database = Database(path)
    try:
        game = database.get_game("phase4-id")
        assert game is not None
        assert game.name == "Phase 4 Game"
        assert game.gamemode_enabled is False
        assert game.mangohud_enabled is True
        assert game.launch_arguments == "--fullscreen"
        assert game.environment == {}
        assert game.install_directory is None
        assert game.install_directory_owned is False
        version = database._connection.execute("PRAGMA user_version").fetchone()[0]
        assert version == SCHEMA_VERSION
        # Reopening must be repeatable and must not alter the row.
        database.close()
        database = Database(path)
        assert database.get_game("phase4-id") == game
    finally:
        database.close()


def test_database_migrates_v3_rows_and_preserves_environment_and_prefix(
    tmp_path: Path,
) -> None:
    """Step 2 data keeps its environment and prefix and gains safe defaults."""
    path = tmp_path / "step2.db"
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
            gamemode_enabled  INTEGER NOT NULL DEFAULT 1,
            mangohud_enabled  INTEGER NOT NULL DEFAULT 0,
            environment       TEXT NOT NULL DEFAULT '{}',
            created_at        TEXT NOT NULL,
            updated_at        TEXT NOT NULL
        );
        PRAGMA user_version = 3;
        INSERT INTO games (
            id, name, platform, executable, working_directory,
            launch_arguments, cover_path, wine_prefix_path,
            gamemode_enabled, mangohud_enabled, environment,
            created_at, updated_at
        ) VALUES (
            'step2-id', 'Step 2 Game', 'windows', '/games/step2.exe',
            '/games', '--safe', NULL, '/data/prefixes/step2-id',
            1, 0, '{"LANG": "C"}',
            '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'
        );
        """
    )
    connection.commit()
    connection.close()

    database = Database(path)
    try:
        game = database.get_game("step2-id")
        assert game is not None
        assert game.environment == {"LANG": "C"}
        assert game.wine_prefix_path == "/data/prefixes/step2-id"
        assert game.install_directory is None
        assert game.install_directory_owned is False
        assert game.installer_source_directory is None
        assert game.installer_source_owned is False
        version = database._connection.execute("PRAGMA user_version").fetchone()[0]
        assert version == SCHEMA_VERSION

        database.close()
        database = Database(path)
        assert database.get_game("step2-id") == game
    finally:
        database.close()


def test_editing_a_game_preserves_ownership_metadata(
    manager: GameManager, make_executable
) -> None:
    """The editor does not own these fields, so an edit must not clear them."""
    executable = make_executable("owned-game.exe")
    game = manager.add_game(_windows_draft("Owned", executable))
    owned = replace(
        game,
        install_directory=f"/data/games/{game.id}",
        install_directory_owned=True,
        installer_source_directory=f"/data/installers/{game.id}",
        installer_source_owned=True,
    )
    assert manager._database.update_game(owned)
    prefix_before = owned.wine_prefix_path

    updated = manager.update_game(
        game.id, _windows_draft("Renamed", executable)
    )

    assert updated.name == "Renamed"
    assert updated.install_directory == f"/data/games/{game.id}"
    assert updated.install_directory_owned is True
    assert updated.installer_source_directory == f"/data/installers/{game.id}"
    assert updated.installer_source_owned is True
    assert updated.wine_prefix_path == prefix_before
    assert manager.get_game(game.id) == updated


def test_environment_variables_persist_and_edit(
    manager: GameManager, make_executable
) -> None:
    """Environment lines are parsed, persisted, and restored on edit."""
    executable = make_executable("environment-game")
    game = manager.add_game(
        _linux_draft(
            "Environment",
            executable,
            environment="A=1\nEMPTY=\nSAFE=two words",
        )
    )

    assert game.environment == {"A": "1", "EMPTY": "", "SAFE": "two words"}
    assert manager.get_game(game.id).environment == game.environment

    updated = manager.update_game(
        game.id,
        _linux_draft(
            "Environment",
            executable,
            environment="ONLY=updated",
        ),
    )
    assert updated.environment == {"ONLY": "updated"}
    assert manager.get_game(game.id).environment == {"ONLY": "updated"}


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("NO_EQUALS", "KEY=VALUE"),
        ("=value", "invalid variable name"),
        ("1BAD=value", "invalid variable name"),
        ("BAD-NAME=value", "invalid variable name"),
        ("WINEPREFIX=/tmp/elsewhere", "managed by the launcher"),
        ("MANGOHUD=1", "managed by the launcher"),
        ("A=1\nA=2", "defined more than once"),
        ("A=bad\x00value", "NUL"),
    ],
)
def test_environment_validation_rejects_invalid_input(
    manager: GameManager,
    make_executable,
    text: str,
    message: str,
) -> None:
    """Malformed or reserved variables never reach the database."""
    with pytest.raises(GameValidationError, match=message):
        manager.add_game(
            _linux_draft(
                "Invalid Environment",
                make_executable("invalid-environment"),
                environment=text,
            )
        )
    assert manager.list_games() == []


def test_bare_command_is_validated_but_not_rewritten(
    manager: GameManager,
    make_executable,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PATH command is accepted and the saved value stays as typed."""
    located = make_executable("fake-command")
    monkeypatch.setattr(
        manager,
        "_which",
        lambda name: str(located) if name == "fake-command" else None,
    )

    game = manager.add_game(
        _linux_draft("Command", "fake-command", launch_arguments="run app")
    )

    assert game.executable == "fake-command"
    assert game.working_directory == ""
    assert manager.get_game(game.id).executable == "fake-command"


def test_unresolvable_command_is_rejected(
    manager: GameManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A command missing from PATH produces a useful validation error."""
    monkeypatch.setattr(manager, "_which", lambda _name: None)

    with pytest.raises(GameValidationError, match="not found in PATH"):
        manager.add_game(
            _linux_draft("Missing Command", "definitely-not-installed")
        )
    assert manager.list_games() == []


def test_gamescope_resolution_persists_and_is_canonicalized(
    manager: GameManager, tmp_path: Path
) -> None:
    """A fixed upscale resolution is stored lower-case and survives a reload."""
    executable = tmp_path / "upscaled.exe"
    executable.write_bytes(b"MZ")

    game = manager.add_game(
        _windows_draft(
            "Upscaled", executable, gamescope_resolution=" 800X600 "
        )
    )

    assert game.gamescope_resolution == "800x600"
    assert manager.get_game(game.id).gamescope_resolution == "800x600"

    updated = manager.update_game(
        game.id, _windows_draft("Upscaled", executable)
    )
    assert updated.gamescope_resolution == ""


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("800", "must look like 800x600"),
        ("800*600", "must look like 800x600"),
        ("0x600", "between 1x1"),
        ("8000x9000", "between 1x1"),
    ],
)
def test_gamescope_resolution_validation_rejects_invalid_input(
    manager: GameManager,
    tmp_path: Path,
    text: str,
    message: str,
) -> None:
    """Malformed resolutions never reach the database."""
    executable = tmp_path / "invalid.exe"
    executable.write_bytes(b"MZ")

    with pytest.raises(GameValidationError, match=message):
        manager.add_game(
            _windows_draft(
                "Invalid", executable, gamescope_resolution=text
            )
        )
    assert manager.list_games() == []
