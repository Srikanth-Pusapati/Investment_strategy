"""Run-7 item B4 — scripts/signal_ic.py: calibrated p_nov, labelled p_boot,
the 60-dates-AND-p_nov reweight gate, and the two run-8 pre-registered
controls (--control-momentum, --control-universe).

Why: the run-6 verdict-day analysis (agentIC_bootsim) showed the block
bootstrap p_boot rejects pure overlapping-window noise at nominal 5% about
17% (h=3,n=22), 26% (h=5,n=20) and 61% (h=10,n=15) of the time, so every
h>=3 p-value in the run-6 tables was uninformative and the 'ok' gate could
pass on 60 dates of nothing. The existing single-draw noise test in
tests/test_run6_signal_taxonomy.py cannot see a 17-60% false-positive rate;
the calibration test here can.

The older IC-harness math tests (spearman, entry_date, nonoverlap_dates,
fwd_excess, planted-signal summary, synthetic run_study) stay in
tests/test_run6_signal_taxonomy.py untouched.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta

import numpy as np

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


def _harness():
    path = os.path.join(REPO, "scripts", "signal_ic.py")
    spec = importlib.util.spec_from_file_location("signal_ic", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _cal(start: datetime, n: int) -> list[str]:
    return [(start + timedelta(days=i)).date().isoformat() for i in range(n)]


# -- (a) calibrated p-value + gate ------------------------------------------ #
def test_student_t_p_matches_textbook_critical_values():
    m = _harness()
    # two-sided 5% critical values from any t-table
    assert abs(m.student_t_p(2.262157, 9) - 0.05) < 2e-4
    assert abs(m.student_t_p(12.7062, 1) - 0.05) < 2e-4
    assert abs(m.student_t_p(2.093024, 19) - 0.05) < 2e-4
    assert abs(m.student_t_p(4.604, 4) - 0.01) < 2e-4
    assert abs(m.student_t_p(1.959964, 1e7) - 0.05) < 2e-4      # -> normal
    assert abs(m.student_t_p(2.0, 23) - 0.0574) < 2e-4         # the old "|t|>2" rule at n_nov=24
    assert m.student_t_p(-2.26, 23) == m.student_t_p(2.26, 23)  # two-sided, symmetric
    assert m.student_t_p(0.0, 5) == 1.0
    assert m.student_t_p(None, 5) is None and m.student_t_p(1.5, 0) is None


def test_p_nov_false_positive_rate_is_calibrated_at_h3_n22_and_p_boot_is_not():
    """200 seeded sims of PURE NOISE with the MA(h-1) autocorrelation that
    overlapping h-day windows create (the analyst's agentIC_bootsim
    construction), at the table's actual n for h=3. p_nov must reject at
    5% no more than 8% of the time; p_boot's rate on the same draws is the
    defect that earned it its column label (measured 0.175 here, 0.172 in
    the analysis)."""
    m = _harness()
    h, n, sims = 3, 22, 200
    cal = _cal(datetime(2026, 1, 1), 40)
    rng = np.random.default_rng(0)
    fp_nov = fp_boot = 0
    for _ in range(sims):
        e = rng.standard_normal(n + h)
        ic = np.convolve(e, np.ones(h) / h, mode="valid")[:n] * 0.2   # per-date IC sd ~0.2
        r = m.summarize([(cal[i], float(ic[i])) for i in range(n)], cal, h, draws=500)
        assert r["n_nonoverlap"] == 8                                 # 22 dates / 3 apart
        fp_nov += r["p_nov"] < 0.05
        fp_boot += r["p_block_boot"] < 0.05
    assert fp_nov / sims <= 0.08, fp_nov / sims
    assert fp_boot / sims >= 0.10, fp_boot / sims   # the anti-conservative column, pinned


def test_reweight_gate_requires_60_dates_and_calibrated_p_nov():
    m = _harness()
    assert m.reweight_ok(60, 0.049) is True
    assert m.reweight_ok(59, 0.0001) is False      # not enough history
    assert m.reweight_ok(200, 0.051) is False      # not significant
    assert m.reweight_ok(200, None) is False       # no non-overlapping t at all
    cal = _cal(datetime(2026, 1, 1), 120)
    rng = np.random.default_rng(2)
    planted = [(cal[i], float(rng.normal(0.30, 0.15))) for i in range(70)]
    noise = [(cal[i], float(rng.normal(0.0, 0.15))) for i in range(70)]
    g = m.summarize(planted, cal, 5, draws=200)
    z = m.summarize(noise, cal, 5, draws=200)
    assert g["n_dates"] == 70 and g["n_nonoverlap"] == 14 and g["p_nov"] < 0.001
    assert g["reweight_ok"] is True
    assert z["p_nov"] > 0.05 and z["reweight_ok"] is False
    short = m.summarize(planted[:40], cal, 5, draws=200)
    assert short["p_nov"] < 0.001 and short["reweight_ok"] is False   # 40 dates: gate shut


def test_render_markdown_has_p_nov_and_labelled_p_boot_columns_and_caveat():
    m = _harness()
    cal = _cal(datetime(2026, 1, 1), 40)
    rng = np.random.default_rng(3)
    ser = [(cal[i], float(rng.normal(0.1, 0.2))) for i in range(22)]
    res = {"meta": {"bench": "SPY", "cal": [cal[0], cal[-1]], "obs": 100, "kinds": ["technical"],
                    "horizons": [3], "min_entry_price": 5.0, "winsor": 0.25,
                    "min_dates_for_reweight": 60, "reweight_p_max": 0.05},
           "kinds": {"technical": {"h3": m.summarize(ser, cal, 3, draws=200),
                                   "h20": {"n_dates": 0}}}}
    md = m.render_markdown(res)
    header = [ln for ln in md.splitlines() if ln.startswith("| kind |")][0]
    assert "| p_nov |" in header
    assert "| p_boot(anti-conservative h>=3) |" in header
    assert "| technical | 3 | 22 |" in md
    assert "| technical | 20 | 0 | - | - | - | - | - | - | - | no |" in md   # empty row has both cells
    assert "p_boot caveat:" in md and "use p_nov" in md
    assert "AND p_nov < 0.05" in md
    row = [ln for ln in md.splitlines() if ln.startswith("| technical | 3 |")][0]
    cells = [c.strip() for c in row.strip("|").split("|")]
    assert len(cells) == len([c for c in header.strip("|").split("|")])
    assert cells[header.strip("|").split("|").index(" p_nov ")] == f"{res['kinds']['technical']['h3']['p_nov']:.3f}"


# -- (b) run-8 pre-registered controls -------------------------------------- #
def test_partial_spearman_zeroes_a_pure_momentum_proxy_and_keeps_an_orthogonal_signal():
    """Direct cross-sections: score = control (+small noise) must read ~0 once
    the control is partialled out, even though its raw IC is strongly
    negative; a signal independent of the control keeps its IC. An EXACT
    proxy (identical ranks) has no residual at all and reads None — the
    harness then drops the date rather than printing a fake 0."""
    m = _harness()
    rng = np.random.default_rng(1)
    raw_proxy, part_proxy, raw_orth, part_orth = [], [], [], []
    for _ in range(60):
        c = rng.standard_normal(30)
        a = rng.standard_normal(30)
        fwd = -0.5 * c + 0.5 * a + 0.5 * rng.standard_normal(30)
        proxy = c + 0.1 * rng.standard_normal(30)      # a few rank swaps per date (rho ~0.99)
        raw_proxy.append(m.spearman(proxy, fwd))
        part_proxy.append(m.partial_spearman(proxy, fwd, c))
        raw_orth.append(m.spearman(a, fwd))
        part_orth.append(m.partial_spearman(a, fwd, c))
    assert None not in part_proxy and None not in part_orth
    assert np.mean(raw_proxy) < -0.4
    assert abs(np.mean(part_proxy)) < 0.06 and abs(m.t_stat(part_proxy)) < 2.0
    assert np.mean(raw_orth) > 0.3 and np.mean(part_orth) > 0.35   # sharper once c is removed
    c = rng.standard_normal(30)
    assert m.partial_spearman(c + 1e-9, -0.5 * c + rng.standard_normal(30), c) is None  # exact proxy
    assert m.partial_spearman([1, 1, 1, 1], [1, 2, 3, 4], [4, 3, 2, 1]) is None   # constant score
    assert m.residualize([1, 2, 3], [5, 5, 5]) is None                           # constant control


def _momentum_fixture(seed: int, momo_noise: float, n_bars=130, n_names=30,
                      kappa=0.06, beta=0.01, h_alpha=5):
    """A mean-reverting tape: each bar reverts kappa of the trailing-21-bar
    log move (so trailing-20d return predicts forward return NEGATIVELY),
    plus an injected drift from an orthogonal per-(bar, name) alpha stamped
    over the next h_alpha bars. Two recorded kinds: 'momo' = trailing-20d
    return + noise (a pure momentum proxy), 'alpha' = the injected alpha +
    noise (real, momentum-independent information)."""
    m = _harness()
    rng = np.random.default_rng(seed)
    cal = _cal(datetime(2026, 1, 5), n_bars)
    px = {"SPY": {d: 100.0 * (1 + 0.0002 * i) for i, d in enumerate(cal)}}
    alpha = rng.standard_normal((n_bars, n_names))
    for j in range(n_names):
        logp = np.zeros(n_bars)
        for t in range(1, n_bars):
            eps = 0.012 * rng.standard_normal()
            rev = -kappa * (logp[t - 1] - logp[max(0, t - 21)]) if t > 21 else 0.0
            drift = beta * alpha[max(0, t - h_alpha):t, j].mean()
            logp[t] = logp[t - 1] + eps + rev + drift
        p = 50.0 * np.exp(logp)
        px[f"S{j:02d}"] = {d: float(p[i]) for i, d in enumerate(cal)}
    last = {}
    for i in range(25, 100):
        d = cal[i]
        for j in range(n_names):
            sym = f"S{j:02d}"
            tr = m.trailing_return(px, cal, sym, d)
            last[(d, sym, "momo")] = float(tr + momo_noise * rng.standard_normal())
            last[(d, sym, "alpha")] = float(alpha[i, j] + 0.3 * rng.standard_normal())
    return m, last, px, cal


def test_trailing_return_uses_the_entry_bar_and_needs_history():
    m = _harness()
    cal = _cal(datetime(2026, 3, 2), 30)
    px = {"X": {d: 100.0 + i for i, d in enumerate(cal)}}
    assert abs(m.trailing_return(px, cal, "X", cal[25]) - (125.0 / 105.0 - 1)) < 1e-12
    assert abs(m.trailing_return(px, cal, "X", cal[5], n=5) - (105.0 / 100.0 - 1)) < 1e-12
    assert m.trailing_return(px, cal, "X", cal[10]) is None    # < 20 bars of history
    assert m.trailing_return(px, cal, "NOPE", cal[25]) is None


def test_control_momentum_residual_ic_kills_the_proxy_and_keeps_the_alpha():
    for noise in (0.03, 0.003):     # realistic proxy (rho ~0.85) and the near-degenerate leakage case
        m, last, px, cal = _momentum_fixture(seed=5, momo_noise=noise)
        res = m.run_study(last, px, horizons=(1, 5), draws=200, control_momentum=True)
        assert res["meta"]["control_momentum"] is True
        raw = res["kinds"]["momo"]["h5"]
        ctl = res["controls"]["momentum"]["kinds"]["momo"]["h5"]
        assert raw["n_dates"] == 75 and raw["mean_ic"] < -0.15 and raw["t_nonoverlap"] < -3
        assert ctl["n_dates"] == 75 and abs(ctl["mean_ic"]) < 0.06 and abs(ctl["t_nonoverlap"]) < 2.0
        assert ctl["p_nov"] > 0.05
        raw_a = res["kinds"]["alpha"]["h5"]
        ctl_a = res["controls"]["momentum"]["kinds"]["alpha"]["h5"]
        assert raw_a["mean_ic"] > 0.2 and ctl_a["mean_ic"] > 0.2 and ctl_a["t_nonoverlap"] > 3
        assert ctl_a["p_nov"] < 0.01
        md = m.render_markdown(res)
        assert "## Momentum control (--control-momentum)" in md
        assert md.count("| momo | 5 | 75 |") == 2      # once in the main table, once in the control
    # without the flag nothing extra is computed
    res0 = _harness().run_study(last, px, horizons=(1,), draws=100)
    assert res0["controls"] == {} and res0["meta"]["control_momentum"] is False


def _trail5(closes):
    return None if len(closes) < 6 else closes[-1] / closes[-6] - 1.0


def _universe_fixture(seed=4, n_bars=120, n_slate=25, n_neutral=25, kappa=0.08):
    """Slate names continue their trailing-5d trend (kappa of the 5-bar log
    move is added each bar), neutral names are pure noise. The recorded
    'technical' score on the slate is the trailing-5d return + tiny noise
    (like the live recorded score vs its recompute, Spearman +0.96), and
    the injectable neutral score_fn is the same trailing-5d return."""
    m = _harness()
    rng = np.random.default_rng(seed)
    cal = _cal(datetime(2026, 2, 2), n_bars)
    px = {"SPY": {d: 100.0 * (1 + 0.0002 * i) for i, d in enumerate(cal)}}

    def path(continuation):
        logp = np.zeros(n_bars)
        for t in range(1, n_bars):
            eps = 0.012 * rng.standard_normal()
            cont = kappa * (logp[t - 1] - logp[max(0, t - 6)]) if (continuation and t > 6) else 0.0
            logp[t] = logp[t - 1] + eps + cont
        return 50.0 * np.exp(logp)

    slate = [f"SL{j:02d}" for j in range(n_slate)]
    neutral = [f"NU{j:02d}" for j in range(n_neutral)]
    for sym in slate:
        p = path(True)
        px[sym] = {d: float(p[i]) for i, d in enumerate(cal)}
    for sym in neutral:
        p = path(False)
        px[sym] = {d: float(p[i]) for i, d in enumerate(cal)}
    last = {}
    for i in range(10, 95):
        d = cal[i]
        for sym in slate:
            sc = m.score_at(px, cal, sym, d, _trail5)
            last[(d, sym, "technical")] = float(sc + 0.002 * rng.standard_normal())
    return m, last, px, cal, slate, neutral


def test_control_universe_paired_difference_detects_a_slate_only_edge():
    m, last, px, cal, slate, neutral = _universe_fixture()
    # SL00 is a slate name and SPY is the bench: both must be dropped from the basket
    res = m.run_study(last, px, horizons=(1, 3), draws=200,
                      control_universe=neutral + ["SL00", "SPY"], neutral_score_fn=_trail5)
    u = res["controls"]["universe"]
    assert u["kind"] == "technical" and u["requested"] == 27
    assert u["dropped_on_slate"] == 2 and u["priced"] == 25
    assert "SL00" not in u["symbols"] and "SPY" not in u["symbols"]
    for hk in ("h1", "h3"):
        r = u[hk]
        assert r["n_dates"] == 85
        assert r["mean_slate"] > 0.15 and abs(r["mean_neutral"]) < 0.05
        assert r["mean_ic"] > 0.1 and r["t_nonoverlap"] > 3 and r["p_nov"] < 0.01
        assert r["slate_below_neutral"] < 30
        assert "reweight_ok" not in r
        nr = r["neutral"]
        assert nr["n_dates"] == 85 and abs(nr["t_nonoverlap"]) < 2.5 and nr["p_nov"] > 0.05
    assert u["h3"]["n_nonoverlap"] == 29
    md = m.render_markdown(res)
    assert "## Universe control (--control-universe)" in md
    assert "25 neutral names (27 requested, 2 dropped as slate names)" in md
    assert "| 3 | 85 |" in md
    # the neutral leg must be computed on the SAME dates as the slate series
    assert len(m.neutral_ic_series(px, cal, px["SPY"], neutral, [cal[20], cal[21]], 1, _trail5)) == 2


def test_score_at_uses_closes_through_the_entry_bar_only():
    m = _harness()
    cal = _cal(datetime(2026, 3, 2), 12)
    px = {"X": {d: float(i) for i, d in enumerate(cal)}}
    seen = {}

    def fn(closes):
        seen["n"] = len(closes)
        return closes[-1]

    cache = {}
    assert m.score_at(px, cal, "X", cal[7], fn, cache) == 7.0 and seen["n"] == 8
    assert cache[("X", cal[7])] == 7.0
    assert m.score_at(px, cal, "X", "2026-03-05T", fn) == 4.0   # first bar on/after the date
    assert m.score_at(px, cal, "NOPE", cal[7], fn) is None


def test_recomputed_technical_score_matches_the_provider_lean():
    m = _harness()
    rng = np.random.default_rng(0)
    up = list(50.0 * np.exp(np.cumsum(0.004 + 0.003 * rng.standard_normal(250))))
    down = list(50.0 * np.exp(np.cumsum(-0.004 + 0.003 * rng.standard_normal(250))))
    su, sd = m.recomputed_technical_score(up), m.recomputed_technical_score(down)
    assert -1.0 <= sd < 0 < su <= 1.0
    assert m.recomputed_technical_score(up[:30]) is None       # < 35 closes: MACD undefined


def test_parse_universe_accepts_a_list_or_a_file():
    m = _harness()
    assert m.parse_universe("msft, googl amzn,MSFT") == ["MSFT", "GOOGL", "AMZN"]
    assert m.parse_universe(None) == [] and m.parse_universe("") == []
    with tempfile.TemporaryDirectory() as td:
        f = os.path.join(td, "basket.txt")
        with open(f, "w", encoding="utf-8") as fh:
            fh.write("# neutral basket\nJPM, V\nMA\n\n")
        assert m.parse_universe(f) == ["JPM", "V", "MA"]


def test_main_offline_end_to_end_renders_both_columns_and_both_controls(capsys):
    """The CLI path: a synthetic signal_history.json + price cache, --offline,
    --control-momentum and --control-universe together; both new columns and
    both control sections render and the JSON carries them."""
    m, last, px, cal, slate, neutral = _universe_fixture()
    series = {}
    for (d, sym, kind), sc in last.items():
        series.setdefault(sym, {}).setdefault(kind, []).append([f"{d}T15:00:00+00:00", sc])
    with tempfile.TemporaryDirectory() as td:
        hist = os.path.join(td, "signal_history.json")
        cache = os.path.join(td, "ic_prices.json")
        out = os.path.join(td, "signal_ic.json")
        basket = os.path.join(td, "basket.txt")
        with open(hist, "w", encoding="utf-8") as fh:
            json.dump({"series": series}, fh)
        with open(cache, "w", encoding="utf-8") as fh:
            json.dump(px, fh)
        with open(basket, "w", encoding="utf-8") as fh:
            fh.write("\n".join(neutral[:12]) + "\n")
        rc = m.main(["--history", hist, "--prices-json", cache, "--offline", "--out", out,
                     "--draws", "100", "--control-momentum", "--control-universe", basket])
        assert rc == 0
        md = capsys.readouterr().out
        with open(out, encoding="utf-8") as fh:
            res = json.load(fh)
    assert "| p_nov |" in md and "| p_boot(anti-conservative h>=3) |" in md
    assert "p_boot caveat:" in md
    assert "## Momentum control (--control-momentum)" in md
    assert "## Universe control (--control-universe)" in md
    assert "| technical | 1 | 85 |" in md
    r = res["kinds"]["technical"]["h1"]
    assert r["n_dates"] == 85 and "p_nov" in r and "reweight_ok" in r
    # slate dates are bars 10..94; the trailing-20d control needs bar >= 20 -> 75 dates
    assert res["controls"]["momentum"]["kinds"]["technical"]["h1"]["n_dates"] == 75
    assert res["controls"]["universe"]["priced"] == 12
    # the CLI scores the basket with the real TechnicalProvider recompute, which needs
    # 35 closes: slate bars 10..33 have no neutral score, so the paired series is bars 34..94
    assert res["controls"]["universe"]["h1"]["n_dates"] == 61
    assert res["controls"]["universe"]["h1"]["neutral"]["n_dates"] == 61
