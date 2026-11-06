# Minimal Game Launcher — Implementation Log

**Purpose:** Chronological record of completed work, current baseline, and
planned phases. This file must not contain fabricated work or unverified test
results.

**Last updated:** 2026-11-05

---

## 1. Completed milestones

Historical entries are based on committed Git history and the previous
handoff. They are contextual; re-verify source and tests before relying on
them.

| Phase | Commit(s) | Summary |
|---|---|---|
| 1 | `061f11a` | Application foundation: GTK4/Adw window, models, settings, XDG paths |
| 2 | Folded into later commits | Library CRUD, search/sort, legacy data-directory migration |
| 3 | `c41b681` | Launch manager, per-game Wine prefixes, `wineboot` initialization, async signals |
| 4 | `ce5cd9d` | GameMode/MangoHud composition with fallback warnings |
| 5 | `0b5cee4` | Cover manager, validation, cache, SteamGridDB client/dialog |
| 6 | `17f933d` | Damaged-library recovery: set aside corrupt DB and sidecars |
| 6 | `7477225` | User-level desktop entry, icon, install script |
| 6 | `0c1203e` | Settings pre-fill no longer writes env API key to disk |
| 6 | `276407d` | Tests for signal-exit reporting and shutdown behavior |
| 6 | `3d96e51` | 150 ms search debounce |
| 6 | `caab1cf`, `1fe8787` | Install/data/backup/recovery/limitations documentation |
| 6.1 | `1df58fa` | Read-only process reattachment and safe shutdown |
| 6.1 | `a2cd938` | Bounded cover texture decoding |
| 6.1 | `02683dc` | SteamGridDB API conformance verification |
| 7 | `6112d45` | Configurable GTK theme system |
| Step 1 | uncommitted | Command/external app launching, per-game environment, schema v3 |
| Step 2 | uncommitted | Read-only on-demand diagnostics and report export |
| Step 3 | uncommitted | Ownership tracking, size estimation, safe deletion, schema v4 |
| Step 4 | uncommitted | Read-only ISO mount, staging/verification, installer discovery, defer/resume, schema v5 |
| Step 5 | uncommitted | Cleanup UI, verified managed-data deletion, on-demand orphan detection |
| Step 6 | uncommitted | Final integration audit, real-ISO verification, packaging fix, full regression |
| Step 7 | uncommitted | Hybrid ISO mount fix: disable partition scan on the read-only loop device |
| Step 8 | uncommitted | Post-install executable discovery, explicit destination types, no unused managed game directory |
| Step 9 | uncommitted | Pending-install removal with confirmation, verified managed cleanup, and a result popup |

### 1.1 Historical technical decisions worth preserving

**Process identity and safe shutdown (`1df58fa`)**

- Games can outlive the launcher. Pidfiles record `pid` + kernel `starttime`;
  reattachment is read-only and never signals or reaps a foreign process.
- Pidfiles are written with `O_NOFOLLOW` and mode `0600`; game IDs are
  restricted to safe characters.
- An unreadable `/proc` entry (`inaccessible`) is treated conservatively as
  potentially running, so duplicate launches are blocked and no false exit is
  emitted.
- `shutdown()` reaps only already-exited owned children with non-blocking
  `poll()`; running games and their pidfiles are left alone.
- There is intentionally no stop-game feature.

**Cover decoding (`a2cd938`, `1df58fa` follow-ups)**

- Decoded textures use a bounded LRU cache (64 entries).
- Cache identity includes device, inode, size, and nanosecond modification time.
- A file replaced during decoding is discarded rather than cached.
- Explicit invalidation is wired into cover replacement and clearing.

**Theme system (`6112d45`, plus uncommitted follow-ups)**

- Themes live in `$XDG_CONFIG_HOME/Minimalist-Game-Launcher/themes.ini`.
- Live reload uses `Gio.FileMonitor` with a debounce and keeps the last valid
  palette/appearance on malformed input.
- Settings writes are atomic and preserve symlinks, mode, line endings, and BOM.
- Phase 7 follow-up edits in the current working tree add icon fallback and
  cover-card layout refinements; they are not committed.

**Earlier safety incident**

- A prior session used a broad `pkill -f` pattern and accidentally matched the
  user's launcher. The log showed a clean exit and the database passed
  integrity checks, but the cause could not be definitively attributed.
- **Never use broad `pkill -f` patterns again.** Track only PIDs started by the
  current session.

**Packaging**

- `docs/PACKAGING.md` records an AppImage/AppDir framework and its blockers.
- `build.sh`, `scripts/build-appdir.sh`, `scripts/package-appimage.sh`, and the
  bundled SVG are untracked. Do not discard or stage them without approval.

---

## 2. Current baseline

**Verified 2026-11-05**

- Branch: `main`
- HEAD: `6112d451ba97e541cd41e1335eb3e9aa81366129`
  (`Phase 7: Add configurable GTK theme system`)
- Version: `0.5.0`
- Library schema: v5 (Step 1 added `games.environment`, Step 3 added install/
  source paths and ownership flags, Step 4 added `installer_sessions`)
- No staged changes, stashes, or tags.
- Dirty working tree:
  - post-Phase-7 UI/theme/cover/README work remains modified
  - Step 1 modified `database.py`, `models.py`, `game_manager.py`,
    `launch_manager.py`, `game_editor_dialog.py`, and two test files
  - Step 2 added `services/diagnostics.py`, `ui/diagnostics_dialog.py`,
    `tests/test_diagnostics.py`, and wired diagnostics into the application,
    main window, game card, and game details dialog
  - Step 3 added `services/ownership.py` and `tests/test_ownership.py`, extended
    `paths.py`, `models.py`, `database.py`, `game_manager.py`, `application.py`,
    and refactored diagnostics prefix classification onto the ownership service
  - Step 4 added `services/iso_mount.py`, `services/installer_manager.py`,
    `ui/installer_dialog.py`, their tests, schema v5, and installer wiring
  - Step 5 added `services/cleanup.py`, `ui/cleanup_dialog.py`,
    `tests/test_cleanup.py`, and rewired removal/orphan entry points in
    `application.py` and `ui/main_window.py`
  - Step 6 fixed the non-relocatable AppImage desktop entry in
    `scripts/build-appdir.sh` and added `tests/test_packaging.py`; no
    application source or schema changed
  - Step 7 added `--no-partition-scan` to the read-only loop setup in
    `services/iso_mount.py`, added an actionable non-mountable-filesystem error,
    and updated `tests/test_iso_mount.py`; no schema, UI, or mounting mechanism
    changed
  - untracked `HANDOFF.md`, packaging scripts/docs, bundled SVG, and the `docs/`
    specifications
- Test command:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider
```

- Test result after Step 7: **409 passed, 51 warnings in 51.78s**

The previous handoff was stale: it recorded HEAD `02683dc` and 220 tests. The
current baseline supersedes those figures.

### 2.1 Audit findings (feature audit preceding Step 0)

The audit was read-only and made no code changes.

- **Verified:** Native and Wine launching already use argv lists and
  `shell=False`; working directory and launch arguments are supported.
- **Verified:** There is no per-game environment-variable field.
- **Verified:** The executable field is file-picker-only; bare `PATH` commands
  are not resolved.
- **Verified:** There is no diagnostics service or UI. Only bounded logging and
  short launch-error dialogs exist.
- **Verified:** There is no ISO installer, staging directory, installer-session
  storage, or deferred-install flow.
- **Verified:** There is no cleanup service or orphan detection.
- **Verified:** Removal from the library deletes only the SQLite row.
- **Verified:** Wine prefixes already use stable `<prefixes>/<game-id>` paths.
- **Verified:** Schema v2 has no environment, install, installer-source, or
  ownership fields.
- **Verified:** `bsdtar`/libarchive can read ISO9660; no UDF symbol was found in
  the installed libarchive. `udisksctl` is present; `7z`, `fuseiso`, and
  `xorriso` are absent.
- **Unknown:** UDF-only ISO behavior, udisks/polkit authorization, and actual
  multi-disc installer behavior require implementation-time validation.

---

## 3. Planned feature sequence

| Step | Feature | Status |
|---|---|---|
| 1 | Command / external application launching | Implemented (uncommitted) |
| 2 | On-demand diagnostics | Implemented (uncommitted) |
| 3 | Ownership and data-management foundation | Implemented (uncommitted) |
| 4 | ISO game installer | Implemented (uncommitted) |
| 5 | Cleanup UI and orphan detection | Implemented (uncommitted) |
| 6 | Final integration and testing | Completed (uncommitted) |
| 7 | Hybrid ISO mount compatibility | Completed (uncommitted) |

Dependencies:

- Step 3 must precede Steps 4 and 5. ISO staging and cleanup both depend on
  reliable ownership and safe deletion.
- Step 4 must precede Step 5 so cleanup knows every new data location and
  session state.
- Step 1 establishes the environment model used by later phases.
- Step 2 can run before Step 3; Step 3 now provides the shared
  managed-vs-external classification used by diagnostics.
- Step 6 is last.

Open product decisions are recorded in `docs/FEATURE-ROADMAP.md` §8. D-4 is
accepted; the remaining decisions are not.

---

## 4. Step log

## Step 0 — Documentation & Handoff Preparation

**Status:** Completed

### Changes
- Replaced the stale handoff with a concise, current entry point.
- Created a durable feature roadmap covering command launching, diagnostics,
  ownership, ISO installation, cleanup, implementation order, and open
  decisions.
- Created this chronological implementation log.
- Preserved `docs/PACKAGING.md` unchanged.
- Recorded the verified 273-test baseline and the dirty working-tree state.
- Made no application-code changes and no feature implementations.

### Files Changed
- `HANDOFF.md` (untracked; rewritten)
- `docs/FEATURE-ROADMAP.md` (new; untracked)
- `docs/IMPLEMENTATION-LOG.md` (new; untracked)

### Verification
- `git status --porcelain=v1 -uall` confirmed the pre-existing modified and
  untracked files were preserved.
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider`
  completed with **273 passed, 51 warnings in 51.30s**.
