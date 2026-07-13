"""Options-flow signal — unusual options activity as a smart-money tell.

Large directional options bets (especially short-dated, high-premium sweeps) are
how institutions express conviction with leverage. We approximate "unusual" as
the call/put day-volume imbalance across the near-dated chain, read from
Alpaca's free indicative option snapshots — the same feed the execution path
and the options_chain signal already pull, so no extra vendor or key.

(This originally read Polygon's options snapshot, whose free tier returns 403
on every call — the signal was silently dead from day one. Reads now go through
per-cycle failure accounting so a gated/broken feed surfaces as a WARNING
instead of a quiet stream of zero candidates.)

This is a coarse proxy. A dedicated flow feed (Unusual Whales, CBOE) gives true
sweep/block detection; wire it here behind the same interface to upgrade.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

import requests

from ..config import Config
from ..execution.options import parse_occ
from ..models import Signal, SignalKind
from .base import SignalProvider

log = logging.getLogger("signals")

_SNAPSHOT = "https://data.alpaca.markets/v1beta1/options/snapshots/{underlying}"
_FEED = "indicative"   # the free feed; OPRA real-time needs a paid Alpaca plan
_MAX_DTE = 45          # near-dated chain only — where conviction flow clusters
_PAGE_LIMIT = 1000     # API max per page
_MAX_PAGES = 4         # 4k contracts inside 45 DTE covers any liquid chain


class OptionsFlowProvider(SignalProvider):
    name = "options_flow"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._get = requests.get   # injectable for tests
        self._probes = 0                              # chain reads this cycle
        self._failures: list[tuple[str, str]] = []    # (symbol, reason)

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.alpaca_api_key and self.cfg.alpaca_secret_key)

    def fetch(self, symbols: list[str]) -> list[Signal]:
        self.begin_cycle()
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
                source="alpaca-options-flow",
                data={"call_volume": call_vol, "put_volume": put_vol},
            ))
        self.log_failures("signal")
        return signals

    # -- per-cycle failure accounting ---------------------------------------- #
    # The Polygon-era version swallowed every non-200 as None, so a plan-gated
    # endpoint produced "0 candidates" forever without one log line. Callers
    # (fetch() above and the options_flow screener) bracket their probe loop
    # with begin_cycle()/log_failures() so broken reads surface once per cycle.

    def begin_cycle(self) -> None:
        self._probes = 0
        self._failures.clear()

    def log_failures(self, context: str) -> None:
        if not self._failures:
            return
        sym, why = self._failures[0]
        log.warning(
            "options-flow (%s): %d of %d chain read(s) failed — first: %s (%s). "
            "Flow read degraded this cycle.",
            context, len(self._failures), self._probes, sym, why,
        )

    def _note_failure(self, symbol: str, reason: str) -> None:
        self._failures.append((symbol, reason))
        log.debug("options flow read failed for %s: %s", symbol, reason)

    def _call_put_volume(self, underlying: str) -> tuple[int, int] | None:
        """(call_volume, put_volume) summed over the latest traded session of
        the near-dated chain, or None on a failed read. dailyBar is each
        contract's LAST traded bar — an illiquid contract can carry a days-old
        bar — so only bars stamped with the newest bar-date seen across the
        chain are counted."""
        self._probes += 1
        params: dict = {
            "feed": _FEED,
            "limit": _PAGE_LIMIT,
            "expiration_date_lte": (date.today() + timedelta(days=_MAX_DTE)).isoformat(),
        }
        headers = {
            "APCA-API-KEY-ID": self.cfg.alpaca_api_key,
            "APCA-API-SECRET-KEY": self.cfg.alpaca_secret_key,
        }
        rows: list[tuple[str, str, int]] = []   # (bar_date, right, volume)
        try:
            for _ in range(_MAX_PAGES):
                r = self._get(
                    _SNAPSHOT.format(underlying=underlying),
                    params=params, headers=headers, timeout=20,
                )
                if r.status_code != 200:
                    self._note_failure(
                        underlying, f"HTTP {r.status_code}: {r.text[:120]}")
                    return None
                body = r.json()
                for occ_sym, snap in (body.get("snapshots") or {}).items():
                    bar = (snap or {}).get("dailyBar") or {}
                    vol = int(bar.get("v") or 0)
                    occ = parse_occ(occ_sym)
                    if vol <= 0 or occ is None:
                        continue
                    rows.append((str(bar.get("t") or "")[:10], occ[2], vol))
                token = body.get("next_page_token")
                if not token:
                    break
                params["page_token"] = token
        except Exception as e:
            self._note_failure(underlying, repr(e))
            return None

        if not rows:
            return (0, 0)   # chain exists but nothing traded — a real, thin read
        latest = max(d for d, _, _ in rows)
        call_vol = sum(v for d, right, v in rows if d == latest and right == "C")
        put_vol = sum(v for d, right, v in rows if d == latest and right == "P")
        return call_vol, put_vol
