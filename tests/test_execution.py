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
from unittest.mock import patch

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
    sub = c.submit_from_decision(_decision(qty=3.0, notional=300.0))
    assert sub.order_id == "oid-1" and sub.fractional is False
    order = c.submitted[0]
    assert order.qty == 3.0 and order.notional is None
    # Bracket legs must be present so the stop/take rest at the exchange.
    assert order.stop_loss_price is not None and order.take_profit_price is not None


def test_whole_share_floor_reports_actual_qty_and_notional():
    # The bracket path floors 2.5 sh -> 2 and drops the remainder; the returned
    # submission must carry the FLOORED numbers so the ledger and the capital
    # snapshot record the order, not the intent (the LLY 2.50767-vs-2.0 bug).
    c = _client(price=100.0)
    sub = c.submit_from_decision(_decision(qty=2.5, notional=250.0))
    assert sub.order_id == "oid-1" and sub.fractional is False
    assert sub.qty == 2.0 and sub.notional == 200.0
    assert c.submitted[0].qty == 2.0


def test_sub_share_falls_back_to_fractional_notional():
    c = _client(price=100.0)
    # $40 budget can't buy a whole $100 share -> fractional dollar-notional order.
    sub = c.submit_from_decision(_decision(qty=0.4, notional=40.0))
    assert sub.order_id == "oid-1" and sub.fractional is True
    assert sub.qty == 0.4 and sub.notional == 40.0
    order = c.submitted[0]
    assert order.notional == 40.0 and order.qty is None
    # Fractional orders carry NO exchange bracket (the watchdog enforces the stop).
    assert order.stop_loss_price is None and order.take_profit_price is None


def test_buy_without_stop_is_refused():
    c = _client(price=100.0)
    # No stop => nothing protects the position; refuse to open it (1B.2b).
    sub = c.submit_from_decision(_decision(qty=3.0, notional=300.0, stop=0.0))
    assert sub.order_id is None and sub.fractional is False
    assert c.submitted == []


def test_sub_share_refused_when_fractional_disabled():
    c = _client(fractional_enabled=False, price=100.0)
    sub = c.submit_from_decision(_decision(qty=0.4, notional=40.0))
    assert sub.order_id is None and sub.fractional is False
    assert c.submitted == []


def test_sub_share_refused_in_whole_shares_mode_even_with_fractional_on():
    # GA-2.3 belt-and-suspenders: the risk layer floors/rejects upstream, but a
    # decision built any other way must not slip an unbracketed buy through.
    c = _client(fractional_enabled=True, whole_shares_only=True, price=100.0)
    sub = c.submit_from_decision(_decision(qty=0.4, notional=40.0))
    assert sub.order_id is None and sub.fractional is False
    assert c.submitted == []


def test_fractional_below_min_order_refused():
    c = _client(min_order_usd=5.0, price=100.0)
    sub = c.submit_from_decision(_decision(qty=0.01, notional=1.0))
    assert sub.order_id is None and sub.fractional is False


def test_rejected_decision_never_submits():
    c = _client()
    sub = c.submit_from_decision(
        _decision(qty=3.0, notional=300.0, verdict=RiskVerdict.REJECTED)
    )
    assert sub.order_id is None and sub.fractional is False
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


# -- position mapping (the FRHC wedge root cause) ----------------------------- #
def _sdk_position(**over):
    """The shape alpaca-py returns: qty_available = qty minus shares reserved
    by open orders (strings, like the real SDK)."""
    base = dict(symbol="FRHC", qty="110.000185944", qty_available="1.000185944",
                avg_entry_price="164.33", current_price="144.4",
                market_value="15884.0", unrealized_pl="-2192.0",
                unrealized_plpc="-0.1213")
    base.update(over)
    return SimpleNamespace(**base)


def test_to_position_maps_sdk_qty_available():
    # Regression: this used to read the nonexistent `qty_available_for_trading`,
    # silently defaulting to full qty — the watchdog then never saw locked shares.
    pos = AlpacaClient._to_position(_sdk_position())
    assert pos.qty == 110.000185944
    assert pos.qty_available == 1.000185944


def test_to_position_missing_qty_available_assumes_all_sellable():
    sdk = _sdk_position()
    del sdk.qty_available
    pos = AlpacaClient._to_position(sdk)
    assert pos.qty_available == pos.qty


# -- clear_orders_for_exit (turn resting sells into the exit) ----------------- #
def _open_order(oid, side="sell", status="new", limit_price="205.41", qty="48",
                filled_qty="0", symbol="FRHC", order_type="limit", stop_price=None):
    return SimpleNamespace(id=oid, symbol=symbol, side=side, status=status,
                           limit_price=limit_price, qty=qty, filled_qty=filled_qty,
                           order_type=order_type, stop_price=stop_price)


