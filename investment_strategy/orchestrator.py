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
from .portfolio.beta import BookBeta, hedge_signal, hedge_target_notional
from .decision import DecisionEngine
from .earnings import EarningsCalendar
from .execution import AlpacaClient, OptionsHelper
from .execution.alpaca_client import broker_5xx_status, broker_error_summary
from .execution.options import parse_occ
from .ledger import (
    SHADOW_STOP_FLOOR_PCT,
    EntryTape,
    TradeLedger,
    TradeRecord,
    floor_shadow_line,
    floor_survival_at_exit,
    shadow_stop_pct,
    would_haircut_usd,
)
from .models import (
    Action,
    Candidate,
    Instrument,
    OptionStrategy,
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
from .risk import RiskManager, format_topup_bar, topup_bar
from .risk import _TRADING_DAYS_SQRT  # 4a-16: the shadow stop uses risk's own sigma scale
from .regime import RegimeReader
from .reset import maybe_reset_on_account_change
from .screener import ScreenerAggregator
from .session_calendar import (
    MIN_SESSIONS,
    REFRESH_SPAN_DAYS,
    SessionCalendar,
    paging_overlap,
    set_active as set_active_session_calendar,
)
from .sectors import SectorMap
from .signals import SignalAggregator, SignalHistory
from .signals.composite import composite_score, perf_weights
from .signals.quiver_client import QuiverClient
from .state import PortfolioState
from .status import EquityHistory, compute_status

log = logging.getLogger("orchestrator")

# Transient network faults that already survived the broker's own retries. They're
# self-healing (the next tick reconnects), so they're logged as a one-line warning
# rather than a full traceback — a reset-by-peer isn't a bug to debug. A broker
# HTTP 5xx (alpaca APIError, classified by broker_5xx_status) is the same kind
# of blip and gets the same one-line treatment in _guarded_tick; it can't join
# this tuple because the SDK uses APIError for 4xx "your request is wrong" too.
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
        self.signal_history = SignalHistory(
            retention_days=getattr(cfg, "signal_history_retention_days", None),
            max_points=getattr(cfg, "signal_history_max_points", None),
        )
        self.screeners = ScreenerAggregator(cfg, self.quiver, broker=self.broker)
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
        # Book-beta reader (run-6 item 7): shares the correlation guard's
        # per-cycle return cache (no double fetch); ONE 'BOOK BETA:' line per
        # decision cycle, persisted to risk_state.json; feeds the buy-path
        # beta cap and the beta-sized auto-hedge.
        self.book_beta = BookBeta(
            self.broker, self.corr_guard,
            hedge_etf=getattr(cfg, "hedge_etf", "") or "",
        )
        self._book_beta_reading = None
        self._hedge_reason = ""   # 'beta:1.31' while the beta hedge is armed
        # Run-7 S-8: beta-mode unwind streak = (decision-cycle seq of the
        # last below-band read, consecutive below-band reads). Counted at
        # most once per cycle — the breadth re-arm re-runs the hedge inside
        # one cycle — and reset on arm / hold / unavailable. In-memory by
        # design (a restart only ever DELAYS an unwind by re-counting).
        self._unwind_reads = (-1, 0)
        # 4a-17: cycle seq the HEDGE COUNTERFACTUAL line was last logged on.
        self._cf_logged_cycle = -1
        # Per-cycle market-regime read; scales position size down in risk-off, and
        # DOWN (not full) when its yfinance feed is degraded — since that same
        # outage blinds the sector cap too (1B.7). S-5: the reader holds a
        # tighter label across cycles and loosens only after
        # REGIME_LOOSEN_MIN_CYCLES clean reads (Sep 10 2026 flap).
        self.regime = RegimeReader(
            degraded_mult=cfg.risk.regime_degraded_mult,
            loosen_min_cycles=getattr(cfg.risk, "regime_loosen_min_cycles", 2),
        )
        self._regime_mult = 1.0   # set each cycle from the regime read
        # Long-run direction ("up"/"down"/"") + blended label, set alongside the
        # multiplier each cycle — they drive the option call/put direction gate.
        self._regime_trend = ""
        self._regime_label = ""
        # True only on the cycle the regime label flipped INTO risk-off
        # (computed before the trim persists the new label) — a deterministic
        # event tag for the LLM sell-authority gate (run-6 item 2).
        self._regime_flipped_off = False
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
        # Put-liquidity proxy outcome this cycle (Aug 14): "" = not attempted.
        self._proxy_put_state = ""
        # Per-cycle put-gate precheck verdicts for bearish-composite slate
        # names (Jul 31): symbol -> (eligible, why). Rendered into the prompt's
        # BEARISH CANDIDATES block and the funnel line, so the 4->0 drop-off
        # decomposes into "gate-blocked (tape)" vs "eligible but declined".
        self._bear_eligibility: dict[str, tuple[bool, str]] = {}
        # Per-cycle model verdict per put-ELIGIBLE name (Aug 1): symbol ->
        # "put proposed" / "declined: ..." / "IGNORED", reconciled from the
        # schema-required bearish_verdicts output right after decide() and
        # appended to the funnel line's per-name stages.
        self._bear_verdict_stage: dict[str, str] = {}
        # Per-cycle NAME-level falling reads (Jul 30 review, Phase-1 gap):
        # held symbol -> "-5.2% today vs SPY +0.1%". Index-level defense
        # never fired on the actual loss days (Jul 29 bottomed -1.2% vs the
        # -1.5% trigger while NU/NOK broke -5% alone); this is the per-name
        # variant. Feeds the HELD prompt lines and the rotation guard's
        # loss-cut release.
        self._falling_names: dict[str, str] = {}
        # Breadth double-count guard (Aug 23): the falling-names map computed
        # in cycle N is re-read by cycle N+1's TOP-of-cycle defense pass (the
        # fresh map only exists once tech context lands mid-cycle), so the
        # breadth re-arm counting it in cycle N and the stale re-read counting
        # it again in cycle N+1 would satisfy auto_hedge_min_cycles=2 with ONE
        # observation. Track which map (by decision-cycle sequence) was
        # already counted toward the persistence bar; _market_falling's
        # breadth-names leg ignores a stale, already-counted map.
        self._cycle_seq = 0
        self._breadth_map_cycle = -1
        self._breadth_counted_cycle = -2
        # Run-7 S-7 cross-day stale-map reset (Sep 11 2026 08:30 ET): the
        # Sep 10 14:38 map {DRAM, INTC, SEI} was still the live map at the
        # next morning's first cycle (17h52m old — the fresh map only exists
        # once tech context lands mid-cycle) and fired the breadth leg on a
        # risk-on +0.9% open: a wrong-day core trim attempt and a hedge target
        # pinned at 0.80. The map now carries the ET date it was computed on;
        # _market_falling's breadth-names leg ignores a map from a PREVIOUS
        # session (the deliberate within-day carry above stays). The stamp
        # ("2026-09-10 14:38") feeds the counterfactual line.
        self._breadth_map_date = ""
        self._breadth_map_stamp = ""
        self._breadth_stale_map = False
        self._stale_map_logged = -1
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
        # Cached exchange calendar (run-7 A2): the paging window and the
        # dead-man used to be weekday clock math, so Labor Day 2026-09-07
        # produced 75 CRITICAL 'positions unwatched during market hours'
        # pages for a closed market. Loaded from state/session_calendar.json
        # (survives restart), refreshed once per ET date by the DECISION loop
        # (_refresh_session_calendar) and only ever READ on the watchdog
        # thread via the module-level active calendar (no network there).
        self._session_calendar = SessionCalendar(
            getattr(cfg, "session_calendar_file", "") or None)
        set_active_session_calendar(self._session_calendar)

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
                self._guarded_tick()
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

    def _guarded_tick(self) -> None:
        """One main-loop tick with the failure policy applied (extracted from
        run() so the handler is unit-testable). Three classes of failure:
          - transient network fault (_TRANSIENT_NET): one WARNING, next tick
            retries;
          - broker HTTP 5xx (alpaca APIError classified by broker_5xx_status):
            one WARNING per tick, NO traceback, consecutive ticks counted and
            paged at doubling rungs by _note_broker_5xx_tick — Sep 11 2026:
            /v2/clock 500'd for eight 30 s ticks and each one logged a 37-line
            traceback at ERROR as if the bot had a bug;
          - anything else (4xx APIError included — the request is wrong):
            log.exception with the full traceback, unchanged.
        A tick that returns cleanly ends any 5xx run."""
        try:
            self._tick()
            self._broker_5xx_ticks = 0     # broker answered: the run is over
        except _TRANSIENT_NET as e:
            log.warning(
                "Decision tick skipped on a transient network error (%s); "
                "retrying next tick.", e.__class__.__name__,
            )
        except Exception as e:  # noqa: BLE001 — the loop must survive any tick
            if broker_5xx_status(e) is not None:
                self._note_broker_5xx_tick(e)
            else:
                log.exception("Decision tick failed; continuing.")

    #: consecutive decision ticks aborted by a broker 5xx before we page (the
    #: watchdog-blind style: first rung here, then every doubling — 10, 20,
    #: 40 … ticks; ~5 min of a dead broker API at the 30 s tick).
    BROKER_5XX_ESCALATE = 10
    #: class-level defaults so bare Orchestrator.__new__ fixtures need no init.
    _broker_5xx_ticks = 0
    _broker_5xx_paged_at = 0

    def _note_broker_5xx_tick(self, e: Exception) -> None:
        """Record one decision tick lost to a broker HTTP 5xx: a single
        greppable WARNING (no traceback — the SDK/requests frames say nothing
        the status line doesn't) and a consecutive-tick counter that pages
        'Broker DOWN' in-hours at the doubling rungs of the run
        (BROKER_5XX_ESCALATE, x2, x4 …), mirroring _maybe_page_on_skip_run so
        a long outage can't storm the log or the alerter queue. The cadence
        is untouched: the failed cycle stays due and the next tick retries.
        Overnight runs stay silent (nothing to trade). Best-effort."""
        self._broker_5xx_ticks += 1
        n = self._broker_5xx_ticks
        status = broker_5xx_status(e)
        log.warning(
            "Decision tick skipped on broker HTTP %s (%s); %d in a row — "
            "retrying next tick.", status, broker_error_summary(e), n,
        )
        if n < self.BROKER_5XX_ESCALATE:
            self._broker_5xx_paged_at = 0     # a fresh run: page at its first rung
            return
        if self._broker_5xx_paged_at and n < self._broker_5xx_paged_at * 2:
            return                            # between rungs
        now = time.time()
        if not self._overlaps_paging_hours(now, now):
            return
        self._broker_5xx_paged_at = n
        secs = n * self.cfg.monitor_interval_s
        log.critical(
            "Broker DOWN: %d consecutive decision ticks failed on HTTP %s (~%.0fs) "
            "— no decision cycle can run during market hours (next page at %d "
            "ticks).", n, status, secs, n * 2,
        )
        paged = self.alerter.critical(
            "broker_5xx",
            f"Broker API down: HTTP {status} for {n} ticks (~{secs:.0f}s)",
            "Alpaca has answered the decision loop's reads with a server error "
            f"({broker_error_summary(e)}) for {n} consecutive ticks. No decision "
            "cycle can run until it recovers; the watchdog keeps its own reads. "
            "Check https://status.alpaca.markets before touching the bot.",
            severity=float(n),
        )
        if not paged:
            log.warning(
                "Broker DOWN page at %d ticks held by the alerter "
                "(cooldown/backoff) — logged only.", n,
            )

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
            except Exception as e:  # noqa: BLE001 — the safety loop must survive any tick
                if broker_5xx_status(e) is not None:
                    # A broker 5xx blinds the safety loop exactly like a
                    # network fault (the reads inside check_once went through
                    # _retry_read and still failed): count it toward the
                    # watchdog-blind rungs — a pure-5xx outage (Sep 11 shape)
                    # used to log a traceback per tick and never page.
                    self._watchdog_skips += 1
                    log.warning(
                        "Watchdog tick skipped on broker HTTP %s (%s); retrying "
                        "next tick (%d in a row).", broker_5xx_status(e),
                        broker_error_summary(e), self._watchdog_skips,
                    )
                    self._maybe_page_on_skip_run()
                else:
                    log.exception("Watchdog tick failed; continuing.")
            self._stop.wait(self.cfg.monitor_interval_s)

    #: consecutive watchdog skips (network) before we page the safety loop is blind.
    WATCHDOG_SKIP_ESCALATE = 5
    #: skip count at which the current run last paged (0 = not yet). Class-level
    #: default so bare Orchestrator.__new__ fixtures need no init; the instance
    #: attribute shadows it once a run pages.
    _blind_paged_at = 0

    def _maybe_page_on_skip_run(self) -> None:
        """Page when the watchdog has skipped WATCHDOG_SKIP_ESCALATE ticks in a
        row on network errors during market hours — the safety loop can't see the
        book. Only fires in-hours (an overnight outage strands nothing) and only
        at the DOUBLING rungs of the run — 5, 10, 20, 40, 80 … in-hours ticks
        (or the first in-hours tick past 5 of a run that began overnight, then
        2x, 4x …). Sep 7 2026: paging every tick past 5 put 75 CRITICALs in
        the log in 56 min while DNS was dead (0 delivered; the alerter's
        failed sends un-stamped its throttle, so each tick retried). The rungs
        are exactly what the alerter's SEVERITY_ESCALATION=2.0 admits with a
        working sink anyway (one page per doubling of the outage — 5 in the
        first hour at a ~47s tick), so gating here costs no page a human would
        have received and stops the log/queue churn. The per-tick WARNING in
        _watchdog_loop keeps the count visible between rungs. Best-effort."""
        skips = self._watchdog_skips
        if skips < self.WATCHDOG_SKIP_ESCALATE:
            self._blind_paged_at = 0      # a fresh run: page at its first rung
            return
        if self._blind_paged_at and skips < self._blind_paged_at * 2:
            return                        # between rungs
        now = time.time()
        if not self._overlaps_paging_hours(now, now):
            return
        self._blind_paged_at = skips
        secs = skips * self.cfg.monitor_interval_s
        log.critical(
            "Watchdog BLIND: %d consecutive ticks failed (~%.0fs) — positions "
            "unwatched during market hours (next page at %d ticks).",
            skips, secs, skips * 2,
        )
        paged = self.alerter.critical(
            "watchdog_blind",
            f"Watchdog blind for {skips} ticks (~{secs:.0f}s)",
            "The safety loop has failed to read the account for several ticks in "
            "a row (network). Stops/floor/flatten can't fire while it's blind. "
            "Check the host's connectivity.",
            severity=float(skips),
        )
        if not paged:
            # Alerter held it (cooldown, or every sink is down and the key is in
            # its retry backoff — the page is spooled for the catch-up summary).
            log.warning(
                "Watchdog BLIND page at %d ticks held by the alerter "
                "(cooldown/backoff) — logged only.", skips,
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

    #: class-level default so bare Orchestrator.__new__ fixtures need no init;
    #: __init__ replaces it with the loaded SessionCalendar.
    _session_calendar: SessionCalendar | None = None

    @staticmethod
    def _overlaps_paging_hours(start_ts: float, end_ts: float,
                               calendar: SessionCalendar | None = None) -> bool:
        """True when any part of wall-clock [start_ts, end_ts] falls inside the
        paging window: (session open - 5min, close + 5min) ET per the cached
        exchange calendar (holidays and early closes honoured; the same file
        ops/deadman.py reads), or weekday 09:25-16:05 ET for any date the
        cache doesn't cover. Checked at both endpoints plus each session open
        inside the span, so a multi-day gap can't thread between samples.
        Pure clock math over the cache — no network — because this runs on
        the watchdog thread. `calendar` defaults to the module-level active
        calendar registered by __init__ (tests pass one explicitly); before
        run-7 this was weekday math only, which is how Labor Day 2026-09-07
        paged 75 times for a closed market."""
        return paging_overlap(start_ts, end_ts, calendar)

    def _refresh_session_calendar(self) -> None:
        """DECISION-thread only: once per ET date, pull the exchange calendar
        for today +/- REFRESH_SPAN_DAYS and persist it (state/session_calendar
        .json) for the watchdog's paging window and ops/deadman.py. A failed or
        rejected fetch keeps the previous cache (weekday math for dates it
        doesn't cover) and retries on the next decision cycle; the watchdog
        never fetches. Best-effort — never raises into the cycle."""
        cal = getattr(self, "_session_calendar", None)
        if cal is None:
            return
        try:
            today = datetime.now(ZoneInfo("America/New_York")).date()
            if not cal.needs_refresh(today):
                return
            fetch = getattr(self.broker, "get_session_calendar", None)
            if not callable(fetch):
                return
            start = today - timedelta(days=REFRESH_SPAN_DAYS)
            end = today + timedelta(days=REFRESH_SPAN_DAYS)
            sessions = fetch(start, end)
            if not sessions:
                log.warning(
                    "Session calendar refresh failed for %s..%s; keeping cached "
                    "%s..%s (weekday paging math outside it); retry next cycle.",
                    start, end, cal.start, cal.end,
                )
                return
            if cal.update(sessions, start, end, today):
                log.info(
                    "Session calendar refreshed: %d session(s) %s..%s -> %s.",
                    len(sessions), start, end, cal.path,
                )
            else:
                log.warning(
                    "Session calendar response rejected (%d session(s) for %s..%s, "
                    "floor %d); keeping cached %s..%s.",
                    len(sessions), start, end, MIN_SESSIONS, cal.start, cal.end,
                )
        except Exception as e:  # noqa: BLE001 — advisory cache, never blocks a cycle
            log.warning("Session calendar refresh error: %s", e)

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
        # Before the closed-market early return so holiday/overnight ticks
        # refresh the paging calendar too (once per ET date, decision thread).
        # While the fetch keeps failing it re-runs every cycle (3 tries x the
        # HTTP timeout + 5xx sleeps, ~60 s worst case), so stamp liveness
        # right after it: the span is otherwise unstamped between _tick's
        # stamp and the first in-cycle one.
        self._refresh_session_calendar()
        self._stamp_liveness()
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
        self._drain_floor_shadow_jobs()
        self._stamp_liveness()
        # Close fence (CRITICAL-1): reconcile/backfill above still run near the
        # bell, but don't START a fresh decision inside the final N minutes — a
        # buy placed this late can't complete before close, and the model
        # churning right before the bell is low-value. Positions stay
        # watchdog-protected; the post-LLM re-check below catches a close that
        # lands mid-cycle.
        if self._within_close_fence():
            return
        # Advance the decision-cycle sequence (breadth double-count guard):
        # anything stamped with an older seq — notably the falling-names map —
        # is a STALE read from a previous cycle.
        self._cycle_seq = getattr(self, "_cycle_seq", 0) + 1
        # Fresh Quiver data this cycle, but pulled once and shared by the signal
        # and screener layers (both read the same cached live feeds).
        self.quiver.new_cycle()
        self.earnings.new_cycle()
        self.sectors.new_cycle()
        self.corr_guard.new_cycle()
        if getattr(self, "book_beta", None) is not None:
            self.book_beta.new_cycle()
        self.regime.new_cycle()
        if self.cfg.risk.regime_filter_enabled:
            regime = self.regime.assess()
            self._regime_mult = regime.multiplier
            self._regime_trend = regime.trend
            self._regime_label = regime.label
            try:
                self._regime_flipped_off = (
                    regime.label == "risk-off"
                    and self.state.get_regime_label() != "risk-off"
                )
            except Exception:  # noqa: BLE001 — a tag, never a cycle blocker
                self._regime_flipped_off = False
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
            self._regime_flipped_off = False
        self._stamp_liveness()  # regime read done (a few benchmark fetches)
        self._record_equity_snapshot()
        account = self.broker.get_account()
        # Ex-ante exposure read (run-6 item 7a) — before any defense acts, so
        # the line records what the book carried INTO the cycle. The reader
        # stamps liveness per symbol (Sep 11: a 163 s beta fan-out under an
        # Alpaca degradation withheld the heartbeat twice) and the stage is
        # stamped once more here so a disabled reader leaves no gap.
        self._read_book_beta(account)
        self._stamp_liveness()

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
                # Run-6: realized perf tilt frozen unless COMPOSITE_PERF_WEIGHTS.
                pw = perf_weights(
                    self.ledger, self.cfg.risk.composite_perf_min_trips,
                    enabled=bool(getattr(
                        self.cfg.risk, "composite_perf_weights", False)),
                )
                # Run-6: the scanner's DISCOVERY line stays in the prompt but
                # no longer scores into the index (COMPOSITE_INCLUDE_DISCOVERY).
                inc_disc = bool(getattr(
                    self.cfg.risk, "composite_include_discovery", False))
                for b in bundles:
                    b.composite_score = composite_score(
                        b, pw, include_discovery=inc_disc)
                composites = {
                    b.symbol: b.composite_score
                    for b in bundles if b.composite_score is not None
                }
                if composites:
                    top = sorted(
                        composites.items(), key=lambda kv: kv[1], reverse=True
                    )[:8]
                    # Bearish tail too (Jul 31): the funnel counts names with
                    # composite <= -bar, but a top-8-only log never showed WHO
                    # they were — the 4->0 autopsy had to guess identities.
                    _bar = self.cfg.screener.bearish_reserve_bar or 0.4
                    tail = sorted(
                        (kv for kv in composites.items() if kv[1] <= -_bar),
                        key=lambda kv: kv[1],
                    )[:6]
                    log.info(
                        "Composite index (%d scored, top: %s%s).",
                        len(composites),
                        ", ".join(f"{s} {v:+.2f}" for s, v in top),
                        (
                            "; bear tail: "
                            + ", ".join(f"{s} {v:+.2f}" for s, v in tail)
                            if tail else ""
                        ),
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

        # Per-NAME falling reads for this cycle: defense that doesn't wait
        # for an index-wide day. Feeds the HELD prompt lines and the rotation
        # guard's loss-cut release below.
        self._falling_names = self._name_falling_reads(account, tech_ctx)
        # Stamp the map with the cycle it was computed in: a map carried into
        # the NEXT cycle's top-of-cycle defense pass is stale, and if the
        # re-arm below already counted it toward the hedge persistence bar it
        # must not count twice (see _market_falling's breadth-names leg).
        self._breadth_map_cycle = self._cycle_seq
        # ...and with the ET session it belongs to (run-7 S-7): a map carried
        # across the overnight into the next day's first cycle is not a read
        # of today's tape and must not trim the core or tighten the hedge.
        _now_et = self._et_now()
        self._breadth_map_date = _now_et.date().isoformat()
        self._breadth_map_stamp = _now_et.strftime("%Y-%m-%d %H:%M")
        for _s, _why in self._falling_names.items():
            log.info("NAME FALLING: %s %s — defense read armed "
                     "(loss-cut release + HELD-line note).", _s, _why)
        # Aug-22 breadth trigger: the defenses above ran on LAST cycle's
        # falling-names map (this one only exists once the tech context
        # lands). If the fresh map alone crosses the breadth bar, re-run
        # them now so an Aug-18-shaped day acts this hour, not next cycle.
        self._breadth_rearm(account)

        # Expectancy gate input (Jul 30 review): signal families whose CITED
        # trailing realized expectancy is negative. Recomputed once per cycle
        # from the ledger; the risk layer blocks FRESH entries whose thesis
        # rests entirely on these families. Run-6 (item 6): with the gate
        # OFF the read still happens and is logged as 'would have armed'
        # (report-only) but {} is handed to the risk layer, so nothing rejects.
        self._neg_families = self._expectancy_gate_read()

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
        # Put-gate PRECHECK per bearish-composite slate name (Jul 31 funnel
        # autopsy): the prompt used to warn "puts are auto-rejected unless the
        # name is breaking down" without saying WHICH names would pass, so the
        # model held every bearish read (slate_bearish>0, put_proposals=0 all
        # week). Run the deterministic gate preview here — same carve-outs as
        # risk._direction_fits_market — and hand the verdicts to the prompt
        # and the funnel line. Only ON-SLATE names: the model must never be
        # asked to trade a name whose data was partitioned out of the prompt.
        self._bear_eligibility = {}
        if self.options is not None:
            for _sym in self._bear_precheck_names(bundles, composites or {}):
                self._bear_eligibility[_sym] = self.risk.put_precheck(
                    _sym, account,
                    (_reg.trend if _reg else ""),
                    (_reg.label if _reg else ""),
                    tech_ctx.get(_sym),
                )
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
            put_eligibility=self._bear_eligibility,
        )
        self._stamp_liveness()
        # Reconcile the schema-required bearish_verdicts against this cycle's
        # ELIGIBLE names BEFORE any filtering drops proposals — a put the
        # slate filter later removes still counts as "the model proposed".
        self._reconcile_bear_verdicts(proposals)
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
        # 4a-17: the book's beta AFTER execution, beside the pre-exec line
        # the hedge sized against (ordering gap made countable; no action).
        self._log_post_exec_beta(account)
        # Bearish funnel (Jul 30 review): the put path was dormant for 388
        # straight trades and nothing surfaced it. One line per cycle makes
        # downside-conviction leakage visible — see _log_bear_funnel for the
        # per-name stage rendering and its truncation rules (Sep 12: every
        # ELIGIBLE name is rendered; only gate-blocked/off-slate names cap).
        bear_bar = self.cfg.screener.bearish_reserve_bar or 0.4
        bear_map = {s: v for s, v in composites.items() if v <= -bear_bar}
        self._log_bear_funnel(bear_map, account)
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
        apart from "RH is dead" instead of the signals silently vanishing.

        Aug-22 review (rank 8): RH OAuth died Aug 19 mid-window and 2 of 5
        SCREENER_SOURCES silently vanished — the eval window was confounded
        and nothing flagged it. Every decision cycle now also logs one FEEDS
        line asserting the health of ALL configured screener sources.

        Run-7 (A7): the line also carries 'earnings=<rh|yfinance-fallback|
        none>' — Sep 10 RH died and the earnings-blackout gate ran on
        yfinance for 12 cycles behind a '3/3 healthy' line. 'earnings=none'
        (no source at all; the gate is blind) logs at WARNING; the
        yfinance fallback itself is announced by earnings.py's own once-per-
        cycle line (WARNING when RH was expected), so FEEDS stays INFO."""
        feeds = self._feed_health_line()
        if feeds:
            if "DEAD" in feeds or "UNHEALTHY" in feeds or "earnings=none" in feeds:
                log.warning("%s", feeds)
            else:
                log.info("%s", feeds)
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

    def _feed_health_line(self) -> str:
        """Per-cycle screener-feed liveness summary: 'FEEDS: 5/5 healthy' or
        'FEEDS: 3/5 — robinhood DEAD (oauth), ... — EVAL WINDOW VALIDITY AT
        RISK'. Derived READ-ONLY from what the aggregator already exposes:
        a configured source whose screener `enabled` gate is False is DEAD
        this cycle (safe_scan skips it and its candidates silently vanish);
        RH-backed sources report the dead-auth latch as (oauth). No new
        probes, no network calls. Best-effort — never breaks a cycle.

        Run-6 (FEEDS_DEGRADED_MODES on): a source that is enabled but whose
        pull FAILED this cycle (screener.degraded, e.g. the EDGAR Form-4 feed
        timing out with 0 rows) counts as 'UNHEALTHY (<reason>)' in the n/n,
        and 'news=vader-fallback' is appended once news.py has latched the
        Finnhub 403 — so 'FEEDS: 5/5 healthy' means what it says.

        Run-7 (A7): under the same knob the line ends with
        'earnings=<rh|yfinance-fallback|none>' — the source the earnings-
        blackout gate consults this cycle (EarningsCalendar.source(): the RH
        reader's enabled flag/dead-auth latch + the last calendar read's
        outcome; no probe). RH is not a SCREENER_SOURCE in run-6/7, so the
        n/n alone could not see the Sep 10 RH outage."""
        try:
            sources = list(getattr(self.cfg.screener, "sources", ()) or ())
            if not sources:
                return "FEEDS: 0/0 configured"
            degraded_modes = bool(getattr(self.cfg, "feeds_degraded_modes", False))
            by_name = {
                s.name: s for s in getattr(self.screeners, "screeners", [])
            }
            rh_dead = RobinhoodReader.auth_dead()
            dead: list[str] = []
            for src in sources:
                scr = by_name.get(src)
                if scr is None:
                    dead.append(f"{src} DEAD (unknown source)")
                    continue
                try:
                    ok = bool(scr.enabled)
                except Exception:
                    ok = False
                if ok:
                    why = getattr(scr, "degraded", None) if degraded_modes else None
                    if why:
                        dead.append(f"{src} UNHEALTHY ({why})")
                    continue
                if src in ("robinhood", "robinhood_scans") and rh_dead:
                    dead.append(f"{src} DEAD (oauth)")
                else:
                    dead.append(f"{src} DEAD (disabled/no credentials)")
            suffix = " news=vader-fallback" if (
                degraded_modes and self._news_vader_fallback()
            ) else ""
            if degraded_modes:
                suffix += self._earnings_source_token()
            total = len(sources)
            if not dead:
                return f"FEEDS: {total}/{total} healthy{suffix}"
            return (
                f"FEEDS: {total - len(dead)}/{total} — " + ", ".join(dead)
                + " — EVAL WINDOW VALIDITY AT RISK" + suffix
            )
        except Exception as e:
            log.debug("feed health line failed: %s", e)
            return ""

    def _earnings_source_token(self) -> str:
        """' earnings=<rh|yfinance-fallback|none>' for the FEEDS line, or ''
        when no calendar is wired (test doubles). Read-only and best-effort:
        a failure here drops the token, never the FEEDS line."""
        cal = getattr(self, "earnings", None)
        if cal is None:
            return ""
        try:
            return f" earnings={cal.source()}"
        except Exception as e:  # noqa: BLE001
            log.debug("earnings source token failed: %s", e)
            return ""

    def _news_vader_fallback(self) -> bool:
        """True once the news provider has latched the Finnhub 403 (sentiment
        = VADER for the rest of the process). Read-only; fails False."""
        try:
            for p in getattr(self.signals, "per_symbol", []) or []:
                if getattr(p, "name", "") == "news":
                    return bool(getattr(p, "_finnhub_gated", False))
        except Exception:
            pass
        return False

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
            self.equity_history.snapshot(compute_status(self.broker), basis="intraday")
        except Exception as e:
            log.warning("Could not record equity snapshot: %s", e)

    def _refresh_closing_snapshot(self, now_et: datetime | None = None) -> None:
        """Market-closed tick: stamp the day's CLOSE equity row.

        Run-6 item 1b (EQUITY_CLOSE_FIXED_STAMP, default on): the row is
        written ONCE, at the first closed tick at/after 16:00 ET on a weekday,
        keyed by the ET date with basis='close', and never overwritten — the
        legacy path re-stamped it on every closed tick with after-hours marks
        (Aug 24 row moved $346 between the bell and 23:56Z; Aug 25 row moved
        from $1,006,145 to $1,007,879), which broke day_pl telescoping.
        Legacy path (knob off): overwrite today's UTC-dated row on every
        closed tick, only when a row already exists for it."""
        try:
            if getattr(self.cfg, "equity_close_fixed_stamp", True):
                et = now_et or datetime.now(ZoneInfo("America/New_York"))
                if et.weekday() >= 5 or (et.hour * 60 + et.minute) < 16 * 60:
                    return
                day = et.date().isoformat()
                if self.equity_history.has_close_row(day, ("close", "late")):
                    return
                # Review fix (Aug 26): a holiday inside the window (Labor Day
                # Sep 7) is not a session — no row at all (a phantom
                # day_pl=0 session would enter max_drawdown/worst_day).
                # Calendar read failure = None = fail open (stamp).
                is_day = getattr(self.broker, "is_trading_day", None)
                if callable(is_day) and is_day(et.date()) is False:
                    log.info("Equity close row skipped for %s (not a session).", day)
                    return
                bb = getattr(self, "_book_beta_reading", None)
                extra = (
                    {"book_beta_spy": bb.spy}
                    if bb is not None and getattr(bb, "spy", None) is not None
                    else None
                )  # item 8: ex-ante beta on the close row (eval beta-adjust)
                # Only the 16:xx ET tick may mint the immutable 'close' row;
                # a bot (re)started later stamps after-hours marks and says
                # so (basis='late') so the checker can see the row is not a
                # bell mark. Also written once, never overwritten.
                basis = "close" if et.hour == 16 else "late"
                self.equity_history.snapshot(
                    compute_status(self.broker), basis=basis, day=day,
                    extra=extra)
                log.info("Equity close row stamped for %s (basis=%s).", day, basis)
                return
            rows = self.equity_history.all()
            today = datetime.now(timezone.utc).date().isoformat()
            if rows and rows[-1].get("date") == today:
                self.equity_history.snapshot(compute_status(self.broker))
        except Exception as e:
            log.warning("Closing equity snapshot failed: %s", e)

    def _expectancy_gate_read(self) -> dict:
        """Negative-expectancy family set for this cycle. Gate ON -> the set
        (logged 'armed against'); gate OFF -> still computed and logged once
        per cycle as 'would have armed against' so the next review can see
        what it would have blocked, but {} is returned (never rejects).
        Best-effort — any failure disarms."""
        r = self.cfg.risk
        try:
            neg = negative_expectancy_families(
                self.ledger,
                window_days=r.expectancy_gate_window_days,
                min_trips=r.expectancy_gate_min_trips,
            )
        except Exception as e:
            log.warning("Expectancy gate read failed: %s", e)
            return {}
        if not neg:
            return {}
        desc = ", ".join(
            f"{k} {v.avg_pl_pct:+.1f}%/trip x{v.trips}"
            for k, v in sorted(neg.items())
        )
        if r.expectancy_gate_enabled:
            log.info("Expectancy gate armed against: %s", desc)
            return neg
        log.info(
            "Expectancy gate (off, report-only): would have armed against: %s",
            desc,
        )
        return {}

    def _attribution_lessons(self) -> str:
        """Per-cycle track-record block (attribution.py) — recomputed every
        cycle and changes whenever a position closes, so it stays in the
        decision prompt's DYNAMIC half (see engine._render_dynamic)."""
        try:
            return render_lessons(
                self.ledger,
                min_source_trips=int(getattr(
                    self.cfg, "track_record_min_trips", 20)),
            )
        except Exception as e:
            log.warning("Could not render track-record lessons: %s", e)
            return ""

    def _curated_lessons(self) -> str:
        """Nightly post-mortem's curated lessons file — changes at most once a
        day, so it belongs in the decision prompt's STABLE/cached half (see
        engine._render_stable), separate from _attribution_lessons above.
        Run-6: injected only when CURATED_LESSONS_INJECT is on (default off);
        the nightly post-mortem keeps writing the file regardless."""
        if not bool(getattr(self.cfg, "curated_lessons_inject", False)):
            return ""
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
        a fresh bundle (gather drops empty ones) so they're still evaluated.
        Run-6: the line is prompt text + bearish-lean input only — it is no
        longer a scored term of the composite (see composite_score)."""
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

    def _option_fallback_allowed(self, symbol: str) -> bool:
        """Is a same-cycle CALL fallback on `symbol` even admissible under the
        single-name bullish gate? True when the knob is on, or the underlying
        is an index the gate exempts (SPY/QQQ/IWM/DIA + the configured
        core/hedge/proxy/defensive ETFs)."""
        r = getattr(getattr(self, "cfg", None), "risk", None)
        if getattr(r, "options_single_name_bullish", True):
            return True
        from .risk import _INDEX_UNDERLYINGS
        sym = (symbol or "").upper()
        cfg = getattr(self, "cfg", None)
        idx = {
            str(x).upper() for x in (
                getattr(cfg, "core_etf", ""), getattr(cfg, "hedge_etf", ""),
                getattr(cfg, "put_proxy_etf", ""),
                getattr(cfg, "defensive_core_etf", ""),
                getattr(self, "_hedge_symbol", ""),
            ) if x
        }
        return sym in _INDEX_UNDERLYINGS or sym in idx

    def _stamp_fill(self, oid: str, symbol: str, filled: float,
                    detail: dict | None = None) -> dict | None:
        """Run-6 item 1e: pull the broker's filled_avg_price / qty / time for a
        FILLED order and write them onto the ledger row (TradeLedger.set_fill).
        `detail` is the fill dict reconcile already got from order_fill_full
        (one REST read per order — review fix); without it the legacy
        order_fill_detail read runs. Returns the fill dict (with 'stamped')
        or None when disabled / not readable. Best-effort — never raises."""
        if not getattr(self.cfg, "ledger_fill_prices", True):
            return None
        reader = getattr(self.broker, "order_fill_detail", None)
        if detail is None and not callable(reader):
            return None
        try:
            fill = dict(detail) if detail is not None else (reader(oid) or {})
            price = float(fill.get("price") or 0.0)
            if price <= 0:
                return None
            fill_qty = float(fill.get("qty") or filled or 0.0)
            fill["qty"] = fill_qty
            setter = getattr(self.ledger, "set_fill", None)
            fill["stamped"] = bool(
                callable(setter)
                and setter(oid, price, fill_qty, fill.get("filled_at"))
            )
            return fill
        except Exception as e:  # noqa: BLE001 — bookkeeping only
            log.warning("Fill-price stamp failed for %s (%s): %s", oid, symbol, e)
            return None

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
            full = getattr(self.broker, "order_fill_full", None)
            detail = None
            if callable(full):
                status, filled, qty, detail = full(oid)
            else:
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
                fill = self._stamp_fill(oid, symbol, filled, detail=detail)
                if fill:
                    log.info(
                        "Order %s (%s) FILLED (%g/%g) @ %.4f x %g%s.",
                        oid, symbol, filled, qty, fill["price"], fill["qty"],
                        " [ledger stamped]" if fill.get("stamped") else "",
                    )
                else:
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
    def _drain_floor_shadow_jobs(self) -> None:
        """Run-7 4a-16 (fix-pass, review 2 #2): compute + stamp the clamp-
        floor shadow for the watchdog's hard-STOP exits HERE, on the decision
        thread, from the jobs the safety loop queued (it took the ledger-lot
        read locally and skipped the bars fetch — a `_retry_read`-budgeted
        network call under the trade lock). One `daily_close_series` read
        per job; an unreadable series leaves the row None (excluded from the
        paired test, never a false 'survived'). Never raises."""
        drain = getattr(getattr(self, "watchdog", None), "drain_floor_shadow_jobs", None)
        if drain is None:
            return
        for job in drain():
            try:
                survive, worst = floor_survival_at_exit(
                    getattr(self.broker, "daily_close_series", None),
                    job["symbol"], job["entry_ts"], job["basis"],
                    exit_ts=job.get("exit_ts"),
                )
                if survive is None:
                    log.info(
                        "FLOOR6 SHADOW: %s stop — series unreadable; row left "
                        "None (order %s).", job["symbol"], job["order_id"],
                    )
                    continue
                log.info("%s", floor_shadow_line(
                    job["symbol"], "stop", survive, worst, job["basis"],
                    job.get("live_stop"),
                ))
                self.ledger.set_floor_shadow(job["order_id"], survive, worst)
            except Exception as e:  # noqa: BLE001 — a shadow never blocks the cycle
                log.debug("floor shadow drain for %s failed: %s", job.get("symbol"), e)

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
        next cycle.

        Run-7 A6: the row is written WITH fill_price / fill_qty / fill_ts from
        the broker's closed order (filled_avg_price / filled_qty / filled_at).
        Run-6's 7 bracket exits — half the closed sample — had fill_price=null
        because this path priced realized_pl from the fill but never stamped
        it, leaving the contract's ledger-vs-fill reconciliation undefined.
        Stamped regardless of LEDGER_FILL_PRICES: that knob gates the extra
        per-order REST read at reconcile; here the fill is already in hand."""
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
            # Once-per-oid skip memory (per process): orders we refuse to
            # backfill stay in the broker's closed list every cycle — log the
            # ERROR once, not hourly forever (Aug-23 measurement integrity).
            skipped = getattr(self, "_backfill_skipped_oids", None)
            if skipped is None:
                skipped = self._backfill_skipped_oids = set()
            for o in sorted(closed, key=lambda x: x["filled_at"] or ""):
                if not o["order_id"] or o["order_id"] in known:
                    continue
                ts = None
                if o["filled_at"]:
                    try:
                        ts = datetime.fromisoformat(o["filled_at"])
                    except ValueError:
                        pass
                # Aug-23 fix: a multi-leg option (MLEG) parent order carries
                # symbol=None at the broker; str()-ing it once wrote a SELL row
                # with symbol="None", exit_price=-1.19 and no P&L (the Aug-17
                # AMZN unwind). Never write a corrupt row — skip and log loud.
                sym = (o["symbol"] or "").strip()
                if not sym or sym == "None":
                    if o["order_id"] not in skipped:
                        skipped.add(o["order_id"])
                        log.error(
                            "Backfill SKIP (corrupt symbol): order %s has "
                            "unresolvable symbol %r (MLEG parent?) — refusing "
                            "to write a corrupt SELL row.",
                            o["order_id"], o["symbol"],
                        )
                    continue
                occ = parse_occ(sym)
                instrument = "option" if occ else "equity"
                lots = open_lots.get(sym, [])
                basis, covered = fifo_basis(lots, o["qty"])
                # 4a-16: the trip's opening lot, read BEFORE the FIFO consume
                # below pops it — the clamp-floor shadow measures from there.
                trip_entry_ts = lots[0].entry_ts if lots else None
                pl_pct = pl = None
                if instrument == "equity" and basis > 0 and o["price"] > 0:
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
                if pl is None:
                    # No FIFO basis: an option chunk fill whose group close the
                    # watchdog already ledgered under another order id (the
                    # 4-leg MLEG cap splits closes across orders), or an equity
                    # sale of pre-ledger shares. A P&L-less SELL row is exactly
                    # the corruption the ledger now rejects — skip, log once,
                    # but still stamp the exit cooldown (the exit DID happen).
                    if o["order_id"] not in skipped:
                        skipped.add(o["order_id"])
                        log.error(
                            "Backfill SKIP (no ledger basis): %s %s %g @ %.2f "
                            "order %s — not writing a P&L-less SELL row (group "
                            "close already ledgered, or entry predates ledger).",
                            instrument, sym, o["qty"], o["price"], o["order_id"],
                        )
                        # NEVER stamp an option fill's per-share PREMIUM as the
                        # UNDERLYING's exit price: exit_prices["AMZN"]=1.19
                        # would trip the price-aware re-entry guard on every
                        # fresh equity buy for up to 7 days (the watchdog's own
                        # option exits omit price for exactly this reason).
                        self.state.register_exit(
                            occ[0] if occ else sym, when=ts,
                            price=None if occ else (o["price"] or None),
                            pl_pct=None)
                    continue
                # A bracket's stop leg is a STOP order; its take-profit leg is a
                # LIMIT. Anything else filled that we didn't place (market/other)
                # was an outside actor — label it external, don't guess.
                reason = {
                    "stop": "bracket_stop", "stop_limit": "bracket_stop",
                    "trailing_stop": "bracket_stop", "limit": "bracket_take",
                }.get(o["type"], "external")
                # Run-7 4a-16: a bracket STOP fill stamps whether a stop at the
                # 6% clamp floor would have survived the trip on closes. Read
                # only — the exchange already filled the stop. None when the
                # series can't be read (excluded from the paired test).
                floor6 = floor6_worst = None
                if (
                    reason == "bracket_stop" and instrument == "equity"
                    and trip_entry_ts is not None and basis > 0
                ):
                    floor6, floor6_worst = floor_survival_at_exit(
                        getattr(self.broker, "daily_close_series", None),
                        sym, trip_entry_ts, basis, exit_ts=ts,
                    )
                    if floor6 is not None:
                        live_stop = next(
                            (r.stop_loss_pct for r in reversed(records)
                             if r.symbol == sym and r.action == "buy"), None,
                        )
                        log.info("%s", floor_shadow_line(
                            sym, reason, floor6, floor6_worst, basis, live_stop,
                        ))
                # o["price"] is the broker's filled_avg_price and o["qty"] its
                # filled_qty (AlpacaClient.closed_sell_orders): realized_pl
                # above is already computed AT the fill, so exit_price and
                # fill_price are the same number here by construction (A6).
                self.ledger.record(TradeRecord.for_sell(
                    sym,
                    f"exchange-side exit backfill ({o['type'] or 'unknown'} sell)",
                    o["order_id"], qty=o["qty"],
                    realized_pl_pct=pl_pct, realized_pl=pl,
                    exit_reason=reason, ts=ts, exit_price=o["price"] or None,
                    instrument=instrument,
                    fill_price=o["price"] or None, fill_qty=o["qty"] or None,
                    fill_ts=ts,
                    floor6_would_survive=floor6,
                    floor6_worst_close_pct=floor6_worst,
                ))
                log.info(
                    "Backfilled exchange exit: %s %g sh @ %.2f (%s -> %s%s) "
                    "[fill stamped%s].",
                    sym, o["qty"], o["price"], o["type"] or "?", reason,
                    f", {pl_pct:+.1f}%" if pl_pct is not None else "",
                    f" {ts.isoformat()}" if ts is not None else ", no filled_at",
                )
                # An exchange-side exit also starts the re-entry cooldown —
                # stamped at the FILL time and price when known (price feeds the
                # price-aware re-entry guard; realized % feeds the loss streak).
                self.state.register_exit(
                    occ[0] if occ else sym, when=ts, price=o["price"] or None,
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
        prior_verdicts: dict[str, tuple[str, float, str, str, str]] = {}
        try:
            for rec in self.journal.today():
                # Only records that carry an actual MODEL verdict — synthetic
                # rows (slate exclusions, backstop drops, fallback declines)
                # would otherwise masquerade as the model's own reasoning.
                if (
                    rec.symbol
                    and rec.action in ("hold", "sell", "buy")
                    and rec.verdict not in (
                        "slate_excluded", "dropped_buy",
                        # Aug 1 verdict-reconciliation rows: bookkeeping about
                        # the put path, not the model's read on a held equity.
                        "put_declined", "put_ignored",
                    )
                    and not rec.reason.startswith("option fallback")
                ):
                    prior_verdicts[rec.symbol] = (
                        rec.action, rec.conviction, rec.rationale_head,
                        str(rec.verdict or ""), str(rec.reason or ""),
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
            # S-6 (run-7): the top-up bar the risk gate will hold an ADD to.
            # Run-6 lost 37 of 77 risk-judged BUYs at that gate — the prompt
            # showed the anchor ("entry conviction 0.66") but never the rule
            # (+0.05), so the model re-proposed the entry number (12x) or a
            # hair over it (20x) every cycle. Read from the STATE clock only,
            # never the ledger fallback above: risk.py compares against the
            # same stamp and fails open once the 7-day clock prunes it, so
            # printed == enforced and nothing prints when nothing is enforced.
            # Best-effort like the rest of this method: a partially built
            # orchestrator (no cfg) simply prints no bar.
            risk_cfg = getattr(getattr(self, "cfg", None), "risk", None)
            delta = float(
                getattr(risk_cfg, "topup_min_conviction_delta", 0.0) or 0.0
            )
            prev = self.state.last_buy_conviction(p.symbol)
            if delta > 0 and prev is not None:
                bits.append(
                    f"top-up needs conviction >= "
                    f"{format_topup_bar(topup_bar(prev, delta))} "
                    f"(+{delta:g} over the last buy) — else HOLD"
                )
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
            # Per-name falling read (Jul 30 review): the model must see that
            # THIS name's own defense trigger fired even when the index reads
            # calm — and that a loss-cut it asks for will not be guard-vetoed.
            falling = self._falling_names_today().get(p.symbol, "")
            if falling:
                bits.append(
                    f"NAME FALLING {falling} — name-level defense read is "
                    "armed; a loss-cut SELL passes the rotation guard this "
                    "cycle"
                )
            note = ", ".join(bits)
            why = latest_rationale.get(p.symbol, "")
            if why:
                note = (note + "; thesis: " if note else "thesis: ") + why[:90]
            pv = prior_verdicts.get(p.symbol)
            if pv:
                action, pconv, phead, pverdict, preason = pv
                # S-6: a BUY that died at the top-up gate is shown AS rejected
                # — echoing "BUY conv 0.66" alone anchored the model to
                # re-assert the number the gate had just refused.
                topup_rejected = (
                    action == "buy"
                    and pverdict == "rejected"
                    and preason.startswith("Top-up conviction")
                )
                note += (
                    f"; your last verdict today: {action.upper()} "
                    f"conv {pconv:.2f}"
                    + (" — rejected at the top-up bar" if topup_rejected else "")
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

    # -- LLM sell authority: deterministic event tags (run-6 item 2) -------- #
    def _sell_authority(self) -> str:
        return (
            getattr(self.cfg.risk, "llm_sell_authority", "full") or "full"
        ).strip().lower()

    def _sell_event_tags(self, symbol: str, account) -> tuple[str, ...]:
        """Event tags that license a model SELL on a LOSING equity position
        under llm_sell_authority='events_only'. Every tag comes from CODE —
        the model cannot assert one: this cycle's NAME FALLING read for the
        symbol, earnings inside the blackout window, an account-wide halt,
        or the regime flipping into risk-off this cycle. Empty tuple = no
        event; each read fails closed (no tag) on error."""
        tags: list[str] = []
        why = self._falling_names_today().get(symbol, "")
        if why:
            tags.append(f"name_falling:{why}")
        try:
            blackout = int(getattr(self.cfg.risk, "earnings_blackout_days", 0) or 0)
            d = self.earnings.days_until_earnings(symbol) if blackout > 0 else None
            if d is not None and 0 <= d <= blackout:
                tags.append(f"earnings:{d}d")
        except Exception:  # noqa: BLE001
            pass
        try:
            halted, halt_why = self.risk.trading_halted(account)
            if halted:
                tags.append(f"halt:{halt_why[:40]}")
        except Exception:  # noqa: BLE001
            pass
        if getattr(self, "_regime_flipped_off", False):
            tags.append("regime_flip:risk-off")
        return tuple(tags)

    def _reached_planned_stop(self, symbol: str, pos) -> bool:
        """True when `pos` sits at/below the planned stop width recorded at
        entry (state.get_stop_width) — the same read _evaluate_sell uses to
        approve a loser under events-only authority. Unknown width = False."""
        try:
            w = float(self.state.get_stop_width(symbol) or 0.0)
        except Exception:  # noqa: BLE001
            return False
        return w > 0 and float(getattr(pos, "unrealized_pl_pct", 0.0) or 0.0) <= -w

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
            # Run-6 item 2: under events-only sell authority a loss-locking
            # rotation sell with NO deterministic event never executes — the
            # release ladder below (red day, depth, persistence, conviction
            # edge) is moot for it. Pass it straight to the risk layer, which
            # rejects it with the one countable 'SELL AUTHORITY' line instead
            # of a rotation veto that would hide the counterfactual.
            # Review fix (Aug 26): a loser that has REACHED its planned stop
            # would be approved downstream as "stop reached", so it must
            # still earn the release ladder here — only the never-executes
            # case (no event AND stop not reached) skips it.
            if (
                self._sell_authority() == "events_only"
                and not self._sell_event_tags(p.symbol, account)
                and not self._reached_planned_stop(p.symbol, pos)
            ):
                kept.append(p)
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
            # Name-level falling release (Jul 30 review, Phase-1 gap): the
            # red-day release needs the BOOK to be losing, but the July
            # pattern is a single name breaking inside a calm or even green
            # tape. When this cycle's per-name falling read fired for the
            # symbol, the requested loss-cut is defense against ITS OWN
            # break — pass it, whatever the book's day P/L reads.
            fall_why = self._falling_names_today().get(p.symbol, "")
            if fall_why:
                log.info(
                    "Rotation guard: PASS %s at %+.1f%% — name-level falling "
                    "read (%s): cutting a breaking name is defense, not "
                    "churn.", p.symbol, pos.unrealized_pl_pct, fall_why,
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
        self._proxy_put_state = ""
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

    def _reconcile_bear_verdicts(self, proposals) -> None:
        """Close the loop the output schema now forces (Aug 1): for every
        put-ELIGIBLE bearish name this cycle the model owes an explicit
        bearish_verdicts entry — a put proposal, or a decline naming the
        missing evidence. Jul 31, the day the precheck/verdict prompt fix
        shipped, three cycles ran with ELIGIBLE names (GLUE, RBLX, VCYT) and
        the decision journal recorded ZERO mention of any of them: the model
        neither proposed nor declined, it silently skipped. Prose asked twice;
        the required schema field is the escalation. Every eligible name now
        lands in the journal as put_proposed / put_declined(reason) /
        put_ignored, so dormancy is a queryable record instead of an absence
        — and the decline REASONS become the nightly postmortem's raw
        material for the next fix."""
        self._bear_verdict_stage = {}
        eligible = [
            s for s, (ok, _w) in getattr(self, "_bear_eligibility", {}).items()
            if ok
        ]
        if not eligible:
            return
        verdicts = getattr(self.engine, "last_bear_verdicts", {}) or {}
        put_syms = {
            p.symbol for p in proposals
            if p.action is Action.BUY
            and p.instrument is Instrument.OPTION
            and p.option_legs
            and all(l.right.lower().startswith("p") for l in p.option_legs)
        }
        for s in eligible:
            if s in put_syms:
                self._bear_verdict_stage[s] = "put proposed"
                continue
            v = verdicts.get(s)
            if v and v[0] == "declined":
                reason = (v[1] or "").strip() or "no reason given"
                self._bear_verdict_stage[s] = f"declined: {reason[:60]}"
                self._journal_decision(
                    s, "hold", "option", 0.0, 0.0, "put_declined", 0.0,
                    reason[:200], "",
                )
                log.info("Bearish verdict: %s DECLINED — %s", s, reason[:160])
            else:
                # No verdict at all, or a claimed put_proposed with no actual
                # put in the response — either way the eligible name went
                # unaddressed.
                why = (
                    "model claimed put_proposed but returned no put proposal"
                    if v and v[0] == "put_proposed" else
                    "model returned no bearish_verdicts entry for an "
                    "ELIGIBLE name (the schema requires one per listed name)"
                )
                self._bear_verdict_stage[s] = "IGNORED"
                self._journal_decision(
                    s, "hold", "option", 0.0, 0.0, "put_ignored", 0.0, why, "",
                )
                log.warning("Bearish verdict: %s ELIGIBLE but IGNORED — %s.",
                            s, why)

    # BEARISH FUNNEL line bounds: gate-blocked / off-slate names are capped at
    # the top N by score; ELIGIBLE names are NEVER capped (see _log_bear_funnel).
    # Per-name reason text (eligibility carve-out / decline reason) is clipped
    # so one verbose model reply cannot balloon the line.
    FUNNEL_BLOCKED_MAX = 6
    FUNNEL_REASON_CHARS = 60

    def _log_bear_funnel(self, bear_map: dict, account) -> int:
        """Emit the per-cycle BEARISH FUNNEL line and return the IGNORED count.

        One line per cycle: how many slate names carried a real bearish read
        (composite <= -bearish_reserve_bar), where each one ended — off-slate
        / gate-blocked / `ELIGIBLE (why) -> put proposed | declined: … |
        IGNORED` — how many puts the model proposed, how many the gates
        passed, and whether the deterministic hedge sleeve is on.

        History: Jul 30 the bare count shipped (the put path had been dormant
        for 388 straight trades). Jul 31 decomposed it per name — the count
        hid WHERE the 4->0 drop-off happened. Aug 1 appended the model's
        reconciled verdict to each ELIGIBLE name. Sep 12 (run-7 change-set,
        item A5): the line rendered only the 6 most-bearish names, so on
        Sep 10 2026 all three "ELIGIBLE but IGNORED" verdicts (ABT ranked 7th
        of 7 at 08:34; KORU/STE 7th/8th of 8 at 09:26) fell off the line and
        never appeared as `-> IGNORED` — the literal handle the away-mode
        runbook, Todo-4 and the Aug-1 escalation trigger grep for — so the
        window was scored "0 IGNORED" on a truncated line. Rules now:

          * every ELIGIBLE name renders its terminal stage, no cap; an
            eligible name with no reconciled stage renders `-> IGNORED`
            (the schema owes a verdict per eligible name, so "no stage" IS
            ignored) — the handle always exists when the miss happens;
          * gate-blocked / off-slate names keep the top-FUNNEL_BLOCKED_MAX
            cap by score, with a `…+N more` tail so the cap is visible;
          * reason text is clipped to FUNNEL_REASON_CHARS;
          * `ignored=N` sits in the summary tail beside put_proposals /
            put_approved so the count is a field, not a grep.
        """
        eligibility = getattr(self, "_bear_eligibility", {}) or {}
        stages = getattr(self, "_bear_verdict_stage", {}) or {}
        parts: list[str] = []
        ignored = 0
        blocked_shown = 0
        blocked_hidden = 0
        for s, v in sorted(bear_map.items(), key=lambda kv: kv[1]):
            verdict = eligibility.get(s)
            if verdict is not None and verdict[0]:
                stage = (stages.get(s) or "").strip() or "IGNORED"
                why = (verdict[1] or "")[:self.FUNNEL_REASON_CHARS]
                if stage == "IGNORED":
                    ignored += 1
                    # Spelled out as ONE literal (not `-> {stage}`) so the
                    # contract handle `-> IGNORED` greps in the source, not
                    # only in the rendered line — run-7 item 4a-20
                    # (tests/test_contract_handles.py); output is unchanged.
                    parts.append(f"{s} {v:+.2f} ELIGIBLE ({why}) -> IGNORED")
                else:
                    parts.append(f"{s} {v:+.2f} ELIGIBLE ({why}) -> {stage}")
                continue
            if blocked_shown >= self.FUNNEL_BLOCKED_MAX:
                blocked_hidden += 1
                continue
            blocked_shown += 1
            parts.append(
                f"{s} {v:+.2f} "
                + ("off-slate" if verdict is None else "gate-blocked")
            )
        if blocked_hidden:
            parts.append(f"…+{blocked_hidden} more gate-blocked/off-slate")
        bear_detail = (" [" + "; ".join(parts) + "]") if parts else ""
        h_etf = getattr(self.cfg, "hedge_etf", "")
        hedge_pos = account.position_for(h_etf) if h_etf else None
        # Aug 22: the armed/holding state names its trigger source —
        # (index) vs (breadth:N-names) vs (book:-X.X%) — so the daily log
        # shows WHICH read armed the hedge sleeve, not just that one did.
        _trig = getattr(self, "_falling_trigger", "") or getattr(
            self, "_hedge_reason", ""
        )
        log.info(
            "BEARISH FUNNEL: slate_bearish=%d%s put_proposals=%d "
            "put_approved=%d ignored=%d auto_hedge=%s proxy_put=%s",
            len(bear_map),
            bear_detail,
            getattr(self, "_bear_puts_proposed", 0),
            getattr(self, "_bear_puts_approved", 0),
            ignored,
            (
                f"${max(0.0, hedge_pos.market_value):,.0f} {h_etf}"
                + (f"({_trig})" if _trig else "")
                if hedge_pos is not None else
                (f"armed({_trig or 'clearing'})"
                 if getattr(self, "_falling_cycles", 0) > 0 and h_etf
                 else ("off" if not h_etf else "flat"))
            ),
            getattr(self, "_proxy_put_state", "") or "none",
        )
        return ignored

    def _bear_precheck_names(self, bundles, composites: dict) -> list[str]:
        """ON-SLATE names that get a put-gate precheck: composite <= -bar, OR
        (review fix, Aug 26) a bundle with NO composite whose bearish lean
        lives in the DISCOVERY score — the run-6 composite excludes discovery,
        so the insider-sell discovery path (PR #41) would otherwise never
        reach put_precheck / put_eligibility and the 0/83 sleeve would shrink
        structurally. `_bearish_lean` still reads discovery."""
        bar = self.cfg.screener.bearish_reserve_bar or 0.4
        out: list[str] = []
        for b in bundles:
            comp = composites.get(b.symbol)
            if comp is None:
                comp = getattr(b, "composite_score", None)
            if comp is not None:
                eligible = comp <= -bar
            else:
                try:
                    eligible = self._bearish_lean(b)
                except Exception:  # noqa: BLE001 — a precheck, never a blocker
                    eligible = False
            if eligible:
                out.append(b.symbol)
        return out

    def _bearish_lean(self, bundle) -> bool:
        """True when the bundle's evidence leans bearish enough to justify
        keeping an equity-blocked, not-held name on the slate as a PUT
        candidate. The discovery signal (the screener's smart-money lean that
        surfaced the name) is the primary read — mirror the aggregator's
        bearish intake bar; without one, fall back to the mean of the scored
        signals. Jul 31: also honor the COMPOSITE — a bullishly-DISCOVERED
        name (Robinhood mover, discovery +0.60) whose full bundle nets
        composite <= -bar is exactly what the funnel counts as bearish, yet
        was dropped here on its positive discovery score alone, so the prompt
        listed put candidates whose data had been partitioned away."""
        comp = getattr(bundle, "composite_score", None)
        if comp is not None and comp <= -(
            self.cfg.screener.bearish_reserve_bar or 0.4
        ):
            return True
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
    @staticmethod
    def _et_now() -> datetime:
        """Wall clock in exchange time — the ET session is the unit the
        falling-names map is stamped and gated by (run-7 S-7). A static
        method so tests can pin the date without touching `datetime`."""
        return datetime.now(ZoneInfo("America/New_York"))

    def _market_falling(self, account=None) -> tuple[bool, str]:
        """Deterministic "the market is falling" read for the core defense,
        the auto-hedge and the index-put sanction. True when the INDEX is
        falling (regime risk-off, SPY under its 200dma, or TODAY'S benchmark
        move breaching the intraday trigger) OR — Aug-22 breadth trigger
        (Aug 18: -$26,844 at 3.9x SPY down-capture with ELEVEN per-name
        falling reads while SPY never breached -1.1% intraday, so every
        defense slept) — when the BOOK itself is falling: at least
        breadth_falling_names_min held names carry a NAME FALLING read, or
        the intraday book P/L (equity vs last_equity — the daily-loss
        halt's own numbers) breaches breadth_book_drawdown_pct. The index
        legs still fail closed when the regime filter is off or degraded;
        the breadth legs need no regime feed. `account` defaults to the
        snapshot the defense callers stash each cycle (kept optional so
        zero-arg callers and test stubs still work). The winning trigger
        source ("index" / "breadth:N-names" / "book:-X.X%") lands in
        self._falling_trigger for the BEARISH FUNNEL line."""
        acct = (
            account if account is not None
            else getattr(self, "_defense_account", None)
        )
        falling, why, source = False, "", ""
        if self.cfg.risk.regime_filter_enabled:
            reg = self.regime.assess()
            if reg.label == "risk-off":
                falling, why, source = True, "regime is risk-off", "index"
            elif reg.trend == "down":
                falling, why, source = (
                    True, "SPY below its 200dma (long-run downtrend)", "index"
                )
            else:
                drop = getattr(self.cfg, "market_drop_defense_pct", 0.0)
                if (
                    drop > 0
                    and reg.day_change_pct is not None
                    and reg.day_change_pct <= -drop
                ):
                    falling, why, source = True, (
                        f"SPY {reg.day_change_pct:+.1f}% today "
                        f"(<= -{drop:g}% intraday defense trigger)"
                    ), "index"
        if not falling:
            names = getattr(self, "_falling_names", {}) or {}
            names_min = int(
                getattr(self.cfg, "breadth_falling_names_min", 0) or 0
            )
            # Double-count guard: a map from a PREVIOUS cycle that the breadth
            # re-arm already counted toward auto_hedge_min_cycles is one
            # observation, not two — the top-of-cycle defense pass must wait
            # for this cycle's fresh map instead of re-counting the stale one
            # (otherwise the 2-cycle persistence bar is satisfied by a single
            # one-hour blip and the hedge whipsaws).
            map_cycle = getattr(self, "_breadth_map_cycle", -1)
            stale_counted = (
                map_cycle == getattr(self, "_breadth_counted_cycle", -2)
                and map_cycle < getattr(self, "_cycle_seq", 0)
            )
            # Cross-day guard (run-7 S-7, Sep 11 2026): a map computed in a
            # PREVIOUS ET session is yesterday's tape, not a breadth read of
            # today's — it neither trims the core nor tightens the hedge
            # target. Unlike the within-day carry (which the double-count
            # guard above merely stops from counting twice), a cross-day map
            # is ignored outright until this cycle's fresh map lands. An
            # unstamped map (tests, first cycle) is treated as fresh.
            map_date = getattr(self, "_breadth_map_date", "") or ""
            cross_day = bool(map_date) and map_date != self._et_now().date().isoformat()
            self._breadth_stale_map = False
            if names_min > 0 and len(names) >= names_min:
                if cross_day:
                    self._breadth_stale_map = True
                    if getattr(self, "_breadth_guard_logged", -1) != map_cycle:
                        self._breadth_guard_logged = map_cycle
                        log.info(
                            "BREADTH STALE MAP: %d-name falling map from %s "
                            "predates today's ET session — ignored (no core "
                            "trim, no hedge-target tighten) until this "
                            "cycle's fresh breadth read lands.",
                            len(names), getattr(self, "_breadth_map_stamp", map_date),
                        )
                elif stale_counted:
                    if getattr(self, "_breadth_guard_logged", -1) != map_cycle:
                        self._breadth_guard_logged = map_cycle
                        log.info(
                            "BREADTH COUNT GUARD: %d-name falling map from a "
                            "prior cycle already counted toward the hedge "
                            "persistence bar — awaiting this cycle's fresh "
                            "breadth read.", len(names),
                        )
                else:
                    falling = True
                    source = f"breadth:{len(names)}-names"
                    why = (
                        f"{len(names)} held names falling at once (>= "
                        f"{names_min}-name breadth bar: "
                        + ", ".join(sorted(names)[:5]) + ")"
                    )
        if not falling:
            raw = getattr(self.cfg, "breadth_book_drawdown_pct", 0.0) or 0.0
            bar = -abs(float(raw))  # sign-agnostic: -1.25 == 1.25 (a loss)
            if bar < 0 and acct is not None and getattr(acct, "last_equity", 0.0) > 0:
                day = acct.day_pl_pct
                if day <= bar:
                    falling = True
                    source = f"book:{day:+.1f}%"
                    why = (
                        f"book P/L {day:+.2f}% intraday "
                        f"(<= {bar:g}% breadth drawdown trigger)"
                    )
        self._falling_read_last = falling
        self._falling_trigger = source
        # One distinctive line per trigger-source TRANSITION (this read runs
        # several times per cycle — dedupe keeps the daily log greppable
        # without a 3x echo); the per-cycle state lives in BEARISH FUNNEL.
        if falling and source != getattr(self, "_falling_trigger_logged", ""):
            log.info(
                "FALLING-TAPE TRIGGER (%s): %s — core defense / auto-hedge / "
                "index-put sanction keying on this read.", source, why,
            )
        self._falling_trigger_logged = source
        return falling, why

    def _name_falling_reads(self, account, tech_ctx) -> dict[str, str]:
        """Per-NAME falling read (Jul 30 review, Phase-1 gap): every falling-
        market defense keys on an INDEX-level trigger, and July's losses did
        not happen on index-level days — Jul 29 bottomed at -1.2% vs the
        -1.5% intraday trigger while NU/NOK broke -5%+ alone in a calm tape.
        A held name down NAME_DROP_DEFENSE_PCT%+ on the day is ITSELF
        falling, whatever the index reads. Day change is the live position
        price against the technical feed's prior daily close; a name without
        tech data this cycle fails open (no read, no release — the bracket
        stop still bounds it). System-managed sleeves are skipped: the
        orchestrator already manages their exits deterministically. Returns
        symbol -> human-readable read ("-5.2% today vs SPY +0.1%"); the SPY
        tail contextualizes name-vs-tape without gating the read on it (a
        name in freefall deserves defense on a red index day too)."""
        thresh = getattr(self.cfg, "name_drop_defense_pct", 0.0) or 0.0
        if thresh <= 0:
            return {}
        spy = None
        try:
            if self.cfg.risk.regime_filter_enabled:
                spy = self.regime.assess().day_change_pct
        except Exception:
            spy = None
        out: dict[str, str] = {}
        managed = self._system_managed_symbols()
        for p in account.positions:
            if p.is_option or p.qty <= 0 or p.symbol in managed:
                continue
            prev = (tech_ctx.get(p.symbol) or {}).get("prev_close")
            px = p.current_price or 0.0
            if not prev or prev <= 0 or px <= 0:
                continue
            day = (px / prev - 1.0) * 100.0
            if day <= -thresh:
                out[p.symbol] = (
                    f"{day:+.1f}% today"
                    + (f" vs SPY {spy:+.1f}%" if spy is not None else "")
                )
        return out

    def _falling_names_today(self) -> dict[str, str]:
        """The NAME FALLING map for TODAY's ET session, or {} while the live
        map still belongs to a previous session (run-7 S-7 fix-pass, review
        1 #2). _market_falling's breadth leg already ignores a cross-day map;
        the prompt's HELD-line note, the sell-authority event tag, the
        rotation guard's loss-cut release and the 4a-15 buy-row tape read
        the raw map and so, at the first cycle of a +0.9% open, told the
        model three names were 'falling today', released the loss-cut bar
        on them and stamped `falling_names=[DRAM, INTC, SEI]` on the very
        shadow rows 4a-15 exists for. Same-day maps (the deliberate within-
        day carry) and unstamped maps (tests, first cycle) pass through."""
        names = getattr(self, "_falling_names", {}) or {}
        if not names:
            return {}
        map_date = getattr(self, "_breadth_map_date", "") or ""
        if map_date and map_date != self._et_now().date().isoformat():
            return {}
        return names

    def _breadth_rearm(self, account) -> None:
        """Aug-22 breadth re-arm: the defense pass at the top of the cycle
        runs BEFORE this cycle's falling-names map exists (the map needs the
        signal bundles' tech context), so a breadth-armed read would
        otherwise act a full cycle late. When the FRESH map alone crosses
        the breadth bar and that earlier pass read clear, re-run the
        defenses now — Aug 18 fired ELEVEN name-falling reads in one cycle
        with zero defense trades. Whipsaw bounds hold: the earlier clear
        pass only advanced _clear_cycles, and this re-run counts at most ONE
        falling cycle, so the auto_hedge_min_cycles persistence and the
        auto_hedge_max_pct ceiling apply unchanged."""
        names_min = int(getattr(self.cfg, "breadth_falling_names_min", 0) or 0)
        if names_min <= 0:
            return
        names = getattr(self, "_falling_names", {}) or {}
        if len(names) < names_min:
            return
        if getattr(self, "_falling_read_last", False):
            return  # the top-of-cycle pass already counted a falling read
        log.info(
            "BREADTH RE-ARM: %d name-falling reads >= %d-name bar — "
            "re-running core defense + auto-hedge on this cycle's breadth.",
            len(names), names_min,
        )
        self._apply_core_defense(account)
        self._apply_auto_hedge(account)

    def _apply_core_defense(self, account) -> None:
        """When the market itself is falling, stop averaging INTO it and take
        risk OFF the core: pause the core-ETF fill for the cycle (the flag the
        fill checks) and sell CORE_DEFENSE_TRIM_PCT of the core position, at
        most once per trading day. This is the deterministic answer to "QQQ
        always drags the book down when it falls" — the core's other guards
        (GTC stop 15% under basis, equity floor, daily-loss flatten) only act
        at catastrophe distance. A defensive trim is not a thesis exit: no
        re-entry cooldown / loss-streak stamp, and the fill resumes (DCA back
        in) as soon as the falling read clears.

        Trim mechanics (run-7 S-7, Sep 11 2026 08:30 ET incident): the
        resting GTC core stop reserves every whole share, so the trim used to
        cancel it and sell in the same instant — the cancel was still
        settling (pending_cancel), the sell got 40310000 "available 0.4975 /
        held_for_orders 163", and _ensure_core_stop then read the
        pending_cancel stop back as "already right" (core stopless 4m53s;
        trim silently dropped). Now the stop is REPLACED qty-down by exactly
        the trim (atomic at the venue: the remainder never rides unprotected
        and nothing has to settle before the sell), then the trim sells; if
        the venue refuses the replace, fall back to cancel -> poll until the
        cancel has settled (<= 5 s) -> sell. On any failure the retry flag
        stays armed for the 30 s watchdog and NOTHING re-places a stop inline
        while a cancel may still be settling. The sub-share residual (the
        part no GTC stop can cover) rides along with the trim so the integer
        stop covers 100% of what is left."""
        # Stash the snapshot for _market_falling's book-P/L breadth leg (the
        # read itself stays zero-arg for its other callers).
        self._defense_account = account
        etf = self.cfg.core_etf
        self._core_defense_active = False
        if not etf or not getattr(self.cfg, "core_defense_enabled", False):
            return
        falling, why = self._market_falling()
        if not falling:
            self._log_stale_map_counterfactual(account, etf)
            return
        self._core_defense_active = True
        pos = account.position_for(etf)
        if pos is None or pos.qty <= 0:
            return
        if self.state.core_defense_fired_today():
            return  # already trimmed today; the paused fill carries the defense
        sell_qty, whole = self._core_trim_qty(pos)
        if whole <= 0:
            return
        oid, detail = self._core_trim_sell(etf, pos, sell_qty, whole)
        # Whatever happened to the stop (replaced smaller, canceled, or left
        # alone on a refused replace), the core must NOT ride the rest of
        # this (minutes-long) cycle unverified: arm the 30s watchdog retry
        # unconditionally.
        self._core_stop_gap = True
        if not oid:
            log.warning(
                "CORE DEFENSE: trim of %g %s NOT submitted (%s) — GTC stop "
                "re-placement left to the ~30s watchdog retry (never inline "
                "while a cancel may be settling); trim retried next cycle "
                "while the falling read holds.", sell_qty, etf, detail,
            )
            return
        frac = self._core_trim_frac()
        self.state.mark_core_defense()
        log.warning(
            "CORE DEFENSE: %s — sold %g of %g %s sh (%.0f%% trim%s); core fill "
            "paused while the falling read holds.",
            why, sell_qty, pos.qty, etf, frac * 100.0,
            (f" + {sell_qty - whole:g} sub-share residual" if sell_qty > whole else ""),
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
        # Verify / re-place the GTC stop for the remainder NOW, sized from the
        # reduced snapshot qty. Replace path: the stop already reads exactly
        # the remainder, so this clears the retry flag without touching it.
        # Cancel-fallback path: the poll confirmed the cancel settled, so a
        # fresh stop can rest at once. If the working trim sell wash-blocks
        # it, the armed watchdog retry heals within ~30s.
        self._ensure_core_stop(account)

    def _core_trim_frac(self) -> float:
        return max(
            0.0, min(100.0, getattr(self.cfg, "core_defense_trim_pct", 0.0))
        ) / 100.0

    def _core_trim_qty(self, pos) -> tuple[float, float]:
        """(sell_qty, whole) for a defense trim of `pos`: `whole` is the
        whole-share part (CORE_DEFENSE_TRIM_PCT of the position, floored —
        the shares freed from the GTC stop), and sell_qty adds the sub-share
        residual (run-7 S-7 rider): the core accumulates through notional
        buys, so it carries a fraction (163.4975 sh on Sep 11) that no GTC
        stop can cover and that only the 30s watchdog guards. Folding it into
        the trim leaves an integer position, so the stop covers 100%. A
        position whose whole-share trim is 0 is left alone entirely (the
        residual is not worth a lone fractional sell)."""
        frac = self._core_trim_frac()
        whole = float(int(pos.qty * frac)) if frac > 0 else 0.0
        if whole <= 0:
            return 0.0, 0.0
        residual = round(pos.qty - int(pos.qty), 6)
        return round(whole + residual, 6), whole

    #: Cancel-fallback poll: 10 x 0.5 s = the 5 s the spec allows a
    #: venue-side cancel to settle before the trim is given up for this cycle.
    _CORE_TRIM_CANCEL_POLLS = 10
    _CORE_TRIM_CANCEL_POLL_S = 0.5

    def _core_trim_sell(
        self, etf: str, pos, sell_qty: float, whole: float,
    ) -> tuple[str | None, str]:
        """Free `whole` shares from the resting GTC core stop and market-sell
        `sell_qty`. Returns (sell order id or None, detail for the log line).

        Preferred: ReplaceOrderRequest(qty = stop_qty - whole) on the resting
        stop — atomic at the venue, frees exactly the trim, the remainder
        stays protected (operator decision 4; "replace on live legs, never
        cancel-then-resell"). Fallback when the venue refuses the replace (or
        the stop would shrink below 1 share): cancel it and poll
        open_stop_sells(resting_only=False) every 0.5 s for up to 5 s until
        the cancel has settled (a pending_cancel order still reserves the
        shares — the Sep 11 race), then sell. No stop resting: sell directly.
        Never re-places a stop here: a failed trim leaves that to the
        watchdog, with the broker's available qty in the WARNING."""
        # Fix-pass (review 2 #5): the pre-S-7 trim blanket-canceled every
        # open order for the ETF first, so a still-working core-fill BUY
        # (queued / partially filled notional order from the prior cycle)
        # never wash-blocked the SELL. The stop-only path would submit the
        # sell into that wash-trade reject and, on the replace path, leave
        # the stop shrunk. Defer the whole trim instead: the core fill is
        # already paused while the falling read holds, a DAY market buy
        # resolves in seconds, and the read persisting retries next cycle.
        # Fails open to 0 (a broker blip must not veto a defense trim).
        try:
            buy_open = float(self.broker.open_buy_notional(etf) or 0.0)
        except Exception:  # noqa: BLE001
            buy_open = 0.0
        if buy_open > 0:
            return None, (
                f"working {etf} BUY (${buy_open:,.0f}) would wash-block the "
                "trim sell — stop untouched, trim deferred to next cycle"
            )
        stops = self.broker.open_stop_sells(etf)
        with self._trade_lock:
            if not stops:
                oid = self.broker.reduce_position(etf, sell_qty)
                return oid, (
                    "no resting stop; " + self._core_available_note(etf)
                    if not oid else "no resting stop"
                )
            stop = max(stops, key=lambda o: o.get("qty", 0.0))
            stop_qty = float(stop.get("qty", 0.0) or 0.0)
            new_qty = stop_qty - whole
            replace = getattr(self.broker, "replace_order_qty", None)
            if replace is not None and new_qty >= 1.0:
                new_id = replace(stop["id"], new_qty)
                if new_id:
                    log.warning(
                        "CORE DEFENSE: stop %s replaced %g -> %g sh, trimming "
                        "%g (new stop %s).", stop["id"], stop_qty, new_qty,
                        whole, new_id,
                    )
                    # Fix-pass (review 2 #1): the replace is asynchronous at
                    # the venue; while it settles the ORIGINAL qty may still
                    # be reserved and a same-instant sell gets the Sep 11
                    # 40310000 — with the stop now SMALLER than the position
                    # (40 sh only watchdog-guarded until the retry re-rests
                    # it, and the trim re-racing the replace every cycle).
                    # Poll the broker's fresh available qty (<= 5 s) before
                    # selling; if the sell still fails, put the stop back to
                    # its full size at once so a failed trim never leaves the
                    # core under-stopped.
                    self._core_trim_wait_free(etf, sell_qty)
                    oid = self.broker.reduce_position(etf, sell_qty)
                    if oid:
                        return oid, f"stop {new_id} resting for {new_qty:g} sh"
                    restored = replace(new_id, stop_qty)
                    log.warning(
                        "CORE DEFENSE: trim sell of %g %s refused after the "
                        "replace — stop %s %s %g -> %g sh (%s).",
                        sell_qty, etf, new_id,
                        "restored" if restored else "NOT restored (replace refused)",
                        new_qty, stop_qty, self._core_available_note(etf),
                    )
                    return None, (
                        f"stop {restored or new_id} resting for "
                        f"{stop_qty if restored else new_qty:g} sh; "
                        + self._core_available_note(etf)
                    )
                log.warning(
                    "CORE DEFENSE: replace of stop %s (%g -> %g sh) refused — "
                    "falling back to cancel -> poll -> sell.",
                    stop["id"], stop_qty, new_qty,
                )
            # Fallback: cancel, then WAIT for the venue to settle it. The
            # shares stay reserved while the order is pending_cancel, so a
            # same-instant sell only gets 40310000 (Sep 11 2026 08:30:18).
            for o in stops:
                self.broker.cancel_order(o["id"])
            settled = False
            for _ in range(self._CORE_TRIM_CANCEL_POLLS):
                pending = self.broker.open_stop_sells(etf, resting_only=False)
                if not pending:
                    settled = True
                    break
                time.sleep(self._CORE_TRIM_CANCEL_POLL_S)
            if not settled:
                return None, (
                    f"stop {stop['id']} cancel still settling after "
                    f"{self._CORE_TRIM_CANCEL_POLLS * self._CORE_TRIM_CANCEL_POLL_S:g}s; "
                    + self._core_available_note(etf)
                )
            oid = self.broker.reduce_position(etf, sell_qty)
            return oid, (
                f"stop {stop['id']} canceled (settled); "
                + self._core_available_note(etf)
                if not oid else f"stop {stop['id']} canceled (settled)"
            )

    def _core_trim_wait_free(self, etf: str, need: float) -> bool:
        """Poll the broker's FRESH qty_available for `etf` every 0.5 s (up to
        the same 5 s budget as the cancel fallback) until at least `need`
        shares are free of working orders — the moment the venue has
        settled the qty-down replace. True when free (or unreadable: the
        sell attempt itself is then the arbiter); False when the budget ran
        out (the caller still tries the sell, so a slow position read never
        skips a defense trim by itself)."""
        for i in range(self._CORE_TRIM_CANCEL_POLLS):
            try:
                fresh = self.broker.open_position(etf)
            except Exception:  # noqa: BLE001 — a poll detail, never a blocker
                fresh = None
            avail = getattr(fresh, "qty_available", None) if fresh is not None else None
            if avail is None or float(avail) + 1e-6 >= need:
                return True
            if i == 0:
                log.info(
                    "CORE DEFENSE: waiting for the replace to free %g %s sh "
                    "(broker available=%g) — polling up to %gs.", need, etf,
                    float(avail),
                    self._CORE_TRIM_CANCEL_POLLS * self._CORE_TRIM_CANCEL_POLL_S,
                )
            time.sleep(self._CORE_TRIM_CANCEL_POLL_S)
        return False

    def _core_available_note(self, etf: str) -> str:
        """'broker available=X sh' from a fresh position read — the number
        the Sep 11 40310000 reject carried (0.4975 of 163.4975), so the
        WARNING says how many shares the venue thinks are free. Best-effort."""
        try:
            fresh = self.broker.open_position(etf)
        except Exception:  # noqa: BLE001 — a log detail, never a blocker
            fresh = None
        if fresh is None:
            return "broker available=n/a"
        return f"broker available={fresh.qty_available:g} of {fresh.qty:g} sh"

    def _log_stale_map_counterfactual(self, account, etf: str) -> None:
        """Once per cross-day map: what the breadth leg WOULD have done had
        yesterday's falling-names map still counted — the Sep 11 2026 08:30
        wrong-day trim (40 QQQ, $29k, on a +0.9% risk-on open), now a log
        line instead of an order."""
        if not getattr(self, "_breadth_stale_map", False):
            return
        map_cycle = getattr(self, "_breadth_map_cycle", -1)
        if getattr(self, "_stale_map_logged", -1) == map_cycle:
            return
        self._stale_map_logged = map_cycle
        names = getattr(self, "_falling_names", {}) or {}
        pos = account.position_for(etf) if account is not None else None
        would = ""
        if pos is not None and pos.qty > 0 and not self.state.core_defense_fired_today():
            sell_qty, whole = self._core_trim_qty(pos)
            if whole > 0:
                usd = sell_qty * max(0.0, pos.current_price or 0.0)
                usd_s = f"${usd / 1000.0:.0f}k" if usd >= 1000 else f"${usd:.0f}"
                would = f"; would have trimmed {sell_qty:g} {etf} ({usd_s})"
        log.warning(
            "CORE DEFENSE: stale falling map (%s, %d names) ignored at "
            "new-day open%s.",
            getattr(self, "_breadth_map_stamp", "") or getattr(self, "_breadth_map_date", ""),
            len(names), would,
        )

    def _system_managed_symbols(self) -> set[str]:
        """Symbols the ORCHESTRATOR owns end-to-end (core ETF, auto-hedge
        inverse ETF, defensive T-bill core). They never enter the model's
        slate and model proposals against them are ignored — Claude proposes
        theses; these are allocations.

        S-1 (run-7): RiskLimits.slot_exempt_symbols (the MAX_OPEN_POSITIONS
        exemption) is derived in load_config from the SAME three env keys —
        add a fourth source here and there together, or the risk gate counts
        a row the orchestrator opens outside it (the Sep 3-4 2026 16/15
        book). tests/test_risk.py::test_slot_exempt_matches_system_managed_symbols
        pins the equality."""
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
        # Stash the snapshot for _market_falling's book-P/L breadth leg.
        self._defense_account = account
        etf = getattr(self.cfg, "hedge_etf", "")
        if not etf:
            return
        # 4a-17: price the last unwound lot for five sessions (once per
        # cycle; the breadth re-arm's second call is a no-op here).
        self._log_hedge_counterfactual(account, etf)
        if str(getattr(self.cfg, "auto_hedge_mode", "falling")).lower() == "beta":
            self._apply_beta_hedge(account, etf)
            return
        falling, why = self._market_falling()
        if falling:
            self._falling_cycles += 1
            self._clear_cycles = 0
            # When BREADTH won this read, mark its falling-names map as
            # counted: next cycle's top-of-cycle pass re-reads the same
            # (by-then stale) map, and one observation must not satisfy the
            # persistence bar twice (_market_falling skips a stale counted
            # map on the breadth-names leg).
            if str(getattr(self, "_falling_trigger", "")).startswith("breadth:"):
                self._breadth_counted_cycle = getattr(
                    self, "_breadth_map_cycle", -1
                )
        else:
            self._clear_cycles += 1
        need = max(1, getattr(self.cfg, "auto_hedge_min_cycles", 2))
        pos = account.position_for(etf)
        held_val = max(0.0, pos.market_value) if pos is not None else 0.0
        if not falling:
            if self._clear_cycles >= need:
                self._falling_cycles = 0
                if pos is not None and pos.qty > 0:
                    self._hedge_close(
                        account, etf, pos, held_val,
                        "auto-hedge unwind: falling read cleared",
                        "AUTO-HEDGE UNWIND: falling read clear %d cycles — "
                        "closing %g %s (%+.1f%%).",
                        self._clear_cycles, pos.qty, etf,
                        pos.unrealized_pl_pct,
                    )
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
        if not self._hedge_submit(
            account, etf, notional, f"auto-hedge: {why}",
            f"deterministic inverse-ETF hedge, {ratio:.0%} of net-long, "
            f"ceiling {getattr(self.cfg, 'auto_hedge_max_pct', 0.0):.0f}% equity",
        ):
            return
        log.warning(
            "AUTO-HEDGE: %s (cycle %d) — bought $%.0f of %s "
            "(hedge $%.0f/$%.0f target = %.0f%% of $%.0f net-long).",
            why, self._falling_cycles, notional, etf, held_val + notional,
            target, ratio * 100.0, net_long,
        )

    # -- run-6 item 7: book-beta reading + beta-sized hedge ---------------- #
    def _read_book_beta(self, account) -> None:
        """ONE 'BOOK BETA:' line per decision cycle (run-6 item 7a); the
        reading is kept for the buy-path cap and the beta hedge and
        persisted to risk_state.json. Never blocks trading."""
        self._book_beta_reading = None
        if not getattr(self.cfg, "book_beta_enabled", False):
            return
        reader = getattr(self, "book_beta", None)
        if reader is None:
            log.info("BOOK BETA: unavailable (no reader)")
            return
        try:
            # Per-symbol liveness stamps: the read is a serial ~20-fetch
            # fan-out (see portfolio/beta.py) that must not read as a hang.
            reading = reader.read(account, on_progress=self._stamp_liveness)
        except Exception as e:  # noqa: BLE001 — a measurement, never a blocker
            log.info("BOOK BETA: unavailable (%s: %s)", type(e).__name__, e)
            return
        self._stamp_hedge_etf(reading)     # 4a-17: 'hedge=PSQ w=... unhedged=...'
        log.info("%s", reading.line())
        if not reading.available:
            return
        self._book_beta_reading = reading
        try:
            self.state.set_book_beta(reading.to_dict())
        except Exception as e:  # noqa: BLE001
            log.warning("BOOK BETA: state persist failed: %s", e)

    def _beta_context(self, symbol: str, account) -> tuple[float | None, float | None]:
        """(book SPY-beta right now, candidate's shrunk SPY-beta) for the
        buy-path beta cap; (None, None) = reader off / blind -> gate skipped.
        The book is re-read against the (mutated) cycle snapshot so a buy
        submitted earlier this cycle already counts; the per-symbol series
        are cycle-cached so this costs at most one fetch (the candidate)."""
        if getattr(self, "_book_beta_reading", None) is None:
            return None, None
        try:
            cur = self.book_beta.read(account)
            book = cur.spy if cur.available else self._book_beta_reading.spy
            return book, self.book_beta.beta_of(symbol, "SPY")
        except Exception as e:  # noqa: BLE001 — fail open
            log.warning("BOOK BETA: context for %s failed: %s", symbol, e)
            return None, None

    def _hedge_beta(self, etf: str) -> tuple[float, str]:
        """(SPY-beta the beta hedge is sized against, 'measured' | 'assumed')
        — run-7 S-2. Prefers the hedge ETF's OWN shrunk, cycle-cached beta
        from the book-beta reader (BookBeta.beta_of — the very number the
        reading prices a held hedge at, so a fresh arm lands ON target);
        falls back to Config.hedge_beta_assumed when the reader is absent
        or has no beta_of, the series is too short (None), or the value is
        not a sane inverse-ETF beta — outside [-3.0, -0.5]: a positive read
        would size a 'hedge' that ADDS exposure and a spurious -0.3 would
        double the order, so neither is ever divided by. Never raises (the
        hedge runs inside the decision cycle); one INFO line per fallback,
        called only when arming so an idle hedge costs no extra fetch."""
        assumed = -1.0
        try:
            assumed = float(getattr(self.cfg, "hedge_beta_assumed", -1.0))
        except (TypeError, ValueError):
            assumed = -1.0
        if not (-3.0 <= assumed <= -0.5):        # config validates too; belt and braces
            assumed = -1.0
        hb: float | None = None
        reader = getattr(self, "book_beta", None)
        if reader is not None:
            try:
                raw = reader.beta_of(etf, "SPY")
                hb = None if raw is None else float(raw)
            except Exception as e:  # noqa: BLE001 — a measurement, never a blocker
                log.info(
                    "Auto-hedge: %s SPY-beta read failed (%s: %s) — using "
                    "assumed %.2f.", etf, type(e).__name__, e, assumed,
                )
                return assumed, "assumed"
        if hb is not None and hb == hb and -3.0 <= hb <= -0.5:
            return hb, "measured"
        if hb is None:
            log.info(
                "Auto-hedge: %s SPY-beta unmeasured (no reader/short history) "
                "— using assumed %.2f.", etf, assumed,
            )
        else:
            log.info(
                "Auto-hedge: %s measured SPY-beta %.2f outside [-3.0, -0.5] — "
                "using assumed %.2f.", etf, hb, assumed,
            )
        return assumed, "assumed"

    def _apply_beta_hedge(self, account, etf: str) -> None:
        """AUTO_HEDGE_MODE=beta (run-6 item 7c): size the inverse ETF to
        max(0, beta_book_spy - target) x equity. Arms when the book's SPY-
        beta exceeds hedge_beta_target by more than hedge_beta_band for ONE
        cycle; unwinds when it falls below target - band (hysteresis);
        holds in between. The falling-tape read is kept as a 'tighten the
        target to hedge_beta_falling_target' condition (its own persistence
        rules no longer gate the hedge). The measured beta INCLUDES a held
        hedge, so the gap is the ADDITIONAL notional; auto_hedge_max_pct
        caps the total. An unavailable reading holds whatever is on.

        Sizing (run-7 S-2, Sep 12 2026): the gap is divided by the hedge
        ETF's OWN measured SPY-beta (_hedge_beta), not by an assumed -1.
        PSQ is -1.0 x QQQ and QQQ's SPY-beta read 1.51 (shrunk, 60 d) in
        run-6, so the undivided gap over-hedged every arm by ~50%: Sep 3
        10:14 and Sep 10 11:06 a 1.20 read bought $203,605 / $204,391 of
        PSQ and the next reading landed at 0.89 / 0.90 — 0.05 above the
        0.85 unwind line instead of ~1.00 — ~$68k of idle hedge per arm
        and extra room under the buy-path beta cap (the Sep 3 12:51 re-arm
        then hit the cash lock). The AUTO-HEDGE line prints the divisor,
        its source and the notional the undivided formula would have
        bought, and risk_state carries hedge_beta / hedge_beta_source."""
        falling, why = self._market_falling()
        target = float(getattr(self.cfg, "hedge_beta_target", 1.0))
        band = max(0.0, float(getattr(self.cfg, "hedge_beta_band", 0.15)))
        if falling:
            target = min(
                target, float(getattr(self.cfg, "hedge_beta_falling_target", target)),
            )
        beta = None
        if getattr(self.cfg, "book_beta_enabled", False):
            try:
                cur = self.book_beta.read(account)
                beta = cur.spy if cur.available else None
            except Exception as e:  # noqa: BLE001
                log.warning("Auto-hedge: beta re-read failed: %s", e)
        if beta is None:
            base = getattr(self, "_book_beta_reading", None)
            beta = base.spy if base is not None else None
        pos = account.position_for(etf)
        held_val = max(0.0, pos.market_value) if pos is not None else 0.0
        held = pos is not None and pos.qty > 0
        if beta is None:
            # S-8: a gap in the reading must not carry a stale streak.
            self._unwind_reads = (-1, 0)
            log.info(
                "AUTO-HEDGE: beta: book beta unavailable this cycle — "
                "holding %s ($%.0f).", etf, held_val,
            )
            return
        sig = hedge_signal(beta, target, band, held)
        tag = f"beta:{beta:.2f}"
        if sig == "unwind":
            # Run-7 S-7 fix-pass (review 1 #1): at the FIRST pass of a new ET
            # day the cross-day guard in _market_falling ignores yesterday's
            # >= N-name map, so this pass reads 'not falling' and evaluates
            # at target 1.00 instead of the 0.80 the stale map used to pin.
            # A held hedge whose overnight-drift reading sits in [0.80, 0.85)
            # (Sep 10 14:38 read 0.84; Sep 11 08:30 read 0.85) would be SOLD
            # here and, minutes later when today's fresh map lands and the
            # breadth re-arm re-runs this method at 0.80, re-BOUGHT in the
            # same cycle (~$230k PSQ round-trip). Until today's map has
            # landed, a below-band read is a HOLD for this pass: the re-arm
            # pass (or the next cycle's top-of-cycle pass, once the map is
            # today's) decides. The S-8 streak is left untouched.
            if (
                getattr(self, "_breadth_stale_map", False)
                and getattr(self, "_breadth_map_cycle", -1)
                != getattr(self, "_cycle_seq", 0)
            ):
                self._hedge_reason = tag
                self._falling_cycles = 1
                log.info(
                    "Auto-hedge: beta: unwind deferred — %d-name map predates "
                    "today; deciding on this cycle's fresh breadth read "
                    "(book spy-beta %.2f < target %.2f - %.2f band; holding "
                    "$%.0f %s this pass).",
                    len(getattr(self, "_falling_names", {}) or {}), beta,
                    target, band, held_val, etf,
                )
                return
            # Run-7 S-8: close only after hedge_unwind_min_cycles CONSECUTIVE
            # below-band readings, counted at most ONCE per decision cycle
            # (the breadth re-arm re-runs this method inside one cycle —
            # Sep 10 14:35:37 and 14:38:52 — and must not count twice).
            # Default 1 = today's one-read unwind, unchanged. The streak is
            # NOT reset here: a declined close (no order id) keeps it, so
            # the next cycle retries at once instead of re-counting.
            try:
                need = max(1, int(getattr(self.cfg, "hedge_unwind_min_cycles", 1) or 1))
            except (TypeError, ValueError):
                need = 1
            cyc = getattr(self, "_cycle_seq", 0)
            last_cyc, n = getattr(self, "_unwind_reads", (-1, 0))
            if cyc != last_cyc:
                n += 1
            self._unwind_reads = (cyc, n)
            if n < need:
                self._hedge_reason = tag
                self._falling_cycles = 1
                log.info(
                    "Auto-hedge: beta: unwind read %d/%d — holding $%.0f %s "
                    "(book spy-beta %.2f < target %.2f - %.2f band; would have "
                    "closed %g %s at HEDGE_UNWIND_MIN_CYCLES=1).",
                    n, need, held_val, etf, beta, target, band, pos.qty, etf,
                )
                return
            self._falling_cycles = 0
            self._hedge_reason = ""
            self._hedge_close(
                account, etf, pos, held_val,
                f"auto-hedge unwind: beta {beta:.2f} < target {target:.2f} - {band:.2f}",
                "AUTO-HEDGE UNWIND: beta: book spy-beta %.2f < target %.2f - "
                "%.2f band — closing %g %s (%+.1f%%).",
                beta, target, band, pos.qty, etf, pos.unrealized_pl_pct,
            )
            return
        if sig == "hold":
            self._unwind_reads = (-1, 0)          # S-8: streak broken
            self._hedge_reason = tag if held else ""
            self._falling_cycles = 1 if held else 0
            if held:
                log.info(
                    "Auto-hedge: beta: book spy-beta %.2f within %.2f +/- %.2f "
                    "— holding $%.0f %s.", beta, target, band, held_val, etf,
                )
            return
        # arm
        self._unwind_reads = (-1, 0)              # S-8: streak broken
        self._falling_cycles = 1
        self._hedge_reason = tag
        halted, halt_why = self.risk.trading_halted(account)
        if halted:
            log.info("Auto-hedge skipped: %s", halt_why)
            return
        max_pct = max(0.0, float(getattr(self.cfg, "auto_hedge_max_pct", 0.0)))
        # S-2: divide the gap by the hedge ETF's own SPY-beta (measured when
        # sane, else the assumed knob) and keep the undivided figure as the
        # counterfactual the log line prints. Stamped into risk_state HERE,
        # at resolution, so a cash-clamped or declined arm still records
        # the divisor the sizer used this cycle.
        hedge_beta, hb_source = self._hedge_beta(etf)
        try:
            self.state.set_hedge_beta(hedge_beta, hb_source)
        except Exception as e:  # noqa: BLE001 — bookkeeping, never a blocker
            log.warning("Auto-hedge: hedge_beta state stamp failed: %s", e)
        gap = hedge_target_notional(
            beta, target, account.equity, max_pct, hedge_beta=hedge_beta,
        )
        gap_at_unit = hedge_target_notional(beta, target, account.equity, max_pct)
        ceiling = account.equity * max_pct / 100.0
        gap = min(gap, max(0.0, ceiling - held_val))
        gap_at_unit = min(gap_at_unit, max(0.0, ceiling - held_val))
        r = self.cfg.risk
        min_fill = max(
            r.min_order_usd, account.equity * (r.min_order_pct / 100.0), 1.0
        )
        if gap < min_fill:
            if ceiling - held_val < min_fill:
                log.info(
                    "Auto-hedge: beta: book spy-beta %.2f > target %.2f but %s "
                    "already at the %.0f%% ceiling ($%.0f).",
                    beta, target, etf, max_pct, held_val,
                )
            return
        min_cash = account.equity * (r.min_cash_buffer_pct / 100.0)
        spendable = max(0.0, min(account.cash - min_cash, account.buying_power))
        notional = round(min(gap, spendable), 2)
        # What the pre-S-2 formula (hedge beta -1.0) would have sent, under
        # the same ceiling and cash clamps — the line's counterfactual.
        notional_at_unit = round(min(gap_at_unit, spendable), 2)
        if notional < min_fill:
            log.info(
                "Auto-hedge: want $%.0f more %s but only $%.0f spendable "
                "after the cash buffer.", gap, etf, spendable,
            )
            return
        reason = (
            f"beta: book spy-beta {beta:.2f} > target {target:.2f} + {band:.2f} band"
            + (f" (tape falling: {why})" if falling else "")
        )
        if not self._hedge_submit(
            account, etf, notional, f"auto-hedge: {reason}",
            f"beta-sized inverse-ETF hedge to target {target:.2f} at hedge "
            f"beta {hedge_beta:.2f} ({hb_source}), ceiling {max_pct:.0f}% equity",
        ):
            return
        log.warning(
            "AUTO-HEDGE: %s — bought $%.0f of %s (hedge $%.0f/$%.0f, "
            "ceiling %.0f%% of equity; hedge beta %.2f %s; at -1.0 would be "
            "$%.0f).",
            reason, notional, etf, held_val + notional,
            min(held_val + gap, ceiling), max_pct, hedge_beta, hb_source,
            notional_at_unit,
        )

    # -- run-7 S-8 / 4a-17: hedge observability (log lines only) ----------- #
    def _stamp_hedge_etf(self, reading) -> None:
        """Name the hedge ETF on a reading that lacks it (a reader built
        without the kwarg) so line() / hedge_view() can render the hedge
        segment. Never raises."""
        etf = str(getattr(self.cfg, "hedge_etf", "") or "").upper()
        if etf and hasattr(reading, "hedge_etf") and not getattr(reading, "hedge_etf", ""):
            try:
                reading.hedge_etf = etf
            except Exception:  # noqa: BLE001
                pass

    def _log_hedge_counterfactual(self, account, etf: str) -> None:
        """'HEDGE COUNTERFACTUAL: last unwind lot 10283 sh @25.84 would be
        +$X today' — ONE line per decision cycle for the five ET sessions
        after a hedge unwind (4a-17). Run-6's Sep 9 exit / Sep 10 re-arm
        whipsaw was priced by hand at $1,917 .. $2,754 depending on which
        prices, lot and endpoints each analyst chose; this line pins ONE
        definition: X = qty x (price now - the unwind's DECISION quote),
        i.e. the P&L the unwound lot would carry were it still held, priced
        at the snapshot price when the ETF is held again else one
        latest_price fetch. Session 0 = the unwind day's remaining cycles;
        sessions 1-5 = the next five ET dates the bot traded, counted in
        risk_state so a restart cannot re-count them. Never raises; never
        trades; no line when no hedge has been unwound."""
        cyc = getattr(self, "_cycle_seq", 0)
        if getattr(self, "_cf_logged_cycle", -1) == cyc:
            return
        self._cf_logged_cycle = cyc
        getter = getattr(self.state, "get_last_unwind", None)
        if getter is None:
            return
        try:
            lu = getter() or {}
        except Exception:  # noqa: BLE001
            return
        if not lu or str(lu.get("symbol", "")).upper() != str(etf).upper():
            return
        qty = float(lu.get("qty") or 0.0)
        px = float(lu.get("price") or 0.0)
        if qty <= 0 or px <= 0:
            return
        try:
            today = self._et_now().date().isoformat()
            session = int(self.state.mark_unwind_session(today))
        except Exception as e:  # noqa: BLE001
            log.warning("HEDGE COUNTERFACTUAL: session stamp failed: %s", e)
            return
        if session > 5:
            return
        pos = account.position_for(etf)
        now_px = 0.0
        if pos is not None and pos.qty > 0:
            now_px = float(getattr(pos, "current_price", 0.0) or 0.0)
        if now_px <= 0:
            try:
                now_px = float(self.broker.latest_price(etf) or 0.0)
            except Exception as e:  # noqa: BLE001
                log.info(
                    "HEDGE COUNTERFACTUAL: %s price unavailable (%s: %s) — "
                    "lot %g sh @%.2f not priced this cycle.",
                    etf, type(e).__name__, e, qty, px,
                )
                return
        if now_px <= 0:
            return
        pnl = qty * (now_px - px)
        amt = f"{'+' if pnl >= 0 else '-'}${abs(pnl):,.0f}"
        log.info(
            "HEDGE COUNTERFACTUAL: last unwind lot %g sh @%.2f would be %s "
            "today (%s %.2f now; unwound %s ET; session %d/5).",
            qty, px, amt, etf, now_px, lu.get("at") or lu.get("date"), session,
        )

    def _log_post_exec_beta(self, account) -> None:
        """'BOOK BETA (post-exec): spy=... unhedged=...' — the book's beta
        AFTER this cycle's proposals executed, re-read against the mutated
        snapshot (per-symbol series are cycle-cached, so at most the new
        names fetch). The pre-exec line is what the hedge sized against;
        run-6 could not say whether a same-cycle buy or sell pushed the
        book across an arm/unwind line the hedge never saw (LF-6: the
        Sep 9 unwind read 0.44 pre-exec and ~0.58 with the buys in — the
        same side of the 0.85 line; refuted as a cause, n=0 crossings).
        This line makes that gap COUNTABLE: 'CROSSING pre=hold post=arm'
        is appended whenever the hysteresis signal differs between the
        two reads at the cycle's own target. A second hedge pass after
        execution ships only after >= 5 in-window crossings. Logs nothing
        when the reader is off/blind this cycle; never raises; never
        trades."""
        base = getattr(self, "_book_beta_reading", None)
        if base is None:
            return
        try:
            # Fix-pass (review 2 #3): symbols new to the mutated snapshot
            # (option underlyings folded into the book, names the corr guard
            # skipped) fetch bars here — up to a _retry_read budget each.
            # Stamp liveness per symbol exactly as the pre-exec read does, or
            # two slow names on a degraded feed cross the 150 s heartbeat
            # gate and page the deadman (three false-fires on record).
            cur = self.book_beta.read(account, on_progress=self._stamp_liveness)
        except Exception as e:  # noqa: BLE001 — a measurement, never a blocker
            log.info("BOOK BETA (post-exec): unavailable (%s: %s)", type(e).__name__, e)
            return
        if not getattr(cur, "available", False):
            log.info(
                "BOOK BETA (post-exec): unavailable (%s)",
                getattr(cur, "reason", "") or "no SPY beta",
            )
            return
        self._stamp_hedge_etf(cur)

        def _f(v):
            return "n/a" if v is None else f"{v:.2f}"

        s = (
            f"BOOK BETA (post-exec): spy={_f(cur.spy)} "
            f"qqq={_f(getattr(cur, 'qqq', None))} "
            f"iwm={_f(getattr(cur, 'iwm', None))} "
            f"invested={float(getattr(cur, 'invested_pct', 0.0) or 0.0):.1f}%"
        )
        etf = str(getattr(self.cfg, "hedge_etf", "") or "").upper()
        hv = cur.hedge_view() if hasattr(cur, "hedge_view") else None
        if hv is not None:
            w, b, unh = hv
            if w:
                s += f" hedge={etf} w={w:.3f} beta={_f(b)} unhedged={unh:.2f}"
            else:
                s += f" hedge={etf} w=0 unhedged={unh:.2f}"
        else:
            s += f" unhedged={_f(cur.spy)}"     # no hedge named: the book IS unhedged
        pre = getattr(base, "spy", None)
        if pre is not None:
            s += f" (pre-exec spy={pre:.2f} delta={cur.spy - pre:+.2f}"
            if etf and str(getattr(self.cfg, "auto_hedge_mode", "")).lower() == "beta":
                target = float(getattr(self.cfg, "hedge_beta_target", 1.0))
                band = max(0.0, float(getattr(self.cfg, "hedge_beta_band", 0.15)))
                if getattr(self, "_falling_read_last", False):
                    target = min(target, float(
                        getattr(self.cfg, "hedge_beta_falling_target", target)))
                pos = account.position_for(etf)
                held = pos is not None and pos.qty > 0
                pre_sig = hedge_signal(pre, target, band, held)
                post_sig = hedge_signal(cur.spy, target, band, held)
                if pre_sig != post_sig:
                    s += f"; CROSSING pre={pre_sig} post={post_sig}"
            s += ")"
        log.info("%s", s)

    def _hedge_close(
        self, account, etf: str, pos, held_val: float, rationale: str,
        msg: str, *args,
    ) -> None:
        """Close the hedge ETF, ledger the unwind, keep the snapshot honest."""
        with self._trade_lock:
            self.broker.cancel_open_orders_for(etf)
            oid = self.broker.close_position(etf)
        if not oid:
            return
        log.warning(msg, *args)
        self.ledger.record(TradeRecord.for_sell(
            etf, rationale, oid, qty=pos.qty,
            realized_pl_pct=pos.unrealized_pl_pct,
            realized_pl=pos.unrealized_pl,
            exit_reason="hedge_unwind",
            exit_price=pos.current_price or None,
        ))
        self._pending_oids.append((oid, etf))
        self.state.add_pending_order(oid, etf)
        # 4a-17: remember the lot so the next five sessions can print what
        # holding it would have been worth (HEDGE COUNTERFACTUAL line).
        try:
            now_et = self._et_now()
            self.state.set_last_unwind(
                etf, pos.qty, float(pos.current_price or 0.0),
                now_et.date().isoformat(), now_et.strftime("%Y-%m-%d %H:%M"),
            )
        except Exception as e:  # noqa: BLE001 — bookkeeping, never a blocker
            log.warning("Hedge unwind: last_unwind state stamp failed: %s", e)
        # Keep the cycle's snapshot honest: hedge is cash now.
        account.cash += held_val
        account.buying_power += held_val
        pos.qty = 0.0
        pos.market_value = 0.0

    def _hedge_submit(
        self, account, etf: str, notional: float, rationale: str, risk_note: str,
    ) -> bool:
        """Submit a notional hedge buy + ledger/state bookkeeping. False when
        the broker declined (caller logs nothing; next cycle retries)."""
        price = self.broker.latest_price(etf)
        with self._trade_lock:
            oid = self.broker.submit_notional_buy(etf, notional)
        if not oid:
            log.warning("Auto-hedge buy of %s failed to submit — next cycle.", etf)
            return False
        self.ledger.record(TradeRecord(
            symbol=etf, action="buy", instrument="equity",
            qty=round(notional / price, 6) if price and price > 0 else 0.0,
            entry_price=price or 0.0, cost_usd=round(notional, 2),
            rationale=rationale,
            entry_signals=["auto_hedge"], verdict="approved",
            risk_note=risk_note,
            order_id=oid,
        ))
        self._pending_oids.append((oid, etf))
        self.state.add_pending_order(oid, etf)
        self.state.register_entry(etf)
        self._apply_pending_buy(
            account, etf, notional, price,
            notional / price if price and price > 0 else 0.0,
        )
        return True

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
                # Run-7 S-7 fix-pass (reviews 1 #7 / 2 #4): a replace is
                # asynchronous at Alpaca — for a tick the OLD id can be listed
                # as `pending_replace` (its qty is the pre-replace size)
                # before the NEW id appears. That order is neither absent nor
                # stale: canceling it and submitting a fresh stop only draws
                # a reserved-qty reject (the replaced stop + the working trim
                # sell already hold every share). Leave it, keep the retry
                # armed; the ~30 s watchdog pass sees the settled state.
                if str(o.get("status", "")).lower() == "pending_replace":
                    if getattr(self, "_core_stop_pending_logged", "") != o["id"]:
                        self._core_stop_pending_logged = o["id"]
                        log.info(
                            "Core stop: %g-sh stop %s is pending_replace — left "
                            "alone this pass (replace settling at the venue); "
                            "watchdog retry stays armed.", o["qty"], o["id"],
                        )
                    self._core_stop_gap = True
                    return
                if (
                    abs(o["qty"] - desired_qty) < 1.0
                    and abs(o["stop_price"] - desired_stop) / desired_stop < 0.005
                ):
                    self._core_stop_gap = False
                    return  # resting stop is already right — leave it alone
            for o in existing:
                # Run-7 S-7: a stop SMALLER than the position whose missing
                # shares are reserved by ANOTHER working order is the trim
                # in flight (replace 163 -> 123, sell 40.4975 queued — e.g.
                # a pre-market DAY order waiting for the bell). Canceling a
                # good stop to chase shares the venue cannot give us only
                # rejects the re-submit (40310000) and leaves the core
                # stopless until the sell fills. Leave it; the next pass
                # after the sell resolves sizes it right.
                short = desired_qty - o["qty"]
                avail = getattr(pos, "qty_available", None)
                if (
                    short >= 1.0
                    and abs(o["stop_price"] - desired_stop) / desired_stop < 0.005
                    and avail is not None and avail < short
                ):
                    if getattr(self, "_core_stop_short_logged", "") != o["id"]:
                        self._core_stop_short_logged = o["id"]
                        log.info(
                            "Core stop: resting %g-sh stop %s covers %g of %g "
                            "%s but only %g sh are free (a sell is working) "
                            "— left alone rather than canceled into a "
                            "reserved-qty reject; watchdog retry stays armed "
                            "until the sell resolves.", o["qty"], o["id"],
                            o["qty"], desired_qty, etf, avail,
                        )
                    # Fix-pass (review 1 #4): the `avail` above may be the
                    # CYCLE-START snapshot's (minutes stale — the exact
                    # field alpaca_client.open_position warns about). If the
                    # DAY sell was rejected/expired since, those shares are
                    # free and under-stopped; clearing the flag here would
                    # disarm the 30 s watchdog retry until the end-of-cycle
                    # pass. Keep it armed: _retry_core_stop re-reads the
                    # position fresh each tick and grows the stop the moment
                    # the shares are free (or clears the flag once the sell
                    # fills and the stop matches the remainder).
                    self._core_stop_gap = True
                    return
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
        # LLM sell authority (run-6 item 2): deterministic event tags + the
        # planned stop width travel with every SELL so the risk layer can
        # hold a loser to the mechanical stack unless code named an event.
        is_sell = proposal.action.value == "sell"
        sell_events = self._sell_event_tags(proposal.symbol, account) if is_sell else None
        stop_width = self.state.get_stop_width(proposal.symbol) if is_sell else None
        # Book-beta cap context (run-6 item 7b): the book's CURRENT SPY-beta
        # (re-read against the snapshot so earlier buys this cycle count)
        # and the candidate's own beta; both None when the reader is off or
        # blind this cycle (the gate then fails open).
        book_spy, cand_beta = (
            self._beta_context(proposal.symbol, account) if is_buy else (None, None)
        )
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
            sell_events=sell_events, stop_width_pct=stop_width,
            book_beta_spy=book_spy, candidate_beta=cand_beta,
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
            # Same-cycle option fallback (Jul 28): a buy that died at the
            # overextension gate gets a scoped follow-up option decision
            # AFTER the main proposal loop. Run-6 item 3: the earnings
            # blackout now applies to option debits too (evaluate_option
            # rejects them), so "Earnings in" no longer feeds the queue;
            # "Overextended" stays ONLY because the fallback hands the
            # underlying's technicals to _handle_option, where the OPTION
            # CHASE GATE re-reads the same tape (a hot-but-not-extreme name
            # deploys at the haircut; an extreme/gap chase is re-blocked).
            # Calls trade WITH the tape only, so skip in a down-trend market.
            # Review fix (Aug 26): with OPTIONS_SINGLE_NAME_BULLISH=off the
            # fallback is a CALL on a single name that evaluate_option will
            # reject unconditionally — don't spend the LLM call (or a
            # journal row against the per-day attempt cap) unless the knob
            # is on or the underlying is an index the gate exempts.
            if (
                is_buy
                and self.options is not None
                and self._regime_trend != "down"
                and decision.reason.startswith("Overextended")
                and self._option_fallback_allowed(proposal.symbol)
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
                        # S-1 (run-7): say so in the log. Sep 4 2026 the
                        # watchdog's "Partial close MKL ... 9 reserved"
                        # WARNING was followed 6s later by "REJECT buy MU:
                        # At max open positions (15)" and nothing recorded
                        # that MKL's slot HAD been folded — the reject came
                        # from QQQ+PSQ sitting in the count, not from the
                        # fold failing.
                        log.info(
                            "ROTATION: %s slot + $%.0f folded into this "
                            "cycle pending fill",
                            proposal.symbol, max(0.0, held.market_value),
                        )
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
                    # Run-7 4a-15/16: the decision-time tape rides on the row
                    # (SPY intraday, regime label, NAME FALLING names, the
                    # refuted haircut's $ and the 6%-floor stop) — logged as
                    # one ENTRY TAPE line. Read-only annotation of a buy that
                    # is already sized and submitted.
                    tape = self._entry_tape(
                        proposal.symbol, decision, vol, tech, sub.notional,
                    )
                    self.ledger.record(TradeRecord.from_equity(
                        decision, price, sub.order_id,
                        entry_signals=signal_kinds or [],
                        submitted_qty=sub.qty, submitted_cost=sub.notional,
                        composite_score=composite, tape=tape))
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

    # -- run-7 4a-15 / 4a-16: decision-time shadow stamps ------------------ #
    def _entry_tape(
        self, symbol: str, decision, volatility: float | None,
        tech: dict | None, approved_notional: float,
    ) -> EntryTape:
        """The tape at the moment a BUY was approved, from reads this cycle
        already holds — NOTHING is fetched here. Stamped onto the buy row and
        logged as ONE greppable line so two claims the run-7 review refuted /
        deferred can be re-tested ex ante on a clean sample instead of
        re-argued from memory:
          - 4a-15 red-tape haircut: Sep 9 2026 the buys went through under
            'Market regime: ... today -0.3%/-0.4%/-0.6% ... -> risk-on' and
            the rows kept none of it; would_haircut_usd is what the refuted
            0.5x rule would have taken off (re-evaluate at >= 15 down dates).
          - 4a-16 clamp floor: stop_pct_if_floor_6 is the stop a 6% floor
            would have set (same arithmetic as risk._exit_levels), and the
            raw unclamped vol stop rides along so ANY floor can be replayed.
        Measurement only: the decision is sized and submitted before this
        runs. Every read fails open to None — a degraded regime feed or a
        blind vol read leaves the stamp empty, never a guessed 0."""
        tape = EntryTape()
        try:
            reader = getattr(self, "regime", None)
            reg = reader.current() if hasattr(reader, "current") else None
            if reg is not None:
                tape.spy_intraday_ret_at_decision = reg.day_change_pct
                tape.regime_label = reg.label or None
                # The breadth confirm surfaces only in the reason text
                # ("QQQ+IWM below 50dma (narrow breadth)", regime._compute);
                # Regime carries no field for it and this stamp must not
                # widen the dataclass the S-5 persistence work owns.
                tape.breadth_narrow = "narrow breadth" in (reg.reason or "")
            else:
                tape.regime_label = getattr(self, "_regime_label", "") or None
            tape.falling_names = sorted(self._falling_names_today().keys())
            tape.would_haircut_usd = would_haircut_usd(
                float(approved_notional or 0.0),
                tape.spy_intraday_ret_at_decision,
                tape.breadth_narrow, len(tape.falling_names),
            )
            lim = getattr(getattr(self, "risk", None), "limits", None)
            if lim is None:
                lim = self.cfg.risk
            if (
                getattr(lim, "vol_stops_enabled", False)
                and volatility is not None and volatility > 0
            ):
                sigma_d = volatility / _TRADING_DAYS_SQRT * 100.0
                mult = float(getattr(lim, "vol_stop_mult", 2.0))
                cap = float(getattr(lim, "vol_stop_max_pct", 10.0))
                ext = None
                if getattr(lim, "stop_cover_extension", False) and tech:
                    ext = tech.get("ext_pct_sma20")
                tape.vol_stop_raw_pct = round(mult * sigma_d, 4)
                tape.stop_pct_if_floor_6 = round(shadow_stop_pct(
                    sigma_d, mult, SHADOW_STOP_FLOOR_PCT, cap, ext_pct=ext,
                ), 4)
            log.info("%s", tape.log_line(
                symbol, float(getattr(decision, "stop_loss_pct", 0.0) or 0.0),
            ))
        except Exception as e:  # noqa: BLE001 — a stamp must never block a buy
            log.debug("ENTRY TAPE for %s partially unavailable: %s", symbol, e)
        return tape

    # -- options path (defined-risk, gated) -------------------------------- #
    def _snap_put_legs(
        self, proposal: TradeProposal, tech: dict | None,
    ) -> tuple[TradeProposal, str]:
        """Run-7 S-4: re-strike a model-proposed SINGLE-NAME put structure
        onto the nearest OI-qualified strike near the money (see
        OptionsHelper.snap_legs_to_liquid for the rule and the run-6
        evidence). Returns (proposal, note): the proposal with snapped legs
        plus a note naming the model's ORIGINAL legs (stamped into the
        verdict reason -> journal `reason` / ledger `risk_note`), or
        (proposal, "") when nothing moved. EVERY failure path keeps the
        model's legs — the existing OI/spread gate then judges them exactly
        as before (Sep 3/4 2026: `REJECT buy HD: Leg HD261016P00400000 open
        interest 2 < 100` is what an unsnapped deep-ITM strike gets, and
        the proxy-put fallback still fires on that reject). Spot = the
        broker's latest trade, else the technical feed's price (the number
        the prompt's candidate line now renders)."""
        from .models import OptionLeg
        snap = getattr(self.options, "snap_legs_to_liquid", None)
        if not callable(snap):
            return proposal, ""
        sym = proposal.symbol.upper()
        spot, src = 0.0, ""
        broker = getattr(self, "broker", None)
        if broker is not None:
            try:
                spot, src = float(broker.latest_price(sym) or 0.0), "broker"
            except Exception as e:  # noqa: BLE001 — fall through to the technical price
                log.debug("STRIKE SNAP: broker price read for %s failed: %s", sym, e)
                spot = 0.0
        if spot <= 0 and tech:
            try:
                spot, src = float(tech.get("price") or 0.0), "technical"
            except (TypeError, ValueError):
                spot = 0.0
        if spot <= 0:
            log.info("STRIKE SNAP: %s skipped — no spot price; model legs stand", sym)
            return proposal, ""
        limits = getattr(getattr(self, "risk", None), "limits", None)
        cfg = getattr(self, "cfg", None)
        try:
            snapped = snap(
                sym, list(proposal.option_legs), spot,
                min_oi=float(getattr(limits, "min_option_open_interest", 100.0) or 0.0),
                max_spread_pct=float(getattr(limits, "max_option_spread_pct", 10.0) or 0.0),
                max_moneyness_pct=float(
                    getattr(cfg, "option_strike_max_moneyness_pct", 10.0) or 0.0
                ),
                min_dte=getattr(limits, "min_option_dte", None),
                max_dte=getattr(limits, "max_option_dte", None),
                today=datetime.now(ZoneInfo("America/New_York")).date(),
            )
        except Exception as e:  # noqa: BLE001 — the snap must never block a proposal
            log.warning("STRIKE SNAP: %s helper failed (%s) — model legs stand", sym, e)
            return proposal, ""
        if (
            not isinstance(snapped, list) or not snapped
            or not all(isinstance(l, OptionLeg) for l in snapped)
        ):
            return proposal, ""

        def _key(l: OptionLeg):
            return (l.expiry, float(l.strike), l.right.lower()[:1], l.side, int(l.ratio))

        if [_key(l) for l in snapped] == [_key(l) for l in proposal.option_legs]:
            return proposal, ""

        def _txt(ls: list[OptionLeg], show_expiry: bool = True) -> str:
            head = f"{ls[0].expiry} " if show_expiry else ""
            return head + "/".join(f"{l.strike:g}" for l in ls) + ls[0].right.upper()[:1]

        note = (
            f"STRIKE SNAP: model legs {_txt(proposal.option_legs)} -> "
            f"{_txt(snapped, snapped[0].expiry != proposal.option_legs[0].expiry)} "
            f"(spot {spot:.2f} {src})"
        )
        return proposal.model_copy(update={"option_legs": snapped}), note

    def _handle_option(
        self, proposal: TradeProposal, account,
        signal_kinds: list[str] | None = None, tech: dict | None = None,
        proxy_for: str = "",
    ) -> None:
        """`proxy_for` marks the SYSTEM's put-liquidity proxy re-proposal (the
        original bearish name whose own chain failed the liquidity floor):
        it satisfies the direction gate the same way the sanctioned hedge
        does — the bearish read was already model-proposed and precheck-
        eligible on the ORIGINAL name; only the venue changed — and it never
        re-proxies (depth 1 by construction)."""
        if self.options is None:
            log.info("Option proposal for %s ignored: options disabled.", proposal.symbol)
            return
        _cfg = getattr(self, "cfg", None)
        index_symbols = frozenset(
            str(x).upper() for x in (
                getattr(_cfg, "core_etf", ""),
                getattr(_cfg, "hedge_etf", ""),
                getattr(_cfg, "put_proxy_etf", ""),
                getattr(_cfg, "defensive_core_etf", ""),
                getattr(self, "_hedge_symbol", ""),
            ) if x
        )
        # Run-7 S-4: snap single-name put strikes to the nearest OI-qualified
        # strike near the money BEFORE the premium / min-leg / liquidity
        # reads, so sizing, the gate, build_legs and the ledger all see the
        # contract that will actually be bought ("before leg_liquidity
        # alone leaves premium sizing on the old strike"). Proxy and index
        # legs are exempt (the proxy builder is OI-aware itself — S-3 — and
        # index puts are sanctioned as proposed); calls are untouched (no
        # call-side failure in evidence). OPTION_STRIKE_SNAP=off restores
        # run-6 behaviour: legs judged exactly as proposed.
        snap_note = ""
        if (
            not proxy_for
            and bool(getattr(_cfg, "option_strike_snap", True))
            and proposal.option_legs
            and all(leg.right.lower().startswith("p") for leg in proposal.option_legs)
            and proposal.symbol.upper() not in index_symbols
        ):
            proposal, snap_note = self._snap_put_legs(proposal, tech)
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
        # Run-6 item 3: the option path gets the SAME earnings read the
        # equity path gets (fails open on a calendar miss, like equities);
        # the configured core/hedge/proxy ETFs join the broad-index exemption.
        days_to_earnings = None
        if not proxy_for:
            try:
                cal = getattr(self, "earnings", None)
                if cal is not None:
                    days_to_earnings = cal.days_until_earnings(proposal.symbol)
            except Exception as e:  # noqa: BLE001 — fail open, never block on a feed error
                log.warning("Option earnings read for %s failed: %s", proposal.symbol, e)
                days_to_earnings = None
        decision = self.risk.evaluate_option(
            proposal, account, premium, leg_liquidity=liquidity,
            min_leg_premium=min_leg,
            market_trend=self._regime_trend,
            regime_label=self._regime_label,
            regime_multiplier=self._regime_mult,
            name_trend=name_trend,
            sanctioned_hedge=(
                bool(proxy_for)
                or (
                    bool(self._hedge_symbol)
                    and proposal.symbol == self._hedge_symbol
                )
            ),
            # % distance from the 20d SMA (negative = below): the broken-
            # momentum put carve-out — NU/NOK-shaped names break hard while
            # still reading name_trend="up" on their 200dma.
            name_ext_pct=tech.get("ext_pct_sma20") if tech else None,
            # Full technicals dict for the bullish-option anti-chase gate
            # (OPTION CHASE GATE): without it the gate fails open and an
            # overextended equity reject re-expressed as a call debit walks
            # straight past the read it was rejected on (the HL -67.6% chase).
            tech=tech,
            days_to_earnings=days_to_earnings,
            index_symbols=index_symbols,
            proxy_put=bool(proxy_for) and bool(
                getattr(_cfg, "proxy_put_thesis_gate", True)
            ),
        )
        if snap_note:
            # Stamp the model's ORIGINAL legs on the verdict: the journal
            # `reason` and the ledger `risk_note` are both decision.reason,
            # so every row says what was proposed vs what was judged/bought.
            decision = decision.model_copy(
                update={"reason": f"{decision.reason} [{snap_note}]"}
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
        if proxy_for:
            self._proxy_put_state = (
                f"{proposal.symbol} for {proxy_for} -> {decision.verdict.value}"
            )
        if decision.verdict == RiskVerdict.REJECTED:
            # Put-liquidity fallback (Aug 14): a model-proposed put that died
            # ONLY on its own chain's liquidity re-expresses on the liquid
            # proxy ETF. Trigger on the liquidity reasons alone — every other
            # rejection (direction, DTE, premium, slots) is a real veto.
            if (
                is_put_play and not proxy_for
                and ("open interest" in decision.reason
                     or "bid-ask spread" in decision.reason)
            ):
                self._propose_proxy_put(proposal, account, signal_kinds)
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

    def _proxy_thesis_transfers(
        self, etf: str, spot: float, blocked: TradeProposal,
    ) -> tuple[bool, str]:
        """Does a single-name bearish read transfer to an index short on
        `etf`? True on any of: the ETF below its 50-day SMA (daily closes
        from the broker), the breadth trigger armed this cycle
        (_market_falling's winning source was "breadth:N-names"), or >= 2
        bearish slate names (this cycle's put-precheck set) in the blocked
        name's sector. Every read fails CLOSED (no data = no transfer) —
        the consequence is the small size, never a skipped hedge."""
        # 1) proxy ETF under its 50-day SMA
        try:
            series = self.broker.daily_close_series(etf, 60)
            closes = [float(c) for _, c in series if c]
            if len(closes) >= 50 and spot > 0:
                sma50 = sum(closes[-50:]) / 50.0
                if spot < sma50:
                    return True, f"{etf} {spot:.2f} below its 50d SMA {sma50:.2f}"
        except Exception as e:  # noqa: BLE001 — a bar-feed miss is not a thesis
            log.debug("Proxy thesis: 50d SMA read for %s failed: %s", etf, e)
        # 2) breadth trigger armed this cycle
        trigger = str(getattr(self, "_falling_trigger", "") or "")
        if trigger.startswith("breadth"):
            return True, f"breadth trigger armed ({trigger})"
        # 3) >= 2 bearish slate names in the same sector
        try:
            sectors = getattr(self, "sectors", None)
            bearish = set(getattr(self, "_bear_eligibility", {}) or {})
            bearish.add(blocked.symbol.upper())
            sec = sectors.sector_for(blocked.symbol) if sectors else None
            if sec:
                same = sorted(
                    s for s in bearish if sectors.sector_for(s) == sec
                )
                if len(same) >= 2:
                    return True, f"{len(same)} bearish slate names in {sec} ({', '.join(same[:4])})"
        except Exception as e:  # noqa: BLE001
            log.debug("Proxy thesis: sector read failed: %s", e)
        return False, "no index/breadth/sector confirmation"

    def _propose_proxy_put(
        self, blocked: TradeProposal, account,
        signal_kinds: list[str] | None,
    ) -> None:
        """Put-liquidity fallback (Aug 14 window-end ship): re-express a
        liquidity-rejected single-name put as a deterministic near-ATM bear
        put spread on the liquid proxy ETF (PUT_PROXY_ETF). The bearish read
        was model-proposed AND precheck-eligible on the original name — only
        its chain was untradeable — so the system supplies a tradeable venue,
        auto-hedge-style. One attempt per cycle; skipped when the proxy
        already carries an option structure; the re-proposal runs the FULL
        gate stack (DTE, premium caps, slots, the proxy's own liquidity)."""
        etf = getattr(self.cfg, "put_proxy_etf", "") or ""
        if not etf or etf == blocked.symbol.upper():
            return
        if getattr(self, "_proxy_put_state", ""):
            return  # once per cycle
        from .execution.options import parse_occ
        held_unders = {
            occ[0] for p in account.positions if p.is_option
            for occ in [parse_occ(p.symbol)] if occ
        }
        if etf in held_unders:
            self._proxy_put_state = f"{etf} skipped: structure already open"
            return
        try:
            spot = self.broker.latest_price(etf)
        except Exception as e:
            log.warning("Proxy put: price read failed for %s: %s", etf, e)
            return
        lo = int(max(25.0, getattr(self.risk.limits, "min_option_dte", 7.0)))
        hi = int(min(50.0, getattr(self.risk.limits, "max_option_dte", 60.0)))
        # Run-7 S-3: the builder pre-filters by the SAME OI floor the gate
        # enforces (min_option_open_interest) so it stops proposing legs the
        # gate is guaranteed to reject (Sep 1/3 2026: Oct-9 weekly OI 38/47),
        # and ranks the third-Friday monthly first. `today` is the ET date so
        # the DTE window matches the gate's clock, not the host's.
        legs = self.options.build_proxy_put_spread(
            etf, spot, lo, hi,
            min_oi=float(getattr(self.risk.limits, "min_option_open_interest", 100.0) or 0.0),
            prefer_monthly=bool(getattr(self.cfg, "proxy_put_prefer_monthly", True)),
            today=datetime.now(ZoneInfo("America/New_York")).date(),
        )
        if not legs:
            self._proxy_put_state = f"{etf} skipped: no workable chain pair"
            log.info("Proxy put for %s: no workable %s chain pair.", blocked.symbol, etf)
            return
        # Run-6 item 3e: a single-name bearish read only transfers to an
        # INDEX short when something index-wide backs it. Otherwise the
        # proxy is a token-sized hedge, not a thesis.
        max_premium = blocked.max_premium_usd
        thesis_note = ""
        if getattr(self.cfg, "proxy_put_thesis_gate", True):
            transfers, why = self._proxy_thesis_transfers(etf, spot, blocked)
            if transfers:
                thesis_note = f" Transferable thesis: {why}."
            else:
                pct = float(getattr(self.cfg, "proxy_put_untransferred_pct", 0.25) or 0.0)
                small = account.equity * (pct / 100.0)
                max_premium = small if max_premium is None else min(max_premium, small)
                thesis_note = (
                    f" Thesis does not transfer to {etf} ({why}) — sized at "
                    f"{pct:g}% of equity (${small:,.0f})."
                )
            log.info("PROXY PUT THESIS: %s -> %s:%s", blocked.symbol, etf, thesis_note)
        proxy = TradeProposal(
            symbol=etf,
            action=Action.BUY,
            conviction=blocked.conviction,
            target_weight_pct=blocked.target_weight_pct,
            rationale=(
                f"SYSTEM PROXY PUT for {blocked.symbol}: its own chain failed "
                f"the liquidity floor, re-expressing the bearish read on "
                f"liquid {etf}.{thesis_note} Original thesis: "
                f"{blocked.rationale[:150]}"
            ),
            key_signals=blocked.key_signals,
            instrument=Instrument.OPTION,
            option_strategy=OptionStrategy.BEAR_PUT_SPREAD,
            option_legs=legs,
            max_premium_usd=max_premium,
        )
        log.info(
            "PROXY PUT: %s put blocked on liquidity -> proposing %s %s/%s %s.",
            blocked.symbol, etf, legs[0].strike, legs[1].strike, legs[0].expiry,
        )
        self._handle_option(
            proxy, account, signal_kinds, tech=None,
            proxy_for=blocked.symbol,
        )
