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

import logging
import os
import sys
import tempfile
import threading
import uuid
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.models import AccountSnapshot, Position
from investment_strategy.orchestrator import Orchestrator
from investment_strategy.state import PortfolioState


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


# -- regime-off book trim (1B.6) --------------------------------------------- #
class _FakeBroker:
    def __init__(self):
        self.reduced = []       # (symbol, qty)
        self.canceled = []
        self.closed = []
        self.core_buys = []     # (symbol, notional)
        self.stop_orders = []   # scripted open stop sells (core-stop tests)
        self.canceled_ids = []
        self.submitted = []     # OrderRequests via submit()

    def cancel_open_orders_for(self, symbol):
        self.canceled.append(symbol)

    def reduce_position(self, symbol, qty):
        self.reduced.append((symbol, round(qty, 6)))
        return f"oid-{symbol}"

    def close_position(self, symbol):
        self.closed.append(symbol)
        return f"oid-{symbol}"

    def open_position(self, symbol):
        # Fresh re-read unavailable in tests: decay path falls back to the
        # cycle-snapshot row; decision-sell tests script their own broker.
        return None

    def latest_price(self, symbol):
        return 100.0

    def submit_notional_buy(self, symbol, notional):
        self.core_buys.append((symbol, round(notional, 2)))
        return f"oid-core-{symbol}"

    def order_fill(self, order_id):
        # Instantly terminal so _apply_core_fill's wait-for-fill poll (which
        # lets the core stop rest the same cycle) never sleeps in tests.
        # filled=0 keeps the post-poll fold reconciliation a no-op.
        return ("filled", 0.0, 0.0)

    def next_market_open(self):
        return None  # closed-path stash: None = no wake-up armed

    def open_stop_sells(self, symbol):
        return [dict(o) for o in self.stop_orders]

    def cancel_order(self, oid):
        self.canceled_ids.append(oid)
        return True

    def submit(self, order):
        self.submitted.append(order)
        return f"oid-submit-{len(self.submitted)}"


class _FakeLedger:
    def __init__(self):
        self.records = []

    def record(self, rec):
        self.records.append(rec)


class _FakeWatchdog:
    """Fakes close_now with the real contract: ("full", oid) routed through the
    fake broker so tests can keep asserting on broker.closed. Script `outcomes`
    (a list popped per call) to exercise the partial/failed branches."""

    def __init__(self, broker=None):
        self.forgotten = []
        self.closed_now = []    # (symbol, reason)
        self.outcomes = []      # scripted (outcome, oid) tuples, FIFO
        self.broker = broker

    def forget(self, symbol):
        self.forgotten.append(symbol)

    def close_now(self, pos, reason):
        self.closed_now.append((pos.symbol, reason))
        if self.outcomes:
            return self.outcomes.pop(0)
        oid = (
            self.broker.close_position(pos.symbol)
            if self.broker else f"oid-{pos.symbol}"
        )
        return ("full", oid)


def _state_tmp():
    p = os.path.join(tempfile.gettempdir(), f"_orch_{uuid.uuid4().hex}.json")
    return PortfolioState(path=p)


def _orch(trim_enabled=True, trim_pct=25.0, state=None,
          thesis_decay_enabled=False, thesis_decay_min_age_days=3.0,
          thesis_min_score=0.1, core_etf="", target_invested_pct=0.0,
          min_cash_buffer_pct=2.0, max_gross_exposure_pct=100.0,
          kill_switch=False, whole_shares_only=False, core_stop_pct=15.0):
    o = Orchestrator.__new__(Orchestrator)
    o.cfg = SimpleNamespace(
        core_etf=core_etf, target_invested_pct=target_invested_pct,
        core_stop_pct=core_stop_pct,
        # These reconcile tests assert the LOG output; the enforcing halt
        # behavior has its own suite in test_ops_hardening.py.
        reconcile_halt_enabled=False,
        risk=SimpleNamespace(
            regime_trim_enabled=trim_enabled, regime_trim_pct=trim_pct,
            default_stop_loss_pct=5.0, default_take_profit_pct=12.0,
            thesis_decay_enabled=thesis_decay_enabled,
            thesis_decay_min_age_days=thesis_decay_min_age_days,
            thesis_min_score=thesis_min_score,
            min_cash_buffer_pct=min_cash_buffer_pct,
            max_gross_exposure_pct=max_gross_exposure_pct,
            min_order_usd=1.0,
            min_order_pct=0.05,
            whole_shares_only=whole_shares_only,
            max_cycle_symbol_share_pct=100.0,  # 100 = off
            # New guards default OFF here — they have their own suites
            # (test_rotation_guard.py); these tests pin the pre-existing paths.
            composite_budget_blend=False,
            rotation_loss_guard_enabled=False,
        ),
    )
    o.risk = SimpleNamespace(kill_switch=kill_switch)
    o.broker = _FakeBroker()
    o.ledger = _FakeLedger()
    o.watchdog = _FakeWatchdog(o.broker)
    o.state = state or _state_tmp()
    o._trade_lock = threading.Lock()
    o._pending_oids = []
    return o


def _regime(label):
    return SimpleNamespace(label=label, multiplier=0.25, reason=label)


