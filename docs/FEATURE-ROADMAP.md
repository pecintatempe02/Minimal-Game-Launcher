# Minimal Game Launcher — Feature Roadmap & Architecture Specification

**Status:** Durable specification for the phases after the 0.5.0 baseline.  
**Last updated:** 2026-11-07
**Implementation state:** Steps 1–12 are implemented on the existing
application architecture. Steps 1–11 are committed at HEAD `072ee85`. Step 12
adds the v0.6.0 feature set: per-game library visibility, Wine runtime
diagnostics, per-game Winecfg and Winetricks, full display/gamescope controls,
and a first-run setup wizard.

This document records requirements and proposed architecture. Recommendations
are explicitly marked **Proposed — not accepted**. Open decisions are collected
in §8 and must be resolved with the user before implementation. D-1 (read-only
mount), D-4 (command launch model), D-5 (destination ownership), D-6
(pending-installs dialog), and D-9 (cleanup while running) are accepted product
decisions; D-1, D-5, D-6, and D-9 are implemented in Steps 4–5. Step 2
provisionally implements the D-7 and D-8 recommendations; those decisions are
not yet marked accepted.

---

## 1. Baseline and principles

### 1.1 Verified baseline

- Application version: `0.5.0` (v0.6.0 feature work is implemented but the
  version is bumped only during release preparation)
- HEAD: `072ee85` (`Preparing for Releases`)
- Library schema: v9 in the working tree, `games` plus `installer_sessions`
  (v8 at HEAD adds `games.gamescope_resolution`; v9 adds visibility, display
  mode, virtual desktop, and full gamescope controls. Earlier steps added
  `environment`, install/source paths, ownership flags, installer sessions, and
  `installer_sessions.source_kind`.)
- Existing launch paths: Linux native/command and Windows through per-game Wine
  prefixes
- Existing removal behavior: metadata-only by default; Step 5 adds explicit
  verified managed-data cleanup while preserving files by default
- Existing diagnostics: rotating log, short user-facing launch errors,
  on-demand read-only diagnostics (Step 2), and on-demand cleanup previews
- Existing tests: **604 passed** on 2026-11-06 (full suite, no skips)

### 1.2 Cross-cutting principles

- Keep the application small, local, and user-level; no root for normal work.
- No background daemon or continuous monitoring.
- No shell execution. All child processes use argument lists and
  `shell=False`.
- Never silently install dependencies, DXVK, runtimes, GameMode, or MangoHud.
- Never modify global Wine configuration or an external Wine prefix.
- Preserve existing behavior unless a change is explicitly requested.
- Default to non-destructive actions; destructive actions require explicit
  confirmation and verified ownership.
- All SQL remains in `database.py`.
- All GTK widgets remain in `ui/`.
- Services own business logic; UI owns presentation.
- Every module keeps its docstring and error handling conventions.

---

## 2. Feature A — Command / External Application Launching

### 2.1 Goal

Allow a library entry to launch an external application or command, for
example:

```sh
flatpak run org.vinegarhq.Sober
```

The launcher must not use a shell to interpret this.

### 2.2 Requirements

- Support an external executable plus arguments.
- Support an optional working directory.
- Support optional per-game environment variables.
- Resolve bare command names available through `PATH`.
- Preserve argument boundaries by passing an argv list.
- Never use `shell=True`.
- Preserve existing Linux native and Windows/Wine launch behavior.
- Preserve existing GameMode/MangoHud semantics unless a later decision says
  otherwise.
- Reuse the existing process lifecycle, signals, pidfiles, and error handling.
- Keep command names and arguments separate from environment values.
- Validate environment keys and values; reject NULs and malformed data.
- Do not allow user environment values to override launcher-managed variables
  such as `WINEPREFIX` or the MangoHud toggle.

### 2.3 Implemented state (Step 1)

- `Platform` remains `linux` / `windows`; no command platform was added.
- Linux entries accept either an executable path or a bare command name.
- Bare commands are resolved with `shutil.which` at validation and launch time.
  The saved value stays exactly as typed and is never rewritten to a resolved
  path. Path values keep the existing resolved-absolute storage behavior.
- Arguments remain a separate field parsed with `shlex.split` and passed as an
  argv list with `shell=False`.
- The game editor exposes an editable executable row and retains the file
  picker as a convenience button.
- The editor accepts a multiline `KEY=VALUE` environment block. Keys and values
  are validated, reserved keys are rejected, and the mapping is stored as JSON
  in the `games.environment` column.
- Environment precedence is: inherited environment → per-game environment →
  platform-managed values → performance-managed values. `WINEPREFIX` and
  `MANGOHUD` remain launcher-managed.
- Empty working-directory values keep the existing parent-directory default for
  executable paths. A bare command starts in the user's home directory unless a
  working directory is configured.
- Environment values are never logged and never included in error messages.

### 2.4 Accepted data model (D-4)

D-4 is resolved: external commands use the existing Linux platform. There is no
new platform enum, category, or launch subsystem. The existing Native/Linux
`LaunchManager` path is the single Linux launch path.

A separate `Platform.COMMAND` was rejected because it would require rebuilding
the SQLite `games` table for the CHECK constraint and would duplicate existing
launch, performance, and lifecycle behavior without a product benefit.

### 2.5 Acceptance outline

- `flatpak run org.vinegarhq.Sober` launches as an argv list containing the
  resolved `flatpak` executable, `run`, and `org.vinegarhq.Sober`.
- Quoted arguments remain individual argv elements.
- Working directory and environment are applied and persist across edits.
- Existing native/Wine launch behavior and tests remain green.
- No shell is used at any point.

---

## 3. Feature B — On-Demand Diagnostics

### 3.1 Goal

Provide a user-triggered diagnostic report that explains why a game or prefix
may not work. Diagnostics must be read-only and local.

### 3.2 Requirements

- No background daemon, timer, or continuous monitoring.
- Run only when the user requests it or from an appropriate launch-failure
  action.
