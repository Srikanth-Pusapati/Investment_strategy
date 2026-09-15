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
from datetime import datetime, timedelta, timezone
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


# --------------------------------------------------------------------------- #
# Run-7 4a-15 / 4a-16: decision-time SHADOW fields (measurement-only)
# --------------------------------------------------------------------------- #
# Two claims the run-7 review REFUTED / marked do-not-do still deserve an
# ex-ante measurement so a later window can re-open them on evidence instead
# of memory:
#   4a-15  "fresh entries on SPY-red tape lose" — refuted on the pooled ledger
#          (red-day entries +$88 avg vs green +$187; date-clustered diff CI
#          [-249, +1,907]; the prior-day-down bucket is the BEST bucket). The
#          run-6 rows carried no intraday-SPY / regime / name-falling field,
#          so the ex-ante version of the test could not be run at all. These
#          stamps let it be — re-evaluate only at >= 15 independent down dates.
#   4a-16  "the 4% clamp floor stops quiet names out on noise" — do-not-do
#          (4 vs 4 events, Fisher p=0.18). The row stamps what a 6% floor WOULD
#          have set, and every stop exit stamps whether the trip's worst
#          close-to-close drawdown ever reached it. Paired test at >= 20
#          tight-stop exits; widening only, never tightening.
# NOTHING here changes what is bought, sold, sized or stopped. The rule
# parameters below are the REFUTED rule's own numbers, kept as constants (not
# .env knobs) precisely so nobody tunes a counterfactual into a guard.
SHADOW_STOP_FLOOR_PCT = 6.0        # the clamp floor under test (live: 4.0)
SHADOW_HAIRCUT_SPY_PCT = -0.3      # SPY intraday <= this ...
SHADOW_HAIRCUT_FALLING_MIN = 2     # ... AND (breadth narrow OR >= N NAME FALLING)
SHADOW_HAIRCUT_MULT = 0.5          # ... would have cut the approved notional by this


def shadow_stop_pct(
    daily_sigma_pct: float, mult: float, floor_pct: float, max_pct: float,
    ext_pct: Optional[float] = None,
) -> float:
    """The vol-stop a name would get under a different clamp floor — the
    EXACT arithmetic of RiskManager._exit_levels (stop = min(max(mult x
    sigma_d, floor), max), then the STOP_COVER_EXTENSION widening to the
    20d-SMA extension, still capped) with only the floor swapped. Mirrored
    here rather than parameterizing the risk layer so the shadow can never
    leak into a live stop; tests/test_ledger.py pins the parity."""
    stop = min(max(mult * daily_sigma_pct, floor_pct), max_pct)
    if ext_pct is not None and ext_pct > stop:
        stop = min(float(ext_pct), max_pct)
    return stop


def would_haircut_usd(
    approved_notional: float, spy_intraday_pct: Optional[float],
    breadth_narrow: bool, falling_count: int,
) -> Optional[float]:
    """Dollars the REFUTED red-tape haircut (4a-15) would have taken off this
    buy: SHADOW_HAIRCUT_MULT x notional when SPY is <= SHADOW_HAIRCUT_SPY_PCT
    intraday AND (breadth is narrow OR >= SHADOW_HAIRCUT_FALLING_MIN held
    names carry a NAME FALLING read); 0.0 when the rule would not fire; None
    when the SPY read is unknown (a degraded feed is not a green tape)."""
    if spy_intraday_pct is None:
        return None
    red = spy_intraday_pct <= SHADOW_HAIRCUT_SPY_PCT
    weak = breadth_narrow or falling_count >= SHADOW_HAIRCUT_FALLING_MIN
    if red and weak and approved_notional > 0:
        return round(SHADOW_HAIRCUT_MULT * approved_notional, 2)
    return 0.0


