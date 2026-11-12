//! Application state: the library, settings, the launcher and the open modal.
//!
//! The state is a plain struct. The UI borrows it immutably while drawing and
//! the action handlers mutate it on the main thread, so the data flow stays
//! easy to follow. Cover decoding/importing is the one exception: those jobs run
//! on worker threads and report back through a channel polled by the event loop.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::mpsc::{self, Receiver, Sender};

use crate::core::cover::{self, CoverImage, ImportedCover};
use crate::core::game::{ExecutionRecord, Game, RunnerLabel};
use crate::core::launcher::Launcher;
use crate::core::library::{self, Filter};
use crate::core::paths;
use crate::storage::{Database, Settings, SettingsStore};
use crate::ui::components::{Field, Scrolls, SelectKey};
use crate::ui::layout::Rect;
use crate::ui::theme::Tokens;

/// A modal currently covering the library.
pub enum Modal {
    Settings,
    Editor(EditorDraft),
    Logs(LogsView),
    Remove(RemoveView),
}

impl Modal {
    /// Fields a modal owns; used to decide which input is still valid after a
    /// transition.
    pub fn contains_field(&self, field: Field) -> bool {
        match self {
            Modal::Settings => false,
            Modal::Editor(_) => matches!(
                field,
                Field::Name | Field::Command | Field::Workdir | Field::Environment | Field::Notes
            ),
            Modal::Logs(_) | Modal::Remove(_) => false,
        }
    }
}

/// A game being created or edited. The id is allocated when the sheet opens, so
/// a cover can be imported before the game row is first written.
#[derive(Clone, Debug)]
pub struct EditorDraft {
    pub id: String,
    pub exists: bool,
    pub name: String,
    pub command: String,
    pub working_dir: String,
    pub environment: String,
    pub notes: String,
    pub runner: RunnerLabel,
    pub cover: Option<String>,
    pub cover_thumb: Option<String>,
    pub hue: f32,
    pub created_at: String,
    pub error: Option<String>,
    pub cover_busy: bool,
}

impl EditorDraft {
    pub fn new_game(id: String) -> Self {
        Self {
            hue: crate::ui::theme::hue_of(&id),
            id,
            exists: false,
            name: String::new(),
            command: String::new(),
            working_dir: String::new(),
            environment: String::new(),
            notes: String::new(),
            runner: RunnerLabel::Auto,
            cover: None,
            cover_thumb: None,
            created_at: String::new(),
            error: None,
            cover_busy: false,
        }
    }

    pub fn from_game(game: &Game) -> Self {
        Self {
            id: game.id.clone(),
            exists: true,
            name: game.name.clone(),
            command: game.command.clone(),
            working_dir: game.working_dir.clone(),
            environment: game.environment.clone(),
            notes: game.notes.clone(),
            runner: game.runner,
            cover: game.cover.clone(),
            cover_thumb: game.cover_thumb.clone(),
            hue: game.hue,
            created_at: game.created_at.clone(),
            error: None,
            cover_busy: false,
        }
    }

    pub fn into_game(self) -> Game {
        Game {
            id: self.id,
            name: self.name,
            command: self.command,
            working_dir: self.working_dir,
            environment: self.environment,
            notes: self.notes,
            runner: self.runner,
            cover: self.cover,
            cover_thumb: self.cover_thumb,
            hue: self.hue,
            created_at: if self.created_at.is_empty() {
                library::now_stamp()
            } else {
                self.created_at
            },
            status: crate::core::game::RunStatus::Idle,
        }
    }
}

/// Log viewer contents.
pub struct LogsView {
    pub game: String,
    pub records: Vec<ExecutionRecord>,
    pub index: usize,
}

/// Remove confirmation.
pub struct RemoveView {
    pub game: String,
    pub name: String,
}

/// Per-game options popover. `anchor` is the settings button that opened it.
pub struct GameMenu {
    pub game: String,
    pub anchor: Rect,
}

/// A message from a background cover job.
pub enum CoverDone {
    Loaded {
        game_id: String,
        result: Result<CoverImage, String>,
    },
    Imported {
        game_id: String,
        result: Result<Option<ImportedCover>, String>,
    },
}

pub struct AppState {
    pub games: Vec<Game>,
    pub settings: Settings,
    pub db: Database,
    pub settings_store: SettingsStore,
    pub launcher: Launcher,

