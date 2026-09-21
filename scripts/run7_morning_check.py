"""Run-7 readiness / day verify — ONE read-only command instead of the hand-run list.

Replaces the by-hand steps of docs/RUN7_SWITCH.md section 4 + section 0 step 5.
Before the bell it answers "is the bot ready for the open?"; once the session is
open it also reads today's log for the first decision cycle, the FEEDS line, the
run-7 / A+ watch handles and the day's ERROR / CRITICAL lines.

    .venv/bin/python scripts/run7_morning_check.py              # auto-detects pre-open vs in-session
    .venv/bin/python scripts/run7_morning_check.py --no-network # skip the Alpaca / Robinhood calls

It places no orders, writes nothing, restarts nothing and never prints a secret
(keys are compared by hash). Exit 0 = no FAIL (WARNs allowed); 1 = at least one FAIL.

ROBINHOOD is classified, not just pass/failed, because the preflight line "the
OAuth token may be expired" is also what a Robinhood-side outage looks like
(2026-09-21: CloudFront 502 for hours; a re-login could not help and was tried):
  up          a real read returned
  THEIR SIDE  the UNAUTHENTICATED metadata endpoint answers 5xx / times out —
              do NOT re-login; the token is untouched and reads resume by themselves
  NEEDS LOGIN the bot's auth-dead latch is set (state/robinhood_health.json)
`rh=dead` is a pre-declared, non-confounding degraded mode in contract v3.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ops.deadman import (  # noqa: E402
    bot_alive, bot_pid, market_hours, tick_stamp_age_minutes,
)

ET = ZoneInfo("America/New_York")
# git tree id of investment_strategy/ at the frozen run-7 merge (contract v3,
# Config row). A different id on HEAD = the freeze is broken.
FROZEN_TREE = "2a6ec14778beafd22d402faae80c82eafb0353b0"
STRATEGY_KEYS = (
    "HEDGE_BETA_ASSUMED", "PROXY_PUT_PREFER_MONTHLY", "OPTION_STRIKE_SNAP",
    "OPTION_STRIKE_MAX_MONEYNESS_PCT", "REGIME_LOOSEN_MIN_CYCLES",
    "TOPUP_MIN_CONVICTION_DELTA", "HEDGE_UNWIND_MIN_CYCLES",
    "CORE_FILL_BETA_CLAMP", "HEDGE_STARVED_CORE_TRIM",
    "HEDGE_STARVED_TRIM_MAX_PCT", "REGIME_FALLING_TAPE_CAP",
    "LEDGER_RESTATE_AT_FILL",
)
ADAPTIVE_LOOPS_OFF = ("COMPOSITE_PERF_WEIGHTS", "EXPECTANCY_GATE_ENABLED",
                      "CURATED_LESSONS_INJECT")
# Handles from RUN7_SWITCH section 0 step 5 + section 4: counted and shown, not graded.
WATCH_HANDLES = (
    "CORE STOPLESS:", "CORE FILL:", "CORE FILL BETA CLAMP:", "AUTO-HEDGE STARVED",
    "REGIME FALLING-TAPE CAP", "Options opened", "AUTO-HEDGE:", "REGIME HOLD:",
    "PROXY PUT PICK:", "STRIKE SNAP:", "ENTRY TAPE:", "CORE DEFENSE:", "SLOT COUNT:",
)
RH_WELL_KNOWN = "https://agent.robinhood.com/.well-known/oauth-authorization-server"
RH_LOGIN_CMD = ".venv/bin/python -m investment_strategy.portfolio.robinhood_auth login"
# The first decision call lands ~5 min after the bell (Sep 18: 08:30 cycle, 08:35 call).
FIRST_CYCLE_GRACE_MIN = 20

_results: list[tuple[str, str, str]] = []


def _add(level: str, name: str, msg: str) -> None:
    _results.append((level, name, msg))
    print(f"  {level:<4}  {name:<15} {msg}")


def _sh(*args: str, timeout: float = 20) -> str:
    try:
        return subprocess.run(args, cwd=ROOT, capture_output=True, text=True,
                              timeout=timeout).stdout.strip()
    except Exception:
        return ""


def _env_lines() -> list[str]:
    try:
        return (ROOT / ".env").read_text().splitlines()
    except OSError:
        return []


def _env_value(lines: list[str], key: str) -> str | None:
    vals = [ln.split("=", 1)[1].split("#", 1)[0].strip().strip("'\"")
            for ln in lines if ln.startswith(f"{key}=")]
    return vals[-1] if vals else None


def _hash(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:12]


# --------------------------------------------------------------------------- #
def check_process() -> int | None:
    pids = [ln.split()[0] for ln in _sh("ps", "-axo", "pid,command").splitlines()
            if re.search(r"-m investment_strategy(\s|$)", ln)]
    pid = bot_pid()
    if len(pids) == 1 and pid and str(pid) == pids[0] and bot_alive(pid):
        up = _sh("ps", "-o", "etime=", "-p", str(pid)).strip()
        _add("PASS", "bot", f"one instance, pid {pid}, up {up}")
    elif not pids:
        _add("FAIL", "bot", "NOT RUNNING — restart: curl -X POST http://127.0.0.1:8787/api/restart")
        return None
    else:
        _add("FAIL", "bot", f"{len(pids)} instance(s) {pids}, lock names {pid} — "
                            "two log files growing = double bot")
    age = tick_stamp_age_minutes()
    if age is None:
        _add("WARN", "tick", "state/last_tick.stamp unreadable")
    elif age <= 5:
        _add("PASS", "tick", f"last tick {age:.1f} min ago")
    else:
        _add("FAIL", "tick", f"last tick {age:.1f} min ago — loop stalled or host slept")
    if (ROOT / "state" / "KILL").exists():
        _add("FAIL", "kill switch", "ENGAGED (state/KILL) — new buys are blocked")
    else:
        _add("PASS", "kill switch", "off")
    return pid


def check_freeze(pid: int | None) -> None:
    tree = _sh("git", "rev-parse", "HEAD:investment_strategy")
    dirty = _sh("git", "status", "--porcelain", "--", "investment_strategy/")
    head = _sh("git", "log", "--oneline", "-1")[:60]
    if tree == FROZEN_TREE and not dirty:
        _add("PASS", "freeze", f"investment_strategy/ = frozen tree {FROZEN_TREE[:8]} ({head})")
    elif tree != FROZEN_TREE:
        _add("FAIL", "freeze", f"investment_strategy/ tree {tree[:8]} != frozen {FROZEN_TREE[:8]} "
                               "— fingerprint changed inside the window")
    else:
        _add("FAIL", "freeze", f"uncommitted edits under investment_strategy/: {dirty.splitlines()[:3]}")
    if not pid:
        return
    try:
        started = dt.datetime.strptime(_sh("ps", "-o", "lstart=", "-p", str(pid)),
                                       "%a %b %d %H:%M:%S %Y").timestamp()
    except ValueError:
        return
    newest = max((p.stat().st_mtime for p in (ROOT / "investment_strategy").rglob("*.py")),
                 default=0.0)
    stale = [n for n, m in (("code", newest), (".env", (ROOT / ".env").stat().st_mtime))
             if m > started]
    if stale:
        _add("FAIL", "loaded", f"{' and '.join(stale)} changed AFTER the bot started — "
                               "it is running the old copy; restart it")
    else:
        _add("PASS", "loaded", "bot started after the last code and .env change")


def check_env(pid: int | None) -> None:
    lines = _env_lines()
    bad = [f"{k}x{n}" for k in STRATEGY_KEYS
           if (n := sum(ln.startswith(f"{k}=") for ln in lines)) != 1]
    on = [k for k in ADAPTIVE_LOOPS_OFF if (_env_value(lines, k) or "off").lower() == "on"]
    if bad:
        _add("FAIL", ".env keys", f"run-7 key lines not exactly once: {bad}")
    elif on:
        _add("FAIL", ".env keys", f"adaptive loop(s) ON {on} — contract validity condition")
    else:
        _add("PASS", ".env keys", "12 strategy keys once each; adaptive loops off")
    if not pid:
        return
    # load_dotenv() is override=False: a key INHERITED from the parent beats .env.
    env = _sh("ps", "eww", "-o", "command=", "-p", str(pid))
    inherited = dict(t.split("=", 1) for t in env.split(" ")
                     if t.startswith(("ALPACA_API_KEY=", "ALPACA_SECRET_KEY=")))
    diff = [k for k, v in inherited.items() if _hash(v) != _hash(_env_value(lines, k) or "")]
    if diff:
        _add("FAIL", "key source", f"bot INHERITED a different {diff} than .env holds "
                                   "— it may be trading another account")
    else:
        _add("PASS", "key source", ".env is the bot's Alpaca key source "
                                   f"({len(inherited)} inherited, none conflicting)")


def check_alpaca() -> bool | None:
    """Returns the broker's is_open (None when unreadable)."""
    lines = _env_lines()
    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
        c = TradingClient(_env_value(lines, "ALPACA_API_KEY"),
                          _env_value(lines, "ALPACA_SECRET_KEY"), paper=True)
        a = c.get_account()
        npos = len(c.get_all_positions())
        nord = len(c.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=100)))
        clock = c.get_clock()
    except Exception as e:
        _add("FAIL", "alpaca", f"could not read the account: {type(e).__name__}: {str(e)[:120]}")
        return None
    want = (ROOT / "state" / "account.json").read_text().strip().removeprefix("paper:")
    blocked = [f for f in ("trading_blocked", "account_blocked", "trade_suspended_by_user")
               if getattr(a, f, False)]
    status = str(a.status).rsplit(".", 1)[-1]
    if a.account_number != want:
        _add("FAIL", "alpaca", f".env keys open {a.account_number} but state/ belongs to {want}")
    elif status != "ACTIVE" or blocked:
        _add("FAIL", "alpaca", f"{a.account_number} status {status}, blocked: {blocked}")
    else:
        _add("PASS", "alpaca", f"{a.account_number} ACTIVE, equity ${float(a.equity):,.2f}, "
                               f"cash ${float(a.cash):,.2f}, options L{a.options_trading_level}, "
                               f"{npos} position(s), {nord} open order(s)")
    nxt = clock.next_close if clock.is_open else clock.next_open
    _add("INFO", "market", f"{'OPEN' if clock.is_open else 'closed'}; next "
                           f"{'close' if clock.is_open else 'open'} {nxt.astimezone(ET):%a %H:%M ET}")
    return bool(clock.is_open)


