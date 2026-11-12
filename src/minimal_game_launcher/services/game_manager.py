"""Game-library business logic and launch-field validation."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

from minimal_game_launcher.database import Database
from minimal_game_launcher.errors import GameNotFoundError, GameValidationError
from minimal_game_launcher.models import (
    DisplayMode,
    Game,
    GameDraft,
    Platform,
    is_bare_command,
)
from minimal_game_launcher.services.prefixes import (
    assign_prefix,
    load_strategy,
    shared_reference_guard,
)
from minimal_game_launcher.settings import SettingsStore, WinePrefixStrategy

SORT_NAME = "name"
SORT_RECENT = "recent"
VALID_SORTS = frozenset({SORT_NAME, SORT_RECENT})

_WhichFactory = Callable[[str], str | None]
_ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
# The launcher owns these values during launch. Allowing a stored override
# would let a per-game environment change Wine isolation or MangoHud state.
_RESERVED_ENVIRONMENT = frozenset({"WINEPREFIX", "MANGOHUD"})
_GAMESCOPE_RESOLUTION = re.compile(r"([0-9]{1,4})x([0-9]{1,4})\Z")
_GAMESCOPE_LIMIT = 7680
_FPS_LIMIT_MAX = 1000
_VSYNC_VALUES = frozenset({"", "on", "off"})
_FILTER_VALUES = frozenset({"", "fsr", "nis", "linear", "nearest"})


def _utc_now() -> str:
    """Return a sortable UTC timestamp for database records."""
    return datetime.now(UTC).isoformat(timespec="microseconds")


class GameManager:
    """Validate, normalize, persist, search, and remove library entries.

    The manager assigns a dedicated Wine prefix path to Windows games but does
    not create it. Prefix creation belongs to the future launch phase, so a
    Windows game may be registered before Wine is installed or initialized.
    """

    def __init__(
        self,
        database: Database,
        prefixes_root: str | Path,
        *,
        which: _WhichFactory | None = None,
        settings_store: SettingsStore | None = None,
    ) -> None:
        self._database = database
        self._prefixes_root = Path(prefixes_root).expanduser()
        self._which = which or shutil.which
        self._settings_store = settings_store

    @staticmethod
    def split_launch_arguments(arguments: str) -> list[str]:
        """Parse the optional user argument string without invoking a shell.

        The raw text is retained in the database for future launch execution;
        parsing here is only a validation step and catches unmatched quotes.
        """
        try:
            return shlex.split(arguments, posix=True)
        except ValueError as error:
            raise GameValidationError(
                "Launch arguments contain invalid quoting. "
                "Use quotes to group an argument that contains spaces.",
                field="launch_arguments",
            ) from error

    @staticmethod
    def parse_gamescope_resolution(text: str) -> str:
        """Validate an optional ``WIDTHxHEIGHT`` gamescope render resolution.

        An empty value disables gamescope for the game. The value is stored in
        its canonical lowercase form so the launch wrapper never has to guess.
        """
        return GameManager._parse_resolution(
            text,
            field="gamescope_resolution",
            description="The upscale resolution",
        )

    @staticmethod
    def _parse_resolution(text: str, *, field: str, description: str) -> str:
        """Validate an optional ``WIDTHxHEIGHT`` value for one field."""
        value = text.strip().lower()
        if not value:
            return ""
        match = _GAMESCOPE_RESOLUTION.match(value)
        if match is None:
            raise GameValidationError(
                f"{description} must look like 800x600.",
                field=field,
            )
        width, height = (int(part) for part in match.groups())
        if not (1 <= width <= _GAMESCOPE_LIMIT) or not (
            1 <= height <= _GAMESCOPE_LIMIT
        ):
            raise GameValidationError(
                f"{description} must be between 1x1 and "
                f"{_GAMESCOPE_LIMIT}x{_GAMESCOPE_LIMIT}.",
                field=field,
            )
        return f"{width}x{height}"

    @staticmethod
    def parse_environment(text: str) -> dict[str, str]:
        """Parse ``KEY=VALUE`` lines without shell evaluation.

        Blank lines and ``#`` comments are ignored. Keys must be valid POSIX
        environment names, duplicates are rejected, and the launcher-managed
        keys are reserved. Values are not shell-evaluated; surrounding
        whitespace is trimmed.
        """
        parsed: dict[str, str] = {}
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if "=" not in line:
                raise GameValidationError(
                    f"Environment line {line_number} must use KEY=VALUE.",
                    field="environment",
                )
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not _ENV_KEY.fullmatch(key):
                raise GameValidationError(
                    f"Environment line {line_number} has an invalid variable "
                    "name. Use letters, digits, and underscores, and do not "
                    "start with a digit.",
                    field="environment",
                )
            if key in _RESERVED_ENVIRONMENT:
                raise GameValidationError(
                    f"The variable {key} is managed by the launcher and cannot "
                    "be set per game.",
                    field="environment",
                )
            if key in parsed:
                raise GameValidationError(
                    f"The variable {key} is defined more than once.",
                    field="environment",
                )
            if "\x00" in value:
                raise GameValidationError(
                    f"The value for {key} contains an invalid NUL character.",
                    field="environment",
                )
            parsed[key] = value
        return parsed

    @staticmethod
    def default_working_directory(executable: str) -> str:
        """Suggest the executable's parent directory for the form field."""
        if not executable.strip():
            return ""
        if is_bare_command(executable.strip()):
            return ""
        return str(Path(executable).expanduser().parent)

    def _normalize_draft(
        self, draft: GameDraft
    ) -> tuple[GameDraft, dict[str, str]]:
        """Validate all user-controlled paths and launch fields."""
        name = draft.name.strip()
        if not name:
            raise GameValidationError("Game name is required.", field="name")
        if len(name) > 200:
            raise GameValidationError(
                "Game name must be 200 characters or fewer.", field="name"
            )

        raw_executable = draft.executable.strip()
        if not raw_executable:
            raise GameValidationError("Game executable is required.", field="executable")

        command = draft.platform is Platform.LINUX and is_bare_command(
            raw_executable
        )
        if command:
            located = self._which(raw_executable)
            if not located:
                raise GameValidationError(
                    f"Command not found in PATH: {raw_executable}",
                    field="executable",
                )
            executable = Path(located).resolve(strict=False)
            # Keep the command name exactly as typed. Resolution happens at
            # launch time so a PATH change is honoured and the saved value is
            # never silently rewritten to a different command.
            stored_executable = raw_executable
        else:
            executable = Path(raw_executable).expanduser()
            if not executable.is_absolute():
                executable = executable.resolve()
            else:
                executable = executable.resolve(strict=False)
            stored_executable = str(executable)

        if not executable.exists():
            raise GameValidationError(
                f"Game executable does not exist: {executable}",
                field="executable",
            )
        if not executable.is_file():
            raise GameValidationError(
                f"Game executable is not a regular file: {executable}",
                field="executable",
            )

        if draft.platform is Platform.WINDOWS:
            if executable.suffix.casefold() != ".exe":
                raise GameValidationError(
                    "Windows games must point to a .exe executable.",
                    field="executable",
                )
        elif not os.access(executable, os.X_OK):
            # Never repair permissions silently. The user must explicitly grant
            # execute access in their file manager or with chmod.
            raise GameValidationError(
                f"The Linux executable is not executable: {executable}\n\n"
                "Grant execute permission (for example, chmod +x on the file) "
                "and try again. The launcher never changes file permissions "
                "automatically.",
                field="executable",
            )

        raw_working_directory = draft.working_directory.strip()
        working_directory = executable.parent
        if raw_working_directory:
            candidate = Path(raw_working_directory).expanduser()
            if not candidate.is_absolute():
                candidate = candidate.resolve()
            else:
                candidate = candidate.resolve(strict=False)
            if not candidate.exists():
                raise GameValidationError(
                    f"Working directory does not exist: {candidate}",
                    field="working_directory",
                )
            if not candidate.is_dir():
                raise GameValidationError(
                    f"Working directory is not a directory: {candidate}",
                    field="working_directory",
                )
            working_directory = candidate
        # A bare command has no meaningful parent folder. Leaving the field
        # empty keeps the launch-time default rather than guessing /usr/bin.
        stored_working_directory = (
            "" if command and not raw_working_directory
            else str(working_directory)
        )

        launch_arguments = draft.launch_arguments.strip()
        self.split_launch_arguments(launch_arguments)
        gamescope_resolution = self.parse_gamescope_resolution(
            draft.gamescope_resolution
        )
        gamescope_output_resolution = self._parse_resolution(
            draft.gamescope_output_resolution,
            field="gamescope_output_resolution",
            description="The gamescope output resolution",
        )
        gamescope_fps_limit = draft.gamescope_fps_limit
        if (
            not isinstance(gamescope_fps_limit, int)
            or isinstance(gamescope_fps_limit, bool)
            or not 0 <= gamescope_fps_limit <= _FPS_LIMIT_MAX
        ):
            raise GameValidationError(
                f"The gamescope FPS limit must be between 0 and "
                f"{_FPS_LIMIT_MAX}.",
                field="gamescope_fps_limit",
            )
        gamescope_vsync = draft.gamescope_vsync.strip().lower()
        if gamescope_vsync not in _VSYNC_VALUES:
            raise GameValidationError(
                "The gamescope vsync value must be empty, on, or off.",
                field="gamescope_vsync",
            )
        gamescope_filter = draft.gamescope_filter.strip().lower()
        if gamescope_filter not in _FILTER_VALUES:
            raise GameValidationError(
                "The gamescope filter must be empty, fsr, nis, linear, or "
                "nearest.",
                field="gamescope_filter",
            )
        try:
            display_mode = DisplayMode(draft.display_mode)
        except (TypeError, ValueError) as error:
            raise GameValidationError(
                "Choose a supported display mode.",
                field="display_mode",
            ) from error
        if display_mode is DisplayMode.VIRTUAL_DESKTOP:
            if draft.platform is not Platform.WINDOWS:
                raise GameValidationError(
                    "The Wine virtual desktop is available only for "
                    "Windows/Wine games.",
                    field="display_mode",
                )
            virtual_desktop_resolution = self._parse_resolution(
                draft.virtual_desktop_resolution,
                field="virtual_desktop_resolution",
                description="The virtual desktop resolution",
            )
            if not virtual_desktop_resolution:
                raise GameValidationError(
                    "Choose a virtual desktop resolution, for example "
                    "1280x720.",
                    field="virtual_desktop_resolution",
                )
        else:
            # A resolution left over from another mode must not resurface when
            # the user switches back later.
            virtual_desktop_resolution = ""
        environment = self.parse_environment(draft.environment)

        cover_path: str | None = None
        if draft.cover_path and draft.cover_path.strip():
            # Covers remain external, user-owned references. They are not
            # copied or deleted, and a missing cover must not make metadata
            # edits impossible.
            cover_path = str(
                Path(draft.cover_path).expanduser().resolve(strict=False)
            )

        return (
            replace(
                draft,
                name=name,
                executable=stored_executable,
                working_directory=stored_working_directory,
                launch_arguments=launch_arguments,
                environment="",
                cover_path=cover_path,
                gamescope_resolution=gamescope_resolution,
                gamescope_enabled=bool(draft.gamescope_enabled),
                gamescope_output_resolution=gamescope_output_resolution,
                gamescope_fps_limit=gamescope_fps_limit,
                gamescope_vsync=gamescope_vsync,
                gamescope_filter=gamescope_filter,
                gamescope_fullscreen=bool(draft.gamescope_fullscreen),
                display_mode=display_mode,
                virtual_desktop_resolution=virtual_desktop_resolution,
                show_in_library=bool(draft.show_in_library),
            ),
            environment,
        )

    def _lifecycle_guard(
        self, platform: Platform, existing: str | None, existing_shared: bool = False
    ):
        """Hold the shared-prefix lifecycle lock when a reference may be written.

        Only a Windows game with no stored prefix can be assigned a new shared
        reference. Existing paths are preserved as-is and need no lock.
        """
        may_assign_shared = (
            platform is Platform.WINDOWS
            and not existing
            and not existing_shared
            and load_strategy(self._settings_store) is WinePrefixStrategy.SHARED
        )
        return shared_reference_guard(self._prefixes_root, may_assign_shared)

    def _prefix_for(
        self,
        game_id: str,
        platform: Platform,
        existing: str | None,
        existing_shared: bool = False,
    ) -> tuple[str | None, bool]:
        """Return the prefix path and shared flag without creating anything.

        An existing path is always preserved. Only a new Windows game follows
        the current strategy.
        """
        if platform is Platform.LINUX:
            return None, False
        if existing:
            return existing, existing_shared
        assignment = assign_prefix(
            self._prefixes_root, game_id, load_strategy(self._settings_store)
        )
        return assignment.path, assignment.shared

    @staticmethod
    def new_game_id() -> str:
        """Return a fresh ID for a game that has not been persisted yet.

        The editor uses this to cache a cover under its final game ID before
        the database row exists, so a cover import never needs a rename.
        """
        return str(uuid.uuid4())

    def add_game(
        self, draft: GameDraft, *, game_id: str | None = None
    ) -> Game:
        """Validate and insert a new library entry.

        ``game_id`` is optional and intended for the editor's cover flow. When
        omitted the manager generates a new identifier.
        """
        normalized, environment = self._normalize_draft(draft)
        game_id = game_id or self.new_game_id()
        timestamp = _utc_now()
        # Assignment and persistence share one lifecycle-locked section when the
        # new game may reference the shared prefix, so a shared-prefix deletion
        # cannot slip between them.
        with self._lifecycle_guard(normalized.platform, None):
            prefix_path, prefix_shared = self._prefix_for(
                game_id, normalized.platform, None
            )
            game = Game(
                id=game_id,
                name=normalized.name,
                platform=normalized.platform,
                executable=normalized.executable,
                working_directory=normalized.working_directory,
                launch_arguments=normalized.launch_arguments,
                cover_path=normalized.cover_path,
                wine_prefix_path=prefix_path,
                gamemode_enabled=normalized.gamemode_enabled,
                mangohud_enabled=normalized.mangohud_enabled,
                gamescope_resolution=normalized.gamescope_resolution,
                gamescope_enabled=normalized.gamescope_enabled,
                gamescope_output_resolution=(
                    normalized.gamescope_output_resolution
                ),
                gamescope_fps_limit=normalized.gamescope_fps_limit,
                gamescope_vsync=normalized.gamescope_vsync,
                gamescope_filter=normalized.gamescope_filter,
                gamescope_fullscreen=normalized.gamescope_fullscreen,
                display_mode=normalized.display_mode,
                virtual_desktop_resolution=(
                    normalized.virtual_desktop_resolution
                ),
                show_in_library=normalized.show_in_library,
                created_at=timestamp,
                updated_at=timestamp,
                environment=environment,
                wine_prefix_shared=prefix_shared,
            )
            self._database.add_game(game)
        return game

    def register_installation(
        self,
        game_id: str,
        draft: GameDraft,
        *,
        install_directory: str | None,
        install_directory_owned: bool,
        installer_source_directory: str | None,
        installer_source_owned: bool,
        wine_prefix_path: str | None = None,
        wine_prefix_shared: bool = False,
    ) -> Game:
        """Insert or update a game after its installer has run.

        The caller must have verified ``install_directory_owned`` through the
        ownership service before setting it. This method only persists the
        supplied ownership metadata; it does not infer ownership from a path.

        ``wine_prefix_path`` is supplied by an installer session so registration
        uses the prefix the session actually installed into, even if the
        strategy changed meanwhile.
        """
        normalized, environment = self._normalize_draft(draft)
        existing = self._database.get_game(game_id)
        timestamp = _utc_now()
        # A session-supplied shared path is persisted exactly as the session
        # recorded it; no strategy is consulted for that case.
        if wine_prefix_path is not None:
            already_persisted = (
                existing is not None
                and existing.wine_prefix_shared
                and existing.wine_prefix_path == wine_prefix_path
            )
            guard = shared_reference_guard(
                self._prefixes_root,
                wine_prefix_shared,
                new_reference=not already_persisted,
            )
        else:
            guard = self._lifecycle_guard(
                normalized.platform,
                existing.wine_prefix_path if existing else None,
                existing.wine_prefix_shared if existing else False,
            )
        with guard:
            if wine_prefix_path is not None:
                prefix_path, prefix_shared = wine_prefix_path, wine_prefix_shared
            else:
                prefix_path, prefix_shared = self._prefix_for(
                    game_id,
                    normalized.platform,
                    existing.wine_prefix_path if existing else None,
                    existing.wine_prefix_shared if existing else False,
                )
            game = Game(
                id=game_id,
                name=normalized.name,
                platform=normalized.platform,
                executable=normalized.executable,
                working_directory=normalized.working_directory,
                launch_arguments=normalized.launch_arguments,
                cover_path=normalized.cover_path,
                wine_prefix_path=prefix_path,
                gamemode_enabled=normalized.gamemode_enabled,
                mangohud_enabled=normalized.mangohud_enabled,
                gamescope_resolution=normalized.gamescope_resolution,
                gamescope_enabled=normalized.gamescope_enabled,
                gamescope_output_resolution=(
                    normalized.gamescope_output_resolution
                ),
                gamescope_fps_limit=normalized.gamescope_fps_limit,
                gamescope_vsync=normalized.gamescope_vsync,
                gamescope_filter=normalized.gamescope_filter,
                gamescope_fullscreen=normalized.gamescope_fullscreen,
                display_mode=normalized.display_mode,
                virtual_desktop_resolution=(
                    normalized.virtual_desktop_resolution
                ),
                show_in_library=normalized.show_in_library,
                created_at=existing.created_at if existing else timestamp,
                updated_at=timestamp,
                environment=environment,
                install_directory=install_directory,
                install_directory_owned=install_directory_owned,
                installer_source_directory=installer_source_directory,
                installer_source_owned=installer_source_owned,
                wine_prefix_shared=prefix_shared,
            )
            if existing is None:
                self._database.add_game(game)
            elif not self._database.update_game(game):
                raise GameNotFoundError("This game is no longer in the library.")
        return game

    def update_game(self, game_id: str, draft: GameDraft) -> Game:
        """Validate and update an existing library entry."""
        existing = self._database.get_game(game_id)
        if existing is None:
            raise GameNotFoundError("This game is no longer in the library.")

        normalized, environment = self._normalize_draft(draft)
        with self._lifecycle_guard(
            normalized.platform,
            existing.wine_prefix_path,
            existing.wine_prefix_shared,
        ):
            prefix_path, prefix_shared = self._prefix_for(
                existing.id,
                normalized.platform,
                existing.wine_prefix_path,
                existing.wine_prefix_shared,
            )
            game = Game(
                id=existing.id,
                name=normalized.name,
                platform=normalized.platform,
                executable=normalized.executable,
                working_directory=normalized.working_directory,
                launch_arguments=normalized.launch_arguments,
                cover_path=normalized.cover_path,
                wine_prefix_path=prefix_path,
                gamemode_enabled=normalized.gamemode_enabled,
                mangohud_enabled=normalized.mangohud_enabled,
                gamescope_resolution=normalized.gamescope_resolution,
                gamescope_enabled=normalized.gamescope_enabled,
                gamescope_output_resolution=(
                    normalized.gamescope_output_resolution
                ),
                gamescope_fps_limit=normalized.gamescope_fps_limit,
                gamescope_vsync=normalized.gamescope_vsync,
                gamescope_filter=normalized.gamescope_filter,
                gamescope_fullscreen=normalized.gamescope_fullscreen,
                display_mode=normalized.display_mode,
                virtual_desktop_resolution=(
                    normalized.virtual_desktop_resolution
                ),
                show_in_library=normalized.show_in_library,
                created_at=existing.created_at,
                updated_at=_utc_now(),
                environment=environment,
                # Ownership metadata is not edited by the game editor. Preserve it
                # so a metadata edit never claims or drops ownership of data.
                install_directory=existing.install_directory,
                install_directory_owned=existing.install_directory_owned,
                installer_source_directory=existing.installer_source_directory,
                installer_source_owned=existing.installer_source_owned,
                wine_prefix_shared=prefix_shared,
            )
            if not self._database.update_game(game):
                raise GameNotFoundError("This game is no longer in the library.")
        return game

    def remove_game(self, game_id: str) -> Game:
        """Remove only the database record and return the removed entry.

        No filesystem operation is performed here. In particular, the game
        directory, executable, Wine prefix, and any shared cover file remain
        untouched on disk.
        """
        existing = self._database.get_game(game_id)
        if existing is None:
            raise GameNotFoundError("This game is no longer in the library.")
        self._database.delete_game(game_id)
        return existing

    def set_cover(self, game_id: str, cover_path: str | None) -> Game:
        """Update only the cover reference after the cache has accepted it.

        File copying, validation, and cache deletion belong to ``CoverManager``;
        this method persists the resulting path in SQLite.
        """
        existing = self._database.get_game(game_id)
        if existing is None:
            raise GameNotFoundError("This game is no longer in the library.")
        updated_at = _utc_now()
        if not self._database.update_cover(game_id, cover_path, updated_at):
            raise GameNotFoundError("This game is no longer in the library.")
        return replace(existing, cover_path=cover_path, updated_at=updated_at)

    def get_game(self, game_id: str) -> Game | None:
        """Return a single entry by ID."""
        return self._database.get_game(game_id)

    def list_games(
        self,
        search: str = "",
        sort_by: str = SORT_NAME,
        *,
        include_hidden: bool = False,
    ) -> list[Game]:
        """Return entries filtered by name and sorted for the library UI.

        Hidden games are omitted by default. Management paths that must see
        every record (cleanup, shared-prefix checks) pass ``include_hidden``.
        """
        if sort_by not in VALID_SORTS:
            raise ValueError(f"Unsupported sort mode: {sort_by}")

        games = self._database.list_games()
        if not include_hidden:
            games = [game for game in games if game.show_in_library]
        needle = search.strip().casefold()
        if needle:
            games = [game for game in games if needle in game.name.casefold()]

        # Sort by a stable secondary key first, then apply the primary key.
        games.sort(key=lambda game: (game.name.casefold(), game.created_at))
        if sort_by == SORT_RECENT:
            games.sort(key=lambda game: game.created_at, reverse=True)
        return games

    def list_hidden_games(self) -> list[Game]:
        """Return only entries hidden from the normal library, sorted by name."""
        return sorted(
            (
                game
                for game in self._database.list_games()
                if not game.show_in_library
            ),
            key=lambda game: (game.name.casefold(), game.created_at),
        )

    def set_library_visibility(self, game_id: str, visible: bool) -> Game:
        """Show or hide one entry without changing any other field."""
        existing = self._database.get_game(game_id)
        if existing is None:
            raise GameNotFoundError("This game is no longer in the library.")
        updated_at = _utc_now()
        if not self._database.update_visibility(
            game_id, visible, updated_at
        ):
            raise GameNotFoundError("This game is no longer in the library.")
        return replace(
            existing,
            show_in_library=visible,
            updated_at=updated_at,
        )