- Inspect:
  - Wine availability and version;
  - prefix existence, readiness, and integrity;
  - DXVK presence and configuration hints;
  - Vulkan availability;
  - executable and working-directory validity;
  - relevant launch failures and process exit information.
- Provide user-readable summaries and technical details.
- Support exporting a diagnostic report.
- Redact sensitive environment data, tokens, and API keys.
- Distinguish MGL-managed resources from external resources.
- Never silently install dependencies or modify system configuration.
- Never run `wineboot`, repair a prefix, change registry entries, or write to
  Wine configuration during diagnostics.
- Run slow checks off the GTK main thread with bounded timeouts.

### 3.3 Implemented check set (Step 2)

| Check | Data source | Safety |
|---|---|---|
| Wine availability/version | settings, `PATH`, `wine --version` | Subprocess with timeout; no prefix writes |
| Prefix state | path, directory access, `system.reg`, `drive_c`, marker | Filesystem reads only |
| Managed vs external prefix | `classify_prefix` path comparison | Path classification only; links are external |
| DXVK presence | `system32`/`syswow64` DLLs, `dxvk.conf`, `user.reg` override hints | Read-only, bounded file read |
| Vulkan | `vulkaninfo --summary`, loader library lookup | Subprocess with timeout; no installation |
| Executable | configured path or `PATH` command | Read-only existence and permission checks |
| Working directory | configured path or launch default | Read-only existence and permission checks |
| Last launch | pidfile identity and process verification | Read-only `/proc` reads; never signals |
| Environment | per-game variable names only | Values are never read into the report |
| Launcher log | path, size, readability | Contents excluded because they may contain arguments |
| Free space | data filesystem usage | Read-only |

The report header also records the launcher version, game ID, platform, and
generation time. Checks run on a worker thread; each failed check becomes an
`unknown` section rather than aborting the report. Exit codes are not persisted
elsewhere, so the last-launch check reports only current pidfile state and not a
historical exit status.

### 3.4 Report and redaction

- Report is plain text and/or structured, generated locally.
- Never include API keys, key-file contents, settings JSON contents, full
  environment values, registry dumps, or game launch arguments.
- Environment keys may be listed without values.
- Home-directory paths are replaced with `~` by default.
- The exact path-redaction policy is an open decision (D-7). Step 2 implements
  the recommendation provisionally: reports use `~` by default and the dialog
  offers an explicit "Show full paths in report" switch that regenerates the
  report.
- Export must not trigger network access.
- Exported reports are plain-text `.txt` files written to a user-selected
  local path. The launcher log contents are deliberately not embedded.

### 3.5 Non-goals

- Automatic repair.
- Automatic dependency installation.
- Background health monitoring.
- Game process termination.
- Uploading reports.

### 3.6 Implemented state (Step 2)

- `services/diagnostics.py` owns all checks and report generation. It never
  writes to a prefix, runs `wineboot`, installs anything, or starts a game.
- Wine and `vulkaninfo` probes use argument lists, `shell=False`, captured
  output, and bounded timeouts.
- Managed prefixes are exactly `<prefixes-root>/<game-id>`, classified through
  the shared `OwnershipService`. Everything else is external or invalid and is
  never modified; symlinked prefixes are `invalid`, not managed.
- DXVK reporting is presence-only with caveated `user.reg` override hints;
  exact DXVK versions are not claimed.
- The UI is reachable from the game card menu and the game details dialog.
  Results appear as a status summary with collapsible technical details.
- Secrets are excluded by construction: environment values, settings file
  contents, and the SteamGridDB key are never read into the report.

---

## 4. Feature C — Ownership and Data-Management Foundation

### 4.1 Goal

Make destructive operations safe and predictable before ISO installation and
cleanup are implemented. Ownership must be explicit, verifiable, and based on
stable identifiers.

### 4.2 Requirements

- Associate MGL-managed Wine prefixes with stable game IDs.
- Explicitly distinguish MGL-owned paths from externally managed paths.
- Validate game/session IDs before using them in filesystem paths.
- Protect against `..`, absolute-path injection, symlinks, hardlinks, and
  path traversal.
- Provide safe size-estimation primitives that do not follow symlinks.
- Provide safe deletion primitives that refuse unverified roots.
- Never delete outside verified MGL-owned directories.
- Never delete the managed root directories themselves.
- Plan database migrations before feature implementation.
- Report partial failures and remaining data.

### 4.3 Implemented ownership model (Step 3)

Ownership is decided by configuration and exact resolved-path equality, never by
a directory name or by living under the home directory. There is exactly one
managed root per resource kind, and a game owns exactly one directory in each:

```text
$XDG_DATA_HOME/minigame-launcher/prefixes/<game-id>     # existing location
$XDG_DATA_HOME/minigame-launcher/games/<game-id>        # new, reserved
$XDG_DATA_HOME/minigame-launcher/installers/<game-id>   # new, reserved
$XDG_DATA_HOME/minigame-launcher/covers/                # unchanged
$XDG_STATE_HOME/minigame-launcher/processes/            # unchanged
```

`paths.py` gained `games_directory()` and `installers_directory()`. Neither is
created in Step 3; the existing prefix, cover, database, and state locations are
unchanged. Covers keep their existing cache-containment rule and are not routed
through the new service.

`services/ownership.py` exposes `OwnershipService` with four operations:

- `classify(kind, game_id, path)` returns an `OwnershipVerdict` with a status of
  `managed`, `external`, `missing`, or `invalid`, plus a human-readable reason.
- `estimate(kind, game_id, path)` returns a `SizeEstimate`.
- `delete(kind, game_id, path, authorized=..., game_running=...)` returns a
  `DeletionResult`.
- `root_for(kind)` and `expected_path(kind, game_id)` expose the configured
  root and the validated game directory.

Classification statuses:

