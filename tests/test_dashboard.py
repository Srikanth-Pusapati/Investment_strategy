"""Tests for the dashboard's round-trip economics (aggregate_round_trips and
the sell-row basis/proceeds/realized derivation in _Row)."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.dashboard import _Row, aggregate_round_trips
from investment_strategy.ledger import TradeRecord


def _ts(minutes: int = 0) -> datetime:
    return datetime(2026, 7, 14, 14, 0, tzinfo=timezone.utc) + timedelta(minutes=minutes)


def _buy(symbol="AMD", qty=14.0, price=546.97, cost=7657.58, minutes=0,
         instrument="equity") -> TradeRecord:
    return TradeRecord(ts=_ts(minutes), symbol=symbol, action="buy",
                       instrument=instrument, qty=qty, entry_price=price,
                       cost_usd=cost)


def _sell(symbol="AMD", qty=14.0, exit_price=556.28, realized_pl=130.48,
          realized_pl_pct=1.704, minutes=60, instrument="equity") -> TradeRecord:
    return TradeRecord(ts=_ts(minutes), symbol=symbol, action="sell",
                       instrument=instrument, qty=qty, exit_price=exit_price,
                       realized_pl=realized_pl, realized_pl_pct=realized_pl_pct)


# ---- _Row sell economics ---------------------------------------------------- #

def test_sell_row_derives_basis_and_proceeds_from_realized_pl():
    row = _Row(_sell(), price_now=None)
    assert abs(row.proceeds - 556.28 * 14) < 0.01          # money coming back
    assert abs(row.basis - (row.proceeds - 130.48)) < 0.01  # money that went in
    assert row.realized_pl == 130.48


def test_sell_row_reconstructs_dollars_from_pct_when_pl_missing():
    rec = _sell(realized_pl=None, realized_pl_pct=10.0, exit_price=110.0, qty=10.0)
    row = _Row(rec, price_now=None)
    assert abs(row.proceeds - 1100.0) < 0.01
    assert abs(row.basis - 1000.0) < 0.01
    assert abs(row.realized_pl - 100.0) < 0.01


def test_sell_row_without_exit_price_stays_blank_not_wrong():
    rec = _sell(exit_price=None, realized_pl=None, realized_pl_pct=None)
    row = _Row(rec, price_now=None)
    assert row.proceeds is None and row.basis is None and row.realized_pl is None


def test_closed_buy_row_suppresses_phantom_unrealized_pl():
    # AMD was fully sold; a live price on the BUY row must not show unrealized
    # P/L on shares no longer held.
    row = _Row(_buy(), price_now=600.0, position_closed=True)
    assert row.unreal_pl is None
    open_row = _Row(_buy(), price_now=600.0, position_closed=False)
    assert open_row.unreal_pl is not None


# ---- aggregate_round_trips --------------------------------------------------- #

def test_round_trip_full_close_nets_realized_only():
    trips = aggregate_round_trips([_buy(), _sell()], prices={"AMD": 600.0})
    (g,) = trips
    assert g["symbol"] == "AMD"
    assert abs(g["bought_usd"] - 7657.58) < 0.01
    assert abs(g["proceeds_usd"] - 556.28 * 14) < 0.01
    assert g["open_qty"] == 0.0                 # flat — no unrealized leg
    assert g["unreal_pl"] is None
    assert abs(g["net_pl"] - 130.48) < 0.01


def test_round_trip_partial_close_combines_realized_and_unrealized():
    buy = _buy(symbol="NU", qty=100.0, price=10.0, cost=1000.0)
    sell = _sell(symbol="NU", qty=40.0, exit_price=12.0,
                 realized_pl=80.0, realized_pl_pct=20.0)
    (g,) = aggregate_round_trips([buy, sell], prices={"NU": 11.0})
    assert g["open_qty"] == 60.0
    assert abs(g["open_basis"] - 600.0) < 0.01   # 60 sh @ $10 avg cost
    assert abs(g["open_value"] - 660.0) < 0.01   # 60 sh @ $11 live
    assert abs(g["unreal_pl"] - 60.0) < 0.01
    assert abs(g["net_pl"] - 140.0) < 0.01       # 80 realized + 60 unrealized


def test_round_trip_groups_equity_and_option_separately():
    trips = aggregate_round_trips(
        [_buy(symbol="CDW"), _buy(symbol="CDW", instrument="option",
                                  qty=1.0, price=2.5, cost=250.0)],
        prices={"CDW": 145.0},
    )
    assert len(trips) == 2
    instruments = {g["instrument"] for g in trips}
    assert instruments == {"equity", "option"}
    opt = next(g for g in trips if g["instrument"] == "option")
    assert opt["open_value"] is None  # no OCC pricing — never priced off equity px


def test_round_trip_realized_derived_from_pct_when_dollars_missing():
    buy = _buy(symbol="HD", qty=5.0, price=339.09, cost=1695.45)
    sell = _sell(symbol="HD", qty=5.0, exit_price=339.75,
                 realized_pl=None, realized_pl_pct=0.19)
    (g,) = aggregate_round_trips([buy, sell], prices={})
    assert g["realized_pl"] is not None
    assert abs(g["realized_pl"] - (339.75 * 5) * (1 - 1 / 1.0019)) < 0.05
    assert g["net_pl"] == g["realized_pl"]      # flat, no live price needed



def test_option_sell_never_multiplies_premium_price_into_proceeds():
    # exit_price on an option row is per-share premium while qty is contracts;
    # price*qty would be 100x off — proceeds/basis must stay unset, while the
    # recorded realized $ still flows through.
    rec = _sell(symbol="CDW", qty=1.0, exit_price=2.50, realized_pl=-120.0,
                realized_pl_pct=-48.0, instrument="option")
    row = _Row(rec, price_now=None)
    assert row.proceeds is None and row.basis is None
    assert row.realized_pl == -120.0
    buy = _buy(symbol="CDW", instrument="option", qty=1.0, price=3.7, cost=370.0)
    (g,) = aggregate_round_trips([buy, rec], prices={"CDW": 145.0})
    assert g["proceeds_usd"] == 0.0
    assert abs(g["realized_pl"] - (-120.0)) < 0.01


def test_round_trip_reentry_does_not_fabricate_unrealized():
    # Verifier path A: buy 10@100, sell 10@120 (+200), re-buy 10@130.
    # Lifetime-average basis fabricated +$150 unrealized at px 130; the open
    # remainder's true basis is the re-entry lot: $1,300 -> $0 unrealized.
    recs = [
        _buy(symbol="HD", qty=10.0, price=100.0, cost=1000.0, minutes=0),
        _sell(symbol="HD", qty=10.0, exit_price=120.0,
              realized_pl=200.0, realized_pl_pct=20.0, minutes=10),
        _buy(symbol="HD", qty=10.0, price=130.0, cost=1300.0, minutes=20),
    ]
    (g,) = aggregate_round_trips(recs, prices={"HD": 130.0})
    assert abs(g["open_basis"] - 1300.0) < 0.01
    assert abs(g["unreal_pl"]) < 0.01
    assert abs(g["net_pl"] - 200.0) < 0.01


def test_round_trip_multilot_fifo_backfill_basis():
    # Verifier path B: buy 10@100 + 10@200; FIFO bracket stop fills 10@90
    # (realized -100 vs the FIRST lot's basis). Remaining basis is the
    # second lot's $2,000, not the $1,500 lifetime average.
    recs = [
        _buy(symbol="X", qty=10.0, price=100.0, cost=1000.0, minutes=0),
        _buy(symbol="X", qty=10.0, price=200.0, cost=2000.0, minutes=5),
        _sell(symbol="X", qty=10.0, exit_price=90.0,
              realized_pl=-100.0, realized_pl_pct=-10.0, minutes=10),
    ]
    (g,) = aggregate_round_trips(recs, prices={"X": 200.0})
    assert abs(g["open_basis"] - 2000.0) < 0.01
    assert abs(g["unreal_pl"] - 0.0) < 0.01
    assert abs(g["net_pl"] - (-100.0)) < 0.01


def test_legacy_qty_zero_sell_consumes_whole_position():
    # Pre-GA-2.5 full closes recorded qty=0; they must flatten the group, not
    # leave phantom open shares stacking unrealized on top of realized.
    recs = [
        _buy(symbol="OLD", qty=10.0, price=100.0, cost=1000.0, minutes=0),
        _sell(symbol="OLD", qty=0.0, exit_price=None,
              realized_pl=50.0, realized_pl_pct=5.0, minutes=10),
    ]
    (g,) = aggregate_round_trips(recs, prices={"OLD": 111.0})
    assert g["open_qty"] == 0.0
    assert g["unreal_pl"] is None
    assert abs(g["net_pl"] - 50.0) < 0.01


def test_fifo_consumed_buys_flags_sold_lot_after_reentry():
    from investment_strategy.dashboard import fifo_consumed_buys
    first = _buy(symbol="HD", qty=10.0, price=100.0, cost=1000.0, minutes=0)
    sell = _sell(symbol="HD", qty=10.0, exit_price=120.0,
                 realized_pl=200.0, realized_pl_pct=20.0, minutes=10)
    reentry = _buy(symbol="HD", qty=10.0, price=130.0, cost=1300.0, minutes=20)
    consumed = fifo_consumed_buys([first, sell, reentry])
    assert id(first) in consumed        # its shares are gone
    assert id(reentry) not in consumed  # still held -> live P/L is real


def _run_all():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS {fn.__name__}")
        except Exception as e:
            failed += 1
            print(f"  FAIL {fn.__name__}: {e!r}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)

