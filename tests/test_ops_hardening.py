"""goGA GA-2.1/2.2 ops hardening: enforcing reconcile-halt, dead-man heartbeat
gating, and dark-gap detection."""
from __future__ import annotations

import os
import tempfile
import threading
import time
import uuid
from types import SimpleNamespace

import pytest

from investment_strategy import orchestrator as orch_mod
from investment_strategy.orchestrator import Orchestrator


class _RecordingAlerter:
    def __init__(self):
        self.calls = []

    def critical(self, key, subject, body, severity=None):
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
        self.exit_oids = set()   # (symbol, oid) pairs marked exit-side

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

    def add_pending_order(self, oid, symbol):
        # Jul 29: _requeue_unresolved persists each requeue immediately so the
        # watchdog's vanished-sweep sparing can see it mid-reconcile.
        if oid not in {p[0] for p in self.pending}:
            self.pending.append((oid, symbol))

    def note_exit_ledgered(self, symbol, oid):
        self.exit_oids.add((symbol, oid))

    def exit_was_ledgered(self, symbol, oid):
        return (symbol, oid) in self.exit_oids


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
    o._oid_retries = {}
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


def test_clean_fill_does_not_halt_and_clears():
    o = _orch({"o1": ("filled", 5.0, 5.0)})
    o._reconcile_fills()
    assert o.risk.kill_switch is False
    assert not os.path.exists(o.cfg.kill_switch_file)
    assert o.alerter.calls == []
    assert o.state.pending == []  # a filled order is resolved and dropped


def test_unknown_read_requeues_instead_of_dropping():
    # A failed broker read ("unknown") is NOT a confirmation — the old code
    # lumped it with "filled" and dropped the oid, so a rejected order caught by
    # a network blip left its phantom ledger intent uncorrected forever. It must
    # re-queue (bounded) and not halt on the first blip.
    o = _orch({"o2": ("unknown", 0.0, 0.0)})
    o._reconcile_fills()
    assert o.risk.kill_switch is False                 # one blip doesn't halt
    assert ("o2", "SYM0") in o.state.pending           # re-queued for next pass
    assert o._oid_retries.get("o2") == 1


def test_unresolved_oid_escalates_after_retry_bound():
    # After MAX_UNRESOLVED_RETRIES unreadable passes, give up: halt + flag so a
    # human reconciles it, rather than looping forever.
    o = _orch({"o2": ("unknown", 0.0, 0.0)})
    for _ in range(o.MAX_UNRESOLVED_RETRIES):
        o._pending_oids = [("o2", "SYM0")]             # re-present it each pass
        o._reconcile_fills()
    assert o.risk.kill_switch is False                 # not yet — exactly at bound
    o._pending_oids = [("o2", "SYM0")]
    o._reconcile_fills()                               # one past the bound
    assert o.risk.kill_switch is True
    os.remove(o.cfg.kill_switch_file)


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


# -- holiday-aware paging window (run-7 A2) ------------------------------------ #
# Labor Day 2026-09-07: 75 CRITICAL "positions unwatched during market hours"
# pages for a closed market — the window was weekday clock math. It now reads
# the decision loop's cached exchange calendar (state/session_calendar.json);
# the static signature (start_ts, end_ts) and the weekday fallback are kept.

def _sep_calendar(path=None):
    from datetime import date
    from investment_strategy.session_calendar import SessionCalendar
    cal = SessionCalendar(path)
    sessions = {d: ("09:30", "16:00") for d in [
        "2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04",
        "2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11",
        "2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18"]}
    assert cal.update(sessions, date(2026, 9, 1), date(2026, 9, 21), date(2026, 9, 11))
    return cal


def _et_ts(*a) -> float:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime(*a, tzinfo=ZoneInfo("America/New_York")).timestamp()


def test_overlaps_paging_hours_skips_holiday_in_cached_calendar():
    cal = _sep_calendar()
    # Labor Day Mon 2026-09-07 10:00 ET: cache says not a session -> no page.
    assert not Orchestrator._overlaps_paging_hours(
        _et_ts(2026, 9, 7, 10, 0), _et_ts(2026, 9, 7, 10, 0), cal)
    # Tue 2026-09-08 10:00 ET -> page.
    assert Orchestrator._overlaps_paging_hours(
        _et_ts(2026, 9, 8, 10, 0), _et_ts(2026, 9, 8, 10, 0), cal)
    # The pre-fix behaviour (no calendar) paged on Labor Day: still the
    # explicit fallback when nothing is cached.
    assert Orchestrator._overlaps_paging_hours(
        _et_ts(2026, 9, 7, 10, 0), _et_ts(2026, 9, 7, 10, 0), None)