- **managed** — resolved path equals `<root>/<game-id>` and is a real directory.
- **missing** — the expected managed path does not exist yet (e.g. a prefix that
  `wineboot` has not created).
- **external** — the path is outside this kind's managed root.
- **invalid** — empty path, parent-directory segment, unsafe game ID, symlink,
  managed root itself, another game's directory under the root, a non-directory,
  or an unreadable path.

Symlinks are never managed, whatever they point to. Rejecting them outright is
deliberate: it avoids both following a link outside the root and accepting a
link inside the root as an alias for owned data.

### 4.4 Database migration (schema v4)

Step 3 adds four columns, all with conservative defaults:

```sql
install_directory            TEXT,
install_directory_owned      INTEGER NOT NULL DEFAULT 0
                             CHECK (install_directory_owned IN (0, 1)),
installer_source_directory   TEXT,
installer_source_owned       INTEGER NOT NULL DEFAULT 0
                             CHECK (installer_source_owned IN (0, 1)),
```

The migration runs every missing step in one transaction and stamps
`SCHEMA_VERSION` only afterwards, so v1→v4, v2→v4, and v3→v4 are repeatable.
Existing rows keep a `NULL` install/source path and `0` owned flags, so no
pre-existing directory becomes launcher-owned. `environment` and
`wine_prefix_path` are preserved. `GameManager.update_game()` preserves the four
fields because the game editor does not edit them.

The owned flags are metadata only. A deletion request is still validated against
the managed root at call time; a flag alone never authorizes deleting a path.
No installer-session or disc tables existed before Step 4. **Step 4 update:**
`SCHEMA_VERSION` is now 5. It adds an `installer_sessions` table and leaves
existing `games` rows unchanged. D-1 is resolved as a read-only mount (§5.4); the
session table stores one ISO per session.

### 4.5 Size estimation and deletion guarantees

Size estimation:

- requires a verified managed resource and otherwise returns `complete=False`;
- walks through directory file descriptors opened with `O_NOFOLLOW`, `dir_fd`,
  and a depth cap;
- counts symlink entries as links and never traverses them;
- counts hardlinked regular files once by `(st_dev, st_ino)`;
- returns partial values with `complete=False` and an error list on any
  inaccessible entry rather than presenting an incomplete total as exact;
- measures apparent file size, so sparse files and block allocation are not
  distinguished.

Deletion:

- requires `authorized=True` from the calling layer; no UI calls it in Step 3;
- refuses while `game_running=True`;
- re-classifies immediately before opening and re-checks the directory identity
  `(st_dev, st_ino)` immediately before `rmdir`;
- refuses managed roots, mismatched game IDs, external paths, symlinks, and the
  root itself;
- unlinks and rmdirs through `dir_fd` and never traverses a symlink, so a link
  inside the tree is removed as a link, not followed;
- reports partial failures in `DeletionResult.errors` and never falls back to a
  broader recursive delete.

Documented limitation: no user-space primitive can fully serialise against an
attacker replacing paths between syscalls. The implementation narrows the window
and records a failure if the leaf directory changes, but it does not claim
protection against every concurrent filesystem race. Ownership of the configured
roots follows the launcher's own configuration: a user-created symlinked data
root is resolved once and treated as that root.

### 4.6 Diagnostics integration

`DiagnosticsService` now depends on the shared `OwnershipService` for prefix
classification. Diagnostics remains read-only, still runs only on explicit
request, and keeps every existing check. The prefix check now reports the
ownership reason and treats `invalid` as a problem, `missing` as a warning,
`external` as read-only context, and never infers ownership from a path name.
`classify_prefix` and `PrefixOwnership` were removed; callers use
`OwnershipService.classify()`.

### 4.7 Acceptance status

- External and symlinked paths are never classified as managed. **Met.**
- Safe deletion refuses roots, mismatched names, and escaping paths. **Met.**
- Size estimation does not follow symlinks. **Met.**
- Existing databases migrate without losing rows, environment, or prefixes.
  **Met** (v1→v4, v2→v4, v3→v4 tests).
- No cleanup or installer feature is implemented by the foundation. **Confirmed
  non-goal.**

Cleanup UI and orphan detection are implemented in Step 5 on top of this
foundation. Automatic removal, multi-disc handling, DXVK management, and prefix
migration remain unimplemented and are still governed by the open decisions in
§8. ISO mounting, complete staging/verification, installer discovery,
defer/resume, and installer execution are implemented in Step 4; schema v5 adds
the `installer_sessions` table (v4→v5 migration tested). Step 5 required no new
schema migration.

---

## 5. Feature D — ISO Game Installer

### 5.1 Goal

Install a game from an ISO image through a dedicated Wine prefix while keeping
the original image and installer sources separate from the final installation.

### 5.2 Intended workflow

1. Select an ISO file.
2. Access its contents safely.
3. Scan for possible installers and game executables.
4. Let the user select an installer.
5. Let the user select an installation destination.
6. Preserve the complete installer source contents when required, not just
   `setup.exe`.
7. Create or associate a Wine prefix with the game's stable ID.
8. Run the installer through Wine.
9. Allow installation to be deferred.
10. Discover likely game executables after installation and let the user select
    one, with manual selection as the fallback.
11. Register the game in the library with the selected executable and its
    working directory.
12. Let the user decide whether to keep or remove installer source files.

**Step 4 status:** steps 1–11 are implemented by the installer workflow. Step 8
adds ranked, read-only executable discovery for step 10 and confirms that
registration uses the existing `Game`/`LaunchManager` architecture. Step 12 is
implemented separately by the Step 5 cleanup dialog: installer sources are
preserved by default and removed only when the user explicitly selects the
verified managed staging directory. The original ISO is never deleted.

### 5.3 Required constraints

- Never delete the original ISO automatically.
- Some games may require the original disc or mounted image at runtime.
- Do not automatically install DXVK or other dependencies.
- Do not silently modify external Wine prefixes.
- Copy full contents when installer data or supporting files are needed.
- Keep installer source files separate from the final game installation.
- Do not run as root.
- Treat ISO contents as untrusted.
- Reject or sanitize path traversal, absolute paths, escaping symlinks,
  hardlinks, device nodes, and special files.
