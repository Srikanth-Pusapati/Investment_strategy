"""Run-6 change-set — review fixes (Aug 26 review of the 8-item change-set).

Major:  (1) beta shrinkage toward sign(beta) so PSQ measures ~ -1.1 in the book
        (2) bearish precheck falls back to the discovery lean when the
            run-6 composite is None
        (3) Finnhub insider signals keep their pre-taxonomy 30d lag
            (SOURCE_LAG_DAYS / FINNHUB_INSIDER_LAG_DAYS)
        (4) same-cycle option fallback not queued for single names the
            bullish gate rejects unconditionally
Minor:  close row only from the 16:xx ET tick ('late' otherwise), holiday
        skip, config enum validation, put blackout knob, rotation guard
        stop-reached path, history env parse, one REST read per fill,
        fill_ts None without filled_at.
"""
from __future__ import annotations

import logging
import os
import sys
import tempfile
import uuid
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from investment_strategy.ledger import TradeLedger, TradeRecord  # noqa: E402
from investment_strategy.models import (  # noqa: E402
    RiskVerdict, Signal, SignalBundle, SignalKind,
)
from investment_strategy.orchestrator import Orchestrator  # noqa: E402
from investment_strategy.portfolio.beta import (  # noqa: E402
    BookBeta, hedge_target_notional, shrink,
)
from investment_strategy.signals import history as hist  # noqa: E402
from investment_strategy.signals.composite import composite_score  # noqa: E402
from investment_strategy.signals.history import lag_weight  # noqa: E402
from test_risk import _account, _limits, _rm  # noqa: E402
from test_run6_beta import _Broker, _acct, _bench_series, _eq, _series  # noqa: E402
from test_run6_options import _eq_orch, _long_put, _put_spread  # noqa: E402
from test_run6_options import _buy as _obuy  # noqa: E402

_ET = ZoneInfo("America/New_York")
_REQ = {"ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s", "ANTHROPIC_API_KEY": "a"}


def _tmp(suffix: str) -> str:
    return os.path.join(tempfile.gettempdir(), f"_r6rf_{uuid.uuid4().hex}{suffix}")


# --------------------------------------------------------------------------- #
# Major 1 — shrink toward the instrument's own sign
# --------------------------------------------------------------------------- #
def test_shrink_pulls_toward_sign_of_raw_beta():
    assert abs(shrink(1.5) - 1.4) < 1e-9        # unchanged for longs
    assert abs(shrink(-1.2) - (-1.16)) < 1e-9   # was -0.76 (toward +1)
    assert abs(shrink(-0.5) - (-0.6)) < 1e-9
    assert abs(shrink(0.0) - 0.2) < 1e-9
    assert shrink(None) is None


def test_psq_measures_near_minus_one_in_the_book_and_hedge_math_closes():
    series = _bench_series()
    series["HOT"] = _series(1.5)
    series["PSQ"] = _series(-1.1)
    bb = BookBeta(_Broker(series))
    psq = bb.beta_of("PSQ", "SPY")
    assert -1.15 < psq < -1.0                      # ~ -1.08, not -0.68
    # book: 80% HOT (beta 1.4 shrunk) -> 1.12; buy the gap in PSQ and the
    # MEASURED beta lands at/below target instead of 24% above it.
    equity = 100_000.0
    r = bb.read(_acct([_eq("HOT", 80_000)], equity))
    gap = hedge_target_notional(r.spy, 1.0, equity, 40.0)
    assert gap > 0
    r2 = BookBeta(_Broker(series)).read(
        _acct([_eq("HOT", 80_000), _eq("PSQ", gap)], equity))
    assert r2.spy <= 1.0 + 1e-6
    assert r2.spy > 1.0 - 0.05                     # ~10% over-hedge at most


# --------------------------------------------------------------------------- #
# Major 2 — bearish precheck with a None composite
# --------------------------------------------------------------------------- #
def _lean_orch():
    o = Orchestrator.__new__(Orchestrator)
    o.cfg = SimpleNamespace(screener=SimpleNamespace(
        min_score=0.25, bearish_reserve_bar=0.4))
    return o


