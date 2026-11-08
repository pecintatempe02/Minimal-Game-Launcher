//! Application logging: one bounded file.
//!
//! Diagnostic logging is deliberately minimal. The launcher's *user facing*
//! record of what happened lives in the execution records (see
//! [`crate::core::launcher`]); this module only exists so a crash or a failed
//! spawn leaves a trace on disk.
//!
//! The file is rotated by keeping its newest half when it passes
//! [`MAX_LOG_BYTES`], so a long session cannot fill the disk.

use std::fs::OpenOptions;
use std::io::Write;
use std::path::PathBuf;

use crate::core::paths;

pub mod execution;

/// Rotate once the file passes this size.
pub const MAX_LOG_BYTES: u64 = 512 * 1024;

struct FileLogger {
    path: PathBuf,
}

impl log::Log for FileLogger {
    fn enabled(&self, metadata: &log::Metadata) -> bool {
        metadata.level() <= log::max_level()
    }

    fn log(&self, record: &log::Record) {
        if !self.enabled(record.metadata()) {
            return;
        }
        let line = format!(
            "{} {:<5} {}: {}\n",
            crate::core::library::now_stamp(),
            record.level(),
            record.target(),
            record.args()
        );
        let _ = ensure_capacity(&self.path);
        if let Ok(mut file) = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.path)
        {
            let _ = file.write_all(line.as_bytes());
        }
    }

    fn flush(&self) {}
}

/// Truncate the log to its newest half when it exceeds the cap.
fn ensure_capacity(path: &PathBuf) -> std::io::Result<()> {
    let Ok(meta) = std::fs::metadata(path) else {
        return Ok(());
    };
    if meta.len() <= MAX_LOG_BYTES {
        return Ok(());
    }
    let contents = std::fs::read(path)?;
    // Start on a line boundary after the halfway point.
    let keep_from = contents.len() / 2;
    let start = contents[keep_from..]
        .iter()
        .position(|b| *b == b'\n')
        .map(|offset| keep_from + offset + 1)
        .unwrap_or(keep_from);
    std::fs::write(path, &contents[start..])
}

/// Install the global logger. Safe to call more than once; later calls are
/// ignored because the logger is process-wide.
pub fn init(verbose: bool) {
    let _ = std::fs::create_dir_all(paths::data_dir());
    let path = paths::data_dir().join("mgl.log");
    let level = if verbose {
        log::LevelFilter::Debug
    } else {
        log::LevelFilter::Info
    };
    if log::set_boxed_logger(Box::new(FileLogger { path })).is_ok() {
        log::set_max_level(level);
    }
}
