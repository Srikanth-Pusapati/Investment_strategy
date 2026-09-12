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
from unittest.mock import patch

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
                                    exit_price=110.0, realized_pl_pct=10.0))
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


def test_composite_score_round_trips_and_defaults_none():
    from investment_strategy.ledger import TradeRecord as TR
    rec = TR(symbol="AAPL", action="buy", composite_score=0.42)
    back = TR.model_validate_json(rec.model_dump_json())
    assert back.composite_score == 0.42
    # Old ledger rows (no field) keep loading — backward compatible.
    old = TR.model_validate_json('{"symbol": "AAPL", "action": "buy"}')
    assert old.composite_score is None


# ---- Aug-23 measurement integrity: append-path SELL validation -------------- #

def test_sell_with_corrupt_symbol_is_rejected():
    """The Aug-17 MLEG parent wrote symbol="None" — such a row must never
    reach the file again (LEDGER REJECT path)."""
    led = _ledger()
    for bad in ("None", "", "  "):
        led.record(TradeRecord.for_sell(
            bad, "backfill", f"oid-{bad!r}", qty=1.0,
            realized_pl_pct=1.0, realized_pl=1.0,
        ))
    assert led.all() == []                     # nothing was written


def test_sell_with_no_realized_outcome_is_rejected():
    """A SELL with realized_pl AND realized_pl_pct both null poisons
    sum(realized_pl) — refuse the record."""
    led = _ledger()
    led.record(TradeRecord.for_sell("AAPL", "exit", "s1", qty=10.0))
    assert led.all() == []


def test_sell_realized_pl_derived_from_pct_price_qty():
    """Trim-style sells (regime trim / core defense) send realized_pl=None
    with pct+price+qty — the append path derives the dollars."""
    led = _ledger()
    led.record(TradeRecord.for_sell(
        "AAPL", "regime trim", "s1", qty=10.0,
        realized_pl_pct=10.0, exit_price=110.0,   # basis 100 -> +$10/sh
    ))
    rows = led.all()
    assert len(rows) == 1
    assert rows[0].realized_pl == 100.0


def test_sell_realized_pl_derivation_uses_option_multiplier():
    led = _ledger()
    led.record(TradeRecord.for_sell(
        "AAPL260918C00150000", "watchdog option stop (AAPL260918C00150000)",
        "s1", qty=2.0, realized_pl_pct=-50.0, exit_price=1.0,
        instrument="option",                       # basis 2.0 -> -$1/sh x100 x2
    ))
    rows = led.all()
    assert rows[0].realized_pl == -200.0
    assert rows[0].underlying == "AAPL"            # parsed off the OCC symbol


def test_sell_derivation_refuses_junk_pct_below_minus_100():
    """pct <= -100 implies a zero/negative basis (junk one-sided quote) — no
    dollar figure is fabricated, but the row still lands (pct is an outcome)."""
    led = _ledger()
    led.record(TradeRecord.for_sell(
        "AAPL", "junk quote", "s1", qty=5.0,
        realized_pl_pct=-150.0, exit_price=1.0,
    ))
    rows = led.all()
    assert len(rows) == 1 and rows[0].realized_pl is None


# ---- Aug-23 OCC ledgering: option rows keyed by contract -------------------- #

def _option_decision(legs, strategy, symbol="AMZN"):
    from investment_strategy.models import (
        Action, Instrument, OptionLeg, OptionStrategy, RiskDecision,
        RiskVerdict, TradeProposal,
    )
    prop = TradeProposal(
        symbol=symbol, action=Action.BUY, conviction=0.6,
        target_weight_pct=2.0, rationale="test",
        instrument=Instrument.OPTION,
        option_strategy=OptionStrategy(strategy),
        option_legs=[OptionLeg(**l) for l in legs],
    )
    return RiskDecision(
        proposal=prop, verdict=RiskVerdict.APPROVED,
        approved_qty=2.0, approved_notional=500.0,
    )


