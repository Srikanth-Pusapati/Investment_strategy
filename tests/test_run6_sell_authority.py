"""Run-6 item 2 — LLM sell authority becomes events-only.

Under RiskLimits.llm_sell_authority='events_only' (run-6 default) a model
SELL on a LOSING equity position that has not reached its planned stop is
rejected unless the orchestrator attached a deterministic event tag (name-
falling read, earnings in blackout, halt, regime flip into risk-off). Winners,
stop-reached positions and authority='full' behave exactly as before. Every
rejection logs a countable 'SELL AUTHORITY: ...' counterfactual line.

    .venv/bin/python -m pytest tests/test_run6_sell_authority.py
"""
from __future__ import annotations

import logging
import os
import sys
import tempfile
import uuid
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.models import (  # noqa: E402
    AccountSnapshot,
    Action,
    Position,
    RiskVerdict,
    TradeProposal,
)
from investment_strategy.orchestrator import Orchestrator  # noqa: E402
from investment_strategy.risk import RiskManager  # noqa: E402
from investment_strategy.state import PortfolioState  # noqa: E402
from tests.test_risk import _limits  # noqa: E402


def _pos(symbol="LOSER", pl_pct=-3.0):
    return Position(
        symbol=symbol, qty=10.0, avg_entry_price=100.0,
        current_price=100.0 * (1 + pl_pct / 100.0), market_value=1_000.0,
        unrealized_pl=pl_pct * 10.0, unrealized_pl_pct=pl_pct,
    )


def _acct(*positions):
    return AccountSnapshot(
        equity=100_000, last_equity=100_000, cash=50_000, buying_power=50_000,
        positions=list(positions),
    )


def _sell(symbol="LOSER", conviction=0.6):
    return TradeProposal(symbol=symbol, action=Action.SELL,
                         conviction=conviction, target_weight_pct=0.0,
                         rationale="thesis re-argued")


def _rm(authority="events_only"):
    return RiskManager(_limits(llm_sell_authority=authority))


# --------------------------------------------------------------------------- #
# Risk layer gate
# --------------------------------------------------------------------------- #
def test_loser_without_event_is_rejected_with_countable_line(caplog):
    rm = _rm()
    with caplog.at_level(logging.WARNING, logger="risk"):
        d = rm.evaluate(_sell(), _acct(_pos(pl_pct=-3.2)), price=96.8,
                        sell_events=(), stop_width_pct=6.0)
    assert d.verdict is RiskVerdict.REJECTED
    expected = ("SELL AUTHORITY: LOSER decision-sell rejected (unrealized "
                "-3.2%, stop -6.0%, no event) — held to the mechanical stack")
    assert d.reason == expected
    assert any(expected in r.getMessage() for r in caplog.records)


def test_loser_unknown_stop_is_still_rejected():
    d = _rm().evaluate(_sell(), _acct(_pos(pl_pct=-1.0)), price=99.0,
                       sell_events=None, stop_width_pct=None)
    assert d.verdict is RiskVerdict.REJECTED
    assert "stop n/a, no event" in d.reason


def test_loser_with_name_falling_event_is_allowed():
    d = _rm().evaluate(_sell(), _acct(_pos(pl_pct=-3.2)), price=96.8,
                       sell_events=("name_falling:-5.1% on the day",),
                       stop_width_pct=6.0)
    assert d.verdict is RiskVerdict.APPROVED
    assert d.approved_qty == 10.0


def test_loser_at_or_beyond_stop_is_allowed():
    d = _rm().evaluate(_sell(), _acct(_pos(pl_pct=-6.5)), price=93.5,
                       sell_events=(), stop_width_pct=6.0)
    assert d.verdict is RiskVerdict.APPROVED


def test_winner_is_allowed_without_event():
    d = _rm().evaluate(_sell("WIN"), _acct(_pos("WIN", pl_pct=4.0)),
                       price=104.0, sell_events=(), stop_width_pct=6.0)
    assert d.verdict is RiskVerdict.APPROVED
    # flat position (unrealized == 0) is not a loser either
    d0 = _rm().evaluate(_sell("FLAT"), _acct(_pos("FLAT", pl_pct=0.0)),
                        price=100.0, sell_events=(), stop_width_pct=6.0)
    assert d0.verdict is RiskVerdict.APPROVED


def test_authority_full_keeps_legacy_behaviour():
    d = _rm("full").evaluate(_sell(), _acct(_pos(pl_pct=-3.2)), price=96.8,
                             sell_events=(), stop_width_pct=6.0)
    assert d.verdict is RiskVerdict.APPROVED
    assert d.reason == "Closing existing position."


def test_legacy_call_without_kwargs_defaults_to_gate():
    # Old call shape (no sell_events/stop_width_pct) still works; under the
    # run-6 default it gates a loser, under 'full' it does not.
    assert _rm().evaluate(_sell(), _acct(_pos(pl_pct=-2.0)), price=98.0
                          ).verdict is RiskVerdict.REJECTED
    assert _rm("full").evaluate(_sell(), _acct(_pos(pl_pct=-2.0)), price=98.0
                                ).verdict is RiskVerdict.APPROVED


