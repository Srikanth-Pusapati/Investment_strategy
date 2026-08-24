"""Aug-22 breadth trigger + feed-liveness tests.

Aug 18 lost -$26,844 (3.9x SPY down-capture) with ELEVEN NAME FALLING reads
firing in one cycle while the core defense, the PSQ auto-hedge and the puts
all executed ZERO trades — _market_falling keyed only on the INDEX and SPY
never fell past -1.1% intraday. The falling-tape defenses now arm on the
index read OR book breadth: enough falling names, or intraday book P/L
through the drawdown bar (equity vs last_equity — the daily-loss halt's own
numbers). Whipsaw bounds (auto-hedge persistence + ceiling) are unchanged.

Separately (rank 8): RH OAuth died Aug 19 mid-window and 2 of 5 screener
sources silently vanished — the FEEDS line now asserts per-source health
every decision cycle.

Runnable two ways:
    .venv/bin/python -m pytest tests/test_breadth_trigger.py
    .venv/bin/pytest tests/
"""
from __future__ import annotations

import logging
import os
import sys
import tempfile
import threading
import uuid
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.config import load_config
from investment_strategy.models import AccountSnapshot, Position
from investment_strategy.orchestrator import Orchestrator
from investment_strategy.portfolio import RobinhoodReader
from investment_strategy.state import PortfolioState


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _acct(equity=100_000.0, last_equity=100_000.0, cash=50_000.0,
          positions=None) -> AccountSnapshot:
    return AccountSnapshot(
        equity=equity, last_equity=last_equity, cash=cash,
        buying_power=cash, positions=positions or [],
    )


def _mf_orch(regime_on=False, names_min=3, dd_pct=-1.25,
             falling_names=None, regime=None):
    """Bare orchestrator exercising the REAL _market_falling."""
    o = Orchestrator.__new__(Orchestrator)
    o.cfg = SimpleNamespace(
        risk=SimpleNamespace(regime_filter_enabled=regime_on),
        market_drop_defense_pct=1.5,
        breadth_falling_names_min=names_min,
        breadth_book_drawdown_pct=dd_pct,
    )
    if regime is not None:
        o.regime = SimpleNamespace(assess=lambda: regime)
    o._falling_names = dict(falling_names or {})
    return o


_THREE = {"AAA": "-4.2% today", "BBB": "-5.0% today", "CCC": "-6.3% today"}


# --------------------------------------------------------------------------- #
# Breadth leg (a): falling-name count
# --------------------------------------------------------------------------- #
def test_breadth_names_arm_the_falling_read():
    o = _mf_orch(falling_names=_THREE, dd_pct=0.0)
    falling, why = o._market_falling()
    assert falling
    assert "3 held names falling" in why
    assert o._falling_trigger == "breadth:3-names"


def test_breadth_names_below_bar_stay_clear():
    o = _mf_orch(falling_names={"AAA": "-4.2%", "BBB": "-5.0%"}, dd_pct=0.0)
    falling, why = o._market_falling()
    assert not falling and why == ""
    assert o._falling_trigger == ""


def test_breadth_names_off_switch():
    o = _mf_orch(names_min=0, falling_names=_THREE, dd_pct=0.0)
    assert o._market_falling() == (False, "")


# --------------------------------------------------------------------------- #
# Breadth leg (b): intraday book drawdown
# --------------------------------------------------------------------------- #
def test_book_drawdown_arms_with_account():
    o = _mf_orch()
    falling, why = o._market_falling(_acct(equity=98_600.0))
    assert falling and "book P/L -1.40%" in why
    assert o._falling_trigger == "book:-1.4%"


def test_book_drawdown_reads_the_stashed_snapshot():
    # The zero-arg call path (core defense / auto-hedge stash the snapshot).
    o = _mf_orch()
    o._defense_account = _acct(equity=98_000.0)
    falling, _ = o._market_falling()
    assert falling and o._falling_trigger == "book:-2.0%"


