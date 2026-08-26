#!/usr/bin/env python3
"""Standing signal information-coefficient (IC) harness — run-6 item 4d.

Joins the bot's per-(symbol, kind) score history (state/signal_history.json,
plus any archived copies under runs/*/state/) to daily closes and asks, per
signal kind and forward horizon: did a higher score predict a higher forward
excess return (vs SPY)?

Method (the statistics the Aug-25 review asked for):
  * one observation per (date, symbol, kind) = the LAST score recorded that
    session (a point stamped after the 16:00 ET close belongs to the NEXT
    session's close);
  * forward h-trading-day return from the entry close, minus SPY's, for
    h in 1/3/5/10/20; entry close must be >= $5 (sub-$5 names are the
    slate's junk tail); returns winsorized at +/-25%;
  * per-date cross-sectional Spearman IC (>= 8 names on the date);
  * NAIVE t across all dates (overlapping windows: consecutive h-day returns
    share bars, so this t is inflated) AND a NON-OVERLAPPING t that only
    uses dates spaced >= h trading days apart (greedy from the first date);
  * a circular block-bootstrap p-value (block length = h) for mean IC != 0
    over the full date series, which respects the overlap without discarding
    dates.

Pure python + numpy; the only network read is the repo's own
AlpacaClient.daily_close_series (read-only market data), and --prices-json
caches it so re-runs are offline. Prints a markdown table and writes JSON.

Usage:
  .venv/bin/python scripts/signal_ic.py                       # live prices
  .venv/bin/python scripts/signal_ic.py --prices-json state/ic_prices.json
  .venv/bin/python scripts/signal_ic.py --prices-json state/ic_prices.json --offline
  .venv/bin/python scripts/signal_ic.py --history state/signal_history.json \
      --history runs/pre-final-test-run-4/state/signal_history.json \
      --out state/signal_ic.json

Do NOT re-weight the composite on this table until it shows >= 60 dates
per kind (the review's precondition); the script prints that gate.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import glob
import json
import math
import os
import sys
from typing import Iterable

import numpy as np

HORIZONS = (1, 3, 5, 10, 20)
MIN_NAMES_PER_DATE = 8
MIN_ENTRY_PRICE = 5.0
WINSOR = 0.25
MIN_DATES_FOR_REWEIGHT = 60
BOOT_DRAWS = 2000
MIN_DATES_FOR_BOOT = 10   # a bootstrap over 2-3 dates prints p=0.0; not a p-value
BENCH = "SPY"


# ---------------------------------------------------------------- history
def load_history(paths: Iterable[str]) -> list[tuple[str, str, str, float]]:
    """(symbol, kind, entry_date, score) rows. Later files override earlier
    ones for the same timestamp; a point after 20:00 UTC (16:00 ET) maps to
    the next calendar day (the next session's close)."""
    series: dict[tuple[str, str], dict[str, float]] = collections.defaultdict(dict)
    for f in paths:
        try:
            with open(f, encoding="utf-8") as fh:
                d = json.load(fh).get("series", {})
        except (OSError, ValueError) as e:
            print(f"skip {f}: {e}", file=sys.stderr)
            continue
        for sym, kinds in d.items():
            for kind, pts in kinds.items():
                for ts, sc in pts:
                    series[(sym.upper(), kind)][ts] = float(sc)
    rows = []
    for (sym, kind), pts in series.items():
        for ts, sc in sorted(pts.items()):
            rows.append((sym, kind, entry_date(ts), sc))
    return rows


def entry_date(ts: str) -> str:
    t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if t.tzinfo is not None:
        t = t.astimezone(dt.timezone.utc)
    d = t.date()
    if t.hour >= 20:
        d = d + dt.timedelta(days=1)
    return d.isoformat()


def last_score_per_day(rows) -> dict[tuple[str, str, str], float]:
    """(date, symbol, kind) -> last score of that session."""
    last: dict[tuple[str, str, str], float] = {}
    for sym, kind, d, sc in rows:  # rows are ts-ordered within a series
        last[(d, sym, kind)] = sc
    return last


# ---------------------------------------------------------------- prices
def fetch_prices(symbols: Iterable[str], days: int, cache: str | None,
                 offline: bool = False) -> dict[str, dict[str, float]]:
    px: dict[str, dict[str, float]] = {}
    if cache and os.path.exists(cache):
        with open(cache, encoding="utf-8") as fh:
            px = json.load(fh)
    missing = sorted(s for s in set(symbols) | {BENCH} if s not in px)
    if missing and offline:
        print(f"offline: {len(missing)} symbols not in cache are skipped", file=sys.stderr)
        missing = []
    if missing:
        from investment_strategy.config import load_config
        from investment_strategy.execution.alpaca_client import AlpacaClient
        broker = AlpacaClient(load_config())
        for i, s in enumerate(missing):
            try:
                pairs = broker.daily_close_series(s, days)
            except Exception as e:  # noqa: BLE001 — one bad symbol must not stop the run
                print(f"price read failed {s}: {e}", file=sys.stderr)
                pairs = []
            if len(pairs) >= 2:
                px[s] = dict(pairs)
            if i % 50 == 0:
                print(f"  prices {i}/{len(missing)}", flush=True, file=sys.stderr)
        if cache:
            with open(cache, "w", encoding="utf-8") as fh:
                json.dump(px, fh)
    return px


def fwd_excess(px: dict, cal: list[str], bench: dict, sym: str, date: str, h: int,
               min_price: float = MIN_ENTRY_PRICE,
               winsor: float = WINSOR):
    """Forward h-bar excess return of `sym` from the first close on/after
    `date`, minus the benchmark's over the same bars; None when unavailable,
    when the entry close is under `min_price`, or when the bars don't exist.
    `cal` is the benchmark's sorted trading calendar."""
    s = px.get(sym)
    if not s:
        return None
    i = int(np.searchsorted(cal, date))
    if i >= len(cal) or i + h >= len(cal):
        return None
    d0, d1 = cal[i], cal[i + h]
    if d0 not in s or d1 not in s or s[d0] < min_price or s[d0] <= 0:
        return None
    r = s[d1] / s[d0] - 1.0
    r = max(-winsor, min(winsor, r))
    rb = bench[d1] / bench[d0] - 1.0
    return r - rb


# ---------------------------------------------------------------- stats
def _rank(a: np.ndarray) -> np.ndarray:
    """Average-rank (ties share the mean rank)."""
    order = np.argsort(a, kind="mergesort")
    ranks = np.empty(len(a), float)
    sa = a[order]
    i = 0
    while i < len(a):
        j = i
        while j + 1 < len(a) and sa[j + 1] == sa[i]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2.0 + 1.0
        i = j + 1
    return ranks


def spearman(x, y) -> float | None:
    x = np.asarray(x, float)
    y = np.asarray(y, float)
    if len(x) < 3 or len(x) != len(y):
        return None
    rx, ry = _rank(x), _rank(y)
    if rx.std() == 0 or ry.std() == 0:
        return None
    return float(np.corrcoef(rx, ry)[0, 1])


def per_date_ic(per_date: dict[str, list[tuple[float, float]]],
                min_names: int = MIN_NAMES_PER_DATE) -> list[tuple[str, float]]:
    """[(date, IC)] sorted by date for dates with >= min_names pairs."""
    out = []
    for d in sorted(per_date):
        pairs = per_date[d]
        if len(pairs) < min_names:
            continue
        ic = spearman([p[0] for p in pairs], [p[1] for p in pairs])
        if ic is not None and not math.isnan(ic):
            out.append((d, ic))
    return out


def t_stat(vals) -> float | None:
    a = np.asarray(vals, float)
    if len(a) < 2:
        return None
    sd = a.std(ddof=1)
    if sd == 0:
        return None
    return float(a.mean() / (sd / math.sqrt(len(a))))


def nonoverlap_dates(dates: list[str], cal: list[str], h: int) -> list[str]:
    """Greedy subset of `dates` (ISO, sorted) spaced >= h trading days apart
    on calendar `cal`, so consecutive h-day forward windows don't share bars."""
    idx = {d: i for i, d in enumerate(cal)}
    keep: list[str] = []
    last_i = None
    for d in sorted(dates):
        i = idx.get(d)
        if i is None:
            i = int(np.searchsorted(cal, d))
        if last_i is None or i - last_i >= h:
            keep.append(d)
            last_i = i
    return keep


def block_bootstrap_p(ics, block: int, draws: int = BOOT_DRAWS, seed: int = 0) -> float | None:
    """Two-sided p-value for mean(IC) != 0 by circular block bootstrap of the
    DEMEANED series (block length = horizon, so overlapping-window
    autocorrelation is preserved inside blocks)."""
    a = np.asarray(ics, float)
    n = len(a)
    if n < MIN_DATES_FOR_BOOT:
        return None
    block = max(1, min(block, n))
    obs = abs(a.mean())
    centered = a - a.mean()
    rng = np.random.default_rng(seed)
    nblocks = int(math.ceil(n / block))
    starts = rng.integers(0, n, size=(draws, nblocks))
    offs = np.arange(block)
    idx = (starts[:, :, None] + offs[None, None, :]).reshape(draws, -1)[:, :n] % n
    means = centered[idx].mean(axis=1)
    return float((np.abs(means) >= obs - 1e-15).mean())


def summarize(ic_series: list[tuple[str, float]], cal: list[str], h: int,
              draws: int = BOOT_DRAWS) -> dict:
    if not ic_series:
        return {"n_dates": 0}
    dates = [d for d, _ in ic_series]
    ics = np.array([v for _, v in ic_series])
    nov = set(nonoverlap_dates(dates, cal, h))
    nov_ics = [v for d, v in ic_series if d in nov]
    t_naive = t_stat(ics)
    t_nov = t_stat(nov_ics)
    return {
        "n_dates": int(len(ics)),
        "mean_ic": round(float(ics.mean()), 4),
        "hit_rate": round(float((ics > 0).mean()), 2),
        "t_naive": None if t_naive is None else round(t_naive, 2),
        "n_nonoverlap": len(nov_ics),
        "t_nonoverlap": None if t_nov is None else round(t_nov, 2),
        "p_block_boot": (lambda p: None if p is None else round(p, 3))(
            block_bootstrap_p(ics, h, draws)),
        "reweight_ok": bool(len(ics) >= MIN_DATES_FOR_REWEIGHT),
    }


def run_study(last: dict[tuple[str, str, str], float], px: dict,
              horizons=HORIZONS, draws: int = BOOT_DRAWS) -> dict:
    bench = px[BENCH]
    cal = sorted(bench)
    kinds = sorted({k[2] for k in last})
    out: dict = {"meta": {"bench": BENCH, "cal": [cal[0], cal[-1]],
                          "obs": len(last), "kinds": kinds,
                          "horizons": list(horizons),
                          "min_entry_price": MIN_ENTRY_PRICE, "winsor": WINSOR,
                          "min_dates_for_reweight": MIN_DATES_FOR_REWEIGHT},
                 "kinds": {}}
    for kind in kinds:
        out["kinds"][kind] = {}
        for h in horizons:
            per_date: dict[str, list[tuple[float, float]]] = collections.defaultdict(list)
            for (d, sym, k), sc in last.items():
                if k != kind:
                    continue
                fx = fwd_excess(px, cal, bench, sym, d, h)
                if fx is None:
                    continue
                per_date[d].append((sc, fx))
            out["kinds"][kind][f"h{h}"] = summarize(per_date_ic(per_date), cal, h, draws)
    return out


def render_markdown(res: dict) -> str:
    m = res["meta"]
    L = ["# Signal IC harness", "",
         f"obs {m['obs']} | kinds {len(m['kinds'])} | {m['bench']} calendar "
         f"{m['cal'][0]}..{m['cal'][1]} | entry close >= ${m['min_entry_price']:.0f} | "
         f"returns winsorized +/-{m['winsor']:.0%}", "",
         "IC = per-date cross-sectional Spearman(score, fwd excess return vs SPY). "
         "t_naive uses every date (overlapping windows, inflated); t_nov uses dates "
         f"spaced >= h apart; p = block bootstrap (block=h). Re-weighting the composite "
         f"needs >= {m['min_dates_for_reweight']} dates (the 'ok' column).", "",
         "| kind | h | dates | mean IC | hit | t_naive | n_nov | t_nov | p_boot | ok |",
         "|---|---|---|---|---|---|---|---|---|---|"]
    for kind, hs in res["kinds"].items():
        for hk, r in hs.items():
            if r.get("n_dates", 0) == 0:
                L.append(f"| {kind} | {hk[1:]} | 0 | - | - | - | - | - | - | no |")
                continue
            L.append(
                f"| {kind} | {hk[1:]} | {r['n_dates']} | {r['mean_ic']:+.3f} | {r['hit_rate']:.2f} | "
                f"{r['t_naive'] if r['t_naive'] is not None else '-'} | {r['n_nonoverlap']} | "
                f"{r['t_nonoverlap'] if r['t_nonoverlap'] is not None else '-'} | "
                f"{r['p_block_boot'] if r['p_block_boot'] is not None else '-'} | "
                f"{'yes' if r['reweight_ok'] else 'no'} |")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------- main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--history", action="append", default=None,
                    help="signal_history.json path (repeatable). Default: state/signal_history.json "
                         "+ every runs/*/state/signal_history.json")
    ap.add_argument("--prices-json", default=None, help="price cache (read if present, written after fetch)")
    ap.add_argument("--days", type=int, default=250, help="daily bars to pull per symbol")
    ap.add_argument("--out", default=os.path.join("state", "signal_ic.json"))
    ap.add_argument("--draws", type=int, default=BOOT_DRAWS)
    ap.add_argument("--offline", action="store_true",
                    help="never touch the network: use --prices-json only, skip uncached symbols")
    args = ap.parse_args(argv)

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, repo)
    os.chdir(repo)
    paths = args.history or (sorted(glob.glob(os.path.join("runs", "*", "state", "signal_history.json")))
                             + [os.path.join("state", "signal_history.json")])
    rows = load_history(paths)
    if not rows:
        print("no signal history rows found", file=sys.stderr)
        return 2
    last = last_score_per_day(rows)
    syms = {k[1] for k in last}
    print(f"history rows {len(rows)} -> {len(last)} (date,symbol,kind) obs over {len(syms)} symbols",
          file=sys.stderr)
    px = fetch_prices(syms, args.days, args.prices_json, offline=args.offline)
    if BENCH not in px:
        print(f"no {BENCH} prices — cannot compute excess returns", file=sys.stderr)
        return 2
    res = run_study(last, px, draws=args.draws)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(res, fh, indent=1)
    print(render_markdown(res))
    print(f"wrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
