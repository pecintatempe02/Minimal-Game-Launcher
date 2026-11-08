//! Layout primitives.
//!
//! The prototype's CSS is a hand-written grid; reproducing a general layout
//! engine would be far more code than the UI needs. Instead this module provides
//! the few deterministic shapes the screens use:
//!
//! * [`Rect`] with the small arithmetic helpers every screen needs;
//! * a [`Row`] stack for vertical flow, so components can lay out top-down and
//!   read back the height they consumed;
//! * [`grid_columns`] for the responsive card grid, the only place that mirrors
//!   CSS behaviour (`repeat(auto-fill, minmax(150px, 1fr))`).

/// Axis-aligned rectangle in logical pixels.
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct Rect {
    pub x: f32,
    pub y: f32,
    pub w: f32,
    pub h: f32,
}

impl Rect {
    pub const fn new(x: f32, y: f32, w: f32, h: f32) -> Self {
        Self { x, y, w, h }
    }

    pub const fn zero() -> Self {
        Self::new(0.0, 0.0, 0.0, 0.0)
    }

    /// The `[x, y, w, h]` form the renderer expects.
    pub fn array(&self) -> [f32; 4] {
        [self.x, self.y, self.w, self.h]
    }

    pub fn right(&self) -> f32 {
        self.x + self.w
    }

    pub fn bottom(&self) -> f32 {
        self.y + self.h
    }

    pub fn center_x(&self) -> f32 {
        self.x + self.w * 0.5
    }

    pub fn center_y(&self) -> f32 {
        self.y + self.h * 0.5
    }

    pub fn shrink(&self, amount: f32) -> Self {
        Self::new(
            self.x + amount,
            self.y + amount,
            self.w - amount * 2.0,
            self.h - amount * 2.0,
        )
    }

    pub fn inset(&self, left: f32, top: f32, right: f32, bottom: f32) -> Self {
        Self::new(
            self.x + left,
            self.y + top,
            (self.w - left - right).max(0.0),
            (self.h - top - bottom).max(0.0),
        )
    }

    pub fn contains(&self, x: f32, y: f32) -> bool {
        x >= self.x && y >= self.y && x <= self.right() && y <= self.bottom()
    }

    /// Top `height` slice, and the remainder below it.
    pub fn split_top(&self, height: f32) -> (Self, Self) {
        let height = height.min(self.h);
        (
            Self::new(self.x, self.y, self.w, height),
            Self::new(self.x, self.y + height, self.w, self.h - height),
        )
    }

    /// Bottom `height` slice, and the remainder above it.
    pub fn split_bottom(&self, height: f32) -> (Self, Self) {
        let height = height.min(self.h);
        (
            Self::new(self.x, self.bottom() - height, self.w, height),
            Self::new(self.x, self.y, self.w, self.h - height),
        )
    }

    /// Left `width` slice, and the remainder to the right.
    pub fn split_left(&self, width: f32) -> (Self, Self) {
        let width = width.min(self.w);
        (
            Self::new(self.x, self.y, width, self.h),
            Self::new(self.x + width, self.y, self.w - width, self.h),
        )
    }

    /// Right `width` slice, and the remainder to the left.
    pub fn split_right(&self, width: f32) -> (Self, Self) {
        let width = width.min(self.w);
        (
            Self::new(self.right() - width, self.y, width, self.h),
            Self::new(self.x, self.y, self.w - width, self.h),
        )
    }

    /// Intersect two rectangles; returns an empty rect when disjoint.
    pub fn intersect(&self, other: &Rect) -> Self {
        let x = self.x.max(other.x);
        let y = self.y.max(other.y);
        let right = self.right().min(other.right());
        let bottom = self.bottom().min(other.bottom());
        Self::new(x, y, (right - x).max(0.0), (bottom - y).max(0.0))
    }

    pub fn translated(&self, dx: f32, dy: f32) -> Self {
        Self::new(self.x + dx, self.y + dy, self.w, self.h)
    }
}

/// A vertical flow: hands out full-width rows and remembers the cursor.
///
/// `gap` is added between rows, matching the prototype's `display:grid;gap`.
#[derive(Clone, Debug)]
pub struct Row {
    pub rect: Rect,
    // Interior mutability so `row.next(widget(ui, row.next(h)))` compiles: the
    // inner call is evaluated first and takes the earlier rect, as intended.
    cursor: std::cell::Cell<f32>,
    gap: f32,
    first: std::cell::Cell<bool>,
}

// Layout trace: debugging a sheet's vertical rhythm is much easier when the row
// cursors can report it. A single relaxed atomic load per row keeps this free
// in the running app.
static TRACING: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);
static TRACE: std::sync::Mutex<Vec<(f32, f32, f32)>> = std::sync::Mutex::new(Vec::new());

