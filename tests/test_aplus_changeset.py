"""Tests for the Sep 21 2026 A+ change-set (items A-1 .. A-9).

Each block names the item it covers and the run-6 incident that motivated it.
Pure logic / fakes only — no network, no broker."""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.ledger import TradeRecord
from investment_strategy.models import Position
from investment_strategy.monitor.watchdog import Watchdog
from investment_strategy.state import PortfolioState

_spec = importlib.util.spec_from_file_location(
    "eval_contract_check",
    Path(__file__).resolve().parents[1] / "scripts" / "eval_contract_check.py",
)
ecc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ecc)


# --------------------------------------------------------------------------- #
# A-5 — the event sanction rides on decision-sell ledger rows; v3 rule 8 counts
# only UNSANCTIONED decision-sell losses (run-6 NO-GO on INTC Sep 14, 0.49x).
# --------------------------------------------------------------------------- #
class _Ledger:
    def __init__(self):
        self.rows: list[TradeRecord] = []

    def record(self, rec):
        self.rows.append(rec)


def _state() -> PortfolioState:
    p = os.path.join(tempfile.gettempdir(), f"_aplus_{uuid.uuid4().hex}.json")
    return PortfolioState(path=p)


def _wd_cfg():
    return SimpleNamespace(
        risk=SimpleNamespace(
            equity_floor_pct=0.0, max_daily_loss_pct=3.0, max_hold_days=0.0,
            time_stop_min_gain_pct=2.0, scale_out_enabled=False,
            scale_out_pct=50.0, whole_shares_only=False,
        ),
        state_file="state/risk_state.json", monitor_interval_s=30,
    )


def _pos(symbol="INTC"):
    return Position(symbol=symbol, qty=503.0, avg_entry_price=101.7,
                    current_price=97.28, market_value=48931.84,
                    unrealized_pl=-2223.26, unrealized_pl_pct=-4.35)


def test_a5_for_sell_carries_the_sanction_and_defaults_empty():
    ev = ["name_falling:-5.6% today vs SPY -0.7%"]
    rec = TradeRecord.for_sell("INTC", "loss-cut", "oid-1", qty=503,
                               realized_pl_pct=-4.33, realized_pl=-2216.87,
                               exit_reason="decision", sell_events=ev)
    assert rec.sell_events == ev
    plain = TradeRecord.for_sell("NOK", "watchdog stop", "oid-2", qty=10,
                                 exit_reason="stop")
    assert plain.sell_events == []


def test_a5_watchdog_record_exit_uses_the_note_for_decision_only():
    led = _Ledger()
    wd = Watchdog(_wd_cfg(), SimpleNamespace(), state=_state(), ledger=led)
    wd.note_sell_events("INTC", ["name_falling:-5.6% today vs SPY -0.7%"])
    wd._record_exit(_pos("INTC"), None, "decision")
    wd._record_exit(_pos("INTC"), None, "stop")
    assert led.rows[0].sell_events == ["name_falling:-5.6% today vs SPY -0.7%"]
    assert led.rows[1].sell_events == []          # mechanical exit: never tagged
    # An empty note clears a stale sanction (a stop-reached decision sell).
    wd.note_sell_events("INTC", [])
    wd._record_exit(_pos("INTC"), None, "decision")
    assert led.rows[2].sell_events == []


def test_a5_queue_decision_sell_persists_the_sanction():
    st = _state()
    st.queue_decision_sell("RIG", "loss-cut", ["technical"], 1.8,
                           sell_events=["name_falling:-5.2% today vs SPY +0.5%"])
    info = st.get_pending_decision_sells()["RIG"]
    assert info["sell_events"] == ["name_falling:-5.2% today vs SPY +0.5%"]
    # Legacy 4-arg call still works and stores an empty list.
    st.queue_decision_sell("NOK", "x", [], None)
    assert st.get_pending_decision_sells()["NOK"]["sell_events"] == []


