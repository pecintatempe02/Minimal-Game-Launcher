//! Process execution: spawn, capture, and stop a whole process tree.
//!
//! Safety decisions, all deliberate:
//!
//! * **Own session / process group.** The child is started with `setsid()` via
//!   `pre_exec`, so it becomes the leader of a fresh process group whose id is
//!   the child's pid. Stop sends `SIGTERM` to `-pid` (the group), waits a grace
//!   period, then sends `SIGKILL` to the group. Wrappers such as `gamescope` or
//!   `gamemoderun` therefore die with everything they spawned; the launcher never
//!   leaves orphans behind.
//! * **Bounded capture.** Two reader threads (stdout, stderr) drain the pipes so
//!   the child never blocks on a full pipe, and keep only the first
//!   [`MAX_CAPTURE_BYTES`]. Everything past the limit is read and discarded, so
//!   the game is never slowed down and memory stays bounded.
//! * **No shell magic.** The command is handed to `/bin/sh -lc` verbatim. Nothing
//!   here parses, rewrites, or appends to it.
//! * **Honest outcomes.** A process that exits is reported as "exit status N",
//!   never as success or failure of the game.

use std::io::Read;
use std::os::unix::process::CommandExt;
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use super::command;
use super::game::Outcome;

/// Bytes kept from stdout and stderr, each.
pub const MAX_CAPTURE_BYTES: usize = 64 * 1024;
/// How long `stop` waits after `SIGTERM` before escalating to `SIGKILL`.
pub const STOP_GRACE: Duration = Duration::from_millis(1500);
/// How long the non-blocking exit check lets the capture threads reach EOF.
///
/// The pipes close with the process group, so this is normally instant. A stray
/// grandchild that inherited stdout can keep a pipe open after the game is gone;
/// joining in that case would freeze the frame, so the readers are detached.
pub const DRAIN: Duration = Duration::from_millis(100);
/// How long a `SIGKILL` gets before supervision gives up on a clean record.
///
/// Escalation is only reached by processes that ignore `SIGTERM`; even a
/// stubborn group is reaped or reported once this lapses.
pub const KILL_GRACE: Duration = Duration::from_secs(3);

/// A running game process.
pub struct ProcessHandle {
    pub pid: u32,
    child: std::process::Child,
    /// Reaped exit status, filled by [`ProcessHandle::try_wait`].
    stdout: Arc<Mutex<Capture>>,
    stderr: Arc<Mutex<Capture>>,
    stdout_thread: Option<std::thread::JoinHandle<()>>,
    stderr_thread: Option<std::thread::JoinHandle<()>>,
    pub started: Instant,
    /// Set when MGL asked for the stop, so the outcome can say so honestly.
    stop_requested: Arc<AtomicBool>,
}

/// Bounded accumulator for one stream.
pub struct Capture {
    buffer: Vec<u8>,
    truncated: bool,
    total: usize,
}

impl Capture {
    fn new() -> Self {
        Self {
            buffer: Vec::new(),
            truncated: false,
            total: 0,
        }
    }

    fn push(&mut self, chunk: &[u8]) {
        self.total += chunk.len();
        let room = MAX_CAPTURE_BYTES.saturating_sub(self.buffer.len());
        if room == 0 {
            self.truncated = true;
            return;
        }
        if chunk.len() > room {
            self.buffer.extend_from_slice(&chunk[..room]);
            self.truncated = true;
        } else {
            self.buffer.extend_from_slice(chunk);
        }
    }

    /// Captured text, lossily decoded. Never fails.
    pub fn text(&self) -> String {
        String::from_utf8_lossy(&self.buffer).into_owned()
    }

    pub fn truncated(&self) -> bool {
        self.truncated
    }

    pub fn is_empty(&self) -> bool {
        self.total == 0
    }
}