- No Python, UI, CSS, database, shell, build, README, or packaging files were
  modified.
- No staging, commit, amend, stash, reset, rebase, or other Git history change
  was performed.
- No user data, Wine prefixes, game files, or configuration were accessed or
  modified.

### Remaining Issues
- The working tree remains dirty with pre-existing post-Phase-7 work and
  untracked packaging files.
- All open decisions in `docs/FEATURE-ROADMAP.md` §8 require user approval.
- Step 1 has not started.

## Step 1 — Command / External Application Launching

**Status:** Completed (uncommitted)

### Changes
- Resolved D-4 in favour of the existing Linux native platform; no new platform,
  category, or launch subsystem was added.
- Linux entries accept bare commands resolved with `shutil.which` as well as
  absolute or relative executable paths.
- Bare command values are stored as typed; resolution happens at validation and
  launch time. Path values retain the existing resolved-absolute storage.
- The editor's executable row is now editable and keeps the file-picker button.
- Added a multiline `KEY=VALUE` environment editor with validation for names,
  duplicates, reserved keys, and NUL characters.
- Added schema v3 with `games.environment TEXT NOT NULL DEFAULT '{}'`; the
  migration runs all missing steps in one transaction and stamps the version
  only afterwards.
- `LaunchManager` merges inherited → per-game → platform-managed → performance
  environment values, resolves Linux commands, and keeps argv lists plus
  `shell=False`.
- Environment values are never logged or included in user-facing errors.
- Empty environment and working-directory values preserve existing behavior.

### Files Changed
- `src/minimal_game_launcher/models.py`
- `src/minimal_game_launcher/database.py`
- `src/minimal_game_launcher/services/game_manager.py`
- `src/minimal_game_launcher/services/launch_manager.py`
- `src/minimal_game_launcher/ui/game_editor_dialog.py`
- `tests/test_game_manager.py`
- `tests/test_launch_manager.py`
- `docs/FEATURE-ROADMAP.md`
- `docs/IMPLEMENTATION-LOG.md`
- `HANDOFF.md`

### Verification
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider`
  completed with **290 passed, 51 warnings in 51.34s**.
- Added tests cover PATH resolution, unresolvable commands, argument-boundary
  preservation, environment persistence/validation/merging, reserved keys,
  command working directories, v1→v3 and v2→v3 migrations, and existing
  native/Wine launch behavior.
- A GTK editor smoke test under `dbus-run-session` with isolated XDG directories
  constructed the editor, restored executable, arguments, and environment, and
  accepted a typed bare command.
- No real user database, Wine prefix, game file, or configuration was touched.
- No staging, commit, amend, stash, reset, rebase, or other Git history change
  was performed.

### Remaining Issues
- Step 1 is uncommitted.
- Diagnostics, ownership, ISO installation, and cleanup were unimplemented at
  the end of Step 1; Steps 2 and 3 have since added diagnostics and the
  ownership foundation.
- `MANGOHUD` and `WINEPREFIX` are reserved and rejected in the per-game editor;
  no other Wine variables are managed.

## Step 2 — On-Demand Diagnostics

**Status:** Completed (uncommitted)

### Changes
- Added a read-only `DiagnosticsService` that generates one report per explicit
  user request. No background timer, daemon, or launch-path hook was added.
- Checks cover Wine availability/version, prefix existence/accessibility/
  initialization, managed vs external prefix classification, DXVK DLL presence
  and caveated `user.reg` override hints, Vulkan availability, executable
  validity, working-directory validity, last recorded process state, per-game
  environment variable names, launcher-log availability, and free space.
- Slow probes use argument lists, `shell=False`, captured output, and bounded
  timeouts. Diagnostics never runs `wineboot`, starts a game, installs a
  dependency, or writes to a prefix.
- Added a diagnostics dialog reachable from the game card menu and the game
  details dialog. Each check shows a status and plain-language summary, with
  practical suggestions and collapsible technical details.
- Added local plain-text report export through a GTK save dialog. Export uses no
  network access.
- Path redaction uses `~` for the home directory by default, matching the D-7
  recommendation. The dialog exposes an explicit full-paths switch. D-7 remains
  provisional, not formally accepted.
- Reports never read or include environment values, settings-file contents,
  API keys, registry dumps, or game launch arguments. Environment keys may be
  listed by name only.
- DXVK reporting follows the D-8 recommendation provisionally: presence plus
  caveated override hints, with no exact-version claim.
- No database schema change was required; schema remains v3.

### Files Changed
- `src/minimal_game_launcher/services/diagnostics.py` (new)
- `src/minimal_game_launcher/ui/diagnostics_dialog.py` (new)
- `src/minimal_game_launcher/application.py`
- `src/minimal_game_launcher/ui/main_window.py`
- `src/minimal_game_launcher/ui/game_card.py`
- `src/minimal_game_launcher/ui/game_details_dialog.py`
- `tests/test_diagnostics.py` (new)
- `docs/FEATURE-ROADMAP.md`
- `docs/IMPLEMENTATION-LOG.md`
- `HANDOFF.md`

### Verification
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider`
  completed with **318 passed, 51 warnings in 51.56s**.
- 28 new tests cover missing and available Wine, prefix missing/inaccessible/
  valid/uninitialized, managed vs external and symlinked prefixes, DXVK present/
  absent/override hints, Vulkan available/unavailable/failure, executable and
  working-directory failures, report text and export, `~` path redaction,
  environment-value exclusion, isolated check failure, process-record reporting,
  and no-write behavior.
- A GTK smoke test under `dbus-run-session` with isolated `HOME`/XDG directories
  constructed the diagnostics dialog with a fake service, received a report on
  the main loop, and enabled export. It did not start a game or touch real
  prefixes.
- All diagnostics tests use temporary directories and injected subprocess/
  Vulkan lookups. No real Wine, Vulkan, game, database, or user configuration
  was modified.
- No staging, commit, amend, stash, reset, rebase, or other Git history change
  was performed.

### Remaining Issues
- Step 1 and Step 2 are uncommitted.
- Diagnostics classify managed vs external prefixes but do not implement the
  ownership foundation or cleanup (Step 3).
- D-7 and D-8 are implemented provisionally and still require explicit user
  approval.
- Exit codes are not persisted, so the last-launch check reports current
  pidfile state only; it cannot show a historical exit status.
- Report export uses plain text; a structured JSON/Markdown format remains an
  open question in `docs/FEATURE-ROADMAP.md` §8.
- Wine and Vulkan probes were exercised with mocks plus a dialog smoke test;
  real host compatibility across distributions is not claimed.

## Step 3 — Ownership & Data-Management Foundation

**Status:** Completed (uncommitted)

### Changes
- Added `services/ownership.py` with `OwnershipService`, `ResourceKind`,
  `OwnershipStatus`, `OwnershipVerdict`, `SizeEstimate`, and `DeletionResult`.
- Ownership is exact resolved-path equality with `<root>/<game-id>` for
  `prefixes`, `games`, or `installers`. It is never inferred from a directory
  name or from living under the home directory.
- Added `paths.games_directory()` and `paths.installers_directory()` for the two
  reserved roots. Neither root is created, and no existing prefix, cover,
  database, or state location moved.
- Added schema v4 fields `install_directory`, `install_directory_owned`,
  `installer_source_directory`, and `installer_source_owned`, with `NULL` paths
  and `0` owned flags by default. The migration runs v1→v4, v2→v4, and v3→v4 in
  one transaction and preserves environment and `wine_prefix_path`.
- `GameManager.update_game()` preserves ownership metadata because the editor
  does not edit it.
- Classification returns `managed`, `external`, `missing`, or `invalid` with a
  reason. Empty paths, `..` segments, unsafe game IDs, symlinks, roots,
  mismatched game IDs, non-directories, and unreadable paths are `invalid`.
- Size estimation verifies ownership first, walks with `O_NOFOLLOW`/`dir_fd`,
  never follows symlinks, counts hardlinks once, and returns
  `complete=False` plus errors on partial traversal.
- Deletion requires `authorized=True`, refuses a running game, revalidates
  ownership, opens with `O_NOFOLLOW|O_DIRECTORY`, deletes via `dir_fd`, and
  re-checks `(st_dev, st_ino)` before `rmdir`. It refuses roots, external paths,
  symlinks, and mismatched IDs, and reports partial failures.
- No UI or automatic cleanup calls the deletion primitive.
- Diagnostics now uses the shared ownership classification for prefixes.
  `classify_prefix` and `PrefixOwnership` were removed. Diagnostics stays
  read-only and keeps all existing checks.
- No ISO mounting/extraction, installer discovery/execution, multi-disc,
  cleanup UI, orphan UI, DXVK, or prefix migration was added.

