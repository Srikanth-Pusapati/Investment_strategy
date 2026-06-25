"""Options-flow signal — unusual options activity as a smart-money tell.

Large directional options bets (especially short-dated, high-premium sweeps) are
how institutions express conviction with leverage. We approximate "unusual" as
call/put volume imbalance relative to open interest from Polygon's options
snapshot (needs POLYGON_API_KEY). Disabled without a key.

This is a coarse proxy. A dedicated flow feed (Unusual Whales, CBOE) gives true
sweep/block detection; wire it here behind the same interface to upgrade.
"""
from __future__ import annotations

import logging

import requests

from ..config import Config
from ..models import Signal, SignalKind
from .base import SignalProvider

log = logging.getLogger("signals")

_SNAPSHOT = "https://api.polygon.io/v3/snapshot/options/{underlying}"


class OptionsFlowProvider(SignalProvider):
    name = "options_flow"

    def __init__(self, cfg: Config):
        self.cfg = cfg

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.polygon_api_key)

    def fetch(self, symbols: list[str]) -> list[Signal]:
        signals: list[Signal] = []
        for symbol in symbols:
            agg = self._call_put_volume(symbol)
            if agg is None:
                continue
            call_vol, put_vol = agg
            total = call_vol + put_vol
            if total < 100:  # too thin to be meaningful
                continue
            # Put/call imbalance -> directional lean. Heavy call volume = bullish.
            score = round((call_vol - put_vol) / total, 3)
            signals.append(Signal(
                kind=SignalKind.NEWS,  # treated as a near-term catalyst/flow signal
                symbol=symbol,
                summary=f"Options flow: {call_vol:,} call vol / {put_vol:,} put vol "
                        f"(C/P imbalance {score:+.2f}).",
                score=score,
                source="polygon-options",
                data={"call_volume": call_vol, "put_volume": put_vol},
            ))
        return signals

    def _call_put_volume(self, underlying: str) -> tuple[int, int] | None:
        try:
            r = requests.get(
                _SNAPSHOT.format(underlying=underlying),
                params={"apiKey": self.cfg.polygon_api_key, "limit": 250},
                timeout=20,
            )
            if r.status_code != 200:
                return None
            results = r.json().get("results", [])
        except Exception as e:
            log.debug("options flow fetch failed for %s: %s", underlying, e)
            return None

        call_vol = put_vol = 0
        for c in results:
            vol = (c.get("day") or {}).get("volume") or 0
            ctype = (c.get("details") or {}).get("contract_type")
            if ctype == "call":
                call_vol += vol
            elif ctype == "put":
                put_vol += vol
        return (call_vol, put_vol)
