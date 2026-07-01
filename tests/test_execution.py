"""Tests for order construction from a risk decision (1B.2 fractional protection).

Pure logic, no network: we build an AlpacaClient via __new__ (skipping the
TradingClient/network setup) and stub latest_price + submit, so we can assert
the whole-share-bracket-vs-fractional choice and the "no stop => refuse" guard.

Runnable two ways:
    .venv/bin/python tests/test_execution.py
    .venv/bin/pytest tests/
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.execution.alpaca_client import AlpacaClient
from investment_strategy.models import (
    Action,
    RiskDecision,
    RiskVerdict,
    TradeProposal,
)


def _client(fractional_enabled=True, min_order_usd=1.0, price=100.0):
    """An AlpacaClient with no broker connection; records the OrderRequest it
    would submit so we can inspect whole-share vs fractional / bracket."""
    c = AlpacaClient.__new__(AlpacaClient)
    c.cfg = SimpleNamespace(
        risk=SimpleNamespace(
            fractional_enabled=fractional_enabled, min_order_usd=min_order_usd,
        )
    )
    c.submitted = []
    c.latest_price = lambda symbol: price
    c.submit = lambda order: (c.submitted.append(order) or "oid-1")
    return c


def _decision(qty, notional, stop=5.0, take=12.0, verdict=RiskVerdict.APPROVED):
    prop = TradeProposal(
        symbol="AAPL", action=Action.BUY, conviction=0.7,
        target_weight_pct=5.0, rationale="test",
    )
    return RiskDecision(
        proposal=prop, verdict=verdict, approved_qty=qty,
        approved_notional=notional, stop_loss_pct=stop, take_profit_pct=take,
    )


def test_whole_share_uses_bracket_not_fractional():
    c = _client(price=100.0)
    oid, fractional = c.submit_from_decision(_decision(qty=3.0, notional=300.0))
    assert oid == "oid-1" and fractional is False
    order = c.submitted[0]
    assert order.qty == 3.0 and order.notional is None
    # Bracket legs must be present so the stop/take rest at the exchange.
    assert order.stop_loss_price is not None and order.take_profit_price is not None


def test_sub_share_falls_back_to_fractional_notional():
    c = _client(price=100.0)
    # $40 budget can't buy a whole $100 share -> fractional dollar-notional order.
    oid, fractional = c.submit_from_decision(_decision(qty=0.4, notional=40.0))
    assert oid == "oid-1" and fractional is True
    order = c.submitted[0]
    assert order.notional == 40.0 and order.qty is None
    # Fractional orders carry NO exchange bracket (the watchdog enforces the stop).
    assert order.stop_loss_price is None and order.take_profit_price is None


def test_buy_without_stop_is_refused():
    c = _client(price=100.0)
    # No stop => nothing protects the position; refuse to open it (1B.2b).
    oid, fractional = c.submit_from_decision(_decision(qty=3.0, notional=300.0, stop=0.0))
    assert oid is None and fractional is False
    assert c.submitted == []


def test_sub_share_refused_when_fractional_disabled():
    c = _client(fractional_enabled=False, price=100.0)
    oid, fractional = c.submit_from_decision(_decision(qty=0.4, notional=40.0))
    assert oid is None and fractional is False
    assert c.submitted == []


def test_fractional_below_min_order_refused():
    c = _client(min_order_usd=5.0, price=100.0)
    oid, fractional = c.submit_from_decision(_decision(qty=0.01, notional=1.0))
    assert oid is None and fractional is False


def test_rejected_decision_never_submits():
    c = _client()
    oid, fractional = c.submit_from_decision(
        _decision(qty=3.0, notional=300.0, verdict=RiskVerdict.REJECTED)
    )
    assert oid is None and fractional is False
    assert c.submitted == []


# -- portfolio_basis: brand-new-account edge case --------------------------- #
class _FakeTrading:
    def __init__(self, created_at, hist=None):
        self._created = created_at
        self._hist = hist
        self.history_calls = 0

    def get_account(self):
        return SimpleNamespace(created_at=self._created)

    def get_portfolio_history(self, req):
        self.history_calls += 1
        return self._hist


def _basis_client(trading):
    c = AlpacaClient.__new__(AlpacaClient)
    c.trading = trading
    return c


def test_portfolio_basis_skips_brand_new_account():
    # Account created a few minutes ago -> no 1D bar yet -> skip quietly (None),
    # and DON'T call the history endpoint (which would 400 on start > end).
    trading = _FakeTrading(created_at=datetime.now(timezone.utc) - timedelta(minutes=5))
    c = _basis_client(trading)
    assert c.portfolio_basis() is None
    assert trading.history_calls == 0


def test_portfolio_basis_computes_for_aged_account():
    hist = SimpleNamespace(base_value=1000.0, cashflow={"x": [100.0, 50.0]})
    trading = _FakeTrading(
        created_at=datetime.now(timezone.utc) - timedelta(days=10), hist=hist,
    )
    c = _basis_client(trading)
    base, net_cf = c.portfolio_basis()
    assert base == 1000.0 and net_cf == 150.0
    assert trading.history_calls == 1


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