class _FakeExitTrading:
    def __init__(self, orders, replace_errors=()):
        self._orders = orders
        self._replace_errors = set(replace_errors)
        self.canceled: list[str] = []
        self.replaced: list[tuple] = []  # (oid, limit_price, stop_price)

    def get_orders(self, *a, **k):
        return self._orders

    def cancel_order_by_id(self, oid):
        self.canceled.append(oid)

    def replace_order_by_id(self, oid, req):
        if oid in self._replace_errors:
            raise RuntimeError("422 invalid replace")
        self.replaced.append(
            (oid, getattr(req, "limit_price", None), getattr(req, "stop_price", None))
        )
        return SimpleNamespace(id=f"new-{oid}")


def test_clear_orders_for_exit_replaces_live_sells_and_skips_wedged():
    c = AlpacaClient.__new__(AlpacaClient)
    c.trading = _FakeExitTrading([
        _open_order("wedged", status="pending_cancel"),      # untouchable: skip
        _open_order("live-tp", status="new", qty="61"),      # replace -> exit
        _open_order("already", status="new", limit_price="49.0"),  # marketable: keep
        _open_order("buy-1", side="buy", status="new"),      # cancel
        _open_order("err-leg", status="new", qty="7"),       # replace fails -> cancel
        _open_order("other", symbol="AAPL"),                 # different symbol: skip
    ], replace_errors={"err-leg"})
    out = c.clear_orders_for_exit("FRHC", ref_price=50.0)
    # 2% through 50.0 -> limit 49.0
    assert c.trading.replaced == [("live-tp", 49.0, None)]
    assert out == [("new-live-tp", 61.0, "live-tp", 0.0)]
    assert sorted(c.trading.canceled) == ["buy-1", "err-leg"]  # never "wedged"


def test_clear_orders_for_exit_counts_only_unfilled_qty():
    c = AlpacaClient.__new__(AlpacaClient)
    c.trading = _FakeExitTrading([_open_order("live-tp", qty="48", filled_qty="8")])
    assert c.clear_orders_for_exit("FRHC", ref_price=50.0) == [("new-live-tp", 40.0, "live-tp", 8.0)]


def test_clear_orders_for_exit_lifts_stop_trigger_never_sends_limit():
    # Regression (AVAV 2026-07-08): the leg reserving all the shares was a
    # bracket stop-MARKET order; replacing it with limit_price gets 42210000
    # ("market orders must not have limit_price") and the fallback cancel
    # reopened the pending_cancel wedge window. A stop leg must instead have
    # its trigger lifted above the market so it fires on the next print.
    c = AlpacaClient.__new__(AlpacaClient)
    c.trading = _FakeExitTrading([
        _open_order("stop-leg", symbol="AVAV", order_type="stop",
                    limit_price=None, stop_price="44.0", qty="37"),
    ])
    out = c.clear_orders_for_exit("AVAV", ref_price=50.0)
    # 2% above 50.0 -> trigger 51.0; NO limit_price on a market-type order
    assert c.trading.replaced == [("stop-leg", None, 51.0)]
    assert out == [("new-stop-leg", 37.0, "stop-leg", 0.0)]
    assert c.trading.canceled == []


def test_clear_orders_for_exit_stop_limit_gets_trigger_and_limit():
    c = AlpacaClient.__new__(AlpacaClient)
    c.trading = _FakeExitTrading([
        _open_order("sl", order_type="stop_limit", limit_price="60.0",
                    stop_price="44.0"),
    ])
    c.clear_orders_for_exit("FRHC", ref_price=50.0)
    assert c.trading.replaced == [("sl", 49.0, 51.0)]


def test_clear_orders_for_exit_never_touches_held_oco_sibling():
    # A bracket's stop leg rests as status "held" while its take-profit sibling
    # is live. Replacing it would hand the caller a SECOND full-qty exit for
    # the same shares (double-ledgered sell), and canceling it cancels every
    # remaining order in the OCO group — including the live leg just made
    # marketable, leaving the position with no exit. It must be skipped
    # entirely; the venue cancels it itself when the sibling fills.
    c = AlpacaClient.__new__(AlpacaClient)
    c.trading = _FakeExitTrading([
        _open_order("tp-leg", status="new", qty="37"),
        _open_order("stop-leg", status="held", order_type="stop",
                    limit_price=None, stop_price="44.0", qty="37"),
    ])
    out = c.clear_orders_for_exit("FRHC", ref_price=50.0)
    assert out == [("new-tp-leg", 37.0, "tp-leg", 0.0)]  # ONE exit, not two
    assert c.trading.replaced == [("tp-leg", 49.0, None)]
    assert c.trading.canceled == []


