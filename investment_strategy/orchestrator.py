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

from .benchmark import BenchmarkTracker
from .config import Config
from .decision import DecisionEngine
from .execution import AlpacaClient, OptionsHelper
from .ledger import TradeLedger, TradeRecord
from .models import Instrument, RiskVerdict, TradeProposal
from .monitor import Watchdog
from .portfolio import RobinhoodReader
from .risk import RiskManager
from .signals import SignalAggregator
from .state import PortfolioState

log = logging.getLogger("orchestrator")

# Default candidate universe. Override with the WATCHLIST env var (comma list).
# Current holdings are always added so existing positions get re-evaluated.
DEFAULT_WATCHLIST = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA"]


class Orchestrator:
    def __init__(self, cfg: Config, watchlist: list[str] | None = None):
        self.cfg = cfg
        self.broker = AlpacaClient(cfg)
        self.signals = SignalAggregator(cfg)
        self.engine = DecisionEngine(cfg)
        # One persisted risk-state instance shared by the risk gate and watchdog
        # so peak equity, the drawdown halt, and the halt latch are consistent.
        self.state = PortfolioState(cfg.state_file)
        self.risk = RiskManager(cfg.risk, kill_switch=cfg.kill_switch, state=self.state)
        self.watchdog = Watchdog(cfg, self.broker, state=self.state)
        self.benchmark = BenchmarkTracker(cfg, self.broker, symbol=cfg.benchmark_symbol)
        self.robinhood = RobinhoodReader(cfg)
        self.options = OptionsHelper(cfg) if cfg.risk.options_enabled else None
        self.ledger = TradeLedger()
        self.watchlist = watchlist or DEFAULT_WATCHLIST
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
        self._pending_oids: list[tuple[str, str]] = []  # (order_id, symbol)

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

        while not self._stop.is_set():
            try:
                self._refresh_runtime_controls()
                if self._decision_due():
                    self.run_decision_cycle()
                    self._last_decision_at = time.monotonic()
            except KeyboardInterrupt:
                log.info("Interrupted — exiting.")
                break
            except Exception:
                log.exception("Decision tick failed; continuing.")
            # Wake promptly on shutdown; otherwise tick on the monitor cadence.
            self._stop.wait(self.cfg.monitor_interval_s)
        self._stop.set()
        wd_thread.join(timeout=self.cfg.monitor_interval_s + 5)

    def _watchdog_loop(self) -> None:
        """Independent safety loop: closing positions is never gated, so this runs
        regardless of the kill switch or what the decision thread is doing."""
        while not self._stop.is_set():
            try:
                with self._trade_lock:
                    self.watchdog.check_once()
            except Exception:
                log.exception("Watchdog tick failed; continuing.")
            self._stop.wait(self.cfg.monitor_interval_s)

    def _decision_due(self) -> bool:
        return (time.monotonic() - self._last_decision_at) >= self.cfg.decision_interval_s

    def _warn_on_weak_safety_config(self) -> None:
        """Loudly flag safety nets that are disabled, so an off-by-default setting
        isn't mistaken for a configured-and-safe one."""
        if self.cfg.risk.equity_floor_usd <= 0:
            log.warning(
                "EQUITY_FLOOR_USD is 0 (off) — the latched liquidate-and-halt "
                "catastrophe guard is DISABLED. Set it before trading real size."
            )
        if self.cfg.robinhood_enabled and self.cfg.robinhood_mcp_token:
            log.warning(
                "Robinhood MCP enabled — we call READ tools only, but a "
                "trade-capable token could place orders if misused. Use a "
                "read-scoped token."
            )

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
        account = self.broker.get_account()
        symbols = sorted(set(self.watchlist) | {p.symbol for p in account.positions})

        bundles = self.signals.gather(symbols)
        bench_stats = self.benchmark.compute()
        bench_line = self.benchmark.context_line(bench_stats)
        external = self.robinhood.holdings()

        proposals = self.engine.decide(bundles, account, bench_line, external)
        if not proposals:
            log.info("No actionable proposals this cycle.")
            return

        for proposal in proposals:
            if proposal.instrument is Instrument.OPTION:
                self._handle_option(proposal, account)
            else:
                self._handle_equity(proposal, account)

    # -- fill reconciliation ------------------------------------------------ #
    def _reconcile_fills(self) -> None:
        """Confirm last cycle's orders actually filled. A recorded order id is only
        an intent — rejects and partial fills mean the ledger and our risk picture
        can drift from reality. Surface that loudly instead of trusting submission."""
        pending, self._pending_oids = self._pending_oids, []
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

    # -- equity path -------------------------------------------------------- #
    def _handle_equity(self, proposal: TradeProposal, account) -> None:
        price = self.broker.latest_price(proposal.symbol)
        vol = self.broker.annualized_vol(proposal.symbol)
        pending = (
            self.broker.open_buy_notional(proposal.symbol)
            if proposal.action.value == "buy" else 0.0
        )
        decision = self.risk.evaluate(proposal, account, price, vol, pending)
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
                self.ledger.record(TradeRecord.for_sell(
                    proposal.symbol, proposal.rationale, oid,
                    qty=held.qty if held else 0.0, key_signals=proposal.key_signals,
                ))
                if oid:
                    self._pending_oids.append((oid, proposal.symbol))
            else:  # buy (approved or resized)
                oid, fractional = self.broker.submit_from_decision(decision)
                if oid:
                    self.ledger.record(TradeRecord.from_equity(decision, price, oid))
                    self._pending_oids.append((oid, proposal.symbol))
                    if fractional:
                        # Fractional orders carry no exchange-side bracket, so the
                        # watchdog enforces the hard stop / take-profit instead.
                        self.state.register_exits(
                            proposal.symbol, decision.stop_loss_pct, decision.take_profit_pct,
                        )

    # -- options path (defined-risk, gated) -------------------------------- #
    def _handle_option(self, proposal: TradeProposal, account) -> None:
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
            self.ledger.record(TradeRecord.from_option(decision, premium, oid))
            self._pending_oids.append((oid, proposal.symbol))
