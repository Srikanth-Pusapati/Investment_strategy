"""Claude decision engine: signal bundles -> structured trade proposals.

Calls the Anthropic API directly (not MCP) — here Claude is a component we invoke
programmatically with the evidence and get back machine-readable proposals, which
then pass through the deterministic RiskManager before any order is placed.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import anthropic

from ..config import Config
from ..usage import record_usage
from ..models import (
    AccountSnapshot,
    ExternalHolding,
    SignalBundle,
    SignalKind,
    TradeProposal,
)
from .prompts import PROPOSALS_SCHEMA, SYSTEM_PROMPT

log = logging.getLogger("decision")


class DecisionEngine:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        # Hard timeout so a stalled LLM call can't block the trade loop. The
        # decision cycle is the longest blocking call in the system; without a
        # cap the SDK default (~10m) leaves the bot deaf for that whole window.
        self.client = anthropic.Anthropic(
            api_key=cfg.anthropic_api_key, timeout=cfg.decision_timeout_s, max_retries=1,
        )
        self.model = cfg.decision_model
        # symbol -> (verdict, reason) from the REQUIRED bearish_verdicts output
        # field, refreshed by every decide() call. The orchestrator reconciles
        # these against the cycle's put-ELIGIBLE names right after decide(), so
        # an eligible name the model neither proposed nor declined surfaces as
        # IGNORED instead of vanishing (Jul 31: three cycles of eligible names,
        # zero mention anywhere in the decision journal).
        self.last_bear_verdicts: dict[str, tuple[str, str]] = {}

    def decide(
        self, bundles: list[SignalBundle], account: AccountSnapshot,
        benchmark_line: str = "", external: list[ExternalHolding] | None = None,
        lessons: str = "", today: str = "",
        buy_excluded: dict[str, str] | None = None,
        signal_notes: dict[str, dict[str, str]] | None = None,
        held_notes: dict[str, str] | None = None,
        data_health: list[str] | None = None,
        composites: dict[str, float] | None = None,
        regime_label: str = "", regime_reason: str = "",
        regime_trend: str = "",
        curated: str = "",
        hedge_symbol: str = "", hedge_price: float | None = None,
        hedge_reason: str = "",
        put_eligibility: dict[str, tuple[bool, str]] | None = None,
    ) -> list[TradeProposal]:
        """Ask Claude for proposals across all candidate symbols at once.

        One call per cycle keeps the macro/market context shared and lets the
        model rank candidates against each other rather than in isolation.
        `lessons` is our own derived track-record (signal attribution), passed as
        trusted context so the model can weight by what has actually paid off.
        `curated` is the nightly post-mortem's curated lessons file — split out
        from `lessons` because it only changes once a day (lessons/attribution
        can change intraday whenever a position closes), so it belongs in the
        CACHED stable block instead of forcing a cache write every cycle.
        `signal_notes` (symbol -> kind -> note) carries OUR deterministic
        freshness/trend annotations (E.1+R.4) — trusted, computed from persisted
        history, never from third-party text.
        `held_notes` (symbol -> note) carries each holding's entry conviction
        and hold age from our own ledger clocks — the incumbent baseline a
        rotation candidate must beat.
        `data_health` lists OUR notes about degraded data feeds this cycle, so
        a missing signal reads as an outage instead of a neutral fact.
        `composites` (symbol -> score) is OUR deterministic weighted signal
        index (score x freshness-lag x realized track record) — a numeric
        prior the model's conviction should not wildly contradict unstated.
        `hedge_symbol`/`hedge_price`/`hedge_reason` (set only when the
        deterministic falling-market read fired and options are on) sanction
        ONE defined-risk index put so the book can PROFIT from a decline
        instead of only bleeding through it.
        """
        self.last_bear_verdicts = {}  # never carry a prior cycle's verdicts
        if not bundles:
            return []

        stable_text = self._render_stable(curated)
        dynamic_text = self._render_dynamic(
            bundles, account, benchmark_line, external or [], lessons,
            today=today, buy_excluded=buy_excluded or {},
            signal_notes=signal_notes or {},
            held_notes=held_notes or {},
            data_health=data_health or [],
            composites=composites or {},
            regime_label=regime_label, regime_reason=regime_reason,
            regime_trend=regime_trend,
            hedge_symbol=hedge_symbol, hedge_price=hedge_price,
            hedge_reason=hedge_reason,
            put_eligibility=put_eligibility or {},
        )
        try:
            resp = self.client.messages.create(
                model=self.model,
                # Headroom so adaptive thinking + a full slate of proposals can't
                # truncate the JSON mid-object (a truncated body fails to parse and
                # silently drops the whole cycle). We detect truncation below too.
                max_tokens=16000,
                thinking={"type": "adaptive"},
                # Prompt caching (Jul 22 upgrade #2): _render_stable's output —
                # date/DTE window/curated lessons/risk contract — is byte-
                # identical across a day's decision cycles, so ONE ephemeral 1h
                # breakpoint on it caches system+stable together (Anthropic's
                # cache prefix is tools -> system -> messages, so `system` stays
                # a plain string; no separate breakpoint needed there). This only
                # pays off because DECISION_INTERVAL_SECONDS now keeps
                # consecutive calls inside the 1h TTL — caching was removed once
                # before because the old 60-min cadence outlived even a 1h cache
                # (Jul 13 ledger: writes on all 6 calls, 0 reads). Also note:
                # Opus prefix caching silently no-ops below a ~4,096-token
                # prefix — if state/api_usage.jsonl keeps showing cache_read=0,
                # the stable block needs to grow before this buys anything.
                system=SYSTEM_PROMPT,
                output_config={
                    "effort": self.cfg.decision_effort,
                    "format": {"type": "json_schema", "schema": PROPOSALS_SCHEMA},
                },
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": stable_text,
                            "cache_control": {"type": "ephemeral", "ttl": "1h"},
                        },
                        {"type": "text", "text": dynamic_text},
                    ],
                }],
            )
        except anthropic.APITimeoutError as e:
            log.error("Claude decision call timed out (%ss): %s", self.cfg.decision_timeout_s, e)
            return []
        except anthropic.APIError as e:
            log.error("Claude decision call failed: %s", e)
            return []

        record_usage(resp, self.model, "decision")

        if resp.stop_reason == "refusal":
            log.warning("Decision model refused; treating as no-action this cycle.")
            return []
        if resp.stop_reason == "max_tokens":
            # Body is almost certainly truncated -> invalid JSON. Skip rather than
            # act on a half-parsed slate; surface it loudly so the cap can be raised.
            log.error("Decision response hit max_tokens — truncated; skipping cycle.")
            return []

        text = next((b.text for b in resp.content if b.type == "text"), "")
        proposals, self.last_bear_verdicts = self._parse(text)
        return proposals

    def decide_option_fallback(
        self, bundle: SignalBundle, account: AccountSnapshot,
        prior_conviction: float, reject_reason: str,
        price: float | None = None,
        regime_label: str = "", regime_reason: str = "",
        curated: str = "",
    ) -> TradeProposal | None:
        """Same-cycle single-symbol follow-up after an equity BUY died at an
        EQUITY-only risk gate (overextension / earnings blackout) — the gates
        evaluate_option deliberately exempts. The main-call prompt teaches this
        pivot, but next-cycle memory rarely converts: the slate rotates, the
        idea decays, and the model spends conviction on fresh unblocked names
        instead. Asking WHILE the conviction is live is what converts.

        Scoped hard: the model may return ONE capped-debit bullish call
        structure for this symbol, or HOLD. Returns None on HOLD, decline,
        parse failure, or API error (fail-quiet — the fallback is a bonus
        path, never a cycle blocker). Reuses the cached stable block, so the
        marginal cost is the small dynamic text + output."""
        lines = [
            "## OPTION FALLBACK — single-symbol follow-up (trusted)",
            f"Earlier THIS cycle you proposed an equity BUY of {bundle.symbol} "
            f"at conviction {prior_conviction:.2f}. The risk layer rejected it: "
            f'"{reject_reason}"',
            "That is an EQUITY-only gate; defined-risk option debits are "
            "exempt (max loss is the capped premium, no stop to gap through). "
            "Decide ONE of:",
            "- A BUY with instrument=\"option\": exactly one capped-debit "
            "bullish structure (long_call or bull_call_spread), expiry 2-8 "
            "weeks out, strikes at/near the money"
            + (f" (latest price ${price:,.2f})" if price else "")
            + ", both legs on ONE expiry — ONLY if the bullish thesis "
            "genuinely clears a high-conviction bar on the evidence below.",
            "- action=\"hold\": if it does not. Chasing with a debit is still "
            "chasing — premium spent on a topping name is risk, not safety. "
            "A high ATM IV on the options_chain line favors the spread over "
            "the single leg. Do NOT propose equity or puts here.",
        ]
        if regime_label:
            lines.append(f"## Market regime: {regime_label}")
            if regime_reason:
                lines.append(regime_reason)
        lines.append("<market_data>")
        lines.append(f"### {bundle.symbol}")
        for s in bundle.signals:
            score = f" score={s.score:+.2f}" if s.score is not None else ""
            lines.append(f"- [{s.kind.value}]{score} {self._safe(s.summary)[:240]}")
        lines.append("</market_data>")
        lines.append(
            f"Return at most ONE proposal, for {bundle.symbol} only."
        )
        try:
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=8000,
                thinking={"type": "adaptive"},
                system=SYSTEM_PROMPT,
                output_config={
                    "effort": self.cfg.decision_effort,
                    "format": {"type": "json_schema", "schema": PROPOSALS_SCHEMA},
                },
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            # Byte-identical to decide()'s stable block -> this
                            # call READS the cache the main call just wrote.
                            "type": "text",
                            "text": self._render_stable(curated),
                            "cache_control": {"type": "ephemeral", "ttl": "1h"},
                        },
                        {"type": "text", "text": "\n".join(lines)},
                    ],
                }],
            )
        except anthropic.APIError as e:
            log.warning("Option-fallback call for %s failed: %s", bundle.symbol, e)
            return None
        record_usage(resp, self.model, "option_fallback")
        if resp.stop_reason in ("refusal", "max_tokens"):
            log.warning(
                "Option-fallback for %s unusable (stop_reason=%s).",
                bundle.symbol, resp.stop_reason,
            )
            return None
        text = next((b.text for b in resp.content if b.type == "text"), "")
        for p in self._parse(text)[0]:
            if (
                p.symbol == bundle.symbol
                and p.action.value == "buy"
                and getattr(p.instrument, "value", str(p.instrument)) == "option"
                and p.option_strategy is not None
                and p.option_strategy.value in ("long_call", "bull_call_spread")
            ):
                return p
        log.info(
            "Option fallback for %s: model declined (HOLD) — conviction did "
            "not clear the bar as an option either.", bundle.symbol,
        )
        return None

    # -- prompt rendering --------------------------------------------------- #
    def _render_stable(self, curated: str = "") -> str:
        """The cache-eligible prefix: content that's byte-identical across a
        day's decision cycles — the date/DTE window change only at ET
        midnight, curated lessons only after the nightly post-mortem, and the
        risk contract only on a config change or restart. This is placed FIRST
        in the user message under a single `cache_control` breakpoint, so it
        must stay deterministic for a given day or the cache silently misses
        every cycle instead of paying off."""
        lines: list[str] = []
        # The model has no clock; without an anchor it dates things from its
        # training data — the 2026-07-13 session proposed option legs expiring
        # 2025-08-15, a year in the past, all dead on arrival at the DTE gate.
        now_et = datetime.now(ZoneInfo("America/New_York")).date()
        lines.append(f"Today's date: {now_et.isoformat()} (US/Eastern).")
        r = getattr(self.cfg, "risk", None)
        if r is not None and getattr(r, "options_enabled", False):
            # ceil/floor so the stated window is exactly the DTE gate's
            # acceptance set even for fractional configured bounds.
            lo_days = math.ceil(r.min_option_dte)
            hi_days = math.floor(r.max_option_dte)
            lines.append(
                f"Option legs must expire {lo_days}-{hi_days} days out: only "
                f"expiries from {(now_et + timedelta(days=lo_days)).isoformat()} "
                f"to {(now_et + timedelta(days=hi_days)).isoformat()} are "
                "accepted."
            )
        lines.append("")
        if curated:
            lines += [curated, ""]
        if r is not None:
            lines += self._risk_contract(r)
        # Signal discipline (2026-07-27 reset day: BEP bought on "congress 7/0
        # buys" as the thesis lead, stopped out -4% five hours later). STOCK Act
        # data is stale by construction — it may support a thesis, never BE one.
        lines += [
            "- Congressional-trading and lobbying data lag up to ~45 days and "
            "are weak corroboration ONLY. Never lead a buy thesis with either: "
            "a BUY needs a FRESH anchor — a technical setup, fundamentals, or "
            "live flow — that would justify it even with the congress/lobbying "
            "lines deleted. If the rationale's strongest signal is congress or "
            "lobbying, the correct action is HOLD.",
        ]
        return "\n".join(lines)

    def _render_dynamic(
        self, bundles: list[SignalBundle], account: AccountSnapshot,
        benchmark_line: str, external: list[ExternalHolding], lessons: str = "",
        today: str = "", buy_excluded: dict[str, str] | None = None,
        signal_notes: dict[str, dict[str, str]] | None = None,
        held_notes: dict[str, str] | None = None,
        data_health: list[str] | None = None,
        composites: dict[str, float] | None = None,
        regime_label: str = "", regime_reason: str = "",
        regime_trend: str = "",
        hedge_symbol: str = "", hedge_price: float | None = None,
        hedge_reason: str = "",
        put_eligibility: dict[str, tuple[bool, str]] | None = None,
    ) -> str:
        # Our own derived data (track-record, today-so-far, exclusions) sits
        # OUTSIDE <market_data> — it's trusted guidance, not third-party text.
        # Everything here can change cycle-to-cycle (a position closing moves
        # `lessons`; the slate/account/market data always do) — none of it
        # belongs in the cached stable block (see _render_stable).
        lines: list[str] = []
        r = getattr(self.cfg, "risk", None)
        if lessons:
            lines += [lessons, ""]
        if today:
            lines += [today, ""]
        if buy_excluded:
            options_on = bool(getattr(self.cfg.risk, "options_enabled", False))
            lines.append(
                "## Buys excluded this cycle (deterministic caps — "
                "do NOT propose an equity BUY for these)"
            )
            shown = list(buy_excluded.items())[:10]
            for sym, reason in shown:
                lines.append(f"- {sym}: {reason}")
            extra = len(buy_excluded) - len(shown)
            if extra > 0:
                lines.append(f"- …and {extra} more")
            lines.append(
                "Spend conviction on alternatives; excluded symbols may still "
                "be proposed as SELL or HOLD"
                + (
                    ", or as a defined-risk OPTION play — the exclusions above "
                    "are EQUITY sizing caps; options have their own premium "
                    "budget and gate. A corroborated bearish thesis on an "
                    "excluded name is a long_put/bear_put_spread candidate."
                    if options_on else "."
                )
            )
            lines.append("")
        # Full-book rotation guidance (postmortem 2026-07-14: MU 0.63 was
        # rejected at the slot cap while CVX sat held at 0.46 — the model never
        # tried pairing a sell with the buy). Counted the same way the risk
        # gate counts (equity rows only; options have their own concurrency
        # cap), so this appears exactly when the cap would actually reject.
        max_slots = int(getattr(r, "max_open_positions", 0) or 0) if r else 0
        equity_rows = sum(
            1 for p in account.positions if not getattr(p, "is_option", False)
        )
        if max_slots and equity_rows >= max_slots:
            lines += [
                f"## Book FULL ({equity_rows}/{max_slots} equity slots) — "
                "new names enter only by ROTATION",
                "An equity BUY of a not-held name will be auto-rejected at the "
                "position cap UNLESS this same response also SELLs a current "
                "holding: sells execute first, so the freed slot and capital "
                "fund the buy. Rotate when a candidate's conviction clearly "
                "beats your weakest holding's (by ~0.10 or more) — each HELD "
                "line shows the incumbent's entry conviction and hold age. "
                "Prefer displacing stale, low-conviction holds; do NOT flip a "
                "name entered within the last day on no new information. "
                "Otherwise HOLD: churn pays the spread twice, and a sold name "
                "is locked out by the re-entry cooldown. Top-ups of held "
                "names are unaffected by the cap.",
                "",
            ]
        # The standing numeric risk contract (trusted guidance) lives in the
        # STABLE block now (_render_stable) — it's invariant per process, so it
        # belongs under the cache breakpoint rather than repeated here.
        # Market regime + option-direction discipline, rendered EVERY cycle
        # (before this, the model saw the regime only inside the risk-off
        # mandate below — in risk-on/neutral it chose calls vs puts blind to
        # the market backdrop, and the direction gate in risk.py would have
        # been rejecting proposals the prompt fully sanctioned).
        if r is not None and regime_label:
            lines.append(f"## Market regime: {regime_label}")
            if regime_reason:
                lines.append(regime_reason)
            # The trend-up call preference is SUPPRESSED in risk-off: the
            # mandate block below carries that cycle's put instruction, and
            # rendering both would tell the model "don't propose puts" and
            # "PROPOSE a put" in the same prompt (vol-spiked uptrend).
            if (
                getattr(r, "options_enabled", False)
                and regime_trend == "up"
                and regime_label != "risk-off"
            ):
                lines.append(
                    "Long-run market trend is UP — option debits should be "
                    "CALL structures (long_call / bull_call_spread). PUTs are "
                    "auto-rejected by the risk layer unless the NAME itself "
                    "is breaking down (price below its own 200-day OR sharply "
                    "below its 20d SMA — a broken momentum name), the put "
                    "hedges a name this account HOLDS, or the regime reads "
                    "risk-off; don't spend conviction on puts outside those "
                    "cases."
                )
            elif getattr(r, "options_enabled", False) and regime_trend == "down":
                lines.append(
                    "Long-run market trend is DOWN — option debits should be "
                    "PUT structures (long_put / bear_put_spread) on names with "
                    "broken theses. Bullish CALL structures are auto-rejected "
                    "by the risk layer while the trend is down; don't spend "
                    "conviction proposing them."
                )
            lines.append("")
        # BEARISH CANDIDATES (Jul 30 review): the model saw bearish reads for
        # weeks (TSCO composite -1.10, COO put-skew +15.9) and every one ended
        # in "HOLD — no action" — zero puts across 388 trades — because
        # nothing ever TAUGHT the downside expression the way PR #45 taught
        # the equity-gate call fallback.
        # Jul 31 rework: naming the names wasn't enough — slate_bearish>0 with
        # put_proposals=0 persisted all week. Two causes fixed here: (1) the
        # list came from the PRE-partition composites map, so it could name
        # symbols whose candidate data was dropped from the prompt; (2) the
        # regime line threatens "puts are auto-rejected unless the name is
        # breaking down", and the model — unable to verify which bearish name
        # would pass the direction gate — rationally held every time. The
        # orchestrator now prechecks the REAL gate per on-slate bearish name
        # (risk.put_precheck) and we render the verdict, so proposing a put on
        # an ELIGIBLE name carries no auto-reject risk the model must guess at.
        if (
            r is not None and getattr(r, "options_enabled", False)
            and put_eligibility
        ):
            ranked = sorted(
                put_eligibility.items(),
                key=lambda kv: composites.get(kv[0], 0.0) if composites else 0.0,
            )
            eligible = [(s, why) for s, (ok, why) in ranked if ok][:4]
            blocked = [(s, why) for s, (ok, why) in ranked if not ok][:4]
            lines.append(
                "## BEARISH CANDIDATES — a corroborated breakdown is a "
                "trade, not a HOLD"
            )
            if eligible:
                listed = ", ".join(
                    f"{s} ({composites.get(s, 0.0):+.2f} composite; "
                    f"gate passes: {why})"
                    for s, why in eligible
                )
                lines += [
                    f"Put-ELIGIBLE (direction gate pre-checked this cycle — "
                    f"a put on these will NOT be auto-rejected): {listed}.",
                    "If the bearish read is corroborated by the name's own "
                    "data (downtrend or broken 20d SMA, bearish options "
                    "flow/chain lean, insider or congress selling), EXPRESS "
                    "it: propose a defined-risk long_put or bear_put_spread — "
                    "instrument 'option', action 'buy', expiry 2-8 weeks out, "
                    "strikes at/near the money, within the premium budget. "
                    "You cannot short stock; an unexpressed bearish read "
                    "earns nothing.",
                    # Aug 1 escalation: the Jul 31 prompt fix showed the model
                    # verified-eligible names and it still skipped every one —
                    # no put, no HOLD, no mention in 76 journal records. The
                    # schema's required bearish_verdicts field ends silence as
                    # an option; this line binds it to THIS list.
                    "MANDATORY: your bearish_verdicts output must contain one "
                    "entry for EVERY symbol in the Put-ELIGIBLE list above — "
                    "verdict 'put_proposed' alongside the option proposal, or "
                    "'declined' naming the specific evidence that is missing. "
                    "An omitted symbol is logged as IGNORED and audited "
                    "nightly.",
                ]
            if blocked:
                lines.append(
                    "Gate-BLOCKED today (do NOT propose puts on these — the "
                    "direction gate would reject them): "
                    + ", ".join(
                        f"{s} ({composites.get(s, 0.0):+.2f}; {why})"
                        for s, why in blocked
                    )
                    + ". A bearish read on a gate-blocked HELD name is a "
                    "SELL/trim decision instead."
                )
            lines.append("")
        # RISK-OFF downside mandate: when the market is genuinely turning down
        # (SPY below its 200dma AND elevated VIX -> regime label "risk-off"), a
        # long-only book just loses more slowly. Tell the model to EXPRESS the
        # downside with a defined-risk put — the only way to PROFIT as prices fall
        # (no shorting). Fires ONLY in risk-off, so it's inert in a calm uptrend
        # (buying puts into an uptrend just bleeds theta); this is why the bot has
        # correctly held no puts through a risk-on trial, not a bug.
        if (
            r is not None
            and getattr(r, "options_enabled", False)
            and regime_label == "risk-off"
        ):
            lines += [
                "## MARKET IS RISK-OFF — express the downside, don't just hold and bleed",
                # The regime line above already carries the reason string.
                "Your long book loses as prices fall and sizing is already cut.",
                "PROPOSE a DEFINED-RISK downside play to PROFIT from the decline: a "
                "long_put or bear_put_spread on the slate name with the most clearly "
                "BROKEN thesis (price below its moving averages, bearish MACD, "
                "deteriorating options-chain lean). You cannot short stock — a put is "
                "the ONLY way to make money as the market falls. Keep it defined-risk "
                "and within the options premium budget. If NO slate name has a "
                "genuinely bearish, corroborated setup, don't force one.",
                "",
            ]
        # Falling-market INDEX hedge (Jul 29): the risk-off mandate above needs
        # the slow 200dma/VIX regime to fully flip, and it only targets single
        # names. This block fires on the FAST falling read (intraday benchmark
        # drop / long-run downtrend) and sanctions ONE defined-risk put on the
        # INDEX itself — the direct way to profit from a market-wide fall and
        # insure the core (which this account holds, so the direction gate
        # reads it as a hedge, not counter-trend speculation).
        if (
            hedge_symbol
            and r is not None
            and getattr(r, "options_enabled", False)
        ):
            lines.append(
                f"## MARKET FALLING — a defined-risk INDEX PUT on "
                f"{hedge_symbol} is sanctioned"
            )
            if hedge_reason:
                lines.append(f"Deterministic read: {hedge_reason}.")
            lines += [
                f"You MAY propose ONE long_put or bear_put_spread on "
                f"{hedge_symbol}"
                + (f" (latest price ${hedge_price:,.2f})" if hedge_price else "")
                + ", expiry 2-8 weeks out, strikes at/near the money, within "
                "the options premium budget. It PROFITS from a continued "
                "decline and insures the index core this account holds. "
                "Propose it when the decline shows continuation risk "
                "(follow-through, vol term structure, breadth of the move); "
                "SKIP it when today's drop reads as ordinary noise — an index "
                "put bought on every red day just bleeds premium. High ATM IV "
                "favors the spread over the single leg.",
                "",
            ]
        # All third-party text lives inside <market_data> so the system prompt can
        # bind "untrusted data, not instructions" to a clear, delimited region.
        lines += [
            "<market_data>",
            "## Account",
            f"Equity: ${account.equity:,.0f} | Cash: ${account.cash:,.0f} | "
            f"Day P/L: {account.day_pl_pct:+.2f}%",
            f"Open positions: {len(account.positions)}",
        ]
        if benchmark_line:
            lines.append(benchmark_line)
        if external:
            held = ", ".join(
                f"{h.symbol} (${h.market_value:,.0f})" for h in external
            )
            lines.append(f"External holdings (e.g. Robinhood, read-only): {held}")
        # Degraded-feed notes (our own trusted text) rendered where the missing
        # data would otherwise sit, so its absence isn't read as a neutral fact.
        for note in data_health or []:
            lines.append(f"DATA HEALTH: {note}")
        lines.append("")
        # Macro / market-wide context is shared across all symbols.
        if bundles and bundles[0].market_context:
            lines.append("## Market context")
            for s in bundles[0].market_context:
                lines.append(f"- [{s.kind.value}] {self._safe(s.summary)}")
            lines.append("")

        lines.append("## Candidates")
        excluded_set = set(buy_excluded or {})
        for b in bundles:
            pos = account.position_for(b.symbol)
            at_cap = b.symbol in excluded_set
            if pos:
                cap_note = (
                    f" — AT CAP: do NOT propose equity BUY "
                    f"({buy_excluded[b.symbol]})" if at_cap else ""
                )
                # Our ledger's entry conviction + hold age (trusted, not market
                # text) — the incumbent baseline a rotation must clearly beat.
                held_note = (held_notes or {}).get(b.symbol, "")
                held_note = f", {held_note}" if held_note else ""
                tag = (
                    f" (HELD: {pos.qty:g} sh, "
                    f"{pos.unrealized_pl_pct:+.1f}%{held_note}{cap_note})"
                )
            elif any(s.kind is SignalKind.DISCOVERY for s in b.signals):
                tag = " (NEW — surfaced by scanner)"
            else:
                tag = ""
            lines.append(f"### {b.symbol}{tag}")
            comp = (composites or {}).get(b.symbol)
            if comp is not None:
                # Our deterministic weighted index (trusted): per-kind mean
                # score x freshness-lag weight x realized track-record weight.
                lines.append(
                    f"Composite signal index: {comp:+.2f} (deterministic: "
                    "score x freshness x realized track record)"
                )
            sym_notes = (signal_notes or {}).get(b.symbol, {})
            for s in b.signals:
                score = f" score={s.score:+.2f}" if s.score is not None else ""
                # Our freshness/trend annotation (E.1+R.4) — deterministic,
                # computed from persisted history, so safe to render as-is.
                note = sym_notes.get(s.kind.value, "") if s.score is not None else ""
                note = f" {note}" if note else ""
                # Bound per-signal text: a pathological news blurb shouldn't be
                # able to blow up the prompt (and the bill) on its own.
                lines.append(
                    f"- [{s.kind.value}]{score}{note} {self._safe(s.summary)[:240]}"
                )
            lines.append("")

        lines.append("</market_data>")
        lines.append(
            "Return proposals for the candidates that warrant action. Use HOLD "
            "(or omit) symbols where the evidence is thin or conflicting."
        )
        return "\n".join(lines)

    @staticmethod
    def _risk_contract(r) -> list[str]:
        """The standing numeric risk contract as trusted guidance. Only non-off
        limits are shown; slot cap and option-DTE window are rendered elsewhere so
        they're not duplicated here."""
        def pct(v: float) -> str:
            return f"{v:g}%"

        out: list[str] = []
        if getattr(r, "max_position_pct", 0):
            out.append(
                f"- New position ≤ {pct(r.max_position_pct)} of equity; total per "
                f"symbol ≤ {pct(r.max_symbol_exposure_pct)}; one sector ≤ "
                f"{pct(r.max_sector_exposure_pct)}; gross deployed ≤ "
                f"{pct(r.max_gross_exposure_pct)}. A target_weight_pct above these "
                "is clamped down, not honored."
            )
        if getattr(r, "min_conviction", 0):
            out.append(
                f"- Equity buys with conviction < {r.min_conviction:g} are rejected "
                "outright — do not propose them."
            )
        if getattr(r, "min_new_name_conviction", 0):
            out.append(
                f"- A NEW name (not currently held) needs conviction ≥ "
                f"{r.min_new_name_conviction:g} — starter positions on lagged/"
                "crowd theses below that bar are rejected; top-ups are exempt."
            )
        if getattr(r, "min_composite_score", 0) and getattr(r, "composite_gate_enabled", False):
            out.append(
                f"- Buys with a composite signal index < {r.min_composite_score:+g} "
                "are rejected."
            )
        if getattr(r, "max_trade_risk_pct", 0):
            out.append(
                f"- $ at risk per trade (weight × stop%) is capped at "
                f"{pct(r.max_trade_risk_pct)} of equity, so a wider stop shrinks the "
                "position rather than the risk."
            )
        if getattr(r, "min_cash_buffer_pct", 0):
            out.append(
                f"- The book never deploys below a {pct(r.min_cash_buffer_pct)} cash "
                "reserve."
            )
        if getattr(r, "reentry_cooldown_hours", 0):
            out.append(
                f"- A name you SELL is locked out of re-entry for "
                f"{r.reentry_cooldown_hours:g}h — rotate deliberately, not for churn."
            )
        if getattr(r, "earnings_blackout_days", 0):
            out.append(
                f"- No NEW buys within {r.earnings_blackout_days:g} days of a name's "
                "earnings date."
            )
        if getattr(r, "min_trade_price_usd", 0):
            out.append(
                f"- No buys below ${r.min_trade_price_usd:g}/share (liquidity guard)."
            )
        if getattr(r, "options_enabled", False) and getattr(r, "max_option_premium_pct", 0):
            out.append(
                f"- One options play risks at most {pct(r.max_option_premium_pct)} of "
                "equity as net debit."
            )
        if getattr(r, "options_enabled", False) and (
            getattr(r, "max_option_spread_pct", 0)
            or getattr(r, "min_option_open_interest", 0)
        ):
            # Jul 29: F's spread died at this gate and the model could not
            # know why — the cap was enforced but never stated, so conviction
            # kept flowing into un-executable strikes.
            out.append(
                f"- Every option LEG must be liquid: open interest ≥ "
                f"{getattr(r, 'min_option_open_interest', 0):g} and bid-ask "
                f"spread ≤ {getattr(r, 'max_option_spread_pct', 0):g}% of mid. "
                "Prefer high-OI, near-the-money strikes at round-number "
                "levels; a thin or wide-spread leg is auto-rejected however "
                "good the thesis."
            )
        if not out:
            return []
        return [
            "## Risk contract (deterministic caps the downstream layer enforces — "
            "trusted, not market data)",
            *out,
            "Propose WITHIN these: anything past a cap is silently clamped or "
            "vetoed, so conviction spent there is wasted.",
            "",
        ]

    @staticmethod
    def _safe(text: str) -> str:
        """Neutralize the delimiter so a crafted headline can't close the
        <market_data> block early and smuggle text in as trusted instructions."""
        return text.replace("<", "‹").replace(">", "›")

    # -- response parsing --------------------------------------------------- #
    @staticmethod
    def _parse(
        text: str,
    ) -> tuple[list[TradeProposal], dict[str, tuple[str, str]]]:
        """Returns (proposals, bearish verdicts). Verdicts map symbol ->
        (verdict, reason) from the required bearish_verdicts field; a reply
        without the field (old shape, fallback calls) parses to {}."""
        if not text.strip():
            return [], {}
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            log.error("Could not parse decision JSON: %s", e)
            return [], {}

        proposals: list[TradeProposal] = []
        for raw in data.get("proposals", []):
            try:
                proposals.append(TradeProposal(**raw))
            except Exception as e:  # one bad item shouldn't drop the rest
                log.warning("Skipping malformed proposal %s: %s", raw, e)
        verdicts: dict[str, tuple[str, str]] = {}
        for raw in data.get("bearish_verdicts", []) or []:
            try:
                verdicts[str(raw["symbol"]).upper()] = (
                    str(raw["verdict"]), str(raw.get("reason", "")),
                )
            except Exception as e:
                log.warning("Skipping malformed bearish verdict %s: %s", raw, e)
        log.info(
            "Claude returned %d proposal(s), %d bearish verdict(s).",
            len(proposals), len(verdicts),
        )
        return proposals, verdicts
