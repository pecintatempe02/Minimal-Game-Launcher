# HANDOFF — Minimal Game Launcher

**Last updated:** 2026-11-07
**Current phase:** v0.6.0 feature set implemented and tested; uncommitted.
**Implementation status:** Steps 1–12 implemented. Steps 1–11 are committed at
HEAD `072ee85`; v0.5.0 release preparation and the v0.6.0 feature set are
uncommitted.

**Current status (verified 2026-11-07):**
- Database schema **v9**, with each migration atomic (one `BEGIN IMMEDIATE`
  transaction per version, including `user_version`). v8 added
  `games.gamescope_resolution`; v9 adds `show_in_library`, `display_mode`,
  `virtual_desktop_resolution`, and the full gamescope control set
  (`gamescope_enabled`, output resolution, FPS limit, vsync, filter,
  fullscreen). v9 defaults preserve pre-0.6.0 behavior.
- Per-game library visibility: hidden games stay in the database and are
  excluded from normal listing/search but remain reachable through the hidden
  games manager. Cleanup and orphan detection still see every record.
- Wine runtime diagnostics: Windows launch output is captured to a bounded
  64 KiB buffer, persisted to `<state>/launch-logs/<game-id>.log`, and analyzed
  on demand for 12 recognizable Wine error patterns.
- Per-game Winecfg and Winetricks: explicit, on-demand actions that verify the
  prefix through `OwnershipService`, refuse external prefixes, validate
  Winetricks verbs, use argv lists only, and detach without waiting.
- Per-game display configuration: display mode (`default`, `fullscreen`,
  `windowed`, `borderless`, `virtual_desktop`), Wine virtual desktop through
  `wine explorer /desktop=MGL-<id8>,WxH`, and the full gamescope control set.
  Explicit gamescope is a hard launch requirement when missing.
- First-run setup wizard: four informational/preference steps, skippable, and
  persisted through `settings.json` as `setup_completed`. It installs nothing
  and creates no prefix.
- No new daemon, polling loop, startup scan, or automatic dependency install.
  Tool detection is a single `PATH` lookup only when a page or dialog is shown.
- Latest verified test count: **645 passed**, 52 warnings in 62.48 s (full
  suite, no skips), 2026-11-07.
- GTK smoke test: the real application starts under `dbus-run-session` with
  isolated `XDG_*` directories, opens `MainWindow`, shows `SetupWizard` on
  first run, and exposes the Wine tools and hidden-games paths without a
  traceback.
- Remaining limitations: see `docs/IMPLEMENTATION-LOG.md` Step 12 and §6
  "Remaining Issues".

This file is the entry point for a new development session. Facts marked
**Verified** were checked against the current repository and runtime. Historical
notes are explicitly labelled and are not claims about the current working tree.
See `docs/FEATURE-ROADMAP.md` for the durable product specification and
`docs/IMPLEMENTATION-LOG.md` for the chronological record.

---

## 1. Project identity

**Verified**

- Name: **Minimal Game Launcher** (MGL)
- Application ID: `io.github.minimal_game_launcher.MinimalGameLauncher`
- Version: **0.5.0** (`src/minimal_game_launcher/__init__.py`)
- Runtime: Python 3.14.9, PyGObject 3.58.1, GTK 4.24.1, libadwaita 1.10.0,
  SQLite 3.54.1, GdkPixbuf
- Target environment: Arch Linux + Hyprland, though the app is a normal
  GTK4/libadwaita desktop application
- No `pyproject.toml` or `setup.py`. Tests use `pytest.ini` with
  `pythonpath = src`. Installation is a user-level source copy via
  `scripts/install.sh`.

---

## 2. Current repository state

**Verified on 2026-11-07**

- Branch: `main`
- HEAD: `072ee85` (`Preparing for Releases`)
- Staged changes: none at baseline
- Stashes: none
- Tags: none
- Working tree: clean at baseline; Steps 1–11 are committed in `d58ab02` and
  `3c50f17`
- v0.5.0 release preparation leaves uncommitted edits to `build.sh`,
  `.gitignore`, `README.md`, packaging, tests, and documentation. The local
  `dist/Minimal-Game-Launcher.AppImage` is preserved but removed from Git
  tracking; that index removal is the only staged change.

Do not commit, tag, or push without explicit user approval.

### Latest verified test result

