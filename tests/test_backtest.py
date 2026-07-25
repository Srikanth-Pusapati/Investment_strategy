"""Tests for the backtest harness (2.1).

Deterministic, no network: prices are injected. We assert that entries size
through the REAL RiskManager, that each exit type (stop / take / scale-out / time)
fires correctly, and that the headline metrics + benchmark comparison compute.

Runnable two ways:
    .venv/bin/python tests/test_backtest.py
    .venv/bin/pytest tests/
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.backtest import BacktestEngine, EntrySignal, run_backtest
from investment_strategy.config import RiskLimits


def _limits(**over) -> RiskLimits:
    base = dict(
        max_position_pct=5.0, max_symbol_exposure_pct=100.0,
        max_gross_exposure_pct=100.0, max_sector_exposure_pct=100.0,
        regime_filter_enabled=False, regime_degraded_mult=0.5,
        regime_trim_enabled=False, regime_trim_pct=25.0,
        max_daily_loss_pct=100.0, max_drawdown_pct=100.0, equity_floor_pct=0.0,
        max_open_positions=15, min_cash_buffer_pct=0.0, min_trade_price_usd=1.0,
        earnings_blackout_days=0, max_hold_days=0.0, time_stop_min_gain_pct=2.0,
        thesis_decay_enabled=False, thesis_decay_min_age_days=3.0, thesis_min_score=0.1,
        pdt_guard_enabled=False, max_day_trades_under_25k=3, min_conviction=0.0,
        max_trade_risk_pct=0.0, est_slippage_pct=0.1, min_edge_ratio=2.0,
        fractional_enabled=True, min_order_usd=1.0,
        default_stop_loss_pct=5.0, default_take_profit_pct=12.0,
        scale_out_enabled=False, scale_out_pct=50.0,
        kelly_fraction=0.0, target_annual_vol_pct=25.0,
        options_enabled=False, max_option_premium_pct=1.0,
    )
    base.update(over)
    return RiskLimits(**base)


def test_take_profit_exit_is_a_win():
    prices = {"AAA": [100, 105, 110, 115, 120]}
    r = run_backtest(_limits(), prices, [EntrySignal(day=0, symbol="AAA")])
    takes = [t for t in r.trades if t.reason == "take"]
    assert len(takes) == 1 and takes[0].pl_usd > 0
    assert r.total_return_pct > 0


def test_stop_loss_exit_is_a_loss():
    prices = {"AAA": [100, 98, 96, 94, 92]}
    r = run_backtest(_limits(), prices, [EntrySignal(day=0, symbol="AAA")])
    stops = [t for t in r.trades if t.reason == "stop"]
    assert len(stops) == 1 and stops[0].pl_usd < 0
    assert r.total_return_pct < 0


def test_time_stop_recycles_flat_position():
    prices = {"AAA": [100.0] * 35}
    r = run_backtest(_limits(max_hold_days=30.0), prices,
                     [EntrySignal(day=0, symbol="AAA")])
    times = [t for t in r.trades if t.reason == "time"]
    assert len(times) == 1
    assert times[0].exit_day == 30       # fired exactly at the max-hold age


def test_sizing_respects_position_cap():
    # kelly off, 5% of $100k = $5k / $100 = 50 shares. No exit -> liquidated at end.
    prices = {"AAA": [100.0] * 5}
    r = run_backtest(_limits(), prices, [EntrySignal(day=0, symbol="AAA")])
    assert len(r.trades) == 1 and r.trades[0].reason == "end"
    assert abs(r.trades[0].qty - 50.0) < 1e-6


def test_scale_out_sells_part_then_exits_remainder():
    # Rise to the take target, then keep rising -> scale a slice, trail the rest.
    prices = {"AAA": [100, 112, 120, 130, 140, 150]}
    engine = BacktestEngine(_limits(scale_out_enabled=True, scale_out_pct=50.0), prices)
    r = engine.run([EntrySignal(day=0, symbol="AAA")])
    reasons = [t.reason for t in r.trades]
    assert "scale" in reasons                      # sold a piece at the first target
    # Remainder later exits (trail/take/end) — the winner wasn't fully capped.
    assert len(r.trades) >= 2


def test_benchmark_excess_return_reported():
    prices = {"AAA": [100, 105, 110, 115, 120]}
    bench = [100, 101, 102, 103, 104]              # +4% buy-and-hold
    r = run_backtest(_limits(), prices, [EntrySignal(day=0, symbol="AAA")],
                     benchmark=bench)
    assert r.benchmark_return_pct is not None
    assert abs(r.benchmark_return_pct - 4.0) < 1e-6
    assert r.excess_return_pct == r.total_return_pct - r.benchmark_return_pct


def test_rejected_entry_never_opens():
    # Conviction below the floor -> RiskManager rejects -> no trade, flat equity.
    prices = {"AAA": [100, 110, 120]}
    r = run_backtest(_limits(min_conviction=0.5), prices,
                     [EntrySignal(day=0, symbol="AAA", conviction=0.1)])
    assert r.trades == []
    assert abs(r.total_return_pct) < 1e-9


def test_metrics_compute_on_a_multi_trade_run():
    prices = {"AAA": [100, 105, 110, 115, 120], "BBB": [100, 98, 96, 94, 92]}
    r = run_backtest(_limits(), prices, [
        EntrySignal(day=0, symbol="AAA"),
        EntrySignal(day=0, symbol="BBB"),
    ])
    assert 0.0 <= r.win_rate_pct <= 100.0
    assert r.max_drawdown_pct >= 0.0
    assert isinstance(r.sharpe, float)
    assert "Backtest result" in r.summary()


# -- account-level guards (D.3): floor latch / daily-loss flatten ------------ #
# One big position (90% of the book, wide 60% stop) so the ACCOUNT guards fire
# before any per-position stop does.
def _big_position(**over):
    return _limits(max_position_pct=100.0, default_stop_loss_pct=60.0, **over)


def test_equity_floor_flattens_and_latches():
    prices = {"AAA": [100.0, 100.0, 85.0, 85.0, 85.0]}   # -15% book day 2
    r = run_backtest(_big_position(equity_floor_pct=90.0), prices, [
        EntrySignal(day=0, symbol="AAA", target_weight_pct=90.0),
        EntrySignal(day=3, symbol="AAA", target_weight_pct=90.0),  # post-latch
    ])
    floors = [e for e in r.halt_events if e.kind == "floor"]
    assert len(floors) == 1 and floors[0].day == 2
    assert [t.reason for t in r.trades] == ["floor"]      # flattened, held nothing
    # The latch blocked the day-3 re-entry and does NOT auto-clear.
    assert r.blocked_buys.get("halt_latch") == 1
    assert all(t.entry_day == 0 for t in r.trades)


def test_daily_loss_emergency_flatten_without_latch():
    prices = {"AAA": [100.0, 100.0, 90.0, 90.0, 90.0]}   # -9% book day 2
    r = run_backtest(_big_position(max_daily_loss_pct=5.0), prices, [
        EntrySignal(day=0, symbol="AAA", target_weight_pct=90.0),
        EntrySignal(day=2, symbol="AAA", target_weight_pct=90.0),  # same-day rebuy
        EntrySignal(day=3, symbol="AAA", target_weight_pct=90.0),  # next-day rebuy
    ])
    dls = [e for e in r.halt_events if e.kind == "daily_loss"]
    assert len(dls) == 1 and dls[0].day == 2
    assert "flatten" in [t.reason for t in r.trades]
    # Same-day rebuy is blocked by the daily-loss halt; the next day (loss reset)
    # trading resumes — the daily flatten is NOT a latch, matching live behavior.
    assert r.blocked_buys.get("daily_loss_halt") == 1
    assert any(t.entry_day == 3 for t in r.trades)


def test_drawdown_halt_blocks_new_buys_without_flattening():
    prices = {
        "AAA": [100.0, 95.0, 88.0, 88.0, 88.0],          # dd ~11% from peak
        "BBB": [100.0, 100.0, 100.0, 100.0, 100.0],      # the blocked new idea
    }
    r = run_backtest(_big_position(max_drawdown_pct=10.0), prices, [
        EntrySignal(day=0, symbol="AAA", target_weight_pct=90.0),
        EntrySignal(day=2, symbol="BBB", target_weight_pct=5.0),
        EntrySignal(day=3, symbol="BBB", target_weight_pct=5.0),
    ])
    # Blocks NEW buys but never dumps the book — that's the floor's job.
    assert r.blocked_buys.get("drawdown_halt") == 2
    assert r.halt_events == []
    assert [t.reason for t in r.trades] == ["end"]       # only AAA, held to the end
    assert all(t.symbol == "AAA" for t in r.trades)


def test_guards_off_change_nothing():
    prices = {"AAA": [100.0, 100.0, 85.0, 85.0, 85.0]}
    r = run_backtest(_big_position(), prices,               # floor 0 / daily 100
                     [EntrySignal(day=0, symbol="AAA", target_weight_pct=90.0)])
    assert r.halt_events == [] and r.blocked_buys == {}
    assert "GUARD" not in r.summary()


def test_default_crash_still_engages_account_brakes():
    """Regression for the D.3 gate (scripts/backtest_real.py --stress): the DEFAULT
    crash_overlay must still drive the account-level brakes even with TIGHT
    per-position stops. Position stops de-risk a full book to cash within a bar or
    two, so a gentle crash lets them front-run the account brakes and the gate
    proves nothing. This locks the crash defaults hard enough that:
      - Pass 1 (live-like knobs): the daily-loss flatten engages, and the equity
        floor stays QUIET (the outer brakes catch it first — correct layering);
      - Pass 2 (daily-loss/drawdown OFF): the equity-floor latch is the last line
        and MUST flatten + latch.
    If a future stop-tightening breaks this, the crash defaults need re-deepening —
    do NOT loosen the guards to make it pass."""
    from dataclasses import replace

    from investment_strategy.backtest_data import crash_overlay, stubborn_entries

    # A full book that fills over 20 FLAT bars (no stops trip pre-crash), then a
    # default-severity crash grafted on from bar 20 over 10 liquid names.
    syms = [f"S{i}" for i in range(10)]
    dates = [f"2026-01-{d:02d}" for d in range(1, 61)]      # 60 bookkeeping dates
    prices = {s: [100.0] * 60 for s in syms}
    crashed = crash_overlay(prices, start=20)               # DEFAULT crash params
    entries = stubborn_entries(dates, crashed, every=5)

    limits = _limits(
        max_position_pct=10.0, max_gross_exposure_pct=100.0, max_open_positions=15,
        kelly_fraction=0.5, target_annual_vol_pct=45.0,
        default_stop_loss_pct=8.0, default_take_profit_pct=20.0,  # tight, live-like
        max_daily_loss_pct=3.0, max_drawdown_pct=15.0, equity_floor_pct=60.0,
        min_cash_buffer_pct=2.0, max_trade_risk_pct=1.0,
    )

    # Pass 1 — live-like: the daily-loss flatten must fire; the floor stays quiet.
    r1 = BacktestEngine(limits, crashed).run(list(entries))
    daily_fired = (
        any(e.kind == "daily_loss" for e in r1.halt_events)
        or r1.blocked_buys.get("daily_loss_halt", 0) > 0
    )
    assert daily_fired, "daily-loss brake never engaged under the default crash"
    assert not any(e.kind == "floor" for e in r1.halt_events), \
        "floor fired in pass 1 — outer brakes should catch it first (layering)"

    # Pass 2 — outer brakes off: the equity floor is the only line left, and must
    # latch + keep blocking every later re-entry (no auto-resume).
    naked = replace(limits, max_daily_loss_pct=100.0, max_drawdown_pct=100.0)
    r2 = BacktestEngine(naked, crashed).run(list(entries))
    assert any(e.kind == "floor" for e in r2.halt_events), \
        "equity-floor latch never engaged as the last line of defense"
    assert r2.blocked_buys.get("halt_latch", 0) > 0, \
        "latch did not keep blocking re-entries after firing"


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


def test_trail_arm_r_defers_micro_banking():
    # Jul-25: with a 5% stop and arm at 1.5R (=7.5%), a +4% pop that fades must
    # NOT be trail-closed at ~+1% (the legacy micro-banking); the position rides.
    prices = {"AAA": [100, 104, 100.5, 100.5, 100.5]}
    legacy = run_backtest(
        _limits(trail_giveback_pct=3.0), prices, [EntrySignal(day=0, symbol="AAA")])
    assert [t.reason for t in legacy.trades] == ["trail"]     # clipped at ~+0.5%
    armed = run_backtest(
        _limits(trail_giveback_pct=3.0, trail_arm_r=1.5),
        prices, [EntrySignal(day=0, symbol="AAA")])
    assert [t.reason for t in armed.trades] == ["end"]        # never trail-armed


def test_trail_giveback_r_widens_room_on_volatile_names():
    # giveback = max(3, 1.0 x 5% stop) = 5%: a 3.5% pullback off the peak no
    # longer fires the trail; the legacy fixed 3% would have closed it.
    prices = {"AAA": [100, 106, 102.5, 102.5, 102.5]}
    legacy = run_backtest(
        _limits(trail_giveback_pct=3.0), prices, [EntrySignal(day=0, symbol="AAA")])
    assert [t.reason for t in legacy.trades] == ["trail"]
    widened = run_backtest(
        _limits(trail_giveback_pct=3.0, trail_giveback_r=1.0),
        prices, [EntrySignal(day=0, symbol="AAA")])
    assert [t.reason for t in widened.trades] == ["end"]
