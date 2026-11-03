# Minimal Game Launcher

A small GTK4/libadwaita game library and launcher for Linux, built with Python
3, PyGObject, SQLite, and GdkPixbuf. It is designed for Arch Linux and Hyprland.

This repository currently implements **Phase 5: cover management**.
Games can be registered, edited, searched, sorted, launched, and removed.
Windows games use per-game Wine prefixes; native games run directly. Optional
GameMode and MangoHud wrappers are configured independently per game. Covers
can be selected from local images or, optionally, found through SteamGridDB.

## Requirements

- `python`
- `python-gobject`
- `gtk4`
- `libadwaita`
- `gdk-pixbuf2`
- `xdg-desktop-portal` and a portal backend for native file chooser dialogs
- `wine` for Windows games, unless a custom Wine executable is configured
- `gamemoderun` for optional GameMode integration
- `mangohud` for optional MangoHud integration

Development tests also use `python-pytest`.

## Run from source

There is no pip/setuptools build in this phase. Run the application directly:

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
