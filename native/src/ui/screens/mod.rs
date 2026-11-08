//! Screens: the library grid and the four modal sheets.
//!
//! Layout is plain arithmetic against the prototype's metrics. There is no
//! retained widget tree: a screen draws itself and records hit rectangles, and
//! the app maps the next click back to an [`Action`].

use crate::app::actions;
use crate::app::state::{AppState, EditorDraft, LogsView, Modal, RemoveView};
use crate::core::game::RunnerLabel;
use crate::core::library::{Filter, SortOrder};
use crate::logging::execution::list_label;
use crate::ui::components::*;
use crate::ui::layout::{Rect, Row, Viewport, grid_columns};
use crate::ui::text::Font;
use crate::ui::theme::*;

const BAR_H: f32 = 88.0;
const STATUS_H: f32 = 26.0;
const PAGE_PAD: f32 = 22.0;
const GRID_GAP: f32 = 18.0;
const GRID_MIN: f32 = 184.0;
const SHEET_PAD: f32 = 24.0;
const SHEET_HEAD: f32 = 64.0;
const SHEET_FOOT: f32 = 66.0;

/// Draw one frame. The library stays behind a modal scrim for depth.
pub fn draw(ui: &mut Ui, state: &AppState) {
    let background = ui.tk.bg;
    ui.rect(ui.vp.full(), background, 0.0);
    library(ui, state);
    match &state.modal {
        Some(Modal::Settings) => settings(ui, state),
        Some(Modal::Editor(draft)) => editor(ui, state, draft),
        Some(Modal::Logs(view)) => logs(ui, state, view),
        Some(Modal::Remove(view)) => remove(ui, view),
        None => {}
    }
}

// ------------------------------------------------------------------- library

fn library(ui: &mut Ui, state: &AppState) {
    let tk = ui.tk;
    let vp = ui.vp;
    let bar = Rect::new(0.0, 0.0, vp.width, BAR_H);
    ui.rect(bar, tk.surface, 0.0);

    ui.text(
        "MGL",
        Rect::new(PAGE_PAD, 10.0, 200.0, 30.0),
        Font::HEAD,
        24.0,
        tk.text,
    );
    ui.text(
        "minimal game launcher",
        Rect::new(PAGE_PAD, 36.0, 260.0, 16.0),
        Font::BODY,
        TEXT_SM,
        tk.muted,
    );

    // Right side: search, add, settings.
    let search_w = 220.0f32.min((vp.width * 0.32).max(140.0));
    let settings_w = ICON_H;
    let add_w = ui.button_width("Add game");
    let gap = 10.0;
    let settings_x = vp.width - PAGE_PAD - settings_w;
    let add_x = settings_x - gap - add_w;
    let search_x = (add_x - gap - search_w).max(PAGE_PAD + 100.0);
    ui.input(
        Field::Query,
        Rect::new(search_x, 18.0, (add_x - gap - search_x).max(80.0), ICON_H),
        &state.query,
        "Search games",
    );
    ui.button(
        Rect::new(add_x, 18.0, add_w, ICON_H),
        "Add game",
        ButtonKind::Primary,
        Action::AddGame,
        true,
    );
    ui.icon_button(
        Rect::new(settings_x, 18.0, settings_w, ICON_H),
        Icon::Gear,
        false,
        Action::OpenSettings,
    );

    // Filter chips.
    let (all, native, wine, custom) = state.counts();
    let counts = [all, native, wine, custom];
    let mut x = PAGE_PAD;
    for (index, filter) in Filter::ALL.into_iter().enumerate() {
        let label = format!("{} {}", filter.label(), counts[index]);
        let width = ui.chip_width(&label);
        ui.chip(
            Rect::new(x, 54.0, width, 26.0),
            &label,
            state.filter == filter,
            Action::Filter(filter),
        );
        x += width + 6.0;
    }

    let status_h = if state.status.is_some() {
        STATUS_H
    } else {
        0.0
    };
    let content = Rect::new(
        PAGE_PAD,
        BAR_H + 16.0,
        vp.width - PAGE_PAD * 2.0,
        (vp.height - BAR_H - 16.0 - 16.0 - status_h).max(1.0),
    );
    let indices = state.visible();
    if indices.is_empty() {
        empty_state(ui, state, content);
    } else {
        cards(ui, state, content, &indices);
    }
    status_bar(ui, state);
}

