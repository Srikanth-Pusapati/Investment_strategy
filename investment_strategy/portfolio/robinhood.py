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
  - Prefer a read-scoped OAuth token if Robinhood supports scopes. Even though we
    only call read tools, a trade-capable token could place orders if misused.

Disabled (and a no-op) unless ROBINHOOD_ENABLED=on with a URL + token.
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
        return (
            self.cfg.robinhood_enabled
            and bool(self.cfg.robinhood_mcp_url)
            and bool(self.cfg.robinhood_mcp_token)
        )

    def holdings(self) -> list[ExternalHolding]:
        """Sync entry point for the orchestrator. Returns [] if disabled/failed."""
        if not self.enabled:
            return []
        try:
            return asyncio.run(self._fetch())
        except Exception as e:
            log.warning("Robinhood MCP read failed: %s", e)
            return []

    # -- MCP client (read-only) -------------------------------------------- #
    async def _fetch(self) -> list[ExternalHolding]:
        try:
            from mcp import ClientSession
            from mcp.client.streamable_http import streamablehttp_client
        except ImportError:
            log.warning("`mcp` SDK not installed; skipping Robinhood import.")
            return []

        headers = {"Authorization": f"Bearer {self.cfg.robinhood_mcp_token}"}
        async with streamablehttp_client(
            self.cfg.robinhood_mcp_url, headers=headers
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = [t.name for t in tools.tools]

                tool = self.cfg.robinhood_positions_tool
                if not tool:
                    log.info(
                        "Robinhood MCP connected. Available tools: %s. "
                        "Set ROBINHOOD_POSITIONS_TOOL to the holdings tool to "
                        "enable import.", names,
                    )
                    return []
                if tool not in names:
                    log.warning(
                        "ROBINHOOD_POSITIONS_TOOL=%s not in MCP tools %s.",
                        tool, names,
                    )
                    return []

                result = await session.call_tool(tool, arguments={})
                return self._parse(result)

    # -- defensive parsing -------------------------------------------------- #
    def _parse(self, result: Any) -> list[ExternalHolding]:
        """MCP tool results carry content blocks; positions usually arrive as a
        JSON text block. Parse defensively and map to ExternalHolding."""
        payload = None
        for block in getattr(result, "content", []) or []:
            text = getattr(block, "text", None)
            if text:
                try:
                    payload = json.loads(text)
                    break
                except json.JSONDecodeError:
                    continue
        if payload is None:
            log.warning("Robinhood MCP returned no parseable positions payload.")
            return []

        rows = payload if isinstance(payload, list) else payload.get("positions", [])
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
