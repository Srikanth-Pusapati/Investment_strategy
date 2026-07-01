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
from ..ledger import TradeLedger, TradeRecord
from ..models import AccountSnapshot, Position
from ..notify import Alerter
from ..state import PortfolioState

log = logging.getLogger("watchdog")


class Watchdog:
    def __init__(
        self, cfg: Config, broker: AlpacaClient,
        state: PortfolioState | None = None, ledger: TradeLedger | None = None,
        alerter: Alerter | None = None,
    ):
        self.cfg = cfg
        self.broker = broker
        self.state = state or PortfolioState(cfg.state_file)
        # Watchdog exits (stops, take-profits, flattens) close positions the
        # decision loop never sees — without recording them the ledger's round-trip
        # history (and signal attribution) would be blind to most exits. Best-effort.
        self.ledger = ledger
        # Out-of-band paging for CRITICALs the loop can't self-heal (failed close =
        # naked position; latched halt). None => alerts are log-only (never raises).
        self.alerter = alerter
        #: give back this much of peak gain before trailing-stopping out.
        self.trail_giveback_pct = 3.0

    def _alert(self, key: str, subject: str, body: str) -> None:
        """Page a human, if an alerter is wired. The event is already logged at
        CRITICAL by the caller; this is purely the outbound channel."""
        if self.alerter is not None:
            self.alerter.critical(key, subject, body)

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
            # Hard stop/take first — fractional positions have no exchange bracket,
            # so this loop is their ONLY hard-exit enforcement.
            if self._enforce_hard_exits(pos):
                continue
            # Then the deterministic time-stop: recycle dead/flat capital so a
            # stalled position isn't held indefinitely (1B.4).
            if self._enforce_time_stop(pos):
                continue
            self._update_trailing_stop(pos)
        # Drop tracking for positions that are gone (filled stop/tp/sell).
        for sym in set(self.state.high_water) | set(self.state.exits):
            if sym not in live:
                self.state.forget_symbol(sym)

    # -- hard equity floor (latched) --------------------------------------- #
    def _equity_floor_breached(self, account: AccountSnapshot) -> bool:
        # Floor is a % of the PEAK high-water mark (auto-scales to any account
        # size). update_equity() ran first this tick, so peak >= current equity.
        pct = self.cfg.risk.equity_floor_pct
        peak = self.state.peak_equity
        if pct <= 0 or peak <= 0:
            return False
        floor = peak * (pct / 100.0)
        if account.equity > floor:
            return False
        reason = (
            f"Equity ${account.equity:,.0f} <= floor ${floor:,.0f} "
            f"({pct:.0f}% of peak ${peak:,.0f}). "
            f"Flattened and halted; clear {self.cfg.state_file} to resume."
        )
        self.state.latch_halt(reason)
        self._flatten_all(account, "EQUITY FLOOR")
        # Latched halt is terminal until a human clears the state file — page once.
        self._alert(
            "equity-floor-halt", "EQUITY FLOOR breached — trading HALTED", reason,
        )
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
                self._record_exit(pos, oid, "flatten")
            else:
                log.critical(
                    "%s: close FAILED for %s — position may be NAKED. Will retry.",
                    why, pos.symbol,
                )
                self._alert(
                    f"flatten-fail:{pos.symbol}",
                    f"NAKED position {pos.symbol} — {why} close FAILED",
                    f"{why} flatten of {pos.symbol} did not go through; the "
                    f"position is unprotected. The watchdog will retry every "
                    f"~{self.cfg.monitor_interval_s}s but may need manual "
                    f"intervention.",
                )

    # -- hard stop / take-profit for fractional (unbracketed) positions ---- #
    def _enforce_hard_exits(self, pos: Position) -> bool:
        """Close `pos` if it has breached the stop/take registered for it (only
        fractional positions are registered — whole-share buys use an exchange
        bracket). Returns True if a close was issued so the caller skips trailing."""
        exits = self.state.get_exits(pos.symbol)
        if not exits:
            return False
        stop_pct = exits.get("stop_pct", 0.0)
        take_pct = exits.get("take_pct", 0.0)
        hit = None
        if stop_pct > 0 and pos.unrealized_pl_pct <= -stop_pct:
            hit = f"stop -{stop_pct:.1f}%"
        elif take_pct > 0 and pos.unrealized_pl_pct >= take_pct:
            hit = f"take +{take_pct:.1f}%"
        if not hit:
            return False
        log.info(
            "Hard %s hit on %s (now %.1f%%). Closing fractional position.",
            hit, pos.symbol, pos.unrealized_pl_pct,
        )
        oid = self.broker.close_position(pos.symbol)
        if oid:
            self.state.forget_symbol(pos.symbol)
            self._record_exit(pos, oid, "stop" if hit.startswith("stop") else "take")
        else:
            log.critical(
                "Hard-exit close FAILED for %s — fractional position unprotected. "
                "Will retry.", pos.symbol,
            )
            self._alert(
                f"hard-exit-fail:{pos.symbol}",
                f"Fractional {pos.symbol} unprotected — {hit} close FAILED",
                f"{pos.symbol} breached its {hit} but the close did not go "
                f"through. Fractional positions have no exchange bracket, so this "
                f"loop is their only hard exit — the position is now unprotected. "
                f"Retrying every ~{self.cfg.monitor_interval_s}s.",
            )
        return True

    # -- deterministic time-stop (recycle dead/flat capital) --------------- #
    def _enforce_time_stop(self, pos: Position) -> bool:
        """Close `pos` if it has been held past max_hold_days WITHOUT reaching a
        meaningful gain — i.e. dead money that should rotate to a live thesis
        rather than sit forever (a name whose price never hit a stop but whose
        thesis went stale). Deterministic and LLM-independent. Returns True if a
        close was issued so the caller skips trailing.

        A first-seen fallback stamps the hold clock here, so a restart or a
        position opened before this feature still gets a (conservative) clock;
        the real entry time is set on the opening buy when available."""
        max_days = getattr(self.cfg.risk, "max_hold_days", 0.0)
        if not max_days or max_days <= 0:
            return False
        self.state.register_entry(pos.symbol)  # idempotent; first-seen fallback
        age = self.state.entry_age_days(pos.symbol)
        if age is None or age < max_days:
            return False
        min_gain = getattr(self.cfg.risk, "time_stop_min_gain_pct", 0.0)
        # A position that IS up and running is left to the trailing stop / take-
        # profit to maximize — only flat/dead capital is force-recycled.
        if pos.unrealized_pl_pct >= min_gain:
            return False
        log.info(
            "Time-stop on %s: held %.1fd (>= %.0fd) at %.1f%% (< %.1f%% target) "
            "— recycling dead capital.",
            pos.symbol, age, max_days, pos.unrealized_pl_pct, min_gain,
        )
        self.broker.cancel_open_orders_for(pos.symbol)  # release any resting bracket
        oid = self.broker.close_position(pos.symbol)
        if oid:
            self.state.forget_symbol(pos.symbol)
            self._record_exit(pos, oid, "time")
        else:
            log.critical(
                "Time-stop close FAILED for %s — position unprotected. Will retry.",
                pos.symbol,
            )
            self._alert(
                f"time-stop-fail:{pos.symbol}",
                f"{pos.symbol} time-stop close FAILED",
                f"{pos.symbol} hit its {max_days:.0f}d time-stop but the close did "
                f"not go through; retrying every ~{self.cfg.monitor_interval_s}s.",
            )
        return True

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
                self._record_exit(pos, oid, "trail")
            else:
                # Keep the high-water mark so we retry next tick rather than
                # leaving the position unprotected (bracket already canceled).
                log.critical(
                    "Trailing close FAILED for %s — position unprotected. Will retry.",
                    pos.symbol,
                )
                self._alert(
                    f"trail-fail:{pos.symbol}",
                    f"{pos.symbol} unprotected — trailing-stop close FAILED",
                    f"Trailing stop fired on {pos.symbol} (peak {peak:.1f}% -> "
                    f"now {pos.unrealized_pl_pct:.1f}%) but the close did not go "
                    f"through, and the bracket was already canceled — the "
                    f"position is unprotected. Retrying every "
                    f"~{self.cfg.monitor_interval_s}s.",
                )

    def forget(self, symbol: str) -> None:
        """Drop trailing state when a position is gone (filled stop/tp)."""
        self.state.forget_symbol(symbol)

    # -- ledger ------------------------------------------------------------- #
    def _record_exit(self, pos: Position, oid: str | None, reason: str) -> None:
        """Log a watchdog-driven close to the ledger so attribution sees the exit.
        The position's unrealized P&L at this instant IS the realized outcome.
        Best-effort and never raises into the safety loop."""
        if self.ledger is None:
            return
        try:
            self.ledger.record(TradeRecord.for_sell(
                pos.symbol, f"watchdog {reason}", oid, qty=pos.qty,
                realized_pl_pct=pos.unrealized_pl_pct, realized_pl=pos.unrealized_pl,
                exit_reason=reason,
            ))
        except Exception as e:  # never let logging break the watchdog
            log.warning("Ledger exit-record failed for %s: %s", pos.symbol, e)
