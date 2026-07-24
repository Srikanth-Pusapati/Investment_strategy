"""Tests for the track-record page (goGA GA-1.2).

The page's contract: disclaimers are BAKED IN (no caller can omit them), the
numbers render whatever they are (honesty rule), and the naive trailing-stop
benchmark is deterministic and surfaces its whipsaw count.

Runnable two ways:
    .venv/bin/python tests/test_track_record.py
    .venv/bin/pytest tests/
"""
from __future__ import annotations

import os
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.track_record import (
    _MIN_RATIO_DAYS,
    _align_benchmark,
    _sharpe,
    _sortino,
    build_html,
    generate,
    trailing_stop_series,
)


# -- the naive trailing-stop benchmark ---------------------------------------- #
def test_trailing_stop_rides_a_straight_uptrend_fully_invested():
    closes = [100.0 * (1.01 ** i) for i in range(50)]
    series, exits = trailing_stop_series(closes, trail_pct=15.0)
    assert exits == 0
    assert abs(series[-1] - closes[-1] / closes[0]) < 1e-9   # tracks the asset


def test_trailing_stop_exits_on_a_crash_and_sidesteps_the_rest():
    closes = [100.0] * 5 + [80.0, 60.0, 40.0]     # -20% breaches a 15% trail
    series, exits = trailing_stop_series(closes, trail_pct=15.0)
    assert exits == 1
    assert abs(series[-1] - 0.80) < 1e-9          # took -20%, missed the rest


def test_trailing_stop_reenters_above_prior_peak():
    # Dip out (-20%), then recover through the old peak -> back in for the run.
    closes = [100.0, 80.0, 90.0, 101.0, 111.1]
    series, exits = trailing_stop_series(closes, trail_pct=15.0)
    assert exits == 1
    # Out for the 80->101 recovery; captures only 101 -> 111.1 (+10%).
    assert abs(series[-1] - 0.80 * (111.1 / 101.0)) < 1e-9


def test_whipsaw_cost_is_counted():
    # Two dip-and-recover cycles = two stop exits = the visible seatbelt cost.
    cycle = [100.0, 84.0, 101.0]
    closes = [100.0] + cycle + cycle
    _, exits = trailing_stop_series(closes, trail_pct=15.0)
    assert exits == 2


# -- benchmark/date alignment -------------------------------------------------- #
def test_align_benchmark_carries_last_close_over_gaps():
    bench = [("2026-07-01", 100.0), ("2026-07-02", 102.0), ("2026-07-06", 104.0)]
    dates = ["2026-07-01", "2026-07-04", "2026-07-06"]  # weekend row in history
    assert _align_benchmark(bench, dates) == [100.0, 102.0, 104.0]


def test_align_benchmark_none_when_history_predates_data():
    bench = [("2026-07-05", 100.0)]
    assert _align_benchmark(bench, ["2026-07-01"]) is None


# -- the page ------------------------------------------------------------------ #
def _rows(values):
    return [
        {"date": f"2026-07-{i + 1:02d}", "equity": v} for i, v in enumerate(values)
    ]


def test_disclaimers_are_baked_in_and_unremovable():
    html = build_html(_rows([100_000, 101_000]), [], [], {}, [])
    for required in ("Paper trading", "Hypothetical performance",
                     "does not guarantee future results", "Not investment advice"):
        assert required in html, f"missing mandated disclaimer: {required}"


def test_honesty_rule_negative_numbers_render():
    html = build_html(_rows([100_000, 80_000]), [], [], {}, [])
    assert "-20.00%" in html                       # the loss is shown, not hidden


def test_config_change_log_renders_and_freeze_note_when_empty():
    html = build_html(_rows([100_000, 101_000]), [], [], {},
                      [{"ts": "2026-07-10", "change": "STOP 5->8",
                        "why": "halt-severity bug"}])
    assert "STOP 5-&gt;8" in html or "STOP 5->8" in html
    empty = build_html(_rows([100_000, 101_000]), [], [], {}, [])
    assert "frozen-config policy" in empty


def test_generate_writes_offline_page():
    tmp = Path(tempfile.gettempdir())
    out = tmp / f"_tr_{uuid.uuid4().hex}.html"
    ledger = tmp / f"_tr_{uuid.uuid4().hex}.jsonl"
    equity = tmp / f"_tr_{uuid.uuid4().hex}_eq.jsonl"
    generate(out, ledger_path=ledger, equity_path=equity, live=False)
    text = out.read_text(encoding="utf-8")
    assert "Track record" in text and "Hypothetical" in text
    out.unlink()


# -- risk-adjusted stats: Sharpe / Sortino ------------------------------------ #
def _series_from_returns(rets):
    s = [100.0]
    for r in rets:
        s.append(s[-1] * (1 + r))
    return s


def test_sharpe_none_below_min_days():
    # Only two daily returns — far below the min sample; withheld as noise.
    assert _sharpe([100.0, 101.0, 102.0]) is None


def test_sharpe_none_on_flat_curve():
    # Enough points but zero variance -> undefined (no risk to divide by).
    assert _sharpe([100.0] * (_MIN_RATIO_DAYS + 5)) is None


def test_sharpe_positive_for_rising_varied_curve():
    rets = [0.01, 0.005, 0.015, 0.008, 0.012] * 5   # 25 positive, varied -> sd>0
    sh = _sharpe(_series_from_returns(rets))
    assert sh is not None and sh > 0


def test_sortino_none_when_no_downside():
    rets = [0.01, 0.005, 0.015, 0.008, 0.012] * 5   # all up -> no downside days
    assert _sortino(_series_from_returns(rets)) is None


def test_sortino_defined_with_downside():
    rets = [0.02, -0.01, 0.015, -0.008, 0.012] * 5  # 25, has downside, net +
    so = _sortino(_series_from_returns(rets))
    assert so is not None and so > 0


def test_attribution_table_uses_cited_basis():
    # Jul-24 prune: the per-source table counts a trip toward a source only
    # when the model cited it as decisive; ride-along presence must not appear.
    from investment_strategy.ledger import TradeRecord
    from investment_strategy.track_record import _attribution_table

    records = []
    for i in range(2):
        records.append(TradeRecord(
            symbol=f"S{i}", action="buy", qty=1.0,
            entry_signals=["insider", "technical"],
            key_signals=["insider Form4 +1.00"]))
        records.append(TradeRecord(
            symbol=f"S{i}", action="sell", qty=1.0, realized_pl_pct=-5.0))
    html_out = _attribution_table(records)
    assert "insider" in html_out
    assert "technical" not in html_out
    assert "Cited basis" in html_out       # footnote documents the semantics


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
