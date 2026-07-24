"""Tests for the insider DISCOVERY screener's bidirectional surfacing.

Buying is unchanged (positive score on cluster breadth). Selling is the new
downside path: a clustered open-market sell surfaces a discounted NEGATIVE score
so the bot can find names set up to fall and express them with a long put. A
lone seller is noise and must never surface. Pure logic — EDGAR is mocked."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.screener.insider_feed import InsiderFeedScreener


def _screener(scan_limit=100):
    s = InsiderFeedScreener.__new__(InsiderFeedScreener)  # skip network __init__
    s.cfg = SimpleNamespace(
        sec_user_agent="test@example.com",
        screener=SimpleNamespace(insider_scan_limit=scan_limit),
    )
    s._headers = {}
    return s


def _run(parsed):
    """parsed: list of (sym, buy_sh, sell_sh) — one per fake filing."""
    s = _screener()
    filings = [("cik", "accno", str(i)) for i in range(len(parsed))]
    s._recent_form4_filings = lambda: filings
    lookup = {str(i): parsed[i] for i in range(len(parsed))}
    s._parse_submission = lambda cik, accno, acc: lookup[acc]
    return {c.symbol: c for c in s.scan()}


def test_single_buy_unchanged():
    out = _run([("AAA", 500, 0)])
    assert abs(out["AAA"].score - round(1 / 3, 3)) < 1e-9
    assert "open-market buy" in out["AAA"].reason
    assert "500 sh" in out["AAA"].reason


def test_buy_cluster_saturates_at_one():
    out = _run([("AAA", 100, 0), ("AAA", 200, 0), ("AAA", 300, 0)])
    assert out["AAA"].score == 1.0


def test_single_seller_is_noise_not_surfaced():
    """One open-market seller must NOT surface — execs sell for many reasons."""
    out = _run([("BBB", 0, 1000)])
    assert "BBB" not in out


def test_two_sellers_surface_discounted_bearish():
    """A cluster of 2 distinct sellers surfaces a negative score, discounted by
    _SELL_WEIGHT (0.6): min(2,3)/3 * 0.6 = 0.4."""
    out = _run([("CCC", 0, 4000), ("CCC", 0, 6000)])
    assert out["CCC"].score == -0.4
    assert "SELL" in out["CCC"].reason
    assert "bearish cluster" in out["CCC"].reason


def test_three_plus_sellers_saturate_at_weighted_max():
    out = _run([("DDD", 0, 1), ("DDD", 0, 1), ("DDD", 0, 1), ("DDD", 0, 1)])
    assert out["DDD"].score == -0.6      # min(4,3)/3 * 0.6


def test_buys_and_sells_net_out_bullish_dominant():
    """3 buyers (1.0) vs 2 sellers (0.4) -> net +0.6, still bullish; the sell
    cluster correctly discounts the buy conviction."""
    out = _run([
        ("EEE", 100, 0), ("EEE", 100, 0), ("EEE", 100, 0),
        ("EEE", 0, 500), ("EEE", 0, 500),
    ])
    assert out["EEE"].score == 0.6
    assert "open-market buy" in out["EEE"].reason


def test_one_buyer_two_sellers_flips_bearish():
    """1 buyer (0.333) vs 2 sellers (0.4) -> net negative: a lone buy can't hold
    up against a selling cluster."""
    out = _run([("FFF", 100, 0), ("FFF", 0, 900), ("FFF", 0, 900)])
    assert out["FFF"].score < 0
    assert "SELL" in out["FFF"].reason


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS {fn.__name__}")
        except Exception:
            failed += 1
            print(f"  FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
