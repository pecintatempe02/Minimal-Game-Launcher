"""Launch Windows/Wine and native Linux games safely and asynchronously.

The launch manager owns process creation and per-game Wine prefix lifecycle. It
never uses a shell: commands are argument lists, user arguments are parsed with
``shlex.split``, and paths are passed as individual elements. Slow prefix
initialization runs on a worker thread, while spawned processes are monitored
from the GTK main loop with non-blocking ``Popen.poll`` calls.

A Windows game gets its own ``WINEPREFIX`` under the application data directory.
Existing prefixes are reused and never removed here. Native games are launched
directly with no Wine involvement.
"""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import gi

gi.require_version("GLib", "2.0")

from gi.repository import GLib, GObject  # noqa: E402

from minimal_game_launcher.errors import (  # noqa: E402
    LaunchError,
    PrefixInitializationError,
    WineNotFoundError,
)
from minimal_game_launcher.models import Game, Platform  # noqa: E402
from minimal_game_launcher.settings import (  # noqa: E402
    LauncherSettings,
    SettingsStore,
    WineSource,
)

LOGGER = logging.getLogger(__name__)

PREFIX_READY_MARKER = ".minigame-launcher-prefix"
_MAX_LOG_OUTPUT = 8000

PopenFactory = Callable[..., subprocess.Popen]
RunFactory = Callable[..., subprocess.CompletedProcess]
WhichFactory = Callable[[str], str | None]


def prefix_is_initialized(path: str | Path) -> bool:
    """Return whether a Wine prefix appears ready for reuse.

    ``system.reg`` is created by Wine itself. The marker is also accepted so an
    older or custom prefix that was successfully initialized can be reused
    without running ``wineboot`` again.
    """
    prefix = Path(path).expanduser()
    return (prefix / "system.reg").is_file() or (
        prefix / PREFIX_READY_MARKER
    ).is_file()


@dataclass(frozen=True, slots=True)
class LaunchSpec:
    """A fully validated command and environment waiting to be spawned."""

    command: tuple[str, ...]
    cwd: str
    env: dict[str, str]
    prefix_path: str | None
    platform: Platform


@dataclass(slots=True)
class _ActiveLaunch:
    """A running process tracked by the manager."""

    game: Game
    process: subprocess.Popen