def test_book_drawdown_above_bar_and_without_snapshot_stays_clear():
    o = _mf_orch()
    assert o._market_falling(_acct(equity=99_000.0)) == (False, "")  # -1.0%
    assert o._market_falling() == (False, "")  # no snapshot at all


def test_book_drawdown_knob_is_sign_agnostic_and_has_off_switch():
    o = _mf_orch(dd_pct=1.25)  # positive spelling means the same loss bar
    falling, _ = o._market_falling(_acct(equity=98_600.0))
    assert falling
    o2 = _mf_orch(dd_pct=0.0)
    assert o2._market_falling(_acct(equity=90_000.0)) == (False, "")


# --------------------------------------------------------------------------- #
# Index leg precedence + trigger transition log
# --------------------------------------------------------------------------- #
def test_index_read_takes_precedence_over_breadth():
    o = _mf_orch(
        regime_on=True, falling_names=_THREE,
        regime=SimpleNamespace(label="risk-off", trend="up",
                               day_change_pct=0.0),
    )
    falling, why = o._market_falling(_acct(equity=98_000.0))
    assert falling and why == "regime is risk-off"
    assert o._falling_trigger == "index"


def test_trigger_transition_logged_once(caplog):
    o = _mf_orch(falling_names=_THREE, dd_pct=0.0)
    with caplog.at_level(logging.INFO, logger="orchestrator"):
        o._market_falling()
        o._market_falling()  # same source — the repeat must not re-log
    hits = [r for r in caplog.records
            if "FALLING-TAPE TRIGGER (breadth:3-names)" in r.getMessage()]
    assert len(hits) == 1


# --------------------------------------------------------------------------- #
# Whipsaw bounds preserved: breadth-armed auto-hedge still needs persistence
# --------------------------------------------------------------------------- #
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


def _breadth_hedge_orch(falling_names=None):
    o = Orchestrator.__new__(Orchestrator)
    o.cfg = SimpleNamespace(
        hedge_etf="PSQ", auto_hedge_ratio=0.30,
        auto_hedge_min_cycles=2, auto_hedge_max_pct=15.0,
        market_drop_defense_pct=1.5,
        breadth_falling_names_min=3, breadth_book_drawdown_pct=-1.25,
        risk=SimpleNamespace(
            regime_filter_enabled=False,
            min_order_usd=1.0, min_order_pct=0.0, min_cash_buffer_pct=0.0,
        ),
    )
    o.risk = SimpleNamespace(trading_halted=lambda a: (False, ""))
    o.broker = _HedgeBroker()
    records = []
    o.ledger = SimpleNamespace(records=records, record=records.append)
    p = os.path.join(tempfile.gettempdir(), f"_bt_hg_{uuid.uuid4().hex}.json")
    o.state = PortfolioState(path=p)
    o._trade_lock = threading.Lock()
    o._pending_oids = []
    o._falling_cycles = 0
    o._clear_cycles = 0
    o._falling_names = dict(falling_names or {})
    return o


def _long_acct(net_long=60_000.0, cash=40_000.0) -> AccountSnapshot:
    return _acct(
        equity=net_long + cash, last_equity=net_long + cash, cash=cash,
        positions=[Position(
            symbol="AAPL", qty=net_long / 100.0, avg_entry_price=100.0,
            current_price=100.0, market_value=net_long,
            unrealized_pl=0.0, unrealized_pl_pct=0.0,
        )],
    )


