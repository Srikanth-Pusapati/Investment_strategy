"""goGA GA-2.1/2.2 ops hardening: enforcing reconcile-halt, dead-man heartbeat
gating, and dark-gap detection."""
from __future__ import annotations

import os
import tempfile
import threading
import time
import uuid
from types import SimpleNamespace

from investment_strategy import orchestrator as orch_mod
from investment_strategy.orchestrator import Orchestrator


class _RecordingAlerter:
    def __init__(self):
        self.calls = []

    def critical(self, key, subject, body):
        self.calls.append((key, subject, body))


class _FillBroker:
    """order_fill returns whatever was scripted per order id."""
    def __init__(self, fills):
        self.fills = fills  # oid -> (status, filled, qty)

    def order_fill(self, oid):
        return self.fills[oid]


class _FakeState:
    def __init__(self):
        self.pending = []

    def set_pending_orders(self, oids):
        self.pending = list(oids)

    def get_pending_orders(self):
        return list(self.pending)

    def drain_pending_orders(self):
        pairs, self.pending = list(self.pending), []
        return pairs

    def merge_pending_orders(self, pairs):
        seen = {p[0] for p in self.pending}
        for oid, sym in pairs:
            if oid not in seen:
                self.pending.append((oid, sym))
                seen.add(oid)


class _FakeLedger:
    def __init__(self):
        self.records = []

    def record(self, rec):
        self.records.append(rec)


def _kill_path():
    return os.path.join(tempfile.gettempdir(), f"_kill_{uuid.uuid4().hex}")


def _orch(fills=None, *, halt_enabled=True, kill_file=None, heartbeat_url=""):
    o = Orchestrator.__new__(Orchestrator)
    o.cfg = SimpleNamespace(
        kill_switch=False,
        kill_switch_file=kill_file or _kill_path(),
        monitor_interval_s=30,
        reconcile_halt_enabled=halt_enabled,
        heartbeat_url=heartbeat_url,
    )
    o.risk = SimpleNamespace(kill_switch=False)
    o.state = _FakeState()
    o.broker = _FillBroker(fills or {})
    o.ledger = _FakeLedger()
    o.alerter = _RecordingAlerter()
    o._trade_lock = threading.Lock()
    o._pending_oids = [(oid, f"SYM{i}") for i, oid in enumerate(fills or {})]
    o._forced_halt = False
    o._last_main_tick = time.monotonic()
    o._last_wall_tick = time.time()
    return o


# -- reconcile halt ----------------------------------------------------------- #

def test_rejected_order_halts_new_buys_and_pages():
    o = _orch({"o1": ("rejected", 0.0, 5.0)})
    o._reconcile_fills()
    assert o.risk.kill_switch is True
    assert os.path.exists(o.cfg.kill_switch_file)
    content = open(o.cfg.kill_switch_file, encoding="utf-8").read()
    assert "reconcile mismatch" in content and "rejected" in content
    assert [c[0] for c in o.alerter.calls] == ["reconcile_halt"]
    os.remove(o.cfg.kill_switch_file)


def test_partial_fill_halts_new_buys():
    o = _orch({"o1": ("partially_filled", 2.0, 5.0)})
    o._reconcile_fills()
    assert o.risk.kill_switch is True
    assert os.path.exists(o.cfg.kill_switch_file)
    os.remove(o.cfg.kill_switch_file)


# -- ledger corrections at reconcile (GA-2.5) --------------------------------- #
def test_rejected_order_writes_a_ledger_correction():
    o = _orch({"o1": ("rejected", 0.0, 5.0)})
    o._reconcile_fills()
    assert len(o.ledger.records) == 1
    c = o.ledger.records[0]
    assert c.action == "correct" and c.order_id == "o1" and c.qty == 0.0
    assert "rejected" in c.risk_note
    os.remove(o.cfg.kill_switch_file)


