"""Off-exchange / dark-pool short-volume signal.

Source: Quiver Quantitative `live/offexchange` (FINRA off-exchange short volume),
read through the SHARED, per-cycle-cached QuiverClient. This is the most timely
Quiver dataset (~1-day lag) — a near-real-time read on institutional short
pressure — so the decision prompt may weight it more than the ~45-day congress
signal. Disabled without a key.

Direction: short_volume / total_volume, centered at 0.5. A ratio above 0.5 (more
of the off-exchange tape printing short) leans bearish; below 0.5 leans bullish.
The lean is deliberately damped (capped at ±0.6) because off-exchange short volume
is noisy — much of it is market-maker hedging of retail flow, not directional
conviction — so it's a timing/positioning tell, never a standalone thesis.

SCHEMA NOTE: field names are parsed defensively (several candidate keys) because
the exact Quiver payload isn't pinned here — VERIFY against the live response and
trim the candidate lists once confirmed. A row that yields neither a short ratio
nor a DPI is skipped (degrades to no signal), never guessed.
"""
from __future__ import annotations

from typing import Any

from ..config import Config
from ..models import Signal, SignalKind
from .base import SignalProvider
from .quiver_client import QuiverClient

# Candidate field names (first present wins). Trim once the live schema is confirmed.
_SHORT_KEYS = ("Sht_Vol", "ShortVolume", "short_volume", "OTC_Short")
_TOTAL_KEYS = ("Tot_Vol", "TotalVolume", "total_volume", "OTC_Total")
_DPI_KEYS = ("DPI", "DarkPoolIndex", "dpi")
_DATE_KEYS = ("Date", "date")
_TICKER_KEYS = ("Ticker", "ticker", "Symbol")

_MAX_LEAN = 0.6   # cap: a noisy, timely tell should never dominate the thesis


class OffExchangeProvider(SignalProvider):
    name = "offexchange"

    def __init__(self, cfg: Config, quiver: QuiverClient | None = None):
        self.cfg = cfg
        self.quiver = quiver or QuiverClient(cfg.quiver_api_key)

    @property
    def enabled(self) -> bool:
        return self.quiver.enabled

    def fetch(self, symbols: list[str]) -> list[Signal]:
        wanted = {s.upper() for s in symbols}

        # The feed may carry several days per ticker; keep only the most recent row
        # per wanted symbol (latest Date wins).
        latest: dict[str, dict[str, Any]] = {}
        for row in self.quiver.live("offexchange"):
            sym = self._str(row, _TICKER_KEYS).upper()
            if sym not in wanted:
                continue
            d = self._str(row, _DATE_KEYS)
            prev = latest.get(sym)
            if prev is None or d >= self._str(prev, _DATE_KEYS):
                latest[sym] = row

        signals: list[Signal] = []
        for sym, row in latest.items():
            ratio = self._short_ratio(row)
            if ratio is None:
                continue  # no usable direction -> skip, don't guess
            # Center at 0.5; >50% short = bearish. Damped and clamped.
            score = round(max(-_MAX_LEAN, min(_MAX_LEAN, -(ratio - 0.5) * 2 * _MAX_LEAN)), 3)
            dpi = self._num(row, _DPI_KEYS)
            dpi_txt = f", DPI {dpi:.2f}" if dpi is not None else ""
            lean = "short pressure" if ratio > 0.5 else "buy-side lean"
            signals.append(Signal(
                kind=SignalKind.OFFEXCHANGE,
                symbol=sym,
                summary=f"Off-exchange short vol {ratio*100:.0f}% ({lean}){dpi_txt} "
                        "(~1d lag; noisy).",
                score=score,
                source="quiver",
                data={"short_ratio": round(ratio, 4), "dpi": dpi},
            ))
        return signals

    # -- defensive field parsing ------------------------------------------- #
    def _short_ratio(self, row: dict[str, Any]) -> float | None:
        """short/total if both present; else fall back to DPI as the off-exchange
        proxy; else None (unusable). Guards against zero/garbage totals."""
        short = self._num(row, _SHORT_KEYS)
        total = self._num(row, _TOTAL_KEYS)
        if short is not None and total and total > 0:
            r = short / total
            return r if 0.0 <= r <= 1.0 else None
        dpi = self._num(row, _DPI_KEYS)
        if dpi is not None and 0.0 <= dpi <= 1.0:
            return dpi
        return None

    @staticmethod
    def _num(row: dict[str, Any], keys: tuple[str, ...]) -> float | None:
        for k in keys:
            v = row.get(k)
            if v is None:
                continue
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _str(row: dict[str, Any], keys: tuple[str, ...]) -> str:
        for k in keys:
            v = row.get(k)
            if v:
                return str(v)
        return ""
