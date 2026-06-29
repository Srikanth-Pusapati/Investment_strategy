"""Discovery screener: recent congressional buying, across ALL tickers.

Where signals/congress.py asks "are members trading THIS symbol?", this asks
"which symbols are members buying right now?" — the discovery direction. Uses
Quiver's live (cross-ticker) congress-trading feed and clusters recent purchases
by ticker. STOCK Act disclosures lag up to ~45 days, so this is a slow, weak
tell; the decision prompt is told to treat it as such. Disabled without a key.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from ..config import Config
from ..models import Candidate
from ..signals.quiver_client import QuiverClient
from .base import Screener

log = logging.getLogger("screener")

_LOOKBACK_DAYS = 60
_CLUSTER_FULL = 4        # filings at which buy/sell conviction saturates to full weight


class CongressFeedScreener(Screener):
    name = "congress"

    def __init__(self, cfg: Config, quiver: QuiverClient | None = None):
        self.cfg = cfg
        self.quiver = quiver or QuiverClient(cfg.quiver_api_key)

    @property
    def enabled(self) -> bool:
        return self.quiver.enabled

    def scan(self) -> list[Candidate]:
        # Shared, cached pull — the signal layer reads this same response.
        feed = self.quiver.live("congresstrading")

        cutoff = datetime.now(timezone.utc) - timedelta(days=_LOOKBACK_DAYS)
        buys: dict[str, int] = defaultdict(int)
        sells: dict[str, int] = defaultdict(int)
        for t in feed:
            sym = (t.get("Ticker") or "").strip().upper()
            if not sym or not self._recent(t.get("TransactionDate"), cutoff):
                continue
            kind = (t.get("Transaction") or "").lower()
            if "purchase" in kind:
                buys[sym] += 1
            elif "sale" in kind:
                sells[sym] += 1

        candidates: list[Candidate] = []
        for sym in set(buys) | set(sells):
            b, s = buys[sym], sells[sym]
            total = b + s
            if total == 0:
                continue
            # Weight the net buy ratio by cluster size so a lone filing can't
            # score the same ±1.0 as a real cluster — confidence saturates at
            # _CLUSTER_FULL filings. Keeps thin single trades from flooding the
            # cap and crowding out corroborated insider/flow names.
            conf = min(total, _CLUSTER_FULL) / _CLUSTER_FULL
            score = round((b - s) / total * conf, 3)
            candidates.append(Candidate(
                symbol=sym,
                sources=[self.name],
                reason=f"Congress: {b} buy / {s} sell filings in last "
                       f"{_LOOKBACK_DAYS}d (disclosures lag up to ~45d).",
                score=score,
            ))
        return candidates

    @staticmethod
    def _recent(date_str: str | None, cutoff: datetime) -> bool:
        if not date_str:
            return False
        try:
            d = datetime.fromisoformat(date_str[:10]).replace(tzinfo=timezone.utc)
        except ValueError:
            return False
        return d >= cutoff