fn empty_state(ui: &mut Ui, state: &AppState, area: Rect) {
    let tk = ui.tk;
    let message = if state.games.is_empty() {
        "No games yet. Add your first command."
    } else {
        "No games match this filter."
    };
    let y = area.center_y() - 50.0;
    ui.text(
        message,
        Rect::new(area.x, y, area.w, 24.0),
        Font::HEAD_MED,
        TEXT_H2,
        tk.muted,
    );
    if state.games.is_empty() {
        let width = ui.button_width("Add game");
        ui.button(
            Rect::new(area.center_x() - width * 0.5, y + 40.0, width, CONTROL_H),
            "Add game",
            ButtonKind::Primary,
            Action::AddGame,
            true,
        );
    }
}

fn cards(ui: &mut Ui, state: &AppState, area: Rect, indices: &[usize]) {
    let (columns, width) = grid_columns(area.w, GRID_MIN, GRID_GAP);
    let card_h = width + 96.0;
    let rows = indices.len().div_ceil(columns);
    let total = rows as f32 * card_h + (rows.saturating_sub(1)) as f32 * GRID_GAP;
    let max = (total - area.h).max(0.0);
    ui.max.library = max;
    let scroll = ui.scroll.library.clamp(0.0, max);
    ui.scroll_regions.push((area, ScrollTarget::Library));

    ui.push_clip(area);
    for (position, index) in indices.iter().enumerate() {
        let column = position % columns;
        let row = position / columns;
        let x = area.x + column as f32 * (width + GRID_GAP);
        let y = area.y + row as f32 * (card_h + GRID_GAP) - scroll;
        let rect = Rect::new(x, y, width, card_h);
        if rect.bottom() < area.y || rect.y > area.bottom() {
            continue;
        }
        card(ui, state, *index, rect);
    }
    ui.pop_clip();
}

fn card(ui: &mut Ui, state: &AppState, index: usize, rect: Rect) {
    let tk = ui.tk;
    let game = &state.games[index];
    let hovered = ui.hovered(rect);

    // Whole card opens the editor; overlays registered later win the click.
    ui.hit(rect, Action::EditGame(game.id.clone()), true);
    ui.rect_border(
        rect,
        tk.surface,
        10.0,
        1.0,
        if hovered { tk.accent } else { tk.line },
    );

    let cover = Rect::new(rect.x + 10.0, rect.y + 10.0, rect.w - 20.0, rect.w - 20.0);
    ui.push_clip(cover);
    if let Some(image) = state.covers.get(&game.id) {
        ui.cover(&cover_key(state, &game.id), image, cover, 8.0);
    } else {
        let (a, b) = tk.cover_gradient(game.hue);
        ui.gradient_diag(cover, a, b, 8.0);
        let initial = game
            .name
            .chars()
            .next()
            .map(|c| c.to_uppercase().collect::<String>())
            .unwrap_or_default();
        ui.text(
            &initial,
            Rect::new(cover.x, cover.y, cover.w, cover.h),
            Font::HEAD,
            TEXT_MONOGRAM.min(cover.h * 0.45),
            Rgba::rgb(255, 255, 255).with_alpha(0.85),
        );
    }
    ui.pop_clip();

    // Play/stop overlay on the cover.
    let run = Rect::new(cover.right() - 44.0, cover.bottom() - 44.0, 34.0, 34.0);
    let running = game.is_running();
    ui.button(
        run,
        if running { "Stop" } else { "Play" },
        if running {
            ButtonKind::Danger
        } else {
            ButtonKind::Primary
        },
        Action::Run(game.id.clone()),
        true,
    );

    let title_y = cover.bottom() + 12.0;
    ui.text(
        &game.name,
        Rect::new(rect.x + 12.0, title_y, rect.w - 24.0, 20.0),
        Font::HEAD_MED,
        TEXT_H3,
        tk.text,
    );
    let state_label = if running {
        "running".to_string()
    } else {
        match &game.status {
            crate::core::game::RunStatus::Finished { outcome } => actions::short(outcome, 28),
            _ => "ready".to_string(),
        }
    };
    ui.text(
        &format!("{} · {}", game.runner_label(), state_label),
        Rect::new(rect.x + 12.0, title_y + 22.0, rect.w - 24.0, 16.0),
        Font::BODY,
        TEXT_SM,
        if running { tk.ok } else { tk.muted },
    );

    let logs_w = ui.button_width("Logs");
    ui.button(
        Rect::new(
            rect.right() - 12.0 - logs_w,
            rect.bottom() - 42.0,
            logs_w,
            30.0,
        ),
        "Logs",
        ButtonKind::Default,
        Action::OpenLogs(game.id.clone()),
        true,
    );
    let remove_w = ui.button_width("Remove");
    ui.button(
        Rect::new(
            rect.right() - 12.0 - logs_w - 6.0 - remove_w,
            rect.bottom() - 42.0,
            remove_w,
            30.0,
        ),
        "Remove",
        ButtonKind::DangerText,
        Action::OpenRemove(game.id.clone()),
        true,
    );
}