def test_breadth_armed_auto_hedge_keeps_two_cycle_persistence():
    o = _breadth_hedge_orch(falling_names=_THREE)  # REAL _market_falling
    # Two DISTINCT breadth observations: each cycle computes a FRESH map
    # (the Aug-23 double-count guard keys on the map's cycle stamp).
    o._cycle_seq, o._breadth_map_cycle = 1, 1
    o._apply_auto_hedge(_long_acct())
    assert o.broker.buys == []          # breadth cycle 1/2 — not armed yet
    o._cycle_seq, o._breadth_map_cycle = 2, 2      # fresh map, next cycle
    o._apply_auto_hedge(_long_acct())
    assert len(o.broker.buys) == 1      # breadth cycle 2/2 — armed
    sym, notional = o.broker.buys[0]
    assert sym == "PSQ"
    # 30% of $60k net-long = $18k, clamped by the 15%-of-equity ceiling $15k.
    assert abs(notional - 15_000.0) < 1.0
    assert o._falling_trigger == "breadth:3-names"


def test_breadth_same_map_never_counts_twice(caplog):
    # Aug-23 double-count fix: cycle N's re-arm counts the fresh map (1/2);
    # cycle N+1's TOP-of-cycle pass re-reads the SAME (now stale) map before
    # a fresh one exists and must NOT count it again — one one-hour blip
    # would otherwise satisfy the whole 2-cycle persistence bar.
    o = _breadth_hedge_orch(falling_names=_THREE)
    o._cycle_seq, o._breadth_map_cycle = 1, 1
    o._apply_auto_hedge(_long_acct())              # re-arm pass: counts 1/2
    assert o._falling_cycles == 1
    o._cycle_seq = 2                               # next cycle, map is stale
    with caplog.at_level(logging.INFO, logger="orchestrator"):
        o._apply_auto_hedge(_long_acct())          # stale re-read: guarded
    assert o.broker.buys == []                     # NOT armed off one snapshot
    assert o._falling_cycles == 1                  # no second count
    assert any("BREADTH COUNT GUARD" in r.getMessage() for r in caplog.records)


def test_breadth_recovered_fresh_map_stays_clear_after_one_blip():
    # The whipsaw the guard exists for: 3 names fall for ONE hour, recover
    # overnight — nothing may arm, and the clear side starts counting.
    o = _breadth_hedge_orch(falling_names=_THREE)
    o._cycle_seq, o._breadth_map_cycle = 1, 1
    o._apply_auto_hedge(_long_acct())              # blip counted 1/2
    o._cycle_seq = 2
    o._apply_auto_hedge(_long_acct())              # stale map guarded -> clear
    o._falling_names = {}                          # fresh map: recovered
    o._breadth_map_cycle = 2
    o._apply_auto_hedge(_long_acct())              # fresh clear read
    assert o.broker.buys == []
    assert o._clear_cycles >= 1


def test_breadth_index_source_does_not_consume_the_map():
    # The guard is breadth-specific: an INDEX-armed count must not mark the
    # falling-names map as consumed (different observations entirely).
    o = _breadth_hedge_orch(falling_names=_THREE)
    o.cfg.risk.regime_filter_enabled = True
    o.regime = SimpleNamespace(assess=lambda: SimpleNamespace(
        label="risk-off", trend="up", day_change_pct=0.0))
    o._cycle_seq, o._breadth_map_cycle = 1, 1
    o._apply_auto_hedge(_long_acct())              # index counts 1/2
    assert o._falling_trigger == "index"
    assert getattr(o, "_breadth_counted_cycle", -2) != 1   # map NOT consumed


# --------------------------------------------------------------------------- #
# Breadth re-arm: the fresh map re-runs the defenses the SAME cycle
# --------------------------------------------------------------------------- #
def _rearm_orch(falling_names, read_last):
    o = Orchestrator.__new__(Orchestrator)
    o.cfg = SimpleNamespace(breadth_falling_names_min=3)
    o._falling_names = dict(falling_names)
    o._falling_read_last = read_last
    o._calls = []
    o._apply_core_defense = lambda a: o._calls.append("core")
    o._apply_auto_hedge = lambda a: o._calls.append("hedge")
    return o


def test_breadth_rearm_reruns_defenses_when_first_pass_read_clear():
    o = _rearm_orch(_THREE, read_last=False)
    o._breadth_rearm(_acct())
    assert o._calls == ["core", "hedge"]


