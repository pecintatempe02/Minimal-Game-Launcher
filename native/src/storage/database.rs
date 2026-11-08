//! SQLite library storage.
//!
//! Two tables:
//! * `games` — one row per saved game, including its full launch command;
//! * `runs` — execution records, bounded to 10 per game.
//!
//! Writes happen on the UI thread only when the user changes something. The
//! schema carries a version so a database written by the previous build is
//! backed up and migrated before any new table replaces it.

use std::path::{Path, PathBuf};

use rusqlite::{Connection, OptionalExtension, params};

use crate::core::game::{ExecutionRecord, Game, Outcome, RunStatus, RunnerLabel};
use crate::core::paths;

/// Records retained per game.
pub const MAX_RECORDS_PER_GAME: usize = 10;

const SCHEMA_VERSION: i32 = 2;

pub struct Database {
    conn: Connection,
    path: PathBuf,
    /// Things the user should know about migration/import, never swallowed.
    notes: Vec<String>,
}

/// Result of reading a legacy Python library.
#[derive(Clone, Debug, Default)]
pub struct ImportReport {
    pub imported: usize,
    pub skipped: usize,
    pub errors: Vec<String>,
}

impl Database {
    /// Open (creating if needed) the library database.
    pub fn open(path: &Path) -> Result<Self, String> {
        if let Some(parent) = path.parent() {
            paths::ensure_dir(parent)
                .map_err(|e| format!("Could not create {}: {e}", parent.display()))?;
        }
        let existed = path.is_file();
        let conn = Connection::open(path)
            .map_err(|e| format!("Could not open the library {}: {e}", path.display()))?;
        let mut db = Self {
            conn,
            path: path.to_path_buf(),
            notes: Vec::new(),
        };
        db.configure()?;
        let version = db.schema_version()?;
        if version < SCHEMA_VERSION {
            if existed && version > 0 {
                db.backup()?;
            }
            db.migrate_from(version)?;
        } else if version > SCHEMA_VERSION {
            return Err(format!(
                "The library {} was written by a newer version of MGL (schema {version}).",
                path.display()
            ));
        }
        Ok(db)
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    pub fn notes(&self) -> &[String] {
        &self.notes
    }

    fn configure(&mut self) -> Result<(), String> {
        self.conn
            .pragma_update(None, "journal_mode", "WAL")
            .map_err(|e| e.to_string())?;
        self.conn
            .pragma_update(None, "synchronous", "NORMAL")
            .map_err(|e| e.to_string())?;
        self.conn
            .pragma_update(None, "foreign_keys", "ON")
            .map_err(|e| e.to_string())?;
        Ok(())
    }

    fn schema_version(&self) -> Result<i32, String> {
        self.conn
            .query_row("PRAGMA user_version", [], |row| row.get(0))
            .map_err(|e| e.to_string())
    }

    fn table_exists(&self, name: &str) -> Result<bool, String> {
        let count: i64 = self
            .conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = ?1",
                params![name],
                |row| row.get(0),
            )
            .map_err(|e| e.to_string())?;
        Ok(count > 0)
    }

    /// Copy a consistent snapshot next to the database before any destructive
    /// schema change. `VACUUM INTO` includes committed WAL pages.
    fn backup(&mut self) -> Result<(), String> {
        let stamp = crate::core::library::now_stamp().replace([' ', ':'], "-");
        let name = self
            .path
            .file_name()
            .map(|name| name.to_string_lossy().into_owned())
            .unwrap_or_else(|| "mgl.db".to_string());
        let backup = self.path.with_file_name(format!("{name}.bak-{stamp}"));
        if backup.exists() {
            return Ok(());
        }
        let target = backup.to_string_lossy().into_owned();
        self.conn
            .execute("VACUUM INTO ?1", params![target])
            .map_err(|e| format!("Could not back up the library to {}: {e}", backup.display()))?;
        self.notes
            .push(format!("Library backed up to {}.", paths::tildify(&backup)));
        Ok(())
    }

