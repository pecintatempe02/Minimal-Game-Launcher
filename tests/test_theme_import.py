"""Tests for ``scripts/mgl-theme-import``.

The importer is executed as a real subprocess against a throwaway XDG config
home, so the file it writes is exactly the file the launcher's parser reads.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from minimal_game_launcher.themes import load_themes

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "mgl-theme-import"

COLOR_INI = """\
[meta]
name = Columbina

[colors]
bg = #FF121014
surface = #FF1C1920
text = #FFF3EEF2
accent = #FF922A5A
"""


def _run(home: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["XDG_CONFIG_HOME"] = str(home)
    environment.pop("PYTHONPATH", None)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *arguments],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """Return a throwaway XDG config home."""
    directory = tmp_path / "xdg"
    directory.mkdir()
    return directory


@pytest.fixture
def desktop(home: Path) -> Path:
    """Create a desktop theme directory and its ``current`` symlink."""
    theme_dir = home / "themes" / "columbina"
    theme_dir.mkdir(parents=True)
    (theme_dir / "color.ini").write_text(COLOR_INI, encoding="utf-8")
    (home / "themes" / "current").symlink_to(theme_dir)
    return theme_dir


def test_import_writes_a_section_the_launcher_can_read(
    home: Path, desktop: Path
) -> None:
    output = home / "Minimalist-Game-Launcher" / "themes.ini"

    result = _run(home, "--active")

    assert result.returncode == 0, result.stderr
    config = load_themes(output)
    assert config.error is None
    assert config.active == "columbina"
    assert config.theme.color("accent") == "#922a5a"
    assert config.theme.color("background") == "#121014"
    assert config.theme.name == "Columbina"
    # The source palette is opened read-only and left byte-identical.
    assert (desktop / "color.ini").read_text(encoding="utf-8") == COLOR_INI


def test_reserved_theme_directory_name_is_normalized(
    tmp_path: Path, home: Path
) -> None:
    theme_dir = home / "themes" / "colors"
    theme_dir.mkdir(parents=True)
    (theme_dir / "color.ini").write_text(COLOR_INI, encoding="utf-8")

    result = _run(home, "--theme", "colors", "--active")

    assert result.returncode == 0, result.stderr
    config = load_themes(home / "Minimalist-Game-Launcher" / "themes.ini")
    imported = [theme_id for theme_id in config.themes if theme_id != "default"]
    assert len(imported) == 1, config.themes
    assert imported[0] not in {"theme", "colors"}
    assert config.active == imported[0]
    assert config.theme.color("accent") == "#922a5a"


def test_distinct_theme_directory_names_never_overwrite_each_other(
    home: Path,
) -> None:
    """L4: reserved and lossy names get stable, distinct section ids."""
    names = ["colors", "theme-colors", "theme", "theme-theme", "色"]
    output = home / "Minimalist-Game-Launcher" / "themes.ini"
    for name in names:
        theme_dir = home / "themes" / name
        theme_dir.mkdir(parents=True)
        (theme_dir / "color.ini").write_text(COLOR_INI, encoding="utf-8")
        result = _run(home, "--theme", name, "--active")
        assert result.returncode == 0, result.stderr

    config = load_themes(output)
    imported = [theme_id for theme_id in config.themes if theme_id != "default"]
    assert len(imported) == len(names), config.themes
    # Re-importing the same source name is stable (same id, same bytes).
    before_ids = set(config.themes)
    before = output.read_bytes()
    assert _run(home, "--theme", "色", "--active").returncode == 0
    assert set(load_themes(output).themes) == before_ids
    assert output.read_bytes() == before


def test_active_uses_the_colon_form_without_duplicating_it(
    tmp_path: Path, home: Path
) -> None:
    """F8: ``active: value`` is updated, not shadowed by a new ``active =``."""
    theme_dir = home / "themes" / "rose"
    theme_dir.mkdir(parents=True)
    (theme_dir / "color.ini").write_text(COLOR_INI, encoding="utf-8")
    output = home / "Minimalist-Game-Launcher" / "themes.ini"
    output.parent.mkdir(parents=True)
    output.write_text(
        "# keep me\n[theme]\nactive: default\nappearance = dark\n\n"
        "[default]\nname = Default\naccent = #000000\n",
        encoding="utf-8",
    )

    result = _run(home, "--theme", "rose", "--active")

    assert result.returncode == 0, result.stderr
    text = output.read_text(encoding="utf-8")
    assert "# keep me" in text
    assert "appearance = dark" in text
    active_keys = [
        line for line in text.splitlines() if line.split("=")[0].split(":")[0].strip() == "active"
    ]
    assert len(active_keys) == 1, text
    assert "active: default" not in text
    config = load_themes(output)
    assert config.active == "rose"
    assert config.appearance.value == "dark"
    assert "default" in config.themes


def test_import_without_active_keeps_the_existing_selection(
    home: Path, desktop: Path
) -> None:
    output = home / "Minimalist-Game-Launcher" / "themes.ini"
    output.parent.mkdir(parents=True)
    output.write_text(
        "[theme]\nactive = keep\n\n[keep]\naccent = #010203\n", encoding="utf-8"
    )

    result = _run(home)

    assert result.returncode == 0, result.stderr
    config = load_themes(output)
    assert config.active == "keep"
    assert "columbina" in config.themes


def test_import_reports_a_missing_palette(home: Path) -> None:
    theme_dir = home / "themes" / "empty"
    theme_dir.mkdir(parents=True)
    (theme_dir / "color.ini").write_text("[meta]\nname = Empty\n", encoding="utf-8")

    result = _run(home, "--theme", "empty")

    assert result.returncode == 1
    assert "No [colors] section" in result.stderr
    assert not (home / "Minimalist-Game-Launcher" / "themes.ini").exists()


def test_malformed_target_is_refused_and_left_byte_identical(
    home: Path, desktop: Path
) -> None:
    """M2: never append to a configuration the parser cannot read."""
    output = home / "Minimalist-Game-Launcher" / "themes.ini"
    output.parent.mkdir(parents=True)
    original = b"[theme]\nactive = good\n\n[broken\naccent = #111111\n"
    output.write_bytes(original)

    result = _run(home, "--active")

    assert result.returncode != 0
    assert "unparsable" in result.stderr.lower()
    assert output.read_bytes() == original


def test_imported_name_cannot_inject_a_section_or_setting(home: Path) -> None:
    """M3: a multi-line desktop name is flattened to one metadata line."""
    theme_dir = home / "themes" / "evil"
    theme_dir.mkdir(parents=True)
    (theme_dir / "color.ini").write_text(
        "[meta]\nname = X\n  [theme]\n  active = hacked\n\n"
        "[colors]\naccent = #FF922A5A\n",
        encoding="utf-8",
    )
    output = home / "Minimalist-Game-Launcher" / "themes.ini"

    result = _run(home, "--theme", "evil", "--active")

    assert result.returncode == 0, result.stderr
    text = output.read_text(encoding="utf-8")
    headers = [
        line.strip()
        for line in text.splitlines()
        if line.strip().startswith("[") and line.strip().endswith("]")
    ]
    active_lines = [
        line
        for line in text.splitlines()
        if line.split("=")[0].split(":")[0].strip() == "active"
    ]
    assert headers == ["[evil]", "[theme]"], text
    assert active_lines == ["active = evil"], text
    config = load_themes(output)
    assert set(config.themes) == {"default", "evil"}
    assert config.active == "evil"
    assert config.themes["evil"].name == "X [theme] active = hacked"


def test_active_update_removes_duplicate_keys(home: Path, desktop: Path) -> None:
    """L1: both ``=`` and ``:`` duplicates are removed by ``--active``."""
    output = home / "Minimalist-Game-Launcher" / "themes.ini"
    output.parent.mkdir(parents=True)
    output.write_text(
        "[theme]\nactive = default\nactive: default\nappearance = dark\n\n"
        "[default]\nname = Default\naccent = #000000\n",
        encoding="utf-8",
    )

    result = _run(home, "--active")

    assert result.returncode == 0, result.stderr
    text = output.read_text(encoding="utf-8")
    active_lines = [
        line
        for line in text.splitlines()
        if line.split("=")[0].split(":")[0].strip() == "active"
    ]
    assert active_lines == ["active = columbina"], text
    config = load_themes(output)
    assert config.active == "columbina"
    assert config.appearance.value == "dark"
