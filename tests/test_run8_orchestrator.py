"""Run-8 change-set — orchestrator-side items: B-3 open-window deferral, B-4
bearish IGNORED fallback, B-5 entry-buffer tier, B-6 put no-quote cooldown,
B-8 premium-cap slate exclusion, C-2 FEEDS cached token, C-4 IGNORED audit,
C-5/C-4 ledger stamps, C-9 post-mortem restart guard. Bare Orchestrator
(``__new__``) + fakes, the pattern of tests/test_all_weather.py."""
from __future__ import annotations

import logging
import os
import sys
import tempfile
import threading
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy import session_calendar as _sc
from investment_strategy.ledger import TradeRecord
from investment_strategy.models import (
    AccountSnapshot, Action, Instrument, OptionLeg, OptionStrategy, Position,
    RiskDecision, RiskVerdict, TradeProposal,
)
from investment_strategy.orchestrator import Orchestrator
from investment_strategy.state import PortfolioState

ET = ZoneInfo("America/New_York")
TUE = datetime(2026, 10, 27, 9, 33, tzinfo=ET)        # a weekday, 3 min after the open


class _Journal:
    def __init__(self):
        self.recs = []
        self._today = []

    def record(self, rec):
        self.recs.append(rec)

    def today(self):
        return list(self._today)

    def has_records_today(self):
        return True


class _Broker:
    def __init__(self, prices=None, market_open=True):
        self.prices = prices or {}
        self.market_open = market_open
        self.submitted = []

    def latest_price(self, sym):
        return self.prices.get(sym, 100.0)

    def is_market_open(self):
        return self.market_open

    def submit_option_legs(self, legs, qty=1, est_premium_per_share=None, **kw):
        self.submitted.append({"legs": legs, "qty": qty, "est": est_premium_per_share, **kw})
        return "oid-1"


def _state() -> PortfolioState:
    return PortfolioState(path=os.path.join(tempfile.gettempdir(), f"_r8o_{uuid.uuid4().hex}.json"))


def _orch(monkeypatch, now=TUE, **cfg_over) -> Orchestrator:
    monkeypatch.setattr(Orchestrator, "_et_now", staticmethod(lambda: now))
    monkeypatch.setattr(_sc, "_ACTIVE", None)
    o = Orchestrator.__new__(Orchestrator)
    cfg = dict(
        entry_open_delay_min=0.0, bearish_ignored_fallback="log_only",
        put_noquote_cooldown_cycles=0, premium_cap_slate_exclude=False,
        core_etf="QQQ", hedge_etf="PSQ", defensive_core_etf="",
        put_proxy_etf="IWM", option_strike_snap=False, proxy_put_thesis_gate=True,
        curated_lessons_inject=False, feeds_degraded_modes=True,
        decision_interval_s=3600.0, monitor_interval_s=30, postmortem_enabled=True,
        postmortem_max_lessons=3,
        screener=SimpleNamespace(bearish_reserve_bar=0.4, sources=("insider",)),
        risk=SimpleNamespace(option_entry_tier1_buffer_pct=0.0, options_enabled=True),
    )
    cfg.update(cfg_over)
    o.cfg = SimpleNamespace(**cfg)
    o.state = _state()
    o.journal = _Journal()
    o.broker = _Broker()
    o.options = SimpleNamespace(
        estimate_net_premium=lambda p: 1.0, min_leg_premium=lambda p: 1.0,
        leg_liquidity=lambda p: [], build_legs=lambda p: ["leg"],
    )
    o.engine = SimpleNamespace(last_bear_verdicts={})
    o.risk = SimpleNamespace(limits=SimpleNamespace())
    o.ledger = SimpleNamespace(recorded=[], record=lambda r, _s=None: None)
    o.ledger.record = o.ledger.recorded.append
    o.earnings = None
    o._regime_label = ""
    o._regime_trend = ""
    o._regime_mult = 1.0
    o._hedge_symbol = ""
    o._trade_lock = threading.Lock()
    o._pending_oids = []
    o._deferred_entries = {}
    o._put_noquote_cooldown = {}
    o._premium_cap_excluded = {}
    o._recheck_at_utc = None
    o._next_open_utc = None
    o._cycle_seq = 1
    o._bear_verdict_stage = {}
    o._bear_eligibility = {}
    o._proxy_put_state = ""
    o._bear_puts_proposed = 0
    o._bear_puts_approved = 0
    o._stamp_liveness = lambda: None
    o._propose_proxy_put = lambda *a, **k: None
    import time as _t
    o._last_decision_at = _t.time()
    return o


