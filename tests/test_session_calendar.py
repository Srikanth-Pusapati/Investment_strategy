"""Run-7 A2: holiday-aware paging window (investment_strategy/session_calendar.py).

Labor Day 2026-09-07: the bot logged "Market closed; skipping decision
cycle." all day, yet a DNS outage produced 75 CRITICAL "positions unwatched
during market hours" pages and ops/deadman.py ran 80 in-hours checks, because
both windows were weekday clock math. These tests pin the cached-calendar
semantics: holidays absent from the cache are not paging hours, early closes
shrink the window, a missing/stale cache falls back to weekday math with ONE
warning per date, the cache is refreshed by the decision loop only and
survives a restart.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from investment_strategy import session_calendar as sc
from investment_strategy.session_calendar import SessionCalendar, paging_overlap

ET = ZoneInfo("America/New_York")

# Sep 2026: Labor Day Mon Sep 7 absent; weekends absent.
SEP_SESSIONS = {
    d: ("09:30", "16:00")
    for d in ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04",
              "2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11",
              "2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17",
              "2026-09-18"]
}
SEP_START, SEP_END = date(2026, 9, 1), date(2026, 9, 21)
# Thanksgiving week: Fri Nov 27 is a 13:00 early close; Thu Nov 26 absent.
NOV_SESSIONS = {
    "2026-11-20": ("09:30", "16:00"), "2026-11-23": ("09:30", "16:00"),
    "2026-11-24": ("09:30", "16:00"), "2026-11-25": ("09:30", "16:00"),
    "2026-11-27": ("09:30", "13:00"), "2026-11-30": ("09:30", "16:00"),
    "2026-12-01": ("09:30", "16:00"),
}
NOV_START, NOV_END = date(2026, 11, 20), date(2026, 12, 4)


def _ts(*a) -> float:
    return datetime(*a, tzinfo=ET).timestamp()


def _cal(path=None, sessions=SEP_SESSIONS, start=SEP_START, end=SEP_END,
         today=date(2026, 9, 11)) -> SessionCalendar:
    cal = SessionCalendar(path)
    assert cal.update(sessions, start, end, today) is True
    return cal


# -- pure lookups ------------------------------------------------------------- #

def test_labor_day_is_not_a_session_when_cached():
    cal = _cal()
    assert cal.covers(date(2026, 9, 7))
    assert cal.is_session(date(2026, 9, 7)) is False
    assert cal.paging_window(date(2026, 9, 7)) is None
    assert cal.in_paging_window(datetime(2026, 9, 7, 10, 0, tzinfo=ET)) is False
    # Tue Sep 8: full session -> open-5 .. close+5.
    win = cal.paging_window(date(2026, 9, 8))
    assert win == (datetime(2026, 9, 8, 9, 25, tzinfo=ET),
                   datetime(2026, 9, 8, 16, 5, tzinfo=ET))
    assert cal.in_paging_window(datetime(2026, 9, 8, 10, 0, tzinfo=ET))
    assert not cal.in_paging_window(datetime(2026, 9, 8, 16, 6, tzinfo=ET))
    # Weekend inside the range: absent == not a session, no fallback warning.
    assert cal.is_session(date(2026, 9, 12)) is False


def test_early_close_shrinks_window():
    cal = _cal(sessions=NOV_SESSIONS, start=NOV_START, end=NOV_END,
               today=date(2026, 11, 25))
    assert cal.paging_window(date(2026, 11, 27)) == (
        datetime(2026, 11, 27, 9, 25, tzinfo=ET),
        datetime(2026, 11, 27, 13, 5, tzinfo=ET))
    assert cal.in_paging_window(datetime(2026, 11, 27, 12, 50, tzinfo=ET))
    assert not cal.in_paging_window(datetime(2026, 11, 27, 13, 20, tzinfo=ET))
    assert paging_overlap(_ts(2026, 11, 27, 12, 50), _ts(2026, 11, 27, 12, 55), cal)
    assert not paging_overlap(_ts(2026, 11, 27, 13, 10), _ts(2026, 11, 27, 15, 0), cal)
    # Thanksgiving Thu Nov 26 absent -> not paging hours at 10:00.
    assert not paging_overlap(_ts(2026, 11, 26, 10, 0), _ts(2026, 11, 26, 10, 0), cal)


def test_utc_instant_is_mapped_to_et_date_window():
    cal = _cal()
    # 2026-09-08 14:00Z == 10:00 ET (EDT) -> inside; 2026-09-08 20:30Z == 16:30 ET -> outside.
    from datetime import timezone
    assert cal.in_paging_window(datetime(2026, 9, 8, 14, 0, tzinfo=timezone.utc))
    assert not cal.in_paging_window(datetime(2026, 9, 8, 20, 30, tzinfo=timezone.utc))


# -- fallback ----------------------------------------------------------------- #

def test_missing_cache_falls_back_to_weekday_math(tmp_path, caplog):
    cal = SessionCalendar(tmp_path / "nope.json")
    assert not cal.covers(date(2026, 9, 7))
    with caplog.at_level(logging.WARNING, logger="session_calendar"):
        # The pre-fix behaviour, now explicitly the fallback: Labor Day is a
        # weekday, so with no cache it IS paging hours.
        assert cal.is_session(date(2026, 9, 7)) is True
        assert cal.paging_window(date(2026, 9, 7)) == (
            datetime(2026, 9, 7, 9, 25, tzinfo=ET),
            datetime(2026, 9, 7, 16, 5, tzinfo=ET))
        cal.is_session(date(2026, 9, 7))                    # repeat, same day
        cal.paging_window(date(2026, 9, 7))                 # and again
    warns = [r for r in caplog.records if "Session calendar fallback" in r.getMessage()]
    assert len(warns) == 1, "one WARNING per ET date, not one per watchdog tick"
    with caplog.at_level(logging.WARNING, logger="session_calendar"):
        assert cal.is_session(date(2026, 9, 12)) is False   # Saturday
        cal.is_session(date(2026, 9, 7))                    # alternating dates
        cal.is_session(date(2026, 9, 12))                   # don't re-warn
    warns = [r for r in caplog.records if "Session calendar fallback" in r.getMessage()]
    assert len(warns) == 2
    # A successful refresh clears the memory so a later lapse warns again.
    assert cal.update(SEP_SESSIONS, SEP_START, SEP_END, date(2026, 9, 11))
    with caplog.at_level(logging.WARNING, logger="session_calendar"):
        assert cal.is_session(date(2026, 10, 6)) is True    # outside range
    warns = [r for r in caplog.records if "Session calendar fallback" in r.getMessage()]
    assert len(warns) == 3


def test_date_outside_cached_range_falls_back(caplog):
    cal = _cal()
    with caplog.at_level(logging.WARNING, logger="session_calendar"):
        assert cal.covers(date(2026, 10, 6)) is False
        assert cal.is_session(date(2026, 10, 6)) is True     # Tue, weekday math
        assert cal.is_session(date(2026, 10, 10)) is False   # Sat
    assert any("Session calendar fallback" in r.getMessage() for r in caplog.records)
    # Covered dates stay authoritative (no fallback) at the same time.
    assert cal.is_session(date(2026, 9, 7)) is False


def test_corrupt_cache_file_falls_back(tmp_path):
    p = tmp_path / "session_calendar.json"
    p.write_text("{not json", encoding="utf-8")
    cal = SessionCalendar(p)
    assert not cal.covers(date(2026, 9, 8))
    assert cal.needs_refresh(date(2026, 9, 8))
    assert cal.is_session(date(2026, 9, 8)) is True  # weekday fallback


def test_paging_overlap_without_calendar_is_weekday_math():
    # Same cases as test_overlaps_paging_hours_clock_math, through the module.
    assert not paging_overlap(_ts(2026, 7, 14, 2, 0), _ts(2026, 7, 14, 3, 0), None)
    assert paging_overlap(_ts(2026, 7, 14, 10, 0), _ts(2026, 7, 14, 10, 30), None)
    assert paging_overlap(_ts(2026, 7, 14, 5, 0), _ts(2026, 7, 14, 9, 40), None)
    assert paging_overlap(_ts(2026, 7, 13, 20, 0), _ts(2026, 7, 15, 6, 0), None)
    assert not paging_overlap(_ts(2026, 7, 18, 10, 0), _ts(2026, 7, 19, 10, 0), None)


def test_paging_overlap_multi_day_gap_across_labor_day():
    cal = _cal()
    # Fri Sep 4 20:00 -> Mon Sep 7 (Labor Day) 20:00: no session inside.
    assert not paging_overlap(_ts(2026, 9, 4, 20, 0), _ts(2026, 9, 7, 20, 0), cal)
    # Without the cache the same gap paged (Monday 09:30 fallback open inside).
    assert paging_overlap(_ts(2026, 9, 4, 20, 0), _ts(2026, 9, 7, 20, 0), None)
    # Extend to Tue Sep 8 06:00 -> still closed; to 09:40 -> Sep 8 open inside.
    assert not paging_overlap(_ts(2026, 9, 4, 20, 0), _ts(2026, 9, 8, 6, 0), cal)
    assert paging_overlap(_ts(2026, 9, 4, 20, 0), _ts(2026, 9, 8, 9, 40), cal)


def test_paging_overlap_uses_active_module_calendar():
    cal = _cal()
    prev = sc.active()
    try:
        sc.set_active(cal)
        assert not paging_overlap(_ts(2026, 9, 7, 10, 0), _ts(2026, 9, 7, 10, 0))
        assert paging_overlap(_ts(2026, 9, 8, 10, 0), _ts(2026, 9, 8, 10, 0))
        sc.set_active(None)
        assert paging_overlap(_ts(2026, 9, 7, 10, 0), _ts(2026, 9, 7, 10, 0))
    finally:
        sc.set_active(prev)


# -- update / persistence ----------------------------------------------------- #

def test_update_rejects_small_or_garbage_response():
    cal = _cal()
    before = cal.session_times(date(2026, 9, 8))
    assert cal.update({"2026-09-08": ("09:30", "16:00")}, SEP_START, SEP_END,
                      date(2026, 9, 12)) is False
    assert cal.update({d: ("x", "y") for d in SEP_SESSIONS}, SEP_START, SEP_END,
                      date(2026, 9, 12)) is False
    assert cal.update(SEP_SESSIONS, SEP_END, SEP_START, date(2026, 9, 12)) is False
    assert cal.session_times(date(2026, 9, 8)) == before
    assert cal.fetched_day == "2026-09-11"   # the rejected fetch did not stamp


def test_needs_refresh_once_per_day():
    cal = _cal(today=date(2026, 9, 11))
    assert cal.needs_refresh(date(2026, 9, 11)) is False
    assert cal.needs_refresh(date(2026, 9, 12)) is True


def test_cache_survives_restart_and_file_shape(tmp_path):
    p = tmp_path / "state" / "session_calendar.json"
    _cal(p)
    raw = json.loads(p.read_text(encoding="utf-8"))
    assert set(raw) == {"fetched", "fetched_day", "start", "end", "sessions"}
    assert raw["fetched_day"] == "2026-09-11"
    assert raw["start"] == "2026-09-01" and raw["end"] == "2026-09-21"
    assert raw["sessions"]["2026-09-08"] == {"open": "09:30", "close": "16:00"}
    assert "2026-09-07" not in raw["sessions"]
    assert not p.with_suffix(".tmp").exists()   # tmp + os.replace, no debris
    # "Restart": a fresh instance on the same path answers without a fetch.
    cal2 = SessionCalendar(p)
    assert cal2.needs_refresh(date(2026, 9, 11)) is False
    assert cal2.is_session(date(2026, 9, 7)) is False
    assert cal2.is_session(date(2026, 9, 8)) is True
    assert cal2.fetched, "fetch stamp survives the reload"


# -- broker read shaping ------------------------------------------------------ #

def test_alpaca_get_session_calendar_shapes_rows_and_fails_to_none():
    from investment_strategy.execution.alpaca_client import AlpacaClient
    c = AlpacaClient.__new__(AlpacaClient)
    rows = [
        SimpleNamespace(date=date(2026, 9, 8), open=datetime(2026, 9, 8, 9, 30),
                        close=datetime(2026, 9, 8, 16, 0)),
        SimpleNamespace(date=date(2026, 11, 27), open=datetime(2026, 11, 27, 9, 30),
                        close=datetime(2026, 11, 27, 13, 0)),
    ]
    c.trading = SimpleNamespace(get_calendar=lambda req: rows)
    out = c.get_session_calendar(date(2026, 9, 1), date(2026, 12, 1))
    assert out == {"2026-09-08": ("09:30", "16:00"), "2026-11-27": ("09:30", "13:00")}

    def _boom(req):
        raise RuntimeError("500")
    c.trading = SimpleNamespace(get_calendar=_boom)
    assert c.get_session_calendar(date(2026, 9, 1), date(2026, 9, 21)) is None


# -- orchestrator wiring: decision loop refreshes, watchdog never fetches ----- #

def _orch(tmp_path, fetch):
    from investment_strategy.orchestrator import Orchestrator
    o = Orchestrator.__new__(Orchestrator)
    o.cfg = SimpleNamespace(monitor_interval_s=30)
    o._session_calendar = SessionCalendar(tmp_path / "session_calendar.json")
    o.broker = SimpleNamespace(get_session_calendar=fetch)
    return o


def _weekday_sessions(center: date, span: int, skip: set[date] = frozenset()):
    return {
        (center + timedelta(days=i)).isoformat(): ("09:30", "16:00")
        for i in range(-span, span + 1)
        if (center + timedelta(days=i)).weekday() < 5
        and (center + timedelta(days=i)) not in skip
    }


def test_refresh_is_once_per_day_and_fails_open(tmp_path, caplog):
    calls = []
    today = datetime.now(ET).date()

    def fetch(start, end):
        calls.append((start, end))
        return _weekday_sessions(today, sc.REFRESH_SPAN_DAYS)

    o = _orch(tmp_path, fetch)
    o._refresh_session_calendar()
    o._refresh_session_calendar()
    assert len(calls) == 1
    assert calls[0] == (today - timedelta(days=sc.REFRESH_SPAN_DAYS),
                        today + timedelta(days=sc.REFRESH_SPAN_DAYS))
    assert o._session_calendar.covers(today)
    assert (tmp_path / "session_calendar.json").exists()

    # A failed fetch: previous cache untouched, still due (retries next cycle).
    def boom(start, end):
        raise RuntimeError("no network")
    o2 = _orch(tmp_path / "b", boom)
    with caplog.at_level(logging.WARNING, logger="orchestrator"):
        o2._refresh_session_calendar()        # never raises into the cycle
    assert not o2._session_calendar.covers(today)
    assert o2._session_calendar.needs_refresh(today)
    assert any("Session calendar refresh" in r.getMessage() for r in caplog.records)

    # A truncated response (< MIN_SESSIONS) is rejected, not adopted.
    o3 = _orch(tmp_path / "c", lambda s, e: {today.isoformat(): ("09:30", "16:00")})
    o3._refresh_session_calendar()
    assert not o3._session_calendar.covers(today)


def test_decision_cycle_refreshes_but_watchdog_paths_never_fetch(tmp_path, monkeypatch):
    calls = []
    today = datetime.now(ET).date()
    o = _orch(tmp_path, lambda s, e: calls.append(1) or _weekday_sessions(today, 10))
    o.broker.is_market_open = lambda: False
    o.broker.next_market_open = lambda: None
    for name in ("_refresh_closing_snapshot", "_maybe_run_postmortem",
                 "_maybe_run_weekly_autotune", "_stamp_liveness"):
        monkeypatch.setattr(o, name, lambda *a, **k: None)
    o.run_decision_cycle()
    o.run_decision_cycle()
    assert calls == [1], "refresh runs from the decision cycle, once per day"

    # Watchdog-thread paths only READ the cache: the broker is now a tripwire.
    def tripwire(*a, **k):
        raise AssertionError("network call on the watchdog thread")
    o.broker.get_session_calendar = tripwire
    o.alerter = SimpleNamespace(critical=lambda *a, **k: True)
    o._last_wall_tick = time.time() - 1_000
    o._note_loop_tick()                      # dark-gap path
    o._watchdog_skips = o.WATCHDOG_SKIP_ESCALATE
    o._blind_paged_at = 0
    o._maybe_page_on_skip_run()              # watchdog-blind path
    assert calls == [1]


def test_decision_cycle_stamps_liveness_right_after_the_calendar_refresh(tmp_path, monkeypatch):
    # Review finding (run-7 A2): while the calendar fetch keeps failing it
    # re-runs every cycle (3 tries x HTTP timeout + 5xx sleeps, ~60 s worst
    # case) BEFORE is_market_open — an unstamped span the heartbeat gate
    # (150 s) would count. The cycle stamps liveness right after it.
    order = []

    def boom(s, e):
        order.append("fetch")
        raise RuntimeError("calendar endpoint down")
    o = _orch(tmp_path, boom)
    o.broker.is_market_open = lambda: order.append("clock") or False
    o.broker.next_market_open = lambda: None
    for name in ("_refresh_closing_snapshot", "_maybe_run_postmortem",
                 "_maybe_run_weekly_autotune"):
        monkeypatch.setattr(o, name, lambda *a, **k: None)
    monkeypatch.setattr(o, "_stamp_liveness", lambda: order.append("stamp"))
    o.run_decision_cycle()
    assert order[:3] == ["fetch", "stamp", "clock"]


def test_bare_orchestrator_without_calendar_is_a_noop():
    from investment_strategy.orchestrator import Orchestrator
    o = Orchestrator.__new__(Orchestrator)
    assert o._session_calendar is None       # class-level default for fixtures
    o._refresh_session_calendar()            # no attribute error, no fetch
