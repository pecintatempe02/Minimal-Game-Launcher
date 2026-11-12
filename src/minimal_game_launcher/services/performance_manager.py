"""Compose optional GameMode and MangoHud wrappers around a game command.

This module owns performance-wrapper detection and command/environment
construction. It deliberately knows nothing about GTK, Wine prefixes, or
process monitoring: ``LaunchManager`` supplies a validated base command and
environment, and the resulting plan is spawned there.

Wrapper order is intentional. Native games become::

    gamemoderun mangohud <game executable> <arguments>

GameMode stays outermost so it can apply CPU/GPU scheduling to the whole
MangoHud-injected process. Windows games are different: MangoHud is activated
with ``MANGOHUD=1`` in the Wine process environment instead of a wrapper, so
the command becomes::

    gamemoderun wine <game.exe> <arguments>

Detection is based on ``PATH``. Missing GameMode or MangoHud never prevents a
game from starting; the launcher falls back to the unwrapped command and returns
a warning for the UI. gamescope is different: when a game enables it, a missing
binary raises ``LaunchError`` rather than silently changing the display mode.
MangoHud's Wine overlay additionally depends on 32-bit Vulkan layer support,
which cannot be verified reliably from ``PATH``. The UI describes that limitation
rather than promising that the overlay will appear.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from typing import Callable

from minimal_game_launcher.errors import LaunchError
from minimal_game_launcher.models import Game, Platform

LOGGER = logging.getLogger(__name__)

WhichFactory = Callable[[str], str | None]


@dataclass(frozen=True, slots=True)
class PerformanceAvailability:
    """Executables found for optional performance integrations."""

    gamemoderun: str | None
    mangohud: str | None
    gamescope: str | None = None

    @property
    def gamemode_available(self) -> bool:
        """Return whether ``gamemoderun`` can be used."""
        return self.gamemoderun is not None

    @property
    def mangohud_available(self) -> bool:
        """Return whether the ``mangohud`` wrapper is installed."""
        return self.mangohud is not None

    @property
    def gamescope_available(self) -> bool:
        """Return whether the ``gamescope`` compositor is installed."""
        return self.gamescope is not None


@dataclass(frozen=True, slots=True)
class PerformancePlan:
    """A game command with optional wrappers and MangoHud environment."""

    command: tuple[str, ...]
    environment: dict[str, str]
    warnings: tuple[str, ...]


class PerformanceManager:
    """Detect and apply GameMode/MangoHud without requiring either tool.

    ``which`` is injectable so tests can model installed and missing tools
    without touching the real system. Detection runs on the launch worker
    thread, never on the GTK main thread.
    """

    def __init__(self, *, which: WhichFactory | None = None) -> None:
        self._which = which or shutil.which

    def detect(self) -> PerformanceAvailability:
        """Resolve both optional executables from ``PATH``."""
        return PerformanceAvailability(
            gamemoderun=self._which("gamemoderun"),
            mangohud=self._which("mangohud"),
            gamescope=self._which("gamescope"),
        )

    def compose(
        self,
        game: Game,
        base_command: tuple[str, ...],
        environment: dict[str, str],
        availability: PerformanceAvailability | None = None,
    ) -> PerformancePlan:
        """Wrap ``base_command`` and adjust the child environment.

        The input environment is copied. A Windows game always keeps the
        ``WINEPREFIX`` supplied by ``LaunchManager``; this method only adds or
        removes MangoHud state. Wrapper executables are prepended as separate
        argument-list elements, so paths with spaces remain safe.
        """
        current = availability
        if current is None:
            # Avoid PATH lookups when every optional integration is disabled.
            current = (
                self.detect()
                if game.gamemode_enabled
                or game.mangohud_enabled
                or game.gamescope_active
                else PerformanceAvailability(
                    gamemoderun=None, mangohud=None, gamescope=None
                )
            )
        command = list(base_command)
        child_environment = dict(environment)
        warnings: list[str] = []

        # Remove inherited MangoHud state first so the per-game toggle always
        # wins. A globally-set MANGOHUD=1 must not leak into disabled games.
        child_environment.pop("MANGOHUD", None)

        if game.mangohud_enabled:
            if game.platform is Platform.WINDOWS:
                # Wine overlays are enabled by environment variable rather
                # than a wrapper executable. WINEPREFIX was already set by
                # LaunchManager and is preserved here.
                if current.mangohud:
                    child_environment["MANGOHUD"] = "1"
                    LOGGER.info(
                        "MangoHud environment enabled for Wine game %s",
                        game.name,
                    )
                else:
                    warnings.append(
                        "MangoHud is enabled for this game, but mangohud was "
                        "not found in PATH. The Wine overlay will not be "
                        "enabled. MangoHud for Wine also requires 32-bit "
                        "Vulkan layer support."
                    )
                    LOGGER.warning(
                        "MangoHud requested for Wine game %s but unavailable",
                        game.name,
                    )
            elif current.mangohud:
                # MangoHud must sit inside GameMode so the overlay process is
                # also covered by the performance profile.
                command.insert(0, current.mangohud)
                LOGGER.info("Wrapping %s with MangoHud", game.name)
            else:
                warnings.append(
                    "MangoHud is enabled for this game, but mangohud was not "
                    "found in PATH. The overlay will not be enabled."
                )
                LOGGER.warning(
                    "MangoHud requested for %s but unavailable", game.name
                )

        if game.gamemode_enabled:
            if current.gamemoderun:
                command.insert(0, current.gamemoderun)
                LOGGER.info("Wrapping %s with GameMode", game.name)
            else:
                warnings.append(
                    "GameMode is enabled for this game, but gamemoderun was "
                    "not found in PATH. The game will launch without it."
                )
                LOGGER.warning(
                    "GameMode requested for %s but unavailable", game.name
                )

        if game.gamescope_active:
            if not current.gamescope:
                # An explicit gamescope request is a launch requirement, not an
                # optional overlay. Silently dropping it would change the game's
                # display mode without the user knowing.
                raise LaunchError(
                    "gamescope is enabled for this game, but it was not found "
                    "in PATH. Install gamescope, or disable gamescope for this "
                    "game."
                )
            arguments: list[str] = []
            if game.gamescope_resolution:
                width, height = game.gamescope_resolution.split("x", 1)
                arguments.extend(("-w", width, "-h", height))
            if game.gamescope_output_resolution:
                width, height = game.gamescope_output_resolution.split("x", 1)
                arguments.extend(("-W", width, "-H", height))
            if game.gamescope_fullscreen:
                arguments.append("-f")
            if game.gamescope_fps_limit > 0:
                arguments.extend(
                    ("--framerate-limit", str(game.gamescope_fps_limit))
                )
            if game.gamescope_vsync:
                # gamescope has no VSync switch in the tested 3.17.0 (and no
                # ``--vsync-enabled`` option in any release). Passing an
                # invented flag makes gamescope reject the whole command, so
                # surface the limitation instead of breaking the launch.
                warnings.append(
                    "This game requests VSync, but gamescope has no VSync "
                    "switch. The setting is not applied."
                )
                LOGGER.warning(
                    "gamescope VSync requested for %s but unsupported",
                    game.name,
                )
            if game.gamescope_filter:
                arguments.extend(("-F", game.gamescope_filter))
            # gamescope owns the outermost layer: it runs a private X server
            # and scales the game's buffer to the display, so the game never
            # has to change the host mode.
            command = [
                current.gamescope,
                *arguments,
                "--",
                *command,
            ]
            LOGGER.info(
                "Wrapping %s with gamescope (%r)", game.name, arguments
            )

        return PerformancePlan(
            command=tuple(command),
            environment=child_environment,
            warnings=tuple(warnings),
        )
