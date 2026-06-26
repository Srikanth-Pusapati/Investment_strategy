"""Tests for the signal-attribution / reflection loop.

Pure logic, no network: build synthetic ledger records and assert round-trips
reconstruct correctly, per-source stats aggregate, and the rendered lessons block
behaves (terse, gated on enough history).

Runnable two ways:
    .venv/bin/python tests/test_attribution.py     # standalone, no pytest
    .venv/bin/pytest tests/                          # if pytest is installed
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.attribution import (
    attribute, render_lessons, round_trips,
)
from investment_strategy.ledger import TradeLedger, TradeRecord

_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _buy(symbol, signals, t):
    return TradeRecord(symbol=symbol, action="buy", entry_signals=signals,
                       ts=_T0 + timedelta(hours=t))


def _sell(symbol, pl_pct, t, reason="decision"):
    return TradeRecord(symbol=symbol, action="sell", realized_pl_pct=pl_pct,
                       exit_reason=reason, ts=_T0 + timedelta(hours=t))


def test_round_trip_basic():
    recs = [_buy("AAPL", ["technical", "fundamentals"], 0), _sell("AAPL", 5.0, 1)]
    trips = round_trips(recs)
    assert len(trips) == 1
    assert trips[0].pl_pct == 5.0
    assert trips[0].signals == ["fundamentals", "technical"]   # sorted union


def test_round_trip_requires_outcome():
    # A sell with no realized P&L (old record) is not attributable.
    recs = [_buy("AAPL", ["technical"], 0),
            TradeRecord(symbol="AAPL", action="sell", ts=_T0 + timedelta(hours=1))]
    assert round_trips(recs) == []


def test_round_trip_orders_by_time():
    # Records out of order still pair correctly once sorted by ts.
    recs = [_sell("MSFT", -2.0, 3), _buy("MSFT", ["news"], 2)]
    trips = round_trips(recs)
    assert len(trips) == 1 and trips[0].pl_pct == -2.0


def test_scale_in_unions_signals():
    recs = [_buy("NVDA", ["technical"], 0), _buy("NVDA", ["congress"], 1),
            _sell("NVDA", 3.0, 2)]
    trips = round_trips(recs)
    assert trips[0].signals == ["congress", "technical"]


def test_sell_without_open_is_ignored():
    # An exit with no matching open buy attributes to nothing (no crash).
    trips = round_trips([_sell("TSLA", 1.0, 0)])
    assert trips[0].signals == []


def test_attribute_winrate_and_avg():
    recs = [
        _buy("A", ["technical"], 0), _sell("A", 10.0, 1),     # win
        _buy("B", ["technical"], 2), _sell("B", -4.0, 3),     # loss
        _buy("C", ["congress"], 4), _sell("C", -1.0, 5),      # loss
    ]
    stats = attribute(round_trips(recs))
    assert stats["technical"].trips == 2
    assert stats["technical"].wins == 1
    assert stats["technical"].win_rate == 0.5
    assert abs(stats["technical"].avg_pl_pct - 3.0) < 1e-9
    assert stats["congress"].win_rate == 0.0


def _ledger_with(recs) -> TradeLedger:
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    led = TradeLedger(path)
    for r in recs:
        led.record(r)
    return led


def test_render_empty_when_no_history():
    led = _ledger_with([])
    assert render_lessons(led) == ""


def test_render_gates_on_min_source_trips():
    # One trip per source -> below the default min_source_trips (2) -> empty.
    led = _ledger_with([_buy("A", ["technical"], 0), _sell("A", 5.0, 1)])
    assert render_lessons(led) == ""


def test_render_includes_source_lines():
    recs = [
        _buy("A", ["technical"], 0), _sell("A", 6.0, 1),
        _buy("B", ["technical"], 2), _sell("B", 2.0, 3),
        _buy("C", ["congress"], 4), _sell("C", -3.0, 5),
        _buy("D", ["congress"], 6), _sell("D", -1.0, 7),
    ]
    out = render_lessons(_ledger_with(recs))
    assert "Track record" in out
    assert "technical:" in out and "congress:" in out
    # Best performer (technical, +4% avg) ranks above the loser (congress).
    assert out.index("technical:") < out.index("congress:")


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
