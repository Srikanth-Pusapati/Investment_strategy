"""Tests for the earnings-calendar helper feeding the risk blackout guard.

Pure logic, no network: the yfinance-backed `_lookup` is stubbed so we assert the
date math (`_to_days`), future-only filtering, per-cycle caching, and the
fail-open None contract — all without hitting the network.

Runnable two ways:
    .venv/bin/python tests/test_earnings.py     # standalone, no pytest
    .venv/bin/pytest tests/                      # if pytest is installed
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.earnings import EarningsCalendar

_TODAY = date(2026, 6, 28)


# --------------------------------------------------------------------------- #
# Date math: _to_days across the shapes yfinance can return
# --------------------------------------------------------------------------- #
def test_to_days_from_date():
    assert EarningsCalendar._to_days(date(2026, 7, 1), _TODAY) == 3


def test_to_days_from_datetime():
    assert EarningsCalendar._to_days(datetime(2026, 7, 1, 16, 0), _TODAY) == 3


def test_to_days_from_iso_string():
    assert EarningsCalendar._to_days("2026-07-01", _TODAY) == 3


def test_to_days_past_is_negative():
    assert EarningsCalendar._to_days("2026-06-20", _TODAY) == -8


def test_to_days_garbage_is_none():
    assert EarningsCalendar._to_days("not-a-date", _TODAY) is None
    assert EarningsCalendar._to_days(None, _TODAY) is None


# --------------------------------------------------------------------------- #
# Lookup: future-only, nearest future, fail-open, caching
# --------------------------------------------------------------------------- #
class _StubCal(EarningsCalendar):
    """Stand in for the yfinance call with a canned list of candidate dates."""
    def __init__(self, candidates):
        super().__init__()
        self._candidates = candidates
        self.lookups = 0

    def _lookup(self, symbol, today):
        self.lookups += 1
        future = [
            d for d in (self._to_days(c, today) for c in self._candidates)
            if d is not None and d >= 0
        ]
        return min(future) if future else None


def test_picks_nearest_future_date_ignoring_past():
    cal = _StubCal(["2026-06-10", "2026-07-05", "2026-07-20"])  # one past, two future
    assert cal.days_until_earnings("AAPL", _TODAY) == 7


def test_all_past_returns_none():
    cal = _StubCal(["2026-01-01", "2026-06-01"])
    assert cal.days_until_earnings("AAPL", _TODAY) is None


def test_empty_returns_none():
    assert _StubCal([]).days_until_earnings("AAPL", _TODAY) is None


def test_cached_within_cycle_then_refreshed():
    cal = _StubCal(["2026-07-05"])
    cal.days_until_earnings("AAPL", _TODAY)
    cal.days_until_earnings("AAPL", _TODAY)
    assert cal.lookups == 1                 # second call served from cache
    cal.new_cycle()
    cal.days_until_earnings("AAPL", _TODAY)
    assert cal.lookups == 2                 # cache cleared -> re-fetched


def test_real_lookup_fails_open_when_yfinance_raises():
    # The REAL _lookup must swallow any yfinance/network error and return None so
    # the risk guard fails OPEN (never blocks a trade on missing data). Stub the
    # yfinance module to raise on use and confirm None comes back, not an exception.
    import types

    boom = types.ModuleType("yfinance")
    def _ticker(_sym):
        raise RuntimeError("network down")
    boom.Ticker = _ticker  # type: ignore[attr-defined]
    saved = sys.modules.get("yfinance")
    sys.modules["yfinance"] = boom
    try:
        assert EarningsCalendar()._lookup("AAPL", _TODAY) is None
    finally:
        if saved is not None:
            sys.modules["yfinance"] = saved
        else:
            sys.modules.pop("yfinance", None)


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
# C.5: Robinhood-first market-wide calendar (yfinance stays the fallback)
# --------------------------------------------------------------------------- #
class _FakeRHReader:
    def __init__(self, payload, enabled=True):
        self.payload = payload
        self.enabled = enabled
        self.calls = []

    def call_json(self, tool, arguments=None):
        self.calls.append((tool, arguments))
        return self.payload


def test_rh_calendar_answers_without_yfinance():
    reader = _FakeRHReader([
        {"symbol": "LLY", "report_date": "2026-06-30"},   # 2 days out
        {"ticker": "TSM", "date": "2026-07-20"},           # alt field names
        {"symbol": "OLD", "report_date": "2026-06-01"},    # past -> dropped
    ])
    cal = EarningsCalendar(reader=reader)
    cal._yf_lookup = lambda *_: (_ for _ in ()).throw(AssertionError("yf called"))
    assert cal.days_until_earnings("LLY", today=_TODAY) == 2
    assert cal.days_until_earnings("TSM", today=_TODAY) == 22
    # Absent from a SUCCESSFUL window read = no report inside 31d -> None,
    # and still no yfinance call.
    assert cal.days_until_earnings("AAPL", today=_TODAY) is None
    # The market-wide calendar was fetched exactly once for all three lookups.
    assert len(reader.calls) == 1
    assert reader.calls[0] == ("get_earnings_calendar", {"days": 31})


def test_rh_calendar_keeps_nearest_of_duplicate_reports():
    reader = _FakeRHReader([
        {"symbol": "LLY", "report_date": "2026-07-15"},
        {"symbol": "LLY", "report_date": "2026-06-30"},
    ])
    cal = EarningsCalendar(reader=reader)
    assert cal.days_until_earnings("LLY", today=_TODAY) == 2


def test_rh_failure_falls_back_to_yfinance():
    reader = _FakeRHReader(None)                      # MCP call failed
    cal = EarningsCalendar(reader=reader)
    cal._yf_lookup = lambda sym, today: 5
    assert cal.days_until_earnings("LLY", today=_TODAY) == 5


def test_rh_disabled_falls_back_to_yfinance():
    reader = _FakeRHReader([{"symbol": "LLY", "report_date": "2026-06-30"}],
                           enabled=False)
    cal = EarningsCalendar(reader=reader)
    cal._yf_lookup = lambda sym, today: 7
    assert cal.days_until_earnings("LLY", today=_TODAY) == 7
    assert reader.calls == []                         # never called while off


def test_rh_calendar_refetched_after_new_cycle():
    reader = _FakeRHReader([{"symbol": "LLY", "report_date": "2026-06-30"}])
    cal = EarningsCalendar(reader=reader)
    cal.days_until_earnings("LLY", today=_TODAY)
    cal.new_cycle()
    cal.days_until_earnings("LLY", today=_TODAY)
    assert len(reader.calls) == 2                     # once per cycle


def test_rh_calendar_nested_payload_tolerated():
    reader = _FakeRHReader({"earnings": [
        {"symbol": "NVDA", "earnings_date": "2026-07-01"},
    ]})
    cal = EarningsCalendar(reader=reader)
    assert cal.days_until_earnings("NVDA", today=_TODAY) == 3


def test_rh_calendar_nested_report_date():
    # The LIVE payload nests the date: {"symbol": "T", "report": {"date": ...}}.
    # The flat-key-only parser shipped blind — every row failed to parse and the
    # empty result suppressed the yfinance fallback (the Jul-20 T buy 2 days
    # before its report). Both halves are pinned here.
    reader = _FakeRHReader({"results": [
        {"symbol": "T", "eps": {"estimate": "0.59", "actual": None},
         "report": {"date": "2026-06-30", "timing": "am", "verified": True}},
        {"symbol": "WFRD", "report": {"date": None}},   # date-less row -> dropped
    ]})
    cal = EarningsCalendar(reader=reader)
    cal._yf_lookup = lambda *_: (_ for _ in ()).throw(AssertionError("yf called"))
    assert cal.days_until_earnings("T", today=_TODAY) == 2
    assert cal.days_until_earnings("WFRD", today=_TODAY) is None


def test_rh_calendar_empty_parse_falls_back_to_yfinance():
    # Rows that all fail to parse must read as a FAILED calendar, not as
    # "no reports market-wide in 31d" — otherwise the guard goes blind.
    reader = _FakeRHReader([{"symbol": "T", "when": "2026-06-30"}])
    cal = EarningsCalendar(reader=reader)
    cal._yf_lookup = lambda sym, today: 4
    assert cal.days_until_earnings("T", today=_TODAY) == 4
    # Still only one market-wide fetch for the cycle despite the fallback.
    cal._yf_lookup = lambda sym, today: 9
    assert cal.days_until_earnings("F", today=_TODAY) == 9
    assert len(reader.calls) == 1


# --------------------------------------------------------------------------- #
# Run-7 A7: the fallback is no longer silent — source() for the FEEDS token and
# ONE 'Earnings calendar:' line per cycle. Sep 10 2026: RH OAuth died 08:34,
# 12 decision cycles ran the blackout gate on yfinance with no log line while
# FEEDS read '3/3 healthy'. The gate's inputs (days_until_earnings) must be
# byte-for-byte what they were — only visibility changes. Fixture-free so the
# standalone runner above still works.
# --------------------------------------------------------------------------- #
import contextlib
import logging


class _DeadRHReader(_FakeRHReader):
    """RobinhoodReader shape under the dead-auth latch: enabled False and a
    callable auth_dead (a classmethod on the real reader)."""
    def __init__(self):
        super().__init__(payload=None, enabled=False)

    def auth_dead(self):
        return True


@contextlib.contextmanager
def _yf_importable(flag: bool):
    # Save the RAW class-dict entry (the staticmethod object): attribute access
    # would unwrap it to a plain function and restore an instance method.
    saved = EarningsCalendar.__dict__["_yf_importable"]
    EarningsCalendar._yf_importable = staticmethod(lambda: flag)
    try:
        yield
    finally:
        EarningsCalendar._yf_importable = saved


@contextlib.contextmanager
def _capture(logger_name: str):
    records: list[logging.LogRecord] = []

    class _H(logging.Handler):
        def emit(self, record):
            records.append(record)

    lg = logging.getLogger(logger_name)
    h = _H(level=logging.DEBUG)
    saved_level = lg.level
    lg.addHandler(h)
    lg.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        lg.removeHandler(h)
        lg.setLevel(saved_level)


def _cal_lines(records):
    return [r for r in records if r.getMessage().startswith("Earnings calendar:")]


def test_source_rh_when_reader_healthy_before_and_after_fetch():
    reader = _FakeRHReader([{"symbol": "LLY", "report_date": "2026-06-30"}])
    cal = EarningsCalendar(reader=reader)
    # The FEEDS line is logged BEFORE the first lookup of the cycle: a healthy
    # reader reads 'rh' on the enabled flag alone, with no calendar call.
    assert cal.source() == "rh"
    assert reader.calls == []
    cal.days_until_earnings("LLY", today=_TODAY)
    assert cal.source() == "rh"
    assert cal.rh_status() == "ok"


def test_source_yfinance_fallback_when_rh_dead_or_disabled():
    with _yf_importable(True):
        assert EarningsCalendar(reader=_DeadRHReader()).source() == "yfinance-fallback"
        assert EarningsCalendar(reader=_FakeRHReader([], enabled=False)).source() \
            == "yfinance-fallback"
        assert EarningsCalendar(reader=None).source() == "yfinance-fallback"


def test_source_none_when_no_source_can_answer():
    # RH unavailable AND yfinance not importable: every lookup is None and the
    # gate fails open on every name — the one state that must never look healthy.
    with _yf_importable(False):
        assert EarningsCalendar(reader=None).source() == "none"
        assert EarningsCalendar(reader=_DeadRHReader()).source() == "none"
        # ...but a healthy RH reader does not need yfinance at all.
        assert EarningsCalendar(reader=_FakeRHReader([])).source() == "rh"


def test_source_latches_fallback_after_failed_rh_read_until_a_read_succeeds():
    # The FEEDS line runs before this cycle's fetch, so the last read's outcome
    # is the evidence it carries: a failed call flips the token from the cycle
    # after the failure and stays until RH answers again (like news=vader-fallback).
    with _yf_importable(True):
        reader = _FakeRHReader(None)                      # MCP call fails
        cal = EarningsCalendar(reader=reader)
        cal._yf_lookup = lambda sym, today: 5
        assert cal.source() == "rh"                       # nothing tried yet
        assert cal.days_until_earnings("LLY", today=_TODAY) == 5   # value unchanged
        assert cal.rh_status() == "call-failed"
        assert cal.source() == "yfinance-fallback"
        cal.new_cycle()
        assert cal.source() == "yfinance-fallback"        # sticky across the cycle boundary
        reader.payload = [{"symbol": "LLY", "report_date": "2026-06-30"}]
        assert cal.days_until_earnings("LLY", today=_TODAY) == 2
        assert cal.source() == "rh"                       # a successful read clears it


def test_source_recovers_when_the_oauth_latch_self_heals_without_a_lookup():
    # Review finding (run-7 A7): the 'oauth-dead' status only changed inside a
    # LOOKUP, and cycles with no buy candidates never look up — so after the
    # reader's latch self-healed, FEEDS could say earnings=yfinance-fallback
    # for days on a healthy RH. _rh_available() now clears that latch itself.
    class _HealingReader(_FakeRHReader):
        def __init__(self):
            super().__init__(payload=[{"symbol": "LLY", "report_date": "2026-06-30"}],
                             enabled=False)
            self.dead = True

        def auth_dead(self):
            return self.dead

    with _yf_importable(True):
        reader = _HealingReader()
        cal = EarningsCalendar(reader=reader)
        cal._yf_lookup = lambda sym, today: 5
        assert cal.days_until_earnings("LLY", today=_TODAY) == 5     # fell back
        assert cal.rh_status() == "oauth-dead"
        assert cal.source() == "yfinance-fallback"
        cal.new_cycle()
        reader.enabled, reader.dead = True, False                    # latch healed
        assert cal.source() == "rh"                                  # no lookup needed
        assert cal.rh_status() == "pending"
        assert cal.days_until_earnings("LLY", today=_TODAY) == 2 and cal.rh_status() == "ok"
        # ...but a latch that is still dead stays yfinance-fallback, and the
        # call-failed latch is untouched (a read must succeed to clear it).
        reader2 = _HealingReader()
        cal2 = EarningsCalendar(reader=reader2)
        cal2._yf_lookup = lambda sym, today: 5
        cal2.days_until_earnings("LLY", today=_TODAY)
        reader2.enabled = True                                       # enabled, still dead
        assert cal2.source() == "yfinance-fallback" and cal2.rh_status() == "oauth-dead"
        reader3 = _FakeRHReader(None)
        cal3 = EarningsCalendar(reader=reader3)
        cal3._yf_lookup = lambda sym, today: 5
        cal3.days_until_earnings("LLY", today=_TODAY)
        assert cal3.source() == "yfinance-fallback" and cal3.rh_status() == "call-failed"


def test_source_parsed_empty_counts_as_fallback():
    with _yf_importable(True):
        reader = _FakeRHReader([{"symbol": "T", "when": "2026-06-30"}])
        cal = EarningsCalendar(reader=reader)
        cal._yf_lookup = lambda sym, today: 4
        cal.days_until_earnings("T", today=_TODAY)
        assert cal.rh_status() == "parsed-empty"
        assert cal.source() == "yfinance-fallback"


def test_fallback_logged_once_per_cycle_at_warning_when_rh_dead():
    with _yf_importable(True), _capture("earnings") as recs:
        cal = EarningsCalendar(reader=_DeadRHReader())
        cal._yf_lookup = lambda sym, today: 3
        for sym in ("AAPL", "MSFT", "NVDA"):
            assert cal.days_until_earnings(sym, today=_TODAY) == 3
        cal.new_cycle()
        cal.days_until_earnings("AAPL", today=_TODAY)
    hits = _cal_lines(recs)
    assert len(hits) == 2                              # once per cycle, not per symbol
    assert all(r.levelno == logging.WARNING for r in hits)
    assert "OAuth dead" in hits[0].getMessage()
    assert "earnings=yfinance-fallback" in hits[0].getMessage()


def test_fallback_logged_at_info_when_rh_simply_not_wired():
    # No RH reader at all: yfinance is the by-design primary, not an outage.
    with _yf_importable(True), _capture("earnings") as recs:
        cal = EarningsCalendar(reader=None)
        cal._yf_lookup = lambda sym, today: None
        cal.days_until_earnings("AAPL", today=_TODAY)
    hit = _cal_lines(recs)[0]
    assert hit.levelno == logging.INFO
    assert "not wired" in hit.getMessage()


def test_fallback_logged_at_warning_when_gate_is_blind():
    with _yf_importable(False), _capture("earnings") as recs:
        cal = EarningsCalendar(reader=None)
        cal._yf_lookup = lambda sym, today: None
        cal.days_until_earnings("AAPL", today=_TODAY)
    hit = _cal_lines(recs)[0]
    assert hit.levelno == logging.WARNING
    assert "earnings=none" in hit.getMessage()


def test_blackout_inputs_unchanged_under_fallback():
    # The risk gate sees exactly what it saw before A7: the yfinance answer
    # under every fallback shape, cached per cycle, and source() is read-only
    # (no calendar call, no cache write).
    for reader in (None, _DeadRHReader(), _FakeRHReader(None)):
        cal = EarningsCalendar(reader=reader)
        seen = []
        cal._yf_lookup = lambda sym, today: seen.append(sym) or 2
        assert cal.source() in ("rh", "yfinance-fallback", "none")
        assert seen == []
        assert cal.days_until_earnings("LLY", today=_TODAY) == 2
        assert cal.days_until_earnings("LLY", today=_TODAY) == 2
        assert seen == ["LLY"]                         # per-cycle cache intact
