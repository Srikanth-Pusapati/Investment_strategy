"""FIFO lot tracking — realized P&L with a correct multi-lot basis (goGA GA-2.5).

The ledger records orders; this module derives LOTS from it. Before this, realized
P&L used the position's average (watchdog exits) or the symbol's most recent buy
price (exchange-exit backfill) — wrong for multi-lot names (FRHC was the observed
case). Here every buy opens a Lot and every sell consumes lots FIRST-IN-FIRST-OUT,
so each realized slice carries the basis of the shares actually sold.

Lots are DERIVED, never stored: they are rebuilt from the corrected ledger stream
(TradeLedger.effective()) on every read, so there is no second file to drift out
of sync. The records themselves are schema-shaped, append-only pydantic models
with explicit lot ids and a schema_version stamp, so the GA-4.5 DB migration is a
storage swap (persist these rows), not a logic rewrite.

Wash-sale flag (= Todo-3 L.2, GA-3.1): a realized LOSS with another BUY of the
same symbol within ±30 calendar days of the sale is flagged wash_sale=True. This
is a heads-up for the operator's tax notes, NOT tax advice — the broker's 1099-B
is the source of truth (it also has visibility we don't, e.g. across accounts).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from pydantic import BaseModel

from .ledger import TradeRecord

log = logging.getLogger("lots")

LOT_SCHEMA_VERSION = 1

#: IRS wash-sale window: a replacement buy within 30 days before or after a loss
#: sale disallows the loss. We flag, the 1099-B decides.
_WASH_SALE_DAYS = 30


class Lot(BaseModel):
    """One opening buy (or the remainder of one). Explicit id + schema stamp so
    these rows can move into a DB (GA-4.5) without a logic rewrite."""
    schema_version: int = LOT_SCHEMA_VERSION
    lot_id: str
    symbol: str
    qty: float                        # original lot size
    remaining: float                  # what a later sell hasn't consumed yet
    entry_price: float
    entry_ts: datetime
    order_id: Optional[str] = None


class RealizedLot(BaseModel):
    """A slice of a lot consumed by a sell — the realized-P&L row with the FIFO
    basis of the shares actually sold."""
    schema_version: int = LOT_SCHEMA_VERSION
    lot_id: str
    symbol: str
    qty: float
    entry_price: float
    exit_price: float
    entry_ts: datetime
    exit_ts: datetime
    pl_usd: float
    pl_pct: float
    exit_order_id: Optional[str] = None
    exit_reason: str = ""
    # True when the sell record predates exit_price (GA-2.5) and the price had to
    # be reconstructed from its recorded realized_pl_pct — legacy rows only.
    basis_estimated: bool = False
    wash_sale: bool = False


def _lot_id(rec: TradeRecord) -> str:
    return f"{rec.symbol}:{rec.order_id or rec.ts.isoformat()}"


def _exit_price_for(rec: TradeRecord, lots: list[Lot]) -> tuple[float, bool]:
    """The sell's per-share price. Prefer the recorded exit_price; a legacy row
    without one is reconstructed from its realized_pl_pct against the (qty-
    weighted) basis of the open lots — the same basis the % was measured
    against, approximately — and flagged estimated. (0.0, False) = unusable."""
    if rec.exit_price and rec.exit_price > 0:
        return float(rec.exit_price), False
    if rec.realized_pl_pct is None or not lots:
        return 0.0, False
    held = sum(l.remaining for l in lots)
    if held <= 0:
        return 0.0, False
    avg_basis = sum(l.remaining * l.entry_price for l in lots) / held
    return avg_basis * (1.0 + rec.realized_pl_pct / 100.0), True


def build_lot_history(
    records: list[TradeRecord],
) -> tuple[dict[str, list[Lot]], list[RealizedLot]]:
    """Replay corrected ledger records chronologically into (open lots by symbol,
    realized slices). Pass TradeLedger.effective() — raw all() still carries
    voided intents. Options are excluded: premium P&L isn't share-lot math."""
    open_by_symbol: dict[str, list[Lot]] = {}
    realized: list[RealizedLot] = []
    buys_by_symbol: dict[str, list[datetime]] = {}

    for rec in sorted(records, key=lambda r: r.ts):
        if rec.instrument != "equity":
            continue
        if rec.action == "buy" and rec.qty > 0 and rec.entry_price > 0:
            open_by_symbol.setdefault(rec.symbol, []).append(Lot(
                lot_id=_lot_id(rec), symbol=rec.symbol,
                qty=rec.qty, remaining=rec.qty,
                entry_price=rec.entry_price, entry_ts=rec.ts,
                order_id=rec.order_id,
            ))
            buys_by_symbol.setdefault(rec.symbol, []).append(rec.ts)
        elif rec.action == "sell":
            lots = open_by_symbol.get(rec.symbol, [])
            if not lots:
                continue  # sell with no recorded basis (pre-ledger position)
            exit_px, estimated = _exit_price_for(rec, lots)
            # qty 0/unknown = a full close (decision sells recorded before the
            # qty was known); otherwise consume exactly what was sold.
            sell_qty = rec.qty if rec.qty > 0 else sum(l.remaining for l in lots)
            realized.extend(_consume_fifo(
                lots, sell_qty, rec, exit_px, estimated,
            ))
            if not lots:
                open_by_symbol.pop(rec.symbol, None)

    _flag_wash_sales(realized, buys_by_symbol)
    return open_by_symbol, realized