def test_regime_trim_sells_slice_on_flip_into_risk_off():
    o = _orch(trim_enabled=True, trim_pct=25.0)
    acct = _acct(cash=0.0, positions=[_pos("AAPL", 400.0), _pos("MSFT", 200.0)])
    acct.positions[0].qty = 4.0   # 25% -> sell 1.0
    acct.positions[1].qty = 2.0   # 25% -> sell 0.5
    o._apply_regime_trim(acct, _regime("risk-off"))
    assert ("AAPL", 1.0) in o.broker.reduced
    assert ("MSFT", 0.5) in o.broker.reduced
    assert len(o.ledger.records) == 2
    # Bracket released + remainder re-protected by the watchdog.
    assert set(o.broker.canceled) == {"AAPL", "MSFT"}
    assert o.state.get_exits("AAPL") == {"stop_pct": 5.0, "take_pct": 12.0, "scaled": 0.0}


def test_regime_trim_fires_once_not_every_cycle():
    state = _state_tmp()
    o = _orch(trim_enabled=True, state=state)
    acct = _acct(positions=[_pos("AAPL", 400.0)])
    acct.positions[0].qty = 4.0
    o._apply_regime_trim(acct, _regime("risk-off"))     # transition -> trims
    o2 = _orch(trim_enabled=True, state=state)           # same persisted state
    acct2 = _acct(positions=[_pos("AAPL", 300.0)])
    acct2.positions[0].qty = 3.0
    o2._apply_regime_trim(acct2, _regime("risk-off"))    # still risk-off -> no-op
    assert o2.broker.reduced == []


def test_regime_trim_noop_when_disabled():
    o = _orch(trim_enabled=False)
    acct = _acct(positions=[_pos("AAPL", 400.0)])
    o._apply_regime_trim(acct, _regime("risk-off"))
    assert o.broker.reduced == []
    # But the label is still tracked so enabling it later trims on the next flip.
    assert o.state.get_regime_label() == "risk-off"


def test_regime_trim_rounds_to_whole_shares_in_whole_shares_mode():
    # GA-2.3: a partial sell must not create fractional dust that can't carry a
    # GTC exit — sub-share trims are floored (to 0 = skipped).
    o = _orch(trim_enabled=True, trim_pct=25.0, whole_shares_only=True)
    acct = _acct(cash=0.0, positions=[_pos("AAPL", 400.0), _pos("MSFT", 200.0)])
    acct.positions[0].qty = 4.0   # 25% -> 1.0 whole share, sells
    acct.positions[1].qty = 2.0   # 25% -> 0.5 -> floored to 0, skipped
    o._apply_regime_trim(acct, _regime("risk-off"))
    assert ("AAPL", 1.0) in o.broker.reduced
    assert all(sym != "MSFT" for sym, _ in o.broker.reduced)


# -- core exchange-side GTC stop (GA-2.3) ------------------------------------- #
def _core_pos(qty=10.4, basis=500.0):
    return Position(symbol="QQQ", qty=qty, avg_entry_price=basis,
                    current_price=basis, market_value=qty * basis,
                    unrealized_pl=0.0, unrealized_pl_pct=0.0)


def test_core_stop_rests_gtc_stop_for_whole_share_part():
    o = _orch(core_etf="QQQ", core_stop_pct=15.0)
    acct = _acct(positions=[_core_pos(qty=10.4, basis=500.0)])
    o._ensure_core_stop(acct)
    assert len(o.broker.submitted) == 1
    order = o.broker.submitted[0]
    assert order.symbol == "QQQ" and order.qty == 10.0          # whole shares only
    assert order.stop_price == 425.0                            # 15% under basis
    assert order.tif.value == "gtc" and order.order_type.value == "stop"
    assert order.side.value == "sell"


def test_core_stop_left_alone_when_already_right():
    o = _orch(core_etf="QQQ", core_stop_pct=15.0)
    o.broker.stop_orders = [{"id": "s1", "qty": 10.0, "stop_price": 425.0}]
    acct = _acct(positions=[_core_pos(qty=10.4, basis=500.0)])
    o._ensure_core_stop(acct)
    assert o.broker.submitted == [] and o.broker.canceled_ids == []


def test_core_stop_replaced_when_position_grows():
    o = _orch(core_etf="QQQ", core_stop_pct=15.0)
    o.broker.stop_orders = [{"id": "s1", "qty": 8.0, "stop_price": 425.0}]
    acct = _acct(positions=[_core_pos(qty=10.4, basis=500.0)])
    o._ensure_core_stop(acct)
    assert o.broker.canceled_ids == ["s1"]
    assert len(o.broker.submitted) == 1 and o.broker.submitted[0].qty == 10.0


def test_core_stop_off_when_pct_zero_or_no_core():
    o = _orch(core_etf="QQQ", core_stop_pct=0.0)   # written-acceptance path
    o._ensure_core_stop(_acct(positions=[_core_pos()]))
    o2 = _orch(core_etf="", core_stop_pct=15.0)    # no core configured
    o2._ensure_core_stop(_acct(positions=[_core_pos()]))
    assert o.broker.submitted == [] and o2.broker.submitted == []


def test_core_stop_skips_sub_share_position():
    # Alpaca rejects GTC on fractional qty — a sub-share core can't carry one.
    o = _orch(core_etf="QQQ", core_stop_pct=15.0)
    o._ensure_core_stop(_acct(positions=[_core_pos(qty=0.6)]))
    assert o.broker.submitted == []


def test_regime_trim_noop_when_not_risk_off():
    o = _orch(trim_enabled=True)
    acct = _acct(positions=[_pos("AAPL", 400.0)])
    o._apply_regime_trim(acct, _regime("risk-on"))
    assert o.broker.reduced == []


