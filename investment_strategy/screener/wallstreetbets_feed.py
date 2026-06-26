"""Discovery screener: WallStreetBets mention surges, across all tickers.

Source: Quiver Quantitative `live/wallstreetbets`, read through the SHARED,
per-cycle-cached QuiverClient. Asks "which names is retail piling into right now?"
— an early-warning for momentum/squeeze setups (~1-day lag). Retail hype is fast
and noisy, so the lean is damped and self-calibrated to the feed (ranked by
mention volume relative to the busiest name), and the decision prompt still
requires a real thesis before sizing. Disabled without a key.

A surfaced name leans BULLISH by default (mentions usually accompany buying), but
if the feed carries a sentiment field we sign the lean by it. Bearish leans on a
name we don't hold are only actionable via a long put, so the aggregator filters
them out unless options are enabled — fine, they still corroborate held names.

SCHEMA NOTE: field names parsed defensively — VERIFY against the live payload and
trim the candidate lists once confirmed.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from ..config import Config
from ..models import Candidate
from ..signals.quiver_client import QuiverClient
from .base import Screener

log = logging.getLogger("screener")

_LOOKBACK_DAYS = 3          # retail momentum is fast; only very recent mentions count
_MAX_LEAN = 0.6            # cap: a noisy, hype-driven tell never dominates

_TICKER_KEYS = ("Ticker", "ticker", "Symbol")
_MENTION_KEYS = ("Mentions", "mentions", "Count")
_SENTIMENT_KEYS = ("Sentiment", "sentiment")
_DATE_KEYS = ("Date", "date")


class WallStreetBetsScreener(Screener):
    name = "wallstreetbets"

    def __init__(self, cfg: Config, quiver: QuiverClient | None = None):
        self.cfg = cfg
        self.quiver = quiver or QuiverClient(cfg.quiver_api_key)

    @property
    def enabled(self) -> bool:
        return self.quiver.enabled

    def scan(self) -> list[Candidate]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=_LOOKBACK_DAYS)
        mentions: dict[str, float] = defaultdict(float)
        sent_sum: dict[str, float] = defaultdict(float)
        sent_n: dict[str, int] = defaultdict(int)

        for row in self.quiver.live("wallstreetbets"):
            sym = self._str(row, _TICKER_KEYS).upper()
            if not sym or not self._recent(self._str(row, _DATE_KEYS), cutoff):
                continue
            m = self._num(row, _MENTION_KEYS)
            if m is None or m <= 0:
                continue
            mentions[sym] += m
            s = self._num(row, _SENTIMENT_KEYS)
            if s is not None:
                sent_sum[sym] += s
                sent_n[sym] += 1

        if not mentions:
            return []

        # Self-calibrate: score relative to the busiest name in this feed, so we
        # don't need to hard-code an absolute mention scale we can't know.
        peak = max(mentions.values())
        candidates: list[Candidate] = []
        for sym, total in mentions.items():
            norm = total / peak                       # 0..1, busiest name = 1
            sign = 1.0                                # mentions ~ buying by default
            if sent_n[sym]:
                avg_sent = sent_sum[sym] / sent_n[sym]
                if avg_sent < 0:
                    sign = -1.0
            score = round(sign * norm * _MAX_LEAN, 3)
            candidates.append(Candidate(
                symbol=sym,
                sources=[self.name],
                reason=f"WSB: {total:.0f} mentions in last {_LOOKBACK_DAYS}d "
                       f"(retail momentum; ~1d lag, noisy).",
                score=score,
            ))
        return candidates

    # -- defensive field parsing ------------------------------------------- #
    @staticmethod
    def _recent(date_str: str, cutoff: datetime) -> bool:
        if not date_str:
            return True   # undated live rows are assumed current
        try:
            d = datetime.fromisoformat(date_str[:10]).replace(tzinfo=timezone.utc)
        except ValueError:
            return True
        return d >= cutoff

    @staticmethod
    def _num(row: dict[str, Any], keys: tuple[str, ...]) -> float | None:
        for k in keys:
            v = row.get(k)
            if v is None:
                continue
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _str(row: dict[str, Any], keys: tuple[str, ...]) -> str:
        for k in keys:
            v = row.get(k)
            if v:
                return str(v)
        return ""
