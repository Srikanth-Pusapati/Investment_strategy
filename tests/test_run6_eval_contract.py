"""Run-6 item 8: eval contract v2 checker, clean-window reset, book-beta close row."""
from __future__ import annotations

import importlib.util
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from investment_strategy.config import Config, load_config
from investment_strategy.reset import _churn_carryover, reset_local_state
from investment_strategy.status import AccountStatus, EquityHistory

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "eval_contract_check", ROOT / "scripts" / "eval_contract_check.py")
ecc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ecc)


# --------------------------------------------------------------- fixtures ---

def _write(path: Path, rows) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def _synthetic_window(tmp: Path, n_trades: int = 10, sessions: int = 25,
                      bot_mult: float = 1.0, stamped_beta: float | None = None,
                      dirty_sell: bool = False):
    """Ledger + equity + spy csv over `sessions` consecutive dates; SPY
    alternates +1%/-1%, the book moves bot_mult x SPY on up days and
    0.9 x bot_mult x SPY on down days (beta 0.95 x bot_mult, up-capture
    > down-capture so the capture rule can pass)."""
    dates = [f"2026-09-{i:02d}" for i in range(1, sessions + 1)]
    spy, eq = 100.0, 1_000_000.0
    spy_rows, eq_rows = [f"{dates[0]},{spy}"], []
    row0 = {"date": dates[0], "equity": eq, "day_pl": 0.0, "basis": "close"}
    if stamped_beta is not None:
        row0["book_beta_spy"] = stamped_beta
    eq_rows.append(row0)
    for i, d in enumerate(dates[1:]):
        r = 0.01 if i % 2 == 0 else -0.01
        spy *= 1 + r
        pl = eq * r * bot_mult * (1.0 if r > 0 else 0.9)
        eq += pl
        spy_rows.append(f"{d},{spy}")
        row = {"date": d, "equity": eq, "day_pl": pl, "basis": "close"}
        if stamped_beta is not None:
            row["book_beta_spy"] = stamped_beta
        eq_rows.append(row)
    trades = []
    for i in range(n_trades):
        trades.append({"ts": f"{dates[i % sessions]}T14:00:00Z", "symbol": f"S{i}",
                       "action": "sell", "exit_reason": "trail",
                       "realized_pl": 100.0 + 10 * (i % 3)})
    if dirty_sell:
        trades.append({"ts": f"{dates[0]}T13:00:00Z", "symbol": "DRT", "action": "buy",
                       "stop_loss_pct": 6.0})
        trades.append({"ts": f"{dates[1]}T14:00:00Z", "symbol": "DRT", "action": "sell",
                       "exit_reason": "decision", "realized_pl_pct": -1.0,
                       "realized_pl": -50.0})
    t = _write(tmp / "trades.jsonl", trades)
    e = _write(tmp / "equity_history.jsonl", eq_rows)
    s = tmp / "spy.csv"
    s.write_text("date,close\n" + "\n".join(spy_rows) + "\n", encoding="utf-8")
    return dates[0], dates[-1], t, e, s


# ------------------------------------------------------------ checker v2 ---

def test_selftest_still_passes():
    assert ecc.selftest() == 0


def test_v1_rules_unchanged_and_default(capsys, tmp_path):
    start, end, t, e, s = _synthetic_window(tmp_path, n_trades=10, sessions=6)
    rc = ecc.main(["--start", start, "--end", end, "--trades", str(t),
                   "--equity", str(e), "--spy-csv", str(s)])
    out = capsys.readouterr().out
    assert rc == ecc.EXIT_NO_GO                    # N=10 < 24 fails v1
    assert "contract v1" in out and "[FAIL] closed trades N >= 24" in out
    assert "need >=6 up AND >=6 down" in out       # v1 capture thresholds


def test_v2_pending_until_pooled_60(capsys, tmp_path):
    start, end, t, e, s = _synthetic_window(tmp_path, n_trades=10, sessions=25)
    rc = ecc.main(["--contract", "v2", "--start", start, "--end", end,
                   "--trades", str(t), "--equity", str(e), "--spy-csv", str(s)])
    out = capsys.readouterr().out
    assert rc == ecc.EXIT_PENDING
    assert "[INFO] sample floor N >= 24: N=10 (NOT met" in out
    assert "[PEND] pooled expectancy: pooled N=10 < 60" in out
    assert "[PASS] realized beta within +/-0.2 of 1.00  (beta=0.95" in out
    assert "alpha/day=+0.050%" in out and "sessions=24" in out
    assert "[PASS] up-capture > down-capture  (up 100.0% vs down 90.0%)" in out
    assert "VERDICT: PENDING" in out


def test_v2_pooling_decides_and_alpha_block(capsys, tmp_path):
    start, end, t, e, s = _synthetic_window(tmp_path, n_trades=10, sessions=25)
    prior = _write(tmp_path / "prior.jsonl", [
        {"ts": f"2026-07-{1 + i % 28:02d}T14:00:00Z", "symbol": f"P{i}",
         "realized_pl": 200.0 + (i % 7) * 10} for i in range(55)])
    rc = ecc.main(["--contract", "v2", "--start", start, "--end", end,
                   "--trades", str(t), "--equity", str(e), "--spy-csv", str(s),
                   "--pool", str(prior), "--beta-target", "1.0"])
    out = capsys.readouterr().out
    assert rc == ecc.EXIT_GO, out
    assert "POOLED: N=65 (2 window(s))" in out and "-> DECIDES" in out
    assert "[PASS] pooled expectancy > 0 @ 95% one-sided  (pooled N=65" in out
    assert "bootstrap95=[" in out
    assert "VERDICT: GO" in out


def test_v2_beta_out_of_band_fails_and_beta_adjusted_capture(capsys, tmp_path):
    # book = 1.5x SPY up / 1.35x down, stamped ex-ante beta 1.5 -> adjusted 100/90
    start, end, t, e, s = _synthetic_window(tmp_path, n_trades=10, sessions=25,
                                            bot_mult=1.5, stamped_beta=1.5)
    rc = ecc.main(["--contract", "v2", "--start", start, "--end", end,
                   "--trades", str(t), "--equity", str(e), "--spy-csv", str(s),
                   "--bench-csv", f"QQQ={s}"])
    out = capsys.readouterr().out
    assert rc == ecc.EXIT_NO_GO
    assert "[FAIL] realized beta within +/-0.2 of 1.00  (beta=1.4" in out
    assert "ex-ante beta source: 25 stamped close row(s)" in out
    assert "SPY: up-capture 100.0%   down-capture 90.0%   (12 up / 12 down, 24 stamped-beta days)" in out
    assert "QQQ: up-capture 100.0%" in out
    assert "--- (4b) capture vs QQQ" in out
    assert "equity basis: close=25 row(s)" in out