### Files Changed
- `src/minimal_game_launcher/errors.py`
- `src/minimal_game_launcher/models.py`
- `src/minimal_game_launcher/paths.py`
- `src/minimal_game_launcher/database.py`
- `src/minimal_game_launcher/application.py`
- `src/minimal_game_launcher/services/game_manager.py`
- `src/minimal_game_launcher/services/ownership.py` (new)
- `src/minimal_game_launcher/services/diagnostics.py`
- `tests/test_ownership.py` (new)
- `tests/test_diagnostics.py`
- `tests/test_game_manager.py`
- `docs/FEATURE-ROADMAP.md`
- `docs/IMPLEMENTATION-LOG.md`
- `HANDOFF.md`

### Verification
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider`
  completed with **354 passed, 51 warnings in 51.52s**.
- 34 new ownership tests cover all three managed kinds, external paths, missing
  paths, invalid paths, traversal, symlinks inside and outside the root,
  symlink replacement before and during deletion, root deletion rejection,
  mismatched game IDs, size estimation (including hardlinks, symlinks, and
  permission errors), deletion authorization, running-game refusal, external
  refusal, partial failures, and implicit-root non-creation.
- Migration tests cover v1→v4, v2→v4, and v3→v4 with row, environment, and Wine
  prefix preservation plus conservative ownership defaults. An update test
  verifies ownership metadata survives a metadata edit.
- Diagnostics tests were updated to the shared ownership API: 28 passed,
  including the symlink-is-invalid behavior.
- A manual sandbox check in `/tmp` created a managed tree with a symlink to
  `/etc`, estimated it without traversing the link, deleted it, and confirmed
  `/etc` was untouched.
- All tests use temporary directories and databases. No real user database,
  Wine prefix, game file, or configuration was modified or deleted.
- No staging, commit, amend, stash, reset, rebase, or other Git history change
  was performed.

### Remaining Issues
- Steps 1–3 are uncommitted.
- The ownership service is not wired to any user-facing deletion flow; that is
  Step 5 work and remains gated on **D-5** and **D-9**.
- Step 4 (ISO installer) had not started when this entry was written. Installer
  session and disc tables were deliberately deferred.
- Deletion narrows but cannot fully eliminate a concurrent path-swap race; it
  records an error instead of claiming complete protection.
- Size estimation uses apparent size, so sparse files and disk allocation are
  not reported separately.
- The owned flags are metadata only; deletion still revalidates the managed
  root and does not trust the flag.

---

## Step 4 — ISO Game Installer

**Status:** Completed (uncommitted)

### Changes
- Added `services/iso_mount.py` with `IsoMountService` and `MountedIso`.
  - Mounts through `udisksctl loop-setup --no-user-interaction --read-only` and
    `udisksctl mount --no-user-interaction -b <loop> --options ro`.
  - Verifies the loop device reports `ro=1` and that exactly one `/proc/self/mountinfo`
    entry matches the recorded device and target before the mount is used.
  - Releases only the mount and loop device recorded by the same operation, with
    `udisksctl unmount` and `udisksctl loop-delete`. No root, no privilege
    escalation, no archive-extraction fallback.
  - Verified on this host: an unprivileged read-only mount of a throwaway
    ISO9660 image succeeded, `/proc/self/mountinfo` showed `ro,nosuid,nodev`,
    and the ISO hash was unchanged after unmount.
- Added `services/installer_manager.py` with `InstallerManager`,
  `InstallerCandidate`, `CandidateKind`, `StagingSummary`, and
  `InstallationDestination`.
  - Stages the complete ISO tree into
    `<installers>/<game-id>/source/` plus `.mgl-staging.json` (SHA-256 and size
    per file, directory list), verifies the tree, then unmounts the ISO.
  - Rejects symlinks, special files, unsafe names, excessive depth, excessive
    entry counts, and insufficient free space before or during copying.
  - Failed staging removes only the directory created by that operation through
    `OwnershipService.delete(..., authorized=True)`; an existing staging
    directory is never altered or removed.
  - Discovery is read-only and name/content based (`.exe` installer-like names,
    `.sh`/`.run`/`.AppImage`, ELF magic). Nothing is executed automatically and
    the first executable is never selected or registered automatically.
  - Sessions can be deferred and resumed; resume verifies the staged copy
    against the manifest without mounting or re-copying the ISO.
  - Managed destinations are exactly `<games>/<game-id>` and owned; external
    destinations outside the managed games root are non-owned and rejected when
    non-empty. Destinations equal to the ISO or inside staging are refused.
  - The installer runs through the existing `LaunchManager` using the reserved
    stable game ID and its managed Wine prefix (`<prefixes>/<game-id>`); the
    prefix is created lazily by the existing launch path. DXVK, Winetricks, and
    runtimes are not installed automatically.
- Added schema v5 with an `installer_sessions` table. Existing `games` rows and
  ownership columns are unchanged; v4→v5 migration is covered by a test.
- Added `models.InstallerStatus` and `models.InstallerSession`.
- Added `GameManager.register_installation()` to register the final game with
  the reserved ID, destination ownership, and installer-source metadata.
- Added `ui/installer_dialog.py` with `InstallerDialog` (ISO selection, staging
  progress, candidate selection, defer, finish-later resume, final executable
  registration) and `PendingInstallsDialog`.
- Wired the ISO install and pending-installs entry points in
  `ui/main_window.py` and the composition root (`application.py`), including
  `IsoMountService.shutdown()` during application shutdown.
- Added `InstallerError`, `IsoMountError`, and `InstallerStagingError`.
- No cleanup UI, orphan detection, automatic deletion, prefix migration,
  multi-disc support, daemons, packaging, theme, or README changes.

### Files Changed
- `src/minimal_game_launcher/errors.py`
- `src/minimal_game_launcher/models.py`
- `src/minimal_game_launcher/database.py`
- `src/minimal_game_launcher/application.py`
- `src/minimal_game_launcher/services/game_manager.py`
- `src/minimal_game_launcher/services/iso_mount.py` (new)
- `src/minimal_game_launcher/services/installer_manager.py` (new)
- `src/minimal_game_launcher/ui/installer_dialog.py` (new)
- `src/minimal_game_launcher/ui/main_window.py`
- `tests/test_iso_mount.py` (new)
- `tests/test_installer_manager.py` (new)
- `tests/test_game_manager.py`
- `docs/FEATURE-ROADMAP.md`
- `docs/IMPLEMENTATION-LOG.md`
- `HANDOFF.md`

### Verification
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider`
  completed with **379 passed, 51 warnings in 52.03s**.
- 25 new tests: 9 in `tests/test_iso_mount.py` and 16 in
  `tests/test_installer_manager.py`, all passing.
- Mount tests cover command construction, unavailable `udisksctl`, read-only loop
  verification, single-mount verification, ownership of cleanup, and no cleanup
  of mounts the service did not create. They use injected `run_factory` and fake
  mountinfo/sysfs paths; no real mount is created by the test suite.
- Installer tests cover complete staging and verification, unexpected/missing
  files, symlink rejection, existing-staging refusal, failed-staging cleanup,
  discovery classification and limits, installer selection, destination
  ownership and rejection rules, session persistence, Wine-prefix association,
  defer/resume verification, and v4→v5 migration. They use temporary XDG-style
  directories and an injected fake mount service.
- Real-host smoke checks (manual, outside the test suite): an unprivileged
  read-only mount and unmount left the ISO hash unchanged; a full
  `InstallerManager.prepare()` against `udisksctl` staged 3 files (21 bytes),
  discovered `setup.exe`, and created the managed destination; no loop device or
  mount remained. GTK smoke under `dbus-run-session` constructed
  `InstallerDialog` (resumed a deferred session and re-populated candidates) and
  `PendingInstallsDialog` with isolated XDG directories and no GTK criticals.
- All tests use temporary directories and databases. No real user database,
  Wine prefix, game file, ISO, or configuration was modified or deleted.
- No staging, commit, amend, stash, reset, rebase, or other Git history change
  was performed.

### Remaining Issues
- Steps 1–4 are uncommitted.
- Multi-disc installers are not supported: one ISO per session; no disc table,
  merge, or disc-swap prompt.
- UDF-only images were not tested on this host; support depends on the kernel
  and UDisks mount helper. There is no `7z` or extraction fallback.
- Deferred/failed sessions are visible in the pending-installs dialog, but there
  is no removal or cleanup UI for abandoned staging directories.
- External destinations are non-owned by design; MGL never deletes them.
- `IsoMountService` cleanup attempts to release a mount even when verification
  fails, but a forced loop-device detach by another process cannot be prevented.
- No DXVK, Winetricks, runtime, or dependency installation is performed.

## Step 5 — Game Data Cleanup and Orphan Detection

**Status:** Completed (uncommitted)

