//! The game model and its execution records.
//!
//! A game is a name, a cover, and one full launch command. The command is the
//! source of truth: MGL never rewrites, appends to, or interprets it.

use std::path::Path;

use serde::{Deserialize, Serialize};

/// Optional label shown on a card. `Auto` derives a label from the command.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "lowercase")]
pub enum RunnerLabel {
    #[default]
    Auto,
    Native,
    Wine,
    Custom,
}

impl RunnerLabel {
    pub const ALL: [RunnerLabel; 4] = [
        RunnerLabel::Auto,
        RunnerLabel::Native,
        RunnerLabel::Wine,
        RunnerLabel::Custom,
    ];

    pub fn as_str(self) -> &'static str {
        match self {
            RunnerLabel::Auto => "auto",
            RunnerLabel::Native => "native",
            RunnerLabel::Wine => "wine",
            RunnerLabel::Custom => "custom",
        }
    }

    pub fn parse(value: &str) -> Self {
        match value.to_ascii_lowercase().as_str() {
            "native" => RunnerLabel::Native,
            "wine" => RunnerLabel::Wine,
            "custom" => RunnerLabel::Custom,
            _ => RunnerLabel::Auto,
        }
    }

    pub fn label(self) -> &'static str {
        match self {
            RunnerLabel::Auto => "Auto",
            RunnerLabel::Native => "Native",
            RunnerLabel::Wine => "Wine",
            RunnerLabel::Custom => "Custom",
        }
    }
}

/// State of a tracked process.
#[derive(Clone, Debug, PartialEq, Eq, Default)]
pub enum RunStatus {
    #[default]
    Idle,
    Running {
        pid: u32,
        /// Wall-clock start, `YYYY-MM-DD HH:MM:SS` local time.
        started: String,
    },
    Finished {
        outcome: String,
    },
}

/// One game in the library.
#[derive(Clone, Debug)]
pub struct Game {
    pub id: String,
    pub name: String,
    /// Complete command handed to `/bin/sh -lc`, verbatim.
    pub command: String,
    /// Optional working directory. Empty means the user's home directory.
    pub working_dir: String,
    /// Optional environment lines, `KEY=VALUE` per line.
    pub environment: String,
    /// Optional free-form notes.
    pub notes: String,
    pub runner: RunnerLabel,
    /// Managed cover original, if any.
    pub cover: Option<String>,
    /// Managed square thumbnail for library rendering.
    pub cover_thumb: Option<String>,
    /// Hue used for the fallback cover art.
    pub hue: f32,
    /// Insertion order, used as a stable tiebreaker when sorting.
    pub created_at: String,
    /// Live process state; never persisted.
    pub status: RunStatus,
}

impl Game {
    pub fn new(id: impl Into<String>, name: impl Into<String>, command: impl Into<String>) -> Self {
        let id = id.into();
        Self {
            hue: super::super::ui::theme::hue_of(&id),
            id,
            name: name.into(),
            command: command.into(),
            working_dir: String::new(),
            environment: String::new(),
            notes: String::new(),
            runner: RunnerLabel::Auto,
            cover: None,
            cover_thumb: None,
            created_at: String::new(),
            status: RunStatus::Idle,
        }
    }

    pub fn is_running(&self) -> bool {
        matches!(self.status, RunStatus::Running { .. })
    }

    pub fn pid(&self) -> Option<u32> {
        match self.status {
            RunStatus::Running { pid, .. } => Some(pid),
            _ => None,
        }
    }

    /// Label to show on the card.
    pub fn runner_label(&self) -> &'static str {
        match self.runner {
            RunnerLabel::Auto => {
                let lower = self.command.to_ascii_lowercase();
                if lower.contains("wine") || lower.contains(".exe") {
                    "Wine"
                } else {
                    "Native"
                }
            }
            other => other.label(),
        }
    }

    /// Parse the environment lines into pairs for `Command::env`.
    ///
    /// Blank lines and `#` comments are ignored. A line without `=` is an
    /// error shown before the process is started, rather than a silently
    /// dropped variable.
    pub fn environment_pairs(&self) -> Result<Vec<(String, String)>, String> {
        parse_environment(&self.environment)
    }

    /// Validated working directory, or the user's home when none is set.
    pub fn resolved_workdir(&self) -> Result<std::path::PathBuf, String> {
        if self.working_dir.trim().is_empty() {
            let home = std::env::var_os("HOME")
                .map(std::path::PathBuf::from)
                .ok_or_else(|| "No working directory is set and HOME is missing.".to_string())?;
            return Ok(home);
        }
        let path = super::paths::expand_tilde(&self.working_dir);
        if !path.is_dir() {
            return Err(format!(
                "The working directory {} does not exist.",
                path.display()
            ));
        }
        Ok(path)
    }

    /// Human-readable directory shown in the config sheet and logs.
    pub fn workdir_display(&self) -> String {
        if self.working_dir.trim().is_empty() {
            std::env::var("HOME").unwrap_or_default()
        } else {
            self.working_dir.clone()
        }
    }

    /// Whether a path is this game's managed cover, and may therefore be
    /// deleted by MGL.
    pub fn owns_cover_path(path: &Path) -> bool {
        super::paths::is_managed_cover(path)
    }
}

