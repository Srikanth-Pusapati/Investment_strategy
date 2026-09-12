"""Run-7 S-7: core-defense cross-day stale-map reset, cancel/sell race,
pending_cancel stop filter, sub-share rider.

Sep 11 2026 08:30 ET (logs/Sep_11_2026.log): the Sep 10 14:38 falling-names
map {DRAM, INTC, SEI} — 17h52m old — fired the breadth leg at the next
morning's first cycle on a risk-on +0.9% open. The trim canceled the 163-sh
QQQ GTC stop and sold in the same instant: 40310000 "available 0.4975 /
held_for_orders 163" (the cancel was still pending_cancel). _ensure_core_stop
then read the pending_cancel stop back as "already right" and cleared the
retry flag — core stopless until 08:35:11 (4m53s); the trim was never
retried. The auto-hedge target read 0.80 (falling) from the same stale map.

Runnable two ways:
    .venv/bin/python -m pytest tests/test_core_defense.py
    .venv/bin/pytest tests/
"""
from __future__ import annotations

import logging
import os
import sys
import tempfile
import threading
import uuid
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.execution.alpaca_client import AlpacaClient
from investment_strategy.models import AccountSnapshot, Position
from investment_strategy.orchestrator import Orchestrator
from investment_strategy.state import PortfolioState


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
_THREE = {"DRAM": "-4.8% today", "INTC": "-4.9% today", "SEI": "-4.3% today"}
_TODAY = "2026-09-11"
_YESTERDAY = "2026-09-10"


def _et(date_s: str, hm: str = "08:30") -> datetime:
    return datetime.fromisoformat(f"{date_s}T{hm}:00").replace(
        tzinfo=ZoneInfo("America/New_York")
    )


def _qqq(qty=163.497544, basis=709.11, price=727.0, qty_available=None):
    return Position(
        symbol="QQQ", qty=qty, avg_entry_price=basis, current_price=price,
        market_value=qty * price, unrealized_pl=0.0, unrealized_pl_pct=2.5,
        qty_available=qty_available,
    )


def _acct(positions=None, cash=400_000.0) -> AccountSnapshot:
    mv = sum(p.market_value for p in (positions or []))
    return AccountSnapshot(
        equity=mv + cash, last_equity=mv + cash, cash=cash, buying_power=cash,
        positions=list(positions or []),
    )


class _TrimBroker:
    """Fake broker recording the ORDER of stop/sell calls. `stops` is what
    open_stop_sells(resting_only=True) returns; `pending` what the
    resting_only=False view adds (a pending_cancel order still reserving
    shares). `replace_ok=False` makes the venue refuse the replace."""

    def __init__(self, stops=None, replace_ok=True, reduce_ok=True,
                 pending_after_cancel=0, open_buy=0.0, free_after_polls=0):
        self.stops = [dict(s) for s in (stops or [])]
        self.replace_ok = replace_ok
        self.reduce_ok = reduce_ok
        # how many resting_only=False polls still show the canceled stop
        self.pending_after_cancel = pending_after_cancel
        self.calls: list[tuple] = []
        self.submitted: list = []
        self.canceled_ids: list[str] = []
        # Venue-like reserved-qty accounting (fix-pass): a qty-down replace
        # frees exactly the difference; a restore re-reserves it. A cancel
        # frees nothing here (pending_cancel still reserves — the Sep 11 race).
        self.available = 0.497544
        self.open_buy = open_buy            # working BUY notional (wash guard)
        # how many open_position polls still report the PRE-replace available
        # (the replace settling at the venue); 0 = frees instantly
        self.free_after_polls = free_after_polls
        self._pending_free = 0.0
        self.position_reads = 0
        self.closed: list[str] = []

    def open_stop_sells(self, symbol, resting_only=True):
        self.calls.append(("open_stop_sells", symbol, resting_only))
        if resting_only:
            return [dict(s) for s in self.stops]
        if self.pending_after_cancel > 0:
            self.pending_after_cancel -= 1
            return [dict(s, status="pending_cancel") for s in self._canceled_stops]
        return [dict(s) for s in self.stops]

    def replace_order_qty(self, order_id, qty):
        self.calls.append(("replace", order_id, qty))
        if not self.replace_ok:
            return None
        for s in self.stops:
            if s["id"] == order_id:
                freed = s["qty"] - float(qty)
                if self.free_after_polls > 0 and freed > 0:
                    self._pending_free += freed       # settles after N polls
                else:
                    self.available += freed
                s["qty"] = float(qty)
                s["id"] = f"new-{order_id}"
                return s["id"]
        return None

    def cancel_order(self, oid):
        self.calls.append(("cancel", oid))
        self.canceled_ids.append(oid)
        self._canceled_stops = [s for s in self.stops if s["id"] == oid]
        self.stops = [s for s in self.stops if s["id"] != oid]
        return True

    def reduce_position(self, symbol, qty):
        self.calls.append(("reduce", symbol, round(qty, 6)))
        return f"oid-reduce-{symbol}" if self.reduce_ok else None

    def open_position(self, symbol):
        self.position_reads += 1
        if self._pending_free and self.position_reads > self.free_after_polls:
            self.available += self._pending_free
            self._pending_free = 0.0
        return _qqq(qty_available=self.available)

    def open_buy_notional(self, symbol):
        return self.open_buy

    def cancel_open_orders_for(self, symbol):
        self.calls.append(("cancel_all", symbol))

    def close_position(self, symbol):
        self.calls.append(("close_position", symbol))
        self.closed.append(symbol)
        return f"oid-close-{symbol}"

    def submit(self, order):
        self.calls.append(("submit", order.symbol, order.qty))
        self.submitted.append(order)
        return f"oid-submit-{len(self.submitted)}"

    def latest_price(self, symbol):
        return 727.0


