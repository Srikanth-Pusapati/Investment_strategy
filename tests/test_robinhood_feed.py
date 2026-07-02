"""Tests for the Robinhood-curated discovery screener (movers + crowd).

Pure logic, no network: a fake reader returns canned MCP payloads, and we assert
the screener resolves list ids by name, extracts equity tickers (skipping crypto /
non-instruments), scores movers above the crowd, and merges a name on both lists.

Runnable two ways:
    .venv/bin/python tests/test_robinhood_feed.py
    .venv/bin/pytest tests/
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.screener.robinhood_feed import RobinhoodMoversScreener

_MOVERS_ID = "id-movers"
_POPULAR_ID = "id-popular"


class _FakeReader:
    """Stands in for RobinhoodReader.call_json with canned payloads."""

    def __init__(self, enabled=True, popular=None, items=None):
        self.enabled = enabled
        self._popular = popular
        self._items = items or {}
        self.calls = []

    def call_json(self, tool, arguments=None):
        self.calls.append((tool, arguments))
        if tool == "get_popular_watchlists":
            return self._popular
        if tool == "get_watchlist_items":
            return self._items.get(arguments["list_id"])
        return None


def _screener(reader) -> RobinhoodMoversScreener:
    s = RobinhoodMoversScreener.__new__(RobinhoodMoversScreener)
    s.cfg = None
    s._reader = reader
    return s


def _popular():
    return {"lists": [
        {"id": _MOVERS_ID, "display_name": "Daily movers", "item_count": 2},
        {"id": _POPULAR_ID, "display_name": "100 most popular", "item_count": 3},
        {"id": "id-crypto", "display_name": "Tradable crypto", "item_count": 1},
    ]}


def _items(symbols_with_type):
    return {"items": [{"object_type": t, "symbol": s} for s, t in symbols_with_type]}


def test_surfaces_movers_and_crowd_with_scores():
    reader = _FakeReader(popular=_popular(), items={
        _MOVERS_ID: _items([("NBIS", "instrument"), ("SOC", "instrument")]),
        _POPULAR_ID: _items([("NVDA", "instrument"), ("AAPL", "instrument")]),
    })
    cands = {c.symbol: c for c in _screener(reader).scan()}
    assert set(cands) == {"NBIS", "SOC", "NVDA", "AAPL"}
    assert cands["NBIS"].score == 0.6      # movers scored higher
    assert cands["NVDA"].score == 0.35     # crowd scored lower
    assert cands["NBIS"].sources == ["robinhood"]


def test_name_on_both_lists_keeps_higher_score_and_merges_reason():
    reader = _FakeReader(popular=_popular(), items={
        _MOVERS_ID: _items([("AMD", "instrument")]),
        _POPULAR_ID: _items([("AMD", "instrument")]),
    })
    cands = {c.symbol: c for c in _screener(reader).scan()}
    assert list(cands) == ["AMD"]          # merged, not duplicated
    assert cands["AMD"].score == 0.6       # keeps the stronger movers score
    assert "|" in cands["AMD"].reason      # both reasons kept


def test_skips_crypto_and_non_instruments():
    reader = _FakeReader(popular=_popular(), items={
        _MOVERS_ID: _items([
            ("NBIS", "instrument"),
            ("BTC-USD", "currency_pair"),   # crypto object_type
            ("DOGE-USD", "instrument"),     # instrument but pair-shaped symbol
        ]),
        _POPULAR_ID: _items([]),
    })
    syms = {c.symbol for c in _screener(reader).scan()}
    assert syms == {"NBIS"}


def test_empty_when_lists_unresolved():
    reader = _FakeReader(popular={"lists": []})
    assert _screener(reader).scan() == []


def test_missing_target_list_still_returns_the_other():
    # Only the movers list exists in the curated feed.
    reader = _FakeReader(
        popular={"lists": [{"id": _MOVERS_ID, "display_name": "Daily movers"}]},
        items={_MOVERS_ID: _items([("RGC", "instrument")])},
    )
    syms = {c.symbol for c in _screener(reader).scan()}
    assert syms == {"RGC"}


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {fn.__name__}: {e!r}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
