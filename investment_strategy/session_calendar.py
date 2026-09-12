"""Network-free exchange session calendar for the paging window and dead-man.

WHY: the watchdog's paging window (Orchestrator._overlaps_paging_hours) and
ops/deadman.py's market_hours were weekday 09:25-16:05 ET clock math with no
holiday or early-close knowledge. On Labor Day 2026-09-07 the bot logged
"Market closed; skipping decision cycle." every hour, yet a DNS outage that
afternoon produced 75 CRITICAL "Watchdog BLIND ... positions unwatched during
market hours" pages (0 delivered, nothing at risk) and the dead-man ran a
full day of in-hours checks. broker.is_trading_day exists but is a live
get_calendar call — unusable on the watchdog thread (no network there by
design, least of all during the outage that triggers the page).

DESIGN: the DECISION loop pulls the exchange calendar for today +/-
REFRESH_SPAN_DAYS at most once per ET date (Orchestrator._refresh_session_
calendar), and persists it to a small JSON under the state dir so a restart
mid-outage keeps it. Everything in this module is pure python over that cache
(stdlib only, so ops/deadman.py can import it standalone): is_session(date),
paging_window(date) -> (open-5min, close+5min) honoring early closes, and
paging_overlap(start_ts, end_ts, calendar) — the function the watchdog
thread actually calls. A date the cache does not cover (missing file, stale
range, first run) falls back to the old weekday 09:25-16:05 ET math and logs
ONE greppable WARNING per ET date ("Session calendar fallback"), so a
populated-but-wrong cache can never silently mute a real session for long
and an empty cache costs nothing but the old behaviour.

Cache shape (state/session_calendar.json):
  {"fetched": "2026-09-11T12:03:55-04:00", "fetched_day": "2026-09-11",
   "start": "2026-09-01", "end": "2026-09-21",
   "sessions": {"2026-09-08": {"open": "09:30", "close": "16:00"}, ...}}
Holidays are simply ABSENT from `sessions` while inside [start, end].
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger("session_calendar")

ET = ZoneInfo("America/New_York")
#: default cache path (relative to the bot's cwd = repo root, like STATE_FILE).
DEFAULT_PATH = "state/session_calendar.json"
#: the decision loop fetches [today - span, today + span].
REFRESH_SPAN_DAYS = 10
#: a fetched range with fewer sessions than this is rejected (a truncated or
#: garbage response must not replace a good cache): 21 calendar days hold
#: >= 13 sessions even across a holiday week.
MIN_SESSIONS = 5
#: the paging window pads the session on both sides so a bot that died
#: overnight pages BEFORE the open and a gap ending just past the bell still
#: counts (same 5 minutes the weekday fallback always used: 09:25 / 16:05).
PAGING_PAD = timedelta(minutes=5)
FALLBACK_OPEN = time(9, 30)
FALLBACK_CLOSE = time(16, 0)


def _parse_hhmm(s: str) -> time | None:
    try:
        hh, mm = str(s).split(":")[:2]
        return time(int(hh), int(mm))
    except (ValueError, AttributeError):
        return None


class SessionCalendar:
    """Cached exchange sessions with a weekday fallback. Thread-safe for the
    one writer (decision loop: update/load) + many readers (watchdog thread,
    deadman) pattern: readers copy the dict reference under the lock."""

    def __init__(self, path: str | os.PathLike | None = DEFAULT_PATH):
        self.path: Path | None = Path(path) if path else None
        self._lock = threading.Lock()
        self._sessions: dict[str, tuple[time, time]] = {}
        self.start: date | None = None
        self.end: date | None = None
        self.fetched: str = ""
        self.fetched_day: str = ""
        # Dates already warned about (one line per date per process;
        # cleared by a successful load/update so a later lapse warns again).
        self._fallback_warned: set[str] = set()
        self.load()

    # -- persistence ---------------------------------------------------------- #
    def load(self) -> bool:
        """Read the cache file; False (and an empty cache) when it is missing
        or unreadable. Never raises — a corrupt cache is the fallback case."""
        if self.path is None:
            return False
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            sessions: dict[str, tuple[time, time]] = {}
            for day, times in (raw.get("sessions") or {}).items():
                o = _parse_hhmm((times or {}).get("open", ""))
                c = _parse_hhmm((times or {}).get("close", ""))
                if o is None or c is None:
                    continue
                sessions[str(day)] = (o, c)
            start = date.fromisoformat(str(raw.get("start")))
            end = date.fromisoformat(str(raw.get("end")))
            if not sessions or end < start:
                return False
            with self._lock:
                self._sessions = sessions
                self.start, self.end = start, end
                self.fetched = str(raw.get("fetched", ""))
                self.fetched_day = str(raw.get("fetched_day", ""))
                self._fallback_warned.clear()
            return True
        except FileNotFoundError:
            return False
        except Exception as e:  # noqa: BLE001 — corrupt cache = fallback, not a crash
            log.warning("Session calendar cache %s unreadable (%s); using weekday "
                        "fallback until the next refresh.", self.path, e)
            return False

    def save(self) -> bool:
        """tmp + os.replace so a crash mid-write can't leave a torn file."""
        if self.path is None:
            return False
        try:
            with self._lock:
                payload = {
                    "fetched": self.fetched,
                    "fetched_day": self.fetched_day,
                    "start": self.start.isoformat() if self.start else "",
                    "end": self.end.isoformat() if self.end else "",
                    "sessions": {
                        d: {"open": o.strftime("%H:%M"), "close": c.strftime("%H:%M")}
                        for d, (o, c) in sorted(self._sessions.items())
                    },
                }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
            return True
        except Exception as e:  # noqa: BLE001 — best-effort persistence
            log.warning("Session calendar cache %s not written: %s", self.path, e)
            return False

    # -- refresh (decision loop only) ----------------------------------------- #
    def needs_refresh(self, today: date) -> bool:
        """True until a fetch has SUCCEEDED for `today` (ET date). A failed
        fetch leaves this True so the next decision cycle tries again; the
        watchdog never calls this."""
        return self.fetched_day != today.isoformat()

    def update(self, sessions: dict[str, tuple[str, str]], start: date, end: date,
               today: date, now: datetime | None = None) -> bool:
        """Replace the cache with a freshly fetched range. Rejects (keeps the
        old cache, returns False) a response with < MIN_SESSIONS sessions or
        unparsable times — fail OPEN to the previous cache / weekday math."""
        parsed: dict[str, tuple[time, time]] = {}
        for day, times in (sessions or {}).items():
            try:
                o = _parse_hhmm(times[0])
                c = _parse_hhmm(times[1])
            except (TypeError, IndexError):
                o = c = None
            if o is None or c is None:
                continue
            parsed[str(day)] = (o, c)
        if len(parsed) < MIN_SESSIONS or end < start:
            return False
        stamp = (now or datetime.now(ET)).isoformat(timespec="seconds")
        with self._lock:
            self._sessions = parsed
            self.start, self.end = start, end
            self.fetched = stamp
            self.fetched_day = today.isoformat()
            self._fallback_warned.clear()
        self.save()
        return True

    # -- pure lookups (safe on any thread) ------------------------------------ #
    def covers(self, day: date) -> bool:
        """True when `day` lies inside the cached [start, end] range — the
        cache then has authority (absent date == holiday/weekend)."""
        with self._lock:
            return bool(self._sessions) and self.start is not None \
                and self.end is not None and self.start <= day <= self.end

    def session_times(self, day: date) -> tuple[time, time] | None:
        """(open, close) wall-clock ET for a covered session day; None for a
        covered non-session day (holiday/weekend). For an UNCOVERED day the
        weekday fallback applies (09:30-16:00 Mon-Fri) with one WARNING per
        ET date."""
        with self._lock:
            if self._sessions and self.start is not None and self.end is not None \
                    and self.start <= day <= self.end:
                return self._sessions.get(day.isoformat())
            cached = f"{self.start}..{self.end}" if self.start else "empty"
        self._warn_fallback(day, cached)
        if day.weekday() >= 5:
            return None
        return (FALLBACK_OPEN, FALLBACK_CLOSE)

    def _warn_fallback(self, day: date, cached: str) -> None:
        key = day.isoformat()
        if key in self._fallback_warned:
            return
        if len(self._fallback_warned) > 64:      # bounded: a very long lapse
            self._fallback_warned.clear()
        self._fallback_warned.add(key)
        log.warning(
            "Session calendar fallback: %s is outside the cached range (%s, file %s) "
            "— using weekday 09:25-16:05 ET paging math until the decision loop "
            "refreshes the calendar.", key, cached, self.path,
        )

    def is_session(self, day: date) -> bool:
        return self.session_times(day) is not None

    def paging_window(self, day: date) -> tuple[datetime, datetime] | None:
        """(open - 5min, close + 5min) as tz-aware ET datetimes, or None when
        `day` is not a session. An early close (13:00) shrinks the window."""
        times = self.session_times(day)
        if times is None:
            return None
        o, c = times
        open_dt = datetime.combine(day, o, tzinfo=ET)
        close_dt = datetime.combine(day, c, tzinfo=ET)
        return (open_dt - PAGING_PAD, close_dt + PAGING_PAD)

    def in_paging_window(self, dt_: datetime) -> bool:
        """True when the tz-aware instant `dt_` falls inside its ET date's
        paging window (naive datetimes are taken as ET wall time)."""
        if dt_.tzinfo is None:
            dt_ = dt_.replace(tzinfo=ET)
        et = dt_.astimezone(ET)
        win = self.paging_window(et.date())
        return win is not None and win[0] <= et <= win[1]


