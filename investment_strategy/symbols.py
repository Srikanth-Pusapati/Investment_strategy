"""Vendor symbol conventions.

Alpaca / SEC / Quiver spell US class shares with a dot (BRK.B, BF.B); Yahoo
Finance spells them with a dash (BRK-B). Every yfinance call must translate at
the boundary or class-share tickers silently return no data ("possibly
delisted") and the name trades on incomplete signals. Internal symbols stay in
dot form everywhere — only the outbound yfinance request is mapped.
"""
from __future__ import annotations


def yahoo_symbol(symbol: str) -> str:
    """Map a broker-style ticker to Yahoo's spelling. Index tickers (^VIX) and
    plain tickers pass through unchanged."""
    return symbol.replace(".", "-")
