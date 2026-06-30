"""Tests for the account-status / P&L tracker and the watchlist resolution.

Pure logic, no network: a fake broker returns a canned account snapshot and
portfolio basis so we assert the total-return / realized identity, the fail-open
path when portfolio history is unavailable, once-per-day equity snapshots, and the
unset-vs-empty watchlist semantics (the live-cutover "start flat" mode).

Runnable two ways:
    .venv/bin/python tests/test_status.py     # standalone, no pytest
    .venv/bin/pytest tests/                    # if pytest is installed
"""
from __future__ import annotations

import os
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.__main__ import resolve_watchlist
from investment_strategy.models import AccountSnapshot, Position
from investment_strategy.status import AccountStatus, EquityHistory, compute_status


class _FakeBroker:
    def __init__(self, account: AccountSnapshot, basis):
        self._account = account
        self._basis = basis

    def get_account(self) -> AccountSnapshot:
        return self._account

    def portfolio_basis(self):
        return self._basis


def _pos(symbol="AAPL", unreal=0.0) -> Position:
    return Position(
        symbol=symbol, qty=1.0, avg_entry_price=100.0, current_price=100.0,
        market_value=100.0, unrealized_pl=unreal, unrealized_pl_pct=0.0,
    )


def _account(equity=100_000.0, last_equity=99_000.0, cash=5_000.0, positions=None):
    return AccountSnapshot(
        equity=equity, last_equity=last_equity, cash=cash,
        buying_power=equity * 2, positions=positions or [],
    )


# --------------------------------------------------------------------------- #
# compute_status — the total-return / realized identity
# --------------------------------------------------------------------------- #
def test_total_return_backs_out_initial_funding():
    # Funded $100k, now $100,044, no deposits -> +$44 total return.
    acct = _account(equity=100_044.0, positions=[_pos(unreal=38.0)])
    st = compute_status(_FakeBroker(acct, (100_000.0, 0.0)))
    assert st.capital_in == 100_000.0
    assert st.total_return == 44.0
    assert st.is_up is True


def test_realized_is_total_minus_unrealized():
    acct = _account(equity=100_044.0, positions=[_pos(unreal=38.0)])
    st = compute_status(_FakeBroker(acct, (100_000.0, 0.0)))
    assert st.realized_pl == round(44.0 - 38.0, 2)   # = 6.0


def test_net_deposits_are_backed_out():
    # Funded $100k, later deposited $2k, now $103k -> +$1k true return (not +$3k).
    acct = _account(equity=103_000.0, positions=[_pos(unreal=0.0)])
    st = compute_status(_FakeBroker(acct, (100_000.0, 2_000.0)))
    assert st.capital_in == 102_000.0
    assert st.total_return == 1_000.0


def test_down_account_reports_negative():
    acct = _account(equity=98_500.0, positions=[_pos(unreal=-200.0)])
    st = compute_status(_FakeBroker(acct, (100_000.0, 0.0)))
    assert st.total_return == -1_500.0
    assert st.is_up is False


def test_day_pl_from_last_equity():
    acct = _account(equity=100_500.0, last_equity=100_000.0)
    st = compute_status(_FakeBroker(acct, (100_000.0, 0.0)))
    assert st.day_pl == 500.0
    assert round(st.day_pl_pct, 2) == 0.5


def test_status_fails_open_without_portfolio_basis():
    # Portfolio history unavailable -> total/realized are None, but day P&L and
    # unrealized still compute from the plain account read.
    acct = _account(equity=100_044.0, positions=[_pos(unreal=38.0)])
    st = compute_status(_FakeBroker(acct, None))
    assert st.total_return is None and st.realized_pl is None
    assert st.is_up is None
    assert st.unrealized_pl == 38.0 and st.day_pl == 1_044.0


# --------------------------------------------------------------------------- #
# EquityHistory — one row per calendar day
# --------------------------------------------------------------------------- #
def _tmp_history() -> EquityHistory:
    p = os.path.join(tempfile.gettempdir(), f"_eqhist_{uuid.uuid4().hex}.jsonl")
    return EquityHistory(path=p)


def test_snapshot_dedupes_same_day():
    h = _tmp_history()
    s1 = AccountStatus(equity=100.0, cash=10.0, buying_power=200.0, n_positions=0,
                       unrealized_pl=0.0, day_pl=0.0, day_pl_pct=0.0)
    s2 = AccountStatus(equity=150.0, cash=10.0, buying_power=200.0, n_positions=0,
                       unrealized_pl=0.0, day_pl=0.0, day_pl_pct=0.0)
    h.snapshot(s1)
    h.snapshot(s2)  # same calendar day -> overwrites, not appended
    rows = h.all()
    assert len(rows) == 1 and rows[0]["equity"] == 150.0


# --------------------------------------------------------------------------- #
# Watchlist resolution — unset vs explicitly empty (live "start flat" mode)
# --------------------------------------------------------------------------- #
def test_watchlist_unset_is_none():
    assert resolve_watchlist(None) is None          # -> default list downstream


def test_watchlist_empty_string_is_flat():
    assert resolve_watchlist("") == []              # explicit: screener-only


def test_watchlist_none_keyword_is_flat():
    assert resolve_watchlist("NONE") == []
    assert resolve_watchlist(" none ") == []


def test_watchlist_parses_and_uppercases():
    assert resolve_watchlist("aapl, msft ,nvda") == ["AAPL", "MSFT", "NVDA"]


def test_watchlist_blank_commas_are_flat():
    assert resolve_watchlist(" , , ") == []


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
