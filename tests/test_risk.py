"""Tests for the deterministic risk gate — the one module that must never regress.

Runnable two ways:
    .venv/bin/python tests/test_risk.py     # standalone, no pytest needed
    .venv/bin/pytest tests/                  # if pytest is installed
"""
from __future__ import annotations

import os
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.config import RiskLimits
from investment_strategy.models import (
    AccountSnapshot,
    Action,
    Instrument,
    OptionLeg,
    OptionStrategy,
    Position,
    RiskVerdict,
    TradeProposal,
)
from investment_strategy.risk import RiskManager
from investment_strategy.state import PortfolioState


def _rm(limits, *, kill_switch=False, state=None) -> RiskManager:
    """Build a RiskManager with ISOLATED risk state. Without this, the default
    PortfolioState() reads the real state/risk_state.json, whose persisted peak
    equity leaks a false ~98% drawdown into small-account tests."""
    if state is None:
        p = os.path.join(tempfile.gettempdir(), f"_rm_test_{uuid.uuid4().hex}.json")
        state = PortfolioState(path=p)
    return RiskManager(limits, kill_switch=kill_switch, state=state)


def _limits(**over) -> RiskLimits:
    base = dict(
        max_position_pct=5.0,
        max_symbol_exposure_pct=10.0,
        max_gross_exposure_pct=100.0,
        max_sector_exposure_pct=30.0,
        regime_filter_enabled=True,
        regime_degraded_mult=0.5,
        regime_trim_enabled=False,
        regime_trim_pct=25.0,
        max_daily_loss_pct=3.0,
        max_drawdown_pct=15.0,
        equity_floor_pct=0.0,
        max_open_positions=15,
        min_cash_buffer_pct=10.0,
        min_trade_price_usd=5.0,
        earnings_blackout_days=3,
        max_hold_days=30.0,
        time_stop_min_gain_pct=2.0,
        thesis_decay_enabled=False,
        thesis_decay_min_age_days=3.0,
        thesis_min_score=0.1,
        pdt_guard_enabled=True,
        max_day_trades_under_25k=3,
        min_conviction=0.0,
        max_trade_risk_pct=1.0,
        est_slippage_pct=0.10,
        min_edge_ratio=2.0,
        fractional_enabled=True,
        min_order_usd=1.0,
        default_stop_loss_pct=5.0,
        default_take_profit_pct=12.0,
        scale_out_enabled=False,
        scale_out_pct=50.0,
        kelly_fraction=0.5,
        target_annual_vol_pct=25.0,
        options_enabled=False,
        max_option_premium_pct=1.0,
        # Most of this suite exercises the FRACTIONAL sizing path; the
        # whole-shares mode (GA-2.3, live default ON) has its own tests below.
        whole_shares_only=False,
    )
    base.update(over)
    return RiskLimits(**base)


def _account(equity=100_000.0, cash=100_000.0, buying_power=100_000.0,
             last_equity=100_000.0, positions=None,
             pattern_day_trader=False, daytrade_count=0) -> AccountSnapshot:
    return AccountSnapshot(
        equity=equity, last_equity=last_equity, cash=cash,
        buying_power=buying_power, positions=positions or [],
        pattern_day_trader=pattern_day_trader, daytrade_count=daytrade_count,
    )


def _pos(symbol="AAPL", qty=10.0, price=100.0) -> Position:
    return Position(
        symbol=symbol, qty=qty, avg_entry_price=price, current_price=price,
        market_value=qty * price, unrealized_pl=0.0, unrealized_pl_pct=0.0,
    )


def _buy(symbol="AAPL", conviction=1.0, weight=100.0) -> TradeProposal:
    return TradeProposal(
        symbol=symbol, action=Action.BUY, conviction=conviction,
        target_weight_pct=weight, rationale="test",
    )


# --------------------------------------------------------------------------- #
# Equity buys
# --------------------------------------------------------------------------- #
def test_buy_clamped_to_max_position_pct():
    rm = _rm(_limits(kelly_fraction=0.0))  # disable kelly -> max cap path
    d = rm.evaluate(_buy(weight=100.0), _account(), price=100.0, volatility=0.3)
    assert d.verdict in (RiskVerdict.APPROVED, RiskVerdict.RESIZED), d.reason
    # max_position_pct=5% of 100k = $5k -> 50 shares at $100
    assert d.approved_qty == 50, d.approved_qty
    assert abs(d.approved_notional - 5000) < 1e-6


def test_missing_vol_sizes_down_not_up():
    rm = _rm(_limits())
    with_vol = rm.evaluate(_buy(), _account(), price=100.0, volatility=0.25)
    no_vol = rm.evaluate(_buy(), _account(), price=100.0, volatility=None)
    # Unknown vol assumes a HIGH vol (0.60) so the size must not exceed the known
    # low-vol case — fail safe is smaller-or-equal, never larger.
    assert no_vol.approved_notional <= with_vol.approved_notional


def test_min_trade_price_guard():
    rm = _rm(_limits(min_trade_price_usd=5.0))
    d = rm.evaluate(_buy(), _account(), price=2.0, volatility=0.3)
    assert d.verdict is RiskVerdict.REJECTED
    assert "liquidity" in d.reason.lower() or "below min" in d.reason.lower()


def test_cash_buffer_respected():
    # Almost no deployable cash: 10% buffer of 100k = 10k reserved, only 10.5k cash.
    rm = _rm(_limits())
    acct = _account(cash=10_500.0, buying_power=10_500.0)
    d = rm.evaluate(_buy(), acct, price=100.0, volatility=0.25)
    # Deployable ~= 500 -> at most 5 shares ($500), well under the position cap.
    assert d.approved_notional <= 500 + 1e-6


def test_symbol_exposure_cap_counts_pending():
    rm = _rm(_limits(max_symbol_exposure_pct=10.0))
    # Already holding $9k of a 10k cap, plus $1k pending -> no room left.
    acct = _account(positions=[_pos(qty=90, price=100.0)])  # $9k held
    d = rm.evaluate(_buy(), acct, price=100.0, volatility=0.25, pending_buy_notional=1000.0)
    assert d.verdict is RiskVerdict.REJECTED
    assert "exposure cap" in d.reason.lower()


def test_fractional_buy_under_one_share():
    # Small account: 5% of $2,000 = $100 budget on a $300 stock -> sub-share.
    rm = _rm(_limits(kelly_fraction=0.0))  # use the full position cap
    acct = _account(equity=2_000.0, cash=2_000.0, buying_power=2_000.0, last_equity=2_000.0)
    d = rm.evaluate(_buy(), acct, price=300.0, volatility=0.3)
    assert d.verdict in (RiskVerdict.APPROVED, RiskVerdict.RESIZED), d.reason
    assert 0 < d.approved_qty < 1, d.approved_qty           # fractional
    assert abs(d.approved_notional - 100.0) < 1.0, d.approved_notional


def test_fractional_disabled_rejects_sub_share():
    rm = _rm(_limits(fractional_enabled=False, kelly_fraction=0.0))
    acct = _account(equity=2_000.0, cash=2_000.0, buying_power=2_000.0, last_equity=2_000.0)
    d = rm.evaluate(_buy(), acct, price=300.0, volatility=0.3)  # $100 budget < $300
    assert d.verdict is RiskVerdict.REJECTED


# -- whole-shares mode (GA-2.3): every entry can rest an exchange bracket ---- #
def test_whole_shares_mode_floors_to_int_qty():
    # $5,000 budget on a $333 stock = 15.01 shares -> floored to 15 whole shares.
    rm = _rm(_limits(whole_shares_only=True, kelly_fraction=0.0,
                     max_trade_risk_pct=0.0))
    d = rm.evaluate(_buy(), _account(), price=333.0, volatility=0.25)
    assert d.verdict in (RiskVerdict.APPROVED, RiskVerdict.RESIZED), d.reason
    assert d.approved_qty == 15.0
    assert d.approved_qty == int(d.approved_qty)


def test_whole_shares_mode_rejects_sub_share_budget():
    # Whole-shares overrides fractional: a budget under one share is REJECTED,
    # never silently downgraded to an unprotected fractional buy.
    rm = _rm(_limits(whole_shares_only=True, fractional_enabled=True,
                     kelly_fraction=0.0))
    acct = _account(equity=2_000.0, cash=2_000.0, buying_power=2_000.0,
                    last_equity=2_000.0)
    d = rm.evaluate(_buy(), acct, price=300.0, volatility=0.3)  # $100 < $300
    assert d.verdict is RiskVerdict.REJECTED
    assert "Whole-shares" in d.reason


def test_conviction_floor_rejects_low_edge():
    # A 0.1-conviction idea is below the 0.2 floor -> rejected before sizing (1B.9).
    rm = _rm(_limits(min_conviction=0.2))
    d = rm.evaluate(_buy(conviction=0.1), _account(), price=100.0, volatility=0.25)
    assert d.verdict is RiskVerdict.REJECTED
    assert "conviction" in d.reason.lower()


def test_conviction_floor_allows_real_edge():
    rm = _rm(_limits(min_conviction=0.2))
    d = rm.evaluate(_buy(conviction=0.5), _account(), price=100.0, volatility=0.25)
    assert d.verdict in (RiskVerdict.APPROVED, RiskVerdict.RESIZED), d.reason