def _consume_fifo(
    lots: list[Lot], qty: float, rec: TradeRecord,
    exit_px: float, estimated: bool,
) -> list[RealizedLot]:
    """Consume `qty` shares from the front of `lots` (mutating it), emitting one
    realized slice per lot touched — skipped (lots still consumed) when no usable
    exit price exists, so a data gap can't fabricate a P&L number."""
    out: list[RealizedLot] = []
    remaining = qty
    while remaining > 1e-9 and lots:
        lot = lots[0]
        take = min(lot.remaining, remaining)
        lot.remaining -= take
        remaining -= take
        if exit_px > 0:
            out.append(RealizedLot(
                lot_id=lot.lot_id, symbol=lot.symbol, qty=take,
                entry_price=lot.entry_price, exit_price=exit_px,
                entry_ts=lot.entry_ts, exit_ts=rec.ts,
                pl_usd=(exit_px - lot.entry_price) * take,
                pl_pct=(exit_px / lot.entry_price - 1.0) * 100.0
                if lot.entry_price > 0 else 0.0,
                exit_order_id=rec.order_id, exit_reason=rec.exit_reason,
                basis_estimated=estimated,
            ))
        if lot.remaining <= 1e-9:
            lots.pop(0)
    return out


def _flag_wash_sales(
    realized: list[RealizedLot], buys_by_symbol: dict[str, list[datetime]],
) -> None:
    """Flag realized LOSSES with a same-symbol buy within ±30 days of the sale.
    The opening buy of the lot being sold doesn't count as its own replacement."""
    window = timedelta(days=_WASH_SALE_DAYS)
    for r in realized:
        if r.pl_usd >= 0:
            continue
        for ts in buys_by_symbol.get(r.symbol, []):
            if ts == r.entry_ts:
                continue  # the lot's own opening buy
            if abs((r.exit_ts - ts).total_seconds()) <= window.total_seconds():
                r.wash_sale = True
                break


def fifo_basis(lots: list[Lot], qty: float) -> tuple[float, float]:
    """(qty-weighted FIFO basis price, qty actually covered) for selling `qty`
    from `lots` — WITHOUT consuming them. Used by the exchange-exit backfill to
    realize P&L against the shares that would actually be sold. covered < qty
    means the ledger doesn't hold enough recorded basis (pre-ledger shares)."""
    remaining = qty
    cost = 0.0
    covered = 0.0
    for lot in lots:
        if remaining <= 1e-9:
            break
        take = min(lot.remaining, remaining)
        cost += take * lot.entry_price
        covered += take
        remaining -= take
    if covered <= 0:
        return 0.0, 0.0
    return cost / covered, covered
