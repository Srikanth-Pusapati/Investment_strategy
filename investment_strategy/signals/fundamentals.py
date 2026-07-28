"""Fundamentals signals — EBITDA margins, revenue growth, leverage.

Two backends behind one scorer, picked by key availability:

- FMP (preferred; needs FMP_API_KEY, wired 2026-07-27): the /stable API's
  ratios-ttm (ebitdaMarginTTM, debtToEquityRatioTTM) + financial-growth
  (revenueGrowth). TTM ratios beat yfinance's snapshot fields, and responses
  are cached in-provider for 24h — fundamentals move quarterly, so re-pulling
  every hourly cycle would only burn the request quota. NOTE: /api/v3 paths are
  legacy-gated for keys created after Aug 2025 and return 403 — only /stable
  works; FMP's debtToEquity is a plain ratio (1.8) where yfinance reports
  percent (180), so the FMP read is scaled x100 before the shared scorer.
- yfinance fallback (free, no key): keeps the system runnable out of the box
  and catches per-symbol FMP misses (unknown tickers, quota, outages).
"""
from __future__ import annotations

import time
from typing import Any

import requests

from ..config import Config
from ..models import Signal, SignalKind
from ..symbols import yahoo_symbol
from .base import SignalProvider

_FMP_BASE = "https://financialmodelingprep.com/stable"
_FMP_CACHE_TTL_S = 24 * 3600.0


class FundamentalsProvider(SignalProvider):
    name = "fundamentals"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._get = requests.get   # injectable for tests
        # symbol -> (fetched_at_monotonic-ish epoch, fields dict or None)
        self._fmp_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}

    @property
    def _fmp_key(self) -> str:
        return getattr(self.cfg, "fmp_api_key", "") or ""

    def fetch(self, symbols: list[str]) -> list[Signal]:
        signals: list[Signal] = []
        for symbol in symbols:
            fields = self._fmp_fields(symbol) if self._fmp_key else None
            source = "fmp"
            if fields is None:
                fields = self._yf_fields(symbol)
                source = "yfinance"
            if fields is None:
                continue  # nothing useful for this name from either source

            ebitda = fields.get("ebitda")
            margins = fields.get("ebitda_margin")        # 0..1
            debt_to_equity = fields.get("debt_to_equity")  # percent
            rev_growth = fields.get("revenue_growth")    # 0..1

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
                source=source,
                data={
                    "ebitda": ebitda, "ebitda_margin": margins,
                    "debt_to_equity": debt_to_equity, "revenue_growth": rev_growth,
                },
            ))
        return signals

    # -- FMP backend -------------------------------------------------------- #
    def _fmp_fields(self, symbol: str) -> dict[str, Any] | None:
        """Normalized fields from FMP /stable, cached 24h (both hits and
        misses — a name FMP doesn't know today won't appear mid-day)."""
        now = time.time()
        hit = self._fmp_cache.get(symbol)
        if hit is not None and now - hit[0] < _FMP_CACHE_TTL_S:
            return hit[1]

        ratios = self._fmp_row("ratios-ttm", symbol)
        fields: dict[str, Any] | None = None
        if ratios:
            margin = self._f(ratios.get("ebitdaMarginTTM"))
            de = self._f(ratios.get("debtToEquityRatioTTM"))
            growth_row = self._fmp_row("financial-growth", symbol, limit=1)
            growth = self._f((growth_row or {}).get("revenueGrowth"))
            if margin is not None or growth is not None:
                fields = {
                    "ebitda": None,   # dollar EBITDA not carried by these endpoints
                    "ebitda_margin": margin,
                    # FMP reports a plain ratio; the scorer expects percent.
                    "debt_to_equity": de * 100.0 if de is not None else None,
                    "revenue_growth": growth,
                }
        self._fmp_cache[symbol] = (now, fields)
        return fields

    def _fmp_row(self, endpoint: str, symbol: str, **params) -> dict[str, Any] | None:
        try:
            r = self._get(
                f"{_FMP_BASE}/{endpoint}",
                params={"symbol": symbol, "apikey": self._fmp_key, **params},
                timeout=15,
            )
            if r.status_code != 200:
                return None
            body = r.json()
            return body[0] if isinstance(body, list) and body else None
        except Exception:
            return None

    @staticmethod
    def _f(v) -> float | None:
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    # -- yfinance fallback --------------------------------------------------- #
    @staticmethod
    def _yf_fields(symbol: str) -> dict[str, Any] | None:
        import yfinance as yf  # imported lazily so the dep is optional

        info = yf.Ticker(yahoo_symbol(symbol)).info or {}
        ebitda = info.get("ebitda")
        margins = info.get("ebitdaMargins")          # 0..1
        if ebitda is None and margins is None:
            return None
        return {
            "ebitda": ebitda,
            "ebitda_margin": margins,
            "debt_to_equity": info.get("debtToEquity"),   # already percent
            "revenue_growth": info.get("revenueGrowth"),  # 0..1
        }

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