    fn migrate_from(&mut self, version: i32) -> Result<(), String> {
        let has_games = self.table_exists("games")?;
        if version == 1 && has_games {
            self.migrate_v1()
        } else {
            self.create_schema()
        }
    }

    fn create_schema(&mut self) -> Result<(), String> {
        self.conn
            .execute_batch(
                r#"
                CREATE TABLE IF NOT EXISTS games (
                    id           TEXT PRIMARY KEY NOT NULL,
                    name         TEXT NOT NULL,
                    command      TEXT NOT NULL,
                    working_dir  TEXT NOT NULL DEFAULT '',
                    environment  TEXT NOT NULL DEFAULT '',
                    notes        TEXT NOT NULL DEFAULT '',
                    runner       TEXT NOT NULL DEFAULT 'auto',
                    cover        TEXT,
                    cover_thumb  TEXT,
                    hue          REAL NOT NULL DEFAULT 210,
                    created_at   TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS runs (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    game_id      TEXT NOT NULL REFERENCES games(id) ON DELETE CASCADE,
                    command      TEXT NOT NULL,
                    working_dir  TEXT NOT NULL DEFAULT '',
                    pid          INTEGER NOT NULL,
                    started      TEXT NOT NULL,
                    finished     TEXT,
                    outcome      TEXT,
                    outcome_code INTEGER,
                    stdout       TEXT NOT NULL DEFAULT '',
                    stderr       TEXT NOT NULL DEFAULT '',
                    truncated    INTEGER NOT NULL DEFAULT 0,
                    no_output    INTEGER NOT NULL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS runs_game_started
                    ON runs(game_id, started DESC);
                "#,
            )
            .map_err(|e| format!("Could not create the library schema: {e}"))?;
        self.conn
            .pragma_update(None, "user_version", SCHEMA_VERSION)
            .map_err(|e| e.to_string())?;
        Ok(())
    }

    fn migrate_v1(&mut self) -> Result<(), String> {
        // Read every old row before touching the old tables. If a row cannot be
        // represented, it is reported instead of being dropped without a word.
        let mut games: Vec<Game> = Vec::new();
        {
            let mut stmt = self
                .conn
                .prepare(
                    "SELECT id, name, kind, executable, working_dir, prefix_strategy,
                            external_prefix, custom_command, cover, hue, created_at
                     FROM games ORDER BY created_at ASC",
                )
                .map_err(|e| format!("Could not read the old game records: {e}"))?;
            let rows = stmt
                .query_map([], |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, String>(1)?,
                        row.get::<_, String>(2)?,
                        row.get::<_, String>(3)?,
                        row.get::<_, String>(4)?,
                        row.get::<_, String>(5)?,
                        row.get::<_, Option<String>>(6)?,
                        row.get::<_, Option<String>>(7)?,
                        row.get::<_, Option<String>>(8)?,
                        row.get::<_, f32>(9)?,
                        row.get::<_, String>(10)?,
                    ))
                })
                .map_err(|e| format!("Could not read the old game records: {e}"))?;
            for row in rows {
                match row {
                    Ok((
                        id,
                        name,
                        kind,
                        executable,
                        working_dir,
                        strategy,
                        external_prefix,
                        custom_command,
                        cover,
                        hue,
                        created_at,
                    )) => {
                        let command = match custom_command.filter(|text| !text.trim().is_empty()) {
                            Some(command) => command,
                            None => generated_v1_command(
                                &id,
                                &kind,
                                &executable,
                                &strategy,
                                external_prefix.as_deref(),
                            ),
                        };
                        let mut game = Game::new(id.clone(), name, command);
                        game.working_dir = working_dir;
                        game.cover = cover.filter(|path| !path.trim().is_empty());
                        game.runner = if kind.eq_ignore_ascii_case("wine") {
                            RunnerLabel::Wine
                        } else {
                            RunnerLabel::Native
                        };
                        game.hue = hue;
                        game.created_at = created_at;
                        games.push(game);
                    }
                    Err(error) => self
                        .notes
                        .push(format!("One old game record could not be read: {error}")),
                }
            }
        }

