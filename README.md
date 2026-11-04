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
  state. Search (debounced) and sorting by name or recently added.
- **Details**: a read-only view of every stored field, with launch, cover,
  edit, and remove actions.
- **Launching**: runs off the GTK thread. Running games are tracked by a
  single main-loop poll that stops when no game is active.
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
| `python-pytest` | Test suite | development only |

The launcher does not install Wine, DXVK, VKD3D, .NET, VC++ runtimes,
GameMode, or MangoHud. Missing optional tools produce a warning, and the game
still starts.

## Install on Arch Linux

The installer is user-level. It writes only under `~/.local` and needs no root.

```sh
git clone <repository-url> minimalist-game-launcher
cd minimalist-game-launcher
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
- Diagnostic log: `$XDG_STATE_HOME/minigame-launcher/launcher.log`.

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

## Game registration

Use **Add** in the header bar. Each game needs:

- **Name**: display name.
- **Platform**: Linux (native) or Windows (Wine).
- **Executable**: an existing file. Native games must have execute permission;
  Windows games must be an `.exe`.
- **Working directory**: defaults to the executable's folder.
- **Launch arguments**: parsed with `shlex`, no shell.
- **GameMode** (on by default) and **MangoHud** (off by default).
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

## Performance settings

- **GameMode** and **MangoHud** are set per game in the game editor.
- GameMode wraps the whole process: `gamemoderun <game>`.
- Native games with both enabled run as `gamemoderun mangohud <game>`.
  Windows games use `MANGOHUD=1` in the Wine environment instead.
- Missing `gamemoderun` or `mangohud` does not stop the game. The launcher
  shows a warning and runs the game without that wrapper.
- The launcher adds no background service and no daemon. It polls running
  game processes only while at least one game is running, every 500 ms.
- Cover images are decoded only when their card is built or when a preview is
  shown. Each cover is decoded at card size. There is no in-memory image cache.

Resource use depends on the machine, library size, and cover sizes, so this
README gives no fixed figures.

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
- **SteamGridDB** response field names were checked against fake responses
  only. No live API call has been verified.
- **Local cover selection** in the cover dialog runs synchronously on the GTK
  thread (bounded to 10 MiB per image).
- **Desktop entry hint.** `desktop-file-validate` reports that
  `Categories=Game;Utility` contains more than one main category. The file
  is valid; the hint is kept because the requirement specifies both categories.
- **No packaging for other distributions.** The installer is a plain
  user-level script, not a package.

## Tests

```sh
pytest
```

The tests use temporary databases, fake subprocesses, and a headless GLib main
loop. They do not launch real Wine or real games, access the network, or modify
system files.

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