def test_canceled_partial_correction_carries_filled_qty():
    o = _orch({"o1": ("canceled", 2.0, 5.0)})
    o._reconcile_fills()
    c = o.ledger.records[0]
    assert c.action == "correct" and c.qty == 2.0
    os.remove(o.cfg.kill_switch_file)


def test_live_partial_writes_no_correction_yet_and_requeues():
    # A non-terminal partial may still fill more — no correction until the
    # order reaches a terminal state; it's re-queued for the next reconcile.
    o = _orch({"o1": ("partially_filled", 2.0, 5.0)})
    o._reconcile_fills()
    assert o.ledger.records == []
    assert ("o1", "SYM0") in o._pending_oids
    assert o.state.pending == o._pending_oids  # survives a crash mid-cycle
    os.remove(o.cfg.kill_switch_file)


def test_clean_fills_do_not_halt():
    o = _orch({"o1": ("filled", 5.0, 5.0), "o2": ("unknown", 0.0, 0.0)})
    o._reconcile_fills()
    assert o.risk.kill_switch is False
    assert not os.path.exists(o.cfg.kill_switch_file)
    assert o.alerter.calls == []
    assert o.state.pending == []  # cleared list persisted before checking


def test_reconcile_checks_watchdog_queued_exits():
    # Watchdog exits are queued via state.add_pending_order from its own
    # thread; reconcile must check them even though the orchestrator's own
    # in-process list never saw them (they used to be clobbered/ignored).
    o = _orch({"wd-1": ("canceled", 0.0, 5.0)})
    o._pending_oids = []                      # orchestrator never saw it
    o.state.pending = [("wd-1", "NU")]
    o._reconcile_fills()
    assert o.ledger.records and o.ledger.records[0].action == "correct"
    os.remove(o.cfg.kill_switch_file)


def test_reconcile_skips_replaced_orders():
    # A superseded (replaced) watchdog exit was already corrected at replace
    # time by the supersede path; reconcile must not correct it again.
    o = _orch({"old-1": ("replaced", 0.0, 5.0)})
    o._reconcile_fills()
    assert o.ledger.records == []
    assert o.risk.kill_switch is False
    assert not os.path.exists(o.cfg.kill_switch_file)


def test_still_pending_order_warns_without_halting():
    # A slow-but-alive order may still fill — not a confirmed divergence.
    o = _orch({"o1": ("accepted", 0.0, 5.0)})
    o._reconcile_fills()
    assert o.risk.kill_switch is False
    assert not os.path.exists(o.cfg.kill_switch_file)


def test_reconcile_halt_can_be_disabled():
    o = _orch({"o1": ("rejected", 0.0, 5.0)}, halt_enabled=False)
    o._reconcile_fills()
    assert o.risk.kill_switch is False
    assert not os.path.exists(o.cfg.kill_switch_file)


def test_unwritable_kill_file_latches_in_memory_halt():
    # Kill path nested under an existing FILE -> makedirs raises -> the halt
    # must latch in memory and survive the per-tick kill-switch recompute.
    with tempfile.NamedTemporaryFile(delete=False) as f:
        blocked = os.path.join(f.name, "sub", "KILL")
    o = _orch({"o1": ("rejected", 0.0, 5.0)}, kill_file=blocked)
    o._reconcile_fills()
    assert o.risk.kill_switch is True
    assert o._forced_halt is True
    o._refresh_runtime_controls()   # would clear a non-latched in-memory halt
    assert o.risk.kill_switch is True
    os.remove(f.name)


def test_deleting_kill_file_resumes():
    o = _orch({"o1": ("rejected", 0.0, 5.0)})
    o._reconcile_fills()
    assert o.risk.kill_switch is True
    os.remove(o.cfg.kill_switch_file)   # the human acknowledgment
    o._refresh_runtime_controls()
    assert o.risk.kill_switch is False


# -- heartbeat gating --------------------------------------------------------- #