def test_new_name_conviction_floor_rejects_fresh_starter():
    # Jul 17-22: every ~-10% realized loss (MU, SPCX x2) was a FRESH name
    # opened at 0.45-0.50 conviction. A new name must clear the higher bar.
    rm = _rm(_limits(min_new_name_conviction=0.5))
    d = rm.evaluate(_buy("NEWCO", conviction=0.45), _account(), price=100.0,
                    volatility=0.25)
    assert d.verdict is RiskVerdict.REJECTED
    assert "new-position floor" in d.reason


def test_new_name_conviction_floor_exempts_topups_and_inert_at_zero():
    # A HELD name keeps the lower min_conviction floor (it earned its slot).
    rm = _rm(_limits(min_new_name_conviction=0.5))
    acct = _account(positions=[_pos("HELD", qty=10, price=100.0)])
    d = rm.evaluate(_buy("HELD", conviction=0.45), acct, price=100.0,
                    volatility=0.25)
    assert d.verdict in (RiskVerdict.APPROVED, RiskVerdict.RESIZED), d.reason
    # 0 disables the gate entirely.
    rm2 = _rm(_limits(min_new_name_conviction=0.0))
    d2 = rm2.evaluate(_buy("NEWCO", conviction=0.45), _account(), price=100.0,
                      volatility=0.25)
    assert d2.verdict in (RiskVerdict.APPROVED, RiskVerdict.RESIZED), d2.reason


def test_min_order_floor_rejects_dust():
    # Budget below the min order $ -> not worth placing even fractionally.
    rm = _rm(_limits(min_order_usd=50.0, kelly_fraction=0.0, max_position_pct=1.0))
    acct = _account(equity=2_000.0, cash=2_000.0, buying_power=2_000.0, last_equity=2_000.0)
    d = rm.evaluate(_buy(), acct, price=300.0, volatility=0.3)  # 1% of 2k = $20 < $50
    assert d.verdict is RiskVerdict.REJECTED
    assert "min order" in d.reason.lower()


# --------------------------------------------------------------------------- #
# Per-trade $-loss cap (small-account survival, 1.5)
# --------------------------------------------------------------------------- #
def test_per_trade_risk_cap_limits_size():
    # 1% of 100k = $1,000 max risk; default 5% stop -> $20,000 max notional, well
    # under the 100% position cap, so the risk cap is the binding constraint.
    rm = _rm(_limits(max_trade_risk_pct=1.0, max_position_pct=100.0,
                     max_symbol_exposure_pct=100.0, kelly_fraction=0.0,
                     min_cash_buffer_pct=0.0))
    d = rm.evaluate(_buy(weight=100.0), _account(), price=100.0, volatility=0.25)
    assert d.verdict in (RiskVerdict.APPROVED, RiskVerdict.RESIZED), d.reason
    assert abs(d.approved_notional - 20_000.0) < 1.0, d.approved_notional


def test_per_trade_risk_cap_wider_stop_means_smaller_size():
    # Same $ risk budget, a wider stop must SHRINK the position (not the $ risk).
    rm = _rm(_limits(max_trade_risk_pct=1.0, max_position_pct=100.0,
                     max_symbol_exposure_pct=100.0, kelly_fraction=0.0,
                     min_cash_buffer_pct=0.0))
    wide = TradeProposal(symbol="AAPL", action=Action.BUY, conviction=1.0,
                         target_weight_pct=100.0, stop_loss_pct=10.0, rationale="t")
    d = rm.evaluate(wide, _account(), price=100.0, volatility=0.25)
    # $1,000 risk / 10% stop = $10,000 notional (half of the 5%-stop case).
    assert abs(d.approved_notional - 10_000.0) < 1.0, d.approved_notional


def test_per_trade_risk_cap_disabled_when_zero():
    rm = _rm(_limits(max_trade_risk_pct=0.0, max_position_pct=100.0,
                     max_symbol_exposure_pct=100.0, kelly_fraction=0.0,
                     min_cash_buffer_pct=0.0))
    d = rm.evaluate(_buy(weight=100.0), _account(), price=100.0, volatility=0.25)
    # No per-trade risk cap -> deploys the full position budget, not the $20k cap.
    assert d.approved_notional > 20_000.0, d.approved_notional


# --------------------------------------------------------------------------- #
# Cost / slippage edge floor (small-account survival, 1.5)
# --------------------------------------------------------------------------- #
def test_edge_floor_rejects_target_below_round_trip_cost():
    # round-trip = 2 * 0.10% = 0.20%; required = 0.20% * 2 = 0.40%. A 0.3% target
    # can't clear the friction -> negative expectancy, reject.
    rm = _rm(_limits(est_slippage_pct=0.10, min_edge_ratio=2.0))
    p = TradeProposal(symbol="AAPL", action=Action.BUY, conviction=1.0,
                      target_weight_pct=5.0, take_profit_pct=0.3, rationale="t")
    d = rm.evaluate(p, _account(), price=100.0, volatility=0.25)
    assert d.verdict is RiskVerdict.REJECTED
    assert "friction" in d.reason.lower() or "round-trip" in d.reason.lower()


def test_edge_floor_allows_target_that_clears_cost():
    rm = _rm(_limits(est_slippage_pct=0.10, min_edge_ratio=2.0))
    p = TradeProposal(symbol="AAPL", action=Action.BUY, conviction=1.0,
                      target_weight_pct=5.0, take_profit_pct=5.0, rationale="t")
    d = rm.evaluate(p, _account(), price=100.0, volatility=0.25)
    assert d.verdict in (RiskVerdict.APPROVED, RiskVerdict.RESIZED), d.reason


def test_edge_floor_disabled_when_slippage_zero():
    rm = _rm(_limits(est_slippage_pct=0.0))
    p = TradeProposal(symbol="AAPL", action=Action.BUY, conviction=1.0,
                      target_weight_pct=5.0, take_profit_pct=0.1, rationale="t")
    d = rm.evaluate(p, _account(), price=100.0, volatility=0.25)
    assert d.verdict in (RiskVerdict.APPROVED, RiskVerdict.RESIZED), d.reason


# --------------------------------------------------------------------------- #
# Account-wide halts
# --------------------------------------------------------------------------- #
def test_daily_loss_halts_new_buys():
    rm = _rm(_limits(max_daily_loss_pct=3.0))
    acct = _account(equity=96_000.0, last_equity=100_000.0)  # -4% day
    d = rm.evaluate(_buy(), acct, price=100.0, volatility=0.25)
    assert d.verdict is RiskVerdict.REJECTED
    assert "loss" in d.reason.lower()


def test_drawdown_halts_new_buys():
    state = PortfolioState(path="/tmp/_does_not_persist_dd.json")
    state.peak_equity = 100_000.0
    rm = _rm(_limits(max_drawdown_pct=15.0), state=state)
    acct = _account(equity=80_000.0, last_equity=80_000.0)  # 20% off peak
    d = rm.evaluate(_buy(), acct, price=100.0, volatility=0.25)
    assert d.verdict is RiskVerdict.REJECTED
    assert "drawdown" in d.reason.lower()


def test_kill_switch_blocks_buys_but_not_sells():
    rm = _rm(_limits(), kill_switch=True)
    buy = rm.evaluate(_buy(), _account(), price=100.0, volatility=0.25)
    assert buy.verdict is RiskVerdict.REJECTED
    acct = _account(positions=[_pos()])
    sell = rm.evaluate(
        TradeProposal(symbol="AAPL", action=Action.SELL, conviction=1.0,
                      target_weight_pct=0.0, rationale="exit"),
        acct, price=100.0,
    )
    assert sell.verdict is RiskVerdict.APPROVED  # risk reduction always allowed


def test_max_open_positions_halt():
    rm = _rm(_limits(max_open_positions=2))
    acct = _account(positions=[_pos("AAPL"), _pos("MSFT")])
    d = rm.evaluate(_buy("NVDA"), acct, price=100.0, volatility=0.25)
    assert d.verdict is RiskVerdict.REJECTED
    assert "max open positions" in d.reason.lower()


def test_max_open_positions_exempts_topup_of_held_name():
    # A top-up reuses the held symbol's position row, so the slot cap must not
    # apply (2026-07-13: a full 15/15 book rejected every buy, including
    # top-ups the cooldown guard had explicitly promised for later).
    rm = _rm(_limits(max_open_positions=2))
    acct = _account(positions=[_pos("AAPL"), _pos("MSFT")])
    d = rm.evaluate(_buy("AAPL"), acct, price=100.0, volatility=0.25)
    assert "max open positions" not in d.reason.lower()


def test_rotation_sell_frees_slot_for_new_buy_same_cycle():
    # Full-book rotation (postmortem 2026-07-14: MU 0.63 died at the slot cap
    # while CVX sat held at 0.46): once the orchestrator folds a decision SELL
    # back into the snapshot, the very same buy must clear the slot cap.
    from investment_strategy.orchestrator import Orchestrator

    rm = _rm(_limits(max_open_positions=2))
    acct = _account(positions=[_pos("CVX"), _pos("AAPL")])
    d = rm.evaluate(_buy("MU"), acct, price=100.0, volatility=0.25)
    assert d.verdict is RiskVerdict.REJECTED
    assert "max open positions" in d.reason.lower()

    Orchestrator._apply_pending_close(acct, "CVX")   # the paired rotation sell
    d2 = rm.evaluate(_buy("MU"), acct, price=100.0, volatility=0.25)
    assert "max open positions" not in d2.reason.lower()