def floor_would_survive(
    entry_price: float, closes: list[float],
    floor_pct: float = SHADOW_STOP_FLOOR_PCT,
) -> tuple[Optional[bool], Optional[float]]:
    """(would_survive, worst_close_pct) for a trip entered at `entry_price`
    whose daily CLOSES from entry to exit are `closes`: True when no close
    sat >= floor_pct under the entry, i.e. a stop resting at the floor would
    never have been reached on a close. A CLOSE-BASED PROXY, and a lower
    bound on touches: bracket stops trigger intraday and daily_close_series
    is close-only (verifier BT-3), so "survives" here means "survived on
    closes". (None, None) when there is nothing to measure."""
    usable = [c for c in closes if c and c > 0]
    if not usable or not entry_price or entry_price <= 0:
        return None, None
    # Rounded before the comparison: 94/100 - 1 is -0.06000000000000001 in
    # binary and must read as exactly the floor. AT the floor = touched (a
    # resting stop fills at or through its level), so only a strictly
    # shallower worst close survives.
    worst = round(min((c / entry_price - 1.0) * 100.0 for c in usable), 4)
    return worst > -floor_pct, round(worst, 2)


def floor_survival_at_exit(
    close_series, symbol: str, entry_ts: datetime, entry_price: float,
    exit_ts: Optional[datetime] = None,
    floor_pct: float = SHADOW_STOP_FLOOR_PCT,
) -> tuple[Optional[bool], Optional[float]]:
    """floor_would_survive over the trip's session closes, fetched through
    `close_series(symbol, days)` (AlpacaClient.daily_close_series: (ISO date,
    close) pairs, last `days` bars). Window = the entry's UTC date through the
    exit's (today when None; the live partial bar then stands in for the exit
    day's close). Never raises — an unreadable series is (None, None), the
    stamp the paired test excludes, not a false "survived"."""
    try:
        if close_series is None or not entry_price or entry_price <= 0:
            return None, None
        end = exit_ts or _now()
        span_days = max(0, (end - entry_ts).days)
        # daily_close_series returns the LAST `days` bars from a 2x-days
        # calendar start; +5 covers weekends/holidays on both ends.
        series = close_series(symbol, span_days + 5) or []
        lo, hi = entry_ts.date().isoformat(), end.date().isoformat()
        closes = [float(c) for d, c in series if lo <= str(d)[:10] <= hi]
        return floor_would_survive(entry_price, closes, floor_pct)
    except Exception as e:  # noqa: BLE001 — a shadow must never break an exit
        log.debug("floor survival shadow for %s unavailable: %s", symbol, e)
        return None, None


def floor_shadow_line(
    symbol: str, exit_reason: str, survive: bool, worst_close_pct: float,
    basis: float, live_stop_pct: Optional[float],
) -> str:
    """The greppable stop-exit counterpart of ENTRY TAPE (one format for the
    bracket backfill and the watchdog's fractional stop)."""
    live = f"{live_stop_pct:.2f}%" if live_stop_pct else "n/a"
    return (
        f"FLOOR6 SHADOW: {symbol} {exit_reason} worst_close={worst_close_pct:+.2f}% "
        f"vs basis {basis:.2f} (live stop {live}, floor under test "
        f"{SHADOW_STOP_FLOOR_PCT:.0f}%) -> would_survive={survive} "
        "(close-based proxy)"
    )


