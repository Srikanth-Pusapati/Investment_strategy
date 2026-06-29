"""Federal government contract-award signal.

Source: Quiver Quantitative `live/govcontractsall` (individual recent awards, with
an award date + dollar amount), read through the SHARED, per-cycle-cached
QuiverClient. A fresh government contract is a HARD future-revenue catalyst for the
awarded company — unlike sentiment or flow, the money is contractually committed —
so it is a real, if slow (days-to-weeks lag), fundamental tailwind. Disabled
without a key.

Direction: BULLISH-ONLY. An award is good news; the ABSENCE of an award is not bad
news, so this provider never emits a negative score — it only adds conviction to
names winning federal business, never subtracts. The lean scales with how many
recent awards a name won (cluster saturation, same discipline as congress) plus a
small bump for large total dollars, and is capped low (±0.4) because contract size
relative to a company's revenue is unknown here — a $50k award to a megacap is
noise, so we never let it move size like a timely dark-pool print.

SCHEMA: verified against live live/govcontractsall on 2026-06-28 — a row is
{"Ticker", "Date", "action_date", "Amount", "Agency", "Description"}. Amount may
arrive as a string or a number; Date may be absent (action_date is the fallback).
Parsing stays defensive and a row with no usable date or amount is skipped.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any

from ..config import Config
from ..models import Signal, SignalKind
from .base import SignalProvider
from .quiver_client import QuiverClient

_LOOKBACK_DAYS = 120          # contracts trickle in; a slow catalyst, wide window
_MAX_LEAN = 0.4               # cap: size relative to revenue is unknown -> stay modest
# Award count drives a SATURATING lean (1 - e^-n/scale) so it discriminates across
# the realistic 1..50+ range instead of pinning at the cap after a few awards —
# most active names win many small awards, so a linear per-award bump is useless.
_COUNT_SCALE = 6.0            # ~63% of the count budget by 6 awards, asymptotes after
_DOLLAR_BUMP = 0.15           # top-end lean reserved for large total dollars
_COUNT_CAP = _MAX_LEAN - _DOLLAR_BUMP  # count alone tops out here; dollars add the rest
_BIG_DOLLARS = 50_000_000.0   # total recent award $ that earns the full dollar bump

_TICKER_KEYS = ("Ticker", "ticker", "Symbol")
_AMOUNT_KEYS = ("Amount", "amount", "Value")
_DATE_KEYS = ("Date", "action_date", "date")


class GovContractsProvider(SignalProvider):
    name = "govcontracts"

    def __init__(self, cfg: Config, quiver: QuiverClient | None = None):
        self.cfg = cfg
        self.quiver = quiver or QuiverClient(cfg.quiver_api_key)

    @property
    def enabled(self) -> bool:
        return self.quiver.enabled

    def fetch(self, symbols: list[str]) -> list[Signal]:
        wanted = {s.upper() for s in symbols}
        cutoff = datetime.now(timezone.utc) - timedelta(days=_LOOKBACK_DAYS)

        # One shared pull of the cross-ticker feed; tally recent awards per symbol.
        counts: dict[str, int] = {s: 0 for s in wanted}
        dollars: dict[str, float] = {s: 0.0 for s in wanted}
        for row in self.quiver.live("govcontractsall"):
            sym = self._str(row, _TICKER_KEYS).upper()
            if sym not in wanted or not self._recent(self._str(row, _DATE_KEYS), cutoff):
                continue
            amt = self._num(row, _AMOUNT_KEYS)
            if amt is None or amt <= 0:
                continue  # no usable dollar figure -> skip, don't guess
            counts[sym] += 1
            dollars[sym] += amt

        signals: list[Signal] = []
        for sym in wanted:
            n = counts[sym]
            if n == 0:
                continue
            total = dollars[sym]
            # Bullish-only: a saturating count lean (discriminates 1..50+ awards)
            # plus a top-end bump for large total dollars. Never negative.
            count_lean = _COUNT_CAP * (1.0 - math.exp(-n / _COUNT_SCALE))
            dollar_lean = _DOLLAR_BUMP * min(1.0, total / _BIG_DOLLARS)
            score = round(min(_MAX_LEAN, count_lean + dollar_lean), 3)
            signals.append(Signal(
                kind=SignalKind.GOVCONTRACTS,
                symbol=sym,
                summary=f"{n} federal contract award(s) totaling ${total/1e6:.1f}M "
                        f"in last {_LOOKBACK_DAYS}d (revenue catalyst; days lag).",
                score=score,
                source="quiver",
                data={"awards": n, "total_usd": round(total, 2)},
            ))
        return signals

    # -- defensive field parsing ------------------------------------------- #
    @staticmethod
    def _recent(date_str: str, cutoff: datetime) -> bool:
        if not date_str:
            return False
        try:
            d = datetime.fromisoformat(date_str[:10]).replace(tzinfo=timezone.utc)
        except ValueError:
            return False
        return d >= cutoff

    @staticmethod
    def _num(row: dict[str, Any], keys: tuple[str, ...]) -> float | None:
        for k in keys:
            v = row.get(k)
            if v is None:
                continue
            try:
                return float(str(v).replace(",", "").replace("$", ""))
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
