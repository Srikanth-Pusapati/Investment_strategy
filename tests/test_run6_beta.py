"""Run-6 item 7 — book beta measurement, beta cap gate, beta-sized hedge."""
from __future__ import annotations

import logging
import os
import sys
import tempfile
import threading
import uuid
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.models import AccountSnapshot, Position, RiskVerdict
from investment_strategy.orchestrator import Orchestrator
from investment_strategy.portfolio.beta import (
    ASSUMED_BETA,
    BookBeta,
    aggregate,
    beta_cap_room,
    daily_returns,
    hedge_signal,
    hedge_target_notional,
    occ_underlying,
    post_trade_beta,
    raw_beta,
    shrink,
)
from investment_strategy.state import PortfolioState
from tests.test_risk import _account, _buy, _limits, _pos, _rm

_REQ = {"ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s", "ANTHROPIC_API_KEY": "a"}


def _env_without(*keys) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in keys}
    env.update(_REQ)
    return env


# --------------------------------------------------------------------------- #
# Synthetic series
# --------------------------------------------------------------------------- #
def _series(mult: float, n: int = 80, start: float = 100.0, offset: float = 0.0):
    """(date, close) pairs whose daily returns are exactly mult x a fixed
    benchmark return pattern (+offset), so the sample beta is `mult`."""
    pattern = [0.01, -0.02, 0.015, -0.005, 0.02, -0.01, 0.007, -0.012]
    pairs, px = [], start
    for i in range(n):
        if i:
            px *= 1.0 + mult * pattern[i % len(pattern)] + offset
        pairs.append((f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}", round(px, 6)))
    return pairs


class _Broker:
    def __init__(self, series: dict, fail: set | None = None):
        self.series = series
        self.fail = fail or set()
        self.calls: list[str] = []

    def daily_close_series(self, symbol, days):
        self.calls.append(symbol)
        if symbol in self.fail:
            raise RuntimeError("feed down")
        return list(self.series.get(symbol, []))[-days:]


def _bench_series():
    return {"SPY": _series(1.0), "QQQ": _series(1.2), "IWM": _series(0.9)}


def _eq(symbol, mv, price=100.0):
    return Position(
        symbol=symbol, qty=mv / price, avg_entry_price=price, current_price=price,
        market_value=mv, unrealized_pl=0.0, unrealized_pl_pct=0.0,
    )


def _acct(positions, equity=100_000.0):
    invested = sum(p.market_value for p in positions)
    return AccountSnapshot(
        equity=equity, last_equity=equity, cash=equity - invested,
        buying_power=equity - invested, positions=positions,
    )


# --------------------------------------------------------------------------- #
# (a) beta math
# --------------------------------------------------------------------------- #
def test_raw_beta_recovers_multiplier_and_shrinks_toward_one():
    b = raw_beta(daily_returns(_series(1.5)), daily_returns(_series(1.0)))
    assert abs(b - 1.5) < 1e-6
    assert abs(shrink(1.5) - 1.4) < 1e-9          # 0.8*1.5 + 0.2
    assert abs(shrink(0.0) - 0.2) < 1e-9
    assert shrink(None) is None


def test_raw_beta_none_on_short_overlap_or_flat_bench():
    assert raw_beta(daily_returns(_series(1.0, n=20)), daily_returns(_series(1.0))) is None
    assert raw_beta({}, daily_returns(_series(1.0))) is None
    flat = {d: 0.0 for d, _ in _series(1.0)[1:]}
    assert raw_beta(daily_returns(_series(1.0)), flat) is None


def test_aggregate_counts_unknown_at_assumed_beta():
    w = {"A": 0.3, "B": 0.2, "PSQ": 0.1}
    assert abs(aggregate(w, {"A": 1.5, "B": None, "PSQ": -1.0}) - (0.45 + 0.2 * ASSUMED_BETA - 0.1)) < 1e-9


def test_post_trade_and_room_arithmetic():
    assert abs(post_trade_beta(1.0, 10_000, 100_000, 2.0) - 1.2) < 1e-9
    assert abs(beta_cap_room(1.0, 1.2, 100_000, 2.0) - 10_000) < 1e-6
    assert beta_cap_room(1.3, 1.2, 100_000, 1.0) == 0.0
    assert beta_cap_room(1.3, 1.2, 100_000, -1.0) is None


def test_occ_underlying():
    assert occ_underlying("IWM260918P00220000") == "IWM"
    assert occ_underlying("AAPL") is None


# --------------------------------------------------------------------------- #
# (a) the reading
# --------------------------------------------------------------------------- #
def test_book_beta_reading_line_and_weights():
    series = _bench_series()
    series["HOT"] = _series(2.0)
    series["PSQ"] = _series(-1.2)
    broker = _Broker(series)
    bb = BookBeta(broker)
    acct = _acct([_eq("HOT", 30_000), _eq("PSQ", 10_000)])
    r = bb.read(acct)
    assert r.available and not r.unknown
    hot = shrink(2.0)                  # 1.8
    psq = shrink(-1.2)                 # -1.16 (toward -1: review fix)
    assert abs(r.spy - (0.3 * hot + 0.1 * psq)) < 1e-6
    assert abs(r.invested_pct - 40.0) < 1e-9
    line = r.line()
    assert line.startswith("BOOK BETA: spy=")
    assert "qqq=" in line and "iwm=" in line and "invested=40.0%" in line
    d = r.to_dict()
    assert set(d) >= {"spy", "qqq", "iwm", "invested_pct", "betas", "weights", "at"}


def test_book_beta_unknown_symbol_assumed_one_and_options_fold_into_underlying():
    series = _bench_series()
    series["AAPL"] = _series(1.0)
    broker = _Broker(series, fail={"NEW"})
    bb = BookBeta(broker)
    opt = Position(
        symbol="AAPL260918C00200000", qty=1, avg_entry_price=5.0, current_price=5.0,
        market_value=500.0, unrealized_pl=0.0, unrealized_pl_pct=0.0,
        asset_class="us_option",
    )
    acct = _acct([_eq("AAPL", 20_000), _eq("NEW", 10_000), opt])
    r = bb.read(acct)
    assert r.available
    assert r.unknown == ["NEW"]
    assert "unknown=NEW(assumed 1.0)" in r.line()
    assert "options at debit notional" in r.line()
    # AAPL weight includes the option's $500 debit at AAPL's beta.
    assert abs(r.weights["AAPL"] - 0.205) < 1e-9
    assert abs(r.spy - (0.205 * 1.0 + 0.1 * ASSUMED_BETA)) < 1e-6


def test_book_beta_unavailable_when_spy_series_fails():
    broker = _Broker(_bench_series(), fail={"SPY"})
    r = BookBeta(broker).read(_acct([_eq("AAPL", 1_000)]))
    assert not r.available
    assert r.line().startswith("BOOK BETA: unavailable (")


def test_book_beta_reuses_correlation_guard_cache():
    from investment_strategy.correlation import CorrelationGuard
    series = _bench_series()
    series["AAPL"] = _series(1.1, n=100)
    broker = _Broker(series)
    guard = CorrelationGuard(broker)
    guard._daily_returns("AAPL")          # the guard already fetched AAPL
    bb = BookBeta(broker, guard)
    bb.beta_of("AAPL", "SPY")
    assert broker.calls.count("AAPL") == 1  # no double fetch
    bb.new_cycle()
    assert bb._betas == {}


# --------------------------------------------------------------------------- #
# (a) liveness: one progress tick per fetched symbol (Sep 11 2026: a 163 s
# serial fan-out under an Alpaca degradation withheld the heartbeat twice)
# --------------------------------------------------------------------------- #
def test_book_beta_read_reports_progress_once_per_fetched_symbol():
    series = _bench_series()
    series["AAPL"] = _series(1.0)
    series["HOT"] = _series(2.0)
    series["PSQ"] = _series(-1.2)
    broker = _Broker(series, fail={"NEW"})
    bb = BookBeta(broker)
    acct = _acct([_eq("AAPL", 10_000), _eq("HOT", 10_000), _eq("PSQ", 5_000),
                  _eq("NEW", 5_000)])
    ticks: list[int] = []
    r = bb.read(acct, on_progress=lambda: ticks.append(1))
    # 3 benchmarks + 4 held = 7 fetches -> 7 ticks. NEW's FAILED fetch ticks
    # too (the loop returned and moved on: that is forward progress, only a
    # read that never returns must go silent). Never more than one tick per
    # symbol even though each holding is measured against three benchmarks.
    assert len(broker.calls) == 7 and len(ticks) == 7
    assert r.available and r.unknown == ["NEW"]
    # Same cycle again: everything is cycle-cached -> no fetch, no tick.
    ticks.clear()
    r2 = bb.read(acct, on_progress=lambda: ticks.append(1))
    assert len(broker.calls) == 7 and ticks == []
    assert r2.spy == r.spy
    # The hook has no effect on the numbers.
    plain = BookBeta(_Broker(series, fail={"NEW"})).read(acct)
    assert plain.to_dict()["betas"] == r.to_dict()["betas"] and plain.spy == r.spy
    # New cycle: refetched, re-ticked.
    bb.new_cycle()
    bb.read(acct, on_progress=lambda: ticks.append(1))
    assert len(broker.calls) == 14 and len(ticks) == 7
    # No hook: still fine.
    bb.new_cycle()
    assert bb.read(acct).available


def test_book_beta_read_reuses_guard_cache_and_still_ticks_once():
    # The per-cycle price cache is CorrelationGuard's series cache, shared
    # with the reader: a symbol the guard already fetched this cycle is not
    # refetched by read(); the hook still ticks once for it (harmless — the
    # stamp is throttled — and simpler than reaching into the guard's cache).
    from investment_strategy.correlation import CorrelationGuard
    series = _bench_series()
    series["AAPL"] = _series(1.1, n=100)
    broker = _Broker(series)
    guard = CorrelationGuard(broker)
    guard._daily_returns("AAPL")
    bb = BookBeta(broker, guard)
    ticks: list[int] = []
    r = bb.read(_acct([_eq("AAPL", 20_000)]), on_progress=lambda: ticks.append(1))
    assert r.available
    assert broker.calls.count("AAPL") == 1        # warmed by the guard, not refetched
    assert sorted(broker.calls) == ["AAPL", "IWM", "QQQ", "SPY"]
    assert len(ticks) == 4                        # SPY, QQQ, IWM, AAPL — once each


def test_book_beta_progress_hook_failure_never_breaks_reading():
    series = _bench_series()
    series["AAPL"] = _series(1.5)
    bb = BookBeta(_Broker(series))
    acct = _acct([_eq("AAPL", 50_000)])

    def boom():
        raise RuntimeError("stamp write failed")

    r = bb.read(acct, on_progress=boom)
    assert r.available and not r.reason
    assert abs(r.spy - 0.5 * shrink(1.5)) < 1e-6
    assert bb._on_progress is None                # released after the read


# --------------------------------------------------------------------------- #
# (b) beta cap gate
# --------------------------------------------------------------------------- #
def _gate_rm(**over):
    return _rm(_limits(kelly_fraction=0.0, max_position_pct=6.0,
                       min_cash_buffer_pct=0.0, **over))


def test_beta_cap_resizes_and_logs_counterfactual(caplog):
    rm = _gate_rm(max_book_beta_spy=1.2)
    caplog.set_level(logging.WARNING, logger="risk")
    # book 1.08, candidate beta 2.0, request 6% -> post 1.20 exactly fits;
    # push the book to 1.12 so the 6% request would land at 1.24.
    d = rm.evaluate(
        _buy("SMCI", weight=6.0), _account(), price=100.0, volatility=0.3,
        book_beta_spy=1.12, candidate_beta=2.0,
    )
    assert d.verdict == RiskVerdict.RESIZED, d.reason
    # room = (1.2 - 1.12) * 100k / 2.0 = $4,000
    assert abs(d.approved_notional - 4_000.0) < 1e-6
    msgs = [r.getMessage() for r in caplog.records if "BOOK BETA CAP" in r.getMessage()]
    assert msgs == ["BOOK BETA CAP: SMCI 6.0% -> 4.0% (book 1.24 -> 1.20)"]


def test_beta_cap_rejects_when_min_order_breaches(caplog):
    rm = _gate_rm(max_book_beta_spy=1.2, min_order_usd=500.0)
    caplog.set_level(logging.WARNING, logger="risk")
    d = rm.evaluate(
        _buy("SMCI", weight=6.0), _account(), price=100.0, volatility=0.3,
        book_beta_spy=1.199, candidate_beta=1.5,
    )
    assert d.verdict == RiskVerdict.REJECTED
    assert "Book beta cap" in d.reason and "min order" in d.reason
    assert any("-> rejected" in r.getMessage() for r in caplog.records)


def test_beta_cap_unknown_beta_assumes_one_and_logs(caplog):
    rm = _gate_rm(max_book_beta_spy=1.2)
    caplog.set_level(logging.INFO, logger="risk")
    d = rm.evaluate(
        _buy("NEW", weight=6.0), _account(), price=100.0, volatility=0.3,
        book_beta_spy=1.17, candidate_beta=None,
    )
    assert d.verdict == RiskVerdict.RESIZED
    assert abs(d.approved_notional - 3_000.0) < 1e-6     # (1.2-1.17)*100k/1.0
    assert any("NEW beta unknown — assuming 1.0" in r.getMessage() for r in caplog.records)


def test_beta_cap_inert_without_reading_or_when_off_or_negative_beta():
    full = 6_000.0
    for kw in (
        dict(book_beta_spy=None, candidate_beta=2.0),
        dict(book_beta_spy=1.9, candidate_beta=-1.0),
    ):
        d = _gate_rm(max_book_beta_spy=1.2).evaluate(
            _buy("X", weight=6.0), _account(), price=100.0, volatility=0.3, **kw,
        )
        assert abs(d.approved_notional - full) < 1e-6, kw
    d = _gate_rm(max_book_beta_spy=0.0).evaluate(
        _buy("X", weight=6.0), _account(), price=100.0, volatility=0.3,
        book_beta_spy=1.9, candidate_beta=2.0,
    )
    assert abs(d.approved_notional - full) < 1e-6
    # Under the cap: untouched.
    d = _gate_rm(max_book_beta_spy=1.2).evaluate(
        _buy("X", weight=6.0), _account(), price=100.0, volatility=0.3,
        book_beta_spy=0.5, candidate_beta=1.5,
    )
    assert abs(d.approved_notional - full) < 1e-6


# --------------------------------------------------------------------------- #
# (c) hedge arithmetic + hysteresis
# --------------------------------------------------------------------------- #
def test_hedge_target_notional_and_ceiling():
    assert abs(hedge_target_notional(1.3, 1.0, 1_000_000, 40.0) - 300_000) < 1e-6
    assert hedge_target_notional(0.9, 1.0, 1_000_000, 40.0) == 0.0
    assert abs(hedge_target_notional(1.6, 1.0, 1_000_000, 40.0) - 400_000) < 1e-6


def test_hedge_signal_hysteresis():
    assert hedge_signal(1.16, 1.0, 0.15, False) == "arm"
    assert hedge_signal(1.10, 1.0, 0.15, False) == "hold"
    assert hedge_signal(1.10, 1.0, 0.15, True) == "hold"
    assert hedge_signal(0.84, 1.0, 0.15, True) == "unwind"
    assert hedge_signal(0.84, 1.0, 0.15, False) == "hold"
    assert hedge_signal(None, 1.0, 0.15, True) == "hold"


class _HedgeBroker:
    def __init__(self):
        self.buys, self.closed, self.canceled = [], [], []

    def cancel_open_orders_for(self, symbol):
        self.canceled.append(symbol)

    def close_position(self, symbol):
        self.closed.append(symbol)
        return f"oid-close-{symbol}"

    def submit_notional_buy(self, symbol, notional):
        self.buys.append((symbol, round(notional, 2)))
        return f"oid-buy-{symbol}"

    def latest_price(self, symbol):
        return 100.0


class _FixedBeta:
    """Stand-in reader returning a scripted SPY-beta per read()."""
    def __init__(self, betas):
        self.betas = list(betas)

    def read(self, account, on_progress=None):
        b = self.betas.pop(0) if len(self.betas) > 1 else self.betas[0]
        return SimpleNamespace(available=b is not None, spy=b)

    def new_cycle(self):
        pass


def _beta_orch(betas, mode="beta", falling=False, max_pct=40.0, target=1.0,
               band=0.15, falling_target=0.8, halted=False):
    o = Orchestrator.__new__(Orchestrator)
    o.cfg = SimpleNamespace(
        hedge_etf="PSQ", auto_hedge_ratio=0.30, auto_hedge_min_cycles=2,
        auto_hedge_max_pct=max_pct, auto_hedge_mode=mode,
        book_beta_enabled=True, hedge_beta_target=target,
        hedge_beta_band=band, hedge_beta_falling_target=falling_target,
        risk=SimpleNamespace(
            min_order_usd=1.0, min_order_pct=0.0, min_cash_buffer_pct=0.0,
        ),
    )
    o._market_falling = lambda: (falling, "test read")
    o.risk = SimpleNamespace(trading_halted=lambda a: (halted, "halt" if halted else ""))
    o.broker = _HedgeBroker()
    records = []
    o.ledger = SimpleNamespace(records=records, record=records.append)
    p = os.path.join(tempfile.gettempdir(), f"_r6_beta_{uuid.uuid4().hex}.json")
    o.state = PortfolioState(path=p)
    o._trade_lock = threading.Lock()
    o._pending_oids = []
    o._falling_cycles = 0
    o._clear_cycles = 0
    o.book_beta = _FixedBeta(betas)
    o._book_beta_reading = None
    o._hedge_reason = ""
    return o


def _hedge_acct(net_long=600_000.0, cash=400_000.0, psq_value=0.0):
    positions = [_eq("AAPL", net_long)]
    if psq_value > 0:
        positions.append(_eq("PSQ", psq_value))
    return AccountSnapshot(
        equity=net_long + cash + psq_value, last_equity=net_long + cash,
        cash=cash, buying_power=cash, positions=positions,
    )


def test_beta_hedge_arms_after_one_cycle_sized_to_gap(caplog):
    caplog.set_level(logging.INFO)
    o = _beta_orch([1.30])
    o._apply_auto_hedge(_hedge_acct())
    # (1.30 - 1.00) x $1,000,000 = $300k, under the 40% ceiling.
    assert o.broker.buys == [("PSQ", 300_000.0)]
    assert o.ledger.records[0].entry_signals == ["auto_hedge"]
    assert o.ledger.records[0].rationale.startswith("auto-hedge: beta:")
    assert o._hedge_reason == "beta:1.30" and o._falling_cycles == 1
    msgs = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("AUTO-HEDGE: beta: book spy-beta 1.30 > target 1.00") for m in msgs)


