"""Run-6 item 1 — measurement plumbing (no strategy change).

(a) post-mortem: open option groups are marked close-to-close (or reported
    UNMARKED explicitly), core_fill rows are excluded, the name count no
    longer counts the summary line, read_curated honours max_lines
(b) equity_history: one fixed basis='close' row per ET day, never overwritten
(c) eval_contract_check: telescoping + basis + extra benchmarks
(d) dashboard: no latest_price() on OCC symbols
(e) fill prices stamped onto the ledger row at FILLED confirmation
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