def _bundle(symbol, *sigs):
    b = SignalBundle(symbol=symbol, signals=list(sigs))
    b.composite_score = composite_score(b)
    return b


def test_bear_precheck_falls_back_to_discovery_lean_without_composite():
    o = _lean_orch()
    disc_only = _bundle("SELLR", Signal(kind=SignalKind.DISCOVERY, symbol="SELLR",
                                        summary="insider sells", score=-0.6))
    assert disc_only.composite_score is None
    comp_bear = _bundle("BEAR", Signal(kind=SignalKind.TECHNICAL, symbol="BEAR",
                                       summary="t", score=-0.9))
    comp_bull = _bundle("BULL", Signal(kind=SignalKind.TECHNICAL, symbol="BULL",
                                       summary="t", score=0.9))
    disc_bull = _bundle("HYPE", Signal(kind=SignalKind.DISCOVERY, symbol="HYPE",
                                       summary="mover", score=0.6))
    comps = {"BEAR": comp_bear.composite_score, "BULL": comp_bull.composite_score}
    names = o._bear_precheck_names([disc_only, comp_bear, comp_bull, disc_bull], comps)
    assert names == ["SELLR", "BEAR"]
    # the composites map is authoritative when present (bundle attr is the fallback)
    assert o._bear_precheck_names([comp_bear], {"BEAR": 0.5}) == []


# --------------------------------------------------------------------------- #
# Major 3 — Finnhub insider keeps its pre-taxonomy lag
# --------------------------------------------------------------------------- #
def test_finnhub_insider_source_lag_override_and_composite_weight():
    assert hist.SOURCE_LAG_DAYS["finnhub-insider"] == 30.0
    assert lag_weight(SignalKind.INSIDER) == 0.91                     # edgar
    assert lag_weight(SignalKind.INSIDER, "sec-edgar") == 0.91
    assert lag_weight(SignalKind.INSIDER, "finnhub-insider") == 0.23  # = old CONGRESS
    fin = SignalBundle(symbol="X", signals=[Signal(
        kind=SignalKind.INSIDER, symbol="X", summary="s", score=1.0,
        source="finnhub-insider")])
    edg = SignalBundle(symbol="X", signals=[Signal(
        kind=SignalKind.INSIDER, symbol="X", summary="s", score=1.0,
        source="sec-edgar")])
    assert composite_score(fin) == 0.23
    assert composite_score(edg) == 0.91
    # one lag per kind still reduces to mean(scores) x lag_weight(kind)
    two = SignalBundle(symbol="X", signals=[
        Signal(kind=SignalKind.NEWS, symbol="X", summary="s", score=1.0),
        Signal(kind=SignalKind.NEWS, symbol="X", summary="s", score=0.0),
    ])
    assert composite_score(two) == round(0.5 * lag_weight(SignalKind.NEWS), 2)


def test_config_mirrors_finnhub_insider_lag_knob():
    from investment_strategy.config import load_config
    env = {k: v for k, v in os.environ.items() if k != "FINNHUB_INSIDER_LAG_DAYS"}
    env.update(_REQ)
    with patch.dict(os.environ, env, clear=True):
        assert load_config().finnhub_insider_lag_days == 30.0
    with patch.dict(os.environ, {**env, "FINNHUB_INSIDER_LAG_DAYS": "2"}, clear=True):
        assert load_config().finnhub_insider_lag_days == 2.0


# --------------------------------------------------------------------------- #
# Major 4 — option fallback queue respects the single-name bullish gate
# --------------------------------------------------------------------------- #
def test_fallback_queue_skips_single_names_when_bullish_knob_off():
    o = _eq_orch("Overextended: RSI 70 and 4.2xATR above the 20d SMA.")
    o.cfg.risk.options_single_name_bullish = False
    o._handle_equity(_obuy("HL"), _account())
    assert o._option_fallbacks == []
    o._handle_equity(_obuy("QQQ"), _account())          # index: still queued
    assert [p.symbol for p, _ in o._option_fallbacks] == ["QQQ"]
    o.cfg.put_proxy_etf = "XLK"                          # configured ETF too
    assert o._option_fallback_allowed("XLK") and not o._option_fallback_allowed("HL")


