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


# --------------------------------------------------------------------------- #
# Run-7 S-3: OI-aware, monthly-first builder (execution/options.py)
#
# Run-6 evidence (logs/Sep_01..04_2026.log): the legacy pick — expiry
# nearest the DTE midpoint, long = highest strike <= spot, OI never read —
# handed the gate the Oct-9 WEEKLY three times: Sep 1 IWM 291P OI 38 and
# Sep 3 294P OI 47 were rejected on the OI floor; only Sep 4's 295P (OI
# 1,011) passed. The Oct-16 MONTHLY sat inside [25, 50] DTE each time with
# >= 5,000 OI on every candidate strike. All fixtures below pin `today`
# explicitly so the DTE arithmetic never depends on the wall clock.
# --------------------------------------------------------------------------- #
import logging
from datetime import date

from investment_strategy.execution.options import _is_third_friday

_S3_TODAY = date(2026, 9, 1)     # window [25, 50] -> Sep 26 .. Oct 21; mid = Oct 8
_WEEKLY = "2026-10-09"           # 1 day from mid -> the legacy pick
_MONTHLY = "2026-10-16"          # third Friday, 8 days from mid, 45 DTE
_SPOT = 291.81                   # IWM at the Sep 1 11:10 proxy attempt


def _oi_chain(expiry: str, oi_by_strike: dict) -> list:
    """Contract rows the way the trading API returns them: `open_interest`
    is a STRING (or None when the venue has none)."""
    return [
        SimpleNamespace(
            expiration_date=expiry, strike_price=str(k),
            open_interest=None if oi is None else str(oi),
            symbol=f"IWM{expiry}{k}",
        )
        for k, oi in oi_by_strike.items()
    ]


def _build(contracts, **kw):
    kw.setdefault("min_oi", 100.0)
    kw.setdefault("today", _S3_TODAY)
    return _helper(contracts).build_proxy_put_spread(
        "IWM", spot=_SPOT, min_dte=25, max_dte=50, width_pct=5.0, **kw,
    )


def test_is_third_friday():
    # Critic #11: the helper the ranking leans on, pinned on its own.
    assert _is_third_friday("2026-10-16") is True      # Oct monthly
    assert _is_third_friday("2026-09-18") is True      # Sep monthly
    assert _is_third_friday(date(2026, 11, 20)) is True  # date objects too
    assert _is_third_friday("2026-10-09") is False     # 2nd Friday (weekly)
    assert _is_third_friday("2026-10-23") is False     # 4th Friday (weekly)
    assert _is_third_friday("2026-10-17") is False     # Saturday, day 17
    assert _is_third_friday("2026-10-15") is False     # Thursday, day 15
    assert _is_third_friday("") is False               # malformed -> not a 3rd Friday
    assert _is_third_friday(None) is False


def test_proxy_put_prefers_oi_qualified_strike():
    # Sep 1 replay on ONE expiry: the legacy long (291, OI 38) is skipped
    # DOWNWARD to 290 (OI 1,800). 289 (OI 743) also qualifies but 290 is
    # the HIGHEST qualifying strike in [spot x 0.98, spot].
    chain = _oi_chain(_WEEKLY, {
        291: 38, 290: 1800, 289: 743, 280: 814, 276: 900, 275: 2000,
    })
    legs = _build(chain)
    assert legs is not None
    lng, sht = legs
    assert (lng.expiry, lng.strike) == (_WEEKLY, 290)
    # short = highest OI-qualified strike <= 290 x 0.95 = 275.5 -> 275
    assert (sht.expiry, sht.strike) == (_WEEKLY, 275)
    assert lng.side is Action.BUY and sht.side is Action.SELL
    assert lng.right == "put" and sht.right == "put"