def test_beta_hedge_ceiling_and_topup_only_the_gap():
    o = _beta_orch([1.60])
    o._apply_auto_hedge(_hedge_acct())
    assert o.broker.buys == [("PSQ", 400_000.0)]        # 60% gap capped at 40%
    o = _beta_orch([1.20], max_pct=40.0)
    o._apply_auto_hedge(_hedge_acct(psq_value=150_000.0))
    # gap = 0.20 x $1.15M equity = $230k additional; ceiling 40% = $460k,
    # held $150k -> room $310k -> buy $230k.
    assert o.broker.buys == [("PSQ", 230_000.0)]


def test_beta_hedge_holds_inside_band_and_unwinds_below(caplog):
    caplog.set_level(logging.INFO)
    o = _beta_orch([1.05])
    acct = _hedge_acct(psq_value=100_000.0)
    o._apply_auto_hedge(acct)
    assert o.broker.buys == [] and o.broker.closed == []
    assert o._hedge_reason == "beta:1.05"       # funnel shows armed(beta:..)
    o = _beta_orch([0.80])
    acct = _hedge_acct(psq_value=100_000.0)
    o._apply_auto_hedge(acct)
    assert o.broker.closed == ["PSQ"]
    assert o.ledger.records[-1].exit_reason == "hedge_unwind"
    assert o._hedge_reason == "" and o._falling_cycles == 0
    assert any(
        m.startswith("AUTO-HEDGE UNWIND: beta: book spy-beta 0.80 < target 1.00")
        for m in (r.getMessage() for r in caplog.records)
    )
    # Below the band with NO hedge on: nothing to do.
    o = _beta_orch([0.80])
    o._apply_auto_hedge(_hedge_acct())
    assert o.broker.closed == [] and o.broker.buys == []