- Check free space before copying.
- Verify the copy before running the installer.
- Use an argument-list Wine invocation with the correct `WINEPREFIX`.
- Do not auto-run the first discovered executable.
- Do not auto-register the first discovered `.exe` as the game.

### 5.4 Implemented architecture (Step 4)

- `services/iso_mount.py` — unprivileged read-only mount through `udisksctl`:
  - `udisksctl loop-setup --no-user-interaction --read-only --no-partition-scan --file <iso>`
  - `udisksctl mount --no-user-interaction -b <loop> --options ro`
  - `--no-partition-scan` is required for hybrid images. Without it, UDisks
    parses the Apple Partition Map or GPT, exposes child partition objects, and
    withholds the whole-disk `Filesystem` interface, so a valid ISO9660 image is
    rejected as "not a mountable filesystem".
  - verify the loop device reports `ro=1` and that one matching entry exists in
    `/proc/self/mountinfo` before using the mount point;
  - `udisksctl unmount` and `udisksctl loop-delete` only for the mount and loop
    device recorded by the same operation.
  - No root, no privilege escalation, no archive-extraction fallback.
- `services/installer_manager.py` — session staging, verification, installer
  discovery/launch handoff, defer/resume, destination ownership.
- App-owned staging (game ID is the stable directory name):

```text
$XDG_DATA_HOME/minigame-launcher/installers/<game-id>/source/
$XDG_DATA_HOME/minigame-launcher/installers/<game-id>/.mgl-staging.json
$XDG_DATA_HOME/minigame-launcher/prefixes/<game-id>/
$XDG_DATA_HOME/minigame-launcher/games/<game-id>/   # managed destination
```

- The complete ISO tree is copied to `source/`; every regular file is hashed
  (SHA-256) and recorded in the manifest. The copy is verified before the ISO is
  unmounted. Symlinks, special files, unsafe names, and excessive depth/counts
  are rejected. Free space is checked before copying.
- Discovery is name- and content-based (`.exe` names, install-like stems, ELF
  magic) and never executes anything. The user explicitly selects an installer
  and a final game executable.
- The original ISO path is retained as a reference only; no delete path exists.
- Step 3's `OwnershipService` accepts only a validated game ID as the managed
  directory name, so staging and the managed destination both use the stable
  game ID. D-6 is implemented as a dedicated pending-installs dialog.
- External destinations are marked non-owned and are never deleted by MGL.
  Existing non-empty destinations are refused rather than overwritten.
- Destination type is explicit: the installer's default inside the Wine prefix,
  the MGL-managed `<games>/<game-id>` directory, or an external folder. The Wine
  default creates no managed game directory and records no installation path;
  the managed directory is created only when the user selects that destination.
- `services/executable_discovery.py` performs a read-only post-install scan. It
  searches the configured installation directory first, then the prefix's
  `drive_c/Program Files`, `drive_c/Program Files (x86)`, and other non-system
  `drive_c` children. It returns deterministic, ranked `.exe` candidates with a
  location and reason, filters installers/redistributables/updaters/crash
  reporters/helpers, never follows symlinks, and bounds depth, entry count, and
  errors. Nothing is executed and no candidate is auto-registered.
- The installer dialog shows discovered candidates, marks the top candidate as
  recommended only when its score is unambiguous, requires explicit selection,
  keeps a manual file picker, and allows cancellation without touching launch
  configuration. Registration sets the executable's parent as the working
  directory and preserves existing arguments, environment, GameMode, MangoHud,
  cover, and Wine prefix when a library record already exists.
- The pending-installs dialog offers two actions per session: resume, and remove
  behind an explicit confirmation. The confirmation offers **Remove session
  only** (the record is deleted, files stay on disk) and **Remove files and
  session**. File removal deletes only verified managed resources — installer
  staging, the Wine prefix, and the managed destination when it is owned. An
  external destination is retained and reported. An already missing resource
  counts as success, so the session record is still removed; only a real failure
  keeps the record for a retry. The outcome is shown in a result popup listing
  removed, already-missing, retained, and failed resources.
- The installer runs through the existing `LaunchManager` with the game's
  stable WINEPREFIX; the prefix is created lazily by the existing launch path.
  DXVK, Winetricks components, and runtimes are not installed automatically.

### 5.5 Multi-disc considerations

This is unresolved (**D-3**). Possible strategies:

- merge all disc contents into one staging tree and record all ISOs;
- mount one disc at a time and request user action on disc-change prompts;
- declare multi-disc unsupported in the first release.

Merging is simple but may fail for installers that expect a volume label or a
specific drive letter. The first implementation must not claim full multi-disc
support without validation.

**Step 4 status:** one ISO per session. Multi-disc installers are not detected
or merged; a disc-change prompt will simply fail inside the installer. There is
no disc table or multi-ISO session model.

### 5.5a UDF support (unresolved, D-2)

The mount path relies on the kernel and UDisks filesystem support. ISO9660,
Rock Ridge, and Joliet images work through the normal mount helper. Step 7
verified one hybrid ISO9660 + Apple Partition Map image by disabling partition
scanning on the read-only loop device, so UDisks exposes the whole-disk ISO9660
filesystem instead of only the APM children. This is one image on one host;
general hybrid layout support is not claimed. UDF-only images were not exercised
on this host and are neither claimed supported nor explicitly rejected; a mount
failure surfaces as an error before staging. No `7z` or archive-extraction
fallback exists.

### 5.6 Acceptance outline

- Original ISO is unchanged and present after success, cancellation, and
  cleanup.
- Full contents are copied when necessary.
- Copy verification occurs before install.
- Mounts, if any, are released on success and failure.
- Deferred sessions survive application restart.
- The final game is registered with the reserved stable ID and prefix.
- No DXVK or runtime dependency is installed automatically.

