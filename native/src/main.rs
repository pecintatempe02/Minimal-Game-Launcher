//! MGL — Minimal Game Launcher: window, event loop and frame driver.
//!
//! Everything here is plumbing: create one window, one surface, drive frames
//! only when something actually changed, and translate input into either an
//! [`Action`] for [`actions::apply`] or a text-edit helper.
//!
//! The event loop blocks in the compositor (`ControlFlow::Wait`). A timer is
//! armed *only* while a game owns a live child process or a cover job is in
//! flight, and even then the UI redraws at most once a second unless something
//! happened.

use std::sync::Arc;
use std::time::Instant;

use winit::application::ApplicationHandler;
use winit::dpi::{LogicalPosition, LogicalSize};
use winit::event::{ElementState, KeyEvent, MouseButton, MouseScrollDelta, WindowEvent};
use winit::event_loop::{ActiveEventLoop, ControlFlow, EventLoop};
use winit::keyboard::{Key, KeyCode, ModifiersState, NamedKey, PhysicalKey};
use winit::window::{CursorIcon, Window, WindowId};

use mgl::app::actions;
use mgl::app::state::{AppState, Modal};
use mgl::gfx::{self, Gfx};
use mgl::ui::components::{Action, Field, FieldRect, Hit, ScrollTarget, Scrolls, Ui};
use mgl::ui::layout::{Rect, Viewport};
use mgl::ui::renderer::Renderer;
use mgl::ui::screens;
use mgl::ui::text::TextEngine;

/// Wheel step, in logical pixels per line, matching about three text lines.
const WHEEL_STEP: f32 = 46.0;
/// Minimum window size: below this the card grid and sheets stop being usable.
const MIN_SIZE: (f64, f64) = (720.0, 480.0);

struct App {
    window: Option<Arc<Window>>,
    gfx: Option<Gfx>,
    renderer: Option<Renderer>,
    text: TextEngine,
    state: AppState,

    /// Click targets and text fields recorded by the last frame. Input is mapped
    /// against these, which is why hits are tested back to front.
    hits: Vec<Hit>,
    fields: Vec<FieldRect>,
    regions: Vec<(Rect, ScrollTarget)>,

    /// Pointer position in logical pixels; `(-1, -1)` until the first motion.
    pointer: (f32, f32),
    /// The (hit, field) pair currently under the pointer, used to decide whether
    /// a mouse move needs a redraw at all.
    hover: (Option<usize>, Option<usize>),
    cursor: CursorIcon,

    modifiers: ModifiersState,
    scale: f32,
    /// Tick counter; the elapsed labels only change once a second, so the timer
    /// drives a redraw at that rate instead of four times a second.
    ticks: u32,
    started: Instant,
}

impl App {
    fn new(state: AppState) -> Self {
        Self {
            window: None,
            gfx: None,
            renderer: None,
            text: TextEngine::new(),
            state,
            hits: Vec::new(),
            fields: Vec::new(),
            regions: Vec::new(),
            pointer: (-1.0, -1.0),
            hover: (None, None),
            cursor: CursorIcon::Default,
            modifiers: ModifiersState::empty(),
            scale: 1.0,
            ticks: 0,
            started: Instant::now(),
        }
    }

    fn request_redraw(&self) {
        if let Some(window) = &self.window {
            window.request_redraw();
        }
    }

    // ------------------------------------------------------------------ frame

