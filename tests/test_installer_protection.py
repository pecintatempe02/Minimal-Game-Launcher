"""Failed installer sessions stay resumable, so their data must stay protected."""

from __future__ import annotations

from minimal_game_launcher.models import InstallerStatus
from minimal_game_launcher.services import installer_manager
from minimal_game_launcher.services.cleanup import PROTECTING_SESSION_STATUSES


def test_failed_session_protects_its_data() -> None:
    assert InstallerStatus.FAILED in PROTECTING_SESSION_STATUSES


def test_installed_session_does_not_protect_data() -> None:
    assert InstallerStatus.INSTALLED not in PROTECTING_SESSION_STATUSES


def test_installer_manager_uses_the_same_status_set() -> None:
    assert installer_manager._PROTECTING_SESSION_STATUSES is PROTECTING_SESSION_STATUSES
