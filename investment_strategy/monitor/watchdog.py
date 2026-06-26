"""Position watchdog — the always-on safety loop.

Alpaca's bracket orders already hold a stop-loss and take-profit per position at
the exchange, so this loop is defense-in-depth, not the only line of defense. It
adds three things brackets can't:

  1. Account-wide emergency flatten when the day's loss breaches the hard limit.
  2. A hard EQUITY FLOOR: below it we flatten everything and LATCH a halt that
     does not auto-resume — the last guard against trading a dying account.
  3. A ratcheting trailing stop that locks in gains as a position runs up.

It runs on a short interval (default 30s). Closing positions is always allowed —
even under the kill switch — because reducing risk must never be blocked. Peak
equity and per-position high-water marks are persisted (see state.PortfolioState)
so a restart doesn't silently reset all protection to "all clear".
"""
from __future__ import annotations

import logging

from ..config import Config
from ..execution import AlpacaClient
from ..models import AccountSnapshot, Position
from ..state import PortfolioState

log = logging.getLogger("watchdog")


class Watchdog:
    def __init__(self, cfg: Config, broker: AlpacaClient, state: PortfolioState | None = None):
        self.cfg = cfg
        self.broker = broker
        self.state = state or PortfolioState(cfg.state_file)
        #: give back this much of peak gain before trailing-stopping out.
        self.trail_giveback_pct = 3.0

    def check_once(self) -> None:
        """One pass. Call on a timer from the orchestrator/monitor loop."""
        account = self.broker.get_account()
        self.state.update_equity(account.equity)  # ratchet the all-time high

        if self._equity_floor_breached(account):
            return  # latched halt + flatten; nothing else to do
        if self._emergency_flatten(account):
            return  # everything is being closed; nothing else to do

        live = {p.symbol for p in account.positions}
        for pos in account.positions:
            self._update_trailing_stop(pos)
        # Drop trailing state for positions that are gone (filled stop/tp/sell).
        for sym in list(self.state.high_water):
            if sym not in live:
                self.state.forget_symbol(sym)

    # -- hard equity floor (latched) --------------------------------------- #
    def _equity_floor_breached(self, account: AccountSnapshot) -> bool:
        floor = self.cfg.risk.equity_floor_usd
        if floor <= 0 or account.equity > floor:
            return False
        self.state.latch_halt(
            f"Equity ${account.equity:,.0f} <= floor ${floor:,.0f}. "
            f"Flattened and halted; clear {self.cfg.state_file} to resume."
        )
        self._flatten_all(account, "EQUITY FLOOR")
        return True

    # -- account-wide emergency exit --------------------------------------- #
    def _emergency_flatten(self, account: AccountSnapshot) -> bool:
        loss_pct = -account.day_pl_pct
        if loss_pct < self.cfg.risk.max_daily_loss_pct:
            return False
        log.error(
            "EMERGENCY: day loss %.2f%% >= limit %.2f%%. Flattening all positions.",
            loss_pct, self.cfg.risk.max_daily_loss_pct,
        )
        self._flatten_all(account, "DAILY LOSS")
        return True

    def _flatten_all(self, account: AccountSnapshot, why: str) -> None:
        """Close every position. Only forget trailing state on CONFIRMED close;
        a failed close is left tracked and loudly flagged so the next tick retries
        instead of silently leaving a naked, unmonitored position."""
        for pos in account.positions:
            self.broker.cancel_open_orders_for(pos.symbol)
            oid = self.broker.close_position(pos.symbol)
            if oid:
                self.state.forget_symbol(pos.symbol)
            else:
                log.critical(
                    "%s: close FAILED for %s — position may be NAKED. Will retry.",
                    why, pos.symbol,
                )

    # -- per-position trailing stop ---------------------------------------- #
    def _update_trailing_stop(self, pos: Position) -> None:
        peak = max(self.state.get_high_water(pos.symbol), pos.unrealized_pl_pct)
        self.state.set_high_water(pos.symbol, peak)

        # Only trail once a position has shown a real gain to protect.
        if peak <= self.trail_giveback_pct:
            return
        if pos.unrealized_pl_pct <= peak - self.trail_giveback_pct:
            log.info(
                "Trailing stop hit on %s: peak %.1f%% -> now %.1f%%. Closing.",
                pos.symbol, peak, pos.unrealized_pl_pct,
            )
            self.broker.cancel_open_orders_for(pos.symbol)  # release bracket
            oid = self.broker.close_position(pos.symbol)
            if oid:
                self.state.forget_symbol(pos.symbol)
            else:
                # Keep the high-water mark so we retry next tick rather than
                # leaving the position unprotected (bracket already canceled).
                log.critical(
                    "Trailing close FAILED for %s — position unprotected. Will retry.",
                    pos.symbol,
                )

    def forget(self, symbol: str) -> None:
        """Drop trailing state when a position is gone (filled stop/tp)."""
        self.state.forget_symbol(symbol)
