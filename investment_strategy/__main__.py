"""Entry point: `python -m investment_strategy`.

Loads config (which validates the paper/live interlock and required keys),
configures logging, and starts the orchestrator loop.
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler

from rich.logging import RichHandler

from .config import load_config
from .orchestrator import Orchestrator


def dated_log_name(default_name: str) -> str:
    """TimedRotatingFileHandler namer: turn the default rotated name
    (`logs/bot.log.2026-07-06`) into a per-day, repo-friendly label
    (`logs/Jul_06_2026.log`) so each trading day's log is a standalone file
    that can be committed and diffed for backward analysis. An unparsable
    suffix falls back to the default name rather than losing the rotation."""
    base, _, stamp = default_name.rpartition(".log.")
    try:
        day = datetime.strptime(stamp, "%Y-%m-%d")
    except ValueError:
        return default_name
    return os.path.join(os.path.dirname(base), day.strftime("%b_%d_%Y") + ".log")


def _setup_logging() -> None:
    # Console (Rich) for a human at the terminal + a rotating file for the
    # post-mortem nobody was at the terminal for (goGA GA-2.4). LOG_DIR=""
    # disables the file sink.
    handlers: list[logging.Handler] = [
        RichHandler(rich_tracebacks=True, show_path=False)
    ]
    log_dir = os.getenv("LOG_DIR", "logs")
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
        # bot.log is the LIVE file; at local midnight (or on the first start of
        # a new day — rollover time is computed from the file's mtime) the
        # previous day's content rotates to Jul_06_2026.log-style names, one
        # file per trading day, kept forever (they're committed to the repo for
        # backward analysis; backupCount-based deletion never matches the
        # custom names, by design).
        file_handler = TimedRotatingFileHandler(
            os.path.join(log_dir, "bot.log"),
            when="midnight", backupCount=0, encoding="utf-8",
        )
        file_handler.namer = dated_log_name
        file_handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s | %(message)s"
        ))
        handlers.append(file_handler)
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(name)s | %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
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