def _orch(broker=None, falling_names=None, map_date=_TODAY, today=_TODAY,
          map_cycle=1, cycle_seq=1, trim_pct=25.0, core_stop_pct=15.0):
    """Bare orchestrator exercising the REAL _market_falling (breadth leg
    only: regime filter off) and the REAL _apply_core_defense."""
    o = Orchestrator.__new__(Orchestrator)
    o.cfg = SimpleNamespace(
        core_etf="QQQ", core_defense_enabled=True,
        core_defense_trim_pct=trim_pct, core_stop_pct=core_stop_pct,
        market_drop_defense_pct=1.5,
        breadth_falling_names_min=3, breadth_book_drawdown_pct=0.0,
        risk=SimpleNamespace(regime_filter_enabled=False),
    )
    o.broker = broker if broker is not None else _TrimBroker()
    records = []
    o.ledger = SimpleNamespace(records=records, record=records.append)
    p = os.path.join(tempfile.gettempdir(), f"_cd_{uuid.uuid4().hex}.json")
    o.state = PortfolioState(path=p)
    o._trade_lock = threading.Lock()
    o._pending_oids = []
    o._core_stop_gap = False
    o._falling_names = dict(falling_names if falling_names is not None else _THREE)
    o._cycle_seq = cycle_seq
    o._breadth_map_cycle = map_cycle
    o._breadth_counted_cycle = -2
    o._breadth_map_date = map_date
    o._breadth_map_stamp = f"{map_date} 14:38"
    o._et_now = lambda: _et(today)
    return o