def test_fallback_queue_takes_single_names_when_bullish_knob_on():
    o = _eq_orch("Overextended: RSI 70 and 4.2xATR above the 20d SMA.")
    o.cfg.risk.options_single_name_bullish = True
    o._handle_equity(_obuy("HL"), _account())
    assert [p.symbol for p, _ in o._option_fallbacks] == ["HL"]


# --------------------------------------------------------------------------- #
# Minor — close row timing, 'late' basis, holiday skip
# --------------------------------------------------------------------------- #
def _hist_orch(trading_day=None):
    from test_run6_measurement import _orch_with_history
    o = _orch_with_history()
    o.cfg.equity_close_fixed_stamp = True
    o.broker.is_trading_day = lambda d: trading_day
    return o


def test_close_row_only_from_the_16xx_tick_else_late_once():
    o = _hist_orch(trading_day=True)
    o._refresh_closing_snapshot(now_et=datetime(2026, 8, 31, 22, 0, tzinfo=_ET))
    rows = o.equity_history.all()
    assert len(rows) == 1 and rows[0]["basis"] == "late"
    # written once: a later tick does not re-stamp, and no 'close' row follows
    o._refresh_closing_snapshot(now_et=datetime(2026, 8, 31, 23, 30, tzinfo=_ET))
    assert len(o.equity_history.all()) == 1
    assert not o.equity_history.has_close_row("2026-08-31")
    assert o.equity_history.has_close_row("2026-08-31", ("close", "late"))
    # the bell tick on another day mints the real thing
    o._refresh_closing_snapshot(now_et=datetime(2026, 9, 1, 16, 5, tzinfo=_ET))
    assert [r["basis"] for r in o.equity_history.all()] == ["late", "close"]


def test_close_row_skipped_on_a_holiday_and_fails_open_without_calendar():
    o = _hist_orch(trading_day=False)                   # Labor Day
    o._refresh_closing_snapshot(now_et=datetime(2026, 9, 7, 16, 1, tzinfo=_ET))
    assert o.equity_history.all() == []
    o = _hist_orch(trading_day=None)                    # calendar read failed
    o._refresh_closing_snapshot(now_et=datetime(2026, 9, 8, 16, 1, tzinfo=_ET))
    assert [r["basis"] for r in o.equity_history.all()] == ["close"]


def test_alpaca_is_trading_day_reads_calendar_and_fails_open():
    from investment_strategy.execution.alpaca_client import AlpacaClient
    c = AlpacaClient.__new__(AlpacaClient)
    c.trading = SimpleNamespace(get_calendar=lambda req: [SimpleNamespace(date="2026-09-08")])
    assert c.is_trading_day(date(2026, 9, 8)) is True
    assert c.is_trading_day(date(2026, 9, 7)) is False
    c.trading = SimpleNamespace(get_calendar=lambda req: 1 / 0)
    with patch("investment_strategy.execution.alpaca_client._retry_read",
               side_effect=RuntimeError("down")):
        assert c.is_trading_day(date(2026, 9, 8)) is None


# --------------------------------------------------------------------------- #
# Minor — config enum validation
# --------------------------------------------------------------------------- #
def test_config_enum_typos_warn_and_use_run6_default(caplog):
    from investment_strategy.config import load_config
    env = {k: v for k, v in os.environ.items()
           if k not in ("LLM_SELL_AUTHORITY", "AUTO_HEDGE_MODE")}
    env.update(_REQ)
    with patch.dict(os.environ, {**env, "LLM_SELL_AUTHORITY": "event_only",
                                 "AUTO_HEDGE_MODE": "Beta "}, clear=True):
        with caplog.at_level(logging.WARNING, logger="config"):
            cfg = load_config()
    assert cfg.risk.llm_sell_authority == "events_only"
    assert cfg.auto_hedge_mode == "beta"
    msgs = [r.getMessage() for r in caplog.records]
    assert any("LLM_SELL_AUTHORITY='event_only'" in m for m in msgs), msgs
    assert not any("AUTO_HEDGE_MODE" in m for m in msgs)    # 'Beta ' normalises fine
    with patch.dict(os.environ, {**env, "AUTO_HEDGE_MODE": "bteа"}, clear=True):
        with caplog.at_level(logging.WARNING, logger="config"):
            cfg = load_config()
    assert cfg.auto_hedge_mode == "beta"
    assert any("AUTO_HEDGE_MODE=" in r.getMessage() for r in caplog.records)
    with patch.dict(os.environ, {**env, "LLM_SELL_AUTHORITY": " Full "}, clear=True):
        assert load_config().risk.llm_sell_authority == "full"


