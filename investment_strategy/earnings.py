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

Source visibility (run-7, A7): the fallback used to be SILENT. Sep 10 2026 the
RH OAuth token died at 08:34 and for 12 decision cycles (to Sep 11 11:09) every
blackout lookup ran on per-symbol yfinance with no log line, while the FEEDS
line read '3/3 healthy' (RH is not a SCREENER_SOURCE, so the n/n cannot see
it). Now `source()` reports which source the gate's lookups use this cycle —
'rh' | 'yfinance-fallback' | 'none' — for the orchestrator's FEEDS token, and
the first fallback lookup of a cycle logs ONE greppable 'Earnings calendar:'
line (WARNING when RH was expected but is dead/failed, INFO when RH is simply
not configured). The values returned to the risk gate are unchanged.
"""
from __future__ import annotations

import importlib.util
import logging
import threading
from datetime import date, datetime

log = logging.getLogger("earnings")

# The RH calendar accepts windows up to 31 days — wider than any sane
# EARNINGS_BLACKOUT_DAYS, so one call covers the guard's whole horizon.
_RH_WINDOW_DAYS = 31

# Robinhood calendar status for the cycle. 'pending' = not asked yet this
# cycle (the FEEDS line is logged BEFORE the first lookup; the reader's
# enabled flag is the only evidence at that point). The *_FAILED set is an
# OUTAGE — RH was expected and did not answer — and is what promotes the
# once-per-cycle fallback line to WARNING.
_RH_OK = "ok"
_RH_PENDING = "pending"
_RH_OFF = "off"                  # no reader wired at all
_RH_DISABLED = "disabled"        # reader off by config / no credentials
_RH_OAUTH_DEAD = "oauth-dead"    # RobinhoodReader dead-auth latch
_RH_CALL_FAILED = "call-failed"  # MCP call returned nothing
_RH_UNPARSEABLE = "unparseable"  # payload had no row list
_RH_EMPTY = "parsed-empty"       # rows present, none parsed to a date
_RH_FAILED = frozenset({_RH_OAUTH_DEAD, _RH_CALL_FAILED, _RH_UNPARSEABLE, _RH_EMPTY})
_RH_STATUS_TEXT = {
    _RH_OFF: "Robinhood calendar not wired",
    _RH_DISABLED: "Robinhood context disabled (config/no credentials)",
    _RH_OAUTH_DEAD: "Robinhood OAuth dead (context reads latched off)",
    _RH_CALL_FAILED: "Robinhood calendar call failed",
    _RH_UNPARSEABLE: "Robinhood calendar payload unparseable",
    _RH_EMPTY: "Robinhood calendar parsed empty",
}

SOURCE_RH = "rh"
SOURCE_YF_FALLBACK = "yfinance-fallback"
SOURCE_NONE = "none"


class EarningsCalendar:
    def __init__(self, reader=None) -> None:
        # symbol -> days-until-next-earnings (or None if unknown). Cleared each cycle.
        self._cache: dict[str, int | None] = {}
        self._lock = threading.Lock()
        # Optional RobinhoodReader (duck-typed: .enabled + .call_json). The
        # market-wide calendar is fetched at most once per cycle and shared.
        self._reader = reader
        self._rh_map: dict[str, int] | None = None   # None = not fetched/failed
        # Outcome of the most recent RH calendar attempt. NOT reset by
        # new_cycle(): the FEEDS line is logged before this cycle's fetch, so
        # "the last read failed" is the best evidence it has — the token stays
        # 'yfinance-fallback' from the cycle after the first failure until a
        # read succeeds (mirrors the news=vader-fallback latch).
        self._rh_status: str = _RH_PENDING
        # Once-per-cycle guard for the fallback log line (≈30 lookups/cycle
        # would otherwise spam it). Reset by new_cycle().
        self._fallback_logged = False

    def new_cycle(self) -> None:
        """Drop the per-cycle cache so the next lookup re-fetches. Call once at the
        start of each decision cycle, before the risk gate runs."""
        with self._lock:
            self._cache.clear()
            self._rh_map = None
            self._fallback_logged = False

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

    # -- source visibility (FEEDS line) ------------------------------------- #
    def source(self) -> str:
        """Which source the blackout gate's lookups use this cycle:
        'rh' (Robinhood market-wide calendar), 'yfinance-fallback' (RH off,
        dead-latched, or its last calendar read failed) or 'none' (RH
        unavailable AND yfinance not importable — every lookup returns None
        and the gate fails open on every name). No network, no lock on the
        reader: reads the reader's `enabled` flag and the last read outcome."""
        if self._rh_available():
            return SOURCE_RH
        return SOURCE_YF_FALLBACK if self._yf_importable() else SOURCE_NONE

    def rh_status(self) -> str:
        """Outcome of the most recent RH calendar attempt (see _RH_* above);
        'pending' until the first attempt. For the fallback log + tests."""
        return self._rh_status

    def _rh_available(self) -> bool:
        if self._reader is None or not getattr(self._reader, "enabled", False):
            return False
        if self._rh_status == _RH_OAUTH_DEAD and not self._reader_auth_dead():
            # The reader's dead-auth latch self-healed (a re-login or its own
            # retry) and only a LOOKUP re-evaluates _rh_status — cycles with no
            # buy candidates never look up, so without this the FEEDS token
            # read 'earnings=yfinance-fallback' for days on a healthy RH.
            # Back to 'pending': the next lookup decides for real.
            self._rh_status = _RH_PENDING
        return self._rh_status not in _RH_FAILED

    @staticmethod
    def _yf_importable() -> bool:
        try:
            return importlib.util.find_spec("yfinance") is not None
        except Exception:  # noqa: BLE001 — broken meta path; treat as absent
            return False

    def _reader_auth_dead(self) -> bool:
        """True when the wired reader reports the dead-auth latch. Duck-typed:
        RobinhoodReader.auth_dead is a classmethod; a plain attribute or a
        reader without it (test doubles) is tolerated."""
        fn = getattr(self._reader, "auth_dead", None)
        try:
            return bool(fn()) if callable(fn) else bool(fn)
        except Exception:  # noqa: BLE001
            return False

    def _log_fallback_once(self) -> None:
        """ONE 'Earnings calendar:' line per cycle on the first lookup that
        falls back to yfinance. WARNING when RH was expected and did not
        answer (an outage the operator should see) or when no source can
        answer at all; INFO when RH is simply not wired/configured (yfinance
        is then the by-design primary, not a degradation)."""
        with self._lock:
            if self._fallback_logged:
                return
            self._fallback_logged = True
        status = self._rh_status
        src = self.source()
        emit = log.warning if (status in _RH_FAILED or src == SOURCE_NONE) else log.info
        emit(
            "Earnings calendar: yfinance per-symbol fallback this cycle — %s "
            "(FEEDS earnings=%s).",
            _RH_STATUS_TEXT.get(status, status), src,
        )

    # -- sources ------------------------------------------------------------ #
    def _lookup(self, symbol: str, today: date) -> int | None:
        rh = self._rh_calendar(today)
        if rh:
            # Successful market-wide read: absence from the 31-day window IS
            # the answer (no upcoming report -> no blackout). No fallback call.
            # An EMPTY market-wide window is treated as a failed read, not an
            # answer — the whole market never has zero reports in 31 days.
            return rh.get(symbol)
        self._log_fallback_once()
        return self._yf_lookup(symbol, today)

    def _rh_calendar(self, today: date) -> dict[str, int] | None:
        """symbol -> days-until-report from one Robinhood MCP calendar call.
        None when the reader is off/unavailable (caller falls back to yfinance).
        Fetched once per cycle; parsed defensively (field names tolerated:
        symbol/ticker, report_date/date/earnings_date). Records the attempt's
        outcome in _rh_status for source()/the fallback line."""
        if self._reader is None:
            self._rh_status = _RH_OFF
            return None
        if not getattr(self._reader, "enabled", False):
            self._rh_status = (
                _RH_OAUTH_DEAD if self._reader_auth_dead() else _RH_DISABLED
            )
            return None
        with self._lock:
            if self._rh_map is not None:
                return self._rh_map
        raw = self._reader.call_json(
            "get_earnings_calendar", {"days": _RH_WINDOW_DAYS},
        )
        if raw is None:
            self._rh_status = _RH_CALL_FAILED
            return None
        rows = raw if isinstance(raw, list) else None
        if rows is None and isinstance(raw, dict):
            # Payload may nest the list under any key — take the first list.
            rows = next((v for v in raw.values() if isinstance(v, list)), None)
        if rows is None:
            self._rh_status = _RH_UNPARSEABLE
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
            self._rh_status = _RH_OK
            log.info(
                "Robinhood earnings calendar: %d name(s) reporting within %dd.",
                len(cal), _RH_WINDOW_DAYS,
            )
        else:
            self._rh_status = _RH_EMPTY
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