def test_overlaps_paging_hours_honors_early_close():
    from datetime import date
    from investment_strategy.session_calendar import SessionCalendar
    cal = SessionCalendar(None)
    sessions = {"2026-11-20": ("09:30", "16:00"), "2026-11-23": ("09:30", "16:00"),
                "2026-11-24": ("09:30", "16:00"), "2026-11-25": ("09:30", "16:00"),
                "2026-11-27": ("09:30", "13:00"), "2026-11-30": ("09:30", "16:00")}
    assert cal.update(sessions, date(2026, 11, 20), date(2026, 12, 4), date(2026, 11, 25))
    # Fri 2026-11-27 closes 13:00 -> window ends 13:05.
    assert Orchestrator._overlaps_paging_hours(
        _et_ts(2026, 11, 27, 12, 50), _et_ts(2026, 11, 27, 12, 50), cal)
    assert not Orchestrator._overlaps_paging_hours(
        _et_ts(2026, 11, 27, 13, 20), _et_ts(2026, 11, 27, 15, 0), cal)


def test_overlaps_paging_hours_falls_back_outside_cache_range():
    cal = _sep_calendar()   # covers Sep 1-21 only
    # Tue 2026-10-06 10:00 ET: uncovered -> weekday math -> page.
    assert Orchestrator._overlaps_paging_hours(
        _et_ts(2026, 10, 6, 10, 0), _et_ts(2026, 10, 6, 10, 0), cal)
    # Sat 2026-10-10 -> no page.
    assert not Orchestrator._overlaps_paging_hours(
        _et_ts(2026, 10, 10, 10, 0), _et_ts(2026, 10, 10, 10, 0), cal)


def test_watchdog_blind_page_respects_labor_day(monkeypatch):
    """The production caller (_maybe_page_on_skip_run) reads the module-level
    active calendar registered by __init__ — no self reference, no network."""
    from investment_strategy import session_calendar as sc
    cal = _sep_calendar()
    prev = sc.active()
    fixed = _et_ts(2026, 9, 7, 10, 0)       # Labor Day, 10:00 ET
    monkeypatch.setattr(orch_mod.time, "time", lambda: fixed)
    try:
        sc.set_active(cal)
        o = _orch()
        o._watchdog_skips = o.WATCHDOG_SKIP_ESCALATE
        o._blind_paged_at = 0
        o._maybe_page_on_skip_run()
        assert o.alerter.calls == [], "Labor Day is not market hours"
        sc.set_active(None)                 # no cache: weekday math pages
        o._blind_paged_at = 0
        o._maybe_page_on_skip_run()
        assert [c[0] for c in o.alerter.calls] == ["watchdog_blind"]
    finally:
        sc.set_active(prev)


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
    # The cadence now runs on WALL clock (time.time), which survives host sleep;
    # the old monotonic clock froze during suspend and stalled the schedule. The
    # -inf sentinel keeps the first tick unconditionally due, and wall-clock is
    # immune to the old monotonic-boot bug entirely (epoch 0 is far in the past).
    o = Orchestrator.__new__(Orchestrator)
    o.cfg = SimpleNamespace(decision_interval_s=3600)
    o._next_open_utc = None
    monkeypatch.setattr(orch_mod.time, "time", lambda: 1_000_000.0)
    o._last_decision_at = float("-inf")           # init sentinel: due
    assert o._decision_due() is True
    o._last_decision_at = 1_000_000.0 - 100.0     # 100s ago, under the interval
    assert o._decision_due() is False
    o._last_decision_at = 1_000_000.0 - 4000.0    # a full interval+ ago: due
    assert o._decision_due() is True
    # A wall-clock jump forward (host resumed from a long sleep) makes it due —
    # the whole point of moving off monotonic.
    o._last_decision_at = 1_000_000.0 - 100.0
    monkeypatch.setattr(orch_mod.time, "time", lambda: 1_000_000.0 + 7200.0)
    assert o._decision_due() is True


# -- exit-side intents never trip the buy halt (Jul 27) ------------------------ #
def test_expired_exit_order_writes_correction_without_halting():
    # An option DAY close expiring at the bell is a routine overnight pattern
    # the watchdog resubmits itself; it must true up the ledger (correction)
    # WITHOUT the all-buys kill switch or a page.
    o = _orch({"x1": ("expired", 0.0, 900.0)})
    o._pending_oids = [("x1", "T")]
    o.state.note_exit_ledgered("T", "x1")
    o._reconcile_fills()
    assert len(o.ledger.records) == 1                       # correction written
    assert o.ledger.records[0].action == "correct"
    assert o.risk.kill_switch is False                      # no halt
    assert not os.path.exists(o.cfg.kill_switch_file)
    assert o.alerter.calls == []                            # no page


