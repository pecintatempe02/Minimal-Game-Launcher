//! Text shaping, layout and glyph rasterisation.
//!
//! Layering, so UI code never touches fonts:
//! * `cosmic_text` shapes a run using the bundled Space Grotesk / Space Mono
//!   subsets and lays it out at a given width;
//! * the shaped result is flattened into positioned glyphs relative to the run's
//!   origin and cached, so a frame pays only for *new* strings;
//! * glyph coverage masks are rasterised on demand and uploaded by the renderer
//!   into a small fixed number of atlas layers.
//!
//! Everything is bounded: a fixed-capacity shaping cache and a hard cap on the
//! number of laid-out lines, so pathological input degrades instead of stalling.

use std::collections::HashMap;

use cosmic_text::{
    Attrs, Buffer, CacheKey, Family, FontSystem, Metrics, Shaping, SwashCache, SwashContent, Weight,
};
use fontdb::Source;

use super::theme::{MONO, SANS};

/// The bundled subsets, 128 KB in total, embedded in the binary.
const FONTS: &[&[u8]] = &[
    include_bytes!("../../assets/fonts/SpaceGrotesk-Regular.ttf"),
    include_bytes!("../../assets/fonts/SpaceGrotesk-Medium.ttf"),
    include_bytes!("../../assets/fonts/SpaceGrotesk-Bold.ttf"),
    include_bytes!("../../assets/fonts/SpaceMono-Regular.ttf"),
    include_bytes!("../../assets/fonts/SpaceMono-Bold.ttf"),
];

/// System fonts consulted only when a string contains characters the bundled
/// subsets do not cover (for example a Japanese game title which contains CJK
/// families). Loaded at most once, on demand.
const FALLBACK_CANDIDATES: &[&str] = &[
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/usr/share/fonts/liberation/LiberationSans-Regular.ttf",
];

/// Which bundled family to use.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Font {
    /// Space Grotesk, for headings and game names.
    Sans(Weight),
    /// Space Mono, for body text and code.
    Mono(Weight),
}

impl Font {
    pub const BODY: Font = Font::Mono(Weight::NORMAL);
    pub const BODY_BOLD: Font = Font::Mono(Weight::BOLD);
    pub const HEAD: Font = Font::Sans(Weight::BOLD);
    pub const HEAD_MED: Font = Font::Sans(Weight::MEDIUM);

    fn family(self) -> Family<'static> {
        match self {
            Font::Sans(_) => Family::Name(SANS),
            Font::Mono(_) => Family::Name(MONO),
        }
    }

    fn weight(self) -> Weight {
        match self {
            Font::Sans(w) | Font::Mono(w) => w,
        }
    }
}

/// One positioned glyph, relative to the shape's top-left corner.
#[derive(Clone, Copy, Debug)]
pub struct Glyph {
    /// Rasterisation identity.
    pub key: CacheKey,
    /// Pen position of the glyph in the shape's coordinate space.
    pub x: i32,
    pub y: i32,
}

/// One shaped cluster, used to place the caret and draw selections.
#[derive(Clone, Copy, Debug)]
pub struct Cluster {
    /// Byte offset of the cluster in the shaped string.
    pub start: usize,
    pub end: usize,
    /// Pen origin of the cluster relative to the shape's top-left corner.
    pub x: i32,
    /// Baseline of the cluster's line.
    pub y: i32,
    /// Advance width of the cluster.
    pub w: i32,
}

/// A shaped run: measured extents plus glyphs relative to `(0, 0)`.
#[derive(Clone, Debug, Default)]
pub struct Shape {
    /// Width of the widest laid-out line.
    pub width: f32,
    /// Total laid-out height.
    pub height: f32,
    pub glyphs: Vec<Glyph>,
    pub clusters: Vec<Cluster>,
    /// Number of laid-out lines.
    pub lines: usize,
}