### Changes
- Added `services/cleanup.py` with `CleanupService`, `ResourceTarget`,
  `CleanupIssue`, `CleanupOutcome`, `CleanupReport`, `OrphanScan`, and
  `PROTECTING_SESSION_STATUSES`.
  - Metadata-only removal stays the default: the library record switch is on,
    every filesystem switch is off, and no file is touched unless the user
    selects it.
  - Target discovery reuses each game's stored `install_directory`,
    `wine_prefix_path`, and `installer_source_directory`. External, missing, and
    invalid paths are shown with an explanation instead of being hidden, and are
    never selectable.
  - Every destructive call requires `authorized=True`, reclassifies ownership
    through `OwnershipService`, refuses a symlinked or non-directory managed
    root, and re-checks the `(st_dev, st_ino)` directory identity captured at
    preview. A changed path, changed metadata reference, running game, or
    protecting installer session skips that target with a reason.
  - Deletion delegates entirely to `OwnershipService.delete`; there is no
    parallel delete implementation and no broader recursive removal.
  - Running games are detected through `LaunchManager.is_active`, so a starting
    or running game blocks all of its resources.
  - `pending`, `staged`, `installing`, `installer_finished`, and `deferred`
    sessions protect their staging directory, owned destination, and managed
    prefix. Terminal `installed` and `failed` sessions do not.
  - Orphan scanning is on demand only, inspects direct children of the three
    managed roots with `os.scandir`, never follows symlinks, never creates a
    root, and is bounded by `max_scan_entries`. Symlinks, non-directories,
    unsafe names, unclassifiable entries, and entries still associated with a
    game or protecting session are reported as issues, not candidates.
  - Orphan revalidation re-checks ownership, root safety, directory identity,
    running state, session state, and game association before deleting.
  - Partial failures are reported per target with remaining size; one failure
    does not stop the remaining selected targets.
  - Covers, state pidfiles, and the original ISO are deliberately not scanned or
    deleted in this step.
- Added `ui/cleanup_dialog.py` with `CleanupDialog`, `OrphanCleanupDialog`, and
  `CleanupResultDialog`.
  - `CleanupDialog` replaces the direct remove action: library record switch on
    by default, one switch per verified managed target with path, status, and
    size, disabled rows plus the reason for anything unsafe, and an explicit
    destructive confirmation listing exactly what will be removed.
  - The library record is removed only after every selected target was fully
    deleted. A partial failure keeps the record and shows the error, so no
    library entry points at data that still exists but is no longer tracked.
  - `OrphanCleanupDialog` shows scan candidates and non-removable issues
    separately, with confirmation and no automatic selection.
  - `CleanupResultDialog` shows removed, skipped, and failed targets plus what
    remains.
- Rewired `ui/main_window.py`: game removal opens `CleanupDialog`; a header
  action opens `OrphanCleanupDialog`. Wired `CleanupService` into the
  composition root in `application.py` with the launch manager's active-game
  probe.
- No database migration was required: schema v5 already provides every path,
  ownership flag, and installer session status used here.
- No automatic deletion, background scanning, daemon, timer, game termination,
  prefix migration, packaging, theme, or README change.

### Files Changed
- `src/minimal_game_launcher/services/cleanup.py` (new)
- `src/minimal_game_launcher/ui/cleanup_dialog.py` (new)
- `src/minimal_game_launcher/application.py`
- `src/minimal_game_launcher/ui/main_window.py`
- `tests/test_cleanup.py` (new)
- `docs/FEATURE-ROADMAP.md`
- `docs/IMPLEMENTATION-LOG.md`
- `HANDOFF.md`

### Verification
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider`
  completed with **407 passed, 51 warnings in 51.81s**.
- 28 new tests in `tests/test_cleanup.py`, all passing. They cover:
  - resource discovery for a valid game, a deferred session, and true orphans;
  - external paths never becoming orphan candidates, unsafe entry names
    reported as issues, bounded scanning, and no managed-root creation;
  - metadata-only removal preserving every file, per-resource deletion
    preserving the other resources, and unselected targets remaining untouched;
  - refusal of external installations, running games, and active/deferred
    installer sessions;
  - symlink swap, directory identity change, path traversal, and metadata change
    after preview all being refused;
  - partial-failure reporting with the library record surviving, symlinked
    managed roots, unrelated records untouched, and the authorization guard;
  - dialog selection/disable behavior, confirmation text, and result-dialog
    partial-failure rendering.
- A GTK smoke test under `dbus-run-session` with isolated `HOME` and XDG
  directories constructed `CleanupDialog`, `OrphanCleanupDialog`, and
  `CleanupResultDialog` against a temporary database with no GTK criticals. It
  performed no real deletion.
- All tests use temporary directories and databases. No real user database,
  Wine prefix, game file, ISO, or configuration was modified or deleted.
- No staging, commit, amend, stash, reset, rebase, or other Git history change
  was performed.

### Remaining Issues
- Steps 1–5 are uncommitted.
- Cleanup is manual only; there is no automatic cleanup, scheduled scan, or
  free-space reclaim on removal.
- Orphan detection covers only the managed roots. Covers orphaned by an external
  change, state pidfiles, and abandoned `installer_sessions` rows are not
  scanned or removed here.
- `installed` and `failed` installer sessions are treated as terminal and
  therefore do not protect data; their rows are not cleaned up by this step.
- Deletion narrows but cannot eliminate a concurrent path-swap race; it records
  an error rather than claiming full protection.
- Size figures are apparent sizes from `OwnershipService`, so sparse files and
  on-disk allocation are not reported separately.
- Step 6 (final integration and testing) has not started.

---

## Step 6 — Final Integration and Testing

**Status:** Completed (uncommitted)

### Changes
- Audited Steps 1–5 against the current source rather than the historical notes.
  No subsystem was rewritten and no feature was added.
- Fixed one concrete packaging defect. `scripts/build-appdir.sh` substituted the
  absolute build path into the installed desktop entries
  (`Exec=/home/.../build/appdir/usr/bin/minimal-game-launcher`), which cannot
  resolve inside a relocated AppImage or an installed AppDir. Installed entries
  now use `Exec=minimal-game-launcher`; only the AppDir root entry uses
  `Exec=AppRun`. The staging directory is overridable with `MGL_APPDIR` so the
  regression test can build outside the repository.
- Added `tests/test_packaging.py`, which builds an AppDir into a temporary
  directory and asserts relocatable `Exec=` values, the root `Exec=AppRun`, the
  required package modules and assets, and the absence of user data.
- Corrected the stale blocker section in `docs/PACKAGING.md`: `appimagetool`,
  `mksquashfs`, and a runtime are available on this host, and
  `dist/Minimal-Game-Launcher.AppImage` is produced.
- No database schema change, migration, application behavior change, theme
  change, or README change. No background service, extraction fallback, UDF
  fallback, multi-disc support, dependency installation, or game termination was
  added.

### Files Changed
- `scripts/build-appdir.sh`
- `tests/test_packaging.py` (new)
- `docs/PACKAGING.md`
- `docs/FEATURE-ROADMAP.md`
- `docs/IMPLEMENTATION-LOG.md`
- `HANDOFF.md`

### Verification
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider`
  completed with **408 passed, 51 warnings in 51.82s**. The 51 warnings are the
  unchanged GTK/PyGObject deprecations (`Gtk.CssProvider.load_from_data`,
  `Gdk.Texture.new_for_pixbuf`, `Gtk.Widget.get_allocation`, asyncio policy) and
  GTK allocation warnings. No warning was suppressed.
- The baseline before this step was 407 passed; `tests/test_packaging.py` adds
  the one new test.
- Schema matrix (`/tmp/opencode/step6_migration_matrix.py`): fresh, v1→v5,
  v2→v5, v3→v5, and v4→v5 all preserve rows and stamp `user_version=5`; a forced
  mid-migration failure rolls back with no partial v3/v4/v5 objects and keeps
  `user_version=1`; a newer schema (`6`) raises `RuntimeError`.
- Cross-feature integration (`/tmp/opencode/step6_integration.py`): installer
  session → staging → discovery → installer selection → installer launch with a
  stable managed prefix and `wineboot` marker → managed destination →
  `register_installation` with ownership metadata → cleanup blocked by an
  `installer_finished` session and by a running game → allowed after the terminal
  `installed` state → installation, prefix, and installer source removed →
  orphan scan and orphan deletion.
- GTK smoke suite (`/tmp/opencode/step6_gtk_smoke.py`) under `dbus-run-session`
  with isolated `HOME`/`XDG_*`, `G_DEBUG=fatal-criticals`,
  `GTK_A11Y=none`, and `NO_AT_BRIDGE=1`: rc=0, `SMOKE OK steps 14 errors []`.
  Covered the main window, repeated population, search/empty state/sort, game
  editor, details, diagnostics, installer resume, pending installs, cleanup,
  orphan cleanup, cleanup result, settings, theme switch, and appearance.
- AppImage: `./build.sh` completes; `dist/Minimal-Game-Launcher.AppImage` is
  1,051,128 bytes; the installed desktop entries contain
  `Exec=minimal-game-launcher` and the root entry `Exec=AppRun`; the AppImage
  contains no database, settings, or user data. A runtime smoke under an isolated
  `HOME`/`XDG_*` with `APPIMAGE_EXTRACT_AND_RUN=1` stayed alive, created only an
  isolated `library.db` and `launcher.log`, and logged no GTK criticals.
  Cross-distribution portability was not tested and is not claimed.
- Real ISO: exactly one suitable image was present,
  `~/Downloads/Plants Vs. Zombies.iso` (420,012,032 bytes, SHA-256
  `67d4107924e3078fef70404cc15adfd4b6c676dca71c72deaca144aded72f3b3`). The image
  is a hybrid ISO9660 + Apple Partition Map disc. Production
  `IsoMountService.mount()` could not mount it on this host:
  `udisksctl mount -b /dev/loop0` returned
  `Object /org/freedesktop/UDisks2/block_devices/loop0 is not a mountable filesystem.`
  even though `blkid` reports `TYPE="iso9660"`, and no unprivileged mount
  alternative exists. This is an image/host limitation, not an MGL code defect;
  no fallback or code change was added.
