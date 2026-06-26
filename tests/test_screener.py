"""Tests for the discovery layer — the scanner that surfaces NEW candidates.

Pure logic, no network: fake Screener subclasses return canned Candidates, and we
assert the aggregator dedupes, excludes, filters, ranks, and caps correctly, and
degrades gracefully when one source raises.

Runnable two ways:
    .venv/bin/python tests/test_screener.py     # standalone, no pytest needed
    .venv/bin/pytest tests/                      # if pytest is installed
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.config import ScreenerConfig
from investment_strategy.models import Candidate, SignalKind
from investment_strategy.screener.aggregator import ScreenerAggregator
from investment_strategy.screener.base import Screener


def _cfg(options_enabled=True, **over) -> SimpleNamespace:
    base = dict(
        enabled=True,
        sources=(),                 # no real screeners built; we inject fakes
        max_candidates=12,
        min_score=0.2,
        options_flow_scan_limit=40,
    )
    base.update(over)
    # Only options_enabled is read off cfg.risk by the aggregator (it gates
    # whether bearish, non-held discoveries are actionable).
    return SimpleNamespace(
        screener=ScreenerConfig(**base),
        risk=SimpleNamespace(options_enabled=options_enabled),
    )


class _Fake(Screener):
    def __init__(self, name, candidates):
        self.name = name
        self._candidates = candidates

    def scan(self):
        return self._candidates


class _Boom(Screener):
    name = "boom"

    def scan(self):
        raise RuntimeError("provider exploded")


def _agg(cfg, screeners) -> ScreenerAggregator:
    agg = ScreenerAggregator.__new__(ScreenerAggregator)  # skip registry build
    agg.cfg = cfg
    agg.screeners = screeners
    return agg


def _cand(symbol, source, score, reason="x"):
    return Candidate(symbol=symbol, sources=[source], reason=reason, score=score)


# --------------------------------------------------------------------------- #
def test_candidate_to_signal_is_discovery():
    sig = _cand("AAPL", "insider", 0.8, "3 buys").to_signal()
    assert sig.kind is SignalKind.DISCOVERY
    assert sig.symbol == "AAPL"
    assert sig.score == 0.8
    assert sig.summary == "3 buys"


def test_dedup_merges_sources_and_sums_scores():
    agg = _agg(_cfg(), [
        _Fake("congress", [_cand("NVDA", "congress", 0.3)]),
        _Fake("insider", [_cand("NVDA", "insider", 0.5)]),
    ])
    out = agg.scan()
    assert len(out) == 1
    nvda = out[0]
    assert nvda.symbol == "NVDA"
    assert set(nvda.sources) == {"congress", "insider"}
    assert abs(nvda.score - 0.8) < 1e-9      # corroboration accumulates


def test_exclude_drops_watchlist_and_held():
    agg = _agg(_cfg(), [
        _Fake("a", [_cand("AAPL", "a", 0.9), _cand("TSLA", "a", 0.9)]),
    ])
    out = agg.scan(exclude={"aapl"})         # case-insensitive
    assert [c.symbol for c in out] == ["TSLA"]


def test_min_score_filters_weak_names():
    agg = _agg(_cfg(min_score=0.5), [
        _Fake("a", [_cand("AAA", "a", 0.4), _cand("BBB", "a", 0.6)]),
    ])
    out = agg.scan()
    assert [c.symbol for c in out] == ["BBB"]


def test_min_score_uses_absolute_value():
    # A strongly bearish name (negative score) is still a high-conviction signal.
    agg = _agg(_cfg(min_score=0.5), [
        _Fake("a", [_cand("BEAR", "a", -0.8)]),
    ])
    assert [c.symbol for c in agg.scan()] == ["BEAR"]


def test_ranked_by_abs_score_and_capped():
    agg = _agg(_cfg(max_candidates=2), [
        _Fake("a", [
            _cand("LOW", "a", 0.25),
            _cand("HIGH", "a", 0.9),
            _cand("MID", "a", -0.6),
        ]),
    ])
    out = agg.scan()
    assert [c.symbol for c in out] == ["HIGH", "MID"]   # |0.9| > |-0.6| > 0.25, cap 2


def test_bearish_dropped_when_options_disabled():
    # A bearish lean on a name we don't hold is unactionable with options off.
    fakes = [_Fake("a", [_cand("BULL", "a", 0.6), _cand("BEAR", "a", -0.9)])]
    assert [c.symbol for c in _agg(_cfg(options_enabled=False), fakes).scan()] == ["BULL"]
    # With options on, the bearish name is a valid long-put candidate and ranks first.
    assert [c.symbol for c in _agg(_cfg(options_enabled=True), fakes).scan()] == ["BEAR", "BULL"]


def test_graceful_degradation_one_source_raises():
    agg = _agg(_cfg(), [
        _Boom(),
        _Fake("good", [_cand("MSFT", "good", 0.7)]),
    ])
    out = agg.scan()                          # must not raise
    assert [c.symbol for c in out] == ["MSFT"]


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
