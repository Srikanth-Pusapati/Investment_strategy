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


# ---- run-7 4a-15 / 4a-16: decision-time shadow fields on BUY rows ---------- #
# Measurement only. 4a-15 (the REFUTED red-tape haircut) and 4a-16 (the
# do-not-do 6% clamp floor) each get an ex-ante column so a later window can
# re-open them on a clean sample; nothing here sizes, stops or sells anything.

from investment_strategy.ledger import (  # noqa: E402
    SHADOW_STOP_FLOOR_PCT, EntryTape, floor_would_survive, floor_survival_at_exit,
    shadow_stop_pct, would_haircut_usd,
)
from investment_strategy.models import (  # noqa: E402
    Action, RiskDecision, RiskVerdict, TradeProposal,
)


def _approved_buy(symbol="NU", notional=30_916.0, stop=5.71, take=14.28):
    prop = TradeProposal(symbol=symbol, action=Action.BUY, conviction=0.7,
                         target_weight_pct=3.0, rationale="test")
    return RiskDecision(proposal=prop, verdict=RiskVerdict.APPROVED,
                        approved_qty=notional / 100.0, approved_notional=notional,
                        stop_loss_pct=stop, take_profit_pct=take,
                        reason="Sized within caps.")


def test_buy_row_carries_decision_context():
    # The Sep 9 2026 tape: 'Market regime: SPY above 200dma (..., today -0.4%)
    # ... -> risk-on' and two NAME FALLING reads — none of it reached the row.
    tape = EntryTape(
        spy_intraday_ret_at_decision=-0.42, regime_label="risk-on",
        breadth_narrow=False, falling_names=["DRAM", "SPCX"],
        would_haircut_usd=15_458.0, stop_pct_if_floor_6=6.0,
        vol_stop_raw_pct=2.5,
    )
    led = _ledger()
    led.record(TradeRecord.from_equity(_approved_buy(), 100.0, "buy-nu", tape=tape))
    row = led.all()[0]
    assert row.spy_intraday_ret_at_decision == -0.42
    assert row.regime_label == "risk-on"
    assert row.falling_names == ["DRAM", "SPCX"]
    assert row.would_haircut_usd == 15_458.0
    assert row.stop_pct_if_floor_6 == 6.0
    assert row.vol_stop_raw_pct == 2.5
    # The live decision is untouched by the shadow: stop/take/size as approved.
    assert row.stop_loss_pct == 5.71 and row.cost_usd == 30_916.0
    # Stop-exit fields stay None on a buy; tape=None leaves every default.
    assert row.floor6_would_survive is None and row.floor6_worst_close_pct is None
    bare = TradeRecord.from_equity(_approved_buy(), 100.0, "buy-2")
    assert bare.spy_intraday_ret_at_decision is None and bare.falling_names == []
    # The greppable line, exactly as the runbook greps it.
    assert tape.log_line("NU", 5.71) == (
        "ENTRY TAPE: NU spy_intraday=-0.42% regime=risk-on falling=2 "
        "would_haircut=$15,458 stop=5.71% stop_if_floor6=6.00%"
    )
    assert EntryTape().log_line("NU", 5.71) == (
        "ENTRY TAPE: NU spy_intraday=n/a regime=n/a falling=0 "
        "would_haircut=n/a stop=5.71% stop_if_floor6=n/a"
    )


def test_legacy_rows_without_shadow_fields_still_load():
    old_buy = TradeRecord.model_validate_json(
        '{"symbol": "NU", "action": "buy", "qty": 100, "entry_price": 12.3}')
    assert old_buy.spy_intraday_ret_at_decision is None
    assert old_buy.regime_label is None
    assert old_buy.falling_names == []
    assert old_buy.would_haircut_usd is None
    assert old_buy.stop_pct_if_floor_6 is None
    old_sell = TradeRecord.model_validate_json(
        '{"symbol": "NU", "action": "sell", "realized_pl": -80.0, '
        '"exit_price": 11.8, "exit_reason": "bracket_stop"}')
    assert old_sell.floor6_would_survive is None
    assert old_sell.floor6_worst_close_pct is None


