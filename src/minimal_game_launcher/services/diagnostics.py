"""Read-only, on-demand diagnostics for one library entry.

A diagnostic run never launches the configured game, runs ``wineboot``,
repairs a prefix, installs dependencies, or writes to a Wine prefix. Checks are
filesystem reads plus argument-list subprocesses with bounded timeouts.

Reports are local plain text. Environment values and credential files are never
read or included, and the user's home directory is shown as ``~`` unless full
paths are explicitly requested.
"""

from __future__ import annotations

import ctypes.util
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Callable

from minimal_game_launcher import APP_VERSION
from minimal_game_launcher.models import Game, Platform, is_bare_command
from minimal_game_launcher.services.launch_manager import (
    PREFIX_READY_MARKER,
    prefix_is_initialized,
)
from minimal_game_launcher.services.ownership import (
    OwnershipService,
    OwnershipStatus,
    ResourceKind,
)
from minimal_game_launcher.services.process_identity import (
    read_pidfile,
    verify_process,
)
from minimal_game_launcher.services.wine_output import (
    MAX_CAPTURE_BYTES,
    analyze_wine_output,
)
from minimal_game_launcher.settings import SettingsStore, WineSource

LOGGER = logging.getLogger(__name__)

Runner = Callable[..., subprocess.CompletedProcess]
WhichFactory = Callable[[str], str | None]
VulkanLibraryFinder = Callable[[str], str | None]

_WINE_TIMEOUT = 10.0
_VULKAN_TIMEOUT = 10.0
_MAX_OUTPUT = 2000
_MAX_REGISTRY_READ = 256 * 1024
_MAX_LAUNCH_LOG_READ = MAX_CAPTURE_BYTES + 64
_FREE_SPACE_WARNING = 1024**3
_DXVK_DLLS = ("dxgi.dll", "d3d11.dll", "d3d10core.dll")
_OVERRIDE_DLLS = frozenset({"d3d9", "d3d10core", "d3d11", "dxgi"})
_OVERRIDE_LINE = re.compile(r'"([A-Za-z0-9_.-]+)"="([^"\r\n]{0,64})"')


class CheckStatus(StrEnum):
    """How a single check or an overall report should be read."""

    OK = "ok"
    INFO = "info"
    WARNING = "warning"
    PROBLEM = "problem"
    UNKNOWN = "unknown"

    @property
    def label(self) -> str:
        """Return a short human-readable status label."""
        return {
            CheckStatus.OK: "OK",
            CheckStatus.INFO: "Information",
            CheckStatus.WARNING: "Warning",
            CheckStatus.PROBLEM: "Problem",
            CheckStatus.UNKNOWN: "Unknown",
        }[self]


@dataclass(frozen=True, slots=True)
class DiagnosticCheck:
    """One read-only check with a summary and optional technical detail."""

    key: str
    title: str
    status: CheckStatus
    summary: str
    suggestion: str | None = None
    details: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DiagnosticReport:
    """A complete diagnostic run, ready for display or export."""

    game_name: str
    game_id: str
    platform: Platform
    generated_at: str
    app_version: str
    redact_paths: bool
    checks: tuple[DiagnosticCheck, ...]

    @property
    def overall_status(self) -> CheckStatus:
        """Return the most serious status in the report."""
        order = {
            CheckStatus.PROBLEM: 0,
            CheckStatus.WARNING: 1,
            CheckStatus.UNKNOWN: 2,
            CheckStatus.INFO: 3,
            CheckStatus.OK: 4,
        }
        if not self.checks:
            return CheckStatus.INFO
        return min(
            (check.status for check in self.checks), key=order.__getitem__
        )

    def counts(self) -> dict[CheckStatus, int]:
        """Count checks by status, including zero-valued statuses."""
        result = {status: 0 for status in CheckStatus}
        for check in self.checks:
            result[check.status] += 1
        return result

    def summary_line(self) -> str:
        """Return the overall status with non-zero check counts."""
        counts = self.counts()
        parts = [
            f"{count} {status.value}"
            for status, count in counts.items()
            if count and status is not CheckStatus.OK
        ]
        if counts[CheckStatus.OK]:
            parts.append(f"{counts[CheckStatus.OK]} ok")
        return f"{self.overall_status.label}: " + (
            ", ".join(parts) if parts else "no checks"
        )

    def to_text(self) -> str:
        """Render a plain-text report suitable for local export."""
        lines = [
            "Minimal Game Launcher diagnostic report",
            f"Generated: {self.generated_at}",
            f"Launcher version: {self.app_version}",
            f"Game: {self.game_name}",
            f"Game ID: {self.game_id}",
            f"Platform: {self.platform.display_name}",
            "Path handling: "
            + (
                "home directory replaced with ~"
                if self.redact_paths
                else "full paths included"
            ),
            f"Overall: {self.summary_line()}",
        ]
        for check in self.checks:
            lines.extend(
                [
                    "",
                    f"[{check.status.value.upper()}] {check.title}",
                    f"  {check.summary}",
                ]
            )
            if check.suggestion:
                lines.append(f"  Suggestion: {check.suggestion}")
            if check.details:
                lines.append("  Technical details:")
                lines.extend(f"    - {detail}" for detail in check.details)
        return "\n".join(lines) + "\n"

    def save(self, path: str | Path) -> None:
        """Write the report to a user-selected local path."""
        Path(path).expanduser().write_text(self.to_text(), encoding="utf-8")


