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
    def __init__(self, positions):
        self._positions = positions
        self.closed: list[str] = []
        self.canceled: list[str] = []

    def cancel_open_orders_for(self, symbol):
        self.canceled.append(symbol)

    def close_position(self, symbol):
        self.closed.append(symbol)
        return f"oid-{symbol}"          # truthy order id => confirmed close


def _cfg(pct, max_hold_days=0.0, time_stop_min_gain_pct=2.0):
    return SimpleNamespace(
        risk=SimpleNamespace(
            equity_floor_pct=pct, max_daily_loss_pct=3.0,
            max_hold_days=max_hold_days, time_stop_min_gain_pct=time_stop_min_gain_pct,
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