def _acct(positions=None) -> AccountSnapshot:
    return AccountSnapshot(equity=100_000.0, last_equity=100_000.0, cash=50_000.0,
                           buying_power=50_000.0, positions=positions or [])


def _pos(sym, value=1_000.0) -> Position:
    return Position(symbol=sym, qty=value / 100.0, avg_entry_price=100.0, current_price=100.0,
                    market_value=value, unrealized_pl=0.0, unrealized_pl_pct=0.0)


def _buy(sym, instrument=Instrument.EQUITY) -> TradeProposal:
    kw = {}
    if instrument is Instrument.OPTION:
        kw = dict(option_strategy=OptionStrategy.LONG_PUT,
                  option_legs=[OptionLeg(expiry="2026-11-20", strike=95, right="put", side=Action.BUY)])
    return TradeProposal(symbol=sym, action=Action.BUY, conviction=0.6, target_weight_pct=5.0,
                         rationale="t", instrument=instrument, **kw)


def _sell(sym) -> TradeProposal:
    return TradeProposal(symbol=sym, action=Action.SELL, conviction=0.6, target_weight_pct=0.0,
                         rationale="t")


# --------------------------------------------------------------------------- #
# B-3 — ENTRY_OPEN_DELAY_MIN (run-7: 0 for 12 in the first 15 minutes)
# --------------------------------------------------------------------------- #
def test_b3_fresh_equity_buys_in_the_open_window_are_deferred(monkeypatch, caplog):
    o = _orch(monkeypatch, entry_open_delay_min=15.0)
    o.broker = _Broker(prices={"X": 50.0})
    acct = _acct([_pos("Y")])
    props = [_buy("X"), _buy("Y"), _sell("Z"), _buy("W", Instrument.OPTION), _buy("QQQ")]
    with caplog.at_level(logging.INFO):
        kept = o._defer_open_window_entries(props, acct)
    assert [p.symbol for p in kept] == ["Y", "Z", "W", "QQQ"]       # held / sell / option / system pass
    assert "X" in o._deferred_entries and o._deferred_entries["X"]["price"] == 50.0
    assert o._recheck_at_utc == datetime(2026, 10, 27, 9, 45, tzinfo=ET).astimezone(timezone.utc)
    assert any("ENTRY DEFERRED (open window): X $50.00 at 09:33" in r.message
               and "re-check 09:45" in r.message for r in caplog.records)
    rec = [r for r in o.journal.recs if r.symbol == "X"][0]
    assert rec.verdict == "deferred" and "ENTRY DEFERRED (open window)" in rec.reason


def test_b3_is_inert_after_the_window_and_when_off(monkeypatch):
    late = _orch(monkeypatch, now=TUE.replace(minute=50), entry_open_delay_min=15.0)
    props = [_buy("X")]
    assert late._defer_open_window_entries(props, _acct()) == props
    assert late._recheck_at_utc is None
    off = _orch(monkeypatch, entry_open_delay_min=0.0)
    assert off._defer_open_window_entries(props, _acct()) == props


def test_b3_recheck_stash_makes_the_cycle_due_once(monkeypatch):
    o = _orch(monkeypatch)
    assert o._decision_due() is False
    o._recheck_at_utc = datetime.now(timezone.utc) + timedelta(hours=1)
    assert o._decision_due() is False
    o._recheck_at_utc = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert o._decision_due() is True


def test_b3_result_line_reports_the_move_and_whether_it_was_bought(monkeypatch, caplog):
    o = _orch(monkeypatch, now=TUE.replace(minute=45))
    o.broker = _Broker(prices={"X": 101.0, "V": 98.0})
    o._deferred_entries = {
        "X": {"price": 100.0, "at": "09:33", "date": "2026-10-27", "cycle": 1},
        "V": {"price": 100.0, "at": "09:33", "date": "2026-10-27", "cycle": 1},
        "S": {"price": 100.0, "at": "09:33", "date": "2026-10-27", "cycle": 2},   # this cycle
        "O": {"price": 100.0, "at": "09:33", "date": "2026-10-26", "cycle": 1},   # stale day
    }
    o._cycle_seq = 2
    with caplog.at_level(logging.INFO):
        o._log_deferred_results(_acct([_pos("X")]))
    msgs = [r.message for r in caplog.records if "ENTRY DEFERRED RESULT:" in r.message]
    assert any("X 09:33 $100.00 -> 09:45 $101.00 (+1.00%) bought" in m for m in msgs), msgs
    assert any("V 09:33 $100.00 -> 09:45 $98.00 (-2.00%) dropped" in m for m in msgs), msgs
    assert set(o._deferred_entries) == {"S"}            # same-cycle kept, stale dropped