def check_robinhood(network: bool) -> None:
    lines = _env_lines()
    if (_env_value(lines, "ROBINHOOD_ENABLED") or "off").lower() != "on":
        _add("INFO", "robinhood", "disabled (ROBINHOOD_ENABLED != on)")
        return
    tok = ROOT / "state" / "robinhood_oauth.json"
    expiry = ""
    try:
        exp = tok.stat().st_mtime + float(json.loads(tok.read_text())["tokens"]["expires_in"])
        left_h = (exp - time.time()) / 3600
        expiry = f"access token until {dt.datetime.fromtimestamp(exp):%a %b %d %H:%M} ({left_h:.0f} h)"
    except Exception:
        expiry = "token file unreadable"
    try:
        health = json.loads((ROOT / "state" / "robinhood_health.json").read_text())
    except Exception:
        health = {}
    if health.get("auth_dead"):
        _add("FAIL", "robinhood", f"NEEDS LOGIN — auth-dead latch set ({health.get('detail')}). "
                                  f"Run: {RH_LOGIN_CMD}")
        return
    if not network:
        _add("INFO", "robinhood", f"latch clear; {expiry} (network probe skipped)")
        return
    try:
        with urllib.request.urlopen(RH_WELL_KNOWN, timeout=20) as r:
            code, via = r.status, r.headers.get("x-cache", "")
    except urllib.error.HTTPError as e:
        code, via = e.code, e.headers.get("x-cache", "")
    except Exception as e:
        code, via = 0, type(e).__name__
    if code == 0 or code >= 500:
        _add("WARN", "robinhood", f"THEIR SIDE — unauthenticated probe got HTTP {code or 'timeout'} "
                                  f"({via}). Do NOT re-login; {expiry}. Bot runs on with rh degraded "
                                  "(pre-declared non-confounding); reads resume by themselves.")
        return
    try:
        import logging
        logging.disable(logging.CRITICAL)
        from investment_strategy.config import load_config
        from investment_strategy.portfolio.robinhood import RobinhoodReader
        reader = RobinhoodReader(load_config())
        names = reader.call_json("__list_tools__")
        logging.disable(logging.NOTSET)
    except Exception as e:
        names = None
        via = f"{type(e).__name__}"
    if names:
        _add("PASS", "robinhood", f"up — MCP session opened, {len(names)} tools; {expiry}")
    else:
        _add("WARN", "robinhood", f"server answers HTTP {code} but the MCP read failed ({via}); "
                                  f"{expiry}. If the latch sets, run: {RH_LOGIN_CMD}")


