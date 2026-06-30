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
        max_daily_loss_pct=3.0,
        max_drawdown_pct=15.0,
        equity_floor_pct=0.0,
        max_open_positions=15,
        min_cash_buffer_pct=10.0,
        min_trade_price_usd=5.0,
        earnings_blackout_days=3,
        pdt_guard_enabled=True,
        max_day_trades_under_25k=3,
        max_trade_risk_pct=1.0,
        est_slippage_pct=0.10,
        min_edge_ratio=2.0,
        fractional_enabled=True,
        min_order_usd=1.0,
        default_stop_loss_pct=5.0,
        default_take_profit_pct=12.0,
        kelly_fraction=0.5,
        target_annual_vol_pct=25.0,
        options_enabled=False,
        max_option_premium_pct=1.0,
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


def test_options_disabled_rejected():
    rm = _rm(_limits(options_enabled=False))
    p = _opt(OptionStrategy.LONG_CALL,
             [OptionLeg(expiry="2026-07-17", strike=200, right="call", side=Action.BUY)])
    d = rm.evaluate_option(p, _account(), est_premium_per_contract=2.0)
    assert d.verdict is RiskVerdict.REJECTED


def test_naked_short_call_rejected():
    rm = _rm(_limits(options_enabled=True))
    p = _opt(OptionStrategy.BULL_CALL_SPREAD,
             [OptionLeg(expiry="2026-07-17", strike=210, right="call", side=Action.SELL)])
    d = rm.evaluate_option(p, _account(), est_premium_per_contract=2.0)
    assert d.verdict is RiskVerdict.REJECTED
    assert "short call" in d.reason.lower() or "must be one long" in d.reason.lower()


def test_net_credit_rejected():
    rm = _rm(_limits(options_enabled=True))
    p = _opt(OptionStrategy.LONG_CALL,
             [OptionLeg(expiry="2026-07-17", strike=200, right="call", side=Action.BUY)])
    d = rm.evaluate_option(p, _account(), est_premium_per_contract=-1.0)  # credit
    assert d.verdict is RiskVerdict.REJECTED


def test_defined_risk_spread_approved_and_premium_capped():
    rm = _rm(_limits(options_enabled=True, max_option_premium_pct=1.0))
    legs = [
        OptionLeg(expiry="2026-07-17", strike=200, right="call", side=Action.BUY),
        OptionLeg(expiry="2026-07-17", strike=210, right="call", side=Action.SELL),
    ]
    p = _opt(OptionStrategy.BULL_CALL_SPREAD, legs)
    # 1% of 100k = $1000 budget; $2/share -> $200/contract -> 5 contracts.
    d = rm.evaluate_option(p, _account(), est_premium_per_contract=2.0)
    assert d.verdict is RiskVerdict.APPROVED
    assert d.approved_qty == 5
    assert d.approved_notional <= 1000 + 1e-6


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
