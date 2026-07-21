"""Track-record page (goGA GA-1.2) — the public, honest performance record.

Auto-generates a self-contained HTML page from the persisted equity history and
the corrected ledger:

  - Equity curve vs THREE benchmark lines: QQQ, SPY, and QQQ with a naive 15%
    trailing stop. The third is the honest competitor for the "seatbelt" story —
    if a dumb trailing stop on the index matches us, our machinery adds nothing —
    and it also SURFACES the measured seatbelt cost (whipsaw exits), rather than
    hiding it.
  - Headline stats for all four lines over the same window (return, max DD).
  - Per-source signal attribution (closed round-trips, win rate, avg P&L).
  - Realized trades with FIFO lot basis + wash-sale flags (lots.py, GA-2.5).
  - The config-change log (state/config_changes.jsonl, one JSON object per line:
    {"ts": ..., "change": ..., "why": ...}) — the GA-1.1 freeze policy says every
    record-account change is logged HERE, visibly.

HONESTY RULE (from goGA): the page shows the live numbers WHATEVER they are.
The mandatory disclaimers are BAKED INTO the template below — they cannot be
omitted by any caller, and there is deliberately no parameter to remove them.

    python -m investment_strategy.track_record                # track_record.html
    python -m investment_strategy.track_record -o page.html --no-live
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import statistics
from datetime import datetime
from pathlib import Path
from typing import Optional

from .attribution import attribute, round_trips
from .ledger import TradeLedger
from .lots import RealizedLot, build_lot_history

log = logging.getLogger("track_record")

DEFAULT_CONFIG_CHANGES_PATH = Path("state") / "config_changes.jsonl"

#: The naive competitor's trailing stop width. Fixed by design — this is a
#: published benchmark definition, not a tunable knob.
TRAIL_PCT = 15.0

# Palette — matches dashboard.py so the two pages read as one product.
_BG = "#0f1419"
_CARD = "#1a2027"
_INK = "#e6edf3"
_MUTE = "#8b949e"
_GRID = "#2d333b"
_GREEN = "#3fb950"
_RED = "#f85149"
_BLUE = "#58a6ff"
_AMBER = "#d29922"
_PURPLE = "#bc8cff"

_LINE_COLORS = {
    "Bot": _BLUE,
    "QQQ": _AMBER,
    "SPY": _MUTE,
    f"QQQ + {TRAIL_PCT:.0f}% trail": _PURPLE,
}


# --------------------------------------------------------------------------- #
# Series math
# --------------------------------------------------------------------------- #
def trailing_stop_series(
    closes: list[float], trail_pct: float = TRAIL_PCT,
) -> tuple[list[float], int]:
    """The naive seatbelt benchmark: hold the asset; exit at the close that sits
    `trail_pct` below the running peak; re-enter at the first close ABOVE the
    peak that forced the exit. Deterministic and fully stated so the comparison
    can't be quietly re-tuned. Returns (growth-of-1 series, exit count — each
    exit is a potential whipsaw, i.e. the measurable seatbelt cost)."""
    if not closes:
        return [], 0
    series = [1.0]
    in_market = True
    peak = closes[0]
    exit_peak = 0.0
    exits = 0
    for i in range(1, len(closes)):
        px, prev = closes[i], closes[i - 1]
        if in_market and prev > 0:
            series.append(series[-1] * px / prev)
            peak = max(peak, px)
            if px <= peak * (1.0 - trail_pct / 100.0):
                in_market = False
                exit_peak = peak
                exits += 1
        else:
            series.append(series[-1])
            if not in_market and px > exit_peak:
                in_market = True
                peak = px
    return series, exits


def _max_drawdown_pct(series: list[float]) -> float:
    peak, worst = 0.0, 0.0
    for v in series:
        peak = max(peak, v)
        if peak > 0:
            worst = max(worst, (peak - v) / peak * 100.0)
    return worst


def _return_pct(series: list[float]) -> float:
    if len(series) < 2 or series[0] <= 0:
        return 0.0
    return (series[-1] / series[0] - 1.0) * 100.0


_TRADING_DAYS = 252
# Below this many daily returns an annualized Sharpe/Sortino is dominated by
# noise — a fresh paper account has only a handful of days, so we WITHHOLD the
# number rather than print a misleadingly precise ratio. It appears on its own
# once enough history accrues.
_MIN_RATIO_DAYS = 20


def _daily_returns(series: list[float]) -> list[float]:
    return [
        series[i] / series[i - 1] - 1.0
        for i in range(1, len(series))
        if series[i - 1] > 0
    ]


def _sharpe(series: list[float]) -> Optional[float]:
    """Annualized Sharpe of the daily equity curve (risk-free = 0). None until
    there are _MIN_RATIO_DAYS returns — below that it is noise. Same math as the
    offline backtest harness, applied to the live curve."""
    rets = _daily_returns(series)
    if len(rets) < _MIN_RATIO_DAYS:
        return None
    sd = statistics.pstdev(rets)
    if sd == 0:
        return None
    return statistics.fmean(rets) / sd * (_TRADING_DAYS ** 0.5)


def _sortino(series: list[float]) -> Optional[float]:
    """Annualized Sortino — like Sharpe but the denominator is downside
    deviation vs a 0 target (penalizes losses, not upside vol), measured over
    ALL periods. None below the min sample or with no downside days."""
    rets = _daily_returns(series)
    if len(rets) < _MIN_RATIO_DAYS:
        return None
    neg_sq = [r * r for r in rets if r < 0]
    if not neg_sq:
        return None
    dd = (sum(neg_sq) / len(rets)) ** 0.5
    if dd == 0:
        return None
    return statistics.fmean(rets) / dd * (_TRADING_DAYS ** 0.5)


def _align_benchmark(
    bench: list[tuple[str, float]], dates: list[str],
) -> Optional[list[float]]:
    """Benchmark closes carried onto the equity curve's dates (last close at or
    before each date — weekends/holidays in the equity history hold the prior
    close). None when there's no overlap to compare."""
    if not bench or not dates:
        return None
    out: list[float] = []
    j = -1
    for d in dates:
        while j + 1 < len(bench) and bench[j + 1][0] <= d:
            j += 1
        if j < 0:
            return None  # equity history starts before the benchmark data
        out.append(bench[j][1])
    return out


