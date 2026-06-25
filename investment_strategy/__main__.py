"""Entry point: `python -m investment_strategy`.

Loads config (which validates the paper/live interlock and required keys),
configures logging, and starts the orchestrator loop.
"""
from __future__ import annotations

import logging
import os
import sys

from rich.logging import RichHandler

from .config import load_config
from .orchestrator import Orchestrator


def _setup_logging() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(name)s | %(message)s",
        datefmt="%H:%M:%S",
        handlers=[RichHandler(rich_tracebacks=True, show_path=False)],
    )


def main() -> int:
    _setup_logging()
    try:
        cfg = load_config()
    except ValueError as e:
        logging.getLogger("main").error("Config error: %s", e)
        logging.getLogger("main").error("Copy .env.example to .env and fill it in.")
        return 1

    watchlist_env = os.getenv("WATCHLIST")
    watchlist = (
        [s.strip().upper() for s in watchlist_env.split(",") if s.strip()]
        if watchlist_env else None
    )

    Orchestrator(cfg, watchlist=watchlist).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
