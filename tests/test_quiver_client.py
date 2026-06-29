"""Tests for the shared, per-cycle-cached Quiver client and the congress refactor.

Pure logic, no network: a fake `requests.get` (or a patched `_do_request`) drives
the client so we can assert the per-cycle cache dedupes pulls (the double-pull
fix), new_cycle() refreshes, disabled (no key) short-circuits, and HTTP status
mapping is correct. Plus the congress signal provider now reads the shared feed.

Runnable two ways:
    .venv/bin/python tests/test_quiver_client.py     # standalone, no pytest
    .venv/bin/pytest tests/                            # if pytest is installed
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.signals import quiver_client as qc
from investment_strategy.signals.congress import CongressProvider
from investment_strategy.signals.quiver_client import QuiverClient, _Retryable


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


# --------------------------------------------------------------------------- #
# Per-cycle cache (the double-pull fix)
# --------------------------------------------------------------------------- #
def test_disabled_without_key_never_fetches():
    client = QuiverClient(api_key="")
    calls = []
    client._request = lambda path: calls.append(path) or []  # type: ignore
    assert client.live("congresstrading") == []
    assert calls == []                       # no key -> no HTTP at all


def test_live_cached_within_cycle():
    client = QuiverClient(api_key="k")
    calls = []
    client._request = lambda path: (calls.append(path), [{"Ticker": "AAPL"}])[1]  # type: ignore
    a = client.live("congresstrading")
    b = client.live("congresstrading")       # second consumer, same cycle
    assert a == b == [{"Ticker": "AAPL"}]
    assert calls == ["live/congresstrading"]  # pulled ONCE, not twice


def test_new_cycle_refetches():
    client = QuiverClient(api_key="k")
    calls = []
    client._request = lambda path: calls.append(path) or []  # type: ignore
    client.live("congresstrading")
    client.new_cycle()
    client.live("congresstrading")
    assert calls == ["live/congresstrading", "live/congresstrading"]


def test_failed_pull_cached_as_empty():
    client = QuiverClient(api_key="k")
    calls = []

    def boom(path):
        calls.append(path)
        raise _Retryable("429")
    client._request = boom  # type: ignore
    assert client.live("x") == []
    assert client.live("x") == []            # not retried again this cycle
    assert calls == ["x" and "live/x"]


def test_distinct_datasets_each_fetch_once():
    client = QuiverClient(api_key="k")
    calls = []
    client._request = lambda path: calls.append(path) or []  # type: ignore
    client.live("congresstrading")
    client.live("offexchange")
    client.live("congresstrading")
    assert calls == ["live/congresstrading", "live/offexchange"]


# --------------------------------------------------------------------------- #
# HTTP status mapping (no backoff — _do_request directly)
# --------------------------------------------------------------------------- #
def test_429_maps_to_retryable(monkeypatch=None):
    client = QuiverClient(api_key="k")
    qc.requests.get = lambda *a, **k: _Resp(429, None)  # type: ignore
    try:
        client._do_request("live/x")
        assert False, "expected _Retryable"
    except _Retryable:
        pass


def test_500_maps_to_empty():
    client = QuiverClient(api_key="k")
    qc.requests.get = lambda *a, **k: _Resp(500, None)  # type: ignore
    assert client._do_request("live/x") == []


def test_200_returns_list_only():
    client = QuiverClient(api_key="k")
    qc.requests.get = lambda *a, **k: _Resp(200, [{"Ticker": "MSFT"}])  # type: ignore
    assert client._do_request("live/x") == [{"Ticker": "MSFT"}]
    qc.requests.get = lambda *a, **k: _Resp(200, {"not": "a list"})  # type: ignore
    assert client._do_request("live/x") == []   # non-list body -> []


# --------------------------------------------------------------------------- #
# Congress signal provider reads the SHARED feed
# --------------------------------------------------------------------------- #
def _feed():
    return [
        {"Ticker": "AAPL", "Transaction": "Purchase", "TransactionDate": "2099-01-02"},
        {"Ticker": "AAPL", "Transaction": "Purchase", "TransactionDate": "2099-01-03"},
        {"Ticker": "AAPL", "Transaction": "Sale", "TransactionDate": "2099-01-04"},
        {"Ticker": "MSFT", "Transaction": "Sale", "TransactionDate": "2099-01-05"},
        {"Ticker": "AAPL", "Transaction": "Purchase", "TransactionDate": "1990-01-01"},  # stale
    ]


def test_congress_provider_scores_from_shared_feed():
    client = QuiverClient(api_key="k")
    client.live = lambda dataset: _feed()  # type: ignore
    prov = CongressProvider(SimpleNamespace(quiver_api_key="k"), client)
    sigs = {s.symbol: s for s in prov.fetch(["AAPL", "MSFT", "GOOG"])}
    # AAPL: 2 buys, 1 sell within window -> (2-1)/3
    assert abs(sigs["AAPL"].score - round(1 / 3, 3)) < 1e-9
    assert sigs["AAPL"].data == {"buys": 2, "sells": 1}
    # MSFT: 1 sell -> -1.0 ; GOOG: nothing -> no signal
    assert sigs["MSFT"].score == -1.0
    assert "GOOG" not in sigs


def test_congress_provider_disabled_without_key():
    prov = CongressProvider(SimpleNamespace(quiver_api_key=""), QuiverClient(""))
    assert prov.enabled is False


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
