"""Trade dashboard — a self-contained HTML view of everything the bot has traded.

Reads the trade ledger (state/trades.jsonl) and renders a single, dependency-free
HTML file: summary cards, two inline-SVG charts (capital deployed per symbol and
cumulative invested over time), and a full table of every trade — executed date,
volume, cost invested, planned exit (take-profit / stop-loss = the "assumed sell"
levels), profit % assumed, and the reason behind the purchase.

If Alpaca credentials are available it best-effort enriches open BUY positions
with the live price so you can see current value and unrealized P/L; without
them it still renders the full ledger offline.

    python -m investment_strategy.dashboard                 # write dashboard.html
    python -m investment_strategy.dashboard -o out.html     # custom path
    python -m investment_strategy.dashboard --open          # write + open browser
    python -m investment_strategy.dashboard --no-live       # skip price enrichment
"""
from __future__ import annotations

import argparse
import html
import logging
import webbrowser
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from .ledger import TradeLedger, TradeRecord
from .status import AccountStatus

log = logging.getLogger("dashboard")

_ET = ZoneInfo("America/New_York")

# Palette — calm, readable, prints fine.
_BG = "#0f1419"
_CARD = "#1a2027"
_INK = "#e6edf3"
_MUTE = "#8b949e"
_GRID = "#2d333b"
_GREEN = "#3fb950"
_RED = "#f85149"
_BLUE = "#58a6ff"
_AMBER = "#d29922"


# --------------------------------------------------------------------------- #
# Optional live enrichment
# --------------------------------------------------------------------------- #
def _live_enrichment(
    symbols: set[str],
) -> tuple[dict[str, float], Optional[AccountStatus]]:
    """Best-effort live data via Alpaca: current prices for ledger symbols AND the
    real account status (equity, P&L, total return). Returns ({}, None) if Alpaca
    is unavailable so the dashboard still renders the ledger offline."""
    try:
        from .config import load_config
        from .execution import AlpacaClient
        from .status import compute_status

        broker = AlpacaClient(load_config())
        prices: dict[str, float] = {}
        for s in symbols:
            px = broker.latest_price(s)
            if px > 0:
                prices[s] = px
        try:
            status = compute_status(broker)
        except Exception as e:  # account read shouldn't sink price enrichment
            log.info("Account status unavailable (%s).", e)
            status = None
        return prices, status
    except Exception as e:
        log.info("Live enrichment skipped (%s).", e)
        return {}, None


# --------------------------------------------------------------------------- #
# Derived per-trade view
# --------------------------------------------------------------------------- #
class _Row:
    """A trade plus any live-derived numbers, ready to render."""

    def __init__(self, rec: TradeRecord, price_now: Optional[float],
                 position_closed: bool = False):
        self.rec = rec
        self.price_now = price_now
        # `position_closed`: the symbol's whole position is gone, so a live
        # unrealized number on the BUY row would be phantom P/L on shares no
        # longer held — the outcome lives on the matching SELL row instead.
        self.position_closed = position_closed
        self.current_value: Optional[float] = None
        self.unreal_pl: Optional[float] = None
        self.unreal_pct: Optional[float] = None
        # Sell economics: what the position cost going in (basis), what the
        # sale returned (proceeds), and the realized result.
        self.proceeds: Optional[float] = None
        self.basis: Optional[float] = None
        self.realized_pl: Optional[float] = None
        self.realized_pct: Optional[float] = None
        if (
            rec.action == "buy" and rec.instrument == "equity"
            and not position_closed
            and price_now and rec.entry_price > 0 and rec.qty > 0
        ):
            self.current_value = price_now * rec.qty
            self.unreal_pl = self.current_value - rec.entry_price * rec.qty
            self.unreal_pct = (price_now / rec.entry_price - 1.0) * 100.0
        elif rec.action == "sell":
            self.realized_pl = rec.realized_pl
            self.realized_pct = rec.realized_pl_pct
            # Equity only: an option's exit_price is per-share premium while
            # qty is contracts, so price*qty would be 100x off. Option rows
            # still show recorded realized $ — just no derived proceeds/basis.
            if rec.exit_price and rec.qty > 0 and rec.instrument == "equity":
                self.proceeds = rec.exit_price * rec.qty
            if self.proceeds is not None:
                # Basis (what went in for the sold shares): prefer exact $,
                # else reconstruct from the recorded %.
                if self.realized_pl is not None:
                    self.basis = self.proceeds - self.realized_pl
                elif self.realized_pct is not None and self.realized_pct > -100.0:
                    self.basis = self.proceeds / (1.0 + self.realized_pct / 100.0)
                    self.realized_pl = self.proceeds - self.basis
            if self.realized_pl is not None and self.basis and self.basis > 0:
                # Keep % and $ describing the same slice: a partially-covered
                # exchange backfill records a per-share % beside a
                # covered-slice $ — recompute % from the numbers shown.
                self.realized_pct = self.realized_pl / self.basis * 100.0


