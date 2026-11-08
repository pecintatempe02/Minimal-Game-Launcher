//! Launch supervision: one place that knows how a game becomes a process, how
//! its record is written, and how it is stopped again.
//!
//! The launcher owns at most one [`ProcessHandle`] per game. It never runs a
//! background poll loop: the UI asks it to advance on a timer that only exists
//! while something is running, and stops the timer when nothing is.

use std::collections::HashMap;
use std::time::{Duration, Instant};

use crate::core::game::{ExecutionRecord, Game, Outcome, RunStatus};
use crate::core::library::now_stamp;
use crate::core::process::{KILL_GRACE, ProcessHandle, STOP_GRACE};
use crate::storage::Database;

/// A launch in flight, plus the record it belongs to.
struct Active {
    handle: ProcessHandle,
    record_id: i64,
    game_id: String,
    game_name: String,
    command: String,
    workdir: String,
    started_stamp: String,
    /// When the `SIGTERM` grace period ends and the group gets `SIGKILL`.
    kill_at: Option<Instant>,
}

/// Result of asking to launch a game.
#[derive(Debug)]
pub struct LaunchOutcome {
    pub game_id: String,
    pub command: String,
    pub pid: Option<u32>,
    pub error: Option<String>,
}

/// Tracks running games and persists their execution records.
pub struct Launcher {
    active: HashMap<String, Active>,
}

impl Default for Launcher {
    fn default() -> Self {
        Self::new()
    }
}

impl Launcher {
    pub fn new() -> Self {
        Self {
            active: HashMap::new(),
        }
    }

    /// Whether a game is currently being supervised.
    pub fn is_active(&self, game_id: &str) -> bool {
        self.active.contains_key(game_id)
    }

    /// Whether any process is alive, so the UI knows if it needs a tick timer.
    pub fn any_active(&self) -> bool {
        !self.active.is_empty()
    }

    /// Start `game` exactly as its command says.
    ///
    /// The record is written before the spawn, and removed again if the process
    /// could not be created, so the log never shows a process that never existed.
    pub fn launch(&mut self, game: &mut Game, db: &Database) -> LaunchOutcome {
        if self.active.contains_key(&game.id) {
            return LaunchOutcome {
                game_id: game.id.clone(),
                command: String::new(),
                pid: None,
                error: Some("This game is already running.".to_string()),
            };
        }

        let command = game.command.clone();
        if let Err(error) = crate::core::command::validate_command(&command) {
            return self.fail(game, &command, error);
        }
        let workdir = match game.resolved_workdir() {
            Ok(path) => path.to_string_lossy().into_owned(),
            Err(error) => return self.fail(game, &command, error),
        };
        let environment = match game.environment_pairs() {
            Ok(pairs) => pairs,
            Err(error) => return self.fail(game, &command, error),
        };
        let env: Vec<(&str, String)> = environment
            .iter()
            .map(|(key, value)| (key.as_str(), value.clone()))
            .collect();

        let started_stamp = now_stamp();
        let placeholder = ExecutionRecord {
            id: 0,
            game_id: game.id.clone(),
            game_name: game.name.clone(),
            command: command.clone(),
            working_dir: workdir.clone(),
            pid: 0,
            started: started_stamp.clone(),
            finished: None,
            outcome: None,
            outcome_text: None,
            stdout: String::new(),
            stderr: String::new(),
            truncated: false,
            no_output: false,
        };
        if let Err(error) = db.save_game(game) {
            return self.fail(game, &command, error);
        }
        let record_id = match db.begin_run(&placeholder) {
            Ok(id) => id,
            Err(error) => return self.fail(game, &command, error),
        };

        let handle = match ProcessHandle::spawn(&command, std::path::Path::new(&workdir), &env) {
            Ok(handle) => handle,
            Err(error) => {
                let _ = db.delete_run(record_id);
                return self.fail(game, &command, error);
            }
        };

        let pid = handle.pid;
        let active = Active {
            handle,
            record_id,
            game_id: game.id.clone(),
            game_name: game.name.clone(),
            command: command.clone(),
            workdir,
            started_stamp: started_stamp.clone(),
            kill_at: None,
        };
        self.active.insert(game.id.clone(), active);
        game.status = RunStatus::Running {
            pid,
            started: started_stamp,
        };
        LaunchOutcome {
            game_id: game.id.clone(),
            command,
            pid: Some(pid),
            error: None,
        }
    }

    fn fail(&self, game: &mut Game, command: &str, error: String) -> LaunchOutcome {
        game.status = RunStatus::Finished {
            outcome: error.clone(),
        };
        LaunchOutcome {
            game_id: game.id.clone(),
            command: command.to_string(),
            pid: None,
            error: Some(error),
        }
    }

    /// Ask a running game to stop. This never blocks: it sends `SIGTERM` to the
    /// process group and arms the escalation deadline; the tick records the
    /// outcome once the process is actually gone.
    pub fn stop(&mut self, game: &mut Game, db: &Database) -> Option<Outcome> {
        let record_id = {
            let active = self.active.get_mut(&game.id)?;
            if active.kill_at.is_none() {
                active.kill_at = Some(Instant::now() + STOP_GRACE);
                active.handle.request_stop();
            }
            active.record_id
        };
        let observed = self.active.get_mut(&game.id)?.handle.try_wait()?;
        let observed = self.finalize_stop(&game.id, record_id, db, observed);
        game.status = RunStatus::Finished {
            outcome: observed.describe(),
        };
        Some(observed)
    }

