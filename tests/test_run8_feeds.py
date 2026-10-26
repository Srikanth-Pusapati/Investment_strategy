"""Run-8 feed-resilience items — C-2 (EDGAR retry + cached insider snapshot)
and C-3 (yfinance's own ERROR noise demoted to DEBUG).

C-2 WHY: on 2026-09-25 three sec.gov timeouts made the FEEDS line read
`insider UNHEALTHY (edgar: timeout)` in 3/8 cycles — a pre-registered validity
breach for a transient network blip (Oct 13 and Oct 16 had more). The pull now
retries once after a short pause, every good scan leaves a snapshot behind, and
with INSIDER_CACHE_MAX_AGE_H > 0 a failed pull serves that snapshot (reported
as `cached_age_h`, never as `degraded`) when it is young enough. Knob 0 keeps
run-7 behaviour exactly. EDGAR is mocked; nothing touches the network.

C-3 WHY: yfinance logs "$HYG: possibly delisted; no price data found" and "No
fundamentals data found for symbol: HYG" at ERROR from its OWN logger — 31 of
them on Oct 9 2026 during a 3-minute blip — which inflates the day's ERROR
count the morning check grades. They are demoted to DEBUG, not dropped.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.__main__ import _YFinanceNoiseFilter, _setup_logging  # noqa: E402
from investment_strategy.models import Candidate  # noqa: E402
from investment_strategy.screener import insider_feed  # noqa: E402
from investment_strategy.screener.insider_feed import InsiderFeedScreener  # noqa: E402

ROW_A = {"symbol": "AAA", "sources": ["insider"], "score": 0.667,
         "reason": "Insider: 2 open-market buy filing(s), 300 sh (SEC Form 4, last filings feed)."}
ROW_B = {"symbol": "BBB", "sources": ["insider"], "score": -0.4,
         "reason": "Insider: 2 open-market SELL filing(s), 10,000 sh — bearish cluster (SEC Form 4, last filings feed)."}
FEED_HTML = '<a href="https://www.sec.gov/Archives/edgar/data/123/000112233/0001-12-33-index.htm">x</a>'


def _screener(tmp_path, max_age_h: float = 0.0) -> InsiderFeedScreener:
    s = InsiderFeedScreener.__new__(InsiderFeedScreener)  # skip network __init__
    s.cfg = SimpleNamespace(
        sec_user_agent="test@example.com",
        screener=SimpleNamespace(insider_scan_limit=100),
        state_file=str(tmp_path / "state" / "risk_state.json"),
        insider_cache_max_age_h=max_age_h,
    )
    s._headers = {}
    s._RETRY_PAUSE_S = 0.0           # no real sleeping in tests
    return s


def _write_snapshot(tmp_path, age_h: float, rows: list[dict]) -> None:
    (tmp_path / "state").mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc) - timedelta(hours=age_h)
    (tmp_path / "state" / "insider_snapshot.json").write_text(
        json.dumps({"ts": ts.isoformat(), "candidates": rows}))


class _EdgarDown:
    """A requests.get that times out every time and counts the attempts."""

    def __init__(self, exc=None):
        self.calls = 0
        self.exc = exc or requests.exceptions.Timeout("sec.gov timed out")

    def __call__(self, *a, **k):
        self.calls += 1
        raise self.exc


def _edgar_down(monkeypatch, exc=None) -> _EdgarDown:
    fake = _EdgarDown(exc)
    monkeypatch.setattr(insider_feed.requests, "get", fake)
    return fake


# -- C-2: the retry ------------------------------------------------------------ #

def test_edgar_get_is_retried_once_before_the_pull_is_degraded(tmp_path, monkeypatch):
    s = _screener(tmp_path)
    fake = _edgar_down(monkeypatch)
    s.degraded = None
    assert s._recent_form4_filings() == []
    assert fake.calls == 2                        # 2 attempts total, then degraded
    assert s.degraded == "edgar: timeout"


def test_http_error_on_both_attempts_is_degraded_with_the_status(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(insider_feed.requests, "get",
                        lambda *a, **k: (calls.append(1), SimpleNamespace(status_code=503, text=""))[1])
    s = _screener(tmp_path)
    s.degraded = None
    assert s._recent_form4_filings() == []
    assert len(calls) == 2 and s.degraded == "edgar: http 503"


def test_a_good_second_attempt_is_a_healthy_pull(tmp_path, monkeypatch):
    calls: list[str] = []

    def fake(url, **kw):
        calls.append(url)
        if len(calls) == 1:
            raise requests.exceptions.ConnectionError("connection reset")
        return SimpleNamespace(status_code=200, text=FEED_HTML)

    monkeypatch.setattr(insider_feed.requests, "get", fake)
    s = _screener(tmp_path)
    s.degraded = None
    assert s._recent_form4_filings() == [("123", "000112233", "0001-12-33")]
    assert len(calls) == 2
    assert s.degraded is None


def test_a_good_first_attempt_makes_exactly_one_request(tmp_path, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(insider_feed.requests, "get",
                        lambda url, **kw: (calls.append(url), SimpleNamespace(status_code=200, text=FEED_HTML))[1])
    s = _screener(tmp_path)
    s.degraded = None
    assert s._recent_form4_filings() == [("123", "000112233", "0001-12-33")]
    assert len(calls) == 1


def test_retry_waits_the_configured_pause_between_attempts(tmp_path, monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(insider_feed.time, "sleep", lambda s: sleeps.append(s))
    s = _screener(tmp_path)
    s._RETRY_PAUSE_S = 1.0
    _edgar_down(monkeypatch)
    s.degraded = None
    s._recent_form4_filings()
    assert sleeps == [1.0]                        # one pause, between the two attempts
    assert InsiderFeedScreener._RETRY_PAUSE_S == 1.0


# -- C-2: the cached snapshot --------------------------------------------------- #

def test_knob_off_edgar_outage_is_degraded_as_before(tmp_path, monkeypatch):
    _write_snapshot(tmp_path, 4, [ROW_A])
    s = _screener(tmp_path, max_age_h=0.0)
    _edgar_down(monkeypatch)
    assert s.scan() == []
    assert s.degraded == "edgar: timeout"
    assert s.cached_age_h is None


def test_knob_on_and_a_fresh_snapshot_is_served_instead_of_degraded(tmp_path, monkeypatch, caplog):
    _write_snapshot(tmp_path, 4, [ROW_A, ROW_B])
    s = _screener(tmp_path, max_age_h=24)
    _edgar_down(monkeypatch)
    with caplog.at_level(logging.INFO, logger="screener"):
        out = s.scan()
    assert out == [Candidate(**ROW_A), Candidate(**ROW_B)]
    assert s.degraded is None
    assert s.cached_age_h is not None and abs(s.cached_age_h - 4.0) < 0.05
    assert ("INSIDER CACHED: serving the 4.0h-old Form-4 snapshot (2 candidates) — EDGAR timeout"
            in caplog.text)


def test_a_snapshot_older_than_the_cap_is_degraded(tmp_path, monkeypatch):
    _write_snapshot(tmp_path, 30, [ROW_A])
    s = _screener(tmp_path, max_age_h=24)
    _edgar_down(monkeypatch)
    assert s.scan() == []
    assert s.degraded == "edgar: timeout"
    assert s.cached_age_h is None


def test_no_snapshot_on_disk_is_degraded(tmp_path, monkeypatch):
    s = _screener(tmp_path, max_age_h=24)
    _edgar_down(monkeypatch)
    assert s.scan() == []
    assert s.degraded == "edgar: timeout"
    assert s.cached_age_h is None


def test_a_corrupt_snapshot_is_degraded_not_a_crash(tmp_path, monkeypatch):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "insider_snapshot.json").write_text("{not json")
    s = _screener(tmp_path, max_age_h=24)
    _edgar_down(monkeypatch)
    assert s.scan() == []
    assert s.degraded == "edgar: timeout"


def test_a_good_scan_writes_a_snapshot_that_round_trips(tmp_path, monkeypatch):
    s = _screener(tmp_path, max_age_h=24)
    s._recent_form4_filings = lambda: [("c", "a", "0"), ("c", "a", "1"), ("c", "a", "2"), ("c", "a", "3")]
    parsed = {"0": ("AAA", 100, 0), "1": ("AAA", 200, 0),      # 2 buyers -> +0.667
              "2": ("BBB", 0, 5000), "3": ("BBB", 0, 5000)}    # 2 sellers -> -0.4
    s._parse_submission = lambda cik, accno, acc: parsed[acc]
    out = s.scan()
    assert {c.symbol for c in out} == {"AAA", "BBB"}
    assert s.degraded is None and s.cached_age_h is None       # a normal cycle exposes neither
    path = tmp_path / "state" / "insider_snapshot.json"
    snap = json.loads(path.read_text())
    ts = datetime.fromisoformat(snap["ts"])
    assert ts.tzinfo is not None and datetime.now(timezone.utc) - ts < timedelta(seconds=30)
    assert [Candidate.model_validate(r) for r in snap["candidates"]] == out
    assert not path.with_suffix(".json.tmp").exists()          # atomic: tmp + os.replace
    # ...and the next degraded cycle serves exactly those rows back.
    s2 = _screener(tmp_path, max_age_h=24)
    _edgar_down(monkeypatch)
    assert s2.scan() == out
    assert s2.degraded is None and s2.cached_age_h is not None and s2.cached_age_h < 0.01


def test_a_degraded_pull_does_not_overwrite_the_snapshot(tmp_path, monkeypatch):
    _write_snapshot(tmp_path, 4, [ROW_A])
    before = (tmp_path / "state" / "insider_snapshot.json").read_text()
    s = _screener(tmp_path, max_age_h=0.0)
    _edgar_down(monkeypatch)
    s.scan()
    assert (tmp_path / "state" / "insider_snapshot.json").read_text() == before


def test_cached_age_resets_on_the_next_healthy_cycle(tmp_path, monkeypatch):
    _write_snapshot(tmp_path, 4, [ROW_A])
    s = _screener(tmp_path, max_age_h=24)
    _edgar_down(monkeypatch)
    s.scan()
    assert s.cached_age_h is not None
    s._recent_form4_filings = lambda: [("c", "a", "0")]
    s._parse_submission = lambda cik, accno, acc: ("AAA", 100, 0)
    s.scan()
    assert s.cached_age_h is None and s.degraded is None


def test_screener_without_a_state_file_never_writes(tmp_path, monkeypatch):
    """The legacy SimpleNamespace fixtures carry no state_file / knob: no snapshot
    I/O may happen (and nothing may land in the cwd)."""
    monkeypatch.chdir(tmp_path)
    s = InsiderFeedScreener.__new__(InsiderFeedScreener)
    s.cfg = SimpleNamespace(sec_user_agent="t", screener=SimpleNamespace(insider_scan_limit=10))
    s._headers = {}
    s._recent_form4_filings = lambda: [("c", "a", "0")]
    s._parse_submission = lambda cik, accno, acc: ("AAA", 100, 0)
    assert [c.symbol for c in s.scan()] == ["AAA"]
    assert list(tmp_path.iterdir()) == []


# -- C-3: yfinance noise at DEBUG --------------------------------------------- #

def _rec(level: int, msg: str) -> logging.LogRecord:
    return logging.LogRecord("yfinance", level, __file__, 1, msg, None, None)


@pytest.mark.parametrize("msg", [
    "$HYG: possibly delisted; no price data found  (period=1y)",
    "No fundamentals data found for symbol: HYG",
    "404 Client Error: Not Found for url: https://query2.finance.yahoo.com/v10/finance/quoteSummary/HYG",
])
def test_yfinance_noise_is_demoted_to_debug_not_dropped(msg):
    rec = _rec(logging.ERROR, msg)
    assert _YFinanceNoiseFilter().filter(rec) is True        # kept, not dropped
    assert rec.levelno == logging.DEBUG and rec.levelname == "DEBUG"


def test_unrelated_yfinance_error_stays_error():
    rec = _rec(logging.ERROR, "HTTP Error 502: Bad Gateway")
    assert _YFinanceNoiseFilter().filter(rec) is True
    assert rec.levelno == logging.ERROR and rec.levelname == "ERROR"


def test_demotion_through_the_logger_lands_at_debug(caplog):
    lg = logging.getLogger("yfinance")
    flt = _YFinanceNoiseFilter()
    lg.addFilter(flt)
    try:
        with caplog.at_level(logging.DEBUG, logger="yfinance"):
            lg.error("$HYG: possibly delisted; no price data found (period=1y)")
            lg.error("HTTP Error 502: Bad Gateway")
    finally:
        lg.removeFilter(flt)
    levels = {r.getMessage(): r.levelno for r in caplog.records if r.name == "yfinance"}
    assert levels["$HYG: possibly delisted; no price data found (period=1y)"] == logging.DEBUG
    assert levels["HTTP Error 502: Bad Gateway"] == logging.ERROR


def test_setup_logging_installs_the_yfinance_demotion_filter(monkeypatch):
    monkeypatch.setenv("LOG_DIR", "")            # no file sink in tests
    root = logging.getLogger()
    yf = logging.getLogger("yfinance")
    saved, saved_filters = root.handlers[:], yf.filters[:]
    try:
        _setup_logging()
        assert any(isinstance(f, _YFinanceNoiseFilter) for f in yf.filters)
    finally:
        root.handlers[:] = saved
        yf.filters[:] = saved_filters