# --------------------------------------------------------------------------- #
# (2) cross-day stale-map reset
# --------------------------------------------------------------------------- #
def test_new_day_ignores_yesterdays_falling_map(caplog):
    # The Sep 11 08:30 shape: yesterday's 3-name map is still the live map at
    # the new day's first cycle. No trim, no stop touched, the counterfactual
    # line names what would have been sold — and the hedge target stays 1.00.
    br = _TrimBroker(stops=[{"id": "10951625", "qty": 163.0, "stop_price": 602.74}])
    o = _orch(broker=br, map_date=_YESTERDAY, today=_TODAY)
    acct = _acct([_qqq()])
    with caplog.at_level(logging.INFO, logger="orchestrator"):
        o._apply_core_defense(acct)
    assert br.calls == []                       # nothing replaced/canceled/sold
    assert o.ledger.records == []
    assert o._core_defense_active is False
    assert not o.state.core_defense_fired_today()
    msgs = [r.getMessage() for r in caplog.records]
    assert any(
        m.startswith("CORE DEFENSE: stale falling map (2026-09-10 14:38, 3 names) "
                     "ignored at new-day open; would have trimmed 40.4975 QQQ ($29k)")
        for m in msgs
    ), msgs
    assert any("BREADTH STALE MAP" in m for m in msgs)
    # The counterfactual is logged ONCE per map, not on every read.
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="orchestrator"):
        o._apply_core_defense(acct)
    assert not any("stale falling map" in r.getMessage() for r in caplog.records)
    # The same date gate covers _market_falling for the hedge target: the
    # beta hedge reads clear, so it holds against 1.00, not 0.80.
    o.cfg.hedge_etf = "PSQ"
    o.cfg.auto_hedge_mode = "beta"
    o.cfg.hedge_beta_target, o.cfg.hedge_beta_band = 1.0, 0.15
    o.cfg.hedge_beta_falling_target = 0.8
    o.cfg.book_beta_enabled = True
    o.book_beta = SimpleNamespace(
        read=lambda a, on_progress=None: SimpleNamespace(available=True, spy=0.88),
    )
    o._book_beta_reading = None
    o._hedge_reason = ""
    o._falling_cycles = 0
    psq = Position(symbol="PSQ", qty=1000.0, avg_entry_price=200.0,
                   current_price=203.0, market_value=203_000.0,
                   unrealized_pl=0.0, unrealized_pl_pct=1.5)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="orchestrator"):
        o._apply_beta_hedge(_acct([_qqq(), psq]), "PSQ")
    assert any("within 1.00 +/- 0.15" in r.getMessage() for r in caplog.records), \
        [r.getMessage() for r in caplog.records]


def test_same_day_map_still_carries():
    # The deliberate within-day carry (tests/test_breadth_trigger.py, Aug-23
    # double-count guard) is untouched: a map from an EARLIER cycle of the
    # SAME session still trims at the next cycle's top-of-cycle pass when it
    # was not already counted toward the hedge persistence bar.
    br = _TrimBroker(stops=[{"id": "s1", "qty": 163.0, "stop_price": 602.74}])
    o = _orch(broker=br, map_date=_TODAY, today=_TODAY, map_cycle=1, cycle_seq=2)
    o._apply_core_defense(_acct([_qqq()]))
    assert o._falling_trigger == "breadth:3-names"
    assert ("reduce", "QQQ", 40.497544) in br.calls
    assert o.state.core_defense_fired_today()


# --------------------------------------------------------------------------- #
# (1) pending_cancel stops are not protection
# --------------------------------------------------------------------------- #
def _order(oid, status, qty="163", otype="stop", side="sell", symbol="QQQ"):
    return SimpleNamespace(id=oid, symbol=symbol, side=side, status=status,
                           qty=qty, stop_price="602.74", order_type=otype)


def test_open_stop_sells_filters_pending_cancel():
    c = AlpacaClient.__new__(AlpacaClient)
    c.trading = SimpleNamespace(get_orders=lambda *a, **k: [
        _order("wedged", "pending_cancel"),
        _order("gone", "canceled"),
        _order("old", "replaced"),
        _order("live", "new", qty="123"),
        _order("accepted", "accepted", qty="10"),
        _order("buy", "new", side="buy"),
        _order("lim", "new", otype="limit"),
        _order("other", "new", symbol="AAPL"),
    ])
    live = c.open_stop_sells("QQQ")
    assert [o["id"] for o in live] == ["live", "accepted"]
    assert live[0] == {"id": "live", "qty": 123.0, "stop_price": 602.74, "status": "new"}
    # The cancel-fallback poll needs to SEE the pending cancel to know when
    # the reserved shares are free.
    everything = c.open_stop_sells("QQQ", resting_only=False)
    assert [o["id"] for o in everything] == ["wedged", "gone", "old", "live", "accepted"]


