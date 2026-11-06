"""Stage ISO contents safely, discover installers, and manage sessions.

The workflow is deliberately split from process launching:

1. The ISO is mounted read-only by :class:`IsoMountService`.
2. Its complete contents are copied into ``<installers>/<game-id>/source``.
3. The copy is verified against a manifest before the ISO is unmounted.
4. Installer candidates are discovered without executing anything.
5. The existing Wine launch path runs only the candidate the user selects.
6. After the installer exits, game executables are discovered read-only in the
   configured destination and the Wine prefix for explicit user confirmation.

The staging directory and the final installation directory are separate
resources. Nothing in this module deletes an existing staging directory, the
original ISO, a game installation, or a Wine prefix.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import stat
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Callable

from minimal_game_launcher.database import Database
from minimal_game_launcher.errors import (
    InstallerError,
    InstallerStagingError,
    IsoMountError,
    OwnershipError,
)
from minimal_game_launcher.models import (
    Game,
    GameDraft,
    InstallerSession,
    InstallerSourceKind,
    InstallerStatus,
    Platform,
)
from minimal_game_launcher.services.executable_discovery import (
    ExecutableCandidate,
    ExecutableDiscovery,
)
from minimal_game_launcher.services.iso_mount import IsoMountService, MountedIso
from minimal_game_launcher.services.cleanup import PROTECTING_SESSION_STATUSES
from minimal_game_launcher.services.ownership import (
    OwnershipService,
    OwnershipStatus,
    ResourceKind,
)
from minimal_game_launcher.services.prefixes import (
    assign_prefix,
    load_strategy,
    shared_reference_guard,
)
from minimal_game_launcher.settings import SettingsStore, WinePrefixStrategy

LOGGER = logging.getLogger(__name__)

MANIFEST_NAME = ".mgl-staging.json"
_CHUNK_SIZE = 1024 * 1024
_MAX_DEPTH = 64
_MAX_COPY_ENTRIES = 200_000
_MAX_DISCOVERY_ENTRIES = 50_000
_MAX_DISCOVERY_DEPTH = 16
_FREE_SPACE_MARGIN = 16 * 1024 * 1024
_OPEN_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(
    os, "O_CLOEXEC", 0
)
_OPEN_FILE = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
_SAFE_NAME = re.compile(r"^[^/\x00]+$")

ProgressCallback = Callable[[str, float], None]

# Single source of truth shared with cleanup, so the two checks cannot drift.
_PROTECTING_SESSION_STATUSES = PROTECTING_SESSION_STATUSES


class CandidateKind(StrEnum):
    """The role a discovered file may play in the installation workflow."""

    INSTALLER = "installer"
    GAME = "game"


@dataclass(frozen=True, slots=True)
class InstallerCandidate:
    """One file found in the staged contents; never executed by discovery."""

    relative_path: str
    kind: CandidateKind
    platform: Platform
    reason: str


@dataclass(frozen=True, slots=True)
class StagingSummary:
    """Verified result of a complete staging copy."""

    staging_directory: Path
    source_directory: Path
    total_bytes: int
    file_count: int
    directory_count: int
    reused: bool = False


@dataclass(frozen=True, slots=True)
class InstallationDestination:
    """A chosen final destination and whether MGL may treat it as owned.

    ``path`` is ``None`` for the installer's default Wine destination. That
    workflow installs inside the prefix, so no ``games/<game-id>`` directory is
    created or recorded as an installation directory.
    """

    path: Path | None
    owned: bool
    reason: str


@dataclass(frozen=True, slots=True)
class PendingRemovalItem:
    """Outcome for one managed resource of a removed pending install."""

    kind: ResourceKind
    path: Path
    deleted: bool = False
    missing: bool = False
    retained: bool = False
    errors: tuple[str, ...] = ()
    reason: str = ""

    @property
    def succeeded(self) -> bool:
        """Return whether the resource is gone or was already absent."""
        return self.deleted or self.missing or self.retained


@dataclass(frozen=True, slots=True)
class PendingRemovalReport:
    """Result of removing a pending installation and its files."""

    game_id: str
    game_name: str
    items: tuple[PendingRemovalItem, ...]
    session_removed: bool
    files_kept: bool = False
    errors: tuple[str, ...] = ()

    @property
    def missing(self) -> tuple[PendingRemovalItem, ...]:
        """Resources that were already absent before removal."""
        return tuple(item for item in self.items if item.missing)

    @property
    def deleted(self) -> tuple[PendingRemovalItem, ...]:
        """Resources this operation removed."""
        return tuple(item for item in self.items if item.deleted)

    @property
    def retained(self) -> tuple[PendingRemovalItem, ...]:
        """Resources deliberately left in place, such as external folders."""
        return tuple(item for item in self.items if item.retained)

    @property
    def failed(self) -> tuple[PendingRemovalItem, ...]:
        """Resources that could not be removed."""
        return tuple(item for item in self.items if not item.succeeded)

    @property
    def complete(self) -> bool:
        """Return whether the session and every managed resource are gone."""
        return self.session_removed and not self.failed and not self.errors


class InstallerManager:
    """Own ISO staging, discovery, and resumable session persistence.

    Filesystem work in :meth:`prepare` may run on a worker thread, so this
    method intentionally does not write to SQLite. The caller persists the
    returned session on the GTK main thread.
    """

    def __init__(
        self,
        database: Database,
        ownership: OwnershipService,
        mount_service: IsoMountService | None = None,
        *,
        settings_store: SettingsStore | None = None,
    ) -> None:
        self._database = database
        self._ownership = ownership
        self._mount_service = mount_service or IsoMountService()
        self._executables = ExecutableDiscovery()
        self._settings_store = settings_store

    # ------------------------------------------------------------------
    # Session persistence

    def create_session(
        self,
        iso_path: str | Path,
        game_name: str,
        *,
        game_id: str | None = None,
    ) -> InstallerSession:
        """Validate the ISO and persist a new pending ISO session."""
        name = self._validated_session_name(game_name, "ISO")
        iso = self._mount_service.validate_iso(iso_path)
        game_id = game_id or str(uuid.uuid4())
        try:
            staging = self._ownership.expected_path(
                ResourceKind.INSTALLER_SOURCE, game_id
            )
        except OwnershipError as error:
            raise InstallerError(str(error)) from error

        existing = self._database.get_installer_session(game_id)
        if existing is not None:
            raise InstallerError(
                "An installer session already exists for this game ID."
            )

        return self._persist_session(
            game_id,
            name,
            iso,
            staging,
            InstallerSourceKind.ISO,
            "ISO selected; contents have not been staged yet.",
        )

    def create_executable_session(
        self,
        executable_path: str | Path,
        game_name: str,
        *,
        game_id: str | None = None,
    ) -> InstallerSession:
        """Validate a standalone Windows installer and persist a pending session.

        The selected executable is only referenced here. It is copied into
        MGL-managed staging by :meth:`prepare` and is never moved, modified, or
        deleted. A matching MZ signature is a file-format check, not evidence
        that the file is safe, authentic, or a working installer.
        """
        name = self._validated_session_name(game_name, "executable")
        source = self.validate_executable_source(executable_path)
        game_id = game_id or str(uuid.uuid4())
        try:
            staging = self._ownership.expected_path(
                ResourceKind.INSTALLER_SOURCE, game_id
            )
        except OwnershipError as error:
            raise InstallerError(str(error)) from error

        existing = self._database.get_installer_session(game_id)
        if existing is not None:
            raise InstallerError(
                "An installer session already exists for this game ID."
            )

        return self._persist_session(
            game_id,
            name,
            source,
            staging,
            InstallerSourceKind.EXECUTABLE,
            "Installer executable selected; it has not been staged yet.",
        )

    @staticmethod
    def _validated_session_name(game_name: str, source_label: str) -> str:
        name = game_name.strip()
        if not name:
            raise InstallerError(
                f"A game name is required for an {source_label} installation."
            )
        if len(name) > 200:
            raise InstallerError("The game name must be 200 characters or fewer.")
        return name

    def validate_executable_source(self, executable_path: str | Path) -> Path:
        """Validate a standalone installer executable without staging it.

        Symbolic links are resolved to their regular-file target, and the
        resolved path is what the session stores. Sources inside MGL-managed
        roots are refused so a later copy cannot collide with owned data.
        """
        raw = Path(executable_path).expanduser()
        try:
            resolved = raw.resolve(strict=True)
        except OSError as error:
            raise InstallerError(
                f"The installer executable was not found: {raw}"
            ) from error
        try:
            info = os.lstat(resolved)
        except OSError as error:
            raise InstallerError(
                f"The installer executable could not be inspected: {resolved}"
            ) from error
        if not stat.S_ISREG(info.st_mode):
            raise InstallerError(
                f"The installer executable is not a regular file: {resolved}"
            )
        if info.st_size <= 0:
            raise InstallerError(
                f"The installer executable is empty: {resolved}"
            )
        if not os.access(resolved, os.R_OK):
            raise InstallerError(
                f"Permission denied while reading the installer executable: "
                f"{resolved}"
            )
        if resolved.suffix.casefold() != ".exe":
            raise InstallerError(
                "Standalone installers must be Windows .exe files."
            )
        if not self._has_mz_signature(resolved):
            raise InstallerError(
                "The selected file does not start with the Windows MZ "
                "signature, so it is not used as an installer executable."
            )
        self._reject_managed_source(resolved)
        return resolved

    @staticmethod
    def _has_mz_signature(path: Path) -> bool:
        try:
            descriptor = os.open(path, _OPEN_FILE)
        except OSError:
            return False
        try:
            return os.read(descriptor, 2) == b"MZ"
        except OSError:
            return False
        finally:
            os.close(descriptor)

    def _reject_managed_source(self, source: Path) -> None:
        """Refuse a source inside MGL-managed staging, game, or prefix roots."""
        for kind in (
            ResourceKind.INSTALLER_SOURCE,
            ResourceKind.INSTALLATION,
            ResourceKind.PREFIX,
            ResourceKind.SHARED_PREFIX,
        ):
            root = self._ownership.root_for(kind)
            try:
                resolved_root = root.resolve(strict=False)
            except (OSError, RuntimeError):
                resolved_root = root
            if source == resolved_root or source.is_relative_to(resolved_root):
                raise InstallerError(
                    "The installer executable is inside an MGL-managed "
                    f"directory and is not used: {source}"
                )

    def _persist_session(
        self,
        game_id: str,
        name: str,
        source: Path,
        staging: Path,
        source_kind: InstallerSourceKind,
        message: str,
    ) -> InstallerSession:
        """Assign a prefix and persist a new session.

        Only a shared-prefix assignment needs the lifecycle lock. The check and
        the insert share one locked section so deletion cannot run between them.
        """
        now = _utc_now()
        prefixes_root = self._ownership.root_for(ResourceKind.PREFIX)
        strategy = load_strategy(self._settings_store)
        with shared_reference_guard(
            prefixes_root, strategy is WinePrefixStrategy.SHARED
        ):
            return self._insert_session(
                game_id,
                name,
                source,
                staging,
                source_kind,
                message,
                now,
                prefixes_root,
                strategy,
            )

    def _insert_session(
        self,
        game_id: str,
        name: str,
        source: Path,
        staging: Path,
        source_kind: InstallerSourceKind,
        message: str,
        now: str,
        prefixes_root: Path,
        strategy: WinePrefixStrategy,
    ) -> InstallerSession:
        """Assign and persist a new session; the caller holds any needed lock."""
        if self._database.get_installer_session(game_id) is not None:
            raise InstallerError(
                "An installer session already exists for this game ID."
            )
        assignment = assign_prefix(prefixes_root, game_id, strategy)
        session = InstallerSession(
            game_id=game_id,
            game_name=name,
            iso_path=str(source),
            staging_directory=str(staging),
            installer_relative_path=None,
            install_directory=None,
            install_directory_owned=False,
            status=InstallerStatus.PENDING,
            message=message,
            created_at=now,
            updated_at=now,
            wine_prefix_path=assignment.path,
            wine_prefix_shared=assignment.shared,
            source_kind=source_kind,
        )
        self._database.save_installer_session(session)
        return session

    def save_session(self, session: InstallerSession) -> InstallerSession:
        """Persist a session from the main thread and return the update.

        A session that references the shared prefix is written under the
        lifecycle lock, so deletion cannot validate and remove the prefix between
        the reference being decided and it reaching the database. Per-game
        sessions do not take the lock.
        """
        updated = replace(session, updated_at=_utc_now())
        stored = self._database.get_installer_session(updated.game_id)
        already_persisted = (
            stored is not None
            and stored.wine_prefix_shared
            and stored.wine_prefix_path == updated.wine_prefix_path
        )
        with shared_reference_guard(
            self._ownership.root_for(ResourceKind.PREFIX),
            updated.wine_prefix_shared,
            new_reference=not already_persisted,
        ):
            self._database.save_installer_session(updated)
        return updated

    def get_session(self, game_id: str) -> InstallerSession | None:
        """Return one persisted session."""
        return self._database.get_installer_session(game_id)

    def list_sessions(self, *, include_installed: bool = False) -> list[InstallerSession]:
        """Return resumable sessions, newest first."""
        sessions = self._database.list_installer_sessions()
        if include_installed:
            return sessions
        return [
            session
            for session in sessions
            if session.status is not InstallerStatus.INSTALLED
        ]

    # ------------------------------------------------------------------
    # Mounting and staging

    def prepare(
        self,
        session: InstallerSession,
        *,
        progress: ProgressCallback | None = None,
    ) -> InstallerSession:
        """Stage the selected source and verify the managed copy.

        ISO sessions keep the existing mount/copy/verify/unmount sequence.
        Executable sessions copy only the selected file and never mount or
        unmount anything.
        """
        if session.source_kind is InstallerSourceKind.EXECUTABLE:
            return self._prepare_executable(session, progress=progress)
        return self._prepare_iso(session, progress=progress)

    def _prepare_iso(
        self,
        session: InstallerSession,
        *,
        progress: ProgressCallback | None = None,
    ) -> InstallerSession:
        """Mount the ISO, copy all contents, verify, and unmount.

        Any failure after creating the staging directory removes only that
        newly created directory. An existing staging directory is never altered.
        """
        staging = self._prepare_staging_area(session)
        mounted = self._mount_service.mount(session.source_path)
        created = False
        try:
            self._report(progress, "Checking free space…", 0.02)
            self._copy_and_verify(
                session, mounted, staging, progress=progress
            )
            created = True
        except BaseException:
            if created or os.path.lexists(staging):
                self._cleanup_failed_staging(session, staging)
            raise
        finally:
            try:
                self._mount_service.unmount(mounted)
            except IsoMountError:
                LOGGER.exception("Could not release the ISO mount after staging")

        return replace(
            session,
            status=InstallerStatus.STAGED,
            staging_directory=str(staging),
            message="Complete ISO contents copied and verified. The ISO is unmounted.",
        )

    def _prepare_executable(
        self,
        session: InstallerSession,
        *,
        progress: ProgressCallback | None = None,
    ) -> InstallerSession:
        """Copy only the selected standalone installer and verify it.

        The source is revalidated immediately before copying. A change to its
        identity, size, or modification time observed after the copy fails the
        operation instead of accepting a moving target.
        """
        source = self.validate_executable_source(session.source_path)
        staging = self._prepare_staging_area(session)
        created = False
        try:
            self._report(progress, "Checking free space…", 0.05)
            try:
                before = os.lstat(source)
            except OSError as error:
                raise InstallerStagingError(
                    f"The installer executable could not be inspected: {source}"
                ) from error
            free = shutil.disk_usage(staging.parent).free
            if before.st_size + _FREE_SPACE_MARGIN > free:
                raise InstallerStagingError(
                    "Not enough free disk space to stage the installer "
                    f"executable. Required: {_human_size(before.st_size)}; "
                    f"available: {_human_size(free)}."
                )

            os.mkdir(staging, 0o700)
            created = True
            try:
                os.chmod(staging, 0o700)
            except OSError:
                LOGGER.warning("Could not set permissions on %s", staging)
            if not self._ownership.classify(
                ResourceKind.INSTALLER_SOURCE, session.game_id, staging
            ).is_managed:
                raise InstallerStagingError(
                    f"The staging directory is not a verified managed path: {staging}"
                )

            source_dir = staging / "source"
            os.mkdir(source_dir, 0o700)
            self._report(progress, f"Copying {source.name}…", 0.2)
            digest = self._copy_installer_file(source, source_dir)
            self._assert_source_unchanged(source, before)

            manifest = {
                "version": 1,
                "iso_path": str(source),
                "source_kind": InstallerSourceKind.EXECUTABLE.value,
                "source_path": str(source),
                "files": [
                    {
                        "path": source.name,
                        "size": before.st_size,
                        "sha256": digest,
                    }
                ],
                "directories": [],
            }
            self._write_manifest(staging, manifest)
            self._report(progress, "Verifying staged copy…", 0.9)
            self._verify_tree(staging, source_dir, manifest)
        except BaseException:
            if created:
                self._cleanup_failed_staging(session, staging)
            raise

        return replace(
            session,
            status=InstallerStatus.STAGED,
            staging_directory=str(staging),
            installer_relative_path=source.name,
            message=(
                "Installer executable copied and verified. "
                "The original file is kept."
            ),
        )

    def _prepare_staging_area(self, session: InstallerSession) -> Path:
        """Return the staging path after preparing and checking its root."""
        staging = self._staging_root(session)
        installer_root = self._ownership.root_for(ResourceKind.INSTALLER_SOURCE)
        try:
            installer_root.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise InstallerStagingError(
                f"The installer staging root could not be created: {installer_root}"
            ) from error

        if os.path.lexists(staging):
            raise InstallerStagingError(
                f"An installer staging directory already exists: {staging}\n\n"
                "It is not removed automatically. Resume that session or remove "
                "it explicitly before starting a new staging copy."
            )
        return staging

    def _copy_installer_file(self, source: Path, destination: Path) -> str:
        """Copy one regular file into an existing directory and hash it."""
        try:
            source_fd = os.open(source.parent, _OPEN_DIRECTORY)
        except OSError as error:
            raise InstallerStagingError(
                f"The installer executable could not be opened safely: {source}"
            ) from error
        try:
            try:
                destination_fd = os.open(destination, _OPEN_DIRECTORY)
            except OSError as error:
                raise InstallerStagingError(
                    f"The staged source directory could not be opened: {destination}"
                ) from error
            try:
                return self._copy_file(
                    source_fd, destination_fd, source.name, source.name
                )
            finally:
                os.close(destination_fd)
        finally:
            os.close(source_fd)

    @staticmethod
    def _assert_source_unchanged(source: Path, before: os.stat_result) -> None:
        """Refuse a copy whose source changed identity while it was read."""
        try:
            after = os.lstat(source)
        except OSError as error:
            raise InstallerStagingError(
                f"The installer executable disappeared while it was copied: {source}"
            ) from error
        if (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) != (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ):
            raise InstallerStagingError(
                "The installer executable changed while it was copied; the "
                "staged copy was discarded."
            )

    def verify_staged(self, session: InstallerSession) -> StagingSummary:
        """Verify an existing staged copy against its manifest.

        This is used to resume a deferred session without mounting or copying
        the ISO again. It never repairs or deletes staged data.
        """
        staging = self._staging_root(session)
        manifest = self._read_manifest(staging)
        source = staging / "source"
        if not source.is_dir():
            raise InstallerStagingError(
                f"The staged source directory is missing: {source}"
            )
        summary = self._verify_tree(staging, source, manifest)
        return StagingSummary(
            staging_directory=staging,
            source_directory=source,
            total_bytes=sum(entry["size"] for entry in manifest["files"]),
            file_count=summary[0],
            directory_count=summary[1],
            reused=True,
        )

    def staging_summary(self, session: InstallerSession) -> StagingSummary:
        """Return verified staging details for a completed session."""
        return self.verify_staged(session)

    def _copy_and_verify(
        self,
        session: InstallerSession,
        mounted: MountedIso,
        staging: Path,
        *,
        progress: ProgressCallback | None,
    ) -> None:
        source = mounted.mount_point
        try:
            source_info = os.lstat(source)
        except OSError as error:
            raise InstallerStagingError(
                f"The mounted ISO could not be inspected: {source}"
            ) from error
        if not stat.S_ISDIR(source_info.st_mode):
            raise InstallerStagingError(
                f"The mounted ISO is not a directory: {source}"
            )

        try:
            source_fd = os.open(source, _OPEN_DIRECTORY)
        except OSError as error:
            raise InstallerStagingError(
                f"The mounted ISO could not be opened safely: {source}"
            ) from error
        try:
            self._report(progress, "Measuring ISO contents…", 0.05)
            totals = _Totals()
            self._scan_sizes(source_fd, totals, 0)

            free = shutil.disk_usage(staging.parent).free
            if totals.bytes + _FREE_SPACE_MARGIN > free:
                raise InstallerStagingError(
                    "Not enough free disk space to stage the ISO contents. "
                    f"Required: {_human_size(totals.bytes)}; "
                    f"available: {_human_size(free)}."
                )

            os.mkdir(staging, 0o700)
            try:
                os.chmod(staging, 0o700)
            except OSError:
                LOGGER.warning("Could not set permissions on %s", staging)
            if not self._ownership.classify(
                ResourceKind.INSTALLER_SOURCE, session.game_id, staging
            ).is_managed:
                raise InstallerStagingError(
                    f"The staging directory is not a verified managed path: {staging}"
                )

            source_dir = staging / "source"
            os.mkdir(source_dir, 0o700)
            destination_fd = os.open(source_dir, _OPEN_DIRECTORY)
            try:
                files: list[dict[str, object]] = []
                directories: list[str] = []
                copy_totals = _Totals()
                try:
                    self._copy_directory(
                        source_fd,
                        destination_fd,
                        "",
                        files,
                        directories,
                        copy_totals,
                        totals.bytes,
                        progress,
                        0,
                    )
                finally:
                    os.close(destination_fd)
            except BaseException:
                raise

            manifest = {
                "version": 1,
                "iso_path": str(mounted.iso_path),
                "files": files,
                "directories": directories,
            }
            self._write_manifest(staging, manifest)
            self._report(progress, "Verifying staged copy…", 0.92)
            self._verify_tree(staging, source_dir, manifest)
            self._report(progress, "Copy verified; unmounting ISO…", 1.0)
        finally:
            os.close(source_fd)

    def _cleanup_failed_staging(self, session: InstallerSession, staging: Path) -> None:
        """Remove only the staging directory created by this operation."""
        try:
            result = self._ownership.delete(
                ResourceKind.INSTALLER_SOURCE,
                session.game_id,
                staging,
                authorized=True,
            )
        except OwnershipError:
            LOGGER.exception("Could not remove failed staging directory %s", staging)
            return
        if not result.deleted:
            LOGGER.warning(
                "Failed staging cleanup was incomplete for %s: %s",
                staging,
                "; ".join(result.errors),
            )

    # ------------------------------------------------------------------
    # Discovery

    def discover(self, session: InstallerSession) -> list[InstallerCandidate]:
        """Return potential installers and game executables without running them."""
        if session.source_kind is InstallerSourceKind.EXECUTABLE:
            return self._discover_direct(session)

        staging = self._staging_root(session)
        source = staging / "source"
        try:
            source_fd = os.open(source, _OPEN_DIRECTORY)
        except OSError as error:
            raise InstallerStagingError(
                f"The staged contents could not be opened: {source}"
            ) from error

        candidates: list[InstallerCandidate] = []
        counter = _Counter()
        try:
            self._discover_directory(source_fd, "", candidates, counter, 0)
        finally:
            os.close(source_fd)

        candidates.sort(
            key=lambda candidate: (
                0 if candidate.kind is CandidateKind.INSTALLER else 1,
                candidate.relative_path.count("/"),
                candidate.relative_path.casefold(),
            )
        )
        return candidates

    def _discover_direct(self, session: InstallerSession) -> list[InstallerCandidate]:
        """Return the user-selected staged executable as the only candidate.

        No name-based ISO heuristics apply: the user already chose this file.
        The reason still states that MGL has not checked installer safety.
        """
        relative = session.installer_relative_path
        if relative is None:
            source = self._staging_root(session) / "source"
            try:
                with os.scandir(source) as iterator:
                    names = sorted(
                        entry.name
                        for entry in iterator
                        if entry.is_file(follow_symlinks=False)
                    )
            except OSError:
                return []
            if len(names) != 1:
                return []
            relative = names[0]
        try:
            self._candidate_path(session, relative)
        except InstallerError:
            return []
        return [
            InstallerCandidate(
                relative_path=relative,
                kind=CandidateKind.INSTALLER,
                platform=Platform.WINDOWS,
                reason=(
                    "User-selected installer executable. MGL copied it but did "
                    "not check whether it is safe or a working installer."
                ),
            )
        ]

    def _discover_directory(
        self,
        descriptor: int,
        prefix: str,
        candidates: list[InstallerCandidate],
        counter: _Counter,
        depth: int,
    ) -> None:
        if depth > _MAX_DISCOVERY_DEPTH:
            return
        try:
            with os.scandir(descriptor) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except OSError as error:
            raise InstallerStagingError(
                f"Could not scan staged contents: {error.strerror or error}"
            ) from error

        for entry in entries:
            counter.count += 1
            if counter.count > _MAX_DISCOVERY_ENTRIES:
                raise InstallerStagingError(
                    "The staged contents contain too many entries to scan safely."
                )
            relative = f"{prefix}/{entry.name}" if prefix else entry.name
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISLNK(info.st_mode):
                # Discovery never follows links; copying already rejects them.
                continue
            if stat.S_ISDIR(info.st_mode):
                try:
                    child = os.open(entry.name, _OPEN_DIRECTORY, dir_fd=descriptor)
                except OSError:
                    continue
                try:
                    self._discover_directory(
                        child, relative, candidates, counter, depth + 1
                    )
                finally:
                    os.close(child)
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            candidate = self._classify_candidate(descriptor, entry.name, relative)
            if candidate is not None:
                candidates.append(candidate)

    def _classify_candidate(
        self, descriptor: int, name: str, relative: str
    ) -> InstallerCandidate | None:
        lowered = name.casefold()
        stem = lowered.rsplit(".", 1)[0] if "." in lowered else lowered
        if lowered.endswith(".exe"):
            installer_like = (
                lowered in {"setup.exe", "install.exe"}
                or "setup" in stem
                or stem.startswith("install")
            )
            if installer_like:
                return InstallerCandidate(
                    relative_path=relative,
                    kind=CandidateKind.INSTALLER,
                    platform=Platform.WINDOWS,
                    reason="Windows executable with an installer-like name.",
                )
            return InstallerCandidate(
                relative_path=relative,
                kind=CandidateKind.GAME,
                platform=Platform.WINDOWS,
                reason="Windows executable; it may be a game or helper.",
            )

        if lowered in {"setup.sh", "install.sh"} or lowered.endswith(
            (".run", ".appimage")
        ):
            return InstallerCandidate(
                relative_path=relative,
                kind=CandidateKind.INSTALLER,
                platform=Platform.LINUX,
                reason="Linux executable with an installer-like name.",
            )

        if self._is_elf(descriptor, name):
            return InstallerCandidate(
                relative_path=relative,
                kind=CandidateKind.GAME,
                platform=Platform.LINUX,
                reason="Linux ELF executable; it may be a game or helper.",
            )
        return None

    @staticmethod
    def _is_elf(descriptor: int, name: str) -> bool:
        """Read only the magic bytes needed to recognize an ELF file."""
        try:
            child = os.open(name, _OPEN_FILE, dir_fd=descriptor)
        except OSError:
            return False
        try:
            return os.read(child, 4) == b"\x7fELF"
        except OSError:
            return False
        finally:
            os.close(child)

    def select_installer(
        self, session: InstallerSession, relative_path: str
    ) -> InstallerSession:
        """Validate and persist the user's explicit installer choice."""
        candidates = {
            candidate.relative_path: candidate
            for candidate in self.discover(session)
        }
        candidate = candidates.get(relative_path)
        if candidate is None:
            raise InstallerError(
                "The selected file is not a discovered installer candidate."
            )
        if candidate.kind is not CandidateKind.INSTALLER:
            raise InstallerError(
                "The selected file is a possible game executable, not an installer."
            )
        # Resolve it now so a later launch cannot drift to another path.
        self._candidate_path(session, relative_path)
        return replace(
            session,
            installer_relative_path=relative_path,
            status=InstallerStatus.STAGED,
            message=f"Selected installer: {relative_path}",
        )

    def _candidate_path(
        self, session: InstallerSession, relative_path: str
    ) -> Path:
        parts = _safe_relative_parts(relative_path)
        root = self._staging_root(session) / "source"
        current = root
        for part in parts:
            current = current / part
            try:
                info = os.lstat(current)
            except OSError as error:
                raise InstallerError(
                    f"The selected installer was not found: {relative_path}"
                ) from error
            if stat.S_ISLNK(info.st_mode):
                raise InstallerError(
                    "The selected installer is a symbolic link; links are never used."
                )
        if not current.is_file():
            raise InstallerError(
                f"The selected installer is not a regular file: {relative_path}"
            )
        return current

    def installer_game(self, session: InstallerSession) -> Game:
        """Build the temporary Windows game record used to run an installer.

        The record is not inserted into the library. It reserves the same stable
        game ID whose managed prefix and final library row will be used later.
        """
        if not session.installer_relative_path:
            raise InstallerError("No installer has been selected.")
        installer = self._candidate_path(session, session.installer_relative_path)
        prefix = self.prefix_path(session)
        now = _utc_now()
        return Game(
            id=session.game_id,
            name=session.game_name,
            platform=Platform.WINDOWS,
            executable=str(installer),
            working_directory=str(installer.parent),
            launch_arguments="",
            cover_path=None,
            wine_prefix_path=str(prefix),
            gamemode_enabled=False,
            mangohud_enabled=False,
            created_at=session.created_at,
            updated_at=now,
            environment={},
            wine_prefix_shared=session.wine_prefix_shared,
        )

    def prefix_path(self, session: InstallerSession) -> Path:
        """Return the stable Wine prefix path for an installer session."""
        if session.wine_prefix_path and session.wine_prefix_path.strip():
            return Path(session.wine_prefix_path).expanduser()
        # Sessions created before prefix metadata was stored were per-game.
        return self._ownership.expected_path(
            ResourceKind.PREFIX, session.game_id
        )

    def discover_game_executables(
        self, session: InstallerSession
    ) -> list[ExecutableCandidate]:
        """Return read-only, ranked executable candidates for a finished install.

        The configured installation directory is searched first, then the
        prefix's ``Program Files``, ``Program Files (x86)``, and other
        ``drive_c`` children. Nothing is executed or modified.
        """
        return self._executables.discover(
            session.game_name,
            installation_directory=session.install_directory,
            prefix_path=self.prefix_path(session),
        )

    def installation_draft(
        self,
        session: InstallerSession,
        executable: str | Path,
        existing: Game | None = None,
    ) -> GameDraft:
        """Build a registration draft that preserves an existing launch setup.

        For a fresh install the launcher defaults apply. For a re-registered
        game, the stored arguments, environment, cover, GameMode, and MangoHud
        settings are kept; only the executable and its parent working directory
        are replaced.
        """
        path = Path(executable).expanduser()
        if existing is not None:
            return GameDraft(
                name=existing.name,
                platform=Platform.WINDOWS,
                executable=str(path),
                working_directory=str(path.parent),
                launch_arguments=existing.launch_arguments,
                environment="\n".join(
                    f"{key}={value}" for key, value in existing.environment.items()
                ),
                cover_path=existing.cover_path,
                gamemode_enabled=existing.gamemode_enabled,
                mangohud_enabled=existing.mangohud_enabled,
            )
        return GameDraft(
            name=session.game_name,
            platform=Platform.WINDOWS,
            executable=str(path),
            working_directory=str(path.parent),
            launch_arguments="",
            environment="",
            cover_path=None,
            gamemode_enabled=True,
            mangohud_enabled=False,
        )

    # ------------------------------------------------------------------
    # Pending-install removal

    def session_resources(
        self, session: InstallerSession
    ) -> list[tuple[ResourceKind, Path]]:
        """Return the managed resources a pending install may own.

        An external destination is never included: MGL does not own it and
        must not delete it. A Wine-default install has no destination at all.
        """
        resources: list[tuple[ResourceKind, Path]] = [
            (
                ResourceKind.INSTALLER_SOURCE,
                self._ownership.expected_path(
                    ResourceKind.INSTALLER_SOURCE, session.game_id
                ),
            ),
        ]
        # A shared prefix serves other games too, so no single pending install
        # may delete it. It stays on disk and is cleaned up as one resource.
        if not session.wine_prefix_shared:
            resources.append(
                (
                    ResourceKind.PREFIX,
                    self._ownership.expected_path(
                        ResourceKind.PREFIX, session.game_id
                    ),
                )
            )
        if session.install_directory and session.install_directory_owned:
            resources.append(
                (
                    ResourceKind.INSTALLATION,
                    self._ownership.expected_path(
                        ResourceKind.INSTALLATION, session.game_id
                    ),
                )
            )
        return resources

    def remove_pending_install(
        self,
        session: InstallerSession,
        *,
        authorized: bool,
        game_running: bool = False,
        delete_files: bool = True,
        delete_session: bool = True,
    ) -> PendingRemovalReport:
        """Delete a pending install's managed files and its session row.

        With ``delete_files=False`` only the session row is removed and every
        managed file stays on disk for the user to clean up later. With
        ``delete_session=False`` the row is left alone; the caller must then run
        :meth:`discard_pending_session` on the database thread.

        Each resource is revalidated through the ownership service immediately
        before deletion. A resource that is already missing counts as success:
        the user's intent is that the pending install no longer exists, and its
        record can still be removed. A failure keeps the session so the user can
        retry. External destinations are reported as retained and untouched.
        """
        if not authorized:
            raise InstallerError(
                "Removing a pending install requires explicit authorization."
            )
        if game_running:
            raise InstallerError(
                "The installer is still running; its pending install was not "
                "removed."
            )

        items: list[PendingRemovalItem] = []
        errors: list[str] = []
        if delete_files:
            for kind, path in self.session_resources(session):
                item = self._remove_resource(kind, session, path)
                items.append(item)
                errors.extend(item.errors)

            if session.install_directory and not session.install_directory_owned:
                items.append(
                    PendingRemovalItem(
                        kind=ResourceKind.INSTALLATION,
                        path=Path(session.install_directory).expanduser(),
                        retained=True,
                    )
                )

        session_removed = False
        if delete_session and not errors:
            try:
                self._database.delete_installer_session(session.game_id)
                session_removed = True
            except Exception as error:  # pragma: no cover - defensive
                LOGGER.exception(
                    "Could not delete installer session %s", session.game_id
                )
                errors.append(f"The session record could not be removed: {error}")

        report = PendingRemovalReport(
            game_id=session.game_id,
            game_name=session.game_name,
            items=tuple(items),
            session_removed=session_removed,
            files_kept=not delete_files,
            errors=tuple(errors),
        )
        if delete_session:
            self._log_removal(report)
        return report

    def discard_pending_session(self, session: InstallerSession) -> bool:
        """Delete only the session row.

        The SQLite connection is thread-bound, so this must run on the thread
        that owns the database (the GTK main thread), never on a worker.
        """
        try:
            self._database.delete_installer_session(session.game_id)
        except Exception:
            LOGGER.exception(
                "Could not delete installer session %s", session.game_id
            )
            return False
        return True

    def _log_removal(self, report: PendingRemovalReport) -> None:
        if report.complete:
            LOGGER.info(
                "Removed pending install %s (%s)", report.game_name, report.game_id
            )
            return
        LOGGER.warning(
            "Pending install removal for %s was incomplete: %s",
            report.game_id,
            "; ".join(report.errors) or "see per-resource outcomes",
        )

    def _remove_resource(
        self, kind: ResourceKind, session: InstallerSession, path: Path
    ) -> PendingRemovalItem:
        verdict = self._ownership.classify(kind, session.game_id, path)
        if verdict.status is OwnershipStatus.MISSING or not os.path.lexists(path):
            return PendingRemovalItem(kind=kind, path=path, missing=True)
        if not verdict.is_managed:
            return PendingRemovalItem(
                kind=kind, path=path, errors=(verdict.reason,)
            )
        try:
            result = self._ownership.delete(
                kind, session.game_id, path, authorized=True
            )
        except (OwnershipError, OSError) as error:
            return PendingRemovalItem(kind=kind, path=path, errors=(str(error),))
        return PendingRemovalItem(
            kind=kind,
            path=path,
            deleted=result.deleted,
            errors=result.errors,
        )

    # ------------------------------------------------------------------
    # Destinations

    def resolve_destination(
        self,
        session: InstallerSession,
        *,
        managed: bool,
        external_path: str | Path | None = None,
        create: bool,
        wine_default: bool = False,
    ) -> InstallationDestination:
        """Validate a final destination without ever claiming an external path.

        Managed destinations are exactly ``<games>/<game-id>``. External paths
        are allowed only outside the managed games root and are always reported
        as non-owned. Existing non-empty destinations are refused. The Wine
        default destination creates nothing and records no installation path.
        """
        expected = self._ownership.expected_path(
            ResourceKind.INSTALLATION, session.game_id
        )
        staging = self._staging_root(session)
        source = Path(session.source_path).expanduser()

        if wine_default:
            if managed or external_path is not None:
                raise InstallerError(
                    "The Wine default destination cannot also be a managed or "
                    "external folder."
                )
            return InstallationDestination(
                path=None,
                owned=False,
                reason="Installer default inside the Wine prefix.",
            )

        if managed:
            destination = expected
            owned = True
            reason = "MGL-managed game directory."
        else:
            if external_path is None or not str(external_path).strip():
                raise InstallerError(
                    "Choose an external installation destination or use the "
                    "MGL-managed destination."
                )
            raw = Path(external_path).expanduser()
            if any(part == ".." for part in raw.parts):
                raise InstallerError(
                    "The external destination contains a parent-directory segment."
                )
            try:
                destination = raw.resolve(strict=False)
            except (OSError, RuntimeError) as error:
                raise InstallerError(
                    f"The external destination could not be resolved: {raw}"
                ) from error

            games_root = self._ownership.root_for(ResourceKind.INSTALLATION)
            try:
                resolved_root = games_root.resolve(strict=False)
            except (OSError, RuntimeError):
                resolved_root = games_root
            if destination == expected.resolve(strict=False):
                destination = expected
                owned = True
                reason = "MGL-managed game directory."
            elif destination.is_relative_to(resolved_root):
                raise InstallerError(
                    "A destination inside the MGL-managed games root must be "
                    f"exactly the game directory for this ID: {expected}"
                )
            else:
                owned = False
                reason = "External destination; MGL will not delete it."

        if destination == source:
            label = "ISO" if session.source_kind_is_iso else "installer source"
            raise InstallerError(
                f"The installation destination cannot be the {label}."
            )
        if destination == staging or destination.is_relative_to(staging):
            raise InstallerError(
                "The installation destination cannot be inside installer staging."
            )

        exists = os.path.lexists(destination)
        if exists:
            try:
                info = os.lstat(destination)
            except OSError as error:
                raise InstallerError(
                    f"The installation destination could not be inspected: {destination}"
                ) from error
            if stat.S_ISLNK(info.st_mode):
                raise InstallerError(
                    "The installation destination is a symbolic link and is "
                    "not used."
                )
            if not stat.S_ISDIR(info.st_mode):
                raise InstallerError(
                    f"The installation destination is not a directory: {destination}"
                )
            if _directory_has_entries(destination):
                raise InstallerError(
                    "The installation destination is not empty. Choose a new "
                    "directory so an existing installation is not overwritten."
                )
        elif create:
            try:
                destination.mkdir(mode=0o700, parents=True, exist_ok=True)
            except OSError as error:
                raise InstallerError(
                    f"The installation destination could not be created: {destination}"
                ) from error

        if owned and create:
            verdict = self._ownership.classify(
                ResourceKind.INSTALLATION, session.game_id, destination
            )
            if not verdict.is_managed:
                raise InstallerError(
                    "The managed destination could not be verified: "
                    f"{verdict.reason}"
                )
        return InstallationDestination(
            path=destination, owned=owned, reason=reason
        )

    # ------------------------------------------------------------------
    # Manifest and verification internals

    def _staging_root(self, session: InstallerSession) -> Path:
        try:
            expected = self._ownership.expected_path(
                ResourceKind.INSTALLER_SOURCE, session.game_id
            )
        except OwnershipError as error:
            raise InstallerStagingError(str(error)) from error
        recorded = Path(session.staging_directory).expanduser()
        if recorded != expected:
            raise InstallerStagingError(
                "The stored staging directory does not match this game ID's "
                "managed path."
            )
        return expected

    def _scan_sizes(self, descriptor: int, totals: "_Totals", depth: int) -> None:
        if depth > _MAX_DEPTH:
            raise InstallerStagingError(
                "The ISO directory structure is nested too deeply to copy safely."
            )
        try:
            with os.scandir(descriptor) as iterator:
                entries = list(iterator)
        except OSError as error:
            raise InstallerStagingError(
                f"Could not read ISO contents: {error.strerror or error}"
            ) from error
        for entry in entries:
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as error:
                raise InstallerStagingError(
                    f"Could not inspect {entry.name}: {error.strerror or error}"
                ) from error
            if stat.S_ISLNK(info.st_mode):
                raise InstallerStagingError(
                    f"The ISO contains a symbolic link, which is not copied: "
                    f"{entry.name}"
                )
            if stat.S_ISDIR(info.st_mode):
                try:
                    child = os.open(entry.name, _OPEN_DIRECTORY, dir_fd=descriptor)
                except OSError as error:
                    raise InstallerStagingError(
                        f"Could not open ISO directory {entry.name}: "
                        f"{error.strerror or error}"
                    ) from error
                try:
                    self._scan_sizes(child, totals, depth + 1)
                finally:
                    os.close(child)
                continue
            if not stat.S_ISREG(info.st_mode):
                raise InstallerStagingError(
                    f"The ISO contains an unsupported special file: {entry.name}"
                )
            totals.files += 1
            if totals.files > _MAX_COPY_ENTRIES:
                raise InstallerStagingError(
                    "The ISO contains too many files to stage safely."
                )
            totals.bytes += info.st_size

    def _copy_directory(
        self,
        source_fd: int,
        destination_fd: int,
        prefix: str,
        files: list[dict[str, object]],
        directories: list[str],
        totals: "_Totals",
        expected_bytes: int,
        progress: ProgressCallback | None,
        depth: int,
    ) -> None:
        if depth > _MAX_DEPTH:
            raise InstallerStagingError(
                "The ISO directory structure is nested too deeply to copy safely."
            )
        try:
            with os.scandir(source_fd) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except OSError as error:
            raise InstallerStagingError(
                f"Could not read ISO contents: {error.strerror or error}"
            ) from error

        for entry in entries:
            if not _SAFE_NAME.fullmatch(entry.name):
                raise InstallerStagingError(
                    f"The ISO contains an unsafe file name: {entry.name!r}"
                )
            relative = f"{prefix}/{entry.name}" if prefix else entry.name
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as error:
                raise InstallerStagingError(
                    f"Could not inspect {relative}: {error.strerror or error}"
                ) from error

            if stat.S_ISLNK(info.st_mode):
                raise InstallerStagingError(
                    f"The ISO contains a symbolic link, which is not followed: "
                    f"{relative}"
                )
            if stat.S_ISDIR(info.st_mode):
                try:
                    os.mkdir(entry.name, 0o700, dir_fd=destination_fd)
                except OSError as error:
                    raise InstallerStagingError(
                        f"Could not create staged directory {relative}: "
                        f"{error.strerror or error}"
                    ) from error
                try:
                    source_child = os.open(
                        entry.name, _OPEN_DIRECTORY, dir_fd=source_fd
                    )
                except OSError as error:
                    raise InstallerStagingError(
                        f"Could not open ISO directory {relative}: "
                        f"{error.strerror or error}"
                    ) from error
                try:
                    destination_child = os.open(
                        entry.name, _OPEN_DIRECTORY, dir_fd=destination_fd
                    )
                except OSError as error:
                    os.close(source_child)
                    raise InstallerStagingError(
                        f"Could not open staged directory {relative}: "
                        f"{error.strerror or error}"
                    ) from error
                try:
                    directories.append(relative)
                    self._copy_directory(
                        source_child,
                        destination_child,
                        relative,
                        files,
                        directories,
                        totals,
                        expected_bytes,
                        progress,
                        depth + 1,
                    )
                finally:
                    os.close(destination_child)
                    os.close(source_child)
                continue

            if not stat.S_ISREG(info.st_mode):
                raise InstallerStagingError(
                    f"The ISO contains an unsupported special file: {relative}"
                )

            digest = self._copy_file(
                source_fd, destination_fd, entry.name, relative
            )
            files.append(
                {"path": relative, "size": info.st_size, "sha256": digest}
            )
            totals.files += 1
            if totals.files > _MAX_COPY_ENTRIES:
                raise InstallerStagingError(
                    "The ISO contains too many files to stage safely."
                )
            totals.bytes += info.st_size
            if progress is not None:
                fraction = (
                    min(0.9, 0.05 + 0.85 * (totals.bytes / expected_bytes))
                    if expected_bytes
                    else 0.5
                )
                self._report(
                    progress,
                    f"Copying {relative}…",
                    fraction,
                )

    @staticmethod
    def _copy_file(
        source_fd: int,
        destination_fd: int,
        name: str,
        relative: str,
    ) -> str:
        try:
            source = os.open(name, _OPEN_FILE, dir_fd=source_fd)
        except OSError as error:
            raise InstallerStagingError(
                f"Could not open source file {relative}: {error.strerror or error}"
            ) from error
        try:
            destination = os.open(
                name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=destination_fd,
            )
        except OSError as error:
            os.close(source)
            raise InstallerStagingError(
                f"Could not create staged file {relative}: "
                f"{error.strerror or error}"
            ) from error

        digest = hashlib.sha256()
        try:
            while True:
                chunk = os.read(source, _CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(destination, view)
                    view = view[written:]
        except OSError as error:
            raise InstallerStagingError(
                f"Could not copy {relative}: {error.strerror or error}"
            ) from error
        finally:
            os.close(source)
            os.close(destination)
        return digest.hexdigest()

    def _write_manifest(self, staging: Path, manifest: dict[str, object]) -> None:
        path = staging / MANIFEST_NAME
        data = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
        try:
            descriptor = os.open(
                path,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
        except OSError as error:
            raise InstallerStagingError(
                f"The staging manifest could not be written: {error.strerror or error}"
            ) from error

    def _read_manifest(self, staging: Path) -> dict[str, object]:
        path = staging / MANIFEST_NAME
        try:
            raw = path.read_text(encoding="utf-8")
            manifest = json.loads(raw)
        except (OSError, UnicodeError, ValueError) as error:
            raise InstallerStagingError(
                f"The staging manifest is missing or unreadable: {path}"
            ) from error
        if (
            not isinstance(manifest, dict)
            or manifest.get("version") != 1
            or not isinstance(manifest.get("files"), list)
            or not isinstance(manifest.get("directories"), list)
        ):
            raise InstallerStagingError(f"The staging manifest is invalid: {path}")
        return manifest

    def _verify_tree(
        self,
        staging: Path,
        source: Path,
        manifest: dict[str, object],
    ) -> tuple[int, int]:
        try:
            source_fd = os.open(source, _OPEN_DIRECTORY)
        except OSError as error:
            raise InstallerStagingError(
                f"The staged source directory could not be opened: {source}"
            ) from error
        expected = {
            str(entry["path"]): entry
            for entry in manifest["files"]  # type: ignore[index]
            if isinstance(entry, dict)
            and isinstance(entry.get("path"), str)
            and isinstance(entry.get("sha256"), str)
            and isinstance(entry.get("size"), int)
        }
        expected_directories = {
            str(value)
            for value in manifest["directories"]  # type: ignore[index]
            if isinstance(value, str)
        }
        seen_directories: set[str] = set()
        try:
            file_count = self._verify_directory(
                source_fd, "", expected, seen_directories, 0
            )
        finally:
            os.close(source_fd)

        if expected:
            missing = ", ".join(sorted(expected)[:5])
            raise InstallerStagingError(
                f"The staged copy is incomplete; missing files: {missing}"
            )
        missing_directories = expected_directories - seen_directories
        if missing_directories:
            missing = ", ".join(sorted(missing_directories)[:5])
            raise InstallerStagingError(
                f"The staged copy is incomplete; missing directories: {missing}"
            )
        return file_count, len(seen_directories)

    def _verify_directory(
        self,
        descriptor: int,
        prefix: str,
        expected: dict[str, dict[str, object]],
        seen_directories: set[str],
        depth: int,
    ) -> int:
        if depth > _MAX_DEPTH:
            raise InstallerStagingError(
                "The staged directory structure is nested too deeply to verify."
            )
        try:
            with os.scandir(descriptor) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except OSError as error:
            raise InstallerStagingError(
                f"Could not verify staged contents: {error.strerror or error}"
            ) from error

        count = 0
        for entry in entries:
            relative = f"{prefix}/{entry.name}" if prefix else entry.name
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as error:
                raise InstallerStagingError(
                    f"Could not verify {relative}: {error.strerror or error}"
                ) from error
            if stat.S_ISLNK(info.st_mode):
                raise InstallerStagingError(
                    f"The staged copy unexpectedly contains a symbolic link: {relative}"
                )
            if stat.S_ISDIR(info.st_mode):
                seen_directories.add(relative)
                try:
                    child = os.open(entry.name, _OPEN_DIRECTORY, dir_fd=descriptor)
                except OSError as error:
                    raise InstallerStagingError(
                        f"Could not open staged directory {relative}: "
                        f"{error.strerror or error}"
                    ) from error
                try:
                    count += self._verify_directory(
                        child, relative, expected, seen_directories, depth + 1
                    )
                finally:
                    os.close(child)
                continue
            if not stat.S_ISREG(info.st_mode):
                raise InstallerStagingError(
                    f"The staged copy contains an unsupported file type: {relative}"
                )
            record = expected.pop(relative, None)
            if record is None:
                raise InstallerStagingError(
                    f"The staged copy contains an unexpected file: {relative}"
                )
            if info.st_size != record["size"]:
                raise InstallerStagingError(
                    f"The staged file size does not match the ISO: {relative}"
                )
            digest = self._hash_file(descriptor, entry.name, relative)
            if digest != record["sha256"]:
                raise InstallerStagingError(
                    f"The staged file contents do not match the ISO: {relative}"
                )
            count += 1
        return count

    @staticmethod
    def _hash_file(descriptor: int, name: str, relative: str) -> str:
        try:
            child = os.open(name, _OPEN_FILE, dir_fd=descriptor)
        except OSError as error:
            raise InstallerStagingError(
                f"Could not read staged file {relative}: {error.strerror or error}"
            ) from error
        digest = hashlib.sha256()
        try:
            while True:
                chunk = os.read(child, _CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
        except OSError as error:
            raise InstallerStagingError(
                f"Could not verify staged file {relative}: "
                f"{error.strerror or error}"
            ) from error
        finally:
            os.close(child)
        return digest.hexdigest()

    @staticmethod
    def _report(
        progress: ProgressCallback | None, message: str, fraction: float
    ) -> None:
        if progress is not None:
            progress(message, max(0.0, min(1.0, fraction)))


@dataclass(slots=True)
class _Totals:
    files: int = 0
    bytes: int = 0


@dataclass(slots=True)
class _Counter:
    count: int = 0


def _safe_relative_parts(relative_path: str) -> list[str]:
    if not relative_path or relative_path.startswith("/"):
        raise InstallerError("The selected installer path is not relative.")
    parts = relative_path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise InstallerError(
            "The selected installer path contains an unsafe segment."
        )
    return parts


def _directory_has_entries(path: Path) -> bool:
    """Return whether a directory contains any entry, without following links."""
    try:
        descriptor = os.open(path, _OPEN_DIRECTORY)
    except OSError:
        return True
    try:
        with os.scandir(descriptor) as iterator:
            return next(iterator, None) is not None
    except OSError:
        return True
    finally:
        os.close(descriptor)


def _human_size(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


__all__ = [
    "CandidateKind",
    "InstallationDestination",
    "InstallerCandidate",
    "InstallerManager",
    "MANIFEST_NAME",
    "PendingRemovalItem",
    "PendingRemovalReport",
    "StagingSummary",
]
