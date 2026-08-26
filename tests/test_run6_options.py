"""Run-6 item 3 — close the options bypass (Aug 25 review, sections 5-6).

Runs 4-5 lost -$24.8k on single-name bullish option debits that rode an
explicit bypass of the earnings blackout and the anti-chase gate (NVDA call
bought one day before its print; HL bull_call_spread on the chase the equity
gate had just blocked). The option path now gets the same gates as shares,
single-name bullish debits are OFF for the window, the equity->option
fallback no longer fires on "Earnings in", and the proxy put needs a
transferable thesis (else a 0.25%-of-equity token size)."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.config import RiskLimits, load_config
from investment_strategy.decision.prompts import SYSTEM_PROMPT
from investment_strategy.models import (
    Action,
    Instrument,
    OptionLeg,
    OptionStrategy,
    RiskDecision,
    RiskVerdict,
    TradeProposal,
)
from investment_strategy.orchestrator import Orchestrator
from test_risk import _account, _buy, _limits, _opt_exp, _rm

_EXTREME = {"rsi14": 70.0, "ext_atr": 4.2, "ext_pct_sma20": 18.0}


def _opt(symbol, strategy, legs) -> TradeProposal:
    return TradeProposal(
        symbol=symbol, action=Action.BUY, conviction=1.0, target_weight_pct=2.0,
        rationale="opt", instrument=Instrument.OPTION,
        option_strategy=strategy, option_legs=legs,
    )


def _long_call(symbol="NVDA"):
    return _opt(symbol, OptionStrategy.LONG_CALL, [
        OptionLeg(expiry=_opt_exp(30), strike=200, right="call", side=Action.BUY),
    ])


def _call_spread(symbol="HL"):
    return _opt(symbol, OptionStrategy.BULL_CALL_SPREAD, [
        OptionLeg(expiry=_opt_exp(30), strike=200, right="call", side=Action.BUY),
        OptionLeg(expiry=_opt_exp(30), strike=210, right="call", side=Action.SELL),
    ])


def _long_put(symbol="DKS"):
    return _opt(symbol, OptionStrategy.LONG_PUT, [
        OptionLeg(expiry=_opt_exp(30), strike=200, right="put", side=Action.BUY),
    ])


def _put_spread(symbol="IWM"):
    return _opt(symbol, OptionStrategy.BEAR_PUT_SPREAD, [
        OptionLeg(expiry=_opt_exp(30), strike=220, right="put", side=Action.BUY),
        OptionLeg(expiry=_opt_exp(30), strike=209, right="put", side=Action.SELL),
    ])


# --------------------------------------------------------------------------- #
# Knob
# --------------------------------------------------------------------------- #
def test_knob_defaults_off_and_loads_from_env(monkeypatch):
    assert RiskLimits.__dataclass_fields__["options_single_name_bullish"].default is False
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    monkeypatch.setenv("OPTIONS_SINGLE_NAME_BULLISH", "on")
    monkeypatch.setenv("PROXY_PUT_THESIS_GATE", "off")
    cfg = load_config()
    assert cfg.risk.options_single_name_bullish is True
    assert cfg.proxy_put_thesis_gate is False
    monkeypatch.delenv("OPTIONS_SINGLE_NAME_BULLISH")
    monkeypatch.delenv("PROXY_PUT_THESIS_GATE")
    monkeypatch.delenv("PROXY_PUT_UNTRANSFERRED_PCT", raising=False)
    cfg = load_config()
    assert cfg.risk.options_single_name_bullish is False
    assert cfg.proxy_put_thesis_gate is True
    assert cfg.proxy_put_untransferred_pct == 0.25


# --------------------------------------------------------------------------- #
# (d) single-name bullish debits off; puts, index calls unaffected
# --------------------------------------------------------------------------- #
def test_single_name_long_call_rejected_when_knob_off():
    rm = _rm(_limits(options_enabled=True, options_single_name_bullish=False))
    d = rm.evaluate_option(_long_call("NVDA"), _account(), est_premium_per_contract=2.0)
    assert d.verdict is RiskVerdict.REJECTED
    assert d.reason.startswith("single-name bullish option debits disabled for this window")
    assert "$200" in d.reason  # counterfactual debit per contract
    d2 = rm.evaluate_option(_call_spread("HL"), _account(), est_premium_per_contract=2.0)
    assert d2.verdict is RiskVerdict.REJECTED
    assert "disabled for this window" in d2.reason


def test_single_name_put_allowed_when_knob_off():
    rm = _rm(_limits(options_enabled=True, options_single_name_bullish=False,
                     per_underlying_premium_pct=0.0))
    d = rm.evaluate_option(_long_put("DKS"), _account(), est_premium_per_contract=2.0,
                           market_trend="down")
    assert d.verdict is RiskVerdict.APPROVED, d.reason
    assert d.approved_qty == 5


def test_index_call_allowed_when_knob_off():
    rm = _rm(_limits(options_enabled=True, options_single_name_bullish=False,
                     per_underlying_premium_pct=0.0))
    for sym in ("SPY", "QQQ"):
        d = rm.evaluate_option(_long_call(sym), _account(), est_premium_per_contract=2.0)
        assert d.verdict is RiskVerdict.APPROVED, d.reason
    # configured ETFs (core / hedge / proxy) join the exemption via index_symbols
    d = rm.evaluate_option(_long_call("XLK"), _account(), est_premium_per_contract=2.0,
                           index_symbols=frozenset({"XLK"}))
    assert d.verdict is RiskVerdict.APPROVED, d.reason
    d = rm.evaluate_option(_long_call("XLK"), _account(), est_premium_per_contract=2.0)
    assert d.verdict is RiskVerdict.REJECTED


def test_single_name_call_allowed_when_knob_on():
    rm = _rm(_limits(options_enabled=True, options_single_name_bullish=True,
                     per_underlying_premium_pct=0.0))
    d = rm.evaluate_option(_long_call("NVDA"), _account(), est_premium_per_contract=2.0)
    assert d.verdict is RiskVerdict.APPROVED, d.reason


# --------------------------------------------------------------------------- #
# (a) earnings blackout applies to option debits; index exempt; anti-chase
# --------------------------------------------------------------------------- #
def test_earnings_blackout_rejects_single_name_option():
    rm = _rm(_limits(options_enabled=True, options_single_name_bullish=True,
                     earnings_blackout_days=3, per_underlying_premium_pct=0.0))
    d = rm.evaluate_option(_long_call("NVDA"), _account(), est_premium_per_contract=2.0,
                           days_to_earnings=1)
    assert d.verdict is RiskVerdict.REJECTED
    assert d.reason.startswith("Earnings in 1d")
    # puts too — a debit gaps through the print either way
    d = rm.evaluate_option(_long_put("DKS"), _account(), est_premium_per_contract=2.0,
                           market_trend="down", days_to_earnings=0)
    assert d.verdict is RiskVerdict.REJECTED
    assert "Earnings in 0d" in d.reason
    # outside the window / unknown date -> fails open
    for dte in (4, None):
        d = rm.evaluate_option(_long_call("NVDA"), _account(), est_premium_per_contract=2.0,
                               days_to_earnings=dte)
        assert d.verdict is RiskVerdict.APPROVED, d.reason


def test_earnings_blackout_exempts_index_underlyings():
    rm = _rm(_limits(options_enabled=True, per_underlying_premium_pct=0.0))
    d = rm.evaluate_option(_put_spread("IWM"), _account(), est_premium_per_contract=2.0,
                           market_trend="down", days_to_earnings=1)
    assert d.verdict is RiskVerdict.APPROVED, d.reason


def test_earnings_blackout_off_at_zero():
    rm = _rm(_limits(options_enabled=True, earnings_blackout_days=0,
                     per_underlying_premium_pct=0.0))
    d = rm.evaluate_option(_long_call("NVDA"), _account(), est_premium_per_contract=2.0,
                           days_to_earnings=1)
    assert d.verdict is RiskVerdict.APPROVED, d.reason


def test_chase_gate_still_covers_bullish_debits_with_knob_on():
    rm = _rm(_limits(options_enabled=True, options_single_name_bullish=True))
    for p in (_long_call("HL"), _call_spread("HL")):
        d = rm.evaluate_option(p, _account(), est_premium_per_contract=2.0, tech=_EXTREME)
        assert d.verdict is RiskVerdict.REJECTED
        assert "OPTION CHASE GATE" in d.reason


# --------------------------------------------------------------------------- #
# orchestrator: earnings read + index set travel into evaluate_option
# --------------------------------------------------------------------------- #
def _opt_orch(reason="ok", earnings_days=1, cfg=None):
    o = Orchestrator.__new__(Orchestrator)
    o.options = SimpleNamespace(
        estimate_net_premium=lambda p: 1.0,
        min_leg_premium=lambda p: 0.5,
        leg_liquidity=lambda p: [],
    )
    o.cfg = cfg if cfg is not None else SimpleNamespace(
        core_etf="QQQ", hedge_etf="PSQ", put_proxy_etf="IWM",
        defensive_core_etf="", proxy_put_thesis_gate=True,
    )
    o._regime_trend, o._regime_label, o._regime_mult = "up", "risk-on", 1.0
    o._hedge_symbol = ""
    o._journal_decision = lambda *a, **k: None
    o.earnings = SimpleNamespace(days_until_earnings=lambda s: earnings_days)
    o.calls = []

    def _eval(proposal, account, *a, **k):
        o.calls.append(k)
        return RiskDecision(proposal=proposal, verdict=RiskVerdict.REJECTED, reason=reason)
    o.risk = SimpleNamespace(evaluate_option=_eval)
    o._propose_proxy_put = lambda *a, **k: None
    return o


def test_handle_option_passes_earnings_and_index_symbols():
    o = _opt_orch(earnings_days=2)
    o._handle_option(_long_call("NVDA"), _account())
    k = o.calls[0]
    assert k["days_to_earnings"] == 2
    assert {"QQQ", "PSQ", "IWM"} <= set(k["index_symbols"])
    assert k["proxy_put"] is False


def test_handle_option_proxy_flag_and_no_earnings_read_for_proxy():
    o = _opt_orch(earnings_days=2)
    o._handle_option(_put_spread("IWM"), _account(), proxy_for="EXTR")
    k = o.calls[0]
    assert k["days_to_earnings"] is None
    assert k["proxy_put"] is True
    assert k["sanctioned_hedge"] is True
    o2 = _opt_orch(cfg=SimpleNamespace(proxy_put_thesis_gate=False))
    o2._handle_option(_put_spread("IWM"), _account(), proxy_for="EXTR")
    assert o2.calls[0]["proxy_put"] is False


def test_handle_option_earnings_read_fails_open():
    o = _opt_orch()
    def _boom(s):
        raise RuntimeError("calendar down")
    o.earnings = SimpleNamespace(days_until_earnings=_boom)
    o._handle_option(_long_call("NVDA"), _account())
    assert o.calls[0]["days_to_earnings"] is None


# --------------------------------------------------------------------------- #
# (b) same-cycle option fallback ignores "Earnings in"
# --------------------------------------------------------------------------- #
def _eq_orch(reason: str):
    o = Orchestrator.__new__(Orchestrator)
    o.cfg = SimpleNamespace(
        core_etf="", hedge_etf="", defensive_core_etf="",
        risk=SimpleNamespace(max_pairwise_corr=0.0),
    )
    o.broker = SimpleNamespace(
        latest_price=lambda s: 100.0, annualized_vol=lambda s: 0.3,
        open_buy_notional=lambda s: 0.0,
    )
    o.earnings = SimpleNamespace(days_until_earnings=lambda s: 1)
    o._sector_context = lambda s, a: (None, 0.0)
    o._regime_mult, o._regime_trend, o._regime_label = 1.0, "up", "risk-on"
    o.options = object()
    o._option_fallbacks = []
    o._journal_decision = lambda *a, **k: None
    o.risk = SimpleNamespace(evaluate=lambda proposal, *a, **k: RiskDecision(
        proposal=proposal, verdict=RiskVerdict.REJECTED, reason=reason))
    return o


def test_fallback_queue_ignores_earnings_reject():
    o = _eq_orch("Earnings in 1d (<= 3d blackout) — gap risk through the print.")
    o._handle_equity(_buy("NVDA"), _account())
    assert o._option_fallbacks == []


def test_fallback_queue_still_takes_overextended_reject():
    o = _eq_orch("Overextended: RSI 70 and 4.2xATR above the 20d SMA.")
    o._handle_equity(_buy("HL"), _account())
    assert [p.symbol for p, _ in o._option_fallbacks] == ["HL"]


# --------------------------------------------------------------------------- #
# (e) proxy put: per-underlying cap + transferable-thesis sizing
# --------------------------------------------------------------------------- #
def test_proxy_put_loses_per_underlying_cap_exemption():
    from test_run5_gates import _opt_lot
    # $500 cap (0.5% of $100k); $400 open on IWM + $200/contract new debit
    rm = _rm(_limits(options_enabled=True))
    held = [_opt_lot("IWM", qty=2.0, avg_entry=2.0, right="put")]
    legacy = rm.evaluate_option(_put_spread("IWM"), _account(positions=held),
                                est_premium_per_contract=2.0, market_trend="down",
                                sanctioned_hedge=True, proxy_put=False)
    assert legacy.verdict is RiskVerdict.APPROVED, legacy.reason
    capped = rm.evaluate_option(_put_spread("IWM"), _account(positions=held),
                                est_premium_per_contract=2.0, market_trend="down",
                                sanctioned_hedge=True, proxy_put=True)
    assert capped.verdict is RiskVerdict.REJECTED
    assert "PER-UNDERLYING PREMIUM CAP" in capped.reason


def _proxy_orch(**over):
    from test_put_proxy import _proxy_orch as _base
    o = _base()
    o.cfg = SimpleNamespace(put_proxy_etf="IWM", proxy_put_thesis_gate=True,
                            proxy_put_untransferred_pct=0.25)
    o.broker = SimpleNamespace(
        latest_price=lambda s: 221.3,
        daily_close_series=lambda s, n: [(f"d{i}", 200.0) for i in range(60)],
    )
    o._falling_trigger = ""
    o._bear_eligibility = {}
    o.sectors = SimpleNamespace(sector_for=lambda s: None)
    for k, v in over.items():
        setattr(o, k, v)
    return o


def _blocked(symbol="EXTR", max_premium=None):
    p = _long_put(symbol)
    p.max_premium_usd = max_premium
    return p


def test_proxy_sized_small_without_transferable_thesis():
    o = _proxy_orch()  # IWM 221 above its 50d SMA (200), no breadth, no sector
    o._propose_proxy_put(_blocked(), _account(), [])
    proxy, tag = o._handled[0]
    assert tag == "EXTR"
    assert proxy.max_premium_usd == 250.0  # 0.25% of $100k
    assert "does not transfer" in proxy.rationale
    # the model's own ceiling never grows: min(model, small)
    o2 = _proxy_orch()
    o2._propose_proxy_put(_blocked(max_premium=100.0), _account(), [])
    assert o2._handled[0][0].max_premium_usd == 100.0


def test_proxy_full_size_when_etf_below_50d_sma():
    o = _proxy_orch(broker=SimpleNamespace(
        latest_price=lambda s: 221.3,
        daily_close_series=lambda s, n: [(f"d{i}", 240.0) for i in range(60)],
    ))
    o._propose_proxy_put(_blocked(max_premium=900.0), _account(), [])
    proxy = o._handled[0][0]
    assert proxy.max_premium_usd == 900.0
    assert "below its 50d SMA" in proxy.rationale


def test_proxy_full_size_when_breadth_armed():
    o = _proxy_orch(_falling_trigger="breadth:4-names")
    o._propose_proxy_put(_blocked(), _account(), [])
    proxy = o._handled[0][0]
    assert proxy.max_premium_usd is None
    assert "breadth trigger armed" in proxy.rationale


def test_proxy_full_size_when_two_bearish_names_share_sector():
    sec = {"EXTR": "Technology", "TDC": "Technology", "DKS": "Consumer"}
    o = _proxy_orch(
        _bear_eligibility={"TDC": (True, "x"), "DKS": (True, "y")},
        sectors=SimpleNamespace(sector_for=lambda s: sec.get(s)),
    )
    o._propose_proxy_put(_blocked("EXTR"), _account(), [])
    proxy = o._handled[0][0]
    assert proxy.max_premium_usd is None
    assert "2 bearish slate names in Technology" in proxy.rationale
    # a lone bearish name in its sector does not transfer
    o2 = _proxy_orch(
        _bear_eligibility={"DKS": (True, "y")},
        sectors=SimpleNamespace(sector_for=lambda s: sec.get(s)),
    )
    o2._propose_proxy_put(_blocked("EXTR"), _account(), [])
    assert o2._handled[0][0].max_premium_usd == 250.0


def test_proxy_thesis_gate_fails_closed_on_feed_error():
    def _boom(s, n):
        raise RuntimeError("bars down")
    o = _proxy_orch(broker=SimpleNamespace(latest_price=lambda s: 221.3,
                                           daily_close_series=_boom))
    o._propose_proxy_put(_blocked(), _account(), [])
    assert o._handled[0][0].max_premium_usd == 250.0


def test_proxy_thesis_gate_off_keeps_legacy_sizing():
    o = _proxy_orch()
    o.cfg = SimpleNamespace(put_proxy_etf="IWM", proxy_put_thesis_gate=False)
    o._propose_proxy_put(_blocked(), _account(), [])
    proxy = o._handled[0][0]
    assert proxy.max_premium_usd is None
    assert "transfer" not in proxy.rationale


# --------------------------------------------------------------------------- #
# (c) prompt text
# --------------------------------------------------------------------------- #
def test_prompt_no_longer_sanctions_option_bypass():
    assert "option debits are exempt" not in SYSTEM_PROMPT
    assert "sanctioned \\\nvehicle" not in SYSTEM_PROMPT and "sanctioned vehicle" not in SYSTEM_PROMPT
    assert "apply to equity buys ONLY" not in SYSTEM_PROMPT
    assert "The same gates apply to every instrument" in SYSTEM_PROMPT
    # neighbouring blocks intact
    assert "long_put or bear_put_spread is the ONLY way" in SYSTEM_PROMPT
    assert "Pick expiries 2-8 weeks out" in SYSTEM_PROMPT
