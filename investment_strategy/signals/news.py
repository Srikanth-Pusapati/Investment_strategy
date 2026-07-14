"""News & sentiment signals.

Headlines come from Alpaca's news API (Benzinga-backed) — same credentials you
already have. Sentiment is scored in priority order:
  - If FINNHUB_API_KEY is set, use Finnhub's news-sentiment score (model-based,
    aggregated across many sources) — the preferred path.
  - Else, if the vaderSentiment package is installed, score the headlines with
    VADER (free, local, lexicon-based — no API key, no network).
  - Else, fall back to a lightweight keyword pass on the headlines.
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

# Lazy VADER singleton: None = not tried yet, False = unavailable, else analyzer.
_VADER: object | None = None


def _vader_analyzer():
    global _VADER
    if _VADER is None:
        try:
            from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
            _VADER = SentimentIntensityAnalyzer()
        except Exception:
            _VADER = False
    return _VADER or None


class NewsProvider(SignalProvider):
    name = "news"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        # Set once we see a 403: the key's plan doesn't include news-sentiment
        # (plan gating is stable, so stop paying per-symbol latency for it).
        self._finnhub_gated = False

    def fetch(self, symbols: list[str]) -> list[Signal]:
        from alpaca.data.historical.news import NewsClient
        from alpaca.data.requests import NewsRequest

        from ..execution.alpaca_client import bound_client

        # Finite read timeout: this exact call wedged 2026-07-14 with
        # "Read timed out. (read timeout=None)" and stalled the cycle.
        client = bound_client(
            NewsClient(self.cfg.alpaca_api_key, self.cfg.alpaca_secret_key)
        )
        signals: list[Signal] = []
        for symbol in symbols:
            req = NewsRequest(symbols=symbol, limit=10)
            articles = client.get_news(req).data.get("news", [])
            if not articles:
                continue
            headlines = [a.headline for a in articles]

            score, src = self._finnhub_sentiment(symbol)
            if score is None:
                vscore = self._vader_sentiment(headlines)
                if vscore is not None:
                    score, src = vscore, "vader"
                else:
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
        if not self.cfg.finnhub_api_key or self._finnhub_gated:
            return None, ""
        try:
            r = requests.get(_FINNHUB_SENTIMENT, params={
                "symbol": symbol, "token": self.cfg.finnhub_api_key,
            }, timeout=15)
            if r.status_code == 403:
                # Premium-gated endpoint on this key — say so ONCE instead of
                # silently degrading (the silent-403 lesson from options flow).
                self._finnhub_gated = True
                log.warning(
                    "Finnhub news-sentiment is premium-gated on this key "
                    "(HTTP 403) — using the VADER fallback for this run.")
                return None, ""
            if r.status_code != 200:
                log.debug("finnhub sentiment HTTP %d for %s", r.status_code, symbol)
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
    def _vader_sentiment(headlines: list[str]) -> float | None:
        """Mean VADER compound score over the headlines, already in [-1, 1].
        Returns None if vaderSentiment isn't installed (caller falls back)."""
        analyzer = _vader_analyzer()
        if analyzer is None or not headlines:
            return None
        scores = [analyzer.polarity_scores(h)["compound"] for h in headlines]
        return round(sum(scores) / len(scores), 3)

    @staticmethod
    def _keyword_sentiment(headlines: list[str]) -> float:
        text = " ".join(headlines).lower()
        pos = sum(w in text for w in _BULLISH)
        neg = sum(w in text for w in _BEARISH)
        if pos + neg == 0:
            return 0.0
        return round((pos - neg) / (pos + neg), 3)
