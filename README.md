# Minimal Game Launcher

A small GTK4/libadwaita game library and launcher for Linux, built with Python
3, PyGObject, SQLite, and GdkPixbuf. It is designed for Arch Linux and Hyprland.

Minimal Game Launcher is a small GTK4/libadwaita library and launcher for
Linux. Games can be registered, edited, searched, sorted, launched, and
removed. Windows games use per-game Wine prefixes; native games run directly.
Optional GameMode and MangoHud wrappers are configured independently per game.
Covers can be selected from local images or, optionally, found through
SteamGridDB. It is developed for Arch Linux and Hyprland.

## Overview

- **Library**: a responsive grid of cards with cover, platform, and running
  state. Search (debounced) and sorting by name or recently added. Games can be
  hidden from the normal library without deleting them.
- **Details**: a read-only view of every stored field, with launch, cover,
  diagnostics, Wine tools, edit, and remove actions.
- **Launching**: runs off the GTK thread. Running games are tracked by a
  single main-loop poll that stops when no game is active.
- **Wine runtime diagnostics**: Windows launch output is captured to a bounded
  per-game log and summarized on demand.
- **First run**: a short setup wizard explains the app and records preferences.
  It installs nothing and starts no service.
- **Recovery**: a damaged library database is set aside, not deleted, and an
  empty library is created. The window explains what happened.

## Dependencies

Install on Arch Linux:

```sh
sudo pacman -S --needed python python-gobject gtk4 libadwaita gdk-pixbuf2 \
    xdg-desktop-portal
```

| Package | Purpose | Required |
| --- | --- | --- |
| `python` | Runtime (3.11+; developed on 3.14) | yes |
| `python-gobject` | GTK bindings (PyGObject) | yes |
| `gtk4` | Widget toolkit | yes |
| `libadwaita` | GNOME-style widgets (dialogs, rows, status pages) | yes |
| `gdk-pixbuf2` | Cover decoding and validation | yes |
| `xdg-desktop-portal` + backend (e.g. `xdg-desktop-portal-gtk`, `xdg-desktop-portal-hyprland`) | Native file chooser dialogs | for file selection |
| `wine` | Runs Windows games in per-game prefixes | for Windows games, unless a custom Wine is set |
| `gamemode` (`gamemoderun`) | Optional GameMode wrapper | optional |
| `mangohud` | Optional MangoHud overlay | optional |
| `gamescope` | Optional per-game compositor/upscaler | optional; required only when a game enables it |
| `winetricks` | Optional component installer for Wine prefixes | optional |
| `python-pytest` | Test suite | development only |

The launcher does not install Wine, DXVK, VKD3D, .NET, VC++ runtimes,
GameMode, MangoHud, gamescope, or Winetricks. Missing GameMode or MangoHud
produces a warning and the game still starts. A game that explicitly enables
gamescope refuses to start when the binary is missing, because silently
dropping it would change the display mode.

## Install on Arch Linux

The installer is user-level. It writes only under `~/.local` and needs no root.

```sh
git clone https://github.com/pecintatempe02/Minimalist-Game-Launcher.git
cd Minimalist-Game-Launcher
scripts/install.sh
```

This installs:

- `~/.local/bin/minimal-game-launcher`: launcher command.
- `~/.local/lib/minimal-game-launcher/`: copy of the Python package.
- `~/.local/share/applications/io.github.minimal_game_launcher.MinimalGameLauncher.desktop`:
  desktop entry named "Minimal Game Launcher" (`Categories=Game;Utility`,
  `Terminal=false`).
- `~/.local/share/icons/hicolor/scalable/apps/io.github.minimal_game_launcher.MinimalGameLauncher.svg`:
  application icon.

Set `PREFIX=/some/path` to install elsewhere. If `~/.local/bin` is not on
`PATH`, run the full path.

Release builds are published as
`Minimal-Game-Launcher-0.5.0-x86_64.AppImage` with a matching `.sha256`
checksum. The AppImage uses the host GTK4/libadwaita/PyGObject runtime; it was
verified only on the build host and cross-distribution portability is not
claimed.

Uninstall:

```sh
scripts/install.sh --uninstall
```

