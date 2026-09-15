"""Account-status / P&L tracker — the honest "are we up or down?" view.

The trade LEDGER answers *why* we traded; it cannot answer *how much we've made*,
because exchange-side bracket auto-fills (the normal exit for whole-share buys)
never pass through our code, so the ledger structurally undercounts realized P&L.
The only complete source of truth is the Alpaca ACCOUNT itself.

This module reads the account and derives, from Alpaca's own numbers:

  total_return  = equity - base_value - net_cashflows
                  (backs out the initial funding AND later deposits/withdrawals —
                   the true cumulative P&L since inception)
  unrealized_pl = sum of open positions' unrealized P&L
  realized_pl   = total_return - unrealized_pl   (identity, no fill-by-fill replay)
  day_pl        = equity - last_equity            (today vs prior close — the
                                                   BROKER's figure; see below)

It also appends a once-per-day equity snapshot to state/equity_history.jsonl so the
curve survives restarts and a real backtest/report has history to read. On the
fixed close row that file measures day_pl against OUR OWN previous close row,
not the broker's last_equity (run-7 B1, EquityHistory.snapshot) — Alpaca
restates last_equity overnight, so the broker figure cannot telescope against
a series stamped by a different clock.

    python -m investment_strategy.status          # print the report
    python -m investment_strategy.status --no-snapshot   # don't append a snapshot
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

log = logging.getLogger("status")

# state/ is gitignored — local account history never gets committed.
DEFAULT_EQUITY_HISTORY_PATH = Path("state") / "equity_history.jsonl"

# Row bases that are a post-bell mark of the day (one immutable row per
# session) and therefore a valid predecessor for the next close row's
# day_pl. 'intraday' rows and legacy rows (no basis) are not.
_CLOSE_BASES = ("close", "late")


def _as_float(v) -> Optional[float]:
    """float(v) or None — a legacy row may carry a non-numeric equity."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None   # NaN is not an equity


class AccountStatus(BaseModel):
    """A point-in-time snapshot of the real Alpaca account. Optional fields are
    None when the portfolio-history call (needed for the capital basis) fails —
    day P&L and unrealized always work from the plain account/position read."""
    as_of: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    equity: float
    cash: float
    buying_power: float
    n_positions: int
    unrealized_pl: float                       # sum over open positions
    day_pl: float                              # equity - last_equity
    day_pl_pct: float
    capital_in: Optional[float] = None         # base_value + net deposits = $ funded
    total_return: Optional[float] = None       # equity - capital_in (net of deposits)
    total_return_pct: Optional[float] = None
    realized_pl: Optional[float] = None        # total_return - unrealized_pl
    pattern_day_trader: bool = False           # PDT-flagged (margin accounts)
    daytrade_count: int = 0                    # day trades in trailing 5 business days

    @property
    def is_up(self) -> Optional[bool]:
        return None if self.total_return is None else self.total_return >= 0


def compute_status(broker) -> AccountStatus:
    """Build an AccountStatus from the live Alpaca account. `broker` is an
    AlpacaClient. Never raises on the optional (history-derived) fields."""
    account = broker.get_account()
    unrealized = round(sum(p.unrealized_pl for p in account.positions), 2)

    capital_in = total_return = total_return_pct = realized = None
    basis = broker.portfolio_basis()
    if basis is not None:
        base_value, net_cashflows = basis
        capital_in = round(base_value + net_cashflows, 2)
        total_return = round(account.equity - capital_in, 2)
        total_return_pct = round(
            (total_return / capital_in * 100.0) if capital_in else 0.0, 2
        )
        realized = round(total_return - unrealized, 2)

    return AccountStatus(
        equity=round(account.equity, 2),
        cash=round(account.cash, 2),
        buying_power=round(account.buying_power, 2),
        n_positions=len(account.positions),
        unrealized_pl=unrealized,
        day_pl=round(account.day_pl, 2),
        day_pl_pct=round(account.day_pl_pct, 2),
        capital_in=capital_in,
        total_return=total_return,
        total_return_pct=total_return_pct,
        realized_pl=realized,
        pattern_day_trader=account.pattern_day_trader,
        daytrade_count=account.daytrade_count,
    )


