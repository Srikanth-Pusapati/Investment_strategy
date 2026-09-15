"""Options helpers — OCC symbol construction, premium estimation, leg building.

Keeps the defined-risk options plumbing separate from the equity client. A
TradeProposal carrying option_legs is turned into broker-ready OptionLegRequest
objects here; premium is estimated from the live option quote so the RiskManager
can bound the debit before anything is placed.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
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


def _is_third_friday(expiry) -> bool:
    """True when `expiry` (ISO string or date) is the third Friday of its
    month — the standard monthly expiry, where listed-option open interest
    concentrates. Run-7 S-3: the proxy-put builder ranks these expiries
    first because the run-6 picks landed on thin WEEKLIES (Sep 1 2026:
    IWM Oct-9 291P OI 38 -> rejected; the Oct-16 monthly 291P carried
    >= 8,829 OI the same morning). Malformed input is simply not a third
    Friday (False), never an exception — the builder ranks with it."""
    try:
        d = expiry if isinstance(expiry, date) else date.fromisoformat(str(expiry)[:10])
    except (TypeError, ValueError):
        return False
    return d.weekday() == 4 and 15 <= d.day <= 21


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
        min_oi: float | None = None, prefer_monthly: bool = True,
        max_below_spot_pct: float = 2.0, today: date | None = None,
    ) -> list[OptionLeg] | None:
        """Deterministic near-ATM bear put spread on a LIQUID proxy ETF (the
        put-liquidity fallback, Aug 14), now OI-aware and monthly-first
        (run-7 S-3).

        WHY: the legacy pick was expiry = argmin |dte - mid| and long = the
        highest strike <= spot, never reading the `open_interest` the same
        contract objects carry — so the builder handed the risk gate the
        thin Oct-9 WEEKLY three times in run-6 (Sep 1 IWM 291P OI 38 and
        Sep 3 294P OI 47 -> `REJECT ... open interest < 100 floor`; only
        Sep 4's 295P at OI 1,011 passed) while the Oct-16 MONTHLY sat inside
        the [25, 50] DTE window with >= 5,000 OI on every candidate strike.
        Two of three in-window proxy attempts were self-inflicted. A "$5
        grid" heuristic was checked and rejected (Oct-9 289/292/293 carried
        743/822/1,142) — liquidity is a property of the listed OI, not the
        strike's roundness, so rank by OI.

        Selection (one contract request, as before):
          * candidate expiries in [today+min_dte, today+max_dte] ranked
            (third-Friday first when `prefer_monthly`, then |dte - mid|);
          * per expiry: long = the highest strike in
            [spot x (1 - max_below_spot_pct%), spot] with oi >= min_oi,
            short = the highest strike <= long x (1 - width_pct%) with
            oi >= min_oi — non-qualifying strikes are skipped DOWNWARD;
          * the first expiry with a qualifying pair wins.
        A leg whose OI reads None is EXCLUDED whenever any contract in the
        chain carries OI (the gate fails open on None; the builder must not
        lean on that). The legacy pick is used only when NO contract carries
        OI or the floor is off (`min_oi` None/0) — i.e. when there is nothing
        to rank by. Strikes/expiries still come from the venue's own list —
        never synthesized. Returns None when no pair qualifies (the caller
        logs and moves on; the greppable `PROXY PUT PICK:` line carries the
        legacy counterfactual either way)."""
        from alpaca.trading.requests import GetOptionContractsRequest
        if spot <= 0:
            return None
        if today is None:
            today = datetime.now().date()
        elif isinstance(today, datetime):
            today = today.date()
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
        if getattr(resp, "next_page_token", None):
            # One request only (as before): a second page would mean the
            # ranking below saw a truncated chain. Surface it rather than
            # silently picking from half the strikes.
            log.warning(
                "proxy put chain for %s is paginated (next_page_token set) — "
                "ranking only the first %d contracts", underlying, len(contracts),
            )
        # expiry -> strike -> open interest (None when the venue reports none)
        chain: dict[str, dict[float, float | None]] = {}
        for c in contracts:
            try:
                exp = str(c.expiration_date)
                strike = float(c.strike_price)
            except (TypeError, ValueError, AttributeError):
                continue
            raw_oi = getattr(c, "open_interest", None)
            try:
                oi = None if raw_oi is None else float(raw_oi)
            except (TypeError, ValueError):
                oi = None
            chain.setdefault(exp, {})[strike] = oi
        if not chain:
            return None

        target = today + timedelta(days=(int(min_dte) + int(max_dte)) // 2)

        def _dte(exp: str) -> int:
            return (date.fromisoformat(exp) - today).days

        def _legacy_pair() -> tuple[str, float, float] | None:
            """The pre-S-3 pick, kept verbatim for the fallback and the
            counterfactual log line."""
            expiry = min(chain, key=lambda e: abs(_dte(e) - (target - today).days))
            strikes = sorted(chain[expiry])
            longs = [k for k in strikes if k <= spot]
            if not longs:
                return None
            long_k = longs[-1]
            shorts = [k for k in strikes if k <= long_k * (1 - width_pct / 100.0)]
            if not shorts:
                return None
            return expiry, long_k, shorts[-1]

        def _legs(expiry: str, long_k: float, short_k: float) -> list[OptionLeg]:
            return [
                OptionLeg(expiry=expiry, strike=long_k, right="put", side=Action.BUY),
                OptionLeg(expiry=expiry, strike=short_k, right="put", side=Action.SELL),
            ]

        def _oi_txt(expiry: str, strike: float) -> str:
            oi = chain.get(expiry, {}).get(strike)
            return "?" if oi is None else f"{oi:.0f}"

        legacy = _legacy_pair()
        floor = float(min_oi or 0.0)
        chain_has_oi = any(
            oi is not None for strikes in chain.values() for oi in strikes.values()
        )
        if floor <= 0 or not chain_has_oi:
            why = "OI floor off" if floor <= 0 else "no OI data in chain"
            if legacy is None:
                log.info("PROXY PUT PICK: %s none — legacy pick found no pair (%s)",
                         underlying, why)
                return None
            log.info(
                "PROXY PUT PICK: %s %s %g/%g (legacy pick: %s)",
                underlying, legacy[0], legacy[1], legacy[2], why,
            )
            return _legs(*legacy)

        mid_days = (target - today).days
        ranked = sorted(
            chain,
            key=lambda e: (
                not (prefer_monthly and _is_third_friday(e)),
                abs(_dte(e) - mid_days),
                e,
            ),
        )
        long_floor = spot * (1 - max_below_spot_pct / 100.0)

        def _ok(expiry: str, strike: float) -> bool:
            oi = chain[expiry][strike]
            return oi is not None and oi >= floor

        for expiry in ranked:
            strikes = sorted(chain[expiry])
            longs = [k for k in strikes if long_floor <= k <= spot and _ok(expiry, k)]
            if not longs:
                continue
            long_k = longs[-1]
            shorts = [
                k for k in strikes
                if k <= long_k * (1 - width_pct / 100.0) and _ok(expiry, k)
            ]
            if not shorts:
                continue
            short_k = shorts[-1]
            kind = "monthly" if _is_third_friday(expiry) else "weekly"
            if legacy == (expiry, long_k, short_k):
                counterfactual = "= legacy pick"
            elif legacy is None:
                counterfactual = "over legacy none (no pair)"
            else:
                counterfactual = (
                    f"over legacy {legacy[0]} {legacy[1]:g}/{legacy[2]:g} "
                    f"(OI {_oi_txt(legacy[0], legacy[1])}/{_oi_txt(legacy[0], legacy[2])})"
                )
            log.info(
                "PROXY PUT PICK: %s %s %g/%g (OI %s/%s, %s, %dd) %s",
                underlying, expiry, long_k, short_k,
                _oi_txt(expiry, long_k), _oi_txt(expiry, short_k),
                kind, _dte(expiry), counterfactual,
            )
            return _legs(expiry, long_k, short_k)

        if legacy is None:
            log.info(
                "PROXY PUT PICK: %s none — no OI-qualified pair in [%s, %s] "
                "(min OI %.0f) and no legacy pair either",
                underlying, lo.isoformat(), hi.isoformat(), floor,
            )
        else:
            log.info(
                "PROXY PUT PICK: %s none — no OI-qualified pair in [%s, %s] "
                "(min OI %.0f); legacy %s %g/%g (OI %s/%s) would have been "
                "proposed and rejected at the gate",
                underlying, lo.isoformat(), hi.isoformat(), floor,
                legacy[0], legacy[1], legacy[2],
                _oi_txt(legacy[0], legacy[1]), _oi_txt(legacy[0], legacy[2]),
            )
        return None

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

    # ------------------------------------------------------------------ #
    # Run-7 S-4: single-name put strike snap (near-the-money, OI-qualified)
    # ------------------------------------------------------------------ #
    def snap_legs_to_liquid(
        self, underlying: str, legs: list[OptionLeg], spot: float,
        min_oi: float | None, max_spread_pct: float | None,
        max_moneyness_pct: float = 10.0, strike_tol_pct: float = 5.0,
        min_dte: float | None = None, max_dte: float | None = None,
        today: date | None = None,
    ) -> list[OptionLeg] | None:
        """Move a single-name PUT structure's strikes to the nearest strike
        the liquidity gate can accept, near the money. Returns the (possibly
        unchanged) legs, or None when no qualifying strike exists / the
        chain cannot be read / the structure is not one this helper owns —
        the caller then judges the model's legs AS PROPOSED (the existing
        OI/spread reject path; never a silent drop).

        WHY: every single-name put failure in run-6 shared ONE mechanism —
        the model picked strikes far from the money although the prompt
        asks for "strikes at/near the money": HD 400P at ~25% ITM (OI 2,
        Sep 3/4 2026), LTH 30P 28% OTM (OI 26), LYV 150P/140P 12-18% OTM
        (OI 5/87), SCI 75P 6% OTM (OI 5), AAL 11P/10P 14-22% OTM with
        $0.03-0.14 of premium (spreads 12-91%) — while each chain carried
        OI >= 100 within ~3-7% of spot (HD 320P 1,744; LTH 40P 843; LYV
        170P 368; SCI 77.5P 161; AAL 12P 9,581). Root cause: the prompt
        rendered no spot price for the name (the Sep 4 journal calls HD's
        400 strike "the ATM put" with HD at $321), and nothing between the
        model and the gate normalised a strike against spot. The gate can
        only reject; this step supplies the tradeable contract.

        Rule (two-sided, ITM and OTM alike):
          * a long-put strike OUTSIDE [spot x (1 - m), spot x (1 + m)]
            (m = max_moneyness_pct) is treated as "the model had no spot":
            its target becomes SPOT (the at-the-money strike it was asking
            for). A literal clamp to the band EDGE was considered and
            rejected — HD 400P would land on 345P (8% ITM, OI 122): still
            ~$25 of intrinsic per share, a synthetic short sized to one
            contract under the per-underlying cap, not the near-ATM put the
            prompt describes;
          * a strike INSIDE the band keeps its own strike as the target;
          * the target then moves to the nearest OI-qualified strike
            (oi >= min_oi; a two-sided NBBO no wider than max_spread_pct)
            on the SAME expiry, within strike_tol_pct of spot; failing
            that, the nearest third-Friday expiry within +/-7 days (inside
            the DTE window when min_dte/max_dte/today are given);
          * a spread keeps its STRUCTURE: the short leg re-targets
            new_long - original_width and snaps to the nearest qualified
            strike strictly below the new long; both legs stay on ONE
            expiry. right/side/ratio never change; only strike/expiry.
        A leg whose OI reads None is qualified only when NO contract in the
        chain carries OI (nothing to rank by; the gate fails open on None
        too) — otherwise None means "not listed with interest". A strike
        with no two-sided quote is never snapped onto (est_premium<=0 would
        refuse it downstream); a quote FEED error fails open like the gate.
        Calls, proxy/index legs, multi-expiry or unrecognised structures:
        the caller does not route them here, and the helper returns None
        for anything it does not recognise. Emits the greppable
        `STRIKE SNAP:` line with the unsnapped counterfactual."""
        from alpaca.trading.requests import GetOptionContractsRequest
        try:
            spot = float(spot or 0.0)
        except (TypeError, ValueError):
            return None
        if spot <= 0 or not legs:
            return None
        if not all(str(l.right).lower().startswith("p") for l in legs):
            return None
        longs = [l for l in legs if l.side is Action.BUY]
        shorts = [l for l in legs if l.side is Action.SELL]
        if len(longs) != 1 or len(shorts) > 1 or len(legs) != len(longs) + len(shorts):
            return None
        long_leg = longs[0]
        short_leg = shorts[0] if shorts else None
        if any(l.expiry != long_leg.expiry for l in legs):
            return None
        if short_leg is not None and not short_leg.strike < long_leg.strike:
            return None  # not a bear put spread — leave the structure alone
        try:
            orig_exp = date.fromisoformat(str(long_leg.expiry)[:10])
        except (TypeError, ValueError):
            return None
        if today is None:
            today = datetime.now().date()
        elif isinstance(today, datetime):
            today = today.date()

        m = max(0.0, float(max_moneyness_pct or 0.0)) / 100.0
        band_lo, band_hi = spot * (1.0 - m), spot * (1.0 + m)
        tol = spot * max(0.0, float(strike_tol_pct or 0.0)) / 100.0
        floor = float(min_oi or 0.0)
        spread_cap = float(max_spread_pct or 0.0)
        orig_strikes = [float(l.strike) for l in legs]
        lo_k = min([band_lo - tol] + orig_strikes)
        hi_k = max([band_hi + tol] + orig_strikes)
        try:
            resp = self.trading.get_option_contracts(GetOptionContractsRequest(
                underlying_symbols=[underlying.upper()],
                type="put",
                expiration_date_gte=(orig_exp - timedelta(days=7)).isoformat(),
                expiration_date_lte=(orig_exp + timedelta(days=7)).isoformat(),
                strike_price_gte=str(round(max(0.01, lo_k), 2)),
                strike_price_lte=str(round(hi_k, 2)),
                limit=1000,
            ))
            contracts = list(resp.option_contracts or [])
        except Exception as e:  # noqa: BLE001 — chain read failure = no snap
            log.warning("STRIKE SNAP: %s chain lookup failed (%s) — model legs stand",
                        underlying, e)
            return None
        if getattr(resp, "next_page_token", None):
            log.warning(
                "STRIKE SNAP: %s chain is paginated (next_page_token set) — "
                "snapping from the first %d contracts only", underlying, len(contracts),
            )
        chain: dict[str, dict[float, float | None]] = {}
        for c in contracts:
            try:
                exp = str(c.expiration_date)[:10]
                strike = float(c.strike_price)
            except (TypeError, ValueError, AttributeError):
                continue
            raw_oi = getattr(c, "open_interest", None)
            try:
                oi = None if raw_oi is None else float(raw_oi)
            except (TypeError, ValueError):
                oi = None
            chain.setdefault(exp, {})[strike] = oi
        if not chain:
            log.info("STRIKE SNAP: %s none — empty put chain around %s; model legs stand",
                     underlying, long_leg.expiry)
            return None
        chain_has_oi = any(
            oi is not None for strikes in chain.values() for oi in strikes.values()
        )

        def _oi(exp: str, k: float) -> float | None:
            return chain.get(exp, {}).get(k)

        def _oi_txt(exp: str, k: float) -> str:
            oi = _oi(exp, k)
            return "?" if oi is None else f"{oi:.0f}"

        def _qualified(exp: str, k: float) -> bool:
            if floor <= 0:
                return True
            oi = _oi(exp, k)
            if oi is None:
                return not chain_has_oi
            return oi >= floor

        def _money(k: float) -> str:
            pct = (k - spot) / spot * 100.0
            if abs(pct) < 0.05:
                return "ATM"
            return f"{abs(pct):.1f}% {'ITM' if pct > 0 else 'OTM'}"

        spread_cache: dict[str, tuple[bool, float | None]] = {}

        def _spread_ok(exp: str, k: float) -> tuple[bool, float | None]:
            """(tradeable?, rel spread %). No/one-sided NBBO -> not
            tradeable (never snap onto a quote-less strike); a feed error
            fails open (True, None) exactly like the gate."""
            sym = occ_symbol(underlying, exp, k, "put")
            if sym in spread_cache:
                return spread_cache[sym]
            try:
                q = self.data.get_option_latest_quote(
                    OptionLatestQuoteRequest(symbol_or_symbols=sym)
                ).get(sym)
            except Exception as e:  # noqa: BLE001
                log.debug("STRIKE SNAP: quote read failed for %s: %s", sym, e)
                spread_cache[sym] = (True, None)
                return spread_cache[sym]
            out: tuple[bool, float | None]
            if q is None:
                out = (False, None)
            else:
                bid, ask = float(q.bid_price or 0), float(q.ask_price or 0)
                if bid <= 0 or ask <= bid:
                    out = (False, None)
                else:
                    spread = (ask - bid) / ((ask + bid) / 2) * 100.0
                    out = (not (spread_cap > 0 and spread > spread_cap), spread)
            spread_cache[sym] = out
            return out

        # Expiries: the model's own first, then the nearest third Friday
        # within +/-7 days (inside the DTE window when the caller gave one).
        def _alt_ok(exp: str) -> bool:
            try:
                d = date.fromisoformat(exp)
            except ValueError:
                return False
            if exp == long_leg.expiry or not _is_third_friday(d):
                return False
            if abs((d - orig_exp).days) > 7:
                return False
            dte = (d - today).days
            if min_dte is not None and dte < float(min_dte):
                return False
            if max_dte is not None and dte > float(max_dte):
                return False
            return True

        expiries = [long_leg.expiry] + sorted(
            (e for e in chain if _alt_ok(e)),
            key=lambda e: (abs((date.fromisoformat(e) - orig_exp).days), e),
        )

        in_band = band_lo <= float(long_leg.strike) <= band_hi
        target = float(long_leg.strike) if in_band else spot
        width = (float(long_leg.strike) - float(short_leg.strike)) if short_leg else 0.0
        max_quote_probes = 6  # bound quote calls per expiry on a dead feed

        chosen: tuple[str, float, float | None] | None = None
        for exp in expiries:
            strikes = chain.get(exp)
            if not strikes:
                continue
            long_cands = sorted(
                (k for k in strikes if abs(k - target) <= tol and _qualified(exp, k)),
                key=lambda k: (abs(k - target), k),
            )[:max_quote_probes]
            for k in long_cands:
                ok, _ = _spread_ok(exp, k)
                if not ok:
                    continue
                if short_leg is None:
                    chosen = (exp, k, None)
                    break
                s_target = k - width
                short_cands = sorted(
                    (s for s in strikes
                     if s < k and abs(s - s_target) <= tol and _qualified(exp, s)),
                    key=lambda s: (abs(s - s_target), -s),
                )[:max_quote_probes]
                for s in short_cands:
                    s_ok, _ = _spread_ok(exp, s)
                    if s_ok:
                        chosen = (exp, k, s)
                        break
                if chosen:
                    break
            if chosen:
                break

        def _legs_txt(exp: str, ks: list[float], show_exp: bool) -> str:
            head = f"{exp} " if show_exp else ""
            return (
                f"{head}{'/'.join(f'{k:g}' for k in ks)}P "
                f"(OI {'/'.join(_oi_txt(exp, k) for k in ks)}, "
                f"{'/'.join(_money(k) for k in ks)})"
            )

        # The gate's own verdict on the UNSNAPPED legs — the counterfactual.
        def _unsnapped_verdict() -> str:
            for l in legs:
                k, exp = float(l.strike), l.expiry
                oi = _oi(exp, k)
                if floor > 0 and oi is not None and oi < floor:
                    return f"unsnapped would be rejected: open interest {oi:.0f} < {floor:.0f}"
                ok, spread = _spread_ok(exp, k)
                if not ok and spread is not None:
                    return (f"unsnapped would be rejected: bid-ask spread "
                            f"{spread:.1f}% > {spread_cap:.0f}%")
                if not ok:
                    return "unsnapped would be rejected: no two-sided quote"
            return (f"unsnapped would pass the gate at {_money(float(long_leg.strike))} "
                    f"(outside the +/-{m * 100:g}% band)")

        orig_txt = _legs_txt(long_leg.expiry, orig_strikes, True)
        if chosen is None:
            log.info(
                "STRIKE SNAP: %s %s none — no OI-qualified put strike within "
                "%.1f%% of %s (min OI %.0f, spread cap %.0f%%, spot %.2f); "
                "model legs stand and the gate will judge them",
                underlying, orig_txt, strike_tol_pct,
                "spot" if not in_band else f"the {long_leg.strike:g} strike",
                floor, spread_cap, spot,
            )
            return None
        new_exp, new_long, new_short = chosen
        new_strikes = [new_long] + ([new_short] if short_leg is not None else [])
        if new_exp == long_leg.expiry and new_strikes == orig_strikes:
            log.info("STRIKE SNAP: %s %s kept — in band and OI-qualified",
                     underlying, orig_txt)
            return list(legs)
        out: list[OptionLeg] = []
        for l in legs:
            k = new_long if l.side is Action.BUY else new_short
            out.append(OptionLeg(
                expiry=new_exp, strike=float(k), right=l.right, side=l.side,
                ratio=l.ratio,
            ))
        log.info(
            "STRIKE SNAP: %s %s -> %s; %s",
            underlying, orig_txt,
            _legs_txt(new_exp, new_strikes, new_exp != long_leg.expiry),
            _unsnapped_verdict(),
        )
        return out