fn status_bar(ui: &mut Ui, state: &AppState) {
    let Some(status) = &state.status else {
        return;
    };
    let tk = ui.tk;
    let rect = Rect::new(0.0, ui.vp.height - STATUS_H, ui.vp.width, STATUS_H);
    ui.rect(rect, tk.surface, 0.0);
    ui.text(
        status,
        Rect::new(PAGE_PAD, rect.y + 4.0, rect.w - PAGE_PAD * 2.0, 18.0),
        Font::BODY,
        TEXT_SM,
        if state.status_error {
            tk.danger
        } else {
            tk.muted
        },
    );
}

// -------------------------------------------------------------------- sheets

fn scrim(ui: &mut Ui) {
    let full = ui.vp.full();
    ui.rect(full, Rgba::rgb(0, 0, 0).with_alpha(0.62), 0.0);
    ui.hit(full, Action::Close, true);
}

fn sheet(ui: &mut Ui, width: f32, height: f32) -> Rect {
    scrim(ui);
    let width = width.min(ui.vp.width - 32.0);
    let height = height.min(ui.vp.height - 32.0);
    let rect = Rect::new(
        (ui.vp.width - width) * 0.5,
        (ui.vp.height - height) * 0.5,
        width,
        height,
    );
    ui.rect_border(rect, ui.tk.surface, 12.0, 1.0, ui.tk.line);
    // Registered after the scrim but before every widget, so a click on the
    // sheet's blank space does not close it.
    ui.hit(rect, Action::Consume, true);
    rect
}

fn sheet_header(ui: &mut Ui, rect: Rect, title: &str, subtitle: &str) {
    ui.text(
        title,
        Rect::new(rect.x + SHEET_PAD, rect.y + 16.0, rect.w - 120.0, 26.0),
        Font::HEAD,
        TEXT_H2,
        ui.tk.text,
    );
    if !subtitle.is_empty() {
        ui.text(
            subtitle,
            Rect::new(rect.x + SHEET_PAD, rect.y + 42.0, rect.w - 120.0, 16.0),
            Font::BODY,
            TEXT_SM,
            ui.tk.muted,
        );
    }
    ui.icon_button(
        Rect::new(rect.right() - SHEET_PAD - 34.0, rect.y + 16.0, 34.0, 34.0),
        Icon::Close,
        false,
        Action::Close,
    );
    ui.rect(
        Rect::new(rect.x, rect.y + SHEET_HEAD - 1.0, rect.w, 1.0),
        ui.tk.line,
        0.0,
    );
}

fn sheet_footer(
    ui: &mut Ui,
    rect: Rect,
    left: &[(&str, Action, ButtonKind)],
    right: &[(&str, Action, ButtonKind)],
) {
    let y = rect.bottom() - SHEET_FOOT;
    ui.rect(Rect::new(rect.x, y, rect.w, 1.0), ui.tk.line, 0.0);
    let mut right_x = rect.right() - SHEET_PAD;
    for (label, action, kind) in right.iter().rev() {
        let width = ui.button_width(label);
        right_x -= width;
        ui.button(
            Rect::new(right_x, y + 14.0, width, CONTROL_H),
            label,
            *kind,
            action.clone(),
            true,
        );
        right_x -= 8.0;
    }
    let mut left_x = rect.x + SHEET_PAD;
    for (label, action, kind) in left {
        let width = ui.button_width(label);
        ui.button(
            Rect::new(left_x, y + 14.0, width, CONTROL_H),
            label,
            *kind,
            action.clone(),
            true,
        );
        left_x += width + 8.0;
    }
}