class EquityHistory:
    """Append-only daily equity snapshots (JSON-Lines). At most one row per
    calendar day — repeated calls the same day overwrite that day's row so a
    fast loop doesn't bloat the file.

    Row fields: date, ts, equity, cash, unrealized_pl, realized_pl,
    total_return, day_pl, then the additive ones — basis (run-6 1b),
    book_beta_spy (run-6 8), broker_day_pl + day_pl_basis (run-7 B1, close/
    late rows only). Readers must tolerate a missing field: older rows in the
    same file never get rewritten."""

    def __init__(self, path: Path | str = DEFAULT_EQUITY_HISTORY_PATH):
        self.path = Path(path)

    def snapshot(
        self, status: AccountStatus, basis: Optional[str] = None,
        day: Optional[str] = None, extra: Optional[dict] = None,
    ) -> None:
        """Record today's snapshot, replacing any earlier row for the same date.
        Best-effort — never raises into the trade loop.

        Run-6 item 1b: `basis` labels the row ('close' = the fixed post-bell
        stamp, 'intraday' = an in-session read; missing = legacy row). A
        'close' row is final: a later non-close snapshot for the same date
        is dropped rather than overwriting it. `day` overrides the date key
        (the ET trading day — after 20:00 ET the UTC date has rolled).
        `extra` adds report-only fields to the row (run-6 item 8: the close
        row carries `book_beta_spy`, the cycle's ex-ante SPY beta, so the
        eval checker can beta-adjust capture per day).

        Run-7 B1 (self-consistent close-row day_pl): on a 'close'/'late' row
        `day_pl` = equity - the PREVIOUS close/late row's equity (our own
        series, one clock) and the broker's figure is kept verbatim as
        `broker_day_pl`; `day_pl_basis` names the rule that produced day_pl
        ('self' = a prior close/late row existed, 'broker' = none yet, so the
        broker figure stands unchanged). WHY: the broker's day_pl is
        equity - last_equity and Alpaca RESTATES last_equity overnight
        (dividends / corporate actions / after-hours option marks), so on
        run-6 it missed our own close-to-close delta by -$777 (Sep 2),
        +$1,421 (Sep 3), +$1,146 (Sep 4), +$1,417 (Sep 8), +$195 (Sep 9),
        +$563 (Sep 10) and the contract's telescoping validity check
        (|dEquity - day_pl| < $1) FAILED by construction on 6 of 7 close
        days. Intraday rows are untouched (never a bell mark) and legacy
        rows (no basis) are never a predecessor (they were re-stamped with
        after-hours marks). No runtime gate reads a history row's day_pl —
        this is measurement only."""
        try:
            today = day or status.as_of.date().isoformat()
            existing = self._read()
            if basis != "close" and any(
                r.get("date") == today and r.get("basis") == "close"
                for r in existing
            ):
                return  # the day's close row is final
            rows = [
                r for r in existing
                if r.get("date") != today  # drop an earlier same-day row
            ]
            row = {
                "date": today,
                "ts": status.as_of.isoformat(),
                "equity": status.equity,
                "cash": status.cash,
                "unrealized_pl": status.unrealized_pl,
                "realized_pl": status.realized_pl,
                "total_return": status.total_return,
                "day_pl": status.day_pl,
            }
            if basis:
                row["basis"] = basis
            if basis in _CLOSE_BASES:
                self._stamp_close_day_pl(row, status, existing, today)
            for k, v in (extra or {}).items():
                if k not in row:
                    row[k] = v
            rows.append(row)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
            )
            tmp.replace(self.path)  # atomic-ish swap
        except Exception as e:  # persistence must never break the loop
            log.warning("equity snapshot failed: %s", e)

    @staticmethod
    def _prior_close_row(rows: list[dict], day: str) -> Optional[dict]:
        """The latest close/late row dated strictly before `day` that carries
        an equity figure — the predecessor a close row's day_pl is measured
        against (run-7 B1). Intraday and legacy (no basis) rows are skipped;
        a run-6 close row without the new fields still qualifies."""
        prior = [
            r for r in rows
            if (r.get("date") or "") < day
            and r.get("basis") in _CLOSE_BASES
            and _as_float(r.get("equity")) is not None   # a malformed row is no predecessor
        ]
        return max(prior, key=lambda r: r["date"]) if prior else None

    def _stamp_close_day_pl(
        self, row: dict, status: AccountStatus, existing: list[dict], day: str,
    ) -> None:
        """Run-7 B1: on a close/late row always keep the broker's day_pl as
        `broker_day_pl`, and make `day_pl` equity - previous close/late row
        equity when such a row exists (`day_pl_basis`='self'); otherwise the
        broker figure stands (`day_pl_basis`='broker')."""
        row["broker_day_pl"] = status.day_pl
        row["day_pl_basis"] = "broker"
        # Own guard, separate from snapshot()'s: a bad predecessor must
        # DEGRADE this row to the broker figure, not lose it. Inside the
        # outer try the exception would abort the write, has_close_row would
        # stay False, every 30 s tick would retry into the same failure until
        # midnight, and the date would end with NO close row — a VOID verdict
        # day for the contract v3 checker.
        try:
            prev = self._prior_close_row(existing, day)
            if prev is None:
                log.info(
                    "Equity close row %s day_pl keeps the broker figure %.2f "
                    "(no prior close/late row).", day, status.day_pl,
                )
                return
            prev_equity = float(prev["equity"])
            day_pl = round(float(status.equity) - prev_equity, 2)
        except Exception as e:  # noqa: BLE001 — degrade, never drop the row
            log.warning(
                "Equity close row %s day_pl keeps the broker figure %.2f: "
                "predecessor unusable (%s: %s).", day, status.day_pl,
                e.__class__.__name__, e,
            )
            return
        row["day_pl"] = day_pl
        row["day_pl_basis"] = "self"
        log.info(
            "Equity close row %s day_pl self-consistent: %.2f = equity %.2f - "
            "prev %s equity %.2f; broker_day_pl %.2f (last_equity restatement "
            "%+.2f).", day, row["day_pl"], status.equity, prev.get("date"),
            prev_equity, status.day_pl, round(row["day_pl"] - status.day_pl, 2),
        )

    def _read(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def all(self) -> list[dict]:
        return sorted(self._read(), key=lambda r: r.get("date", ""))

    def has_close_row(self, day: str, bases: tuple[str, ...] = ("close",)) -> bool:
        """True when `day` already has its fixed post-bell row (basis in
        `bases`; 'late' = stamped by a bot started after the 16:xx tick)."""
        return any(
            r.get("date") == day and r.get("basis") in bases
            for r in self._read()
        )


# --------------------------------------------------------------------------- #
# CLI report
# --------------------------------------------------------------------------- #
def _money(v: Optional[float]) -> str:
    if v is None:
        return "n/a"
    sign = "+" if v >= 0 else "-"
    return f"{sign}${abs(v):,.2f}"


def render_report(status: AccountStatus) -> str:
    lines = [
        "═══ Account status ═══",
        f"  Equity         ${status.equity:,.2f}",
        f"  Cash           ${status.cash:,.2f}   Buying power ${status.buying_power:,.2f}",
        f"  Open positions {status.n_positions}",
        "",
        f"  Today's P&L    {_money(status.day_pl)} ({status.day_pl_pct:+.2f}%)",
        f"  Unrealized     {_money(status.unrealized_pl)} (open positions)",
    ]
    if status.total_return is not None:
        verdict = "UP ▲" if status.is_up else "DOWN ▼"
        lines += [
            f"  Realized       {_money(status.realized_pl)} (closed, net of deposits)",
            "",
            f"  Capital funded ${status.capital_in:,.2f}",
            f"  TOTAL RETURN   {_money(status.total_return)} "
            f"({status.total_return_pct:+.2f}%)  →  {verdict}",
        ]
    else:
        lines += [
            "",
            "  TOTAL RETURN   n/a (portfolio history unavailable this run)",
        ]
    # PDT status matters only for a sub-$25k account that can day-trade (margin).
    if status.equity < 25_000:
        if status.pattern_day_trader:
            lines.append(
                f"\n  ⚠ PDT-FLAGGED under $25k (day-trades 5d: {status.daytrade_count}) "
                "— opening restricted. Use a CASH account."
            )
        elif status.daytrade_count:
            lines.append(
                f"\n  Day-trades (5d): {status.daytrade_count} "
                "(PDT flag at 4 on a margin account under $25k)"
            )
    return "\n".join(lines)


def main() -> int:
    logging.basicConfig(level="INFO", format="%(name)s | %(message)s")
    ap = argparse.ArgumentParser(description="Print real Alpaca account status / P&L.")
    ap.add_argument("--no-snapshot", dest="snapshot", action="store_false",
                    help="don't append today's equity snapshot to state/")
    args = ap.parse_args()

    from .config import load_config
    from .execution import AlpacaClient

    broker = AlpacaClient(load_config())
    status = compute_status(broker)
    if args.snapshot:
        EquityHistory().snapshot(status)
    print(render_report(status))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