class EntryTape(BaseModel):
    """What the tape looked like at the moment a BUY was approved (4a-15/16)
    — built by the orchestrator from reads it already holds that cycle and
    stamped onto the buy row verbatim. Every field is optional: a degraded
    regime read or vol-stops off leaves the stamp None, never a guessed 0."""
    spy_intraday_ret_at_decision: Optional[float] = None   # Regime.day_change_pct
    regime_label: Optional[str] = None                     # applied label
    breadth_narrow: bool = False                           # QQQ+IWM < 50dma read
    falling_names: list[str] = Field(default_factory=list) # NAME FALLING map keys
    would_haircut_usd: Optional[float] = None
    stop_pct_if_floor_6: Optional[float] = None
    # The UNCLAMPED vol stop (mult x sigma_d) — with it any floor (5/6/7%) can
    # be replayed ex post, not only the 6% column (verifier BT-3 correction).
    vol_stop_raw_pct: Optional[float] = None

    def log_line(self, symbol: str, stop_pct: float) -> str:
        """The greppable one-liner the orchestrator logs per buy."""
        spy = (
            f"{self.spy_intraday_ret_at_decision:+.2f}%"
            if self.spy_intraday_ret_at_decision is not None else "n/a"
        )
        cut = (
            f"${self.would_haircut_usd:,.0f}"
            if self.would_haircut_usd is not None else "n/a"
        )
        s6 = (
            f"{self.stop_pct_if_floor_6:.2f}%"
            if self.stop_pct_if_floor_6 is not None else "n/a"
        )
        return (
            f"ENTRY TAPE: {symbol} spy_intraday={spy} "
            f"regime={self.regime_label or 'n/a'} "
            f"falling={len(self.falling_names)} would_haircut={cut} "
            f"stop={stop_pct:.2f}% stop_if_floor6={s6}"
        )


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

    # -- Run-7 4a-15 / 4a-16 shadow fields (measurement-only; see the module
    # header above TradeRecord). BUY rows: the tape at decision time — the
    # values the orchestrator already held that cycle (Regime.day_change_pct,
    # the applied regime label, the NAME FALLING map) plus two counterfactuals
    # (the refuted red-tape haircut in $, the stop a 6% clamp floor would have
    # set). STOP exits (bracket_stop / watchdog stop): whether the trip's worst
    # close-to-close drawdown ever reached the 6% floor. None / [] on every row
    # predating the fields and on rows where the read was unavailable — a
    # missing read is excluded from the paired test, never counted as 0.
    spy_intraday_ret_at_decision: Optional[float] = None
    regime_label: Optional[str] = None
    falling_names: list[str] = Field(default_factory=list)
    would_haircut_usd: Optional[float] = None
    stop_pct_if_floor_6: Optional[float] = None
    vol_stop_raw_pct: Optional[float] = None
    floor6_would_survive: Optional[bool] = None
    floor6_worst_close_pct: Optional[float] = None

    # -- Run-7 4a-18 (measurement-only): SELL rows carry their entry lot's
    # attributes AT CLOSE, so a closed row is self-describing and the pooled
    # analysis never has to re-join sells to buys offline (197 sells, 39 of
    # them pre-run-5 phantoms/dupes that never joined). Stamped by
    # TradeLedger.record() from the FIFO lot(s) the sell consumes (lots.py):
    # a multi-lot exit takes the OLDEST lot's attributes (the buy that opened
    # the episode) and counts the lots in `lots_n`. None / [] on BUY rows,
    # on rows predating the fields, and on sells with no ledger lot
    # (pre-ledger shares; lots_n=0 then). `entry_fill_source` says whether
    # entry_fill_price is the broker fill ("fill", set_fill stamped the buy)
    # or the decision quote ("quote", LEDGER_FILL_PRICES off / legacy buy).
    entry_ts: Optional[datetime] = None
    entry_fill_price: Optional[float] = None
    entry_fill_source: Optional[str] = None
    entry_composite: Optional[float] = None
    entry_conviction: Optional[float] = None
    entry_stop_pct: Optional[float] = None
    entry_key_signals: list[str] = Field(default_factory=list)
    lots_n: Optional[int] = None
    # Option BUY rows (critic #13): cost_usd AS FIRST RECORDED (the proposal's
    # estimated debit) when set_fill restated cost_usd at the broker's fill —
    # the HD put of 2026-09-02 was ledgered at $3,213 (32.13 x 100) and filled
    # at 34.30 = $3,430; every premium-cap / option-P&L read off the ledger
    # was $217 light. Same knob as the equity SELL restatement
    # (LEDGER_RESTATE_AT_FILL); None when never restated.
    quote_cost_usd: Optional[float] = None

    # -- builders ---------------------------------------------------------- #
    @classmethod
    def from_equity(
        cls, decision: RiskDecision, entry_price: float,
        order_id: Optional[str], entry_signals: Optional[list[str]] = None,
        submitted_qty: Optional[float] = None,
        submitted_cost: Optional[float] = None,
        composite_score: Optional[float] = None,
        tape: Optional[EntryTape] = None,
    ) -> "TradeRecord":
        """`submitted_qty`/`submitted_cost` are what actually went to the broker
        when it differs from the decision (whole-share flooring drops the
        sub-share remainder) — the ledger must reflect the order, not the
        intent, or the divergence is invisible to fill reconciliation.
        `tape` (run-7 4a-15/16) is the decision-time shadow context; None
        leaves every shadow field at its legacy default."""
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
            **(
                {
                    "spy_intraday_ret_at_decision": tape.spy_intraday_ret_at_decision,
                    "regime_label": tape.regime_label,
                    "falling_names": list(tape.falling_names),
                    "would_haircut_usd": tape.would_haircut_usd,
                    "stop_pct_if_floor_6": tape.stop_pct_if_floor_6,
                    "vol_stop_raw_pct": tape.vol_stop_raw_pct,
                } if tape is not None else {}
            ),
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
        floor6_would_survive: Optional[bool] = None,
        floor6_worst_close_pct: Optional[float] = None,
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
        # Run-7 4a-16: the clamp-floor shadow, stamped by STOP exits only
        # (floor_survival_at_exit); every other exit leaves the legacy None.
        if floor6_would_survive is not None:
            kwargs["floor6_would_survive"] = bool(floor6_would_survive)
            kwargs["floor6_worst_close_pct"] = floor6_worst_close_pct
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


