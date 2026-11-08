//! Design tokens: the single source of colours, radii, spacing and typography.
//!
//! Values mirror the HTML prototype's CSS custom properties so the four themes
//! stay faithful. UI code never writes literal colours; it reads `Tokens`.

/// A colour in straight sRGB with straight (non-premultiplied) alpha.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Rgba {
    pub r: f32,
    pub g: f32,
    pub b: f32,
    pub a: f32,
}

impl Rgba {
    pub const fn rgb(r: u8, g: u8, b: u8) -> Self {
        Self {
            r: r as f32 / 255.0,
            g: g as f32 / 255.0,
            b: b as f32 / 255.0,
            a: 1.0,
        }
    }

    #[allow(clippy::self_named_constructors)]
    pub const fn rgba(r: u8, g: u8, b: u8, a: f32) -> Self {
        Self {
            r: r as f32 / 255.0,
            g: g as f32 / 255.0,
            b: b as f32 / 255.0,
            a,
        }
    }

    pub const fn with_alpha(mut self, a: f32) -> Self {
        self.a = a;
        self
    }

    /// Multiply the alpha channel, used for hover/disabled states.
    pub fn fade(mut self, factor: f32) -> Self {
        self.a *= factor;
        self
    }

    /// Linear interpolation in straight sRGB, used by short transitions.
    pub fn lerp(self, other: Self, t: f32) -> Self {
        let t = t.clamp(0.0, 1.0);
        Self {
            r: self.r + (other.r - self.r) * t,
            g: self.g + (other.g - self.g) * t,
            b: self.b + (other.b - self.b) * t,
            a: self.a + (other.a - self.a) * t,
        }
    }

    /// Mix `self` towards `other` by `amount` of `other`, mirroring CSS `color-mix`.
    pub fn mix(self, other: Self, amount: f32) -> Self {
        self.lerp(other, amount)
    }

    pub fn to_premul(self) -> [f32; 4] {
        [self.r * self.a, self.g * self.a, self.b * self.a, self.a]
    }

    pub const TRANSPARENT: Self = Self {
        r: 0.0,
        g: 0.0,
        b: 0.0,
        a: 0.0,
    };
}

/// Which reference theme is active. The prototype's four palettes.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Default, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ThemeName {
    #[default]
    Dark,
    Light,
    Forest,
    Rose,
}

impl ThemeName {
    pub const ALL: [ThemeName; 4] = [Self::Dark, Self::Light, Self::Forest, Self::Rose];

    /// Label shown in the theme dropdown and stored in settings. Lower case
    /// because the prototype's option list is the raw theme id.
    pub fn label(self) -> &'static str {
        match self {
            Self::Dark => "dark",
            Self::Light => "light",
            Self::Forest => "forest",
            Self::Rose => "rose",
        }
    }

    pub fn as_str(self) -> &'static str {
        self.label()
    }

    /// Parse a stored theme name, falling back to `Dark`.
    pub fn parse(value: &str) -> Self {
        match value.trim().to_ascii_lowercase().as_str() {
            "light" => Self::Light,
            "forest" => Self::Forest,
            "rose" => Self::Rose,
            _ => Self::Dark,
        }
    }
}

/// All colours one theme needs.
#[derive(Clone, Copy, Debug)]
pub struct Tokens {
    /// Window background, drawn at the configured opacity.
    pub bg: Rgba,
    pub surface: Rgba,
    pub surface2: Rgba,
    pub text: Rgba,
    pub muted: Rgba,
    pub accent: Rgba,
    /// Hairline borders. The prototype derives it as accent @30 %.
    pub line: Rgba,
    /// Tinted wash used by hover/active surfaces: accent @9 % over surface.
    pub wash: Rgba,
    pub ok: Rgba,
    pub danger: Rgba,
    pub warn: Rgba,
    /// Card cover gradient endpoints are derived from a per-game hue.
    pub is_dark: bool,
}

/// Corner radius used across the UI (prototype: `--r: 5px`).
pub const RADIUS: f32 = 5.0;