def test_v2_decision_sell_validity_fails(capsys, tmp_path):
    start, end, t, e, s = _synthetic_window(tmp_path, n_trades=10, sessions=25,
                                            dirty_sell=True)
    rc = ecc.main(["--contract", "v2", "--start", start, "--end", end,
                   "--trades", str(t), "--equity", str(e), "--spy-csv", str(s)])
    out = capsys.readouterr().out
    assert rc == ecc.EXIT_NO_GO
    assert "[FAIL] zero decision-sell losses below 0.5x stop  (count=1" in out
    assert "DRT realized -1.00% vs stop 6.00% (0.17x)" in out


def test_beta_json_fallback_and_telescoping(capsys, tmp_path):
    start, end, t, e, s = _synthetic_window(tmp_path, n_trades=3, sessions=6)
    bj = tmp_path / "risk_state.json"
    bj.write_text(json.dumps({"book_beta": {"spy": 1.25}}), encoding="utf-8")
    rc = ecc.main(["--contract", "v2", "--start", start, "--end", end,
                   "--trades", str(t), "--equity", str(e), "--spy-csv", str(s),
                   "--beta-json", str(bj)])
    out = capsys.readouterr().out
    assert "fallback beta=1.25" in out
    assert "-> PASS (limit $1.00)" in out         # synthetic rows telescope exactly
    assert rc in (ecc.EXIT_PENDING, ecc.EXIT_NO_GO)


def test_ols_and_bootstrap_units():
    days = [(f"2026-09-{i:02d}", 100.0 * (1 + 0.002 * i), None) for i in range(1, 11)]
    spy = {f"2026-09-{i:02d}": 50.0 * (1 + 0.001 * i) for i in range(1, 11)}
    o = ecc.ols_alpha_beta(days, spy)
    assert o["n"] == 9 and o["beta"] is not None
    lo, hi = ecc.bootstrap_ci_mean([1.0, 2.0, 3.0, 4.0, 5.0])
    assert lo <= 3.0 <= hi and ecc.bootstrap_ci_mean([1.0]) is None


# -------------------------------------------------------- reset carry off ---

def _old_state(tmp: Path) -> Path:
    p = tmp / "risk_state.json"
    p.write_text(json.dumps({
        "entry_times": {"NU": "2026-08-28T13:00:00+00:00"},
        "exit_times": {"OLD": "2026-08-27T15:00:00+00:00"},
        "exit_prices": {"OLD": 14.17},
        "loss_streaks": {"NU": 1},
    }), encoding="utf-8")
    return p


def test_reset_carry_churn_default_off_in_config():
    assert Config.__dataclass_fields__["reset_carry_churn"].default is False
    for k in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ANTHROPIC_API_KEY"):
        os.environ.setdefault(k, "x")
    os.environ.pop("RESET_CARRY_CHURN", None)
    assert load_config().reset_carry_churn is False
    os.environ["RESET_CARRY_CHURN"] = "on"
    try:
        assert load_config().reset_carry_churn is True
    finally:
        os.environ.pop("RESET_CARRY_CHURN", None)


def test_churn_carryover_off_is_empty_and_on_carries(tmp_path):
    state = _old_state(tmp_path)
    off = SimpleNamespace(state_file=str(state), core_etf="QQQ", reset_carry_churn=False)
    assert _churn_carryover(off) == {}
    on = SimpleNamespace(state_file=str(state), core_etf="QQQ", reset_carry_churn=True)
    carry = _churn_carryover(on)
    assert carry["exit_prices"] == {"OLD": 14.17} and "NU" in carry["exit_times"]
    # legacy cfg objects without the attribute are treated as OFF
    assert _churn_carryover(SimpleNamespace(state_file=str(state))) == {}


def test_reset_local_state_clean_mode_leaves_no_state_and_says_so(tmp_path):
    state = _old_state(tmp_path)
    cfg = SimpleNamespace(state_file=str(state), core_etf="QQQ",
                          dashboard_file="", reset_carry_churn=False)
    notes = reset_local_state(cfg, archive=False)
    assert any("churn carry: OFF (clean state" in n for n in notes)
    assert not any("carried churn memory" in n for n in notes)
    assert not state.exists()


def test_reset_local_state_carry_mode_reseeds(tmp_path):
    state = _old_state(tmp_path)
    cfg = SimpleNamespace(state_file=str(state), core_etf="QQQ",
                          dashboard_file="", reset_carry_churn=True)
    notes = reset_local_state(cfg, archive=False)
    assert any("churn carry: ON" in n for n in notes)
    assert any("carried churn memory" in n for n in notes)
    seeded = json.loads(state.read_text(encoding="utf-8"))
    assert seeded["exit_prices"] == {"OLD": 14.17}


# ---------------------------------------------------- book beta on close ---

def test_snapshot_extra_field_lands_on_row(tmp_path):
    from datetime import datetime, timezone
    eh = EquityHistory(path=tmp_path / "eq.jsonl")
    st = AccountStatus(as_of=datetime(2026, 8, 31, 20, 5, tzinfo=timezone.utc),
                       equity=1_000_000.0, cash=500_000.0, buying_power=1.0,
                       n_positions=0, unrealized_pl=0.0, day_pl=0.0, day_pl_pct=0.0)
    eh.snapshot(st, basis="close", day="2026-08-31", extra={"book_beta_spy": 1.21})
    rows = eh.all()
    assert rows[0]["basis"] == "close" and rows[0]["book_beta_spy"] == 1.21
    # extra never overrides a core field
    eh.snapshot(st, basis="close", day="2026-09-01", extra={"equity": -1})
    assert [r["equity"] for r in eh.all()] == [1_000_000.0, 1_000_000.0]


