"""Discovery screener: Robinhood-curated lists (momentum + retail crowd).

The hobbyist Quiver tier and Polygon's free tier leave discovery running on the
slow congress feed alone. Robinhood (Gold) exposes curated, server-side lists via
its read-only Agentic MCP — notably "Daily movers" (today's biggest movers, a free
price-momentum tell) and "100 most popular" (retail crowd positioning). This
screener surfaces those names so the scanner isn't blind between congress filings.

It is NOT market-wide options flow or insider data — Robinhood is a broker, not a
flow/insider vendor — but movers/crowd are genuine, timely discovery signals.

Resolves list ids by display name at scan time (via get_popular_watchlists) rather
than hardcoding UUIDs, so it survives Robinhood re-issuing a list id. Read-only:
every call goes through RobinhoodReader.call_json, which never touches a trade tool.
Disabled (a no-op) unless ROBINHOOD_ENABLED=on with a URL + token.
"""
from __future__ import annotations

import logging

from ..config import Config
from ..models import Candidate, is_valid_ticker
from ..portfolio import RobinhoodReader
from .base import Screener

log = logging.getLogger("screener")


class RobinhoodMoversScreener(Screener):
    name = "robinhood"

    # (curated list display name, discovery score, why). "Daily movers" is the
    # momentum tell and scores higher; the crowd list is a weaker positioning read.
    # Scores stay below the strongest smart-money signals (e.g. clustered congress
    # buys at 1.0) — a name still has to earn conviction in the signal/decision layer.
    _LISTS: tuple[tuple[str, float, str], ...] = (
        ("Daily movers", 0.6, "Robinhood Daily Movers (today's biggest movers — momentum)"),
        ("100 most popular", 0.35, "Robinhood 100 Most Popular (retail crowd positioning)"),
    )

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._reader = RobinhoodReader(cfg)

    @property
    def enabled(self) -> bool:
        return self._reader.enabled

    def scan(self) -> list[Candidate]:
        lists = self._popular_list_ids()
        if not lists:
            log.info("Robinhood: no curated lists resolved (feed empty/unreachable).")
            return []

        # A name on BOTH lists keeps the stronger (movers) score and both reasons.
        merged: dict[str, Candidate] = {}
        for display_name, score, why in self._LISTS:
            list_id = lists.get(display_name.lower())
            if not list_id:
                continue
            symbols = self._list_symbols(list_id)
            log.info("Robinhood '%s' -> %d name(s).", display_name, len(symbols))
            for sym in symbols:
                existing = merged.get(sym)
                if existing is None:
                    merged[sym] = Candidate(
                        symbol=sym, sources=[self.name], reason=why, score=score,
                    )
                else:
                    existing.score = max(existing.score, score)
                    existing.reason = f"{existing.reason} | {why}"
        return list(merged.values())

    # -- MCP reads (via the read-only RobinhoodReader) --------------------- #
    def _popular_list_ids(self) -> dict[str, str]:
        """{lowercased display_name: list_id} for Robinhood's curated lists."""
        payload = self._reader.call_json("get_popular_watchlists")
        rows = payload.get("lists", []) if isinstance(payload, dict) else (payload or [])
        out: dict[str, str] = {}
        for row in rows:
            name = str(row.get("display_name", "")).strip().lower()
            lid = row.get("id")
            if name and lid:
                out[name] = lid
        return out

    def _list_symbols(self, list_id: str) -> list[str]:
        """Equity tickers in a curated list (skips crypto pairs / non-instruments)."""
        payload = self._reader.call_json("get_watchlist_items", {"list_id": list_id})
        items = payload.get("items", []) if isinstance(payload, dict) else (payload or [])
        out: list[str] = []
        for it in items:
            if it.get("object_type") != "instrument":
                continue  # crypto pairs, futures, indexes — not equity-tradable here
            sym = str(it.get("symbol", "")).strip().upper()
            if is_valid_ticker(sym) and "-" not in sym:   # "-" == crypto pair (BTC-USD)
                out.append(sym)
        return out
