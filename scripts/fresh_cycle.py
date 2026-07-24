"""Start a FRESH paper-trading cycle: reset local state, then relaunch the bot.

Use this to begin a new measurement window (e.g. a fresh 15-day paper run) after
the account itself is back to a clean $100k. It sequences the steps so the local
state can't fight the broker state:

  1. Confirm the Alpaca-side reset is done — you must have EITHER reset the paper
     account to $100k in the Alpaca dashboard, OR put new paper API keys in .env.
  2. Stop the running bot cleanly (SIGTERM the flock-held pid, wait for exit).
  3. Archive + clear THIS account's local state via investment_strategy.reset —
     the ledger, equity-history, and the risk latch/peak-equity. This is the step
     that matters: without it the dashboard shows the OLD trades and a stale
     peak-equity or today's halt latch can trip a FALSE drawdown/equity-floor
     halt on the fresh account.
  4. Run preflight as a sanity check.
  5. Relaunch the bot detached (same command the control panel uses).

Nothing is deleted — reset.py ARCHIVES to state/archive/<ts>/. Never touches
.env or the kill-switch file.

    .venv/bin/python scripts/fresh_cycle.py           # interactive (type FRESH)
    .venv/bin/python scripts/fresh_cycle.py --yes      # skip the prompt

NOTE: if you switched to a NEW paper account (new keys), the bot would also
auto-reset on its own via maybe_reset_on_account_change() at startup — this
script just makes the same-account "reset to $100k" case explicit and clean.
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ops.deadman import bot_alive, bot_pid  # noqa: E402


def _stop_bot(timeout_s: float = 30.0) -> None:
    pid = bot_pid()
    if not (pid and bot_alive(pid)):
        print("  bot not running — nothing to stop.")
        return
    print(f"  stopping bot (pid {pid})…")
    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not bot_alive(pid):
            print("  bot stopped.")
            return
        time.sleep(0.5)
    raise SystemExit(
        f"Old bot (pid {pid}) did not exit within {timeout_s:.0f}s — aborting the "
        "reset rather than risk two instances or a half-written wipe."
    )


def _run(label: str, args: list[str]) -> None:
    print(f"  {label}…")
    r = subprocess.run([sys.executable, "-m", *args], cwd=str(ROOT))
    if r.returncode != 0:
        raise SystemExit(f"{label} failed (exit {r.returncode}) — stopping here.")


def _relaunch() -> None:
    logs = ROOT / "logs"
    logs.mkdir(exist_ok=True)
    out = open(logs / "stdout.log", "ab")
    proc = subprocess.Popen(
        [sys.executable, "-m", "investment_strategy"],
        cwd=str(ROOT), stdout=out, stderr=subprocess.STDOUT, start_new_session=True,
    )
    time.sleep(4)
    if not bot_alive(proc.pid):
        raise SystemExit("Bot relaunched but died within 4s — check logs/bot.log.")
    print(f"  bot restarted (pid {proc.pid}).")


def main() -> int:
    ap = argparse.ArgumentParser(description="Reset local state + start a fresh paper cycle.")
    ap.add_argument("--yes", "-y", action="store_true", help="skip the confirmation prompt")
    args = ap.parse_args()

    print("FRESH CYCLE — this archives (not deletes) the current account's local")
    print("state and relaunches the bot. Do this ONLY after the Alpaca paper")
    print("account is back to a clean $100k (dashboard reset) or you've swapped")
    print("to new paper API keys in .env.\n")
    if not args.yes:
        if input("Type FRESH to proceed: ").strip() != "FRESH":
            print("Aborted — nothing changed.")
            return 1

    print("\n[1/4] Stopping the bot")
    _stop_bot()
    print("[2/4] Archiving + clearing local state")
    _run("reset", ["investment_strategy.reset", "--yes"])
    print("[3/4] Preflight")
    _run("preflight", ["investment_strategy.preflight"])
    print("[4/4] Relaunching the bot")
    _relaunch()
    print("\n✅ Fresh cycle started. Watch logs/bot.log; the first decision cycle")
    print("   runs at the next interval (or the market-open bell).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