def test_beta_hedge_falling_tape_tightens_target():
    o = _beta_orch([1.10], falling=True)          # 1.10 > 0.8 + 0.15
    o._apply_auto_hedge(_hedge_acct())
    assert o.broker.buys == [("PSQ", 300_000.0)]  # (1.10 - 0.80) x $1M
    assert "tape falling" in o.ledger.records[0].rationale


def test_beta_hedge_unavailable_reading_holds_and_never_raises(caplog):
    caplog.set_level(logging.INFO)
    o = _beta_orch([None])
    o._apply_auto_hedge(_hedge_acct(psq_value=50_000.0))
    assert o.broker.buys == [] and o.broker.closed == []
    assert any("book beta unavailable" in r.getMessage() for r in caplog.records)
    o = _beta_orch([1.5], halted=True)
    o._apply_auto_hedge(_hedge_acct())
    assert o.broker.buys == []


def test_falling_mode_is_the_legacy_path():
    o = _beta_orch([1.60], mode="falling", falling=True)
    o._apply_auto_hedge(_hedge_acct())
    assert o.broker.buys == []                  # cycle 1/2 — legacy persistence
    o._apply_auto_hedge(_hedge_acct())
    # 30% of $600k = $180k, under the (new default) 40% ceiling.
    assert o.broker.buys == [("PSQ", 180_000.0)]