# --------------------------------------------------------------------------- #
# Tiny inline-SVG charting (no JS, no CDN, prints + emails fine)
# --------------------------------------------------------------------------- #
def _bar_chart(pairs: list[tuple[str, float]], unit: str = "$") -> str:
    """Horizontal bars, value-labeled. pairs = [(label, value), ...]."""
    if not pairs:
        return "<p class='empty'>No data.</p>"
    pairs = sorted(pairs, key=lambda kv: kv[1], reverse=True)
    top = max(v for _, v in pairs) or 1.0
    row_h, gap, label_w, bar_max, pad = 26, 8, 70, 360, 8
    width = label_w + bar_max + 90
    height = pad * 2 + len(pairs) * (row_h + gap)
    parts = [f"<svg viewBox='0 0 {width} {height}' width='100%' "
             f"role='img' class='chart'>"]
    y = pad
    for label, val in pairs:
        w = max(2.0, bar_max * (val / top))
        parts.append(
            f"<text x='{label_w - 6}' y='{y + row_h * 0.68}' "
            f"text-anchor='end' class='c-lbl'>{html.escape(label)}</text>"
            f"<rect x='{label_w}' y='{y}' width='{w:.1f}' height='{row_h}' "
            f"rx='3' fill='{_BLUE}' />"
            f"<text x='{label_w + w + 6}' y='{y + row_h * 0.68}' "
            f"class='c-val'>{unit}{val:,.0f}</text>"
        )
        y += row_h + gap
    parts.append("</svg>")
    return "".join(parts)