# --------------------------------------------------------------------------- #
# (3) trim mechanics: replace-qty-down first, cancel -> poll -> sell fallback
# --------------------------------------------------------------------------- #
def test_trim_replaces_stop_qty_then_reduces(caplog):
    br = _TrimBroker(stops=[{"id": "10951625", "qty": 163.0, "stop_price": 602.74}])
    o = _orch(broker=br)
    acct = _acct([_qqq()])
    with caplog.at_level(logging.INFO, logger="orchestrator"):
        o._apply_core_defense(acct)
    # Replace BEFORE the sell; the stop is never canceled; the sell carries
    # the whole 40 + the 0.497544 residual.
    assert br.calls[:3] == [
        ("open_stop_sells", "QQQ", True),
        ("replace", "10951625", 123.0),
        ("reduce", "QQQ", 40.497544),
    ]
    assert br.canceled_ids == []
    assert any(
        r.getMessage().startswith(
            "CORE DEFENSE: stop 10951625 replaced 163 -> 123 sh, trimming 40")
        for r in caplog.records
    )
    # The resting stop now reads exactly the integer remainder, so the inline
    # _ensure_core_stop leaves it alone and clears the retry flag.
    assert br.submitted == []
    assert o._core_stop_gap is False
    assert o.state.core_defense_fired_today()
    rec = o.ledger.records[-1]
    assert rec.symbol == "QQQ" and rec.qty == 40.497544
    assert rec.exit_reason == "core_defense"


