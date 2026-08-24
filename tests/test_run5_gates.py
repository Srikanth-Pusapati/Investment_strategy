"""Tests for the Aug 12-21 forensic-review gates (run-5 fixes).

TASK 1 — options gate bypass: HL's equity buy was rejected as overextended
(RSI 70, 4.2xATR) and the SAME thesis re-expressed as a $5,200
bull_call_spread bypassed anti-chase entirely (-67.6% in 21h); AMZN piled
~$29.8k of premium into one underlying (-$14,956). evaluate_option now runs
BULLISH structures through the shared overextension read and caps the OPEN
premium per underlying (PER_UNDERLYING_PREMIUM_PCT).

TASK 2 — corroboration gate: insider-cited entries -$18,681/13 trades and
every single-soft-signal starter (QNT, LFTO, INTC, F, AVBC) failed fast. A
fresh name cited on exactly ONE soft family (insider/congress/options_flow)
is halved AND needs composite >= CORROBORATION_MIN_COMPOSITE to enter.

Runnable two ways:
    .venv/bin/python -m pytest tests/test_run5_gates.py
    .venv/bin/pytest tests/
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.config import RiskLimits
from investment_strategy.models import (
    Action,
    OptionLeg,
    OptionStrategy,
    Position,
    RiskVerdict,
)

from test_risk import _account, _buy, _limits, _opt, _opt_exp, _pos, _rm

# Hot-and-extended but UNDER the 3.0x extreme leg -> haircut territory.
_HOT = {"rsi14": 70.0, "ext_atr": 2.5, "ext_pct_sma20": 12.0}
# The HL shape: RSI 70 at 4.2xATR -> extreme leg, hard block by default.
_EXTREME = {"rsi14": 70.0, "ext_atr": 4.2, "ext_pct_sma20": 18.0}
# Gap-day chase: +18% over the prior close with calm RSI/ATR reads.
_GAP = {"rsi14": 50.0, "ext_atr": 1.0, "prev_close": 100.0, "price": 118.0}


def _call_spread():
    return _opt(OptionStrategy.BULL_CALL_SPREAD, [
        OptionLeg(expiry=_opt_exp(30), strike=200, right="call", side=Action.BUY),
        OptionLeg(expiry=_opt_exp(30), strike=210, right="call", side=Action.SELL),
    ])


def _long_put():
    return _opt(OptionStrategy.LONG_PUT, [
        OptionLeg(expiry=_opt_exp(30), strike=200, right="put", side=Action.BUY),
    ])


def _opt_lot(underlying, qty=1.0, avg_entry=2.0, days=30, right="call"):
    """One open option-position row (an OCC contract) with a chosen premium
    basis — net premium of a lot is avg_entry * qty * 100 (short legs carry
    negative qty, i.e. a credit)."""
    from investment_strategy.execution.options import occ_symbol
    sym = occ_symbol(underlying, _opt_exp(days), 100.0, right)
    return Position(
        symbol=sym, qty=qty, avg_entry_price=avg_entry, current_price=avg_entry,
        market_value=abs(qty) * avg_entry * 100.0, unrealized_pl=0.0,
        unrealized_pl_pct=0.0, asset_class="us_option",
    )


# --------------------------------------------------------------------------- #
# TASK 1a — bullish option debits share the equity anti-chase gate
# --------------------------------------------------------------------------- #
def test_bullish_spread_haircut_on_hot_underlying():
    # $1000 budget at $200/contract = 5 contracts clean; hot-and-extended
    # halves the premium budget -> 2 contracts (the equity haircut's twin).
    rm = _rm(_limits(options_enabled=True, per_underlying_premium_pct=0.0))
    clean = rm.evaluate_option(_call_spread(), _account(),
                               est_premium_per_contract=2.0)
    assert clean.approved_qty == 5
    cut = rm.evaluate_option(_call_spread(), _account(),
                             est_premium_per_contract=2.0, tech=_HOT)
    assert cut.verdict is RiskVerdict.APPROVED
    assert cut.approved_qty == 2
    assert "chase haircut" in cut.reason.lower()


def test_bullish_call_extreme_extension_rejected():
    # The HL bypass: an equity buy at RSI 70 / 4.2xATR is a hard block, so
    # the same thesis as a call structure must be too.
    rm = _rm(_limits(options_enabled=True))
    d = rm.evaluate_option(_call_spread(), _account(),
                           est_premium_per_contract=2.0, tech=_EXTREME)
    assert d.verdict is RiskVerdict.REJECTED
    assert "OPTION CHASE GATE" in d.reason


def test_bullish_gap_day_chase_rejected():
    rm = _rm(_limits(options_enabled=True))
    d = rm.evaluate_option(_call_spread(), _account(),
                           est_premium_per_contract=2.0, tech=_GAP)
    assert d.verdict is RiskVerdict.REJECTED
    assert "OPTION CHASE GATE" in d.reason


def test_puts_exempt_from_upside_overextension():
    # A put on an overextended name is the OPPOSITE case — never blocked or
    # haircut by upside extension.
    rm = _rm(_limits(options_enabled=True, per_underlying_premium_pct=0.0))
    d = rm.evaluate_option(_long_put(), _account(),
                           est_premium_per_contract=2.0, tech=_EXTREME)
    assert d.verdict is RiskVerdict.APPROVED
    assert d.approved_qty == 5
    assert "chase" not in d.reason.lower()


def test_option_chase_gate_fails_open_without_tech():
    rm = _rm(_limits(options_enabled=True, per_underlying_premium_pct=0.0))
    d = rm.evaluate_option(_call_spread(), _account(),
                           est_premium_per_contract=2.0, tech=None)
    assert d.verdict is RiskVerdict.APPROVED
    assert d.approved_qty == 5


def test_option_chase_gate_off_with_equity_knob():
    # One knob governs both paths — OVEREXTENSION_GATE_ENABLED off disarms
    # the option twin too.
    rm = _rm(_limits(options_enabled=True, per_underlying_premium_pct=0.0,
                     overextension_gate_enabled=False))
    d = rm.evaluate_option(_call_spread(), _account(),
                           est_premium_per_contract=2.0, tech=_EXTREME)
    assert d.verdict is RiskVerdict.APPROVED
    assert d.approved_qty == 5


def test_option_chase_hot_respects_block_mode():
    # Mode parity with the equity gate: OVEREXTENSION_MODE=block rejects the
    # hot-and-extended case instead of halving.
    rm = _rm(_limits(options_enabled=True, overextension_mode="block"))
    d = rm.evaluate_option(_call_spread(), _account(),
                           est_premium_per_contract=2.0, tech=_HOT)
    assert d.verdict is RiskVerdict.REJECTED
    assert "OPTION CHASE GATE" in d.reason


# --------------------------------------------------------------------------- #
# TASK 1b — per-underlying premium concentration cap
# --------------------------------------------------------------------------- #
def test_per_underlying_premium_cap_rejects_pileup():
    # 0.5% of $100k = $500 cap; $400 already open on AAPL + $200/contract new
    # debit can't fit one contract -> the AMZN pile-up is refused.
    rm = _rm(_limits(options_enabled=True))
    held = [_opt_lot("AAPL", qty=2.0, avg_entry=2.0)]   # $400 open premium
    d = rm.evaluate_option(_opt(OptionStrategy.LONG_CALL, [
        OptionLeg(expiry=_opt_exp(30), strike=200, right="call", side=Action.BUY),
    ]), _account(positions=held), est_premium_per_contract=2.0)
    assert d.verdict is RiskVerdict.REJECTED
    assert "PER-UNDERLYING PREMIUM CAP" in d.reason


def test_per_underlying_premium_cap_clamps_budget():
    # $100 open premium leaves $400 headroom under the $500 cap -> the $1000
    # play budget clamps to 2 contracts; open + new never exceeds the cap.
    rm = _rm(_limits(options_enabled=True))
    held = [_opt_lot("AAPL", qty=1.0, avg_entry=1.0)]   # $100 open premium
    d = rm.evaluate_option(_call_spread(), _account(positions=held),
                           est_premium_per_contract=2.0)
    assert d.verdict is RiskVerdict.APPROVED
    assert d.approved_qty == 2
    assert d.approved_notional + 100.0 <= 500.0 + 1e-9


def test_per_underlying_cap_nets_short_legs():
    # A held debit spread counts at its NET premium: long $300 - short $100 =
    # $200 open -> $300 headroom -> exactly 1 more $200 contract fits.
    rm = _rm(_limits(options_enabled=True))
    held = [
        _opt_lot("AAPL", qty=1.0, avg_entry=3.0),               # +$300
        _opt_lot("AAPL", qty=-1.0, avg_entry=1.0, right="put"), # -$100 credit
    ]
    d = rm.evaluate_option(_call_spread(), _account(positions=held),
                           est_premium_per_contract=2.0)
    assert d.verdict is RiskVerdict.APPROVED
    assert d.approved_qty == 1


def test_per_underlying_cap_ignores_other_underlyings():
    # NVDA's open premium must not shrink AAPL's headroom (it's a
    # concentration cap, not a global one — max_option_premium_pct is that).
    rm = _rm(_limits(options_enabled=True))
    held = [_opt_lot("NVDA", qty=4.0, avg_entry=2.0)]   # $800 on NVDA
    d = rm.evaluate_option(_call_spread(), _account(positions=held),
                           est_premium_per_contract=2.0)
    assert d.verdict is RiskVerdict.APPROVED
    assert d.approved_qty == 2    # clamped by AAPL's own $500 cap, not NVDA's


def test_per_underlying_cap_off_at_zero():
    rm = _rm(_limits(options_enabled=True, per_underlying_premium_pct=0.0))
    held = [_opt_lot("AAPL", qty=2.0, avg_entry=2.0)]
    d = rm.evaluate_option(_call_spread(), _account(positions=held),
                           est_premium_per_contract=2.0)
    assert d.verdict is RiskVerdict.APPROVED
    assert d.approved_qty == 5


def test_per_underlying_cap_default_on():
    assert RiskLimits.__dataclass_fields__[
        "per_underlying_premium_pct"].default == 0.5
    assert RiskLimits.__dataclass_fields__[
        "corroboration_gate_enabled"].default is True
    assert RiskLimits.__dataclass_fields__[
        "corroboration_min_composite"].default == 1.25


# --------------------------------------------------------------------------- #
# TASK 2 — corroboration gate on single-soft-signal starters
# --------------------------------------------------------------------------- #
def _decide(fams, composite=None, conviction=1.0, positions=None, **over):
    rm = _rm(_limits(**over))
    return rm.evaluate(
        _buy(conviction=conviction), _account(positions=positions),
        price=100.0, volatility=0.25,
        composite_score=composite, entry_families=fams,
    )


def test_single_soft_signal_below_composite_bar_rejected():
    d = _decide({"insider"}, composite=1.0)
    assert d.verdict is RiskVerdict.REJECTED
    assert "CORROBORATION GATE" in d.reason


def test_single_soft_signal_above_bar_enters_at_half_size():
    base = _decide({"insider", "news"}, composite=1.31)
    cut = _decide({"insider"}, composite=1.31)
    assert cut.verdict in (RiskVerdict.APPROVED, RiskVerdict.RESIZED)
    assert abs(cut.approved_notional - base.approved_notional / 2) < 1e-6
    assert "Corroboration haircut" in cut.reason


def test_corroboration_and_starter_haircuts_never_stack():
    # Conviction 0.55 already triggers the starter haircut (< 0.65 full-
    # conviction bar); the corroboration trigger takes the MAX reduction,
    # never 0.25x.
    base = _decide({"insider", "news"}, composite=1.5, conviction=0.55)
    assert "Starter haircut" in base.reason
    cut = _decide({"insider"}, composite=1.5, conviction=0.55)
    assert cut.verdict in (RiskVerdict.APPROVED, RiskVerdict.RESIZED)
    assert abs(cut.approved_notional - base.approved_notional) < 1e-6
    assert "not halving twice" in cut.reason


def test_corroboration_gate_exempts_held_names():
    # A top-up of a held name is not a fresh single-soft starter.
    d = _decide({"insider"}, composite=0.0,
                positions=[_pos("AAPL", qty=10, price=100.0)])
    assert d.verdict in (RiskVerdict.APPROVED, RiskVerdict.RESIZED), d.reason
    assert "CORROBORATION" not in d.reason.upper() or "haircut" not in d.reason


def test_corroboration_gate_fails_open_on_missing_composite():
    # No composite -> the bar can't judge (best-effort feed), but the size
    # haircut still applies: thin evidence deploys at half.
    base = _decide({"insider", "news"}, composite=None)
    d = _decide({"insider"}, composite=None)
    assert d.verdict in (RiskVerdict.APPROVED, RiskVerdict.RESIZED), d.reason
    assert abs(d.approved_notional - base.approved_notional / 2) < 1e-6


def test_discovery_citation_is_not_corroboration():
    # Jul-27 laundering: congress re-cited through the DISCOVERY scanner is
    # still ONE soft read — the tag-along bucket must not defeat the gate.
    d = _decide({"congress", "discovery"}, composite=1.0)
    assert d.verdict is RiskVerdict.REJECTED
    assert "CORROBORATION GATE" in d.reason


def test_two_real_families_pass_untouched():
    base = _decide({"insider", "fundamentals"}, composite=1.0)
    assert base.verdict in (RiskVerdict.APPROVED, RiskVerdict.RESIZED)
    assert "Corroboration" not in base.reason


def test_corroboration_gate_off_by_knob():
    base = _decide({"insider", "news"}, composite=1.0)
    d = _decide({"insider"}, composite=1.0, corroboration_gate_enabled=False)
    assert d.verdict in (RiskVerdict.APPROVED, RiskVerdict.RESIZED)
    assert abs(d.approved_notional - base.approved_notional) < 1e-6


def test_missing_citations_fail_open():
    d = _decide(None, composite=0.0)
    assert d.verdict in (RiskVerdict.APPROVED, RiskVerdict.RESIZED), d.reason


# --------------------------------------------------------------------------- #
# Aug-23 review fixes: hedge exemption from the per-underlying cap, and the
# orchestrator-level wiring pin for the option chase gate
# --------------------------------------------------------------------------- #
def test_sanctioned_hedge_put_exempt_from_per_underlying_cap(caplog):
    # Crash protection routes ALL its puts through one or two fixed venues
    # (core ETF / put_proxy_etf) by design — a pile of open hedge premium
    # must never hard-block the next sanctioned re-up in a falling tape.
    import logging
    rm = _rm(_limits(options_enabled=True))
    held = [_opt_lot("AAPL", qty=4.0, avg_entry=2.0)]   # $800 >= the $500 cap
    with caplog.at_level(logging.INFO, logger="risk"):
        d = rm.evaluate_option(_long_put(), _account(positions=held),
                               est_premium_per_contract=2.0,
                               sanctioned_hedge=True)
    assert d.verdict is RiskVerdict.APPROVED, d.reason
    assert any("PER-UNDERLYING PREMIUM CAP" in r.getMessage()
               and "exempt" in r.getMessage() for r in caplog.records)
    # The same pile still rejects an ORDINARY (non-sanctioned) put.
    d2 = rm.evaluate_option(_long_put(), _account(positions=held),
                            est_premium_per_contract=2.0)
    assert d2.verdict is RiskVerdict.REJECTED
    assert "PER-UNDERLYING PREMIUM CAP" in d2.reason


def test_handle_option_wires_tech_into_chase_gate():
    """Orchestrator-level pin for the Aug-23 wiring fix: evaluate_option's
    chase gate deliberately fails open on tech=None, so _handle_option MUST
    pass the tech dict through — without it the HL re-expression (equity buy
    rejected 'Overextended', same thesis re-proposed as a call debit)
    deploys ungated while every direct evaluate_option test stays green."""
    from types import SimpleNamespace

    from investment_strategy.orchestrator import Orchestrator

    o = Orchestrator.__new__(Orchestrator)
    o.risk = _rm(_limits(options_enabled=True))         # REAL RiskManager
    o.options = SimpleNamespace(
        estimate_net_premium=lambda p: 2.0,
        min_leg_premium=lambda p: 2.0,
        leg_liquidity=lambda p: None,
    )
    o._regime_trend, o._regime_label, o._regime_mult = "up", "risk-on", 1.0
    o._hedge_symbol = ""
    journaled = []
    o._journal_decision = lambda *a, **k: journaled.append(a)
    o._handle_option(_call_spread(), _account(), tech=dict(_EXTREME))
    assert len(journaled) == 1
    verdict, reason = journaled[0][5], journaled[0][7]
    assert verdict == RiskVerdict.REJECTED.value
    assert "OPTION CHASE GATE" in reason
