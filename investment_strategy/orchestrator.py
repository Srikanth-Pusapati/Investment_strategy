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

from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import Timeout as RequestsTimeout
from urllib3.exceptions import ProtocolError

from .attribution import render_lessons
from .benchmark import BenchmarkTracker
from .config import Config
from .decision import DecisionEngine
from .earnings import EarningsCalendar
from .execution import AlpacaClient, OptionsHelper
from .ledger import TradeLedger, TradeRecord
from .models import (
    Candidate,
    Instrument,
    Position,
    RiskVerdict,
    SignalBundle,
    TradeProposal,
)
from .monitor import Watchdog
from .notify import Alerter
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
                with self._trade_lock:
                    self.watchdog.check_once()
            except _TRANSIENT_NET as e:
                log.warning(
                    "Watchdog tick skipped on a transient network error (%s); "
                    "retrying next tick.", e.__class__.__name__,
                )
            except Exception:
                log.exception("Watchdog tick failed; continuing.")
            self._stop.wait(self.cfg.monitor_interval_s)

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
        desired = self.cfg.kill_switch or file_kill
        if desired != self.risk.kill_switch:
            log.warning(
                "Kill switch -> %s (file=%s).", "ON" if desired else "off",
                self.cfg.kill_switch_file if file_kill else "n/a",
            )
        self.risk.kill_switch = desired

    # -- the slow cycle ----------------------------------------------------- #
    def run_decision_cycle(self) -> None:
        if not self.broker.is_market_open():
            log.info("Market closed; skipping decision cycle.")
            return

        self._reconcile_fills()
        # Fresh Quiver data this cycle, but pulled once and shared by the signal
        # and screener layers (both read the same cached live feeds).
        self.quiver.new_cycle()
        self.earnings.new_cycle()
        self.sectors.new_cycle()
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
        base = set(self.watchlist) | {p.symbol for p in account.positions}
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

        proposals = self.engine.decide(bundles, account, bench_line, external, lessons)
        if not proposals:
            log.info("No actionable proposals this cycle.")
        else:
            for proposal in proposals:
                kinds = signal_kinds.get(proposal.symbol, [])
                if proposal.instrument is Instrument.OPTION:
                    self._handle_option(proposal, account, kinds)
                else:
                    self._handle_equity(proposal, account, kinds)
        # Core-satellite fill (Todo 1.6): deploy whatever cash the single-name book
        # left idle into the broad core ETF, so we're not structurally short the
        # benchmark. Runs EVEN when there were no proposals — that's exactly the
        # cash-drag case it exists to fix.
        self._apply_core_fill(account)
        # Persist this cycle's freshly-submitted order ids so the next boot (even
        # after a crash between cycles) reconciles their fills (1B.9).
        self.state.set_pending_orders(self._pending_oids)

    def _refresh_dashboard(self) -> None:
        """Regenerate the live dashboard HTML after a cycle so the tracker stays
        fresh (off unless DASHBOARD_FILE is set). Best-effort; never blocks."""
        if not self.cfg.dashboard_file:
            return
        try:
            from pathlib import Path

            from .dashboard import generate
            generate(Path(self.cfg.dashboard_file), live=True)
        except Exception as e:
            log.warning("Dashboard refresh failed: %s", e)

    def _sector_context(self, symbol: str, account) -> tuple[str | None, float]:
        """(sector of `symbol`, $ already held in that sector) for the risk
        sector-concentration cap. Best-effort — a lookup miss returns (None, 0)
        so the cap is simply skipped for that name."""
        try:
            sector = self.sectors.sector_for(symbol)
            if not sector:
                return None, 0.0
            held = {p.symbol: p.market_value for p in account.positions}
            return sector, self.sectors.exposure_by_sector(held).get(sector, 0.0)
        except Exception as e:
            log.warning("sector context for %s failed: %s", symbol, e)
            return None, 0.0

    def _record_equity_snapshot(self) -> None:
        """Persist a once-per-day account P&L snapshot (true total return from the
        Alpaca account, not the ledger). Best-effort; never blocks a cycle."""
        try:
            self.equity_history.snapshot(compute_status(self.broker))
        except Exception as e:
            log.warning("Could not record equity snapshot: %s", e)

    def _lessons(self) -> str:
        try:
            return render_lessons(self.ledger)
        except Exception as e:  # attribution must never break the trade loop
            log.warning("Could not render track-record lessons: %s", e)
            return ""

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
        can drift from reality. Surface that loudly instead of trusting submission."""
        pending, self._pending_oids = self._pending_oids, []
        # Persist the cleared list immediately: these are about to be checked, so a
        # crash mid-reconcile must not re-examine (or re-strand) them next boot.
        self.state.set_pending_orders(self._pending_oids)
        for oid, symbol in pending:
            status, filled, qty = self.broker.order_fill(oid)
            if status in ("filled", "unknown"):
                continue
            if status in ("rejected", "canceled", "expired"):
                log.error(
                    "Order %s (%s) ended %s with %g/%g filled — ledger records an "
                    "intent that did not (fully) execute.", oid, symbol, status, filled, qty,
                )
            elif qty and 0 < filled < qty:
                log.warning(
                    "Order %s (%s) PARTIAL: %g/%g filled (status=%s).",
                    oid, symbol, filled, qty, status,
                )
            else:  # still new/accepted/pending_new long after submission
                log.warning("Order %s (%s) still %s a full cycle later.", oid, symbol, status)

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
                sell_qty = round(pos.qty * frac, 6)
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
                ))
                if oid:
                    self._pending_oids.append((oid, pos.symbol))
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
        if notional < max(r.min_order_usd, 1.0):
            return
        price = self.broker.latest_price(etf)
        with self._trade_lock:
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

    # -- equity path -------------------------------------------------------- #
    def _handle_equity(
        self, proposal: TradeProposal, account, signal_kinds: list[str] | None = None
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
        decision = self.risk.evaluate(
            proposal, account, price, vol, pending, days_to_earnings,
            sector, sector_exposure, self._regime_mult,
        )
        log.info(
            "%s %s -> %s: %s | %s",
            proposal.action.value.upper(), proposal.symbol,
            decision.verdict.value, decision.reason, proposal.rationale[:100],
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
                ))
                if oid:
                    self._pending_oids.append((oid, proposal.symbol))
                    # Reflect the close in this cycle's snapshot so later proposals
                    # see the freed capital / slot (see _apply_pending_buy).
                    self._apply_pending_close(account, proposal.symbol)
            else:  # buy (approved or resized)
                oid, fractional = self.broker.submit_from_decision(decision)
                if oid:
                    self.ledger.record(TradeRecord.from_equity(
                        decision, price, oid, entry_signals=signal_kinds or []))
                    self._pending_oids.append((oid, proposal.symbol))
                    # Start (or preserve) the hold clock for the deterministic
                    # time-stop (1B.4). register_entry only stamps a first entry.
                    self.state.register_entry(proposal.symbol)
                    # Fold this fill back into the once-per-cycle snapshot so the
                    # REST of the cycle's proposals treat the capital as deployed
                    # (1B.3 — closes the intra-cycle over-deploy hole).
                    self._apply_pending_buy(
                        account, proposal.symbol, decision.approved_notional,
                        price, decision.approved_qty,
                    )
                    if fractional:
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