def test_expired_entry_order_still_halts():
    # The GA-2.1 phantom-BUY class keeps its teeth: entry-side divergence
    # halts and pages exactly as before.
    o = _orch({"b1": ("expired", 0.0, 5.0)})
    o._pending_oids = [("b1", "QQQ")]
    o._reconcile_fills()
    assert o.risk.kill_switch is True
    assert os.path.exists(o.cfg.kill_switch_file)
    os.remove(o.cfg.kill_switch_file)


# -- flatten script: leg order + per-symbol retry (Aug 31 2026 abort) --------- #
# scripts/flatten_and_restart.py used close_all_positions: 17 closes fired at
# once, Alpaca rejected the long IWM 295P (the cover of the short IWM 280P —
# selling it first would leave the short naked), the per-position response
# was discarded and nothing retried, so after 15 min the script quit with
# "NOT FLAT — 1 positions remain: IWM260930P00295000" and the run-6 switch
# needed a manual sell (logs/flatten_restart.log 2026-08-31 11:41-11:56 ET).

def _flatten_script(monkeypatch):
    """Import scripts/flatten_and_restart with the hold marker redirected to a
    temp path (never the real state/flatten.hold), sleeps no-op'd and say()
    captured. Importing the script has no side effects (module constants only)."""
    import sys
    from pathlib import Path
    scripts_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import flatten_and_restart as fr  # noqa: E402
    monkeypatch.setattr(fr, "HOLD_MARKER", Path(tempfile.mkdtemp()) / "flatten.hold")
    monkeypatch.setattr(fr.time, "sleep", lambda s: None)
    lines = []
    monkeypatch.setattr(fr, "say", lines.append)
    return fr, lines


def _pos(symbol, qty, asset_class="us_equity"):
    # Alpaca Position rows: qty is a STRING, negative for a short option leg.
    return SimpleNamespace(symbol=symbol, qty=str(qty), asset_class=asset_class)


def _aug31_book():
    # The 17 rows the Aug 31 flatten listed, in the order Alpaca returned them.
    return [
        _pos("AAPL", 176), _pos("ASND", 169), _pos("BHVN", 934), _pos("HLF", 1819),
        _pos("HOOD", 1139), _pos("INTC", 340),
        _pos("IWM260930P00280000", -30, "us_option"),   # short leg of the put spread
        _pos("IWM260930P00295000", 30, "us_option"),    # its cover (long leg)
        _pos("NEXA", 1812), _pos("NOK", 2904), _pos("NVDA", 181),
        _pos("NVDA261016C00180000", 1, "us_option"),
        _pos("PYPL", 554), _pos("QQQ", "207.481670383"), _pos("SMCI", 1576),
        _pos("SOFI", 1646), _pos("SPCX", 216),
    ]


class _FakeTC:
    """Scripted TradingClient. An accepted close_position removes the row at
    once (marketable closes fill in seconds in RTH). `naked_guard` mirrors the
    Alpaca options rule that bit on Aug 31: selling a LONG option while a
    SHORT option on the same underlying is still held is rejected. `reject`
    maps symbol -> how many closes to refuse first (-1 = every time)."""

    def __init__(self, positions, *, naked_guard=False, reject=None):
        self.positions = {p.symbol: p for p in positions}
        self.naked_guard = naked_guard
        self.reject = dict(reject or {})
        self.closes = []                 # every close_position call, in order
        self.close_all_calls = 0

    def get_orders(self):
        return []

    def cancel_orders(self):
        pass

    def get_all_positions(self):
        return list(self.positions.values())

    def close_all_positions(self, cancel_orders=None):
        self.close_all_calls += 1
        raise AssertionError("close_all_positions fires every leg at once — the Aug 31 bug")

    @staticmethod
    def _underlying(symbol):
        return symbol[:-15] if len(symbol) > 15 else symbol

    def close_position(self, symbol):
        from alpaca.common.exceptions import APIError
        self.closes.append(symbol)
        p = self.positions[symbol]
        n = self.reject.get(symbol, 0)
        if n:
            if n > 0:
                self.reject[symbol] = n - 1
            raise APIError('{"code":40310000,"message":"insufficient qty available for order"}')
        if self.naked_guard and float(p.qty) > 0 and p.asset_class == "us_option":
            und = self._underlying(symbol)
            if any(float(q.qty) < 0 and q.asset_class == "us_option"
                   and self._underlying(s) == und
                   for s, q in self.positions.items()):
                raise APIError('{"code":40310000,"message":"selling this leg would leave an uncovered short option"}')
        del self.positions[symbol]
        return SimpleNamespace(id=f"oid-{symbol}", status="accepted")


