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
from datetime import datetime, timezone

from ..config import Config
from ..execution import AlpacaClient
from ..execution.options import parse_occ
from ..ledger import TradeLedger, TradeRecord
from ..models import AccountSnapshot, Position
from ..notify import Alerter
from ..risk import trail_geometry
from ..state import PortfolioState

log = logging.getLogger("watchdog")


def _partial(pos: Position, qty: float) -> Position:
    """A view of `pos` reduced to `qty` shares — for recording a partial
    (scale-out) exit in the ledger with the right quantity and a proportional
    realized P&L, while the rest of the position stays open."""
    frac = (qty / pos.qty) if pos.qty else 0.0
    return pos.model_copy(update={
        "qty": qty,
        "market_value": pos.current_price * qty,
        "unrealized_pl": pos.unrealized_pl * frac,
    })


class Watchdog:
    def __init__(
        self, cfg: Config, broker: AlpacaClient,
        state: PortfolioState | None = None, ledger: TradeLedger | None = None,
        alerter: Alerter | None = None,
        on_exchange_exit=None,
    ):
        self.cfg = cfg
        self.broker = broker
        # Called (best-effort, no args) when a tracked position vanishes — an
        # exchange bracket leg filled with no code running. The orchestrator
        # points it at a locked exchange-exit backfill so the ledger and the
        # re-entry cooldown learn about the exit within ~1 watchdog tick instead
        # of at the next hourly cycle. None => no immediate backfill (legacy).
        self._on_exchange_exit = on_exchange_exit
        self.state = state or PortfolioState(cfg.state_file)
        # Watchdog exits (stops, take-profits, flattens) close positions the
        # decision loop never sees — without recording them the ledger's round-trip
        # history (and signal attribution) would be blind to most exits. Best-effort.
        self.ledger = ledger
        # Out-of-band paging for CRITICALs the loop can't self-heal (failed close =
        # naked position; latched halt). None => alerts are log-only (never raises).
        self.alerter = alerter
        #: give back this much of peak gain before trailing-stopping out
        #: (TRAIL_GIVEBACK_PCT; the backtest engine mirrors the same knob).
        self.trail_giveback_pct = getattr(cfg.risk, "trail_giveback_pct", 3.0)
        #: last time we logged the "protected, waiting to fill" no-op per
        #: symbol — an illiquid pre/post-market exit can sit unfilled for
        #: hours, and at a 30s tick that line repeats hundreds of times a day
        #: for a single ticker (BIIB, 2026-07-15: 318x) without saying
        #: anything new. Throttled below, not silenced (see _log_waiting_throttled).
        self._last_wait_log: dict[str, datetime] = {}
        #: one-shot latch for the "day-loss breach but market CLOSED" note, so
        #: the deferral logs once per closed session instead of every 30s tick
        #: (Jul 23: 1,301 EMERGENCY lines; Jul 24: 228 more until 01:56).
        self._flatten_deferred_noted = False

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

        # Option rows (OCC symbols, per-share prices, no exchange bracket) are
        # invisible to the equity paths below — latest_price() on an OCC symbol
        # returns garbage and %-P&L math differs. They get their own
        # deterministic exits (premium stop/take + expiry time-stop).
        equities = [p for p in account.positions if not p.is_option]
        option_rows = [p for p in account.positions if p.is_option]

        # Gate the trailing stop to regular hours (TRAIL_RTH_ONLY): thin
        # pre/post-market marks false-trigger it and ratchet phantom peaks. The
        # HARD exits below (stop/take/time-stop/floor/flatten) stay 24/7 —
        # they're the actual protection; the trail only locks in gains. A clock
        # read failure fails toward trailing (protection over noise-suppression).
        trail_ok = True
        if getattr(self.cfg.risk, "trail_rth_only", True) and equities:
            try:
                trail_ok = self.broker.is_market_open()
            except Exception as e:  # noqa: BLE001 — never break the safety loop
                log.debug("trail RTH gate: clock read failed (%s); trailing on.", e)
                trail_ok = True

        live = {p.symbol for p in equities}
        core_etf = getattr(self.cfg, "core_etf", "")
        for pos in equities:
            # Hard stop/take first — fractional positions have no exchange bracket,
            # so this loop is their ONLY hard-exit enforcement.
            if self._enforce_hard_exits(pos):
                continue
            # Then the deterministic time-stop: recycle dead/flat capital so a
            # stalled position isn't held indefinitely (1B.4).
            if self._enforce_time_stop(pos):
                continue
            # The core ETF is exempt from the trailing stop for the same reason
            # it's exempt from the time-stop: it's a permanent diversified
            # allocation, not a thesis trade. A +5% run followed by an ordinary
            # 5% giveback would otherwise dump the ENTIRE core in one close
            # (Jul 29 audit: high_water QQQ=0.96 showed the trail live on it).
            # Its protection is the GTC core stop, the equity floor, the daily-
            # loss flatten, and the falling-tape core-defense trim. The
            # exemption requires the GTC stop to actually be configured
            # (CORE_STOP_PCT > 0) — with it off, the trail stays the core's
            # only position-level guard and must keep running.
            core_exempt = (
                core_etf and pos.symbol == core_etf
                and getattr(self.cfg, "core_stop_pct", 0.0) > 0
            )
            if trail_ok and not core_exempt:
                self._update_trailing_stop(pos)
        self._retry_pending_decision_sells({p.symbol: p for p in equities})
        self._check_option_positions(option_rows)
        # Drop tracking for positions that are gone (filled stop/tp/sell). A
        # vanished tracked position means an exchange-side bracket leg (or a
        # manual sell) filled with no code running — fire the exchange-exit
        # backfill NOW instead of waiting for the next hourly decision cycle
        # (SOFI 2026-07-16: broker stop filled, ledger/cooldown blind ~1h).
        vanished = False
        # Key the sweep on EVERY per-symbol tracking map, not just high_water/
        # exits: a position that entered and crashed inside one watchdog tick
        # has entry_times/stop_widths but no high-water mark yet, and would
        # stay stranded forever. Symbols with an order still pending (a buy
        # submitted seconds ago that hasn't filled into a position row) are
        # skipped — wiping their fresh clocks would reset the hold age.
        pending_syms = {sym for _oid, sym in self.state.get_pending_orders()}
        tracked = (
            set(self.state.high_water) | set(self.state.exits)
            | set(self.state.entry_times) | set(self.state.stop_widths)
        )
        for sym in tracked:
            if sym not in live and sym not in pending_syms:
                self.state.forget_symbol(sym)
                vanished = True
        if vanished and self._on_exchange_exit is not None:
            try:
                self._on_exchange_exit()
            except Exception as e:  # noqa: BLE001 — backfill must never break the loop
                log.warning("exchange-exit backfill callback failed: %s", e)

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
        # Latch + flatten is terminal, so never act on a single read: a glitched
        # snapshot (equity==cash, 2026-07-07) must not kill the account. Only a
        # SUCCESSFUL re-read showing equity back above the floor cancels the
        # halt; a failed re-read confirms it, so a genuine collapse on a flaky
        # network still halts.
        try:
            confirm = self.broker.get_account()
            if confirm.equity > floor:
                log.warning(
                    "EQUITY FLOOR breach NOT confirmed on re-read (equity $%.0f "
                    "then $%.0f vs floor $%.0f) — ignoring glitched read.",
                    account.equity, confirm.equity, floor,
                )
                return False
            account = confirm
        except Exception as e:
            log.warning(
                "EQUITY FLOOR confirm re-read failed (%s) — proceeding with halt.", e,
            )
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
            self._flatten_deferred_noted = False
            return False
        # A CLOSED market gets ONE flatten pass, not one per 30s tick: the
        # first pass rests the exits at the venue (marketable-limit closes
        # work the reopen even if this process dies overnight); every repeat
        # was pure spam — nothing new can fill, DAY closes expire at the bell
        # and re-fire each tick (Jul 23: 1,301 EMERGENCY lines; Jul 24: 228
        # more past midnight, where a stale Alpaca last_equity made
        # YESTERDAY'S loss read as today's until ~02:00). The latch resets
        # when the loss clears or the market reopens; a clock-read failure
        # fails toward protection (flatten as if open).
        try:
            market_open = self.broker.is_market_open()
        except Exception as e:  # noqa: BLE001 — never break the safety loop
            log.debug("emergency-flatten clock read failed (%s); proceeding.", e)
            market_open = True
        if not market_open:
            if self._flatten_deferred_noted:
                return False  # exits already rested on the first closed tick
            self._flatten_deferred_noted = True
            log.warning(
                "Day loss %.2f%% >= limit %.2f%% with the market CLOSED — "
                "resting the exits ONCE at the venue (they work the reopen "
                "without this process), then staying quiet until the open. "
                "After midnight this reading is yesterday's session until "
                "the broker rolls last_equity.",
                loss_pct, self.cfg.risk.max_daily_loss_pct,
            )
            self._flatten_all(account, "DAILY LOSS")
            return True
        self._flatten_deferred_noted = False
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
        # Option structures first, whole groups at a time (_close_hard would
        # half-work on an OCC row: the close itself succeeds but every price
        # fallback is broken, and a spread must never unwind leg-by-leg).
        option_rows = [p for p in account.positions if p.is_option]
        for group in self._option_groups(option_rows).values():
            self._exit_option_group(group, "flatten")
        for pos in account.positions:
            if pos.is_option:
                continue
            if pos.qty <= 0:
                # Already flat at broker (paper account lag can keep a zero-qty
                # position in the snapshot briefly after a bracket stop fills).
                self.state.forget_symbol(pos.symbol)
                continue
            outcome, oid = self._close_hard(pos, "flatten")
            if outcome == "full":
                self.state.forget_symbol(pos.symbol)
                self._record_exit(pos, oid, "flatten")
            elif outcome == "partial":
                pass  # ledgered in _close_hard; keep tracked, retry next tick
            else:
                log.critical(
                    "%s: close FAILED for %s — position may be NAKED. Will retry.",
                    why, pos.symbol,
                )
                self._alert(
                    f"flatten-fail:{pos.symbol}",
                    f"NAKED position {pos.symbol} — {why} close FAILED",
                    f"{why} flatten of {pos.symbol} did not go through (even the "
                    f"marketable-limit fallback); the position is unprotected. The "
                    f"watchdog will retry every ~{self.cfg.monitor_interval_s}s but "
                    f"may need manual intervention.",
                )

    #: minimum gap between repeated "still waiting to fill" log lines for the
    #: same symbol — the underlying retry cadence (the watchdog tick) is far
    #: shorter and would otherwise spam identical WARNINGs for hours.
    WAIT_LOG_THROTTLE_S = 300

    def _log_throttled(
        self, key: str, msg: str, *args, level: int = logging.WARNING,
    ) -> None:
        """Emit `msg` at most once per WAIT_LOG_THROTTLE_S per `key`. The
        watchdog re-evaluates every ~30s, so any condition that persists across
        ticks (an illiquid exit sitting unfilled, a trail that keeps re-firing
        while the close works) would otherwise repeat the same line hundreds of
        times a day. `key` namespaces the throttle (e.g. wait:SYM vs trail:SYM)."""
        now = datetime.now(timezone.utc)
        last = self._last_wait_log.get(key)
        if last is not None and (now - last).total_seconds() < self.WAIT_LOG_THROTTLE_S:
            return
        self._last_wait_log[key] = now
        log.log(level, msg, *args)

    def _log_waiting_throttled(self, symbol: str, msg: str, *args) -> None:
        self._log_throttled(f"wait:{symbol}", msg, *args, level=logging.WARNING)

    def close_now(self, pos: Position, reason: str) -> tuple[str, str | None]:
        """Public entry for decision-loop closes (orchestrator SELL / thesis
        decay): the same escalation ladder as watchdog exits — exit-via-replace
        when shares are reserved by live sell legs, never cancel-then-resell
        (the async pending-cancel wedge that stranded SPCX 2026-07-16: cancel
        issued, close refused 40310000, position left naked). Partial exits
        are ledgered in here with `reason`; the caller records only a "full"
        close and must NOT ledger anything on "failed"."""
        return self._close_hard(pos, reason)

    def _retry_pending_decision_sells(self, by_symbol: dict[str, Position]) -> None:
        """Retry a decision-driven SELL that failed to execute on an earlier
        cycle (queued by the orchestrator via state.queue_decision_sell) —
        the same close_now escalation ladder as any other exit, every tick,
        until it succeeds or the position is confirmed gone. Regression
        2026-07-20/23: previously a failed decision-SELL just sat until the
        next hourly cycle happened to re-propose the same sell, with no page
        in between; every OTHER exit path already retries + pages."""
        for symbol, info in self.state.get_pending_decision_sells().items():
            pos = by_symbol.get(symbol)
            if pos is None:
                # Already gone — a different exit path closed it, or a
                # broker-side fill not yet reconciled. Stop retrying; the
                # exchange-exit backfill (if any) covers ledgering it.
                self.state.pop_decision_sell(symbol)
                continue
            outcome, oid = self.close_now(pos, "decision")
            if outcome == "full":
                self.state.pop_decision_sell(symbol)
                self.forget(symbol)
                self._record_exit(
                    pos, oid, "decision",
                    rationale=info.get("rationale") or "",
                    key_signals=info.get("key_signals") or [],
                    composite_score=info.get("composite_score"),
                )
            elif outcome == "partial":
                pass  # ledgered inside close_now; keep queued, retry next tick
            else:
                log.critical(
                    "Decision-SELL retry FAILED for %s — still queued, will "
                    "retry again next tick.", symbol,
                )
                self._alert(
                    f"decision-sell-fail:{symbol}",
                    f"{symbol} SELL still unresolved — close retrying",
                    f"A decision-driven SELL for {symbol} has failed every "
                    f"retry since it was queued; still retrying every "
                    f"~{self.cfg.monitor_interval_s}s.",
                )

    # -- best-effort hard close (1B.5) -------------------------------------- #
    def _close_hard(self, pos: Position, reason: str) -> tuple[str, str | None]:
        """Close `pos` as hard as the broker allows, escalating through every
        fallback. Partial exits are ledgered here (with `reason`) at submit
        time; the caller records only a "full" close. Returns (outcome, oid):

          ("full", oid)     the whole position is closing — forget + record it.
          ("partial", oid?) some shares are reserved by open sell orders. We
                            replaced the live sells with marketable limits so
                            they fill AS the exit, and/or sold the available
                            slice. Caller keeps the position tracked and
                            retries the full close next tick.
          ("failed", None)  nothing could be done — caller pages a human.

        When the shares are free, the plain close comes FIRST, before touching
        any order: canceling a bracket leg and re-selling its shares opens a
        wedge window — a cancel stuck in `pending_cancel` reserves the shares
        indefinitely (new sells 40310000, re-cancels 42210000, replaces
        refused; seen with FRHC) — whereas replacing a still-live leg into a
        marketable limit is atomic and cannot strand anything. When shares ARE
        reserved by open sells, the plain close is provably refused with
        40310000 (seen with BTDR/EQPT), so we skip straight to the
        replace-live-legs path — same outcome, no doomed ERROR line per tick.

        The market-closed / LULD-halt fallback (a plain market order is
        rejected) rests a GTC marketable-limit so the exit still fills at the
        reopen. That fallback is whole-share only (Alpaca rejects GTC/limit on
        fractional), so a sub-share position's overnight-gap risk stays
        irreducible."""
        avail = pos.qty_available
        if avail < pos.qty:
            # Shares reserved by open sell orders (bracket / GTC legs). Turn
            # the live legs themselves into the exit, sweep the rest, and sell
            # whatever is free right now — still never cancel-then-resell.
            ref = self.broker.latest_price(pos.symbol)
            replaced = self.broker.clear_orders_for_exit(pos.symbol, ref)
            oid = self.broker.reduce_position(pos.symbol, avail) if avail > 0 else None
            for rid, rqty, old_id, old_filled in replaced:
                # A falling price can re-replace the SAME exit on a later tick
                # (new order id each time). If we already ledgered a SELL for
                # the superseded id, void/resize that record first — otherwise
                # each declining 30s tick stacks another full-qty exit.
                self._supersede_exit_record(
                    pos.symbol, old_id, old_filled, old_filled + rqty,
                )
                self._record_exit(_partial(pos, rqty), rid, reason)
            if oid:
                self._record_exit(_partial(pos, avail), oid, reason)
            if oid or replaced:
                log.warning(
                    "Partial close %s (%s): sold %.6g available share(s), made "
                    "%d resting sell(s) marketable; %.6g reserved by open "
                    "orders — will retry the full close next tick.",
                    pos.symbol, reason, avail if oid else 0.0, len(replaced),
                    pos.qty - avail,
                )
                return "partial", oid
            # Nothing new to do this tick — but the reserved shares may already be
            # covered by marketable exits made on a PRIOR tick that simply haven't
            # filled yet (clear_orders_for_exit leaves a leg alone once it IS the
            # exit, so it returns nothing here). That is protected, not a failed
            # close: page only when no working sell exit is resting (e.g. legs
            # wedged in pending_cancel, or none at all — the FRHC-class stall).
            if self.broker.has_working_exit(pos.symbol, ref):
                self._log_waiting_throttled(
                    pos.symbol,
                    "Close %s (%s): %.6g reserved share(s) already covered by a "
                    "resting marketable exit from a prior tick — protected, "
                    "waiting to fill. Will retry the full close next tick.",
                    pos.symbol, reason, pos.qty - avail,
                )
                return "partial", None
            return "failed", None
        oid = self.broker.close_position(pos.symbol)
        if oid:
            return "full", oid
        # Nothing reserved, yet the close was refused: resting orders (e.g. a
        # bracket on the buy side / wash-trade block) or a closed / halted
        # market. Sweep orders, retry once, then rest a GTC exit.
        self.broker.cancel_open_orders_for(pos.symbol)
        oid = self.broker.close_position(pos.symbol)
        if oid:
            return "full", oid
        ref = self.broker.latest_price(pos.symbol)
        oid = self.broker.close_position_marketable_limit(pos.symbol, pos.qty, ref)
        return ("full", oid) if oid else ("failed", None)

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
        # Stop-loss is always a FULL exit. Take-profit may scale out (1B.8).
        if stop_pct > 0 and pos.unrealized_pl_pct <= -stop_pct:
            hit = f"stop -{stop_pct:.1f}%"
        elif take_pct > 0 and pos.unrealized_pl_pct >= take_pct:
            # Let winners run: sell a slice at the target and trail the rest rather
            # than capping the whole position here (unless already scaled/disabled).
            if (
                getattr(self.cfg.risk, "scale_out_enabled", False)
                and not exits.get("scaled")
                and self._scale_out(pos, stop_pct, take_pct)
            ):
                return True
            hit = f"take +{take_pct:.1f}%"
        else:
            return False
        log.info(
            "Hard %s hit on %s (now %.1f%%). Closing fractional position.",
            hit, pos.symbol, pos.unrealized_pl_pct,
        )
        reason = "stop" if hit.startswith("stop") else "take"
        outcome, oid = self._close_hard(pos, reason)
        if outcome == "full":
            self.state.forget_symbol(pos.symbol)
            self._record_exit(pos, oid, reason)
        elif outcome == "partial":
            return True  # exits stay registered; retry the full close next cycle
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

    # -- scale-out at the take-profit target (let winners run, 1B.8) ------- #
    def _scale_out(self, pos: Position, stop_pct: float, take_pct: float) -> bool:
        """Sell SCALE_OUT_PCT of `pos` at its take-profit target and let the rest
        ride the trailing stop, instead of closing the whole winner here. Drops the
        hard take (so it isn't re-triggered) but KEEPS the stop, and marks the
        position scaled so this fires once. Returns True if the partial sell went
        through (caller then skips trailing this tick). A failed partial sell
        returns False so the caller falls back to the full-close take path."""
        frac = getattr(self.cfg.risk, "scale_out_pct", 0.0) / 100.0
        sell_qty = round(pos.qty * frac, 6)
        # Whole-shares mode (GA-2.3): a partial sell must not leave fractional
        # dust that can't carry a GTC exit — round the slice down; too small to
        # make a whole share -> fall back to the full take-profit close.
        if getattr(self.cfg.risk, "whole_shares_only", False):
            sell_qty = float(int(sell_qty))
        if frac <= 0 or sell_qty <= 0:
            return False
        self.broker.cancel_open_orders_for(pos.symbol)  # release any resting bracket
        oid = self.broker.reduce_position(pos.symbol, sell_qty)
        if not oid:
            log.warning(
                "Scale-out sell FAILED for %s — falling back to full take-profit close.",
                pos.symbol,
            )
            return False
        log.info(
            "Scale-out %s: sold %.0f%% (%g sh) at +%.1f%% target; trailing the rest.",
            pos.symbol, frac * 100.0, sell_qty, pos.unrealized_pl_pct,
        )
        # Keep the downside stop, drop the take, and mark scaled -> the remainder is
        # now governed by the trailing stop (which already locks in gains).
        # Ratchet the high-water mark to the take-crossing gain FIRST: this tick
        # `continue`s before _update_trailing_stop runs (and pre/post-market
        # take-crossings never reach it at all under TRAIL_RTH_ONLY), so without
        # the ratchet the recorded peak can sit BELOW the R-scaled arm and the
        # remainder would hold no take AND an unarmed trail — unprotected until
        # the original stop. At the live knobs take (2.5R) > arm (1.5R), so this
        # ratchet always re-arms the trail for the remainder.
        self.state.set_high_water(pos.symbol, max(
            self.state.get_high_water(pos.symbol), pos.unrealized_pl_pct))
        self.state.register_exits(pos.symbol, stop_pct, 0.0, scaled=True)
        self._record_exit(_partial(pos, sell_qty), oid, "scale")
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
        # The core-satellite ETF (Todo 1.6) is a permanent, diversified holding — it
        # has no thesis to go stale, so the "recycle dead capital" time-stop must not
        # rotate it out. Account-level guards still protect it.
        if getattr(self.cfg, "core_etf", "") and pos.symbol == self.cfg.core_etf:
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
        outcome, oid = self._close_hard(pos, "time")
        if outcome == "full":
            self.state.forget_symbol(pos.symbol)
            self._record_exit(pos, oid, "time")
        elif outcome == "partial":
            pass  # ledgered in _close_hard; keep tracked, retry next tick
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

        # R-scaled geometry (risk.trail_geometry): arm only once the peak has
        # covered the position's own planned risk, and give volatile names
        # proportionally more room. get_stop_width prefers the enforced exits
        # record and falls back to the buy-time stop-width note, so bracketed
        # whole-share names get the R geometry too; a position with no
        # recorded stop at all (core fills, pre-existing books) runs the fixed
        # legacy %.
        arm, giveback = trail_geometry(
            self.cfg.risk, self.state.get_stop_width(pos.symbol))

        # Only trail once a position has shown a real gain to protect.
        if peak <= arm:
            return
        if pos.unrealized_pl_pct <= peak - giveback:
            # Throttled: while the close works through an illiquid book the
            # trigger keeps re-evaluating every tick (PATH 2026-07-20: 24 lines
            # in 19 min). Throttle the announcement, never the close itself.
            self._log_throttled(
                f"trail:{pos.symbol}",
                "Trailing stop hit on %s: peak %.1f%% -> now %.1f%%. Closing.",
                pos.symbol, peak, pos.unrealized_pl_pct, level=logging.INFO,
            )
            outcome, oid = self._close_hard(pos, "trail")
            if outcome == "full":
                self.state.forget_symbol(pos.symbol)
                self._record_exit(pos, oid, "trail")
            elif outcome == "partial":
                pass  # ledgered in _close_hard; keep tracked, retry next tick
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

    # -- deterministic option exits (premium stop/take + expiry time-stop) -- #
    @staticmethod
    def _option_groups(
        option_rows: list[Position],
    ) -> dict[tuple[str, str], list[Position]]:
        """Group option legs by (underlying, expiry) — a vertical's two legs are
        ONE structure and must exit together. Unparseable symbols are skipped
        loudly (they can't be managed deterministically)."""
        groups: dict[tuple[str, str], list[Position]] = {}
        for p in option_rows:
            occ = parse_occ(p.symbol)
            if occ is None:
                log.warning("Unrecognized option symbol %s — not managed.", p.symbol)
                continue
            groups.setdefault((occ[0], occ[1]), []).append(p)
        return groups

    def _check_option_positions(self, option_rows: list[Position]) -> None:
        """Options have no exchange bracket, so this loop is their ONLY
        protection. Exit the WHOLE structure when its P&L (relative to the net
        premium paid — the max loss on a debit play) breaches the premium
        stop/take, or when expiry is close enough that assignment risk and
        terminal theta outweigh any remaining thesis (the stale-option
        time-stop), whichever comes first."""
        if not option_rows:
            return
        stop = getattr(self.cfg.risk, "option_stop_loss_pct", 50.0)
        take = getattr(self.cfg.risk, "option_take_profit_pct", 100.0)
        close_dte = getattr(self.cfg.risk, "option_close_dte", 3.0)
        for (under, expiry), group in self._option_groups(option_rows).items():
            # Net premium PAID: long legs debit, short legs (negative qty)
            # credit. The risk gate only approves net-debit structures, so this
            # is positive for anything we opened ourselves.
            basis = sum(p.avg_entry_price * p.qty * 100.0 for p in group)
            pl = sum(p.unrealized_pl for p in group)
            pl_pct = (pl / basis * 100.0) if basis > 1e-9 else 0.0
            dte = self._days_to_expiry(expiry)
            if stop > 0 and basis > 1e-9 and pl_pct <= -stop:
                reason = "stop"
                hit = f"premium stop -{stop:.0f}% (now {pl_pct:+.1f}%)"
            elif take > 0 and basis > 1e-9 and pl_pct >= take:
                reason = "take"
                hit = f"premium take +{take:.0f}% (now {pl_pct:+.1f}%)"
            elif close_dte > 0 and dte is not None and dte <= close_dte:
                reason = "option_expiry"
                hit = f"{dte:.0f} DTE <= {close_dte:.0f} — closing before expiry"
            else:
                continue
            log.info(
                "Option exit on %s %s: %s. Closing %d leg(s).",
                under, expiry, hit, len(group),
            )
            self._exit_option_group(group, reason, under=under, pl_pct=pl_pct, pl=pl)

    @staticmethod
    def _days_to_expiry(expiry: str) -> float | None:
        """Calendar days until the OCC expiry date. Computed against the UTC
        date, which after 8pm ET reads one day AHEAD of the exchange calendar —
        i.e. at worst a day conservative (closes sooner), never late."""
        try:
            exp = datetime.strptime(expiry, "%Y-%m-%d").date()
        except ValueError:
            return None
        return float((exp - datetime.now(timezone.utc).date()).days)

    def _exit_option_group(
        self, group: list[Position], reason: str, *,
        under: str | None = None, pl_pct: float | None = None,
        pl: float | None = None,
    ) -> bool:
        """Close every leg of one option structure as a single order, ledger the
        round-trip under the UNDERLYING (pairing it with the entry record so
        attribution scores option trades), and page on failure. Returns True
        when the close order went in (or one is ALREADY resting from a prior
        tick — see the qty_available check below).

        Regression 2026-07-23: after close_option_leg's DAY-limit fallback got
        a close order resting at the venue, this loop had no memory of that
        and re-submitted ANOTHER close next tick. With every contract already
        reserved by the first order, qty_available was 0 and the SECOND
        attempt came back "account not eligible to trade uncovered option
        contracts" — a real API rejection, but a false CRITICAL page, since a
        working exit was already in flight. qty_available (Alpaca's own
        reserved-by-open-orders tracking) is the same signal the equity path
        uses via has_working_exit; a leg whose contracts are already fully
        reserved is treated as protected, not failed."""
        if under is None:
            occ = parse_occ(group[0].symbol)
            under = occ[0] if occ else group[0].symbol
        if pl is None:
            pl = sum(p.unrealized_pl for p in group)
        if pl_pct is None:
            basis = sum(p.avg_entry_price * p.qty * 100.0 for p in group)
            pl_pct = (pl / basis * 100.0) if basis > 1e-9 else None
        if all(
            p.qty_available is not None
            and abs(p.qty_available) < abs(p.qty) - 1e-9
            for p in group
        ):
            log.info(
                "Option exit on %s already resting (every leg's contracts are "
                "reserved by an open order) — skipping a redundant close "
                "this tick.", under,
            )
            return True
        oid = self.broker.close_option_group(group)
        if oid:
            self._record_option_exit(under, group, oid, reason, pl_pct, pl)
            return True
        log.critical(
            "Option close FAILED for %s (%s) — %d leg(s) unprotected. Will retry.",
            under, reason, len(group),
        )
        self._alert(
            f"option-exit-fail:{under}",
            f"Option position {under} unprotected — {reason} close FAILED",
            f"The {reason} exit for the {under} option structure "
            f"({', '.join(p.symbol for p in group)}) did not go through. Options "
            f"have no exchange bracket, so this loop is their only hard exit. "
            f"Retrying every ~{self.cfg.monitor_interval_s}s.",
        )
        return False

    def _record_option_exit(
        self, under: str, group: list[Position], oid: str | None,
        reason: str, pl_pct: float | None, pl: float,
    ) -> None:
        """Best-effort bookkeeping for an option close — mirrors _record_exit."""
        try:
            # Re-entry cooldown + loss streak on the NAME (an option trip that
            # burned premium counts against the same underlying's record).
            self.state.register_exit(under, pl_pct=pl_pct)
        except Exception as e:
            log.warning("Exit-clock stamp failed for %s: %s", under, e)
        if self.ledger is None:
            return
        try:
            contracts = max(abs(p.qty) for p in group)
            occs = ",".join(p.symbol for p in group)
            self.ledger.record(TradeRecord.for_sell(
                under, f"watchdog option {reason} ({occs})", oid,
                qty=contracts, realized_pl_pct=pl_pct, realized_pl=pl,
                exit_reason=reason, instrument="option",
            ))
        except Exception as e:  # never let logging break the watchdog
            log.warning("Ledger option-exit record failed for %s: %s", under, e)
        if not oid:
            return
        try:
            # Queue the close for the orchestrator's reconcile pass, exactly
            # like equity exits (_record_exit). Without this, an option close
            # was ledgered at SUBMIT and never followed: a DAY close that
            # expired at the bell kept its phantom exit row forever, the
            # resubmit added a second one (the archived T book shows the loss
            # double-counted that way), and the eventual FILL produced no log
            # line — the Jul-24 postmortem read "no closed positions" off a
            # ledger that had missed a real close. note_exit_ledgered marks
            # the oid as EXIT-side so an expired DAY close gets its ledger
            # correction WITHOUT tripping the reconcile buy-halt (the
            # watchdog's resubmit loop owns the recovery).
            self.state.add_pending_order(oid, under)
            self.state.note_exit_ledgered(under, oid)
        except Exception as e:  # bookkeeping must never break the safety loop
            log.warning("Option-exit order bookkeeping failed for %s: %s", under, e)

    def forget(self, symbol: str) -> None:
        """Drop trailing state when a position is gone (filled stop/tp)."""
        self.state.forget_symbol(symbol)

    # -- ledger ------------------------------------------------------------- #
    def _record_exit(
        self, pos: Position, oid: str | None, reason: str, *,
        rationale: str | None = None, key_signals: list[str] | None = None,
        composite_score: float | None = None,
    ) -> None:
        """Log a watchdog-driven close to the ledger so attribution sees the exit.
        The position's unrealized P&L at this instant IS the realized outcome.
        Best-effort and never raises into the safety loop.

        `rationale`/`key_signals`/`composite_score` let a retried DECISION-sell
        (see _retry_pending_decision_sells) ledger with the original decision's
        real context instead of the generic "watchdog {reason}" label."""
        # Start the re-entry cooldown clock (churn guard). A scale-out is only a
        # partial exit, but stamping it is harmless: the cooldown applies only
        # when the symbol is no longer held. The exit mark feeds the price-aware
        # re-entry guard; the P&L feeds the loss-streak scorecard (scale-outs
        # are winners by construction, so they only ever CLEAR a streak).
        try:
            self.state.register_exit(
                pos.symbol, price=pos.current_price or None,
                pl_pct=pos.unrealized_pl_pct,
            )
        except Exception as e:
            log.warning("Exit-clock stamp failed for %s: %s", pos.symbol, e)
        if self.ledger is None:
            return
        try:
            self.ledger.record(TradeRecord.for_sell(
                pos.symbol, rationale or f"watchdog {reason}", oid,
                qty=pos.qty, key_signals=key_signals or [],
                realized_pl_pct=pos.unrealized_pl_pct, realized_pl=pos.unrealized_pl,
                exit_reason=reason, exit_price=pos.current_price or None,
                composite_score=composite_score,
            ))
        except Exception as e:  # never let logging break the watchdog
            log.warning("Ledger exit-record failed for %s: %s", pos.symbol, e)
        if not oid:
            return
        try:
            # This SELL record is an INTENT at submit/replace-time marks, not a
            # fill. Queue it for the orchestrator's reconcile pass so a
            # canceled/expired/partial outcome corrects the ledger (a phantom
            # sell otherwise lives forever), and remember we ledgered this id
            # so a later re-replace can supersede it.
            self.state.add_pending_order(oid, pos.symbol)
            self.state.note_exit_ledgered(pos.symbol, oid)
        except Exception as e:  # bookkeeping must never break the safety loop
            log.warning("Exit-order bookkeeping failed for %s: %s", pos.symbol, e)

    def _supersede_exit_record(
        self, symbol: str, old_id: str, old_filled: float, old_qty: float,
    ) -> None:
        """Void (or resize to what actually filled) the SELL we ledgered for a
        now-replaced exit order, so re-replaces don't double-count the exit.
        Only touches ids WE ledgered — a bracket leg replaced on its first tick
        was never in the ledger and needs no correction. Never raises."""
        if self.ledger is None or not old_id:
            return
        try:
            if not self.state.exit_was_ledgered(symbol, old_id):
                return
            self.ledger.record(TradeRecord.correction(
                old_id, symbol, "replaced", old_filled, old_qty,
            ))
            log.info(
                "Superseded exit %s (%s): prior SELL record corrected to "
                "%.6g/%.6g filled; the replacement order carries the exit.",
                old_id, symbol, old_filled, old_qty,
            )
        except Exception as e:  # never let bookkeeping break the watchdog
            log.warning("Supersede correction failed for %s: %s", symbol, e)
