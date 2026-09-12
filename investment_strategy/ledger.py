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
import math
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from .models import RiskDecision

log = logging.getLogger("ledger")

# state/ is already gitignored — local trade history never gets committed.
DEFAULT_LEDGER_PATH = Path("state") / "trades.jsonl"

# OCC contract symbols (AMZN260918C00230000) — used by the Aug-23 measurement-
# integrity fix to key option rows by their contract instead of laundering
# premium P&L into the underlying ticker's history. Kept local instead of
# importing execution.options.occ_symbol, which would drag the alpaca SDK into
# every ledger consumer (tests, scripts, dashboard).
_OCC_RE = re.compile(r"\b([A-Z][A-Z0-9.]{0,5}\d{6}[CP]\d{8})\b")


def _occ_symbol(underlying: str, expiry: str, strike: float, right: str) -> str:
    """OCC contract symbol (AAPL, 2026-01-16, 150, call -> AAPL260116C00150000).
    Mirror of execution.options.occ_symbol — see _OCC_RE note for why."""
    d = datetime.strptime(expiry, "%Y-%m-%d")
    cp = "C" if right.lower().startswith("c") else "P"
    return f"{underlying.upper()}{d:%y%m%d}{cp}{int(round(strike * 1000)):08d}"


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
    # OCC ledgering (Aug-23 measurement integrity): option rows key `symbol`
    # by the OCC contract when the structure is a single leg, so attribution /
    # autotune see options as their own bucket instead of blending premium P&L
    # into the underlying's equity history. Multi-leg structures keep the
    # underlying as the row key (no single OCC names a spread, and the
    # watchdog ledgers group exits under the underlying). Either way the
    # underlying ticker survives here and every leg lives in occ_symbols.
    underlying: Optional[str] = None
    occ_symbols: list[str] = Field(default_factory=list)
    # Leg direction per occ_symbols entry ("buy" | "sell"), same order (run-6
    # item 1a): lets the post-mortem sign each leg when marking a spread
    # close-to-close. Empty on rows predating the field (then a multi-leg
    # group is reported 'unmarked' rather than guessed).
    occ_sides: list[str] = Field(default_factory=list)
    # Broker-confirmed fill (run-6 item 1e): entry_price is the DECISION quote;
    # these are what the broker actually reported when the order was seen
    # FILLED at reconcile (filled_avg_price, filled qty, filled_at). None until
    # confirmed / on rows predating the field. Buy rows: realized/cost
    # semantics unchanged. Equity SELL rows (run-7 B2): set_fill also restates
    # exit_price / realized_pl / realized_pl_pct at fill_price — see set_fill.
    fill_price: Optional[float] = None
    fill_qty: Optional[float] = None
    fill_ts: Optional[datetime] = None
    # Run-7 B2 (measurement-only): an equity SELL row's exit figures AS FIRST
    # RECORDED, preserved when set_fill restates the row at the broker's fill.
    # For decision/watchdog exits that is the submission-time quote (run-6:
    # 7/14 closed rows, net -$72.24 vs the fills); for exchange-backfill rows,
    # which are recorded from the fill itself, quote == fill. None on rows
    # never restated (buys, option rows, LEDGER_RESTATE_AT_FILL=off, rows
    # predating the field). The restatement is a pure function of these, so
    # a refined fill re-restates from the original instead of compounding.
    quote_exit_price: Optional[float] = None
    quote_realized_pl: Optional[float] = None
    quote_realized_pl_pct: Optional[float] = None
    # Set only by repair scripts on rows they rewrote (e.g.
    # scripts/repair_mleg_ledger_rows.py) — documents why a row's numbers were
    # changed and marks informational duplicates. Never set by live code.
    repair_note: Optional[str] = None
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
        # OCC ledgering (Aug-23): resolve every leg's OCC symbol from the
        # proposal so the row itself says WHICH contracts were bought. A
        # single-leg structure is keyed by its contract; a spread keeps the
        # underlying as the key (see the field comment on `underlying`).
        occs: list[str] = []
        sides: list[str] = []
        for leg in p.option_legs:
            try:
                occs.append(_occ_symbol(p.symbol, leg.expiry, leg.strike, leg.right))
                side = getattr(leg, "side", None)
                sides.append(str(getattr(side, "value", side) or "buy").lower())
            except (ValueError, AttributeError, TypeError):
                pass  # malformed leg spec — keep the row keyed by underlying
        if len(sides) != len(occs):
            sides = []
        symbol = occs[0] if (len(occs) == 1 and len(p.option_legs) == 1) else p.symbol
        return cls(
            symbol=symbol, action=p.action.value, instrument="option",
            qty=decision.approved_qty, entry_price=premium,
            cost_usd=decision.approved_notional,
            conviction=p.conviction,
            take_profit_pct=decision.take_profit_pct,
            stop_loss_pct=decision.stop_loss_pct,
            rationale=p.rationale, key_signals=p.key_signals,
            entry_signals=entry_signals or [],
            verdict=decision.verdict.value, risk_note=decision.reason,
            option_strategy=p.option_strategy.value if p.option_strategy else None,
            underlying=p.symbol, occ_symbols=occs, occ_sides=sides,
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
        underlying: Optional[str] = None,
        occ_symbols: Optional[list[str]] = None,
        fill_price: Optional[float] = None,
        fill_qty: Optional[float] = None,
        fill_ts: Optional[datetime] = None,
    ) -> "TradeRecord":
        """`ts` overrides the record time — the exchange-exit backfill (F.1)
        stamps the order's actual FILL time so attribution's chronological
        round-trip pairing sees the exit where it really happened, not when the
        backfill noticed it. `exit_price` is the sell's fill/quote price —
        record it whenever known so FIFO lot P&L (GA-2.5) has a real basis.
        `composite_score` is the name's weighted composite at exit — recorded so
        sell rows aren't blind to it (buys already carry it).

        `fill_price` / `fill_qty` / `fill_ts` (run-7 A6): the broker-confirmed
        fill, stamped AT CONSTRUCTION when the row is being recorded FROM a
        broker fill — the exchange-exit backfill, whose exit_price IS the
        order's filled_avg_price. Every other sell path leaves them None for
        set_fill to stamp at FILLED reconcile. Run-6 closed 14 trips and the 7
        exchange bracket exits all carried fill_price=null (the backfill priced
        realized_pl from the fill but never stamped it), so the contract's
        ledger-vs-fill reconciliation could not cover half the sample. Passed
        through verbatim (no rounding) so fill_price == exit_price on such
        rows and a later set_fill at the same fill is a numeric no-op.

        Option sells (Aug-23 OCC ledgering): callers that don't pass
        `occ_symbols` explicitly (the watchdog predates the field) get them
        parsed out of the rationale, whose fixed format lists the group's OCC
        contracts — so option exit rows always name their contracts, and
        TradeLedger.record() can re-key a single-leg exit onto the same OCC
        symbol its entry was ledgered under."""
        if instrument == "option":
            if occ_symbols is None:
                occ_symbols = _OCC_RE.findall((rationale or "").upper())
            if underlying is None:
                # An OCC symbol is <underlying> + 15 chars (yymmdd C/P strike).
                underlying = (
                    symbol[:-15] if symbol and _OCC_RE.fullmatch(symbol.strip())
                    else symbol
                )
        kwargs: dict = dict(
            symbol=symbol, action="sell", instrument=instrument, qty=qty,
            rationale=rationale, key_signals=key_signals or [], order_id=order_id,
            realized_pl_pct=realized_pl_pct, realized_pl=realized_pl,
            exit_reason=exit_reason, exit_price=exit_price,
            composite_score=composite_score,
            underlying=underlying, occ_symbols=occ_symbols or [],
        )
        if ts is not None:
            kwargs["ts"] = ts
        if fill_price is not None and fill_price > 0:
            kwargs["fill_price"] = float(fill_price)
            kwargs["fill_qty"] = float(fill_qty if fill_qty is not None else qty)
            kwargs["fill_ts"] = fill_ts
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

    # One process-wide lock: record() appends from the decision AND watchdog
    # threads, and set_fill() rewrites the file in place — the two must not
    # interleave (a torn append inside a rewrite would lose a row).
    _io_lock = threading.Lock()

    def __init__(
        self, path: Path | str = DEFAULT_LEDGER_PATH,
        restate_at_fill: Optional[bool] = None,
    ):
        self.path = Path(path)
        # Run-7 B2 knob: LEDGER_RESTATE_AT_FILL (default on; off = the run-6
        # annotation-only set_fill, see there). Resolved HERE, not as a
        # Config field: every consumer builds the ledger bare — TradeLedger()
        # in the orchestrator, post-mortem, dashboard, track record, autotune —
        # with no Config in hand, so a Config field would be dead code. None
        # reads the env key (config._flag semantics: on/true/1/yes) after
        # .env has been loaded; tests pass the bool explicitly.
        if restate_at_fill is None:
            from .config import _flag  # lazy: config imports dotenv/notify
            restate_at_fill = _flag("LEDGER_RESTATE_AT_FILL", "on")
        self.restate_at_fill = bool(restate_at_fill)

    def record(self, rec: TradeRecord) -> None:
        try:
            checked = self._validate_sell(rec)
            if checked is None:
                return  # rejected — the loud log already fired
            rec = checked
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._io_lock, self.path.open("a", encoding="utf-8") as fh:
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

    def set_fill(
        self, order_id: str, fill_price: float, fill_qty: float,
        fill_ts: Optional[datetime] = None, restate: Optional[bool] = None,
    ) -> bool:
        """Stamp the broker-confirmed fill (run-6 item 1e) onto the buy/sell
        row(s) carrying `order_id` and — run-7 B2 — restate an equity SELL
        row's exit_price / realized_pl / realized_pl_pct at that fill.

        Why the run-6 version stamped fill_* and touched NOTHING else: the
        ledger is append-only by design, `qty` is what effective()'s
        partial-fill corrections scale against, and realized_pl was the
        contract's counted number mid-window. Run-6 then showed the cost of
        that purity: 7/14 closed rows carried the submission-time quote (net
        -$72.24 vs the fills; the PSQ hedge_unwind read +$41.76 at the quote
        and -$61.07 filled), so "ledger vs broker realized" was unmeasurable.
        The restatement keeps every one of the original reasons intact:
          - `qty`, cost_usd and BUY rows are never touched (entry_price stays
            the decision quote; corrections still scale on the row qty);
          - the as-recorded figures survive as quote_exit_price /
            quote_realized_pl / quote_realized_pl_pct, and the restatement is
            a pure function of them (a refined fill re-restates from the
            original, never compounds);
          - option rows keep the annotation-only convention — a multi-leg
            filled_avg_price is a net debit/credit per spread that does not
            map onto the group's realized_pl, so we don't guess;
          - LEDGER_RESTATE_AT_FILL=off (or restate=False here) restores the
            run-6 behaviour exactly. `restate=None` uses the ledger's knob.
        Rewrites atomically (tmp + replace) under the append lock. Returns
        True when at least one row was stamped; False (never raises) when
        the order id is unknown or the file can't be rewritten."""
        if not order_id or not (fill_price and fill_price > 0):
            return False
        do_restate = self.restate_at_fill if restate is None else bool(restate)
        try:
            fill_px = round(float(fill_price), 4)
            restated: list[tuple] = []
            skipped: list[tuple] = []
            with self._io_lock:
                if not self.path.exists():
                    return False
                lines = self.path.read_text(encoding="utf-8").splitlines()
                hit = False
                out: list[str] = []
                for line in lines:
                    raw = line.strip()
                    if not raw:
                        continue
                    try:
                        obj = json.loads(raw)
                    except json.JSONDecodeError:
                        out.append(line)
                        continue
                    if (obj.get("order_id") == order_id
                            and obj.get("action") in ("buy", "sell")):
                        obj["fill_price"] = fill_px
                        obj["fill_qty"] = float(fill_qty)
                        # No broker filled_at -> None, never "now": the
                        # field means the FILL time or nothing (review fix).
                        obj["fill_ts"] = (
                            fill_ts.isoformat() if fill_ts is not None else None
                        )
                        if obj.get("action") == "sell":
                            if do_restate:
                                upd, why = self._restate_sell_at_fill(obj, fill_px)
                            else:
                                upd, why = None, "LEDGER_RESTATE_AT_FILL=off"
                            if upd is not None:
                                restated.append((
                                    obj.get("symbol"), obj.get("realized_pl"),
                                    upd["realized_pl"], upd["quote_exit_price"],
                                ))
                                obj.update(upd)
                            else:
                                skipped.append((obj.get("symbol"), why))
                        out.append(json.dumps(obj))
                        hit = True
                    else:
                        out.append(line)
                if not hit:
                    return False
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text("".join(l + "\n" for l in out), encoding="utf-8")
                tmp.replace(self.path)
            for sym, old_pl, new_pl, quote_px in restated:
                if abs(new_pl - old_pl) < 0.005:
                    # Exchange-backfill rows are recorded FROM the fill, so
                    # their quote == fill: provenance stamped, numbers as-is.
                    log.info(
                        "Ledger: fill %.4f matches recorded exit_price on SELL "
                        "%s: realized $%.2f unchanged [order %s]",
                        fill_px, sym, new_pl, order_id,
                    )
                    continue
                log.info(
                    "Ledger: RESTATED SELL %s at fill %.4f (quote %.4f): "
                    "realized $%.2f -> $%.2f (%+.2f) [order %s]",
                    sym, fill_px, quote_px, old_pl, new_pl, new_pl - old_pl,
                    order_id,
                )
            for sym, why in skipped:
                log.info(
                    "Ledger: fill %.4f stamped on SELL %s WITHOUT restatement "
                    "(%s) [order %s]", fill_px, sym, why, order_id,
                )
            return True
        except Exception as e:  # never let bookkeeping break the trade loop
            log.warning("Ledger set_fill failed for order %s: %s", order_id, e)
            return False

    @staticmethod
    def _restate_sell_at_fill(
        obj: dict, fill_price: float,
    ) -> tuple[Optional[dict], str]:
        """Field updates that move an equity SELL row's exit_price /
        realized_pl / realized_pl_pct from the as-recorded quote to
        `fill_price`, or (None, reason) when the row must stay as recorded:
        option rows (annotation-only convention), no exit_price, no qty, no
        realized $, or a basis that isn't positive (the row's realized_pl was
        not computed over the row's qty — restating would fabricate a %).

        Math, on the ROW qty — never fill_qty: effective()'s partial-fill
        correction scales realized_pl by filled/row qty, so restating on the
        fill qty would double-scale a partial.
            basis         = quote_exit_price - quote_realized_pl / qty
            realized_pl   = quote_realized_pl + (fill - quote_exit_price) * qty
            realized_pct  = (fill - basis) / basis * 100
        The $ figure is the basis source (Alpaca's unrealized_pl is exact to
        the cent) rather than the pct (Alpaca rounds unrealized_plpc to ~5
        significant digits — $0.45 off on the $12.5k BE trip)."""
        if (obj.get("instrument") or "equity") != "equity":
            return None, "option row: annotation-only convention"
        # Origin = the figures as first recorded (a second set_fill for a
        # refined fill must not compound the first restatement).
        if obj.get("quote_exit_price") is not None:
            px, pl, pct = (obj.get("quote_exit_price"),
                           obj.get("quote_realized_pl"),
                           obj.get("quote_realized_pl_pct"))
        else:
            px, pl, pct = (obj.get("exit_price"), obj.get("realized_pl"),
                           obj.get("realized_pl_pct"))
        try:
            qty = float(obj.get("qty") or 0.0)
            px = float(px) if px is not None else 0.0
            pl = float(pl) if pl is not None else None
        except (TypeError, ValueError):
            return None, "non-numeric row fields"
        if not (px > 0 and math.isfinite(px)):
            return None, "no exit_price on row"
        if not (qty > 0 and math.isfinite(qty)):
            return None, "row qty is 0"
        if pl is None or not math.isfinite(pl):
            return None, "no realized_pl on row"
        basis = px - pl / qty
        if not (basis > 0 and math.isfinite(basis)):
            return None, "inconsistent basis (realized_pl not over row qty)"
        new_pl = pl + (fill_price - px) * qty
        new_pct = (fill_price - basis) / basis * 100.0
        return {
            "exit_price": fill_price,
            "realized_pl": round(new_pl, 4),
            "realized_pl_pct": round(new_pct, 6),
            "quote_exit_price": px,
            "quote_realized_pl": pl,
            "quote_realized_pl_pct": pct,
        }, "restated"

    def _validate_sell(self, rec: TradeRecord) -> Optional[TradeRecord]:
        """Append-path integrity gate (Aug-23). The Aug-17 MLEG unwind wrote
        SELL rows with symbol="None" (str() of a broker MLEG parent's null
        symbol) and realized_pl=null — corrupt records that silently skewed
        every realized-P&L read. Rules, applied to SELL rows only:

        - symbol None/'None'/'' -> REJECT (return None), loud ERROR log.
        - realized_pl null but derivable (realized_pl_pct + exit_price + qty
          all present) -> derive it against the pct's own basis, x100 for
          option contracts. Regime trims / core-defense sells deliberately
          send realized_pl=None with pct set — deriving keeps those rows AND
          makes the dollar ledger complete.
        - realized_pl AND realized_pl_pct both null -> REJECT: a SELL with no
          outcome at all poisons sum(realized_pl) and attribution.
        - option sell whose rationale/occ_symbols name exactly ONE contract,
          when that contract's BUY was ledgered under its OCC symbol -> re-key
          the row to the OCC so the round-trip pairs (entry rows moved to OCC
          keys on Aug-23; older entries under the underlying keep pairing
          because the re-key only fires when an OCC-keyed BUY exists).

        Returns the (possibly updated) record, or None when rejected."""
        if rec.action != "sell":
            return rec
        sym = (rec.symbol or "").strip()
        if not sym or sym == "None":
            log.error(
                "LEDGER REJECT: SELL row with corrupt symbol %r "
                "(order %s, exit_reason %s) — record refused; fix the caller.",
                rec.symbol, rec.order_id, rec.exit_reason,
            )
            return None
        if (rec.instrument or "equity") == "option":
            occs = rec.occ_symbols or _OCC_RE.findall((rec.rationale or "").upper())
            if occs and len(occs) == 1 and occs[0] != sym:
                try:
                    entry_keys = {
                        r.symbol for r in self.all() if r.action == "buy"
                    }
                except Exception:
                    entry_keys = set()
                if occs[0] in entry_keys:
                    log.info(
                        "Ledger: option SELL %s re-keyed to OCC %s "
                        "(entry is OCC-ledgered).", sym, occs[0],
                    )
                    rec = rec.model_copy(update={
                        "symbol": occs[0],
                        "underlying": rec.underlying or sym,
                        "occ_symbols": occs,
                    })
        if rec.realized_pl is None:
            pct, px, qty = rec.realized_pl_pct, rec.exit_price, rec.qty
            # (1 + pct/100) must be positive: at pct <= -100 the implied basis
            # is zero/negative (junk-quote territory) — don't fabricate a $.
            if (pct is not None and px and px > 0 and qty and qty > 0
                    and (1.0 + pct / 100.0) > 1e-9):
                mult = 100.0 if (rec.instrument or "equity") == "option" else 1.0
                basis = px / (1.0 + pct / 100.0)
                derived = round((px - basis) * qty * mult, 2)
                log.info(
                    "Ledger: derived realized_pl $%.2f for SELL %s from "
                    "pct/price/qty (caller sent None).", derived, rec.symbol,
                )
                rec = rec.model_copy(update={"realized_pl": derived})
        if rec.realized_pl is None and rec.realized_pl_pct is None:
            log.error(
                "LEDGER REJECT: SELL %s (order %s, exit_reason %s) carries no "
                "realized outcome (realized_pl AND realized_pl_pct null) — "
                "record refused to protect measurement integrity.",
                rec.symbol, rec.order_id, rec.exit_reason,
            )
            return None
        return rec

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
                    # The as-recorded $ (run-7 B2) is over the row qty too, so
                    # realized_pl - quote_realized_pl stays the fill slippage.
                    "quote_realized_pl": (
                        r.quote_realized_pl * frac
                        if r.quote_realized_pl is not None else None
                    ),
                    "risk_note": (r.risk_note + " " if r.risk_note else "")
                    + f"[corrected: {c.qty:g}/{r.qty:g} filled]",
                })
            out.append(r)
        return out
