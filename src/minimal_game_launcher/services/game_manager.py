"""Game-library business logic and launch-field validation."""

from __future__ import annotations

import os
import shlex
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from minimal_game_launcher.database import Database
from minimal_game_launcher.errors import GameNotFoundError, GameValidationError
from minimal_game_launcher.models import Game, GameDraft, Platform

SORT_NAME = "name"
SORT_RECENT = "recent"
VALID_SORTS = frozenset({SORT_NAME, SORT_RECENT})


def _utc_now() -> str:
    """Return a sortable UTC timestamp for database records."""
    return datetime.now(UTC).isoformat(timespec="microseconds")


class GameManager:
    """Validate, normalize, persist, search, and remove library entries.

    The manager assigns a dedicated Wine prefix path to Windows games but does
    not create it. Prefix creation belongs to the future launch phase, so a
    Windows game may be registered before Wine is installed or initialized.
    """

    def __init__(self, database: Database, prefixes_root: str | Path) -> None:
        self._database = database
        self._prefixes_root = Path(prefixes_root).expanduser()

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
    def default_working_directory(executable: str) -> str:
        """Suggest the executable's parent directory for the form field."""
        if not executable.strip():
            return ""
        return str(Path(executable).expanduser().parent)

    def _normalize_draft(self, draft: GameDraft) -> GameDraft:
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

        executable = Path(raw_executable).expanduser()
        if not executable.is_absolute():
            executable = executable.resolve()
        else:
            executable = executable.resolve(strict=False)
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

        launch_arguments = draft.launch_arguments.strip()
        self.split_launch_arguments(launch_arguments)

        cover_path: str | None = None
        if draft.cover_path and draft.cover_path.strip():
            # Covers remain external, user-owned references. They are not
            # copied or deleted, and a missing cover must not make metadata
            # edits impossible.
            cover_path = str(
                Path(draft.cover_path).expanduser().resolve(strict=False)
            )

        return replace(
            draft,
            name=name,
            executable=str(executable),
            working_directory=str(working_directory),
            launch_arguments=launch_arguments,
            cover_path=cover_path,
        )

    def _prefix_for(self, game_id: str, platform: Platform, existing: str | None) -> str | None:
        """Return the dedicated prefix path without creating the directory."""
        if platform is Platform.LINUX:
            return None
        return existing or str(self._prefixes_root / game_id)

    def add_game(self, draft: GameDraft) -> Game:
        """Validate and insert a new library entry."""
        normalized = self._normalize_draft(draft)
        game_id = str(uuid.uuid4())
        timestamp = _utc_now()
        game = Game(
            id=game_id,
            name=normalized.name,
            platform=normalized.platform,
            executable=normalized.executable,
            working_directory=normalized.working_directory,
            launch_arguments=normalized.launch_arguments,
            cover_path=normalized.cover_path,
            wine_prefix_path=self._prefix_for(game_id, normalized.platform, None),
            created_at=timestamp,
            updated_at=timestamp,
        )
        self._database.add_game(game)
        return game

    def update_game(self, game_id: str, draft: GameDraft) -> Game:
        """Validate and update an existing library entry."""
        existing = self._database.get_game(game_id)
        if existing is None:
            raise GameNotFoundError("This game is no longer in the library.")

        normalized = self._normalize_draft(draft)
        game = Game(
            id=existing.id,
            name=normalized.name,
            platform=normalized.platform,
            executable=normalized.executable,
            working_directory=normalized.working_directory,
            launch_arguments=normalized.launch_arguments,
            cover_path=normalized.cover_path,
            wine_prefix_path=self._prefix_for(
                existing.id, normalized.platform, existing.wine_prefix_path
            ),
            created_at=existing.created_at,
            updated_at=_utc_now(),
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

    def get_game(self, game_id: str) -> Game | None:
        """Return a single entry by ID."""
        return self._database.get_game(game_id)

    def list_games(self, search: str = "", sort_by: str = SORT_NAME) -> list[Game]:
        """Return entries filtered by name and sorted for the library UI."""
        if sort_by not in VALID_SORTS:
            raise ValueError(f"Unsupported sort mode: {sort_by}")

        games = self._database.list_games()
        needle = search.strip().casefold()
        if needle:
            games = [game for game in games if needle in game.name.casefold()]

        # Sort by a stable secondary key first, then apply the primary key.
        games.sort(key=lambda game: (game.name.casefold(), game.created_at))
        if sort_by == SORT_RECENT:
            games.sort(key=lambda game: game.created_at, reverse=True)
        return games
