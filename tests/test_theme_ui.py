"""Tests for the GTK theme controller and generated CSS.

CSS generation and configuration handling are real. GTK display registration is
mocked (``Gtk.StyleContext.add_provider_for_display`` is patched) so the suite
runs without a compositor; the generated stylesheet is still parsed by a real
``Gtk.CssProvider``. File monitoring uses real ``Gio.FileMonitor`` objects
driven by a GLib main loop.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

import pytest  # noqa: E402
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

from minimal_game_launcher.settings import SettingsStore  # noqa: E402
from minimal_game_launcher.themes import (  # noqa: E402
    Appearance,
    ThemePalette,
    builtin_theme,
)
from minimal_game_launcher.ui.settings_dialog import SettingsDialog  # noqa: E402
from minimal_game_launcher.ui.theme import (  # noqa: E402
    PROVIDER_PRIORITY,
    ThemeController,
    build_theme_css,
)

VALID = """\
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


class _FakeStyleManager:
    """Record color-scheme requests instead of touching the desktop."""

    def __init__(self) -> None:
        self.schemes: list[Adw.ColorScheme] = []

    def set_color_scheme(self, scheme: Adw.ColorScheme) -> None:
        self.schemes.append(scheme)


@pytest.fixture
def gtk_registry(monkeypatch):
    """Replace display registration with call counters."""
    calls = {"add": 0, "remove": 0, "priorities": []}

    def add(_display, _provider, priority):
        calls["add"] += 1
        calls["priorities"].append(priority)

    def remove(_display, _provider):
        calls["remove"] += 1

    monkeypatch.setattr(
        Gtk.StyleContext, "add_provider_for_display", staticmethod(add)
    )
    monkeypatch.setattr(
        Gtk.StyleContext, "remove_provider_for_display", staticmethod(remove)
    )
    return calls


@pytest.fixture
def theme_file(tmp_path: Path) -> Path:
    path = tmp_path / "themes.ini"
    path.write_text(VALID, encoding="utf-8")
    return path


def _run_until(predicate, *, timeout_ms: int = 3000) -> bool:
    """Pump the main loop until ``predicate()`` is true or the timeout expires."""
    loop = GLib.MainLoop()
    deadline = time.monotonic() + timeout_ms / 1000
    result = {"ok": False}

    def poll() -> bool:
        if predicate():
            result["ok"] = True
            loop.quit()
            return GLib.SOURCE_REMOVE
        if time.monotonic() > deadline:
            loop.quit()
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    GLib.timeout_add(20, poll)
    loop.run()
    return result["ok"]


def _settle(milliseconds: int = 400) -> None:
    """Pump the loop for a fixed period so stray events would be delivered."""
    loop = GLib.MainLoop()
    GLib.timeout_add(milliseconds, lambda: (loop.quit(), GLib.SOURCE_REMOVE)[1])
    loop.run()


@pytest.fixture
def controller(theme_file: Path, gtk_registry):
    instance = ThemeController(
        theme_file,
        debounce_ms=30,
        display=object(),
        style_manager=_FakeStyleManager(),
    )
    instance.start()
    yield instance
    instance.shutdown()


# ---------------------------------------------------------------------------
# CSS generation


def test_css_maps_every_semantic_color() -> None:
    palette = ThemePalette(
        "rose",
        "Obsidian Rose",
        {
            "accent": "#922a5a",
            "accent_hover": "#b13a6f",
            "background": "#121014",
            "surface": "#1c1920",
            "surface_elevated": "#26222b",
            "text": "#f3eef2",
            "text_muted": "#a9a1ac",
            "border": "#3a333e",
            "success": "#4caf6d",
            "warning": "#d9a441",
            "danger": "#d9534f",
        },
    )
    css = build_theme_css(palette)

    assert "@define-color accent_color #922a5a;" in css
    assert "@define-color accent_bg_color #922a5a;" in css
    assert "@define-color mgl_accent_hover #b13a6f;" in css
    assert "@define-color window_bg_color #121014;" in css
    assert "@define-color card_bg_color #1c1920;" in css
    assert "@define-color popover_bg_color #26222b;" in css
    assert "@define-color window_fg_color #f3eef2;" in css
    assert "@define-color borders #3a333e;" in css
    assert "@define-color success_color #4caf6d;" in css
    assert "@define-color warning_color #d9a441;" in css
    assert "@define-color error_color #d9534f;" in css
    assert "@define-color destructive_bg_color #d9534f;" in css


