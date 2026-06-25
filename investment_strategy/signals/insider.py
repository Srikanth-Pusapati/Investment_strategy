"""Smart-money signal from insider transactions (Form 4 data).

Clustered insider BUYING is one of the more reliable "smart money" tells — execs
buy their own stock for one reason; they sell for many. Form 4 lands ~2 business
days after the trade, so this is timelier than 13F or congressional data.

Source: Finnhub's parsed insider-transactions endpoint (needs FINNHUB_API_KEY).
The raw filings are also free from SEC EDGAR, but EDGAR returns filing metadata,
not parsed P/S transaction codes — you'd have to fetch and parse each Form 4 XML.
Finnhub does that parsing for us, so we use it when a key is present.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import requests

from ..config import Config
from ..models import Signal, SignalKind
from .base import SignalProvider

log = logging.getLogger("signals")

_URL = "https://finnhub.io/api/v1/stock/insider-transactions"


class InsiderProvider(SignalProvider):
    name = "insider"

    def __init__(self, cfg: Config):
        self.cfg = cfg

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.finnhub_api_key)

    def fetch(self, symbols: list[str]) -> list[Signal]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).date()
        signals: list[Signal] = []
        for symbol in symbols:
            txns = self._transactions(symbol)
            if txns is None:
                continue
            buy_sh = sell_sh = 0
            for t in txns:
                if not self._recent(t.get("transactionDate"), cutoff):
                    continue
                change = t.get("change") or 0
                code = (t.get("transactionCode") or "").upper()
                if code == "P" or change > 0:      # purchase
                    buy_sh += abs(change)
                elif code == "S" or change < 0:    # sale
                    sell_sh += abs(change)
            total = buy_sh + sell_sh
            if total == 0:
                continue
            score = round((buy_sh - sell_sh) / total, 3)
            signals.append(Signal(
                kind=SignalKind.CONGRESS,  # shares the "smart-money" reasoning bucket
                symbol=symbol,
                summary=f"Insider net (90d): {buy_sh:,} sh bought / {sell_sh:,} sold.",
                score=score,
                source="finnhub-insider",
                data={"buy_shares": buy_sh, "sell_shares": sell_sh},
            ))
        return signals

    def _transactions(self, symbol: str) -> list[dict] | None:
        try:
            r = requests.get(_URL, params={
                "symbol": symbol, "token": self.cfg.finnhub_api_key,
            }, timeout=15)
            if r.status_code != 200:
                return None
            return r.json().get("data", [])
        except Exception as e:
            log.debug("insider fetch failed for %s: %s", symbol, e)
            return None

    @staticmethod
    def _recent(date_str: str | None, cutoff) -> bool:
        if not date_str:
            return False
        try:
            return datetime.fromisoformat(date_str[:10]).date() >= cutoff
        except ValueError:
            return False