**Verified 2026-11-06**

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests
```

Result after Step 11: **604 passed, 51 warnings in 62.09s**

Fresh release-preparation run on 2026-11-06: **604 passed, 51 warnings in
62.71s**, no skips.

The 51 warnings are the unchanged GTK/PyGObject deprecations and GTK allocation
warnings. No test failures were observed, and no warning was suppressed. The
suite uses temporary databases, fake subprocesses, injected host probes, and
fake HTTP responses; it does not launch real Wine, real games, real Vulkan
probes, or touch the user's application data.

---

## 3. Implementation status

| Area | Status | Notes |
|---|---|---|
| Library CRUD, search, sort | Completed | Phases 1–2; committed in later history |
| Native and Wine launching | Completed | Phase 3; `LaunchManager` |
| Per-game Wine prefixes | Completed | Lazy `wineboot -u`, stable `<prefixes>/<game-id>` paths |
| GameMode / MangoHud | Completed | Phase 4; optional wrappers and warnings |
| Cover manager and SteamGridDB | Completed | Phase 5 and Phase 6.1 verification |
| Database recovery | Completed | Phase 6; damaged DB set aside, never deleted |
| Desktop entry and user install | Completed | Phase 6 |
| Process reattachment and safe shutdown | Completed | Phase 6.1; pidfile identity, no signaling |
| Theme system | Completed | Phase 7 and follow-ups committed |
| Command / external app launching | Completed (committed) | Step 1; existing Linux path, schema v3 |
| On-demand diagnostics | Completed (committed) | Step 2; read-only service, dialog, text export |
| Ownership / cleanup foundation | Completed (committed) | Step 3; classification, size estimation, safe deletion, schema v4 |
| ISO game installer | Completed (committed) | Step 4; read-only mount, full staging/verification, discovery, defer/resume, schema v5 |
| Hybrid ISO mount compatibility | **Completed (committed)** | Step 7; `--no-partition-scan` lets UDisks mount a hybrid ISO9660+APM whole disk |
| Executable discovery and registration | **Completed (committed)** | Step 8; read-only ranked candidates, explicit destination types, no unused `games/<game-id>` directory |
| Pending-install removal | **Completed (committed)** | Step 9; confirmed removal of staging, prefix, and owned destination, result popup, external folders kept |
| Cleanup UI / orphan detection | Completed (committed) | Step 5; verified managed-data deletion, on-demand orphan scan |
| Final integration and testing | Completed (committed) | Step 6; full suite, schema matrix, integration, GTK smoke, real-ISO content, packaging fix |
| Memory management | Completed (committed) | Step 11; signal ownership, card reuse, byte-budgeted cover cache, pre-decode image limits |

The previous feature audit found no ISO, diagnostics, cleanup, or ownership
implementation in the current source. Step 2 adds diagnostics; Step 3 adds the
ownership foundation; Step 4 adds the ISO installer; Step 5 adds manual cleanup
and on-demand orphan detection. Automatic deletion and background scanning
remain unimplemented by design.

---

## 4. Architecture snapshot

**Verified**

### Layering rules

- All SQL lives in `src/minimal_game_launcher/database.py`.
- Game/prefix/launch logic lives in `services/game_manager.py` and
  `services/launch_manager.py`; new services must not move SQL into the UI.
- All GTK widgets live in `ui/`.
- Every module has a docstring.
- No `shell=True` anywhere.
- No root privilege is required.
- Per-game Wine prefixes live under the app data directory; global Wine
  configuration is never modified.

### Composition root

`src/minimal_game_launcher/application.py`:

- `do_startup()` configures logging, loads icon/CSS, starts the theme
  controller, opens the library, and constructs all services.
- `do_activate()` creates `MainWindow`.
- `do_shutdown()` shuts down theme and launch monitoring, closes the DB, and
  deliberately leaves running games running.

### Database

`database.py`, schema **v7**, with the existing `games` table plus
`installer_sessions`.

`games` columns: `id`, `name`, `platform`, `executable`, `working_directory`,
`launch_arguments`, `cover_path`, `wine_prefix_path`, `gamemode_enabled`,
`mangohud_enabled`, `environment`, `install_directory`,
`install_directory_owned`, `installer_source_directory`,
`installer_source_owned`, `created_at`, `updated_at`.

- `platform` is constrained to `linux` or `windows`.
- `environment` is JSON text and defaults to `{}`.
- The ownership paths are nullable; both owned flags default to `0`, so an
  existing record never becomes launcher-owned implicitly.
- `open_library()` sets aside damaged databases rather than deleting them.
- Migrations run every missing step in one transaction and stamp
  `SCHEMA_VERSION` afterwards.
- `installer_sessions` is keyed by the stable game ID and stores the game name,
  ISO path, staging directory, selected installer, destination and its owned
  flag, status, message, and timestamps. Statuses are `pending`, `staged`,
  `installing`, `installer_finished`, `deferred`, `installed`, and `failed`.
  Existing `games` rows are unchanged by the v4→v5 migration.

### Game registration

`services/game_manager.py`:

- `GameDraft` → `_normalize_draft()` → `Game` → `Database.add_game()`.
- Native Linux entries require either an existing executable with execute
  permission or a bare command resolvable through `shutil.which`.
- Bare command values are stored as typed; absolute/relative paths keep the
  resolved-absolute storage behavior.
- Windows entries require an existing `.exe`.
- Working directory defaults to the executable's parent; a bare command keeps
  an empty working directory and starts in the user's home directory.
- Launch arguments are validated with `shlex.split`.
- Environment lines are parsed and validated by `GameManager.parse_environment`;
  `WINEPREFIX` and `MANGOHUD` are reserved.
- `new_game_id()` returns a UUID4 string.
- `add_game(..., game_id=...)` can reserve an ID.
- Windows games receive `wine_prefix_path = prefixes_root / game_id`.
- `remove_game()` deletes only the database row. It never touches game files,
  prefixes, covers, or external paths.

### Launch lifecycle

`services/launch_manager.py`:

- `launch()` validates and spawns on a daemon worker thread.
- `Popen` receives an argument list, `cwd`, `env`, `shell=False`, and
  `stdin`/`stdout`/`stderr = `DEVNULL`.