**Step 7 real-image result:** the one available real ISO on this host is a hybrid
ISO9660 + Apple Partition Map image. Root cause: UDisks parsed the APM and
withheld the whole-disk `Filesystem` interface, so `udisksctl mount` returned
`... is not a mountable filesystem` even though `blkid` reports ISO9660. Passing
`--no-partition-scan` to `udisksctl loop-setup` fixed the production mount. The
real image mounted read-only at `/run/media/<user>/<VOLUME-LABEL>` with
`ro,nosuid,nodev` and filesystem type `iso9660`; its contents were readable,
unmount succeeded, no loop backing remained, and the SHA-256 was unchanged.
General hybrid-ISO support is not claimed. UDF-only images remain unverified and
multi-disc remains unsupported; no fallback was added.

---

## 6. Feature E — Game Data Cleanup and Orphan Detection

### 6.1 Goal

Let users remove library entries and optionally clean MGL-owned data without
risking external files, user installations, or running games.

### 6.2 Required user choices

- Remove from library only (safe default; current behavior).
- Remove game files and associated MGL-owned data.
- Remove game files but preserve the Wine prefix.
- Remove the Wine prefix only.
- Keep or remove installer source files.

### 6.3 Requirements

- Detect orphaned MGL-owned data.
- Show exact deletion targets, ownership status, file counts, and estimated
  sizes before confirmation.
- Never delete external Wine prefixes or unverified paths.
- Protect against symlink and path-traversal attacks.
- Report partial failures and remaining data clearly.
- Do not delete data belonging to a running game.
- Preserve the existing metadata-only removal behavior as the safe default.
- Never delete the original ISO automatically.
- Do not delete external game installation directories.
- Require explicit confirmation for every destructive action.

### 6.4 Proposed orphan detection scope

- Prefixes under `prefixes/` not associated with a current game or session.
- Installer staging under `installers/` not associated with a session.
- Managed game directories under `games/` not associated with a game.
- Cached covers under `covers/` not referenced by any game.
- Pidfiles under the state directory without a matching game, after process
  verification.
- Unknown entries at managed roots are reported but not offered for deletion
  unless they match a known ownership pattern.

### 6.5 Proposed deletion flow

1. Build a cleanup plan.
2. Show targets, ownership, sizes, and running-state status.
3. Require explicit confirmation.
4. Re-verify containment immediately before deletion.
5. Delete only verified owned targets.
6. Delete the DB row and installer-session rows only after success.
7. On partial failure, keep the record by default and report remaining data.
8. Offer a separate explicit “Remove record anyway” action if the user wants to
   unregister even when files remain.

### 6.6 Safety notes

- The application currently has no stop-game action. Cleanup must block while a
  game is active or reattached, rather than adding a stop action implicitly.
- Switching a game from Windows to Linux can orphan its old prefix; orphan
  detection must handle this.
- Symlinks inside a managed tree must be unlinked, never followed.

### 6.7 Implemented state (Step 5)

`services/cleanup.py` now exposes `CleanupService`, `ResourceTarget`,
`CleanupIssue`, `CleanupOutcome`, `CleanupReport`, and `OrphanScan`. It uses the
Step 3 `OwnershipService` as the only ownership, size-estimation, and deletion
authority.

Cleanup choices:

- **Remove from library only** — the default. `GameManager.remove_game()` is
  called and no filesystem operation occurs. Installation, prefix, installer
  source, ISO, and covers remain.
- **Remove game files** — deletes only the verified managed
  `<games>/<game-id>` directory. The Wine prefix and installer source are
  separate switches and stay unless explicitly selected.
- **Remove the Wine prefix** — deletes only the verified managed
  `<prefixes>/<game-id>` directory. Game files, installer source, and the
  library record stay unless the record switch was explicitly selected.
- **Remove installer source** — deletes only the verified managed
  `<installers>/<game-id>` staging directory. The original ISO and external
  installer directories are never offered.
- Every selection is independent. The confirmation body lists each selected
  target, its path, ownership status, size, and whether it is blocked.

Orphan detection:

- On demand only. The three direct managed roots are scanned:
  `prefixes/<game-id>`, `games/<game-id>`, and `installers/<game-id>`.
- An entry is a candidate only when `OwnershipService.classify()` reports
  `managed` and no current game or active/deferred installer session references
  that exact path. Ownership is never inferred from the directory name alone.
- A game references a resource through its stored install, prefix, or installer
  source path. A session references staging and a managed destination through
  its stored paths; its prefix is the stable `<prefixes>/<game-id>` path.
- Active and deferred session statuses (`pending`, `staged`, `installing`,
  `installer_finished`, `deferred`) protect their data. Terminal `installed`
  and `failed` sessions do not.
- Symlinks, non-directories, unsafe names, unreadable entries, and entries
  beyond the bounded scan limit are reported as issues and are never deletion
  candidates.
- Covers and state pidfiles are deliberately not scanned in this step; the
  roadmap's proposal for those entries remains unimplemented.

Deletion safety:

- `delete_target()` reclassifies ownership, rejects a symlinked managed root,
  compares the directory `(st_dev, st_ino)` identity captured at preview time,
  checks running state, and checks installer-session state immediately before
  calling `OwnershipService.delete(authorized=True)`.
- For a library-associated target, the current game record must still reference
  the exact path. For an orphan, the resource must still be unassociated.
- Targets that changed after preview are skipped and the UI reports the reason.
  Selected targets are processed independently; a failure does not prevent the
  remaining selected targets from being attempted.
- The library record is removed only after every selected target was fully
  removed. Partial failure keeps the record and reports the remaining size when
  it can be measured.
- No database migration was required. Schema v5 already stores every path and
  ownership flag needed by cleanup. Deferred installer sessions are never
  deleted or modified by cleanup.
