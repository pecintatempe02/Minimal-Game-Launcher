//! Presentation layer: tokens, text, renderer, widgets and screens.
//!
//! Nothing in `ui` owns application data; screens read an immutable view of the
//! state and return actions for `app` to apply.

pub mod components;
pub mod layout;
pub mod renderer;
pub mod screens;
pub mod text;
pub mod theme;