def test_css_picks_readable_accent_foreground() -> None:
    dark = build_theme_css(ThemePalette("d", "D", {"accent": "#121014"}))
    light = build_theme_css(ThemePalette("l", "L", {"accent": "#f3eef2"}))

    assert "@define-color accent_fg_color #ffffff;" in dark
    assert "@define-color accent_fg_color #000000;" in light


def test_css_defines_fallbacks_for_omitted_base_colors() -> None:
    css = build_theme_css(builtin_theme())

    assert "@define-color mgl_surface @card_bg_color;" in css
    assert "@define-color mgl_text_muted alpha(@window_fg_color, 0.7);" in css
    # No base override is emitted, so libadwaita's light/dark palette stays.
    assert "window_bg_color #" not in css


def test_generated_css_is_accepted_by_gtk() -> None:
    provider = Gtk.CssProvider()
    for palette in (
        builtin_theme(),
        ThemePalette(
            "full",
            "Full",
            {
                "accent": "#922a5a",
                "accent_hover": "#b13a6f",
                "background": "#121014",
                "surface": "#1c1920",
                "surface_elevated": "#26222b",
                "text": "#f3eef2",
                "text_muted": "#a9a1ac",
                "border": "#3a333e",
                "success": "#4caf6d",
                "warning": "#d9a441",
                "danger": "#d9534f",
            },
        ),
    ):
        provider.load_from_string(build_theme_css(palette))


def test_css_comment_cannot_be_injected_through_the_theme_name() -> None:
    """M1: a name must never close the generated comment and add a rule."""
    palette = ThemePalette(
        "evil",
        "*/ * { color: #ff0000; } /*",
        {"accent": "#922a5a"},
    )

    css = build_theme_css(palette)

    assert css.startswith("/* Generated from the active launcher theme. Do not edit. */")
    assert "* { color" not in css
    assert "#ff0000" not in css
    assert css.count("*/") == 1
    provider = Gtk.CssProvider()
    provider.load_from_string(css)
    assert "rgb(255,0,0)" not in provider.to_string()


# ---------------------------------------------------------------------------
# Provider lifecycle and appearance


def test_provider_is_registered_once_above_libadwaita(controller, gtk_registry) -> None:
    assert gtk_registry["add"] == 1
    assert gtk_registry["priorities"] == [PROVIDER_PRIORITY]
    assert PROVIDER_PRIORITY > Gtk.STYLE_PROVIDER_PRIORITY_USER


def test_repeated_theme_changes_reuse_one_provider(controller, gtk_registry) -> None:
    for theme_id in ("evernight", "rose", "default", "rose"):
        assert controller.set_active_theme(theme_id) is True

    assert gtk_registry["add"] == 1
    assert controller.config.theme.color("accent") == "#922a5a"


def test_unknown_theme_is_rejected(controller, gtk_registry) -> None:
    before = controller.config.active

    assert controller.set_active_theme("does-not-exist") is False

    assert controller.config.active == before
    assert gtk_registry["add"] == 1


def test_appearance_modes_map_to_libadwaita_schemes(controller) -> None:
    manager = controller._style_manager
    manager.schemes.clear()  # start() already applied the file's "dark" value

    controller.set_appearance(Appearance.SYSTEM)
    controller.set_appearance("dark")
    controller.set_appearance(Appearance.LIGHT)

    assert manager.schemes == [
        Adw.ColorScheme.DEFAULT,
        Adw.ColorScheme.FORCE_DARK,
        Adw.ColorScheme.FORCE_LIGHT,
    ]


