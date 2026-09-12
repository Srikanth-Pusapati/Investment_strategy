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
    def __init__(self, spy, vix, vix3m=None, breadth_below=None, **kw):
        super().__init__(**kw)
        self._spy = spy
        self._vix = vix
        self._vix3m = vix3m
        # None = legacy (QQQ/IWM return a 1-bar list, breadth confirm inert);
        # True/False = both indexes below/above their 50dma (S-5 replay tests).
        self._breadth_below = breadth_below

    def _daily_closes(self, symbol, days):   # overrides the static parent method
        if symbol in ("QQQ", "IWM") and self._breadth_below is not None:
            tail = [80.0] * 10 if self._breadth_below else [110.0] * 10
            return [100.0] * 50 + tail
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


# -- long-run trend field (drives the option call/put direction gate) -------- #
def test_trend_up_exposed_independently_of_label():
    r = _FakeRegime(_UPTREND, vix=15.0).assess()
    assert r.trend == "up"


def test_trend_down_even_when_label_reads_neutral():
    # Calm-VIX downtrend: label blends to "neutral" (0.5) but the LONG-RUN
    # direction is down — the direction gate must see "down" here.
    r = _FakeRegime(_DOWNTREND, vix=15.0).assess()
    assert r.label == "neutral"
    assert r.trend == "down"


def test_trend_up_survives_a_vol_spike_riskoff_label():
    # VIX>=30 inside an uptrend: label says risk-off (vol alone), but the
    # 200dma trend is still up — trend and label must not be conflated.
    r = _FakeRegime(_UPTREND, vix=35.0).assess()
    assert r.label == "risk-off"
    assert r.trend == "up"


def test_trend_empty_on_degraded_read():
    r = _FakeRegime([], vix=None).assess()
    assert r.label == "unknown"
    assert r.trend == ""


# -- S-5 (run-7): label persistence — tighten fast, loosen slow ------------- #
# Evidence: logs/Sep_10_2026.log flapped risk-on/neutral 5x in 8 reads on a
# partial QQQ bar (see the regime.py module docstring). Default
# REGIME_LOOSEN_MIN_CYCLES=2: a looser read is applied only on the 2nd
# consecutive clean read; a tighter read is applied at once.
import logging as _logging


class _Capture(_logging.Handler):
    """Collects 'regime' log lines so the greppable handles can be asserted
    without pytest's caplog (keeps the standalone runner working)."""
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def _read(reader, *, vix=None, spy=None, breadth_below=None):
    """Advance one decision cycle with new inputs and return the applied read."""
    reader.new_cycle()
    if vix is not None:
        reader._vix = vix
    if spy is not None:
        reader._spy = spy
    if breadth_below is not None:
        reader._breadth_below = breadth_below
    return reader.assess()


def test_regime_tightens_immediately():
    # risk-on -> neutral -> risk-off: every tighter read is adopted on the
    # cycle it appears, with no hold, no streak, and the fresh multiplier.
    reader = _FakeRegime(_UPTREND, vix=15.0)
    assert reader.assess().label == "risk-on"
    r = _read(reader, vix=25.0)
    assert (r.label, r.multiplier) == ("neutral", 0.7)
    assert "held" not in r.reason
    r = _read(reader, vix=35.0)
    assert (r.label, r.multiplier) == ("risk-off", 0.4)
    assert reader._loosen_streak == 0 and reader._eff_label == "risk-off"


def test_regime_loosens_only_after_min_cycles():
    # Spec sequence: fresh (neutral, risk-on, risk-on) -> applied
    # (neutral, neutral, risk-on). The first clean read is HELD at neutral
    # x0.70 with the hold spelled out in the reason; the second is applied.
    reader = _FakeRegime(_UPTREND, vix=25.0)
    assert reader.assess().label == "neutral"
    r = _read(reader, vix=15.0)                      # fresh risk-on x1.00
    assert (r.label, r.multiplier) == ("neutral", 0.7)
    assert ", held neutral (1/2 clean reads)" in r.reason
    assert "applied neutral x0.70" in r.reason
    r = _read(reader, vix=15.0)                      # 2nd clean read
    assert (r.label, r.multiplier) == ("risk-on", 1.0)
    assert "held" not in r.reason
    assert reader._loosen_streak == 0


def test_regime_same_label_read_resets_loosen_streak():
    # neutral, risk-on (1/2), neutral, risk-on -> the streak must restart, so
    # this last read is again 1/2 and still held. This is exactly the Sep 10
    # 10:14 / 11:06 / 11:58 pattern.
    reader = _FakeRegime(_UPTREND, vix=25.0)
    reader.assess()
    _read(reader, vix=15.0)
    assert reader._loosen_streak == 1
    _read(reader, vix=25.0)
    assert reader._loosen_streak == 0
    r = _read(reader, vix=15.0)
    assert r.label == "neutral" and "(1/2 clean reads)" in r.reason


def test_regime_unknown_does_not_reset_streak():
    # neutral, risk-on (1/2), UNKNOWN, risk-on: the degraded read passes
    # through unchanged (label unknown, degraded x0.50), counts for nothing,
    # and keeps the held label — so the next clean read is 2/2 and loosens.
    reader = _FakeRegime(_UPTREND, vix=25.0)
    reader.assess()
    _read(reader, vix=15.0)
    assert reader._eff_label == "neutral" and reader._loosen_streak == 1
    r = _read(reader, spy=[], vix=None)
    assert (r.label, r.multiplier) == ("unknown", 0.5)
    assert "held" not in r.reason
    assert reader._eff_label == "neutral" and reader._loosen_streak == 1
    r = _read(reader, spy=_UPTREND, vix=15.0)
    assert (r.label, r.multiplier) == ("risk-on", 1.0)
    # An unknown FIRST read never seeds the held label either.
    fresh = _FakeRegime([], vix=None)
    fresh.assess()
    assert fresh._eff_label is None
    assert _read(fresh, spy=_UPTREND, vix=15.0).label == "risk-on"


