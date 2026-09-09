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
the equity basis in use.  Exit codes: 0=GO, 2=NO-GO, 3=PENDING (v2 only).
"""
from __future__ import annotations

import argparse
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
    fraction), beta, se_alpha, t_alpha, r2. n < 3 -> alpha/beta None."""
    xs, ys = [], []
    for d0, d1, r in daily_returns(days):
        if d0 in spy_closes and d1 in spy_closes and spy_closes[d0]:
            xs.append(spy_closes[d1] / spy_closes[d0] - 1.0)
            ys.append(r)
    n = len(xs)
    out = {"n": n, "alpha": None, "beta": None, "se_alpha": None,
           "t_alpha": None, "r2": None}
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
               r2=(1.0 - sse / sst) if sst else None)
    return out


def exante_betas(equity_rows: list[dict], start: str, end: str) -> dict[str, float]:
    """{date: book_beta_spy} from the equity rows (the run-6 close row
    carries the cycle's ex-ante SPY beta); a basis='close' row wins."""
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


# ----------------------------------------------------------------- report ---

def _money(v) -> str:
    return "n/a" if v is None else f"${v:,.2f}"


def render_report(start: str, end: str, trade_rows, equity_rows,
                  spy_closes: dict[str, float] | None = None,
                  bench_closes: dict[str, dict[str, float]] | None = None,
                  contract: str = "v1",
                  pool: list[tuple[str, list[dict]]] | None = None,
                  beta_target: float = 1.0,
                  beta_fallback: float | None = None) -> int:
    """Print the full report and return the exit code (EXIT_GO / EXIT_NO_GO /
    EXIT_PENDING). `bench_closes` ({SYM: {date: close}}) adds capture lines
    per benchmark. `contract` picks the rule set ('v1' = run-5, 'v2' =
    run-6). `pool` = [(label, trade_rows)] prior same-config windows for the
    v2 pooled expectancy test. `beta_fallback` = ex-ante beta used for days
    without a stamped `book_beta_spy` (default: beta_target)."""
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
    ap.add_argument("--contract", choices=("v1", "v2"), default="v1",
                    help="rule set: v1 = run-5 (default), v2 = run-6 "
                         "(pooled expectancy, alpha/beta, 4/4 capture, "
                         "decision-sell validity); see the module docstring")
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

    return render_report(args.start, args.end, trade_rows, equity_rows,
                         spy_closes=spy_closes, bench_closes=bench or None,
                         contract=args.contract, pool=pool or None,
                         beta_target=args.beta_target,
                         beta_fallback=beta_fallback)


if __name__ == "__main__":
    sys.exit(main())
