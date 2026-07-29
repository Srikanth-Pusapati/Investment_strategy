"""Market-regime filter — scale aggressiveness to the market backdrop.

Buying the same size into a calm uptrend and into a VIX-spiking downtrend is how a
discovery bot quietly takes on its worst risk. This reads two classic, robust regime
gauges and turns them into a single RISK MULTIPLIER (0..1) the risk layer applies to
position sizing, so gross exposure shrinks automatically when the market is risk-off:

  - Trend: SPY vs its 200-day moving average (above = risk-on, below = risk-off).
  - Fear:  VIX level (calm < 20, elevated 20–30, high > 30), PLUS the VIX term
           structure (spot ^VIX vs 3-month ^VIX3M): backwardation (spot above
           3-month) is near-term fear bid over longer-term — an earlier risk-off
           tell than the spot LEVEL crossing 20, so it forces at least the
           elevated vol tier. It only ever tightens sizing, never loosens it.

multiplier = trend_factor * vol_factor, floored so we never size to ~zero on noise.
It is advisory data feeding a DETERMINISTIC gate in risk.py — the gate never depends
on the network. Source: yfinance (free, no key). Per-cycle cached via new_cycle().

DEGRADED-DATA POSTURE (1B.7): the regime filter AND the sector-concentration cap
both source yfinance, so ONE yfinance outage silently drops BOTH risk-off guards
at once — during exactly the vol spike they exist for. So a failed read no longer
"fails open" to full size: it returns a DEGRADED multiplier (< 1.0) and logs
LOUDLY. We still trade (never block on missing data), just SMALLER while flying
blind, on the assumption the sector cap is blind too.
"""
from __future__ import annotations

import logging
import statistics
import threading
from dataclasses import dataclass

log = logging.getLogger("regime")

_SMA_DAYS = 200
_FLOOR = 0.25          # never shrink new positions below this fraction on regime alone
# Size DOWN to this fraction when the regime read fails (yfinance degraded), rather
# than to full — because the same outage almost certainly blinds the sector cap too.
_DEFAULT_DEGRADED_MULT = 0.5
# Force at least the "elevated" vol tier when the VIX term structure inverts (spot
# ^VIX above 3-month ^VIX3M = backwardation). Applied via min(), so it only ever
# tightens the level-based vol_factor, never loosens it.
_BACKWARDATION_VOL_FACTOR = 0.7


@dataclass
class Regime:
    multiplier: float          # 0.25..1.0 applied to position sizing
    label: str                 # "risk-on" | "neutral" | "risk-off" | "unknown"
    reason: str                # human/LLM-readable one-liner
    # The LONG-RUN market direction on its own: SPY above/below its 200dma,
    # BEFORE the label folds it together with the VIX read. The label can say
    # "risk-off" in a vol-spiked uptrend or "neutral" in a calm downtrend, so
    # direction-sensitive consumers (the option call/put gate) key on this
    # field, never on the label. "" = unknown (degraded read).
    trend: str = ""            # "up" | "down" | ""
    # TODAY'S move: SPY's last bar vs the prior daily close (during the
    # session yfinance's last daily bar is the live partial bar, so this is
    # the intraday move; after hours it's the day's close-to-close). The
    # 200dma trend above is deliberately slow — this is the FAST read that
    # drives the falling-tape core defense and the index-put sanction.
    # None = unknown (degraded read); consumers fail open.
    day_change_pct: float | None = None


class RegimeReader:
    def __init__(self, degraded_mult: float = _DEFAULT_DEGRADED_MULT) -> None:
        # Clamp: a degraded read must SIZE DOWN (0..1), never inflate or zero out.
        self.degraded_mult = max(_FLOOR, min(1.0, degraded_mult))
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
            # yfinance is degraded -> the sector cap is almost certainly blind too.
            # Size DOWN (not full) and shout, so a correlated data outage can't
            # silently drop two risk-off guards during a vol spike (1B.7).
            mult = self.degraded_mult
            log.warning(
                "Regime data unavailable (yfinance degraded) — sizing DOWN to "
                "x%.2f; the sector cap is likely blind too. Investigate the feed.",
                mult,
            )
            return Regime(
                mult, "unknown",
                f"Regime data unavailable (yfinance degraded) — defensively "
                f"sizing DOWN to x{mult:.2f} (sector cap likely blind too).",
            )

        sma = statistics.fmean(spy[-_SMA_DAYS:])
        price = spy[-1]
        day_change = (
            round((price / spy[-2] - 1.0) * 100.0, 2) if len(spy) >= 2 and spy[-2] > 0
            else None
        )
        above = price >= sma
        trend_factor = 1.0 if above else 0.5
        vol_factor = 1.0 if vix < 20 else (0.7 if vix < 30 else 0.4)
        # VIX term structure: when spot ^VIX rises ABOVE 3-month ^VIX3M the curve
        # is in BACKWARDATION — near-term fear bid over longer-term, historically
        # an earlier risk-off tell than the spot LEVEL crossing 20. Force at least
        # the elevated tier even when spot looks calm; min() means it never loosens
        # an already-stressed read. ^VIX3M is an ENHANCEMENT, not a requirement: if
        # it's unavailable we keep the level-only read (NO degraded posture — SPY
        # and spot VIX are still in hand, so the two risk-off guards aren't blind).
        vix3m = self._last_close("^VIX3M")
        backwardated = vix3m is not None and vix > vix3m
        if backwardated:
            vol_factor = min(vol_factor, _BACKWARDATION_VOL_FACTOR)
        mult = round(max(_FLOOR, trend_factor * vol_factor), 2)

        if mult >= 0.9:
            label = "risk-on"
        elif mult >= 0.5:
            label = "neutral"
        else:
            label = "risk-off"
        trend = "above" if above else "below"
        term = ""
        if vix3m is not None:
            term = f", VIX3M {vix3m:.1f} ({'BACKWARDATION' if backwardated else 'contango'})"
        day_part = f", today {day_change:+.1f}%" if day_change is not None else ""
        # VIX at one decimal: the old %.0f rounded a sub-20 read up to "VIX 20"
        # and made the calm tier look like a missed down-scale (Jul 29 audit).
        reason = (
            f"SPY {trend} 200dma ({price:.0f} vs {sma:.0f}{day_part}), VIX {vix:.1f}{term} "
            f"-> {label}, size x{mult:.2f}."
        )
        return Regime(
            mult, label, reason, trend="up" if above else "down",
            day_change_pct=day_change,
        )

    # -- data (yfinance; keyless) ------------------------------------------ #
    @staticmethod
    def _daily_closes(symbol: str, days: int) -> list[float]:
        try:
            import yfinance as yf
            from .symbols import yahoo_symbol
            hist = yf.Ticker(yahoo_symbol(symbol)).history(period=f"{days + 20}d")
            return [float(c) for c in hist["Close"].tolist() if c == c]  # drop NaN
        except Exception as e:
            log.debug("regime closes for %s failed: %s", symbol, e)
            return []

    @classmethod
    def _last_close(cls, symbol: str) -> float | None:
        closes = cls._daily_closes(symbol, 5)
        return closes[-1] if closes else None
