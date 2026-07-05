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
from datetime import datetime
from pathlib import Path
from typing import Optional

from .ledger import TradeLedger, TradeRecord
from .status import AccountStatus

log = logging.getLogger("dashboard")

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

    def __init__(self, rec: TradeRecord, price_now: Optional[float]):
        self.rec = rec
        self.price_now = price_now
        self.current_value: Optional[float] = None
        self.unreal_pl: Optional[float] = None
        self.unreal_pct: Optional[float] = None
        if (
            rec.action == "buy" and rec.instrument == "equity"
            and price_now and rec.entry_price > 0 and rec.qty > 0
        ):
            self.current_value = price_now * rec.qty
            self.unreal_pl = self.current_value - rec.entry_price * rec.qty
            self.unreal_pct = (price_now / rec.entry_price - 1.0) * 100.0


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
    return dt.strftime("%Y-%m-%d %H:%M")


def _exit_cell(r: TradeRecord) -> str:
    """The 'assumed sell' analogue: bracket take-profit / stop-loss levels."""
    if r.action != "buy":
        return "<span class='mute'>—</span>"
    tp = (f"TP {r.take_profit_pct:.0f}%"
          + (f" (${r.take_profit_price:,.2f})" if r.take_profit_price else ""))
    sl = (f"SL {r.stop_loss_pct:.0f}%"
          + (f" (${r.stop_loss_price:,.2f})" if r.stop_loss_price else ""))
    return (f"<span class='up'>{tp}</span><br>"
            f"<span class='down'>{sl}</span>")


def _pl_cell(row: _Row) -> str:
    if row.unreal_pct is None:
        return "<span class='mute'>—</span>"
    cls = "up" if row.unreal_pl >= 0 else "down"
    sign = "+" if row.unreal_pl >= 0 else ""
    return (f"<span class='{cls}'>{sign}{row.unreal_pct:,.1f}%<br>"
            f"{sign}${row.unreal_pl:,.0f}</span>")


def _table(rows: list[_Row]) -> str:
    if not rows:
        return ("<p class='empty'>No trades recorded yet. The ledger fills as the "
                "bot executes orders.</p>")
    head = (
        "<tr><th>Executed</th><th>Symbol</th><th>Side</th><th>Type</th>"
        "<th class='num'>Volume</th><th class='num'>Entry</th>"
        "<th class='num'>Cost invested</th><th>Planned exit (TP / SL)</th>"
        "<th class='num'>Profit % assumed</th><th>Live P/L</th>"
        "<th>Conviction</th><th>Reason behind the purchase</th></tr>"
    )
    body = []
    for row in sorted(rows, key=lambda x: x.rec.ts, reverse=True):
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
        body.append(
            "<tr>"
            f"<td class='nowrap'>{_fmt_dt(r.ts)}</td>"
            f"<td class='sym'>{html.escape(r.symbol)}</td>"
            f"<td><span class='{side_cls}'>{r.action.upper()}</span></td>"
            f"<td>{html.escape(r.instrument)}{strat}</td>"
            f"<td class='num'>{r.qty:,.4g}</td>"
            f"<td class='num'>{('$%.2f' % r.entry_price) if r.entry_price else '—'}</td>"
            f"<td class='num'>${r.cost_usd:,.0f}</td>"
            f"<td>{_exit_cell(r)}</td>"
            f"<td class='num up'>{('%.0f%%' % r.take_profit_pct) if r.action == 'buy' and r.take_profit_pct else '—'}</td>"
            f"<td class='num'>{_pl_cell(row)}</td>"
            f"<td>{conv}</td>"
            f"<td class='reason'>{html.escape(r.rationale) or '<span class=\"mute\">—</span>'}{signals}</td>"
            "</tr>"
        )
    return f"<table><thead>{head}</thead><tbody>{''.join(body)}</tbody></table>"


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
    rows = [_Row(r, prices.get(r.symbol)) for r in records]
    buys = [r for r in records if r.action == "buy"]

    total_invested = sum(r.cost_usd for r in buys)
    symbols = sorted({r.symbol for r in records})
    n_buys, n_sells = len(buys), sum(1 for r in records if r.action == "sell")

    live_rows = [r for r in rows if r.unreal_pl is not None]
    total_unreal = sum(r.unreal_pl for r in live_rows) if live_rows else None
    cur_basis = sum(r.rec.entry_price * r.rec.qty for r in live_rows) or 1.0
    unreal_pct = (total_unreal / cur_basis * 100.0) if total_unreal is not None else None

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

    generated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return _PAGE.format(
        account_panel=_account_panel(account),
        cards="".join(cards),
        bar=bar,
        area=area,
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
<div class="panel" style="padding:0">
  <h2 style="padding:18px 18px 0">All trades</h2>
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
