"""Tests for durable risk state — the memory that must survive a restart."""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.state import PortfolioState


def _tmp() -> str:
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.unlink(path)  # start clean; PortfolioState creates it on save
    return path


def test_peak_ratchets_up_only():
    s = PortfolioState(path=_tmp())
    s.update_equity(100_000)
    s.update_equity(120_000)
    s.update_equity(90_000)  # a drop must NOT lower the peak
    assert s.peak_equity == 120_000


def test_drawdown_pct():
    s = PortfolioState(path=_tmp())
    s.update_equity(100_000)
    assert s.drawdown_pct(85_000) == 15.0
    assert s.drawdown_pct(110_000) == 0.0  # at/above peak -> no drawdown


def test_peak_persists_across_restart():
    path = _tmp()
    s1 = PortfolioState(path=path)
    s1.update_equity(150_000)
    s2 = PortfolioState(path=path)  # fresh load == a process restart
    assert s2.peak_equity == 150_000


def test_halt_latch_persists_and_clears():
    path = _tmp()
    s1 = PortfolioState(path=path)
    s1.latch_halt("equity floor breached")
    s2 = PortfolioState(path=path)
    assert s2.halted is True
    assert "equity floor" in s2.halt_reason
    s2.clear_halt()
    s3 = PortfolioState(path=path)
    assert s3.halted is False


def test_high_water_round_trip_and_forget():
    path = _tmp()
    s1 = PortfolioState(path=path)
    s1.set_high_water("AAPL", 12.5)
    s2 = PortfolioState(path=path)
    assert s2.get_high_water("AAPL") == 12.5
    s2.forget_symbol("AAPL")
    s3 = PortfolioState(path=path)
    assert s3.get_high_water("AAPL") == 0.0


def test_exits_round_trip_and_cleared_by_forget():
    path = _tmp()
    s1 = PortfolioState(path=path)
    s1.register_exits("NVDA", stop_pct=5.0, take_pct=12.0)
    s2 = PortfolioState(path=path)  # survives restart
    ex = s2.get_exits("NVDA")
    assert ex == {"stop_pct": 5.0, "take_pct": 12.0, "scaled": 0.0}
    s2.forget_symbol("NVDA")  # closing a position must clear its hard exits
    s3 = PortfolioState(path=path)
    assert s3.get_exits("NVDA") is None


def test_entry_time_round_trip_idempotent_and_forget():
    from datetime import datetime, timedelta, timezone

    path = _tmp()
    s1 = PortfolioState(path=path)
    t0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
    s1.register_entry("AAPL", when=t0)
    s1.register_entry("AAPL", when=t0 + timedelta(days=10))  # idempotent: no reset
    s2 = PortfolioState(path=path)  # survives restart
    # ~29 days as of 2026-06-30, measured from the ORIGINAL entry, not the re-stamp.
    age = s2.entry_age_days("AAPL", now=datetime(2026, 6, 30, tzinfo=timezone.utc))
    assert abs(age - 29.0) < 0.01
    s2.forget_symbol("AAPL")  # closing a position clears its hold clock
    s3 = PortfolioState(path=path)
    assert s3.entry_age_days("AAPL") is None


def test_entry_age_unknown_symbol_is_none():
    s = PortfolioState(path=_tmp())
    assert s.entry_age_days("NOPE") is None


def test_pending_orders_round_trip_across_restart():
    path = _tmp()
    s1 = PortfolioState(path=path)
    s1.set_pending_orders([("oid-1", "AAPL"), ("oid-2", "MSFT")])
    s2 = PortfolioState(path=path)  # restart mid-flight
    assert s2.get_pending_orders() == [("oid-1", "AAPL"), ("oid-2", "MSFT")]
    s2.set_pending_orders([])  # reconciled -> cleared
    s3 = PortfolioState(path=path)
    assert s3.get_pending_orders() == []


