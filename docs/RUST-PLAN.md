# MGL native Rust remake — implementation record

## Purpose

`native/` is a small command launcher: each library entry is a name, a cover,
and one complete shell command. MGL stores the command verbatim and never
generates, rewrites, or interprets it.

The Python/GTK implementation under `src/minimal_game_launcher/` is untouched.
Its `library.db` is only ever opened read-only, once, when the native library is
still empty.

## What the application does

| Area | Implementation |
|---|---|
| Storage | SQLite (`mgl.db`, schema version 2) with `games` and `runs`; `runs` keeps the newest 10 per game. |
| Migration | A schema-version-1 database is snapshotted with `VACUUM INTO` first, then migrated. Legacy Python rows are read read-only and converted into full commands. |
| Launch | `/bin/sh -lc "<command>"`, exactly as typed, in the configured working directory (empty means `$HOME`). Optional `KEY=VALUE` environment lines are validated before launch. |
| Process control | One process group per game (`setsid`), nonblocking play/stop, bounded stdout/stderr capture, `SIGTERM` then `SIGKILL`, automatic reaping and record completion. |
| Covers | The picker and import run on a worker thread. PNG is decoded natively (including palette and low-bit-depth images); JPEG/WebP use `ffmpeg`, `magick`, or `convert` when installed. Originals are copied into `covers/` and a 384×384 square PNG thumbnail is generated. |
| Rendering | One instanced quad pipeline draws rounded rectangles, borders, gradients, glyphs, and covers. Cover thumbnails live in a 16-layer 384×384 RGBA texture array. |
| Idle policy | The event loop blocks in the compositor. A 250 ms timer exists only while a game or cover job is live. |

## Screens

* **Library** — search, All/Native/Wine/Custom filters, responsive card grid,
  play/stop, logs, edit, and remove.
* **Settings** — theme, sort order, transparency and solid fallback, opacity.
* **Editor** — name, command (textarea), working directory, environment, notes,
  runner label, and cover picker with preview.
* **Logs** — the live run plus the newest 10 completed records, with copy and
  clear.
* **Remove** — confirmation that states precisely what is deleted: the database
  row, its logs, and any cover MGL copied. Game files, installers, and Wine
  prefixes are never touched.

## Deliberate limits

* No installer, ISO mounting, Wine prefix management, diagnostics, hidden games,
  screenshots, or resource deletion. Wine remains only as a runner label inferred
  from `wine`/`.exe`, or chosen by the user; the command itself is authoritative.
* Covers are the only managed files. Deletion is restricted to paths inside
  `covers/`, checked both lexically and by directory ownership.
* A cover chosen for a new game is imported immediately; cancelling the editor
  removes it again.

## Verification

Run from `native/`:

```sh
cargo fmt --check
cargo check --offline
cargo test --offline --release
cargo build --offline --release --bins
cargo clippy --offline --all-targets
```

Tests cover verbatim shell execution, environment validation, process capture
and exit status, supervision and relaunch, the 10-record retention bound,
schema-v1 backup/migration, read-only legacy import, PNG/thumbnail handling,
managed-path ownership, and command/SQL injection safety.

A release build was also smoke-tested in the development Wayland session with an
isolated `XDG_DATA_HOME`: the window, shader pipeline, and cover texture upload
initialised without validation errors or panics. This is a smoke test, not a
complete manual acceptance pass.