        let mut runs: Vec<(String, ExecutionRecord)> = Vec::new();
        if self.table_exists("runs")? {
            let mut stmt = self
                .conn
                .prepare(
                    "SELECT game_id, command, working_dir, pid, started, finished,
                            outcome, outcome_code, stdout, stderr, truncated, no_output
                     FROM runs ORDER BY id ASC",
                )
                .map_err(|e| format!("Could not read the old execution records: {e}"))?;
            let rows = stmt
                .query_map([], |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        ExecutionRecord {
                            id: 0,
                            game_id: row.get(0)?,
                            game_name: String::new(),
                            command: row.get(1)?,
                            working_dir: row.get(2)?,
                            pid: row.get::<_, i64>(3)? as u32,
                            started: row.get(4)?,
                            finished: row.get(5)?,
                            outcome_text: row.get(6)?,
                            outcome: row.get::<_, Option<i32>>(7).ok().flatten().map(|code| {
                                if code >= 128 {
                                    Outcome::Signal(code - 128)
                                } else {
                                    Outcome::Exit(code)
                                }
                            }),
                            stdout: row.get(8)?,
                            stderr: row.get(9)?,
                            truncated: row.get::<_, i64>(10)? != 0,
                            no_output: row.get::<_, i64>(11)? != 0,
                        },
                    ))
                })
                .map_err(|e| format!("Could not read the old execution records: {e}"))?;
            for row in rows.flatten() {
                runs.push(row);
            }
        }

        // The backup taken by `backup()` is the safety net for dropping the old
        // tables. Creating the new schema under the same names cannot preserve
        // the old column layout, so a same-file migration must replace them.
        self.conn
            .execute_batch(
                "DROP TABLE IF EXISTS runs;
                 DROP TABLE IF EXISTS games;",
            )
            .map_err(|e| format!("Could not replace the old schema: {e}"))?;
        self.create_schema()?;

        let mut migrated = 0usize;
        for game in &games {
            if let Err(error) = self.save_game(game) {
                self.notes
                    .push(format!("Could not migrate game “{}”: {error}", game.name));
            } else {
                migrated += 1;
            }
        }
        let mut run_errors = 0usize;
        for (_, mut run) in runs {
            if let Some(game) = games.iter().find(|game| game.id == run.game_id) {
                run.game_name = game.name.clone();
            }
            // A run whose game could not be migrated has no row to reference.
            if !games.iter().any(|game| game.id == run.game_id) {
                continue;
            }
            match self.begin_run(&run) {
                Ok(id) => {
                    if let Some(outcome) = &run.outcome {
                        let _ = self.finish_run(
                            id,
                            run.finished.as_deref().unwrap_or(&run.started),
                            outcome,
                            &run.stdout,
                            &run.stderr,
                            run.truncated,
                            run.no_output,
                        );
                    }
                }
                Err(error) => {
                    run_errors += 1;
                    if run_errors <= 3 {
                        self.notes
                            .push(format!("Could not migrate one execution record: {error}"));
                    }
                }
            }
        }
        self.notes.push(format!(
            "Migrated {migrated} game(s) from the previous Rust schema."
        ));
        Ok(())
    }

    // ------------------------------------------------------------------ games

    pub fn load_games(&self) -> Result<Vec<Game>, String> {
        let mut stmt = self
            .conn
            .prepare(
                "SELECT id, name, command, working_dir, environment, notes, runner,
                        cover, cover_thumb, hue, created_at
                 FROM games ORDER BY created_at ASC",
            )
            .map_err(|e| e.to_string())?;
        let rows = stmt
            .query_map([], |row| {
                let runner: String = row.get(6)?;
                Ok(Game {
                    id: row.get(0)?,
                    name: row.get(1)?,
                    command: row.get(2)?,
                    working_dir: row.get(3)?,
                    environment: row.get(4)?,
                    notes: row.get(5)?,
                    runner: RunnerLabel::parse(&runner),
                    cover: row.get(7)?,
                    cover_thumb: row.get(8)?,
                    hue: row.get(9)?,
                    created_at: row.get(10)?,
                    status: RunStatus::Idle,
                })
            })
            .map_err(|e| e.to_string())?;
        let mut games = Vec::new();
        for game in rows {
            games.push(game.map_err(|e| e.to_string())?);
        }
        Ok(games)
    }

    /// Insert or replace a game row.
    pub fn save_game(&self, game: &Game) -> Result<(), String> {
        self.conn
            .execute(
                "INSERT INTO games (id, name, command, working_dir, environment, notes,
                                    runner, cover, cover_thumb, hue, created_at)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11)
                 ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    command = excluded.command,
                    working_dir = excluded.working_dir,
                    environment = excluded.environment,
                    notes = excluded.notes,
                    runner = excluded.runner,
                    cover = excluded.cover,
                    cover_thumb = excluded.cover_thumb,
                    hue = excluded.hue",
                params![
                    game.id,
                    game.name,
                    game.command,
                    game.working_dir,
                    game.environment,
                    game.notes,
                    game.runner.as_str(),
                    game.cover,
                    game.cover_thumb,
                    game.hue,
                    if game.created_at.is_empty() {
                        crate::core::library::now_stamp()
                    } else {
                        game.created_at.clone()
                    },
                ],
            )
            .map_err(|e| format!("Could not save the game: {e}"))?;
        Ok(())
    }

    /// Remove a game row and (through the foreign key) its executions.
    pub fn delete_game(&self, id: &str) -> Result<(), String> {
        self.conn
            .execute("DELETE FROM games WHERE id = ?1", params![id])
            .map_err(|e| format!("Could not remove the game: {e}"))?;
        Ok(())
    }

    // ------------------------------------------------------------------- runs

    /// Insert a started run and trim the game to the retained maximum.
    pub fn begin_run(&self, record: &ExecutionRecord) -> Result<i64, String> {
        self.conn
            .execute(
                "INSERT INTO runs (game_id, command, working_dir, pid, started,
                                   finished, outcome, outcome_code, stdout, stderr,
                                   truncated, no_output)
                 VALUES (?1, ?2, ?3, ?4, ?5, NULL, NULL, NULL, '', '', 0, 0)",
                params![
                    record.game_id,
                    record.command,
                    record.working_dir,
                    record.pid,
                    record.started,
                ],
            )
            .map_err(|e| format!("Could not record the launch: {e}"))?;
        let id = self.conn.last_insert_rowid();
        self.trim_runs(&record.game_id)?;
        Ok(id)
    }

    /// Complete a run with its outcome and captured output.
    #[allow(clippy::too_many_arguments)]
    pub fn finish_run(
        &self,
        id: i64,
        finished: &str,
        outcome: &Outcome,
        stdout: &str,
        stderr: &str,
        truncated: bool,
        no_output: bool,
    ) -> Result<(), String> {
        self.conn
            .execute(
                "UPDATE runs SET finished = ?2, outcome = ?3, outcome_code = ?4,
                                 stdout = ?5, stderr = ?6, truncated = ?7, no_output = ?8
                 WHERE id = ?1",
                params![
                    id,
                    finished,
                    outcome.describe(),
                    outcome.as_i32(),
                    stdout,
                    stderr,
                    truncated as i64,
                    no_output as i64,
                ],
            )
            .map_err(|e| format!("Could not record the result: {e}"))?;
        Ok(())
    }

    fn trim_runs(&self, game_id: &str) -> Result<(), String> {
        self.conn
            .execute(
                "DELETE FROM runs WHERE game_id = ?1 AND id NOT IN (
                    SELECT id FROM runs WHERE game_id = ?1 ORDER BY id DESC LIMIT ?2
                 )",
                params![game_id, MAX_RECORDS_PER_GAME as i64],
            )
            .map_err(|e| format!("Could not trim the execution records: {e}"))?;
        Ok(())
    }

    /// Execution records for one game, newest first.
    pub fn load_runs(
        &self,
        game_id: &str,
        game_name: &str,
    ) -> Result<Vec<ExecutionRecord>, String> {
        let mut stmt = self
            .conn
            .prepare(
                "SELECT id, game_id, command, working_dir, pid, started,
                        finished, outcome, outcome_code, stdout, stderr, truncated, no_output
                 FROM runs WHERE game_id = ?1 ORDER BY id DESC LIMIT ?2",
            )
            .map_err(|e| e.to_string())?;
        let rows = stmt
            .query_map(params![game_id, MAX_RECORDS_PER_GAME as i64], |row| {
                let outcome_text: Option<String> = row.get(7)?;
                let outcome_code: Option<i32> = row.get(8)?;
                Ok(ExecutionRecord {
                    id: row.get(0)?,
                    game_id: row.get(1)?,
                    game_name: game_name.to_string(),
                    command: row.get(2)?,
                    working_dir: row.get(3)?,
                    pid: row.get::<_, i64>(4)? as u32,
                    started: row.get(5)?,
                    finished: row.get(6)?,
                    outcome_text,
                    outcome: outcome_code.map(|code| {
                        if code >= 128 {
                            Outcome::Signal(code - 128)
                        } else {
                            Outcome::Exit(code)
                        }
                    }),
                    stdout: row.get(9)?,
                    stderr: row.get(10)?,
                    truncated: row.get::<_, i64>(11)? != 0,
                    no_output: row.get::<_, i64>(12)? != 0,
                })
            })
            .map_err(|e| e.to_string())?;
        let mut runs = Vec::new();
        for run in rows {
            runs.push(run.map_err(|e| e.to_string())?);
        }
        Ok(runs)
    }

    /// Number of records kept for a game.
    pub fn count_runs(&self, game_id: &str) -> Result<usize, String> {
        let count: i64 = self
            .conn
            .query_row(
                "SELECT COUNT(*) FROM runs WHERE game_id = ?1",
                params![game_id],
                |row| row.get(0),
            )
            .map_err(|e| e.to_string())?;
        Ok(count as usize)
    }

    /// Delete every record for a game.
    pub fn clear_runs(&self, game_id: &str) -> Result<(), String> {
        self.conn
            .execute("DELETE FROM runs WHERE game_id = ?1", params![game_id])
            .map_err(|e| format!("Could not clear the logs: {e}"))?;
        Ok(())
    }

    /// Delete one record.
    pub fn delete_run(&self, id: i64) -> Result<(), String> {
        self.conn
            .execute("DELETE FROM runs WHERE id = ?1", params![id])
            .map_err(|e| format!("Could not delete the record: {e}"))?;
        Ok(())
    }

    /// Adopt games from the previous Python implementation's database.
    ///
    /// The old database is opened **read-only** and never modified. Each game is
    /// converted into one full command; the launch arguments and environment are
    /// preserved. Records that cannot be represented are reported.
    pub fn import_legacy(&self, legacy: &Path) -> Result<ImportReport, String> {
        let mut report = ImportReport::default();
        if !legacy.is_file() {
            return Ok(report);
        }
        let conn = Connection::open_with_flags(
            legacy,
            rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY | rusqlite::OpenFlags::SQLITE_OPEN_URI,
        )
        .map_err(|e| format!("Could not read the old library: {e}"))?;
        let mut stmt = match conn.prepare("SELECT * FROM games") {
            Ok(stmt) => stmt,
            Err(_) => return Ok(report),
        };
        let columns: Vec<String> = stmt
            .column_names()
            .iter()
            .map(|name| name.to_ascii_lowercase())
            .collect();
        let index = |name: &str| columns.iter().position(|column| column == name);
        let rows = stmt
            .query_map([], |row| {
                let text = |name: &str| -> Option<String> {
                    index(name)
                        .and_then(|position| row.get::<_, Option<String>>(position).ok())
                        .flatten()
                };
                Ok((
                    text("id"),
                    text("name"),
                    text("platform"),
                    text("executable"),
                    text("working_directory"),
                    text("launch_arguments"),
                    text("cover_path"),
                    text("wine_prefix_path"),
                    text("environment"),
                    text("created_at"),
                ))
            })
            .map_err(|e| e.to_string())?;
        for row in rows {
            let row = match row {
                Ok(row) => row,
                Err(error) => {
                    report.skipped += 1;
                    report
                        .errors
                        .push(format!("One old record could not be read: {error}"));
                    continue;
                }
            };
            let (
                id,
                name,
                platform,
                executable,
                working_dir,
                arguments,
                cover,
                prefix,
                environment,
                created_at,
            ) = row;
            let (Some(id), Some(name), Some(executable)) = (id, name, executable) else {
                report.skipped += 1;
                report.errors.push(
                    "One old record had no id, name or executable and was skipped.".to_string(),
                );
                continue;
            };
            let exists: Option<String> = self
                .conn
                .query_row("SELECT id FROM games WHERE id = ?1", params![id], |r| {
                    r.get(0)
                })
                .optional()
                .map_err(|e| e.to_string())?;
            if exists.is_some() {
                report.skipped += 1;
                continue;
            }
            let windows = platform
                .as_deref()
                .is_some_and(|platform| platform.eq_ignore_ascii_case("windows"));
            let command = legacy_command(
                &executable,
                arguments.as_deref().unwrap_or(""),
                windows,
                prefix.as_deref(),
            );
            let mut game = Game::new(id.clone(), name, command);
            game.working_dir = working_dir.unwrap_or_default();
            game.environment = legacy_environment(environment.as_deref());
            game.cover = cover.filter(|path| !path.trim().is_empty());
            game.runner = if windows {
                RunnerLabel::Wine
            } else {
                RunnerLabel::Native
            };
            game.created_at = created_at.unwrap_or_else(crate::core::library::now_stamp);
            match self.save_game(&game) {
                Ok(()) => report.imported += 1,
                Err(error) => {
                    report.skipped += 1;
                    report
                        .errors
                        .push(format!("Could not import “{}”: {error}", game.name));
                }
            }
        }
        Ok(report)
    }
}

