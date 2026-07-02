"""Tests for the data-subscription evaluation (2.2).

Builds a small ledger of closed round-trips with known entry signals + outcomes,
then asserts the pay/don't-pay gate: too few trips -> INSUFFICIENT DATA; a measured
positive edge -> SUBSCRIBE; enough trips but no edge -> KEEP MEASURING.

Runnable two ways:
    .venv/bin/python tests/test_subscriptions.py
    .venv/bin/pytest tests/
"""
from __future__ import annotations

import os
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.ledger import TradeLedger, TradeRecord
from investment_strategy.subscriptions import (
    CANDIDATES,
    evaluate_subscriptions,
    render_report,
)


def _ledger() -> TradeLedger:
    p = os.path.join(tempfile.gettempdir(), f"_subs_{uuid.uuid4().hex}.jsonl")
    return TradeLedger(path=p)


def _round_trip(ledger, symbol, source, pl_pct):
    """Record a buy (carrying the entry signal source) then a sell with an outcome."""
    ledger.record(TradeRecord(symbol=symbol, action="buy", entry_signals=[source]))
    ledger.record(TradeRecord(symbol=symbol, action="sell", realized_pl_pct=pl_pct))


def _verdict_for(verdicts, rank):
    return next(v for v in verdicts if v.subscription.rank == rank)


def test_insufficient_data_when_too_few_trips():
    ledger = _ledger()
    _round_trip(ledger, "AAA", "fundamentals", 5.0)   # only 1 trip < min_trips
    v = _verdict_for(evaluate_subscriptions(ledger, min_trips=5), rank=1)
    assert v.recommendation == "INSUFFICIENT DATA"


def test_subscribe_when_measured_edge_exists():
    ledger = _ledger()
    for i in range(6):
        _round_trip(ledger, f"S{i}", "fundamentals", 4.0)  # 6 winning trips
    v = _verdict_for(evaluate_subscriptions(ledger, min_trips=5), rank=1)
    assert v.recommendation == "SUBSCRIBE"
    assert v.trips == 6 and v.avg_pl_pct > 0


def test_keep_measuring_when_no_edge():
    ledger = _ledger()
    for i in range(6):
        _round_trip(ledger, f"S{i}", "fundamentals", -3.0)  # enough trips, losing
    v = _verdict_for(evaluate_subscriptions(ledger, min_trips=5), rank=1)
    assert v.recommendation == "KEEP MEASURING"
    assert v.avg_pl_pct < 0


def test_quiver_tierup_pools_its_sources():
    ledger = _ledger()
    # rank-3 (Quiver) pools congress + offexchange + govcontracts.
    for i in range(3):
        _round_trip(ledger, f"C{i}", "congress", 6.0)
    for i in range(3):
        _round_trip(ledger, f"O{i}", "offexchange", 2.0)
    v = _verdict_for(evaluate_subscriptions(ledger, min_trips=5), rank=3)
    assert v.trips == 6                       # pooled across the two sources
    assert v.recommendation == "SUBSCRIBE"    # positive pooled edge


def test_report_lists_all_candidates_ranked():
    ledger = _ledger()
    report = render_report(ledger)
    assert "Data-subscription evaluation" in report
    for sub in CANDIDATES:
        assert sub.name in report
    # ranked #1 before #3 in the text
    assert report.index("#1") < report.index("#3")


def test_empty_ledger_is_all_insufficient():
    verdicts = evaluate_subscriptions(_ledger())
    assert {v.recommendation for v in verdicts} == {"INSUFFICIENT DATA"}


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