fn body_rect(rect: Rect) -> Rect {
    Rect::new(
        rect.x + SHEET_PAD,
        rect.y + SHEET_HEAD + 14.0,
        rect.w - SHEET_PAD * 2.0,
        rect.h - SHEET_HEAD - SHEET_FOOT - 20.0,
    )
}

// -------------------------------------------------------------------- editor

fn editor(ui: &mut Ui, state: &AppState, draft: &EditorDraft) {
    let height = (ui.vp.height - 48.0).min(760.0);
    let rect = sheet(ui, 660.0, height);
    sheet_header(
        ui,
        rect,
        if draft.exists {
            "Edit game"
        } else {
            "New game"
        },
        "The command is passed to /bin/sh -lc exactly as written.",
    );

    let body = body_rect(rect);
    ui.push_clip(body);
    let scrolled = ui.scroll.sheet;
    let flow = Rect::new(body.x, body.y - scrolled, body.w, body.h + scrolled);
    let row = Row::new(flow, 16.0);

    if let Some(error) = &draft.error {
        let area = row.next(0.0);
        let note = ui.note(
            Rect::new(area.x, area.y, area.w, 0.0),
            error,
            NoteKind::Danger,
        );
        row.skip(note);
    }

    input_field(
        ui,
        Field::Name,
        &row,
        "Name",
        &draft.name,
        "My game",
        CONTROL_H,
    );
    input_field(
        ui,
        Field::Command,
        &row,
        "Command",
        &draft.command,
        "gamescope -f -- wine \"/games/Game/game.exe\"",
        104.0,
    );
    input_field(
        ui,
        Field::Workdir,
        &row,
        "Working directory (optional)",
        &draft.working_dir,
        "~/games/My Game",
        CONTROL_H,
    );
    input_field(
        ui,
        Field::Environment,
        &row,
        "Environment (optional, KEY=VALUE per line)",
        &draft.environment,
        "WINEPREFIX=~/.wine\nDXVK_HUD=fps",
        78.0,
    );
    input_field(
        ui,
        Field::Notes,
        &row,
        "Notes (optional)",
        &draft.notes,
        "Anything you want to remember.",
        70.0,
    );

    // Runner label.
    let area = row.next(FIELD_H);
    ui.label(
        "Runner label",
        Rect::new(area.x, area.y, area.w, FIELD_LABEL_H),
    );
    let options: Vec<&str> = RunnerLabel::ALL
        .iter()
        .map(|runner| runner.label())
        .collect();
    let current = RunnerLabel::ALL
        .iter()
        .position(|runner| *runner == draft.runner)
        .unwrap_or(0);
    ui.select(
        SelectKey::Runner,
        Rect::new(
            area.x,
            area.y + FIELD_LABEL_H + FIELD_GAP,
            area.w,
            CONTROL_H,
        ),
        draft.runner.label(),
        &options,
        current,
    );

    // Cover.
    let area = row.next(132.0);
    ui.label("Cover", Rect::new(area.x, area.y, area.w, FIELD_LABEL_H));
    let preview = Rect::new(area.x, area.y + 20.0, 96.0, 96.0);
    if let Some(image) = state.covers.get(&draft.id) {
        ui.cover(&cover_key(state, &draft.id), image, preview, 8.0);
    } else {
        let (a, b) = ui.tk.cover_gradient(draft.hue);
        ui.gradient_diag(preview, a, b, 8.0);
        ui.text(
            "No cover",
            Rect::new(preview.x, preview.y, preview.w, preview.h),
            Font::BODY,
            TEXT_SM,
            Rgba::rgb(255, 255, 255).with_alpha(0.8),
        );
    }
    let mut x = preview.right() + 12.0;
    let browse = if draft.cover_busy {
        "Importing…"
    } else {
        "Choose…"
    };
    let width = ui.button_width(browse);
    ui.button(
        Rect::new(x, preview.y, width, CONTROL_H),
        browse,
        ButtonKind::Default,
        Action::BrowseCover,
        !draft.cover_busy,
    );
    x += width + 8.0;
    if draft.cover.is_some() {
        let width = ui.button_width("Remove cover");
        ui.button(
            Rect::new(x, preview.y, width, CONTROL_H),
            "Remove cover",
            ButtonKind::DangerText,
            Action::RemoveCover,
            true,
        );
    }
    ui.text(
        "PNG is decoded natively; JPEG/WebP need ffmpeg or ImageMagick.",
        Rect::new(
            preview.right() + 12.0,
            preview.y + CONTROL_H + 8.0,
            (area.right() - preview.right() - 12.0).max(40.0),
            32.0,
        ),
        Font::BODY,
        TEXT_SM,
        ui.tk.muted,
    );

    let content_h = row.consumed();
    let max = (content_h - body.h).max(0.0);
    ui.max.sheet = max;
    if max > 0.0 {
        ui.scroll_regions.push((body, ScrollTarget::Sheet));
    }
    ui.pop_clip();

    sheet_footer(
        ui,
        rect,
        &[],
        &[
            ("Cancel", Action::Close, ButtonKind::Default),
            ("Save", Action::SaveEditor, ButtonKind::Primary),
        ],
    );
}

