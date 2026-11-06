"""Verify, measure, and safely delete launcher-owned resources.

Ownership is decided by configuration, not by a directory name or by living
under the user's home directory. Each resource kind has exactly one managed
root and each game owns exactly one directory inside it:

``<data>/games/<game-id>``, ``<data>/prefixes/<game-id>``, and
``<data>/installers/<game-id>``.

Guarantees:

- A resource is managed only when its resolved path equals the expected
  game-specific directory under the configured root. Containment uses resolved
  path components, never string prefixes.
- Game IDs are validated before they are used in a path, so ``..``, absolute
  paths, and separators cannot escape a root. A supplied path containing a
  parent-directory segment is rejected outright instead of being normalised.
- A symlink at a resource path is never managed, whatever it points to.
- Size estimation and deletion never follow symlinks. Both walk directory file
  descriptors opened with ``O_NOFOLLOW``; symlink entries are counted as links
  and unlinked, never traversed.
- Deletion requires explicit authorization from the caller and refuses managed
  roots, mismatched game IDs, external paths, and running games.

Limitations:

- The leaf-directory identity is re-checked immediately before ``rmdir``, but
  no primitive can serialise against an attacker who replaces paths between
  syscalls. The worst case under a concurrent swap is a recorded error rather
  than traversal outside the opened directory.
- Size estimation returns partial values with ``complete=False`` on any error;
  hardlinked regular files are counted once, but sparse files are measured by
  apparent size.
- Ownership of the configured roots themselves follows the launcher's own
  configuration. A user-created symlinked data root is resolved once and is
  treated as that root.
"""

from __future__ import annotations

import logging
import os
import re
import stat
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from minimal_game_launcher.errors import OwnershipError
from minimal_game_launcher.paths import SHARED_PREFIX_DIRECTORY_NAME

LOGGER = logging.getLogger(__name__)

_SAFE_GAME_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_OPEN_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(
    os, "O_CLOEXEC", 0
)
_MAX_DEPTH = 64


class ResourceKind(StrEnum):
    """Kinds of filesystem data the launcher can manage."""

    INSTALLATION = "installation"
    PREFIX = "prefix"
    SHARED_PREFIX = "shared_prefix"
    INSTALLER_SOURCE = "installer_source"


class OwnershipStatus(StrEnum):
    """Outcome of an ownership check."""

    MANAGED = "managed"
    EXTERNAL = "external"
    MISSING = "missing"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class OwnershipVerdict:
    """The ownership answer for one path, with the reason it was reached."""

    kind: ResourceKind
    game_id: str
    requested_path: Path
    path: Path | None
    expected_path: Path | None
    status: OwnershipStatus
    reason: str

    @property
    def is_managed(self) -> bool:
        """Return whether the resource is verified MGL-owned."""
        return self.status is OwnershipStatus.MANAGED


@dataclass(frozen=True, slots=True)
class SizeEstimate:
    """Disk usage for a managed resource, flagged when it is incomplete."""

    total_bytes: int
    file_count: int
    symlink_count: int
    complete: bool
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DeletionResult:
    """Outcome of a deletion request after ownership was accepted."""

    deleted: bool
    removed_entries: int
    errors: tuple[str, ...] = ()


@dataclass(slots=True)
class _Totals:
    total_bytes: int = 0
    file_count: int = 0
    symlink_count: int = 0
    errors: list[str] = field(default_factory=list)