#[derive(Clone, PartialEq, Eq, Hash)]
struct ShapeKey {
    text: String,
    font: Font,
    /// Font size quantised to 0.25 px.
    size_q: u32,
    /// Layout width in whole pixels; `u32::MAX` when unbounded.
    width_q: u32,
    /// Explicit line height in quarter pixels; `0` means `line-height: normal`.
    lh_q: u32,
    wrap: bool,
}

/// A rasterised glyph, in the form the atlas uploader wants.
pub struct RasterGlyph {
    pub data: Vec<u8>,
    pub width: u32,
    pub height: u32,
    /// Offset from the pen position to the mask's left edge.
    pub left: i32,
    /// Offset from the pen baseline to the mask's top edge (positive up).
    pub top: i32,
    /// True when `data` holds RGBA instead of coverage.
    pub color: bool,
    /// True when the face belongs to the bundled Latin subsets. Only those are
    /// cached in the persistent atlas layer.
    pub bundled: bool,
}

/// Only these many shaped runs are retained: enough for every string visible on
/// a screen plus an open dialog.
const CACHE_CAPACITY: usize = 640;
/// Shaping refuses to lay out past this many lines, so a pasted log cannot stall
/// a frame.
const MAX_LINES: usize = 200;
/// Strings longer than this are not shaped at all (guards against pasting a
/// multi-megabyte file into a field).
pub const MAX_TEXT_BYTES: usize = 64 * 1024;

pub struct TextEngine {
    /// Device pixels per logical pixel. Layout stays in logical units, while
    /// glyphs are shaped and rasterised at the physical size so text is sharp on
    /// fractional-scale compositors instead of being upscaled.
    scale: f32,
    fonts: FontSystem,
    swash: SwashCache,
    cache: HashMap<ShapeKey, Shape>,
    order: Vec<ShapeKey>,
    scratch: Buffer,
    /// Whether the on-demand non-Latin fallback has been considered.
    fallback_loaded: bool,
    /// Face ids of the bundled subsets, so glyphs can be routed to the right
    /// atlas layer without guessing.
    bundled_faces: Vec<fontdb::ID>,
}

impl Default for TextEngine {
    fn default() -> Self {
        Self::new()
    }
}

impl TextEngine {
    pub fn new() -> Self {
        // Only the bundled faces are registered. `FontSystem::new()` would scan
        // every installed font (hundreds of files, tens of megabytes) for text
        // the UI can measure itself.
        let mut fonts = FontSystem::new_with_locale_and_db("en-US".to_string(), Default::default());
        for data in FONTS {
            fonts
                .db_mut()
                .load_font_source(Source::Binary(std::sync::Arc::new(*data)));
        }
        let bundled_faces: Vec<fontdb::ID> = fonts.db().faces().map(|face| face.id).collect();
        let scratch = Buffer::new(&mut fonts, Metrics::new(13.0, 17.0));
        Self {
            scale: 1.0,
            fonts,
            swash: SwashCache::new(),
            cache: HashMap::new(),
            order: Vec::new(),
            scratch,
            fallback_loaded: false,
            bundled_faces,
        }
    }

    /// Set the device scale used for rasterisation. Cheap: cached shapes built at
    /// another scale are simply dropped, which only happens on a scale change.
    pub fn set_scale(&mut self, scale: f32) {
        let scale = scale.clamp(0.5, 4.0);
        if (scale - self.scale).abs() < 0.01 {
            return;
        }
        self.scale = scale;
        self.cache.clear();
        self.order.clear();
    }

    pub fn scale(&self) -> f32 {
        self.scale
    }

    /// Shape `text` at `size` px. `wrap` lays it out inside `width`; otherwise a
    /// single unbounded line is produced.
    pub fn shape(&mut self, text: &str, font: Font, size: f32, width: f32, wrap: bool) -> &Shape {
        self.shape_lh(text, font, size, width, wrap, 0.0)
    }

