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
    attribute, behavior_diagnostics, render_lessons, round_trips,
)
from investment_strategy.ledger import TradeLedger, TradeRecord

_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _buy(symbol, signals, t, qty=0.0):
    return TradeRecord(symbol=symbol, action="buy", entry_signals=signals,
                       qty=qty, ts=_T0 + timedelta(hours=t))


def _sell(symbol, pl_pct, t, reason="decision", qty=0.0):
    return TradeRecord(symbol=symbol, action="sell", realized_pl_pct=pl_pct,
                       exit_reason=reason, qty=qty, ts=_T0 + timedelta(hours=t))


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


def test_scale_out_partial_keeps_remainder_attributed():
    # 1B.8: a scale-out sells half at +12%, the rest exits later at +20%. BOTH
    # trips must attribute to the entry signal — the remainder isn't orphaned.
    recs = [
        _buy("NVDA", ["congress"], 0, qty=2.0),
        _sell("NVDA", 12.0, 1, reason="scale", qty=1.0),   # partial
        _sell("NVDA", 20.0, 2, reason="trail", qty=1.0),   # remainder, full close
    ]
    trips = round_trips(recs)
    assert len(trips) == 2
    assert all(t.signals == ["congress"] for t in trips)   # neither is signal-less


def test_regime_trim_partial_is_not_a_full_close():
    # 1B.6: a 25% regime trim must not flatten the attribution for the rest.
    recs = [
        _buy("AAPL", ["technical"], 0, qty=4.0),
        _sell("AAPL", -2.0, 1, reason="regime_trim", qty=1.0),  # partial
        _sell("AAPL", -5.0, 2, reason="stop", qty=3.0),         # remainder
    ]
    trips = round_trips(recs)
    assert len(trips) == 2
    assert all(t.signals == ["technical"] for t in trips)


def test_full_close_still_flattens_all_lots():
    # A normal (non-partial) exit clears the whole position even with a qty set.
    recs = [
        _buy("MSFT", ["news"], 0, qty=3.0),
        _sell("MSFT", 4.0, 1, reason="take", qty=3.0),
        _sell("MSFT", 9.0, 2, reason="decision", qty=1.0),  # nothing open -> empty
    ]
    trips = round_trips(recs)
    assert len(trips) == 2
    assert trips[0].signals == ["news"] and trips[1].signals == []


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


def test_round_trip_carries_mean_entry_conviction():
    from investment_strategy.attribution import round_trips as rt
    recs = [
        TradeRecord(symbol="A", action="buy", qty=1.0, conviction=0.4,
                    entry_signals=["technical"], ts=_T0),
        TradeRecord(symbol="A", action="buy", qty=1.0, conviction=0.6,
                    entry_signals=["technical"], ts=_T0 + timedelta(hours=1)),
        _sell("A", 5.0, 2),
    ]
    trips = rt(recs)
    assert len(trips) == 1
    assert abs(trips[0].conviction - 0.5) < 1e-9


def test_round_trip_conviction_none_when_unrecorded():
    # conviction 0.0 means "not recorded" (core fills, pre-tracking rows).
    recs = [_buy("A", ["technical"], 0), _sell("A", 5.0, 1)]
    assert round_trips(recs)[0].conviction is None


def test_conviction_calibration_buckets_and_min_trips():
    from investment_strategy.attribution import RoundTrip, conviction_calibration
    trips = (
        [RoundTrip("X", +3.0, [], conviction=0.3) for _ in range(3)]
        + [RoundTrip("Y", -5.0, [], conviction=0.7) for _ in range(3)]
        + [RoundTrip("Z", +9.0, [], conviction=0.5)]          # n=1 -> suppressed
        + [RoundTrip("W", +9.0, [], conviction=None)]         # unknown -> ignored
    )
    lines = conviction_calibration(trips, min_trips=3)
    joined = "\n".join(lines)
    assert "conviction 0.2-0.4: 3 trades, 100% win, +3.0% avg" in joined
    assert "conviction 0.6+: 3 trades, 0% win, -5.0% avg" in joined
    assert "0.4-0.6" not in joined
    # High-conviction losing to low-conviction -> the inversion flag leads.
    assert lines[0].startswith("CONVICTION INVERTED")


def test_conviction_calibration_no_inversion_when_high_wins():
    from investment_strategy.attribution import RoundTrip, conviction_calibration
    trips = (
        [RoundTrip("X", -2.0, [], conviction=0.3) for _ in range(3)]
        + [RoundTrip("Y", +6.0, [], conviction=0.8) for _ in range(3)]
    )
    lines = conviction_calibration(trips, min_trips=3)
    assert lines and not lines[0].startswith("CONVICTION INVERTED")


def test_render_lessons_includes_calibration_block():
    from investment_strategy.ledger import TradeLedger
    led = TradeLedger.__new__(TradeLedger)
    recs = []
    for i, (conv, pl) in enumerate([(0.7, -5.0)] * 3 + [(0.3, 3.0)] * 3):
        sym = f"S{i}"
        recs.append(TradeRecord(symbol=sym, action="buy", qty=1.0,
                                conviction=conv, entry_signals=["technical"],
                                ts=_T0 + timedelta(hours=2 * i)))
        recs.append(_sell(sym, pl, 2 * i + 1))
    led.effective = lambda: recs  # type: ignore[method-assign]
    out = render_lessons(led)
    assert "Conviction calibration" in out
    assert "CONVICTION INVERTED" in out