def test_regime_hold_multiplier_is_min():
    # Held neutral caps a fresh x1.00 at the neutral tier x0.70; held risk-off
    # caps a fresh neutral x0.50 (calm downtrend) AND a fresh risk-on x1.00 at
    # the risk-off tier x0.40. min() with the fresh read, never max().
    reader = _FakeRegime(_UPTREND, vix=25.0)
    reader.assess()
    assert _read(reader, vix=15.0).multiplier == 0.7
    reader = _FakeRegime(_UPTREND, vix=35.0)           # 1.0*0.4 -> risk-off
    assert reader.assess().multiplier == 0.4
    r = _read(reader, spy=_DOWNTREND, vix=15.0)       # fresh neutral x0.50
    assert (r.label, r.multiplier) == ("risk-off", 0.4)
    assert ", held risk-off (1/2 clean reads)" in r.reason
    reader = _FakeRegime(_UPTREND, vix=35.0)
    reader.assess()
    r = _read(reader, vix=15.0)                        # fresh risk-on x1.00
    assert (r.label, r.multiplier) == ("risk-off", 0.4)


def test_regime_hold_passes_trend_and_day_change_through():
    # The direction gate keys on trend and the core defense on day_change_pct:
    # both come from the FRESH read even while the label is held.
    reader = _FakeRegime(_UPTREND, vix=25.0)
    reader.assess()
    r = _read(reader, vix=15.0)
    assert r.label == "neutral" and r.trend == "up"
    assert r.day_change_pct == round((210.0 / 209.0 - 1.0) * 100.0, 2)


def test_regime_loosen_min_cycles_one_is_legacy_no_memory():
    # REGIME_LOOSEN_MIN_CYCLES=1 reproduces today's behaviour exactly: every
    # fresh read is applied as-is. 0 / negative clamp to 1 (never "never").
    reader = _FakeRegime(_UPTREND, vix=25.0, loosen_min_cycles=1)
    reader.assess()
    r = _read(reader, vix=15.0)
    assert (r.label, r.multiplier) == ("risk-on", 1.0) and "held" not in r.reason
    assert RegimeReader(loosen_min_cycles=0).loosen_min_cycles == 1


def test_regime_hold_logs_greppable_line():
    cap = _Capture()
    lg = _logging.getLogger("regime")
    old_level = lg.level
    lg.addHandler(cap)
    lg.setLevel(_logging.INFO)
    try:
        reader = _FakeRegime(_UPTREND, vix=25.0)
        reader.assess()
        _read(reader, vix=15.0)
        _read(reader, vix=15.0)
        _read(reader, vix=25.0)
    finally:
        lg.removeHandler(cap)
        lg.setLevel(old_level)
    assert (
        "REGIME HOLD: neutral held (1/2 clean reads); fresh read risk-on x1.00 "
        "-> applied neutral x0.70"
    ) in cap.lines
    assert any(l.startswith("REGIME LOOSEN: neutral -> risk-on after 2/2 clean reads")
               for l in cap.lines)
    assert any(l.startswith("REGIME TIGHTEN: risk-on -> neutral adopted now")
               for l in cap.lines)


def test_regime_sep10_replay_holds_neutral_from_0922():
    # logs/Sep_10_2026.log fresh labels, 08:30..14:35 (lines 12/86/139/201/
    # 254/304/356/403), driven through the same breadth path that flapped:
    fresh = ["risk-on", "neutral", "risk-on", "neutral", "risk-on",
             "neutral", "neutral", "neutral"]
    reader = _FakeRegime(_UPTREND, vix=17.0, vix3m=19.0, breadth_below=False)
    applied = []
    for label in fresh:
        r = _read(reader, breadth_below=(label == "neutral"))
        applied.append((r.label, r.multiplier))
    # Counterfactual: neutral held from 09:22 through the close; the 08:30
    # (INTC cycle) read is a genuine completed-bar risk-on and is UNCHANGED.
    assert applied == [("risk-on", 1.0)] + [("neutral", 0.7)] * 7
    # The reason on a held read still carries the fresh breadth evidence.
    assert reader._eff_label == "neutral"


def test_regime_assess_cache_applies_persistence_once_per_cycle():
    # Repeated assess() calls inside ONE cycle (orchestrator calls it several
    # times) must not advance the loosen streak: 2 calls != 2 clean reads.
    reader = _FakeRegime(_UPTREND, vix=25.0)
    reader.assess()
    _read(reader, vix=15.0)
    for _ in range(3):
        assert reader.assess().label == "neutral"
    assert reader._loosen_streak == 1

def test_regime_loosen_min_cycles_config_default_and_env():
    # Config key REGIME_LOOSEN_MIN_CYCLES: dataclass default 2 (the operator
    # decision), env override honoured by the same _i() loader load_config uses.
    from investment_strategy.config import RiskLimits, _i
    assert RiskLimits.__dataclass_fields__["regime_loosen_min_cycles"].default == 2
    os.environ["REGIME_LOOSEN_MIN_CYCLES"] = "3"
    try:
        assert _i("REGIME_LOOSEN_MIN_CYCLES", 2) == 3
    finally:
        os.environ.pop("REGIME_LOOSEN_MIN_CYCLES", None)
    assert _i("REGIME_LOOSEN_MIN_CYCLES", 2) == 2