def test_heartbeat_pings_when_main_loop_fresh(monkeypatch):
    pings = []
    monkeypatch.setattr(orch_mod, "ping_heartbeat", lambda url: pings.append(url))
    o = _orch(heartbeat_url="http://hb.example/ping")
    o._maybe_heartbeat()
    assert pings == ["http://hb.example/ping"]


def test_heartbeat_withheld_when_main_loop_stale(monkeypatch):
    pings = []
    monkeypatch.setattr(orch_mod, "ping_heartbeat", lambda url: pings.append(url))
    o = _orch(heartbeat_url="http://hb.example/ping")
    o._last_main_tick = time.monotonic() - 10_000   # main loop long dead
    o._maybe_heartbeat()
    assert pings == []   # silence is the signal: the external monitor pages


def test_heartbeat_off_when_unconfigured(monkeypatch):
    pings = []
    monkeypatch.setattr(orch_mod, "ping_heartbeat", lambda url: pings.append(url))
    o = _orch(heartbeat_url="")
    o._maybe_heartbeat()
    assert pings == []


# -- dark-gap detection -------------------------------------------------------- #

def test_dark_gap_pages_and_restamps(monkeypatch):
    o = _orch()
    # Force the market-hours overlap so the test is deterministic regardless
    # of when the suite runs (paging is gated to gaps touching 09:25-16:05 ET).
    monkeypatch.setattr(type(o), "_overlaps_paging_hours",
                        staticmethod(lambda s, e: True))
    o._last_wall_tick = time.time() - 1_000   # ~17 min dark vs 150s threshold
    o._note_loop_tick()
    assert [c[0] for c in o.alerter.calls] == ["dark_gap"]
    assert time.time() - o._last_wall_tick < 5   # restamped; won't re-fire


def test_dark_gap_closed_market_logs_but_does_not_page(monkeypatch):
    # 2026-07-14: 10 overnight laptop-sleep gaps produced 2 CRITICAL pager
    # emails while the market was closed — pure noise; nothing was at risk.
    o = _orch()
    monkeypatch.setattr(type(o), "_overlaps_paging_hours",
                        staticmethod(lambda s, e: False))
    o._last_wall_tick = time.time() - 1_000
    o._note_loop_tick()
    assert o.alerter.calls == []
    assert time.time() - o._last_wall_tick < 5   # still restamped


def test_overlaps_paging_hours_clock_math():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from investment_strategy.orchestrator import Orchestrator
    et = ZoneInfo("America/New_York")
    ts = lambda *a: datetime(*a, tzinfo=et).timestamp()  # noqa: E731
    # Tue 2026-07-14 02:00-03:00 ET: fully closed -> no page.
    assert not Orchestrator._overlaps_paging_hours(
        ts(2026, 7, 14, 2, 0), ts(2026, 7, 14, 3, 0))
    # Tue 10:00-10:30 ET: inside the session -> page.
    assert Orchestrator._overlaps_paging_hours(
        ts(2026, 7, 14, 10, 0), ts(2026, 7, 14, 10, 30))
    # Gap spanning overnight INTO the open (05:00 -> 09:40) -> page.
    assert Orchestrator._overlaps_paging_hours(
        ts(2026, 7, 14, 5, 0), ts(2026, 7, 14, 9, 40))
    # Multi-day gap whose endpoints are both closed (Mon 20:00 -> Wed 06:00)
    # still covered Tuesday's session -> page.
    assert Orchestrator._overlaps_paging_hours(
        ts(2026, 7, 13, 20, 0), ts(2026, 7, 15, 6, 0))
    # Sat 10:00 -> Sun 10:00: weekend -> no page.
    assert not Orchestrator._overlaps_paging_hours(
        ts(2026, 7, 18, 10, 0), ts(2026, 7, 19, 10, 0))


def test_normal_tick_is_silent():
    o = _orch()
    o._note_loop_tick()
    assert o.alerter.calls == []


