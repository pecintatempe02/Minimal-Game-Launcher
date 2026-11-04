"""Tests for theme configuration parsing, validation, and persistence.

These tests never touch a display or a real user configuration: every file is
created under ``tmp_path``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from minimal_game_launcher.themes import (
    BUILTIN_THEME_ID,
    DEFAULT_COLORS,
    SEMANTIC_KEYS,
    Appearance,
    ThemePalette,
    default_config,
    load_themes,
    parse_color,
    sanitize_theme_name,
    slugify,
    theme_identifier,
    update_theme_setting,
)

TWO_THEMES = """\
# User-managed theme file. Keep this comment.
[theme]
active = rose
appearance = dark

[rose]
name = Obsidian Rose
accent = #922a5a
accent-hover = #b13a6f
background = #121014
surface = #1c1920
surface-elevated = #26222b
text = #f3eef2
text-muted = #a9a1ac
border = #3a333e
success = #4caf6d
warning = #d9a441
danger = #d9534f

[evernight]
name = Evernight
accent = #b22222
"""


def _write(tmp_path: Path, text: str, name: str = "themes.ini") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Parsing


def test_valid_file_parses_every_theme(tmp_path: Path) -> None:
    config = load_themes(_write(tmp_path, TWO_THEMES))

    assert config.error is None
    assert set(config.themes) == {"default", "rose", "evernight"}
    assert config.active == "rose"
    assert config.appearance is Appearance.DARK
    assert config.theme.name == "Obsidian Rose"
    assert config.theme.color("accent") == "#922a5a"
    assert config.theme.color("accent_hover") == "#b13a6f"
    assert config.theme.color("surface_elevated") == "#26222b"
    assert config.theme.color("text_muted") == "#a9a1ac"


def test_underscore_and_hyphen_keys_are_equivalent(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "[theme]\nactive = mixed\n\n[mixed]\naccent_hover = #111111\n"
        "surface-elevated = #222222\n",
    )
    theme = load_themes(path).theme

    assert theme.color("accent_hover") == "#111111"
    assert theme.color("surface_elevated") == "#222222"


@pytest.mark.parametrize(
    "raw",
    ["system", "dark", "light", "DARK", "  light  "],
)
def test_appearance_modes_parse(tmp_path: Path, raw: str) -> None:
    path = _write(tmp_path, f"[theme]\nappearance = {raw}\n")
    assert load_themes(path).appearance is Appearance(raw.strip().lower())


def test_unknown_appearance_falls_back_to_system(tmp_path: Path) -> None:
    path = _write(tmp_path, "[theme]\nappearance = sepia\n")
    config = load_themes(path)

    assert config.appearance is Appearance.SYSTEM
    assert any("unknown appearance" in warning for warning in config.warnings)


def test_missing_file_uses_defaults(tmp_path: Path) -> None:
    config = load_themes(tmp_path / "absent.ini")

    assert config.active == BUILTIN_THEME_ID
    assert config.appearance is Appearance.SYSTEM
    assert config.error is None
    assert config.theme.color("accent") == DEFAULT_COLORS["accent"]


def test_none_path_uses_defaults() -> None:
    assert load_themes(None).themes == default_config().themes


def test_missing_keys_use_defaults(tmp_path: Path) -> None:
    path = _write(tmp_path, "[theme]\nactive = sparse\n\n[sparse]\nname = Sparse\n")
    theme = load_themes(path).theme

    assert theme.name == "Sparse"
    assert theme.color("accent") == DEFAULT_COLORS["accent"]
    assert theme.color("success") == DEFAULT_COLORS["success"]
    # Base surfaces are inherited from libadwaita, not invented.
    assert theme.color("background") is None
    assert "background" not in theme.resolved


def test_invalid_color_is_rejected_and_default_kept(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "[theme]\nactive = broken\n\n[broken]\naccent = banana\n"
        "danger = #12345\nwarning = #fff\n",
    )
    config = load_themes(path)
    theme = config.theme

    assert theme.color("accent") == DEFAULT_COLORS["accent"]
    assert theme.color("danger") == DEFAULT_COLORS["danger"]
    assert theme.color("warning") == "#ffffff"  # #fff is expanded
    assert sum("invalid color" in warning for warning in config.warnings) == 2


def test_unknown_active_theme_falls_back_safely(tmp_path: Path) -> None:
    path = _write(tmp_path, "[theme]\nactive = missing\n\n[real]\naccent = #010203\n")
    config = load_themes(path)

    assert config.active == BUILTIN_THEME_ID
    assert config.theme.color("accent") == DEFAULT_COLORS["accent"]
    assert any("unknown active theme" in warning for warning in config.warnings)


def test_malformed_ini_never_raises(tmp_path: Path) -> None:
    path = _write(tmp_path, "this is not an ini file\n[unclosed\n")
    config = load_themes(path)

    assert config.error is not None
    assert config.active == BUILTIN_THEME_ID
    assert config.theme.color("accent") == DEFAULT_COLORS["accent"]


def test_unreadable_file_never_raises(tmp_path: Path) -> None:
    path = tmp_path / "themes.ini"
    path.write_bytes(b"\xff\xfe\x00invalid utf-8")
    config = load_themes(path)

    assert config.error is not None
    assert config.appearance is Appearance.SYSTEM


def test_unknown_keys_are_ignored(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "[theme]\nactive = t\n\n[t]\naccent = #010203\nfancy-new-key = #ffffff\n",
    )
    theme = load_themes(path).theme

    assert theme.colors == {"accent": "#010203"}


def test_section_name_is_the_theme_id(tmp_path: Path) -> None:
    path = _write(tmp_path, "[theme]\nactive = evernight\n\n[evernight]\naccent = #010203\n")
    config = load_themes(path)

    assert config.active == "evernight"
    assert config.theme.color("accent") == "#010203"


# ---------------------------------------------------------------------------
# Legacy single-theme file


def test_legacy_theme_file_is_imported_read_only(tmp_path: Path) -> None:
    legacy = _write(
        tmp_path,
        "[theme]\nname = Default\nmode = system\n\n[colors]\n"
        "accent = #922a5a\naccent_hover = #b13a6f\nbackground = #121014\n"
        "surface = #1c1920\nsurface_elevated = #26222b\ntext = #f3eef2\n"
        "text_muted = #a9a1ac\nborder = #3a333e\nsuccess = #4caf6d\n"
        "warning = #d9a441\ndanger = #d9534f\n",
        name="theme.ini",
    )
    before = legacy.read_bytes()

    config = load_themes(tmp_path / "themes.ini", legacy)

    assert config.active == BUILTIN_THEME_ID
    assert config.appearance is Appearance.SYSTEM
    assert config.theme.name == "Default"
    assert config.theme.color("background") == "#121014"
    assert config.theme.color("accent_hover") == "#b13a6f"
    assert legacy.read_bytes() == before, "legacy file must never be rewritten"


def test_themes_ini_wins_over_legacy(tmp_path: Path) -> None:
    legacy = _write(
        tmp_path, "[theme]\nname = Default\n\n[colors]\naccent = #111111\n",
        name="theme.ini",
    )
    path = _write(tmp_path, "[theme]\nactive = default\n\n[default]\naccent = #222222\n")

    config = load_themes(path, legacy)

    assert config.theme.color("accent") == "#222222"


def test_legacy_values_survive_when_only_active_is_written(tmp_path: Path) -> None:
    legacy = _write(
        tmp_path, "[theme]\nname = Default\n\n[colors]\naccent = #111111\n",
        name="theme.ini",
    )
    path = tmp_path / "themes.ini"
    update_theme_setting(path, "active", BUILTIN_THEME_ID)

    config = load_themes(path, legacy)

    assert config.active == BUILTIN_THEME_ID
    assert config.theme.color("accent") == "#111111"


# ---------------------------------------------------------------------------
# Color helpers


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("#abc", "#aabbcc"),
        ("#AABBCC", "#aabbcc"),
        (" #123456 ", "#123456"),
        ("#aabbccdd", None),
        ("#12345", None),
        ("red", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_color(raw: str | None, expected: str | None) -> None:
    assert parse_color(raw) == expected


def test_slugify() -> None:
    assert slugify("Obsidian Rose") == "obsidian-rose"
    assert slugify("  Evernight  ") == "evernight"
    assert slugify("***") == "theme"


def test_theme_identifier_is_deterministic_and_collision_resistant() -> None:
    names = ["colors", "theme", "theme-colors", "theme-theme", "色"]
    identifiers = [theme_identifier(name) for name in names]

    assert len(set(identifiers)) == len(names)
    assert all(theme_identifier(name) == identifier for name, identifier in zip(names, identifiers))
    assert all(identifier not in {"theme", "colors"} for identifier in identifiers)
    # A safe ordinary name keeps its stable slug.
    assert theme_identifier("columbina") == "columbina"
    # Lossy slugs are disambiguated rather than silently merged.
    assert theme_identifier("Obsidian Rose") != theme_identifier("obsidian-rose")


def test_sanitize_theme_name_removes_line_breaks_and_controls() -> None:
    assert sanitize_theme_name("X\n[theme]\nactive = hacked") == (
        "X [theme] active = hacked"
    )
    assert "\n" not in sanitize_theme_name("a\r\nb\tc\x00d")


def test_palette_resolved_contains_only_usable_values() -> None:
    palette = ThemePalette("x", "X", {"accent": "#010203"})
    resolved = palette.resolved

    assert resolved["accent"] == "#010203"
    assert set(resolved) <= set(SEMANTIC_KEYS)
    assert "background" not in resolved


# ---------------------------------------------------------------------------
# Writing


def test_update_preserves_comments_and_other_values(tmp_path: Path) -> None:
    path = _write(tmp_path, TWO_THEMES)

    update_theme_setting(path, "active", "evernight")

    text = path.read_text(encoding="utf-8")
    assert "# User-managed theme file. Keep this comment." in text
    assert "appearance = dark" in text
    assert "active = evernight" in text
    assert "[rose]" in text and "accent-hover = #b13a6f" in text


def test_update_replaces_existing_key_in_place(tmp_path: Path) -> None:
    path = _write(tmp_path, "[theme]\n# leading comment\nactive = rose\n")

    update_theme_setting(path, "appearance", "light")

    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines == ["[theme]", "# leading comment", "active = rose", "appearance = light"]


def test_update_creates_file_without_other_content(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "themes.ini"

    update_theme_setting(path, "active", "default")

    text = path.read_text(encoding="utf-8")
    assert text.startswith("# Minimal Game Launcher theme configuration.")
    assert text.endswith("[theme]\nactive = default\n")
    # The created file parses and yields the requested value.
    assert load_themes(path).active == "default"


def test_update_creates_theme_section_when_absent(tmp_path: Path) -> None:
    path = _write(tmp_path, "[rose]\naccent = #010203\n")

    update_theme_setting(path, "appearance", "dark")

    text = path.read_text(encoding="utf-8")
    assert "[rose]" in text
    assert "[theme]\nappearance = dark" in text


def test_update_is_atomic_and_leaves_no_temporary_file(tmp_path: Path) -> None:
    path = _write(tmp_path, TWO_THEMES)

    update_theme_setting(path, "active", "evernight")

    assert [entry.name for entry in tmp_path.iterdir()] == ["themes.ini"]


@pytest.mark.parametrize(
    "first",
    ["active = default", "active: default"],
)
@pytest.mark.parametrize(
    "second",
    ["active = default", "active: default"],
)
def test_update_removes_duplicate_active_keys(
    tmp_path: Path, first: str, second: str
) -> None:
    """L1: a later duplicate would shadow the freshly written selection."""
    path = _write(
        tmp_path,
        f"[theme]\n{first}\n{second}\nappearance = dark\n\n"
        "[rose]\naccent = #922a5a\n",
    )

    update_theme_setting(path, "active", "rose")

    text = path.read_text(encoding="utf-8")
    active_lines = [
        line
        for line in text.splitlines()
        if line.split("=")[0].split(":")[0].strip() == "active"
    ]
    assert active_lines == ["active = rose"], text
    config = load_themes(path)
    assert config.active == "rose"
    assert config.appearance is Appearance.DARK


def test_update_unwritable_path_raises_oserror(tmp_path: Path) -> None:
    blocked = tmp_path / "file"
    blocked.write_text("not a directory", encoding="utf-8")

    with pytest.raises(OSError):
        update_theme_setting(blocked / "themes.ini", "active", "x")


# ---------------------------------------------------------------------------
# Legendary review corrections


MIXED_DOCUMENT = """\
[theme]
active = rose