def test_shadow_stop_equals_min_max():
    # stop = min(max(mult x sigma_d, floor), max) — risk._exit_levels with the
    # floor swapped to 6; the raw 2-sigma of a quiet name (1.25%/day -> 2.5%)
    # is lifted to the floor, a mid-vol name is untouched, a wild one is capped.
    assert shadow_stop_pct(1.25, 2.0, 6.0, 10.0) == 6.0
    assert shadow_stop_pct(3.5, 2.0, 6.0, 10.0) == 7.0
    assert shadow_stop_pct(6.0, 2.0, 6.0, 10.0) == 10.0
    # STOP_COVER_EXTENSION widens past the floor, still capped.
    assert shadow_stop_pct(1.25, 2.0, 6.0, 10.0, ext_pct=8.0) == 8.0
    assert shadow_stop_pct(1.25, 2.0, 6.0, 10.0, ext_pct=12.0) == 10.0
    assert shadow_stop_pct(1.25, 2.0, 6.0, 10.0, ext_pct=5.0) == 6.0
    # Parity with the risk layer's own arithmetic at a 6% floor, so the shadow
    # cannot drift from what a real VOL_STOP_MIN_PCT=6 would set.
    from investment_strategy.risk import _TRADING_DAYS_SQRT
    from test_risk import _limits, _rm, _buy as _rbuy
    rm = _rm(_limits(vol_stops_enabled=True, vol_stop_mult=2.0,
                     vol_stop_min_pct=SHADOW_STOP_FLOOR_PCT, vol_stop_max_pct=10.0,
                     stop_cover_extension=True))
    for vol, ext in ((0.20, None), (0.55, None), (1.10, None), (0.20, 8.0)):
        sigma_d = vol / _TRADING_DAYS_SQRT * 100.0
        tech = {"ext_pct_sma20": ext} if ext is not None else None
        live_stop, _take = rm._exit_levels(_rbuy("NU"), vol, tech)
        assert abs(shadow_stop_pct(sigma_d, 2.0, 6.0, 10.0, ext_pct=ext) - live_stop) < 1e-9


def test_would_haircut_rule_and_unknown_spy():
    # 0.5 x notional when SPY intraday <= -0.3% AND (narrow breadth OR >= 2
    # NAME FALLING); else 0; None when the SPY read is unknown.
    assert would_haircut_usd(30_916.0, -0.42, False, 2) == 15_458.0
    assert would_haircut_usd(30_916.0, -0.30, True, 0) == 15_458.0
    assert would_haircut_usd(30_916.0, -0.42, False, 1) == 0.0    # one name only
    assert would_haircut_usd(30_916.0, -0.29, False, 3) == 0.0    # tape not red
    assert would_haircut_usd(30_916.0, +0.90, True, 5) == 0.0
    assert would_haircut_usd(30_916.0, None, True, 5) is None     # degraded feed
    assert would_haircut_usd(0.0, -0.9, True, 5) == 0.0


def test_floor_would_survive_from_closes():
    # Worst close -5.5% under entry: a 6% floor would have held; -6.1% breaches.
    assert floor_would_survive(100.0, [99.0, 94.5, 97.0]) == (True, -5.5)
    assert floor_would_survive(100.0, [99.0, 93.9, 97.0]) == (False, -6.1)
    # AT the floor = touched (a resting stop fills at its level); 94/100-1 is
    # -0.06000000000000001 in binary and must not read as "under" the floor
    # either way — the comparison is on the rounded figure.
    assert floor_would_survive(100.0, [101.0, 94.0]) == (False, -6.0)
    assert floor_would_survive(100.0, [101.0, 94.01]) == (True, -5.99)
    assert floor_would_survive(100.0, []) == (None, None)
    assert floor_would_survive(100.0, [0.0, None]) == (None, None)
    assert floor_would_survive(0.0, [99.0]) == (None, None)
    # The fetch wrapper windows the series to [entry date, exit date] and asks
    # for enough bars to cover the trip; an unreadable series is (None, None).
    from datetime import datetime, timezone
    asked = []

    def _series(symbol, days):
        asked.append((symbol, days))
        return [("2026-06-30", 100.0), ("2026-07-01", 97.0), ("2026-07-02", 92.0),
                ("2026-07-03", 80.0)]   # after the exit: must not count
    entry = datetime(2026, 7, 1, 14, 0, tzinfo=timezone.utc)
    exit_ = datetime(2026, 7, 2, 15, 30, tzinfo=timezone.utc)
    assert floor_survival_at_exit(_series, "NU", entry, 100.0, exit_ts=exit_) == (False, -8.0)
    assert asked == [("NU", 6)]
    assert floor_survival_at_exit(None, "NU", entry, 100.0, exit_ts=exit_) == (None, None)

    def _boom(symbol, days):
        raise RuntimeError("bars down")
    assert floor_survival_at_exit(_boom, "NU", entry, 100.0, exit_ts=exit_) == (None, None)


