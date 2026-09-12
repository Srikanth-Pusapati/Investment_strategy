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
  3. closes every position (equities + options) ONE SYMBOL AT A TIME in leg
     order — short legs first, then their covers — waiting for each batch to
     leave the position list, and retries leftovers for up to CLOSE_ROUNDS
     (a leftover whose earlier close order is still working is waited on,
     not re-sent),
     printing every broker response (order id, or the rejection body)
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
import re
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
# Dead-man hold-off marker: ops/deadman.py (5-min launchd ticks) auto-restarts
# a dead bot through the control panel — mid-flatten that would resurrect the
# bot to trade AGAINST the close-all. Touched when the flatten BEGINS (not at
# script launch: the open-wait can be hours and the dead-man must keep guarding
# until then), removed in a finally after the relaunch. deadman ignores markers
# older than 2h, so even a flatten that dies before its finally cannot mute the
# dead-man forever.
HOLD_MARKER = ROOT / "state" / "flatten.hold"
# Close choreography (Aug 31 2026 abort): the old flatten called
# close_all_positions, which fires every close at once. The long IWM 295P was
# the cover of the short IWM 280P (a debit put spread), and Alpaca refuses to
# sell a covering long while the short it covers is still open (it would leave
# a naked short) — so the 295P sale was rejected, the per-position response
# was thrown away, nothing was retried, and after 15 minutes the script quit
# with "NOT FLAT — 1 positions remain: IWM260930P00295000" (the operator sold
# it by hand; logs/flatten_restart.log 11:41-11:56 ET). Now: one
# close_position per symbol, SHORT legs first and confirmed gone before the
# longs go out, leftovers retried up to CLOSE_ROUNDS with every response
# printed so a persistent rejection is diagnosable from the log alone.
# Round >= 2 (review fix, 2026-09-12): a leftover whose close order from an
# earlier round is STILL WORKING (accepted, unfilled past CLOSE_WAIT_S — an
# illiquid option cover) is NOT re-sent: Alpaca would reject the duplicate on
# qty available (the open order holds the contracts), the cover would keep
# failing the naked guard, and after 3 rounds the script would report NOT
# FLAT although the first order fills minutes later. It is waited on instead.
CLOSE_ROUNDS = 3          # per-symbol close attempts before giving up
CLOSE_WAIT_S = 180        # max wait for one batch's closes to leave the book
CLOSE_POLL_S = 5          # position-list poll interval during that wait
CLOSE_ROUND_PAUSE_S = 10  # pause between rounds (lets rejected fills settle)
_OCC_RE = re.compile(r"^[A-Z]{1,6}\d{6}[CP]\d{8}$")   # e.g. IWM260930P00295000


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


def _qty(p) -> float:
    return float(getattr(p, "qty", 0) or 0)


def _is_option(p) -> bool:
    """Alpaca Position.asset_class is the AssetClass enum ("us_option"); fall
    back to the OCC symbol shape when the row carries no asset_class."""
    ac = getattr(p, "asset_class", None)
    ac = getattr(ac, "value", ac)
    if ac:
        return str(ac).lower() == "us_option"
    return bool(_OCC_RE.match(str(getattr(p, "symbol", "") or "")))


def order_close_legs(positions) -> list:
    """The order positions must be closed in: SHORT legs first (qty < 0,
    short options before short stock), then longs (options before equities),
    symbol-sorted within a group so the sequence is deterministic. A short
    option's cover is a long on the same underlying; buying back the short is
    always allowed, selling the cover while the short is open is not — so
    every short must be gone before its cover is sold. Pure: returns a new
    list, never mutates the input."""
    return sorted(
        positions,
        key=lambda p: (0 if _qty(p) < 0 else 1,
                       0 if _is_option(p) else 1,
                       str(getattr(p, "symbol", ""))),
    )


def describe_close_response(resp) -> str:
    """One greppable clause for whatever a close call returned: an Order
    (close_position) or a ClosePositionResponse (close_all_positions)."""
    http = getattr(resp, "status", None)
    if isinstance(http, int):                     # ClosePositionResponse
        body = getattr(resp, "body", None)
        detail = (f"order={getattr(resp, 'order_id', None)}" if http < 300
                  else f"body={body!r}")
        return f"HTTP {http} {detail}"
    return f"order={getattr(resp, 'id', None)} status={http}"


def _describe_error(e: Exception) -> str:
    """alpaca APIError carries status_code + a JSON body with code/message;
    anything else (connection blips) prints as type: text."""
    code = getattr(e, "status_code", None)
    try:
        msg = e.message                            # APIError: parsed JSON body
    except Exception:  # noqa: BLE001 — non-JSON body or not an APIError
        msg = str(e)
    return f"{type(e).__name__} HTTP {code if code is not None else '?'}: {msg}"


def _working_close_orders(tc) -> set:
    """Symbols with an OPEN order at the broker — consulted from round 2 on,
    when the only open orders are the earlier rounds' accepted-but-unfilled
    closes (step 2 cancelled everything else). Never raises: an unreadable
    order list means every leftover is re-sent, exactly as before."""
    try:
        return {getattr(o, "symbol", None) for o in tc.get_orders()} - {None}
    except Exception as e:  # noqa: BLE001 — advisory read
        say(f"  open-order check failed ({_describe_error(e)}); re-sending every leftover")
        return set()


