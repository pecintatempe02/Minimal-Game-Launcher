//! Immediate-mode widgets.
//!
//! Every widget takes the rectangle it should occupy, draws itself, and records
//! a [`Hit`] (or a [`FieldRect`]) so the app can map the next click back to an
//! [`Action`]. Layout is therefore just arithmetic in the screens; there is no
//! retained widget tree, no invalidation, and no per-frame allocation beyond the
//! hit list.
//!
//! The bundled font subsets contain no symbol glyphs, so icons are composed from
//! rotated capsules, circles and triangles drawn by the same quad pipeline.

use crate::ui::layout::{Rect, Viewport};
use crate::ui::renderer::{Renderer, Shape};
use crate::ui::text::{Font, TextEngine};
use crate::ui::theme::*;

// --------------------------------------------------------------------- types

/// Text inputs the app owns. The variant *is* the identity; the screens read the
/// text from the model, so there is no copy to keep in sync.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Field {
    Query,
    Name,
    Command,
    Workdir,
    Environment,
    Notes,
}

impl Field {
    pub fn multiline(self) -> bool {
        matches!(self, Field::Command | Field::Environment | Field::Notes)
    }
}

/// Top of a single-line text box inside a control of height `area_h`.
///
/// The prototype centres the CSS line box in the control, so the baseline lands
/// at `(area_h - line_h) / 2 + ascent` — where the browser puts it. The shape's
/// own height already *is* that line box (the font's ascent + descent), so this
/// needs no hard-coded ratio.
pub fn line_box_top(area_h: f32, line_h: f32) -> f32 {
    ((area_h - line_h) * 0.5).max(0.0)
}

/// Where text is drawn inside a control, from its box and the real line metrics.
///
/// A single line is the line box itself, centred in the control (the renderer
/// then has nothing left to centre); a textarea starts at the prototype's 9px
/// top padding and flows down. `line_h` must come from [`TextEngine`] so the
/// position follows the font, not a guessed offset.
pub fn text_rect(area: Rect, multiline: bool, line_h: f32) -> Rect {
    if multiline {
        Rect::new(area.x, area.y + 9.0, area.w, (area.h - 16.0).max(1.0))
    } else {
        Rect::new(
            area.x,
            area.y + line_box_top(area.h, line_h),
            area.w,
            line_h,
        )
    }
}

/// Drop-down menus. The options are rebuilt every frame from the model.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SelectKey {
    Sort,
    Theme,
    Runner,
}

/// Scrollable regions. The app routes wheel events to the region under the mouse.
#[derive(Clone, Copy, Debug, Default)]
pub struct Scrolls {
    pub library: f32,
    pub sheet: f32,
    pub logs: f32,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ScrollTarget {
    Library,
    Sheet,
    Logs,
}

/// Something the user can click.
#[derive(Clone, Debug)]
pub enum Action {
    // library
    Filter(crate::core::library::Filter),
    ToggleSidebar,
    OpenSettings,
    AddGame,
    EditGame(String),
    Run(String),
    OpenLogs(String),
    OpenRemove(String),
    /// Per-game options popover, anchored to the card's settings button.
    OpenGameMenu {
        game: String,
        anchor: Rect,
    },
    CloseGameMenu,
    // modal shell
    Close,
    /// Swallow a click on a sheet's own background so it cannot reach the scrim.
    Consume,
    SelectClose,
    // settings
    SelectOpen(SelectKey),
    SelectPick(SelectKey, usize),
    ToggleTransparency,
    ToggleSolid,
    Opacity(Rect),
    // editor
    SaveEditor,
    BrowseCover,
    RemoveCover,
    // logs
    PickLog(usize),
    CopyLog,
    ClearLogs,
    // removal
    ConfirmRemove,
}

impl Action {
    /// True for the invisible sheet backdrop that must not steal field clicks.
    pub fn is_consume(&self) -> bool {
        matches!(self, Self::Consume)
    }
}

/// A clickable rectangle, in draw order. The app tests them back to front.
#[derive(Clone, Debug)]
pub struct Hit {
    pub rect: Rect,
    pub action: Action,
    pub enabled: bool,
}

/// A text input's text area, used to place the caret on click.
#[derive(Clone, Copy, Debug)]
pub struct FieldRect {
    pub field: Field,
    pub rect: Rect,
    pub font: Font,
    pub size: f32,
    pub wrap: bool,
    pub width: f32,
    /// Explicit line height for multiline fields (`.code`/`textarea` 1.6);
    /// `0.0` means the font's own `normal`.
    pub line_height: f32,
}

/// One frame's worth of UI state and output.
pub struct Ui<'a> {
    pub r: &'a mut Renderer,
    pub t: &'a mut TextEngine,
    pub tk: Tokens,
    pub vp: Viewport,
    pub mouse: (f32, f32),
    pub focus: Option<Field>,
    pub caret: usize,
    pub sel_anchor: Option<usize>,
    pub open_select: Option<SelectKey>,
    pub scroll: Scrolls,
    /// When true, nothing is drawn; only measurements are produced. Used to size
    /// a sheet to its content before the real pass.
    pub measure: bool,
    pub hits: Vec<Hit>,
    pub fields: Vec<FieldRect>,
    pub scroll_regions: Vec<(Rect, ScrollTarget)>,
    /// Maximum scroll for each region, recomputed every frame.
    pub max: Scrolls,
    popup: Option<(SelectKey, Rect, Vec<String>, usize)>,
}

