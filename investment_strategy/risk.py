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

# Broad-index option underlyings (run-6 item 3): exempt from the single-name
# option gates (earnings blackout, OPTIONS_SINGLE_NAME_BULLISH). The
# configured core / hedge / proxy ETFs are added per call via `index_symbols`.
_INDEX_UNDERLYINGS = frozenset({"SPY", "QQQ", "IWM", "DIA"})

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

# Corroboration gate (Aug 12-21 forensic review): the SOFT signal families —
# smart-money follows with no independent view of the business or the tape.
# A fresh entry cited on exactly one of these, uncorroborated by
# fundamentals/news/technical, was the run's worst cohort (insider-cited
# -$18,681 across 13 trades; QNT/LFTO/INTC/F/AVBC all failed fast).
_SOFT_FAMILIES = frozenset({"insider", "congress", "options_flow"})
# Cited buckets that never COUNT as corroboration when judging soloness:
# "discovery" is the scanner that surfaced the name (the Jul-27 congress->
# DISCOVERY laundering — the same soft read wearing the scanner's label) and
# "composite" is the LLM citing the deterministic index itself.
_NON_CORROBORATING = frozenset({"discovery", "composite"})


def trail_geometry(limits, stop_pct: float) -> tuple[float, float]:
    """(arm_threshold_pct, giveback_pct) for a position's trailing stop.

    The single source of truth shared by the live watchdog and the backtest's
    mirror of it — the Jul-25 calibration found the fixed 3% giveback armed at
    any +3.1% peak and clipped every winner at ~+1% while vol-scaled stops
    risked 5-7% (median winner captured 8% of its target; payoff 0.56 vs the
    64% breakeven win rate that geometry demands).

    giveback = max(trail_giveback_pct, trail_giveback_r x stop)  — runners on
    volatile names get proportionally more room, like their stops do.
    arm      = max(giveback,           trail_arm_r      x stop)  — the trail
    only starts protecting once the peak has covered the position's own risk.

    `stop_pct` <= 0 (unknown — e.g. a position whose stop was never recorded)
    or both R knobs at 0 reproduce the legacy fixed-% behavior:
    arm == giveback == trail_giveback_pct.

    The stop input is CLAMPED to vol_stop_max_pct before scaling: when vol is
    unknown (fresh listings with <10 bars) the risk layer passes the LLM's
    proposed stop through unbounded, and a 20% stop would put the arm at 30%
    — a trail that never arms. The [--sweep-trail] evidence that blessed the
    R knobs only ever contained vol-clamped stops, so the geometry must not
    extrapolate beyond that regime.
    """
    giveback = limits.trail_giveback_pct
    arm = giveback
    if stop_pct and stop_pct > 0:
        cap = getattr(limits, "vol_stop_max_pct", 0.0)
        if cap and cap > 0:
            stop_pct = min(stop_pct, cap)
        r_gb = getattr(limits, "trail_giveback_r", 0.0)
        if r_gb > 0:
            giveback = max(giveback, r_gb * stop_pct)
            arm = giveback
        r_arm = getattr(limits, "trail_arm_r", 0.0)
        if r_arm > 0:
            arm = max(arm, r_arm * stop_pct)
    return arm, giveback


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
        regime_label: str = "",
        entry_families: set[str] | None = None,
        neg_families: dict | None = None,
        defensive_exempt_usd: float = 0.0,
        sell_events: tuple[str, ...] | None = None,
        stop_width_pct: float | None = None,
        book_beta_spy: float | None = None,
        candidate_beta: float | None = None,
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
        when None. `regime_label` drives the exposure ladder (risk-exposure
        clamp in neutral/risk-off) and `defensive_exempt_usd` is the market
        value of the defensive T-bill sleeve the ladder must not count as
        risk. `entry_families` are the cited signal families behind this
        proposal and `neg_families` maps family -> SourceStats for families
        with negative trailing expectancy (the expectancy gate); all default
        inert. `sell_events` are the deterministic event tags the
        orchestrator attached to a SELL (name-falling read, earnings, halt,
        regime flip — from code, never the model) and `stop_width_pct` is the
        position's planned stop width; both feed the LLM sell-authority gate
        (llm_sell_authority='events_only'). `book_beta_spy` is the book's
        current SPY-beta (portfolio/beta.py, None = no reading -> the beta
        cap is skipped) and `candidate_beta` the candidate's own shrunk
        SPY-beta (None = unknown -> assumed 1.0) for the book-beta cap
        (run-6 item 7b)."""
        if proposal.action is Action.HOLD:
            # Same REJECTED verdict (nothing downstream may execute a HOLD),
            # but without _reject's "REJECT hold X" log line — a no-op HOLD is
            # not a failure, and the orchestrator logs it once, quietly.
            return RiskDecision(
                proposal=proposal, verdict=RiskVerdict.REJECTED,
                reason="HOLD — no action.",
            )
        if proposal.action is Action.SELL:
            return self._evaluate_sell(
                proposal, account, sell_events=sell_events,
                stop_width_pct=stop_width_pct,
            )
        return self._evaluate_buy(
            proposal, account, price, volatility, pending_buy_notional,
            days_to_earnings, sector, sector_exposure_usd, regime_multiplier,
            max_held_corr, corr_symbol, cycle_budget_cap, corr_data_missing,
            tech, composite_score, regime_label=regime_label,
            entry_families=entry_families, neg_families=neg_families,
            defensive_exempt_usd=defensive_exempt_usd,
            book_beta_spy=book_beta_spy, candidate_beta=candidate_beta,
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
        tech: dict | None = None,
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
        (a data outage must not silently change the exit regime).

        STOP_COVER_EXTENSION (Jul 29): a breakout entry sitting ABOVE its 20d
        SMA by more than the sigma stop would rest its stop INSIDE the base it
        broke out from — ordinary reversion to the mean tags it at the low
        (NU: entered 6.8% over the SMA, stopped at -4.1%, the exact session
        low, then it bounced). Widen the stop to at least the extension (still
        clamped to the max) so only a move BELOW the mean stops it out; the
        $-risk cap shrinks the position to keep dollar risk unchanged."""
        lim = self.limits
        if lim.vol_stops_enabled and volatility and volatility > 0:
            daily_sigma_pct = volatility / (_TRADING_DAYS_SQRT) * 100.0
            stop = min(
                max(lim.vol_stop_mult * daily_sigma_pct, lim.vol_stop_min_pct),
                lim.vol_stop_max_pct,
            )
            if getattr(lim, "stop_cover_extension", False) and tech:
                ext_pct = tech.get("ext_pct_sma20")
                if ext_pct is not None and ext_pct > stop:
                    stop = min(float(ext_pct), lim.vol_stop_max_pct)
            return stop, stop * lim.vol_stop_take_ratio
        return (
            proposal.stop_loss_pct or lim.default_stop_loss_pct,
            proposal.take_profit_pct or lim.default_take_profit_pct,
        )

    # -- sells: size = what we hold; losers gated by sell authority --------- #
    def _evaluate_sell(
        self, proposal: TradeProposal, account: AccountSnapshot,
        sell_events: tuple[str, ...] | None = None,
        stop_width_pct: float | None = None,
    ) -> RiskDecision:
        pos = account.position_for(proposal.symbol)
        if not pos or pos.qty <= 0:
            return self._reject(proposal, "No long position to sell.")
        # Run-6 item 2 — LLM sell authority. Under 'events_only' the model may
        # not cut a LOSER short of its stop on a re-argued thesis: the
        # mechanical stack (bracket/vol stop, R-trail, time-stop, name-falling
        # defense) owns losing positions unless CODE attached a concrete event
        # tag. Winners and stop-reached positions pass exactly as before.
        authority = getattr(self.limits, "llm_sell_authority", "full") or "full"
        pl_pct = float(pos.unrealized_pl_pct or 0.0)
        if authority == "events_only" and pl_pct < 0:
            stop_w = float(stop_width_pct or 0.0)
            reached_stop = stop_w > 0 and pl_pct <= -stop_w
            if not reached_stop and not sell_events:
                reason = (
                    f"SELL AUTHORITY: {proposal.symbol} decision-sell rejected "
                    f"(unrealized {pl_pct:+.1f}%, "
                    f"stop {('-%.1f%%' % stop_w) if stop_w > 0 else 'n/a'}, "
                    "no event) — held to the mechanical stack"
                )
                log.warning("%s", reason)
                return RiskDecision(
                    proposal=proposal, verdict=RiskVerdict.REJECTED,
                    reason=reason,
                )
            if sell_events and not reached_stop:
                log.info(
                    "SELL AUTHORITY: %s decision-sell allowed on event(s) %s "
                    "(unrealized %+.1f%%).",
                    proposal.symbol, ", ".join(sell_events), pl_pct,
                )
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
        regime_label: str = "",
        entry_families: set[str] | None = None,
        neg_families: dict | None = None,
        defensive_exempt_usd: float = 0.0,
        book_beta_spy: float | None = None,
        candidate_beta: float | None = None,
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

        # New-name conviction floor (Jul 17-22: every ~-10% realized loss — MU,
        # SPCX twice — was a FRESH position opened at 0.45-0.50 conviction on
        # lagged congress/crowd theses). A new name claims a slot, pays the
        # spread, and starts a churn clock; sub-coin-flip conviction doesn't
        # earn that. Top-ups are exempt — the held position already cleared
        # this bar at entry and has its own evidence gate below. Inert at 0.
        if (
            self.limits.min_new_name_conviction > 0
            and account.position_for(proposal.symbol) is None
            and proposal.conviction < self.limits.min_new_name_conviction
        ):
            return self._reject(
                proposal,
                f"Fresh-name conviction {proposal.conviction:.2f} below the "
                f"new-position floor {self.limits.min_new_name_conviction:.2f} "
                "— starter positions need better than coin-flip conviction.",
            )

        # Corroboration gate, reject leg (Aug 12-21 forensic review): a FRESH
        # name whose cited signal set is exactly ONE soft family (insider /
        # congress / options_flow) with zero fundamentals/news/technical
        # corroboration must clear the composite bar to enter at all — the
        # single-soft cohort (QNT, LFTO, INTC, F, AVBC) failed fast, and the
        # autotune sweeps proved conviction floors can't express this (0
        # trades affected at every candidate). The size haircut lives in the
        # sizing section below; fails open on a missing composite (best-
        # effort feed) and on missing citations. Top-ups exempt.
        soft_solo = self._solo_soft_family(proposal, account, entry_families)
        if soft_solo:
            bar = getattr(self.limits, "corroboration_min_composite", 0.0)
            if composite_score is not None and composite_score < bar:
                return self._reject(
                    proposal,
                    f"CORROBORATION GATE: single-soft-signal ({soft_solo}) "
                    f"with no fundamentals/news/technical corroboration and "
                    f"composite {composite_score:+.2f} < {bar:+.2f} bar — one "
                    "soft family alone doesn't earn a fresh slot.",
                )

        # Expectancy gate on signal families (Jul 30 review): a FRESH entry
        # whose cited thesis families are ALL losing money over the trailing
        # window is the same trade the ledger just paid to learn (NU/NOK/BEP
        # were consecutive momentum-flow starters). Blocks only when every
        # cited family is in the negative set — one healthy/unjudged family
        # keeps the entry alive. Top-ups exempt; no citations = fail open.
        if (
            self.limits.expectancy_gate_enabled
            and neg_families and entry_families
            and account.position_for(proposal.symbol) is None
            and set(entry_families) <= set(neg_families)
        ):
            worst_fam = min(
                entry_families, key=lambda f: neg_families[f].avg_pl_pct
            )
            ws = neg_families[worst_fam]
            return self._reject(
                proposal,
                f"Expectancy gate: every cited thesis family is negative over "
                f"the trailing window (worst: {worst_fam} "
                f"{ws.avg_pl_pct:+.1f}%/trip across {ws.trips} closed trips) — "
                "no fresh entries from families that are currently losing "
                "money.",
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
        # ran straight to their stops). The trigger logic lives in
        # _overextension_read, SHARED with the bullish-option chase gate in
        # evaluate_option (Aug 12-21: HL re-expressed a rejected equity chase
        # as a bull_call_spread). Block it, or halve the size (haircut mode)
        # so a wrong top costs half. Fails open on missing technicals — a
        # yfinance outage must not freeze all buying.
        overext_note = ""
        overext_mult = 1.0
        overext_trigger, overext_why = self._overextension_read(price, tech)
        if overext_trigger:
            # The EXTREME leg has its own mode: a >=Nx-ATR screaming
            # extension is a different risk than a mild hot-and-extended
            # entry, and defaults to a hard block (the shared "haircut" mode
            # only halved it — CVX still bought $2,799 at 3.2xATR Jul 17).
            mode = (
                self.limits.overext_extreme_mode if overext_trigger == "extreme"
                else self.limits.overextension_mode
            )
            if mode == "block":
                return self._reject(
                    proposal,
                    f"Overextended: {overext_why} — chasing a local top; wait "
                    "for a pullback or base.",
                )
            overext_mult = max(0.0, min(1.0, self.limits.overext_haircut))
            overext_note = f" Overextension haircut x{overext_mult:g} ({overext_why})."

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

        # Price-aware re-entry guard (2026-07 audit): the time cooldown above
        # can't see PRICE — after it lapses, re-buying a recently exited name AT
        # OR ABOVE the price we sold it for is chasing (CVX/PATH/HUBB were re-
        # bought higher). Blocked while the exit clock is still warm, unless the
        # composite clears the override (a genuine new edge, not just momentum).
        # Fails open on a missing exit price or composite.
        if (
            self.limits.reentry_price_guard_enabled
            and account.position_for(proposal.symbol) is None
        ):
            exit_price = self.state.last_exit_price(proposal.symbol)
            if exit_price is not None and exit_price > 0 and price >= exit_price:
                override = (
                    composite_score is not None
                    and composite_score >= self.limits.reentry_price_override_composite
                )
                if not override:
                    return self._reject(
                        proposal,
                        f"Re-buying {proposal.symbol} at ${price:.2f} >= recent "
                        f"exit ${exit_price:.2f} — chasing above the exit; "
                        f"composite {'n/a' if composite_score is None else f'{composite_score:+.2f}'}"
                        f" doesn't clear the +{self.limits.reentry_price_override_composite:g} "
                        "override (churn guard).",
                    )

        # Loss-streak re-entry bar (Jul 29 diagnosis: the book recycles a small
        # universe — a name whose last N trips ALL lost keeps getting a fresh
        # slot at the same floor conviction). After `loss_streak_guard`
        # consecutive losing closed trips, a FRESH entry needs the composite
        # override bar — the same "top-decile new edge" standard the price
        # guard uses — or it waits. A winning trip clears the streak.
        streak_bar = int(getattr(self.limits, "loss_streak_guard", 0))
        if (
            streak_bar > 0
            and account.position_for(proposal.symbol) is None
        ):
            streak = self.state.loss_streak(proposal.symbol)
            if streak >= streak_bar:
                override = (
                    composite_score is not None
                    and composite_score >= self.limits.reentry_price_override_composite
                )
                if not override:
                    return self._reject(
                        proposal,
                        f"{proposal.symbol} lost its last {streak} closed "
                        f"trip(s) — fresh entry needs composite >= "
                        f"+{self.limits.reentry_price_override_composite:g} "
                        f"({'n/a' if composite_score is None else f'{composite_score:+.2f}'}) "
                        "to try again (loss-streak guard).",
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

        stop_pct, take_pct = self._exit_levels(proposal, volatility, tech)

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

        # 1a-ii) Starter haircut (Jul 27-29: BEP -$3,189 / NU -$3,291 — every
        #     fresh name this cycle entered AT the conviction floor with the
        #     stop clamped near the 4% vol floor, and two of five died inside
        #     a day). A thin-edge STARTER deploys at half size; conviction can
        #     still build the position via top-ups once the thesis is working.
        #     Top-ups are exempt — the position already earned its slot.
        starter_note = ""
        if (
            getattr(self.limits, "starter_haircut_enabled", False)
            and account.position_for(proposal.symbol) is None
        ):
            low_conv = (
                self.limits.starter_full_conviction > 0
                and proposal.conviction < self.limits.starter_full_conviction
            )
            floor_stop = (
                self.limits.vol_stops_enabled
                and volatility is not None and volatility > 0
                and self.limits.vol_stop_min_pct > 0
                and stop_pct <= self.limits.vol_stop_min_pct * 1.15
            )
            if low_conv or floor_stop:
                mult = max(0.0, min(1.0, self.limits.starter_haircut_mult))
                target_notional *= mult
                bits = []
                if low_conv:
                    bits.append(
                        f"conviction {proposal.conviction:.2f} < "
                        f"{self.limits.starter_full_conviction:g}"
                    )
                if floor_stop:
                    bits.append(f"stop at the {stop_pct:.1f}% vol floor")
                starter_note = f" Starter haircut x{mult:g} ({'; '.join(bits)})."

        # 1a-iii) Corroboration haircut (Aug 12-21 forensic review): the
        #     single-soft-signal starter flagged above deploys at the starter-
        #     haircut fraction even when its composite clears the bar. Never
        #     stacks with the starter haircut — the two express the same
        #     "thin evidence = half size" idea, so the max reduction wins,
        #     not the product. Logged with the would-have-been size so the
        #     counterfactual cohort stays measurable in the daily logs.
        corro_note = ""
        if soft_solo:
            mult = max(0.0, min(1.0, self.limits.starter_haircut_mult))
            comp_txt = (
                "composite n/a (fails open past the bar)"
                if composite_score is None else
                f"composite {composite_score:+.2f} >= "
                f"{self.limits.corroboration_min_composite:+.2f}"
            )
            if starter_note:
                log.info(
                    "CORROBORATION GATE: %s single-soft-signal (%s) — starter "
                    "haircut already took x%g; max reduction wins, not "
                    "stacking (size stays $%s; %s).",
                    proposal.symbol, soft_solo, mult,
                    f"{target_notional:,.0f}", comp_txt,
                )
                corro_note = (
                    f" Corroboration gate: single soft family ({soft_solo}); "
                    "starter haircut already applied — not halving twice."
                )
            else:
                would_be = target_notional
                target_notional *= mult
                log.info(
                    "CORROBORATION GATE: %s single-soft-signal (%s) — size "
                    "halved to $%s from $%s (%s).",
                    proposal.symbol, soft_solo,
                    f"{target_notional:,.0f}", f"{would_be:,.0f}", comp_txt,
                )
                corro_note = (
                    f" Corroboration haircut x{mult:g} "
                    f"(single soft family: {soft_solo})."
                )

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

        # 2c) Exposure ladder (Jul 30 review) — in a neutral/risk-off regime,
        #     RISK exposure is capped at the regime's rung. The multiplier only
        #     ever shrank individual buys, so the book's floor posture stayed
        #     fully-invested-long through every decline; the ladder stops NEW
        #     money from keeping the book maxed (existing positions aren't
        #     force-sold — the regime trim / core defense handle that). The
        #     defensive T-bill sleeve is a cash proxy and doesn't count as
        #     risk (`defensive_exempt_usd`), or parking cash defensively
        #     would block every remaining satellite.
        gross_held = sum(p.market_value for p in account.positions)
        if self.limits.exposure_ladder_enabled and regime_label in (
            "neutral", "risk-off",
        ):
            rung = (
                self.limits.exposure_neutral_pct if regime_label == "neutral"
                else self.limits.exposure_risk_off_pct
            )
            if 0 < rung < self.limits.max_gross_exposure_pct:
                risk_held = gross_held - max(0.0, defensive_exempt_usd)
                rung_val = equity * (rung / 100.0)
                ladder_room = (
                    rung_val - risk_held - max(0.0, pending_buy_notional)
                )
                if ladder_room <= 0:
                    return self._reject(
                        proposal,
                        f"At/over the {rung:.0f}% exposure-ladder cap "
                        f"({regime_label} regime; risk deployed "
                        f"${risk_held:,.0f} of ${rung_val:,.0f}).",
                    )
                target_notional = min(target_notional, ladder_room)

        # 2c') No-leverage gross cap — never let TOTAL deployed exceed this % of
        #     equity. On a margin account (Alpaca offers ~2x buying power) this is
        #     the explicit guard that we never trade with borrowed money.
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

        # 2c'') BOOK BETA CAP (run-6 item 7b): the gross cap bounds dollars,
        #     not exposure. With a book reading this cycle, the post-trade
        #     SPY-beta (book + w_new * beta_new) must stay <= max_book_beta_spy:
        #     the buy is RESIZED to the room, rejected when even the min order
        #     breaches. Unknown candidate beta = 1.0 (logged); no book reading
        #     = fail open (an outage must not freeze buying). Negative-beta
        #     buys (an inverse ETF) can't breach and pass untouched.
        cap = float(getattr(self.limits, "max_book_beta_spy", 0.0) or 0.0)
        if cap > 0 and book_beta_spy is not None and equity > 0:
            if candidate_beta is None:
                cand_beta = 1.0
                log.info(
                    "BOOK BETA CAP: %s beta unknown — assuming 1.0.",
                    proposal.symbol,
                )
            else:
                cand_beta = float(candidate_beta)
            post = book_beta_spy + (target_notional / equity) * cand_beta
            if cand_beta > 0 and post > cap + 1e-9:
                room = max(0.0, (cap - book_beta_spy) * equity / cand_beta)
                min_order = max(
                    self.limits.min_order_usd,
                    equity * (getattr(self.limits, "min_order_pct", 0.0) / 100.0),
                    price if self.limits.whole_shares_only else 0.0,
                )
                req_pct = target_notional / equity * 100.0
                if room < min_order:
                    log.warning(
                        "BOOK BETA CAP: %s %.1f%% -> rejected (book %.2f -> "
                        "%.2f > cap %.2f; beta %.2f; room $%.0f < min order "
                        "$%.0f).", proposal.symbol, req_pct, book_beta_spy,
                        post, cap, cand_beta, room, min_order,
                    )
                    return self._reject(
                        proposal,
                        f"Book beta cap: post-trade SPY-beta {post:.2f} > "
                        f"{cap:.2f} cap (book {book_beta_spy:.2f}, "
                        f"{proposal.symbol} beta {cand_beta:.2f}) and even the "
                        f"min order (${min_order:,.0f}) breaches it.",
                    )
                new_post = book_beta_spy + (room / equity) * cand_beta
                log.warning(
                    "BOOK BETA CAP: %s %.1f%% -> %.1f%% (book %.2f -> %.2f)",
                    proposal.symbol, req_pct, room / equity * 100.0,
                    post, new_post,
                )
                target_notional = min(target_notional, room)

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
                + starter_note
                + corro_note
            ),
        )

    # -- shared anti-chase / overextension read ------------------------------ #
    def _overextension_read(
        self, price: float, tech: dict | None,
    ) -> tuple[str, str]:
        """The anti-chasing overextension read shared by equity buys and
        BULLISH option debits (Aug 12-21 forensic: HL's equity buy was
        rejected as overextended at RSI 70 / 4.2xATR, and the SAME thesis
        re-expressed as a $5,200 bull_call_spread walked straight past the
        gate to -67.6% in 21h — the two paths must read the same tape).

        Returns (trigger, why):
          ""        — clean, or no data (fails open: a yfinance outage must
                      not freeze all buying), or the gate is disabled;
          "hot"     — RSI >= overext_rsi AND price >= overext_atr_mult ATRs
                      over the 20d SMA (% fallback when ATR is unavailable);
          "extreme" — >= overext_extreme_atr_mult ATRs over the 20d SMA
                      regardless of RSI (the Jul-13 losers entered at RSI
                      61-64 but 3.4-4.0 ATRs extended), or a gap-day chase
                      >= overext_gap_pct above the PRIOR close (VRRM Jul 29).

        `price` feeds only the gap leg; pass 0 when unknown (that leg then
        fails open like the rest)."""
        if not self.limits.overextension_gate_enabled or not tech:
            return "", ""
        rsi = tech.get("rsi14")
        # Prefer the PRIOR-day ATR denominator when the feed provides it: a
        # gap day's own huge bar inflates today's ATR and deflates the
        # extension read (VRRM Jul 29: 4.1x true extension read as 2.93x —
        # under the 3.0x extreme block — because the +28% gap bar had
        # already fattened its own yardstick).
        ext_atr = tech.get("ext_atr_prior")
        if ext_atr is None:
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
        # Gap-day trigger: RSI/ATR-vs-SMA never see a ONE-DAY move, so a
        # +28% gap open (VRRM) walked through both legs. An entry this far
        # above the PRIOR close is a chase by definition — fires the
        # extreme leg's mode (hard block by default).
        gap_pct = None
        prev_close = tech.get("prev_close")
        if (
            self.limits.overext_gap_pct > 0
            and prev_close and prev_close > 0 and price > 0
        ):
            gap_pct = (price / prev_close - 1.0) * 100.0
            if gap_pct >= self.limits.overext_gap_pct:
                extreme = True
        gapped = (
            gap_pct is not None and self.limits.overext_gap_pct > 0
            and gap_pct >= self.limits.overext_gap_pct
        )
        if not (hot_and_extended or extreme):
            return "", ""
        if ext_atr is not None:
            how_far = f"{ext_atr:.1f}xATR"
        elif ext_pct is not None:
            how_far = f"{ext_pct:.1f}%"
        else:
            how_far = "n/a"
        if gapped:
            why = (
                f"+{gap_pct:.0f}% above the prior close (gap-day chase; "
                f"{how_far} over the 20d SMA)"
            )
        elif extreme and not hot_and_extended:
            why = f"{how_far} above the 20d SMA (extreme extension)"
        else:
            why = f"RSI {rsi:.0f} and {how_far} above the 20d SMA"
        return ("extreme" if extreme else "hot"), why

    # -- corroboration gate: solo-soft-family read --------------------------- #
    def _solo_soft_family(
        self, proposal: TradeProposal, account: AccountSnapshot,
        entry_families: set[str] | None,
    ) -> str:
        """The soft family name when a FRESH entry's cited signal set boils
        down to exactly ONE soft family (insider / congress / options_flow)
        with zero fundamentals/news/technical corroboration; "" otherwise.
        "discovery"/"composite" citations are ignored when judging soloness —
        the scanner surfacing the name is the same soft read wearing another
        label (Jul-27 congress->DISCOVERY laundering), and the composite is
        the index citing itself. Fails open on missing citations; top-ups are
        exempt (the position already earned its slot at entry)."""
        if not getattr(self.limits, "corroboration_gate_enabled", False):
            return ""
        if not entry_families or account.position_for(proposal.symbol) is not None:
            return ""
        effective = set(entry_families) - _NON_CORROBORATING
        if len(effective) == 1:
            fam = next(iter(effective))
            if fam in _SOFT_FAMILIES:
                return fam
        return ""

    # -- options: defined-risk premium gate -------------------------------- #
    def evaluate_option(
        self, proposal: TradeProposal, account: AccountSnapshot,
        est_premium_per_contract: float,
        leg_liquidity: list[dict] | None = None,
        min_leg_premium: float | None = None,
        market_trend: str = "",
        regime_label: str = "",
        regime_multiplier: float = 1.0,
        name_trend: str = "",
        sanctioned_hedge: bool = False,
        name_ext_pct: float | None = None,
        tech: dict | None = None,
        days_to_earnings: int | None = None,
        index_symbols: frozenset[str] | set[str] | None = None,
        proxy_put: bool = False,
    ) -> RiskDecision:
        """Size a defined-risk options play by capped DEBIT. Max loss on a long
        option / debit spread is the premium paid, so we bound that premium to a
        small % of equity. Rejects everything if options are disabled.

        `leg_liquidity` is per-leg {'symbol', 'oi', 'rel_spread_pct'} context
        from OptionsHelper.leg_liquidity — optional, and None FIELDS fail open
        (the est_premium<=0 gate already refuses quote-less legs).

        `market_trend` is the regime's LONG-RUN direction read ("up"/"down"/
        "" = unknown) and `regime_label` its blended label — together they
        drive the direction gate (calls with the tape, puts against it).
        `name_trend` is the UNDERLYING's own long-run read (price vs its
        200dma from the technical signal) — a single-name breakdown keeps its
        put candidacy even in a bull tape. `regime_multiplier` scales the
        premium budget the same way it already scales equity sizing, so
        option risk also shrinks when the market turns; all four default to
        inert values for legacy callers. `sanctioned_hedge` marks the
        deterministic falling-market INDEX-put sanction (Jul 29): the same
        read that authored the prompt block must also satisfy the direction
        gate, or the sanctioned put dies at the gate exactly when the core is
        not held (fresh reset / CORE_ETF unset). `name_ext_pct` is the
        underlying's % distance from its 20d SMA (ext_pct_sma20; negative =
        below) — a sharp short-term breakdown keeps its put candidacy even
        while the name still sits above its 200dma (Jul 30 review: NU/NOK
        broke hard yet read name_trend="up", so every put died at the
        gate). `tech` is the UNDERLYING's technicals dict (rsi14 / ext_atr /
        prev_close / price — the same dict _evaluate_buy's anti-chase gate
        reads) for the bullish-option chase gate; None fails open.

        Run-6 item 3 (close the options bypass): `days_to_earnings` is the
        same calendar read the equity path gets — a single-name debit inside
        earnings_blackout_days is rejected (None fails open, like equities).
        `index_symbols` extends the broad-index exemption set (core / hedge /
        proxy ETFs from config). `proxy_put` marks the put-liquidity proxy
        re-proposal: it keeps the direction-gate sanction but NOT the
        per-underlying premium-cap exemption when PROXY_PUT_THESIS_GATE is
        on."""
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
        # ---- run-6 item 3: single-name gates (index underlyings exempt) ----
        under = proposal.symbol.upper()
        is_index = (
            under in _INDEX_UNDERLYINGS
            or under in {str(x).upper() for x in (index_symbols or ())}
        )
        rights = {
            "c" if leg.right.lower().startswith("c") else "p"
            for leg in proposal.option_legs
        }
        bullish = rights == {"c"}
        if (
            bullish and not is_index
            and not getattr(self.limits, "options_single_name_bullish", True)
        ):
            debit = max(0.0, est_premium_per_contract) * 100.0
            budget = account.equity * (self.limits.max_option_premium_pct / 100.0)
            log.warning(
                "OPTIONS SINGLE-NAME BULLISH: %s %s rejected "
                "(OPTIONS_SINGLE_NAME_BULLISH=off) — counterfactual debit "
                "$%s/contract, budget $%s.",
                under, proposal.option_strategy, f"{debit:,.0f}",
                f"{budget:,.0f}",
            )
            return self._reject(
                proposal,
                "single-name bullish option debits disabled for this window "
                f"(OPTIONS_SINGLE_NAME_BULLISH=off; counterfactual debit "
                f"${debit:,.0f}/contract).",
            )
        blackout = self.limits.earnings_blackout_days
        # Review fix (Aug 26): puts pass the blackout only when the operator
        # turned OPTIONS_BLACKOUT_PUTS off; the default blocks every
        # single-name debit into the print (calls AND puts).
        blackout_applies = bullish or bool(
            getattr(self.limits, "earnings_blackout_puts", True))
        if (
            blackout > 0 and not is_index and blackout_applies
            and days_to_earnings is not None
            and 0 <= days_to_earnings <= blackout
        ):
            return self._reject(
                proposal,
                f"Earnings in {days_to_earnings}d (<= {blackout}d blackout) — "
                "a single-name option debit gaps through the print exactly "
                "like shares (run-6: the equity blackout applies to every "
                "instrument).",
            )
        ok, why = self._direction_fits_market(
            proposal, account, market_trend, regime_label, name_trend,
            sanctioned_hedge=sanctioned_hedge, name_ext_pct=name_ext_pct,
        )
        if not ok:
            return self._reject(proposal, why)
        ok, why = self._under_option_position_cap(proposal, account)
        if not ok:
            return self._reject(proposal, why)
        ok, why = self._legs_merge_safe(proposal, account)
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
        # Per-leg premium floor: the cheapest leg must be a real, priced
        # contract. A sub-floor mid ($0.01 on the T blowup) is a deep-OTM /
        # illiquid lottery ticket whose penny price mints a huge, un-exitable
        # contract count. Applies to the LEG, not the net, so a legitimately
        # tight debit spread still passes. None = caller had no per-leg data
        # (fails open — est_premium<=0 already refused any quote-less leg).
        floor = getattr(self.limits, "min_option_premium", 0.0)
        if floor > 0 and min_leg_premium is not None and 0 < min_leg_premium < floor:
            return self._reject(
                proposal,
                f"Cheapest leg ${min_leg_premium:.2f}/share < ${floor:.2f} floor "
                f"— sub-floor premium is a deep-OTM/illiquid lottery ticket.",
            )

        # Anti-chase parity for BULLISH structures (Aug 12-21 forensic: HL's
        # equity buy was rejected as overextended at RSI 70 / 4.2xATR, and the
        # SAME thesis re-expressed as a $5,200 bull_call_spread bypassed the
        # gate entirely and lost -67.6% in 21h). The UNDERLYING runs through
        # the exact read _evaluate_buy uses: hot-and-extended haircuts the
        # premium budget; an extreme extension / gap-day chase follows the
        # extreme mode (hard block by default). Bearish structures are exempt
        # — a put on a falling name is the OPPOSITE of an upside chase and
        # must never be muzzled by upside overextension. Fails open on
        # missing technicals, like the equity gate.
        chase_mult = 1.0
        chase_note = ""
        if bullish:
            under_price = float((tech or {}).get("price") or 0.0)
            trigger, why = self._overextension_read(under_price, tech)
            if trigger:
                mode = (
                    self.limits.overext_extreme_mode if trigger == "extreme"
                    else self.limits.overextension_mode
                )
                if mode == "block":
                    return self._reject(
                        proposal,
                        f"OPTION CHASE GATE: underlying overextended — {why} — "
                        "a bullish option debit is the same chase the equity "
                        "gate blocks; wait for a pullback or base.",
                    )
                chase_mult = max(0.0, min(1.0, self.limits.overext_haircut))
                chase_note = f" Option chase haircut x{chase_mult:g} ({why})."
                log.info(
                    "OPTION CHASE GATE: %s bullish structure on an "
                    "overextended underlying (%s) — premium budget x%g.",
                    proposal.symbol, why, chase_mult,
                )

        equity = account.equity
        cap = equity * (self.limits.max_option_premium_pct / 100.0)
        # Regime-scaled premium budget: equity sizing already shrinks with the
        # regime multiplier (see evaluate); before this, option debits sized
        # identically in risk-on and risk-off. Scale the equity-derived cap
        # only — the model's own max_premium_usd stays an absolute ceiling.
        if self.limits.regime_filter_enabled:
            cap *= max(0.0, min(1.0, regime_multiplier))
        # Anti-chase haircut (computed above): an overextended underlying's
        # bullish debit deploys at the haircut fraction, like an equity buy.
        cap *= chase_mult
        if proposal.max_premium_usd is not None:
            cap = min(cap, proposal.max_premium_usd)

        # premium quoted per share; one contract = 100 shares
        per_contract_cost = est_premium_per_contract * 100.0

        # Per-underlying premium concentration cap (Aug 12-21 forensic: AMZN
        # stacked ~$29.8k of open premium across structures on ONE underlying
        # and lost -$14,956 — max_option_premium_pct bounds each PLAY, so
        # nothing bounded the pile-up). Sum the OPEN option lots' net premium
        # for this underlying (same basis math as the watchdog's premium
        # exits: long legs debit, short legs negative qty credit) and keep
        # existing + new debit under the cap: the budget is clamped into the
        # remaining headroom, and rejected when one contract no longer fits.
        # Sanctioned hedges are EXEMPT: the falling-market index put and the
        # put-liquidity proxy route ALL crash protection through one or two
        # fixed underlyings (core ETF / put_proxy_etf) by design, so a
        # concentration cap on that venue would halve — then hard-block —
        # further downside protection exactly in the falling tape it exists
        # for. Each sanctioned play is still bounded by max_option_premium_pct
        # and the direction gate's own sanction plumbing.
        # Run-6 item 3e: the put-liquidity PROXY is no longer exempt — an
        # index short re-expressing a single-name read sits under the same
        # 0.5% cap as every other debit; only the falling-market index put
        # (sanctioned_hedge without proxy_put) keeps the exemption.
        per_under_pct = getattr(self.limits, "per_underlying_premium_pct", 0.0)
        cap_exempt = sanctioned_hedge and not proxy_put
        if per_under_pct > 0 and cap_exempt:
            log.info(
                "PER-UNDERLYING PREMIUM CAP: %s exempt (sanctioned hedge — "
                "venue concentration must not cap crash protection).",
                proposal.symbol.upper(),
            )
        if per_under_pct > 0 and not cap_exempt:
            from .execution.options import parse_occ
            open_premium = max(0.0, sum(
                p.avg_entry_price * p.qty * 100.0
                for p in account.positions if p.is_option
                for occ in [parse_occ(p.symbol)] if occ and occ[0] == under
            ))
            under_cap = equity * (per_under_pct / 100.0)
            under_room = under_cap - open_premium
            if under_room < per_contract_cost:
                return self._reject(
                    proposal,
                    f"PER-UNDERLYING PREMIUM CAP: ${open_premium:,.0f} open "
                    f"option premium on {under} + "
                    f"${per_contract_cost:,.0f}/contract new debit would "
                    f"exceed ${under_cap:,.0f} ({per_under_pct:g}% of equity) "
                    "— one underlying must not concentrate the option book "
                    "(AMZN Aug-12).",
                )
            if cap > under_room:
                log.info(
                    "PER-UNDERLYING PREMIUM CAP: %s debit budget clamped "
                    "$%s -> $%s (open premium $%s of $%s cap).",
                    under, f"{cap:,.0f}", f"{under_room:,.0f}",
                    f"{open_premium:,.0f}", f"{under_cap:,.0f}",
                )
                cap = under_room

        contracts = int(cap / per_contract_cost)
        if contracts < 1:
            return self._reject(
                proposal,
                f"Premium ${per_contract_cost:,.0f}/contract exceeds "
                f"${cap:,.0f} options budget.",
            )
        # Hard contract-count ceiling: even within the debit cap, a cheap
        # premium can size a monster order that itself moves a thin book (or
        # can't fill). Clamp — deploying LESS than the cap is always safe.
        max_ct = int(getattr(self.limits, "max_option_contracts", 0))
        if max_ct > 0 and contracts > max_ct:
            log.info(
                "Option %s: clamping %d contracts to %d (thin-book count cap).",
                proposal.symbol, contracts, max_ct,
            )
            contracts = max_ct
        spent = contracts * per_contract_cost
        return RiskDecision(
            proposal=proposal,
            verdict=RiskVerdict.APPROVED,
            approved_qty=float(contracts),
            approved_notional=spent,
            reason=(
                f"{contracts} contract(s), ${spent:,.0f} debit "
                f"(cap ${cap:,.0f})." + chase_note
            ),
        )

    # -- option direction vs long-run market trend --------------------------- #
    def _direction_fits_market(
        self, proposal: TradeProposal, account: AccountSnapshot,
        market_trend: str, regime_label: str, name_trend: str = "",
        sanctioned_hedge: bool = False,
        name_ext_pct: float | None = None,
    ) -> tuple[bool, str]:
        """Option debits must trade WITH the long-run market trend (SPY vs its
        200dma): calls in an up market, puts in a down market. A put bought
        into a long-run uptrend bleeds theta against the tape; a call into a
        downtrend fights it — either way the debit pays for a fight the odds
        are against. Direction comes from the LEGS' rights, never the declared
        strategy name (the shape check doesn't verify a "bear_put_spread"
        actually uses puts — legs are what trade).

        Carve-outs for bearish structures in an up market: (a) the regime
        label reads risk-off (a vol spike inside an uptrend — the risk-off put
        mandate ASKS for puts; its own gate must not fight it), (b) the put
        hedges a HELD equity position in the same name (insurance, not
        counter-trend speculation), and (c) the NAME's own long-run trend is
        broken (`name_trend` "down": price below its 200dma) — the insider-
        sell / bearish-slate pipeline exists to short single-name breakdowns,
        which happen in bull tapes too. Unknown trend ("") passes — act only
        on data we have; the degraded regime multiplier already shrinks the
        premium budget."""
        if not getattr(self.limits, "option_direction_gate", True):
            return True, ""
        if market_trend not in ("up", "down"):
            return True, ""
        rights = {
            "c" if leg.right.lower().startswith("c") else "p"
            for leg in proposal.option_legs
        }
        if rights == {"c", "p"}:
            # No approved defined-risk shape mixes rights — and a mixed
            # structure has no single direction to check.
            return False, (
                "Mixed call/put legs match no approved defined-risk shape "
                "(long_call, long_put, bull_call_spread, bear_put_spread)."
            )
        bullish = rights == {"c"}
        if market_trend == "down" and bullish:
            return False, (
                "Long-run market trend is DOWN (SPY below its 200dma) — "
                "bullish call structures fight the tape and are blocked "
                "(OPTION_DIRECTION_GATE). Express upside conviction as an "
                "equity BUY if the name earns it."
            )
        if market_trend == "up" and not bullish:
            if regime_label == "risk-off":
                return True, ""  # vol-spiked uptrend: the put mandate rules
            if name_trend == "down":
                return True, ""  # single-name breakdown keeps its put candidacy
            # Broken-MOMENTUM carve-out (Jul 30 review): the names that
            # actually break intraday (NU, NOK) are recent runners still far
            # ABOVE their 200dma — name_trend reads "up" and the put dies. A
            # name at least put_breakdown_ext_pct BELOW its 20d SMA is in a
            # sharp short-term breakdown; its put keeps candidacy too.
            bd = getattr(self.limits, "put_breakdown_ext_pct", 0.0)
            if bd > 0 and name_ext_pct is not None and name_ext_pct <= -bd:
                return True, ""
            if sanctioned_hedge:
                # The falling-market read sanctioned this index put in the
                # prompt — the gate honors its own system's sanction (the
                # intraday-drop leg can fire while the 200dma trend still
                # reads "up", and the core may not be held yet on a reset day).
                return True, ""
            holds_equity = any(
                not p.is_option and p.symbol == proposal.symbol and p.qty > 0
                for p in account.positions
            )
            if holds_equity:
                return True, ""  # protective put on a held name = insurance
            return False, (
                "Long-run market trend is UP (SPY above its 200dma) and this "
                "name is not in its own breakdown — puts into an uptrend "
                "bleed theta and are blocked (OPTION_DIRECTION_GATE) unless "
                "the name trades below its 200dma, sits sharply below its "
                "20d SMA (PUT_BREAKDOWN_EXT_PCT), the put hedges a held "
                "position, or the regime turns risk-off."
            )
        return True, ""

    def put_precheck(
        self, symbol: str, account: AccountSnapshot,
        market_trend: str, regime_label: str,
        tech: dict | None,
    ) -> tuple[bool, str]:
        """Deterministic PREVIEW of `_direction_fits_market` for a hypothetical
        all-puts structure on `symbol`, computed BEFORE the model is asked to
        decide. Jul 31 funnel autopsy: the prompt warned "puts are auto-rejected
        unless the name is breaking down" but never said WHICH bearish names
        would pass — so the model, unable to verify eligibility, held every
        time (slate_bearish=4 -> put_proposals=0, all week). Annotating each
        bearish candidate with this verdict replaces that guess. Mirrors the
        real gate's carve-outs exactly (same order); keep the two in lockstep."""
        if not getattr(self.limits, "option_direction_gate", True):
            return True, "direction gate off"
        if market_trend != "up":
            return True, "market trend not up"
        if regime_label == "risk-off":
            return True, "risk-off regime"
        price = (tech or {}).get("price")
        sma200 = (tech or {}).get("sma200")
        if price and sma200 and price < sma200:
            return True, "below its 200dma"
        bd = getattr(self.limits, "put_breakdown_ext_pct", 0.0)
        ext = (tech or {}).get("ext_pct_sma20")
        if bd > 0 and ext is not None and ext <= -bd:
            return True, f"{ext:+.1f}% vs 20d SMA breakdown"
        if any(
            not p.is_option and p.symbol == symbol and p.qty > 0
            for p in account.positions
        ):
            return True, "hedges a held position"
        return False, "uptrend name, not in its own breakdown"

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

    # -- entry-side leg-merge guard (Alpaca 4-leg MLEG cap) -------------------- #
    def _legs_merge_safe(
        self, proposal: TradeProposal, account: AccountSnapshot,
    ) -> tuple[bool, str]:
        """Alpaca caps the mleg order class at 4 legs, and the watchdog closes
        a whole underlying+expiry group as ONE order. Adding a structure that
        pushes an existing group past 4 distinct contracts makes the merged
        group unclosable atomically (2026-08-07 AMZN: a long call + two
        spreads merged to 5 legs and every stop-close was rejected at request
        validation while the position sat unprotected). The close-side
        chunking (PR #52) remains the backstop for legacy groups; this guard
        stops NEW ones from forming. A leg on an already-held contract merges
        into that row (top-up), so it doesn't count twice."""
        from .execution.options import occ_symbol, parse_occ
        max_legs = 4  # Alpaca hard cap — keep in sync with AlpacaClient._MLEG_MAX_LEGS
        held: dict[str, set[str]] = {}
        under = proposal.symbol.upper()
        for p in account.positions:
            if p.is_option:
                occ = parse_occ(p.symbol)
                if occ and occ[0] == under:
                    held.setdefault(occ[1], set()).add(p.symbol)
        for expiry in {leg.expiry for leg in proposal.option_legs}:
            existing = held.get(expiry, set())
            merged = existing | {
                occ_symbol(under, leg.expiry, leg.strike, leg.right)
                for leg in proposal.option_legs if leg.expiry == expiry
            }
            if len(merged) > max_legs:
                return False, (
                    f"Would merge to {len(merged)} legs on {under} {expiry} "
                    f"({len(existing)} already held) — Alpaca caps multi-leg "
                    f"orders at {max_legs}, so the merged group could not "
                    f"close atomically (AMZN 2026-08-07)."
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