fn parse_optional(value: Option<String>) -> Option<String> {
    value.filter(|text| !text.trim().is_empty())
}

fn generated_v1_command(
    id: &str,
    kind: &str,
    executable: &str,
    strategy: &str,
    external_prefix: Option<&str>,
) -> String {
    let executable = paths::shell_quote(executable);
    if !kind.eq_ignore_ascii_case("wine") {
        return executable;
    }
    let prefix = match strategy {
        "external" => external_prefix
            .filter(|path| !path.trim().is_empty())
            .map(PathBuf::from)
            .unwrap_or_else(|| paths::data_dir().join("prefixes/.shared")),
        "shared" => paths::data_dir().join("prefixes/.shared"),
        _ => paths::data_dir().join("prefixes").join(id),
    };
    format!(
        "WINEPREFIX={} wine {}",
        paths::shell_quote(&prefix.to_string_lossy()),
        executable
    )
}

fn legacy_command(
    executable: &str,
    arguments: &str,
    windows: bool,
    prefix: Option<&str>,
) -> String {
    let base = if windows {
        let executable = paths::shell_quote(executable);
        match parse_optional(prefix.map(str::to_string)) {
            Some(prefix) => format!(
                "WINEPREFIX={} wine {}",
                paths::shell_quote(&prefix),
                executable
            ),
            None => format!("wine {executable}"),
        }
    } else if executable.contains('/') || executable.contains(char::is_whitespace) {
        paths::shell_quote(executable)
    } else {
        executable.to_string()
    };
    let arguments = arguments.trim();
    if arguments.is_empty() {
        base
    } else {
        format!("{base} {arguments}")
    }
}