def check_host() -> None:
    today = dt.datetime.now(ET).date()
    try:
        cal = json.loads((ROOT / "state" / "session_calendar.json").read_text())
        sess = cal.get("sessions", {}).get(today.isoformat())
        nxt = min((d for d in cal.get("sessions", {}) if d > today.isoformat()), default=None)
        if sess:
            _add("PASS", "calendar", f"today {today} is a session {sess['open']}-{sess['close']} ET")
        elif today.weekday() < 5 and cal.get("end", "") < today.isoformat():
            _add("WARN", "calendar", f"cache ends {cal.get('end')} — the decision loop refreshes it")
        else:
            _add("INFO", "calendar", f"today {today} is not a session; next {nxt}")
    except Exception as e:
        _add("WARN", "calendar", f"state/session_calendar.json unreadable ({type(e).__name__}) "
                                 "— weekday fallback math applies")
    spool = ROOT / "state" / "alerts_spool.jsonl"
    if spool.exists() and spool.stat().st_size:
        _add("FAIL", "alert spool", f"{len(spool.read_text().splitlines())} undelivered page(s) "
                                    "— the pager is not delivering; check the alert sinks")
    else:
        _add("PASS", "alert spool", "empty (pages are being delivered)")
    jobs = {ln.split("\t")[2]: ln.split("\t")[:2] for ln in _sh("launchctl", "list").splitlines()
            if "com.investment-strategy." in ln}
    dm = jobs.get("com.investment-strategy.deadman")
    try:
        dm_age = (time.time() - (ROOT / "logs" / "deadman.log").stat().st_mtime) / 60
    except OSError:
        dm_age = 999.0
    if dm and dm[1] == "0" and dm_age <= 7:
        _add("PASS", "deadman", f"loaded, last exit 0, ran {dm_age:.1f} min ago")
    else:
        _add("FAIL", "deadman", f"job={dm} last run {dm_age:.0f} min ago — the self-heal net is down")
    for job in ("panel", "awake"):
        j = jobs.get(f"com.investment-strategy.{job}")
        if not (j and j[0] != "-"):
            _add("WARN", job, "launchd job not running")
    try:
        with urllib.request.urlopen("http://127.0.0.1:8787/api/status", timeout=5) as r:
            st = json.loads(r.read())
        _add("PASS", "panel", f"http://127.0.0.1:8787 up (bot_alive={st.get('bot_alive')})")
    except Exception as e:
        _add("WARN", "panel", f"control panel not answering ({type(e).__name__})")
    batt = _sh("pmset", "-g", "batt")
    if "AC Power" in batt:
        _add("PASS", "power", "on AC")
    else:
        lvl = re.search(r"(\d+)%", batt)
        _add("FAIL", "power", f"ON BATTERY ({lvl.group(1) if lvl else '?'}%) — battery/clamshell "
                              "sleep blinds the bot; plug in AC")
    free_gb = shutil.disk_usage(ROOT).free / 1e9
    _add("PASS" if free_gb > 5 else "FAIL", "disk", f"{free_gb:.0f} GB free")


