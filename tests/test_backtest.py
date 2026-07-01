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