def test_trim_falls_back_to_cancel_poll_reduce(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr("investment_strategy.orchestrator.time.sleep",
                        lambda s: sleeps.append(s))
    br = _TrimBroker(
        stops=[{"id": "s1", "qty": 163.0, "stop_price": 602.74}],
        replace_ok=False, pending_after_cancel=2,
    )
    o = _orch(broker=br)
    acct = _acct([_qqq()])
    o._apply_core_defense(acct)
    kinds = [c[0] for c in br.calls]
    # replace refused -> cancel -> poll (pending, pending, empty) -> sell
    assert kinds[:2] == ["open_stop_sells", "replace"]
    assert kinds[2] == "cancel"
    polls = [c for c in br.calls if c[0] == "open_stop_sells" and c[2] is False]
    assert len(polls) == 3                       # two pending views, then clear
    assert sleeps == [0.5, 0.5]                  # waited between polls only
    assert kinds.index("reduce") > kinds.index("cancel")
    assert ("reduce", "QQQ", 40.497544) in br.calls
    # The core is stopless after the cancel: a fresh stop rests at once for
    # the 123-share remainder (the poll proved the cancel settled).
    assert len(br.submitted) == 1 and br.submitted[0].qty == 123.0
    assert o._core_stop_gap is False
    assert o.state.core_defense_fired_today()


# --------------------------------------------------------------------------- #
# (4) any failure keeps the watchdog retry armed; nothing re-placed inline
# --------------------------------------------------------------------------- #
def test_core_stop_gap_stays_armed_when_trim_fails(monkeypatch, caplog):
    monkeypatch.setattr("investment_strategy.orchestrator.time.sleep", lambda s: None)
    # Replace refused AND the cancel never settles within the 5 s budget.
    br = _TrimBroker(
        stops=[{"id": "s1", "qty": 163.0, "stop_price": 602.74}],
        replace_ok=False, pending_after_cancel=99,
    )
    o = _orch(broker=br)
    ensure_calls = []
    o._ensure_core_stop = lambda a: ensure_calls.append(a)
    acct = _acct([_qqq()])
    with caplog.at_level(logging.INFO, logger="orchestrator"):
        o._apply_core_defense(acct)
    assert o._core_stop_gap is True               # watchdog retry armed
    assert ensure_calls == []                     # NOT re-placed inline
    assert not any(c[0] == "reduce" for c in br.calls)   # never sold blind
    assert not o.state.core_defense_fired_today()  # retried next cycle
    assert o.ledger.records == []
    warn = [r.getMessage() for r in caplog.records
            if r.levelno == logging.WARNING and "NOT submitted" in r.getMessage()]
    assert len(warn) == 1
    assert "cancel still settling after 5s" in warn[0]
    assert "broker available=0.497544 of 163.498 sh" in warn[0]
    # The 0.497544 residual in the snapshot is untouched (nothing sold).
    assert acct.position_for("QQQ").qty == 163.497544
    # Second shape: the replace lands but the SELL is refused (e.g. the Sep
    # 11 40310000) — same contract: flag armed, no inline re-place, the
    # broker's available qty in the WARNING.
    br2 = _TrimBroker(stops=[{"id": "s1", "qty": 163.0, "stop_price": 602.74}],
                      reduce_ok=False)
    o2 = _orch(broker=br2)
    o2._ensure_core_stop = lambda a: ensure_calls.append(a)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="orchestrator"):
        o2._apply_core_defense(_acct([_qqq()]))
    assert o2._core_stop_gap is True and ensure_calls == []
    assert not o2.state.core_defense_fired_today()
    assert any("broker available=" in r.getMessage() and "NOT submitted" in r.getMessage()
               for r in caplog.records)
    # Fix-pass (review 2 #1): a refused sell after a landed replace puts the
    # stop BACK to its full size at once — a failed trim never leaves the
    # 163.4975-sh core covered by a 123-sh stop until the watchdog retry.
    assert br2.calls[-1] == ("replace", "new-s1", 163.0)
    assert br2.stops[0]["qty"] == 163.0
    assert any("refused after the replace — stop new-s1 restored 123 -> 163 sh"
               in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------- #
# (5) rider: the sub-share residual rides along so the stop covers 100%
# --------------------------------------------------------------------------- #
def test_trim_includes_subshare_residual():
    br = _TrimBroker(stops=[{"id": "s1", "qty": 163.0, "stop_price": 602.74}])
    o = _orch(broker=br)
    pos = _qqq(qty=163.4975)
    assert o._core_trim_qty(pos) == (40.4975, 40.0)   # 163.4975 x 0.25 -> 40 + 0.4975
    acct = _acct([pos])
    o._apply_core_defense(acct)
    assert ("replace", "s1", 123.0) in br.calls        # stop frees the WHOLE part only
    assert ("reduce", "QQQ", 40.4975) in br.calls      # the sell carries the residual
    assert acct.position_for("QQQ").qty == 123.0        # integer remainder
    # A position too small for a whole-share trim is left alone entirely.
    assert o._core_trim_qty(_qqq(qty=3.7)) == (0.0, 0.0)
    assert o._core_trim_qty(_qqq(qty=8.0)) == (2.0, 2.0)   # no residual -> plain trim


# --------------------------------------------------------------------------- #
# _ensure_core_stop: never cancel a good stop to chase shares a working sell
# holds (the pre-market shape of the replace path)
# --------------------------------------------------------------------------- #
def test_core_stop_left_alone_while_trim_sell_is_working(caplog):
    br = _TrimBroker(stops=[{"id": "new-s1", "qty": 123.0, "stop_price": 602.74}])
    o = _orch(broker=br)
    # Snapshot taken while the 40.4975 trim sell is still queued: the venue
    # still reports 163.4975 held, 0 available (stop 123 + sell 40.4975).
    acct = _acct([_qqq(qty=163.497544, qty_available=0.0)])
    with caplog.at_level(logging.INFO, logger="orchestrator"):
        o._ensure_core_stop(acct)
    assert br.canceled_ids == [] and br.submitted == []
    # Fix-pass (review 1 #4): the retry flag stays ARMED — `qty_available`
    # here is the cycle-start snapshot's (minutes stale); if the DAY sell was
    # rejected since, the 40 sh are free and under-stopped, and only the 30 s
    # watchdog re-check (fresh position read) can grow the stop. Previously
    # this branch cleared the flag and disarmed that retry for the cycle.
    assert o._core_stop_gap is True
    assert any("left alone rather than canceled" in r.getMessage()
               and "watchdog retry stays armed" in r.getMessage()
               for r in caplog.records)
    # Same stop, but the shares ARE free (sell was canceled/expired): grow it.
    br2 = _TrimBroker(stops=[{"id": "new-s1", "qty": 123.0, "stop_price": 602.74}])
    o2 = _orch(broker=br2)
    o2._ensure_core_stop(_acct([_qqq(qty=163.497544, qty_available=40.497544)]))
    assert br2.canceled_ids == ["new-s1"]
    assert len(br2.submitted) == 1 and br2.submitted[0].qty == 163.0


# --------------------------------------------------------------------------- #
# Fix-pass (review 1 #1, vote LF-1 correction c): the cross-day guard must not
# turn the first pass of a falling day into a same-cycle PSQ sell -> re-buy
# --------------------------------------------------------------------------- #
def _psq(qty=1000.0):
    return Position(symbol="PSQ", qty=qty, avg_entry_price=200.0,
                    current_price=203.0, market_value=qty * 203.0,
                    unrealized_pl=0.0, unrealized_pl_pct=1.5)


def _hedge_cfg(o, read_beta):
    o.cfg.hedge_etf = "PSQ"
    o.cfg.auto_hedge_mode = "beta"
    o.cfg.hedge_beta_target, o.cfg.hedge_beta_band = 1.0, 0.15
    o.cfg.hedge_beta_falling_target = 0.8
    o.cfg.hedge_unwind_min_cycles = 1
    o.cfg.book_beta_enabled = True
    o.book_beta = SimpleNamespace(
        read=lambda a, on_progress=None: SimpleNamespace(available=True, spy=read_beta),
    )
    o._book_beta_reading = None
    o._hedge_reason = ""
    o._falling_cycles = 0


def test_stale_map_defers_beta_unwind_until_fresh_read(caplog):
    # Monday 08:30 ET: PSQ held, overnight drift reads 0.84 (Sep 10 14:38
    # read exactly 0.84), yesterday's 3-name map is still the live map. The
    # top-of-cycle pass evaluates at target 1.00 (cross-day guard) -> 0.84 <
    # 0.85 would UNWIND; minutes later today's fresh map re-arms at 0.80 and
    # would re-buy. The pass must HOLD until today's map has landed.
    br = _TrimBroker()
    o = _orch(broker=br, map_date=_YESTERDAY, today=_TODAY, map_cycle=7, cycle_seq=8)
    _hedge_cfg(o, 0.84)
    acct = _acct([_qqq(), _psq()])
    with caplog.at_level(logging.INFO, logger="orchestrator"):
        o._apply_beta_hedge(acct, "PSQ")
    assert br.closed == []                                    # no hedge_unwind
    assert not any(r.exit_reason == "hedge_unwind" for r in o.ledger.records)
    assert acct.position_for("PSQ").qty == 1000.0
    assert o._hedge_reason == "beta:0.84" and o._falling_cycles == 1
    assert getattr(o, "_unwind_reads", (-1, 0)) == (-1, 0)   # S-8 streak untouched
    msgs = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("Auto-hedge: beta: unwind deferred — 3-name map predates today")
               for m in msgs), msgs
    assert not any("AUTO-HEDGE UNWIND" in m for m in msgs)
    # Today's fresh map lands (same cycle) with the three names still falling
    # -> target 0.80, 0.84 is inside 0.80 +/- 0.15 -> hold; hedge still held.
    o._falling_names = dict(_THREE)
    o._breadth_map_cycle = o._cycle_seq
    o._breadth_map_date = _TODAY
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="orchestrator"):
        o._apply_beta_hedge(acct, "PSQ")
    assert br.closed == [] and acct.position_for("PSQ").qty == 1000.0
    assert any("within 0.80 +/- 0.15" in r.getMessage() for r in caplog.records), \
        [r.getMessage() for r in caplog.records]
    # Same-day map (no cross-day guard): a 0.84 read at 1.00 still unwinds
    # exactly as before at HEDGE_UNWIND_MIN_CYCLES=1 — the deferral is
    # scoped to the stale-map pass only.
    br3 = _TrimBroker()
    o3 = _orch(broker=br3, map_date=_TODAY, today=_TODAY, falling_names={},
               map_cycle=8, cycle_seq=8)
    _hedge_cfg(o3, 0.84)
    o3._apply_beta_hedge(_acct([_qqq(), _psq()]), "PSQ")
    assert br3.closed == ["PSQ"]


