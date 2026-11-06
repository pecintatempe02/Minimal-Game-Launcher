"""Preview, verify, and delete MGL-managed game data.

This service is the only user-facing layer allowed to call
:meth:`OwnershipService.delete`. It adds association rules on top of the
ownership primitive:

- a target must still be the exact managed path for its stable game ID;
- a running or starting game blocks every one of its resources;
- an active or deferred installer session blocks its staging, destination, and
  prefix;
- game metadata must still reference a non-orphan target at deletion time;
- the directory identity observed during preview must be unchanged.

Orphan scanning is on demand and limited to the three managed roots. It never
follows symlinks, never leaves a root, and never deletes anything by itself.
"""

from __future__ import annotations

import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from minimal_game_launcher.database import Database
from minimal_game_launcher.errors import OwnershipError, PrefixInitializationError
from minimal_game_launcher.models import (
    Game,
    InstallerSession,
    InstallerStatus,
    Platform,
)
from minimal_game_launcher.paths import SHARED_PREFIX_DIRECTORY_NAME
from minimal_game_launcher.services.prefix_lock import (
    shared_prefix_lifecycle_lock,
    shared_prefix_lock,
)
from minimal_game_launcher.services.ownership import (
    DeletionResult,
    OwnershipService,
    OwnershipStatus,
    OwnershipVerdict,
    ResourceKind,
    SizeEstimate,
)

LOGGER = logging.getLogger(__name__)

# Sessions whose staged or installed data must not be deleted by orphan cleanup.
# A ``failed`` session stays resumable (its staging is retried or the user removes
# it explicitly), so it protects data too. Only ``installed`` is terminal: its
# data now belongs to the library entry.
PROTECTING_SESSION_STATUSES = frozenset(
    {
        InstallerStatus.PENDING,
        InstallerStatus.STAGED,
        InstallerStatus.INSTALLING,
        InstallerStatus.INSTALLER_FINISHED,
        InstallerStatus.DEFERRED,
        InstallerStatus.FAILED,
    }
)

_KIND_ORDER = (
    ResourceKind.INSTALLATION,
    ResourceKind.PREFIX,
    ResourceKind.INSTALLER_SOURCE,
)
# The shared prefix lives under the prefix root, so it is not a _KIND_ORDER
# entry (that would scan the root twice). It is previewed on its own.
_SHARED_TARGET_ID = SHARED_PREFIX_DIRECTORY_NAME


@dataclass(frozen=True, slots=True)
class ResourceTarget:
    """One previewable managed resource and the reason it may be deleted."""

    kind: ResourceKind
    game_id: str
    game_name: str | None
    path: Path
    verdict: OwnershipVerdict
    estimate: SizeEstimate | None
    identity: tuple[int, int] | None
    running: bool
    installer_active: bool
    orphan: bool
    unavailable_reason: str | None = None

    @property
    def can_delete(self) -> bool:
        """Return whether the target passed preview-time safety checks."""
        return self.unavailable_reason is None and self.verdict.is_managed

    @property
    def status(self) -> OwnershipStatus:
        """Return the ownership classification shown in the UI."""
        return self.verdict.status


@dataclass(frozen=True, slots=True)
class CleanupIssue:
    """A managed-root entry that is not a safe deletion candidate."""

    kind: ResourceKind
    path: Path
    reason: str


@dataclass(frozen=True, slots=True)
class CleanupOutcome:
    """Result of one selected target, including partial-failure details."""

    target: ResourceTarget
    deleted: bool
    removed_entries: int = 0
    errors: tuple[str, ...] = ()
    skipped_reason: str | None = None
    remaining: SizeEstimate | None = None


@dataclass(frozen=True, slots=True)
class CleanupReport:
    """Outcome list for a user-confirmed cleanup selection."""

    outcomes: tuple[CleanupOutcome, ...]

    @property
    def all_deleted(self) -> bool:
        """Return whether every selected target was removed completely."""
        return all(outcome.deleted for outcome in self.outcomes)

    @property
    def failed(self) -> tuple[CleanupOutcome, ...]:
        """Return targets that were attempted but not fully removed."""
        return tuple(
            outcome
            for outcome in self.outcomes
            if not outcome.deleted and outcome.skipped_reason is None
        )

    @property
    def skipped(self) -> tuple[CleanupOutcome, ...]:
        """Return targets refused before deletion."""
        return tuple(
            outcome for outcome in self.outcomes if outcome.skipped_reason
        )


