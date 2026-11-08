//! Action application, text editing and clipboard helpers.
//!
//! The screens are pure: they draw and record [`Action`]s. This module is the
//! only place where the state changes. Cover import and the file chooser run on
//! worker threads (see [`AppState`]); everything here stays on the UI thread.

use std::time::Duration;

use crate::app::state::{AppState, EditorDraft, LogsView, Modal, RemoveView, search_path};
use crate::core::command;
use crate::core::cover;
use crate::core::game::{Game, RunnerLabel};
use crate::core::library::{self as library};
use crate::logging::execution;
use crate::storage::SortOrder;
use crate::ui::components::{Action, Field, SelectKey};
use crate::ui::layout::Rect;
use crate::ui::theme::ThemeName;

/// Process supervision tick. Exposed so the event loop arms exactly this timer.
pub const TICK: Duration = Duration::from_millis(250);

// ------------------------------------------------------------------- dispatch

/// Apply one action.
pub fn apply(state: &mut AppState, action: Action) {
    match action {
        // ---------------------------------------------------------- library
        Action::Filter(filter) => state.filter = filter,
        Action::OpenSettings => open(state, Modal::Settings),
        Action::AddGame => {
            let id = library::allocate_id("new-game", &state.games);
            open(state, Modal::Editor(EditorDraft::new_game(id)));
            focus_field(state, Field::Name, 0);
        }
        Action::EditGame(id) => {
            let Some(game) = state.game(&id).cloned() else {
                return;
            };
            open(state, Modal::Editor(EditorDraft::from_game(&game)));
        }
        Action::Run(id) => run_game(state, &id),

        // ------------------------------------------------------ modal shell
        Action::Close => close_modal(state),
        Action::Consume => {}
        Action::SelectClose => state.open_select = None,
        Action::SelectOpen(key) => {
            state.open_select = if state.open_select == Some(key) {
                None
            } else {
                Some(key)
            };
        }
        Action::SelectPick(key, index) => pick(state, key, index),

        // --------------------------------------------------------- settings
        Action::ToggleTransparency => {
            state.settings.transparent = !state.settings.transparent;
            state.settings.solid_fallback = !state.settings.transparent;
            state.persist_settings();
        }
        Action::ToggleSolid => {
            state.settings.solid_fallback = !state.settings.solid_fallback;
            if !state.settings.solid_fallback {
                state.settings.transparent = true;
            }
            state.persist_settings();
        }
        Action::Opacity(rect) => {
            state.opacity_drag = Some(rect);
            set_opacity_from(state, rect, state.mouse.0);
        }

        // ----------------------------------------------------------- editor
        Action::SaveEditor => save_editor(state),
        Action::BrowseCover => browse_cover(state),
        Action::RemoveCover => remove_cover(state),

        // ------------------------------------------------------------- logs
        Action::OpenLogs(id) => open_logs(state, &id),
        Action::PickLog(index) => {
            if let Some(Modal::Logs(view)) = &mut state.modal {
                view.index = index;
            }
            state.scroll.logs = 0.0;
        }
        Action::CopyLog => copy_log(state),
        Action::ClearLogs => clear_logs(state),

        // ---------------------------------------------------------- removal
        Action::OpenRemove(id) => {
            let Some(game) = state.game(&id) else {
                return;
            };
            open(
                state,
                Modal::Remove(RemoveView {
                    game: id,
                    name: game.name.clone(),
                }),
            );
        }
        Action::ConfirmRemove => confirm_remove(state),
    }
}

// -------------------------------------------------------------------- helpers

fn open(state: &mut AppState, modal: Modal) {
    state.modal = Some(modal);
    state.open_select = None;
    state.focus = None;
    state.sel_anchor = None;
    state.scroll.sheet = 0.0;
    state.scroll.logs = 0.0;
}

fn focus_field(state: &mut AppState, field: Field, caret: usize) {
    state.focus = Some(field);
    state.caret = caret;
    state.sel_anchor = None;
}

fn close_modal(state: &mut AppState) {
    // An unsaved new game owns no database row, so a cover imported while the
    // sheet was open must not be left behind.
    if let Some(Modal::Editor(draft)) = &state.modal
        && !draft.exists
    {
        cover::remove_for_game(&draft.id);
    }
    state.modal = None;
    state.focus = None;
    state.sel_anchor = None;
    state.open_select = None;
    state.scroll.sheet = 0.0;
}

