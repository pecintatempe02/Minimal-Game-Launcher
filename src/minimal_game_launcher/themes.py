"""Theme configuration parsing, validation, and persistence.

Themes are read from a standalone INI file (``themes.ini``) that may define
several named palettes plus the active theme and the appearance mode. This
module uses only the standard library and never imports GTK, so parsing and
validation can be tested and reasoned about without a display.

Every failure mode degrades to a built-in palette: a missing file, a malformed
file, an unknown active theme, a missing key, or an invalid color never stops
the application from starting. Writing configuration updates only the affected
lines so comments and user formatting survive.
"""

from __future__ import annotations

import configparser
import hashlib
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Final

LOGGER = logging.getLogger(__name__)

#: Canonical semantic color keys. Configuration accepts either hyphens or
#: underscores (``accent-hover`` / ``accent_hover``).
SEMANTIC_KEYS: Final[tuple[str, ...]] = (
    "accent",
    "accent_hover",
    "background",
    "surface",
    "surface_elevated",
    "text",
    "text_muted",
    "border",
    "success",
    "warning",
    "danger",
)

#: Keys that describe the base surfaces. A theme that omits them inherits the
#: libadwaita light/dark palette, which keeps ``system``/``light``/``dark``
#: usable without the theme author shipping a palette per appearance.
BASE_KEYS: Final[frozenset[str]] = frozenset(
    {"background", "surface", "surface_elevated", "text", "text_muted", "border"}
)

#: Built-in fallbacks. Accent and status colors are explicit so a theme that
#: only names itself still looks intentional; base surfaces are inherited from
#: libadwaita so light and dark both work.
DEFAULT_COLORS: Final[dict[str, str]] = {
    "accent": "#922a5a",
    "accent_hover": "#b13a6f",
    "success": "#4caf6d",
    "warning": "#d9a441",
    "danger": "#d9534f",
}

BUILTIN_THEME_ID: Final[str] = "default"
BUILTIN_THEME_NAME: Final[str] = "Default"

_BOM = b"\xef\xbb\xbf"

_CONFIG_HEADER = (
    "# Minimal Game Launcher theme configuration.\n"
    "# [theme] holds the active theme and appearance mode; every other section\n"
    "# is a named palette of semantic colors written as #rgb or #rrggbb.\n"
    "# Edit freely: changes are picked up without restarting the launcher.\n"
)

_HEX_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_SECTION_HEADER = re.compile(r"^\s*\[(?P<name>[^\]]+)\]\s*$")
_OPTION_LINE = re.compile(r"^(?P<indent>\s*)(?P<key>[^=:#]+?)\s*[=:]\s*(?P<value>.*)$")


class Appearance(StrEnum):
    """How the application chooses between light and dark styling."""

    SYSTEM = "system"
    DARK = "dark"
    LIGHT = "light"


@dataclass(frozen=True, slots=True)
class ThemePalette:
    """One named theme: an id, a display name, and its configured colors."""

    theme_id: str
    name: str
    colors: dict[str, str] = field(default_factory=dict)

    def color(self, key: str) -> str | None:
        """Return the configured color, or the built-in default if any.

        Base surface keys have no built-in default: ``None`` means "inherit the
        libadwaita palette", which is what makes appearance switching work.
        """
        return self.colors.get(key) or DEFAULT_COLORS.get(key)

    @property
    def resolved(self) -> dict[str, str]:
        """Return every semantic key that has a usable value."""
        return {
            key: value
            for key in SEMANTIC_KEYS
            if (value := self.color(key)) is not None
        }


@dataclass(frozen=True, slots=True)
class ThemeConfig:
    """The parsed theme file: available themes, the active one, and the mode."""

    active: str
    appearance: Appearance
    themes: dict[str, ThemePalette]
    error: str | None = None
    warnings: tuple[str, ...] = ()
    #: Why this configuration must not replace a previously valid one during a
    #: live reload. ``None`` means the file parsed and described a usable theme.
    #: Startup ignores it: a bad file still degrades to the built-in palette.
    invalid_reason: str | None = None

    @property
    def theme(self) -> ThemePalette:
        """Return the active palette, falling back to the built-in theme."""
        return self.themes.get(self.active) or builtin_theme()


def builtin_theme() -> ThemePalette:
    """Return the built-in fallback palette."""
    return ThemePalette(BUILTIN_THEME_ID, BUILTIN_THEME_NAME, dict(DEFAULT_COLORS))


