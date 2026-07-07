"""Tests for investment_strategy.journal (B1 intra-day decision journal)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.journal import DecisionJournal, DecisionRecord, _trading_day


def _tmpdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="_journal_"))


def _rec(symbol="LLY", verdict="approved", notional=1000.0, action="buy",
         conviction=0.78, reason="test") -> DecisionRecord:
    return DecisionRecord(
        ts=datetime.now(timezone.utc).isoformat(),
        symbol=symbol,
        action=action,
        instrument="equity",
        conviction=conviction,
        target_weight_pct=8.0,
        verdict=verdict,
        approved_notional=notional,
        reason=reason,
        rationale_head="test rationale",
    )


# ---- record round-trip ---------------------------------------------------- #

def test_record_roundtrip():
    j = DecisionJournal(base_dir=_tmpdir())
    r = _rec("AAPL", "approved", 1234.56, conviction=0.82)
    j.record(r)
    recs = j.today()
    assert len(recs) == 1
    assert recs[0].symbol == "AAPL"
    assert abs(recs[0].approved_notional - 1234.56) < 0.01
    assert recs[0].conviction == 0.82
    assert recs[0].verdict == "approved"


def test_multiple_records_in_order():
    j = DecisionJournal(base_dir=_tmpdir())
    for sym in ["AAPL", "MSFT", "TSLA"]:
        j.record(_rec(sym, "approved", 500.0))
    recs = j.today()
    assert len(recs) == 3
    assert [r.symbol for r in recs] == ["AAPL", "MSFT", "TSLA"]


# ---- day-keyed filename ---------------------------------------------------- #

def test_day_keyed_filename():
    base = _tmpdir()
    j = DecisionJournal(base_dir=base)
    when = datetime(2026, 7, 6, 18, 0, 0, tzinfo=timezone.utc)
    day = _trading_day(when)  # "2026-07-06" (ET is UTC-4/5 so this stays 7-6)
    j.record(_rec("LLY"))
    files = list(base.glob("*.jsonl"))
    assert len(files) == 1
    assert files[0].stem == _trading_day()  # today's key


def test_has_records_today_false_when_empty():
    j = DecisionJournal(base_dir=_tmpdir())
    assert not j.has_records_today()


def test_has_records_today_true_after_write():
    j = DecisionJournal(base_dir=_tmpdir())
    j.record(_rec("AAPL"))
    assert j.has_records_today()


# ---- render_today aggregation --------------------------------------------- #

def test_render_today_empty_returns_empty():
    j = DecisionJournal(base_dir=_tmpdir())
    assert j.render_today(equity=100_000.0) == ""


def test_render_today_approved_shows_symbol():
    j = DecisionJournal(base_dir=_tmpdir())
    j.record(_rec("LLY", "approved", 2000.0, conviction=0.78))
    block = j.render_today(equity=100_000.0)
    assert "LLY" in block
    assert "2,000" in block or "2000" in block


def test_render_today_aggregates_multi_buy():
    j = DecisionJournal(base_dir=_tmpdir())
    j.record(_rec("LLY", "approved", 1000.0, conviction=0.78))
    j.record(_rec("LLY", "approved", 1500.0, conviction=0.80))
    block = j.render_today(equity=100_000.0)
    assert "2x" in block  # two buys aggregated
    assert "2,500" in block or "2500" in block  # total shown


def test_render_today_rejected_appears():
    j = DecisionJournal(base_dir=_tmpdir())
    j.record(_rec("NVDA", "rejected", 0.0, reason="daily cap reached"))
    block = j.render_today(equity=100_000.0)
    assert "Rejected" in block
    assert "NVDA" in block


def test_render_today_excluded_appears_and_warns():
    j = DecisionJournal(base_dir=_tmpdir())
    j.record(_rec("LLY", "slate_excluded", 0.0, reason="daily buy cap"))
    block = j.render_today(equity=100_000.0)
    assert "excluded" in block.lower()
    assert "Do not re-propose" in block


def test_render_today_dropped_buy_appears():
    j = DecisionJournal(base_dir=_tmpdir())
    j.record(_rec("LLY", "dropped_buy", 0.0, reason="backstop: buy excluded"))
    block = j.render_today(equity=100_000.0)
    assert "excluded" in block.lower()


def test_render_today_line_cap():
    """The block should not explode to an arbitrary number of lines."""
    j = DecisionJournal(base_dir=_tmpdir())
    for i in range(30):
        j.record(_rec(f"SYM{i:02d}", "approved", 100.0))
    block = j.render_today(equity=100_000.0)
    lines = block.splitlines()
    # Bought line aggregates to 8 symbols max; total block should be bounded
    assert len(lines) <= 20, f"Too many lines ({len(lines)}) in render_today block"


# ---- from_dict round-trip (serialization) ---------------------------------- #

def test_from_dict_preserves_fields():
    r = _rec("TSLA", "resized", 750.25, conviction=0.91, reason="resized by risk layer")
    d = r.to_dict()
    r2 = DecisionRecord.from_dict(d)
    assert r2.symbol == "TSLA"
    assert r2.verdict == "resized"
    assert abs(r2.approved_notional - 750.25) < 0.01
    assert r2.conviction == 0.91


def test_reason_truncated_at_200():
    long_reason = "x" * 300
    r = _rec(reason=long_reason)
    assert len(r.reason) == 200


def test_rationale_head_truncated_at_120():
    long = "y" * 200
    r = DecisionRecord(
        ts="", symbol="X", action="buy", instrument="equity",
        conviction=0.5, target_weight_pct=5.0, verdict="approved",
        rationale_head=long,
    )
    assert len(r.rationale_head) == 120


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS {fn.__name__}")
        except Exception as e:
            failed += 1
            print(f"  FAIL {fn.__name__}: {e!r}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
