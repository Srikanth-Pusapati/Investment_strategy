"""Per-symbol sector lookup for the sector-concentration cap.

Discovery is now the only funnel, and a scanner can easily surface several names
from the same hot sector (all big-tech, all semis) — which look diversified but
are really one bet. The risk layer caps total exposure per sector; this helper
tells it which sector a symbol is in, and the orchestrator sums current holdings
per sector to feed that cap.

It is advisory data feeding a DETERMINISTIC gate in risk.py — the gate never
depends on the network. Source: yfinance (free, no key), the same source the
fundamentals provider uses. Fails OPEN: an unknown sector returns None and the
risk layer simply skips the sector cap for that name (we never block a trade on
missing data). Per-cycle cached: new_cycle() drops the cache so each symbol is
looked up at most once per cycle, mirroring QuiverClient / EarningsCalendar.
"""
from __future__ import annotations

import logging
import threading

log = logging.getLogger("sectors")


class SectorMap:
    def __init__(self) -> None:
        self._cache: dict[str, str | None] = {}
        self._lock = threading.Lock()

    def new_cycle(self) -> None:
        """Drop the per-cycle cache so the next lookup re-fetches. Call once at the
        start of each decision cycle."""
        with self._lock:
            self._cache.clear()

    def sector_for(self, symbol: str) -> str | None:
        """The symbol's sector (e.g. 'Technology'), or None if unknown. Cached for
        the cycle; never raises."""
        sym = symbol.upper()
        with self._lock:
            if sym in self._cache:
                return self._cache[sym]
        sector = self._lookup(sym)
        with self._lock:
            self._cache[sym] = sector
        return sector

    def exposure_by_sector(self, holdings: dict[str, float]) -> dict[str, float]:
        """Sum {symbol: market_value} into {sector: total_$}. Symbols with an
        unknown sector are dropped (can't be attributed)."""
        out: dict[str, float] = {}
        for sym, mv in holdings.items():
            sec = self.sector_for(sym)
            if sec:
                out[sec] = out.get(sec, 0.0) + mv
        return out

    # -- source ------------------------------------------------------------- #
    def _lookup(self, symbol: str) -> str | None:
        try:
            import yfinance as yf  # lazy import so the dep stays optional
            info = yf.Ticker(symbol).info or {}
            sec = info.get("sector")
            return str(sec) if sec else None
        except Exception as e:  # network/parse/missing — fail OPEN (no cap)
            log.debug("sector lookup for %s failed: %s", symbol, e)
            return None