_ROWS = [
    {"ts": "2026-09-10T14:00:00Z", "symbol": "INTC", "action": "buy",
     "stop_loss_pct": 8.91},
    {"ts": "2026-09-14T14:25:43Z", "symbol": "INTC", "action": "sell",
     "exit_reason": "decision", "realized_pl_pct": -4.33, "realized_pl": -2216.87,
     "sell_events": ["name_falling:-5.6% today vs SPY -0.7%"]},
    {"ts": "2026-09-10T14:00:00Z", "symbol": "ZZZ", "action": "buy",
     "stop_loss_pct": 8.0},
    {"ts": "2026-09-14T15:00:00Z", "symbol": "ZZZ", "action": "sell",
     "exit_reason": "decision", "realized_pl_pct": -1.0, "realized_pl": -100.0},
]


def test_a5_checker_v3_counts_only_unsanctioned_rows():
    aware = ecc.decision_sell_losses_below_stop(
        _ROWS, "2026-09-01", "2026-09-30", 0.5, sanction_aware=True)
    assert aware["count"] == 1 and aware["rows"][0][1] == "ZZZ"
    assert [r[1] for r in aware["sanctioned_rows"]] == ["INTC"]
    assert aware["n_decision_losses"] == 2


def test_a5_checker_v2_reading_is_unchanged():
    legacy = ecc.decision_sell_losses_below_stop(
        _ROWS, "2026-09-01", "2026-09-30", 0.5)
    assert legacy["count"] == 2                   # as written: both rows count
    assert legacy["sanctioned_rows"] == []


# --------------------------------------------------------------------------- #
# A-1 / A-2 / A-4a — core fill: settle-wait before the BUY, a loud line when
# the BUY is refused, the CORE STOPLESS handle, and the beta clamp.
# Incident: Sep 15 2026 14:40:07 CT — cancel + BUY in the same instant, BUY
# wash-rejected 40310000, no log line, $137.6k QQQ stopless until 08:35 next day.
# --------------------------------------------------------------------------- #
import logging

import test_orchestrator as _to          # the shared orchestrator harness


class _CoreBroker(_to._FakeBroker):
    """Scripts the venue's view of the core stop: `settle_after` polls of
    open_stop_sells(resting_only=False) still list the cancelled stop."""

    def __init__(self, stop_listed=True, settle_after=1, buy_ok=True):
        super().__init__()
        self._listed = stop_listed
        self._settle_after = settle_after
        self._polls = 0
        self.buy_ok = buy_ok
        self.events: list[str] = []

    def open_stop_sells(self, symbol, resting_only=True):
        if resting_only:
            return [dict(o) for o in self.stop_orders]
        if not self._listed:
            return []
        self._polls += 1
        if self._polls > self._settle_after + 1:   # +1 = the had_stop probe
            return []
        return [{"id": "stop-1", "qty": 195.0, "stop_price": 602.98,
                 "status": "pending_cancel"}]

    def cancel_open_orders_for(self, symbol):
        self.events.append("cancel")
        super().cancel_open_orders_for(symbol)

    def submit_notional_buy(self, symbol, notional):
        self.events.append("buy")
        if not self.buy_ok:
            return None
        return super().submit_notional_buy(symbol, notional)


def _core_orch(broker, **cfg_extra):
    o = _to._orch(core_etf="QQQ", target_invested_pct=90.0, core_stop_pct=0.0)
    o.broker = broker
    o._CORE_TRIM_CANCEL_POLL_S = 0.0           # never sleep in tests
    for k, v in cfg_extra.items():
        setattr(o.cfg, k, v)
    return o


def test_a1_core_fill_waits_for_the_stop_cancel_to_settle_then_buys():
    b = _CoreBroker(stop_listed=True, settle_after=2)
    o = _core_orch(b)
    o._apply_core_fill(_to._acct(cash=1000.0, positions=[]))
    assert b.events == ["cancel", "buy"]
    assert b._polls >= 3                        # it polled until the view emptied
    assert len(b.core_buys) == 1