# --------------------------------------------------------------------------- #
# B-4 — BEARISH_IGNORED_FALLBACK (run-7: 1-30 IGNORED name-cycles per session)
# --------------------------------------------------------------------------- #
def _bundles(*syms):
    return [SimpleNamespace(symbol=s, composite_score=None, signals=[]) for s in syms]


def test_b4_log_only_never_calls_the_engine(monkeypatch):
    o = _orch(monkeypatch, bearish_ignored_fallback="log_only")
    called = []
    o.engine.decide_bearish_fallback = lambda *a, **k: called.append(a) or None
    o._bear_verdict_stage = {"A": "IGNORED"}
    o._run_bearish_ignored_fallback(_acct(), {}, {}, _bundles("A"), {"A": -1.9})
    assert called == []


def test_b4_asks_once_for_the_most_bearish_ignored_name_and_hold_means_no_trade(monkeypatch, caplog):
    o = _orch(monkeypatch, bearish_ignored_fallback="proxy_put")
    called = []
    o.engine.decide_bearish_fallback = lambda bundle, *a, **k: called.append(bundle.symbol) or None
    handled = []
    o._handle_option = lambda *a, **k: handled.append(a)
    o._bear_verdict_stage = {"A": "IGNORED", "B": "IGNORED", "C": "declined: x"}
    o._bear_eligibility = {"A": (True, "below its 200dma"), "B": (True, "x")}
    with caplog.at_level(logging.INFO):
        o._run_bearish_ignored_fallback(_acct(), {}, {}, _bundles("A", "B", "C"),
                                        {"A": -1.2, "B": -1.9})
    assert called == ["B"]                      # most bearish composite, one call
    assert handled == []
    assert any("BEARISH FALLBACK: B IGNORED" in r.message and "veto:none/hold" in r.message
               for r in caplog.records)
    assert any(r.reason.startswith("BEARISH FALLBACK declined") for r in o.journal.recs)


def test_b4_a_returned_put_runs_the_gate_stack(monkeypatch, caplog):
    o = _orch(monkeypatch, bearish_ignored_fallback="proxy_put")
    put = _buy("A", Instrument.OPTION)
    o.engine.decide_bearish_fallback = lambda *a, **k: put
    handled = []
    o._handle_option = lambda prop, *a, **k: handled.append(prop.symbol)
    o._bear_verdict_stage = {"A": "IGNORED"}
    with caplog.at_level(logging.INFO):
        o._run_bearish_ignored_fallback(_acct(), {"A": ["insider"]}, {}, _bundles("A"), {"A": -1.9})
    assert handled == ["A"]
    assert any("BEARISH FALLBACK: A IGNORED" in r.message and "long_put proposed" in r.message
               for r in caplog.records)


def test_b4_daily_cap_and_closed_market_skip(monkeypatch, caplog):
    o = _orch(monkeypatch, bearish_ignored_fallback="proxy_put")
    called = []
    o.engine.decide_bearish_fallback = lambda *a, **k: called.append(1) or None
    o._bear_verdict_stage = {"A": "IGNORED"}
    o.journal._today = [SimpleNamespace(symbol="A", reason="BEARISH FALLBACK declined", instrument="option")] * 2
    with caplog.at_level(logging.INFO):
        o._run_bearish_ignored_fallback(_acct(), {}, {}, _bundles("A"), {"A": -1.9})
    assert called == [] and any("daily cap 2" in r.message for r in caplog.records)
    o.journal._today = []
    o.broker = _Broker(market_open=False)
    o._run_bearish_ignored_fallback(_acct(), {}, {}, _bundles("A"), {"A": -1.9})
    assert called == []


# --------------------------------------------------------------------------- #
# B-6 / B-8 — bearish-slate exclusions (cooldown; premium-cap for the day)
# --------------------------------------------------------------------------- #
def test_b6_b8_excluded_names_leave_the_bearish_precheck_with_one_log_line(monkeypatch, caplog):
    o = _orch(monkeypatch)
    o._put_noquote_cooldown = {"A": 3}
    o._premium_cap_excluded = {"B": "2026-10-27", "D": "2026-10-26"}
    with caplog.at_level(logging.INFO):
        out = o._bear_precheck_names(_bundles("A", "B", "C", "D", "E"),
                                     {"A": -1.0, "B": -1.0, "C": -1.0, "D": -1.0, "E": 0.5})
    assert out == ["C", "D"]                       # D's exclusion was yesterday's
    line = [r.message for r in caplog.records if "Bearish-slate excluded this cycle" in r.message]
    assert line == ["Bearish-slate excluded this cycle: A (PUT NO-QUOTE: cooldown 3 cycles), "
                    "B (option premium cap)"]