# --------------------------------------------------------------------------- #
# cycle wiring: reading, persistence, graceful degradation, buy context
# --------------------------------------------------------------------------- #
def _read_orch(reader, enabled=True):
    o = Orchestrator.__new__(Orchestrator)
    o.cfg = SimpleNamespace(book_beta_enabled=enabled)
    o.book_beta = reader
    o._book_beta_reading = None
    p = os.path.join(tempfile.gettempdir(), f"_r6_bb_{uuid.uuid4().hex}.json")
    o.state = PortfolioState(path=p)
    return o


def test_read_book_beta_logs_one_line_and_persists(caplog):
    caplog.set_level(logging.INFO)
    series = _bench_series()
    series["AAPL"] = _series(1.5)
    o = _read_orch(BookBeta(_Broker(series)))
    acct = _acct([_eq("AAPL", 50_000)])
    o._read_book_beta(acct)
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("BOOK BETA:")]
    assert len(lines) == 1 and "invested=50.0%" in lines[0]
    assert o._book_beta_reading is not None
    persisted = PortfolioState(path=o.state.path).get_book_beta()
    assert abs(persisted["spy"] - 0.5 * shrink(1.5)) < 1e-6
    assert persisted["at"]
    # Buy context: book beta + the candidate's own beta.
    series["SMCI"] = _series(2.0)
    book, cand = o._beta_context("SMCI", acct)
    assert abs(book - 0.5 * shrink(1.5)) < 1e-5 and abs(cand - shrink(2.0)) < 1e-5


