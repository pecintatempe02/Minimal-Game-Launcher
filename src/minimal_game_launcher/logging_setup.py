"""Configure bounded local diagnostic logging.

Launch failures and Wine prefix diagnostics are useful after a dialog has been
dismissed. A rotating file handler keeps those details in the user's state
directory without allowing the log to grow without bound. User-facing dialogs
show short messages; full exception details stay in this log.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from minimal_game_launcher.paths import log_path, state_directory

_HANDLER_MARKER = "_minimal_game_launcher_handler"


def configure_logging() -> Path | None:
    """Install a rotating file logger and return its path when available."""
    root = logging.getLogger()
    for handler in root.handlers:
        if getattr(handler, _HANDLER_MARKER, False):
            return log_path()

    target = log_path()
    try:
        state_directory().mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = RotatingFileHandler(
            target,
            maxBytes=1_000_000,
            backupCount=3,
            encoding="utf-8",
        )
    except OSError:
        # Logging must never prevent the application from starting.
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        logging.getLogger(__name__).exception(
            "Could not create the diagnostic log at %s", target
        )
        return None

    setattr(handler, _HANDLER_MARKER, True)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    logging.captureWarnings(True)
    return target
