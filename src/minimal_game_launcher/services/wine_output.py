"""Pattern recognition for bounded Wine launch output.

The launcher captures a small, bounded amount of a Wine game's combined output
while it runs. This module turns that text into a few recognizable findings so
diagnostics can suggest a cause without dumping raw logs or guessing.

Nothing here executes a process, writes to a prefix, or installs a dependency.
Confidence labels describe how strongly the text matches a known pattern, not
whether the pattern is the only possible cause.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_CAPTURE_BYTES = 64 * 1024
_MAX_SAMPLE = 200
_MAX_SAMPLES = 3


@dataclass(frozen=True, slots=True)
class OutputPattern:
    """One recognizable Wine output signature."""

    key: str
    label: str
    regex: re.Pattern[str]
    confidence: str
    severity: str
    suggestion: str


@dataclass(frozen=True, slots=True)
class OutputFinding:
    """A matched signature with bounded example lines."""

    key: str
    label: str
    confidence: str
    severity: str
    suggestion: str
    samples: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LaunchOutputAnalysis:
    """The recognized findings for one launch log."""

    findings: tuple[OutputFinding, ...]
    exit_status: int | None = None

    @property
    def summary(self) -> str:
        """Return a one-line description of the findings."""
        if not self.findings:
            return "No recognized Wine error patterns were found."
        return "Recognized: " + ", ".join(
            f"{finding.label} ({finding.confidence} confidence)"
            for finding in self.findings
        )

    @property
    def has_problem(self) -> bool:
        """Return whether any finding indicates a probable failure."""
        return any(finding.severity == "problem" for finding in self.findings)

    @property
    def has_warning(self) -> bool:
        """Return whether any finding indicates a possible failure."""
        return any(finding.severity in {"problem", "warning"} for finding in self.findings)


#: Ordered from the most actionable to the least specific. Matching is
#: case-insensitive; samples keep the original line for context.
_PATTERNS: tuple[OutputPattern, ...] = (
    OutputPattern(
        "permission",
        "Permission or access denial",
        re.compile(
            r"permission denied|access is denied|operation not permitted|"
            r"\bEACCES\b|\bEPERM\b",
            re.IGNORECASE,
        ),
        "high",
        "problem",
        "Check file and directory permissions. MGL never changes permissions "
        "automatically.",
    ),
    OutputPattern(
        "executable",
        "Executable or library not found",
        re.compile(
            r"no such file or directory|cannot find the file|"
            r"file not found|could not find .*\.(exe|dll)",
            re.IGNORECASE,
        ),
        "high",
        "problem",
        "Verify the game executable and its installation files still exist. "
        "Reinstall the game if files were removed.",
    ),
    OutputPattern(
        "prefix",
        "Wine prefix problem",
        re.compile(
            r"wine: .*prefix|wineserver.*(failed|not found)|"
            r"could not load kernel32|wineboot.*(failed|error)|"
            r"prefix .* is not a valid",
            re.IGNORECASE,
        ),
        "high",
        "problem",
        "The Wine prefix may be incomplete. Launch the game once to let MGL "
        "initialize it, or inspect the prefix with diagnostics.",
    ),
    OutputPattern(
        "dll",
        "Missing DLL",
        re.compile(
            r"err:module:import_dll|library .*\(.*\) not found|"
            r"dll not found|could not load .*\.dll",
            re.IGNORECASE,
        ),
        "high",
        "warning",
        "A required DLL is missing. Install the game's redistributable "
        "components in its prefix; MGL does not install them automatically.",
    ),
    OutputPattern(
        "msvc",
        "Microsoft Visual C++ runtime",
        re.compile(
            r"\bMSVCP\d+|\bMSVCR\d+|\bVCRUNTIME\d+|Visual C\+\+",
            re.IGNORECASE,
        ),
        "medium",
        "warning",
        "The game may need a Microsoft Visual C++ runtime. Install the "
        "matching redistributable into the game's prefix.",
    ),
    OutputPattern(
        "dotnet",
        ".NET runtime",
        re.compile(
            r"\.NET Framework|\bmscoree\b|\bCLR\b|\bdotnet\b",
            re.IGNORECASE,
        ),
        "medium",
        "warning",
        "The game may need a .NET runtime. Install a compatible version into "
        "the game's prefix.",
    ),
    OutputPattern(
        "d3d",
        "Direct3D",
        re.compile(
            r"\bd3d(9|10|11|12)\b|Direct3D|CreateDevice",
            re.IGNORECASE,
        ),
        "medium",
        "warning",
        "Direct3D initialization failed. Check the prefix's Direct3D "
        "components and the graphics driver.",
    ),
    OutputPattern(
        "dxgi",
        "DXGI",
        re.compile(r"\bDXGI_ERROR|\bdxgi\b", re.IGNORECASE),
        "medium",
        "warning",
        "DXGI reported a problem. This often indicates a graphics driver or "
        "display-mode mismatch.",
    ),
    OutputPattern(
        "dxvk",
        "DXVK",
        re.compile(r"\bdxvk\b", re.IGNORECASE),
        "low",
        "info",
        "DXVK messages are informational. Confirm the prefix's DXVK "
        "installation if rendering fails.",
    ),
    OutputPattern(
        "vkd3d",
        "VKD3D",
        re.compile(r"\bvkd3d\b", re.IGNORECASE),
        "low",
        "info",
        "VKD3D messages are informational. Direct3D 12 support depends on the "
        "prefix and driver.",
    ),
    OutputPattern(
        "vulkan",
        "Vulkan",
        re.compile(r"\bvulkan\b|\bVK_ERROR|\bVK_ICD\b", re.IGNORECASE),
        "medium",
        "warning",
        "Vulkan reported a problem. Check the GPU driver and Vulkan loader; "
        "MGL does not install drivers.",
    ),
    OutputPattern(
        "crash",
        "Unhandled exception or crash",
        re.compile(
            r"unhandled exception|page fault|segmentation fault|"
            r"wine: .*crash|backtrace:",
            re.IGNORECASE,
        ),
        "high",
        "problem",
        "The game or Wine crashed. Run diagnostics and check the launcher log "
        "for the last recorded launch.",
    ),
)


def analyze_wine_output(
    text: str, *, exit_status: int | None = None
) -> LaunchOutputAnalysis:
    """Return bounded findings for captured launch output.

    Wine's ``fixme:`` and ``trace:`` lines are implementation chatter that
    appear during normal runs (Direct3D, Vulkan, and MSVC names show up there
    constantly), so they are excluded from matching. Real failures use ``err:``,
    ``warn:``, or the game's own output.
    """
    lines = text.splitlines()
    searchable = [
        line
        for line in lines
        if not line.lstrip().lower().startswith(("fixme:", "trace:"))
    ]
    findings: list[OutputFinding] = []
    for pattern in _PATTERNS:
        samples: list[str] = []
        for line in searchable:
            if not pattern.regex.search(line):
                continue
            sample = line.strip()
            if len(sample) > _MAX_SAMPLE:
                sample = sample[:_MAX_SAMPLE] + "…"
            if sample and sample not in samples:
                samples.append(sample)
            if len(samples) >= _MAX_SAMPLES:
                break
        if samples:
            findings.append(
                OutputFinding(
                    key=pattern.key,
                    label=pattern.label,
                    confidence=pattern.confidence,
                    severity=pattern.severity,
                    suggestion=pattern.suggestion,
                    samples=tuple(samples),
                )
            )
    if exit_status not in (None, 0) and not any(
        finding.severity == "problem" for finding in findings
    ):
        findings.append(
            OutputFinding(
                key="exit_status",
                label=f"Non-zero exit status ({exit_status})",
                confidence="high",
                severity="warning",
                suggestion=(
                    "The game exited with an error status. Check the other "
                    "findings and the launcher log."
                ),
                samples=(),
            )
        )
    return LaunchOutputAnalysis(findings=tuple(findings), exit_status=exit_status)


__all__ = [
    "LaunchOutputAnalysis",
    "MAX_CAPTURE_BYTES",
    "OutputFinding",
    "analyze_wine_output",
]
