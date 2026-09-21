"""Run-6 item 1 — measurement plumbing (no strategy change).

(a) post-mortem: open option groups are marked close-to-close (or reported
    UNMARKED explicitly), core_fill rows are excluded, the name count no
    longer counts the summary line, read_curated honours max_lines
(b) equity_history: one fixed basis='close' row per ET day, never overwritten;
    run-7 B1: a close/late row's day_pl is equity - the previous close/late
    row's equity (self-consistent, telescopes by construction) and the
    broker's figure is preserved as broker_day_pl
(c) eval_contract_check: telescoping + basis + extra benchmarks
(d) dashboard: no latest_price() on OCC symbols
(e) fill prices stamped onto the ledger row at FILLED confirmation;
    run-7 B2: an equity SELL row's exit_price / realized_pl / realized_pl_pct
    are restated at that fill (as-recorded figures kept as quote_*)
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import investment_strategy.postmortem as pm_mod  # noqa: E402
from investment_strategy.ledger import TradeLedger, TradeRecord  # noqa: E402
from investment_strategy.status import AccountStatus, EquityHistory  # noqa: E402

_ET = ZoneInfo("America/New_York")


def _tmp(suffix: str) -> Path:
    return Path(tempfile.gettempdir()) / f"_run6_{uuid.uuid4().hex}{suffix}"


def _rec(**kw) -> TradeRecord:
    return TradeRecord(**kw)


def _series(table):
    def _fn(symbol, days):
        return table.get(symbol, [])
    return _fn


# ---------------------------------------------------------------- (a) -------

DAY = "2026-08-25"
PUT = "IWM260918P00220000"       # single-leg long put, OCC-keyed row
CALL_LO = "NVDA260918C00180000"  # bull call spread legs (keyed by underlying)
CALL_HI = "NVDA260918C00190000"


def test_open_single_leg_option_is_marked_close_to_close():
    records = [
        _rec(ts="2026-08-24T14:00:00Z", symbol=PUT, action="buy",
             instrument="option", qty=10.0, entry_price=3.0, cost_usd=3000.0,
             underlying="IWM", occ_symbols=[PUT]),
    ]
    opt = _series({PUT: [("2026-08-24", 3.20), (DAY, 2.90)]})
    lines, winner, loser = pm_mod.day_marks(records, DAY, _series({}),
                                            option_close_series=opt)
    text = "\n".join(lines)
    # 10 contracts x 100 x (2.90 - 3.20) = -300, held into close
    assert "IWM" in text and "-300" in text and "held into close" in text
    assert "UNMARKED" not in text
    assert "LOSER by close-to-close: IWM" in loser
    assert pm_mod.name_count(lines) == 1     # the '-> Day WINNER' line is not a name


def test_open_spread_uses_leg_sides_and_bought_today_uses_cost():
    records = [
        _rec(ts=f"{DAY}T14:30:00Z", symbol="NVDA", action="buy",
             instrument="option", qty=5.0, entry_price=4.0, cost_usd=2000.0,
             underlying="NVDA", occ_symbols=[CALL_LO, CALL_HI],
             occ_sides=["buy", "sell"], option_strategy="bull_call_spread"),
    ]
    opt = _series({CALL_LO: [(DAY, 9.0)], CALL_HI: [(DAY, 4.5)]})
    lines, _, _ = pm_mod.day_marks(records, DAY, _series({}),
                                   option_close_series=opt)
    text = "\n".join(lines)
    # end mark = 5*100*(9.0 - 4.5) = 2250; bought today for 2000 -> +250
    assert "NVDA" in text and "+250" in text and "UNMARKED" not in text


def test_option_closed_today_uses_proceeds_vs_prior_mark():
    records = [
        _rec(ts="2026-08-20T14:00:00Z", symbol=PUT, action="buy",
             instrument="option", qty=10.0, entry_price=3.0, cost_usd=3000.0,
             underlying="IWM", occ_symbols=[PUT]),
        _rec(ts=f"{DAY}T15:00:00Z", symbol=PUT, action="sell",
             instrument="option", qty=10.0, realized_pl_pct=-20.0,
             realized_pl=-600.0, exit_reason="stop", underlying="IWM",
             occ_symbols=[PUT]),
    ]
    opt = _series({PUT: [("2026-08-24", 2.70), (DAY, 2.30)]})
    lines, _, _ = pm_mod.day_marks(records, DAY, _series({}),
                                   option_close_series=opt)
    text = "\n".join(lines)
    # proceeds = 3000 - 600 = 2400; prior mark = 10*100*2.70 = 2700 -> -300
    assert "-300" in text and "flat at close" in text
    assert "realized today (premium-basis): -600" in text


def test_option_without_bars_or_sides_is_reported_unmarked_not_silent():
    records = [
        _rec(ts="2026-08-24T14:00:00Z", symbol=PUT, action="buy",
             instrument="option", qty=10.0, entry_price=3.0, cost_usd=3000.0,
             underlying="IWM", occ_symbols=[PUT]),
        # legacy spread row: legs listed, sides unknown
        _rec(ts="2026-08-24T14:00:00Z", symbol="NVDA", action="buy",
             instrument="option", qty=5.0, entry_price=4.0, cost_usd=2000.0,
             underlying="NVDA", occ_symbols=[CALL_LO, CALL_HI]),
    ]
    lines, winner, loser = pm_mod.day_marks(
        records, DAY, _series({}), option_close_series=pm_mod._no_option_marks)
    text = "\n".join(lines)
    assert text.count("UNMARKED") == 2
    assert "no prior bar for " + PUT in text
    assert "leg sides unknown" in text
    assert winner == "" and loser == ""


def test_option_marks_off_keeps_legacy_realized_only_line():
    records = [
        _rec(ts=f"{DAY}T15:00:00Z", symbol="AMZN", action="sell",
             instrument="option", qty=26.0, realized_pl_pct=-50.0,
             realized_pl=-14956.0, exit_reason="stop"),
    ]
    lines, _, _ = pm_mod.day_marks(records, DAY, _series({}))
    assert any("no close-to-close mark for options" in l for l in lines)


def test_core_fill_rows_and_their_symbol_are_excluded():
    records = [
        TradeRecord.from_core_fill("QQQ", 150_000.0, 500.0, "oid-core"),
        _rec(ts=f"{DAY}T18:00:00Z", symbol="QQQ", action="sell", qty=10.0,
             exit_price=495.0, realized_pl_pct=-1.0, realized_pl=-50.0,
             exit_reason="core_stop"),
        _rec(ts=f"{DAY}T14:00:00Z", symbol="AAPL", action="buy", qty=10.0,
             entry_price=100.0, cost_usd=1000.0, entry_signals=["technical"]),
    ]
    kept, excluded = pm_mod.exclude_core_fill(records)
    assert excluded == ["QQQ"]
    assert [r.symbol for r in kept] == ["AAPL"]
    kept2, excluded2 = pm_mod.exclude_core_fill(records[2:])
    assert excluded2 == [] and len(kept2) == 1


def test_run_postmortem_prompt_excludes_core_fill_and_marks_option(tmp_path):
    from unittest.mock import MagicMock
    from investment_strategy.journal import DecisionJournal

    (tmp_path / f"{DAY}.jsonl").write_text(json.dumps({
        "ts": f"{DAY}T14:00:00+00:00", "symbol": "AAPL", "action": "buy",
        "instrument": "equity", "conviction": 0.6, "target_weight_pct": 5.0,
        "verdict": "approved", "approved_notional": 500.0, "reason": "r",
        "rationale_head": "rh",
    }) + "\n", encoding="utf-8")
    journal = DecisionJournal(base_dir=tmp_path)

    class _Ledger:
        def effective(self):
            core = TradeRecord.from_core_fill("QQQ", 150_000.0, 500.0, "oid-core")
            core = core.model_copy(update={
                "ts": datetime(2026, 8, 25, 14, 0, tzinfo=timezone.utc)})
            return [
                core,
                _rec(ts="2026-08-24T14:00:00Z", symbol=PUT, action="buy",
                     instrument="option", qty=10.0, entry_price=3.0,
                     cost_usd=3000.0, underlying="IWM", occ_symbols=[PUT]),
            ]

    broker = MagicMock()
    broker.daily_close_series = _series({})
    broker.option_close_series = _series({PUT: [("2026-08-24", 3.2), (DAY, 2.9)]})
    cfg = SimpleNamespace(postmortem_option_marks=True)
    printed: list[str] = []
    with patch.dict(sys.modules, {"anthropic": MagicMock()}), \
         patch("builtins.print", lambda *a, **k: printed.append(" ".join(map(str, a)))):
        result = pm_mod.run_postmortem(cfg, _Ledger(), journal, day=DAY,
                                       dry_run=True, broker=broker)
    assert result is not None
    prompt = "\n".join(printed)
    assert "BUY QQQ" not in prompt
    assert "core ETF rows excluded — QQQ" in prompt
    assert "IWM" in prompt and "-300" in prompt and "UNMARKED" not in prompt


def test_run_postmortem_broker_without_option_source_reports_unmarked(tmp_path):
    from unittest.mock import MagicMock
    from investment_strategy.journal import DecisionJournal

    (tmp_path / f"{DAY}.jsonl").write_text(json.dumps({
        "ts": f"{DAY}T14:00:00+00:00", "symbol": "AAPL", "action": "buy",
        "instrument": "equity", "conviction": 0.6, "target_weight_pct": 5.0,
        "verdict": "approved", "approved_notional": 500.0, "reason": "r",
        "rationale_head": "rh",
    }) + "\n", encoding="utf-8")
    journal = DecisionJournal(base_dir=tmp_path)

    class _Ledger:
        def effective(self):
            return [_rec(ts="2026-08-24T14:00:00Z", symbol=PUT, action="buy",
                         instrument="option", qty=10.0, entry_price=3.0,
                         cost_usd=3000.0, underlying="IWM", occ_symbols=[PUT])]

    class _Broker:  # no option_close_series / option_close_marks at all
        daily_close_series = staticmethod(_series({}))

    printed: list[str] = []
    with patch.dict(sys.modules, {"anthropic": MagicMock()}), \
         patch("builtins.print", lambda *a, **k: printed.append(" ".join(map(str, a)))):
        pm_mod.run_postmortem(None, _Ledger(), journal, day=DAY,
                              dry_run=True, broker=_Broker())
    assert "IWM" in "\n".join(printed) and "UNMARKED" in "\n".join(printed)


def test_read_curated_honours_max_lines():
    d = _tmp("")
    d.mkdir()
    (d / "curated.md").write_text("\n".join(f"L{i}" for i in range(10)) + "\n")
    with patch.object(pm_mod, "_CURATED_FILE", d / "curated.md"):
        out = pm_mod.read_curated(max_lines=3)
    body = out.splitlines()[1:]
    assert body == ["L7", "L8", "L9"]


def test_name_count_ignores_summary_line():
    lines = ["  AAPL     day +1 USD", "  MSFT     day -2 USD",
             "  -> Day WINNER by close-to-close: AAPL +1 USD; Day LOSER ..."]
    assert pm_mod.name_count(lines) == 2


# ---------------------------------------------------------------- (b) -------

def _status(equity: float, day_pl: float = 0.0, as_of=None) -> AccountStatus:
    kw = dict(equity=equity, cash=1.0, buying_power=1.0, n_positions=0,
              unrealized_pl=0.0, day_pl=day_pl, day_pl_pct=0.0)
    if as_of is not None:
        kw["as_of"] = as_of
    return AccountStatus(**kw)


def test_close_row_is_final_and_intraday_rows_are_labelled():
    h = EquityHistory(path=_tmp(".jsonl"))
    h.snapshot(_status(100.0), basis="intraday", day="2026-08-25")
    h.snapshot(_status(101.0), basis="close", day="2026-08-25")
    h.snapshot(_status(102.0), basis="intraday", day="2026-08-25")   # dropped
    h.snapshot(_status(103.0), day="2026-08-25")                     # legacy: dropped
    rows = h.all()
    assert len(rows) == 1 and rows[0]["equity"] == 101.0
    assert rows[0]["basis"] == "close" and h.has_close_row("2026-08-25")
    assert not h.has_close_row("2026-08-26")
    # legacy writer path still dedupes/overwrites and writes no basis field
    h.snapshot(_status(50.0, as_of=datetime(2026, 8, 26, 15, 0, tzinfo=timezone.utc)))
    h.snapshot(_status(51.0, as_of=datetime(2026, 8, 26, 16, 0, tzinfo=timezone.utc)))
    rows = h.all()
    assert rows[-1]["equity"] == 51.0 and "basis" not in rows[-1]


def _orch_with_history():
    from test_orchestrator import _orch
    o = _orch()
    o.equity_history = EquityHistory(path=_tmp(".jsonl"))
    o.broker.get_account = lambda: SimpleNamespace(
        positions=[], equity=1_000_000.0, cash=500_000.0, buying_power=1.0,
        day_pl=0.0, day_pl_pct=0.0, pattern_day_trader=False, daytrade_count=0)
    o.broker.portfolio_basis = lambda: None
    return o


def test_closing_snapshot_stamps_once_after_1600_et_and_never_overwrites():
    o = _orch_with_history()
    o.cfg.equity_close_fixed_stamp = True
    before = datetime(2026, 8, 25, 8, 30, tzinfo=_ET)      # pre-open closed tick
    o._refresh_closing_snapshot(now_et=before)
    assert o.equity_history.all() == []
    at_close = datetime(2026, 8, 25, 16, 1, tzinfo=_ET)
    o._refresh_closing_snapshot(now_et=at_close)
    rows = o.equity_history.all()
    assert len(rows) == 1 and rows[0]["date"] == "2026-08-25"
    assert rows[0]["basis"] == "close" and rows[0]["equity"] == 1_000_000.0
    # after-hours drift must NOT move the row (the Aug 24/25 re-stamp bug)
    o.broker.get_account = lambda: SimpleNamespace(
        positions=[], equity=1_007_879.0, cash=1.0, buying_power=1.0,
        day_pl=0.0, day_pl_pct=0.0, pattern_day_trader=False, daytrade_count=0)
    o._refresh_closing_snapshot(now_et=datetime(2026, 8, 25, 23, 56, tzinfo=_ET))
    rows = o.equity_history.all()
    assert len(rows) == 1 and rows[0]["equity"] == 1_000_000.0
    # weekend ticks never mint a row
    o._refresh_closing_snapshot(now_et=datetime(2026, 8, 29, 17, 0, tzinfo=_ET))
    assert len(o.equity_history.all()) == 1


def test_closing_snapshot_legacy_path_when_knob_off():
    o = _orch_with_history()
    o.cfg.equity_close_fixed_stamp = False
    o._refresh_closing_snapshot(now_et=datetime(2026, 8, 25, 16, 1, tzinfo=_ET))
    assert o.equity_history.all() == []   # legacy: no row today -> nothing


# ---- run-7 B1: self-consistent close-row day_pl ----------------------------
# Real run-6 rows (state/equity_history.jsonl). The broker's day_pl is
# equity - Alpaca last_equity, and Alpaca restates last_equity overnight, so
# the broker figure disagreed with our own close-to-close delta by -776.77
# (Sep 2) and +1,421.49 (Sep 3) — the checker's telescoping FAIL every run.
_RUN6_ROWS = [  # (day, basis, equity, broker day_pl)
    ("2026-08-31", "late", 1_000_000.00, 0.0),
    ("2026-09-01", "close", 1_000_943.64, 943.64),
    ("2026-09-02", "close", 1_005_629.55, 5_462.68),
    ("2026-09-03", "close", 1_016_452.05, 9_401.01),
]


def test_close_rows_telescope_exactly_across_three_sessions():
    h = EquityHistory(path=_tmp(".jsonl"))
    for day, basis, eq, pl in _RUN6_ROWS:
        h.snapshot(_status(eq, day_pl=pl), basis=basis, day=day)
    rows = h.all()
    closes = [r for r in rows if r["basis"] == "close"]
    assert [r["day_pl"] for r in closes] == [943.64, 4_685.91, 10_822.50]
    assert [r["day_pl_basis"] for r in closes] == ["self"] * 3
    # the broker's figure is preserved verbatim next to the derived one
    assert [r["broker_day_pl"] for r in closes] == [943.64, 5_462.68, 9_401.01]
    # telescoping: sum(day_pl) over the three close rows == delta-equity, to the cent
    assert round(sum(r["day_pl"] for r in closes), 2) == round(1_016_452.05 - 1_000_000.0, 2)
    for prev, cur in zip(rows, rows[1:]):
        assert abs((cur["equity"] - prev["equity"]) - cur["day_pl"]) < 0.005
    # Sep 3's restatement is exactly the $1,421.49 gap the run-6 checker flagged
    assert round(closes[2]["day_pl"] - closes[2]["broker_day_pl"], 2) == 1_421.49


def test_late_baseline_is_the_predecessor_of_the_first_close_row():
    h = EquityHistory(path=_tmp(".jsonl"))
    h.snapshot(_status(1_000_000.0, day_pl=0.0), basis="late", day="2026-08-31")
    # broker restated last_equity overnight -> its day_pl is NOT our delta
    h.snapshot(_status(1_000_943.64, day_pl=1_720.41), basis="close", day="2026-09-01")
    rows = h.all()
    assert rows[0]["basis"] == "late" and rows[0]["day_pl_basis"] == "broker"
    assert rows[0]["day_pl"] == 0.0 and rows[0]["broker_day_pl"] == 0.0
    assert rows[1]["day_pl"] == 943.64 and rows[1]["day_pl_basis"] == "self"
    assert rows[1]["broker_day_pl"] == 1_720.41


def test_first_close_row_without_predecessor_keeps_broker_day_pl():
    h = EquityHistory(path=_tmp(".jsonl"))
    h.snapshot(_status(1_000_456.0, day_pl=456.0), basis="close", day="2026-08-24")
    r = h.all()[0]
    assert r["day_pl"] == 456.0 and r["broker_day_pl"] == 456.0
    assert r["day_pl_basis"] == "broker"
    # legacy (no basis) and intraday rows are not predecessors either
    h2 = EquityHistory(path=_tmp(".jsonl"))
    h2.snapshot(_status(100.0, day_pl=1.0), day="2026-08-21")                    # legacy
    h2.snapshot(_status(101.0, day_pl=2.0), basis="intraday", day="2026-08-24")
    h2.snapshot(_status(105.0, day_pl=3.0), basis="close", day="2026-08-25")
    r = h2.all()[-1]
    assert r["day_pl"] == 3.0 and r["broker_day_pl"] == 3.0
    assert r["day_pl_basis"] == "broker"


def test_intraday_rows_are_unchanged_and_skipped_as_predecessors():
    h = EquityHistory(path=_tmp(".jsonl"))
    h.snapshot(_status(100.0, day_pl=0.0), basis="close", day="2026-09-01")
    # Sep 2: bell tick missed (bot down) -> only an intraday read survives
    h.snapshot(_status(104.0, day_pl=4.0), basis="intraday", day="2026-09-02")
    h.snapshot(_status(110.0, day_pl=7.0), basis="intraday", day="2026-09-03")
    intraday = h.all()[1]
    assert intraday["day_pl"] == 4.0
    assert "broker_day_pl" not in intraday and "day_pl_basis" not in intraday
    h.snapshot(_status(109.0, day_pl=6.0), basis="close", day="2026-09-03")  # replaces Sep 3 intraday
    rows = h.all()
    assert [r["basis"] for r in rows] == ["close", "intraday", "close"]
    # predecessor is the Sep 1 CLOSE row, not the Sep 2 intraday read
    assert rows[-1]["day_pl"] == 9.0 and rows[-1]["broker_day_pl"] == 6.0
    assert rows[-1]["day_pl_basis"] == "self"
    # legacy writer path (no basis) untouched: broker figure, no new fields
    h.snapshot(_status(50.0, day_pl=-1.0,
                       as_of=datetime(2026, 9, 4, 15, 0, tzinfo=timezone.utc)))
    r = h.all()[-1]
    assert r["day_pl"] == -1.0 and "broker_day_pl" not in r and "basis" not in r


def test_legacy_rows_without_the_new_fields_still_load_and_predecess():
    p = _tmp(".jsonl")
    legacy = [
        {"date": "2026-08-24", "equity": 1_000_110.0, "day_pl": 110.0},   # pre run-6 row
        {"date": "2026-08-25", "equity": 1_006_145.0, "day_pl": 5689.0,
         "basis": "close", "book_beta_spy": 0.9},                          # run-6 close row
        {"date": "2026-08-26", "equity": 1_006_500.0, "day_pl": 355.0, "basis": "intraday"},
    ]
    p.write_text("".join(json.dumps(r) + "\n" for r in legacy), encoding="utf-8")
    h = EquityHistory(path=p)
    rows = h.all()
    assert rows == legacy
    assert h.has_close_row("2026-08-25") and not h.has_close_row("2026-08-24")
    # a run-6 close row (no broker_day_pl / day_pl_basis) is still a valid predecessor
    h.snapshot(_status(1_005_145.0, day_pl=-1_600.0), basis="close", day="2026-08-26")
    rows = h.all()
    assert len(rows) == 3 and rows[-1]["basis"] == "close"
    assert rows[-1]["day_pl"] == -1_000.0 and rows[-1]["broker_day_pl"] == -1_600.0
    assert rows[-1]["day_pl_basis"] == "self"
    assert rows[:2] == legacy[:2]   # older rows are never rewritten


def test_malformed_predecessor_degrades_to_broker_day_pl_never_a_lost_row(caplog):
    # Review finding (run-7 B1): float(prev["equity"]) on a malformed legacy
    # value raised inside snapshot()'s single try -> "equity snapshot failed",
    # nothing written, has_close_row stayed False, every 30 s tick retried into
    # the same failure until midnight -> NO close row -> v3 VOID for the date.
    # (a) a non-numeric predecessor is skipped (an older sound one is used)
    p = _tmp(".jsonl")
    p.write_text(json.dumps({"date": "2026-09-01", "equity": 100.0, "day_pl": 0.0,
                             "basis": "close"}) + "\n"
                 + json.dumps({"date": "2026-09-02", "equity": "n/a", "day_pl": 0.0,
                               "basis": "close"}) + "\n", encoding="utf-8")
    h = EquityHistory(path=p)
    h.snapshot(_status(104.0, day_pl=3.0), basis="close", day="2026-09-03")
    r = h.all()[-1]
    assert r["date"] == "2026-09-03" and r["basis"] == "close"
    assert r["day_pl"] == 4.0 and r["day_pl_basis"] == "self" and r["broker_day_pl"] == 3.0
    assert h.has_close_row("2026-09-03")
    # (b) the stamp itself blowing up degrades the row to the broker figure
    h2 = EquityHistory(path=_tmp(".jsonl"))
    h2.snapshot(_status(100.0, day_pl=0.0), basis="close", day="2026-09-01")
    with patch.object(EquityHistory, "_prior_close_row",
                      side_effect=RuntimeError("corrupt predecessor")), \
            caplog.at_level(logging.WARNING, logger="status"):
        h2.snapshot(_status(105.0, day_pl=7.0), basis="close", day="2026-09-02")
    r = h2.all()[-1]
    assert r["date"] == "2026-09-02" and h2.has_close_row("2026-09-02")
    assert r["day_pl"] == 7.0 and r["broker_day_pl"] == 7.0 and r["day_pl_basis"] == "broker"
    assert any("keeps the broker figure" in rec.getMessage() and "predecessor unusable"
               in rec.getMessage() for rec in caplog.records)
    assert not any("equity snapshot failed" in rec.getMessage() for rec in caplog.records)


def test_closing_snapshot_second_session_telescopes_from_prior_close_row():
    o = _orch_with_history()
    o.cfg.equity_close_fixed_stamp = True
    o._refresh_closing_snapshot(now_et=datetime(2026, 9, 1, 16, 1, tzinfo=_ET))
    # next session: Alpaca restated last_equity overnight -> broker day_pl
    # 4,852.78 while our own close-to-close delta is 5,629.55
    o.broker.get_account = lambda: SimpleNamespace(
        positions=[], equity=1_005_629.55, cash=1.0, buying_power=1.0,
        day_pl=4_852.78, day_pl_pct=0.48, pattern_day_trader=False, daytrade_count=0)
    o._refresh_closing_snapshot(now_et=datetime(2026, 9, 2, 16, 1, tzinfo=_ET))
    rows = o.equity_history.all()
    assert [r["date"] for r in rows] == ["2026-09-01", "2026-09-02"]
    assert rows[0]["day_pl_basis"] == "broker" and rows[0]["day_pl"] == 0.0
    assert rows[1]["day_pl"] == 5_629.55 and rows[1]["broker_day_pl"] == 4_852.78
    assert rows[1]["day_pl_basis"] == "self"


# ---------------------------------------------------------------- (c) -------

_PY = sys.executable
_SCRIPT = os.path.join(ROOT, "scripts", "eval_contract_check.py")


def _run_checker(equity_rows: list[dict], extra: list[str] | None = None) -> str:
    trades = _tmp(".jsonl")
    trades.write_text("", encoding="utf-8")
    eq = _tmp(".jsonl")
    eq.write_text("".join(json.dumps(r) + "\n" for r in equity_rows), encoding="utf-8")
    cmd = [_PY, _SCRIPT, "--start", "2026-08-24", "--end", "2026-08-28",
           "--trades", str(trades), "--equity", str(eq)] + (extra or [])
    return subprocess.run(cmd, capture_output=True, text=True).stdout


def test_eval_checker_telescoping_pass_and_basis_line():
    out = _run_checker([
        {"date": "2026-08-24", "equity": 1_000_456.0, "day_pl": 456.0, "basis": "close"},
        {"date": "2026-08-25", "equity": 1_006_145.0, "day_pl": 5689.0, "basis": "close"},
        {"date": "2026-08-26", "equity": 1_005_145.0, "day_pl": -1000.0, "basis": "close"},
    ])
    assert "equity basis: close=3 row(s)" in out
    assert "max per-day gap |Δequity - day_pl|: $0.00" in out and "-> PASS" in out
    assert "sum day_pl: $4,689.00" in out and "equity diff (first->last row): $4,689.00" in out


def test_eval_checker_telescoping_fail_on_restamped_legacy_rows():
    out = _run_checker([
        {"date": "2026-08-24", "equity": 1_000_110.0, "day_pl": 110.0},
        {"date": "2026-08-25", "equity": 1_007_879.0, "day_pl": 7423.0},
    ])
    assert "equity basis: legacy=2 row(s)" in out
    assert "-> FAIL" in out and "$346.00 on 2026-08-25" in out
    assert "VERDICT: NO-GO" in out   # pass rules unchanged: N=0 < 24


def test_eval_checker_bench_csv_prints_iwm_and_qqq():
    iwm = _tmp(".csv")
    iwm.write_text("2026-08-24,220\n2026-08-25,222\n2026-08-26,221\n")
    out = _run_checker([
        {"date": "2026-08-24", "equity": 100.0, "day_pl": 0.0, "basis": "close"},
        {"date": "2026-08-25", "equity": 101.0, "day_pl": 1.0, "basis": "close"},
        {"date": "2026-08-26", "equity": 100.5, "day_pl": -0.5, "basis": "close"},
    ], ["--bench-csv", f"IWM={iwm}", "--bench-csv", f"QQQ={iwm}"])
    assert "capture vs IWM (informational, not judged)" in out
    assert "capture vs QQQ (informational, not judged)" in out
    assert "IWM days in window: 1 up / 1 down" in out


def test_eval_checker_selftest_still_passes():
    r = subprocess.run([_PY, _SCRIPT, "--selftest"], capture_output=True, text=True)
    assert r.returncode == 0 and "SELFTEST PASS" in r.stdout


# ---------------------------------------------------------------- (d) -------

def test_dashboard_price_symbols_skip_occ_contracts():
    from investment_strategy.dashboard import price_symbols
    assert price_symbols({"AAPL", PUT, "NVDA", CALL_LO, "", "BRK.B"}) == [
        "AAPL", "BRK.B", "NVDA"]


# ---------------------------------------------------------------- (e) -------

def test_ledger_set_fill_stamps_only_the_matching_row():
    led = TradeLedger(path=_tmp(".jsonl"))
    led.record(_rec(symbol="AAPL", action="buy", qty=10.0, entry_price=100.0,
                    cost_usd=1000.0, order_id="oid-1"))
    led.record(_rec(symbol="MSFT", action="buy", qty=5.0, entry_price=50.0,
                    cost_usd=250.0, order_id="oid-2"))
    ts = datetime(2026, 8, 25, 13, 31, tzinfo=timezone.utc)
    assert led.set_fill("oid-1", 100.37, 10.0, ts) is True
    assert led.set_fill("oid-missing", 1.0, 1.0) is False
    assert led.set_fill("oid-2", 0.0, 5.0) is False           # no price -> no-op
    rows = {r.order_id: r for r in led.effective()}
    assert rows["oid-1"].fill_price == 100.37 and rows["oid-1"].fill_qty == 10.0
    assert rows["oid-1"].fill_ts == ts
    assert rows["oid-1"].entry_price == 100.0                 # decision quote kept
    assert rows["oid-2"].fill_price is None
    assert len(led.all()) == 2                                # in place, no new rows


def _reconcile_msgs(o) -> list[str]:
    """Run _reconcile_fills and return the orchestrator log messages (INFO
    lines included — the logger's effective level is WARNING under pytest)."""
    from test_orchestrator import _LogCapture
    cap = _LogCapture()
    lg = logging.getLogger("orchestrator")
    old_level = lg.level
    lg.addHandler(cap)
    lg.setLevel(logging.INFO)
    try:
        o._reconcile_fills()
    finally:
        lg.removeHandler(cap)
        lg.setLevel(old_level)
    return [r.getMessage() for r in cap.records]


def test_reconcile_filled_order_records_fill_price_and_logs_it():
    from test_orchestrator import _orch
    o = _orch()
    o.cfg.ledger_fill_prices = True
    o.ledger = TradeLedger(path=_tmp(".jsonl"))
    o.ledger.record(_rec(symbol="AAPL", action="buy", qty=3.0, entry_price=100.0,
                         cost_usd=300.0, order_id="oid-1"))
    o.broker.order_fill = lambda oid: ("filled", 3.0, 3.0)
    o.broker.order_fill_detail = lambda oid: {
        "price": 100.42, "qty": 3.0,
        "filled_at": datetime(2026, 8, 25, 13, 31, tzinfo=timezone.utc)}
    o._pending_oids = [("oid-1", "AAPL")]
    msgs = _reconcile_msgs(o)
    assert any("FILLED (3/3) @ 100.4200 x 3 [ledger stamped]" in m for m in msgs), msgs
    row = o.ledger.effective()[0]
    assert row.fill_price == 100.42 and row.fill_qty == 3.0
    assert row.fill_ts == datetime(2026, 8, 25, 13, 31, tzinfo=timezone.utc)
    assert o._pending_oids == []


def test_reconcile_fill_stamp_off_or_unavailable_keeps_plain_line():
    from test_orchestrator import _orch
    for knob, has_detail in ((False, True), (True, False)):
        o = _orch()
        o.cfg.ledger_fill_prices = knob
        if has_detail:
            o.broker.order_fill_detail = lambda oid: {"price": 1.0, "qty": 1.0}
        o.broker.order_fill = lambda oid: ("filled", 3.0, 3.0)
        o._pending_oids = [("oid-1", "AAPL")]
        msgs = _reconcile_msgs(o)
        assert any(m.endswith("FILLED (3/3).") for m in msgs), msgs
        assert not any("ledger stamped" in m for m in msgs)


def test_from_option_records_leg_sides():
    from investment_strategy.models import (
        Action, OptionLeg, OptionStrategy, RiskDecision, RiskVerdict, TradeProposal,
    )
    p = TradeProposal(
        symbol="NVDA", action=Action.BUY, conviction=0.6, target_weight_pct=2.0,
        rationale="r",
        instrument="option", option_strategy=OptionStrategy.BULL_CALL_SPREAD,
        option_legs=[
            OptionLeg(expiry="2026-09-18", strike=180.0, right="call", side=Action.BUY),
            OptionLeg(expiry="2026-09-18", strike=190.0, right="call", side=Action.SELL),
        ],
    )
    d = RiskDecision(proposal=p, verdict=RiskVerdict.APPROVED, approved_qty=5,
                     approved_notional=2000.0, reason="ok")
    rec = TradeRecord.from_option(d, 4.0, "oid-o")
    assert rec.occ_symbols == [CALL_LO, CALL_HI]
    assert rec.occ_sides == ["buy", "sell"]
    assert pm_mod._option_leg_signs(rec) == [(CALL_LO, 1.0), (CALL_HI, -1.0)]


# ---- run-7 B2: equity SELL rows restated at the broker's fill --------------
# The 7 priced closed run-6 rows (state/trades.jsonl, Sep 4-10): exit_price
# is the submission-time quote, fill_price the broker's filled_avg_price.
# The other 7 closed rows (exchange bracket exits) had fill_price=None.
_RUN6_PRICED = [  # sym, exit_reason, qty, exit_price, fill_price, realized_pl, pct
    ("MKL", "decision", 9.0, 1836.39, 1835.62, 108.9, 0.663),
    ("F", "decision", 2140.0, 14.1529, 14.15, 348.606, 1.164),
    ("BE", "trail", 299.0, 276.28, 277.0338, 12525.11, 17.872),
    ("NU", "trail", 2061.0, 15.045, 15.0394, 1076.924025, 3.598),
    ("NVDA", "trail", 370.0, 223.9401, 223.9, 2893.437, 3.618),
    ("PSQ", "hedge_unwind", 10283.17942229, 25.85, 25.84, 41.759991, 0.016),
    ("QQQ", "core_defense", 54.0, 709.23, 709.03, 27.56, 0.072),
]
_RUN6_BRACKET = [  # sym, exit_reason, qty, exit_price (== the fill), realized_pl
    ("BE", "bracket_take", 83.0, 272.13, 5182.105),
    ("INTC", "bracket_take", 462.0, 105.89, 8930.46),
    ("BLK", "bracket_stop", 18.0, 1082.48, -835.92),
    ("RIG", "bracket_stop", 1419.0, 5.72, -688.215),
    ("AAPL", "bracket_stop", 64.0, 310.65375, -923.92),
    ("ABT", "bracket_stop", 192.0, 104.574427, -951.47),
    ("SNXX", "bracket_stop", 425.0, 16.22193, -772.67975),
]


def _run6_ledger() -> TradeLedger:
    led = TradeLedger(path=_tmp(".jsonl"), restate_at_fill=True)
    for sym, why, qty, px, _fill, pl, pct in _RUN6_PRICED:
        led.record(_rec(symbol=sym, action="sell", qty=qty, exit_price=px,
                        realized_pl=pl, realized_pl_pct=pct, exit_reason=why,
                        order_id=f"oid-{sym}-{why}"))
    for sym, why, qty, px, pl in _RUN6_BRACKET:
        led.record(_rec(symbol=sym, action="sell", qty=qty, exit_price=px,
                        realized_pl=pl, realized_pl_pct=pl / (px * qty - pl) * 100,
                        exit_reason=why, order_id=f"oid-{sym}-{why}"))
    return led


def test_run6_priced_sell_rows_restate_to_the_fill_sum():
    led = _run6_ledger()
    assert round(sum(r.realized_pl for r in led.effective()), 2) == 26_962.66
    for sym, why, qty, _px, fill, _pl, _pct in _RUN6_PRICED:
        assert led.set_fill(f"oid-{sym}-{why}", fill, qty) is True
    rows = {r.order_id: r for r in led.effective()}
    # fill-restated sum = ledger sum + $72.24 (the analyst's table D, to the cent)
    assert round(sum(r.realized_pl for r in rows.values()), 2) == 27_034.90
    assert round(sum(r.realized_pl - r.quote_realized_pl
                     for r in rows.values() if r.quote_realized_pl is not None), 2) == 72.24
    deltas = {sym: round(rows[f"oid-{sym}-{why}"].realized_pl
                         - rows[f"oid-{sym}-{why}"].quote_realized_pl, 2)
              for sym, why, *_ in _RUN6_PRICED}
    assert deltas == {"MKL": -6.93, "F": -6.21, "BE": 225.39, "NU": -11.54,
                      "NVDA": -14.84, "PSQ": -102.83, "QQQ": -10.80}
    psq = rows["oid-PSQ-hedge_unwind"]
    assert round(psq.realized_pl, 2) == -61.07 and psq.quote_realized_pl == 41.759991
    assert psq.exit_price == 25.84 and psq.quote_exit_price == 25.85
    # sum of the quote figures reproduces the run-6 ledger exactly
    assert round(sum(r.quote_realized_pl if r.quote_realized_pl is not None
                     else r.realized_pl for r in rows.values()), 2) == 26_962.66
    # the unstamped bracket rows are untouched
    for sym, why, _qty, px, pl in _RUN6_BRACKET:
        r = rows[f"oid-{sym}-{why}"]
        assert r.exit_price == px and r.realized_pl == pl and r.fill_price is None
        assert r.quote_exit_price is None


def _ledger_msgs(fn) -> list[str]:
    """Run fn() and return the 'ledger' logger's messages (INFO included)."""
    from test_orchestrator import _LogCapture
    cap = _LogCapture()
    lg = logging.getLogger("ledger")
    old_level = lg.level
    lg.addHandler(cap)
    lg.setLevel(logging.INFO)
    try:
        fn()
    finally:
        lg.removeHandler(cap)
        lg.setLevel(old_level)
    return [r.getMessage() for r in cap.records]


def test_backfill_style_row_recorded_from_the_fill_is_stamped_not_restated():
    """An exchange-backfill SELL row is recorded FROM the broker fill, so a
    later set_fill at the same price is a numeric no-op: quote_* == fill and
    the log says so (no RESTATED line)."""
    led = TradeLedger(path=_tmp(".jsonl"), restate_at_fill=True)
    led.record(_rec(symbol="INTC", action="sell", qty=462.0, exit_price=105.89,
                    realized_pl=8930.46, realized_pl_pct=22.331,
                    exit_reason="bracket_take", order_id="oid-1"))
    msgs = _ledger_msgs(lambda: led.set_fill("oid-1", 105.89, 462.0))
    row = led.effective()[0]
    assert row.fill_price == 105.89 and row.exit_price == 105.89
    assert row.realized_pl == 8930.46 and row.quote_realized_pl == 8930.46
    assert row.quote_exit_price == 105.89
    assert any("fill 105.8900 matches recorded exit_price on SELL INTC" in m
               for m in msgs), msgs
    assert not any("RESTATED" in m for m in msgs)


# ---- run-7 A6: exchange-backfilled bracket exits carry the fill they were
# recorded from (analyst C8: 7/14 closed run-6 rows had fill_price=null) ----
def _backfill_msgs(o) -> list[str]:
    """Run _backfill_exchange_exits and return the orchestrator's
    'Backfilled exchange exit' log lines (INFO; WARNING-level under pytest)."""
    from test_orchestrator import _LogCapture
    cap = _LogCapture()
    lg = logging.getLogger("orchestrator")
    old_level = lg.level
    lg.addHandler(cap)
    lg.setLevel(logging.INFO)
    try:
        o._backfill_exchange_exits()
    finally:
        lg.removeHandler(cap)
        lg.setLevel(old_level)
    return [r.getMessage() for r in cap.records
            if r.getMessage().startswith("Backfilled exchange exit")]


def test_run6_bracket_exits_backfilled_with_fill_fields_stamped():
    """Replay run-6's 7 exchange bracket exits (half the closed sample; all
    ledgered with fill_price=null in the window) through the REAL backfill
    into a file-backed ledger: every row carries fill_price == the broker's
    filled_avg_price (== exit_price), fill_qty == filled_qty, fill_ts ==
    filled_at, and realized_pl computed AT that fill — so the contract-v3
    '100% of closed rows carry fill_price' validity line holds and the B2
    set_fill at the same fill is a numeric no-op (nothing to restate)."""
    from datetime import timedelta
    from test_orchestrator import _backfill_orch, _closed
    fill_ts = datetime(2026, 9, 8, 14, 31, 7, tzinfo=timezone.utc)
    buys, closed = [], []
    for i, (sym, why, qty, px, pl) in enumerate(_RUN6_BRACKET):
        basis = px - pl / qty                     # the FIFO lot the exit sold
        buys.append(_rec(symbol=sym, action="buy", qty=qty, entry_price=basis,
                         cost_usd=basis * qty, order_id=f"buy-{sym}"))
        closed.append(_closed(
            f"leg-{sym}", symbol=sym, qty=qty, price=px,
            otype="limit" if why == "bracket_take" else "stop",
            filled_at=(fill_ts + timedelta(minutes=i)).isoformat()))
    o = _backfill_orch(closed)
    o.ledger = TradeLedger(path=_tmp(".jsonl"), restate_at_fill=True)
    for b in buys:
        o.ledger.record(b)
    msgs = _backfill_msgs(o)
    sells = {r.symbol: r for r in o.ledger.effective() if r.action == "sell"}
    assert len(sells) == 7 and len(msgs) == 7
    assert all("[fill stamped 2026-09-08T14:" in m for m in msgs), msgs
    for i, (sym, why, qty, px, pl) in enumerate(_RUN6_BRACKET):
        r = sells[sym]
        assert r.exit_reason == why and r.order_id == f"leg-{sym}"
        assert r.fill_price == px == r.exit_price      # == filled_avg_price
        assert r.fill_qty == qty                       # == filled_qty
        assert r.fill_ts == fill_ts + timedelta(minutes=i) == r.ts
        assert abs(r.realized_pl - pl) < 1e-6          # realized AT the fill
        assert r.quote_exit_price is None              # never restated
    assert round(sum(r.realized_pl for r in sells.values()), 2) == round(
        sum(pl for *_, pl in _RUN6_BRACKET), 2)
    assert all(r.fill_price is not None for r in sells.values())  # 7/7, not 0/7
    # The reconcile path (B2) re-stamping one at the same fill changes nothing.
    led_msgs = _ledger_msgs(
        lambda: o.ledger.set_fill("leg-INTC", 105.89, 462.0, fill_ts))
    intc = [r for r in o.ledger.effective()
            if r.action == "sell" and r.symbol == "INTC"][0]
    assert abs(intc.realized_pl - 8930.46) < 1e-6 and intc.fill_price == 105.89
    assert intc.quote_exit_price == 105.89
    assert not any("RESTATED" in m for m in led_msgs), led_msgs


def test_reconcile_filled_sell_restates_the_ledger_row_and_logs_it():
    """The orchestrator's FILLED reconcile -> _stamp_fill -> set_fill path on
    a SELL row: the PSQ Sep 9 hedge_unwind, +$41.76 at the quote, -$61.07 at
    the fill. orchestrator.py is not edited by this item — the ledger does it."""
    from test_orchestrator import _orch
    o = _orch()
    o.cfg.ledger_fill_prices = True
    o.ledger = TradeLedger(path=_tmp(".jsonl"), restate_at_fill=True)
    qty = 10283.17942229
    o.ledger.record(_rec(symbol="PSQ", action="sell", qty=qty, exit_price=25.85,
                         realized_pl=41.759991, realized_pl_pct=0.016,
                         exit_reason="hedge_unwind", order_id="oid-1"))
    ts = datetime(2026, 9, 9, 14, 22, 45, tzinfo=timezone.utc)
    o.broker.order_fill_full = lambda oid: (
        "filled", qty, qty, {"price": 25.84, "qty": qty, "filled_at": ts})
    o._pending_oids = [("oid-1", "PSQ")]
    orch_msgs: list[str] = []
    led_msgs = _ledger_msgs(lambda: orch_msgs.extend(_reconcile_msgs(o)))
    assert any("FILLED" in m and "@ 25.8400" in m and "[ledger stamped]" in m
               for m in orch_msgs), orch_msgs
    assert any(m.startswith("Ledger: RESTATED SELL PSQ at fill 25.8400 (quote 25.8500): "
                            "realized $41.76 -> $-61.07 (-102.83) [order oid-1]")
               for m in led_msgs), led_msgs
    row = o.ledger.effective()[0]
    assert row.fill_price == 25.84 and row.fill_ts == ts
    assert round(row.realized_pl, 2) == -61.07 and row.quote_realized_pl == 41.759991
    assert row.exit_price == 25.84 and row.quote_exit_price == 25.85
    assert o._pending_oids == []