# --------------------------------------------------------------------------- #
# Earnings-blackout guard
# --------------------------------------------------------------------------- #
def test_earnings_blackout_blocks_buy_inside_window():
    rm = _rm(_limits(earnings_blackout_days=3))
    d = rm.evaluate(_buy(), _account(), price=100.0, volatility=0.25, days_to_earnings=2)
    assert d.verdict is RiskVerdict.REJECTED
    assert "earnings" in d.reason.lower()


def test_earnings_blackout_blocks_on_report_day():
    rm = _rm(_limits(earnings_blackout_days=3))
    d = rm.evaluate(_buy(), _account(), price=100.0, volatility=0.25, days_to_earnings=0)
    assert d.verdict is RiskVerdict.REJECTED


def test_earnings_blackout_allows_buy_outside_window():
    rm = _rm(_limits(earnings_blackout_days=3))
    d = rm.evaluate(_buy(), _account(), price=100.0, volatility=0.25, days_to_earnings=10)
    assert d.verdict in (RiskVerdict.APPROVED, RiskVerdict.RESIZED)


def test_earnings_blackout_fails_open_when_date_unknown():
    rm = _rm(_limits(earnings_blackout_days=3))
    d = rm.evaluate(_buy(), _account(), price=100.0, volatility=0.25, days_to_earnings=None)
    assert d.verdict in (RiskVerdict.APPROVED, RiskVerdict.RESIZED)


def test_earnings_blackout_disabled_when_zero():
    rm = _rm(_limits(earnings_blackout_days=0))
    d = rm.evaluate(_buy(), _account(), price=100.0, volatility=0.25, days_to_earnings=1)
    assert d.verdict in (RiskVerdict.APPROVED, RiskVerdict.RESIZED)


def test_earnings_blackout_does_not_block_sells():
    rm = _rm(_limits(earnings_blackout_days=3))
    acct = _account(positions=[_pos(qty=10.0)])
    d = rm.evaluate(
        TradeProposal(symbol="AAPL", action=Action.SELL, conviction=1.0,
                      target_weight_pct=0.0, rationale="exit"),
        acct, price=100.0, days_to_earnings=1,
    )
    assert d.verdict is RiskVerdict.APPROVED   # exits are never blacked out


# --------------------------------------------------------------------------- #
# Market-regime sizing multiplier
# --------------------------------------------------------------------------- #
def test_regime_multiplier_scales_size_down():
    rm = _rm(_limits(kelly_fraction=0.0))  # position-cap path: 5% of 100k = $5k
    full = rm.evaluate(_buy(), _account(), price=100.0, volatility=0.3, regime_multiplier=1.0)
    half = rm.evaluate(_buy(), _account(), price=100.0, volatility=0.3, regime_multiplier=0.5)
    assert abs(half.approved_notional - full.approved_notional * 0.5) < 100.0


def test_regime_multiplier_cannot_inflate_above_cap():
    rm = _rm(_limits(kelly_fraction=0.0))
    d = rm.evaluate(_buy(), _account(), price=100.0, volatility=0.3, regime_multiplier=5.0)
    assert d.approved_notional <= 5_000.0 + 1e-6   # clamped to <=1, still capped


def test_regime_filter_disabled_ignores_multiplier():
    rm = _rm(_limits(regime_filter_enabled=False, kelly_fraction=0.0))
    d = rm.evaluate(_buy(), _account(), price=100.0, volatility=0.3, regime_multiplier=0.1)
    assert abs(d.approved_notional - 5_000.0) < 1e-6   # full size despite 0.1


# --------------------------------------------------------------------------- #
# No-leverage gross-exposure cap (margin safety)
# --------------------------------------------------------------------------- #
def test_gross_cap_blocks_when_fully_deployed():
    # 100% gross cap, already holding ~equity in positions -> no room, reject.
    rm = _rm(_limits(max_gross_exposure_pct=100.0))
    acct = _account(equity=10_000.0, cash=10_000.0, buying_power=20_000.0,
                    last_equity=10_000.0,
                    positions=[_pos("MSFT", qty=100, price=100.0)])  # $10k held
    d = rm.evaluate(_buy("NVDA"), acct, price=100.0, volatility=0.25)
    assert d.verdict is RiskVerdict.REJECTED
    assert "gross" in d.reason.lower() or "leverage" in d.reason.lower()


def test_gross_cap_does_not_use_margin_buying_power():
    # buying_power is 2x equity (margin), but the gross cap pins deployment to
    # equity: ~half already held leaves room only up to equity, not 2x.
    rm = _rm(_limits(max_gross_exposure_pct=100.0, max_position_pct=100.0,
                     min_cash_buffer_pct=0.0, kelly_fraction=0.0))
    acct = _account(equity=10_000.0, cash=10_000.0, buying_power=20_000.0,
                    last_equity=10_000.0,
                    positions=[_pos("MSFT", qty=80, price=100.0)])  # $8k held
    d = rm.evaluate(_buy("NVDA", weight=100.0), acct, price=100.0, volatility=0.25)
    # Room = 10k - 8k = 2k, never the 12k that 2x buying power would allow.
    assert d.approved_notional <= 2_000.0 + 1e-6


# --------------------------------------------------------------------------- #
# Sector concentration cap
# --------------------------------------------------------------------------- #
def test_sector_cap_blocks_when_sector_full():
    rm = _rm(_limits(max_sector_exposure_pct=30.0))
    acct = _account(equity=10_000.0, last_equity=10_000.0)  # 30% cap = $3k per sector
    d = rm.evaluate(_buy("NVDA"), acct, price=100.0, volatility=0.25,
                    sector="Technology", sector_exposure_usd=3_000.0)
    assert d.verdict is RiskVerdict.REJECTED
    assert "sector" in d.reason.lower()


def test_sector_cap_limits_partial_room():
    rm = _rm(_limits(max_sector_exposure_pct=30.0, max_position_pct=100.0,
                     kelly_fraction=0.0, min_cash_buffer_pct=0.0))
    acct = _account(equity=10_000.0, cash=10_000.0, buying_power=10_000.0,
                    last_equity=10_000.0)
    d = rm.evaluate(_buy("NVDA", weight=100.0), acct, price=100.0, volatility=0.25,
                    sector="Technology", sector_exposure_usd=2_500.0)
    # 30% of 10k = 3k; 2.5k already in sector -> only $500 of room.
    assert d.approved_notional <= 500.0 + 1e-6


def test_sector_cap_skipped_when_sector_unknown():
    rm = _rm(_limits(max_sector_exposure_pct=30.0, kelly_fraction=0.0))
    acct = _account(equity=10_000.0, cash=10_000.0, buying_power=10_000.0,
                    last_equity=10_000.0)
    # sector=None -> cap skipped; normal position cap applies.
    d = rm.evaluate(_buy("NVDA"), acct, price=100.0, volatility=0.25,
                    sector=None, sector_exposure_usd=9_999.0)
    assert d.verdict in (RiskVerdict.APPROVED, RiskVerdict.RESIZED)


# --------------------------------------------------------------------------- #
# Pattern-Day-Trader guard (small margin accounts)
# --------------------------------------------------------------------------- #
def test_pdt_flagged_under_25k_blocks_buys():
    rm = _rm(_limits())
    acct = _account(equity=1_000.0, cash=1_000.0, buying_power=1_000.0,
                    last_equity=1_000.0, pattern_day_trader=True)
    d = rm.evaluate(_buy(), acct, price=100.0, volatility=0.25)
    assert d.verdict is RiskVerdict.REJECTED
    assert "pdt" in d.reason.lower() or "cash account" in d.reason.lower()


def test_pdt_daytrade_count_at_limit_blocks_buys():
    rm = _rm(_limits(max_day_trades_under_25k=3))
    acct = _account(equity=1_000.0, cash=1_000.0, buying_power=1_000.0,
                    last_equity=1_000.0, daytrade_count=3)
    d = rm.evaluate(_buy(), acct, price=100.0, volatility=0.25)
    assert d.verdict is RiskVerdict.REJECTED
    assert "day-trade" in d.reason.lower()


def test_pdt_guard_inert_below_limit():
    rm = _rm(_limits(max_day_trades_under_25k=3))
    acct = _account(equity=1_000.0, cash=1_000.0, buying_power=1_000.0,
                    last_equity=1_000.0, daytrade_count=2)
    d = rm.evaluate(_buy(), acct, price=100.0, volatility=0.25)
    assert d.verdict in (RiskVerdict.APPROVED, RiskVerdict.RESIZED)


def test_pdt_guard_inert_at_or_above_25k():
    # A $25k+ account is not PDT-restricted even if flagged.
    rm = _rm(_limits())
    acct = _account(equity=30_000.0, cash=30_000.0, buying_power=30_000.0,
                    last_equity=30_000.0, pattern_day_trader=True, daytrade_count=9)
    d = rm.evaluate(_buy(), acct, price=100.0, volatility=0.25)
    assert d.verdict in (RiskVerdict.APPROVED, RiskVerdict.RESIZED)


