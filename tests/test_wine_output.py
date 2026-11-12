"""Tests for bounded Wine output pattern recognition.

No process is started: the analyzer only inspects text.
"""

from __future__ import annotations

import pytest

from minimal_game_launcher.services.wine_output import (
    MAX_CAPTURE_BYTES,
    analyze_wine_output,
)


def test_clean_output_has_no_findings() -> None:
    analysis = analyze_wine_output("fixme: nothing important\n", exit_status=0)

    assert analysis.findings == ()
    assert analysis.has_problem is False
    assert "No recognized" in analysis.summary


def test_permission_and_missing_library_are_reported() -> None:
    analysis = analyze_wine_output(
        "wine: cannot find the file\n"
        "err:module:import_dll Library MSVCP140.dll not found\n"
    )

    keys = {finding.key for finding in analysis.findings}
    assert {"executable", "dll", "msvc"} <= keys
    assert analysis.has_problem is True
    assert analysis.findings[0].samples


def test_samples_are_bounded_and_deduplicated() -> None:
    long_line = "err:module:import_dll " + "x" * 500
    analysis = analyze_wine_output(
        "\n".join([long_line, long_line, "err:module:import_dll short"])
    )

    dll = next(f for f in analysis.findings if f.key == "dll")
    assert len(dll.samples) == 2
    assert all(len(sample) <= 201 for sample in dll.samples)
    assert dll.samples[0].endswith("…")


def test_crash_pattern_is_a_problem() -> None:
    analysis = analyze_wine_output(
        "wine: Unhandled exception: page fault\nbacktrace:\n"
    )

    crash = next(f for f in analysis.findings if f.key == "crash")
    assert crash.severity == "problem"
    assert analysis.has_problem is True


def test_non_zero_exit_adds_a_finding_only_without_a_problem() -> None:
    warning_only = analyze_wine_output("dxvk: info\n", exit_status=1)
    keys = {finding.key for finding in warning_only.findings}
    assert "exit_status" in keys

    problem = analyze_wine_output("permission denied\n", exit_status=1)
    assert "exit_status" not in {finding.key for finding in problem.findings}


def test_capture_limit_is_bounded() -> None:
    assert 0 < MAX_CAPTURE_BYTES <= 1024 * 1024


def test_fixme_chatter_is_not_reported_as_a_failure() -> None:
    """Normal Wine fixme traces name D3D/Vulkan/MSVC without any problem."""
    analysis = analyze_wine_output(
        "fixme:d3d11:d3d11_device_create Ignoring D3D11_CREATE_DEVICE_DEBUG\n"
        "fixme:dxgi:dxgi_factory_CreateSwapChain Ignoring swap effect\n"
        "fixme:vulkan:wine_vk_instance_load_physical_devices No Vulkan loader\n"
        "fixme:msvcp:_Locinfo__Locinfo_ctor_cat_cstr (1 0x32f0c0) semi-stub\n"
        "fixme:dotnet:CLR created\n"
        "trace:module:import_dll loaded MSVCP140.dll\n",
        exit_status=0,
    )

    assert analysis.findings == ()
    assert analysis.has_warning is False


def test_real_error_lines_are_still_matched() -> None:
    analysis = analyze_wine_output(
        "err:module:import_dll Library MSVCP140.dll (which is needed by "
        "L\"game.exe\") not found\n"
        "warn:vulkan:instance_create Failed to create Vulkan instance\n"
    )

    keys = {finding.key for finding in analysis.findings}
    assert {"dll", "msvc", "vulkan"} <= keys


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("err:module:import_dll Library foo.dll not found", "dll"),
        ("err:module:import_dll Library MSVCP140.dll not found", "msvc"),
        ("err:module:import_dll Library mscoree.dll not found", "dotnet"),
        ("err:d3d11:device_create Failed to create device", "d3d"),
        ("err:dxgi:factory failed: DXGI_ERROR_INVALID_CALL", "dxgi"),
        ("err:vkd3d:create_device failed", "vkd3d"),
        ("err:dxvk:init failed", "dxvk"),
        ("err:vulkan:instance_create failed", "vulkan"),
        ("wine: failed to open prefix", "prefix"),
        ("wine: cannot find the file game.exe", "executable"),
        ("permission denied", "permission"),
    ],
)
def test_each_known_error_pattern_is_recognized(
    line: str, expected: str
) -> None:
    analysis = analyze_wine_output(line, exit_status=1)

    assert expected in {finding.key for finding in analysis.findings}


def test_unknown_errors_are_not_guessed() -> None:
    analysis = analyze_wine_output(
        "something unexpected happened\n", exit_status=1
    )

    keys = {finding.key for finding in analysis.findings}
    assert keys == {"exit_status"}
    assert analysis.has_problem is False


