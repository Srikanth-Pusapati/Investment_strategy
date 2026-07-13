#!/usr/bin/env python
"""Dead-man watchdog — pages when the bot goes silent (goGA GA-2.2).

Runs OUTSIDE the bot (its own launchd job, every 5 min) so it survives the
failure it exists to detect. During market hours it checks:

  1. PROCESS: the pid in state/bot.lock is alive and is the bot.
  2. FRESHNESS: logs/bot.log was written recently — a live pid with a stale
     log is a wedged bot (deadlocked thread, hung API call), which is worse
     than a dead one because the lock still blocks a manual restart.

On failure it pages through the SAME SMTP/webhook sink the bot's CRITICAL
alerts use (proven by preflight's test alert), throttled to one page per
condition per ~30 min by the Alerter's own cooldown.

SCOPE: this covers "bot died / wedged while the laptop is up". If the whole
laptop sleeps or dies, this script dies with it — that failure domain needs
the EXTERNAL half: a free healthchecks.io check whose ping URL goes in
HEARTBEAT_URL (.env); the bot pings it each healthy cycle and the SERVICE
pages on silence. Both halves together = GA-2.2 done.

Install:  cp ops/launchd/com.investment-strategy.deadman.plist ~/Library/LaunchAgents/
          launchctl load ~/Library/LaunchAgents/com.investment-strategy.deadman.plist
"""
from __future__ import annotations

import datetime as dt
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
ET = ZoneInfo("America/New_York")

# The bot logs at least each decision cycle (30-60 min) and every watchdog
# CRITICAL; a log silent for 40+ min during market hours means wedged/dead.
STALE_LOG_MINUTES = 40.0


def market_hours(now: dt.datetime | None = None) -> bool:
    """Weekday 09:25-16:05 ET — slightly wider than the session so a bot that
    died overnight pages BEFORE the open, not 40 minutes into it."""
    now = now or dt.datetime.now(ET)
    if now.weekday() >= 5:
        return False
    t = now.hour * 60 + now.minute
    return (9 * 60 + 25) <= t <= (16 * 60 + 5)


def bot_pid(lock_file: Path = ROOT / "state" / "bot.lock") -> int | None:
    try:
        pid = int(lock_file.read_text().strip() or 0)
        return pid if pid > 0 else None
    except (OSError, ValueError):
        return None


def bot_alive(pid: int | None) -> bool:
    """True when `pid` is running AND is the bot (a recycled pid must not
    count as alive)."""
    if not pid:
        return False
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "investment_strategy" in out


def log_age_minutes(log_file: Path = ROOT / "logs" / "bot.log",
                    now: dt.datetime | None = None) -> float | None:
    """Minutes since the bot last wrote its log; None when the log is missing
    (treated as stale by the caller — a missing log is not a healthy bot)."""
    try:
        mtime = dt.datetime.fromtimestamp(log_file.stat().st_mtime, tz=dt.timezone.utc)
    except OSError:
        return None
    now = now or dt.datetime.now(dt.timezone.utc)
    return (now - mtime).total_seconds() / 60.0


def diagnose(now: dt.datetime | None = None) -> str | None:
    """None when healthy; otherwise a one-line description of what's wrong."""
    pid = bot_pid()
    if not bot_alive(pid):
        return (
            f"Bot process is NOT RUNNING (lock pid {pid or 'missing'}). "
            f"Restart: cd {ROOT} && nohup .venv/bin/python -m investment_strategy "
            ">> logs/stdout.log 2>&1 &"
        )
    age = log_age_minutes(now=now)
    if age is None or age > STALE_LOG_MINUTES:
        shown = "missing" if age is None else f"{age:.0f} min old"
        return (
            f"Bot pid {pid} is alive but logs/bot.log is {shown} "
            f"(> {STALE_LOG_MINUTES:.0f} min) — likely WEDGED. It still holds "
            f"the instance lock, so kill it (kill -TERM {pid}) and restart."
        )
    return None


def main() -> int:
    now = dt.datetime.now(ET)
    if not market_hours(now):
        return 0
    problem = diagnose()
    stamp = f"[{now:%Y-%m-%d %H:%M:%S ET}]"
    if problem is None:
        print(f"{stamp} ok", flush=True)
        return 0
    print(f"{stamp} PAGING: {problem}", flush=True)
    # The bot's own alert sink (SMTP/webhook) — configured, preflight-tested,
    # and throttled per key so a dead bot pages ~2x/hour, not every 5 min.
    sys.path.insert(0, str(ROOT))
    from investment_strategy.config import load_config
    from investment_strategy.notify import Alerter

    alerter = Alerter(load_config().alerts)
    alerter.critical(
        "deadman:bot-silent",
        "DEAD-MAN: trading bot silent during market hours",
        f"{problem}\n\nChecked at {now:%Y-%m-%d %H:%M ET} by ops/deadman.py.",
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
