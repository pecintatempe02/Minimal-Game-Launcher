//! Behaviour that can only be checked against real processes and real files.
//!
//! Run with `cargo test --release`. Everything here touches only a temporary
//! directory and short-lived children of the test process.

use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use mgl::app::actions;
use mgl::app::state::{AppState, Modal};
use mgl::core::cover;
use mgl::core::game::{Game, Outcome, RunStatus, RunnerLabel};
use mgl::core::launcher::Launcher;
use mgl::core::library::{self, Filter};
use mgl::core::{paths, process};
use mgl::storage::{Database, MAX_RECORDS_PER_GAME, SortOrder};
use mgl::ui::components::Action;
use mgl::ui::layout::Rect;
use rusqlite::Connection;

/// `paths` reads `XDG_DATA_HOME` on every call, so tests that point it at a
/// throwaway root must not overlap.
static ENV: Mutex<()> = Mutex::new(());

fn env_lock() -> std::sync::MutexGuard<'static, ()> {
    ENV.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
}

/// A scratch directory removed when the test ends, even if it panics.
struct TempDir(PathBuf);

impl std::ops::Deref for TempDir {
    type Target = Path;
    fn deref(&self) -> &Path {
        &self.0
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn temp_dir(name: &str) -> TempDir {
    let dir = std::env::temp_dir().join(format!("mgl-test-{}-{name}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).expect("create scratch directory");
    TempDir(dir)
}

fn png_bytes(width: u32, height: u32, rgba: &[u8]) -> Vec<u8> {
    let mut out = Vec::new();
    {
        let mut encoder = png::Encoder::new(&mut out, width, height);
        encoder.set_color(png::ColorType::Rgba);
        encoder.set_depth(png::BitDepth::Eight);
        let mut writer = encoder.write_header().unwrap();
        writer.write_image_data(rgba).unwrap();
    }
    out
}

// ------------------------------------------------------------------- process

#[test]
fn a_process_runs_verbatim_captures_both_streams_and_reports_its_exit() {
    let _guard = env_lock();
    let dir = temp_dir("process");
    let mut handle =
        process::ProcessHandle::spawn("printf 'out'; printf 'err' >&2; exit 7", &dir, &[])
            .expect("spawn");
    let deadline = Instant::now() + Duration::from_secs(10);
    let outcome = loop {
        if let Some(outcome) = handle.try_wait() {
            break outcome;
        }
        assert!(Instant::now() < deadline, "process did not exit");
        std::thread::sleep(Duration::from_millis(10));
    };
    assert_eq!(outcome, Outcome::Exit(7));
    let (stdout, stderr, truncated) = handle.output();
    assert_eq!(stdout, "out");
    assert_eq!(stderr, "err");
    assert!(!truncated);
    assert!(!handle.produced_no_output());
}

#[test]
fn shell_syntax_is_handed_to_the_shell_unchanged() {
    let _guard = env_lock();
    let dir = temp_dir("shell");
    let mut handle = process::ProcessHandle::spawn(
        "X=world; printf 'hello %s' \"$X\"",
        &dir,
        &[("GREETING", "ignored".to_string())],
    )
    .expect("spawn");
    let deadline = Instant::now() + Duration::from_secs(10);
    let outcome = loop {
        if let Some(outcome) = handle.try_wait() {
            break outcome;
        }
        assert!(Instant::now() < deadline, "process did not exit");
        std::thread::sleep(Duration::from_millis(10));
    };
    assert_eq!(outcome, Outcome::Exit(0));
    assert_eq!(handle.output().0, "hello world");
}

#[test]
fn a_missing_working_directory_refuses_to_spawn() {
    let _guard = env_lock();
    let error =
        process::ProcessHandle::spawn("true", Path::new("/definitely/not/a/directory/mgl"), &[])
            .err()
            .expect("must fail");
    assert!(error.contains("does not exist"), "{error}");
}

#[test]
fn environment_lines_are_validated_and_used() {
    let _guard = env_lock();
    let dir = temp_dir("env");
    let mut game = Game::new("g", "G", "printf '%s' \"$MGL_TEST\"");
    game.environment = "MGL_TEST=works\n# comment\n".to_string();
    game.working_dir = dir.to_string_lossy().into_owned();
    let db = Database::open(&dir.join("mgl.db")).unwrap();
    let mut launcher = Launcher::new();
    let outcome = launcher.launch(&mut game, &db);
    assert!(outcome.error.is_none(), "{:?}", outcome.error);
    let deadline = Instant::now() + Duration::from_secs(10);
    while game.is_running() {
        launcher.poll(std::slice::from_mut(&mut game), &db);
        assert!(Instant::now() < deadline, "process did not exit");
        std::thread::sleep(Duration::from_millis(10));
    }
    let runs = db.load_runs("g", "G").unwrap();
    assert_eq!(runs.len(), 1);
    assert_eq!(runs[0].stdout, "works");

    game.environment = "NO_EQUALS_SIGN\n".to_string();
    let outcome = launcher.launch(&mut game, &db);
    assert!(outcome.error.is_some());
    assert!(outcome.pid.is_none());
}

// ------------------------------------------------------------------- launcher

#[test]
fn launch_records_output_and_finishes_in_the_library() {
    let _guard = env_lock();
    let dir = temp_dir("launch");
    let mut game = Game::new("game", "Game", "printf 'hello\\n'; exit 3");
    game.working_dir = dir.to_string_lossy().into_owned();
    let db = Database::open(&dir.join("mgl.db")).unwrap();
    let mut launcher = Launcher::new();

    let outcome = launcher.launch(&mut game, &db);
    assert!(outcome.error.is_none(), "{:?}", outcome.error);
    let pid = outcome.pid.expect("pid");
    assert!(launcher.is_active("game"));
    assert!(matches!(game.status, RunStatus::Running { pid: p, .. } if p == pid));

    let deadline = Instant::now() + Duration::from_secs(10);
    while game.is_running() {
        launcher.poll(std::slice::from_mut(&mut game), &db);
        assert!(Instant::now() < deadline, "process did not exit");
        std::thread::sleep(Duration::from_millis(10));
    }
    assert!(!launcher.is_active("game"));
    let runs = db.load_runs("game", "Game").unwrap();
    assert_eq!(runs.len(), 1);
    assert!(runs[0].stdout.contains("hello"));
    assert_eq!(runs[0].outcome, Some(Outcome::Exit(3)));
    assert_eq!(runs[0].outcome_text.as_deref(), Some("exit status 3"));
    assert!(runs[0].finished.is_some());
}

#[test]
fn a_second_launch_of_the_same_game_is_refused() {
    let _guard = env_lock();
    let dir = temp_dir("launch-again");
    let mut game = Game::new("game", "Game", "sleep 2");
    game.working_dir = dir.to_string_lossy().into_owned();
    let db = Database::open(&dir.join("mgl.db")).unwrap();
    let mut launcher = Launcher::new();
    let first = launcher.launch(&mut game, &db);
    assert!(first.error.is_none());
    let second = launcher.launch(&mut game, &db);
    assert!(second.error.is_some());
    assert!(second.pid.is_none());
    launcher.shutdown(std::slice::from_mut(&mut game), &db);
}

// ---------------------------------------------------------------------- cover

#[test]
fn covers_are_copied_managed_and_thumbnail_is_square() {
    let _guard = env_lock();
    let root = temp_dir("covers-xdg");
    let source_dir = temp_dir("covers-source");
    // SAFETY: the test holds ENV, so no other test reads XDG_DATA_HOME here.
    unsafe { std::env::set_var("XDG_DATA_HOME", &*root) };

    let source = source_dir.join("art.png");
    std::fs::write(&source, png_bytes(8, 4, &[200, 30, 40, 255].repeat(32))).unwrap();
    let imported = cover::import(&source, "my-game").expect("import");
    assert!(imported.cover.is_file());
    assert!(imported.thumbnail.is_file());
    assert!(paths::is_managed_cover(&imported.cover));
    assert!(paths::is_managed_cover(&imported.thumbnail));
    assert_eq!(
        (imported.preview.width, imported.preview.height),
        (384, 384)
    );

    let preview = cover::load_preview(&imported.thumbnail).unwrap();
    assert_eq!((preview.width, preview.height), (384, 384));
    assert_eq!(preview.rgba.len(), 384 * 384 * 4);

    // Deleting through the model only touches managed files.
    let outside = source_dir.join("keep.txt");
    std::fs::write(&outside, b"keep").unwrap();
    cover::remove_paths(Some(&outside.to_string_lossy()), None);
    assert!(outside.is_file());

    cover::remove_for_game("my-game");
    assert!(!imported.cover.exists());
    assert!(!imported.thumbnail.exists());

    unsafe { std::env::remove_var("XDG_DATA_HOME") };
}

#[test]
fn a_non_image_file_is_rejected_without_writing_a_cover() {
    let _guard = env_lock();
    let root = temp_dir("covers-bad-xdg");
    let source_dir = temp_dir("covers-bad-source");
    unsafe { std::env::set_var("XDG_DATA_HOME", &*root) };
    let source = source_dir.join("not-an-image.txt");
    std::fs::write(&source, b"hello").unwrap();
    assert!(cover::import(&source, "bad").is_err());
    assert!(!paths::covers_dir().join("bad.png").exists());
    unsafe { std::env::remove_var("XDG_DATA_HOME") };
}

// ------------------------------------------------------------------- library

#[test]
fn filtering_searching_and_sorting_are_pure() {
    let mut native = Game::new("a", "Alpha", "flatpak run org.gnome.Chess");
    native.runner = RunnerLabel::Native;
    native.created_at = "2026-01-01 00:00:00".to_string();
    let mut wine = Game::new("b", "Beta", "wine game.exe");
    wine.runner = RunnerLabel::Wine;
    wine.created_at = "2026-01-02 00:00:00".to_string();
    let mut custom = Game::new("c", "Gamma", "gamescope -f");
    custom.runner = RunnerLabel::Custom;
    custom.created_at = "2026-01-03 00:00:00".to_string();
    let games = vec![native, wine, custom];

    assert_eq!(library::counts(&games), (3, 1, 1, 1));
    assert_eq!(
        library::visible(&games, Filter::Wine, "", SortOrder::Name),
        vec![1]
    );
    assert_eq!(
        library::visible(&games, Filter::All, "amm", SortOrder::Name),
        vec![2]
    );
    assert_eq!(
        library::visible(&games, Filter::All, "", SortOrder::Type).len(),
        3
    );
    assert_eq!(library::allocate_id("Gamma", &games), "gamma");
}

// ---------------------------------------------------------------- persistence

fn make_game(id: &str, name: &str) -> Game {
    let mut game = Game::new(id, name, "echo hello");
    game.working_dir = "/tmp".to_string();
    game.environment = "FOO=bar\n".to_string();
    game.notes = "note".to_string();
    game.runner = RunnerLabel::Custom;
    game
}

#[test]
fn game_options_menu_opens_closes_and_reaches_its_sheets() {
    let _guard = env_lock();
    let root = temp_dir("ui-menu-data");
    let config = temp_dir("ui-menu-config");
    unsafe {
        std::env::set_var("XDG_DATA_HOME", &*root);
        std::env::set_var("XDG_CONFIG_HOME", &*config);
    }
    let db = Database::open(&paths::database_path()).unwrap();
    db.save_game(&make_game("g", "G")).unwrap();
    drop(db);

    let mut state = AppState::load().unwrap();
    assert_eq!(state.games.len(), 1);
    let anchor = Rect::new(400.0, 100.0, 30.0, 30.0);
    let open = |state: &mut AppState| {
        actions::apply(
            state,
            Action::OpenGameMenu {
                game: "g".to_string(),
                anchor,
            },
        );
    };

    open(&mut state);
    let menu = state.game_menu.as_ref().expect("menu should open");
    assert_eq!(menu.game, "g");
    actions::apply(&mut state, Action::CloseGameMenu);
    assert!(state.game_menu.is_none());

    open(&mut state);
    actions::apply(&mut state, Action::OpenLogs("g".to_string()));
    assert!(state.game_menu.is_none(), "opening a sheet closes the menu");
    assert!(matches!(state.modal, Some(Modal::Logs(_))));
    actions::apply(&mut state, Action::Close);

    open(&mut state);
    actions::apply(&mut state, Action::EditGame("g".to_string()));
    assert!(state.game_menu.is_none());
    match &state.modal {
        Some(Modal::Editor(draft)) => assert_eq!(draft.command, "echo hello"),
        _ => panic!("edit command should open the editor"),
    }
    actions::apply(&mut state, Action::Close);

    open(&mut state);
    actions::apply(&mut state, Action::OpenRemove("g".to_string()));
    assert!(state.game_menu.is_none());
    assert!(matches!(state.modal, Some(Modal::Remove(_))));

    unsafe {
        std::env::remove_var("XDG_DATA_HOME");
        std::env::remove_var("XDG_CONFIG_HOME");
    }
}

#[test]
fn game_rows_round_trip_and_commands_are_not_trimmed() {
    let dir = temp_dir("db-roundtrip");
    let db = Database::open(&dir.join("mgl.db")).unwrap();
    let mut game = make_game("game", "Game");
    game.command = "  echo '  spaced  '  ".to_string();
    db.save_game(&game).unwrap();
    let loaded = db.load_games().unwrap();
    assert_eq!(loaded.len(), 1);
    assert_eq!(loaded[0].command, game.command);
    assert_eq!(loaded[0].environment, game.environment);
    assert_eq!(loaded[0].runner, RunnerLabel::Custom);
    db.delete_game("game").unwrap();
    assert!(db.load_games().unwrap().is_empty());
}

#[test]
fn only_ten_runs_are_retained_per_game() {
    let dir = temp_dir("db-runs");
    let db = Database::open(&dir.join("mgl.db")).unwrap();
    db.save_game(&make_game("g", "G")).unwrap();
    for index in 0..15 {
        let record = mgl::core::game::ExecutionRecord {
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
}

#[test]
fn a_version_one_database_is_backed_up_and_migrated() {
    let dir = temp_dir("db-v1");
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
    assert_eq!(linux.runner, RunnerLabel::Native);
    assert_eq!(linux.working_dir, "/opt/game");
    let wine = games.iter().find(|game| game.id == "wine").unwrap();
    assert_eq!(wine.command, "echo custom");
    assert_eq!(wine.runner, RunnerLabel::Wine);
    assert_eq!(db.load_runs("linux", "Linux Game").unwrap().len(), 1);
    let backups: Vec<_> = std::fs::read_dir(&*dir)
        .unwrap()
        .flatten()
        .filter(|entry| entry.file_name().to_string_lossy().contains(".bak-"))
        .collect();
    assert_eq!(backups.len(), 1);
    assert!(db.notes().iter().any(|note| note.contains("Migrated")));
}

#[test]
fn legacy_python_rows_import_read_only_with_environment() {
    let dir = temp_dir("db-legacy");
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
                    '{"FOO":"bar"}', '2025-02-01 00:00:00', '2025-02-01 00:00:00'),
                   ('wine-game', 'Wine Game', 'windows', '/games/A B/g.exe',
                    '/games/A B', '--fullscreen', '/prefixes/w', '{}',
                    '2025-02-02 00:00:00', '2025-02-02 00:00:00');
            "#,
        )
        .unwrap();
    }
    let before = std::fs::read(&legacy).unwrap();
    let db = Database::open(&dir.join("mgl.db")).unwrap();
    let report = db.import_legacy(&legacy).unwrap();
    assert_eq!(report.imported, 2);
    let games = db.load_games().unwrap();
    let chess = games.iter().find(|game| game.id == "chess").unwrap();
    assert_eq!(chess.command, "flatpak run org.gnome.Chess");
    assert_eq!(chess.environment, "FOO=bar\n");
    assert_eq!(chess.runner, RunnerLabel::Native);
    let wine = games.iter().find(|game| game.id == "wine-game").unwrap();
    assert_eq!(
        wine.command,
        "WINEPREFIX=\"/prefixes/w\" wine \"/games/A B/g.exe\" --fullscreen"
    );
    assert_eq!(wine.runner, RunnerLabel::Wine);
    // The source database was never modified.
    assert_eq!(std::fs::read(&legacy).unwrap(), before);
    // Importing twice skips records that already exist.
    let again = db.import_legacy(&legacy).unwrap();
    assert_eq!(again.imported, 0);
    assert_eq!(again.skipped, 2);
}

#[test]
fn legacy_rows_with_missing_columns_do_not_break_startup() {
    let dir = temp_dir("db-legacy-old");
    let legacy = dir.join("library.db");
    {
        let conn = Connection::open(&legacy).unwrap();
        conn.execute_batch(
            "CREATE TABLE games (id TEXT PRIMARY KEY, name TEXT NOT NULL, executable TEXT NOT NULL);
             INSERT INTO games VALUES ('old', 'Old', 'run-old');",
        )
        .unwrap();
    }
    let db = Database::open(&dir.join("mgl.db")).unwrap();
    let report = db.import_legacy(&legacy).unwrap();
    assert_eq!(report.imported, 1);
    let games = db.load_games().unwrap();
    assert_eq!(games[0].id, "old");
    assert_eq!(games[0].command, "run-old");
}

// --------------------------------------------------------------------- paths

#[test]
fn managed_paths_only_match_the_covers_directory() {
    let _guard = env_lock();
    let root = temp_dir("paths-xdg");
    unsafe { std::env::set_var("XDG_DATA_HOME", &*root) };
    let inside = paths::covers_dir().join("a.png");
    assert!(paths::is_managed_cover(&inside));
    assert!(!paths::is_managed_cover(Path::new("/tmp/a.png")));
    assert!(!paths::is_managed_cover(
        &paths::covers_dir().join("../escape.png")
    ));
    assert_eq!(paths::slugify("My Game / Part 2"), "my-game-part-2");
    assert_eq!(paths::slugify("!!!"), "game");
    unsafe { std::env::remove_var("XDG_DATA_HOME") };
}

#[test]
fn database_parameter_binding_is_used_everywhere() {
    // A canary: a malicious game id must be stored as data, not executed as SQL.
    let dir = temp_dir("db-injection");
    let db = Database::open(&dir.join("mgl.db")).unwrap();
    let evil = make_game("x'; DROP TABLE games; --", "Evil");
    db.save_game(&evil).unwrap();
    assert_eq!(db.load_games().unwrap().len(), 1);
}

// ------------------------------------------------------------------ keyboard

/// Keyboard navigation only exists in terms of the visible list, so it is
/// checked against a real library rather than a mock.
#[test]
fn keyboard_selection_moves_by_cells_and_survives_filtering() {
    let _guard = env_lock();
    let root = temp_dir("keys-data");
    let config = temp_dir("keys-config");
    unsafe {
        std::env::set_var("XDG_DATA_HOME", &*root);
        std::env::set_var("XDG_CONFIG_HOME", &*config);
    }
    let db = Database::open(&paths::database_path()).unwrap();
    for (index, name) in ["Alpha", "Bravo", "Charlie", "Delta", "Echo"]
        .iter()
        .enumerate()
    {
        let mut game = Game::new(format!("g{index}"), *name, "true");
        game.runner = RunnerLabel::Native;
        db.save_game(&game).unwrap();
    }
    drop(db);
    let mut state = AppState::load().unwrap();
    assert_eq!(state.games.len(), 5);

    // Down from nothing selects the first card.
    actions::select_move(&mut state, 3, 0, 1);
    assert_eq!(actions::selected_position(&state), Some(0));

    // Right stays inside the row, Down moves a whole row.
    actions::select_move(&mut state, 3, 1, 0);
    assert_eq!(actions::selected_position(&state), Some(1));
    actions::select_move(&mut state, 3, 0, 1);
    assert_eq!(actions::selected_position(&state), Some(4));
    // Right on the last card of a partial row is a no-op.
    actions::select_move(&mut state, 3, 1, 0);
    assert_eq!(actions::selected_position(&state), Some(4));

    actions::select_edge(&mut state, false);
    assert_eq!(actions::selected_position(&state), Some(0));
    actions::select_edge(&mut state, true);
    assert_eq!(actions::selected_position(&state), Some(4));

    // A filter change keeps the selection on the same game while it is visible.
    let selected = state.selected.clone();
    state.filter = Filter::Native;
    assert_eq!(state.selected, selected);
    assert_eq!(actions::selected_position(&state), Some(4));

    // Removing the selected game clears the selection instead of dangling.
    let id = state.selected.clone().unwrap();
    actions::apply(&mut state, Action::OpenRemove(id));
    actions::apply(&mut state, Action::ConfirmRemove);
    assert!(state.selected.is_none());

    unsafe {
        std::env::remove_var("XDG_DATA_HOME");
        std::env::remove_var("XDG_CONFIG_HOME");
    }
}

#[test]
fn log_selection_and_reveal_stay_in_bounds() {
    assert_eq!(actions::step_index(0, -1, 3), 0);
    assert_eq!(actions::step_index(0, 1, 3), 1);
    assert_eq!(actions::step_index(2, 1, 3), 2);

    let vp = mgl::ui::layout::Viewport::new(1200.0, 800.0, 1.0);
    let grid = mgl::ui::screens::grid(vp, false, 20);
    assert!(grid.columns > 1);
    // A card below the fold scrolls just enough to be visible.
    let mut scroll = 0.0;
    mgl::ui::screens::reveal(&grid, &mut scroll, grid.columns * 3);
    assert!(scroll > 0.0);
    assert!(scroll <= grid.max);
    // Revealing a card above the fold scrolls back up.
    let mut scroll = 400.0;
    mgl::ui::screens::reveal(&grid, &mut scroll, 0);
    assert_eq!(scroll, 0.0);
}
