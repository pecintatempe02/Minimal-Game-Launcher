"""Tests for read-only, on-demand diagnostics.

All host probes are injected. Tests never run Wine, vulkaninfo, or a game, and
they use temporary directories for prefixes, logs, and reports.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from minimal_game_launcher import APP_VERSION
from minimal_game_launcher.models import Game, Platform
from minimal_game_launcher.services import diagnostics
from minimal_game_launcher.services.launch_manager import PREFIX_READY_MARKER
from minimal_game_launcher.services.diagnostics import (
    CheckStatus,
    DiagnosticsService,
)
from minimal_game_launcher.services.ownership import (
    OwnershipService,
    OwnershipStatus,
    ResourceKind,
)
from minimal_game_launcher.services.process_identity import ProcessIdentity
from minimal_game_launcher.settings import SettingsStore

GAME_ID = "game-1"
WINE_VERSION = "wine-9.0\n"
VULKAN_SUMMARY = (
    "==========\n"
    "Vulkan Instance Version: 1.3.280\n"
    "Devices:\n"
    "GPU0:\n"
    "\tapiVersion = 1.3.0\n"
    "\tdeviceName = Fake GPU\n"
)


class FakeRunner:
    """Return canned output and record every command line and option."""

    def __init__(
        self,
        *,
        wine_output: str = WINE_VERSION,
        vulkan_output: str = VULKAN_SUMMARY,
        returncode: int = 0,
        error: Exception | None = None,
    ) -> None:
        self.wine_output = wine_output
        self.vulkan_output = vulkan_output
        self.returncode = returncode
        self.error = error
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(
        self, command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess:
        self.calls.append((list(command), kwargs))
        if self.error is not None:
            raise self.error
        output = (
            self.vulkan_output if "--summary" in command else self.wine_output
        )
        return subprocess.CompletedProcess(
            command, self.returncode, output, ""
        )


def make_game(tmp_path: Path, **overrides: object) -> Game:
    """Build a library entry whose executable and working directory exist."""
    platform = overrides.pop("platform", Platform.WINDOWS)
    executable = overrides.pop("executable", None)
    if executable is None:
        suffix = ".exe" if platform is Platform.WINDOWS else ""
        executable_path = tmp_path / f"game{suffix}"
        executable_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable_path.chmod(0o755)
        executable = str(executable_path)
    prefix = overrides.pop(
        "prefix", tmp_path / "data" / "prefixes" / GAME_ID
    )
    values: dict[str, object] = {
        "id": GAME_ID,
        "name": "Test Game",
        "platform": platform,
        "executable": executable,
        "working_directory": str(tmp_path),
        "launch_arguments": "",
        "cover_path": None,
        "wine_prefix_path": (
            str(prefix) if platform is Platform.WINDOWS else None
        ),
        "gamemode_enabled": False,
        "mangohud_enabled": False,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "environment": {},
    }
    values.update(overrides)
    return Game(**values)  # type: ignore[arg-type]


def make_service(
    tmp_path: Path,
    *,
    which_map: dict[str, str | None] | None = None,
    runner: FakeRunner | None = None,
    finder: object | None = None,
    home: Path | None = None,
) -> DiagnosticsService:
    """Build a service whose filesystem roots all live under ``tmp_path``."""
    home_directory = home or (tmp_path / "home")
    home_directory.mkdir(parents=True, exist_ok=True)
    lookups = {"wine": "/usr/bin/wine"} if which_map is None else which_map
    return DiagnosticsService(
        tmp_path / "data" / "prefixes",
        SettingsStore(tmp_path / "config" / "settings.json"),
        pidfiles_root=tmp_path / "state" / "processes",
        log_path=tmp_path / "state" / "launcher.log",
        data_directory=tmp_path / "data",
        home=home_directory,
        which=lambda name: lookups.get(name),
        run=runner or FakeRunner(),
        vulkan_library=finder or (lambda _name: None),
    )


def check(report, key: str):
    """Return one check by key."""
    return next(item for item in report.checks if item.key == key)


def create_prefix(tmp_path: Path, *, initialized: bool = True) -> Path:
    """Create a plausible managed prefix under the temporary data directory."""
    prefix = tmp_path / "data" / "prefixes" / GAME_ID
    prefix.mkdir(parents=True)
    if initialized:
        (prefix / "system.reg").write_text("fake registry\n", encoding="utf-8")
        (prefix / "drive_c").mkdir()
        (prefix / PREFIX_READY_MARKER).write_text("ready\n", encoding="utf-8")
    return prefix


def test_wine_missing_reports_problem(tmp_path: Path) -> None:
    service = make_service(tmp_path, which_map={})
    report = service.generate(make_game(tmp_path))

    wine = check(report, "wine")
    assert wine.status is CheckStatus.PROBLEM
    assert "not found" in wine.summary
    assert "Install Wine" in (wine.suggestion or "")
    assert report.overall_status is CheckStatus.PROBLEM


def test_wine_available_reports_version_without_a_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WINEPREFIX", str(tmp_path / "unrelated-prefix"))
    runner = FakeRunner()
    service = make_service(tmp_path, runner=runner)
    report = service.generate(make_game(tmp_path))

    wine = check(report, "wine")
    assert wine.status is CheckStatus.OK
    assert "wine-9.0" in wine.summary
    assert runner.calls
    command, options = runner.calls[0]
    assert command == ["/usr/bin/wine", "--version"]
    assert options["shell"] is False
    environment = options["env"]
    assert isinstance(environment, dict)
    assert "WINEPREFIX" not in environment


def test_wine_timeout_is_reported_as_warning(tmp_path: Path) -> None:
    runner = FakeRunner(
        error=subprocess.TimeoutExpired(["wine", "--version"], timeout=1)
    )
    service = make_service(tmp_path, runner=runner)
    wine = check(service.generate(make_game(tmp_path)), "wine")

    assert wine.status is CheckStatus.WARNING
    assert "timed out" in " ".join(wine.details)


def test_linux_entry_does_not_probe_wine(tmp_path: Path) -> None:
    runner = FakeRunner()
    service = make_service(tmp_path, which_map={}, runner=runner)
    game = make_game(tmp_path, platform=Platform.LINUX)

    wine = check(service.generate(game), "wine")
    assert wine.status is CheckStatus.INFO
    assert "not used" in wine.summary
    assert runner.calls == []


def test_managed_prefix_is_classified_and_missing_is_a_warning(
    tmp_path: Path,
) -> None:
    prefix = tmp_path / "data" / "prefixes" / GAME_ID
    service = make_service(tmp_path)
    report = service.generate(make_game(tmp_path, prefix=prefix))

    ownership = OwnershipService(
        tmp_path / "data" / "prefixes",
        tmp_path / "data" / "games",
        tmp_path / "data" / "installers",
    )
    assert ownership.classify(
        ResourceKind.PREFIX, GAME_ID, prefix
    ).status is OwnershipStatus.MISSING
    prefix_check = check(report, "prefix")
    assert prefix_check.status is CheckStatus.WARNING
    assert "not been created" in prefix_check.summary
    assert "Ownership: missing" in prefix_check.details


def test_valid_managed_prefix_is_ok(tmp_path: Path) -> None:
    prefix = create_prefix(tmp_path)
    report = make_service(tmp_path).generate(
        make_game(tmp_path, prefix=prefix)
    )

    prefix_check = check(report, "prefix")
    assert prefix_check.status is CheckStatus.OK
    assert "managed" in prefix_check.summary
    assert "drive_c: present" in prefix_check.details


def test_external_prefix_is_read_only_and_classified_external(
    tmp_path: Path,
) -> None:
    external = tmp_path / "external-prefix"
    (external / "drive_c").mkdir(parents=True)
    (external / "system.reg").write_text("registry\n", encoding="utf-8")
    (external / PREFIX_READY_MARKER).write_text("ready\n", encoding="utf-8")
    report = make_service(tmp_path).generate(
        make_game(tmp_path, prefix=external)
    )

    prefix_check = check(report, "prefix")
    assert prefix_check.status is CheckStatus.OK
    assert "external" in prefix_check.summary
    assert "Ownership: external" in prefix_check.details
    assert "never modifies external prefixes" in (
        prefix_check.suggestion or ""
    )


def test_prefix_that_is_a_symlink_is_not_managed(tmp_path: Path) -> None:
    real = create_prefix(tmp_path)
    link = tmp_path / "linked-prefix"
    link.symlink_to(real, target_is_directory=True)

    ownership = OwnershipService(
        tmp_path / "data" / "prefixes",
        tmp_path / "data" / "games",
        tmp_path / "data" / "installers",
    )
    assert ownership.classify(
        ResourceKind.PREFIX, GAME_ID, link
    ).status is OwnershipStatus.INVALID
    prefix_check = check(
        make_service(tmp_path).generate(make_game(tmp_path, prefix=link)),
        "prefix",
    )
    assert prefix_check.status is CheckStatus.PROBLEM
    assert "symbolic link" in " ".join(prefix_check.details)


def test_inaccessible_prefix_is_a_problem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix = create_prefix(tmp_path)
    monkeypatch.setattr(
        diagnostics.os,
        "access",
        lambda path, mode: Path(path) != prefix,
    )

    prefix_check = check(
        make_service(tmp_path).generate(
            make_game(tmp_path, prefix=prefix)
        ),
        "prefix",
    )
    assert prefix_check.status is CheckStatus.PROBLEM
    assert "not accessible" in prefix_check.summary


def test_uninitialized_prefix_is_a_warning(tmp_path: Path) -> None:
    prefix = create_prefix(tmp_path, initialized=False)
    prefix_check = check(
        make_service(tmp_path).generate(
            make_game(tmp_path, prefix=prefix)
        ),
        "prefix",
    )
    assert prefix_check.status is CheckStatus.WARNING
    assert "not been initialized" in prefix_check.summary


def test_dxvk_absent_is_reported_without_installing_anything(
    tmp_path: Path,
) -> None:
    prefix = create_prefix(tmp_path)
    report = make_service(tmp_path).generate(
        make_game(tmp_path, prefix=prefix)
    )

    dxvk = check(report, "dxvk")
    assert dxvk.status is CheckStatus.INFO
    assert "No DXVK DLL files" in dxvk.summary
    assert "does not install" in (dxvk.suggestion or "")
    assert not (prefix / "system32").exists()


def test_dxvk_present_is_detected(tmp_path: Path) -> None:
    prefix = create_prefix(tmp_path)
    for folder in ("system32", "syswow64"):
        (prefix / folder).mkdir()
        for name in ("dxgi.dll", "d3d11.dll"):
            (prefix / folder / name).write_bytes(b"fake dll")
    (prefix / "dxvk.conf").write_text("dxgi.maxFrameRate = 60\n")

    dxvk = check(
        make_service(tmp_path).generate(
            make_game(tmp_path, prefix=prefix)
        ),
        "dxvk",
    )
    assert dxvk.status is CheckStatus.OK
    assert "dxvk.conf: present" in dxvk.details
    assert "system32: dxgi.dll, d3d11.dll" in dxvk.details


def test_dxvk_override_hints_are_caveated(tmp_path: Path) -> None:
    prefix = create_prefix(tmp_path)
    (prefix / "user.reg").write_text(
        'WINE REGISTRY Version 2\n'
        '[Software\\\\Wine\\\\DllOverrides] 123\n'
        '"d3d11"="native"\n'
        '"dxgi"="native,builtin"\n'
        '[Software\\\\Other] 456\n'
        '"secret"="not-reported"\n',
        encoding="utf-8",
    )

    dxvk = check(
        make_service(tmp_path).generate(
            make_game(tmp_path, prefix=prefix)
        ),
        "dxvk",
    )
    assert dxvk.status is CheckStatus.WARNING
    assert "d3d11=native" in " ".join(dxvk.details)
    assert "not proof" in " ".join(dxvk.details)
    assert "secret" not in " ".join(dxvk.details)


def test_vulkan_available_from_vulkaninfo(tmp_path: Path) -> None:
    runner = FakeRunner()
    service = make_service(
        tmp_path, which_map={"vulkaninfo": "/usr/bin/vulkaninfo"}, runner=runner
    )
    vulkan = check(service.generate(make_game(tmp_path)), "vulkan")

    assert vulkan.status is CheckStatus.OK
    assert vulkan.summary == "Vulkan is available."
    assert any("deviceName = Fake GPU" in line for line in vulkan.details)
    assert runner.calls[0][0] == ["/usr/bin/vulkaninfo", "--summary"]


def test_vulkan_loader_only_is_available(tmp_path: Path) -> None:
    service = make_service(
        tmp_path,
        which_map={},
        finder=lambda _name: "libvulkan.so.1",
    )
    vulkan = check(service.generate(make_game(tmp_path)), "vulkan")

    assert vulkan.status is CheckStatus.OK
    assert "loader" in vulkan.summary.lower()
    assert "vulkaninfo is not installed" in (vulkan.suggestion or "")


def test_vulkan_unavailable_is_a_warning(tmp_path: Path) -> None:
    service = make_service(
        tmp_path, which_map={}, finder=lambda _name: None
    )
    vulkan = check(service.generate(make_game(tmp_path)), "vulkan")

    assert vulkan.status is CheckStatus.WARNING
    assert "No Vulkan loader" in vulkan.summary
    assert "does not install" in (vulkan.suggestion or "")


def test_vulkaninfo_failure_is_a_warning(tmp_path: Path) -> None:
    runner = FakeRunner(returncode=3)
    service = make_service(
        tmp_path, which_map={"vulkaninfo": "/usr/bin/vulkaninfo"}, runner=runner
    )
    vulkan = check(service.generate(make_game(tmp_path)), "vulkan")

    assert vulkan.status is CheckStatus.WARNING
    assert "reported a failure" in vulkan.summary


def test_missing_and_available_executables(tmp_path: Path) -> None:
    missing = make_game(
        tmp_path,
        platform=Platform.LINUX,
        executable=str(tmp_path / "missing"),
    )
    assert check(make_service(tmp_path).generate(missing), "executable").status is (
        CheckStatus.PROBLEM
    )

    executable = tmp_path / "native-game"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    available = make_game(
        tmp_path, platform=Platform.LINUX, executable=str(executable)
    )
    assert check(
        make_service(tmp_path).generate(available), "executable"
    ).status is CheckStatus.OK


def test_bare_command_is_resolved_through_which(tmp_path: Path) -> None:
    command = tmp_path / "bin" / "flatpak"
    command.parent.mkdir()
    command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    command.chmod(0o755)
    service = make_service(tmp_path, which_map={"flatpak": str(command)})

    game = make_game(
        tmp_path, platform=Platform.LINUX, executable="flatpak"
    )
    executable = check(service.generate(game), "executable")
    assert executable.status is CheckStatus.OK
    assert str(command) in executable.details[0]


def test_non_executable_linux_file_is_a_problem(tmp_path: Path) -> None:
    executable = tmp_path / "not-executable"
    executable.write_text("data\n", encoding="utf-8")
    executable.chmod(0o644)
    game = make_game(
        tmp_path, platform=Platform.LINUX, executable=str(executable)
    )

    check_result = check(make_service(tmp_path).generate(game), "executable")
    assert check_result.status is CheckStatus.PROBLEM
    assert "not executable" in check_result.summary


def test_working_directory_missing_and_available(tmp_path: Path) -> None:
    missing = make_game(
        tmp_path, working_directory=str(tmp_path / "missing")
    )
    assert check(
        make_service(tmp_path).generate(missing), "working_directory"
    ).status is CheckStatus.PROBLEM

    present = make_game(tmp_path, working_directory=str(tmp_path))
    assert check(
        make_service(tmp_path).generate(present), "working_directory"
    ).status is CheckStatus.OK


def test_report_text_contains_summary_and_technical_details(
    tmp_path: Path,
) -> None:
    prefix = create_prefix(tmp_path)
    report = make_service(tmp_path).generate(
        make_game(tmp_path, prefix=prefix)
    )
    text = report.to_text()

    assert "Minimal Game Launcher diagnostic report" in text
    assert f"Launcher version: {APP_VERSION}" in text
    assert "Game: Test Game" in text
    assert "[OK] Wine" in text
    assert "Technical details:" in text
    assert f"Overall: {report.overall_status.label}" in text


def test_report_export_writes_a_local_plain_text_file(tmp_path: Path) -> None:
    report = make_service(tmp_path).generate(make_game(tmp_path))
    destination = tmp_path / "report.txt"

    report.save(destination)

    assert destination.read_text(encoding="utf-8") == report.to_text()


def test_home_paths_are_redacted_by_default(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    executable = home / "game"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    game = make_game(
        tmp_path,
        platform=Platform.LINUX,
        executable=str(executable),
        working_directory=str(home),
    )
    service = make_service(tmp_path, home=home)

    redacted = service.generate(game, redact_paths=True).to_text()
    assert str(home) not in redacted
    assert "~/game" in redacted
    assert "home directory replaced with ~" in redacted

    full = service.generate(game, redact_paths=False).to_text()
    assert str(home) in full
    assert "full paths included" in full


def test_environment_values_are_never_reported(tmp_path: Path) -> None:
    game = make_game(
        tmp_path,
        environment={"SECRET_TOKEN": "super-secret-value", "LANG": "en_US"},
    )
    text = make_service(tmp_path).generate(game).to_text()

    assert "super-secret-value" not in text
    assert "SECRET_TOKEN" in text
    assert "Values are omitted" in text


def test_a_failing_check_does_not_hide_other_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = make_service(tmp_path)

    def boom(_game: Game, _redact: bool) -> None:
        raise RuntimeError("unexpected probe failure")

    monkeypatch.setattr(service, "_check_disk", boom)
    report = service.generate(make_game(tmp_path))

    disk = check(report, "disk")
    assert disk.status is CheckStatus.UNKNOWN
    assert "could not be completed (RuntimeError)" in disk.summary
    assert check(report, "executable").status is CheckStatus.OK


def test_process_record_is_reported_without_signaling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        diagnostics,
        "read_pidfile",
        lambda _path, _game_id: ProcessIdentity(pid=123, starttime="42"),
    )
    monkeypatch.setattr(diagnostics, "verify_process", lambda _record: "alive")
    service = make_service(tmp_path)

    process = check(service.generate(make_game(tmp_path)), "process")
    assert process.status is CheckStatus.OK
    assert process.details == ("Recorded process ID: 123",)

    monkeypatch.setattr(diagnostics, "read_pidfile", lambda _path, _id: None)
    no_record = check(service.generate(make_game(tmp_path)), "process")
    assert no_record.status is CheckStatus.INFO


def test_diagnostics_are_read_only(tmp_path: Path) -> None:
    prefix = create_prefix(tmp_path)
    (prefix / "system32").mkdir()
    (prefix / "system32" / "dxgi.dll").write_bytes(b"dll")
    (prefix / "user.reg").write_text(
        '[Software\\\\Wine\\\\DllOverrides] 1\n"dxgi"="native"\n',
        encoding="utf-8",
    )
    log = tmp_path / "state" / "launcher.log"
    log.parent.mkdir()
    log.write_text("existing log\n", encoding="utf-8")
    service = make_service(tmp_path)
    game = make_game(tmp_path, prefix=prefix)

    def snapshot() -> dict[str, tuple[bytes, int]]:
        return {
            str(path.relative_to(tmp_path)): (path.read_bytes(), path.stat().st_mode)
            for path in tmp_path.rglob("*")
            if path.is_file()
        }

    before = snapshot()
    service.generate(game)
    assert snapshot() == before
    assert not (prefix / "system32" / "d3d11.dll").exists()
    assert (prefix / "system.reg").read_text(encoding="utf-8") == "fake registry\n"