# -- deterministic thesis-decay exit (1B.4b) --------------------------------- #
from datetime import datetime, timedelta, timezone

from investment_strategy.models import Signal, SignalBundle, SignalKind


def _bundle(symbol, score):
    sig = Signal(kind=SignalKind.CONGRESS, symbol=symbol, summary="x", score=score)
    return SignalBundle(symbol=symbol, signals=[sig])


def _held(state, symbol, days_ago):
    state.register_entry(symbol, when=datetime.now(timezone.utc) - timedelta(days=days_ago))


def test_thesis_decay_exits_uncorroborated_held_name():
    state = _state_tmp()
    _held(state, "AAPL", days_ago=10)     # past the 3d grace
    o = _orch(thesis_decay_enabled=True, state=state)
    acct = _acct(positions=[_pos("AAPL", 300.0)])
    # Bundle present but bearish (score -0.5) -> thesis no longer corroborated.
    exited = o._apply_thesis_decay_exits([_bundle("AAPL", -0.5)], acct)
    assert exited == {"AAPL"}
    assert o.broker.closed == ["AAPL"]
    assert "AAPL" in o.watchdog.forgotten
    assert acct.position_for("AAPL") is None       # snapshot kept honest


def test_thesis_decay_spares_still_corroborated_name():
    state = _state_tmp()
    _held(state, "AAPL", days_ago=10)
    o = _orch(thesis_decay_enabled=True, state=state)
    acct = _acct(positions=[_pos("AAPL", 300.0)])
    exited = o._apply_thesis_decay_exits([_bundle("AAPL", 0.6)], acct)  # still bullish
    assert exited == set()
    assert o.broker.closed == []


def test_thesis_decay_respects_grace_age():
    state = _state_tmp()
    _held(state, "AAPL", days_ago=1)      # younger than the 3d grace
    o = _orch(thesis_decay_enabled=True, state=state)
    acct = _acct(positions=[_pos("AAPL", 300.0)])
    exited = o._apply_thesis_decay_exits([_bundle("AAPL", -0.9)], acct)
    assert exited == set()                 # too fresh to decay-exit


def test_thesis_decay_exits_when_no_bundle_at_all():
    state = _state_tmp()
    _held(state, "AAPL", days_ago=10)
    o = _orch(thesis_decay_enabled=True, state=state)
    acct = _acct(positions=[_pos("AAPL", 300.0)])
    exited = o._apply_thesis_decay_exits([], acct)  # signals went stale entirely
    assert exited == {"AAPL"}


def test_thesis_decay_failed_close_ledgers_nothing_and_keeps_position():
    """SPCX regression (2026-07-16): a close refused by the broker must NOT
    write a phantom SELL record, must NOT drop watchdog tracking, and must
    keep the position in the cycle snapshot for a later retry."""
    state = _state_tmp()
    _held(state, "AAPL", days_ago=10)
    o = _orch(thesis_decay_enabled=True, state=state)
    o.watchdog.outcomes = [("failed", None)]
    acct = _acct(positions=[_pos("AAPL", 300.0)])
    exited = o._apply_thesis_decay_exits([_bundle("AAPL", -0.5)], acct)
    assert exited == set()                          # not treated as exited
    assert o.ledger.records == []                   # no phantom SELL
    assert o.watchdog.forgotten == []               # still tracked
    assert acct.position_for("AAPL") is not None    # snapshot keeps it


def test_thesis_decay_partial_close_keeps_tracking_but_frees_capital():
    """A partial close (legs replaced into marketable exits) is ledgered
    inside close_now — the orchestrator must not double-record, must keep
    watchdog tracking until the fills land, and frees the cycle capital."""
    state = _state_tmp()
    _held(state, "AAPL", days_ago=10)
    o = _orch(thesis_decay_enabled=True, state=state)
    o.watchdog.outcomes = [("partial", None)]
    acct = _acct(positions=[_pos("AAPL", 300.0)])
    exited = o._apply_thesis_decay_exits([_bundle("AAPL", -0.5)], acct)
    assert exited == {"AAPL"}                       # dropped from the slate
    assert o.ledger.records == []                   # close_now already ledgered
    assert o.watchdog.forgotten == []               # tracked to completion
    assert acct.position_for("AAPL") is None        # capital freed downstream


def test_thesis_decay_noop_when_disabled():
    state = _state_tmp()
    _held(state, "AAPL", days_ago=10)
    o = _orch(thesis_decay_enabled=False, state=state)
    acct = _acct(positions=[_pos("AAPL", 300.0)])
    assert o._apply_thesis_decay_exits([], acct) == set()
    assert o.broker.closed == []


# -- core-satellite fill (1.6) ----------------------------------------------- #
def test_core_fill_deploys_idle_cash_to_target():
    # $1000 equity, nothing deployed, target 90% -> buy ~$900 of QQQ
    # (cash buffer 2% = $20 reserve, so spendable $980 covers the $900 gap).
    o = _orch(core_etf="QQQ", target_invested_pct=90.0, min_cash_buffer_pct=2.0)
    acct = _acct(cash=1_000.0)
    o._apply_core_fill(acct)
    assert o.broker.core_buys == [("QQQ", 900.0)]
    assert o._pending_oids == [("oid-core-QQQ", "QQQ")]
    assert len(o.ledger.records) == 1
    # Snapshot folded the fill in so a later call this cycle sees it deployed.
    assert acct.position_for("QQQ").market_value == 900.0