- Deletion still cannot be atomic against a concurrent path swap between the
  final identity check and the syscall; the implementation narrows that window
  and records a skip/error rather than claiming complete race protection.
- Cleanup never deletes external installations, external prefixes, original
  ISOs, covers, or arbitrary database paths.

`ui/cleanup_dialog.py` adds a per-game cleanup dialog, an on-demand orphan
dialog, and a results dialog. The game card/details removal action opens the
cleanup dialog. A header-bar action opens the orphan inspector. Unsafe rows are
disabled, the library record is on by default, and a second confirmation names
the exact selected targets.

---

## 7. Implementation order and dependencies

The recorded proposed sequence is:

1. **Command / external application launching** — implemented (Step 1)
2. **On-demand diagnostics** — implemented (Step 2)
3. **Ownership and data-management foundation** — implemented (Step 3)
4. **ISO installer** — implemented (Step 4)
5. **Cleanup UI and orphan detection** — implemented (Step 5)
6. **Final integration and testing** — completed (Step 6)

### Dependency explanation

- **Command launching** is independent and low-risk. It establishes the
  per-game environment model that the installer can also use.
- **Diagnostics** depends on the existing launch/prefix code and benefits from
  the ownership classifier, but can begin before ISO installation.
- **Ownership foundation must precede ISO installation.** The ISO workflow
  creates staging, prefixes, managed destinations, and optional external
  destinations. Without reliable ownership, cleanup cannot know what is safe
  to delete.
- **ISO installation must precede full cleanup UI** because cleanup needs to
  know every new data location and session state.
- **Final integration** must come last: combined UI, schema migration,
  documentation, and regression testing.

**Important:** ownership tracking is a prerequisite for destructive cleanup.
The proposed order intentionally places it before ISO installation even though
diagnostics may temporarily rely on simpler path checks.

---

## 8. Open decisions

**Recommendations below are not accepted unless marked otherwise.** Do not
encode a recommendation as implemented behavior without user approval.

| ID | Decision | Options | Decision / recommendation | Impact |
|---|---|---|---|---|
| D-1 | ISO access: mount vs extraction | literal read-only mount; archive-first extraction; both with fallback | **Accepted:** literal read-only mount through `udisksctl`, no fallback. Implemented in Step 4. | Security, permissions, UDF support, dependency shape |
| D-2 | UDF-only ISO support | reject clearly; require optional `7z`; mount-only fallback | Mount-only; UDF behavior unverified on this host. Still unresolved. | User coverage, dependencies, testing |
| D-3 | Multi-disc strategy | merge staging; mount-swap; unsupported | One ISO per session; multi-disc unsupported in Step 4. Still unresolved. | Installer compatibility, complexity |
| D-4 | Command launch data model | reuse Linux native; add `Platform.COMMAND`; add separate launch-kind field | **Accepted:** reuse Linux native + validated per-game environment field. Implemented in Step 1. | Schema migration, UI, performance behavior |
| D-5 | Destination ownership | only app-managed `games/<id>`; external allowed but non-deletable; external deletable | **Implemented:** managed `games/<id>` is owned; external destinations are non-owned and never deleted automatically. | Cleanup safety, UX |
| D-6 | Deferred-install UI | header “Pending installs”; dialog; placeholder card | **Implemented:** dedicated pending-installs dialog; deferred sessions persist in `installer_sessions`. | Navigation, discoverability |
| D-7 | Diagnostic path redaction | `~` by default; full paths; toggle | `~` by default with optional explicit full paths. Implemented provisionally in Step 2; not yet formally accepted. | Privacy, support usefulness |
| D-8 | DXVK detail level | presence only; presence + override hints; exact version | Presence + caveated override hints. Implemented provisionally in Step 2; exact versions are not claimed. | Accuracy, implementation effort |
| D-9 | Cleanup while running | block; warn and allow; add stop action | **Accepted and implemented:** block until the game exits. No stop action was added. | Safety, UX |
| D-10 | Version bump after 0.5.0 | 0.6.0; 0.5.1; other | 0.6.0 after the feature set is accepted | Packaging, documentation |

### Additional unresolved questions

- Should the ISO installer support Linux installers, or only Windows/Wine?
- Should the original ISO path appear in library details after registration?
- Should cleanup offer cached-cover removal in the first release?
- What exact report format should diagnostics export (plain text, Markdown,
  JSON)?
- How should deferred sessions be surfaced when the library is empty?
- Is a literal kernel mount a hard requirement, or is secure extraction
  sufficient?

---

## 9. Out of scope

- Background services, daemons, or continuous monitoring.
- Root-required workflows.
- Automatic dependency installation.
- Game process termination / stop-game action.
- Modifying external Wine prefixes.
- Network upload of diagnostics.
- Cloud synchronization.
- Cross-distribution packaging changes.
- Changes to `README.md`, packaging files, or application code during Step 0.

---

## 10. Verification expectations

Every implemented phase must provide:

- automated tests using isolated temporary data;
- a migration test from the prior schema version;
- explicit no-write assertions for diagnostics and dry-run cleanup;
- symlink/path-traversal tests for any destructive operation;
- regression of the existing test baseline (409 tests after Step 7);
- a manual GTK/Wine smoke test under `dbus-run-session` with isolated XDG
  directories;
- an implementation-log entry using the format in
  `docs/IMPLEMENTATION-LOG.md`.

Runtime behavior must not be claimed as verified solely because the source
appears correct.

---

## 11. Step 6 final verification record

> These records reflect the state when each step was written. Steps 1–11 were
> later committed in `d58ab02` and `3c50f17`; current state is in §1.1.

**Status:** completed and uncommitted. This section records what was actually
executed; see `docs/IMPLEMENTATION-LOG.md` §Step 6 for the full entry.

- Full suite: **408 passed, 51 warnings in 51.82s** with
  `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider`.
- Schema matrix: fresh, v1→v5, v2→v5, v3→v5, v4→v5, forced mid-migration
  rollback, and newer-schema rejection all behaved as required.
