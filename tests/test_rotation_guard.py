"""Tests for the rotation loss guard + the composite budget blend (orchestrator).

Week of 2026-07-13: UNH (-$204) and HUBB (-$158) were sold purely to free a
slot under the position cap — the prompt ASKS for a +0.10 conviction edge on
rotations but nothing enforced it. The guard vetoes a cap-forced SELL that
locks in a real loss unless the best incoming new-name BUY clears the
incumbent's ENTRY conviction by the configured edge. Detection is structural
(cap reached + paired new-name buy), never rationale-text parsing, so watchdog
stops and standalone risk-off sells can't be blocked.

Runnable two ways:
    .venv/bin/python -m pytest tests/test_rotation_guard.py
    .venv/bin/pytest tests/
"""
from __future__ import annotations

import os
import sys
import tempfile
import uuid
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.models import (
    AccountSnapshot,
    Action,
    Position,
    TradeProposal,
)
from investment_strategy.orchestrator import Orchestrator
from investment_strategy.state import PortfolioState


def _orch(max_open_positions=2, guard_enabled=True, min_loss=4.0, edge=0.10,
          require_comp=False, blend=True, exempt_sell_conv=0.65,
          ledger_records=None):
    o = Orchestrator.__new__(Orchestrator)
    o.cfg = SimpleNamespace(risk=SimpleNamespace(
        rotation_loss_guard_enabled=guard_enabled,
        rotation_guard_min_loss_pct=min_loss,
        rotation_min_conviction_edge=edge,
        rotation_require_composite_edge=require_comp,
        rotation_guard_exempt_sell_conviction=exempt_sell_conv,
        max_open_positions=max_open_positions,
        composite_budget_blend=blend,
        min_cash_buffer_pct=0.0,
        max_cycle_symbol_share_pct=100.0,
    ))
    p = os.path.join(tempfile.gettempdir(), f"_rot_{uuid.uuid4().hex}.json")
    o.state = PortfolioState(path=p)
    o.ledger = SimpleNamespace(effective=lambda: list(ledger_records or []))
    o.risk = SimpleNamespace(kill_switch=False)
    o.journal_records = []
    o.journal = SimpleNamespace(record=o.journal_records.append)
    return o


def _pos(symbol, pl_pct=0.0):
    return Position(
        symbol=symbol, qty=10.0, avg_entry_price=100.0, current_price=100.0,
        market_value=1_000.0, unrealized_pl=pl_pct * 10.0,
        unrealized_pl_pct=pl_pct,
    )


def _acct(positions, cash=10_000.0):
    return AccountSnapshot(
        equity=100_000, last_equity=100_000, cash=cash, buying_power=cash,
        positions=positions,
    )


def _prop(symbol, action, conviction=0.5):
    return TradeProposal(symbol=symbol, action=action, conviction=conviction,
                         target_weight_pct=5.0, rationale="test")


def _full_book_setup(o, entry_conv=0.5):
    """Two-slot book, both held, LOSER at -10%; entry conviction recorded."""
    o.state.register_buy("LOSER", conviction=entry_conv)
    return _acct([_pos("LOSER", pl_pct=-10.0), _pos("KEEP", pl_pct=5.0)])


def test_vetoes_loss_sell_without_conviction_edge():
    o = _orch()
    acct = _full_book_setup(o, entry_conv=0.5)
    props = [_prop("LOSER", Action.SELL, 0.5), _prop("NEW", Action.BUY, 0.55)]
    kept = o._apply_rotation_guard(props, acct, {})
    assert [p.symbol for p in kept] == ["NEW"]   # sell vetoed, buy passes on
    assert o.journal_records and o.journal_records[0].verdict == "rotation_guard"


def test_allows_rotation_with_clear_edge():
    o = _orch()
    acct = _full_book_setup(o, entry_conv=0.5)
    props = [_prop("LOSER", Action.SELL, 0.5), _prop("NEW", Action.BUY, 0.65)]
    kept = o._apply_rotation_guard(props, acct, {})
    assert [p.symbol for p in kept] == ["LOSER", "NEW"]


