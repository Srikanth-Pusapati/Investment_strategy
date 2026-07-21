"""Tests for the watchdog's percentage equity floor — the latched catastrophe stop.

Pure logic, no network: a fake broker stands in for Alpaca. We assert the floor is
computed as a % of the PEAK high-water mark (so it auto-scales to any account size),
that breaching it flattens every position and latches a halt, and that it's off at 0.

Runnable two ways:
    .venv/bin/python tests/test_watchdog.py     # standalone, no pytest
    .venv/bin/pytest tests/                      # if pytest is installed
"""
from __future__ import annotations

import os
import sys
import tempfile
import uuid
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.models import AccountSnapshot, Position
from investment_strategy.monitor.watchdog import Watchdog
from investment_strategy.state import PortfolioState


class _FakeBroker:
    def __init__(self, positions, market_close_fails=False, stuck_replaces=(),
                 working_exit=False, option_close_fails=False):
        self._positions = positions
        self.market_close_fails = market_close_fails   # simulate closed/halted market
        # (new_order_id, qty, old_order_id, old_filled_qty) tuples
        # clear_orders_for_exit "replaces" per call
        self.stuck_replaces = list(stuck_replaces)
        # True -> a marketable sell exit is already resting (made on a prior
        # tick, not yet filled): the reserved shares are protected, not stranded.
        self.working_exit = working_exit
        # What the floor-breach confirming re-read returns. None -> the re-read
        # raises, which the watchdog treats as breach CONFIRMED (fail-safe), so
        # fixtures that don't wire an account keep the old one-read behavior.
        self.account: AccountSnapshot | None = None
        self.closed: list[str] = []
        self.canceled: list[str] = []
        self.reduced: list[tuple[str, float]] = []
        self.rested: list[tuple[str, float, float]] = []
        self.unwedged: list[tuple[str, float]] = []
        self.option_close_fails = option_close_fails
        self.option_groups_closed: list[list[str]] = []
        self.priced: list[str] = []      # every latest_price() lookup

    def get_account(self):
        if self.account is None:
            raise RuntimeError("no confirm account wired")
        return self.account

    def cancel_open_orders_for(self, symbol):
        self.canceled.append(symbol)

    def close_position(self, symbol):
        if self.market_close_fails:
            return None                 # market order can't fill (closed / halted)
        self.closed.append(symbol)
        return f"oid-{symbol}"          # truthy order id => confirmed close

    def reduce_position(self, symbol, qty):
        self.reduced.append((symbol, round(qty, 6)))
        return f"oid-{symbol}"

    def latest_price(self, symbol):
        self.priced.append(symbol)
        return 50.0

    def close_option_group(self, positions):
        if self.option_close_fails:
            return None
        self.option_groups_closed.append([p.symbol for p in positions])
        return "opt-close-1"

    def close_position_marketable_limit(self, symbol, qty, ref_price):
        self.rested.append((symbol, qty, ref_price))
        return f"rest-{symbol}"

    def clear_orders_for_exit(self, symbol, ref_price):
        self.unwedged.append((symbol, ref_price))
        return self.stuck_replaces

    def has_working_exit(self, symbol, ref_price):
        return self.working_exit

    def is_market_open(self):
        return getattr(self, "market_open", True)


def _cfg(pct, max_hold_days=0.0, time_stop_min_gain_pct=2.0,
         scale_out_enabled=False, scale_out_pct=50.0, whole_shares_only=False):
    return SimpleNamespace(
        risk=SimpleNamespace(
            equity_floor_pct=pct, max_daily_loss_pct=3.0,
            max_hold_days=max_hold_days, time_stop_min_gain_pct=time_stop_min_gain_pct,
            scale_out_enabled=scale_out_enabled, scale_out_pct=scale_out_pct,
            whole_shares_only=whole_shares_only,
        ),
        state_file="state/risk_state.json",
        monitor_interval_s=30,
    )


def _pos(symbol="AAPL"):
    return Position(symbol=symbol, qty=10.0, avg_entry_price=100.0,
                    current_price=50.0, market_value=500.0,
                    unrealized_pl=-500.0, unrealized_pl_pct=-50.0)


