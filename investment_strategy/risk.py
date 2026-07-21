"""RiskManager — the deterministic gate every trade must pass.

Design principle: Claude PROPOSES, RiskManager DISPOSES. The LLM's numbers are
treated as untrusted input. This module enforces hard caps that the model
cannot override, regardless of how confident its rationale sounds. If you only
trust one file in this repo, trust this one — read it before going live.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

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

# FINRA Pattern-Day-Trader minimum equity. Below this, a margin account that day
# trades too often gets flagged and restricted to closing-only. Cash accounts are
# exempt (they report pattern_day_trader=False / daytrade_count=0).
_PDT_MIN_EQUITY = 25_000.0

# sqrt(252): converts annualized vol back to a daily sigma for the vol-scaled
# stop (R.1) — the inverse of the annualization the vol input arrived with.
_TRADING_DAYS_SQRT = 252 ** 0.5


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
        pdt_block, pdt_why = self._pdt_block(account)
        if pdt_block:
            return True, pdt_why
        return False, ""

    # -- pattern-day-trader guard (small margin accounts) ------------------- #
    def _pdt_block(self, account: AccountSnapshot) -> tuple[bool, str]:
        """Block NEW opening buys when a sub-$25k MARGIN account is at/over the PDT
        line, so an incidental same-day stop can't flag it and freeze it to
        closing-only. Cash accounts report pattern_day_trader=False / daytrade
        count 0, so this is inert for them (the recommended setup for small size)."""
        if not self.limits.pdt_guard_enabled or account.equity >= _PDT_MIN_EQUITY:
            return False, ""
        if account.pattern_day_trader:
            return True, (
                f"PDT-flagged under ${_PDT_MIN_EQUITY:,.0f} "
                f"(equity ${account.equity:,.0f}) — opening new positions is "
                "restricted. Use a CASH account for small balances (PDT-exempt)."
            )
        if account.daytrade_count >= self.limits.max_day_trades_under_25k:
            return True, (
                f"{account.daytrade_count} day-trades in 5d at the PDT line under "
                f"${_PDT_MIN_EQUITY:,.0f} — pausing new buys to avoid a "
                "pattern-day-trader flag (closing still allowed)."
            )
        return False, ""

    # -- per-proposal evaluation ------------------------------------------- #
    def evaluate(
        self, proposal: TradeProposal, account: AccountSnapshot, price: float,
        volatility: float | None = None, pending_buy_notional: float = 0.0,
        days_to_earnings: int | None = None,
        sector: str | None = None, sector_exposure_usd: float = 0.0,
        regime_multiplier: float = 1.0,
        max_held_corr: float | None = None, corr_symbol: str = "",
        cycle_budget_cap: float | None = None,
        corr_data_missing: bool = False,
        tech: dict | None = None,
        composite_score: float | None = None,
    ) -> RiskDecision:
        """`price` is the current market price for proposal.symbol. `volatility`
        is the symbol's annualized realized vol (fraction, e.g. 0.45) used for
        vol-targeted sizing. `pending_buy_notional` is the $ of already-open
        (unfilled) BUY orders for this symbol, so repeated cycles can't stack
        duplicate buys past the exposure cap. `days_to_earnings` is calendar days
        until the symbol's next earnings report (None if unknown) for the
        earnings-blackout guard. `sector` is the symbol's sector and
        `sector_exposure_usd` is the $ already held in that sector, for the sector
        concentration cap. `regime_multiplier` (0..1) scales position size down in a
        risk-off market backdrop. `max_held_corr` is the highest daily-return
        correlation between this symbol and any already-held satellite (None =
        unknown -> guard skipped, fail-open) and `corr_symbol` names that
        position, for the pairwise-correlation guard (R.2). All from the
        execution client / orchestrator. `cycle_budget_cap` is this proposal's
        fair share of the cycle's deployable cash when several buys compete in
        one cycle (None = no share cap), so the first buy can't starve the rest
        to "Budget $0.00". `corr_data_missing` is True when we HOLD satellites
        but couldn't compute correlations against them (data outage) — a blind
        guard sizes down instead of failing open. `tech` is the symbol's
        technical-signal data dict (rsi14 / ext_atr / ext_pct_sma20) for the
        anti-chasing overextension gate; `composite_score` is our deterministic
        weighted signal index for the opt-in composite floor. Both fail open
        when None."""
        if proposal.action is Action.HOLD:
            # Same REJECTED verdict (nothing downstream may execute a HOLD),
            # but without _reject's "REJECT hold X" log line — a no-op HOLD is
            # not a failure, and the orchestrator logs it once, quietly.
            return RiskDecision(
                proposal=proposal, verdict=RiskVerdict.REJECTED,
                reason="HOLD — no action.",
            )
        if proposal.action is Action.SELL:
            return self._evaluate_sell(proposal, account)
        return self._evaluate_buy(
            proposal, account, price, volatility, pending_buy_notional,
            days_to_earnings, sector, sector_exposure_usd, regime_multiplier,
            max_held_corr, corr_symbol, cycle_budget_cap, corr_data_missing,
            tech, composite_score,
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

    # -- vol-scaled ("ATR-style") stop / take distances (R.1) --------------- #
    def _exit_levels(
        self, proposal: TradeProposal, volatility: float | None,
    ) -> tuple[float, float]:
        """Stop/take distances (%) for this buy. With VOL_STOPS_ENABLED and a
        known realized vol, the stop scales to the name's daily sigma — tight on
        quiet names, wide on volatile ones — and the take is a fixed reward:risk
        multiple of it. Both are DETERMINISTIC (they override the LLM's proposed
        levels; Claude's numbers are untrusted input) and the stop is clamped to
        [vol_stop_min_pct, vol_stop_max_pct]. Interplay that makes this safe at
        full Kelly: the per-trade $-risk cap (2d) divides by the stop width, so
        a wider stop buys FEWER shares — dollar risk per position stays ~flat.
        Falls back to proposal-else-default when disabled or vol is unknown
        (a data outage must not silently change the exit regime)."""
        lim = self.limits
        if lim.vol_stops_enabled and volatility and volatility > 0:
            daily_sigma_pct = volatility / (_TRADING_DAYS_SQRT) * 100.0
            stop = min(
                max(lim.vol_stop_mult * daily_sigma_pct, lim.vol_stop_min_pct),
                lim.vol_stop_max_pct,
            )
            return stop, stop * lim.vol_stop_take_ratio
        return (
            proposal.stop_loss_pct or lim.default_stop_loss_pct,
            proposal.take_profit_pct or lim.default_take_profit_pct,
        )

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
        sector: str | None = None, sector_exposure_usd: float = 0.0,
        regime_multiplier: float = 1.0,
        max_held_corr: float | None = None, corr_symbol: str = "",
        cycle_budget_cap: float | None = None,
        corr_data_missing: bool = False,
        tech: dict | None = None,
        composite_score: float | None = None,
    ) -> RiskDecision:
        halted, why = self.trading_halted(account)
        if halted:
            return self._reject(proposal, why)

        # The slot cap blocks NEW names only. A top-up of a held symbol reuses
        # its position row, so counting it against the cap froze ALL buying
        # once the book filled (2026-07-13: NU top-up rejected at 15/15 while
        # the churn guard's own message said the next add was fine in 4h).
        # Option rows don't count either: they have their own concurrency cap
        # (max_option_positions) and premium cap, and letting a ~1%-of-equity
        # debit eat an equity slot starves the equity book.
        equity_rows = [p for p in account.positions if not p.is_option]
        if (
            len(equity_rows) >= self.limits.max_open_positions
            and account.position_for(proposal.symbol) is None
        ):
            return self._reject(
                proposal,
                f"At max open positions ({self.limits.max_open_positions}).",
            )

        # Conviction floor: a barely-there idea that only clears the friction floor
        # still pays spread + slippage and dilutes the book. Require a real edge
        # before risking capital (1B.9). Inert at 0.
        if proposal.conviction < self.limits.min_conviction:
            return self._reject(
                proposal,
                f"Conviction {proposal.conviction:.2f} below floor "
                f"{self.limits.min_conviction:.2f} — no real edge; skip.",
            )

        # Composite floor (opt-in): the deterministic weighted signal index
        # must corroborate the LLM's conviction. Fails open on None — the
        # composite is best-effort context, not a required feed.
        if (
            self.limits.composite_gate_enabled
            and composite_score is not None
            and composite_score < self.limits.min_composite_score
        ):
            return self._reject(
                proposal,
                f"Composite {composite_score:+.2f} below floor "
                f"{self.limits.min_composite_score:+.2f} — the weighted signals "
                "don't corroborate the conviction.",
            )

        # Anti-chasing overextension gate (week of 2026-07-13: 68% of realized
        # losses were momentum entries near local tops — CDW/SOFI/PATH — that
        # ran straight to their stops). Two triggers:
        #   (a) hot AND extended: RSI >= overext_rsi AND price >= overext_atr_mult
        #       ATRs above the 20d SMA (% fallback when ATR is unavailable);
        #   (b) EXTREME extension alone: >= overext_extreme_atr_mult ATRs over
        #       the 20d SMA fires regardless of RSI — the actual Jul-13 losers
        #       entered at RSI 61-64 (under any sane RSI floor) but 3.4-4.0 ATRs
        #       extended; the RSI leg must not muzzle a screaming extension leg.
        # Block it, or halve the size (haircut mode) so a wrong top costs half.
        # Fails open on missing technicals — a yfinance outage must not freeze
        # all buying.
        overext_note = ""
        overext_mult = 1.0
        if self.limits.overextension_gate_enabled and tech:
            rsi = tech.get("rsi14")
            ext_atr = tech.get("ext_atr")
            ext_pct = tech.get("ext_pct_sma20")
            extended = (
                (ext_atr is not None and ext_atr >= self.limits.overext_atr_mult)
                or (
                    ext_atr is None
                    and ext_pct is not None
                    and ext_pct >= self.limits.overext_pct
                )
            )
            hot_and_extended = (
                rsi is not None and rsi >= self.limits.overext_rsi and extended
            )
            extreme = (
                self.limits.overext_extreme_atr_mult > 0
                and ext_atr is not None
                and ext_atr >= self.limits.overext_extreme_atr_mult
            )
            if hot_and_extended or extreme:
                how_far = (
                    f"{ext_atr:.1f}xATR" if ext_atr is not None
                    else f"{ext_pct:.1f}%"
                )
                why = (
                    f"{how_far} above the 20d SMA (extreme extension)"
                    if extreme and not hot_and_extended
                    else f"RSI {rsi:.0f} and {how_far} above the 20d SMA"
                )
                # The EXTREME leg has its own mode: a >=Nx-ATR screaming
                # extension is a different risk than a mild hot-and-extended
                # entry, and defaults to a hard block (the shared "haircut" mode
                # only halved it — CVX still bought $2,799 at 3.2xATR Jul 17).
                mode = (
                    self.limits.overext_extreme_mode if extreme
                    else self.limits.overextension_mode
                )
                if mode == "block":
                    return self._reject(
                        proposal,
                        f"Overextended: {why} — chasing a local top; wait "
                        "for a pullback or base.",
                    )
                overext_mult = max(0.0, min(1.0, self.limits.overext_haircut))
                overext_note = f" Overextension haircut x{overext_mult:g} ({why})."

        # Churn guards (2026-07-06 log: LLY bought 10x in one day, every 30-min
        # cycle, while all other buys starved). (a) Top-up spacing: a name bought
        # less than min_add_interval_hours ago is not bought again — adds must be
        # spaced decisions, not a per-cycle reflex. (b) Re-entry cooldown: a name
        # we EXITED less than reentry_cooldown_hours ago is not re-entered fresh —
        # an instant re-buy pays the spread twice and usually chases the same
        # falling knife the stop just saved us from. Both fail open on missing
        # clocks (first entry ever) and are inert at 0.
        if self.limits.min_add_interval_hours > 0:
            since_buy = self.state.hours_since_buy(proposal.symbol)
            if since_buy is not None and since_buy < self.limits.min_add_interval_hours:
                return self._reject(
                    proposal,
                    f"Bought {proposal.symbol} {since_buy:.1f}h ago — top-ups are "
                    f"spaced {self.limits.min_add_interval_hours:g}h apart (churn guard).",
                )
        if (
            self.limits.reentry_cooldown_hours > 0
            and account.position_for(proposal.symbol) is None
        ):
            since_exit = self.state.hours_since_exit(proposal.symbol)
            if since_exit is not None and since_exit < self.limits.reentry_cooldown_hours:
                return self._reject(
                    proposal,
                    f"Exited {proposal.symbol} {since_exit:.1f}h ago — re-entry "
                    f"waits {self.limits.reentry_cooldown_hours:g}h (churn guard).",
                )

        # Daily concentration brake, count leg (2026-07-06: 10 LLY buys in one
        # session). Hard cap on submitted buy orders per symbol per ET trading
        # day; the dollar leg lives below once equity is known. Inert at 0.
        if self.limits.max_daily_buys_per_symbol > 0:
            n_today = self.state.daily_symbol_buys(proposal.symbol)
            if n_today >= self.limits.max_daily_buys_per_symbol:
                return self._reject(
                    proposal,
                    f"Already bought {proposal.symbol} {n_today}x today (daily cap "
                    f"{self.limits.max_daily_buys_per_symbol}/symbol) — no more "
                    "buys today (concentration guard).",
                )

        # Top-up evidence gate: an ADD to a held name must show conviction above
        # the prior entry's by topup_min_conviction_delta. Re-proposing the same
        # number every cycle is a reflex ("adding to a winner"), not new
        # evidence. Fails open on a missing prior; inert at 0.
        if (
            self.limits.topup_min_conviction_delta > 0
            and account.position_for(proposal.symbol) is not None
        ):
            prev = self.state.last_buy_conviction(proposal.symbol)
            if prev is not None and (
                proposal.conviction < prev + self.limits.topup_min_conviction_delta
            ):
                return self._reject(
                    proposal,
                    f"Top-up conviction {proposal.conviction:.2f} shows no new "
                    f"edge over prior entry {prev:.2f} (needs "
                    f"+{self.limits.topup_min_conviction_delta:g}) — 'adding to "
                    "a winner' is not a signal.",
                )

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

        # Daily concentration brake, dollar leg: cap the $ deployed into ONE
        # symbol per ET trading day. Headroom under the min order is a REJECT,
        # not a resize — resizing-to-headroom is exactly the dust-grinding the
        # 2026-07-06 log showed ($2-$8 orders against a full cap). Inert at 0.
        day_room: float | None = None
        if self.limits.max_daily_symbol_deploy_pct > 0:
            day_cap = equity * (self.limits.max_daily_symbol_deploy_pct / 100.0)
            spent = self.state.daily_symbol_spend(proposal.symbol)
            day_room = day_cap - spent
            min_order = max(
                self.limits.min_order_usd,
                equity * (self.limits.min_order_pct / 100.0),
            )
            if day_room <= min_order:
                return self._reject(
                    proposal,
                    f"${spent:,.0f} already deployed into {proposal.symbol} today "
                    f"vs ${day_cap:,.0f} daily ceiling (headroom ${max(0.0, day_room):,.0f} "
                    "< min order) — buys resume next trading day (concentration guard).",
                )

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

        stop_pct, take_pct = self._exit_levels(proposal, volatility)

        # Cost / slippage edge floor: a trade whose profit target can't clear the
        # round-trip friction (spread + slippage) is negative-expectancy the moment
        # it fills — on a tiny float that friction is the whole game. Estimate the
        # round-trip cost as 2x the one-way slippage estimate and require the
        # take-profit target to beat it by MIN_EDGE_RATIO. Inert for liquid names
        # with normal targets; it catches dust/tight-target degenerates. Size-
        # independent (it's a % comparison), so it's a gate, not a resize.
        if self.limits.est_slippage_pct > 0 and take_pct > 0:
            round_trip_cost_pct = 2.0 * self.limits.est_slippage_pct
            if take_pct < round_trip_cost_pct * self.limits.min_edge_ratio:
                return self._reject(
                    proposal,
                    f"Take-profit {take_pct:.2f}% can't clear ~{round_trip_cost_pct:.2f}% "
                    f"round-trip cost by {self.limits.min_edge_ratio:g}x — friction "
                    "eats the edge.",
                )

        # 1) Take the SMALLEST of: what the LLM wants, what vol-targeted
        #    fractional-Kelly sizing allows, and the hard single-position cap.
        sized_pct = self._sized_weight_pct(proposal.conviction, volatility)
        weight_pct = min(
            proposal.target_weight_pct, sized_pct, self.limits.max_position_pct
        )
        target_notional = equity * (weight_pct / 100.0)

        # 1a) Anti-chasing haircut (computed above): halve what an extended
        #     entry may deploy, BEFORE the additive caps below shave it further.
        target_notional *= overext_mult

        # 1b) Market-regime scaling — shrink size in a risk-off backdrop (SPY below
        #     its 200dma / elevated VIX). 1.0 in a calm uptrend; clamped to [0,1]
        #     so it can only ever REDUCE size, never inflate it.
        if self.limits.regime_filter_enabled:
            target_notional *= max(0.0, min(1.0, regime_multiplier))

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

        # 2e) Daily per-symbol ceiling clamp (dollar leg computed above — the
        #     exhausted case already rejected; here we just cap the remainder).
        if day_room is not None:
            target_notional = min(target_notional, day_room)

        # 2b) Sector concentration cap — keep the discovery scanner from quietly
        #     stacking several correlated names (e.g. all big-tech) into one bet.
        #     sector_exposure_usd is the $ already held in this name's sector.
        #     Fail-closed on missing sector data: size down by missing_data_mult
        #     instead of silently skipping the guard (the sector cap is blind, so
        #     "blinder = smaller" is the safe posture).
        if self.limits.max_sector_exposure_pct > 0:
            _mdm = max(0.0, min(1.0, self.limits.missing_data_mult))
            if sector:
                max_sector_val = equity * (self.limits.max_sector_exposure_pct / 100.0)
                sector_room = max_sector_val - max(0.0, sector_exposure_usd)
                if sector_room <= 0:
                    return self._reject(
                        proposal,
                        f"At/over {self.limits.max_sector_exposure_pct:.0f}% sector cap "
                        f"for '{sector}' (held ${sector_exposure_usd:,.0f}).",
                    )
                target_notional = min(target_notional, sector_room)
            else:
                target_notional *= _mdm

        # 2b-ii) Pairwise-correlation guard (R.2) — the sector cap's finer-
        #     grained sibling. A NEW name whose daily returns track an already-
        #     held satellite is not diversification, it's the SAME bet wearing a
        #     different ticker; under the aggressive caps that quietly stacks one
        #     factor. The orchestrator computes max_held_corr against held names
        #     (core ETF excluded — satellites are MEANT to correlate with the
        #     index core); None (no data / nothing held) fails open. Adding to
        #     the SAME symbol is exempt upstream (a top-up isn't a new bet).
        #     Fail-closed on missing data when we DO hold satellites: size down.
        if self.limits.max_pairwise_corr > 0:
            if max_held_corr is not None and max_held_corr >= self.limits.max_pairwise_corr:
                return self._reject(
                    proposal,
                    f"Return correlation {max_held_corr:.2f} with held "
                    f"{corr_symbol or 'position'} >= {self.limits.max_pairwise_corr:.2f} "
                    "cap — effectively the same bet; diversify instead.",
                )
            if corr_data_missing:
                _mdm = max(0.0, min(1.0, self.limits.missing_data_mult))
                target_notional *= _mdm

        # 2c) No-leverage gross cap — never let TOTAL deployed exceed this % of
        #     equity. On a margin account (Alpaca offers ~2x buying power) this is
        #     the explicit guard that we never trade with borrowed money.
        gross_held = sum(p.market_value for p in account.positions)
        max_gross_val = equity * (self.limits.max_gross_exposure_pct / 100.0)
        gross_room = max_gross_val - gross_held - max(0.0, pending_buy_notional)
        if gross_room <= 0:
            return self._reject(
                proposal,
                f"At/over {self.limits.max_gross_exposure_pct:.0f}% gross-exposure "
                f"cap (deployed ${gross_held:,.0f} of ${max_gross_val:,.0f}; "
                "no leverage).",
            )
        target_notional = min(target_notional, gross_room)

        # 2d) Per-trade $-loss cap — bound the ABSOLUTE dollars at risk if the stop
        #     fires, independent of the % weight. The classic "risk 1% per trade"
        #     rule: notional * stop% must stay under equity * MAX_TRADE_RISK_PCT.
        #     This is what keeps a string of small losers survivable on the live
        #     float; a wide stop now SHRINKS the position instead of the dollar risk.
        if self.limits.max_trade_risk_pct > 0 and stop_pct > 0:
            max_risk_usd = equity * (self.limits.max_trade_risk_pct / 100.0)
            max_notional_by_risk = max_risk_usd / (stop_pct / 100.0)
            target_notional = min(target_notional, max_notional_by_risk)

        # 3) Respect the cash buffer — never spend the reserve.
        min_cash = equity * (self.limits.min_cash_buffer_pct / 100.0)
        deployable = max(0.0, account.cash - min_cash)
        deployable = min(deployable, account.buying_power)
        target_notional = min(target_notional, deployable)

        # 3b) Fair share of the CYCLE's cash when several buys compete. The
        #     orchestrator splits deployable cash across the cycle's buy
        #     proposals by conviction; without it, the first (highest-conviction)
        #     buy takes everything and every later proposal — however good —
        #     dies on "Budget $0.00" (the 2026-07-06 all-LLY failure).
        if cycle_budget_cap is not None:
            target_notional = min(target_notional, max(0.0, cycle_budget_cap))

        # 4) Convert the capped dollar budget into a quantity. Whole-shares mode
        #    (GA-2.3, default ON) floors DOWN so every entry can rest an
        #    exchange-side GTC bracket — a budget under one share is REJECTED,
        #    never downgraded to an unprotected fractional buy. With it off and
        #    fractional enabled (tiny accounts), any budget >= the min order
        #    deploys as a notional order whose only stop is the watchdog.
        if self.limits.whole_shares_only:
            if target_notional < price:
                return self._reject(
                    proposal,
                    f"Whole-shares mode: budget ${target_notional:,.2f} can't buy "
                    f"one share at ${price:,.2f} — no unbracketed fractional "
                    "fallback (GA-2.3).",
                )
            qty = float(int(target_notional / price))  # floor to whole shares
        elif self.limits.fractional_enabled:
            # Dust guard: the min order scales with equity (a $98k book firing a
            # $2 top-up pays spread for nothing — 2026-07-06 log) while the
            # absolute floor keeps a $500 float tradable.
            min_order = max(
                self.limits.min_order_usd,
                equity * (self.limits.min_order_pct / 100.0),
            )
            if target_notional < min_order:
                return self._reject(
                    proposal,
                    f"Budget ${target_notional:,.2f} below min order "
                    f"${min_order:,.2f} after buffers/caps.",
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

        return RiskDecision(
            proposal=proposal,
            verdict=RiskVerdict.RESIZED if resized else RiskVerdict.APPROVED,
            approved_qty=qty,
            approved_notional=approved_notional,
            stop_loss_pct=stop_pct,
            take_profit_pct=take_pct,
            reason=(
                f"Sized to {qty:g} sh (${approved_notional:,.0f}) within caps."
                + (" Reduced from request." if resized else "")
                + overext_note
            ),
        )

    # -- options: defined-risk premium gate -------------------------------- #
    def evaluate_option(
        self, proposal: TradeProposal, account: AccountSnapshot,
        est_premium_per_contract: float,
        leg_liquidity: list[dict] | None = None,
    ) -> RiskDecision:
        """Size a defined-risk options play by capped DEBIT. Max loss on a long
        option / debit spread is the premium paid, so we bound that premium to a
        small % of equity. Rejects everything if options are disabled.

        `leg_liquidity` is per-leg {'symbol', 'oi', 'rel_spread_pct'} context
        from OptionsHelper.leg_liquidity — optional, and None FIELDS fail open
        (the est_premium<=0 gate already refuses quote-less legs)."""
        if not self.limits.options_enabled:
            return self._reject(proposal, "Options trading disabled (OPTIONS_ENABLED=off).")
        # Same account-wide gate as equity buys: halt latch, kill switch, daily
        # loss, drawdown, PDT. An option debit is still a new position — it
        # must never open through a halt.
        halted, why = self.trading_halted(account)
        if halted:
            return self._reject(proposal, why)
        # NO global slot-cap check here — deliberately. The old strict cap
        # rejected every option play exactly when the LLM proposes them: the
        # prompt steers to defined-risk options when equity buys are capped, so
        # the book was 15/15 for all 8 option proposals of 2026-07-13/14 and
        # OPTIONS_ENABLED was structurally dead. An option debit is not an
        # equity-sized position: concurrency is bounded by its OWN gates below
        # (_under_option_position_cap underlyings + the ~1%-of-equity premium
        # cap), so worst-case marginal exposure is a few % of equity, and the
        # account-wide halt gates above still apply.
        if proposal.option_strategy is None or not proposal.option_legs:
            return self._reject(proposal, "Option proposal missing strategy/legs.")
        ok, why = self._legs_are_defined_risk(proposal)
        if not ok:
            return self._reject(proposal, why)
        ok, why = self._legs_dte_sane(proposal)
        if not ok:
            return self._reject(proposal, why)
        ok, why = self._under_option_position_cap(proposal, account)
        if not ok:
            return self._reject(proposal, why)
        ok, why = self._legs_liquid(leg_liquidity)
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

    # -- option expiry sanity ------------------------------------------------ #
    def _legs_dte_sane(self, proposal: TradeProposal) -> tuple[bool, str]:
        """Every leg's expiry inside [min_option_dte, max_option_dte]: too close
        and theta/assignment dominate any thesis; too far and the debit buys
        mostly time value the thesis window doesn't need. Verticals must share
        ONE expiry — a mislabeled diagonal has a different risk shape than the
        defined-risk check above assumed."""
        lo = getattr(self.limits, "min_option_dte", 7.0)
        hi = getattr(self.limits, "max_option_dte", 60.0)
        today = datetime.now(timezone.utc).date()
        for leg in proposal.option_legs:
            try:
                exp = datetime.strptime(leg.expiry, "%Y-%m-%d").date()
            except ValueError:
                return False, f"Unparseable leg expiry {leg.expiry!r}."
            dte = (exp - today).days
            if lo > 0 and dte < lo:
                return False, (
                    f"Leg expires {leg.expiry} ({dte}d out) < {lo:.0f}d minimum "
                    f"— too close to expiry."
                )
            if hi > 0 and dte > hi:
                return False, (
                    f"Leg expires {leg.expiry} ({dte}d out) > {hi:.0f}d maximum "
                    f"— too far-dated."
                )
        if len({leg.expiry for leg in proposal.option_legs}) > 1:
            return False, (
                "Vertical legs must share one expiry — a diagonal is not an "
                "approved defined-risk shape."
            )
        return True, ""

    # -- concurrent option-structure cap -------------------------------------- #
    def _under_option_position_cap(
        self, proposal: TradeProposal, account: AccountSnapshot,
    ) -> tuple[bool, str]:
        """Cap the number of distinct UNDERLYINGS with open option structures.
        Premium caps bound each play's loss; this bounds how many concurrent
        theta-decaying bets exist at once. Adding legs on an already-held
        underlying doesn't consume a new slot."""
        cap = int(getattr(self.limits, "max_option_positions", 3))
        if cap <= 0:
            return True, ""
        from .execution.options import parse_occ
        held = {
            occ[0] for p in account.positions if p.is_option
            for occ in [parse_occ(p.symbol)] if occ
        }
        if proposal.symbol not in held and len(held) >= cap:
            return False, (
                f"At max option positions ({cap} underlyings: "
                f"{', '.join(sorted(held))})."
            )
        return True, ""

    # -- option leg liquidity -------------------------------------------------- #
    def _legs_liquid(self, leg_liquidity: list[dict] | None) -> tuple[bool, str]:
        """Open-interest floor + bid-ask spread ceiling per leg. A leg that
        can't be exited near mid turns the premium cap into a fiction. None
        FIELDS fail open (only act on data we have — the earnings guard's
        rule); a missing list entirely means the caller had no data source."""
        if not leg_liquidity:
            return True, ""
        min_oi = getattr(self.limits, "min_option_open_interest", 100.0)
        max_spread = getattr(self.limits, "max_option_spread_pct", 10.0)
        for liq in leg_liquidity:
            sym = liq.get("symbol", "?")
            oi = liq.get("oi")
            if min_oi > 0 and oi is not None and oi < min_oi:
                return False, (
                    f"Leg {sym} open interest {oi:.0f} < {min_oi:.0f} floor "
                    f"— too illiquid to exit cleanly."
                )
            spread = liq.get("rel_spread_pct")
            if max_spread > 0 and spread is not None and spread > max_spread:
                return False, (
                    f"Leg {sym} bid-ask spread {spread:.1f}% > "
                    f"{max_spread:.1f}% cap — round-trip friction too high."
                )
        return True, ""

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
