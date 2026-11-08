//! MGL — Minimal Game Launcher, native Rust implementation.
//!
//! Layering:
//! * [`core`] — models, verbatim commands, covers, processes, supervision. No UI.
//! * [`storage`] — SQLite library and JSON settings.
//! * [`ui`] — design tokens, text, renderer, widgets, screens.
//! * [`app`] — state and actions wiring the three together.
//! * [`gfx`] — window and GPU surface plumbing.

pub mod app;
pub mod core;
pub mod gfx;
pub mod logging;
pub mod storage;
pub mod ui;
