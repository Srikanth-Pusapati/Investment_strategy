#!/usr/bin/env python3
"""Standing signal information-coefficient (IC) harness — run-6 item 4d,
run-7 item B4 (calibrated p-value + the two run-8 pre-registered controls).

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
  * p_nov = two-sided Student-t p-value of the non-overlapping t with
    df = n_nov - 1 (pure-python incomplete beta; scipy is not in the venv).
    This is the CALIBRATED column: simulated false-positive rate at nominal
    5% under pure overlapping-window noise is 5-7% for h <= 5 (run-7 B4);
  * a circular block-bootstrap p-value (block length = h) for mean IC != 0
    over the full date series. KEPT FOR CONTINUITY BUT ANTI-CONSERVATIVE
    FOR h >= 3: with only n/h = 1.5-7 blocks the block=h bootstrap Bartlett-
    truncates the MA(h-1) long-run variance, and the block=min(block,n) clamp
    degenerates when block ~ n. Simulated false-positive rate at nominal 5%
    under pure overlapping-window noise (run-6 verdict-day analysis,
    agentIC_bootsim): h=1,n=24 5.2% | h=3,n=22 17.2% | h=5,n=20 25.5% |
    h=10,n=15 61.3% (58% of noise draws print p=0.0) | still 10-25% at
    n=60. Read it only at h=1; the column header and the caveat line under
    the table say so.

The 'ok' (reweight) gate: n_dates >= 60 AND p_nov < 0.05. Before run-7 it
was n_dates >= 60 alone; a 60-date kind whose calibrated p-value is not
significant is not a re-weighting candidate, and a p_boot=0.000 at h=10 is
not evidence of anything (see above). Do NOT re-weight the composite on this
table until the gate shows 'yes' (the review's precondition).

Run-8 pre-registered controls (opt-in, log-only; the IC-2 finding of the
run-6 verdict-day analysis: the technical kind's negative IC reads as a
market-wide momentum-reversal regime, not a slate inversion, and the run-8
decision rule needs both controls in the committed table):
  --control-momentum   regress each date's score ranks (and forward-return
                       ranks) on the names' trailing-20d return rank cross-
                       sectionally and report the IC of the residuals — the
                       partial Spearman(score, fwd | trail20). Ranks because
                       the slate's trailing returns are fat-tailed. A signal
                       that is only a momentum proxy shows ~0 here;
  --control-universe   SYM1,SYM2,... (or a file, one symbol per line/comma-
                       separated): recompute TechnicalProvider._score from
                       closes on that neutral basket (look-ahead free, closes
                       through the entry bar; Spearman +0.96 with the recorded
                       slate score), compute the same per-date IC on the same
                       dates, and print the PAIRED slate-minus-neutral
                       difference with t_naive/t_nov/p_nov. Technical kind
                       only (the only kind computable from prices). Names
                       already scored on the slate are dropped from the
                       basket. The provider's MACD is O(n^2) per (name,
                       date), ~2 ms each: ~10 s for 120 names x 25 dates.

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
  .venv/bin/python scripts/signal_ic.py --prices-json state/ic_prices.json \
      --control-momentum --control-universe docs/neutral_basket.txt
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
from typing import Callable, Iterable

import numpy as np

HORIZONS = (1, 3, 5, 10, 20)
MIN_NAMES_PER_DATE = 8
MIN_ENTRY_PRICE = 5.0
WINSOR = 0.25
MIN_DATES_FOR_REWEIGHT = 60
REWEIGHT_P_MAX = 0.05     # run-7 B4: the 'ok' gate also needs p_nov below this
BOOT_DRAWS = 2000
MIN_DATES_FOR_BOOT = 10   # a bootstrap over 2-3 dates prints p=0.0; not a p-value
BENCH = "SPY"
TRAIL_DAYS = 20           # --control-momentum: trailing return window (bars)
NEUTRAL_KIND = "technical"  # --control-universe applies to this kind only
MIN_CLOSES_FOR_TECH = 35  # TechnicalProvider needs MACD(26)+signal(9)
TECH_CLOSES_WINDOW = 260  # ~1y like the live provider (SMA200 + warm-up)
P_BOOT_COLUMN = "p_boot(anti-conservative h>=3)"
P_BOOT_CAVEAT = (
    "p_boot caveat: the block bootstrap (block=h) is anti-conservative for h>=3 — "
    "under pure overlapping-window noise it rejects at nominal 5% about 17% (h=3,n=22), "
    "26% (h=5,n=20) and 61% (h=10,n=15) of the time, and t_naive is inflated the same way; "
    "read p_boot only at h=1 and use p_nov (Student-t on the non-overlapping t, df=n_nov-1) "
    "for every horizon.")


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


def trailing_return(px: dict, cal: list[str], sym: str, date: str,
                    n: int = TRAIL_DAYS):
    """Trailing n-bar return of `sym` ending at the SAME entry bar fwd_excess
    starts from (the first close on/after `date`), so the control is known
    at the entry close and never peeks forward. None when the bars are
    missing from the cache."""
    s = px.get(sym)
    if not s:
        return None
    i = int(np.searchsorted(cal, date))
    if i >= len(cal) or i - n < 0:
        return None
    d0, d1 = cal[i - n], cal[i]
    if d0 not in s or d1 not in s or s[d0] <= 0:
        return None
    return s[d1] / s[d0] - 1.0


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


def residualize(values, controls) -> np.ndarray | None:
    """Cross-sectional rank regression of `values` on `controls`; returns the
    OLS residual of the value ranks (Pearson-orthogonal to the control rank
    by construction). Ranks rather than raw values because a single +200%
    trailing return would own an OLS fit on the slate's fat-tailed universe;
    Spearman downstream is rank-based anyway. None when either side is
    constant."""
    rv = _rank(np.asarray(values, float))
    rc = _rank(np.asarray(controls, float))
    if rv.std() == 0 or rc.std() == 0:
        return None
    b = float(np.cov(rv, rc, ddof=0)[0, 1] / rc.var())
    return rv - b * rc


def partial_spearman(scores, fwd, controls) -> float | None:
    """Spearman(score, fwd | control): BOTH the score ranks and the forward-
    return ranks are residualized on the control ranks, then Pearson-
    correlated (the textbook partial rank correlation). Residualizing only
    the score and taking Spearman(residual, fwd) is NOT enough: when the
    score is nearly the control itself (a pure momentum proxy) the residual
    is ~0 for most names and its tiny leftover (1-b)*rank(control) orders
    them by the control again, so a proxy read IC -0.10 instead of ~0 in
    the run-7 B4 synthetic check. Pearson-orthogonality on both sides makes
    the proxy case exactly zero in expectation."""
    rs = residualize(scores, controls)
    rf = residualize(fwd, controls)
    if rs is None or rf is None or rs.std() == 0 or rf.std() == 0:
        return None
    return float(np.corrcoef(rs, rf)[0, 1])


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


def per_date_ic_residual(per_date: dict[str, list[tuple[float, float, float]]],
                         min_names: int = MIN_NAMES_PER_DATE) -> list[tuple[str, float]]:
    """[(date, IC)] like per_date_ic but on (score, fwd, control) triples: the
    IC is the partial Spearman of score and fwd given the control within the
    date (--control-momentum)."""
    out = []
    for d in sorted(per_date):
        trip = per_date[d]
        if len(trip) < min_names:
            continue
        ic = partial_spearman([t[0] for t in trip], [t[1] for t in trip], [t[2] for t in trip])
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


def _betacf(a: float, b: float, x: float, max_iter: int = 300, eps: float = 3e-14) -> float:
    """Continued fraction for the incomplete beta function (modified Lentz),
    the textbook Numerical-Recipes `betacf`."""
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) >= tiny else tiny)
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) >= tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) >= tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) >= tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) >= tiny else tiny
        de = d * c
        h *= de
        if abs(de - 1.0) < eps:
            break
    return h


def _betai(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta I_x(a, b) in pure python (no scipy in the
    venv)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbt = (math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
           + a * math.log(x) + b * math.log1p(-x))
    bt = math.exp(lbt)
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def student_t_p(t: float | None, df: float | None) -> float | None:
    """Two-sided p-value of a Student-t statistic `t` on `df` degrees of
    freedom: I_x(df/2, 1/2) with x = df / (df + t^2). None when undefined
    (df < 1 or no t). Matches the textbook critical values (t=2.262 at df=9
    -> 0.05; t=12.706 at df=1 -> 0.05)."""
    if t is None or df is None or df < 1:
        return None
    t = float(t)
    if math.isnan(t) or math.isinf(t):
        return None if math.isnan(t) else 0.0
    x = df / (df + t * t)
    return float(min(1.0, max(0.0, _betai(df / 2.0, 0.5, x))))


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
    autocorrelation is preserved inside blocks).

    ANTI-CONSERVATIVE for block >= 3 at the table's n (see module docstring:
    false-positive rate 17-61% at nominal 5%); kept for continuity with the
    run-6 tables, labelled as such in the rendered column, and NOT used by
    the reweight gate — that is p_nov's job."""
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