# -- module-level active calendar --------------------------------------------- #
# The orchestrator registers its calendar here at startup so the STATIC
# Orchestrator._overlaps_paging_hours (called from the watchdog thread with the
# same two-arg signature tests monkeypatch) can consult it without a self
# reference. Tests pass a calendar explicitly instead. None = weekday math.
_ACTIVE: SessionCalendar | None = None


def set_active(cal: SessionCalendar | None) -> None:
    global _ACTIVE
    _ACTIVE = cal


def active() -> SessionCalendar | None:
    return _ACTIVE


def _fallback_in_window(dt_: datetime) -> bool:
    if dt_.weekday() >= 5:
        return False
    minute = dt_.hour * 60 + dt_.minute
    return (9 * 60 + 25) <= minute <= (16 * 60 + 5)


def paging_overlap(start_ts: float, end_ts: float,
                   calendar: SessionCalendar | None = None) -> bool:
    """True when any part of wall-clock [start_ts, end_ts] falls inside a
    paging window: (session open - 5min, session close + 5min) ET per
    `calendar` (default: the active module calendar), or weekday 09:25-16:05
    ET for dates the calendar does not cover. Checked at both endpoints plus
    each session open inside the span, so a multi-day gap can't thread
    between samples. Pure clock math over the cache — no network — because
    the watchdog thread calls this."""
    cal = calendar if calendar is not None else _ACTIVE

    def in_window(dt_: datetime) -> bool:
        if cal is None:
            return _fallback_in_window(dt_)
        return cal.in_paging_window(dt_)

    def session_open(day: date) -> datetime | None:
        if cal is None:
            if day.weekday() >= 5:
                return None
            return datetime.combine(day, FALLBACK_OPEN, tzinfo=ET)
        times = cal.session_times(day)
        if times is None:
            return None
        return datetime.combine(day, times[0], tzinfo=ET)

    start = datetime.fromtimestamp(start_ts, ET)
    end = datetime.fromtimestamp(end_ts, ET)
    if in_window(start) or in_window(end):
        return True
    day = start.date()
    while day <= end.date():
        opn = session_open(day)
        if opn is not None and start <= opn <= end:
            return True
        day += timedelta(days=1)
    return False