def default_config() -> ThemeConfig:
    """Return the configuration used when no usable file exists."""
    return ThemeConfig(
        active=BUILTIN_THEME_ID,
        appearance=Appearance.SYSTEM,
        themes={BUILTIN_THEME_ID: builtin_theme()},
    )


def parse_color(value: str | None) -> str | None:
    """Return a normalized ``#rrggbb`` color, or ``None`` when invalid.

    Only ``#rgb`` and ``#rrggbb`` are accepted. Eight-digit values are rejected
    on purpose: desktop themes write ``#AARRGGBB`` (alpha first) while GTK CSS
    writes ``#RRGGBBAA``, so silently accepting them would swap channels.
    """
    candidate = (value or "").strip()
    if not _HEX_COLOR.match(candidate):
        return None
    if len(candidate) == 4:
        candidate = "#" + "".join(character * 2 for character in candidate[1:])
    return candidate.lower()


def slugify(name: str) -> str:
    """Turn a theme name into a stable section identifier."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().casefold()).strip("-")
    return slug or "theme"


def sanitize_theme_name(name: str) -> str:
    """Return an imported display name as safe, single-line INI metadata.

    Desktop theme names are untrusted input. Control characters (including CR
    and LF) are collapsed, so a crafted name cannot start a new section or
    setting line when written as ``name = ...``.
    """
    cleaned = re.sub(r"[\x00-\x1f\x7f\ud800-\udfff]+", " ", name)
    return " ".join(cleaned.split())


#: Section names that carry configuration metadata, not a theme. A theme in one
#: of these sections is either unaddressable (``[theme]``) or reinterpreted as
#: the legacy palette (``[colors]``), so both are reserved.
RESERVED_SECTIONS: Final[frozenset[str]] = frozenset({"theme", "colors"})

#: Sections that never describe a theme, including the desktop-theme metadata
#: section that ``mgl-theme-import`` copies in for reference.
_NON_THEME_SECTIONS: Final[frozenset[str]] = RESERVED_SECTIONS | {"meta"}


def theme_identifier(name: str) -> str:
    """Return a deterministic, collision-resistant section id for a name.

    A name that is already a safe lowercase slug keeps that slug, which keeps
    repeated imports of the same directory stable. Names whose slug would be
    lossy (spaces, punctuation, non-ASCII) or reserved gain a short digest of
    the original normalized name, so distinct desktop theme names cannot
    silently overwrite one another.
    """
    normalized = name.strip().casefold()
    slug = slugify(name)
    if slug == normalized and slug not in RESERVED_SECTIONS:
        return slug
    digest = hashlib.sha256(
        normalized.encode("utf-8", errors="surrogatepass")
    ).hexdigest()[:8]
    return f"{slug}-{digest}"


def _canonical_key(raw_key: str) -> str | None:
    """Map a configured option name to its canonical semantic key."""
    key = raw_key.strip().casefold().replace("-", "_").replace(" ", "_")
    return key if key in SEMANTIC_KEYS else None


def _read_text(path: Path) -> tuple[str | None, str | None]:
    """Read ``path`` as text, tolerating and stripping a UTF-8 BOM.

    ``utf-8-sig`` keeps a byte-order mark out of the first section header: a
    plain ``utf-8`` read leaves ``\\ufeff`` at the start of the first line, and
    ``configparser`` then reports "File contains no section headers".
    """
    try:
        return path.read_text(encoding="utf-8-sig"), None
    except (OSError, UnicodeError) as error:
        return None, f"{path.name}: {error}"


def _read_parser(path: Path) -> tuple[configparser.ConfigParser | None, str | None]:
    """Read an INI file, returning ``(parser, error_message)``."""
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    text, error = _read_text(path)
    if text is None:
        LOGGER.warning("Could not read theme configuration %s: %s", path, error)
        return None, error
    try:
        parser.read_string(text)
    except configparser.Error as parse_error:
        LOGGER.warning(
            "Could not read theme configuration %s: %s", path, parse_error
        )
        return None, f"{path.name}: {parse_error}"
    return parser, None


def _palette_from_section(
    theme_id: str,
    section: configparser.SectionProxy,
    warnings: list[str],
) -> ThemePalette:
    """Build one palette from a section, rejecting invalid colors safely."""
    name = (section.get("name") or "").strip() or theme_id
    colors: dict[str, str] = {}
    for raw_key, raw_value in section.items():
        key = _canonical_key(raw_key)
        if key is None:
            continue
        value = parse_color(raw_value)
        if value is None:
            warnings.append(
                f"theme {theme_id!r}: ignoring invalid color {raw_key} = "
                f"{raw_value!r}"
            )
            continue
        colors[key] = value
    return ThemePalette(theme_id, name, colors)


def _legacy_palette(
    parser: configparser.ConfigParser,
    warnings: list[str],
    *,
    allow_named: bool,
) -> tuple[str, ThemePalette] | None:
    """Return the ``[colors]`` palette, or ``None`` if this is not legacy layout."""
    if not parser.has_section("colors"):
        return None
    # In the primary file ``[colors]`` is only the legacy palette when no named
    # theme section exists. A document that defines themes *and* a ``[colors]``
    # section is a modern file with a stray section; dropping the named themes
    # would silently discard the user's data.
    if not allow_named and any(
        name.strip().casefold() not in _NON_THEME_SECTIONS
        for name in parser.sections()
    ):
        return None

    name = ""
    if parser.has_section("theme"):
        name = (parser.get("theme", "name", fallback="") or "").strip()
    legacy_id = theme_identifier(name) if name else BUILTIN_THEME_ID
    palette = _palette_from_section(legacy_id, parser["colors"], warnings)
    if name:
        palette = replace(palette, name=name)
    return legacy_id, palette


def _parse_document(
    parser: configparser.ConfigParser,
    *,
    legacy: bool = False,
) -> tuple[dict[str, ThemePalette], str | None, str | None, list[str]]:
    """Return themes, raw active id, raw appearance, and warnings."""
    warnings: list[str] = []
    themes: dict[str, ThemePalette] = {}

    global_section = parser["theme"] if parser.has_section("theme") else None
    active_raw = None
    appearance_raw = None
    if global_section is not None:
        active_raw = (global_section.get("active") or "").strip() or None
        appearance_raw = (
            (global_section.get("appearance") or global_section.get("mode") or "")
            .strip()
            or None
        )

    legacy_palette = _legacy_palette(parser, warnings, allow_named=legacy)
    if legacy_palette is not None:
        legacy_id, palette = legacy_palette
        themes[legacy_id] = palette
        active_raw = active_raw or legacy_id
        return themes, active_raw, appearance_raw, warnings

    for section_name in parser.sections():
        key = section_name.strip().casefold()
        if key in _NON_THEME_SECTIONS:
            continue
        themes[key] = _palette_from_section(
            key, parser[section_name], warnings
        )

    return themes, active_raw, appearance_raw, warnings


def _load_legacy(
    legacy_path: str | Path | None,
) -> tuple[dict[str, ThemePalette], str | None, str | None, list[str]]:
    """Read the legacy single-theme file, returning nothing when absent.

    The legacy file is *defined* by its single-theme layout, so a ``[colors]``
    section is always accepted there even when other sections exist.
    """
    if legacy_path is None:
        return {}, None, None, []
    legacy = Path(legacy_path).expanduser()
    if not legacy.is_file():
        return {}, None, None, []
    parser, _error = _read_parser(legacy)
    if parser is None:
        return {}, None, None, []
    return _parse_document(parser, legacy=True)


def load_themes(
    path: str | Path | None,
    legacy_path: str | Path | None = None,
) -> ThemeConfig:
    """Load the theme file, returning built-in defaults on any failure.

    ``legacy_path`` optionally points at a single-theme ``theme.ini`` written by
    an earlier build. It is read-only and acts as the base layer: a theme of the
    same id in ``themes.ini`` always wins, and the legacy file itself is never
    rewritten.

    ``invalid_reason`` records why the *primary* file cannot be trusted. It is
    what a live reload uses to refuse a semantically broken file; startup
    ignores it and keeps the built-in fallback so the application always runs.
    """
    legacy_themes, legacy_active, legacy_appearance, legacy_warnings = _load_legacy(
        legacy_path
    )
    themes: dict[str, ThemePalette] = {BUILTIN_THEME_ID: builtin_theme()}
    themes.update(legacy_themes)
    warnings = list(legacy_warnings)
    active_raw = legacy_active
    appearance_raw = legacy_appearance

    if path is None:
        return _finalize(themes, active_raw, appearance_raw, warnings)

    file_path = Path(path).expanduser()
    if not file_path.is_file():
        return _finalize(themes, active_raw, appearance_raw, warnings)

    parser, error = _read_parser(file_path)
    if parser is None:
        config = _finalize(themes, active_raw, appearance_raw, warnings)
        return replace(config, error=error, invalid_reason=error)

    document_themes, document_active, document_appearance, document_warnings = (
        _parse_document(parser)
    )
    themes.update(document_themes)
    warnings.extend(document_warnings)
    if document_active is not None:
        active_raw = document_active
    if document_appearance is not None:
        appearance_raw = document_appearance

    reason = None
    if not document_themes and not legacy_themes:
        reason = "the file defines no theme"
    elif document_active is not None:
        candidate = document_active.strip().casefold()
        if candidate not in themes:
            reason = f"unknown active theme {document_active!r}"
    if reason is None:
        reason = _appearance_error(appearance_raw)

    config = _finalize(themes, active_raw, appearance_raw, warnings)
    return replace(config, invalid_reason=reason)


def _appearance_error(raw: str | None) -> str | None:
    """Return why ``raw`` is not a usable appearance value, or ``None``."""
    if raw is None:
        return None
    try:
        Appearance(raw.strip().casefold())
    except ValueError:
        return f"unknown appearance {raw!r}"
    return None


def _finalize(
    themes: dict[str, ThemePalette],
    active_raw: str | None,
    appearance_raw: str | None,
    warnings: list[str],
) -> ThemeConfig:
    """Validate the active theme and appearance and log any warnings."""
    appearance = Appearance.SYSTEM
    if appearance_raw is not None:
        try:
            appearance = Appearance(appearance_raw.strip().casefold())
        except ValueError:
            warnings.append(
                f"unknown appearance {appearance_raw!r}; using 'system'"
            )

    active = BUILTIN_THEME_ID
    if active_raw is not None:
        candidate = active_raw.strip().casefold()
        if candidate in themes:
            active = candidate
        else:
            warnings.append(
                f"unknown active theme {active_raw!r}; using {active!r}"
            )
    elif themes:
        active = next(iter(themes))

    config = ThemeConfig(
        active=active,
        appearance=appearance,
        themes=themes,
        error=None,
        warnings=tuple(warnings),
    )
    for warning in config.warnings:
        LOGGER.warning("Theme configuration: %s", warning)
    return config


def set_option(text: str, section: str, key: str, value: str) -> str:
    """Return ``text`` with ``key = value`` set inside ``[section]``.

    Pure text editing: every other line, including comments and unknown
    options, is passed through unchanged. The section header is created when
    missing; a missing option is appended to the end of the section.
    """
    new_line = f"{key} = {value}"
    lines = text.splitlines()
    output: list[str] = []
    inside = False
    header_index: int | None = None
    replaced = False

    for raw in lines:
        header = _SECTION_HEADER.match(raw)
        if header is not None:
            inside = header.group("name").strip().casefold() == section.casefold()
            if inside and header_index is None:
                header_index = len(output)
            output.append(raw)
            continue

        if inside and not replaced:
            option = _OPTION_LINE.match(raw)
            if option is not None and option.group("key").strip().casefold() == key:
                # A trailing comment on this line is intentionally dropped; the
                # value it described no longer applies.
                output.append(f"{option.group('indent')}{new_line}")
                replaced = True
                continue
        elif inside:
            # A duplicate key would shadow the value written above because
            # configparser lets the last occurrence win. Drop it so the update
            # actually takes effect.
            option = _OPTION_LINE.match(raw)
            if option is not None and option.group("key").strip().casefold() == key:
                continue

        output.append(raw)

    if not replaced:
        if header_index is None:
            if output and output[-1].strip():
                output.append("")
            output.extend([f"[{section}]", new_line])
        else:
            # Append after the last non-blank line of the section so any
            # comment describing the existing options stays attached to them.
            insert_at = header_index + 1
            for index in range(header_index + 1, len(output)):
                if _SECTION_HEADER.match(output[index]):
                    break
                if output[index].strip():
                    insert_at = index + 1
            output.insert(insert_at, new_line)

    trailing = text.endswith(("\n", "\r")) or not text
    return "\n".join(output) + ("\n" if trailing else "")


def replace_section(text: str, section: str, body: list[str]) -> str:
    """Return ``text`` with ``[section]`` replaced by ``body``.

    The section is removed from its old position and re-appended, which keeps
    other sections byte-identical. Comments inside the replaced section are
    regenerated with the body.
    """
    output: list[str] = []
    skipping = False
    for raw in text.splitlines():
        header = _SECTION_HEADER.match(raw)
        if header is not None:
            skipping = header.group("name").strip().casefold() == section.casefold()
        if not skipping:
            output.append(raw)

    while output and not output[-1].strip():
        output.pop()
    if output:
        output.append("")
    output.append(f"[{section}]")
    output.extend(body)
    return "\n".join(output) + "\n"


def read_theme_text(path: str | Path) -> str:
    """Return a theme file's text without a UTF-8 BOM, or ``""`` if absent.

    Raises ``OSError`` for an unreadable file or invalid UTF-8 so callers can
    report a real error instead of overwriting data they could not read.
    """
    file_path = Path(path).expanduser()
    try:
        data = file_path.read_bytes()
    except FileNotFoundError:
        return ""
    except OSError as error:
        raise OSError(f"{file_path}: {error}") from error
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise OSError(f"{file_path}: not valid UTF-8 ({error})") from error


def write_theme_file(path: str | Path, text: str) -> None:
    """Write ``text`` atomically, preserving symlinks, permissions and newlines.

    A symlinked configuration keeps its symlink: the resolved target is guration keeps its symlink: the resolved target is written
    instead. Only targets whose parent directory already exists are followed, so
    a symlink cannot make the launcher create files in an unexpected location.
    """
    file_path = Path(path).expanduser()
    target = _write_target(file_path)

    newline = "\n"
    mode: int | None = None
    bom = False
    if target.exists():
        try:
            raw = target.read_bytes()
            mode = target.stat().st_mode & 0o7777
        except OSError as error:
            raise OSError(f"{target}: {error}") from error
        bom = raw.startswith(_BOM)
        if b"\r\n" in (raw[len(_BOM):] if bom else raw):
            newline = "\r\n"

    normalized = text.replace("\r\n", "\n")
    if newline != "\n":
        normalized = normalized.replace("\n", newline)
    data = normalized.encode("utf-8")
    if bom:
        data = _BOM + data
    _atomic_write_bytes(target, data, mode)


def _write_target(file_path: Path) -> Path:
    """Return the regular file to write, following a symlink to its target."""
    if not file_path.is_symlink():
        return file_path
    target = Path(os.path.realpath(file_path))
    if not target.parent.is_dir():
        raise OSError(
            f"{file_path}: the symlink target directory {target.parent} does not "
            "exist; refusing to create it"
        )
    return target


def update_theme_setting(path: str | Path, key: str, value: str) -> None:
    """Set one ``[theme]`` option, preserving every other line.

    ``configparser`` cannot round-trip comments, so this edits the file as
    text: only the matching option line is replaced, or appended to the
    ``[theme]`` section. The write is atomic and refuses to touch a file the
    application could not parse. ``OSError`` propagates so the UI can report an
    unwritable or malformed file.
    """
    file_path = Path(path).expanduser()
    target = _write_target(file_path)

    text = read_theme_text(target)
    if not text and not target.exists():
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            _create_theme_file(target, key, value)
            return
        except FileExistsError:
            # Lost a creation race: edit the file that won instead of replacing
            # it, so the other writer's content is not discarded.
            text = read_theme_text(target)
            if not text:
                raise OSError(
                    f"{target}: appeared while creating it; not overwriting"
                ) from None

    if text.strip():
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            parser.read_string(text)
        except configparser.Error as error:
            raise OSError(
                f"{target}: refusing to modify an unparsable file ({error})"
            ) from error

    write_theme_file(target, set_option(text, "theme", key, value))


def _create_theme_file(target: Path, key: str, value: str) -> None:
    """Create a new configuration file, failing if it appeared concurrently."""
    body = (
        f"{_CONFIG_HEADER}\n[theme]\n{key} = {value}\n"
    )
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(body)


def _atomic_write_bytes(target: Path, data: bytes, mode: int | None) -> None:
    """Replace ``target`` with ``data`` through a temporary file beside it."""
    try:
        descriptor, temporary = tempfile.mkstemp(
            dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
        )
    except OSError as error:
        raise OSError(f"{target}: {error}") from error
    path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        if mode is not None:
            os.chmod(path, mode)
        os.replace(path, target)
    except OSError:
        path.unlink(missing_ok=True)
        raise
