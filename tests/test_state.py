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


def test_corrupt_state_does_not_crash():
    path = _tmp()
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{not valid json")
    s = PortfolioState(path=path)  # must degrade to clean state, not raise
    assert s.peak_equity == 0.0
    assert s.halted is False


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