fn pick(state: &mut AppState, key: SelectKey, index: usize) {
    state.open_select = None;
    match key {
        SelectKey::Sort => {
            state.settings.sort = if index == 0 {
                SortOrder::Name
            } else {
                SortOrder::Type
            };
            state.persist_settings();
        }
        SelectKey::Theme => {
            if let Some(theme) = ThemeName::ALL.get(index) {
                state.settings.theme = *theme;
                state.persist_settings();
            }
        }
        SelectKey::Runner => {
            if let Some(runner) = RunnerLabel::ALL.get(index)
                && let Some(Modal::Editor(draft)) = &mut state.modal
            {
                draft.runner = *runner;
                draft.error = None;
            }
        }
    }
}

// ----------------------------------------------------------------------- runs

fn run_game(state: &mut AppState, id: &str) {
    let Some(index) = state.games.iter().position(|game| game.id == id) else {
        return;
    };
    if state.launcher.is_active(id) {
        let name = state.games[index].name.clone();
        match state.launcher.stop(&mut state.games[index], &state.db) {
            Some(outcome) => state.set_status(format!("{name} stopped: {}", outcome.describe())),
            // The group was signalled; the tick records the outcome when the
            // process is actually gone, so the UI never blocks here.
            None => state.set_status(format!("Stopping {name}…")),
        }
        return;
    }
    let outcome = state.launcher.launch(&mut state.games[index], &state.db);
    match (&outcome.error, outcome.pid) {
        (Some(error), _) => state.set_error(error.clone()),
        (None, Some(pid)) => {
            state.set_status(format!("Started {} (PID {pid})", state.games[index].name))
        }
        _ => {}
    }
}

// --------------------------------------------------------------------- editor

fn save_editor(state: &mut AppState) {
    let draft = match &state.modal {
        Some(Modal::Editor(draft)) => draft.clone(),
        _ => return,
    };
    let name = draft.name.trim().to_string();
    if name.is_empty() {
        set_editor_error(state, "The game needs a name.");
        return;
    }
    if let Err(error) = command::validate_command(&draft.command) {
        set_editor_error(state, &error);
        return;
    }
    let mut game = draft.into_game();
    game.name = name.clone();
    if let Err(error) = state.db.save_game(&game) {
        set_editor_error(state, &error);
        return;
    }
    if let Some(index) = state
        .games
        .iter()
        .position(|candidate| candidate.id == game.id)
    {
        game.status = state.games[index].status.clone();
        state.games[index] = game;
    } else {
        state.games.push(game);
    }
    state.set_status(format!("Saved “{name}”."));
    state.modal = None;
    state.focus = None;
    state.sel_anchor = None;
    state.open_select = None;
    state.scroll.sheet = 0.0;
}

fn set_editor_error(state: &mut AppState, message: &str) {
    if let Some(Modal::Editor(draft)) = &mut state.modal {
        draft.error = Some(message.to_string());
    }
}

fn browse_cover(state: &mut AppState) {
    let Some(Modal::Editor(draft)) = &state.modal else {
        return;
    };
    if draft.cover_busy {
        return;
    }
    let id = draft.id.clone();
    if let Some(Modal::Editor(draft)) = &mut state.modal {
        draft.cover_busy = true;
        draft.error = None;
    }
    state.spawn_cover_import(id);
}

fn remove_cover(state: &mut AppState) {
    let Some(Modal::Editor(draft)) = &state.modal else {
        return;
    };
    let id = draft.id.clone();
    let exists = draft.exists;
    if let Some(Modal::Editor(draft)) = &mut state.modal {
        draft.cover = None;
        draft.cover_thumb = None;
        draft.cover_busy = false;
    }
    cover::remove_for_game(&id);
    state.covers.remove(&id);
    if exists {
        if let Some(game) = state.game_mut(&id) {
            game.cover = None;
            game.cover_thumb = None;
        }
        state.persist(&id);
    }
}

// ----------------------------------------------------------------------- logs

fn open_logs(state: &mut AppState, id: &str) {
    let Some(game) = state.game(id) else {
        return;
    };
    let name = game.name.clone();
    match state.db.load_runs(id, &name) {
        Ok(records) => {
            open(
                state,
                Modal::Logs(LogsView {
                    game: id.to_string(),
                    records,
                    index: 0,
                }),
            );
            state.focus = None;
        }
        Err(error) => state.set_error(error),
    }
}

