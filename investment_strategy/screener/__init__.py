"""Market-discovery layer.

Screeners scan the market for smart-money activity and emit candidate symbols
*before* the per-symbol signal layer runs — the piece that lets buy ideas
originate from the market rather than from a hand-typed watchlist. Discovered
candidates are unioned into the normal signals -> decide -> risk -> execute
pipeline; every resulting order still passes the deterministic RiskManager.
"""
from .aggregator import ScreenerAggregator

__all__ = ["ScreenerAggregator"]
