"""Tests for the decision-prompt renderer's full-book ROTATION guidance.

Postmortem 2026-07-14: with the book at 15/15, MU (conviction 0.63) was
rejected at the slot cap while CVX (0.46) sat held — the model never tried
pairing a SELL with the BUY because nothing told it rotation was possible.
The prompt now says so exactly when the cap would actually reject.

Runnable two ways:
    .venv/bin/python -m pytest tests/test_decision_prompt.py
    .venv/bin/pytest tests/
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.decision.engine import DecisionEngine
from investment_strategy.models import (
    AccountSnapshot,
    Position,
    Signal,
    SignalBundle,
    SignalKind,
)


def _engine(max_open_positions=2, options_enabled=False) -> DecisionEngine:
    eng = DecisionEngine.__new__(DecisionEngine)  # skip API client construction
    eng.cfg = SimpleNamespace(risk=SimpleNamespace(
        options_enabled=options_enabled,
        max_open_positions=max_open_positions,
    ))
    return eng


def _pos(symbol, asset_class="us_equity") -> Position:
    return Position(
        symbol=symbol, qty=1.0, avg_entry_price=100.0, current_price=100.0,
        market_value=100.0, unrealized_pl=0.0, unrealized_pl_pct=0.0,
        asset_class=asset_class,
    )


def _acct(positions) -> AccountSnapshot:
    return AccountSnapshot(
        equity=100_000, last_equity=100_000, cash=1_000, buying_power=1_000,
        positions=positions,
    )


def _bundle(symbol) -> SignalBundle:
    sig = Signal(kind=SignalKind.CONGRESS, symbol=symbol, summary="x", score=0.5)
    return SignalBundle(symbol=symbol, signals=[sig])


def test_rotation_block_renders_when_book_full():
    eng = _engine(max_open_positions=2)
    acct = _acct([_pos("CVX"), _pos("AAPL")])
    text = eng._render([_bundle("MU")], acct, "", [])
    assert "Book FULL (2/2 equity slots)" in text
    assert "ROTATION" in text
    # Trusted guidance must sit OUTSIDE the untrusted region.
    assert text.index("Book FULL") < text.index("<market_data>")


def test_rotation_block_absent_when_book_has_room():
    eng = _engine(max_open_positions=3)
    acct = _acct([_pos("CVX"), _pos("AAPL")])
    text = eng._render([_bundle("MU")], acct, "", [])
    assert "Book FULL" not in text
    assert "ROTATION" not in text


def test_rotation_block_ignores_option_rows():
    # Options have their own concurrency cap and don't consume equity slots —
    # the prompt must count the way the risk gate counts (equity rows only).
    eng = _engine(max_open_positions=2)
    acct = _acct([
        _pos("CVX"),
        _pos("AAPL260821C00200000", asset_class="us_option"),
    ])
    text = eng._render([_bundle("MU")], acct, "", [])
    assert "Book FULL" not in text


def test_rotation_block_survives_missing_risk_config():
    # Engines built without a risk config (some tests, tooling) must not crash.
    eng = DecisionEngine.__new__(DecisionEngine)
    eng.cfg = SimpleNamespace()
    acct = _acct([_pos("CVX")])
    text = eng._render([_bundle("MU")], acct, "", [])
    assert "Book FULL" not in text
