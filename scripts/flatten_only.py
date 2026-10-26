#!/usr/bin/env python
"""Flatten the PAPER account at the next market open — and STOP there.

Run-7 close (2026-10-26): the operator asked to stop the run and close every
old position, options spreads included, with NO relaunch — run-8 is being
built in a separate worktree and starts from a flat account on its own day 0.
`scripts/flatten_and_restart.py` would reset local state and relaunch the bot on
whatever code sits in this tree; this wrapper reuses its wait / stop / flatten
machinery (leg-ordered option closes, retries, hold marker) and skips steps 4-6.

Alpaca rejects option MARKET orders outside regular hours (code 42210000, seen
113x on Oct 26 pre-market), so the close has to happen at 09:31 ET.

    nohup .venv/bin/python scripts/flatten_only.py >> logs/flatten_only.log 2>&1 &
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "flatten_and_restart", ROOT / "scripts" / "flatten_and_restart.py")
far = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(far)


def main() -> int:
    base = far.env_val("ALPACA_BASE_URL")
    if "paper-api" not in base:
        far.say(f"REFUSING: ALPACA_BASE_URL is not the paper endpoint ({base})")
        return 1
    target = far.next_run_time()
    far.say(f"flatten-only: waiting for market open — will run at {target:%Y-%m-%d %H:%M ET}")
    far.wait_until(target)
    try:
        far.HOLD_MARKER.parent.mkdir(parents=True, exist_ok=True)
        far.HOLD_MARKER.touch()
        far.say("FLATTEN-HOLD ON (deadman stands down; it is also unloaded until the run-8 switch)")
    except OSError as e:
        far.say(f"WARNING: could not touch {far.HOLD_MARKER}: {e}")
    try:
        far.stop_running_bot()          # no-op when the bot is already down
        from alpaca.trading.client import TradingClient
        tc = TradingClient(far.env_val("ALPACA_API_KEY"), far.env_val("ALPACA_SECRET_KEY"), paper=True)
        acct = tc.get_account()
        far.say(f"account {acct.account_number}  equity=${float(acct.equity):,.2f}  cash=${float(acct.cash):,.2f}")
        ok = far.flatten(tc)
        acct = tc.get_account()
        far.say(f"after flatten: equity=${float(acct.equity):,.2f}  cash=${float(acct.cash):,.2f}  "
                f"positions={len(tc.get_all_positions())}  orders={len(tc.get_orders())}")
        far.say("flatten-only DONE — bot NOT restarted (run-8 switch is a separate step)" if ok
                else "flatten-only INCOMPLETE — see the REJECTED lines above; rerun during RTH or close by hand")
        return 0 if ok else 1
    finally:
        try:
            far.HOLD_MARKER.unlink(missing_ok=True)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