def test_appearance_change_keeps_the_accent_palette(controller) -> None:
    controller.set_active_theme("evernight")
    controller.set_appearance("light")
    controller.set_appearance("dark")

    assert controller.config.theme.color("accent") == "#b22222"


def test_changes_are_persisted(theme_file: Path, controller) -> None:
    controller.set_active_theme("evernight")
    controller.set_appearance("light")

    text = theme_file.read_text(encoding="utf-8")
    assert "active = evernight" in text
    assert "appearance = light" in text
    # The rest of the user's file is untouched.
    assert "[rose]" in text and "accent-hover = #b13a6f" in text


def test_unwritable_config_still_applies_for_the_session(
    tmp_path: Path, gtk_registry
) -> None:
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    instance = ThemeController(
        blocked / "themes.ini",
        debounce_ms=30,
        display=object(),
        style_manager=_FakeStyleManager(),
    )
    try:
        instance.start()
        with pytest.raises(OSError):
            instance.set_active_theme("default")
        assert instance.config.active == "default"
        assert gtk_registry["add"] == 1
    finally:
        instance.shutdown()


def test_shutdown_removes_the_provider(controller, gtk_registry) -> None:
    controller.shutdown()

    assert gtk_registry["remove"] == 1
    assert controller._monitor is None
    assert controller._debounce_source == 0


def test_invalid_generated_css_keeps_the_last_stylesheet_and_selection(
    controller, theme_file: Path, monkeypatch
) -> None:
    """L5: a rejected stylesheet must not persist or report a new selection."""
    good = controller._provider.to_string()
    before_text = theme_file.read_text(encoding="utf-8")
    assert good.strip()

    monkeypatch.setattr(
        "minimal_game_launcher.ui.theme.build_theme_css",
        lambda _palette: "this is { not css\n",
    )
    emissions = []
    controller.connect("changed", lambda *_args: emissions.append(1))

    assert controller.set_active_theme("evernight") is False
    assert controller._provider.to_string() == good
    assert controller.config.active == "rose"
    assert theme_file.read_text(encoding="utf-8") == before_text
    assert emissions == [], "a rejected stylesheet must not report a change"


def test_invalid_generated_css_keeps_the_last_appearance(
    controller, theme_file: Path, monkeypatch
) -> None:
    before_text = theme_file.read_text(encoding="utf-8")
    assert controller.config.appearance is Appearance.DARK

    monkeypatch.setattr(
        "minimal_game_launcher.ui.theme.build_theme_css",
        lambda _palette: "this is { not css\n",
    )
    controller.set_appearance("light")

    assert controller.config.appearance is Appearance.DARK
    assert theme_file.read_text(encoding="utf-8") == before_text


def test_invalid_reload_css_keeps_the_last_applied_configuration(
    controller, theme_file: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "minimal_game_launcher.ui.theme.build_theme_css",
        lambda _palette: "this is { not css\n",
    )
    theme_file.write_text(
        VALID.replace("active = rose", "active = evernight"), encoding="utf-8"
    )
    _settle(300)

    assert controller.config.active == "rose"
    assert controller.config.appearance is Appearance.DARK


# ---------------------------------------------------------------------------
# Settings dialog integration (needs a real GDK display for widget construction)

needs_display = pytest.mark.skipif(
    Gdk.Display.get_default() is None,
    reason="constructing GTK widgets needs a GDK display",
)


