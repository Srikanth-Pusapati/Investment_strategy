"""Market-regime filter — scale aggressiveness to the market backdrop.

Buying the same size into a calm uptrend and into a VIX-spiking downtrend is how a
discovery bot quietly takes on its worst risk. This reads two classic, robust regime
gauges and turns them into a single RISK MULTIPLIER (0..1) the risk layer applies to
position sizing, so gross exposure shrinks automatically when the market is risk-off:

  - Trend: SPY vs its 200-day moving average (above = risk-on, below = risk-off).
  - Fear:  VIX level (calm < 20, elevated 20–30, high > 30).

multiplier = trend_factor * vol_factor, floored so we never size to ~zero on noise.
It is advisory data feeding a DETERMINISTIC gate in risk.py — the gate never depends
on the network. Source: yfinance (free, no key). FAILS OPEN: if the data can't be
read the multiplier is 1.0 (no change), matching the "never block on missing data"
rule used by the other guards. Per-cycle cached via new_cycle().
"""
from __future__ import annotations

import logging
import statistics
import threading
from dataclasses import dataclass

log = logging.getLogger("regime")

_SMA_DAYS = 200
_FLOOR = 0.25          # never shrink new positions below this fraction on regime alone


@dataclass
class Regime:
    multiplier: float          # 0.25..1.0 applied to position sizing
    label: str                 # "risk-on" | "neutral" | "risk-off" | "unknown"
    reason: str                # human/LLM-readable one-liner


class RegimeReader:
    def __init__(self) -> None:
        self._cached: Regime | None = None
        self._lock = threading.Lock()

    def new_cycle(self) -> None:
        """Drop the cached read so the next assess() recomputes. Call once per
        decision cycle."""
        with self._lock:
            self._cached = None

    def assess(self) -> Regime:
        with self._lock:
            if self._cached is not None:
                return self._cached
        regime = self._compute()
        with self._lock:
            self._cached = regime
        return regime

    # -- computation -------------------------------------------------------- #
    def _compute(self) -> Regime:
        spy = self._daily_closes("SPY", _SMA_DAYS + 10)
        vix = self._last_close("^VIX")
        if len(spy) < _SMA_DAYS or vix is None:
            return Regime(1.0, "unknown", "Regime data unavailable — full size.")

        sma = statistics.fmean(spy[-_SMA_DAYS:])
        price = spy[-1]
        above = price >= sma
        trend_factor = 1.0 if above else 0.5
        vol_factor = 1.0 if vix < 20 else (0.7 if vix < 30 else 0.4)
        mult = round(max(_FLOOR, trend_factor * vol_factor), 2)

        if mult >= 0.9:
            label = "risk-on"
        elif mult >= 0.5:
            label = "neutral"
        else:
            label = "risk-off"
        trend = "above" if above else "below"
        reason = (
            f"SPY {trend} 200dma ({price:.0f} vs {sma:.0f}), VIX {vix:.0f} "
            f"-> {label}, size x{mult:.2f}."
        )
        return Regime(mult, label, reason)

    # -- data (yfinance; keyless) ------------------------------------------ #
    @staticmethod
    def _daily_closes(symbol: str, days: int) -> list[float]:
        try:
            import yfinance as yf
            hist = yf.Ticker(symbol).history(period=f"{days + 20}d")
            return [float(c) for c in hist["Close"].tolist() if c == c]  # drop NaN
        except Exception as e:
            log.debug("regime closes for %s failed: %s", symbol, e)
            return []

    @classmethod
    def _last_close(cls, symbol: str) -> float | None:
        closes = cls._daily_closes(symbol, 5)
        return closes[-1] if closes else None