# ---- run-7 4a-18: sell rows stamp lot attributes; loader drops phantoms; ---- #
# ---- option BUY cost_usd restated at fill (critic #13) ---------------------- #
# Fixtures are the real archive rows (scratchpad analysis pooled_trips.jsonl):
# AVAV Jul 7-8 (negative-qty flatten + re-marked trail dupe), BIIB Jul 15
# (correction chain), T Jul 23 (option flatten ledgered twice), the LLY Jul 9
# storm (one 0.349754-sh fraction "sold" 25 times), the HD put of Sep 2.

import importlib.util
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from investment_strategy.ledger import (  # noqa: E402
    PHANTOM_DUPE_WINDOW_H, DroppedSell, dedup_sells,
)

_U = timezone.utc


def _t(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=_U)


def _b(sym, qty, px, oid, ts, **kw):
    return TradeRecord(ts=_t(ts), symbol=sym, action="buy", qty=qty,
                       entry_price=px, cost_usd=px * qty, order_id=oid, **kw)


def _s(sym, qty, px, pl, oid, ts, reason, pct=None, **kw):
    return TradeRecord.for_sell(sym, f"{reason} exit", oid, qty=qty, exit_price=px,
                                realized_pl=pl, realized_pl_pct=pct,
                                exit_reason=reason, ts=_t(ts), **kw)


class _Capture(logging.Handler):
    """Standalone-mode friendly log capture (no pytest caplog fixture)."""
    def __init__(self):
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def _capture():
    h = _Capture()
    lg = logging.getLogger("ledger")
    lg.addHandler(h)
    lg.setLevel(logging.INFO)
    return h, (lambda: lg.removeHandler(h))


def _avav(led):
    led.record(_b("AVAV", 1.799568, 196.64, "a115", "2026-07-02T14:16:45"))
    led.record(_b("AVAV", 36.301127, 190.97, "3512", "2026-07-02T20:00:19"))
    led.record(_s("AVAV", 1.0, 175.0, -21.64, "344a", "2026-07-07T13:33:14", "bracket_stop", -11.005))
    led.record(_s("AVAV", 36.0, 173.546667, -631.77, "dfe1", "2026-07-07T13:37:09", "bracket_stop", -9.183))
    led.record(_s("AVAV", -37.0, 163.72, 365.040002, "c9e7", "2026-07-07T14:54:45", "flatten", 5.684))
    led.record(_s("AVAV", 37.0, 169.67, 212.01, "6f23", "2026-07-08T13:23:02", "trail", 3.495))
    led.record(_s("AVAV", 37.0, 169.7232, 213.9784, "bf06", "2026-07-08T13:23:38", "trail", 3.528))


def _t_option(led):
    led.record(TradeRecord(ts=_t("2026-07-23T16:33:31"), symbol="T", action="buy",
                           instrument="option", qty=900.0, entry_price=0.01,
                           cost_usd=900.0, order_id="8278"))
    led.record(TradeRecord.for_sell("T", "flatten", "cf8b", qty=900.0, realized_pl=-2700.0,
                                    realized_pl_pct=-100.0, exit_reason="flatten",
                                    instrument="option", ts=_t("2026-07-23T16:50:50")))
    led.record(TradeRecord.for_sell("T", "flatten", "b4dc", qty=900.0, realized_pl=-2700.0,
                                    realized_pl_pct=-100.0, exit_reason="flatten",
                                    instrument="option", ts=_t("2026-07-23T20:00:13")))


def _biib(led):
    led.record(_b("BIIB", 11.0, 190.0, "c01d", "2026-07-14T15:33:08"))
    led.record(_s("BIIB", 11.0, 190.5, -3.85, "757e", "2026-07-15T08:29:08", "trail", -0.183))
    led.record(TradeRecord.correction("757e", "BIIB", "replaced", 0.0, 11.0))
    led.record(_s("BIIB", 11.0, 190.22, -6.93, "2810", "2026-07-15T13:30:24", "trail", -0.33))
    led.record(TradeRecord.correction("2810", "BIIB", "replaced", 0.0, 11.0))
    led.record(_s("BIIB", 11.0, 188.11, -30.14, "74bd", "2026-07-15T13:31:57", "trail", -1.436))
    led.record(TradeRecord.correction("74bd", "BIIB", "replaced", 0.0, 11.0))
    led.record(_s("BIIB", 11.0, 187.96, -31.79, "f03a", "2026-07-15T13:32:28", "trail", -1.514))
    led.record(TradeRecord.correction("f03a", "BIIB", "replaced", 10.0, 11.0))
    led.record(_s("BIIB", 1.0, 186.84, -4.01, "a876", "2026-07-15T13:32:59", "trail", -2.101))


