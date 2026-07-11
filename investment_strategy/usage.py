"""Anthropic API usage ledger: one JSONL line per call, plus a daily tally.

The Console dashboard only shows org-level aggregates; this gives per-call
visibility (tokens, cache behavior, estimated $) so cost regressions show up
in the logs the same day, not on the monthly bill.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("usage")

DEFAULT_USAGE_PATH = Path("state") / "api_usage.jsonl"

# $/MTok (input, output). Cache reads bill at 0.1x input; cache writes at 2x
# input for the 1h TTL the decision engine uses.
PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
_CACHE_READ_MULT = 0.1
_CACHE_WRITE_MULT = 2.0


def estimate_cost_usd(
    model: str, in_tok: int, out_tok: int, cache_read: int, cache_write: int,
) -> float:
    in_rate, out_rate = PRICES.get(model, (5.0, 25.0))  # unknown model: assume Opus
    return (
        in_tok * in_rate
        + out_tok * out_rate
        + cache_read * in_rate * _CACHE_READ_MULT
        + cache_write * in_rate * _CACHE_WRITE_MULT
    ) / 1_000_000


def record_usage(
    resp, model: str, call_type: str, path: Path = DEFAULT_USAGE_PATH,
) -> None:
    """Append one usage record for an Anthropic Messages response.

    Never raises — cost accounting must not break the trading loop.
    """
    try:
        u = resp.usage
        in_tok = u.input_tokens or 0
        out_tok = u.output_tokens or 0
        cache_read = getattr(u, "cache_read_input_tokens", 0) or 0
        cache_write = getattr(u, "cache_creation_input_tokens", 0) or 0
        cost = estimate_cost_usd(model, in_tok, out_tok, cache_read, cache_write)
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "call_type": call_type,
            "model": model,
            "in": in_tok,
            "out": out_tok,
            "cache_read": cache_read,
            "cache_write": cache_write,
            "est_cost_usd": round(cost, 6),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        log.info(
            "%s call: %d in / %d out (cache r=%d w=%d) ~$%.4f [%s]",
            call_type, in_tok, out_tok, cache_read, cache_write, cost, model,
        )
    except Exception as e:
        log.warning("Could not record API usage: %s", e)


def summarize_day(
    day: str | None = None, path: Path = DEFAULT_USAGE_PATH,
) -> tuple[int, int, int, float]:
    """Return (calls, input_tokens, output_tokens, est_cost_usd) for a UTC day
    (``YYYY-MM-DD``, default today)."""
    day = day or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    calls = in_tok = out_tok = 0
    cost = 0.0
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not str(rec.get("ts", "")).startswith(day):
                    continue
                calls += 1
                in_tok += rec.get("in", 0)
                out_tok += rec.get("out", 0)
                cost += rec.get("est_cost_usd", 0.0)
    except FileNotFoundError:
        pass
    return calls, in_tok, out_tok, cost
