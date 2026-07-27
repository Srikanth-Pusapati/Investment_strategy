"""Per-signal score history: trend + lag-decay weighting (Todo-3 E.1 + R.4).

Two things the raw signal level can't tell the decision model:

  1. LAG (E.1): a +0.6 congress score is built from filings disclosed up to
     ~45 days after the trade; a +0.6 dark-pool print is ~1 day old. The prompt
     says "weight by lag" qualitatively — this module formalizes it as a
     per-kind FRESHNESS WEIGHT derived from each source's typical publication
     lag (w = 0.5 ** (lag / half-life)). Kind-level, because providers don't
     carry uniform per-event dates (congress data is already an aggregate).
     Thesis-class kinds (fundamentals, macro, discovery) are exempt: they are
     slow by nature, not stale — decaying them would punish the thesis leg.

  2. TREND (R.4, from enving/TradeAgent's sentiment_tracker): an IMPROVING
     2-day-old signal is not the same as a DECAYING one at the same level.
     Each cycle's scores are persisted per (symbol, kind) to state/ and a
     least-squares slope over the retained window classifies the series as
     improving / decaying / stable, with an INFLECTION override when the
     latest score flips sign against the prior series.

Both are rendered as a compact bracketed annotation on each signal line in
the decision prompt (see DecisionEngine._render). This is EVIDENCE for the
LLM, not a risk control: Claude proposes, risk disposes — nothing here sizes
or vetoes a trade, so no .env knob changes (record-account freeze friendly).
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

from ..models import SignalBundle, SignalKind

log = logging.getLogger("signals")

DEFAULT_HISTORY_PATH = Path("state") / "signal_history.json"

# Typical publication lag of each TIMING/CATALYST source, in days — the time
# between the underlying event and us being able to observe it. Thesis-class
# kinds are deliberately absent (no decay). Sources for the numbers: FINRA
# off-exchange prints T+1; SEC Form 4 is due within 2 business days; STOCK Act
# disclosures run up to ~45d (typ. ~30); contract awards hit the feed within
# ~a week; news/technical/options-chain are effectively live.
KIND_LAG_DAYS: dict[SignalKind, float] = {
    SignalKind.TECHNICAL: 0.0,
    SignalKind.NEWS: 0.5,
    SignalKind.OPTIONS_CHAIN: 0.0,
    SignalKind.OFFEXCHANGE: 1.0,
    SignalKind.INSIDER: 2.0,
    SignalKind.GOVCONTRACTS: 7.0,
    SignalKind.CONGRESS: 30.0,
    SignalKind.LOBBYING: 45.0,
}

# A source whose event is one half-life old carries half the timing weight.
_LAG_HALF_LIFE_DAYS = 14.0

# Series shape: at most one point per (symbol, kind) per _MIN_SPACING_HOURS
# unless the score moved by _RECORD_JUMP (inflections must land immediately);
# points older than the retention window are pruned, series capped at
# _MAX_POINTS. At the 30-60 min cycle cadence this holds roughly two weeks
# of trading history in a few points per day.
_MIN_SPACING_HOURS = 2.0
_RECORD_JUMP = 0.05
_RETENTION_DAYS = 14.0
_MAX_POINTS = 48

# Trend classification: slope is score-units per day from a least-squares fit.
# Below _MIN_OBS points or _MIN_SPAN_DAYS of span there is no trend, only noise.
_MIN_OBS = 3
_MIN_SPAN_DAYS = 1.0
_SLOPE_FLAT = 0.04         # |slope| under this = stable
_INFLECTION_LEVEL = 0.10   # sign flip only counts when both sides are non-trivial


def lag_weight(kind: SignalKind) -> float | None:
    """Freshness weight in (0, 1] for timing signals; None for thesis kinds
    (fundamentals/macro/discovery), which don't decay by design."""
    lag = KIND_LAG_DAYS.get(kind)
    if lag is None:
        return None
    return round(0.5 ** (lag / _LAG_HALF_LIFE_DAYS), 2)


