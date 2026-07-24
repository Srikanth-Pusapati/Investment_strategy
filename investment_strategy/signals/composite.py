"""Deterministic weighted signal index — the numeric prior beside the LLM.

The decision path previously had NO deterministic aggregation of a candidate's
signals: every per-signal score went to Claude one line at a time and the model
alone produced `conviction`. The week of 2026-07-13 showed why that needs a
counterweight (conviction was inversely correlated with realized outcomes).
This module combines primitives that already existed but were never joined:

  composite = sum over kinds of  mean(kind scores)
                                 x lag_weight(kind)        (signals/history.py)
                                 x perf_weight(kind)       (attribution.py)

- Per-kind MEAN first, so one chatty provider (many news rows) can't dominate
  the sum the way it would in a flat per-signal loop.
- `lag_weight` discounts stale-by-construction sources (congress ~0.23) and is
  None for thesis kinds (fundamentals/macro/discovery) which don't decay —
  those get weight 1.0 here.
- `perf_weight` tilts toward sources whose closed round-trips actually made
  money in THIS book (realized attribution), clamped to 0.5..1.5 and inert
  (1.0) until a source has COMPOSITE_PERF_MIN_TRIPS closed trips.

Pure functions, no I/O beyond the ledger object handed in — trivially testable.
Consumers: prompt anchor line (decision/engine.py), cycle budget blend and
opt-in buy floor (orchestrator.py / risk.py), and the buy row in the ledger so
future calibration can score the composite itself.
"""
from __future__ import annotations

from ..ledger import TradeLedger
from ..models import SignalBundle, SignalKind
from .history import lag_weight

# Below this many closed round-trips a source's performance weight stays 1.0 —
# two lucky trades must not double a source's say.
COMPOSITE_PERF_MIN_TRIPS = 3

# +/- this avg realized P&L (%) maps to the full 0.5..1.5 clamp: a source
# averaging -5% halves its weight, +5% adds half.
_PERF_SPAN = 10.0


def perf_weights(
    ledger: TradeLedger, min_trips: int = COMPOSITE_PERF_MIN_TRIPS,
) -> dict[str, float]:
    """SignalKind-value -> 0.5..1.5 multiplier from realized attribution.

    weight = clamp(1.0 + avg_pl_pct / _PERF_SPAN, 0.5, 1.5) once a source has
    `min_trips` closed round-trips; 1.0 (inert) below that. Uses the same
    reconciled round-trip reconstruction as the prompt's Track record block,
    on the CITED basis (Jul-24): presence-based stats were bunched within
    ~0.9pp — every source got the same mild haircut and the weights
    differentiated nothing — while cited stats spread ~6pp and actually
    separate earners from bleeders. Report-only cited buckets that aren't
    SignalKind values ("options_flow", "composite") land in the dict harmlessly:
    composite_score() looks up by kind.value and never sees them.
    """
    from ..attribution import attribute, round_trips

    stats = attribute(round_trips(ledger.effective()), basis="cited")
    out: dict[str, float] = {}
    for kind, s in stats.items():
        if s.trips < min_trips:
            continue
        out[kind] = max(0.5, min(1.5, 1.0 + s.avg_pl_pct / _PERF_SPAN))
    return out


def composite_score(
    bundle: SignalBundle, perf_w: dict[str, float] | None = None,
) -> float | None:
    """The bundle's deterministic weighted index, or None if nothing is scored.

    Sum over kinds of mean(kind scores) x lag_weight x perf_weight. Bounded in
    practice by the number of signal kinds (~7 active), each term in [-1.5, 1.5];
    typical values land in roughly -2..+2 with corroborated names near the top.
    """
    perf_w = perf_w or {}
    by_kind: dict[SignalKind, list[float]] = {}
    for s in bundle.signals:
        if s.score is None:
            continue
        by_kind.setdefault(s.kind, []).append(s.score)
    if not by_kind:
        return None
    total = 0.0
    for kind, scores in by_kind.items():
        mean = sum(scores) / len(scores)
        lw = lag_weight(kind)
        total += mean * (lw if lw is not None else 1.0) * perf_w.get(kind.value, 1.0)
    return round(total, 2)