def _reject_option_orch(monkeypatch, reason, **cfg):
    o = _orch(monkeypatch, **cfg)
    o.options.leg_liquidity = lambda p: [{"symbol": "X", "oi": 100, "rel_spread_pct": None}]
    o.risk = SimpleNamespace(
        limits=SimpleNamespace(),
        evaluate_option=lambda p, *a, **k: RiskDecision(proposal=p, verdict=RiskVerdict.REJECTED,
                                                        reason=reason),
    )
    return o


def test_b6_no_nbbo_reject_arms_the_cooldown(monkeypatch, caplog):
    o = _reject_option_orch(monkeypatch, "no NBBO on feed for IWM261121P00275000 — ...",
                            put_noquote_cooldown_cycles=8)
    with caplog.at_level(logging.INFO):
        o._handle_option(_buy("IWM", Instrument.OPTION), _acct(), ["insider"])
    assert o._put_noquote_cooldown == {"IWM": 8}
    assert any(r.message.startswith("PUT NO-QUOTE: IWM cooldown 8 cycles") for r in caplog.records)


def test_b6_off_at_zero_and_other_rejects_do_not_arm(monkeypatch):
    o = _reject_option_orch(monkeypatch, "no NBBO on feed for X", put_noquote_cooldown_cycles=0)
    o._handle_option(_buy("IWM", Instrument.OPTION), _acct(), [])
    assert o._put_noquote_cooldown == {}
    o2 = _reject_option_orch(monkeypatch, "Net credit / no debit", put_noquote_cooldown_cycles=8)
    o2._handle_option(_buy("IWM", Instrument.OPTION), _acct(), [])
    assert o2._put_noquote_cooldown == {}


def test_b8_premium_cap_reject_excludes_the_underlying_for_the_day(monkeypatch):
    reason = "PER-UNDERLYING PREMIUM CAP: $4,912 open option premium on HYG + ..."
    o = _reject_option_orch(monkeypatch, reason, premium_cap_slate_exclude=True)
    o._handle_option(_buy("HYG", Instrument.OPTION), _acct(), [])
    assert o._premium_cap_excluded == {"HYG": "2026-10-27"}
    off = _reject_option_orch(monkeypatch, reason, premium_cap_slate_exclude=False)
    off._handle_option(_buy("HYG", Instrument.OPTION), _acct(), [])
    assert off._premium_cap_excluded == {}


# --------------------------------------------------------------------------- #
# B-5 — entry-buffer tier at the submit call site
# --------------------------------------------------------------------------- #
def _approve_option_orch(monkeypatch, liquidity, tier1_pct):
    o = _orch(monkeypatch)
    o.cfg.risk.option_entry_tier1_buffer_pct = tier1_pct
    o.options.leg_liquidity = lambda p: liquidity
    o.risk = SimpleNamespace(
        limits=SimpleNamespace(),
        evaluate_option=lambda p, *a, **k: RiskDecision(
            proposal=p, verdict=RiskVerdict.APPROVED, approved_qty=2.0,
            approved_notional=200.0, reason="2 contract(s)"),
    )
    return o


def test_b5_tier1_structure_passes_the_tight_buffer_and_stamps_the_tier(monkeypatch):
    liq = [{"symbol": "X", "oi": 5_000, "rel_spread_pct": 2.0}]
    o = _approve_option_orch(monkeypatch, liq, 5.0)
    o._handle_option(_buy("HYG", Instrument.OPTION), _acct(), [])
    assert o.broker.submitted[0]["limit_buffer_pct"] == 5.0
    assert o.ledger.recorded[0].entry_limit_tier == 1


def test_b5_wide_or_thin_legs_keep_the_legacy_buffer(monkeypatch):
    for liq in ([{"symbol": "X", "oi": 5_000, "rel_spread_pct": 8.0}],
                [{"symbol": "X", "oi": 300, "rel_spread_pct": 2.0}],
                [{"symbol": "X", "oi": None, "rel_spread_pct": 2.0}]):
        o = _approve_option_orch(monkeypatch, liq, 5.0)
        o._handle_option(_buy("HYG", Instrument.OPTION), _acct(), [])
        assert "limit_buffer_pct" not in o.broker.submitted[0]
        assert o.ledger.recorded[0].entry_limit_tier == 2
    off = _approve_option_orch(monkeypatch, [{"symbol": "X", "oi": 5_000, "rel_spread_pct": 2.0}], 0.0)
    off._handle_option(_buy("HYG", Instrument.OPTION), _acct(), [])
    assert "limit_buffer_pct" not in off.broker.submitted[0]