def test_core_fill_respects_cash_buffer():
    # Target wants $900 but only $500 is spendable above the 50% buffer -> caps at $500.
    o = _orch(core_etf="QQQ", target_invested_pct=90.0, min_cash_buffer_pct=50.0)
    acct = _acct(cash=1_000.0)
    o._apply_core_fill(acct)
    assert o.broker.core_buys == [("QQQ", 500.0)]


def test_core_fill_accounts_for_existing_positions():
    # Already 80% invested; target 90% -> only top up the remaining $100.
    o = _orch(core_etf="QQQ", target_invested_pct=90.0, min_cash_buffer_pct=2.0)
    acct = _acct(cash=200.0, positions=[_pos("AAPL", 800.0)])
    o._apply_core_fill(acct)
    assert o.broker.core_buys == [("QQQ", 100.0)]


def test_core_fill_noop_when_already_at_target():
    o = _orch(core_etf="QQQ", target_invested_pct=90.0)
    acct = _acct(cash=50.0, positions=[_pos("AAPL", 950.0)])  # 95% invested
    o._apply_core_fill(acct)
    assert o.broker.core_buys == []


def test_core_fill_noop_when_disabled():
    o = _orch(core_etf="", target_invested_pct=90.0)
    acct = _acct(cash=1_000.0)
    o._apply_core_fill(acct)
    assert o.broker.core_buys == []


def test_core_fill_noop_under_kill_switch():
    o = _orch(core_etf="QQQ", target_invested_pct=90.0, kill_switch=True)
    acct = _acct(cash=1_000.0)
    o._apply_core_fill(acct)
    assert o.broker.core_buys == []


def test_core_fill_clamped_to_gross_cap():
    # Target 90% but the no-leverage gross cap is 60% -> deploy only to 60%.
    o = _orch(core_etf="QQQ", target_invested_pct=90.0, max_gross_exposure_pct=60.0,
              min_cash_buffer_pct=2.0)
    acct = _acct(cash=1_000.0)
    o._apply_core_fill(acct)
    assert o.broker.core_buys == [("QQQ", 600.0)]


# -- slate whitelist (Todo-3 S.1) --------------------------------------------- #
from investment_strategy.models import Action, TradeProposal


def _prop(symbol, action="buy"):
    return TradeProposal(symbol=symbol, action=Action(action), conviction=0.5,
                         target_weight_pct=5.0, rationale="test")


def _slate_bundle(symbol):
    return SimpleNamespace(symbol=symbol)


def test_slate_whitelist_drops_unpresented_symbol():
    # A symbol that is neither a candidate bundle nor held = injection fallout.
    acct = _acct(positions=[_pos("AAPL", 100.0)])
    kept = Orchestrator._filter_to_slate(
        [_prop("AMD"), _prop("SCAM")], [_slate_bundle("AMD")], acct)
    assert [p.symbol for p in kept] == ["AMD"]


def test_slate_whitelist_allows_sell_of_held_name_without_slate_bundle():
    # Closing what we hold is never blocked, even with no bundle for it.
    acct = _acct(positions=[_pos("AAPL", 100.0)])
    kept = Orchestrator._filter_to_slate([_prop("AAPL", "sell")], [], acct)
    assert [p.symbol for p in kept] == ["AAPL"]


def test_slate_whitelist_passes_all_on_slate_untouched():
    acct = _acct()
    props = [_prop("AMD"), _prop("TDG", "sell")]
    kept = Orchestrator._filter_to_slate(
        props, [_slate_bundle("AMD"), _slate_bundle("TDG")], acct)
    assert kept == props


# -- fill reconciliation ------------------------------------------------------ #
class _LogCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


def _reconcile(status, filled, qty):
    """Run _reconcile_fills over one pending order with the given broker fill
    state; return the orchestrator and the log records it emitted."""
    o = _orch()
    o.broker.order_fill = lambda oid: (status, filled, qty)
    o._pending_oids = [("oid-1", "AAPL")]
    cap = _LogCapture()
    orch_log = logging.getLogger("orchestrator")
    orch_log.addHandler(cap)
    try:
        o._reconcile_fills()
    finally:
        orch_log.removeHandler(cap)
    return o, cap.records


def test_reconcile_filled_order_is_silent():
    # Regression: order_fill used to return "OrderStatus.FILLED", so cleanly
    # filled orders fell through to the "still pending" warning every cycle.
    o, recs = _reconcile("filled", 3.0, 3.0)
    assert [r for r in recs if r.levelno >= logging.WARNING] == []
    assert o._pending_oids == []


def test_reconcile_rejected_order_logs_error():
    _, recs = _reconcile("rejected", 0.0, 3.0)
    assert any(r.levelno == logging.ERROR for r in recs)


def test_reconcile_partial_fill_logs_warning():
    _, recs = _reconcile("accepted", 1.0, 3.0)
    assert any(
        r.levelno == logging.WARNING and "PARTIAL" in r.getMessage() for r in recs
    )


def test_reconcile_still_pending_logs_warning():
    _, recs = _reconcile("new", 0.0, 3.0)
    assert any("full cycle later" in r.getMessage() for r in recs)


# --------------------------------------------------------------------------- #
# Exchange-exit backfill (F.1) — record bracket-leg fills / manual sells the
# process never issued, keyed (idempotently) on order id against the ledger.
# --------------------------------------------------------------------------- #
from investment_strategy.ledger import TradeRecord  # noqa: E402


class _BackfillLedger:
    def __init__(self, records=None):
        self.records = list(records or [])

    def record(self, rec):
        self.records.append(rec)

    def all(self):
        return list(self.records)

    def effective(self):
        # No corrections in these fixtures — effective == all.
        return list(self.records)


