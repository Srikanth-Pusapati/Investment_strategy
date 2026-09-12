"""Run-7 4a-15 / 4a-16: the decision-time shadow stamps are POPULATED on the
live paths (measurement-only; nothing here changes an order).

  - BUY: orchestrator._handle_equity stamps the tape from reads the cycle
    already holds (RegimeReader.current(), _falling_names, the vol read) and
    logs ONE 'ENTRY TAPE:' line.
  - STOP exits: the bracket backfill (bracket_stop) and the watchdog's
    fractional hard stop ('stop') stamp floor6_would_survive from daily
    closes; every other exit leaves it None; an unreadable series is None,
    never a false 'survived'.

Runnable two ways:
    .venv/bin/python -m pytest tests/test_entry_tape.py
    .venv/bin/pytest tests/
"""
from __future__ import annotations

import logging
import os
import sys
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.execution.alpaca_client import BuySubmission
from investment_strategy.ledger import TradeLedger, TradeRecord
from investment_strategy.models import (
    AccountSnapshot, Action, Position, RiskDecision, RiskVerdict, TradeProposal,
)
from investment_strategy.monitor.watchdog import Watchdog
from investment_strategy.orchestrator import Orchestrator
from investment_strategy.regime import Regime
from investment_strategy.risk import _TRADING_DAYS_SQRT
from investment_strategy.state import PortfolioState

# A quiet name: 1.25%/day -> raw 2-sigma stop 2.5%, live floor 4.0, shadow 6.0.
_VOL = 1.25 / 100.0 * _TRADING_DAYS_SQRT
_SEP9_REASON = ("SPY above 200dma (762 vs 711, today -0.42%), VIX 16.0, "
                "VIX3M 18.6 (contango) -> risk-on, size x1.00.")


def _tmp(prefix):
    return os.path.join(tempfile.gettempdir(), f"_{prefix}_{uuid.uuid4().hex}")


def _acct():
    return AccountSnapshot(equity=1_000_000.0, last_equity=1_000_000.0,
                           cash=500_000.0, buying_power=1_000_000.0, positions=[])


def _buy_prop(symbol="NU"):
    return TradeProposal(symbol=symbol, action=Action.BUY, conviction=0.7,
                         target_weight_pct=3.0, rationale="test thesis")


def _decision(prop, notional=30_916.0, stop=4.0):
    return RiskDecision(proposal=prop, verdict=RiskVerdict.APPROVED,
                        approved_qty=notional / 100.0, approved_notional=notional,
                        stop_loss_pct=stop, take_profit_pct=stop * 2.5,
                        reason="Sized within caps.")


class _Ledger:
    def __init__(self):
        self.records = []

    def record(self, rec):
        self.records.append(rec)


def _buy_orch(regime=None, falling=None, vol=_VOL, vol_stops=True, ext=None):
    """The minimum surface _handle_equity's BUY path touches, with the risk
    verdict scripted (the tape is an annotation of an already-sized buy)."""
    o = Orchestrator.__new__(Orchestrator)
    o.cfg = SimpleNamespace(
        core_etf="", hedge_etf="", defensive_core_etf="",
        risk=SimpleNamespace(max_pairwise_corr=0.0, fractional_enabled=True),
    )
    o.broker = SimpleNamespace(
        latest_price=lambda s: 100.0, annualized_vol=lambda s: vol,
        open_buy_notional=lambda s: 0.0,
        submit_from_decision=lambda d: BuySubmission(
            "oid-nu", False, d.approved_qty, d.approved_notional),
    )
    o.earnings = SimpleNamespace(days_until_earnings=lambda s: None)
    o._sector_context = lambda s, a: (None, 0.0)
    o._journal_decision = lambda *a, **k: None
    o._regime_mult, o._regime_trend, o._regime_label = 1.0, "up", "risk-on"
    o._falling_names = dict(falling or {})
    o.regime = SimpleNamespace(current=lambda: regime)
    o.risk = SimpleNamespace(
        evaluate=lambda proposal, *a, **k: _decision(proposal),
        limits=SimpleNamespace(
            vol_stops_enabled=vol_stops, vol_stop_mult=2.0,
            vol_stop_max_pct=10.0, stop_cover_extension=True,
        ),
    )
    o.ledger = _Ledger()
    o.state = PortfolioState(path=_tmp("tape") + ".json")
    o._trade_lock = threading.Lock()
    o._pending_oids = []
    o._tech = {"ext_pct_sma20": ext} if ext is not None else None
    return o