- Real-ISO content verification therefore replaced only the mount provider with
  a fake service backed by a read-only `bsdtar` extraction of the same image.
  Everything after mount ran through production code: 33 files and 4 directories
  (382,898,830 bytes) staged and manifest-verified, 5 installer and 3 game
  candidates discovered, the deferred session survived a database reopen, the
  managed destination was created and owned, ownership deletion removed the
  expected 40 entries plus the source directory, and the ISO SHA-256 was
  unchanged. The mount step of this run is explicitly mocked; it is not evidence
  that this image mounts on this host.
- No real user database, Wine prefix, game file, or configuration was touched.
  The ISO was read only and was not moved, renamed, or deleted. Test-created
  directories under `/tmp/opencode` were removed after ownership was verified.
- No staging, commit, amend, stash, reset, rebase, checkout, or branch change was
  performed.

### Remaining Issues
- Steps 1–6 remain uncommitted; `HANDOFF.md`, the docs, packaging scripts, and
  several services/tests are still untracked.
- The real ISO could not be mounted through production `IsoMountService` on this
  host. UDF-only images remain unverified, and multi-disc installers remain
  unsupported. There is no extraction or UDF fallback.
- AppImage packaging was verified only on this host and does not bundle GTK,
  libadwaita, or PyGObject; cross-distribution portability is not claimed.
- Cleanup remains manual only; orphan detection still excludes covers and state
  pidfiles; a concurrent path-swap race is narrowed but not eliminated; sizes are
  apparent sizes.
- Version remains `0.5.0`; D-10 (version bump) and D-2/D-3 remain unresolved, and
  D-7/D-8 remain provisional.
- No automatic cleanup, background scanning, dependency installation, game
  termination, or prefix migration exists, by design.

---

## Step 7 — Hybrid ISO Mount Compatibility

**Status:** Completed (uncommitted)

### Changes
- Reproduced the Step 6 production failure on the real image instead of assuming
  the cause from the error text.
- Root cause: the image is ISO9660 with an Apple Partition Map. UDisks parsed
  the APM, exposed a `PartitionTable` object and `loop0p1`/`loop0p2` children,
  and withheld the whole-disk `Filesystem` interface. `udisksctl mount` therefore
  returned `Object /org/freedesktop/UDisks2/block_devices/loop0 is not a
  mountable filesystem`, even though `blkid` reports `TYPE="iso9660"`.
- Fixed `IsoMountService` with the smallest change: add `--no-partition-scan`
  to the existing `udisksctl loop-setup --read-only` argv. UDisks then exposes
  the ISO9660 filesystem on the loop device and mounts it read-only.
- Added an actionable `IsoMountError` for a remaining `not a mountable
  filesystem` response, naming a partitioned/hybrid layout as the likely cause.
- No mounting mechanism change, no privilege change, no `sudo`, no FUSE
  dependency, no archive extraction, no schema change, and no UI change.
- Existing ownership tracking and cleanup are unchanged: only the loop device
  and mount recorded by the same operation are released. Failed mounts still
  delete only the loop device created by that attempt.

### Files Changed
- `src/minimal_game_launcher/services/iso_mount.py`
- `tests/test_iso_mount.py`
- `docs/FEATURE-ROADMAP.md`
- `docs/IMPLEMENTATION-LOG.md`
- `HANDOFF.md`

### Verification
- Image facts: `~/Downloads/Plants Vs. Zombies.iso`, 420,012,032 bytes,
  SHA-256 `67d4107924e3078fef70404cc15adfd4b6c676dca71c72deaca144aded72f3b3`.
  `file` reports `ISO 9660 CD-ROM filesystem data 'PVZPCMC_EN'`; `blkid -p`
  reports `TYPE="iso9660"`, `LABEL="PVZPCMC-EN"`,
  `VERSION="Joliet Extension"`, and `PTTYPE="mac"`.
- Before the fix, `gdbus introspect` on
  `/org/freedesktop/UDisks2/block_devices/loop0` showed `UDisks2.PartitionTable`
  with `Type='mac'`, children `loop0p1`/`loop0p2`, and no
  `UDisks2.Filesystem`; `udisksctl mount -b /dev/loop0 --options ro` failed with
  the reported error.
- After the fix, `udisksctl loop-setup --read-only --no-partition-scan` produced
  `loop0` with no partition children; `gdbus introspect` showed
  `UDisks2.Filesystem`; production `IsoMountService.mount()` returned
  `/run/media/onirafu/PVZPCMC-EN`.
- Verification script (`/tmp/opencode/verify_iso_mount.py`) observed: `ro=1` in
  `/sys/block/loop0/ro`; exactly one `/proc/self/mountinfo` entry,
  `ro,nosuid,nodev,relatime ... - iso9660 /dev/loop0 ro,...`; 8 readable
  top-level entries including `AUTOPLAY.BAT`, `DirectX`, and `installers`;
  successful unmount; no remaining mount entry; `losetup -a` empty; and an
  unchanged SHA-256.
- Full production pipeline on the real image
  (`/tmp/opencode/step7_iso_integration.py`): `InstallerManager.create_session`
  → `prepare` mounted through `IsoMountService`, staged and manifest-verified
  33 files and 4 directories (382,898,830 bytes), then unmounted. `losetup -a`
  was empty after staging; ownership classified the staging directory as
  `managed`; `OwnershipService.delete(authorized=True)` removed it; the ISO
  SHA-256 was unchanged.
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider`:
  **409 passed, 51 warnings in 51.78s**. The new test covers the actionable
  detection-failure error and loop-device cleanup; the existing argv assertions
  now require `--no-partition-scan`.
- No real user database, Wine prefix, game file, or configuration was touched.
  The ISO was read-only and was not moved, renamed, or deleted.
- No staging, commit, amend, stash, reset, rebase, checkout, or branch change was
  performed.

### Remaining Issues
- Steps 1–7 remain uncommitted.
- The hybrid fix is verified on one ISO9660 + Apple Partition Map image on this
  host. General hybrid-ISO support is not claimed.
- UDF-only images remain unverified; multi-disc installers remain unsupported.
  There is no extraction or UDF fallback.
- `--no-partition-scan` assumes the ISO filesystem is on the whole device, which
  is the normal ISO9660 layout. A partitioned image whose payload lives only in a
  child partition is not mounted by this workflow and would surface the new
  actionable error.

---

## Step 8 — Post-Installation Executable Discovery

**Status:** Completed (uncommitted)

### Changes
- Root cause of the empty `games/<game-id>` directory: `InstallerDialog`
  called `resolve_destination(create=True)` unconditionally in `_on_prepared`,
  with the MGL-managed destination as the only default. The directory was
  created before the installer ran even when the installer wrote into its Wine
  prefix. Ownership, cleanup, and installer metadata did not require that empty
  directory.
- Added `services/executable_discovery.py`, a read-only `.exe` scan for a
  completed installation. Search order is the configured installation
  directory, `drive_c/Program Files`, `drive_c/Program Files (x86)`, then other
  non-system direct children of `drive_c`.
- Ranking is deterministic: root priority, shallower path, exact/contained game
  name, and top-level placement. Installers, uninstallers, setup programs,
  redistributables, crash reporters, and diagnostics are hidden. Updaters and
  setup-style names are hidden only as whole words. Helper-like words that also
  appear in ordinary titles (`Patchwork`, `Setup Quest`, `Updated Quest`) are
  demoted, not hidden. The best candidate is marked recommended only when its
  score is strictly higher than the runner-up.
- **Fix (post-audit):** the earlier substring filter hid valid games such as
  `Patchwork.exe`, `Crashlands.exe`, `Updated Quest.exe`, and `Setup Quest.exe`.
  Regression tests in `tests/test_executable_discovery.py` cover them.
- Discovery never executes files, follows no symlinks, opens directories with
  `O_NOFOLLOW`, skips missing/unreadable roots and entries, and bounds depth,
  entries, and candidates. No Wine prefix or game directory is modified.
- Added explicit destination types: installer Wine-prefix default, MGL-managed
  `games/<game-id>`, or external folder. The Wine default creates no game
  directory and stores no installation path; the managed directory is created
  only when explicitly selected. Existing session destinations are preserved
  when resuming; nothing is silently redirected.
- Reworked the installer finish page to display discovered candidates with
  relative path, search location, reason, and a recommended badge. Registration
  requires explicit selection. Manual file selection remains available, and
  "Register later"/close persists no launch change.
- Registration uses the existing `GameManager.register_installation` and
  `LaunchManager` architecture. The executable's parent is the working
  directory. `InstallerManager.installation_draft` preserves an existing
  record's arguments, environment, cover, GameMode, MangoHud, and prefix.
- No schema change was required. Existing records and launch paths are
  unchanged; `install_directory = NULL` now correctly represents a
  Wine-prefix-only installation.
- No new dependencies, shell execution, platform enum, or launch manager.

### Files Changed
- `src/minimal_game_launcher/services/executable_discovery.py` (new)
- `src/minimal_game_launcher/services/installer_manager.py`
- `src/minimal_game_launcher/ui/installer_dialog.py`
- `tests/test_executable_discovery.py` (new)
- `tests/test_installer_ui.py` (new)
- `tests/test_installer_manager.py`
- `docs/FEATURE-ROADMAP.md`
- `docs/IMPLEMENTATION-LOG.md`
- `HANDOFF.md`

### Verification
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider`:
  **431 passed, 51 warnings in 51.71s**.