def _backfill_orch(closed_sells, records=None):
    o = Orchestrator.__new__(Orchestrator)
    o.broker = SimpleNamespace(closed_sell_orders=lambda: closed_sells)
    o.ledger = _BackfillLedger(records)
    o.state = _state_tmp()   # backfilled exits stamp the re-entry cooldown clock
    return o


def _buy_rec(symbol="AAPL", entry=100.0, oid="buy-1", qty=10.0):
    return TradeRecord(symbol=symbol, action="buy", qty=qty,
                       entry_price=entry, cost_usd=entry * qty, order_id=oid)


def _closed(oid, symbol="AAPL", qty=10.0, price=92.0, otype="stop",
            filled_at="2026-07-02T15:30:00+00:00"):
    return {"order_id": oid, "symbol": symbol, "qty": qty, "price": price,
            "type": otype, "filled_at": filled_at}


def test_backfill_records_bracket_legs_with_realized_pl():
    o = _backfill_orch(
        closed_sells=[
            _closed("known-1", qty=2.0),                          # already ours
            _closed("leg-stop", qty=10.0, price=92.0, otype="stop"),
            _closed("leg-take", symbol="NVDA", qty=5.0, price=240.0,
                    otype="limit"),
            _closed("manual", symbol="MSFT", qty=1.0, price=50.0,
                    otype="market"),                              # outside actor
        ],
        records=[
            _buy_rec("AAPL", entry=100.0),
            _buy_rec("NVDA", entry=200.0, oid="buy-2"),
            # A sell we already recorded — dedupes by order id, and its qty
            # consumed 2 of AAPL's 10 shares before the backfill runs.
            TradeRecord(symbol="AAPL", action="sell", qty=2.0,
                        order_id="known-1", exit_price=100.0),
        ],
    )
    o._backfill_exchange_exits()
    new = o.ledger.records[3:]
    assert [r.exit_reason for r in new] == ["bracket_stop", "bracket_take", "external"]
    stop, take, manual = new
    # FIFO basis: 8 shares remain of the $100 lot -> -8% on what's covered.
    assert abs(stop.realized_pl_pct - (-8.0)) < 1e-9
    assert abs(stop.realized_pl - (-64.0)) < 1e-9   # (92-100) x 8 covered shares
    assert stop.exit_price == 92.0                   # recorded for FIFO lot math
    assert stop.ts.isoformat() == "2026-07-02T15:30:00+00:00"  # actual FILL time
    assert abs(take.realized_pl_pct - 20.0) < 1e-9
    assert abs(take.realized_pl - 200.0) < 1e-9
    assert manual.realized_pl_pct is None      # no ledger entry price for MSFT
    assert manual.symbol == "MSFT"


def test_backfill_uses_fifo_basis_across_multiple_lots():
    # Two lots (5 @ $100, then 5 @ $200); an exchange stop sells all 10 @ $150.
    # FIFO: +$250 on the first lot, -$250 on the second -> $0 realized. The old
    # most-recent-buy basis would have booked (150-200)x10 = -$500 (GA-2.5).
    o = _backfill_orch(
        closed_sells=[_closed("leg-stop", qty=10.0, price=150.0, otype="stop")],
        records=[
            _buy_rec("AAPL", entry=100.0, oid="buy-1", qty=5.0),
            _buy_rec("AAPL", entry=200.0, oid="buy-2", qty=5.0),
        ],
    )
    o._backfill_exchange_exits()
    rec = o.ledger.records[-1]
    assert rec.exit_reason == "bracket_stop"
    assert abs(rec.realized_pl - 0.0) < 1e-9
    assert abs(rec.realized_pl_pct - 0.0) < 1e-9   # basis = $150 blended FIFO


def test_backfill_is_idempotent_across_cycles():
    sells = [_closed("leg-stop")]
    o = _backfill_orch(sells, records=[_buy_rec()])
    o._backfill_exchange_exits()
    n = len(o.ledger.records)
    o._backfill_exchange_exits()               # same broker answer next cycle
    assert len(o.ledger.records) == n          # deduped by order id


def test_backfill_never_breaks_the_cycle_on_broker_failure():
    o = Orchestrator.__new__(Orchestrator)

    def _boom():
        raise RuntimeError("api down")

    o.broker = SimpleNamespace(closed_sell_orders=_boom)
    o.ledger = _BackfillLedger()
    o._backfill_exchange_exits()               # must swallow, not raise
    assert o.ledger.records == []


def test_backfill_stamps_reentry_cooldown_clock():
    # The fill must be RECENT: register_exit prunes stamps older than the
    # 7-day clock retention, so a hardcoded fill date turns this test into a
    # time bomb (it did — written with a Jul 2 stamp, failing from Jul 9 on).
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    recent = (_dt.now(_tz.utc) - _td(hours=1)).isoformat()
    o = _backfill_orch([_closed("stop-1", filled_at=recent)], records=[_buy_rec()])
    o._backfill_exchange_exits()
    # The exchange-side stop fill must start the re-entry cooldown at the FILL
    # time, so the next cycle can't immediately re-buy the stopped name.
    assert o.state.hours_since_exit("AAPL") is not None


# --------------------------------------------------------------------------- #
# Per-cycle budget fair-share (the 2026-07-06 all-LLY fix)
# --------------------------------------------------------------------------- #
from investment_strategy.models import TradeProposal  # noqa: E402
from investment_strategy.models import Action, Instrument  # noqa: E402