# --------------------------------------------------------------------------- #
# Minor — put blackout is an explicit knob (default: puts blocked too)
# --------------------------------------------------------------------------- #
def test_option_blackout_puts_knob():
    base = dict(options_enabled=True, earnings_blackout_days=3,
                per_underlying_premium_pct=0.0)
    rm = _rm(_limits(**base))
    d = rm.evaluate_option(_long_put("DKS"), _account(), est_premium_per_contract=2.0,
                           market_trend="down", days_to_earnings=1)
    assert d.verdict is RiskVerdict.REJECTED and "Earnings in 1d" in d.reason
    rm = _rm(_limits(**base, earnings_blackout_puts=False))
    d = rm.evaluate_option(_long_put("DKS"), _account(), est_premium_per_contract=2.0,
                           market_trend="down", days_to_earnings=1)
    assert d.verdict is RiskVerdict.APPROVED, d.reason
    d = rm.evaluate_option(_put_spread("DKS"), _account(), est_premium_per_contract=2.0,
                           market_trend="down", days_to_earnings=1)
    assert d.verdict is RiskVerdict.APPROVED, d.reason
    # calls stay blocked whatever the put knob says (knob on for the call gate)
    from test_run6_options import _long_call
    rm = _rm(_limits(**base, earnings_blackout_puts=False,
                     options_single_name_bullish=True))
    d = rm.evaluate_option(_long_call("NVDA"), _account(), est_premium_per_contract=2.0,
                           days_to_earnings=1)
    assert d.verdict is RiskVerdict.REJECTED and "Earnings in 1d" in d.reason


def test_config_blackout_puts_default_on():
    from investment_strategy.config import load_config
    env = {k: v for k, v in os.environ.items() if k != "OPTIONS_BLACKOUT_PUTS"}
    env.update(_REQ)
    with patch.dict(os.environ, env, clear=True):
        assert load_config().risk.earnings_blackout_puts is True
    with patch.dict(os.environ, {**env, "OPTIONS_BLACKOUT_PUTS": "off"}, clear=True):
        assert load_config().risk.earnings_blackout_puts is False


# --------------------------------------------------------------------------- #
# Minor — rotation guard: a stop-reached loser still earns the release ladder
# --------------------------------------------------------------------------- #
def test_rotation_guard_stop_reached_loser_goes_through_the_ladder():
    from test_run6_sell_authority import _orch as _sa_orch, _acct as _sa_acct
    from test_run6_sell_authority import _pos as _sa_pos, _rot_props
    o = _sa_orch()
    o.state.register_buy("LOSER", conviction=0.5)
    o.state.register_stop_width("LOSER", 8.0)
    acct = _sa_acct(_sa_pos("LOSER", pl_pct=-10.0), _sa_pos("KEEP", pl_pct=5.0))
    kept = o._apply_rotation_guard(_rot_props(), acct, {})
    # no conviction edge (0.55 vs 0.5 < +0.10) -> vetoed, exactly as legacy
    assert [p.symbol for p in kept] == ["NEW"]
    assert o.journal_records[0].verdict == "rotation_guard"
    # stop NOT reached (width 12) -> straight to the risk layer as before
    o2 = _sa_orch()
    o2.state.register_buy("LOSER", conviction=0.5)
    o2.state.register_stop_width("LOSER", 12.0)
    kept = o2._apply_rotation_guard(_rot_props(), acct, {})
    assert [p.symbol for p in kept] == ["LOSER", "NEW"]