    /// Reap stopped games without blocking; escalate to `SIGKILL` on lapse.
    pub fn poll_stopped(&mut self, games: &mut [Game], db: &Database) -> Vec<String> {
        if self.active.is_empty() {
            return Vec::new();
        }
        let mut finished = Vec::new();
        let due: Vec<String> = self
            .active
            .iter()
            .filter(|(_, active)| active.kill_at.is_some())
            .map(|(id, _)| id.clone())
            .collect();
        for id in due {
            let outcome = {
                let Some(active) = self.active.get_mut(&id) else {
                    continue;
                };
                match active.handle.try_wait() {
                    Some(outcome) => outcome,
                    None => {
                        if active.kill_at.is_some_and(|d| Instant::now() >= d) {
                            active.kill_at = Some(Instant::now() + KILL_GRACE);
                            active.handle.kill_group();
                        }
                        continue;
                    }
                }
            };
            let record_id = self.active.get(&id).map(|a| a.record_id);
            if let Some(record_id) = record_id {
                let observed = self.finalize_stop(&id, record_id, db, outcome);
                if let Some(game) = games.iter_mut().find(|g| g.id == id) {
                    game.status = RunStatus::Finished {
                        outcome: observed.describe(),
                    };
                }
                finished.push(id);
            }
        }
        finished
    }

    fn finalize_stop(
        &mut self,
        game_id: &str,
        record_id: i64,
        db: &Database,
        outcome: Outcome,
    ) -> Outcome {
        let Some(active) = self.active.remove(game_id) else {
            return outcome;
        };
        let (stdout, stderr, truncated) = active.handle.output();
        let no_output = active.handle.produced_no_output();
        let _ = db.finish_run(
            record_id,
            &now_stamp(),
            &outcome,
            &stdout,
            &stderr,
            truncated,
            no_output,
        );
        outcome
    }

    /// Advance supervision for every running game. Returns the ids that finished.
    pub fn poll(&mut self, games: &mut [Game], db: &Database) -> Vec<String> {
        if self.active.is_empty() {
            return Vec::new();
        }
        let mut finished = Vec::new();
        let ids: Vec<String> = self.active.keys().cloned().collect();
        for id in ids {
            let Some(active) = self.active.get_mut(&id) else {
                continue;
            };
            let Some(outcome) = active.handle.try_wait() else {
                continue;
            };
            let (stdout, stderr, truncated) = active.handle.output();
            let no_output = active.handle.produced_no_output();
            let finished_at = now_stamp();
            let _ = db.finish_run(
                active.record_id,
                &finished_at,
                &outcome,
                &stdout,
                &stderr,
                truncated,
                no_output,
            );
            let description = outcome.describe();
            let game_id = active.game_id.clone();
            self.active.remove(&id);
            if let Some(game) = games.iter_mut().find(|g| g.id == game_id) {
                game.status = RunStatus::Finished {
                    outcome: description,
                };
            }
            finished.push(game_id);
        }
        finished
    }

    pub fn elapsed(&self, game_id: &str) -> Option<Duration> {
        self.active.get(game_id).map(|a| a.handle.elapsed())
    }

    /// Stop everything, used on application shutdown. This is the one place a
    /// bounded wait is allowed: the app is going away.
    pub fn shutdown(&mut self, games: &mut [Game], db: &Database) {
        let ids: Vec<String> = self.active.keys().cloned().collect();
        for id in &ids {
            if let Some(active) = self.active.get_mut(id) {
                active.handle.request_stop();
            }
        }
        for id in ids {
            let Some(record_id) = self.active.get(&id).map(|a| a.record_id) else {
                continue;
            };
            let outcome = {
                let Some(active) = self.active.get_mut(&id) else {
                    continue;
                };
                match active.handle.wait_timeout(STOP_GRACE) {
                    Some(outcome) => outcome,
                    None => {
                        active.handle.kill_group();
                        active
                            .handle
                            .wait_timeout(Duration::from_millis(500))
                            .unwrap_or(Outcome::Stopped)
                    }
                }
            };
            let observed = self.finalize_stop(&id, record_id, db, outcome);
            if let Some(game) = games.iter_mut().find(|g| g.id == id) {
                game.status = RunStatus::Finished {
                    outcome: observed.describe(),
                };
            }
        }
    }

    /// Names captured for a still-running game, for the log viewer.
    pub fn live_snapshot(&self, game_id: &str) -> Option<(String, String, String)> {
        let active = self.active.get(game_id)?;
        let (stdout, stderr, _) = active.handle.output();
        Some((active.command.clone(), stdout, stderr))
    }

    /// Metadata of an active run, for a live log row.
    pub fn active_info(&self, game_id: &str) -> Option<ActiveInfo> {
        let active = self.active.get(game_id)?;
        Some(ActiveInfo {
            game_name: active.game_name.clone(),
            command: active.command.clone(),
            workdir: active.workdir.clone(),
            pid: active.handle.pid,
            started: active.started_stamp.clone(),
            elapsed: active.handle.elapsed(),
        })
    }
}

/// Snapshot of an active run for the log viewer.
#[derive(Clone, Debug)]
pub struct ActiveInfo {
    pub game_name: String,
    pub command: String,
    pub workdir: String,
    pub pid: u32,
    pub started: String,
    pub elapsed: Duration,
}