def reweight_ok(n_dates: int, p_nov: float | None) -> bool:
    """The composite re-weighting gate ('ok' column): >= 60 dates (the Aug-25
    review's precondition) AND a calibrated two-sided p_nov < 0.05. Both are
    required — 60 dates of a null signal must not read as 'ok', and a
    significant p on 12 dates is still not enough history."""
    return bool(n_dates >= MIN_DATES_FOR_REWEIGHT and p_nov is not None
                and p_nov < REWEIGHT_P_MAX)


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
    p_nov = student_t_p(t_nov, len(nov_ics) - 1) if t_nov is not None else None
    return {
        "n_dates": int(len(ics)),
        "mean_ic": round(float(ics.mean()), 4),
        "hit_rate": round(float((ics > 0).mean()), 2),
        "t_naive": None if t_naive is None else round(t_naive, 2),
        "n_nonoverlap": len(nov_ics),
        "t_nonoverlap": None if t_nov is None else round(t_nov, 2),
        "p_nov": None if p_nov is None else round(p_nov, 4),
        "p_block_boot": (lambda p: None if p is None else round(p, 3))(
            block_bootstrap_p(ics, h, draws)),
        "reweight_ok": reweight_ok(len(ics), p_nov),
    }


def paired_difference(slate: list[tuple[str, float]], neutral: list[tuple[str, float]],
                      cal: list[str], h: int, draws: int = BOOT_DRAWS) -> dict:
    """Per-date slate IC minus neutral-basket IC on the dates both have, with
    the same t_naive / t_nov / p_nov machinery applied to the difference
    series (--control-universe). Pairing by date removes the common regime
    component (a market-wide momentum reversal hits both baskets)."""
    a, b = dict(slate), dict(neutral)
    ds = sorted(set(a) & set(b))
    if not ds:
        return {"n_dates": 0}
    r = summarize([(d, a[d] - b[d]) for d in ds], cal, h, draws)
    r.pop("reweight_ok", None)   # a difference is not a re-weighting candidate
    r["mean_slate"] = round(float(np.mean([a[d] for d in ds])), 4)
    r["mean_neutral"] = round(float(np.mean([b[d] for d in ds])), 4)
    r["slate_below_neutral"] = int(sum(1 for d in ds if a[d] < b[d]))
    return r