def _lly_storm(led, n=25):
    """One 0.349754-sh fraction, REDUCE'd every minute (Jul 9 07:21-07:45 ET);
    each submission ledgered at the tick's mark: pl moves by qty x mark."""
    qty, basis = 0.349754, 792.0
    led.record(_b("LLY", qty, basis, "lly-b", "2026-07-08T15:00:00"))
    t0 = _t("2026-07-09T11:21:40")
    for i in range(n):
        px = 800.0 + 0.05 * i
        led.record(TradeRecord.for_sell(
            "LLY", "watchdog trail", f"lly-{i:02d}", qty=qty, exit_price=px,
            realized_pl=round((px - basis) * qty, 6), realized_pl_pct=1.0,
            exit_reason="trail", ts=t0 + timedelta(minutes=i)))


def test_sell_row_stamps_entry_lot_attributes_at_close():
    led = TradeLedger(path=_ledger().path, restate_at_fill=True)
    led.record(TradeRecord(
        ts=_t("2026-09-01T14:00:00"), symbol="NU", action="buy", qty=10.0,
        entry_price=100.0, cost_usd=1000.0, order_id="b1", conviction=0.66,
        composite_score=1.42, stop_loss_pct=5.71,
        key_signals=["technical +0.8", "insider +0.4"],
    ))
    led.set_fill("b1", 100.37, 10.0)                       # broker fill on the buy
    h, off = _capture()
    try:
        led.record(_s("NU", 10.0, 110.0, 96.3, "s1", "2026-09-03T15:00:00", "decision", 9.6))
    finally:
        off()
    row = [r for r in led.effective() if r.action == "sell"][0]
    assert row.entry_ts == _t("2026-09-01T14:00:00")
    assert row.entry_fill_price == 100.37 and row.entry_fill_source == "fill"
    assert row.entry_conviction == 0.66 and row.entry_composite == 1.42
    assert row.entry_stop_pct == 5.71
    assert row.entry_key_signals == ["technical +0.8", "insider +0.4"]
    assert row.lots_n == 1
    assert row.realized_pl == 96.3 and row.qty == 10.0        # nothing else touched
    assert any(l.startswith(
        "LOT STAMP: NU decision entry_ts=2026-09-01 14:00 entry_fill=100.3700 (fill) "
        "conviction=0.66 composite=+1.42 stop=5.71% lots_n=1 key_signals=") for l in h.lines), h.lines
    # No fill stamped on the buy -> the decision quote, labelled as such.
    led2 = TradeLedger(path=_ledger().path, restate_at_fill=True)
    led2.record(_b("F", 5.0, 10.0, "b1", "2026-09-01T14:00:00", conviction=0.5))
    led2.record(_s("F", 5.0, 11.0, 5.0, "s1", "2026-09-02T14:00:00", "trail", 10.0))
    row = [r for r in led2.effective() if r.action == "sell"][0]
    assert row.entry_fill_price == 10.0 and row.entry_fill_source == "quote"
    assert row.entry_conviction == 0.5 and row.entry_stop_pct is None   # 0.0 = unrecorded
    assert row.entry_composite is None and row.entry_key_signals == []


def test_multi_lot_sell_stamps_oldest_lot_and_counts_lots():
    led = TradeLedger(path=_ledger().path, restate_at_fill=True)
    led.record(_b("NU", 10.0, 100.0, "b1", "2026-09-01T14:00:00", conviction=0.60, composite_score=1.0))
    led.record(_b("NU", 5.0, 110.0, "b2", "2026-09-02T14:00:00", conviction=0.70, composite_score=2.0))
    # scale-out of 8 sh touches only the oldest lot
    led.record(_s("NU", 8.0, 120.0, 160.0, "s1", "2026-09-03T14:00:00", "scale", 20.0))
    # the remainder (2 of b1 + 5 of b2): still the OLDEST lot's attributes, lots_n=2
    led.record(_s("NU", 7.0, 125.0, 125.0, "s2", "2026-09-04T14:00:00", "trail", 17.0))
    s1, s2 = [r for r in led.effective() if r.action == "sell"]
    assert (s1.entry_conviction, s1.entry_composite, s1.lots_n) == (0.60, 1.0, 1)
    assert (s2.entry_conviction, s2.entry_composite, s2.lots_n) == (0.60, 1.0, 2)
    assert s2.entry_ts == _t("2026-09-01T14:00:00")
    # legacy full close with unknown qty (qty=0) = every open lot
    led.record(_b("NU", 3.0, 130.0, "b3", "2026-09-05T14:00:00", conviction=0.9))
    led.record(_b("NU", 3.0, 131.0, "b4", "2026-09-05T15:00:00", conviction=0.8))
    led.record(TradeRecord.for_sell("NU", "legacy", "s3", qty=0.0, realized_pl_pct=1.0,
                                    realized_pl=8.0, exit_price=132.0, exit_reason="decision",
                                    ts=_t("2026-09-06T14:00:00")))
    s3 = [r for r in led.effective() if r.order_id == "s3"][0]
    assert s3.lots_n == 2 and s3.entry_conviction == 0.9


