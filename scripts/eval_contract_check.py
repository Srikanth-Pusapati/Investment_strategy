#!/usr/bin/env python3
"""EVAL-CONTRACT: pre-registered GO/NO-GO checker for pre-final-test-run-5.

Deterministic and OFFLINE — stdlib only (json + math), no bot imports, no
network, no scipy. Run it with plain python3 against the bot's own ledgers:

  state/trades.jsonl           one JSON object per trade row; CLOSED trades
                               carry a non-null "realized_pl"
  state/equity_history.jsonl   one JSON object per day: date/equity/day_pl

Usage:
  python3 scripts/eval_contract_check.py --start 2026-08-24 --end 2026-09-04
  python3 scripts/eval_contract_check.py --start ... --end ... --spy-csv spy.csv
  python3 scripts/eval_contract_check.py --start ... --end ... --spy-csv spy.csv \
      --bench-csv IWM=iwm.csv --bench-csv QQQ=qqq.csv
  python3 scripts/eval_contract_check.py --selftest
  python3 scripts/eval_contract_check.py --contract v2 --start ... --end ... \
      --spy-csv spy.csv --bench-csv QQQ=qqq.csv --bench-csv IWM=iwm.csv \
      --pool runs/pre-final-test-run-5/state/trades.jsonl --beta-target 1.0
  python3 scripts/eval_contract_check.py --contract v3 --start <day1> --end <last> \
      --spy-csv spy.csv --bench-csv QQQ=qqq.csv --bench-csv IWM=iwm.csv \
      --beta-target 1.0 --system-symbols QQQ,PSQ   # run-7; run ONLY after the
                                  # last close (or late) row is stamped

Run-6 item 1c additions (report-only; the pass rules below are unchanged):
  * telescoping check — sum(day_pl) over the window vs the equity difference
    between consecutive equity rows; prints the max per-day gap and PASS/FAIL
    against $1 (the contract's validity condition), NOT counted in the verdict
  * the equity BASIS in use — rows carry basis='close' (fixed post-bell stamp)
    or 'intraday'; rows without the field are 'legacy'
  * capture vs extra benchmarks (IWM, QQQ, ...) via --bench-csv SYM=path,
    informational only — SPY remains the contract's capture benchmark

The contract (pre-registered BEFORE the window; do not move goalposts after):
  1. N >= 24 closed trades in the window
  2. expectancy/trade > 0, significant at 95% one-sided (Student t, df=N-1)
  3. max drawdown of daily equity closes > -5%
  4. up-capture > down-capture vs SPY — evaluated ONLY when the window holds
     >= 6 SPY up-days AND >= 6 SPY down-days, else INSUFFICIENT SAMPLE
     (not counted for or against the verdict)
VERDICT: GO only if every counted check passes.  Exit code: 0=GO, 2=NO-GO.

Run-6 item 8a — contract v2 (--contract v2; v1 above stays the default and is
byte-for-byte the run-5 rule set). v2 rules:
  * N >= 24 closed trades is a SAMPLE FLOOR — reported, never pass/fail
  * expectancy is judged POOLED across same-config windows (--pool <prior
    trades.jsonl>, repeatable; every closed row of a pooled file counts, the
    current window contributes its in-window rows). Each window prints N,
    mean, one-sided t and a 95% bootstrap CI; ONLY the pooled test decides,
    and only once pooled N >= 60 (else the verdict is PENDING, exit 3)
  * daily alpha: OLS of daily book return on SPY return over the window —
    alpha/day, its t, beta; the t is flagged as not interpretable under 20
    sessions (reported either way)
  * realized beta within +/-0.2 of --beta-target (default 1.0), judged when
    the OLS has >= 5 sessions. AMENDMENT 2 (run-6, pre-registered 2026-09-09
    before the rule first counted): below 20 sessions the OLS slope is
    unidentifiable (single days dominate; r2 ~ 0), so the rule is graded on
    the MEAN of the stamped ex-ante `book_beta_spy` across the window's
    basis='close' rows instead; the OLS beta is still printed and recorded.
    At >= 20 sessions grading reverts to realized OLS as originally written.
    See runs/pre-final-test-run-6/EVAL_CONTRACT.md Amendment 2.
  * max drawdown > -5% (unchanged)
  * capture judged when >= 4 SPY up-days AND >= 4 down-days (was 6/6)
  * zero decision-sell losses below 0.5x the planned stop (validity check —
    item 2 makes them impossible; any count > 0 means the sell-authority
    gate is not the one running)
Always printed (both contracts): capture vs SPY / QQQ / IWM (from --spy-csv
and --bench-csv), a beta-adjusted capture (book / (beta_exante x index)) using
the per-day `book_beta_spy` field the run-6 close row carries (fallback:
--beta-json risk_state.json's latest book_beta.spy, then --beta-target), and
the equity basis in use.  Exit codes: 0=GO, 2=NO-GO, 3=PENDING (v2/v3),
4=VOID (v3 only: no verdict row for --end — distinct from PENDING so a
runbook step can tell 'run after the close stamp' from 'under-floor').

Run-7 item B3 — contract v3 (--contract v3; v1 and v2 above are untouched and
their outputs stay byte-identical so every recorded run-5/run-6 number still
reproduces). Pre-registered text: runs/pre-final-test-run-7/EVAL_CONTRACT.md.
WHY a v3 — the run-6 verdict day (2026-09-11) showed the v2 checker
  (a) admitted the 12:03 CT basis='intraday' row as a verdict day (OLS/DD/
      capture moved between 12:03 and the 16:28 ET close stamp),
  (b) counted the PSQ hedge_unwind and QQQ core_defense rows in N (14 vs 12),
  (c) dropped the first in-window session (Sep 1: book +0.09% vs SPY -0.69%)
      because it had no in-window predecessor,
  (d) printed 'sessions' where it meant return PAIRS (thresholds bit a day
      late), (e) took the worst day from the broker's day_pl (Alpaca restates
      last_equity overnight: self-consistent worst day was Sep 9, not Sep 10),
  (f) let 3 trips carrying 98.8% of realized read as 'expectancy'.
v3 rules (all thresholds in the V3_* block below):
  * day-0 predecessor = the latest basis='close'/'late' row dated BEFORE
    --start (the reset-day baseline); it is the first point of the equity
    series so day 1 forms a pair. Pairs == sessions from then on.
  * verdict rows are basis='close' rows, plus a basis='late' row on a date
    that has NO close row (the bot was down at the 16:xx tick — crash,
    dead-man restart, the post-window restart itself — and stamped the
    session mark on relaunch; the run-7 writer measures the next close
    row's day_pl against that late row, so the checker must read the same
    predecessor or telescoping FAILs by construction). Intraday/legacy rows
    never enter DD/OLS/capture/telescoping. If the --end date's row is
    neither 'close' nor 'late' the checker prints 'VOID: end-date row is
    basis=<x> — run after the close stamp' and exits 4 WITHOUT a verdict
    (no partial-day numbers to quote). Admitted late rows are listed.
  * rule 1 sample floor: SATELLITE closed trips only — action='sell',
    realized_pl non-null, exit_reason not in V3_SYSTEM_EXIT_REASONS AND
    symbol not in --system-symbols (default QQQ,PSQ = the run-7 CORE_ETF /
    HEDGE_ETF: a core stop or a trailed/stopped hedge leaves as
    bracket_stop/trail, which the exit_reason filter cannot see); the
    excluded system-managed rows are printed on their own line.
  * rule 2 window expectancy: counted once satellite N >= 24 (95% one-sided
    t); bootstrap-95 CI, profit factor, mean ex-top-3 and the equity/options
    split are printed, not counted.
  * rule 3 pooled expectancy (pooled satellite N >= 60): the LIVE-PILOT bar
    only — reported, never a window rule. Pooling is by config fingerprint,
    which the checker cannot verify: it prints a same-config note instead.
  * rule 4 daily alpha: OLS over consecutive close rows including day 1;
    counted at >= 20 pairs as alpha > 0 (t reported, not thresholded).
  * rule 5 beta: mean stamped ex-ante book_beta_spy within +/-0.2 of target
    at >= 5 pairs AND, at >= 20 pairs, the OLS beta's 90% CI
    (beta +/- 1.645 SE) overlaps [target-0.2, target+0.2].
  * rule 6 drawdown > -5% on close-row equity; worst day on delta-equity.
  * rule 7 capture at >= 6 up AND >= 6 down pairs: up > down AND down < 100%.
  * rule 8 zero decision-sell losses below 0.5x planned stop (unchanged).
  * telescoping (validity): uses the row's day_pl, which the run-7 writer
    makes self-consistent (day_pl_basis='self', broker figure kept as
    broker_day_pl); the broker restatement gap is printed as information.
VERDICT: PASS (every counted rule passes and rule 2 is decided) / PENDING
(UNDER-FLOOR: all counted pass, N < 24) / FAIL.  Exit codes: 0=PASS,
2=FAIL, 3=PENDING, 4=VOID (distinct so a runbook step can tell "run again
after the close stamp" from "under-floor").
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRADES_DEFAULT = ROOT / "state" / "trades.jsonl"
EQUITY_DEFAULT = ROOT / "state" / "equity_history.jsonl"

# Contract thresholds — pre-registered, keep in one place.
MIN_TRADES = 24
TELESCOPE_MAX_GAP_USD = 1.0   # validity condition: |Δequity - day_pl| per day
MAX_DD_FLOOR_PCT = -5.0
CAPTURE_MIN_UP_DAYS = 6
CAPTURE_MIN_DOWN_DAYS = 6

# Contract v2 thresholds (run-6 item 8a) — pre-registered in
# runs/pre-final-test-run-6/EVAL_CONTRACT.md; keep in one place.
V2_MIN_TRADES_FLOOR = 24          # reported only, never pass/fail
V2_POOLED_MIN_TRADES = 60         # the pooled expectancy test decides at/after this
V2_MIN_ALPHA_SESSIONS = 20        # alpha t flagged "not interpretable" below this
V2_MIN_BETA_SESSIONS = 5          # realized-beta check needs this many sessions
V2_BETA_TOLERANCE = 0.2           # |realized beta - target| must be <= this
V2_CAPTURE_MIN_UP_DAYS = 4
V2_CAPTURE_MIN_DOWN_DAYS = 4
V2_STOP_FRACTION = 0.5            # a decision-sell loss shallower than this x stop
BOOTSTRAP_RESAMPLES = 2000
BOOTSTRAP_SEED = 20260831         # deterministic: the same ledger prints the same CI
EXIT_GO, EXIT_NO_GO, EXIT_PENDING = 0, 2, 3

# Contract v3 thresholds (run-7 item B3) — pre-registered in
# runs/pre-final-test-run-7/EVAL_CONTRACT.md; keep in one place. v1/v2
# constants above are NOT reused so a v3 edit can never move a v2 number.
V3_MIN_TRADES_FLOOR = 24          # rule 1: satellite N; rule 2 counts at/after it
V3_POOLED_MIN_TRADES = 60         # rule 3: live-pilot bar (reported only)
V3_MIN_ALPHA_PAIRS = 20           # rule 4 counts (alpha > 0) at/after this
V3_MIN_BETA_PAIRS = 5             # rule 5 ex-ante mean counts at/after this
V3_BETA_TOLERANCE = 0.2           # |mean ex-ante beta - target| <= this; CI band
V3_BETA_CI_Z90 = 1.645            # two-sided 90% normal quantile for the OLS beta CI
V3_CAPTURE_MIN_UP_DAYS = 6
V3_CAPTURE_MIN_DOWN_DAYS = 6
V3_CAPTURE_DOWN_MAX_PCT = 100.0   # rule 7: down-capture must be < this
V3_CONCENTRATION_TOP = 3          # 'mean ex-top-3' concentration line
V3_STOP_FRACTION = 0.5            # rule 8 (same as v2)
# exit_reason values the bot actually writes on system-managed rows — exactly
# the literals in investment_strategy/ (orchestrator: hedge_unwind,
# core_defense, regime_trim, defensive_rotate; ledger: correction). They are
# not model round trips: run-6 had PSQ hedge_unwind +$41.76 and QQQ
# core_defense +$27.56 inside "N=14". core_fill / core_trim are NOT exit
# reasons (core_fill is an entry signal) and were dropped at the fix pass.
V3_SYSTEM_EXIT_REASONS = frozenset(
    {"hedge_unwind", "core_defense", "regime_trim", "defensive_rotate",
     "correction"})
# Symbols the system manages regardless of exit_reason (run-7 CORE_ETF /
# HEDGE_ETF): QQQ carries a core stop and PSQ can be trailed/stopped, so those
# rows arrive as bracket_stop / trail and would count as model trips.
# --system-symbols overrides ("" disables).
V3_SYSTEM_SYMBOLS = frozenset({"QQQ", "PSQ"})
V3_DAY0_BASES = ("close", "late")   # a valid day-0 predecessor row
V3_VERDICT_BASES = ("close", "late")  # in-window verdict rows (late only when no close that date)
EXIT_VOID = 4                       # v3: no verdict row for --end — distinct from PENDING

# One-sided 95% (alpha=0.05) Student-t critical values by df. Lookup takes the
# value of the LARGEST table df <= actual df — the critical value shrinks as df
# grows, so rounding df down is always conservative.
_T_CRIT_95_ONE_SIDED = {
    1: 6.314, 2: 2.920, 3: 2.353, 4: 2.132, 5: 2.015, 6: 1.943, 7: 1.895,
    8: 1.860, 9: 1.833, 10: 1.812, 11: 1.796, 12: 1.782, 13: 1.771, 14: 1.761,
    15: 1.753, 16: 1.746, 17: 1.740, 18: 1.734, 19: 1.729, 20: 1.725,
    21: 1.721, 22: 1.717, 23: 1.714, 24: 1.711, 25: 1.708, 26: 1.706,
    27: 1.703, 28: 1.701, 29: 1.699, 30: 1.697, 40: 1.684, 60: 1.671,
    120: 1.658,
}

SPY_CSV_HELP = """\
daily SPY closes, one 'YYYY-MM-DD,close' per line (header lines and blanks are
skipped). Export it from the repo venv — the checker itself never touches the
network:

  .venv/bin/python - <<'PY' > spy.csv
  import datetime as dt
  from dotenv import dotenv_values
  from alpaca.data.historical import StockHistoricalDataClient
  from alpaca.data.requests import StockBarsRequest
  from alpaca.data.timeframe import TimeFrame
  env = dotenv_values(".env")
  c = StockHistoricalDataClient(env["ALPACA_API_KEY"], env["ALPACA_SECRET_KEY"])
  bars = c.get_stock_bars(StockBarsRequest(
      symbol_or_symbols="SPY", timeframe=TimeFrame.Day,
      start=dt.datetime(2026, 8, 20))).data["SPY"]
  print("\\n".join(f"{b.timestamp:%Y-%m-%d},{b.close}" for b in bars))
  PY

Repeat with "IWM" / "QQQ" for --bench-csv IWM=iwm.csv --bench-csv QQQ=qqq.csv
(same read-only bars endpoint; those captures are printed, not judged).

Daily returns are taken between consecutive daily equity closes INSIDE the
window, so the csv only needs to cover the window's dates."""


# ---------------------------------------------------------------- parsing ---

def parse_jsonl(lines) -> list[dict]:
    """Parse jsonl, silently skipping blank/corrupt lines (a live-appended
    ledger can have a torn last line)."""
    rows = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def closed_pls_in_window(trade_rows: list[dict], start: str, end: str) -> list[float]:
    """realized_pl of CLOSED trades (non-null realized_pl) whose ts date falls
    in [start, end]. ISO dates compare lexicographically."""
    pls = []
    for row in trade_rows:
        rp = row.get("realized_pl")
        if rp is None:
            continue
        date = str(row.get("ts") or "")[:10]
        if start <= date <= end:
            pls.append(float(rp))
    return pls


def equity_days_in_window(equity_rows: list[dict], start: str, end: str):
    """[(date, equity, day_pl)] sorted by date; a basis='close' row wins
    over any other row for the same date, else the LAST row per date wins
    (later rows are later snapshots of the same day's close)."""
    by_date: dict[str, tuple] = {}
    is_close: dict[str, bool] = {}
    for row in equity_rows:
        date = str(row.get("date") or "")
        if not (start <= date <= end):
            continue
        try:
            eq = float(row["equity"])
        except (KeyError, TypeError, ValueError):
            continue
        close_row = row.get("basis") == "close"
        if is_close.get(date) and not close_row:
            continue  # the fixed close stamp is final
        day_pl = row.get("day_pl")
        by_date[date] = (eq, None if day_pl is None else float(day_pl))
        is_close[date] = close_row
    return [(d, by_date[d][0], by_date[d][1]) for d in sorted(by_date)]


def equity_basis_summary(equity_rows: list[dict], start: str, end: str) -> dict[str, int]:
    """{basis: row count} for in-window rows; rows without the field are
    'legacy' (pre run-6 writer, re-stamped on every closed tick)."""
    out: dict[str, int] = {}
    for row in equity_rows:
        date = str(row.get("date") or "")
        if not (start <= date <= end):
            continue
        b = str(row.get("basis") or "legacy")
        out[b] = out.get(b, 0) + 1
    return out