# --------------------------------------------------------------------------- #
# Minor — history env parse never raises at import
# --------------------------------------------------------------------------- #
def test_history_env_parse_falls_back_on_garbage(caplog):
    with patch.dict(os.environ, {"SIGNAL_HISTORY_RETENTION_DAYS": "ninety"}):
        with caplog.at_level(logging.WARNING, logger="signals.history"):
            assert hist._env_num("SIGNAL_HISTORY_RETENTION_DAYS", 120.0) == 120.0
    assert any("SIGNAL_HISTORY_RETENTION_DAYS" in r.getMessage() for r in caplog.records)
    with patch.dict(os.environ, {"SIGNAL_HISTORY_MAX_POINTS": " 12 "}):
        assert hist._env_num("SIGNAL_HISTORY_MAX_POINTS", 480, int) == 12
    with patch.dict(os.environ, {"SIGNAL_HISTORY_MAX_POINTS": ""}):
        assert hist._env_num("SIGNAL_HISTORY_MAX_POINTS", 480, int) == 480


# --------------------------------------------------------------------------- #
# Minor — one REST read per FILLED order; fill_ts None without filled_at
# --------------------------------------------------------------------------- #
def test_reconcile_uses_order_fill_full_once_and_stamps():
    from test_orchestrator import _orch
    from test_run6_measurement import _reconcile_msgs, _rec
    o = _orch()
    o.cfg.ledger_fill_prices = True
    o.ledger = TradeLedger(path=_tmp(".jsonl"))
    o.ledger.record(_rec(symbol="AAPL", action="buy", qty=3.0, entry_price=100.0,
                         cost_usd=300.0, order_id="oid-1"))
    calls = []
    ts = datetime(2026, 8, 31, 14, 0, tzinfo=timezone.utc)
    o.broker.order_fill_full = lambda oid: (
        calls.append(oid) or ("filled", 3.0, 3.0,
                              {"price": 100.5, "qty": 3.0, "filled_at": ts}))
    o.broker.order_fill = lambda oid: 1 / 0            # must not be called
    o.broker.order_fill_detail = lambda oid: 1 / 0     # must not be called
    o._pending_oids = [("oid-1", "AAPL")]
    msgs = _reconcile_msgs(o)
    assert calls == ["oid-1"]
    assert any("@ 100.5000 x 3 [ledger stamped]" in m for m in msgs), msgs
    row = o.ledger.effective()[0]
    assert row.fill_price == 100.5 and row.fill_ts == ts


def test_alpaca_order_fill_wrappers_share_one_read():
    from investment_strategy.execution.alpaca_client import AlpacaClient
    c = AlpacaClient.__new__(AlpacaClient)
    reads = []
    order = SimpleNamespace(status=SimpleNamespace(value="FILLED"), filled_qty="2",
                            qty="2", filled_avg_price="10.5", filled_at=None)
    c.trading = SimpleNamespace(get_order_by_id=lambda oid: reads.append(oid) or order)
    assert c.order_fill_full("x") == ("filled", 2.0, 2.0,
                                      {"price": 10.5, "qty": 2.0, "filled_at": None})
    assert c.order_fill("x") == ("filled", 2.0, 2.0)
    assert c.order_fill_detail("x")["price"] == 10.5
    assert len(reads) == 3
    c.trading = SimpleNamespace(get_order_by_id=lambda oid: 1 / 0)
    assert c.order_fill_full("x") == ("unknown", 0.0, 0.0, {})


def test_set_fill_without_filled_at_writes_none_not_now():
    led = TradeLedger(path=_tmp(".jsonl"))
    led.record(TradeRecord(symbol="AAPL", action="buy", qty=1.0, entry_price=10.0,
                           cost_usd=10.0, order_id="oid-1"))
    assert led.set_fill("oid-1", 10.2, 1.0) is True
    row = led.effective()[0]
    assert row.fill_price == 10.2 and row.fill_ts is None
