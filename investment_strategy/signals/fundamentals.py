"""Fundamentals signals — EBITDA, margins, leverage.

Uses yfinance (free, no API key) as the default source so the system is runnable
out of the box. Swap in Financial Modeling Prep / Finnhub for higher quality by
setting FMP_API_KEY / FINNHUB_API_KEY and extending fetch().
"""
from __future__ import annotations

from ..config import Config
from ..models import Signal, SignalKind
from ..symbols import yahoo_symbol
from .base import SignalProvider


class FundamentalsProvider(SignalProvider):
    name = "fundamentals"

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def fetch(self, symbols: list[str]) -> list[Signal]:
        import yfinance as yf  # imported lazily so the dep is optional

        signals: list[Signal] = []
        for symbol in symbols:
            info = yf.Ticker(yahoo_symbol(symbol)).info or {}
            ebitda = info.get("ebitda")
            margins = info.get("ebitdaMargins")          # 0..1
            debt_to_equity = info.get("debtToEquity")    # percent
            rev_growth = info.get("revenueGrowth")       # 0..1

            if ebitda is None and margins is None:
                continue  # nothing useful for this name

            score = self._score(margins, rev_growth, debt_to_equity)
            summary = (
                f"EBITDA ${ebitda/1e9:.1f}B" if ebitda else "EBITDA n/a"
            ) + (
                f", margin {margins*100:.1f}%" if margins is not None else ""
            ) + (
                f", rev growth {rev_growth*100:+.1f}%" if rev_growth is not None else ""
            ) + (
                f", D/E {debt_to_equity:.0f}" if debt_to_equity is not None else ""
            )

            signals.append(Signal(
                kind=SignalKind.FUNDAMENTALS,
                symbol=symbol,
                summary=summary,
                score=score,
                source="yfinance",
                data={
                    "ebitda": ebitda, "ebitda_margin": margins,
                    "debt_to_equity": debt_to_equity, "revenue_growth": rev_growth,
                },
            ))
        return signals

    @staticmethod
    def _score(margin, growth, debt_to_equity) -> float:
        """Crude composite in [-1, 1]: healthy margins + growth bullish,
        heavy leverage bearish. Tune or replace with a real factor model."""
        s = 0.0
        if margin is not None:
            s += max(-0.3, min(0.4, (margin - 0.10) * 2))   # >10% margin is good
        if growth is not None:
            s += max(-0.3, min(0.4, growth))                 # positive growth good
        if debt_to_equity is not None:
            s -= max(0.0, min(0.3, (debt_to_equity - 150) / 500))  # high D/E bad
        return round(max(-1.0, min(1.0, s)), 3)