# --------------------------------------------------------------------------- #
# Orchestrator event tags (from code, never the model)
# --------------------------------------------------------------------------- #
def _orch(authority="events_only", falling=None, dte=None, halted=False,
          flipped=False, blackout=3):
    o = Orchestrator.__new__(Orchestrator)
    o.cfg = SimpleNamespace(risk=SimpleNamespace(
        llm_sell_authority=authority, earnings_blackout_days=blackout,
        rotation_loss_guard_enabled=True, rotation_guard_min_loss_pct=4.0,
        rotation_min_conviction_edge=0.10, rotation_require_composite_edge=False,
        rotation_guard_exempt_sell_conviction=0.65,
        rotation_guard_max_loss_pct=0.0, rotation_guard_repeat_release_pct=0.0,
        rotation_guard_red_day_release=True,
    ))
    p = os.path.join(tempfile.gettempdir(), f"_sa_{uuid.uuid4().hex}.json")
    o.state = PortfolioState(path=p)
    o.ledger = SimpleNamespace(effective=lambda: [])
    o.journal_records = []
    o.journal = SimpleNamespace(record=o.journal_records.append)
    o._rotation_vetoes = {}
    o._falling_names = dict(falling or {})
    o.earnings = SimpleNamespace(days_until_earnings=lambda s: dte)
    o.risk = SimpleNamespace(
        kill_switch=False,
        trading_halted=lambda a: (halted, "HALT LATCH set: test" if halted else ""),
    )
    o._regime_flipped_off = flipped
    return o


def test_event_tags_empty_when_nothing_fired():
    assert _orch()._sell_event_tags("LOSER", _acct()) == ()


def test_event_tags_name_falling_earnings_halt_regime():
    o = _orch(falling={"LOSER": "-5.1% on the day"}, dte=2, halted=True,
              flipped=True)
    tags = o._sell_event_tags("LOSER", _acct())
    assert tags == (
        "name_falling:-5.1% on the day", "earnings:2d",
        "halt:HALT LATCH set: test", "regime_flip:risk-off",
    )
    # earnings outside the blackout window is not an event
    assert _orch(dte=10)._sell_event_tags("LOSER", _acct()) == ()
    # a falling read for ANOTHER name does not license this one
    assert _orch(falling={"OTHER": "x"})._sell_event_tags("LOSER", _acct()) == ()


def test_event_tag_reads_fail_closed():
    o = _orch()
    o.earnings = SimpleNamespace(days_until_earnings=lambda s: 1 / 0)
    o.risk = SimpleNamespace(kill_switch=False,
                             trading_halted=lambda a: 1 / 0)
    assert o._sell_event_tags("LOSER", _acct()) == ()


# --------------------------------------------------------------------------- #
# Rotation guard under events_only
# --------------------------------------------------------------------------- #
def _rot_props():
    return [
        TradeProposal(symbol="LOSER", action=Action.SELL, conviction=0.5,
                      target_weight_pct=5.0, rationale="t"),
        TradeProposal(symbol="NEW", action=Action.BUY, conviction=0.55,
                      target_weight_pct=5.0, rationale="t"),
    ]


def test_rotation_guard_passes_non_event_loser_to_the_risk_layer():
    # Legacy: this sell is vetoed (no conviction edge). Under events_only the
    # release ladder is moot — the sell is handed on so the risk layer logs
    # the single countable SELL AUTHORITY rejection instead of a veto.
    o = _orch()
    o.state.register_buy("LOSER", conviction=0.5)
    acct = _acct(_pos("LOSER", pl_pct=-10.0), _pos("KEEP", pl_pct=5.0))
    kept = o._apply_rotation_guard(_rot_props(), acct, {})
    assert [p.symbol for p in kept] == ["LOSER", "NEW"]
    assert o.journal_records == []
    # and the risk layer then rejects it (no event, stop not reached)
    d = _rm().evaluate(kept[0], acct, price=90.0, sell_events=(),
                       stop_width_pct=12.0)
    assert d.verdict is RiskVerdict.REJECTED


def test_rotation_guard_full_authority_unchanged():
    o = _orch(authority="full")
    o.state.register_buy("LOSER", conviction=0.5)
    acct = _acct(_pos("LOSER", pl_pct=-10.0), _pos("KEEP", pl_pct=5.0))
    kept = o._apply_rotation_guard(_rot_props(), acct, {})
    assert [p.symbol for p in kept] == ["NEW"]
    assert o.journal_records[0].verdict == "rotation_guard"


def test_rotation_guard_event_tagged_loser_takes_the_release_path():
    o = _orch(falling={"LOSER": "-5.1% on the day"})
    o.state.register_buy("LOSER", conviction=0.5)
    acct = _acct(_pos("LOSER", pl_pct=-10.0), _pos("KEEP", pl_pct=5.0))
    kept = o._apply_rotation_guard(_rot_props(), acct, {})
    assert [p.symbol for p in kept] == ["LOSER", "NEW"]
    d = _rm().evaluate(kept[0], acct, price=90.0,
                       sell_events=o._sell_event_tags("LOSER", acct),
                       stop_width_pct=12.0)
    assert d.verdict is RiskVerdict.APPROVED


# --------------------------------------------------------------------------- #
# Config + prompt
# --------------------------------------------------------------------------- #
def test_config_default_and_env_override(monkeypatch):
    from investment_strategy.config import load_config
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    monkeypatch.delenv("LLM_SELL_AUTHORITY", raising=False)
    assert load_config().risk.llm_sell_authority == "events_only"
    monkeypatch.setenv("LLM_SELL_AUTHORITY", " Full ")
    assert load_config().risk.llm_sell_authority == "full"


def test_prompt_says_losers_are_mechanically_managed():
    from investment_strategy.decision.prompts import SYSTEM_PROMPT
    assert "holding is a DECISION you re-make each cycle" not in SYSTEM_PROMPT
    assert "propose the SELL and salvage" not in SYSTEM_PROMPT
    assert "LOSING positions are managed by the mechanical stops" in SYSTEM_PROMPT
    assert "name such a concrete NEW event" in SYSTEM_PROMPT
    # neighbouring blocks intact
    assert "- ROTATION:" in SYSTEM_PROMPT
    assert "- conviction (0..1)" in SYSTEM_PROMPT