Uninstall removes only the program files above. It never removes user data
(see **Data storage locations**). Delete those directories manually if you
no longer want them.

## Run

From the installed desktop entry, or:

```sh
minimal-game-launcher
```

Run from a source checkout without installing:

```sh
PYTHONPATH=src python3 -m minimal_game_launcher
```

## Paths

- Library database:
  `$XDG_DATA_HOME/minigame-launcher/library.db`
  (normally `~/.local/share/minigame-launcher/library.db`).
- Wine prefixes:
  `$XDG_DATA_HOME/minigame-launcher/prefixes/{game-id}/`
  (normally `~/.local/share/minigame-launcher/prefixes/{game-id}/`).
- Settings: `$XDG_CONFIG_HOME/minigame-launcher/settings.json`.
- Optional SteamGridDB key:
  `$XDG_CONFIG_HOME/minigame-launcher/steamgriddb.key` (mode `0600`).
  The `STEAMGRIDDB_API_KEY` environment variable is used as a fallback.
- Cover cache: `$XDG_DATA_HOME/minigame-launcher/covers/`.
- Installer staging: `$XDG_DATA_HOME/minigame-launcher/installers/{game-id}/`.
- Managed game installations: `$XDG_DATA_HOME/minigame-launcher/games/{game-id}/`.
- Optional desktop entry: `$XDG_DATA_HOME/applications/`.
- Optional desktop icon: `$XDG_DATA_HOME/icons/hicolor/scalable/apps/`.
- Diagnostic log: `$XDG_STATE_HOME/minigame-launcher/launcher.log`.
- Per-game launch output:
  `$XDG_STATE_HOME/minigame-launcher/launch-logs/{game-id}.log`.
- Wine tool output:
  `$XDG_STATE_HOME/minigame-launcher/wine-tools/{game-id}.log`.