# -- behavior diagnostics: disposition effect + overtrading ------------------- #
def _lot_buy(symbol, entry, day):
    return TradeRecord(symbol=symbol, action="buy", qty=1.0, entry_price=entry,
                       cost_usd=entry, order_id=f"b-{symbol}",
                       ts=_T0 + timedelta(days=day))


def _lot_sell(symbol, price, day):
    return TradeRecord.for_sell(symbol, "exit", f"s-{symbol}", qty=1.0,
                                exit_price=price, exit_reason="decision",
                                ts=_T0 + timedelta(days=day))


def _ledger_of(recs):
    led = TradeLedger(path=tempfile.mktemp())
    led.effective = lambda: recs  # type: ignore[method-assign]
    return led


def test_behavior_diagnostics_flags_disposition_effect():
    # 3 winners held ~1 day, 3 losers held ~10 days -> losers held far longer,
    # so the disposition-effect flag fires. (Distinct symbols so FIFO lots don't
    # interfere; all buys on day 0 so the overtrading line stays suppressed.)
    recs = []
    for i in range(3):
        s = f"WIN{i}"
        recs += [_lot_buy(s, 100.0, 0), _lot_sell(s, 110.0, 1)]     # +10%, 1d
    for i in range(3):
        s = f"LOSE{i}"
        recs += [_lot_buy(s, 100.0, 0), _lot_sell(s, 90.0, 10)]     # -10%, 10d
    lines = behavior_diagnostics(_ledger_of(recs))
    text = "\n".join(lines)
    assert "DISPOSITION EFFECT" in text
    assert "winners 1.0d" in text and "losers 10.0d" in text


def test_behavior_diagnostics_suppressed_below_min_trips():
    # Only 2 winners / 2 losers -> below min_trips (3): no disposition line, and
    # all buys land on one day so no overtrading line either -> empty block.
    recs = []
    for i in range(2):
        recs += [_lot_buy(f"W{i}", 100.0, 0), _lot_sell(f"W{i}", 110.0, 1)]
        recs += [_lot_buy(f"L{i}", 100.0, 0), _lot_sell(f"L{i}", 90.0, 5)]
    assert behavior_diagnostics(_ledger_of(recs)) == []


# -- episode-opening conviction/composite (weekly auto-tuner inputs) --------- #
def test_opening_conviction_survives_partial_exit_and_topup():
    # Buy opens at 0.45, a later top-up buys more at 0.70 — a scale-out consumes
    # the OPENING lot FIFO, so both resulting trips' opening_conviction must
    # stay 0.45 (the entry's own number), never drift to the top-up's.
    recs = [
        TradeRecord(symbol="SPCX", action="buy", qty=1.0, conviction=0.45,
                    composite_score=0.66, ts=_T0),
        TradeRecord(symbol="SPCX", action="buy", qty=1.0, conviction=0.70,
                    composite_score=1.10, ts=_T0 + timedelta(hours=1)),
        _sell("SPCX", 3.0, 2, reason="scale", qty=1.0),   # consumes the OPENING lot
        _sell("SPCX", -8.0, 3, reason="stop", qty=1.0),   # remainder, full close
    ]
    trips = round_trips(recs)
    assert len(trips) == 2
    assert all(abs(t.opening_conviction - 0.45) < 1e-9 for t in trips)
    assert all(abs(t.opening_composite - 0.66) < 1e-9 for t in trips)


def test_opening_conviction_none_when_opener_unrecorded():
    # conviction 0.0 (core fills, pre-tracking) -> None, same sentinel as the
    # existing mean-conviction field.
    recs = [_buy("A", ["technical"], 0), _sell("A", 5.0, 1)]
    trip = round_trips(recs)[0]
    assert trip.opening_conviction is None
    assert trip.opening_composite is None


def test_opening_conviction_resets_across_episodes():
    # A full close then a fresh re-entry: the SECOND episode's opening numbers
    # must be its OWN entry, not the first episode's leftover.
    recs = [
        TradeRecord(symbol="A", action="buy", qty=1.0, conviction=0.40, ts=_T0),
        _sell("A", 5.0, 1, reason="take", qty=1.0),
        TradeRecord(symbol="A", action="buy", qty=1.0, conviction=0.80,
                    ts=_T0 + timedelta(hours=2)),
        _sell("A", -3.0, 3, reason="stop", qty=1.0),
    ]
    trips = round_trips(recs)
    assert len(trips) == 2
    assert abs(trips[0].opening_conviction - 0.40) < 1e-9
    assert abs(trips[1].opening_conviction - 0.80) < 1e-9


def test_realized_pl_and_exit_ts_carried():
    ts_exit = _T0 + timedelta(hours=1)
    recs = [
        TradeRecord(symbol="A", action="buy", qty=1.0, ts=_T0),
        TradeRecord(symbol="A", action="sell", realized_pl_pct=5.0,
                    realized_pl=123.45, ts=ts_exit),
    ]
    trip = round_trips(recs)[0]
    assert trip.realized_pl == 123.45
    assert trip.exit_ts == str(ts_exit)


def test_new_roundtrip_fields_default_for_backcompat():
    from investment_strategy.attribution import RoundTrip
    trip = RoundTrip(symbol="X", pl_pct=1.0, signals=[])
    assert trip.opening_conviction is None
    assert trip.opening_composite is None
    assert trip.realized_pl is None
    assert trip.exit_ts == ""


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