def test_breadth_rearm_skips_when_first_pass_already_counted_falling():
    o = _rearm_orch(_THREE, read_last=True)
    o._breadth_rearm(_acct())
    assert o._calls == []


def test_breadth_rearm_skips_below_bar_and_when_disabled():
    o = _rearm_orch({"AAA": "-4.2%"}, read_last=False)
    o._breadth_rearm(_acct())
    assert o._calls == []
    o2 = _rearm_orch(_THREE, read_last=False)
    o2.cfg.breadth_falling_names_min = 0
    o2._breadth_rearm(_acct())
    assert o2._calls == []


# --------------------------------------------------------------------------- #
# In-situ cycle wiring: the re-arm runs right after the fresh map lands
# --------------------------------------------------------------------------- #
class _StopCycle(RuntimeError):
    """Sentinel: aborts run_decision_cycle right at engine.decide, after the
    breadth wiring under test has already executed."""


def test_cycle_wiring_rearm_runs_defenses_after_fresh_map():
    """Pin the CALL SITE, not just the helpers (the change-set-A lesson: a
    perfectly tested gate left unwired). run_decision_cycle must (a) advance
    _cycle_seq, (b) stamp the fresh falling-names map with it, and (c) re-run
    core defense + auto-hedge via _breadth_rearm in the SAME cycle when the
    top-of-cycle pass read clear — the Aug-18 shape."""
    ns = SimpleNamespace
    o = Orchestrator.__new__(Orchestrator)
    calls: list[str] = []
    o.cfg = ns(
        risk=ns(regime_filter_enabled=False, composite_enabled=False,
                expectancy_gate_enabled=False, options_enabled=False),
        screener=ns(enabled=False),
        core_etf="", hedge_etf="", defensive_core_etf="",
        breadth_falling_names_min=3,
    )
    o.broker = ns(is_market_open=lambda: True, get_account=lambda: _acct())
    for feed in ("quiver", "earnings", "sectors", "corr_guard", "regime"):
        setattr(o, feed, ns(new_cycle=lambda: None))
    o.watchlist = []
    o.signals = ns(gather=lambda syms, on_progress=None: [])
    o.signal_history = ns(record=lambda b: None, notes_for=lambda b: {})
    o.benchmark = ns(compute=lambda: None, context_line=lambda s: "")
    o.robinhood = ns(holdings=lambda: [])
    o.options = None
    o.journal = ns(render_today=lambda *a, **k: "")

    def _decide(*a, **k):
        raise _StopCycle()

    o.engine = ns(decide=_decide)
    # Instance-level stubs shadow the bound methods not under test.
    o._reconcile_fills = lambda: None
    o._backfill_exchange_exits_locked = lambda: None
    o._stamp_liveness = lambda: None
    o._within_close_fence = lambda: False
    o._record_equity_snapshot = lambda: None
    o._inject_discovery = lambda b, d: None
    o._apply_thesis_decay_exits = lambda b, a: set()
    o._apply_core_defense = lambda a: calls.append("core")
    o._apply_auto_hedge = lambda a: calls.append("hedge")
    o._apply_defensive_rotation = lambda a: None
    o._check_robinhood_health = lambda: []
    o._attribution_lessons = lambda: ""
    o._curated_lessons = lambda: ""
    o._partition_slate = lambda b, a: (b, {})
    o._journal_decision = lambda *a, **k: None
    o._name_falling_reads = lambda a, t: dict(_THREE)  # 3 fresh falling names
    o._cycle_seq = 4
    try:
        o.run_decision_cycle()
        raise AssertionError("engine.decide never reached — cycle rewired?")
    except _StopCycle:
        pass
    # Top-of-cycle defense pass + the breadth re-arm = both defenses TWICE.
    assert calls == ["core", "hedge", "core", "hedge"]
    assert o._cycle_seq == 5                       # cycle advanced the seq
    assert o._breadth_map_cycle == 5               # fresh map stamped with it