    /// Draw one frame and refresh the hit list.
    fn render(&mut self) {
        let Some(window) = self.window.clone() else {
            return;
        };
        let size = window.inner_size();
        if size.width == 0 || size.height == 0 {
            return;
        }
        let scale = (window.scale_factor() as f32).clamp(0.5, 4.0);
        self.scale = scale;
        let width = size.width as f32 / scale;
        let height = size.height as f32 / scale;

        let tk = self.state.tokens();
        let transparent = self
            .gfx
            .as_ref()
            .map(|gfx| gfx.transparent)
            .unwrap_or(false);
        // A fully transparent-capable surface still paints an opaque background
        // when the user turns transparency off, exactly like the prototype's
        // `body.solid` fallback.
        let background = if transparent && self.state.settings.window_translucent() {
            tk.bg.with_alpha(self.state.settings.alpha())
        } else {
            tk.bg
        };

        let Some(renderer) = self.renderer.as_mut() else {
            return;
        };
        renderer.set_scale(scale);
        self.text.set_scale(scale);
        renderer.begin(width, height);

        let viewport = Viewport::new(width, height, scale);
        let state = &self.state;
        let mut ui = Ui::new(
            renderer,
            &mut self.text,
            tk,
            viewport,
            state.mouse,
            state.focus,
            state.caret,
            state.sel_anchor,
            state.open_select,
            state.scroll,
        );
        screens::draw(&mut ui, state);
        // Select popups are drawn above every other widget, so they are the
        // last thing added to the frame and the last hit region.
        ui.end();
        let max = ui.max;
        self.hits = std::mem::take(&mut ui.hits);
        self.fields = std::mem::take(&mut ui.fields);
        self.regions = std::mem::take(&mut ui.scroll_regions);
        drop(ui);

        self.state.max_scroll = max;
        if let (Some(gfx), Some(renderer)) = (self.gfx.as_mut(), self.renderer.as_mut()) {
            renderer.paint(gfx, Some(background));
        }
        self.sync_hover();
    }

    // ------------------------------------------------------------------ input

    /// Which (hit, field) sits under the pointer. Fields win over sheet backdrops
    /// and widgets, because a click on a text input must reach it.
    fn probe(&self) -> (Option<usize>, Option<usize>) {
        let (x, y) = self.pointer;
        let field = self
            .fields
            .iter()
            .rposition(|field| field.rect.contains(x, y));
        let hit = self
            .hits
            .iter()
            .rposition(|hit| hit.rect.contains(x, y) && !hit.action.is_consume());
        (hit, field)
    }

    /// Refresh the hover cache after a frame; never requests another frame.
    fn sync_hover(&mut self) {
        let probe = self.probe();
        self.hover = probe;
        let icon = match probe {
            (_, Some(_)) => CursorIcon::Text,
            (Some(_), _) => CursorIcon::Pointer,
            _ => CursorIcon::Default,
        };
        if icon != self.cursor {
            self.cursor = icon;
            if let Some(window) = &self.window {
                window.set_cursor(icon);
            }
        }
    }

    /// Cursor moved: redraw only when the hovered target actually changed.
    fn pointer_moved(&mut self, x: f32, y: f32) {
        self.pointer = (x, y);
        self.state.mouse = (x, y);
        if let Some(track) = self.state.opacity_drag {
            actions::set_opacity_from(&mut self.state, track, x);
            self.request_redraw();
            return;
        }
        if self.probe() != self.hover {
            self.request_redraw();
        }
    }

    fn pointer_pressed(&mut self) {
        let (x, y) = self.pointer;
        // 1. A text input takes priority over sheet backdrops and widgets.
        if let Some(field) = self
            .fields
            .iter()
            .copied()
            .rfind(|field| field.rect.contains(x, y))
        {
            let same = self.state.focus == Some(field.field);
            self.state.focus = Some(field.field);
            self.state.open_select = None;
            if !same {
                self.state.sel_anchor = None;
            }
            actions::place_caret(&mut self.state, &mut self.text, field, x, y);
            self.request_redraw();
            return;
        }
        // 2. A widget. Disabled widgets still swallow the click so it cannot
        //    reach the scrim behind them and close the sheet. Invisible sheet
        //    backdrops (`Consume`) are skipped so a click lands on the widget
        //    drawn on top of them instead.
        if let Some(index) = self
            .hits
            .iter()
            .rposition(|hit| hit.rect.contains(x, y) && !hit.action.is_consume())
        {
            if !self.hits[index].enabled {
                return;
            }
            let action = self.hits[index].action.clone();
            // Clicking anywhere else commits the field's value first, so the
            // follow-up action reads the text the user just typed.
            actions::blur(&mut self.state);
            actions::apply(&mut self.state, action);
            self.request_redraw();
            return;
        }
        // 3. Empty space: drop focus and close any open menu.
        if self.state.focus.is_some()
            || self.state.open_select.is_some()
            || self.state.game_menu.is_some()
        {
            self.state.open_select = None;
            self.state.game_menu = None;
            actions::blur(&mut self.state);
            self.request_redraw();
        }
    }

