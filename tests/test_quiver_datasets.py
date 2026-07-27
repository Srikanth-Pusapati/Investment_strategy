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

from datetime import date, timedelta

from investment_strategy.screener.wallstreetbets_feed import WallStreetBetsScreener
from investment_strategy.signals.govcontracts import GovContractsProvider
from investment_strategy.signals.lobbying import LobbyingProvider
from investment_strategy.signals.offexchange import OffExchangeProvider


def _recent(days_ago=10):
    return (date.today() - timedelta(days=days_ago)).isoformat()


def _old(days_ago=400):
    return (date.today() - timedelta(days=days_ago)).isoformat()


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


def test_offexchange_real_live_schema_row():
    # Exact row shape from live/offexchange (verified 2026-06-26).
    feed = [{"Ticker": "ADTX", "Date": "2026-06-22",
             "OTC_Short": 439923174, "OTC_Total": 859031869, "DPI": 0.51211508}]
    prov = OffExchangeProvider(_cfg(), _FakeQuiver({"offexchange": feed}))
    sig = prov.fetch(["ADTX"])[0]
    assert abs(sig.data["short_ratio"] - 0.512) < 1e-3   # ~51% short -> mild bearish
    assert sig.score < 0 and sig.data["dpi"] == 0.51211508


def test_offexchange_ignores_unwanted_symbols():
    feed = [{"Ticker": "ZZZ", "Date": "2099-01-05", "Sht_Vol": 70, "Tot_Vol": 100}]
    prov = OffExchangeProvider(_cfg(), _FakeQuiver({"offexchange": feed}))
    assert prov.fetch(["AAPL"]) == []


# --------------------------------------------------------------------------- #
# Government contracts signal (bullish-only catalyst)
# --------------------------------------------------------------------------- #
def test_govcontracts_recent_award_is_bullish():
    feed = [{"Ticker": "LMT", "Date": _recent(), "Amount": "5000000"}]
    prov = GovContractsProvider(_cfg(), _FakeQuiver({"govcontractsall": feed}))
    sig = prov.fetch(["LMT"])[0]
    assert sig.score > 0                        # award -> bullish lean
    assert sig.data["awards"] == 1
    assert sig.data["total_usd"] == 5_000_000.0


def test_govcontracts_never_negative():
    # Even with many awards the score is bullish-only and capped.
    feed = [{"Ticker": "RTX", "Date": _recent(), "Amount": 1_000} for _ in range(20)]
    prov = GovContractsProvider(_cfg(), _FakeQuiver({"govcontractsall": feed}))
    sig = prov.fetch(["RTX"])[0]
    assert 0 < sig.score <= 0.4                 # capped at _MAX_LEAN, never < 0


def test_govcontracts_more_awards_score_higher():
    one = [{"Ticker": "A", "Date": _recent(), "Amount": 1000}]
    three = [{"Ticker": "A", "Date": _recent(), "Amount": 1000} for _ in range(3)]
    p1 = GovContractsProvider(_cfg(), _FakeQuiver({"govcontractsall": one}))
    p3 = GovContractsProvider(_cfg(), _FakeQuiver({"govcontractsall": three}))
    assert p3.fetch(["A"])[0].score > p1.fetch(["A"])[0].score


def test_govcontracts_ignores_old_awards():
    feed = [{"Ticker": "GD", "Date": _old(), "Amount": 9_000_000}]
    prov = GovContractsProvider(_cfg(), _FakeQuiver({"govcontractsall": feed}))
    assert prov.fetch(["GD"]) == []             # outside 120d lookback


def test_govcontracts_skips_zero_or_missing_amount():
    feed = [
        {"Ticker": "X", "Date": _recent(), "Amount": 0},
        {"Ticker": "X", "Date": _recent()},     # no amount at all
    ]
    prov = GovContractsProvider(_cfg(), _FakeQuiver({"govcontractsall": feed}))
    assert prov.fetch(["X"]) == []


def test_govcontracts_parses_messy_amount_and_action_date():
    # Real feed: Amount may be "$1,234,567.89"; Date may be absent (action_date).
    feed = [{"Ticker": "BA", "action_date": _recent(), "Amount": "$1,234,567.89"}]
    prov = GovContractsProvider(_cfg(), _FakeQuiver({"govcontractsall": feed}))
    sig = prov.fetch(["BA"])[0]
    assert abs(sig.data["total_usd"] - 1_234_567.89) < 1e-6


def test_govcontracts_dollar_bump_lifts_large_awards():
    small = [{"Ticker": "A", "Date": _recent(), "Amount": 1_000}]
    big = [{"Ticker": "A", "Date": _recent(), "Amount": 100_000_000}]
    p_small = GovContractsProvider(_cfg(), _FakeQuiver({"govcontractsall": small}))
    p_big = GovContractsProvider(_cfg(), _FakeQuiver({"govcontractsall": big}))
    # Same award count, but the large-dollar bump makes big score higher.
    assert p_big.fetch(["A"])[0].score > p_small.fetch(["A"])[0].score