# --------------------------------------------------------------------------- #
# Fix-pass (review 1 #2): the wrong-day map must not drive the prompt note,
# the sell-authority tag, the loss-cut release or the 4a-15 tape either
# --------------------------------------------------------------------------- #
def test_falling_names_today_gates_prompt_tags_and_tape():
    o = _orch(map_date=_YESTERDAY, today=_TODAY)
    o.cfg.risk.earnings_blackout_days = 0
    o.risk = SimpleNamespace(trading_halted=lambda a: (False, ""))
    o._regime_flipped_off = False
    assert o._falling_names_today() == {}
    assert o._sell_event_tags("DRAM", _acct([_qqq()])) == ()
    # Same-day map: everything reads it as before.
    o2 = _orch(map_date=_TODAY, today=_TODAY)
    o2.cfg.risk.earnings_blackout_days = 0
    o2.risk = SimpleNamespace(trading_halted=lambda a: (False, ""))
    o2._regime_flipped_off = False
    assert o2._falling_names_today() == _THREE
    assert o2._sell_event_tags("DRAM", _acct([_qqq()])) == ("name_falling:-4.8% today",)
    # An unstamped map (first cycle / tests) passes through.
    o3 = _orch(map_date="", today=_TODAY)
    assert o3._falling_names_today() == _THREE