def test_pdt_guard_cash_account_unaffected():
    # Cash account: pattern_day_trader=False, daytrade_count=0 -> inert.
    rm = _rm(_limits())
    acct = _account(equity=500.0, cash=500.0, buying_power=500.0,
                    last_equity=500.0)
    d = rm.evaluate(_buy(), acct, price=100.0, volatility=0.25)
    assert d.verdict in (RiskVerdict.APPROVED, RiskVerdict.RESIZED)


def test_pdt_guard_can_be_disabled():
    rm = _rm(_limits(pdt_guard_enabled=False))
    acct = _account(equity=1_000.0, cash=1_000.0, buying_power=1_000.0,
                    last_equity=1_000.0, pattern_day_trader=True)
    d = rm.evaluate(_buy(), acct, price=100.0, volatility=0.25)
    assert d.verdict in (RiskVerdict.APPROVED, RiskVerdict.RESIZED)


def test_pdt_does_not_block_sells():
    rm = _rm(_limits())
    acct = _account(equity=1_000.0, cash=1_000.0, buying_power=1_000.0,
                    last_equity=1_000.0, pattern_day_trader=True, positions=[_pos(qty=5.0)])
    d = rm.evaluate(
        TradeProposal(symbol="AAPL", action=Action.SELL, conviction=1.0,
                      target_weight_pct=0.0, rationale="exit"),
        acct, price=100.0,
    )
    assert d.verdict is RiskVerdict.APPROVED   # closing always allowed


# --------------------------------------------------------------------------- #
# Sells
# --------------------------------------------------------------------------- #
def test_sell_without_position_rejected():
    rm = _rm(_limits())
    d = rm.evaluate(
        TradeProposal(symbol="AAPL", action=Action.SELL, conviction=1.0,
                      target_weight_pct=0.0, rationale="exit"),
        _account(), price=100.0,
    )
    assert d.verdict is RiskVerdict.REJECTED


def test_sell_approves_full_held_qty():
    rm = _rm(_limits())
    acct = _account(positions=[_pos(qty=37.0)])
    d = rm.evaluate(
        TradeProposal(symbol="AAPL", action=Action.SELL, conviction=1.0,
                      target_weight_pct=0.0, rationale="exit"),
        acct, price=100.0,
    )
    assert d.verdict is RiskVerdict.APPROVED
    assert d.approved_qty == 37.0


# --------------------------------------------------------------------------- #
# Options (defined-risk only)
# --------------------------------------------------------------------------- #
def _opt(strategy, legs) -> TradeProposal:
    return TradeProposal(
        symbol="AAPL", action=Action.BUY, conviction=1.0, target_weight_pct=2.0,
        rationale="opt", instrument=Instrument.OPTION,
        option_strategy=strategy, option_legs=legs,
    )


def _opt_exp(days: int = 30) -> str:
    """Dynamic expiry N days out — a fixed date here is a time bomb once the
    DTE gate exists (it drifts under the 7d minimum and flips approvals to
    rejections as the calendar advances)."""
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%d")


def test_options_disabled_rejected():
    rm = _rm(_limits(options_enabled=False))
    p = _opt(OptionStrategy.LONG_CALL,
             [OptionLeg(expiry=_opt_exp(30), strike=200, right="call", side=Action.BUY)])
    d = rm.evaluate_option(p, _account(), est_premium_per_contract=2.0)
    assert d.verdict is RiskVerdict.REJECTED


def test_naked_short_call_rejected():
    rm = _rm(_limits(options_enabled=True))
    p = _opt(OptionStrategy.BULL_CALL_SPREAD,
             [OptionLeg(expiry=_opt_exp(30), strike=210, right="call", side=Action.SELL)])
    d = rm.evaluate_option(p, _account(), est_premium_per_contract=2.0)
    assert d.verdict is RiskVerdict.REJECTED
    assert "short call" in d.reason.lower() or "must be one long" in d.reason.lower()


def test_net_credit_rejected():
    rm = _rm(_limits(options_enabled=True))
    p = _opt(OptionStrategy.LONG_CALL,
             [OptionLeg(expiry=_opt_exp(30), strike=200, right="call", side=Action.BUY)])
    d = rm.evaluate_option(p, _account(), est_premium_per_contract=-1.0)  # credit
    assert d.verdict is RiskVerdict.REJECTED


def test_defined_risk_spread_approved_and_premium_capped():
    rm = _rm(_limits(options_enabled=True, max_option_premium_pct=1.0))
    legs = [
        OptionLeg(expiry=_opt_exp(30), strike=200, right="call", side=Action.BUY),
        OptionLeg(expiry=_opt_exp(30), strike=210, right="call", side=Action.SELL),
    ]
    p = _opt(OptionStrategy.BULL_CALL_SPREAD, legs)
    # 1% of 100k = $1000 budget; $2/share -> $200/contract -> 5 contracts.
    d = rm.evaluate_option(p, _account(), est_premium_per_contract=2.0)
    assert d.verdict is RiskVerdict.APPROVED
    assert d.approved_qty == 5
    assert d.approved_notional <= 1000 + 1e-6


def test_option_sub_floor_leg_premium_rejected():
    """A cheapest-leg mid below the premium floor is a deep-OTM/illiquid junk
    contract (the 2026-07-23 T blowup: a $0.01 leg minted 900 dead contracts) —
    refuse it even though the tiny net debit fits the cap."""
    rm = _rm(_limits(options_enabled=True, min_option_premium=0.10))
    p = _opt(OptionStrategy.LONG_CALL,
             [OptionLeg(expiry=_opt_exp(30), strike=200, right="call", side=Action.BUY)])
    d = rm.evaluate_option(p, _account(), est_premium_per_contract=0.05,
                           min_leg_premium=0.01)
    assert d.verdict is RiskVerdict.REJECTED
    assert "floor" in d.reason.lower()


def test_option_leg_premium_at_floor_passes_floor_gate():
    """Exactly at the floor the leg is a real contract — not rejected on the
    floor (strictly-less-than, so the boundary is admitted)."""
    rm = _rm(_limits(options_enabled=True, min_option_premium=0.10))
    p = _opt(OptionStrategy.LONG_CALL,
             [OptionLeg(expiry=_opt_exp(30), strike=200, right="call", side=Action.BUY)])
    d = rm.evaluate_option(p, _account(), est_premium_per_contract=0.50,
                           min_leg_premium=0.10)
    assert d.verdict is RiskVerdict.APPROVED


def test_option_none_min_leg_premium_fails_open():
    """No per-leg data (None) must not reject on the floor — est_premium<=0 is
    the backstop for quote-less legs; the floor only acts on data we have."""
    rm = _rm(_limits(options_enabled=True, min_option_premium=0.10))
    p = _opt(OptionStrategy.LONG_CALL,
             [OptionLeg(expiry=_opt_exp(30), strike=200, right="call", side=Action.BUY)])
    d = rm.evaluate_option(p, _account(), est_premium_per_contract=2.0,
                           min_leg_premium=None)
    assert d.verdict is RiskVerdict.APPROVED


def test_option_contract_count_clamped_to_cap():
    """A cheap-but-valid premium that would size past the count cap is clamped —
    never the 900-lot thin-book order that tripled the T fill."""
    rm = _rm(_limits(options_enabled=True, max_option_premium_pct=1.0,
                     min_option_premium=0.10, max_option_contracts=50))
    p = _opt(OptionStrategy.LONG_CALL,
             [OptionLeg(expiry=_opt_exp(30), strike=200, right="call", side=Action.BUY)])
    # $0.10/share -> $10/contract; 1% of $100k = $1000 budget -> 100 contracts,
    # clamped to 50 (and $500 spent, well under the cap).
    d = rm.evaluate_option(p, _account(), est_premium_per_contract=0.10,
                           min_leg_premium=0.10)
    assert d.verdict is RiskVerdict.APPROVED
    assert d.approved_qty == 50
    assert d.approved_notional == 500.0


# --------------------------------------------------------------------------- #
# R.1 — vol-scaled ("ATR-style") dynamic stops
# --------------------------------------------------------------------------- #
_SQRT252 = 252 ** 0.5


def test_vol_stops_scale_stop_and_take_to_realized_vol():
    rm = _rm(_limits(vol_stops_enabled=True, vol_stop_mult=2.5,
                     vol_stop_take_ratio=2.5))
    vol = 0.40                                   # calm-ish name
    d = rm.evaluate(_buy(), _account(), price=100.0, volatility=vol)
    expected_stop = 2.5 * (vol / _SQRT252 * 100.0)   # ~6.3%, inside the clamp
    assert d.verdict in (RiskVerdict.APPROVED, RiskVerdict.RESIZED), d.reason
    assert abs(d.stop_loss_pct - expected_stop) < 1e-9
    assert abs(d.take_profit_pct - expected_stop * 2.5) < 1e-9


def test_vol_stops_clamped_at_both_ends():
    rm = _rm(_limits(vol_stops_enabled=True, vol_stop_mult=2.5,
                     vol_stop_min_pct=4.0, vol_stop_max_pct=15.0))
    # A very quiet name would compute ~1.6% -> clamped up to the 4% floor.
    d = rm.evaluate(_buy(), _account(), price=100.0, volatility=0.10)
    assert d.stop_loss_pct == 4.0
    # A meme-vol name would compute ~31.5% -> clamped down to the 15% ceiling.
    d = rm.evaluate(_buy(), _account(), price=100.0, volatility=2.0)
    assert d.stop_loss_pct == 15.0


