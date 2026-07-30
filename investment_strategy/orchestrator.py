"""Ties the four stages together and runs the two timed loops.

  decision cycle (slow, ~15m):  signals + benchmark + holdings -> Claude
                                -> risk (vol-targeted sizing) -> orders
  watchdog      (fast, ~30s):   positions -> emergency exit / trailing stops

A single thread drives both: the watchdog runs every tick; the decision cycle
runs when its interval has elapsed. Closing positions is never gated; opening is
gated by the kill switch and the market being open.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import Timeout as RequestsTimeout
from urllib3.exceptions import ProtocolError

from .attribution import negative_expectancy_families, parse_cited, render_lessons
from .journal import DecisionJournal, DecisionRecord
from .benchmark import BenchmarkTracker
from .config import Config
from .correlation import CorrelationGuard
from .decision import DecisionEngine
from .earnings import EarningsCalendar
from .execution import AlpacaClient, OptionsHelper
from .execution.options import parse_occ
from .ledger import TradeLedger, TradeRecord
from .models import (
    Action,
    Candidate,
    Instrument,
    OrderRequest,
    OrderType,
    Position,
    RiskVerdict,
    SignalBundle,
    SignalKind,
    TIF,
    TradeProposal,
)
from .monitor import Watchdog
from .notify import Alerter, ping_heartbeat
from .portfolio import RobinhoodReader
from .risk import RiskManager
from .regime import RegimeReader
from .reset import maybe_reset_on_account_change
from .screener import ScreenerAggregator
from .sectors import SectorMap
from .signals import SignalAggregator, SignalHistory
from .signals.composite import composite_score, perf_weights
from .signals.quiver_client import QuiverClient
from .state import PortfolioState
from .status import EquityHistory, compute_status

log = logging.getLogger("orchestrator")

# Transient network faults that already survived the broker's own retries. They're
# self-healing (the next tick reconnects), so they're logged as a one-line warning
# rather than a full traceback — a reset-by-peer isn't a bug to debug.
_TRANSIENT_NET = (RequestsConnectionError, RequestsTimeout, ProtocolError)

# The model is DISCOVERY-DRIVEN: there is no standing watchlist. The scanner
# surfaces names each cycle and current holdings are always re-evaluated, so the
# default universe is EMPTY. An explicit WATCHLIST (comma list) can still be set to
# force-include names (e.g. for testing), but it is not part of the design.
DEFAULT_WATCHLIST: list[str] = []


class Orchestrator:
    def __init__(self, cfg: Config, watchlist: list[str] | None = None):
        self.cfg = cfg
        self.broker = AlpacaClient(cfg)
        # If the connected Alpaca account changed since the last run (recreated, or
        # a paper<->live switch), archive the OLD account's local state and start
        # fresh — BEFORE loading state/ledger/equity below, so they load clean and
        # the dashboard + risk memory don't carry a stale peak-equity or old trades.
        maybe_reset_on_account_change(cfg, self.broker)
        # One Quiver client shared by the signal and screener layers so each live
        # feed (congress, etc.) is pulled at most once per cycle, not once per
        # layer — the double-pull fix that keeps us under Quiver's rate limit.
        self.quiver = QuiverClient(cfg.quiver_api_key)
        self.signals = SignalAggregator(cfg, self.quiver)
        # Per-(symbol, kind) score series persisted across cycles (E.1+R.4):
        # feeds the freshness/trend annotations rendered on each signal line.
        self.signal_history = SignalHistory()
        self.screeners = ScreenerAggregator(cfg, self.quiver)
        self.robinhood = RobinhoodReader(cfg)
        # A restart clears the in-memory dead-auth latch; clear a stale
        # AUTH DEAD health file too, or the panel shows a phantom outage.
        self.robinhood.reconcile_health()
        # Per-cycle-cached next-earnings lookup feeding the risk earnings-blackout
        # guard (one lookup per symbol per cycle; advisory, fails open). With the
        # RH MCP on, ONE market-wide calendar call per cycle replaces the flaky
        # per-symbol yfinance lookups (C.5); yfinance stays as the fallback.
        self.earnings = EarningsCalendar(reader=self.robinhood)
        # Per-cycle-cached sector lookup feeding the risk sector-concentration cap.
        self.sectors = SectorMap()
        # Per-cycle-cached pairwise-correlation guard (R.2): measures whether a
        # NEW buy is effectively a duplicate of something already held; the
        # RiskManager enforces the cap on the number computed here.
        self.corr_guard = CorrelationGuard(self.broker)
        # Per-cycle market-regime read; scales position size down in risk-off, and
        # DOWN (not full) when its yfinance feed is degraded — since that same
        # outage blinds the sector cap too (1B.7).
        self.regime = RegimeReader(degraded_mult=cfg.risk.regime_degraded_mult)
        self._regime_mult = 1.0   # set each cycle from the regime read
        # Long-run direction ("up"/"down"/"") + blended label, set alongside the
        # multiplier each cycle — they drive the option call/put direction gate.
        self._regime_trend = ""
        self._regime_label = ""
        # Buys rejected at EQUITY-only gates this cycle, queued for the scoped
        # same-cycle option fallback (reset each _execute_proposals pass).
        self._option_fallbacks: list[tuple[TradeProposal, str]] = []
        # Falling-tape core defense flag, set each cycle by _apply_core_defense
        # and read by _apply_core_fill (no DCA into a falling market).
        self._core_defense_active = False
        # The index symbol whose defined-risk put the falling-market read
        # sanctioned THIS cycle ("" = none) — _handle_option passes it to the
        # direction gate so the sanction survives even when the core isn't held.
        self._hedge_symbol = ""
        # Auto-hedge persistence counters (Jul 30): consecutive decision cycles
        # the falling read has held / been clear. In-memory by design — a
        # restart just re-counts, which only ever DELAYS arming or unwinding.
        self._falling_cycles = 0
        self._clear_cycles = 0
        # Signal families with negative trailing expectancy, recomputed each
        # cycle from the ledger and fed to the risk layer's expectancy gate.
        self._neg_families: dict = {}
        # Bearish-funnel counters (Jul 30): reset each cycle, logged at the
        # end so put-path dormancy is visible instead of silent.
        self._bear_puts_proposed = 0
        self._bear_puts_approved = 0
        if (
            cfg.risk.options_enabled
            and getattr(cfg.risk, "option_direction_gate", True)
            and not cfg.risk.regime_filter_enabled
        ):
            log.warning(
                "OPTION_DIRECTION_GATE=on but REGIME_FILTER_ENABLED=off — no "
                "market trend is ever read, so the option direction gate (and "
                "the regime-scaled premium cap) is inert."
            )
        self.engine = DecisionEngine(cfg)
        # One persisted risk-state instance shared by the risk gate and watchdog
        # so peak equity, the drawdown halt, and the halt latch are consistent.
        self.state = PortfolioState(cfg.state_file)
        self.risk = RiskManager(cfg.risk, kill_switch=cfg.kill_switch, state=self.state)
        self.ledger = TradeLedger()
        # Out-of-band paging for watchdog CRITICALs (failed close = naked position;
        # latched halt). Log-only unless ALERTS_ENABLED + a sink is configured.
        # async_send: this is a long-lived process, so blocking SMTP/webhook I/O
        # moves off the watchdog thread (a post-wake DNS stall must not wedge the
        # safety loop); run() flushes on shutdown so no page is lost.
        self.alerter = Alerter(cfg.alerts, async_send=True)
        # Last RH dead-auth latch timestamp we paged for: one page per latch
        # EVENT (not per cycle), and a fresh latch after recovery pages again.
        self._rh_paged_for: float = 0.0
        # Rotation-guard persistence memory: symbol -> (ET day, loss% at the
        # day's FIRST veto), feeding the repeated-veto deterioration release.
        self._rotation_vetoes: dict[str, tuple[str, float]] = {}
        # The watchdog records its own exits (stops/take-profits/flattens) to the
        # ledger so signal attribution sees every close, not just decision sells.
        # A core stop that wash-trade-rejects (entry buy still open) is retried
        # from the 30s watchdog LOOP (outside its trade-lock hold — see
        # _watchdog_loop) instead of waiting a full decision cycle.
        self._core_stop_gap = False
        self.watchdog = Watchdog(
            cfg, self.broker, state=self.state, ledger=self.ledger,
            alerter=self.alerter,
            on_exchange_exit=self._backfill_exchange_exits_locked,
        )
        self.benchmark = BenchmarkTracker(cfg, self.broker, symbol=cfg.benchmark_symbol)
        self.options = OptionsHelper(cfg) if cfg.risk.options_enabled else None
        # Discovery-driven by design: default (unset) is no standing watchlist.
        # An explicit list can still force-include names; otherwise we trade only
        # what the scanner discovers plus whatever we currently hold.
        self.watchlist = DEFAULT_WATCHLIST if watchlist is None else watchlist
        if not self.watchlist and not cfg.screener.enabled:
            log.warning(
                "No watchlist AND the screener is disabled — there is nothing to "
                "discover or evaluate. Enable SCREENER_ENABLED (the model is "
                "discovery-driven) or set WATCHLIST to force-include names."
            )
        elif not self.watchlist:
            log.info("Discovery-driven mode: trading scanner-found names + holdings.")
        # Intra-day decision journal: records every verdict so the 'Today so far'
        # block in the prompt gives Claude self-awareness within the trading day,
        # and so rejected proposals have a durable audit trail (not just log.info).
        self.journal = DecisionJournal()
        # Persisted daily equity snapshots so the account P&L curve survives restarts.
        self.equity_history = EquityHistory()
        # WALL-CLOCK stamp (time.time()) of the last decision cycle's START.
        # -inf => the FIRST tick is unconditionally due. This used to be
        # time.monotonic(), which on macOS freezes during sleep/suspend: a
        # laptop that slept an hour advanced monotonic by only the awake time,
        # so the cadence stalled and cycles were MISSED for the whole slept span
        # (Jul 18: 02:05 -> 12:34 starvation). Wall-clock keeps ticking through
        # sleep, so the next cycle is due the moment the host wakes. Hang
        # detection (_last_main_tick) stays monotonic — that's the correct clock
        # for 'is the loop wedged', which sleep is not.
        self._last_decision_at = float("-inf")
        # Next session open (UTC), stashed by closed-market ticks so the loop
        # can fire a decision AT the bell instead of at the next hourly tick
        # (2026-07-13: a tick 17s before the open slept through the first hour).
        self._next_open_utc: datetime | None = None
        # Whether the last cycle found the market open. Gates the dashboard
        # refresh: rebuilding the HTML every hour overnight burns an AlpacaClient
        # + full price sweep for byte-identical output. True initially so the
        # first tick always paints; a final refresh still fires on the
        # open->closed transition to capture the settled end-of-day picture.
        self._cycle_market_open = True
        self._dashboard_open_last = True
        # Serializes broker order mutations so the watchdog's emergency closes and
        # the decision cycle's order placement can't interleave (e.g. double-close).
        # It guards only the quick submit/close calls — never the slow LLM call —
        # so the safety thread is delayed at most by an order-submission window.
        self._trade_lock = threading.Lock()
        # Serializes the exchange-exit backfill's ledger read-modify-append: it
        # now runs from BOTH the decision cycle and the watchdog thread (on a
        # vanished position), and the two must not interleave a double-append.
        self._backfill_lock = threading.Lock()
        self._stop = threading.Event()
        # Order ids submitted last cycle, reconciled against actual fills at the
        # start of the next one (by then ~a decision interval has passed, so async
        # fills have settled). Catches rejects, partial fills, and silent drops.
        # Loaded from persisted state so a restart between cycles still reconciles
        # a reject/partial instead of leaving a phantom ledger intent (1B.9).
        self._pending_oids: list[tuple[str, str]] = self.state.get_pending_orders()
        if self._pending_oids:
            log.info(
                "Loaded %d pending order(s) from state to reconcile.",
                len(self._pending_oids),
            )  # (order_id, symbol)
        # How many reconcile passes each oid has come back UNRESOLVED (broker read
        # failed = "unknown", or still "new"/"accepted"). In-memory: a restart
        # re-reconciles from the persisted pending list. Bounds the re-queue so a
        # permanently unreadable oid isn't chased forever, but isn't DROPPED on a
        # single blip either (the old code lumped "unknown" with "filled" and
        # dropped it after one look — a rejected order caught by a network blip
        # left its phantom ledger intent uncorrected forever).
        self._oid_retries: dict[str, int] = {}
        # Liveness stamps (goGA GA-2.1/2.2). _last_main_tick gates the heartbeat:
        # the watchdog thread only pings the external dead-man URL while the main
        # loop is ALSO fresh, so either thread dying silences the ping and the
        # external monitor pages. _last_wall_tick detects dark gaps (laptop sleep,
        # clock jumps) that monotonic timers can't see.
        self._last_main_tick = time.monotonic()
        self._last_wall_tick = time.time()
        # Wall-clock stamp of the last MAIN-loop tick (distinct from the
        # watchdog's _last_wall_tick): a big jump here means the host just
        # resumed from sleep, so the decision loop settles the network before
        # its first post-wake cycle (see _tick / _await_network_settle).
        self._last_main_wall = time.time()
        # In-memory halt latch: backs the kill-switch FILE when the file write
        # itself failed (disk full/read-only). Cleared only by restart — if we
        # couldn't write the ack file, there's nothing a human can delete to ack.
        self._forced_halt = False
        # Last time we paged about running on battery during market hours (0 =
        # never). Throttled: clamshell/battery sleep is a standing condition, not
        # a one-shot event, so one page per BATTERY_WARN_COOLDOWN_S is enough.
        self._last_battery_warn = 0.0
        # Consecutive watchdog ticks that failed on a transient network error.
        # A one-off skip is normal; a RUN of them during market hours means the
        # safety loop is effectively blind and must page (Jul 17: 9 in a row,
        # zero alerts). Reset on any clean tick.
        self._watchdog_skips = 0

    # -- main loop ---------------------------------------------------------- #
    def run(self) -> None:
        mode = "LIVE 🔴" if self.cfg.is_live else "paper"
        log.info(
            "Starting orchestrator (%s). Kill switch: %s. Options: %s. Benchmark: %s.",
            mode, "ON" if self.cfg.kill_switch else "off",
            "on" if self.cfg.risk.options_enabled else "off", self.cfg.benchmark_symbol,
        )
        self._warn_on_weak_safety_config()

        # The watchdog runs on its OWN thread so emergency exits, the equity floor,
        # and trailing stops keep firing on their fast cadence even while the slow
        # decision cycle is blocked on the LLM/network. That independence is the
        # whole point of a watchdog — on a single thread it was only "always-on"
        # between decision cycles.
        wd_thread = threading.Thread(
            target=self._watchdog_loop, name="watchdog", daemon=True
        )
        wd_thread.start()

        # Ctrl-C almost always lands inside _stop.wait() below (that's where this
        # loop spends nearly all its time), so the KeyboardInterrupt handler wraps
        # the WHOLE loop, not just the decision body — otherwise an interrupt during
        # the wait escaped as an ugly traceback. The finally guarantees a clean
        # shutdown (signal the watchdog thread, then join it) on any exit path.
        try:
            while not self._stop.is_set():
                try:
                    self._tick()
                except _TRANSIENT_NET as e:
                    log.warning(
                        "Decision tick skipped on a transient network error (%s); "
                        "retrying next tick.", e.__class__.__name__,
                    )
                except Exception:
                    log.exception("Decision tick failed; continuing.")
                # Wake promptly on shutdown; otherwise tick on the monitor cadence.
                self._stop.wait(self.cfg.monitor_interval_s)
        except KeyboardInterrupt:
            log.info("Interrupted — shutting down.")
        finally:
            self._stop.set()
            wd_thread.join(timeout=self.cfg.monitor_interval_s + 5)
            # Drain any in-flight page before exiting (async alert worker).
            try:
                self.alerter.flush()
            except Exception:  # noqa: BLE001 — shutdown best-effort
                pass

    def _watchdog_loop(self) -> None:
        """Independent safety loop: closing positions is never gated, so this runs
        regardless of the kill switch or what the decision thread is doing."""
        while not self._stop.is_set():
            try:
                self._note_loop_tick()
                self._maybe_warn_on_battery()
                with self._trade_lock:
                    self.watchdog.check_once()
                # OUTSIDE the lock: _ensure_core_stop acquires _trade_lock
                # itself, and the lock is a plain (non-reentrant) Lock — calling
                # this from inside check_once would self-deadlock the safety
                # loop. The get_account read also stays off the trade lock.
                self._retry_core_stop()
                self._maybe_heartbeat()
                self._watchdog_skips = 0   # a clean tick clears the run
            except _TRANSIENT_NET as e:
                self._watchdog_skips += 1
                log.warning(
                    "Watchdog tick skipped on a transient network error (%s); "
                    "retrying next tick (%d in a row).",
                    e.__class__.__name__, self._watchdog_skips,
                )
                self._maybe_page_on_skip_run()
            except Exception:
                log.exception("Watchdog tick failed; continuing.")
            self._stop.wait(self.cfg.monitor_interval_s)

    #: consecutive watchdog skips (network) before we page the safety loop is blind.
    WATCHDOG_SKIP_ESCALATE = 5

    def _maybe_page_on_skip_run(self) -> None:
        """Page when the watchdog has skipped WATCHDOG_SKIP_ESCALATE ticks in a
        row on network errors during market hours — the safety loop can't see the
        book. Throttled by the alerter's dark_gap-style key; only fires in-hours
        (an overnight outage strands nothing). Best-effort."""
        if self._watchdog_skips < self.WATCHDOG_SKIP_ESCALATE:
            return
        now = time.time()
        if not self._overlaps_paging_hours(now, now):
            return
        secs = self._watchdog_skips * self.cfg.monitor_interval_s
        log.critical(
            "Watchdog BLIND: %d consecutive ticks failed (~%.0fs) — positions "
            "unwatched during market hours.", self._watchdog_skips, secs,
        )
        self.alerter.critical(
            "watchdog_blind",
            f"Watchdog blind for {self._watchdog_skips} ticks (~{secs:.0f}s)",
            "The safety loop has failed to read the account for several ticks in "
            "a row (network). Stops/floor/flatten can't fire while it's blind. "
            "Check the host's connectivity.",
            severity=float(self._watchdog_skips),
        )

    #: page at most once per this window about running on battery in-hours.
    BATTERY_WARN_COOLDOWN_S = 1800.0

    def _maybe_warn_on_battery(self) -> None:
        """Proactively page when the host is on BATTERY during market hours.
        `caffeinate` can't prevent clamshell/battery sleep (Jul 20: pmset
        'Clamshell Sleep … Using Batt 39%' mapped 1:1 to the dark gaps), so the
        only in-code mitigation is to warn BEFORE the bot goes dark — the
        durable fix is AC power or the always-on host in ops/. macOS-only,
        throttled, best-effort; never raises."""
        if sys.platform != "darwin":
            return
        now = time.time()
        if now - self._last_battery_warn < self.BATTERY_WARN_COOLDOWN_S:
            return
        if not self._overlaps_paging_hours(now, now):
            return  # off-hours: sleeping on battery is fine, don't cry wolf
        if self._on_battery() is not True:
            return
        self._last_battery_warn = now
        log.critical(
            "On BATTERY during market hours — clamshell/battery sleep will blind "
            "the bot (caffeinate cannot prevent it). Plug in AC or move to the "
            "always-on host (ops/)."
        )
        self.alerter.critical(
            "on_battery",
            "Bot on battery during market hours",
            "The host is running on battery while the market is open. macOS will "
            "sleep on lid-close/idle even with caffeinate held, and the bot goes "
            "dark with positions unwatched between watchdog ticks. Plug in AC "
            "power, or move to the always-on host (ops/Dockerfile).",
        )

    @staticmethod
    def _on_battery() -> bool | None:
        """True on battery, False on AC, None if undetermined (pmset missing /
        parse fail). macOS `pmset -g batt` prints 'Now drawing from Battery
        Power' or 'AC Power'."""
        try:
            out = subprocess.run(
                ["pmset", "-g", "batt"], capture_output=True, text=True, timeout=3,
            ).stdout
        except Exception:  # noqa: BLE001 — advisory probe, never raises
            return None
        if "AC Power" in out:
            return False
        if "Battery Power" in out:
            return True
        return None

    def _note_loop_tick(self) -> None:
        """Detect dark gaps. A wall-clock jump much larger than the tick interval
        means the process was suspended (laptop sleep) or the host clock jumped —
        positions moved unwatched, so say so loudly. Runs on the WATCHDOG cadence,
        not the main loop's: the watchdog thread keeps ticking through a
        minutes-long decision cycle, so a slow LLM call can't masquerade as
        darkness (it did when this ran on the main loop — every long cycle fired
        a false CRITICAL). Trading itself needs no special resume path: the
        watchdog's next tick re-checks every position and the decision cycle
        reconciles first."""
        now_wall = time.time()
        gap = now_wall - self._last_wall_tick
        self._last_wall_tick = now_wall
        threshold = self.cfg.monitor_interval_s * 3 + 60
        if gap > threshold:
            # Page only when the gap touched market hours: overnight/weekend
            # laptop sleep left nothing unwatched (prices weren't moving), and
            # 2-4am CRITICAL emails for a sleeping laptop are pure noise
            # (2026-07-14: 10 closed-market dark gaps, 2 pager emails).
            if self._overlaps_paging_hours(now_wall - gap, now_wall):
                log.critical(
                    "DARK GAP: no loop tick for %.0f min (sleep/suspend?). Positions "
                    "were unwatched; reconciling before trading resumes.", gap / 60.0,
                )
                self.alerter.critical(
                    "dark_gap",
                    f"Bot was dark for {gap / 60.0:.0f} min",
                    "The process missed loop ticks (host slept, was suspended, or the "
                    "clock jumped). Position monitoring resumed; the next decision "
                    "cycle reconciles pending orders first. Check the host.",
                    severity=gap / 60.0,   # minutes — a bigger gap out-pages a smaller
                )
            else:
                log.warning(
                    "DARK GAP (market closed): no loop tick for %.0f min "
                    "(sleep/suspend?) — positions could not move; not paging.",
                    gap / 60.0,
                )

    @staticmethod
    def _overlaps_paging_hours(start_ts: float, end_ts: float) -> bool:
        """True when any part of wall-clock [start_ts, end_ts] falls inside the
        weekday 09:25-16:05 ET paging window (the same window ops/deadman.py
        uses). Checked at both endpoints plus each session open inside the
        span, so a multi-day gap can't thread between samples. Pure clock math
        — no network — because this runs on the watchdog thread."""
        et = ZoneInfo("America/New_York")

        def in_window(dt_: datetime) -> bool:
            if dt_.weekday() >= 5:
                return False
            minute = dt_.hour * 60 + dt_.minute
            return (9 * 60 + 25) <= minute <= (16 * 60 + 5)

        start = datetime.fromtimestamp(start_ts, et)
        end = datetime.fromtimestamp(end_ts, et)
        if in_window(start) or in_window(end):
            return True
        day = start.date()
        while day <= end.date():
            session_open = datetime(day.year, day.month, day.day, 9, 30, tzinfo=et)
            if start <= session_open <= end and in_window(session_open):
                return True
            day += timedelta(days=1)
        return False

    def _maybe_heartbeat(self) -> None:
        """Ping the external dead-man monitor — only while the MAIN loop is also
        fresh. Called from the watchdog thread, so a dead/hung decision loop OR a
        dead watchdog both silence the ping and the external monitor pages.

        Freshness is kept by _stamp_liveness at progress points THROUGH the
        decision cycle (a healthy cycle never goes >150s between stamps), so
        withheld here means genuinely stuck, not merely busy. Known exception:
        the LLM call is one opaque SDK call whose timeout+retry worst case
        (~185s) can outlast the gate — accepted, because the resulting outward
        silence ends within ~65s and only a real hang reaches the external
        monitor's period+grace."""
        if not self.cfg.heartbeat_url:
            return
        main_age = time.monotonic() - self._last_main_tick
        if main_age > self.cfg.monitor_interval_s * 3 + 60:
            log.warning(
                "Heartbeat withheld: main loop last ticked %.0fs ago — letting "
                "the external monitor page.", main_age,
            )
            return
        ping_heartbeat(self.cfg.heartbeat_url)

    def _stamp_liveness(self) -> None:
        """Forward-progress stamp for the heartbeat gate. Call from the MAIN
        thread only, right after a unit of cycle work completes — never from
        the watchdog thread and never from an except path, so a wedged network
        read stops the stamps and the external monitor still pages."""
        self._last_main_tick = time.monotonic()
        self._write_tick_stamp()

    def _write_tick_stamp(self) -> None:
        """Write a wall-clock freshness stamp for the LOCAL deadman (state/
        last_tick.stamp). The deadman used to key only on logs/bot.log mtime,
        which the 24/7 watchdog keeps warm even when the MAIN loop is wedged —
        so a stuck decision thread stayed invisible below the ~75-min log-stale
        threshold. This stamp moves only from main-thread forward progress, so a
        wedge goes stale in minutes. Throttled + best-effort."""
        now = time.monotonic()
        if now - getattr(self, "_last_stamp_write", 0.0) < 10.0:
            return
        self._last_stamp_write = now
        try:
            from pathlib import Path
            stamp = Path(self.cfg.state_file).parent / "last_tick.stamp"
            stamp.write_text(f"{time.time():.0f}\n", encoding="utf-8")
        except Exception as e:  # noqa: BLE001 — liveness stamp must never raise
            log.debug("tick-stamp write failed: %s", e)

    def _tick(self) -> None:
        """One monitor-cadence pass of the decision-loop body (extracted from
        run() so the cadence/bell semantics are unit-testable)."""
        # Liveness stamp for the heartbeat gate only. Dark-gap detection lives
        # on the WATCHDOG thread (_note_loop_tick): this loop blocks for
        # minutes inside a decision cycle (LLM + signal fetches), and busy is
        # not dark — the watchdog keeps watching positions the whole time.
        self._last_main_tick = time.monotonic()
        self._write_tick_stamp()   # local deadman freshness (idle ticks too)
        now_wall = time.time()
        wake_gap = now_wall - self._last_main_wall
        self._last_main_wall = now_wall
        self._refresh_runtime_controls()
        if not self._decision_due():
            return
        # Post-wake settle: a wall-clock jump far larger than the tick interval
        # means the host just resumed from sleep. Give the network a moment to
        # reconnect before the cycle's reconcile/reads, so their retry budget
        # isn't burnt while Wi-Fi is still coming up (Jul 18: 18/18 post-wake
        # tick failures in ~1.5s).
        if wake_gap > self.cfg.monitor_interval_s * 3 + 60:
            self._await_network_settle(wake_gap)
        cycle_start = time.time()   # WALL clock — the cadence must survive sleep
        self.run_decision_cycle()
        # Refresh the dashboard while the market is open, plus exactly once on
        # the open->closed transition (the settled end-of-day snapshot). Skip
        # the hourly overnight rebuilds — they repaint byte-identical HTML.
        if self._cycle_market_open or self._dashboard_open_last:
            self._refresh_dashboard()
        self._dashboard_open_last = self._cycle_market_open
        # Stamp the cycle START, not the end: an end stamp adds each cycle's
        # own runtime (~3-4 min of signal fetches + LLM) to the cadence, so
        # ticks drifted later every hour (Jul 13: 10:30 -> 11:34 -> ... ->
        # 15:45 ET). Still stamped only on success — a failed cycle keeps
        # retrying on the 30s monitor tick, as before.
        self._last_decision_at = cycle_start
        # One-shot bell: clear a CONSUMED (past) stash only after a SUCCESSFUL
        # cycle, so an exception at the open keeps it armed and the 30s tick
        # retries at the bell instead of sleeping to the hourly grid. A FUTURE
        # stash (just re-armed by this closed tick) survives.
        if (self._next_open_utc is not None
                and datetime.now(timezone.utc) >= self._next_open_utc):
            self._next_open_utc = None

    def _await_network_settle(self, gap_s: float) -> None:
        """After a resume-from-sleep, wait up to wake_settle_seconds for the
        network to come back before the first decision cycle. Returns as soon as
        a TCP probe succeeds (typically well under the cap). Best-effort and
        interruptible via the stop event; never raises."""
        secs = getattr(self.cfg, "wake_settle_seconds", 0.0)
        if secs <= 0:
            return
        log.info(
            "Resumed after a %.0f-min gap — settling the network (<= %gs) before "
            "the first cycle.", gap_s / 60.0, secs,
        )
        deadline = time.monotonic() + secs
        while time.monotonic() < deadline and not self._stop.is_set():
            if self._network_reachable():
                return
            self._stop.wait(2.0)

    @staticmethod
    def _network_reachable() -> bool:
        """A DNS-free TCP reachability probe (Cloudflare 1.1.1.1:443, then
        Google DNS 8.8.8.8:53). True if either connects — 'the internet is
        back', without depending on the broker host resolving yet."""
        import socket
        for host, port in (("1.1.1.1", 443), ("8.8.8.8", 53)):
            try:
                socket.create_connection((host, port), timeout=3).close()
                return True
            except OSError:
                continue
        return False

    def _within_close_fence(self) -> bool:
        """True when we're inside close_fence_minutes of the session close, so a
        fresh decision cycle should be skipped. Fail OPEN (return False) on a
        missing/failed close-time read — an unknown close must not silently
        freeze trading. Best-effort; never raises."""
        fence = getattr(self.cfg, "close_fence_minutes", 0.0)
        if fence <= 0:
            return False
        try:
            close_at = self.broker.next_market_close()
            if close_at is None:
                return False
            mins = (close_at - datetime.now(timezone.utc)).total_seconds() / 60.0
            if 0.0 <= mins <= fence:
                log.info(
                    "Within %.1f min of the close (<= %g-min fence) — skipping new "
                    "decisions; positions stay watchdog-protected.", mins, fence,
                )
                return True
        except Exception as e:  # noqa: BLE001 — fence is best-effort
            log.warning("close-fence check failed (%s); proceeding.", e)
        return False

    def _decision_due(self) -> bool:
        if (time.time() - self._last_decision_at) >= self.cfg.decision_interval_s:
            return True
        # The hourly grid rarely lands on the bell: when the last closed-market
        # tick stashed the next session open, fire at that moment too instead
        # of sleeping into the first (usually busiest) hour of the session.
        if self._next_open_utc is not None:
            return datetime.now(timezone.utc) >= self._next_open_utc
        return False

    def _warn_on_weak_safety_config(self) -> None:
        """Loudly flag safety nets that are disabled, so an off-by-default setting
        isn't mistaken for a configured-and-safe one."""
        if self.cfg.risk.equity_floor_pct <= 0:
            log.warning(
                "EQUITY_FLOOR_PCT is 0 (off) — the latched liquidate-and-halt "
                "catastrophe guard is DISABLED. Set it before trading real size."
            )
        if self.cfg.robinhood_enabled and self.cfg.robinhood_mcp_token:
            log.warning(
                "Robinhood MCP enabled — we call READ tools only, but a "
                "trade-capable token could place orders if misused. Use a "
                "read-scoped token."
            )
        hb = (self.cfg.heartbeat_url or "").strip()
        if not hb:
            log.warning(
                "HEARTBEAT_URL is empty — laptop-dead paging is DISABLED. The "
                "deadman launchd job only covers 'bot died while the laptop is "
                "up'; for the other half, create a free healthchecks.io check "
                "and put its ping URL (https://hc-ping.com/<uuid>) in "
                "HEARTBEAT_URL."
            )
        elif any(h in hb for h in ("localhost", "127.0.0.1", "0.0.0.0")):
            log.warning(
                "HEARTBEAT_URL points at THIS machine (%s) — a self-ping can't "
                "page when the laptop dies. It must be an EXTERNAL monitor's "
                "ping URL (healthchecks.io: https://hc-ping.com/<uuid>), not "
                "the dashboard or the control panel.", hb,
            )
        # Tiny-float sanity: if the per-name budget after the position cap can't
        # clear the min order, the bot can never fill MAX_OPEN_POSITIONS slots and
        # will sit in cash. Surface it once at startup rather than silently.
        try:
            account = self.broker.get_account()
            r = self.cfg.risk
            per_name_budget = account.equity * (r.max_position_pct / 100.0)
            if r.fractional_enabled and per_name_budget < r.min_order_usd:
                log.warning(
                    "Tiny float: %.0f%% position cap on $%.0f equity = $%.2f/name, "
                    "below the $%.2f min order — no buys will size. Lower "
                    "MIN_ORDER_USD or raise MAX_POSITION_PCT for this account.",
                    r.max_position_pct, account.equity, per_name_budget, r.min_order_usd,
                )
        except Exception:  # noqa: BLE001 — a startup advisory must never crash boot
            log.debug("Tiny-float config check skipped (account unavailable).")

    def _refresh_runtime_controls(self) -> None:
        """Let an operator halt NEW buys WITHOUT a restart by creating the
        kill-switch file. Startup KILL_SWITCH stays in effect regardless."""
        file_kill = os.path.exists(self.cfg.kill_switch_file)
        desired = self.cfg.kill_switch or file_kill or self._forced_halt
        if desired != self.risk.kill_switch:
            log.warning(
                "Kill switch -> %s (file=%s).", "ON" if desired else "off",
                self.cfg.kill_switch_file if file_kill else "n/a",
            )
        self.risk.kill_switch = desired

    # -- the slow cycle ----------------------------------------------------- #
    def _maybe_run_postmortem(self) -> None:
        """Fire the nightly post-mortem once per ET trading day, on the first
        market-closed tick after a day that has journal records."""
        if not self.cfg.postmortem_enabled:
            return
        try:
            from .journal import _trading_day as _tj
            day = _tj()
            if day == self.state.get_postmortem_done_day():
                return  # already ran today
            if not self.journal.has_records_today():
                return  # no decisions today (e.g. weekend restart)
            log.info("Running nightly post-mortem for %s …", day)
            from .postmortem import run_postmortem
            result = run_postmortem(self.cfg, self.ledger, self.journal, day,
                                    max_lessons=self.cfg.postmortem_max_lessons)
            # Latch only on success: latching a failed run marked the day done
            # and permanently skipped it (Jul 7 + Jul 13 post-mortems were lost
            # this way). A None result retries on the next closed tick.
            if result is not None:
                self.state.set_postmortem_done(day)
            from .usage import summarize_day
            calls, in_tok, out_tok, cost = summarize_day()
            log.info(
                "API spend today: $%.2f across %d calls (%s in / %s out tokens).",
                cost, calls, f"{in_tok:,}", f"{out_tok:,}",
            )
        except Exception as e:
            log.warning("Nightly post-mortem failed: %s", e)

    def _maybe_run_weekly_autotune(self) -> None:
        """Fire the deterministic ledger-driven auto-tune report once per ET
        week, on a market-closed tick that falls on the weekend — clone of
        _maybe_run_postmortem's once-per-day latch, at week granularity."""
        if not self.cfg.autotune_enabled:
            return
        try:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            now_et = datetime.now(ZoneInfo("America/New_York"))
            if now_et.weekday() < 5:  # Mon-Fri: wait for the weekend
                return
            iso_year, iso_week, _ = now_et.isocalendar()
            week = f"{iso_year}-W{iso_week:02d}"
            if week == self.state.get_autotune_done_week():
                return  # already ran this week
            log.info("Running weekly auto-tune report for %s …", week)
            from .autotune import run_autotune
            result = run_autotune(
                self.cfg, self.ledger, self.journal,
                days=self.cfg.autotune_days, min_sample=self.cfg.autotune_min_sample,
            )
            # Latch only on success (same lost-run lesson as the post-mortem):
            # a None result — no data, or a mid-run failure — retries on the
            # next closed weekend tick instead of being silently skipped for
            # the rest of the week.
            if result is not None:
                self.state.set_autotune_done(week)
        except Exception as e:
            log.warning("Weekly auto-tune failed: %s", e)

    def run_decision_cycle(self) -> None:
        self._cycle_market_open = self.broker.is_market_open()
        if not self._cycle_market_open:
            log.info("Market closed; skipping decision cycle.")
            self._refresh_closing_snapshot()
            self._maybe_run_postmortem()
            self._maybe_run_weekly_autotune()
            self._stamp_liveness()  # the postmortem's LLM call can run ~2 min
            # Arm the at-the-bell wake-up (see _decision_due; _tick clears it
            # only after the first SUCCESSFUL open-market cycle consumes it).
            self._next_open_utc = self.broker.next_market_open()
            return

        self._reconcile_fills()
        self._backfill_exchange_exits_locked()
        self._stamp_liveness()
        # Close fence (CRITICAL-1): reconcile/backfill above still run near the
        # bell, but don't START a fresh decision inside the final N minutes — a
        # buy placed this late can't complete before close, and the model
        # churning right before the bell is low-value. Positions stay
        # watchdog-protected; the post-LLM re-check below catches a close that
        # lands mid-cycle.
        if self._within_close_fence():
            return
        # Fresh Quiver data this cycle, but pulled once and shared by the signal
        # and screener layers (both read the same cached live feeds).
        self.quiver.new_cycle()
        self.earnings.new_cycle()
        self.sectors.new_cycle()
        self.corr_guard.new_cycle()
        self.regime.new_cycle()
        if self.cfg.risk.regime_filter_enabled:
            regime = self.regime.assess()
            self._regime_mult = regime.multiplier
            self._regime_trend = regime.trend
            self._regime_label = regime.label
            # A degraded ("unknown") read means we're flying blind on BOTH regime
            # and the sector cap — surface that at WARNING, not INFO (1B.7).
            if regime.label == "unknown":
                log.warning("Market regime: %s", regime.reason)
            else:
                log.info("Market regime: %s", regime.reason)
        else:
            self._regime_mult = 1.0
            self._regime_trend = ""
            self._regime_label = ""
        self._record_equity_snapshot()
        account = self.broker.get_account()

        # De-risk the EXISTING book on a flip into risk-off (the regime multiplier
        # otherwise only shrinks NEW buys). Runs before new proposals so the trimmed
        # snapshot is what the buy path sizes against.
        if self.cfg.risk.regime_filter_enabled:
            self._apply_regime_trim(account, self.regime.assess())
        # Falling-tape core defense (Jul 29: the QQQ core is pure beta — it ate
        # -$1.8k while the DCA fill kept BUYING the decline). On a falling read
        # (risk-off label, long-run downtrend, or an intraday benchmark drop),
        # trim the core once per day and pause the core fill for the cycle.
        self._apply_core_defense(account)
        # Deterministic inverse-ETF hedge + defensive-core rotation (Jul 30
        # review): the Jul-29 index-put sanction is model-discretionary and
        # has fired zero times — this pair is the system acting on its own
        # falling read. Runs right after the core defense so both share the
        # same per-cycle read and the hedge sizes against the trimmed book.
        self._apply_auto_hedge(account)
        self._apply_defensive_rotation(account)

        # Watchlist + current holdings are always evaluated; the scanner widens
        # this with NEW smart-money names so buy ideas can originate from the
        # market, not just a hand-typed list. Everything still flows through the
        # same signals -> decide -> risk path below.
        # Option rows carry OCC contract symbols — signal providers and
        # screeners must see the UNDERLYING (keeps the thesis under review each
        # cycle), never the raw contract symbol.
        held: set[str] = set()
        for p in account.positions:
            if p.is_option:
                occ = parse_occ(p.symbol)
                if occ:
                    held.add(occ[0])
            else:
                held.add(p.symbol)
        base = set(self.watchlist) | held
        # System-managed symbols (core ETF, auto-hedge inverse ETF, defensive
        # T-bill core) are opened and closed by the orchestrator, not by
        # Claude — drop them from the decision slate so the model doesn't
        # churn them (buy/sell/thesis-decay); they're passive allocations.
        base -= self._system_managed_symbols()
        discovered = self.screeners.scan(exclude=base) if self.cfg.screener.enabled else []
        symbols = sorted(base | {c.symbol for c in discovered})
        self._stamp_liveness()

        # Signal gathering is the cycle's longest stretch (3+ min across ~10
        # providers); per-provider progress stamps keep the heartbeat gate
        # from mistaking busy for hung.
        bundles = self.signals.gather(symbols, on_progress=self._stamp_liveness)
        self._inject_discovery(bundles, discovered)

        # Persist this cycle's scores and build the freshness/trend annotations
        # (E.1+R.4). Recorded BEFORE thesis-decay exits so a decaying series is
        # remembered even for names we exit this cycle. Best-effort: history
        # must never block a decision.
        signal_notes: dict[str, dict[str, str]] = {}
        try:
            self.signal_history.record(bundles)
            signal_notes = self.signal_history.notes_for(bundles)
        except Exception as e:
            log.warning("Signal history unavailable this cycle: %s", e)

        # Deterministic thesis-decay exits (1B.4b): sell held names whose fresh
        # signals no longer corroborate the entry thesis, BEFORE asking Claude — so
        # a stale-thesis name is recycled even if the LLM is down, and we don't
        # spend tokens deciding on a name we've already exited.
        decayed = self._apply_thesis_decay_exits(bundles, account)
        if decayed:
            bundles = [b for b in bundles if b.symbol not in decayed]

        # Deterministic weighted signal index ("composite"): per-kind mean
        # score x freshness-lag weight x realized track-record weight. Rendered
        # as a per-candidate anchor in the prompt, blended into the cycle
        # budget split, and (opt-in) a risk floor. Best-effort — the composite
        # is context, never a required feed.
        composites: dict[str, float] = {}
        if self.cfg.risk.composite_enabled:
            try:
                pw = perf_weights(
                    self.ledger, self.cfg.risk.composite_perf_min_trips
                )
                for b in bundles:
                    b.composite_score = composite_score(b, pw)
                composites = {
                    b.symbol: b.composite_score
                    for b in bundles if b.composite_score is not None
                }
                if composites:
                    top = sorted(
                        composites.items(), key=lambda kv: kv[1], reverse=True
                    )[:8]
                    log.info(
                        "Composite index (%d scored, top: %s).",
                        len(composites),
                        ", ".join(f"{s} {v:+.2f}" for s, v in top),
                    )
            except Exception as e:
                log.warning("Composite index unavailable this cycle: %s", e)

        # Technical context (RSI / extension over the 20d SMA) per symbol for
        # the risk layer's anti-chasing gate. Missing entries fail open there.
        tech_ctx: dict[str, dict] = {
            b.symbol: s.data
            for b in bundles
            for s in b.signals
            if s.kind is SignalKind.TECHNICAL and s.data
        }

        # Expectancy gate input (Jul 30 review): signal families whose CITED
        # trailing realized expectancy is negative. Recomputed once per cycle
        # from the ledger; the risk layer blocks FRESH entries whose thesis
        # rests entirely on these families. Best-effort — {} disarms the gate.
        self._neg_families = {}
        if self.cfg.risk.expectancy_gate_enabled:
            self._neg_families = negative_expectancy_families(
                self.ledger,
                window_days=self.cfg.risk.expectancy_gate_window_days,
                min_trips=self.cfg.risk.expectancy_gate_min_trips,
            )
            if self._neg_families:
                log.info(
                    "Expectancy gate armed against: %s",
                    ", ".join(
                        f"{k} {v.avg_pl_pct:+.1f}%/trip x{v.trips}"
                        for k, v in sorted(self._neg_families.items())
                    ),
                )

        bench_stats = self.benchmark.compute()
        bench_line = self.benchmark.context_line(bench_stats)
        external = self.robinhood.holdings()
        data_health = self._check_robinhood_health()
        # Reflection loop: our realized P&L per entry signal, fed back so Claude can
        # weight by what has actually paid off. Best-effort; never blocks a cycle.
        # Split dynamic (per-cycle) from stable (once-a-day) so the latter can
        # sit in the cached half of the decision prompt (engine._render_stable).
        lessons = self._attribution_lessons()
        curated = self._curated_lessons()

        # The signal kinds present per symbol at decision time — recorded on each
        # entry so closed round-trips can later be attributed back to their sources.
        signal_kinds = {
            b.symbol: sorted({s.kind.value for s in b.signals}) for b in bundles
        }

        # Prompt-time slate filtering: compute per-symbol buy headroom and tell
        # Claude which symbols are at-cap (A2). Drops fully-blocked not-held names;
        # keeps held-but-blocked names for SELL/HOLD evaluation.
        bundles, buy_excluded = self._partition_slate(bundles, account)

        # Journal slate exclusions so they appear in 'Today so far' and the nightly
        # post-mortem can identify wasted proposal slots.
        for sym, reason in buy_excluded.items():
            self._journal_decision(sym, "buy", "equity", 0.0, 0.0, "slate_excluded",
                                   0.0, reason, "")

        # 'Today so far' block: what Claude has already done this session (trusted).
        today_block = ""
        try:
            today_block = self.journal.render_today(
                account.equity,
                options_on=bool(self.cfg.risk.options_enabled),
            )
        except Exception as e:
            log.warning("Could not render today block: %s", e)

        self._stamp_liveness()
        # Surface the deterministic regime to the model so it can express the
        # DOWNSIDE with a defined-risk put when the market turns risk-off (assess()
        # is cached per-cycle). Without this the model never sees the risk-off
        # state and a long-only book just bleeds through a decline.
        _reg = self.regime.assess() if self.cfg.risk.regime_filter_enabled else None
        # Falling-market INDEX-put sanction (Jul 29): when the deterministic
        # falling read fires and options are on, tell the model it may buy ONE
        # defined-risk put on the index itself — profit from the fall, not
        # just less bleed. The direction gate reads it as core insurance.
        hedge_symbol, hedge_price, hedge_reason = "", None, ""
        if self.options is not None:
            falling, why = self._market_falling()
            if falling:
                hedge_symbol = self.cfg.core_etf or self.cfg.benchmark_symbol
                hedge_reason = why
                if hedge_symbol:
                    try:
                        hedge_price = self.broker.latest_price(hedge_symbol)
                    except Exception:
                        hedge_price = None
        self._hedge_symbol = hedge_symbol
        proposals = self.engine.decide(
            bundles, account, bench_line, external, lessons,
            today=today_block, buy_excluded=buy_excluded,
            signal_notes=signal_notes, held_notes=self._held_notes(account),
            data_health=data_health, composites=composites,
            regime_label=(_reg.label if _reg else ""),
            regime_reason=(_reg.reason if _reg else ""),
            regime_trend=(_reg.trend if _reg else ""),
            curated=curated,
            hedge_symbol=hedge_symbol, hedge_price=hedge_price,
            hedge_reason=hedge_reason,
        )
        self._stamp_liveness()
        proposals = self._filter_to_slate(
            proposals, bundles, account,
            extra={hedge_symbol} if hedge_symbol else None,
        )
        # Hard backstop: Claude may still propose an excluded BUY; drop it.
        dropped = set()
        proposals_before = proposals
        proposals = self._drop_excluded_buys(proposals, buy_excluded)
        for prop in proposals_before:
            if prop not in proposals:
                dropped.add(prop.symbol)
                self._journal_decision(
                    prop.symbol, prop.action.value,
                    prop.instrument.value if hasattr(prop.instrument, "value") else str(prop.instrument),
                    prop.conviction, prop.target_weight_pct, "dropped_buy",
                    0.0, buy_excluded.get(prop.symbol, "excluded from slate"),
                    prop.rationale[:120] if prop.rationale else "",
                )
        # Post-LLM close fence: signal gathering + the LLM span minutes, so the
        # bell can ring mid-cycle. Executing proposals after the close places
        # after-hours orders (Jul 20: 5 proposals returned 41 min past close).
        # Discard and journal them; positions stay watchdog-protected. Sells are
        # kept — reducing risk is always allowed, even after hours.
        if self.cfg.close_fence_minutes > 0 and not self.broker.is_market_open():
            kept_sells = [p for p in proposals if p.action.value == "sell"]
            for p in proposals:
                if p.action.value != "sell":
                    self._journal_decision(
                        p.symbol, p.action.value,
                        p.instrument.value if hasattr(p.instrument, "value") else str(p.instrument),
                        p.conviction, p.target_weight_pct, "rejected", 0.0,
                        "Market closed mid-cycle — proposal discarded (close fence).",
                        p.rationale[:120] if p.rationale else "",
                    )
            if len(kept_sells) != len(proposals):
                log.warning(
                    "Market closed during the cycle — discarded %d non-sell "
                    "proposal(s) past the bell; keeping %d risk-reducing sell(s).",
                    len(proposals) - len(kept_sells), len(kept_sells),
                )
            proposals = kept_sells
        undeployed = self._execute_proposals(
            proposals, account, signal_kinds, tech_ctx=tech_ctx,
            composites=composites, bundles=bundles,
        )
        # Bearish funnel (Jul 30 review): the put path was dormant for 388
        # straight trades and nothing surfaced it. One line per cycle makes
        # downside-conviction leakage visible: how many slate names carried a
        # real bearish read, how many puts the model proposed, how many the
        # gates passed, and whether the deterministic hedge sleeve is on.
        bear_bar = self.cfg.screener.bearish_reserve_bar or 0.4
        bear_slate = sum(1 for v in composites.values() if v <= -bear_bar)
        h_etf = getattr(self.cfg, "hedge_etf", "")
        hedge_pos = account.position_for(h_etf) if h_etf else None
        log.info(
            "BEARISH FUNNEL: slate_bearish=%d put_proposals=%d put_approved=%d "
            "auto_hedge=%s",
            bear_slate,
            getattr(self, "_bear_puts_proposed", 0),
            getattr(self, "_bear_puts_approved", 0),
            (
                f"${max(0.0, hedge_pos.market_value):,.0f} {h_etf}"
                if hedge_pos is not None else
                ("armed" if self._falling_cycles > 0 and h_etf
                 else ("off" if not h_etf else "flat"))
            ),
        )
        if undeployed >= 1.0:
            # Whole-share bracket flooring drops each buy's sub-share remainder
            # (deliberate — the exchange-resident bracket wins over precision);
            # total it here so the drag is visible instead of silent cash.
            log.info(
                "Cycle budget not fully deployed: $%.2f dropped by whole-share "
                "flooring across this cycle's buys (stays cash; core sweep / "
                "next cycle can redeploy).", undeployed,
            )
        self._stamp_liveness()
        # Core-satellite fill (Todo 1.6): deploy whatever cash the single-name book
        # left idle into the broad core ETF, so we're not structurally short the
        # benchmark. Runs EVEN when there were no proposals — that's exactly the
        # cash-drag case it exists to fix.
        self._apply_core_fill(account)
        # GA-2.3: keep the core's exchange-resident GTC stop sized to the
        # (growing) position — the core previously had NO exchange-side stop.
        self._ensure_core_stop(account)
        # Persist this cycle's freshly-submitted order ids so the next boot (even
        # after a crash between cycles) reconciles their fills (1B.9). MERGE, not
        # overwrite: the watchdog queues its exit orders into state DURING the
        # minutes-long cycle, and a plain overwrite silently dropped them.
        self.state.merge_pending_orders(self._pending_oids)

    def _check_robinhood_health(self) -> list[str]:
        """Page ONCE per RH dead-auth latch event and return the DATA HEALTH
        notes for the decision prompt — so Claude can tell "RH says nothing"
        apart from "RH is dead" instead of the signals silently vanishing."""
        if not self.cfg.robinhood_enabled:
            return []
        since = RobinhoodReader.auth_dead_since()
        if since is None:
            return []
        if since != self._rh_paged_for:
            self._rh_paged_for = since
            self.alerter.critical(
                "robinhood_auth",
                "Robinhood OAuth dead — context reads disabled",
                "The RH refresh token is dead; external holdings, the RH "
                "movers/scan screeners and the RH earnings calendar (yfinance "
                "fallback active) are offline. Trading continues on Alpaca. "
                "Fix: run `python -m investment_strategy.portfolio."
                "robinhood_auth login` on the host — reads auto-resume within "
                "~1 minute of the new token landing (no restart needed).",
            )
        return [
            "Robinhood data unavailable (OAuth expired): external holdings, RH "
            "movers/scans and the RH earnings calendar are missing this cycle "
            "— their absence is an outage, not a neutral signal."
        ]

    def _refresh_dashboard(self) -> None:
        """Regenerate the live dashboard HTML — and the public track-record page
        (GA-1.2) — after a cycle so both stay fresh (each off unless its file is
        set). Best-effort; never blocks."""
        if self.cfg.dashboard_file:
            try:
                from pathlib import Path

                from .dashboard import generate
                generate(Path(self.cfg.dashboard_file), live=True)
            except Exception as e:
                log.warning("Dashboard refresh failed: %s", e)
        if self.cfg.track_record_file:
            try:
                from pathlib import Path

                from .track_record import generate as generate_track_record
                generate_track_record(Path(self.cfg.track_record_file), live=True)
            except Exception as e:
                log.warning("Track-record refresh failed: %s", e)

    def _sector_context(self, symbol: str, account) -> tuple[str | None, float]:
        """(sector of `symbol`, $ already held in that sector) for the risk
        sector-concentration cap. Best-effort — a lookup miss returns (None, 0)
        so the cap is simply skipped for that name."""
        try:
            sector = self.sectors.sector_for(symbol)
            if not sector:
                return None, 0.0
            # Option rows are skipped: sector_for(OCC) is meaningless and each
            # premium is capped at ~1% of equity — immaterial to the sector cap.
            held = {p.symbol: p.market_value
                    for p in account.positions if not p.is_option}
            return sector, self.sectors.exposure_by_sector(held).get(sector, 0.0)
        except Exception as e:
            log.warning("sector context for %s failed: %s", symbol, e)
            return None, 0.0

    def _corr_context(self, symbol: str, account) -> tuple[float | None, str, bool]:
        """(max_corr, corr_symbol, data_missing) for the pairwise-correlation
        guard (R.2). Exclusions: the candidate itself (corr=1 would block adds)
        and the core ETF (satellites are meant to track it). data_missing=True
        when we DO hold comparable satellites but couldn't compute correlations
        (data outage) — the risk gate sizes down instead of failing open."""
        try:
            held = [
                p.symbol for p in account.positions
                if not p.is_option and p.symbol != symbol
                and not (self.cfg.core_etf and p.symbol == self.cfg.core_etf)
            ]
            if not held:
                return None, "", False
            best = self.corr_guard.max_correlation(symbol, held)
            if best is None:
                return None, "", True  # held satellites but no data — blind guard
            return best[0], best[1], False
        except Exception as e:  # advisory context — never blocks a cycle
            log.warning("correlation context for %s failed: %s", symbol, e)
            held = [
                p.symbol for p in account.positions
                if not p.is_option and p.symbol != symbol
                and not (self.cfg.core_etf and p.symbol == self.cfg.core_etf)
            ]
            return None, "", bool(held)

    def _record_equity_snapshot(self) -> None:
        """Persist a once-per-day account P&L snapshot (true total return from the
        Alpaca account, not the ledger). Best-effort; never blocks a cycle."""
        try:
            self.equity_history.snapshot(compute_status(self.broker))
        except Exception as e:
            log.warning("Could not record equity snapshot: %s", e)

    def _refresh_closing_snapshot(self) -> None:
        """Market-closed tick: overwrite today's equity row with the TRUE
        post-close read. The in-session snapshot lands whenever the last open
        cycle ran (Jul 28: stamped 46 min before the bell, overstating the
        close by $498 — every day-P&L forensic then reconciles against a wrong
        baseline). Only refreshes a row that already exists for today's UTC
        date, so a post-midnight tick can't mint a phantom next-day row."""
        try:
            rows = self.equity_history.all()
            today = datetime.now(timezone.utc).date().isoformat()
            if rows and rows[-1].get("date") == today:
                self.equity_history.snapshot(compute_status(self.broker))
        except Exception as e:
            log.warning("Closing equity snapshot failed: %s", e)

    def _attribution_lessons(self) -> str:
        """Per-cycle track-record block (attribution.py) — recomputed every
        cycle and changes whenever a position closes, so it stays in the
        decision prompt's DYNAMIC half (see engine._render_dynamic)."""
        try:
            return render_lessons(self.ledger)
        except Exception as e:
            log.warning("Could not render track-record lessons: %s", e)
            return ""

    def _curated_lessons(self) -> str:
        """Nightly post-mortem's curated lessons file — changes at most once a
        day, so it belongs in the decision prompt's STABLE/cached half (see
        engine._render_stable), separate from _attribution_lessons above."""
        try:
            from .postmortem import read_curated
            return read_curated(self.cfg.postmortem_max_lessons)
        except Exception:
            return ""  # postmortem module may not exist yet; silently skip

    def _inject_discovery(
        self, bundles: list[SignalBundle], discovered: list[Candidate]
    ) -> None:
        """Attach each scanner candidate's 'why' as a leading DISCOVERY signal so
        Claude sees why a name surfaced. Names that gathered no other signals get
        a fresh bundle (gather drops empty ones) so they're still evaluated."""
        if not discovered:
            return
        by_symbol = {b.symbol: b for b in bundles}
        context = bundles[0].market_context if bundles else []
        for cand in discovered:
            bundle = by_symbol.get(cand.symbol)
            if bundle is None:
                bundle = SignalBundle(
                    symbol=cand.symbol, signals=[], market_context=context
                )
                by_symbol[cand.symbol] = bundle
                bundles.append(bundle)
            bundle.signals.insert(0, cand.to_signal())

    # -- fill reconciliation ------------------------------------------------ #
    #: how many reconcile passes an oid may stay unresolved before we stop
    #: re-queuing it and escalate (halt + page) — a mandatory retry bound so a
    #: permanently unreadable/stuck order neither loops forever nor drops silently.
    MAX_UNRESOLVED_RETRIES = 5

    def _requeue_unresolved(self, oid: str, symbol: str, why: str) -> bool:
        """Re-queue an oid whose fate we couldn't confirm this pass (broker read
        failed, or still non-terminal). Returns True when the retry bound is
        EXHAUSTED — the caller then treats it as a confirmed mismatch (halt +
        page) so a human reconciles it against the broker. Never drops it
        silently (the SPCX/LPLA phantom-row class)."""
        n = self._oid_retries.get(oid, 0) + 1
        if n <= self.MAX_UNRESOLVED_RETRIES:
            self._oid_retries[oid] = n
            self._pending_oids.append((oid, symbol))
            # Persist the requeue NOW (not just at cycle-end merge): the
            # watchdog's vanished-position sweep spares symbols with a pending
            # order in STATE — during reconcile's drain window an in-memory-
            # only requeue left a still-working buy's fresh clocks sweepable.
            self.state.add_pending_order(oid, symbol)
            log.warning(
                "Order %s (%s) %s — unresolved, re-queued (%d/%d).",
                oid, symbol, why, n, self.MAX_UNRESOLVED_RETRIES,
            )
            return False
        self._oid_retries.pop(oid, None)
        log.error(
            "Order %s (%s) %s after %d reconcile passes — giving up and flagging "
            "a divergence; verify it against the broker.",
            oid, symbol, why, self.MAX_UNRESOLVED_RETRIES,
        )
        return True

    def _reconcile_fills(self) -> None:
        """Confirm last cycle's orders actually filled. A recorded order id is only
        an intent — rejects and partial fills mean the ledger and our risk picture
        have DIVERGED from the broker's reality. That used to be log-only
        (advisory); now it's enforcing (goGA GA-2.1): a confirmed divergence halts
        NEW buys via the kill-switch file until a human deletes the file to
        acknowledge. Sells and the watchdog are never gated — closing out of a
        mis-booked position is exactly what we still want to work."""
        # Check the union of this process's list and the persisted one: the
        # watchdog queues its exit orders straight into state (add_pending_order)
        # from its own thread, so state can hold oids this list has never seen.
        # drain_pending_orders clears the persisted list atomically — these are
        # about to be checked, so a crash mid-reconcile must not re-examine (or
        # re-strand) them next boot.
        drained = self.state.drain_pending_orders()
        seen_oids: set[str] = set()
        pending: list[tuple[str, str]] = []
        for oid, symbol in list(self._pending_oids) + drained:
            if oid not in seen_oids:  # boot loads state into _pending_oids; dedupe
                seen_oids.add(oid)
                pending.append((oid, symbol))
        self._pending_oids = []
        mismatches: list[str] = []
        for oid, symbol in pending:
            status, filled, qty = self.broker.order_fill(oid)
            # EXIT-side intents (watchdog stops/takes/flattens/option closes)
            # self-heal: the watchdog resubmits every tick until the position
            # is gone, and the correction below trues up the ledger. Their
            # terminal-unfilled outcomes must not escalate to the all-buys
            # halt — an option DAY close expiring at the bell is a routine
            # overnight pattern, not a ledger/broker divergence. Entry-side
            # intents (the GA-2.1 phantom-BUY class) still halt.
            is_exit = self.state.exit_was_ledgered(symbol, oid)
            if status == "filled":
                log.info("Order %s (%s) FILLED (%g/%g).", oid, symbol, filled, qty)
                self._oid_retries.pop(oid, None)
                continue
            if status == "replaced":
                # A watchdog exit superseded by a later re-replace: its ledger
                # record was already corrected at replace time (the replacement
                # order carries the exit, and is itself in this list).
                self._oid_retries.pop(oid, None)
                continue
            if status in ("rejected", "canceled", "expired"):
                log.error(
                    "Order %s (%s) ended %s with %g/%g filled — ledger records an "
                    "intent that did not (fully) execute.", oid, symbol, status, filled, qty,
                )
                # CORRECT the ledger (GA-2.5): append a correction pointing at the
                # original record so effective() voids a zero-fill intent or
                # resizes a partial — no more phantom BUY rows.
                self.ledger.record(TradeRecord.correction(oid, symbol, status, filled, qty))
                if not is_exit:
                    mismatches.append(f"{symbol} {status} ({filled:g}/{qty:g} filled)")
                self._oid_retries.pop(oid, None)
            elif qty and 0 < filled < qty:
                log.warning(
                    "Order %s (%s) PARTIAL: %g/%g filled (status=%s).",
                    oid, symbol, filled, qty, status,
                )
                # Non-terminal partial: may still fill more, so no correction yet —
                # re-queue it and let a later reconcile write the final number.
                self._pending_oids.append((oid, symbol))
                if not is_exit:
                    mismatches.append(f"{symbol} partial {filled:g}/{qty:g}")
                self._oid_retries.pop(oid, None)   # partial is progress, not a stall
            elif status == "unknown":
                # The broker fetch FAILED (blip/transient) — NOT a confirmation.
                # drain_pending_orders already cleared the persisted copy, so
                # dropping here (the old behavior) left a rejected order caught by
                # a blip with its phantom ledger intent uncorrected forever. Fail
                # CLOSED: re-queue and re-check next cycle, bounded.
                if self._requeue_unresolved(oid, symbol, "unreadable") and not is_exit:
                    mismatches.append(f"{symbol} unresolved (broker read failed)")
            else:  # still new/accepted/pending_new long after submission — may yet
                # fill; re-queue (bounded) instead of DROPPING (the exact path an
                # oid was lost through), and true it up on a later reconcile.
                # An exit resting unfilled all day (a thin option close) is the
                # watchdog's problem, not a buy-halt.
                if self._requeue_unresolved(oid, symbol, f"still {status}") and not is_exit:
                    mismatches.append(f"{symbol} stuck ({status})")
        if self._pending_oids:
            # Re-queued live partials must survive a crash before the cycle's
            # end-of-run persist, or their final fill never gets corrected.
            # MERGE (not overwrite): the watchdog may have queued an exit into
            # state while the fill checks above were running.
            self.state.merge_pending_orders(self._pending_oids)
        if mismatches and self.cfg.reconcile_halt_enabled:
            self._halt_new_buys(
                "reconcile mismatch: " + "; ".join(mismatches),
                "Ledger/broker divergence at reconcile",
                "The ledger records intents that did not execute as recorded: "
                + "; ".join(mismatches)
                + ".\nNew buys are halted (kill-switch file). Verify positions "
                "against the broker, then delete the file to resume: "
                + self.cfg.kill_switch_file,
            )

    def _halt_new_buys(self, reason: str, subject: str, body: str) -> None:
        """Halt new entries via the kill-switch FILE (not just the in-memory
        flag): the file survives restarts, is picked up within one tick by
        _refresh_runtime_controls, and deleting it is the explicit human
        acknowledgment that resumes buying. Closing positions is never gated."""
        log.critical("HALTING NEW BUYS — %s", reason)
        try:
            os.makedirs(os.path.dirname(self.cfg.kill_switch_file) or ".", exist_ok=True)
            with open(self.cfg.kill_switch_file, "a", encoding="utf-8") as f:
                f.write(f"{datetime.now().isoformat()} {reason}\n")
        except OSError as e:
            # Latch in memory so the halt still holds this run (survives the
            # per-tick kill-switch recompute; cleared only by restart).
            log.error("Could not write kill-switch file (%s); using in-memory halt.", e)
            self._forced_halt = True
        self.risk.kill_switch = True
        self.alerter.critical("reconcile_halt", subject, body)

    # -- exchange-exit backfill (F.1) ---------------------------------------- #
    def _backfill_exchange_exits_locked(self) -> None:
        """Serialized entry point for the backfill — used by both the decision
        cycle and the watchdog's vanished-position callback. The lock guards the
        ledger read-modify-append against a concurrent double-record; the body
        is already idempotent (order-id keyed), so the lock only prevents a
        rare same-instant duplicate."""
        with self._backfill_lock:
            self._backfill_exchange_exits()

    def _backfill_exchange_exits(self) -> None:
        """Record exits that happened with NO code running: a resting bracket's
        stop or take-profit leg filling at the exchange, or a manual sell in the
        broker UI. Every code path that sells already writes the ledger at
        submit time (decision sells, watchdog stops/takes/flattens, trims,
        scale-outs) — those dedupe away by order id. Whatever filled sell
        remains is exactly the blind spot attribution used to carry: realized
        P&L nobody recorded, silently undercounting exits in the round-trip
        history (and the track record Claude is shown). P&L is realized against
        the FIFO basis of the shares actually sold (lots.py, GA-2.5 — the old
        most-recent-buy-price basis was wrong for multi-lot names); the record
        is stamped with the actual fill time so chronological pairing holds.
        Idempotent (order-id keyed) and best-effort — a failure just retries
        next cycle."""
        try:
            from .lots import build_lot_history, fifo_basis

            closed = self.broker.closed_sell_orders()
            if not closed:
                return
            records = self.ledger.effective()
            known = {r.order_id for r in records if r.order_id}
            # Open FIFO lots after every recorded trade so far; consumed as we
            # backfill (oldest fills first) so multiple exits in one batch each
            # see the lots the earlier ones left behind.
            open_lots, _ = build_lot_history(records)
            for o in sorted(closed, key=lambda x: x["filled_at"] or ""):
                if not o["order_id"] or o["order_id"] in known:
                    continue
                lots = open_lots.get(o["symbol"], [])
                basis, covered = fifo_basis(lots, o["qty"])
                pl_pct = pl = None
                if basis > 0 and o["price"] > 0:
                    pl_pct = (o["price"] / basis - 1.0) * 100.0
                    pl = (o["price"] - basis) * covered
                    # Consume the shares this exit sold so the next backfilled
                    # sell in this batch realizes against the remaining lots.
                    remaining = o["qty"]
                    while remaining > 1e-9 and lots:
                        take = min(lots[0].remaining, remaining)
                        lots[0].remaining -= take
                        remaining -= take
                        if lots[0].remaining <= 1e-9:
                            lots.pop(0)
                # A bracket's stop leg is a STOP order; its take-profit leg is a
                # LIMIT. Anything else filled that we didn't place (market/other)
                # was an outside actor — label it external, don't guess.
                reason = {
                    "stop": "bracket_stop", "stop_limit": "bracket_stop",
                    "trailing_stop": "bracket_stop", "limit": "bracket_take",
                }.get(o["type"], "external")
                ts = None
                if o["filled_at"]:
                    try:
                        ts = datetime.fromisoformat(o["filled_at"])
                    except ValueError:
                        pass
                self.ledger.record(TradeRecord.for_sell(
                    o["symbol"],
                    f"exchange-side exit backfill ({o['type'] or 'unknown'} sell)",
                    o["order_id"], qty=o["qty"],
                    realized_pl_pct=pl_pct, realized_pl=pl,
                    exit_reason=reason, ts=ts, exit_price=o["price"] or None,
                ))
                log.info(
                    "Backfilled exchange exit: %s %g sh @ %.2f (%s -> %s%s).",
                    o["symbol"], o["qty"], o["price"], o["type"] or "?", reason,
                    f", {pl_pct:+.1f}%" if pl_pct is not None else "",
                )
                # An exchange-side exit also starts the re-entry cooldown —
                # stamped at the FILL time and price when known (price feeds the
                # price-aware re-entry guard; realized % feeds the loss streak).
                self.state.register_exit(
                    o["symbol"], when=ts, price=o["price"] or None,
                    pl_pct=pl_pct)
        except Exception as e:  # bookkeeping must never break a decision cycle
            log.warning("Exchange-exit backfill failed: %s", e)

    # -- per-cycle budget fair-share (2026-07-06 all-LLY fix) ---------------- #
    def _cycle_budget_caps(
        self, proposals, account, composites: dict[str, float] | None = None,
    ) -> dict[str, float]:
        """Split the cycle's deployable cash across ALL equity-buy proposals,
        weighted by conviction, and return {symbol: $cap}. Proposals are executed
        in the order Claude returns them, and every hard cap in RiskManager still
        applies — this only stops the FIRST buy from consuming the whole cycle's
        cash and starving every later idea to "Budget $0.00" (the 2026-07-06 log:
        10 LLY top-ups in one day while TSM/TDG/BIIB/T were rejected every
        cycle). Single-buy cycles get no share cap. Decision sells run BEFORE
        this split (_execute_proposals), so capital they free is part of the
        deployable pool — that's what funds a full-book rotation buy; whatever
        the buys leave unused is swept by the core-ETF fill.

        With COMPOSITE_BUDGET_BLEND on, each weight is conviction x the
        deterministic composite index (floored at 0.1 so a thin composite
        shrinks a share rather than zeroing it) — corroborated ideas get more
        of the cycle's cash than the LLM's say-so alone."""
        buys = [
            p for p in proposals
            if p.action is Action.BUY and p.instrument is not Instrument.OPTION
        ]
        if len(buys) <= 1:
            return {}
        r = self.cfg.risk
        min_cash = account.equity * (r.min_cash_buffer_pct / 100.0)
        deployable = max(0.0, min(account.cash - min_cash, account.buying_power))
        # Floor each weight so a zero-conviction proposal can't zero-divide and a
        # tiny one still gets a sliver (the risk gate handles the rest).
        weights = {p.symbol: max(p.conviction, 0.05) for p in buys}
        if r.composite_budget_blend and composites:
            weights = {
                sym: w * max(composites[sym], 0.1)
                if sym in composites else w
                for sym, w in weights.items()
            }
        total = sum(weights.values())
        caps = {sym: deployable * w / total for sym, w in weights.items()}
        # Per-symbol share cap: even with many candidates, one name can't sweep
        # >max_cycle_symbol_share_pct% of the cycle's deployable cash. Unused
        # budget is NOT redistributed — the core-ETF fill sweeps what's left.
        share_pct = r.max_cycle_symbol_share_pct
        if 0 < share_pct < 100 and len(buys) >= 2:
            share_cap = deployable * (share_pct / 100.0)
            caps = {sym: min(c, share_cap) for sym, c in caps.items()}
        if deployable > 0:
            log.info(
                "Cycle budget $%s split across %d buy(s): %s.",
                f"{deployable:,.0f}", len(buys),
                ", ".join(f"{s} ${c:,.0f}" for s, c in caps.items()),
            )
        return caps

    # -- held-position context for the prompt (rotation baseline) ----------- #
    def _held_notes(self, account) -> dict[str, str]:
        """Entry conviction + hold age per held equity name, from our own state
        clocks (trusted derived data, not market text). This is the incumbent
        baseline a rotation candidate must beat — without it the model compares
        a fresh candidate's conviction against nothing (postmortem 2026-07-14:
        the CVX 0.46 vs MU 0.63 gap was visible only in our journal). Best-
        effort: a name traded before conviction tracking simply has no note."""
        notes: dict[str, str] = {}
        # Latest entry rationale per symbol (head), so a rotation debate sees WHY
        # each incumbent is held — not just its stale conviction number. The
        # rotation guard enforces a conviction EDGE; showing the thesis lets the
        # model argue against the incumbent's REASONING, which the number can't.
        latest_rationale: dict[str, str] = {}
        try:
            for t in self.ledger.effective():
                if t.action == "buy" and getattr(t, "rationale", ""):
                    latest_rationale[t.symbol] = t.rationale
        except Exception:
            pass
        # The model's own LAST verdict on each held name TODAY (from the
        # decision journal). Jul 29 NU: the 9:25 cycle held a name sitting
        # 1.4pp from its stop with no memory of its own earlier reasoning and
        # no view of the stop — 23 minutes later the bracket fired. Confronting
        # the model with its prior verdict turns hold-reaffirmation into an
        # explicit decision instead of fresh anchoring each cycle.
        prior_verdicts: dict[str, tuple[str, float, str]] = {}
        try:
            for rec in self.journal.today():
                # Only records that carry an actual MODEL verdict — synthetic
                # rows (slate exclusions, backstop drops, fallback declines)
                # would otherwise masquerade as the model's own reasoning.
                if (
                    rec.symbol
                    and rec.action in ("hold", "sell", "buy")
                    and rec.verdict not in ("slate_excluded", "dropped_buy")
                    and not rec.reason.startswith("option fallback")
                ):
                    prior_verdicts[rec.symbol] = (
                        rec.action, rec.conviction, rec.rationale_head,
                    )
        except Exception:
            pass
        for p in account.positions:
            if p.is_option:
                continue
            if p.symbol in self._system_managed_symbols():
                continue  # passive core/hedge/defensive: never on the slate
            bits: list[str] = []
            # Same durable baseline the rotation guard enforces (state clock
            # with ledger fallback) — the prompt must show the bar the guard
            # will actually hold a rotation to.
            conv = self._entry_conviction(p.symbol)
            if conv is not None:
                bits.append(f"entry conviction {conv:.2f}")
            age = self.state.entry_age_days(p.symbol)
            if age is not None:
                bits.append(f"held {age:.1f}d")
            # Stop geometry (trusted, from our own risk state): the planned
            # stop width and how far the position currently sits from it —
            # the number a losing hold must consciously accept riding toward.
            stop_w = self.state.get_stop_width(p.symbol)
            if stop_w > 0:
                dist = stop_w + p.unrealized_pl_pct  # pp of room left
                bits.append(
                    f"stop -{stop_w:.1f}% ({max(dist, 0.0):.1f}pp of room left)"
                )
            note = ", ".join(bits)
            why = latest_rationale.get(p.symbol, "")
            if why:
                note = (note + "; thesis: " if note else "thesis: ") + why[:90]
            pv = prior_verdicts.get(p.symbol)
            if pv:
                action, pconv, phead = pv
                note += (
                    f"; your last verdict today: {action.upper()} "
                    f"conv {pconv:.2f}"
                    + (f" ('{phead[:70]}')" if phead else "")
                )
            if note:
                notes[p.symbol] = note
        return notes

    # -- rotation loss guard (week of 2026-07-13: UNH -$204 / HUBB -$158
    #    realized purely to free a slot) ------------------------------------ #
    def _entry_conviction(self, symbol: str) -> float | None:
        """The conviction of `symbol`'s most recent BUY. Tries the state clock
        first (fast), then falls back to the ledger: the state store rides the
        7-day churn-guard retention (state._CLOCK_RETENTION_DAYS), so a
        never-topped-up name held past a week — exactly the stale incumbent a
        rotation targets — has NO state entry, while the ledger keeps every
        buy's conviction forever. 0.0 in the ledger means 'not recorded'
        (core fills, pre-tracking rows), not zero conviction."""
        conv = self.state.last_buy_conviction(symbol)
        if conv is not None:
            return conv
        try:
            for rec in reversed(self.ledger.effective()):
                if (
                    rec.action == "buy" and rec.symbol == symbol
                    and (rec.conviction or 0.0) > 0
                ):
                    return rec.conviction
        except Exception as e:
            log.debug("Ledger entry-conviction lookup failed for %s: %s", symbol, e)
        return None

    def _apply_rotation_guard(self, proposals, account, composites):
        """Enforce the rotation edge the prompt only ASKS for: a SELL that locks
        in a real loss to free capital for a new name must be displaced by a
        clearly stronger incoming name. Detection is deterministic (never the
        LLM's rationale text): the same response BUYs a not-held equity name (the
        buy the freed capital would fund) AND sells a held loser. This fires
        whenever that PAIR appears — NOT only at the slot cap: the original
        `positions < MAX_OPEN` bypass meant the guard never once ran (Jul 17: 13
        of 15 slots, so the SPCX -9.9% / MU -9.7% loss-rotations it was built to
        veto sailed straight through). A loss-locking sell frees CAPITAL for the
        rotation buy regardless of how many slots are open. Watchdog
        stops/trails/flattens never pass through decision proposals, and a
        standalone risk-off sell has no paired new-name buy, so neither can be
        blocked here. Escape hatches keep this from ever pinning a position the
        model urgently wants out of: buys are halted (kill switch) => nothing to
        fund, so pass everything; a SELL whose OWN conviction is at/above
        rotation_guard_exempt_sell_conviction is a risk-off exit (never vetoed —
        'never block a legitimate exit' outranks anti-churn); a missing
        entry-conviction baseline fails open; a loss deeper than
        rotation_guard_max_loss_pct passes (the guard band is exhausted); and a
        sell already vetoed today whose loss has deteriorated by
        rotation_guard_repeat_release_pct since that veto passes (persistent
        exit intent — SPCX Jul 22 was vetoed at -5.4% and stopped out at -9.8%). Vetoed positions still keep their
        exchange bracket + watchdog stops — the veto holds, it never strands.
        Every guarded loss-sell leaves an auditable ruling (log + journal),
        pass or veto."""
        r = self.cfg.risk
        if not r.rotation_loss_guard_enabled or not proposals:
            return proposals
        if self.risk.kill_switch:
            return proposals  # buys are halted — a paired sell funds no rotation
        held_syms = {
            p.symbol for p in account.positions
            if not getattr(p, "is_option", False)
        }
        incoming = [
            p for p in proposals
            if p.action is Action.BUY and p.instrument is not Instrument.OPTION
            and p.symbol not in held_syms
        ]
        if not incoming:
            return proposals  # no new name to fund — not a rotation
        best_in_conv = max(p.conviction for p in incoming)
        best_in_comp = max(
            (composites.get(p.symbol) for p in incoming
             if composites.get(p.symbol) is not None),
            default=None,
        )
        kept = []
        for p in proposals:
            if not (
                p.action is Action.SELL
                and p.instrument is not Instrument.OPTION
            ):
                kept.append(p)
                continue
            pos = account.position_for(p.symbol)
            if pos is None or pos.unrealized_pl_pct > -r.rotation_guard_min_loss_pct:
                kept.append(p)  # not a loss-locking sell
                continue
            # Red-day release (Jul 29: NOK's -4.8% exit was vetoed at 16:09
            # and only closed at 18:00 — the guard held a sinking loser open
            # ~2h into a losing session). On a day the BOOK is losing, a
            # loss-cut the model asks for is defense, not lukewarm churn.
            day_pl = getattr(account, "day_pl_pct", 0.0) or 0.0
            if getattr(r, "rotation_guard_red_day_release", False) and day_pl < 0:
                log.info(
                    "Rotation guard: PASS %s at %+.1f%% — red book day "
                    "(day P/L %.2f%%): a requested loss-cut is defense, "
                    "not churn.", p.symbol, pos.unrealized_pl_pct, day_pl,
                )
                kept.append(p)
                continue
            # Deterioration releases (SPCX Jul 22: vetoed at -5.4/-5.6/-6.6/
            # -9.3%, then the bracket stop fired at -9.8% — the guard pinned a
            # sinking position all the way into a WORSE exit). (a) Depth: past
            # max_loss the guarded band [min_loss, max_loss] is exhausted;
            # only the stop remains, so the sell passes. (b) Persistence: a
            # sell already vetoed earlier TODAY whose loss has since worsened
            # by the release delta is a repeated exit request against a
            # deteriorating tape — a thesis-break, not lukewarm churn.
            max_loss = r.rotation_guard_max_loss_pct
            if (
                max_loss > r.rotation_guard_min_loss_pct
                and pos.unrealized_pl_pct <= -max_loss
            ):
                log.info(
                    "Rotation guard: PASS %s at %+.1f%% — loss beyond the "
                    "%.1f%% guard band; pinning it further only rides into "
                    "the stop.", p.symbol, pos.unrealized_pl_pct, max_loss,
                )
                kept.append(p)
                continue
            today = self.state._trading_day()
            veto_day, veto_loss = self._rotation_vetoes.get(p.symbol, ("", 0.0))
            release = r.rotation_guard_repeat_release_pct
            if (
                release > 0 and veto_day == today
                and pos.unrealized_pl_pct <= veto_loss - release
            ):
                log.info(
                    "Rotation guard: PASS %s at %+.1f%% — already vetoed "
                    "today at %+.1f%% and the loss kept deteriorating "
                    "(persistent exit intent released).",
                    p.symbol, pos.unrealized_pl_pct, veto_loss,
                )
                kept.append(p)
                continue
            exempt = r.rotation_guard_exempt_sell_conviction
            if exempt > 0 and p.conviction >= exempt:
                # The model strongly wants OUT (thesis broken / risk-off) — a
                # co-occurring unrelated buy must not reclassify this exit as
                # a lukewarm capital-freeing rotation.
                log.info(
                    "Rotation guard: PASS %s at %+.1f%% — own sell conviction "
                    "%.2f >= exempt %.2f (risk-off exit, not vetoed).",
                    p.symbol, pos.unrealized_pl_pct, p.conviction, exempt,
                )
                kept.append(p)
                continue
            entry_conv = self._entry_conviction(p.symbol)
            if entry_conv is None:
                log.info(
                    "Rotation guard: PASS %s at %+.1f%% — no entry-conviction "
                    "baseline; failing open.", p.symbol, pos.unrealized_pl_pct,
                )
                kept.append(p)  # no baseline anywhere — fail open
                continue
            conv_ok = best_in_conv >= entry_conv + r.rotation_min_conviction_edge
            comp_ok = True
            if r.rotation_require_composite_edge:
                inc_comp = composites.get(p.symbol)
                if best_in_comp is not None and inc_comp is not None:
                    comp_ok = best_in_comp > inc_comp
            if conv_ok and comp_ok:
                log.info(
                    "Rotation guard: PASS %s at %+.1f%% — incoming conviction "
                    "%.2f clears entry %.2f by +%g%s; rotation allowed.",
                    p.symbol, pos.unrealized_pl_pct, best_in_conv, entry_conv,
                    r.rotation_min_conviction_edge,
                    "" if comp_ok else " (composite edge waived)",
                )
                kept.append(p)
                continue
            reason = (
                f"Rotation guard: selling {p.symbol} at "
                f"{pos.unrealized_pl_pct:+.1f}% locks in a real loss, and the "
                f"best incoming buy (conviction {best_in_conv:.2f}) doesn't "
                f"clear the incumbent's entry {entry_conv:.2f} by "
                f"+{r.rotation_min_conviction_edge:g}"
                + ("" if comp_ok else " (composite edge missing)")
                + " — holding instead."
            )
            log.warning("%s", reason)
            # Persistence baseline: remember today's FIRST veto loss for this
            # symbol (in-memory; a restart just means one more veto before the
            # release can fire). Keyed to the ET day so yesterday's veto can't
            # release today's first sell.
            if veto_day != today:
                self._rotation_vetoes[p.symbol] = (today, pos.unrealized_pl_pct)
            self._journal_decision(
                p.symbol, "sell", "equity", p.conviction, p.target_weight_pct,
                "rotation_guard", 0.0, reason,
                p.rationale[:120] if p.rationale else "",
            )
        return kept

    # -- proposal execution: equity sells first (rotation support) ---------- #
    def _execute_proposals(
        self, proposals, account, signal_kinds,
        tech_ctx: dict[str, dict] | None = None,
        composites: dict[str, float] | None = None,
        bundles: list | None = None,
    ) -> float:
        """Execute the cycle's proposals, equity SELLs first. Returns the $
        dropped by whole-share flooring across the cycle's buys.

        Sells-first is what makes ROTATION work on a full book (postmortem
        2026-07-14: MU at 0.63 conviction died at the slot cap while CVX sat
        held at 0.46): each decision sell folds back into the snapshot
        (_apply_pending_close), so a paired SELL-weak + BUY-strong response
        frees the slot and the capital before any buy is evaluated — whatever
        order the model listed them in. The budget split runs AFTER the sells
        for the same reason: on a full book, the rotation buy's deployable
        cash IS the freed capital."""
        self._option_fallbacks = []
        self._bear_puts_proposed = 0
        self._bear_puts_approved = 0
        if not proposals:
            log.info("No actionable proposals this cycle.")
            return 0.0
        tech_ctx = tech_ctx or {}
        composites = composites or {}
        proposals = self._apply_rotation_guard(proposals, account, composites)
        sells = [
            p for p in proposals
            if p.instrument is not Instrument.OPTION and p.action.value == "sell"
        ]
        rest = [
            p for p in proposals
            if p.instrument is Instrument.OPTION or p.action.value != "sell"
        ]
        for proposal in sells:
            self._stamp_liveness()  # order placement progresses per name
            self._handle_equity(
                proposal, account, signal_kinds.get(proposal.symbol, []),
                composite=composites.get(proposal.symbol),
            )
        budget_caps = self._cycle_budget_caps(rest, account, composites)
        undeployed = 0.0
        for proposal in rest:
            self._stamp_liveness()
            kinds = signal_kinds.get(proposal.symbol, [])
            if proposal.instrument is Instrument.OPTION:
                self._handle_option(
                    proposal, account, kinds,
                    tech=tech_ctx.get(proposal.symbol),
                )
            else:
                undeployed += self._handle_equity(
                    proposal, account, kinds,
                    cycle_budget_cap=budget_caps.get(proposal.symbol),
                    tech=tech_ctx.get(proposal.symbol),
                    composite=composites.get(proposal.symbol),
                )
        self._run_option_fallbacks(account, signal_kinds, tech_ctx, bundles)
        return undeployed

    def _run_option_fallbacks(
        self, account, signal_kinds, tech_ctx: dict[str, dict],
        bundles: list | None,
    ) -> None:
        """Consume the cycle's equity-gate rejections (overextension /
        earnings blackout) as scoped option-fallback decisions — capped at 2
        extra LLM calls per cycle, highest conviction first. Each approved
        structure flows through the normal _handle_option path (premium cap,
        direction/DTE/liquidity gates, journal, ledger)."""
        queue, self._option_fallbacks = self._option_fallbacks, []
        if not queue or self.options is None or not bundles:
            return
        if not self.broker.is_market_open():
            return  # same reasoning as the post-LLM close fence
        # Per-day attempt cap (Jul 29: F burned a fallback call at 9:25 and a
        # main-path spread reject at 10:17, and would have re-tried hourly all
        # day). Count today's option attempts per symbol — fallback declines
        # AND main-path option verdicts — and stop after 2.
        attempts: dict[str, int] = {}
        try:
            for rec in self.journal.today():
                if rec.instrument == "option" or rec.reason.startswith(
                    "option fallback"
                ):
                    attempts[rec.symbol] = attempts.get(rec.symbol, 0) + 1
        except Exception:
            pass
        by_symbol = {b.symbol: b for b in bundles}
        queue.sort(key=lambda pr: -pr[0].conviction)
        for proposal, reason in queue[:2]:
            bundle = by_symbol.get(proposal.symbol)
            if bundle is None:
                continue
            if attempts.get(proposal.symbol, 0) >= 2:
                log.info(
                    "Option fallback for %s skipped: %d option attempt(s) "
                    "already today (daily cap 2 — stop ping-ponging one name).",
                    proposal.symbol, attempts[proposal.symbol],
                )
                continue
            self._stamp_liveness()
            opt = self.engine.decide_option_fallback(
                bundle, account, proposal.conviction, reason,
                price=self.broker.latest_price(proposal.symbol),
                regime_label=self._regime_label,
                regime_reason="",
                curated=self._curated_lessons(),
            )
            if opt is None:
                # Journal the decline: without a durable record the next cycle
                # (and the nightly postmortem) can't see the attempt happened,
                # and the daily attempt cap above has nothing to count.
                self._journal_decision(
                    proposal.symbol, "hold", "option", proposal.conviction,
                    0.0, "rejected", 0.0,
                    "option fallback declined (model HOLD)", "",
                )
                continue
            self._handle_option(
                opt, account, signal_kinds.get(proposal.symbol, []),
                tech=tech_ctx.get(proposal.symbol),
            )

    # -- prompt-time buy-headroom / slate filtering (A2) --------------------- #
    def _buy_headroom_usd(self, symbol: str, account) -> tuple[float, str]:
        """How many dollars can still go into `symbol` as a buy this cycle?
        Returns (headroom_usd, binding_reason). Reason is '' when headroom > 0
        and names the binding constraint when headroom is zero/negative.
        Conservative: uses held market value only (no broker round-trip for
        pending orders) since the risk gate will count pending anyway."""
        r = self.cfg.risk
        equity = account.equity
        if equity <= 0:
            return 0.0, "zero equity"
        min_order = max(r.min_order_usd, equity * (r.min_order_pct / 100.0))

        # Churn clocks
        if r.min_add_interval_hours > 0:
            since_buy = self.state.hours_since_buy(symbol)
            if since_buy is not None and since_buy < r.min_add_interval_hours:
                return 0.0, f"topped up {since_buy:.1f}h ago (next ok in {r.min_add_interval_hours:g}h)"
        pos = account.position_for(symbol)
        if pos is None and r.reentry_cooldown_hours > 0:
            since_exit = self.state.hours_since_exit(symbol)
            if since_exit is not None and since_exit < r.reentry_cooldown_hours:
                return 0.0, f"exited {since_exit:.1f}h ago (cooldown {r.reentry_cooldown_hours:g}h)"

        # Daily buy count cap
        if r.max_daily_buys_per_symbol > 0:
            n = self.state.daily_symbol_buys(symbol)
            if n >= r.max_daily_buys_per_symbol:
                return 0.0, f"bought {n}x today (daily cap {r.max_daily_buys_per_symbol})"

        # Daily dollar ceiling
        if r.max_daily_symbol_deploy_pct > 0:
            day_cap = equity * (r.max_daily_symbol_deploy_pct / 100.0)
            spent = self.state.daily_symbol_spend(symbol)
            day_room = day_cap - spent
            if day_room <= min_order:
                return 0.0, (
                    f"daily ceiling ${day_cap:,.0f} nearly exhausted "
                    f"(${max(0, day_room):,.0f} headroom)"
                )

        # Symbol exposure cap (held only — conservative)
        held_val = pos.market_value if pos else 0.0
        sym_room = equity * (r.max_symbol_exposure_pct / 100.0) - held_val
        if sym_room <= min_order:
            return 0.0, f"at {r.max_symbol_exposure_pct:.0f}% symbol cap"

        return min(sym_room, day_room if r.max_daily_symbol_deploy_pct > 0 else sym_room), ""

    def _partition_slate(
        self, bundles: list, account
    ) -> tuple[list, dict[str, str]]:
        """Split bundles into (filtered_bundles, buy_excluded_map).
        - Not-held + headroom < min_order → drop entirely (nothing to sell; saves tokens).
        - Held + headroom < min_order → keep for SELL/HOLD, add to buy_excluded.
        - Not-held + price under the liquidity floor → same as headroom-blocked:
          the risk gate rejects these unconditionally, yet AMC (~$2.20) was
          re-proposed and rejected EVERY day Jul 17-22 while topping the
          composite — a permanent reject must not keep costing prompt tokens
          and proposal slots.
        Both paths are also captured in buy_excluded so the prompt's "excluded" section
        is complete and the backstop pass can check the full set."""
        r = self.cfg.risk
        equity = account.equity if account.equity > 0 else 1.0
        min_order = max(r.min_order_usd, equity * (r.min_order_pct / 100.0))
        buy_excluded: dict[str, str] = {}
        filtered: list = []
        for b in bundles:
            headroom, reason = self._buy_headroom_usd(b.symbol, account)
            px = next(
                (s.data.get("price") for s in b.signals
                 if s.kind is SignalKind.TECHNICAL and s.data
                 and s.data.get("price")),
                None,
            )
            if (
                headroom >= min_order
                and px is not None and px < r.min_trade_price_usd
            ):
                headroom = 0.0
                reason = (
                    f"price ${px:.2f} < ${r.min_trade_price_usd:g} "
                    "liquidity floor"
                )
            if headroom < min_order:
                buy_excluded[b.symbol] = reason or "no buy headroom"
                if account.position_for(b.symbol) is not None:
                    filtered.append(b)  # keep: may need SELL/HOLD
                elif self.options is not None and self._bearish_lean(b):
                    # Keep: the equity buy is blocked, but a BEARISH candidate's
                    # whole reason for being on the slate is a defined-risk PUT
                    # play (the screener admits score<=-min_score names only when
                    # options are on). Options spend from their own budget —
                    # dropping these silently disabled "profit from the downside"
                    # exactly when the book was full.
                    filtered.append(b)
                # else: drop; nothing to sell and buy is blocked
            else:
                filtered.append(b)
        if buy_excluded:
            log.info(
                "Buy-excluded from slate this cycle: %s",
                ", ".join(f"{s} ({r})" for s, r in list(buy_excluded.items())[:5])
                + (f" …+{len(buy_excluded) - 5} more" if len(buy_excluded) > 5 else ""),
            )
        return filtered, buy_excluded

    def _bearish_lean(self, bundle) -> bool:
        """True when the bundle's evidence leans bearish enough to justify
        keeping an equity-blocked, not-held name on the slate as a PUT
        candidate. The discovery signal (the screener's smart-money lean that
        surfaced the name) is the primary read — mirror the aggregator's
        bearish intake bar; without one, fall back to the mean of the scored
        signals."""
        bar = max(0.2, self.cfg.screener.min_score)
        disc = [
            s.score for s in bundle.signals
            if s.kind is SignalKind.DISCOVERY and s.score is not None
        ]
        if disc:
            return min(disc) <= -bar
        scores = [s.score for s in bundle.signals if s.score is not None]
        return bool(scores) and sum(scores) / len(scores) <= -bar

    def _journal_decision(
        self, symbol: str, action: str, instrument: str,
        conviction: float, target_weight_pct: float, verdict: str,
        approved_notional: float, reason: str, rationale: str,
    ) -> None:
        try:
            self.journal.record(DecisionRecord(
                ts=__import__("datetime").datetime.now(
                    __import__("datetime").timezone.utc
                ).isoformat(),
                symbol=symbol, action=action, instrument=instrument,
                conviction=conviction, target_weight_pct=target_weight_pct,
                verdict=verdict,  # type: ignore[arg-type]
                approved_notional=approved_notional,
                reason=reason, rationale_head=rationale,
            ))
        except Exception as e:
            log.warning("Journal record failed: %s", e)

    def _drop_excluded_buys(
        self, proposals: list, buy_excluded: dict[str, str]
    ) -> list:
        """Hard backstop: if Claude proposes a BUY for a symbol we explicitly
        excluded, drop it. SELL and HOLD always pass — closing is never blocked.
        OPTION proposals also pass: the buy-exclusions are EQUITY sizing
        headroom (symbol/position/cash/gross caps); options spend from their
        own budget (premium cap, concurrency cap, halt gate — all enforced in
        evaluate_option). Dropping them here silently blocked long_put /
        bear_put_spread on exactly the capped names whose decline we most
        need to be able to profit from. Logs any drops so it's auditable."""
        if not buy_excluded:
            return proposals
        kept = []
        for prop in proposals:
            if (prop.action.value == "buy" and prop.symbol in buy_excluded
                    and prop.instrument is not Instrument.OPTION):
                log.warning(
                    "DROPPED BUY %s: model ignored buy-exclusion (%s) — "
                    "the risk gate would also reject it, but we drop it here "
                    "to save the broker round-trip.",
                    prop.symbol, buy_excluded[prop.symbol],
                )
            else:
                kept.append(prop)
        return kept

    # -- slate whitelist (Todo-3 S.1) --------------------------------------- #
    @staticmethod
    def _filter_to_slate(proposals, bundles, account, extra=None):
        """Drop any proposal whose symbol was never presented to the model. The
        decision prompt embeds UNTRUSTED third-party text (headlines, social
        posts, curated-list names); a crafted payload could persuade the model to
        propose a pumped ticker no screener surfaced. The model may only act on
        the slate it was shown (candidate bundles) plus what we already hold
        (so closing a position is never blocked). `extra` adds symbols WE
        explicitly sanctioned in the prompt this cycle (the falling-market
        index-put hedge), which are neither slate names nor necessarily held."""
        allowed = {b.symbol for b in bundles} | {p.symbol for p in account.positions}
        hedge_only = (set(extra) if extra else set()) - allowed
        kept = []
        for prop in proposals:
            if prop.symbol in allowed:
                kept.append(prop)
            elif prop.symbol in hedge_only:
                # Sanctioned via `extra` alone (the index-put hedge on a name
                # neither slated nor held): the sanction is PUT-ONLY — enforce
                # that at the gate, not just in the prompt, so the whitelist
                # can't be ridden into an equity buy or a call structure.
                is_put_play = (
                    getattr(prop.instrument, "value", str(prop.instrument))
                    == "option"
                    and prop.option_legs
                    and all(
                        leg.right.lower().startswith("p")
                        for leg in prop.option_legs
                    )
                )
                if is_put_play:
                    kept.append(prop)
                else:
                    log.warning(
                        "DROPPED %s proposal for %s: the falling-market "
                        "sanction covers defined-risk PUTs only.",
                        prop.action.value.upper(), prop.symbol,
                    )
            else:
                log.warning(
                    "DROPPED %s proposal for %s: symbol not in the candidate "
                    "slate or held book (possible prompt injection).",
                    prop.action.value.upper(), prop.symbol,
                )
        return kept

    # -- intra-cycle running tally (1B.3) ---------------------------------- #
    # The account is fetched ONCE per cycle; every proposal is then evaluated
    # against that single snapshot. Without folding each fill back in, N buys in
    # one cycle each size against the pre-cycle picture and can JOINTLY breach the
    # no-leverage gross cap, the cash buffer, or max-open-positions — and on a
    # margin account Alpaca's ~2x buying power will NOT reject the over-deploy for
    # us. These mutate the in-memory snapshot so later proposals see reality.
    @staticmethod
    def _apply_pending_buy(
        account, symbol: str, notional: float, price: float, qty: float
    ) -> None:
        """Reflect a just-submitted BUY into the snapshot: add/extend the position
        and draw down cash + buying power by the (conservatively full) notional."""
        notional = max(0.0, float(notional))
        existing = account.position_for(symbol)
        if existing is not None:
            existing.qty += qty
            existing.market_value += notional
        else:
            account.positions.append(Position(
                symbol=symbol, qty=qty, avg_entry_price=price,
                current_price=price, market_value=notional,
                unrealized_pl=0.0, unrealized_pl_pct=0.0,
            ))
        account.cash = max(0.0, account.cash - notional)
        account.buying_power = max(0.0, account.buying_power - notional)

    @staticmethod
    def _apply_pending_close(account, symbol: str) -> None:
        """Reflect a decision SELL into the snapshot: drop the position and return
        its market value to cash + buying power, so a later buy this cycle can use
        the freed capital and slot."""
        pos = account.position_for(symbol)
        if pos is None:
            return
        account.positions = [p for p in account.positions if p.symbol != symbol]
        account.cash += max(0.0, pos.market_value)
        account.buying_power += max(0.0, pos.market_value)

    # -- regime-off book trim (1B.6) --------------------------------------- #
    def _apply_regime_trim(self, account, regime) -> None:
        """On the FLIP into a risk-off regime, sell a slice of every held name to
        actively de-risk the existing book — the "salvage when the market is down"
        lever. The regime multiplier alone only shrinks NEW buys, so held names
        would otherwise ride a downturn to their own stops with no account-level
        response. Fires ONCE per downturn (keyed off the persisted last label), not
        every cycle we stay risk-off. Off by default.

        The bracket is released before the partial sell (its legs reserve the
        shares), so the trimmed remainder is re-protected by a watchdog stop/take
        at the default levels rather than an exchange bracket."""
        r = self.cfg.risk
        prev = self.state.get_regime_label()
        self.state.set_regime_label(regime.label)
        if not r.regime_trim_enabled:
            return
        # Only on the transition INTO risk-off, and only if there's a book to trim.
        if regime.label != "risk-off" or prev == "risk-off":
            return
        frac = r.regime_trim_pct / 100.0
        if frac <= 0 or not account.positions:
            return
        log.warning(
            "Regime flipped to risk-off — trimming the book by %.0f%% to de-risk "
            "(%d position[s]).", r.regime_trim_pct, len(account.positions),
        )
        with self._trade_lock:
            for pos in list(account.positions):
                if pos.is_option:
                    # A partial trim of an option structure makes no sense
                    # (contracts, paired legs) — options are premium-capped and
                    # watchdog-managed; the regime trim de-risks the EQUITY book.
                    continue
                sell_qty = round(pos.qty * frac, 6)
                # Whole-shares mode (GA-2.3): don't leave fractional dust that
                # can't carry a GTC exit; a sub-share trim is skipped.
                if r.whole_shares_only:
                    sell_qty = float(int(sell_qty))
                if sell_qty <= 0:
                    continue
                self.broker.cancel_open_orders_for(pos.symbol)  # release bracket
                oid = self.broker.reduce_position(pos.symbol, sell_qty)
                if not oid:
                    continue
                self._pending_oids.append((oid, pos.symbol))
                # Persist the oid at SUBMIT (mirror the watchdog, watchdog.py:644):
                # _pending_oids otherwise persists only at cycle end, so a crash
                # between here and merge_pending_orders would orphan the fill-check
                # and leave a rejected/partial trim uncorrected forever.
                self.state.add_pending_order(oid, pos.symbol)
                self.ledger.record(TradeRecord.for_sell(
                    pos.symbol, f"regime risk-off trim {r.regime_trim_pct:.0f}%", oid,
                    qty=sell_qty, realized_pl_pct=pos.unrealized_pl_pct,
                    realized_pl=None, exit_reason="regime_trim",
                    exit_price=pos.current_price or None,
                ))
                # Keep the in-memory snapshot honest for the rest of the cycle and
                # re-protect the (now bracket-less) remainder via the watchdog.
                # PRESERVE the position's real stop/take (and scaled flag) when
                # one is registered: clobbering a 4%-stop name with the 8%
                # default doubled its risk AND (post trail_geometry) jumped its
                # trail arm from 6% to 12%, disarming an armed winner at the
                # exact moment the trim canceled its bracket. The buy-time
                # stop_widths note covers whole-share names the exits map never
                # saw. Defaults remain the last resort.
                pos.qty = round(pos.qty - sell_qty, 6)
                pos.market_value = pos.qty * pos.current_price
                prev = self.state.get_exits(pos.symbol) or {}
                if (prev.get("stop_pct") or 0.0) > 0:
                    stop, take = prev["stop_pct"], prev["take_pct"]
                else:
                    stop = (self.state.get_stop_width(pos.symbol)
                            or r.default_stop_loss_pct)
                    take = r.default_take_profit_pct
                self.state.register_exits(
                    pos.symbol, stop, take,
                    scaled=bool(prev.get("scaled", 0.0)),
                )

    # -- deterministic thesis-decay exit (1B.4b) --------------------------- #
    def _apply_thesis_decay_exits(self, bundles, account) -> set[str]:
        """SELL held names whose entry thesis is no longer corroborated by fresh
        signals — a name whose signals went stale but never hit a price stop would
        otherwise be held forever, and a decision-sell needs the LLM up. This is
        LLM-INDEPENDENT and deterministic. Guarded by a grace age so a fresh buy
        isn't dumped on one quiet signal day. Returns the exited symbols.

        CAUTION (why it's opt-in): signal ABSENCE is the decay trigger, so a
        transient data outage that blanks the feed could force spurious exits — run
        it only once you trust the signal feed."""
        r = self.cfg.risk
        if not r.thesis_decay_enabled:
            return set()
        by_symbol = {b.symbol: b for b in bundles}
        exited: set[str] = set()
        for pos in list(account.positions):
            # Option rows: bundles are keyed by underlying, so an OCC symbol
            # always looks "uncorroborated" — decay would wrongly fire an
            # EQUITY close on it. Options already have a DTE-bounded lifecycle
            # (watchdog premium stop/take + expiry close); leave them to it.
            if pos.is_option:
                continue
            # System-managed allocations (core / hedge / defensive) carry no
            # per-name thesis, so signal ABSENCE must not decay-exit them —
            # the orchestrator opens and closes them off deterministic reads.
            if pos.symbol in self._system_managed_symbols():
                continue
            age = self.state.entry_age_days(pos.symbol)
            if age is None or age < r.thesis_decay_min_age_days:
                continue
            if self._thesis_corroborated(by_symbol.get(pos.symbol), r.thesis_min_score):
                continue
            log.info(
                "Thesis decay on %s: no signal >= %.2f after %.1fd — deterministic "
                "exit (%.1f%%).", pos.symbol, r.thesis_min_score, age,
                pos.unrealized_pl_pct,
            )
            with self._trade_lock:
                # Fresh read (qty_available drives the close ladder); fall back
                # to the snapshot row if the re-read is transiently unreadable.
                live = self.broker.open_position(pos.symbol) or pos
                outcome, oid = self.watchdog.close_now(live, "thesis_decay")
                if outcome == "full":
                    self.watchdog.forget(pos.symbol)
                    self.ledger.record(TradeRecord.for_sell(
                        pos.symbol,
                        "thesis decay: entry signals no longer corroborated",
                        oid, qty=live.qty, realized_pl_pct=live.unrealized_pl_pct,
                        realized_pl=live.unrealized_pl, exit_reason="thesis_decay",
                        exit_price=live.current_price or None,
                    ))
                    self._pending_oids.append((oid, pos.symbol))
                    # Persist at submit (mirror the watchdog) — crash-safe fill-check.
                    self.state.add_pending_order(oid, pos.symbol)
                    # Start the re-entry cooldown clock (churn guard) — with
                    # the exit mark for the price-aware guard and the trip P&L
                    # for the loss-streak scorecard (decay exits are usually
                    # losers; without this the streak never sees them).
                    self.state.register_exit(
                        pos.symbol, price=live.current_price or None,
                        pl_pct=live.unrealized_pl_pct)
                elif outcome == "partial":
                    # Live legs replaced into marketable exits and ledgered
                    # inside close_now — close_now's own records already stamp
                    # the exit clock with price and P&L, so this bare stamp is
                    # just the cooldown belt (None fields are no-ops).
                    self.state.register_exit(pos.symbol)
                else:
                    log.error(
                        "Thesis-decay close FAILED for %s — stays held (and in "
                        "the slate) until a later cycle exits it.", pos.symbol,
                    )
                    continue
                # Keep this cycle's snapshot honest (frees capital/slot downstream).
                self._apply_pending_close(account, pos.symbol)
            exited.add(pos.symbol)
        return exited

    @staticmethod
    def _thesis_corroborated(bundle, min_score: float) -> bool:
        """True if `bundle` still carries at least one bullish signal (score >=
        min_score). A held name with no bundle, or only sub-threshold / bearish
        signals, has a decayed thesis. DISCOVERY signals count: a name the scanner
        re-surfaces with a positive lean is still corroborated."""
        if bundle is None:
            return False
        return any(
            s.score is not None and s.score >= min_score for s in bundle.signals
        )

    # -- falling-tape core defense (Jul 29) --------------------------------- #
    def _market_falling(self) -> tuple[bool, str]:
        """Deterministic "the market is falling" read for the core defense and
        the index-put sanction. True when the regime label is risk-off, the
        long-run trend is down (SPY below its 200dma), or TODAY'S benchmark
        move breaches the intraday defense trigger. Fails closed to (False, '')
        when the regime filter is off or the data is degraded."""
        if not self.cfg.risk.regime_filter_enabled:
            return False, ""
        reg = self.regime.assess()
        if reg.label == "risk-off":
            return True, "regime is risk-off"
        if reg.trend == "down":
            return True, "SPY below its 200dma (long-run downtrend)"
        drop = getattr(self.cfg, "market_drop_defense_pct", 0.0)
        if (
            drop > 0
            and reg.day_change_pct is not None
            and reg.day_change_pct <= -drop
        ):
            return True, (
                f"SPY {reg.day_change_pct:+.1f}% today "
                f"(<= -{drop:g}% intraday defense trigger)"
            )
        return False, ""

    def _apply_core_defense(self, account) -> None:
        """When the market itself is falling, stop averaging INTO it and take
        risk OFF the core: pause the core-ETF fill for the cycle (the flag the
        fill checks) and sell CORE_DEFENSE_TRIM_PCT of the core position, at
        most once per trading day. This is the deterministic answer to "QQQ
        always drags the book down when it falls" — the core's other guards
        (GTC stop 15% under basis, equity floor, daily-loss flatten) only act
        at catastrophe distance. A defensive trim is not a thesis exit: no
        re-entry cooldown / loss-streak stamp, and the fill resumes (DCA back
        in) as soon as the falling read clears."""
        etf = self.cfg.core_etf
        self._core_defense_active = False
        if not etf or not getattr(self.cfg, "core_defense_enabled", False):
            return
        falling, why = self._market_falling()
        if not falling:
            return
        self._core_defense_active = True
        pos = account.position_for(etf)
        if pos is None or pos.qty <= 0:
            return
        if self.state.core_defense_fired_today():
            return  # already trimmed today; the paused fill carries the defense
        frac = max(
            0.0, min(100.0, getattr(self.cfg, "core_defense_trim_pct", 0.0))
        ) / 100.0
        sell_qty = float(int(pos.qty * frac))  # whole shares; sub-share trim skipped
        if frac <= 0 or sell_qty <= 0:
            return
        with self._trade_lock:
            # Release the resting GTC core stop first — its legs reserve the
            # shares (the same wash-trade/reserved-qty dance as the core fill).
            self.broker.cancel_open_orders_for(etf)
            oid = self.broker.reduce_position(etf, sell_qty)
        # The GTC core stop was just canceled — the core must NOT ride the
        # rest of this (minutes-long) cycle stopless. Arm the 30s watchdog
        # retry unconditionally, then try to re-place the stop for the
        # remainder right now (the snapshot qty is reduced below before this
        # runs on the success path; on the failure path the full-size stop is
        # re-placed for the untouched position).
        self._core_stop_gap = True
        if not oid:
            log.warning(
                "Core defense: trim of %s failed to submit — retrying next "
                "cycle (GTC stop re-placement armed).", etf,
            )
            self._ensure_core_stop(account)
            return
        self.state.mark_core_defense()
        log.warning(
            "CORE DEFENSE: %s — sold %g of %g %s sh (%.0f%% trim); core fill "
            "paused while the falling read holds.",
            why, sell_qty, pos.qty, etf, frac * 100.0,
        )
        self._pending_oids.append((oid, etf))
        self.state.add_pending_order(oid, etf)
        self.ledger.record(TradeRecord.for_sell(
            etf, f"core defense trim {frac * 100.0:.0f}% — {why}", oid,
            qty=sell_qty, realized_pl_pct=pos.unrealized_pl_pct,
            realized_pl=None, exit_reason="core_defense",
            exit_price=pos.current_price or None,
        ))
        # Keep this cycle's snapshot honest: the trimmed shares are cash now.
        freed = sell_qty * max(0.0, pos.current_price or 0.0)
        pos.qty = round(pos.qty - sell_qty, 6)
        pos.market_value = pos.qty * max(0.0, pos.current_price or 0.0)
        account.cash += freed
        account.buying_power += freed
        # Re-place the GTC stop for the remainder NOW (sized from the reduced
        # snapshot qty). If the working trim sell wash-blocks it, the armed
        # watchdog retry heals within ~30s — the core never waits a full
        # decision cycle unprotected.
        self._ensure_core_stop(account)

    def _system_managed_symbols(self) -> set[str]:
        """Symbols the ORCHESTRATOR owns end-to-end (core ETF, auto-hedge
        inverse ETF, defensive T-bill core). They never enter the model's
        slate and model proposals against them are ignored — Claude proposes
        theses; these are allocations."""
        return {
            s for s in (
                getattr(self.cfg, "core_etf", ""),
                getattr(self.cfg, "hedge_etf", ""),
                getattr(self.cfg, "defensive_core_etf", ""),
            ) if s
        }

    # -- deterministic inverse-ETF auto-hedge (Jul 30) ---------------------- #
    def _apply_auto_hedge(self, account) -> None:
        """When the falling read holds for auto_hedge_min_cycles consecutive
        decision cycles, buy a 1x inverse ETF sized to auto_hedge_ratio x the
        book's net long exposure; unwind it once the read stays clear for the
        same number of cycles. This is the plain-EQUITY downside instrument
        the Jul-30 review called for: no options fragility, works with
        OPTIONS_ENABLED off, and — unlike the index-put sanction, which asks
        the model — it never waits on discretion. The persistence requirement
        is the noise filter: a single red tick arms nothing."""
        etf = getattr(self.cfg, "hedge_etf", "")
        if not etf:
            return
        falling, why = self._market_falling()
        if falling:
            self._falling_cycles += 1
            self._clear_cycles = 0
        else:
            self._clear_cycles += 1
        need = max(1, getattr(self.cfg, "auto_hedge_min_cycles", 2))
        pos = account.position_for(etf)
        held_val = max(0.0, pos.market_value) if pos is not None else 0.0
        if not falling:
            if self._clear_cycles >= need:
                self._falling_cycles = 0
                if pos is not None and pos.qty > 0:
                    with self._trade_lock:
                        self.broker.cancel_open_orders_for(etf)
                        oid = self.broker.close_position(etf)
                    if oid:
                        log.warning(
                            "AUTO-HEDGE UNWIND: falling read clear %d cycles — "
                            "closing %g %s (%+.1f%%).",
                            self._clear_cycles, pos.qty, etf,
                            pos.unrealized_pl_pct,
                        )
                        self.ledger.record(TradeRecord.for_sell(
                            etf, "auto-hedge unwind: falling read cleared",
                            oid, qty=pos.qty,
                            realized_pl_pct=pos.unrealized_pl_pct,
                            realized_pl=pos.unrealized_pl,
                            exit_reason="hedge_unwind",
                            exit_price=pos.current_price or None,
                        ))
                        self._pending_oids.append((oid, etf))
                        self.state.add_pending_order(oid, etf)
                        # Keep the cycle's snapshot honest: hedge is cash now.
                        account.cash += held_val
                        account.buying_power += held_val
                        pos.qty = 0.0
                        pos.market_value = 0.0
            return
        if self._falling_cycles < need:
            log.info(
                "Auto-hedge: falling read (%s) cycle %d/%d — not arming yet.",
                why, self._falling_cycles, need,
            )
            return
        # An account-wide halt means the book is already in flatten/defense
        # mode — don't open anything through it, even a hedge.
        halted, halt_why = self.risk.trading_halted(account)
        if halted:
            log.info("Auto-hedge skipped: %s", halt_why)
            return
        net_long = sum(
            max(0.0, p.market_value) for p in account.positions
            if not p.is_option and p.symbol != etf
        )
        ratio = max(0.0, min(1.0, getattr(self.cfg, "auto_hedge_ratio", 0.0)))
        ceiling = account.equity * (
            max(0.0, getattr(self.cfg, "auto_hedge_max_pct", 0.0)) / 100.0
        )
        target = min(ratio * net_long, ceiling)
        gap = target - held_val
        r = self.cfg.risk
        min_fill = max(
            r.min_order_usd, account.equity * (r.min_order_pct / 100.0), 1.0
        )
        if gap < min_fill:
            return
        min_cash = account.equity * (r.min_cash_buffer_pct / 100.0)
        spendable = max(0.0, min(account.cash - min_cash, account.buying_power))
        notional = round(min(gap, spendable), 2)
        if notional < min_fill:
            log.info(
                "Auto-hedge: want $%.0f more %s but only $%.0f spendable "
                "after the cash buffer.", gap, etf, spendable,
            )
            return
        price = self.broker.latest_price(etf)
        with self._trade_lock:
            oid = self.broker.submit_notional_buy(etf, notional)
        if not oid:
            log.warning("Auto-hedge buy of %s failed to submit — next cycle.", etf)
            return
        log.warning(
            "AUTO-HEDGE: %s (cycle %d) — bought $%.0f of %s "
            "(hedge $%.0f/$%.0f target = %.0f%% of $%.0f net-long).",
            why, self._falling_cycles, notional, etf, held_val + notional,
            target, ratio * 100.0, net_long,
        )
        self.ledger.record(TradeRecord(
            symbol=etf, action="buy", instrument="equity",
            qty=round(notional / price, 6) if price and price > 0 else 0.0,
            entry_price=price or 0.0, cost_usd=round(notional, 2),
            rationale=f"auto-hedge: {why}",
            entry_signals=["auto_hedge"], verdict="approved",
            risk_note=(
                f"deterministic inverse-ETF hedge, {ratio:.0%} of net-long, "
                f"ceiling {getattr(self.cfg, 'auto_hedge_max_pct', 0.0):.0f}% equity"
            ),
            order_id=oid,
        ))
        self._pending_oids.append((oid, etf))
        self.state.add_pending_order(oid, etf)
        self.state.register_entry(etf)
        self._apply_pending_buy(
            account, etf, notional, price,
            notional / price if price and price > 0 else 0.0,
        )

    def _apply_defensive_rotation(self, account) -> None:
        """Rotate the defensive T-bill core (SGOV/BIL) back to cash once the
        falling read clears, so the regular core fill can redeploy toward the
        real core ETF. The inbound leg lives in _apply_core_fill (which buys
        the defensive ETF instead of the core while the defense is active);
        this is the outbound leg. Position goes in one piece — it's a cash
        proxy, there is nothing to average out of."""
        d_etf = getattr(self.cfg, "defensive_core_etf", "")
        if not d_etf or getattr(self, "_core_defense_active", False):
            return
        pos = account.position_for(d_etf)
        if pos is None or pos.qty <= 0:
            return
        held_val = max(0.0, pos.market_value)
        with self._trade_lock:
            self.broker.cancel_open_orders_for(d_etf)
            oid = self.broker.close_position(d_etf)
        if not oid:
            log.warning(
                "Defensive rotation: close of %s failed to submit — next cycle.",
                d_etf,
            )
            return
        log.info(
            "DEFENSIVE ROTATION: falling read clear — closing $%.0f of %s so "
            "the core fill can redeploy.", held_val, d_etf,
        )
        self.ledger.record(TradeRecord.for_sell(
            d_etf, "defensive-core rotation: falling read cleared",
            oid, qty=pos.qty,
            realized_pl_pct=pos.unrealized_pl_pct,
            realized_pl=pos.unrealized_pl,
            exit_reason="defensive_rotate",
            exit_price=pos.current_price or None,
        ))
        self._pending_oids.append((oid, d_etf))
        self.state.add_pending_order(oid, d_etf)
        # Snapshot honesty: the defensive sleeve is cash again this cycle.
        account.cash += held_val
        account.buying_power += held_val
        pos.qty = 0.0
        pos.market_value = 0.0

    # -- core-satellite fill (1.6) ----------------------------------------- #
    def _apply_core_fill(self, account) -> None:
        """Deploy idle cash into the broad CORE_ETF until the book reaches
        TARGET_INVESTED_PCT, so sitting in cash isn't a structural short against the
        benchmark. The ETF is exempt from the single-name / sector caps (it IS the
        diversified core) but still bounded by the cash buffer and the no-leverage
        gross cap. Held as a passive base allocation — managed here, not by Claude,
        and protected only by the account-level guards (equity floor, emergency
        flatten, regime trim). Off unless CORE_ETF is set."""
        etf = self.cfg.core_etf
        if not etf or self.cfg.target_invested_pct <= 0:
            return
        # Regression 2026-07-23: this only checked kill_switch, so a daily-loss
        # halt, a drawdown halt, an equity-floor HALT LATCH, or a PDT block all
        # left the core sweep free to buy right through them — confirmed live:
        # the risk gate correctly rejected buys at "Daily loss 3.70% >= 3.00%"
        # and one second later the core fill bought $28,155 of QQQ anyway,
        # forcing an immediate unwind. trading_halted() is the SAME account-wide
        # check every other new-buy path goes through; the core sweep is a new
        # buy and must be gated the same way.
        halted, why = self.risk.trading_halted(account)
        if halted:
            log.info("Core fill skipped: %s", why)
            return
        # Falling-tape core defense: never DCA INTO a falling market — the
        # Jul 29 pattern was the fill buying the decline every cycle while the
        # core dragged the book down. Resumes when the falling read clears.
        # With a defensive core configured (Jul 30), the fill REDIRECTS into
        # the T-bill ETF instead of stopping — idle cash earns the short rate
        # while the book waits out the decline (_apply_defensive_rotation
        # sells it back once the read clears).
        defensive_fill = False
        if getattr(self, "_core_defense_active", False):
            d_etf = getattr(self.cfg, "defensive_core_etf", "")
            if not d_etf:
                log.info(
                    "Core fill skipped: core defense active (market falling — "
                    "no DCA into the decline)."
                )
                return
            etf = d_etf
            defensive_fill = True
        r = self.cfg.risk
        equity = account.equity
        if equity <= 0:
            return
        deployed = sum(max(0.0, p.market_value) for p in account.positions)
        invested_pct = deployed / equity * 100.0
        # Never target beyond the no-leverage gross cap (respect the same ceiling
        # single-name buys do).
        target = min(self.cfg.target_invested_pct, r.max_gross_exposure_pct)
        # Exposure ladder (Jul 30): a RISK-core fill must respect the regime
        # rung too, or the sweep quietly rebuilds the exposure the ladder just
        # capped for satellites. The DEFENSIVE fill is exempt — T-bills are a
        # cash proxy, and parking cash is the point of the defensive posture.
        _label = getattr(self, "_regime_label", "")
        if (
            not defensive_fill
            and getattr(r, "exposure_ladder_enabled", False)
            and _label in ("neutral", "risk-off")
        ):
            rung = (
                r.exposure_neutral_pct if _label == "neutral"
                else r.exposure_risk_off_pct
            )
            if rung > 0:
                target = min(target, rung)
        if invested_pct >= target:
            return
        gap = equity * (target - invested_pct) / 100.0
        # Respect the cash buffer: keep min_cash_buffer_pct of equity uninvested.
        min_cash = equity * (r.min_cash_buffer_pct / 100.0)
        spendable = max(0.0, account.cash - min_cash)
        # Core position ceiling (CORE_MAX_PCT): the core is exempt from the
        # single-name cap, so idle cash otherwise sweeps it unbounded (~47% of
        # equity, 2026-07 audit). Cap the buy so the core never exceeds the
        # ceiling — this stops further accumulation but does not trim an existing
        # overweight (that stays a decision/manual action; trimming a resting GTC
        # stop risks the pending-cancel wedge).
        core_max_pct = getattr(self.cfg, "core_max_pct", 0.0)
        core_room = float("inf")
        if core_max_pct > 0:
            core_pos = account.position_for(etf)
            core_val = max(0.0, core_pos.market_value) if core_pos else 0.0
            core_room = max(0.0, equity * (core_max_pct / 100.0) - core_val)
            if core_room <= 0:
                log.info(
                    "Core fill skipped: %s already at/above the %.0f%% ceiling.",
                    etf, core_max_pct,
                )
                return
        # Per-cycle DCA throttle (CORE_FILL_MAX_PCT): never buy the whole gap at
        # one print — reset day 2026-07-27 swept $300k of QQQ (30% of the fresh
        # book) two minutes after the open and ate ~44% of the day's loss.
        # Spreading the fill across cycles averages the entry.
        per_cycle = getattr(self.cfg, "core_fill_max_pct", 0.0)
        cycle_cap = equity * (per_cycle / 100.0) if per_cycle > 0 else float("inf")
        notional = round(min(gap, spendable, core_room, cycle_cap), 2)
        # Same dust guard as satellite buys: a $98k book topping the core up by
        # $5 every cycle pays spread for nothing (min order scales with equity).
        min_fill = max(r.min_order_usd, equity * (r.min_order_pct / 100.0), 1.0)
        if notional < min_fill:
            return
        price = self.broker.latest_price(etf)
        with self._trade_lock:
            # Cancel the resting GTC stop-sell before buying: Alpaca treats a
            # buy against an open stop-sell as a potential wash trade and rejects
            # it. _ensure_core_stop (called right after this) will re-place the
            # stop at the updated size/level.
            self.broker.cancel_open_orders_for(etf)
            oid = self.broker.submit_notional_buy(etf, notional)
        if not oid:
            return
        log.info(
            "%s fill: bought $%.0f of %s (invested %.0f%% -> ~%.0f%%, target %.0f%%).",
            "Defensive core" if defensive_fill else "Core",
            notional, etf, invested_pct,
            invested_pct + notional / equity * 100.0, target,
        )
        if defensive_fill:
            self.ledger.record(TradeRecord(
                symbol=etf, action="buy", instrument="equity",
                qty=round(notional / price, 6) if price > 0 else 0.0,
                entry_price=price, cost_usd=round(notional, 2),
                rationale=(
                    "defensive core fill: park idle cash in T-bills while "
                    "the falling read holds"
                ),
                entry_signals=["defensive_fill"], verdict="approved",
                risk_note="defensive T-bill core — cash proxy, ladder-exempt",
                order_id=oid,
            ))
        else:
            self.ledger.record(
                TradeRecord.from_core_fill(etf, notional, price, oid)
            )
        self._pending_oids.append((oid, etf))
        # Persist at submit (mirror the watchdog) — crash-safe fill-check.
        self.state.add_pending_order(oid, etf)
        self.state.register_entry(etf)
        # Fold into this cycle's snapshot so a later call sees the deployed capital.
        self._apply_pending_buy(
            account, etf, notional, price, notional / price if price > 0 else 0.0,
        )
        # _ensure_core_stop runs right after this, and Alpaca wash-trade-rejects
        # its STOP SELL while this market BUY is still open (40310000 "opposite
        # side market/stop order exists", 2026-07-13 09:34: the stop went out
        # 160ms behind the buy and the core sat without exchange-side protection
        # for a full cycle). Wait briefly for the buy to go terminal so the stop
        # can rest THIS cycle; on timeout, _ensure_core_stop's warn-and-retry-
        # next-cycle path applies unchanged. Deliberately OUTSIDE the trade lock
        # so the watchdog's exits are never delayed behind this wait.
        status, filled = "unknown", 0.0
        for _ in range(15):
            status, filled, _qty = self.broker.order_fill(oid)
            if status not in ("new", "accepted", "pending_new", "partially_filled"):
                break
            time.sleep(1)
        # Reconcile the fold with the REAL outcome: _ensure_core_stop floors
        # its stop qty with int(), so sizing from the pre-submit estimate
        # rejects the stop whenever up-slippage fills fractionally fewer
        # shares than estimated — and a rejected/canceled buy must not leave
        # phantom shares in the snapshot for the stop (or later buys this
        # cycle) to size against.
        pos = account.position_for(etf)
        est_qty = notional / price if price > 0 else 0.0
        if pos is not None and est_qty > 0:
            if status == "filled" and filled > 0:
                pos.qty += filled - est_qty
            elif status in ("rejected", "canceled", "expired"):
                pos.qty -= est_qty
                pos.market_value = max(0.0, pos.market_value - notional)
                account.cash += notional
                account.buying_power += notional
                if pos.qty <= 0:
                    account.positions = [
                        p for p in account.positions if p.symbol != etf
                    ]

    # -- core exchange-side stop (GA-2.3) ----------------------------------- #
    def _ensure_core_stop(self, account) -> None:
        """Rest a standalone GTC STOP at the exchange for the core position,
        CORE_STOP_PCT under its average basis. The core accumulates through
        notional (fractional) buys, which can't carry brackets — before this its
        only protection was the 30s watchdog in a killable process; a resting
        stop survives a crash, a sleeping laptop, and the overnight session's
        open. Covers the whole-share part only (Alpaca rejects GTC on
        fractional qty); the sub-share residual stays watchdog-guarded.
        Re-issued (cancel + replace) when the position grows by >= 1 share or
        the basis moves the stop by > 0.5%. Placing a protective SELL is risk
        reduction — never gated by the kill switch. Best-effort: a failed
        cancel/submit is retried next cycle."""
        etf = self.cfg.core_etf
        pct = self.cfg.core_stop_pct
        if not etf or pct <= 0:
            return
        pos = account.position_for(etf)
        if pos is None or pos.qty < 1 or pos.avg_entry_price <= 0:
            # Nothing visible to protect — but if the gap is flagged and the
            # entry BUY is still working, the position simply hasn't landed
            # yet (the gap was flagged off the cycle snapshot's folded
            # estimate): keep the retry armed instead of disarming it exactly
            # when it's needed. open_buy_notional fails open to 0.0 on a
            # broker blip, so a blip clears the flag and the next decision
            # cycle heals it (best-effort contract).
            if self._core_stop_gap and self.broker.open_buy_notional(etf) > 0:
                return
            self._core_stop_gap = False
            return
        desired_qty = float(int(pos.qty))
        desired_stop = round(pos.avg_entry_price * (1 - pct / 100.0), 2)
        if desired_stop <= 0:
            self._core_stop_gap = False
            return
        with self._trade_lock:
            # Read-check-cancel-submit is ONE atomic step: both the decision
            # cycle and the watchdog-loop retry come through here, and an
            # unlocked read would let two callers each see "no resting stop"
            # and double-submit GTC stops.
            existing = self.broker.open_stop_sells(etf)
            for o in existing:
                if (
                    abs(o["qty"] - desired_qty) < 1.0
                    and abs(o["stop_price"] - desired_stop) / desired_stop < 0.005
                ):
                    self._core_stop_gap = False
                    return  # resting stop is already right — leave it alone
            for o in existing:  # stale size/level — replace
                self.broker.cancel_order(o["id"])
            oid = self.broker.submit(OrderRequest(
                symbol=etf, side=Action.SELL, order_type=OrderType.STOP,
                tif=TIF.GTC, qty=desired_qty, stop_price=desired_stop,
            ))
            self._core_stop_gap = oid is None
        if oid:
            log.info(
                "Core stop: GTC stop resting for %g %s @ %.2f (%.0f%% under "
                "basis %.2f).", desired_qty, etf, desired_stop, pct,
                pos.avg_entry_price,
            )
        else:
            log.warning(
                "Core stop for %s could not be placed (entry buy likely still "
                "open — wash-trade guard); the watchdog still guards it and "
                "retries every ~30s tick.", etf,
            )

    def _retry_core_stop(self) -> None:
        """Watchdog-tick retry for a core stop that couldn't rest (wash-trade
        reject while the entry buy was still open — Jul 24: $28k of QQQ sat
        watchdog-only for ~50 min waiting on the next decision cycle). No-op
        unless _ensure_core_stop flagged a gap, so the healthy path costs
        nothing per tick."""
        if not self._core_stop_gap:
            return
        try:
            self._ensure_core_stop(self.broker.get_account())
        except Exception as e:  # noqa: BLE001 — retry must never break the loop
            log.warning("Core-stop retry failed: %s", e)

    # -- equity path -------------------------------------------------------- #
    def _handle_equity(
        self, proposal: TradeProposal, account, signal_kinds: list[str] | None = None,
        cycle_budget_cap: float | None = None,
        tech: dict | None = None,
        composite: float | None = None,
    ) -> float:
        """Evaluate + execute one equity proposal. Returns the $ the whole-share
        bracket flooring dropped from an approved buy (0 for everything else)
        so the cycle can total the undeployed drag. `tech` (RSI/extension) and
        `composite` feed the risk layer's anti-chasing gate and composite
        floor; both fail open when None."""
        # System-managed allocations (core / auto-hedge / defensive core) are
        # opened and closed by the orchestrator off deterministic reads — a
        # model proposal against one is ignored, never executed (the slate
        # already excludes them, but held names ride in via _filter_to_slate).
        if proposal.symbol in self._system_managed_symbols():
            log.info(
                "Proposal for %s ignored: system-managed allocation "
                "(core/hedge/defensive sleeve).", proposal.symbol,
            )
            return 0.0
        price = self.broker.latest_price(proposal.symbol)
        vol = self.broker.annualized_vol(proposal.symbol)
        is_buy = proposal.action.value == "buy"
        pending = self.broker.open_buy_notional(proposal.symbol) if is_buy else 0.0
        # Only the buy path needs the earnings + sector context.
        days_to_earnings = (
            self.earnings.days_until_earnings(proposal.symbol) if is_buy else None
        )
        sector, sector_exposure = self._sector_context(proposal.symbol, account) \
            if is_buy else (None, 0.0)
        # Pairwise-correlation context (R.2) — only when the guard is on and
        # this is a buy; the fetches are per-cycle cached in the guard.
        max_corr, corr_sym, corr_missing = (
            self._corr_context(proposal.symbol, account)
            if is_buy and self.cfg.risk.max_pairwise_corr > 0 else (None, "", False)
        )
        # The cited thesis families behind a BUY, for the expectancy gate —
        # the same parse the attribution layer scores realized trips with.
        fams = parse_cited(proposal.key_signals) if is_buy else set()
        d_etf = getattr(self.cfg, "defensive_core_etf", "")
        d_pos = account.position_for(d_etf) if d_etf else None
        decision = self.risk.evaluate(
            proposal, account, price, vol, pending, days_to_earnings,
            sector, sector_exposure, self._regime_mult,
            max_held_corr=max_corr, corr_symbol=corr_sym,
            cycle_budget_cap=cycle_budget_cap,
            corr_data_missing=corr_missing,
            tech=tech, composite_score=composite,
            regime_label=getattr(self, "_regime_label", ""),
            entry_families=fams or None,
            neg_families=getattr(self, "_neg_families", None) or None,
            defensive_exempt_usd=(
                max(0.0, d_pos.market_value) if d_pos is not None else 0.0
            ),
        )
        if proposal.action.value == "hold":
            # A HOLD is the model saying "no action" — the risk layer returns
            # REJECTED so nothing executes, but logging it as "REJECT hold"
            # read like an error for a no-op. One quiet line instead.
            log.info("HOLD %s (no action) | %s",
                     proposal.symbol, proposal.rationale[:100])
        else:
            log.info(
                "%s %s -> %s: %s | %s",
                proposal.action.value.upper(), proposal.symbol,
                decision.verdict.value, decision.reason, proposal.rationale[:100],
            )
        # Journal every verdict so rejects have a durable record (not just a log
        # line), and the 'Today so far' block can surface them to Claude next cycle.
        instr = proposal.instrument.value if hasattr(proposal.instrument, "value") else str(proposal.instrument)
        verdict_str = decision.verdict.value
        if decision.verdict == RiskVerdict.REJECTED:
            self._journal_decision(
                proposal.symbol, proposal.action.value, instr,
                proposal.conviction, proposal.target_weight_pct, verdict_str,
                0.0,
                decision.reason, proposal.rationale[:120] if proposal.rationale else "",
            )
            # Same-cycle option fallback (Jul 28): a buy that died at an
            # EQUITY-only gate (overextension / earnings blackout) is the
            # sanctioned capped-debit call setup — evaluate_option exempts
            # both gates. Queue it for a scoped follow-up decision AFTER the
            # main proposal loop; next-cycle journal memory alone never
            # converted (slate rotates, idea decays, model picks fresh names).
            # Calls trade WITH the tape only, so skip in a down-trend market.
            if (
                is_buy
                and self.options is not None
                and self._regime_trend != "down"
                and decision.reason.startswith(("Overextended", "Earnings in"))
            ):
                self._option_fallbacks.append((proposal, decision.reason))
            return 0.0
        dropped_notional = 0.0
        # Journaled dollars: sells keep the approved figure; buys are journaled
        # AFTER submit with the ACTUAL submitted notional — the whole-share
        # bracket path floors the sized qty, and journaling the intent made the
        # postmortem/'Today so far' see $2,001 deployed when $1,154 was.
        executed_notional = decision.approved_notional
        # Hold the trade lock across broker mutations so the watchdog thread can't
        # interleave an emergency close on the same symbol mid-operation.
        with self._trade_lock:
            if proposal.action.value == "sell":
                # Fresh read: the close ladder branches on qty_available, and
                # the cycle-start snapshot is minutes old after the LLM call.
                held = self.broker.open_position(proposal.symbol)
                if held is None or held.qty <= 0:
                    log.warning(
                        "SELL %s: no open position at the broker — nothing to "
                        "close (already exited?).", proposal.symbol,
                    )
                    executed_notional = 0.0
                else:
                    outcome, oid = self.watchdog.close_now(held, "decision")
                    if outcome == "full":
                        self.watchdog.forget(proposal.symbol)
                        # The held position's unrealized P&L at close IS the
                        # realized outcome — record it so this round-trip is
                        # attributable.
                        self.ledger.record(TradeRecord.for_sell(
                            proposal.symbol, proposal.rationale, oid,
                            qty=held.qty, key_signals=proposal.key_signals,
                            realized_pl_pct=held.unrealized_pl_pct,
                            realized_pl=held.unrealized_pl,
                            exit_reason="decision",
                            exit_price=held.current_price or None,
                            composite_score=composite,
                        ))
                        self._pending_oids.append((oid, proposal.symbol))
                        # Persist at submit (mirror the watchdog) — crash-safe fill-check.
                        self.state.add_pending_order(oid, proposal.symbol)
                        # Start the re-entry cooldown clock (churn guard) — with
                        # the exit price so the price-aware re-entry guard can
                        # block a re-buy above where we just sold, and the P&L
                        # so the loss-streak scorecard sees the trip.
                        self.state.register_exit(
                            proposal.symbol, price=held.current_price or None,
                            pl_pct=held.unrealized_pl_pct)
                        # Reflect the close in this cycle's snapshot so later
                        # proposals see the freed capital / slot.
                        self._apply_pending_close(account, proposal.symbol)
                    elif outcome == "partial":
                        # Exit in motion: live sell legs were replaced into
                        # marketable limits (ledgered inside close_now with
                        # their order ids, so reconcile can true them up).
                        # Keep watchdog tracking until the fills land; free
                        # the capital in this cycle's snapshot — marketable
                        # exits fill within ticks.
                        self.state.register_exit(
                            proposal.symbol, price=held.current_price or None,
                            pl_pct=held.unrealized_pl_pct)
                        self._apply_pending_close(account, proposal.symbol)
                    else:
                        # Nothing was ledgered and nothing must be: a phantom
                        # SELL with no order id is invisible to reconcile and
                        # poisons attribution forever (SPCX 2026-07-16).
                        # Regression 2026-07-20/23: this used to just log and
                        # wait for the NEXT hourly cycle to maybe re-propose
                        # the same sell, with no alert in between — a real
                        # thesis-break exit (COO) sat unretried for ~26h.
                        # Every OTHER exit path (watchdog trailing/premium/
                        # time-stop) retries every ~30s tick and pages on
                        # failure; queue the same retry here instead, keeping
                        # the original rationale so a later successful close
                        # still ledgers with real context.
                        self.state.queue_decision_sell(
                            proposal.symbol, proposal.rationale,
                            proposal.key_signals, composite,
                        )
                        log.error(
                            "SELL %s approved but the close FAILED — queued "
                            "for the watchdog to retry every tick.",
                            proposal.symbol,
                        )
                        self.alerter.critical(
                            f"decision-sell-fail:{proposal.symbol}",
                            f"{proposal.symbol} SELL approved but close FAILED",
                            f"The decision engine approved a SELL for "
                            f"{proposal.symbol} ({decision.reason[:160]}) but "
                            "the close order did not go through. Queued for "
                            "the watchdog to retry every "
                            f"~{self.cfg.monitor_interval_s}s until it "
                            "succeeds or the position is confirmed gone.",
                        )
                        executed_notional = 0.0
            else:  # buy (approved or resized)
                sub = self.broker.submit_from_decision(decision)
                executed_notional = sub.notional if sub.order_id else 0.0
                dropped_notional = sub.dropped_notional if sub.order_id else 0.0
                if sub.order_id:
                    # Record the SUBMITTED qty/notional, not the decision's: the
                    # whole-share bracket path floors the sized qty (2.5 sh -> 2),
                    # and the dropped remainder must not live on as phantom
                    # position/cost in the ledger or the capital snapshot.
                    self.ledger.record(TradeRecord.from_equity(
                        decision, price, sub.order_id,
                        entry_signals=signal_kinds or [],
                        submitted_qty=sub.qty, submitted_cost=sub.notional,
                        composite_score=composite))
                    self._pending_oids.append((sub.order_id, proposal.symbol))
                    # Persist at submit (mirror the watchdog) — crash-safe fill-check.
                    self.state.add_pending_order(sub.order_id, proposal.symbol)
                    # Start (or preserve) the hold clock for the deterministic
                    # time-stop (1B.4). register_entry only stamps a first entry.
                    self.state.register_entry(proposal.symbol)
                    # Stamp EVERY buy for the top-up spacing guard (churn guard)
                    # and record conviction for the top-up evidence gate (B4).
                    self.state.register_buy(
                        proposal.symbol, conviction=proposal.conviction,
                    )
                    # Daily concentration accumulator: count $ against the
                    # per-symbol daily ceiling (concentration guard A1).
                    self.state.register_daily_deploy(proposal.symbol, sub.notional)
                    # Fold this fill back into the once-per-cycle snapshot so the
                    # REST of the cycle's proposals treat the capital as deployed
                    # (1B.3 — closes the intra-cycle over-deploy hole).
                    self._apply_pending_buy(
                        account, proposal.symbol, sub.notional, price, sub.qty,
                    )
                    if sub.fractional:
                        # Fractional orders carry no exchange-side bracket, so the
                        # watchdog enforces the hard stop / take-profit instead.
                        self.state.register_exits(
                            proposal.symbol, decision.stop_loss_pct, decision.take_profit_pct,
                        )
                    # Every buy (bracketed whole-share ones included) records its
                    # planned stop WIDTH so the R-scaled trail geometry has the
                    # position's risk unit — `exits` stays fractional-only
                    # because it doubles as the hard-exit enforcement list.
                    self.state.register_stop_width(
                        proposal.symbol, decision.stop_loss_pct,
                    )
        self._journal_decision(
            proposal.symbol, proposal.action.value, instr,
            proposal.conviction, proposal.target_weight_pct, verdict_str,
            executed_notional,
            decision.reason, proposal.rationale[:120] if proposal.rationale else "",
        )
        return dropped_notional

    # -- options path (defined-risk, gated) -------------------------------- #
    def _handle_option(
        self, proposal: TradeProposal, account,
        signal_kinds: list[str] | None = None, tech: dict | None = None,
    ) -> None:
        if self.options is None:
            log.info("Option proposal for %s ignored: options disabled.", proposal.symbol)
            return
        premium = self.options.estimate_net_premium(proposal)
        min_leg = self.options.min_leg_premium(proposal)
        liquidity = self.options.leg_liquidity(proposal)
        # The NAME's own long-run read (its 200dma) from the technical signal
        # already computed for the anti-chase gate — a single-name breakdown
        # keeps its put candidacy even when the MARKET trend is up.
        name_trend = ""
        if tech:
            t_price, t_sma200 = tech.get("price"), tech.get("sma200")
            if t_price and t_sma200:
                name_trend = "down" if t_price < t_sma200 else "up"
        # Bearish-funnel bookkeeping (Jul 30): count every all-puts structure
        # proposed and approved, so put-path dormancy shows in the cycle log.
        is_put_play = bool(proposal.option_legs) and all(
            leg.right.lower().startswith("p") for leg in proposal.option_legs
        )
        if is_put_play:
            self._bear_puts_proposed = getattr(self, "_bear_puts_proposed", 0) + 1
        decision = self.risk.evaluate_option(
            proposal, account, premium, leg_liquidity=liquidity,
            min_leg_premium=min_leg,
            market_trend=self._regime_trend,
            regime_label=self._regime_label,
            regime_multiplier=self._regime_mult,
            name_trend=name_trend,
            sanctioned_hedge=(
                bool(self._hedge_symbol)
                and proposal.symbol == self._hedge_symbol
            ),
            # % distance from the 20d SMA (negative = below): the broken-
            # momentum put carve-out — NU/NOK-shaped names break hard while
            # still reading name_trend="up" on their 200dma.
            name_ext_pct=tech.get("ext_pct_sma20") if tech else None,
        )
        if is_put_play and decision.verdict != RiskVerdict.REJECTED:
            self._bear_puts_approved = getattr(self, "_bear_puts_approved", 0) + 1
        log.info(
            "OPTION %s %s -> %s: %s | %s",
            proposal.option_strategy, proposal.symbol,
            decision.verdict.value, decision.reason, proposal.rationale[:100],
        )
        # Journal every option verdict too — without this, rejects are invisible
        # to the 'Today so far' block and the nightly post-mortem.
        self._journal_decision(
            proposal.symbol, proposal.action.value, "option",
            proposal.conviction, proposal.target_weight_pct,
            decision.verdict.value,
            decision.approved_notional if decision.verdict != RiskVerdict.REJECTED else 0.0,
            decision.reason, proposal.rationale[:120] if proposal.rationale else "",
        )
        if decision.verdict == RiskVerdict.REJECTED:
            return
        legs = self.options.build_legs(proposal)
        with self._trade_lock:
            oid = self.broker.submit_option_legs(
                legs, qty=int(decision.approved_qty),
                est_premium_per_share=premium,
            )
        if oid:
            self.ledger.record(TradeRecord.from_option(
                decision, premium, oid, entry_signals=signal_kinds or []))
            self._pending_oids.append((oid, proposal.symbol))
            # Persist at submit (mirror the watchdog) — crash-safe fill-check.
            self.state.add_pending_order(oid, proposal.symbol)
            # Same churn bookkeeping as equity buys: top-up spacing + the
            # per-symbol daily budget both count option debits.
            self.state.register_buy(proposal.symbol, conviction=proposal.conviction)
            self.state.register_daily_deploy(
                proposal.symbol, decision.approved_notional,
            )
