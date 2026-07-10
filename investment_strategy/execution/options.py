"""Options helpers — OCC symbol construction, premium estimation, leg building.

Keeps the defined-risk options plumbing separate from the equity client. A
TradeProposal carrying option_legs is turned into broker-ready OptionLegRequest
objects here; premium is estimated from the live option quote so the RiskManager
can bound the debit before anything is placed.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from math import gcd

from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionLatestQuoteRequest
from alpaca.trading.enums import OrderSide, PositionIntent
from alpaca.trading.requests import OptionLegRequest

from ..config import Config
from ..models import Action, OptionLeg, Position, TradeProposal

log = logging.getLogger("options")

_SIDE = {Action.BUY: OrderSide.BUY, Action.SELL: OrderSide.SELL}


def occ_symbol(underlying: str, expiry: str, strike: float, right: str) -> str:
    """Build an OCC option symbol, e.g. AAPL 2026-01-16 C 150 -> AAPL260116C00150000."""
    d = datetime.strptime(expiry, "%Y-%m-%d")
    cp = "C" if right.lower().startswith("c") else "P"
    strike_int = int(round(strike * 1000))
    return f"{underlying.upper()}{d:%y%m%d}{cp}{strike_int:08d}"


_OCC_RE = re.compile(r"^([A-Z][A-Z0-9.]{0,5})(\d{6})([CP])(\d{8})$")


def parse_occ(symbol: str) -> tuple[str, str, str, float] | None:
    """Inverse of occ_symbol: AAPL260116C00150000 ->
    ("AAPL", "2026-01-16", "C", 150.0). None when not OCC-shaped (an equity
    ticker never matches — the digits run is too short)."""
    m = _OCC_RE.match(symbol.upper().strip())
    if not m:
        return None
    under, ymd, right, strike = m.groups()
    try:
        expiry = datetime.strptime(ymd, "%y%m%d").strftime("%Y-%m-%d")
    except ValueError:
        return None
    return under, expiry, right, int(strike) / 1000.0


def build_closing_legs(positions: list[Position]) -> tuple[list[OptionLegRequest], int]:
    """Broker-ready legs that CLOSE existing option positions (one Position row
    per OCC contract; the short leg of a spread carries negative qty). Long ->
    SELL_TO_CLOSE, short -> BUY_TO_CLOSE, so a spread unwinds as ONE MLEG order
    and never passes through a naked-short intermediate state. Returns
    (legs, group_qty) where group_qty x ratio_qty = contracts per leg."""
    counts = [max(1, int(round(abs(p.qty)))) for p in positions]
    group_qty = counts[0]
    for c in counts[1:]:
        group_qty = gcd(group_qty, c)
    legs: list[OptionLegRequest] = []
    for pos, count in zip(positions, counts):
        is_long = pos.qty > 0
        legs.append(OptionLegRequest(
            symbol=pos.symbol,
            ratio_qty=count // group_qty,
            side=OrderSide.SELL if is_long else OrderSide.BUY,
            position_intent=(
                PositionIntent.SELL_TO_CLOSE if is_long
                else PositionIntent.BUY_TO_CLOSE
            ),
        ))
    return legs, group_qty


class OptionsHelper:
    def __init__(self, cfg: Config):
        self.data = OptionHistoricalDataClient(
            cfg.alpaca_api_key, cfg.alpaca_secret_key
        )

    def build_legs(self, proposal: TradeProposal) -> list[OptionLegRequest]:
        """Convert a proposal's OptionLeg list to broker OptionLegRequests."""
        legs: list[OptionLegRequest] = []
        for leg in proposal.option_legs:
            sym = occ_symbol(proposal.symbol, leg.expiry, leg.strike, leg.right)
            intent = (
                PositionIntent.BUY_TO_OPEN if leg.side is Action.BUY
                else PositionIntent.SELL_TO_OPEN
            )
            legs.append(OptionLegRequest(
                symbol=sym, ratio_qty=leg.ratio, side=_SIDE[leg.side],
                position_intent=intent,
            ))
        return legs

    def estimate_net_premium(self, proposal: TradeProposal) -> float:
        """Net debit per share (×100 = per contract) for the strategy. Buys add
        to the debit, sells subtract (credit). Returns 0 if any quote is missing.
        For defined-risk debit strategies this should be positive."""
        net = 0.0
        for leg in proposal.option_legs:
            mid = self._mid_price(proposal.symbol, leg)
            if mid <= 0:
                return 0.0
            net += mid * leg.ratio if leg.side is Action.BUY else -mid * leg.ratio
        return round(net, 2)

    def _mid_price(self, underlying: str, leg: OptionLeg) -> float:
        sym = occ_symbol(underlying, leg.expiry, leg.strike, leg.right)
        try:
            q = self.data.get_option_latest_quote(
                OptionLatestQuoteRequest(symbol_or_symbols=sym)
            )[sym]
            bid, ask = float(q.bid_price or 0), float(q.ask_price or 0)
            if bid > 0 and ask > 0:
                return (bid + ask) / 2
            return ask or bid
        except Exception as e:
            log.warning("option quote failed for %s: %s", sym, e)
            return 0.0