def test_from_option_single_leg_keys_by_occ_and_keeps_underlying():
    d = _option_decision(
        [{"expiry": "2026-09-18", "strike": 230.0, "right": "call", "side": "buy"}],
        "long_call",
    )
    rec = TradeRecord.from_option(d, premium=2.5, order_id="o1")
    assert rec.symbol == "AMZN260918C00230000"
    assert rec.underlying == "AMZN"
    assert rec.occ_symbols == ["AMZN260918C00230000"]
    assert rec.instrument == "option"


def test_from_option_multi_leg_keeps_underlying_key_with_all_occs():
    d = _option_decision(
        [{"expiry": "2026-09-18", "strike": 230.0, "right": "call", "side": "buy"},
         {"expiry": "2026-09-18", "strike": 245.0, "right": "call", "side": "sell"}],
        "bull_call_spread",
    )
    rec = TradeRecord.from_option(d, premium=1.2, order_id="o1")
    assert rec.symbol == "AMZN"                # no single OCC names a spread
    assert rec.underlying == "AMZN"
    assert rec.occ_symbols == [
        "AMZN260918C00230000", "AMZN260918C00245000",
    ]


def test_option_sell_rekeys_to_occ_when_entry_is_occ_ledgered():
    """Watchdog exits ledger under the underlying with the OCC list in the
    rationale; when the entry was OCC-keyed the sell must follow it so the
    round-trip pairs."""
    led = _ledger()
    d = _option_decision(
        [{"expiry": "2026-09-18", "strike": 230.0, "right": "call", "side": "buy"}],
        "long_call",
    )
    led.record(TradeRecord.from_option(d, premium=2.5, order_id="o1"))
    led.record(TradeRecord.for_sell(
        "AMZN", "watchdog option stop (AMZN260918C00230000)", "o2",
        qty=2.0, realized_pl_pct=-50.0, realized_pl=-250.0,
        exit_reason="stop", instrument="option",
    ))
    rows = led.all()
    assert rows[1].symbol == "AMZN260918C00230000"
    assert rows[1].underlying == "AMZN"


def test_option_sell_keeps_underlying_key_for_legacy_entries():
    """Entries ledgered under the underlying (pre-Aug-23 rows, spreads) keep
    their sell under the underlying — the re-key only follows an OCC entry."""
    led = _ledger()
    led.record(_buy(symbol="PFE", oid="o1"))   # legacy-style entry key
    led.record(TradeRecord.for_sell(
        "PFE", "watchdog option stop (PFE260918C00025000)", "o2",
        qty=3.0, realized_pl_pct=-40.0, realized_pl=-120.0,
        exit_reason="stop", instrument="option",
    ))
    rows = led.all()
    assert rows[1].symbol == "PFE"
    assert rows[1].occ_symbols == ["PFE260918C00025000"]