# --------------------------------------------------------------------------- #
# Run-7 4a-18: phantom / duplicate SELL rows the loader drops
# --------------------------------------------------------------------------- #
# The pooled ledger (197 sells through run-6) carried 39 rows that never
# FIFO-joined to a buy; two shapes are mechanical artefacts of the order
# lifecycle, not trades, and every consumer that sums realized_pl counted
# them (the eval checker, attribution, the track-record prompt block):
#   negative_qty   AVAV 2026-07-07 14:54 flatten qty=-37 +$365.04 — a flatten
#                  ledgered off a snapshot that read the position SHORT after
#                  the Jul-7 bracket double-fill (the pending_cancel wedge).
#   replaced_dupe  the same exit ledgered twice: a watchdog exit re-replaced at
#                  a new mark seconds later (AVAV 2026-07-08 13:23:02 trail 37
#                  sh +$212.01, then 13:23:38 37 sh +$213.98 — the $1.97 delta
#                  is exactly 37 x the $0.0532 re-mark), or a DAY close that
#                  expired at the bell and was resubmitted (T 2026-07-23
#                  16:50 / 20:00 option flatten, -$2,700 twice). Since Aug the
#                  watchdog's _supersede_exit_record voids the replaced id
#                  with a correction row, so effective() already handles new
#                  ledgers; the archives predate it.
# Rule (a loader concern — the file is never rewritten): after corrections,
# a SELL row is dropped when its qty is NEGATIVE, or when a LATER sell on the
# same (symbol, instrument, exit_reason) with the same qty lands within
# PHANTOM_DUPE_WINDOW_H and either carries the same realized_pl to the cent
# or a realized_pl whose delta is exactly qty x the exit_price delta (same
# shares, same basis, re-marked). The EARLIER row is the phantom (the
# replaced / expired submission); the later row carries the exit. A row the
# broker confirmed FILLED (fill_price stamped) is never a phantom. qty == 0
# is KEPT: the pre-qty legacy shape means "full close, size unknown" (lots.py
# consumes every open lot for it) — a deliberate refinement of the contract's
# "qty <= 0" wording; the pooled history holds no qty-0 sell with a P&L.
PHANTOM_DUPE_WINDOW_H = 4.0
_PHANTOM_PL_TOL = 0.005       # realized_pl equal to the cent
_PHANTOM_QTY_TOL = 1e-6
_PHANTOM_LOG_ROWS = 10        # LEDGER PHANTOMS lists this many; the LLY Jul-9
                              # storm alone is 24 rows (phantoms() has them all)


class DroppedSell(BaseModel):
    """One SELL row effective() dropped, with the rule and (for a dupe) the
    order id of the row that carries the exit — the checker's --show-dropped
    line and the LEDGER PHANTOMS log print these."""
    record: TradeRecord
    rule: str                              # "negative_qty" | "replaced_dupe"
    kept_order_id: Optional[str] = None

    def line(self) -> str:
        r = self.record
        pl = f"${r.realized_pl:+.2f}" if r.realized_pl is not None else "$n/a"
        kept = f" (exit carried by order {self.kept_order_id})" if self.kept_order_id else ""
        return (
            f"{r.symbol} {r.instrument or 'equity'} {r.exit_reason or '-'} "
            f"{str(r.ts)[:16]} qty={r.qty:g} {pl} [{self.rule}]{kept}"
        )


