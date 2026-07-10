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
import threading
import time
from datetime import datetime

from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import Timeout as RequestsTimeout
from urllib3.exceptions import ProtocolError

from .attribution import render_lessons
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
from .signals import SignalAggregator
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
        self.screeners = ScreenerAggregator(cfg, self.quiver)
        # Per-cycle-cached next-earnings lookup feeding the risk earnings-blackout
        # guard (one lookup per symbol per cycle; advisory, fails open).
        self.earnings = EarningsCalendar()
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
        self.engine = DecisionEngine(cfg)
        # One persisted risk-state instance shared by the risk gate and watchdog
        # so peak equity, the drawdown halt, and the halt latch are consistent.
        self.state = PortfolioState(cfg.state_file)
        self.risk = RiskManager(cfg.risk, kill_switch=cfg.kill_switch, state=self.state)
        self.ledger = TradeLedger()
        # Out-of-band paging for watchdog CRITICALs (failed close = naked position;
        # latched halt). Log-only unless ALERTS_ENABLED + a sink is configured.
        self.alerter = Alerter(cfg.alerts)
        # The watchdog records its own exits (stops/take-profits/flattens) to the
        # ledger so signal attribution sees every close, not just decision sells.
        self.watchdog = Watchdog(
            cfg, self.broker, state=self.state, ledger=self.ledger,
            alerter=self.alerter,
        )
        self.benchmark = BenchmarkTracker(cfg, self.broker, symbol=cfg.benchmark_symbol)
        self.robinhood = RobinhoodReader(cfg)
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
        self._last_decision_at = 0.0
        # Serializes broker order mutations so the watchdog's emergency closes and
        # the decision cycle's order placement can't interleave (e.g. double-close).
        # It guards only the quick submit/close calls — never the slow LLM call —
        # so the safety thread is delayed at most by an order-submission window.
        self._trade_lock = threading.Lock()
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
        # Liveness stamps (goGA GA-2.1/2.2). _last_main_tick gates the heartbeat:
        # the watchdog thread only pings the external dead-man URL while the main
        # loop is ALSO fresh, so either thread dying silences the ping and the
        # external monitor pages. _last_wall_tick detects dark gaps (laptop sleep,
        # clock jumps) that monotonic timers can't see.
        self._last_main_tick = time.monotonic()
        self._last_wall_tick = time.time()
        # In-memory halt latch: backs the kill-switch FILE when the file write
        # itself failed (disk full/read-only). Cleared only by restart — if we
        # couldn't write the ack file, there's nothing a human can delete to ack.
        self._forced_halt = False

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
                    # Liveness stamp for the heartbeat gate only. Dark-gap
                    # detection lives on the WATCHDOG thread (_note_loop_tick):
                    # this loop blocks for minutes inside a decision cycle
                    # (LLM + signal fetches), and busy is not dark — the
                    # watchdog keeps watching positions the whole time.
                    self._last_main_tick = time.monotonic()
                    self._refresh_runtime_controls()
                    if self._decision_due():
                        self.run_decision_cycle()
                        self._refresh_dashboard()
                        self._last_decision_at = time.monotonic()
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

    def _watchdog_loop(self) -> None:
        """Independent safety loop: closing positions is never gated, so this runs
        regardless of the kill switch or what the decision thread is doing."""
        while not self._stop.is_set():
            try:
                self._note_loop_tick()
                with self._trade_lock:
                    self.watchdog.check_once()
                self._maybe_heartbeat()
            except _TRANSIENT_NET as e:
                log.warning(
                    "Watchdog tick skipped on a transient network error (%s); "
                    "retrying next tick.", e.__class__.__name__,
                )
            except Exception:
                log.exception("Watchdog tick failed; continuing.")
            self._stop.wait(self.cfg.monitor_interval_s)

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
            )

    def _maybe_heartbeat(self) -> None:
        """Ping the external dead-man monitor — only while the MAIN loop is also
        fresh. Called from the watchdog thread, so a dead/hung decision loop OR a
        dead watchdog both silence the ping and the external monitor pages."""
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

    def _decision_due(self) -> bool:
        return (time.monotonic() - self._last_decision_at) >= self.cfg.decision_interval_s

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
            run_postmortem(self.cfg, self.ledger, self.journal, day,
                           max_lessons=self.cfg.postmortem_max_lessons)
            self.state.set_postmortem_done(day)
        except Exception as e:
            log.warning("Nightly post-mortem failed: %s", e)

    def run_decision_cycle(self) -> None:
        if not self.broker.is_market_open():
            log.info("Market closed; skipping decision cycle.")
            self._maybe_run_postmortem()
            return

        self._reconcile_fills()
        self._backfill_exchange_exits()
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
            # A degraded ("unknown") read means we're flying blind on BOTH regime
            # and the sector cap — surface that at WARNING, not INFO (1B.7).
            if regime.label == "unknown":
                log.warning("Market regime: %s", regime.reason)
            else:
                log.info("Market regime: %s", regime.reason)
        else:
            self._regime_mult = 1.0
        self._record_equity_snapshot()
        account = self.broker.get_account()

        # De-risk the EXISTING book on a flip into risk-off (the regime multiplier
        # otherwise only shrinks NEW buys). Runs before new proposals so the trimmed
        # snapshot is what the buy path sizes against.
        if self.cfg.risk.regime_filter_enabled:
            self._apply_regime_trim(account, self.regime.assess())

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
        # The core-satellite ETF (Todo 1.6) is managed by _apply_core_fill, not by
        # Claude — drop it from the decision slate so the model doesn't churn the
        # core (buy/sell/thesis-decay it); it's held as a passive base allocation.
        if self.cfg.core_etf:
            base.discard(self.cfg.core_etf)
        discovered = self.screeners.scan(exclude=base) if self.cfg.screener.enabled else []
        symbols = sorted(base | {c.symbol for c in discovered})

        bundles = self.signals.gather(symbols)
        self._inject_discovery(bundles, discovered)

        # Deterministic thesis-decay exits (1B.4b): sell held names whose fresh
        # signals no longer corroborate the entry thesis, BEFORE asking Claude — so
        # a stale-thesis name is recycled even if the LLM is down, and we don't
        # spend tokens deciding on a name we've already exited.
        decayed = self._apply_thesis_decay_exits(bundles, account)
        if decayed:
            bundles = [b for b in bundles if b.symbol not in decayed]

        bench_stats = self.benchmark.compute()
        bench_line = self.benchmark.context_line(bench_stats)
        external = self.robinhood.holdings()
        # Reflection loop: our realized P&L per entry signal, fed back so Claude can
        # weight by what has actually paid off. Best-effort; never blocks a cycle.
        lessons = self._lessons()

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
            today_block = self.journal.render_today(account.equity)
        except Exception as e:
            log.warning("Could not render today block: %s", e)

        proposals = self.engine.decide(
            bundles, account, bench_line, external, lessons,
            today=today_block, buy_excluded=buy_excluded,
        )
        proposals = self._filter_to_slate(proposals, bundles, account)
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
        budget_caps = self._cycle_budget_caps(proposals, account)
        if not proposals:
            log.info("No actionable proposals this cycle.")
        else:
            for proposal in proposals:
                kinds = signal_kinds.get(proposal.symbol, [])
                if proposal.instrument is Instrument.OPTION:
                    self._handle_option(proposal, account, kinds)
                else:
                    self._handle_equity(
                        proposal, account, kinds,
                        cycle_budget_cap=budget_caps.get(proposal.symbol),
                    )
        # Core-satellite fill (Todo 1.6): deploy whatever cash the single-name book
        # left idle into the broad core ETF, so we're not structurally short the
        # benchmark. Runs EVEN when there were no proposals — that's exactly the
        # cash-drag case it exists to fix.
        self._apply_core_fill(account)
        # GA-2.3: keep the core's exchange-resident GTC stop sized to the
        # (growing) position — the core previously had NO exchange-side stop.
        self._ensure_core_stop(account)
        # Persist this cycle's freshly-submitted order ids so the next boot (even
        # after a crash between cycles) reconciles their fills (1B.9).
        self.state.set_pending_orders(self._pending_oids)

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

    def _lessons(self) -> str:
        parts = []
        try:
            attr = render_lessons(self.ledger)
            if attr:
                parts.append(attr)
        except Exception as e:
            log.warning("Could not render track-record lessons: %s", e)
        try:
            from .postmortem import read_curated
            curated = read_curated(self.cfg.postmortem_max_lessons)
            if curated:
                parts.append(curated)
        except Exception:
            pass  # postmortem module may not exist yet; silently skip
        return "\n\n".join(parts) if parts else ""

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
    def _reconcile_fills(self) -> None:
        """Confirm last cycle's orders actually filled. A recorded order id is only
        an intent — rejects and partial fills mean the ledger and our risk picture
        have DIVERGED from the broker's reality. That used to be log-only
        (advisory); now it's enforcing (goGA GA-2.1): a confirmed divergence halts
        NEW buys via the kill-switch file until a human deletes the file to
        acknowledge. Sells and the watchdog are never gated — closing out of a
        mis-booked position is exactly what we still want to work."""
        pending, self._pending_oids = self._pending_oids, []
        # Persist the cleared list immediately: these are about to be checked, so a
        # crash mid-reconcile must not re-examine (or re-strand) them next boot.
        self.state.set_pending_orders(self._pending_oids)
        mismatches: list[str] = []
        for oid, symbol in pending:
            status, filled, qty = self.broker.order_fill(oid)
            if status in ("filled", "unknown"):
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
                mismatches.append(f"{symbol} {status} ({filled:g}/{qty:g} filled)")
            elif qty and 0 < filled < qty:
                log.warning(
                    "Order %s (%s) PARTIAL: %g/%g filled (status=%s).",
                    oid, symbol, filled, qty, status,
                )
                # Non-terminal partial: may still fill more, so no correction yet —
                # re-queue it and let a later reconcile write the final number.
                self._pending_oids.append((oid, symbol))
                mismatches.append(f"{symbol} partial {filled:g}/{qty:g}")
            else:  # still new/accepted/pending_new long after submission — may yet
                # fill; not a confirmed divergence, so warn without halting.
                log.warning("Order %s (%s) still %s a full cycle later.", oid, symbol, status)
        if self._pending_oids:
            # Re-queued live partials must survive a crash before the cycle's
            # end-of-run persist, or their final fill never gets corrected.
            self.state.set_pending_orders(self._pending_oids)
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
                # stamped at the FILL time when known (falls back to now).
                self.state.register_exit(o["symbol"], when=ts)
        except Exception as e:  # bookkeeping must never break a decision cycle
            log.warning("Exchange-exit backfill failed: %s", e)

    # -- per-cycle budget fair-share (2026-07-06 all-LLY fix) ---------------- #
    def _cycle_budget_caps(self, proposals, account) -> dict[str, float]:
        """Split the cycle's deployable cash across ALL equity-buy proposals,
        weighted by conviction, and return {symbol: $cap}. Proposals are executed
        in the order Claude returns them, and every hard cap in RiskManager still
        applies — this only stops the FIRST buy from consuming the whole cycle's
        cash and starving every later idea to "Budget $0.00" (the 2026-07-06 log:
        10 LLY top-ups in one day while TSM/TDG/BIIB/T were rejected every
        cycle). Single-buy cycles get no share cap. Capital freed by sells this
        cycle isn't re-split; the core-ETF fill sweeps whatever is left."""
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
        Both paths are also captured in buy_excluded so the prompt's "excluded" section
        is complete and the backstop pass can check the full set."""
        r = self.cfg.risk
        equity = account.equity if account.equity > 0 else 1.0
        min_order = max(r.min_order_usd, equity * (r.min_order_pct / 100.0))
        buy_excluded: dict[str, str] = {}
        filtered: list = []
        for b in bundles:
            headroom, reason = self._buy_headroom_usd(b.symbol, account)
            if headroom < min_order:
                buy_excluded[b.symbol] = reason or "no buy headroom"
                if account.position_for(b.symbol) is not None:
                    filtered.append(b)  # keep: may need SELL/HOLD
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
        Logs any drops so it's auditable."""
        if not buy_excluded:
            return proposals
        kept = []
        for prop in proposals:
            if prop.action.value == "buy" and prop.symbol in buy_excluded:
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
    def _filter_to_slate(proposals, bundles, account):
        """Drop any proposal whose symbol was never presented to the model. The
        decision prompt embeds UNTRUSTED third-party text (headlines, social
        posts, curated-list names); a crafted payload could persuade the model to
        propose a pumped ticker no screener surfaced. The model may only act on
        the slate it was shown (candidate bundles) plus what we already hold
        (so closing a position is never blocked)."""
        allowed = {b.symbol for b in bundles} | {p.symbol for p in account.positions}
        kept = []
        for prop in proposals:
            if prop.symbol in allowed:
                kept.append(prop)
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
                self.ledger.record(TradeRecord.for_sell(
                    pos.symbol, f"regime risk-off trim {r.regime_trim_pct:.0f}%", oid,
                    qty=sell_qty, realized_pl_pct=pos.unrealized_pl_pct,
                    realized_pl=None, exit_reason="regime_trim",
                    exit_price=pos.current_price or None,
                ))
                # Keep the in-memory snapshot honest for the rest of the cycle and
                # re-protect the (now bracket-less) remainder via the watchdog.
                pos.qty = round(pos.qty - sell_qty, 6)
                pos.market_value = pos.qty * pos.current_price
                self.state.register_exits(
                    pos.symbol, r.default_stop_loss_pct, r.default_take_profit_pct,
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
            # The core-satellite ETF carries no per-name thesis, so signal ABSENCE
            # must not decay-exit it (Todo 1.6) — it's a passive base allocation.
            if self.cfg.core_etf and pos.symbol == self.cfg.core_etf:
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
                self.broker.cancel_open_orders_for(pos.symbol)
                oid = self.broker.close_position(pos.symbol)
                self.watchdog.forget(pos.symbol)
                self.ledger.record(TradeRecord.for_sell(
                    pos.symbol, "thesis decay: entry signals no longer corroborated",
                    oid, qty=pos.qty, realized_pl_pct=pos.unrealized_pl_pct,
                    realized_pl=pos.unrealized_pl, exit_reason="thesis_decay",
                    exit_price=pos.current_price or None,
                ))
                if oid:
                    self._pending_oids.append((oid, pos.symbol))
                    # Start the re-entry cooldown clock (churn guard).
                    self.state.register_exit(pos.symbol)
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
        if self.risk.kill_switch:
            return  # new buys halted — don't top up the core either
        r = self.cfg.risk
        equity = account.equity
        if equity <= 0:
            return
        deployed = sum(max(0.0, p.market_value) for p in account.positions)
        invested_pct = deployed / equity * 100.0
        # Never target beyond the no-leverage gross cap (respect the same ceiling
        # single-name buys do).
        target = min(self.cfg.target_invested_pct, r.max_gross_exposure_pct)
        if invested_pct >= target:
            return
        gap = equity * (target - invested_pct) / 100.0
        # Respect the cash buffer: keep min_cash_buffer_pct of equity uninvested.
        min_cash = equity * (r.min_cash_buffer_pct / 100.0)
        spendable = max(0.0, account.cash - min_cash)
        notional = round(min(gap, spendable), 2)
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
            "Core fill: bought $%.0f of %s (invested %.0f%% -> ~%.0f%%, target %.0f%%).",
            notional, etf, invested_pct,
            invested_pct + notional / equity * 100.0, target,
        )
        self.ledger.record(TradeRecord.from_core_fill(etf, notional, price, oid))
        self._pending_oids.append((oid, etf))
        self.state.register_entry(etf)
        # Fold into this cycle's snapshot so a later call sees the deployed capital.
        self._apply_pending_buy(
            account, etf, notional, price, notional / price if price > 0 else 0.0,
        )

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
            return
        desired_qty = float(int(pos.qty))
        desired_stop = round(pos.avg_entry_price * (1 - pct / 100.0), 2)
        if desired_stop <= 0:
            return
        existing = self.broker.open_stop_sells(etf)
        for o in existing:
            if (
                abs(o["qty"] - desired_qty) < 1.0
                and abs(o["stop_price"] - desired_stop) / desired_stop < 0.005
            ):
                return  # resting stop is already right — leave it alone
        with self._trade_lock:
            for o in existing:  # stale size/level — replace
                self.broker.cancel_order(o["id"])
            oid = self.broker.submit(OrderRequest(
                symbol=etf, side=Action.SELL, order_type=OrderType.STOP,
                tif=TIF.GTC, qty=desired_qty, stop_price=desired_stop,
            ))
        if oid:
            log.info(
                "Core stop: GTC stop resting for %g %s @ %.2f (%.0f%% under "
                "basis %.2f).", desired_qty, etf, desired_stop, pct,
                pos.avg_entry_price,
            )
        else:
            log.warning(
                "Core stop for %s could not be placed this cycle; the watchdog "
                "still guards it. Will retry next cycle.", etf,
            )

    # -- equity path -------------------------------------------------------- #
    def _handle_equity(
        self, proposal: TradeProposal, account, signal_kinds: list[str] | None = None,
        cycle_budget_cap: float | None = None,
    ) -> None:
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
        decision = self.risk.evaluate(
            proposal, account, price, vol, pending, days_to_earnings,
            sector, sector_exposure, self._regime_mult,
            max_held_corr=max_corr, corr_symbol=corr_sym,
            cycle_budget_cap=cycle_budget_cap,
            corr_data_missing=corr_missing,
        )
        log.info(
            "%s %s -> %s: %s | %s",
            proposal.action.value.upper(), proposal.symbol,
            decision.verdict.value, decision.reason, proposal.rationale[:100],
        )
        # Journal every verdict so rejects have a durable record (not just a log
        # line), and the 'Today so far' block can surface them to Claude next cycle.
        instr = proposal.instrument.value if hasattr(proposal.instrument, "value") else str(proposal.instrument)
        verdict_str = decision.verdict.value
        self._journal_decision(
            proposal.symbol, proposal.action.value, instr,
            proposal.conviction, proposal.target_weight_pct, verdict_str,
            decision.approved_notional if decision.verdict != RiskVerdict.REJECTED else 0.0,
            decision.reason, proposal.rationale[:120] if proposal.rationale else "",
        )
        if decision.verdict == RiskVerdict.REJECTED:
            return
        # Hold the trade lock across broker mutations so the watchdog thread can't
        # interleave an emergency close on the same symbol mid-operation.
        with self._trade_lock:
            if proposal.action.value == "sell":
                self.broker.cancel_open_orders_for(proposal.symbol)
                held = account.position_for(proposal.symbol)
                oid = self.broker.close_position(proposal.symbol)
                self.watchdog.forget(proposal.symbol)
                # The held position's unrealized P&L at close IS the realized
                # outcome — record it so this round-trip is attributable.
                self.ledger.record(TradeRecord.for_sell(
                    proposal.symbol, proposal.rationale, oid,
                    qty=held.qty if held else 0.0, key_signals=proposal.key_signals,
                    realized_pl_pct=held.unrealized_pl_pct if held else None,
                    realized_pl=held.unrealized_pl if held else None,
                    exit_reason="decision",
                    exit_price=held.current_price if held else None,
                ))
                if oid:
                    self._pending_oids.append((oid, proposal.symbol))
                    # Start the re-entry cooldown clock (churn guard).
                    self.state.register_exit(proposal.symbol)
                    # Reflect the close in this cycle's snapshot so later proposals
                    # see the freed capital / slot (see _apply_pending_buy).
                    self._apply_pending_close(account, proposal.symbol)
            else:  # buy (approved or resized)
                sub = self.broker.submit_from_decision(decision)
                if sub.order_id:
                    # Record the SUBMITTED qty/notional, not the decision's: the
                    # whole-share bracket path floors the sized qty (2.5 sh -> 2),
                    # and the dropped remainder must not live on as phantom
                    # position/cost in the ledger or the capital snapshot.
                    self.ledger.record(TradeRecord.from_equity(
                        decision, price, sub.order_id,
                        entry_signals=signal_kinds or [],
                        submitted_qty=sub.qty, submitted_cost=sub.notional))
                    self._pending_oids.append((sub.order_id, proposal.symbol))
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

    # -- options path (defined-risk, gated) -------------------------------- #
    def _handle_option(
        self, proposal: TradeProposal, account, signal_kinds: list[str] | None = None
    ) -> None:
        if self.options is None:
            log.info("Option proposal for %s ignored: options disabled.", proposal.symbol)
            return
        premium = self.options.estimate_net_premium(proposal)
        decision = self.risk.evaluate_option(proposal, account, premium)
        log.info(
            "OPTION %s %s -> %s: %s | %s",
            proposal.option_strategy, proposal.symbol,
            decision.verdict.value, decision.reason, proposal.rationale[:100],
        )
        if decision.verdict == RiskVerdict.REJECTED:
            return
        legs = self.options.build_legs(proposal)
        with self._trade_lock:
            oid = self.broker.submit_option_legs(legs, qty=int(decision.approved_qty))
        if oid:
            self.ledger.record(TradeRecord.from_option(
                decision, premium, oid, entry_signals=signal_kinds or []))
            self._pending_oids.append((oid, proposal.symbol))
