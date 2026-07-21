"""Next-earnings-date lookup for the earnings-blackout guard.

Gap risk around an earnings report routinely dwarfs a normal stop — the price can
open well through it, so a tight stop offers little protection. This helper tells
the risk layer how many days until a symbol's next scheduled report so it can block
(or shrink) NEW buys inside a blackout window. It is advisory data feeding a
DETERMINISTIC gate in risk.py — the gate itself never depends on the network.

Sources, in order (C.5):
  1. Robinhood MCP get_earnings_calendar — ONE market-wide call per cycle
     (31-day forward window) covering every slate symbol at once. A symbol
     absent from a SUCCESSFUL read has no report inside the window, which for
     a <=31-day blackout means "no blackout" with no further lookup needed.
  2. yfinance per-symbol (free, no key) — the fallback when Robinhood is off
     or the calendar call fails.
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

# The RH calendar accepts windows up to 31 days — wider than any sane
# EARNINGS_BLACKOUT_DAYS, so one call covers the guard's whole horizon.
_RH_WINDOW_DAYS = 31


class EarningsCalendar:
    def __init__(self, reader=None) -> None:
        # symbol -> days-until-next-earnings (or None if unknown). Cleared each cycle.
        self._cache: dict[str, int | None] = {}
        self._lock = threading.Lock()
        # Optional RobinhoodReader (duck-typed: .enabled + .call_json). The
        # market-wide calendar is fetched at most once per cycle and shared.
        self._reader = reader
        self._rh_map: dict[str, int] | None = None   # None = not fetched/failed

    def new_cycle(self) -> None:
        """Drop the per-cycle cache so the next lookup re-fetches. Call once at the
        start of each decision cycle, before the risk gate runs."""
        with self._lock:
            self._cache.clear()
            self._rh_map = None

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

    # -- sources ------------------------------------------------------------ #
    def _lookup(self, symbol: str, today: date) -> int | None:
        rh = self._rh_calendar(today)
        if rh:
            # Successful market-wide read: absence from the 31-day window IS
            # the answer (no upcoming report -> no blackout). No fallback call.
            # An EMPTY market-wide window is treated as a failed read, not an
            # answer — the whole market never has zero reports in 31 days.
            return rh.get(symbol)
        return self._yf_lookup(symbol, today)

    def _rh_calendar(self, today: date) -> dict[str, int] | None:
        """symbol -> days-until-report from one Robinhood MCP calendar call.
        None when the reader is off/unavailable (caller falls back to yfinance).
        Fetched once per cycle; parsed defensively (field names tolerated:
        symbol/ticker, report_date/date/earnings_date)."""
        if self._reader is None or not getattr(self._reader, "enabled", False):
            return None
        with self._lock:
            if self._rh_map is not None:
                return self._rh_map
        raw = self._reader.call_json(
            "get_earnings_calendar", {"days": _RH_WINDOW_DAYS},
        )
        if raw is None:
            return None
        rows = raw if isinstance(raw, list) else None
        if rows is None and isinstance(raw, dict):
            # Payload may nest the list under any key — take the first list.
            rows = next((v for v in raw.values() if isinstance(v, list)), None)
        if rows is None:
            return None
        cal: dict[str, int] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            sym = str(row.get("symbol") or row.get("ticker") or "").upper()
            when = (
                row.get("report_date") or row.get("date")
                or row.get("earnings_date")
            )
            if when is None and isinstance(row.get("report"), dict):
                # Live payload shape: {"symbol": "T", "report": {"date": ...}}.
                when = row["report"].get("date")
            days = self._to_days(when, today)
            if not sym or days is None or days < 0:
                continue
            # Keep the NEAREST future report if a symbol appears twice.
            if sym not in cal or days < cal[sym]:
                cal[sym] = days
        if cal:
            log.info(
                "Robinhood earnings calendar: %d name(s) reporting within %dd.",
                len(cal), _RH_WINDOW_DAYS,
            )
        else:
            log.warning(
                "Robinhood earnings calendar parsed EMPTY from %d row(s) — "
                "treating as a failed read; falling back to per-symbol lookups.",
                len(rows),
            )
        with self._lock:
            self._rh_map = cal
        return cal

    def _yf_lookup(self, symbol: str, today: date) -> int | None:
        try:
            import yfinance as yf  # lazy import so the dep stays optional
            from .symbols import yahoo_symbol
            cal = yf.Ticker(yahoo_symbol(symbol)).calendar or {}
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