def _acct(equity):
    return AccountSnapshot(equity=equity, last_equity=equity, cash=0.0,
                           buying_power=0.0, positions=[_pos("AAPL"), _pos("MSFT")])


def _state() -> PortfolioState:
    p = os.path.join(tempfile.gettempdir(), f"_wd_{uuid.uuid4().hex}.json")
    return PortfolioState(path=p)


def _wd(pct, state):
    return Watchdog(_cfg(pct), _FakeBroker([]), state=state)


def test_trail_giveback_comes_from_risk_limits():
    # R.1: the trailing giveback is a knob (TRAIL_GIVEBACK_PCT), no longer a
    # hardcoded 3.0; fixtures without it still get the 3.0 default.
    cfg = _cfg(0.0)
    cfg.risk.trail_giveback_pct = 5.0
    assert Watchdog(cfg, _FakeBroker([]), state=_state()).trail_giveback_pct == 5.0
    assert _wd(0.0, _state()).trail_giveback_pct == 3.0


def test_trail_rth_gate_skips_trailing_when_market_closed():
    # TRAIL_RTH_ONLY: a winner that has given back past the trail threshold must
    # NOT trail out on a thin pre/post-market mark. Hard exits still run 24/7.
    state = _state()
    state.set_high_water("AAPL", 20.0)               # peaked at +20%
    up = Position(symbol="AAPL", qty=10.0, avg_entry_price=100.0,
                  current_price=110.0, market_value=1100.0,
                  unrealized_pl=100.0, unrealized_pl_pct=10.0)  # gave back to +10%
    acct = AccountSnapshot(equity=10_000.0, last_equity=10_000.0, cash=9_000.0,
                           buying_power=9_000.0, positions=[up])
    broker = _FakeBroker([])
    broker.account = acct
    cfg = _cfg(0.0)
    cfg.risk.trail_giveback_pct = 3.0
    cfg.risk.trail_rth_only = True

    wd = Watchdog(cfg, broker, state=state)
    broker.market_open = False
    wd.check_once()
    assert broker.closed == []                        # trail suppressed off-hours

    broker.market_open = True
    wd.check_once()
    assert "AAPL" in broker.closed                    # trails once RTH resumes


def test_vanished_position_fires_exchange_exit_callback():
    # A tracked position that is no longer live = an exchange bracket leg filled
    # with no code running; the backfill callback must fire immediately.
    state = _state()
    state.set_high_water("AAPL", 5.0)                 # tracked, but not in live book
    acct = AccountSnapshot(equity=10_000.0, last_equity=10_000.0, cash=10_000.0,
                           buying_power=10_000.0, positions=[])   # AAPL gone
    broker = _FakeBroker([])
    broker.account = acct
    fired = []
    wd = Watchdog(_cfg(0.0), broker, state=state,
                  on_exchange_exit=lambda: fired.append(True))
    wd.check_once()
    assert fired == [True]
    assert "AAPL" not in state.high_water             # and tracking was dropped


def test_floor_breached_flattens_and_latches():
    state = _state()
    state.peak_equity = 1_000.0           # floor = 60% -> $600
    wd = _wd(60.0, state)
    breached = wd._equity_floor_breached(_acct(equity=500.0))   # below $600
    assert breached is True
    assert state.halted is True
    assert sorted(wd.broker.closed) == ["AAPL", "MSFT"]         # everything flattened


def test_floor_not_breached_above_threshold():
    state = _state()
    state.peak_equity = 1_000.0           # floor $600
    wd = _wd(60.0, state)
    assert wd._equity_floor_breached(_acct(equity=700.0)) is False
    assert state.halted is False and wd.broker.closed == []


def test_floor_scales_with_peak():
    # Same 60% auto-scales: peak $1,000 -> floor $600; peak $1,000,000 -> floor $600k.
    small = _state(); small.peak_equity = 1_000.0
    assert _wd(60.0, small)._equity_floor_breached(_acct(550.0)) is True       # < $600
    big = _state(); big.peak_equity = 1_000_000.0
    assert _wd(60.0, big)._equity_floor_breached(_acct(550_000.0)) is True     # < $600k
    big2 = _state(); big2.peak_equity = 1_000_000.0
    assert _wd(60.0, big2)._equity_floor_breached(_acct(700_000.0)) is False   # > $600k