    /// Shape with an explicit CSS line height (`.code`/`textarea` declare
    /// `line-height: 1.6` rather than the font's own `normal` ratio).
    pub fn shape_lh(
        &mut self,
        text: &str,
        font: Font,
        size: f32,
        width: f32,
        wrap: bool,
        line_height: f32,
    ) -> &Shape {
        let key = ShapeKey {
            text: if text.len() > MAX_TEXT_BYTES {
                text[..floor_char_boundary(text, MAX_TEXT_BYTES)].to_string()
            } else {
                text.to_string()
            },
            font,
            size_q: (size.max(1.0) * self.scale * 4.0).round() as u32,
            width_q: if wrap {
                (width.max(1.0) * self.scale).round() as u32
            } else {
                u32::MAX
            },
            lh_q: (line_height.max(0.0) * self.scale * 4.0).round() as u32,
            wrap,
        };
        if !self.cache.contains_key(&key) {
            let shape = self.build(&key);
            if self.order.len() >= CACHE_CAPACITY {
                let victim = self.order.remove(0);
                self.cache.remove(&victim);
            }
            self.order.push(key.clone());
            self.cache.insert(key.clone(), shape);
        }
        &self.cache[&key]
    }

    fn build(&mut self, key: &ShapeKey) -> Shape {
        if needs_fallback(&key.text) && !self.fallback_loaded {
            self.load_fallback();
        }
        let size = key.size_q as f32 / 4.0;
        // CSS `line-height: normal` is the font's own ascent + descent, and both
        // bundled families declare theirs in `hhea`: Space Mono is 1.481 em and
        // Space Grotesk 1.276 em. Using the real ratios keeps a line box the same
        // height the prototype's browser gives it instead of one guessed average.
        let ratio = match key.font {
            Font::Sans(_) => 1.276,
            Font::Mono(_) => 1.481,
        };
        let line_height = if key.lh_q > 0 {
            key.lh_q as f32 / 4.0
        } else {
            (size * ratio).max(size + 2.0)
        };
        self.scratch.set_metrics(Metrics::new(size, line_height));
        let bound = if key.wrap {
            Some(key.width_q as f32)
        } else {
            None
        };
        self.scratch.set_size(bound, None);
        self.scratch.set_wrap(if key.wrap {
            cosmic_text::Wrap::WordOrGlyph
        } else {
            cosmic_text::Wrap::None
        });
        let attrs = Attrs::new()
            .family(key.font.family())
            .weight(key.font.weight());
        self.scratch
            .set_text(&key.text, &attrs, Shaping::Advanced, None);
        self.scratch.shape_until_scroll(&mut self.fonts, true);

        let scale = self.scale;
        let mut shape = Shape::default();
        for run in self.scratch.layout_runs() {
            if run.line_i >= MAX_LINES {
                break;
            }
            shape.lines = run.line_i + 1;
            shape.width = shape.width.max(run.line_w / scale);
            shape.height = shape.height.max((run.line_top + run.line_height) / scale);
            shape.glyphs.reserve(run.glyphs.len());
            // Glyphs are positioned from the *baseline* (`line_y`), not the line
            // top: swash's mask offsets are measured up from the baseline, so
            // offsets anchored at the top would push every glyph above its box.
            for glyph in run.glyphs {
                let physical = glyph.physical((0.0, run.line_y), 1.0);
                shape.glyphs.push(Glyph {
                    key: physical.cache_key,
                    x: (physical.x as f32 / scale).round() as i32,
                    y: (physical.y as f32 / scale).round() as i32,
                });
                shape.clusters.push(Cluster {
                    start: glyph.start,
                    end: glyph.end,
                    x: (physical.x as f32 / scale).round() as i32,
                    y: (physical.y as f32 / scale).round() as i32,
                    w: (glyph.w / scale).round() as i32,
                });
            }
        }
        if shape.height == 0.0 {
            shape.height = line_height / scale;
        }
        shape
    }

