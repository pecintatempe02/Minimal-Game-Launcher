//! SQLite persistence for the library.
//!
//! The schema is deliberately small: two tables, no triggers, no views. Writes
//! happen on the calling thread only when the user changes something.

pub mod database;
pub mod settings;

pub use database::{Database, ImportReport, MAX_RECORDS_PER_GAME};
pub use settings::{Settings, SettingsStore, SortOrder, WindowGeometry};
