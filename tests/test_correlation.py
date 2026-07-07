"""Tests for the pairwise-correlation guard (R.2).

CorrelationGuard measures whether a NEW buy is effectively a duplicate of an
already-held name (daily-return correlation over ~90d). Deterministic fakes —
no network; the broker fake serves synthetic dated close series.

Runnable two ways:
    .venv/bin/python tests/test_correlation.py
    .venv/bin/pytest tests/
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.correlation import MIN_OVERLAP, CorrelationGuard
from investment_strategy.models import AccountSnapshot, Position
from investment_strategy.orchestrator import Orchestrator

_N = 100  # bars per synthetic series (> MIN_OVERLAP + 1)


def _dates(n=_N):
    return [f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}" for i in range(n)]


def _series(closes):
    return list(zip(_dates(len(closes)), closes))


def _wiggle(i):  # deterministic day-to-day texture (varying daily returns)
    return 1.0 + ((i * 7) % 5 - 2) / 100.0


class _FakeBroker:
    """daily_close_series fake with a call counter (for cache assertions)."""

    def __init__(self, series):
        self.series = series
        self.calls = []

    def daily_close_series(self, symbol, days):
        self.calls.append(symbol)
        return self.series.get(symbol, [])


def _broker():
    base = [100.0]
    for i in range(1, _N):
        base.append(base[-1] * _wiggle(i))
    clone = [c * 2.0 for c in base]                      # same returns -> corr 1
    inverse = [10_000.0 / c for c in base]               # opposite -> corr -1
    flat = [50.0] * _N                                   # zero variance
    short = base[: MIN_OVERLAP // 2]                     # not enough history
    return _FakeBroker({
        "BASE": _series(base), "CLONE": _series(clone),
        "INV": _series(inverse), "FLAT": _series(flat),
        "SHORT": _series(short),
    })


def test_clone_detected_as_max_correlation():
    g = CorrelationGuard(_broker())
    best = g.max_correlation("BASE", ["INV", "CLONE"])
    assert best is not None
    corr, sym = best
    assert sym == "CLONE"
    assert corr > 0.99


def test_inverse_name_is_not_flagged_high():
    g = CorrelationGuard(_broker())
    corr, sym = g.max_correlation("BASE", ["INV"])
    assert sym == "INV"
    assert corr < -0.99          # diversifier, nowhere near any positive cap


def test_fails_open_on_short_history_and_flat_series():
    g = CorrelationGuard(_broker())
    assert g.max_correlation("SHORT", ["BASE"]) is None   # candidate too short
    assert g.max_correlation("BASE", ["SHORT", "FLAT"]) is None  # nothing comparable


def test_never_self_compares():
    g = CorrelationGuard(_broker())
    assert g.max_correlation("BASE", ["BASE"]) is None    # top-up isn't a new bet


def test_series_cached_within_cycle_and_dropped_on_new_cycle():
    b = _broker()
    g = CorrelationGuard(b)
    g.max_correlation("BASE", ["CLONE"])
    g.max_correlation("BASE", ["CLONE", "INV"])
    assert b.calls.count("BASE") == 1                     # cached across calls
    assert b.calls.count("CLONE") == 1
    g.new_cycle()
    g.max_correlation("BASE", ["CLONE"])
    assert b.calls.count("BASE") == 2                     # refetched next cycle


# --------------------------------------------------------------------------- #
# Orchestrator._corr_context — the exclusions that make the guard correct
# --------------------------------------------------------------------------- #
class _FakeGuard:
    def __init__(self, result=(0.9, "NVDA")):
        self.result = result
        self.asked = None

    def max_correlation(self, symbol, held):
        self.asked = (symbol, list(held))
        return self.result


def _pos(symbol):
    return Position(symbol=symbol, qty=1.0, avg_entry_price=100.0,
                    current_price=100.0, market_value=100.0,
                    unrealized_pl=0.0, unrealized_pl_pct=0.0)


def _acct(*symbols):
    return AccountSnapshot(equity=1_000.0, last_equity=1_000.0, cash=0.0,
                           buying_power=0.0, positions=[_pos(s) for s in symbols])


def _orch(core_etf="QQQ", guard=None):
    o = Orchestrator.__new__(Orchestrator)
    o.cfg = SimpleNamespace(core_etf=core_etf)
    o.corr_guard = guard or _FakeGuard()
    return o


def test_corr_context_excludes_core_etf_and_self():
    g = _FakeGuard()
    o = _orch(core_etf="QQQ", guard=g)
    corr, sym, data_missing = o._corr_context("SMCI", _acct("QQQ", "NVDA", "SMCI"))
    assert g.asked == ("SMCI", ["NVDA"])   # no core ETF, no self-compare
    assert (corr, sym) == (0.9, "NVDA")
    assert data_missing is False  # guard had an answer


def test_corr_context_none_when_only_core_held():
    o = _orch(core_etf="QQQ", guard=_FakeGuard())
    corr, sym, data_missing = o._corr_context("SMCI", _acct("QQQ"))
    assert (corr, sym) == (None, "")
    assert data_missing is False  # nothing held to compare → not a data gap


def test_corr_context_fails_open_when_guard_has_no_answer():
    o = _orch(guard=_FakeGuard(result=None))
    corr, sym, data_missing = o._corr_context("SMCI", _acct("QQQ", "NVDA"))
    assert (corr, sym) == (None, "")
    assert data_missing is True  # held non-core satellites but guard had no answer


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