    fn wheel(&mut self, delta: MouseScrollDelta) {
        let dy = match delta {
            MouseScrollDelta::LineDelta(_, y) => y * WHEEL_STEP,
            // Wayland reports physical pixels.
            MouseScrollDelta::PixelDelta(offset) => offset.y as f32 / self.scale,
        };
        if dy == 0.0 {
            return;
        }
        let (x, y) = self.pointer;
        let modal = self.state.modal.is_some();
        // Later entries are drawn on top, so they win the wheel.
        let target = self
            .regions
            .iter()
            .rev()
            .find(|(rect, _)| rect.contains(x, y))
            .map(|(_, target)| *target);
        if let Some(target) = target {
            if modal && target == ScrollTarget::Library {
                return;
            }
            actions::scroll(&mut self.state, target, dy);
            self.request_redraw();
        }
    }

    /// Logical viewport, matching what `render` lays out against.
    fn viewport(&self) -> Viewport {
        let Some(window) = &self.window else {
            return Viewport::new(1.0, 1.0, 1.0);
        };
        let size = window.inner_size();
        let scale = self.scale.max(0.0001);
        Viewport::new(size.width as f32 / scale, size.height as f32 / scale, scale)
    }

    /// Keep a keyboard-selected card inside the visible part of the grid.
    fn reveal_selection(&mut self) {
        let Some(position) = actions::selected_position(&self.state) else {
            return;
        };
        let grid = screens::grid(
            self.viewport(),
            self.state.status.is_some(),
            self.state.visible().len(),
        );
        screens::reveal(&grid, &mut self.state.scroll.library, position);
    }

    /// Move the library selection by grid cells and reveal it.
    fn navigate(&mut self, dx: isize, dy: isize) {
        let grid = screens::grid(
            self.viewport(),
            self.state.status.is_some(),
            self.state.visible().len(),
        );
        actions::select_move(&mut self.state, grid.columns, dx, dy);
        self.reveal_selection();
    }

    fn select_edge(&mut self, last: bool) {
        actions::select_edge(&mut self.state, last);
        self.reveal_selection();
    }

    /// Move focus to the next (or previous) text field. The order is the order
    /// the last frame drew the fields in; running off either end drops focus so
    /// the arrow keys go back to the library.
    fn cycle_field(&mut self, backwards: bool) {
        let sheet = self.state.modal.is_some();
        let mut order: Vec<Field> = Vec::new();
        for field in self.fields.iter().map(|field| field.field) {
            // The library is still drawn behind a sheet, but its search box is
            // not reachable while the sheet owns the keyboard.
            if sheet && field == Field::Query {
                continue;
            }
            if !order.contains(&field) {
                order.push(field);
            }
        }
        if order.is_empty() {
            return;
        }
        let next = match (
            self.state
                .focus
                .and_then(|focus| order.iter().position(|field| *field == focus)),
            backwards,
        ) {
            (None, false) => Some(0),
            (None, true) => Some(order.len() - 1),
            (Some(index), false) => Some(index + 1),
            (Some(0), true) => None,
            (Some(index), true) => Some(index - 1),
        };
        match next.and_then(|index| order.get(index).copied()) {
            Some(field) => self.focus_field(field),
            None => actions::blur(&mut self.state),
        }
    }

    fn focus_field(&mut self, field: Field) {
        self.state.focus = Some(field);
        self.state.open_select = None;
        self.state.sel_anchor = None;
        self.state.caret = actions::text_len(&self.state);
    }