class SignalHistory:
    """JSON-backed per-(symbol, kind) score series with trend classification.

    Follows the PortfolioState pattern: atomic tmp-swap save, corrupt file =
    clean start, an RLock around mutation + save (gather runs on the decision
    thread but the file also gets read by tests/tools)."""

    def __init__(self, path: Path | str = DEFAULT_HISTORY_PATH):
        self.path = Path(path)
        # symbol -> kind value -> list of [iso_ts, score]
        self.series: dict[str, dict[str, list[list]]] = {}
        self._lock = threading.RLock()
        self._load()

    # -- persistence -------------------------------------------------------- #
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.series = {
                str(sym): {
                    str(kind): [[str(ts), float(sc)] for ts, sc in pts]
                    for kind, pts in kinds.items()
                }
                for sym, kinds in raw.get("series", {}).items()
            }
        except Exception as e:  # corrupt history must not crash startup
            log.error("Could not read %s (%s); starting signal history fresh.",
                      self.path, e)
            self.series = {}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({"series": self.series}), encoding="utf-8",
            )
            tmp.replace(self.path)
        except Exception as e:  # never let history persistence break the cycle
            log.warning("Signal history save failed: %s", e)

    # -- recording ----------------------------------------------------------- #
    def record(self, bundles: list[SignalBundle], now: datetime | None = None) -> None:
        """Append this cycle's scores. One point per (symbol, kind): the mean of
        that kind's scored signals in the bundle. Market-wide (symbol-less)
        signals are skipped — R.4 is a per-ticker series."""
        now = now or datetime.now(timezone.utc)
        with self._lock:
            for b in bundles:
                for kind_value, score in self._cycle_scores(b).items():
                    self._append(b.symbol, kind_value, score, now)
            self._prune(now)
            self._save()

    @staticmethod
    def _cycle_scores(bundle: SignalBundle) -> dict[str, float]:
        sums: dict[str, list[float]] = {}
        for s in bundle.signals:
            if s.score is None or not s.symbol:
                continue
            sums.setdefault(s.kind.value, []).append(float(s.score))
        return {k: sum(v) / len(v) for k, v in sums.items()}

    def _append(self, symbol: str, kind_value: str, score: float,
                now: datetime) -> None:
        pts = self.series.setdefault(symbol, {}).setdefault(kind_value, [])
        if pts:
            last_ts, last_score = pts[-1]
            age_h = self._hours_between(last_ts, now)
            # Compress flat stretches; always capture a real move immediately.
            if (age_h is not None and age_h < _MIN_SPACING_HOURS
                    and abs(score - last_score) < _RECORD_JUMP):
                return
        pts.append([now.isoformat(), round(float(score), 4)])
        if len(pts) > _MAX_POINTS:
            del pts[: len(pts) - _MAX_POINTS]

    def _prune(self, now: datetime) -> None:
        """Drop points past retention and any emptied series/symbols."""
        for sym in list(self.series):
            kinds = self.series[sym]
            for kind_value in list(kinds):
                kinds[kind_value] = [
                    p for p in kinds[kind_value]
                    if (h := self._hours_between(p[0], now)) is not None
                    and h <= _RETENTION_DAYS * 24.0
                ]
                if not kinds[kind_value]:
                    del kinds[kind_value]
            if not kinds:
                del self.series[sym]

    # -- trend --------------------------------------------------------------- #
    def trend(self, symbol: str, kind: SignalKind,
              now: datetime | None = None) -> dict | None:
        """Least-squares drift of the stored series, or None when there isn't
        enough history to call it. Returns {label, slope_per_day, span_days,
        n_obs}; label is improving/decaying/stable or inflection-bullish/
        inflection-bearish when the latest score flips sign vs the prior run."""
        pts = self.series.get(symbol, {}).get(kind.value, [])
        if len(pts) < _MIN_OBS:
            return None
        now = now or datetime.now(timezone.utc)
        times: list[float] = []
        scores: list[float] = []
        for ts, sc in pts:
            h = self._hours_between(ts, now)
            if h is None:
                continue
            times.append(-h / 24.0)  # days relative to now (negative = past)
            scores.append(float(sc))
        if len(times) < _MIN_OBS:
            return None
        span = max(times) - min(times)
        if span < _MIN_SPAN_DAYS:
            return None

        n = len(times)
        mean_t = sum(times) / n
        mean_s = sum(scores) / n
        var_t = sum((t - mean_t) ** 2 for t in times)
        if var_t <= 0:
            return None
        slope = sum((t - mean_t) * (s - mean_s)
                    for t, s in zip(times, scores)) / var_t

        latest = scores[-1]
        prior_mean = sum(scores[:-1]) / (n - 1)
        if (abs(latest) >= _INFLECTION_LEVEL and abs(prior_mean) >= _INFLECTION_LEVEL
                and (latest > 0) != (prior_mean > 0)):
            label = "inflection-bullish" if latest > 0 else "inflection-bearish"
        elif slope >= _SLOPE_FLAT:
            label = "improving"
        elif slope <= -_SLOPE_FLAT:
            label = "decaying"
        else:
            label = "stable"
        return {
            "label": label,
            "slope_per_day": round(slope, 3),
            "span_days": round(span, 1),
            "n_obs": n,
        }

    # -- prompt annotation ---------------------------------------------------- #
    def annotate(self, symbol: str, kind: SignalKind,
                 now: datetime | None = None) -> str:
        """Compact bracketed note for one signal line in the decision prompt,
        e.g. '[w=0.23 trend=improving(+0.09/d,4.1d)]'. Empty string when there
        is nothing to say (thesis kind with no trend history yet)."""
        parts: list[str] = []
        w = lag_weight(kind)
        if w is not None:
            parts.append(f"w={w:.2f}")
        t = self.trend(symbol, kind, now)
        if t is not None:
            parts.append(
                f"trend={t['label']}({t['slope_per_day']:+.2f}/d,{t['span_days']:.1f}d)"
            )
        return f"[{' '.join(parts)}]" if parts else ""

    def notes_for(self, bundles: list[SignalBundle],
                  now: datetime | None = None) -> dict[str, dict[str, str]]:
        """symbol -> kind value -> annotation, for every scored signal in the
        slate. Built once per cycle and handed to DecisionEngine so the engine
        stays decoupled from the history store."""
        now = now or datetime.now(timezone.utc)
        notes: dict[str, dict[str, str]] = {}
        for b in bundles:
            for s in b.signals:
                if s.score is None or not s.symbol:
                    continue
                note = self.annotate(b.symbol, s.kind, now)
                if note:
                    notes.setdefault(b.symbol, {})[s.kind.value] = note
        return notes

    # -- helpers --------------------------------------------------------------#
    @staticmethod
    def _hours_between(ts: str, now: datetime) -> float | None:
        try:
            then = datetime.fromisoformat(ts)
        except (TypeError, ValueError):
            return None
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        return (now - then).total_seconds() / 3600.0