- Linux bare commands are resolved with `shutil.which` at launch time; missing
  commands produce a readable error.
- Child environment order is inherited → per-game → platform-managed →
  performance-managed. Environment values are never logged.
- Signals: `launch-started`, `launch-finished`, `launch-failed`,
  `launch-warning`.
- One `GLib.timeout_add` poll (default 500 ms) runs only while a game is active.
- Pidfiles in `$XDG_STATE_HOME/minigame-launcher/processes/<game-id>.json`
  support read-only reattachment after a launcher restart.
- The launcher never signals or reaps a running game; there is intentionally no
  stop-game feature.
- Game stdout/stderr are discarded. `wineboot` output is captured for bounded
  diagnostic logging.
- Exit codes are delivered to the UI but not persisted.

### Wine

- System Wine resolves `wine`, then `wine64`, from `PATH`.
- Custom Wine is validated and stored in `settings.json`.
- `ensure_prefix()` creates `prefixes/<game-id>` with mode `0700` and runs
  `wineboot -u` on first use.
- Initialization is recognized by `system.reg` or the
  `.minigame-launcher-prefix` marker.
- Each process gets its own `WINEPREFIX`; `~/.wine` is never modified.

### Ownership foundation

`services/ownership.py`:

- `OwnershipService(prefixes_root, games_root, installers_root)` classifies,
  measures, and deletes resources inside those configured roots.
- A resource is managed only when its resolved path equals
  `<root>/<game-id>`; names and home-directory location prove nothing.
- `classify()` returns `managed`, `external`, `missing`, or `invalid` with a
  reason. Unsafe game IDs, `..` segments, symlinks, roots, mismatched game IDs,
  non-directories, and unreadable paths are `invalid`.
- `estimate()` requires a managed resource, walks with
  `O_NOFOLLOW`/`dir_fd`, never follows symlinks, counts hardlinks once, and
  returns `complete=False` with errors for partial traversal.
- `delete()` requires `authorized=True`, refuses a running game, revalidates
  ownership, uses `O_NOFOLLOW|O_DIRECTORY`, deletes through `dir_fd`, and
  re-checks `(st_dev, st_ino)` before `rmdir`. It reports partial failures and
  never widens to a broader recursive delete.
- It is not wired to a general cleanup UI. Step 4 uses `classify()` for staging
  and destinations and `delete(authorized=True)` only to remove a staging
  directory created by a failed copy operation. Step 5 is the only user-facing
  caller of `delete()`.
- Documented limitation: a concurrent path swap cannot be fully serialised; the
  implementation narrows the window and records an error instead of claiming
  complete race protection.

### Cleanup and orphan detection

`services/cleanup.py` and `ui/cleanup_dialog.py`:

- `CleanupService(database, ownership, is_game_active=...)` owns no GTK code and
  performs no automatic cleanup. Background scanning does not exist; every scan
  and every deletion follows an explicit user action.
- Removal keeps metadata-only as the safe default: the library-record switch is
  on, every filesystem switch is off, and only explicitly selected targets are
  deleted.
- Targets come from each game's stored `install_directory`, `wine_prefix_path`,
  and `installer_source_directory`. External, missing, and invalid paths are
  listed with a reason and cannot be selected.
- `delete_target()` raises without `authorized=True`, then revalidates ownership
  through `OwnershipService`, refuses a symlinked or non-directory managed root,
  compares the `(st_dev, st_ino)` identity captured at preview, refuses a running
  or starting game, refuses a protecting installer session, and refuses a
  changed metadata reference. Only then does it delegate to
  `OwnershipService.delete`.
- `pending`, `staged`, `installing`, `installer_finished`, and `deferred`
  sessions protect their staging directory, owned destination, and managed
  prefix. Terminal `installed` and `failed` sessions do not.
- The library record is removed only after all selected targets were fully
  deleted; partial failure keeps the record.
- `scan_orphans()` inspects direct children of the three managed roots with
  `os.scandir`, never follows symlinks, never creates a root, is bounded by
  `max_scan_entries`, and returns candidates plus non-removable issues.
- Covers, state pidfiles, and the original ISO are deliberately neither scanned
  nor deleted.
- `CleanupDialog` replaces the previous direct removal; `OrphanCleanupDialog`
  is opened from the main-window header action; `CleanupResultDialog` shows
  removed, skipped, and failed targets plus remaining size.

### ISO installer