def test_floor_off_when_zero():
    state = _state()
    state.peak_equity = 1_000.0
    wd = _wd(0.0, state)
    assert wd._equity_floor_breached(_acct(equity=1.0)) is False   # disabled
    assert state.halted is False


def test_floor_breach_canceled_by_healthy_confirming_reread():
    # A single glitched snapshot (the 2026-07-07 equity==cash read) must NOT
    # latch the halt: the confirming re-read shows equity back above the floor.
    state = _state()
    state.peak_equity = 1_000.0           # floor $600
    wd = _wd(60.0, state)
    wd.broker.account = _acct(equity=900.0)                       # fresh read: healthy
    assert wd._equity_floor_breached(_acct(equity=500.0)) is False
    assert state.halted is False and wd.broker.closed == []


def test_floor_breach_confirmed_by_reread_latches():
    state = _state()
    state.peak_equity = 1_000.0           # floor $600
    wd = _wd(60.0, state)
    wd.broker.account = _acct(equity=480.0)                       # fresh read: still dead
    assert wd._equity_floor_breached(_acct(equity=500.0)) is True
    assert state.halted is True
    assert sorted(wd.broker.closed) == ["AAPL", "MSFT"]


# -- market-closed / halt exit fallback (1B.5) ------------------------------- #
def test_flatten_falls_back_to_marketable_limit_when_market_closed():
    state = _state()
    state.peak_equity = 1_000.0                       # floor 60% -> $600
    broker = _FakeBroker([], market_close_fails=True)  # market order won't fill
    wd = Watchdog(_cfg(60.0), broker, state=state)
    wd._equity_floor_breached(_acct(equity=500.0))     # triggers _flatten_all
    # Both names fell through the failed market close to a rested GTC limit.
    assert {r[0] for r in broker.rested} == {"AAPL", "MSFT"}
    assert broker.closed == []                         # no market close filled


def test_flatten_prefers_plain_market_close_when_open():
    state = _state()
    state.peak_equity = 1_000.0
    broker = _FakeBroker([], market_close_fails=False)
    wd = Watchdog(_cfg(60.0), broker, state=state)
    wd._equity_floor_breached(_acct(equity=500.0))
    assert sorted(broker.closed) == ["AAPL", "MSFT"]   # market close used
    assert broker.rested == []                         # fallback not needed


# -- shares locked by stuck pending-cancel orders (the FRHC wedge) ----------- #
def _pos_locked(symbol="FRHC", qty=110.000186, avail=1.000186, pl_pct=-12.0):
    """A position whose shares are (partly) reserved by open orders the broker
    won't release — a full close is rejected with 40310000."""
    price = 100.0 * (1 + pl_pct / 100.0)
    return Position(symbol=symbol, qty=qty, qty_available=avail,
                    avg_entry_price=100.0, current_price=price,
                    market_value=qty * price, unrealized_pl=qty * pl_pct,
                    unrealized_pl_pct=pl_pct)


def test_qty_available_defaults_to_full_qty():
    # Constructors that don't know qty_available (backtest, older call sites)
    # must NOT look "fully locked" (that would reroute every failed close).
    p = _pos("AAPL")
    assert p.qty_available == p.qty


def test_hard_stop_partial_close_when_shares_held_by_stuck_orders():
    state = _state()
    state.register_exits("FRHC", stop_pct=10.0, take_pct=25.0)
    broker = _FakeBroker([], market_close_fails=True)   # full close rejected
    wd = Watchdog(_cfg(0.0), broker, state=state)
    assert wd._enforce_hard_exits(_pos_locked()) is True
    assert broker.reduced == [("FRHC", 1.000186)]   # available slice sold NOW
    assert broker.unwedged == [("FRHC", 50.0)]      # resting sells -> exit
    assert broker.rested == []                      # NOT the market-closed path
    assert state.get_exits("FRHC")                  # still tracked -> retries


