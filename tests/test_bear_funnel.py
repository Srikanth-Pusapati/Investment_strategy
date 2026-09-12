"""Bear-funnel end-to-end + Aug-1 escalation tests.

The Jul-30 review's open finding: no end-to-end bear test existed — every
funnel stage was unit-tested, but nothing asserted that a bearish read can
actually SURVIVE the whole pipeline, so the funnel died at zero three times
(Jul 30 instrumentation, Jul 31 precheck fix, and still: three cycles of
ELIGIBLE names on Jul 31 with zero puts and zero journal mentions).

Two escalations shipped Aug 1, both covered here:

  1. Schema-forced bearish verdicts — the decision output REQUIRES one
     bearish_verdicts entry per put-ELIGIBLE name (put_proposed/declined);
     the orchestrator reconciles them and journals put_declined/put_ignored,
     so silence is now a queryable record.
  2. Per-NAME falling read (NAME_DROP_DEFENSE_PCT) — defense that doesn't
     wait for an index-wide day: HELD-line note + rotation-guard loss-cut
     release (Jul 29 NOK: -4.8% guard-pinned ~2h while SPY bottomed -1.2%).

The e2e tests chain the REAL components stage by stage: precheck (risk) ->
prompt render (engine) -> model reply parse (engine) -> option gate (risk).
If any stage regresses — precheck drifts from the gate, the prompt stops
rendering eligibility, the parser drops verdicts, the gate rejects its own
sanctioned shape — a bearish position no longer survives and CI fails.

Runnable two ways:
    .venv/bin/python -m pytest tests/test_bear_funnel.py
    .venv/bin/pytest tests/
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import uuid
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.config import load_config
from investment_strategy.decision.engine import DecisionEngine
from investment_strategy.decision.prompts import PROPOSALS_SCHEMA, SYSTEM_PROMPT
from investment_strategy.models import (
    Action,
    Instrument,
    OptionLeg,
    OptionStrategy,
    Position,
    RiskVerdict,
    Signal,
    SignalBundle,
    SignalKind,
    TradeProposal,
)
from investment_strategy.orchestrator import Orchestrator
from investment_strategy.state import PortfolioState

from test_risk import _account, _limits, _opt_exp, _rm
from test_all_weather import _rot_acct, _rot_orch, _rot_props


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _engine(limits) -> DecisionEngine:
    """Engine with no API client — only the pure render/parse paths run."""
    e = DecisionEngine.__new__(DecisionEngine)
    e.cfg = SimpleNamespace(risk=limits)
    e.last_bear_verdicts = {}
    return e


def _bundle(sym: str, tech: dict) -> SignalBundle:
    return SignalBundle(symbol=sym, signals=[Signal(
        kind=SignalKind.TECHNICAL, symbol=sym,
        summary="downtrend, below moving averages", score=-0.8,
        source="test", data=tech,
    )])


def _put_proposal(sym: str, strike: float = 75.0) -> TradeProposal:
    return TradeProposal(
        symbol=sym, action=Action.BUY, conviction=0.7, target_weight_pct=0.0,
        rationale="broken name", instrument=Instrument.OPTION,
        option_strategy=OptionStrategy.LONG_PUT,
        option_legs=[OptionLeg(
            expiry=_opt_exp(30), strike=strike, right="put", side=Action.BUY,
        )],
    )


def _held(sym: str, price: float, entry: float, qty: float = 10.0) -> Position:
    pl_pct = (price / entry - 1.0) * 100.0
    return Position(
        symbol=sym, qty=qty, avg_entry_price=entry, current_price=price,
        market_value=qty * price, unrealized_pl=qty * (price - entry),
        unrealized_pl_pct=pl_pct,
    )


# --------------------------------------------------------------------------- #
# End-to-end: calm-tape single-name breakdown (the actual July loss pattern —
# NU/NOK-shaped: index reads risk-on/up, the name alone is broken)
# --------------------------------------------------------------------------- #
def test_funnel_survives_calm_tape_name_breakdown():
    limits = _limits(options_enabled=True)
    rm = _rm(limits)
    tech = {"price": 80.0, "sma200": 70.0, "ext_pct_sma20": -18.3,
            "prev_close": 90.0}

    # Stage 1: deterministic precheck says ELIGIBLE (20d-SMA breakdown
    # carve-out — the name still sits ABOVE its 200dma, like NU/NOK did).
    ok, why = rm.put_precheck("VCYT", _account(), "up", "risk-on", tech)
    assert ok and "20d SMA" in why

    # Stage 2: the prompt renders the eligibility AND the mandatory-verdict
    # contract bound to the eligible list.
    text = _engine(limits)._render_dynamic(
        [_bundle("VCYT", tech)], _account(), "", [],
        composites={"VCYT": -1.10},
        regime_label="risk-on", regime_trend="up",
        put_eligibility={"VCYT": (ok, why)},
    )
    assert "Put-ELIGIBLE" in text and "VCYT" in text
    assert "MANDATORY" in text and "bearish_verdicts" in text

    # Stage 3: a schema-shaped model reply parses into the proposal AND the
    # verdict map.
    reply = json.dumps({
        "proposals": [{
            "symbol": "VCYT", "action": "buy", "conviction": 0.7,
            "target_weight_pct": 0.0, "stop_loss_pct": None,
            "take_profit_pct": None,
            "rationale": "-18.3% under 20d SMA with bearish flow",
            "key_signals": ["technical"], "instrument": "option",
            "option_strategy": "long_put",
            "option_legs": [{
                "expiry": _opt_exp(30), "strike": 75.0, "right": "put",
                "side": "buy", "ratio": 1,
            }],
            "max_premium_usd": 800.0,
        }],
        "bearish_verdicts": [{
            "symbol": "VCYT", "verdict": "put_proposed",
            "reason": "broken 20d SMA, bearish composite -1.10",
        }],
    })
    proposals, verdicts = DecisionEngine._parse(reply)
    assert len(proposals) == 1
    assert verdicts["VCYT"] == (
        "put_proposed", "broken 20d SMA, bearish composite -1.10",
    )

    # Stage 4: the REAL option gate approves what the precheck promised —
    # >= 1 surviving bearish position. The funnel did not die at zero.
    d = rm.evaluate_option(
        proposals[0], _account(), est_premium_per_contract=1.50,
        market_trend="up", regime_label="risk-on", name_trend="up",
        name_ext_pct=-18.3,
    )
    assert d.verdict is not RiskVerdict.REJECTED
    assert d.approved_qty >= 1


# --------------------------------------------------------------------------- #
# End-to-end: red tape (the Jul-30 report's requested fixture — a simulated
# risk-off tape must end with >= 1 surviving bearish position)
# --------------------------------------------------------------------------- #
def test_funnel_survives_red_tape():
    limits = _limits(options_enabled=True)
    rm = _rm(limits)

    ok, why = rm.put_precheck("XYZ", _account(), "down", "risk-off", None)
    assert ok

    text = _engine(limits)._render_dynamic(
        [_bundle("XYZ", {"price": 90.0})], _account(), "", [],
        composites={"XYZ": -0.90},
        regime_label="risk-off", regime_trend="down",
        put_eligibility={"XYZ": (ok, why)},
    )
    assert "Put-ELIGIBLE" in text and "RISK-OFF" in text

    d = rm.evaluate_option(
        _put_proposal("XYZ", strike=85.0), _account(),
        est_premium_per_contract=1.20,
        market_trend="down", regime_label="risk-off",
    )
    assert d.verdict is not RiskVerdict.REJECTED
    assert d.approved_qty >= 1


# --------------------------------------------------------------------------- #
# Schema + parse contract
# --------------------------------------------------------------------------- #
def test_schema_requires_bearish_verdicts():
    assert "bearish_verdicts" in PROPOSALS_SCHEMA["required"]
    item = PROPOSALS_SCHEMA["properties"]["bearish_verdicts"]["items"]
    assert set(item["required"]) == {"symbol", "verdict", "reason"}
    assert "BEARISH VERDICTS" in SYSTEM_PROMPT


def test_parse_tolerates_missing_verdicts_field():
    # Old-shape replies (option-fallback calls, historical fixtures) must not
    # crash the parser — they just carry no verdicts.
    proposals, verdicts = DecisionEngine._parse('{"proposals": []}')
    assert proposals == [] and verdicts == {}


def test_parse_skips_malformed_verdict_keeps_rest():
    reply = json.dumps({"proposals": [], "bearish_verdicts": [
        {"verdict": "declined"},  # missing symbol -> skipped
        {"symbol": "glue", "verdict": "declined", "reason": "thin composite"},
    ]})
    _, verdicts = DecisionEngine._parse(reply)
    assert verdicts == {"GLUE": ("declined", "thin composite")}


# --------------------------------------------------------------------------- #
# Verdict reconciliation (orchestrator side)
# --------------------------------------------------------------------------- #
def _reco_orch(eligibility, verdicts):
    o = Orchestrator.__new__(Orchestrator)
    o._bear_eligibility = eligibility
    o.engine = SimpleNamespace(last_bear_verdicts=verdicts)
    o._journal_rows = []
    o._journal_decision = (
        lambda *a, **k: o._journal_rows.append(a)
    )
    return o


def test_reconcile_stages_proposed_declined_and_skips_blocked():
    o = _reco_orch(
        {"AAA": (True, "below its 200dma"), "BBB": (True, "risk-off regime"),
         "CCC": (False, "uptrend name")},
        {"AAA": ("declined", "no bearish flow confirm")},
    )
    o._reconcile_bear_verdicts([_put_proposal("BBB")])
    assert o._bear_verdict_stage["BBB"] == "put proposed"
    assert o._bear_verdict_stage["AAA"].startswith("declined: no bearish flow")
    assert "CCC" not in o._bear_verdict_stage  # gate-blocked names owe nothing
    journaled = {row[0]: row[5] for row in o._journal_rows}
    assert journaled == {"AAA": "put_declined"}


def test_reconcile_flags_silent_skip_as_ignored():
    # The exact Jul-31 failure: ELIGIBLE name, no verdict, no proposal, no
    # journal trace. Now it must land as put_ignored.
    o = _reco_orch({"GLUE": (True, "below its 200dma")}, {})
    o._reconcile_bear_verdicts([])
    assert o._bear_verdict_stage["GLUE"] == "IGNORED"
    journaled = {row[0]: row[5] for row in o._journal_rows}
    assert journaled == {"GLUE": "put_ignored"}


def test_reconcile_claimed_put_without_proposal_is_ignored():
    o = _reco_orch(
        {"RBLX": (True, "below its 200dma")},
        {"RBLX": ("put_proposed", "will propose")},
    )
    o._reconcile_bear_verdicts([])  # ...but no put actually returned
    assert o._bear_verdict_stage["RBLX"] == "IGNORED"
    row = o._journal_rows[0]
    assert row[5] == "put_ignored" and "claimed put_proposed" in row[7]


def test_reconcile_noop_without_eligible_names():
    o = _reco_orch({"CCC": (False, "uptrend name")}, {})
    o._reconcile_bear_verdicts([])
    assert o._bear_verdict_stage == {} and o._journal_rows == []


# --------------------------------------------------------------------------- #
# BEARISH FUNNEL line (run-7 change-set, item A5): every ELIGIBLE name renders
# its terminal stage, '-> IGNORED' is a literal handle, ignored=N is a field.
#
# Sep 10 2026: the line rendered only the 6 most-bearish names, so all three
# 'ELIGIBLE but IGNORED' verdicts that day (ABT 7th of 7; KORU/STE 7th/8th of
# 8) fell off it — `grep '-> IGNORED'` returned 0 across the whole run-6
# window while the WARNING fired three times, and the window was scored
# "0 IGNORED" on a truncated line.
# --------------------------------------------------------------------------- #
def _funnel_orch(eligibility, verdicts, stages=None, hedge_etf=""):
    o = _reco_orch(eligibility, verdicts)
    o.cfg = SimpleNamespace(hedge_etf=hedge_etf)
    o._falling_cycles = 0
    o._bear_puts_proposed = 0
    o._bear_puts_approved = 0
    o._proxy_put_state = ""
    if stages is not None:
        o._bear_verdict_stage = stages
    return o


def _funnel_line(o, bear_map, caplog) -> str:
    with caplog.at_level(logging.INFO, logger="orchestrator"):
        o._log_bear_funnel(bear_map, _account())
    lines = [r.getMessage() for r in caplog.records
             if r.getMessage().startswith("BEARISH FUNNEL:")]
    assert len(lines) == 1
    return lines[0]


def test_funnel_line_renders_ignored_names_past_the_top_six(caplog):
    # The Sep 10 09:26 shape: 8 bearish names, the two IGNORED ones ranked
    # 7th and 8th by score. The old [:6] cut rendered neither.
    bear_map = {"LYV": -1.65, "CLH": -1.57, "VIPS": -1.27, "HD": -1.22,
                "VIK": -1.11, "IWM": -1.09, "KORU": -0.80, "STE": -0.77}
    eligibility = {
        "LYV": (True, "-5.3% vs 20d SMA breakdown"),
        "CLH": (False, "uptrend name"),
        "VIPS": (True, "below its 200dma"),
        "HD": (True, "below its 200dma"),
        "VIK": (True, "-6.6% vs 20d SMA breakdown"),
        "IWM": (False, "uptrend name"),
        "KORU": (True, "below its 200dma"),
        "STE": (True, "below its 200dma"),
    }
    verdicts = {
        "LYV": ("declined", "thesis real but options flow neutral"),
        "VIPS": ("declined", "RSI 27 deeply oversold"),
        "HD": ("declined", "RSI 31 oversold"),
        "VIK": ("declined", "bearish flow on tiny volume"),
    }
    o = _funnel_orch(eligibility, verdicts)
    o._reconcile_bear_verdicts([])  # real path: KORU/STE owe a verdict, get none
    assert o._bear_verdict_stage["KORU"] == "IGNORED"
    assert o._bear_verdict_stage["STE"] == "IGNORED"
    line = _funnel_line(o, bear_map, caplog)
    # Both 7th/8th-ranked names render with the literal handle.
    assert "KORU -0.80 ELIGIBLE (below its 200dma) -> IGNORED" in line
    assert "STE -0.77 ELIGIBLE (below its 200dma) -> IGNORED" in line
    assert line.count("-> IGNORED") == 2
    # Every other eligible name still shows its terminal stage, blocked names
    # keep their existing rendering, and the counter sits in the tail beside
    # put_proposals/put_approved.
    assert "LYV -1.65 ELIGIBLE (-5.3% vs 20d SMA breakdown) -> declined: thesis real" in line
    assert "VIPS -1.27 ELIGIBLE (below its 200dma) -> declined: RSI 27" in line
    assert "HD -1.22 ELIGIBLE (below its 200dma) -> declined: RSI 31" in line
    assert "VIK -1.11 ELIGIBLE (-6.6% vs 20d SMA breakdown) -> declined: bearish flow" in line
    assert "CLH -1.57 gate-blocked" in line and "IWM -1.09 gate-blocked" in line
    assert "slate_bearish=8 [" in line
    assert "put_proposals=0 put_approved=0 ignored=2 auto_hedge=off proxy_put=none" in line
    assert "more gate-blocked" not in line  # nothing was cut


def test_funnel_line_eligible_without_reconciled_stage_renders_ignored(caplog):
    # Defensive: an eligible name that never got a reconciled stage must still
    # carry the handle — the schema owes a verdict per eligible name, so "no
    # stage" IS ignored. Before, it rendered "ELIGIBLE (why)" with no arrow.
    o = _funnel_orch({"GLUE": (True, "below its 200dma")}, {}, stages={})
    line = _funnel_line(o, {"GLUE": -0.9}, caplog)
    assert "GLUE -0.90 ELIGIBLE (below its 200dma) -> IGNORED" in line
    assert "ignored=1" in line


def test_funnel_line_zero_ignored_when_every_eligible_name_is_addressed(caplog):
    o = _funnel_orch(
        {"AAA": (True, "below its 200dma"), "BBB": (True, "risk-off regime"),
         "CCC": (False, "uptrend name")},
        {"AAA": ("declined", "no bearish flow confirm")},
    )
    o._reconcile_bear_verdicts([_put_proposal("BBB")])
    line = _funnel_line(o, {"AAA": -1.0, "BBB": -0.8, "CCC": -0.6}, caplog)
    assert "AAA -1.00 ELIGIBLE (below its 200dma) -> declined: no bearish flow confirm" in line
    assert "BBB -0.80 ELIGIBLE (risk-off regime) -> put proposed" in line
    assert "CCC -0.60 gate-blocked" in line
    assert "-> IGNORED" not in line and "ignored=0" in line


def test_funnel_line_caps_blocked_names_but_never_eligible_ones(caplog):
    # 10 blocked names all outrank the single eligible one: the eligible name
    # still renders (it is the only stage that matters), blocked names cap at
    # the top 6 by score with a visible '+N more' tail, off-slate keeps its
    # label, and reason text is clipped to 60 chars so the line stays bounded.
    long_why = "y" * 100
    bear_map = {f"B{i:02d}": -2.0 + i * 0.05 for i in range(10)}
    bear_map["ELIG"] = -0.5
    eligibility = {s: (False, "uptrend name") for s in bear_map if s != "ELIG"}
    del eligibility["B03"]  # off-slate: no precheck verdict at all
    eligibility["ELIG"] = (True, long_why)
    o = _funnel_orch(eligibility, {}, stages={"ELIG": "IGNORED"})
    line = _funnel_line(o, bear_map, caplog)
    for i in range(6):
        assert f"B{i:02d} " in line
    for i in range(6, 10):
        assert f"B{i:02d} " not in line
    assert "B03 -1.85 off-slate" in line
    assert "…+4 more gate-blocked/off-slate" in line
    assert f"ELIG -0.50 ELIGIBLE ({'y' * 60}) -> IGNORED" in line
    assert "y" * 61 not in line
    assert "slate_bearish=11 [" in line and "ignored=1" in line


def test_funnel_line_empty_map_keeps_bare_counts(caplog):
    o = _funnel_orch({}, {}, stages={})
    line = _funnel_line(o, {}, caplog)
    assert line.startswith("BEARISH FUNNEL: slate_bearish=0 put_proposals=0 "
                           "put_approved=0 ignored=0 auto_hedge=off proxy_put=none")
    assert "[" not in line


# --------------------------------------------------------------------------- #
# Per-NAME falling read
# --------------------------------------------------------------------------- #
def _nf_orch(thresh: float = 4.0):
    o = Orchestrator.__new__(Orchestrator)
    o.cfg = SimpleNamespace(
        name_drop_defense_pct=thresh,
        risk=SimpleNamespace(regime_filter_enabled=False),
        core_etf="QQQ", hedge_etf="PSQ", defensive_core_etf="SGOV",
    )
    return o


def test_name_falling_flags_breaking_held_name():
    o = _nf_orch()
    acct = _account(positions=[_held("NOK", price=4.50, entry=5.00)])
    out = o._name_falling_reads(acct, {"NOK": {"prev_close": 4.75}})
    assert "NOK" in out and "-5.3% today" in out["NOK"]


def test_name_falling_ignores_small_dips_and_missing_tech():
    o = _nf_orch()
    acct = _account(positions=[
        _held("AAA", price=98.0, entry=100.0),   # -2% day vs prev_close 100
        _held("BBB", price=90.0, entry=100.0),   # broken, but no tech data
    ])
    out = o._name_falling_reads(acct, {"AAA": {"prev_close": 100.0}})
    assert out == {}  # AAA under threshold; BBB fails open without tech


def test_name_falling_skips_system_managed_sleeves():
    o = _nf_orch()
    acct = _account(positions=[_held("QQQ", price=470.0, entry=500.0)])
    out = o._name_falling_reads(acct, {"QQQ": {"prev_close": 500.0}})
    assert out == {}  # the core's defense is the orchestrator's own job


def test_name_falling_off_switch():
    o = _nf_orch(thresh=0.0)
    acct = _account(positions=[_held("NOK", price=4.50, entry=5.00)])
    assert o._name_falling_reads(acct, {"NOK": {"prev_close": 5.00}}) == {}


def test_rotation_guard_releases_on_name_falling_green_day():
    # Green book day (red-day release OFF to isolate the new path): the
    # name-level read alone must free the loss-cut. Jul 29 NOK fixture.
    o = _rot_orch(red_day_release=False)
    o.state.register_buy("LOSER", conviction=0.5)
    o._falling_names = {"LOSER": "-5.0% today vs SPY +0.1%"}
    kept = o._apply_rotation_guard(_rot_props(), _rot_acct(False), {})
    assert {p.symbol for p in kept} == {"LOSER", "NEW"}


def test_rotation_guard_still_vetoes_without_falling_read():
    o = _rot_orch(red_day_release=False)
    o.state.register_buy("LOSER", conviction=0.5)
    o._falling_names = {}
    kept = o._apply_rotation_guard(_rot_props(), _rot_acct(False), {})
    assert {p.symbol for p in kept} == {"NEW"}


def test_held_note_carries_falling_read():
    o = Orchestrator.__new__(Orchestrator)
    p = os.path.join(tempfile.gettempdir(), f"_bf_state_{uuid.uuid4().hex}.json")
    o.state = PortfolioState(path=p)
    o.ledger = SimpleNamespace(effective=lambda: [])
    o.journal = SimpleNamespace(today=lambda: [])
    o._system_managed_symbols = lambda: set()
    o._falling_names = {"NOK": "-5.3% today vs SPY +0.1%"}
    acct = _account(positions=[_held("NOK", price=4.50, entry=5.00)])
    notes = o._held_notes(acct)
    assert "NAME FALLING -5.3% today" in notes["NOK"]
    assert "rotation guard" in notes["NOK"]


# --------------------------------------------------------------------------- #
# Config default
# --------------------------------------------------------------------------- #
def test_name_drop_knob_default(monkeypatch):
    monkeypatch.delenv("NAME_DROP_DEFENSE_PCT", raising=False)
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    cfg = load_config()
    assert cfg.name_drop_defense_pct == 4.0
