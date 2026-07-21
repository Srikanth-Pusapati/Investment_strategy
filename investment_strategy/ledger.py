"""Trade ledger — the durable audit log of every order the bot actually places.

Orders are submitted to Alpaca, but Alpaca only remembers the *mechanical* facts
(symbol, qty, price, time). The *why* — Claude's rationale, conviction, the
key signals, and the planned exit (take-profit / stop-loss) the risk layer
approved — lives only in memory during a decision cycle and is otherwise lost.

This module persists that full context, one JSON object per line, so the
dashboard (and any future backtest/audit) can answer: what did we buy, when,
how much did it cost, why, and what was the planned exit?

Exits are PRICE-triggered brackets, not calendar dates — there is no "sell date"
in this system. We record the take-profit / stop-loss targets instead, which is
the honest analogue of an "assumed sell" level.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from .models import RiskDecision

log = logging.getLogger("ledger")

# state/ is already gitignored — local trade history never gets committed.
DEFAULT_LEDGER_PATH = Path("state") / "trades.jsonl"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class TradeRecord(BaseModel):
    """One executed order, with the decision context that produced it."""
    ts: datetime = Field(default_factory=_now)
    symbol: str
    action: str                       # "buy" | "sell" | "correct"
    instrument: str = "equity"        # "equity" | "option"

    qty: float = 0.0
    entry_price: float = 0.0          # fill/quote price at execution (0 if n/a)
    cost_usd: float = 0.0             # dollars deployed (notional)

    conviction: float = 0.0           # Claude's 0..1 conviction
    take_profit_pct: float = 0.0      # planned profit target — "profit % assumed"
    stop_loss_pct: float = 0.0        # planned downside cap
    take_profit_price: Optional[float] = None
    stop_loss_price: Optional[float] = None

    # Fill/quote price at exit (sells) — the counterpart of entry_price, needed
    # for FIFO lot P&L (lots.py). None on old records; lots.py then reconstructs
    # a price from realized_pl_pct and flags the result as estimated.
    exit_price: Optional[float] = None

    rationale: str = ""               # why — the reason behind the purchase
    key_signals: list[str] = Field(default_factory=list)
    # SignalKind values present in the bundle at entry (buys) — the attribution
    # layer joins these to the round-trip's realized P&L to score each source.
    entry_signals: list[str] = Field(default_factory=list)
    # Realized outcome at close (sells/exits): the position's unrealized P&L at the
    # moment we issued the close, which IS the realized result. None for buys.
    realized_pl_pct: Optional[float] = None
    realized_pl: Optional[float] = None
    verdict: str = ""                 # risk verdict: approved | resized
    risk_note: str = ""               # risk layer's sizing note
    exit_reason: str = ""             # what closed it: decision | stop | take | trail | flatten
                                      # | time | thesis_decay | regime_trim | scale
                                      # | bracket_stop | bracket_take | external (F.1 backfill)
    option_strategy: Optional[str] = None
    order_id: Optional[str] = None
    # Deterministic weighted signal index at entry (buys; signals/composite.py).
    # None on sells and on records predating the composite — lets future
    # calibration score the composite against realized outcomes.
    composite_score: Optional[float] = None

    # -- builders ---------------------------------------------------------- #
    @classmethod
    def from_equity(
        cls, decision: RiskDecision, entry_price: float,
        order_id: Optional[str], entry_signals: Optional[list[str]] = None,
        submitted_qty: Optional[float] = None,
        submitted_cost: Optional[float] = None,
        composite_score: Optional[float] = None,
    ) -> "TradeRecord":
        """`submitted_qty`/`submitted_cost` are what actually went to the broker
        when it differs from the decision (whole-share flooring drops the
        sub-share remainder) — the ledger must reflect the order, not the
        intent, or the divergence is invisible to fill reconciliation."""
        p = decision.proposal
        qty = submitted_qty if submitted_qty is not None else decision.approved_qty
        cost = (
            submitted_cost if submitted_cost is not None
            else decision.approved_notional or (decision.approved_qty * entry_price)
        )
        tp_price = sl_price = None
        if entry_price > 0:
            tp_price = round(entry_price * (1 + decision.take_profit_pct / 100.0), 2)
            sl_price = round(entry_price * (1 - decision.stop_loss_pct / 100.0), 2)
        return cls(
            symbol=p.symbol, action=p.action.value, instrument="equity",
            qty=qty, entry_price=entry_price, cost_usd=cost,
            conviction=p.conviction,
            take_profit_pct=decision.take_profit_pct,
            stop_loss_pct=decision.stop_loss_pct,
            take_profit_price=tp_price, stop_loss_price=sl_price,
            rationale=p.rationale, key_signals=p.key_signals,
            entry_signals=entry_signals or [],
            verdict=decision.verdict.value, risk_note=decision.reason,
            order_id=order_id,
            composite_score=composite_score,
        )

    @classmethod
    def from_option(
        cls, decision: RiskDecision, premium: float, order_id: Optional[str],
        entry_signals: Optional[list[str]] = None,
    ) -> "TradeRecord":
        p = decision.proposal
        return cls(
            symbol=p.symbol, action=p.action.value, instrument="option",
            qty=decision.approved_qty, entry_price=premium,
            cost_usd=decision.approved_notional,
            conviction=p.conviction,
            take_profit_pct=decision.take_profit_pct,
            stop_loss_pct=decision.stop_loss_pct,
            rationale=p.rationale, key_signals=p.key_signals,
            entry_signals=entry_signals or [],
            verdict=decision.verdict.value, risk_note=decision.reason,
            option_strategy=p.option_strategy.value if p.option_strategy else None,
            order_id=order_id,
        )

    @classmethod
    def from_core_fill(
        cls, symbol: str, notional: float, entry_price: float,
        order_id: Optional[str],
    ) -> "TradeRecord":
        """A core-satellite (Todo 1.6) top-up buy of the broad CORE_ETF. It is NOT
        a Claude proposal — it deploys idle cash toward TARGET_INVESTED_PCT — so it
        carries no conviction/thesis and no per-name stop (account-level guards
        protect the core). exit_reason left blank; entry marked 'core_fill'."""
        qty = round(notional / entry_price, 6) if entry_price > 0 else 0.0
        return cls(
            symbol=symbol, action="buy", instrument="equity", qty=qty,
            entry_price=entry_price, cost_usd=round(notional, 2),
            rationale="core-satellite fill: deploy idle cash toward target invested %",
            entry_signals=["core_fill"], verdict="approved",
            risk_note="core ETF — exempt from single-name caps", order_id=order_id,
        )

    @classmethod
    def for_sell(
        cls, symbol: str, rationale: str, order_id: Optional[str],
        qty: float = 0.0, key_signals: Optional[list[str]] = None,
        realized_pl_pct: Optional[float] = None, realized_pl: Optional[float] = None,
        exit_reason: str = "decision", instrument: str = "equity",
        ts: Optional[datetime] = None, exit_price: Optional[float] = None,
        composite_score: Optional[float] = None,
    ) -> "TradeRecord":
        """`ts` overrides the record time — the exchange-exit backfill (F.1)
        stamps the order's actual FILL time so attribution's chronological
        round-trip pairing sees the exit where it really happened, not when the
        backfill noticed it. `exit_price` is the sell's fill/quote price —
        record it whenever known so FIFO lot P&L (GA-2.5) has a real basis.
        `composite_score` is the name's weighted composite at exit — recorded so
        sell rows aren't blind to it (buys already carry it)."""
        kwargs: dict = dict(
            symbol=symbol, action="sell", instrument=instrument, qty=qty,
            rationale=rationale, key_signals=key_signals or [], order_id=order_id,
            realized_pl_pct=realized_pl_pct, realized_pl=realized_pl,
            exit_reason=exit_reason, exit_price=exit_price,
            composite_score=composite_score,
        )
        if ts is not None:
            kwargs["ts"] = ts
        return cls(**kwargs)

    @classmethod
    def correction(
        cls, order_id: str, symbol: str, status: str,
        filled_qty: float, orig_qty: float,
    ) -> "TradeRecord":
        """A CORRECTION for an earlier record whose order did not (fully) execute
        (goGA GA-2.5). The ledger is append-only, so a reject/cancel/partial found
        at reconcile is fixed by appending a record that points at the original
        via order_id: `qty` is the ACTUAL filled quantity (0 = the intent never
        executed at all). effective() applies these — voiding zero-fill records
        and resizing partials — so every consumer (dashboard, attribution, lots,
        track record) sees the broker's reality, not the recorded intent."""
        return cls(
            symbol=symbol, action="correct", qty=max(0.0, float(filled_qty)),
            rationale=(
                f"reconcile correction: order ended {status} with "
                f"{filled_qty:g}/{orig_qty:g} filled"
            ),
            risk_note=status, order_id=order_id, exit_reason="correction",
        )