def test_hard_stop_zero_available_exits_via_replaced_legs():
    state = _state()
    state.register_exits("FRHC", stop_pct=10.0, take_pct=25.0)
    broker = _FakeBroker([], market_close_fails=True,
                         stuck_replaces=[("new-1", 48.0, "old-1", 0.0), ("new-2", 61.0, "old-2", 0.0)])
    wd = Watchdog(_cfg(0.0), broker, state=state)
    assert wd._enforce_hard_exits(_pos_locked(avail=0.0)) is True
    assert broker.reduced == []                     # nothing sellable directly
    assert broker.unwedged == [("FRHC", 50.0)]      # legs made marketable
    assert state.get_exits("FRHC")                  # still tracked -> retries


def test_hard_stop_locked_and_unwedge_rejected_keeps_retrying():
    state = _state()
    state.register_exits("FRHC", stop_pct=10.0, take_pct=25.0)
    broker = _FakeBroker([], market_close_fails=True)   # replaces rejected: ()
    wd = Watchdog(_cfg(0.0), broker, state=state)
    assert wd._enforce_hard_exits(_pos_locked(avail=0.0)) is True  # CRITICAL path
    assert broker.reduced == [] and broker.rested == []
    assert state.get_exits("FRHC")                  # never dropped while open


def test_hard_stop_covered_by_resting_marketable_exit_is_partial_not_failed():
    # LLY (2026-07-08/09): every leg was made marketable on a PRIOR tick, so this
    # tick has nothing to replace/sell (avail=0, replaces=()) — but the shares
    # ARE reserved by resting marketable exits, i.e. protected. That must be a
    # quiet "partial" retry, not the "unprotected" CRITICAL page (which claimed,
    # wrongly, that the bracket had been canceled).
    state = _state()
    state.register_exits("LLY", stop_pct=10.0, take_pct=25.0)
    broker = _FakeBroker([], market_close_fails=True, working_exit=True)
    wd = Watchdog(_cfg(0.0), broker, state=state)
    outcome, oid = wd._close_hard(_pos_locked("LLY", avail=0.0), "trail")
    assert outcome == "partial"                     # protected, NOT "failed"
    assert oid is None
    assert broker.reduced == [] and broker.rested == []
    assert state.get_exits("LLY")                   # still tracked -> retries


def test_flatten_partial_when_shares_locked():
    state = _state()
    state.peak_equity = 1_000.0                     # floor 60% -> $600
    broker = _FakeBroker([], market_close_fails=True)
    wd = Watchdog(_cfg(60.0), broker, state=state)
    acct = AccountSnapshot(equity=500.0, last_equity=500.0, cash=0.0,
                           buying_power=0.0, positions=[_pos_locked()])
    assert wd._equity_floor_breached(acct) is True
    assert broker.reduced == [("FRHC", 1.000186)]
    assert broker.unwedged == [("FRHC", 50.0)]
    assert broker.rested == []                      # locked path, not market-closed


def test_close_hard_tries_plain_close_before_touching_orders():
    # Cancel-then-close is what manufactures pending-cancel wedges: when the
    # shares are FREE the close must be attempted FIRST, and no cancel sweep
    # may run when it succeeds.
    state = _state()
    state.register_exits("AAPL", stop_pct=10.0, take_pct=25.0)
    wd = Watchdog(_cfg(0.0), _FakeBroker([]), state=state)
    assert wd._enforce_hard_exits(_pos_locked("AAPL", avail=110.000186)) is True
    assert wd.broker.closed == ["AAPL"]
    assert wd.broker.canceled == []                 # nothing was in the way
    assert wd.broker.unwedged == []


def test_close_hard_skips_doomed_plain_close_when_shares_reserved():
    # BTDR/EQPT (2026-07-10): with every share held_for_orders the plain close
    # is provably refused 40310000 — the watchdog must go straight to the
    # replace-live-legs path without emitting the doomed close (ERROR noise).
    state = _state()
    state.register_exits("BTDR", stop_pct=10.0, take_pct=25.0)
    broker = _FakeBroker([], stuck_replaces=[("new-1", 14.09, "old-1", 0.0)])
    wd = Watchdog(_cfg(0.0), broker, state=state)
    outcome, oid = wd._close_hard(_pos_locked("BTDR", qty=124.0, avail=0.0), "trail")
    assert outcome == "partial"
    assert broker.closed == []                      # doomed close never fired
    assert broker.unwedged == [("BTDR", 50.0)]      # legs made marketable
    assert broker.reduced == []                     # nothing free to sell


