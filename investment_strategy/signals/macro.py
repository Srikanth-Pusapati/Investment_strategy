"""Macro / government data signals (market-wide, not per-symbol).

Source: FRED (FRED_API_KEY). Pulls a few headline indicators that set the
risk backdrop the decision engine reasons against. Disabled without a key.
"""
from __future__ import annotations

import requests

from ..config import Config
from ..models import Signal, SignalKind
from .base import SignalProvider

_FRED_URL = "https://api.stlouisfed.org/fred/series/observations"

# Series to track. (id, human label, "higher_is_risk_on")
_SERIES = [
    ("DFF", "Fed funds rate", False),       # higher rate = risk-off
    ("UNRATE", "Unemployment", False),       # rising unemployment = risk-off
    ("T10Y2Y", "10y-2y spread", True),       # inversion (<0) = recession signal
]


class MacroProvider(SignalProvider):
    name = "macro"
    market_wide = True

    def __init__(self, cfg: Config):
        self.cfg = cfg

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.fred_api_key)

    def fetch(self, symbols: list[str]) -> list[Signal]:
        signals: list[Signal] = []
        for series_id, label, risk_on_when_high in _SERIES:
            latest = self._latest(series_id)
            if latest is None:
                continue
            summary = f"{label}: {latest:.2f}"
            # Crude directional read; the model interprets the actual number.
            score = None
            if series_id == "T10Y2Y":
                score = -0.5 if latest < 0 else 0.2  # inverted curve is bearish
                summary += " (inverted — recession signal)" if latest < 0 else ""
            signals.append(Signal(
                kind=SignalKind.MACRO,
                symbol=None,
                summary=summary,
                score=score,
                source="fred",
                data={"series": series_id, "value": latest},
            ))
        return signals

    def _latest(self, series_id: str) -> float | None:
        r = requests.get(_FRED_URL, params={
            "series_id": series_id,
            "api_key": self.cfg.fred_api_key,
            "file_type": "json",
            "sort_order": "desc",
            "limit": 1,
        }, timeout=15)
        if r.status_code != 200:
            return None
        obs = r.json().get("observations", [])
        if not obs:
            return None
        try:
            return float(obs[0]["value"])
        except (ValueError, KeyError):
            return None