def test_clear_orders_for_exit_skips_stop_already_firing():
    # A trigger at/above the market fires on the next print (e.g. replaced
    # last tick) — it IS the exit; replacing again would just churn order ids.
    c = AlpacaClient.__new__(AlpacaClient)
    c.trading = _FakeExitTrading([
        _open_order("armed", order_type="stop", limit_price=None,
                    stop_price="51.0"),
    ])
    assert c.clear_orders_for_exit("FRHC", ref_price=50.0) == []
    assert c.trading.replaced == []
    assert c.trading.canceled == []


# -- has_working_exit (is the position covered while waiting to fill?) -------- #
def test_has_working_exit_true_for_marketable_limit():
    # LLY (2026-07-08/09): the exit legs were made marketable on a prior tick and
    # are still resting — the reserved shares are protected, so the watchdog must
    # NOT page "unprotected". ref 50.0 -> marketable at/below 49.0.
    c = AlpacaClient.__new__(AlpacaClient)
    c.trading = _FakeExitTrading([_open_order("mkt", limit_price="49.0", qty="9")])
    assert c.has_working_exit("FRHC", ref_price=50.0) is True


def test_has_working_exit_false_for_pending_cancel_far_legs_and_buys():
    # The FRHC-class stall: the only sells are a wedged pending_cancel and a far
    # take-profit that won't fill; a resting buy is irrelevant. -> unprotected.
    c = AlpacaClient.__new__(AlpacaClient)
    c.trading = _FakeExitTrading([
        _open_order("wedged", status="pending_cancel", limit_price="49.0"),
        _open_order("far-tp", limit_price="205.41"),      # above market: won't fill
        _open_order("buy-1", side="buy", limit_price="1.0"),
    ])
    assert c.has_working_exit("FRHC", ref_price=50.0) is False


def test_has_working_exit_true_for_firing_stop():
    c = AlpacaClient.__new__(AlpacaClient)
    c.trading = _FakeExitTrading([
        _open_order("armed", order_type="stop", limit_price=None, stop_price="51.0"),
    ])
    assert c.has_working_exit("FRHC", ref_price=50.0) is True


def test_has_working_exit_false_for_held_oco_sibling():
    # A held stop leg cannot execute while held — even with its trigger through
    # the market it is NOT protection on its own; only a live leg counts.
    c = AlpacaClient.__new__(AlpacaClient)
    c.trading = _FakeExitTrading([
        _open_order("parked", status="held", order_type="stop",
                    limit_price=None, stop_price="51.0"),
    ])
    assert c.has_working_exit("FRHC", ref_price=50.0) is False


# -- get_account: glitched-equity guard (the 2026-07-07 false halt) ---------- #
class _AcctTrading:
    """Serves queued (account, raw_positions) read cycles; repeats the last."""

    def __init__(self, cycles):
        self._cycles = list(cycles)
        self.reads = 0

    def _cur(self):
        return self._cycles[min(self.reads, len(self._cycles) - 1)]

    def get_account(self):
        return self._cur()[0]

    def get_all_positions(self):
        cur = self._cur()[1]
        self.reads += 1                     # positions end a read cycle
        return cur


def _acct_row(equity, cash, last_equity=None):
    return SimpleNamespace(
        equity=equity,
        last_equity=equity if last_equity is None else last_equity,
        cash=cash, buying_power=cash,
        pattern_day_trader=False, daytrade_count=0,
    )


def _raw_pos(symbol="QQQ", mv=95_000.0):
    return SimpleNamespace(
        symbol=symbol, qty=100.0, qty_available=100.0, avg_entry_price=900.0,
        current_price=mv / 100.0, market_value=mv, unrealized_pl=0.0,
        unrealized_plpc=0.0,
    )


def _no_sleep():
    """Patch out the re-read backoff; returns a restore callable."""
    import investment_strategy.execution.alpaca_client as ac
    orig = ac.time.sleep
    ac.time.sleep = lambda s: None
    return lambda: setattr(ac.time, "sleep", orig)


def test_get_account_consistent_read_passes_through():
    trading = _AcctTrading([(_acct_row(equity=97_326.75, cash=2_326.75),
                             [_raw_pos(mv=95_000.0)])])
    c = AlpacaClient.__new__(AlpacaClient)
    c.trading = trading
    snap = c.get_account()
    assert snap.equity == 97_326.75 and trading.reads == 1


def test_get_account_glitch_recovers_on_reread():
    # First read is the glitch signature (equity == cash while $95k is held);
    # the re-read is healthy and must be the one returned.
    glitch = (_acct_row(equity=2_326.75, cash=2_326.75), [_raw_pos(mv=95_000.0)])
    good = (_acct_row(equity=97_326.75, cash=2_326.75), [_raw_pos(mv=95_000.0)])
    trading = _AcctTrading([glitch, good])
    c = AlpacaClient.__new__(AlpacaClient)
    c.trading = trading
    restore = _no_sleep()
    try:
        snap = c.get_account()
    finally:
        restore()
    assert snap.equity == 97_326.75 and trading.reads == 2