@needs_display
def test_settings_dialog_lists_themes_and_applies_choices(
    theme_file: Path, tmp_path: Path, gtk_registry
) -> None:
    instance = ThemeController(
        theme_file,
        debounce_ms=30,
        display=object(),
        style_manager=_FakeStyleManager(),
    )
    instance.start()
    try:
        dialog = SettingsDialog(
            None, SettingsStore(tmp_path / "settings.json"), instance
        )

        assert dialog._theme_ids == ["default", "evernight", "rose"]
        assert dialog._theme_row.get_selected() == 2  # "rose" is active
        assert dialog._appearance_row.get_selected() == 1  # dark

        dialog._theme_row.set_selected(1)  # select "evernight"
        assert instance.config.active == "evernight"
        assert "active = evernight" in theme_file.read_text(encoding="utf-8")

        dialog._appearance_row.set_selected(2)  # light
        assert instance.config.appearance is Appearance.LIGHT
        assert "appearance = light" in theme_file.read_text(encoding="utf-8")
    finally:
        instance.shutdown()


# ---------------------------------------------------------------------------
# Live reload


def test_external_edit_is_reloaded(theme_file: Path, controller) -> None:
    emissions = []
    controller.connect("changed", lambda *_args: emissions.append(1))

    theme_file.write_text(
        VALID.replace("#922a5a", "#00ff88"), encoding="utf-8"
    )

    assert _run_until(
        lambda: controller.config.theme.color("accent") == "#00ff88"
    ), "file monitor did not deliver the edit"
    assert emissions


def test_atomic_replacement_is_reloaded(theme_file: Path, controller) -> None:
    replacement = theme_file.with_name("themes.ini.new")
    replacement.write_text(VALID.replace("#b22222", "#00ff88"), encoding="utf-8")

    os.replace(replacement, theme_file)

    assert _run_until(
        lambda: controller.config.themes["evernight"].color("accent") == "#00ff88"
    ), "atomic replacement was not picked up"


def test_recreated_file_is_reloaded(theme_file: Path, controller) -> None:
    theme_file.unlink()
    _settle(150)
    assert controller.config.theme.color("accent") == "#922a5a"

    theme_file.write_text(
        "[theme]\nactive = fresh\n\n[fresh]\naccent = #00ff88\n",
        encoding="utf-8",
    )

    assert _run_until(
        lambda: controller.config.theme.color("accent") == "#00ff88"
    ), "recreated file was not picked up"


def test_rapid_writes_are_debounced(theme_file: Path, controller) -> None:
    emissions = []
    controller.connect("changed", lambda *_args: emissions.append(1))

    for value in ("#010101", "#020202", "#030303", "#040404", "#050505"):
        theme_file.write_text(
            VALID.replace("#922a5a", value), encoding="utf-8"
        )

    assert _run_until(
        lambda: controller.config.theme.color("accent") == "#050505"
    )
    _settle(300)
    assert len(emissions) <= 2, f"reload storm: {len(emissions)} emissions"


def test_invalid_reload_keeps_last_valid_theme(theme_file: Path, controller) -> None:
    theme_file.write_text("this is not ini\n[unclosed\n", encoding="utf-8")
    _settle(300)

    assert controller.config.theme.color("accent") == "#922a5a"

    theme_file.write_text(
        VALID.replace("#922a5a", "#00ff88"), encoding="utf-8"
    )

    assert _run_until(
        lambda: controller.config.theme.color("accent") == "#00ff88"
    ), "did not recover after the file became valid again"


#: A file that parses but is semantically broken, per F3 regression case.
INVALID_RELOADS = {
    "malformed": "[theme]\nactive = rose\n\n[rose\naccent = #00ff88\n",
    "theme-section-only": "[theme]\nactive = rose\nappearance = light\n",
    "unknown-active": "[theme]\nactive = missing\nappearance = light\n\n[rose]\naccent = #00ff88\n",
    "no-theme-definitions": "[theme]\nappearance = light\n\n[meta]\nname = nothing here\n",
    "invalid-appearance": "[theme]\nactive = rose\nappearance = sepia\n\n[rose]\naccent = #00ff88\n",
}