def _is_remark_dupe(earlier: TradeRecord, later: TradeRecord) -> bool:
    """Same shares closed twice? True when the two rows' realized $ agree to
    the cent, or differ by exactly qty x (exit_price delta) — the signature
    of one position re-marked between two submissions (same basis)."""
    if earlier.realized_pl is None or later.realized_pl is None:
        return False
    d_pl = float(later.realized_pl) - float(earlier.realized_pl)
    if abs(d_pl) < _PHANTOM_PL_TOL:
        return True
    if (earlier.exit_price or 0) > 0 and (later.exit_price or 0) > 0:
        d_px = float(later.exit_price) - float(earlier.exit_price)
        return abs(d_pl - float(later.qty) * d_px) < 0.01
    return False


def dedup_sells(
    records: list[TradeRecord],
) -> tuple[list[TradeRecord], list[DroppedSell]]:
    """Apply the phantom rule above to CORRECTED records (pass effective()'s
    stream). Returns (kept in original order, dropped). Pure — never touches
    the file."""
    if not records:
        return [], []
    order = sorted(range(len(records)), key=lambda i: records[i].ts)
    dropped: dict[int, DroppedSell] = {}
    window = timedelta(hours=PHANTOM_DUPE_WINDOW_H)
    # Per (symbol, instrument, exit_reason): index of the last kept sell.
    last_kept: dict[tuple[str, str, str], int] = {}
    for i in order:
        r = records[i]
        if r.action != "sell":
            continue
        if r.qty < 0:
            dropped[i] = DroppedSell(record=r, rule="negative_qty")
            continue
        key = (r.symbol, r.instrument or "equity", r.exit_reason or "")
        j = last_kept.get(key)
        if j is not None:
            prev = records[j]
            if (
                (prev.fill_price or 0) <= 0            # a confirmed fill is real
                and prev.qty > 0
                and abs(prev.qty - r.qty) < _PHANTOM_QTY_TOL
                and timedelta(0) <= (r.ts - prev.ts) <= window
                and _is_remark_dupe(prev, r)
            ):
                dropped[j] = DroppedSell(
                    record=prev, rule="replaced_dupe", kept_order_id=r.order_id,
                )
        last_kept[key] = i
    kept = [r for i, r in enumerate(records) if i not in dropped]
    return kept, [dropped[i] for i in sorted(dropped)]


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
        # Run-7 4a-18: what the last effective() dropped, and the signature
        # already logged (LEDGER PHANTOMS fires once per distinct set).
        self.last_dropped: list[DroppedSell] = []
        self._phantoms_logged: Optional[tuple] = None

    def record(self, rec: TradeRecord) -> None:
        try:
            checked = self._validate_sell(rec)
            if checked is None:
                return  # rejected — the loud log already fired
            rec = checked
            if rec.action == "sell":
                rec = self._stamp_entry_lots(rec)   # 4a-18; never raises
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

    def _stamp_entry_lots(self, rec: TradeRecord) -> TradeRecord:
        """Run-7 4a-18: copy the entry lot's attributes onto a SELL row at
        close (entry_ts / entry_fill_price / entry_composite /
        entry_conviction / entry_stop_pct / entry_key_signals / lots_n).
        ONE implementation for every sell path — decision sells, watchdog
        exits, trims, hedge unwinds and the exchange backfill all land here
        — so no call site can forget it. Equity: the FIFO lots the sell
        consumes (lots.py, rebuilt from the corrected stream on disk, which
        already holds the earlier rows of a multi-exit batch); a multi-lot
        exit takes the OLDEST lot (the buy that opened the episode) and
        counts the lots. Options: premium P&L is not share-lot math, so the
        stamp takes the oldest still-open option BUY on the same key (same
        symbol, or an OCC leg in common) with the same episode rule
        attribution uses (a sell covering the open contracts clears the
        key). No lot at all -> lots_n=0 and every field None, logged, never
        a guessed value. Best-effort: any failure returns the row unstamped
        (a stamp must never block an exit record)."""
        try:
            from .lots import build_lot_history, fifo_lots  # lazy: lots imports us
            records = self.effective()
            entries: list = []
            if (rec.instrument or "equity") == "option":
                entries = self._open_option_entries(records, rec)
                oldest = entries[0] if entries else None
                lot_fill = (
                    float(oldest.fill_price)
                    if oldest is not None and (oldest.fill_price or 0) > 0 else None
                )
                lot_quote = float(oldest.entry_price) if oldest is not None else None
                opened_at = oldest.ts if oldest is not None else None
            else:
                open_lots, _ = build_lot_history(records)
                entries = fifo_lots(open_lots.get(rec.symbol) or [], rec.qty)
                oldest = entries[0] if entries else None
                lot_fill = oldest.fill_price if oldest is not None else None
                lot_quote = oldest.entry_price if oldest is not None else None
                opened_at = oldest.entry_ts if oldest is not None else None
            if oldest is None:
                log.info(
                    "LOT STAMP: %s %s has no ledger lot (pre-ledger shares or "
                    "phantom) — sell row carries no entry attributes (lots_n=0)",
                    rec.symbol, rec.exit_reason or "sell",
                )
                return rec.model_copy(update={"lots_n": 0})
            source = "fill" if lot_fill is not None else "quote"
            entry_px = lot_fill if lot_fill is not None else lot_quote
            conv = getattr(oldest, "conviction", None)
            comp = getattr(oldest, "composite_score", None)
            stop = getattr(oldest, "stop_loss_pct", None)
            if (rec.instrument or "equity") == "option":
                # TradeRecord fields: 0.0 = not recorded (see lots.py).
                conv = conv if (conv or 0.0) > 0 else None
                stop = stop if (stop or 0.0) > 0 else None
            sigs = list(getattr(oldest, "key_signals", None) or [])
            stamped = rec.model_copy(update={
                "entry_ts": opened_at,
                "entry_fill_price": entry_px,
                "entry_fill_source": source,
                "entry_composite": comp,
                "entry_conviction": conv,
                "entry_stop_pct": stop,
                "entry_key_signals": sigs,
                "lots_n": len(entries),
            })
            log.info(
                "LOT STAMP: %s %s entry_ts=%s entry_fill=%.4f (%s) conviction=%s "
                "composite=%s stop=%s lots_n=%d key_signals=%s",
                rec.symbol, rec.exit_reason or "sell",
                str(stamped.entry_ts)[:16], entry_px, source,
                f"{conv:.2f}" if conv is not None else "n/a",
                f"{comp:+.2f}" if comp is not None else "n/a",
                f"{stop:.2f}%" if stop is not None else "n/a",
                len(entries), sigs,
            )
            return stamped
        except Exception as e:  # noqa: BLE001 — a stamp never blocks an exit
            log.warning("LOT STAMP failed for %s (row recorded unstamped): %s",
                        rec.symbol, e)
            return rec

    @staticmethod
    def _open_option_entries(
        records: list[TradeRecord], sell: TradeRecord,
    ) -> list[TradeRecord]:
        """Option BUY rows still open on the sell's key, oldest first. Key =
        same symbol (OCC for a single leg, underlying for a spread) or any
        OCC leg in common (a group close ledgered under the underlying that
        closes two OCC-keyed single legs). Episode rule mirrors attribution.
        round_trips: a sell whose qty covers >= 99% of the open contracts —
        or carries no qty — clears the key."""
        legs = set(sell.occ_symbols or [])

        def _matches(r: TradeRecord) -> bool:
            if (r.instrument or "equity") != "option":
                return False
            if r.symbol == sell.symbol:
                return True
            return bool(legs and legs.intersection(r.occ_symbols or []))

        open_rows: list[TradeRecord] = []
        for r in sorted(records, key=lambda x: x.ts):
            if not _matches(r):
                continue
            if r.action == "buy" and r.qty > 0:
                open_rows.append(r)
            elif r.action == "sell":
                total = sum(x.qty for x in open_rows)
                if r.qty <= 0 or r.qty >= total * 0.99:
                    open_rows = []
                else:
                    remaining = r.qty
                    while remaining > 1e-9 and open_rows:
                        take = min(open_rows[0].qty, remaining)
                        remaining -= take
                        if take >= open_rows[0].qty - 1e-9:
                            open_rows.pop(0)
                        else:
                            open_rows[0] = open_rows[0].model_copy(
                                update={"qty": open_rows[0].qty - take})
        return open_rows

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
            opt_restated: list[tuple] = []
            opt_skipped: list[tuple] = []
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
                        elif (obj.get("instrument") or "equity") == "option":
                            # Run-7 4a-18 / critic #13: an option BUY's
                            # cost_usd is the proposal's ESTIMATED debit;
                            # restate it at the fill under the same knob.
                            if do_restate:
                                upd, why = self._restate_option_buy_at_fill(obj, fill_px)
                            else:
                                upd, why = None, "LEDGER_RESTATE_AT_FILL=off"
                            if upd is not None:
                                opt_restated.append((
                                    obj.get("symbol"), float(obj.get("cost_usd") or 0.0),
                                    upd["cost_usd"], float(obj.get("entry_price") or 0.0),
                                ))
                                obj.update(upd)
                            else:
                                opt_skipped.append((obj.get("symbol"), why))
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
            for sym, old_cost, new_cost, quote_px in opt_restated:
                if abs(new_cost - old_cost) < 0.005:
                    log.info(
                        "Ledger: fill %.4f matches recorded premium on OPTION BUY "
                        "%s: cost $%.2f unchanged [order %s]",
                        fill_px, sym, new_cost, order_id,
                    )
                    continue
                log.info(
                    "Ledger: RESTATED OPTION BUY %s cost at fill %.4f (quote "
                    "%.4f): $%.2f -> $%.2f (%+.2f) [order %s]",
                    sym, fill_px, quote_px, old_cost, new_cost,
                    new_cost - old_cost, order_id,
                )
            for sym, why in opt_skipped:
                log.info(
                    "Ledger: fill %.4f stamped on OPTION BUY %s WITHOUT cost "
                    "restatement (%s) [order %s]", fill_px, sym, why, order_id,
                )
            return True
        except Exception as e:  # never let bookkeeping break the trade loop
            log.warning("Ledger set_fill failed for order %s: %s", order_id, e)
            return False

    def set_floor_shadow(
        self, order_id: str, survive: Optional[bool], worst_close_pct: Optional[float],
    ) -> bool:
        """Stamp the 4a-16 clamp-floor shadow onto the SELL row carrying
        `order_id` AFTER the fact. WHY a second writer exists: the watchdog's
        fractional hard-stop exit records its row on the SAFETY thread inside
        the trade lock, and the shadow needs a bars fetch (`daily_close_series`
        -> `_retry_read`, up to ~60 s on a degraded feed) — a blocking network
        call the safety loop must never make (run-7 fix-pass, review 2 #2).
        The watchdog records the row with the shadow None and queues the job;
        the decision thread fetches and stamps here. Touches ONLY the two
        shadow fields (measurement, never P&L). Same atomic tmp+replace as
        set_fill under the append lock. True when a row was stamped; False
        (never raises) when the id is unknown or the file can't be rewritten."""
        if not order_id:
            return False
        try:
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
                    if obj.get("order_id") == order_id and obj.get("action") == "sell":
                        obj["floor6_would_survive"] = survive
                        obj["floor6_worst_close_pct"] = worst_close_pct
                        out.append(json.dumps(obj))
                        hit = True
                    else:
                        out.append(line)
                if not hit:
                    return False
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text("".join(l + "\n" for l in out), encoding="utf-8")
                tmp.replace(self.path)
            return True
        except Exception as e:  # never let bookkeeping break the trade loop
            log.warning("Ledger set_floor_shadow failed for order %s: %s", order_id, e)
            return False

    @staticmethod
    def _restate_option_buy_at_fill(
        obj: dict, fill_price: float,
    ) -> tuple[Optional[dict], str]:
        """Field updates that move an option BUY row's cost_usd from the
        proposal's estimated debit to the broker's fill (run-7 4a-18, critic
        #13), or (None, reason) when the row must stay as recorded.

        The HD put of 2026-09-02 (HD261016P00350000): ledgered at 32.13 x
        100 x 1 = $3,213, filled at 34.30 = $3,430 — $217 light in every
        premium-cap and option-P&L read off the ledger. Restated only when
        the recorded cost IS premium x 100 x qty (within $1 / 0.5%): that
        identity is how risk sizes every option debit (approved_notional =
        contracts x premium x 100, single leg or net spread alike), and a
        row that does not satisfy it was priced some other way we won't
        guess at. entry_price (the decision quote) stays — same convention
        as equity buys; the as-recorded cost survives as quote_cost_usd and
        the restatement is a pure function of it (never compounds). Row
        qty, never fill_qty: effective() scales a partial on the row qty."""
        if (obj.get("instrument") or "equity") != "option":
            return None, "not an option row"
        if obj.get("action") != "buy":
            return None, "not a buy row"
        try:
            qty = float(obj.get("qty") or 0.0)
            px = float(obj.get("entry_price") or 0.0)
            origin = obj.get("quote_cost_usd")
            cost = float(origin if origin is not None else (obj.get("cost_usd") or 0.0))
        except (TypeError, ValueError):
            return None, "non-numeric row fields"
        if not (qty > 0 and math.isfinite(qty)):
            return None, "row qty is 0"
        if not (px > 0 and math.isfinite(px)):
            return None, "no entry premium on row"
        expected = px * 100.0 * qty
        if abs(cost - expected) > max(1.0, 0.005 * expected):
            return None, (
                f"cost_usd ${cost:,.2f} is not premium x 100 x qty "
                f"(${expected:,.2f}) — priced some other way, not restated"
            )
        return {
            "cost_usd": round(fill_price * 100.0 * qty, 2),
            "quote_cost_usd": round(cost, 2),
        }, "restated"

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

    def effective(self, dedup: bool = True) -> list[TradeRecord]:
        """Records with reconcile CORRECTIONS applied (goGA GA-2.5): a corrected
        record whose order filled 0 is dropped (the intent never executed — the
        old phantom-BUY-row bug); a partial fill is resized to what actually
        filled (qty, cost, and realized $ scaled proportionally; per-share
        prices and % are size-independent and stand). Correction rows themselves
        are consumed, never returned. Every read-side consumer (dashboard,
        attribution, lots, track record) should use this, not all().

        Run-7 4a-18: phantom / duplicate SELL rows (negative qty; a replaced
        or expired-and-resubmitted exit ledgered twice — see dedup_sells) are
        dropped AFTER the corrections, so every consumer sums the same
        phantom-free realized series the eval checker does. The file is
        never rewritten; `dedup=False` returns the corrected stream as-is.
        What was dropped is kept on `self.last_dropped` (phantoms() returns
        it) and logged ONCE per distinct set per process as
        `LEDGER PHANTOMS:` with the dollars the sum would otherwise carry."""
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
                    # 4a-18: the as-recorded option debit scales with the
                    # fill too, so cost_usd - quote_cost_usd stays the
                    # premium slippage on a partial.
                    "quote_cost_usd": (
                        round(r.quote_cost_usd * frac, 2)
                        if r.quote_cost_usd is not None else None
                    ),
                    "risk_note": (r.risk_note + " " if r.risk_note else "")
                    + f"[corrected: {c.qty:g}/{r.qty:g} filled]",
                })
            out.append(r)
        if not dedup:
            return out
        kept, dropped = dedup_sells(out)
        self.last_dropped = dropped
        sig = tuple((d.record.order_id, str(d.record.ts), d.rule) for d in dropped)
        if dropped and sig != getattr(self, "_phantoms_logged", None):
            self._phantoms_logged = sig
            shown = [d.line() for d in dropped[:_PHANTOM_LOG_ROWS]]
            more = len(dropped) - len(shown)
            log.info(
                "LEDGER PHANTOMS: dropped %d SELL row(s) worth $%+.2f that the "
                "realized sum would otherwise carry (kept %d rows): %s%s",
                len(dropped),
                sum(float(d.record.realized_pl or 0.0) for d in dropped),
                len(kept), "; ".join(shown),
                f"; ... and {more} more (ledger.phantoms() lists all)" if more else "",
            )
        return kept

    def phantoms(self) -> list[DroppedSell]:
        """The SELL rows effective() drops (run-7 4a-18) — what the checker's
        --show-dropped prints. Re-derived from the file on every call."""
        self.effective()
        return list(getattr(self, "last_dropped", []) or [])