def test_fires_regardless_of_free_slots():
    # Inverted from the old behavior: the guard used to bail out when the book
    # had free slots (positions < MAX_OPEN), which meant it NEVER fired — Jul 17
    # was at 13/15 slots and the SPCX/MU loss-rotations sailed through. A
    # loss-locking sell frees CAPITAL for the paired buy no matter how many
    # slots are open, so the edge must be enforced here too.
    o = _orch(max_open_positions=5)               # 3 slots free, book of 2
    acct = _full_book_setup(o)                    # LOSER at -10%, entry conv 0.5
    props = [_prop("LOSER", Action.SELL, 0.5), _prop("NEW", Action.BUY, 0.5)]
    kept = o._apply_rotation_guard(props, acct, {})
    assert [p.symbol for p in kept] == ["NEW"]    # weak buy -> loss-sell vetoed
    # A clear edge still passes even with free slots.
    strong = [_prop("LOSER", Action.SELL, 0.5), _prop("NEW", Action.BUY, 0.65)]
    assert o._apply_rotation_guard(strong, acct, {}) == strong


def test_halt_disables_guard_so_standalone_sell_is_never_pinned():
    # Under the kill switch, buys don't execute — the paired "buy" funds no
    # rotation, so the loss-sell must pass (never pin an exit when there's
    # nothing to rotate INTO).
    o = _orch()
    o.risk.kill_switch = True
    acct = _full_book_setup(o)
    props = [_prop("LOSER", Action.SELL, 0.5), _prop("NEW", Action.BUY, 0.5)]
    assert o._apply_rotation_guard(props, acct, {}) == props


def test_inert_without_paired_new_name_buy():
    # A standalone risk-off sell (no incoming name) must NEVER be blocked.
    o = _orch()
    acct = _full_book_setup(o)
    props = [_prop("LOSER", Action.SELL, 0.5)]
    assert o._apply_rotation_guard(props, acct, {}) == props
    # A top-up of a HELD name doesn't consume the freed slot either.
    props2 = [_prop("LOSER", Action.SELL, 0.5), _prop("KEEP", Action.BUY, 0.9)]
    assert o._apply_rotation_guard(props2, acct, {}) == props2


def test_small_loss_and_winner_sells_pass():
    o = _orch()   # min_loss 4.0
    o.state.register_buy("DOWN3", conviction=0.5)
    o.state.register_buy("UP", conviction=0.5)
    acct = _acct([_pos("DOWN3", pl_pct=-3.0), _pos("UP", pl_pct=8.0)])
    props = [
        _prop("DOWN3", Action.SELL, 0.5), _prop("UP", Action.SELL, 0.5),
        _prop("NEW", Action.BUY, 0.5),
    ]
    assert o._apply_rotation_guard(props, acct, {}) == props


def test_fails_open_without_recorded_entry_conviction():
    o = _orch()
    acct = _acct([_pos("LOSER", pl_pct=-10.0), _pos("KEEP", pl_pct=5.0)])
    props = [_prop("LOSER", Action.SELL, 0.5), _prop("NEW", Action.BUY, 0.5)]
    assert o._apply_rotation_guard(props, acct, {}) == props


def test_entry_conviction_falls_back_to_ledger_when_state_pruned():
    # The state store rides a 7-DAY churn retention: a never-topped-up name
    # held past a week (exactly the stale incumbent rotations target) has NO
    # state entry. The ledger keeps every buy's conviction forever — the
    # guard must still find the baseline there and enforce the edge.
    from investment_strategy.ledger import TradeRecord
    led = [
        TradeRecord(symbol="LOSER", action="buy", qty=10.0, conviction=0.5),
        TradeRecord(symbol="LOSER", action="buy", qty=5.0, conviction=0.55),
        TradeRecord(symbol="OTHER", action="buy", qty=1.0, conviction=0.9),
    ]
    o = _orch(ledger_records=led)   # note: state has NO conviction for LOSER
    acct = _acct([_pos("LOSER", pl_pct=-10.0), _pos("KEEP", pl_pct=5.0)])
    assert o._entry_conviction("LOSER") == 0.55   # latest buy wins
    props = [_prop("LOSER", Action.SELL, 0.5), _prop("NEW", Action.BUY, 0.6)]
    kept = o._apply_rotation_guard(props, acct, {})
    assert [p.symbol for p in kept] == ["NEW"]    # 0.6 < 0.55+0.10 -> vetoed
    strong = [_prop("LOSER", Action.SELL, 0.5), _prop("NEW", Action.BUY, 0.65)]
    assert o._apply_rotation_guard(strong, acct, {}) == strong


