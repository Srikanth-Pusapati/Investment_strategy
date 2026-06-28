"""RiskManager — the deterministic gate every trade must pass.

Design principle: Claude PROPOSES, RiskManager DISPOSES. The LLM's numbers are
treated as untrusted input. This module enforces hard caps that the model
cannot override, regardless of how confident its rationale sounds. If you only
trust one file in this repo, trust this one — read it before going live.
"""
from __future__ import annotations

import logging

from .config import RiskLimits
from .models import (
    AccountSnapshot,
    Action,
    OptionStrategy,
    RiskDecision,
    RiskVerdict,
    TradeProposal,
)
from .state import PortfolioState

log = logging.getLogger("risk")

# When realized volatility is unavailable we must NOT default to the largest
# allowed size — a data outage is exactly when to be cautious. Size as if the
# name were quite volatile so vol-targeting shrinks the position.
_ASSUMED_VOL_WHEN_UNKNOWN = 0.60


class RiskManager:
    def __init__(
        self, limits: RiskLimits, kill_switch: bool = False,
        state: PortfolioState | None = None,
    ):
        self.limits = limits
        self.kill_switch = kill_switch
        # Shared, persisted risk memory (peak equity, halt latch). A default
        # instance keeps unit tests and ad-hoc use working without wiring.
        self.state = state or PortfolioState()

    # -- top-level halts ---------------------------------------------------- #
    def trading_halted(self, account: AccountSnapshot) -> tuple[bool, str]:
        """Account-wide reasons to block ALL new buying. Sells/exits still allowed."""
        if self.state.halted:
            return True, f"HALT LATCH set: {self.state.halt_reason}"
        if self.kill_switch:
            return True, "KILL_SWITCH is on — no new positions."
        loss_pct = -account.day_pl_pct  # positive number when losing
        if loss_pct >= self.limits.max_daily_loss_pct:
            return True, (
                f"Daily loss {loss_pct:.2f}% >= limit "
                f"{self.limits.max_daily_loss_pct:.2f}% — halting new buys."
            )
        # Peak-to-trough drawdown does NOT reset daily — this catches the slow
        # bleed that the daily-loss limit structurally misses.
        dd = self.state.drawdown_pct(account.equity)
        if dd >= self.limits.max_drawdown_pct:
            return True, (
                f"Drawdown {dd:.2f}% from peak ${self.state.peak_equity:,.0f} "
                f">= limit {self.limits.max_drawdown_pct:.2f}% — halting new buys."
            )
        if len(account.positions) >= self.limits.max_open_positions:
            return True, (
                f"At max open positions ({self.limits.max_open_positions})."
            )
        return False, ""

    # -- per-proposal evaluation ------------------------------------------- #
    def evaluate(
        self, proposal: TradeProposal, account: AccountSnapshot, price: float,
        volatility: float | None = None, pending_buy_notional: float = 0.0,
        days_to_earnings: int | None = None,
    ) -> RiskDecision:
        """`price` is the current market price for proposal.symbol. `volatility`
        is the symbol's annualized realized vol (fraction, e.g. 0.45) used for
        vol-targeted sizing. `pending_buy_notional` is the $ of already-open
        (unfilled) BUY orders for this symbol, so repeated cycles can't stack
        duplicate buys past the exposure cap. `days_to_earnings` is calendar days
        until the symbol's next earnings report (None if unknown) for the
        earnings-blackout guard. All from the execution client."""
        if proposal.action is Action.HOLD:
            return self._reject(proposal, "HOLD — no action.")
        if proposal.action is Action.SELL:
            return self._evaluate_sell(proposal, account)
        return self._evaluate_buy(
            proposal, account, price, volatility, pending_buy_notional, days_to_earnings
        )

    # -- survival-first sizing: vol-targeted, fractional-Kelly -------------- #
    def _sized_weight_pct(self, conviction: float, volatility: float | None) -> float:
        """Intended position weight BEFORE hard caps. Scales position size down
        for higher volatility and lower conviction, so a fixed vol budget is
        spread across names. The hard max_position_pct still bounds the result."""
        if self.limits.kelly_fraction <= 0:
            return self.limits.max_position_pct   # sizing model disabled
        # Fail SAFE on missing vol: assume a high vol so we size DOWN, not up.
        vol = volatility if (volatility and volatility > 0) else _ASSUMED_VOL_WHEN_UNKNOWN
        target = self.limits.target_annual_vol_pct / 100.0
        vol_ratio = min(target / vol, 1.5)   # cap upsizing on calm names
        sized_frac = self.limits.kelly_fraction * conviction * vol_ratio
        return sized_frac * 100.0

    # -- sells: always allowed (risk reduction), size = what we hold -------- #
    def _evaluate_sell(
        self, proposal: TradeProposal, account: AccountSnapshot
    ) -> RiskDecision:
        pos = account.position_for(proposal.symbol)
        if not pos or pos.qty <= 0:
            return self._reject(proposal, "No long position to sell.")
        return RiskDecision(
            proposal=proposal,
            verdict=RiskVerdict.APPROVED,
            approved_qty=pos.qty,
            approved_notional=pos.market_value,
            reason="Closing existing position.",
        )

    # -- buys: the heavily-guarded path ------------------------------------ #
    def _evaluate_buy(
        self, proposal: TradeProposal, account: AccountSnapshot, price: float,
        volatility: float | None = None, pending_buy_notional: float = 0.0,
        days_to_earnings: int | None = None,
    ) -> RiskDecision:
        halted, why = self.trading_halted(account)
        if halted:
            return self._reject(proposal, why)

        # Earnings-blackout guard: refuse NEW buys within N days of a scheduled
        # report. Gap risk through the print dwarfs the stop, so a tight stop gives
        # false comfort. Fail OPEN — only block on a date we actually have.
        blackout = self.limits.earnings_blackout_days
        if (
            blackout > 0
            and days_to_earnings is not None
            and 0 <= days_to_earnings <= blackout
        ):
            return self._reject(
                proposal,
                f"Earnings in {days_to_earnings}d (<= {blackout}d blackout) — "
                "gap risk dwarfs the stop; no new buy.",
            )

        equity = account.equity
        if equity <= 0:
            return self._reject(proposal, "Non-positive equity.")

        if price <= 0:
            return self._reject(proposal, "No current price available.")

        # Liquidity guard: refuse cheap/illiquid names where market orders bleed
        # to slippage and stops gap straight through.
        if price < self.limits.min_trade_price_usd:
            return self._reject(
                proposal,
                f"Price ${price:.2f} below min ${self.limits.min_trade_price_usd:.2f} "
                f"(liquidity guard).",
            )

        # 1) Take the SMALLEST of: what the LLM wants, what vol-targeted
        #    fractional-Kelly sizing allows, and the hard single-position cap.
        sized_pct = self._sized_weight_pct(proposal.conviction, volatility)
        weight_pct = min(
            proposal.target_weight_pct, sized_pct, self.limits.max_position_pct
        )
        target_notional = equity * (weight_pct / 100.0)

        # 2) Respect total per-symbol exposure. Count BOTH the filled holding
        #    AND any open (unfilled) buy orders — otherwise repeated decision
        #    cycles stack duplicate buys before the first one fills and blow
        #    past the cap.
        existing = account.position_for(proposal.symbol)
        existing_val = existing.market_value if existing else 0.0
        committed_val = existing_val + max(0.0, pending_buy_notional)
        max_symbol_val = equity * (self.limits.max_symbol_exposure_pct / 100.0)
        room = max_symbol_val - committed_val
        if room <= 0:
            return self._reject(
                proposal,
                f"Already at/over {self.limits.max_symbol_exposure_pct:.0f}% "
                f"exposure cap for {proposal.symbol} "
                f"(held ${existing_val:,.0f} + pending ${pending_buy_notional:,.0f}).",
            )
        target_notional = min(target_notional, room)

        # 3) Respect the cash buffer — never spend the reserve.
        min_cash = equity * (self.limits.min_cash_buffer_pct / 100.0)
        deployable = max(0.0, account.cash - min_cash)
        deployable = min(deployable, account.buying_power)
        target_notional = min(target_notional, deployable)

        # 4) Convert the capped dollar budget into a quantity. With fractional
        #    enabled (small accounts) we can deploy any budget >= the min order;
        #    otherwise we floor to whole shares and need at least one. Execution
        #    decides whole-share-bracket vs. fractional-notional from this qty.
        if self.limits.fractional_enabled:
            if target_notional < self.limits.min_order_usd:
                return self._reject(
                    proposal,
                    f"Budget ${target_notional:,.2f} below min order "
                    f"${self.limits.min_order_usd:.2f} after buffers/caps.",
                )
            qty = round(target_notional / price, 6)  # fractional shares
        else:
            if target_notional < price:  # can't even afford one whole share
                return self._reject(
                    proposal, "Insufficient deployable cash after buffers/caps."
                )
            qty = float(int(target_notional / price))  # whole shares, conservative
        if qty <= 0:
            return self._reject(proposal, "Sizing rounded to zero shares.")

        approved_notional = qty * price
        requested_notional = equity * (proposal.target_weight_pct / 100.0)
        resized = approved_notional < requested_notional * 0.999

        stop = proposal.stop_loss_pct or self.limits.default_stop_loss_pct
        take = proposal.take_profit_pct or self.limits.default_take_profit_pct

        return RiskDecision(
            proposal=proposal,
            verdict=RiskVerdict.RESIZED if resized else RiskVerdict.APPROVED,
            approved_qty=qty,
            approved_notional=approved_notional,
            stop_loss_pct=stop,
            take_profit_pct=take,
            reason=(
                f"Sized to {qty:g} sh (${approved_notional:,.0f}) within caps."
                + (" Reduced from request." if resized else "")
            ),
        )

    # -- options: defined-risk premium gate -------------------------------- #
    def evaluate_option(
        self, proposal: TradeProposal, account: AccountSnapshot,
        est_premium_per_contract: float,
    ) -> RiskDecision:
        """Size a defined-risk options play by capped DEBIT. Max loss on a long
        option / debit spread is the premium paid, so we bound that premium to a
        small % of equity. Rejects everything if options are disabled."""
        if not self.limits.options_enabled:
            return self._reject(proposal, "Options trading disabled (OPTIONS_ENABLED=off).")
        if self.kill_switch:
            return self._reject(proposal, "KILL_SWITCH is on — no new positions.")
        if proposal.option_strategy is None or not proposal.option_legs:
            return self._reject(proposal, "Option proposal missing strategy/legs.")
        ok, why = self._legs_are_defined_risk(proposal)
        if not ok:
            return self._reject(proposal, why)
        if est_premium_per_contract <= 0:
            # A debit means net premium PAID; <=0 means a net credit, i.e. a
            # short-premium structure whose max loss is NOT the debit. Refuse.
            return self._reject(
                proposal, "Net credit / no debit — not a bounded-loss debit play."
            )

        equity = account.equity
        cap = equity * (self.limits.max_option_premium_pct / 100.0)
        if proposal.max_premium_usd is not None:
            cap = min(cap, proposal.max_premium_usd)

        # premium quoted per share; one contract = 100 shares
        per_contract_cost = est_premium_per_contract * 100.0
        contracts = int(cap / per_contract_cost)
        if contracts < 1:
            return self._reject(
                proposal,
                f"Premium ${per_contract_cost:,.0f}/contract exceeds "
                f"${cap:,.0f} options budget.",
            )
        spent = contracts * per_contract_cost
        return RiskDecision(
            proposal=proposal,
            verdict=RiskVerdict.APPROVED,
            approved_qty=float(contracts),
            approved_notional=spent,
            reason=f"{contracts} contract(s), ${spent:,.0f} debit (cap ${cap:,.0f}).",
        )

    # -- option structure safety ------------------------------------------- #
    @staticmethod
    def _legs_are_defined_risk(proposal: TradeProposal) -> tuple[bool, str]:
        """Verify the legs actually CAN'T lose more than the debit, instead of
        trusting the declared strategy. The danger is a short leg that isn't
        fully covered by a long leg of the same right (a naked or ratio short =
        unbounded / large loss). Invariant: per right (call/put), total long
        contracts must be >= total short contracts. Also require exactly the
        legs each named strategy should have."""
        legs = proposal.option_legs
        long_calls = sum(l.ratio for l in legs if l.right.lower().startswith("c") and l.side is Action.BUY)
        short_calls = sum(l.ratio for l in legs if l.right.lower().startswith("c") and l.side is Action.SELL)
        long_puts = sum(l.ratio for l in legs if l.right.lower().startswith("p") and l.side is Action.BUY)
        short_puts = sum(l.ratio for l in legs if l.right.lower().startswith("p") and l.side is Action.SELL)

        if short_calls > long_calls:
            return False, (
                f"Uncovered short calls ({short_calls} short > {long_calls} long) "
                f"— unbounded risk, refused."
            )
        if short_puts > long_puts:
            return False, (
                f"Uncovered short puts ({short_puts} short > {long_puts} long) "
                f"— large risk, refused."
            )

        # Shape check per declared strategy so a mislabeled structure is caught.
        strat = proposal.option_strategy
        n = len(legs)
        if strat in (OptionStrategy.LONG_CALL, OptionStrategy.LONG_PUT):
            if n != 1 or legs[0].side is not Action.BUY:
                return False, f"{strat.value} must be a single long leg."
        elif strat in (OptionStrategy.BULL_CALL_SPREAD, OptionStrategy.BEAR_PUT_SPREAD):
            if n != 2 or long_calls + long_puts < 1 or short_calls + short_puts < 1:
                return False, f"{strat.value} must be one long + one short leg."
        return True, ""

    # -- helpers ------------------------------------------------------------ #
    @staticmethod
    def _reject(proposal: TradeProposal, reason: str) -> RiskDecision:
        log.info("REJECT %s %s: %s", proposal.action.value, proposal.symbol, reason)
        return RiskDecision(
            proposal=proposal, verdict=RiskVerdict.REJECTED, reason=reason
        )