# --------------------------------------------------------------------------- #
# Data assembly
# --------------------------------------------------------------------------- #
def _benchmark_series(dates: list[str]) -> dict[str, list[float]]:
    """Fetch QQQ/SPY closes (Alpaca) and derive the three benchmark lines as
    growth-of-1 over the equity curve's dates. {} offline / on failure — the
    page still renders the bot's own record."""
    try:
        from .config import load_config
        from .execution import AlpacaClient

        broker = AlpacaClient(load_config())
        span = max(30, len(dates) + 40)  # buffer for non-trading days
        out: dict[str, list[float]] = {}
        qqq_aligned: Optional[list[float]] = None
        for sym in ("QQQ", "SPY"):
            series = broker.daily_close_series(sym, span)
            series = [(d, c) for d, c in series if d <= dates[-1]]
            aligned = _align_benchmark(series, dates)
            if aligned and aligned[0] > 0:
                out[sym] = [c / aligned[0] for c in aligned]
                if sym == "QQQ":
                    qqq_aligned = aligned
        if qqq_aligned:
            trail, exits = trailing_stop_series(qqq_aligned)
            out[f"QQQ + {TRAIL_PCT:.0f}% trail"] = trail
            out["_trail_exits"] = [float(exits)]
        return out
    except Exception as e:
        log.info("Benchmark series unavailable (%s) — rendering without.", e)
        return {}