def test_sell_without_ledger_lot_stamps_lots_n_zero_not_a_guess():
    led = TradeLedger(path=_ledger().path, restate_at_fill=True)
    h, off = _capture()
    try:
        led.record(_s("MSFT", 4.0, 400.0, 20.0, "s1", "2026-09-03T15:00:00", "external", 1.0))
    finally:
        off()
    row = led.effective()[0]
    assert row.lots_n == 0 and row.entry_ts is None and row.entry_fill_price is None
    assert row.entry_conviction is None and row.entry_key_signals == []
    assert any("LOT STAMP: MSFT external has no ledger lot" in l for l in h.lines)


def test_option_sell_stamps_from_the_option_buy_row():
    led = TradeLedger(path=_ledger().path, restate_at_fill=True)
    d = _option_decision([{"expiry": "2026-10-16", "strike": 350.0, "right": "put",
                           "side": "buy"}], "long_put", symbol="HD")
    d.proposal.conviction = 0.62
    d.proposal.key_signals = ["technical -0.54 downtrend", "insider -1.00"]
    rec = TradeRecord.from_option(d, premium=32.13, order_id="hd1")
    assert rec.symbol == "HD261016P00350000"
    # the real row: ONE contract (the helper's decision approves 2)
    led.record(rec.model_copy(update={"ts": _t("2026-09-02T13:34:54"), "qty": 1.0,
                                      "cost_usd": 3213.0}))
    led.set_fill("hd1", 34.3, 1.0)
    # watchdog-style group close under the underlying; _validate_sell re-keys
    # it to the OCC (entry is OCC-ledgered) and the stamp finds the buy.
    led.record(TradeRecord.for_sell(
        "HD", "watchdog option stop (HD261016P00350000)", "hd-x", qty=1.0,
        realized_pl_pct=-30.0, realized_pl=-1029.0, exit_reason="stop",
        instrument="option", ts=_t("2026-09-10T15:00:00")))
    row = [r for r in led.effective() if r.action == "sell"][0]
    assert row.symbol == "HD261016P00350000"
    assert row.entry_ts == _t("2026-09-02T13:34:54")
    assert row.entry_fill_price == 34.3 and row.entry_fill_source == "fill"
    assert row.entry_conviction == 0.62 and row.lots_n == 1
    assert row.entry_key_signals == ["technical -0.54 downtrend", "insider -1.00"]
    assert row.entry_stop_pct is None                        # options carry no % stop
    # a second close on the same key after the full close finds nothing open
    led.record(TradeRecord.for_sell(
        "HD", "watchdog option stop (HD261016P00350000)", "hd-y", qty=1.0,
        realized_pl_pct=-30.0, realized_pl=-1029.0, exit_reason="flatten",
        instrument="option", ts=_t("2026-09-11T15:00:00")))
    assert [r for r in led.effective() if r.order_id == "hd-y"][0].lots_n == 0


def test_effective_drops_avav_negative_qty_flatten_and_remarked_trail_dupe():
    led = TradeLedger(path=_ledger().path, restate_at_fill=True)
    _avav(led)
    raw = led.effective(dedup=False)
    eff = led.effective()
    assert len(raw) == 7 and len(eff) == 5
    dropped = led.phantoms()
    assert [(d.record.order_id, d.rule, d.kept_order_id) for d in dropped] == [
        ("c9e7", "negative_qty", None), ("6f23", "replaced_dupe", "bf06")]
    assert [r.order_id for r in eff if r.action == "sell"] == ["344a", "dfe1", "bf06"]
    kept_sum = sum(r.realized_pl for r in eff if r.action == "sell")
    assert round(kept_sum, 2) == round(-21.64 - 631.77 + 213.9784, 2)
    assert round(sum(d.record.realized_pl for d in dropped), 2) == 577.05  # would-be double count
    # the re-mark identity that makes 6f23 a dupe of bf06: dPL == qty x dPX
    assert abs((213.9784 - 212.01) - 37.0 * (169.7232 - 169.67)) < 1e-6
    assert all(isinstance(d, DroppedSell) for d in dropped)
    assert dropped[1].line() == (
        "AVAV equity trail 2026-07-08 13:23 qty=37 $+212.01 [replaced_dupe] "
        "(exit carried by order bf06)")


