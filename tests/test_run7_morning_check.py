"""scripts/run7_morning_check.py — the two ways its first live run lied.

WHY: on its first run (Mon 2026-09-21 evening, run-7 day 0) the checker printed
NOT READY for a healthy bot. (1) It read the WHOLE local day of logs/bot.log,
and on a switch day that file also holds the PREVIOUS account's session — it
charged run-7 with run-6's morning battery pages. (2) It matched the word
CRITICAL anywhere on the line, so `INFO notify | CRITICAL alert emailed ...`
counted as a CRITICAL (8 reported, 3 real on the Sep 18 replay). A readiness
check that cries wolf gets ignored, so both are pinned here, with the FEEDS
breach threshold it grades against contract v3 (> 2 degraded cycles in a day).
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "run7_morning_check",
    Path(__file__).resolve().parent.parent / "scripts" / "run7_morning_check.py")
mc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mc)

TODAY = dt.date.today().isoformat()


@pytest.fixture
def root(tmp_path, monkeypatch):
    (tmp_path / "logs").mkdir()
    (tmp_path / "state").mkdir()
    monkeypatch.setattr(mc, "ROOT", tmp_path)
    monkeypatch.setattr(mc, "_results", [])
    return tmp_path


def _line(hms: str, level: str, logger: str, msg: str) -> str:
    return f"{TODAY} {hms},000 {level} {logger} | {msg}"


def _cycle(hms: str, feeds: str = "3/3 healthy news=vader-fallback") -> list[str]:
    return [_line(hms, "INFO", "orchestrator", "BOOK BETA: spy=1.00 qqq=0.80"),
            _line(hms, "INFO", "orchestrator", f"FEEDS: {feeds}"),
            _line(hms, "INFO", "decision", "Claude returned 1 proposal(s), 0 bearish verdict(s).")]


def _result(name: str) -> tuple[str, str]:
    return next((lvl, msg) for lvl, n, msg in mc._results if n == name)


def test_level_is_the_field_not_a_word_in_the_message():
    assert mc._level(_line("09:56:37", "INFO", "notify", "CRITICAL alert emailed to x")) == "INFO"
    assert mc._level(_line("09:56:37", "CRITICAL", "orchestrator", "On BATTERY")) == "CRITICAL"
    assert mc._level("not a log line") == ""


def test_paged_info_lines_are_not_counted_as_critical(root):
    (root / "logs" / "bot.log").write_text("\n".join(
        _cycle("08:35:00")
        + [_line("00:00:01", "INFO", "notify", "CRITICAL alert emailed to x: Bot was dark")]))
    mc.check_log(is_open=True)
    lvl, msg = _result("severity")
    assert lvl == "PASS" and msg.startswith("0 CRITICAL")


def test_log_is_scoped_to_the_current_run_on_a_switch_day(root):
    # The previous account's session (a real CRITICAL at 00:00:05) precedes this
    # run's first equity row; only the lines after it are this run's business.
    start = dt.datetime.now().astimezone().replace(hour=0, minute=30, second=0, microsecond=0)
    (root / "state" / "equity_history.jsonl").write_text(
        json.dumps({"date": TODAY, "ts": start.isoformat(), "basis": "late"}) + "\n")
    (root / "logs" / "bot.log").write_text("\n".join(
        [_line("00:00:05", "CRITICAL", "orchestrator", "On BATTERY during market hours")]
        + _cycle("00:00:06")
        + [_line("00:29:00", "INFO", "orchestrator", "Starting orchestrator (paper).")]))
    mc.check_log(is_open=False)
    assert _result("severity")[1].startswith("0 CRITICAL")
    assert "1 start(s)" in _result("restarts")[1]      # the 2-min slack keeps the start-up line
    assert _result("cycles")[0] == "INFO"              # the old account's cycle is not counted
    assert "00:28:00" in _result("log scope")[1]


def test_an_old_critical_warns_and_a_fresh_one_fails(root):
    now = dt.datetime.now()
    if now.hour < 2:
        pytest.skip("needs a same-day timestamp more than an hour old")
    (root / "logs" / "bot.log").write_text(
        _line("00:00:05", "CRITICAL", "orchestrator", "On BATTERY during market hours"))
    mc.check_log(is_open=False)
    assert _result("severity")[0] == "WARN"
    mc._results.clear()
    fresh = (now - dt.timedelta(minutes=5)).strftime("%H:%M:%S")
    (root / "logs" / "bot.log").write_text(
        _line(fresh, "CRITICAL", "orchestrator", "On BATTERY during market hours"))
    mc.check_log(is_open=False)
    assert _result("severity")[0] == "FAIL"


@pytest.mark.parametrize("degraded,want", [(0, "PASS"), (2, "WARN"), (3, "FAIL")])
def test_feeds_breach_is_more_than_two_degraded_cycles(root, degraded, want):
    sick = "2/3 — insider UNHEALTHY (edgar: timeout) — EVAL WINDOW VALIDITY AT RISK"
    lines: list[str] = []
    for i in range(4):
        lines += _cycle(f"0{i}:10:00", sick if i < degraded else "3/3 healthy news=vader-fallback")
    (root / "logs" / "bot.log").write_text("\n".join(lines))
    mc.check_log(is_open=True)
    assert _result("feeds")[0] == want


def test_cycles_ran_without_a_feeds_line_is_a_breach(root):
    (root / "logs" / "bot.log").write_text(
        _line("08:35:00", "INFO", "decision", "Claude returned 0 proposal(s), 0 bearish verdict(s)."))
    mc.check_log(is_open=True)
    assert _result("feeds")[0] == "FAIL"


def test_env_value_takes_the_last_line_and_strips_comment_and_quotes():
    lines = ["FOO=off", 'FOO="on"   # flipped', "BAR=1"]
    assert mc._env_value(lines, "FOO") == "on"
    assert mc._env_value(lines, "MISSING") is None


def test_frozen_tree_matches_the_pre_registered_contract():
    contract = (Path(__file__).resolve().parent.parent
                / "runs" / "pre-final-test-run-7" / "EVAL_CONTRACT.md").read_text()
    assert mc.FROZEN_TREE in contract
