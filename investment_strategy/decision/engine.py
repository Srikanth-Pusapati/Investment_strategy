"""Claude decision engine: signal bundles -> structured trade proposals.

Calls the Anthropic API directly (not MCP) — here Claude is a component we invoke
programmatically with the evidence and get back machine-readable proposals, which
then pass through the deterministic RiskManager before any order is placed.
"""
from __future__ import annotations

import json
import logging

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
    ) -> list[TradeProposal]:
        """Ask Claude for proposals across all candidate symbols at once.

        One call per cycle keeps the macro/market context shared and lets the
        model rank candidates against each other rather than in isolation.
        `lessons` is our own derived track-record (signal attribution), passed as
        trusted context so the model can weight by what has actually paid off.
        `signal_notes` (symbol -> kind -> note) carries OUR deterministic
        freshness/trend annotations (E.1+R.4) — trusted, computed from persisted
        history, never from third-party text.
        """
        if not bundles:
            return []

        user_content = self._render(
            bundles, account, benchmark_line, external or [], lessons,
            today=today, buy_excluded=buy_excluded or {},
            signal_notes=signal_notes or {},
        )
        try:
            resp = self.client.messages.create(
                model=self.model,
                # Headroom so adaptive thinking + a full slate of proposals can't
                # truncate the JSON mid-object (a truncated body fails to parse and
                # silently drops the whole cycle). We detect truncation below too.
                max_tokens=16000,
                thinking={"type": "adaptive"},
                # Cache the static system prompt so it isn't re-billed every cycle.
                # 1h TTL (not the 5m default) so it can survive the gap between
                # decision cycles (see DECISION_INTERVAL_SECONDS). Note: on Opus
                # the minimum cacheable prefix is 4096 tokens; until SYSTEM_PROMPT
                # (plus any future shared context) crosses that, this is a no-op and
                # cache_creation_input_tokens stays 0. The output_config schema is
                # cached automatically for 24h by structured outputs.
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral", "ttl": "1h"},
                    }
                ],
                output_config={
                    "effort": self.cfg.decision_effort,
                    "format": {"type": "json_schema", "schema": PROPOSALS_SCHEMA},
                },
                messages=[{"role": "user", "content": user_content}],
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
    def _render(
        self, bundles: list[SignalBundle], account: AccountSnapshot,
        benchmark_line: str, external: list[ExternalHolding], lessons: str = "",
        today: str = "", buy_excluded: dict[str, str] | None = None,
        signal_notes: dict[str, dict[str, str]] | None = None,
    ) -> str:
        # Our own derived data (track-record, today-so-far, exclusions) sits
        # OUTSIDE <market_data> — it's trusted guidance, not third-party text.
        lines: list[str] = []
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
                tag = f" (HELD: {pos.qty:g} sh, {pos.unrealized_pl_pct:+.1f}%{cap_note})"
            elif any(s.kind is SignalKind.DISCOVERY for s in b.signals):
                tag = " (NEW — surfaced by scanner)"
            else:
                tag = ""
            lines.append(f"### {b.symbol}{tag}")
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