fn parse_environment(text: &str) -> Result<Vec<(String, String)>, String> {
    let mut pairs = Vec::new();
    for (index, line) in text.lines().enumerate() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let Some((key, value)) = line.split_once('=') else {
            return Err(format!("Environment line {} has no '=': {line}", index + 1));
        };
        let key = key.trim();
        let valid = !key.is_empty()
            && key
                .chars()
                .next()
                .is_some_and(|c| c.is_ascii_alphabetic() || c == '_')
            && key.chars().all(|c| c.is_ascii_alphanumeric() || c == '_');
        if !valid {
            return Err(format!(
                "Environment line {} has an invalid name: {key}",
                index + 1
            ));
        }
        let mut value = value.trim().to_string();
        // Allow the common shell quoting style `KEY="value with spaces"`.
        if value.len() >= 2
            && ((value.starts_with('"') && value.ends_with('"'))
                || (value.starts_with('\'') && value.ends_with('\'')))
        {
            value = value[1..value.len() - 1].to_string();
        }
        pairs.push((key.to_string(), value));
    }
    Ok(pairs)
}

/// How a process ended, as far as MGL can observe it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Outcome {
    Exit(i32),
    Signal(i32),
    Stopped,
    SpawnFailed(String),
}

impl Outcome {
    pub fn describe(&self) -> String {
        match self {
            Outcome::Exit(code) => format!("exit status {code}"),
            Outcome::Signal(sig) => format!("terminated by {}", signal_name(*sig)),
            Outcome::Stopped => "stopped by MGL".to_string(),
            Outcome::SpawnFailed(reason) => format!("could not start: {reason}"),
        }
    }

    pub fn as_i32(&self) -> Option<i32> {
        match self {
            Outcome::Exit(code) => Some(*code),
            Outcome::Signal(sig) => Some(128 + sig),
            _ => None,
        }
    }
}

fn signal_name(sig: i32) -> String {
    let name = match sig {
        1 => "SIGHUP",
        2 => "SIGINT",
        3 => "SIGQUIT",
        4 => "SIGILL",
        6 => "SIGABRT",
        9 => "SIGKILL",
        11 => "SIGSEGV",
        13 => "SIGPIPE",
        14 => "SIGALRM",
        15 => "SIGTERM",
        _ => return format!("signal {sig}"),
    };
    format!("{name} ({sig})")
}

/// One execution record, bounded to 10 per game.
#[derive(Clone, Debug)]
pub struct ExecutionRecord {
    pub id: i64,
    pub game_id: String,
    pub game_name: String,
    /// Exact text handed to the shell.
    pub command: String,
    pub working_dir: String,
    pub pid: u32,
    pub started: String,
    pub finished: Option<String>,
    pub outcome: Option<Outcome>,
    /// Wording recorded at completion time, so stored records keep their text.
    pub outcome_text: Option<String>,
    pub stdout: String,
    pub stderr: String,
    pub truncated: bool,
    pub no_output: bool,
}

impl ExecutionRecord {
    pub fn to_text(&self) -> String {
        let mut out = String::with_capacity(1024);
        out.push_str(&format!("Game:     {}\n", self.game_name));
        out.push_str(&format!("Command:  {}\n", self.command));
        out.push_str(&format!("Workdir:  {}\n", self.working_dir));
        out.push_str(&format!("PID:      {}\n", self.pid));
        out.push_str(&format!("Started:  {}\n", self.started));
        out.push_str(&format!(
            "Finished: {}\n",
            self.finished.as_deref().unwrap_or("(still running)")
        ));
        out.push_str(&format!(
            "Result:   {}\n",
            self.outcome_text
                .clone()
                .or_else(|| self.outcome.as_ref().map(Outcome::describe))
                .unwrap_or_else(|| "running".to_string())
        ));
        out.push_str("\n--- stdout/stderr ---\n");
        if self.stdout.is_empty() && self.stderr.is_empty() {
            out.push_str("(No output captured)\n");
        } else {
            out.push_str(&self.stdout);
            out.push_str(&self.stderr);
        }
        if self.truncated {
            out.push_str("\n(capture limit reached; output was truncated)\n");
        }
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn environment_lines_are_validated_not_silently_dropped() {
        let mut game = Game::new("g", "G", "true");
        game.environment = "FOO=bar\n# comment\n\nBAR=\"two words\"\n".to_string();
        assert_eq!(
            game.environment_pairs().unwrap(),
            vec![
                ("FOO".to_string(), "bar".to_string()),
                ("BAR".to_string(), "two words".to_string())
            ]
        );
        game.environment = "BROKEN\n".to_string();
        assert!(game.environment_pairs().is_err());
        game.environment = "1BAD=x\n".to_string();
        assert!(game.environment_pairs().is_err());
    }

    #[test]
    fn auto_runner_label_is_derived_from_the_command() {
        let game = Game::new("g", "G", "flatpak run org.gnome.Chess");
        assert_eq!(game.runner_label(), "Native");
        let game = Game::new("g", "G", "wine \"/games/My Game/game.exe\"");
        assert_eq!(game.runner_label(), "Wine");
    }
}
