"""On-demand Winecfg and Winetricks integration.

Both tools are optional and run only after an explicit user action. Nothing is
detected at startup, no component is installed automatically, and only a prefix
that ``OwnershipService`` verifies as launcher-managed may be modified. External
prefixes are refused.

Commands are argument lists with ``shell=False``. The tools are detached from
the launcher process and their output is appended to a per-game log under the
launcher state directory; the launcher does not wait for them.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from minimal_game_launcher.errors import WineToolError
from minimal_game_launcher.models import Game, Platform
from minimal_game_launcher.services.launch_manager import prefix_is_initialized
from minimal_game_launcher.services.ownership import (
    OwnershipService,
    OwnershipStatus,
    ResourceKind,
)
from minimal_game_launcher.settings import SettingsStore, WineSource

LOGGER = logging.getLogger(__name__)

WhichFactory = Callable[[str], str | None]
PopenFactory = Callable[..., subprocess.Popen]

_VERB = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}\Z")
_MAX_VERBS = 20


@dataclass(frozen=True, slots=True)
class WineToolsAvailability:
    """Executables found for the optional Wine tools."""

    wine: str | None
    winetricks: str | None

    @property
    def winetricks_available(self) -> bool:
        """Return whether ``winetricks`` can be used."""
        return self.winetricks is not None


@dataclass(frozen=True, slots=True)
class WineToolResult:
    """A started tool command and where its output goes."""

    action: str
    command: tuple[str, ...]
    log_path: Path | None
    pid: int


class WineToolsService:
    """Start Winecfg or Winetricks for one verified managed prefix."""

    def __init__(
        self,
        prefixes_root: str | Path,
        settings_store: SettingsStore,
        *,
        ownership: OwnershipService | None = None,
        state_root: str | Path | None = None,
        which: WhichFactory | None = None,
        popen_factory: PopenFactory | None = None,
    ) -> None:
        self._prefixes_root = Path(prefixes_root).expanduser()
        self._settings_store = settings_store
        self._ownership = ownership or OwnershipService(
            self._prefixes_root,
            self._prefixes_root.parent / "games",
            self._prefixes_root.parent / "installers",
        )
        self._state_root = (
            Path(state_root).expanduser() if state_root is not None else None
        )
        self._which = which or shutil.which
        self._popen_factory = popen_factory or subprocess.Popen

    def detect(self) -> WineToolsAvailability:
        """Resolve Wine and Winetricks from the current settings and PATH."""
        try:
            wine = self._resolve_wine()
        except WineToolError:
            wine = None
        return WineToolsAvailability(
            wine=wine, winetricks=self._which("winetricks")
        )

    def _resolve_wine(self) -> str:
        try:
            settings = self._settings_store.load()
        except Exception as error:
            raise WineToolError(
                "The launcher settings could not be read."
            ) from error
        if settings.wine_source is WineSource.CUSTOM:
            raw = (settings.custom_wine_path or "").strip()
            if not raw:
                raise WineToolError(
                    "No custom Wine executable is configured."
                )
            candidate = Path(raw).expanduser()
            if not candidate.is_absolute():
                located = self._which(raw)
                if not located:
                    raise WineToolError(
                        f"The custom Wine command was not found in PATH: {raw}"
                    )
                candidate = Path(located)
            if not candidate.is_file() or not os.access(candidate, os.X_OK):
                raise WineToolError(
                    f"The custom Wine executable was not found or is not "
                    f"executable: {candidate}"
                )
            return str(candidate)
        located = self._which("wine") or self._which("wine64")
        if not located:
            raise WineToolError(
                "Wine is not installed or was not found in PATH."
            )
        return located

    def _verified_prefix(self, game: Game) -> Path:
        """Return a managed, initialized prefix or raise a readable error."""
        if game.platform is not Platform.WINDOWS:
            raise WineToolError(
                "Wine tools are available only for Windows/Wine games."
            )
        if not game.wine_prefix_path:
            raise WineToolError(
                "This game has no Wine prefix yet. Launch it once first."
            )
        kind = (
            ResourceKind.SHARED_PREFIX
            if game.wine_prefix_shared
            else ResourceKind.PREFIX
        )
        verdict = self._ownership.classify(
            kind, game.id, game.wine_prefix_path
        )
        if verdict.status is not OwnershipStatus.MANAGED:
            raise WineToolError(
                "Wine tools only modify prefixes managed by MGL. This game "
                "uses an external prefix, which is never changed."
            )
        prefix = Path(game.wine_prefix_path).expanduser()
        if not prefix_is_initialized(prefix):
            raise WineToolError(
                "The Wine prefix is not initialized yet. Launch the game once, "
                "then try again."
            )
        return prefix

    def _environment(self, wine: str, prefix: Path) -> dict[str, str]:
        environment = dict(os.environ)
        environment["WINEPREFIX"] = str(prefix)
        environment["WINE"] = wine
        return environment

    def _log_path(self, game: Game) -> Path | None:
        if self._state_root is None:
            return None
        return self._state_root / "wine-tools" / f"{game.id}.log"

    def _spawn(
        self,
        game: Game,
        action: str,
        command: list[str],
        wine: str,
        prefix: Path,
    ) -> WineToolResult:
        log_path = self._log_path(game)
        output = subprocess.DEVNULL
        handle = None
        if log_path is not None:
            try:
                log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                descriptor = os.open(
                    log_path,
                    os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                    0o600,
                )
                handle = os.fdopen(descriptor, "ab")
                try:
                    os.chmod(log_path, 0o600)
                except OSError:
                    pass
                output = handle
            except OSError:
                LOGGER.warning("Could not open Wine tool log for %s", game.name)
        try:
            process = self._popen_factory(
                command,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                env=self._environment(wine, prefix),
                shell=False,
                close_fds=True,
                start_new_session=True,
            )
        except OSError as error:
            raise WineToolError(
                f"{action} could not be started: {error.strerror or error}"
            ) from error
        finally:
            if handle is not None:
                handle.close()
        LOGGER.info("Started %s for %s (pid %s)", action, game.name, process.pid)
        return WineToolResult(
            action=action,
            command=tuple(command),
            log_path=log_path,
            pid=process.pid,
        )

    def run_winecfg(self, game: Game) -> WineToolResult:
        """Open Winecfg for the game's verified managed prefix."""
        prefix = self._verified_prefix(game)
        wine = self._resolve_wine()
        return self._spawn(
            game, "Winecfg", [wine, "winecfg"], wine, prefix
        )

    def run_winetricks(self, game: Game, verbs: str | list[str]) -> WineToolResult:
        """Run Winetricks with validated verbs for the managed prefix."""
        prefix = self._verified_prefix(game)
        raw = (
            verbs.split()
            if isinstance(verbs, str)
            else [str(verb) for verb in verbs]
        )
        cleaned: list[str] = []
        for verb in raw:
            if not _VERB.fullmatch(verb):
                raise WineToolError(
                    f"Invalid Winetricks verb: {verb!r}. Use names such as "
                    "vcrun2019 or dotnet48."
                )
            cleaned.append(verb)
        if not cleaned:
            raise WineToolError("Choose at least one Winetricks verb.")
        if len(cleaned) > _MAX_VERBS:
            raise WineToolError(
                f"Choose at most {_MAX_VERBS} Winetricks verbs at once."
            )
        winetricks = self._which("winetricks")
        if not winetricks:
            raise WineToolError(
                "Winetricks is not installed or was not found in PATH."
            )
        wine = self._resolve_wine()
        return self._spawn(
            game,
            "Winetricks",
            [winetricks, *cleaned],
            wine,
            prefix,
        )


__all__ = [
    "WineToolResult",
    "WineToolsAvailability",
    "WineToolsService",
]
