"""Put-liquidity proxy fallback + entry-side leg-merge guard (Aug 14 ship).

The Aug 3-14 window proved the model DOES propose puts (EXTR, TDC) and the
liquidity floor correctly kills them — micro-cap chains with OI 4-20 can't be
exited. The fallback re-expresses that same bearish read as a deterministic
near-ATM bear put spread on a liquid proxy ETF; the merge guard stops a new
structure from pushing an underlying+expiry group past Alpaca's 4-leg MLEG
cap (the unclosable 5-leg AMZN group of 2026-08-07)."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.execution.options import OptionsHelper
from investment_strategy.models import (
    AccountSnapshot,
    Action,
    Instrument,
    OptionLeg,
    OptionStrategy,
    Position,
    RiskDecision,
    RiskVerdict,
    TradeProposal,
)
from investment_strategy.orchestrator import Orchestrator
from investment_strategy.risk import RiskManager


def _exp(days: int = 35) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%d")


def _opt_pos(symbol: str, qty: float = 1.0) -> Position:
    return Position(
        symbol=symbol, qty=qty, avg_entry_price=3.0, current_price=2.0,
        market_value=qty * 200.0, unrealized_pl=0.0, unrealized_pl_pct=0.0,
        asset_class="us_option",
    )


def _account(positions=None) -> AccountSnapshot:
    return AccountSnapshot(
        equity=100_000.0, last_equity=100_000.0, cash=100_000.0,
        buying_power=100_000.0, positions=positions or [],
    )


def _put_proposal(symbol="EXTR", legs=None, expiry_days=35) -> TradeProposal:
    e = _exp(expiry_days)
    return TradeProposal(
        symbol=symbol, action=Action.BUY, conviction=0.7, target_weight_pct=1.0,
        rationale="breakdown", instrument=Instrument.OPTION,
        option_strategy=OptionStrategy.BEAR_PUT_SPREAD,
        option_legs=legs or [
            OptionLeg(expiry=e, strike=20, right="put", side=Action.BUY),
            OptionLeg(expiry=e, strike=18, right="put", side=Action.SELL),
        ],
    )


# --------------------------------------------------------------------------- #
# Entry-side leg-merge guard (risk layer)
# --------------------------------------------------------------------------- #
def _rm() -> RiskManager:
    from tests.test_risk import _limits, _rm as _mk
    # per_underlying_premium_pct=0: the AMZN fixtures here hold ~$900 of open
    # premium on one underlying, which the (default-on) concentration cap of
    # test_run5_gates.py would reject before the MERGE guard under test ever
    # answers.
    return _mk(_limits(options_enabled=True, per_underlying_premium_pct=0.0))


def _amzn_sym(strike: int, e: str) -> str:
    from investment_strategy.execution.options import occ_symbol
    return occ_symbol("AMZN", e, float(strike), "call")


def test_merge_guard_rejects_group_past_four_legs():
    e = _exp(35)
    held = [_opt_pos(_amzn_sym(k, e)) for k in (230, 240, 245)]
    p = TradeProposal(
        symbol="AMZN", action=Action.BUY, conviction=0.7, target_weight_pct=1.0,
        rationale="spread", instrument=Instrument.OPTION,
        option_strategy=OptionStrategy.BULL_CALL_SPREAD,
        option_legs=[
            OptionLeg(expiry=e, strike=280, right="call", side=Action.BUY),
            OptionLeg(expiry=e, strike=290, right="call", side=Action.SELL),
        ],
    )
    d = _rm().evaluate_option(p, _account(held), est_premium_per_contract=2.0)
    assert d.verdict is RiskVerdict.REJECTED
    assert "merge" in d.reason.lower() and "4" in d.reason


def test_merge_guard_allows_separate_expiry_group():
    e_held, e_new = _exp(35), _exp(49)
    held = [_opt_pos(_amzn_sym(k, e_held)) for k in (230, 240, 245)]
    p = TradeProposal(
        symbol="AMZN", action=Action.BUY, conviction=0.7, target_weight_pct=1.0,
        rationale="spread", instrument=Instrument.OPTION,
        option_strategy=OptionStrategy.BULL_CALL_SPREAD,
        option_legs=[
            OptionLeg(expiry=e_new, strike=280, right="call", side=Action.BUY),
            OptionLeg(expiry=e_new, strike=290, right="call", side=Action.SELL),
        ],
    )
    d = _rm().evaluate_option(p, _account(held), est_premium_per_contract=2.0)
    assert d.verdict is not RiskVerdict.REJECTED


def test_merge_guard_topup_of_held_contracts_passes():
    e = _exp(35)
    held = [
        _opt_pos(_amzn_sym(280, e)), _opt_pos(_amzn_sym(290, e), qty=-1.0),
        _opt_pos(_amzn_sym(230, e)), _opt_pos(_amzn_sym(245, e), qty=-1.0),
    ]
    p = TradeProposal(  # same two contracts again -> merges into held rows
        symbol="AMZN", action=Action.BUY, conviction=0.7, target_weight_pct=1.0,
        rationale="top-up", instrument=Instrument.OPTION,
        option_strategy=OptionStrategy.BULL_CALL_SPREAD,
        option_legs=[
            OptionLeg(expiry=e, strike=280, right="call", side=Action.BUY),
            OptionLeg(expiry=e, strike=290, right="call", side=Action.SELL),
        ],
    )
    ok, why = _rm()._legs_merge_safe(p, _account(held))
    assert ok, why


# --------------------------------------------------------------------------- #
# Deterministic proxy-spread builder (OptionsHelper)
# --------------------------------------------------------------------------- #
class _FakeChainTrading:
    def __init__(self, contracts):
        self._contracts = contracts

    def get_option_contracts(self, req):
        return SimpleNamespace(option_contracts=self._contracts)


def _chain(expiry: str, strikes) -> list:
    return [
        SimpleNamespace(expiration_date=expiry, strike_price=str(k),
                        symbol=f"IWM{k}")
        for k in strikes
    ]


def _helper(contracts) -> OptionsHelper:
    h = OptionsHelper.__new__(OptionsHelper)
    h._trading = _FakeChainTrading(contracts)
    return h


def test_builder_picks_atm_long_and_width_short():
    near, far = _exp(35), _exp(49)  # target = today + (25+50)//2 = 37d -> near
    strikes = [200, 205, 210, 215, 220, 225]
    legs = _helper(_chain(near, strikes) + _chain(far, strikes)) \
        .build_proxy_put_spread("IWM", spot=221.3, min_dte=25, max_dte=50)
    assert legs is not None and len(legs) == 2
    lng, sht = legs
    assert lng.side is Action.BUY and sht.side is Action.SELL
    assert lng.expiry == near and sht.expiry == near
    assert lng.strike == 220            # highest strike <= spot
    assert sht.strike == 205            # highest strike <= 220 x 0.95 = 209
    assert lng.right == "put" and sht.right == "put"


def test_builder_none_when_no_short_strike_below_width():
    legs = _helper(_chain(_exp(35), [220])) \
        .build_proxy_put_spread("IWM", spot=221.0, min_dte=25, max_dte=50)
    assert legs is None


def test_builder_none_on_empty_chain_or_bad_spot():
    assert _helper([]).build_proxy_put_spread("IWM", 221.0, 25, 50) is None
    assert _helper(_chain(_exp(35), [200, 210])) \
        .build_proxy_put_spread("IWM", 0.0, 25, 50) is None


# --------------------------------------------------------------------------- #
# Orchestrator trigger + once-per-cycle + skip conditions
# --------------------------------------------------------------------------- #
def _proxy_orch(etf="IWM", positions=None, chain_legs="default"):
    o = Orchestrator.__new__(Orchestrator)
    o.cfg = SimpleNamespace(put_proxy_etf=etf)
    o.risk = SimpleNamespace(
        limits=SimpleNamespace(min_option_dte=7.0, max_option_dte=60.0),
    )
    o.broker = SimpleNamespace(latest_price=lambda s: 221.3)
    if chain_legs == "default":
        e = _exp(35)
        chain_legs = [
            OptionLeg(expiry=e, strike=220, right="put", side=Action.BUY),
            OptionLeg(expiry=e, strike=209, right="put", side=Action.SELL),
        ]
    o.options = SimpleNamespace(
        build_proxy_put_spread=lambda *a, **k: chain_legs,
    )
    o._proxy_put_state = ""
    o._handled = []
    o._handle_option = lambda p, acct, sk, tech=None, proxy_for="": (
        o._handled.append((p, proxy_for))
    )
    return o


def test_proxy_reproposes_on_liquid_etf():
    o = _proxy_orch()
    blocked = _put_proposal("EXTR")
    o._propose_proxy_put(blocked, _account(), ["insider_sell"])
    assert len(o._handled) == 1
    proxy, tag = o._handled[0]
    assert tag == "EXTR"
    assert proxy.symbol == "IWM"
    assert proxy.option_strategy is OptionStrategy.BEAR_PUT_SPREAD
    assert all(l.right == "put" for l in proxy.option_legs)
    assert "EXTR" in proxy.rationale and "PROXY" in proxy.rationale


def test_proxy_once_per_cycle_and_disabled_by_blank_etf():
    o = _proxy_orch()
    o._proxy_put_state = "IWM for TDC -> approved"
    o._propose_proxy_put(_put_proposal("EXTR"), _account(), [])
    assert o._handled == []
    o2 = _proxy_orch(etf="")
    o2._propose_proxy_put(_put_proposal("EXTR"), _account(), [])
    assert o2._handled == []


def test_proxy_skips_when_etf_structure_already_open():
    from investment_strategy.execution.options import occ_symbol
    held = [_opt_pos(occ_symbol("IWM", _exp(30), 215.0, "put"))]
    o = _proxy_orch(positions=held)
    o._propose_proxy_put(_put_proposal("EXTR"), _account(held), [])
    assert o._handled == []
    assert "already open" in o._proxy_put_state


def test_proxy_skips_without_workable_chain():
    o = _proxy_orch(chain_legs=None)
    o._propose_proxy_put(_put_proposal("EXTR"), _account(), [])
    assert o._handled == []
    assert "no workable" in o._proxy_put_state


# --------------------------------------------------------------------------- #
# _handle_option hook: liquidity rejects re-route, real vetoes stay final
# --------------------------------------------------------------------------- #
def _hook_orch(reason: str):
    o = Orchestrator.__new__(Orchestrator)
    o.options = SimpleNamespace(
        estimate_net_premium=lambda p: 1.0,
        min_leg_premium=lambda p: 0.5,
        leg_liquidity=lambda p: [],
    )
    o._regime_trend, o._regime_label, o._regime_mult = "up", "risk-on", 1.0
    o._hedge_symbol = ""
    o._journal_decision = lambda *a, **k: None

    def _reject(proposal, account, *a, **k):
        return RiskDecision(
            proposal=proposal, verdict=RiskVerdict.REJECTED, reason=reason,
        )
    o.risk = SimpleNamespace(evaluate_option=_reject)
    o._proxied = []
    o._propose_proxy_put = lambda blocked, acct, sk: o._proxied.append(blocked.symbol)
    return o


def test_hook_fires_only_on_liquidity_reason():
    o = _hook_orch("Leg X open interest 4 < 100 floor — too illiquid to exit cleanly.")
    o._handle_option(_put_proposal("EXTR"), _account())
    assert o._proxied == ["EXTR"]

    o2 = _hook_orch("Long-run market trend is UP — puts blocked.")
    o2._handle_option(_put_proposal("EXTR"), _account())
    assert o2._proxied == []


def test_hook_never_reproxies_a_proxy():
    o = _hook_orch("Leg X open interest 4 < 100 floor.")
    o._handle_option(_put_proposal("IWM"), _account(), proxy_for="EXTR")
    assert o._proxied == []
    assert o._proxy_put_state == "IWM for EXTR -> rejected"