def test_govcontracts_real_live_schema_row():
    # Exact key set from live/govcontractsall (verified 2026-06-28); extra fields
    # (Agency, Description, action_date) must be ignored, Amount read as a number.
    feed = [{"Ticker": "ACN", "Date": _recent(), "action_date": _recent(),
             "Amount": 48700000.0, "Agency": "Department of Defense",
             "Description": "PROFESSIONAL SERVICES"}]
    prov = GovContractsProvider(_cfg(), _FakeQuiver({"govcontractsall": feed}))
    sig = prov.fetch(["ACN"])[0]
    assert sig.score > 0 and sig.data["total_usd"] == 48_700_000.0


def test_govcontracts_ignores_unwanted_symbols():
    feed = [{"Ticker": "ZZZ", "Date": _recent(), "Amount": 5_000_000}]
    prov = GovContractsProvider(_cfg(), _FakeQuiver({"govcontractsall": feed}))
    assert prov.fetch(["LMT"]) == []


# --------------------------------------------------------------------------- #
# Lobbying signal (bullish-only, context; heavy lag discount lives in history.py)
# --------------------------------------------------------------------------- #
def test_lobbying_real_live_schema_row():
    # Exact key set from live/lobbying (verified 2026-07-27); Amount is a string,
    # Issue is a newline-separated list whose FIRST line becomes prompt context.
    feed = [{"Date": _recent(), "Amount": "50000.0", "Client": "OCCIDENTAL PETROLEUM",
             "Issue": "Taxation/Internal Revenue Code \nEnergy/Nuclear",
             "Specific_Issue": "45Q tax credit", "Registrant": "X", "Ticker": "OXY"}]
    prov = LobbyingProvider(_cfg(), _FakeQuiver({"lobbying": feed}))
    sig = prov.fetch(["OXY"])[0]
    assert sig.score > 0 and sig.data["filings"] == 1
    assert sig.data["total_usd"] == 50_000.0
    assert "Taxation/Internal Revenue Code" in sig.summary
    assert "Energy/Nuclear" not in sig.summary   # only the first issue line


def test_lobbying_bullish_only_and_capped():
    feed = [{"Ticker": "META", "Date": _recent(), "Amount": 10_000_000, "Issue": "Tech"}]
    prov = LobbyingProvider(_cfg(), _FakeQuiver({"lobbying": feed}))
    sig = prov.fetch(["META"])[0]
    assert sig.score == 0.25                    # full lean at >=$2M, never above cap


def test_lobbying_more_dollars_score_higher():
    small = [{"Ticker": "A", "Date": _recent(), "Amount": 20_000}]
    big = [{"Ticker": "A", "Date": _recent(), "Amount": 1_000_000}]
    p_s = LobbyingProvider(_cfg(), _FakeQuiver({"lobbying": small}))
    p_b = LobbyingProvider(_cfg(), _FakeQuiver({"lobbying": big}))
    assert p_b.fetch(["A"])[0].score > p_s.fetch(["A"])[0].score


def test_lobbying_zero_amount_filing_still_signals_with_floor():
    # "0.0" amounts are real (spend withheld); the filing still carries the ISSUE
    # context to the prompt at a tiny floor score.
    feed = [{"Ticker": "WEN", "Date": _recent(), "Amount": "0.0",
             "Issue": "Small Business \nAgriculture"}]
    prov = LobbyingProvider(_cfg(), _FakeQuiver({"lobbying": feed}))
    sig = prov.fetch(["WEN"])[0]
    assert sig.score == 0.05 and sig.data["total_usd"] == 0.0
    assert "Small Business" in sig.summary


def test_lobbying_keeps_issue_of_largest_filing():
    feed = [
        {"Ticker": "OXY", "Date": _recent(), "Amount": 10_000, "Issue": "Minor"},
        {"Ticker": "OXY", "Date": _recent(), "Amount": 500_000, "Issue": "Energy"},
        {"Ticker": "OXY", "Date": _recent(), "Amount": 5_000, "Issue": "Tiny"},
    ]
    prov = LobbyingProvider(_cfg(), _FakeQuiver({"lobbying": feed}))
    sig = prov.fetch(["OXY"])[0]
    assert "top issue: Energy" in sig.summary
    assert sig.data["filings"] == 3 and sig.data["total_usd"] == 515_000.0


def test_lobbying_ignores_old_and_unwanted_rows():
    feed = [
        {"Ticker": "OXY", "Date": _old(), "Amount": 900_000, "Issue": "Stale"},
        {"Ticker": "ZZZ", "Date": _recent(), "Amount": 900_000, "Issue": "Other"},
    ]
    prov = LobbyingProvider(_cfg(), _FakeQuiver({"lobbying": feed}))
    assert prov.fetch(["OXY"]) == []            # outside 90d lookback; ZZZ unwanted


def test_lobbying_lag_weight_is_heavily_discounted():
    # The composite must treat lobbying as ~45d-stale corroboration (w ~0.11),
    # even staler than congress (0.23) — the 2026-07-27 lesson, applied from birth.
    from investment_strategy.models import SignalKind
    from investment_strategy.signals.history import lag_weight
    assert lag_weight(SignalKind.LOBBYING) < lag_weight(SignalKind.CONGRESS)
    assert abs(lag_weight(SignalKind.LOBBYING) - 0.11) < 1e-9


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