def test_read_book_beta_stamps_liveness_once_per_fetch(caplog):
    # The orchestrator hands the reader its heartbeat liveness stamp, the way
    # it does for SignalAggregator.gather: one stamp per symbol fetched, so a
    # slow serial fan-out (Sep 11: 163 s, heartbeat withheld twice) can't
    # exceed the 150 s freshness bar while the loop is merely busy.
    caplog.set_level(logging.INFO)
    series = _bench_series()
    series["AAPL"] = _series(1.5)
    series["HOT"] = _series(2.0)
    broker = _Broker(series)
    o = _read_orch(BookBeta(broker))
    stamps: list[int] = []
    o._stamp_liveness = lambda: stamps.append(1)
    acct = _acct([_eq("AAPL", 30_000), _eq("HOT", 20_000)])
    o._read_book_beta(acct)
    assert o._book_beta_reading is not None
    assert len(stamps) == len(broker.calls) == 5          # SPY QQQ IWM AAPL HOT
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("BOOK BETA:")]
    assert len(lines) == 1 and "invested=50.0%" in lines[0]
    assert abs(o._book_beta_reading.spy - (0.3 * shrink(1.5) + 0.2 * shrink(2.0))) < 1e-6


def test_read_book_beta_degrades_gracefully(caplog):
    caplog.set_level(logging.INFO)

    class _Boom:
        def read(self, account, on_progress=None):
            raise RuntimeError("data plan exhausted")

    o = _read_orch(_Boom())
    o._read_book_beta(_acct([_eq("AAPL", 1_000)]))
    assert o._book_beta_reading is None
    assert any(
        r.getMessage().startswith("BOOK BETA: unavailable (RuntimeError")
        for r in caplog.records
    )
    assert o._beta_context("AAPL", _acct([])) == (None, None)
    # Knob off: no line, no reading.
    caplog.clear()
    o = _read_orch(BookBeta(_Broker(_bench_series())), enabled=False)
    o._read_book_beta(_acct([]))
    assert not [r for r in caplog.records if "BOOK BETA" in r.getMessage()]
    assert o._book_beta_reading is None


