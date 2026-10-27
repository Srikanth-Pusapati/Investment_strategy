"""scripts/run8_morning_check.py — the run-8 adaptations of the run-7 checker.

WHY each is pinned: (1) FROZEN_TREE ships EMPTY and is filled at pre-registration;
an empty pin must read as FAIL, never as PASS (an unpinned "" would otherwise
equal the "" that `git rev-parse` returns outside a repo). (2) The .env check
must count the run-7 twelve PLUS the twelve run-8 strategy keys, each exactly
once. (3) C-7: the Sep 18 Robinhood token died Sep 24 11:09 CT mid-session and
the refresh needs a browser, so a token with < 24 h left is a WARN telling the
operator to re-login today; >= 24 h stays quiet. (4) The in-session log check
counts the run-8 watch handles. The log-level / scoping behaviour inherited from
the run-7 checker is covered by tests/test_run7_morning_check.py.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "run8_morning_check", ROOT / "scripts" / "run8_morning_check.py")
mc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mc)

TODAY = dt.date.today().isoformat()
RUN7_KEYS = (
    "HEDGE_BETA_ASSUMED", "PROXY_PUT_PREFER_MONTHLY", "OPTION_STRIKE_SNAP",
    "OPTION_STRIKE_MAX_MONEYNESS_PCT", "REGIME_LOOSEN_MIN_CYCLES",
    "TOPUP_MIN_CONVICTION_DELTA", "HEDGE_UNWIND_MIN_CYCLES",
    "CORE_FILL_BETA_CLAMP", "HEDGE_STARVED_CORE_TRIM",
    "HEDGE_STARVED_TRIM_MAX_PCT", "REGIME_FALLING_TAPE_CAP",
    "LEDGER_RESTATE_AT_FILL",
)
RUN8_KEYS = (
    "TOPUP_AUTHORITY", "TOPUP_MAX_MULTIPLE_OF_HELD", "MAX_TOP3_SATELLITE_PCT",
    "ENTRY_OPEN_DELAY_MIN", "BEARISH_IGNORED_FALLBACK",
    "OPTION_ENTRY_TIER1_BUFFER_PCT", "PUT_NOQUOTE_COOLDOWN_CYCLES",
    "OPTION_OI_PER_CONTRACT", "OPTION_MARK_FALLBACK", "OPTION_EXITS_RTH_ONLY",
    "PREMIUM_CAP_SLATE_EXCLUDE", "MAX_SYMBOL_EXPOSURE_PCT",
)
RUN8_HANDLES = (
    "TOPUP AUTHORITY:", "BUCKET CAP:", "ENTRY DEFERRED", "BEARISH FALLBACK:",
    "OPTION ENTRY SLIP:", "PUT NO-QUOTE:", "OPTION MARK FALLBACK:",
    "OPTION EXIT QUEUED", "OPTION LOT CAP:",
)


@pytest.fixture
def root(tmp_path, monkeypatch):
    (tmp_path / "logs").mkdir()
    (tmp_path / "state").mkdir()
    monkeypatch.setattr(mc, "ROOT", tmp_path)
    monkeypatch.setattr(mc, "_results", [])
    return tmp_path


def _line(hms: str, level: str, logger: str, msg: str) -> str:
    return f"{TODAY} {hms},000 {level} {logger} | {msg}"


def _result(name: str) -> tuple[str, str]:
    return next((lvl, msg) for lvl, n, msg in mc._results if n == name)


def _results(name: str) -> list[tuple[str, str]]:
    return [(lvl, msg) for lvl, n, msg in mc._results if n == name]


# -- (a) the freeze pin ----------------------------------------------------- #

def test_unpinned_frozen_tree_is_a_fail_not_a_silent_pass(root, monkeypatch):
    monkeypatch.setattr(mc, "FROZEN_TREE", "")
    mc.check_freeze(pid=None)                     # cwd is tmp_path: git answers ""
    lvl, msg = _result("freeze")
    assert lvl == "FAIL"
    assert "FROZEN_TREE not pinned" in msg
    assert "git rev-parse HEAD:investment_strategy" in msg


def test_pinned_tree_mismatch_fails_and_match_passes(root, monkeypatch):
    def git(tree):
        def _sh(*args, **kw):
            return tree if args[1:3] == ("rev-parse", "HEAD:investment_strategy") else ""
        return _sh

    monkeypatch.setattr(mc, "FROZEN_TREE", "a" * 40)
    monkeypatch.setattr(mc, "_sh", git("b" * 40))
    mc.check_freeze(pid=None)
    lvl, msg = _result("freeze")
    assert lvl == "FAIL" and "!= frozen" in msg
    mc._results.clear()
    monkeypatch.setattr(mc, "_sh", git("a" * 40))
    mc.check_freeze(pid=None)
    assert _result("freeze")[0] == "PASS"


def test_frozen_tree_matches_the_pre_registered_contract_once_pinned():
    contract = ROOT / "runs" / "pre-final-test-run-8" / "EVAL_CONTRACT.md"
    if not mc.FROZEN_TREE or not contract.exists():
        pytest.skip("run-8 not pre-registered yet (FROZEN_TREE unpinned or no EVAL_CONTRACT.md)")
    assert mc.FROZEN_TREE in contract.read_text()


# -- (b) the strategy-key list ----------------------------------------------- #

def test_strategy_keys_are_the_run7_twelve_plus_the_run8_twelve():
    assert set(RUN7_KEYS) <= set(mc.STRATEGY_KEYS)
    assert set(RUN8_KEYS) <= set(mc.STRATEGY_KEYS)
    assert len(mc.STRATEGY_KEYS) == 24 == len(set(mc.STRATEGY_KEYS))


def test_env_check_wants_every_key_exactly_once(root):
    lines = [f"{k}=x" for k in mc.STRATEGY_KEYS]
    (root / ".env").write_text("\n".join(lines) + "\n")
    mc.check_env(pid=None)
    lvl, msg = _result(".env keys")
    assert lvl == "PASS" and msg.startswith("24 strategy keys once each")
    mc._results.clear()
    (root / ".env").write_text("\n".join(lines + ["TOPUP_AUTHORITY=again"]) + "\n")
    mc.check_env(pid=None)
    lvl, msg = _result(".env keys")
    assert lvl == "FAIL" and "run-8 key lines" in msg and "TOPUP_AUTHORITYx2" in msg
    mc._results.clear()
    (root / ".env").write_text("\n".join(ln for ln in lines if not ln.startswith("OPTION_MARK_FALLBACK=")) + "\n")
    mc.check_env(pid=None)
    lvl, msg = _result(".env keys")
    assert lvl == "FAIL" and "OPTION_MARK_FALLBACKx0" in msg


# -- (c) C-7: the Robinhood token horizon ------------------------------------- #

def _rh(root, hours_left: float | None) -> None:
    (root / ".env").write_text("ROBINHOOD_ENABLED=on\n")
    if hours_left is not None:                   # file mtime = now, so left = expires_in
        (root / "state" / "robinhood_oauth.json").write_text(
            json.dumps({"tokens": {"expires_in": hours_left * 3600}}))


def test_token_with_less_than_a_day_left_warns_to_relogin_today(root):
    _rh(root, 10)
    mc.check_robinhood(network=False)
    warns = [msg for lvl, msg in _results("robinhood") if lvl == "WARN"]
    assert len(warns) == 1
    assert warns[0].startswith("token expires in 10 h — re-login today (")
    assert mc.RH_LOGIN_CMD in warns[0]


def test_token_with_more_than_a_day_left_does_not_warn(root):
    _rh(root, 50)
    mc.check_robinhood(network=False)
    assert all(lvl != "WARN" for lvl, _ in _results("robinhood"))
    assert any(lvl == "INFO" and "(50 h)" in msg for lvl, msg in _results("robinhood"))


def test_unreadable_token_file_is_reported_without_an_expiry_warn(root):
    _rh(root, None)
    mc.check_robinhood(network=False)
    assert all(lvl != "WARN" for lvl, _ in _results("robinhood"))
    assert any("token file unreadable" in msg for _, msg in _results("robinhood"))


def test_auth_dead_latch_still_fails_ahead_of_the_expiry_warn(root):
    _rh(root, 10)
    (root / "state" / "robinhood_health.json").write_text(json.dumps({"auth_dead": True, "detail": "401"}))
    mc.check_robinhood(network=False)
    assert any(lvl == "FAIL" and "NEEDS LOGIN" in msg for lvl, msg in _results("robinhood"))


# -- (d)/(e) run-8 wording and watch handles ---------------------------------- #

def test_watch_handles_include_the_run8_handles_and_the_log_check_counts_them(root):
    assert set(RUN8_HANDLES) <= set(mc.WATCH_HANDLES)
    (root / "logs" / "bot.log").write_text("\n".join([
        _line("09:40:00", "INFO", "orchestrator", "TOPUP AUTHORITY: DBRG top-up capped at 2.0x held"),
        _line("09:41:00", "INFO", "orchestrator", "ENTRY DEFERRED (open +7 min < 15): NVDA"),
        _line("09:42:00", "WARNING", "orchestrator", "PUT NO-QUOTE: STE cooled for 3 cycles"),
        _line("09:43:00", "INFO", "orchestrator", "PUT NO-QUOTE: STE still cooled"),
    ]))
    mc.check_log(is_open=False)
    msg = _result("handles")[1]
    assert "TOPUP AUTHORITY x1" in msg and "ENTRY DEFERRED x1" in msg and "PUT NO-QUOTE x2" in msg


def test_no_run7_wording_or_paths_remain():
    src = (ROOT / "scripts" / "run8_morning_check.py").read_text()
    for stale in ("RUN-7", "run-7", "run7_", "RUN7_SWITCH", "pre-final-test-run-7"):
        assert stale not in src, stale
    assert "RUN-8 CHECK" in src and "run8_morning_check.py" in src
