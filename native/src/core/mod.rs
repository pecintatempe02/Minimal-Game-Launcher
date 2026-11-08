//! Application core: models, command helpers, cover storage and process
//! execution. Nothing in here knows that a renderer exists.

pub mod command;
pub mod cover;
pub mod game;
pub mod launcher;
pub mod library;
pub mod paths;
pub mod process;

pub use game::{Game, RunStatus, RunnerLabel};