def test_buy_row_is_stamped_with_the_cycle_tape_and_logs_entry_tape(caplog):
    reg = Regime(1.0, "risk-on", _SEP9_REASON, trend="up", day_change_pct=-0.42)
    o = _buy_orch(regime=reg, falling={"SPCX": "-4.0% today", "DRAM": "-5.5% today"})
    caplog.set_level(logging.INFO, logger="orchestrator")

    o._handle_equity(_buy_prop("NU"), _acct(), [], tech=o._tech, composite=0.4)

    assert len(o.ledger.records) == 1
    row = o.ledger.records[0]
    assert row.action == "buy" and row.symbol == "NU"
    assert row.spy_intraday_ret_at_decision == -0.42
    assert row.regime_label == "risk-on"
    assert row.falling_names == ["DRAM", "SPCX"]          # sorted, from the map
    assert row.would_haircut_usd == 15_458.0             # 0.5 x $30,916: red + 2 falling
    assert row.stop_pct_if_floor_6 == 6.0                # 2.5% raw lifted to the 6 floor
    assert abs(row.vol_stop_raw_pct - 2.5) < 1e-6
    assert row.stop_loss_pct == 4.0                      # the LIVE stop is untouched
    assert row.cost_usd == 30_916.0                      # and so is the size
    assert (
        "ENTRY TAPE: NU spy_intraday=-0.42% regime=risk-on falling=2 "
        "would_haircut=$15,458 stop=4.00% stop_if_floor6=6.00%"
    ) in caplog.text


def test_buy_row_haircut_is_zero_on_a_green_or_broad_tape():
    # Red but broad with one falling name -> rule would not fire -> 0.
    reg = Regime(1.0, "risk-on", _SEP9_REASON, trend="up", day_change_pct=-0.42)
    o = _buy_orch(regime=reg, falling={"SPCX": "-4.0% today"})
    o._handle_equity(_buy_prop("NU"), _acct(), [], composite=0.4)
    assert o.ledger.records[0].would_haircut_usd == 0.0
    # Narrow breadth (the reason's breadth note) counts as weak tape.
    narrow = Regime(0.7, "neutral", _SEP9_REASON.replace(
        " -> risk-on", ", QQQ+IWM below 50dma (narrow breadth) -> neutral"),
        trend="up", day_change_pct=-0.35)
    o2 = _buy_orch(regime=narrow)
    o2._handle_equity(_buy_prop("NU"), _acct(), [], composite=0.4)
    assert o2.ledger.records[0].would_haircut_usd == 15_458.0
    assert o2.ledger.records[0].regime_label == "neutral"
    # Green tape -> 0 even with the whole book falling.
    green = Regime(1.0, "risk-on", _SEP9_REASON, trend="up", day_change_pct=+0.9)
    o3 = _buy_orch(regime=green, falling={"A": "x", "B": "y", "C": "z"})
    o3._handle_equity(_buy_prop("NU"), _acct(), [], composite=0.4)
    assert o3.ledger.records[0].would_haircut_usd == 0.0


def test_buy_row_tape_fails_open_to_none_when_reads_are_unavailable(caplog):
    # No regime read this cycle (filter off / degraded), vol unknown: the row
    # still records, the shadow fields are None (not 0), the line still prints.
    o = _buy_orch(regime=None, vol=None)
    caplog.set_level(logging.INFO, logger="orchestrator")
    o._handle_equity(_buy_prop("NU"), _acct(), [], composite=0.4)
    row = o.ledger.records[0]
    assert row.spy_intraday_ret_at_decision is None
    assert row.would_haircut_usd is None
    assert row.stop_pct_if_floor_6 is None and row.vol_stop_raw_pct is None
    assert row.regime_label == "risk-on"          # falls back to the cycle label
    assert "ENTRY TAPE: NU spy_intraday=n/a regime=risk-on falling=0 would_haircut=n/a" in caplog.text
    # Vol stops off: the clamp-floor shadow is meaningless -> None.
    o2 = _buy_orch(regime=None, vol_stops=False)
    o2._handle_equity(_buy_prop("NU"), _acct(), [], composite=0.4)
    assert o2.ledger.records[0].stop_pct_if_floor_6 is None
    # A broken regime reader must not block the buy.
    o3 = _buy_orch(regime=None)
    o3.regime = SimpleNamespace(current=lambda: (_ for _ in ()).throw(RuntimeError("x")))
    o3._handle_equity(_buy_prop("NU"), _acct(), [], composite=0.4)
    assert len(o3.ledger.records) == 1


def test_buy_row_shadow_stop_honours_the_cover_extension():
    # Extension 8% over the 20d SMA widens both the live and the shadow stop.
    o = _buy_orch(regime=None, ext=8.0)
    o._handle_equity(_buy_prop("NU"), _acct(), [], tech=o._tech, composite=0.4)
    assert o.ledger.records[0].stop_pct_if_floor_6 == 8.0


# --------------------------------------------------------------------------- #
# STOP exits: floor6_would_survive
# --------------------------------------------------------------------------- #
class _BackfillLedger(_Ledger):
    def __init__(self, records):
        super().__init__()
        self.records = list(records)

    def all(self):
        return list(self.records)

    def effective(self):
        return list(self.records)