`services/iso_mount.py` and `services/installer_manager.py`:

- Mounting uses `udisksctl loop-setup --read-only --no-partition-scan` then
  `udisksctl mount --options ro`, with `--no-user-interaction`. No root or
  privilege escalation. The partition-scan flag is required for hybrid images:
  UDisks otherwise parses the Apple Partition Map or GPT, creates child
  partition objects, and refuses the whole-disk ISO9660 filesystem.
- After mounting, the service verifies `ro=1` on the loop device and exactly one
  matching entry in `/proc/self/mountinfo`. Cleanup releases only the mount and
  loop device recorded by the same operation.
- The complete ISO tree is copied to `<installers>/<game-id>/source/` and hashed
  into `.mgl-staging.json`; verification runs before unmounting.
- Discovery is read-only and never executes or auto-selects anything.
- Destination types are explicit: the installer's Wine-prefix default (creates
  no game directory and stores no installation path), the managed
  `<games>/<game-id>` directory (owned, created only when selected), or an
  external folder (non-owned, never deleted, refused when non-empty).
- `services/executable_discovery.py` scans read-only after installation in this
  order: configured installation directory, `drive_c/Program Files`,
  `drive_c/Program Files (x86)`, then other non-system `drive_c` children. It
  filters installers/redistributables/updaters/crash reporters/helpers, ranks
  deterministically by location/depth/name match, never follows symlinks, and
  bounds depth, entries, and candidates. It never executes a candidate.
- The finish page shows candidates with relative paths and locations, marks a
  recommended candidate only when unambiguous, requires explicit selection,
  keeps manual selection, and allows closing/registering later without changing
  launch configuration. Registration stores the executable and its parent as
  the working directory through the existing `GameManager`; existing arguments,
  environment, cover, GameMode, MangoHud, and Wine prefix are preserved.
- The selected installer runs through `LaunchManager` with the reserved stable
  game ID and `<prefixes>/<game-id>`. The prefix is created lazily by the launch
  path; DXVK, Winetricks, and runtimes are not installed automatically.
- Sessions persist in `installer_sessions`, so a deferred install survives a
  restart. Resume re-verifies the manifest without mounting or re-copying.
- `remove_pending_install()` deletes a pending install's managed resources:
  installer staging, the Wine prefix, and the managed destination when it is
  owned. Each resource is revalidated through `OwnershipService` before
  deletion. An external destination is retained and reported. An already
  missing resource counts as success, so the session row is still removed; only
  a real failure keeps the row for a retry. A running installer is refused.
- `ui/installer_dialog.py` provides the install/resume dialog and the
  pending-installs dialog (resume, remove with confirmation, and a result
  popup); entry points are in `ui/main_window.py`.
- Multi-disc installers are not supported (one ISO per session). UDF-only images
  are unverified on this host; there is no extraction fallback.

### Diagnostics

- `services/diagnostics.py` generates a report only when the user requests it.
- It is separate from the launch path: no daemon, timer, automatic repair,
  dependency installation, or game execution.
- Checks read Wine settings and `PATH`, prefix files, DXVK DLLs and `user.reg`
  override hints, Vulkan via library lookup or `vulkaninfo --summary`, the
  configured executable and working directory, pidfile process state, log
  metadata, and free space.
- Managed prefixes are `<prefixes-root>/<game-id>`, classified through the
  shared `OwnershipService`; everything else is external or invalid and is never
  modified. Symlinked prefixes are `invalid`, not managed.
- Subprocess probes use argv lists, `shell=False`, captured output, and bounded
  timeouts.
- Reports omit environment values, settings contents, API keys, registry dumps,
  and launch arguments. Home paths become `~` by default; the dialog has an
  explicit full-paths switch.
- Exit codes are not persisted, so diagnostics report current pidfile state,
  not a historical exit status. Launcher-log contents are excluded because they
  may contain launch arguments.
- `ui/diagnostics_dialog.py` shows summaries with collapsible technical details
  and exports plain text to a user-selected local file.
- Entry points: game card menu and game details dialog.

### Performance

`services/performance_manager.py` composes optional wrappers:

- Native: `gamemoderun mangohud <game>` when enabled.
- Windows: `gamemoderun wine <game.exe>`; MangoHud uses `MANGOHUD=1`.
- Missing tools produce warnings, not launch failures.
- Defaults: GameMode on, MangoHud off.

### Covers and themes

- Covers are validated and copied into the app-owned cache under
  `covers/`; user source images are never modified or deleted.
- Only cache-contained covers can be deleted.
- Themes live in `$XDG_CONFIG_HOME/Minimalist-Game-Launcher/themes.ini`;
  the Phase 7 live-reload and appearance behavior is committed.
- Decoded cover textures use a byte-budgeted LRU cache (64 MiB, secondary
  128-entry cap) keyed by resolved path and requested size. Entries record the
  file's device/inode/size/mtime and are invalidated when it changes; dead
  entries are pruned on library refresh.