def test_get_account_persistent_glitch_self_heals():
    # Every read is poisoned -> rebuild equity from cash + position values so
    # the watchdog/risk layer never sees the equity==cash number.
    glitch = (_acct_row(equity=2_326.75, cash=2_326.75), [_raw_pos(mv=95_000.0)])
    trading = _AcctTrading([glitch])
    c = AlpacaClient.__new__(AlpacaClient)
    c.trading = trading
    restore = _no_sleep()
    try:
        snap = c.get_account()
    finally:
        restore()
    assert snap.equity == 2_326.75 + 95_000.0
    assert trading.reads == 3               # initial read + 2 re-reads


def test_get_account_no_positions_is_trivially_consistent():
    # All-cash account: equity == cash is the NORMAL state, not a glitch.
    trading = _AcctTrading([(_acct_row(equity=2_326.75, cash=2_326.75), [])])
    c = AlpacaClient.__new__(AlpacaClient)
    c.trading = trading
    snap = c.get_account()
    assert snap.equity == 2_326.75 and trading.reads == 1


# -- get_account: glitched last_equity guard (2026-07-23 fabricated day P&L) -- #
def test_get_account_last_equity_zero_recovers_on_reread():
    # First read has the glitch signature (last_equity=0 while equity/cash/
    # positions all agree) — the re-read carries a real last_equity and must
    # be the one returned.
    glitch = (_acct_row(equity=97_326.75, cash=2_326.75, last_equity=0.0),
              [_raw_pos(mv=95_000.0)])
    good = (_acct_row(equity=97_326.75, cash=2_326.75, last_equity=96_500.0),
            [_raw_pos(mv=95_000.0)])
    trading = _AcctTrading([glitch, good])
    c = AlpacaClient.__new__(AlpacaClient)
    c.trading = trading
    restore = _no_sleep()
    try:
        snap = c.get_account()
    finally:
        restore()
    assert snap.last_equity == 96_500.0 and trading.reads == 2


def test_get_account_last_equity_persistent_glitch_heals_from_equity_history():
    # Every read is poisoned -> recover from our own equity_history.jsonl
    # (the most recent PRIOR-day row), not just zero the number out.
    glitch = (_acct_row(equity=97_326.75, cash=2_326.75, last_equity=0.0),
              [_raw_pos(mv=95_000.0)])
    trading = _AcctTrading([glitch])
    c = AlpacaClient.__new__(AlpacaClient)
    c.trading = trading
    restore = _no_sleep()
    from investment_strategy.status import EquityHistory
    with patch.object(EquityHistory, "all", return_value=[
        {"date": "2026-07-20", "equity": 96_000.0},
        {"date": "2026-07-22", "equity": 96_800.0},
    ]):
        try:
            snap = c.get_account()
        finally:
            restore()
    assert snap.last_equity == 96_800.0
    assert trading.reads == 3               # initial read + 2 re-reads


def test_get_account_last_equity_ignores_todays_own_row():
    # A same-day row (e.g. the bug's own corrupted snapshot, already written
    # before the fix landed) must never be used as "yesterday's" baseline.
    glitch = (_acct_row(equity=97_326.75, cash=2_326.75, last_equity=0.0),
              [_raw_pos(mv=95_000.0)])
    trading = _AcctTrading([glitch])
    c = AlpacaClient.__new__(AlpacaClient)
    c.trading = trading
    restore = _no_sleep()
    today = datetime.now(timezone.utc).date().isoformat()
    from investment_strategy.status import EquityHistory
    with patch.object(EquityHistory, "all", return_value=[
        {"date": "2026-07-22", "equity": 96_800.0},
        {"date": today, "equity": 97_326.75},   # corrupted "today" row
    ]):
        try:
            snap = c.get_account()
        finally:
            restore()
    assert snap.last_equity == 96_800.0


def test_get_account_last_equity_falls_back_to_current_equity_without_history():
    # No usable history at all -> fall back to CURRENT equity (day P/L reads
    # as unknown/0), never a fabricated "you made your whole balance today".
    glitch = (_acct_row(equity=97_326.75, cash=2_326.75, last_equity=0.0),
              [_raw_pos(mv=95_000.0)])
    trading = _AcctTrading([glitch])
    c = AlpacaClient.__new__(AlpacaClient)
    c.trading = trading
    restore = _no_sleep()
    from investment_strategy.status import EquityHistory
    with patch.object(EquityHistory, "all", return_value=[]):
        try:
            snap = c.get_account()
        finally:
            restore()
    assert snap.last_equity == snap.equity
    assert snap.day_pl == 0.0


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


