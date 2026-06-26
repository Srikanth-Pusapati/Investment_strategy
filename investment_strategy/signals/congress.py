"""Congressional / senator trading signals.

Source: Quiver Quantitative (QUIVER_API_KEY). Without a key this provider is
disabled and contributes nothing. IMPORTANT: STOCK Act disclosures lag up to ~45
days, so this is a slow, weak signal — the decision prompt is told to treat it
as such. Do not size trades on congress data alone.

This reads the per-symbol view out of the SHARED, cached cross-ticker live feed
(see QuiverClient) instead of fanning `historical/congresstrading/{symbol}` out
once per symbol. The screener pulls the same feed, so congress is now fetched at
most once per cycle total rather than once here PLUS once in the screener — the
double-pull fix that keeps us under Quiver's rate limit as datasets are added.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..config import Config
from ..models import Signal, SignalKind
from .base import SignalProvider
from .quiver_client import QuiverClient

_LOOKBACK_DAYS = 90


class CongressProvider(SignalProvider):
    name = "congress"

    def __init__(self, cfg: Config, quiver: QuiverClient | None = None):
        self.cfg = cfg
        self.quiver = quiver or QuiverClient(cfg.quiver_api_key)

    @property
    def enabled(self) -> bool:
        return self.quiver.enabled

    def fetch(self, symbols: list[str]) -> list[Signal]:
        wanted = {s.upper() for s in symbols}
        cutoff = datetime.now(timezone.utc) - timedelta(days=_LOOKBACK_DAYS)

        # One shared pull of the cross-ticker feed; tally per wanted symbol.
        buys: dict[str, int] = {s: 0 for s in wanted}
        sells: dict[str, int] = {s: 0 for s in wanted}
        for t in self.quiver.live("congresstrading"):
            sym = (t.get("Ticker") or "").strip().upper()
            if sym not in wanted or not self._recent(t.get("TransactionDate"), cutoff):
                continue
            kind = (t.get("Transaction") or "").lower()
            if "purchase" in kind:
                buys[sym] += 1
            elif "sale" in kind:
                sells[sym] += 1

        signals: list[Signal] = []
        for sym in wanted:
            b, s = buys[sym], sells[sym]
            total = b + s
            if total == 0:
                continue
            score = round((b - s) / total, 3)
            signals.append(Signal(
                kind=SignalKind.CONGRESS,
                symbol=sym,
                summary=f"{b} buys / {s} sells by members in last {_LOOKBACK_DAYS}d "
                        "(disclosures lag up to ~45d).",
                score=score,
                source="quiver",
                data={"buys": b, "sells": s},
            ))
        return signals

    @staticmethod
    def _recent(date_str: str | None, cutoff: datetime) -> bool:
        if not date_str:
            return False
        try:
            d = datetime.fromisoformat(date_str[:10]).replace(tzinfo=timezone.utc)
        except ValueError:
            return False
        return d >= cutoff