fn copy_log(state: &mut AppState) {
    let Some(Modal::Logs(view)) = &state.modal else {
        return;
    };
    let text = selected_log_text(state, view);
    match clipboard_set(&text) {
        Ok(()) => state.set_status("Log copied to the clipboard."),
        Err(error) => state.set_error(error),
    }
}

/// Text of the selected log entry, including the live run when it is selected.
pub fn selected_log_text(state: &AppState, view: &LogsView) -> String {
    let live = state.launcher.is_active(&view.game);
    if live {
        if view.index == 0 {
            if let (Some(info), Some((_, stdout, stderr))) = (
                state.launcher.active_info(&view.game),
                state.launcher.live_snapshot(&view.game),
            ) {
                let live_view = execution::LiveView {
                    command: info.command,
                    working_dir: info.workdir,
                    pid: info.pid,
                    started: info.started,
                    elapsed_label: library::format_duration(info.elapsed.as_secs()),
                    stdout,
                    stderr,
                };
                return live_view.as_text(&info.game_name);
            }
            return "(live output is not available)".to_string();
        }
        if let Some(record) = view.records.get(view.index - 1) {
            return record.to_text();
        }
    }
    if let Some(record) = view.records.get(view.index) {
        return record.to_text();
    }
    "(no log selected)".to_string()
}

fn clear_logs(state: &mut AppState) {
    let Some(Modal::Logs(view)) = &state.modal else {
        return;
    };
    let id = view.game.clone();
    match state.db.clear_runs(&id) {
        Ok(()) => {
            state.reload_logs();
            state.set_status("Execution logs cleared.");
        }
        Err(error) => state.set_error(error),
    }
}

// -------------------------------------------------------------------- removal

fn confirm_remove(state: &mut AppState) {
    let Some(Modal::Remove(view)) = &state.modal else {
        return;
    };
    let id = view.game.clone();
    let name = view.name.clone();
    if let Some(index) = state.games.iter().position(|game| game.id == id) {
        if state.launcher.is_active(&id) {
            state.launcher.stop(&mut state.games[index], &state.db);
        }
        if let Err(error) = state.db.delete_game(&id) {
            state.set_error(error);
            return;
        }
        state.games.remove(index);
    }
    // Only the cover files MGL copied are removed; a path outside the managed
    // covers directory is never touched.
    cover::remove_for_game(&id);
    state.covers.remove(&id);
    state.modal = None;
    state.focus = None;
    state.set_status(format!("Removed “{name}” from the library."));
}

// --------------------------------------------------------------- text editing

/// The literal text a key press types, if any.
///
/// winit reports a plain space as `Key::Named(NamedKey::Space)` on X11 and
/// Wayland, never as `Key::Character(" ")`; matching only the latter silently
/// dropped every space typed into a field. Control characters are never text.
pub fn typed_text(key: &winit::keyboard::Key) -> Option<String> {
    use winit::keyboard::{Key, NamedKey};
    match key {
        Key::Character(text) if !text.is_empty() => {
            let filtered: String = text.chars().filter(|c| !c.is_control()).collect();
            (!filtered.is_empty()).then_some(filtered)
        }
        Key::Named(NamedKey::Space) => Some(" ".to_string()),
        _ => None,
    }
}

/// What a key press should insert while a field is focused.
pub fn text_input(key: &winit::keyboard::Key, editing: bool) -> Option<String> {
    editing.then(|| typed_text(key)).flatten()
}

/// Byte length of the focused text, used to park the caret at the end.
pub fn text_len(state: &AppState) -> usize {
    state.focused_text().map(|text| text.len()).unwrap_or(0)
}

/// Insert `text` at the caret, replacing any selection.
pub fn insert_text(state: &mut AppState, text: &str) {
    let (caret, anchor) = (state.caret, state.sel_anchor);
    let Some(buffer) = state.focused_text_mut() else {
        return;
    };
    let (start, end) = match anchor {
        Some(anchor) => (anchor.min(caret), anchor.max(caret)),
        None => (caret.min(buffer.len()), caret.min(buffer.len())),
    };
    let start = floor_boundary(buffer, start);
    let end = floor_boundary(buffer, end);
    buffer.replace_range(start..end, text);
    state.caret = start + text.len();
    state.sel_anchor = None;
}