def test_close_hard_reserved_with_free_slice_sells_slice_without_plain_close():
    state = _state()
    state.register_exits("LLY", stop_pct=10.0, take_pct=25.0)
    broker = _FakeBroker([], stuck_replaces=[("new-1", 48.0, "old-1", 0.0)])
    wd = Watchdog(_cfg(0.0), broker, state=state)
    outcome, _oid = wd._close_hard(_pos_locked("LLY", qty=9.34975, avail=0.34975), "stop")
    assert outcome == "partial"
    assert broker.closed == []                      # no doomed full close
    assert broker.reduced == [("LLY", 0.34975)]     # free slice sold NOW


# -- scale-out at the take-profit target (1B.8) ------------------------------ #
def _pos_qty(symbol="AAPL", qty=1.0, pl_pct=12.0):
    return Position(symbol=symbol, qty=qty, avg_entry_price=100.0,
                    current_price=100.0 * (1 + pl_pct / 100.0),
                    market_value=qty * 100.0 * (1 + pl_pct / 100.0),
                    unrealized_pl=qty * pl_pct, unrealized_pl_pct=pl_pct)


def test_scale_out_sells_part_and_trails_the_rest():
    state = _state()
    state.register_exits("AAPL", stop_pct=5.0, take_pct=12.0)
    wd = Watchdog(_cfg(0.0, scale_out_enabled=True, scale_out_pct=50.0),
                  _FakeBroker([]), state=state)
    fired = wd._enforce_hard_exits(_pos_qty("AAPL", qty=2.0, pl_pct=12.0))
    assert fired is True
    assert wd.broker.reduced == [("AAPL", 1.0)]     # sold 50% of 2.0
    assert wd.broker.closed == []                    # NOT a full close
    ex = state.get_exits("AAPL")
    assert ex["scaled"] == 1.0 and ex["take_pct"] == 0.0 and ex["stop_pct"] == 5.0


def test_scale_out_fires_once_then_trails():
    state = _state()
    # Post-scale state: take dropped to 0, marked scaled (what _scale_out records).
    state.register_exits("AAPL", stop_pct=5.0, take_pct=0.0, scaled=True)
    wd = Watchdog(_cfg(0.0, scale_out_enabled=True), _FakeBroker([]), state=state)
    # take_pct is 0 after scaling, so a further run-up is NOT re-taken here.
    assert wd._enforce_hard_exits(_pos_qty("AAPL", qty=1.0, pl_pct=20.0)) is False
    assert wd.broker.reduced == []


def test_scale_out_rounds_down_to_whole_shares_in_whole_shares_mode():
    # GA-2.3: 50% of 3 shares = 1.5 -> sell 1 whole share, trail the rest.
    state = _state()
    state.register_exits("AAPL", stop_pct=5.0, take_pct=12.0)
    wd = Watchdog(_cfg(0.0, scale_out_enabled=True, scale_out_pct=50.0,
                       whole_shares_only=True), _FakeBroker([]), state=state)
    assert wd._enforce_hard_exits(_pos_qty("AAPL", qty=3.0, pl_pct=12.0)) is True
    assert wd.broker.reduced == [("AAPL", 1.0)]


def test_scale_out_of_single_share_falls_back_to_full_take():
    # 50% of 1 share floors to 0 -> no partial possible -> full take-profit close.
    state = _state()
    state.register_exits("AAPL", stop_pct=5.0, take_pct=12.0)
    wd = Watchdog(_cfg(0.0, scale_out_enabled=True, scale_out_pct=50.0,
                       whole_shares_only=True), _FakeBroker([]), state=state)
    assert wd._enforce_hard_exits(_pos_qty("AAPL", qty=1.0, pl_pct=12.0)) is True
    assert wd.broker.reduced == []
    assert wd.broker.closed == ["AAPL"]