- Added focused coverage for: custom installation folders, Program Files,
  Program Files (x86), other `drive_c` folders, deterministic ranking,
  helper/installer filtering, no candidates, missing and inaccessible roots,
  symlink safety, traversal limits, registration persistence and working
  directory, cancellation without registration, finished/deferred resume,
  Wine-prefix-only installs without a `games/<game-id>` directory, managed
  destination ownership/cleanup, external destination preservation, and
  existing launch-configuration compatibility.
- GTK smoke tests passed as part of the suite, including the new installer
  dialog candidate-selection/cancel smoke test and the existing theme UI tests.
- No real game installation was performed. No user database, prefix, game file,
  or configuration was touched.
- No staging, commit, amend, stash, reset, rebase, checkout, or branch change was
  performed.

### Remaining Issues
- Discovery is name-, location-, and extension-based; it cannot verify that a
  candidate is functionally the game's main executable. Ambiguous results still
  require the user's judgement, and manual selection remains the fallback.
- Helper filtering may hide an unusually named primary executable; the manual
  picker covers that case.
- The Wine default destination relies on the installer's own choices. MGL does
  not redirect an installer to the managed folder automatically.
- Steps 1–8 remain uncommitted.

---

## Step 9 — Pending Installation Removal

**Status:** Completed (uncommitted)

### Changes
- `PendingInstallsDialog` gains a destructive `Remove` action per session, next
  to `Resume`. It opens a confirmation dialog listing the installer staging
  directory, the Wine prefix, the managed destination when owned, and any
  external destination that will be kept, and states that the original ISO is
  kept. The confirmation offers two choices: **Remove session only** (files stay
  on disk) and **Remove files and session** (destructive).
- `InstallerManager.session_resources()` returns the managed resources a
  pending install may own: installer staging, the Wine prefix, and the managed
  destination only when `install_directory_owned` is true. An external
  destination and a Wine-default install contribute no deletable path.
- `InstallerManager.remove_pending_install(session, authorized=True,
  game_running=False, delete_files=True)` revalidates each resource through
  `OwnershipService` immediately before deletion, deletes verified managed
  directories, and then removes the session row. With `delete_files=False` only
  the session row is deleted and every file stays on disk. A running installer
  is refused. Authorization is mandatory.
- A resource that is already missing is recorded as `missing` and counted as
  success, so the session record is still removed and the user's intent is
  honoured. Only a real failure (unmanaged path, deletion error) keeps the
  session for a retry.
- Removal runs on a worker thread; the session row is deleted on the GTK main
  thread because the SQLite connection is thread-bound. Deleting it from the
  worker raised `sqlite3.ProgrammingError`, which made every removal report that
  the record was kept. The worker passes `delete_session=False`, then
  `discard_pending_session()` runs on the main thread.
- The result is presented in a popup listing
  `Deleted`, `Already missing`, `Kept (external)`, and `Failed` resources plus
  whether the session record was removed. Missing files are called out but do
  not turn the result into a failure.
- `PendingInstallsDialog` emits `removed`, refreshes its list, and shows the
  empty state when no sessions remain. `MainWindow` refreshes the library and
  the pending-install button visibility on that signal.
- No schema change, new dependency, shell execution, or launch-path change. The
  original ISO and any external folder are never deleted.

### Files Changed
- `src/minimal_game_launcher/services/installer_manager.py`
- `src/minimal_game_launcher/ui/installer_dialog.py`
- `src/minimal_game_launcher/ui/main_window.py`
- `tests/test_pending_removal.py` (new)
- `tests/test_installer_ui.py`
- `docs/FEATURE-ROADMAP.md`
- `docs/IMPLEMENTATION-LOG.md`
- `HANDOFF.md`

### Verification
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider`:
  **444 passed, 51 warnings in 52.41s**.
- Added coverage for: removal of staging, owned destination, and prefix;
  already-missing files still removing the session; session-only removal keeping
  every file and its result text; the worker deferring the session delete to the
  caller (thread-bound SQLite); a missing ISO not blocking removal; external
  destinations retained and untouched; a running installer refused; mandatory
  authorization; a deletion failure keeping the session; other sessions
  untouched; result-text reporting of missing files; and a GTK smoke test for the
  confirmation text, its two removal choices, result popup, list refresh, and
  `removed` signal.
- No real game installation was performed. No user database, prefix, game file,
  or configuration was touched.
- No staging, commit, amend, stash, reset, rebase, checkout, or branch change was
  performed.

### Remaining Issues
- Removal is synchronous per resource and continues after individual failures;
  a partial removal keeps the session, so a retry re-runs the same checks.
- A Wine prefix that another library entry references by an unusual stored path
  would not be removed here, because only the stable `<prefixes>/<game-id>` path
  is considered managed.
- Steps 1–9 remain uncommitted.

---

## 5. Entry format for future steps

Use this format for every future entry:

```markdown
## Step N — Title

**Status:** Planned / In Progress / Completed

### Changes
- ...

### Files Changed
- ...

### Verification
- ...

### Remaining Issues
- ...
```

Rules:

- Do not claim a step is complete unless its acceptance criteria and tests pass.
- Record exact commands and results for verification.
- Record uncommitted state accurately.
- Link to commits only after they exist.
- Do not turn recommendations into accepted decisions without user approval.

---

## 6. Wine prefix stabilization (Fixes 1–5)

**Status:** Completed (uncommitted). Stabilization only; no feature added.

### Changes
- **Fix 1 — atomic migrations.** Each schema version runs in one explicit
  `BEGIN IMMEDIATE` transaction with its DDL, data change, and
  `PRAGMA user_version`. Any failure rolls the whole version back. Python's
  legacy `sqlite3` mode does not open transactions for DDL, which is why the
  transaction is explicit. No silent repair or quarantine was added.
- **Fix 2 — prefix readiness marker.** A prefix is ready only when
  `.minigame-launcher-prefix`, `system.reg`, and `drive_c` all exist. The marker
  is written only after `wineboot` exits 0 and the prefix validates. A bare
  `system.reg` left by a failed or timed-out `wineboot` is no longer trusted.
- **Fix 3 — inter-process shared-prefix lock.** New `services/prefix_lock.py`.
  A process-local `threading.Lock` plus `fcntl.flock` on
  `<prefixes>/.shared-prefix.lock`, outside `.shared`. Wait is bounded at 300 s
  with an actionable error. The kernel releases `flock` on process exit.
  Per-game prefixes are not locked.
- **Fix 4 — shared-prefix lifecycle synchronization.** `prefix_lock.py` now has
  two locks, each a stable lock file outside `.shared` plus a process-local
  `threading.Lock`:
  - `shared_prefix_lock` (initialization, `.shared-prefix.lock`, 300 s): held
    through `wineboot`.
  - `shared_prefix_lifecycle_lock` (references, `.shared-prefix.lifecycle.lock`,
    10 s, overridable by `LIFECYCLE_LOCK_TIMEOUT`): held only briefly while a
    shared reference is persisted and while deletion checks references and
    deletes. Never held across `wineboot`.
  - **Lock order:** lifecycle, then initialization. Deletion takes initialization
    with `timeout=0` (try-lock) and reports busy instead of waiting. Launch takes
    only initialization; assignment takes only lifecycle. No path waits on
    lifecycle while holding initialization.
  - **Transaction boundaries:** each guarded write holds lifecycle around its
    decision and one `Database` write. No database lock spans filesystem work.
    Guarded writes: `GameManager.add_game`, `update_game`, `register_installation`
    (via `_lifecycle_guard` / `shared_reference_guard`), `InstallerManager.create_session`
    (duplicate check moved under the lock), and `save_session` when
    `wine_prefix_shared`. Per-game writes pass through unlocked.
  - `CleanupService.delete_target` for `SHARED_PREFIX` holds lifecycle, then
    initialization (`timeout=0`), then `_delete_checked` (revalidate and delete).
  - Tests: `tests/test_shared_lifecycle_concurrency.py` (7 tests) and the
    busy-lock test in `tests/test_wine_prefix_strategy.py`.
  - **Stale-assignment rejection (added after Fix 4).** `prefix_lock` yields
    `contended` from `shared_prefix_lifecycle_lock` (True when the caller found
    the lock busy and waited). `shared_reference_guard(..., new_reference=True)`
    raises `SharedPrefixUnavailableError` when `contended` is True and `.shared`
    is missing, before the caller's database write. The guard never creates
    `.shared`. A missing `.shared` without contention is a normal first
    assignment. `new_reference=False` (re-saving an already-persisted reference
    in `save_session` or the session branch of `register_installation`) holds the
    lock but is not refused. The UI shows the error through the existing
    `LauncherError` handlers in the editor and installer dialogs.
  - Tests: `tests/test_shared_assignment_rejection.py` (deterministic contention
    via an observed lock; covers add, session, update, registration, re-save,
    per-game, and the UI-facing message). The earlier
    `test_deletion_holding_lock_makes_assignment_wait_then_see_no_shared_prefix`
    now expects rejection; it previously asserted a dangling reference was
    persisted.
  - SQLite connections are thread-bound. The concurrency tests give each worker
    thread its own `Database`. The app runs cleanup and writes on the GTK thread.
- **Fix 5 — installer session coherence.** `FAILED` joins
  `PROTECTING_SESSION_STATUSES`, because failed sessions remain resumable and
  their staging must not be treated as orphaned. `installer_manager` now reuses
  that single set. `_run_installer` persists `INSTALLING` before `launch()` and
  restores the previous state if launch is refused or raises. The pending
  installs list shows explicit text for failed sessions.

### Legacy and compatibility
- Earlier per-game and shared prefixes that have `system.reg` but no marker get
  one idempotent `wineboot -u` on next launch. Nothing is deleted; user files
  are preserved (covered by a test).
- External prefixes are never modified; the readiness check is read-only for them.

### Files Changed
- `src/minimal_game_launcher/database.py`
- `src/minimal_game_launcher/services/launch_manager.py`
- `src/minimal_game_launcher/services/prefix_lock.py` (new)
- `src/minimal_game_launcher/services/cleanup.py`
- `src/minimal_game_launcher/services/installer_manager.py`
- `src/minimal_game_launcher/ui/installer_dialog.py`
- `tests/test_database_migration.py` (new), `tests/test_prefix_lock.py` (new),
  `tests/test_installer_protection.py` (new)
- `tests/test_launch_manager.py`, `tests/test_wine_prefix_strategy.py`,
  `tests/test_diagnostics.py`, `tests/test_performance_manager.py`,
  `tests/test_installer_manager.py` (fakes now emulate a complete prefix)

### Verification
- Baseline before edits: 471 passed.
- Focused: `tests/test_database_migration.py`, `test_wine_prefix_strategy.py`,
  `test_database_recovery.py` → 39 passed.
- Focused after Fix 2–5: 137 + 61 + 17 passed.
- Full suite before Fix 4 lifecycle work: **488 passed in 59.75s**.
- Full suite after Fix 4 lifecycle work: **495 passed in 60.48s**.
- Full suite after the stale-assignment rejection: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src dbus-run-session -- pytest -q -p no:cacheprovider -W ignore` → **506 passed in 60.35s**. GTK smoke rc=0.
- GTK smoke (`/tmp/opencode/fix_gtk_smoke.py`, isolated temp dirs and XDG):
  builds `InstallerDialog` and `PendingInstallsDialog` with a failed session,
  checks the listing and confirmation text → rc=0. Desktop portal and AT-SPI
  warnings came from the host session, not the app.