def test_effective_drops_t_option_flatten_ledgered_twice():
    led = TradeLedger(path=_ledger().path, restate_at_fill=True)
    _t_option(led)
    eff = led.effective()
    sells = [r for r in eff if r.action == "sell"]
    assert [r.order_id for r in sells] == ["b4dc"]               # the resubmit that filled
    assert sum(r.realized_pl for r in sells) == -2700.0           # counted ONCE
    assert [(d.record.order_id, d.rule) for d in led.phantoms()] == [("cf8b", "replaced_dupe")]


def test_biib_correction_chain_composes_with_dedup():
    """Corrections (by order id) and the phantom rule are two layers: the
    three voided trail rows never reach dedup, the resized 10-sh row and the
    1-sh remainder differ in qty so nothing is dropped, and FIFO consumes
    exactly the 11-sh lot."""
    led = TradeLedger(path=_ledger().path, restate_at_fill=True)
    _biib(led)
    eff = led.effective()
    sells = [r for r in eff if r.action == "sell"]
    assert [(r.order_id, r.qty) for r in sells] == [("f03a", 10.0), ("a876", 1.0)]
    assert round(sells[0].realized_pl, 2) == round(-31.79 * 10 / 11, 2)
    assert led.phantoms() == []
    from investment_strategy.lots import build_lot_history
    open_lots, realized = build_lot_history(eff)
    assert open_lots == {} and sum(r.qty for r in realized) == 11.0


def test_lly_storm_fraction_rows_collapse_to_one():
    led = TradeLedger(path=_ledger().path, restate_at_fill=True)
    _lly_storm(led)
    eff = led.effective()
    sells = [r for r in eff if r.action == "sell"]
    assert len(sells) == 1 and sells[0].order_id == "lly-24"
    dropped = led.phantoms()
    assert len(dropped) == 24 and {d.rule for d in dropped} == {"replaced_dupe"}
    assert [d.kept_order_id for d in dropped] == [f"lly-{i:02d}" for i in range(1, 25)]


def test_dedup_keeps_legitimate_same_qty_sells():
    def _pair(gap_h, *, filled_first=False, qty2=5.0, reason2="trail", pl2=10.0,
              px2=12.0, qty1=5.0):
        led = TradeLedger(path=_ledger().path, restate_at_fill=True)
        led.record(_b("X", 20.0, 10.0, "b", "2026-09-01T14:00:00"))
        kw = {"fill_price": 12.0, "fill_qty": qty1} if filled_first else {}
        led.record(_s("X", qty1, 12.0, 10.0, "s1", "2026-09-02T14:00:00", "trail", 20.0, **kw))
        t2 = (_t("2026-09-02T14:00:00") + timedelta(hours=gap_h)).isoformat()
        led.record(_s("X", qty2, px2, pl2, "s2", t2[:19], reason2, 20.0))
        return [r.order_id for r in led.effective() if r.action == "sell"]
    assert _pair(3.9) == ["s2"]                       # inside the window: dupe
    assert _pair(4.1) == ["s1", "s2"]                 # outside: two real trims
    assert _pair(0.5, filled_first=True) == ["s1", "s2"]   # a confirmed fill is never a phantom
    assert _pair(0.5, qty2=6.0, pl2=12.0) == ["s1", "s2"]  # different qty
    assert _pair(0.5, reason2="scale") == ["s1", "s2"]     # different reason
    assert _pair(0.5, pl2=11.0, px2=12.0) == ["s1", "s2"]  # same mark, different $: not a re-mark
    # BRK.B-style flatten split (7 sh then 16 sh seconds apart): both real
    led = TradeLedger(path=_ledger().path, restate_at_fill=True)
    led.record(_b("BRK.B", 23.0, 100.0, "b", "2026-07-20T14:00:00"))
    led.record(_s("BRK.B", 7.0, 99.0, -7.0, "f1", "2026-07-23T14:00:00", "flatten", -1.0))
    led.record(_s("BRK.B", 16.0, 99.0, -16.0, "f2", "2026-07-23T14:00:05", "flatten", -1.0))
    assert len([r for r in led.effective() if r.action == "sell"]) == 2
    # qty == 0 legacy full-close rows are kept (deliberate: "qty <= 0" in the
    # contract text would void the pre-qty shape lots.py reads as a full close)
    led = TradeLedger(path=_ledger().path, restate_at_fill=True)
    led.record(TradeRecord.for_sell("OLD", "legacy", "z", qty=0.0, realized_pl_pct=1.0,
                                    realized_pl=5.0, exit_price=10.0,
                                    ts=_t("2026-06-26T14:00:00")))
    assert [r.order_id for r in led.effective()] == ["z"] and led.phantoms() == []
    assert dedup_sells([]) == ([], [])