def telescoping(days) -> dict:
    """Do the day_pl figures telescope into the equity curve? For each
    consecutive pair of equity rows, gap = |(e1 - e0) - day_pl_1|. Reports
    the max gap and its date, the sum of day_pl vs the end-to-end equity
    difference, and PASS/FAIL against TELESCOPE_MAX_GAP_USD. Pairs where
    day_pl is missing are skipped (counted in 'skipped'). Report-only."""
    max_gap, max_date, n, skipped = 0.0, None, 0, 0
    sum_pl = 0.0
    for (d0, e0, _), (d1, e1, pl1) in zip(days, days[1:]):
        if pl1 is None:
            skipped += 1
            continue
        gap = abs((e1 - e0) - pl1)
        sum_pl += pl1
        n += 1
        if gap > max_gap:
            max_gap, max_date = gap, d1
    eq_diff = (days[-1][1] - days[0][1]) if len(days) >= 2 else 0.0
    return {"pairs": n, "skipped": skipped, "max_gap": max_gap,
            "max_gap_date": max_date, "sum_day_pl": sum_pl,
            "equity_diff": eq_diff,
            "passed": n > 0 and max_gap < TELESCOPE_MAX_GAP_USD}


def parse_spy_csv_lines(lines) -> dict[str, float]:
    """'YYYY-MM-DD,close' per line -> {date: close}; header/blank lines skipped."""
    closes: dict[str, float] = {}
    for line in lines:
        parts = line.strip().split(",")
        if len(parts) < 2:
            continue
        date = parts[0].strip()
        try:
            close = float(parts[1])
        except ValueError:
            continue  # header line
        if len(date) == 10 and date[4] == "-" and date[7] == "-":
            closes[date] = close
    return closes


# ------------------------------------------------------------------ stats ---

def trade_stats(pls: list[float]) -> dict:
    n = len(pls)
    wins = [p for p in pls if p > 0]
    losses = [p for p in pls if p < 0]
    return {
        "n": n,
        "total": sum(pls),
        "wins": len(wins),
        "losses": len(losses),
        "flat": n - len(wins) - len(losses),
        "win_rate": (len(wins) / n) if n else None,
        "avg_win": (sum(wins) / len(wins)) if wins else None,
        "avg_loss": (sum(losses) / len(losses)) if losses else None,
        "expectancy": (sum(pls) / n) if n else None,
    }


def t_crit_one_sided_95(df: int) -> float:
    if df < 1:
        return float("inf")
    crit = _T_CRIT_95_ONE_SIDED[1]
    for k in sorted(_T_CRIT_95_ONE_SIDED):
        if k <= df:
            crit = _T_CRIT_95_ONE_SIDED[k]
    return crit


def t_test_mean_gt_zero(pls: list[float]):
    """One-sided one-sample t-test, H1: mean > 0.
    Returns (t, df, crit, passed); t is None when N < 2 (undefined)."""
    n = len(pls)
    if n < 2:
        return None, max(n - 1, 0), float("inf"), False
    mean = sum(pls) / n
    var = sum((p - mean) ** 2 for p in pls) / (n - 1)
    sd = math.sqrt(var)
    df = n - 1
    crit = t_crit_one_sided_95(df)
    if sd == 0.0:
        t = float("inf") if mean > 0 else (float("-inf") if mean < 0 else 0.0)
    else:
        t = mean / (sd / math.sqrt(n))
    return t, df, crit, t > crit


def max_drawdown(days) -> tuple[float, str, str] | None:
    """Peak-to-trough drawdown (pct, <=0) over daily equity closes.
    Returns (dd_pct, peak_date, trough_date) or None when no days."""
    if not days:
        return None
    peak_eq, peak_date = days[0][1], days[0][0]
    dd, dd_peak, dd_trough = 0.0, days[0][0], days[0][0]
    for date, eq, _ in days:
        if eq > peak_eq:
            peak_eq, peak_date = eq, date
        draw = (eq / peak_eq - 1.0) * 100.0 if peak_eq else 0.0
        if draw < dd:
            dd, dd_peak, dd_trough = draw, peak_date, date
    return dd, dd_peak, dd_trough


def worst_day(days) -> tuple[str, float] | None:
    with_pl = [(d, pl) for d, _, pl in days if pl is not None]
    if not with_pl:
        return None
    return min(with_pl, key=lambda x: x[1])


def capture_vs_spy(days, spy_closes: dict[str, float]) -> dict:
    """Up/down capture of bot daily returns vs SPY daily returns, over
    consecutive in-window equity dates where SPY has closes for both dates."""
    up_bot, up_spy, down_bot, down_spy = [], [], [], []
    for (d0, e0, _), (d1, e1, _) in zip(days, days[1:]):
        if d0 not in spy_closes or d1 not in spy_closes:
            continue
        if not e0 or not spy_closes[d0]:
            continue
        bot_ret = e1 / e0 - 1.0
        spy_ret = spy_closes[d1] / spy_closes[d0] - 1.0
        if spy_ret > 0:
            up_bot.append(bot_ret)
            up_spy.append(spy_ret)
        elif spy_ret < 0:
            down_bot.append(bot_ret)
            down_spy.append(spy_ret)
    up_days, down_days = len(up_spy), len(down_spy)
    qualified = up_days >= CAPTURE_MIN_UP_DAYS and down_days >= CAPTURE_MIN_DOWN_DAYS
    up_capture = (sum(up_bot) / len(up_bot)) / (sum(up_spy) / len(up_spy)) * 100.0 \
        if up_days else None
    down_capture = (sum(down_bot) / len(down_bot)) / (sum(down_spy) / len(down_spy)) * 100.0 \
        if down_days else None
    return {"up_days": up_days, "down_days": down_days, "qualified": qualified,
            "up_capture": up_capture, "down_capture": down_capture}


def bootstrap_ci_mean(pls: list[float], resamples: int = BOOTSTRAP_RESAMPLES,
                      seed: int = BOOTSTRAP_SEED) -> tuple[float, float] | None:
    """95% percentile bootstrap CI of the mean (2.5 / 97.5 pct), seeded so
    the same ledger always prints the same interval. None when N < 2."""
    n = len(pls)
    if n < 2:
        return None
    rng = random.Random(seed)
    means = []
    for _ in range(resamples):
        means.append(sum(rng.choice(pls) for _ in range(n)) / n)
    means.sort()
    lo = means[int(0.025 * (resamples - 1))]
    hi = means[int(0.975 * (resamples - 1))]
    return lo, hi


def all_closed_pls(trade_rows: list[dict]) -> list[float]:
    """realized_pl of every CLOSED row regardless of date (pooled prior
    windows contribute their whole ledger)."""
    return [float(r["realized_pl"]) for r in trade_rows
            if r.get("realized_pl") is not None]


def daily_returns(days) -> list[tuple[str, str, float]]:
    """[(d0, d1, r)] book return between consecutive equity rows."""
    out = []
    for (d0, e0, _), (d1, e1, _) in zip(days, days[1:]):
        if e0:
            out.append((d0, d1, e1 / e0 - 1.0))
    return out


def ols_alpha_beta(days, spy_closes: dict[str, float]) -> dict:
    """Daily alpha block: OLS of book daily return on SPY daily return over
    the window's consecutive equity dates. Returns n, alpha (per day, as a
    fraction), beta, se_alpha, t_alpha, r2, se_beta. n < 3 -> alpha/beta
    None."""
    xs, ys = [], []
    for d0, d1, r in daily_returns(days):
        if d0 in spy_closes and d1 in spy_closes and spy_closes[d0]:
            xs.append(spy_closes[d1] / spy_closes[d0] - 1.0)
            ys.append(r)
    n = len(xs)
    out = {"n": n, "alpha": None, "beta": None, "se_alpha": None,
           "t_alpha": None, "r2": None, "se_beta": None}
    if n < 3:
        return out
    xbar, ybar = sum(xs) / n, sum(ys) / n
    sxx = sum((x - xbar) ** 2 for x in xs)
    if sxx == 0.0:
        return out
    sxy = sum((x - xbar) * (y - ybar) for x, y in zip(xs, ys))
    beta = sxy / sxx
    alpha = ybar - beta * xbar
    sse = sum((y - (alpha + beta * x)) ** 2 for x, y in zip(xs, ys))
    sst = sum((y - ybar) ** 2 for y in ys)
    s2 = sse / (n - 2)
    se_alpha = math.sqrt(s2 * (1.0 / n + xbar ** 2 / sxx))
    t_alpha = (alpha / se_alpha) if se_alpha > 0 else (
        float("inf") if alpha > 0 else float("-inf") if alpha < 0 else 0.0)
    out.update(alpha=alpha, beta=beta, se_alpha=se_alpha, t_alpha=t_alpha,
               r2=(1.0 - sse / sst) if sst else None,
               se_beta=math.sqrt(s2 / sxx))   # v3 rule 5 CI; unused by v1/v2
    return out


def exante_betas(equity_rows: list[dict], start: str, end: str,
                 close_only: bool = False) -> dict[str, float]:
    """{date: book_beta_spy} from the equity rows (the run-6 close row
    carries the cycle's ex-ante SPY beta); a basis='close' row wins.
    `close_only` (v3): only verdict-basis rows are read (close, or late — a
    close row still wins the date); any other basis is ignored outright."""
    out: dict[str, float] = {}
    is_close: dict[str, bool] = {}
    for row in equity_rows:
        date = str(row.get("date") or "")
        if not (start <= date <= end):
            continue
        b = row.get("book_beta_spy")
        if b is None:
            continue
        close_row = row.get("basis") == "close"
        if close_only and row.get("basis") not in V3_VERDICT_BASES:
            continue
        if is_close.get(date) and not close_row:
            continue
        try:
            out[date] = float(b)
        except (TypeError, ValueError):
            continue
        is_close[date] = close_row
    return out


def capture_beta_adjusted(days, idx_closes: dict[str, float],
                          betas: dict[str, float], fallback: float,
                          min_up: int = CAPTURE_MIN_UP_DAYS,
                          min_down: int = CAPTURE_MIN_DOWN_DAYS) -> dict:
    """Up/down capture of the book vs (beta_exante x index return). The
    ex-ante beta for the return d0->d1 is the reading stamped on d0's close
    row (known BEFORE the day), else `fallback`. Adds 'beta_days' = how many
    days used a stamped reading (vs the fallback)."""
    up_bot, up_idx, down_bot, down_idx, stamped = [], [], [], [], 0
    for (d0, e0, _), (d1, e1, _) in zip(days, days[1:]):
        if d0 not in idx_closes or d1 not in idx_closes:
            continue
        if not e0 or not idx_closes[d0]:
            continue
        beta = betas.get(d0)
        if beta is None:
            beta = fallback
        else:
            stamped += 1
        idx_ret = idx_closes[d1] / idx_closes[d0] - 1.0
        bot_ret = e1 / e0 - 1.0
        if idx_ret > 0:
            up_bot.append(bot_ret)
            up_idx.append(beta * idx_ret)
        elif idx_ret < 0:
            down_bot.append(bot_ret)
            down_idx.append(beta * idx_ret)
    up_days, down_days = len(up_idx), len(down_idx)

    def _cap(b, i):
        if not i:
            return None
        denom = sum(i) / len(i)
        return None if denom == 0 else (sum(b) / len(b)) / denom * 100.0
    return {"up_days": up_days, "down_days": down_days,
            "qualified": up_days >= min_up and down_days >= min_down,
            "up_capture": _cap(up_bot, up_idx),
            "down_capture": _cap(down_bot, down_idx),
            "beta_days": stamped, "fallback": fallback}


def decision_sell_losses_below_stop(trade_rows: list[dict], start: str, end: str,
                                    fraction: float = V2_STOP_FRACTION) -> dict:
    """Validity check (v2): decision-sells that closed a LOSS shallower than
    `fraction` x the planned stop. Sell rows carry realized_pl_pct but no
    stop width, so each is joined to the latest prior BUY row of the same
    symbol with stop_loss_pct > 0. Returns count, n_decision_losses,
    unknown_stop (no joinable stop) and the offending rows."""
    rows = sorted(
        (r for r in trade_rows if r.get("ts")), key=lambda r: str(r["ts"]))
    last_stop: dict[str, float] = {}
    hits, n_losses, unknown = [], 0, 0
    for r in rows:
        sym = str(r.get("symbol") or "")
        action = str(r.get("action") or "").lower()
        if action == "buy":
            try:
                w = float(r.get("stop_loss_pct") or 0.0)
            except (TypeError, ValueError):
                w = 0.0
            if w > 0:
                last_stop[sym] = w
            continue
        if action != "sell" or str(r.get("exit_reason") or "") != "decision":
            continue
        date = str(r["ts"])[:10]
        if not (start <= date <= end):
            continue
        pct = r.get("realized_pl_pct")
        if pct is None or float(pct) >= 0:
            continue
        n_losses += 1
        stop = last_stop.get(sym)
        if not stop:
            unknown += 1
            continue
        if abs(float(pct)) < fraction * stop:
            hits.append((date, sym, float(pct), stop))
    return {"count": len(hits), "n_decision_losses": n_losses,
            "unknown_stop": unknown, "rows": hits}


def _fmt_t(t) -> str:
    return "n/a" if t is None else f"{t:.3f}"


# ------------------------------------------------------------- v3 pieces ---
# Everything below is used ONLY by --contract v3. v1/v2 keep calling the
# functions above with their original signatures.

def satellite_closed_in_window(trade_rows: list[dict], start: str, end: str,
                               system_symbols=None,
                               ) -> tuple[list[dict], list[dict]]:
    """v3 rule 1: (satellite_rows, excluded_rows) among CLOSED rows in
    [start, end]. Closed = non-null realized_pl and action 'sell' (rows
    with no action field — old fixtures — still count). A closed row whose
    exit_reason is in V3_SYSTEM_EXIT_REASONS, or whose symbol is in
    `system_symbols` (None = no symbol filter; the CLI passes
    --system-symbols, default V3_SYSTEM_SYMBOLS), is a system-managed exit
    (hedge / core sleeve) and goes to `excluded`, never into N."""
    syms = frozenset(str(x).upper() for x in (system_symbols or ()))
    sat, excl = [], []
    for row in trade_rows:
        if row.get("realized_pl") is None:
            continue
        action = row.get("action")
        if action is not None and str(action).lower() != "sell":
            continue
        date = str(row.get("ts") or "")[:10]
        if not (start <= date <= end):
            continue
        reason = str(row.get("exit_reason") or "")
        sym = str(row.get("symbol") or "").upper()
        system = reason in V3_SYSTEM_EXIT_REASONS or sym in syms
        (excl if system else sat).append(row)
    return sat, excl


def all_satellite_pls(trade_rows: list[dict], system_symbols=None) -> list[float]:
    """realized_pl of every satellite closed row regardless of date (a
    pooled prior same-config ledger contributes its whole file, minus the
    system-managed exits, exactly as the current window does)."""
    sat, _ = satellite_closed_in_window(trade_rows, "0000-00-00", "9999-99-99",
                                        system_symbols)
    return [float(r["realized_pl"]) for r in sat]


# Run-7 4a-18: phantom / duplicate SELL rows. A dict-level MIRROR of
# investment_strategy.ledger.dedup_sells (this script stays import-free so it
# runs on archived ledgers from any checkout); tests/test_ledger.py pins the
# two rules to the same dropped set on the AVAV / BIIB / T fixtures.
#   negative_qty   qty < 0 (AVAV 2026-07-07 flatten qty=-37 +$365.04, a
#                  snapshot that read the position short after the Jul-7
#                  bracket double-fill).
#   replaced_dupe  a LATER sell on the same (symbol, instrument, exit_reason)
#                  with the same qty within PHANTOM_DUPE_WINDOW_H whose
#                  realized_pl matches to the cent (T 2026-07-23 option
#                  flatten -$2,700 x2, an expired DAY close resubmitted) or
#                  differs by exactly qty x the exit_price delta (AVAV
#                  2026-07-08 trail 37 sh re-replaced 36 s later, +$212.01 ->
#                  +$213.98 = 37 x $0.0532): the EARLIER row is the phantom.
#                  A row with a broker fill stamped (fill_price) is never one.
# qty == 0 / missing is KEPT (legacy "full close, size unknown"). Rows whose
# ts cannot be parsed are kept. Applied by the v3 report to the window ledger
# and to every --pool file; --show-dropped lists each dropped row.
PHANTOM_DUPE_WINDOW_H = 4.0


def _row_ts(row: dict):
    """UTC-aware datetime of a row's ts, or None when unparseable."""
    from datetime import datetime, timezone
    raw = str(row.get("ts") or "").strip()
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)


