//! Execution log helpers.
//!
//! Execution records are stored in SQLite and bounded per game. This module
//! holds the parts that are not storage: how a live run is summarised, and how a
//! record is rendered as text for copying.

use crate::core::game::ExecutionRecord;

/// What the log viewer shows for the currently running process.
pub struct LiveView {
    pub command: String,
    pub working_dir: String,
    pub pid: u32,
    pub started: String,
    pub elapsed_label: String,
    pub stdout: String,
    pub stderr: String,
}

impl LiveView {
    pub fn as_text(&self, game_name: &str) -> String {
        let mut out = String::with_capacity(512);
        out.push_str(&format!("Game:     {game_name}\n"));
        out.push_str(&format!("Command:  {}\n", self.command));
        out.push_str(&format!("Workdir:  {}\n", self.working_dir));
        out.push_str(&format!("PID:      {}\n", self.pid));
        out.push_str(&format!("Started:  {}\n", self.started));
        out.push_str(&format!("Elapsed:  {}\n", self.elapsed_label));
        out.push_str("Result:   still running\n");
        out.push_str("\n--- stdout/stderr (live) ---\n");
        if self.stdout.is_empty() && self.stderr.is_empty() {
            out.push_str("(No output captured)\n");
        } else {
            out.push_str(&self.stdout);
            out.push_str(&self.stderr);
        }
        out
    }
}

/// Label for a record in the run list: `started` on one line, outcome on the next.
pub fn list_label(record: &ExecutionRecord) -> String {
    let status = match (&record.finished, &record.outcome_text) {
        (Some(_), Some(text)) => text.clone(),
        (Some(_), None) => "finished".to_string(),
        (None, _) => "running".to_string(),
    };
    format!("{}\n{}", record.started, status)
}

/// Clamp a string to `limit` characters, appending an ellipsis when cut.
pub fn clamp(text: &str, limit: usize) -> String {
    match text.char_indices().nth(limit) {
        Some((idx, _)) => format!("{}…", &text[..idx]),
        None => text.to_string(),
    }
}
