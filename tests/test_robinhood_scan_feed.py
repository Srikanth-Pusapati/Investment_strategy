"""Tests for the C.6 Robinhood saved-scan discovery screener.

Pure logic, no network: the RobinhoodReader is replaced with a fake that returns
scripted get_scans / run_scan payloads. We assert candidate extraction, the
multi-scan corroboration merge, the caps, defensive parsing, and the no-scans
no-op.

Runnable two ways:
    .venv/bin/python tests/test_robinhood_scan_feed.py
    .venv/bin/pytest tests/
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.screener.robinhood_scan_feed import RobinhoodScanScreener


class _FakeReader:
    def __init__(self, scans, results):
        self.enabled = True
        self.scans = scans            # get_scans payload
        self.results = results        # scan_id -> run_scan payload
        self.calls = []

    def call_json(self, tool, arguments=None):
        self.calls.append((tool, arguments))
        if tool == "get_scans":
            return self.scans
        if tool == "run_scan":
            return self.results.get((arguments or {}).get("scan_id"))
        raise AssertionError(f"unexpected tool {tool}")


def _screener(scans, results):
    s = RobinhoodScanScreener.__new__(RobinhoodScanScreener)
    s.cfg = SimpleNamespace()
    s._reader = _FakeReader(scans, results)
    return s


def test_scan_results_become_candidates():
    s = _screener(
        scans=[{"id": "s1", "title": "RSI breakout"}],
        results={"s1": {"instruments": [
            {"ticker": "NVDA"}, {"ticker": "AMD"},
        ]}},
    )
    cands = s.scan()
    assert sorted(c.symbol for c in cands) == ["AMD", "NVDA"]
    c = next(c for c in cands if c.symbol == "NVDA")
    assert c.sources == ["robinhood_scans"]
    assert "RSI breakout" in c.reason
    assert c.score == 0.5


def test_name_hit_by_two_scans_merges_with_both_titles():
    s = _screener(
        scans=[{"id": "s1", "title": "RSI breakout"},
               {"id": "s2", "title": "Volume spike"}],
        results={
            "s1": {"instruments": [{"ticker": "NVDA"}]},
            "s2": {"instruments": [{"ticker": "NVDA"}]},
        },
    )
    cands = s.scan()
    assert len(cands) == 1
    assert "RSI breakout" in cands[0].reason and "Volume spike" in cands[0].reason


def test_no_saved_scans_is_a_quiet_noop():
    s = _screener(scans=[], results={})
    assert s.scan() == []
    s2 = _screener(scans=None, results={})       # MCP call failed
    assert s2.scan() == []


def test_scan_and_row_caps_respected():
    scans = [{"id": f"s{i}", "title": f"scan {i}"} for i in range(10)]
    results = {
        f"s{i}": {"instruments": [{"ticker": f"T{i}A"}]} for i in range(10)
    }
    s = _screener(scans, results)
    s.scan()
    run_calls = [c for c in s._reader.calls if c[0] == "run_scan"]
    assert len(run_calls) == 5                   # _MAX_SCANS


def test_garbage_rows_and_invalid_tickers_skipped():
    s = _screener(
        scans=[{"id": "s1", "title": "mixed"}],
        results={"s1": {"instruments": [
            {"ticker": "NVDA"}, {"no_ticker": True}, "amd",
            {"ticker": "not a ticker !!"}, 42,
        ]}},
    )
    assert sorted(c.symbol for c in s.scan()) == ["AMD", "NVDA"]


def _run_all():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS {fn.__name__}")
        except Exception as e:
            failed += 1
            print(f"  FAIL {fn.__name__}: {e!r}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
