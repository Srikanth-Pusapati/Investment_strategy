"""Tests for the D.1 backtest glue (backtest_data.py) — pure date->index logic:
alignment inner-join, ledger date mapping, trailing vol, and the breakout rule.
No network: dated close series are injected.

Runnable two ways:
    .venv/bin/python tests/test_backtest_data.py
    .venv/bin/pytest tests/
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.backtest_data import (
    align_price_history,
    annualized_vol_at,
    breakout_entries,
    entries_from_ledger,
)


def _dates(n, start_day=1):
    return [f"2026-01-{d:02d}" for d in range(start_day, start_day + n)]


def _pairs(dates, closes):
    return list(zip(dates, closes))


# -- align_price_history ------------------------------------------------------ #
def test_align_inner_joins_on_common_dates():
    d = _dates(4)                                   # 01..04
    series = {
        "AAA": _pairs(d, [1.0, 2.0, 3.0, 4.0]),
        "BBB": _pairs(d[1:], [10.0, 20.0, 30.0]),   # missing day 01
    }
    dates, prices = align_price_history(series)
    assert dates == d[1:]                            # inner join drops day 01
    assert prices["AAA"] == [2.0, 3.0, 4.0]
    assert prices["BBB"] == [10.0, 20.0, 30.0]


def test_align_empty_when_no_overlap():
    series = {
        "AAA": _pairs(_dates(2, start_day=1), [1.0, 2.0]),
        "BBB": _pairs(_dates(2, start_day=10), [3.0, 4.0]),
    }
    dates, prices = align_price_history(series)
    assert dates == [] and prices == {}


# -- entries_from_ledger ------------------------------------------------------ #
def _ledger(records):
    path = os.path.join(tempfile.gettempdir(), f"_bt_ledger_{uuid.uuid4().hex}.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    return path


def test_ledger_buy_maps_to_first_trading_date_on_or_after_ts():
    dates = ["2026-06-29", "2026-06-30", "2026-07-01", "2026-07-02"]
    path = _ledger([
        {"action": "buy", "symbol": "SPG", "ts": "2026-07-01T18:08:02Z",
         "conviction": 0.58, "stop_loss_pct": 8.0, "take_profit_pct": 16.0},
        # Weekend-dated buy -> next trading date.
        {"action": "buy", "symbol": "AMD", "ts": "2026-06-28T12:00:00Z"},
    ])
    entries = entries_from_ledger(path, dates)
    by_sym = {e.symbol: e for e in entries}
    assert by_sym["SPG"].day == 2
    assert by_sym["SPG"].conviction == 0.58
    assert by_sym["SPG"].stop_loss_pct == 8.0
    assert by_sym["SPG"].take_profit_pct == 16.0
    assert by_sym["AMD"].day == 0                    # 06-28 -> first bar >= it


def test_ledger_skips_sells_options_and_out_of_window_buys():
    dates = ["2026-06-29", "2026-06-30"]
    path = _ledger([
        {"action": "sell", "symbol": "SPG", "ts": "2026-06-29T10:00:00Z"},
        {"action": "buy", "symbol": "OPT", "ts": "2026-06-29T10:00:00Z",
         "instrument": "option"},
        {"action": "buy", "symbol": "LATE", "ts": "2026-08-01T10:00:00Z"},
    ])
    assert entries_from_ledger(path, dates) == []


def test_ledger_skips_symbols_without_price_history():
    dates = ["2026-06-29", "2026-06-30"]
    path = _ledger([
        {"action": "buy", "symbol": "GONE", "ts": "2026-06-29T10:00:00Z"},
    ])
    assert entries_from_ledger(path, dates, prices={"SPG": [1.0, 2.0]}) == []


# -- annualized_vol_at / breakout_entries ------------------------------------- #
def test_vol_none_on_short_history_and_positive_on_noise():
    assert annualized_vol_at([100.0] * 5, day=4) is None
    closes = [100.0 + (3.0 if i % 2 else -3.0) for i in range(40)]
    vol = annualized_vol_at(closes, day=39)
    assert vol is not None and vol > 0


def test_breakout_fires_on_fresh_high_with_cooldown():
    n = 40
    dates = [f"2026-02-{i + 1:02d}" for i in range(n)]  # synthetic calendar
    rising = [100.0 * (1.01 ** i) for i in range(n)]    # fresh high every bar
    flat = [50.0] * n                                    # never breaks out
    entries = breakout_entries(
        dates, {"UP": rising, "FLAT": flat}, lookback=20, cooldown=10,
    )
    symbols = {e.symbol for e in entries}
    assert symbols == {"UP"}
    days = [e.day for e in entries]
    assert days[0] == 20                                 # first bar past lookback
    assert all(b - a >= 10 for a, b in zip(days, days[1:]))  # cooldown respected


def test_breakout_excludes_benchmark():
    n = 30
    dates = [f"2026-03-{i + 1:02d}" for i in range(n)]
    rising = [100.0 * (1.01 ** i) for i in range(n)]
    entries = breakout_entries(
        dates, {"QQQ": rising}, lookback=20, benchmark="QQQ")
    assert entries == []


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {fn.__name__}: {e!r}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