# ---------------------------------------------------------------- neutral-universe score
def recomputed_technical_score(closes: list[float]) -> float | None:
    """TechnicalProvider._score from a close series ending at the entry bar —
    the same RSI(14) / MACD(12,26,9) / SMA50 / SMA200 lean the live provider
    records, so the neutral basket is scored with the slate's own formula
    (look-ahead free: nothing past the entry close is in `closes`). None with
    fewer than 35 closes (MACD(26)+signal(9))."""
    if len(closes) < MIN_CLOSES_FOR_TECH:
        return None
    try:
        from investment_strategy.signals.technical import TechnicalProvider as T
    except ModuleNotFoundError:  # loaded by path (tests / ad-hoc), not via main()
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from investment_strategy.signals.technical import TechnicalProvider as T
    closes = [float(c) for c in closes[-TECH_CLOSES_WINDOW:]]
    rsi = T._rsi(closes, 14)
    macd, sig = T._macd(closes)
    price = closes[-1]
    return T._score(rsi, macd - sig, price, T._sma(closes, 50), T._sma(closes, 200))


def score_at(px: dict, cal: list[str], sym: str, date: str,
             score_fn: Callable[[list[float]], float | None],
             cache: dict | None = None) -> float | None:
    """`score_fn` over `sym`'s closes through the entry bar (first close on/
    after `date`, the bar fwd_excess starts from). `cache` memoizes per
    (sym, date) across horizons."""
    key = (sym, date)
    if cache is not None and key in cache:
        return cache[key]
    s = px.get(sym)
    val = None
    if s:
        i = int(np.searchsorted(cal, date))
        if i < len(cal) and cal[i] in s:
            keys = sorted(s)
            j = int(np.searchsorted(keys, cal[i], side="right"))
            val = score_fn([s[k] for k in keys[:j]])
    if cache is not None:
        cache[key] = val
    return val


