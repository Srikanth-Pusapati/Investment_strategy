"""Tests for per-signal score history: trend + lag-decay weighting (E.1+R.4)."""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.models import Signal, SignalBundle, SignalKind
from investment_strategy.signals.history import SignalHistory, lag_weight


def _tmp() -> str:
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.unlink(path)  # start clean; SignalHistory creates it on save
    return path


def _bundle(symbol: str, kind: SignalKind, score: float) -> SignalBundle:
    sig = Signal(kind=kind, symbol=symbol, summary="x", score=score)
    return SignalBundle(symbol=symbol, signals=[sig])


_T0 = datetime(2026, 7, 6, 14, 30, tzinfo=timezone.utc)


def _feed(h: SignalHistory, symbol: str, kind: SignalKind,
          scores: list[float], hours_apart: float = 12.0,
          start: datetime = _T0) -> datetime:
    """Record a series of scores spaced hours_apart; returns the last timestamp."""
    now = start
    for sc in scores:
        h.record([_bundle(symbol, kind, sc)], now=now)
        now += timedelta(hours=hours_apart)
    return now - timedelta(hours=hours_apart)


# -- lag weight (E.1) -------------------------------------------------------- #

def test_lag_weight_orders_timing_sources_by_publication_lag():
    # Real-time > T+1 dark-pool > 2-day insider > ~30-day congress.
    w_tech = lag_weight(SignalKind.TECHNICAL)
    w_dark = lag_weight(SignalKind.OFFEXCHANGE)
    w_insider = lag_weight(SignalKind.INSIDER)
    w_congress = lag_weight(SignalKind.CONGRESS)
    assert w_tech == 1.0
    assert w_tech > w_dark > w_insider > w_congress > 0.0


def test_thesis_kinds_carry_no_lag_weight():
    # Fundamentals/macro/discovery are slow by nature, not stale — no decay.
    assert lag_weight(SignalKind.FUNDAMENTALS) is None
    assert lag_weight(SignalKind.MACRO) is None
    assert lag_weight(SignalKind.DISCOVERY) is None


# -- recording + persistence -------------------------------------------------- #

def test_record_persists_across_restart():
    path = _tmp()
    h1 = SignalHistory(path=path)
    _feed(h1, "AAPL", SignalKind.CONGRESS, [0.2, 0.4, 0.6])
    h2 = SignalHistory(path=path)  # fresh load == a process restart
    assert len(h2.series["AAPL"][SignalKind.CONGRESS.value]) == 3


def test_flat_stretches_compress_but_real_moves_record_immediately():
    h = SignalHistory(path=_tmp())
    now = _T0
    h.record([_bundle("AAPL", SignalKind.NEWS, 0.30)], now=now)
    # 30 min later, same score -> compressed away (min spacing not reached).
    h.record([_bundle("AAPL", SignalKind.NEWS, 0.30)],
             now=now + timedelta(minutes=30))
    assert len(h.series["AAPL"][SignalKind.NEWS.value]) == 1
    # 30 min later but a REAL move -> recorded despite the spacing.
    h.record([_bundle("AAPL", SignalKind.NEWS, 0.10)],
             now=now + timedelta(minutes=60))
    assert len(h.series["AAPL"][SignalKind.NEWS.value]) == 2


def test_same_cycle_scores_of_one_kind_average():
    h = SignalHistory(path=_tmp())
    b = SignalBundle(symbol="AAPL", signals=[
        Signal(kind=SignalKind.INSIDER, symbol="AAPL", summary="a", score=0.2),
        Signal(kind=SignalKind.INSIDER, symbol="AAPL", summary="b", score=0.6),
    ])
    h.record([b], now=_T0)
    assert h.series["AAPL"][SignalKind.INSIDER.value][0][1] == 0.4


def test_unscored_and_marketwide_signals_are_skipped():
    h = SignalHistory(path=_tmp())
    b = SignalBundle(symbol="AAPL", signals=[
        Signal(kind=SignalKind.NEWS, symbol="AAPL", summary="no score"),
        Signal(kind=SignalKind.MACRO, symbol=None, summary="market-wide", score=0.5),
    ])
    h.record([b], now=_T0)
    assert h.series == {}


def test_retention_prunes_old_points_and_empty_symbols():
    h = SignalHistory(path=_tmp())
    h.record([_bundle("OLD", SignalKind.NEWS, 0.5)], now=_T0)
    # 20 days later a different name records; OLD's lone point ages out.
    h.record([_bundle("NEW", SignalKind.NEWS, 0.5)],
             now=_T0 + timedelta(days=20))
    assert "OLD" not in h.series
    assert "NEW" in h.series


def test_corrupt_file_starts_clean():
    path = _tmp()
    with open(path, "w", encoding="utf-8") as f:
        f.write("{not json")
    h = SignalHistory(path=path)
    assert h.series == {}
    h.record([_bundle("AAPL", SignalKind.NEWS, 0.1)], now=_T0)  # still usable


# -- trend (R.4) --------------------------------------------------------------- #

def test_trend_improving():
    h = SignalHistory(path=_tmp())
    last = _feed(h, "AAPL", SignalKind.OFFEXCHANGE, [-0.1, 0.0, 0.1, 0.2, 0.3])
    t = h.trend("AAPL", SignalKind.OFFEXCHANGE, now=last)
    assert t is not None
    assert t["label"] == "improving"
    assert t["slope_per_day"] > 0
    assert t["n_obs"] == 5


