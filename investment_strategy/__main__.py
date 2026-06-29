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

    Orchestrator(cfg, watchlist=resolve_watchlist(os.getenv("WATCHLIST"))).run()
    return 0


def resolve_watchlist(env_value: str | None) -> list[str] | None:
    """Map the WATCHLIST env var to a watchlist.

    None  (unset)                 -> None  => orchestrator uses its default list.
    ""    or "NONE" (explicit)    -> []    => start flat; trade only screener finds.
    "AAPL,msft"                   -> ["AAPL", "MSFT"].
    """
    if env_value is None:
        return None
    cleaned = [s.strip().upper() for s in env_value.split(",") if s.strip()]
    return [] if cleaned in ([], ["NONE"]) else cleaned


if __name__ == "__main__":
    sys.exit(main())