def neutral_ic_series(px: dict, cal: list[str], bench: dict, universe: list[str],
                      dates: Iterable[str], h: int,
                      score_fn: Callable[[list[float]], float | None],
                      score_cache: dict | None = None) -> list[tuple[str, float]]:
    """Per-date IC of `score_fn` (recomputed from closes) over `universe` on
    `dates` — the neutral-basket leg of --control-universe."""
    per_date: dict[str, list[tuple[float, float]]] = collections.defaultdict(list)
    for d in sorted(set(dates)):
        for sym in universe:
            sc = score_at(px, cal, sym, d, score_fn, score_cache)
            if sc is None:
                continue
            fx = fwd_excess(px, cal, bench, sym, d, h)
            if fx is None:
                continue
            per_date[d].append((sc, fx))
    return per_date_ic(per_date)


def parse_universe(spec: str | None) -> list[str]:
    """--control-universe value: a comma/whitespace-separated symbol list, or
    a path to a file holding one (one per line and/or comma-separated)."""
    if not spec:
        return []
    text = spec
    if os.path.exists(spec):
        with open(spec, encoding="utf-8") as fh:
            text = fh.read()
    lines = [ln.split("#", 1)[0] for ln in text.splitlines()]   # '#' starts a comment
    toks = [t.strip().upper() for t in " ".join(lines).replace(",", " ").split()]
    seen: list[str] = []
    for t in toks:
        if t and t not in seen:
            seen.append(t)
    return seen


