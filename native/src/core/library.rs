//! Library aggregation: filtering, sorting, search and id allocation.
//!
//! Pure functions over `&[Game]`, so the UI can call them freely on every
//! redraw without a cache to invalidate.

use crate::core::game::Game;
use crate::core::paths;
pub use crate::storage::settings::SortOrder;

/// Sidebar filter, based on the card's runner label.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Default)]
pub enum Filter {
    #[default]
    All,
    Native,
    Wine,
    Custom,
}

impl Filter {
    pub const ALL: [Filter; 4] = [Filter::All, Filter::Native, Filter::Wine, Filter::Custom];

    pub fn label(self) -> &'static str {
        match self {
            Filter::All => "All Games",
            Filter::Native => "Native",
            Filter::Wine => "Wine",
            Filter::Custom => "Custom",
        }
    }

    pub fn matches(self, game: &Game) -> bool {
        match self {
            Filter::All => true,
            Filter::Native => game.runner_label() == "Native",
            Filter::Wine => game.runner_label() == "Wine",
            Filter::Custom => game.runner_label() == "Custom",
        }
    }
}

/// Indices of the games to show, in display order.
pub fn visible(games: &[Game], filter: Filter, query: &str, sort: SortOrder) -> Vec<usize> {
    let needle = query.trim().to_lowercase();
    let mut indices: Vec<usize> = games
        .iter()
        .enumerate()
        .filter(|(_, game)| filter.matches(game))
        .filter(|(_, game)| needle.is_empty() || game.name.to_lowercase().contains(&needle))
        .map(|(index, _)| index)
        .collect();

    indices.sort_by(|a, b| {
        let left = &games[*a];
        let right = &games[*b];
        let by_name = left.name.to_lowercase().cmp(&right.name.to_lowercase());
        match sort {
            SortOrder::Name => by_name.then_with(|| left.created_at.cmp(&right.created_at)),
            SortOrder::Type => left.runner_label().cmp(right.runner_label()).then(by_name),
        }
    });
    indices
}

/// Count of games per filter, for the sidebar badges.
pub fn counts(games: &[Game]) -> (usize, usize, usize, usize) {
    let native = games
        .iter()
        .filter(|g| g.runner_label() == "Native")
        .count();
    let wine = games.iter().filter(|g| g.runner_label() == "Wine").count();
    let custom = games
        .iter()
        .filter(|g| g.runner_label() == "Custom")
        .count();
    (games.len(), native, wine, custom)
}

/// Allocate an id from a display name, avoiding collisions.
pub fn allocate_id(name: &str, games: &[Game]) -> String {
    let base = paths::slugify(name);
    paths::unique_id(&base, |candidate| {
        games.iter().any(|game| game.id == candidate)
    })
}

/// Timestamp in the prototype's format: `YYYY-MM-DD HH:MM:SS`.
pub fn now_stamp() -> String {
    format_stamp(std::time::SystemTime::now())
}

/// Format a `SystemTime` as local time without pulling in a date library.
pub fn format_stamp(time: std::time::SystemTime) -> String {
    let secs = time
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let mut tm = Tm::default();
    // SAFETY: `localtime_r` writes into the provided `tm` and only reads `secs`.
    let ok = unsafe {
        let time_t = secs as i64;
        localtime_r(&time_t, &mut tm)
    };
    if !ok.is_null() {
        format!(
            "{:04}-{:02}-{:02} {:02}:{:02}:{:02}",
            tm.tm_year + 1900,
            tm.tm_mon + 1,
            tm.tm_mday,
            tm.tm_hour,
            tm.tm_min,
            tm.tm_sec
        )
    } else {
        let days = secs / 86_400;
        let rem = secs % 86_400;
        format!(
            "{:04}-{:02}-{:02} {:02}:{:02}:{:02}",
            1970 + days / 365,
            1 + (days % 365) / 31,
            1 + days % 31,
            rem / 3600,
            (rem % 3600) / 60,
            rem % 60
        )
    }
}

/// `struct tm` subset, matching glibc's layout for the fields used above.
#[repr(C)]
#[derive(Default)]
struct Tm {
    tm_sec: i32,
    tm_min: i32,
    tm_hour: i32,
    tm_mday: i32,
    tm_mon: i32,
    tm_year: i32,
    tm_wday: i32,
    tm_yday: i32,
    tm_isdst: i32,
    tm_gmtoff: i64,
    tm_zone: *const u8,
}

unsafe extern "C" {
    fn localtime_r(time: *const i64, result: *mut Tm) -> *mut Tm;
}

/// Format a duration as `H:MM:SS` or `M:SS`.
pub fn format_duration(seconds: u64) -> String {
    let hours = seconds / 3600;
    let minutes = (seconds % 3600) / 60;
    let secs = seconds % 60;
    if hours > 0 {
        format!("{hours}:{minutes:02}:{secs:02}")
    } else {
        format!("{minutes}:{secs:02}")
    }
}