impl ProcessHandle {
    /// Spawn `command_text` through `/bin/sh -lc` in `workdir`.
    ///
    /// `env` entries are the user's optional per-game environment variables,
    /// applied on top of the inherited environment. Nothing else is injected.
    pub fn spawn(
        command_text: &str,
        workdir: &Path,
        env: &[(&str, String)],
    ) -> Result<Self, String> {
        command::validate_command(command_text)?;
        if !workdir.is_dir() {
            return Err(format!(
                "The working directory {} does not exist.",
                workdir.display()
            ));
        }

        let mut builder = std::process::Command::new(command::SHELL);
        builder
            .arg("-lc")
            .arg(command_text)
            .current_dir(workdir)
            .stdin(std::process::Stdio::null())
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped());
        for (key, value) in env {
            builder.env(key, value);
        }

        // New session => new process group led by the child. This is what makes
        // stopping the tree possible; the unsafe block is required because
        // `pre_exec` runs between fork and exec and must only call
        // async-signal-safe functions.
        unsafe {
            builder.pre_exec(|| {
                // SAFETY: `setsid` is async-signal-safe and takes no arguments.
                if libc_setsid() == -1 {
                    return Err(std::io::Error::last_os_error());
                }
                Ok(())
            });
        }

        let mut child = builder
            .spawn()
            .map_err(|e| format!("Could not start the game: {e}"))?;
        let pid = child.id();

        let stdout = Arc::new(Mutex::new(Capture::new()));
        let stderr = Arc::new(Mutex::new(Capture::new()));
        let stdout_thread = child
            .stdout
            .take()
            .map(|pipe| spawn_reader(pipe, Arc::clone(&stdout)));
        let stderr_thread = child
            .stderr
            .take()
            .map(|pipe| spawn_reader(pipe, Arc::clone(&stderr)));

        Ok(Self {
            pid,
            child,
            stdout,
            stderr,
            stdout_thread,
            stderr_thread,
            started: Instant::now(),
            stop_requested: Arc::new(AtomicBool::new(false)),
        })
    }

    /// Non-blocking check for termination.
    pub fn try_wait(&mut self) -> Option<Outcome> {
        match self.child.try_wait() {
            Ok(Some(status)) => {
                self.drain_readers();
                if self.stop_requested.load(Ordering::Relaxed) {
                    return Some(Outcome::Stopped);
                }
                Some(outcome_of(&status))
            }
            Ok(None) => None,
            Err(_) => {
                // A wait error means MGL can no longer observe the process; report
                // it without claiming anything about the game.
                self.drain_readers();
                Some(Outcome::SpawnFailed(
                    "the process could not be observed".to_string(),
                ))
            }
        }
    }

    /// Ask the whole process group to terminate (`SIGTERM`), without waiting.
    pub fn request_stop(&mut self) {
        self.stop_requested.store(true, Ordering::Relaxed);
        let _ = libc_kill_group(self.pid, SIGTERM);
    }

    /// Escalate to `SIGKILL` on the group. Used when a stop outlives its grace.
    pub fn kill_group(&self) {
        let _ = libc_kill_group(self.pid, SIGKILL);
    }

    /// Block until the process exits. For reaper threads only, never the UI.
    pub fn wait(&mut self) -> Option<Outcome> {
        let status = self.child.wait().ok()?;
        self.drain_readers();
        Some(if self.stop_requested.load(Ordering::Relaxed) {
            Outcome::Stopped
        } else {
            outcome_of(&status)
        })
    }

    /// Wait up to `timeout` for the process to exit.
    pub fn wait_timeout(&mut self, timeout: Duration) -> Option<Outcome> {
        let deadline = Instant::now() + timeout;
        loop {
            if let Some(outcome) = self.try_wait() {
                return Some(outcome);
            }
            if Instant::now() >= deadline {
                return None;
            }
            std::thread::sleep(Duration::from_millis(30));
        }
    }

    /// Terminate the whole process group: `SIGTERM`, grace period, `SIGKILL`.
    ///
    /// Blocks for up to [`STOP_GRACE`] plus three seconds, so the UI never calls
    /// this: [`Launcher::stop`] asks for the stop and lets the tick observe it.
    /// Returns the observed outcome. A process that already exited is reported
    /// with its real status instead of a fabricated "stopped".
    pub fn stop(&mut self) -> Option<Outcome> {
        if let Some(outcome) = self.try_wait() {
            return Some(outcome);
        }
        self.request_stop();
        if let Some(outcome) = self.wait_timeout(STOP_GRACE) {
            return Some(outcome);
        }
        // Still alive after the grace period: escalate. The group may include
        // children that ignored SIGTERM.
        self.kill_group();
        self.wait_timeout(Duration::from_secs(3))
    }

    /// Captured output so far (safe to call while running).
    pub fn output(&self) -> (String, String, bool) {
        let out = self.stdout.lock().map(|c| c.text()).unwrap_or_default();
        let err = self.stderr.lock().map(|c| c.text()).unwrap_or_default();
        let truncated = self.stdout.lock().map(|c| c.truncated()).unwrap_or(false)
            || self.stderr.lock().map(|c| c.truncated()).unwrap_or(false);
        (out, err, truncated)
    }

    /// Whether anything at all was ever written by the process.
    pub fn produced_no_output(&self) -> bool {
        let out_empty = self.stdout.lock().map(|c| c.is_empty()).unwrap_or(true);
        let err_empty = self.stderr.lock().map(|c| c.is_empty()).unwrap_or(true);
        out_empty && err_empty
    }

    pub fn elapsed(&self) -> Duration {
        self.started.elapsed()
    }

    /// Give the capture threads a moment to reach EOF, then let them go.
    ///
    /// A grandchild that inherited stdout can keep a pipe open after the game is
    /// gone; joining would block the caller indefinitely, so anything still
    /// running is detached. Whatever the readers already stored stays readable
    /// through the shared buffer.
    fn drain_readers(&mut self) {
        let deadline = Instant::now() + DRAIN;
        while !self.readers_finished() && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(1));
        }
        for handle in [self.stdout_thread.take(), self.stderr_thread.take()]
            .into_iter()
            .flatten()
        {
            if handle.is_finished() {
                let _ = handle.join();
            }
        }
    }

    fn readers_finished(&self) -> bool {
        [&self.stdout_thread, &self.stderr_thread]
            .into_iter()
            .all(|handle| handle.as_ref().is_none_or(|handle| handle.is_finished()))
    }
}