def test_entry_conviction_ignores_unrecorded_zero_conviction_rows():
    from investment_strategy.ledger import TradeRecord
    led = [TradeRecord(symbol="LOSER", action="buy", qty=1.0, conviction=0.0)]
    o = _orch(ledger_records=led)
    assert o._entry_conviction("LOSER") is None   # 0.0 = not recorded


def test_high_conviction_sell_is_exempt_risk_off_exit():
    # Thesis-broken exit (SELL conviction 0.9) + an UNRELATED weak new-name
    # buy in the same response must NOT be reclassified as a rotation and
    # vetoed — never block a legitimate exit.
    o = _orch()
    acct = _full_book_setup(o, entry_conv=0.8)
    props = [_prop("LOSER", Action.SELL, 0.9), _prop("NEW", Action.BUY, 0.5)]
    assert o._apply_rotation_guard(props, acct, {}) == props
    # Same shape but a lukewarm slot-freeing sell -> still vetoed.
    weak = [_prop("LOSER", Action.SELL, 0.5), _prop("NEW", Action.BUY, 0.5)]
    kept = o._apply_rotation_guard(weak, acct, {})
    assert [p.symbol for p in kept] == ["NEW"]


def test_exempt_sell_conviction_off_at_zero():
    o = _orch(exempt_sell_conv=0.0)
    acct = _full_book_setup(o, entry_conv=0.8)
    props = [_prop("LOSER", Action.SELL, 0.9), _prop("NEW", Action.BUY, 0.5)]
    kept = o._apply_rotation_guard(props, acct, {})
    assert [p.symbol for p in kept] == ["NEW"]    # exemption disabled -> vetoed


def test_composite_edge_optional_leg():
    o = _orch(require_comp=True)
    acct = _full_book_setup(o, entry_conv=0.5)
    props = [_prop("LOSER", Action.SELL, 0.5), _prop("NEW", Action.BUY, 0.65)]
    # Conviction edge OK but incoming composite is WEAKER -> vetoed.
    kept = o._apply_rotation_guard(props, acct, {"LOSER": 0.8, "NEW": 0.2})
    assert [p.symbol for p in kept] == ["NEW"]
    # Composite edge present -> passes.
    kept = o._apply_rotation_guard(props, acct, {"LOSER": 0.2, "NEW": 0.8})
    assert [p.symbol for p in kept] == ["LOSER", "NEW"]


def test_guard_off_by_knob():
    o = _orch(guard_enabled=False)
    acct = _full_book_setup(o, entry_conv=0.5)
    props = [_prop("LOSER", Action.SELL, 0.5), _prop("NEW", Action.BUY, 0.5)]
    assert o._apply_rotation_guard(props, acct, {}) == props


# -- composite budget blend ---------------------------------------------------- #
def test_budget_blend_tilts_toward_corroborated_buys():
    o = _orch(blend=True)
    acct = _acct([], cash=10_000.0)
    props = [_prop("A", Action.BUY, 0.5), _prop("B", Action.BUY, 0.5)]
    caps = o._cycle_budget_caps(props, acct, {"A": 1.0, "B": 0.25})
    assert abs(caps["A"] - 8_000.0) < 1e-6   # 0.5*1.0 vs 0.5*0.25 -> 80/20
    assert abs(caps["B"] - 2_000.0) < 1e-6


def test_budget_blend_floors_weak_composites():
    # A composite at/below 0 shrinks a share to the 0.1 floor, never zeroes it.
    o = _orch(blend=True)
    acct = _acct([], cash=11_000.0)
    props = [_prop("A", Action.BUY, 0.5), _prop("B", Action.BUY, 0.5)]
    caps = o._cycle_budget_caps(props, acct, {"A": 1.0, "B": -2.0})
    assert abs(caps["A"] - 10_000.0) < 1e-6  # 0.5 vs 0.05 -> 10/11 and 1/11
    assert abs(caps["B"] - 1_000.0) < 1e-6


def test_budget_blend_off_keeps_conviction_split():
    o = _orch(blend=False)
    acct = _acct([], cash=10_000.0)
    props = [_prop("A", Action.BUY, 0.5), _prop("B", Action.BUY, 0.5)]
    caps = o._cycle_budget_caps(props, acct, {"A": 1.0, "B": 0.25})
    assert abs(caps["A"] - caps["B"]) < 1e-6
