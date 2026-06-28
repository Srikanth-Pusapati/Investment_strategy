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
