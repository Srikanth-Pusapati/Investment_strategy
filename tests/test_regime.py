"""Tests for the market-regime filter's scoring logic.

Pure logic, no network: the yfinance-backed data fetchers are overridden with
canned SPY closes + a VIX level so we assert the trend x fear multiplier, the
labels, the floor, and the fail-open (unknown -> full size) contract.

Runnable two ways:
    .venv/bin/python tests/test_regime.py     # standalone, no pytest
    .venv/bin/pytest tests/                    # if pytest is installed
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.regime import RegimeReader

# 210 rising closes -> last value sits well above the 200d mean (uptrend).
_UPTREND = [float(i) for i in range(1, 211)]
# 210 falling closes -> last value sits well below the 200d mean (downtrend).
_DOWNTREND = [float(i) for i in range(210, 0, -1)]


class _FakeRegime(RegimeReader):
    """Injects SPY closes + a VIX level (and optional 3-month VIX3M) instead of
    hitting yfinance."""
    def __init__(self, spy, vix, vix3m=None):
        super().__init__()
        self._spy = spy
        self._vix = vix
        self._vix3m = vix3m

    def _daily_closes(self, symbol, days):   # overrides the static parent method
        return self._spy if symbol == "SPY" else ([self._vix] if self._vix else [])

    def _last_close(self, symbol):
        if symbol == "^VIX3M":
            return self._vix3m
        return self._vix


def test_risk_on_uptrend_low_vix():
    r = _FakeRegime(_UPTREND, vix=15.0).assess()
    assert r.multiplier == 1.0 and r.label == "risk-on"


def test_neutral_uptrend_elevated_vix():
    r = _FakeRegime(_UPTREND, vix=25.0).assess()
    assert r.multiplier == 0.7 and r.label == "neutral"


def test_risk_off_downtrend_high_vix():
    r = _FakeRegime(_DOWNTREND, vix=35.0).assess()
    # trend 0.5 * vol 0.4 = 0.20 -> floored to 0.25, risk-off
    assert r.multiplier == 0.25 and r.label == "risk-off"


def test_downtrend_low_vix_is_half():
    r = _FakeRegime(_DOWNTREND, vix=15.0).assess()
    assert r.multiplier == 0.5 and r.label == "neutral"


def test_degraded_data_sizes_down_not_full():
    # yfinance down -> the sector cap is blind too, so we size DOWN (default 0.5),
    # not full, and label it unknown (1B.7). Never blocks; just smaller.
    r = _FakeRegime([], vix=None).assess()      # no SPY history, no VIX
    assert r.multiplier == 0.5 and r.label == "unknown"


def test_degraded_multiplier_is_configurable_and_clamped():
    reader = _FakeRegime([], vix=None)
    reader.degraded_mult = 0.3                   # custom cautious downsize
    assert reader.assess().multiplier == 0.3
    # Out-of-range values are clamped to [floor, 1.0] at construction.
    assert RegimeReader(degraded_mult=5.0).degraded_mult == 1.0
    assert RegimeReader(degraded_mult=0.0).degraded_mult == 0.25


def test_backwardation_forces_elevated_tier_when_spot_calm():
    # Spot VIX calm (<20) alone would be full size, but the term structure is
    # inverted (spot 18 > 3-month 15) — backwardation forces at least the
    # elevated tier even though the spot LEVEL hasn't crossed 20.
    r = _FakeRegime(_UPTREND, vix=18.0, vix3m=15.0).assess()
    assert r.multiplier == 0.7 and "BACKWARDATION" in r.reason


def test_contango_leaves_level_read_untouched():
    # Normal term structure (spot 15 < 3-month 18) — no tightening; full size.
    r = _FakeRegime(_UPTREND, vix=15.0, vix3m=18.0).assess()
    assert r.multiplier == 1.0 and "contango" in r.reason


def test_backwardation_never_loosens_a_stressed_read():
    # High VIX (35) in a downtrend already floors to 0.25; backwardation must
    # not raise it — min() only ever tightens.
    r = _FakeRegime(_DOWNTREND, vix=35.0, vix3m=30.0).assess()
    assert r.multiplier == 0.25


def test_missing_vix3m_keeps_level_only_read_not_degraded():
    # ^VIX3M unavailable is an enhancement gap, NOT a degraded posture: spot VIX
    # + SPY still drive the full level-based read (risk-on, full size).
    r = _FakeRegime(_UPTREND, vix=15.0, vix3m=None).assess()
    assert r.multiplier == 1.0 and r.label == "risk-on"


def test_assess_is_cached_until_new_cycle():
    reader = _FakeRegime(_UPTREND, vix=15.0)
    reader.assess()
    reader._spy = _DOWNTREND          # change inputs; cache should hold
    reader._vix = 40.0
    assert reader.assess().label == "risk-on"   # served from cache
    reader.new_cycle()
    assert reader.assess().label == "risk-off"  # recomputed after reset


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