# --------------------------------------------------------------------------- #
# config defaults
# --------------------------------------------------------------------------- #
def test_run6_defaults_in_config():
    env = _env_without(
        "MAX_BOOK_BETA_SPY", "AUTO_HEDGE_MODE", "AUTO_HEDGE_MAX_PCT",
        "HEDGE_BETA_TARGET", "HEDGE_BETA_BAND", "HEDGE_BETA_FALLING_TARGET",
        "BOOK_BETA_ENABLED",
    )
    with patch.dict(os.environ, env, clear=True):
        from investment_strategy.config import load_config
        cfg = load_config()
    assert cfg.risk.max_book_beta_spy == 1.2
    assert cfg.auto_hedge_mode == "beta"
    assert cfg.auto_hedge_max_pct == 40.0
    assert cfg.hedge_beta_target == 1.0
    assert cfg.hedge_beta_band == 0.15
    assert cfg.hedge_beta_falling_target == 0.8
    assert cfg.book_beta_enabled is True
    with patch.dict(os.environ, {**env, "AUTO_HEDGE_MODE": "Falling",
                                 "MAX_BOOK_BETA_SPY": "0"}, clear=True):
        from investment_strategy.config import load_config
        cfg = load_config()
    assert cfg.auto_hedge_mode == "falling" and cfg.risk.max_book_beta_spy == 0.0


# --------------------------------------------------------------------------- #
# Run-7 S-2 (Sep 12 2026): the hedge notional is divided by the hedge ETF's
# MEASURED SPY-beta. Run-6 sized every arm on an implicit -1.0 while PSQ read
# -1.51 (it is -1.0 x QQQ; QQQ's SPY-beta was 1.51 shrunk on 60 d): Sep 3
# 10:14 / Sep 10 11:06 a 1.20 read bought $203,605 / $204,391 and the next
# reading landed at 0.89 / 0.90 — 0.05 above the 0.85 unwind line instead of
# ~1.00; correct size was ~$135k (~$68k idle per arm).
# --------------------------------------------------------------------------- #
def test_hedge_target_notional_divides_by_hedge_beta():
    # 0.30 gap x $1M / 1.5 = $200k (was $300k undivided).
    assert abs(hedge_target_notional(1.30, 1.0, 1_000_000, 40.0, hedge_beta=-1.5) - 200_000) < 1e-6
    # The Sep 3 arm at the measured -1.51: 0.20 x $1.02M / 1.51 = $135,099
    # — not the $204k the undivided formula sent.
    got = hedge_target_notional(1.20, 1.0, 1_020_000, 40.0, -1.51)
    assert abs(got - 135_099.34) < 1.0
    assert abs(hedge_target_notional(1.20, 1.0, 1_020_000, 40.0) - 204_000) < 1e-6
    # Sign-agnostic (the divisor is |hedge_beta|) and no gap -> no order.
    assert abs(hedge_target_notional(1.30, 1.0, 1_000_000, 40.0, 1.5) - 200_000) < 1e-6
    assert hedge_target_notional(0.9, 1.0, 1_000_000, 40.0, -1.5) == 0.0
    # A zero/NaN divisor can never raise on the decision path: legacy 1.0.
    assert abs(hedge_target_notional(1.30, 1.0, 1_000_000, 40.0, 0.0) - 300_000) < 1e-6
    assert abs(hedge_target_notional(1.30, 1.0, 1_000_000, 40.0, float("nan")) - 300_000) < 1e-6


def test_hedge_target_notional_default_is_minus_one():
    # The positional 4-arg call (every pre-S-2 caller and test) is unchanged.
    assert abs(hedge_target_notional(1.3, 1.0, 1_000_000, 40.0) - 300_000) < 1e-6
    assert hedge_target_notional(1.3, 1.0, 1_000_000, 40.0) == \
        hedge_target_notional(1.3, 1.0, 1_000_000, 40.0, hedge_beta=-1.0)


def test_hedge_target_notional_ceiling_unchanged_by_hedge_beta():
    # The ceiling is on NOTIONAL and is never divided: 60% gap capped at 40%.
    assert abs(hedge_target_notional(1.6, 1.0, 1_000_000, 40.0, -1.0) - 400_000) < 1e-6
    assert abs(hedge_target_notional(1.6, 1.0, 1_000_000, 40.0, -1.5) - 400_000) < 1e-6   # exactly at
    assert abs(hedge_target_notional(1.6, 1.0, 1_000_000, 40.0, -0.5) - 400_000) < 1e-6   # $1.2M capped