def test_proxy_put_prefers_monthly_when_both_qualify(caplog):
    weekly = _oi_chain(_WEEKLY, {291: 1200, 290: 1827, 276: 900, 275: 1500})
    monthly = _oi_chain(_MONTHLY, {291: 9781, 290: 32253, 276: 14135, 275: 35617})
    with caplog.at_level(logging.INFO, logger="options"):
        legs = _build(weekly + monthly)
    assert legs is not None
    assert legs[0].expiry == _MONTHLY and legs[1].expiry == _MONTHLY
    assert (legs[0].strike, legs[1].strike) == (291, 276)
    # The greppable pick line carries the legacy counterfactual (the weekly
    # is 1 day from the DTE midpoint, so it IS the legacy pick).
    assert (
        "PROXY PUT PICK: IWM 2026-10-16 291/276 (OI 9781/14135, monthly, 45d) "
        "over legacy 2026-10-09 291/276 (OI 1200/900)"
    ) in caplog.text
    # Knob off: nearest-mid-DTE expiry ranking, strikes still OI-qualified.
    legs = _build(weekly + monthly, prefer_monthly=False)
    assert legs[0].expiry == _WEEKLY and legs[1].expiry == _WEEKLY
    assert (legs[0].strike, legs[1].strike) == (291, 276)


def test_proxy_put_monthly_preference_yields_to_oi():
    # A monthly whose strikes do NOT clear the floor is skipped for the
    # qualifying weekly — the preference is an ordering, not a mandate.
    weekly = _oi_chain(_WEEKLY, {291: 1200, 276: 900})
    thin_monthly = _oi_chain(_MONTHLY, {291: 60, 290: 80, 276: 20, 275: 50})
    legs = _build(weekly + thin_monthly)
    assert legs is not None and legs[0].expiry == _WEEKLY
    assert (legs[0].strike, legs[1].strike) == (291, 276)


def test_proxy_put_excludes_none_oi_when_chain_has_oi():
    # The gate fails OPEN on a None OI; the builder must not lean on that
    # when the chain carries real OI elsewhere: 291 and 276 read None and
    # are skipped for 290 / 275.
    chain = _oi_chain(_MONTHLY, {291: None, 290: 1800, 276: None, 275: 5000})
    legs = _build(chain)
    assert legs is not None
    assert (legs[0].strike, legs[1].strike) == (290, 275)


def test_proxy_put_legacy_pick_when_no_oi_data(caplog):
    # Nothing to rank by (every OI None): the pre-S-3 pick verbatim —
    # nearest-mid expiry (the weekly, NOT the monthly), highest strike
    # <= spot, highest strike <= long x 0.95 — and the log says so.
    weekly = _oi_chain(_WEEKLY, {291: None, 290: None, 276: None, 275: None})
    monthly = _oi_chain(_MONTHLY, {291: None, 290: None, 276: None, 275: None})
    with caplog.at_level(logging.INFO, logger="options"):
        legs = _build(weekly + monthly)
    assert legs is not None
    assert legs[0].expiry == _WEEKLY and legs[1].expiry == _WEEKLY
    assert (legs[0].strike, legs[1].strike) == (291, 276)
    assert "PROXY PUT PICK: IWM 2026-10-09 291/276 (legacy pick: no OI data in chain)" in caplog.text
    # OI floor off (min_oi None / 0) -> legacy too, even with OI present.
    live = _oi_chain(_WEEKLY, {291: 38, 290: 1800, 276: 900, 275: 2000})
    for off in (None, 0.0):
        legs = _build(live, min_oi=off)
        assert (legs[0].strike, legs[1].strike) == (291, 276)


def test_proxy_put_both_legs_clear_min_oi():
    # The SHORT leg is OI-qualified too: 276 (OI 40) and 275 (OI 60) are
    # skipped downward to 274 (OI 3,000); the long stays 291.
    oi = {291: 5000, 290: 1800, 276: 40, 275: 60, 274: 3000}
    legs = _build(_oi_chain(_MONTHLY, oi))
    assert legs is not None
    lng, sht = legs
    assert (lng.strike, sht.strike) == (291, 274)
    assert oi[int(lng.strike)] >= 100 and oi[int(sht.strike)] >= 100
    # A higher floor moves BOTH legs: 291 (5,000) still clears 4,000 but
    # 274 (3,000) no longer does -> no short -> no pair on this expiry.
    assert _build(_oi_chain(_MONTHLY, oi), min_oi=4000.0) is None


