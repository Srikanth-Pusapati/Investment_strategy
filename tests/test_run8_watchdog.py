"""Run-8 change-set: watchdog option-exit items N-1 and B-7c, plus the B-5
entry-buffer API on AlpacaClient.submit_option_legs.

N-1 OPTION_EXITS_RTH_ONLY — Oct 26 2026 06:30-08:29 CT the watchdog tried to
close an IWM put spread on a premium stop 113 times pre-market; every attempt
got Alpaca 42210000 "options market orders are only allowed during market
hours", logged CRITICAL "Option close FAILED ... Will retry." and paged. With
the knob ON a breach outside the regular session is logged ONCE as queued and
the close is submitted at the open; OFF = legacy (close attempted 24/7).

B-7c OPTION_MARK_FALLBACK=conservative — HYG 78P quoted bid 0.42 / ask 1.31
(100%+ spread) for 13 sessions; the absurd-spread veto skipped 977-3,055
ticks PER DAY Oct 8-20, so a -65% put never had a working premium stop. After
OPTION_MARK_FALLBACK_TICKS consecutive absurd-spread ticks the structure is
marked at liquidation (long legs at BID, short legs at ASK) and the premium
stop/take evaluates on that mark through the normal two-tick confirmation.
One-sided quotes still veto; a sane NBBO resets the counter; off = legacy.

B-5 (API only) — submit_option_legs(limit_buffer_pct=...) replaces the 20%
legacy entry buffer; the $0.02 floor still applies; None = legacy.
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy import session_calendar as sc
from investment_strategy.execution.alpaca_client import AlpacaClient
from investment_strategy.execution.options import occ_symbol
from investment_strategy.monitor.watchdog import Watchdog
from test_contract_handles import code_string_constants
from test_watchdog import (
    _FakeBroker, _FakeLedger, _cfg, _exp, _opt_leg, _pin_open_clock, _state,
)

ET = ZoneInfo("America/New_York")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WATCHDOG_SRC = os.path.join(ROOT, "investment_strategy", "monitor", "watchdog.py")

# Mon Oct 26 2026 — the IWM incident day. 07:30 ET == 06:30 CT (pre-market).
PREMARKET = datetime(2026, 10, 26, 7, 30, tzinfo=ET)
MIDDAY = datetime(2026, 10, 26, 11, 0, tzinfo=ET)
NEXT_PREMARKET = datetime(2026, 10, 27, 7, 30, tzinfo=ET)


@pytest.fixture
def no_calendar():
    """Pin the module-level session calendar to 'none' (weekday math) and
    restore whatever another test left there."""
    prev = sc.active()
    sc.set_active(None)
    try:
        yield
    finally:
        sc.set_active(prev)


def _wd(broker, state, *, rth_only=None, fallback=None, fallback_ticks=None,
        now_et=None, ledger=None, alerter=None):
    cfg = _cfg(0.0)
    if rth_only is not None:
        cfg.risk.option_exits_rth_only = rth_only
    if fallback is not None:
        cfg.risk.option_mark_fallback = fallback
    if fallback_ticks is not None:
        cfg.risk.option_mark_fallback_ticks = fallback_ticks
    wd = _pin_open_clock(Watchdog(cfg, broker, state=state, ledger=ledger,
                                  alerter=alerter))
    if now_et is not None:
        wd._now_et = lambda: now_et
    return wd


def _alerter(sink: list):
    return SimpleNamespace(critical=lambda key, subject, body: sink.append(key))


def _lines(caplog, needle: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if needle in r.getMessage()]


IWM_EXP = _exp(30)


def _iwm_put(price=1.0):
    # Long put at -66.7% of premium vs the default -50% stop width.
    return _opt_leg(occ_symbol("IWM", IWM_EXP, 230, "put"), qty=2, basis=3.0,
                    price=price)


# =========================================================================== #
# N-1 OPTION_EXITS_RTH_ONLY
# =========================================================================== #

def test_rth_only_premarket_stop_breach_is_queued_once_not_closed_or_paged(
        no_calendar, caplog):
    alerts: list[str] = []
    state, broker = _state(), _FakeBroker([], option_close_fails=True)
    wd = _wd(broker, state, rth_only=True, now_et=PREMARKET,
             alerter=_alerter(alerts))
    put = _iwm_put()
    # Even an already-CONFIRMED streak must not close pre-market, and the
    # counter must come out of the closed session exactly as it went in.
    wd._opt_breach[("IWM", IWM_EXP)] = ("stop", 2)
    caplog.set_level(logging.INFO, logger="watchdog")
    for _ in range(3):                                    # three 30s ticks
        wd._check_option_positions([put])
    assert broker.option_groups_closed == []
    assert alerts == []                                   # no page
    assert not [r for r in caplog.records if r.levelno >= logging.CRITICAL]
    queued = _lines(caplog, "OPTION EXIT QUEUED")
    assert len(queued) == 1, [r.getMessage() for r in queued]
    msg = queued[0].getMessage()
    assert queued[0].levelno == logging.INFO
    assert "closed market" in msg and "IWM" in msg and IWM_EXP in msg
    assert "premium stop" in msg and "will close at the open" in msg
    assert wd._opt_breach == {("IWM", IWM_EXP): ("stop", 2)}


def test_rth_only_inside_session_closes_as_before(no_calendar):
    state, broker = _state(), _FakeBroker([])
    wd = _wd(broker, state, rth_only=True, now_et=MIDDAY)
    put = _iwm_put()
    wd._check_option_positions([put])                     # breach tick 1/2
    assert broker.option_groups_closed == []
    wd._check_option_positions([put])                     # tick 2/2 -> fires
    assert broker.option_groups_closed == [[put.symbol]]


def test_rth_only_off_premarket_keeps_legacy_close_attempt(no_calendar, caplog):
    # OFF (explicit False AND the knob absent from cfg): byte-identical legacy
    # behaviour — the close is attempted pre-market and nothing is queued.
    for rth_only in (False, None):
        state, broker = _state(), _FakeBroker([])
        wd = _wd(broker, state, rth_only=rth_only, now_et=PREMARKET)
        put = _iwm_put()
        caplog.set_level(logging.INFO, logger="watchdog")
        wd._check_option_positions([put])
        wd._check_option_positions([put])
        assert broker.option_groups_closed == [[put.symbol]]
        assert _lines(caplog, "OPTION EXIT QUEUED") == []


def test_rth_only_premarket_expiry_close_is_queued_then_fires_at_open(
        no_calendar, caplog):
    state, broker, led = _state(), _FakeBroker([]), _FakeLedger()
    wd = _wd(broker, state, rth_only=True, now_et=PREMARKET, ledger=led)
    call = _opt_leg(occ_symbol("TSM", _exp(2), 250, "call"), qty=1, basis=2.0,
                    price=2.1)                            # P&L fine, 2 DTE
    caplog.set_level(logging.INFO, logger="watchdog")
    for _ in range(3):
        wd._check_option_positions([call])
    assert broker.option_groups_closed == []
    queued = _lines(caplog, "OPTION EXIT QUEUED")
    assert len(queued) == 1 and "DTE" in queued[0].getMessage()
    wd._now_et = lambda: MIDDAY                           # the open arrives
    wd._check_option_positions([call])
    assert broker.option_groups_closed == [[call.symbol]]
    assert led.records[-1].exit_reason == "option_expiry"


def test_rth_only_queue_note_relogs_on_a_new_et_date(no_calendar, caplog):
    state, broker = _state(), _FakeBroker([])
    wd = _wd(broker, state, rth_only=True, now_et=PREMARKET)
    put = _iwm_put()
    caplog.set_level(logging.INFO, logger="watchdog")
    wd._check_option_positions([put])
    wd._check_option_positions([put])
    assert len(_lines(caplog, "OPTION EXIT QUEUED")) == 1
    wd._now_et = lambda: NEXT_PREMARKET                   # next closed session
    wd._check_option_positions([put])
    wd._check_option_positions([put])
    assert len(_lines(caplog, "OPTION EXIT QUEUED")) == 2
    assert broker.option_groups_closed == []


def test_in_regular_session_weekday_fallback_without_a_calendar(no_calendar):
    wd = _wd(_FakeBroker([]), _state())
    d = lambda h, m, day=26: datetime(2026, 10, day, h, m, tzinfo=ET)  # noqa: E731
    assert wd._in_regular_session(d(7, 30)) is False      # pre-market
    assert wd._in_regular_session(d(9, 29)) is False
    assert wd._in_regular_session(d(9, 30)) is True       # the open
    assert wd._in_regular_session(d(15, 59)) is True
    assert wd._in_regular_session(d(16, 0)) is True       # the close itself
    assert wd._in_regular_session(d(16, 1)) is False      # after hours
    assert wd._in_regular_session(d(11, 0, day=24)) is False   # Saturday


def test_in_regular_session_honors_active_calendar_holiday_and_early_close():
    # Thanksgiving week 2026: Thu Nov 26 absent (holiday), Fri Nov 27 closes
    # 13:00. The weekday fallback would call both of those "open".
    sessions = {
        "2026-11-23": ("09:30", "16:00"), "2026-11-24": ("09:30", "16:00"),
        "2026-11-25": ("09:30", "16:00"), "2026-11-27": ("09:30", "13:00"),
        "2026-11-30": ("09:30", "16:00"), "2026-12-01": ("09:30", "16:00"),
    }
    cal = sc.SessionCalendar(None)
    assert cal.update(sessions, date(2026, 11, 20), date(2026, 12, 4),
                      date(2026, 11, 23)) is True
    prev = sc.active()
    sc.set_active(cal)
    try:
        wd = _wd(_FakeBroker([]), _state())
        et = lambda *a: datetime(*a, tzinfo=ET)           # noqa: E731
        assert wd._in_regular_session(et(2026, 11, 23, 10, 0)) is True
        assert wd._in_regular_session(et(2026, 11, 26, 11, 0)) is False  # holiday
        assert wd._in_regular_session(et(2026, 11, 27, 12, 59)) is True
        assert wd._in_regular_session(et(2026, 11, 27, 13, 30)) is False  # early close
    finally:
        sc.set_active(prev)


def test_rth_only_clock_failure_fails_toward_protection(no_calendar):
    # A clock/calendar read that raises must NOT silently park every option
    # exit: the gate reads as 'in session' and the close goes out.
    state, broker = _state(), _FakeBroker([])
    wd = _wd(broker, state, rth_only=True)

    def _boom():
        raise RuntimeError("clock unreadable")

    wd._now_et = _boom
    assert wd._in_regular_session() is True
    put = _iwm_put()
    wd._check_option_positions([put])
    wd._check_option_positions([put])
    assert broker.option_groups_closed == [[put.symbol]]


# =========================================================================== #
# B-7c OPTION_MARK_FALLBACK=conservative
# =========================================================================== #

HYG_EXP = _exp(30)
HYG_KEY = ("HYG", HYG_EXP)
# Run-7 HYG 78P: entry 1.26, broker mark ~0.44 (-65%), NBBO 0.42 / 1.31.
HYG_QUOTE = (0.42, 1.31)


def _hyg_put(price=0.44):
    return _opt_leg(occ_symbol("HYG", HYG_EXP, 78, "put"), qty=1, basis=1.26,
                    price=price)


def test_mark_fallback_off_absurd_spread_skips_every_tick(caplog):
    for fallback in ("off", None):
        state, broker = _state(), _FakeBroker([])
        wd = _wd(broker, state, fallback=fallback, fallback_ticks=1)
        put = _hyg_put()
        broker.option_quotes[put.symbol] = HYG_QUOTE
        caplog.set_level(logging.INFO, logger="watchdog")
        for _ in range(5):
            wd._check_option_positions([put])
        assert broker.option_groups_closed == []
        assert _lines(caplog, "OPTION MARK FALLBACK:") == []
        assert len(_lines(caplog, "premium mark unreliable")) >= 5
        assert wd._opt_absurd == {}                        # never counted


def test_mark_fallback_conservative_marks_long_put_at_bid_after_n_ticks(caplog):
    state, broker, led = _state(), _FakeBroker([]), _FakeLedger()
    wd = _wd(broker, state, fallback="conservative", fallback_ticks=3, ledger=led)
    put = _hyg_put()
    broker.option_quotes[put.symbol] = HYG_QUOTE
    caplog.set_level(logging.INFO, logger="watchdog")
    wd._check_option_positions([put])                     # absurd tick 1: skip
    wd._check_option_positions([put])                     # absurd tick 2: skip
    assert broker.option_groups_closed == []
    assert _lines(caplog, "OPTION MARK FALLBACK:") == []
    assert len(_lines(caplog, "premium mark unreliable")) == 2
    assert wd._opt_absurd[HYG_KEY] == 2
    assert wd._opt_breach.get(HYG_KEY, ("stop", 0))[1] == 0   # streak held at 0

    wd._check_option_positions([put])                     # tick 3: fallback mark
    assert broker.option_groups_closed == []              # breach tick 1/2 only
    fb = _lines(caplog, "OPTION MARK FALLBACK:")
    assert len(fb) == 1 and fb[0].levelno == logging.WARNING
    msg = fb[0].getMessage()
    # basis 1.26 x 1 x 100 = $126; long leg at BID 0.42 -> $42 -> -66.7%
    assert "HYG" in msg and HYG_EXP in msg and "conservative mark $42 " in msg
    assert "(long@bid/short@ask)" in msg and "-66.7%" in msg
    assert "after 3 absurd-spread ticks" in msg
    assert wd._opt_breach[HYG_KEY] == ("stop", 1)
    assert _lines(caplog, "breach tick 1/2")               # awaiting confirmation

    wd._check_option_positions([put])                     # tick 4: 2/2 -> close
    assert broker.option_groups_closed == [[put.symbol]]
    fb = _lines(caplog, "OPTION MARK FALLBACK:")
    assert len(fb) == 2 and fb[1].levelno == logging.INFO  # later ticks: INFO
    rec = led.records[-1]
    assert rec.exit_reason == "stop" and rec.instrument == "option"
    assert rec.realized_pl_pct == pytest.approx(-66.7, abs=0.05)
    assert rec.realized_pl == pytest.approx(-84.0, abs=0.01)


def test_mark_fallback_marks_short_leg_at_ask(caplog):
    # Bear put spread: long 160P x2 @3.00, short 150P x-2 @1.00 -> basis $400.
    exp = _exp(30)
    long_put = _opt_leg(occ_symbol("AMD", exp, 160, "put"), qty=2, basis=3.0,
                        price=1.0)
    short_put = _opt_leg(occ_symbol("AMD", exp, 150, "put"), qty=-2, basis=1.0,
                         price=0.55)                      # broker: -77.5%
    state, broker = _state(), _FakeBroker([])
    wd = _wd(broker, state, fallback="conservative", fallback_ticks=1)
    broker.option_quotes[long_put.symbol] = (0.80, 2.00)  # ~86% spread: absurd
    broker.option_quotes[short_put.symbol] = (0.50, 0.60)  # sane
    caplog.set_level(logging.INFO, logger="watchdog")
    wd._check_option_positions([long_put, short_put])     # fallback, 1/2
    assert broker.option_groups_closed == []
    fb = _lines(caplog, "OPTION MARK FALLBACK:")
    assert len(fb) == 1
    msg = fb[0].getMessage()
    # long@bid 0.80 x 2 x 100 = 160; short@ask 0.60 x -2 x 100 = -120 -> $40
    assert "conservative mark $40 " in msg and "-90.0%" in msg
    wd._check_option_positions([long_put, short_put])     # 2/2 -> ONE close
    assert len(broker.option_groups_closed) == 1
    assert sorted(broker.option_groups_closed[0]) == sorted(
        [long_put.symbol, short_put.symbol])


def test_mark_fallback_one_sided_quote_still_vetoes(caplog):
    state, broker = _state(), _FakeBroker([])
    wd = _wd(broker, state, fallback="conservative", fallback_ticks=1)
    put = _hyg_put()
    broker.option_quotes[put.symbol] = (0.0, 1.31)        # bid vanished
    caplog.set_level(logging.INFO, logger="watchdog")
    for _ in range(4):
        wd._check_option_positions([put])
    assert broker.option_groups_closed == []
    assert _lines(caplog, "OPTION MARK FALLBACK:") == []
    assert len(_lines(caplog, "one-sided quote")) == 4
    assert HYG_KEY not in wd._opt_absurd                   # not an absurd tick


def test_mark_fallback_needs_every_leg_two_sided(caplog):
    # Long leg absurd, short leg one-sided: no liquidation mark can be built
    # (a short leg at ask=0 would be a free close), so the tick still skips.
    exp = _exp(30)
    long_put = _opt_leg(occ_symbol("AMD", exp, 160, "put"), qty=2, basis=3.0,
                        price=1.0)
    short_put = _opt_leg(occ_symbol("AMD", exp, 150, "put"), qty=-2, basis=1.0,
                         price=0.55)
    state, broker = _state(), _FakeBroker([])
    wd = _wd(broker, state, fallback="conservative", fallback_ticks=1)
    broker.option_quotes[long_put.symbol] = (0.80, 2.00)
    broker.option_quotes[short_put.symbol] = (0.50, 0.0)
    caplog.set_level(logging.INFO, logger="watchdog")
    for _ in range(3):
        wd._check_option_positions([long_put, short_put])
    assert broker.option_groups_closed == []
    assert _lines(caplog, "OPTION MARK FALLBACK:") == []


def test_mark_fallback_sane_quote_resets_absurd_counter(caplog):
    state, broker = _state(), _FakeBroker([])
    wd = _wd(broker, state, fallback="conservative", fallback_ticks=3)
    put = _hyg_put()
    broker.option_quotes[put.symbol] = HYG_QUOTE
    caplog.set_level(logging.INFO, logger="watchdog")
    wd._check_option_positions([put])                     # absurd 1
    wd._check_option_positions([put])                     # absurd 2
    assert wd._opt_absurd[HYG_KEY] == 2
    broker.option_quotes[put.symbol] = (0.43, 0.45)       # sane two-sided NBBO
    wd._check_option_positions([put])                     # counts: breach 1/2
    assert HYG_KEY not in wd._opt_absurd                   # counter reset
    assert wd._opt_breach[HYG_KEY] == ("stop", 1)
    broker.option_quotes[put.symbol] = HYG_QUOTE
    wd._check_option_positions([put])                     # absurd 1 (held 1/2)
    wd._check_option_positions([put])                     # absurd 2 (held 1/2)
    assert broker.option_groups_closed == []              # a stale 3 would fire
    assert wd._opt_absurd[HYG_KEY] == 2
    assert _lines(caplog, "OPTION MARK FALLBACK:") == []
    wd._check_option_positions([put])                     # absurd 3: fallback 2/2
    assert broker.option_groups_closed == [[put.symbol]]


def test_mark_fallback_counter_dropped_when_structure_leaves_the_book():
    state, broker = _state(), _FakeBroker([])
    wd = _wd(broker, state, fallback="conservative", fallback_ticks=5)
    put = _hyg_put()
    broker.option_quotes[put.symbol] = HYG_QUOTE
    wd._check_option_positions([put])
    assert wd._opt_absurd[HYG_KEY] == 1
    wd._check_option_positions([])                        # book empty
    assert wd._opt_absurd == {}


def test_run8_watchdog_handles_are_code_literals():
    with open(WATCHDOG_SRC, encoding="utf-8") as fh:
        consts = code_string_constants(fh.read(), WATCHDOG_SRC)
    for handle in ("OPTION EXIT QUEUED", "OPTION MARK FALLBACK:"):
        assert any(handle in c for c in consts), handle


# =========================================================================== #
# B-5 (API): submit_option_legs(limit_buffer_pct=...)
# =========================================================================== #

from alpaca.trading.enums import OrderSide as _OS            # noqa: E402
from alpaca.trading.enums import PositionIntent as _PI       # noqa: E402
from alpaca.trading.requests import OptionLegRequest as _OLR  # noqa: E402


class _FakeOptionTrading:
    def __init__(self):
        self.submitted = []

    def submit_order(self, req):
        self.submitted.append(req)
        return SimpleNamespace(id="opt-oid-1")


def _opt_client():
    c = AlpacaClient.__new__(AlpacaClient)
    c.cfg = SimpleNamespace(can_open_orders=True)
    c.trading = _FakeOptionTrading()
    return c


def _leg(symbol="HYG261120P00078000"):
    return _OLR(symbol=symbol, ratio_qty=1, side=_OS.BUY,
                position_intent=_PI.BUY_TO_OPEN)


def test_submit_option_legs_tier1_buffer_replaces_legacy_pct(caplog):
    c = _opt_client()
    caplog.set_level(logging.INFO, logger="alpaca")
    oid = c.submit_option_legs([_leg()], qty=50, est_premium_per_share=0.79,
                               limit_buffer_pct=5.0)
    assert oid == "opt-oid-1"
    req = c.trading.submitted[0]
    assert req.limit_price == 0.83          # 0.79 + max(0.79*0.05, 0.02) = 0.8295
    line = [r.getMessage() for r in caplog.records if "OPTION 1-leg order" in r.getMessage()]
    assert line and "buffer=5%" in line[0]


def test_submit_option_legs_default_buffer_is_legacy_twenty_pct(caplog):
    c = _opt_client()
    caplog.set_level(logging.INFO, logger="alpaca")
    c.submit_option_legs([_leg()], qty=50, est_premium_per_share=0.79)
    assert c.trading.submitted[0].limit_price == 0.95       # 0.79 + 0.158
    line = [r.getMessage() for r in caplog.records if "OPTION 1-leg order" in r.getMessage()]
    assert line and "buffer=20%" in line[0]
    c2 = _opt_client()
    c2.submit_option_legs([_leg()], qty=50, est_premium_per_share=0.79,
                          limit_buffer_pct=0.0)              # 0 = off = legacy
    assert c2.trading.submitted[0].limit_price == 0.95


def test_submit_option_legs_tier1_buffer_keeps_the_dollar_floor():
    c = _opt_client()
    c.submit_option_legs([_leg()], qty=10, est_premium_per_share=0.05,
                         limit_buffer_pct=5.0)
    assert c.trading.submitted[0].limit_price == 0.07       # 5% = $0.0025 < $0.02


def test_submit_option_legs_multi_leg_uses_tier1_buffer_too():
    c = _opt_client()
    legs = [
        _OLR(symbol="AMD261120P00160000", ratio_qty=1, side=_OS.BUY,
             position_intent=_PI.BUY_TO_OPEN),
        _OLR(symbol="AMD261120P00150000", ratio_qty=1, side=_OS.SELL,
             position_intent=_PI.SELL_TO_OPEN),
    ]
    c.submit_option_legs(legs, qty=2, est_premium_per_share=2.00,
                         limit_buffer_pct=5.0)
    assert c.trading.submitted[0].limit_price == 2.10       # 2.00 + 0.10
