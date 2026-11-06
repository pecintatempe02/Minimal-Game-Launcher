"""Tests for read-only post-installation executable discovery and registration."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from minimal_game_launcher.database import Database
from minimal_game_launcher.models import (
    GameDraft,
    InstallerStatus,
    Platform,
)
from minimal_game_launcher.services.cleanup import CleanupService
from minimal_game_launcher.services.executable_discovery import (
    ExecutableDiscovery,
)
from minimal_game_launcher.services.game_manager import GameManager
from minimal_game_launcher.services.installer_manager import InstallerManager
from minimal_game_launcher.services.ownership import (
    OwnershipService,
    ResourceKind,
)


class _FakeMount:
    """Validate an ISO without mounting or invoking UDisks."""

    def validate_iso(self, iso_path: str | Path) -> Path:
        path = Path(iso_path)
        if not path.is_file():
            raise FileNotFoundError(path)
        return path.resolve()


def _manager(
    tmp_path: Path, database: Database
) -> tuple[InstallerManager, OwnershipService]:
    ownership = OwnershipService(
        tmp_path / "prefixes", tmp_path / "games", tmp_path / "installers"
    )
    return InstallerManager(database, ownership, _FakeMount()), ownership


def _write(path: Path, payload: bytes = b"MZ") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _discovery() -> ExecutableDiscovery:
    return ExecutableDiscovery()


def test_custom_installation_directory_is_searched_first(
    tmp_path: Path,
) -> None:
    install = tmp_path / "custom install"
    executable = _write(install / "Test Game.exe")

    candidates = _discovery().discover(
        "Test Game", installation_directory=install
    )

    assert [candidate.path for candidate in candidates] == [executable]
    assert candidates[0].relative_path == "Test Game.exe"
    assert candidates[0].location == "installation folder"
    assert candidates[0].recommended is True


def test_default_wine_program_files_is_discovered(tmp_path: Path) -> None:
    prefix = tmp_path / "prefix"
    executable = _write(
        prefix / "drive_c" / "Program Files" / "Test Game" / "Test Game.exe"
    )

    candidates = _discovery().discover(
        "Test Game", installation_directory=None, prefix_path=prefix
    )

    assert [candidate.path for candidate in candidates] == [executable]
    assert candidates[0].relative_path == (
        "drive_c/Program Files/Test Game/Test Game.exe"
    )
    assert candidates[0].location == "Program Files"


def test_program_files_x86_is_discovered(tmp_path: Path) -> None:
    prefix = tmp_path / "prefix"
    executable = _write(
        prefix
        / "drive_c"
        / "Program Files (x86)"
        / "Test Game"
        / "Test Game.exe"
    )

    candidates = _discovery().discover(
        "Test Game", prefix_path=prefix
    )

    assert [candidate.path for candidate in candidates] == [executable]
    assert candidates[0].location == "Program Files (x86)"


def test_other_drive_c_folder_is_a_fallback(tmp_path: Path) -> None:
    prefix = tmp_path / "prefix"
    executable = _write(
        prefix / "drive_c" / "Games" / "Test Game" / "Test Game.exe"
    )

    candidates = _discovery().discover("Test Game", prefix_path=prefix)

    assert [candidate.path for candidate in candidates] == [executable]
    assert candidates[0].location == "Wine drive_c"


def test_multiple_candidates_are_ranked_deterministically(
    tmp_path: Path,
) -> None:
    install = tmp_path / "install"
    preferred = _write(install / "Test Game.exe")
    _write(install / "Bonus.exe")
    _write(install / "extras" / "Other.exe")

    discovery = _discovery()
    first = discovery.discover("Test Game", installation_directory=install)
    second = discovery.discover("Test Game", installation_directory=install)

    assert [candidate.path for candidate in first] == [
        candidate.path for candidate in second
    ]
    assert first[0].path == preferred
    assert first[0].recommended is True
    assert first[0].score > first[1].score
    assert all(not candidate.recommended for candidate in first[1:])


def test_helpers_installers_and_redistributables_are_filtered(
    tmp_path: Path,
) -> None:
    install = tmp_path / "install"
    game = _write(install / "Test Game.exe")
    for name in (
        "unins000.exe",
        "setup.exe",
        "vcredist_x64.exe",
        "crashpad_handler.exe",
        "updater.exe",
        "UnityCrashHandler64.exe",
        "helper.exe",
        "config.exe",
    ):
        _write(install / name)

    candidates = _discovery().discover(
        "Test Game", installation_directory=install
    )

    assert [candidate.path for candidate in candidates] == [game]


def test_no_candidates_returns_an_empty_list(tmp_path: Path) -> None:
    install = tmp_path / "install"
    install.mkdir()
    _write(install / "readme.txt")

    assert _discovery().discover(
        "Test Game", installation_directory=install
    ) == []


def test_missing_and_inaccessible_roots_are_safe(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    assert _discovery().discover(
        "Test Game", installation_directory=missing, prefix_path=missing
    ) == []

    locked = tmp_path / "locked"
    _write(locked / "Test Game.exe")
    locked.chmod(0o000)
    try:
        assert _discovery().discover(
            "Test Game", installation_directory=locked
        ) == []
    finally:
        locked.chmod(0o700)


def test_symlinks_are_never_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside_executable = _write(outside / "Test Game.exe")
    install = tmp_path / "install"
    install.mkdir()
    (install / "linked.exe").symlink_to(outside_executable)
    (install / "linked-folder").symlink_to(outside, target_is_directory=True)

    candidates = _discovery().discover(
        "Test Game", installation_directory=install
    )

    assert candidates == []


def test_traversal_depth_is_limited(tmp_path: Path) -> None:
    install = tmp_path / "install"
    shallow = _write(install / "Test Game.exe")
    _write(install / "a" / "b" / "c" / "Test Game.exe")

    candidates = ExecutableDiscovery(max_depth=1).discover(
        "Test Game", installation_directory=install
    )

    assert [candidate.path for candidate in candidates] == [shallow]


def test_discovery_limit_stops_traversal(tmp_path: Path) -> None:
    install = tmp_path / "install"
    for index in range(10):
        _write(install / f"folder-{index}" / f"Game-{index}.exe")

    candidates = ExecutableDiscovery(max_files=3).discover(
        "Test Game", installation_directory=install
    )

    assert len(candidates) <= 1


def test_wine_default_destination_creates_no_game_directory(
    tmp_path: Path, database: Database
) -> None:
    manager, ownership = _manager(tmp_path, database)
    iso = tmp_path / "disc.iso"
    iso.write_bytes(b"iso")
    session = manager.create_session(iso, "Test Game")

    destination = manager.resolve_destination(
        session, managed=False, create=True, wine_default=True
    )

    assert destination.path is None
    assert destination.owned is False
    assert not (tmp_path / "games").exists()


def test_existing_custom_destination_is_not_silently_redirected(
    tmp_path: Path, database: Database
) -> None:
    manager, ownership = _manager(tmp_path, database)
    iso = tmp_path / "disc.iso"
    iso.write_bytes(b"iso")
    session = manager.create_session(iso, "Test Game")
    external = tmp_path / "external"
    external.mkdir()

    destination = manager.resolve_destination(
        session,
        managed=False,
        external_path=external,
        create=True,
    )

    assert destination.path == external
    assert destination.owned is False
    assert external.is_dir()
    assert not (tmp_path / "games").exists()


def test_registration_persists_executable_and_working_directory(
    tmp_path: Path, database: Database
) -> None:
    manager, ownership = _manager(tmp_path, database)
    install = tmp_path / "custom"
    executable = _write(install / "Test Game.exe")
    iso = tmp_path / "disc.iso"
    iso.write_bytes(b"iso")
    session = manager.save_session(
        replace(
            manager.create_session(iso, "Test Game"),
            install_directory=str(install),
            install_directory_owned=False,
            status=InstallerStatus.INSTALLER_FINISHED,
        )
    )
    candidate = manager.discover_game_executables(session)[0]

    games = GameManager(database, tmp_path / "prefixes")
    draft = manager.installation_draft(session, candidate.path)
    game = games.register_installation(
        session.game_id,
        draft,
        install_directory=session.install_directory,
        install_directory_owned=session.install_directory_owned,
        installer_source_directory=session.staging_directory,
        installer_source_owned=True,
    )

    assert game.executable == str(executable)
    assert game.working_directory == str(install)
    assert database.get_game(session.game_id) == game


def test_canceling_registration_leaves_launch_configuration_unchanged(
    tmp_path: Path, database: Database
) -> None:
    manager, ownership = _manager(tmp_path, database)
    prefix = tmp_path / "prefixes" / "game-id"
    _write(prefix / "drive_c" / "Program Files" / "Test Game" / "Test Game.exe")
    iso = tmp_path / "disc.iso"
    iso.write_bytes(b"iso")
    session = manager.save_session(
        replace(
            manager.create_session(iso, "Test Game", game_id="game-id"),
            status=InstallerStatus.INSTALLER_FINISHED,
            message="Installer exited with code 0.",
        )
    )

    manager.discover_game_executables(session)

    assert database.list_games() == []
    stored = database.get_installer_session("game-id")
    assert stored is not None
    assert stored.status is InstallerStatus.INSTALLER_FINISHED
    assert stored.message == "Installer exited with code 0."


def test_managed_destination_ownership_and_cleanup(
    tmp_path: Path, database: Database
) -> None:
    manager, ownership = _manager(tmp_path, database)
    iso = tmp_path / "disc.iso"
    iso.write_bytes(b"iso")
    session = manager.create_session(iso, "Test Game")
    destination = manager.resolve_destination(
        session, managed=True, create=True
    )
    assert destination.path is not None
    executable = _write(destination.path / "Test Game.exe")
    session = manager.save_session(
        replace(
            session,
            install_directory=str(destination.path),
            install_directory_owned=True,
            status=InstallerStatus.INSTALLED,
        )
    )
    games = GameManager(database, tmp_path / "prefixes")
    draft = manager.installation_draft(session, executable)
    game = games.register_installation(
        session.game_id,
        draft,
        install_directory=session.install_directory,
        install_directory_owned=True,
        installer_source_directory=session.staging_directory,
        installer_source_owned=True,
    )

    cleanup = CleanupService(database, ownership)
    target = next(
        item
        for item in cleanup.targets_for_game(game)
        if item.kind is ResourceKind.INSTALLATION
    )
    assert target.can_delete is True
    outcome = cleanup.delete_target(target, authorized=True)
    assert outcome.deleted is True
    assert not destination.path.exists()


def test_external_destination_is_preserved_and_never_owned(
    tmp_path: Path, database: Database
) -> None:
    manager, ownership = _manager(tmp_path, database)
    iso = tmp_path / "disc.iso"
    iso.write_bytes(b"iso")
    session = manager.create_session(iso, "Test Game")
    external = tmp_path / "external"
    destination = manager.resolve_destination(
        session,
        managed=False,
        external_path=external,
        create=True,
    )
    assert destination.owned is False
    executable = _write(external / "Test Game.exe")
    session = manager.save_session(
        replace(
            session,
            install_directory=str(external),
            install_directory_owned=False,
            status=InstallerStatus.INSTALLED,
        )
    )
    games = GameManager(database, tmp_path / "prefixes")
    draft = manager.installation_draft(session, executable)
    game = games.register_installation(
        session.game_id,
        draft,
        install_directory=session.install_directory,
        install_directory_owned=False,
        installer_source_directory=session.staging_directory,
        installer_source_owned=True,
    )

    cleanup = CleanupService(database, ownership)
    target = next(
        item
        for item in cleanup.targets_for_game(game)
        if item.kind is ResourceKind.INSTALLATION
    )
    assert target.can_delete is False
    assert cleanup.delete_target(target, authorized=True).skipped_reason
    assert executable.exists()


def test_existing_launch_configuration_is_preserved_on_registration(
    tmp_path: Path, database: Database
) -> None:
    manager, ownership = _manager(tmp_path, database)
    games = GameManager(database, tmp_path / "prefixes")
    old_executable = _write(tmp_path / "old" / "Old.exe")
    existing = games.add_game(
        GameDraft(
            name="Existing Game",
            platform=Platform.WINDOWS,
            executable=str(old_executable),
            launch_arguments="--windowed",
            environment="FOO=bar\nBAZ=qux",
            gamemode_enabled=False,
            mangohud_enabled=True,
        ),
        game_id="existing-id",
    )
    prefix = existing.wine_prefix_path

    iso = tmp_path / "disc.iso"
    iso.write_bytes(b"iso")
    session = manager.save_session(
        replace(
            manager.create_session(iso, "Test Game", game_id="existing-id"),
            status=InstallerStatus.INSTALLER_FINISHED,
        )
    )
    new_executable = _write(tmp_path / "new" / "New.exe")
    draft = manager.installation_draft(session, new_executable, existing)
    game = games.register_installation(
        "existing-id",
        draft,
        install_directory=None,
        install_directory_owned=False,
        installer_source_directory=session.staging_directory,
        installer_source_owned=True,
    )

    assert game.name == "Existing Game"
    assert game.executable == str(new_executable)
    assert game.working_directory == str(new_executable.parent)
    assert game.launch_arguments == "--windowed"
    assert game.environment == {"FOO": "bar", "BAZ": "qux"}
    assert game.gamemode_enabled is False
    assert game.mangohud_enabled is True
    assert game.wine_prefix_path == prefix


@pytest.mark.parametrize(
    "name",
    ["Patchwork.exe", "Crashlands.exe", "Updated Quest.exe", "Setup Quest.exe"],
)
def test_game_names_containing_helper_substrings_stay_visible(
    tmp_path: Path, name: str
) -> None:
    install = tmp_path / "install"
    executable = _write(install / name)

    candidates = _discovery().discover(
        "Test Game", installation_directory=install
    )

    assert [candidate.path for candidate in candidates] == [executable]


def test_helper_word_names_rank_below_the_game_but_stay_visible(
    tmp_path: Path,
) -> None:
    install = tmp_path / "install"
    game = _write(install / "Test Game.exe")
    demoted = _write(install / "Setup Quest.exe")
    _write(install / "Patchwork.exe")

    candidates = _discovery().discover(
        "Test Game", installation_directory=install
    )
    by_path = {candidate.path: candidate for candidate in candidates}

    assert set(by_path) == {game, demoted, install / "Patchwork.exe"}
    assert candidates[0].path == game
    assert candidates[0].recommended is True
    assert by_path[demoted].score < by_path[game].score
    assert by_path[demoted].recommended is False
    assert "ranked lower" in by_path[demoted].reason
    assert "ranked lower" not in by_path[install / "Patchwork.exe"].reason


def test_helper_word_in_game_title_is_not_demoted(tmp_path: Path) -> None:
    install = tmp_path / "install"
    setup_game = _write(install / "Setup Quest.exe")
    other = _write(install / "Other.exe")

    candidates = _discovery().discover(
        "Setup Quest", installation_directory=install
    )

    assert candidates[0].path == setup_game
    assert "ranked lower" not in candidates[0].reason
    assert other in [candidate.path for candidate in candidates]


@pytest.mark.parametrize(
    "helper",
    [
        "Setup.exe",
        "setup_x64.exe",
        "unins000.exe",
        "vcredist_x64.exe",
        "dxsetup.exe",
        "UnityCrashHandler64.exe",
        "crashpad_handler.exe",
        "updater.exe",
        "helper.exe",
    ],
)
def test_clear_helpers_stay_hidden_in_ranked_results(
    tmp_path: Path, helper: str
) -> None:
    install = tmp_path / "install"
    game = _write(install / "Test Game.exe")
    _write(install / helper)

    candidates = _discovery().discover(
        "Test Game", installation_directory=install
    )

    assert [candidate.path for candidate in candidates] == [game]


@pytest.mark.parametrize("name", ["Test Game.exe", "TestGame.EXE"])
def test_extension_matching_is_case_insensitive(tmp_path: Path, name: str) -> None:
    install = tmp_path / "install"
    executable = _write(install / name)

    candidates = _discovery().discover(
        "Test Game", installation_directory=install
    )

    assert [candidate.path for candidate in candidates] == [executable]


def test_dummy_install_tree_filters_helpers_and_is_deterministic(
    tmp_path: Path,
) -> None:
    install = tmp_path / "test-game"
    sentinel = tmp_path / "executed"
    dummy = {
        "Patchwork.exe": install / "Patchwork.exe",
        "Crashlands.exe": install / "Crashlands.exe",
        "Setup.exe": install / "Setup.exe",
        "unins000.exe": install / "unins000.exe",
        "Updated Quest.exe": install / "Updated Quest.exe",
        "Redist/vcredist_x64.exe": install / "Redist" / "vcredist_x64.exe",
    }
    for relative, path in dummy.items():
        _write(path)
    # An executable script would create the sentinel if discovery ever ran it.
    dummy["Crashlands.exe"].write_text(
        f"#!/bin/sh\ntouch {sentinel}\n", encoding="utf-8"
    )
    dummy["Crashlands.exe"].chmod(0o755)

    runs = [
        _discovery().discover("Updated Quest", installation_directory=install)
        for _ in range(3)
    ]

    expected = [
        dummy["Updated Quest.exe"],
        dummy["Crashlands.exe"],
        dummy["Patchwork.exe"],
    ]
    assert [candidate.path for candidate in runs[0]] == expected
    assert all(
        [
            (candidate.path, candidate.score, candidate.recommended)
            for candidate in run
        ]
        == [
            (candidate.path, candidate.score, candidate.recommended)
            for candidate in runs[0]
        ]
        for run in runs[1:]
    )
    assert [candidate.recommended for candidate in runs[0]] == [True, False, False]
    visible = {candidate.path for candidate in runs[0]}
    assert dummy["Setup.exe"] not in visible
    assert dummy["unins000.exe"] not in visible
    assert dummy["Redist/vcredist_x64.exe"] not in visible
    assert not sentinel.exists()


def test_candidate_limit_is_respected_deterministically(tmp_path: Path) -> None:
    install = tmp_path / "install"
    for index in range(6):
        _write(install / f"Game {index}.exe")

    candidates = ExecutableDiscovery(max_candidates=2).discover(
        "Test Game", installation_directory=install
    )

    assert [candidate.path for candidate in candidates] == [
        install / "Game 0.exe",
        install / "Game 1.exe",
    ]


# A realistic Wine prefix: built-in demo applications and accessories sit next
# to an installed game under Program Files (x86), plus unrelated games that must
# remain discoverable.
_WINE_PREFIX_FILES = (
    "Program Files/Internet Explorer/iexplore.exe",
    "Program Files/Windows Media Player/wmplayer.exe",
    "Program Files/Windows NT/Accessories/wordpad.exe",
    "Program Files (x86)/PopCap Games/Plants vs. Zombies/PlantsVsZombies.exe",
    "Program Files (x86)/PopCap Games/Plants vs. Zombies/popcapgame1.exe",
    "Program Files/Other Studio/Other Game.exe",
    "Program Files (x86)/Another Studio/Another Game.exe",
)


def _wine_prefix_files(tmp_path: Path) -> tuple[Path, dict[str, Path]]:
    prefix = tmp_path / "prefix"
    paths = {
        relative: _write(prefix / "drive_c" / relative)
        for relative in _WINE_PREFIX_FILES
    }
    return prefix, paths


def test_wine_builtins_do_not_outrank_the_installed_game(
    tmp_path: Path,
) -> None:
    prefix, paths = _wine_prefix_files(tmp_path)
    candidates = _discovery().discover(
        "Plants vs. Zombies", prefix_path=prefix
    )

    by_path = {candidate.path: candidate for candidate in candidates}
    game = paths[
        "Program Files (x86)/PopCap Games/Plants vs. Zombies/PlantsVsZombies.exe"
    ]

    assert game in by_path
    assert by_path[game].recommended is True
    assert by_path[game].score > max(
        candidate.score
        for path, candidate in by_path.items()
        if path != game
    )
    for builtin in (
        "Program Files/Internet Explorer/iexplore.exe",
        "Program Files/Windows Media Player/wmplayer.exe",
        "Program Files/Windows NT/Accessories/wordpad.exe",
    ):
        assert paths[builtin] not in by_path
    assert "matches the game name exactly" in by_path[game].reason


def test_popcapgame1_remains_visible_as_an_alternative(tmp_path: Path) -> None:
    prefix, paths = _wine_prefix_files(tmp_path)
    candidates = _discovery().discover(
        "Plants vs. Zombies", prefix_path=prefix
    )

    by_path = {candidate.path: candidate for candidate in candidates}
    game = paths[
        "Program Files (x86)/PopCap Games/Plants vs. Zombies/PlantsVsZombies.exe"
    ]
    alternative = paths[
        "Program Files (x86)/PopCap Games/Plants vs. Zombies/popcapgame1.exe"
    ]

    assert alternative in by_path
    assert by_path[alternative].recommended is False
    assert by_path[alternative].score < by_path[game].score


def test_other_games_under_program_files_remain_discoverable(
    tmp_path: Path,
) -> None:
    prefix, paths = _wine_prefix_files(tmp_path)
    candidates = _discovery().discover(
        "Plants vs. Zombies", prefix_path=prefix
    )

    by_path = {candidate.path: candidate for candidate in candidates}
    assert paths["Program Files/Other Studio/Other Game.exe"] in by_path
    assert paths[
        "Program Files (x86)/Another Studio/Another Game.exe"
    ] in by_path


def test_wine_prefix_helper_filtering_and_demotion_are_preserved(
    tmp_path: Path,
) -> None:
    prefix, paths = _wine_prefix_files(tmp_path)
    game_folder = (
        prefix
        / "drive_c"
        / "Program Files (x86)"
        / "PopCap Games"
        / "Plants vs. Zombies"
    )
    setup = _write(game_folder / "Setup.exe")
    launcher = _write(game_folder / "Game Launcher.exe")

    candidates = _discovery().discover(
        "Plants vs. Zombies", prefix_path=prefix
    )

    by_path = {candidate.path: candidate for candidate in candidates}
    assert setup not in by_path
    assert launcher in by_path
    assert by_path[launcher].recommended is False
    assert "ranked lower" in by_path[launcher].reason


def test_builtin_name_outside_a_system_folder_is_demoted_not_hidden(
    tmp_path: Path,
) -> None:
    install = tmp_path / "games"
    game = _write(install / "My Game.exe")
    builtin = _write(install / "iexplore.exe")

    candidates = _discovery().discover(
        "My Game", installation_directory=install
    )

    by_path = {candidate.path: candidate for candidate in candidates}
    assert builtin in by_path
    assert by_path[builtin].score < by_path[game].score
    assert by_path[builtin].recommended is False
    assert "built-in" in by_path[builtin].reason


def test_wine_prefix_discovery_is_deterministic(tmp_path: Path) -> None:
    prefix, paths = _wine_prefix_files(tmp_path)
    discovery = _discovery()
    runs = [
        discovery.discover("Plants vs. Zombies", prefix_path=prefix)
        for _ in range(3)
    ]

    snapshots = [
        [
            (candidate.path, candidate.score, candidate.recommended)
            for candidate in run
        ]
        for run in runs
    ]
    assert all(snapshot == snapshots[0] for snapshot in snapshots[1:])
    assert snapshots[0][0][0] == paths[
        "Program Files (x86)/PopCap Games/Plants vs. Zombies/PlantsVsZombies.exe"
    ]
    assert [candidate.recommended for candidate in runs[0]].count(True) == 1