def check_fresh_state() -> None:
    """Pre-open on day 1 only: the ledger and risk state must be the clean fresh cycle."""
    try:
        rows = [json.loads(ln) for ln in
                (ROOT / "state" / "equity_history.jsonl").read_text().splitlines() if ln.strip()]
    except Exception:
        rows = []
    anchors = [r for r in rows if r.get("basis") in ("close", "late")]
    if anchors:
        a = anchors[-1]
        _add("PASS", "equity rows", f"{len(anchors)} close/late row(s); last {a['date']} "
                                    f"basis={a['basis']} equity ${a['equity']:,.2f}")
    else:
        _add("FAIL", "equity rows", "no close/late row — the checker will print NO DAY-0 ANCHOR")
    try:
        ntrades = sum(1 for ln in (ROOT / "state" / "trades.jsonl").read_text().splitlines()
                      if ln.strip())
    except OSError:
        ntrades = 0
    _add("INFO", "ledger", f"{ntrades} trade row(s) in state/trades.jsonl")


def _run_start_local() -> str:
    """Local 'YYYY-MM-DD HH:MM:SS' of the current run's first equity row, less 2
    min of slack for the start-up lines that precede it; '' when unreadable.
    fresh_cycle clears equity_history, so that row IS the run's start: on the
    switch day the same bot.log also holds the PREVIOUS account's session (Sep 21
    2026: run-6's morning battery CRITICALs), which is not this run's business."""
    try:
        first = json.loads((ROOT / "state" / "equity_history.jsonl").read_text().splitlines()[0])
        ts = dt.datetime.fromisoformat(first["ts"]).astimezone() - dt.timedelta(minutes=2)
        return ts.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def _level(ln: str) -> str:
    """The logging LEVEL field (3rd token) — not the word inside a message
    ('INFO notify | CRITICAL alert emailed ...' is an INFO line)."""
    parts = ln.split(" ", 3)
    ok = len(parts) > 3 and parts[2] in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
    return parts[2] if ok else ""  # '' for a continuation line (traceback, wrapped message)