def test_vol_stops_override_llm_proposed_levels():
    rm = _rm(_limits(vol_stops_enabled=True))
    p = _buy()
    p.stop_loss_pct = 1.0        # untrusted LLM numbers must not win
    p.take_profit_pct = 99.0
    d = rm.evaluate(p, _account(), price=100.0, volatility=0.40)
    assert d.stop_loss_pct != 1.0
    assert d.take_profit_pct != 99.0


def test_vol_stops_fall_back_to_defaults_when_vol_unknown():
    rm = _rm(_limits(vol_stops_enabled=True))
    d = rm.evaluate(_buy(), _account(), price=100.0, volatility=None)
    assert d.stop_loss_pct == 5.0        # fixture default_stop_loss_pct
    assert d.take_profit_pct == 12.0


def test_vol_stops_disabled_keeps_fixed_behavior():
    rm = _rm(_limits(vol_stops_enabled=False))
    d = rm.evaluate(_buy(), _account(), price=100.0, volatility=0.40)
    assert d.stop_loss_pct == 5.0
    assert d.take_profit_pct == 12.0


def test_wider_vol_stop_shrinks_position_via_trade_risk_cap():
    """The safety interplay: 2d caps $-at-risk = notional * stop%, so the wide
    stop a volatile name gets must buy FEWER dollars, not risk more."""
    lim = _limits(vol_stops_enabled=True, max_trade_risk_pct=1.0,
                  max_position_pct=100.0, max_symbol_exposure_pct=100.0,
                  min_cash_buffer_pct=0.0, kelly_fraction=0.0)
    rm = _rm(lim)
    calm = rm.evaluate(_buy(), _account(), price=100.0, volatility=0.20)
    wild = rm.evaluate(_buy(), _account(), price=100.0, volatility=1.50)
    assert wild.stop_loss_pct > calm.stop_loss_pct
    assert wild.approved_notional < calm.approved_notional
    # Dollar risk if the stop fires stays pinned at ~1% of equity for both
    # (cent-level slack: qty is rounded to 6dp before notional is recomputed).
    for d in (calm, wild):
        assert d.approved_notional * d.stop_loss_pct / 100.0 <= 1_000.0 + 0.01


# --------------------------------------------------------------------------- #
# R.2 — pairwise-correlation guard
# --------------------------------------------------------------------------- #
def test_corr_guard_rejects_duplicate_bet():
    rm = _rm(_limits(max_pairwise_corr=0.85))
    d = rm.evaluate(_buy("SMCI"), _account(), price=100.0, volatility=0.3,
                    max_held_corr=0.91, corr_symbol="NVDA")
    assert d.verdict is RiskVerdict.REJECTED
    assert "NVDA" in d.reason and "same bet" in d.reason


def test_corr_guard_passes_below_cap():
    rm = _rm(_limits(max_pairwise_corr=0.85))
    d = rm.evaluate(_buy("SMCI"), _account(), price=100.0, volatility=0.3,
                    max_held_corr=0.60, corr_symbol="NVDA")
    assert d.verdict is not RiskVerdict.REJECTED, d.reason


def test_corr_guard_fails_open_when_unknown():
    rm = _rm(_limits(max_pairwise_corr=0.85))
    d = rm.evaluate(_buy("SMCI"), _account(), price=100.0, volatility=0.3,
                    max_held_corr=None)
    assert d.verdict is not RiskVerdict.REJECTED, d.reason


def test_corr_guard_off_at_zero():
    rm = _rm(_limits(max_pairwise_corr=0.0))
    d = rm.evaluate(_buy("SMCI"), _account(), price=100.0, volatility=0.3,
                    max_held_corr=0.99, corr_symbol="NVDA")
    assert d.verdict is not RiskVerdict.REJECTED, d.reason


# --------------------------------------------------------------------------- #
# Churn guards + cycle budget fair-share (the 2026-07-06 all-LLY fixes)
# --------------------------------------------------------------------------- #
def _fresh_state() -> PortfolioState:
    p = os.path.join(tempfile.gettempdir(), f"_rm_test_{uuid.uuid4().hex}.json")
    return PortfolioState(path=p)


def test_top_up_cooldown_rejects_recent_rebuy():
    # LLY was bought 10 times on 2026-07-06, one per 30-min cycle. With a 4h
    # spacing, a buy 0.5h after the last one is refused.
    state = _fresh_state()
    state.register_buy("LLY")
    rm = _rm(_limits(min_add_interval_hours=4.0), state=state)
    d = rm.evaluate(_buy("LLY"), _account(), price=100.0, volatility=0.3)
    assert d.verdict is RiskVerdict.REJECTED
    assert "churn guard" in d.reason


def test_top_up_cooldown_allows_spaced_add():
    from datetime import datetime, timedelta, timezone

    state = _fresh_state()
    state.register_buy("LLY", when=datetime.now(timezone.utc) - timedelta(hours=5))
    rm = _rm(_limits(min_add_interval_hours=4.0), state=state)
    d = rm.evaluate(_buy("LLY"), _account(), price=100.0, volatility=0.3)
    assert d.verdict is not RiskVerdict.REJECTED, d.reason


def test_top_up_cooldown_off_at_zero():
    state = _fresh_state()
    state.register_buy("LLY")
    rm = _rm(_limits(min_add_interval_hours=0.0), state=state)
    d = rm.evaluate(_buy("LLY"), _account(), price=100.0, volatility=0.3)
    assert d.verdict is not RiskVerdict.REJECTED, d.reason


def test_reentry_cooldown_rejects_fresh_rebuy_after_exit():
    # CRWD trail-stopped at 10:54 and was back on the candidate slate at 11:05.
    # With no position held and a recent exit, the fresh re-buy is refused.
    state = _fresh_state()
    state.register_exit("CRWD")
    rm = _rm(_limits(reentry_cooldown_hours=24.0), state=state)
    d = rm.evaluate(_buy("CRWD"), _account(), price=100.0, volatility=0.3)
    assert d.verdict is RiskVerdict.REJECTED
    assert "re-entry" in d.reason.lower()


def test_reentry_cooldown_does_not_gate_adds_to_held_position():
    # A recorded exit (e.g. an old scale-out) must not block topping up a name
    # we STILL hold — only fresh entries are cooled down.
    state = _fresh_state()
    state.register_exit("AAPL")
    rm = _rm(_limits(reentry_cooldown_hours=24.0, min_add_interval_hours=0.0),
             state=state)
    acct = _account(positions=[_pos("AAPL", qty=1.0, price=100.0)])
    d = rm.evaluate(_buy("AAPL"), acct, price=100.0, volatility=0.3)
    assert d.verdict is not RiskVerdict.REJECTED, d.reason


def test_price_aware_reentry_blocks_rebuy_above_exit():
    # Time cooldown OFF, but we exited PATH at $90 and it's now $95 — re-buying
    # above the exit is chasing; blocked unless the composite overrides.
    state = _fresh_state()
    state.register_exit("PATH", price=90.0)
    rm = _rm(_limits(reentry_cooldown_hours=0.0,
                     reentry_price_override_composite=0.5), state=state)
    d = rm.evaluate(_buy("PATH"), _account(), price=95.0, volatility=0.3)
    assert d.verdict is RiskVerdict.REJECTED
    assert "above the exit" in d.reason

    # Below the exit price -> fine (buying the dip we sold into, not chasing).
    d2 = rm.evaluate(_buy("PATH"), _account(), price=85.0, volatility=0.3)
    assert d2.verdict is not RiskVerdict.REJECTED, d2.reason


def test_price_aware_reentry_composite_override():
    state = _fresh_state()
    state.register_exit("PATH", price=90.0)
    rm = _rm(_limits(reentry_cooldown_hours=0.0,
                     reentry_price_override_composite=0.5), state=state)
    # Strong composite = genuine new edge -> allowed above the exit.
    d = rm.evaluate(_buy("PATH"), _account(), price=95.0, volatility=0.3,
                    composite_score=0.8)
    assert d.verdict is not RiskVerdict.REJECTED, d.reason


def test_price_aware_reentry_fails_open_without_exit_price():
    # An exit recorded with no price (older rows) must not block anything.
    state = _fresh_state()
    state.register_exit("PATH")   # no price
    rm = _rm(_limits(reentry_cooldown_hours=0.0), state=state)
    d = rm.evaluate(_buy("PATH"), _account(), price=95.0, volatility=0.3)
    assert d.verdict is not RiskVerdict.REJECTED, d.reason


def test_cycle_budget_cap_bounds_the_order():
    # The orchestrator hands each buy its fair share of the cycle's cash; the
    # order must not exceed it even when every other cap allows more.
    rm = _rm(_limits(kelly_fraction=0.0, min_cash_buffer_pct=0.0))
    d = rm.evaluate(_buy(), _account(), price=100.0, volatility=0.3,
                    cycle_budget_cap=1_234.0)
    assert d.verdict in (RiskVerdict.APPROVED, RiskVerdict.RESIZED), d.reason
    assert d.approved_notional <= 1_234.0 + 1e-6


def test_cycle_budget_cap_none_changes_nothing():
    rm = _rm(_limits(kelly_fraction=0.0))
    d = rm.evaluate(_buy(), _account(), price=100.0, volatility=0.3,
                    cycle_budget_cap=None)
    assert abs(d.approved_notional - 5_000.0) < 1e-6  # the 5% position cap