/// Start recording every row handed out. Call once before drawing.
pub fn trace_start() {
    TRACING.store(true, std::sync::atomic::Ordering::Relaxed);
    if let Ok(mut rows) = TRACE.lock() {
        rows.clear();
    }
}

/// Drain the recorded `(y, height, gap_before)` rows.
pub fn trace_take() -> Vec<(f32, f32, f32)> {
    TRACING.store(false, std::sync::atomic::Ordering::Relaxed);
    TRACE.lock().map(|rows| rows.clone()).unwrap_or_default()
}

impl Row {
    pub fn new(rect: Rect, gap: f32) -> Self {
        Self {
            rect,
            cursor: std::cell::Cell::new(rect.y),
            gap,
            first: std::cell::Cell::new(true),
        }
    }

    /// Advance and return the next row of `height`.
    pub fn next(&self, height: f32) -> Rect {
        let mut cursor = self.cursor.get();
        let gap = if !self.first.get() { self.gap } else { 0.0 };
        cursor += gap;
        self.first.set(false);
        let row = Rect::new(self.rect.x, cursor, self.rect.w, height);
        self.cursor.set(cursor + height);
        if TRACING.load(std::sync::atomic::Ordering::Relaxed)
            && let Ok(mut rows) = TRACE.lock()
        {
            rows.push((row.y, height, gap));
        }
        row
    }

    /// Height consumed so far, including gaps.
    pub fn consumed(&self) -> f32 {
        self.cursor.get() - self.rect.y
    }

    /// Remaining height.
    pub fn remaining(&self) -> f32 {
        (self.rect.bottom() - self.cursor.get()).max(0.0)
    }

    /// Cursor position, for content that draws itself.
    pub fn cursor_y(&self) -> f32 {
        self.cursor.get()
    }

    pub fn skip(&self, height: f32) {
        self.cursor.set(self.cursor.get() + height);
    }
}

/// Column count and column width for a responsive card grid.
///
/// Mirrors `repeat(auto-fill, minmax(min_width, 1fr))`: as many columns as fit,
/// each expanded to fill the row.
pub fn grid_columns(available: f32, min_width: f32, gap: f32) -> (usize, f32) {
    if available <= 0.0 {
        return (1, 0.0);
    }
    let mut columns = ((available + gap) / (min_width + gap)).floor().max(1.0) as usize;
    // Guard against rounding pushing the last column outside the content area.
    while columns > 1 && columns as f32 * min_width + (columns as f32 - 1.0) * gap > available {
        columns -= 1;
    }
    let width = (available - (columns as f32 - 1.0) * gap) / columns as f32;
    (columns, width)
}

/// Everything a screen needs to know about the window.
#[derive(Clone, Copy, Debug)]
pub struct Viewport {
    pub width: f32,
    pub height: f32,
    /// Compositor scale factor. Geometry is in logical pixels throughout, which
    /// is what `winit` reports and what `wgpu` surfaces are configured with, so
    /// this is only used for hit-test tolerance.
    pub scale: f32,
}

impl Viewport {
    pub fn new(width: f32, height: f32, scale: f32) -> Self {
        Self {
            width,
            height,
            scale,
        }
    }

    pub fn full(&self) -> Rect {
        Rect::new(0.0, 0.0, self.width, self.height)
    }

    /// Whether the window is narrow enough for the compact layout.
    pub fn compact(&self) -> bool {
        self.width < 700.0
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn grid_fits_columns() {
        // 1000 px with 150 px minimum cards and 14 px gap.
        let (columns, width) = grid_columns(1000.0, 150.0, 14.0);
        assert_eq!(columns, 6);
        assert!((columns as f32 * width + (columns as f32 - 1.0) * 14.0 - 1000.0).abs() < 0.01);
    }

    #[test]
    fn grid_handles_narrow() {
        let (columns, width) = grid_columns(120.0, 150.0, 14.0);
        assert_eq!(columns, 1);
        assert!((width - 120.0).abs() < 0.01);
    }

    #[test]
    fn rows_accumulate_gaps() {
        let row = Row::new(Rect::new(0.0, 10.0, 100.0, 200.0), 8.0);
        let first = row.next(20.0);
        let second = row.next(20.0);
        assert_eq!(first.y, 10.0);
        assert_eq!(second.y, 38.0);
        assert_eq!(row.consumed(), 48.0);
    }

    #[test]
    fn rect_splits() {
        let rect = Rect::new(0.0, 0.0, 100.0, 50.0);
        let (left, right) = rect.split_left(30.0);
        assert_eq!(left.w, 30.0);
        assert_eq!(right.x, 30.0);
        assert_eq!(right.w, 70.0);
    }
}