/// Delete the selection, or the character before the caret.
pub fn backspace(state: &mut AppState) {
    let (caret, anchor) = (state.caret, state.sel_anchor);
    let Some(buffer) = state.focused_text_mut() else {
        return;
    };
    let mut next_caret = None;
    if let Some(anchor) = anchor {
        let (start, end) = (anchor.min(caret), anchor.max(caret));
        if start != end {
            let start = floor_boundary(buffer, start);
            let end = floor_boundary(buffer, end);
            buffer.replace_range(start..end, "");
            next_caret = Some(start);
        }
    }
    if next_caret.is_none() {
        let caret = floor_boundary(buffer, caret);
        if caret == 0 {
            return;
        }
        let previous = buffer[..caret]
            .chars()
            .next_back()
            .map(|c| c.len_utf8())
            .unwrap_or(0);
        buffer.replace_range(caret - previous..caret, "");
        next_caret = Some(caret - previous);
    }
    state.caret = next_caret.unwrap_or(caret);
    state.sel_anchor = None;
}

/// Delete the selection, or the character after the caret.
pub fn delete_forward(state: &mut AppState) {
    let (caret, anchor) = (state.caret, state.sel_anchor);
    let Some(buffer) = state.focused_text_mut() else {
        return;
    };
    let mut next_caret = None;
    if let Some(anchor) = anchor {
        let (start, end) = (anchor.min(caret), anchor.max(caret));
        if start != end {
            let start = floor_boundary(buffer, start);
            let end = floor_boundary(buffer, end);
            buffer.replace_range(start..end, "");
            next_caret = Some(start);
        }
    }
    if next_caret.is_none() {
        let caret = floor_boundary(buffer, caret);
        if caret >= buffer.len() {
            return;
        }
        let next = buffer[caret..]
            .chars()
            .next()
            .map(|c| c.len_utf8())
            .unwrap_or(0);
        buffer.replace_range(caret..caret + next, "");
        next_caret = Some(caret);
    }
    state.caret = next_caret.unwrap_or(caret);
    state.sel_anchor = None;
}

/// Move the caret one character left (`delta < 0`) or right.
pub fn move_caret(state: &mut AppState, delta: isize, extend: bool) {
    let Some(buffer) = state.focused_text() else {
        return;
    };
    let len = buffer.len();
    let mut caret = state.caret.min(len);
    if delta < 0 {
        let previous = buffer[..floor_boundary(buffer, caret)]
            .chars()
            .next_back()
            .map(|c| c.len_utf8())
            .unwrap_or(0);
        caret -= previous;
    } else {
        let next = buffer[caret..]
            .chars()
            .next()
            .map(|c| c.len_utf8())
            .unwrap_or(0);
        caret = (caret + next).min(len);
    }
    if extend {
        if state.sel_anchor.is_none() {
            state.sel_anchor = Some(state.caret);
        }
    } else {
        state.sel_anchor = None;
    }
    state.caret = caret;
}

pub fn move_caret_to(state: &mut AppState, index: usize, extend: bool) {
    let Some(buffer) = state.focused_text() else {
        return;
    };
    let index = floor_boundary(buffer, index);
    if extend {
        if state.sel_anchor.is_none() {
            state.sel_anchor = Some(state.caret);
        }
    } else {
        state.sel_anchor = None;
    }
    state.caret = index;
}

pub fn select_all(state: &mut AppState) {
    let len = text_len(state);
    state.sel_anchor = Some(0);
    state.caret = len;
}

/// Clear focus and any open drop-down.
pub fn blur(state: &mut AppState) {
    state.focus = None;
    state.sel_anchor = None;
}

fn floor_boundary(text: &str, index: usize) -> usize {
    let mut index = index.min(text.len());
    while index > 0 && !text.is_char_boundary(index) {
        index -= 1;
    }
    index
}

// ------------------------------------------------------------------ clipboard

/// Copy text through `wl-copy`, the clipboard MGL's Wayland session provides.
pub fn clipboard_set(text: &str) -> Result<(), String> {
    let Some(binary) = search_path("wl-copy") else {
        return Err("wl-copy was not found, so MGL cannot use the clipboard.".to_string());
    };
    use std::io::Write;
    let mut child = std::process::Command::new(binary)
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .spawn()
        .map_err(|e| format!("Could not start wl-copy: {e}"))?;
    if let Some(stdin) = child.stdin.as_mut() {
        stdin
            .write_all(text.as_bytes())
            .map_err(|e| format!("Could not write to the clipboard: {e}"))?;
    }
    // `wl-copy` keeps running as the clipboard owner; do not wait for it.
    std::mem::drop(child);
    Ok(())
}