# --------------------------------------------------------------------------- #
# FEEDS liveness line (rank 8)
# --------------------------------------------------------------------------- #
_FIVE = ("congress", "insider", "options_flow", "robinhood", "robinhood_scans")


def _feed_orch(sources=_FIVE, down=(), robinhood_enabled=False):
    o = Orchestrator.__new__(Orchestrator)
    o.cfg = SimpleNamespace(
        robinhood_enabled=robinhood_enabled,
        screener=SimpleNamespace(sources=tuple(sources)),
    )
    o.screeners = SimpleNamespace(screeners=[
        SimpleNamespace(name=n, enabled=(n not in down)) for n in sources
    ])
    return o


def test_feeds_line_all_healthy(monkeypatch):
    monkeypatch.setattr(RobinhoodReader, "_auth_dead", False)
    assert _feed_orch()._feed_health_line() == "FEEDS: 5/5 healthy"


def test_feeds_line_flags_dead_rh_sources_as_oauth(monkeypatch):
    monkeypatch.setattr(RobinhoodReader, "_auth_dead", True)
    line = _feed_orch(down=("robinhood", "robinhood_scans"))._feed_health_line()
    assert line.startswith("FEEDS: 3/5 — ")
    assert "robinhood DEAD (oauth)" in line
    assert "robinhood_scans DEAD (oauth)" in line
    assert line.endswith("EVAL WINDOW VALIDITY AT RISK")


def test_feeds_line_non_rh_outage_and_unknown_source(monkeypatch):
    monkeypatch.setattr(RobinhoodReader, "_auth_dead", False)
    o = _feed_orch(sources=("congress", "insider"), down=("congress",))
    line = o._feed_health_line()
    assert "congress DEAD (disabled/no credentials)" in line
    # A configured source the registry never instantiated is dead too.
    o2 = _feed_orch(sources=("congress", "bogus"))
    o2.screeners = SimpleNamespace(
        screeners=[SimpleNamespace(name="congress", enabled=True)]
    )
    assert "bogus DEAD (unknown source)" in o2._feed_health_line()


def test_check_robinhood_health_logs_feeds_every_cycle(caplog, monkeypatch):
    monkeypatch.setattr(RobinhoodReader, "_auth_dead", False)
    o = _feed_orch()  # robinhood_enabled=False: the old path returned early
    with caplog.at_level(logging.INFO, logger="orchestrator"):
        notes = o._check_robinhood_health()
    assert notes == []
    assert any("FEEDS: 5/5 healthy" in r.getMessage() for r in caplog.records)


def test_degraded_feeds_log_at_warning(caplog, monkeypatch):
    monkeypatch.setattr(RobinhoodReader, "_auth_dead", True)
    o = _feed_orch(down=("robinhood",))
    with caplog.at_level(logging.INFO, logger="orchestrator"):
        o._check_robinhood_health()
    hit = next(r for r in caplog.records if "FEEDS:" in r.getMessage())
    assert hit.levelno == logging.WARNING


# --------------------------------------------------------------------------- #
# Config knob defaults + env overrides
# --------------------------------------------------------------------------- #
def test_breadth_knob_defaults(monkeypatch):
    monkeypatch.delenv("BREADTH_FALLING_NAMES_MIN", raising=False)
    monkeypatch.delenv("BREADTH_BOOK_DRAWDOWN_PCT", raising=False)
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    cfg = load_config()
    assert cfg.breadth_falling_names_min == 3
    assert cfg.breadth_book_drawdown_pct == -1.25


def test_breadth_knob_env_overrides(monkeypatch):
    monkeypatch.setenv("BREADTH_FALLING_NAMES_MIN", "5")
    monkeypatch.setenv("BREADTH_BOOK_DRAWDOWN_PCT", "-2.0")
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    cfg = load_config()
    assert cfg.breadth_falling_names_min == 5
    assert cfg.breadth_book_drawdown_pct == -2.0