impl Tokens {
    pub fn of(theme: ThemeName) -> Self {
        use ThemeName::*;
        let (bg, surface, surface2, text, accent, is_dark) = match theme {
            Dark => (
                Rgba::rgb(0x0d, 0x0e, 0x12),
                Rgba::rgb(0x17, 0x18, 0x1d),
                Rgba::rgb(0x23, 0x25, 0x2c),
                Rgba::rgb(0xf2, 0xf3, 0xf5),
                Rgba::rgb(0x0a, 0x84, 0xff),
                true,
            ),
            Light => (
                Rgba::rgb(0xf2, 0xf3, 0xf5),
                Rgba::rgb(0xff, 0xff, 0xff),
                Rgba::rgb(0xec, 0xee, 0xf1),
                Rgba::rgb(0x11, 0x13, 0x18),
                Rgba::rgb(0x0a, 0x84, 0xff),
                false,
            ),
            Forest => (
                Rgba::rgb(0x0c, 0x13, 0x0f),
                Rgba::rgb(0x14, 0x1d, 0x17),
                Rgba::rgb(0x1e, 0x2a, 0x22),
                Rgba::rgb(0xe8, 0xf2, 0xea),
                Rgba::rgb(0x30, 0xd1, 0x58),
                true,
            ),
            Rose => (
                Rgba::rgb(0x15, 0x0d, 0x11),
                Rgba::rgb(0x1f, 0x13, 0x18),
                Rgba::rgb(0x2c, 0x1b, 0x22),
                Rgba::rgb(0xf6, 0xe9, 0xee),
                Rgba::rgb(0xff, 0x5c, 0x8a),
                true,
            ),
        };
        let muted = text.with_alpha(0.5);
        let line = accent.with_alpha(0.3);
        // CSS `color-mix(in srgb, var(--accent) 9%, var(--surface))`: a 9 % accent
        // *in* the surface colour, i.e. lerp from surface towards accent. Mixing
        // the other way round would paint hovered widgets almost solid accent.
        let wash = surface.mix(accent, 0.09);
        Self {
            bg,
            surface,
            surface2,
            text,
            muted,
            accent,
            line,
            wash,
            ok: Rgba::rgb(0x30, 0xd1, 0x58),
            danger: Rgba::rgb(0xff, 0x45, 0x3a),
            warn: Rgba::rgb(0xff, 0xb3, 0x40),
            is_dark,
        }
    }

    /// Cover gradient for a game without cover art, mirroring the prototype's
    /// `linear-gradient(145deg, hsl(h 45% 38%), hsl(h+40 50% 14%))`.
    pub fn cover_gradient(&self, hue: f32) -> (Rgba, Rgba) {
        (
            hsl_to_rgb(hue, 0.45, 0.38),
            hsl_to_rgb(hue + 40.0, 0.50, 0.14),
        )
    }
}

/// HSL→RGB for the cover gradients only.
fn hsl_to_rgb(h: f32, s: f32, l: f32) -> Rgba {
    let h = h.rem_euclid(360.0) / 360.0;
    let q = if l < 0.5 {
        l * (1.0 + s)
    } else {
        l + s - l * s
    };
    let p = 2.0 * l - q;
    let conv = |t: f32| {
        let t = t.rem_euclid(1.0);
        if t < 1.0 / 6.0 {
            p + (q - p) * 6.0 * t
        } else if t < 0.5 {
            q
        } else if t < 2.0 / 3.0 {
            p + (q - p) * (2.0 / 3.0 - t) * 6.0
        } else {
            p
        }
    };
    Rgba {
        r: conv(h + 1.0 / 3.0),
        g: conv(h),
        b: conv(h - 1.0 / 3.0),
        a: 1.0,
    }
}

/// Font families loaded from the bundled subsets.
pub const SANS: &str = "Space Grotesk";
pub const MONO: &str = "Space Mono";