def test_scale_out_disabled_full_close_at_take():
    state = _state()
    state.register_exits("AAPL", stop_pct=5.0, take_pct=12.0)
    wd = Watchdog(_cfg(0.0, scale_out_enabled=False), _FakeBroker([]), state=state)
    assert wd._enforce_hard_exits(_pos_qty("AAPL", qty=2.0, pl_pct=12.0)) is True
    assert wd.broker.closed == ["AAPL"]              # original full-close behavior
    assert wd.broker.reduced == []


def test_stop_is_always_a_full_exit_even_with_scale_out():
    state = _state()
    state.register_exits("AAPL", stop_pct=5.0, take_pct=12.0)
    wd = Watchdog(_cfg(0.0, scale_out_enabled=True), _FakeBroker([]), state=state)
    # Down through the stop -> full close, never a scale-out.
    assert wd._enforce_hard_exits(_pos_qty("AAPL", qty=2.0, pl_pct=-6.0)) is True
    assert wd.broker.closed == ["AAPL"]
    assert wd.broker.reduced == []


# -- deterministic time-stop (1B.4) ------------------------------------------ #
def _pos_pl(symbol="AAPL", pl_pct=-1.0):
    return Position(symbol=symbol, qty=1.0, avg_entry_price=100.0,
                    current_price=100.0 + pl_pct, market_value=100.0 + pl_pct,
                    unrealized_pl=pl_pct, unrealized_pl_pct=pl_pct)


def _wd_ts(max_hold_days, state):
    wd = Watchdog(_cfg(0.0, max_hold_days=max_hold_days), _FakeBroker([]), state=state)
    return wd


def test_time_stop_recycles_old_flat_position():
    from datetime import datetime, timezone
    state = _state()
    # Entered 40 days ago; still flat (-1%) -> dead money, recycle after 30d.
    state.register_entry("AAPL", when=datetime(2026, 1, 1, tzinfo=timezone.utc))
    wd = _wd_ts(30.0, state)
    fired = wd._enforce_time_stop(_pos_pl("AAPL", pl_pct=-1.0))
    assert fired is True
    assert wd.broker.closed == ["AAPL"]


def test_time_stop_spares_a_winner():
    from datetime import datetime, timezone
    state = _state()
    state.register_entry("AAPL", when=datetime(2026, 1, 1, tzinfo=timezone.utc))
    wd = _wd_ts(30.0, state)
    # Old but UP +8% (>= 2% target) -> let the trailing stop run it, don't recycle.
    assert wd._enforce_time_stop(_pos_pl("AAPL", pl_pct=8.0)) is False
    assert wd.broker.closed == []


def test_time_stop_spares_young_position():
    state = _state()
    wd = _wd_ts(30.0, state)
    # No recorded entry: the watchdog stamps first-seen NOW, so age ~0 < 30d.
    assert wd._enforce_time_stop(_pos_pl("AAPL", pl_pct=-1.0)) is False
    assert wd.broker.closed == []
    assert state.entry_times.get("AAPL") is not None  # clock was started


def test_time_stop_off_when_zero():
    from datetime import datetime, timezone
    state = _state()
    state.register_entry("AAPL", when=datetime(2026, 1, 1, tzinfo=timezone.utc))
    wd = _wd_ts(0.0, state)  # disabled
    assert wd._enforce_time_stop(_pos_pl("AAPL", pl_pct=-50.0)) is False
    assert wd.broker.closed == []


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {fn.__name__}: {e!r}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)


# -- option positions: premium stop/take + expiry time-stop ------------------- #
from datetime import datetime as _dt, timedelta as _td, timezone as _tz

from investment_strategy.execution.options import occ_symbol


def _exp(days: int) -> str:
    return (_dt.now(_tz.utc) + _td(days=days)).strftime("%Y-%m-%d")


def _opt_leg(symbol, qty, basis, price):
    return Position(symbol=symbol, qty=qty, avg_entry_price=basis,
                    current_price=price, market_value=qty * price * 100.0,
                    unrealized_pl=(price - basis) * qty * 100.0,
                    unrealized_pl_pct=(price / basis - 1.0) * 100.0,
                    asset_class="us_option")


