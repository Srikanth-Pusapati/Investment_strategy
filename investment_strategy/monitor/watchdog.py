"""Position watchdog — the always-on safety loop.

Alpaca's bracket orders already hold a stop-loss and take-profit per position at
the exchange, so this loop is defense-in-depth, not the only line of defense. It
adds two things brackets can't:

  1. Account-wide emergency flatten when the day's loss breaches the hard limit.
  2. A ratcheting trailing stop that locks in gains as a position runs up.

It runs on a short interval (default 30s). Closing positions is always allowed —
even under the kill switch — because reducing risk must never be blocked.
"""
from __future__ import annotations

import logging

from ..config import Config
from ..execution import AlpacaClient
from ..models import AccountSnapshot, Position

log = logging.getLogger("watchdog")


class Watchdog:
    def __init__(self, cfg: Config, broker: AlpacaClient):
        self.cfg = cfg
        self.broker = broker
        #: symbol -> highest unrealized P/L % seen, for the trailing stop.
        self._high_water: dict[str, float] = {}
        #: give back this much of peak gain before trailing-stopping out.
        self.trail_giveback_pct = 3.0

    def check_once(self) -> None:
        """One pass. Call on a timer from the orchestrator/monitor loop."""
        account = self.broker.get_account()

        if self._emergency_flatten(account):
            return  # everything is being closed; nothing else to do

        for pos in account.positions:
            self._update_trailing_stop(pos)

    # -- account-wide emergency exit --------------------------------------- #
    def _emergency_flatten(self, account: AccountSnapshot) -> bool:
        loss_pct = -account.day_pl_pct
        if loss_pct < self.cfg.risk.max_daily_loss_pct:
            return False
        log.error(
            "EMERGENCY: day loss %.2f%% >= limit %.2f%%. Flattening all positions.",
            loss_pct, self.cfg.risk.max_daily_loss_pct,
        )
        for pos in account.positions:
            self.broker.cancel_open_orders_for(pos.symbol)
            self.broker.close_position(pos.symbol)
        self._high_water.clear()
        return True

    # -- per-position trailing stop ---------------------------------------- #
    def _update_trailing_stop(self, pos: Position) -> None:
        peak = max(self._high_water.get(pos.symbol, 0.0), pos.unrealized_pl_pct)
        self._high_water[pos.symbol] = peak

        # Only trail once a position has shown a real gain to protect.
        if peak <= self.trail_giveback_pct:
            return
        if pos.unrealized_pl_pct <= peak - self.trail_giveback_pct:
            log.info(
                "Trailing stop hit on %s: peak %.1f%% -> now %.1f%%. Closing.",
                pos.symbol, peak, pos.unrealized_pl_pct,
            )
            self.broker.cancel_open_orders_for(pos.symbol)  # release bracket
            self.broker.close_position(pos.symbol)
            self._high_water.pop(pos.symbol, None)

    def forget(self, symbol: str) -> None:
        """Drop trailing state when a position is gone (filled stop/tp)."""
        self._high_water.pop(symbol, None)