impl Drop for ProcessHandle {
    fn drop(&mut self) {
        // Dropping a still-running handle must not leave a game orphaned.
        if self.try_wait().is_none() {
            let _ = libc_kill_group(self.pid, SIGTERM);
            if self.wait_timeout(Duration::from_millis(300)).is_none() {
                let _ = libc_kill_group(self.pid, SIGKILL);
                let _ = self.wait_timeout(Duration::from_secs(1));
            }
        }
    }
}

fn spawn_reader(
    mut pipe: impl Read + Send + 'static,
    sink: Arc<Mutex<Capture>>,
) -> std::thread::JoinHandle<()> {
    std::thread::Builder::new()
        .name("mgl-capture".into())
        .spawn(move || {
            let mut chunk = [0u8; 8192];
            loop {
                match pipe.read(&mut chunk) {
                    Ok(0) => break,
                    Ok(n) => {
                        if let Ok(mut sink) = sink.lock() {
                            sink.push(&chunk[..n]);
                        }
                    }
                    Err(ref e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
                    Err(_) => break,
                }
            }
        })
        .expect("capture thread")
}

fn outcome_of(status: &std::process::ExitStatus) -> Outcome {
    use std::os::unix::process::ExitStatusExt;
    if let Some(signal) = status.signal() {
        Outcome::Signal(signal)
    } else {
        Outcome::Exit(status.code().unwrap_or(-1))
    }
}

// --- thin libc bindings -----------------------------------------------------
//
// Only three functions are needed, so the `libc` crate (and its build time) is
// not pulled in. Each declaration matches the C signature on Linux.

const SIGTERM: i32 = 15;
const SIGKILL: i32 = 9;

unsafe extern "C" {
    fn setsid() -> i32;
    fn kill(pid: i32, sig: i32) -> i32;
}

/// `setsid(2)`: start a new session, making the caller a process group leader.
fn libc_setsid() -> i32 {
    // SAFETY: no arguments, always safe to call.
    unsafe { setsid() }
}

/// Send `sig` to a process group (`-pid` means "the group led by pid").
fn libc_kill_group(pid: u32, sig: i32) -> i32 {
    // SAFETY: `kill` only needs an integer pid and signal number.
    unsafe { kill(-(pid as i32), sig) }
}
