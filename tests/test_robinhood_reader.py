"""Tests for RobinhoodReader account resolution + holdings parsing.

Pure logic, no network: we stub call_json with canned MCP payloads and assert the
reader (1) targets the agentic_allowed account and NEVER the main portfolio,
(2) honours an explicit ROBINHOOD_ACCOUNT_NUMBER override, and (3) turns RH's
price-less position rows into a market value + unrealized P&L via a batched quote,
while honouring a market_value a tool already provides.

Runnable two ways:
    .venv/bin/python tests/test_robinhood_reader.py
    .venv/bin/pytest tests/
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.portfolio.robinhood import RobinhoodReader, _UNSET


def _reader(account_number="", positions_tool="get_equity_positions", responses=None):
    r = RobinhoodReader.__new__(RobinhoodReader)
    r.cfg = SimpleNamespace(
        robinhood_account_number=account_number,
        robinhood_positions_tool=positions_tool,
    )
    r._account_number = _UNSET
    r._responses = responses or {}
    r.calls = []

    def fake_call_json(tool, arguments=None):
        r.calls.append((tool, arguments))
        val = r._responses.get(tool)
        return val(arguments) if callable(val) else val

    r.call_json = fake_call_json
    return r


_ACCOUNTS = {"accounts": [
    {"account_number": "111", "agentic_allowed": False, "is_default": True},   # main
    {"account_number": "222", "agentic_allowed": False, "management_type": "managed"},
    {"account_number": "999", "agentic_allowed": True},                        # agentic
]}


def test_resolve_picks_agentic_not_main():
    r = _reader(responses={"get_accounts": _ACCOUNTS})
    assert r._resolve_account_number() == "999"       # NOT the default "111"


def test_resolve_caches_after_first_lookup():
    r = _reader(responses={"get_accounts": _ACCOUNTS})
    assert r._resolve_account_number() == "999"
    r._responses["get_accounts"] = {"accounts": []}   # would now fail if re-fetched
    assert r._resolve_account_number() == "999"        # served from cache
    assert sum(1 for c in r.calls if c[0] == "get_accounts") == 1


def test_resolve_none_when_no_agentic_account():
    # Must NOT fall back to the main/default account.
    r = _reader(responses={"get_accounts": {"accounts": [
        {"account_number": "111", "agentic_allowed": False, "is_default": True},
    ]}})
    assert r._resolve_account_number() is None


def test_explicit_override_wins_without_network():
    r = _reader(account_number="777", responses={})
    assert r._resolve_account_number() == "777"
    assert r.calls == []                               # never called get_accounts


def test_holdings_enriches_priceless_rows_via_quotes():
    positions = {"positions": [
        {"symbol": "SCHD", "quantity": "2", "average_buy_price": "10.00"},
        {"symbol": "AAPL", "quantity": "1", "average_buy_price": "200.00"},
    ]}
    quotes = {"results": [
        {"quote": {"symbol": "SCHD", "last_trade_price": "11.00"}},
        {"quote": {"symbol": "AAPL", "last_trade_price": "250.00"}},
    ]}
    r = _reader(responses={
        "get_accounts": _ACCOUNTS,
        "get_equity_positions": positions,
        "get_equity_quotes": quotes,
    })
    # bypass the token/env gate (enabled is a read-only property) for this unit test
    with patch.object(RobinhoodReader, "enabled", property(lambda self: True)):
        hs = {h.symbol: h for h in r.holdings()}
    assert hs["SCHD"].market_value == 22.0            # 2 * 11.00
    assert round(hs["SCHD"].unrealized_pl_pct, 1) == 10.0   # 11 vs 10 avg
    assert hs["AAPL"].market_value == 250.0
    assert round(hs["AAPL"].unrealized_pl_pct, 1) == 25.0
    # positions tool was called WITH the resolved account number
    assert ("get_equity_positions", {"account_number": "999"}) in r.calls


def test_parse_honours_existing_market_value_and_skips_quote():
    r = _reader(responses={"get_equity_quotes": {"results": []}})
    # a tool that already carries market_value + P&L needs no quote enrichment
    out = r._parse_holdings({"positions": [
        {"symbol": "MSFT", "quantity": "3", "market_value": "900.0", "unrealized_pl_pct": "5.0"},
    ]})
    assert len(out) == 1 and out[0].market_value == 900.0 and out[0].unrealized_pl_pct == 5.0
    assert r.calls == []                               # no quote call needed


def test_parse_falls_back_to_cost_basis_when_quote_missing():
    r = _reader(responses={"get_equity_quotes": {"results": []}})  # no price
    out = r._parse_holdings({"positions": [
        {"symbol": "NVDA", "quantity": "2", "average_buy_price": "100.0"},
    ]})
    assert out[0].market_value == 200.0                # 2 * 100 cost basis
    assert out[0].unrealized_pl_pct is None            # unknown without a price


# -- read-only enforcement (Todo-3 S.2) --------------------------------------- #
def test_read_tool_classifier():
    for tool in ("get_equity_positions", "get_accounts", "get_option_chains",
                 "__list_tools__", "search", "run_scan"):
        assert RobinhoodReader._is_read_tool(tool), tool
    for tool in ("place_equity_order", "place_option_order", "cancel_equity_order",
                 "review_option_order", "update_watchlist", "add_to_watchlist",
                 "remove_from_watchlist", "create_scan", "create_watchlist",
                 "follow_watchlist", "unfollow_watchlist", "update_scan_filters"):
        assert not RobinhoodReader._is_read_tool(tool), tool


def test_call_json_refuses_trade_tool_before_any_network():
    # The block fires before cfg/enabled/network are ever touched — an object
    # with NO cfg proves the refusal path can't accidentally dispatch.
    r = RobinhoodReader.__new__(RobinhoodReader)
    assert r.call_json("place_equity_order", {"symbol": "AAPL"}) is None
    assert r.call_json("cancel_option_order", {"id": "x"}) is None


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