def test_order_close_legs_short_options_first(monkeypatch):
    fr, _ = _flatten_script(monkeypatch)
    book = _aug31_book() + [_pos("XYZ", -5)]        # plus a short stock row
    ordered = [p.symbol for p in fr.order_close_legs(book)]
    # Short option leg first, short stock next, then long options, then equities.
    assert ordered[0] == "IWM260930P00280000"
    assert ordered[1] == "XYZ"
    assert ordered[2:4] == ["IWM260930P00295000", "NVDA261016C00180000"]
    assert ordered.index("IWM260930P00280000") < ordered.index("IWM260930P00295000")
    assert all(p.asset_class == "us_equity" for p in fr.order_close_legs(book)[4:])
    assert sorted(ordered) == sorted(p.symbol for p in book)   # nothing lost/dup'd
    assert [p.symbol for p in book][0] == "AAPL"                # pure: input untouched
    # Rows without asset_class fall back to the OCC symbol shape.
    bare = [SimpleNamespace(symbol="AAPL", qty="1"),
            SimpleNamespace(symbol="IWM260930P00280000", qty="-1")]
    assert [p.symbol for p in fr.order_close_legs(bare)][0] == "IWM260930P00280000"


def test_flatten_closes_short_leg_before_its_cover(monkeypatch):
    # The Aug 31 book under the naked-short guard: the old close-all would have
    # left the 295P; the new choreography closes the 280P first, waits for it to
    # leave the book, then sells the 295P — flat in ONE round, no close_all.
    fr, lines = _flatten_script(monkeypatch)
    tc = _FakeTC(_aug31_book(), naked_guard=True)
    assert fr.flatten(tc) is True
    assert tc.close_all_calls == 0
    assert tc.closes.index("IWM260930P00280000") < tc.closes.index("IWM260930P00295000")
    assert len(tc.closes) == 17                          # one close per row, no retries
    assert not any("REJECTED" in ln for ln in lines)
    assert any("close round 1/3" in ln for ln in lines)
    assert not any("close round 2/3" in ln for ln in lines)
    assert any("short close IWM260930P00280000 qty=-30 -> order=oid-IWM260930P00280000" in ln
               for ln in lines)
    assert lines[-1] == "account is flat: no orders, no positions"


def test_flatten_retries_rejected_leg_and_stops_when_flat(monkeypatch):
    # A transient per-symbol rejection is printed with the broker's body and
    # retried next round; the loop stops as soon as the book is flat.
    fr, lines = _flatten_script(monkeypatch)
    tc = _FakeTC(_aug31_book(), reject={"AAPL": 1})
    assert fr.flatten(tc) is True
    assert tc.closes.count("AAPL") == 2
    assert len(tc.closes) == 18                          # 17 + the one retry
    rej = [ln for ln in lines if "REJECTED" in ln]
    assert len(rej) == 1
    assert "long close AAPL qty=176 REJECTED: APIError HTTP ?: insufficient qty available" in rej[0]
    assert any("round 1/3: 1 remain: AAPL" in ln for ln in lines)
    assert any("close round 2/3: 1 positions" in ln for ln in lines)
    assert not any("close round 3/3" in ln for ln in lines)


def test_flatten_returns_false_on_persistent_leftover(monkeypatch):
    # A leg the broker refuses every time is retried CLOSE_ROUNDS times, each
    # refusal logged, and the script still reports NOT FLAT (exit-1 path in
    # _flatten_reset_restart, bot NOT started) — never a silent success.
    fr, lines = _flatten_script(monkeypatch)
    tc = _FakeTC(_aug31_book(), reject={"IWM260930P00295000": -1})
    assert fr.flatten(tc) is False
    assert tc.closes.count("IWM260930P00295000") == fr.CLOSE_ROUNDS == 3
    rej = [ln for ln in lines
           if "long close IWM260930P00295000 qty=30 REJECTED: APIError HTTP ?: " in ln]
    assert len(rej) == 3                                 # every refusal logged with its body
    assert lines[-1].startswith("NOT FLAT — 1 positions remain after 3 rounds: IWM260930P00295000")
    assert len(tc.positions) == 1                        # everything else got closed


