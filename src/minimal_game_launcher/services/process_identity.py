"""Reliable, read-only process identity for launch reattachment.

Game processes outlive the launcher: closing the window deliberately leaves a
running game alone. To show correct status after a restart, the launcher writes
a small pidfile per launched game and later revalidates it.

Identity is ``pid`` plus the kernel's ``starttime`` field (jiffies since boot,
read from ``/proc/<pid>/stat``). A reused PID belongs to a process that started
in a different clock tick, so an equal ``starttime`` is treated as the same
process. ``exe`` and ``cmdline`` are recorded as advisory hints only: Wine and
wrapper chains (``gamemoderun wine game.exe``) legally change both through
``exec`` while keeping the same PID and start time.

Nothing in this module ever sends a signal to any process. Verification is a
read of ``/proc``. An entry that cannot be read while the PID still exists is
reported as ``inaccessible`` and is treated as *potentially running* by the
launch manager: conservative behavior can only delay a launch, while guessing
"gone" could report a false exit or allow a duplicate launch.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

LOGGER = logging.getLogger(__name__)

VerifyResult = Literal["alive", "gone", "reused", "inaccessible"]

# Game ids are UUID hex strings from GameManager. The restriction also makes a
# pidfile name unable to escape its directory.
_SAFE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    """A validated snapshot of one running process."""

    pid: int
    starttime: str
    exe: str | None = None
    cmdline: tuple[str, ...] = ()

    def to_json(self) -> str:
        """Serialize for pidfile storage."""
        return json.dumps(
            {
                "pid": self.pid,
                "starttime": self.starttime,
                "exe": self.exe,
                "cmdline": list(self.cmdline),
            }
        )

    @classmethod
    def from_json(cls, payload: str) -> ProcessIdentity | None:
        """Parse a pidfile body, returning ``None`` for anything malformed."""
        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        pid = data.get("pid")
        starttime = data.get("starttime")
        if not isinstance(pid, int) or isinstance(pid, bool):
            return None
        if not isinstance(starttime, str) or not starttime:
            return None
        exe = data.get("exe")
        if exe is not None and not isinstance(exe, str):
            return None
        raw_cmdline = data.get("cmdline", [])
        if not isinstance(raw_cmdline, list) or not all(
            isinstance(item, str) for item in raw_cmdline
        ):
            return None
        return cls(
            pid=pid,
            starttime=starttime,
            exe=exe,
            cmdline=tuple(raw_cmdline),
        )


def _read_stat_fields(pid: int) -> tuple[list[str] | None, bool]:
    """Read /proc stat fields after the comm field.

    Returns ``(fields, absent)``. ``absent`` is ``True`` only when the
    ``/proc/<pid>`` entry does not exist (``FileNotFoundError``), which is
    proof the process no longer exists. Any other failure (hidepid mounts,
    I/O errors, decode problems) yields ``(None, False)`` so callers can
    distinguish *inaccessible* from *gone* instead of guessing.
    """
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(
            encoding="utf-8", errors="replace"
        )
    except FileNotFoundError:
        return None, True
    except (OSError, ValueError):
        return None, False
    # comm may contain spaces and parentheses; everything after the final ")"
    # starts at field 3 (state). Non-ASCII in comm becomes U+FFFD, which never
    # introduces a stray ")".
    after_comm = raw.rsplit(")", 1)
    if len(after_comm) != 2:
        return None, False
    return after_comm[1].split(), False


def process_identity(pid: int) -> ProcessIdentity | None:
    """Snapshot a process, or return ``None`` when it cannot be observed."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    fields, _absent = _read_stat_fields(pid)
    if fields is None or len(fields) < 20:
        return None
    # Field 3 is state (index 0); field 22 is starttime (index 19).
    state = fields[0]
    starttime = fields[19]
    if state == "Z":
        # A zombie is dead; its entry lingers until its parent reaps it.
        return None
    exe: str | None
    try:
        exe = os.readlink(f"/proc/{pid}/exe")
    except OSError:
        exe = None
    try:
        raw_cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        raw_cmdline = b""
    cmdline = tuple(
        part.decode("utf-8", errors="replace")
        for part in raw_cmdline.split(b"\0")
        if part
    )
    return ProcessIdentity(
        pid=pid, starttime=starttime, exe=exe, cmdline=cmdline
    )


def verify_process(recorded: ProcessIdentity) -> VerifyResult:
    """Compare a recorded identity against the live process table.

    ``alive`` means the same PID and start time are still present. ``reused``
    means the PID now belongs to a different process. ``gone`` means the
    process does not exist (proven by ``FileNotFoundError``) or is a zombie
    shell awaiting collection. ``inaccessible`` means the PID entry exists but
    ``/proc`` could not be read (hidepid mount, I/O error): the honest answer
    is *unknown*, so it is reported rather than guessed about.
    """
    if recorded.pid <= 0:
        return "reused"
    fields, absent = _read_stat_fields(recorded.pid)
    if fields is None:
        # A truly absent process is gone; one we merely cannot read is
        # inaccessible and must not be treated as an exit.
        return "gone" if absent else "inaccessible"
    if len(fields) < 20:
        return "inaccessible"
    if fields[0] == "Z":
        return "gone"
    if fields[19] != recorded.starttime:
        return "reused"
    # exe and cmdline drift legally inside wrapper chains, so they are not
    # required to match. They are logged when they change completely.
    if recorded.exe is not None or recorded.cmdline:
        fresh_exe: str | None
        try:
            fresh_exe = os.readlink(f"/proc/{recorded.pid}/exe")
        except OSError:
            fresh_exe = None
        if (
            recorded.exe is not None
            and fresh_exe is not None
            and fresh_exe != recorded.exe
        ):
            LOGGER.debug(
                "Process %s changed its executable from %s to %s",
                recorded.pid,
                recorded.exe,
                fresh_exe,
            )
    return "alive"


def _pidfile_path(directory: Path, game_id: str) -> Path | None:
    """Return a safe pidfile path, or ``None`` for an unusable game id."""
    if not _SAFE_ID_PATTERN.match(game_id):
        LOGGER.warning("Refusing unusual game id for pid tracking: %r", game_id)
        return None
    return directory / f"{game_id}.json"


def write_pidfile(
    directory: str | Path, identity: ProcessIdentity, game_id: str
) -> Path | None:
    """Record a launched process as a private JSON file, best effort.

    The file is created with ``O_NOFOLLOW`` and mode ``0600`` at open time,
    so a pre-planted symlink is never followed and the content is never
    briefly readable by other users. Writing replaces content in place;
    pidfiles are per-game single-writer files on a single-user desktop.
    """
    path = _pidfile_path(Path(directory), game_id)
    if path is None or identity.pid <= 0:
        return None
    payload = identity.to_json()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
            0o600,
        )
    except OSError:
        LOGGER.exception("Could not create process pidfile %s", path)
        return None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
    except OSError:
        LOGGER.exception("Could not write process pidfile %s", path)
        return None
    return path


def read_pidfile(
    directory: str | Path, game_id: str
) -> ProcessIdentity | None:
    """Load a recorded identity, or ``None`` when absent or unreadable."""
    path = _pidfile_path(Path(directory), game_id)
    if path is None or not path.is_file():
        return None
    try:
        payload = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        LOGGER.exception("Could not read process pidfile %s", path)
        return None
    return ProcessIdentity.from_json(payload)


def remove_pidfile(directory: str | Path, game_id: str) -> None:
    """Delete a pidfile, ignoring absence and permission problems."""
    path = _pidfile_path(Path(directory), game_id)
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        LOGGER.exception("Could not remove process pidfile %s", path)
