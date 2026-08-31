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
from ..models import Candidate, SignalKind, is_valid_ticker
from ..signals.history import lag_weight
from ..signals.quiver_client import QuiverClient
from .base import Screener
from .congress_feed import CongressFeedScreener
from .insider_feed import InsiderFeedScreener
from .options_flow_feed import OptionsFlowScreener
from .robinhood_feed import RobinhoodMoversScreener
from .robinhood_scan_feed import RobinhoodScanScreener
from .wallstreetbets_feed import WallStreetBetsScreener

log = logging.getLogger("screener")

# Source name (as used in SCREENER_SOURCES) -> screener class.
_REGISTRY: dict[str, type[Screener]] = {
    "congress": CongressFeedScreener,
    "insider": InsiderFeedScreener,
    "options_flow": OptionsFlowScreener,
    "robinhood": RobinhoodMoversScreener,
    "robinhood_scans": RobinhoodScanScreener,
    "wallstreetbets": WallStreetBetsScreener,
}
# Quiver-backed screeners take the shared client so they reuse the signal layer's
# cached live-feed pull instead of fetching it a second time.
_QUIVER_SCREENERS: set[type[Screener]] = {CongressFeedScreener, WallStreetBetsScreener}

# Freshness discount applied to each screener's score at merge time, using the
# same lag half-life as the composite (signals/history.py). STOCK Act congress
# disclosures lag up to ~45 days and Form 4 ~2 business days; live sources
# (options flow, Robinhood crowd, WSB chatter) keep full weight. Without this
# a congress-only cluster ranked the slate's top AND laundered into the
# composite at FULL weight via the DISCOVERY kind — the lag discount the
# composite applies to the `congress` signal never touched the screener score
# it rode in on (BEP 2026-07-27: bought on "congress 7/0", composite +1.66
# mostly discovery, stopped out -4.0% five hours later).
_SOURCE_LAG_KIND: dict[str, SignalKind] = {
    "congress": SignalKind.CONGRESS,
    "insider": SignalKind.INSIDER,
}


def _source_weight(name: str) -> float:
    kind = _SOURCE_LAG_KIND.get(name)
    w = lag_weight(kind) if kind is not None else None
    return w if w is not None else 1.0


class ScreenerAggregator:
    def __init__(
        self, cfg: Config, quiver: QuiverClient | None = None, broker=None,
    ):
        self.cfg = cfg
        self.quiver = quiver or QuiverClient(cfg.quiver_api_key)
        # Optional price reader (AlpacaClient) for the pre-cap liquidity floor;
        # None = floor skipped (the orchestrator's post-slate floor still applies).
        self.broker = broker
        self.screeners: list[Screener] = []
        for src in cfg.screener.sources:
            cls = _REGISTRY.get(src)
            if cls is None:
                log.warning("Unknown screener source %r; ignoring.", src)
                continue
            self.screeners.append(
                cls(cfg, self.quiver) if cls in _QUIVER_SCREENERS else cls(cfg)
            )

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
            src_w = _source_weight(s.name)
            for cand in s.safe_scan():
                sym = cand.symbol.upper()
                if not is_valid_ticker(sym):
                    log.debug("Dropping invalid candidate symbol %r from %s.", sym, s.name)
                    continue
                if sym in exclude:
                    continue
                score = round(cand.score * src_w, 3)
                existing = merged.get(sym)
                if existing is None:
                    merged[sym] = cand.model_copy(
                        update={"symbol": sym, "score": score}
                    )
                else:
                    existing.sources = list(dict.fromkeys(existing.sources + cand.sources))
                    existing.reason = f"{existing.reason} | {cand.reason}"
                    existing.score += score

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
        cands = self._apply_liquidity_floor(cands)
        cands.sort(key=lambda c: abs(c.score), reverse=True)
        capped = self._cap_with_bearish_reserve(cands, options_on)

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

    def _apply_liquidity_floor(self, cands: list[Candidate]) -> list[Candidate]:
        """Run-6 universe hygiene: drop sub-floor names BEFORE the slate cap and
        the bearish reserve, so they never consume a capped slot, a per-symbol
        signal fetch or prompt tokens (48% of run-5 slate exclusions were
        sub-$5 names the orchestrator's later floor rejected anyway).

        Prices come from ONE batched broker read for the whole merged set;
        the optional ADV floor (screener.min_adv_usd > 0) is ONE batched daily
        bars read. A name whose price/ADV is unknown is KEPT (fail open — the
        orchestrator's floor and the risk gate still stand behind it). Any
        broker error leaves the list untouched."""
        scr = self.cfg.screener
        broker = getattr(self, "broker", None)
        if not cands or broker is None:
            return cands
        floor = float(getattr(self.cfg.risk, "min_trade_price_usd", 0.0) or 0.0)
        use_px = bool(getattr(scr, "price_floor_pre_cap", False)) and floor > 0
        adv_floor = float(getattr(scr, "min_adv_usd", 0.0) or 0.0)
        if not use_px and adv_floor <= 0:
            return cands
        syms = [c.symbol for c in cands]
        try:
            prices = broker.latest_prices(syms) if use_px else {}
            advs = broker.avg_dollar_volume(syms) if adv_floor > 0 else {}
        except Exception as e:  # defensive: never let hygiene break discovery
            log.warning("screener liquidity floor read failed: %s", e)
            return cands
        kept: list[Candidate] = []
        dropped: list[str] = []
        for c in cands:
            px = prices.get(c.symbol)
            adv = advs.get(c.symbol)
            if use_px and px is not None and px < floor:
                dropped.append(f"{c.symbol} ${px:.2f}<${floor:g}")
                continue
            if adv_floor > 0 and adv is not None and adv < adv_floor:
                dropped.append(f"{c.symbol} ADV ${adv / 1e6:.1f}M<${adv_floor / 1e6:g}M")
                continue
            kept.append(c)
        if dropped:
            log.info(
                "Screener liquidity floor dropped %d/%d pre-cap: %s",
                len(dropped), len(cands), ", ".join(dropped),
            )
        return kept

    def _cap_with_bearish_reserve(
        self, cands: list[Candidate], options_on: bool
    ) -> list[Candidate]:
        """Cap to max_candidates by |score|, but guarantee up to `bearish_reserve`
        slots for the STRONGEST bearish names clearing `bearish_reserve_bar`, so a
        bull-heavy tape can't crowd every short setup off the capped slate. `cands`
        must already be sorted by |score| descending.

        Guardrails: only names past the bar are eligible (a weak bearish name is
        never forced in); a guaranteed name is never re-capped back out; and when
        the natural top-N already includes the strong bearish names (the common
        case) this is a no-op."""
        max_c = self.cfg.screener.max_candidates
        if len(cands) <= max_c:
            return cands[:max_c]
        reserve = getattr(self.cfg.screener, "bearish_reserve", 0)
        bar = getattr(self.cfg.screener, "bearish_reserve_bar", 0.4)
        if reserve <= 0 or not options_on:
            return cands[:max_c]
        # cands is |score|-sorted, so the first matches ARE the strongest bearish.
        guaranteed_idx = [
            i for i, c in enumerate(cands) if c.score <= -bar
        ][: min(reserve, max_c)]
        if not guaranteed_idx:
            return cands[:max_c]
        guaranteed = set(guaranteed_idx)
        picked = list(guaranteed_idx)                 # reserved first — never dropped
        for i in range(len(cands)):                   # fill the rest by |score|
            if len(picked) >= max_c:
                break
            if i not in guaranteed:
                picked.append(i)
        picked.sort(key=lambda i: abs(cands[i].score), reverse=True)
        return [cands[i] for i in picked]