# -- options: order construction + OCC parsing + closing legs ----------------- #
from alpaca.trading.enums import OrderClass as _OC
from alpaca.trading.enums import OrderSide as _OS
from alpaca.trading.enums import PositionIntent as _PI
from alpaca.trading.enums import TimeInForce as _TIF
from alpaca.trading.requests import OptionLegRequest as _OLR

from investment_strategy.execution.options import (
    build_closing_legs,
    occ_symbol,
    parse_occ,
    split_option_close_chunks,
)
from investment_strategy.models import Position


class _FakeOptionTrading:
    def __init__(self):
        self.submitted = []

    def submit_order(self, req):
        self.submitted.append(req)
        return SimpleNamespace(id="opt-oid-1")


def _opt_client():
    c = AlpacaClient.__new__(AlpacaClient)
    c.cfg = SimpleNamespace(can_open_orders=True)
    c.trading = _FakeOptionTrading()
    return c


def test_single_leg_option_submits_plain_order_with_symbol_and_side():
    # Regression: a 1-leg OrderClass.SIMPLE with legs=[...] and no symbol/side
    # is refused by the SDK's request validator — long calls/puts could never
    # submit. Uses the REAL request class so the SDK itself vets the shape.
    c = _opt_client()
    leg = _OLR(symbol="AAPL260814P00150000", ratio_qty=1, side=_OS.BUY,
               position_intent=_PI.BUY_TO_OPEN)
    oid = c.submit_option_legs([leg], qty=3)
    assert oid == "opt-oid-1"
    req = c.trading.submitted[0]
    assert req.symbol == "AAPL260814P00150000"
    assert req.side == _OS.BUY
    assert req.qty == 3
    assert req.position_intent == _PI.BUY_TO_OPEN
    assert not req.legs                              # no MLEG wrapper


def test_two_leg_option_submits_one_mleg_order():
    c = _opt_client()
    legs = [
        _OLR(symbol="AAPL260814P00160000", ratio_qty=1, side=_OS.BUY,
             position_intent=_PI.BUY_TO_OPEN),
        _OLR(symbol="AAPL260814P00150000", ratio_qty=1, side=_OS.SELL,
             position_intent=_PI.SELL_TO_OPEN),
    ]
    oid = c.submit_option_legs(legs, qty=2)
    assert oid == "opt-oid-1"
    req = c.trading.submitted[0]
    assert req.order_class == _OC.MLEG
    assert len(req.legs) == 2 and req.qty == 2


# -- submit_option_legs: entry-side limit price (2026-07-23 slippage fix) --- #
# Regression: a long call estimated (mid-quote) at $0.01/share, sized to a
# $900 cap, went out as a plain MARKET order and filled at $0.03/share — 3x
# the estimate — a real $2,700 loss instead of the intended $900. Entries now
# price a DAY limit at the estimate plus a buffer instead of an unbounded
# market order.
def test_single_leg_option_entry_uses_buffered_limit_when_estimate_given():
    c = _opt_client()
    leg = _OLR(symbol="T260821C00028000", ratio_qty=1, side=_OS.BUY,
               position_intent=_PI.BUY_TO_OPEN)
    oid = c.submit_option_legs([leg], qty=900, est_premium_per_share=0.01)
    assert oid == "opt-oid-1"
    req = c.trading.submitted[0]
    # min $-buffer floor applies (20% of $0.01 rounds to nothing at the cent
    # tick) -> $0.01 + $0.02 floor = $0.03, matching the real incident's fill.
    assert req.limit_price == 0.03
    assert req.symbol == "T260821C00028000"
    assert req.qty == 900


def test_single_leg_option_entry_uses_percentage_buffer_above_the_floor():
    c = _opt_client()
    leg = _OLR(symbol="AAPL260814P00150000", ratio_qty=1, side=_OS.BUY,
               position_intent=_PI.BUY_TO_OPEN)
    oid = c.submit_option_legs([leg], qty=3, est_premium_per_share=1.50)
    req = c.trading.submitted[0]
    assert req.limit_price == 1.80   # 1.50 + max(1.50*0.20, 0.02) = 1.80


def test_multi_leg_option_entry_uses_buffered_net_limit():
    c = _opt_client()
    legs = [
        _OLR(symbol="AAPL260814P00160000", ratio_qty=1, side=_OS.BUY,
             position_intent=_PI.BUY_TO_OPEN),
        _OLR(symbol="AAPL260814P00150000", ratio_qty=1, side=_OS.SELL,
             position_intent=_PI.SELL_TO_OPEN),
    ]
    oid = c.submit_option_legs(legs, qty=2, est_premium_per_share=0.50)
    req = c.trading.submitted[0]
    assert req.order_class == _OC.MLEG
    assert req.limit_price == 0.60    # 0.50 + max(0.50*0.20, 0.02) = 0.60