class _FakeLedger:
    def __init__(self):
        self.records = []

    def record(self, rec):
        self.records.append(rec)


def test_option_premium_stop_closes_group_and_ledgers_under_underlying():
    state, broker, led = _state(), _FakeBroker([]), _FakeLedger()
    wd = Watchdog(_cfg(0.0), broker, state=state, ledger=led)
    put = _opt_leg(occ_symbol("LLY", _exp(30), 700, "put"), qty=2, basis=3.0,
                   price=1.2)                                  # -60% of premium
    wd._check_option_positions([put])
    assert broker.option_groups_closed == [[put.symbol]]
    rec = led.records[-1]
    assert rec.symbol == "LLY"                    # pairs with the entry record
    assert rec.instrument == "option" and rec.exit_reason == "stop"
    assert state.hours_since_exit("LLY") is not None   # re-entry cooldown stamped


def test_option_spread_take_closes_both_legs_as_one_group():
    state, broker, led = _state(), _FakeBroker([]), _FakeLedger()
    wd = Watchdog(_cfg(0.0), broker, state=state, ledger=led)
    exp = _exp(30)
    long_put = _opt_leg(occ_symbol("AMD", exp, 160, "put"), qty=2, basis=3.0, price=6.5)
    short_put = _opt_leg(occ_symbol("AMD", exp, 150, "put"), qty=-2, basis=1.0, price=1.5)
    # net premium 600-200=400; net P&L 700-100=600 -> +150% >= +100% take
    wd._check_option_positions([long_put, short_put])
    assert len(broker.option_groups_closed) == 1               # ONE close order
    assert sorted(broker.option_groups_closed[0]) == sorted(
        [long_put.symbol, short_put.symbol])
    assert led.records[-1].exit_reason == "take"


def test_option_near_expiry_closes_regardless_of_pl():
    state, broker, led = _state(), _FakeBroker([]), _FakeLedger()
    wd = Watchdog(_cfg(0.0), broker, state=state, ledger=led)
    call = _opt_leg(occ_symbol("TSM", _exp(2), 250, "call"), qty=1, basis=2.0,
                    price=2.0)                                 # flat, 2 DTE <= 3
    wd._check_option_positions([call])
    assert broker.option_groups_closed == [[call.symbol]]
    assert led.records[-1].exit_reason == "option_expiry"


def test_option_within_bounds_left_alone():
    state, broker = _state(), _FakeBroker([])
    wd = Watchdog(_cfg(0.0), broker, state=state)
    call = _opt_leg(occ_symbol("TSM", _exp(20), 250, "call"), qty=1, basis=2.0,
                    price=1.6)                                 # -20%, 20 DTE
    wd._check_option_positions([call])
    assert broker.option_groups_closed == []


def test_option_close_failure_pages_and_keeps_retrying():
    state, broker = _state(), _FakeBroker([], option_close_fails=True)
    alerts = []
    wd = Watchdog(_cfg(0.0), broker, state=state,
                  alerter=SimpleNamespace(critical=lambda k, s, b: alerts.append(k)))
    put = _opt_leg(occ_symbol("LLY", _exp(30), 700, "put"), qty=1, basis=3.0, price=1.0)
    wd._check_option_positions([put])
    assert alerts == ["option-exit-fail:LLY"]
    assert state.hours_since_exit("LLY") is None    # nothing recorded as closed


def test_check_once_routes_options_away_from_equity_paths():
    # An option row must never hit the equity stop/trail/time paths (they'd
    # call latest_price on an OCC symbol and register bogus clocks).
    state, broker = _state(), _FakeBroker([])
    call = _opt_leg(occ_symbol("TSM", _exp(20), 250, "call"), qty=1, basis=2.0,
                    price=1.6)                                 # within bounds
    broker.account = AccountSnapshot(equity=1000.0, last_equity=1000.0, cash=0.0,
                                     buying_power=0.0, positions=[call])
    wd = Watchdog(_cfg(0.0, max_hold_days=30.0), broker, state=state)
    wd.check_once()
    assert broker.closed == [] and broker.option_groups_closed == []
    assert call.symbol not in broker.priced         # equity paths never saw it
    assert state.entry_age_days(call.symbol) is None  # no first-seen hold clock


