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
