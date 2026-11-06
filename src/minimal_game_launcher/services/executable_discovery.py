"""Read-only discovery of installed game executables.

The service never executes a file, follows a symbolic link, or writes to an
installation or Wine prefix. It scans only the explicitly supplied installation
directory and the Wine prefix's ``drive_c`` tree, in a fixed priority order, and
returns deterministic, ranked candidates for the user to confirm.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

_MAX_DEPTH = 8
_MAX_FILES = 20_000
_MAX_CANDIDATES = 200
_MAX_NAME_LENGTH = 200

_OPEN_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(
    os, "O_CLOEXEC", 0
)

# Hidden: clearly identifiable helpers. Matched against the whole executable
# stem (after dropping architecture suffixes such as ``_x64``), or against a
# distinctive compound fragment. Fragments are never ordinary words, so
# "Patchwork", "Crashlands", and "Updated Quest" are not affected.
_HIDDEN_STEMS = frozenset(
    {
        "setup",
        "install",
        "installer",
        "uninstall",
        "updater",
        "helper",
        "config",
        "settings",
    }
)
_HIDDEN_FRAGMENTS = (
    "unins",
    "vcredist",
    "redist",
    "dxsetup",
    "dotnet",
    "crashhandler",
    "crashpad",
    "crashreport",
    "diagnostic",
    "benchmark",
)
# Demoted: words that name helpers but also appear in real game titles. These
# files stay visible and receive a score penalty, so they rank below others.
_DEMOTED_TOKENS = frozenset(
    {
        "setup",
        "install",
        "installer",
        "uninstall",
        "update",
        "updater",
        "patch",
        "crash",
        "helper",
        "repair",
        "register",
        "touchup",
        "launcher",
        "config",
        "settings",
        "server",
        "tool",
        "editor",
        "readme",
    }
)
_ARCH_TOKENS = frozenset({"x64", "x86", "win64", "win32", "32", "64"})
_DEMOTION = 30

# Wine provisions its own demo applications and Windows accessories into every
# prefix. They are never the game the user just installed. When one of these
# executable names appears in a directory that unambiguously belongs to Wine or
# Windows, it is excluded instead of competing with the real game. Elsewhere the
# same name is only penalised, because a game may legitimately use it.
_WINE_BUILTIN_STEMS = frozenset(
    {
        "charmap",
        "clock",
        "cmd",
        "control",
        "cscript",
        "dvdplay",
        "explorer",
        "hh",
        "ieframe",
        "iexplore",
        "magnify",
        "mshta",
        "msiexec",
        "mspaint",
        "narrator",
        "notepad",
        "osk",
        "progman",
        "regedit",
        "regsvr32",
        "rundll32",
        "services",
        "start",
        "svchost",
        "taskmgr",
        "uninstaller",
        "winemine",
        "wineboot",
        "winebrowser",
        "winecfg",
        "wineconsole",
        "winedbg",
        "winefile",
        "winepath",
        "winhlp32",
        "wmplayer",
        "wordpad",
        "wscript",
        "xcopy",
        "xwizard",
    }
)
_WINE_SYSTEM_DIRECTORIES = frozenset(
    {
        "accessories",
        "common files",
        "internet explorer",
        "microsoft.net",
        "msbuild",
        "netmeeting",
        "outlook express",
        "reference assemblies",
        "system32",
        "syswow64",
        "windows defender",
        "windows journal",
        "windows mail",
        "windows media player",
        "windows nt",
        "windows photo viewer",
        "windows portable devices",
        "windows powershell",
        "windows sidebar",
        "windows system32",
    }
)
_BUILTIN_DEMOTION = 60
_DRIVE_C_SKIP = frozenset(
    {
        "windows",
        "program files",
        "program files (x86)",
        "users",
        "$recycle.bin",
        "system volume information",
        "windows.old",
        "recycler",
    }
)

_NAME_TOKEN = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True, slots=True)
class ExecutableCandidate:
    """One discovered executable, its origin, and its ranking explanation.

    ``path`` is a real host path so the launch configuration can store it
    unchanged. ``relative_path`` is relative to the candidate's search root and
    ``location`` names that root for display.
    """

    path: Path
    relative_path: str
    location: str
    score: int
    recommended: bool
    reason: str


@dataclass(frozen=True, slots=True)
class _Root:
    path: Path
    location: str
    base_score: int
    relative_prefix: str


@dataclass(slots=True)
class _ScanState:
    seen_files: set[tuple[int, int]]
    entries: int = 0
    candidates: int = 0


class ExecutableDiscovery:
    """Find likely game executables without running or modifying anything."""

    def __init__(
        self,
        *,
        max_depth: int = _MAX_DEPTH,
        max_files: int = _MAX_FILES,
        max_candidates: int = _MAX_CANDIDATES,
    ) -> None:
        self._max_depth = max(1, max_depth)
        self._max_files = max(1, max_files)
        self._max_candidates = max(1, max_candidates)

    def discover(
        self,
        game_name: str,
        *,
        installation_directory: str | Path | None = None,
        prefix_path: str | Path | None = None,
    ) -> list[ExecutableCandidate]:
        """Return ranked candidates from the allowed search roots.

        Search order is the configured installation directory, ``Program
        Files``, ``Program Files (x86)``, then other direct children of
        ``drive_c``. Missing, unreadable, or malformed roots are skipped
        silently; a scan never escapes the selected roots.
        """
        roots = self._roots(installation_directory, prefix_path)
        if not roots:
            return []

        name_tokens = _tokens(game_name)
        name_compact = _compact(game_name)
        state = _ScanState(seen_files=set())
        found: list[ExecutableCandidate] = []
        for root in roots:
            if state.entries >= self._max_files:
                break
            self._scan_root(root, name_tokens, name_compact, state, found)

        ranked = sorted(
            found,
            key=lambda candidate: (
                -candidate.score,
                candidate.relative_path.casefold(),
                candidate.location.casefold(),
                str(candidate.path),
            ),
        )[: self._max_candidates]
        if not ranked:
            return []
        best = ranked[0]
        recommendation = len(ranked) == 1 or best.score > ranked[1].score
        return [
            ExecutableCandidate(
                path=candidate.path,
                relative_path=candidate.relative_path,
                location=candidate.location,
                score=candidate.score,
                recommended=recommendation and index == 0,
                reason=candidate.reason,
            )
            for index, candidate in enumerate(ranked)
        ]

    # ------------------------------------------------------------------
    # Roots

    @staticmethod
    def _roots(
        installation_directory: str | Path | None,
        prefix_path: str | Path | None,
    ) -> list[_Root]:
        roots: list[_Root] = []
        if installation_directory and str(installation_directory).strip():
            roots.append(
                _Root(
                    Path(installation_directory).expanduser(),
                    "installation folder",
                    40,
                    "",
                )
            )
        if prefix_path and str(prefix_path).strip():
            drive_c = Path(prefix_path).expanduser() / "drive_c"
            roots.extend(
                (
                    _Root(
                        drive_c / "Program Files",
                        "Program Files",
                        30,
                        "drive_c/Program Files",
                    ),
                    _Root(
                        drive_c / "Program Files (x86)",
                        "Program Files (x86)",
                        28,
                        "drive_c/Program Files (x86)",
                    ),
                )
            )
            try:
                entries = sorted(
                    drive_c.iterdir(), key=lambda entry: entry.name.casefold()
                )
            except OSError:
                entries = []
            for entry in entries:
                if entry.name.casefold() in _DRIVE_C_SKIP:
                    continue
                roots.append(
                    _Root(entry, "Wine drive_c", 20, f"drive_c/{entry.name}")
                )
        return roots

    def _scan_root(
        self,
        root: _Root,
        name_tokens: frozenset[str],
        name_compact: str,
        state: _ScanState,
        found: list[ExecutableCandidate],
    ) -> None:
        try:
            info = os.lstat(root.path)
        except OSError:
            return
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            return
        try:
            descriptor = os.open(root.path, _OPEN_DIRECTORY)
        except OSError:
            return
        try:
            self._walk(
                descriptor,
                root,
                root.path,
                "",
                name_tokens,
                name_compact,
                state,
                found,
                0,
            )
        finally:
            os.close(descriptor)

    # ------------------------------------------------------------------
    # Traversal

    def _walk(
        self,
        descriptor: int,
        root: _Root,
        current: Path,
        relative: str,
        name_tokens: frozenset[str],
        name_compact: str,
        state: _ScanState,
        found: list[ExecutableCandidate],
        depth: int,
    ) -> None:
        if depth > self._max_depth or state.candidates >= self._max_candidates:
            return
        try:
            with os.scandir(descriptor) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name.casefold())
        except OSError:
            return

        for entry in entries:
            if state.entries >= self._max_files:
                return
            state.entries += 1
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISLNK(info.st_mode):
                continue
            child_relative = f"{relative}/{entry.name}" if relative else entry.name

            if stat.S_ISDIR(info.st_mode):
                try:
                    child = os.open(
                        entry.name, _OPEN_DIRECTORY, dir_fd=descriptor
                    )
                except OSError:
                    continue
                try:
                    self._walk(
                        child,
                        root,
                        current / entry.name,
                        child_relative,
                        name_tokens,
                        name_compact,
                        state,
                        found,
                        depth + 1,
                    )
                finally:
                    os.close(child)
                continue

            if not stat.S_ISREG(info.st_mode):
                continue
            if not entry.name.casefold().endswith(".exe"):
                continue
            if _is_hidden(entry.name):
                continue
            if _is_wine_builtin_application(entry.name, child_relative):
                continue

            identity = (info.st_dev, info.st_ino)
            if identity in state.seen_files:
                continue
            state.seen_files.add(identity)

            path = current / entry.name
            score, reason = self._score(
                entry.name,
                child_relative,
                root,
                depth,
                name_tokens,
                name_compact,
            )
            found.append(
                ExecutableCandidate(
                    path=path,
                    relative_path=(
                        f"{root.relative_prefix}/{child_relative}"
                        if root.relative_prefix
                        else child_relative
                    ),
                    location=root.location,
                    score=score,
                    recommended=False,
                    reason=reason,
                )
            )
            state.candidates += 1
            if state.candidates >= self._max_candidates:
                return

    # ------------------------------------------------------------------
    # Ranking

    @staticmethod
    def _score(
        name: str,
        relative: str,
        root: _Root,
        depth: int,
        name_tokens: frozenset[str],
        name_compact: str,
    ) -> tuple[int, str]:
        stem = name.rsplit(".", 1)[0]
        score = root.base_score + max(0, 12 - depth * 2)
        reasons = [f"Found in {root.location}."]

        strength = _match_strength(stem, name_tokens, name_compact)
        if strength == 3:
            score += 80
            reasons.append("Executable name matches the game name exactly.")
        elif strength == 2:
            score += 55
            reasons.append("Executable name contains the game name.")
        elif strength == 1:
            score += 25
            reasons.append("Executable name shares words with the game name.")

        directory = (
            relative.rsplit("/", 1)[0].rsplit("/", 1)[-1]
            if "/" in relative
            else ""
        )
        directory_strength = _match_strength(directory, name_tokens, name_compact)
        if directory_strength == 3:
            score += 30
            reasons.append("It is in a folder named after the game.")
        elif directory_strength == 2:
            score += 18
            reasons.append("Its folder name contains the game name.")
        elif directory_strength == 1:
            score += 10
            reasons.append("Its folder name shares words with the game name.")

        if "/" not in relative:
            score += 8
            reasons.append("It is at the top of the search folder.")
        if len(relative.split("/")) <= 2:
            score += 4
        if _is_demoted(name, name_tokens):
            score -= _DEMOTION
            reasons.append("Its name looks like a helper, so it is ranked lower.")
        if _is_builtin_stem(name):
            score -= _BUILTIN_DEMOTION
            reasons.append(
                "It is a Wine or Windows built-in application, so it is "
                "ranked lower."
            )
        return score, " ".join(reasons)


def _compact(value: str) -> str:
    """Return a lowercase identifier with all separators removed."""
    return _NAME_TOKEN.sub("", value.casefold()[:_MAX_NAME_LENGTH])


def _tokens(value: str) -> frozenset[str]:
    """Split a display name into lowercase words, including camel case.

    Wine games commonly ship ``PlantsVsZombies.exe`` while the stored game name
    is "Plants vs. Zombies". Splitting camel-case boundaries lets the two forms
    compare equal without special-casing any title.
    """
    bounded = value[:_MAX_NAME_LENGTH]
    bounded = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", bounded)
    bounded = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", bounded)
    return frozenset(
        token for token in _NAME_TOKEN.split(bounded.casefold()) if token
    )


def _match_strength(
    value: str, name_tokens: frozenset[str], name_compact: str
) -> int:
    """Return 3 for an exact match, 2 for containment, 1 for overlap, else 0."""
    if not value or not name_tokens:
        return 0
    tokens = _tokens(value)
    if not tokens:
        return 0
    compact = _compact(value)
    if tokens == name_tokens or (
        len(name_compact) >= 3 and compact == name_compact
    ):
        return 3
    if name_tokens <= tokens or (
        len(name_compact) >= 4 and name_compact in compact
    ):
        return 2
    if name_tokens & tokens:
        return 1
    return 0


def _is_builtin_stem(name: str) -> bool:
    """Return whether the executable name is a known Wine/Windows built-in."""
    return any(word in _WINE_BUILTIN_STEMS for word in _words(name))


def _is_wine_builtin_application(name: str, relative: str) -> bool:
    """Return whether this is a built-in inside an unambiguous system folder."""
    if not _is_builtin_stem(name):
        return False
    directories = {
        segment.casefold() for segment in relative.split("/")[:-1]
    }
    return bool(directories & _WINE_SYSTEM_DIRECTORIES)



def _words(name: str) -> list[str]:
    """Normalize a stem for helper matching: drop digits and architecture tags.

    ``unins000`` becomes ``unins``, and ``vc_redist.x64`` becomes ``vc``,
    ``redist``.
    """
    stem = name.rsplit(".", 1)[0].casefold()[:_MAX_NAME_LENGTH]
    tokens = (token for token in _NAME_TOKEN.split(stem) if token not in _ARCH_TOKENS)
    words = (re.sub(r"\d+$", "", token) for token in tokens)
    return [word for word in words if word]


def _is_hidden(name: str) -> bool:
    """Return whether a file is a clearly identifiable helper to drop entirely.

    The whole stem must be a helper word (``Setup.exe``), or one word must
    contain a distinctive fragment (``UnityCrashHandler64.exe``). Matching is
    per word, so fragments never match across word boundaries.
    """
    words = _words(name)
    if len(words) == 1 and words[0] in _HIDDEN_STEMS:
        return True
    return any(fragment in word for word in words for fragment in _HIDDEN_FRAGMENTS)


def _is_demoted(name: str, name_tokens: frozenset[str]) -> bool:
    """Return whether a name contains a helper-like word the game title lacks.

    A word that is also in the game's own name (``Setup Quest`` for a game
    called "Setup Quest") is not a helper signal and is not demoted.
    """
    return any(
        word in _DEMOTED_TOKENS and word not in name_tokens
        for word in _words(name)
    )


__all__ = ["ExecutableCandidate", "ExecutableDiscovery"]