def test_ledger_phantoms_logged_once_per_distinct_set_with_the_counterfactual():
    led = TradeLedger(path=_ledger().path, restate_at_fill=True)
    h, off = _capture()
    try:
        _avav(led)           # the set GROWS as phantoms land: logged as it grows
    finally:
        off()
    # (the trail dupe only becomes one when its replacement lands, and the
    # stamp reads the file BEFORE the append — so the build sees "dropped 1")
    grow = [l for l in h.lines if l.startswith("LEDGER PHANTOMS:")]
    assert [l.split(" SELL")[0] for l in grow] == ["LEDGER PHANTOMS: dropped 1"]
    # a fresh process (new instance) on the same file: once, not per read
    fresh = TradeLedger(path=led.path, restate_at_fill=True)
    h, off = _capture()
    try:
        fresh.effective(); fresh.effective(); fresh.phantoms()
    finally:
        off()
    lines = [l for l in h.lines if l.startswith("LEDGER PHANTOMS:")]
    assert len(lines) == 1, lines
    assert lines[0] == (
        "LEDGER PHANTOMS: dropped 2 SELL row(s) worth $+577.05 that the realized "
        "sum would otherwise carry (kept 5 rows): AVAV equity flatten 2026-07-07 14:54 "
        "qty=-37 $+365.04 [negative_qty]; AVAV equity trail 2026-07-08 13:23 qty=37 "
        "$+212.01 [replaced_dupe] (exit carried by order bf06)")
    # long lists are capped at 10 rows in the log (phantoms() has them all)
    _lly_storm(led)
    fresh = TradeLedger(path=led.path, restate_at_fill=True)
    h, off = _capture()
    try:
        fresh.effective()
    finally:
        off()
    lines = [l for l in h.lines if l.startswith("LEDGER PHANTOMS:")]
    assert len(lines) == 1 and "dropped 26 SELL row(s)" in lines[0]
    assert lines[0].endswith("; ... and 16 more (ledger.phantoms() lists all)")
    assert len(fresh.phantoms()) == 26


def test_set_fill_restates_option_buy_cost_at_fill_hd_put():
    """HD261016P00350000, 2026-09-02: ledgered at the proposal's 32.13 x 100
    = $3,213 debit, filled at 34.30 = $3,430 (critic #13)."""
    led = TradeLedger(path=_ledger().path, restate_at_fill=True)
    led.record(TradeRecord(symbol="HD261016P00350000", action="buy", instrument="option",
                           qty=1.0, entry_price=32.13, cost_usd=3213.0000000000005,
                           order_id="hd1", underlying="HD",
                           occ_symbols=["HD261016P00350000"]))
    h, off = _capture()
    try:
        assert led.set_fill("hd1", 34.3, 1.0) is True
    finally:
        off()
    row = led.effective()[0]
    assert row.cost_usd == 3430.0 and row.quote_cost_usd == 3213.0
    assert row.entry_price == 32.13 and row.fill_price == 34.3      # quote kept, fill stamped
    assert row.qty == 1.0 and row.realized_pl is None
    assert any(l == ("Ledger: RESTATED OPTION BUY HD261016P00350000 cost at fill 34.3000 "
                     "(quote 32.1300): $3213.00 -> $3430.00 (+217.00) [order hd1]")
               for l in h.lines), h.lines
    # pure function of the as-recorded cost: a refined fill re-restates, never compounds
    led.set_fill("hd1", 33.0, 1.0)
    row = led.effective()[0]
    assert row.cost_usd == 3300.0 and row.quote_cost_usd == 3213.0
    # fill == the recorded premium: provenance stamped, number unchanged,
    # distinct line (the B2 convention: compared against the row as it stands)
    led = TradeLedger(path=_ledger().path, restate_at_fill=True)
    led.record(TradeRecord(symbol="HD261016P00350000", action="buy", instrument="option",
                           qty=1.0, entry_price=32.13, cost_usd=3213.0, order_id="hd1"))
    h, off = _capture()
    try:
        led.set_fill("hd1", 32.13, 1.0)
    finally:
        off()
    row = led.effective()[0]
    assert row.cost_usd == 3213.0 and row.quote_cost_usd == 3213.0 and row.fill_price == 32.13
    assert any("matches recorded premium on OPTION BUY HD261016P00350000: cost $3213.00 "
               "unchanged" in l for l in h.lines), h.lines


