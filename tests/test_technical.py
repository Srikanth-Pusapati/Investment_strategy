"""Tests for the technical-signal indicator math (RSI, MACD, trend score).

Pure functions, no network — we feed synthetic price series and assert the
indicators and the composite lean behave directionally.

Runnable two ways:
    .venv/bin/python tests/test_technical.py     # standalone, no pytest needed
    .venv/bin/pytest tests/                       # if pytest is installed
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.signals.technical import TechnicalProvider as T


def test_rsi_all_gains_is_max():
    closes = [100 + i for i in range(30)]      # monotonically rising
    assert T._rsi(closes, 14) == 100.0


def test_rsi_all_losses_is_min():
    closes = [100 - i for i in range(30)]       # monotonically falling
    assert T._rsi(closes, 14) == 0.0


def test_rsi_neutral_band_for_choppy_series():
    closes = [100 + (1 if i % 2 else -1) for i in range(40)]  # flat oscillation
    rsi = T._rsi(closes, 14)
    assert 30 < rsi < 70, rsi


def test_macd_positive_in_uptrend():
    closes = [100 * (1.01 ** i) for i in range(60)]  # steady uptrend
    macd, signal = T._macd(closes)
    assert macd > 0, macd
    assert macd > signal, (macd, signal)         # fast above signal = bullish


def test_macd_negative_in_downtrend():
    # In a sustained downtrend the MACD line sits below zero. (Histogram sign is
    # a decay artifact for smooth monotonic series, so we don't assert the cross.)
    closes = [100 * (0.99 ** i) for i in range(60)]
    macd, signal = T._macd(closes)
    assert macd < 0, macd
    assert signal < 0, signal


def test_sma_none_when_insufficient_history():
    assert T._sma([1, 2, 3], 50) is None
    assert T._sma(list(range(60)), 50) is not None


def test_score_bullish_uptrend():
    # Uptrend, bullish MACD histogram, neutral RSI -> positive lean.
    s = T._score(rsi=55, macd_hist=1.0, price=100, sma50=95, sma200=90)
    assert s > 0.4, s


def test_score_bearish_downtrend():
    s = T._score(rsi=45, macd_hist=-1.0, price=80, sma50=90, sma200=100)
    assert s < -0.4, s


def test_score_overbought_trims_lean():
    hot = T._score(rsi=85, macd_hist=1.0, price=100, sma50=95, sma200=90)
    cool = T._score(rsi=55, macd_hist=1.0, price=100, sma50=95, sma200=90)
    assert hot < cool, (hot, cool)               # overbought RSI reduces the lean


def test_score_bounded():
    s = T._score(rsi=5, macd_hist=1000, price=100, sma50=10, sma200=5)
    assert -1.0 <= s <= 1.0


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