- Migration rollback method: a monkeypatched v6 step whose second statement is
  invalid, after a valid `ALTER`. Verified rollback leaves no `wine_prefix_shared`
  column and `user_version` 5, then a retry with the real migration succeeds.
- Concurrency method: real subprocesses for cross-process serialization, a hard
  `os._exit` while holding the lock for crash release, and threads for
  in-process serialization. No real Wine was run; all Wine calls used fakes.

### Remaining Issues
- Lock wait is 300 s; a very long `wineboot` could exceed it and surface the
  busy error. Lock file is not removed after use (intentional; avoids races).
- Assignment writes take the lifecycle lock, so a shared reference cannot be
  persisted while deletion holds it. The lock is process-local plus `flock`, and
  cross-process assignment is not tested. Only the in-process and init-lock
  cross-process paths have tests.
- A shared reference persisted without contention after deletion still points at
  a missing prefix; the next launch recreates it. Only a contended assignment is
  rejected. A non-contended assignment that runs after deletion is a normal
  first assignment and is not blocked.
- Lifecycle wait is 10 s. A long DB write on another thread could exceed it and
  surface the busy error to the user.
- Lock files are not removed after use (intentional). `flock` is untested on
  network filesystems.
- `wineboot` is still not isolated from a Wine process started outside MGL.
- Steps 1–9 and Fixes 1–5 remain uncommitted. No Git mutation was performed.

---

## Step 10 — Direct `.exe` installers and desktop launcher integration

**Status:** implemented and uncommitted, 2026-11-06. No Git mutation.
**Schema:** v7 (`installer_sessions.source_kind`, default `iso`).

### Standalone Windows installers
- `InstallerSourceKind` (`iso`, `executable`) and
  `InstallerSession.source_kind`; `iso_path` remains the column/field name for
  compatibility and `source_path` is the documented accessor.
- `InstallerManager.create_executable_session()` validates a regular,
  non-empty, readable `.exe` with an `MZ` signature, resolves symlinks to the
  target, and refuses sources inside MGL-owned staging, game, or prefix roots.
  No MSI support.
- `prepare()` branches: ISO sessions keep mount → copy → verify → unmount;
  executable sessions revalidate, copy only the selected file, detect source
  changes during the copy, write the same manifest shape with
  `source_kind`/`source_path`, and verify the copy. Direct mode never mounts or
  invokes UDisks.
- Direct discovery returns only the user-selected staged executable without ISO
  name heuristics. It is preselected in the dialog but never executed
  automatically. The original source never enters an ownership deletion.
- `InstallerDialog` and `PendingInstallsDialog` use source-aware titles,
  filters, safety notes, staging labels, resume text, and preserved-source
  labels. The header install button is now a menu: **From ISO image…** and
  **From installer executable…**.

### Desktop launcher entry
- New `services/desktop_integration.py` writes one entry and icon named after
  `APP_ID` under `$XDG_DATA_HOME/applications` and
  `$XDG_DATA_HOME/icons/hicolor/scalable/apps`.
- Launch detection order: `APPIMAGE`, `APPDIR/AppRun`, installed
  `minimal-game-launcher`, wrapper beside the program, then
  `env PYTHONPATH=<absolute> <python> -m minimal_game_launcher`. `Exec=`
  quoting escapes `\`, `"`, `` ` ``, `$`, and `%`; no shell is used.
- Writes are atomic (same-directory temp file plus `os.replace`, mode `0644`).
  Repeated enable does not rewrite an identical entry.
- `X-MGL-Managed=true` is the ownership marker. Unowned canonical files are
  never deleted or overwritten. Disable removes only an owned entry and leaves
  the icon. With a same-ID system entry, disable writes a user-level
  `Hidden=true` override; enable replaces it. System directories, Fuzzel, and
  Hyprland configuration are never touched.
- `update-desktop-database` is best-effort (`argv`, `shell=False`, timeout).
- `LauncherSettings.desktop_entry_enabled: bool | None = None`; invalid or
  missing values load as `None`. Startup reconciles only explicit values.
- `SettingsDialog` gained the switch with read-only failure reporting and
  desktop-state rollback when settings persistence fails. The service remains
  optional for existing callers.
- Packaging template and AppDir generation carry the marker and produce only
  the canonical desktop ID.

### Files Changed
- `src/minimal_game_launcher/models.py`, `database.py`, `paths.py`,
  `settings.py`, `errors.py`, `application.py`
- `src/minimal_game_launcher/services/installer_manager.py`
- `src/minimal_game_launcher/services/desktop_integration.py` (new)
- `src/minimal_game_launcher/ui/installer_dialog.py`,
  `ui/main_window.py`, `ui/settings_dialog.py`
- `packaging/io.github.minimal_game_launcher.MinimalGameLauncher.desktop`
- `scripts/build-appdir.sh`
- `tests/test_direct_installer.py` (new), `tests/test_desktop_integration.py`
  (new), `tests/test_database_migration.py`, `tests/test_installer_manager.py`,
  `tests/test_packaging.py`, `tests/test_theme_ui.py`,
  `tests/test_wine_prefix_strategy.py`
- `README.md`, `docs/PACKAGING.md`, `docs/FEATURE-ROADMAP.md`, `HANDOFF.md`

### Verification
- Baseline before edits: **531 passed**, 51 warnings.
- Focused direct installer: 22 passed. Focused desktop integration: 21 passed.
- Focused installer/ISO/migration/settings/packaging/GTK: 68 + 38 passed.
- Full suite:
  `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider` →
  **578 passed, 51 warnings in 60.24 s**, exit 0, no skips.
- GTK smoke with `dbus-run-session`: builds a direct-session `InstallerDialog`,
  `PendingInstallsDialog`, and the settings switch against temporary
  directories → rc=0.
- No Wine, real prefixes, sessions, library data, or user desktop entries were
  touched; tests use temporary directories and injected services.

### Remaining Issues
- Only standalone `.exe` installers are supported; MSI and installers that need
  companion `.cab`/`.bin`/data files must use an ISO.
- The `MZ` check is a format check, not a safety or compatibility guarantee.
- The user-level icon is left in place on disable; ownership of a pre-existing
  icon is not claimed.
- A stale unowned file at the canonical desktop path blocks enabling with an
  explicit error until the user moves it.
- Version remains `0.5.0`; the repository's D-10 version decision is still
  unresolved.

---

## Step 11 — Memory management fixes (cards, dialogs, cover cache, image limits)

**Status:** implemented and uncommitted, 2026-11-06. No Git mutation, no new
dependency, no version bump.

### Root causes
- **GameCard lifecycle.** Every `_refresh_library()` rebuilt all cards and
  dropped the old ones from the `FlowBox`, but each card's buttons held
  PyGObject signal handlers that referenced the card through bound methods.
  The cycle `card -> child button -> handler -> card` is not visible to
  Python's collector, so cards accumulated on every refresh. A 120-game
  library reached 840 live cards after six refreshes.
- **Dialog lifecycle.** The same cycle existed in every dialog: closing a
  dialog never disconnected handlers owned by its child widgets, and the
  window also kept `_active_*` references after cancel/close. Repeated
  open/close batches retained ten instances per dialog class.