def test_occ_rekeyed_rows_still_pair_in_attribution_round_trips():
    """Read-path integrity pin (Aug-23 review): the OCC re-key exists so
    BUY/SELL symbol pairing survives into attribution — assert it END-TO-END
    over led.effective(), not just the written symbol. Three shapes:
    (a) OCC-keyed single-leg entry + watchdog-rationale sell (re-keyed),
    (b) spread entry under the underlying + multi-OCC group-exit sell
        (must NOT re-key — two legs, one structure),
    (c) legacy single-leg pair both under the underlying (no OCC entry, so
        the re-key must leave it alone).
    Exactly 3 trips, each with its own P&L, no phantom/unpaired rows."""
    from investment_strategy.attribution import round_trips

    led = _ledger()
    # (a) OCC-keyed long call entry; watchdog-style stop sell.
    d = _option_decision(
        [{"expiry": "2026-09-18", "strike": 230.0, "right": "call", "side": "buy"}],
        "long_call",
    )
    led.record(TradeRecord.from_option(d, premium=2.5, order_id="a1"))
    led.record(TradeRecord.for_sell(
        "AMZN", "watchdog option stop (AMZN260918C00230000)", "a2",
        qty=2.0, realized_pl_pct=-50.0, realized_pl=-250.0,
        exit_reason="stop", instrument="option",
    ))
    # (b) spread entry keyed under the underlying; group-exit sell whose
    # rationale names BOTH legs (the 4-leg-MLEG-cap watchdog shape).
    d2 = _option_decision(
        [{"expiry": "2026-09-18", "strike": 230.0, "right": "call", "side": "buy"},
         {"expiry": "2026-09-18", "strike": 245.0, "right": "call", "side": "sell"}],
        "bull_call_spread",
    )
    led.record(TradeRecord.from_option(d2, premium=1.2, order_id="b1"))
    led.record(TradeRecord.for_sell(
        "AMZN",
        "watchdog option take (AMZN260918C00230000, AMZN260918C00245000)",
        "b2", qty=2.0, realized_pl_pct=100.0, realized_pl=240.0,
        exit_reason="take", instrument="option",
    ))
    # (c) legacy pre-Aug-23 pair: entry AND sell keyed under the underlying.
    led.record(TradeRecord(
        symbol="PFE", action="buy", instrument="option", qty=3.0,
        entry_price=0.5, cost_usd=150.0, order_id="c1",
    ))
    led.record(TradeRecord.for_sell(
        "PFE", "watchdog option stop (PFE260918C00025000)", "c2",
        qty=3.0, realized_pl_pct=-40.0, realized_pl=-120.0,
        exit_reason="stop", instrument="option",
    ))
    trips = round_trips(led.effective())
    assert len(trips) == 3, [
        (t.symbol, t.realized_pl) for t in trips
    ]
    by_pl = {round(t.realized_pl or 0.0): t for t in trips}
    assert set(by_pl) == {-250, 240, -120}
    assert by_pl[-250].symbol == "AMZN260918C00230000"   # re-keyed pair
    assert by_pl[240].symbol == "AMZN"                   # spread stays grouped
    assert by_pl[-120].symbol == "PFE"                   # legacy stays paired
    assert all(t.instrument == "option" for t in trips)
    # The AMZN spread's loss/win is counted exactly once — no phantom trip
    # from the multi-OCC rationale leaking into the single-leg OCC key.
    assert sum(1 for t in trips if t.symbol.startswith("AMZN")) == 2


def test_new_fields_are_backward_compatible():
    from investment_strategy.ledger import TradeRecord as TR
    old = TR.model_validate_json('{"symbol": "AAPL", "action": "buy"}')
    assert old.underlying is None
    assert old.occ_symbols == []
    assert old.repair_note is None


# ---- run-7 B2: set_fill restates equity SELL rows at the broker's fill ----- #
# Run-6 item 1e stamped fill_price/fill_qty/fill_ts as a pure annotation and
# left realized_pl at the submission-time quote, so 7/14 closed run-6 rows
# were net -$72.24 off the fills (PSQ hedge_unwind read +$41.76 at the quote,
# -$61.07 filled). The row's exit figures AS RECORDED survive as quote_*.

_PSQ = dict(qty=10283.17942229, realized_pl=41.759991, realized_pl_pct=0.016,
            exit_price=25.85)   # the real Sep 9 hedge_unwind row


def _sell(symbol="PSQ", oid="s1", instrument="equity", **kw):
    fields = dict(_PSQ) if instrument == "equity" and not kw else kw
    return TradeRecord.for_sell(
        symbol, "exit", oid, instrument=instrument, exit_reason="hedge_unwind",
        **fields,
    )


def test_set_fill_restates_equity_sell_at_the_broker_fill():
    led = TradeLedger(path=_ledger().path, restate_at_fill=True)
    led.record(_sell())
    assert led.set_fill("s1", 25.84, _PSQ["qty"]) is True
    row = led.effective()[0]
    assert row.fill_price == 25.84 and row.fill_qty == _PSQ["qty"]
    assert row.exit_price == 25.84
    assert round(row.realized_pl, 2) == -61.07              # was +41.76 at the quote
    assert round(row.realized_pl_pct, 3) == -0.023
    assert row.quote_exit_price == 25.85
    assert row.quote_realized_pl == 41.759991
    assert row.quote_realized_pl_pct == 0.016
    assert round(row.realized_pl - row.quote_realized_pl, 2) == -102.83  # slippage
    assert row.qty == _PSQ["qty"]                           # qty never touched
    assert len(led.all()) == 1                              # in place, no new row