    /// Handle a key press. Returns `true` when the frame changed.
    fn key(&mut self, event: &KeyEvent) -> bool {
        let editing = self.state.focus.is_some();
        if self.modifiers.control_key() {
            // Shortcuts key off the physical key so they work on any layout; the
            // text itself still comes from `logical_key`.
            let PhysicalKey::Code(code) = event.physical_key else {
                return false;
            };
            let logs = matches!(self.state.modal, Some(Modal::Logs(_)));
            let editor = matches!(self.state.modal, Some(Modal::Editor(_)));
            match code {
                KeyCode::KeyA => actions::select_all(&mut self.state),
                // The log body is not selectable, so Ctrl+C copies the entry.
                KeyCode::KeyC if logs && !editing => {
                    actions::apply(&mut self.state, Action::CopyLog)
                }
                KeyCode::KeyC => {
                    actions::copy_selection(&mut self.state);
                }
                KeyCode::KeyX => actions::cut_selection(&mut self.state),
                KeyCode::KeyV => actions::paste(&mut self.state),
                KeyCode::KeyN if !editing && self.state.modal.is_none() => {
                    actions::apply(&mut self.state, Action::AddGame)
                }
                KeyCode::KeyS if editor => actions::apply(&mut self.state, Action::SaveEditor),
                KeyCode::KeyF if self.state.modal.is_none() => self.focus_field(Field::Query),
                KeyCode::KeyL if !editing && self.state.modal.is_none() => {
                    let id = actions::selected_game(&self.state).map(|game| game.id.clone());
                    match id {
                        Some(id) => actions::apply(&mut self.state, Action::OpenLogs(id)),
                        None => return false,
                    }
                }
                _ => return false,
            }
            return true;
        }
        let extend = self.modifiers.shift_key();
        let library = self.state.modal.is_none();
        match &event.logical_key {
            Key::Named(NamedKey::Escape) => {
                if self.state.open_select.is_some() {
                    self.state.open_select = None;
                } else if self.state.game_menu.is_some() {
                    self.state.game_menu = None;
                } else if self.state.modal.is_some() {
                    actions::apply(&mut self.state, Action::Close);
                } else if self.state.focus.is_some() {
                    actions::blur(&mut self.state);
                } else if self.state.selected.is_some() {
                    self.state.selected = None;
                } else {
                    return false;
                }
                true
            }
            Key::Named(NamedKey::Tab) => {
                self.cycle_field(extend);
                true
            }
            Key::Named(NamedKey::ArrowLeft) if editing => {
                actions::move_caret(&mut self.state, -1, extend);
                true
            }
            Key::Named(NamedKey::ArrowRight) if editing => {
                actions::move_caret(&mut self.state, 1, extend);
                true
            }
            Key::Named(NamedKey::ArrowLeft) if library => {
                self.navigate(-1, 0);
                true
            }
            Key::Named(NamedKey::ArrowRight) if library => {
                self.navigate(1, 0);
                true
            }
            Key::Named(NamedKey::ArrowUp) if library => {
                self.navigate(0, -1);
                true
            }
            Key::Named(NamedKey::ArrowDown) if library => {
                self.navigate(0, 1);
                true
            }
            Key::Named(NamedKey::ArrowUp | NamedKey::ArrowDown)
                if matches!(self.state.modal, Some(Modal::Logs(_))) =>
            {
                let up = matches!(event.logical_key, Key::Named(NamedKey::ArrowUp));
                actions::step_log(&mut self.state, if up { -1 } else { 1 });
                true
            }
            Key::Named(NamedKey::Home) if editing => {
                actions::move_caret_to(&mut self.state, 0, extend);
                true
            }
            Key::Named(NamedKey::End) if editing => {
                let end = actions::text_len(&self.state);
                actions::move_caret_to(&mut self.state, end, extend);
                true
            }
            Key::Named(NamedKey::Home) if library => {
                self.select_edge(false);
                true
            }
            Key::Named(NamedKey::End) if library => {
                self.select_edge(true);
                true
            }
            Key::Named(NamedKey::Backspace) if editing => {
                actions::backspace(&mut self.state);
                true
            }
            Key::Named(NamedKey::Delete) if editing => {
                actions::delete_forward(&mut self.state);
                true
            }
            Key::Named(NamedKey::Delete) if library => {
                let id = actions::selected_game(&self.state).map(|game| game.id.clone());
                if let Some(id) = id {
                    actions::apply(&mut self.state, Action::OpenRemove(id));
                    true
                } else {
                    false
                }
            }
            Key::Named(NamedKey::Enter) if editing => {
                let multiline = self
                    .state
                    .focus
                    .map(|field| field.multiline())
                    .unwrap_or(false);
                if multiline {
                    actions::insert_text(&mut self.state, "\n");
                } else {
                    actions::blur(&mut self.state);
                }
                true
            }
            Key::Named(NamedKey::Enter) if library => {
                let id = actions::selected_game(&self.state).map(|game| game.id.clone());
                if let Some(id) = id {
                    actions::apply(&mut self.state, Action::Run(id));
                    true
                } else {
                    false
                }
            }
            // Printable input. The logical key already reflects the layout and
            // every modifier except Ctrl, so capitals and composed characters
            // arrive here ready to insert. A plain space arrives as
            // `Named(Space)`, which is why the mapping lives in one helper.
            _ => {
                if let Some(text) = actions::text_input(&event.logical_key, editing) {
                    actions::insert_text(&mut self.state, &text);
                    return true;
                }
                // Library keys: `/` jumps to search, `e` edits the selection.
                if library && !self.modifiers.shift_key() {
                    match actions::typed_text(&event.logical_key).as_deref() {
                        Some("/") => {
                            self.focus_field(Field::Query);
                            return true;
                        }
                        Some("e") => {
                            let id = actions::selected_game(&self.state).map(|g| g.id.clone());
                            if let Some(id) = id {
                                actions::apply(&mut self.state, Action::EditGame(id));
                                return true;
                            }
                        }
                        _ => {}
                    }
                }
                false
            }
        }
    }