@pytest.mark.parametrize("case", sorted(INVALID_RELOADS))
def test_invalid_reload_never_replaces_the_last_valid_theme(
    case: str, theme_file: Path, controller
) -> None:
    """A bad reload keeps both the palette *and* the appearance in effect."""
    assert controller.config.active == "rose"
    assert controller.config.appearance is Appearance.DARK
    before_theme = controller.config.theme

    theme_file.write_text(INVALID_RELOADS[case], encoding="utf-8")
    _settle(300)

    assert controller.config.active == "rose"
    assert controller.config.appearance is Appearance.DARK
    assert controller.config.theme == before_theme
    assert controller.config.theme.color("accent") == "#922a5a"
    assert controller.monitor_active, "the watch must survive a bad reload"

    theme_file.write_text(
        VALID.replace("#922a5a", "#00ff88"), encoding="utf-8"
    )
    assert _run_until(
        lambda: controller.config.theme.color("accent") == "#00ff88"
    ), "did not recover once the file was repaired"


def test_startup_still_uses_the_builtin_fallback_for_a_bad_file(
    tmp_path: Path, gtk_registry
) -> None:
    """Startup degrades to the built-in palette instead of keeping nothing."""
    path = tmp_path / "themes.ini"
    path.write_text("this is not ini\n[unclosed\n", encoding="utf-8")
    instance = ThemeController(
        path,
        debounce_ms=30,
        display=object(),
        style_manager=_FakeStyleManager(),
    )
    try:
        instance.start()
        assert instance.config.active == "default"
        assert instance.config.theme.color("accent") == builtin_theme().color(
            "accent"
        )
    finally:
        instance.shutdown()


def test_fresh_profile_starts_watching_after_the_directory_appears(
    tmp_path: Path, gtk_registry
) -> None:
    """F1: a missing config directory must not disable live reload forever."""
    config_dir = tmp_path / "Minimalist-Game-Launcher"
    path = config_dir / "themes.ini"
    assert not config_dir.exists()

    instance = ThemeController(
        path,
        debounce_ms=30,
        display=object(),
        style_manager=_FakeStyleManager(),
    )
    instance.start()
    try:
        assert instance.monitor_active
        # Monitoring was established without creating the file itself.
        assert not path.exists()

        # The file appears externally, as it would when the user writes it.
        config_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "[theme]\nactive = fresh\nappearance = light\n\n"
            "[fresh]\nname = Fresh\naccent = #00ff88\n",
            encoding="utf-8",
        )

        assert _run_until(
            lambda: instance.config.theme.color("accent") == "#00ff88"
        ), "the file created after start() was never picked up"
        assert instance.config.appearance is Appearance.LIGHT

        # A later external change is still seen, proving the watch is live.
        path.write_text(
            "[theme]\nactive = fresh\n\n[fresh]\naccent = #123456\n",
            encoding="utf-8",
        )
        assert _run_until(
            lambda: instance.config.theme.color("accent") == "#123456"
        ), "later edits were not reloaded"
    finally:
        instance.shutdown()

    assert instance._monitor is None
    assert instance._debounce_source == 0
    path.write_text(
        "[theme]\nactive = fresh\n\n[fresh]\naccent = #654321\n",
        encoding="utf-8",
    )
    _settle(300)
    assert instance.config.theme.color("accent") == "#123456", (
        "a shutdown controller kept reloading"
    )


def test_saving_a_theme_re_arms_a_lost_monitor(
    theme_file: Path, gtk_registry
) -> None:
    """F1: if the watch is unusable, a successful save retries arming it."""
    instance = ThemeController(
        theme_file,
        debounce_ms=30,
        display=object(),
        style_manager=_FakeStyleManager(),
    )
    instance.start()
    try:
        assert instance.monitor_active
        instance._cancel_monitor()  # simulate a watch that failed to arm
        assert not instance.monitor_active

        instance.set_active_theme("evernight")

        assert instance.monitor_active
        theme_file.write_text(
            VALID.replace("#922a5a", "#00ff88"), encoding="utf-8"
        )
        assert _run_until(
            lambda: instance.config.theme.color("accent") == "#00ff88"
        ), "the re-armed watch never fired"
    finally:
        instance.shutdown()