def _buy_prop(symbol, conviction=0.5, action=Action.BUY):
    return TradeProposal(symbol=symbol, action=action, conviction=conviction,
                         target_weight_pct=10.0, rationale="test")


def test_cycle_budget_split_by_conviction():
    o = _orch(min_cash_buffer_pct=0.0)
    acct = _acct(cash=1_000.0)
    caps = o._cycle_budget_caps(
        [_buy_prop("LLY", 0.8), _buy_prop("TSM", 0.2)], acct,
    )
    # $1,000 deployable split 0.8 : 0.2 -> LLY $800, TSM $200. The first buy
    # can no longer take the full $1,000 and starve the second to $0.
    assert abs(caps["LLY"] - 800.0) < 1.0
    assert abs(caps["TSM"] - 200.0) < 1.0


def test_cycle_budget_no_cap_for_single_buy():
    o = _orch(min_cash_buffer_pct=0.0)
    caps = o._cycle_budget_caps([_buy_prop("LLY", 0.8)], _acct(cash=1_000.0))
    assert caps == {}


def test_cycle_budget_ignores_sells_and_holds():
    o = _orch(min_cash_buffer_pct=0.0)
    caps = o._cycle_budget_caps(
        [_buy_prop("LLY", 0.8), _buy_prop("SPG", 0.9, action=Action.SELL),
         _buy_prop("AVAV", 0.5, action=Action.HOLD)],
        _acct(cash=1_000.0),
    )
    assert caps == {}  # only one BUY -> no share cap needed


def test_cycle_budget_respects_cash_buffer():
    o = _orch(min_cash_buffer_pct=10.0)   # 10% of $1,000 equity reserved
    caps = o._cycle_budget_caps(
        [_buy_prop("A", 0.5), _buy_prop("B", 0.5)], _acct(cash=1_000.0),
    )
    assert abs(sum(caps.values()) - 900.0) < 1.0


# --------------------------------------------------------------------------- #
# Sells-first proposal execution (rotation on a full book)
# --------------------------------------------------------------------------- #
def _rotation_orch():
    """Orchestrator with _handle_equity stubbed to record call order and fold
    sells back into the snapshot the way the real sell path does."""
    o = _orch(min_cash_buffer_pct=0.0)
    o._stamp_liveness = lambda: None
    o._calls = []

    def handle_equity(proposal, account, kinds, cycle_budget_cap=None,
                      tech=None, composite=None):
        o._calls.append((proposal.action.value, proposal.symbol, cycle_budget_cap))
        if proposal.action.value == "sell":
            Orchestrator._apply_pending_close(account, proposal.symbol)
        return 0.0

    def handle_option(proposal, account, kinds):
        o._calls.append(("option", proposal.symbol, None))

    o._handle_equity = handle_equity
    o._handle_option = handle_option
    return o


def test_execute_proposals_runs_equity_sells_first():
    # Rotation (postmortem 2026-07-14): the SELL must free the slot/capital
    # before any BUY is evaluated, even when the model lists the buy first.
    o = _rotation_orch()
    acct = _acct(cash=0.0, positions=[_pos("CVX", 500.0)])
    props = [
        _buy_prop("MU", 0.63),
        _buy_prop("CVX", 0.46, action=Action.SELL),
    ]
    o._execute_proposals(props, acct, {})
    assert [c[:2] for c in o._calls] == [("sell", "CVX"), ("buy", "MU")]
    assert acct.position_for("CVX") is None   # slot freed before the buy ran
    assert acct.cash == 500.0                 # capital freed before the buy ran


def test_execute_proposals_budget_split_sees_freed_capital():
    # The fair-share budget split must run AFTER the sells: on a full book the
    # rotation buys' deployable cash IS the sell's freed capital.
    o = _rotation_orch()
    seen = {}
    real_caps = o._cycle_budget_caps

    def caps_spy(props, account, composites=None):
        seen["cash"] = account.cash
        return real_caps(props, account, composites)

    o._cycle_budget_caps = caps_spy
    acct = _acct(cash=0.0, positions=[_pos("CVX", 500.0)])
    props = [
        _buy_prop("MU", 0.6),
        _buy_prop("TSM", 0.4),
        _buy_prop("CVX", 0.46, action=Action.SELL),
    ]
    o._execute_proposals(props, acct, {})
    assert seen["cash"] == 500.0
    # And the split itself reached the buys (both capped, sell uncapped).
    buys = [c for c in o._calls if c[0] == "buy"]
    assert all(cap is not None and cap > 0 for _, _, cap in buys)


def test_execute_proposals_keeps_options_and_holds_in_second_phase():
    o = _rotation_orch()
    acct = _acct(cash=100.0, positions=[_pos("CVX", 500.0)])
    opt = TradeProposal(
        symbol="NVDA", action=Action.BUY, conviction=0.7,
        target_weight_pct=10.0, rationale="test", instrument=Instrument.OPTION,
    )
    props = [
        opt,
        _buy_prop("AVAV", 0.5, action=Action.HOLD),
        _buy_prop("CVX", 0.46, action=Action.SELL),
    ]
    o._execute_proposals(props, acct, {})
    # Sell first; option and hold keep their relative order in phase two.
    assert [c[:2] for c in o._calls] == [
        ("sell", "CVX"), ("option", "NVDA"), ("hold", "AVAV"),
    ]