class _MeasuredBeta(_FixedBeta):
    """_FixedBeta plus a scripted beta_of(): `hedge` is what the reader
    returns for the hedge ETF vs SPY (a value, None, or an exception)."""
    def __init__(self, betas, hedge):
        super().__init__(betas)
        self.hedge = hedge
        self.asked: list[tuple[str, str]] = []

    def beta_of(self, symbol, bench="SPY"):
        self.asked.append((symbol, bench))
        if isinstance(self.hedge, Exception):
            raise self.hedge
        return self.hedge


def _auto_hedge_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("AUTO-HEDGE:")]


def test_beta_hedge_uses_measured_beta_and_logs_source(caplog):
    caplog.set_level(logging.INFO)
    o = _beta_orch([1.30])
    o.book_beta = _MeasuredBeta([1.30], hedge=-1.5)
    o._apply_auto_hedge(_hedge_acct())
    # (1.30 - 1.00) x $1M / 1.5 = $200k — not the $300k the undivided gap sent.
    assert o.broker.buys == [("PSQ", 200_000.0)]
    assert ("PSQ", "SPY") in o.book_beta.asked
    lines = _auto_hedge_lines(caplog)
    assert len(lines) == 1
    assert lines[0].startswith("AUTO-HEDGE: beta: book spy-beta 1.30 > target 1.00 + 0.15 band")
    assert "hedge beta -1.50 measured" in lines[0]
    assert "at -1.0 would be $300000" in lines[0]          # the counterfactual
    assert "bought $200000 of PSQ" in lines[0]
    # The divisor is auditable from the ledger row and from risk_state.
    assert "hedge beta -1.50 (measured)" in o.ledger.records[0].risk_note
    assert o.state.get_hedge_beta() == (-1.5, "measured")
    assert PortfolioState(path=o.state.path).get_hedge_beta() == (-1.5, "measured")


def test_beta_hedge_measured_beta_lands_reading_on_target():
    # End to end with the REAL reader: HOT (beta 1.4 shrunk) 80% of the
    # book reads 1.12; PSQ measures ~ -1.08 shrunk. Dividing by the SAME
    # shrunk number the reading prices PSQ at lands the post-fill reading
    # ON target (run-6 landed target - 0.10 with a -1.0 assumption).
    series = _bench_series()
    series["HOT"] = _series(1.5)
    series["PSQ"] = _series(-1.1)
    bb = BookBeta(_Broker(series))
    equity = 100_000.0
    r = bb.read(_acct([_eq("HOT", 80_000)], equity))
    psq = bb.beta_of("PSQ", "SPY")
    gap = hedge_target_notional(r.spy, 1.0, equity, 40.0, hedge_beta=psq)
    assert 0 < gap < hedge_target_notional(r.spy, 1.0, equity, 40.0)
    r2 = BookBeta(_Broker(series)).read(_acct([_eq("HOT", 80_000), _eq("PSQ", gap)], equity))
    assert abs(r2.spy - 1.0) < 1e-6


def test_beta_hedge_falls_back_on_bad_measured_beta(caplog):
    caplog.set_level(logging.INFO)
    # +0.4 (a 'hedge' that adds exposure), None (short history), -0.3 (a
    # spurious read that would double the order), -3.5 (outside the band),
    # a raising reader, and the legacy stub with NO beta_of at all.
    for bad in (0.4, None, -0.3, -3.5, RuntimeError("feed down")):
        caplog.clear()
        o = _beta_orch([1.30])
        o.book_beta = _MeasuredBeta([1.30], hedge=bad)
        o._apply_auto_hedge(_hedge_acct())
        assert o.broker.buys == [("PSQ", 300_000.0)], bad     # assumed -1.0
        lines = _auto_hedge_lines(caplog)
        assert len(lines) == 1 and "hedge beta -1.00 assumed" in lines[0], bad
        assert "at -1.0 would be $300000" in lines[0]
        assert o.state.get_hedge_beta() == (-1.0, "assumed"), bad
        msgs = [r.getMessage() for r in caplog.records]
        assert any(m.startswith("Auto-hedge: PSQ") and "using assumed -1.00" in m for m in msgs), bad
    caplog.clear()
    o = _beta_orch([1.30])                                   # _FixedBeta: AttributeError
    o._apply_auto_hedge(_hedge_acct())
    assert o.broker.buys == [("PSQ", 300_000.0)]
    assert "hedge beta -1.00 assumed" in _auto_hedge_lines(caplog)[0]
    # The assumed value is the CONFIG knob, not a hard-coded -1.
    caplog.clear()
    o = _beta_orch([1.30])
    o.cfg.hedge_beta_assumed = -1.2
    o._apply_auto_hedge(_hedge_acct())
    assert o.broker.buys == [("PSQ", 250_000.0)]             # $300k / 1.2
    assert "hedge beta -1.20 assumed" in _auto_hedge_lines(caplog)[0]
    # ...but an insane knob (positive) is itself guarded back to -1.0.
    o = _beta_orch([1.30])
    o.cfg.hedge_beta_assumed = 0.7
    o._apply_auto_hedge(_hedge_acct())
    assert o.broker.buys == [("PSQ", 300_000.0)]