class _SlowFillTC(_FakeTC):
    """A close the broker ACCEPTS but does not fill within CLOSE_WAIT_S (an
    illiquid option cover): the order stays open, the position stays on the
    book for `slow[symbol]` more position polls, and a SECOND close for that
    symbol is rejected on qty available — the open order holds it (what
    Alpaca does to a duplicate close)."""

    def __init__(self, positions, *, slow):
        super().__init__(positions)
        self.slow = dict(slow)          # symbol -> position polls until the fill lands
        self.working = {}               # symbol -> polls left while the order is open

    def get_orders(self):
        return [SimpleNamespace(symbol=s, id=f"oid-{s}") for s in self.working]

    def get_all_positions(self):
        for s in list(self.working):
            self.working[s] -= 1
            if self.working[s] <= 0:
                del self.working[s]
                del self.positions[s]
        return list(self.positions.values())

    def close_position(self, symbol):
        from alpaca.common.exceptions import APIError
        if symbol in self.working:
            self.closes.append(symbol)
            raise APIError('{"code":40310000,"message":"insufficient qty available for order (requested: 30, available: 0)"}')
        if symbol in self.slow:
            self.closes.append(symbol)
            self.working[symbol] = self.slow.pop(symbol)
            return SimpleNamespace(id=f"oid-{symbol}", status="accepted")
        return super().close_position(symbol)


def test_flatten_round_two_waits_on_a_working_close_instead_of_resending(monkeypatch):
    # Review finding (run-7 C2): round 2 re-sent close_position for a symbol
    # whose round-1 close was accepted but unfilled past CLOSE_WAIT_S; Alpaca
    # rejects the duplicate on qty available (the open order holds it), so
    # the leg kept "failing" and the script reported NOT FLAT although the
    # first order fills minutes later. Now a leftover with a working close
    # order is skipped and waited on.
    fr, lines = _flatten_script(monkeypatch)
    polls_per_wait = fr.CLOSE_WAIT_S // fr.CLOSE_POLL_S      # 36 (+1 initial poll)
    book = [_pos("AAPL", 176), _pos("IWM260930P00295000", 30, "us_option")]
    tc = _SlowFillTC(book, slow={"IWM260930P00295000": polls_per_wait + 14})
    assert fr.flatten(tc) is True
    assert tc.closes == ["IWM260930P00295000", "AAPL"]      # ONE close each, never re-sent
    assert not any("REJECTED" in ln for ln in lines)
    assert any("long closes still open after 180s: IWM260930P00295000" in ln for ln in lines)
    assert any("round 1/3: 1 remain: IWM260930P00295000" in ln for ln in lines)
    assert any(ln == "  long close IWM260930P00295000 qty=30 SKIPPED: a close order is "
                     "still working from an earlier round — waiting on it" for ln in lines)
    assert any("close round 2/3: 1 positions" in ln for ln in lines)
    assert not any("close round 3/3" in ln for ln in lines)
    assert lines[-1] == "account is flat: no orders, no positions"
    # The fake really would have rejected a duplicate (the pre-fix path).
    from alpaca.common.exceptions import APIError
    tc2 = _SlowFillTC([_pos("IWM260930P00295000", 30, "us_option")],
                      slow={"IWM260930P00295000": 99})
    tc2.close_position("IWM260930P00295000")
    with pytest.raises(APIError):
        tc2.close_position("IWM260930P00295000")
    # Round 1 never consults the order list (step 2 just cancelled everything);
    # an unreadable order list in a later round falls back to re-sending.
    tc3 = _FakeTC(_aug31_book())
    tc3.get_orders = lambda: (_ for _ in ()).throw(RuntimeError("orders endpoint down"))
    assert fr._working_close_orders(tc3) == set()
    assert any("open-order check failed (RuntimeError HTTP ?: orders endpoint down)" in ln
               for ln in lines)


def test_flatten_refuses_non_paper_base_url(monkeypatch):
    # PAPER-ONLY guard is untouched: a live endpoint exits 1 before the
    # open-wait, before the hold marker, before any broker call.
    fr, lines = _flatten_script(monkeypatch)
    monkeypatch.setattr(fr, "env_val", lambda n: "https://api.alpaca.markets")
    monkeypatch.setattr(fr, "wait_until", lambda t: (_ for _ in ()).throw(AssertionError("waited")))
    monkeypatch.setattr(fr, "_flatten_reset_restart",
                        lambda: (_ for _ in ()).throw(AssertionError("flattened")))
    assert fr.main() == 1
    assert any(ln.startswith("REFUSING: ALPACA_BASE_URL is not the paper endpoint") for ln in lines)
    assert not fr.HOLD_MARKER.exists()
