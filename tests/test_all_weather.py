"""Tests for the Jul-30 all-weather upgrade.

The complete-review verdict was an A-grade risk system around a strategy that
made +$8,024 on 9 up days and lost -$27,953 on 12 down days, with ZERO bearish
trades in 388 all-time (state/decisions logs: the model saw bearish reads and
could only HOLD). This suite covers the machinery that changes that:

  Phase 1 — expectancy gate on signal families, rotation-guard red-day
            release, thesis-decay default flip (config).
  Phase 2 — broken-momentum put carve-out, deterministic inverse-ETF
            auto-hedge (arm, persistence, ceiling, unwind).
  Phase 3 — regime exposure ladder (with the defensive T-bill exemption),
            defensive core fill/rotation, breadth-aware regime, capture-ratio
            KPI.

Runnable two ways:
    .venv/bin/python -m pytest tests/test_all_weather.py
    .venv/bin/pytest tests/
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import uuid
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.attribution import (
    SourceStats,
    negative_expectancy_families,
)
from investment_strategy.config import load_config
from investment_strategy.ledger import TradeLedger, TradeRecord
from investment_strategy.models import (
    AccountSnapshot,
    Action,
    OptionLeg,
    OptionStrategy,
    Position,
    RiskVerdict,
    TradeProposal,
)
from investment_strategy.orchestrator import Orchestrator
from investment_strategy.regime import RegimeReader
from investment_strategy.state import PortfolioState
from investment_strategy.track_record import _capture_ratio_card

from test_risk import _account, _buy, _limits, _pos, _rm
from test_risk import _opt, _opt_exp


def _neg(source: str, avg: float = -2.0, trips: int = 10) -> SourceStats:
    return SourceStats(source=source, trips=trips, wins=0, pl_pcts=[avg] * trips)


# --------------------------------------------------------------------------- #
# Phase 1 — expectancy gate on signal families
# --------------------------------------------------------------------------- #
def test_expectancy_gate_blocks_fresh_entry_when_all_families_negative():
    rm = _rm(_limits())
    d = rm.evaluate(
        _buy(), _account(), price=100.0, volatility=0.3,
        entry_families={"options_flow"},
        neg_families={"options_flow": _neg("options_flow")},
    )
    assert d.verdict is RiskVerdict.REJECTED
    assert "Expectancy gate" in d.reason and "options_flow" in d.reason


def test_expectancy_gate_passes_with_one_healthy_family():
    rm = _rm(_limits())
    d = rm.evaluate(
        _buy(), _account(), price=100.0, volatility=0.3,
        entry_families={"options_flow", "fundamentals"},
        neg_families={"options_flow": _neg("options_flow")},
    )
    assert "Expectancy gate" not in d.reason
    assert d.verdict is not RiskVerdict.REJECTED


def test_expectancy_gate_exempts_topups():
    rm = _rm(_limits())
    acct = _account(positions=[_pos("AAPL", qty=10.0, price=100.0)])
    d = rm.evaluate(
        _buy(), acct, price=100.0, volatility=0.3,
        entry_families={"options_flow"},
        neg_families={"options_flow": _neg("options_flow")},
    )
    assert "Expectancy gate" not in d.reason


def test_expectancy_gate_off_switch_and_no_citations_fail_open():
    rm = _rm(_limits(expectancy_gate_enabled=False))
    d = rm.evaluate(
        _buy(), _account(), price=100.0, volatility=0.3,
        entry_families={"options_flow"},
        neg_families={"options_flow": _neg("options_flow")},
    )
    assert "Expectancy gate" not in d.reason
    rm2 = _rm(_limits())
    d2 = rm2.evaluate(
        _buy(), _account(), price=100.0, volatility=0.3,
        entry_families=None,
        neg_families={"options_flow": _neg("options_flow")},
    )
    assert "Expectancy gate" not in d2.reason


def test_negative_expectancy_families_from_ledger():
    path = os.path.join(tempfile.gettempdir(), f"_aw_{uuid.uuid4().hex}.jsonl")
    ledger = TradeLedger(path)
    for i in range(3):
        ledger.record(TradeRecord(
            symbol=f"X{i}", action="buy", qty=10.0, entry_price=100.0,
            cost_usd=1000.0, key_signals=["options_flow imbalance +0.65"],
            entry_signals=["news"],
        ))
        ledger.record(TradeRecord(
            symbol=f"X{i}", action="sell", qty=10.0, realized_pl_pct=-5.0,
            realized_pl=-50.0, exit_reason="decision",
        ))
    neg = negative_expectancy_families(ledger, window_days=14, min_trips=3)
    assert "options_flow" in neg
    assert neg["options_flow"].trips == 3
    # Small samples never judge a family.
    assert negative_expectancy_families(ledger, window_days=14, min_trips=4) == {}


# --------------------------------------------------------------------------- #
# Phase 1 — rotation-guard red-day release
# --------------------------------------------------------------------------- #
def _rot_orch(red_day_release=True):
    o = Orchestrator.__new__(Orchestrator)
    o.cfg = SimpleNamespace(risk=SimpleNamespace(
        rotation_loss_guard_enabled=True,
        rotation_guard_min_loss_pct=4.0,
        rotation_min_conviction_edge=0.10,
        rotation_require_composite_edge=False,
        rotation_guard_exempt_sell_conviction=0.65,
        rotation_guard_max_loss_pct=8.0,
        rotation_guard_repeat_release_pct=0.0,
        rotation_guard_red_day_release=red_day_release,
        max_open_positions=2,
        composite_budget_blend=False,
        min_cash_buffer_pct=0.0,
        max_cycle_symbol_share_pct=100.0,
    ))
    p = os.path.join(tempfile.gettempdir(), f"_aw_rot_{uuid.uuid4().hex}.json")
    o.state = PortfolioState(path=p)
    o.ledger = SimpleNamespace(effective=lambda: [])
    o.risk = SimpleNamespace(kill_switch=False)
    o.journal = SimpleNamespace(record=lambda *a, **k: None)
    o._rotation_vetoes = {}
    return o


def _rot_acct(day_pl_negative: bool):
    # day_pl_pct is derived from equity vs last_equity.
    equity = 99_000.0 if day_pl_negative else 101_000.0
    return AccountSnapshot(
        equity=equity, last_equity=100_000.0, cash=10_000.0,
        buying_power=10_000.0,
        positions=[Position(
            symbol="LOSER", qty=10.0, avg_entry_price=100.0,
            current_price=95.0, market_value=950.0, unrealized_pl=-50.0,
            unrealized_pl_pct=-5.0,
        )],
    )


def _rot_props():
    return [
        TradeProposal(symbol="LOSER", action=Action.SELL, conviction=0.5,
                      target_weight_pct=5.0, rationale="cut"),
        TradeProposal(symbol="NEW", action=Action.BUY, conviction=0.55,
                      target_weight_pct=5.0, rationale="in"),
    ]


def test_red_day_releases_loss_cut():
    o = _rot_orch(red_day_release=True)
    o.state.register_buy("LOSER", conviction=0.5)
    kept = o._apply_rotation_guard(_rot_props(), _rot_acct(True), {})
    assert {p.symbol for p in kept} == {"LOSER", "NEW"}


def test_green_day_still_vetoes_without_edge():
    o = _rot_orch(red_day_release=True)
    o.state.register_buy("LOSER", conviction=0.5)
    kept = o._apply_rotation_guard(_rot_props(), _rot_acct(False), {})
    assert {p.symbol for p in kept} == {"NEW"}


def test_red_day_release_off_switch():
    o = _rot_orch(red_day_release=False)
    o.state.register_buy("LOSER", conviction=0.5)
    kept = o._apply_rotation_guard(_rot_props(), _rot_acct(True), {})
    assert {p.symbol for p in kept} == {"NEW"}


# --------------------------------------------------------------------------- #
# Phase 2 — broken-momentum put carve-out
# --------------------------------------------------------------------------- #
def _long_put():
    return _opt(OptionStrategy.LONG_PUT, [OptionLeg(
        expiry=_opt_exp(30), strike=95, right="put", side=Action.BUY,
    )])


def test_put_passes_when_name_sharply_below_20d_sma():
    rm = _rm(_limits(options_enabled=True))
    d = rm.evaluate_option(
        _long_put(), _account(), est_premium_per_contract=2.0,
        market_trend="up", regime_label="risk-on", name_trend="up",
        name_ext_pct=-6.0,
    )
    assert d.verdict is not RiskVerdict.REJECTED


def test_put_still_blocked_under_breakdown_threshold():
    rm = _rm(_limits(options_enabled=True))
    d = rm.evaluate_option(
        _long_put(), _account(), est_premium_per_contract=2.0,
        market_trend="up", regime_label="risk-on", name_trend="up",
        name_ext_pct=-3.0,
    )
    assert d.verdict is RiskVerdict.REJECTED
    assert "20d SMA" in d.reason


def test_put_breakdown_carveout_off_switch():
    rm = _rm(_limits(options_enabled=True, put_breakdown_ext_pct=0.0))
    d = rm.evaluate_option(
        _long_put(), _account(), est_premium_per_contract=2.0,
        market_trend="up", regime_label="risk-on", name_trend="up",
        name_ext_pct=-6.0,
    )
    assert d.verdict is RiskVerdict.REJECTED


# --------------------------------------------------------------------------- #
# Phase 3 — regime exposure ladder
# --------------------------------------------------------------------------- #
def test_ladder_caps_risk_off_gross():
    rm = _rm(_limits())
    acct = _account(positions=[_pos("MSFT", qty=350.0, price=100.0)])  # $35k
    d = rm.evaluate(
        _buy(), acct, price=100.0, volatility=0.3, regime_label="risk-off",
    )
    assert d.verdict is RiskVerdict.REJECTED
    assert "exposure-ladder" in d.reason and "risk-off" in d.reason


def test_ladder_neutral_rung_allows_below_60pct():
    rm = _rm(_limits())
    acct = _account(positions=[_pos("MSFT", qty=350.0, price=100.0)])  # $35k
    d = rm.evaluate(
        _buy(), acct, price=100.0, volatility=0.3, regime_label="neutral",
    )
    assert "exposure-ladder" not in d.reason


def test_ladder_exempts_defensive_sleeve():
    rm = _rm(_limits())
    acct = _account(positions=[
        _pos("MSFT", qty=250.0, price=100.0),   # $25k risk
        _pos("SGOV", qty=150.0, price=100.0),   # $15k cash proxy
    ])
    blocked = rm.evaluate(
        _buy(), acct, price=100.0, volatility=0.3, regime_label="risk-off",
    )
    assert blocked.verdict is RiskVerdict.REJECTED  # 40k counted without exemption
    ok = rm.evaluate(
        _buy(), acct, price=100.0, volatility=0.3, regime_label="risk-off",
        defensive_exempt_usd=15_000.0,
    )
    assert "exposure-ladder" not in ok.reason


def test_ladder_inert_in_risk_on_and_when_disabled():
    acct = _account(positions=[_pos("MSFT", qty=350.0, price=100.0)])
    d = _rm(_limits()).evaluate(
        _buy(), acct, price=100.0, volatility=0.3, regime_label="risk-on",
    )
    assert "exposure-ladder" not in d.reason
    d2 = _rm(_limits(exposure_ladder_enabled=False)).evaluate(
        _buy(), acct, price=100.0, volatility=0.3, regime_label="risk-off",
    )
    assert "exposure-ladder" not in d2.reason


# --------------------------------------------------------------------------- #
# Phase 2 — deterministic auto-hedge (arm / persistence / ceiling / unwind)
# --------------------------------------------------------------------------- #
class _HedgeBroker:
    def __init__(self):
        self.buys = []
        self.closed = []
        self.canceled = []

    def cancel_open_orders_for(self, symbol):
        self.canceled.append(symbol)

    def close_position(self, symbol):
        self.closed.append(symbol)
        return f"oid-close-{symbol}"

    def submit_notional_buy(self, symbol, notional):
        self.buys.append((symbol, round(notional, 2)))
        return f"oid-buy-{symbol}"

    def latest_price(self, symbol):
        return 100.0


def _hedge_orch(falling=True, hedge_etf="PSQ", ratio=0.30, min_cycles=2,
                max_pct=15.0, halted=False):
    o = Orchestrator.__new__(Orchestrator)
    o.cfg = SimpleNamespace(
        hedge_etf=hedge_etf, auto_hedge_ratio=ratio,
        auto_hedge_min_cycles=min_cycles, auto_hedge_max_pct=max_pct,
        risk=SimpleNamespace(
            min_order_usd=1.0, min_order_pct=0.0, min_cash_buffer_pct=0.0,
        ),
    )
    o._market_falling = lambda: (falling, "test read")
    o.risk = SimpleNamespace(trading_halted=lambda a: (halted, "halt" if halted else ""))
    o.broker = _HedgeBroker()
    o.ledger = SimpleNamespace(records=[], record=lambda r, _s=o: None)
    records = []
    o.ledger = SimpleNamespace(records=records, record=records.append)
    p = os.path.join(tempfile.gettempdir(), f"_aw_hg_{uuid.uuid4().hex}.json")
    o.state = PortfolioState(path=p)
    o._trade_lock = threading.Lock()
    o._pending_oids = []
    o._falling_cycles = 0
    o._clear_cycles = 0
    return o


def _hedge_acct(net_long=60_000.0, cash=40_000.0, psq_value=0.0):
    positions = [Position(
        symbol="AAPL", qty=net_long / 100.0, avg_entry_price=100.0,
        current_price=100.0, market_value=net_long, unrealized_pl=0.0,
        unrealized_pl_pct=0.0,
    )]
    if psq_value > 0:
        positions.append(Position(
            symbol="PSQ", qty=psq_value / 100.0, avg_entry_price=100.0,
            current_price=100.0, market_value=psq_value, unrealized_pl=0.0,
            unrealized_pl_pct=0.0,
        ))
    return AccountSnapshot(
        equity=net_long + cash + psq_value, last_equity=net_long + cash,
        cash=cash, buying_power=cash, positions=positions,
    )


def test_auto_hedge_needs_persistence_before_arming():
    o = _hedge_orch(falling=True)
    o._apply_auto_hedge(_hedge_acct())
    assert o.broker.buys == []          # cycle 1/2 — not armed
    o._apply_auto_hedge(_hedge_acct())
    assert len(o.broker.buys) == 1      # cycle 2/2 — armed
    sym, notional = o.broker.buys[0]
    assert sym == "PSQ"
    # 30% of $60k net-long = $18k, clamped by the 15%-of-equity ceiling $15k.
    assert abs(notional - 15_000.0) < 1.0
    assert o.ledger.records and o.ledger.records[0].entry_signals == ["auto_hedge"]


def test_auto_hedge_tops_up_only_the_gap():
    o = _hedge_orch(falling=True, max_pct=50.0)
    o._falling_cycles = 2
    o._apply_auto_hedge(_hedge_acct(psq_value=10_000.0))
    # target 30% of 60k = 18k; held 10k -> buy the 8k gap.
    assert abs(o.broker.buys[0][1] - 8_000.0) < 1.0


def test_auto_hedge_skips_when_halted_and_when_disabled():
    o = _hedge_orch(falling=True, halted=True)
    o._falling_cycles = 5
    o._apply_auto_hedge(_hedge_acct())
    assert o.broker.buys == []
    o2 = _hedge_orch(falling=True, hedge_etf="")
    o2._falling_cycles = 5
    o2._apply_auto_hedge(_hedge_acct())
    assert o2.broker.buys == []


def test_auto_hedge_unwinds_after_clear_persistence():
    o = _hedge_orch(falling=False)
    acct = _hedge_acct(psq_value=15_000.0)
    o._apply_auto_hedge(acct)
    assert o.broker.closed == []        # clear 1/2 — hold the hedge
    o._apply_auto_hedge(acct)
    assert o.broker.closed == ["PSQ"]   # clear 2/2 — unwind
    assert o.ledger.records[-1].exit_reason == "hedge_unwind"
    assert o._falling_cycles == 0


# --------------------------------------------------------------------------- #
# Phase 3 — defensive core fill + rotation
# --------------------------------------------------------------------------- #
def _core_orch(defensive="SGOV", defense_active=True):
    o = Orchestrator.__new__(Orchestrator)
    o.cfg = SimpleNamespace(
        core_etf="QQQ", target_invested_pct=90.0, core_stop_pct=0.0,
        core_max_pct=0.0, core_fill_max_pct=0.0,
        defensive_core_etf=defensive,
        risk=SimpleNamespace(
            min_cash_buffer_pct=0.0, max_gross_exposure_pct=100.0,
            min_order_usd=1.0, min_order_pct=0.0,
            exposure_ladder_enabled=True,
            exposure_neutral_pct=60.0, exposure_risk_off_pct=30.0,
        ),
    )
    o.risk = SimpleNamespace(trading_halted=lambda a: (False, ""))
    o.broker = _HedgeBroker()
    records = []
    o.ledger = SimpleNamespace(records=records, record=records.append)
    p = os.path.join(tempfile.gettempdir(), f"_aw_cf_{uuid.uuid4().hex}.json")
    o.state = PortfolioState(path=p)
    o._trade_lock = threading.Lock()
    o._pending_oids = []
    o._core_defense_active = defense_active
    o._regime_label = ""
    return o


def test_defensive_fill_redirects_core_dca_to_tbills():
    o = _core_orch(defensive="SGOV", defense_active=True)
    # order_fill poll: instantly terminal.
    o.broker.order_fill = lambda oid: ("filled", 0.0, 0.0)
    acct = AccountSnapshot(
        equity=100_000.0, last_equity=100_000.0, cash=100_000.0,
        buying_power=100_000.0, positions=[],
    )
    o._apply_core_fill(acct)
    assert o.broker.buys and o.broker.buys[0][0] == "SGOV"
    assert o.ledger.records[0].entry_signals == ["defensive_fill"]


def test_defense_without_defensive_etf_still_pauses_fill():
    o = _core_orch(defensive="", defense_active=True)
    acct = AccountSnapshot(
        equity=100_000.0, last_equity=100_000.0, cash=100_000.0,
        buying_power=100_000.0, positions=[],
    )
    o._apply_core_fill(acct)
    assert o.broker.buys == []


def test_defensive_rotation_sells_tbills_once_clear():
    o = _core_orch(defensive="SGOV", defense_active=False)
    acct = AccountSnapshot(
        equity=100_000.0, last_equity=100_000.0, cash=40_000.0,
        buying_power=40_000.0,
        positions=[Position(
            symbol="SGOV", qty=600.0, avg_entry_price=100.0,
            current_price=100.0, market_value=60_000.0, unrealized_pl=20.0,
            unrealized_pl_pct=0.03,
        )],
    )
    o._apply_defensive_rotation(acct)
    assert o.broker.closed == ["SGOV"]
    assert o.ledger.records[-1].exit_reason == "defensive_rotate"
    assert acct.cash == 100_000.0  # snapshot folded back to cash


def test_defensive_rotation_holds_while_defense_active():
    o = _core_orch(defensive="SGOV", defense_active=True)
    acct = AccountSnapshot(
        equity=100_000.0, last_equity=100_000.0, cash=40_000.0,
        buying_power=40_000.0,
        positions=[Position(
            symbol="SGOV", qty=600.0, avg_entry_price=100.0,
            current_price=100.0, market_value=60_000.0, unrealized_pl=0.0,
            unrealized_pl_pct=0.0,
        )],
    )
    o._apply_defensive_rotation(acct)
    assert o.broker.closed == []


# --------------------------------------------------------------------------- #
# Phase 3 — breadth-aware regime
# --------------------------------------------------------------------------- #
def _patched_reader(monkeypatch, qqq_below: bool, iwm_below: bool):
    def closes(symbol, days):
        if symbol == "SPY":
            return [100.0] * 200 + [120.0] * 10          # well above 200dma
        if symbol == "^VIX":
            return [15.0] * 5                            # calm
        if symbol == "^VIX3M":
            return [17.0] * 5                            # contango
        below = qqq_below if symbol == "QQQ" else iwm_below
        return [100.0] * 50 + ([80.0] * 10 if below else [110.0] * 10)
    monkeypatch.setattr(RegimeReader, "_daily_closes", staticmethod(closes))
    return RegimeReader()


def test_breadth_divergence_shades_risk_on_to_neutral(monkeypatch):
    r = _patched_reader(monkeypatch, qqq_below=True, iwm_below=True).assess()
    assert r.label == "neutral"
    assert r.multiplier <= 0.7
    assert "narrow breadth" in r.reason
    assert r.trend == "up"  # the direction gate still reads the SPY trend


def test_breadth_needs_both_indexes_below(monkeypatch):
    r = _patched_reader(monkeypatch, qqq_below=True, iwm_below=False).assess()
    assert r.label == "risk-on"
    assert "narrow breadth" not in r.reason


# --------------------------------------------------------------------------- #
# Phase 3 — capture-ratio KPI
# --------------------------------------------------------------------------- #
def test_capture_ratio_card_math_and_render():
    rows = [
        {"date": "2026-07-28", "day_pl": 100.0},
        {"date": "2026-07-29", "day_pl": -50.0},
    ]
    html = _capture_ratio_card(rows)
    assert "2.00" in html and "Capture ratio" in html
    assert _capture_ratio_card([]) == ""


# --------------------------------------------------------------------------- #
# System-managed symbols never execute via model proposals
# --------------------------------------------------------------------------- #
def test_model_proposal_on_hedge_symbol_is_ignored():
    o = Orchestrator.__new__(Orchestrator)
    o.cfg = SimpleNamespace(core_etf="QQQ", hedge_etf="PSQ",
                            defensive_core_etf="SGOV")
    prop = TradeProposal(symbol="PSQ", action=Action.SELL, conviction=0.9,
                         target_weight_pct=5.0, rationale="model wants out")
    # No broker attached: reaching any broker call would raise — the guard
    # must return before touching one.
    assert o._handle_equity(prop, account=None) == 0.0


# --------------------------------------------------------------------------- #
# Config defaults (thesis decay flipped on; new knobs wired)
# --------------------------------------------------------------------------- #
def test_new_knob_defaults(monkeypatch):
    for k in (
        "THESIS_DECAY_ENABLED", "EXPECTANCY_GATE", "EXPOSURE_LADDER",
        "HEDGE_ETF", "DEFENSIVE_CORE_ETF", "ROTATION_GUARD_RED_DAY_RELEASE",
        "PUT_BREAKDOWN_EXT_PCT",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    cfg = load_config()
    assert cfg.risk.thesis_decay_enabled is True
    assert cfg.risk.expectancy_gate_enabled is True
    assert cfg.risk.exposure_ladder_enabled is True
    assert cfg.risk.rotation_guard_red_day_release is True
    assert cfg.risk.put_breakdown_ext_pct == 5.0
    assert cfg.hedge_etf == "" and cfg.defensive_core_etf == ""
