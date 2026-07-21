"""ET-day bucketing for the API-spend rollup.

Records are stamped in UTC but the nightly rollup must key on the ET trading
day — otherwise a post-mortem that fires after UTC midnight (e.g. 03:43Z, which
is still ~23:43 ET the SAME trading day) tallies only the sliver of spend after
00:00Z. This is the same UTC-midnight bug class as the PR #27 post-mortem fix.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.usage import _et_day, summarize_day


def test_et_day_maps_post_utc_midnight_to_prior_trading_day():
    # 03:43Z on Jul 21 is 23:43 ET on Jul 20 — still Jul 20's session.
    assert _et_day("2026-07-21T03:43:00+00:00") == "2026-07-20"
    assert _et_day("2026-07-20T18:00:00+00:00") == "2026-07-20"
    # naive timestamps are assumed UTC, not silently dropped
    assert _et_day("2026-07-21T03:43:00") == "2026-07-20"
    assert _et_day("garbage") == ""


def test_summarize_day_buckets_by_et_trading_day():
    d = Path(tempfile.mkdtemp()) / "api_usage.jsonl"
    rows = [
        {"ts": "2026-07-20T14:00:00+00:00", "in": 100, "out": 50, "est_cost_usd": 0.5},
        {"ts": "2026-07-21T02:00:00+00:00", "in": 100, "out": 50, "est_cost_usd": 0.8},
        {"ts": "2026-07-21T14:00:00+00:00", "in": 100, "out": 50, "est_cost_usd": 0.3},
    ]
    d.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    # The 02:00Z Jul-21 record belongs to the Jul-20 ET session (22:00 ET).
    calls, in_tok, out_tok, cost = summarize_day("2026-07-20", d)
    assert calls == 2
    assert round(cost, 2) == 1.30
    calls, *_ , cost = summarize_day("2026-07-21", d)
    assert calls == 1
    assert round(cost, 2) == 0.30


def test_summarize_day_missing_file_is_zero():
    missing = Path(tempfile.mkdtemp()) / "nope.jsonl"
    assert summarize_day("2026-07-20", missing) == (0, 0, 0, 0.0)


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
