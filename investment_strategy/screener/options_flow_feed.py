"""Discovery screener: unusual options flow on the most-active names.

signals/options_flow.py checks call/put imbalance for symbols you already track;
this scans a pool of the day's most-active stocks (Alpaca's screener — free, uses
your existing keys) and surfaces the ones showing a strong directional options
lean via Polygon's snapshot. The most-actives list is only the *scan pool* — a
name surfaces for unusual flow, not merely for being active. Needs POLYGON_API_KEY.
"""
from __future__ import annotations

import logging

from alpaca.data.enums import MostActivesBy
from alpaca.data.historical.screener import ScreenerClient
from alpaca.data.requests import MostActivesRequest

from ..config import Config
from ..models import Candidate
from ..signals.options_flow import OptionsFlowProvider
from .base import Screener

log = logging.getLogger("screener")

# Only surface names whose call/put imbalance is genuinely lopsided.
_MIN_TOTAL_VOL = 100
_MIN_ABS_IMBALANCE = 0.30


class OptionsFlowScreener(Screener):
    name = "options_flow"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        # Reuse the per-symbol call/put-volume probe; the screener just decides
        # WHICH symbols to probe (the most-active pool) and which to surface.
        self._flow = OptionsFlowProvider(cfg)
        self._screener = ScreenerClient(cfg.alpaca_api_key, cfg.alpaca_secret_key)

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.polygon_api_key)

    def scan(self) -> list[Candidate]:
        pool = self._most_actives()
        candidates: list[Candidate] = []
        for symbol in pool:
            agg = self._flow._call_put_volume(symbol)
            if agg is None:
                continue
            call_vol, put_vol = agg
            total = call_vol + put_vol
            if total < _MIN_TOTAL_VOL:
                continue
            imbalance = round((call_vol - put_vol) / total, 3)
            if abs(imbalance) < _MIN_ABS_IMBALANCE:
                continue
            lean = "bullish call" if imbalance > 0 else "bearish put"
            candidates.append(Candidate(
                symbol=symbol,
                sources=[self.name],
                reason=f"Options flow: {call_vol:,} call / {put_vol:,} put vol "
                       f"({lean} imbalance {imbalance:+.2f}) on a most-active name.",
                score=imbalance,
            ))
        return candidates

    def _most_actives(self) -> list[str]:
        req = MostActivesRequest(
            top=self.cfg.screener.options_flow_scan_limit, by=MostActivesBy.VOLUME
        )
        res = self._screener.get_most_actives(req)
        actives = getattr(res, "most_actives", None) or []
        return [a.symbol for a in actives if getattr(a, "symbol", None)]