# ---------------------------------------------------------------- study
def run_study(last: dict[tuple[str, str, str], float], px: dict,
              horizons=HORIZONS, draws: int = BOOT_DRAWS,
              control_momentum: bool = False,
              control_universe: list[str] | None = None,
              neutral_score_fn: Callable[[list[float]], float | None] | None = None) -> dict:
    bench = px[BENCH]
    cal = sorted(bench)
    kinds = sorted({k[2] for k in last})
    out: dict = {"meta": {"bench": BENCH, "cal": [cal[0], cal[-1]],
                          "obs": len(last), "kinds": kinds,
                          "horizons": list(horizons),
                          "min_entry_price": MIN_ENTRY_PRICE, "winsor": WINSOR,
                          "min_dates_for_reweight": MIN_DATES_FOR_REWEIGHT,
                          "reweight_p_max": REWEIGHT_P_MAX,
                          "control_momentum": bool(control_momentum),
                          "control_universe": list(control_universe or [])},
                 "kinds": {}, "controls": {}}
    trail_cache: dict[tuple[str, str], float | None] = {}
    tech_series: dict[int, list[tuple[str, float]]] = {}
    if control_momentum:
        out["controls"]["momentum"] = {"trail_days": TRAIL_DAYS, "kinds": {}}
    for kind in kinds:
        out["kinds"][kind] = {}
        if control_momentum:
            out["controls"]["momentum"]["kinds"][kind] = {}
        for h in horizons:
            per_date: dict[str, list[tuple[float, float]]] = collections.defaultdict(list)
            per_date_ctl: dict[str, list[tuple[float, float, float]]] = collections.defaultdict(list)
            for (d, sym, k), sc in last.items():
                if k != kind:
                    continue
                fx = fwd_excess(px, cal, bench, sym, d, h)
                if fx is None:
                    continue
                per_date[d].append((sc, fx))
                if control_momentum:
                    if (d, sym) not in trail_cache:
                        trail_cache[(d, sym)] = trailing_return(px, cal, sym, d)
                    tr = trail_cache[(d, sym)]
                    if tr is not None:
                        per_date_ctl[d].append((sc, fx, tr))
            series = per_date_ic(per_date)
            if kind == NEUTRAL_KIND:
                tech_series[h] = series
            out["kinds"][kind][f"h{h}"] = summarize(series, cal, h, draws)
            if control_momentum:
                out["controls"]["momentum"]["kinds"][kind][f"h{h}"] = summarize(
                    per_date_ic_residual(per_date_ctl), cal, h, draws)
    if control_universe:
        slate_syms = {sym for (_d, sym, k) in last if k == NEUTRAL_KIND}
        basket = [s for s in control_universe if s not in slate_syms and s != BENCH]
        priced = [s for s in basket if s in px]
        score_fn = neutral_score_fn or recomputed_technical_score
        u: dict = {"kind": NEUTRAL_KIND, "requested": len(control_universe),
                   "dropped_on_slate": len(control_universe) - len(basket),
                   "priced": len(priced), "symbols": priced}
        tech_dates = sorted({d for (d, _sym, k) in last if k == NEUTRAL_KIND})
        score_cache: dict = {}
        for h in horizons:
            slate = tech_series.get(h, [])
            if not slate or not priced:
                u[f"h{h}"] = {"n_dates": 0, "neutral": {"n_dates": 0}}
                continue
            neutral = neutral_ic_series(px, cal, bench, priced, tech_dates, h,
                                        score_fn, score_cache)
            r = paired_difference(slate, neutral, cal, h, draws)
            r["neutral"] = summarize(neutral, cal, h, draws)
            u[f"h{h}"] = r
        out["controls"]["universe"] = u
    return out


# ---------------------------------------------------------------- render
def _fmt(v, spec: str = "") -> str:
    if v is None:
        return "-"
    return format(v, spec) if spec else str(v)


