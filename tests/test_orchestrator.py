"""Tests for the intra-cycle running-tally over-deploy fix (1B.3).

The account is fetched once per cycle; these static helpers fold each fill back
into that snapshot so later proposals in the SAME cycle see capital as deployed
(closing the no-leverage / cash-buffer / max-positions hole on a margin account).
Pure state mutation on an AccountSnapshot — no broker, no network.

Runnable two ways:
    .venv/bin/python tests/test_orchestrator.py
    .venv/bin/pytest tests/
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.models import AccountSnapshot, Position
from investment_strategy.orchestrator import Orchestrator


def _acct(cash=1_000.0, positions=None):
    return AccountSnapshot(
        equity=1_000.0, last_equity=1_000.0, cash=cash, buying_power=2_000.0,
        positions=positions or [],
    )


def _pos(symbol, mv):
    return Position(symbol=symbol, qty=1.0, avg_entry_price=mv, current_price=mv,
                    market_value=mv, unrealized_pl=0.0, unrealized_pl_pct=0.0)


def test_pending_buy_adds_new_position_and_draws_down_cash():
    acct = _acct(cash=1_000.0)
    Orchestrator._apply_pending_buy(acct, "AAPL", notional=300.0, price=100.0, qty=3.0)
    assert acct.position_for("AAPL").market_value == 300.0
    assert acct.cash == 700.0
    assert acct.buying_power == 1_700.0
    # A SECOND buy this cycle now sees the first as deployed (gross grows).
    Orchestrator._apply_pending_buy(acct, "MSFT", notional=400.0, price=200.0, qty=2.0)
    gross = sum(p.market_value for p in acct.positions)
    assert gross == 700.0
    assert acct.cash == 300.0


def test_pending_buy_extends_existing_position():
    acct = _acct(cash=1_000.0, positions=[_pos("AAPL", 100.0)])
    Orchestrator._apply_pending_buy(acct, "AAPL", notional=150.0, price=100.0, qty=1.5)
    assert len(acct.positions) == 1
    assert acct.position_for("AAPL").market_value == 250.0
    assert acct.cash == 850.0


def test_pending_close_frees_capital_and_slot():
    acct = _acct(cash=100.0, positions=[_pos("AAPL", 300.0), _pos("MSFT", 200.0)])
    Orchestrator._apply_pending_close(acct, "AAPL")
    assert acct.position_for("AAPL") is None
    assert len(acct.positions) == 1        # slot freed for a later buy this cycle
    assert acct.cash == 400.0              # 100 + 300 market value returned
    assert acct.buying_power == 2_300.0


def test_pending_close_noop_when_not_held():
    acct = _acct(cash=100.0, positions=[_pos("AAPL", 300.0)])
    Orchestrator._apply_pending_close(acct, "TSLA")  # not held
    assert acct.cash == 100.0
    assert len(acct.positions) == 1


def test_cash_never_goes_negative():
    acct = _acct(cash=50.0)
    Orchestrator._apply_pending_buy(acct, "AAPL", notional=300.0, price=100.0, qty=3.0)
    assert acct.cash == 0.0                # clamped, not negative
    assert acct.buying_power == 1_700.0


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
