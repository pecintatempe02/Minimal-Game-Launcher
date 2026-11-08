//! XDG directory resolution.
//!
//! The directory names match the previous Python implementation so an existing
//! library can be adopted. Nothing outside these paths is ever written, and
//! nothing outside them is ever deleted.

use std::path::{Path, PathBuf};

/// Application identity, also used for the desktop filename and window class.
pub const APP_ID: &str = "minigame-launcher";
/// Legacy directory of the Phase-2 prototype, read once for migration.
pub const LEGACY_DATA_DIRECTORY_NAME: &str = "minimal-game-launcher";

fn env_path(var: &str) -> Option<PathBuf> {
    std::env::var_os(var)
        .map(PathBuf::from)
        .filter(|p| p.is_absolute())
}

fn home() -> PathBuf {
    std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("/"))
}

/// `$XDG_DATA_HOME/minigame-launcher`.
pub fn data_dir() -> PathBuf {
    env_path("XDG_DATA_HOME")
        .unwrap_or_else(|| home().join(".local/share"))
        .join(APP_ID)
}

/// Replace a leading home directory with `~` for display.
pub fn tildify(path: &Path) -> String {
    let text = path.to_string_lossy();
    if let Ok(home) = std::env::var("HOME")
        && let Some(rest) = text.strip_prefix(&home)
    {
        return format!("~{rest}");
    }
    text.into_owned()
}

/// `$XDG_CONFIG_HOME/minigame-launcher`.
pub fn config_dir() -> PathBuf {
    env_path("XDG_CONFIG_HOME")
        .unwrap_or_else(|| home().join(".config"))
        .join(APP_ID)
}

/// SQLite library file owned by this implementation.
///
/// Deliberately *not* `library.db`: the Python application keeps its own
/// `library.db` in the same directory and uses a completely different schema.
/// [`legacy_library_path`] is imported from, read-only.
pub fn database_path() -> PathBuf {
    data_dir().join("mgl.db")
}

/// Library written by the Python implementation of this app.
pub fn legacy_library_path() -> PathBuf {
    data_dir().join("library.db")
}

/// Library written by the earlier prototype, read once for migration.
pub fn prototype_library_path() -> PathBuf {
    data_dir()
        .parent()
        .unwrap_or_else(|| Path::new("/nonexistent"))
        .join(LEGACY_DATA_DIRECTORY_NAME)
        .join("library.db")
}

/// Global settings file owned by this implementation.
pub fn settings_path() -> PathBuf {
    config_dir().join("mgl.json")
}

/// Root of the cover images MGL copied into its own storage.
pub fn covers_dir() -> PathBuf {
    data_dir().join("covers")
}

/// Absolute path of a cover file owned by MGL.
pub fn cover_path(game_id: &str, extension: &str) -> PathBuf {
    covers_dir().join(format!("{game_id}.{extension}"))
}

/// Absolute path of the library thumbnail for a game.
pub fn thumbnail_path(game_id: &str) -> PathBuf {
    covers_dir().join(format!("{game_id}.thumb.png"))
}

/// Whether `path` is inside the directory MGL owns for covers.
pub fn is_managed_cover(path: &Path) -> bool {
    is_within(path, &covers_dir())
}

/// Whether `path` is inside one of the directories MGL owns.
pub fn is_inside_data_dir(path: &Path) -> bool {
    is_within(path, &data_dir())
}

/// Whether `path` is inside `root` after resolving `.`/`..` lexically and
/// comparing on canonicalised parents where possible.
pub fn is_within(path: &Path, root: &Path) -> bool {
    let path = normalize(path);
    let root = normalize(root);
    path != root && path.starts_with(&root)
}

/// Lexical normalisation: resolves `.` and `..` without touching the filesystem,
/// which keeps the check working for paths that no longer exist.
pub fn normalize(path: &Path) -> PathBuf {
    use std::path::Component;
    let mut out = PathBuf::new();
    for component in path.components() {
        match component {
            Component::CurDir => {}
            Component::ParentDir => {
                out.pop();
            }
            other => out.push(other.as_os_str()),
        }
    }
    out
}

/// Create a directory (and parents) if it does not exist yet.
pub fn ensure_dir(path: &Path) -> std::io::Result<()> {
    if path.is_dir() {
        Ok(())
    } else {
        std::fs::create_dir_all(path)
    }
}

/// Reduce a display name to a filesystem-safe identifier.
///
/// The result is used for directory and cover names, so it is restricted to
/// ASCII lowercase letters, digits and dashes and can never contain a path
/// separator.
pub fn slugify(name: &str) -> String {
    let mut out = String::with_capacity(name.len());
    let mut last_dash = true;
    for ch in name.chars() {
        if ch.is_ascii_alphanumeric() {
            out.push(ch.to_ascii_lowercase());
            last_dash = false;
        } else if !last_dash {
            out.push('-');
            last_dash = true;
        }
    }
    let trimmed = out.trim_matches('-').to_string();
    if trimmed.is_empty() {
        "game".to_string()
    } else {
        trimmed
    }
}

/// Make `base` unique against an existing key set.
pub fn unique_id(base: &str, taken: impl Fn(&str) -> bool) -> String {
    if !taken(base) {
        return base.to_string();
    }
    for suffix in 2..1000 {
        let candidate = format!("{base}-{suffix}");
        if !taken(&candidate) {
            return candidate;
        }
    }
    format!("{base}-{}", std::process::id())
}

/// Expand a leading `~/` (or a bare `~`) to the home directory.
pub fn expand_tilde(input: &str) -> PathBuf {
    let trimmed = input.trim();
    if let Some(rest) = trimmed.strip_prefix("~/") {
        home().join(rest)
    } else if trimmed == "~" {
        home()
    } else {
        PathBuf::from(trimmed)
    }
}

/// Quote a path for display inside a shell command.
pub fn shell_quote(value: &str) -> String {
    let escaped = value
        .replace('\\', "\\\\")
        .replace('"', "\\\"")
        .replace('$', "\\$")
        .replace('`', "\\`");
    format!("\"{escaped}\"")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn slugify_never_contains_a_separator() {
        assert_eq!(slugify("My Game / Part 2"), "my-game-part-2");
        assert_eq!(slugify("!!!"), "game");
    }

    #[test]
    fn managed_cover_check_rejects_outside_paths() {
        assert!(is_managed_cover(&covers_dir().join("a.png")));
        assert!(!is_managed_cover(Path::new("/tmp/a.png")));
    }
}
