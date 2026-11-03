# Minimal Game Launcher

A small GTK4/libadwaita game library and launcher for Linux, built with Python
3, PyGObject, SQLite, and GdkPixbuf. It is designed for Arch Linux and Hyprland.

This repository currently implements **Phase 4: performance integration**.
Games can be registered, edited, searched, sorted, launched, and removed.
Windows games use per-game Wine prefixes; native games run directly. Optional
GameMode and MangoHud wrappers are configured independently per game.

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
- Cover images are external references selected through the native file chooser.
  They are never copied or deleted by the launcher.
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
