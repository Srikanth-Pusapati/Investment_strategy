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
from .insider import InsiderProvider
from .insider_edgar import EdgarInsiderProvider
from .macro import MacroProvider
from .news import NewsProvider
from .options_flow import OptionsFlowProvider

log = logging.getLogger("signals")


class SignalAggregator:
    def __init__(self, cfg: Config):
        self.per_symbol: list[SignalProvider] = [
            FundamentalsProvider(cfg),    # EBITDA, margins, leverage
            NewsProvider(cfg),            # headlines + sentiment (finnhub/VADER)
            CongressProvider(cfg),        # congressional trades (Quiver; if key)
            InsiderProvider(cfg),         # Form 4 insider via Finnhub (if key)
            EdgarInsiderProvider(cfg),    # Form 4 insider via SEC EDGAR (free; no key)
            OptionsFlowProvider(cfg),     # unusual options activity
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
