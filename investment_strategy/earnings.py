"""Next-earnings-date lookup for the earnings-blackout guard.

Gap risk around an earnings report routinely dwarfs a normal stop — the price can
open well through it, so a tight stop offers little protection. This helper tells
the risk layer how many days until a symbol's next scheduled report so it can block
(or shrink) NEW buys inside a blackout window. It is advisory data feeding a
DETERMINISTIC gate in risk.py — the gate itself never depends on the network.

Source: yfinance (free, no key), the same source the fundamentals provider uses.
Earnings dates are estimates and move, so this is a fail-OPEN guard: if the date
is unknown we return None and the risk layer does NOT blackout (we never block a
trade on missing data — we only act on a date we actually have).

Per-cycle cached: new_cycle() drops the cache at the top of each decision cycle so
each symbol is looked up at most once per cycle, mirroring QuiverClient.
"""
from __future__ import annotations

import logging
import threading
from datetime import date, datetime

log = logging.getLogger("earnings")


class EarningsCalendar:
    def __init__(self) -> None:
        # symbol -> days-until-next-earnings (or None if unknown). Cleared each cycle.
        self._cache: dict[str, int | None] = {}
        self._lock = threading.Lock()

    def new_cycle(self) -> None:
        """Drop the per-cycle cache so the next lookup re-fetches. Call once at the
        start of each decision cycle, before the risk gate runs."""
        with self._lock:
            self._cache.clear()

    def days_until_earnings(self, symbol: str, today: date | None = None) -> int | None:
        """Calendar days until the next scheduled earnings report, or None if no
        FUTURE date is known. Negative/past dates are treated as unknown (the last
        report, not the next one). Cached for the cycle; never raises."""
        sym = symbol.upper()
        with self._lock:
            if sym in self._cache:
                return self._cache[sym]
        days = self._lookup(sym, today or date.today())
        with self._lock:
            self._cache[sym] = days
        return days

    # -- source ------------------------------------------------------------- #
    def _lookup(self, symbol: str, today: date) -> int | None:
        try:
            import yfinance as yf  # lazy import so the dep stays optional
            cal = yf.Ticker(symbol).calendar or {}
        except Exception as e:  # network/parse/missing — fail OPEN (no blackout)
            log.debug("earnings lookup for %s failed: %s", symbol, e)
            return None

        # yfinance returns {"Earnings Date": [date, ...], ...}; dates may be
        # datetime.date, datetime, or ISO strings depending on version.
        raw = cal.get("Earnings Date") if isinstance(cal, dict) else None
        candidates = raw if isinstance(raw, (list, tuple)) else [raw]

        future_days = [
            d for d in (self._to_days(c, today) for c in candidates)
            if d is not None and d >= 0
        ]
        return min(future_days) if future_days else None

    @staticmethod
    def _to_days(value, today: date) -> int | None:
        if value is None:
            return None
        d: date | None = None
        if isinstance(value, datetime):
            d = value.date()
        elif isinstance(value, date):
            d = value
        else:
            try:
                d = datetime.fromisoformat(str(value)[:10]).date()
            except (ValueError, TypeError):
                return None
        return (d - today).days
