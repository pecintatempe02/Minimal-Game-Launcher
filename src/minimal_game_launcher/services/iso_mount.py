"""Mount ISO images read-only through UDisks without root privileges.

The verified host mechanism is ``udisksctl``: it creates a loop device owned by
the caller and mounts it read-only without elevation when the desktop's polkit
policy allows it. Extraction is deliberately not a fallback. If UDisks is
missing or refuses the request, the caller receives an actionable error.

Safety rules:

- The original ISO is only opened by UDisks; this module never writes to it.
- ``loop-setup`` always receives ``--read-only`` and ``--no-partition-scan``,
  and the mount always requests ``ro``. Without ``--no-partition-scan``, UDisks
  sees a hybrid image's Apple Partition Map or GPT and withholds the whole-disk
  ``Filesystem`` interface, so even a valid ISO9660 image is rejected as "not
  a mountable filesystem". The resulting mount is verified in
  ``/proc/self/mountinfo`` and the loop device is verified through
  ``/sys/block/loopN/ro``.
- Unmounting requires the exact loop device and mount point recorded by this
  module. A mount created by another process is never touched.
- Every failure after loop setup attempts to release the loop device, and the
  service tracks active mounts so application shutdown can clean up.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import stat
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from minimal_game_launcher.errors import IsoMountError

LOGGER = logging.getLogger(__name__)

_LOOP_DEVICE = re.compile(r"^/dev/loop\d+$")
_LOOP_OUTPUT = re.compile(r"\bas\s+(/dev/loop\d+)\b")
_MOUNTINFO_ESCAPE = re.compile(r"\\([0-7]{3})")
_RUN_FACTORY = Callable[..., subprocess.CompletedProcess]


@dataclass(frozen=True, slots=True)
class MountedIso:
    """A mount created and verified by :class:`IsoMountService`."""

    iso_path: Path
    loop_device: str
    mount_point: Path


@dataclass(frozen=True, slots=True)
class _MountEntry:
    """One parsed ``/proc/self/mountinfo`` entry."""

    mount_point: Path
    options: tuple[str, ...]
    filesystem: str
    source: str
    super_options: tuple[str, ...]


class IsoMountService:
    """Create and release verified read-only ISO mounts."""

    def __init__(
        self,
        *,
        udisksctl: str | None = None,
        run_factory: _RUN_FACTORY | None = None,
        mountinfo_path: str | Path = "/proc/self/mountinfo",
        sys_block_path: str | Path = "/sys/block",
        timeout: float = 30.0,
    ) -> None:
        self._udisksctl = (
            shutil.which("udisksctl") if udisksctl is None else udisksctl or None
        )
        self._run_factory = run_factory or subprocess.run
        self._mountinfo_path = Path(mountinfo_path)
        self._sys_block_path = Path(sys_block_path)
        self._timeout = max(1.0, timeout)
        self._active: dict[str, MountedIso] = {}
        self._lock = threading.Lock()

    def available(self) -> tuple[bool, str]:
        """Return whether the host has a usable unprivileged mount helper."""
        if not self._udisksctl:
            return False, (
                "ISO installation requires udisksctl, which was not found in "
                "PATH. Install UDisks or make udisksctl available. Archive "
                "extraction is not used as a fallback."
            )
        return True, "UDisks is available for read-only ISO mounts."

    def validate_iso(self, iso_path: str | Path) -> Path:
        """Validate an ISO as a readable regular file without modifying it."""
        available, reason = self.available()
        if not available:
            raise IsoMountError(reason)

        raw = Path(iso_path).expanduser()
        try:
            resolved = raw.resolve(strict=True)
        except OSError as error:
            raise IsoMountError(f"The ISO image was not found: {raw}") from error
        try:
            info = os.lstat(resolved)
        except OSError as error:
            raise IsoMountError(
                f"The ISO image could not be inspected: {resolved}"
            ) from error
        if stat.S_ISLNK(info.st_mode):
            raise IsoMountError(
                f"The ISO image is a symbolic link and is not used directly: {raw}"
            )
        if not stat.S_ISREG(info.st_mode):
            raise IsoMountError(f"The ISO image is not a regular file: {resolved}")
        if info.st_size <= 0:
            raise IsoMountError(f"The ISO image is empty: {resolved}")
        if not os.access(resolved, os.R_OK):
            raise IsoMountError(
                f"Permission denied while reading the ISO image: {resolved}"
            )
        return resolved

    def mount(self, iso_path: str | Path) -> MountedIso:
        """Set up a read-only loop device and mount it, verifying ownership."""
        iso = self.validate_iso(iso_path)
        assert self._udisksctl is not None

        loop_device = self._loop_setup(iso)
        mounted_attempt = False
        try:
            self._verify_loop_read_only(loop_device)
            if self._entries_for_device(loop_device):
                raise IsoMountError(
                    f"The new loop device is already mounted: {loop_device}"
                )
            self._mount_loop(loop_device)
            mounted_attempt = True
            mounted = self._verified_mount(iso, loop_device)
        except BaseException:
            if mounted_attempt:
                self._best_effort_unmount(loop_device)
            self._best_effort_loop_delete(loop_device)
            raise

        with self._lock:
            self._active[loop_device] = mounted
        LOGGER.info(
            "Mounted ISO %s read-only at %s through %s",
            iso,
            mounted.mount_point,
            loop_device,
        )
        return mounted

    def unmount(self, mounted: MountedIso) -> None:
        """Release a mount created by this service, verifying it first.

        A mount whose device or target no longer matches the recorded values is
        left alone. The loop device is deleted only when it is no longer in use.
        """
        with self._lock:
            recorded = self._active.get(mounted.loop_device)
        if recorded != mounted:
            raise IsoMountError(
                "Refusing to unmount a mount that this launcher did not create."
            )

        entries = self._entries_for_device(mounted.loop_device)
        if entries:
            if len(entries) != 1 or entries[0].mount_point != mounted.mount_point:
                raise IsoMountError(
                    "Refusing to unmount: the loop device is mounted somewhere "
                    "other than the location this launcher recorded."
                )
            self._run(
                ["unmount", "--no-user-interaction", "-b", mounted.loop_device],
                "unmount",
            )
            if self._entries_for_device(mounted.loop_device):
                raise IsoMountError(
                    "The ISO mount is still present after unmounting; the loop "
                    "device was kept to avoid disrupting another user."
                )
        self._loop_delete(mounted.loop_device)
        with self._lock:
            self._active.pop(mounted.loop_device, None)
        LOGGER.info("Released ISO mount %s", mounted.mount_point)

    def shutdown(self) -> None:
        """Release every mount still tracked by this service, best effort."""
        with self._lock:
            active = list(self._active.values())
        for mounted in active:
            try:
                self.unmount(mounted)
            except IsoMountError:
                LOGGER.exception(
                    "Could not release ISO mount %s during shutdown",
                    mounted.mount_point,
                )

    def _loop_setup(self, iso: Path) -> str:
        completed = self._run(
            [
                "loop-setup",
                "--no-user-interaction",
                "--read-only",
                "--no-partition-scan",
                "--file",
                str(iso),
            ],
            "loop setup",
        )
        output = f"{completed.stdout or ''}\n{completed.stderr or ''}"
        match = _LOOP_OUTPUT.search(output)
        if match is None:
            raise IsoMountError(
                "UDisks did not report a loop device for this ISO. "
                f"Details: {self._detail(completed)}"
            )
        device = match.group(1)
        if not _LOOP_DEVICE.match(device):
            raise IsoMountError(f"UDisks returned an unexpected device: {device}")
        return device

    def _mount_loop(self, loop_device: str) -> None:
        try:
            self._run(
                [
                    "mount",
                    "--no-user-interaction",
                    "-b",
                    loop_device,
                    "--options",
                    "ro",
                ],
                "mount",
            )
        except IsoMountError as error:
            if "not a mountable filesystem" in str(error).lower():
                raise IsoMountError(
                    f"UDisks did not expose a mountable filesystem on "
                    f"{loop_device}. The image may use a partitioned or hybrid "
                    "layout that this host cannot mount without privileges. "
                    f"Details: {error}"
                ) from error
            raise

    def _verified_mount(self, iso: Path, loop_device: str) -> MountedIso:
        entries = self._entries_for_device(loop_device)
        if len(entries) != 1:
            raise IsoMountError(
                "The mounted ISO could not be verified in the system mount table."
            )
        entry = entries[0]
        if "ro" not in entry.options and "ro" not in entry.super_options:
            raise IsoMountError(
                "The ISO was mounted without read-only protection; it was "
                "released without copying anything."
            )
        try:
            info = os.lstat(entry.mount_point)
        except OSError as error:
            raise IsoMountError(
                f"The ISO mount point could not be inspected: {entry.mount_point}"
            ) from error
        if not stat.S_ISDIR(info.st_mode):
            raise IsoMountError(
                f"The ISO mount point is not a directory: {entry.mount_point}"
            )
        if entry.mount_point == Path("/"):
            raise IsoMountError("Refusing to use the filesystem root as an ISO mount.")
        if not os.access(entry.mount_point, os.R_OK | os.X_OK):
            raise IsoMountError(
                f"The ISO contents are not readable: {entry.mount_point}"
            )
        return MountedIso(
            iso_path=iso,
            loop_device=loop_device,
            mount_point=entry.mount_point,
        )

    def _verify_loop_read_only(self, loop_device: str) -> None:
        name = Path(loop_device).name
        ro_path = self._sys_block_path / name / "ro"
        try:
            value = ro_path.read_text(encoding="ascii").strip()
        except OSError as error:
            raise IsoMountError(
                f"The read-only state of {loop_device} could not be verified."
            ) from error
        if value != "1":
            raise IsoMountError(
                f"{loop_device} is not read-only; refusing to mount the ISO."
            )

    def _run(self, arguments: list[str], operation: str) -> subprocess.CompletedProcess:
        assert self._udisksctl is not None
        try:
            completed = self._run_factory(
                [self._udisksctl, *arguments],
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired as error:
            raise IsoMountError(
                f"UDisks timed out while trying to {operation} the ISO."
            ) from error
        except (OSError, subprocess.SubprocessError) as error:
            raise IsoMountError(
                f"UDisks could not be run for {operation}: {error}"
            ) from error
        if completed.returncode != 0:
            raise IsoMountError(
                f"UDisks could not {operation} the ISO. {self._detail(completed)}"
            )
        return completed

    def _loop_delete(self, loop_device: str) -> None:
        self._run(
            ["loop-delete", "--no-user-interaction", "-b", loop_device],
            "loop release",
        )

    def _best_effort_loop_delete(self, loop_device: str) -> None:
        """Release a loop device after a failed mount; never mask the cause."""
        try:
            if self._entries_for_device(loop_device):
                LOGGER.warning(
                    "Not deleting %s after failure: it is still mounted",
                    loop_device,
                )
                return
            self._loop_delete(loop_device)
        except IsoMountError:
            LOGGER.exception("Could not release loop device %s", loop_device)

    def _best_effort_unmount(self, loop_device: str) -> None:
        """Unmount a device this service just mounted after verification failed."""
        try:
            entries = self._entries_for_device(loop_device)
            if len(entries) == 1:
                self._run(
                    ["unmount", "--no-user-interaction", "-b", loop_device],
                    "unmount",
                )
        except IsoMountError:
            LOGGER.exception(
                "Could not unmount failed ISO mount %s", loop_device
            )

    def _entries_for_device(self, loop_device: str) -> list[_MountEntry]:
        device = Path(loop_device)
        try:
            device_real = device.resolve(strict=False)
        except OSError:
            device_real = device
        entries = []
        for entry in self._read_mountinfo():
            source = Path(entry.source)
            try:
                source_real = source.resolve(strict=False)
            except OSError:
                source_real = source
            if source == device or source_real == device_real:
                entries.append(entry)
        return entries

    def _read_mountinfo(self) -> list[_MountEntry]:
        try:
            lines = self._mountinfo_path.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
        except OSError as error:
            raise IsoMountError(
                "The system mount table could not be read to verify the ISO mount."
            ) from error

        entries: list[_MountEntry] = []
        for line in lines:
            left, separator, right = line.partition(" - ")
            left_fields = left.split()
            right_fields = right.split()
            if not separator or len(left_fields) < 6 or len(right_fields) < 3:
                continue
            entries.append(
                _MountEntry(
                    mount_point=Path(_unescape(left_fields[4])),
                    options=tuple(left_fields[5].split(",")),
                    filesystem=right_fields[0],
                    source=_unescape(right_fields[1]),
                    super_options=tuple(right_fields[2].split(",")),
                )
            )
        return entries

    @staticmethod
    def _detail(completed: subprocess.CompletedProcess) -> str:
        text = " ".join(
            part.strip()
            for part in (completed.stderr, completed.stdout)
            if isinstance(part, str) and part.strip()
        )
        return text[:500] or f"exit status {completed.returncode}"


def _unescape(value: str) -> str:
    """Decode the octal escapes used by ``/proc/self/mountinfo``."""
    return _MOUNTINFO_ESCAPE.sub(
        lambda match: chr(int(match.group(1), 8)), value
    )


__all__ = ["IsoMountService", "MountedIso"]