def _kind_rows(kinds: dict) -> list[str]:
    L = ["| kind | h | dates | mean IC | hit | t_naive | n_nov | t_nov | p_nov | "
         f"{P_BOOT_COLUMN} | ok |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    for kind, hs in kinds.items():
        for hk, r in hs.items():
            if r.get("n_dates", 0) == 0:
                L.append(f"| {kind} | {hk[1:]} | 0 | - | - | - | - | - | - | - | no |")
                continue
            L.append(
                f"| {kind} | {hk[1:]} | {r['n_dates']} | {r['mean_ic']:+.3f} | {r['hit_rate']:.2f} | "
                f"{_fmt(r['t_naive'])} | {r['n_nonoverlap']} | {_fmt(r['t_nonoverlap'])} | "
                f"{_fmt(r.get('p_nov'), '.3f')} | {_fmt(r['p_block_boot'])} | "
                f"{'yes' if r['reweight_ok'] else 'no'} |")
    return L


def render_markdown(res: dict) -> str:
    m = res["meta"]
    L = ["# Signal IC harness", "",
         f"obs {m['obs']} | kinds {len(m['kinds'])} | {m['bench']} calendar "
         f"{m['cal'][0]}..{m['cal'][1]} | entry close >= ${m['min_entry_price']:.0f} | "
         f"returns winsorized +/-{m['winsor']:.0%}", "",
         "IC = per-date cross-sectional Spearman(score, fwd excess return vs SPY). "
         "t_naive uses every date (overlapping windows, inflated); t_nov uses dates "
         "spaced >= h apart; p_nov = two-sided Student-t p of t_nov (df = n_nov-1), the "
         "calibrated column; p_boot = block bootstrap (block=h). Re-weighting the composite "
         f"needs >= {m['min_dates_for_reweight']} dates AND p_nov < "
         f"{m.get('reweight_p_max', REWEIGHT_P_MAX):.2f} (the 'ok' column).", ""]
    L += _kind_rows(res["kinds"])
    L += ["", P_BOOT_CAVEAT]
    ctl = res.get("controls") or {}
    if "momentum" in ctl:
        mo = ctl["momentum"]
        L += ["", f"## Momentum control (--control-momentum): IC = partial Spearman(score, fwd | "
                  f"trailing-{mo.get('trail_days', TRAIL_DAYS)}d return), score and fwd ranks each "
                  "residualized on the trailing-return rank within the date; a pure momentum proxy "
                  "reads ~0 here", ""]
        L += _kind_rows(mo["kinds"])
    if "universe" in ctl:
        u = ctl["universe"]
        L += ["", f"## Universe control (--control-universe): {u['kind']} score recomputed from "
                  f"closes on {u['priced']} neutral names ({u['requested']} requested, "
                  f"{u['dropped_on_slate']} dropped as slate names), same dates; paired "
                  "slate-minus-neutral per-date IC", "",
              "| h | dates | mean slate | mean neutral | mean diff | t_naive | n_nov | t_nov | "
              "p_nov | slate<neutral | neutral t_nov | neutral p_nov |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for hk, r in u.items():
            if not hk.startswith("h"):
                continue
            nr = r.get("neutral") or {}
            if r.get("n_dates", 0) == 0:
                L.append(f"| {hk[1:]} | 0 | - | - | - | - | - | - | - | - | - | - |")
                continue
            L.append(
                f"| {hk[1:]} | {r['n_dates']} | {r['mean_slate']:+.3f} | {r['mean_neutral']:+.3f} | "
                f"{r['mean_ic']:+.3f} | {_fmt(r['t_naive'])} | {r['n_nonoverlap']} | "
                f"{_fmt(r['t_nonoverlap'])} | {_fmt(r.get('p_nov'), '.3f')} | "
                f"{r['slate_below_neutral']}/{r['n_dates']} | {_fmt(nr.get('t_nonoverlap'))} | "
                f"{_fmt(nr.get('p_nov'), '.3f')} |")
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
    ap.add_argument("--control-momentum", action="store_true",
                    help="also report the partial Spearman IC given the names' trailing-20d "
                         "return (run-8 pre-registered control)")
    ap.add_argument("--control-universe", default=None, metavar="SYMS_OR_FILE",
                    help="comma-separated neutral symbols (or a file of them): recompute the "
                         "technical score on that basket and print the paired slate-minus-neutral "
                         "IC (run-8 pre-registered control)")
    args = ap.parse_args(argv)

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, repo)
    universe = parse_universe(args.control_universe)
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
    if universe:
        print(f"control universe: {len(universe)} symbols requested", file=sys.stderr)
    px = fetch_prices(syms | set(universe), args.days, args.prices_json, offline=args.offline)
    if BENCH not in px:
        print(f"no {BENCH} prices — cannot compute excess returns", file=sys.stderr)
        return 2
    res = run_study(last, px, draws=args.draws, control_momentum=args.control_momentum,
                    control_universe=universe or None)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(res, fh, indent=1)
    print(render_markdown(res))
    print(f"wrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
