"""Tests for the C.4 options-chain positioning signal (signals/options_chain.py).

Pure logic, no network: fake Alpaca clients are injected through the provider's
constructor. We assert the directional score (put-heavy OI + bid put skew ->
bearish), the thin-chain skip, the enable gate, and the per-cycle symbol cap.

Runnable two ways:
    .venv/bin/python tests/test_options_chain.py
    .venv/bin/pytest tests/
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.execution.options import occ_symbol
from investment_strategy.models import SignalKind
from investment_strategy.signals.options_chain import OptionsChainProvider


def _cfg(enabled=True, cap=25):
    return SimpleNamespace(
        options_chain_signal=enabled, options_chain_max_symbols=cap,
        alpaca_api_key="k", alpaca_secret_key="s", is_live=False,
    )


def _exp(days=30):
    return (date.today() + timedelta(days=days)).strftime("%Y-%m-%d")


class _FakeStock:
    def __init__(self, price=100.0):
        self.price = price

    def get_stock_latest_trade(self, req):
        sym = req.symbol_or_symbols
        return {sym: SimpleNamespace(price=self.price)}


class _FakeData:
    """get_option_chain -> {occ: snapshot(implied_volatility=...)}."""
    def __init__(self, chain):
        self.chain = chain

    def get_option_chain(self, req):
        return self.chain


class _FakeTrading:
    def __init__(self, contracts):
        self.contracts = contracts

    def get_option_contracts(self, req):
        return SimpleNamespace(option_contracts=self.contracts)


def _snap(iv):
    return SimpleNamespace(implied_volatility=iv)


def _contract(ctype, oi):
    return SimpleNamespace(type=SimpleNamespace(value=ctype), open_interest=oi)


def _bearish_provider(symbol="LLY"):
    """Put-heavy OI (2x) + puts bid 8 IV pts over calls at $100 spot."""
    exp = _exp(30)
    chain = {}
    # ATM contracts (IV 40%)
    chain[occ_symbol(symbol, exp, 100, "call")] = _snap(0.40)
    chain[occ_symbol(symbol, exp, 100, "put")] = _snap(0.40)
    # OTM puts (strike 88-92, IV 48%) and OTM calls (strike 108-112, IV 40%)
    for k in (88, 90, 92):
        chain[occ_symbol(symbol, exp, k, "put")] = _snap(0.48)
    for k in (108, 110, 112):
        chain[occ_symbol(symbol, exp, k, "call")] = _snap(0.40)
    # Padding so the chain isn't "thin" (>= 10 quoted contracts)
    for k in (95, 105):
        chain[occ_symbol(symbol, exp, k, "call")] = _snap(0.41)
        chain[occ_symbol(symbol, exp, k, "put")] = _snap(0.43)
    contracts = [_contract("call", 500), _contract("put", 1000)]
    return OptionsChainProvider(
        _cfg(), data=_FakeData(chain), trading=_FakeTrading(contracts),
        stock=_FakeStock(100.0),
    )


def test_bearish_chain_scores_negative_and_reads_bearish():
    sigs = _bearish_provider().fetch(["LLY"])
    assert len(sigs) == 1
    sig = sigs[0]
    assert sig.kind is SignalKind.OPTIONS_CHAIN
    assert sig.symbol == "LLY"
    assert sig.score < -0.15, sig.score            # put OI 2x + put skew
    assert "bearish" in sig.summary
    assert sig.data["put_oi"] == 1000.0 and sig.data["call_oi"] == 500.0
    assert sig.data["skew_pts"] and sig.data["skew_pts"] > 5.0


def test_bullish_chain_scores_positive():
    p = _bearish_provider("NVDA")
    # Flip the OI lean: calls 3x puts, and flatten the skew.
    p._trading = _FakeTrading([_contract("call", 1500), _contract("put", 500)])
    for occ_sym in list(p._data.chain):
        p._data.chain[occ_sym] = _snap(0.40)       # uniform IV -> skew ~0
    sigs = p.fetch(["NVDA"])
    assert len(sigs) == 1
    assert sigs[0].score > 0.15
    assert "bullish" in sigs[0].summary


def test_thin_chain_returns_no_signal():
    exp = _exp(30)
    chain = {occ_symbol("PLUG", exp, 3, "call"): _snap(0.9)}   # 1 contract only
    p = OptionsChainProvider(_cfg(), data=_FakeData(chain),
                             trading=_FakeTrading([]), stock=_FakeStock(3.0))
    assert p.fetch(["PLUG"]) == []


def test_disabled_without_flag():
    p = OptionsChainProvider(_cfg(enabled=False))
    assert p.enabled is False
    assert p.safe_fetch(["AAPL"]) == []


def test_symbol_cap_respected():
    p = _bearish_provider()
    seen = []
    p._one = lambda s: seen.append(s)              # count, return None
    p.cfg = _cfg(cap=3)
    p.fetch([f"SYM{i}" for i in range(10)])
    assert len(seen) == 3


def test_provider_never_raises_per_symbol():
    p = _bearish_provider()

    def _boom(_s):
        raise RuntimeError("api down")

    p._one = _boom
    assert p.fetch(["AAPL", "MSFT"]) == []         # both swallowed


def _run_all():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS {fn.__name__}")
        except Exception as e:
            failed += 1
            print(f"  FAIL {fn.__name__}: {e!r}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
