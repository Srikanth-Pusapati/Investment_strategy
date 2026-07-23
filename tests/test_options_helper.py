"""Unit tests for OptionsHelper premium/quote plumbing — the guard that turns a
junk ONE-SIDED quote into a hard zero so the risk gate refuses the leg. This is
the source-level fix for the 2026-07-23 T blowup, where a stale $0.01 bid with
no ask was taken as a real mid and sized to 900 dead contracts."""
from datetime import datetime, timedelta, timezone

from investment_strategy.execution.options import OptionsHelper, occ_symbol
from investment_strategy.models import (
    Action,
    Instrument,
    OptionLeg,
    OptionStrategy,
    TradeProposal,
)


class _FakeQuote:
    def __init__(self, bid, ask):
        self.bid_price = bid
        self.ask_price = ask


class _FakeOptData:
    """Minimal stand-in for OptionHistoricalDataClient: maps OCC symbol ->
    quote (or None for 'feed has no NBBO for this contract')."""

    def __init__(self, quotes):
        self._quotes = quotes

    def get_option_latest_quote(self, req):
        sym = req.symbol_or_symbols
        return {sym: self._quotes.get(sym)}


def _helper(quotes):
    h = OptionsHelper.__new__(OptionsHelper)  # bypass __init__ (no live client)
    h.data = _FakeOptData(quotes)
    h._cfg = None
    h._trading = None
    return h


def _exp(days=30):
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%d")


def _long_call(expiry, strike=28):
    return TradeProposal(
        symbol="T", action=Action.BUY, conviction=1.0, target_weight_pct=1.0,
        rationale="opt", instrument=Instrument.OPTION,
        option_strategy=OptionStrategy.LONG_CALL,
        option_legs=[OptionLeg(expiry=expiry, strike=strike, right="call", side=Action.BUY)],
    )


def _spread(expiry):
    return TradeProposal(
        symbol="T", action=Action.BUY, conviction=1.0, target_weight_pct=1.0,
        rationale="opt", instrument=Instrument.OPTION,
        option_strategy=OptionStrategy.BULL_CALL_SPREAD,
        option_legs=[
            OptionLeg(expiry=expiry, strike=28, right="call", side=Action.BUY),
            OptionLeg(expiry=expiry, strike=30, right="call", side=Action.SELL),
        ],
    )


def test_two_sided_quote_averages_to_mid():
    exp = _exp()
    sym = occ_symbol("T", exp, 28, "call")
    h = _helper({sym: _FakeQuote(0.50, 0.54)})
    assert h.estimate_net_premium(_long_call(exp)) == 0.52


def test_one_sided_quote_yields_zero_premium():
    """A stale bid with no ask (the T blowup shape) is NOT a tradeable mid — it
    must collapse to 0.0 so evaluate_option's est_premium<=0 gate refuses it."""
    exp = _exp()
    sym = occ_symbol("T", exp, 28, "call")
    h = _helper({sym: _FakeQuote(0.01, 0.0)})
    p = _long_call(exp)
    assert h.estimate_net_premium(p) == 0.0
    assert h.min_leg_premium(p) == 0.0


def test_missing_quote_yields_zero_premium():
    exp = _exp()
    h = _helper({})  # feed has no NBBO for the contract at all
    p = _long_call(exp)
    assert h.estimate_net_premium(p) == 0.0
    assert h.min_leg_premium(p) == 0.0


def test_min_leg_premium_zero_when_any_leg_quoteless():
    """A spread where one leg has no market can't be priced honestly — the
    cheapest-leg floor input is 0.0 (blocks it downstream)."""
    exp = _exp()
    long_sym = occ_symbol("T", exp, 28, "call")
    h = _helper({long_sym: _FakeQuote(0.50, 0.54)})  # short leg missing
    assert h.min_leg_premium(_spread(exp)) == 0.0


def test_min_leg_premium_reports_cheapest_leg_not_net():
    """The floor input is the cheapest LEG (0.22), even though the net debit
    (0.52 - 0.22 = 0.30) is larger — a junk penny leg can't hide behind the
    net."""
    exp = _exp()
    long_sym = occ_symbol("T", exp, 28, "call")
    short_sym = occ_symbol("T", exp, 30, "call")
    h = _helper({long_sym: _FakeQuote(0.50, 0.54), short_sym: _FakeQuote(0.20, 0.24)})
    p = _spread(exp)
    assert h.estimate_net_premium(p) == 0.30
    assert h.min_leg_premium(p) == 0.22