def test_dust_guard_min_order_scales_with_equity():
    # 0.05% of $100k = $50: a $30 order is dust on this book and refused, even
    # though it clears the $1 absolute floor.
    rm = _rm(_limits(min_order_pct=0.05, min_order_usd=1.0, kelly_fraction=0.0))
    d = rm.evaluate(_buy(), _account(), price=100.0, volatility=0.3,
                    cycle_budget_cap=30.0)
    assert d.verdict is RiskVerdict.REJECTED
    assert "min order" in d.reason.lower()


def test_dust_guard_keeps_small_floats_tradable():
    # On a $2k book the % min is $1 — a $100 order still trades.
    rm = _rm(_limits(min_order_pct=0.05, kelly_fraction=0.0))
    acct = _account(equity=2_000.0, cash=2_000.0, buying_power=2_000.0,
                    last_equity=2_000.0)
    d = rm.evaluate(_buy(), acct, price=300.0, volatility=0.3)
    assert d.verdict in (RiskVerdict.APPROVED, RiskVerdict.RESIZED), d.reason


# --------------------------------------------------------------------------- #
# Daily concentration brake (A1)
# --------------------------------------------------------------------------- #
def test_daily_buy_count_cap_rejects_nth_buy():
    state = _fresh_state()
    state.register_daily_deploy("LLY", 1000.0)
    state.register_daily_deploy("LLY", 1000.0)
    state.register_daily_deploy("LLY", 1000.0)
    rm = _rm(_limits(max_daily_buys_per_symbol=3, max_daily_symbol_deploy_pct=0), state=state)
    d = rm.evaluate(_buy("LLY"), _account(), price=100.0, volatility=0.3)
    assert d.verdict is RiskVerdict.REJECTED
    assert "concentration guard" in d.reason


def test_daily_buy_count_cap_allows_before_limit():
    state = _fresh_state()
    state.register_daily_deploy("LLY", 1000.0)
    rm = _rm(_limits(max_daily_buys_per_symbol=3, max_daily_symbol_deploy_pct=0), state=state)
    d = rm.evaluate(_buy("LLY"), _account(), price=100.0, volatility=0.3)
    assert d.verdict is not RiskVerdict.REJECTED, d.reason


def test_daily_symbol_ceiling_rejects_when_exhausted():
    state = _fresh_state()
    state.register_daily_deploy("LLY", 8000.0)  # 8% of 100k = ceiling
    rm = _rm(_limits(max_daily_symbol_deploy_pct=8.0, max_daily_buys_per_symbol=0,
                     min_order_pct=0.05), state=state)
    d = rm.evaluate(_buy("LLY"), _account(), price=100.0, volatility=0.3)
    assert d.verdict is RiskVerdict.REJECTED
    assert "daily ceiling" in d.reason.lower() or "concentration guard" in d.reason


def test_daily_symbol_ceiling_clamps_to_remaining_room():
    state = _fresh_state()
    state.register_daily_deploy("LLY", 5000.0)  # 5k of 8k used; 3k room
    rm = _rm(_limits(max_daily_symbol_deploy_pct=8.0, max_daily_buys_per_symbol=0,
                     min_order_usd=1.0, min_order_pct=0.0,
                     kelly_fraction=0.0, max_position_pct=100.0,
                     max_symbol_exposure_pct=100.0, max_gross_exposure_pct=100.0,
                     max_trade_risk_pct=0.0), state=state)
    d = rm.evaluate(_buy("LLY", weight=100.0), _account(), price=10.0, volatility=0.3)
    assert d.verdict in (RiskVerdict.APPROVED, RiskVerdict.RESIZED), d.reason
    assert d.approved_notional <= 3000.0 + 1.0  # within remaining room (+$1 fp tolerance)


def test_daily_gates_off_at_zero():
    state = _fresh_state()
    for _ in range(10):
        state.register_daily_deploy("LLY", 1000.0)
    rm = _rm(_limits(max_daily_buys_per_symbol=0, max_daily_symbol_deploy_pct=0), state=state)
    d = rm.evaluate(_buy("LLY"), _account(), price=100.0, volatility=0.3)
    assert d.verdict is not RiskVerdict.REJECTED, d.reason


# --------------------------------------------------------------------------- #
# Top-up evidence gate (B4)
# --------------------------------------------------------------------------- #
def test_topup_evidence_rejects_flat_conviction():
    state = _fresh_state()
    state.register_buy("LLY", conviction=0.78)
    rm = _rm(_limits(topup_min_conviction_delta=0.05, min_add_interval_hours=0), state=state)
    acct = _account(positions=[_pos("LLY", qty=1.0, price=100.0)])
    d = rm.evaluate(_buy("LLY", conviction=0.78), acct, price=100.0, volatility=0.3)
    assert d.verdict is RiskVerdict.REJECTED
    assert "top-up conviction" in d.reason.lower()


def test_topup_evidence_allows_higher_conviction():
    state = _fresh_state()
    state.register_buy("LLY", conviction=0.78)
    rm = _rm(_limits(topup_min_conviction_delta=0.05, min_add_interval_hours=0), state=state)
    acct = _account(positions=[_pos("LLY", qty=1.0, price=100.0)])
    d = rm.evaluate(_buy("LLY", conviction=0.85), acct, price=100.0, volatility=0.3)
    assert d.verdict is not RiskVerdict.REJECTED, d.reason


def test_topup_evidence_fails_open_no_prior():
    state = _fresh_state()  # no prior conviction recorded
    rm = _rm(_limits(topup_min_conviction_delta=0.05, min_add_interval_hours=0), state=state)
    acct = _account(positions=[_pos("LLY", qty=1.0, price=100.0)])
    d = rm.evaluate(_buy("LLY", conviction=0.5), acct, price=100.0, volatility=0.3)
    assert d.verdict is not RiskVerdict.REJECTED, d.reason


def test_topup_evidence_ignores_fresh_entries():
    # Gate should not fire on a first entry (no position held)
    state = _fresh_state()
    state.register_buy("LLY", conviction=0.90)  # prior recorded but no position
    rm = _rm(_limits(topup_min_conviction_delta=0.05, min_add_interval_hours=0), state=state)
    d = rm.evaluate(_buy("LLY", conviction=0.50), _account(), price=100.0, volatility=0.3)
    # No position -> not a top-up -> gate doesn't apply
    assert d.verdict is not RiskVerdict.REJECTED, d.reason


def test_topup_evidence_off_at_zero():
    state = _fresh_state()
    state.register_buy("LLY", conviction=0.90)
    rm = _rm(_limits(topup_min_conviction_delta=0.0, min_add_interval_hours=0), state=state)
    acct = _account(positions=[_pos("LLY", qty=1.0, price=100.0)])
    d = rm.evaluate(_buy("LLY", conviction=0.50), acct, price=100.0, volatility=0.3)
    assert d.verdict is not RiskVerdict.REJECTED, d.reason


# --------------------------------------------------------------------------- #
# Missing-data multipliers (A4)
# --------------------------------------------------------------------------- #
def test_missing_sector_sizes_down():
    rm = _rm(_limits(max_sector_exposure_pct=30.0, missing_data_mult=0.5,
                     kelly_fraction=0.0, max_position_pct=100.0,
                     max_trade_risk_pct=0.0))
    # No sector data passed -> multiplier applied
    d_no_sector = rm.evaluate(_buy(weight=10.0), _account(), price=100.0,
                               sector=None, volatility=0.3)
    d_with_sector = rm.evaluate(_buy(weight=10.0), _account(), price=100.0,
                                 sector="Technology", sector_exposure_usd=0.0,
                                 volatility=0.3)
    assert d_no_sector.verdict in (RiskVerdict.APPROVED, RiskVerdict.RESIZED)
    assert d_with_sector.verdict in (RiskVerdict.APPROVED, RiskVerdict.RESIZED)
    assert d_no_sector.approved_notional < d_with_sector.approved_notional - 1


def test_missing_corr_data_sizes_down():
    rm = _rm(_limits(max_pairwise_corr=0.85, missing_data_mult=0.5,
                     kelly_fraction=0.0, max_position_pct=100.0,
                     max_sector_exposure_pct=0.0, max_trade_risk_pct=0.0))
    d_missing = rm.evaluate(_buy(weight=10.0), _account(), price=100.0,
                             max_held_corr=None, corr_data_missing=True, volatility=0.3)
    d_no_held = rm.evaluate(_buy(weight=10.0), _account(), price=100.0,
                             max_held_corr=None, corr_data_missing=False, volatility=0.3)
    assert d_missing.verdict in (RiskVerdict.APPROVED, RiskVerdict.RESIZED)
    assert d_no_held.verdict in (RiskVerdict.APPROVED, RiskVerdict.RESIZED)
    assert d_missing.approved_notional < d_no_held.approved_notional - 1


def test_missing_data_mult_one_restores_fail_open():
    rm = _rm(_limits(max_sector_exposure_pct=30.0, missing_data_mult=1.0,
                     kelly_fraction=0.0, max_position_pct=100.0, max_trade_risk_pct=0.0))
    d_no = rm.evaluate(_buy(weight=10.0), _account(), price=100.0, sector=None, volatility=0.3)
    d_yes = rm.evaluate(_buy(weight=10.0), _account(), price=100.0,
                         sector="Technology", sector_exposure_usd=0.0, volatility=0.3)
    assert abs(d_no.approved_notional - d_yes.approved_notional) < 5.0


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


