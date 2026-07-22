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
        curated: str = "",
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
        """
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
        return self._parse(text)

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
                (regime_reason or "The regime filter reads risk-off.")
                + " Your long book loses as prices fall and sizing is already cut.",
                "PROPOSE a DEFINED-RISK downside play to PROFIT from the decline: a "
                "long_put or bear_put_spread on the slate name with the most clearly "
                "BROKEN thesis (price below its moving averages, bearish MACD, "
                "deteriorating options-chain lean). You cannot short stock — a put is "
                "the ONLY way to make money as the market falls. Keep it defined-risk "
                "and within the options premium budget. If NO slate name has a "
                "genuinely bearish, corroborated setup, don't force one.",
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
    def _parse(text: str) -> list[TradeProposal]:
        if not text.strip():
            return []
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            log.error("Could not parse decision JSON: %s", e)
            return []

        proposals: list[TradeProposal] = []
        for raw in data.get("proposals", []):
            try:
                proposals.append(TradeProposal(**raw))
            except Exception as e:  # one bad item shouldn't drop the rest
                log.warning("Skipping malformed proposal %s: %s", raw, e)
        log.info("Claude returned %d proposal(s).", len(proposals))
        return proposals