def test_set_fill_restatement_is_a_pure_function_of_the_quote_figures():
    """A refined fill re-restates from the AS-RECORDED figures — the second
    stamp must equal a fresh single stamp at that price, never compound."""
    led = TradeLedger(path=_ledger().path, restate_at_fill=True)
    led.record(_sell())
    led.set_fill("s1", 25.84, _PSQ["qty"])
    led.set_fill("s1", 25.83, _PSQ["qty"])
    twice = led.effective()[0]
    once = TradeLedger(path=_ledger().path, restate_at_fill=True)
    once.record(_sell())
    once.set_fill("s1", 25.83, _PSQ["qty"])
    fresh = once.effective()[0]
    for f in ("exit_price", "realized_pl", "realized_pl_pct", "quote_exit_price",
              "quote_realized_pl", "quote_realized_pl_pct"):
        assert getattr(twice, f) == getattr(fresh, f), f
    assert twice.quote_exit_price == 25.85 and round(twice.realized_pl, 2) == -163.90
    # same price again: idempotent
    led.set_fill("s1", 25.83, _PSQ["qty"])
    assert led.effective()[0] == twice


def test_set_fill_leaves_option_sell_rows_at_the_recorded_figures():
    """Option rows keep the run-6 annotation-only convention: a multi-leg
    filled_avg_price is a per-spread net debit/credit that does not map onto
    the group's realized_pl, so we don't guess."""
    led = TradeLedger(path=_ledger().path, restate_at_fill=True)
    led.record(TradeRecord.for_sell(
        "AAPL", "watchdog option stop (AAPL260918C00150000,AAPL260918C00160000)",
        "o1", qty=2.0, realized_pl_pct=-50.0, realized_pl=-200.0, exit_price=1.0,
        instrument="option",
    ))
    assert led.set_fill("o1", 1.1, 2.0) is True
    row = led.effective()[0]
    assert row.fill_price == 1.1 and row.fill_qty == 2.0    # annotated ...
    assert row.exit_price == 1.0 and row.realized_pl == -200.0   # ... not restated
    assert row.realized_pl_pct == -50.0
    assert row.quote_exit_price is None and row.quote_realized_pl is None


def test_set_fill_knob_off_keeps_the_run6_annotation_only_behaviour():
    # constructor arg
    led = TradeLedger(path=_ledger().path, restate_at_fill=False)
    led.record(_sell())
    assert led.set_fill("s1", 25.84, _PSQ["qty"]) is True
    row = led.effective()[0]
    assert row.fill_price == 25.84                          # stamped ...
    assert row.exit_price == 25.85 and row.realized_pl == 41.759991   # ... untouched
    assert row.quote_exit_price is None
    # env key (what the bare TradeLedger() the orchestrator builds resolves)
    with patch.dict(os.environ, {"LEDGER_RESTATE_AT_FILL": "off"}):
        assert TradeLedger(path=_ledger().path).restate_at_fill is False
    with patch.dict(os.environ, {"LEDGER_RESTATE_AT_FILL": "on"}):
        assert TradeLedger(path=_ledger().path).restate_at_fill is True
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("LEDGER_RESTATE_AT_FILL", None)
        assert TradeLedger(path=_ledger().path).restate_at_fill is True   # default on
    # per-call override in both directions
    led2 = TradeLedger(path=_ledger().path, restate_at_fill=False)
    led2.record(_sell())
    led2.set_fill("s1", 25.84, _PSQ["qty"], restate=True)
    assert round(led2.effective()[0].realized_pl, 2) == -61.07
    led3 = TradeLedger(path=_ledger().path, restate_at_fill=True)
    led3.record(_sell())
    led3.set_fill("s1", 25.84, _PSQ["qty"], restate=False)
    assert led3.effective()[0].realized_pl == 41.759991


def test_set_fill_never_touches_buy_rows_qty_or_cost():
    led = TradeLedger(path=_ledger().path, restate_at_fill=True)
    led.record(_buy(qty=10.0, entry=100.0, oid="b1"))
    led.record(TradeRecord.for_sell("AAPL", "exit", "s1", qty=10.0,
                                    realized_pl_pct=5.0, realized_pl=50.0,
                                    exit_price=105.0))
    led.set_fill("b1", 100.37, 10.0)
    led.set_fill("s1", 105.5, 10.0)
    rows = {r.order_id: r for r in led.effective()}
    b, s = rows["b1"], rows["s1"]
    assert b.fill_price == 100.37 and b.entry_price == 100.0 and b.cost_usd == 1000.0
    assert b.realized_pl is None and b.quote_exit_price is None
    assert s.qty == 10.0 and s.cost_usd == 0.0
    assert s.exit_price == 105.5 and s.realized_pl == 55.0 and s.realized_pl_pct == 5.5
    assert s.quote_exit_price == 105.0 and s.quote_realized_pl == 50.0