def _config_changes(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _multi_line_chart(series: dict[str, list[float]], dates: list[str]) -> str:
    """All lines share the x axis (the equity curve's dates) and start at 1.0."""
    drawable = {k: v for k, v in series.items() if len(v) >= 2}
    if not drawable:
        return "<p class='empty'>Not enough history yet for a curve.</p>"
    w, h, pad = 760, 300, 42
    lo = min(min(v) for v in drawable.values())
    hi = max(max(v) for v in drawable.values())
    span = (hi - lo) or 1.0
    n = max(len(v) for v in drawable.values())

    def px(i: int) -> float:
        return pad + i / max(1, n - 1) * (w - pad * 2)

    def py(v: float) -> float:
        return h - pad - (v - lo) / span * (h - pad * 2)

    parts = [f"<svg viewBox='0 0 {w} {h}' width='100%' role='img' class='chart'>"]
    for frac in (0.0, 0.5, 1.0):
        v = lo + span * frac
        gy = py(v)
        parts.append(
            f"<line x1='{pad}' y1='{gy:.1f}' x2='{w - pad}' y2='{gy:.1f}' "
            f"stroke='{_GRID}' stroke-width='1' />"
            f"<text x='{pad - 6}' y='{gy + 4:.1f}' text-anchor='end' "
            f"class='c-lbl'>{(v - 1) * 100:+.0f}%</text>"
        )
    for label, vals in drawable.items():
        color = _LINE_COLORS.get(label, _GREEN)
        pts = " ".join(f"{px(i):.1f},{py(v):.1f}" for i, v in enumerate(vals))
        parts.append(
            f"<polyline points='{pts}' fill='none' stroke='{color}' "
            f"stroke-width='{2.5 if label == 'Bot' else 1.6}' />"
        )
    if dates:
        parts.append(
            f"<text x='{pad}' y='{h - 10}' class='c-lbl'>{dates[0]}</text>"
            f"<text x='{w - pad}' y='{h - 10}' text-anchor='end' "
            f"class='c-lbl'>{dates[-1]}</text>"
        )
    parts.append("</svg>")
    legend = "".join(
        f"<span class='leg'><span class='dot' style='background:"
        f"{_LINE_COLORS.get(k, _GREEN)}'></span>{html.escape(k)}</span>"
        for k in drawable
    )
    return f"<div class='legend'>{legend}</div>" + "".join(parts)


def _stat_cards(series: dict[str, list[float]], trail_exits: int) -> str:
    cards = []
    for label, vals in series.items():
        ret = _return_pct(vals)
        dd = _max_drawdown_pct(vals)
        sub = f"max DD {dd:.1f}%"
        # Risk-adjusted return, once enough daily history exists to be meaningful
        # (withheld during the noisy first weeks of a fresh account).
        sharpe = _sharpe(vals)
        if sharpe is not None:
            sub += f" · Sharpe {sharpe:.2f}"
            sortino = _sortino(vals)
            if sortino is not None:
                sub += f" · Sortino {sortino:.2f}"
        if label.endswith("trail") and trail_exits:
            sub += f" · {trail_exits} stop exit(s) — the seatbelt cost"
        cards.append(
            f"<div class='card'><div class='card-lbl'>{html.escape(label)}</div>"
            f"<div class='card-val' style='color:{_GREEN if ret >= 0 else _RED}'>"
            f"{ret:+.2f}%</div>"
            f"<div class='card-sub'>{html.escape(sub)}</div></div>"
        )
    return "".join(cards)


def _attribution_table(records) -> str:
    trips = round_trips(records)
    if not trips:
        return "<p class='empty'>No closed round-trips yet.</p>"
    stats = sorted(
        attribute(trips).values(), key=lambda s: s.avg_pl_pct, reverse=True,
    )
    rows = "".join(
        f"<tr><td>{html.escape(s.source)}</td>"
        f"<td class='num'>{s.trips}</td>"
        f"<td class='num'>{s.win_rate * 100:.0f}%</td>"
        f"<td class='num {'up' if s.avg_pl_pct >= 0 else 'down'}'>"
        f"{s.avg_pl_pct:+.1f}%</td></tr>"
        for s in stats
    )
    note = (
        "<p class='mute'>A source with under 2 round-trips in the window is "
        "INSUFFICIENT (per-source rule, GA-1.1) — small samples, not verdicts.</p>"
    )
    return (
        "<table><thead><tr><th>Signal source</th><th class='num'>Round-trips"
        "</th><th class='num'>Win rate</th><th class='num'>Avg realized P&L"
        f"</th></tr></thead><tbody>{rows}</tbody></table>{note}"
    )


def _realized_table(realized: list[RealizedLot]) -> str:
    if not realized:
        return "<p class='empty'>No realized (closed) lots yet.</p>"
    rows = []
    for r in sorted(realized, key=lambda x: x.exit_ts, reverse=True):
        cls = "up" if r.pl_usd >= 0 else "down"
        flags = []
        if r.wash_sale:
            flags.append("wash-sale?")
        if r.basis_estimated:
            flags.append("est. basis")
        rows.append(
            f"<tr><td class='nowrap'>{r.exit_ts.strftime('%Y-%m-%d')}</td>"
            f"<td class='sym'>{html.escape(r.symbol)}</td>"
            f"<td class='num'>{r.qty:,.4g}</td>"
            f"<td class='num'>${r.entry_price:,.2f}</td>"
            f"<td class='num'>${r.exit_price:,.2f}</td>"
            f"<td class='num {cls}'>{r.pl_pct:+.1f}% (${r.pl_usd:+,.0f})</td>"
            f"<td>{html.escape(r.exit_reason or '—')}</td>"
            f"<td class='mute'>{html.escape(', '.join(flags) or '—')}</td></tr>"
        )
    return (
        "<table><thead><tr><th>Closed</th><th>Symbol</th><th class='num'>Qty"
        "</th><th class='num'>FIFO basis</th><th class='num'>Exit</th>"
        "<th class='num'>Realized P&L</th><th>Exit path</th><th>Flags</th>"
        f"</tr></thead><tbody>{''.join(rows)}</tbody></table>"
        "<p class='mute'>Basis is per-lot FIFO (GA-2.5). “wash-sale?” "
        "= a loss with a same-symbol buy within ±30 days — a flag for the tax "
        "notes, not tax advice; the broker's 1099-B is the source of truth.</p>"
    )


def _changes_section(changes: list[dict]) -> str:
    if not changes:
        return ("<p class='empty'>No config changes recorded this window "
                "(frozen-config policy, GA-1.1).</p>")
    rows = "".join(
        f"<tr><td class='nowrap'>{html.escape(str(c.get('ts', '?'))[:10])}</td>"
        f"<td>{html.escape(str(c.get('change', '')))}</td>"
        f"<td class='mute'>{html.escape(str(c.get('why', '')))}</td></tr>"
        for c in changes
    )
    return ("<table><thead><tr><th>Date</th><th>Change</th><th>Why</th></tr>"
            f"</thead><tbody>{rows}</tbody></table>")


# The disclaimers are part of the template string itself — no caller can render
# this page without them (goGA GA-1.2: "baked into the generator").
_DISCLAIMER_HTML = """
<div class="disclaimer">
<strong>Required disclosures — read before reading any number above.</strong>
<ul>
<li><strong>Paper trading.</strong> All results on this page are from simulated
(paper) trading unless a line is explicitly labeled live. Simulated results do
not reflect real execution: slippage, liquidity, partial fills, and borrow
constraints are approximated or absent.</li>
<li><strong>Hypothetical performance.</strong> Hypothetical and backtested
results have inherent limitations and are frequently prepared with the benefit
of hindsight. No representation is made that any account will or is likely to
achieve results similar to those shown.</li>
<li><strong>Past performance, real or simulated,
does not guarantee future results.</strong> The strategy's edge is unproven;
losing money is a real outcome.</li>
<li><strong>Not investment advice.</strong> Nothing on this page is a
recommendation or an offer to buy or sell any security.</li>
<li><strong>The seatbelt has a cost.</strong> The risk machinery (stops,
daily-loss flatten, drawdown halts) measurably drags performance in normal
years (whipsaw exits are shown above, not hidden), in exchange for bounding the
bad ones.</li>
</ul>
</div>
"""

_CSS = """
*{box-sizing:border-box}
body{margin:0;background:%(bg)s;color:%(ink)s;
  font:14px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:32px 20px 64px}
h1{font-size:22px;margin:0 0 2px}
.sub{color:%(mute)s;margin:0 0 24px;font-size:13px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));
  gap:14px;margin-bottom:24px}
.card{background:%(card)s;border:1px solid %(grid)s;border-radius:12px;padding:14px 16px}
.card-lbl{color:%(mute)s;font-size:12px;text-transform:uppercase;letter-spacing:.04em}
.card-val{font-size:24px;font-weight:650;margin-top:4px}
.card-sub{color:%(mute)s;font-size:12px;margin-top:4px}
.panel{background:%(card)s;border:1px solid %(grid)s;border-radius:12px;
  padding:18px;margin-bottom:22px}
.panel h2{font-size:13px;color:%(mute)s;margin:0 0 12px;text-transform:uppercase;
  letter-spacing:.04em;font-weight:600}
.chart{display:block}
.c-lbl{fill:%(mute)s;font-size:11px}
.legend{display:flex;gap:16px;margin-bottom:8px;flex-wrap:wrap}
.leg{color:%(mute)s;font-size:12px;display:flex;align-items:center;gap:6px}
.dot{width:10px;height:10px;border-radius:5px;display:inline-block}
.empty{color:%(mute)s;padding:18px 0;text-align:center}
table{width:100%%;border-collapse:collapse;font-size:13px}
th,td{padding:9px 10px;text-align:left;border-bottom:1px solid %(grid)s}
th{color:%(mute)s;font-size:11px;text-transform:uppercase;letter-spacing:.03em}
.num{text-align:right;font-variant-numeric:tabular-nums}
.nowrap{white-space:nowrap}.mute{color:%(mute)s;font-size:12px}
.up{color:%(green)s}.down{color:%(red)s}
.sym{font-weight:650}
.disclaimer{background:rgba(210,153,34,.08);border:1px solid %(amber)s;
  border-radius:12px;padding:16px 20px;font-size:13px;margin-top:8px}
.disclaimer ul{margin:8px 0 0;padding-left:18px}
.disclaimer li{margin-bottom:6px}
footer{color:%(mute)s;font-size:12px;margin-top:24px;text-align:center}
""" % {
    "bg": _BG, "card": _CARD, "ink": _INK, "mute": _MUTE, "grid": _GRID,
    "green": _GREEN, "red": _RED, "amber": _AMBER,
}

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Track record — paper trading, hypothetical results</title>
<style>{css}</style></head>
<body><div class="wrap">
<h1>Track record</h1>
<p class="sub">PAPER TRADING — HYPOTHETICAL RESULTS (see required disclosures
below). Auto-generated from the account's equity history and the corrected
trade ledger; the numbers are shown whatever they are. Benchmark lines: QQQ,
SPY, and QQQ with a naive {trail:.0f}% trailing stop (exit {trail:.0f}% below
the running peak, re-enter above the prior peak) — the honest competitor for
the risk-machinery story.</p>
<div class="cards">{stat_cards}</div>
<div class="panel"><h2>Equity curve vs benchmarks (growth of $1)</h2>{chart}</div>
<div class="panel"><h2>Per-source signal attribution (closed round-trips)</h2>{attribution}</div>
<div class="panel"><h2>Realized trades (FIFO lots)</h2>{realized}</div>
<div class="panel"><h2>Config changes this window</h2>{changes}</div>
{disclaimer}
<footer>Generated {generated} · sources: state/equity_history.jsonl,
state/trades.jsonl (corrections applied), state/config_changes.jsonl</footer>
</div></body></html>
"""


def build_html(
    equity_rows: list[dict], records, realized: list[RealizedLot],
    benchmarks: dict[str, list[float]], changes: list[dict],
) -> str:
    dates = [r["date"] for r in equity_rows]
    equities = [float(r.get("equity") or 0.0) for r in equity_rows]

    series: dict[str, list[float]] = {}
    if len(equities) >= 2 and equities[0] > 0:
        series["Bot"] = [e / equities[0] for e in equities]
    trail_exits = int(benchmarks.pop("_trail_exits", [0])[0]) if benchmarks else 0
    series.update(benchmarks)

    stat_cards = _stat_cards(series, trail_exits) or (
        "<div class='card'><div class='card-lbl'>Equity history</div>"
        "<div class='card-val'>—</div>"
        "<div class='card-sub'>needs >= 2 daily snapshots</div></div>"
    )
    return _PAGE.format(
        css=_CSS,
        trail=TRAIL_PCT,
        stat_cards=stat_cards,
        chart=_multi_line_chart(series, dates),
        attribution=_attribution_table(records),
        realized=_realized_table(realized),
        changes=_changes_section(changes),
        disclaimer=_DISCLAIMER_HTML,
        generated=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )


def generate(
    out: Path, ledger_path: Optional[Path] = None,
    equity_path: Optional[Path] = None, live: bool = True,
    config_changes_path: Path = DEFAULT_CONFIG_CHANGES_PATH,
) -> Path:
    from .status import EquityHistory

    ledger = TradeLedger(ledger_path) if ledger_path else TradeLedger()
    records = ledger.effective()
    _, realized = build_lot_history(records)
    history = EquityHistory(equity_path) if equity_path else EquityHistory()
    equity_rows = history.all()
    dates = [r["date"] for r in equity_rows]
    benchmarks = _benchmark_series(dates) if (live and dates) else {}
    out.write_text(
        build_html(equity_rows, records, realized, benchmarks,
                   _config_changes(config_changes_path)),
        encoding="utf-8",
    )
    log.info("Wrote track record (%d equity days, %d realized lots) -> %s",
             len(equity_rows), len(realized), out)
    return out


def main() -> int:
    logging.basicConfig(level="INFO", format="%(name)s | %(message)s")
    ap = argparse.ArgumentParser(description="Generate the track-record HTML page.")
    ap.add_argument("-o", "--out", default="track_record.html")
    ap.add_argument("-l", "--ledger", default=None,
                    help="ledger path (default: state/trades.jsonl)")
    ap.add_argument("--no-live", dest="live", action="store_false",
                    help="skip benchmark fetches (offline)")
    args = ap.parse_args()
    out = generate(
        Path(args.out), Path(args.ledger) if args.ledger else None,
        live=args.live,
    )
    print(f"Track record written to {out.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
