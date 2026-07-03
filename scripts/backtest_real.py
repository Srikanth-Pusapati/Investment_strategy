"""D.1 — run the 2.1 backtest on REAL Alpaca history with the LIVE .env knobs.

Modes:
  replay (default)  Replay the ledger's actual buys over real daily closes —
                    validates live sizing+exits on what actually fired. Honest
                    caveat: a young ledger is a tiny sample.
  --sweep           Grid-sweep the Phase-A knobs (kelly x vol-target x stop/take)
                    on rule-based breakout entries over the full window, ranked
                    by excess return vs the benchmark. This is the EVIDENCE pass
                    for the aggressive config.
  --stress          D.3 brake test: graft a deterministic crash onto real history
                    and keep firing stubborn re-entries into it, twice —
                    (1) LIVE knobs: the daily-loss + drawdown halts must stop the
                        bleeding early;
                    (2) outer brakes disabled: the equity-floor latch (the last
                        line) must flatten + halt on its own.
                    Verifies the guards ENGAGE under full Kelly / vol 45 /
                    sector 50; says nothing about returns.

Usage:
  .venv/bin/python scripts/backtest_real.py                 # ledger replay, 365d
  .venv/bin/python scripts/backtest_real.py --sweep         # knob sweep
  .venv/bin/python scripts/backtest_real.py --sweep --days 500 --symbols AMD,TDG
  .venv/bin/python scripts/backtest_real.py --stress        # D.3 guard check

Read-only: market-data calls only; never places orders or touches live state.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.backtest import BacktestEngine, EntrySignal  # noqa: E402
from investment_strategy.backtest_data import (  # noqa: E402
    breakout_entries,
    crash_overlay,
    entries_from_ledger,
    fetch_price_history,
    stubborn_entries,
)
from investment_strategy.config import load_config  # noqa: E402
from investment_strategy.execution.alpaca_client import AlpacaClient  # noqa: E402

log = logging.getLogger("backtest.real")

# Sweep grid: brackets the Phase-A aggressive settings (kelly 1.0 / vol 45) with
# the old preservation defaults (0.5 / 25) and one step beyond, so the result
# says whether the loosening helped, hurt, or should go further.
_KELLY = (0.25, 0.5, 1.0)
_VOL = (25.0, 45.0, 60.0)
_STOP_TAKE = ((5.0, 12.0), (8.0, 15.0), (8.0, 20.0))


def _ledger_symbols(path: str) -> list[str]:
    syms = set()
    p = Path(path)
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("action") == "buy" and rec.get("symbol"):
                syms.add(rec["symbol"])
    return sorted(syms)


def _run(limits, prices, entries, benchmark):
    return BacktestEngine(limits, prices, benchmark=benchmark).run(entries)


# Liquid fallback basket so --stress works before the ledger has any buys.
_STRESS_BASKET = ["AAPL", "MSFT", "NVDA", "AMD", "AMZN", "GOOGL", "META", "AVGO", "COST", "TSLA"]


def _stress(cfg, dates, prices, benchmark) -> int:
    """D.3 brake test: crash path + stubborn re-entries, run twice.
    Pass 1 (live knobs) must show the daily-loss/drawdown halts engaging;
    pass 2 (outer brakes off) must show the equity-floor latch flattening.
    Returns a shell exit code: 0 only if every expected guard engaged."""
    start = len(dates) // 2
    crashed = crash_overlay(prices, start)
    bench = crash_overlay({"_b": benchmark}, start)["_b"] if benchmark else None
    entries = stubborn_entries(dates, crashed)
    r = cfg.risk
    print(
        f"Crash grafted from bar {start} ({dates[start]}): -2%/day x 70 bars + "
        f"two -9% gap days; {len(entries)} stubborn re-entries into the decline.\n"
    )

    print("== Pass 1: LIVE knobs — daily-loss + drawdown halts should stop the bleed ==")
    res1 = _run(r, crashed, list(entries), bench)
    print(res1.summary())
    p1_daily = (
        any(e.kind == "daily_loss" for e in res1.halt_events)
        or res1.blocked_buys.get("daily_loss_halt", 0) > 0
    )
    p1_dd = res1.blocked_buys.get("drawdown_halt", 0) > 0
    p1_floor_quiet = not any(e.kind == "floor" for e in res1.halt_events)

    print("\n== Pass 2: daily-loss/drawdown brakes OFF — the equity floor is the last line ==")
    naked = replace(r, max_daily_loss_pct=100.0, max_drawdown_pct=100.0)
    res2 = _run(naked, crashed, list(entries), bench)
    print(res2.summary())
    floor_evs = [e for e in res2.halt_events if e.kind == "floor"]
    p2_floor = bool(floor_evs)
    p2_latch = res2.blocked_buys.get("halt_latch", 0) > 0

    def _mark(ok: bool) -> str:
        return "PASS" if ok else "FAIL"

    print("\n== D.3 verdict ==")
    print(f"[{_mark(p1_daily)}] daily-loss guard engaged under live knobs "
          f"(flatten and/or buy-halt)")
    print(f"[{_mark(p1_dd)}] drawdown halt blocked re-buys under live knobs "
          f"({res1.blocked_buys.get('drawdown_halt', 0)} blocked)")
    print(f"[{_mark(p1_floor_quiet)}] equity floor NOT needed while the outer "
          f"brakes are on (fired earlier = correct layering)")
    print(f"[{_mark(p2_floor)}] equity floor latched + flattened when it was the "
          f"only guard left"
          + (f" (day {floor_evs[0].day}, final ${res2.final_equity:,.0f}; "
             f"floor is {r.equity_floor_pct:.0f}% of PEAK — see GUARD line)"
             if p2_floor else ""))
    print(f"[{_mark(p2_latch)}] latch kept blocking every later re-entry "
          f"({res2.blocked_buys.get('halt_latch', 0)} blocked; no auto-resume)")
    ok = all((p1_daily, p1_dd, p1_floor_quiet, p2_floor, p2_latch))
    print("\nAll account-level brakes engaged as intended."
          if ok else "\nA guard did NOT engage — investigate before live money.")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=365, help="trading-day window")
    ap.add_argument("--symbols", default="", help="comma list; default = ledger buys")
    ap.add_argument("--sweep", action="store_true", help="knob grid on breakout entries")
    ap.add_argument("--stress", action="store_true",
                    help="D.3 crash-path brake test (guards must engage)")
    ap.add_argument("--lookback", type=int, default=20, help="breakout high lookback")
    ap.add_argument("--ledger", default="state/trades.jsonl")
    args = ap.parse_args()

    logging.basicConfig(level="INFO", format="%(message)s")
    cfg = load_config()
    broker = AlpacaClient(cfg)
    bench_sym = cfg.benchmark_symbol or "QQQ"

    symbols = (
        [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        or _ledger_symbols(args.ledger)
    )
    if not symbols:
        if args.stress:
            symbols = list(_STRESS_BASKET)   # brake test needs A book, not YOUR book
        else:
            print("No symbols: ledger is empty and --symbols not given.")
            return 1
    fetch = sorted(set(symbols) | {bench_sym})
    print(f"Fetching {args.days} trading days for {len(fetch)} symbols: {', '.join(fetch)}")
    dates, prices = fetch_price_history(broker, fetch, args.days)
    if not dates:
        print("No aligned price history — check symbols/API access.")
        return 1
    benchmark = prices.pop(bench_sym, None)
    print(f"Calendar: {dates[0]} -> {dates[-1]} ({len(dates)} bars, "
          f"{len(prices)} tradable symbols)\n")

    if args.stress:
        return _stress(cfg, dates, prices, benchmark)

    if not args.sweep:
        entries = entries_from_ledger(args.ledger, dates, prices)
        if not entries:
            print("Ledger produced no replayable entries in this window.")
            return 1
        # Trim the window to start at the first ledger entry: otherwise a young
        # ledger's buys sit at the tail of a long window and the benchmark return
        # (computed over the WHOLE window) makes the excess meaningless.
        start = min(e.day for e in entries)
        if start > 0:
            prices = {s: c[start:] for s, c in prices.items()}
            benchmark = benchmark[start:] if benchmark else None
            for e in entries:
                e.day -= start
            print(f"Window trimmed to first ledger entry: {dates[start]} -> {dates[-1]}")
        print(f"Replaying {len(entries)} ledger buys through the LIVE risk knobs:")
        print(_run(cfg.risk, prices, entries, benchmark).summary())
        return 0

    entries = breakout_entries(
        dates, prices, lookback=args.lookback, benchmark=bench_sym,
    )
    print(f"Sweep: {len(entries)} breakout entries "
          f"({args.lookback}-day highs) x {len(_KELLY) * len(_VOL) * len(_STOP_TAKE)} configs\n")
    rows = []
    for k in _KELLY:
        for v in _VOL:
            for stop, take in _STOP_TAKE:
                limits = replace(
                    cfg.risk, kelly_fraction=k, target_annual_vol_pct=v,
                    default_stop_loss_pct=stop, default_take_profit_pct=take,
                )
                r = _run(limits, prices, list(entries), benchmark)
                rows.append((k, v, stop, take, r))
    rows.sort(key=lambda x: x[4].total_return_pct, reverse=True)

    bench_ret = rows[0][4].benchmark_return_pct
    print(f"{'kelly':>5} {'vol':>5} {'stop':>5} {'take':>5} | "
          f"{'return':>8} {'excess':>8} {'maxDD':>6} {'sharpe':>6} {'PF':>5} {'trades':>6}")
    for k, v, stop, take, r in rows:
        excess = f"{r.excess_return_pct:+8.1f}%" if r.excess_return_pct is not None else "     n/a"
        pf = f"{r.profit_factor:5.2f}" if r.profit_factor != float("inf") else "  inf"
        closed = len([t for t in r.trades if t.reason != "end"])
        print(f"{k:5g} {v:5g} {stop:5g} {take:5g} | "
              f"{r.total_return_pct:+7.1f}% {excess} {r.max_drawdown_pct:5.1f}% "
              f"{r.sharpe:6.2f} {pf} {closed:6d}")
    if bench_ret is not None:
        print(f"\nBenchmark {bench_sym}: {bench_ret:+.1f}% over the window.")
    live = (cfg.risk.kelly_fraction, cfg.risk.target_annual_vol_pct,
            cfg.risk.default_stop_loss_pct, cfg.risk.default_take_profit_pct)
    print(f"Live .env config today: kelly={live[0]:g} vol={live[1]:g} "
          f"stop={live[2]:g} take={live[3]:g}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
