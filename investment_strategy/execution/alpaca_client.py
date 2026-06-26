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

import logging
import statistics
from datetime import datetime, timedelta, timezone
from typing import Optional

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
from alpaca.trading.requests import (
    LimitOrderRequest,
    MarketOrderRequest,
    OptionLegRequest,
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


class AlpacaClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        paper = not cfg.is_live
        self.trading = TradingClient(
            cfg.alpaca_api_key, cfg.alpaca_secret_key, paper=paper
        )
        self.data = StockHistoricalDataClient(
            cfg.alpaca_api_key, cfg.alpaca_secret_key
        )
        log.info("Alpaca client ready (mode=%s).", cfg.mode.value)

    # -- read --------------------------------------------------------------- #
    def get_account(self) -> AccountSnapshot:
        a = self.trading.get_account()
        positions = [self._to_position(p) for p in self.trading.get_all_positions()]
        return AccountSnapshot(
            equity=float(a.equity),
            last_equity=float(a.last_equity),
            cash=float(a.cash),
            buying_power=float(a.buying_power),
            positions=positions,
        )

    def latest_price(self, symbol: str) -> float:
        try:
            req = StockLatestTradeRequest(symbol_or_symbols=symbol)
            return float(self.data.get_stock_latest_trade(req)[symbol].price)
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
        return bool(self.trading.get_clock().is_open)

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

    def submit_from_decision(self, decision: RiskDecision) -> Optional[str]:
        """Convenience: build a bracket BUY from a risk-approved equity decision."""
        if decision.verdict not in (RiskVerdict.APPROVED, RiskVerdict.RESIZED):
            return None
        symbol = decision.proposal.symbol
        price = self.latest_price(symbol)
        if price <= 0:
            log.warning("Skip %s: no price for bracket levels.", symbol)
            return None
        order = OrderRequest(
            symbol=symbol, side=Action.BUY, order_type=OrderType.MARKET,
            qty=decision.approved_qty,
            take_profit_price=round(price * (1 + decision.take_profit_pct / 100.0), 2),
            stop_loss_price=round(price * (1 - decision.stop_loss_pct / 100.0), 2),
        )
        return self.submit(order)

    # -- write: options (defined-risk) ------------------------------------- #
    def submit_option_legs(
        self, legs: list[OptionLegRequest], qty: int = 1,
    ) -> Optional[str]:
        """Submit a single- or multi-leg options order (market, DAY). Caller is
        responsible for building OCC-symbol legs that form a defined-risk play."""
        if not self.cfg.can_open_orders:
            log.warning("Option order blocked: new orders disabled (kill switch).")
            return None
        order_class = OrderClass.MLEG if len(legs) > 1 else OrderClass.SIMPLE
        try:
            req = MarketOrderRequest(
                qty=qty, time_in_force=TimeInForce.DAY,
                order_class=order_class, legs=legs,
            )
            placed = self.trading.submit_order(req)
        except Exception as e:
            log.error("submit_option_legs failed: %s", e)
            return None
        log.info("OPTION %d-leg order qty=%d (order %s)", len(legs), qty, placed.id)
        return str(placed.id)

    # -- closing (never gated) --------------------------------------------- #
    def close_position(self, symbol: str) -> Optional[str]:
        try:
            order = self.trading.close_position(symbol)
            log.info("CLOSE %s (order %s)", symbol, order.id)
            return str(order.id)
        except Exception as e:
            log.error("close_position(%s) failed: %s", symbol, e)
            return None

    def cancel_open_orders_for(self, symbol: str) -> None:
        for o in self.trading.get_orders():
            if o.symbol == symbol:
                try:
                    self.trading.cancel_order_by_id(o.id)
                except Exception as e:
                    log.warning("cancel order %s failed: %s", o.id, e)

    def order_fill(self, order_id: str) -> tuple[str, float, float]:
        """(status, filled_qty, qty) for an order — for post-hoc fill reconciliation.
        Returns ('unknown', 0, 0) if the order can't be fetched."""
        try:
            o = self.trading.get_order_by_id(order_id)
            return (
                str(getattr(o, "status", "")),
                float(getattr(o, "filled_qty", 0) or 0),
                float(getattr(o, "qty", 0) or 0),
            )
        except Exception as e:
            log.warning("order_fill(%s) failed: %s", order_id, e)
            return ("unknown", 0.0, 0.0)

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

    def _daily_closes(self, symbol: str, days: int) -> list[float]:
        try:
            req = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame.Day,
                start=datetime.now(timezone.utc) - timedelta(days=days * 2),
            )
            bars = self.data.get_stock_bars(req).data.get(symbol, [])
            return [float(b.close) for b in bars][-days:]
        except Exception as e:
            log.warning("_daily_closes(%s) failed: %s", symbol, e)
            return []

    @staticmethod
    def _to_position(p) -> Position:
        return Position(
            symbol=p.symbol,
            qty=float(p.qty),
            avg_entry_price=float(p.avg_entry_price),
            current_price=float(p.current_price or 0),
            market_value=float(p.market_value or 0),
            unrealized_pl=float(p.unrealized_pl or 0),
            unrealized_pl_pct=float(p.unrealized_plpc or 0) * 100.0,
        )
