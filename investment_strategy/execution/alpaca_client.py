"""Thin wrapper over alpaca-py: account/positions, prices, volatility, and orders.

All trading goes through here so the paper/live boundary lives in exactly one
place. Supports equities + ETFs (whole or fractional), limit/stop orders, bracket
exits, scale-in/out ladders, and defined-risk options.

Key Alpaca constraints handled here:
  - Fractional / notional orders cannot carry bracket (stop+take-profit) legs and
    must be market or limit DAY. We submit them plain and rely on the watchdog.
  - Bracket orders are whole-share equity only.
"""
from __future__ import annotations

import functools
import logging
import statistics
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, NamedTuple, Optional, TypeVar

from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import Timeout as RequestsTimeout
from urllib3.exceptions import ProtocolError

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (
    GetOrdersRequest,
    GetPortfolioHistoryRequest,
    LimitOrderRequest,
    MarketOrderRequest,
    OptionLegRequest,
    ReplaceOrderRequest,
    StopLossRequest,
    StopOrderRequest,
    TakeProfitRequest,
)

from ..config import Config
from ..models import (
    AccountSnapshot,
    Action,
    OrderRequest,
    OrderType,
    Position,
    RiskDecision,
    RiskVerdict,
    TIF,
)

log = logging.getLogger("alpaca")

_SIDE = {Action.BUY: OrderSide.BUY, Action.SELL: OrderSide.SELL}
_TIF = {TIF.DAY: TimeInForce.DAY, TIF.GTC: TimeInForce.GTC, TIF.IOC: TimeInForce.IOC}

_T = TypeVar("_T")


class BuySubmission(NamedTuple):
    """What was ACTUALLY sent to the broker. The whole-share bracket path floors
    the risk layer's sized qty and drops the sub-share remainder, so the
    decision's approved qty/notional can OVERSTATE the real order — the ledger
    and the intra-cycle capital snapshot must record these numbers, not the
    decision's (a divergence reconcile can't catch: the floored order fills
    completely, so fill-vs-order checks see nothing wrong)."""
    order_id: Optional[str]
    fractional: bool
    qty: float        # shares submitted (approx for notional orders: $/price)
    notional: float   # dollars submitted (approx for whole-share: qty*price)
    dropped_notional: float = 0.0  # $ the whole-share flooring left undeployed


_NO_BUY = BuySubmission(None, False, 0.0, 0.0)

# alpaca-py's RESTClient exposes NO timeout knob and issues every request
# through a bare requests.Session with no timeout argument — so a wedged read
# blocks forever ("Read timed out. (read timeout=None)", seen 2026-07-14 on
# the news feed; a hang here stalls the whole decision cycle, and via the
# trade lock can starve the watchdog thread). Bind (connect, read) bounds at
# the Session level; a fired timeout surfaces as requests.Timeout, which is
# already in _TRANSIENT_NET and absorbed as a transient tick-skip.
HTTP_TIMEOUT: tuple[int, int] = (5, 15)


def bound_client(client: _T, timeout: tuple[int, int] = HTTP_TIMEOUT) -> _T:
    """Inject a finite timeout into an alpaca-py client's private Session.
    Every SDK request funnels through _session.request without a timeout
    kwarg, so a functools.partial can't collide (and if a future SDK passes
    one, call-time kwargs override the partial's). Private-attr poke by
    necessity — a unit test asserts it survives SDK upgrades."""
    client._session.request = functools.partial(
        client._session.request, timeout=timeout
    )
    return client

# Transient, self-healing network faults. A "connection reset by peer" (errno 54)
# mid-read surfaces as requests' ConnectionError wrapping urllib3's ProtocolError;
# a slow endpoint surfaces as a Timeout. None of these mean the request is bad —
# a quick retry almost always succeeds — so we swallow-and-retry rather than let a
# single blip fail a whole watchdog/decision tick.
_TRANSIENT_NET = (RequestsConnectionError, RequestsTimeout, ProtocolError)


def _retry_read(fn: Callable[[], _T], *, what: str, tries: int = 3,
                backoff_s: float = 0.5) -> _T:
    """Call `fn` (an IDEMPOTENT read) and retry on a transient network fault with a
    short linear backoff. Only reads go through here — never order submits, which
    aren't safe to blind-retry (a reset can drop the response AFTER the order was
    accepted, so a retry could double-submit). Re-raises the last error if every
    attempt fails, so real outages still surface."""
    last: Exception | None = None
    for attempt in range(1, tries + 1):
        try:
            return fn()
        except _TRANSIENT_NET as e:
            last = e
            if attempt < tries:
                log.warning(
                    "%s: transient network error (%s); retry %d/%d.",
                    what, e.__class__.__name__, attempt, tries - 1,
                )
                time.sleep(backoff_s * attempt)
    assert last is not None  # loop only exits early via return
    raise last


class AlpacaClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        paper = not cfg.is_live
        self.trading = bound_client(TradingClient(
            cfg.alpaca_api_key, cfg.alpaca_secret_key, paper=paper
        ))
        self.data = bound_client(StockHistoricalDataClient(
            cfg.alpaca_api_key, cfg.alpaca_secret_key
        ))
        log.info("Alpaca client ready (mode=%s).", cfg.mode.value)

    # -- read --------------------------------------------------------------- #
    def get_account(self) -> AccountSnapshot:
        """Account snapshot, validated for internal consistency.

        Alpaca occasionally serves a glitched account row whose equity ignores
        the held positions entirely (equity == cash while ~$96k of stock is
        held — seen 2026-07-07, where a single such read latched a false
        EQUITY FLOOR halt and fired a flatten). Equity is redundant with
        cash + position market values, so a poisoned read is detectable:
        re-read, and if the API keeps disagreeing with itself, rebuild equity
        from the parts that DO agree rather than hand the bad number to the
        risk layer / watchdog.

        A SEPARATE glitch (seen 2026-07-23): `last_equity` itself reads back 0
        even though equity/cash/positions all agree with each other. Nothing
        above catches this — `_equity_consistent` never looks at last_equity —
        so day_pl = equity - 0 = equity and day_pl_pct falls into the "no
        last_equity" 0.00% branch: the dashboard showed "Today's P&L
        +$94,026 (+0.00%)", claiming the WHOLE account balance as today's
        gain. Checked and healed independently of the equity check below."""
        snap = self._read_account_once()
        for attempt in (1, 2):
            if self._equity_consistent(snap) and self._last_equity_plausible(snap):
                return snap
            log.warning(
                "get_account: INCONSISTENT snapshot (equity $%.2f vs cash "
                "$%.2f + positions $%.2f; last_equity $%.2f) — re-reading "
                "(%d/2).", snap.equity, snap.cash,
                sum(p.market_value for p in snap.positions), snap.last_equity,
                attempt,
            )
            time.sleep(0.5 * attempt)
            snap = self._read_account_once()
        updates: dict = {}
        if not self._equity_consistent(snap):
            healed_equity = snap.cash + sum(p.market_value for p in snap.positions)
            log.error(
                "get_account: equity STILL inconsistent after re-reads "
                "(reported $%.2f); substituting cash+positions $%.2f.",
                snap.equity, healed_equity,
            )
            updates["equity"] = healed_equity
        if not self._last_equity_plausible(snap):
            current_equity = updates.get("equity", snap.equity)
            healed_last_equity = self._recover_last_equity(current_equity)
            log.error(
                "get_account: last_equity STILL implausible ($%.2f) after "
                "re-reads; substituting %s.", snap.last_equity,
                f"${healed_last_equity:,.2f} from the prior day's equity "
                "snapshot" if healed_last_equity != current_equity
                else "today's equity (day P/L reads as unknown, not a "
                "fabricated gain)",
            )
            updates["last_equity"] = healed_last_equity
        return snap.model_copy(update=updates) if updates else snap

    def _read_account_once(self) -> AccountSnapshot:
        a = _retry_read(self.trading.get_account, what="get_account")
        raw_positions = _retry_read(
            self.trading.get_all_positions, what="get_all_positions"
        )
        positions = [self._to_position(p) for p in raw_positions]
        return AccountSnapshot(
            equity=float(a.equity),
            last_equity=float(a.last_equity),
            cash=float(a.cash),
            buying_power=float(a.buying_power),
            positions=positions,
            pattern_day_trader=bool(getattr(a, "pattern_day_trader", False)),
            daytrade_count=int(getattr(a, "daytrade_count", 0) or 0),
        )

    @staticmethod
    def _equity_consistent(snap: AccountSnapshot) -> bool:
        """True when reported equity agrees with cash + position market values,
        within a tolerance for price drift between the two API calls. Trivially
        true with no positions (equity == cash by definition then, and there is
        no independent signal to check it against)."""
        if not snap.positions:
            return True
        expected = snap.cash + sum(p.market_value for p in snap.positions)
        denom = max(abs(expected), abs(snap.equity), 1.0)
        return abs(snap.equity - expected) / denom <= 0.03

    @staticmethod
    def _last_equity_plausible(snap: AccountSnapshot) -> bool:
        """Alpaca occasionally serves last_equity=0 (seen 2026-07-23) even when
        equity/cash/positions all agree with each other — a distinct glitch
        from the equity==cash one above. A real account's prior-close equity
        is never actually zero (paper accounts fund at $100k; a live account
        always carries a balance), so <=0 is unambiguously bad data."""
        return snap.last_equity > 0

    def _recover_last_equity(self, current_equity: float) -> float:
        """Best-effort fallback when Alpaca's own last_equity is glitched: our
        own daily equity snapshots (state/equity_history.jsonl, written by
        status.EquityHistory) already record real prior-day equity for
        exactly this kind of recovery. Falls back to CURRENT equity (day P/L
        then reads as unknown/0, not a fabricated gain) if no usable prior-day
        row exists — never raises."""
        try:
            from ..status import EquityHistory
            today = datetime.now(timezone.utc).date().isoformat()
            prior = [
                r for r in EquityHistory().all()
                if r.get("date") and r["date"] < today and r.get("equity") is not None
            ]
            if prior:
                return float(prior[-1]["equity"])
        except Exception as e:
            log.warning("Could not recover last_equity from equity history: %s", e)
        return current_equity

    def account_id(self) -> str:
        """Stable identifier for the connected Alpaca account. It changes if the
        account is recreated OR you switch paper<->live, so it's the fingerprint we
        use to detect an account change and reset stale local state. Empty string
        if unreadable (caller then skips the check rather than wiping anything)."""
        try:
            a = self.trading.get_account()
            return str(getattr(a, "account_number", "") or getattr(a, "id", "") or "")
        except Exception as e:
            log.warning("account_id() failed: %s", e)
            return ""

    def latest_price(self, symbol: str) -> float:
        try:
            req = StockLatestTradeRequest(symbol_or_symbols=symbol)
            trade = _retry_read(
                lambda: self.data.get_stock_latest_trade(req),
                what=f"latest_price({symbol})",
            )
            return float(trade[symbol].price)
        except Exception as e:
            log.warning("latest_price(%s) failed: %s", symbol, e)
            return 0.0

    def annualized_vol(self, symbol: str, lookback_days: int = 30) -> Optional[float]:
        """Realized annualized volatility from daily closes — used for
        vol-targeted position sizing. None if data is unavailable."""
        closes = self._daily_closes(symbol, lookback_days + 5)
        if len(closes) < 10:
            return None
        rets = [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes))]
        try:
            daily_sd = statistics.stdev(rets)
        except statistics.StatisticsError:
            return None
        return daily_sd * (252 ** 0.5)

    def period_return_pct(self, symbol: str, days: int) -> Optional[float]:
        """Simple price return over `days` calendar days — for benchmark compare."""
        closes = self._daily_closes(symbol, days + 5)
        if len(closes) < 2:
            return None
        return (closes[-1] / closes[0] - 1.0) * 100.0

    def is_market_open(self) -> bool:
        clock = _retry_read(self.trading.get_clock, what="get_clock")
        return bool(clock.is_open)

    def next_market_open(self) -> Optional[datetime]:
        """UTC datetime of the next session open, or None when the clock read
        fails (callers treat None as 'no open-time wake-up armed')."""
        try:
            clock = _retry_read(self.trading.get_clock, what="get_clock")
            nxt = clock.next_open
            if nxt is not None and nxt.tzinfo is None:
                nxt = nxt.replace(tzinfo=timezone.utc)
            return nxt
        except Exception as e:
            log.warning("next_market_open failed: %s", e)
            return None

    def next_market_close(self) -> Optional[datetime]:
        """UTC datetime of the current/next session close, or None on a failed
        clock read (callers treat None as 'unknown — don't fence'). Feeds the
        decision loop's close fence."""
        try:
            clock = _retry_read(self.trading.get_clock, what="get_clock")
            nxt = getattr(clock, "next_close", None)
            if nxt is not None and nxt.tzinfo is None:
                nxt = nxt.replace(tzinfo=timezone.utc)
            return nxt
        except Exception as e:
            log.warning("next_market_close failed: %s", e)
            return None

    def portfolio_basis(self) -> Optional[tuple[float, float]]:
        """(base_value, net_cashflows) since account inception, for true
        total-return math: total_return = equity - base_value - net_cashflows
        (so it backs out both the initial funding AND any later deposits/
        withdrawals — the honest "are we up or down" number). None if the
        portfolio-history call fails. Best-effort, never raises."""
        try:
            acct = self.trading.get_account()
            created = acct.created_at
            if not isinstance(created, datetime):
                created = datetime.fromisoformat(str(created))
            created = created.astimezone(timezone.utc)
            now = datetime.now(timezone.utc)
            # A brand-new account (created today, especially after the open) has no
            # completed 1D portfolio-history bar yet, and Alpaca 400s when start >
            # end. Skip quietly and return None — total return simply isn't
            # computable until there's a day of history, and forcing the call would
            # misreport the initial funding as profit. Not a failure; just too new.
            if now - created < timedelta(days=1):
                log.debug("portfolio_basis: account too new for 1D history; skipping.")
                return None
            req = GetPortfolioHistoryRequest(
                start=created, end=now, timeframe="1D",
            )
            hist = self.trading.get_portfolio_history(req)
            base = float(hist.base_value or 0.0)
            net_cf = 0.0
            for v in (hist.cashflow or {}).values():
                if isinstance(v, (list, tuple)):
                    net_cf += sum(float(x or 0) for x in v)
                else:
                    net_cf += float(v or 0)
            return base, net_cf
        except Exception as e:
            log.warning("portfolio_basis failed: %s", e)
            return None

    # -- write: generalized order ------------------------------------------ #
    def submit(self, order: OrderRequest) -> Optional[str]:
        """Place any equity order described by an OrderRequest. Returns order id."""
        if order.side is Action.BUY and not self.cfg.can_open_orders:
            log.warning("Buy blocked: new orders disabled (kill switch).")
            return None

        bracketed = (
            order.take_profit_price is not None
            or order.stop_loss_price is not None
        )
        if bracketed and order.is_fractional:
            # Alpaca rejects brackets on fractional — drop the legs, warn loudly.
            log.warning(
                "%s: fractional order can't carry bracket legs; submitting plain "
                "(watchdog enforces the stop).", order.symbol,
            )
            bracketed = False

        try:
            req = self._build_equity_request(order, bracketed)
            placed = self.trading.submit_order(req)
        except Exception as e:
            log.error("submit(%s) failed: %s", order.symbol, e)
            return None
        log.info(
            "%s %s %s qty=%s notional=%s lim=%s (order %s)",
            order.side.value.upper(), order.order_type.value, order.symbol,
            order.qty, order.notional, order.limit_price, placed.id,
        )
        return str(placed.id)

    def submit_ladder(
        self, symbol: str, side: Action, total_qty: float,
        low: float, high: float, rungs: int = 4, tif: TIF = TIF.GTC,
    ) -> list[str]:
        """Scale-in/out ladder: split total_qty across `rungs` limit orders evenly
        spaced over [low, high]. Alpaca has no native ladder; this is N limits."""
        if rungs < 1 or total_qty <= 0 or high < low:
            return []
        per = round(total_qty / rungs, 4)
        step = (high - low) / max(1, rungs - 1)
        ids: list[str] = []
        for i in range(rungs):
            price = round(low + step * i, 2)
            oid = self.submit(OrderRequest(
                symbol=symbol, side=side, order_type=OrderType.LIMIT,
                tif=tif, qty=per, limit_price=price,
            ))
            if oid:
                ids.append(oid)
        log.info("Ladder %s %s: %d rungs %.2f–%.2f", side.value, symbol, len(ids), low, high)
        return ids

    def submit_from_decision(self, decision: RiskDecision) -> BuySubmission:
        """Build a BUY from a risk-approved equity decision.

        Returns a BuySubmission carrying the qty/notional actually submitted —
        callers must record THOSE, not the decision's. When at least one WHOLE
        share is affordable we PREFER a whole-share BRACKET order so the
        stop/take-profit rest at the exchange (they survive a process crash /
        market close) and we drop any sub-share remainder. Below one share —
        only reachable on small
        accounts with fractional enabled — we submit a dollar-NOTIONAL order,
        which Alpaca will not let us bracket; its ONLY protection is the watchdog
        stop the caller must then register.

        Every long we open MUST carry a hard stop: without a positive
        stop_loss_pct a whole-share bracket has no stop leg and a fractional buy
        has nothing for the watchdog to enforce — i.e. a naked position. We refuse
        rather than open one. The irreducible residual on the fractional path is
        an OVERNIGHT / halt GAP that jumps the stop before the ~30s watchdog can
        market-sell (the market is closed): that risk is bounded but not
        removable, which is exactly why we prefer the exchange-resident bracket
        whenever a whole share is affordable (see 1B.5)."""
        if decision.verdict not in (RiskVerdict.APPROVED, RiskVerdict.RESIZED):
            return _NO_BUY
        symbol = decision.proposal.symbol
        if decision.stop_loss_pct <= 0:
            log.warning(
                "Skip %s: decision carries no stop-loss — refusing to open an "
                "unprotected position (whole-share bracket needs a stop leg; a "
                "fractional buy needs a watchdog stop).", symbol,
            )
            return _NO_BUY
        price = self.latest_price(symbol)
        if price <= 0:
            log.warning("Skip %s: no price for order/bracket levels.", symbol)
            return _NO_BUY

        whole = int(decision.approved_qty)
        if whole >= 1:
            dropped_qty = decision.approved_qty - whole
            dropped_usd = round(dropped_qty * price, 2)
            if dropped_usd >= 0.01:
                # Deliberate trade-off (see docstring): the exchange-resident
                # bracket wins over sizing precision — but never silently.
                log.warning(
                    "%s: floored %.6g sh -> %d for the exchange bracket; $%.2f "
                    "of the $%.2f allocation NOT deployed (stays cash; core "
                    "sweep / next cycle can redeploy).", symbol,
                    decision.approved_qty, whole, dropped_usd,
                    decision.approved_notional,
                )
            order = OrderRequest(
                symbol=symbol, side=Action.BUY, order_type=OrderType.MARKET,
                # GTC so the protective stop/take-profit legs REST at the exchange
                # across sessions (a DAY bracket's stop would expire at the close,
                # leaving the position unprotected into the overnight gap — exactly
                # when a gap-down needs it). The market entry fills immediately; the
                # OCO legs persist until hit or canceled (1B.5).
                tif=TIF.GTC,
                qty=float(whole),
                take_profit_price=round(price * (1 + decision.take_profit_pct / 100.0), 2),
                stop_loss_price=round(price * (1 - decision.stop_loss_pct / 100.0), 2),
            )
            return BuySubmission(
                self.submit(order), False, float(whole), round(whole * price, 2),
                dropped_notional=dropped_usd,
            )

        # Sub-share: fractional dollar-notional order, no exchange bracket.
        # Whole-shares mode (GA-2.3) must never reach here — the risk layer
        # floors/rejects upstream — but a decision built another way (or a
        # future refactor) must not slip an unbracketed buy through either.
        if self.cfg.risk.whole_shares_only:
            log.warning(
                "Skip %s: whole-shares mode — refusing sub-share fractional "
                "fallback (no exchange bracket).", symbol,
            )
            return _NO_BUY
        if not self.cfg.risk.fractional_enabled:
            log.warning("Skip %s: under one share and fractional disabled.", symbol)
            return _NO_BUY
        notional = round(decision.approved_notional, 2)
        if notional < self.cfg.risk.min_order_usd:
            log.warning("Skip %s: notional $%.2f below min order.", symbol, notional)
            return _NO_BUY
        order = OrderRequest(
            symbol=symbol, side=Action.BUY, order_type=OrderType.MARKET, notional=notional,
        )
        return BuySubmission(self.submit(order), True, notional / price, notional)

    def submit_notional_buy(self, symbol: str, notional: float) -> Optional[str]:
        """Plain dollar-notional MARKET buy (no bracket) — used by the core-ETF
        fill (Todo 1.6) to deploy idle cash into a broad index toward the target
        invested %. The core is a diversified holding managed at the account level
        (equity floor, emergency flatten, regime trim), so it deliberately carries
        no per-name stop; that's why it goes through this path, not
        submit_from_decision (which refuses a stop-less buy)."""
        notional = round(float(notional), 2)
        if notional < self.cfg.risk.min_order_usd:
            log.warning("Core fill %s: $%.2f below min order.", symbol, notional)
            return None
        return self.submit(OrderRequest(
            symbol=symbol, side=Action.BUY, order_type=OrderType.MARKET, notional=notional,
        ))

    # -- write: options (defined-risk) ------------------------------------- #
    #: Entry-side limit buffer: cap what we're willing to pay at the pre-trade
    #: estimated net premium plus this much headroom (percentage, floored by
    #: the $ minimum below) rather than sending a plain market order with no
    #: ceiling at all. Regression 2026-07-23: a long call estimated (mid-quote)
    #: at $0.01/share was sized to a $900 cap, but the market order filled at
    #: $0.03/share — 3x the estimate — turning an intended $900 debit into a
    #: real $2,700 one on a thin/illiquid book. A limit order that doesn't fill
    #: just means the position isn't opened this cycle (safe); an unbounded
    #: market fill is not.
    ENTRY_LIMIT_BUFFER_PCT = 20.0
    #: Floor buffer in $/share, so a sub-dime estimate (like $0.01) still gets
    #: real headroom instead of a percentage buffer that rounds to nothing at
    #: Alpaca's whole-cent option tick.
    ENTRY_LIMIT_MIN_BUFFER = 0.02

    def submit_option_legs(
        self, legs: list[OptionLegRequest], qty: int = 1,
        est_premium_per_share: float | None = None,
    ) -> Optional[str]:
        """Submit a single- or multi-leg options ENTRY order (DAY).

        Priced as a limit at the estimated net premium plus a buffer whenever
        `est_premium_per_share` is provided (the normal, expected path — see
        ENTRY_LIMIT_BUFFER_PCT above for why this replaced a plain market
        order). Falls back to a market order only when no estimate is
        available. Caller is responsible for building OCC-symbol legs that
        form a defined-risk play."""
        if not self.cfg.can_open_orders:
            log.warning("Option order blocked: new orders disabled (kill switch).")
            return None
        limit_price = None
        if est_premium_per_share is not None and est_premium_per_share > 0:
            buffer = max(
                est_premium_per_share * self.ENTRY_LIMIT_BUFFER_PCT / 100.0,
                self.ENTRY_LIMIT_MIN_BUFFER,
            )
            limit_price = round(est_premium_per_share + buffer, 2)
        try:
            if len(legs) == 1:
                # A 1-leg "MLEG" is rejected by the SDK (MLEG needs 2-4 legs)
                # and OrderClass.SIMPLE requires symbol+side on the request
                # itself — so a long call/put goes out on the OCC symbol
                # directly, carrying the leg's position intent.
                leg = legs[0]
                kwargs = dict(
                    symbol=leg.symbol, qty=qty * int(leg.ratio_qty or 1),
                    side=leg.side, time_in_force=TimeInForce.DAY,
                    position_intent=leg.position_intent,
                )
                req = (
                    LimitOrderRequest(limit_price=limit_price, **kwargs)
                    if limit_price is not None else MarketOrderRequest(**kwargs)
                )
            else:
                kwargs = dict(
                    qty=qty, time_in_force=TimeInForce.DAY,
                    order_class=OrderClass.MLEG, legs=legs,
                )
                req = (
                    LimitOrderRequest(limit_price=limit_price, **kwargs)
                    if limit_price is not None else MarketOrderRequest(**kwargs)
                )
            placed = self.trading.submit_order(req)
        except Exception as e:
            log.error("submit_option_legs failed: %s", e)
            return None
        log.info(
            "OPTION %d-leg order qty=%d limit=%s (order %s)",
            len(legs), qty, limit_price, placed.id,
        )
        return str(placed.id)

    #: Minimum valid option limit price (Alpaca ticks options in whole cents;
    #: a SELL-to-close resting at this price is still marketable against any
    #: live bid, however small, without asking for a literal $0.00 order).
    _MIN_OPTION_LIMIT = 0.01

    def close_option_leg(self, position: Position) -> Optional[str]:
        """Close ONE option leg: plain market close first (the common path),
        falling back to a DAY limit order when Alpaca rejects the market
        close for having no live quote to route against.

        Seen 2026-07-23: a long call whose mark had gone to $0 (no NBBO) got
        error 40310000 "order has been rejected due to no available quote
        for symbol. please reenter with a limit" on every watchdog tick — the
        SAME market close retried every ~30s for 12+ minutes with the
        position stuck unprotected, because there was no fallback order type.
        Options are DAY-only at Alpaca (no GTC), so DAY is the most
        persistent single order this venue allows; the watchdog's own retry
        loop still covers a DAY order that doesn't fill before this order
        expires unfilled at the close."""
        oid = self.close_position(position.symbol)
        if oid:
            return oid
        side = OrderSide.SELL if position.qty > 0 else OrderSide.BUY
        # current_price==0 is itself a meaningful reading here (Alpaca marked
        # this worthless / no bid) — NOT "missing data" to fall back from.
        # avg_entry_price is what we PAID, unrelated to what it's worth now;
        # using it would ask 3x+ the going rate for a dead contract and likely
        # never fill. Floor at the min tick only when the live mark is <= 0.
        limit = max(round(position.current_price, 2), self._MIN_OPTION_LIMIT)
        try:
            order = self.trading.submit_order(LimitOrderRequest(
                symbol=position.symbol, qty=abs(position.qty), side=side,
                time_in_force=TimeInForce.DAY, limit_price=limit,
            ))
        except Exception as e:
            log.error(
                "close_option_leg(%s) limit fallback failed: %s",
                position.symbol, e,
            )
            return None
        log.warning(
            "Option leg %s: market close unavailable — resting DAY limit "
            "%s %g @ %.2f instead.", position.symbol, side.value,
            abs(position.qty), limit,
        )
        return str(order.id)

    # Alpaca hard-caps the mleg order class at 4 legs — a 5th leg fails
    # request validation before it ever reaches the venue (AMZN 2026-08-07).
    _MLEG_MAX_LEGS = 4

    def close_option_group(self, positions: list[Position]) -> Optional[str]:
        """Close a whole option structure — never gated by the kill switch
        (closing is risk reduction). One leg -> close_option_leg (market, then
        a DAY-limit fallback — see its docstring); 2-4 legs -> ONE closing MLEG
        market order (each leg flipped to its *_TO_CLOSE intent) so a spread
        never passes through a naked-short intermediate state. 5+ legs cannot
        go as one order (Alpaca's 4-leg MLEG cap; groups get that big when two
        structures share an underlying+expiry) — they split into risk-safe
        chunks (each short atomically paired with its cover, singles for the
        rest) submitted as several orders. Returns an order id only when EVERY
        chunk went through; on a partial failure it returns None so the
        watchdog stays CRITICAL and retries — legs whose close did fill drop
        out of the group by the next tick, shrinking the retry. The MLEG path
        has no limit-order fallback yet (a net limit across legs needs a
        per-leg quote, which is exactly what's missing when this fires) — a
        failed multi-leg close is retried by the watchdog next tick same as
        before."""
        from .options import split_option_close_chunks
        if len(positions) == 1:
            return self.close_option_leg(positions[0])
        if len(positions) <= self._MLEG_MAX_LEGS:
            return self._submit_mleg_close(positions)
        chunks = split_option_close_chunks(positions)
        log.warning(
            "Option group %s has %d legs — over the %d-leg MLEG cap; closing "
            "as %d risk-safe chunk(s).",
            ",".join(p.symbol for p in positions), len(positions),
            self._MLEG_MAX_LEGS, len(chunks),
        )
        first: Optional[str] = None
        all_ok = True
        for chunk in chunks:
            oid = (
                self.close_option_leg(chunk[0]) if len(chunk) == 1
                else self._submit_mleg_close(chunk)
            )
            if oid is None:
                all_ok = False
            elif first is None:
                first = oid
        return first if all_ok else None

    def _submit_mleg_close(self, positions: list[Position]) -> Optional[str]:
        """Submit ONE closing MLEG market order for <=4 legs."""
        from .options import build_closing_legs
        try:
            legs, group_qty = build_closing_legs(positions)
            req = MarketOrderRequest(
                qty=group_qty, time_in_force=TimeInForce.DAY,
                order_class=OrderClass.MLEG, legs=legs,
            )
            placed = self.trading.submit_order(req)
        except Exception as e:
            log.error(
                "close_option_group(%s) failed: %s",
                ",".join(p.symbol for p in positions), e,
            )
            return None
        log.info(
            "OPTION close %d-leg qty=%d (order %s)", len(legs), group_qty, placed.id,
        )
        return str(placed.id)

    # -- partial reduce (never gated — risk reduction) --------------------- #
    def reduce_position(self, symbol: str, qty: float) -> Optional[str]:
        """Market-SELL `qty` shares (whole or fractional) of an existing long — a
        PARTIAL close used by the regime-off trim (1B.6) and the scale-out
        take-profit (1B.8). Selling is never gated by the kill switch (reducing
        risk must never be blocked). Caller should cancel any resting bracket for
        the symbol first, since its protective legs reserve the shares."""
        if qty is None or qty <= 0:
            return None
        try:
            order = self.trading.submit_order(MarketOrderRequest(
                symbol=symbol, qty=qty, side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
            ))
        except Exception as e:
            log.error("reduce_position(%s, %g) failed: %s", symbol, qty, e)
            return None
        log.info("REDUCE %s qty=%g (order %s)", symbol, qty, order.id)
        return str(order.id)

    def open_position(self, symbol: str) -> Optional[Position]:
        """Fresh single-position read straight from the broker. Close paths
        branch on qty_available, and the cycle-start snapshot can be minutes
        old by the time a decision sell executes (LLM round-trip) — brackets
        placed or replaced in between change what's reserved. None => flat
        (or unreadable this instant; callers treat both as nothing-to-close
        and let the next cycle re-decide)."""
        try:
            return self._to_position(self.trading.get_open_position(symbol))
        except Exception as e:
            if "position does not exist" not in str(e).lower():
                log.warning("open_position(%s) failed: %s", symbol, e)
            return None

    # -- closing (never gated) --------------------------------------------- #
    def close_position(self, symbol: str) -> Optional[str]:
        try:
            order = self.trading.close_position(symbol)
            log.info("CLOSE %s (order %s)", symbol, order.id)
            return str(order.id)
        except Exception as e:
            log.error("close_position(%s) failed: %s", symbol, e)
            return None

    #: How far THROUGH the last price a fallback exit limit is set. Aggressive
    #: enough to fill on the next print (marketable), capped so a closed/halted
    #: market can't fill us at a catastrophic gap.
    EXIT_LIMIT_BUFFER_PCT = 2.0

    def close_position_marketable_limit(
        self, symbol: str, qty: float, ref_price: float,
    ) -> Optional[str]:
        """Fallback exit when a plain MARKET close can't fill — e.g. the market is
        closed or the name is LULD-halted. Rests a GTC SELL LIMIT priced through
        the last trade (marketable), so it fills on the next print / at the reopen
        instead of leaving the position with no working exit. Whole-share only:
        Alpaca rejects GTC/limit on fractional qty, so a sub-share position's
        overnight-gap risk stays irreducible (see 1B.2)."""
        if qty <= 0 or ref_price <= 0:
            return None
        if qty != int(qty):
            log.warning(
                "%s: marketable-limit fallback needs whole shares (qty=%g is "
                "fractional; GTC/limit not allowed) — cannot rest a fallback exit.",
                symbol, qty,
            )
            return None
        limit = round(ref_price * (1 - self.EXIT_LIMIT_BUFFER_PCT / 100.0), 2)
        try:
            order = self.trading.submit_order(LimitOrderRequest(
                symbol=symbol, qty=float(int(qty)), side=OrderSide.SELL,
                time_in_force=TimeInForce.GTC, limit_price=limit,
            ))
        except Exception as e:
            log.error("close_position_marketable_limit(%s) failed: %s", symbol, e)
            return None
        log.warning(
            "REST fallback exit for %s: GTC sell-limit %g @ %.2f (market close "
            "unavailable).", symbol, qty, limit,
        )
        return str(order.id)

    @staticmethod
    def _order_status(o) -> str:
        s = getattr(o, "status", "")
        return str(getattr(s, "value", s)).lower()

    def cancel_open_orders_for(self, symbol: str) -> None:
        for o in self.trading.get_orders():
            if o.symbol != symbol:
                continue
            if self._order_status(o) == "pending_cancel":
                # A cancel is already in flight; re-sending one only errors
                # (42210000 "order pending cancel") and spams the log.
                continue
            try:
                self.trading.cancel_order_by_id(o.id)
            except Exception as e:
                log.warning("cancel order %s failed: %s", o.id, e)

    #: Order states Alpaca refuses to replace (documented for PATCH /v2/orders).
    #: An order wedged in pending_cancel can be neither canceled (42210000) nor
    #: replaced — only the venue-side cancel finally settling frees its shares.
    _UNREPLACEABLE = frozenset(
        {"accepted", "pending_new", "pending_cancel", "pending_replace"}
    )

    def clear_orders_for_exit(
        self, symbol: str, ref_price: float,
    ) -> list[tuple[str, float, str, float]]:
        """Make every open order for `symbol` either BE the exit or go away,
        ahead of a liquidation whose shares they reserve.

        Open SELL orders in a replaceable state get re-priced through the
        market so they execute like a market order — by order type, because a
        replace can never change the type: limit sells get their limit moved
        down; stop / stop-limit sells get their trigger lifted ABOVE the last
        trade so they fire on the next print (PATCHing a stop-market leg with
        limit_price is refused — 42210000 "market orders must not have
        limit_price"; seen 2026-07-08 when AVAV's bracket stop held all 37
        shares). Replacing is atomic at the venue, so unlike cancel-then-resell
        it can never strand the shares: a cancel that hangs in `pending_cancel`
        (seen with paper bracket legs) keeps them reserved indefinitely — new
        sells get 40310000, re-cancels get 42210000, and replaces are refused
        too. Everything else (buys, sells that can't be replaced) gets a
        cancel, except orders already pending_cancel where re-sending only
        errors.

        Returns (new_order_id, unfilled_qty, old_order_id, old_filled_qty) per
        replaced sell so the caller can ledger those orders as the exit they
        now are — and, when the SAME exit gets re-replaced on a later tick
        (price fell, the old marketable limit went stale), void the SELL it
        already ledgered for old_order_id instead of double-counting.

        A `held` sell is the parked OCO sibling of a live bracket leg (Alpaca
        rests the stop as `held` while the take-profit works) and is left
        entirely alone: replacing it would return a SECOND full-qty exit for
        the same shares (the caller would ledger the position sold twice), and
        canceling it cancels every remaining order in the OCO group — including
        the live leg this pass just made marketable, leaving the position with
        no exit at all. The venue cancels the held leg itself when its sibling
        fills. (The default non-nested open-orders query currently hides held
        legs, so this guard is armed for the day one shows up — e.g. an API
        change or a nested query.)"""
        limit = (
            round(ref_price * (1 - self.EXIT_LIMIT_BUFFER_PCT / 100.0), 2)
            if ref_price > 0 else 0.0
        )
        trigger = round(ref_price * (1 + self.EXIT_LIMIT_BUFFER_PCT / 100.0), 2)
        replaced: list[tuple[str, float, str, float]] = []
        for o in self.trading.get_orders():
            if o.symbol != symbol:
                continue
            status = self._order_status(o)
            is_sell = str(getattr(o, "side", "")).lower().endswith("sell")
            if is_sell and status == "held":
                continue  # parked OCO sibling — see docstring; never touch it
            if is_sell and limit > 0 and status not in self._UNREPLACEABLE:
                otype = getattr(o, "order_type", None) or getattr(o, "type", "")
                kind = str(getattr(otype, "value", otype)).lower()
                cur_limit = float(getattr(o, "limit_price", None) or 0)
                cur_stop = float(getattr(o, "stop_price", None) or 0)
                # Marketable = priced to fill on the next print (at/below the
                # last trade). Comparing against the BUFFERED price instead
                # re-replaced a still-marketable limit every ~30s tick on a
                # falling tape (PLTR 2026-07-15: 4 exit lots in 93s, each
                # replacement resetting the paper-sim queue and delaying the
                # fill). Fresh replacements below still price 2% through.
                limit_marketable = 0 < cur_limit <= ref_price
                stop_firing = cur_stop >= ref_price
                if "stop" in kind:
                    if stop_firing and ("limit" not in kind or limit_marketable):
                        continue  # fires on the next print (e.g. replaced last tick) — it IS the exit
                    req = (
                        ReplaceOrderRequest(stop_price=trigger, limit_price=limit)
                        if "limit" in kind
                        else ReplaceOrderRequest(stop_price=trigger)
                    )
                    new_px = trigger
                elif kind == "market":
                    continue  # a live market sell already IS the exit; leave it
                else:  # limit — or unknown type, where a limit replace is the safe default
                    if limit_marketable:
                        continue  # already marketable (e.g. replaced last tick) — it IS the exit
                    req, new_px = ReplaceOrderRequest(limit_price=limit), limit
                try:
                    new = self.trading.replace_order_by_id(o.id, req)
                    old_filled = float(getattr(o, "filled_qty", 0) or 0)
                    left = float(o.qty or 0) - old_filled
                    replaced.append((str(new.id), left, str(o.id), old_filled))
                    log.warning(
                        "Exit via resting sell %s (%s): replaced %s -> %.2f "
                        "(marketable; new order %s).", o.id, symbol,
                        "stop trigger" if "stop" in kind else "limit", new_px, new.id,
                    )
                    continue
                except Exception as e:
                    log.warning(
                        "replace sell %s failed (%s); falling back to cancel.",
                        o.id, e,
                    )
            if status == "pending_cancel":
                continue  # re-sending a cancel only errors (42210000)
            try:
                self.trading.cancel_order_by_id(o.id)
            except Exception as e:
                log.warning("cancel order %s failed: %s", o.id, e)
        return replaced

    def has_working_exit(self, symbol: str, ref_price: float) -> bool:
        """True if an open SELL order for `symbol` is already priced to fill on
        the next print — a marketable limit (limit at/through the last trade), a
        stop whose trigger is at/through it, or a live market sell. Uses the SAME
        marketability test as clear_orders_for_exit (keep them in sync), so a leg
        that method left in place because it IS the exit reads as protection
        here. This lets the watchdog tell a position whose reserved shares are
        covered by in-flight marketable exits (protected — the sells just haven't
        filled yet) from one whose only sells are wedged in pending_cancel, or
        which has none at all (genuinely unprotected — page a human). A
        pending_cancel leg reserves shares but will never fill, so it does NOT
        count; neither does a `held` OCO sibling — it cannot execute while its
        live leg works, so only the live leg is real protection.
        Best-effort: False on error, so the caller errs toward paging."""
        if ref_price <= 0:
            return False
        try:
            for o in self.trading.get_orders():
                if o.symbol != symbol:
                    continue
                if not str(getattr(o, "side", "")).lower().endswith("sell"):
                    continue
                if self._order_status(o) in ("pending_cancel", "held"):
                    continue  # wedged cancel / parked OCO sibling — can't fill as-is
                otype = getattr(o, "order_type", None) or getattr(o, "type", "")
                kind = str(getattr(otype, "value", otype)).lower()
                cur_limit = float(getattr(o, "limit_price", None) or 0)
                cur_stop = float(getattr(o, "stop_price", None) or 0)
                if kind == "market":
                    return True  # a live market sell is already the exit
                # Same marketability test as clear_orders_for_exit (see the
                # comment there): at/below the LAST TRADE, not the buffered
                # price, or a leg that method just left in place reads as
                # unprotected here.
                marketable_limit = 0 < cur_limit <= ref_price
                if "stop" in kind:
                    if cur_stop >= ref_price and ("limit" not in kind or marketable_limit):
                        return True  # trigger fires on the next print
                elif marketable_limit:
                    return True
        except Exception as e:
            log.warning("has_working_exit(%s) failed: %s", symbol, e)
        return False

    def cancel_order(self, order_id: str) -> bool:
        """Cancel one order by id. False (logged) on failure."""
        try:
            self.trading.cancel_order_by_id(order_id)
            return True
        except Exception as e:
            log.warning("cancel order %s failed: %s", order_id, e)
            return False

    def open_stop_sells(self, symbol: str) -> list[dict]:
        """OPEN stop-type SELL orders for `symbol` as (id, qty, stop_price)
        dicts — how the core-stop maintainer (GA-2.3) sees the protection that
        is ALREADY resting at the exchange before deciding to replace it.
        Best-effort: [] on failure (caller then leaves the resting stop alone
        rather than risking a cancel with no replacement)."""
        out: list[dict] = []
        try:
            for o in self.trading.get_orders():
                if o.symbol != symbol:
                    continue
                if not str(getattr(o, "side", "")).lower().endswith("sell"):
                    continue
                otype = getattr(o, "order_type", None) or getattr(o, "type", "")
                if "stop" not in str(getattr(otype, "value", otype)).lower():
                    continue
                out.append({
                    "id": str(o.id),
                    "qty": float(getattr(o, "qty", 0) or 0),
                    "stop_price": float(getattr(o, "stop_price", 0) or 0),
                })
        except Exception as e:
            log.warning("open_stop_sells(%s) failed: %s", symbol, e)
        return out

    def order_fill(self, order_id: str) -> tuple[str, float, float]:
        """(status, filled_qty, qty) for an order — for post-hoc fill reconciliation.
        Returns ('unknown', 0, 0) if the order can't be fetched."""
        try:
            o = self.trading.get_order_by_id(order_id)
            status = getattr(o, "status", "")
            # OrderStatus enum stringifies to "OrderStatus.FILLED"; callers
            # compare against plain values like "filled" — use .value.
            status = getattr(status, "value", status)
            return (
                str(status).lower(),
                float(getattr(o, "filled_qty", 0) or 0),
                float(getattr(o, "qty", 0) or 0),
            )
        except Exception as e:
            log.warning("order_fill(%s) failed: %s", order_id, e)
            return ("unknown", 0.0, 0.0)

    def order_fill_detail(self, order_id: str) -> dict:
        """Broker-reported fill facts for a FILLED order (run-6 item 1e):
        {'price': filled_avg_price, 'qty': filled_qty, 'filled_at': datetime|None}.
        Read-only; {} when the order can't be fetched."""
        try:
            o = self.trading.get_order_by_id(order_id)
            return {
                "price": float(getattr(o, "filled_avg_price", 0) or 0),
                "qty": float(getattr(o, "filled_qty", 0) or 0),
                "filled_at": getattr(o, "filled_at", None),
            }
        except Exception as e:
            log.warning("order_fill_detail(%s) failed: %s", order_id, e)
            return {}

    def closed_sell_orders(self, limit: int = 500) -> list[dict]:
        """Recently CLOSED (terminal-state) SELL orders from the broker, newest
        first, as plain dicts — the raw material for the exchange-exit backfill
        (F.1). This is how exits the process never issued become visible: a
        bracket's stop/take leg filling at the exchange, or a manual sell in the
        Alpaca UI, happens with no code running. Only FILLED orders are
        returned (canceled/expired legs realized nothing). Read-only,
        best-effort: [] on failure."""
        try:
            req = GetOrdersRequest(
                status=QueryOrderStatus.CLOSED, side=OrderSide.SELL, limit=limit,
            )
            orders = _retry_read(
                lambda: self.trading.get_orders(filter=req),
                what="closed_sell_orders",
            )
        except Exception as e:
            log.warning("closed_sell_orders failed: %s", e)
            return []
        out: list[dict] = []
        for o in orders:
            status = str(getattr(o.status, "value", o.status) or "").lower()
            if status != "filled":
                continue
            otype = getattr(o, "order_type", None) or getattr(o, "type", "")
            filled_at = getattr(o, "filled_at", None)
            out.append({
                "order_id": str(o.id),
                "symbol": str(o.symbol),
                "qty": float(getattr(o, "filled_qty", 0) or 0),
                "price": float(getattr(o, "filled_avg_price", 0) or 0),
                "type": str(getattr(otype, "value", otype) or "").lower(),
                "filled_at": filled_at.isoformat() if filled_at else "",
            })
        return out

    def open_buy_notional(self, symbol: str) -> float:
        """$ value of OPEN (unfilled) BUY orders for `symbol`. The risk layer
        counts this against the per-symbol exposure cap so repeated decision
        cycles can't stack duplicate buys before the first one fills."""
        try:
            price = self.latest_price(symbol)
            total = 0.0
            for o in self.trading.get_orders():
                if o.symbol != symbol:
                    continue
                if not str(getattr(o, "side", "")).lower().endswith("buy"):
                    continue
                # Prefer explicit notional, else remaining qty * (limit or last).
                notional = getattr(o, "notional", None)
                if notional:
                    total += float(notional)
                    continue
                qty = float(getattr(o, "qty", 0) or 0)
                filled = float(getattr(o, "filled_qty", 0) or 0)
                remaining = max(0.0, qty - filled)
                ref = float(getattr(o, "limit_price", 0) or 0) or price
                total += remaining * ref
            return total
        except Exception as e:
            log.warning("open_buy_notional(%s) failed: %s", symbol, e)
            return 0.0

    # -- builders / mapping ------------------------------------------------- #
    def _build_equity_request(self, o: OrderRequest, bracketed: bool):
        common = dict(
            symbol=o.symbol, side=_SIDE[o.side], time_in_force=_TIF[o.tif],
        )
        if o.notional is not None:
            common["notional"] = o.notional
        else:
            common["qty"] = o.qty

        if bracketed:
            common["order_class"] = OrderClass.BRACKET
            if o.take_profit_price is not None:
                common["take_profit"] = TakeProfitRequest(limit_price=o.take_profit_price)
            if o.stop_loss_price is not None:
                common["stop_loss"] = StopLossRequest(stop_price=o.stop_loss_price)

        if o.order_type is OrderType.MARKET:
            return MarketOrderRequest(**common)
        if o.order_type is OrderType.LIMIT:
            return LimitOrderRequest(limit_price=o.limit_price, **common)
        if o.order_type is OrderType.STOP:
            return StopOrderRequest(stop_price=o.stop_price, **common)
        # stop_limit
        return LimitOrderRequest(
            limit_price=o.limit_price, stop_price=o.stop_price, **common
        )

    def daily_close_series(self, symbol: str, days: int) -> list[tuple[str, float]]:
        """(ISO-date, close) pairs for the last `days` trading days — the dated
        variant of _daily_closes. The dates are what lets the backtest glue align
        multi-symbol histories and map ledger timestamps to bar indexes (D.1)."""
        try:
            req = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame.Day,
                start=datetime.now(timezone.utc) - timedelta(days=days * 2),
            )
            resp = _retry_read(
                lambda: self.data.get_stock_bars(req),
                what=f"daily_close_series({symbol})",
            )
            bars = resp.data.get(symbol, [])
            return [
                (b.timestamp.date().isoformat(), float(b.close)) for b in bars
            ][-days:]
        except Exception as e:
            log.warning("daily_close_series(%s) failed: %s", symbol, e)
            return []

    _option_data = None  # lazily built OptionHistoricalDataClient

    def option_close_series(self, symbol: str, days: int) -> list[tuple[str, float]]:
        """(ISO-date, close) pairs of DAILY option bars for one OCC contract —
        the option twin of daily_close_series, so the nightly post-mortem can
        mark open option groups close-to-close (run-6 item 1a). Per-share
        prices (x100 per contract). [] on any failure or when the contract
        has no daily bars (thin names print no bar on a no-trade day)."""
        try:
            if self._option_data is None:
                from alpaca.data.historical.option import OptionHistoricalDataClient
                self._option_data = bound_client(OptionHistoricalDataClient(
                    self.cfg.alpaca_api_key, self.cfg.alpaca_secret_key,
                ))
            from alpaca.data.requests import OptionBarsRequest
            req = OptionBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame.Day,
                start=datetime.now(timezone.utc) - timedelta(days=days * 2),
            )
            resp = _retry_read(
                lambda: self._option_data.get_option_bars(req),
                what=f"option_close_series({symbol})",
            )
            bars = resp.data.get(symbol, [])
            return [
                (b.timestamp.date().isoformat(), float(b.close)) for b in bars
            ][-days:]
        except Exception as e:
            log.warning("option_close_series(%s) failed: %s", symbol, e)
            return []

    def _daily_closes(self, symbol: str, days: int) -> list[float]:
        try:
            req = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame.Day,
                start=datetime.now(timezone.utc) - timedelta(days=days * 2),
            )
            resp = _retry_read(
                lambda: self.data.get_stock_bars(req), what=f"_daily_closes({symbol})"
            )
            bars = resp.data.get(symbol, [])
            return [float(b.close) for b in bars][-days:]
        except Exception as e:
            log.warning("_daily_closes(%s) failed: %s", symbol, e)
            return []

    @staticmethod
    def _to_position(p) -> Position:
        qty = float(p.qty)
        # alpaca-py calls this `qty_available` (= qty minus shares reserved by
        # open orders). Missing/None -> assume all of it is sellable.
        avail_raw = getattr(p, "qty_available", None)
        qty_available = float(avail_raw) if avail_raw is not None else qty
        ac_raw = getattr(p, "asset_class", None)
        asset_class = str(getattr(ac_raw, "value", ac_raw) or "us_equity")
        return Position(
            symbol=p.symbol,
            qty=qty,
            qty_available=qty_available,
            asset_class=asset_class,
            avg_entry_price=float(p.avg_entry_price),
            current_price=float(p.current_price or 0),
            market_value=float(p.market_value or 0),
            unrealized_pl=float(p.unrealized_pl or 0),
            unrealized_pl_pct=float(p.unrealized_plpc or 0) * 100.0,
        )