- Cross-feature integration: install session, staging, discovery, installer
  launch, registration, protected cleanup, allowed cleanup, and orphan removal.
- GTK smoke: 14 steps, rc=0, no GTK criticals, under `dbus-run-session` with
  isolated `HOME`/`XDG_*`, `G_DEBUG=fatal-criticals`, `GTK_A11Y=none`, and
  `NO_AT_BRIDGE=1`.
- Packaging: fixed the absolute `Exec=` path in the installed desktop entry,
  added `tests/test_packaging.py`, rebuilt the AppDir and
  `dist/Minimal-Game-Launcher.AppImage` (1,051,128 bytes), and smoke-started the
  AppImage with isolated data directories. Cross-distribution portability is not
  claimed.
- Real ISO: mount blocked by the host/image shape; real content verified through
  production staging/registration/cleanup with only the mount provider faked.
  The ISO hash was unchanged before and after. UDF and multi-disc remain
  unresolved.
- The working tree remains dirty: Steps 1–6 are uncommitted and several files
  are untracked. No Git history operation was performed.

---

## 12. Step 7 hybrid ISO mount fix record

**Status:** completed and uncommitted. See `docs/IMPLEMENTATION-LOG.md` §Step 7.

- Real image: `~/Downloads/<game>.iso`, 420,012,032 bytes, SHA-256
  `67d4107924e3078fef70404cc15adfd4b6c676dca71c72deaca144aded72f3b3`.
  `file` reports ISO 9660; `blkid -p` reports `TYPE="iso9660"` and
  `PTTYPE="mac"`.
- Diagnosed with `gdbus introspect`: `loop0` exposed `PartitionTable`
  (`Type='mac'`) and children `loop0p1`/`loop0p2`, but no `Filesystem`
  interface. `udisksctl mount` therefore returned `... is not a mountable
  filesystem`.
- Fix: add `--no-partition-scan` to the existing read-only `udisksctl
  loop-setup` argv. UDisks then exposed `Filesystem` on `loop0` and mounted the
  ISO9660 filesystem read-only.
- Verified mount options: `/proc/self/mountinfo` showed one entry for
  `/dev/loop0` at `/run/media/<user>/<VOLUME-LABEL>` with `ro,nosuid,nodev` and
  superblock type `iso9660`. Unmount removed the entry; `losetup -a` was empty;
  the SHA-256 was unchanged.
- End-to-end production staging on the real image mounted, verified 33 files and
  4 directories (382,898,830 bytes), unmounted, and left no loop backing. The
  managed staging directory was removed through the ownership service and the
  ISO hash stayed unchanged.
- Full suite: **409 passed, 51 warnings in 51.78s** with
  `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider`.
- The working tree remains dirty: Steps 1–7 are uncommitted. No Git history
  operation was performed.

---

## 13. Step 8 post-installation executable discovery record

**Status:** completed and uncommitted. See `docs/IMPLEMENTATION-LOG.md` §Step 8.

- Root cause of the empty `games/<game-id>` directory: the installer dialog
  always called `resolve_destination(..., create=True)` with the managed
  destination as the only default, before the installer ran. The destination
  was therefore created even when the installer wrote into the Wine prefix. No
  ownership, metadata, or cleanup rule required that empty directory.
- Destination types are now explicit: the installer's Wine-prefix default
  (creates nothing and stores `install_directory = NULL`), the managed
  `<games>/<game-id>` directory, or an external non-owned folder.
- `services/executable_discovery.py` searches, in order: the configured
  installation directory, `drive_c/Program Files`, `drive_c/Program Files
  (x86)`, then other non-system `drive_c` children. It hides only clearly
  identifiable helpers (whole-word `setup`, `unins*`, redistributables, crash
  handlers, and similar). Helper-like words in other names (`Patchwork`,
  `Setup Quest`, `Updated Quest`) are demoted, not hidden. Ranking is
  deterministic by root priority, depth, and game-name match.
- The finish page shows ranked candidates with relative paths and locations,
  requires explicit selection, marks a recommended candidate only when it is
  unambiguous, provides manual selection, and allows cancel/registration later
  without changing launch configuration.
- Registration uses the existing `GameManager.register_installation` and
  `LaunchManager`; the executable's parent becomes the working directory, and
  existing arguments, environment, cover, GameMode, MangoHud, and Wine prefix
  are preserved for existing records.
- Full suite: **431 passed, 51 warnings in 51.71s** with
  `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider`.
- No real game installation was performed. No Git operation was performed.

---

## 14. Step 9 pending-install removal record

**Status:** completed and uncommitted. See `docs/IMPLEMENTATION-LOG.md` §Step 9.

- The pending-installs dialog now has a destructive `Remove` action per session
  with an explicit confirmation dialog. The confirmation lists the installer
  staging directory, the Wine prefix, the managed destination when owned, any
  external folder that will be kept, and notes that the original ISO is kept. It
  offers **Remove session only** (files stay on disk) and **Remove files and
  session**.
- `InstallerManager.remove_pending_install()` requires authorization, refuses a
  running installer, revalidates each resource through `OwnershipService`, and
  deletes only verified managed directories: staging, prefix, and the managed
  destination when `install_directory_owned` is true. With `delete_files=False`
  it deletes only the session row.
- External destinations are reported as retained and never deleted. A
  Wine-default install has no destination to delete.
- An already missing resource counts as success, so the session record is still
  removed. Only a real failure keeps the session for a retry.
- The session row is deleted on the GTK main thread, because the SQLite
  connection is thread-bound. The worker deletes files with
  `delete_session=False` and the main thread then runs
  `discard_pending_session()`.
- The result popup lists `Deleted`, `Already missing`, `Kept (external)`, and
  `Failed` resources plus whether the session record was removed. A session-only
  removal reports that every staged file was kept. Missing files are named but do
  not make the result a failure.
