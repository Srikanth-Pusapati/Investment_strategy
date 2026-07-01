"""Durable risk state — the bot's memory of how bad things have gotten.

Two things MUST survive a restart or they silently reset to "all clear":

  1. peak_equity — the high-water mark for the whole account. Drawdown is
     measured peak-to-trough, NOT day-over-day, so a slow bleed can't hide by
     resetting every morning (the daily-loss limit alone does reset).
  2. high_water[symbol] — per-position peak unrealized P/L, so the trailing
     stop doesn't forget its ratchet every time the process bounces.

It also holds a HALT LATCH: once the equity floor is breached we flatten and
refuse to open anything new until a human clears it. This is the last line
against "the bot kept trading a dying account." Clearing requires deleting the
state file (or calling clear_halt) — by design it does not auto-resume.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("state")

# state/ is gitignored — local risk state never gets committed.
DEFAULT_STATE_PATH = Path("state") / "risk_state.json"


class PortfolioState:
    """JSON-backed risk memory shared by RiskManager and Watchdog."""

    def __init__(self, path: Path | str = DEFAULT_STATE_PATH):
        self.path = Path(path)
        self.peak_equity: float = 0.0
        self.halted: bool = False
        self.halt_reason: str = ""
        self.halted_at: str | None = None
        self.high_water: dict[str, float] = {}
        # Hard stop / take-profit targets (% from entry) for FRACTIONAL positions,
        # which have no exchange-side bracket. The watchdog enforces these. Keyed
        # by symbol: {"stop_pct": float, "take_pct": float}.
        self.exits: dict[str, dict[str, float]] = {}
        # First-entry timestamp (ISO) per held symbol — the "hold clock" for the
        # deterministic time-stop that recycles dead/flat capital (1B.4). Set on
        # the opening buy; the watchdog also stamps a first-seen fallback so a
        # restart or pre-existing position still gets a clock.
        self.entry_times: dict[str, str] = {}
        # The watchdog (its own thread) and the decision/risk path both touch this
        # state. A reentrant lock keeps reads/writes and the file save consistent.
        self._lock = threading.RLock()
        self._load()

    # -- persistence -------------------------------------------------------- #
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            self.peak_equity = float(d.get("peak_equity", 0.0))
            self.halted = bool(d.get("halted", False))
            self.halt_reason = str(d.get("halt_reason", ""))
            self.halted_at = d.get("halted_at")
            self.high_water = {k: float(v) for k, v in d.get("high_water", {}).items()}
            self.exits = {
                k: {"stop_pct": float(v.get("stop_pct", 0.0)),
                    "take_pct": float(v.get("take_pct", 0.0))}
                for k, v in d.get("exits", {}).items()
            }
            self.entry_times = {
                k: str(v) for k, v in d.get("entry_times", {}).items()
            }
            if self.halted:
                log.warning("Loaded LATCHED HALT from state: %s", self.halt_reason)
        except Exception as e:  # corrupt state must not crash startup
            log.error("Could not read %s (%s); starting from clean state.", self.path, e)

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(
                    {
                        "peak_equity": self.peak_equity,
                        "halted": self.halted,
                        "halt_reason": self.halt_reason,
                        "halted_at": self.halted_at,
                        "high_water": self.high_water,
                        "exits": self.exits,
                        "entry_times": self.entry_times,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            tmp.replace(self.path)  # atomic-ish swap so a crash mid-write can't corrupt
        except Exception as e:  # never let state persistence break the trade loop
            log.warning("Risk state save failed: %s", e)

    # -- equity high-water / drawdown -------------------------------------- #
    def update_equity(self, equity: float) -> None:
        """Ratchet the all-time-high equity. Persists only on a new high."""
        with self._lock:
            if equity > self.peak_equity:
                self.peak_equity = equity
                self._save()

    def drawdown_pct(self, equity: float) -> float:
        """Peak-to-current drawdown as a positive % (0 if at/above peak)."""
        if self.peak_equity <= 0:
            return 0.0
        return max(0.0, (self.peak_equity - equity) / self.peak_equity * 100.0)

    # -- the halt latch ----------------------------------------------------- #
    def latch_halt(self, reason: str) -> None:
        with self._lock:
            if not self.halted:
                log.critical("LATCHING HALT: %s", reason)
            self.halted = True
            self.halt_reason = reason
            self.halted_at = datetime.now(timezone.utc).isoformat()
            self._save()

    def clear_halt(self) -> None:
        with self._lock:
            self.halted = False
            self.halt_reason = ""
            self.halted_at = None
            self._save()

    # -- per-symbol trailing high-water ------------------------------------ #
    def get_high_water(self, symbol: str) -> float:
        return self.high_water.get(symbol, 0.0)

    def set_high_water(self, symbol: str, value: float) -> None:
        with self._lock:
            if value != self.high_water.get(symbol):
                self.high_water[symbol] = value
                self._save()

    def forget_symbol(self, symbol: str) -> None:
        with self._lock:
            dropped = self.high_water.pop(symbol, None) is not None
            dropped |= self.exits.pop(symbol, None) is not None
            dropped |= self.entry_times.pop(symbol, None) is not None
            if dropped:
                self._save()

    def clear_high_water(self) -> None:
        with self._lock:
            if self.high_water:
                self.high_water.clear()
                self._save()

    # -- hard exits for fractional positions (no exchange bracket) ---------- #
    def register_exits(self, symbol: str, stop_pct: float, take_pct: float) -> None:
        """Record the hard stop / take-profit (% from entry) the watchdog must
        enforce for a fractional position that can't carry an exchange bracket."""
        with self._lock:
            self.exits[symbol] = {"stop_pct": float(stop_pct), "take_pct": float(take_pct)}
            self._save()

    def get_exits(self, symbol: str) -> dict[str, float] | None:
        return self.exits.get(symbol)

    # -- hold clock for the deterministic time-stop (1B.4) ------------------ #
    def register_entry(self, symbol: str, when: datetime | None = None) -> None:
        """Stamp the FIRST time we saw this position, starting its hold clock.
        Idempotent: a symbol already on the clock is left untouched, so adding to
        an existing position (or a watchdog first-seen fallback) never resets the
        age of the original entry."""
        with self._lock:
            if symbol not in self.entry_times:
                self.entry_times[symbol] = (when or datetime.now(timezone.utc)).isoformat()
                self._save()

    def entry_age_days(self, symbol: str, now: datetime | None = None) -> float | None:
        """Calendar days since the position's first entry, or None if unknown."""
        ts = self.entry_times.get(symbol)
        if not ts:
            return None
        try:
            entered = datetime.fromisoformat(ts)
        except ValueError:
            return None
        if entered.tzinfo is None:
            entered = entered.replace(tzinfo=timezone.utc)
        now = now or datetime.now(timezone.utc)
        return (now - entered).total_seconds() / 86_400.0