class DiagnosticsService:
    """Generate local, read-only diagnostic reports for library entries.

    ``which``, ``run``, ``vulkan_library``, ``home``, and the filesystem roots
    are injectable so tests never depend on the host's Wine, Vulkan, or user
    directories.
    """

    def __init__(
        self,
        prefixes_root: str | Path,
        settings_store: SettingsStore,
        *,
        ownership: OwnershipService | None = None,
        pidfiles_root: str | Path | None = None,
        log_path: str | Path | None = None,
        data_directory: str | Path | None = None,
        launch_logs_root: str | Path | None = None,
        home: str | Path | None = None,
        which: WhichFactory | None = None,
        run: Runner | None = None,
        vulkan_library: VulkanLibraryFinder | None = None,
        wine_timeout: float = _WINE_TIMEOUT,
        vulkan_timeout: float = _VULKAN_TIMEOUT,
    ) -> None:
        self._prefixes_root = Path(prefixes_root).expanduser()
        self._settings_store = settings_store
        data_root = (
            Path(data_directory).expanduser()
            if data_directory is not None
            else self._prefixes_root.parent
        )
        self._ownership = ownership or OwnershipService(
            self._prefixes_root,
            data_root / "games",
            data_root / "installers",
        )
        self._pidfiles_root = (
            Path(pidfiles_root).expanduser()
            if pidfiles_root is not None
            else None
        )
        self._log_path = (
            Path(log_path).expanduser() if log_path is not None else None
        )
        self._launch_logs_root = (
            Path(launch_logs_root).expanduser()
            if launch_logs_root is not None
            else None
        )
        self._data_directory = data_root
        self._home = (
            Path(home).expanduser() if home is not None else Path.home()
        )
        self._which = which or shutil.which
        self._run_factory = run or subprocess.run
        self._vulkan_library = vulkan_library or ctypes.util.find_library
        self._wine_timeout = wine_timeout
        self._vulkan_timeout = vulkan_timeout

    def generate(
        self, game: Game, *, redact_paths: bool = True
    ) -> DiagnosticReport:
        """Run every check, isolating failures to their own section.

        A check that raises is reported as unknown; other checks still run and
        no exception reaches the GTK main thread.
        """
        builders = (
            ("wine", "Wine", self._check_wine),
            ("prefix", "Wine prefix", self._check_prefix),
            ("dxvk", "DXVK", self._check_dxvk),
            ("vulkan", "Vulkan", self._check_vulkan),
            ("executable", "Executable", self._check_executable),
            ("working_directory", "Working directory", self._check_working_directory),
            ("process", "Last launch", self._check_process),
            ("launch_output", "Last launch output", self._check_launch_output),
            ("environment", "Environment", self._check_environment),
            ("log", "Launcher log", self._check_log),
            ("disk", "Disk space", self._check_disk),
        )
        checks: list[DiagnosticCheck] = []
        for key, title, builder in builders:
            try:
                checks.append(builder(game, redact_paths))
            except Exception as error:  # A failed check must not hide the rest.
                LOGGER.exception("Diagnostic check %s failed", key)
                checks.append(
                    DiagnosticCheck(
                        key=key,
                        title=title,
                        status=CheckStatus.UNKNOWN,
                        summary=(
                            "This check could not be completed "
                            f"({type(error).__name__})."
                        ),
                        suggestion=(
                            "Run diagnostics again. If the problem continues, "
                            "see the launcher log."
                        ),
                        details=(
                            self._redact_text(str(error), redact_paths),
                        ),
                    )
                )

        report = DiagnosticReport(
            game_name=game.name,
            game_id=game.id,
            platform=game.platform,
            generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
            app_version=APP_VERSION,
            redact_paths=redact_paths,
            checks=tuple(checks),
        )
        LOGGER.info(
            "Generated diagnostics for %s: %s",
            game.name,
            report.overall_status.value,
        )
        return report

    def _check_wine(
        self, game: Game, redact_paths: bool
    ) -> DiagnosticCheck:
        if game.platform is not Platform.WINDOWS:
            return DiagnosticCheck(
                key="wine",
                title="Wine",
                status=CheckStatus.INFO,
                summary="Wine is not used for this native Linux entry.",
                details=("Read-only check; no Wine command was run.",),
            )

        try:
            settings = self._settings_store.load()
        except Exception:
            return DiagnosticCheck(
                key="wine",
                title="Wine",
                status=CheckStatus.UNKNOWN,
                summary="The launcher settings could not be read.",
                suggestion="Check the launcher settings and try again.",
            )

        if settings.wine_source is WineSource.CUSTOM:
            raw_path = (settings.custom_wine_path or "").strip()
            if not raw_path:
                return DiagnosticCheck(
                    key="wine",
                    title="Wine",
                    status=CheckStatus.PROBLEM,
                    summary=(
                        "Custom Wine is selected in settings, but no executable "
                        "path is configured."
                    ),
                    suggestion=(
                        "Open Settings, choose a valid custom Wine executable, "
                        "or switch back to system Wine."
                    ),
                )
            candidate = Path(raw_path).expanduser()
            if not candidate.is_absolute():
                located = self._which(raw_path)
                if not located:
                    return self._wine_missing(
                        f"The custom Wine command was not found in PATH: "
                        f"{raw_path}",
                        redact_paths,
                    )
                candidate = Path(located)
            if not candidate.is_file():
                return self._wine_missing(
                    f"The custom Wine executable was not found: "
                    f"{candidate}",
                    redact_paths,
                )
            if not os.access(candidate, os.X_OK):
                return self._wine_missing(
                    f"The custom Wine executable is not executable: "
                    f"{candidate}",
                    redact_paths,
                )
            wine_executable = str(candidate)
            source = "custom"
        else:
            located = self._which("wine") or self._which("wine64")
            if not located:
                return self._wine_missing(
                    "Wine was not found in PATH (checked wine and wine64).",
                    redact_paths,
                )
            wine_executable = located
            source = "system"

        details = [
            f"Source: {source}",
            f"Executable: {self._redact_text(wine_executable, redact_paths)}",
        ]
        completed, error = self._run(
            [wine_executable, "--version"], self._wine_timeout
        )
        if error is not None:
            return DiagnosticCheck(
                key="wine",
                title="Wine",
                status=CheckStatus.WARNING,
                summary=(
                    "A Wine executable was found, but its version could not be "
                    "determined."
                ),
                suggestion=(
                    "Try running the Wine executable manually. Diagnostics did "
                    "not modify any prefix."
                ),
                details=tuple(details)
                + (self._redact_text(error, redact_paths),),
            )
        stdout = self._coerce_output(completed.stdout).strip()
        stderr = self._coerce_output(completed.stderr).strip()
        version = stdout.splitlines()[0].strip() if stdout else ""
        safe_version = self._redact_text(
            self._truncate(version), redact_paths
        )
        if completed.returncode != 0 or not version:
            detail = stderr or stdout or "No output"
            details.append(
                "wine --version exited with status "
                f"{completed.returncode}: "
                f"{self._redact_text(self._truncate(detail), redact_paths)}"
            )
            return DiagnosticCheck(
                key="wine",
                title="Wine",
                status=CheckStatus.WARNING,
                summary=(
                    "A Wine executable was found, but it did not report a "
                    "version."
                ),
                suggestion=(
                    "Verify the selected Wine installation in Settings. No Wine "
                    "prefix was modified."
                ),
                details=tuple(details),
            )
        details.append(safe_version)
        return DiagnosticCheck(
            key="wine",
            title="Wine",
            status=CheckStatus.OK,
            summary=f"Wine is available ({safe_version}).",
            details=tuple(details),
        )

    def _check_prefix(
        self, game: Game, redact_paths: bool
    ) -> DiagnosticCheck:
        if game.platform is not Platform.WINDOWS:
            return DiagnosticCheck(
                key="prefix",
                title="Wine prefix",
                status=CheckStatus.INFO,
                summary="This native Linux entry does not use a Wine prefix.",
            )

        prefix = self._prefix_for(game)
        kind = (
            ResourceKind.SHARED_PREFIX
            if game.wine_prefix_shared
            else ResourceKind.PREFIX
        )
        verdict = self._ownership.classify(kind, game.id, prefix)
        ownership = verdict.status
        # Only the type is reported; no environment values or registry contents.
        if ownership is OwnershipStatus.EXTERNAL:
            prefix_type = "external prefix"
        elif game.wine_prefix_shared:
            prefix_type = "shared managed prefix"
        else:
            prefix_type = "per-game managed prefix"
        details = [
            f"Type: {prefix_type}",
            f"Path: {self._redact_text(str(prefix), redact_paths)}",
            f"Ownership: {ownership.value}",
            f"Ownership reason: {verdict.reason}",
        ]

        if ownership is OwnershipStatus.INVALID:
            return DiagnosticCheck(
                key="prefix",
                title="Wine prefix",
                status=CheckStatus.PROBLEM,
                summary=(
                    "The configured prefix path cannot be treated as a safe "
                    "launcher-managed resource."
                ),
                suggestion=(
                    "Point the game at a real prefix directory. MGL never "
                    "follows symbolic links or modifies untrusted paths."
                ),
                details=tuple(details),
            )

        if ownership is OwnershipStatus.MISSING:
            return DiagnosticCheck(
                key="prefix",
                title="Wine prefix",
                status=CheckStatus.WARNING,
                summary="The managed Wine prefix has not been created yet.",
                suggestion=(
                    "Launch the game once to let the launcher initialize "
                    "this prefix. Diagnostics never runs wineboot."
                ),
                details=tuple(details),
            )

        if ownership is OwnershipStatus.EXTERNAL and not prefix.exists():
            return DiagnosticCheck(
                key="prefix",
                title="Wine prefix",
                status=CheckStatus.PROBLEM,
                summary="The configured external prefix does not exist.",
                suggestion=(
                    "Check the prefix path in the game settings. MGL will not "
                    "create or modify an external prefix."
                ),
                details=tuple(details),
            )

        if not prefix.is_dir():
            return DiagnosticCheck(
                key="prefix",
                title="Wine prefix",
                status=CheckStatus.PROBLEM,
                summary="The configured prefix path is not a directory.",
                suggestion=(
                    "Choose a valid Wine prefix directory in the game settings."
                ),
                details=tuple(details),
            )
        if not os.access(prefix, os.R_OK | os.X_OK):
            return DiagnosticCheck(
                key="prefix",
                title="Wine prefix",
                status=CheckStatus.PROBLEM,
                summary="The Wine prefix directory is not accessible.",
                suggestion=(
                    "Check ownership and permissions on the prefix directory. "
                    "MGL does not change permissions automatically."
                ),
                details=tuple(details),
            )

        initialized = prefix_is_initialized(prefix)
        marker = prefix / PREFIX_READY_MARKER
        if marker.is_file():
            details.append("Launcher initialization marker: present")
        if (prefix / "system.reg").is_file():
            details.append("system.reg: present")
        drive_c = prefix / "drive_c"
        if initialized and not drive_c.is_dir():
            details.append("drive_c: missing")

        if not initialized:
            if ownership is OwnershipStatus.MANAGED:
                summary = (
                    "The managed prefix exists but has not been initialized by "
                    "Wine yet."
                )
                suggestion = (
                    "Launch the game once; the launcher initializes managed "
                    "prefixes with wineboot. Diagnostics does not do this."
                )
            else:
                summary = (
                    "The external prefix exists but has no Wine initialization "
                    "marker."
                )
                suggestion = (
                    "Confirm this is the intended prefix. MGL never modifies "
                    "external prefixes."
                )
            return DiagnosticCheck(
                key="prefix",
                title="Wine prefix",
                status=CheckStatus.WARNING,
                summary=summary,
                suggestion=suggestion,
                details=tuple(details),
            )

        if ownership is OwnershipStatus.MANAGED:
            summary = "The managed Wine prefix appears initialized."
            suggestion = None
        else:
            summary = "The external Wine prefix appears initialized."
            suggestion = (
                "This path is outside MGL's managed prefix directory. "
                "Diagnostics only reads it; the launcher never modifies "
                "external prefixes."
            )
        if not drive_c.is_dir():
            status = CheckStatus.WARNING
            summary += " The drive_c directory is missing."
        else:
            status = CheckStatus.OK
            details.append("drive_c: present")
        return DiagnosticCheck(
            key="prefix",
            title="Wine prefix",
            status=status,
            summary=summary,
            suggestion=suggestion,
            details=tuple(details),
        )

    def _check_dxvk(
        self, game: Game, redact_paths: bool
    ) -> DiagnosticCheck:
        if game.platform is not Platform.WINDOWS:
            return DiagnosticCheck(
                key="dxvk",
                title="DXVK",
                status=CheckStatus.INFO,
                summary="DXVK applies only to Windows/Wine entries.",
            )

        prefix = self._prefix_for(game)
        if not prefix.is_dir():
            return DiagnosticCheck(
                key="dxvk",
                title="DXVK",
                status=CheckStatus.UNKNOWN,
                summary=(
                    "The Wine prefix is not available, so DXVK presence cannot "
                    "be inspected."
                ),
                suggestion=(
                    "Launch the game once so its managed prefix exists, then "
                    "run diagnostics again."
                ),
                details=(
                    f"Path: {self._redact_text(str(prefix), redact_paths)}",
                ),
            )

        found: dict[str, list[str]] = {}
        for folder in ("system32", "syswow64"):
            present = [
                name
                for name in _DXVK_DLLS
                if (prefix / folder / name).is_file()
            ]
            if present:
                found[folder] = present

        details: list[str] = []
        for folder, names in found.items():
            details.append(f"{folder}: {', '.join(names)}")
        for folder in ("system32", "syswow64"):
            if folder not in found:
                details.append(f"{folder}: no DXVK DLLs detected")

        if (prefix / "dxvk.conf").is_file():
            details.append("dxvk.conf: present")

        overrides = self._registry_override_hints(prefix / "user.reg")
        for hint in overrides:
            details.append(f"user.reg override hint: {hint}")
        if overrides:
            details.append(
                "Override hints are read from user.reg and are not proof that "
                "DXVK is active. No registry was modified."
            )

        if not found:
            if overrides:
                return DiagnosticCheck(
                    key="dxvk",
                    title="DXVK",
                    status=CheckStatus.WARNING,
                    summary=(
                        "DLL override hints mention DXVK-related libraries, but "
                        "no DXVK DLL files were detected."
                    ),
                    suggestion=(
                        "Verify the DXVK installation in the prefix. MGL neither "
                        "installs nor removes DXVK."
                    ),
                    details=tuple(details),
                )
            return DiagnosticCheck(
                key="dxvk",
                title="DXVK",
                status=CheckStatus.INFO,
                summary="No DXVK DLL files were detected in the prefix.",
                suggestion=(
                    "DXVK may be absent or provided externally. MGL does not "
                    "install or remove DXVK."
                ),
                details=tuple(details),
            )

        if "system32" not in found or "syswow64" not in found:
            details.append(
                "Only one architecture's DXVK DLLs were detected; some games "
                "may need both."
            )
        return DiagnosticCheck(
            key="dxvk",
            title="DXVK",
            status=CheckStatus.OK,
            summary="DXVK DLL files were detected in the prefix.",
            suggestion=(
                "DXVK is provided outside MGL. The launcher did not install, "
                "enable, or modify it."
            ),
            details=tuple(details),
        )

    def _check_vulkan(
        self, game: Game, redact_paths: bool
    ) -> DiagnosticCheck:
        del game  # Vulkan is a host capability, not a per-entry setting.
        finder_error: str | None = None
        try:
            library = self._vulkan_library("vulkan")
        except Exception as error:
            library = None
            finder_error = f"{type(error).__name__}: {error}"

        vulkaninfo = self._which("vulkaninfo")
        if vulkaninfo:
            completed, error = self._run(
                [vulkaninfo, "--summary"], self._vulkan_timeout
            )
            details = [
                f"vulkaninfo: {self._redact_text(vulkaninfo, redact_paths)}"
            ]
            if error is not None:
                return DiagnosticCheck(
                    key="vulkan",
                    title="Vulkan",
                    status=CheckStatus.WARNING,
                    summary="vulkaninfo could not be run.",
                    suggestion=(
                        "Check the Vulkan installation and graphics driver. "
                        "MGL does not install drivers or Vulkan components."
                    ),
                    details=tuple(details)
                    + (self._redact_text(error, redact_paths),),
                )
            stdout = self._coerce_output(completed.stdout)
            stderr = self._coerce_output(completed.stderr)
            if completed.returncode != 0:
                detail = stderr.strip() or stdout.strip() or "No output"
                details.append(
                    "vulkaninfo exited with status "
                    f"{completed.returncode}: "
                    f"{self._redact_text(self._truncate(detail), redact_paths)}"
                )
                return DiagnosticCheck(
                    key="vulkan",
                    title="Vulkan",
                    status=CheckStatus.WARNING,
                    summary="vulkaninfo reported a failure.",
                    suggestion=(
                        "Check the Vulkan installation and graphics driver. "
                        "Some Wine games require working Vulkan."
                    ),
                    details=tuple(details),
                )
            summary_lines = self._vulkan_summary_lines(stdout)
            if not summary_lines:
                return DiagnosticCheck(
                    key="vulkan",
                    title="Vulkan",
                    status=CheckStatus.WARNING,
                    summary=(
                        "vulkaninfo ran but did not report a usable summary."
                    ),
                    suggestion=(
                        "Verify that a Vulkan-capable GPU driver is installed."
                    ),
                    details=tuple(details),
                )
            details.extend(
                self._redact_text(line, redact_paths)
                for line in summary_lines
            )
            return DiagnosticCheck(
                key="vulkan",
                title="Vulkan",
                status=CheckStatus.OK,
                summary="Vulkan is available.",
                details=tuple(details),
            )

        if library:
            return DiagnosticCheck(
                key="vulkan",
                title="Vulkan",
                status=CheckStatus.OK,
                summary="The Vulkan loader library was found.",
                suggestion=(
                    "vulkaninfo is not installed, so GPU and driver details "
                    "could not be read. MGL does not install it."
                ),
                details=(
                    f"Loader: {self._redact_text(library, redact_paths)}",
                ),
            )
        if finder_error is not None:
            return DiagnosticCheck(
                key="vulkan",
                title="Vulkan",
                status=CheckStatus.UNKNOWN,
                summary="The Vulkan loader could not be checked.",
                suggestion="Run diagnostics again or inspect the launcher log.",
                details=(self._redact_text(finder_error, redact_paths),),
            )
        return DiagnosticCheck(
            key="vulkan",
            title="Vulkan",
            status=CheckStatus.WARNING,
            summary="No Vulkan loader was detected.",
            suggestion=(
                "Wine games using DXVK require a working Vulkan driver. Install "
                "one for your GPU; MGL does not install drivers or dependencies."
            ),
            details=("Checked for the Vulkan loader library and vulkaninfo.",),
        )

    def _check_executable(
        self, game: Game, redact_paths: bool
    ) -> DiagnosticCheck:
        raw = game.executable.strip()
        if not raw:
            return DiagnosticCheck(
                key="executable",
                title="Executable",
                status=CheckStatus.PROBLEM,
                summary="No executable is configured.",
                suggestion="Edit the game and choose an executable or command.",
            )

        executable, error = self._locate_executable(game)
        if executable is None:
            return DiagnosticCheck(
                key="executable",
                title="Executable",
                status=CheckStatus.PROBLEM,
                summary=self._redact_text(
                    error or "The configured executable could not be found.",
                    redact_paths,
                ),
                suggestion=(
                    "Edit the game and choose an existing executable, or install "
                    "the command so it is available in PATH."
                ),
                details=(
                    f"Configured value: {self._redact_text(raw, redact_paths)}",
                ),
            )

        details = [
            f"Path: {self._redact_text(str(executable), redact_paths)}",
            "Read-only check; the executable was not run.",
        ]
        if not executable.exists():
            return DiagnosticCheck(
                key="executable",
                title="Executable",
                status=CheckStatus.PROBLEM,
                summary="The configured executable does not exist.",
                suggestion=(
                    "Edit the game and choose an existing executable. Nothing "
                    "was deleted or changed."
                ),
                details=tuple(details),
            )
        if not executable.is_file():
            return DiagnosticCheck(
                key="executable",
                title="Executable",
                status=CheckStatus.PROBLEM,
                summary="The configured path is not a regular file.",
                suggestion="Choose a game executable file in the game settings.",
                details=tuple(details),
            )
        if not os.access(executable, os.R_OK):
            return DiagnosticCheck(
                key="executable",
                title="Executable",
                status=CheckStatus.PROBLEM,
                summary="The configured executable is not readable.",
                suggestion=(
                    "Check file ownership and permissions. MGL does not change "
                    "permissions automatically."
                ),
                details=tuple(details),
            )
        if game.platform is Platform.LINUX and not os.access(
            executable, os.X_OK
        ):
            return DiagnosticCheck(
                key="executable",
                title="Executable",
                status=CheckStatus.PROBLEM,
                summary="The native executable is not executable.",
                suggestion=(
                    "Grant execute permission (for example, chmod +x on the "
                    "file). MGL does not change permissions."
                ),
                details=tuple(details),
            )
        return DiagnosticCheck(
            key="executable",
            title="Executable",
            status=CheckStatus.OK,
            summary="The configured executable was found.",
            details=tuple(details),
        )

    def _check_working_directory(
        self, game: Game, redact_paths: bool
    ) -> DiagnosticCheck:
        raw = game.working_directory.strip()
        if raw:
            try:
                directory = Path(raw).expanduser().resolve(strict=False)
            except (OSError, RuntimeError) as error:
                return DiagnosticCheck(
                    key="working_directory",
                    title="Working directory",
                    status=CheckStatus.PROBLEM,
                    summary="The configured working directory could not be resolved.",
                    suggestion="Edit the game and choose a valid directory.",
                    details=(self._redact_text(str(error), redact_paths),),
                )
        elif (
            game.platform is Platform.LINUX
            and is_bare_command(game.executable.strip())
        ):
            directory = self._home
        else:
            executable, error = self._locate_executable(game)
            if executable is None:
                return DiagnosticCheck(
                    key="working_directory",
                    title="Working directory",
                    status=CheckStatus.UNKNOWN,
                    summary=(
                        "No working directory is configured and the executable "
                        "could not be resolved to derive one."
                    ),
                    suggestion=(
                        "Fix the executable path, or set a working directory "
                        "explicitly."
                    ),
                    details=(
                        self._redact_text(error or "", redact_paths),
                    ),
                )
            directory = executable.parent

        details = [
            f"Path: {self._redact_text(str(directory), redact_paths)}",
            "Read-only check; no directory was created or changed.",
        ]
        if not directory.exists():
            return DiagnosticCheck(
                key="working_directory",
                title="Working directory",
                status=CheckStatus.PROBLEM,
                summary="The working directory does not exist.",
                suggestion=(
                    "Edit the game and choose an existing directory. MGL does "
                    "not create directories during diagnostics."
                ),
                details=tuple(details),
            )
        if not directory.is_dir():
            return DiagnosticCheck(
                key="working_directory",
                title="Working directory",
                status=CheckStatus.PROBLEM,
                summary="The working directory path is not a directory.",
                suggestion="Choose a directory in the game settings.",
                details=tuple(details),
            )
        if not os.access(directory, os.R_OK | os.X_OK):
            return DiagnosticCheck(
                key="working_directory",
                title="Working directory",
                status=CheckStatus.PROBLEM,
                summary="The working directory is not accessible.",
                suggestion=(
                    "Check directory ownership and permissions. MGL does not "
                    "change permissions automatically."
                ),
                details=tuple(details),
            )
        return DiagnosticCheck(
            key="working_directory",
            title="Working directory",
            status=CheckStatus.OK,
            summary="The working directory is available.",
            details=tuple(details),
        )

    def _check_process(
        self, game: Game, redact_paths: bool
    ) -> DiagnosticCheck:
        del redact_paths  # No path or command line is included here.
        if self._pidfiles_root is None:
            return DiagnosticCheck(
                key="process",
                title="Last launch",
                status=CheckStatus.INFO,
                summary="Process tracking is not available in this session.",
            )

        identity = read_pidfile(self._pidfiles_root, game.id)
        if identity is None:
            return DiagnosticCheck(
                key="process",
                title="Last launch",
                status=CheckStatus.INFO,
                summary="No launch record was found for this game.",
                details=("Diagnostics does not start or stop game processes.",),
            )
        verdict = verify_process(identity)
        details = (f"Recorded process ID: {identity.pid}",)
        if verdict == "alive":
            return DiagnosticCheck(
                key="process",
                title="Last launch",
                status=CheckStatus.OK,
                summary="A process recorded for this game is still running.",
                details=details,
            )
        if verdict == "inaccessible":
            return DiagnosticCheck(
                key="process",
                title="Last launch",
                status=CheckStatus.WARNING,
                summary=(
                    "A recorded process exists, but its state cannot be "
                    "verified right now."
                ),
                suggestion=(
                    "The game may still be running. Diagnostics leaves the "
                    "process and its launch record untouched."
                ),
                details=details,
            )
        if verdict == "reused":
            return DiagnosticCheck(
                key="process",
                title="Last launch",
                status=CheckStatus.WARNING,
                summary=(
                    "The recorded process ID now belongs to a different "
                    "process; the launch record is stale."
                ),
                suggestion=(
                    "The next launch replaces this record. No process was "
                    "signaled."
                ),
                details=details,
            )
        return DiagnosticCheck(
            key="process",
            title="Last launch",
            status=CheckStatus.INFO,
            summary="The last recorded process is no longer running.",
            details=details,
        )

    def _check_launch_output(
        self, game: Game, redact_paths: bool
    ) -> DiagnosticCheck:
        """Summarize the bounded Wine output captured during the last launch."""
        if self._launch_logs_root is None:
            return DiagnosticCheck(
                key="launch_output",
                title="Last launch output",
                status=CheckStatus.INFO,
                summary="Launch output capture is not available in this session.",
            )
        path = self._launch_logs_root / f"{game.id}.log"
        if not path.is_file():
            return DiagnosticCheck(
                key="launch_output",
                title="Last launch output",
                status=CheckStatus.INFO,
                summary=(
                    "No launch output has been recorded for this game yet. "
                    "Wine output is captured on the next launch."
                ),
                details=(
                    "Only the first "
                    f"{_MAX_LAUNCH_LOG_READ // 1024} KiB are retained.",
                ),
            )
        try:
            with path.open("rb") as handle:
                raw = handle.read(_MAX_LAUNCH_LOG_READ)
        except OSError as error:
            return DiagnosticCheck(
                key="launch_output",
                title="Last launch output",
                status=CheckStatus.WARNING,
                summary="The recorded launch output could not be read.",
                details=(self._redact_text(str(error), redact_paths),),
            )

        text = raw.decode("utf-8", errors="replace")
        exit_status: int | None = None
        if text.startswith("exit_status="):
            header, _, text = text.partition("\n")
            try:
                exit_status = int(header.split("=", 1)[1])
            except ValueError:
                exit_status = None
        analysis = analyze_wine_output(text, exit_status=exit_status)
        details = [
            f"Captured: {len(raw)} bytes (bounded)",
            f"Exit status: {exit_status if exit_status is not None else 'unknown'}",
        ]
        for finding in analysis.findings:
            details.append(
                f"{finding.label}: {finding.confidence} confidence"
            )
            details.extend(
                "  " + self._redact_text(sample, redact_paths)
                for sample in finding.samples
            )
        if analysis.has_problem:
            status = CheckStatus.PROBLEM
        elif analysis.has_warning:
            status = CheckStatus.WARNING
        elif analysis.findings:
            status = CheckStatus.INFO
        else:
            status = CheckStatus.OK
        suggestion = None
        if analysis.findings:
            suggestion = analysis.findings[0].suggestion
        return DiagnosticCheck(
            key="launch_output",
            title="Last launch output",
            status=status,
            summary=analysis.summary,
            suggestion=suggestion,
            details=tuple(details),
        )

    def _check_environment(
        self, game: Game, redact_paths: bool
    ) -> DiagnosticCheck:
        del redact_paths  # Only key names are ever shown.
        keys = sorted(game.environment)
        if not keys:
            return DiagnosticCheck(
                key="environment",
                title="Environment",
                status=CheckStatus.INFO,
                summary="No per-game environment variables are configured.",
            )
        # Values are deliberately never read into the report.
        return DiagnosticCheck(
            key="environment",
            title="Environment",
            status=CheckStatus.INFO,
            summary=(
                f"{len(keys)} per-game environment variable(s) are configured. "
                "Values are omitted from diagnostic reports."
            ),
            details=(f"Variables: {', '.join(keys)}",),
        )

    def _check_log(
        self, game: Game, redact_paths: bool
    ) -> DiagnosticCheck:
        del game
        if self._log_path is None:
            return DiagnosticCheck(
                key="log",
                title="Launcher log",
                status=CheckStatus.INFO,
                summary="The launcher log location is not available.",
            )
        details = [
            f"Path: {self._redact_text(str(self._log_path), redact_paths)}",
            "Log contents are not included because they may contain launch "
            "arguments.",
        ]
        if not self._log_path.exists():
            return DiagnosticCheck(
                key="log",
                title="Launcher log",
                status=CheckStatus.INFO,
                summary="No launcher log exists yet.",
                details=tuple(details),
            )
        try:
            if not self._log_path.is_file():
                return DiagnosticCheck(
                    key="log",
                    title="Launcher log",
                    status=CheckStatus.WARNING,
                    summary="The launcher log path is not a regular file.",
                    details=tuple(details),
                )
            size = self._log_path.stat().st_size
            readable = os.access(self._log_path, os.R_OK)
        except OSError as error:
            return DiagnosticCheck(
                key="log",
                title="Launcher log",
                status=CheckStatus.WARNING,
                summary="The launcher log could not be inspected.",
                details=tuple(details)
                + (self._redact_text(str(error), redact_paths),),
            )
        if not readable:
            return DiagnosticCheck(
                key="log",
                title="Launcher log",
                status=CheckStatus.WARNING,
                summary="The launcher log is not readable.",
                suggestion="Check the log file permissions. Nothing was changed.",
                details=tuple(details),
            )
        details.append(f"Size: {size} bytes")
        return DiagnosticCheck(
            key="log",
            title="Launcher log",
            status=CheckStatus.INFO,
            summary="The launcher log is available for manual inspection.",
            suggestion=(
                "Open the log outside MGL to see detailed launch errors."
            ),
            details=tuple(details),
        )

    def _check_disk(
        self, game: Game, redact_paths: bool
    ) -> DiagnosticCheck:
        del game
        target = self._existing_parent(self._data_directory)
        try:
            usage = shutil.disk_usage(target)
        except OSError as error:
            return DiagnosticCheck(
                key="disk",
                title="Disk space",
                status=CheckStatus.UNKNOWN,
                summary="Free space on the data filesystem could not be read.",
                details=(
                    self._redact_text(str(error), redact_paths),
                ),
            )
        free = usage.free
        summary = f"{free / 1024**3:.1f} GiB free on the data filesystem."
        details = (
            f"Path: {self._redact_text(str(target), redact_paths)}",
            f"Free: {free} bytes",
            f"Total: {usage.total} bytes",
        )
        return DiagnosticCheck(
            key="disk",
            title="Disk space",
            status=(
                CheckStatus.WARNING
                if free < _FREE_SPACE_WARNING
                else CheckStatus.OK
            ),
            summary=summary,
            suggestion=(
                "Free some space before installing or updating a game."
                if free < _FREE_SPACE_WARNING
                else None
            ),
            details=details,
        )

    def _prefix_for(self, game: Game) -> Path:
        """Return the prefix path that a launch would use, without creating it."""
        if game.wine_prefix_path and game.wine_prefix_path.strip():
            return Path(game.wine_prefix_path).expanduser()
        if game.wine_prefix_shared:
            return self._ownership.shared_prefix_path()
        return self._prefixes_root / game.id

    def _locate_executable(
        self, game: Game
    ) -> tuple[Path | None, str | None]:
        raw = game.executable.strip()
        if not raw:
            return None, "No executable is configured."
        try:
            if is_bare_command(raw):
                if game.platform is not Platform.LINUX:
                    return None, "Windows games require an executable path."
                located = self._which(raw)
                if not located:
                    return (
                        None,
                        f"The command was not found in PATH: {raw}",
                    )
                return Path(located).expanduser().resolve(strict=False), None
            return Path(raw).expanduser().resolve(strict=False), None
        except (OSError, RuntimeError) as error:
            return None, f"The executable path could not be resolved: {error}"

    def _wine_missing(
        self, summary: str, redact_paths: bool
    ) -> DiagnosticCheck:
        return DiagnosticCheck(
            key="wine",
            title="Wine",
            status=CheckStatus.PROBLEM,
            summary=self._redact_text(summary, redact_paths),
            suggestion=(
                "Install Wine or select a custom Wine executable in Settings. "
                "MGL does not install dependencies automatically."
            ),
            details=("Windows/Wine entries cannot launch without Wine.",),
        )

    def _registry_override_hints(
        self, registry_path: Path
    ) -> tuple[str, ...]:
        """Read DXVK-related DLL override hints without dumping the registry."""
        if not registry_path.is_file():
            return ()
        try:
            with registry_path.open(
                "r", encoding="utf-8", errors="replace"
            ) as handle:
                text = handle.read(_MAX_REGISTRY_READ)
        except OSError:
            return ()

        in_section = False
        hints: list[str] = []
        for line in text.splitlines():
            if line.startswith("["):
                in_section = line.lower().startswith(
                    "[software\\\\wine\\\\dlloverrides]"
                )
                continue
            if not in_section:
                continue
            match = _OVERRIDE_LINE.match(line)
            if match and match.group(1).lower() in _OVERRIDE_DLLS:
                hints.append(f"{match.group(1)}={match.group(2)}")
                if len(hints) >= 10:
                    break
        return tuple(hints)

    def _run(
        self, command: list[str], timeout: float
    ) -> tuple[subprocess.CompletedProcess | None, str | None]:
        """Run a bounded, read-only probe with an argument list."""
        # Never let an inherited WINEPREFIX direct a Wine probe at a real
        # prefix. ``wine --version`` is read-only, but this removes any doubt.
        environment = dict(os.environ)
        environment.pop("WINEPREFIX", None)
        try:
            completed = self._run_factory(
                command,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
                shell=False,
                env=environment,
            )
        except subprocess.TimeoutExpired:
            return None, "The command timed out."
        except (OSError, subprocess.SubprocessError) as error:
            return None, f"{type(error).__name__}: {error}"
        return completed, None

    @staticmethod
    def _coerce_output(value: str | bytes | None) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value

    @staticmethod
    def _truncate(text: str) -> str:
        return text if len(text) <= _MAX_OUTPUT else text[:_MAX_OUTPUT] + "…"

    @staticmethod
    def _vulkan_summary_lines(output: str) -> list[str]:
        wanted = ("apiVersion", "driverVersion", "deviceName", "driverName")
        lines: list[str] = []
        for raw_line in output.splitlines():
            line = raw_line.strip()
            if "=" not in line or not any(key in line for key in wanted):
                continue
            if len(line) > 200:
                line = line[:200] + "…"
            lines.append(line)
            if len(lines) >= 8:
                break
        return lines

    @staticmethod
    def _existing_parent(path: Path) -> Path:
        current = path
        while not current.exists() and current != current.parent:
            current = current.parent
        return current

    def _redact_text(self, text: str, redact_paths: bool) -> str:
        if not redact_paths or not text:
            return text
        home = str(self._home)
        if len(home) <= 1:
            return text
        # Match the home directory as a path prefix, not as a prefix of a
        # different user's directory name.
        return re.sub(
            re.escape(home) + r"(?![A-Za-z0-9._-])",
            "~",
            text,
        )


__all__ = [
    "CheckStatus",
    "DiagnosticCheck",
    "DiagnosticReport",
    "DiagnosticsService",
]
