"""Tests for the off-exchange signal and WallStreetBets screener.

Pure logic, no network: a fake QuiverClient returns canned live-feed rows so we
assert directional scoring, defensive field parsing (alternate key names),
graceful skipping of unusable rows, and self-calibrated WSB ranking — all against
MOCKED responses (the market's closed and the live schema isn't pinned here).

Runnable two ways:
    .venv/bin/python tests/test_quiver_datasets.py     # standalone, no pytest
    .venv/bin/pytest tests/                              # if pytest is installed
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.screener.wallstreetbets_feed import WallStreetBetsScreener
from investment_strategy.signals.offexchange import OffExchangeProvider


class _FakeQuiver:
    """Shared-client stand-in: serves canned rows per dataset, tracks pull count."""
    enabled = True

    def __init__(self, feeds):
        self._feeds = feeds
        self.calls = 0

    def live(self, dataset):
        self.calls += 1
        return self._feeds.get(dataset, [])


def _cfg():
    return SimpleNamespace(quiver_api_key="k")


# --------------------------------------------------------------------------- #
# Off-exchange signal
# --------------------------------------------------------------------------- #
def test_offexchange_high_short_is_bearish():
    feed = [{"Ticker": "AAPL", "Date": "2099-01-05", "Sht_Vol": 70, "Tot_Vol": 100}]
    prov = OffExchangeProvider(_cfg(), _FakeQuiver({"offexchange": feed}))
    sig = prov.fetch(["AAPL"])[0]
    assert sig.score < 0                       # 70% short -> bearish lean
    assert sig.data["short_ratio"] == 0.7


def test_offexchange_low_short_is_bullish():
    feed = [{"Ticker": "MSFT", "Date": "2099-01-05", "Sht_Vol": 30, "Tot_Vol": 100}]
    prov = OffExchangeProvider(_cfg(), _FakeQuiver({"offexchange": feed}))
    assert prov.fetch(["MSFT"])[0].score > 0


def test_offexchange_lean_is_capped():
    feed = [{"Ticker": "X", "Date": "2099-01-05", "Sht_Vol": 100, "Tot_Vol": 100}]
    prov = OffExchangeProvider(_cfg(), _FakeQuiver({"offexchange": feed}))
    assert prov.fetch(["X"])[0].score == -0.6   # clamped at -_MAX_LEAN


def test_offexchange_uses_latest_date_per_symbol():
    feed = [
        {"Ticker": "AAPL", "Date": "2099-01-01", "Sht_Vol": 20, "Tot_Vol": 100},
        {"Ticker": "AAPL", "Date": "2099-01-09", "Sht_Vol": 80, "Tot_Vol": 100},
    ]
    prov = OffExchangeProvider(_cfg(), _FakeQuiver({"offexchange": feed}))
    sig = prov.fetch(["AAPL"])[0]
    assert sig.data["short_ratio"] == 0.8       # newest row wins


def test_offexchange_dpi_fallback_when_no_volumes():
    feed = [{"Ticker": "NVDA", "Date": "2099-01-05", "DPI": 0.65}]
    prov = OffExchangeProvider(_cfg(), _FakeQuiver({"offexchange": feed}))
    sig = prov.fetch(["NVDA"])[0]
    assert sig.score < 0 and sig.data["dpi"] == 0.65


def test_offexchange_skips_unusable_row():
    feed = [{"Ticker": "GME", "Date": "2099-01-05", "Sht_Vol": 50, "Tot_Vol": 0}]
    prov = OffExchangeProvider(_cfg(), _FakeQuiver({"offexchange": feed}))
    assert prov.fetch(["GME"]) == []            # zero total -> no guess, no signal


def test_offexchange_alternate_field_names():
    feed = [{"ticker": "T", "date": "2099-01-05", "OTC_Short": 60, "OTC_Total": 100}]
    prov = OffExchangeProvider(_cfg(), _FakeQuiver({"offexchange": feed}))
    assert abs(prov.fetch(["T"])[0].data["short_ratio"] - 0.6) < 1e-9


def test_offexchange_ignores_unwanted_symbols():
    feed = [{"Ticker": "ZZZ", "Date": "2099-01-05", "Sht_Vol": 70, "Tot_Vol": 100}]
    prov = OffExchangeProvider(_cfg(), _FakeQuiver({"offexchange": feed}))
    assert prov.fetch(["AAPL"]) == []


# --------------------------------------------------------------------------- #
# WallStreetBets screener
# --------------------------------------------------------------------------- #
def test_wsb_ranks_by_mentions_self_calibrated():
    feed = [
        {"Ticker": "GME", "Date": "2099-01-05", "Mentions": 500},
        {"Ticker": "AMC", "Date": "2099-01-05", "Mentions": 100},
    ]
    scr = WallStreetBetsScreener(_cfg(), _FakeQuiver({"wallstreetbets": feed}))
    cands = {c.symbol: c for c in scr.scan()}
    assert cands["GME"].score == 0.6            # busiest -> full lean
    assert abs(cands["AMC"].score - 0.6 * (100 / 500)) < 1e-9


def test_wsb_negative_sentiment_flips_sign():
    feed = [{"Ticker": "BBBY", "Date": "2099-01-05", "Mentions": 200, "Sentiment": -0.5}]
    scr = WallStreetBetsScreener(_cfg(), _FakeQuiver({"wallstreetbets": feed}))
    assert scr.scan()[0].score < 0              # bearish chatter -> bearish lean


def test_wsb_aggregates_multiple_rows_per_symbol():
    feed = [
        {"Ticker": "GME", "Date": "2099-01-04", "Mentions": 150},
        {"Ticker": "GME", "Date": "2099-01-05", "Mentions": 150},
    ]
    scr = WallStreetBetsScreener(_cfg(), _FakeQuiver({"wallstreetbets": feed}))
    cand = scr.scan()[0]
    assert "300 mentions" in cand.reason        # summed across rows


def test_wsb_empty_feed_returns_nothing():
    scr = WallStreetBetsScreener(_cfg(), _FakeQuiver({"wallstreetbets": []}))
    assert scr.scan() == []


def test_wsb_skips_zero_mention_rows():
    feed = [{"Ticker": "X", "Date": "2099-01-05", "Mentions": 0}]
    scr = WallStreetBetsScreener(_cfg(), _FakeQuiver({"wallstreetbets": feed}))
    assert scr.scan() == []


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