fn input_field(
    ui: &mut Ui,
    field: Field,
    row: &Row,
    label: &str,
    text: &str,
    placeholder: &str,
    height: f32,
) {
    let area = row.next(FIELD_LABEL_H + FIELD_GAP + height);
    ui.label(label, Rect::new(area.x, area.y, area.w, FIELD_LABEL_H));
    ui.input(
        field,
        Rect::new(area.x, area.y + FIELD_LABEL_H + FIELD_GAP, area.w, height),
        text,
        placeholder,
    );
}

// ------------------------------------------------------------------ settings

fn settings(ui: &mut Ui, state: &AppState) {
    let rect = sheet(ui, 540.0, 620.0);
    sheet_header(ui, rect, "Settings", "Window and library appearance.");
    let body = body_rect(rect);
    let row = Row::new(body, 16.0);

    let area = row.next(FIELD_H);
    ui.label("Theme", Rect::new(area.x, area.y, area.w, FIELD_LABEL_H));
    let themes: Vec<&str> = ThemeName::ALL.iter().map(|theme| theme.label()).collect();
    let current = ThemeName::ALL
        .iter()
        .position(|theme| *theme == state.settings.theme)
        .unwrap_or(0);
    ui.select(
        SelectKey::Theme,
        Rect::new(
            area.x,
            area.y + FIELD_LABEL_H + FIELD_GAP,
            area.w,
            CONTROL_H,
        ),
        state.settings.theme.label(),
        &themes,
        current,
    );

    let area = row.next(FIELD_H);
    ui.label(
        "Sort games",
        Rect::new(area.x, area.y, area.w, FIELD_LABEL_H),
    );
    let orders = [SortOrder::Name.label(), SortOrder::Type.label()];
    ui.select(
        SelectKey::Sort,
        Rect::new(
            area.x,
            area.y + FIELD_LABEL_H + FIELD_GAP,
            area.w,
            CONTROL_H,
        ),
        state.settings.sort.label(),
        &orders,
        if state.settings.sort == SortOrder::Name {
            0
        } else {
            1
        },
    );

    let area = row.next(Ui::toggle_height());
    ui.toggle(
        area,
        "Transparent window",
        "Ask the compositor for a translucent background.",
        state.settings.transparent,
        Action::ToggleTransparency,
    );

    let area = row.next(Ui::toggle_height());
    ui.toggle(
        area,
        "Solid fallback",
        "Use an opaque background when transparency is unavailable.",
        state.settings.solid_fallback,
        Action::ToggleSolid,
    );

    let area = row.next(FIELD_LABEL_H + 10.0 + 28.0);
    ui.label(
        &format!("Opacity — {}%", state.settings.opacity),
        Rect::new(area.x, area.y, area.w, FIELD_LABEL_H),
    );
    ui.slider(
        Rect::new(area.x, area.y + FIELD_LABEL_H + 8.0, area.w, 20.0),
        (state.settings.opacity.saturating_sub(30) as f32) / 70.0,
    );

    sheet_footer(
        ui,
        rect,
        &[],
        &[("Done", Action::Close, ButtonKind::Primary)],
    );
}

