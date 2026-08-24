"""Tests for the Jul-29 loss-diagnosis fixes.

The -$6k day decomposed into a repeatable geometry: floor-conviction starters
with floor-width stops resting INSIDE the breakout base (NU), a gap-day chase
the extension gate couldn't see (VRRM), a re-entry channel laundered by the
cycle reset, and a core trail that could dump the whole QQQ position. Each fix
is deterministic and tested here.

Runnable two ways:
    .venv/bin/python -m pytest tests/test_jul29_fixes.py
    .venv/bin/pytest tests/
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import uuid
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.journal import DecisionJournal, DecisionRecord
from investment_strategy.models import AccountSnapshot, Position, RiskVerdict
from investment_strategy.monitor.watchdog import Watchdog
from investment_strategy.reset import _churn_carryover
from investment_strategy.state import PortfolioState

from test_risk import _account, _buy, _limits, _opt_exp, _pos, _rm


def _tmp_state() -> PortfolioState:
    p = os.path.join(tempfile.gettempdir(), f"_j29_{uuid.uuid4().hex}.json")
    return PortfolioState(path=p)


# --------------------------------------------------------------------------- #
# Fix 1 — starter haircut
# --------------------------------------------------------------------------- #
def test_starter_haircut_halves_floor_conviction_fresh_name():
    rm = _rm(_limits(kelly_fraction=0.0))
    base = rm.evaluate(_buy(conviction=0.80), _account(), price=100.0, volatility=0.3)
    cut = rm.evaluate(_buy(conviction=0.62), _account(), price=100.0, volatility=0.3)
    assert cut.verdict in (RiskVerdict.APPROVED, RiskVerdict.RESIZED)
    assert abs(cut.approved_notional - base.approved_notional / 2) < 1e-6
    assert "Starter haircut" in cut.reason


def test_starter_haircut_exempts_topups():
    rm = _rm(_limits(kelly_fraction=0.0))
    acct = _account(positions=[_pos("AAPL", qty=10.0, price=100.0)])
    d = rm.evaluate(_buy(conviction=0.62), acct, price=100.0, volatility=0.3)
    assert "Starter haircut" not in d.reason


def test_starter_haircut_fires_on_floor_stop_even_at_high_conviction():
    # A quiet name whose 2-sigma stop clamps UP to the 4% floor is exactly the
    # NU/BEP geometry — half size regardless of stated conviction.
    rm = _rm(_limits(kelly_fraction=0.0, vol_stops_enabled=True))
    d = rm.evaluate(_buy(conviction=0.90), _account(), price=100.0, volatility=0.20)
    assert "Starter haircut" in d.reason and "vol floor" in d.reason


def test_starter_haircut_off_switch():
    rm = _rm(_limits(kelly_fraction=0.0, starter_haircut_enabled=False))
    d = rm.evaluate(_buy(conviction=0.62), _account(), price=100.0, volatility=0.3)
    assert "Starter haircut" not in d.reason


# --------------------------------------------------------------------------- #
# Fix 2 — stop widens to cover the extension over the 20d SMA
# --------------------------------------------------------------------------- #
def test_stop_covers_extension():
    rm = _rm(_limits(vol_stops_enabled=True))
    # 0.20 annualized vol -> 2-sigma ~2.5% -> clamped to the 4% floor. Entered
    # 6.8% above the SMA (the NU geometry): the stop must reach the mean.
    d = rm.evaluate(
        _buy(), _account(), price=100.0, volatility=0.20,
        tech={"ext_pct_sma20": 6.8},
    )
    assert abs(d.stop_loss_pct - 6.8) < 0.01
    assert abs(d.take_profit_pct - 6.8 * 2.5) < 0.05


def test_stop_extension_still_clamped_to_max():
    rm = _rm(_limits(vol_stops_enabled=True))
    d = rm.evaluate(
        _buy(), _account(), price=100.0, volatility=0.20,
        tech={"ext_pct_sma20": 15.0},
    )
    assert abs(d.stop_loss_pct - 10.0) < 0.01  # vol_stop_max_pct


def test_stop_extension_off_switch_keeps_sigma_stop():
    rm = _rm(_limits(vol_stops_enabled=True, stop_cover_extension=False))
    d = rm.evaluate(
        _buy(), _account(), price=100.0, volatility=0.20,
        tech={"ext_pct_sma20": 6.8},
    )
    assert abs(d.stop_loss_pct - 4.0) < 0.01  # clamped floor, unwidened


# --------------------------------------------------------------------------- #
# Fix 4 — gap-day chase trigger + prior-day ATR denominator
# --------------------------------------------------------------------------- #
def test_gap_day_entry_is_blocked():
    # VRRM Jul 29: bought +28% above the prior close; RSI/ATR-vs-SMA never saw
    # the one-day move. The gap trigger fires the extreme leg (block).
    rm = _rm(_limits())
    d = rm.evaluate(
        _buy(), _account(), price=100.0, volatility=0.3,
        tech={"rsi14": 50.0, "prev_close": 80.0},
    )
    assert d.verdict is RiskVerdict.REJECTED
    assert "gap-day chase" in d.reason


def test_gap_trigger_inert_at_zero_and_under_threshold():
    rm = _rm(_limits(overext_gap_pct=0.0))
    off = rm.evaluate(
        _buy(), _account(), price=100.0, volatility=0.3,
        tech={"rsi14": 50.0, "prev_close": 80.0},
    )
    assert off.verdict is not RiskVerdict.REJECTED
    rm2 = _rm(_limits())
    small = rm2.evaluate(
        _buy(), _account(), price=100.0, volatility=0.3,
        tech={"rsi14": 50.0, "prev_close": 95.0},  # +5.3% — ordinary move
    )
    assert small.verdict is not RiskVerdict.REJECTED


def test_prior_day_atr_denominator_preferred():
    # The gap bar inflates its own ATR: ext reads 2.9x on today's ATR but 4.1x
    # on the prior-day ATR. The gate must use the prior-day read -> extreme.
    rm = _rm(_limits())
    d = rm.evaluate(
        _buy(), _account(), price=100.0, volatility=0.3,
        tech={"rsi14": 60.0, "ext_atr": 2.9, "ext_atr_prior": 4.1,
              "ext_pct_sma20": 12.0},
    )
    assert d.verdict is RiskVerdict.REJECTED
    assert "extreme extension" in d.reason
    # And the symmetric case: prior-day read is calm -> no block.
    rm2 = _rm(_limits())
    calm = rm2.evaluate(
        _buy(), _account(), price=100.0, volatility=0.3,
        tech={"rsi14": 60.0, "ext_atr": 4.1, "ext_atr_prior": 2.9,
              "ext_pct_sma20": 12.0},
    )
    assert calm.verdict is not RiskVerdict.REJECTED


# --------------------------------------------------------------------------- #
# Fix 5 — loss-streak re-entry bar + reset churn carryover
# --------------------------------------------------------------------------- #
def _streak_limits(**over):
    # The streak guard sits BEHIND the time/price cooldowns; disable those so
    # these tests exercise the streak logic itself.
    return _limits(
        reentry_cooldown_hours=0.0, reentry_price_guard_enabled=False,
        reentry_price_override_composite=1.25, **over,
    )


def _two_trip_losses(state, symbol="AAPL"):
    """Two SEPARATE losing trips: backdate the first streak stamp past the
    same-trip dedupe window so the second register_exit counts."""
    from datetime import datetime, timedelta, timezone
    state.register_exit(symbol, pl_pct=-4.0)
    state.streak_times[symbol] = (
        datetime.now(timezone.utc) - timedelta(hours=30)
    ).isoformat()
    state.register_exit(symbol, pl_pct=-3.0)


def test_loss_streak_blocks_third_fresh_entry():
    state = _tmp_state()
    _two_trip_losses(state)
    assert state.loss_streak("AAPL") == 2
    rm = _rm(_streak_limits(), state=state)
    d = rm.evaluate(_buy(), _account(), price=100.0, volatility=0.3)
    assert d.verdict is RiskVerdict.REJECTED
    assert "loss-streak" in d.reason


def test_loss_streak_composite_override_and_win_reset():
    state = _tmp_state()
    _two_trip_losses(state)
    rm = _rm(_streak_limits(), state=state)
    strong = rm.evaluate(
        _buy(), _account(), price=100.0, volatility=0.3, composite_score=1.5,
    )
    assert strong.verdict is not RiskVerdict.REJECTED  # top-decile edge overrides
    state.register_exit("AAPL", pl_pct=+5.0)           # a win clears the streak
    assert state.loss_streak("AAPL") == 0
    d = rm.evaluate(_buy(), _account(), price=100.0, volatility=0.3)
    assert d.verdict is not RiskVerdict.REJECTED


def test_loss_streak_dedupes_same_trip_stamps():
    # The close ladder can stamp one losing trip several times (per replaced
    # leg, per retry tick, next-open resubmit). Stamps inside the 20h window
    # are the SAME trip — the streak must count once.
    state = _tmp_state()
    state.register_exit("AAPL", pl_pct=-4.0)
    state.register_exit("AAPL", pl_pct=-4.0)   # replace-ladder duplicate
    state.register_exit("AAPL", pl_pct=-4.1)   # retry-tick duplicate
    assert state.loss_streak("AAPL") == 1


def test_sanctioned_hedge_put_passes_direction_gate_without_held_core():
    # Intraday-drop leg: market trend still "up", label risk-on, core NOT held
    # (reset day) — the sanctioned index put must pass the direction gate.
    from investment_strategy.models import (
        Action, Instrument, OptionLeg, OptionStrategy, TradeProposal,
    )
    rm = _rm(_limits(options_enabled=True))
    put = TradeProposal(
        symbol="QQQ", action=Action.BUY, conviction=0.7, target_weight_pct=1.0,
        rationale="index hedge", instrument=Instrument.OPTION,
        option_strategy=OptionStrategy.LONG_PUT,
        # Dynamic expiry: a fixed date here drifted under the 7d DTE minimum
        # and failed the test at the DTE gate before it ever reached the
        # direction gate it exists to exercise.
        option_legs=[OptionLeg(expiry=_opt_exp(30), strike=650.0,
                               right="put", side=Action.BUY, ratio=1)],
    )
    blocked = rm.evaluate_option(
        put, _account(), est_premium_per_contract=5.0,
        market_trend="up", regime_label="risk-on",
    )
    assert blocked.verdict is RiskVerdict.REJECTED  # gate holds without sanction
    ok = rm.evaluate_option(
        put, _account(), est_premium_per_contract=5.0,
        market_trend="up", regime_label="risk-on", sanctioned_hedge=True,
    )
    assert ok.verdict is not RiskVerdict.REJECTED


def test_reset_carries_churn_memory_and_synthesizes_exits():
    d = tempfile.mkdtemp()
    state_file = os.path.join(d, "risk_state.json")
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump({
            "entry_times": {"NU": "2026-07-28T13:00:00+00:00",
                            "QQQ": "2026-07-28T13:00:00+00:00"},
            "exit_times": {"OLD": "2026-07-27T15:00:00+00:00"},
            "exit_prices": {"OLD": 14.17},
            "loss_streaks": {"NU": 1},
        }, f)
    cfg = SimpleNamespace(state_file=state_file, core_etf="QQQ")
    carry = _churn_carryover(cfg)
    # Open positions at reset get a synthesized exit (the re-entry cooldown
    # applies to what the reset flattened) — but never the passive core.
    assert "NU" in carry["exit_times"] and "QQQ" not in carry["exit_times"]
    assert carry["exit_times"]["OLD"] == "2026-07-27T15:00:00+00:00"
    assert carry["exit_prices"] == {"OLD": 14.17}
    assert carry["loss_streaks"] == {"NU": 1}


# --------------------------------------------------------------------------- #
# Fix 6 — core exempt from the watchdog trailing stop
# --------------------------------------------------------------------------- #
class _TrailBroker:
    def __init__(self, positions):
        self._acct = AccountSnapshot(
            equity=100_000.0, last_equity=100_000.0, cash=50_000.0,
            buying_power=50_000.0, positions=positions,
        )
        self.closed: list[str] = []

    def get_account(self):
        return self._acct

    def is_market_open(self):
        return True

    def close_position(self, symbol):
        self.closed.append(symbol)
        return f"oid-{symbol}"

    def cancel_open_orders_for(self, symbol):
        pass

    def latest_price(self, symbol):
        return 100.0


def _gaveback(symbol):
    # Peaked +5%, now +1% — past the 3% legacy giveback.
    return Position(symbol=symbol, qty=100.0, avg_entry_price=100.0,
                    current_price=101.0, market_value=10_100.0,
                    unrealized_pl=100.0, unrealized_pl_pct=1.0)


def _trail_cfg(core_stop_pct=15.0):
    return SimpleNamespace(
        risk=SimpleNamespace(
            equity_floor_pct=0.0, max_daily_loss_pct=99.0,
            max_hold_days=0.0, time_stop_min_gain_pct=2.0,
            scale_out_enabled=False, scale_out_pct=50.0,
            whole_shares_only=False, trail_giveback_pct=3.0,
        ),
        core_etf="QQQ",
        core_stop_pct=core_stop_pct,
        state_file="state/risk_state.json",
        monitor_interval_s=30,
    )


def test_core_is_exempt_from_trailing_stop_but_satellites_trail():
    state = _tmp_state()
    state.set_high_water("QQQ", 5.0)
    state.set_high_water("AAPL", 5.0)
    broker = _TrailBroker([_gaveback("QQQ"), _gaveback("AAPL")])
    wd = Watchdog(_trail_cfg(), broker, state=state)
    wd.check_once()
    assert "AAPL" in broker.closed     # satellite trail fires
    assert "QQQ" not in broker.closed  # core is exempt


def test_core_trail_exemption_requires_the_gtc_stop():
    # With CORE_STOP_PCT=0 the GTC core stop is off — the trail is the core's
    # only position-level guard and must keep running.
    state = _tmp_state()
    state.set_high_water("QQQ", 5.0)
    broker = _TrailBroker([_gaveback("QQQ")])
    wd = Watchdog(_trail_cfg(core_stop_pct=0.0), broker, state=state)
    wd.check_once()
    assert "QQQ" in broker.closed


# --------------------------------------------------------------------------- #
# Fix 11c — vanished-position sweep keys on every tracking map
# --------------------------------------------------------------------------- #
def test_vanished_sweep_covers_entry_times_and_spares_pending_orders():
    state = _tmp_state()
    state.register_entry("GHOST")            # entered + crashed inside one tick:
    state.register_stop_width("GHOST", 4.0)  # no high-water mark yet
    state.register_entry("FRESH")            # buy submitted seconds ago,
    state.add_pending_order("oid-1", "FRESH")  # not yet a position row
    fired = []
    broker = _TrailBroker([])
    wd = Watchdog(_trail_cfg(), broker, state=state,
                  on_exchange_exit=lambda: fired.append(True))
    wd.check_once()
    assert "GHOST" not in state.entry_times      # swept + backfill fired
    assert fired
    assert "FRESH" in state.entry_times          # pending order protected


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
