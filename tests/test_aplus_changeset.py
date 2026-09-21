"""Tests for the Sep 21 2026 A+ change-set (items A-1 .. A-9).

Each block names the item it covers and the run-6 incident that motivated it.
Pure logic / fakes only — no network, no broker."""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.ledger import TradeRecord
from investment_strategy.models import Position
from investment_strategy.monitor.watchdog import Watchdog
from investment_strategy.state import PortfolioState

_spec = importlib.util.spec_from_file_location(
    "eval_contract_check",
    Path(__file__).resolve().parents[1] / "scripts" / "eval_contract_check.py",
)
ecc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ecc)


# --------------------------------------------------------------------------- #
# A-5 — the event sanction rides on decision-sell ledger rows; v3 rule 8 counts
# only UNSANCTIONED decision-sell losses (run-6 NO-GO on INTC Sep 14, 0.49x).
# --------------------------------------------------------------------------- #
class _Ledger:
    def __init__(self):
        self.rows: list[TradeRecord] = []

    def record(self, rec):
        self.rows.append(rec)


def _state() -> PortfolioState:
    p = os.path.join(tempfile.gettempdir(), f"_aplus_{uuid.uuid4().hex}.json")
    return PortfolioState(path=p)


def _wd_cfg():
    return SimpleNamespace(
        risk=SimpleNamespace(
            equity_floor_pct=0.0, max_daily_loss_pct=3.0, max_hold_days=0.0,
            time_stop_min_gain_pct=2.0, scale_out_enabled=False,
            scale_out_pct=50.0, whole_shares_only=False,
        ),
        state_file="state/risk_state.json", monitor_interval_s=30,
    )


def _pos(symbol="INTC"):
    return Position(symbol=symbol, qty=503.0, avg_entry_price=101.7,
                    current_price=97.28, market_value=48931.84,
                    unrealized_pl=-2223.26, unrealized_pl_pct=-4.35)


def test_a5_for_sell_carries_the_sanction_and_defaults_empty():
    ev = ["name_falling:-5.6% today vs SPY -0.7%"]
    rec = TradeRecord.for_sell("INTC", "loss-cut", "oid-1", qty=503,
                               realized_pl_pct=-4.33, realized_pl=-2216.87,
                               exit_reason="decision", sell_events=ev)
    assert rec.sell_events == ev
    plain = TradeRecord.for_sell("NOK", "watchdog stop", "oid-2", qty=10,
                                 exit_reason="stop")
    assert plain.sell_events == []


def test_a5_watchdog_record_exit_uses_the_note_for_decision_only():
    led = _Ledger()
    wd = Watchdog(_wd_cfg(), SimpleNamespace(), state=_state(), ledger=led)
    wd.note_sell_events("INTC", ["name_falling:-5.6% today vs SPY -0.7%"])
    wd._record_exit(_pos("INTC"), None, "decision")
    wd._record_exit(_pos("INTC"), None, "stop")
    assert led.rows[0].sell_events == ["name_falling:-5.6% today vs SPY -0.7%"]
    assert led.rows[1].sell_events == []          # mechanical exit: never tagged
    # An empty note clears a stale sanction (a stop-reached decision sell).
    wd.note_sell_events("INTC", [])
    wd._record_exit(_pos("INTC"), None, "decision")
    assert led.rows[2].sell_events == []


def test_a5_queue_decision_sell_persists_the_sanction():
    st = _state()
    st.queue_decision_sell("RIG", "loss-cut", ["technical"], 1.8,
                           sell_events=["name_falling:-5.2% today vs SPY +0.5%"])
    info = st.get_pending_decision_sells()["RIG"]
    assert info["sell_events"] == ["name_falling:-5.2% today vs SPY +0.5%"]
    # Legacy 4-arg call still works and stores an empty list.
    st.queue_decision_sell("NOK", "x", [], None)
    assert st.get_pending_decision_sells()["NOK"]["sell_events"] == []


_ROWS = [
    {"ts": "2026-09-10T14:00:00Z", "symbol": "INTC", "action": "buy",
     "stop_loss_pct": 8.91},
    {"ts": "2026-09-14T14:25:43Z", "symbol": "INTC", "action": "sell",
     "exit_reason": "decision", "realized_pl_pct": -4.33, "realized_pl": -2216.87,
     "sell_events": ["name_falling:-5.6% today vs SPY -0.7%"]},
    {"ts": "2026-09-10T14:00:00Z", "symbol": "ZZZ", "action": "buy",
     "stop_loss_pct": 8.0},
    {"ts": "2026-09-14T15:00:00Z", "symbol": "ZZZ", "action": "sell",
     "exit_reason": "decision", "realized_pl_pct": -1.0, "realized_pl": -100.0},
]


def test_a5_checker_v3_counts_only_unsanctioned_rows():
    aware = ecc.decision_sell_losses_below_stop(
        _ROWS, "2026-09-01", "2026-09-30", 0.5, sanction_aware=True)
    assert aware["count"] == 1 and aware["rows"][0][1] == "ZZZ"
    assert [r[1] for r in aware["sanctioned_rows"]] == ["INTC"]
    assert aware["n_decision_losses"] == 2


def test_a5_checker_v2_reading_is_unchanged():
    legacy = ecc.decision_sell_losses_below_stop(
        _ROWS, "2026-09-01", "2026-09-30", 0.5)
    assert legacy["count"] == 2                   # as written: both rows count
    assert legacy["sanctioned_rows"] == []