# -- single-instance lock (the 2026-07-07/08 double-bot incident) -------------- #

def test_second_instance_is_refused_and_lock_frees_on_close(tmp_path):
    from investment_strategy.__main__ import acquire_single_instance_lock

    state_file = str(tmp_path / "state" / "risk_state.json")
    first = acquire_single_instance_lock(state_file)
    assert first is not None
    # Same lockfile, second acquire -> refused (this is the double-start guard)
    assert acquire_single_instance_lock(state_file) is None
    # The pid of the holder is recorded for the error message
    assert (tmp_path / "state" / "bot.lock").read_text().strip() == str(os.getpid())
    # Releasing the fd (process death) frees the lock — no stale-lock lockout
    os.close(first)
    second = acquire_single_instance_lock(state_file)
    assert second is not None
    os.close(second)


# -- third-party log noise pinned to WARNING (RH MCP 405/reconnect spam) ------- #

def test_setup_logging_pins_noisy_third_party_loggers(monkeypatch):
    import logging
    from investment_strategy.__main__ import _setup_logging

    monkeypatch.setenv("LOG_DIR", "")            # no file sink in tests
    root = logging.getLogger()
    saved = root.handlers[:]
    try:
        _setup_logging()
        assert logging.getLogger("mcp.client.streamable_http").level == logging.WARNING
        assert logging.getLogger("httpx").level == logging.WARNING
    finally:
        root.handlers[:] = saved


# -- in-cycle liveness stamps (busy is not hung) -------------------------------- #

def test_stamp_liveness_refreshes_heartbeat_gate(monkeypatch):
    # A decision cycle blocks the main loop for minutes; progress stamps must
    # keep the gate open so the external monitor only sees silence on a REAL
    # hang (2026-07-14: every hourly cycle withheld the ping for ~3-5 min).
    pings = []
    monkeypatch.setattr(orch_mod, "ping_heartbeat", lambda url: pings.append(url))
    o = _orch(heartbeat_url="http://hb.example/ping")
    o._last_main_tick = time.monotonic() - 10_000   # mid-cycle, stale
    o._maybe_heartbeat()
    assert pings == []                               # gate correctly closed
    o._stamp_liveness()                              # forward progress
    o._maybe_heartbeat()
    assert pings == ["http://hb.example/ping"]       # gate reopened


def test_gather_reports_progress_per_provider():
    # The aggregator drives the longest cycle stretch; it must tick the
    # liveness callback after EVERY provider so no healthy path goes silent.
    from investment_strategy.signals.aggregator import SignalAggregator
    agg = SignalAggregator.__new__(SignalAggregator)
    provider = SimpleNamespace(safe_fetch=lambda syms: [])
    agg.market_wide = [provider]
    agg.per_symbol = [provider, provider]
    ticks = []
    agg.gather(["AAPL"], on_progress=lambda: ticks.append(1))
    assert len(ticks) == 3


# -- first decision tick after a machine reboot --------------------------------- #

def test_first_decision_due_even_on_fresh_boot(monkeypatch):
    # time.monotonic() counts from MACHINE boot. With _last_decision_at = 0.0
    # a bot started minutes after a reboot wasn't "due" until machine uptime
    # exceeded the whole decision interval (2026-07-14: a silent first hour).
    # The -inf sentinel makes the first tick unconditionally due.
    o = Orchestrator.__new__(Orchestrator)
    o.cfg = SimpleNamespace(decision_interval_s=3600)
    o._next_open_utc = None
    monkeypatch.setattr(orch_mod.time, "monotonic", lambda: 300.0)  # 5 min up
    o._last_decision_at = 0.0                 # the old init value: NOT due
    assert o._decision_due() is False         # documents the reboot bug
    o._last_decision_at = float("-inf")       # the fixed init value: due
    assert o._decision_due() is True