class LaunchManager(GObject.Object):
    """Create, initialize, spawn, and monitor game processes.

    Signals run on the GTK main thread because asynchronous completions are
    delivered through ``GLib.idle_add``. The manager exposes synchronous
    preparation methods as well, which keeps command construction and prefix
    handling directly testable without spawning a real game.
    """

    __gsignals__ = {
        "launch-started": (GObject.SignalFlags.RUN_FIRST, None, (str, int)),
        "launch-finished": (GObject.SignalFlags.RUN_FIRST, None, (str, int)),
        "launch-failed": (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
    }

    def __init__(
        self,
        prefixes_root: str | Path,
        settings_store: SettingsStore,
        *,
        popen_factory: PopenFactory | None = None,
        run_factory: RunFactory | None = None,
        which: WhichFactory | None = None,
        poll_interval_ms: int = 500,
        wineboot_timeout: float = 300.0,
    ) -> None:
        super().__init__()
        self._prefixes_root = Path(prefixes_root).expanduser()
        self._settings_store = settings_store
        self._popen_factory = popen_factory or subprocess.Popen
        self._run_factory = run_factory or subprocess.run
        self._which = which or shutil.which
        self._poll_interval_ms = max(10, poll_interval_ms)
        self._wineboot_timeout = wineboot_timeout
        self._active: dict[str, _ActiveLaunch] = {}
        self._starting: set[str] = set()
        self._poll_source_id = 0
        self._shutdown = False

    def launch(self, game: Game) -> bool:
        """Start preparing and launching a game without blocking the UI.

        Returns ``False`` when a launch for this game is already in progress.
        Validation, prefix initialization, and process creation happen on a
        worker thread; their outcome is reported with manager signals.
        """
        if game.id in self._active or game.id in self._starting:
            return False

        self._starting.add(game.id)
        LOGGER.info(
            "Preparing %s launch for %s (%s)",
            game.platform.value,
            game.name,
            game.id,
        )
        thread = threading.Thread(
            target=self._launch_worker,
            args=(game,),
            name=f"game-launch-{game.id[:8]}",
            daemon=True,
        )
        thread.start()
        return True

    def _launch_worker(self, game: Game) -> None:
        """Prepare and spawn a process off the GTK main thread."""
        try:
            spec = self.prepare_launch(game)
            process = self._popen_factory(
                list(spec.command),
                cwd=spec.cwd,
                env=spec.env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                close_fds=True,
            )
        except LaunchError as error:
            LOGGER.warning("Could not launch %s: %s", game.name, error)
            GLib.idle_add(self._finish_start_failure, game.id, str(error))
            return
        except OSError as error:
            LOGGER.exception("Process creation failed for %s", game.name)
            detail = error.strerror or str(error)
            GLib.idle_add(
                self._finish_start_failure,
                game.id,
                f"The game process could not be created: {detail}",
            )
            return
        except Exception:
            LOGGER.exception("Unexpected launch error for %s", game.name)
            GLib.idle_add(
                self._finish_start_failure,
                game.id,
                "The game could not be started because of an unexpected error. "
                "See the launcher log for details.",
            )
            return

        LOGGER.info(
            "Spawned %s with pid %s: %r",
            game.name,
            process.pid,
            list(spec.command),
        )
        GLib.idle_add(self._register_process, game, process)

    def _finish_start_failure(self, game_id: str, message: str) -> bool:
        """Remove the starting state and report a readable failure."""
        self._starting.discard(game_id)
        if not self._shutdown:
            self.emit("launch-failed", game_id, message)
        return GLib.SOURCE_REMOVE

    def _register_process(self, game: Game, process: subprocess.Popen) -> bool:
        """Record a successfully spawned process on the main thread."""
        self._starting.discard(game.id)
        if self._shutdown:
            return GLib.SOURCE_REMOVE

        self._active[game.id] = _ActiveLaunch(game=game, process=process)
        self._ensure_polling()
        self.emit("launch-started", game.id, int(process.pid))
        return GLib.SOURCE_REMOVE

    def _ensure_polling(self) -> None:
        """Start the main-loop polling source when the first game is active."""
        if self._poll_source_id == 0:
            self._poll_source_id = GLib.timeout_add(
                self._poll_interval_ms, self._poll_processes
            )

    def _poll_processes(self) -> bool:
        """Check processes without waiting and emit exit status changes."""
        if self._shutdown:
            self._poll_source_id = 0
            return GLib.SOURCE_REMOVE

        finished: list[tuple[str, int]] = []
        for game_id, active in list(self._active.items()):
            returncode = active.process.poll()
            if returncode is not None:
                finished.append((game_id, int(returncode)))

        for game_id, returncode in finished:
            active = self._active.pop(game_id, None)
            if active is None:
                continue
            LOGGER.info(
                "%s exited with status %s", active.game.name, returncode
            )
            self.emit("launch-finished", game_id, returncode)

        if not self._active:
            self._poll_source_id = 0
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def status(self, game_id: str) -> str | None:
        """Return ``starting``, ``running``, or ``None`` for a game."""
        if game_id in self._active:
            return "running"
        if game_id in self._starting:
            return "starting"
        return None

    def is_running(self, game_id: str) -> bool:
        """Return whether a game process is currently running."""
        return game_id in self._active

    def is_active(self, game_id: str) -> bool:
        """Return whether a game is starting or running."""
        return game_id in self._active or game_id in self._starting

    def shutdown(self) -> None:
        """Stop monitoring; deliberately leave game processes running."""
        self._shutdown = True
        if self._poll_source_id != 0:
            GLib.source_remove(self._poll_source_id)
            self._poll_source_id = 0
        self._starting.clear()
        self._active.clear()

    def prepare_launch(self, game: Game) -> LaunchSpec:
        """Validate a game and return its complete launch specification.

        This method may run ``wineboot`` for a Windows game's first launch and
        therefore must not be called on the GTK main thread in normal use.
        """
        executable = self._validated_executable(game)
        working_directory = self._validated_working_directory(game, executable)
        arguments = self.parse_launch_arguments(game.launch_arguments)

        if game.platform is Platform.WINDOWS:
            settings = self._load_settings()
            wine_executable = self.resolve_wine_executable(settings)
            prefix = self.ensure_prefix(
                game, wine_executable, working_directory
            )
            if prefix is None:
                raise LaunchError("The Wine prefix could not be determined.")
            # Every process for a Windows game receives its own WINEPREFIX in
            # its environment; global Wine configuration is never modified.
            environment = self.build_environment(Platform.WINDOWS, prefix)
            command = (wine_executable, str(executable), *arguments)
            return LaunchSpec(
                command=command,
                cwd=str(working_directory),
                env=environment,
                prefix_path=prefix,
                platform=game.platform,
            )

        # Native games are executed directly. Removing an inherited WINEPREFIX
        # prevents an accidental Wine environment from affecting the process.
        environment = self.build_environment(Platform.LINUX, None)
        command = (str(executable), *arguments)
        return LaunchSpec(
            command=command,
            cwd=str(working_directory),
            env=environment,
            prefix_path=None,
            platform=game.platform,
        )

    def _validated_executable(self, game: Game) -> Path:
        """Resolve and validate the game executable before any process starts."""
        raw_executable = game.executable.strip()
        if not raw_executable:
            raise LaunchError("The game executable path is empty.")

        try:
            executable = Path(raw_executable).expanduser().resolve(strict=False)
        except (OSError, RuntimeError) as error:
            raise LaunchError(
                f"The game executable path could not be resolved: {raw_executable}"
            ) from error

        if not executable.exists():
            raise LaunchError(
                f"The game executable was not found: {executable}"
            )
        if not executable.is_file():
            raise LaunchError(
                f"The game executable is not a file: {executable}"
            )
        if not os.access(executable, os.R_OK):
            raise LaunchError(
                f"Permission denied while reading the game executable: "
                f"{executable}"
            )
        if game.platform is Platform.LINUX and not os.access(
            executable, os.X_OK
        ):
            raise LaunchError(
                f"The native executable is not executable: {executable}\n\n"
                "Grant execute permission (for example, chmod +x on the file) "
                "and try again. The launcher never changes file permissions."
            )
        return executable

    def _validated_working_directory(
        self, game: Game, executable: Path
    ) -> Path:
        """Resolve and validate the directory used as the process working dir."""
        raw_directory = game.working_directory.strip()
        try:
            directory = (
                Path(raw_directory).expanduser().resolve(strict=False)
                if raw_directory
                else executable.parent
            )
        except (OSError, RuntimeError) as error:
            raise LaunchError(
                f"The working directory path could not be resolved: "
                f"{raw_directory}"
            ) from error

        if not directory.exists():
            raise LaunchError(
                f"The working directory does not exist: {directory}"
            )
        if not directory.is_dir():
            raise LaunchError(
                f"The working directory is not a directory: {directory}"
            )
        if not os.access(directory, os.R_OK | os.X_OK):
            raise LaunchError(
                f"Permission denied for the working directory: {directory}"
            )
        return directory

    @staticmethod
    def parse_launch_arguments(arguments: str) -> tuple[str, ...]:
        """Parse stored arguments safely without invoking a shell."""
        try:
            return tuple(shlex.split(arguments, posix=True))
        except ValueError as error:
            raise LaunchError(
                "Launch arguments contain invalid quoting. Use quotes to group "
                "an argument that contains spaces."
            ) from error

    @staticmethod
    def build_environment(
        platform: Platform, prefix_path: str | Path | None = None
    ) -> dict[str, str]:
        """Construct a child environment without changing global Wine state."""
        environment = dict(os.environ)
        if platform is Platform.WINDOWS:
            if prefix_path is None:
                raise ValueError("A Wine prefix is required for Windows games.")
            environment["WINEPREFIX"] = str(prefix_path)
        else:
            environment.pop("WINEPREFIX", None)
        return environment

    def _load_settings(self) -> LauncherSettings:
        """Read current settings, using defaults if the file is unusable."""
        try:
            return self._settings_store.load()
        except Exception:
            LOGGER.exception("Could not load launcher settings")
            return LauncherSettings()

    def resolve_wine_executable(
        self, settings: LauncherSettings | None = None
    ) -> str:
        """Resolve system Wine or a configured custom Wine executable."""
        current = settings or self._load_settings()
        if current.wine_source is WineSource.CUSTOM:
            raw_path = (current.custom_wine_path or "").strip()
            if not raw_path:
                raise WineNotFoundError(
                    "No custom Wine executable is configured. Select one in "
                    "Settings or switch back to system Wine."
                )

            candidate = Path(raw_path).expanduser()
            if not candidate.is_absolute():
                located = self._which(raw_path)
                if not located:
                    raise WineNotFoundError(
                        f"The custom Wine executable could not be found: "
                        f"{raw_path}"
                    )
                candidate = Path(located)
            if not candidate.is_file():
                raise WineNotFoundError(
                    f"The custom Wine executable was not found: {candidate}"
                )
            if not os.access(candidate, os.X_OK):
                raise WineNotFoundError(
                    f"The custom Wine executable is not executable: {candidate}"
                )
            return str(candidate)

        located = self._which("wine") or self._which("wine64")
        if not located:
            raise WineNotFoundError(
                "Wine is not installed or was not found in PATH. Install Wine "
                "or select a custom Wine executable in Settings."
            )
        return located

    def ensure_prefix(
        self,
        game: Game,
        wine_executable: str,
        working_directory: str | Path | None = None,
    ) -> str | None:
        """Create and initialize a Windows game's prefix on first launch.

        Prefix creation is lazy: registration never calls this method. The
        prefix directory is app-owned and is not removed with the library entry.
        """
        if game.platform is not Platform.WINDOWS:
            return None

        if game.wine_prefix_path and game.wine_prefix_path.strip():
            prefix = Path(game.wine_prefix_path).expanduser()
        else:
            # Older records or manually created entries may lack a stored path.
            # Deriving it from the unique game ID still guarantees separation.
            prefix = self._prefixes_root / game.id

        try:
            prefix = prefix.resolve(strict=False)
        except (OSError, RuntimeError) as error:
            raise PrefixInitializationError(
                f"The Wine prefix path could not be resolved: {prefix}"
            ) from error

        if prefix.exists() and not prefix.is_dir():
            raise PrefixInitializationError(
                f"The Wine prefix path is not a directory: {prefix}"
            )
        try:
            prefix.parent.mkdir(parents=True, exist_ok=True)
            prefix.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as error:
            LOGGER.exception("Could not create Wine prefix %s", prefix)
            raise PrefixInitializationError(
                f"The Wine prefix directory could not be created: {prefix}"
            ) from error

        if prefix_is_initialized(prefix):
            LOGGER.info("Reusing initialized Wine prefix %s", prefix)
            return str(prefix)

        cwd = self._prefix_working_directory(game, working_directory)
        environment = self.build_environment(Platform.WINDOWS, prefix)
        command = [wine_executable, "wineboot", "-u"]
        LOGGER.info(
            "Initializing Wine prefix %s with %r", prefix, command
        )

        try:
            completed = self._run_factory(
                command,
                cwd=str(cwd),
                env=environment,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._wineboot_timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            LOGGER.error("wineboot timed out for %s", prefix)
            raise PrefixInitializationError(
                "Wine prefix initialization timed out. Check the launcher log "
                "for details."
            ) from error
        except FileNotFoundError as error:
            raise WineNotFoundError(
                f"The Wine executable was not found: {wine_executable}"
            ) from error
        except PermissionError as error:
            raise WineNotFoundError(
                f"The Wine executable could not be run: {wine_executable}"
            ) from error
        except (OSError, subprocess.SubprocessError) as error:
            LOGGER.exception("wineboot could not run for %s", prefix)
            raise PrefixInitializationError(
                "Wine prefix initialization could not be started. Check the "
                "launcher log for details."
            ) from error

        stdout = _coerce_output(completed.stdout)
        stderr = _coerce_output(completed.stderr)
        LOGGER.info(
            "wineboot finished with status %s; stdout=%s stderr=%s",
            completed.returncode,
            _truncate(stdout),
            _truncate(stderr),
        )

        if completed.returncode != 0:
            raise PrefixInitializationError(
                "Wine prefix initialization failed "
                f"(wineboot exited with code {completed.returncode}). "
                "Check the launcher log for details."
            )
        if not prefix.is_dir():
            raise PrefixInitializationError(
                f"Wine did not create the prefix directory: {prefix}"
            )

        try:
            (prefix / PREFIX_READY_MARKER).write_text(
                "initialized by Minimal Game Launcher\n", encoding="utf-8"
            )
        except OSError:
            # A missing marker only means the next launch may run wineboot -u
            # again; the successfully created prefix itself remains usable.
            LOGGER.warning("Could not write prefix marker in %s", prefix)
        return str(prefix)

    @staticmethod
    def _prefix_working_directory(
        game: Game, working_directory: str | Path | None
    ) -> Path:
        """Choose an existing directory for the short wineboot process."""
        if working_directory is not None:
            candidate = Path(working_directory).expanduser()
            if candidate.is_dir():
                return candidate
        configured = Path(game.working_directory).expanduser()
        if configured.is_dir():
            return configured
        return Path.home()


def _coerce_output(value: str | bytes | None) -> str:
    """Normalize captured error output to text for logging."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _truncate(text: str) -> str:
    """Keep diagnostic log entries bounded."""
    if len(text) <= _MAX_LOG_OUTPUT:
        return text
    return f"{text[:_MAX_LOG_OUTPUT]}… [truncated]"