def test_proxy_put_returns_none_when_no_pair_qualifies(caplog):
    # Weekly: no qualifying long (291 OI 38; 280 at 4% below spot is
    # outside the 2% long band even with OI 5,000). Monthly: qualifying
    # long, no qualifying short. -> None, with the legacy counterfactual.
    weekly = _oi_chain(_WEEKLY, {291: 38, 280: 5000, 276: 900, 265: 1500})
    monthly = _oi_chain(_MONTHLY, {291: 5000, 276: 40, 275: 20})
    with caplog.at_level(logging.INFO, logger="options"):
        assert _build(weekly + monthly) is None
    assert "PROXY PUT PICK: IWM none — no OI-qualified pair" in caplog.text
    assert "legacy 2026-10-09 291/276 (OI 38/900)" in caplog.text
    # Widening the long band to 5% admits 280 as the weekly long; its short
    # must sit <= 280 x 0.95 = 266, so 276 is skipped for 265 (OI 1,500).
    legs = _build(weekly + monthly, max_below_spot_pct=5.0)
    assert legs is not None and (legs[0].strike, legs[1].strike) == (280, 265)


def test_proxy_put_warns_on_paginated_chain(caplog):
    class _Paged(_FakeChainTrading):
        def get_option_contracts(self, req):
            return SimpleNamespace(option_contracts=self._contracts, next_page_token="p2")
    h = OptionsHelper.__new__(OptionsHelper)
    h._trading = _Paged(_oi_chain(_MONTHLY, {291: 9781, 276: 14135}))
    with caplog.at_level(logging.WARNING, logger="options"):
        legs = h.build_proxy_put_spread("IWM", _SPOT, 25, 50, min_oi=100.0, today=_S3_TODAY)
    assert legs is not None
    assert "paginated" in caplog.text and "next_page_token" in caplog.text


def test_proxy_passes_min_oi_prefer_monthly_and_today_to_builder():
    # Orchestrator plumbing: the builder gets the gate's OWN OI floor, the
    # knob, and an ET date — so it can never propose a leg the gate is
    # guaranteed to reject.
    o = _proxy_orch()
    o.risk.limits.min_option_open_interest = 250.0
    o.cfg.proxy_put_prefer_monthly = False
    seen = {}

    def _capture(*a, **k):
        seen.update(k)
        e = _exp(35)
        return [
            OptionLeg(expiry=e, strike=220, right="put", side=Action.BUY),
            OptionLeg(expiry=e, strike=209, right="put", side=Action.SELL),
        ]
    o.options = SimpleNamespace(build_proxy_put_spread=_capture)
    o._propose_proxy_put(_put_proposal("EXTR"), _account(), [])
    assert seen["min_oi"] == 250.0
    assert seen["prefer_monthly"] is False
    assert isinstance(seen["today"], date)
    assert len(o._handled) == 1
    # Fixture without the limit / knob (older SimpleNamespace shapes): the
    # documented defaults travel (100 OI floor, monthly-first on).
    o2 = _proxy_orch()
    seen.clear()
    o2.options = SimpleNamespace(build_proxy_put_spread=_capture)
    o2._propose_proxy_put(_put_proposal("EXTR"), _account(), [])
    assert seen["min_oi"] == 100.0 and seen["prefer_monthly"] is True


def test_load_config_proxy_put_prefer_monthly(monkeypatch):
    from investment_strategy.config import Config, load_config
    assert Config.__dataclass_fields__["proxy_put_prefer_monthly"].default is True
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    monkeypatch.setenv("PROXY_PUT_PREFER_MONTHLY", "off")
    assert load_config().proxy_put_prefer_monthly is False
    monkeypatch.delenv("PROXY_PUT_PREFER_MONTHLY")
    assert load_config().proxy_put_prefer_monthly is True