@dataclass(frozen=True, slots=True)
class OrphanScan:
    """Safe orphan candidates and entries that could not be classified."""

    candidates: tuple[ResourceTarget, ...]
    issues: tuple[CleanupIssue, ...]


class CleanupService:
    """Build cleanup previews, scan orphans, and delete confirmed targets.

    The service owns no GTK widgets and performs no automatic cleanup. Every
    destructive call requires ``authorized=True`` and revalidates ownership,
    association, running state, session state, and directory identity.
    """

    def __init__(
        self,
        database: Database,
        ownership: OwnershipService,
        *,
        is_game_active: Callable[[str], bool] | None = None,
        max_scan_entries: int = 10_000,
    ) -> None:
        self._database = database
        self._ownership = ownership
        self._is_game_active = is_game_active or (lambda _game_id: False)
        self._max_scan_entries = max(1, max_scan_entries)

    # ------------------------------------------------------------------
    # Game-target preview

    def targets_for_game(self, game: Game) -> list[ResourceTarget]:
        """Return every stored resource reference for one library entry.

        External, missing, and invalid paths are returned with an
        ``unavailable_reason`` instead of being hidden, so the UI can explain
        why they cannot be deleted.
        """
        targets: list[ResourceTarget] = []
        if game.install_directory:
            targets.append(
                self._build_target(
                    ResourceKind.INSTALLATION,
                    game.id,
                    game.name,
                    game.install_directory,
                )
            )
        if game.wine_prefix_path and game.wine_prefix_shared:
            # The shared prefix is one resource for all games; it is never
            # deleted as a per-game prefix.
            targets.append(self.shared_prefix_target())
        elif game.wine_prefix_path:
            targets.append(
                self._build_target(
                    ResourceKind.PREFIX,
                    game.id,
                    game.name,
                    game.wine_prefix_path,
                )
            )
        if game.installer_source_directory:
            targets.append(
                self._build_target(
                    ResourceKind.INSTALLER_SOURCE,
                    game.id,
                    game.name,
                    game.installer_source_directory,
                )
            )
        return targets

    def target_for(
        self,
        kind: ResourceKind,
        game_id: str,
        path: str | Path,
        *,
        game_name: str | None = None,
        orphan: bool = False,
    ) -> ResourceTarget:
        """Build one target, used by tests and focused callers."""
        return self._build_target(
            kind, game_id, game_name, path, orphan=orphan
        )

    def shared_prefix_target(self) -> ResourceTarget:
        """Return the one shared prefix as a single previewable resource.

        It is deletable only when no library game and no protecting installer
        session references it, and no referencing game is running.
        """
        kind = ResourceKind.SHARED_PREFIX
        path = self._ownership.shared_prefix_path()
        verdict = self._ownership.classify(kind, _SHARED_TARGET_ID, path)
        estimate = (
            self._ownership.estimate(kind, _SHARED_TARGET_ID, path)
            if verdict.is_managed
            else None
        )
        identity = _directory_identity(verdict.path) if verdict.is_managed else None
        games, sessions = self._shared_references()
        running = any(self._is_game_active(game.id) for game in games)

        reason: str | None = None
        if not self._root_is_safe(kind):
            reason = (
                "The managed root is a symbolic link or not a directory; "
                "cleanup refuses to operate through it."
            )
        elif not verdict.is_managed:
            reason = verdict.reason
        elif running:
            reason = "A game that uses the shared prefix is starting or running."
        elif games or sessions:
            reason = (
                f"The shared prefix is still used by {len(games)} library "
                f"game(s) and {len(sessions)} pending install(s)."
            )
        return ResourceTarget(
            kind=kind,
            game_id=_SHARED_TARGET_ID,
            game_name="Shared Wine prefix",
            path=Path(path),
            verdict=verdict,
            estimate=estimate,
            identity=identity,
            running=running,
            installer_active=bool(sessions),
            orphan=not (games or sessions),
            unavailable_reason=reason,
        )

    # ------------------------------------------------------------------
    # Orphan scanning

    def scan_orphans(self) -> OrphanScan:
        """Find managed directories with no game or protecting session.

        Only direct children of the three managed roots are inspected. Symlinks,
        non-directories, unsafe names, unreadable entries, and entries beyond the
        scan bound are reported as issues and are never deletion candidates.
        """
        candidates: list[ResourceTarget] = []
        issues: list[CleanupIssue] = []

        for kind in _KIND_ORDER:
            root = self._ownership.root_for(kind)
            try:
                root_info = os.lstat(root)
            except FileNotFoundError:
                continue
            except OSError as error:
                issues.append(
                    CleanupIssue(kind, root, _error_text(error))
                )
                continue
            if stat.S_ISLNK(root_info.st_mode):
                issues.append(
                    CleanupIssue(
                        kind,
                        root,
                        "The managed root is a symbolic link; it was not scanned.",
                    )
                )
                continue
            if not stat.S_ISDIR(root_info.st_mode):
                issues.append(
                    CleanupIssue(kind, root, "The managed root is not a directory.")
                )
                continue

            try:
                iterator = os.scandir(root)
            except OSError as error:
                issues.append(CleanupIssue(kind, root, _error_text(error)))
                continue

            count = 0
            try:
                with iterator:
                    for entry in iterator:
                        count += 1
                        if count > self._max_scan_entries:
                            issues.append(
                                CleanupIssue(
                                    kind,
                                    root,
                                    "The scan limit was reached; remaining "
                                    "entries were not inspected.",
                                )
                            )
                            break
                        self._inspect_orphan_entry(kind, root, entry, candidates, issues)
            except OSError as error:
                issues.append(CleanupIssue(kind, root, _error_text(error)))

        # A referenced shared prefix is simply in use; only an unreferenced one
        # is an orphan candidate, and a missing one has nothing to report.
        shared = self.shared_prefix_target()
        if shared.orphan and shared.verdict.status is not OwnershipStatus.MISSING:
            if shared.can_delete:
                candidates.append(shared)
            else:
                issues.append(
                    CleanupIssue(
                        ResourceKind.SHARED_PREFIX,
                        shared.path,
                        shared.unavailable_reason or "Not safely removable.",
                    )
                )

        return OrphanScan(tuple(candidates), tuple(issues))

    def _inspect_orphan_entry(
        self,
        kind: ResourceKind,
        root: Path,
        entry: os.DirEntry,
        candidates: list[ResourceTarget],
        issues: list[CleanupIssue],
    ) -> None:
        if kind is ResourceKind.PREFIX and entry.name == _SHARED_TARGET_ID:
            # Handled once by shared_prefix_target(), never as a per-game prefix.
            return
        path = root / entry.name
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError as error:
            issues.append(CleanupIssue(kind, path, _error_text(error)))
            return
        if stat.S_ISLNK(info.st_mode):
            issues.append(
                CleanupIssue(
                    kind,
                    path,
                    "The entry is a symbolic link; links are never deleted or "
                    "followed.",
                )
            )
            return
        if not stat.S_ISDIR(info.st_mode):
            issues.append(
                CleanupIssue(kind, path, "The entry is not a directory.")
            )
            return

        verdict = self._ownership.classify(kind, entry.name, path)
        if not verdict.is_managed:
            issues.append(CleanupIssue(kind, path, verdict.reason))
            return

        if self._associated_with_game(kind, entry.name, path):
            return
        if self._associated_with_protected_session(kind, entry.name, path):
            return

        target = self._build_target(
            kind, entry.name, None, path, orphan=True
        )
        if target.can_delete:
            candidates.append(target)
        else:
            issues.append(
                CleanupIssue(
                    kind,
                    path,
                    target.unavailable_reason or "The resource is not safely removable.",
                )
            )

    # ------------------------------------------------------------------
    # Deletion

    def delete_targets(
        self, targets: list[ResourceTarget] | tuple[ResourceTarget, ...],
        *, authorized: bool,
    ) -> CleanupReport:
        """Delete confirmed targets, continuing after individual failures."""
        if not authorized:
            raise OwnershipError(
                "Deleting launcher data requires explicit authorization."
            )
        return CleanupReport(
            tuple(self.delete_target(target, authorized=True) for target in targets)
        )

    def delete_target(
        self, target: ResourceTarget, *, authorized: bool
    ) -> CleanupOutcome:
        """Delete one target after revalidating every preview-time condition."""
        if not authorized:
            raise OwnershipError(
                "Deleting launcher data requires explicit authorization."
            )
        if target.kind is not ResourceKind.SHARED_PREFIX:
            return self._delete_checked(target)

        # Lock order: lifecycle (references) first, then initialization. Holding
        # lifecycle keeps assignment from persisting a new reference while we
        # revalidate and delete. Initialization is taken with a short wait so a
        # running wineboot makes deletion report "busy" instead of blocking.
        root = self._ownership.root_for(ResourceKind.SHARED_PREFIX)
        try:
            with shared_prefix_lifecycle_lock(root):
                with shared_prefix_lock(root, timeout=0):
                    return self._delete_checked(target)
        except PrefixInitializationError as error:
            return CleanupOutcome(
                target=target, deleted=False, skipped_reason=str(error)
            )

    def _delete_checked(self, target: ResourceTarget) -> CleanupOutcome:
        """Revalidate and delete; the caller holds any required lock."""
        skipped = self._revalidate(target)
        if skipped is not None:
            LOGGER.warning(
                "Skipped cleanup of %s for game %s: %s",
                target.kind.value,
                target.game_id,
                skipped,
            )
            return CleanupOutcome(
                target=target, deleted=False, skipped_reason=skipped
            )

        try:
            result = self._ownership.delete(
                target.kind,
                target.game_id,
                target.path,
                authorized=True,
                game_running=self._is_game_active(target.game_id),
            )
        except OwnershipError as error:
            return CleanupOutcome(
                target=target,
                deleted=False,
                errors=(str(error),),
                remaining=self._remaining(target),
            )
        except OSError as error:
            return CleanupOutcome(
                target=target,
                deleted=False,
                errors=(_error_text(error),),
                remaining=self._remaining(target),
            )

        return self._outcome(target, result)

    def _revalidate(self, target: ResourceTarget) -> str | None:
        """Return a skip reason, or ``None`` when deletion may proceed."""
        verdict = self._ownership.classify(
            target.kind, target.game_id, target.path
        )
        if not verdict.is_managed:
            return f"Ownership changed: {verdict.reason}"
        if not self._root_is_safe(target.kind):
            return (
                "The managed root is a symbolic link or not a directory; "
                "cleanup refuses to operate through it."
            )

        current_identity = _directory_identity(verdict.path)
        if target.identity is None or current_identity != target.identity:
            return (
                "The directory changed after the preview; nothing was deleted."
            )

        if target.kind is ResourceKind.SHARED_PREFIX:
            # Re-derived at delete time: a shared prefix is deleted only when
            # nothing references it any more.
            games, sessions = self._shared_references()
            if any(self._is_game_active(game.id) for game in games):
                return "A game that uses the shared prefix is starting or running."
            if games or sessions:
                return (
                    "The shared prefix is still referenced; nothing was deleted."
                )
            return None

        if self._is_game_active(target.game_id):
            return "The game is starting or running."

        session = self._database.get_installer_session(target.game_id)
        if session is not None and session.status in PROTECTING_SESSION_STATUSES:
            return (
                "An active or deferred installer session uses this data."
            )

        if target.orphan:
            if self._associated_with_game(
                target.kind, target.game_id, target.path
            ):
                return "The resource is now associated with a library game."
            return None

        game = self._database.get_game(target.game_id)
        if game is None or not self._game_references(game, target):
            return (
                "The library entry no longer references this path; nothing "
                "was deleted."
            )
        return None

    def _outcome(
        self, target: ResourceTarget, result: DeletionResult
    ) -> CleanupOutcome:
        if result.deleted:
            return CleanupOutcome(
                target=target,
                deleted=True,
                removed_entries=result.removed_entries,
            )
        return CleanupOutcome(
            target=target,
            deleted=False,
            removed_entries=result.removed_entries,
            errors=result.errors,
            remaining=self._remaining(target),
        )

    def _remaining(self, target: ResourceTarget) -> SizeEstimate | None:
        """Measure what still exists after a failed or partial deletion."""
        try:
            return self._ownership.estimate(
                target.kind, target.game_id, target.path
            )
        except OSError:
            return None

    # ------------------------------------------------------------------
    # Association rules

    def _root_is_safe(self, kind: ResourceKind) -> bool:
        """Refuse a managed root that is a symlink or not a directory."""
        root = self._ownership.root_for(kind)
        try:
            info = os.lstat(root)
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return stat.S_ISDIR(info.st_mode)

    def _build_target(
        self,
        kind: ResourceKind,
        game_id: str,
        game_name: str | None,
        path: str | Path,
        *,
        orphan: bool = False,
    ) -> ResourceTarget:
        verdict = self._ownership.classify(kind, game_id, path)
        estimate = (
            self._ownership.estimate(kind, game_id, path)
            if verdict.is_managed
            else None
        )
        identity = _directory_identity(verdict.path) if verdict.is_managed else None
        running = bool(self._is_game_active(game_id))
        session = self._database.get_installer_session(game_id)
        installer_active = (
            session is not None
            and session.status in PROTECTING_SESSION_STATUSES
        )

        reason: str | None = None
        if not self._root_is_safe(kind):
            reason = (
                "The managed root is a symbolic link or not a directory; "
                "cleanup refuses to operate through it."
            )
        elif not verdict.is_managed:
            reason = verdict.reason
        elif running:
            reason = "The game is starting or running."
        elif installer_active:
            reason = "An active or deferred installer session uses this data."

        return ResourceTarget(
            kind=kind,
            game_id=game_id,
            game_name=game_name,
            path=Path(path).expanduser(),
            verdict=verdict,
            estimate=estimate,
            identity=identity,
            running=running,
            installer_active=installer_active,
            orphan=orphan,
            unavailable_reason=reason,
        )

    def _associated_with_game(
        self, kind: ResourceKind, game_id: str, path: str | Path
    ) -> bool:
        game = self._database.get_game(game_id)
        if game is None:
            return False
        expected = self._ownership.expected_path(kind, game_id)
        if not _same_path(path, expected):
            return False
        return self._game_references_to_path(game, kind, path)

    def _associated_with_protected_session(
        self, kind: ResourceKind, game_id: str, path: str | Path
    ) -> bool:
        session = self._database.get_installer_session(game_id)
        if (
            session is None
            or session.status not in PROTECTING_SESSION_STATUSES
        ):
            return False
        if kind is ResourceKind.INSTALLER_SOURCE:
            return _same_path(path, session.staging_directory)
        if kind is ResourceKind.INSTALLATION:
            return bool(
                session.install_directory_owned
                and session.install_directory
                and _same_path(path, session.install_directory)
            )
        return _same_path(
            path, self._ownership.expected_path(ResourceKind.PREFIX, game_id)
        )

    def _game_references(
        self, game: Game, target: ResourceTarget
    ) -> bool:
        return self._game_references_to_path(
            game, target.kind, target.path
        )

    def _shared_references(self) -> tuple[list[Game], list[InstallerSession]]:
        """Return library games and protecting sessions using the shared prefix."""
        shared_path = self._ownership.shared_prefix_path()
        games = [
            game
            for game in self._database.list_games()
            if self._uses_shared_prefix(game.wine_prefix_shared, game.wine_prefix_path, shared_path)
        ]
        sessions = [
            session
            for session in self._database.list_installer_sessions()
            if session.status in PROTECTING_SESSION_STATUSES
            and self._uses_shared_prefix(
                session.wine_prefix_shared, session.wine_prefix_path, shared_path
            )
        ]
        return games, sessions

    @staticmethod
    def _uses_shared_prefix(
        shared: bool, prefix_path: str | None, shared_path: Path
    ) -> bool:
        return bool(shared and prefix_path and _same_path(prefix_path, shared_path))

    def _game_references_to_path(
        self, game: Game, kind: ResourceKind, path: str | Path
    ) -> bool:
        if kind is ResourceKind.SHARED_PREFIX:
            return self._uses_shared_prefix(
                game.wine_prefix_shared,
                game.wine_prefix_path,
                self._ownership.shared_prefix_path(),
            ) and _same_path(path, self._ownership.shared_prefix_path())
        if kind is ResourceKind.INSTALLATION:
            return bool(
                game.install_directory
                and _same_path(path, game.install_directory)
            )
        if kind is ResourceKind.PREFIX:
            return bool(
                game.platform is Platform.WINDOWS
                and game.wine_prefix_path
                and _same_path(path, game.wine_prefix_path)
            )
        return bool(
            game.installer_source_directory
            and _same_path(path, game.installer_source_directory)
        )


def _same_path(left: str | Path, right: str | Path) -> bool:
    try:
        return Path(left).expanduser().resolve(strict=False) == Path(
            right
        ).expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        return False


def _directory_identity(path: Path | None) -> tuple[int, int] | None:
    if path is None:
        return None
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISDIR(info.st_mode):
        return None
    return (info.st_dev, info.st_ino)


def _error_text(error: OSError) -> str:
    return error.strerror or str(error)


__all__ = [
    "CleanupIssue",
    "CleanupOutcome",
    "CleanupReport",
    "CleanupService",
    "OrphanScan",
    "PROTECTING_SESSION_STATUSES",
    "ResourceTarget",
]