def test_beta_hedge_counterfactual_and_topup_respect_ceiling_and_cash():
    # Held $150k PSQ, book 1.20 on $1.15M, measured -1.5: gap = 0.20 x
    # 1.15M / 1.5 = $153,333; ceiling 40% = $460k, room $310k -> buy the
    # gap. The counterfactual (-1.0) is $230k, also inside the room.
    o = _beta_orch([1.20])
    o.book_beta = _MeasuredBeta([1.20], hedge=-1.5)
    o._apply_auto_hedge(_hedge_acct(psq_value=150_000.0))
    assert o.broker.buys == [("PSQ", 153_333.33)]
    # Cash-clamped arm (Sep 3 12:51: wanted $204k, $62k spendable): the
    # order is the spendable cash and the divisor is still stamped.
    o = _beta_orch([1.30])
    o.book_beta = _MeasuredBeta([1.30], hedge=-1.5)
    o._apply_auto_hedge(_hedge_acct(net_long=900_000.0, cash=100_000.0))
    assert o.broker.buys == [("PSQ", 100_000.0)]
    assert o.state.get_hedge_beta() == (-1.5, "measured")
    # Hold / unwind cycles never consult the hedge beta (no extra fetch).
    o = _beta_orch([1.05])
    o.book_beta = _MeasuredBeta([1.05], hedge=-1.5)
    o._apply_auto_hedge(_hedge_acct(psq_value=100_000.0))
    assert o.broker.buys == [] and o.book_beta.asked == []
    assert o.state.get_hedge_beta() == (None, "")


def test_hedge_beta_state_round_trip_and_legacy_file():
    p = os.path.join(tempfile.gettempdir(), f"_r7_hb_{uuid.uuid4().hex}.json")
    st = PortfolioState(path=p)
    assert st.get_hedge_beta() == (None, "")
    st.set_hedge_beta(-1.51, "measured")
    assert PortfolioState(path=p).get_hedge_beta() == (-1.51, "measured")
    # A pre-S-2 risk_state.json (no keys) loads clean.
    import json
    d = json.loads(open(p, encoding="utf-8").read())
    d.pop("hedge_beta"); d.pop("hedge_beta_source")
    open(p, "w", encoding="utf-8").write(json.dumps(d))
    assert PortfolioState(path=p).get_hedge_beta() == (None, "")


def test_hedge_beta_assumed_config_default_env_and_validation(caplog):
    env = _env_without("HEDGE_BETA_ASSUMED")
    from investment_strategy.config import load_config
    with patch.dict(os.environ, env, clear=True):
        assert load_config().hedge_beta_assumed == -1.0
    with patch.dict(os.environ, {**env, "HEDGE_BETA_ASSUMED": "-1.5"}, clear=True):
        assert load_config().hedge_beta_assumed == -1.5
    # Positive / zero / out-of-band / garbage -> WARN + -1.0 (never a
    # divisor that adds exposure or blows the gap up).
    for bad in ("0.7", "0", "-0.2", "-4", "psq"):
        caplog.clear()
        caplog.set_level(logging.WARNING, logger="config")
        with patch.dict(os.environ, {**env, "HEDGE_BETA_ASSUMED": bad}, clear=True):
            assert load_config().hedge_beta_assumed == -1.0, bad
        assert any("HEDGE_BETA_ASSUMED=" in r.getMessage() and "[-3.0, -0.5]" in r.getMessage()
                   for r in caplog.records), bad


def test_config_logs_beta_cap_vs_hedge_arm_line(caplog):
    # Decision 6 (run-7): MAX_BOOK_BETA_SPY stays 1.2 — ONE INFO line at
    # load makes the cap/arm geometry greppable per run.
    env = _env_without("MAX_BOOK_BETA_SPY", "HEDGE_BETA_TARGET", "HEDGE_BETA_BAND")
    from investment_strategy.config import load_config
    caplog.set_level(logging.INFO, logger="config")
    with patch.dict(os.environ, env, clear=True):
        load_config()
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("BOOK BETA CAP vs")]
    assert lines == [
        "BOOK BETA CAP vs hedge arm line: cap 1.20 - (target 1.00 + band 0.15) "
        "= +0.05 (cap-bound buys land inside the arm zone)"
    ]
    caplog.clear()
    with patch.dict(os.environ, {**env, "MAX_BOOK_BETA_SPY": "1.10"}, clear=True):
        load_config()
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("BOOK BETA CAP vs")]
    assert lines == [
        "BOOK BETA CAP vs hedge arm line: cap 1.10 - (target 1.00 + band 0.15) "
        "= -0.05 (cap-bound buys stay at/below the arm line)"
    ]
    caplog.clear()
    with patch.dict(os.environ, {**env, "MAX_BOOK_BETA_SPY": "0"}, clear=True):
        load_config()
    assert not [r for r in caplog.records if "BOOK BETA CAP vs" in r.getMessage()]
