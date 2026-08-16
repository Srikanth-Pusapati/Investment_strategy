"""Options helpers — OCC symbol construction, premium estimation, leg building.

Keeps the defined-risk options plumbing separate from the equity client. A
TradeProposal carrying option_legs is turned into broker-ready OptionLegRequest
objects here; premium is estimated from the live option quote so the RiskManager
can bound the debit before anything is placed.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from math import gcd

from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionLatestQuoteRequest
from alpaca.trading.enums import OrderSide, PositionIntent
from alpaca.trading.requests import OptionLegRequest

from ..config import Config
from .alpaca_client import bound_client
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


def split_option_close_chunks(positions: list[Position]) -> list[list[Position]]:
    """Partition an option group too big to close as ONE MLEG order — Alpaca
    caps the mleg order class at 4 legs (2026-08-07: a 5-leg AMZN call group,
    two structures merged under one underlying+expiry, had its stop-close
    rejected with "At most 4 legs are allowed" on every retry, leaving the
    book unprotected) — into chunks that are each risk-safe to close alone:

    - every SHORT leg travels with a covering LONG (call cover: long strike
      <= short strike; put cover: long strike >= short strike) in the same
      2-leg chunk, so a cover is only ever sold in the SAME atomic order
      that buys its short back — no fill order can pass through a
      naked-short state;
    - a short with no available cover closes ALONE (buying back a short is
      pure risk reduction, always safe);
    - leftover longs close alone, last.

    A pairing that consumes only part of a leg clones the row at the matched
    qty; the remainder stays available for further chunks. Contracts in the
    output always sum back to the input.
    """
    shorts: list[dict] = []
    longs: list[dict] = []
    for p in positions:
        occ = parse_occ(p.symbol)
        row = {
            "pos": p, "left": abs(p.qty),
            "right": occ[2] if occ else None,
            "strike": occ[3] if occ else None,
        }
        (shorts if p.qty < 0 else longs).append(row)

    def _covers(lng: dict, sht: dict) -> bool:
        if lng["right"] is None or lng["right"] != sht["right"]:
            return False
        if sht["right"] == "C":
            return lng["strike"] <= sht["strike"]
        return lng["strike"] >= sht["strike"]

    def _clone(row: dict, qty: float) -> Position:
        return row["pos"].model_copy(
            update={"qty": qty, "qty_available": qty}
        )

    chunks: list[list[Position]] = []
    # Hardest-to-cover shorts first (lowest call strike / highest put strike
    # has the fewest eligible covers); give each the least generally-useful
    # cover (tightest strike) so wider covers stay free for later shorts.
    def _short_order(r: dict):
        if r["strike"] is None:
            return float("inf")
        return r["strike"] if r["right"] == "C" else -r["strike"]

    for sht in sorted([r for r in shorts if r["right"]], key=_short_order):
        while sht["left"] > 1e-9:
            cands = [l for l in longs if l["left"] > 1e-9 and _covers(l, sht)]
            if not cands:
                break
            # Prefer a cover that absorbs the WHOLE short (fewest orders),
            # tightest strike among those; only fragment across covers when
            # no single long is big enough (then: biggest first).
            full = [l for l in cands if l["left"] >= sht["left"] - 1e-9]
            strike_sign = 1.0 if sht["right"] == "C" else -1.0
            if full:
                cover = max(full, key=lambda l: strike_sign * l["strike"])
            else:
                cover = max(cands, key=lambda l: l["left"])
            qty = min(sht["left"], cover["left"])
            chunks.append([_clone(cover, qty), _clone(sht, -qty)])
            sht["left"] -= qty
            cover["left"] -= qty
    for sht in shorts:                       # bare shorts: safe to close alone
        if sht["left"] > 1e-9:
            chunks.append([_clone(sht, -sht["left"])])
    for lng in longs:                        # leftover longs go last
        if lng["left"] > 1e-9:
            chunks.append([_clone(lng, lng["left"])])
    return chunks


class OptionsHelper:
    def __init__(self, cfg: Config):
        self.data = bound_client(OptionHistoricalDataClient(
            cfg.alpaca_api_key, cfg.alpaca_secret_key
        ))
        self._cfg = cfg
        self._trading = None  # lazy — only liquidity checks need the trading API

    @property
    def trading(self):
        if self._trading is None:
            from alpaca.trading.client import TradingClient
            self._trading = bound_client(TradingClient(
                self._cfg.alpaca_api_key, self._cfg.alpaca_secret_key,
                paper=not self._cfg.is_live,
            ))
        return self._trading

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

    def min_leg_premium(self, proposal: TradeProposal) -> float:
        """Smallest per-leg mid (per share) across the structure. The risk gate
        floors THIS, not the net debit: a defined-risk vertical can have a
        legitimately small NET, but every leg must still be a real, priced
        contract — a $0.01 leg is deep-OTM/illiquid junk regardless of the net.
        Returns 0.0 if any leg has no two-sided quote (already refused upstream
        by the est_premium<=0 gate)."""
        mids = [self._mid_price(proposal.symbol, leg) for leg in proposal.option_legs]
        if not mids or any(m <= 0 for m in mids):
            return 0.0
        return round(min(mids), 2)

    def leg_liquidity(self, proposal: TradeProposal) -> list[dict]:
        """Per-leg {'symbol', 'oi', 'rel_spread_pct'} context for the risk
        gate's liquidity check. OI comes from the trading API's contract
        metadata; the spread from the same latest quote the premium estimate
        reads. Any field we can't source is None — the gate fails open on None
        (est_premium<=0 already refuses quote-less legs)."""
        out: list[dict] = []
        for leg in proposal.option_legs:
            sym = occ_symbol(proposal.symbol, leg.expiry, leg.strike, leg.right)
            oi = None
            try:
                from alpaca.trading.requests import GetOptionContractsRequest
                resp = self.trading.get_option_contracts(
                    GetOptionContractsRequest(
                        underlying_symbols=[proposal.symbol.upper()],
                        expiration_date=leg.expiry,
                        strike_price_gte=str(leg.strike),
                        strike_price_lte=str(leg.strike),
                        type="call" if leg.right.lower().startswith("c") else "put",
                    )
                )
                for c in (resp.option_contracts or []):
                    if c.symbol == sym and c.open_interest is not None:
                        oi = float(c.open_interest)
                        break
            except Exception as e:
                log.warning("open-interest lookup failed for %s: %s", sym, e)
            spread = None
            try:
                q = self.data.get_option_latest_quote(
                    OptionLatestQuoteRequest(symbol_or_symbols=sym)
                ).get(sym)
                # The feed simply omits contracts it has no NBBO for (illiquid,
                # unlisted strike, past-dated expiry) — spread stays None and
                # _legs_liquid fails open; the est_premium<=0 gate is what
                # refuses the quote-less leg (_mid_price logs the one line).
                if q is not None:
                    bid, ask = float(q.bid_price or 0), float(q.ask_price or 0)
                    if bid > 0 and ask > bid:
                        spread = (ask - bid) / ((ask + bid) / 2) * 100.0
            except Exception as e:
                log.warning("spread lookup failed for %s: %s", sym, e)
            out.append({"symbol": sym, "oi": oi, "rel_spread_pct": spread})
        return out

    def build_proxy_put_spread(
        self, underlying: str, spot: float,
        min_dte: int = 25, max_dte: int = 50, width_pct: float = 5.0,
    ) -> list[OptionLeg] | None:
        """Deterministic near-ATM bear put spread on a LIQUID proxy ETF (the
        put-liquidity fallback, Aug 14): long the highest put strike at or
        below spot, short the nearest strike at or below long x (1-width%),
        on the listed expiry closest to the middle of [min_dte, max_dte].
        Strikes/expiries come from the venue's own contract list — never
        synthesized, so an unlisted strike can't be proposed. Returns None
        when the chain has no workable pair (caller logs and moves on)."""
        from alpaca.trading.requests import GetOptionContractsRequest
        if spot <= 0:
            return None
        today = datetime.now().date()
        lo = today + timedelta(days=int(min_dte))
        hi = today + timedelta(days=int(max_dte))
        try:
            resp = self.trading.get_option_contracts(GetOptionContractsRequest(
                underlying_symbols=[underlying.upper()],
                type="put",
                expiration_date_gte=lo.isoformat(),
                expiration_date_lte=hi.isoformat(),
                strike_price_gte=str(round(spot * (1 - 2.5 * width_pct / 100.0), 2)),
                strike_price_lte=str(round(spot * 1.02, 2)),
                limit=500,
            ))
            contracts = list(resp.option_contracts or [])
        except Exception as e:
            log.warning("proxy put chain lookup failed for %s: %s", underlying, e)
            return None
        by_expiry: dict[str, list[float]] = {}
        for c in contracts:
            try:
                exp = str(c.expiration_date)
                by_expiry.setdefault(exp, []).append(float(c.strike_price))
            except (TypeError, ValueError):
                continue
        if not by_expiry:
            return None
        target = today + timedelta(days=(int(min_dte) + int(max_dte)) // 2)
        expiry = min(
            by_expiry,
            key=lambda e: abs((datetime.strptime(e, "%Y-%m-%d").date() - target).days),
        )
        strikes = sorted(set(by_expiry[expiry]))
        longs = [k for k in strikes if k <= spot]
        if not longs:
            return None
        long_k = longs[-1]
        shorts = [k for k in strikes if k <= long_k * (1 - width_pct / 100.0)]
        if not shorts:
            return None
        short_k = shorts[-1]
        return [
            OptionLeg(expiry=expiry, strike=long_k, right="put", side=Action.BUY),
            OptionLeg(expiry=expiry, strike=short_k, right="put", side=Action.SELL),
        ]

    def _mid_price(self, underlying: str, leg: OptionLeg) -> float:
        sym = occ_symbol(underlying, leg.expiry, leg.strike, leg.right)
        try:
            q = self.data.get_option_latest_quote(
                OptionLatestQuoteRequest(symbol_or_symbols=sym)
            ).get(sym)
            if q is None:
                # No NBBO on the feed (illiquid, unlisted strike, past-dated
                # expiry) — not an API failure. 0.0 makes the risk gate refuse
                # the leg (est_premium<=0), which is the designed backstop.
                log.warning("no quote available for %s — contract unknown to feed", sym)
                return 0.0
            bid, ask = float(q.bid_price or 0), float(q.ask_price or 0)
            if bid > 0 and ask > 0:
                return (bid + ask) / 2
            # One-sided or empty NBBO = no real two-sided market. A leg you
            # can't get a live bid AND ask on is a leg you can't exit — the
            # 2026-07-23 T blowup was a stale $0.01 bid with no ask, taken as a
            # real mid, sized to 900 dead contracts. NEVER fabricate a mid from
            # one side: return 0.0 so evaluate_option's est_premium<=0 backstop
            # refuses the leg outright.
            log.warning(
                "one-sided/empty NBBO for %s (bid=%.4f ask=%.4f) — no tradeable "
                "market, treating as no quote", sym, bid, ask,
            )
            return 0.0
        except Exception as e:
            log.warning("option quote failed for %s: %s", sym, e)
            return 0.0