def test_a1_core_fill_skips_the_buy_when_the_cancel_never_settles(caplog):
    b = _CoreBroker(stop_listed=True, settle_after=10_000)
    o = _core_orch(b)
    with caplog.at_level(logging.WARNING):
        o._apply_core_fill(_to._acct(cash=1000.0, positions=[]))
    assert b.events == ["cancel"]               # no BUY into a wash reject
    assert o._core_stop_gap is True             # watchdog retry armed
    assert "CORE FILL: BUY of" in caplog.text and "still settling" in caplog.text


def test_a1_refused_buy_is_logged_and_arms_the_stop_retry(caplog):
    b = _CoreBroker(stop_listed=True, settle_after=0, buy_ok=False)
    o = _core_orch(b)
    with caplog.at_level(logging.WARNING):
        o._apply_core_fill(_to._acct(cash=1000.0, positions=[]))
    assert b.events == ["cancel", "buy"]
    assert o._core_stop_gap is True
    assert "NOT submitted (broker refused the order)" in caplog.text
    assert o.ledger.records == []               # nothing phantom in the ledger


def test_a1_no_stop_means_no_wait_and_no_warning(caplog):
    b = _CoreBroker(stop_listed=False, buy_ok=False)
    o = _core_orch(b)
    with caplog.at_level(logging.WARNING):
        o._apply_core_fill(_to._acct(cash=1000.0, positions=[]))
    assert b._polls == 0 and o._core_stop_gap is False
    assert "CORE FILL: BUY" not in caplog.text  # legacy quiet path unchanged


def test_a2_stopless_window_is_logged_once_when_the_stop_rests_again(caplog):
    o = _to._orch(core_etf="QQQ", core_stop_pct=15.0)
    o._note_core_stop_absent("QQQ", "core_fill")
    o._note_core_stop_absent("QQQ", "missing")   # the earlier cause wins
    t0, cause = o._core_stop_absent_since
    o._core_stop_absent_since = (t0 - 125.0, cause)
    with caplog.at_level(logging.INFO):
        o._log_core_stopless_closed("QQQ")
        o._log_core_stopless_closed("QQQ")       # second call: nothing open
    lines = [r.getMessage() for r in caplog.records if "CORE STOPLESS" in r.getMessage()]
    assert len(lines) == 1
    assert "QQQ 125." in lines[0] and "cause core_fill" in lines[0]
    assert caplog.records[0].levelno == logging.WARNING   # > 60 s pages the eye


def _clamp_orch(book, core_beta, on=True):
    o = _to._orch(core_etf="QQQ", target_invested_pct=90.0)
    o.cfg.core_fill_beta_clamp = on
    o.cfg.auto_hedge_mode = "beta"
    o.cfg.hedge_beta_target = 1.0
    o.cfg.hedge_beta_band = 0.15
    o._beta_context = lambda sym, acct: (book, core_beta)
    return o


def test_a4a_core_fill_is_clamped_to_the_room_under_the_arm_line():
    o = _clamp_orch(book=1.10, core_beta=1.5)
    acct = SimpleNamespace(equity=1_000_000.0)
    # room = (1.15 - 1.10) * 1e6 / 1.5 = 33,333.33
    assert o._core_fill_beta_clamp(acct, "QQQ", 50_000.0, 500.0) == 33_333.33
    assert o._core_fill_beta_clamp(acct, "QQQ", 20_000.0, 500.0) == 20_000.0


def test_a4a_core_fill_is_skipped_above_the_arm_line_and_fails_open():
    acct = SimpleNamespace(equity=1_000_000.0)
    assert _clamp_orch(1.20, 1.5)._core_fill_beta_clamp(acct, "QQQ", 50_000.0, 500.0) == 0.0
    # no reading -> unchanged; knob off -> unchanged
    assert _clamp_orch(None, None)._core_fill_beta_clamp(acct, "QQQ", 50_000.0, 500.0) == 50_000.0
    assert _clamp_orch(1.20, 1.5, on=False)._core_fill_beta_clamp(acct, "QQQ", 50_000.0, 500.0) == 50_000.0


