#!/usr/bin/env python
"""Flatten the PAPER account at the next market open, then restart the bot clean.

Why this exists: `python -m investment_strategy.reset` clears LOCAL state only
(ledger, risk memory, dashboard) — it cannot touch the broker. Positions and
orders live at Alpaca, and while the market is closed cancels sit in
`pending_cancel` holding every share (the after-hours form of the
pending_cancel wedge), so the account can't be flattened until a session opens.

This script waits for the next regular session (09:31 ET), then:
  1. stops the RUNNING bot (pid from state/bot.lock; SIGINT -> TERM -> KILL) —
     it must not trade against the flatten or hold the single-instance flock
  2. cancels every open order and waits for the cancels to finalize
  3. closes every position (equities + options) and waits until flat
  4. runs `investment_strategy.reset --yes`  (archives local state)
  5. runs `investment_strategy.preflight`    (sanity gate)
  6. starts the bot detached (fresh code from this working tree), appending
     to logs/stdout.log

If the account is already flat (e.g. you clicked "Reset" on the Alpaca paper
dashboard over the weekend — which is also the only way to restore the exact
$100k default), steps 1–2 are no-ops and it just resets + restarts.

Run detached:  nohup .venv/bin/python scripts/flatten_and_restart.py \
                   >> logs/flatten_restart.log 2>&1 &
Run now (market already open):  same command — it fires immediately during RTH.
PAPER-ONLY: refuses to run unless ALPACA_BASE_URL points at paper-api.
"""
from __future__ import annotations

import datetime as dt
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"
PYTHON = ROOT / ".venv" / "bin" / "python"
ET = ZoneInfo("America/New_York")


def say(msg: str) -> None:
    print(f"[{dt.datetime.now(ET):%Y-%m-%d %H:%M:%S ET}] {msg}", flush=True)


def env_val(name: str) -> str:
    for line in ENV.read_text(encoding="utf-8").splitlines():
        if line.startswith(name + "="):
            return line.split("=", 1)[1].split("#", 1)[0].strip()
    return ""


def next_run_time() -> dt.datetime:
    """Next weekday 09:31 ET — or right now if we're inside regular hours."""
    now = dt.datetime.now(ET)
    open_t = now.replace(hour=9, minute=31, second=0, microsecond=0)
    close_t = now.replace(hour=15, minute=55, second=0, microsecond=0)
    if now.weekday() < 5 and open_t <= now < close_t:
        return now
    day = now if (now.weekday() < 5 and now < open_t) else now + dt.timedelta(days=1)
    while day.weekday() >= 5:
        day += dt.timedelta(days=1)
    return day.replace(hour=9, minute=31, second=0, microsecond=0)


def wait_until(target: dt.datetime) -> None:
    # Short chunks so a Mac that slept through the target fires promptly on wake
    # (a single long time.sleep does not count time spent in system sleep).
    while True:
        remaining = (target - dt.datetime.now(ET)).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(600, remaining))


def stop_running_bot() -> None:
    """SIGINT (then escalate) the bot holding state/bot.lock, and wait for it
    to exit. Without this step the OLD process keeps trading against the
    flatten, rewrites the freshly-archived state, and its flock makes the new
    start REFUSE (the single-instance guard) — i.e. the day runs on stale code
    with reset state. Called only at market open, not at script launch, so the
    watchdog keeps protecting positions across the weekend wait."""
    lock = ROOT / "state" / "bot.lock"
    if not lock.exists():
        return
    try:
        pid = int(lock.read_text().strip() or 0)
    except ValueError:
        pid = 0
    if pid <= 0:
        return
    # Only signal a process that is actually the bot — a pid from a stale
    # lockfile could have been recycled by something unrelated.
    cmd = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                         capture_output=True, text=True).stdout
    if "investment_strategy" not in cmd:
        say(f"bot.lock pid {pid} is not a running bot — nothing to stop")
        return
    # SIGINT first: the orchestrator's KeyboardInterrupt path is its clean
    # "Interrupted — shutting down" shutdown. Escalate only if it hangs.
    for sig, wait_s in ((signal.SIGINT, 30), (signal.SIGTERM, 15),
                        (signal.SIGKILL, 5)):
        say(f"stopping the running bot: pid {pid} <- {sig.name}")
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            say("old bot exited")
            return
        for _ in range(wait_s):
            time.sleep(1)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                say("old bot exited")
                return
    say(f"WARNING: pid {pid} survived SIGKILL?! — continuing anyway")


def flatten(tc) -> bool:
    """Cancel all orders, close all positions. True when the account is flat."""
    orders = tc.get_orders()
    say(f"open orders: {len(orders)}")
    if orders:
        tc.cancel_orders()
        for _ in range(120):
            if not tc.get_orders():
                break
            time.sleep(5)
        left = tc.get_orders()
        say(f"after cancel: {len(left)} orders remain")
        if left:
            return False

    positions = tc.get_all_positions()
    say(f"open positions: {len(positions)}")
    if positions:
        for p in positions:
            say(f"  closing {p.symbol} qty={p.qty}")
        tc.close_all_positions(cancel_orders=True)
        for _ in range(180):
            if not tc.get_all_positions():
                break
            time.sleep(5)
        left = tc.get_all_positions()
        if left:
            say(f"NOT FLAT — {len(left)} positions remain: "
                + ", ".join(p.symbol for p in left))
            return False
    say("account is flat: no orders, no positions")
    return True


def main() -> int:
    base = env_val("ALPACA_BASE_URL")
    if "paper-api" not in base:
        say(f"REFUSING: ALPACA_BASE_URL is not the paper endpoint ({base})")
        return 1

    target = next_run_time()
    say(f"waiting for market open — will run at {target:%Y-%m-%d %H:%M ET}")
    wait_until(target)

    # Stop the old bot FIRST: it must not trade against the flatten, must not
    # rewrite state after the reset archives it, and must release the
    # single-instance flock or the fresh start below gets refused.
    stop_running_bot()

    from alpaca.trading.client import TradingClient
    tc = TradingClient(env_val("ALPACA_API_KEY"), env_val("ALPACA_SECRET_KEY"),
                       paper=True)
    acct = tc.get_account()
    say(f"account {acct.account_number}  equity=${float(acct.equity):,.2f}")

    if not flatten(tc):
        say("Could not fully flatten — NOT starting the bot. "
            "Fix manually, or click Reset on the Alpaca paper dashboard, "
            "then rerun this script.")
        return 1

    say("resetting local state...")
    subprocess.run([str(PYTHON), "-m", "investment_strategy.reset", "--yes"],
                   cwd=ROOT, check=True)

    say("running preflight...")
    pf = subprocess.run([str(PYTHON), "-m", "investment_strategy.preflight"], cwd=ROOT)
    if pf.returncode != 0:
        say("preflight FAILED — NOT starting the bot. See output above.")
        return 1

    say("starting the bot detached...")
    out = open(ROOT / "logs" / "stdout.log", "ab")
    proc = subprocess.Popen([str(PYTHON), "-m", "investment_strategy"],
                            cwd=ROOT, stdout=out, stderr=subprocess.STDOUT,
                            start_new_session=True)
    time.sleep(5)
    lock = ROOT / "state" / "bot.lock"
    say(f"bot started pid={proc.pid}  bot.lock="
        f"{lock.read_text().strip() if lock.exists() else 'MISSING'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