def test_option_rejected_when_daily_loss_halted():
    # An option debit is a new position — the account-wide halt gate applies.
    rm = _rm(_limits(options_enabled=True))
    p = _opt(OptionStrategy.LONG_PUT,
             [OptionLeg(expiry=_opt_exp(30), strike=200, right="put", side=Action.BUY)])
    acct = _account(equity=96_000.0, last_equity=100_000.0)  # -4% day >= 3% cap
    d = rm.evaluate_option(p, acct, est_premium_per_contract=2.0)
    assert d.verdict is RiskVerdict.REJECTED
    assert "daily loss" in d.reason.lower()


def test_option_rejected_when_halt_latched():
    import tempfile as _tf
    import uuid as _uuid
    from investment_strategy.state import PortfolioState as _PS
    state = _PS(path=os.path.join(_tf.gettempdir(), f"_rm_opt_{_uuid.uuid4().hex}.json"))
    state.halted, state.halt_reason = True, "EQUITY FLOOR (test)"
    rm = _rm(_limits(options_enabled=True), state=state)
    p = _opt(OptionStrategy.LONG_CALL,
             [OptionLeg(expiry=_opt_exp(30), strike=200, right="call", side=Action.BUY)])
    d = rm.evaluate_option(p, _account(), est_premium_per_contract=2.0)
    assert d.verdict is RiskVerdict.REJECTED
    assert "halt latch" in d.reason.lower()


def test_option_not_blocked_by_full_equity_book():
    # 2026-07-13/14: all 8 option proposals died at "max open positions" — the
    # prompt steers Claude to defined-risk options exactly when equity buys are
    # capped, so the global slot cap made OPTIONS_ENABLED structurally dead.
    # Option concurrency is bounded by its OWN gates (max_option_positions
    # underlyings + the premium cap), not the equity slot cap.
    rm = _rm(_limits(options_enabled=True, max_open_positions=1))
    p = _opt(OptionStrategy.LONG_CALL,
             [OptionLeg(expiry=_opt_exp(30), strike=200, right="call", side=Action.BUY)])
    d = rm.evaluate_option(p, _account(positions=[_pos()]),
                           est_premium_per_contract=2.0)
    assert "max open positions" not in d.reason.lower()
    assert d.verdict is not RiskVerdict.REJECTED


def test_equity_slot_cap_counts_only_equity_rows():
    # An option row must not eat an equity slot: with the cap at 2, one equity
    # position + one OCC contract row still leaves room for a new equity name.
    rm = _rm(_limits(max_open_positions=2))
    opt_row = Position(
        symbol="AAPL260117C00200000", qty=1.0, avg_entry_price=2.0,
        current_price=2.0, market_value=200.0, unrealized_pl=0.0,
        unrealized_pl_pct=0.0, asset_class="us_option",
    )
    d = rm.evaluate(_buy("MSFT"), _account(positions=[_pos(), opt_row]),
                    price=100.0, volatility=0.2)
    assert "max open positions" not in d.reason.lower()


def test_option_dte_too_short_rejected():
    rm = _rm(_limits(options_enabled=True))
    p = _opt(OptionStrategy.LONG_PUT,
             [OptionLeg(expiry=_opt_exp(3), strike=200, right="put", side=Action.BUY)])
    d = rm.evaluate_option(p, _account(), est_premium_per_contract=2.0)
    assert d.verdict is RiskVerdict.REJECTED
    assert "minimum" in d.reason and "expires" in d.reason.lower()


def test_option_dte_too_long_rejected():
    rm = _rm(_limits(options_enabled=True))
    p = _opt(OptionStrategy.LONG_CALL,
             [OptionLeg(expiry=_opt_exp(90), strike=200, right="call", side=Action.BUY)])
    d = rm.evaluate_option(p, _account(), est_premium_per_contract=2.0)
    assert d.verdict is RiskVerdict.REJECTED
    assert "maximum" in d.reason


def test_option_mismatched_vertical_expiries_rejected():
    # A diagonal mislabeled as a vertical has a different risk shape — refuse.
    rm = _rm(_limits(options_enabled=True))
    p = _opt(OptionStrategy.BEAR_PUT_SPREAD, [
        OptionLeg(expiry=_opt_exp(30), strike=210, right="put", side=Action.BUY),
        OptionLeg(expiry=_opt_exp(45), strike=200, right="put", side=Action.SELL),
    ])
    d = rm.evaluate_option(p, _account(), est_premium_per_contract=2.0)
    assert d.verdict is RiskVerdict.REJECTED
    assert "one expiry" in d.reason


def _opt_position(underlying, days=30):
    from investment_strategy.execution.options import occ_symbol
    sym = occ_symbol(underlying, _opt_exp(days), 100.0, "call")
    return Position(symbol=sym, qty=1.0, avg_entry_price=2.0, current_price=2.0,
                    market_value=200.0, unrealized_pl=0.0, unrealized_pl_pct=0.0,
                    asset_class="us_option")


def test_option_concurrent_underlyings_capped():
    rm = _rm(_limits(options_enabled=True, max_option_positions=3,
                     max_open_positions=15))
    held = [_opt_position(u) for u in ("NVDA", "TSM", "AMD")]
    p = _opt(OptionStrategy.LONG_CALL,   # AAPL would be a 4th underlying
             [OptionLeg(expiry=_opt_exp(30), strike=200, right="call", side=Action.BUY)])
    d = rm.evaluate_option(p, _account(positions=held), est_premium_per_contract=2.0)
    assert d.verdict is RiskVerdict.REJECTED
    assert "max option positions" in d.reason.lower()


def test_option_same_underlying_not_a_new_slot():
    rm = _rm(_limits(options_enabled=True, max_option_positions=3,
                     max_open_positions=15))
    held = [_opt_position(u) for u in ("AAPL", "TSM", "AMD")]  # AAPL already held
    p = _opt(OptionStrategy.LONG_CALL,
             [OptionLeg(expiry=_opt_exp(30), strike=200, right="call", side=Action.BUY)])
    d = rm.evaluate_option(p, _account(positions=held), est_premium_per_contract=2.0)
    assert d.verdict is RiskVerdict.APPROVED


def test_option_low_open_interest_rejected():
    rm = _rm(_limits(options_enabled=True))
    p = _opt(OptionStrategy.LONG_CALL,
             [OptionLeg(expiry=_opt_exp(30), strike=200, right="call", side=Action.BUY)])
    d = rm.evaluate_option(p, _account(), est_premium_per_contract=2.0,
                           leg_liquidity=[{"symbol": "X", "oi": 20,
                                          "rel_spread_pct": 2.0}])
    assert d.verdict is RiskVerdict.REJECTED
    assert "open interest" in d.reason.lower()


def test_option_wide_spread_rejected():
    rm = _rm(_limits(options_enabled=True))
    p = _opt(OptionStrategy.LONG_CALL,
             [OptionLeg(expiry=_opt_exp(30), strike=200, right="call", side=Action.BUY)])
    d = rm.evaluate_option(p, _account(), est_premium_per_contract=2.0,
                           leg_liquidity=[{"symbol": "X", "oi": 500,
                                          "rel_spread_pct": 18.0}])
    assert d.verdict is RiskVerdict.REJECTED
    assert "spread" in d.reason.lower()


def test_option_missing_liquidity_data_fails_open():
    rm = _rm(_limits(options_enabled=True))
    p = _opt(OptionStrategy.LONG_CALL,
             [OptionLeg(expiry=_opt_exp(30), strike=200, right="call", side=Action.BUY)])
    d = rm.evaluate_option(p, _account(), est_premium_per_contract=2.0,
                           leg_liquidity=[{"symbol": "X", "oi": None,
                                          "rel_spread_pct": None}])
    assert d.verdict is RiskVerdict.APPROVED


# -- R-scaled trailing-stop geometry (Jul-25 calibration) -------------------- #
def test_trail_geometry_legacy_and_r_scaled():
    from investment_strategy.risk import trail_geometry
    # Legacy: both R knobs off -> arm == giveback == the fixed pct.
    lim = _limits(trail_giveback_pct=3.0)
    assert trail_geometry(lim, 6.0) == (3.0, 3.0)
    # Unknown stop -> legacy fixed geometry regardless of the R knobs.
    lim = _limits(trail_giveback_pct=5.0, trail_arm_r=1.5, trail_giveback_r=0.5)
    assert trail_geometry(lim, 0.0) == (5.0, 5.0)
    # R-scaled: giveback = max(5, 0.5x8) = 5; arm = max(5, 1.5x8) = 12.
    assert trail_geometry(lim, 8.0) == (12.0, 5.0)
    # Wide stop input CLAMPS to vol_stop_max_pct (10) before scaling:
    # giveback max(5, 5) = 5, arm max(5, 15) = 15 — never beyond the regime
    # the --sweep-trail evidence actually measured.
    assert trail_geometry(lim, 12.0) == (15.0, 5.0)
    assert trail_geometry(lim, 20.0) == (15.0, 5.0)   # unbounded LLM stop


