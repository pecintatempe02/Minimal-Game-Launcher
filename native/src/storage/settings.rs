//! Global settings, persisted as JSON next to the library.
//!
//! Unknown keys are ignored and missing keys fall back to defaults, so a
//! settings file written by a future version never prevents startup. Old Wine
//! and prefix keys from earlier builds are simply ignored.

use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::core::paths;
use crate::ui::theme::ThemeName;

/// Window alpha bounds, matching the prototype's 30–100 % slider.
pub const MIN_OPACITY: u8 = 30;
pub const MAX_OPACITY: u8 = 100;

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default)]
pub struct Settings {
    pub theme: ThemeName,
    /// Whether MGL asks the compositor for a translucent window.
    pub transparent: bool,
    /// Window opacity percentage, 30–100.
    pub opacity: u8,
    /// Solid background fallback for compositors without transparency.
    pub solid_fallback: bool,
    /// Window geometry, remembered between runs.
    pub window: WindowGeometry,
    /// Whether the sidebar starts collapsed.
    pub sidebar_collapsed: bool,
    /// Sort order for the library list.
    pub sort: SortOrder,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum SortOrder {
    #[default]
    Name,
    Type,
}

impl SortOrder {
    pub fn label(self) -> &'static str {
        match self {
            SortOrder::Name => "Name",
            SortOrder::Type => "Type",
        }
    }
}

#[derive(Clone, Copy, Debug, Serialize, Deserialize)]
#[serde(default)]
pub struct WindowGeometry {
    pub width: u32,
    pub height: u32,
    /// `None` lets the compositor place the window.
    pub x: Option<i32>,
    pub y: Option<i32>,
}

impl Default for WindowGeometry {
    fn default() -> Self {
        Self {
            width: 1100,
            height: 720,
            x: None,
            y: None,
        }
    }
}

impl Default for Settings {
    fn default() -> Self {
        Self {
            theme: ThemeName::Dark,
            transparent: true,
            opacity: 85,
            solid_fallback: false,
            window: WindowGeometry::default(),
            sidebar_collapsed: false,
            sort: SortOrder::Name,
        }
    }
}

impl Settings {
    /// Whether the window should actually be translucent right now.
    pub fn window_translucent(&self) -> bool {
        self.transparent && !self.solid_fallback
    }

    /// Effective background alpha, clamped to the allowed range.
    pub fn alpha(&self) -> f32 {
        if !self.window_translucent() {
            return 1.0;
        }
        self.opacity.clamp(MIN_OPACITY, MAX_OPACITY) as f32 / 100.0
    }

    /// Clamp values that could make the UI unusable.
    pub fn sanitize(&mut self) {
        self.opacity = self.opacity.clamp(MIN_OPACITY, MAX_OPACITY);
        self.window.width = self.window.width.clamp(720, 8000);
        self.window.height = self.window.height.clamp(480, 8000);
    }
}

/// Loads and saves [`Settings`].
pub struct SettingsStore {
    path: PathBuf,
}

impl SettingsStore {
    pub fn new(path: PathBuf) -> Self {
        Self { path }
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    /// Load settings, falling back to defaults when the file is missing or
    /// unreadable. A damaged file is preserved next to the new one.
    pub fn load(&self) -> Settings {
        let text = match std::fs::read_to_string(&self.path) {
            Ok(text) => text,
            Err(_) => return Settings::default(),
        };
        match serde_json::from_str::<Settings>(&text) {
            Ok(mut settings) => {
                settings.sanitize();
                settings
            }
            Err(_) => {
                let backup = self.path.with_extension("json.invalid");
                let _ = std::fs::rename(&self.path, &backup);
                Settings::default()
            }
        }
    }

    /// Persist settings. Errors are reported to the caller for display.
    pub fn save(&self, settings: &Settings) -> Result<(), String> {
        if let Some(parent) = self.path.parent() {
            paths::ensure_dir(parent)
                .map_err(|e| format!("Could not create {}: {e}", parent.display()))?;
        }
        let text = serde_json::to_string_pretty(settings)
            .map_err(|e| format!("Could not encode the settings: {e}"))?;
        let temporary = self.path.with_extension("json.tmp");
        std::fs::write(&temporary, text)
            .map_err(|e| format!("Could not write {}: {e}", temporary.display()))?;
        std::fs::rename(&temporary, &self.path)
            .map_err(|e| format!("Could not replace {}: {e}", self.path.display()))?;
        Ok(())
    }
}
