"""Tests for the anti-chasing overextension gate + the composite buy floor.

Week of 2026-07-13: 68% of realized losses were momentum entries near local
tops (CDW/SOFI/PATH — high RSI + bullish flow, straight to the stop). The gate
fires only when BOTH legs are elevated (RSI and extension over the 20d SMA),
haircuts by default, can block, and FAILS OPEN on missing technicals so a
yfinance outage can't freeze all buying. The composite floor is opt-in and
fails open on None.

Runnable two ways:
    .venv/bin/python -m pytest tests/test_overextension_gate.py
    .venv/bin/pytest tests/
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.models import RiskVerdict

from test_risk import _account, _buy, _limits, _rm

# rsi >= 65 AND ext_atr >= 2.0 with the default knobs -> extended.
_HOT = {"rsi14": 70.0, "ext_atr": 2.5, "ext_pct_sma20": 12.0}


def _decide(tech=None, composite=None, **limit_over):
    rm = _rm(_limits(**limit_over))
    return rm.evaluate(
        _buy(), _account(), price=100.0, volatility=0.25,
        tech=tech, composite_score=composite,
    )


def test_haircut_halves_extended_buy():
    base = _decide()
    cut = _decide(tech=_HOT)
    assert cut.verdict in (RiskVerdict.APPROVED, RiskVerdict.RESIZED)
    assert abs(cut.approved_notional - base.approved_notional / 2) < 1e-6
    assert "Overextension haircut" in cut.reason


def test_block_mode_rejects_extended_buy():
    d = _decide(tech=_HOT, overextension_mode="block")
    assert d.verdict is RiskVerdict.REJECTED
    assert "Overextended" in d.reason


def test_gate_needs_both_legs_below_extreme():
    base = _decide()
    # Hot RSI but NOT extended (an ordinary uptrend) -> untouched.
    calm_ext = _decide(tech={"rsi14": 72.0, "ext_atr": 0.8, "ext_pct_sma20": 2.0})
    # Moderately extended (under the 3-ATR extreme leg) with a cool RSI -> untouched.
    calm_rsi = _decide(tech={"rsi14": 55.0, "ext_atr": 2.5, "ext_pct_sma20": 12.0})
    for d in (calm_ext, calm_rsi):
        assert abs(d.approved_notional - base.approved_notional) < 1e-6
        assert "haircut" not in d.reason.lower()


def test_extreme_extension_blocks_by_default_regardless_of_rsi():
    # The actual Jul-13 losers: CDW RSI 63.9 @ 3.41 ATR, PATH RSI 60.9 @ 3.68
    # ATR — under the RSI floor, but screaming extension. The extreme leg
    # must catch exactly these, and now HARD-BLOCKS by default (the shared
    # haircut mode only halved size — CVX still bought $2,799 at 3.2xATR).
    cdw_like = _decide(tech={"rsi14": 63.9, "ext_atr": 3.41, "ext_pct_sma20": 7.5})
    assert cdw_like.verdict is RiskVerdict.REJECTED
    assert "extreme extension" in cdw_like.reason


def test_extreme_extension_haircut_mode_still_downsizes():
    # OVEREXT_EXTREME_MODE=haircut restores the old shared behavior: halve, not
    # reject. A mild (non-extreme) hot-and-extended entry is unaffected by this
    # knob and still follows overextension_mode.
    base = _decide()
    cut = _decide(tech={"rsi14": 63.9, "ext_atr": 3.41, "ext_pct_sma20": 7.5},
                  overext_extreme_mode="haircut")
    assert cut.verdict in (RiskVerdict.APPROVED, RiskVerdict.RESIZED)
    assert abs(cut.approved_notional - base.approved_notional / 2) < 1e-6
    assert "extreme extension" in cut.reason


def test_extreme_leg_disabled_at_zero():
    base = _decide()
    d = _decide(tech={"rsi14": 60.0, "ext_atr": 3.5, "ext_pct_sma20": 12.0},
                overext_extreme_atr_mult=0.0)
    assert abs(d.approved_notional - base.approved_notional) < 1e-6


def test_pct_fallback_when_atr_unavailable():
    base = _decide()
    d = _decide(tech={"rsi14": 70.0, "ext_atr": None, "ext_pct_sma20": 9.0})
    assert abs(d.approved_notional - base.approved_notional / 2) < 1e-6
    near = _decide(tech={"rsi14": 70.0, "ext_atr": None, "ext_pct_sma20": 5.0})
    assert abs(near.approved_notional - base.approved_notional) < 1e-6


def test_fails_open_without_technicals():
    base = _decide()
    d = _decide(tech=None)
    assert abs(d.approved_notional - base.approved_notional) < 1e-6


def test_gate_off_by_knob():
    base = _decide()
    d = _decide(tech=_HOT, overextension_gate_enabled=False)
    assert abs(d.approved_notional - base.approved_notional) < 1e-6


# -- composite floor (opt-in) -------------------------------------------------- #
def test_composite_floor_rejects_below_when_enabled():
    d = _decide(composite=-0.5, composite_gate_enabled=True,
                min_composite_score=0.0)
    assert d.verdict is RiskVerdict.REJECTED
    assert "Composite" in d.reason


def test_composite_floor_passes_at_or_above():
    d = _decide(composite=0.1, composite_gate_enabled=True,
                min_composite_score=0.0)
    assert d.verdict in (RiskVerdict.APPROVED, RiskVerdict.RESIZED)


def test_composite_floor_fails_open_on_none_and_off_by_default():
    open_ = _decide(composite=None, composite_gate_enabled=True)
    assert open_.verdict in (RiskVerdict.APPROVED, RiskVerdict.RESIZED)
    off = _decide(composite=-0.5)   # gate default off
    assert off.verdict in (RiskVerdict.APPROVED, RiskVerdict.RESIZED)