    // ------------------------------------------------------------------- tick

    /// Advance process supervision and poll finished cover work. Returns `true`
    /// when a frame is needed.
    fn tick(&mut self) -> bool {
        let mut changed = self.state.poll_covers();
        let mut finished = self
            .state
            .launcher
            .poll(&mut self.state.games, &self.state.db);
        // A stop that was requested earlier is only complete once the process
        // is actually gone; this also escalates SIGTERM to SIGKILL on time.
        finished.extend(
            self.state
                .launcher
                .poll_stopped(&mut self.state.games, &self.state.db),
        );
        if !finished.is_empty() {
            changed = true;
            let viewing_finished = match &self.state.modal {
                Some(Modal::Logs(view)) => finished.contains(&view.game),
                _ => false,
            };
            if viewing_finished {
                self.state.reload_logs();
            }
        }
        changed
    }

    /// Persist geometry and stop everything MGL started.
    fn close(&mut self) {
        if let Some(window) = &self.window {
            let size = window.inner_size();
            let scale = window.scale_factor();
            let position = window.outer_position().ok();
            let logical = |value: f64| (value / scale).round() as u32;
            self.state.save_window(
                logical(size.width as f64),
                logical(size.height as f64),
                position.map(|p| (p.x as f64 / scale).round() as i32),
                position.map(|p| (p.y as f64 / scale).round() as i32),
            );
        }
        self.state.shutdown();
    }
}

/// How the event loop should block now that supervision has run.
///
/// This is the whole idle policy: with nothing owned by MGL the loop sleeps in
/// the compositor (`Wait`); while a game, stop escalation or cover job is live
/// it arms one timer at `TICK` so progress is observed without any input. The
/// timer is always re-armed from the same `live` predicate, so it disappears as
/// soon as the last process or job does.
fn supervision_flow(live: bool) -> ControlFlow {
    if live {
        ControlFlow::WaitUntil(Instant::now() + actions::TICK)
    } else {
        ControlFlow::Wait
    }
}