def test_set_fill_skips_restatement_when_the_row_has_no_usable_basis():
    """Rows the restatement cannot honestly recompute keep their numbers:
    no realized $ (junk-pct row), qty 0 (legacy full-close sells), or a
    basis <= 0 (realized $ that was not computed over the row's qty)."""
    led = TradeLedger(path=_ledger().path, restate_at_fill=True)
    led.record(TradeRecord.for_sell("AAPL", "junk quote", "s1", qty=5.0,
                                    realized_pl_pct=-150.0, exit_price=1.0))
    led.record(TradeRecord.for_sell("MSFT", "legacy full close", "s2", qty=0.0,
                                    realized_pl_pct=2.0, realized_pl=20.0,
                                    exit_price=50.0))
    led.record(TradeRecord.for_sell("NVDA", "group $ on a per-share row", "s3",
                                    qty=10.0, realized_pl_pct=1.0,
                                    realized_pl=50.0, exit_price=1.0))
    for oid, px in (("s1", 1.1), ("s2", 50.5), ("s3", 1.1)):
        assert led.set_fill(oid, px, 5.0) is True
    rows = {r.order_id: r for r in led.effective()}
    assert rows["s1"].fill_price == 1.1 and rows["s1"].realized_pl is None
    assert rows["s1"].exit_price == 1.0 and rows["s1"].quote_exit_price is None
    assert rows["s2"].exit_price == 50.0 and rows["s2"].realized_pl == 20.0
    assert rows["s3"].exit_price == 1.0 and rows["s3"].realized_pl == 50.0
    assert all(rows[o].quote_realized_pl is None for o in ("s1", "s2", "s3"))


def test_effective_scales_restated_and_quote_dollars_together_on_a_partial():
    led = TradeLedger(path=_ledger().path, restate_at_fill=True)
    led.record(TradeRecord.for_sell("AAPL", "exit", "s1", qty=10.0,
                                    realized_pl_pct=5.0, realized_pl=50.0,
                                    exit_price=105.0))
    led.set_fill("s1", 106.0, 10.0)                # +$1/sh on the ROW qty
    led.record(TradeRecord.correction("s1", "AAPL", "canceled", 5.0, 10.0))
    eff = led.effective()[0]
    assert eff.qty == 5.0
    assert eff.realized_pl == 30.0                 # 60 x 5/10
    assert eff.quote_realized_pl == 25.0           # 50 x 5/10
    assert eff.realized_pl_pct == 6.0 and eff.quote_realized_pl_pct == 5.0
    assert round(eff.realized_pl - eff.quote_realized_pl, 2) == 5.0   # 1 x 5 sh


def test_restated_exit_price_feeds_fifo_lot_pl():
    from investment_strategy.lots import build_lot_history
    led = TradeLedger(path=_ledger().path, restate_at_fill=True)
    led.record(_buy(qty=10.0, entry=100.0, oid="b1"))
    led.record(TradeRecord.for_sell("AAPL", "exit", "s1", qty=10.0,
                                    realized_pl_pct=5.0, realized_pl=50.0,
                                    exit_price=105.0))
    led.set_fill("s1", 104.0, 10.0)
    _open, realized = build_lot_history(led.effective())
    assert len(realized) == 1 and realized[0].exit_price == 104.0
    assert not realized[0].basis_estimated


def test_quote_fields_default_none_on_rows_predating_them():
    old = TradeRecord.model_validate_json(
        '{"symbol": "AAPL", "action": "sell", "realized_pl": 1.0, '
        '"exit_price": 10.0, "fill_price": 10.1}')
    assert old.quote_exit_price is None
    assert old.quote_realized_pl is None
    assert old.quote_realized_pl_pct is None


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
