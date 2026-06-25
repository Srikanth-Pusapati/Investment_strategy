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
    RiskDecision,
    RiskVerdict,
    TradeProposal,
)

log = logging.getLogger("risk")


class RiskManager:
    def __init__(self, limits: RiskLimits, kill_switch: bool = False):
        self.limits = limits
        self.kill_switch = kill_switch

    # -- top-level halts ---------------------------------------------------- #
    def trading_halted(self, account: AccountSnapshot) -> tuple[bool, str]:
        """Account-wide reasons to block ALL new buying. Sells/exits still allowed."""
        if self.kill_switch:
            return True, "KILL_SWITCH is on — no new positions."
        loss_pct = -account.day_pl_pct  # positive number when losing
        if loss_pct >= self.limits.max_daily_loss_pct:
            return True, (
                f"Daily loss {loss_pct:.2f}% >= limit "
                f"{self.limits.max_daily_loss_pct:.2f}% — halting new buys."
            )
        if len(account.positions) >= self.limits.max_open_positions:
            return True, (
                f"At max open positions ({self.limits.max_open_positions})."
            )
        return False, ""

    # -- per-proposal evaluation ------------------------------------------- #
    def evaluate(
        self, proposal: TradeProposal, account: AccountSnapshot, price: float,
        volatility: float | None = None,
    ) -> RiskDecision:
        """`price` is the current market price for proposal.symbol. `volatility`
        is the symbol's annualized realized vol (fraction, e.g. 0.45) used for
        vol-targeted sizing. Both from the execution client. Required for buys."""
        if proposal.action is Action.HOLD:
            return self._reject(proposal, "HOLD — no action.")
        if proposal.action is Action.SELL:
            return self._evaluate_sell(proposal, account)
        return self._evaluate_buy(proposal, account, price, volatility)

    # -- survival-first sizing: vol-targeted, fractional-Kelly -------------- #
    def _sized_weight_pct(self, conviction: float, volatility: float | None) -> float:
        """Intended position weight BEFORE hard caps. Scales position size down
        for higher volatility and lower conviction, so a fixed vol budget is
        spread across names. The hard max_position_pct still bounds the result."""
        if self.limits.kelly_fraction <= 0:
            return self.limits.max_position_pct   # sizing model disabled
        vol_ratio = 1.0
        if volatility and volatility > 0:
            target = self.limits.target_annual_vol_pct / 100.0
            vol_ratio = min(target / volatility, 1.5)   # cap upsizing on calm names
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
        volatility: float | None = None,
    ) -> RiskDecision:
        halted, why = self.trading_halted(account)
        if halted:
            return self._reject(proposal, why)

        equity = account.equity
        if equity <= 0:
            return self._reject(proposal, "Non-positive equity.")

        if price <= 0:
            return self._reject(proposal, "No current price available.")

        # 1) Take the SMALLEST of: what the LLM wants, what vol-targeted
        #    fractional-Kelly sizing allows, and the hard single-position cap.
        sized_pct = self._sized_weight_pct(proposal.conviction, volatility)
        weight_pct = min(
            proposal.target_weight_pct, sized_pct, self.limits.max_position_pct
        )
        target_notional = equity * (weight_pct / 100.0)

        # 2) Respect total per-symbol exposure (existing holding counts).
        existing = account.position_for(proposal.symbol)
        existing_val = existing.market_value if existing else 0.0
        max_symbol_val = equity * (self.limits.max_symbol_exposure_pct / 100.0)
        room = max_symbol_val - existing_val
        if room <= 0:
            return self._reject(
                proposal,
                f"Already at/over {self.limits.max_symbol_exposure_pct:.0f}% "
                f"exposure cap for {proposal.symbol}.",
            )
        target_notional = min(target_notional, room)

        # 3) Respect the cash buffer — never spend the reserve.
        min_cash = equity * (self.limits.min_cash_buffer_pct / 100.0)
        deployable = max(0.0, account.cash - min_cash)
        deployable = min(deployable, account.buying_power)
        target_notional = min(target_notional, deployable)

        if target_notional < price:  # can't even afford one share
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
        if est_premium_per_contract <= 0:
            return self._reject(proposal, "No premium estimate for option.")

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

    # -- helpers ------------------------------------------------------------ #
    @staticmethod
    def _reject(proposal: TradeProposal, reason: str) -> RiskDecision:
        log.info("REJECT %s %s: %s", proposal.action.value, proposal.symbol, reason)
        return RiskDecision(
            proposal=proposal, verdict=RiskVerdict.REJECTED, reason=reason
        )