- **Cover cache.** The cache was bounded by entry count (64) only, so a
  library larger than 64 covers evicted entries that were re-requested on the
  next rebuild. `CoverCacheStats` showed `hits=0` and `evictions=96` at startup
  for a 160-cover library.
- **Image decode.** Validation decoded every image at full resolution before
  checking anything, and the card path had no dimension check at all. A
  7000×7000 PNG (0.67 MiB on disk, 147 MB decoded) peaked the process at
  ~203 MiB.

### Ownership design
- New `ui/lifecycle.py`:
  - `SignalOwner.connect_owned(instance, signal, handler, *args)` records
    `(instance, handler_id)`; `disconnect_owned_signals()` disconnects them
    idempotently and tolerates already-collected instances or handlers.
  - `DialogSignalOwner` connects the dialog's own `closed` signal through
    `connect_owned`, then disconnects every owned handler and calls the
    `_release_dialog_references()` hook. Subclass hooks must be safe to call
    twice.
- `GameCard` connects child handlers through `connect_owned` and exposes an
  idempotent `dispose()` that clears `_launch_button`, `_launch_menu_item`,
  and `_running_pill`; `set_status()` is a no-op after disposal.
- `MainWindow._refresh_library()` disposes old cards before
  `flowbox.remove_all()`, prunes dead cache entries, and reuses the existing
  cards when the returned game list is unchanged (`_refresh_card_statuses`),
  so an unchanged refresh performs no decode at all.
- Every cleared `_active_*` reference is now guarded by
  `if self._active_x is dialog`, so an older dialog cannot clear a newer one.
  Details, cover, editor, cleanup, and orphan dialogs gained `closed`
  handlers; settings, diagnostics, installer, and pending handlers were
  guarded the same way.

### Cover cache policy
- `DEFAULT_CACHE_BUDGET_BYTES = 64 MiB`, with `DEFAULT_CACHE_LIMIT = 128`
  entries as a secondary guard for very small textures. A card texture is
  368×492 RGBA ≈ 0.69 MiB, so the budget holds roughly 90 cards.
- Accounting is by decoded bytes (`texture_nbytes()` = width × height × 4),
  updated on insert, replacement, eviction, invalidation, and pruning.
- Eviction drops the cache's reference only. A visible `Gtk.Picture` keeps its
  own reference, so eviction never blanks an on-screen card; it only means the
  next request for the same `(path, width, height)` decodes again.
- `prune_dead()` runs on each library refresh and drops entries whose file is
  gone or whose device/inode/size/mtime changed.
- `CoverCacheStats` gained `prunes`, `entries`, and `bytes_cached`; existing
  fields are unchanged.

### Image limits
- `services/cover_manager.py` reads the format and dimensions from the image
  header before any pixel allocation. `GdkPixbuf.Pixbuf.get_file_info()` needs
  a path, so in-memory bytes are exposed through an anonymous
  `os.memfd_create` file (with a named-temporary-file fallback).
- Limits: `MAX_COVER_BYTES` 10 MiB, `MIN_COVER_DIMENSION` 16,
  `MAX_COVER_DIMENSION` 10 000, `MAX_COVER_PIXELS` 12 000 000 (48 MiB RGBA).
  A valid image is still decoded once to prove it is complete, at
  `PREVIEW_MAX_DIMENSION` 512 on the long side with aspect ratio preserved.
- `ui/cover.py` applies the same dimension/pixel guard in the texture cache
  and in `load_cover_texture_from_bytes`, so legacy or externally edited cover
  paths cannot trigger a full-resolution decode. Such covers render as the
  placeholder.
- Honest limitation: GdkPixbuf's PNG loader allocates the full pixel buffer
  even when `PixbufLoader.set_size()` is used (measured 147 MB for a
  7000×7000 PNG). The protection for PNG is therefore *rejection before
  decode*; the scale-while-decoding win applies to formats whose loader
  supports it (for example JPEG).

### Files Changed
- `src/minimal_game_launcher/ui/lifecycle.py` (new)
- `src/minimal_game_launcher/ui/game_card.py`, `ui/main_window.py`,
  `ui/cover.py`
- `src/minimal_game_launcher/ui/game_details_dialog.py`,
  `ui/game_editor_dialog.py`, `ui/settings_dialog.py`,
  `ui/cover_selection_dialog.py`, `ui/diagnostics_dialog.py`,
  `ui/installer_dialog.py`, `ui/cleanup_dialog.py`
- `src/minimal_game_launcher/services/cover_manager.py`
- `tests/test_lifecycle.py` (new), `tests/test_cover_cache.py`,
  `tests/test_cover_manager.py`
- `docs/IMPLEMENTATION-LOG.md`, `HANDOFF.md`

### Verification
- Baseline before edits: **578 passed**, 51 warnings in 60.27 s.
- After edits, full suite:
  `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests`
  → **604 passed, 51 warnings in 62.09 s**, exit 0. 26 new tests.
- Retention tests assert collection with `weakref` + `gc.collect()`, not just
  successful calls: card GC after `dispose()`, 3 × 120-card rebuilds with zero
  survivors, dialog GC after `closed`, and details-dialog GC.
- Controlled A/B under the same headless session (fresh process per mode):
  - Cards, 6 × 120 rebuilds: old policy **720 live cards, RSS +94.3 MiB**;
    new policy **0 live cards, RSS +15.8 MiB**.
  - 160 covers at 368×492: old `limit=64` pinned 64 entries / 39.4 MiB with
    96 evictions per pass; new budget holds 103 entries / 63.4 MiB with 57
    evictions. Both performed 160 loads; the decisive change is that an
    unchanged library no longer rebuilds cards at all.
  - 7000×7000 PNG: old validation accepted it and the card load peaked at
    **203.1 MiB**; new validation rejects it in 0.135 s with **+5.3 MiB**, and
    the texture loader returns `None` with no measurable growth.
- Full-application workloads (headless Weston, `GDK_BACKEND=wayland`):
  - `lib160` (160 games, covers): baseline cards 160→320→480→640 and
    `loads=160→640, hits=0, evictions=96→576`; after **cards=160,
    loads=160, cache=103 entries / 63.4 MiB** for all six refreshes, RSS
    flat. `prunes=0`.
  - `lib120nc` (120 games, no covers): baseline cards 120→840,
    RSS 112.4→224.1 MiB; after **cards=120** and flat RSS for all refreshes.
  - `lib20b`: six refreshes kept `cards=20, loads=20, cache=20`; details,
    editor, settings, cover, and diagnostics each reported
    `RETAINED ... value=none` after closing.
  - Dialog batches (`Settings`, `Details`, `Editor`, `Cover`, `Pending`,
    `Diagnostics`): every batch start and the final report show **0 live
    instances**; baseline retained 10 per class. A 120-dialog settings loop
    kept Python objects and widgets flat (35 875 objects / 188 widgets at both
    the start and the end). The residual RSS rise there is in GTK/GSK
    C-side caches: a bare `Adw.Dialog` control plateaus at +12 MiB, while the
    content-heavy settings dialog levels off at roughly +0.2 MiB/dialog after
    the first 30.
  - Theme switching (40 switches) and the 5-cover library: flat.
- **Measurement limitation.** Absolute RSS in this headless session is not
  comparable with the numbers recorded before this step: an empty library now
  starts at ~348 MiB versus 108.6 MiB then, with ~95 MiB of libLLVM, ~30 MiB
  of libgallium, ~14 MiB of libvulkan_intel, and a ~70 MiB heap. The shift is
  in the Mesa/LLVM llvmpipe path, not in MGL objects, and it is present with
  zero cards and zero covers. The before/after claims above therefore use
  object counts, cache counters, and within-session deltas; they are not
  presented as real Hyprland-session numbers.

### Reproducing the memory checks
- Object retention (no display needed for the synthetic cases, display needed
  for real widgets):
  `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_lifecycle.py tests/test_cover_cache.py tests/test_cover_manager.py`
- Headless full-application run: start a Weston headless compositor on a
  private socket, point `HOME`/`XDG_*` at a scratch directory, then run the
  application with `WAYLAND_DISPLAY=<socket> GDK_BACKEND=wayland
  NO_AT_BRIDGE=1`. Sample `/proc/<pid>/status` (`VmRSS`, `VmHWM`) and
  `/proc/<pid>/smaps_rollup` once per second while repeatedly calling
  `MainWindow._refresh_library()`.
- The scratch harness used for these numbers is under
  `/tmp/opencode/mgl-audit/` (`launch_measure.sh`, `audit_refreshstats.py`,
  `audit_dialogs.py`, `audit_after_ab.py`) and is not part of the repository.
  It must be recreated for a future measurement.

### Remaining Issues
- GdkPixbuf cannot scale PNG while decoding, so an accepted 12 MP PNG still
  allocates its full ~48 MiB buffer during validation. Oversized files are
  rejected before that allocation.
- The cache decodes on the GTK main thread; a library rebuild larger than the
  budget still costs one decode per requested cover. The budget bounds
  retention, not per-refresh work.
- Residual RSS growth from repeated dialog creation lives in GTK/GSK caches,
  not in retained Python objects; it is not addressed here.
- Absolute memory numbers from a headless llvmpipe session understate or
  overstate a real Hyprland/GPU session depending on the driver; no real
  session was measured.
- Steps 1–10 and this step remain uncommitted. No Git mutation was performed.