def test_flatten_all_closes_mixed_book_options_first_as_groups():
    state = _state()
    state.peak_equity = 1_000.0                     # floor 60% -> $600
    broker = _FakeBroker([])
    exp = _exp(30)
    legs = [
        _opt_leg(occ_symbol("AMD", exp, 160, "put"), qty=1, basis=3.0, price=3.0),
        _opt_leg(occ_symbol("AMD", exp, 150, "put"), qty=-1, basis=1.0, price=1.0),
    ]
    acct = AccountSnapshot(equity=480.0, last_equity=480.0, cash=0.0,
                           buying_power=0.0,
                           positions=[_pos("AAPL"), *legs])
    broker.account = acct                            # confirming re-read: still dead
    wd = Watchdog(_cfg(60.0), broker, state=state)
    assert wd._equity_floor_breached(acct) is True
    assert broker.closed == ["AAPL"]                            # equity flattened
    assert len(broker.option_groups_closed) == 1                # spread as ONE order
    assert sorted(broker.option_groups_closed[0]) == sorted([l.symbol for l in legs])


# -- exit-via-replace bookkeeping (supersede + reconcile queue) --------------- #

def test_replaced_exit_supersedes_prior_ledger_record():
    # Falling price: tick 1 turns the resting leg into the exit (SELL ledgered
    # under new-1); tick 2 re-replaces the now-stale limit (new-2 supersedes
    # new-1). The prior SELL must be corrected (voided at 0 filled), not left
    # to double-count the exit — one effective SELL per actual exit.
    state, led = _state(), _FakeLedger()
    state.register_exits("NU", stop_pct=10.0, take_pct=25.0)
    broker = _FakeBroker([], stuck_replaces=[("new-1", 172.0, "leg-0", 0.0)])
    wd = Watchdog(_cfg(0.0), broker, state=state, ledger=led)
    pos = _pos_locked("NU", qty=172.0, avail=0.0)
    wd._close_hard(pos, "trail")
    assert [r.action for r in led.records] == ["sell"]
    assert state.exit_was_ledgered("NU", "new-1")
    broker.stuck_replaces = [("new-2", 172.0, "new-1", 0.0)]
    wd._close_hard(pos, "trail")
    assert [r.action for r in led.records] == ["sell", "correct", "sell"]
    corr = led.records[1]
    assert corr.order_id == "new-1" and corr.qty == 0.0


def test_replaced_exit_with_partial_fill_resizes_not_voids():
    # If the superseded order partially filled between ticks, those shares
    # really sold — the correction must resize to the filled qty, not void.
    state, led = _state(), _FakeLedger()
    state.register_exits("NU", stop_pct=10.0, take_pct=25.0)
    broker = _FakeBroker([], stuck_replaces=[("new-1", 172.0, "leg-0", 0.0)])
    wd = Watchdog(_cfg(0.0), broker, state=state, ledger=led)
    pos = _pos_locked("NU", qty=172.0, avail=0.0)
    wd._close_hard(pos, "trail")
    broker.stuck_replaces = [("new-2", 130.0, "new-1", 42.0)]  # 42 filled first
    wd._close_hard(pos, "trail")
    corr = led.records[1]
    assert corr.action == "correct" and corr.order_id == "new-1"
    assert corr.qty == 42.0


def test_watchdog_exit_orders_queued_for_reconcile():
    # A replace-time SELL is an intent, not a fill: the order id must land in
    # the persisted pending list so the orchestrator's reconcile checks its
    # real outcome (canceled/expired -> ledger correction, no phantom sell).
    state, led = _state(), _FakeLedger()
    state.register_exits("NU", stop_pct=10.0, take_pct=25.0)
    broker = _FakeBroker([], stuck_replaces=[("new-1", 172.0, "leg-0", 0.0)])
    wd = Watchdog(_cfg(0.0), broker, state=state, ledger=led)
    wd._close_hard(_pos_locked("NU", qty=172.0, avail=0.0), "trail")
    assert ("new-1", "NU") in state.get_pending_orders()
