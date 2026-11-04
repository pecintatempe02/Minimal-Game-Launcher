"""GTK presentation for the theme service: CSS, appearance, live reload.

The controller owns exactly one :class:`Gtk.CssProvider` and registers it once
per display at a priority above libadwaita's own stylesheet, so generated
``@define-color`` overrides win. Reloading simply replaces the provider's
contents; it never registers a second provider and never touches the library,
covers, or running games.

Appearance mode and palette are independent: the color scheme comes from
libadwaita's ``Adw.StyleManager`` while the accent and status colors come from
the active palette, so switching light/dark keeps the chosen accent.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk  # noqa: E402

from minimal_game_launcher.themes import (  # noqa: E402
    SEMANTIC_KEYS,
    Appearance,
    ThemeConfig,
    ThemePalette,
    load_themes,
    update_theme_setting,
)

LOGGER = logging.getLogger(__name__)

#: Above ``GTK_STYLE_PROVIDER_PRIORITY_USER`` because libadwaita installs its
#: own palette at that priority and would otherwise win the cascade.
PROVIDER_PRIORITY = Gtk.STYLE_PROVIDER_PRIORITY_USER + 1

_COLOR_SCHEMES = {
    Appearance.SYSTEM: Adw.ColorScheme.DEFAULT,
    Appearance.DARK: Adw.ColorScheme.FORCE_DARK,
    Appearance.LIGHT: Adw.ColorScheme.FORCE_LIGHT,
}

#: Semantic base colors map onto libadwaita's named colors. Base keys are
#: emitted only when the theme defines them; otherwise libadwaita's own
#: light/dark values stay in effect.
_BASE_TARGETS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("background", ("window_bg_color", "view_bg_color", "headerbar_bg_color")),
    ("surface", ("card_bg_color",)),
    ("surface_elevated", ("popover_bg_color", "dialog_bg_color")),
    ("text", ("window_fg_color", "view_fg_color")),
    ("border", ("borders",)),
)

#: Status colors feed the widgets that already use libadwaita's status palette.
_STATUS_TARGETS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("success", ("success_color", "success_bg_color")),
    ("warning", ("warning_color", "warning_bg_color")),
    (
        "danger",
        (
            "error_color",
            "error_bg_color",
            "destructive_color",
            "destructive_bg_color",
        ),
    ),
)


def _contrast_text(color: str) -> str:
    """Return black or white, whichever reads better on ``color``."""
    red, green, blue = (
        int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)
    )

    def linearize(channel: float) -> float:
        return (
            channel / 12.92
            if channel <= 0.03928
            else ((channel + 0.055) / 1.055) ** 2.4
        )

    luminance = (
        0.2126 * linearize(red)
        + 0.7152 * linearize(green)
        + 0.0722 * linearize(blue)
    )
    return "#000000" if luminance > 0.5 else "#ffffff"


def build_theme_css(palette: ThemePalette) -> str:
    """Generate the GTK stylesheet for one palette.

    The first block defines ``@mgl_*`` aliases that fall back to libadwaita, so
    ``style.css`` can reference them unconditionally even when a theme omits
    the base surfaces. The second block overrides what the theme actually set.
    """
    colors = palette.resolved
    lines = [
        "/* Generated from the active launcher theme. Do not edit. */",
        "@define-color mgl_accent @accent_color;",
        "@define-color mgl_accent_hover @accent_color;",
        "@define-color mgl_background @window_bg_color;",
        "@define-color mgl_surface @card_bg_color;",
        "@define-color mgl_surface_elevated @popover_bg_color;",
        "@define-color mgl_text @window_fg_color;",
        "@define-color mgl_text_muted alpha(@window_fg_color, 0.7);",
        "@define-color mgl_border @borders;",
    ]

    accent = colors.get("accent")
    if accent:
        lines.extend(
            (
                f"@define-color accent_color {accent};",
                f"@define-color accent_bg_color {accent};",
                f"@define-color accent_fg_color {_contrast_text(accent)};",
                f"@define-color mgl_accent {accent};",
            )
        )
    hover = colors.get("accent_hover")
    if hover:
        lines.append(f"@define-color mgl_accent_hover {hover};")

    for key, targets in _BASE_TARGETS:
        value = colors.get(key)
        if value:
            for target in targets:
                lines.append(f"@define-color {target} {value};")

    for key in SEMANTIC_KEYS:
        value = colors.get(key)
        if value and key not in {"accent", "accent_hover"}:
            lines.append(f"@define-color mgl_{key} {value};")

    for key, targets in _STATUS_TARGETS:
        value = colors.get(key)
        if value:
            for target in targets:
                lines.append(f"@define-color {target} {value};")

    return "\n".join(lines) + "\n"


class ThemeController(GObject.Object):
    """Apply the theme palette and appearance mode, and watch the file.

    One instance per application. ``start()`` registers the provider and arms a
    :class:`Gio.FileMonitor`; ``shutdown()`` removes both. Every file event is
    debounced so an editor that writes several times in a row causes one reload.
    """

    __gsignals__ = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    DEBOUNCE_MS = 250

    def __init__(
        self,
        path: str | Path,
        legacy_path: str | Path | None = None,
        *,
        debounce_ms: int | None = None,
        style_manager: Adw.StyleManager | None = None,
        display: Gdk.Display | None = None,
    ) -> None:
        super().__init__()
        self.path = Path(path).expanduser()
        self.legacy_path = (
            Path(legacy_path).expanduser() if legacy_path is not None else None
        )
        self._debounce_ms = self.DEBOUNCE_MS if debounce_ms is None else debounce_ms
        self._style_manager = style_manager or Adw.StyleManager.get_default()
        self._preferred_display = display
        self._provider = Gtk.CssProvider()
        self._display: Gdk.Display | None = None
        self._monitor: Gio.FileMonitor | None = None
        self._debounce_source = 0
        self._config = load_themes(self.path, self.legacy_path)
        self._started = False

    @property
    def config(self) -> ThemeConfig:
        """Return the last successfully loaded configuration."""
        return self._config

    def start(self) -> None:
        """Register the CSS provider, apply the theme, and watch the file."""
        if self._started:
            return
        self._started = True

        display = self._preferred_display or Gdk.Display.get_default()
        if display is None:
            LOGGER.warning(
                "No GDK display; theme CSS is generated but not applied"
            )
        else:
            self._display = display
            Gtk.StyleContext.add_provider_for_display(
                display, self._provider, PROVIDER_PRIORITY
            )
        self._apply()
        self._arm_monitor()

    def shutdown(self) -> None:
        """Remove the provider and cancel monitoring and pending callbacks."""
        self._cancel_monitor()
        if self._debounce_source:
            GLib.source_remove(self._debounce_source)
            self._debounce_source = 0
        if self._display is not None:
            Gtk.StyleContext.remove_provider_for_display(
                self._display, self._provider
            )
            self._display = None
        self._started = False

    def reload(self) -> ThemeConfig:
        """Re-read the configuration and apply it, keeping the last valid one."""
        return self._reload(keep_on_error=True)

    def set_active_theme(self, theme_id: str) -> bool:
        """Select and persist a theme, applying it immediately.

        The palette is applied before the file is written so the preview works
        even when the configuration file is read-only; ``OSError`` is raised for
        the caller to report. Returns ``False`` for an unknown theme id or when
        the generated stylesheet is rejected; in the latter case neither the
        in-memory nor the on-disk selection changes.
        """
        if theme_id not in self._config.themes:
            return False
        previous = self._config
        self._config = replace(previous, active=theme_id)
        if not self._apply():
            self._config = previous
            return False
        self._persist("active", theme_id)
        return True

    def set_appearance(self, appearance: Appearance | str) -> None:
        """Force system, dark, or light appearance without changing the palette."""
        mode = Appearance(str(appearance))
        previous = self._config
        self._config = replace(previous, appearance=mode)
        if not self._apply():
            self._config = previous
            return
        self._persist("appearance", mode.value)

    def _reload(self, *, keep_on_error: bool) -> ThemeConfig:
        """Reload from disk; optionally retain the last valid configuration."""
        before = self._config
        config = load_themes(self.path, self.legacy_path)
        # A missing, unparsable, or semantically empty file must not replace a
        # working theme; the previous configuration stays in effect until the
        # file is repaired, and the monitor keeps watching for that repair.
        problem = config.error or config.invalid_reason
        unusable = problem is not None or not self.path.is_file()
        if keep_on_error and unusable:
            LOGGER.warning(
                "Keeping the previous theme; %s is unusable: %s",
                self.path,
                problem or "file is missing",
            )
            self._config = before
        else:
            self._config = config
        if not self._apply():
            # The provider still holds the last stylesheet; keep the matching
            # in-memory configuration instead of reporting a change that was
            # never applied.
            self._config = before
        return self._config

    def _apply(self) -> bool:
        """Push the palette and mode out; return whether the CSS was applied."""
        if not self._load_stylesheet(build_theme_css(self._config.theme)):
            return False
        try:
            self._style_manager.set_color_scheme(
                _COLOR_SCHEMES[self._config.appearance]
            )
        except (TypeError, KeyError):
            LOGGER.warning(
                "Could not apply appearance mode %r", self._config.appearance
            )
        self.emit("changed")
        return True

    def _load_stylesheet(self, css: str) -> bool:
        """Replace the provider contents only if GTK can parse ``css``.

        ``Gtk.CssProvider.load_from_string`` reports most syntax errors as GTK
        warnings and leaves the provider empty rather than raising, so the
        stylesheet is parsed into a throwaway provider first. A rejected
        stylesheet never replaces the last good one.
        """
        scratch = Gtk.CssProvider()
        try:
            scratch.load_from_string(css)
        except GLib.Error:
            LOGGER.exception("Generated theme CSS was rejected; keeping the old one")
            return False
        try:
            parsed = scratch.to_string()
        except GLib.Error:
            LOGGER.exception("Could not verify theme CSS; keeping the old one")
            return False
        if not parsed.strip():
            LOGGER.error(
                "Generated theme CSS did not parse; keeping the previous stylesheet"
            )
            return False
        try:
            self._provider.load_from_string(css)
        except GLib.Error:
            LOGGER.exception("Could not apply theme CSS; keeping the old one")
            return False
        return True

    @property
    def monitor_active(self) -> bool:
        """Return whether a file watch is currently armed."""
        return self._monitor is not None

    def _arm_monitor(self) -> None:
        """Watch the theme file, replacing any previous monitor.

        The parent directory is created first: a fresh profile has no config
        directory at all, and watching a file inside a missing directory yields
        no events, which would leave live reload permanently inactive.
        """
        self._cancel_monitor()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            LOGGER.warning(
                "Could not create %s; live theme reload is unavailable: %s",
                self.path.parent,
                error,
            )
            return
        try:
            monitor = Gio.File.new_for_path(str(self.path)).monitor_file(
                Gio.FileMonitorFlags.WATCH_MOVES, None
            )
        except GLib.Error:
            LOGGER.warning("Could not watch %s for theme changes", self.path)
            return
        monitor.connect("changed", self._on_file_changed)
        self._monitor = monitor

    def _cancel_monitor(self) -> None:
        """Disconnect and cancel the active monitor, if any."""
        if self._monitor is None:
            return
        self._monitor.disconnect_by_func(self._on_file_changed)
        self._monitor.cancel()
        self._monitor = None

    def _on_file_changed(
        self,
        _monitor: Gio.FileMonitor,
        _file: Gio.File,
        _other: Gio.File | None,
        _event: Gio.FileMonitorEvent,
    ) -> None:
        """Debounce any change; atomic replacement fires several events."""
        self._schedule_reload()

    def _schedule_reload(self) -> None:
        """(Re)start the debounce timer."""
        if self._debounce_source:
            GLib.source_remove(self._debounce_source)
        self._debounce_source = GLib.timeout_add(
            self._debounce_ms, self._on_debounce
        )

    def _on_debounce(self) -> bool:
        """Reload once the burst of events has settled, then re-arm the watch."""
        self._debounce_source = 0
        self._reload(keep_on_error=True)
        # A rename-based replacement can detach the inode watch; re-arm so
        # later edits are still seen.
        self._arm_monitor()
        return GLib.SOURCE_REMOVE

    # -- persistence -------------------------------------------------------

    def _persist(self, key: str, value: str) -> None:
        """Write one ``[theme]`` option and re-arm the watch if it was unusable.

        ``OSError`` propagates so the caller can report an unwritable or
        malformed file; the in-memory choice still applies for the session.
        """
        update_theme_setting(self.path, key, value)
        if self._started and self._monitor is None:
            # The write may have created the directory that made monitoring
            # possible in the first place.
            self._arm_monitor()