- Themes: `$XDG_CONFIG_HOME/Minimalist-Game-Launcher/themes.ini` (optional;
  see [Themes](#themes)).

On startup, a Phase 2 database stored under the old
`minimal-game-launcher` directory is copied once to the current directory. The
old files are left untouched. Phase 3 databases are upgraded in place with the
new per-game performance toggles and their documented defaults.

## Data storage locations

| Data | Location |
| --- | --- |
| Library database | `$XDG_DATA_HOME/minigame-launcher/library.db` (usually `~/.local/share/minigame-launcher/library.db`) |
| Wine prefixes | `$XDG_DATA_HOME/minigame-launcher/prefixes/{game-id}/` |
| Cover cache | `$XDG_DATA_HOME/minigame-launcher/covers/` |
| Settings | `$XDG_CONFIG_HOME/minigame-launcher/settings.json` |
| SteamGridDB key (optional) | `$XDG_CONFIG_HOME/minigame-launcher/steamgriddb.key` (mode `0600`) |
| Diagnostic log | `$XDG_STATE_HOME/minigame-launcher/launcher.log` (rotating) |
| Themes (optional) | `$XDG_CONFIG_HOME/Minimalist-Game-Launcher/themes.ini` |
| Damaged library backups | `library.db.damaged-<UTC timestamp>` next to `library.db` |

Program files live under `~/.local/lib/minimal-game-launcher/` and
`~/.local/bin/`. They are separate from all of the above.

## Backup and restore

Back up while the launcher is closed so the database and its write-ahead log
are consistent:

```sh
tar -C ~ -czf minigame-launcher-backup.tar.gz \
    .local/share/minigame-launcher .config/minigame-launcher
```

The archive includes the library, Wine prefixes, covers, settings, and the API
key file. To back up library metadata only, add
`--exclude=.local/share/minigame-launcher/prefixes` before the archive name.
Theme configuration lives outside these directories; add
`.config/Minimalist-Game-Launcher` to the archive if you use it.

To restore, close the launcher, move the existing directories aside, and
extract the archive:

```sh
mv ~/.local/share/minigame-launcher ~/.local/share/minigame-launcher.old
mv ~/.config/minigame-launcher ~/.config/minigame-launcher.old
tar -C ~ -xzf minigame-launcher-backup.tar.gz
```

Game executables and their install directories are never part of the
launcher's data, so back those up separately if needed. The launcher stores
paths to them, so restore them to the same locations.

### Library recovery

If `library.db` is not a valid SQLite file, or SQLite reports page corruption
when opening it, the launcher:

1. Renames the damaged file, and any `-wal`/`-shm` sidecars that still exist,
   to `library.db.damaged-<UTC timestamp>`. Nothing is deleted. For a file
   with a valid header but damaged pages, SQLite may already have removed the
   `-wal` during the failed open, so that log may not be preserved.
2. Creates an empty library.
3. Shows a **Library recovered** dialog with the backup path.

To recover games, stop the launcher and copy the old file back over
`library.db` (removing the new `-wal`/`-shm` files first). Locked databases,
unreadable files, and databases with a newer schema version are **not** moved;
the launcher reports the error instead.

## Installing games

Use the install menu in the header bar:

- **From ISO image…** mounts the image read-only (no root), copies its complete
  contents into MGL staging, verifies the copy, and unmounts it.
- **From installer executable…** accepts one standalone Windows `.exe` from any
  location.

Both flows create a resumable installer session and reuse the same Wine prefix,
installer launch, executable discovery, and registration paths. Nothing is
executed until **Run installer** is pressed.

Direct executable mode checks only that the source is a regular, non-empty,
readable file with a `.exe` suffix and a Windows `MZ` signature. That check does
not establish that the file is safe, authentic, or a working installer.

- The selected executable is copied into
  `$XDG_DATA_HOME/minigame-launcher/installers/{game-id}/source/`.
- Only that file is copied. Standalone installers that need companion `.cab`,
  `.bin`, or other data files beside them are not supported; use an ISO instead.
- The original executable is never moved, modified, or deleted.
- A pre-existing staging directory is never overwritten. A failed copy removes
  only staging created by that attempt.
- MSI packages are not supported.

## Desktop launcher entry

**Settings → Application launcher** can show Minimal Game Launcher in the
application menu. The switch writes or removes only MGL's own user-level entry:

- `$XDG_DATA_HOME/applications/io.github.minimal_game_launcher.MinimalGameLauncher.desktop`
- `$XDG_DATA_HOME/icons/hicolor/scalable/apps/io.github.minimal_game_launcher.MinimalGameLauncher.svg`

`XDG_DATA_HOME` defaults to `~/.local/share`. The entry carries
`X-MGL-Managed=true`; MGL never deletes an entry without that marker. Fuzzel,
Hyprland, and `/usr/share/applications` are never modified. When a packaged
system entry with the same ID exists, disabling writes a user-level
`Hidden=true` override so the packaged entry cannot reappear. Enabling replaces
the override. No per-game desktop entries are created.

If the preference has never been set, startup does not touch the filesystem. A
failed entry update or settings save is reported without reporting success.

## Game registration

Use **Add** in the header bar. Each game needs:

- **Name**: display name.
- **Platform**: Linux (native) or Windows (Wine).
- **Executable**: an existing file. Native games must have execute permission;
  Windows games must be an `.exe`.
- **Working directory**: defaults to the executable's folder.
- **Launch arguments**: parsed with `shlex`, no shell.
- **GameMode** (on by default) and **MangoHud** (off by default).
- **Display mode**: default, fullscreen, windowed, borderless, or Wine virtual
  desktop. Only the virtual desktop is enforced; the others are hints.
- **gamescope** (off by default): render/output resolution, FPS limit, vsync,
  filter, and fullscreen.
- **Show in library** (on by default): turning it off hides the game from the
  grid and search without deleting anything. Restore it from **Hidden games**.
- **Cover** (optional): see **Covers**.

Removing a game deletes only its library record. Files on disk are untouched.

## Wine prefix behavior

- Each Windows game has its own prefix at
  `$XDG_DATA_HOME/minigame-launcher/prefixes/{game-id}/`.
- The prefix is created on the game's first launch by running
  `wineboot -u` with `WINEPREFIX` set to that directory. Creation happens off
  the GTK thread.
- Later launches reuse the prefix. It is never deleted by the launcher.
- If prefix initialization fails, the launch is reported with the error and
  the diagnostic log has details. The game is not started.
- Wine's global configuration (`~/.wine`) is never modified.

## Wine tools

The game card and details view offer **Wine tools** for Windows games:

- **Winecfg** opens the configuration window for the game's prefix.
- **Winetricks** runs the verbs you enter after a confirmation. It needs
  network access and can take several minutes.
- Only prefixes that MGL manages are accepted. External prefixes are refused,
  and the game must have been launched once so its prefix exists.
- The tools run detached with argv lists (`shell=False`); the launcher does not
  wait. Their output goes to `$XDG_STATE_HOME/minigame-launcher/wine-tools/{game-id}.log`.
- Nothing is installed automatically.

## Launch output diagnostics

For Windows games, output written while the game runs is captured up to 64 KiB
and saved to
`$XDG_STATE_HOME/minigame-launcher/launch-logs/{game-id}.log` after exit.
**Diagnose** reads that log and reports recognizable Wine problems such as
missing DLLs, permission errors, prefix failures, and graphics-driver errors.
Native game output is still discarded.

## Performance settings

- **GameMode** and **MangoHud** are set per game in the game editor.
- GameMode wraps the whole process: `gamemoderun <game>`.
- Native games with both enabled run as `gamemoderun mangohud <game>`.
  Windows games use `MANGOHUD=1` in the Wine environment instead.
- Missing `gamemoderun` or `mangohud` does not stop the game. The launcher
  shows a warning and runs the game without that wrapper.
- **gamescope** is opt-in per game. When enabled, the wrapper runs outside
  GameMode/MangoHud with the configured render/output sizes, fullscreen, FPS
  limit, vsync, and filter. The host display resolution is never changed.
- **Wine virtual desktop** runs the game through
  `wine explorer /desktop=MGL-<id8>,<width>x<height>`. The host resolution is
  not changed.
- The launcher adds no background service and no daemon. It polls running
  game processes only while at least one game is running, every 500 ms.
- Cover images are decoded only when their card is built or when a preview is
  shown. Each cover is decoded at card size. There is no in-memory image cache.

Resource use depends on the machine, library size, and cover sizes, so this
README gives no fixed figures.

## Themes

Themes are standalone and optional. The launcher reads:

```
$XDG_CONFIG_HOME/Minimalist-Game-Launcher/themes.ini
```

(normally `~/.config/Minimalist-Game-Launcher/themes.ini`). This file is
independent of `settings.json` and of any desktop theme system.

### Format

```ini
[theme]
active = obsidian-rose
appearance = system

[obsidian-rose]
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
```

- `[theme] active` names the section to use; `appearance` is `system`, `dark`,
  or `light` (the legacy key `mode` is accepted too).
- Every other section is a theme. The section name is its id; `name` is the
  label shown in Settings. Ids are matched case-insensitively.
- `theme` and `colors` are reserved section names. `[theme]` is the settings
  section, and `[colors]` alone is the older single-theme layout. The importer
  gives reserved or ambiguous desktop theme names a stable digest suffix
  (for example `colors-1a2b3c4d`) so distinct names cannot collide.
- Colors accept `#rgb` and `#rrggbb`, with `-` or `_` in key names
  (`accent-hover` and `accent_hover` are the same key).
- `background`, `surface`, `surface-elevated`, `text`, `text-muted`, and
  `border` are optional. Omitting them inherits libadwaita's light/dark
  palette, which keeps `system` and forced light/dark usable.
- `text` supplies primary foreground colors for window, view, header bar,
  popover, dialog, and card surfaces. `text-muted` supplies libadwaita's
  dimmed foreground used by descriptions and secondary labels. Omitting either
  keeps the corresponding system light/dark color.
- `accent`, `accent-hover`, `success`, `warning`, and `danger` fall back to the
  built-in palette when omitted or invalid.

### Appearance

`appearance` is independent of the palette. `system` follows the desktop's
color scheme through libadwaita's `Adw.StyleManager`; `dark` and `light` force
it. Switching appearance keeps the selected accent. No GNOME Shell dependency
is involved: on a desktop that does not report a scheme, `system` simply keeps
libadwaita's default, which is the same behavior GTK apps have there today.

### Adding a theme

Add a section and point `active` at it, or pick it in **Settings →
Appearance**. Theme and appearance choices apply and save as soon as they are
selected; the Settings button is **Close**, not Cancel, because there is
nothing to roll back. Wine settings still use **Save**.

The built-in `default` theme always exists and is the fallback for a missing
file, a malformed file, or an unknown `active` value on startup.

### Live reload

The launcher watches `themes.ini` with `Gio.FileMonitor` and reloads on
external edits and atomic replacements, debounced by 250 ms. A reload only
regenerates the CSS provider and re-applies the appearance mode: it does not
reload the database, rebuild the grid, decode covers, touch Wine, or restart
the launcher.

If the configuration directory does not exist yet, the launcher creates it and
watches for the file to appear; it never creates `themes.ini` on its own. If
the file becomes malformed, contains no theme, names a theme that does not
exist, or sets an unknown `appearance`, the last valid palette *and* appearance
stay in effect and the launcher keeps running; it recovers when the file is
valid again. A stylesheet that GTK rejects never replaces the last valid one,
and a rejected stylesheet does not change the saved selection. The monitor is
cancelled on shutdown.

### Fallback and troubleshooting

- No `themes.ini`: built-in palette, `system` appearance.
- Malformed file at startup: startup continues with the built-in palette and a
  warning in the diagnostic log. During a live reload the last valid theme is
  kept instead.
- Unknown `active`: falls back to `default` and logs a warning at startup; a
  live reload ignores the file until it is fixed.
- Unknown `appearance`: the whole reload is ignored so the last valid
  appearance stays in effect; startup falls back to `system` with a warning.
- Invalid color: that key is ignored and its default is used.
- Not writable: Settings still applies the choice for the session and shows an
  error; the file is not modified.
- Unparsable file: the writer refuses to modify it rather than appending a
  duplicate `[theme]` section, and reports the error.

Choices made in Settings are written by editing only the affected `[theme]`
lines, so comments and other themes survive. Writes are atomic (temporary file
in the same directory, then `os.replace`) and preserve:

- a symlinked `themes.ini` (the resolved target is updated, the symlink stays),
- the existing file mode,
- CRLF or LF line endings,
- a UTF-8 BOM, if present.

Line endings, BOM, and permissions are preserved; arbitrary formatting inside
the edited `[theme]` section is not rewritten byte-for-byte (a trailing comment
on the edited line is dropped, and a missing `[theme]` section is appended at
the end). A newly created file starts with a short comment header. A legacy
single-theme `theme.ini` in the same directory is read as a base layer and
never rewritten.

### Optional desktop theme integration

`scripts/mgl-theme-import` maps a desktop theme's `color.ini` onto the
semantic keys and writes one section into `themes.ini`. It is opt-in, run
manually or from your own theme-switching script:

```sh
scripts/mgl-theme-import --active
```

The launcher itself never reads `~/.config/themes/`. The helper drops the
desktop file's alpha-first `#AARRGGBB` byte so channels are not swapped, never
modifies the source `color.ini`, preserves unrelated sections and comments,
and gives reserved or ambiguous directory names a stable digest suffix so
distinct theme names never overwrite one another. Imported display names are
flattened to a single line before being written, so metadata cannot inject
sections or settings. `--active` updates both `active = value` and `active:
value` forms without leaving duplicate keys. It uses the same tested writers as
the application. A malformed existing `themes.ini` is refused with a non-zero
exit code and left byte-identical; a successful import produces a file the
launcher can parse.

## Known limitations

- **No stop-game action.** The launcher can start and monitor games, but it
  cannot terminate them. Closing the launcher stops monitoring and leaves any
  running game running.
- **Concurrent launcher instances.** Two instances running at the same time
  (the app does not use single-instance activation) can race around the
  per-game pidfile: if instance A has just spawned a game but has not yet
  written its pidfile, instance B's duplicate-launch check can miss it and
  start a second copy. The race window is milliseconds; resolving it would
  require pidfile locking (`flock`), which is deliberately deferred.
- **Keyboard navigation and live GTK interaction were not exercised** in the
  automated tests. The test suite does not drive the GTK window.
- **MangoHud for Wine** needs 32-bit Vulkan layer support, which the launcher
  cannot verify. The overlay may not appear for every game or graphics API.
- **SteamGridDB** endpoint structure, authentication, and response fields
  are checked against the official OpenAPI specification (v2.10.0) and
  unauthenticated error responses. Authenticated responses with real API
  data have not been observed: the field handling relies on the published
  schema plus defensive parsing, not on a live key. The optional
  `width`/`height` grid fields are absent from the official schema and are
  treated as unknown when missing.
- **Local cover selection** in the cover dialog runs synchronously on the GTK
  thread (bounded to 10 MiB per image).
- **Cover texture cache.** The in-memory cache is bounded to 64 decoded
  textures (roughly 45 MiB worst case) and tracks file changes by device,
  inode, size, and modification time. A cover replaced while it is being
  decoded is discarded instead of cached, so stale pixels are not served.
- **Desktop entry hint.** `desktop-file-validate` reports that
  `Categories=Game;Utility` contains more than one main category. The file
  is valid; the hint is kept because the requirement specifies both categories.
- **No packaging for other distributions.** The installer is a plain
  user-level script, not a package.
- **Theme visuals are not pixel-regression tested.** Settings was visually
  checked in dark and light modes with two temporary custom palettes, and the
  wide-cover layout is checked in GTK at normal and resized window dimensions.
  Automated tests validate generated CSS and widget allocations, but do not
  compare screenshot pixels. `system` appearance follows libadwaita; on
  desktops that do not report a color scheme it stays at libadwaita's default.
- **Theme writes preserve structure, not every byte of formatting.** Symlinks,
  file mode, CRLF/LF, and a UTF-8 BOM are preserved, and every other section
  and comment is passed through unchanged. Only the edited `[theme]` line is
  rewritten (a trailing comment on that line is dropped), and a file with no
  `[theme]` section gets one appended rather than reordered.

## License

Minimal Game Launcher is released under the MIT License. See `LICENSE`.

## Tests

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider
```

The tests use temporary databases, fake subprocesses, and a headless GLib main
loop. They do not launch real Wine or real games, access the network, or modify
system files.

Theme tests cover configuration parsing and persistence with real files, CSS
generation parsed by a real `Gtk.CssProvider`, and live reload with real
`Gio.FileMonitor` objects. Display registration
(`Gtk.StyleContext.add_provider_for_display`) is mocked so the suite runs
without a compositor; the settings dialog is constructed for real when a GDK
display is available and skipped otherwise. The importer tests run
`scripts/mgl-theme-import` as a subprocess against a temporary
`XDG_CONFIG_HOME`, so the written file is parsed by the application's own
loader.

## Library behavior

- Linux native games require an existing executable file with execute
  permission. The launcher explains missing permissions and never changes file
  permissions automatically.
- Windows games require an existing `.exe` file and use one dedicated Wine
  prefix per game.
- The working directory defaults to the executable's parent directory.
- Launch arguments are parsed with `shlex.split` and passed as an argument list.
  No shell is used.
- Cover images are validated and copied into the launcher's cover cache. The
  original files are never modified or deleted. See **Covers** below.
- GameMode and MangoHud are independent per-game toggles. New games default to
  GameMode enabled and MangoHud disabled.
- Removing a game deletes only its SQLite record. The game directory,
  executable, Wine prefix, and cover files remain on disk.

## Launching

### Native Linux games

The selected executable is launched directly with the configured working
directory. Wine is not consulted and no prefix is created. Execute permission
is checked again immediately before launch.

### Windows / Wine games

A game's prefix is created lazily on its first launch. The launcher runs
`wineboot -u` with `WINEPREFIX` set to that game's dedicated directory, then
launches the game with the same `WINEPREFIX`. Existing prefixes are reused.
Wine's global configuration is never modified.

Wine can be selected in **Settings**:

- **System Wine** resolves `wine` or `wine64` from `PATH`.
- **Custom executable** uses a file selected through the native file chooser.

Wine prefix initialization and process creation run off the GTK main thread.
Running processes are monitored with non-blocking polling from the GLib main
loop. Games receive `/dev/null` for standard output and error; `wineboot`
output is captured only for bounded diagnostic logging.

## Performance integrations

Both options are configured per game in the game editor and can be toggled
independently. Detection happens through `PATH` when the launch is prepared.

### GameMode

- Enabled by default.
- If `gamemoderun` is found, it wraps the complete game process:
  `gamemoderun <game command>`.
- If it is missing, the game still launches normally and the UI reports that
  GameMode was requested but is unavailable.
- The launcher does not modify `/etc/gamemode.ini`, CPU governors, or require
  GameMode to run.

### MangoHud

- Disabled by default.
- Native games: `gamemoderun mangohud <game>` when both are enabled. GameMode
  stays outermost so the overlay process is also covered by its profile.
- Windows games: MangoHud is activated with `MANGOHUD=1` in the Wine process
  environment rather than a wrapper, so the command remains
  `gamemoderun wine <game.exe>`.
- `WINEPREFIX` remains per game and is never replaced by performance settings.
- A missing `mangohud` executable produces a warning and disables the overlay;
  the game itself still starts.
- MangoHud for Wine additionally requires 32-bit Vulkan layer support. The
  launcher cannot verify those components, so the game editor describes that
  limitation instead of promising that the overlay will appear. MangoHud also
  does not support every graphics API or game.

Wrapper ordering, environment construction, and fallback behavior live in
`services/performance_manager.py`. No shell is used, and no background daemon
is started.

The launcher does not install DXVK, VKD3D, .NET, VC++ runtimes, GameMode,
MangoHud, or other dependencies.

## Covers

Open a game's details and choose **Change** to open the cover dialog. Covers
are never replaced automatically; a cover is applied only after you select it
and press **Apply**. **Remove** clears the cover reference.

### Local images

- Accepted formats are PNG, JPEG, and WebP, as decoded by the installed
  GdkPixbuf loaders.
- The file is validated, then copied into the cover cache when you apply it.
  The original image is only read and is never modified or deleted.
- The game editor follows the same rule: a newly chosen cover is copied when
  you save the game.
- Library-card thumbnails fill the fixed poster frame proportionally; images
  with a different aspect ratio are center-cropped, not stretched. The editor
  and cover-selection previews continue to show the full image without crop.

### Image cache

- Cached files live in `$XDG_DATA_HOME/minigame-launcher/covers/`, created
  with private permissions.
- Filenames combine the game ID with a random token and keep the detected
  format suffix, for example `{game-id}-{token}.png`. Replacing a cover never
  overwrites the previous file.
- When a cover is replaced or removed, only the previous file inside the cache
  is deleted. Legacy references to external files and any file outside the
  cache are left untouched.
- Writes go to a temporary file first and are then renamed into place.

### Decoded texture cache

Decoded cover images are kept in an in-memory least-recently-used cache so
that library refreshes, search rebuilds, and dialog previews reuse one
decoded texture instead of re-decoding the file:

- **Key**: the resolved cover path plus the requested pixel size. The same
  file at different sizes produces independent entries.
- **Limit**: at most 64 decoded textures. A card-sized texture is roughly
  0.7 MiB, which bounds the cache to well under 50 MiB. Loading a 65th image
  evicts the least-recently-used entry.
- **Invalidation**: an entry is reused only while the file's device, inode,
  size, and nanosecond modification time still match the values recorded at
  decode time. Editing, replacing, or deleting a cover file is picked up on
  the next request without any explicit refresh; replacing or clearing a cover
  in the UI also invalidates the affected paths explicitly.
- **Failure handling**: missing, unreadable, and undecodable files return
  the placeholder as before; failed loads are not cached, so a file that
  reappears or becomes readable is decoded again.
- **Release**: evicted or invalidated textures hold no cache-side reference
  and are freed as soon as no widget shows them.
- Decoding runs on the GTK main thread as before (the same thread that
  builds the cards); the cache only removes duplicate work. There is no
  background service and no new dependency.
- The preview of a downloaded SteamGridDB image is decoded straight from
  memory and is not cached.

### Validation

Every image, whether local or downloaded, must pass these checks:

- Not empty and at most 10 MiB.
- Decodable by GdkPixbuf.
- Between 16×16 and 10000×10000 pixels.
- Format is PNG, JPEG, or WebP. For downloads, the declared content type must
  match the decoded format.

### Online search (SteamGridDB, optional)

- The provider is optional. Without a key, online search is disabled and local
  images still work.
- Get a key from SteamGridDB and enter it in **Settings → Online cover
  search**. It is saved to `$XDG_CONFIG_HOME/minigame-launcher/steamgriddb.key`
  with mode `0600`, separate from `settings.json`. Clearing the field deletes
  the file.
- `STEAMGRIDDB_API_KEY` is used only when no key file exists. Keys are never
  hardcoded in the source.
- Searches run only when you press **Search** in the cover dialog. The only
  data sent is the search text. Executable paths, working directories, Wine
  prefixes, and library records are never uploaded.
- The key is sent in an `Authorization: Bearer` header to the SteamGridDB API
  only. It is never sent to image CDN URLs.
- Each search looks up up to 3 matching games, then lists up to 24 grid
  results. Nothing is downloaded until you select a result, and the image is
  downloaded only when you press **Apply**.
- The preview loads one selected result's thumbnail. It is decoded in memory
  and is not cached.

The integration targets the official SteamGridDB API v2 and was checked
against the published OpenAPI specification (v2.10.0,
`https://www.steamgriddb.com/api/v2`) in November 2026:

- **Authentication** (`Authorization: Bearer <key>` header) and the base URL
  `https://www.steamgriddb.com/api/v2` follow the official spec.
- **Endpoints**: `GET /search/autocomplete/{term}` for game lookup and
  `GET /grids/game/{gameId}` for covers, with documented filter parameters
  (`types=static`, `dimensions=...` from the official enum). Each request
  carries the `MinimalGameLauncher/<version>` user agent.
- **Response envelope**: `{ "success": true, "data": [...] }`; games expose
  `id` and `name` (the spec also lists `types` and `verified`, which the
  launcher ignores). Covers expose `url`, `thumb`, `style`, `score`, `tags`,
  and `author` in the official schema. The launcher reads `url` and `thumb`
  and treats `width`/`height` as optional extras: real responses may omit
  them, in which case the size label reads "Unknown size".
- **Errors**: the API answers `{"success": false, "errors": [...]}` with
  HTTP 401 for missing or invalid keys (live-verified: "Authentication
  Required" for no key, "Invalid key format" for a malformed key). The
  launcher maps 401/403 to an authentication error, 429 to a rate-limit
  message, and everything else to a readable HTTP-status message.
- **Pagination**: grids responses may include `page`, `total`, and `limit`
  fields. The launcher reads the first page only (the default page size is
  50) and caps what it shows at 24 results; it never pages through the rest.
- **Image handling**: cover URLs must be HTTPS. The thumbnail is used for
  the preview, the full image only after Apply. Downloads are limited to
  10 MiB, restricted to PNG/JPEG/WebP content types, and validated by
  GdkPixbuf before being cached.
- **Timeouts**: each request has a 15-second timeout; socket and timeout
  failures are reported as network problems.

### Error handling

- Missing key: the dialog explains how to add one; no network request is made.
- Offline or timeout: reported as a network problem. A search that fails
  partway through is reported as an error, not as empty results.
- Rejected key (HTTP 401/403): reported as an authentication error.
- Rate limit (HTTP 429) and other HTTP errors: reported with the status code.
- Non-JSON responses, unexpected content types, API `success: false` payloads,
  and responses over 2 MiB are rejected.
- Image responses over 10 MiB, or with an unsupported content type, are
  rejected before validation.
- Network work runs on worker threads and returns to the GTK main loop, so the
  window stays responsive. Results from a superseded search or preview are
  discarded.

Tests in `tests/test_cover_manager.py`, `tests/test_steamgriddb.py`,
`tests/test_settings.py`, and `tests/test_cover_integration.py` cover manual
selection, cache behavior, invalid files, offline and API failures, and
replacement. They use fake HTTP responses and never contact the network.