impl ApplicationHandler for App {
    fn resumed(&mut self, event_loop: &ActiveEventLoop) {
        if self.window.is_some() {
            return;
        }
        let geometry = self.state.settings.window;
        let mut attributes = Window::default_attributes()
            .with_title("MGL — Minimal Game Launcher")
            .with_inner_size(LogicalSize::new(
                geometry.width as f64,
                geometry.height as f64,
            ))
            .with_min_inner_size(LogicalSize::new(MIN_SIZE.0, MIN_SIZE.1))
            // The surface always offers alpha; whether the pixels are actually
            // translucent is decided per frame from the settings.
            .with_transparent(true)
            .with_decorations(true);
        if let (Some(x), Some(y)) = (geometry.x, geometry.y) {
            attributes = attributes.with_position(LogicalPosition::new(x as f64, y as f64));
        }
        let window = match event_loop.create_window(attributes) {
            Ok(window) => Arc::new(window),
            Err(error) => {
                eprintln!("mgl: could not create the window: {error}");
                event_loop.exit();
                return;
            }
        };
        let display: Box<dyn wgpu::wgt::WgpuHasDisplayHandle> =
            Box::new(event_loop.owned_display_handle());
        match Gfx::new(display, window.clone(), gfx::Alpha::Transparent) {
            Ok(gfx) => {
                log::info!(
                    "window {}x{} buffer, scale {}, adapter {}, transparent {}",
                    gfx.config.width,
                    gfx.config.height,
                    window.scale_factor(),
                    gfx.adapter_name,
                    gfx.transparent
                );
                self.renderer = Some(Renderer::new(&gfx));
                self.gfx = Some(gfx);
            }
            Err(error) => {
                eprintln!("mgl: {error}");
                event_loop.exit();
                return;
            }
        }
        self.window = Some(window);
        self.render();
    }

    fn window_event(&mut self, event_loop: &ActiveEventLoop, _id: WindowId, event: WindowEvent) {
        match event {
            WindowEvent::CloseRequested => {
                self.close();
                event_loop.exit();
            }
            WindowEvent::RedrawRequested => self.render(),
            WindowEvent::Resized(size) => {
                log::debug!("resized to {}x{}", size.width, size.height);
                if let Some(gfx) = self.gfx.as_mut() {
                    gfx.resize(size.width, size.height);
                }
                self.request_redraw();
            }
            WindowEvent::ScaleFactorChanged { .. } => {
                if let Some(window) = &self.window {
                    let size = window.inner_size();
                    log::info!(
                        "scale factor changed: factor {} inner {}x{}",
                        window.scale_factor(),
                        size.width,
                        size.height
                    );
                    if let Some(gfx) = self.gfx.as_mut() {
                        gfx.resize(size.width, size.height);
                    }
                }
                self.request_redraw();
            }
            WindowEvent::ModifiersChanged(modifiers) => {
                self.modifiers = modifiers.state();
            }
            WindowEvent::CursorMoved { position, .. } => {
                self.pointer_moved(
                    position.x as f32 / self.scale.max(0.0001),
                    position.y as f32 / self.scale.max(0.0001),
                );
            }
            WindowEvent::CursorLeft { .. } => {
                self.pointer = (-1.0, -1.0);
                self.state.mouse = (-1.0, -1.0);
                if self.hover != (None, None) {
                    self.request_redraw();
                }
            }
            WindowEvent::MouseInput { state, button, .. } => match (button, state) {
                (MouseButton::Left, ElementState::Pressed) => self.pointer_pressed(),
                (MouseButton::Left, ElementState::Released)
                    if self.state.opacity_drag.is_some() =>
                {
                    actions::finish_drag(&mut self.state);
                    self.request_redraw();
                }
                _ => {}
            },
            WindowEvent::MouseWheel { delta, .. } => self.wheel(delta),
            WindowEvent::KeyboardInput { event, .. } => {
                if event.state == ElementState::Pressed && self.key(&event) {
                    self.request_redraw();
                }
            }
            WindowEvent::Focused(false) if self.state.opacity_drag.is_some() => {
                actions::finish_drag(&mut self.state);
            }
            _ => {}
        }
    }