# --------------------------------------------------------------------------- #
# A-4b — a hedge that cannot be funded trims the core ETF toward target.
# Incident: Sep 21 2026 09:33 CT — book spy-beta 1.20, invested 98%,
# "Auto-hedge: want $213703 more PSQ but only $0 spendable".
# --------------------------------------------------------------------------- #
def _starved_orch(on=True, core_beta=1.5, trim_ok=True, max_pct=50.0):
    o = _to._orch(core_etf="QQQ", core_stop_pct=0.0)
    o.cfg.hedge_starved_core_trim = on
    o.cfg.hedge_starved_trim_max_pct = max_pct
    o.book_beta = SimpleNamespace(beta_of=lambda sym, bench: core_beta)
    o._cycle_seq = 7
    o.trims = []

    def _sell(etf, pos, sell_qty, whole):
        o.trims.append((etf, sell_qty, whole))
        return ("oid-trim", "stop replaced") if trim_ok else (None, "replace refused")
    o._core_trim_sell = _sell
    return o


def _core_acct(qty=214.0, price=700.0, equity=1_050_000.0, cash=20_000.0):
    from investment_strategy.models import AccountSnapshot
    pos = Position(symbol="QQQ", qty=qty, avg_entry_price=708.28,
                   current_price=price, market_value=qty * price,
                   unrealized_pl=0.0, unrealized_pl_pct=-1.2)
    return AccountSnapshot(equity=equity, last_equity=equity, cash=cash,
                           buying_power=cash, positions=[pos])


def test_a4b_starved_hedge_sells_core_shares_toward_target():
    o = _starved_orch()
    acct = _core_acct()
    o._starved_hedge_core_trim(acct, beta=1.20, target=1.00, band=0.15)
    # want = 0.20 * 1.05e6 / 1.5 = $140,000; cap = 50% of $149,800 = $74,900
    # -> int(74,900 / 700) = 107 whole shares
    assert o.trims == [("QQQ", 107.0, 107.0)]
    rec = o.ledger.records[-1]
    assert rec.exit_reason == "beta_trim" and rec.qty == 107.0 and rec.symbol == "QQQ"
    assert acct.position_for("QQQ").qty == 107.0
    assert acct.cash == 20_000.0 + 107 * 700.0
    assert o._core_stop_gap is True and ("oid-trim", "QQQ") in o._pending_oids


def test_a4b_is_sized_by_the_beta_gap_when_that_is_smaller_than_the_cap():
    o = _starved_orch(max_pct=100.0)
    acct = _core_acct()
    o._starved_hedge_core_trim(acct, beta=1.17, target=1.00, band=0.15)
    # want = 0.17 * 1.05e6 / 1.5 = $119,000 -> 170 shares
    assert o.trims == [("QQQ", 170.0, 170.0)]


def test_a4b_once_per_cycle_off_by_default_and_quiet_on_failure():
    o = _starved_orch()
    acct = _core_acct()
    o._starved_hedge_core_trim(acct, 1.20, 1.00, 0.15)
    o._starved_hedge_core_trim(acct, 1.20, 1.00, 0.15)     # breadth re-arm pass
    assert len(o.trims) == 1
    off = _starved_orch(on=False)
    off._starved_hedge_core_trim(_core_acct(), 1.20, 1.00, 0.15)
    assert off.trims == [] and off.ledger.records == []
    bad = _starved_orch(trim_ok=False)
    a = _core_acct()
    bad._starved_hedge_core_trim(a, 1.20, 1.00, 0.15)
    assert bad.ledger.records == []                        # no phantom sell
    assert a.position_for("QQQ").qty == 214.0 and bad._core_stop_gap is True


def test_a4b_beta_trim_is_a_system_exit_for_the_v3_checker():
    assert "beta_trim" in ecc.V3_SYSTEM_EXIT_REASONS


