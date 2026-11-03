# Minimal Game Launcher

A small GTK4/libadwaita game library for Linux, built with Python 3, PyGObject,
SQLite, and GdkPixbuf. It is designed for Arch Linux and Hyprland.

This repository currently implements **Phase 2: game library and game
management**. Games can be registered, edited, searched, sorted, and removed.
Wine execution and process launching are intentionally not implemented yet.

## Requirements

- `python`
- `python-gobject`
- `gtk4`
- `libadwaita`
- `gdk-pixbuf2`
- `xdg-desktop-portal` and a portal backend for native file chooser dialogs

Development tests also use `python-pytest`.

## Run from source

There is no pip/setuptools build in this phase. Run the application directly:

```sh
PYTHONPATH=src python3 -m minimal_game_launcher
```

The SQLite database and future Wine prefix paths are stored under
`$XDG_DATA_HOME/minimal-game-launcher/` (normally
`~/.local/share/minimal-game-launcher/`).

## Tests

```sh
pytest
```

The tests use temporary databases and executables. They do not launch games,
invoke Wine, access the network, or modify system files.

## Library behavior

- Linux native games require an existing executable file with execute
  permission. The launcher explains missing permissions and never changes file
  permissions automatically.
- Windows games require an existing `.exe` file. A dedicated Wine prefix path is
  reserved, but the prefix does not need to exist and is not created during
  registration.
- The working directory defaults to the executable's parent directory.
- Launch arguments are validated with shell-like quoting but are stored as text;
  no shell or process execution is used in this phase.
- Cover images are external references selected through the native file chooser.
  They are never copied or deleted by the launcher.
- Removing a game deletes only its SQLite record. The game directory,
  executable, Wine prefix, and cover files remain on disk.