def test_trend_decaying():
    h = SignalHistory(path=_tmp())
    last = _feed(h, "AAPL", SignalKind.NEWS, [0.6, 0.45, 0.3, 0.15])
    t = h.trend("AAPL", SignalKind.NEWS, now=last)
    assert t["label"] == "decaying"
    assert t["slope_per_day"] < 0


def test_trend_stable_when_flat():
    h = SignalHistory(path=_tmp())
    # Tiny wiggles so spacing compression doesn't collapse the series.
    last = _feed(h, "AAPL", SignalKind.NEWS, [0.30, 0.36, 0.30, 0.36])
    t = h.trend("AAPL", SignalKind.NEWS, now=last)
    assert t["label"] == "stable"


def test_trend_needs_enough_history():
    h = SignalHistory(path=_tmp())
    # Two points: below _MIN_OBS.
    last = _feed(h, "AAPL", SignalKind.NEWS, [0.1, 0.2])
    assert h.trend("AAPL", SignalKind.NEWS, now=last) is None
    # Three points but crammed into < 1 day of span.
    h2 = SignalHistory(path=_tmp())
    last = _feed(h2, "AAPL", SignalKind.NEWS, [0.1, 0.2, 0.3], hours_apart=3.0)
    assert h2.trend("AAPL", SignalKind.NEWS, now=last) is None
    assert h.trend("MSFT", SignalKind.NEWS) is None  # unknown symbol


def test_inflection_flags_sign_flip_over_slope_label():
    h = SignalHistory(path=_tmp())
    # A solidly bearish series whose LATEST print flips bullish.
    last = _feed(h, "AAPL", SignalKind.OFFEXCHANGE, [-0.4, -0.35, -0.3, 0.25])
    t = h.trend("AAPL", SignalKind.OFFEXCHANGE, now=last)
    assert t["label"] == "inflection-bullish"
    # And the mirror image.
    h2 = SignalHistory(path=_tmp())
    last = _feed(h2, "AAPL", SignalKind.NEWS, [0.4, 0.35, 0.3, -0.25])
    t2 = h2.trend("AAPL", SignalKind.NEWS, now=last)
    assert t2["label"] == "inflection-bearish"


def test_no_inflection_when_levels_are_trivial():
    h = SignalHistory(path=_tmp())
    # Sign flips but both sides are inside the noise floor -> not an inflection.
    last = _feed(h, "AAPL", SignalKind.NEWS, [0.05, 0.06, 0.05, -0.05])
    t = h.trend("AAPL", SignalKind.NEWS, now=last)
    assert t["label"] in ("stable", "decaying")


# -- prompt annotations --------------------------------------------------------- #

def test_annotate_combines_weight_and_trend():
    h = SignalHistory(path=_tmp())
    last = _feed(h, "AAPL", SignalKind.CONGRESS, [0.2, 0.4, 0.6, 0.8])
    note = h.annotate("AAPL", SignalKind.CONGRESS, now=last)
    assert note.startswith("[w=0.2")           # congress ~30d lag -> heavy discount
    assert "trend=improving(" in note
    assert note.endswith("]")


def test_annotate_thesis_kind_without_history_is_empty():
    h = SignalHistory(path=_tmp())
    assert h.annotate("AAPL", SignalKind.FUNDAMENTALS) == ""


def test_annotate_timing_kind_without_history_still_shows_weight():
    h = SignalHistory(path=_tmp())
    note = h.annotate("AAPL", SignalKind.OFFEXCHANGE)
    assert note.startswith("[w=0.9")
    assert "trend" not in note


def test_notes_for_builds_per_symbol_kind_map():
    h = SignalHistory(path=_tmp())
    last = _feed(h, "AAPL", SignalKind.OFFEXCHANGE, [-0.1, 0.0, 0.1, 0.2])
    bundles = [
        _bundle("AAPL", SignalKind.OFFEXCHANGE, 0.2),
        _bundle("MSFT", SignalKind.FUNDAMENTALS, 0.5),  # thesis kind, no history
    ]
    notes = h.notes_for(bundles, now=last)
    assert "trend=" in notes["AAPL"][SignalKind.OFFEXCHANGE.value]
    assert "MSFT" not in notes  # nothing to say -> no entry


# -- engine rendering ------------------------------------------------------------ #

def test_engine_renders_annotation_on_signal_line():
    from investment_strategy.config import Config
    from investment_strategy.decision.engine import DecisionEngine
    from investment_strategy.models import AccountSnapshot

    eng = DecisionEngine.__new__(DecisionEngine)  # skip API client construction
    eng.cfg = Config.__new__(Config)
    account = AccountSnapshot(
        equity=100_000, last_equity=100_000, cash=50_000, buying_power=50_000,
    )
    bundles = [_bundle("AAPL", SignalKind.CONGRESS, 0.6)]
    notes = {"AAPL": {SignalKind.CONGRESS.value: "[w=0.23 trend=improving(+0.09/d,4.1d)]"}}
    text = eng._render(bundles, account, "", [], signal_notes=notes)
    assert "- [congress] score=+0.60 [w=0.23 trend=improving(+0.09/d,4.1d)] x" in text
    # And without notes the line renders as before.
    text2 = eng._render(bundles, account, "", [])
    assert "- [congress] score=+0.60 x" in text2
