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

_UNSET = object()  # "not resolved yet" sentinel (distinct from a resolved None)


class RobinhoodReader:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._account_number: Any = _UNSET  # cached agentic account number

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
        # RH's get_equity_positions is scoped per account and REQUIRES the account
        # number. Resolve the dedicated agentic account (never the main portfolio).
        account = self._resolve_account_number()
        if account is None:
            return []
        payload = self.call_json(tool, {"account_number": account})
        return self._parse_holdings(payload) if payload is not None else []

    def _resolve_account_number(self) -> str | None:
        """The account whose holdings we import. An explicit ROBINHOOD_ACCOUNT_NUMBER
        wins; otherwise auto-pick the one with agentic_allowed=true. We NEVER fall
        back to the default (main-portfolio) account — the whole point is to read the
        dedicated, funded agentic account, not your real book. Cached per instance."""
        if self.cfg.robinhood_account_number:
            return self.cfg.robinhood_account_number
        if self._account_number is not _UNSET:
            return self._account_number  # type: ignore[return-value]

        payload = self.call_json("get_accounts", {})
        accounts = payload.get("accounts") if isinstance(payload, dict) else (payload or [])
        agentic = [a for a in (accounts or []) if a.get("agentic_allowed")]
        if not agentic:
            log.warning(
                "Robinhood: no agentic-enabled account found (token sees %d account(s)). "
                "Set ROBINHOOD_ACCOUNT_NUMBER to choose one; skipping holdings import.",
                len(accounts or []),
            )
            self._account_number = None
            return None
        if len(agentic) > 1:
            log.info(
                "Robinhood: %d agentic accounts visible; using the first. Pin one with "
                "ROBINHOOD_ACCOUNT_NUMBER to be explicit.", len(agentic),
            )
        num = str(agentic[0].get("account_number") or "") or None
        self._account_number = num
        return num

    def _quote_prices(self, symbols: list[str]) -> dict[str, float]:
        """{symbol: last price} via one batched get_equity_quotes read. Used to turn
        RH's price-less position rows into a market value + unrealized P&L. Best
        effort: any symbol without a usable price is simply omitted."""
        if not symbols:
            return {}
        payload = self.call_json("get_equity_quotes", {"symbols": symbols})
        results = payload.get("results", []) if isinstance(payload, dict) else (payload or [])
        out: dict[str, float] = {}
        for r in results or []:
            q = (r.get("quote") if isinstance(r, dict) else None) or r
            sym = str(q.get("symbol", "")).upper()
            price = self._opt_float(
                q.get("last_trade_price")
                or q.get("last_non_reg_trade_price")
                or q.get("previous_close")
            )
            if sym and price is not None:
                out[sym] = price
        return out

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
        # terminate_on_close=False: RH's MCP rejects the session-termination DELETE
        # with a 400, which the SDK logs as a scary (but harmless) warning on every
        # call. We open a fresh session per call anyway, so skip the teardown DELETE.
        async with streamablehttp_client(
            self.cfg.robinhood_mcp_url, headers=headers, auth=auth,
            terminate_on_close=False,
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
        positions/results list) to ExternalHolding. Defensive: unknown shapes -> [].

        RH's get_equity_positions rows carry symbol + quantity + average_buy_price but
        NO market value or P&L, so we batch-fetch live quotes and derive them
        (market_value = qty*price; unrealized_pl_pct off the avg buy). A tool that
        already provides market_value/unrealized_pl_pct is honoured as-is."""
        if isinstance(payload, list):
            rows = payload
        elif isinstance(payload, dict):
            rows = payload.get("positions") or payload.get("results") or []
        else:
            log.warning("Robinhood MCP returned no parseable positions payload.")
            return []

        # First pass: pull the raw fields (market_value/P&L may be absent).
        parsed: list[tuple[str, float, float | None, float | None, float | None]] = []
        for r in rows or []:
            sym = str(r.get("symbol") or r.get("ticker") or "").upper()
            qty = self._opt_float(r.get("quantity") or r.get("qty")) or 0.0
            if not sym or qty == 0.0:
                continue
            avg = self._opt_float(r.get("average_buy_price") or r.get("average_price"))
            mv = self._opt_float(r.get("market_value") or r.get("equity"))
            upl = self._opt_float(r.get("unrealized_pl_pct") or r.get("percent_change"))
            parsed.append((sym, qty, avg, mv, upl))

        # Enrich the rows missing a market value with one batched quote read.
        need = [p[0] for p in parsed if p[3] is None]
        prices = self._quote_prices(need) if need else {}

        out: list[ExternalHolding] = []
        for sym, qty, avg, mv, upl in parsed:
            price = prices.get(sym)
            if mv is None:
                # live market value if we have a price, else cost basis, else 0
                mv = qty * price if price is not None else (qty * avg if avg else 0.0)
            if upl is None and price is not None and avg:
                upl = (price / avg - 1.0) * 100.0
            out.append(ExternalHolding(
                source="robinhood", symbol=sym, qty=qty,
                market_value=round(mv, 2), unrealized_pl_pct=upl,
            ))
        log.info("Imported %d Robinhood holding(s) for context (read-only).", len(out))
        return out

    @staticmethod
    def _opt_float(v) -> float | None:
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