@needs_display
def test_settings_dialog_follows_external_theme_changes(
    theme_file: Path, tmp_path: Path, gtk_registry, monkeypatch
) -> None:
    """F4: an externally written theme updates the open dialog's rows."""
    instance = ThemeController(
        theme_file,
        debounce_ms=30,
        display=object(),
        style_manager=_FakeStyleManager(),
    )
    instance.start()
    writes: list[tuple[str, str]] = []
    monkeypatch.setattr(
        ThemeController,
        "set_active_theme",
        lambda self, theme_id: writes.append(("theme", theme_id)) or True,
    )
    try:
        dialog = SettingsDialog(
            None, SettingsStore(tmp_path / "settings.json"), instance
        )
        assert dialog._theme_row.get_selected() == 2  # "rose"

        theme_file.write_text(
            VALID.replace("active = rose", "active = evernight"), encoding="utf-8"
        )

        assert _run_until(lambda: dialog._theme_row.get_selected() == 1), (
            "the dialog did not follow the external change"
        )
        assert writes == [], "a refresh must not be written back as a user pick"

        # A theme added externally also appears in the row.
        theme_file.write_text(
            VALID.replace("active = rose", "active = evernight")
            + "\n[ember]\nname = Ember\naccent = #cc5500\n",
            encoding="utf-8",
        )
        assert _run_until(lambda: "ember" in dialog._theme_ids)
        index = dialog._theme_row.get_selected()
        assert dialog._theme_ids[index] == "evernight"
        assert writes == []
    finally:
        instance.shutdown()


@needs_display
def test_settings_dialog_disconnects_when_closed(
    theme_file: Path, tmp_path: Path, gtk_registry
) -> None:
    instance = ThemeController(
        theme_file,
        debounce_ms=30,
        display=object(),
        style_manager=_FakeStyleManager(),
    )
    instance.start()
    try:
        window = Gtk.Window()
        dialog = SettingsDialog(
            window, SettingsStore(tmp_path / "settings.json"), instance
        )
        assert dialog._theme_handler is not None
        dialog.present(window)

        dialog.close()

        assert _run_until(lambda: dialog._theme_handler is None), (
            "closing the dialog must disconnect it from the controller"
        )
        emissions = []
        applied = []
        instance.connect("changed", lambda controller: applied.append(controller.config.active))
        original = dialog._refresh_theme_rows
        refreshes: list[int] = []
        dialog._refresh_theme_rows = lambda: refreshes.append(1) or original()
        instance.set_active_theme("evernight")
        _settle(300)
        assert applied and all(name == "evernight" for name in applied)
        assert refreshes == [], "the closed dialog must not react any more"
    finally:
        instance.shutdown()


def test_no_changes_after_shutdown(theme_file: Path, gtk_registry) -> None:
    instance = ThemeController(
        theme_file,
        debounce_ms=30,
        display=object(),
        style_manager=_FakeStyleManager(),
    )
    instance.start()
    emissions = []
    instance.connect("changed", lambda *_args: emissions.append(1))
    instance.shutdown()

    theme_file.write_text(
        VALID.replace("#922a5a", "#00ff88"), encoding="utf-8"
    )
    _settle(400)

    assert emissions == []
    assert instance._monitor is None


def test_shutdown_cancels_a_pending_reload(theme_file: Path, gtk_registry) -> None:
    instance = ThemeController(
        theme_file,
        debounce_ms=500,
        display=object(),
        style_manager=_FakeStyleManager(),
    )
    instance.start()
    emissions = []
    instance.connect("changed", lambda *_args: emissions.append(1))

    theme_file.write_text(
        VALID.replace("#922a5a", "#00ff88"), encoding="utf-8"
    )
    assert _run_until(lambda: instance._debounce_source != 0, timeout_ms=2000)
    instance.shutdown()
    _settle(700)

    assert emissions == []
    assert instance._debounce_source == 0