def drop_phantom_sells(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """(kept rows in file order, dropped rows) — each dropped row is a copy
    carrying `_phantom_rule` and, for a dupe, `_kept_order_id`."""
    from datetime import timedelta
    if not rows:
        return [], []
    stamped = [(i, r, _row_ts(r)) for i, r in enumerate(rows)]
    order = sorted(stamped, key=lambda t: (t[2] is None, t[2] or 0, t[0]))
    window = timedelta(hours=PHANTOM_DUPE_WINDOW_H)
    dropped: dict[int, dict] = {}
    last_kept: dict[tuple, tuple[int, dict, object]] = {}
    for i, r, ts in order:
        if str(r.get("action") or "sell").lower() != "sell":
            continue
        qty = r.get("qty")
        try:
            qty = float(qty) if qty is not None else None
        except (TypeError, ValueError):
            qty = None
        if qty is not None and qty < 0:
            dropped[i] = dict(r, _phantom_rule="negative_qty")
            continue
        key = (str(r.get("symbol") or "").upper(),
               str(r.get("instrument") or "equity").lower(),
               str(r.get("exit_reason") or ""))
        prev = last_kept.get(key)
        if prev is not None and ts is not None:
            j, p, pts = prev
            pq = p.get("qty")
            try:
                pq = float(pq) if pq is not None else None
            except (TypeError, ValueError):
                pq = None
            if (
                pts is not None and qty is not None and pq is not None
                and pq > 0 and abs(pq - qty) < 1e-6
                and float(p.get("fill_price") or 0) <= 0
                and timedelta(0) <= (ts - pts) <= window
                and _remark_dupe(p, r, qty)
            ):
                dropped[j] = dict(p, _phantom_rule="replaced_dupe",
                                  _kept_order_id=r.get("order_id"))
        last_kept[key] = (i, r, ts)
    kept = [r for i, r in enumerate(rows) if i not in dropped]
    return kept, [dropped[i] for i in sorted(dropped)]


def _remark_dupe(earlier: dict, later: dict, qty: float) -> bool:
    e_pl, l_pl = earlier.get("realized_pl"), later.get("realized_pl")
    if e_pl is None or l_pl is None:
        return False
    d_pl = float(l_pl) - float(e_pl)
    if abs(d_pl) < 0.005:
        return True
    e_px, l_px = earlier.get("exit_price"), later.get("exit_price")
    if e_px and l_px and float(e_px) > 0 and float(l_px) > 0:
        return abs(d_pl - qty * (float(l_px) - float(e_px))) < 0.01
    return False


def _phantom_desc(r: dict) -> str:
    pl = r.get("realized_pl")
    kept = f" (exit carried by order {r['_kept_order_id']})" if r.get("_kept_order_id") else ""
    return (f"{str(r.get('ts') or '')[:16]} {r.get('symbol')} "
            f"{r.get('instrument') or 'equity'} {r.get('exit_reason') or '-'} "
            f"qty={r.get('qty')} {_money(pl) if pl is not None else '$n/a'} "
            f"[{r.get('_phantom_rule')}]{kept}")


def print_phantoms(label: str, dropped: list[dict], show: bool) -> None:
    """One summary line per ledger (always) + one line per row (--show-dropped)."""
    total = sum(float(r.get("realized_pl") or 0.0) for r in dropped)
    rules = {}
    for r in dropped:
        rules[r["_phantom_rule"]] = rules.get(r["_phantom_rule"], 0) + 1
    detail = " ".join(f"{k}={v}" for k, v in sorted(rules.items())) or "-"
    print(f"phantom/dupe sell rows dropped ({label}): n={len(dropped)} "
          f"sum={_money(total)} [{detail}]"
          + ("" if show or not dropped else "  (--show-dropped lists them)"))
    if show:
        for r in dropped:
            print(f"  dropped: {_phantom_desc(r)}")


def profit_factor(pls: list[float]) -> float | None:
    """sum(wins) / |sum(losses)|; inf when there are wins and no losses;
    None when the sample is empty or has neither."""
    wins = sum(p for p in pls if p > 0)
    losses = -sum(p for p in pls if p < 0)
    if losses > 0:
        return wins / losses
    if wins > 0:
        return float("inf")
    return None


def concentration(pls: list[float], top: int = V3_CONCENTRATION_TOP) -> dict:
    """How much of the realized sum sits in the `top` largest trips, and the
    mean of the rest (run-6: 3 trips were 98.8% of +$26,963; ex-top-3 mean
    $29.54). `mean_ex_top` is None when N <= top (nothing left to average)."""
    ordered = sorted(pls, reverse=True)
    top_sum = sum(ordered[:top])
    rest = ordered[top:]
    total = sum(pls)
    return {"top": top, "top_sum": top_sum,
            "top_share": (top_sum / total) if total else None,
            "mean_ex_top": (sum(rest) / len(rest)) if rest else None,
            "n_rest": len(rest)}


def instrument_split(rows: list[dict]) -> dict[str, list[float]]:
    """{'equity': [...], 'option': [...]} realized_pl by the row's
    `instrument` field (missing field -> equity)."""
    out: dict[str, list[float]] = {"equity": [], "option": []}
    for r in rows:
        kind = "option" if str(r.get("instrument") or "").lower() == "option" else "equity"
        out[kind].append(float(r["realized_pl"]))
    return out


def _row_equity(row: dict) -> float | None:
    try:
        return float(row["equity"])
    except (KeyError, TypeError, ValueError):
        return None


def day0_row(equity_rows: list[dict], start: str) -> dict | None:
    """v3 day-0 predecessor: the latest row dated strictly BEFORE `start`
    whose basis is 'close' or 'late' (the reset-day baseline written by
    fresh_cycle/flatten is basis='late'). For one date a 'close' row beats a
    'late' row. Intraday/legacy rows are never a predecessor. None when the
    file has no such row (the first close row then starts the series)."""
    best: dict | None = None
    for row in equity_rows:
        date = str(row.get("date") or "")
        if not date or date >= start or row.get("basis") not in V3_DAY0_BASES:
            continue
        if _row_equity(row) is None:
            continue
        if best is None or date > str(best["date"]) or (
                date == str(best["date"]) and row.get("basis") == "close"
                and best.get("basis") != "close"):
            best = row
    return best


def close_rows_by_date(equity_rows: list[dict], start: str, end: str) -> dict[str, dict]:
    """{date: row} of in-window verdict rows: basis='close' rows (the last
    close row per date wins), plus a basis='late' row for a date with NO
    close row — the session mark the writer stamps on relaunch after a
    missed 16:xx tick, and the predecessor the NEXT close row's day_pl was
    measured against (status.EquityHistory._prior_close_row uses the same
    close/late rule). Every other basis is dropped."""
    out: dict[str, dict] = {}
    for row in equity_rows:
        date = str(row.get("date") or "")
        basis = row.get("basis")
        if not (start <= date <= end) or basis not in V3_VERDICT_BASES:
            continue
        if _row_equity(row) is None:
            continue
        if basis == "late" and out.get(date, {}).get("basis") == "close":
            continue                      # a close row for the date already wins
        out[date] = row
    return out


def end_row_basis(equity_rows: list[dict], end: str) -> str | None:
    """Basis of the --end date's verdict row: 'close' if any close row is
    stamped for that date, else 'late' if a late row is, else the LAST
    row's basis ('legacy' when the field is missing), else None when no row
    exists for the date."""
    last: str | None = None
    late = False
    for row in equity_rows:
        if str(row.get("date") or "") != end:
            continue
        b = str(row.get("basis") or "legacy")
        if b == "close":
            return "close"
        if b == "late":
            late = True
        last = b
    return "late" if late else last


def verdict_days_v3(equity_rows: list[dict], start: str, end: str):
    """(days, day0, close_rows): `days` = [(date, equity, day_pl)] starting
    with the day-0 predecessor (day_pl None — it is a baseline, never a
    session) followed by the in-window close rows in date order."""
    d0 = day0_row(equity_rows, start)
    close_rows = close_rows_by_date(equity_rows, start, end)
    days = []
    if d0 is not None:
        days.append((str(d0["date"]), _row_equity(d0), None))
    for date in sorted(close_rows):
        row = close_rows[date]
        pl = row.get("day_pl")
        days.append((date, _row_equity(row), None if pl is None else float(pl)))
    return days, d0, close_rows


def worst_day_delta(days) -> tuple[str, float] | None:
    """Worst day on delta-equity between consecutive verdict rows (self-
    consistent by construction; the broker's day_pl is restated overnight)."""
    deltas = [(d1, e1 - e0) for (_, e0, _), (d1, e1, _) in zip(days, days[1:])]
    return min(deltas, key=lambda x: x[1]) if deltas else None


def day_pl_basis_counts(close_rows: dict[str, dict]) -> dict[str, int]:
    """{'self': n, 'broker': n} — how many close rows carry a self-consistent
    day_pl (run-7 writer) vs the broker figure (run-6 rows have no
    day_pl_basis field and their day_pl IS the broker figure)."""
    out = {"self": 0, "broker": 0}
    for row in close_rows.values():
        out["self" if row.get("day_pl_basis") == "self" else "broker"] += 1
    return out


def broker_restatement(days, close_rows: dict[str, dict]) -> dict:
    """Informational: max |delta-equity - broker day_pl| over consecutive
    verdict rows, where the broker figure is `broker_day_pl` (run-7 rows)
    or `day_pl` when day_pl_basis != 'self' (run-6 rows). Rows with no
    broker figure are skipped."""
    max_gap, max_date, n = 0.0, None, 0
    for (_, e0, _), (d1, e1, _) in zip(days, days[1:]):
        row = close_rows.get(d1) or {}
        broker = row.get("broker_day_pl")
        if broker is None and row.get("day_pl_basis") != "self":
            broker = row.get("day_pl")
        if broker is None:
            continue
        gap = abs((e1 - e0) - float(broker))
        n += 1
        if gap > max_gap:
            max_gap, max_date = gap, d1
    return {"rows": n, "max_gap": max_gap, "max_gap_date": max_date}


def beta_ci90(ols: dict) -> tuple[float, float] | None:
    """OLS beta +/- 1.645 x SE(beta); None when the slope is undefined."""
    if ols.get("beta") is None or ols.get("se_beta") is None:
        return None
    return (ols["beta"] - V3_BETA_CI_Z90 * ols["se_beta"],
            ols["beta"] + V3_BETA_CI_Z90 * ols["se_beta"])


def _interval_overlaps(ci: tuple[float, float], lo: float, hi: float) -> bool:
    return ci[0] <= hi and ci[1] >= lo


# ----------------------------------------------------------------- report ---

def _money(v) -> str:
    return "n/a" if v is None else f"${v:,.2f}"


def render_report(start: str, end: str, trade_rows, equity_rows,
                  spy_closes: dict[str, float] | None = None,
                  bench_closes: dict[str, dict[str, float]] | None = None,
                  contract: str = "v1",
                  pool: list[tuple[str, list[dict]]] | None = None,
                  beta_target: float = 1.0,
                  beta_fallback: float | None = None,
                  system_symbols=None, show_dropped: bool = False) -> int:
    """Print the full report and return the exit code (EXIT_GO / EXIT_NO_GO /
    EXIT_PENDING; v3 also EXIT_VOID). `bench_closes` ({SYM: {date: close}})
    adds capture lines per benchmark. `contract` picks the rule set ('v1' =
    run-5, 'v2' = run-6, 'v3' = run-7). `pool` = [(label, trade_rows)] prior
    same-config windows for the v2/v3 pooled expectancy test. `beta_fallback`
    = ex-ante beta used for days without a stamped `book_beta_spy` (default:
    beta_target). `system_symbols` (v3 only; v1/v2 ignore it) = symbols whose
    closed rows are system-managed regardless of exit_reason (None = the
    V3_SYSTEM_SYMBOLS default; an empty set disables the symbol filter)."""
    if contract == "v3":
        return render_report_v3(start, end, trade_rows, equity_rows,
                                spy_closes=spy_closes, bench_closes=bench_closes,
                                pool=pool, beta_target=beta_target,
                                beta_fallback=beta_fallback,
                                system_symbols=system_symbols,
                                show_dropped=show_dropped)
    label = "pre-final-test-run-6 (v2)" if contract == "v2" else "pre-final-test-run-5"
    print(f"=== EVAL-CONTRACT {label} | window {start}..{end} | contract {contract} ===")

    # (1) closed trades
    pls = closed_pls_in_window(trade_rows, start, end)
    st = trade_stats(pls)
    print("--- (1) closed trades ---")
    print(f"closed trades (realized_pl non-null, ts in window): N={st['n']}")
    print(f"sum realized P&L: {_money(st['total'])}")
    wr = "n/a" if st["win_rate"] is None else f"{st['win_rate'] * 100:.1f}%"
    print(f"win rate: {wr} ({st['wins']}W / {st['losses']}L / {st['flat']} flat)")
    print(f"avg win: {_money(st['avg_win'])}   avg loss: {_money(st['avg_loss'])}")
    print(f"expectancy/trade: {_money(st['expectancy'])}")

    # (2) t-test
    t, df, crit, t_pass = t_test_mean_gt_zero(pls)
    print("--- (2) one-sided 95% t-test (H1: mean realized_pl > 0) ---")
    if t is None:
        print(f"t-stat: n/a (N={st['n']} < 2 — cannot test)")
    else:
        print(f"t-stat: {t:.3f}  df={df}  crit(95%, one-sided)={crit:.3f}")
    print(f"result: {'PASS (t > crit)' if t_pass else 'FAIL (t <= crit)'}")

    # (2b) pooled expectancy across same-config windows (v2 decides on it)
    print("--- (2b) expectancy pooled across same-config windows ---")
    windows: list[tuple[str, list[float]]] = [(f"this window {start}..{end}", pls)]
    for plabel, prows in (pool or []):
        windows.append((plabel, all_closed_pls(prows)))
    pooled: list[float] = []
    for wlabel, wpls in windows:
        wt, wdf, wcrit, _ = t_test_mean_gt_zero(wpls)
        ci = bootstrap_ci_mean(wpls)
        mean = (sum(wpls) / len(wpls)) if wpls else None
        ci_s = "n/a" if ci is None else f"[{_money(ci[0])}, {_money(ci[1])}]"
        print(f"{wlabel}: N={len(wpls)} mean={_money(mean)} t={_fmt_t(wt)} "
              f"(df={wdf}) bootstrap95={ci_s}")
        pooled.extend(wpls)
    pt, pdf, pcrit, p_pass = t_test_mean_gt_zero(pooled)
    pci = bootstrap_ci_mean(pooled)
    pmean = (sum(pooled) / len(pooled)) if pooled else None
    pooled_decided = len(pooled) >= V2_POOLED_MIN_TRADES
    print(f"POOLED: N={len(pooled)} ({len(windows)} window(s)) mean={_money(pmean)} "
          f"t={_fmt_t(pt)} crit={pcrit if pcrit != float('inf') else 'n/a'} "
          f"bootstrap95="
          + ("n/a" if pci is None else f"[{_money(pci[0])}, {_money(pci[1])}]")
          + f"  -> {'DECIDES' if pooled_decided else 'PENDING'} "
          f"(needs N >= {V2_POOLED_MIN_TRADES})")

    # (3) drawdown
    days = equity_days_in_window(equity_rows, start, end)
    dd = max_drawdown(days)
    wd = worst_day(days)
    print("--- (3) drawdown (daily equity closes in window) ---")
    if dd is None:
        print("max drawdown: n/a (no equity days in window)")
    else:
        print(f"max drawdown: {dd[0]:.2f}% (peak {dd[1]} -> trough {dd[2]}, "
              f"{len(days)} days)")
    if wd is None:
        print("worst day: n/a (no day_pl in window)")
    else:
        print(f"worst day: {wd[0]} day_pl {_money(wd[1])}")
    basis = equity_basis_summary(equity_rows, start, end)
    print("equity basis: " + (", ".join(
        f"{k}={v} row(s)" for k, v in sorted(basis.items())) or "n/a")
        + ("  (close rows win per date)" if "close" in basis else ""))
    tel = telescoping(days)
    print("--- (3b) telescoping: sum(day_pl) vs equity difference (validity, "
          "not a verdict check) ---")
    if tel["pairs"] == 0:
        print("telescoping: n/a (< 2 equity rows with day_pl)")
    else:
        print(f"sum day_pl: {_money(tel['sum_day_pl'])}   equity diff "
              f"(first->last row): {_money(tel['equity_diff'])}   "
              f"pairs={tel['pairs']} skipped={tel['skipped']}")
        print(f"max per-day gap |Δequity - day_pl|: {_money(tel['max_gap'])} "
              f"on {tel['max_gap_date']}  -> "
              f"{'PASS' if tel['passed'] else 'FAIL'} (limit "
              f"${TELESCOPE_MAX_GAP_USD:.2f})")

    # (4) capture
    min_up = V2_CAPTURE_MIN_UP_DAYS if contract == "v2" else CAPTURE_MIN_UP_DAYS
    min_down = V2_CAPTURE_MIN_DOWN_DAYS if contract == "v2" else CAPTURE_MIN_DOWN_DAYS
    print("--- (4) up/down capture vs SPY ---")
    cap = None
    if spy_closes is None:
        print("skipped: no --spy-csv provided (capture not counted in verdict)")
    else:
        cap = capture_vs_spy(days, spy_closes)
        cap["qualified"] = cap["up_days"] >= min_up and cap["down_days"] >= min_down
        print(f"SPY days in window: {cap['up_days']} up / {cap['down_days']} down")
        if cap["qualified"]:
            print(f"up-capture: {cap['up_capture']:.1f}%   "
                  f"down-capture: {cap['down_capture']:.1f}%")
        else:
            print(f"INSUFFICIENT SAMPLE: need >={min_up} up AND "
                  f">={min_down} down SPY days "
                  f"(got {cap['up_days']} up / {cap['down_days']} down)")

    for sym, closes in sorted((bench_closes or {}).items()):
        c = capture_vs_spy(days, closes)
        print(f"--- (4b) capture vs {sym} (informational, not judged) ---")
        print(f"{sym} days in window: {c['up_days']} up / {c['down_days']} down")
        if c["up_capture"] is not None and c["down_capture"] is not None:
            print(f"up-capture: {c['up_capture']:.1f}%   "
                  f"down-capture: {c['down_capture']:.1f}%"
                  + ("" if c["qualified"] else "   (INSUFFICIENT SAMPLE)"))
        else:
            print("capture: n/a (no overlapping up/down days)")

    # (4c) daily alpha: OLS of book daily return on SPY
    print("--- (4c) daily alpha (OLS book return ~ SPY return, per session) ---")
    ols = None
    if spy_closes is None:
        print("skipped: no --spy-csv")
    else:
        ols = ols_alpha_beta(days, spy_closes)
        if ols["alpha"] is None:
            print(f"alpha: n/a ({ols['n']} sessions < 3)")
        else:
            flag = ("" if ols["n"] >= V2_MIN_ALPHA_SESSIONS
                    else f"  (< {V2_MIN_ALPHA_SESSIONS} sessions: t NOT interpretable)")
            print(f"sessions={ols['n']}  alpha/day={ols['alpha'] * 100:+.3f}%  "
                  f"t(alpha)={ols['t_alpha']:.2f}  beta={ols['beta']:.2f}  "
                  f"r2={ols['r2']:.2f}{flag}" if ols["r2"] is not None else
                  f"sessions={ols['n']}  alpha/day={ols['alpha'] * 100:+.3f}%  "
                  f"t(alpha)={ols['t_alpha']:.2f}  beta={ols['beta']:.2f}{flag}")
            print(f"realized beta vs target {beta_target:.2f} +/- {V2_BETA_TOLERANCE:.1f}: "
                  f"{'WITHIN' if abs(ols['beta'] - beta_target) <= V2_BETA_TOLERANCE else 'OUTSIDE'}")

    # (4d) beta-adjusted capture: book / (beta_exante x index)
    betas = exante_betas(equity_rows, start, end)
    fb = beta_target if beta_fallback is None else beta_fallback
    print("--- (4d) beta-adjusted capture: book / (beta_exante x index) "
          "(informational) ---")
    print(f"ex-ante beta source: {len(betas)} stamped close row(s) "
          f"(book_beta_spy); fallback beta={fb:.2f} for the rest")
    all_idx = dict(bench_closes or {})
    if spy_closes is not None:
        all_idx = {"SPY": spy_closes, **all_idx}
    if not all_idx:
        print("skipped: no index closes")
    for sym, closes in sorted(all_idx.items()):
        c = capture_beta_adjusted(days, closes, betas, fb, min_up, min_down)
        if c["up_capture"] is None or c["down_capture"] is None:
            print(f"{sym}: n/a (no overlapping up/down days)")
        else:
            print(f"{sym}: up-capture {c['up_capture']:.1f}%   down-capture "
                  f"{c['down_capture']:.1f}%   ({c['up_days']} up / "
                  f"{c['down_days']} down, {c['beta_days']} stamped-beta days)"
                  + ("" if c["qualified"] else "   (INSUFFICIENT SAMPLE)"))

    # (4e) decision-sell validity: losses cut shallower than 0.5x the stop
    dsl = decision_sell_losses_below_stop(trade_rows, start, end)
    print(f"--- (4e) decision-sell losses below {V2_STOP_FRACTION:.1f}x planned stop "
          "(validity) ---")
    print(f"decision-sell losses in window: {dsl['n_decision_losses']}  "
          f"below {V2_STOP_FRACTION:.1f}x stop: {dsl['count']}  "
          f"no joinable stop: {dsl['unknown_stop']}")
    for date, sym, pct, stop in dsl["rows"]:
        print(f"  {date} {sym} realized {pct:+.2f}% vs stop {stop:.2f}% "
              f"({abs(pct) / stop:.2f}x)")

    # (5) verdict
    if contract == "v2":
        return _verdict_v2(st, pooled, pooled_decided, pt, pcrit, pdf, p_pass,
                           dd, cap, ols, beta_target, dsl, betas)
    print("--- (5) verdict: pre-final-test-run-5 contract ---")
    checks = [
        (f"closed trades N >= {MIN_TRADES}", st["n"] >= MIN_TRADES,
         f"N={st['n']}"),
        ("expectancy > 0 @ 95% one-sided", t_pass,
         "no t-stat" if t is None else f"t={t:.3f} vs crit={crit:.3f}, df={df}"),
        (f"max drawdown > {MAX_DD_FLOOR_PCT:.0f}%",
         dd is not None and dd[0] > MAX_DD_FLOOR_PCT,
         "no equity data" if dd is None else f"{dd[0]:.2f}%"),
    ]
    for label, ok, detail in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {label}  ({detail})")
    if cap is not None and cap["qualified"]:
        cap_ok = cap["up_capture"] > cap["down_capture"]
        checks.append(("capture", cap_ok, ""))
        print(f"[{'PASS' if cap_ok else 'FAIL'}] up-capture > down-capture  "
              f"(up {cap['up_capture']:.1f}% vs down {cap['down_capture']:.1f}%)")
    elif cap is not None:
        print(f"[N/A ] capture: INSUFFICIENT SAMPLE "
              f"({cap['up_days']} up / {cap['down_days']} down) — not counted")
    else:
        print("[N/A ] capture: no --spy-csv — not counted")
    verdict = all(ok for _, ok, _ in checks)
    print(f"VERDICT: {'GO' if verdict else 'NO-GO'}")
    return EXIT_GO if verdict else EXIT_NO_GO


def _verdict_v2(st, pooled, pooled_decided, pt, pcrit, pdf, p_pass,
                dd, cap, ols, beta_target, dsl,
                exante: dict[str, float] | None = None) -> int:
    """Contract v2 verdict (run-6). Counted checks: pooled expectancy (only
    once pooled N >= 60 — else PENDING), max DD, realized beta vs target
    (when >= V2_MIN_BETA_SESSIONS sessions; below V2_MIN_ALPHA_SESSIONS it is
    graded on the mean stamped ex-ante book_beta_spy per Amendment 2, with the
    OLS beta recorded), capture (when qualified), zero decision-sell losses
    below 0.5x stop. N >= 24 is a floor, reported. `exante` = {date:
    book_beta_spy} from the window's close rows (Amendment 2 input)."""
    print("--- (5) verdict: pre-final-test-run-6 contract v2 ---")
    n = st["n"]
    print(f"[INFO] sample floor N >= {V2_MIN_TRADES_FLOOR}: N={n} "
          f"({'met' if n >= V2_MIN_TRADES_FLOOR else 'NOT met — extend the window; not a fail'})")
    checks: list[tuple[str, bool]] = []
    if pooled_decided:
        checks.append(("pooled expectancy", p_pass))
        print(f"[{'PASS' if p_pass else 'FAIL'}] pooled expectancy > 0 @ 95% one-sided  "
              f"(pooled N={len(pooled)}, t={_fmt_t(pt)} vs crit={pcrit:.3f}, df={pdf})")
    else:
        print(f"[PEND] pooled expectancy: pooled N={len(pooled)} < "
              f"{V2_POOLED_MIN_TRADES} — undecided; pool the next same-config window")
    dd_ok = dd is not None and dd[0] > MAX_DD_FLOOR_PCT
    checks.append(("max drawdown", dd_ok))
    print(f"[{'PASS' if dd_ok else 'FAIL'}] max drawdown > {MAX_DD_FLOOR_PCT:.0f}%  "
          f"({'no equity data' if dd is None else f'{dd[0]:.2f}%'})")
    if ols is not None and ols["beta"] is not None and ols["n"] >= V2_MIN_BETA_SESSIONS:
        if ols["n"] >= V2_MIN_ALPHA_SESSIONS:
            b_ok = abs(ols["beta"] - beta_target) <= V2_BETA_TOLERANCE
            checks.append(("realized beta", b_ok))
            print(f"[{'PASS' if b_ok else 'FAIL'}] realized beta within +/-{V2_BETA_TOLERANCE:.1f} "
                  f"of {beta_target:.2f}  (beta={ols['beta']:.2f} over {ols['n']} sessions)")
        elif exante:
            # Amendment 2 (pre-registered 2026-09-09, before the rule first
            # counted): under V2_MIN_ALPHA_SESSIONS the OLS slope is dominated
            # by single days, so grade the hedge on the mean stamped ex-ante
            # book_beta_spy; the OLS beta stays printed and recorded.
            vals = sorted(exante.values())
            mean_b = sum(vals) / len(vals)
            b_ok = abs(mean_b - beta_target) <= V2_BETA_TOLERANCE
            checks.append(("realized beta (Amend-2 ex-ante)", b_ok))
            print(f"[{'PASS' if b_ok else 'FAIL'}] beta rule (Amendment 2, "
                  f"{ols['n']} sessions < {V2_MIN_ALPHA_SESSIONS}): mean ex-ante "
                  f"book_beta_spy={mean_b:.2f} over {len(vals)} stamped close row(s) "
                  f"(range {vals[0]:.2f}..{vals[-1]:.2f}) within +/-"
                  f"{V2_BETA_TOLERANCE:.1f} of {beta_target:.2f}")
            print(f"[INFO] realized OLS beta={ols['beta']:.2f} "
                  f"(r2={ols['r2']:.2f}) recorded, NOT counted below "
                  f"{V2_MIN_ALPHA_SESSIONS} sessions (Amendment 2)"
                  if ols["r2"] is not None else
                  f"[INFO] realized OLS beta={ols['beta']:.2f} recorded, NOT "
                  f"counted below {V2_MIN_ALPHA_SESSIONS} sessions (Amendment 2)")
        else:
            print(f"[N/A ] beta rule (Amendment 2): {ols['n']} sessions < "
                  f"{V2_MIN_ALPHA_SESSIONS} and NO stamped book_beta_spy close "
                  f"rows to grade on — not counted "
                  f"(OLS beta={ols['beta']:.2f} recorded)")
        note = ("" if ols["n"] >= V2_MIN_ALPHA_SESSIONS
                else f" — < {V2_MIN_ALPHA_SESSIONS} sessions, not interpretable")
        print(f"[INFO] daily alpha {ols['alpha'] * 100:+.3f}%/day, t={ols['t_alpha']:.2f} "
              f"over {ols['n']} sessions{note}")
    else:
        why = ("no --spy-csv" if ols is None else
               f"{ols['n']} sessions < {V2_MIN_BETA_SESSIONS}")
        print(f"[N/A ] realized beta / daily alpha: {why} — not counted")
    if cap is not None and cap["qualified"]:
        cap_ok = cap["up_capture"] > cap["down_capture"]
        checks.append(("capture", cap_ok))
        print(f"[{'PASS' if cap_ok else 'FAIL'}] up-capture > down-capture  "
              f"(up {cap['up_capture']:.1f}% vs down {cap['down_capture']:.1f}%)")
    elif cap is not None:
        print(f"[N/A ] capture: INSUFFICIENT SAMPLE ({cap['up_days']} up / "
              f"{cap['down_days']} down; need {V2_CAPTURE_MIN_UP_DAYS}/"
              f"{V2_CAPTURE_MIN_DOWN_DAYS}) — not counted")
    else:
        print("[N/A ] capture: no --spy-csv — not counted")
    ds_ok = dsl["count"] == 0
    checks.append(("decision-sell validity", ds_ok))
    print(f"[{'PASS' if ds_ok else 'FAIL'}] zero decision-sell losses below "
          f"{V2_STOP_FRACTION:.1f}x stop  (count={dsl['count']}, "
          f"unknown stop={dsl['unknown_stop']})")
    all_ok = all(ok for _, ok in checks)
    if not all_ok:
        print("VERDICT: NO-GO")
        return EXIT_NO_GO
    if not pooled_decided:
        print("VERDICT: PENDING (every counted check passes; expectancy undecided "
              f"until pooled N >= {V2_POOLED_MIN_TRADES})")
        return EXIT_PENDING
    print("VERDICT: GO")
    return EXIT_GO


# ------------------------------------------------------------- v3 report ---

def _pf_str(pf) -> str:
    return "n/a" if pf is None else ("inf (no losses)" if pf == float("inf") else f"{pf:.2f}")


def _v3_row_desc(r: dict) -> str:
    return (f"{str(r.get('ts') or '')[:10]} {r.get('symbol')} "
            f"{r.get('exit_reason') or '-'} {float(r['realized_pl']):+,.2f}")


def render_report_v3(start: str, end: str, trade_rows, equity_rows,
                     spy_closes: dict[str, float] | None = None,
                     bench_closes: dict[str, dict[str, float]] | None = None,
                     pool: list[tuple[str, list[dict]]] | None = None,
                     beta_target: float = 1.0,
                     beta_fallback: float | None = None,
                     system_symbols=None, show_dropped: bool = False) -> int:
    """Contract v3 report + verdict (run-7). Same inputs as render_report;
    see the module docstring for the rule set. Prints 'pairs', never
    'sessions'. Returns EXIT_GO (PASS) / EXIT_NO_GO (FAIL) / EXIT_PENDING
    (UNDER-FLOOR) / EXIT_VOID (no close/late verdict row for --end: no
    verdict; exit 4, never confused with PENDING's 3). `system_symbols`:
    None = V3_SYSTEM_SYMBOLS, empty = no symbol filter."""
    print(f"=== EVAL-CONTRACT pre-final-test-run-7 (v3) | window {start}..{end} "
          f"| contract v3 ===")

    # (0) verdict-row guard FIRST: refuse to print partial-day numbers that
    # could be quoted (run-6 Amendment 2 quoted an intraday OLS reading).
    end_basis = end_row_basis(equity_rows, end)
    if end_basis not in V3_VERDICT_BASES:
        shown = "none (no row for that date)" if end_basis is None else end_basis
        print(f"VOID: end-date row is basis={shown} — run after the close stamp "
              f"(verdict rows are basis='close', or 'late' on a date with no "
              f"close row; no verdict printed)")
        return EXIT_VOID
    syms = (V3_SYSTEM_SYMBOLS if system_symbols is None
            else frozenset(str(x).upper() for x in system_symbols))

    days, d0, close_rows = verdict_days_v3(equity_rows, start, end)
    if d0 is not None:
        print(f"day-0 predecessor: {d0['date']} {_money(_row_equity(d0))} "
              f"(basis={d0.get('basis')}) — first point of the series; "
              f"day 1 = {min(close_rows) if close_rows else 'n/a'} forms a pair")
    else:
        print("day-0 predecessor: NONE before --start (no close/late row) — "
              "the series starts at the first in-window close row; the first "
              "in-window day forms no pair")

    # (1) satellite closed trips — on the phantom-free ledger (4a-18): the
    # same rows TradeLedger.effective() drops are dropped here, and listed.
    trade_rows, dropped = drop_phantom_sells(trade_rows)
    pool = [(plabel, drop_phantom_sells(prows)) for plabel, prows in (pool or [])]
    sat_rows, excl_rows = satellite_closed_in_window(trade_rows, start, end, syms)
    pls = [float(r["realized_pl"]) for r in sat_rows]
    st = trade_stats(pls)
    print("--- (1) satellite closed trips (rule 1: sample floor) ---")
    print(f"satellite closed trips (action=sell, realized_pl non-null, exit_reason "
          f"and symbol not system-managed, ts in window): N={st['n']}")
    reasons = "/".join(sorted(V3_SYSTEM_EXIT_REASONS))
    label = (f"exit_reason {reasons}; symbols "
             + ("/".join(sorted(syms)) if syms else "none"))
    if excl_rows:
        print(f"excluded system-managed rows ({label}): n={len(excl_rows)} "
              f"sum={_money(sum(float(r['realized_pl']) for r in excl_rows))}  "
              f"[{'; '.join(_v3_row_desc(r) for r in excl_rows)}]")
    else:
        print(f"excluded system-managed rows ({label}): n=0")
    print(f"sum realized P&L: {_money(st['total'])}")
    wr = "n/a" if st["win_rate"] is None else f"{st['win_rate'] * 100:.1f}%"
    print(f"win rate: {wr} ({st['wins']}W / {st['losses']}L / {st['flat']} flat)")
    print(f"avg win: {_money(st['avg_win'])}   avg loss: {_money(st['avg_loss'])}")
    print(f"expectancy/trade: {_money(st['expectancy'])}")
    print_phantoms("this ledger", dropped, show_dropped)
    for plabel, (_prows, pdropped) in pool:
        print_phantoms(plabel, pdropped, show_dropped)

    # (2) window expectancy
    t, df, crit, t_pass = t_test_mean_gt_zero(pls)
    n = st["n"]
    rule2_decided = n >= V3_MIN_TRADES_FLOOR
    print(f"--- (2) window expectancy (rule 2: one-sided 95% t, H1: mean > 0; "
          f"counted at N >= {V3_MIN_TRADES_FLOOR}) ---")
    if t is None:
        print(f"t-stat: n/a (N={n} < 2 — cannot test)")
    else:
        print(f"t-stat: {t:.3f}  df={df}  crit(95%, one-sided)={crit:.3f}")
    print(f"result: {'PASS (t > crit)' if t_pass else 'FAIL (t <= crit)'}"
          + ("" if rule2_decided else
             f"  [NOT COUNTED: N={n} < {V3_MIN_TRADES_FLOOR}]"))
    ci = bootstrap_ci_mean(pls)
    conc = concentration(pls)
    split = instrument_split(sat_rows)
    print("bootstrap95=" + ("n/a" if ci is None else f"[{_money(ci[0])}, {_money(ci[1])}]")
          + f"   profit factor={_pf_str(profit_factor(pls))}")
    share = ("n/a" if conc["top_share"] is None else f"{conc['top_share'] * 100:.1f}%")
    print(f"concentration: top-{conc['top']} trips sum={_money(conc['top_sum'])} "
          f"({share} of realized)   mean ex-top-{conc['top']}="
          f"{_money(conc['mean_ex_top'])} over {conc['n_rest']} trip(s)")
    for kind in ("equity", "option"):
        k = split[kind]
        kmean = (sum(k) / len(k)) if k else None
        print(f"{kind}-only: N={len(k)} sum={_money(sum(k))} mean={_money(kmean)}")

    # (3) pooled expectancy — live-pilot bar only
    print(f"--- (3) pooled expectancy (rule 3: live-pilot bar, pooled satellite "
          f"N >= {V3_POOLED_MIN_TRADES}; never a window rule) ---")
    windows: list[tuple[str, list[float]]] = [(f"this window {start}..{end}", pls)]
    for plabel, (prows, _pdropped) in pool:
        windows.append((plabel, all_satellite_pls(prows, syms)))
    pooled: list[float] = []
    for wlabel, wpls in windows:
        wt, wdf, wcrit, _ = t_test_mean_gt_zero(wpls)
        wci = bootstrap_ci_mean(wpls)
        mean = (sum(wpls) / len(wpls)) if wpls else None
        ci_s = "n/a" if wci is None else f"[{_money(wci[0])}, {_money(wci[1])}]"
        print(f"{wlabel}: N={len(wpls)} mean={_money(mean)} t={_fmt_t(wt)} "
              f"(df={wdf}) bootstrap95={ci_s}")
        pooled.extend(wpls)
    pt, pdf, pcrit, p_pass = t_test_mean_gt_zero(pooled)
    pooled_decided = len(pooled) >= V3_POOLED_MIN_TRADES
    pmean = (sum(pooled) / len(pooled)) if pooled else None
    print(f"POOLED: N={len(pooled)} ({len(windows)} window(s)) mean={_money(pmean)} "
          f"t={_fmt_t(pt)} crit={pcrit if pcrit != float('inf') else 'n/a'}"
          f"  -> {'DECIDES' if pooled_decided else 'PENDING'} "
          f"(needs N >= {V3_POOLED_MIN_TRADES})")
    print("[NOTE] same-config: pooling is valid only across windows with an "
          "IDENTICAL config fingerprint (merge SHA + sorted .env STRATEGY KEYS "
          "+ adaptive loops off, per docs/RUN7_SWITCH.md); the checker does "
          "not verify fingerprints — every --pool file is the operator's "
          "assertion of same-config")

    # (4) equity series: verdict rows
    basis = equity_basis_summary(equity_rows, start, end)
    late_admitted = sorted(d for d, r in close_rows.items() if r.get("basis") == "late")
    n_close = len(close_rows) - len(late_admitted)
    print("--- (4) equity series (verdict rows = basis='close', or 'late' on a "
          "date with no close row; + day-0 predecessor) ---")
    print(f"verdict rows: {n_close} close row(s)"
          + (f" + {len(late_admitted)} late row(s)" if late_admitted else "")
          + " in window"
          + (" + day-0" if d0 is not None else "")
          + f" = {len(days)} point(s), {max(len(days) - 1, 0)} pair(s)")
    # Every in-window row that is not a verdict row is excluded from every
    # rule: intraday/legacy always, and a late row on a date that also has a
    # close row (the close row wins that date).
    non_close = {k: v for k, v in basis.items() if k not in V3_VERDICT_BASES}
    late_dropped = basis.get("late", 0) - len(late_admitted)
    if late_dropped > 0:
        non_close["late"] = late_dropped
    print("equity basis: " + (", ".join(
        f"{k}={v} row(s)" for k, v in sorted(basis.items())) or "n/a")
        + (f"  (late rows admitted as verdict rows — no close row that date: "
           + ", ".join(late_admitted) + ")" if late_admitted else "")
        + (f"  (non-close rows excluded from every rule: "
           + ", ".join(f"{k}={v}" for k, v in sorted(non_close.items())) + ")"
           if non_close else ""))
    dd = max_drawdown(days)
    if dd is None:
        print("max drawdown: n/a (no verdict rows)")
    else:
        print(f"max drawdown: {dd[0]:.2f}% (peak {dd[1]} -> trough {dd[2]}, "
              f"{len(days)} point(s))")
    wd = worst_day_delta(days)
    if wd is None:
        print("worst day: n/a (< 2 verdict rows)")
    else:
        brow = close_rows.get(wd[0]) or {}
        broker = brow.get("broker_day_pl")
        if broker is None and brow.get("day_pl_basis") != "self":
            broker = brow.get("day_pl")
        print(f"worst day (delta-equity, self-consistent): {wd[0]} {_money(wd[1])}"
              f"   broker day_pl that date: {_money(None if broker is None else float(broker))}")
    tel = telescoping(days)
    dpb = day_pl_basis_counts(close_rows)
    print("--- (4b) telescoping: sum(day_pl) vs equity difference (validity, "
          "not a verdict rule) ---")
    print(f"day_pl basis on close rows: self-consistent={dpb['self']} "
          f"broker={dpb['broker']}")
    if tel["pairs"] == 0:
        print("telescoping: n/a (< 2 verdict rows with day_pl)")
    else:
        print(f"sum day_pl: {_money(tel['sum_day_pl'])}   equity diff "
              f"(day-0->last row): {_money(tel['equity_diff'])}   "
              f"pairs={tel['pairs']} skipped={tel['skipped']}")
        print(f"max per-day gap |delta-equity - day_pl|: {_money(tel['max_gap'])} "
              f"on {tel['max_gap_date']}  -> "
              f"{'PASS' if tel['passed'] else 'FAIL'} (limit "
              f"${TELESCOPE_MAX_GAP_USD:.2f})")
    br = broker_restatement(days, close_rows)
    if br["rows"]:
        print(f"broker last_equity restatement (informational): max "
              f"|delta-equity - broker_day_pl| {_money(br['max_gap'])} on "
              f"{br['max_gap_date']} ({br['rows']} row(s))")
    else:
        print("broker last_equity restatement (informational): n/a (no broker figure)")

    # (5) capture
    print(f"--- (5) up/down capture vs SPY (rule 7: counted at >= "
          f"{V3_CAPTURE_MIN_UP_DAYS} up AND >= {V3_CAPTURE_MIN_DOWN_DAYS} down pairs) ---")
    cap = None
    if spy_closes is None:
        print("skipped: no --spy-csv provided (capture not counted in verdict)")
    else:
        cap = capture_vs_spy(days, spy_closes)
        cap["qualified"] = (cap["up_days"] >= V3_CAPTURE_MIN_UP_DAYS
                            and cap["down_days"] >= V3_CAPTURE_MIN_DOWN_DAYS)
        print(f"SPY pairs in window: {cap['up_days']} up / {cap['down_days']} down")
        if cap["up_capture"] is not None and cap["down_capture"] is not None:
            print(f"up-capture: {cap['up_capture']:.1f}%   "
                  f"down-capture: {cap['down_capture']:.1f}%"
                  + ("" if cap["qualified"] else "   (INSUFFICIENT SAMPLE)"))
        if not cap["qualified"]:
            print(f"INSUFFICIENT SAMPLE: need >={V3_CAPTURE_MIN_UP_DAYS} up AND "
                  f">={V3_CAPTURE_MIN_DOWN_DAYS} down SPY pairs "
                  f"(got {cap['up_days']} up / {cap['down_days']} down)")
    for sym, closes in sorted((bench_closes or {}).items()):
        c = capture_vs_spy(days, closes)
        print(f"--- (5b) capture vs {sym} (informational, not judged) ---")
        print(f"{sym} pairs in window: {c['up_days']} up / {c['down_days']} down")
        if c["up_capture"] is not None and c["down_capture"] is not None:
            print(f"up-capture: {c['up_capture']:.1f}%   "
                  f"down-capture: {c['down_capture']:.1f}%"
                  + ("" if c["qualified"] else "   (INSUFFICIENT SAMPLE)"))
        else:
            print("capture: n/a (no overlapping up/down pairs)")

    # (6) daily alpha / beta
    print("--- (6) daily alpha & beta (rules 4-5: OLS book return ~ SPY return "
          "over consecutive close rows incl. day 1) ---")
    ols = None
    ci_b = None
    if spy_closes is None:
        print("skipped: no --spy-csv")
    else:
        ols = ols_alpha_beta(days, spy_closes)
        if ols["alpha"] is None:
            print(f"alpha: n/a ({ols['n']} pairs < 3)")
        else:
            ci_b = beta_ci90(ols)
            r2 = "n/a" if ols["r2"] is None else f"{ols['r2']:.2f}"
            print(f"pairs={ols['n']}  alpha/day={ols['alpha'] * 100:+.3f}%  "
                  f"SE(alpha)={ols['se_alpha'] * 100:.3f}%  t(alpha)={ols['t_alpha']:.2f}  "
                  f"r2={r2}"
                  + ("" if ols["n"] >= V3_MIN_ALPHA_PAIRS else
                     f"  (< {V3_MIN_ALPHA_PAIRS} pairs: reported, not counted)"))
            print(f"beta={ols['beta']:.2f}  SE(beta)={ols['se_beta']:.2f}  "
                  f"90% CI=[{ci_b[0]:.2f}, {ci_b[1]:.2f}]  band=[{beta_target - V3_BETA_TOLERANCE:.2f}, "
                  f"{beta_target + V3_BETA_TOLERANCE:.2f}]  -> "
                  f"{'OVERLAPS' if _interval_overlaps(ci_b, beta_target - V3_BETA_TOLERANCE, beta_target + V3_BETA_TOLERANCE) else 'NO OVERLAP'}"
                  + ("" if ols["n"] >= V3_MIN_ALPHA_PAIRS else
                     f"  (< {V3_MIN_ALPHA_PAIRS} pairs: reported, not counted)"))
    betas = exante_betas(equity_rows, start, end, close_only=True)
    if betas:
        vals = sorted(betas.values())
        mean_b = sum(vals) / len(vals)
        print(f"mean ex-ante book_beta_spy={mean_b:.2f} over {len(vals)} stamped "
              f"close row(s) (range {vals[0]:.2f}..{vals[-1]:.2f}) vs target "
              f"{beta_target:.2f} +/- {V3_BETA_TOLERANCE:.1f}: "
              f"{'WITHIN' if abs(mean_b - beta_target) <= V3_BETA_TOLERANCE else 'OUTSIDE'}")
    else:
        mean_b = None
        print("mean ex-ante book_beta_spy: n/a (no stamped close rows)")

    # (6b) beta-adjusted capture
    fb = beta_target if beta_fallback is None else beta_fallback
    print("--- (6b) beta-adjusted capture: book / (beta_exante x index) "
          "(informational) ---")
    print(f"ex-ante beta source: {len(betas)} stamped close row(s) "
          f"(book_beta_spy); fallback beta={fb:.2f} for the rest")
    all_idx = dict(bench_closes or {})
    if spy_closes is not None:
        all_idx = {"SPY": spy_closes, **all_idx}
    if not all_idx:
        print("skipped: no index closes")
    for sym, closes in sorted(all_idx.items()):
        c = capture_beta_adjusted(days, closes, betas, fb,
                                  V3_CAPTURE_MIN_UP_DAYS, V3_CAPTURE_MIN_DOWN_DAYS)
        if c["up_capture"] is None or c["down_capture"] is None:
            print(f"{sym}: n/a (no overlapping up/down pairs)")
        else:
            print(f"{sym}: up-capture {c['up_capture']:.1f}%   down-capture "
                  f"{c['down_capture']:.1f}%   ({c['up_days']} up / "
                  f"{c['down_days']} down, {c['beta_days']} stamped-beta pairs)"
                  + ("" if c["qualified"] else "   (INSUFFICIENT SAMPLE)"))

    # (7) decision-sell validity
    dsl = decision_sell_losses_below_stop(trade_rows, start, end, V3_STOP_FRACTION)
    print(f"--- (7) decision-sell losses below {V3_STOP_FRACTION:.1f}x planned stop "
          "(rule 8) ---")
    print(f"decision-sell losses in window: {dsl['n_decision_losses']}  "
          f"below {V3_STOP_FRACTION:.1f}x stop: {dsl['count']}  "
          f"no joinable stop: {dsl['unknown_stop']}")
    for date, sym, pct, stop in dsl["rows"]:
        print(f"  {date} {sym} realized {pct:+.2f}% vs stop {stop:.2f}% "
              f"({abs(pct) / stop:.2f}x)")

    # (8) verdict
    print("--- (8) verdict: pre-final-test-run-7 contract v3 ---")
    checks: list[tuple[str, bool]] = []
    print(f"[INFO] rule 1 sample floor N >= {V3_MIN_TRADES_FLOOR}: N={n} "
          f"({'met' if rule2_decided else 'UNDER-FLOOR — drives the extension only; not a fail'})")
    if rule2_decided:
        checks.append(("window expectancy", t_pass))
        print(f"[{'PASS' if t_pass else 'FAIL'}] rule 2 window expectancy > 0 @ 95% "
              f"one-sided  (N={n}, t={_fmt_t(t)} vs crit={crit:.3f}, df={df})")
    else:
        print(f"[PEND] rule 2 window expectancy: N={n} < {V3_MIN_TRADES_FLOOR} — "
              f"undecided (t={_fmt_t(t)} recorded)")
    if pooled_decided:
        print(f"[INFO] rule 3 live-pilot bar: pooled N={len(pooled)} >= "
              f"{V3_POOLED_MIN_TRADES}, t={_fmt_t(pt)} vs crit={pcrit:.3f} -> "
              f"{'PASS' if p_pass else 'FAIL'} (not a window rule)")
    else:
        print(f"[INFO] rule 3 live-pilot bar: pooled N={len(pooled)} < "
              f"{V3_POOLED_MIN_TRADES} — PENDING (not a window rule)")
    pairs = 0 if ols is None else ols["n"]
    if ols is not None and ols["alpha"] is not None and pairs >= V3_MIN_ALPHA_PAIRS:
        a_ok = ols["alpha"] > 0
        checks.append(("daily alpha", a_ok))
        print(f"[{'PASS' if a_ok else 'FAIL'}] rule 4 daily alpha > 0  "
              f"(alpha/day={ols['alpha'] * 100:+.3f}%, t={ols['t_alpha']:.2f}, "
              f"{pairs} pairs)")
    else:
        why = ("no --spy-csv" if ols is None else
               f"{pairs} pairs < {V3_MIN_ALPHA_PAIRS}")
        print(f"[N/A ] rule 4 daily alpha: {why} — not counted"
              + ("" if ols is None or ols["alpha"] is None else
                 f" (alpha/day={ols['alpha'] * 100:+.3f}%, t={ols['t_alpha']:.2f} recorded)"))
    if pairs >= V3_MIN_BETA_PAIRS and mean_b is not None:
        b_ok = abs(mean_b - beta_target) <= V3_BETA_TOLERANCE
        checks.append(("beta ex-ante mean", b_ok))
        print(f"[{'PASS' if b_ok else 'FAIL'}] rule 5a mean ex-ante book_beta_spy "
              f"within +/-{V3_BETA_TOLERANCE:.1f} of {beta_target:.2f}  "
              f"(mean={mean_b:.2f} over {len(betas)} close row(s), {pairs} pairs)")
    elif pairs >= V3_MIN_BETA_PAIRS:
        print(f"[N/A ] rule 5a beta ex-ante mean: {pairs} pairs but NO stamped "
              f"book_beta_spy close rows — not counted")
    else:
        print(f"[N/A ] rule 5a beta ex-ante mean: {pairs} pairs < "
              f"{V3_MIN_BETA_PAIRS} — not counted")
    if pairs >= V3_MIN_ALPHA_PAIRS and ci_b is not None:
        lo, hi = beta_target - V3_BETA_TOLERANCE, beta_target + V3_BETA_TOLERANCE
        c_ok = _interval_overlaps(ci_b, lo, hi)
        checks.append(("beta OLS CI", c_ok))
        print(f"[{'PASS' if c_ok else 'FAIL'}] rule 5b OLS beta 90% CI "
              f"[{ci_b[0]:.2f}, {ci_b[1]:.2f}] overlaps [{lo:.2f}, {hi:.2f}]  "
              f"(beta={ols['beta']:.2f}, {pairs} pairs)")
    else:
        print(f"[N/A ] rule 5b OLS beta CI: {pairs} pairs < {V3_MIN_ALPHA_PAIRS} "
              f"— not counted"
              + ("" if ci_b is None else
                 f" (90% CI [{ci_b[0]:.2f}, {ci_b[1]:.2f}] recorded)"))
    dd_ok = dd is not None and dd[0] > MAX_DD_FLOOR_PCT
    checks.append(("max drawdown", dd_ok))
    print(f"[{'PASS' if dd_ok else 'FAIL'}] rule 6 max drawdown > {MAX_DD_FLOOR_PCT:.0f}%  "
          f"({'no equity data' if dd is None else f'{dd[0]:.2f}%'})")
    if cap is not None and cap["qualified"]:
        cap_ok = (cap["up_capture"] > cap["down_capture"]
                  and cap["down_capture"] < V3_CAPTURE_DOWN_MAX_PCT)
        checks.append(("capture", cap_ok))
        print(f"[{'PASS' if cap_ok else 'FAIL'}] rule 7 up-capture > down-capture "
              f"AND down-capture < {V3_CAPTURE_DOWN_MAX_PCT:.0f}%  "
              f"(up {cap['up_capture']:.1f}% vs down {cap['down_capture']:.1f}%, "
              f"{cap['up_days']} up / {cap['down_days']} down pairs)")
    elif cap is not None:
        print(f"[N/A ] rule 7 capture: INSUFFICIENT SAMPLE ({cap['up_days']} up / "
              f"{cap['down_days']} down pairs; need {V3_CAPTURE_MIN_UP_DAYS}/"
              f"{V3_CAPTURE_MIN_DOWN_DAYS}) — not counted")
    else:
        print("[N/A ] rule 7 capture: no --spy-csv — not counted")
    ds_ok = dsl["count"] == 0
    checks.append(("decision-sell validity", ds_ok))
    print(f"[{'PASS' if ds_ok else 'FAIL'}] rule 8 zero decision-sell losses below "
          f"{V3_STOP_FRACTION:.1f}x stop  (count={dsl['count']}, "
          f"unknown stop={dsl['unknown_stop']})")
    if tel["pairs"] and not tel["passed"]:
        print(f"[WARN] validity: telescoping FAIL (max gap {_money(tel['max_gap'])} "
              f"on {tel['max_gap_date']}) — CONFOUNDED unless disclosed-and-"
              f"accepted in an amendment before the last close row; not part "
              f"of the exit code")
    all_ok = all(ok for _, ok in checks)
    if not all_ok:
        print("VERDICT: FAIL  (" + ", ".join(f"{k}" for k, ok in checks if not ok) + ")")
        return EXIT_NO_GO
    if not rule2_decided:
        print(f"VERDICT: PENDING (UNDER-FLOOR: every counted rule passes; N={n} < "
              f"{V3_MIN_TRADES_FLOOR} so rule 2 is undecided)")
        return EXIT_PENDING
    print("VERDICT: PASS")
    return EXIT_GO


# --------------------------------------------------------------- selftest ---

_FIXTURE_TRADES = [
    # 6 closed in-window (2026-08-10..2026-08-14): +10 +20 -5 +15 -10 +30
    '{"ts":"2026-08-10T14:00:00Z","symbol":"AAA","realized_pl":10.0}',
    '{"ts":"2026-08-10T15:00:00Z","symbol":"BBB","realized_pl":20.0}',
    '{"ts":"2026-08-11T14:00:00Z","symbol":"CCC","realized_pl":-5.0}',
    '{"ts":"2026-08-12T14:00:00Z","symbol":"DDD","realized_pl":15.0}',
    '{"ts":"2026-08-13T14:00:00Z","symbol":"EEE","realized_pl":-10.0}',
    '{"ts":"2026-08-14T14:00:00Z","symbol":"FFF","realized_pl":30.0}',
    # excluded: closed but OUTSIDE the window
    '{"ts":"2026-08-01T14:00:00Z","symbol":"OLD","realized_pl":999.0}',
    # excluded: in-window but still OPEN (realized_pl null)
    '{"ts":"2026-08-12T15:00:00Z","symbol":"OPN","realized_pl":null}',
    # excluded: torn line (live-appended ledger)
    '{"ts":"2026-08-12T16:00:00Z","symbol":"TORN","realized',
]
_FIXTURE_EQUITY = [
    '{"date":"2026-08-10","equity":100000.0,"day_pl":500.0}',
    '{"date":"2026-08-11","equity":102000.0,"day_pl":2000.0}',
    '{"date":"2026-08-12","equity":99960.0,"day_pl":-2040.0}',
    '{"date":"2026-08-13","equity":101000.0,"day_pl":1040.0}',
]
_FIXTURE_SPY = [
    "date,close",  # header must be skipped
    "2026-08-10,640.0",
    "2026-08-11,645.0",
    "2026-08-12,641.0",
    "2026-08-13,646.0",
]


def selftest() -> int:
    """Verify the checker against a tiny embedded fixture. Prints SELFTEST
    PASS and exits 0, or raises AssertionError."""
    start, end = "2026-08-10", "2026-08-14"
    trades = parse_jsonl(_FIXTURE_TRADES)
    pls = closed_pls_in_window(trades, start, end)
    assert pls == [10.0, 20.0, -5.0, 15.0, -10.0, 30.0], pls

    st = trade_stats(pls)
    assert st["n"] == 6 and abs(st["total"] - 60.0) < 1e-9
    assert st["wins"] == 4 and st["losses"] == 2 and st["flat"] == 0
    assert abs(st["win_rate"] - 4 / 6) < 1e-9
    assert abs(st["avg_win"] - 18.75) < 1e-9
    assert abs(st["avg_loss"] - (-7.5)) < 1e-9
    assert abs(st["expectancy"] - 10.0) < 1e-9

    t, df, crit, t_pass = t_test_mean_gt_zero(pls)
    # mean=10, sd=sqrt(230), t = 10 / (sqrt(230)/sqrt(6)) = 1.61515...
    assert df == 5 and abs(crit - 2.015) < 1e-9
    assert abs(t - 10.0 / (math.sqrt(230.0) / math.sqrt(6.0))) < 1e-12
    assert abs(t - 1.61515) < 1e-4 and not t_pass
    # degenerate cases
    assert t_test_mean_gt_zero([]) == (None, 0, float("inf"), False)
    assert t_test_mean_gt_zero([5.0]) == (None, 0, float("inf"), False)
    t0, _, _, p0 = t_test_mean_gt_zero([3.0, 3.0, 3.0])   # sd=0, mean>0
    assert t0 == float("inf") and p0
    # conservative df lookup
    assert t_crit_one_sided_95(35) == _T_CRIT_95_ONE_SIDED[30]
    assert t_crit_one_sided_95(500) == _T_CRIT_95_ONE_SIDED[120]

    days = equity_days_in_window(parse_jsonl(_FIXTURE_EQUITY), start, end)
    assert [d for d, _, _ in days] == ["2026-08-10", "2026-08-11",
                                       "2026-08-12", "2026-08-13"]
    dd = max_drawdown(days)
    assert dd is not None and abs(dd[0] - (-2.0)) < 1e-9
    assert dd[1] == "2026-08-11" and dd[2] == "2026-08-12"
    wd = worst_day(days)
    assert wd == ("2026-08-12", -2040.0)
    tel = telescoping(days)
    assert tel["pairs"] == 3 and tel["passed"] and tel["max_gap"] < 1e-9
    assert abs(tel["sum_day_pl"] - 1000.0) < 1e-9
    assert abs(tel["equity_diff"] - 1000.0) < 1e-9
    # a re-stamped (after-hours) row breaks telescoping and is reported
    broken = days[:2] + [("2026-08-12", 99960.0 + 346.0, -2040.0)] + days[3:]
    tel2 = telescoping(broken)
    assert not tel2["passed"] and abs(tel2["max_gap"] - 346.0) < 1e-9
    assert tel2["max_gap_date"] == "2026-08-12"
    # basis: a 'close' row beats a later legacy/intraday row for the same date
    mixed = parse_jsonl(_FIXTURE_EQUITY + [
        '{"date":"2026-08-13","equity":101500.0,"day_pl":1540.0,"basis":"close"}',
        '{"date":"2026-08-13","equity":101900.0,"day_pl":1940.0,"basis":"intraday"}',
    ])
    mdays = equity_days_in_window(mixed, start, end)
    assert mdays[-1] == ("2026-08-13", 101500.0, 1540.0)
    assert equity_basis_summary(mixed, start, end) == {
        "legacy": 4, "close": 1, "intraday": 1}

    # capture: 3 return days -> INSUFFICIENT SAMPLE
    spy = parse_spy_csv_lines(_FIXTURE_SPY)
    assert len(spy) == 4  # header skipped
    cap = capture_vs_spy(days, spy)
    assert cap["up_days"] == 2 and cap["down_days"] == 1 and not cap["qualified"]

    # capture math on a qualified synthetic series: SPY alternates +1%/-1%
    # (6 up, 6 down), bot moves exactly HALF of SPY each day -> 50%/50%.
    dates = [f"2026-09-{i:02d}" for i in range(1, 14)]
    spy_px, bot_eq = 100.0, 100000.0
    spy2, days2 = {dates[0]: spy_px}, [(dates[0], bot_eq, None)]
    for i, d in enumerate(dates[1:]):
        r = 0.01 if i % 2 == 0 else -0.01
        spy_px *= 1.0 + r
        bot_eq *= 1.0 + r / 2.0
        spy2[d] = spy_px
        days2.append((d, bot_eq, None))
    cap2 = capture_vs_spy(days2, spy2)
    assert cap2["qualified"] and cap2["up_days"] == 6 and cap2["down_days"] == 6
    assert abs(cap2["up_capture"] - 50.0) < 1e-6
    assert abs(cap2["down_capture"] - 50.0) < 1e-6

    # v2 pieces (run-6 item 8a)
    ci = bootstrap_ci_mean(pls)
    assert ci is not None and ci[0] <= 10.0 <= ci[1]
    assert bootstrap_ci_mean(pls) == ci            # seeded -> deterministic
    assert bootstrap_ci_mean([1.0]) is None
    # OLS: bot = 0.5 x SPY exactly -> beta 0.5, alpha 0, r2 1
    o = ols_alpha_beta(days2, spy2)
    assert o["n"] == 12 and abs(o["beta"] - 0.5) < 1e-9
    assert abs(o["alpha"]) < 1e-12 and abs(o["r2"] - 1.0) < 1e-9
    assert ols_alpha_beta(days[:2], spy)["alpha"] is None
    # beta-adjusted capture: with beta_exante 0.5 stamped, 0.5x SPY -> 100%
    stamped = {d: 0.5 for d in dates}
    cb = capture_beta_adjusted(days2, spy2, stamped, 1.0)
    assert cb["beta_days"] == 12 and abs(cb["up_capture"] - 100.0) < 1e-6
    assert abs(cb["down_capture"] - 100.0) < 1e-6
    cb_fb = capture_beta_adjusted(days2, spy2, {}, 1.0)
    assert cb_fb["beta_days"] == 0 and abs(cb_fb["up_capture"] - 50.0) < 1e-6
    # ex-ante betas: close row wins over a later intraday row
    eb = exante_betas(parse_jsonl([
        '{"date":"2026-08-10","equity":1,"book_beta_spy":1.3,"basis":"close"}',
        '{"date":"2026-08-10","equity":1,"book_beta_spy":0.9,"basis":"intraday"}',
        '{"date":"2026-08-11","equity":1}',
    ]), start, end)
    assert eb == {"2026-08-10": 1.3}
    # decision-sell validity: a -1.5% decision sell against a 5% stop is 0.3x
    dsl = decision_sell_losses_below_stop(parse_jsonl([
        '{"ts":"2026-08-10T14:00:00Z","symbol":"AAA","action":"buy","stop_loss_pct":5.0}',
        '{"ts":"2026-08-11T14:00:00Z","symbol":"AAA","action":"sell","exit_reason":"decision","realized_pl_pct":-1.5,"realized_pl":-15}',
        '{"ts":"2026-08-11T15:00:00Z","symbol":"BBB","action":"sell","exit_reason":"decision","realized_pl_pct":-1.5,"realized_pl":-15}',
        '{"ts":"2026-08-12T14:00:00Z","symbol":"AAA","action":"sell","exit_reason":"stop","realized_pl_pct":-5.0,"realized_pl":-50}',
    ]), start, end)
    assert dsl["count"] == 1 and dsl["n_decision_losses"] == 2 and dsl["unknown_stop"] == 1
    assert dsl["rows"][0][1] == "AAA"

    # end-to-end print path: N=6 < 24 must give NO-GO under v1
    verdict = render_report(start, end, trades, parse_jsonl(_FIXTURE_EQUITY),
                            spy_closes=spy)
    assert verdict == EXIT_NO_GO
    verdict_nospy = render_report(start, end, trades,
                                  parse_jsonl(_FIXTURE_EQUITY))
    assert verdict_nospy == EXIT_NO_GO
    verdict_bench = render_report(start, end, trades, parse_jsonl(_FIXTURE_EQUITY),
                                  spy_closes=spy, bench_closes={"IWM": spy, "QQQ": spy})
    assert verdict_bench == EXIT_NO_GO
    # v2: N=6 is only a floor; DD -2% passes; beta n/a (3 sessions);
    # capture insufficient; no decision-sell hits; pooled N=6 < 60 -> PENDING
    v2 = render_report(start, end, trades, parse_jsonl(_FIXTURE_EQUITY),
                       spy_closes=spy, contract="v2")
    assert v2 == EXIT_PENDING
    # v2 with a pooled prior ledger of 60 positive trips -> decided -> GO
    prior = parse_jsonl([
        f'{{"ts":"2026-07-{1 + i % 28:02d}T14:00:00Z","symbol":"P{i}","realized_pl":{5.0 + (i % 5)}}}'
        for i in range(60)
    ])
    v2_pool = render_report(start, end, trades, parse_jsonl(_FIXTURE_EQUITY),
                            spy_closes=spy, contract="v2",
                            pool=[("run-x", prior)])
    assert v2_pool == EXIT_GO
    # v2: a decision-sell loss below 0.5x stop is a validity FAIL
    dirty = trades + parse_jsonl([
        '{"ts":"2026-08-10T13:00:00Z","symbol":"AAA","action":"buy","stop_loss_pct":5.0}',
        '{"ts":"2026-08-11T14:00:00Z","symbol":"AAA","action":"sell","exit_reason":"decision","realized_pl_pct":-1.5,"realized_pl":-15.0}',
    ])
    assert render_report(start, end, dirty, parse_jsonl(_FIXTURE_EQUITY),
                         spy_closes=spy, contract="v2",
                         pool=[("run-x", prior)]) == EXIT_NO_GO

    # Amendment 2: 5-19 OLS sessions -> beta graded on the mean stamped
    # ex-ante book_beta_spy; the OLS slope (here ~0: book drifts +0.1%/day
    # against SPY alternating +/-1%) is recorded but not counted.
    def _am_equity(stamp: float):
        return parse_jsonl([
            json.dumps({"date": dates[i], "equity": 100000.0 * (1.001 ** i),
                        "day_pl": None, "basis": "close",
                        "book_beta_spy": stamp})
            for i in range(7)
        ])
    assert render_report("2026-09-01", "2026-09-07", trades, _am_equity(1.05),
                         spy_closes=spy2, contract="v2") == EXIT_PENDING
    assert render_report("2026-09-01", "2026-09-07", trades, _am_equity(0.5),
                         spy_closes=spy2, contract="v2") == EXIT_NO_GO
    # 5-19 sessions but NO stamps at all -> beta rule N/A, verdict PENDING
    nostamp = parse_jsonl([
        json.dumps({"date": dates[i], "equity": 100000.0 * (1.001 ** i),
                    "day_pl": None, "basis": "close"})
        for i in range(7)
    ])
    assert render_report("2026-09-01", "2026-09-07", trades, nostamp,
                         spy_closes=spy2, contract="v2") == EXIT_PENDING
    # >= 20 sessions: grading reverts to realized OLS as originally written —
    # book = 0.5 x SPY + drift gives OLS beta 0.5 (FAIL) even though every
    # stamp says 1.0, so the amendment no longer shields it.
    dates3 = [f"2026-10-{i:02d}" for i in range(1, 23)]
    spy_px, bot_eq = 100.0, 100000.0
    spy3, rows3 = {dates3[0]: spy_px}, [
        json.dumps({"date": dates3[0], "equity": bot_eq, "day_pl": None,
                    "basis": "close", "book_beta_spy": 1.0})]
    for i, d in enumerate(dates3[1:]):
        r = 0.01 if i % 2 == 0 else -0.01
        spy_px *= 1.0 + r
        bot_eq *= 1.0 + r / 2.0 + 0.0004
        spy3[d] = spy_px
        rows3.append(json.dumps({"date": d, "equity": bot_eq, "day_pl": None,
                                 "basis": "close", "book_beta_spy": 1.0}))
    assert render_report("2026-10-01", "2026-10-22", trades, parse_jsonl(rows3),
                         spy_closes=spy3, contract="v2") == EXIT_NO_GO

    # ------------------------------------------------------------- v3 ---
    # Run-7 item B3. Each block below pins one of the v3 rules (1)-(7) in
    # the module docstring; v1/v2 assertions above are untouched.
    def _quiet(fn, *a, **kw):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = fn(*a, **kw)
        return rc, buf.getvalue()

    # (1) day-0 predecessor: the latest close/late row BEFORE --start starts
    # the series, so the first in-window close row forms a pair (run-6 v2
    # silently dropped Sep 1). Intraday/legacy rows are never a predecessor;
    # a 'close' beats a 'late' row on the same date.
    v3_eq = parse_jsonl([
        '{"date":"2026-08-30","equity":990000.0,"day_pl":0.0,"basis":"close"}',
        '{"date":"2026-08-31","equity":1000000.0,"day_pl":0.0,"basis":"late"}',
        '{"date":"2026-08-31","equity":1000500.0,"day_pl":0.0,"basis":"intraday"}',
        '{"date":"2026-09-01","equity":1001000.0,"day_pl":1000.0,"basis":"close",'
        '"day_pl_basis":"self","broker_day_pl":1500.0,"book_beta_spy":1.0}',
        '{"date":"2026-09-02","equity":1003500.0,"day_pl":2500.0,"basis":"intraday"}',
        '{"date":"2026-09-03","equity":1003000.0,"day_pl":2000.0,"basis":"close",'
        '"day_pl_basis":"self","broker_day_pl":-2500.0,"book_beta_spy":1.1}',
        '{"date":"2026-09-04","equity":1002500.0,"day_pl":-500.0}',
        '{"date":"2026-09-05","equity":1002000.0,"day_pl":-1000.0,"basis":"close",'
        '"day_pl_basis":"self","broker_day_pl":-1000.0}',
        '{"date":"2026-09-08","equity":1005000.0,"day_pl":3000.0,"basis":"intraday"}',
    ])
    d0 = day0_row(v3_eq, "2026-09-01")
    assert d0 is not None and d0["date"] == "2026-08-31" and d0["basis"] == "late"
    assert day0_row(v3_eq, "2026-08-31")["date"] == "2026-08-30"   # close row
    assert day0_row(v3_eq, "2026-08-30") is None
    both = parse_jsonl([
        '{"date":"2026-08-31","equity":1.0,"basis":"late"}',
        '{"date":"2026-08-31","equity":2.0,"basis":"close"}',
        '{"date":"2026-08-31","equity":3.0,"basis":"intraday"}'])
    assert day0_row(both, "2026-09-01")["equity"] == 2.0  # close beats late; intraday never
    days3, d0b, crows = verdict_days_v3(v3_eq, "2026-09-01", "2026-09-05")
    assert [d for d, _, _ in days3] == ["2026-08-31", "2026-09-01", "2026-09-03", "2026-09-05"]
    assert days3[0] == ("2026-08-31", 1000000.0, None)        # baseline, never a session
    assert len(days3) - 1 == 3 == len(crows)                  # pairs == close rows
    nod0, _, _ = verdict_days_v3(v3_eq, "2026-09-01", "2026-09-05")
    nod0_only, d0_none, _ = verdict_days_v3(
        [r for r in v3_eq if str(r["date"]) >= "2026-09-01"], "2026-09-01", "2026-09-05")
    assert d0_none is None and len(nod0_only) == 3            # no crash, one pair fewer

    # (2) verdict rows are basis='close' only: the intraday-only 09-02 and
    # the legacy 09-04 rows are absent above; an intraday --end row VOIDs.
    assert end_row_basis(v3_eq, "2026-09-05") == "close"
    assert end_row_basis(v3_eq, "2026-09-08") == "intraday"
    assert end_row_basis(v3_eq, "2026-09-04") == "legacy"
    assert end_row_basis(v3_eq, "2026-09-09") is None
    rc, out = _quiet(render_report, "2026-09-01", "2026-09-08", trades, v3_eq,
                     spy_closes=spy, contract="v3")
    assert rc == EXIT_VOID and "VOID: end-date row is basis=intraday" in out
    assert EXIT_VOID == 4 and EXIT_VOID != EXIT_PENDING      # a runbook can branch on it
    assert "VERDICT" not in out                               # no verdict at all
    rc, out = _quiet(render_report, "2026-09-01", "2026-09-09", trades, v3_eq,
                     spy_closes=spy, contract="v3")
    assert rc == EXIT_VOID and "basis=none (no row for that date)" in out
    rc, out = _quiet(render_report, "2026-09-01", "2026-09-05", trades, v3_eq,
                     spy_closes=spy, contract="v3")
    assert rc == EXIT_PENDING and "VERDICT: PENDING (UNDER-FLOOR" in out
    assert "day-0 predecessor: 2026-08-31 $1,000,000.00 (basis=late)" in out
    assert "verdict rows: 3 close row(s) in window + day-0 = 4 point(s), 3 pair(s)" in out
    assert "non-close rows excluded from every rule: intraday=1, legacy=1" in out
    # (8) v3 says 'pairs', never 'sessions'; (10) same-config note present
    assert "sessions" not in out and "pairs" in out
    assert "[NOTE] same-config:" in out
    # (9) telescoping reads the self-consistent day_pl (exact by construction)
    # and prints the broker restatement gap as information
    assert "day_pl basis on close rows: self-consistent=3 broker=0" in out
    assert "-> PASS (limit $1.00)" in out
    assert ("broker last_equity restatement (informational): max "
            "|delta-equity - broker_day_pl| $4,500.00 on 2026-09-03 (3 row(s))") in out
    assert day_pl_basis_counts(crows) == {"self": 3, "broker": 0}
    br = broker_restatement(days3, crows)
    assert br["rows"] == 3 and abs(br["max_gap"] - 4500.0) < 1e-9

    # (2b) a basis='late' row IS the verdict row for a date with NO close row
    # (the bot missed the 16:xx tick — crash, dead-man restart, the
    # post-window restart — and stamped the session mark on relaunch; the
    # run-7 writer measured the next close row's day_pl against it, so the
    # checker must read the same predecessor). It is listed, an --end on such
    # a date is NOT void, and a late row on a date that also has a close row
    # is dropped (close wins) and counted among the excluded rows.
    late_eq = parse_jsonl([
        '{"date":"2026-08-31","equity":1000000.0,"day_pl":0.0,"basis":"late"}',
        '{"date":"2026-09-01","equity":1001000.0,"day_pl":1000.0,"basis":"close","day_pl_basis":"self"}',
        '{"date":"2026-09-02","equity":1003500.0,"day_pl":2500.0,"basis":"intraday"}',
        '{"date":"2026-09-02","equity":1003000.0,"day_pl":2000.0,"basis":"late","day_pl_basis":"self"}',
        '{"date":"2026-09-03","equity":1002000.0,"day_pl":-1000.0,"basis":"late","day_pl_basis":"self"}',
        '{"date":"2026-09-03","equity":1002500.0,"day_pl":-500.0,"basis":"close","day_pl_basis":"self"}',
        '{"date":"2026-09-04","equity":1004000.0,"day_pl":1500.0,"basis":"late","day_pl_basis":"self"}',
    ])
    assert end_row_basis(late_eq, "2026-09-02") == "late"
    assert end_row_basis(late_eq, "2026-09-03") == "close"      # close beats late
    assert end_row_basis(late_eq, "2026-09-04") == "late"
    crows_l = close_rows_by_date(late_eq, "2026-09-01", "2026-09-04")
    assert [(d, r["basis"]) for d, r in sorted(crows_l.items())] == [
        ("2026-09-01", "close"), ("2026-09-02", "late"),
        ("2026-09-03", "close"), ("2026-09-04", "late")]
    assert crows_l["2026-09-03"]["equity"] == 1002500.0          # the close row, not the late one
    rc, out = _quiet(render_report, "2026-09-01", "2026-09-04", trades, late_eq,
                     spy_closes=spy, contract="v3")
    assert rc == EXIT_PENDING and "VOID" not in out, out
    assert "verdict rows: 2 close row(s) + 2 late row(s) in window + day-0 = 5 point(s), 4 pair(s)" in out
    assert ("equity basis: close=2 row(s), intraday=1 row(s), late=3 row(s)  "
            "(late rows admitted as verdict rows — no close row that date: 2026-09-02, 2026-09-04)  "
            "(non-close rows excluded from every rule: intraday=1, late=1)") in out
    assert "-> PASS (limit $1.00)" in out       # telescoping exact against the late rows
    rc, out = _quiet(render_report, "2026-09-01", "2026-09-02", trades, late_eq,
                     spy_closes=spy, contract="v3")
    assert rc == EXIT_PENDING and "VOID" not in out    # --end on a late-only date is a verdict
    assert "verdict rows: 1 close row(s) + 1 late row(s) in window + day-0 = 3 point(s), 2 pair(s)" in out

    # (3) satellite-only N: system-managed exits are excluded and reported —
    # by exit_reason (the five literals the bot writes) AND by symbol (the
    # CORE_ETF / HEDGE_ETF: a QQQ core stop leaves as bracket_stop, which the
    # exit_reason filter cannot see).
    sat_trades = parse_jsonl([
        '{"ts":"2026-08-10T14:00:00Z","symbol":"AAA","action":"sell","exit_reason":"trail","realized_pl":10.0}',
        '{"ts":"2026-08-10T15:00:00Z","symbol":"PSQ","action":"sell","exit_reason":"hedge_unwind","realized_pl":41.76}',
        '{"ts":"2026-08-11T14:00:00Z","symbol":"QQQ","action":"sell","exit_reason":"core_defense","realized_pl":27.56}',
        '{"ts":"2026-08-11T15:00:00Z","symbol":"XLU","action":"sell","exit_reason":"defensive_rotate","realized_pl":1.0}',
        '{"ts":"2026-08-11T16:00:00Z","symbol":"QQQ","action":"sell","exit_reason":"regime_trim","realized_pl":1.0}',
        '{"ts":"2026-08-11T17:00:00Z","symbol":"FIX","action":"sell","exit_reason":"correction","realized_pl":1.0}',
        '{"ts":"2026-08-11T18:00:00Z","symbol":"QQQ","action":"sell","exit_reason":"bracket_stop","realized_pl":2.0}',
        '{"ts":"2026-08-12T14:00:00Z","symbol":"BBB","action":"sell","exit_reason":"bracket_stop","realized_pl":-5.0,"instrument":"option"}',
        '{"ts":"2026-08-12T15:00:00Z","symbol":"CCC","action":"buy","realized_pl":99.0}',
        '{"ts":"2026-08-12T16:00:00Z","symbol":"DDD","realized_pl":20.0}',
        '{"ts":"2026-08-20T14:00:00Z","symbol":"EEE","action":"sell","exit_reason":"trail","realized_pl":7.0}',
    ])
    assert V3_SYSTEM_EXIT_REASONS == {"hedge_unwind", "core_defense", "regime_trim",
                                      "defensive_rotate", "correction"}
    assert V3_SYSTEM_SYMBOLS == {"QQQ", "PSQ"}
    sat, excl = satellite_closed_in_window(sat_trades, "2026-08-10", "2026-08-14")
    assert [r["symbol"] for r in sat] == ["AAA", "QQQ", "BBB", "DDD"]  # no symbol filter: the QQQ core stop counts; no-action row counts
    assert [r["exit_reason"] for r in excl] == [
        "hedge_unwind", "core_defense", "defensive_rotate", "regime_trim", "correction"]
    sat_s, excl_s = satellite_closed_in_window(sat_trades, "2026-08-10", "2026-08-14",
                                               V3_SYSTEM_SYMBOLS)
    assert [r["symbol"] for r in sat_s] == ["AAA", "BBB", "DDD"]
    assert [(r["symbol"], r["exit_reason"]) for r in excl_s][-1] == ("QQQ", "bracket_stop")
    assert satellite_closed_in_window(sat_trades, "2026-08-10", "2026-08-14",
                                      ["qqq"])[0][1]["symbol"] == "BBB"   # case-insensitive
    assert all_satellite_pls(sat_trades) == [10.0, 2.0, -5.0, 20.0, 7.0]
    assert all_satellite_pls(sat_trades, V3_SYSTEM_SYMBOLS) == [10.0, -5.0, 20.0, 7.0]
    assert closed_pls_in_window(sat_trades, "2026-08-10", "2026-08-14") == [
        10.0, 41.76, 27.56, 1.0, 1.0, 1.0, 2.0, -5.0, 99.0, 20.0]    # v1/v2 unchanged
    assert instrument_split(sat_s) == {"equity": [10.0, 20.0], "option": [-5.0]}
    rc, out = _quiet(render_report, "2026-08-10", "2026-08-14", sat_trades, v3_eq[:2],
                     contract="v3")
    assert rc == EXIT_VOID  # no close row on 08-14 — still guards first
    rc, out = _quiet(render_report, "2026-08-31", "2026-09-05", sat_trades + parse_jsonl([
        '{"ts":"2026-09-01T14:00:00Z","symbol":"PSQ","action":"sell","exit_reason":"hedge_unwind","realized_pl":41.76}',
        '{"ts":"2026-09-02T14:00:00Z","symbol":"ZZZ","action":"sell","exit_reason":"trail","realized_pl":3.0}',
    ]), v3_eq, spy_closes=spy, contract="v3")
    assert "N=1" in out and "excluded system-managed rows" in out
    assert ("excluded system-managed rows (exit_reason core_defense/correction/"
            "defensive_rotate/hedge_unwind/regime_trim; symbols PSQ/QQQ): "
            "n=1 sum=$41.76  [2026-09-01 PSQ hedge_unwind +41.76]") in out
    assert "day-0 predecessor: 2026-08-30 $990,000.00 (basis=close)" in out
    # the symbol filter: a QQQ core stop (bracket_stop) is excluded by default,
    # counted with the filter disabled, and the line names the symbols in use
    core_stop = parse_jsonl([
        '{"ts":"2026-09-02T15:00:00Z","symbol":"QQQ","action":"sell","exit_reason":"bracket_stop","realized_pl":-30.0}',
        '{"ts":"2026-09-02T16:00:00Z","symbol":"ZZZ","action":"sell","exit_reason":"trail","realized_pl":3.0}',
    ])
    rc, out = _quiet(render_report, "2026-08-31", "2026-09-05", core_stop, v3_eq,
                     spy_closes=spy, contract="v3")
    assert "ts in window): N=1" in out
    assert "symbols PSQ/QQQ): n=1 sum=$-30.00  [2026-09-02 QQQ bracket_stop -30.00]" in out
    rc, out = _quiet(render_report, "2026-08-31", "2026-09-05", core_stop, v3_eq,
                     spy_closes=spy, contract="v3", system_symbols=frozenset())
    assert "ts in window): N=2" in out and "symbols none): n=0" in out
    rc, out = _quiet(render_report, "2026-08-31", "2026-09-05", core_stop, v3_eq,
                     spy_closes=spy, contract="v3", system_symbols=["psq"])
    assert "ts in window): N=2" in out and "symbols PSQ): n=0" in out

    # (4) rule 2 counted at satellite N >= 24 (in-window t); bootstrap CI,
    # profit factor, concentration and the equity/options split printed.
    assert profit_factor([10.0, -5.0, 20.0]) == 6.0
    assert profit_factor([10.0, 20.0]) == float("inf")
    assert profit_factor([]) is None and profit_factor([0.0]) is None
    c = concentration([100.0, 50.0, 20.0, 1.0, -1.0, 2.0])
    assert c["top_sum"] == 170.0 and abs(c["top_share"] - 170.0 / 172.0) < 1e-12
    assert abs(c["mean_ex_top"] - (2.0 / 3.0)) < 1e-12 and c["n_rest"] == 3
    assert concentration([1.0, 2.0])["mean_ex_top"] is None
    seven = parse_jsonl([json.dumps(
        {"date": dates[i], "equity": 100000.0 * (1.001 ** i), "day_pl": None,
         "basis": "close", "book_beta_spy": 1.0}) for i in range(7)] + [
        '{"date":"2026-08-31","equity":99900.0,"basis":"late"}'])

    def _trips(n, pl=50.0):
        return parse_jsonl([json.dumps(
            {"ts": f"{dates[i % 7]}T14:00:00Z", "symbol": f"T{i}", "action": "sell",
             "exit_reason": "trail", "realized_pl": pl + (i % 4),
             "instrument": "option" if i % 6 == 0 else "equity"}) for i in range(n)])
    rc, out = _quiet(render_report, "2026-09-01", "2026-09-07", _trips(24), seven,
                     spy_closes=spy2, contract="v3")
    assert rc == EXIT_GO and "VERDICT: PASS" in out, out
    assert "[PASS] rule 2 window expectancy > 0 @ 95% one-sided  (N=24" in out
    assert "bootstrap95=[" in out and "profit factor=inf (no losses)" in out
    assert "concentration: top-3 trips sum=" in out and "mean ex-top-3=" in out
    assert "equity-only: N=20" in out and "option-only: N=4" in out
    assert "[INFO] rule 3 live-pilot bar: pooled N=24 < 60 — PENDING (not a window rule)" in out
    assert "[PASS] rule 5a mean ex-ante book_beta_spy within +/-0.2 of 1.00  (mean=1.00 over 7 close row(s), 6 pairs)" in out
    rc, out = _quiet(render_report, "2026-09-01", "2026-09-07", _trips(23), seven,
                     spy_closes=spy2, contract="v3")
    assert rc == EXIT_PENDING and "[PEND] rule 2 window expectancy: N=23 < 24" in out
    # N >= 24 but a mean not distinguishable from zero -> counted FAIL
    mixed24 = _trips(12, 50.0) + parse_jsonl([json.dumps(
        {"ts": f"{dates[i % 7]}T15:00:00Z", "symbol": f"L{i}", "action": "sell",
         "exit_reason": "bracket_stop", "realized_pl": -50.0 - (i % 4)}) for i in range(12)])
    rc, out = _quiet(render_report, "2026-09-01", "2026-09-07", mixed24, seven,
                     spy_closes=spy2, contract="v3")
    assert rc == EXIT_NO_GO and "[FAIL] rule 2 window expectancy" in out
    assert "VERDICT: FAIL  (window expectancy)" in out
    # rule 3: a pooled ledger is satellite-filtered too and DECIDES at >= 60
    rc, out = _quiet(render_report, "2026-09-01", "2026-09-07", _trips(24), seven,
                     spy_closes=spy2, contract="v3",
                     pool=[("run-x", prior + parse_jsonl([
                         '{"ts":"2026-07-01T14:00:00Z","symbol":"PSQ","action":"sell","exit_reason":"hedge_unwind","realized_pl":500.0}']))])
    assert rc == EXIT_GO and "run-x: N=60 " in out
    assert "[INFO] rule 3 live-pilot bar: pooled N=84 >= 60" in out and "-> PASS (not a window rule)" in out

    # (5) beta rule: mean ex-ante +/-0.2 at >= 5 pairs; OLS 90% CI overlap
    # with [0.8, 1.2] at >= 20 pairs (both always printed).
    seven_lo = parse_jsonl([json.dumps(
        {"date": dates[i], "equity": 100000.0 * (1.001 ** i), "day_pl": None,
         "basis": "close", "book_beta_spy": 0.5}) for i in range(7)])
    rc, out = _quiet(render_report, "2026-09-01", "2026-09-07", trades, seven_lo,
                     spy_closes=spy2, contract="v3")
    assert rc == EXIT_NO_GO and "[FAIL] rule 5a mean ex-ante book_beta_spy" in out
    assert "(mean=0.50 over 7 close row(s), 6 pairs)" in out      # no day-0 row here
    assert "[N/A ] rule 5b OLS beta CI: 6 pairs < 20 — not counted (90% CI [" in out
    o3 = ols_alpha_beta(days2, spy2)
    assert o3["se_beta"] is not None and o3["se_beta"] < 1e-9        # exact fit
    assert beta_ci90(o3) is not None and abs(beta_ci90(o3)[0] - 0.5) < 1e-6
    assert _interval_overlaps((0.7, 0.9), 0.8, 1.2) and not _interval_overlaps((0.3, 0.7), 0.8, 1.2)
    # 22 close rows + day-0: book = 0.5 x SPY + drift, stamps 1.0 -> 5a PASS
    # on the stamps but 5b FAIL on the OLS CI (beta 0.50, SE ~0); rule 4 now
    # counted (alpha > 0 PASS); capture 11/10 qualifies: up 104% > down 96%
    rows3_d0 = parse_jsonl(['{"date":"2026-09-30","equity":100000.0,"basis":"late"}']) \
        + parse_jsonl(rows3)
    rc, out = _quiet(render_report, "2026-10-01", "2026-10-22", trades, rows3_d0,
                     spy_closes=spy3, contract="v3")
    assert rc == EXIT_NO_GO, out
    assert "[PASS] rule 4 daily alpha > 0" in out and "21 pairs" in out
    assert "[PASS] rule 5a mean ex-ante" in out
    assert "[FAIL] rule 5b OLS beta 90% CI [0.50, 0.50] overlaps [0.80, 1.20]" in out
    assert "VERDICT: FAIL  (beta OLS CI)" in out
    # same series with book = 1.0 x SPY + drift -> 5b PASS -> PENDING (N=6)
    spy_px, bot_eq = 100.0, 100000.0
    rows4 = [json.dumps({"date": dates3[0], "equity": bot_eq, "day_pl": None,
                         "basis": "close", "book_beta_spy": 1.0})]
    for i, d in enumerate(dates3[1:]):
        r = 0.01 if i % 2 == 0 else -0.01
        spy_px *= 1.0 + r
        bot_eq *= 1.0 + r + 0.0004
        rows4.append(json.dumps({"date": d, "equity": bot_eq, "day_pl": None,
                                 "basis": "close", "book_beta_spy": 1.0}))
    rc, out = _quiet(render_report, "2026-10-01", "2026-10-22", trades,
                     parse_jsonl(rows4), spy_closes=spy3, contract="v3")
    assert rc == EXIT_PENDING, out
    assert "[PASS] rule 5b OLS beta 90% CI [1.00, 1.00] overlaps [0.80, 1.20]" in out
    assert "[PASS] rule 7 up-capture > down-capture AND down-capture < 100%  (up 104.0% vs down 96.0%" in out

    # (6) capture counted at >= 6 up AND >= 6 down pairs: up > down AND
    # down < 100%. Book 1.6x SPY up / 1.2x down -> up 160 > down 120 but
    # down >= 100 -> FAIL; 1.0x / 0.9x -> PASS.
    def _cap_rows(up_mult, dn_mult):
        spy_px, bot_eq = 100.0, 100000.0
        rows = [json.dumps({"date": dates[0], "equity": bot_eq, "basis": "close"})]
        for i, d in enumerate(dates[1:]):
            r = 0.01 if i % 2 == 0 else -0.01
            spy_px *= 1.0 + r
            bot_eq *= 1.0 + r * (up_mult if r > 0 else dn_mult)
            rows.append(json.dumps({"date": d, "equity": bot_eq, "basis": "close"}))
        return parse_jsonl(rows)
    rc, out = _quiet(render_report, dates[0], dates[-1], trades, _cap_rows(1.6, 1.2),
                     spy_closes=spy2, contract="v3")
    assert rc == EXIT_NO_GO and "VERDICT: FAIL  (capture)" in out, out
    assert "[FAIL] rule 7 up-capture > down-capture AND down-capture < 100%  (up 160.0% vs down 120.0%, 6 up / 6 down pairs)" in out
    rc, out = _quiet(render_report, dates[0], dates[-1], trades, _cap_rows(1.0, 0.9),
                     spy_closes=spy2, contract="v3")
    assert rc == EXIT_PENDING and "[PASS] rule 7 up-capture > down-capture AND down-capture < 100%  (up 100.0% vs down 90.0%" in out
    rc, out = _quiet(render_report, dates[0], dates[-1], trades, _cap_rows(0.8, 0.9),
                     spy_closes=spy2, contract="v3")
    assert rc == EXIT_NO_GO and "(up 80.0% vs down 90.0%" in out
    # 5 up / 5 down pairs -> INSUFFICIENT, not counted
    rc, out = _quiet(render_report, dates[0], dates[-3], trades, _cap_rows(1.6, 1.2),
                     spy_closes=spy2, contract="v3")
    assert rc == EXIT_PENDING and "[N/A ] rule 7 capture: INSUFFICIENT SAMPLE (5 up / 5 down pairs; need 6/6)" in out

    # (7) worst day on delta-equity, not the broker day_pl: run-6-style rows
    # (no day_pl_basis) whose broker figure names 09-02 while the equity
    # curve's worst step is 09-03.
    wd_rows = parse_jsonl([
        '{"date":"2026-08-31","equity":100000.0,"day_pl":0.0,"basis":"late"}',
        '{"date":"2026-09-01","equity":101000.0,"day_pl":1000.0,"basis":"close"}',
        '{"date":"2026-09-02","equity":103000.0,"day_pl":-2500.0,"basis":"close"}',
        '{"date":"2026-09-03","equity":102000.0,"day_pl":-900.0,"basis":"close"}',
    ])
    wdays, _, wcrows = verdict_days_v3(wd_rows, "2026-09-01", "2026-09-03")
    assert worst_day(wdays) == ("2026-09-02", -2500.0)            # v1/v2 reading
    assert worst_day_delta(wdays) == ("2026-09-03", -1000.0)      # v3 reading
    assert worst_day_delta(wdays[:1]) is None
    assert day_pl_basis_counts(wcrows) == {"self": 0, "broker": 3}
    rc, out = _quiet(render_report, "2026-09-01", "2026-09-03", trades, wd_rows,
                     spy_closes=spy, contract="v3")
    assert "worst day (delta-equity, self-consistent): 2026-09-03 $-1,000.00   broker day_pl that date: $-900.00" in out
    assert "day_pl basis on close rows: self-consistent=0 broker=3" in out
    assert "max per-day gap |delta-equity - day_pl|: $4,500.00 on 2026-09-02  -> FAIL" in out
    assert "[WARN] validity: telescoping FAIL (max gap $4,500.00 on 2026-09-02)" in out
    assert "broker last_equity restatement (informational): max |delta-equity - broker_day_pl| $4,500.00 on 2026-09-02 (3 row(s))" in out
    assert rc == EXIT_PENDING                                     # validity never sets the exit code

    # (10) run-7 4a-18: phantom / duplicate sell rows (AVAV / T shapes)
    ph = parse_jsonl([
        '{"ts":"2026-07-07T14:54:45Z","symbol":"AVAV","action":"sell","qty":-37.0,"exit_price":163.72,"realized_pl":365.04,"exit_reason":"flatten","order_id":"c9e7"}',
        '{"ts":"2026-07-08T13:23:02Z","symbol":"AVAV","action":"sell","qty":37.0,"exit_price":169.67,"realized_pl":212.01,"exit_reason":"trail","order_id":"6f23"}',
        '{"ts":"2026-07-08T13:23:38Z","symbol":"AVAV","action":"sell","qty":37.0,"exit_price":169.7232,"realized_pl":213.9784,"exit_reason":"trail","order_id":"bf06"}',
        '{"ts":"2026-07-23T16:50:50Z","symbol":"T","action":"sell","instrument":"option","qty":900.0,"realized_pl":-2700.0,"exit_reason":"flatten","order_id":"cf8b"}',
        '{"ts":"2026-07-23T20:00:13Z","symbol":"T","action":"sell","instrument":"option","qty":900.0,"realized_pl":-2700.0,"exit_reason":"flatten","order_id":"b4dc"}',
        '{"ts":"2026-07-24T14:00:00Z","symbol":"T","action":"sell","instrument":"option","qty":900.0,"realized_pl":-2700.0,"exit_reason":"flatten","order_id":"late"}',
        '{"ts":"2026-07-25T14:00:00Z","symbol":"ZZ","action":"sell","qty":5.0,"realized_pl":10.0,"exit_reason":"trail","order_id":"f1","fill_price":10.0}',
        '{"ts":"2026-07-25T14:05:00Z","symbol":"ZZ","action":"sell","qty":5.0,"realized_pl":10.0,"exit_reason":"trail","order_id":"f2"}',
        '{"ts":"2026-07-26T14:00:00Z","symbol":"LEG","action":"sell","qty":0.0,"realized_pl":1.0,"exit_reason":"decision"}',
    ])
    kept_ph, dropped_ph = drop_phantom_sells(ph)
    assert [(r["order_id"], r["_phantom_rule"]) for r in dropped_ph] == [
        ("c9e7", "negative_qty"), ("6f23", "replaced_dupe"), ("cf8b", "replaced_dupe")]
    assert dropped_ph[1]["_kept_order_id"] == "bf06"
    assert [r.get("order_id") for r in kept_ph] == ["bf06", "b4dc", "late", "f1", "f2", None]
    assert drop_phantom_sells(kept_ph) == (kept_ph, [])            # idempotent
    rc, out = _quiet(render_report, "2026-07-01", "2026-07-31",
                     ph + parse_jsonl(['{"ts":"2026-07-08T15:00:00Z","symbol":"AAA","action":"sell","qty":1.0,"realized_pl":5.0,"exit_reason":"trail"}']),
                     [{"date": "2026-06-30", "equity": 100.0, "basis": "close"},
                      {"date": "2026-07-31", "equity": 100.0, "basis": "close"}],
                     contract="v3", pool=[("prior", ph)], show_dropped=True)
    assert "phantom/dupe sell rows dropped (this ledger): n=3 sum=$-2,122.95 [negative_qty=1 replaced_dupe=2]" in out
    assert "phantom/dupe sell rows dropped (prior): n=3" in out
    assert "  dropped: 2026-07-07T14:54 AVAV equity flatten qty=-37.0 $365.04 [negative_qty]" in out
    assert "  dropped: 2026-07-08T13:23 AVAV equity trail qty=37.0 $212.01 [replaced_dupe] (exit carried by order bf06)" in out
    assert "ts in window): N=7" in out                            # 10 sells - 3 phantoms
    rc, out = _quiet(render_report, "2026-07-01", "2026-07-31", ph,
                     [{"date": "2026-06-30", "equity": 100.0, "basis": "close"},
                      {"date": "2026-07-31", "equity": 100.0, "basis": "close"}],
                     contract="v3")
    assert "n=3 sum=$-2,122.95 [negative_qty=1 replaced_dupe=2]  (--show-dropped lists them)" in out
    assert "  dropped:" not in out

    print("SELFTEST PASS")
    return 0


# ------------------------------------------------------------------- main ---

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="--spy-csv format:\n" + SPY_CSV_HELP,
    )
    ap.add_argument("--start", help="window start, YYYY-MM-DD (inclusive)")
    ap.add_argument("--end", help="window end, YYYY-MM-DD (inclusive)")
    ap.add_argument("--trades", default=str(TRADES_DEFAULT),
                    help="path to trades.jsonl (default: %(default)s)")
    ap.add_argument("--equity", default=str(EQUITY_DEFAULT),
                    help="path to equity_history.jsonl (default: %(default)s)")
    ap.add_argument("--spy-csv", default=None,
                    help="daily SPY closes, one 'YYYY-MM-DD,close' per line "
                         "(see the epilog below for how to export it)")
    ap.add_argument("--bench-csv", action="append", default=[],
                    metavar="SYM=path",
                    help="extra benchmark closes (same csv format), e.g. "
                         "IWM=iwm.csv; repeatable; informational only")
    ap.add_argument("--contract", choices=("v1", "v2", "v3"), default="v1",
                    help="rule set: v1 = run-5 (default), v2 = run-6 "
                         "(pooled expectancy, alpha/beta, 4/4 capture, "
                         "decision-sell validity), v3 = run-7 (day-0 "
                         "predecessor, close rows only, satellite N, "
                         "in-window expectancy at N>=24, beta CI, 6/6 "
                         "capture with down<100%%); see the module docstring")
    ap.add_argument("--pool", action="append", default=[], metavar="TRADES_JSONL",
                    help="prior same-config window ledger(s) to pool for the v2 "
                         "expectancy test; repeatable; every closed row counts")
    ap.add_argument("--beta-target", type=float, default=1.0,
                    help="target book SPY-beta for the v2 realized-beta check "
                         "(HEDGE_BETA_TARGET; default %(default)s)")
    ap.add_argument("--beta-json", default=None,
                    help="risk_state.json — its latest book_beta.spy becomes the "
                         "ex-ante beta fallback for days without a stamped "
                         "book_beta_spy close row (default fallback: --beta-target)")
    ap.add_argument("--system-symbols", default=",".join(sorted(V3_SYSTEM_SYMBOLS)),
                    metavar="SYM[,SYM]",
                    help="v3 only: symbols whose closed rows are system-managed "
                         "regardless of exit_reason (the run-7 CORE_ETF / "
                         "HEDGE_ETF — a core stop or a trailed hedge leaves as "
                         "bracket_stop/trail, invisible to the exit_reason "
                         "filter); excluded from satellite N and printed on the "
                         "'excluded system-managed rows' line; '' disables "
                         "(default: %(default)s)")
    ap.add_argument("--show-dropped", action="store_true",
                    help="v3 only: list every phantom/duplicate SELL row the "
                         "checker dropped (negative qty; a replaced or "
                         "expired-and-resubmitted exit ledgered twice within "
                         f"{PHANTOM_DUPE_WINDOW_H:g} h) — the summary line "
                         "prints regardless")
    ap.add_argument("--selftest", action="store_true",
                    help="run the embedded-fixture selftest and exit")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()
    if not args.start or not args.end:
        ap.error("--start and --end are required (unless --selftest)")

    trade_rows = parse_jsonl(
        Path(args.trades).read_text(encoding="utf-8").splitlines())
    equity_rows = parse_jsonl(
        Path(args.equity).read_text(encoding="utf-8").splitlines())
    spy_closes = None
    if args.spy_csv:
        spy_closes = parse_spy_csv_lines(
            Path(args.spy_csv).read_text(encoding="utf-8").splitlines())

    bench: dict[str, dict[str, float]] = {}
    for spec in args.bench_csv:
        sym, _, path = spec.partition("=")
        if not path:
            ap.error(f"--bench-csv expects SYM=path, got {spec!r}")
        bench[sym.strip().upper()] = parse_spy_csv_lines(
            Path(path).read_text(encoding="utf-8").splitlines())

    pool: list[tuple[str, list[dict]]] = []
    for path in args.pool:
        pool.append((path, parse_jsonl(
            Path(path).read_text(encoding="utf-8").splitlines())))

    beta_fallback = None
    if args.beta_json:
        try:
            bb = json.loads(Path(args.beta_json).read_text(encoding="utf-8"))
            spy_b = (bb.get("book_beta") or {}).get("spy")
            beta_fallback = float(spy_b) if spy_b is not None else None
        except (OSError, ValueError, AttributeError):
            beta_fallback = None

    system_symbols = frozenset(
        x.strip().upper() for x in str(args.system_symbols).split(",") if x.strip())

    return render_report(args.start, args.end, trade_rows, equity_rows,
                         spy_closes=spy_closes, bench_closes=bench or None,
                         contract=args.contract, pool=pool or None,
                         beta_target=args.beta_target,
                         beta_fallback=beta_fallback,
                         system_symbols=system_symbols,
                         show_dropped=bool(args.show_dropped))


if __name__ == "__main__":
    sys.exit(main())