- Image headers are checked before decode: 10 MiB file, 16 px minimum,
  10 000 px per side, 12 MP total. Oversized covers render as the placeholder
  instead of allocating their pixel buffer.
- Cards and dialogs own their child-widget handlers through
  `ui/lifecycle.py` (`SignalOwner`, `DialogSignalOwner`). Disposing a card or
  closing a dialog disconnects them and drops transient references, which is
  what lets PyGObject collect the object. Unchanged library refreshes reuse
  existing cards, so they perform no decodes.

### Data locations

```text
$XDG_DATA_HOME/minigame-launcher/library.db
$XDG_DATA_HOME/minigame-launcher/prefixes/<game-id>/
$XDG_DATA_HOME/minigame-launcher/games/<game-id>/          (created on use)
$XDG_DATA_HOME/minigame-launcher/installers/<game-id>/     (created on use)
$XDG_DATA_HOME/minigame-launcher/covers/
$XDG_CONFIG_HOME/minigame-launcher/settings.json
$XDG_CONFIG_HOME/minigame-launcher/steamgriddb.key      (mode 0600)
$XDG_CONFIG_HOME/Minimalist-Game-Launcher/themes.ini
$XDG_STATE_HOME/minigame-launcher/launcher.log           (rotating)
$XDG_STATE_HOME/minigame-launcher/processes/<game-id>.json
```

`installers/` is created only when an ISO installation reaches staging. The
managed `games/<game-id>` destination is created only when the user explicitly
selects it; a Wine-prefix default install creates no game directory and records
`install_directory = NULL`. Existing prefixes, covers, database, and state were
not moved.

---

## 5. Current development phase

**Step 12 — v0.6.0 Feature Set** is complete but uncommitted. It adds the
features below without adding a service, daemon, or automatic installer.

What Step 12 did:

- Schema v9: added `show_in_library`, `display_mode`,
  `virtual_desktop_resolution`, `gamescope_enabled`,
  `gamescope_output_resolution`, `gamescope_fps_limit`, `gamescope_vsync`,
  `gamescope_filter`, and `gamescope_fullscreen` to `games`. The v9 migration
  is transactional and refuses newer schemas. `Game.gamescope_active` keeps
  the legacy rule (`gamescope_enabled or bool(gamescope_resolution)`).
- Library visibility: `GameManager.list_games()` excludes hidden records by
  default; `list_hidden_games()` and `set_library_visibility()` back the
  hidden-games manager. Cleanup and orphan scans still call the unfiltered
  path, so hidden games are never treated as orphaned.
- Display and gamescope: `DisplayMode` validation, Windows-only virtual
  desktop with a required `WxH`, and the full gamescope argv
  (`-w/-h`, `-W/-H`, `-f`, `--fps-limit`, `--vsync-enabled`, `-F`, `--`).
  An explicit gamescope request now raises `LaunchError` when the binary is
  missing instead of silently dropping the overlay.
- Wine runtime diagnostics: Windows launches pipe combined output through
  `_BoundedCapture` (64 KiB, daemon reader), write
  `<state>/launch-logs/<game-id>.log` with an `exit_status=` header, and
  analyze the text with 12 patterns in `services/wine_output.py`. Diagnostics
  exposes a new "Last launch output" check.
- Wine tools: `services/wine_tools.py` resolves Wine/Winetricks on demand,
  verifies the prefix with `OwnershipService`, refuses external and
  uninitialized prefixes, validates verbs, and detaches with argv lists and
  per-game logs. The UI adds a Windows-only "Wine tools" action on cards and
  the details dialog.
- First-run wizard: `ui/setup_wizard.py` explains the app, detects optional
  tools once, records the prefix strategy and desktop-entry preference, and
  persists `setup_completed`. Skip also marks it complete. `SettingsDialog`
  now preserves the flag instead of resetting it.
- Verification: **645 passed**, 52 warnings in 62.48 s; a real GTK smoke run
  under `dbus-run-session` with isolated `XDG_*` directories opened
  `MainWindow`, showed `SetupWizard`, and exposed the new dialogs without a
  traceback. No real Wine, Winetricks, gamescope, or installer was run.
- No version bump, no commit, no tag, and no Git mutation.

**Step 11 — Memory Management Fixes** is complete but uncommitted. It follows
Step 10 and addresses four measured memory problems.

What Step 11 did:

- Added `ui/lifecycle.py` with `SignalOwner` (records and disconnects owned
  child-widget handlers) and `DialogSignalOwner` (releases handlers and
  transient references when the dialog emits `closed`). This breaks the
  `dialog/card -> child widget -> bound method -> dialog/card` cycle that
  PyGObject cannot collect.
- `GameCard` gained an idempotent `dispose()`; `MainWindow` disposes old cards
  before removing them and reuses cards when the game list is unchanged.
  Guarded every `_active_*` clear with `is dialog` so an older dialog cannot
  clear a newer one.
