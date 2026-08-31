"""Options-flow signal — unusual options activity as a smart-money tell.

Large directional options bets (especially short-dated, high-premium sweeps) are
how institutions express conviction with leverage. Two backends behind one
interface, picked by key availability:

- POLYGON (preferred; needs POLYGON_API_KEY on the Options Starter plan,
  unlocked 2026-07-27): v3 options snapshot carries per-contract day volume AND
  open interest, so besides the call/put imbalance we get the true unusualness
  tell — volume running ahead of OI means NEW positioning today, volume far
  below OI is churn in existing positions. 15-min delayed, irrelevant at our
  hourly cadence.
- ALPACA fallback (free indicative snapshots): call/put dailyBar-volume
  imbalance only; no OI, so no freshness read. This is the same coarse proxy
  that carried the signal while Polygon's free tier 403'd every call.

Reads go through per-cycle failure accounting so a gated/broken feed surfaces
as a WARNING instead of a quiet stream of zero candidates (the lesson from the
original Polygon-free-tier era, when this signal was silently dead).
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

_POLY_SNAPSHOT = "https://api.polygon.io/v3/snapshot/options/{underlying}"
_POLY_PAGE_LIMIT = 250   # Polygon's max per page
_POLY_MAX_PAGES = 8      # 2k contracts inside 45 DTE; Starter has unlimited calls
# Vol/OI conviction shading: volume above OI = new positioning (boost), volume
# far below OI = churn in stale inventory (damp). Deliberately mild — the
# imbalance stays the signal, OI only shades conviction.
_FRESH_VOL_OI = 1.0
_STALE_VOL_OI = 0.25
_FRESH_BOOST = 1.2
_STALE_DAMP = 0.85


class OptionsFlowProvider(SignalProvider):
    name = "options_flow"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._get = requests.get   # injectable for tests
        self._probes = 0                              # chain reads this cycle
        self._failures: list[tuple[str, str]] = []    # (symbol, reason)
        self._detail: dict[str, dict] = {}            # per-symbol OI extras (Polygon)

    @property
    def _polygon_key(self) -> str:
        return getattr(self.cfg, "polygon_api_key", "") or ""

    @property
    def enabled(self) -> bool:
        return bool(self._polygon_key) or bool(
            self.cfg.alpaca_api_key and self.cfg.alpaca_secret_key)

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
            detail = self._detail.get(symbol)
            oi_txt = ""
            if detail and detail.get("vol_oi") is not None:
                vol_oi = detail["vol_oi"]
                if vol_oi >= _FRESH_VOL_OI:
                    score = round(max(-1.0, min(1.0, score * _FRESH_BOOST)), 3)
                    oi_txt = f", vol {vol_oi:.1f}x OI (fresh positioning)"
                elif vol_oi < _STALE_VOL_OI:
                    score = round(score * _STALE_DAMP, 3)
                    oi_txt = f", vol {vol_oi:.2f}x OI (mostly existing inventory)"
                else:
                    oi_txt = f", vol {vol_oi:.2f}x OI"
            signals.append(Signal(
                kind=SignalKind.OPTIONS_FLOW,  # run-6: its own kind (was NEWS)
                symbol=symbol,
                summary=f"Options flow: {call_vol:,} call vol / {put_vol:,} put vol "
                        f"(C/P imbalance {score:+.2f}{oi_txt}).",
                score=score,
                source="polygon-options-flow" if detail is not None
                       else "alpaca-options-flow",
                data={"call_volume": call_vol, "put_volume": put_vol,
                      **({"vol_oi": detail["vol_oi"]} if detail else {})},
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
        self._detail.clear()

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
        """(call_volume, put_volume) across the near-dated chain, or None on a
        failed read. Dispatches to the richest backend the keys allow; both
        share the same failure accounting. The Polygon path also stashes OI
        detail in self._detail for conviction shading in fetch()."""
        if self._polygon_key:
            return self._polygon_volume(underlying)
        return self._alpaca_volume(underlying)

    def _polygon_volume(self, underlying: str) -> tuple[int, int] | None:
        """Sum day volume and open interest per side from Polygon's v3 options
        snapshot (15-min delayed on Starter). Vol/OI across the whole near-dated
        chain lands in self._detail — volume outrunning OI is the unusualness
        tell the Alpaca proxy can't see."""
        self._probes += 1
        url = _POLY_SNAPSHOT.format(underlying=underlying)
        params: dict = {
            "limit": _POLY_PAGE_LIMIT,
            "expiration_date.lte":
                (date.today() + timedelta(days=_MAX_DTE)).isoformat(),
        }
        headers = {"Authorization": f"Bearer {self._polygon_key}"}
        vol = {"call": 0, "put": 0}
        oi = {"call": 0, "put": 0}
        try:
            for _ in range(_POLY_MAX_PAGES):
                r = self._get(url, params=params, headers=headers, timeout=20)
                if r.status_code != 200:
                    self._note_failure(
                        underlying, f"HTTP {r.status_code}: {r.text[:120]}")
                    return None
                body = r.json()
                for row in body.get("results") or []:
                    side = ((row.get("details") or {}).get("contract_type")
                            or "").lower()
                    if side not in vol:
                        continue
                    vol[side] += int((row.get("day") or {}).get("volume") or 0)
                    oi[side] += int(row.get("open_interest") or 0)
                next_url = body.get("next_url")
                if not next_url:
                    break
                url, params = next_url, {}   # cursor URL carries the query
        except Exception as e:
            self._note_failure(underlying, repr(e))
            return None

        total_vol, total_oi = sum(vol.values()), sum(oi.values())
        self._detail[underlying] = {
            "call_oi": oi["call"], "put_oi": oi["put"],
            "vol_oi": round(total_vol / total_oi, 3) if total_oi > 0 else None,
        }
        return vol["call"], vol["put"]

    def _alpaca_volume(self, underlying: str) -> tuple[int, int] | None:
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
