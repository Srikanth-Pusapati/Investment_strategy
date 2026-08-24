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
  python3 scripts/eval_contract_check.py --selftest

The contract (pre-registered BEFORE the window; do not move goalposts after):
  1. N >= 24 closed trades in the window
  2. expectancy/trade > 0, significant at 95% one-sided (Student t, df=N-1)
  3. max drawdown of daily equity closes > -5%
  4. up-capture > down-capture vs SPY — evaluated ONLY when the window holds
     >= 6 SPY up-days AND >= 6 SPY down-days, else INSUFFICIENT SAMPLE
     (not counted for or against the verdict)
VERDICT: GO only if every counted check passes.  Exit code: 0=GO, 2=NO-GO.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRADES_DEFAULT = ROOT / "state" / "trades.jsonl"
EQUITY_DEFAULT = ROOT / "state" / "equity_history.jsonl"

# Contract thresholds — pre-registered, keep in one place.
MIN_TRADES = 24
MAX_DD_FLOOR_PCT = -5.0
CAPTURE_MIN_UP_DAYS = 6
CAPTURE_MIN_DOWN_DAYS = 6

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
    """[(date, equity, day_pl)] sorted by date; the LAST row per date wins
    (later rows are later snapshots of the same day's close)."""
    by_date: dict[str, tuple] = {}
    for row in equity_rows:
        date = str(row.get("date") or "")
        if not (start <= date <= end):
            continue
        try:
            eq = float(row["equity"])
        except (KeyError, TypeError, ValueError):
            continue
        day_pl = row.get("day_pl")
        by_date[date] = (eq, None if day_pl is None else float(day_pl))
    return [(d, by_date[d][0], by_date[d][1]) for d in sorted(by_date)]


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


# ----------------------------------------------------------------- report ---

def _money(v) -> str:
    return "n/a" if v is None else f"${v:,.2f}"


def render_report(start: str, end: str, trade_rows, equity_rows,
                  spy_closes: dict[str, float] | None = None) -> bool:
    """Print the full report; True means VERDICT: GO."""
    print(f"=== EVAL-CONTRACT pre-final-test-run-5 | window {start}..{end} ===")

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

    # (4) capture
    print("--- (4) up/down capture vs SPY ---")
    cap = None
    if spy_closes is None:
        print("skipped: no --spy-csv provided (capture not counted in verdict)")
    else:
        cap = capture_vs_spy(days, spy_closes)
        print(f"SPY days in window: {cap['up_days']} up / {cap['down_days']} down")
        if cap["qualified"]:
            print(f"up-capture: {cap['up_capture']:.1f}%   "
                  f"down-capture: {cap['down_capture']:.1f}%")
        else:
            print(f"INSUFFICIENT SAMPLE: need >={CAPTURE_MIN_UP_DAYS} up AND "
                  f">={CAPTURE_MIN_DOWN_DAYS} down SPY days "
                  f"(got {cap['up_days']} up / {cap['down_days']} down)")

    # (5) verdict
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
    return verdict


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

    # end-to-end print path: N=6 < 24 must give NO-GO
    verdict = render_report(start, end, trades, parse_jsonl(_FIXTURE_EQUITY),
                            spy_closes=spy)
    assert verdict is False
    verdict_nospy = render_report(start, end, trades,
                                  parse_jsonl(_FIXTURE_EQUITY))
    assert verdict_nospy is False

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

    verdict = render_report(args.start, args.end, trade_rows, equity_rows,
                            spy_closes=spy_closes)
    return 0 if verdict else 2


if __name__ == "__main__":
    sys.exit(main())