- `removed` refreshes the list and the main window's pending-install indicator.
- Full suite: **444 passed, 51 warnings in 52.41s** with
  `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider`.
- No real game installation was performed. No Git operation was performed.

---

## Stabilization: Wine prefix audit fixes (Fixes 1–5)

Stabilization only; no feature was added. Completed, uncommitted.

- **Atomic migrations:** each schema version is one transaction with its
  `user_version` bump; failures roll back fully.
- **Prefix readiness:** a prefix is ready only with the launcher marker, `system.reg`,
  and `drive_c`; the marker is written after validated `wineboot` success.
- **Shared-prefix locks:** inter-process `flock` outside `.shared`, bounded wait.
  Initialization lock (300 s) is held through `wineboot`. Lifecycle lock (10 s) is
  held briefly for shared-reference persistence and deletion. Order: lifecycle,
  then initialization; deletion takes initialization without waiting.
- **Shared-prefix assignment:** new game, update, registration, installer session
  creation, and session save with a shared reference all take the lifecycle lock.
- **Shared-prefix cleanup:** deletion revalidates references under both locks.
- **Stale assignment:** an assignment that waited for the lifecycle lock and
  finds `.shared` missing is refused with `SharedPrefixUnavailableError` before
  any database write. Uncontended first assignment is still allowed.
- **Installer sessions:** `FAILED` sessions protect their data; launch state is
  persisted before the process starts and reverted on refusal.
- Full suite: **506 passed**. GTK smoke: rc=0.
- Not done: committing; a per-game lock; Wine isolation from external Wine processes.

---

## 15. Step 10 direct installer and desktop launcher record

**Status:** implemented and uncommitted. See `docs/IMPLEMENTATION-LOG.md` §Step 10.

- **Standalone `.exe` installers** reuse the ISO session, staging, prefix,
  launch, discovery, and registration services. `InstallerSourceKind` and
  `installer_sessions.source_kind` distinguish `iso` from `executable`;
  schema v7 defaults existing rows to `iso`.
- Direct mode validates a regular, non-empty, readable `.exe` with an `MZ`
  signature, resolves symlinks, and refuses sources inside MGL-managed roots.
  It copies only the selected file; companion `.cab`/`.bin` files are
  unsupported and the original file is never modified.
- Direct staging never mounts an ISO or invokes UDisks. Discovery returns the
  user-selected staged file without ISO name heuristics and never auto-runs it.
- **Desktop launcher integration** writes one user-level entry and icon named
  after `APP_ID`, marked `X-MGL-Managed=true`. Writes are atomic. Disabling
  writes a `Hidden=true` override when a same-ID system entry exists; an
  unowned canonical file is never deleted or overwritten. Runtime detection
  covers AppImage, AppDir, installed command, wrapper, and source checkout.
- Settings persist `desktop_entry_enabled` as `None`/`True`/`False`; `None`
  performs no startup filesystem changes. Saving rolls back desktop changes if
  settings persistence fails.

---

## 16. Step 12 — v0.6.0 feature set record

**Status:** implemented and uncommitted, 2026-11-07.

- **Library visibility** is per game. `games.show_in_library` defaults to `1`.
  `GameManager.list_games()` filters hidden rows by default; `include_hidden`
  is used by management paths, so cleanup and orphan detection still see every
  record. `list_hidden_games()` and `set_library_visibility()` back the hidden
  games dialog. Nothing is deleted.
- **Wine runtime diagnostics** capture only Windows launch output. A daemon
  reader drains the pipe into a 64 KiB buffer so a full pipe cannot block the
  game; the launcher writes `<state>/launch-logs/<game-id>.log` with an
  `exit_status=` header after exit. `services/wine_output.py` recognizes 12
  signatures (permissions, missing executable/library, prefix, DLL, MSVC,
  .NET, Direct3D, DXGI, DXVK, VKD3D, Vulkan, crash) plus a non-zero exit
  finding. Diagnostics adds a "Last launch output" section.
- **Per-game Winecfg and Winetricks** are explicit on-demand actions.
  `services/wine_tools.py` verifies the prefix through `OwnershipService`,
  refuses external and uninitialized prefixes, validates Winetricks verbs
  against a bounded name pattern, uses argv lists with `shell=False`, and
  detaches with `start_new_session=True`. Output is appended to
  `<state>/wine-tools/<game-id>.log`; the launcher does not wait. Missing
  tools disable only the matching action; nothing is installed automatically.
- **Display configuration** stores `display_mode` (`default`, `fullscreen`,
  `windowed`, `borderless`, `virtual_desktop`). Virtual desktop is
  Windows-only, requires a `WxH`, and launches through
  `wine explorer /desktop=MGL-<id8>,WxH <exe> ...`; the host resolution is
  never changed. Other modes are recorded hints that MGL cannot enforce for
  every game.
- **gamescope** stores an explicit enable flag, render and output resolutions,
  FPS limit, vsync, filter, and fullscreen. The argv order is render size,
  output size, fullscreen, FPS limit, vsync, filter, then `--` and the wrapped
  command. An explicit request is a launch requirement: a missing binary raises
  `LaunchError` rather than silently changing the display mode.
- **First-run wizard** is informational and preference-only. It performs one
  `PATH` lookup per optional tool when its page is shown, records the prefix
  strategy and desktop-entry preference, and persists
  `settings.json:setup_completed`. Skipping also marks it complete. It starts
  no service and creates no prefix.
- **Idle resources:** no new daemon, timer, polling loop, startup scan, or
  automatic dependency install. All new work is triggered by a user action.
- **Verification:** 645 passed, 52 warnings in 62.48 s; real GTK startup under
  `dbus-run-session` with isolated `XDG_*` directories; no real Wine,
  Winetricks, gamescope, or installer process was run.
- **Known limitations:** gamescope and display modes other than the Wine
  virtual desktop depend on the game/driver; Winetricks requires network
  access and can take minutes; captured output is bounded and may omit the
  root cause after the first 64 KiB.
