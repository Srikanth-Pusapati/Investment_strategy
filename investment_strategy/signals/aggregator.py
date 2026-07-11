"""Gathers every provider's signals into one SignalBundle per symbol, with
market-wide (macro) signals attached as shared context.
"""
from __future__ import annotations

import logging

from ..config import Config
from ..models import SignalBundle
from .base import SignalProvider
from .congress import CongressProvider
from .fundamentals import FundamentalsProvider
from .govcontracts import GovContractsProvider
from .insider import InsiderProvider
from .insider_edgar import EdgarInsiderProvider
from .macro import MacroProvider
from .news import NewsProvider
from .offexchange import OffExchangeProvider
from .options_chain import OptionsChainProvider
from .options_flow import OptionsFlowProvider
from .quiver_client import QuiverClient
from .technical import TechnicalProvider

log = logging.getLogger("signals")


class SignalAggregator:
    def __init__(self, cfg: Config, quiver: QuiverClient | None = None):
        # One shared Quiver client so every Quiver-backed provider (and the
        # screener, when handed the same instance) pulls each live feed once.
        self.quiver = quiver or QuiverClient(cfg.quiver_api_key)
        self.per_symbol: list[SignalProvider] = [
            FundamentalsProvider(cfg),    # EBITDA, margins, leverage
            TechnicalProvider(cfg),       # RSI, MACD, trend (yfinance; no key)
            NewsProvider(cfg),            # headlines + sentiment (finnhub/VADER)
            CongressProvider(cfg, self.quiver),  # congressional trades (Quiver; if key)
            OffExchangeProvider(cfg, self.quiver),  # dark-pool short vol (Quiver; ~1d lag)
            GovContractsProvider(cfg, self.quiver),  # federal contract awards (Quiver; catalyst)
            InsiderProvider(cfg),         # Form 4 insider via Finnhub (if key)
            EdgarInsiderProvider(cfg),    # Form 4 insider via SEC EDGAR (free; no key)
            OptionsFlowProvider(cfg),     # unusual options activity
            OptionsChainProvider(cfg),    # ATM IV / skew / OI lean (Alpaca; C.4)
        ]
        self.market_wide: list[SignalProvider] = [MacroProvider(cfg)]

    def gather(self, symbols: list[str]) -> list[SignalBundle]:
        # Market context fetched once and shared across all bundles.
        context = []
        for p in self.market_wide:
            context.extend(p.safe_fetch(symbols))

        # Per-symbol signals, indexed by symbol.
        by_symbol: dict[str, list] = {s: [] for s in symbols}
        for p in self.per_symbol:
            for sig in p.safe_fetch(symbols):
                if sig.symbol in by_symbol:
                    by_symbol[sig.symbol].append(sig)

        bundles = [
            SignalBundle(symbol=s, signals=sigs, market_context=context)
            for s, sigs in by_symbol.items()
            if sigs  # drop symbols we couldn't gather anything on
        ]
        log.info(
            "Gathered signals for %d/%d symbols (+%d market signals).",
            len(bundles), len(symbols), len(context),
        )
        return bundles