def test_option_entry_falls_back_to_market_without_an_estimate():
    # No estimate available (e.g. a quote-less leg) -> unchanged legacy path.
    c = _opt_client()
    leg = _OLR(symbol="AAPL260814P00150000", ratio_qty=1, side=_OS.BUY,
               position_intent=_PI.BUY_TO_OPEN)
    oid = c.submit_option_legs([leg], qty=3)
    req = c.trading.submitted[0]
    assert not hasattr(req, "limit_price") or req.limit_price is None


def test_parse_occ_round_trips_and_rejects_equities():
    sym = occ_symbol("AAPL", "2026-08-14", 150.0, "put")
    assert sym == "AAPL260814P00150000"
    assert parse_occ(sym) == ("AAPL", "2026-08-14", "P", 150.0)
    assert parse_occ("AAPL") is None
    assert parse_occ("BRK.B") is None
    assert parse_occ("QQQ") is None


# -- close_option_leg: DAY-limit fallback when market close has no quote ------ #
# Regression 2026-07-23: a long call whose mark went to $0 (no live NBBO) got
# error 40310000 "no available quote for symbol. please reenter with a limit"
# on every watchdog tick — the identical market close retried every ~30s for
# 12+ minutes with the position stuck unprotected, because there was no
# fallback order type.
class _FakeOptionCloseTrading:
    """close_position() optionally rejects (simulating the venue's "no quote"
    error); submit_order() (the DAY-limit fallback) records the request."""

    def __init__(self, close_error=None):
        self.close_error = close_error
        self.submitted = []

    def close_position(self, symbol):
        if self.close_error:
            raise self.close_error
        return SimpleNamespace(id="market-close-oid")

    def submit_order(self, req):
        self.submitted.append(req)
        return SimpleNamespace(id="limit-fallback-oid")


def _opt_position(symbol="T260821C00028000", qty=900.0, current_price=0.0,
                   avg_entry_price=0.03):
    return Position(
        symbol=symbol, qty=qty, avg_entry_price=avg_entry_price,
        current_price=current_price, market_value=current_price * qty * 100,
        unrealized_pl=(current_price - avg_entry_price) * qty * 100,
        unrealized_pl_pct=-100.0 if avg_entry_price else 0.0,
        asset_class="us_option",
    )


def test_close_option_leg_market_close_succeeds_no_fallback():
    trading = _FakeOptionCloseTrading()
    c = AlpacaClient.__new__(AlpacaClient)
    c.trading = trading
    oid = c.close_option_leg(_opt_position())
    assert oid == "market-close-oid"
    assert trading.submitted == []          # no fallback needed


def test_close_option_leg_falls_back_to_day_limit_on_no_quote_rejection():
    trading = _FakeOptionCloseTrading(close_error=Exception(
        '{"code":40310000,"message":"order has been rejected due to no '
        'available quote for symbol. please reenter with a limit"}'
    ))
    c = AlpacaClient.__new__(AlpacaClient)
    c.trading = trading
    oid = c.close_option_leg(_opt_position(qty=900.0, current_price=0.0,
                                            avg_entry_price=0.03))
    assert oid == "limit-fallback-oid"
    assert len(trading.submitted) == 1
    req = trading.submitted[0]
    assert req.symbol == "T260821C00028000"
    assert req.side == _OS.SELL              # long leg -> SELL to close
    assert req.qty == 900.0
    assert req.time_in_force == _TIF.DAY
    assert req.limit_price == 0.01           # mark is 0 -> floor to the min tick


def test_close_option_leg_short_leg_falls_back_to_buy_to_close():
    trading = _FakeOptionCloseTrading(close_error=Exception("no quote"))
    c = AlpacaClient.__new__(AlpacaClient)
    c.trading = trading
    oid = c.close_option_leg(_opt_position(qty=-5.0, current_price=1.20,
                                            avg_entry_price=0.80))
    assert oid == "limit-fallback-oid"
    req = trading.submitted[0]
    assert req.side == _OS.BUY               # short leg -> BUY to close
    assert req.qty == 5.0
    assert req.limit_price == 1.20           # uses the live mark, not the floor


def test_close_option_leg_returns_none_when_fallback_also_fails():
    class _AlwaysFails:
        def close_position(self, symbol):
            raise Exception("no quote")

        def submit_order(self, req):
            raise Exception("still rejected")

    c = AlpacaClient.__new__(AlpacaClient)
    c.trading = _AlwaysFails()
    assert c.close_option_leg(_opt_position()) is None


def test_close_option_group_single_leg_uses_close_option_leg_fallback():
    trading = _FakeOptionCloseTrading(close_error=Exception("no quote"))
    c = AlpacaClient.__new__(AlpacaClient)
    c.trading = trading
    oid = c.close_option_group([_opt_position()])
    assert oid == "limit-fallback-oid"