    pub filter: Filter,
    pub query: String,
    pub modal: Option<Modal>,
    /// Open per-game options popover, if any.
    pub game_menu: Option<GameMenu>,
    /// Keyboard-selected game, by id. Survives filtering and re-sorting.
    pub selected: Option<String>,

    pub focus: Option<Field>,
    pub caret: usize,
    pub sel_anchor: Option<usize>,
    pub open_select: Option<SelectKey>,

    pub scroll: Scrolls,
    pub max_scroll: Scrolls,

    pub status: Option<String>,
    pub status_error: bool,
    pub opacity_drag: Option<Rect>,
    pub mouse: (f32, f32),

    /// Decoded square thumbnails, keyed by game id.
    pub covers: HashMap<String, CoverImage>,
    /// Bumped when a cover changes, so the renderer re-uploads its texture.
    pub cover_epoch: u64,
    cover_tx: Sender<CoverDone>,
    cover_rx: Receiver<CoverDone>,
    cover_pending: usize,
}

impl AppState {
    pub fn load() -> Result<Self, String> {
        let db = Database::open(&paths::database_path())?;
        let mut games = db.load_games()?;
        let mut notes: Vec<String> = db.notes().to_vec();
        if games.is_empty() {
            // Adopt an existing library once, read-only. The Python app's own
            // `library.db` is tried first, then the older prototype directory.
            for legacy in [
                paths::legacy_library_path(),
                paths::prototype_library_path(),
            ] {
                match db.import_legacy(&legacy) {
                    Ok(report) if report.imported > 0 => {
                        notes.push(format!(
                            "Imported {} game(s) from {}.",
                            report.imported,
                            paths::tildify(&legacy)
                        ));
                        for error in report.errors.iter().take(3) {
                            notes.push(error.clone());
                        }
                        games = db.load_games()?;
                        break;
                    }
                    Ok(_) => {}
                    Err(error) => notes.push(error),
                }
            }
        }
        let settings_store = SettingsStore::new(paths::settings_path());
        let mut settings = settings_store.load();
        settings.sanitize();

        let (cover_tx, cover_rx) = mpsc::channel();
        let mut state = Self {
            games,
            filter: Filter::All,
            query: String::new(),
            modal: None,
            game_menu: None,
            selected: None,
            focus: None,
            caret: 0,
            sel_anchor: None,
            open_select: None,
            scroll: Scrolls::default(),
            max_scroll: Scrolls::default(),
            status: (!notes.is_empty()).then(|| notes.join(" ")),
            status_error: false,
            opacity_drag: None,
            mouse: (-1.0, -1.0),
            settings,
            db,
            settings_store,
            launcher: Launcher::new(),
            covers: HashMap::new(),
            cover_epoch: 0,
            cover_tx,
            cover_rx,
            cover_pending: 0,
        };
        // Decode existing thumbnails in the background, never on the UI thread.
        let jobs: Vec<(String, PathBuf)> = state
            .games
            .iter()
            .filter_map(|game| {
                let path = game
                    .cover_thumb
                    .as_deref()
                    .or(game.cover.as_deref())
                    .map(PathBuf::from)?;
                Some((game.id.clone(), path))
            })
            .collect();
        for (id, path) in jobs {
            state.spawn_cover_load(id, path);
        }
        Ok(state)
    }

    pub fn tokens(&self) -> Tokens {
        Tokens::of(self.settings.theme)
    }

    pub fn game(&self, id: &str) -> Option<&Game> {
        self.games.iter().find(|game| game.id == id)
    }

    pub fn game_mut(&mut self, id: &str) -> Option<&mut Game> {
        self.games.iter_mut().find(|game| game.id == id)
    }

    pub fn visible(&self) -> Vec<usize> {
        library::visible(&self.games, self.filter, &self.query, self.settings.sort)
    }

    /// `(all, native, wine, custom)`.
    pub fn counts(&self) -> (usize, usize, usize, usize) {
        library::counts(&self.games)
    }

    /// Persist one game. Errors are surfaced in the status line.
    pub fn persist(&mut self, id: &str) {
        let Some(index) = self.games.iter().position(|game| game.id == id) else {
            return;
        };
        if let Err(error) = self.db.save_game(&self.games[index]) {
            self.set_error(error);
        }
    }