def _close_batch(tc, phase: str, batch: list, working: set = frozenset()) -> set:
    """One close_position per symbol, in the given order. Per-symbol failures
    are printed (greppable 'close ... REJECTED') and skipped so one bad leg
    never stops the rest. A symbol in `working` (its close from an earlier
    round is still open) is NOT re-sent — printed as 'SKIPPED' and waited on.
    Returns the symbols worth waiting on: the ones the broker ACCEPTED a close
    for now, plus the skipped ones."""
    sent: set = set()
    for p in batch:
        if p.symbol in working:
            say(f"  {phase} close {p.symbol} qty={p.qty} SKIPPED: a close order is "
                f"still working from an earlier round — waiting on it")
            sent.add(p.symbol)
            continue
        try:
            resp = tc.close_position(p.symbol)
        except Exception as e:  # noqa: BLE001 — isolate per-symbol failures
            say(f"  {phase} close {p.symbol} qty={p.qty} REJECTED: {_describe_error(e)}")
            continue
        sent.add(p.symbol)
        say(f"  {phase} close {p.symbol} qty={p.qty} -> {describe_close_response(resp)}")
    return sent


def _wait_gone(tc, symbols: set) -> set:
    """Poll the position list until none of `symbols` remains, or CLOSE_WAIT_S
    elapses. Returns the symbols still held."""
    still = symbols & {p.symbol for p in tc.get_all_positions()}
    waited = 0
    while still and waited < CLOSE_WAIT_S:
        time.sleep(CLOSE_POLL_S)
        waited += CLOSE_POLL_S
        still = symbols & {p.symbol for p in tc.get_all_positions()}
    return still


def flatten(tc) -> bool:
    """Cancel all orders, then close every position — one close_position per
    symbol in order_close_legs() order, shorts confirmed gone before their
    covers go out, leftovers retried up to CLOSE_ROUNDS (a leftover whose
    earlier close is still working is waited on, not re-sent). True when flat."""
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
    for rnd in range(1, CLOSE_ROUNDS + 1):
        if not positions:
            break
        legs = order_close_legs(positions)
        say(f"close round {rnd}/{CLOSE_ROUNDS}: {len(legs)} positions in leg order: "
            + ", ".join(f"{p.symbol} qty={p.qty}" for p in legs))
        shorts = [p for p in legs if _qty(p) < 0]
        longs = [p for p in legs if _qty(p) >= 0]
        # Round >= 2: an earlier round's close may still be working (see the
        # constants block) — such a leftover is waited on, never re-sent.
        working = _working_close_orders(tc) if rnd > 1 else set()
        # Shorts go out first and must be OFF the book before a single long is
        # sold: the long may be the short's cover, and Alpaca rejects selling
        # a cover while the short it covers is open (the Aug 31 abort).
        for phase, batch in (("short", shorts), ("long", longs)):
            if not batch:
                continue
            sent = _close_batch(tc, phase, batch, working)
            if not sent:
                continue
            still = _wait_gone(tc, sent)
            if still:
                say(f"  {phase} closes still open after {CLOSE_WAIT_S}s: "
                    + ", ".join(sorted(still)))
        positions = tc.get_all_positions()
        if positions and rnd < CLOSE_ROUNDS:
            say(f"  round {rnd}/{CLOSE_ROUNDS}: {len(positions)} remain: "
                + ", ".join(p.symbol for p in positions)
                + f" — retrying in {CLOSE_ROUND_PAUSE_S}s")
            time.sleep(CLOSE_ROUND_PAUSE_S)
    if positions:
        say(f"NOT FLAT — {len(positions)} positions remain after {CLOSE_ROUNDS} rounds: "
            + ", ".join(p.symbol for p in positions)
            + " (see the 'close ... REJECTED' lines above for the broker's reason)")
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

    # The flatten BEGINS here: raise the dead-man hold-off so ops/deadman.py
    # does not auto-restart the bot we are about to stop (it would trade
    # against the close-all). finally-cleanup covers every exit path below.
    try:
        HOLD_MARKER.parent.mkdir(parents=True, exist_ok=True)
        HOLD_MARKER.touch()
        say("FLATTEN-HOLD ON: touched state/flatten.hold — deadman stands down (auto-expires after 2h)")
    except OSError as e:
        say(f"WARNING: could not touch {HOLD_MARKER}: {e} — deadman may auto-restart mid-flatten")
    try:
        return _flatten_reset_restart()
    finally:
        try:
            HOLD_MARKER.unlink(missing_ok=True)
            say("FLATTEN-HOLD OFF: removed state/flatten.hold — deadman resumes")
        except OSError as e:
            say(f"WARNING: could not remove {HOLD_MARKER}: {e} — deadman ignores it after 2h anyway")


def _flatten_reset_restart() -> int:
    """The flatten itself — bot already waited-for-open; hold marker is up."""
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
