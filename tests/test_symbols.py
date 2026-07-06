"""Broker->Yahoo symbol mapping: class shares use a dot at Alpaca/SEC/Quiver
but a dash at Yahoo — unmapped, yfinance returns "possibly delisted" and the
name trades on incomplete signals (the BRK.B bug).

Runnable two ways:
    .venv/bin/python tests/test_symbols.py
    .venv/bin/pytest tests/
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.symbols import yahoo_symbol


def test_class_shares_map_dot_to_dash():
    assert yahoo_symbol("BRK.B") == "BRK-B"
    assert yahoo_symbol("BF.B") == "BF-B"


def test_plain_and_index_tickers_pass_through():
    assert yahoo_symbol("AAPL") == "AAPL"
    assert yahoo_symbol("^VIX") == "^VIX"


if __name__ == "__main__":
    test_class_shares_map_dot_to_dash()
    test_plain_and_index_tickers_pass_through()
    print("ok")
