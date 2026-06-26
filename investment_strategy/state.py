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
        if not self.halted:
            log.critical("LATCHING HALT: %s", reason)
        self.halted = True
        self.halt_reason = reason
        self.halted_at = datetime.now(timezone.utc).isoformat()
        self._save()

    def clear_halt(self) -> None:
        self.halted = False
        self.halt_reason = ""
        self.halted_at = None
        self._save()

    # -- per-symbol trailing high-water ------------------------------------ #
    def get_high_water(self, symbol: str) -> float:
        return self.high_water.get(symbol, 0.0)

    def set_high_water(self, symbol: str, value: float) -> None:
        if value != self.high_water.get(symbol):
            self.high_water[symbol] = value
            self._save()

    def forget_symbol(self, symbol: str) -> None:
        if self.high_water.pop(symbol, None) is not None:
            self._save()

    def clear_high_water(self) -> None:
        if self.high_water:
            self.high_water.clear()
            self._save()
