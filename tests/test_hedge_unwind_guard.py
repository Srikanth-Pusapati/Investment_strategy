"""Run-7 S-8: HEDGE_UNWIND_MIN_CYCLES (beta-mode unwind noise guard, default 1
= today's behaviour) + 4a-17 hedge observability.

WHY: the beta hedge unwinds on ONE below-band reading (run-6 Sep 9 09:22:
0.44 after the same-cycle exits closed 10,283 PSQ at 25.84; re-armed Sep 10
11:06 at 26.09). The counterfactual showed 13 consecutive below-band reads
followed, so a persistence bar would NOT have kept that hedge — the knob only
guards a noise-driven unwind (n=0 in run-6) and ships at 1 so the fingerprint
is unchanged. The observability lines exist so run-7 can price the next
whipsaw in the log instead of by hand (the run-6 figure came out $1,917 ..
$2,754 depending on the analyst's prices, lot and endpoints).

Streak hazard pinned here: the breadth re-arm (orchestrator._breadth_rearm)
re-runs _apply_auto_hedge inside ONE decision cycle (Sep_10_2026.log 14:35:37
and 14:38:52), so a per-call counter would meet N=2 in a single cycle; the
streak is keyed by _cycle_seq and counts once per cycle.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import investment_strategy.postmortem as pm_mod
from investment_strategy.portfolio.beta import BookBeta, shrink
from investment_strategy.state import PortfolioState
from tests.test_run6_beta import (
    _Broker, _acct, _bench_series, _beta_orch, _env_without, _eq, _hedge_acct,
    _read_orch, _series,
)

_ET = ZoneInfo("America/New_York")


def _msgs(caplog, prefix):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith(prefix)]


def _next_cycle(o):
    o._cycle_seq = getattr(o, "_cycle_seq", 0) + 1


# --------------------------------------------------------------------------- #
# S-8: the knob
# --------------------------------------------------------------------------- #
def test_hedge_unwind_min_cycles_config_default_env_and_validation(caplog):
    from investment_strategy.config import Config, load_config
    assert Config.__dataclass_fields__["hedge_unwind_min_cycles"].default == 1
    env = _env_without("HEDGE_UNWIND_MIN_CYCLES")
    with patch.dict(os.environ, env, clear=True):
        assert load_config().hedge_unwind_min_cycles == 1
    with patch.dict(os.environ, {**env, "HEDGE_UNWIND_MIN_CYCLES": "2"}, clear=True):
        assert load_config().hedge_unwind_min_cycles == 2
    # 0 / negative / garbage -> WARN + 1 (the streak bar must stay reachable).
    for bad in ("0", "-1", "two"):
        caplog.clear()
        caplog.set_level(logging.WARNING, logger="config")
        with patch.dict(os.environ, {**env, "HEDGE_UNWIND_MIN_CYCLES": bad}, clear=True):
            assert load_config().hedge_unwind_min_cycles == 1, bad
        assert any("HEDGE_UNWIND_MIN_CYCLES=" in r.getMessage() for r in caplog.records), bad


def test_default_min_cycles_one_unwinds_on_first_read_unchanged():
    # No attribute at all (pre-S-8 cfg) and an explicit 1 both close on the
    # first below-band read — tests/test_run6_beta.py's one-read unwind test
    # is the same contract and passes untouched.
    o = _beta_orch([0.80])
    o._apply_auto_hedge(_hedge_acct(psq_value=100_000.0))
    assert o.broker.closed == ["PSQ"]
    o = _beta_orch([0.80])
    o.cfg.hedge_unwind_min_cycles = 1
    o._apply_auto_hedge(_hedge_acct(psq_value=100_000.0))
    assert o.broker.closed == ["PSQ"]


def test_beta_hedge_unwind_min_cycles_two_holds_first_read(caplog):
    caplog.set_level(logging.INFO)
    o = _beta_orch([0.80])
    o.cfg.hedge_unwind_min_cycles = 2
    o._cycle_seq = 1
    acct = _hedge_acct(psq_value=100_000.0)
    o._apply_auto_hedge(acct)
    assert o.broker.closed == []                       # read 1/2: held
    assert o._unwind_reads == (1, 1)
    assert o._hedge_reason == "beta:0.80" and o._falling_cycles == 1
    held = _msgs(caplog, "Auto-hedge: beta: unwind read 1/2 — holding $100000 PSQ")
    assert len(held) == 1
    assert "book spy-beta 0.80 < target 1.00 - 0.15 band" in held[0]
    assert "would have closed 1000 PSQ at HEDGE_UNWIND_MIN_CYCLES=1" in held[0]
    # Second consecutive below-band read in the NEXT cycle -> close.
    _next_cycle(o)
    o._apply_auto_hedge(acct)
    assert o.broker.closed == ["PSQ"]
    assert o.ledger.records[-1].exit_reason == "hedge_unwind"
    assert o._hedge_reason == "" and o._falling_cycles == 0
    assert any(m.startswith("AUTO-HEDGE UNWIND: beta: book spy-beta 0.80 < target 1.00")
               for m in _msgs(caplog, "AUTO-HEDGE UNWIND"))


def test_unwind_streak_counts_once_per_cycle_on_breadth_rearm(caplog):
    caplog.set_level(logging.INFO)
    o = _beta_orch([0.80])
    o.cfg.hedge_unwind_min_cycles = 2
    o.cfg.breadth_falling_names_min = 3
    o._cycle_seq = 7
    o._falling_names = {"DRAM": "-5.1%", "INTC": "-4.2%", "SEI": "-6.0%"}
    o._falling_read_last = False                       # top-of-cycle pass read clear
    o._apply_core_defense = lambda account: None
    acct = _hedge_acct(psq_value=100_000.0)
    o._apply_auto_hedge(acct)                          # top-of-cycle pass: read 1/2
    o._breadth_rearm(acct)                             # same cycle: re-runs the hedge
    assert len(_msgs(caplog, "BREADTH RE-ARM:")) == 1
    assert o.broker.closed == []                       # one cycle != two reads
    assert o._unwind_reads == (7, 1)
    assert len(_msgs(caplog, "Auto-hedge: beta: unwind read 1/2")) == 2   # logged per call, counted per cycle
    _next_cycle(o)
    o._apply_auto_hedge(acct)
    assert o.broker.closed == ["PSQ"]


def test_unwind_streak_resets_on_arm_hold_and_unavailable():
    acct = _hedge_acct(psq_value=100_000.0)
    # Unavailable reading breaks the streak (a gap must not carry a stale count).
    o = _beta_orch([0.80, None, 0.80])
    o.cfg.hedge_unwind_min_cycles = 2
    o._cycle_seq = 1
    o._apply_auto_hedge(acct)
    assert o._unwind_reads == (1, 1)
    _next_cycle(o)
    o._apply_auto_hedge(acct)
    assert o._unwind_reads == (-1, 0)
    _next_cycle(o)
    o._apply_auto_hedge(acct)
    assert o._unwind_reads == (3, 1) and o.broker.closed == []
    # A hold-with-hedge read breaks it.
    o = _beta_orch([0.80, 1.00, 0.80])
    o.cfg.hedge_unwind_min_cycles = 2
    o._cycle_seq = 1
    o._apply_auto_hedge(acct)
    assert o._unwind_reads == (1, 1)
    _next_cycle(o)
    o._apply_auto_hedge(acct)
    assert o._unwind_reads == (-1, 0)
    _next_cycle(o)
    o._apply_auto_hedge(acct)
    assert o._unwind_reads == (3, 1) and o.broker.closed == []
    # An arm breaks it.
    o = _beta_orch([0.80, 1.30, 0.80])
    o.cfg.hedge_unwind_min_cycles = 2
    o._cycle_seq = 1
    o._apply_auto_hedge(acct)
    assert o._unwind_reads == (1, 1)
    _next_cycle(o)
    o._apply_auto_hedge(acct)
    assert o._unwind_reads == (-1, 0) and o.broker.buys   # topped the hedge up
    _next_cycle(o)
    o._apply_auto_hedge(acct)
    assert o._unwind_reads == (3, 1) and o.broker.closed == []


# --------------------------------------------------------------------------- #
# 4a-17: BOOK BETA line hedge segment + post-exec line
# --------------------------------------------------------------------------- #
def test_book_beta_line_reports_hedge_weight_and_unhedged_beta():
    series = _bench_series()
    series["HOT"] = _series(2.0)
    series["PSQ"] = _series(-1.2)
    bb = BookBeta(_Broker(series), hedge_etf="psq")
    r = bb.read(_acct([_eq("HOT", 30_000), _eq("PSQ", 10_000)]))
    assert r.available and r.hedge_etf == "PSQ"
    w, b, unh = r.hedge_view()
    assert abs(w - 0.1) < 1e-9 and abs(b - shrink(-1.2)) < 1e-6
    assert abs(unh - 0.3 * shrink(2.0)) < 1e-6        # the book with PSQ removed
    assert abs(r.spy - (unh + w * b)) < 1e-9          # headline = unhedged + hedge pull
    line = r.line()
    assert f" hedge=PSQ w=0.100 beta={shrink(-1.2):.2f} unhedged={unh:.2f}" in line
    d = r.to_dict()
    assert d["hedge_etf"] == "PSQ" and abs(d["hedge_w"] - 0.1) < 1e-6
    assert abs(d["hedge_beta"] - shrink(-1.2)) < 1e-4 and abs(d["unhedged_spy"] - unh) < 1e-4
    # Configured but not held: w=0 and unhedged == spy.
    r2 = bb.read(_acct([_eq("HOT", 30_000)]))
    assert " hedge=PSQ w=0 unhedged=" in r2.line()
    assert abs(r2.hedge_view()[2] - r2.spy) < 1e-9
    # No hedge named: the run-6 line and dict shape, unchanged.
    r3 = BookBeta(_Broker(series)).read(_acct([_eq("HOT", 30_000), _eq("PSQ", 10_000)]))
    assert "hedge=" not in r3.line() and r3.hedge_view() is None
    assert r3.to_dict()["hedge_etf"] == "" and r3.to_dict()["unhedged_spy"] is None


def test_read_book_beta_line_gains_hedge_segment_from_cfg(caplog):
    # A reader built without the kwarg (older wiring) still renders the
    # segment: the orchestrator stamps HEDGE_ETF on the reading.
    caplog.set_level(logging.INFO)
    series = _bench_series()
    series["AAPL"] = _series(1.5)
    series["PSQ"] = _series(-1.2)
    o = _read_orch(BookBeta(_Broker(series)))
    o.cfg.hedge_etf = "PSQ"
    o._read_book_beta(_acct([_eq("AAPL", 50_000), _eq("PSQ", 10_000)]))
    lines = _msgs(caplog, "BOOK BETA:")
    assert len(lines) == 1
    assert " hedge=PSQ w=0.100 beta=" in lines[0] and " unhedged=" in lines[0]
    persisted = PortfolioState(path=o.state.path).get_book_beta()
    assert persisted["hedge_etf"] == "PSQ"
    assert abs(persisted["unhedged_spy"] - 0.5 * shrink(1.5)) < 1e-4


def test_post_exec_beta_line_logged(caplog):
    caplog.set_level(logging.INFO)
    series = _bench_series()
    series["AAPL"] = _series(1.5)
    series["HOT"] = _series(2.0)
    o = _read_orch(BookBeta(_Broker(series), hedge_etf="PSQ"))
    o.cfg.hedge_etf = "PSQ"
    o.cfg.auto_hedge_mode = "beta"
    o.cfg.hedge_beta_target = 1.0
    o.cfg.hedge_beta_band = 0.15
    acct = _acct([_eq("AAPL", 50_000)])
    o._read_book_beta(acct)
    pre = o._book_beta_reading.spy                     # 0.5 x 1.35 = 0.675: hold zone
    # A same-cycle buy lands mid-cycle: HOT at 40% of equity pushes the
    # book to 0.675 + 0.4 x 1.8 = 1.395 — across the 1.15 arm line the
    # hedge sized BEFORE execution never saw.
    acct.positions.append(_eq("HOT", 40_000))
    acct.cash -= 40_000
    o._log_post_exec_beta(acct)
    lines = _msgs(caplog, "BOOK BETA (post-exec):")
    assert len(lines) == 1
    post = 0.5 * shrink(1.5) + 0.4 * shrink(2.0)
    assert lines[0].startswith(f"BOOK BETA (post-exec): spy={post:.2f} qqq=")
    assert " invested=90.0%" in lines[0]
    assert f" hedge=PSQ w=0 unhedged={post:.2f}" in lines[0]
    assert lines[0].endswith(
        f"(pre-exec spy={pre:.2f} delta={post - pre:+.2f}; CROSSING pre=hold post=arm)"
    )
    # Same side of every line -> no CROSSING tag.
    caplog.clear()
    o._book_beta_reading = SimpleNamespace(spy=post - 0.01)
    o._log_post_exec_beta(acct)
    lines = _msgs(caplog, "BOOK BETA (post-exec):")
    assert len(lines) == 1 and "CROSSING" not in lines[0] and "delta=+0.01)" in lines[0]
    # Reader blind pre-exec -> nothing (there is no pre-exec line to pair with).
    caplog.clear()
    o._book_beta_reading = None
    o._log_post_exec_beta(acct)
    assert _msgs(caplog, "BOOK BETA (post-exec):") == []
    # Reader blind post-exec -> an 'unavailable' line, never a raise.
    o._book_beta_reading = SimpleNamespace(spy=pre)
    # (fix-pass: the post-exec read now passes on_progress= for liveness, so
    # the fake accepts the kwarg — same widening as test_core_defense's fake.)
    o.book_beta = SimpleNamespace(
        read=lambda a, on_progress=None: SimpleNamespace(available=False, reason="feed down"))
    o._log_post_exec_beta(acct)
    assert _msgs(caplog, "BOOK BETA (post-exec): unavailable (feed down)")


# --------------------------------------------------------------------------- #
# 4a-17: HEDGE COUNTERFACTUAL line + last_unwind state
# --------------------------------------------------------------------------- #
def test_last_unwind_state_round_trip_and_session_count(tmp_path):
    p = tmp_path / "rs.json"
    st = PortfolioState(path=p)
    assert st.get_last_unwind() == {} and st.mark_unwind_session("2026-09-10") == 0
    st.set_last_unwind("psq", 10283.2, 25.84, "2026-09-09", "2026-09-09 09:22")
    assert st.mark_unwind_session("2026-09-09") == 0            # the unwind day
    assert st.mark_unwind_session("2026-09-10") == 1
    assert st.mark_unwind_session("2026-09-10") == 1            # same session, same index
    st2 = PortfolioState(path=p)                                # restart keeps the count
    lu = st2.get_last_unwind()
    assert lu["symbol"] == "PSQ" and lu["qty"] == 10283.2 and lu["price"] == 25.84
    assert lu["sessions"] == ["2026-09-10"]
    assert st2.mark_unwind_session("2026-09-11") == 2
    for i, d in enumerate(["2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17"], start=3):
        assert st2.mark_unwind_session(d) == i
    # Past the window the list stops growing.
    assert st2.mark_unwind_session("2026-09-18") == 7
    assert len(st2.get_last_unwind()["sessions"]) == 6
    # A pre-S-8 risk_state.json (no key) loads clean.
    (tmp_path / "old.json").write_text('{"peak_equity": 1.0}', encoding="utf-8")
    assert PortfolioState(path=tmp_path / "old.json").get_last_unwind() == {}


def test_hedge_counterfactual_logged_for_five_sessions_after_unwind(caplog):
    caplog.set_level(logging.INFO)
    o = _beta_orch([0.80])
    o._et_now = lambda: datetime(2026, 9, 9, 9, 22, tzinfo=_ET)
    o._cycle_seq = 1
    o._apply_auto_hedge(_hedge_acct(psq_value=100_000.0))      # 1000 sh @ 100 unwound
    assert o.broker.closed == ["PSQ"]
    lu = PortfolioState(path=o.state.path).get_last_unwind()
    assert (lu["symbol"], lu["qty"], lu["price"], lu["date"], lu["at"]) == (
        "PSQ", 1000.0, 100.0, "2026-09-09", "2026-09-09 09:22")
    assert _msgs(caplog, "HEDGE COUNTERFACTUAL:") == []         # the unwind cycle itself
    # Later cycle the same day = session 0; PSQ rallied after the exit.
    o.broker.latest_price = lambda s: 104.0
    _next_cycle(o)
    o._apply_auto_hedge(_hedge_acct())                          # no hedge held now
    assert _msgs(caplog, "HEDGE COUNTERFACTUAL:") == [
        "HEDGE COUNTERFACTUAL: last unwind lot 1000 sh @100.00 would be +$4,000 "
        "today (PSQ 104.00 now; unwound 2026-09-09 09:22 ET; session 0/5)."
    ]
    o._apply_auto_hedge(_hedge_acct())                          # breadth re-arm's 2nd call: no-op
    assert len(_msgs(caplog, "HEDGE COUNTERFACTUAL:")) == 1
    # Five sessions log (one line per cycle, two cycles each); the sixth does
    # not; a restart mid-way keeps the session index.
    plan = [(10, 102.0), (11, 99.0), (14, 101.0), (15, 100.5), (16, 103.0), (17, 110.0)]
    for i, (d, px) in enumerate(plan, start=1):
        o._et_now = lambda d=d: datetime(2026, 9, d, 10, 0, tzinfo=_ET)
        o.broker.latest_price = lambda s, px=px: px
        if i == 3:
            o.state = PortfolioState(path=o.state.path)
        for _ in range(2):
            _next_cycle(o)
            o._apply_auto_hedge(_hedge_acct())
    m = _msgs(caplog, "HEDGE COUNTERFACTUAL:")
    assert len(m) == 1 + 5 * 2
    assert m[3] == ("HEDGE COUNTERFACTUAL: last unwind lot 1000 sh @100.00 would be -$1,000 "
                    "today (PSQ 99.00 now; unwound 2026-09-09 09:22 ET; session 2/5).")
    assert m[5].endswith("session 3/5).") and m[-1].endswith("session 5/5).")
    assert not any("session 6/5" in x for x in m)
    # Hedge on again: priced at the held ETF's snapshot price, no quote fetch.
    caplog.clear()
    o.broker.latest_price = lambda s: (_ for _ in ()).throw(RuntimeError("no fetch"))
    o2 = _beta_orch([1.00])
    o2.state = o.state
    o2._et_now = lambda: datetime(2026, 9, 10, 12, 0, tzinfo=_ET)
    o2._cycle_seq = 99
    o2._apply_auto_hedge(_hedge_acct(psq_value=50_000.0))       # PSQ @ 100 held
    assert _msgs(caplog, "HEDGE COUNTERFACTUAL:") == [
        "HEDGE COUNTERFACTUAL: last unwind lot 1000 sh @100.00 would be +$0 "
        "today (PSQ 100.00 now; unwound 2026-09-09 09:22 ET; session 1/5)."
    ]


# --------------------------------------------------------------------------- #
# 4a-17: post-mortem counters
# --------------------------------------------------------------------------- #
def _rec(ts, symbol, action, **kw):
    return SimpleNamespace(
        ts=ts, symbol=symbol, action=action,
        entry_signals=kw.pop("entry_signals", []),
        exit_reason=kw.pop("exit_reason", ""),
        cost_usd=kw.pop("cost_usd", 0.0), **kw,
    )


def test_postmortem_hedge_diagnostics_whipsaw_and_cap_arm_pairs(tmp_path):
    utc = timezone.utc
    rows = [   # run-6 fixture: Sep 3 arm, Sep 9 09:22 ET unwind, Sep 10 11:06 ET re-arm
        _rec(datetime(2026, 9, 3, 14, 14, tzinfo=utc), "PSQ", "buy",
             entry_signals=["auto_hedge"], cost_usd=203605.0),
        _rec(datetime(2026, 9, 9, 13, 22, tzinfo=utc), "PSQ", "sell", exit_reason="hedge_unwind"),
        _rec(datetime(2026, 9, 10, 15, 6, tzinfo=utc), "PSQ", "buy",
             entry_signals=["auto_hedge"], cost_usd=204391.0),
    ]
    ledger = SimpleNamespace(effective=lambda: rows)
    (tmp_path / "bot.log").write_text("\n".join([
        "2026-09-10 08:30:11,519 INFO orchestrator | BOOK BETA: spy=1.06 qqq=0.72 iwm=0.87 invested=58.3%",
        "2026-09-10 08:35:03,311 WARNING risk | BOOK BETA CAP: INTC 8.0% -> 5.0% (book 1.28 -> 1.20)",
        "2026-09-10 09:22:21,856 INFO orchestrator | BOOK BETA: spy=1.13 qqq=0.79 iwm=0.94 invested=60.8%",
        "2026-09-10 10:14:32,193 INFO orchestrator | BOOK BETA: spy=1.13 qqq=0.80 iwm=0.94 invested=60.9%",
        "2026-09-10 10:19:19,691 WARNING risk | BOOK BETA CAP: SMCI 8.0% -> 2.6% (book 1.34 -> 1.20)",
        "2026-09-10 10:20:00,000 INFO orchestrator | BOOK BETA (post-exec): spy=1.20 qqq=0.85 iwm=0.99 invested=64.0% unhedged=1.20",
        "2026-09-10 11:06:12,000 INFO orchestrator | BOOK BETA: spy=1.20 qqq=0.85 iwm=0.99 invested=64.0%",
        "2026-09-10 11:06:39,475 WARNING orchestrator | AUTO-HEDGE: beta: book spy-beta 1.20 > target 1.00 + 0.15 band — bought $204391 of PSQ (hedge $204391/$204391, ceiling 40% of equity).",
        "2026-09-10 11:58:00,000 INFO orchestrator | BOOK BETA: spy=0.90 qqq=0.60 iwm=0.80 invested=64.0%",
        "2026-09-10 11:58:57,055 INFO orchestrator | HEDGE COUNTERFACTUAL: last unwind lot 10283.2 sh @25.84 would be +$2,571 today (PSQ 26.09 now; unwound 2026-09-09 09:22 ET; session 1/5).",
        "2026-09-09 09:22:41,958 WARNING orchestrator | AUTO-HEDGE UNWIND: beta: (other day: ignored)",
    ]), encoding="utf-8")
    lines = pm_mod.hedge_diagnostics("2026-09-10", ledger, log_dir=tmp_path)
    assert lines[0] == (
        "HEDGE WHIPSAW: 1 of 1 arm(s) today re-armed within 2 sessions of an unwind "
        "(PSQ unwound 2026-09-09 09:22 ET -> re-armed 2026-09-10 11:06 ET, 1 session(s), $204,391)."
    )
    assert lines[1] == (
        "BOOK BETA CAP -> next-cycle arm: 1 pair(s) today (cycles 5; cap-bound cycles 2; arms 1)."
    )
    assert lines[2].startswith(
        "HEDGE COUNTERFACTUAL: last unwind lot 10283.2 sh @25.84 would be +$2,571 today")
    # Sep 3: an arm with no prior unwind is not a whipsaw; no log for that
    # day -> 'n/a', never a silent zero.
    lines = pm_mod.hedge_diagnostics("2026-09-03", ledger, log_dir=tmp_path)
    assert lines[0].startswith("HEDGE WHIPSAW: 0 of 1 arm(s) today")
    assert "no unwind within the window" in lines[0]
    assert lines[1] == "BOOK BETA CAP -> next-cycle arm: n/a (no log lines for 2026-09-03)."
    assert len(lines) == 2
    # Fri unwind -> Wed re-arm = 3 weekday sessions: outside the 2-session bar.
    rows2 = [
        _rec(datetime(2026, 9, 11, 14, 0, tzinfo=utc), "PSQ", "sell", exit_reason="hedge_unwind"),
        _rec(datetime(2026, 9, 16, 14, 0, tzinfo=utc), "PSQ", "buy",
             entry_signals=["auto_hedge"], cost_usd=1.0),
    ]
    lines = pm_mod.hedge_diagnostics(
        "2026-09-16", SimpleNamespace(effective=lambda: rows2), log_dir=tmp_path)
    assert lines[0].startswith("HEDGE WHIPSAW: 0 of 1 arm(s)")
    d = datetime(2026, 9, 11).date()
    assert pm_mod._weekday_sessions_between(d, datetime(2026, 9, 14).date()) == 1
    assert pm_mod._weekday_sessions_between(d, datetime(2026, 9, 16).date()) == 3
    assert pm_mod._weekday_sessions_between(d, d) == 0
    # No arm today at all.
    assert pm_mod.hedge_diagnostics(
        "2026-09-08", ledger, log_dir=tmp_path)[0] == "HEDGE WHIPSAW: 0 (no auto-hedge arm today)."


def test_run_postmortem_dry_run_includes_hedge_counters(tmp_path, capsys, monkeypatch):
    from tests.test_postmortem import _FakeJournal, _FakeLedger
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    result = pm_mod.run_postmortem(None, _FakeLedger(), _FakeJournal(),
                                   day="2026-07-10", dry_run=True)
    assert result == {"summary_md": "(dry-run)", "lessons": []}
    out = capsys.readouterr().out
    assert "## Behavior diagnostics (deterministic — trusted)" in out
    assert "HEDGE WHIPSAW: 0 (no auto-hedge arm today)." in out
    assert "BOOK BETA CAP -> next-cycle arm: n/a (no log lines for 2026-07-10)." in out
