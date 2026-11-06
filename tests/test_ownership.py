"""Tests for the ownership foundation: classification, sizes, and deletion.

Everything runs in temporary directories. Deletion tests only ever target
fixtures created by the test itself; no real launcher data, prefix, or game file
is touched.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from minimal_game_launcher.errors import OwnershipError
from minimal_game_launcher.services.ownership import (
    OwnershipService,
    OwnershipStatus,
    ResourceKind,
)

GAME_ID = "game-1"


@pytest.fixture
def roots(tmp_path: Path) -> dict[str, Path]:
    """Return the three managed roots without creating them."""
    data = tmp_path / "data"
    return {
        "prefixes": data / "prefixes",
        "games": data / "games",
        "installers": data / "installers",
    }


@pytest.fixture
def service(roots: dict[str, Path]) -> OwnershipService:
    """Build a service over the temporary roots."""
    return OwnershipService(
        roots["prefixes"], roots["games"], roots["installers"]
    )


_ROOT_KEYS = {
    ResourceKind.INSTALLATION: "games",
    ResourceKind.PREFIX: "prefixes",
    ResourceKind.INSTALLER_SOURCE: "installers",
}


def make_resource(roots: dict[str, Path], kind: str, game_id: str = GAME_ID) -> Path:
    """Create a managed directory with a file and a nested directory."""
    resource = roots[kind] / game_id
    (resource / "nested").mkdir(parents=True)
    (resource / "file.txt").write_text("hello", encoding="utf-8")
    (resource / "nested" / "inner.bin").write_bytes(b"1234")
    return resource


@pytest.mark.parametrize(
    "kind", [ResourceKind.INSTALLATION, ResourceKind.PREFIX, ResourceKind.INSTALLER_SOURCE]
)
def test_each_kind_owns_only_its_game_directory(
    service: OwnershipService, roots: dict[str, Path], kind: ResourceKind
) -> None:
    resource = make_resource(roots, _ROOT_KEYS[kind])

    verdict = service.classify(kind, GAME_ID, resource)

    assert verdict.status is OwnershipStatus.MANAGED
    assert verdict.is_managed
    assert verdict.expected_path == resource.resolve()


def test_external_directory_is_not_managed(
    service: OwnershipService, tmp_path: Path
) -> None:
    external = tmp_path / "external-game"
    external.mkdir()
    (external / "game.exe").write_bytes(b"MZ")

    verdict = service.classify(ResourceKind.INSTALLATION, GAME_ID, external)

    assert verdict.status is OwnershipStatus.EXTERNAL
    assert not verdict.is_managed
    assert "outside the managed root" in verdict.reason


def test_missing_managed_directory_is_reported_missing(
    service: OwnershipService, roots: dict[str, Path]
) -> None:
    verdict = service.classify(
        ResourceKind.PREFIX, GAME_ID, roots["prefixes"] / GAME_ID
    )

    assert verdict.status is OwnershipStatus.MISSING
    assert not verdict.is_managed


def test_managed_roots_are_never_created_implicitly(
    service: OwnershipService, roots: dict[str, Path]
) -> None:
    service.classify(ResourceKind.INSTALLATION, GAME_ID, roots["games"] / GAME_ID)
    service.estimate(ResourceKind.INSTALLATION, GAME_ID, roots["games"] / GAME_ID)

    assert not any(path.exists() for path in roots.values())


@pytest.mark.parametrize(
    "game_id", ["../escape", "a/b", "", ".", "..", "id with space", "x" * 200]
)
def test_unsafe_game_ids_are_rejected(
    service: OwnershipService, roots: dict[str, Path], game_id: str
) -> None:
    verdict = service.classify(
        ResourceKind.INSTALLATION, game_id, roots["games"] / "whatever"
    )

    assert verdict.status is OwnershipStatus.INVALID
    assert "game ID" in verdict.reason
    with pytest.raises(OwnershipError):
        service.expected_path(ResourceKind.INSTALLATION, game_id)


def test_parent_directory_segments_are_rejected_not_normalised(
    service: OwnershipService, roots: dict[str, Path]
) -> None:
    other = make_resource(roots, "games", "other-game")
    traversal = roots["games"] / GAME_ID / ".." / "other-game"

    verdict = service.classify(ResourceKind.INSTALLATION, GAME_ID, traversal)

    assert verdict.status is OwnershipStatus.INVALID
    assert "parent-directory" in verdict.reason
    assert other.exists()


def test_wrong_game_id_inside_managed_root_is_invalid(
    service: OwnershipService, roots: dict[str, Path]
) -> None:
    other = make_resource(roots, "games", "other-game")

    verdict = service.classify(ResourceKind.INSTALLATION, GAME_ID, other)

    assert verdict.status is OwnershipStatus.INVALID
    assert "does not match this game ID" in verdict.reason


def test_managed_root_itself_is_invalid(
    service: OwnershipService, roots: dict[str, Path]
) -> None:
    roots["games"].mkdir(parents=True)

    verdict = service.classify(ResourceKind.INSTALLATION, GAME_ID, roots["games"])

    assert verdict.status is OwnershipStatus.INVALID
    assert "root itself" in verdict.reason


def test_file_instead_of_directory_is_invalid(
    service: OwnershipService, roots: dict[str, Path]
) -> None:
    roots["games"].mkdir(parents=True)
    path = roots["games"] / GAME_ID
    path.write_text("not a directory", encoding="utf-8")

    verdict = service.classify(ResourceKind.INSTALLATION, GAME_ID, path)

    assert verdict.status is OwnershipStatus.INVALID
    assert "not a directory" in verdict.reason


def test_symlink_to_inside_managed_root_is_not_managed(
    service: OwnershipService, roots: dict[str, Path]
) -> None:
    target = make_resource(roots, "games")
    link = roots["games"] / GAME_ID
    target.rename(roots["games"] / "real-target")
    link.symlink_to(roots["games"] / "real-target", target_is_directory=True)

    verdict = service.classify(ResourceKind.INSTALLATION, GAME_ID, link)

    assert verdict.status is OwnershipStatus.INVALID
    assert "symbolic link" in verdict.reason


def test_symlink_to_outside_the_managed_root_is_not_managed(
    service: OwnershipService, roots: dict[str, Path], tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    roots["games"].mkdir(parents=True)
    link = roots["games"] / GAME_ID
    link.symlink_to(outside, target_is_directory=True)

    verdict = service.classify(ResourceKind.INSTALLATION, GAME_ID, link)

    assert verdict.status is OwnershipStatus.INVALID
    assert (outside / "keep.txt").exists()


def test_unreadable_path_is_invalid(
    service: OwnershipService, roots: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    path = make_resource(roots, "games")
    real_lstat = os.lstat

    def broken_lstat(target, *args, **kwargs):
        if Path(target) == path:
            raise PermissionError(13, "Permission denied", str(target))
        return real_lstat(target, *args, **kwargs)

    monkeypatch.setattr(
        "minimal_game_launcher.services.ownership.os.lstat", broken_lstat
    )

    verdict = service.classify(ResourceKind.INSTALLATION, GAME_ID, path)

    assert verdict.status is OwnershipStatus.INVALID
    assert "could not be inspected" in verdict.reason


def test_size_estimation_counts_files_and_nested_directories(
    service: OwnershipService, roots: dict[str, Path]
) -> None:
    resource = make_resource(roots, "games")

    estimate = service.estimate(ResourceKind.INSTALLATION, GAME_ID, resource)

    assert estimate.complete
    assert estimate.errors == ()
    assert estimate.file_count == 2
    assert estimate.total_bytes == len("hello") + len(b"1234")
    assert estimate.symlink_count == 0


def test_size_estimation_counts_hardlinks_once(
    service: OwnershipService, roots: dict[str, Path]
) -> None:
    resource = make_resource(roots, "games")
    (resource / "hardlink.txt").hardlink_to(resource / "file.txt")

    estimate = service.estimate(ResourceKind.INSTALLATION, GAME_ID, resource)

    assert estimate.complete
    assert estimate.file_count == 2
    assert estimate.total_bytes == len("hello") + len(b"1234")


def test_size_estimation_never_follows_symlinks(
    service: OwnershipService, roots: dict[str, Path], tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "large.bin").write_bytes(b"x" * 5000)
    resource = make_resource(roots, "games")
    (resource / "outside-link").symlink_to(outside, target_is_directory=True)

    estimate = service.estimate(ResourceKind.INSTALLATION, GAME_ID, resource)

    assert estimate.complete
    assert estimate.symlink_count == 1
    assert estimate.total_bytes < 5000


def test_size_estimation_reports_partial_results_on_permission_error(
    service: OwnershipService, roots: dict[str, Path]
) -> None:
    resource = make_resource(roots, "games")
    blocked = resource / "nested"
    blocked.chmod(0o000)
    try:
        estimate = service.estimate(
            ResourceKind.INSTALLATION, GAME_ID, resource
        )
    finally:
        blocked.chmod(0o700)

    assert not estimate.complete
    assert estimate.errors
    assert estimate.file_count == 1
    assert estimate.total_bytes == len("hello")


def test_size_estimation_refuses_external_paths(
    service: OwnershipService, tmp_path: Path
) -> None:
    external = tmp_path / "external"
    external.mkdir()
    (external / "file").write_bytes(b"x")

    estimate = service.estimate(ResourceKind.INSTALLATION, GAME_ID, external)

    assert not estimate.complete
    assert "Not a managed resource" in estimate.errors[0]


def test_authorized_deletion_removes_only_the_game_directory(
    service: OwnershipService, roots: dict[str, Path]
) -> None:
    resource = make_resource(roots, "games")
    sibling = make_resource(roots, "games", "keep-me")
    outside = resource.parent.parent / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    (resource / "outside-link").symlink_to(outside, target_is_directory=True)

    result = service.delete(
        ResourceKind.INSTALLATION, GAME_ID, resource, authorized=True
    )

    assert result.deleted
    assert result.errors == ()
    assert not resource.exists()
    assert (sibling / "file.txt").exists()
    assert (outside / "keep.txt").exists()
    assert roots["games"].is_dir()


def test_deletion_requires_explicit_authorization(
    service: OwnershipService, roots: dict[str, Path]
) -> None:
    resource = make_resource(roots, "games")

    with pytest.raises(OwnershipError, match="explicit authorization"):
        service.delete(ResourceKind.INSTALLATION, GAME_ID, resource, authorized=False)

    assert (resource / "file.txt").exists()


def test_deletion_is_refused_while_the_game_runs(
    service: OwnershipService, roots: dict[str, Path]
) -> None:
    resource = make_resource(roots, "games")

    with pytest.raises(OwnershipError, match="running"):
        service.delete(
            ResourceKind.INSTALLATION,
            GAME_ID,
            resource,
            authorized=True,
            game_running=True,
        )

    assert (resource / "file.txt").exists()


def test_deletion_refuses_external_resources(
    service: OwnershipService, tmp_path: Path
) -> None:
    external = tmp_path / "external-game"
    external.mkdir()
    (external / "file").write_bytes(b"data")

    with pytest.raises(OwnershipError, match="Refusing to delete"):
        service.delete(
            ResourceKind.INSTALLATION, GAME_ID, external, authorized=True
        )

    assert (external / "file").exists()


def test_deletion_refuses_the_managed_root_itself(
    service: OwnershipService, roots: dict[str, Path]
) -> None:
    make_resource(roots, "games")

    with pytest.raises(OwnershipError, match="root itself"):
        service.delete(
            ResourceKind.INSTALLATION, GAME_ID, roots["games"], authorized=True
        )

    assert (roots["games"] / GAME_ID / "file.txt").exists()


def test_deletion_refuses_a_mismatched_game_id(
    service: OwnershipService, roots: dict[str, Path]
) -> None:
    other = make_resource(roots, "games", "other-game")

    with pytest.raises(OwnershipError, match="does not match this game ID"):
        service.delete(ResourceKind.INSTALLATION, GAME_ID, other, authorized=True)

    assert (other / "file.txt").exists()


def test_symlink_replacement_before_deletion_is_refused(
    service: OwnershipService, roots: dict[str, Path], tmp_path: Path
) -> None:
    resource = make_resource(roots, "games")
    # Classify while the real directory still exists.
    assert service.classify(
        ResourceKind.INSTALLATION, GAME_ID, resource
    ).is_managed
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")

    import shutil as shutil_module

    shutil_module.rmtree(resource)
    resource.symlink_to(outside, target_is_directory=True)

    with pytest.raises(OwnershipError, match="symbolic link"):
        service.delete(
            ResourceKind.INSTALLATION, GAME_ID, resource, authorized=True
        )
    assert (outside / "keep.txt").exists()


def test_symlink_swapped_in_after_validation_is_refused(
    service: OwnershipService, roots: dict[str, Path], tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource = make_resource(roots, "games")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    verdict = service.classify(ResourceKind.INSTALLATION, GAME_ID, resource)
    assert verdict.is_managed

    # Model a swap between validation and the open in the deletion path: the
    # classification is forced to look managed while the path is a symlink.
    monkeypatch.setattr(
        service, "classify", lambda *_args, **_kwargs: verdict
    )
    import shutil as shutil_module

    shutil_module.rmtree(resource)
    resource.symlink_to(outside, target_is_directory=True)

    with pytest.raises(OwnershipError, match="opened safely"):
        service.delete(
            ResourceKind.INSTALLATION, GAME_ID, resource, authorized=True
        )
    assert (outside / "keep.txt").exists()


def test_deletion_reports_partial_failures_and_keeps_the_directory(
    service: OwnershipService, roots: dict[str, Path]
) -> None:
    resource = make_resource(roots, "games")
    blocked = resource / "nested"
    blocked.chmod(0o500)
    try:
        result = service.delete(
            ResourceKind.INSTALLATION, GAME_ID, resource, authorized=True
        )
    finally:
        blocked.chmod(0o700)

    assert not result.deleted
    assert result.errors
    assert resource.exists()
    assert blocked.exists()
    assert not (resource / "file.txt").exists()
