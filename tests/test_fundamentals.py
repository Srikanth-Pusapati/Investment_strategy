"""Tests for the fundamentals signal (signals/fundamentals.py) — FMP backend.

Pure logic, no network: the provider's HTTP getter is swapped for a fake that
serves canned FMP /stable rows. We assert backend selection (FMP when keyed,
yfinance fallback otherwise / on miss), the debt-to-equity ratio->percent
conversion, and the 24h response cache that keeps hourly cycles from burning
the FMP request quota.

Runnable two ways:
    .venv/bin/python tests/test_fundamentals.py
    .venv/bin/pytest tests/
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.signals.fundamentals import FundamentalsProvider


def _cfg(fmp="fk"):
    return SimpleNamespace(fmp_api_key=fmp)


class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body if body is not None else []

    def json(self):
        return self._body


_RATIOS = [{"ebitdaMarginTTM": 0.34, "debtToEquityRatioTTM": 1.8}]
_GROWTH = [{"revenueGrowth": 0.05}]


def _provider(feeds=None, fmp="fk"):
    """Provider whose getter serves canned rows per endpoint and counts calls."""
    feeds = feeds if feeds is not None else {"ratios-ttm": _RATIOS,
                                             "financial-growth": _GROWTH}
    p = FundamentalsProvider(_cfg(fmp))
    calls = []

    def fake_get(url, params=None, timeout=None):
        endpoint = url.rsplit("/", 1)[-1]
        calls.append((endpoint, (params or {}).get("symbol")))
        body = feeds.get(endpoint)
        return _Resp(body=body) if body is not None else _Resp(status=404)

    p._get = fake_get
    return p, calls


def test_fmp_backend_used_when_keyed():
    p, calls = _provider()
    sig = p.fetch(["AAPL"])[0]
    assert sig.source == "fmp"
    assert sig.data["ebitda_margin"] == 0.34
    assert sig.data["revenue_growth"] == 0.05
    assert {e for e, _ in calls} == {"ratios-ttm", "financial-growth"}


def test_fmp_debt_to_equity_scaled_to_percent():
    # FMP reports a plain ratio (1.8); yfinance-style scoring expects percent.
    p, _ = _provider()
    sig = p.fetch(["AAPL"])[0]
    assert sig.data["debt_to_equity"] == 180.0
    assert sig.score == FundamentalsProvider._score(0.34, 0.05, 180.0)


def test_fmp_responses_cached_across_fetches():
    p, calls = _provider()
    p.fetch(["AAPL"])
    n = len(calls)
    p.fetch(["AAPL"])                        # same day -> served from cache
    assert len(calls) == n


def test_fmp_miss_falls_back_to_yfinance():
    p, _ = _provider(feeds={})               # FMP knows nothing
    p._yf_fields = lambda s: {"ebitda": 2e9, "ebitda_margin": 0.2,
                              "debt_to_equity": 80.0, "revenue_growth": 0.1}
    sig = p.fetch(["ZZZ"])[0]
    assert sig.source == "yfinance"
    assert "EBITDA $2.0B" in sig.summary


def test_no_key_goes_straight_to_yfinance():
    p = FundamentalsProvider(_cfg(fmp=""))
    seen = []
    p._get = lambda *a, **k: (_ for _ in ()).throw(AssertionError("FMP called"))
    p._yf_fields = lambda s: (seen.append(s), None)[1]
    assert p.fetch(["AAPL"]) == []           # yf returned None -> no signal
    assert seen == ["AAPL"]


def test_fmp_miss_cached_too():
    p, calls = _provider(feeds={})
    p._yf_fields = lambda s: None
    p.fetch(["ZZZ"])
    n = len(calls)
    p.fetch(["ZZZ"])                         # miss is cached; no re-probe
    assert len(calls) == n


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