class TradeLedger:
    """Append-only JSON-Lines ledger. One file, one record per line."""

    def __init__(self, path: Path | str = DEFAULT_LEDGER_PATH):
        self.path = Path(path)

    def record(self, rec: TradeRecord) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(rec.model_dump_json() + "\n")
            # Sells carry no cost_usd; show the exit proceeds instead so the log
            # doesn't read "SELL MXL qty=12 $0" for a $1,200 close.
            value = rec.cost_usd
            if not value and rec.action == "sell" and rec.exit_price and rec.qty:
                value = rec.exit_price * rec.qty
            log.info("Ledger: %s %s qty=%g $%.0f",
                     rec.action.upper(), rec.symbol, rec.qty, value)
        except Exception as e:  # never let logging break the trade loop
            log.warning("Ledger write failed for %s: %s", rec.symbol, e)

    def all(self) -> list[TradeRecord]:
        if not self.path.exists():
            return []
        out: list[TradeRecord] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(TradeRecord.model_validate_json(line))
            except Exception as e:
                log.warning("Skipping malformed ledger line: %s", e)
        return out

    def effective(self) -> list[TradeRecord]:
        """Records with reconcile CORRECTIONS applied (goGA GA-2.5): a corrected
        record whose order filled 0 is dropped (the intent never executed — the
        old phantom-BUY-row bug); a partial fill is resized to what actually
        filled (qty, cost, and realized $ scaled proportionally; per-share
        prices and % are size-independent and stand). Correction rows themselves
        are consumed, never returned. Every read-side consumer (dashboard,
        attribution, lots, track record) should use this, not all()."""
        records = self.all()
        # Last correction per order id wins (a re-reconcile can refine a fill).
        corrections = {
            r.order_id: r for r in records
            if r.action == "correct" and r.order_id
        }
        out: list[TradeRecord] = []
        for r in records:
            if r.action == "correct":
                continue
            c = corrections.get(r.order_id) if r.order_id else None
            if c is None:
                out.append(r)
                continue
            if c.qty <= 0:
                continue  # order never executed — void the recorded intent
            if r.qty > 0 and c.qty < r.qty:
                frac = c.qty / r.qty
                r = r.model_copy(update={
                    "qty": c.qty,
                    "cost_usd": round(r.cost_usd * frac, 2),
                    "realized_pl": (
                        r.realized_pl * frac if r.realized_pl is not None else None
                    ),
                    "risk_note": (r.risk_note + " " if r.risk_note else "")
                    + f"[corrected: {c.qty:g}/{r.qty:g} filled]",
                })
            out.append(r)
        return out
