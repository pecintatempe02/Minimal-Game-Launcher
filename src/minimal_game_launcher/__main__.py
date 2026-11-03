"""Module entry point for running the launcher with ``python -m``."""

from __future__ import annotations

import sys

from minimal_game_launcher.application import MinimalGameLauncherApplication


def main(argv: list[str] | None = None) -> int:
    """Create and run the GTK application."""
    application = MinimalGameLauncherApplication()
    return application.run(argv if argv is not None else sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