class OwnershipService:
    """Classify, measure, and delete resources inside configured roots.

    The service performs no implicit creation. It is intentionally not wired to
    any user-facing deletion flow in this step.
    """

    def __init__(
        self,
        prefixes_root: str | Path,
        games_root: str | Path,
        installers_root: str | Path,
    ) -> None:
        self._roots = {
            ResourceKind.PREFIX: Path(prefixes_root).expanduser(),
            ResourceKind.SHARED_PREFIX: Path(prefixes_root).expanduser(),
            ResourceKind.INSTALLATION: Path(games_root).expanduser(),
            ResourceKind.INSTALLER_SOURCE: Path(installers_root).expanduser(),
        }

    def root_for(self, kind: ResourceKind) -> Path:
        """Return the configured managed root for a resource kind."""
        return self._roots[kind]

    def shared_prefix_path(self) -> Path:
        """Return the one stable managed shared-prefix path."""
        return self.root_for(ResourceKind.SHARED_PREFIX) / (
            SHARED_PREFIX_DIRECTORY_NAME
        )

    def expected_path(self, kind: ResourceKind, game_id: str) -> Path:
        """Return the managed path for ``game_id`` after validating the ID."""
        if kind is ResourceKind.SHARED_PREFIX:
            return self.shared_prefix_path()
        self._validate_game_id(game_id)
        return self.root_for(kind) / game_id

    def classify(
        self, kind: ResourceKind, game_id: str, path: str | Path
    ) -> OwnershipVerdict:
        """Decide whether ``path`` is the managed resource for ``game_id``."""
        raw = Path(path).expanduser()
        root = self.root_for(kind)
        shared = kind is ResourceKind.SHARED_PREFIX

        if not str(path).strip():
            return self._verdict(
                kind, game_id, raw, None, None,
                OwnershipStatus.INVALID, "The resource path is empty.",
            )
        if any(part == ".." for part in raw.parts):
            return self._verdict(
                kind, game_id, raw, None, None,
                OwnershipStatus.INVALID,
                "The resource path contains a parent-directory segment.",
            )
        if not shared:
            try:
                self._validate_game_id(game_id)
            except OwnershipError as error:
                return self._verdict(
                    kind, game_id, raw, None, None,
                    OwnershipStatus.INVALID, str(error),
                )

        try:
            resolved_root = root.resolve(strict=False)
            resolved = raw.resolve(strict=False)
        except (OSError, RuntimeError):
            return self._verdict(
                kind, game_id, raw, None, None,
                OwnershipStatus.INVALID,
                "The resource path could not be resolved safely.",
            )
        expected = resolved_root / (
            SHARED_PREFIX_DIRECTORY_NAME if shared else game_id
        )

        try:
            info = os.lstat(raw)
        except FileNotFoundError:
            info = None
        except OSError:
            return self._verdict(
                kind, game_id, raw, resolved, expected,
                OwnershipStatus.INVALID,
                "The resource path could not be inspected.",
            )
        if info is not None and stat.S_ISLNK(info.st_mode):
            return self._verdict(
                kind, game_id, raw, resolved, expected,
                OwnershipStatus.INVALID,
                "The resource path is a symbolic link; links are never "
                "treated as launcher-owned data.",
            )

        if resolved == expected:
            if info is None:
                return self._verdict(
                    kind, game_id, raw, resolved, expected,
                    OwnershipStatus.MISSING,
                    "The expected managed directory does not exist.",
                )
            if not stat.S_ISDIR(info.st_mode):
                return self._verdict(
                    kind, game_id, raw, resolved, expected,
                    OwnershipStatus.INVALID,
                    "The expected managed path is not a directory.",
                )
            return self._verdict(
                kind, game_id, raw, resolved, expected,
                OwnershipStatus.MANAGED,
                "The path is the managed directory for this game ID.",
            )

        if resolved == resolved_root:
            return self._verdict(
                kind, game_id, raw, resolved, expected,
                OwnershipStatus.INVALID,
                "The path is the managed root itself, not a game resource.",
            )
        if resolved.is_relative_to(resolved_root):
            return self._verdict(
                kind, game_id, raw, resolved, expected,
                OwnershipStatus.INVALID,
                "The path is under the managed root but does not match this "
                "game ID's directory.",
            )
        return self._verdict(
            kind, game_id, raw, resolved, expected,
            OwnershipStatus.EXTERNAL,
            "The path is outside the managed root for this resource kind.",
        )

    def estimate(
        self, kind: ResourceKind, game_id: str, path: str | Path
    ) -> SizeEstimate:
        """Measure a verified managed resource without following symlinks."""
        verdict = self.classify(kind, game_id, path)
        if not verdict.is_managed or verdict.path is None:
            return SizeEstimate(
                total_bytes=0,
                file_count=0,
                symlink_count=0,
                complete=False,
                errors=(f"Not a managed resource: {verdict.reason}",),
            )

        try:
            descriptor = os.open(verdict.path, _OPEN_DIRECTORY)
        except OSError as error:
            return SizeEstimate(
                0, 0, 0, False,
                (f"{verdict.path}: {self._error_text(error)}",),
            )
        totals = _Totals()
        try:
            self._measure(descriptor, str(verdict.path), totals, set(), 0)
        finally:
            os.close(descriptor)
        return SizeEstimate(
            total_bytes=totals.total_bytes,
            file_count=totals.file_count,
            symlink_count=totals.symlink_count,
            complete=not totals.errors,
            errors=tuple(totals.errors),
        )

    def delete(
        self,
        kind: ResourceKind,
        game_id: str,
        path: str | Path,
        *,
        authorized: bool,
        game_running: bool = False,
    ) -> DeletionResult:
        """Delete a verified managed resource.

        ``authorized`` must be set by the calling layer after an explicit user
        confirmation. Ownership is revalidated here, immediately before the
        directory is opened, and again before the directory itself is removed.
        """
        if not authorized:
            raise OwnershipError(
                "Deleting launcher data requires explicit authorization."
            )
        if game_running:
            raise OwnershipError(
                "The game is running; its data is not deleted."
            )

        verdict = self.classify(kind, game_id, path)
        if not verdict.is_managed or verdict.path is None:
            raise OwnershipError(
                f"Refusing to delete {kind.value} data: {verdict.reason}"
            )

        try:
            descriptor = os.open(verdict.path, _OPEN_DIRECTORY)
        except OSError as error:
            raise OwnershipError(
                f"Refusing to delete {kind.value} data: the directory could "
                f"not be opened safely ({self._error_text(error)})."
            ) from error

        errors: list[str] = []
        removed = 0
        try:
            root_info = os.fstat(descriptor)
            removed += self._remove_children(
                descriptor, str(verdict.path), errors
            )
        except OSError as error:
            errors.append(f"{verdict.path}: {self._error_text(error)}")
            root_info = None
        finally:
            os.close(descriptor)

        if root_info is not None:
            try:
                current = os.lstat(verdict.path)
            except OSError as error:
                errors.append(f"{verdict.path}: {self._error_text(error)}")
            else:
                same = (
                    (current.st_dev, current.st_ino)
                    == (root_info.st_dev, root_info.st_ino)
                    and stat.S_ISDIR(current.st_mode)
                )
                if not same:
                    errors.append(
                        f"{verdict.path}: changed during deletion; the "
                        "directory itself was not removed."
                    )
                else:
                    try:
                        os.rmdir(verdict.path)
                        removed += 1
                    except OSError as error:
                        errors.append(
                            f"{verdict.path}: {self._error_text(error)}"
                        )

        deleted = not errors and not os.path.lexists(verdict.path)
        if deleted:
            LOGGER.info(
                "Deleted managed %s for game %s (%s entries)",
                kind.value,
                game_id,
                removed,
            )
        else:
            LOGGER.warning(
                "Managed %s deletion for game %s was incomplete: %s",
                kind.value,
                game_id,
                "; ".join(errors) or "directory still exists",
            )
        return DeletionResult(
            deleted=deleted, removed_entries=removed, errors=tuple(errors)
        )

    @staticmethod
    def _validate_game_id(game_id: str) -> None:
        if not isinstance(game_id, str) or not _SAFE_GAME_ID.match(game_id):
            raise OwnershipError(
                "The game ID is not usable in a managed filesystem path."
            )

    @staticmethod
    def _verdict(
        kind: ResourceKind,
        game_id: str,
        requested: Path,
        resolved: Path | None,
        expected: Path | None,
        status: OwnershipStatus,
        reason: str,
    ) -> OwnershipVerdict:
        return OwnershipVerdict(
            kind=kind,
            game_id=game_id,
            requested_path=requested,
            path=resolved,
            expected_path=expected,
            status=status,
            reason=reason,
        )

    def _measure(
        self,
        descriptor: int,
        label: str,
        totals: _Totals,
        seen: set[tuple[int, int]],
        depth: int,
    ) -> None:
        """Accumulate sizes through an open directory descriptor."""
        if depth > _MAX_DEPTH:
            totals.errors.append(f"{label}: directory nesting is too deep")
            return
        for entry in self._scan(descriptor, label, totals.errors):
            child_label = f"{label}/{entry.name}"
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as error:
                totals.errors.append(
                    f"{child_label}: {self._error_text(error)}"
                )
                continue
            if stat.S_ISLNK(info.st_mode):
                # Links are counted as entries, never traversed.
                totals.symlink_count += 1
                totals.file_count += 1
                totals.total_bytes += info.st_size
                continue
            if stat.S_ISDIR(info.st_mode):
                try:
                    child = os.open(entry.name, _OPEN_DIRECTORY, dir_fd=descriptor)
                except OSError as error:
                    totals.errors.append(
                        f"{child_label}: {self._error_text(error)}"
                    )
                    continue
                try:
                    self._measure(child, child_label, totals, seen, depth + 1)
                finally:
                    os.close(child)
                continue
            key = (info.st_dev, info.st_ino)
            if key in seen:
                continue
            seen.add(key)
            totals.file_count += 1
            totals.total_bytes += info.st_size

    def _remove_children(
        self, descriptor: int, label: str, errors: list[str]
    ) -> int:
        """Unlink every entry under an open directory without following links."""
        removed = 0
        for entry in self._scan(descriptor, label, errors):
            child_label = f"{label}/{entry.name}"
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as error:
                errors.append(f"{child_label}: {self._error_text(error)}")
                continue
            if stat.S_ISDIR(info.st_mode):
                try:
                    child = os.open(entry.name, _OPEN_DIRECTORY, dir_fd=descriptor)
                except OSError as error:
                    errors.append(f"{child_label}: {self._error_text(error)}")
                    continue
                try:
                    removed += self._remove_children(child, child_label, errors)
                finally:
                    os.close(child)
                try:
                    os.rmdir(entry.name, dir_fd=descriptor)
                    removed += 1
                except OSError as error:
                    errors.append(f"{child_label}: {self._error_text(error)}")
                continue
            try:
                # Works for regular files and symlinks; a link is removed
                # itself, never its target.
                os.unlink(entry.name, dir_fd=descriptor)
                removed += 1
            except OSError as error:
                errors.append(f"{child_label}: {self._error_text(error)}")
        return removed

    @staticmethod
    def _scan(descriptor: int, label: str, errors: list[str]) -> list[os.DirEntry]:
        try:
            with os.scandir(descriptor) as iterator:
                return list(iterator)
        except OSError as error:
            errors.append(f"{label}: {OwnershipService._error_text(error)}")
            return []

    @staticmethod
    def _error_text(error: OSError) -> str:
        return error.strerror or str(error)


__all__ = [
    "DeletionResult",
    "OwnershipService",
    "OwnershipStatus",
    "OwnershipVerdict",
    "ResourceKind",
    "SizeEstimate",
]
