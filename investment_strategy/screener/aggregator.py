"""Runs every enabled screener, merges their candidates into a single ranked,
deduped, capped list of NEW symbols to evaluate this cycle.

Mirrors signals/aggregator.py: each source degrades to [] on failure, so the
scan proceeds on whatever responded. The output is bounded by min_score and
max_candidates so a wide market scan can't blow up the decision prompt or the
downstream per-symbol signal calls.
"""
from __future__ import annotations

import logging

from ..config import Config
from ..models import Candidate
from .base import Screener
from .congress_feed import CongressFeedScreener
from .insider_feed import InsiderFeedScreener
from .options_flow_feed import OptionsFlowScreener

log = logging.getLogger("screener")

# Source name (as used in SCREENER_SOURCES) -> screener class.
_REGISTRY: dict[str, type[Screener]] = {
    "congress": CongressFeedScreener,
    "insider": InsiderFeedScreener,
    "options_flow": OptionsFlowScreener,
}


class ScreenerAggregator:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.screeners: list[Screener] = []
        for src in cfg.screener.sources:
            cls = _REGISTRY.get(src)
            if cls is None:
                log.warning("Unknown screener source %r; ignoring.", src)
                continue
            self.screeners.append(cls(cfg))

    def scan(self, exclude: set[str] | None = None) -> list[Candidate]:
        """Discover candidate symbols, excluding names already being evaluated
        (current watchlist + held positions). Returns a ranked, capped list."""
        exclude = {s.upper() for s in (exclude or set())}

        # Merge candidates from every source, keyed by symbol. A name flagged by
        # multiple screeners (e.g. congress + insider) accumulates their sources,
        # reasons, and scores — that corroboration is exactly what we want to rank
        # to the top.
        merged: dict[str, Candidate] = {}
        for s in self.screeners:
            for cand in s.safe_scan():
                sym = cand.symbol.upper()
                if sym in exclude:
                    continue
                existing = merged.get(sym)
                if existing is None:
                    merged[sym] = cand.model_copy(update={"symbol": sym})
                else:
                    existing.sources = list(dict.fromkeys(existing.sources + cand.sources))
                    existing.reason = f"{existing.reason} | {cand.reason}"
                    existing.score += cand.score

        # Drop weak signals, rank by conviction (|score|), cap the slate. A
        # bearish lean on a name we DON'T hold is only actionable via a long put;
        # with options off there's nothing to do but HOLD it, so don't spend a
        # candidate slot (or prompt tokens) on it.
        min_score = self.cfg.screener.min_score
        options_on = self.cfg.risk.options_enabled
        cands = [
            c for c in merged.values()
            if (c.score >= min_score) or (options_on and c.score <= -min_score)
        ]
        cands.sort(key=lambda c: abs(c.score), reverse=True)
        capped = cands[: self.cfg.screener.max_candidates]

        log.info(
            "Discovered %d candidate(s) from %d source(s) "
            "(%d merged, %d above min_score, capped to %d).",
            len(capped), len(self.screeners), len(merged), len(cands),
            self.cfg.screener.max_candidates,
        )
        for c in capped:
            log.info("  candidate %s [%s] score=%+.2f — %s",
                     c.symbol, "+".join(c.sources), c.score, c.reason)
        return capped
