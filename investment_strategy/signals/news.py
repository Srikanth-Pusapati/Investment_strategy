"""News & sentiment signals.

Headlines come from Alpaca's news API (Benzinga-backed) — same credentials you
already have. Sentiment is scored two ways:
  - If FINNHUB_API_KEY is set, use Finnhub's news-sentiment score (model-based,
    aggregated across many sources) — the preferred path.
  - Otherwise fall back to a lightweight keyword pass on the headlines.
Claude still reads the actual headlines, so it can override a noisy score.
"""
from __future__ import annotations

import logging

import requests

from ..config import Config
from ..models import Signal, SignalKind
from .base import SignalProvider

log = logging.getLogger("signals")

_BULLISH = {"beat", "surge", "soar", "upgrade", "record", "growth", "rally", "wins"}
_BEARISH = {"miss", "plunge", "downgrade", "lawsuit", "probe", "cut", "warns", "falls"}
_FINNHUB_SENTIMENT = "https://finnhub.io/api/v1/news-sentiment"


class NewsProvider(SignalProvider):
    name = "news"

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def fetch(self, symbols: list[str]) -> list[Signal]:
        from alpaca.data.historical.news import NewsClient
        from alpaca.data.requests import NewsRequest

        client = NewsClient(self.cfg.alpaca_api_key, self.cfg.alpaca_secret_key)
        signals: list[Signal] = []
        for symbol in symbols:
            req = NewsRequest(symbols=symbol, limit=10)
            articles = client.get_news(req).data.get("news", [])
            if not articles:
                continue
            headlines = [a.headline for a in articles]

            score, src = self._finnhub_sentiment(symbol)
            if score is None:
                score, src = self._keyword_sentiment(headlines), "keyword"

            signals.append(Signal(
                kind=SignalKind.NEWS,
                symbol=symbol,
                summary=f"{len(headlines)} recent headlines ({src} sentiment "
                        f"{score:+.2f}); latest: \"{headlines[0][:80]}\"",
                score=score,
                source=f"alpaca-news+{src}",
                data={"headlines": headlines[:5]},
            ))
        return signals

    def _finnhub_sentiment(self, symbol: str) -> tuple[float | None, str]:
        if not self.cfg.finnhub_api_key:
            return None, ""
        try:
            r = requests.get(_FINNHUB_SENTIMENT, params={
                "symbol": symbol, "token": self.cfg.finnhub_api_key,
            }, timeout=15)
            if r.status_code != 200:
                return None, ""
            data = r.json()
            # companyNewsScore is 0..1; bullishPercent 0..1. Map to [-1, 1].
            bull = data.get("sentiment", {}).get("bullishPercent")
            if bull is None:
                return None, ""
            return round(bull * 2 - 1, 3), "finnhub"
        except Exception as e:
            log.debug("finnhub sentiment failed for %s: %s", symbol, e)
            return None, ""

    @staticmethod
    def _keyword_sentiment(headlines: list[str]) -> float:
        text = " ".join(headlines).lower()
        pos = sum(w in text for w in _BULLISH)
        neg = sum(w in text for w in _BEARISH)
        if pos + neg == 0:
            return 0.0
        return round((pos - neg) / (pos + neg), 3)
