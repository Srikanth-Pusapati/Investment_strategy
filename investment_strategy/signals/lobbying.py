"""Corporate federal-lobbying signal.

Source: Quiver Quantitative `live/lobbying` (LDA lobbying disclosures), read
through the SHARED, per-cycle-cached QuiverClient. Companies spending heavily on
lobbying tend to be defending or pursuing concrete policy/contract outcomes, and
the Issue text is genuinely useful context for the decision prompt ("lobbying on
45Q tax credits" says a lot about a name's policy exposure). Disabled without a
key.

LAG DISCIPLINE (the 2026-07-27 congress lesson, applied from birth): LDA reports
are filed quarterly and cover activity up to ~3 months old, so this kind carries
a heavy lag discount in the composite (KIND_LAG_DAYS ~45d -> weight ~0.11) and is
deliberately NOT a screener/discovery source — it may corroborate a name already
on the slate but can never surface one, and can never lead a thesis.

Direction: BULLISH-ONLY and capped low (±0.25). Heavy lobbying correlates with
outperformance in Quiver's own long-horizon strategies, but spend is ambiguous at
the single-name level (companies also lobby when threatened), so this only ever
adds a modest conviction nudge, never subtracts and never moves size.

SCHEMA: verified against live live/lobbying on 2026-07-27 — a row is
{"Date", "Amount" (string), "Client", "Issue" (newline-separated list),
"Specific_Issue", "Registrant", "Ticker"}. Amount may be "0.0" (filing with
undisclosed spend); such rows still count as filings but add no dollars.
Parsing stays defensive and a row with no usable date is skipped.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ..config import Config
from ..models import Signal, SignalKind
from .base import SignalProvider
from .quiver_client import QuiverClient

_LOOKBACK_DAYS = 90           # one filing cycle of headroom past the quarterly dump
_MAX_LEAN = 0.25              # ambiguous at single-name level -> smallest cap in the stack
_BIG_DOLLARS = 2_000_000.0    # recent-quarter spend that earns the full lean

_TICKER_KEYS = ("Ticker", "ticker", "Symbol")
_AMOUNT_KEYS = ("Amount", "amount")
_DATE_KEYS = ("Date", "date")
_ISSUE_KEYS = ("Issue", "issue")


class LobbyingProvider(SignalProvider):
    name = "lobbying"

    def __init__(self, cfg: Config, quiver: QuiverClient | None = None):
        self.cfg = cfg
        self.quiver = quiver or QuiverClient(cfg.quiver_api_key)

    @property
    def enabled(self) -> bool:
        return self.quiver.enabled

    def fetch(self, symbols: list[str]) -> list[Signal]:
        wanted = {s.upper() for s in symbols}
        cutoff = datetime.now(timezone.utc) - timedelta(days=_LOOKBACK_DAYS)

        filings: dict[str, int] = {s: 0 for s in wanted}
        dollars: dict[str, float] = {s: 0.0 for s in wanted}
        issues: dict[str, str] = {}       # first issue line of the largest filing
        biggest: dict[str, float] = {}
        for row in self.quiver.live("lobbying"):
            sym = self._str(row, _TICKER_KEYS).upper()
            if sym not in wanted or not self._recent(self._str(row, _DATE_KEYS), cutoff):
                continue
            filings[sym] += 1
            amt = self._num(row, _AMOUNT_KEYS) or 0.0
            if amt > 0:
                dollars[sym] += amt
            issue = self._str(row, _ISSUE_KEYS).split("\n")[0].strip()
            if issue and amt >= biggest.get(sym, -1.0):
                biggest[sym] = amt
                issues[sym] = issue

        signals: list[Signal] = []
        for sym in wanted:
            n = filings[sym]
            if n == 0:
                continue
            total = dollars[sym]
            # Bullish-only nudge scaling with disclosed dollars; a filing with no
            # disclosed spend still earns a floor tick so the ISSUE context flows
            # to the prompt even when Amount is withheld.
            score = round(max(0.05, _MAX_LEAN * min(1.0, total / _BIG_DOLLARS)), 3)
            issue_txt = f"; top issue: {issues[sym]}" if sym in issues else ""
            signals.append(Signal(
                kind=SignalKind.LOBBYING,
                symbol=sym,
                summary=f"{n} lobbying filing(s), ${total/1e6:.2f}M disclosed in "
                        f"last {_LOOKBACK_DAYS}d{issue_txt} "
                        "(~45d activity lag; context only, never a thesis).",
                score=score,
                source="quiver",
                data={"filings": n, "total_usd": round(total, 2)},
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