    pub fn persist_settings(&mut self) {
        if let Err(error) = self.settings_store.save(&self.settings) {
            self.set_error(error);
        }
    }

    pub fn set_status(&mut self, text: impl Into<String>) {
        self.status = Some(text.into());
        self.status_error = false;
    }

    pub fn set_error(&mut self, text: impl Into<String>) {
        self.status = Some(text.into());
        self.status_error = true;
    }

    /// Text currently focused, if it belongs to the open modal.
    pub fn focused_text(&self) -> Option<&str> {
        let field = self.focus?;
        match field {
            Field::Query => Some(&self.query),
            Field::Name | Field::Command | Field::Workdir | Field::Environment | Field::Notes => {
                match &self.modal {
                    Some(Modal::Editor(draft)) => Some(match field {
                        Field::Name => &draft.name,
                        Field::Command => &draft.command,
                        Field::Workdir => &draft.working_dir,
                        Field::Environment => &draft.environment,
                        _ => &draft.notes,
                    }),
                    _ => None,
                }
            }
        }
    }

    /// Mutable access to the focused text.
    pub fn focused_text_mut(&mut self) -> Option<&mut String> {
        let field = self.focus?;
        match field {
            Field::Query => Some(&mut self.query),
            Field::Name | Field::Command | Field::Workdir | Field::Environment | Field::Notes => {
                match &mut self.modal {
                    Some(Modal::Editor(draft)) => Some(match field {
                        Field::Name => &mut draft.name,
                        Field::Command => &mut draft.command,
                        Field::Workdir => &mut draft.working_dir,
                        Field::Environment => &mut draft.environment,
                        _ => &mut draft.notes,
                    }),
                    _ => None,
                }
            }
        }
    }

    /// Rebuild the log viewer from the database, keeping the selected index when
    /// possible.
    pub fn reload_logs(&mut self) {
        let Some(Modal::Logs(view)) = &self.modal else {
            return;
        };
        let game_id = view.game.clone();
        let selected = view.index;
        let Some(game) = self.game(&game_id) else {
            return;
        };
        let name = game.name.clone();
        match self.db.load_runs(&game_id, &name) {
            Ok(records) => {
                if let Some(Modal::Logs(view)) = &mut self.modal {
                    view.records = records;
                    view.index = selected.min(view.records.len());
                }
            }
            Err(error) => self.set_error(error),
        }
    }

    // ---------------------------------------------------------------- covers

    /// Decode one thumbnail on a worker thread.
    pub fn spawn_cover_load(&mut self, game_id: String, path: PathBuf) {
        let tx = self.cover_tx.clone();
        self.cover_pending += 1;
        std::thread::spawn(move || {
            let result = cover::load_preview(&path);
            let _ = tx.send(CoverDone::Loaded { game_id, result });
        });
    }

    /// Let the user choose a cover and import it on a worker thread.
    pub fn spawn_cover_import(&mut self, game_id: String) {
        let tx = self.cover_tx.clone();
        self.cover_pending += 1;
        std::thread::spawn(move || {
            let result = choose_cover_file().and_then(|picked| match picked {
                Some(source) => cover::import(&source, &game_id).map(Some),
                None => Ok(None),
            });
            let _ = tx.send(CoverDone::Imported { game_id, result });
        });
    }

    pub fn cover_busy(&self) -> bool {
        self.cover_pending > 0
    }

