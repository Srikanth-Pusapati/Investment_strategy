"""Intra-day decision journal (B1).

Records EVERY risk verdict — approved, resized, rejected, slate-excluded, and
backstop-dropped — so Claude can see its own behavior within the trading day.
The 'Today so far' block injected into the decision prompt closes the statefulness
gap: without it, every cycle Claude sees the same candidate slate and re-proposes
the same names with no awareness that it already bought them three times today.

All writes are best-effort (no write can break the trade loop). Persists to
state/decisions/YYYY-MM-DD.jsonl, one record per line, keyed by the ET trading day.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from zoneinfo import ZoneInfo

log = logging.getLogger("journal")

_ET = ZoneInfo("America/New_York")
_DECISIONS_DIR = Path("state") / "decisions"

Verdict = Literal[
    "approved", "resized", "rejected", "slate_excluded", "dropped_buy",
    "rotation_guard",
]


def _trading_day(when: datetime | None = None) -> str:
    when = when or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(_ET).date().isoformat()


class DecisionRecord:
    __slots__ = (
        "ts", "symbol", "action", "instrument", "conviction",
        "target_weight_pct", "verdict", "approved_notional", "reason",
        "rationale_head",
    )

    def __init__(
        self,
        ts: str,
        symbol: str,
        action: str,
        instrument: str,
        conviction: float,
        target_weight_pct: float,
        verdict: Verdict,
        approved_notional: float = 0.0,
        reason: str = "",
        rationale_head: str = "",
    ):
        self.ts = ts
        self.symbol = symbol
        self.action = action
        self.instrument = instrument
        self.conviction = conviction
        self.target_weight_pct = target_weight_pct
        self.verdict = verdict
        self.approved_notional = approved_notional
        self.reason = reason[:200]
        self.rationale_head = rationale_head[:120]

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__slots__}

    @staticmethod
    def from_dict(d: dict) -> "DecisionRecord":
        return DecisionRecord(
            ts=d.get("ts", ""),
            symbol=d.get("symbol", ""),
            action=d.get("action", ""),
            instrument=d.get("instrument", "equity"),
            conviction=float(d.get("conviction", 0.0)),
            target_weight_pct=float(d.get("target_weight_pct", 0.0)),
            verdict=d.get("verdict", "rejected"),
            approved_notional=float(d.get("approved_notional", 0.0)),
            reason=d.get("reason", ""),
            rationale_head=d.get("rationale_head", ""),
        )


class DecisionJournal:
    """Append-only per-day decision journal. Thread-safe writes via best-effort
    file append; reads load the whole day file (small: <500 records/day)."""

    def __init__(self, base_dir: Path | str = _DECISIONS_DIR):
        self.base_dir = Path(base_dir)

    def _day_path(self, day: str) -> Path:
        return self.base_dir / f"{day}.jsonl"

    def record(self, rec: DecisionRecord) -> None:
        try:
            self.base_dir.mkdir(parents=True, exist_ok=True)
            path = self._day_path(_trading_day())
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec.to_dict()) + "\n")
        except Exception as e:
            log.warning("Journal write failed: %s", e)

    def today(self, when: datetime | None = None) -> list[DecisionRecord]:
        try:
            path = self._day_path(_trading_day(when))
            if not path.exists():
                return []
            recs = []
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    recs.append(DecisionRecord.from_dict(json.loads(line)))
                except Exception:
                    pass
            return recs
        except Exception as e:
            log.warning("Journal read failed: %s", e)
            return []

    def has_records_today(self, when: datetime | None = None) -> bool:
        try:
            return self._day_path(_trading_day(when)).exists()
        except Exception:
            return False

    def render_today(
        self, equity: float, when: datetime | None = None,
        options_on: bool = False,
    ) -> str:
        """A token-bounded 'Today so far' block for the decision prompt.

        Aggregates per-symbol so the block stays short even on a busy day;
        sorts by total approved $ desc; caps at 15 lines. `options_on` adds a
        call-candidate tag to rejections from the EQUITY-only gates
        (overextension / earnings blackout) — without it the model reads
        "Overextended: rejected" as the THESIS being rejected and never pivots
        to the defined-risk option vehicle those gates deliberately exempt."""
        recs = self.today(when)
        if not recs:
            return ""

        buy_spent: dict[str, float] = defaultdict(float)
        buy_count: dict[str, int] = defaultdict(int)
        buy_last_conv: dict[str, float] = {}
        excluded: dict[str, str] = {}  # symbol -> reason (last seen)
        rejected_count: dict[str, int] = defaultdict(int)
        rejected_reason: dict[str, str] = {}
        guard_vetoed: dict[str, str] = {}  # sell vetoes (rotation_guard etc.)
        sold: dict[str, str] = {}          # executed sells (rationale head)
        hold_count: dict[str, int] = defaultdict(int)
        hold_last: dict[str, tuple[float, str]] = {}  # conv, rationale head

        for r in recs:
            if r.action == "sell" and r.verdict == "rotation_guard":
                # Surface guard vetoes so the model doesn't re-propose the
                # SAME rotation every cycle for the rest of the day.
                guard_vetoed[r.symbol] = r.reason
                continue
            if r.action == "sell" and r.verdict in ("approved", "resized"):
                sold[r.symbol] = r.rationale_head or r.reason
                continue
            if r.action == "hold":
                # The model's own HOLD verdicts, per symbol — so the next cycle
                # re-examines its prior reasoning instead of re-anchoring
                # fresh (Jul 29 NU: three cycles of blind hold into the stop).
                # Option-instrument holds are synthetic records (fallback
                # declines on names the book never held) — they feed the
                # fallback attempt cap, not this line.
                if r.instrument != "option":
                    hold_count[r.symbol] += 1
                    hold_last[r.symbol] = (r.conviction, r.rationale_head)
                continue
            if r.action != "buy":
                continue
            if r.verdict in ("approved", "resized"):
                buy_spent[r.symbol] += r.approved_notional
                buy_count[r.symbol] += 1
                buy_last_conv[r.symbol] = r.conviction
            elif r.verdict == "slate_excluded":
                excluded[r.symbol] = r.reason
            elif r.verdict == "dropped_buy":
                excluded[r.symbol] = r.reason or "buy excluded"
            elif r.verdict == "rejected":
                rejected_count[r.symbol] += 1
                rejected_reason[r.symbol] = r.reason

        lines = [
            "## Today so far (your own actions this trading day — "
            "trusted, not market data)"
        ]

        if buy_spent:
            pct_eq = (lambda n: f", {n / equity * 100:.1f}% eq") if equity > 0 else (lambda n: "")
            parts = []
            for sym, spent in sorted(buy_spent.items(), key=lambda x: -x[1])[:8]:
                conv = buy_last_conv.get(sym, 0)
                parts.append(
                    f"{sym} {buy_count[sym]}x "
                    f"(${spent:,.0f}{pct_eq(spent)}, last conv {conv:.2f})"
                )
            lines.append("Bought: " + ", ".join(parts))
        else:
            lines.append("Bought: nothing yet today.")

        if sold:
            lines.append(
                "Sold: " + ", ".join(
                    f"{sym} ({why[:60]})" if why else sym
                    for sym, why in list(sold.items())[:6]
                )
            )

        if hold_count:
            hold_parts = []
            for sym, n in list(hold_count.items())[:8]:
                conv, head = hold_last.get(sym, (0.0, ""))
                hold_parts.append(
                    f"{sym} {n}x (last conv {conv:.2f}"
                    + (f", '{head[:50]}'" if head else "")
                    + ")"
                )
            lines.append(
                "Held (your own prior verdicts today — re-examine, don't "
                "re-anchor): " + ", ".join(hold_parts)
            )

        if excluded:
            excl_parts = [
                f"{sym} ({reason[:60]})"
                for sym, reason in list(excluded.items())[:6]
            ]
            if len(excluded) > 6:
                excl_parts.append(f"…+{len(excluded) - 6} more")
            lines.append("Buys now excluded (at cap/cooldown): " + ", ".join(excl_parts))

        if rejected_count:
            rej_parts = [
                f"{sym} {n}x ({rejected_reason.get(sym, '')[:50]})"
                for sym, n in list(rejected_count.items())[:4]
            ]
            lines.append("Rejected today: " + ", ".join(rej_parts))
            if options_on:
                # These reason strings only ever come from the equity buy path
                # (risk._evaluate_buy). Overextension is NO LONGER an option
                # escape hatch: evaluate_option runs bullish debits through
                # the same _overextension_read (OPTION CHASE GATE, Aug 2026 —
                # the HL -67.6% re-expression). Only the earnings blackout
                # stays equity-only (a defined-risk debit through earnings is
                # a legitimate bounded-loss expression).
                call_cands = sorted(
                    sym for sym in rejected_count
                    if rejected_reason.get(sym, "").startswith("Earnings in")
                )
                if call_cands:
                    lines.append(
                        "Of those, " + ", ".join(call_cands[:6]) + " fell to "
                        "the EQUITY-only earnings blackout — option debits "
                        "are exempt from that gate. If the bullish thesis "
                        "still holds at high conviction, propose a capped-debit "
                        "long_call/bull_call_spread instead (max loss = the "
                        "premium, no stop to gap through; direction/DTE/"
                        "liquidity gates still apply). Overextension rejects "
                        "are NOT re-expressible: bullish option debits hit "
                        "the same chase gate."
                    )

        if guard_vetoed:
            veto_parts = [
                f"{sym} ({reason[:70]})"
                for sym, reason in list(guard_vetoed.items())[:4]
            ]
            lines.append(
                "Rotation sells VETOED today (loss-locking without a clear "
                "incoming edge): " + ", ".join(veto_parts)
                + ". Do not re-propose the same rotation without a stronger "
                "incoming candidate."
            )

        if excluded:
            lines.append(
                "Do not re-propose excluded buys; spend conviction on alternatives."
            )

        return "\n".join(lines)