def test_corrupt_state_does_not_crash():
    path = _tmp()
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{not valid json")
    s = PortfolioState(path=path)  # must degrade to clean state, not raise
    assert s.peak_equity == 0.0
    assert s.halted is False


def test_buy_and_exit_clocks_round_trip_across_restart():
    from datetime import datetime, timedelta, timezone

    path = _tmp()
    s1 = PortfolioState(path=path)
    two_h_ago = datetime.now(timezone.utc) - timedelta(hours=2)
    s1.register_buy("LLY", when=two_h_ago)
    s1.register_exit("CRWD", when=two_h_ago)
    s2 = PortfolioState(path=path)  # fresh load == a process restart
    assert 1.9 < s2.hours_since_buy("LLY") < 2.1
    assert 1.9 < s2.hours_since_exit("CRWD") < 2.1
    assert s2.hours_since_buy("TSM") is None      # never bought
    assert s2.hours_since_exit("TSM") is None     # never exited


def test_buy_clock_updates_on_every_buy_unlike_entry_time():
    from datetime import datetime, timedelta, timezone

    s = PortfolioState(path=_tmp())
    old = datetime.now(timezone.utc) - timedelta(hours=10)
    s.register_buy("LLY", when=old)
    s.register_buy("LLY")  # a top-up RESETS the clock (register_entry wouldn't)
    assert s.hours_since_buy("LLY") < 0.1


def test_clock_stamps_survive_forget_symbol():
    # forget_symbol drops trailing/exit/entry state when a position closes, but
    # the re-entry cooldown must still see WHEN it closed.
    s = PortfolioState(path=_tmp())
    s.register_exit("MXL")
    s.forget_symbol("MXL")
    assert s.hours_since_exit("MXL") is not None


def test_old_clock_stamps_are_pruned():
    from datetime import datetime, timedelta, timezone

    s = PortfolioState(path=_tmp())
    stale = datetime.now(timezone.utc) - timedelta(days=30)
    s.register_exit("OLD", when=stale)
    s.register_exit("NEW")  # any later write prunes week-old stamps
    assert s.hours_since_exit("OLD") is None
    assert s.hours_since_exit("NEW") is not None


def test_daily_accumulator_persists_across_restart():
    from datetime import datetime, timezone

    path = _tmp()
    when = datetime(2026, 7, 6, 16, 0, 0, tzinfo=timezone.utc)  # Mon 12pm ET
    s1 = PortfolioState(path=path)
    s1.register_daily_deploy("LLY", 1000.0, when=when)
    s1.register_daily_deploy("LLY", 500.0, when=when)
    s2 = PortfolioState(path=path)  # restart
    assert abs(s2.daily_symbol_spend("LLY", when=when) - 1500.0) < 0.01
    assert s2.daily_symbol_buys("LLY", when=when) == 2


def test_daily_accumulator_rolls_at_et_midnight():
    from datetime import datetime, timezone

    path = _tmp()
    # Jul 6 ET: 14:00 UTC = 10:00 ET
    day1 = datetime(2026, 7, 6, 14, 0, 0, tzinfo=timezone.utc)
    # Jul 7 ET: 04:00 UTC = 00:00 ET (next day for exchange)
    day2 = datetime(2026, 7, 7, 4, 0, 1, tzinfo=timezone.utc)
    s = PortfolioState(path=path)
    s.register_daily_deploy("LLY", 2000.0, when=day1)
    # After ET midnight: fresh day
    assert s.daily_symbol_spend("LLY", when=day2) == 0.0
    assert s.daily_symbol_buys("LLY", when=day2) == 0


def test_daily_accumulator_utc_vs_et_date():
    """UTC midnight ≠ ET midnight: accumulators must use ET so an evening restart
    (e.g. 23:00 ET, 03:00 UTC next day) doesn't hand back a fresh budget."""
    from datetime import datetime, timezone

    path = _tmp()
    # 2026-07-06 23:30 ET = 2026-07-07 03:30 UTC
    evening_et = datetime(2026, 7, 7, 3, 30, 0, tzinfo=timezone.utc)
    s = PortfolioState(path=path)
    s.register_daily_deploy("LLY", 3000.0, when=evening_et)
    # Still the same ET day (Jul 6 23:30 ET), so spend stays at $3k
    assert abs(s.daily_symbol_spend("LLY", when=evening_et) - 3000.0) < 0.01


def test_conviction_clock_round_trip():
    from datetime import datetime, timezone

    path = _tmp()
    s1 = PortfolioState(path=path)
    s1.register_buy("LLY", conviction=0.82)
    s2 = PortfolioState(path=path)  # restart
    assert abs(s2.last_buy_conviction("LLY") - 0.82) < 0.001


def test_conviction_none_when_not_set():
    s = PortfolioState(path=_tmp())
    assert s.last_buy_conviction("UNKNOWN") is None


def test_conviction_updates_on_subsequent_buy():
    s = PortfolioState(path=_tmp())
    s.register_buy("LLY", conviction=0.70)
    s.register_buy("LLY", conviction=0.85)
    assert abs(s.last_buy_conviction("LLY") - 0.85) < 0.001


def test_postmortem_done_day_round_trip():
    path = _tmp()
    s1 = PortfolioState(path=path)
    s1.set_postmortem_done("2026-07-06")
    s2 = PortfolioState(path=path)
    assert s2.get_postmortem_done_day() == "2026-07-06"


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


def test_pending_order_add_drain_merge_and_restart():
    path = _tmp()
    s = PortfolioState(path=path)
    # add is idempotent per order id and appends from any thread
    s.add_pending_order("wd-1", "NU")
    s.add_pending_order("wd-1", "NU")
    s.set_pending_orders([("o-1", "LLY")])
    s.add_pending_order("wd-2", "NU")
    # merge unions without duplicating what's already there
    s.merge_pending_orders([("o-1", "LLY"), ("o-2", "CVX")])
    assert s.get_pending_orders() == [("o-1", "LLY"), ("wd-2", "NU"), ("o-2", "CVX")]
    # survives a restart
    s2 = PortfolioState(path=path)
    assert s2.get_pending_orders() == [("o-1", "LLY"), ("wd-2", "NU"), ("o-2", "CVX")]
    # drain atomically takes-and-clears
    assert s2.drain_pending_orders() == [("o-1", "LLY"), ("wd-2", "NU"), ("o-2", "CVX")]
    assert s2.get_pending_orders() == []
    assert PortfolioState(path=path).get_pending_orders() == []


def test_ledgered_exit_oids_tracked_and_forgotten():
    path = _tmp()
    s = PortfolioState(path=path)
    s.note_exit_ledgered("NU", "new-1")
    assert s.exit_was_ledgered("NU", "new-1")
    assert not s.exit_was_ledgered("NU", "other")
    assert not s.exit_was_ledgered("LLY", "new-1")
    # survives a restart
    assert PortfolioState(path=path).exit_was_ledgered("NU", "new-1")
    # position gone -> tracking dropped with the rest of the symbol state
    s.forget_symbol("NU")
    assert not s.exit_was_ledgered("NU", "new-1")
