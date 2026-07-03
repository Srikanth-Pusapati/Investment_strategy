"""Tests for shared model helpers — currently the ticker validator that keeps
feed placeholders (EDGAR's literal "N/A" issuerTradingSymbol, etc.) out of the
symbol universe.

Runnable two ways:
    .venv/bin/python tests/test_models.py
    .venv/bin/pytest tests/
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.models import is_valid_ticker


def test_accepts_plain_and_five_letter_tickers():
    for sym in ("A", "AAPL", "RIVN", "ASMVY", "LZRFY"):
        assert is_valid_ticker(sym), sym


def test_accepts_class_share_suffixes():
    for sym in ("BRK.B", "BF-B"):
        assert is_valid_ticker(sym), sym


def test_rejects_placeholders():
    for sym in ("N/A", "NA", "NONE", "NULL", ""):
        assert not is_valid_ticker(sym), sym


def test_rejects_crypto_pairs_and_junk():
    for sym in ("BTC-USD", "TOOLONGG", "12AB", "AAPL/B", "aapl"):
        assert not is_valid_ticker(sym), sym


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {fn.__name__}: {e!r}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