def test_closing_snapshot_carries_book_beta(tmp_path, monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from test_orchestrator import _orch
    o = _orch()
    o.cfg.equity_close_fixed_stamp = True
    o.equity_history = EquityHistory(path=tmp_path / "eq.jsonl")
    o.broker.get_account = lambda: SimpleNamespace(
        positions=[], equity=1_000_500.0, cash=600_000.0, buying_power=1.0,
        day_pl=500.0, day_pl_pct=0.05, pattern_day_trader=False, daytrade_count=0)
    o.broker.portfolio_basis = lambda: None
    o._book_beta_reading = SimpleNamespace(spy=1.31)
    o._refresh_closing_snapshot(now_et=datetime(2026, 8, 31, 16, 5,
                                                tzinfo=ZoneInfo("America/New_York")))
    rows = o.equity_history.all()
    assert rows[0]["date"] == "2026-08-31" and rows[0]["book_beta_spy"] == 1.31


# ------------------------------------------------------------ checker v3 ---
# Run-7 item B3: --contract v3 (runs/pre-final-test-run-7/EVAL_CONTRACT.md).
# These mirror the v3 blocks of scripts/eval_contract_check.py --selftest
# and pin the run-6 regression numbers the analysts recomputed by hand on
# 2026-09-11 (satellite N=12 / mean $2,241.11 / t 1.750; 7 pairs incl. Sep 1;
# alpha +0.484%/day; worst day Sep 9 on delta-equity; VOID on the intraday row).

_RUN6_EQUITY = [
    {"date": "2026-08-31", "equity": 1000000.0, "day_pl": 0.0, "basis": "late"},
    {"date": "2026-09-01", "equity": 1000943.64, "day_pl": 943.64, "basis": "close", "book_beta_spy": 0.7698304838586011},
    {"date": "2026-09-02", "equity": 1005629.55, "day_pl": 5462.68, "basis": "close", "book_beta_spy": 1.0411954956599345},
    {"date": "2026-09-03", "equity": 1016452.05, "day_pl": 9401.01, "basis": "close", "book_beta_spy": 1.1080876854986772},
    {"date": "2026-09-04", "equity": 1025915.86, "day_pl": 8318.03, "basis": "close", "book_beta_spy": 1.0769125795493069},
    {"date": "2026-09-08", "equity": 1042978.24, "day_pl": 15645.11, "basis": "close", "book_beta_spy": 0.9228747304205474},
    {"date": "2026-09-09", "equity": 1035532.13, "day_pl": -7641.31, "basis": "close", "book_beta_spy": 0.991604989111895},
    {"date": "2026-09-10", "equity": 1028264.64, "day_pl": -7830.19, "basis": "close", "book_beta_spy": 0.89271105188726},
    {"date": "2026-09-11", "equity": 1034842.49, "day_pl": 7490.42, "basis": "intraday"},
]
_RUN6_SPY = ["date,close", "2026-08-31,767.05", "2026-09-01,761.78", "2026-09-02,765.16",
             "2026-09-03,773.17", "2026-09-04,770.19", "2026-09-08,765.96",
             "2026-09-09,762.4", "2026-09-10,757.83", "2026-09-11,764.29"]
_RUN6_TRADES = [
    {"ts": "2026-09-04T15:14:00Z", "symbol": "MKL", "action": "sell", "exit_reason": "decision", "realized_pl": 108.9},
    {"ts": "2026-09-08T14:00:00Z", "symbol": "BE", "action": "sell", "exit_reason": "bracket_take", "realized_pl": 5182.105000000002},
    {"ts": "2026-09-08T14:30:00Z", "symbol": "F", "action": "sell", "exit_reason": "decision", "realized_pl": 348.606},
    {"ts": "2026-09-08T15:00:00Z", "symbol": "INTC", "action": "sell", "exit_reason": "bracket_take", "realized_pl": 8930.46},
    {"ts": "2026-09-09T14:00:00Z", "symbol": "BE", "action": "sell", "exit_reason": "trail", "realized_pl": 12525.11},
    {"ts": "2026-09-09T14:10:00Z", "symbol": "NU", "action": "sell", "exit_reason": "trail", "realized_pl": 1076.924025},
    {"ts": "2026-09-09T14:15:00Z", "symbol": "BLK", "action": "sell", "exit_reason": "bracket_stop", "realized_pl": -835.920000000001},
    {"ts": "2026-09-09T14:20:00Z", "symbol": "NVDA", "action": "sell", "exit_reason": "trail", "realized_pl": 2893.437},
    {"ts": "2026-09-09T14:22:00Z", "symbol": "PSQ", "action": "sell", "exit_reason": "hedge_unwind", "realized_pl": 41.759991},
    {"ts": "2026-09-09T15:00:00Z", "symbol": "RIG", "action": "sell", "exit_reason": "bracket_stop", "realized_pl": -688.2150000000005},
    {"ts": "2026-09-09T16:00:00Z", "symbol": "AAPL", "action": "sell", "exit_reason": "bracket_stop", "realized_pl": -923.9199999999983},
    {"ts": "2026-09-10T14:00:00Z", "symbol": "ABT", "action": "sell", "exit_reason": "bracket_stop", "realized_pl": -951.4700160000029},
    {"ts": "2026-09-10T15:00:00Z", "symbol": "SNXX", "action": "sell", "exit_reason": "bracket_stop", "realized_pl": -772.6797499999994},
    {"ts": "2026-09-10T19:38:00Z", "symbol": "QQQ", "action": "sell", "exit_reason": "core_defense", "realized_pl": 27.56},
]


def _run6_files(tmp: Path):
    t = _write(tmp / "trades.jsonl", _RUN6_TRADES)
    e = _write(tmp / "equity_history.jsonl", _RUN6_EQUITY)
    s = tmp / "spy.csv"
    s.write_text("\n".join(_RUN6_SPY) + "\n", encoding="utf-8")
    return t, e, s


def _v3_synthetic(tmp: Path, **kw):
    """_synthetic_window + a basis='late' day-0 row dated the day before
    dates[0] at the same equity (so day 1 forms a zero-return pair) and a
    SPY close for that date."""
    start, end, t, e, s = _synthetic_window(tmp, **kw)
    rows = [json.loads(l) for l in e.read_text(encoding="utf-8").splitlines() if l.strip()]
    rows.insert(0, {"date": "2026-08-31", "equity": rows[0]["equity"], "day_pl": 0.0,
                    "basis": "late"})
    _write(e, rows)
    s.write_text(s.read_text(encoding="utf-8").replace("date,close\n", "date,close\n2026-08-31,100.0\n"),
                 encoding="utf-8")
    return start, end, t, e, s


def test_v3_selftest_blocks_pass():
    assert ecc.selftest() == 0


def test_v3_run6_regression_pairs_satellite_and_worst_day(capsys, tmp_path):
    t, e, s = _run6_files(tmp_path)
    rc = ecc.main(["--contract", "v3", "--start", "2026-09-01", "--end", "2026-09-10",
                   "--trades", str(t), "--equity", str(e), "--spy-csv", str(s)])
    out = capsys.readouterr().out
    assert rc == ecc.EXIT_PENDING
    # (1) day-0 predecessor = the Aug 31 basis='late' baseline -> Sep 1 counts
    assert "day-0 predecessor: 2026-08-31 $1,000,000.00 (basis=late)" in out
    assert "verdict rows: 7 close row(s) in window + day-0 = 8 point(s), 7 pair(s)" in out
    assert "SPY pairs in window: 2 up / 5 down" in out          # v2 said 2 up / 4 down
    assert "pairs=7  alpha/day=+0.484%" in out and "beta=0.48" in out
    # (3) satellite-only N excludes the PSQ hedge_unwind + QQQ core_defense rows
    assert "N=12" in out and "expectancy/trade: $2,241.11" in out
    assert "t-stat: 1.750  df=11  crit(95%, one-sided)=1.796" in out
    assert ("excluded system-managed rows (exit_reason core_defense/correction/"
            "defensive_rotate/hedge_unwind/regime_trim; symbols PSQ/QQQ): n=2 "
            "sum=$69.32  [2026-09-09 PSQ hedge_unwind +41.76; "
            "2026-09-10 QQQ core_defense +27.56]") in out
    assert "concentration: top-3 trips sum=$26,637.68 (99.0% of realized)" in out
    assert "profit factor=7.45" in out
    # (7) worst day on delta-equity (Sep 9), not the broker day_pl (Sep 10)
    assert ("worst day (delta-equity, self-consistent): 2026-09-09 $-7,446.11   "
            "broker day_pl that date: $-7,641.31") in out
    assert "max drawdown: -1.41% (peak 2026-09-08 -> trough 2026-09-10, 8 point(s))" in out
    # (9) run-6 rows carry the broker day_pl -> telescoping FAIL is a validity
    # warning, the restatement is printed, and the exit code is untouched
    assert "day_pl basis on close rows: self-consistent=0 broker=7" in out
    assert "max per-day gap |delta-equity - day_pl|: $1,421.49 on 2026-09-03  -> FAIL" in out
    assert ("broker last_equity restatement (informational): max |delta-equity - "
            "broker_day_pl| $1,421.49 on 2026-09-03 (7 row(s))") in out
    assert "[WARN] validity: telescoping FAIL" in out
    # (5) beta: ex-ante mean counted at >= 5 pairs; OLS CI printed, not counted
    assert "[PASS] rule 5a mean ex-ante book_beta_spy within +/-0.2 of 1.00  (mean=0.97 over 7 close row(s), 7 pairs)" in out
    assert "[N/A ] rule 5b OLS beta CI: 7 pairs < 20 — not counted (90% CI [-0.47, 1.42] recorded)" in out
    assert "[N/A ] rule 4 daily alpha: 7 pairs < 20 — not counted (alpha/day=+0.484%, t=1.33 recorded)" in out
    assert "[N/A ] rule 7 capture: INSUFFICIENT SAMPLE (2 up / 5 down pairs; need 6/6)" in out
    assert "[PEND] rule 2 window expectancy: N=12 < 24" in out
    # (8) 'pairs', never 'sessions'; (10) same-config note
    assert "sessions" not in out and "[NOTE] same-config:" in out
    assert "VERDICT: PENDING (UNDER-FLOOR: every counted rule passes; N=12 < 24" in out


def test_v3_voids_on_intraday_end_row_and_missing_row(capsys, tmp_path):
    t, e, s = _run6_files(tmp_path)
    rc = ecc.main(["--contract", "v3", "--start", "2026-09-01", "--end", "2026-09-11",
                   "--trades", str(t), "--equity", str(e), "--spy-csv", str(s)])
    out = capsys.readouterr().out
    # VOID exits 4 — distinct from PENDING's 3, so a runbook step can tell
    # "run after the close stamp" from "under-floor" (review finding).
    assert rc == ecc.EXIT_VOID == 4 and ecc.EXIT_PENDING == 3
    assert "VOID: end-date row is basis=intraday — run after the close stamp" in out
    assert "VERDICT" not in out and "N=" not in out            # nothing quotable printed
    rc = ecc.main(["--contract", "v3", "--start", "2026-09-01", "--end", "2026-09-14",
                   "--trades", str(t), "--equity", str(e), "--spy-csv", str(s)])
    out = capsys.readouterr().out
    assert rc == ecc.EXIT_VOID
    assert "VOID: end-date row is basis=none (no row for that date)" in out


def test_v3_late_row_is_the_verdict_row_when_the_date_has_no_close_row(capsys, tmp_path):
    # Review finding #1 (B1 x B3): the writer treats a basis='late' row as a
    # session mark (the bot missed the 16:xx tick and stamped on relaunch;
    # the NEXT close row's day_pl is measured against it) but the checker
    # dropped it — a 2-day pair, a telescoping FAIL by construction the next
    # day, and a permanent VOID when it was the --end date (a close row can
    # never be minted for a date that already has a late row).
    rows = [dict(r) for r in _RUN6_EQUITY]
    sep4 = next(r for r in rows if r["date"] == "2026-09-04")
    sep4["basis"] = "late"                       # bot was down at 16:xx on Sep 4
    sep4["day_pl_basis"] = "self"
    # a late row on a date that ALSO has a close row is dropped (close wins)
    rows.append({"date": "2026-09-09", "equity": 1035000.0, "day_pl": -8000.0, "basis": "late"})
    t = _write(tmp_path / "trades.jsonl", _RUN6_TRADES)
    e = _write(tmp_path / "equity_history.jsonl", rows)
    s = tmp_path / "spy.csv"
    s.write_text("\n".join(_RUN6_SPY) + "\n", encoding="utf-8")
    rc = ecc.main(["--contract", "v3", "--start", "2026-09-01", "--end", "2026-09-10",
                   "--trades", str(t), "--equity", str(e), "--spy-csv", str(s)])
    out = capsys.readouterr().out
    assert rc == ecc.EXIT_PENDING and "VOID" not in out
    assert "verdict rows: 6 close row(s) + 1 late row(s) in window + day-0 = 8 point(s), 7 pair(s)" in out
    assert ("equity basis: close=6 row(s), late=2 row(s)  "
            "(late rows admitted as verdict rows — no close row that date: 2026-09-04)  "
            "(non-close rows excluded from every rule: late=1)") in out
    assert "pairs=7  alpha/day=+0.484%" in out                # same series as the all-close run
    assert "worst day (delta-equity, self-consistent): 2026-09-09 $-7,446.11" in out
    # --end ON the late-only date is a verdict, not a VOID
    rc = ecc.main(["--contract", "v3", "--start", "2026-09-01", "--end", "2026-09-04",
                   "--trades", str(t), "--equity", str(e), "--spy-csv", str(s)])
    out = capsys.readouterr().out
    assert rc == ecc.EXIT_PENDING and "VOID" not in out
    assert "verdict rows: 3 close row(s) + 1 late row(s) in window + day-0 = 5 point(s), 4 pair(s)" in out
    assert ecc.end_row_basis(ecc.parse_jsonl([json.dumps(r) for r in rows]), "2026-09-04") == "late"
    assert ecc.end_row_basis(ecc.parse_jsonl([json.dumps(r) for r in rows]), "2026-09-09") == "close"


def test_v3_system_symbols_flag_excludes_core_and_hedge_bracket_exits(capsys, tmp_path):
    # Review finding #4: QQQ carries a core stop and PSQ can be trailed /
    # stopped — those rows arrive as bracket_stop / trail, invisible to the
    # exit_reason filter. --system-symbols (default QQQ,PSQ) excludes them by
    # symbol and names the symbols on the excluded line; '' disables.
    trades = sorted(_RUN6_TRADES + [
        {"ts": "2026-09-05T15:00:00Z", "symbol": "QQQ", "action": "sell", "exit_reason": "bracket_stop", "realized_pl": -100.0},
        {"ts": "2026-09-08T16:00:00Z", "symbol": "PSQ", "action": "sell", "exit_reason": "trail", "realized_pl": 50.0},
    ], key=lambda r: r["ts"])                    # the excluded line prints rows in file order
    t = _write(tmp_path / "trades.jsonl", trades)
    e = _write(tmp_path / "equity_history.jsonl", _RUN6_EQUITY)
    s = tmp_path / "spy.csv"
    s.write_text("\n".join(_RUN6_SPY) + "\n", encoding="utf-8")
    base = ["--contract", "v3", "--start", "2026-09-01", "--end", "2026-09-10",
            "--trades", str(t), "--equity", str(e), "--spy-csv", str(s)]
    ecc.main(base)
    out = capsys.readouterr().out
    assert "ts in window): N=12" in out                        # run-6 satellite N unchanged
    assert ("excluded system-managed rows (exit_reason core_defense/correction/"
            "defensive_rotate/hedge_unwind/regime_trim; symbols PSQ/QQQ): n=4 "
            "sum=$19.32  [2026-09-05 QQQ bracket_stop -100.00; 2026-09-08 PSQ trail +50.00; "
            "2026-09-09 PSQ hedge_unwind +41.76; 2026-09-10 QQQ core_defense +27.56]") in out
    ecc.main(base + ["--system-symbols", ""])
    out = capsys.readouterr().out
    assert "ts in window): N=14" in out and "symbols none): n=2 sum=$69.32" in out
    ecc.main(base + ["--system-symbols", "qqq"])
    out = capsys.readouterr().out
    assert "ts in window): N=13" in out                        # PSQ trail counts, hedge_unwind still out
    assert "symbols QQQ): n=3 sum=$-30.68" in out
    # v1/v2 ignore the flag (their outputs stay byte-identical)
    ecc.main(["--contract", "v2", "--start", "2026-09-01", "--end", "2026-09-10",
              "--trades", str(t), "--equity", str(e), "--spy-csv", str(s), "--system-symbols", ""])
    v2a = capsys.readouterr().out
    ecc.main(["--contract", "v2", "--start", "2026-09-01", "--end", "2026-09-10",
              "--trades", str(t), "--equity", str(e), "--spy-csv", str(s)])
    assert capsys.readouterr().out == v2a and "N=16" in v2a


def test_v2_still_admits_intraday_row_and_drops_first_day(capsys, tmp_path):
    """Pins the v2 behaviour v3 fixes, so recorded run-6 numbers reproduce."""
    t, e, s = _run6_files(tmp_path)
    rc = ecc.main(["--contract", "v2", "--start", "2026-09-01", "--end", "2026-09-11",
                   "--trades", str(t), "--equity", str(e), "--spy-csv", str(s)])
    out = capsys.readouterr().out
    assert rc == ecc.EXIT_PENDING
    assert "closed trades (realized_pl non-null, ts in window): N=14" in out
    assert "equity basis: close=7 row(s), intraday=1 row(s)" in out
    assert "SPY days in window: 3 up / 4 down" in out          # intraday Sep 11 counted, Sep 1 dropped
    assert "sessions=7" in out
    assert "worst day: 2026-09-10 day_pl $-7,830.19" in out


def test_v3_intraday_and_legacy_rows_never_verdict_rows():
    rows = ecc.parse_jsonl([
        '{"date":"2026-08-31","equity":1000000.0,"basis":"late"}',
        '{"date":"2026-09-01","equity":1001000.0,"day_pl":1000.0,"basis":"close"}',
        '{"date":"2026-09-02","equity":1003500.0,"basis":"intraday"}',
        '{"date":"2026-09-03","equity":1002500.0,"day_pl":-500.0}',
        '{"date":"2026-09-04","equity":1002000.0,"day_pl":-1000.0,"basis":"close"}',
    ])
    days, d0, crows = ecc.verdict_days_v3(rows, "2026-09-01", "2026-09-04")
    assert d0["date"] == "2026-08-31"
    assert [d for d, _, _ in days] == ["2026-08-31", "2026-09-01", "2026-09-04"]
    assert days[0][2] is None and sorted(crows) == ["2026-09-01", "2026-09-04"]
    assert ecc.end_row_basis(rows, "2026-09-02") == "intraday"
    assert ecc.end_row_basis(rows, "2026-09-03") == "legacy"
    assert ecc.end_row_basis(rows, "2026-09-04") == "close"
    # v2's selector still takes the last row per date (behaviour pinned)
    v2 = ecc.equity_days_in_window(rows, "2026-09-01", "2026-09-04")
    assert [d for d, _, _ in v2] == ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"]


def test_v3_day0_prefers_close_over_late_and_never_intraday():
    rows = ecc.parse_jsonl([
        '{"date":"2026-08-30","equity":5.0,"basis":"close"}',
        '{"date":"2026-08-31","equity":1.0,"basis":"late"}',
        '{"date":"2026-08-31","equity":2.0,"basis":"close"}',
        '{"date":"2026-08-31","equity":3.0,"basis":"intraday"}',
        '{"date":"2026-09-01","equity":4.0,"basis":"intraday"}',
    ])
    assert ecc.day0_row(rows, "2026-09-01")["equity"] == 2.0
    assert ecc.day0_row(rows, "2026-08-31")["equity"] == 5.0
    assert ecc.day0_row(rows, "2026-08-30") is None
    assert ecc.day0_row(rows[3:], "2026-09-02") is None   # intraday rows only


def test_v3_no_day0_row_falls_back_without_crash(capsys, tmp_path):
    start, end, t, e, s = _synthetic_window(tmp_path, n_trades=10, sessions=8)
    rc = ecc.main(["--contract", "v3", "--start", start, "--end", end,
                   "--trades", str(t), "--equity", str(e), "--spy-csv", str(s)])
    out = capsys.readouterr().out
    assert rc == ecc.EXIT_PENDING
    assert "day-0 predecessor: NONE before --start" in out
    assert "verdict rows: 8 close row(s) in window = 8 point(s), 7 pair(s)" in out


def test_v3_day0_adds_exactly_one_pair_vs_v2(capsys, tmp_path):
    start, end, t, e, s = _v3_synthetic(tmp_path, n_trades=10, sessions=8)
    ecc.main(["--contract", "v2", "--start", start, "--end", end,
              "--trades", str(t), "--equity", str(e), "--spy-csv", str(s)])
    v2 = capsys.readouterr().out
    ecc.main(["--contract", "v3", "--start", start, "--end", end,
              "--trades", str(t), "--equity", str(e), "--spy-csv", str(s)])
    v3 = capsys.readouterr().out
    assert "sessions=7" in v2                                # v2 unchanged
    assert "pairs=8" in v3 and "sessions" not in v3           # day 1 counted
    assert "verdict rows: 8 close row(s) in window + day-0 = 9 point(s), 8 pair(s)" in v3


def test_v3_satellite_filter_and_pooled_ledgers():
    # The exit_reason set is exactly the literals the bot writes on
    # system-managed rows (orchestrator: hedge_unwind / core_defense /
    # regime_trim / defensive_rotate; ledger: correction) — core_fill and
    # core_trim were never exit reasons. A QQQ/PSQ row leaving as
    # bracket_stop/trail is caught by the SYMBOL filter (review finding #4).
    rows = ecc.parse_jsonl([
        '{"ts":"2026-09-01T14:00:00Z","symbol":"AAA","action":"sell","exit_reason":"trail","realized_pl":10.0}',
        '{"ts":"2026-09-01T15:00:00Z","symbol":"PSQ","action":"sell","exit_reason":"hedge_unwind","realized_pl":41.76}',
        '{"ts":"2026-09-02T14:00:00Z","symbol":"QQQ","action":"sell","exit_reason":"core_defense","realized_pl":27.56}',
        '{"ts":"2026-09-02T15:00:00Z","symbol":"XLU","action":"sell","exit_reason":"defensive_rotate","realized_pl":1.0}',
        '{"ts":"2026-09-02T16:00:00Z","symbol":"FIX","action":"sell","exit_reason":"correction","realized_pl":1.0}',
        '{"ts":"2026-09-02T17:00:00Z","symbol":"QQQ","action":"sell","exit_reason":"regime_trim","realized_pl":1.0}',
        '{"ts":"2026-09-02T18:00:00Z","symbol":"QQQ","action":"sell","exit_reason":"bracket_stop","realized_pl":-30.0}',
        '{"ts":"2026-09-02T19:00:00Z","symbol":"PSQ","action":"sell","exit_reason":"trail","realized_pl":5.0}',
        '{"ts":"2026-09-03T14:00:00Z","symbol":"OPT","action":"sell","exit_reason":"trail","realized_pl":-4.0,"instrument":"option"}',
        '{"ts":"2026-09-03T15:00:00Z","symbol":"BUY","action":"buy","realized_pl":99.0}',
        '{"ts":"2026-09-03T16:00:00Z","symbol":"OPN","action":"sell","realized_pl":null}',
    ])
    # no symbol filter (helper default): only the exit_reason set applies
    sat, excl = ecc.satellite_closed_in_window(rows, "2026-09-01", "2026-09-03")
    assert [r["symbol"] for r in sat] == ["AAA", "QQQ", "PSQ", "OPT"]
    assert sorted(r["exit_reason"] for r in excl) == sorted(ecc.V3_SYSTEM_EXIT_REASONS)
    assert ecc.all_satellite_pls(rows) == [10.0, -30.0, 5.0, -4.0]
    # the CLI default V3_SYSTEM_SYMBOLS: the QQQ core stop and PSQ trail go too
    sat_s, excl_s = ecc.satellite_closed_in_window(rows, "2026-09-01", "2026-09-03",
                                                   ecc.V3_SYSTEM_SYMBOLS)
    assert [r["symbol"] for r in sat_s] == ["AAA", "OPT"]
    assert [(r["symbol"], r["exit_reason"]) for r in excl_s[-2:]] == [
        ("QQQ", "bracket_stop"), ("PSQ", "trail")]
    assert ecc.all_satellite_pls(rows, ecc.V3_SYSTEM_SYMBOLS) == [10.0, -4.0]
    assert ecc.all_satellite_pls(rows, ["qqq"]) == [10.0, 5.0, -4.0]   # case-insensitive
    assert ecc.instrument_split(sat_s) == {"equity": [10.0], "option": [-4.0]}
    # v1/v2 selector unchanged: every non-null realized_pl counts
    assert len(ecc.closed_pls_in_window(rows, "2026-09-01", "2026-09-03")) == 10


def test_v3_rule2_counts_at_24_and_prints_concentration(capsys, tmp_path):
    start, end, t, e, s = _v3_synthetic(tmp_path, n_trades=24, sessions=8, stamped_beta=1.0)
    rc = ecc.main(["--contract", "v3", "--start", start, "--end", end,
                   "--trades", str(t), "--equity", str(e), "--spy-csv", str(s)])
    out = capsys.readouterr().out
    assert rc == ecc.EXIT_GO and "VERDICT: PASS" in out, out
    assert "[INFO] rule 1 sample floor N >= 24: N=24 (met)" in out
    assert "[PASS] rule 2 window expectancy > 0 @ 95% one-sided  (N=24, t=" in out
    assert "bootstrap95=[" in out and "profit factor=inf (no losses)" in out
    assert "concentration: top-3 trips sum=$360.00" in out and "mean ex-top-3=$" in out
    assert "equity-only: N=24" in out and "option-only: N=0" in out
    assert "[INFO] rule 3 live-pilot bar: pooled N=24 < 60 — PENDING (not a window rule)" in out
    start, end, t, e, s = _v3_synthetic(tmp_path, n_trades=23, sessions=8, stamped_beta=1.0)
    rc = ecc.main(["--contract", "v3", "--start", start, "--end", end,
                   "--trades", str(t), "--equity", str(e), "--spy-csv", str(s)])
    out = capsys.readouterr().out
    assert rc == ecc.EXIT_PENDING
    assert "[PEND] rule 2 window expectancy: N=23 < 24 — undecided" in out
    assert "VERDICT: PENDING (UNDER-FLOOR: every counted rule passes; N=23 < 24" in out


def test_v3_rule2_fails_when_counted_and_mean_not_positive(capsys, tmp_path):
    start, end, t, e, s = _v3_synthetic(tmp_path, n_trades=12, sessions=8, stamped_beta=1.0)
    rows = [json.loads(l) for l in t.read_text(encoding="utf-8").splitlines() if l.strip()]
    rows += [{"ts": f"{start}T15:00:00Z", "symbol": f"L{i}", "action": "sell",
              "exit_reason": "bracket_stop", "realized_pl": -100.0 - 10 * (i % 3)}
             for i in range(12)]
    _write(t, rows)
    rc = ecc.main(["--contract", "v3", "--start", start, "--end", end,
                   "--trades", str(t), "--equity", str(e), "--spy-csv", str(s)])
    out = capsys.readouterr().out
    assert rc == ecc.EXIT_NO_GO
    assert "[FAIL] rule 2 window expectancy > 0 @ 95% one-sided  (N=24" in out
    assert "VERDICT: FAIL  (window expectancy)" in out


def test_v3_pooled_is_live_pilot_bar_only_and_satellite_filtered(capsys, tmp_path):
    start, end, t, e, s = _v3_synthetic(tmp_path, n_trades=10, sessions=8, stamped_beta=1.0)
    prior = _write(tmp_path / "prior.jsonl", [
        {"ts": f"2026-07-{1 + i % 28:02d}T14:00:00Z", "symbol": f"P{i}", "action": "sell",
         "exit_reason": "trail", "realized_pl": 200.0 + (i % 7) * 10} for i in range(55)] + [
        {"ts": "2026-07-02T14:00:00Z", "symbol": "PSQ", "action": "sell",
         "exit_reason": "hedge_unwind", "realized_pl": 9999.0}])
    rc = ecc.main(["--contract", "v3", "--start", start, "--end", end,
                   "--trades", str(t), "--equity", str(e), "--spy-csv", str(s),
                   "--pool", str(prior)])
    out = capsys.readouterr().out
    assert rc == ecc.EXIT_PENDING                              # pooled never decides a window
    assert "prior.jsonl: N=55 " in out                          # hedge row filtered out
    assert "POOLED: N=65 (2 window(s))" in out and "-> DECIDES (needs N >= 60)" in out
    assert "[INFO] rule 3 live-pilot bar: pooled N=65 >= 60" in out and "-> PASS (not a window rule)" in out
    assert "[PEND] rule 2 window expectancy: N=10 < 24" in out
    assert "[NOTE] same-config:" in out


def test_v3_beta_exante_mean_counted_at_5_pairs(capsys, tmp_path):
    start, end, t, e, s = _v3_synthetic(tmp_path, n_trades=5, sessions=8, stamped_beta=0.5)
    rc = ecc.main(["--contract", "v3", "--start", start, "--end", end,
                   "--trades", str(t), "--equity", str(e), "--spy-csv", str(s)])
    out = capsys.readouterr().out
    assert rc == ecc.EXIT_NO_GO
    assert "[FAIL] rule 5a mean ex-ante book_beta_spy within +/-0.2 of 1.00  (mean=0.50 over 8 close row(s), 8 pairs)" in out
    assert "[N/A ] rule 5b OLS beta CI: 8 pairs < 20 — not counted (90% CI [" in out
    assert "VERDICT: FAIL  (beta ex-ante mean)" in out
    # below 5 pairs the ex-ante rule is not counted either
    start, end, t, e, s = _v3_synthetic(tmp_path, n_trades=5, sessions=4, stamped_beta=0.5)
    rc = ecc.main(["--contract", "v3", "--start", start, "--end", end,
                   "--trades", str(t), "--equity", str(e), "--spy-csv", str(s)])
    out = capsys.readouterr().out
    assert rc == ecc.EXIT_PENDING and "[N/A ] rule 5a beta ex-ante mean: 4 pairs < 5" in out


def test_v3_beta_ols_ci_counted_at_20_pairs(capsys, tmp_path):
    # book = 1.5x SPY up / 1.35x down, stamps 1.5 -> 5a FAIL (stamps) and
    # 5b FAIL (beta 1.4, tight CI, no overlap with [0.8, 1.2]); rule 4 counted.
    start, end, t, e, s = _v3_synthetic(tmp_path, n_trades=10, sessions=25,
                                        bot_mult=1.5, stamped_beta=1.5)
    rc = ecc.main(["--contract", "v3", "--start", start, "--end", end,
                   "--trades", str(t), "--equity", str(e), "--spy-csv", str(s)])
    out = capsys.readouterr().out
    assert rc == ecc.EXIT_NO_GO
    assert "pairs=25" in out and "SE(beta)=" in out and "90% CI=[" in out
    assert "[FAIL] rule 5a mean ex-ante book_beta_spy" in out
    assert "[FAIL] rule 5b OLS beta 90% CI [" in out and "overlaps [0.80, 1.20]  (beta=1.4" in out
    assert "[PASS] rule 4 daily alpha > 0" in out
    # 1.5x up / 1.35x down: up > down but down-capture 135% >= 100% -> rule 7 FAIL too
    assert "[FAIL] rule 7 up-capture > down-capture AND down-capture < 100%  (up 150.0% vs down 135.0%" in out
    assert "VERDICT: FAIL  (beta ex-ante mean, beta OLS CI, capture)" in out
    # book = 1.0x / 0.9x with stamps 1.0 -> both beta rules PASS, capture PASS
    start, end, t, e, s = _v3_synthetic(tmp_path, n_trades=10, sessions=25, stamped_beta=1.0)
    rc = ecc.main(["--contract", "v3", "--start", start, "--end", end,
                   "--trades", str(t), "--equity", str(e), "--spy-csv", str(s)])
    out = capsys.readouterr().out
    assert rc == ecc.EXIT_PENDING, out
    assert "[PASS] rule 5a mean ex-ante book_beta_spy within +/-0.2 of 1.00  (mean=1.00" in out
    assert "[PASS] rule 5b OLS beta 90% CI [" in out and "(beta=0.95, 25 pairs)" in out
    assert "[PASS] rule 4 daily alpha > 0  (alpha/day=+0.0" in out
    assert "[PASS] rule 7 up-capture > down-capture AND down-capture < 100%  (up 100.0% vs down 90.0%, 12 up / 12 down pairs)" in out
    assert "sessions" not in out


def test_v3_capture_needs_6_6_and_down_below_100(capsys, tmp_path):
    # 13 close rows + day-0 (zero-return pair) -> SPY 6 up / 6 down pairs
    start, end, t, e, s = _v3_synthetic(tmp_path, n_trades=3, sessions=13, bot_mult=1.0)
    rows = [json.loads(l) for l in e.read_text(encoding="utf-8").splitlines() if l.strip()]
    # rebuild the book at 1.6x SPY on up days / 1.2x on down days
    eq = rows[1]["equity"]
    for i, r in enumerate(rows[2:]):
        ret = 0.01 if i % 2 == 0 else -0.01
        eq *= 1 + ret * (1.6 if ret > 0 else 1.2)
        r["equity"] = eq
        r["day_pl"] = None
    _write(e, rows)
    rc = ecc.main(["--contract", "v3", "--start", start, "--end", end,
                   "--trades", str(t), "--equity", str(e), "--spy-csv", str(s)])
    out = capsys.readouterr().out
    assert rc == ecc.EXIT_NO_GO
    assert "[FAIL] rule 7 up-capture > down-capture AND down-capture < 100%  (up 160.0% vs down 120.0%, 6 up / 6 down pairs)" in out
    assert "VERDICT: FAIL  (capture)" in out
    # 5 up / 5 down -> INSUFFICIENT, not counted
    rc = ecc.main(["--contract", "v3", "--start", start, "--end", "2026-09-11",
                   "--trades", str(t), "--equity", str(e), "--spy-csv", str(s)])
    out = capsys.readouterr().out
    assert rc == ecc.EXIT_PENDING
    assert "[N/A ] rule 7 capture: INSUFFICIENT SAMPLE (5 up / 5 down pairs; need 6/6)" in out


def test_v3_worst_day_on_delta_equity_and_self_consistent_telescoping(capsys, tmp_path):
    rows = [
        {"date": "2026-08-31", "equity": 100000.0, "day_pl": 0.0, "basis": "late"},
        {"date": "2026-09-01", "equity": 101000.0, "day_pl": 1000.0, "basis": "close",
         "day_pl_basis": "self", "broker_day_pl": 1300.0},
        {"date": "2026-09-02", "equity": 103000.0, "day_pl": 2000.0, "basis": "close",
         "day_pl_basis": "self", "broker_day_pl": -2500.0},
        {"date": "2026-09-03", "equity": 102000.0, "day_pl": -1000.0, "basis": "close",
         "day_pl_basis": "self", "broker_day_pl": -900.0},
    ]
    days, _, crows = ecc.verdict_days_v3(rows, "2026-09-01", "2026-09-03")
    assert ecc.worst_day_delta(days) == ("2026-09-03", -1000.0)
    assert ecc.worst_day(days) == ("2026-09-03", -1000.0)     # self day_pl agrees
    assert ecc.day_pl_basis_counts(crows) == {"self": 3, "broker": 0}
    br = ecc.broker_restatement(days, crows)
    assert br["rows"] == 3 and abs(br["max_gap"] - 4500.0) < 1e-9 and br["max_gap_date"] == "2026-09-02"
    t = _write(tmp_path / "trades.jsonl", [])
    e = _write(tmp_path / "equity_history.jsonl", rows)
    rc = ecc.main(["--contract", "v3", "--start", "2026-09-01", "--end", "2026-09-03",
                   "--trades", str(t), "--equity", str(e)])
    out = capsys.readouterr().out
    assert rc == ecc.EXIT_PENDING
    assert "worst day (delta-equity, self-consistent): 2026-09-03 $-1,000.00   broker day_pl that date: $-900.00" in out
    assert "day_pl basis on close rows: self-consistent=3 broker=0" in out
    assert "max per-day gap |delta-equity - day_pl|: $0.00 on None  -> PASS (limit $1.00)" in out
    assert "broker last_equity restatement (informational): max |delta-equity - broker_day_pl| $4,500.00 on 2026-09-02 (3 row(s))" in out
    assert "[WARN] validity" not in out
    # run-6-style rows (broker day_pl, no day_pl_basis): telescoping FAIL is
    # a [WARN] line and the worst day still comes from delta-equity
    legacy = [dict(r) for r in rows]
    for r in legacy[1:]:
        r.pop("day_pl_basis"); r["day_pl"] = r.pop("broker_day_pl")
    e = _write(tmp_path / "equity_history.jsonl", legacy)
    rc = ecc.main(["--contract", "v3", "--start", "2026-09-01", "--end", "2026-09-03",
                   "--trades", str(t), "--equity", str(e)])
    out = capsys.readouterr().out
    assert rc == ecc.EXIT_PENDING                              # validity never sets the exit code
    assert "worst day (delta-equity, self-consistent): 2026-09-03 $-1,000.00   broker day_pl that date: $-900.00" in out
    assert "day_pl basis on close rows: self-consistent=0 broker=3" in out
    assert "[WARN] validity: telescoping FAIL (max gap $4,500.00 on 2026-09-02)" in out


def test_v3_helper_units():
    assert ecc.profit_factor([10.0, -5.0, 20.0]) == 6.0
    assert ecc.profit_factor([10.0]) == float("inf") and ecc.profit_factor([]) is None
    c = ecc.concentration([100.0, 50.0, 20.0, 1.0, -1.0, 2.0])
    assert c["top_sum"] == 170.0 and c["n_rest"] == 3 and abs(c["mean_ex_top"] - 2 / 3) < 1e-12
    assert ecc.concentration([1.0])["mean_ex_top"] is None
    o = ecc.ols_alpha_beta([(f"2026-09-{i:02d}", 100.0 * (1 + 0.002 * i), None) for i in range(1, 11)],
                           {f"2026-09-{i:02d}": 50.0 * (1 + 0.001 * i) for i in range(1, 11)})
    assert o["se_beta"] is not None and ecc.beta_ci90(o)[0] <= o["beta"] <= ecc.beta_ci90(o)[1]
    assert ecc.beta_ci90({"beta": None, "se_beta": None}) is None
    assert ecc._interval_overlaps((0.7, 0.9), 0.8, 1.2) and not ecc._interval_overlaps((0.3, 0.7), 0.8, 1.2)
    assert ecc.V3_SYSTEM_EXIT_REASONS == {"hedge_unwind", "core_defense", "regime_trim",
                                          "defensive_rotate", "correction"}
    assert ecc.V3_SYSTEM_SYMBOLS == {"QQQ", "PSQ"}
    assert (ecc.EXIT_GO, ecc.EXIT_NO_GO, ecc.EXIT_PENDING, ecc.EXIT_VOID) == (0, 2, 3, 4)
    assert (ecc.V3_MIN_TRADES_FLOOR, ecc.V3_POOLED_MIN_TRADES, ecc.V3_MIN_ALPHA_PAIRS,
            ecc.V3_MIN_BETA_PAIRS, ecc.V3_CAPTURE_MIN_UP_DAYS, ecc.V3_CAPTURE_MIN_DOWN_DAYS) == (24, 60, 20, 5, 6, 6)
