"""Read-only Robinhood holdings via the OFFICIAL Agentic Trading MCP — CONTEXT ONLY.

Robinhood's Agentic Trading exposes an official MCP endpoint (OAuth-authenticated).
We connect as a plain MCP client and call READ tools only, to pull the agentic
account's positions so Claude knows what you already hold. We NEVER call trade
tools — all execution stays on Alpaca behind the RiskManager.

Important caveats:
  - The agentic MCP is scoped to a SEPARATE, dedicated Robinhood account (the one
    you fund for the agent), NOT your main brokerage portfolio.
  - This calls the MCP tool DIRECTLY from Python (not via Claude), so the data
    goes straight into our prompt context — Claude never gets the trade tool.
  - Tool names/schemas vary; on first run with ROBINHOOD_POSITIONS_TOOL unset we
    connect, log the available tools, and return [] so you can set the right name.
  - AUTH: run the OAuth handshake once (see robinhood_auth.py:
    `python -m investment_strategy.portfolio.robinhood_auth login`). It persists an
    access + refresh token that the SDK auto-refreshes here. A legacy pasted
    ROBINHOOD_MCP_TOKEN still works as a fallback. RH advertises a single scope
    ("internal") — there is NO read-only token; the trade-capable token is kept safe
    only by this client calling read tools and by you authorizing a dedicated account.

Disabled (and a no-op) unless ROBINHOOD_ENABLED=on with a URL and either a completed
OAuth handshake or a pasted token.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from ..config import Config
from ..models import ExternalHolding

log = logging.getLogger("robinhood")


class RobinhoodReader:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    @property
    def enabled(self) -> bool:
        from .robinhood_auth import has_tokens

        return (
            self.cfg.robinhood_enabled
            and bool(self.cfg.robinhood_mcp_url)
            # Either the OAuth handshake has been completed (preferred: auto-refresh)
            # or a legacy pre-obtained Bearer token is pasted in the env.
            and (has_tokens(self.cfg) or bool(self.cfg.robinhood_mcp_token))
        )

    def holdings(self) -> list[ExternalHolding]:
        """Sync entry point for the orchestrator. Returns [] if disabled/failed."""
        if not self.enabled:
            return []
        tool = self.cfg.robinhood_positions_tool
        if not tool:
            # Without the configured holdings tool we can't know which read tool
            # returns positions — list what's available once, then no-op.
            names = self.call_json("__list_tools__")
            log.info(
                "Robinhood MCP connected. Set ROBINHOOD_POSITIONS_TOOL to the "
                "holdings tool to enable import. Available tools: %s", names,
            )
            return []
        payload = self.call_json(tool, {})
        return self._parse_holdings(payload) if payload is not None else []

    # -- generic read-only MCP access -------------------------------------- #
    def call_json(self, tool: str, arguments: dict | None = None) -> Any | None:
        """Call any Robinhood MCP READ tool and return its parsed JSON payload
        (dict/list), or None on any failure. The sentinel tool "__list_tools__"
        returns the list of available tool names instead of calling one. Sync
        wrapper around the async MCP client so callers (screeners/signals) stay
        simple. NEVER call a trade tool here — reads only."""
        if not self.enabled:
            return None
        try:
            return asyncio.run(self._call_tool(tool, arguments or {}))
        except Exception as e:
            log.warning("Robinhood MCP call %s failed: %s", tool, e)
            return None

    async def _call_tool(self, tool: str, arguments: dict) -> Any:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        from .robinhood_auth import build_provider, has_tokens

        # Prefer the persisted OAuth handshake (the SDK refreshes the token in the
        # background and re-persists it). Fall back to a legacy pasted Bearer token.
        # interactive=False so a dead refresh token surfaces as an error we catch —
        # a trading loop must never block on a browser prompt.
        auth = headers = None
        if has_tokens(self.cfg):
            auth = build_provider(self.cfg, interactive=False)
        else:
            headers = {"Authorization": f"Bearer {self.cfg.robinhood_mcp_token}"}
        async with streamablehttp_client(
            self.cfg.robinhood_mcp_url, headers=headers, auth=auth
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                if tool == "__list_tools__":
                    tools = await session.list_tools()
                    return [t.name for t in tools.tools]
                result = await session.call_tool(tool, arguments=arguments)
                return self._payload(result)

    @staticmethod
    def _payload(result: Any) -> Any | None:
        """Extract the first JSON content block from an MCP tool result. Robinhood
        wraps the useful data under a top-level "data" key; unwrap it when present
        so callers see the records directly."""
        for block in getattr(result, "content", []) or []:
            text = getattr(block, "text", None)
            if not text:
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and "data" in payload:
                return payload["data"]
            return payload
        return None

    # -- defensive parsing -------------------------------------------------- #
    def _parse_holdings(self, payload: Any) -> list[ExternalHolding]:
        """Map an already-unwrapped positions payload (list, or a dict carrying a
        positions/results list) to ExternalHolding. Defensive: unknown shapes -> []."""
        if isinstance(payload, list):
            rows = payload
        elif isinstance(payload, dict):
            rows = payload.get("positions") or payload.get("results") or []
        else:
            log.warning("Robinhood MCP returned no parseable positions payload.")
            return []
        out: list[ExternalHolding] = []
        for r in rows or []:
            try:
                out.append(ExternalHolding(
                    source="robinhood",
                    symbol=str(r.get("symbol") or r.get("ticker")),
                    qty=float(r.get("quantity") or r.get("qty") or 0),
                    market_value=float(r.get("market_value") or r.get("equity") or 0),
                    unrealized_pl_pct=self._opt_float(
                        r.get("unrealized_pl_pct") or r.get("percent_change")
                    ),
                ))
            except (TypeError, ValueError):
                continue
        log.info("Imported %d Robinhood holding(s) for context (read-only).", len(out))
        return out

    @staticmethod
    def _opt_float(v) -> float | None:
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
