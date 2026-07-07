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
from zoneinfo import ZoneInfo

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
        # Churn-guard clocks (2026-07-06): last BUY submit time per symbol (the
        # same-symbol top-up spacing) and last EXIT time per symbol (the post-
        # exit re-entry cooldown). Persisted so a restart doesn't forget that a
        # name was topped up / stopped out minutes ago. Pruned on write so they
        # can't grow unbounded.
        self.last_buy_times: dict[str, str] = {}
        self.exit_times: dict[str, str] = {}
        # Conviction of the LAST buy per symbol — the top-up evidence gate
        # rejects an add whose conviction shows no new edge over the prior entry
        # ("adding to a winner" is not a signal).
        self.last_buy_convictions: dict[str, float] = {}
        # Daily concentration accumulators (the all-LLY guard): $ submitted and
        # buy count per symbol for ONE ET trading day. Keyed to the exchange's
        # calendar (not UTC) so an evening restart doesn't hand back a fresh
        # budget mid-session; rolled lazily on access so no scheduler is needed.
        self.daily_deploy_day: str = ""
        self.daily_deploy_usd: dict[str, float] = {}
        self.daily_buy_counts: dict[str, int] = {}
        # Last trading day the nightly post-mortem ran, so the market-closed
        # tick fires it exactly once per day.
        self.postmortem_done_day: str = ""
        # Order ids submitted but not yet reconciled against their fills, as
        # [order_id, symbol] pairs. Persisted so a restart between cycles still
        # reconciles a reject/partial fill instead of leaving a phantom ledger
        # intent that never gets checked (1B.9).
        self.pending_orders: list[list[str]] = []
        # Last market-regime label seen, so the regime-off book TRIM (1B.6) fires
        # ONCE on the transition into risk-off, not every cycle we stay there.
        self.regime_label: str = ""
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
                    "take_pct": float(v.get("take_pct", 0.0)),
                    "scaled": float(v.get("scaled", 0.0))}
                for k, v in d.get("exits", {}).items()
            }
            self.entry_times = {
                k: str(v) for k, v in d.get("entry_times", {}).items()
            }
            self.last_buy_times = {
                k: str(v) for k, v in d.get("last_buy_times", {}).items()
            }
            self.exit_times = {
                k: str(v) for k, v in d.get("exit_times", {}).items()
            }
            self.last_buy_convictions = {
                k: float(v) for k, v in d.get("last_buy_convictions", {}).items()
            }
            self.daily_deploy_day = str(d.get("daily_deploy_day", ""))
            self.daily_deploy_usd = {
                k: float(v) for k, v in d.get("daily_deploy_usd", {}).items()
            }
            self.daily_buy_counts = {
                k: int(v) for k, v in d.get("daily_buy_counts", {}).items()
            }
            self.postmortem_done_day = str(d.get("postmortem_done_day", ""))
            self.pending_orders = [
                [str(oid), str(sym)] for oid, sym in d.get("pending_orders", [])
            ]
            self.regime_label = str(d.get("regime_label", ""))
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
                        "last_buy_times": self.last_buy_times,
                        "exit_times": self.exit_times,
                        "last_buy_convictions": self.last_buy_convictions,
                        "daily_deploy_day": self.daily_deploy_day,
                        "daily_deploy_usd": self.daily_deploy_usd,
                        "daily_buy_counts": self.daily_buy_counts,
                        "postmortem_done_day": self.postmortem_done_day,
                        "pending_orders": self.pending_orders,
                        "regime_label": self.regime_label,
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
    def register_exits(
        self, symbol: str, stop_pct: float, take_pct: float, scaled: bool = False,
    ) -> None:
        """Record the hard stop / take-profit (% from entry) the watchdog must
        enforce for a fractional position that can't carry an exchange bracket.
        `scaled` marks that the take-profit scale-out has already fired (1B.8), so
        the remainder rides the trailing stop instead of taking the full profit."""
        with self._lock:
            self.exits[symbol] = {
                "stop_pct": float(stop_pct), "take_pct": float(take_pct),
                "scaled": 1.0 if scaled else 0.0,
            }
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

    # -- churn-guard clocks (top-up spacing + re-entry cooldown) ------------- #
    _CLOCK_RETENTION_DAYS = 7.0  # cooldowns are hours-scale; week-old stamps are noise

    def register_buy(
        self, symbol: str, when: datetime | None = None,
        conviction: float | None = None,
    ) -> None:
        """Stamp the LAST buy-submit time for `symbol` (every buy, unlike
        register_entry which only stamps the first). Drives the same-symbol
        top-up spacing guard. Also records the buy's conviction when given —
        the top-up evidence gate compares the next add against it."""
        with self._lock:
            self.last_buy_times[symbol] = (
                when or datetime.now(timezone.utc)
            ).isoformat()
            if conviction is not None:
                self.last_buy_convictions[symbol] = float(conviction)
            self._prune_clock(self.last_buy_times)
            # Convictions ride the same retention as the buy clock: no stamp,
            # no comparison (the gate fails open on a missing prior).
            for sym in list(self.last_buy_convictions):
                if sym not in self.last_buy_times:
                    del self.last_buy_convictions[sym]
            self._save()

    def last_buy_conviction(self, symbol: str) -> float | None:
        return self.last_buy_convictions.get(symbol)

    def register_exit(self, symbol: str, when: datetime | None = None) -> None:
        """Stamp the time `symbol` was exited (any reason: decision sell, trail,
        stop, take, time-stop, flatten, exchange-side fill). Drives the post-exit
        re-entry cooldown."""
        with self._lock:
            self.exit_times[symbol] = (
                when or datetime.now(timezone.utc)
            ).isoformat()
            self._prune_clock(self.exit_times)
            self._save()

    def hours_since_buy(self, symbol: str, now: datetime | None = None) -> float | None:
        return self._hours_since(self.last_buy_times.get(symbol), now)

    def hours_since_exit(self, symbol: str, now: datetime | None = None) -> float | None:
        return self._hours_since(self.exit_times.get(symbol), now)

    @staticmethod
    def _hours_since(ts: str | None, now: datetime | None = None) -> float | None:
        if not ts:
            return None
        try:
            then = datetime.fromisoformat(ts)
        except ValueError:
            return None
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        now = now or datetime.now(timezone.utc)
        return (now - then).total_seconds() / 3600.0

    def _prune_clock(self, clock: dict[str, str]) -> None:
        """Drop stamps older than the retention window (call under the lock)."""
        cutoff_h = self._CLOCK_RETENTION_DAYS * 24.0
        stale = [
            sym for sym, ts in clock.items()
            if (h := self._hours_since(ts)) is None or h > cutoff_h
        ]
        for sym in stale:
            del clock[sym]

    # -- daily per-symbol concentration accumulators (the all-LLY guard) ----- #
    @staticmethod
    def _trading_day(when: datetime | None = None) -> str:
        """The ET calendar date, e.g. '2026-07-06' — the exchange's day, so a
        late-evening restart doesn't hand back a fresh daily budget while the
        session that spent it is still the same trading day."""
        when = when or datetime.now(timezone.utc)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return when.astimezone(ZoneInfo("America/New_York")).date().isoformat()

    def _roll_daily(self, when: datetime | None = None) -> None:
        """Reset the accumulators when the ET trading day changes (call under
        the lock). Lazy: every accessor rolls first, so there's no scheduler to
        miss and a restart lands on the right day automatically."""
        day = self._trading_day(when)
        if self.daily_deploy_day != day:
            self.daily_deploy_day = day
            self.daily_deploy_usd = {}
            self.daily_buy_counts = {}
            self._save()

    def register_daily_deploy(
        self, symbol: str, notional: float, when: datetime | None = None,
    ) -> None:
        """Count a submitted buy against the symbol's daily budget. Stamped at
        SUBMIT, not fill — a later reject leaves the budget spent (fail-closed)."""
        with self._lock:
            self._roll_daily(when)
            self.daily_deploy_usd[symbol] = (
                self.daily_deploy_usd.get(symbol, 0.0) + max(0.0, float(notional))
            )
            self.daily_buy_counts[symbol] = self.daily_buy_counts.get(symbol, 0) + 1
            self._save()

    def daily_symbol_spend(self, symbol: str, when: datetime | None = None) -> float:
        with self._lock:
            self._roll_daily(when)
            return self.daily_deploy_usd.get(symbol, 0.0)

    def daily_symbol_buys(self, symbol: str, when: datetime | None = None) -> int:
        with self._lock:
            self._roll_daily(when)
            return self.daily_buy_counts.get(symbol, 0)

    # -- nightly post-mortem once-per-day marker ----------------------------- #
    def get_postmortem_done_day(self) -> str:
        return self.postmortem_done_day

    def set_postmortem_done(self, day: str) -> None:
        with self._lock:
            if day != self.postmortem_done_day:
                self.postmortem_done_day = day
                self._save()

    # -- pending-order reconciliation list (survives a restart) ------------- #
    def set_pending_orders(self, pairs: list[tuple[str, str]]) -> None:
        """Persist the not-yet-reconciled (order_id, symbol) pairs so a restart
        between cycles still reconciles them (1B.9)."""
        with self._lock:
            self.pending_orders = [[str(oid), str(sym)] for oid, sym in pairs]
            self._save()

    def get_pending_orders(self) -> list[tuple[str, str]]:
        """The persisted pending (order_id, symbol) pairs, as tuples."""
        return [(oid, sym) for oid, sym in self.pending_orders]

    # -- last regime label (for the risk-off trim transition, 1B.6) --------- #
    def get_regime_label(self) -> str:
        return self.regime_label

    def set_regime_label(self, label: str) -> None:
        with self._lock:
            if label != self.regime_label:
                self.regime_label = label
                self._save()