impl<'a> Ui<'a> {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        r: &'a mut Renderer,
        t: &'a mut TextEngine,
        tk: Tokens,
        vp: Viewport,
        mouse: (f32, f32),
        focus: Option<Field>,
        caret: usize,
        sel_anchor: Option<usize>,
        open_select: Option<SelectKey>,
        scroll: Scrolls,
    ) -> Self {
        Self {
            r,
            t,
            tk,
            vp,
            mouse,
            focus,
            caret,
            sel_anchor,
            open_select,
            scroll,
            measure: false,
            hits: Vec::with_capacity(64),
            fields: Vec::with_capacity(8),
            scroll_regions: Vec::with_capacity(4),
            max: Scrolls::default(),
            popup: None,
        }
    }

    /// Reset the collected output, keeping the state. Used between the measuring
    /// pass and the drawing pass of a modal.
    pub fn reset_output(&mut self) {
        self.hits.clear();
        self.fields.clear();
        self.scroll_regions.clear();
        self.popup = None;
    }

    pub fn hit(&mut self, rect: Rect, action: Action, enabled: bool) {
        if !self.measure {
            self.hits.push(Hit {
                rect,
                action,
                enabled,
            });
        }
    }

    pub fn hovered(&self, rect: Rect) -> bool {
        rect.contains(self.mouse.0, self.mouse.1)
    }

    // ---------------------------------------------------------------- shapes

    pub fn rect(&mut self, rect: Rect, color: Rgba, radius: f32) {
        if !self.measure {
            self.r.rect(rect.array(), color, radius);
        }
    }

    pub fn rect_border(
        &mut self,
        rect: Rect,
        fill: Rgba,
        radius: f32,
        border: f32,
        border_color: Rgba,
    ) {
        if !self.measure {
            self.r
                .rect_border(rect.array(), fill, radius, border, border_color);
        }
    }

    pub fn gradient(&mut self, rect: Rect, a: Rgba, b: Rgba, radius: f32) {
        if !self.measure {
            self.r.gradient(rect.array(), a, b, radius, true);
        }
    }

    pub fn gradient_diag(&mut self, rect: Rect, a: Rgba, b: Rgba, radius: f32) {
        if !self.measure {
            self.r.gradient_diag(rect.array(), a, b, radius);
        }
    }

    /// Draw a managed cover thumbnail through the renderer's texture array.
    pub fn cover(
        &mut self,
        key: &str,
        image: &crate::core::cover::CoverImage,
        rect: Rect,
        radius: f32,
    ) {
        if !self.measure {
            self.r.cover(key, image, rect.array(), radius);
        }
    }

    /// Restrict every primitive drawn from now on to `rect`.
    pub fn push_clip(&mut self, rect: Rect) {
        if !self.measure {
            self.r.push_clip(rect.array());
        }
    }

    pub fn pop_clip(&mut self) {
        if !self.measure {
            self.r.pop_clip();
        }
    }

    // ------------------------------------------------------------------ text

    /// Single line, ellipsized to the rectangle width. Returns the line height.
    pub fn text(&mut self, text: &str, rect: Rect, font: Font, size: f32, color: Rgba) -> f32 {
        if self.measure {
            return self.t.shape(text, font, size, rect.w, false).height;
        }
        self.r.text(self.t, text, rect.array(), font, size, color)
    }

    /// Wrapped text. Returns the laid-out height.
    pub fn wrapped(&mut self, text: &str, rect: Rect, font: Font, size: f32, color: Rgba) -> f32 {
        if self.measure {
            return self.t.shape(text, font, size, rect.w, true).height;
        }
        self.r
            .text_wrapped(self.t, text, rect.array(), font, size, color)
    }

    pub fn measure_wrapped(&mut self, text: &str, font: Font, size: f32, width: f32) -> f32 {
        self.t.shape(text, font, size, width, true).height
    }

    /// `measure_wrapped` with an explicit CSS line height.
    pub fn measure_wrapped_lh(
        &mut self,
        text: &str,
        font: Font,
        size: f32,
        width: f32,
        line_height: f32,
    ) -> f32 {
        self.t
            .shape_lh(text, font, size, width, true, line_height)
            .height
    }

    // --------------------------------------------------------------- widgets

    /// Uppercase field label.
    pub fn label(&mut self, text: &str, rect: Rect) -> f32 {
        let upper = text.to_uppercase();
        self.text(&upper, rect, Font::BODY_BOLD, TEXT_XS + 0.5, self.tk.muted)
    }

    pub fn button(
        &mut self,
        rect: Rect,
        label: &str,
        kind: ButtonKind,
        action: Action,
        enabled: bool,
    ) {
        let hover = enabled && self.hovered(rect);
        let (bg, border, fg) = match kind {
            ButtonKind::Default => (
                self.tk.surface2,
                if hover { self.tk.accent } else { self.tk.line },
                self.tk.text,
            ),
            ButtonKind::Primary => (self.tk.accent, self.tk.accent, Rgba::rgb(255, 255, 255)),
            ButtonKind::Danger => (self.tk.danger, self.tk.danger, Rgba::rgb(255, 255, 255)),
            ButtonKind::DangerText => (
                self.tk.surface2,
                if hover { self.tk.danger } else { self.tk.line },
                self.tk.danger,
            ),
        };
        let fade = if enabled { 1.0 } else { 0.4 };
        if !self.measure {
            self.r
                .rect_border(rect.array(), bg.fade(fade), RADIUS, 1.0, border.fade(fade));
            let inner = rect.inset(14.0, 0.0, 14.0, 0.0);
            self.r.text(
                self.t,
                label,
                inner.array(),
                Font::BODY,
                TEXT_UI,
                fg.fade(fade),
            );
        }
        self.hit(rect, action, enabled);
    }

    pub fn button_width(&mut self, label: &str) -> f32 {
        // `.btn` is `padding:9px 14px` with a 1px border on each side.
        self.t.measure(label, Font::BODY, TEXT_UI) + 30.0
    }

    pub fn icon_button(&mut self, rect: Rect, icon: Icon, active: bool, action: Action) {
        let hover = self.hovered(rect);
        let on = hover || active;
        if !self.measure {
            self.r.rect_border(
                rect.array(),
                if on { self.tk.wash } else { self.tk.surface },
                RADIUS,
                1.0,
                if on { self.tk.accent } else { self.tk.line },
            );
            self.icon(
                icon,
                rect,
                if on { self.tk.accent } else { self.tk.text },
                self.tk.surface,
            );
        }
        self.hit(rect, action, true);
    }

    /// Circular, filled icon button used for the play/stop control on a card.
    pub fn round_icon_button(&mut self, rect: Rect, icon: Icon, kind: ButtonKind, action: Action) {
        let hover = self.hovered(rect);
        let (bg, fg) = match kind {
            ButtonKind::Default | ButtonKind::DangerText => {
                if hover {
                    (self.tk.wash, self.tk.text)
                } else {
                    (self.tk.surface2, self.tk.text)
                }
            }
            ButtonKind::Primary => (
                if hover {
                    self.tk.accent.fade(0.85)
                } else {
                    self.tk.accent
                },
                Rgba::rgb(255, 255, 255),
            ),
            ButtonKind::Danger => (
                if hover {
                    self.tk.danger.fade(0.85)
                } else {
                    self.tk.danger
                },
                Rgba::rgb(255, 255, 255),
            ),
        };
        if !self.measure {
            let radius = rect.h * 0.5;
            self.r.rect(rect.array(), bg, radius);
            self.r
                .rect_border(rect.array(), Rgba::TRANSPARENT, radius, 1.0, bg.fade(0.6));
            self.icon(icon, rect, fg, bg);
        }
        self.hit(rect, action, true);
    }

    /// Sidebar navigation row: a label, an optional icon and an optional count.
    pub fn nav_item(
        &mut self,
        rect: Rect,
        label: &str,
        count: Option<usize>,
        icon: Option<Icon>,
        on: bool,
        action: Action,
    ) {
        let hover = self.hovered(rect);
        if !self.measure {
            if on {
                self.r.rect(rect.array(), self.tk.wash, RADIUS);
            } else if hover {
                self.r.rect(rect.array(), self.tk.surface2, RADIUS);
            }
            let fg = if on { self.tk.accent } else { self.tk.text };
            if on {
                self.r.rect(
                    [rect.x, rect.y + 8.0, 3.0, rect.h - 16.0],
                    self.tk.accent,
                    1.5,
                );
            }
            let mut text_x = rect.x + 14.0;
            if let Some(icon) = icon {
                self.icon(
                    icon,
                    Rect::new(rect.x + 12.0, rect.y, 20.0, rect.h),
                    fg,
                    self.tk.surface,
                );
                text_x = rect.x + 40.0;
            }
            let count_w = if count.is_some() { 40.0 } else { 0.0 };
            self.r.text(
                self.t,
                label,
                Rect::new(
                    text_x,
                    rect.y,
                    (rect.right() - text_x - count_w - 12.0).max(1.0),
                    rect.h,
                )
                .array(),
                Font::BODY,
                TEXT_UI,
                fg,
            );
            if let Some(count) = count {
                self.r.text(
                    self.t,
                    &count.to_string(),
                    Rect::new(rect.right() - count_w - 12.0, rect.y, count_w, rect.h).array(),
                    Font::BODY,
                    TEXT_SM,
                    self.tk.muted,
                );
            }
        }
        self.hit(rect, action, true);
    }

    /// Compact sidebar row: a centred initial, used when the sidebar is
    /// collapsed to an icon rail.
    pub fn nav_compact(
        &mut self,
        rect: Rect,
        label: &str,
        count: Option<usize>,
        on: bool,
        action: Action,
    ) {
        let hover = self.hovered(rect);
        if !self.measure {
            if on {
                self.r.rect(rect.array(), self.tk.wash, RADIUS);
            } else if hover {
                self.r.rect(rect.array(), self.tk.surface2, RADIUS);
            }
            self.r.text(
                self.t,
                label,
                rect.array(),
                Font::BODY_BOLD,
                TEXT_UI,
                if on { self.tk.accent } else { self.tk.text },
            );
            if let Some(count) = count {
                self.r.text(
                    self.t,
                    &count.to_string(),
                    [rect.x, rect.bottom() - 15.0, rect.w, 13.0],
                    Font::BODY,
                    TEXT_XS,
                    self.tk.muted,
                );
            }
        }
        self.hit(rect, action, true);
    }

    /// One row of a popover menu.
    pub fn menu_item(&mut self, rect: Rect, label: &str, danger: bool, action: Action) {
        let hover = self.hovered(rect);
        if !self.measure {
            if hover {
                self.r.rect(rect.array(), self.tk.wash, RADIUS);
            }
            self.r.text(
                self.t,
                label,
                rect.inset(12.0, 0.0, 12.0, 0.0).array(),
                Font::BODY,
                TEXT_UI,
                if danger { self.tk.danger } else { self.tk.text },
            );
        }
        self.hit(rect, action, true);
    }

    pub fn chip(&mut self, rect: Rect, label: &str, on: bool, action: Action) {
        let hover = self.hovered(rect);
        if !self.measure {
            self.r.rect_border(
                rect.array(),
                if on { self.tk.wash } else { self.tk.surface2 },
                RADIUS,
                1.0,
                if on || hover {
                    self.tk.accent
                } else {
                    self.tk.line
                },
            );
            self.r.text(
                self.t,
                label,
                rect.inset(12.0, 0.0, 12.0, 0.0).array(),
                Font::BODY,
                TEXT_UI,
                if on { self.tk.accent } else { self.tk.text },
            );
        }
        self.hit(rect, action, true);
    }

    pub fn chip_width(&mut self, label: &str) -> f32 {
        // `.chip` is `padding:7px 12px` with a 1px border on each side.
        self.t.measure(label, Font::BODY, TEXT_UI) + 26.0
    }

    /// Label + description with a switch on the right.
    pub fn toggle(&mut self, rect: Rect, label: &str, desc: &str, on: bool, action: Action) {
        let hover = self.hovered(rect);
        if !self.measure {
            self.r.rect_border(
                rect.array(),
                self.tk.surface2,
                RADIUS,
                1.0,
                if hover { self.tk.accent } else { self.tk.line },
            );
            let text_rect = Rect::new(rect.x + 12.0, rect.y + 11.0, rect.w - 70.0, 20.0);
            self.r.text(
                self.t,
                label,
                text_rect.array(),
                Font::BODY,
                TEXT_UI,
                self.tk.text,
            );
            // `.sw small` is 10px with a 2px top margin.
            self.r.text(
                self.t,
                desc,
                Rect::new(text_rect.x, text_rect.bottom() + 2.0, text_rect.w, 15.0).array(),
                Font::BODY,
                TEXT_SM,
                self.tk.muted,
            );
            // Track and knob, mirroring `.sw i`.
            let track = Rect::new(rect.right() - 46.0, rect.center_y() - 9.0, 34.0, 18.0);
            self.r.rect_border(
                track.array(),
                if on { self.tk.accent } else { self.tk.surface },
                9.0,
                1.0,
                if on { self.tk.accent } else { self.tk.line },
            );
            let knob_x = if on { track.x + 18.0 } else { track.x + 2.0 };
            self.r.shape(
                Shape::Circle,
                [knob_x, track.y + 2.0, 14.0, 14.0],
                if on {
                    Rgba::rgb(255, 255, 255)
                } else {
                    self.tk.muted
                },
                0.0,
            );
        }
        self.hit(rect, action, true);
    }

    pub fn toggle_height() -> f32 {
        // `.sw`: 10px padding, a 13px label line and a 10px small line.
        SWITCH_SPAN
    }

    pub fn tab(&mut self, rect: Rect, label: &str, on: bool, action: Action) {
        if !self.measure {
            let color = if on { self.tk.accent } else { self.tk.muted };
            self.r.text(
                self.t,
                label,
                Rect::new(rect.x + 12.0, rect.y, rect.w - 24.0, rect.h).array(),
                Font::BODY,
                TEXT_UI,
                color,
            );
            if on {
                self.r.rect(
                    [rect.x, rect.bottom() - 2.0, rect.w, 2.0],
                    self.tk.accent,
                    0.0,
                );
            }
        }
        self.hit(rect, action, true);
    }

    pub fn tab_width(&mut self, label: &str) -> f32 {
        // `.tab` is `padding:10px 12px`.
        self.t.measure(label, Font::BODY, TEXT_UI) + 24.0
    }

    pub fn checkbox(
        &mut self,
        rect: Rect,
        label: &str,
        desc: &str,
        checked: bool,
        allowed: bool,
        action: Action,
    ) {
        let fade = if allowed { 1.0 } else { 0.5 };
        if !self.measure {
            self.r.rect_border(
                rect.array(),
                self.tk.surface,
                RADIUS,
                1.0,
                self.tk.line.fade(fade),
            );
            let box_rect = Rect::new(rect.x + 11.0, rect.y + 11.0, 14.0, 14.0);
            self.r.rect_border(
                box_rect.array(),
                if checked && allowed {
                    self.tk.accent
                } else {
                    self.tk.surface2
                },
                RADIUS - 2.0,
                1.0,
                if checked && allowed {
                    self.tk.accent
                } else {
                    self.tk.line
                },
            );
            if checked && allowed {
                self.icon(
                    Icon::Check,
                    box_rect.shrink(1.0),
                    Rgba::rgb(255, 255, 255),
                    self.tk.accent,
                );
            }
            let text_x = box_rect.right() + 10.0;
            let text_w = rect.right() - text_x - 11.0;
            // `.chk` is `line-height:1.5` on 11px: a 16.5px label line. The
            // checkbox itself is the first flex item, 14px tall with a 2px top
            // margin, and the label's line box starts at the content top.
            let label_h = TEXT_BODY * 1.5;
            self.r.text(
                self.t,
                label,
                Rect::new(text_x, rect.y + 9.0, text_w, label_h).array(),
                Font::BODY,
                TEXT_BODY,
                self.tk.text.fade(fade),
            );
            let y = rect.y + 9.0 + label_h;
            // `.chk small` has no declared size, so it is the browser's
            // `smaller` (5/6 em) at the inherited 1.5 line height, and it wraps
            // as one block (a `<small>` can hold a line break).
            let size = TEXT_BODY * (5.0 / 6.0);
            let desc_h = self
                .t
                .shape_lh(desc, Font::BODY, size, text_w, true, size * 1.5)
                .height;
            self.r.text_wrapped_lh(
                self.t,
                desc,
                Rect::new(text_x, y, text_w, desc_h).array(),
                Font::BODY,
                size,
                self.tk.muted.fade(fade),
                size * 1.5,
            );
        }
        self.hit(rect, action, allowed);
    }

    /// `.chk`: `padding:9px 11px`, a 14px input with a 2px top margin, a 10px
    /// gap, then an 11px/1.5 label and one `small` block for the description.
    /// `small` has no declared size, so it takes the browser's `smaller`
    /// (5/6 em) with the inherited 1.5 line height.
    pub fn checkbox_height(&mut self, desc: &str, width: f32) -> f32 {
        // 11px horizontal padding, a 14px box and the 10px gap.
        let text_w = (width - 11.0 - 14.0 - 10.0 - 11.0).max(1.0);
        let small = TEXT_BODY * (5.0 / 6.0);
        let small_h = self
            .t
            .shape_lh(desc, Font::BODY, small, text_w, true, small * 1.5)
            .height;
        (1.0 + 9.0 + TEXT_BODY * 1.5 + small_h + 9.0 + 1.0).max(50.25)
    }

    /// Badge; returns the width it used.
    pub fn badge(&mut self, x: f32, y: f32, text: &str, kind: BadgeKind) -> f32 {
        let width = self.t.measure(text, Font::BODY_BOLD, TEXT_XS) + BADGE_PAD_X * 2.0;
        let height = BADGE_H;
        let (bg, fg) = match kind {
            BadgeKind::Default => (self.tk.surface2, self.tk.muted),
            BadgeKind::Custom => (self.tk.warn.with_alpha(0.2), self.tk.warn),
            BadgeKind::Generated => (self.tk.accent.with_alpha(0.18), self.tk.accent),
        };
        if !self.measure {
            self.r.rect([x, y, width, height], bg, BADGE_RADIUS);
            self.r.text(
                self.t,
                text,
                [x + BADGE_PAD_X, y, width - BADGE_PAD_X * 2.0, height],
                Font::BODY_BOLD,
                TEXT_XS,
                fg,
            );
        }
        width
    }

    pub fn badge_width(&mut self, text: &str) -> f32 {
        self.t.measure(text, Font::BODY_BOLD, TEXT_XS) + BADGE_PAD_X * 2.0
    }

    /// Info / warning / danger note. Returns the height it used.
    pub fn note(&mut self, rect: Rect, text: &str, kind: NoteKind) -> f32 {
        let inner_w = (rect.w - 24.0).max(1.0);
        // `.note` is `font-size:11px; line-height:1.5` with a 1px border.
        let line_h = 11.0 * NOTE_LINE_HEIGHT;
        let text_h = self.measure_wrapped_lh(text, Font::BODY, 11.0, inner_w, line_h);
        let height = text_h + 20.0 + 2.0;
        let (bg, border) = match kind {
            NoteKind::Info => (self.tk.wash, self.tk.line),
            NoteKind::Warn => (self.tk.warn.with_alpha(0.1), self.tk.warn),
            NoteKind::Danger => (self.tk.danger.with_alpha(0.1), self.tk.danger),
        };
        if !self.measure {
            self.r
                .rect_border([rect.x, rect.y, rect.w, height], bg, RADIUS, 1.0, border);
            self.r.text_wrapped_lh(
                self.t,
                text,
                [rect.x + 12.0, rect.y + 11.0, inner_w, text_h],
                Font::BODY,
                11.0,
                self.tk.text,
                line_h,
            );
        }
        height
    }

    /// Preformatted, wrapped block. `scroll` shifts the text inside the block.
    /// Returns the content height (excluding padding).
    pub fn code(&mut self, rect: Rect, text: &str, scroll: f32, target: ScrollTarget) -> f32 {
        let inner_w = (rect.w - 24.0).max(1.0);
        // `.code` is `font:12px/1.6`, i.e. a 19.2px line box.
        let line_h = TEXT_CODE * CODE_LINE_HEIGHT;
        let content_h = self.measure_wrapped_lh(text, Font::BODY, TEXT_CODE, inner_w, line_h);
        if !self.measure {
            self.r
                .rect_border(rect.array(), self.tk.bg, RADIUS, 1.0, self.tk.line);
            let inner = Rect::new(
                rect.x + 12.0,
                rect.y + 12.0,
                inner_w,
                (rect.h - 24.0).max(1.0),
            );
            self.r.push_clip(inner.array());
            self.r.text_wrapped_lh(
                self.t,
                text,
                [inner.x, inner.y - scroll, inner_w, content_h],
                Font::BODY,
                TEXT_CODE,
                self.tk.text,
                line_h,
            );
            self.r.pop_clip();
            if content_h > inner.h {
                self.scroll_regions.push((rect, target));
            }
        }
        content_h
    }

    /// Standard labelled input. `multiline` wraps and keeps the text as typed.
    pub fn input(&mut self, field: Field, rect: Rect, text: &str, placeholder: &str) {
        let focused = self.focus == Some(field);
        if !self.measure {
            self.r.rect_border(
                rect.array(),
                self.tk.surface2,
                RADIUS,
                1.0,
                if focused {
                    self.tk.accent
                } else {
                    self.tk.line
                },
            );
        }
        let pad = 10.0;
        // `area` is the interactive box: clicks and focus use the whole control,
        // while the text itself is positioned inside it from real metrics.
        let area = Rect::new(
            rect.x + pad,
            rect.y,
            (rect.w - pad * 2.0).max(1.0),
            rect.h.max(1.0),
        );
        self.text_area(field, area, text, placeholder, focused);
    }

    /// Input without a frame, for the search box.
    pub fn input_inline(&mut self, field: Field, rect: Rect, text: &str, placeholder: &str) {
        let focused = self.focus == Some(field);
        self.text_area(field, rect, text, placeholder, focused);
    }

    fn text_area(
        &mut self,
        field: Field,
        area: Rect,
        text: &str,
        placeholder: &str,
        focused: bool,
    ) {
        let font = Font::BODY;
        // `textarea` is 12px; every other control inherits `body`'s 13px.
        let size = if field.multiline() {
            TEXT_CODE
        } else {
            TEXT_UI
        };
        let wrap = field.multiline();
        let line_height = if wrap { size * CODE_LINE_HEIGHT } else { 0.0 };
        // Text is placed by its line box, exactly like the prototype's box model
        // (`.in` and `textarea` both use `padding: 9px 11px`):
        //   * a single line is centred in the control as a whole, so its
        //     baseline lands at `(area.h - line_h) / 2 + ascent`;
        //   * a textarea starts at the 9px top padding and flows down.
        // `shape_lh` without an explicit line height reports the CSS
        // `line-height: normal` box (the font's ascent + descent), so the
        // position comes from real metrics rather than a guessed offset. The
        // same rect is used for the placeholder, the text, the selection and the
        // caret, so all four always agree.
        let text_rect = {
            let probe = match (text.is_empty(), placeholder.is_empty()) {
                (false, _) => text,
                (true, false) => placeholder,
                (true, true) => "X",
            };
            let line_h = self
                .t
                .shape_lh(probe, font, size, area.w, wrap, line_height)
                .height;
            text_rect(area, wrap, line_h)
        };
        self.fields.push(FieldRect {
            field,
            rect: area,
            font,
            size,
            wrap,
            width: area.w,
            line_height,
        });
        if self.measure {
            return;
        }
        self.r.push_clip(area.array());
        if focused && let Some(anchor) = self.sel_anchor {
            let (lo, hi) = (anchor.min(self.caret), anchor.max(self.caret));
            if lo != hi {
                let clusters = self
                    .t
                    .shape_lh(text, font, size, text_rect.w, wrap, line_height)
                    .clusters
                    .clone();
                for cluster in clusters {
                    if cluster.start >= lo && cluster.start < hi {
                        self.r.rect(
                            [
                                text_rect.x + cluster.x as f32,
                                text_rect.y + cluster.y as f32 - size * 0.85,
                                cluster.w.max(2) as f32,
                                size * 1.2,
                            ],
                            self.tk.accent.with_alpha(0.28),
                            0.0,
                        );
                    }
                }
            }
        }
        if text.is_empty() {
            self.r.text(
                self.t,
                placeholder,
                text_rect.array(),
                font,
                size,
                self.tk.muted.with_alpha(0.7),
            );
        } else if wrap {
            self.r.text_wrapped_lh(
                self.t,
                text,
                text_rect.array(),
                font,
                size,
                self.tk.text,
                line_height,
            );
        } else {
            self.r
                .text(self.t, text, text_rect.array(), font, size, self.tk.text);
        }
        if focused {
            let index = self.caret.min(text.len());
            let (x, baseline) =
                self.t
                    .caret_at_lh(text, font, size, text_rect.w, wrap, index, line_height);
            self.r.rect(
                [
                    text_rect.x + x,
                    text_rect.y + baseline - size * 0.85,
                    1.0,
                    size * 1.15,
                ],
                self.tk.accent,
                0.0,
            );
        }
        self.r.pop_clip();
    }

    /// Select box. The popup is drawn last, by [`Ui::end`].
    pub fn select(
        &mut self,
        key: SelectKey,
        rect: Rect,
        value: &str,
        options: &[&str],
        current: usize,
    ) {
        let hover = self.hovered(rect);
        let open = self.open_select == Some(key);
        if !self.measure {
            self.r.rect_border(
                rect.array(),
                self.tk.surface,
                RADIUS,
                1.0,
                if hover || open {
                    self.tk.accent
                } else {
                    self.tk.line
                },
            );
            self.r.text(
                self.t,
                value,
                rect.inset(11.0, 0.0, 28.0, 0.0).array(),
                Font::BODY,
                12.0,
                self.tk.text,
            );
            self.icon(
                Icon::Chevron,
                Rect::new(rect.right() - 24.0, rect.y, 18.0, rect.h),
                self.tk.muted,
                self.tk.surface,
            );
        }
        self.hit(rect, Action::SelectOpen(key), true);
        if open && !self.measure {
            self.popup = Some((
                key,
                rect,
                options.iter().map(|option| option.to_string()).collect(),
                current,
            ));
        }
    }

    /// Opacity slider. The app turns the reported rectangle into a drag.
    pub fn slider(&mut self, rect: Rect, value: f32) {
        let track = Rect::new(rect.x + 7.0, rect.center_y() - 2.0, rect.w - 14.0, 4.0);
        if !self.measure {
            self.r.rect(track.array(), self.tk.surface2, 2.0);
            let filled = (track.w * value.clamp(0.0, 1.0)).max(2.0);
            self.r
                .rect([track.x, track.y, filled, track.h], self.tk.accent, 2.0);
            let knob_x = track.x + track.w * value.clamp(0.0, 1.0) - 7.0;
            self.r.shape(
                Shape::Circle,
                [knob_x, rect.center_y() - 7.0, 14.0, 14.0],
                self.tk.accent,
                0.0,
            );
        }
        self.hit(rect, Action::Opacity(rect), true);
    }

    // ----------------------------------------------------------------- icons

    pub fn icon(&mut self, icon: Icon, box_rect: Rect, color: Rgba, bg: Rgba) {
        if self.measure {
            return;
        }
        let cx = box_rect.center_x();
        let cy = box_rect.center_y();
        match icon {
            // The prototype draws these as 16px text glyphs (`☰ + ⚙ ⌕ ✕`), so
            // each icon is sized to the glyph's ink box and offset to where the
            // glyph's ink sits inside the line box (its centre is ~1.5px below
            // the box centre, the baseline being lower than the optical middle).
            Icon::Menu => {
                for offset in [-3.0f32, 0.0, 3.0] {
                    self.r
                        .capsule(cx - 4.5, cy + offset + 0.75, 9.0, 1.5, color, 0.0);
                }
            }
            Icon::Search => {
                let ring = Rect::new(cx - 5.0, cy - 0.5, 6.0, 6.0);
                self.r
                    .rect_border(ring.array(), Rgba::TRANSPARENT, 3.0, 1.3, color);
                self.r.capsule(
                    cx - 1.6,
                    cy - 0.4,
                    2.6,
                    1.3,
                    color,
                    -std::f32::consts::FRAC_PI_4,
                );
            }
            Icon::Plus => {
                self.r.capsule(cx - 3.5, cy + 1.25, 7.0, 1.5, color, 0.0);
                self.r.capsule(cx - 0.75, cy - 2.5, 1.5, 7.0, color, 0.0);
            }
            Icon::Gear => {
                for index in 0..8 {
                    let angle = index as f32 * std::f32::consts::TAU / 8.0;
                    let x = cx + angle.cos() * 3.0 - 1.1;
                    let y = cy + 1.0 + angle.sin() * 3.0 - 0.65;
                    self.r.capsule(x, y, 2.2, 1.3, color, angle);
                }
                self.r.rect_border(
                    [cx - 2.6, cy - 1.6, 5.2, 5.2],
                    Rgba::TRANSPARENT,
                    3.0,
                    1.2,
                    color,
                );
            }
            Icon::Play => {
                self.r.shape(
                    Shape::TriangleRight,
                    [cx - 4.0, cy - 4.5, 8.0, 9.0],
                    color,
                    0.0,
                );
            }
            Icon::Stop => {
                self.r.rect([cx - 4.0, cy - 4.0, 8.0, 8.0], color, 1.5);
            }
            Icon::Close => {
                self.r.capsule(
                    cx - 4.0,
                    cy - 0.7,
                    8.0,
                    1.4,
                    color,
                    std::f32::consts::FRAC_PI_4,
                );
                self.r.capsule(
                    cx - 4.0,
                    cy - 0.7,
                    8.0,
                    1.4,
                    color,
                    -std::f32::consts::FRAC_PI_4,
                );
            }
            Icon::Grid => {
                // `▦` — a square with orthogonal crosshatch fill.
                self.r.rect_border(
                    [cx - 5.5, cy - 5.5, 11.0, 11.0],
                    Rgba::TRANSPARENT,
                    0.0,
                    1.2,
                    color,
                );
                for d in [-1.8f32, 1.8] {
                    self.r.rect([cx + d - 0.5, cy - 5.5, 1.0, 11.0], color, 0.0);
                    self.r.rect([cx - 5.5, cy + d - 0.5, 11.0, 1.0], color, 0.0);
                }
            }
            Icon::Diamond => {
                self.r.shape(
                    Shape::Rect,
                    [cx - 4.6, cy - 4.6, 9.2, 9.2],
                    color,
                    std::f32::consts::FRAC_PI_4,
                );
            }
            Icon::DiamondOpen => {
                // A 1.2px outline: the ring is the gap between two concentric
                // rotated squares, so the inner side is 2 x 1.2 smaller.
                self.r.shape(
                    Shape::Rect,
                    [cx - 4.6, cy - 4.6, 9.2, 9.2],
                    color,
                    std::f32::consts::FRAC_PI_4,
                );
                self.r.shape(
                    Shape::Rect,
                    [cx - 3.4, cy - 3.4, 6.8, 6.8],
                    bg,
                    std::f32::consts::FRAC_PI_4,
                );
            }
            Icon::Chevron => {
                self.r.capsule(
                    cx - 5.0,
                    cy - 0.8,
                    6.0,
                    1.6,
                    color,
                    std::f32::consts::FRAC_PI_4,
                );
                self.r.capsule(
                    cx + 0.0,
                    cy - 0.8,
                    6.0,
                    1.6,
                    color,
                    -std::f32::consts::FRAC_PI_4,
                );
            }
            Icon::Check => {
                // Two strokes meeting at a vertex, placed as segments so the
                // shape matches a tick rather than a V: short arm down-right,
                // long arm up-right. Rotation is clockwise on screen, where y
                // grows downward, so a plain `atan2` of the screen delta works.
                // `capsule` takes the top-left of an unrotated bar and spins it
                // about its centre, so a segment is placed by its midpoint minus
                // half the length and half the thickness.
                const T: f32 = 1.8;
                let seg = |from: (f32, f32), to: (f32, f32)| {
                    let (dx, dy) = (to.0 - from.0, to.1 - from.1);
                    let len = (dx * dx + dy * dy).sqrt();
                    let mid = (cx + (from.0 + to.0) * 0.5, cy + (from.1 + to.1) * 0.5);
                    (mid.0 - len * 0.5, mid.1 - T * 0.5, len, dy.atan2(dx))
                };
                let (ax, ay, al, ar) = seg((-3.2, 1.6), (-0.2, 4.4));
                self.r.capsule(ax, ay, al, T, color, ar);
                let (bx, by, bl, br) = seg((-0.2, 4.4), (4.9, -1.6));
                self.r.capsule(bx, by, bl, T, color, br);
            }
        }
    }

    /// Draw the pending select popup. Called once, after every other widget.
    pub fn end(&mut self) {
        if self.measure {
            return;
        }
        let Some((key, anchor, options, current)) = self.popup.take() else {
            return;
        };
        // The backdrop is registered before the items so the items stay on top.
        self.hits.push(Hit {
            rect: self.vp.full(),
            action: Action::SelectClose,
            enabled: true,
        });
        let width = anchor.w.max(180.0);
        let item_h = 34.0;
        let height = options.len() as f32 * item_h + 8.0;
        let mut y = anchor.bottom() + 8.0;
        if y + height > self.vp.height {
            y = (anchor.y - height - 8.0).max(0.0);
        }
        let popup = Rect::new(anchor.x, y, width, height);
        self.r
            .rect_border(popup.array(), self.tk.surface, RADIUS, 1.0, self.tk.line);
        for (index, option) in options.iter().enumerate() {
            let item = Rect::new(
                popup.x + 4.0,
                popup.y + 4.0 + index as f32 * item_h,
                width - 8.0,
                item_h,
            );
            if self.hovered(item) {
                self.r.rect(item.array(), self.tk.surface2, RADIUS - 1.0);
            }
            self.r.text(
                self.t,
                option,
                item.inset(12.0, 0.0, 12.0, 0.0).array(),
                Font::BODY,
                TEXT_UI,
                if index == current {
                    self.tk.accent
                } else {
                    self.tk.text
                },
            );
            self.hits.push(Hit {
                rect: item,
                action: Action::SelectPick(key, index),
                enabled: true,
            });
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ButtonKind {
    Default,
    Primary,
    Danger,
    DangerText,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BadgeKind {
    Default,
    Custom,
    Generated,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum NoteKind {
    Info,
    Warn,
    Danger,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Icon {
    Menu,
    Search,
    Plus,
    Gear,
    Play,
    Stop,
    Close,
    Grid,
    Diamond,
    DiamondOpen,
    Chevron,
    Check,
}

/// `.sw` total height: 10px padding + 20px label line + 2px margin + 15px small
/// line + 10px padding + 1px border.
pub const SWITCH_SPAN: f32 = 59.0;
/// Height of a single input box (`.in`, `select`, `.btn`): 9px padding, a whole
/// 20px line box and the 1px border on each side.
pub const INPUT_H: f32 = 40.0;
/// Height of a `.btn`; the same box as an input.
pub const BUTTON_H: f32 = 40.0;
/// Height of a `.ib` / search box. The toolbar is built from these, which is why
/// `.bar` is 74px tall.
pub const ICON_H: f32 = 42.0;

#[cfg(test)]
mod tests {
    use super::*;

    /// A 40px `.in` with 13px Space Mono: the prototype's `padding: 9px 11px`
    /// and `line-height: normal` put the baseline ~24.5px below the top edge
    /// (measured from the HTML render). The old code inset the text by 12px and
    /// then let the renderer centre the line box again, landing ~12px too low.
    #[test]
    fn single_line_baseline_matches_the_prototype() {
        let mut text = TextEngine::new();
        let shape = text.shape_lh("New Game", Font::BODY, TEXT_UI, 300.0, false, 0.0);
        let area = Rect::new(0.0, 0.0, 300.0, INPUT_H);
        let placed = text_rect(area, false, shape.height);
        let baseline = placed.y + shape.clusters[0].y as f32;
        eprintln!("line_h={} baseline={baseline}", shape.height);
        assert!(
            (shape.height - TEXT_UI * 1.481).abs() < 0.05,
            "line box {}",
            shape.height
        );
        // The line box must be the drawn rect: a taller rect would be centred
        // again by the renderer and shift the text down.
        assert!(
            (placed.h - shape.height).abs() < 0.01,
            "drawn rect is {} tall, line box is {}",
            placed.h,
            shape.height
        );
        assert!(
            (baseline - 24.5).abs() < 1.0,
            "baseline {baseline}px from the box top"
        );
    }

    /// Placeholder and typed text must share one origin, or the text jumps when
    /// the first character arrives.
    #[test]
    fn placeholder_and_entered_text_sit_at_the_same_height() {
        let mut text = TextEngine::new();
        let area = Rect::new(0.0, 0.0, 300.0, INPUT_H);
        let mut seen = Vec::new();
        for probe in ["Search games", "hollow", "New Game", "gy", "Q"] {
            let shape = text.shape_lh(probe, Font::BODY, TEXT_UI, 300.0, false, 0.0);
            let placed = text_rect(area, false, shape.height);
            seen.push(placed.y + shape.clusters[0].y as f32);
        }
        eprintln!("baselines={seen:?}");
        let first = seen[0];
        for baseline in &seen {
            assert!(
                (baseline - first).abs() < 0.01,
                "baselines differ: {seen:?}"
            );
        }
    }

    /// A textarea starts at the prototype's 9px top padding; a single-line
    /// control centres the line box instead. Both come from one function, so a
    /// field cannot drift between the two paths.
    #[test]
    fn multiline_text_starts_at_the_css_padding() {
        let area = Rect::new(4.0, 10.0, 300.0, 110.0);
        let wrapped = text_rect(area, true, 0.0);
        assert_eq!(wrapped.y, area.y + 9.0);
        assert_eq!(wrapped.x, area.x);
        let single = text_rect(area, false, 19.253);
        assert_eq!(single.y, area.y + (110.0 - 19.253) * 0.5);
    }
}