def _area_chart(points: list[tuple[datetime, float]]) -> str:
    """Cumulative line/area over time."""
    if len(points) < 2:
        return "<p class='empty'>Not enough trades yet for a trend.</p>"
    w, h, pad = 720, 240, 36
    xs = [p[0].timestamp() for p in points]
    ys = [p[1] for p in points]
    x0, x1 = min(xs), max(xs)
    y1 = max(ys) or 1.0
    xspan = (x1 - x0) or 1.0

    def px(t):
        return pad + (t - x0) / xspan * (w - pad * 2)

    def py(v):
        return h - pad - (v / y1) * (h - pad * 2)

    pts = [(px(t), py(v)) for t, v in zip(xs, ys)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    area = (f"{pad},{h - pad} " + line +
            f" {pts[-1][0]:.1f},{h - pad}")
    # y gridlines (0, 50%, 100%)
    grid = []
    for frac in (0.0, 0.5, 1.0):
        gy = py(y1 * frac)
        grid.append(
            f"<line x1='{pad}' y1='{gy:.1f}' x2='{w - pad}' y2='{gy:.1f}' "
            f"stroke='{_GRID}' stroke-width='1' />"
            f"<text x='{pad - 6}' y='{gy + 4:.1f}' text-anchor='end' "
            f"class='c-lbl'>${y1 * frac:,.0f}</text>"
        )
    start = points[0][0].strftime("%b %d")
    end = points[-1][0].strftime("%b %d")
    return (
        f"<svg viewBox='0 0 {w} {h}' width='100%' role='img' class='chart'>"
        + "".join(grid) +
        f"<polygon points='{area}' fill='{_BLUE}' opacity='0.15' />"
        f"<polyline points='{line}' fill='none' stroke='{_BLUE}' "
        f"stroke-width='2.5' />"
        f"<text x='{pad}' y='{h - 8}' class='c-lbl'>{start}</text>"
        f"<text x='{w - pad}' y='{h - 8}' text-anchor='end' "
        f"class='c-lbl'>{end}</text>"
        "</svg>"
    )


# --------------------------------------------------------------------------- #
# HTML assembly
# --------------------------------------------------------------------------- #
def _card(label: str, value: str, sub: str = "", tone: str = "") -> str:
    color = {"up": _GREEN, "down": _RED, "": _INK}.get(tone, _INK)
    sub_html = f"<div class='card-sub'>{html.escape(sub)}</div>" if sub else ""
    return (
        f"<div class='card'><div class='card-lbl'>{html.escape(label)}</div>"
        f"<div class='card-val' style='color:{color}'>{value}</div>{sub_html}</div>"
    )


def _fmt_dt(dt: datetime) -> str:
    """Ledger timestamps are UTC; render in ET so times read as market time
    (the raw UTC previously showed a 10:33 ET fill as '14:33')."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_ET).strftime("%Y-%m-%d %H:%M")


def _exit_cell(row: _Row) -> str:
    """Buys: the 'assumed sell' bracket levels. Sells: what actually happened —
    the exit fill and the total proceeds coming back."""
    r = row.rec
    if r.action == "sell":
        if not r.exit_price:
            return "<span class='mute'>—</span>"
        reason = f" · {html.escape(r.exit_reason)}" if r.exit_reason else ""
        proceeds = (f"<br><span class='up'>${row.proceeds:,.0f} back</span>"
                    if row.proceeds is not None else "")
        return (f"<span>Sold @ ${r.exit_price:,.2f}{reason}</span>{proceeds}")
    if r.action != "buy":
        return "<span class='mute'>—</span>"
    tp = (f"TP {r.take_profit_pct:.0f}%"
          + (f" (${r.take_profit_price:,.2f})" if r.take_profit_price else ""))
    sl = (f"SL {r.stop_loss_pct:.0f}%"
          + (f" (${r.stop_loss_price:,.2f})" if r.stop_loss_price else ""))
    return (f"<span class='up'>{tp}</span><br>"
            f"<span class='down'>{sl}</span>")


def _fmt_pl(pl: Optional[float], pct: Optional[float],
            label: str = "") -> str:
    """±%/±$ pair with up/down coloring; '—' when unknown."""
    if pl is None and pct is None:
        return "<span class='mute'>—</span>"
    ref = pl if pl is not None else pct
    cls = "up" if ref >= 0 else "down"
    sign = "+" if ref >= 0 else ""
    pct_s = f"{sign}{pct:,.1f}%" if pct is not None else ""
    pl_s = f"{sign}${pl:,.0f}" if pl is not None else ""
    lbl = f"<br><span class='mute'>{label}</span>" if label else ""
    joiner = "<br>" if pct_s and pl_s else ""
    return f"<span class='{cls}'>{pct_s}{joiner}{pl_s}</span>{lbl}"


def _pl_cell(row: _Row) -> str:
    if row.rec.action == "sell":
        return _fmt_pl(row.realized_pl, row.realized_pct, label="realized")
    if row.position_closed:
        return "<span class='mute'>closed — see sell row</span>"
    return _fmt_pl(row.unreal_pl, row.unreal_pct)


# --------------------------------------------------------------------------- #
# Per-ticker round trips
# --------------------------------------------------------------------------- #
def aggregate_round_trips(
    records: list[TradeRecord], prices: dict[str, float],
) -> list[dict]:
    """Group the ledger by (symbol, instrument) into round-trip economics:
    money in (buys), money out (sell proceeds), realized P/L on the closed
    part, live value + unrealized P/L on whatever is still open, and the net.
    Pure function over ledger records — unit-testable without a broker."""
    groups: dict[tuple[str, str], dict] = {}
    for r in sorted(records, key=lambda x: x.ts):
        g = groups.setdefault((r.symbol, r.instrument), {
            "symbol": r.symbol, "instrument": r.instrument,
            "bought_qty": 0.0, "bought_usd": 0.0,
            "sold_qty": 0.0, "proceeds_usd": 0.0,
            "realized_pl": None, "last_ts": r.ts,
        })
        g["last_ts"] = max(g["last_ts"], r.ts)
        if r.action == "buy":
            g["bought_qty"] += r.qty
            g["bought_usd"] += r.cost_usd
        elif r.action == "sell":
            if r.qty > 0:
                sell_qty = r.qty
            elif r.realized_pl is not None or r.realized_pl_pct is not None:
                # Legacy full-close shape (pre-GA-2.5, mirrored from lots.py):
                # qty 0/unknown with realized data means "sold everything held".
                sell_qty = max(0.0, g["bought_qty"] - g["sold_qty"])
            else:
                sell_qty = 0.0
            g["sold_qty"] += sell_qty
            realized = r.realized_pl
            # Equity only — see _Row: option exit_price is per-share premium.
            if r.exit_price and sell_qty > 0 and r.instrument == "equity":
                proceeds = r.exit_price * sell_qty
                g["proceeds_usd"] += proceeds
                if realized is None and (
                    r.realized_pl_pct is not None and r.realized_pl_pct > -100.0
                ):
                    realized = proceeds - proceeds / (1.0 + r.realized_pl_pct / 100.0)
            if realized is not None:
                g["realized_pl"] = (g["realized_pl"] or 0.0) + realized
    out = []
    for g in groups.values():
        g["oversold"] = g["sold_qty"] > g["bought_qty"] + 1e-6
        open_qty = max(0.0, g["bought_qty"] - g["sold_qty"])
        # Tolerate float dust from fractional fills. $0.05 absolute: broker
        # rounding on sub-share sells leaves ~1e-4 sh slivers (ORCL 2026-07-14:
        # 0.000085 sh ≈ $0.011) that the old $0.01 bar counted as still-open,
        # freezing the round trip forever.
        if open_qty * max(prices.get(g["symbol"], 0.0), 1.0) < 0.05:
            open_qty = 0.0
        g["open_qty"] = open_qty
        avg_cost = (g["bought_usd"] / g["bought_qty"]) if g["bought_qty"] > 0 else 0.0
        # Open basis = dollars in minus the basis the sells consumed
        # (proceeds - realized). Lifetime average cost misprices the remainder
        # whenever a closed lot traded at a different price (re-entry after a
        # full close, multi-lot FIFO backfills) — it fabricated unrealized P/L.
        consumed = (
            g["proceeds_usd"] - g["realized_pl"]
            if (g["realized_pl"] is not None and g["proceeds_usd"] > 0)
            else None
        )
        if open_qty <= 0:
            g["open_basis"] = 0.0
        elif consumed is not None and 0.0 <= consumed <= g["bought_usd"]:
            g["open_basis"] = g["bought_usd"] - consumed
        else:
            g["open_basis"] = avg_cost * open_qty
        px = prices.get(g["symbol"]) if g["instrument"] == "equity" else None
        g["open_value"] = px * open_qty if (px and open_qty > 0) else None
        g["unreal_pl"] = (
            g["open_value"] - g["open_basis"] if g["open_value"] is not None else None
        )
        parts = [v for v in (g["realized_pl"], g["unreal_pl"]) if v is not None]
        g["net_pl"] = sum(parts) if parts else None
        out.append(g)
    out.sort(key=lambda g: g["last_ts"], reverse=True)
    return out


_TABLE_HEAD = (
    "<tr><th>Executed (ET)</th><th>Symbol</th><th>Side</th><th>Type</th>"
    "<th class='num'>Volume</th><th class='num'>Entry</th>"
    "<th class='num'>Went in ($)</th><th>Planned / actual exit</th>"
    "<th class='num'>Profit % assumed</th><th>P/L</th>"
    "<th>Conviction</th><th>Reason behind the trade</th></tr>"
)


def _row_html(row: _Row) -> str:
    r = row.rec
    side_cls = "buy-pill" if r.action == "buy" else "sell-pill"
    signals = ""
    if r.key_signals:
        chips = "".join(
            f"<span class='chip'>{html.escape(s)}</span>" for s in r.key_signals
        )
        signals = f"<div class='chips'>{chips}</div>"
    conv = (f"<div class='conv'><div class='conv-bar' "
            f"style='width:{r.conviction * 100:.0f}%'></div></div>"
            f"<span class='mute'>{r.conviction:.2f}</span>") if r.conviction else \
        "<span class='mute'>—</span>"
    strat = f" <span class='mute'>({r.option_strategy})</span>" if r.option_strategy else ""
    # Sells carry no entry/cost of their own in the ledger — surface the basis
    # of the shares sold ("went in for") so the row reads in → out → result.
    if r.action == "sell" and row.basis is not None and r.qty > 0:
        entry_cell = f"${row.basis / r.qty:,.2f}"
        cost_cell = f"${row.basis:,.0f} <span class='mute'>in</span>"
    else:
        entry_cell = f"${r.entry_price:,.2f}" if r.entry_price else "—"
        cost_cell = f"${r.cost_usd:,.0f}"
    return (
        "<tr>"
        f"<td class='nowrap'>{_fmt_dt(r.ts)}</td>"
        f"<td class='sym'>{html.escape(r.symbol)}</td>"
        f"<td><span class='{side_cls}'>{r.action.upper()}</span></td>"
        f"<td>{html.escape(r.instrument)}{strat}</td>"
        f"<td class='num'>{r.qty:,.4g}</td>"
        f"<td class='num'>{entry_cell}</td>"
        f"<td class='num'>{cost_cell}</td>"
        f"<td>{_exit_cell(row)}</td>"
        f"<td class='num up'>{('%.0f%%' % r.take_profit_pct) if r.action == 'buy' and r.take_profit_pct else '—'}</td>"
        f"<td class='num'>{_pl_cell(row)}</td>"
        f"<td>{conv}</td>"
        f"<td class='reason'>{html.escape(r.rationale) or '<span class=\"mute\">—</span>'}{signals}</td>"
        "</tr>"
    )


def _table(rows: list[_Row]) -> str:
    if not rows:
        return ("<p class='empty'>No trades recorded yet. The ledger fills as the "
                "bot executes orders.</p>")
    body = [
        _row_html(row) for row in sorted(rows, key=lambda x: x.rec.ts, reverse=True)
    ]
    return f"<table><thead>{_TABLE_HEAD}</thead><tbody>{''.join(body)}</tbody></table>"


def fifo_consumed_buys(records: list[TradeRecord]) -> set[int]:
    """id()s of BUY records whose shares were fully sold, matching sells to
    buys FIFO within each (symbol, instrument) group. A consumed buy's "live"
    unrealized P/L would be phantom — the outcome already realized on sells —
    including the re-entry case where the GROUP holds shares again but this
    particular lot is long gone."""
    out: set[int] = set()
    open_lots: dict[tuple[str, str], list[list]] = defaultdict(list)
    for r in sorted(records, key=lambda x: x.ts):
        key = (r.symbol, r.instrument)
        if r.action == "buy" and r.qty > 0:
            open_lots[key].append([r.qty, r])
        elif r.action == "sell":
            lots = open_lots[key]
            if r.qty > 0:
                remaining = r.qty
            elif r.realized_pl is not None or r.realized_pl_pct is not None:
                remaining = sum(q for q, _ in lots)  # legacy full close
            else:
                continue
            while remaining > 1e-9 and lots:
                take = min(lots[0][0], remaining)
                lots[0][0] -= take
                remaining -= take
                if lots[0][0] <= 1e-9:
                    out.add(id(lots[0][1]))
                    lots.pop(0)
    return out


def _rt_stat(label: str, value: str) -> str:
    return (f"<div class='rt-stat'><div class='rt-lbl'>{html.escape(label)}</div>"
            f"<div class='rt-val'>{value}</div></div>")


def _round_trips_html(trips: list[dict], rows: list[_Row]) -> str:
    """One expandable block per (symbol, instrument): the rollup line answers
    'how much in, how much out, what's the total P/L for this ticker'; expanding
    shows that ticker's buys and sells together, newest first."""
    if not trips:
        return "<p class='empty'>No trades recorded yet.</p>"
    by_group: dict[tuple[str, str], list[_Row]] = defaultdict(list)
    for row in rows:
        by_group[(row.rec.symbol, row.rec.instrument)].append(row)
    blocks = []
    for g in trips:
        grp_rows = sorted(
            by_group.get((g["symbol"], g["instrument"]), []),
            key=lambda x: x.rec.ts, reverse=True,
        )
        inner = (f"<div class='tablewrap'><table><thead>{_TABLE_HEAD}</thead>"
                 f"<tbody>{''.join(_row_html(r) for r in grp_rows)}</tbody>"
                 "</table></div>")
        if g["open_qty"] > 0:
            open_val = (f"{g['open_qty']:,.4g} open"
                        + (f" · ${g['open_value']:,.0f}" if g["open_value"] is not None else "")
                        + (f" · {_fmt_pl(g['unreal_pl'], None)}"
                           if g["unreal_pl"] is not None else ""))
        elif g.get("oversold"):
            # More sold than the ledger ever bought (e.g. positions surviving a
            # reset.py wipe) — say so instead of hiding it behind "flat".
            open_val = (f"<span class='down'>oversold — {g['sold_qty']:,.4g} "
                        f"sold vs {g['bought_qty']:,.4g} recorded</span>")
        else:
            open_val = "<span class='mute'>flat</span>" if g["sold_qty"] else \
                "<span class='mute'>—</span>"
        if not g["sold_qty"]:
            out_cell = "<span class='mute'>—</span>"
        elif g["proceeds_usd"] > 0:
            out_cell = (f"${g['proceeds_usd']:,.0f} <span class='mute'>· "
                        f"{g['sold_qty']:,.4g}</span>")
        else:
            # Sold, but no exit price on record (option closes) — unknown
            # proceeds, not zero.
            out_cell = (f"<span class='mute'>$? · {g['sold_qty']:,.4g}</span>")
        stats = [
            _rt_stat("In (buys)",
                     f"${g['bought_usd']:,.0f} <span class='mute'>· "
                     f"{g['bought_qty']:,.4g}</span>"),
            _rt_stat("Out (sells)", out_cell),
            _rt_stat("Realized", _fmt_pl(g["realized_pl"], None)),
            _rt_stat("Still open", open_val),
            _rt_stat("Net P/L", _fmt_pl(g["net_pl"], None)),
        ]
        opt = (" <span class='mute'>(option)</span>"
               if g["instrument"] == "option" else "")
        blocks.append(
            "<details class='rt'><summary>"
            f"<div class='rt-stat'><div class='rt-lbl'>Ticker</div>"
            f"<div class='rt-val sym'>{html.escape(g['symbol'])}{opt}</div></div>"
            + "".join(stats) +
            "<span class='rt-hint'>trades ▾</span>"
            f"</summary>{inner}</details>"
        )
    return "".join(blocks)


def _account_panel(status: Optional[AccountStatus]) -> str:
    """Cards for the REAL Alpaca account (truth), distinct from the ledger-derived
    'invested' cards below. Omitted entirely when the account can't be read."""
    if status is None:
        return ""
    cards = [
        _card("Account equity", f"${status.equity:,.0f}",
              f"${status.cash:,.0f} cash · {status.n_positions} open"),
        _card("Today's P&L", f"{'+' if status.day_pl >= 0 else ''}${status.day_pl:,.0f}",
              f"{status.day_pl_pct:+.2f}%",
              tone="up" if status.day_pl >= 0 else "down"),
        _card("Unrealized P&L",
              f"{'+' if status.unrealized_pl >= 0 else ''}${status.unrealized_pl:,.0f}",
              "open positions",
              tone="up" if status.unrealized_pl >= 0 else "down"),
    ]
    if status.total_return is not None:
        up = status.is_up
        cards.append(_card(
            "Total return", f"{'+' if up else ''}${status.total_return:,.0f}",
            f"{status.total_return_pct:+.2f}% · realized "
            f"{'+' if (status.realized_pl or 0) >= 0 else ''}${status.realized_pl:,.0f} "
            f"· net of deposits",
            tone="up" if up else "down",
        ))
    return ("<h2 class='section'>Account (live)</h2>"
            f"<div class='cards'>{''.join(cards)}</div>")


def build_html(
    records: list[TradeRecord], prices: dict[str, float],
    account: Optional[AccountStatus] = None,
) -> str:
    trips = aggregate_round_trips(records, prices)
    closed_groups = {
        (g["symbol"], g["instrument"]) for g in trips
        if g["sold_qty"] > 0 and g["open_qty"] <= 0
    }
    # Per-BUY FIFO consumption (not per-group): after a re-entry the group
    # holds shares again, but the original buy's shares are gone and its
    # "unrealized" P/L would be phantom.
    consumed_buys = fifo_consumed_buys(records)
    rows = [
        _Row(r, prices.get(r.symbol),
             position_closed=(r.action == "buy" and id(r) in consumed_buys))
        for r in records
    ]
    buys = [r for r in records if r.action == "buy"]

    total_invested = sum(r.cost_usd for r in buys)
    symbols = sorted({r.symbol for r in records})
    n_buys, n_sells = len(buys), sum(1 for r in records if r.action == "sell")

    live_rows = [r for r in rows if r.unreal_pl is not None]
    total_unreal = sum(r.unreal_pl for r in live_rows) if live_rows else None
    cur_basis = sum(r.rec.entry_price * r.rec.qty for r in live_rows) or 1.0
    unreal_pct = (total_unreal / cur_basis * 100.0) if total_unreal is not None else None

    realized_parts = [g["realized_pl"] for g in trips if g["realized_pl"] is not None]
    total_realized = sum(realized_parts) if realized_parts else None
    n_closed = len(closed_groups)

    # charts
    per_symbol = defaultdict(float)
    for r in buys:
        per_symbol[r.symbol] += r.cost_usd
    bar = _bar_chart(list(per_symbol.items()))

    cum, running = [], 0.0
    for r in sorted(buys, key=lambda x: x.ts):
        running += r.cost_usd
        cum.append((r.ts, running))
    area = _area_chart(cum)

    # cards
    cards = [
        _card("Total invested", f"${total_invested:,.0f}",
              f"{n_buys} buys · {n_sells} sells"),
        _card("Symbols traded", str(len(symbols)),
              ", ".join(symbols[:6]) + ("…" if len(symbols) > 6 else "")),
        _card("Avg cost / buy",
              f"${(total_invested / n_buys):,.0f}" if n_buys else "—",
              "per opening order"),
    ]
    if total_realized is not None:
        tone = "up" if total_realized >= 0 else "down"
        sign = "+" if total_realized >= 0 else ""
        cards.append(_card(
            "Realized P/L", f"{sign}${total_realized:,.0f}",
            f"{n_sells} sells · {n_closed} closed round trips",
            tone=tone,
        ))
    if total_unreal is not None:
        tone = "up" if total_unreal >= 0 else "down"
        sign = "+" if total_unreal >= 0 else ""
        cards.append(_card(
            "Unrealized P/L", f"{sign}${total_unreal:,.0f}",
            f"{sign}{unreal_pct:,.1f}% · {len(live_rows)} open · live",
            tone=tone,
        ))
    else:
        cards.append(_card("Unrealized P/L", "—",
                           "set Alpaca keys for live P/L"))

    generated = datetime.now(_ET).strftime("%Y-%m-%d %H:%M:%S ET")
    return _PAGE.format(
        account_panel=_account_panel(account),
        cards="".join(cards),
        bar=bar,
        area=area,
        round_trips=_round_trips_html(trips, rows),
        table=_table(rows),
        generated=generated,
        css=_CSS,
    )


_CSS = """
*{box-sizing:border-box}
body{margin:0;background:%(bg)s;color:%(ink)s;
  font:14px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif}
.wrap{max-width:1240px;margin:0 auto;padding:32px 20px 64px}
h1{font-size:22px;margin:0 0 2px}
.sub{color:%(mute)s;margin:0 0 24px;font-size:13px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));
  gap:14px;margin-bottom:28px}
.card{background:%(card)s;border:1px solid %(grid)s;border-radius:12px;padding:16px 18px}
.card-lbl{color:%(mute)s;font-size:12px;text-transform:uppercase;letter-spacing:.04em}
.card-val{font-size:26px;font-weight:650;margin-top:4px}
.card-sub{color:%(mute)s;font-size:12px;margin-top:4px}
.section{font-size:13px;color:%(mute)s;text-transform:uppercase;letter-spacing:.04em;
  font-weight:600;margin:8px 0 12px}
.grid2{display:grid;grid-template-columns:1fr 1.4fr;gap:18px;margin-bottom:28px}
@media(max-width:880px){.grid2{grid-template-columns:1fr}}
.panel{background:%(card)s;border:1px solid %(grid)s;border-radius:12px;padding:18px}
.panel h2{font-size:13px;color:%(mute)s;margin:0 0 14px;text-transform:uppercase;
  letter-spacing:.04em;font-weight:600}
.chart{display:block}
.c-lbl{fill:%(mute)s;font-size:11px}
.c-val{fill:%(ink)s;font-size:11px;font-weight:600}
.empty{color:%(mute)s;padding:24px 0;text-align:center}
table{width:100%%;border-collapse:collapse;font-size:13px}
.tablewrap{background:%(card)s;border:1px solid %(grid)s;border-radius:12px;
  overflow:auto}
th,td{padding:11px 12px;text-align:left;border-bottom:1px solid %(grid)s;
  vertical-align:top}
th{color:%(mute)s;font-size:11px;text-transform:uppercase;letter-spacing:.03em;
  position:sticky;top:0;background:%(card)s;white-space:nowrap}
tbody tr:hover{background:rgba(255,255,255,.025)}
.num{text-align:right;font-variant-numeric:tabular-nums}
.nowrap{white-space:nowrap}.mute{color:%(mute)s}
.up{color:%(green)s}.down{color:%(red)s}
.sym{font-weight:650}
.buy-pill,.sell-pill{font-size:11px;font-weight:600;padding:2px 8px;border-radius:20px}
.buy-pill{background:rgba(63,185,80,.15);color:%(green)s}
.sell-pill{background:rgba(248,81,73,.15);color:%(red)s}
.reason{max-width:340px;color:#c9d1d9}
.chips{margin-top:6px;display:flex;flex-wrap:wrap;gap:4px}
.chip{font-size:10px;background:%(grid)s;color:%(mute)s;padding:1px 7px;border-radius:10px}
.conv{height:6px;width:60px;background:%(grid)s;border-radius:4px;overflow:hidden;
  display:inline-block;vertical-align:middle;margin-right:6px}
.conv-bar{height:100%%;background:%(amber)s}
details.rt{background:%(card)s;border:1px solid %(grid)s;border-radius:12px;
  margin-bottom:10px;overflow:hidden}
details.rt summary{display:flex;flex-wrap:wrap;align-items:center;gap:8px 28px;
  padding:14px 18px;cursor:pointer;list-style:none}
details.rt summary::-webkit-details-marker{display:none}
details.rt summary:hover{background:rgba(255,255,255,.025)}
details.rt .tablewrap{border:0;border-top:1px solid %(grid)s;border-radius:0}
.rt-stat{min-width:110px}
.rt-lbl{color:%(mute)s;font-size:10px;text-transform:uppercase;letter-spacing:.04em}
.rt-val{font-size:14px;font-weight:600;margin-top:2px;
  font-variant-numeric:tabular-nums}
.rt-hint{color:%(mute)s;font-size:11px;margin-left:auto}
footer{color:%(mute)s;font-size:12px;margin-top:24px;text-align:center}
""" % {
    "bg": _BG, "card": _CARD, "ink": _INK, "mute": _MUTE, "grid": _GRID,
    "green": _GREEN, "red": _RED, "amber": _AMBER,
}

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Trade Dashboard — Investment Strategy bot</title>
<style>{css}</style></head>
<body><div class="wrap">
<h1>📈 Trade Dashboard</h1>
<p class="sub">Every order the bot has executed — what, when, how much, the planned
exit, and why. Exits are price-triggered brackets (TP/SL), not calendar dates.</p>
{account_panel}
<h2 class="section">Ledger (bot orders)</h2>
<div class="cards">{cards}</div>
<div class="grid2">
  <div class="panel"><h2>Capital deployed per symbol</h2>{bar}</div>
  <div class="panel"><h2>Cumulative invested over time</h2>{area}</div>
</div>
<h2 class="section">By ticker — round trips (money in → money out → P/L; expand for the trades)</h2>
{round_trips}
<div class="panel" style="padding:0;margin-top:28px">
  <h2 style="padding:18px 18px 0">All trades (newest first)</h2>
  <div class="tablewrap">{table}</div>
</div>
<footer>Generated {generated} · source: state/trades.jsonl</footer>
</div></body></html>
"""


def generate(
    out: Path, ledger_path: Optional[Path] = None, live: bool = True,
) -> Path:
    ledger = TradeLedger(ledger_path) if ledger_path else TradeLedger()
    # effective(): reconcile corrections applied — no phantom rows (GA-2.5).
    records = ledger.effective()
    prices, account = _live_enrichment({r.symbol for r in records}) if live else ({}, None)
    out.write_text(build_html(records, prices, account), encoding="utf-8")
    log.info("Wrote dashboard with %d trades -> %s", len(records), out)
    return out


def main() -> int:
    logging.basicConfig(level="INFO", format="%(name)s | %(message)s")
    ap = argparse.ArgumentParser(description="Generate the trade dashboard HTML.")
    ap.add_argument("-o", "--out", default="dashboard.html",
                    help="output HTML path (default: dashboard.html)")
    ap.add_argument("-l", "--ledger", default=None,
                    help="ledger path (default: state/trades.jsonl)")
    ap.add_argument("--no-live", dest="live", action="store_false",
                    help="skip live price enrichment (offline)")
    ap.add_argument("--open", dest="open_", action="store_true",
                    help="open the dashboard in a browser after writing")
    args = ap.parse_args()

    out = generate(
        Path(args.out),
        Path(args.ledger) if args.ledger else None,
        live=args.live,
    )
    print(f"Dashboard written to {out.resolve()}")
    if args.open_:
        webbrowser.open(out.resolve().as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