fn legacy_environment(raw: Option<&str>) -> String {
    let Some(raw) = raw else {
        return String::new();
    };
    let Ok(value) = serde_json::from_str::<serde_json::Value>(raw) else {
        return String::new();
    };
    let Some(object) = value.as_object() else {
        return String::new();
    };
    let mut lines = String::new();
    for (key, value) in object {
        let value = match value {
            serde_json::Value::String(text) => text.clone(),
            other => other.to_string(),
        };
        lines.push_str(&format!("{key}={value}\n"));
    }
    lines
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("mgl-db-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn game_round_trip_preserves_the_command_and_cover() {
        let dir = temp("roundtrip");
        let db = Database::open(&dir.join("mgl.db")).unwrap();
        let mut game = Game::new("game", "Game", "flatpak run org.gnome.Chess");
        game.working_dir = "/tmp".to_string();
        game.environment = "FOO=bar\n".to_string();
        game.notes = "note".to_string();
        game.runner = RunnerLabel::Custom;
        game.cover = Some("/tmp/cover.png".to_string());
        game.cover_thumb = Some("/tmp/thumb.png".to_string());
        db.save_game(&game).unwrap();
        let loaded = db.load_games().unwrap();
        assert_eq!(loaded.len(), 1);
        assert_eq!(loaded[0].command, game.command);
        assert_eq!(loaded[0].environment, game.environment);
        assert_eq!(loaded[0].notes, game.notes);
        assert_eq!(loaded[0].runner, RunnerLabel::Custom);
        assert_eq!(loaded[0].cover, game.cover);
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn only_ten_runs_are_retained_per_game() {
        let dir = temp("runs");
        let db = Database::open(&dir.join("mgl.db")).unwrap();
        db.save_game(&Game::new("g", "G", "true")).unwrap();
        for index in 0..15 {
            let record = ExecutionRecord {
                id: 0,
                game_id: "g".to_string(),
                game_name: "G".to_string(),
                command: format!("echo {index}"),
                working_dir: "/tmp".to_string(),
                pid: index + 1,
                started: format!("2026-01-01 00:00:{index:02}"),
                finished: None,
                outcome: None,
                outcome_text: None,
                stdout: String::new(),
                stderr: String::new(),
                truncated: false,
                no_output: false,
            };
            db.begin_run(&record).unwrap();
        }
        assert_eq!(db.count_runs("g").unwrap(), MAX_RECORDS_PER_GAME);
        let runs = db.load_runs("g", "G").unwrap();
        assert_eq!(runs.len(), MAX_RECORDS_PER_GAME);
        assert_eq!(runs[0].command, "echo 14");
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn a_version_one_database_is_backed_up_and_migrated() {
        let dir = temp("migrate");
        let path = dir.join("mgl.db");
        {
            let conn = Connection::open(&path).unwrap();
            conn.execute_batch(
                r#"
                CREATE TABLE games (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
                    executable TEXT NOT NULL, working_dir TEXT NOT NULL,
                    runner TEXT NOT NULL DEFAULT '', prefix_strategy TEXT NOT NULL DEFAULT 'pergame',
                    external_prefix TEXT, custom_command TEXT, custom_stale INTEGER NOT NULL DEFAULT 0,
                    gamemode INTEGER NOT NULL DEFAULT 0, mangohud INTEGER NOT NULL DEFAULT 0,
                    cover TEXT, files_ownership TEXT NOT NULL DEFAULT 'external',
                    prefix_ownership TEXT NOT NULL DEFAULT 'managed_by_mgl',
                    hue REAL NOT NULL DEFAULT 210, created_at TEXT NOT NULL
                );
                CREATE TABLE runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    game_id TEXT NOT NULL, command TEXT NOT NULL, working_dir TEXT NOT NULL,
                    runner TEXT NOT NULL DEFAULT '', prefix TEXT NOT NULL DEFAULT '',
                    pid INTEGER NOT NULL, started TEXT NOT NULL, finished TEXT,
                    outcome TEXT, outcome_code INTEGER, stdout TEXT NOT NULL DEFAULT '',
                    stderr TEXT NOT NULL DEFAULT '', truncated INTEGER NOT NULL DEFAULT 0,
                    no_output INTEGER NOT NULL DEFAULT 0
                );
                INSERT INTO games VALUES
                    ('linux', 'Linux Game', 'linux', '/opt/game/run', '/opt/game', '', 'pergame',
                     NULL, NULL, 0, 0, 0, NULL, 'external', 'managed_by_mgl', 12.0, '2025-01-01 00:00:00'),
                    ('wine', 'Wine Game', 'wine', '/games/A B/game.exe', '/games/A B', 'system', 'external',
                     '/prefixes/wine', 'echo custom', 0, 0, 0, '/old/cover.png', 'external', 'external',
                     34.0, '2025-01-02 00:00:00');
                INSERT INTO runs (game_id, command, working_dir, pid, started, finished, outcome, outcome_code)
                    VALUES ('linux', '/opt/game/run', '/opt/game', 10, '2025-01-03 00:00:00',
                            '2025-01-03 00:00:01', 'exit status 0', 0);
                PRAGMA user_version = 1;
                "#,
            )
            .unwrap();
        }
        let db = Database::open(&path).unwrap();
        let games = db.load_games().unwrap();
        assert_eq!(games.len(), 2);
        let linux = games.iter().find(|game| game.id == "linux").unwrap();
        assert_eq!(linux.command, "\"/opt/game/run\"");
        assert_eq!(linux.working_dir, "/opt/game");
        let wine = games.iter().find(|game| game.id == "wine").unwrap();
        assert_eq!(wine.command, "echo custom");
        assert_eq!(wine.runner, RunnerLabel::Wine);
        assert_eq!(wine.cover.as_deref(), Some("/old/cover.png"));
        assert_eq!(db.load_runs("linux", "Linux Game").unwrap().len(), 1);
        // A backup file exists next to the database.
        let backups: Vec<_> = std::fs::read_dir(&dir)
            .unwrap()
            .flatten()
            .filter(|entry| entry.file_name().to_string_lossy().contains(".bak-"))
            .collect();
        assert_eq!(backups.len(), 1);
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn legacy_python_rows_import_with_their_environment() {
        let dir = temp("legacy");
        let legacy = dir.join("library.db");
        {
            let conn = Connection::open(&legacy).unwrap();
            conn.execute_batch(
                r#"
                CREATE TABLE games (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, platform TEXT NOT NULL,
                    executable TEXT NOT NULL, working_directory TEXT NOT NULL,
                    launch_arguments TEXT NOT NULL DEFAULT '', cover_path TEXT,
                    wine_prefix_path TEXT, gamemode_enabled INTEGER NOT NULL DEFAULT 1,
                    mangohud_enabled INTEGER NOT NULL DEFAULT 0,
                    environment TEXT NOT NULL DEFAULT '{}', show_in_library INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                INSERT INTO games (id, name, platform, executable, working_directory,
                                   launch_arguments, wine_prefix_path, environment, created_at, updated_at)
                VALUES ('chess', 'Chess', 'linux', 'flatpak',
                        '/home/user', 'run org.gnome.Chess', NULL,
                        '{"FOO":"bar"}', '2025-02-01 00:00:00', '2025-02-01 00:00:00');
                "#,
            )
            .unwrap();
        }
        let db = Database::open(&dir.join("mgl.db")).unwrap();
        let report = db.import_legacy(&legacy).unwrap();
        assert_eq!(report.imported, 1);
        let games = db.load_games().unwrap();
        assert_eq!(games.len(), 1);
        assert_eq!(games[0].command, "flatpak run org.gnome.Chess");
        assert_eq!(games[0].environment, "FOO=bar\n");
        assert_eq!(games[0].runner, RunnerLabel::Native);
        let _ = std::fs::remove_dir_all(dir);
    }
}
