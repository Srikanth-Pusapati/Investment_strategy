"""Tests for ledger corrections + the effective() view (goGA GA-2.5).

The ledger is append-only, so a rejected/canceled/partial order found at
reconcile is fixed by APPENDING a correction record that points at the original
via order_id. effective() applies them: zero-fill intents vanish (the old
phantom-BUY-row bug), partials are resized to what actually filled.

Runnable two ways:
    .venv/bin/python tests/test_ledger.py
    .venv/bin/pytest tests/
"""
from __future__ import annotations

import os
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.ledger import TradeLedger, TradeRecord


def _ledger() -> TradeLedger:
    p = os.path.join(tempfile.gettempdir(), f"_ledger_{uuid.uuid4().hex}.jsonl")
    return TradeLedger(path=p)


def _buy(symbol="AAPL", qty=10.0, entry=100.0, oid="buy-1"):
    return TradeRecord(symbol=symbol, action="buy", qty=qty, entry_price=entry,
                       cost_usd=entry * qty, order_id=oid)


def test_zero_fill_correction_voids_the_phantom_buy():
    led = _ledger()
    led.record(_buy(oid="o1"))
    led.record(TradeRecord.correction("o1", "AAPL", "rejected", 0.0, 10.0))
    assert len(led.all()) == 2                 # raw file keeps the full story
    assert led.effective() == []               # but the intent never executed


def test_partial_correction_resizes_qty_and_cost():
    led = _ledger()
    led.record(_buy(qty=10.0, entry=100.0, oid="o1"))
    led.record(TradeRecord.correction("o1", "AAPL", "canceled", 4.0, 10.0))
    eff = led.effective()
    assert len(eff) == 1
    assert eff[0].qty == 4.0
    assert eff[0].cost_usd == 400.0            # scaled with the fill
    assert eff[0].entry_price == 100.0         # per-share price stands
    assert "corrected" in eff[0].risk_note


def test_partial_correction_scales_a_sells_realized_dollars():
    led = _ledger()
    led.record(TradeRecord.for_sell(
        "AAPL", "exit", "s1", qty=10.0, realized_pl_pct=5.0, realized_pl=50.0,
        exit_price=105.0,
    ))
    led.record(TradeRecord.correction("s1", "AAPL", "canceled", 5.0, 10.0))
    eff = led.effective()
    assert eff[0].qty == 5.0
    assert eff[0].realized_pl == 25.0          # $ scale with size
    assert eff[0].realized_pl_pct == 5.0       # % is size-independent


def test_correction_rows_never_appear_in_effective():
    led = _ledger()
    led.record(_buy(oid="o1"))
    led.record(TradeRecord.correction("o1", "AAPL", "canceled", 10.0, 10.0))
    eff = led.effective()
    assert all(r.action != "correct" for r in eff)
    # Full fill confirmed late: nothing to resize, record passes through whole.
    assert len(eff) == 1 and eff[0].qty == 10.0


def test_uncorrected_records_pass_through_unchanged():
    led = _ledger()
    led.record(_buy(oid="o1"))
    led.record(TradeRecord.for_sell("AAPL", "exit", "s1", qty=10.0,
                                    exit_price=110.0))
    eff = led.effective()
    assert len(eff) == 2
    assert eff[1].exit_price == 110.0          # GA-2.5 exit price round-trips


def test_last_correction_wins():
    led = _ledger()
    led.record(_buy(qty=10.0, entry=100.0, oid="o1"))
    led.record(TradeRecord.correction("o1", "AAPL", "partially_filled", 2.0, 10.0))
    led.record(TradeRecord.correction("o1", "AAPL", "canceled", 6.0, 10.0))
    eff = led.effective()
    assert eff[0].qty == 6.0                   # the later, final number


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
