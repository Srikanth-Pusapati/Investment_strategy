"""Tests for FIFO lot tracking + wash-sale flagging (goGA GA-2.5).

Runnable two ways:
    .venv/bin/python tests/test_lots.py
    .venv/bin/pytest tests/
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.ledger import TradeRecord
from investment_strategy.lots import build_lot_history, fifo_basis, fifo_lots

_T0 = datetime(2026, 7, 1, 15, 0, tzinfo=timezone.utc)


def _buy(symbol="AAPL", qty=10.0, entry=100.0, oid="b1", days=0):
    return TradeRecord(ts=_T0 + timedelta(days=days), symbol=symbol,
                       action="buy", qty=qty, entry_price=entry,
                       cost_usd=entry * qty, order_id=oid)


def _sell(symbol="AAPL", qty=10.0, price=110.0, oid="s1", days=1,
          pl_pct=None, reason="stop"):
    return TradeRecord.for_sell(
        symbol, "exit", oid, qty=qty, exit_price=price,
        realized_pl_pct=pl_pct, exit_reason=reason,
        ts=_T0 + timedelta(days=days),
    )


def test_multi_lot_sell_realizes_against_fifo_basis():
    # 10 @ $10, 10 @ $20; sell 15 @ $30 -> 10 realized vs $10, 5 vs $20.
    open_lots, realized = build_lot_history([
        _buy(qty=10.0, entry=10.0, oid="b1"),
        _buy(qty=10.0, entry=20.0, oid="b2", days=0),
        _sell(qty=15.0, price=30.0),
    ])
    assert len(realized) == 2
    first, second = realized
    assert first.qty == 10.0 and first.entry_price == 10.0
    assert abs(first.pl_usd - 200.0) < 1e-9        # (30-10) x 10
    assert second.qty == 5.0 and second.entry_price == 20.0
    assert abs(second.pl_usd - 50.0) < 1e-9        # (30-20) x 5
    # 5 shares of the $20 lot remain open.
    remaining = open_lots["AAPL"]
    assert len(remaining) == 1 and remaining[0].remaining == 5.0


def test_unknown_qty_sell_is_a_full_close():
    open_lots, realized = build_lot_history([
        _buy(qty=10.0, entry=100.0),
        _sell(qty=0.0, price=110.0),               # decision sells may lack qty
    ])
    assert open_lots == {}
    assert sum(r.qty for r in realized) == 10.0


def test_legacy_sell_without_exit_price_reconstructs_and_flags():
    # Old records carry realized_pl_pct but no exit_price: the price is rebuilt
    # from the % against the open-lot basis and marked estimated.
    _, realized = build_lot_history([
        _buy(qty=10.0, entry=100.0),
        TradeRecord.for_sell("AAPL", "exit", "s1", qty=10.0,
                             realized_pl_pct=-8.0,
                             ts=_T0 + timedelta(days=1)),
    ])
    assert len(realized) == 1
    assert realized[0].basis_estimated is True
    assert abs(realized[0].exit_price - 92.0) < 1e-9
    assert abs(realized[0].pl_pct - (-8.0)) < 1e-9


def test_sell_with_no_price_info_consumes_lots_but_realizes_nothing():
    open_lots, realized = build_lot_history([
        _buy(qty=10.0, entry=100.0),
        TradeRecord.for_sell("AAPL", "exit", "s1", qty=10.0,
                             ts=_T0 + timedelta(days=1)),
    ])
    assert realized == []                          # no fabricated P&L
    assert open_lots == {}                         # but the shares are gone


def test_wash_sale_flags_loss_with_repurchase_inside_30_days():
    _, realized = build_lot_history([
        _buy(qty=10.0, entry=100.0, oid="b1"),
        _sell(qty=10.0, price=90.0, days=5),                    # -10% loss
        _buy(qty=5.0, entry=88.0, oid="b2", days=20),           # repurchase
    ])
    loss = realized[0]
    assert loss.pl_usd < 0 and loss.wash_sale is True


def test_wash_sale_not_flagged_without_repurchase_or_on_gains():
    _, realized = build_lot_history([
        _buy(qty=10.0, entry=100.0, oid="b1"),
        _sell(qty=10.0, price=90.0, days=5),                    # loss, no re-buy
        _buy(symbol="MSFT", qty=1.0, entry=50.0, oid="b2", days=6),
        _sell(symbol="MSFT", qty=1.0, price=60.0, oid="s2", days=40),  # a gain
    ])
    assert all(not r.wash_sale for r in realized)


def test_options_records_are_excluded_from_share_lots():
    open_lots, realized = build_lot_history([
        TradeRecord(ts=_T0, symbol="AAPL", action="buy", instrument="option",
                    qty=1.0, entry_price=2.50, cost_usd=250.0, order_id="opt1"),
    ])
    assert open_lots == {} and realized == []


def test_fifo_basis_is_non_consuming_and_reports_coverage():
    lots, _ = build_lot_history([
        _buy(qty=5.0, entry=100.0, oid="b1"),
        _buy(qty=5.0, entry=200.0, oid="b2"),
    ])
    basis, covered = fifo_basis(lots["AAPL"], 10.0)
    assert abs(basis - 150.0) < 1e-9 and covered == 10.0
    # Asking for more than held: covered says how much basis actually exists.
    basis2, covered2 = fifo_basis(lots["AAPL"], 12.0)
    assert covered2 == 10.0 and abs(basis2 - 150.0) < 1e-9
    # And nothing was consumed by either call.
    assert sum(l.remaining for l in lots["AAPL"]) == 10.0


# ---- run-7 4a-18: lots carry the entry attributes the sell-row stamp needs -- #

def test_lot_carries_entry_attributes_from_the_buy_row():
    rec = TradeRecord(
        ts=_T0, symbol="NU", action="buy", qty=10.0, entry_price=100.0,
        cost_usd=1000.0, order_id="b1", conviction=0.66, composite_score=1.42,
        stop_loss_pct=5.71, key_signals=["technical +0.8", "insider +0.4"],
        fill_price=100.37,
    )
    lots, _ = build_lot_history([rec])
    lot = lots["NU"][0]
    assert lot.conviction == 0.66 and lot.composite_score == 1.42
    assert lot.stop_loss_pct == 5.71 and lot.fill_price == 100.37
    assert lot.key_signals == ["technical +0.8", "insider +0.4"]
    # 0.0 in the ledger means "not recorded" (core fills, pre-tracking rows),
    # never zero conviction / a zero stop; an unstamped fill is None.
    core = _buy(symbol="QQQ", qty=5.0, entry=400.0, oid="core")
    lots, _ = build_lot_history([core])
    lot = lots["QQQ"][0]
    assert lot.conviction is None and lot.stop_loss_pct is None
    assert lot.fill_price is None and lot.composite_score is None
    assert lot.key_signals == []


def test_fifo_lots_is_non_consuming_oldest_first_and_full_close_on_qty_zero():
    lots, _ = build_lot_history([
        _buy(qty=5.0, entry=100.0, oid="b1"),
        _buy(qty=5.0, entry=200.0, oid="b2"),
        _buy(qty=5.0, entry=300.0, oid="b3"),
    ])
    held = lots["AAPL"]
    assert [l.order_id for l in fifo_lots(held, 3.0)] == ["b1"]
    assert [l.order_id for l in fifo_lots(held, 5.0)] == ["b1"]
    assert [l.order_id for l in fifo_lots(held, 7.0)] == ["b1", "b2"]
    assert [l.order_id for l in fifo_lots(held, 99.0)] == ["b1", "b2", "b3"]
    assert [l.order_id for l in fifo_lots(held, 0.0)] == ["b1", "b2", "b3"]  # legacy full close
    assert sum(l.remaining for l in held) == 15.0                          # nothing consumed


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
