# Minimal Game Launcher — Packaging & AppImage Documentation

## Build-time requirements

- Python 3.11+ (only needed to verify source compatibility)
- `sh` / POSIX shell
- `cp`, `mkdir`, `find`, `rm`
- `python3` (host installation; not bundled unless a base runtime is provided)
- For `.AppImage`: `appimagetool` + `mksquashfs` + an AppImage runtime binary

## Runtime requirements (host or bundled)

The launcher requires a GTK4 + libadwaita + PyGObject runtime. The build framework does **not** bundle these libraries by default; it relies on a host-provided Python/GTK environment or a pre-existing AppImage runtime base.

To create a fully self-contained `.AppImage`, provide:
- A Python 3 + `python-gobject` + `gtk4` + `libadwaita` + `gdk-pixbuf2` runtime
- `.typelib` files for `Gtk-4.0`, `Adw-1`, `GdkPixbuf-2.0`, `Gio-2.0`, `GLib-2.0`, `GObject-2.0`
- GTK schemas (`glib-2.0/schemas/`)
- System TLS CA certificates (for SteamGridDB)

## Build commands

```sh
# Stage the AppDir (always works)
./build.sh

# The AppDir is created at:
# build/appdir/

# The final packaging step produces:
# dist/Minimal-Game-Launcher.AppImage
```

## Blocker status

As of Step 6 (2026-11-05), `appimagetool`, `mksquashfs`, and an AppImage runtime
are available on the build host. `./build.sh` produces both `build/appdir/` and
`dist/Minimal-Game-Launcher.AppImage` (about 1.1 MB with the host GTK runtime).
The output is verified only on this host; cross-distribution portability is not
claimed.

## Relocatable desktop entry

`scripts/build-appdir.sh` installs one canonical desktop entry,
`io.github.minimal_game_launcher.MinimalGameLauncher.desktop`, with
`Exec=minimal-game-launcher`, and keeps `Exec=AppRun` only on the AppDir root
entry. Earlier versions embedded the absolute build path, which cannot resolve
inside a relocated AppImage or an installed AppDir, and also staged a second
basename entry. `tests/test_packaging.py` guards the single ID, the ownership
marker, and relocation. The staging directory is overridable with `MGL_APPDIR`
for tests.

The packaging template carries `X-MGL-Managed=true`. The runtime desktop
service uses the same marker to decide whether a user-level entry is MGL-owned.
An AppImage launched directly is detected through `APPIMAGE`, so runtime
enablement points `Exec=` at the `.AppImage` file rather than its internal
`AppRun`. When MGL is disabled while a packaged entry is installed, the
user-level file becomes a `Hidden=true` override; the packaged file is never
modified.

## What the AppDir includes

- `usr/lib/minimal-game-launcher/minimal_game_launcher/` — complete Python package
- `usr/share/applications/` — desktop entry
- `usr/share/icons/hicolor/scalable/apps/` — application icon
- `usr/bin/minimal-game-launcher` — launcher wrapper
- `AppRun` — entry point

## What is NOT bundled (host-provided by design)

- Wine
- GPU drivers / DXVK / VKD3D
- Game binaries or game files
- Wine prefixes (`XDG_DATA_HOME/minigame-launcher/`)
- User settings, API keys, database, covers (`XDG_DATA_HOME`, `XDG_CONFIG_HOME`, `XDG_STATE_HOME`)
- GameMode / MangoHud executables (detected via host `PATH`)

## Runtime environment

- The wrapper script preserves host `PYTHONPATH`
- `APPDIR` is set by the AppImage loader; `AppRun` uses it to resolve internal paths
- `XDG_DATA_HOME`, `XDG_CONFIG_HOME`, `XDG_STATE_HOME` are never redirected into the AppImage; they remain host/user paths
- No `LD_PRELOAD`, `LD_LIBRARY_PATH`, or `LD_PRELOAD` overrides are applied globally
- `PYTHONHOME` is preserved from host or defaults to `/usr`

## Icon portability fix

`src/minimal_game_launcher/application.py` was changed to try the host theme icon first (`APP_ID`), then fall back to a package-relative SVG (`src/minimal_game_launcher/ui/io.github...svg`). This ensures the icon appears inside portable builds where the host theme does not have the application registered.

## Testing strategy

Steps 1–7 below were executed in Step 6 against the rebuilt AppDir/AppImage:

1. Inspect `build/appdir/` structure
2. Verify no user data paths are included (`find build/appdir | grep -E 'home|\.local'` should return nothing)
3. Verify the canonical desktop entry: installed copy uses `Exec=minimal-game-launcher`, the AppDir root uses `Exec=AppRun`, and both carry `X-MGL-Managed=true`
4. Test with isolated `HOME` and `XDG_*` variables
5. Verify `python3 -c "import minimal_game_launcher"` works when `PYTHONPATH` points to `build/appdir/usr/lib/minimal-game-launcher`
6. Verify `style.css`, icon, and desktop entry are present
7. Confirm `build.sh` exits cleanly and produces `dist/Minimal-Game-Launcher.AppImage`

An AppImage runtime smoke under an isolated `HOME`/`XDG_*` with
`APPIMAGE_EXTRACT_AND_RUN=1` stayed alive, created only an isolated
`library.db` and `launcher.log`, and logged no GTK criticals.

## Cross-distribution compatibility notes

- The AppDir relies on host GTK4/libadwaita libraries unless a base runtime is provided
- Font rendering, wayland/x11 integration, and GPU access depend on the host environment
- The build framework does not force a bundled graphics stack; this avoids library conflicts and preserves native theme integration
- If a fully portable binary is needed, use a base runtime image (e.g., `openSUSE` Python3 + GTK4 AppImage) and stage MGL on top
