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
        self.risk = RiskManager(cfg.risk, kill_switch=cfg.kill_switch)
        self.watchdog = Watchdog(cfg, self.broker)
        self.benchmark = BenchmarkTracker(cfg, self.broker, symbol=cfg.benchmark_symbol)
        self.robinhood = RobinhoodReader(cfg)
        self.options = OptionsHelper(cfg) if cfg.risk.options_enabled else None
        self.ledger = TradeLedger()
        self.watchlist = watchlist or DEFAULT_WATCHLIST
        self._last_decision_at = 0.0

    # -- main loop ---------------------------------------------------------- #
    def run(self) -> None:
        mode = "LIVE 🔴" if self.cfg.is_live else "paper"
        log.info(
            "Starting orchestrator (%s). Kill switch: %s. Options: %s. Benchmark: %s.",
            mode, "ON" if self.cfg.kill_switch else "off",
            "on" if self.cfg.risk.options_enabled else "off", self.cfg.benchmark_symbol,
        )
        while True:
            try:
                self.watchdog.check_once()
                if self._decision_due():
                    self.run_decision_cycle()
                    self._last_decision_at = time.monotonic()
            except KeyboardInterrupt:
                log.info("Interrupted — exiting.")
                break
            except Exception:
                log.exception("Tick failed; continuing.")
            time.sleep(self.cfg.monitor_interval_s)

    def _decision_due(self) -> bool:
        return (time.monotonic() - self._last_decision_at) >= self.cfg.decision_interval_s

    # -- the slow cycle ----------------------------------------------------- #
    def run_decision_cycle(self) -> None:
        if not self.broker.is_market_open():
            log.info("Market closed; skipping decision cycle.")
            return

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

    # -- equity path -------------------------------------------------------- #
    def _handle_equity(self, proposal: TradeProposal, account) -> None:
        price = self.broker.latest_price(proposal.symbol)
        vol = self.broker.annualized_vol(proposal.symbol)
        decision = self.risk.evaluate(proposal, account, price, vol)
        log.info(
            "%s %s -> %s: %s | %s",
            proposal.action.value.upper(), proposal.symbol,
            decision.verdict.value, decision.reason, proposal.rationale[:100],
        )
        if decision.verdict == RiskVerdict.REJECTED:
            return
        if proposal.action.value == "sell":
            self.broker.cancel_open_orders_for(proposal.symbol)
            held = account.position_for(proposal.symbol)
            oid = self.broker.close_position(proposal.symbol)
            self.watchdog.forget(proposal.symbol)
            self.ledger.record(TradeRecord.for_sell(
                proposal.symbol, proposal.rationale, oid,
                qty=held.qty if held else 0.0, key_signals=proposal.key_signals,
            ))
        else:  # buy (approved or resized) -> bracket order
            oid = self.broker.submit_from_decision(decision)
            if oid:
                self.ledger.record(TradeRecord.from_equity(decision, price, oid))

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
        oid = self.broker.submit_option_legs(legs, qty=int(decision.approved_qty))
        if oid:
            self.ledger.record(TradeRecord.from_option(decision, premium, oid))
