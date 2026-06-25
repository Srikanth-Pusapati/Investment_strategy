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
from ..models import AccountSnapshot, SignalBundle, TradeProposal
from .prompts import PROPOSALS_SCHEMA, SYSTEM_PROMPT

log = logging.getLogger("decision")


class DecisionEngine:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
        self.model = cfg.decision_model

    def decide(
        self, bundles: list[SignalBundle], account: AccountSnapshot
    ) -> list[TradeProposal]:
        """Ask Claude for proposals across all candidate symbols at once.

        One call per cycle keeps the macro/market context shared and lets the
        model rank candidates against each other rather than in isolation.
        """
        if not bundles:
            return []

        user_content = self._render(bundles, account)
        try:
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=8000,
                thinking={"type": "adaptive"},
                system=SYSTEM_PROMPT,
                output_config={
                    "format": {"type": "json_schema", "schema": PROPOSALS_SCHEMA}
                },
                messages=[{"role": "user", "content": user_content}],
            )
        except anthropic.APIError as e:
            log.error("Claude decision call failed: %s", e)
            return []

        if resp.stop_reason == "refusal":
            log.warning("Decision model refused; treating as no-action this cycle.")
            return []

        text = next((b.text for b in resp.content if b.type == "text"), "")
        return self._parse(text)

    # -- prompt rendering --------------------------------------------------- #
    def _render(self, bundles: list[SignalBundle], account: AccountSnapshot) -> str:
        lines = [
            "## Account",
            f"Equity: ${account.equity:,.0f} | Cash: ${account.cash:,.0f} | "
            f"Day P/L: {account.day_pl_pct:+.2f}%",
            f"Open positions: {len(account.positions)}",
            "",
        ]
        # Macro / market-wide context is shared across all symbols.
        if bundles and bundles[0].market_context:
            lines.append("## Market context")
            for s in bundles[0].market_context:
                lines.append(f"- [{s.kind.value}] {s.summary}")
            lines.append("")

        lines.append("## Candidates")
        for b in bundles:
            pos = account.position_for(b.symbol)
            held = (
                f" (HELD: {pos.qty:g} sh, {pos.unrealized_pl_pct:+.1f}%)"
                if pos else ""
            )
            lines.append(f"### {b.symbol}{held}")
            for s in b.signals:
                score = f" score={s.score:+.2f}" if s.score is not None else ""
                lines.append(f"- [{s.kind.value}]{score} {s.summary}")
            lines.append("")

        lines.append(
            "Return proposals for the candidates that warrant action. Use HOLD "
            "(or omit) symbols where the evidence is thin or conflicting."
        )
        return "\n".join(lines)

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