def test_option_buy_cost_restatement_is_knob_gated_guarded_and_scales_on_partial():
    def _hd(qty=1.0, cost=None):
        return TradeRecord(symbol="HD261016P00350000", action="buy", instrument="option",
                           qty=qty, entry_price=32.13,
                           cost_usd=cost if cost is not None else 3213.0 * qty,
                           order_id="hd1")
    # knob off: annotation only (the run-6 behaviour)
    led = TradeLedger(path=_ledger().path, restate_at_fill=False)
    led.record(_hd())
    h, off = _capture()
    try:
        led.set_fill("hd1", 34.3, 1.0)
    finally:
        off()
    row = led.effective()[0]
    assert row.fill_price == 34.3 and row.cost_usd == 3213.0 and row.quote_cost_usd is None
    assert any("stamped on OPTION BUY HD261016P00350000 WITHOUT cost restatement "
               "(LEDGER_RESTATE_AT_FILL=off)" in l for l in h.lines)
    # guard: a cost that is NOT premium x 100 x qty was priced some other way
    led = TradeLedger(path=_ledger().path, restate_at_fill=True)
    led.record(_hd(cost=5000.0))
    h, off = _capture()
    try:
        led.set_fill("hd1", 34.3, 1.0)
    finally:
        off()
    row = led.effective()[0]
    assert row.cost_usd == 5000.0 and row.quote_cost_usd is None
    assert any("is not premium x 100 x qty ($3,213.00)" in l for l in h.lines)
    # equity BUY rows are still never restated (B2 convention holds)
    led = TradeLedger(path=_ledger().path, restate_at_fill=True)
    led.record(_buy(qty=10.0, entry=100.0, oid="b1"))
    led.set_fill("b1", 100.37, 10.0)
    assert led.effective()[0].cost_usd == 1000.0 and led.effective()[0].quote_cost_usd is None
    # partial-fill correction scales cost_usd AND quote_cost_usd together
    led = TradeLedger(path=_ledger().path, restate_at_fill=True)
    led.record(_hd(qty=2.0))
    led.set_fill("hd1", 34.3, 2.0)
    led.record(TradeRecord.correction("hd1", "HD261016P00350000", "canceled", 1.0, 2.0))
    row = led.effective()[0]
    assert row.qty == 1.0 and row.cost_usd == 3430.0 and row.quote_cost_usd == 3213.0


def test_checker_phantom_rule_matches_the_ledger_rule():
    """scripts/eval_contract_check.py stays import-free, so its
    drop_phantom_sells is a dict-level mirror of ledger.dedup_sells — the two
    must drop the same rows on the AVAV / T / BIIB / LLY fixtures."""
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "eval_contract_check", root / "scripts" / "eval_contract_check.py")
    ecc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ecc)
    led = TradeLedger(path=_ledger().path, restate_at_fill=True)
    _avav(led); _t_option(led); _biib(led); _lly_storm(led)
    corrected = led.effective(dedup=False)
    want = [(d.record.order_id, d.rule) for d in led.phantoms()]
    assert len(want) == 2 + 1 + 0 + 24
    rows = [json.loads(r.model_dump_json()) for r in corrected]
    kept, dropped = ecc.drop_phantom_sells(rows)
    assert [(r["order_id"], r["_phantom_rule"]) for r in dropped] == want
    assert [r.get("order_id") for r in kept] == [r.order_id for r in led.effective()]
    assert ecc.PHANTOM_DUPE_WINDOW_H == PHANTOM_DUPE_WINDOW_H          # one window, two readers


def test_legacy_rows_load_with_4a18_fields_at_their_defaults():
    old = ('{"ts":"2026-07-08T13:23:02Z","symbol":"AVAV","action":"sell","qty":37.0,'
           '"exit_price":169.67,"realized_pl":212.01,"exit_reason":"trail"}')
    r = TradeRecord.model_validate_json(old)
    assert r.entry_ts is None and r.entry_fill_price is None and r.entry_fill_source is None
    assert r.entry_composite is None and r.entry_conviction is None and r.entry_stop_pct is None
    assert r.entry_key_signals == [] and r.lots_n is None and r.quote_cost_usd is None


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