[colors]
accent = #010203
background = #040506

[rose]
name = Rose
accent = #922a5a

[evernight]
name = Evernight
accent = #b22222
"""


def test_colors_section_does_not_hide_named_themes(tmp_path: Path) -> None:
    """F2: a stray [colors] must not turn a multi-theme file into legacy."""
    config = load_themes(_write(tmp_path, MIXED_DOCUMENT))

    assert {"rose", "evernight"} <= set(config.themes)
    assert config.error is None
    assert config.invalid_reason is None
    assert config.active == "rose"
    assert config.theme.color("accent") == "#922a5a"


def test_legacy_layout_still_applies_without_named_themes(tmp_path: Path) -> None:
    """F2: [theme] name/mode + [colors] alone is still the legacy layout."""
    path = _write(
        tmp_path,
        "[theme]\nname = Legacy\nappearance = light\n\n[colors]\naccent = #010203\n",
    )
    config = load_themes(path)

    assert config.themes["legacy"].name == "Legacy"
    assert config.theme.color("accent") == "#010203"
    assert config.appearance is Appearance.LIGHT


def test_legacy_file_layer_accepts_colors_alongside_sections(tmp_path: Path) -> None:
    """The legacy file is defined by its layout, so [colors] always applies."""
    legacy = _write(
        tmp_path,
        "[theme]\nname = Old\n\n[colors]\naccent = #010203\n\n[ignored]\naccent = #0f0f0f\n",
        name="theme.ini",
    )
    config = load_themes(None, legacy)

    assert config.theme.name == "Old"
    assert config.theme.color("accent") == "#010203"


def test_precedence_prefers_the_named_section_over_legacy_palette(
    tmp_path: Path,
) -> None:
    """F2: a named theme wins over the [colors] palette of the same file."""
    path = _write(
        tmp_path,
        "[theme]\nname = Rose\nactive = rose\n\n[colors]\naccent = #010203\n\n"
        "[rose]\naccent = #922a5a\n",
    )
    config = load_themes(path)

    assert config.theme.color("accent") == "#922a5a"


# ---------------------------------------------------------------------------
# invalid_reason


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "[theme]\nactive = rose\n\n[rose\naccent = #00ff88\n",
            "parsing error",
        ),
        ("[theme]\nactive = rose\n", "no theme"),
        ("[meta]\nname = x\n", "no theme"),
        ("[theme]\nactive = missing\n\n[rose]\naccent = #00ff88\n", "unknown active"),
    ],
)
def test_invalid_reason_marks_unusable_documents(
    tmp_path: Path, text: str, expected: str
) -> None:
    config = load_themes(_write(tmp_path, text))

    assert config.invalid_reason is not None
    assert expected in config.invalid_reason


def test_valid_document_has_no_invalid_reason(tmp_path: Path) -> None:
    assert load_themes(_write(tmp_path, TWO_THEMES)).invalid_reason is None


def test_invalid_appearance_marks_the_document_invalid(tmp_path: Path) -> None:
    """L3: an unknown appearance must not replace the last valid reload."""
    path = _write(
        tmp_path,
        "[theme]\nactive = rose\nappearance = sepia\n\n[rose]\naccent = #922a5a\n",
    )
    config = load_themes(path)

    assert config.invalid_reason is not None
    assert "appearance" in config.invalid_reason
    assert config.appearance is Appearance.SYSTEM


def test_missing_file_is_not_semantically_invalid(tmp_path: Path) -> None:
    """A missing file is a first run, not a broken configuration."""
    assert load_themes(tmp_path / "absent.ini").invalid_reason is None


# ---------------------------------------------------------------------------
# Writer: symlinks, line endings, BOM, permissions


def test_update_preserves_crlf_line_endings(tmp_path: Path) -> None:
    path = tmp_path / "themes.ini"
    path.write_bytes(b"[theme]\r\nactive = rose\r\n\r\n[rose]\r\naccent = #922a5a\r\n")

    update_theme_setting(path, "appearance", "dark")

    data = path.read_bytes()
    assert b"\r\n" in data
    assert b"\n" not in data.replace(b"\r\n", b"")
    assert b"appearance = dark" in data
    assert load_themes(path).appearance is Appearance.DARK


def test_update_preserves_utf8_bom(tmp_path: Path) -> None:
    path = tmp_path / "themes.ini"
    path.write_bytes(b"\xef\xbb\xbf[theme]\nactive = rose\n\n[rose]\naccent = #922a5a\n")

    update_theme_setting(path, "appearance", "light")

    data = path.read_bytes()
    assert data.startswith(b"\xef\xbb\xbf")
    config = load_themes(path)
    assert config.appearance is Appearance.LIGHT
    assert config.error is None


def test_bom_file_is_readable_without_being_rewritten(tmp_path: Path) -> None:
    path = tmp_path / "themes.ini"
    path.write_bytes(b"\xef\xbb\xbf[rose]\naccent = #123456\n")

    config = load_themes(path)

    assert config.error is None
    # Without [theme] active, the built-in theme stays selected; the BOM file's
    # own theme must still be parsed.
    assert config.themes["rose"].color("accent") == "#123456"


def test_update_preserves_permissions(tmp_path: Path) -> None:
    path = _write(tmp_path, TWO_THEMES)
    path.chmod(0o600)

    update_theme_setting(path, "active", "evernight")

    assert path.stat().st_mode & 0o777 == 0o600
    assert path.read_text(encoding="utf-8").count("active = evernight") == 1


def test_update_follows_a_symlink_and_keeps_it(tmp_path: Path) -> None:
    real_dir = tmp_path / "dotfiles"
    real_dir.mkdir()
    real = real_dir / "themes.ini"
    real.write_text(TWO_THEMES, encoding="utf-8")
    link = tmp_path / "themes.ini"
    link.symlink_to(real)

    update_theme_setting(link, "active", "evernight")

    assert link.is_symlink()
    assert link.resolve() == real
    assert "active = evernight" in real.read_text(encoding="utf-8")
    assert "# User-managed theme file. Keep this comment." in real.read_text(
        encoding="utf-8"
    )


def test_update_refuses_a_symlink_into_a_missing_directory(tmp_path: Path) -> None:
    link = tmp_path / "themes.ini"
    link.symlink_to(tmp_path / "nowhere" / "themes.ini")

    with pytest.raises(OSError):
        update_theme_setting(link, "active", "rose")

    assert not (tmp_path / "nowhere").exists()
    assert link.is_symlink()


def test_update_refuses_an_unparsable_file_and_keeps_it_intact(
    tmp_path: Path,
) -> None:
    path = tmp_path / "themes.ini"
    original = b"[theme]\nactive = rose\n\n[broken\naccent = #922a5a\n"
    path.write_bytes(original)

    with pytest.raises(OSError, match="unparsable"):
        update_theme_setting(path, "active", "evernight")

    assert path.read_bytes() == original


def test_failed_write_leaves_no_temporary_files(tmp_path: Path) -> None:
    path = _write(tmp_path, TWO_THEMES)

    real_replace = os.replace

    def failing_replace(source, destination):
        raise OSError("disk full")

    os.replace = failing_replace
    try:
        with pytest.raises(OSError):
            update_theme_setting(path, "active", "evernight")
    finally:
        os.replace = real_replace

    assert [entry.name for entry in tmp_path.iterdir()] == ["themes.ini"]
    assert "active = rose" in path.read_text(encoding="utf-8")


def test_creation_race_keeps_the_winners_content(
    tmp_path: Path, monkeypatch
) -> None:
    """F6: if the file appears between the check and the create, edit it."""
    path = tmp_path / "themes.ini"
    import minimal_game_launcher.themes as themes_module

    real_create = themes_module._create_theme_file

    def racing_create(target, key, value):
        target.write_text(
            "[theme]\nactive = rose\n\n[rose]\naccent = #922a5a\n",
            encoding="utf-8",
        )
        return real_create(target, key, value)

    monkeypatch.setattr(themes_module, "_create_theme_file", racing_create)

    update_theme_setting(path, "appearance", "dark")

    text = path.read_text(encoding="utf-8")
    assert "[rose]" in text and "accent = #922a5a" in text
    assert "appearance = dark" in text
    config = load_themes(path)
    assert config.active == "rose"
    assert config.appearance is Appearance.DARK


def test_creation_race_with_empty_file_reports_an_error(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "themes.ini"
    import minimal_game_launcher.themes as themes_module

    def empty_create(target, key, value):
        target.write_text("", encoding="utf-8")
        raise FileExistsError(str(target))

    monkeypatch.setattr(themes_module, "_create_theme_file", empty_create)

    with pytest.raises(OSError):
        update_theme_setting(path, "active", "rose")


def test_bad_colors_in_one_theme_do_not_discard_the_others(
    tmp_path: Path,
) -> None:
    """F3: an unrelated bad value must not drop valid user themes."""
    path = _write(
        tmp_path,
        "[theme]\nactive = rose\n\n[rose]\naccent = #922a5a\n\n"
        "[broken]\naccent = not-a-color\nbackground = #121014\n",
    )
    config = load_themes(path)

    assert config.invalid_reason is None
    assert config.active == "rose"
    assert config.theme.color("accent") == "#922a5a"
    assert "broken" in config.themes
    assert config.themes["broken"].color("background") == "#121014"
    assert any("invalid color" in warning for warning in config.warnings)