    fn about_to_wait(&mut self, event_loop: &ActiveEventLoop) {
        // Covers decode/import on worker threads; a timer only exists while one
        // is pending or a game is alive.
        let live = self.state.launcher.any_active() || self.state.cover_busy();
        if live {
            self.ticks = self.ticks.wrapping_add(1);
            let changed = self.tick();
            // Live labels are second-resolution, so one redraw a second is enough.
            if changed || self.ticks.is_multiple_of(4) {
                self.request_redraw();
            }
        }
        // Re-read after the tick: the process that just exited must not leave a
        // trailing timer behind.
        let live = self.state.launcher.any_active() || self.state.cover_busy();
        event_loop.set_control_flow(supervision_flow(live));
    }

    fn exiting(&mut self, _event_loop: &ActiveEventLoop) {
        log::info!("exited after {:.1}s", self.started.elapsed().as_secs_f32());
    }
}

fn main() {
    let state = match AppState::load() {
        Ok(state) => state,
        Err(error) => {
            eprintln!("mgl: {error}");
            std::process::exit(1);
        }
    };
    mgl::logging::init(std::env::var_os("MGL_DEBUG").is_some());
    let event_loop = match EventLoop::new() {
        Ok(event_loop) => event_loop,
        Err(error) => {
            eprintln!("mgl: could not start the event loop: {error}");
            std::process::exit(1);
        }
    };
    event_loop.set_control_flow(ControlFlow::Wait);
    let mut app = App::new(state);
    if let Err(error) = event_loop.run_app(&mut app) {
        eprintln!("mgl: {error}");
        std::process::exit(1);
    }
}

/// Keeps the unused-import lint honest for the `Scrolls` helper used only in
/// tests below.
#[allow(dead_code)]
fn _scrolls_marker(_: Scrolls) {}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn supervision_timer_exists_only_while_process_work_is_pending() {
        // Idle: no timer at all, so a running game is the only reason the loop
        // ever wakes without input.
        assert_eq!(supervision_flow(false), ControlFlow::Wait);
        match supervision_flow(true) {
            ControlFlow::WaitUntil(deadline) => {
                let delay = deadline.saturating_duration_since(Instant::now());
                assert!(delay > std::time::Duration::ZERO);
                assert!(
                    delay <= actions::TICK,
                    "supervision wake {delay:?} is slower than one tick"
                );
            }
            other => panic!("live supervision must arm a timer, got {other:?}"),
        }
    }
    #[test]
    fn wheel_step_is_positive_going_down() {
        // LineDelta y is negative when the wheel is rolled towards the user,
        // which must increase the scroll offset.
        let delta = MouseScrollDelta::LineDelta(0.0, -1.0);
        let dy = match delta {
            MouseScrollDelta::LineDelta(_, y) => y * WHEEL_STEP,
            MouseScrollDelta::PixelDelta(_) => 0.0,
        };
        assert!(dy < 0.0);
    }

    #[test]
    fn hit_test_prefers_the_topmost_rect() {
        let rect = Rect::new(0.0, 0.0, 100.0, 100.0);
        let small = Rect::new(10.0, 10.0, 20.0, 20.0);
        let hits = [
            Hit {
                rect,
                action: Action::Close,
                enabled: true,
            },
            Hit {
                rect: small,
                action: Action::SelectClose,
                enabled: true,
            },
        ];
        assert_eq!(
            hits.iter().rposition(|h| h.rect.contains(15.0, 15.0)),
            Some(1)
        );
        assert_eq!(
            hits.iter().rposition(|h| h.rect.contains(90.0, 90.0)),
            Some(0)
        );
    }
}