# --------------------------------------------------------------------------- #
# Fix-pass (reviews 1 #7 / 2 #4): a pending_replace stop is neither absent
# nor stale — leave it, keep the retry armed
# --------------------------------------------------------------------------- #
def test_core_stop_pending_replace_left_alone(caplog):
    br = _TrimBroker(stops=[{"id": "10951625", "qty": 163.0, "stop_price": 602.74,
                             "status": "pending_replace"}])
    o = _orch(broker=br)
    # Snapshot already trimmed to 123 (the replace 163 -> 123 is settling).
    with caplog.at_level(logging.INFO, logger="orchestrator"):
        o._ensure_core_stop(_acct([_qqq(qty=123.0, qty_available=0.0)]))
        o._ensure_core_stop(_acct([_qqq(qty=123.0, qty_available=0.0)]))
    assert br.canceled_ids == [] and br.submitted == []
    assert o._core_stop_gap is True
    pend = [r for r in caplog.records if "is pending_replace" in r.getMessage()]
    assert len(pend) == 1                                     # logged once per id
    # Settled: the new id is listed with the right qty -> gap cleared, untouched.
    br.stops = [{"id": "new-1", "qty": 123.0, "stop_price": 602.74, "status": "new"}]
    o._ensure_core_stop(_acct([_qqq(qty=123.0, qty_available=0.0)]))
    assert o._core_stop_gap is False and br.canceled_ids == [] and br.submitted == []


# --------------------------------------------------------------------------- #
# Fix-pass (review 2 #1): the sell waits for the replace to free the shares
# --------------------------------------------------------------------------- #
def test_trim_waits_for_replace_to_free_shares(monkeypatch, caplog):
    sleeps: list[float] = []
    monkeypatch.setattr("investment_strategy.orchestrator.time.sleep",
                        lambda s: sleeps.append(s))
    br = _TrimBroker(stops=[{"id": "s1", "qty": 163.0, "stop_price": 602.74}],
                     free_after_polls=2)
    o = _orch(broker=br)
    with caplog.at_level(logging.INFO, logger="orchestrator"):
        o._apply_core_defense(_acct([_qqq()]))
    kinds = [c[0] for c in br.calls]
    assert kinds[:3] == ["open_stop_sells", "replace", "reduce"]
    assert sleeps == [0.5, 0.5]                    # two stale reads, then free
    assert ("reduce", "QQQ", 40.497544) in br.calls
    assert o.state.core_defense_fired_today()
    assert any(r.getMessage().startswith(
        "CORE DEFENSE: waiting for the replace to free 40.4975 QQQ sh (broker available=0.497544)")
        for r in caplog.records)
    # Budget exhausted: the sell is still attempted (the venue is the arbiter),
    # not skipped on a slow position read.
    br2 = _TrimBroker(stops=[{"id": "s1", "qty": 163.0, "stop_price": 602.74}],
                      free_after_polls=99)
    o2 = _orch(broker=br2)
    sleeps.clear()
    o2._apply_core_defense(_acct([_qqq()]))
    assert len(sleeps) == 10 and ("reduce", "QQQ", 40.497544) in br2.calls


# --------------------------------------------------------------------------- #
# Fix-pass (review 2 #5): a working same-symbol BUY would wash-block the sell
# --------------------------------------------------------------------------- #
def test_trim_deferred_while_core_buy_is_working(caplog):
    br = _TrimBroker(stops=[{"id": "s1", "qty": 163.0, "stop_price": 602.74}],
                     open_buy=5_000.0)
    o = _orch(broker=br)
    with caplog.at_level(logging.INFO, logger="orchestrator"):
        o._apply_core_defense(_acct([_qqq()]))
    assert br.calls == []                          # stop untouched, nothing sold
    assert not o.state.core_defense_fired_today()  # retried next cycle
    assert o.ledger.records == []
    warn = [r.getMessage() for r in caplog.records if "NOT submitted" in r.getMessage()]
    assert len(warn) == 1
    assert "working QQQ BUY ($5,000) would wash-block the trim sell" in warn[0]