def test_trail_geometry_arm_never_below_giveback():
    from investment_strategy.risk import trail_geometry
    # giveback_r dominating with arm_r OFF: the arm must ride UP with the
    # giveback (arm < giveback would let the trail close positions AT A LOSS).
    lim = _limits(trail_giveback_pct=3.0, trail_giveback_r=1.0)
    assert trail_geometry(lim, 8.0) == (8.0, 8.0)
    # A small arm_r must not pull the arm BELOW the giveback either.
    lim = _limits(trail_giveback_pct=3.0, trail_arm_r=0.3)
    assert trail_geometry(lim, 8.0) == (3.0, 3.0)


# --------------------------------------------------------------------------- #
# Option direction gate — calls WITH the long-run trend, puts against it
# --------------------------------------------------------------------------- #
def _long_put(symbol="AAPL"):
    return TradeProposal(
        symbol=symbol, action=Action.BUY, conviction=1.0, target_weight_pct=2.0,
        rationale="opt", instrument=Instrument.OPTION,
        option_strategy=OptionStrategy.LONG_PUT,
        option_legs=[OptionLeg(expiry=_opt_exp(30), strike=180, right="put",
                               side=Action.BUY)],
    )


def _long_call(symbol="AAPL"):
    return TradeProposal(
        symbol=symbol, action=Action.BUY, conviction=1.0, target_weight_pct=2.0,
        rationale="opt", instrument=Instrument.OPTION,
        option_strategy=OptionStrategy.LONG_CALL,
        option_legs=[OptionLeg(expiry=_opt_exp(30), strike=200, right="call",
                               side=Action.BUY)],
    )


def test_put_rejected_in_longrun_uptrend():
    rm = _rm(_limits(options_enabled=True))
    d = rm.evaluate_option(_long_put(), _account(), est_premium_per_contract=2.0,
                           market_trend="up", regime_label="risk-on")
    assert d.verdict is RiskVerdict.REJECTED
    assert "UP" in d.reason and "theta" in d.reason.lower()


def test_call_rejected_in_longrun_downtrend():
    rm = _rm(_limits(options_enabled=True))
    d = rm.evaluate_option(_long_call(), _account(), est_premium_per_contract=2.0,
                           market_trend="down", regime_label="neutral")
    assert d.verdict is RiskVerdict.REJECTED
    assert "DOWN" in d.reason


def test_call_approved_in_uptrend_put_approved_in_downtrend():
    rm = _rm(_limits(options_enabled=True))
    up = rm.evaluate_option(_long_call(), _account(), est_premium_per_contract=2.0,
                            market_trend="up", regime_label="risk-on")
    down = rm.evaluate_option(_long_put(), _account(), est_premium_per_contract=2.0,
                              market_trend="down", regime_label="neutral")
    assert up.verdict is RiskVerdict.APPROVED, up.reason
    assert down.verdict is RiskVerdict.APPROVED, down.reason


def test_put_allowed_in_uptrend_when_regime_riskoff():
    # Vol-spiked uptrend: the risk-off put mandate ASKS for puts — the gate
    # must never fight its own mandate.
    rm = _rm(_limits(options_enabled=True))
    d = rm.evaluate_option(_long_put(), _account(), est_premium_per_contract=2.0,
                           market_trend="up", regime_label="risk-off")
    assert d.verdict is RiskVerdict.APPROVED, d.reason


def test_put_allowed_in_uptrend_as_hedge_on_held_name():
    rm = _rm(_limits(options_enabled=True))
    acct = _account(positions=[_pos("AAPL", qty=100.0)])
    d = rm.evaluate_option(_long_put("AAPL"), acct, est_premium_per_contract=2.0,
                           market_trend="up", regime_label="risk-on")
    assert d.verdict is RiskVerdict.APPROVED, d.reason


def test_put_on_unheld_name_still_rejected_when_other_names_held():
    rm = _rm(_limits(options_enabled=True))
    acct = _account(positions=[_pos("MSFT", qty=100.0)])
    d = rm.evaluate_option(_long_put("AAPL"), acct, est_premium_per_contract=2.0,
                           market_trend="up", regime_label="risk-on")
    assert d.verdict is RiskVerdict.REJECTED


def test_unknown_trend_passes_direction_gate():
    # Act only on data we have — a degraded regime read must not block options
    # outright (the degraded multiplier already shrinks the premium budget).
    rm = _rm(_limits(options_enabled=True))
    d = rm.evaluate_option(_long_put(), _account(), est_premium_per_contract=2.0,
                           market_trend="", regime_label="unknown",
                           regime_multiplier=0.5)
    assert d.verdict is RiskVerdict.APPROVED, d.reason


def test_direction_gate_off_restores_old_behavior():
    rm = _rm(_limits(options_enabled=True, option_direction_gate=False))
    d = rm.evaluate_option(_long_put(), _account(), est_premium_per_contract=2.0,
                           market_trend="up", regime_label="risk-on")
    assert d.verdict is RiskVerdict.APPROVED, d.reason


def test_mixed_call_put_legs_rejected():
    # A mixed-rights structure is always refused: the defined-risk check
    # catches the 1-long+1-short case first (the short is an uncovered short
    # of the OTHER right), and the direction gate's own mixed-rights branch
    # backstops any shape that slips past it (gate order is not a contract).
    rm = _rm(_limits(options_enabled=True))
    p = TradeProposal(
        symbol="AAPL", action=Action.BUY, conviction=1.0, target_weight_pct=2.0,
        rationale="opt", instrument=Instrument.OPTION,
        option_strategy=OptionStrategy.BEAR_PUT_SPREAD,
        option_legs=[
            OptionLeg(expiry=_opt_exp(30), strike=180, right="put", side=Action.BUY),
            OptionLeg(expiry=_opt_exp(30), strike=210, right="call", side=Action.SELL),
        ],
    )
    d = rm.evaluate_option(p, _account(), est_premium_per_contract=2.0,
                           market_trend="up", regime_label="risk-on")
    assert d.verdict is RiskVerdict.REJECTED
    ok, why = rm._direction_fits_market(p, _account(), "up", "risk-on")
    assert not ok and "mixed" in why.lower()


def test_direction_derived_from_legs_not_declared_strategy():
    # A "bear_put_spread" built from CALL legs is really a bullish call spread
    # — the gate must judge the LEGS (what actually trades), so in a DOWN
    # trend this is rejected as a bullish structure despite its bearish name.
    rm = _rm(_limits(options_enabled=True))
    p = TradeProposal(
        symbol="AAPL", action=Action.BUY, conviction=1.0, target_weight_pct=2.0,
        rationale="opt", instrument=Instrument.OPTION,
        option_strategy=OptionStrategy.BEAR_PUT_SPREAD,
        option_legs=[
            OptionLeg(expiry=_opt_exp(30), strike=200, right="call", side=Action.BUY),
            OptionLeg(expiry=_opt_exp(30), strike=210, right="call", side=Action.SELL),
        ],
    )
    d = rm.evaluate_option(p, _account(), est_premium_per_contract=2.0,
                           market_trend="down", regime_label="neutral")
    assert d.verdict is RiskVerdict.REJECTED
    assert "DOWN" in d.reason


# -- regime-scaled option premium budget -------------------------------------- #
def test_option_premium_cap_scales_with_regime_multiplier():
    rm = _rm(_limits(options_enabled=True, max_option_premium_pct=1.0))
    p = _long_call()
    # 1% of 100k = $1000; x0.4 regime -> $400 budget; $2/share = $200/contract
    # -> 2 contracts instead of 5.
    d = rm.evaluate_option(p, _account(), est_premium_per_contract=2.0,
                           market_trend="up", regime_label="risk-off",
                           regime_multiplier=0.4)
    assert d.verdict is RiskVerdict.APPROVED, d.reason
    assert d.approved_qty == 2
    assert d.approved_notional == 400.0


def test_option_premium_cap_unscaled_when_regime_filter_off():
    rm = _rm(_limits(options_enabled=True, max_option_premium_pct=1.0,
                     regime_filter_enabled=False))
    d = rm.evaluate_option(_long_call(), _account(), est_premium_per_contract=2.0,
                           regime_multiplier=0.4)
    assert d.verdict is RiskVerdict.APPROVED
    assert d.approved_qty == 5


def test_put_allowed_in_uptrend_when_name_in_own_breakdown():
    # The insider-sell / bearish-slate pipeline shorts single-name breakdowns
    # in any tape: a name below its OWN 200dma keeps its put candidacy even
    # while the MARKET trend is up.
    rm = _rm(_limits(options_enabled=True))
    d = rm.evaluate_option(_long_put(), _account(), est_premium_per_contract=2.0,
                           market_trend="up", regime_label="risk-on",
                           name_trend="down")
    assert d.verdict is RiskVerdict.APPROVED, d.reason


def test_put_rejected_in_uptrend_when_name_also_trending_up():
    rm = _rm(_limits(options_enabled=True))
    d = rm.evaluate_option(_long_put(), _account(), est_premium_per_contract=2.0,
                           market_trend="up", regime_label="risk-on",
                           name_trend="up")
    assert d.verdict is RiskVerdict.REJECTED


def test_call_in_downtrend_rejected_even_if_name_trending_up():
    # The single-name carve-out is puts-only: calls in a down market are
    # blocked with no exceptions (the user's mandate).
    rm = _rm(_limits(options_enabled=True))
    d = rm.evaluate_option(_long_call(), _account(), est_premium_per_contract=2.0,
                           market_trend="down", regime_label="neutral",
                           name_trend="up")
    assert d.verdict is RiskVerdict.REJECTED
