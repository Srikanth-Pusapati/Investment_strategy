"""Tests for the deterministic weighted composite index (signals/composite.py).

Pure logic, no network: synthetic bundles + a duck-typed ledger. Asserts the
per-kind mean (one chatty provider can't dominate), the freshness-lag weighting,
the realized-performance tilt with its min-trips floor and 0.5..1.5 clamp, and
the None-on-unscored contract the fail-open consumers rely on.

Runnable two ways:
    .venv/bin/python -m pytest tests/test_composite.py
    .venv/bin/pytest tests/
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.ledger import TradeRecord
from investment_strategy.models import Signal, SignalBundle, SignalKind
from investment_strategy.signals.composite import composite_score, perf_weights

_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _sig(kind, score, symbol="AAPL"):
    return Signal(kind=kind, symbol=symbol, summary="x", score=score)


def _bundle(*sigs, symbol="AAPL"):
    return SignalBundle(symbol=symbol, signals=list(sigs))


def _ledger_with_trips(source: str, pl_pct: float, n: int):
    """n closed round-trips attributed to `source`, each realizing pl_pct."""
    recs = []
    for i in range(n):
        sym = f"S{i}"
        recs.append(TradeRecord(symbol=sym, action="buy", qty=1.0,
                                entry_signals=[source],
                                ts=_T0 + timedelta(hours=2 * i)))
        recs.append(TradeRecord(symbol=sym, action="sell", qty=1.0,
                                realized_pl_pct=pl_pct, exit_reason="decision",
                                ts=_T0 + timedelta(hours=2 * i + 1)))
    return SimpleNamespace(effective=lambda: recs)


# -- composite_score ---------------------------------------------------------- #
def test_none_when_nothing_scored():
    assert composite_score(_bundle()) is None
    assert composite_score(_bundle(_sig(SignalKind.NEWS, None))) is None


def test_single_technical_signal_passes_through():
    # technical lag weight is 0.5^0 = 1.0 and no perf weight -> the raw score.
    assert composite_score(_bundle(_sig(SignalKind.TECHNICAL, 0.5))) == 0.5


def test_per_kind_mean_stops_chatty_provider_domination():
    # Three news rows average first (0.6), they don't sum to 1.8.
    b = _bundle(
        _sig(SignalKind.NEWS, 0.4), _sig(SignalKind.NEWS, 0.8),
        _sig(SignalKind.NEWS, 0.6),
    )
    from investment_strategy.signals.history import lag_weight
    expected = round(0.6 * lag_weight(SignalKind.NEWS), 2)
    assert composite_score(b) == expected


def test_lag_weight_discounts_stale_sources_and_spares_thesis_kinds():
    from investment_strategy.signals.history import lag_weight
    congress = composite_score(_bundle(_sig(SignalKind.CONGRESS, 0.8)))
    fundamentals = composite_score(_bundle(_sig(SignalKind.FUNDAMENTALS, 0.8)))
    assert congress == round(0.8 * lag_weight(SignalKind.CONGRESS), 2)  # ~0.18
    assert fundamentals == 0.8   # thesis kind: lag_weight None -> 1.0
    assert congress < fundamentals


def test_kinds_sum_and_perf_weight_applies():
    b = _bundle(_sig(SignalKind.TECHNICAL, 0.5), _sig(SignalKind.FUNDAMENTALS, 0.5))
    assert composite_score(b) == 1.0
    # A 0.5 perf weight on technical halves only that term.
    assert composite_score(b, {"technical": 0.5}) == 0.75


def test_bearish_signals_go_negative():
    assert composite_score(_bundle(_sig(SignalKind.TECHNICAL, -0.7))) == -0.7


# -- perf_weights ------------------------------------------------------------- #
def test_perf_weights_clamped_and_signed():
    up = perf_weights(_ledger_with_trips("technical", 20.0, 3))
    down = perf_weights(_ledger_with_trips("technical", -20.0, 3))
    assert up["technical"] == 1.5     # clamp above
    assert down["technical"] == 0.5   # clamp below
    mid = perf_weights(_ledger_with_trips("news", 2.0, 3))
    assert abs(mid["news"] - 1.2) < 1e-9


def test_perf_weights_min_trips_floor():
    # Two lucky trades must not tilt the weight — source omitted (defaults 1.0).
    w = perf_weights(_ledger_with_trips("technical", 20.0, 2))
    assert "technical" not in w


def test_perf_weights_empty_ledger():
    assert perf_weights(SimpleNamespace(effective=lambda: [])) == {}