def _series_factory(rows, asked):
    def _series(symbol, days):
        asked.append((symbol, days))
        return list(rows)
    return _series


def _backfill_orch(closed, records, series_rows=None, series=None):
    o = Orchestrator.__new__(Orchestrator)
    asked = []
    o.broker = SimpleNamespace(
        closed_sell_orders=lambda: closed,
        daily_close_series=series or _series_factory(series_rows or [], asked),
    )
    o.ledger = _BackfillLedger(records)
    o.state = PortfolioState(path=_tmp("bf") + ".json")
    o._asked = asked
    return o


def _buy_rec(symbol="NU", entry=100.0, oid="buy-1", qty=100.0, stop=4.0,
             ts=datetime(2026, 7, 1, 14, 0, tzinfo=timezone.utc)):
    return TradeRecord(symbol=symbol, action="buy", qty=qty, entry_price=entry,
                       cost_usd=entry * qty, order_id=oid, stop_loss_pct=stop, ts=ts)


def _closed(oid, symbol="NU", qty=100.0, price=95.5, otype="stop",
            filled_at="2026-07-06T15:30:00+00:00"):
    return {"order_id": oid, "symbol": symbol, "qty": qty, "price": price,
            "type": otype, "filled_at": filled_at}


_CLOSES = [("2026-06-30", 100.0), ("2026-07-01", 99.0), ("2026-07-02", 97.0),
           ("2026-07-03", 95.0), ("2026-07-06", 96.0), ("2026-07-07", 80.0)]


def test_bracket_stop_backfill_stamps_floor6_would_survive(caplog):
    # A 4%-floor stop filled intraday on Jul 6; closes never sat 6% under the
    # basis (worst -5.0% on Jul 3), the Jul 7 print is after the exit.
    o = _backfill_orch([_closed("leg-stop")], [_buy_rec()], series_rows=_CLOSES)
    caplog.set_level(logging.INFO, logger="orchestrator")
    o._backfill_exchange_exits()
    row = o.ledger.records[-1]
    assert row.exit_reason == "bracket_stop"
    assert row.floor6_would_survive is True
    assert row.floor6_worst_close_pct == -5.0
    assert o._asked == [("NU", 10)]              # 5 calendar days + 5 buffer
    assert ("FLOOR6 SHADOW: NU bracket_stop worst_close=-5.00% vs basis 100.00 "
            "(live stop 4.00%, floor under test 6%) -> would_survive=True "
            "(close-based proxy)") in caplog.text
    # The realized figures are exactly what they were before the shadow.
    assert abs(row.realized_pl_pct - (-4.5)) < 1e-9
    assert row.fill_price == 95.5


def test_bracket_stop_backfill_breach_and_non_stop_rows_left_none():
    closes = [("2026-07-01", 99.0), ("2026-07-02", 93.5), ("2026-07-06", 96.0)]
    o = _backfill_orch(
        [_closed("leg-stop"), _closed("leg-take", symbol="AAPL", price=120.0, otype="limit")],
        [_buy_rec(), _buy_rec("AAPL", oid="buy-2")], series_rows=closes,
    )
    o._backfill_exchange_exits()
    stop, take = o.ledger.records[-2:]
    assert stop.exit_reason == "bracket_stop"
    assert stop.floor6_would_survive is False and stop.floor6_worst_close_pct == -6.5
    assert take.exit_reason == "bracket_take"
    assert take.floor6_would_survive is None and take.floor6_worst_close_pct is None
    assert [s for s, _ in o._asked] == ["NU"]     # the take never fetched bars


def test_bracket_stop_backfill_unreadable_series_is_none_not_false():
    def _boom(symbol, days):
        raise RuntimeError("bars down")
    o = _backfill_orch([_closed("leg-stop")], [_buy_rec()], series=_boom)
    o._backfill_exchange_exits()
    row = o.ledger.records[-1]
    assert row.exit_reason == "bracket_stop" and row.realized_pl is not None
    assert row.floor6_would_survive is None
    # No daily_close_series on the broker at all (older fakes): same.
    o2 = _backfill_orch([_closed("leg-stop")], [_buy_rec()])
    del o2.broker.daily_close_series
    o2._backfill_exchange_exits()
    assert o2.ledger.records[-1].floor6_would_survive is None


def _wd_cfg():
    return SimpleNamespace(
        risk=SimpleNamespace(
            equity_floor_pct=0.0, max_daily_loss_pct=3.0, max_hold_days=0.0,
            time_stop_min_gain_pct=2.0, scale_out_enabled=False,
            scale_out_pct=50.0, whole_shares_only=False,
        ),
        state_file=_tmp("wdstate") + ".json",
    )