/// Text styles used by the UI.
///
/// The prototype relies on inherited CSS sizes, so these are the computed values
/// rather than the declarations: `body` is 13px, `.f>span`/`.lbl` 10px,
/// `.note`/`.chk` 11px, `.code` and `.menu small`/`.runs button` smaller still.
pub const TEXT_XS: f32 = 9.0;
pub const TEXT_SM: f32 = 10.0;
pub const TEXT_BODY: f32 = 11.0;
/// Inherited `body` size: inputs, buttons, chips, tabs, nav, card titles.
pub const TEXT_UI: f32 = 13.0;
pub const TEXT_H3: f32 = 13.0;
pub const TEXT_H2: f32 = 19.0;
/// `.f>span`, `.lbl` — uppercase 10px bold.
pub const TEXT_LABEL: f32 = 10.0;
/// `.code`, `textarea`.
pub const TEXT_CODE: f32 = 12.0;
/// `.code`/`textarea` declare `line-height: 1.6` instead of the font's `normal`.
pub const CODE_LINE_HEIGHT: f32 = 1.6;
/// `.note` declares `line-height: 1.5`.
pub const NOTE_LINE_HEIGHT: f32 = 1.5;
/// `.card .art` initial.
pub const TEXT_MONOGRAM: f32 = 54.0;
/// `.hero h1` upper clamp bound.
pub const TEXT_HERO: f32 = 38.0;

/// Box metrics taken from the stylesheet, so screens can stack them exactly.
///
/// These are the values the browser actually lays out with, measured from the
/// prototype: `select`/`.in` are `padding:9px 11px` and a 1px border, and the
/// 13px line box resolves to a whole 20px. Space Mono's exact 1.481 em would
/// give 19.25px; the browser rounds line boxes to integers, and matching that
/// integer is what keeps these boxes the same height as the reference.
pub const CONTROL_H: f32 = 40.0; // `.in`, `select`, `.btn`
pub const FIELD_GAP: f32 = 5.0; // `label.f` row gap
pub const FIELD_LABEL_H: f32 = 15.0; // one 10px uppercase label line
pub const FIELD_H: f32 = FIELD_LABEL_H + FIELD_GAP + CONTROL_H;
pub const CHIP_H: f32 = 36.0; // `.chip`: 7px padding, 1px border, 20px line
pub const SWITCH_H: f32 = 59.0; // `.sw`: 10px padding, 20px + 2 + 15px lines, 2px border
pub const TAB_H: f32 = 42.0; // `.tab`: 10px padding + 2px underline
pub const TABS_H: f32 = 43.0; // `.tabs`: tab plus its 1px rule
pub const SHEET_HEAD_H: f32 = 54.0; // `.sh`: 14px + heading + 8px
pub const SHEET_FOOT_H: f32 = 65.0; // `.sf`: 12px padding + control + border
pub const SHEET_PAD: f32 = 20.0; // `.sb` horizontal padding
pub const SHEET_BODY_TOP: f32 = 8.0; // `.sb` padding-top
pub const SHEET_BODY_BOTTOM: f32 = 14.0; // `.sb` padding-bottom
pub const ROW_GAP: f32 = 14.0; // `.sb` grid gap
pub const KV_H: f32 = 16.0; // one `dl` row: 11px Space Mono, `normal` line box
pub const KV_KEY_W: f32 = 110.0; // `dl` first column
pub const KV_GAP: f32 = 12.0; // `dl` column gap
pub const KV_ROW_GAP: f32 = 6.0; // `dl` row gap

/// `.badge`: `padding:2px 7px`, 9px bold, `border-radius:3px`. Its height is
/// one 9px line box (Space Mono's 1.481 em) plus the padding.
pub const BADGE_H: f32 = 17.3;
pub const BADGE_PAD_X: f32 = 7.0;
pub const BADGE_RADIUS: f32 = 3.0;

/// `.cb`: card body padding and gap between the title and the meta row.
pub const CARD_PAD: f32 = 10.0;
pub const CARD_GAP: f32 = 6.0;
/// `.cb h3` is `13px/1.3`.
pub const CARD_TITLE_H: f32 = 16.9;
/// `.meta` is `min-height:20px`.
pub const CARD_META_H: f32 = 20.0;
/// Whole `.cb` block: two paddings plus title, gap and meta row.
pub const CARD_BODY_H: f32 = CARD_PAD * 2.0 + CARD_TITLE_H + CARD_GAP + CARD_META_H;

/// Generate a stable cover hue from a game identifier.
pub fn hue_of(seed: &str) -> f32 {
    let mut hash: u32 = 2166136261;
    for byte in seed.as_bytes() {
        hash ^= *byte as u32;
        hash = hash.wrapping_mul(16777619);
    }
    (hash % 360) as f32
}