- Rewrote the cover texture cache as a byte-budgeted LRU: 64 MiB default,
  128-entry secondary cap, file-identity validation, `prune_dead()`, and
  `prunes`/`entries`/`bytes_cached` stats.
- Cover validation now reads the image header through a `memfd` before any
  decode and rejects images over 12 MP or 10 000 px per side. The card texture
  cache applies the same guard, so legacy oversized covers cannot spike
  memory.
- Added `tests/test_lifecycle.py` and extended the cover cache and cover
  manager tests. Full suite: **604 passed**, 51 warnings in 62.09 s.
- Measured controlled A/B: 6 × 120 card rebuilds retained 720 cards before and
  0 after; a 7000×7000 cover peaked at 203.1 MiB before and is rejected in
  0.135 s / +5.3 MiB after; dialog batches retain zero instances.
- No new dependency, no schema change, no version bump, no Git mutation.

**Step 9 — Pending-Installation Removal** was completed in this uncommitted
series. It follows Step 8 and adds the missing way to abandon a pending ISO
install.

What Step 9 did:

- Added a destructive `Remove` action to each session in the pending-installs
  dialog, next to `Resume`. Its confirmation lists the installer staging
  directory, the Wine prefix, the managed destination when owned, any external
  folder that will be kept, and notes that the original ISO is kept. It offers
  two choices: **Remove session only** (files stay on disk) and **Remove files
  and session** (destructive).
- Added `InstallerManager.session_resources()` and
  `InstallerManager.remove_pending_install(session, authorized=True,
  game_running=False, delete_files=True)`. With `delete_files=False` only the
  session row is deleted and every managed file stays on disk. Otherwise each
  resource is revalidated through `OwnershipService`
  immediately before deletion. Only verified managed directories are deleted:
  staging, the prefix, and the managed destination when
  `install_directory_owned` is true. A running installer is refused and
  authorization is mandatory.
- External destinations are reported as retained and never deleted. A
  Wine-default install has no destination to delete.
- An already missing resource counts as success, so the session row is still
  removed; only a real failure keeps it for a retry.
- Removal runs on a worker thread; the session row is deleted on the GTK main
  thread because the SQLite connection is thread-bound. Deleting it from the
  worker raised `sqlite3.ProgrammingError` and made every removal report that
  the record was kept. The worker calls
  `remove_pending_install(..., delete_session=False)`, then
  `discard_pending_session()` runs on the main thread.
- The result popup reports `Deleted`, `Already missing`,
  `Kept (external)`, and `Failed` resources plus whether the session record was
  removed in a result popup. A session-only removal reports that every staged
  file was kept on disk. Missing files are named but do not make the result a
  failure.
- The dialog emits `removed`, refreshes its list, and `MainWindow` refreshes the
  library and the pending-install indicator.
- No schema change, new dependency, shell execution, or launch-path change.
- Added service tests and a GTK smoke test. The full suite result is **443
  passed, 51 warnings in 52.41s**. No real game installation was performed.

**Step 8 — Post-Installation Executable Discovery** was also completed in this
uncommitted series.

What Step 8 did:

- Root cause of the empty `games/<game-id>` directory: the installer dialog
  always called `resolve_destination(create=True)` with the managed destination
  as the only default, before the installer ran. No ownership, metadata,
  installer, or cleanup rule required that directory.
- Added explicit destination types: installer Wine-prefix default (creates
  nothing, `install_directory = NULL`), managed `games/<game-id>` (created only
  when selected), and external folders (non-owned, never redirected).
- Added `services/executable_discovery.py`: read-only ranked `.exe` discovery in
  the installation folder, `Program Files`, `Program Files (x86)`, then other
  non-system `drive_c` folders. Helpers, installers, uninstallers, setup
  programs, redistributables, updaters, crash reporters, and diagnostics are
  filtered. Symlinks are never followed; depth, entries, and candidates are
  bounded.
- Reworked the finish page: candidates show filename, relative path, location,
  reason, and an unambiguous recommended badge; selection is explicit; manual
  selection remains; closing or "Register later" changes no launch
  configuration; nothing is auto-launched.
- Registration uses the existing `GameManager.register_installation` and
  `LaunchManager`. The executable parent becomes the working directory, and
  existing arguments, environment, cover, GameMode, MangoHud, and prefix are
  preserved. No schema migration was needed.
- Added focused service and GTK smoke tests. The full suite result is **431
  passed, 51 warnings in 51.71s**. No real game installation was performed.

**Step 7 — Hybrid ISO Mount Compatibility** was also completed in this
uncommitted series. It fixed the production mount failure recorded at the end
of Step 6.

What Step 7 did:

- Reproduced the production workflow on the real image
  `~/Downloads/<game>.iso` (420,012,032 bytes, SHA-256
  `67d4107924e3078fef70404cc15adfd4b6c676dca71c72deaca144aded72f3b3`).
  `blkid -p` reports `TYPE="iso9660"` and `PTTYPE="mac"`; UDisks exposes a
  `PartitionTable` interface (`Type='mac'`) and children `loop0p1`/`loop0p2`
  but no `Filesystem` interface on `loop0`. `udisksctl mount` therefore failed
  with `... is not a mountable filesystem.`
