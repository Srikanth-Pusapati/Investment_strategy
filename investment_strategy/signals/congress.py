"""Congressional / senator trading signals.

Source: Quiver Quantitative (QUIVER_API_KEY). Without a key this provider is
disabled and contributes nothing. IMPORTANT: STOCK Act disclosures lag up to ~45
days, so this is a slow, weak signal — the decision prompt is told to treat it
as such. Do not size trades on congress data alone.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import requests

from ..config import Config
from ..models import Signal, SignalKind
from .base import SignalProvider

_QUIVER_URL = "https://api.quiverquant.com/beta/historical/congresstrading/{symbol}"


class CongressProvider(SignalProvider):
    name = "congress"

    def __init__(self, cfg: Config):
        self.cfg = cfg

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.quiver_api_key)

    def fetch(self, symbols: list[str]) -> list[Signal]:
        headers = {"Authorization": f"Bearer {self.cfg.quiver_api_key}"}
        cutoff = datetime.now(timezone.utc) - timedelta(days=90)
        signals: list[Signal] = []

        for symbol in symbols:
            r = requests.get(
                _QUIVER_URL.format(symbol=symbol), headers=headers, timeout=15
            )
            if r.status_code != 200:
                continue
            trades = r.json() or []
            buys = sells = 0
            for t in trades:
                if not self._recent(t.get("TransactionDate"), cutoff):
                    continue
                kind = (t.get("Transaction") or "").lower()
                if "purchase" in kind:
                    buys += 1
                elif "sale" in kind:
                    sells += 1
            total = buys + sells
            if total == 0:
                continue
            score = round((buys - sells) / total, 3)
            signals.append(Signal(
                kind=SignalKind.CONGRESS,
                symbol=symbol,
                summary=f"{buys} buys / {sells} sells by members in last 90d "
                        "(disclosures lag up to ~45d).",
                score=score,
                source="quiver",
                data={"buys": buys, "sells": sells},
            ))
        return signals

    @staticmethod
    def _recent(date_str: str | None, cutoff: datetime) -> bool:
        if not date_str:
            return False
        try:
            d = datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc)
        except ValueError:
            return False
        return d >= cutoff
