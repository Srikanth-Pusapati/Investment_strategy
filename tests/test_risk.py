"""Tests for the deterministic risk gate — the one module that must never regress.

Runnable two ways:
    .venv/bin/python tests/test_risk.py     # standalone, no pytest needed
    .venv/bin/pytest tests/                  # if pytest is installed
"""
from __future__ import annotations

import os
import sys

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


def _limits(**over) -> RiskLimits:
    base = dict(
        max_position_pct=5.0,
        max_symbol_exposure_pct=10.0,
        max_daily_loss_pct=3.0,
        max_drawdown_pct=15.0,
        equity_floor_usd=0.0,
        max_open_positions=15,
        min_cash_buffer_pct=10.0,
        min_trade_price_usd=5.0,
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
             last_equity=100_000.0, positions=None) -> AccountSnapshot:
    return AccountSnapshot(
        equity=equity, last_equity=last_equity, cash=cash,
        buying_power=buying_power, positions=positions or [],
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
    rm = RiskManager(_limits(kelly_fraction=0.0))  # disable kelly -> max cap path
    d = rm.evaluate(_buy(weight=100.0), _account(), price=100.0, volatility=0.3)
    assert d.verdict in (RiskVerdict.APPROVED, RiskVerdict.RESIZED), d.reason
    # max_position_pct=5% of 100k = $5k -> 50 shares at $100
    assert d.approved_qty == 50, d.approved_qty
    assert abs(d.approved_notional - 5000) < 1e-6


def test_missing_vol_sizes_down_not_up():
    rm = RiskManager(_limits())
    with_vol = rm.evaluate(_buy(), _account(), price=100.0, volatility=0.25)
    no_vol = rm.evaluate(_buy(), _account(), price=100.0, volatility=None)
    # Unknown vol assumes a HIGH vol (0.60) so the size must not exceed the known
    # low-vol case — fail safe is smaller-or-equal, never larger.
    assert no_vol.approved_notional <= with_vol.approved_notional


def test_min_trade_price_guard():
    rm = RiskManager(_limits(min_trade_price_usd=5.0))
    d = rm.evaluate(_buy(), _account(), price=2.0, volatility=0.3)
    assert d.verdict is RiskVerdict.REJECTED
    assert "liquidity" in d.reason.lower() or "below min" in d.reason.lower()


def test_cash_buffer_respected():
    # Almost no deployable cash: 10% buffer of 100k = 10k reserved, only 10.5k cash.
    rm = RiskManager(_limits())
    acct = _account(cash=10_500.0, buying_power=10_500.0)
    d = rm.evaluate(_buy(), acct, price=100.0, volatility=0.25)
    # Deployable ~= 500 -> at most 5 shares ($500), well under the position cap.
    assert d.approved_notional <= 500 + 1e-6


def test_symbol_exposure_cap_counts_pending():
    rm = RiskManager(_limits(max_symbol_exposure_pct=10.0))
    # Already holding $9k of a 10k cap, plus $1k pending -> no room left.
    acct = _account(positions=[_pos(qty=90, price=100.0)])  # $9k held
    d = rm.evaluate(_buy(), acct, price=100.0, volatility=0.25, pending_buy_notional=1000.0)
    assert d.verdict is RiskVerdict.REJECTED
    assert "exposure cap" in d.reason.lower()


# --------------------------------------------------------------------------- #
# Account-wide halts
# --------------------------------------------------------------------------- #
def test_daily_loss_halts_new_buys():
    rm = RiskManager(_limits(max_daily_loss_pct=3.0))
    acct = _account(equity=96_000.0, last_equity=100_000.0)  # -4% day
    d = rm.evaluate(_buy(), acct, price=100.0, volatility=0.25)
    assert d.verdict is RiskVerdict.REJECTED
    assert "loss" in d.reason.lower()


def test_drawdown_halts_new_buys():
    state = PortfolioState(path="/tmp/_does_not_persist_dd.json")
    state.peak_equity = 100_000.0
    rm = RiskManager(_limits(max_drawdown_pct=15.0), state=state)
    acct = _account(equity=80_000.0, last_equity=80_000.0)  # 20% off peak
    d = rm.evaluate(_buy(), acct, price=100.0, volatility=0.25)
    assert d.verdict is RiskVerdict.REJECTED
    assert "drawdown" in d.reason.lower()


def test_kill_switch_blocks_buys_but_not_sells():
    rm = RiskManager(_limits(), kill_switch=True)
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
    rm = RiskManager(_limits(max_open_positions=2))
    acct = _account(positions=[_pos("AAPL"), _pos("MSFT")])
    d = rm.evaluate(_buy("NVDA"), acct, price=100.0, volatility=0.25)
    assert d.verdict is RiskVerdict.REJECTED
    assert "max open positions" in d.reason.lower()


# --------------------------------------------------------------------------- #
# Sells
# --------------------------------------------------------------------------- #
def test_sell_without_position_rejected():
    rm = RiskManager(_limits())
    d = rm.evaluate(
        TradeProposal(symbol="AAPL", action=Action.SELL, conviction=1.0,
                      target_weight_pct=0.0, rationale="exit"),
        _account(), price=100.0,
    )
    assert d.verdict is RiskVerdict.REJECTED


def test_sell_approves_full_held_qty():
    rm = RiskManager(_limits())
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
    rm = RiskManager(_limits(options_enabled=False))
    p = _opt(OptionStrategy.LONG_CALL,
             [OptionLeg(expiry="2026-07-17", strike=200, right="call", side=Action.BUY)])
    d = rm.evaluate_option(p, _account(), est_premium_per_contract=2.0)
    assert d.verdict is RiskVerdict.REJECTED


def test_naked_short_call_rejected():
    rm = RiskManager(_limits(options_enabled=True))
    p = _opt(OptionStrategy.BULL_CALL_SPREAD,
             [OptionLeg(expiry="2026-07-17", strike=210, right="call", side=Action.SELL)])
    d = rm.evaluate_option(p, _account(), est_premium_per_contract=2.0)
    assert d.verdict is RiskVerdict.REJECTED
    assert "short call" in d.reason.lower() or "must be one long" in d.reason.lower()


def test_net_credit_rejected():
    rm = RiskManager(_limits(options_enabled=True))
    p = _opt(OptionStrategy.LONG_CALL,
             [OptionLeg(expiry="2026-07-17", strike=200, right="call", side=Action.BUY)])
    d = rm.evaluate_option(p, _account(), est_premium_per_contract=-1.0)  # credit
    assert d.verdict is RiskVerdict.REJECTED


def test_defined_risk_spread_approved_and_premium_capped():
    rm = RiskManager(_limits(options_enabled=True, max_option_premium_pct=1.0))
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
