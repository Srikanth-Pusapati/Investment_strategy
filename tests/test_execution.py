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


def _client(fractional_enabled=True, min_order_usd=1.0, price=100.0,
            whole_shares_only=False):
    """An AlpacaClient with no broker connection; records the OrderRequest it
    would submit so we can inspect whole-share vs fractional / bracket."""
    c = AlpacaClient.__new__(AlpacaClient)
    c.cfg = SimpleNamespace(
        risk=SimpleNamespace(
            fractional_enabled=fractional_enabled, min_order_usd=min_order_usd,
            whole_shares_only=whole_shares_only,
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


def test_sub_share_refused_in_whole_shares_mode_even_with_fractional_on():
    # GA-2.3 belt-and-suspenders: the risk layer floors/rejects upstream, but a
    # decision built any other way must not slip an unbracketed buy through.
    c = _client(fractional_enabled=True, whole_shares_only=True, price=100.0)
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


# -- order_fill: status normalization ---------------------------------------- #
class _FakeOrderTrading:
    def __init__(self, order=None, err=None):
        self._order = order
        self._err = err

    def get_order_by_id(self, oid):
        if self._err:
            raise self._err
        return self._order


def test_order_fill_normalizes_status_enum_to_plain_value():
    # alpaca-py returns an OrderStatus enum; str() of it is "OrderStatus.FILLED",
    # which broke _reconcile_fills' comparisons against plain "filled".
    from alpaca.trading.enums import OrderStatus

    c = AlpacaClient.__new__(AlpacaClient)
    c.trading = _FakeOrderTrading(
        order=SimpleNamespace(status=OrderStatus.FILLED, filled_qty="3", qty="3")
    )
    status, filled, qty = c.order_fill("oid-1")
    assert status == "filled"
    assert filled == 3.0 and qty == 3.0


def test_order_fill_unknown_on_fetch_failure():
    c = AlpacaClient.__new__(AlpacaClient)
    c.trading = _FakeOrderTrading(err=RuntimeError("boom"))
    assert c.order_fill("oid-1") == ("unknown", 0.0, 0.0)


# -- closed_sell_orders: raw material for the exchange-exit backfill (F.1) -- #
def _closed_order(oid="o-1", status="filled", otype="stop", qty="10",
                  price="92.5", filled_at=None):
    return SimpleNamespace(
        id=oid, symbol="AAPL",
        status=SimpleNamespace(value=status),
        order_type=SimpleNamespace(value=otype),
        filled_qty=qty, filled_avg_price=price,
        filled_at=filled_at or datetime(2026, 7, 2, 15, 30, tzinfo=timezone.utc),
    )


class _FakeClosedOrdersTrading:
    def __init__(self, orders=None, err=None):
        self.orders = orders or []
        self.err = err

    def get_orders(self, filter=None):  # noqa: A002 — alpaca-py kwarg name
        if self.err:
            raise self.err
        return self.orders


def test_closed_sell_orders_returns_filled_only_as_plain_dicts():
    c = AlpacaClient.__new__(AlpacaClient)
    c.trading = _FakeClosedOrdersTrading([
        _closed_order("o-1", status="filled", otype="stop"),
        _closed_order("o-2", status="canceled"),   # realized nothing -> dropped
    ])
    out = c.closed_sell_orders()
    assert len(out) == 1
    o = out[0]
    assert o["order_id"] == "o-1" and o["symbol"] == "AAPL"
    assert o["qty"] == 10.0 and o["price"] == 92.5
    assert o["type"] == "stop"
    assert o["filled_at"].startswith("2026-07-02T15:30")


def test_closed_sell_orders_empty_on_failure():
    c = AlpacaClient.__new__(AlpacaClient)
    c.trading = _FakeClosedOrdersTrading(err=RuntimeError("api down"))
    assert c.closed_sell_orders() == []


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
