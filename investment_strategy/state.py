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
        # Planned stop WIDTH (%) of every executed decision buy — bracketed
        # whole-share names included, unlike `exits` (which doubles as the
        # watchdog's hard-exit enforcement list and must stay fractional-only
        # to avoid double-selling against the exchange bracket). Read by the
        # R-scaled trailing-stop geometry (risk.trail_geometry) as the
        # position's risk unit; informational, never enforced.
        self.stop_widths: dict[str, float] = {}
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
        # Last EXIT price per symbol (when known) — the price-aware re-entry
        # guard: within the cooldown, re-buying ABOVE the price we just exited is
        # chasing (paying up for the same name the exit just left). Pruned with
        # exit_times so a stale price never outlives its clock.
        self.exit_prices: dict[str, float] = {}
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
        # Last ISO week (e.g. "2026-W30") the weekly auto-tune report ran, so
        # the market-closed weekend tick fires it exactly once per week.
        self.autotune_done_week: str = ""
        # Decision-driven SELLs whose close attempt FAILED — retried by the
        # watchdog every tick (the same escalation ladder as any other exit)
        # instead of waiting for the next hourly decision cycle to maybe
        # re-propose the same sell, with no alert in between (2026-07-20 COO:
        # a real thesis-break exit sat unretried ~26h; every OTHER exit path
        # already retries + pages on failure). Keyed by symbol; the value
        # carries the ORIGINAL decision's rationale/signals/composite so the
        # eventual successful close still ledgers with real context, not a
        # generic "watchdog decision" label.
        self.pending_decision_sells: dict[str, dict] = {}
        # Order ids submitted but not yet reconciled against their fills, as
        # [order_id, symbol] pairs. Persisted so a restart between cycles still
        # reconciles a reject/partial fill instead of leaving a phantom ledger
        # intent that never gets checked (1B.9).
        self.pending_orders: list[list[str]] = []
        # Exit order ids the WATCHDOG has already ledgered, per symbol. A
        # falling-price re-replace supersedes the prior exit order with a new
        # id; this set lets the watchdog void the superseded SELL record
        # instead of stacking a duplicate full-qty exit each 30s tick.
        self.ledgered_exit_oids: dict[str, list[str]] = {}
        # Last market-regime label seen, so the regime-off book TRIM (1B.6) fires
        # ONCE on the transition into risk-off, not every cycle we stay there.
        self.regime_label: str = ""
        # Consecutive LOSING closed trips per symbol (Jul 29: the book recycles
        # a small universe — NOK/SPCX/SOFI-class names get re-entered after
        # every stop-out). A win (>= +1%) resets the streak; a scratch leaves
        # it. Survives resets (reset.py carries it over) so a fresh cycle
        # can't launder a name's record.
        self.loss_streaks: dict[str, int] = {}
        # When each symbol's streak was last INCREMENTED. One losing trip can
        # stamp register_exit several times (the partial-close ladder records
        # per replaced leg, the orchestrator stamps again, a DAY-expired option
        # close resubmits next open) — dedupe so a trip counts ONCE: stamps
        # within _STREAK_DEDUPE_HOURS are the same trip, because two REAL trips
        # are always separated by the 24h re-entry cooldown plus holding time.
        self.streak_times: dict[str, str] = {}
        # Last ET trading day the core-defense trim fired, so a falling tape
        # trims the core at most once per day instead of every hourly cycle.
        self.core_defense_day: str = ""
        # Last BOOK BETA reading (run-6 item 7a: portfolio/beta.py dict —
        # spy/qqq/iwm/invested_pct/betas/weights/unknown/at). Persisted so
        # the nightly post-mortem and scripts/eval_contract_check.py can
        # read the ex-ante exposure the cycle traded against. Informational.
        self.book_beta: dict = {}
        # SPY-beta the LAST beta-hedge arm was sized against and where it
        # came from (run-7 S-2): 'measured' = BookBeta.beta_of(HEDGE_ETF),
        # shrunk, cycle-cached; 'assumed' = Config.hedge_beta_assumed
        # because the ETF's own series could not be read or read outside
        # [-3.0, -0.5]. Stamped beside book_beta so the eval checker can
        # verify each arm's notional = gap x equity / |hedge_beta| without
        # re-deriving the ETF's beta. Informational; None = never armed.
        self.hedge_beta: float | None = None
        self.hedge_beta_source: str = ""
        # Last hedge-ETF unwind lot (run-7 S-8 / 4a-17 observability):
        # {symbol, qty, price (decision-exit quote), date (ET), at (UTC iso),
        # sessions: [ET dates the counterfactual line was logged on]}. The
        # orchestrator prints 'HEDGE COUNTERFACTUAL: last unwind lot ...
        # would be +$X today' for the five sessions after an unwind so a
        # whipsaw (Sep 9 exit 25.84 -> Sep 10 re-arm 26.09) is priced in
        # the log instead of reconstructed by hand. Informational.
        self.last_unwind: dict = {}
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
            self.stop_widths = {
                k: float(v) for k, v in d.get("stop_widths", {}).items()
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
            self.exit_prices = {
                k: float(v) for k, v in d.get("exit_prices", {}).items()
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
            self.autotune_done_week = str(d.get("autotune_done_week", ""))
            self.pending_decision_sells = {
                k: dict(v) for k, v in d.get("pending_decision_sells", {}).items()
            }
            self.pending_orders = [
                [str(oid), str(sym)] for oid, sym in d.get("pending_orders", [])
            ]
            self.ledgered_exit_oids = {
                k: [str(o) for o in v]
                for k, v in d.get("ledgered_exit_oids", {}).items()
            }
            self.regime_label = str(d.get("regime_label", ""))
            self.loss_streaks = {
                k: int(v) for k, v in d.get("loss_streaks", {}).items()
            }
            self.streak_times = {
                k: str(v) for k, v in d.get("streak_times", {}).items()
            }
            self.core_defense_day = str(d.get("core_defense_day", ""))
            bb = d.get("book_beta", {})
            self.book_beta = dict(bb) if isinstance(bb, dict) else {}
            hb = d.get("hedge_beta")
            self.hedge_beta = float(hb) if isinstance(hb, (int, float)) else None
            self.hedge_beta_source = str(d.get("hedge_beta_source", "") or "")
            lu = d.get("last_unwind", {})
            self.last_unwind = dict(lu) if isinstance(lu, dict) else {}
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
                        "stop_widths": self.stop_widths,
                        "entry_times": self.entry_times,
                        "last_buy_times": self.last_buy_times,
                        "exit_times": self.exit_times,
                        "exit_prices": self.exit_prices,
                        "last_buy_convictions": self.last_buy_convictions,
                        "daily_deploy_day": self.daily_deploy_day,
                        "daily_deploy_usd": self.daily_deploy_usd,
                        "daily_buy_counts": self.daily_buy_counts,
                        "postmortem_done_day": self.postmortem_done_day,
                        "autotune_done_week": self.autotune_done_week,
                        "pending_decision_sells": self.pending_decision_sells,
                        "pending_orders": self.pending_orders,
                        "ledgered_exit_oids": self.ledgered_exit_oids,
                        "regime_label": self.regime_label,
                        "loss_streaks": self.loss_streaks,
                        "streak_times": self.streak_times,
                        "core_defense_day": self.core_defense_day,
                        "book_beta": self.book_beta,
                        "hedge_beta": self.hedge_beta,
                        "hedge_beta_source": self.hedge_beta_source,
                        "last_unwind": self.last_unwind,
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
            dropped |= self.stop_widths.pop(symbol, None) is not None
            dropped |= self.entry_times.pop(symbol, None) is not None
            dropped |= self.ledgered_exit_oids.pop(symbol, None) is not None
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

    def register_stop_width(self, symbol: str, stop_pct: float) -> None:
        """Record the planned stop width of an executed buy (any position class)
        — the risk unit the R-scaled trail geometry reads. Never enforced."""
        if not stop_pct or stop_pct <= 0:
            return
        with self._lock:
            self.stop_widths[symbol] = float(stop_pct)
            self._save()

    def get_stop_width(self, symbol: str) -> float:
        """The registered planned stop width, preferring the enforced `exits`
        record (kept in sync on trims/scale-outs) over the buy-time note."""
        ex = self.exits.get(symbol)
        if ex and ex.get("stop_pct", 0.0) > 0:
            return float(ex["stop_pct"])
        return float(self.stop_widths.get(symbol, 0.0))

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
    #: A-8: last-buy convictions are kept by COUNT, not by the 7-day clock
    #: (the top-up bar must outlive a week-long hold). ~25 names/week -> years.
    _CONVICTION_MAX_ENTRIES = 400
    # Same-trip window for loss-streak stamps: must exceed the longest gap a
    # single trip's exit records can span (a DAY-expired close resubmitted at
    # the next open is ~17.5h later) while staying under the 24h+hold minimum
    # between two genuine trips of one symbol.
    _STREAK_DEDUPE_HOURS = 20.0

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
                # Re-insert so dict order == recency (the size cap below
                # drops the OLDEST buys first).
                self.last_buy_convictions.pop(symbol, None)
                self.last_buy_convictions[symbol] = float(conviction)
            self._prune_clock(self.last_buy_times)
            # A-8 (Sep 21 2026): convictions NO LONGER ride the 7-day buy
            # clock. They used to be deleted with the stamp, and the top-up
            # gate "fails open on a missing prior" — so any name held longer
            # than a week could be added to at ANY conviction, below its own
            # entry included (run-6: SMCI Sep 10 + SPCX Sep 15 top-ups,
            # $65,573, passed only through that hole). The gate consults a
            # conviction only while the symbol is HELD, and every buy
            # re-stamps it, so a stale entry for a closed name is inert; the
            # map is bounded by count instead of age.
            while len(self.last_buy_convictions) > self._CONVICTION_MAX_ENTRIES:
                del self.last_buy_convictions[next(iter(self.last_buy_convictions))]
            self._save()

    def last_buy_conviction(self, symbol: str) -> float | None:
        return self.last_buy_convictions.get(symbol)

    def register_exit(
        self, symbol: str, when: datetime | None = None,
        price: float | None = None, pl_pct: float | None = None,
    ) -> None:
        """Stamp the time `symbol` was exited (any reason: decision sell, trail,
        stop, take, time-stop, flatten, exchange-side fill). Drives the post-exit
        re-entry cooldown. `price` (the exit fill/mark, when known) drives the
        price-aware re-entry guard — re-buying above it within the cooldown is
        chasing. `pl_pct` (the trip's realized %, when known) drives the
        loss-streak scorecard: a losing trip (<= -1%) extends the symbol's
        streak, a winning one (>= +1%) clears it, a scratch leaves it."""
        with self._lock:
            self.exit_times[symbol] = (
                when or datetime.now(timezone.utc)
            ).isoformat()
            if price is not None and price > 0:
                self.exit_prices[symbol] = float(price)
            if pl_pct is not None:
                if pl_pct <= -1.0:
                    # Dedupe: the close ladder can stamp one losing trip
                    # several times (per replaced leg, per retry tick, on a
                    # next-open resubmit). Real trips are >= 24h apart (the
                    # re-entry cooldown), so stamps inside the window are the
                    # SAME trip and must not inflate the streak.
                    since = self._hours_since(self.streak_times.get(symbol))
                    if since is None or since >= self._STREAK_DEDUPE_HOURS:
                        self.loss_streaks[symbol] = (
                            self.loss_streaks.get(symbol, 0) + 1
                        )
                        self.streak_times[symbol] = datetime.now(
                            timezone.utc
                        ).isoformat()
                elif pl_pct >= 1.0:
                    self.loss_streaks.pop(symbol, None)
                    self.streak_times.pop(symbol, None)
            self._prune_clock(self.exit_times)
            # Drop any exit price whose clock was just pruned away.
            for sym in list(self.exit_prices):
                if sym not in self.exit_times:
                    del self.exit_prices[sym]
            self._save()

    def loss_streak(self, symbol: str) -> int:
        """Consecutive losing closed trips for `symbol` (0 = none recorded)."""
        return self.loss_streaks.get(symbol, 0)

    def last_exit_price(self, symbol: str) -> float | None:
        """The price at which `symbol` was last exited, or None if unknown."""
        return self.exit_prices.get(symbol)

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

    # -- weekly auto-tune once-per-week marker ------------------------------- #
    def get_autotune_done_week(self) -> str:
        return self.autotune_done_week

    def set_autotune_done(self, week: str) -> None:
        with self._lock:
            if week != self.autotune_done_week:
                self.autotune_done_week = week
                self._save()

    # -- decision-sell retry queue (survives a restart) ---------------------- #
    def queue_decision_sell(
        self, symbol: str, rationale: str, key_signals: list[str] | None = None,
        composite_score: float | None = None,
        sell_events: list[str] | None = None,
    ) -> None:
        """Remember a decision-driven SELL whose close attempt failed, so the
        watchdog retries it every tick — see the field's docstring above.
        `sell_events` (A-5) is the risk gate's event sanction; persisted so a
        retry that lands after a restart still ledgers it."""
        with self._lock:
            self.pending_decision_sells[symbol] = {
                "rationale": rationale,
                "key_signals": list(key_signals or []),
                "composite_score": composite_score,
                "sell_events": [str(e) for e in (sell_events or [])],
            }
            self._save()

    def pop_decision_sell(self, symbol: str) -> dict | None:
        with self._lock:
            d = self.pending_decision_sells.pop(symbol, None)
            if d is not None:
                self._save()
            return d

    def get_pending_decision_sells(self) -> dict[str, dict]:
        return dict(self.pending_decision_sells)

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

    def add_pending_order(self, oid: str, symbol: str) -> None:
        """Append ONE pair from any thread. The watchdog queues its exit orders
        here so _reconcile_fills checks their real outcome next cycle (a
        replace-time SELL record is an intent, not a fill) — appended under the
        lock so it can't interleave with the orchestrator's list writes."""
        with self._lock:
            if not any(p[0] == str(oid) for p in self.pending_orders):
                self.pending_orders.append([str(oid), str(symbol)])
                self._save()

    def drain_pending_orders(self) -> list[tuple[str, str]]:
        """Atomically take-and-clear the persisted pairs. Reconcile is about to
        check them, and a crash mid-reconcile must not re-examine (or
        re-strand) them next boot — the same contract the old clear-then-check
        had, but race-free against a concurrent watchdog add."""
        with self._lock:
            pairs = [(oid, sym) for oid, sym in self.pending_orders]
            self.pending_orders = []
            self._save()
            return pairs

    def merge_pending_orders(self, pairs: list[tuple[str, str]]) -> None:
        """Union `pairs` into the persisted list (dedupe by order id). The
        orchestrator's end-of-cycle persist uses this instead of a plain
        overwrite, which silently dropped any exit the watchdog thread queued
        DURING the minutes-long decision cycle."""
        with self._lock:
            seen = {p[0] for p in self.pending_orders}
            added = False
            for oid, sym in pairs:
                if str(oid) not in seen:
                    self.pending_orders.append([str(oid), str(sym)])
                    seen.add(str(oid))
                    added = True
            if added:
                self._save()

    # -- watchdog-ledgered exit orders (re-replace supersede tracking) ------ #
    def note_exit_ledgered(self, symbol: str, oid: str) -> None:
        """Remember that the watchdog ledgered a SELL for exit order `oid`, so
        a later re-replace of that same order can void the superseded record
        instead of double-counting the exit. Capped per symbol — the set only
        needs to cover the short window an exit is being chased."""
        with self._lock:
            oids = self.ledgered_exit_oids.setdefault(symbol, [])
            if str(oid) not in oids:
                oids.append(str(oid))
                del oids[:-20]  # bound growth; an exit chase is a few ticks
                self._save()

    def exit_was_ledgered(self, symbol: str, oid: str) -> bool:
        return str(oid) in self.ledgered_exit_oids.get(symbol, [])

    # -- core-defense daily latch (falling-tape core trim) ------------------- #
    def core_defense_fired_today(self, when: datetime | None = None) -> bool:
        return self.core_defense_day == self._trading_day(when)

    def mark_core_defense(self, when: datetime | None = None) -> None:
        with self._lock:
            self.core_defense_day = self._trading_day(when)
            self._save()

    # -- last regime label (for the risk-off trim transition, 1B.6) --------- #
    def get_regime_label(self) -> str:
        return self.regime_label

    def get_book_beta(self) -> dict:
        return dict(self.book_beta)

    def set_book_beta(self, reading: dict) -> None:
        """Persist the cycle's BOOK BETA reading (run-6 item 7a)."""
        with self._lock:
            self.book_beta = dict(reading or {})
            self._save()

    def get_hedge_beta(self) -> tuple[float | None, str]:
        """(hedge SPY-beta the last beta-hedge arm divided by, its source
        'measured' | 'assumed' | '' when never armed) — run-7 S-2."""
        return self.hedge_beta, self.hedge_beta_source

    def set_hedge_beta(self, beta: float, source: str) -> None:
        """Stamp the divisor used for a beta-hedge arm beside book_beta so
        the arm's notional is auditable from risk_state alone (run-7 S-2)."""
        with self._lock:
            self.hedge_beta = float(beta)
            self.hedge_beta_source = str(source or "")
            self._save()

    # -- last hedge unwind lot (run-7 S-8 / 4a-17) --------------------------- #
    def get_last_unwind(self) -> dict:
        return dict(self.last_unwind)

    def set_last_unwind(
        self, symbol: str, qty: float, price: float, date: str, at: str,
    ) -> None:
        """Stamp the lot a hedge unwind just closed (decision-exit quote —
        the fill lands asynchronously; see ledger.set_fill). Replaces the
        previous lot: the counterfactual line prices the LAST unwind only."""
        with self._lock:
            self.last_unwind = {
                "symbol": str(symbol or "").upper(), "qty": float(qty or 0.0),
                "price": float(price or 0.0), "date": str(date or ""),
                "at": str(at or ""), "sessions": [],
            }
            self._save()

    def mark_unwind_session(self, date: str) -> int:
        """Record that the counterfactual line was logged on ET `date` and
        return its 1-based session index after the unwind (0 = the unwind
        day itself). Persisted so a restart does not re-count sessions."""
        with self._lock:
            lu = self.last_unwind
            if not lu:
                return 0
            if date == lu.get("date", ""):
                return 0
            sessions = [str(s) for s in lu.get("sessions", []) or []]
            if date not in sessions:
                if len(sessions) >= 6:
                    return len(sessions) + 1      # past the window: no growth
                sessions.append(date)
                lu["sessions"] = sessions
                self._save()
            return sessions.index(date) + 1

    def set_regime_label(self, label: str) -> None:
        with self._lock:
            if label != self.regime_label:
                self.regime_label = label
                self._save()