# --------------------------------------------------------------------------- #
# A-6 — an approved put is never rendered to the model as a share purchase.
# Incident: Sep 16 2026 — HBAN long_put filled 09:25 CT; the next six cycles
# declined HBAN puts because "Account holds HBAN long (starter bought today)".
# --------------------------------------------------------------------------- #
from datetime import datetime, timezone

from investment_strategy.journal import DecisionJournal, DecisionRecord


def _journal_with(*recs):
    j = DecisionJournal(base_dir=tempfile.mkdtemp(prefix="_aplus_journal_"))
    for r in recs:
        j.record(r)
    return j


def _rec(symbol, instrument, verdict, notional, reason, conv=0.6, action="buy"):
    return DecisionRecord(
        ts=datetime.now(timezone.utc).isoformat(), symbol=symbol, action=action,
        instrument=instrument, conviction=conv, target_weight_pct=0.5,
        verdict=verdict, approved_notional=notional, reason=reason,
        rationale_head="corroborated breakdown",
    )


def test_a6_option_buy_is_listed_as_an_option_with_its_direction():
    j = _journal_with(
        _rec("HBAN", "option", "approved", 1650.0,
             "long_put: 50 contract(s), $1,650 debit (cap $5,096)."),
        _rec("BWIN", "equity", "approved", 17781.0, "sized to 1.7%"),
    )
    block = j.render_today(equity=1_000_000.0)
    bought = next(l for l in block.splitlines() if l.startswith("Bought:"))
    opts = next(l for l in block.splitlines() if l.startswith("Options opened"))
    assert "BWIN" in bought and "HBAN" not in bought
    assert "HBAN long_put (BEARISH) 1x ($1,650 debit, conv 0.60)" in opts
    assert "NOT shares" in opts


def test_a6_only_option_buys_still_says_no_shares_bought():
    j = _journal_with(_rec("HBAN", "option", "approved", 1650.0,
                           "bear_put_spread: 14 contract(s), $5,096 debit"))
    block = j.render_today(equity=1_000_000.0)
    assert "Bought: nothing yet today." in block
    assert "HBAN bear_put_spread (BEARISH)" in block


def test_a6_legacy_rows_without_a_strategy_prefix_render_as_option():
    j = _journal_with(_rec("HD", "option", "approved", 3430.0,
                           "1 contract(s), $3,430 debit (cap $5,000)."))
    assert "HD option 1x ($3,430 debit" in j.render_today(equity=1_000_000.0)


# --------------------------------------------------------------------------- #
# A-8 — the top-up conviction bar outlives the 7-day buy clock.
# Run-6: SMCI (Sep 10) and SPCX (Sep 15) top-ups, $65,573, passed only because
# the prior conviction had been pruned with the week-old buy stamp.
# --------------------------------------------------------------------------- #
from datetime import timedelta


def test_a8_conviction_survives_the_seven_day_clock_prune():
    st = _state()
    old = datetime.now(timezone.utc) - timedelta(days=9)
    st.register_buy("SMCI", when=old, conviction=0.66)
    st.register_buy("BWIN", conviction=0.61)        # a later buy prunes the clock
    assert "SMCI" not in st.last_buy_times           # 9-day-old stamp is gone...
    assert st.last_buy_conviction("SMCI") == 0.66    # ...the bar is not
    # and it survives a reload
    assert PortfolioState(path=st.path).last_buy_conviction("SMCI") == 0.66


def test_a8_conviction_map_is_bounded_by_count_oldest_first():
    st = _state()
    st._CONVICTION_MAX_ENTRIES = 3
    for i, sym in enumerate(["A", "B", "C", "D"]):
        st.register_buy(sym, conviction=0.5 + i / 100)
    assert list(st.last_buy_convictions) == ["B", "C", "D"]
    st.register_buy("B", conviction=0.9)             # a re-buy moves B to newest
    st.register_buy("E", conviction=0.7)
    assert list(st.last_buy_convictions) == ["D", "B", "E"]