    /// Load one system font for characters outside the bundled subgroups.
    ///
    /// This is the only place the UI touches system fonts, and it happens at
    /// most once per process, only when a string actually needs it.
    fn load_fallback(&mut self) {
        self.fallback_loaded = true;
        for candidate in FALLBACK_CANDIDATES {
            let path = std::path::Path::new(candidate);
            if !path.is_file() {
                continue;
            }
            self.fonts
                .db_mut()
                .load_font_source(Source::File(path.to_path_buf()));
            // Shaped runs created before the fallback existed may contain tofu;
            // drop them.
            self.cache.clear();
            self.order.clear();
            return;
        }
    }

    /// Measured width of a single line.
    pub fn measure(&mut self, text: &str, font: Font, size: f32) -> f32 {
        self.shape(text, font, size, 0.0, false).width
    }

    /// Truncate `text` with an ellipsis so it fits `max_width` pixels.
    ///
    /// The comparison has half a pixel of slack: widget widths are derived from
    /// a measurement (for example a badge is `measure + padding` wide) and
    /// floating-point rounding of that arithmetic can land a few ulps below the
    /// measured width, which must not truncate text that visibly fits.
    pub fn ellipsize(&mut self, text: &str, font: Font, size: f32, max_width: f32) -> String {
        if max_width <= 0.0 {
            return String::new();
        }
        if self.measure(text, font, size) <= max_width + 0.5 {
            return text.to_string();
        }
        let chars: Vec<char> = text.chars().collect();
        let mut lo = 0usize;
        let mut hi = chars.len();
        while lo < hi {
            let mid = (lo + hi).div_ceil(2);
            let mut probe: String = chars[..mid].iter().collect();
            probe.push('…');
            if self.measure(&probe, font, size) <= max_width {
                lo = mid;
            } else {
                hi = mid - 1;
            }
        }
        let mut out: String = chars[..lo].iter().collect();
        out.push('…');
        out
    }

    /// Caret position for a byte index, relative to the shape's top-left corner.
    /// The second value is the baseline of the line the caret sits on.
    pub fn caret_at(
        &mut self,
        text: &str,
        font: Font,
        size: f32,
        width: f32,
        wrap: bool,
        index: usize,
    ) -> (f32, f32) {
        self.caret_at_lh(text, font, size, width, wrap, index, 0.0)
    }

    /// [`TextEngine::caret_at`] with an explicit CSS line height.
    #[allow(clippy::too_many_arguments)]
    pub fn caret_at_lh(
        &mut self,
        text: &str,
        font: Font,
        size: f32,
        width: f32,
        wrap: bool,
        index: usize,
        line_height: f32,
    ) -> (f32, f32) {
        let shape = self.shape_lh(text, font, size, width, wrap, line_height);
        let Some(first) = shape.clusters.first().copied() else {
            return (0.0, size);
        };
        let mut found = None;
        for cluster in &shape.clusters {
            if cluster.start <= index {
                found = Some(*cluster);
            } else {
                break;
            }
        }
        match found {
            Some(cluster) => {
                let x = if index >= cluster.end {
                    (cluster.x + cluster.w) as f32
                } else {
                    cluster.x as f32
                };
                (x, cluster.y as f32)
            }
            None => (0.0, first.y as f32),
        }
    }

    /// Byte index nearest to a point, relative to the shape's top-left corner.
    #[allow(clippy::too_many_arguments)]
    pub fn index_at(
        &mut self,
        text: &str,
        font: Font,
        size: f32,
        width: f32,
        wrap: bool,
        x: f32,
        y: f32,
    ) -> usize {
        self.index_at_lh(text, font, size, width, wrap, x, y, 0.0)
    }