def _opt_pos(symbol, qty, basis=3.0, price=2.0):
    return Position(symbol=symbol, qty=qty, avg_entry_price=basis,
                    current_price=price, market_value=qty * price * 100,
                    unrealized_pl=(price - basis) * qty * 100,
                    unrealized_pl_pct=(price / basis - 1) * 100,
                    asset_class="us_option")


def test_build_closing_legs_flips_sides_and_uses_close_intents():
    long_leg = _opt_pos("AAPL260814P00160000", qty=2.0)
    short_leg = _opt_pos("AAPL260814P00150000", qty=-2.0, basis=1.0, price=0.5)
    legs, group_qty = build_closing_legs([long_leg, short_leg])
    assert group_qty == 2
    assert [l.ratio_qty for l in legs] == [1, 1]
    assert legs[0].side == _OS.SELL
    assert legs[0].position_intent == _PI.SELL_TO_CLOSE
    assert legs[1].side == _OS.BUY
    assert legs[1].position_intent == _PI.BUY_TO_CLOSE


def _amzn_5leg_group():
    # The exact 2026-08-07 shape: two call structures merged under one
    # underlying+expiry — 6x 230/245 spread, 26x 280/290 spread, +2 240 long.
    return [
        _opt_pos("AMZN260918C00230000", qty=6.0),
        _opt_pos("AMZN260918C00240000", qty=2.0),
        _opt_pos("AMZN260918C00245000", qty=-6.0),
        _opt_pos("AMZN260918C00280000", qty=26.0),
        _opt_pos("AMZN260918C00290000", qty=-26.0),
    ]


def test_split_chunks_amzn_5leg_pairs_shorts_and_conserves_contracts():
    chunks = split_option_close_chunks(_amzn_5leg_group())
    assert all(len(c) <= 2 for c in chunks)
    # Every short leg rides with a call cover at a strike at or below its own.
    for c in chunks:
        for s in (p for p in c if p.qty < 0):
            k_short = parse_occ(s.symbol)[3]
            assert any(
                p.qty > 0 and parse_occ(p.symbol)[3] <= k_short for p in c
            ), f"short {s.symbol} left uncovered in its chunk"
    totals: dict[str, float] = {}
    for c in chunks:
        for p in c:
            totals[p.symbol] = totals.get(p.symbol, 0.0) + p.qty
    assert totals == {
        "AMZN260918C00230000": 6.0,
        "AMZN260918C00240000": 2.0,
        "AMZN260918C00245000": -6.0,
        "AMZN260918C00280000": 26.0,
        "AMZN260918C00290000": -26.0,
    }


def test_split_chunks_put_cover_needs_higher_strike():
    chunks = split_option_close_chunks([
        _opt_pos("SPY260918P00500000", qty=3.0),    # cover (strike above)
        _opt_pos("SPY260918P00480000", qty=-3.0),   # short
        _opt_pos("SPY260918P00450000", qty=2.0),    # too LOW to cover a 480 short
        _opt_pos("SPY260918P00470000", qty=1.0),
        _opt_pos("SPY260918P00440000", qty=1.0),
    ])
    pair = next(c for c in chunks if len(c) == 2)
    assert {p.symbol for p in pair} == {
        "SPY260918P00500000", "SPY260918P00480000"
    }


def test_split_chunks_uncovered_short_closes_alone():
    chunks = split_option_close_chunks([
        _opt_pos("XYZ260918C00100000", qty=-2.0),   # no long anywhere below
        _opt_pos("XYZ260918C00110000", qty=1.0),
        _opt_pos("XYZ260918C00120000", qty=1.0),
        _opt_pos("XYZ260918C00130000", qty=1.0),
        _opt_pos("XYZ260918C00140000", qty=1.0),
    ])
    bare = [c for c in chunks if len(c) == 1 and c[0].qty < 0]
    assert len(bare) == 1 and bare[0][0].qty == -2.0


def test_split_chunks_partial_qty_splits_the_long():
    chunks = split_option_close_chunks([
        _opt_pos("QQQ260918C00400000", qty=6.0),
        _opt_pos("QQQ260918C00410000", qty=-4.0),
        _opt_pos("QQQ260918C00420000", qty=1.0),
        _opt_pos("QQQ260918C00430000", qty=1.0),
        _opt_pos("QQQ260918C00440000", qty=1.0),
    ])
    pair = next(c for c in chunks if len(c) == 2)
    lng = next(p for p in pair if p.qty > 0)
    assert lng.symbol == "QQQ260918C00400000" and lng.qty == 4.0
    leftover = [
        c[0] for c in chunks
        if len(c) == 1 and c[0].symbol == "QQQ260918C00400000"
    ]
    assert len(leftover) == 1 and leftover[0].qty == 2.0