- Identified the cause: UDisks withholds the whole-disk `Filesystem` interface
  when partition scanning creates a partition-table object, even though the
  ISO9660 filesystem starts at offset 0.
- Fixed `IsoMountService` by passing `--no-partition-scan` to the existing
  read-only `udisksctl loop-setup` call. No mounting mechanism changed, no
  privilege changed, and no fallback was added.
- Added an actionable `IsoMountError` when UDisks still reports a
  non-mountable filesystem, naming a partitioned/hybrid layout as the likely
  cause.
- Updated `tests/test_iso_mount.py` for the new argv and for the detection
  failure path. The suite result is **409 passed, 51 warnings in 51.78s**.
- Verified the real image through production `IsoMountService`: read-only loop
  (`ro=1`), one `/proc/self/mountinfo` entry with `ro,nosuid,nodev` and
  `iso9660`, a readable ISO9660/Joliet tree, successful unmount, no remaining
  loop backing, and an unchanged SHA-256.
- Ran the full production staging path on the real image: 33 files and 4
  directories (382,898,830 bytes) staged and manifest-verified, unmounted, then
  the managed staging directory removed through the ownership service. The ISO
  was unchanged.

At the time of Step 11, the working tree contained uncommitted Steps 1–11. They
were later committed in `d58ab02` and `3c50f17`; no Git history operation was
performed during this preparation.

---

## 6. Exact next task

**Immediate action: review the v0.5.0 release-preparation changes and choose a
license. Do not start another development phase.**

Release preparation is complete but uncommitted. Do not add automatic deletion,
background monitoring, dependency installation, game termination, prefix
migration, extraction fallback, UDF fallback, or multi-disc support without
explicit approval.

Outstanding limitations to keep visible:

- Hybrid ISO mounting is verified for this one ISO9660+APM image on this host.
  General hybrid-ISO support is not claimed.
- UDF-only images are unverified; multi-disc is unsupported.
- AppImage output is verified on this host only and bundles no GTK runtime.
- Cleanup is manual only; orphan detection excludes covers and state pidfiles;
  a concurrent path-swap race is narrowed but not eliminated; sizes are apparent
  sizes.
- Executable discovery is heuristic: it hides clearly identifiable helpers only,
  demotes helper-like words in other names, and ranks by
  location/depth/name, but cannot prove a candidate is the game's main
  executable. Ambiguous results need user judgement, and manual selection
  remains available.
- GdkPixbuf cannot scale PNG while decoding; accepted 12 MP PNGs still
  allocate their full buffer during validation, and oversized files are
  rejected before that point.
- Cover decoding remains on the GTK main thread; the cache bounds retention,
  not per-rebuild work.
- Repeated dialog creation leaves a small amount of GTK/GSK C-side cache
  growth; Python objects and widgets stay flat.
- Headless llvmpipe absolute RSS is not comparable with a real Hyprland/GPU
  session; the Step 11 numbers use object counts, cache counters, and
  within-session deltas.
- Version stays `0.5.0`; D-2, D-3, and D-10 remain unresolved, and D-7/D-8 remain
  provisional.

---

## 7. Instructions for the next OpenCode session

1. Read this file completely.
2. Read `docs/FEATURE-ROADMAP.md` and `docs/IMPLEMENTATION-LOG.md`.
3. Run `git status --porcelain=v1 -uall` and `git log --oneline -5`.
   Release-preparation changes are expected to be uncommitted.
4. Verify the test baseline with:

   ```sh
   PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider
   ```

5. Inspect the actual source before trusting any historical note.
6. Do not implement a feature until the user explicitly requests it.
7. When implementing, preserve the existing release-preparation changes and
   follow the layering rules in §4.
8. Record each completed step in `docs/IMPLEMENTATION-LOG.md` using the entry
   format defined there.
9. Do not stage or commit unless the user explicitly approves.

---

## 8. Safety restrictions

These carry forward from earlier phases:

1. **Never** touch the user's real library database, prefixes, covers,
   settings, API key, logs, or game installations during development or tests.
2. Run test instances with isolated `HOME` and `XDG_DATA_HOME`,
   `XDG_CONFIG_HOME`, and `XDG_STATE_HOME` under `/tmp/opencode`, preferably
   inside `dbus-run-session`.
3. Never use broad `pkill -f` patterns or signal a process you did not start.
   An earlier session accidentally matched the user's launcher this way.
4. Never run `scripts/install.sh --uninstall` against the real `$HOME`.
5. No package installs or system/permission changes without explicit approval.
6. No root-required workflows. Do not modify Wine prefixes, game data, or
   system configuration as a side effect of diagnostics or tests.
7. Never run `open_library()` recovery against user data.
8. Do not commit, amend, stage, stash, reset, or rebase without explicit user
   approval.
9. Preserve all uncommitted release-preparation work, including packaging
   scripts and the bundled SVG.