    /// Drain finished cover jobs. Returns `true` when the frame changed.
    pub fn poll_covers(&mut self) -> bool {
        let mut changed = false;
        while let Ok(done) = self.cover_rx.try_recv() {
            self.cover_pending = self.cover_pending.saturating_sub(1);
            changed = true;
            match done {
                CoverDone::Loaded { game_id, result } => match result {
                    Ok(image) => {
                        self.covers.insert(game_id, image);
                    }
                    Err(error) => {
                        // A missing cover is not worth a permanent error bar, but
                        // a broken decode is reported once.
                        log::warn!("cover {game_id}: {error}");
                    }
                },
                CoverDone::Imported { game_id, result } => match result {
                    Ok(Some(imported)) => {
                        self.covers
                            .insert(game_id.clone(), imported.preview.clone());
                        let cover = imported.cover.to_string_lossy().into_owned();
                        let thumb = imported.thumbnail.to_string_lossy().into_owned();
                        if let Some(Modal::Editor(draft)) = &mut self.modal
                            && draft.id == game_id
                        {
                            draft.cover = Some(cover.clone());
                            draft.cover_thumb = Some(thumb.clone());
                            draft.cover_busy = false;
                            draft.error = None;
                        }
                        // Existing games commit a cover immediately: replacing a
                        // file is not something Cancel can meaningfully undo.
                        if let Some(game) = self.game_mut(&game_id) {
                            game.cover = Some(cover);
                            game.cover_thumb = Some(thumb);
                            let id = game_id.clone();
                            self.persist(&id);
                        }
                        self.cover_epoch = self.cover_epoch.wrapping_add(1);
                        let known = self.game(&game_id).is_some()
                            || matches!(&self.modal, Some(Modal::Editor(draft)) if draft.id == game_id);
                        if !known {
                            // The sheet was cancelled while the import was still
                            // running; do not leave an unreferenced file behind.
                            cover::remove_for_game(&game_id);
                        }
                        self.set_status("Cover updated.");
                    }
                    Ok(None) => {
                        if let Some(Modal::Editor(draft)) = &mut self.modal
                            && draft.id == game_id
                        {
                            draft.cover_busy = false;
                        }
                    }
                    Err(error) => {
                        if let Some(Modal::Editor(draft)) = &mut self.modal
                            && draft.id == game_id
                        {
                            draft.cover_busy = false;
                            draft.error = Some(error.clone());
                        }
                        self.set_error(error);
                    }
                },
            }
        }
        changed
    }

    /// Persist the window geometry before exiting.
    pub fn save_window(&mut self, width: u32, height: u32, x: Option<i32>, y: Option<i32>) {
        self.settings.window.width = width;
        self.settings.window.height = height;
        self.settings.window.x = x;
        self.settings.window.y = y;
        self.persist_settings();
    }

    /// Stop every game; called once on shutdown.
    pub fn shutdown(&mut self) {
        if self.launcher.any_active() {
            self.launcher.shutdown(&mut self.games, &self.db);
        }
    }
}

/// Native file chooser through zenity or kdialog. Both talk to the desktop's
/// own portal dialog. Cancelling returns `None` and changes nothing.
fn choose_cover_file() -> Result<Option<PathBuf>, String> {
    let filter = "*.png *.jpg *.jpeg *.webp";
    if let Some(zenity) = search_path("zenity") {
        let output = std::process::Command::new(zenity)
            .arg("--file-selection")
            .arg("--title=Select a cover image")
            .arg(format!("--file-filter=Images | {filter}"))
            .stdin(std::process::Stdio::null())
            .output()
            .map_err(|e| format!("Could not start the file chooser: {e}"))?;
        if !output.status.success() {
            return Ok(None);
        }
        let path = String::from_utf8_lossy(&output.stdout).trim().to_string();
        return Ok((!path.is_empty()).then(|| PathBuf::from(path)));
    }
    if let Some(kdialog) = search_path("kdialog") {
        let output = std::process::Command::new(kdialog)
            .arg("--getopenfilename")
            .arg(std::env::var("HOME").unwrap_or_else(|_| ".".to_string()))
            .arg(filter)
            .stdin(std::process::Stdio::null())
            .output()
            .map_err(|e| format!("Could not start the file chooser: {e}"))?;
        if !output.status.success() {
            return Ok(None);
        }
        let path = String::from_utf8_lossy(&output.stdout).trim().to_string();
        return Ok((!path.is_empty()).then(|| PathBuf::from(path)));
    }
    Err("zenity or kdialog is required to choose a cover image.".to_string())
}

pub(crate) fn search_path(binary: &str) -> Option<PathBuf> {
    if binary.contains('/') {
        let path = PathBuf::from(binary);
        return is_executable(&path).then_some(path);
    }
    let path = std::env::var_os("PATH")?;
    std::env::split_paths(&path)
        .map(|directory| directory.join(binary))
        .find(|candidate| is_executable(candidate))
}

fn is_executable(path: &Path) -> bool {
    use std::os::unix::fs::PermissionsExt;
    path.metadata()
        .map(|meta| meta.is_file() && meta.permissions().mode() & 0o111 != 0)
        .unwrap_or(false)
}
