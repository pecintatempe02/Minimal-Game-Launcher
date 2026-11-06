"""Packaging checks for the AppDir staging script."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_ID = "io.github.minimal_game_launcher.MinimalGameLauncher"
APP_VERSION = re.search(
    r'^APP_VERSION = "([^"]+)"$',
    (ROOT / "src" / "minimal_game_launcher" / "__init__.py").read_text(
        encoding="utf-8"
    ),
    re.MULTILINE,
).group(1)


def test_appdir_desktop_entries_are_relocatable_and_complete(
    tmp_path: Path,
) -> None:
    staging = tmp_path / "appdir"
    environment = os.environ.copy()
    environment["MGL_APPDIR"] = str(staging)
    subprocess.run(
        ["sh", str(ROOT / "scripts" / "build-appdir.sh")],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    root_entry = (staging / f"{APP_ID}.desktop").read_text(encoding="utf-8")
    assert "Exec=AppRun" in root_entry

    installed = staging / "usr" / "share" / "applications"
    entries = sorted(installed.glob("*.desktop"))
    assert [entry.name for entry in entries] == [f"{APP_ID}.desktop"]
    text = entries[0].read_text(encoding="utf-8")
    assert "Exec=minimal-game-launcher" in text
    assert "X-MGL-Managed=true" in text
    assert f"X-AppImage-Version={APP_VERSION}" in text
    assert "Version=1.5" in text
    assert str(ROOT) not in text
    assert str(staging) not in text
    assert "X-MGL-Managed=true" in root_entry

    package = staging / "usr" / "lib" / "minimal-game-launcher" / "minimal_game_launcher"
    for relative in (
        "services/installer_manager.py",
        "services/iso_mount.py",
        "services/cleanup.py",
        "services/ownership.py",
        "services/diagnostics.py",
        "ui/installer_dialog.py",
        "ui/cleanup_dialog.py",
        "ui/diagnostics_dialog.py",
        "ui/style.css",
        f"ui/{APP_ID}.svg",
    ):
        assert (package / relative).is_file(), relative
    assert (staging / "AppRun").is_file()
    assert (staging / ".DirIcon").is_symlink()

    names = {path.name for path in staging.rglob("*")}
    assert not names & {"library.db", "settings.json", "themes.ini"}