# --------------------------------------------------------------------------- #
# C-4 — IGNORED audit detail on the warning and the journal row
# --------------------------------------------------------------------------- #
def test_c4_ignored_warning_names_the_returned_verdicts_and_the_eligible_list(monkeypatch, caplog):
    o = _orch(monkeypatch)
    o._bear_eligibility = {"A": (True, "below 200dma"), "B": (True, "x")}
    o.engine.last_bear_verdicts = {"B": ("declined", "no flow")}
    with caplog.at_level(logging.INFO):
        o._reconcile_bear_verdicts([])
    assert o._bear_verdict_stage == {"A": "IGNORED", "B": "declined: no flow"}
    w = [r for r in caplog.records if "ELIGIBLE but IGNORED" in r.message][0]
    assert "[verdicts returned for: B; eligible: A,B]" in w.message
    rec = [r for r in o.journal.recs if r.symbol == "A"][0]
    assert rec.verdict == "put_ignored" and "verdicts returned for: B; eligible: A,B" in rec.reason


# --------------------------------------------------------------------------- #
# C-2 — FEEDS line carries insider=cached(<h>h) instead of UNHEALTHY
# --------------------------------------------------------------------------- #
def test_c2_feeds_line_shows_the_cached_token(monkeypatch):
    from investment_strategy.portfolio.robinhood import RobinhoodReader
    monkeypatch.setattr(RobinhoodReader, "auth_dead", staticmethod(lambda: False))
    o = _orch(monkeypatch)
    o._news_vader_fallback = lambda: False
    o._earnings_source_token = lambda: ""
    o.screeners = SimpleNamespace(screeners=[
        SimpleNamespace(name="insider", enabled=True, degraded=None, cached_age_h=4.2)])
    assert o._feed_health_line() == "FEEDS: 1/1 healthy insider=cached(4h)"
    o.screeners.screeners[0].cached_age_h = None
    assert o._feed_health_line() == "FEEDS: 1/1 healthy"
    o.screeners.screeners[0].degraded = "edgar: timeout"
    assert o._feed_health_line().startswith("FEEDS: 0/1 — insider UNHEALTHY (edgar: timeout)")


# --------------------------------------------------------------------------- #
# C-9 — a restart must not re-run a post-mortem whose file is on disk
# --------------------------------------------------------------------------- #
def test_c9_postmortem_skips_when_the_day_file_exists(monkeypatch, tmp_path, caplog):
    import investment_strategy.postmortem as pm
    from investment_strategy.journal import _trading_day
    monkeypatch.setattr(pm, "_LESSONS_DIR", tmp_path)
    day = _trading_day()
    (tmp_path / f"{day}.md").write_text("# lessons\n")
    monkeypatch.setattr(pm, "run_postmortem", lambda *a, **k: pytest.fail("post-mortem re-ran"))
    o = _orch(monkeypatch)
    with caplog.at_level(logging.INFO):
        o._maybe_run_postmortem()
    assert o.state.get_postmortem_done_day() == day
    assert any("already on disk" in r.message for r in caplog.records)


# --------------------------------------------------------------------------- #
# C-5 / C-4 — ledger stamps on equity buy rows
# --------------------------------------------------------------------------- #
def test_c5_c4_equity_row_carries_entry_minute_and_topup_audit():
    d = RiskDecision(proposal=_buy("LLY"), verdict=RiskVerdict.APPROVED, approved_qty=10.0,
                     approved_notional=1_000.0, stop_loss_pct=5.0, take_profit_pct=10.0,
                     topup_events=["price_gain:+3.0%"])
    starter = TradeRecord.from_equity(d, 100.0, "oid", entry_minute=3, prior_meta=None)
    assert starter.entry_minute == 3 and starter.topup_n == 1 and starter.prior_conviction is None
    top = TradeRecord.from_equity(d, 103.0, "oid2", entry_minute=47,
                                  prior_meta={"conviction": 0.6, "composite": 1.5, "price": 100.0, "n": 1})
    assert (top.topup_n, top.prior_conviction, top.prior_composite, top.prior_fill_price) == (2, 0.6, 1.5, 100.0)
    assert top.topup_events == ["price_gain:+3.0%"]