---

## 9. Historical context

Detailed phase history, review corrections, and the audit baseline are in
`docs/IMPLEMENTATION-LOG.md`. Key historical facts:

- Phases 1–7 are committed through `6112d45`.
- Steps 1–11 were later committed in `d58ab02` and `3c50f17` (HEAD
  `6be4122`). The list below records the historical sequence.
- Phase 6.1 added read-only process reattachment and safe shutdown
  (`1df58fa`), bounded cover decoding (`a2cd938`), and SteamGridDB
  conformance verification (`02683dc`).
- Phase 7 added the theme system.
- Post-Phase-7 work included the Step 1 command-launching implementation, the
  Step 2 diagnostics implementation, the Step 3 ownership foundation, the Step
  4 ISO installer, the Step 5 cleanup/orphan work, the Step 6 integration
  fixes, the Step 7 hybrid mount fix, the Step 8 executable-discovery work, the
  Step 9 pending-install removal work, Step 10 direct installers/desktop
  integration, and Step 11 memory fixes.
- The previous handoff was stale: it recorded HEAD `02683dc` and 220 tests.
- Step 1 adds command launching, per-game environment support, and schema v3;
  the verified result is **290 tests** at HEAD `6112d45`.
- Step 2 adds read-only diagnostics and text report export; the verified result
  is **318 tests**.
- Step 3 adds ownership classification, size estimation, an authorized-only
  deletion primitive, and schema v4; the verified result is **354 tests**.
- Step 4 adds read-only ISO mounting, complete staging/verification, installer
  discovery, defer/resume sessions, destination ownership, and schema v5; the
  verified result is **379 tests**.
- Step 5 adds previewed and revalidated managed-data deletion, the cleanup and
  orphan dialogs, and on-demand orphan scanning; the verified result is
  **407 tests**.
- Step 6 audits Steps 1–5, fixes the non-relocatable AppImage desktop entry,
  adds `tests/test_packaging.py`, verifies migrations and cross-feature
  integration, runs a 14-step GTK smoke suite, and verifies the real ISO content
  where the production mount is blocked by the host/image shape; the verified
  result is **408 tests**. The working tree remains uncommitted.
- Step 7 diagnoses the real ISO mount failure as UDisks withholding the
  whole-disk `Filesystem` interface for a hybrid ISO9660+APM image, adds
  `--no-partition-scan` to the read-only loop setup, and verifies a real
  production mount with unchanged SHA-256; the verified result is **409 tests**.
  The working tree remains uncommitted.
- Step 8 adds read-only ranked executable discovery, explicit destination types,
  and finish-page registration while preserving existing launch settings; the
  verified result is **431 tests**. The working tree remains uncommitted.
- Step 9 adds confirmed pending-install removal with verified managed cleanup,
  external folders retained, missing files tolerated, and a result popup; the
  verified result is **444 tests**. The working tree remains uncommitted.
- The Step 0 audit found no implementation of diagnostics, ISO installation,
  cleanup, or ownership tracking. All four are now implemented; only automatic
  cleanup remains absent by design.

Historical notes are for context only. Re-verify all repository facts.

---

## 10. Documentation map

| File | Purpose |
|---|---|
| `HANDOFF.md` | Current session entry point, exact state, next task, safety rules |
| `docs/FEATURE-ROADMAP.md` | Product/architecture requirements, order, open decisions |
| `docs/IMPLEMENTATION-LOG.md` | Chronological milestones, audit baseline, step entries |
| `docs/PACKAGING.md` | Existing AppImage/packaging notes (preserved unchanged) |
| `README.md` | User-facing documentation (do not modify casually) |

---

## 9A. Wine prefix stabilization (Fixes 1–5)

**Verified 2026-11-05.** Details and commands: `docs/IMPLEMENTATION-LOG.md` §6.

- Migrations: each version is one `BEGIN IMMEDIATE` transaction including
  `PRAGMA user_version`; failures roll back fully. `SCHEMA_VERSION` is **6**.
- Prefix readiness: `.minigame-launcher-prefix` + `system.reg` + `drive_c`. The
  marker is written only after successful `wineboot` and validation. Legacy
  prefixes without a marker get one idempotent `wineboot -u`; nothing is deleted.
- Shared-prefix lock: `services/prefix_lock.py`, `flock` on
  `<prefixes>/.shared-prefix.lock`, bounded 300 s wait. Used by `ensure_prefix`
  and shared-prefix deletion (revalidated under the lock).
- Installer sessions: `FAILED` is protecting; `INSTALLING` is persisted before
  launch and reverted on refusal.
- Tests: full suite **488 passed in 59.75s**; GTK smoke rc=0.
- Git: HEAD unchanged (`6112d451…`). Working tree remains uncommitted; no Git
  mutation was performed. `git status` output in this environment was
  inconsistent (`--porcelain` reported clean while `git diff HEAD --stat` showed
  22 modified tracked files and 32 untracked); re-verify before committing.