/// Read text from `wl-paste`, bounded so a huge clipboard cannot stall the UI.
pub fn clipboard_get() -> Result<String, String> {
    let Some(binary) = search_path("wl-paste") else {
        return Err("wl-paste was not found, so MGL cannot read the clipboard.".to_string());
    };
    let output = std::process::Command::new(binary)
        .arg("--no-newline")
        .stdin(std::process::Stdio::null())
        .output()
        .map_err(|e| format!("Could not start wl-paste: {e}"))?;
    if !output.status.success() {
        return Ok(String::new());
    }
    const LIMIT: usize = 64 * 1024;
    let mut text = String::from_utf8_lossy(&output.stdout).into_owned();
    if text.len() > LIMIT {
        let mut cut = LIMIT;
        while cut > 0 && !text.is_char_boundary(cut) {
            cut -= 1;
        }
        text.truncate(cut);
    }
    Ok(text)
}

/// Paste into the focused field.
pub fn paste(state: &mut AppState) {
    match clipboard_get() {
        Ok(text) if !text.is_empty() => {
            let multiline = state.focus.map(|field| field.multiline()).unwrap_or(false);
            let text = if multiline {
                text
            } else {
                text.replace(['\n', '\r'], " ")
            };
            insert_text(state, &text);
        }
        Ok(_) => {}
        Err(error) => state.set_error(error),
    }
}

/// Copy the current selection, if any.
pub fn copy_selection(state: &mut AppState) -> bool {
    let (caret, anchor) = (state.caret, state.sel_anchor);
    let Some(anchor) = anchor else {
        return false;
    };
    let Some(text) = state.focused_text() else {
        return false;
    };
    let (start, end) = (
        floor_boundary(text, anchor.min(caret)),
        floor_boundary(text, anchor.max(caret)),
    );
    if start == end {
        return false;
    }
    let selected = text[start..end].to_string();
    match clipboard_set(&selected) {
        Ok(()) => true,
        Err(error) => {
            state.set_error(error);
            false
        }
    }
}

/// Cut the current selection.
pub fn cut_selection(state: &mut AppState) {
    if copy_selection(state) {
        backspace(state);
    }
}

// ------------------------------------------------------------------ scrolling

/// Apply a wheel delta to the region under the pointer.
pub fn scroll(state: &mut AppState, target: crate::ui::components::ScrollTarget, delta: f32) {
    use crate::ui::components::ScrollTarget as T;
    let max = match target {
        T::Library => state.max_scroll.library,
        T::Sheet => state.max_scroll.sheet,
        T::Logs => state.max_scroll.logs,
    };
    let slot = match target {
        T::Library => &mut state.scroll.library,
        T::Sheet => &mut state.scroll.sheet,
        T::Logs => &mut state.scroll.logs,
    };
    *slot = (*slot - delta).clamp(0.0, max.max(0.0));
}

/// Update the opacity while dragging the slider.
pub fn set_opacity_from(state: &mut AppState, track: Rect, x: f32) {
    let usable = (track.w - 14.0).max(1.0);
    let value = ((x - track.x - 7.0) / usable).clamp(0.0, 1.0);
    let opacity = (30.0 + value * 70.0).round() as u8;
    let clamped = opacity.clamp(
        crate::storage::settings::MIN_OPACITY,
        crate::storage::settings::MAX_OPACITY,
    );
    if state.settings.opacity != clamped {
        state.settings.opacity = clamped;
    }
}

/// Persist settings once a drag ends, instead of on every mouse move.
pub fn finish_drag(state: &mut AppState) {
    if state.opacity_drag.take().is_some() {
        state.persist_settings();
    }
}

/// Place the caret from a click inside a field rectangle.
pub fn place_caret(
    state: &mut AppState,
    text: &mut crate::ui::text::TextEngine,
    field: crate::ui::components::FieldRect,
    x: f32,
    y: f32,
) {
    let Some(buffer) = state.focused_text() else {
        return;
    };
    let local_x = x - field.rect.x;
    let local_y = y - field.rect.y;
    let index = text.index_at_lh(
        buffer,
        field.font,
        field.size,
        field.width,
        field.wrap,
        local_x,
        local_y,
        field.line_height,
    );
    state.caret = index;
    state.sel_anchor = None;
}

/// Short label for a capture, reused by the status line.
pub fn short(text: &str, limit: usize) -> String {
    execution::clamp(text, limit)
}

/// Convenience for the editor: the game behind the open draft, if any.
pub fn editor_game(state: &AppState) -> Option<&Game> {
    match &state.modal {
        Some(Modal::Editor(draft)) => state.game(&draft.id),
        _ => None,
    }
}
