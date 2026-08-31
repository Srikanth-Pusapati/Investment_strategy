#!/usr/bin/env python
"""Dead-man watchdog — pages when the bot goes silent (goGA GA-2.2).

Runs OUTSIDE the bot (its own launchd job, every 5 min) so it survives the
failure it exists to detect. During market hours it checks:

  1. PROCESS: the pid in state/bot.lock is alive and is the bot.
  2. FRESHNESS: logs/bot.log was written recently — a live pid with a stale
     log is a wedged bot (deadlocked thread, hung API call), which is worse
     than a dead one because the lock still blocks a manual restart.

On failure it SELF-HEALS first (added 2026-08-12, for unattended stretches):
a wedged bot is killed to release the instance lock, then the launchd-
supervised control panel is asked to restart the bot (the same POST
/api/restart path used interactively — panel spawn keeps the caffeinate
attach). Restarts are throttled to one per ~10 min by an on-disk stamp so a
crash-looping bot can't be restart-spammed. It THEN pages through the SAME
SMTP/webhook sink the bot's CRITICAL alerts use (proven by preflight's test
alert), with the auto-restart outcome in the page body, throttled to one
page per ~30 min by its own stamp file — the Alerter's cooldown lives in
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
RESTART_COOLDOWN_S = 600.0
# The control panel (its own KeepAlive launchd job) owns the spawn path —
# restarting through it keeps the flock handling + caffeinate attach identical
# to an interactive restart.
PANEL_RESTART_URL = "http://127.0.0.1:8787/api/restart"
# The bot writes state/last_tick.stamp on every MAIN-loop tick (~30s) and
# through each decision cycle, so a wedged decision thread goes stale here in
# minutes even while the 24/7 watchdog keeps logs/bot.log warm. Tighter than the
# log-mtime threshold; only consulted when the stamp file exists.
TICK_STAMP_STALE_MINUTES = 5.0
# scripts/flatten_and_restart.py touches this marker when it BEGINS the flatten
# (bot intentionally stopped, orders cancelling, positions closing) and removes
# it after the relaunch. While the marker is fresh the dead-man must stand down
# completely — an auto-restart mid-flatten would resurrect the bot to trade
# AGAINST the close-all. A marker older than FLATTEN_HOLD_STALE_S is debris
# from a crashed flatten (its try/finally never ran) and is ignored, so a
# failed flatten can never mute the dead-man forever.
FLATTEN_HOLD_FILE = ROOT / "state" / "flatten.hold"
FLATTEN_HOLD_STALE_S = 7200.0


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


def tick_stamp_age_minutes(
    stamp_file: Path = ROOT / "state" / "last_tick.stamp",
    now: dt.datetime | None = None,
) -> float | None:
    """Minutes since the bot's MAIN loop last wrote its tick stamp, or None if
    the stamp file is missing/unreadable (older bot, or never started — the
    caller falls back to the log-mtime check rather than false-paging)."""
    try:
        written = float(stamp_file.read_text().strip())
    except (OSError, ValueError):
        return None
    now = now or dt.datetime.now(dt.timezone.utc)
    return (now.timestamp() - written) / 60.0


def flatten_hold_active(marker: Path = FLATTEN_HOLD_FILE,
                        now_ts: float | None = None) -> bool:
    """True while a flatten-and-restart is in progress: the marker exists and
    is younger than FLATTEN_HOLD_STALE_S. A stale marker (crashed flatten that
    never reached its finally-cleanup) is treated as absent."""
    import time

    try:
        mtime = marker.stat().st_mtime
    except OSError:
        return False
    age = (now_ts if now_ts is not None else time.time()) - mtime
    return age < FLATTEN_HOLD_STALE_S


def diagnose(now: dt.datetime | None = None) -> str | None:
    """None when healthy; otherwise a one-line description of what's wrong."""
    pid = bot_pid()
    if not bot_alive(pid):
        return (
            f"Bot process is NOT RUNNING (lock pid {pid or 'missing'}). "
            f"Restart: cd {ROOT} && nohup .venv/bin/python -m investment_strategy "
            ">> logs/stdout.log 2>&1 &"
        )
    # Main-loop tick stamp first: it detects a WEDGED decision thread that the
    # 24/7 watchdog would otherwise mask by keeping the log warm. Only trusted
    # when present; a missing stamp falls through to the log-mtime check.
    tick_age = tick_stamp_age_minutes(now=now)
    if tick_age is not None and tick_age > TICK_STAMP_STALE_MINUTES:
        return (
            f"Bot pid {pid} is alive but its main-loop tick stamp is "
            f"{tick_age:.0f} min old (> {TICK_STAMP_STALE_MINUTES:.0f} min) — the "
            f"decision loop is WEDGED (the watchdog may still be logging). It "
            f"holds the instance lock, so kill it (kill -TERM {pid}) and restart."
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


def attempt_restart(
    pid: int | None,
    stamp: Path = ROOT / "state" / "deadman.restart-stamp",
) -> str | None:
    """Self-heal a dead/wedged bot: release the instance lock (TERM, then KILL
    if it won't die) and ask the control panel to restart. Returns a one-line
    outcome for the page body, or None when the restart cooldown is active
    (a bot that dies again within ~10 min needs a human, not a spam loop)."""
    import time
    import urllib.request

    try:
        if time.time() - stamp.stat().st_mtime < RESTART_COOLDOWN_S:
            return None
    except OSError:
        pass
    try:
        stamp.touch()
    except OSError:
        pass
    if pid and bot_alive(pid):          # wedged — it still holds the flock
        subprocess.run(["kill", "-TERM", str(pid)], capture_output=True)
        for _ in range(10):
            time.sleep(1)
            if not bot_alive(pid):
                break
        else:
            subprocess.run(["kill", "-KILL", str(pid)], capture_output=True)
            time.sleep(2)
    try:
        req = urllib.request.Request(PANEL_RESTART_URL, method="POST")
        with urllib.request.urlopen(req, timeout=60) as resp:
            return f"auto-restart: {resp.read().decode().strip()}"
    except Exception as e:  # panel down too — the page must say so
        return f"auto-restart FAILED (panel unreachable?): {e}"


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
        # Print a visible heartbeat so an off-hours run is distinguishable from
        # a launchd job that never fired (the old silent return looked the same).
        print(f"[{now:%Y-%m-%d %H:%M:%S ET}] skip (market closed)", flush=True)
        return 0
    stamp = f"[{now:%Y-%m-%d %H:%M:%S ET}]"
    if flatten_hold_active():
        # scripts/flatten_and_restart.py is mid-flatten: the bot is down ON
        # PURPOSE while orders cancel and positions close. Do not kill,
        # restart, or page — a resurrection here would trade against the
        # close-all. The heartbeat line below keeps this run distinguishable
        # from a launchd job that never fired.
        print(f"{stamp} skip (flatten hold)", flush=True)
        return 0
    problem = diagnose()
    if problem is None:
        print(f"{stamp} ok", flush=True)
        return 0
    outcome = attempt_restart(bot_pid())
    if outcome:
        problem = f"{problem}\n\n{outcome}"
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