def test_watchdog_hard_stop_exit_stamps_floor6_from_ledger_lots(caplog):
    led = TradeLedger(path=_tmp("wdled") + ".jsonl", restate_at_fill=False)
    led.record(_buy_rec(qty=10.0, ts=datetime(2026, 7, 1, 14, 0, tzinfo=timezone.utc)))
    asked = []
    broker = SimpleNamespace(daily_close_series=_series_factory(
        [("2026-07-01", 99.0), ("2026-07-02", 93.5)], asked))
    wd = Watchdog(_wd_cfg(), broker, state=PortfolioState(path=_tmp("wd") + ".json"),
                  ledger=led)
    pos = Position(symbol="NU", qty=10.0, avg_entry_price=100.0, current_price=95.9,
                   market_value=959.0, unrealized_pl=-41.0, unrealized_pl_pct=-4.1)
    caplog.set_level(logging.INFO, logger="watchdog")
    caplog.set_level(logging.INFO, logger="orchestrator")
    wd._record_exit(pos, "oid-stop", "stop")
    row = led.all()[-1]
    assert row.action == "sell" and row.exit_reason == "stop"
    # Fix-pass (review 2 #2): the SAFETY thread never fetches bars — it
    # records the row with the shadow None, takes the lot/basis locally
    # (before the sell row consumed the lot) and queues the job.
    assert row.floor6_would_survive is None and row.floor6_worst_close_pct is None
    assert asked == []
    assert "FLOOR6 SHADOW: NU stop deferred to the decision thread (basis 100.00" in caplog.text
    assert len(wd.floor_shadow_jobs) == 1
    job = wd.floor_shadow_jobs[0]
    assert job["symbol"] == "NU" and job["order_id"] == "oid-stop"
    assert job["basis"] == 100.0 and job["live_stop"] == 4.0
    assert job["entry_ts"] == datetime(2026, 7, 1, 14, 0, tzinfo=timezone.utc)
    # The DECISION thread drains the queue: one bars fetch, the line, the stamp.
    o = Orchestrator.__new__(Orchestrator)
    o.watchdog, o.broker, o.ledger = wd, broker, led
    o._drain_floor_shadow_jobs()
    row = led.all()[-1]
    assert row.order_id == "oid-stop"
    assert row.floor6_would_survive is False and row.floor6_worst_close_pct == -6.5
    assert asked and asked[0][0] == "NU"
    assert "FLOOR6 SHADOW: NU stop worst_close=-6.50% vs basis 100.00 (live stop 4.00%" in caplog.text
    assert wd.floor_shadow_jobs == []
    # The stamp touched the two shadow fields only.
    assert row.realized_pl == -41.0 and row.realized_pl_pct == -4.1 and row.qty == 10.0
    # A take / trail exit never queues and leaves the field None.
    wd._record_exit(pos, "oid-take", "take")
    assert led.all()[-1].floor6_would_survive is None
    assert wd.floor_shadow_jobs == []
    o._drain_floor_shadow_jobs()
    assert len(asked) == 1


def test_watchdog_stop_exit_without_ledger_lots_is_none_and_never_raises():
    led = TradeLedger(path=_tmp("wdled2") + ".jsonl", restate_at_fill=False)
    def _boom(symbol, days):
        raise RuntimeError("bars down")
    wd = Watchdog(_wd_cfg(), SimpleNamespace(daily_close_series=_boom),
                  state=PortfolioState(path=_tmp("wd2") + ".json"), ledger=led)
    pos = Position(symbol="NU", qty=10.0, avg_entry_price=100.0, current_price=95.9,
                   market_value=959.0, unrealized_pl=-41.0, unrealized_pl_pct=-4.1)
    wd._record_exit(pos, "oid-stop", "stop")          # no lot for NU
    assert led.all()[-1].floor6_would_survive is None
    assert wd.floor_shadow_jobs == []                  # nothing to compute
    # 20-sh lot dated Jul 1: the first exit's sell row (ts=now) FIFO-consumes
    # 10 of it on the next ledger read, so 10 remain for the second exit.
    led.record(_buy_rec(qty=20.0))
    wd._record_exit(pos, "oid-stop-2", "stop")        # lot present, bars down
    assert led.all()[-1].floor6_would_survive is None
    assert [j["order_id"] for j in wd.floor_shadow_jobs] == ["oid-stop-2"]
    o = Orchestrator.__new__(Orchestrator)
    o.watchdog, o.broker, o.ledger = wd, wd.broker, led
    o._drain_floor_shadow_jobs()                       # bars down -> None, no raise
    assert led.all()[-1].floor6_would_survive is None
    assert wd.floor_shadow_jobs == []
    # An orchestrator without a watchdog (scripts, tests) is a no-op.
    o2 = Orchestrator.__new__(Orchestrator)
    o2._drain_floor_shadow_jobs()
