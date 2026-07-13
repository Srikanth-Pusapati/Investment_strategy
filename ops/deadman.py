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
~30 min by an on-disk stamp file — the Alerter's own cooldown lives in
process memory and dies with each 5-min launchd run, so it cannot throttle
across runs.

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

# The log is only guaranteed to move once per decision cycle, so the alarm
# must sit ABOVE the cycle interval or every healthy cycle's tail pages
# (2026-07-13: DECISION_INTERVAL_SECONDS=3600 vs a hardcoded 40 here paged
# every hour). Derive it from the bot's own .env cadence + grace.
STALE_GRACE_MINUTES = 15.0
PAGE_COOLDOWN_S = 1800.0


def stale_log_minutes() -> float:
    try:
        from dotenv import dotenv_values

        interval_s = float(
            dotenv_values(ROOT / ".env").get("DECISION_INTERVAL_SECONDS") or 900.0
        )
    except Exception:
        interval_s = 3600.0
    return max(40.0, interval_s / 60.0 + STALE_GRACE_MINUTES)


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
    stale_after = stale_log_minutes()
    if age is None or age > stale_after:
        shown = "missing" if age is None else f"{age:.0f} min old"
        return (
            f"Bot pid {pid} is alive but logs/bot.log is {shown} "
            f"(> {stale_after:.0f} min) — likely WEDGED. It still holds "
            f"the instance lock, so kill it (kill -TERM {pid}) and restart."
        )
    return None


def should_page(stamp: Path = ROOT / "state" / "deadman.page-stamp") -> bool:
    """Cross-run throttle: each launchd run is a fresh process, so the
    Alerter's in-memory cooldown never applies here. One page per ~30 min."""
    import time

    try:
        if time.time() - stamp.stat().st_mtime < PAGE_COOLDOWN_S:
            return False
    except OSError:
        pass
    try:
        stamp.touch()
    except OSError:
        pass
    return True


def main() -> int:
    now = dt.datetime.now(ET)
    if not market_hours(now):
        return 0
    problem = diagnose()
    stamp = f"[{now:%Y-%m-%d %H:%M:%S ET}]"
    if problem is None:
        print(f"{stamp} ok", flush=True)
        return 0
    if not should_page():
        print(f"{stamp} STALE (page throttled): {problem}", flush=True)
        return 1
    print(f"{stamp} PAGING: {problem}", flush=True)
    # The bot's own alert sink (SMTP/webhook) — configured and preflight-tested.
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
