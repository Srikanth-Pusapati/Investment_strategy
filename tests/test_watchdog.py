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
    def __init__(self, positions, market_close_fails=False):
        self._positions = positions
        self.market_close_fails = market_close_fails   # simulate closed/halted market
        self.closed: list[str] = []
        self.canceled: list[str] = []
        self.reduced: list[tuple[str, float]] = []
        self.rested: list[tuple[str, float, float]] = []

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
        return 50.0

    def close_position_marketable_limit(self, symbol, qty, ref_price):
        self.rested.append((symbol, qty, ref_price))
        return f"rest-{symbol}"


def _cfg(pct, max_hold_days=0.0, time_stop_min_gain_pct=2.0,
         scale_out_enabled=False, scale_out_pct=50.0):
    return SimpleNamespace(
        risk=SimpleNamespace(
            equity_floor_pct=pct, max_daily_loss_pct=3.0,
            max_hold_days=max_hold_days, time_stop_min_gain_pct=time_stop_min_gain_pct,
            scale_out_enabled=scale_out_enabled, scale_out_pct=scale_out_pct,
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