# --------------------------------------------------------------------------- #
# Held-position notes for the prompt (rotation baseline)
# --------------------------------------------------------------------------- #
def test_held_notes_built_from_state_clocks():
    o = _orch(core_etf="QQQ")
    o.state.register_buy("CVX", conviction=0.46)
    o.state.register_entry("CVX")
    acct = _acct(positions=[_pos("CVX", 100.0), _pos("QQQ", 500.0)])
    notes = o._held_notes(acct)
    assert "entry conviction 0.46" in notes["CVX"]
    assert "held 0.0d" in notes["CVX"]
    assert "QQQ" not in notes           # passive core: never slated, never rotated


def test_held_notes_skip_options_and_unclocked_names():
    o = _orch()
    opt = Position(
        symbol="AAPL260821C00200000", qty=1.0, avg_entry_price=2.0,
        current_price=2.0, market_value=200.0, unrealized_pl=0.0,
        unrealized_pl_pct=0.0, asset_class="us_option",
    )
    acct = _acct(positions=[opt, _pos("MSFT", 100.0)])
    # Option rows are skipped; MSFT predates conviction tracking -> no note.
    assert o._held_notes(acct) == {}


# --------------------------------------------------------------------------- #
# Daily dated log names (backward-analysis archive)
# --------------------------------------------------------------------------- #
from investment_strategy.__main__ import dated_log_name  # noqa: E402


def test_dated_log_name_formats_day_label():
    assert dated_log_name("logs/bot.log.2026-07-06") == "logs/Jul_06_2026.log"


def test_dated_log_name_falls_back_on_unparsable_suffix():
    assert dated_log_name("logs/bot.log.weird") == "logs/bot.log.weird"


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


# --------------------------------------------------------------------------- #
# Bearish option path through slate exclusions (the "earn on lows" fix)
# --------------------------------------------------------------------------- #
def _slate_orch(options_on=True, headroom=0.0, min_score=0.2):
    o = Orchestrator.__new__(Orchestrator)
    o.cfg = SimpleNamespace(
        risk=SimpleNamespace(min_order_usd=1.0, min_order_pct=0.0),
        screener=SimpleNamespace(min_score=min_score),
    )
    o.options = object() if options_on else None
    o._buy_headroom_usd = lambda sym, acct: (headroom, "at gross exposure cap")
    return o


def _discovery_bundle(symbol="XYZ", score=-0.5):
    sig = Signal(kind=SignalKind.DISCOVERY, symbol=symbol, summary="scan", score=score)
    return SignalBundle(symbol=symbol, signals=[sig])


def test_partition_keeps_bearish_notheld_candidate_when_options_on():
    # A bearish scanner name (admitted for a PUT play) must survive equity
    # buy-exclusion — dropping it silently disabled profiting from declines.
    o = _slate_orch(options_on=True)
    kept, excluded = o._partition_slate([_discovery_bundle(score=-0.5)], _acct())
    assert [b.symbol for b in kept] == ["XYZ"]
    assert "XYZ" in excluded  # the equity buy stays blocked


def test_partition_drops_bearish_notheld_when_options_off():
    # Without options there is no way to act on a blocked bearish name — drop
    # it as before (token savings).
    o = _slate_orch(options_on=False)
    kept, excluded = o._partition_slate([_discovery_bundle(score=-0.5)], _acct())
    assert kept == []
    assert "XYZ" in excluded


def test_partition_drops_bullish_notheld_when_blocked():
    # A BULLISH blocked name offers nothing actionable; still dropped.
    o = _slate_orch(options_on=True)
    kept, _ = o._partition_slate([_discovery_bundle(score=0.5)], _acct())
    assert kept == []


def test_bearish_lean_falls_back_to_mean_of_scored_signals():
    o = _slate_orch()
    sigs = [
        Signal(kind=SignalKind.NEWS, symbol="XYZ", summary="bad", score=-0.4),
        Signal(kind=SignalKind.TECHNICAL, symbol="XYZ", summary="down", score=-0.2),
    ]
    assert o._bearish_lean(SignalBundle(symbol="XYZ", signals=sigs)) is True
    assert o._bearish_lean(SignalBundle(symbol="XYZ", signals=[])) is False


def test_drop_excluded_buys_passes_option_proposals():
    # Equity buy-exclusions must not veto defined-risk option plays: the
    # option gate (premium/concurrency/DTE/halt) is their authority.
    o = Orchestrator.__new__(Orchestrator)
    put = TradeProposal(
        symbol="AMD", action=Action.BUY, conviction=0.7, target_weight_pct=0.0,
        rationale="bearish", instrument=Instrument.OPTION,
    )
    equity = _buy_prop("AMD", 0.7)
    kept = o._drop_excluded_buys([put, equity], {"AMD": "at symbol cap"})
    assert kept == [put]          # option passes, equity buy dropped


def test_drop_excluded_buys_still_drops_equity_and_passes_sells():
    o = Orchestrator.__new__(Orchestrator)
    sell = _buy_prop("AMD", 0.7, action=Action.SELL)
    kept = o._drop_excluded_buys([_buy_prop("AMD"), sell], {"AMD": "capped"})
    assert kept == [sell]


# -- decision cadence: at-the-bell wake-up + core-fill wait-for-fill --------- #
# (2026-07-13 incidents: first cycle ran 10:30 ET after a 09:29:43 tick missed
# the open; core stop wash-trade-rejected against its own still-open buy.)
import time  # noqa: E402
from unittest.mock import patch  # noqa: E402


