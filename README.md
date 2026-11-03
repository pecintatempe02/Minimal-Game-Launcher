# Minimal Game Launcher

A small GTK4/libadwaita game library and launcher for Linux, built with Python
3, PyGObject, SQLite, and GdkPixbuf. It is designed for Arch Linux and Hyprland.

This repository currently implements **Phase 3: Wine prefix and game
launching**. Games can be registered, edited, searched, sorted, launched, and
removed. Windows games use per-game Wine prefixes; native games run directly.

## Requirements

- `python`
- `python-gobject`
- `gtk4`
- `libadwaita`
- `gdk-pixbuf2`
- `xdg-desktop-portal` and a portal backend for native file chooser dialogs
- `wine` for Windows games, unless a custom Wine executable is configured

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
old files are left untouched.

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

The launcher does not install DXVK, VKD3D, .NET, VC++ runtimes, or other
dependencies. GameMode and MangoHud integration are not implemented.
