"""Tests for read-only ISO mounting and verified mount ownership."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from minimal_game_launcher.errors import IsoMountError
from minimal_game_launcher.services.iso_mount import IsoMountService, MountedIso


def _completed(
    arguments: list[str], returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        arguments, returncode, stdout=stdout, stderr=stderr
    )


class _FakeUdisks:
    """Record argv calls and simulate UDisks without touching real devices."""

    def __init__(
        self,
        mountinfo: Path,
        mount_point: Path,
        *,
        mount_returncode: int = 0,
        mounted_options: str = "ro",
        mount_stderr: str = "",
    ) -> None:
        self.mountinfo = mountinfo
        self.mount_point = mount_point
        self.mount_returncode = mount_returncode
        self.mounted_options = mounted_options
        self.mount_stderr = mount_stderr
        self.calls: list[list[str]] = []
        self.kwargs: list[dict[str, object]] = []

    def __call__(self, arguments, **kwargs):
        self.calls.append(list(arguments))
        self.kwargs.append(dict(kwargs))
        command = arguments[1]
        if command == "loop-setup":
            return _completed(
                list(arguments),
                stdout=(
                    f"Mapped file {arguments[-1]} as /dev/loop7.\n"
                ),
            )
        if command == "mount":
            if self.mount_returncode != 0:
                return _completed(
                    list(arguments),
                    returncode=self.mount_returncode,
                    stderr=self.mount_stderr,
                )
            self.mount_point.mkdir(parents=True, exist_ok=True)
            self.mountinfo.write_text(
                "36 25 7:7 / "
                f"{self.mount_point} "
                f"{self.mounted_options},nosuid,nodev - iso9660 /dev/loop7 "
                f"{self.mounted_options}\n",
                encoding="utf-8",
            )
            return _completed(list(arguments))
        if command == "unmount":
            self.mountinfo.write_text("", encoding="utf-8")
            return _completed(list(arguments))
        if command == "loop-delete":
            return _completed(list(arguments))
        raise AssertionError(f"unexpected UDisks command: {command}")


def _service(
    tmp_path: Path,
    *,
    mount_returncode: int = 0,
    mounted_options: str = "ro",
    mount_stderr: str = "",
    loop_ro: str = "1",
) -> tuple[IsoMountService, _FakeUdisks, Path, Path]:
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text("", encoding="utf-8")
    mount_point = tmp_path / "media" / "CDROM"
    sys_block = tmp_path / "sys" / "block"
    (sys_block / "loop7").mkdir(parents=True)
    (sys_block / "loop7" / "ro").write_text(loop_ro, encoding="utf-8")
    fake = _FakeUdisks(
        mountinfo,
        mount_point,
        mount_returncode=mount_returncode,
        mounted_options=mounted_options,
        mount_stderr=mount_stderr,
    )
    iso = tmp_path / "disc.iso"
    iso.write_bytes(b"ISO")
    service = IsoMountService(
        udisksctl="/usr/bin/udisksctl",
        run_factory=fake,
        mountinfo_path=mountinfo,
        sys_block_path=sys_block,
    )
    return service, fake, iso, mount_point


def test_mount_uses_read_only_arguments_and_verifies_ownership(
    tmp_path: Path,
) -> None:
    service, fake, iso, mount_point = _service(tmp_path)

    mounted = service.mount(iso)

    assert mounted == MountedIso(
        iso_path=iso.resolve(),
        loop_device="/dev/loop7",
        mount_point=mount_point,
    )
    loop_setup = fake.calls[0]
    assert loop_setup[1] == "loop-setup"
    assert "--read-only" in loop_setup
    assert "--no-partition-scan" in loop_setup
    assert "--no-user-interaction" in loop_setup
    mount_call = fake.calls[1]
    assert mount_call[1] == "mount"
    assert mount_call[mount_call.index("--options") + 1] == "ro"
    assert all(call_kwargs.get("shell") is False for call_kwargs in fake.kwargs)


def test_unmount_releases_only_the_recorded_mount(tmp_path: Path) -> None:
    service, fake, iso, mount_point = _service(tmp_path)
    mounted = service.mount(iso)

    service.unmount(mounted)

    assert ["/usr/bin/udisksctl", "unmount", "--no-user-interaction", "-b", "/dev/loop7"] in fake.calls
    assert ["/usr/bin/udisksctl", "loop-delete", "--no-user-interaction", "-b", "/dev/loop7"] in fake.calls


def test_unmount_refuses_a_mount_this_service_did_not_create(
    tmp_path: Path,
) -> None:
    service, fake, iso, mount_point = _service(tmp_path)
    forged = MountedIso(
        iso_path=iso, loop_device="/dev/loop7", mount_point=mount_point
    )

    with pytest.raises(IsoMountError, match="did not create"):
        service.unmount(forged)
    assert not any(call[1] == "unmount" for call in fake.calls)


def test_unmount_refuses_when_target_no_longer_matches(tmp_path: Path) -> None:
    service, fake, iso, mount_point = _service(tmp_path)
    mounted = service.mount(iso)
    fake.mountinfo.write_text(
        "36 25 7:7 / /run/elsewhere ro - iso9660 /dev/loop7 ro\n",
        encoding="utf-8",
    )

    with pytest.raises(IsoMountError, match="somewhere"):
        service.unmount(mounted)
    assert not any(call[1] == "unmount" for call in fake.calls)


def test_mount_failure_releases_the_loop_device(tmp_path: Path) -> None:
    service, fake, iso, _mount_point = _service(tmp_path, mount_returncode=1)

    with pytest.raises(IsoMountError, match="could not mount"):
        service.mount(iso)

    assert ["/usr/bin/udisksctl", "loop-delete", "--no-user-interaction", "-b", "/dev/loop7"] in fake.calls


def test_filesystem_detection_failure_is_actionable_and_released(
    tmp_path: Path,
) -> None:
    service, fake, iso, _mount_point = _service(
        tmp_path,
        mount_returncode=1,
        mount_stderr=(
            "Object /org/freedesktop/UDisks2/block_devices/loop7 is not a "
            "mountable filesystem."
        ),
    )

    with pytest.raises(IsoMountError, match="partitioned or hybrid"):
        service.mount(iso)

    assert ["/usr/bin/udisksctl", "loop-delete", "--no-user-interaction", "-b", "/dev/loop7"] in fake.calls


def test_mount_rejects_a_rw_mount(tmp_path: Path) -> None:
    service, fake, iso, _mount_point = _service(tmp_path, mounted_options="rw")

    with pytest.raises(IsoMountError, match="read-only"):
        service.mount(iso)
    assert any(call[1] == "loop-delete" for call in fake.calls)


def test_mount_rejects_a_writable_loop_device(tmp_path: Path) -> None:
    service, fake, iso, _mount_point = _service(tmp_path, loop_ro="0")

    with pytest.raises(IsoMountError, match="not read-only"):
        service.mount(iso)
    assert any(call[1] == "loop-delete" for call in fake.calls)


def test_validate_rejects_missing_and_empty_files(tmp_path: Path) -> None:
    service, _fake, _iso, _mount_point = _service(tmp_path)

    with pytest.raises(IsoMountError, match="not found"):
        service.validate_iso(tmp_path / "missing.iso")

    empty = tmp_path / "empty.iso"
    empty.write_bytes(b"")
    with pytest.raises(IsoMountError, match="empty"):
        service.validate_iso(empty)


def test_missing_udisksctl_is_reported_without_fallback(tmp_path: Path) -> None:
    service = IsoMountService(udisksctl="")
    available, reason = service.available()
    assert available is False
    assert "udisksctl" in reason
    with pytest.raises(IsoMountError, match="udisksctl"):
        service.validate_iso(tmp_path / "anything.iso")