    /// [`TextEngine::index_at`] with an explicit CSS line height.
    #[allow(clippy::too_many_arguments)]
    pub fn index_at_lh(
        &mut self,
        text: &str,
        font: Font,
        size: f32,
        width: f32,
        wrap: bool,
        x: f32,
        y: f32,
        line_height: f32,
    ) -> usize {
        let shape = self.shape_lh(text, font, size, width, wrap, line_height);
        let mut best: Option<(f32, usize)> = None;
        for cluster in &shape.clusters {
            let cx = cluster.x as f32 + cluster.w as f32 * 0.5;
            let dx = x - cx;
            // Weight vertical distance so a click on another line wins over a
            // slightly closer glyph on the current one.
            let dy = (y - cluster.y as f32) * 2.0;
            let distance = dx * dx + dy * dy;
            let index = if x > cx { cluster.end } else { cluster.start };
            if best.map(|(d, _)| distance < d).unwrap_or(true) {
                best = Some((distance, index));
            }
        }
        match best {
            Some((_, index)) => index,
            None => 0,
        }
    }

    /// Rasterise one glyph. Returns `None` for empty glyphs such as the space.
    pub fn rasterize(&mut self, key: CacheKey) -> Option<RasterGlyph> {
        let image = self.swash.get_image(&mut self.fonts, key).as_ref()?;
        let placement = image.placement;
        if placement.width == 0 || placement.height == 0 {
            return None;
        }
        let color = matches!(image.content, SwashContent::Color);
        let bundled = self.bundled_faces.contains(&key.font_id);
        Some(RasterGlyph {
            data: image.data.clone(),
            width: placement.width,
            height: placement.height,
            left: placement.left,
            top: placement.top,
            color,
            bundled,
        })
    }
}

/// Byte index of the largest char boundary at or below `index`.
fn floor_char_boundary(text: &str, index: usize) -> usize {
    let mut idx = index.min(text.len());
    while idx > 0 && !text.is_char_boundary(idx) {
        idx -= 1;
    }
    idx
}

/// Whether `text` contains characters the bundled Latin subsets cannot render.
fn needs_fallback(text: &str) -> bool {
    text.chars().any(|c| {
        let u = c as u32;
        // ASCII, Latin-1 supplement, Latin Extended-A and the punctuation and
        // symbols the subsets were built with.
        !(u < 0x0180
            || (0x2000..=0x206F).contains(&u)
            || (0x20A0..=0x20CF).contains(&u)
            || (0x2190..=0x21FF).contains(&u)
            || (0x2500..=0x25FF).contains(&u)
            || (0x2600..=0x27BF).contains(&u))
    })
}

#[cfg(test)]
mod placement {
    use cosmic_text::{Attrs, Buffer, FontSystem, Metrics, Shaping};
    use fontdb::Source;

    /// Glyph offsets must be anchored at the baseline (`line_y`), not the line
    /// top: swash measures masks up from the baseline, so a top-anchored call
    /// pushes every glyph above its box. This asserts the inside/outside split.
    #[test]
    fn glyphs_are_placed_from_the_baseline() {
        let mut fonts = FontSystem::new_with_locale_and_db("en-US".to_string(), Default::default());
        for data in super::FONTS {
            fonts
                .db_mut()
                .load_font_source(Source::Binary(std::sync::Arc::new(*data)));
        }
        let line_height = 17.0;
        let mut buf = Buffer::new(&mut fonts, Metrics::new(12.0, line_height));
        buf.set_size(None, None);
        buf.set_wrap(cosmic_text::Wrap::None);
        buf.set_text("Ag", &Attrs::new(), Shaping::Advanced, None);
        buf.shape_until_scroll(&mut fonts, true);
        let run = buf.layout_runs().next().expect("one run");
        let glyph = run.glyphs.first().expect("one glyph");
        let baseline = glyph.physical((0.0, run.line_y), 1.0);
        assert!(
            baseline.y >= 0 && baseline.y <= line_height as i32,
            "baseline-anchored glyph at {} is outside the {}px line box",
            baseline.y,
            line_height
        );
        // The baseline sits below the line top by the half-leading plus the
        // ascent; anchoring at `line_top` would lose both.
        assert!(run.line_y > run.line_top);
    }
}