// ----------------------------------------------------------------------- logs

fn logs(ui: &mut Ui, state: &AppState, view: &LogsView) {
    let rect = sheet(ui, 820.0, (ui.vp.height - 40.0).min(720.0));
    let name = state
        .game(&view.game)
        .map(|game| game.name.as_str())
        .unwrap_or("Game");
    sheet_header(
        ui,
        rect,
        &format!("Logs — {name}"),
        "The latest 10 executions are kept.",
    );
    let body = body_rect(rect);
    let list_w = 252.0f32.min(body.w * 0.36);
    let list = Rect::new(body.x, body.y, list_w, body.h);
    let detail = Rect::new(
        list.right() + 16.0,
        body.y,
        (body.right() - list.right() - 16.0).max(80.0),
        body.h,
    );

    let running = state.launcher.is_active(&view.game);
    let mut entries = 0usize;
    if running {
        entries += 1;
    }
    entries += view.records.len();

    ui.push_clip(list);
    for position in 0..entries {
        let item = Rect::new(list.x, list.y + position as f32 * 48.0, list.w, 44.0);
        if item.bottom() > list.bottom() {
            break;
        }
        let selected = view.index == position;
        let hovered = ui.hovered(item);
        if selected || hovered {
            ui.rect(item, ui.tk.wash, RADIUS);
        }
        let (line, status) = if running && position == 0 {
            ("Running now".to_string(), "live output".to_string())
        } else {
            let index = if running { position - 1 } else { position };
            match view.records.get(index) {
                Some(record) => {
                    let label = list_label(record);
                    let mut lines = label.lines();
                    (
                        lines.next().unwrap_or_default().to_string(),
                        lines.next().unwrap_or_default().to_string(),
                    )
                }
                None => (String::new(), String::new()),
            }
        };
        ui.text(
            &line,
            Rect::new(item.x + 10.0, item.y + 5.0, item.w - 20.0, 16.0),
            Font::BODY,
            TEXT_SM,
            ui.tk.text,
        );
        ui.text(
            &status,
            Rect::new(item.x + 10.0, item.y + 23.0, item.w - 20.0, 15.0),
            Font::BODY,
            TEXT_SM,
            if running && position == 0 {
                ui.tk.ok
            } else {
                ui.tk.muted
            },
        );
        ui.hit(item, Action::PickLog(position), true);
    }
    ui.pop_clip();

    let text = actions::selected_log_text(state, view);
    let content_h = ui.code(detail, &text, ui.scroll.logs, ScrollTarget::Logs);
    let max = (content_h - (detail.h - 24.0)).max(0.0);
    ui.max.logs = max;
    if max > 0.0 {
        ui.scroll_regions.push((detail, ScrollTarget::Logs));
    }

    sheet_footer(
        ui,
        rect,
        &[
            ("Copy", Action::CopyLog, ButtonKind::Default),
            ("Clear", Action::ClearLogs, ButtonKind::DangerText),
        ],
        &[("Close", Action::Close, ButtonKind::Primary)],
    );
}

// --------------------------------------------------------------------- remove

fn remove(ui: &mut Ui, view: &RemoveView) {
    let rect = sheet(ui, 480.0, 280.0);
    sheet_header(ui, rect, "Remove game", "");
    let body = body_rect(rect);
    ui.wrapped(
        &format!(
            "Remove “{}” from the library?\n\nThis deletes its saved execution logs and any cover MGL copied. Game files, installers and Wine prefixes are never touched.",
            view.name
        ),
        Rect::new(body.x, body.y, body.w, body.h),
        Font::BODY,
        TEXT_UI,
        ui.tk.text,
    );
    sheet_footer(
        ui,
        rect,
        &[],
        &[
            ("Cancel", Action::Close, ButtonKind::Default),
            ("Remove", Action::ConfirmRemove, ButtonKind::Danger),
        ],
    );
}

/// Renderer key for a cover. The epoch changes after an import, which forces a
/// texture-array layer upload instead of reusing stale pixels.
fn cover_key(state: &AppState, id: &str) -> String {
    format!("{id}#{}", state.cover_epoch)
}

/// Keeps the unused import honest when the viewport helper is not referenced.
#[allow(dead_code)]
fn _viewport_marker(_: Viewport) {}