def test_decision_due_fires_at_stashed_next_open_and_not_before():
    o = Orchestrator.__new__(Orchestrator)
    o.cfg = SimpleNamespace(decision_interval_s=3600)
    o._last_decision_at = time.time()        # wall-clock: hourly grid not due
    o._next_open_utc = None
    assert not o._decision_due()
    o._next_open_utc = datetime.now(timezone.utc) + timedelta(seconds=30)
    assert not o._decision_due()
    o._next_open_utc = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert o._decision_due()


def test_within_close_fence_only_near_the_bell():
    o = Orchestrator.__new__(Orchestrator)
    o.cfg = SimpleNamespace(close_fence_minutes=5.0)
    now = datetime.now(timezone.utc)
    # 3 min to close -> fenced; 30 min -> not; already closed (negative) -> not;
    # unknown close time -> fail open (not fenced).
    o.broker = SimpleNamespace(next_market_close=lambda: now + timedelta(minutes=3))
    assert o._within_close_fence() is True
    o.broker = SimpleNamespace(next_market_close=lambda: now + timedelta(minutes=30))
    assert o._within_close_fence() is False
    o.broker = SimpleNamespace(next_market_close=lambda: now - timedelta(minutes=1))
    assert o._within_close_fence() is False
    o.broker = SimpleNamespace(next_market_close=lambda: None)
    assert o._within_close_fence() is False
    o.cfg = SimpleNamespace(close_fence_minutes=0.0)   # disabled
    o.broker = SimpleNamespace(next_market_close=lambda: now + timedelta(minutes=1))
    assert o._within_close_fence() is False


def test_closed_tick_arms_the_bell_wakeup():
    o = Orchestrator.__new__(Orchestrator)
    o.cfg = SimpleNamespace(postmortem_enabled=False)
    bell = datetime.now(timezone.utc) + timedelta(hours=1)
    o.broker = SimpleNamespace(is_market_open=lambda: False,
                               next_market_open=lambda: bell)
    o._next_open_utc = None
    o.run_decision_cycle()
    assert o._next_open_utc == bell


def test_bell_stash_survives_failed_cycle_and_clears_on_success():
    # An exception AT the open must keep the stash armed (30s retry at the
    # bell); only a successful cycle consumes it; a FUTURE stash survives.
    o = Orchestrator.__new__(Orchestrator)
    o.cfg = SimpleNamespace(decision_interval_s=3600, monitor_interval_s=30)
    o._last_main_tick = 0.0
    o._last_main_wall = time.time()     # recent -> no post-wake settle path
    o._last_decision_at = 0.0            # hourly due -> _tick runs the cycle
    o._refresh_runtime_controls = lambda: None
    o._refresh_dashboard = lambda: None
    o._cycle_market_open = True
    o._dashboard_open_last = True
    bell = datetime.now(timezone.utc) - timedelta(seconds=1)  # consumed stash
    o._next_open_utc = bell

    def _boom():
        raise RuntimeError("transient at the bell")
    o.run_decision_cycle = _boom
    try:
        o._tick()
    except RuntimeError:
        pass
    assert o._next_open_utc == bell      # still armed: retry at the bell

    o.run_decision_cycle = lambda: None
    o._tick()
    assert o._next_open_utc is None      # consumed on success

    o._next_open_utc = datetime.now(timezone.utc) + timedelta(hours=12)
    o._last_decision_at = 0.0
    o._tick()
    assert o._next_open_utc is not None  # future stash survives success


def test_core_fill_poll_waits_through_open_statuses_until_terminal():
    o = _orch(core_etf="SPY", target_invested_pct=50.0, min_cash_buffer_pct=0.0)
    seq = iter(["new", "accepted", "partially_filled", "filled"])
    o.broker.order_fill = lambda oid: (next(seq), 0.0, 0.0)
    sleeps = []
    with patch("investment_strategy.orchestrator.time.sleep", sleeps.append):
        o._apply_core_fill(_acct(cash=1_000.0))
    assert len(sleeps) == 3


def test_core_fill_poll_breaks_immediately_on_rejected():
    o = _orch(core_etf="SPY", target_invested_pct=50.0, min_cash_buffer_pct=0.0)
    o.broker.order_fill = lambda oid: ("rejected", 0.0, 0.0)
    sleeps = []
    with patch("investment_strategy.orchestrator.time.sleep", sleeps.append):
        o._apply_core_fill(_acct(cash=1_000.0))
    assert sleeps == []


def test_core_fill_poll_is_bounded_when_order_never_goes_terminal():
    o = _orch(core_etf="SPY", target_invested_pct=50.0, min_cash_buffer_pct=0.0)
    o.broker.order_fill = lambda oid: ("new", 0.0, 0.0)
    sleeps = []
    with patch("investment_strategy.orchestrator.time.sleep", sleeps.append):
        o._apply_core_fill(_acct(cash=1_000.0))
    assert len(sleeps) == 15


def test_core_fill_rejected_buy_reverts_the_snapshot_fold():
    # A broker-side rejected core buy must not leave phantom shares for
    # _ensure_core_stop (or later buys this cycle) to size against.
    o = _orch(core_etf="SPY", target_invested_pct=50.0, min_cash_buffer_pct=0.0)
    o.broker.order_fill = lambda oid: ("rejected", 0.0, 0.0)
    acct = _acct(cash=1_000.0)
    with patch("investment_strategy.orchestrator.time.sleep", lambda s: None):
        o._apply_core_fill(acct)
    assert acct.position_for("SPY") is None
    assert acct.cash == 1_000.0