class _FakeMlegCloseTrading:
    """Accepts market closes AND MLEG submits, mirroring Alpaca's 4-leg cap
    (MarketOrderRequest itself enforces it at construction, before submit)."""

    def __init__(self, fail_on_submit: int | None = None):
        self.submitted = []
        self.closed = []
        self.fail_on_submit = fail_on_submit

    def close_position(self, symbol):
        self.closed.append(symbol)
        return SimpleNamespace(id=f"mkt-{len(self.closed)}")

    def submit_order(self, req):
        self.submitted.append(req)
        if self.fail_on_submit == len(self.submitted):
            raise Exception("simulated venue rejection")
        return SimpleNamespace(id=f"mleg-{len(self.submitted)}")


def test_close_option_group_5_legs_splits_into_capped_orders():
    trading = _FakeMlegCloseTrading()
    c = AlpacaClient.__new__(AlpacaClient)
    c.trading = trading
    oid = c.close_option_group(_amzn_5leg_group())
    assert oid is not None
    # Two covered pairs as MLEG orders + the lone 240C via market close.
    assert len(trading.submitted) == 2
    assert all(len(r.legs) <= 4 for r in trading.submitted)
    assert trading.closed == ["AMZN260918C00240000"]


def test_close_option_group_partial_chunk_failure_returns_none():
    trading = _FakeMlegCloseTrading(fail_on_submit=2)
    c = AlpacaClient.__new__(AlpacaClient)
    c.trading = trading
    # None -> the watchdog stays CRITICAL and retries the remainder.
    assert c.close_option_group(_amzn_5leg_group()) is None
    assert len(trading.submitted) == 2         # both pairs were attempted


def test_to_position_maps_asset_class():
    sdk = _sdk_position()
    sdk.asset_class = SimpleNamespace(value="us_option")
    pos = AlpacaClient._to_position(sdk)
    assert pos.asset_class == "us_option" and pos.is_option is True
    # And absent asset_class (backtest fixtures, older SDKs) stays equity.
    pos2 = AlpacaClient._to_position(_sdk_position())
    assert pos2.asset_class == "us_equity" and pos2.is_option is False


# -- flooring visibility + HTTP timeout injection ----------------------------- #

def test_whole_share_floor_reports_dropped_notional():
    # LLY 2026-07-14: 1.73433 sh floored to 1 dropped $847 with no trace.
    # The submission must carry the dropped $ so the cycle can total the drag.
    c = _client(price=100.0)
    sub = c.submit_from_decision(_decision(qty=2.5, notional=250.0))
    assert sub.dropped_notional == 50.0
    c2 = _client(price=100.0)
    sub2 = c2.submit_from_decision(_decision(qty=3.0, notional=300.0))
    assert sub2.dropped_notional == 0.0


def test_bound_client_injects_session_timeout():
    # alpaca-py exposes no timeout surface, so we bind one onto the private
    # Session ("read timeout=None" hung the news fetch 2026-07-14). This test
    # fails loudly if an SDK upgrade renames _session or starts passing its
    # own timeout.
    from alpaca.trading.client import TradingClient

    from investment_strategy.execution.alpaca_client import (
        HTTP_TIMEOUT,
        bound_client,
    )
    client = bound_client(TradingClient("key", "secret", paper=True))
    assert client._session.request.keywords["timeout"] == HTTP_TIMEOUT


def test_still_marketable_limit_is_left_alone_on_falling_tape():
    # PLTR 2026-07-15: a trail exit was re-REPLACED every ~30s tick because the
    # "already marketable" test compared against the BUFFERED price (ref-2%),
    # so any dip re-priced a limit that could already fill on the next print —
    # each replacement minted a new order id (4 realized lots in 93s) and reset
    # the paper-sim queue. Marketable now means at/below the LAST TRADE.
    c = AlpacaClient.__new__(AlpacaClient)
    c.trading = _FakeExitTrading([
        # Priced between ref (50.0) and the 2% buffer (49.0): fills on the next
        # print -> it IS the exit; must NOT be replaced again.
        _open_order("mid", limit_price="49.5"),
    ])
    assert c.clear_orders_for_exit("FRHC", ref_price=50.0) == []
    assert c.trading.replaced == []
    assert c.trading.canceled == []


def test_has_working_exit_counts_limit_between_buffer_and_last_trade():
    # Mirror of the clear_orders_for_exit change (the docstring demands the two
    # marketability tests stay in sync): a leg left alone as "already the exit"
    # must also read as protection, or the watchdog pages a false CRITICAL.
    c = AlpacaClient.__new__(AlpacaClient)
    c.trading = _FakeExitTrading([_open_order("mid", limit_price="49.5", qty="9")])
    assert c.has_working_exit("FRHC", ref_price=50.0) is True