def check_log(is_open: bool | None) -> None:
    now_et = dt.datetime.now(ET)
    today_local = dt.date.today().isoformat()
    since = max(today_local, _run_start_local())
    try:
        lines = [ln for ln in (ROOT / "logs" / "bot.log").read_text(errors="replace").splitlines()
                 if ln.startswith(today_local) and ln[:19] >= since]
    except OSError:
        _add("FAIL", "log", "logs/bot.log unreadable")
        return
    if since > today_local:
        _add("INFO", "log scope", f"this run began today — reading bot.log from {since[11:]} local "
                                  "(earlier lines belong to the previous account)")
    starts = [ln[11:19] for ln in lines if "Starting orchestrator" in ln]
    _add("INFO", "restarts", f"{len(starts)} start(s) today {starts[-3:]}")
    in_session = bool(is_open) if is_open is not None else market_hours()
    open_et = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    mins_open = (now_et - open_et).total_seconds() / 60
    cycles = [ln[11:19] for ln in lines if "Claude returned" in ln]
    if not in_session and not cycles:
        _add("INFO", "cycles", "market closed — no decision cycle expected yet. Re-run this "
                               "after 09:50 ET / 08:50 CT for the in-session verdict.")
    elif cycles:
        _add("PASS", "cycles", f"{len(cycles)} decision cycle(s) today, first {cycles[0]}, "
                               f"last {cycles[-1]} (local time)")
    elif mins_open < FIRST_CYCLE_GRACE_MIN:
        _add("INFO", "cycles", f"open for {mins_open:.0f} min — first decision call is due by "
                               f"+{FIRST_CYCLE_GRACE_MIN} min; re-run then")
    else:
        _add("FAIL", "cycles", f"market open {mins_open:.0f} min and NO decision cycle today — "
                               "check 'tail -50 logs/bot.log'")
    feeds = [ln for ln in lines if "| FEEDS:" in ln]
    sick = [ln for ln in feeds if re.search(r"UNHEALTHY|DEAD|earnings=none", ln)]
    if feeds:
        last = feeds[-1].split("| FEEDS:", 1)[1].strip()[:110]
        lvl = "PASS" if not sick else ("WARN" if len(sick) <= 2 else "FAIL")
        note = "" if not sick else f"; {len(sick)} degraded cycle(s) (>2 in a day = validity breach)"
        _add(lvl, "feeds", f"{len(feeds)} line(s), last: {last}{note}")
    elif cycles:
        _add("FAIL", "feeds", "decision cycles ran but no FEEDS: line — a missing handle is a "
                              "measurement breach, not a zero")
    betas = sum("| BOOK BETA:" in ln for ln in lines)
    if cycles:
        _add("PASS" if betas >= len(cycles) else "WARN", "book beta",
             f"{betas} BOOK BETA line(s) for {len(cycles)} cycle(s)")
    refreshed = next((i for i, ln in enumerate(lines) if "Session calendar refreshed" in ln), None)
    if refreshed is not None and any("Session calendar fallback:" in ln for ln in lines[refreshed:]):
        _add("WARN", "calendar log", "'Session calendar fallback:' AFTER today's refresh")
    if any("LEDGER PHANTOMS:" in ln for ln in lines):
        _add("WARN", "ledger", "LEDGER PHANTOMS: printed — should not on a fresh ledger")
    sev = Counter(_level(ln) for ln in lines)
    tb = sum("Traceback" in ln for ln in lines)
    # A CRITICAL is a page. One from the last hour means something is wrong NOW
    # (FAIL); an older one was already paged and may be over (WARN — read it).
    hour_ago = (dt.datetime.now() - dt.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    fresh = sum(_level(ln) == "CRITICAL" and ln[:19] >= hour_ago for ln in lines)
    lvl = "FAIL" if fresh else ("WARN" if sev["CRITICAL"] or sev["ERROR"] or tb else "PASS")
    _add(lvl, "severity", f"{sev['CRITICAL']} CRITICAL ({fresh} in the last hour), "
                          f"{sev['ERROR']} ERROR, {sev['WARNING']} WARNING, {tb} traceback(s)")
    for ln in [ln for ln in lines if _level(ln) in ("ERROR", "CRITICAL")][-4:]:
        print(f"          └ {ln[11:19]} {ln.split(' ', 3)[-1][:150]}")
    seen = {h: sum(h in ln for ln in lines) for h in WATCH_HANDLES}
    hits = ", ".join(f"{h.rstrip(':')} x{n}" for h, n in seen.items() if n)
    _add("INFO", "handles", hits or "none of the run-7 / A+ watch handles printed yet today")
    for h in ("CORE STOPLESS:", "AUTO-HEDGE STARVED", "REGIME FALLING-TAPE CAP"):
        for ln in [ln for ln in lines if h in ln][-2:]:
            print(f"          └ {ln[11:19]} {ln.split('| ', 1)[-1][:150]}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--no-network", action="store_true",
                    help="skip the Alpaca and Robinhood calls (local checks only)")
    args = ap.parse_args()
    now = dt.datetime.now(ET)
    print(f"RUN-7 CHECK — {now:%a %Y-%m-%d %H:%M ET} / {dt.datetime.now():%H:%M} local\n")
    pid = check_process()
    check_freeze(pid)
    check_env(pid)
    is_open = None if args.no_network else check_alpaca()
    check_robinhood(network=not args.no_network)
    check_host()
    check_fresh_state()
    check_log(is_open)
    fails = [r for r in _results if r[0] == "FAIL"]
    warns = [r for r in _results if r[0] == "WARN"]
    verdict = "NOT READY" if fails else ("READY (with warnings)" if warns else "READY")
    print(f"\nVERDICT: {verdict} — {len(fails)} fail, {len(warns)} warn")
    for _, name, msg in fails:
        print(f"  FIX  {name}: {msg[:160]}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
