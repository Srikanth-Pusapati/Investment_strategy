"""Run-6 item 4: signal taxonomy, history retention, composite exclusion of
DISCOVERY, and the standing IC harness math (scripts/signal_ic.py)."""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.attribution import parse_cited
from investment_strategy.config import Config, RiskLimits
from investment_strategy.models import Signal, SignalBundle, SignalKind
from investment_strategy.signals import history as hist
from investment_strategy.signals.composite import composite_score
from investment_strategy.signals.history import KIND_LAG_DAYS, SignalHistory, lag_weight
from investment_strategy.signals.insider import InsiderProvider
from investment_strategy.signals.options_flow import OptionsFlowProvider

_T0 = datetime(2026, 8, 24, 14, 30, tzinfo=timezone.utc)


def _sig(kind, score, symbol="XYZ"):
    return Signal(kind=kind, symbol=symbol, summary="x", score=score)


# -- (a) taxonomy ------------------------------------------------------------ #
def test_options_flow_is_its_own_kind_with_zero_lag():
    assert SignalKind.OPTIONS_FLOW.value == "options_flow"
    assert KIND_LAG_DAYS[SignalKind.OPTIONS_FLOW] == 0.0
    assert lag_weight(SignalKind.OPTIONS_FLOW) == 1.0
    assert KIND_LAG_DAYS[SignalKind.INSIDER] == 2.0


def test_options_flow_provider_emits_options_flow_kind():
    from tests.test_options_flow import _occ, _provider, _snap
    p, _ = _provider([{"snapshots": {_occ("C"): _snap(800), _occ("P"): _snap(200)}}])
    sigs = p.fetch(["AAPL"])
    assert len(sigs) == 1
    assert sigs[0].kind is SignalKind.OPTIONS_FLOW
    assert sigs[0].score == 0.6


def test_finnhub_insider_provider_emits_insider_kind():
    p = InsiderProvider.__new__(InsiderProvider)
    p.cfg = SimpleNamespace(finnhub_api_key="k")
    today = datetime.now(timezone.utc).date().isoformat()
    p._transactions = lambda sym: [
        {"transactionDate": today, "change": 1000, "transactionCode": "P"},
        {"transactionDate": today, "change": -250, "transactionCode": "S"},
    ]
    sigs = p.fetch(["AAPL"])
    assert len(sigs) == 1
    assert sigs[0].kind is SignalKind.INSIDER
    assert sigs[0].source == "finnhub-insider"
    assert sigs[0].score == 0.6


def test_parse_cited_maps_legacy_and_new_flow_citations_to_the_same_bucket():
    # Old ledger rows (flow emitted under kind=news) and new rows (own kind)
    # must land in ONE bucket so attribution history stays continuous.
    assert parse_cited(["news options flow C/P +0.76"]) == {"options_flow"}
    assert parse_cited(["options_flow +0.60 call imbalance"]) == {"options_flow"}
    assert parse_cited(["insider Form4 +1.00"]) == {"insider"}


def test_history_records_flow_under_its_own_kind_value():
    h = SignalHistory(path=_tmp())
    b = SignalBundle(symbol="XYZ", signals=[
        _sig(SignalKind.OPTIONS_FLOW, 0.4), _sig(SignalKind.NEWS, -0.2)])
    h.record([b], now=_T0)
    assert set(h.series["XYZ"]) == {"options_flow", "news"}


# -- (b) retention ------------------------------------------------------------ #
def _tmp():
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.unlink(path)
    return path


def test_retention_defaults_and_constructor_override():
    assert hist._RETENTION_DAYS == 120.0
    assert hist._MAX_POINTS == 480
    h = SignalHistory(path=_tmp())
    assert h.retention_days == 120.0 and h.max_points == 480
    h2 = SignalHistory(path=_tmp(), retention_days=3, max_points=5)
    assert h2.retention_days == 3.0 and h2.max_points == 5
    # max_points caps the series; retention prunes by age.
    now = _T0
    for i in range(9):
        h2.record([SignalBundle(symbol="A", signals=[_sig(SignalKind.NEWS, 0.1 * i)])], now=now)
        now += timedelta(hours=3)
    assert len(h2.series["A"]["news"]) == 5
    h2.record([SignalBundle(symbol="B", signals=[_sig(SignalKind.NEWS, 0.1)])],
              now=now + timedelta(days=4))
    assert "A" not in h2.series


def test_config_exposes_history_knobs(monkeypatch):
    monkeypatch.setenv("SIGNAL_HISTORY_RETENTION_DAYS", "90")
    monkeypatch.setenv("SIGNAL_HISTORY_MAX_POINTS", "360")
    from investment_strategy.config import _f, _i
    assert _f("SIGNAL_HISTORY_RETENTION_DAYS", 120.0) == 90.0
    assert _i("SIGNAL_HISTORY_MAX_POINTS", 480) == 360
    assert Config.__dataclass_fields__["signal_history_retention_days"].default == 120.0
    assert Config.__dataclass_fields__["signal_history_max_points"].default == 480


# -- (c) composite excludes discovery ---------------------------------------- #
def test_composite_excludes_discovery_by_default():
    b = SignalBundle(symbol="XYZ", signals=[
        _sig(SignalKind.DISCOVERY, 0.9), _sig(SignalKind.TECHNICAL, 0.5)])
    assert composite_score(b) == 0.5
    assert composite_score(b, include_discovery=True) == 1.4
    only = SignalBundle(symbol="XYZ", signals=[_sig(SignalKind.DISCOVERY, 0.9)])
    assert composite_score(only) is None
    assert composite_score(only, include_discovery=True) == 0.9


def test_composite_include_discovery_knob_default_off():
    assert RiskLimits.__dataclass_fields__["composite_include_discovery"].default is False


def test_orchestrator_bearish_lean_still_reads_discovery_without_composite():
    from investment_strategy.orchestrator import Orchestrator
    me = SimpleNamespace(cfg=SimpleNamespace(screener=SimpleNamespace(
        min_score=0.25, bearish_reserve_bar=0.4)))
    b = SignalBundle(symbol="XYZ", signals=[_sig(SignalKind.DISCOVERY, -0.5)])
    b.composite_score = composite_score(b)  # None under run-6
    assert b.composite_score is None
    assert Orchestrator._bearish_lean(me, b)


# -- (d) IC harness math ------------------------------------------------------ #
def _harness():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "scripts", "signal_ic.py")
    spec = importlib.util.spec_from_file_location("signal_ic", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_spearman_with_ties_and_perfect_monotone():
    m = _harness()
    assert abs(m.spearman([1, 2, 3, 4, 5], [10, 20, 30, 40, 50]) - 1.0) < 1e-12
    assert abs(m.spearman([1, 2, 3, 4, 5], [5, 4, 3, 2, 1]) + 1.0) < 1e-12
    assert m.spearman([1, 1, 1, 1], [1, 2, 3, 4]) is None       # constant -> None
    r = m.spearman([1, 2, 2, 3], [1, 2, 3, 4])                   # ties averaged
    assert 0.9 < r <= 1.0


def test_entry_date_rolls_after_close():
    m = _harness()
    assert m.entry_date("2026-08-24T19:59:00+00:00") == "2026-08-24"
    assert m.entry_date("2026-08-24T20:10:00+00:00") == "2026-08-25"
    assert m.entry_date("2026-08-24T16:10:00-04:00") == "2026-08-25"


def test_nonoverlap_dates_spaced_by_horizon():
    m = _harness()
    cal = [f"d{i:02d}" for i in range(20)]
    dates = cal[:12]
    assert m.nonoverlap_dates(dates, cal, 1) == dates
    assert m.nonoverlap_dates(dates, cal, 5) == ["d00", "d05", "d10"]
    assert m.nonoverlap_dates(["d03", "d04", "d09", "d10"], cal, 5) == ["d03", "d09"]


def test_fwd_excess_price_floor_and_winsor():
    m = _harness()
    cal = ["2026-08-03", "2026-08-04", "2026-08-05"]
    bench = {d: 100.0 for d in cal}
    px = {"OK": {"2026-08-03": 10.0, "2026-08-04": 11.0, "2026-08-05": 20.0},
          "PENNY": {"2026-08-03": 3.0, "2026-08-04": 4.0, "2026-08-05": 5.0}}
    assert abs(m.fwd_excess(px, cal, bench, "OK", "2026-08-03", 1) - 0.10) < 1e-12
    assert abs(m.fwd_excess(px, cal, bench, "OK", "2026-08-03", 2) - 0.25) < 1e-12  # winsorized
    assert m.fwd_excess(px, cal, bench, "PENNY", "2026-08-03", 1) is None  # < $5 entry
    assert m.fwd_excess(px, cal, bench, "OK", "2026-08-05", 1) is None     # no forward bar
    assert m.fwd_excess(px, cal, bench, "NOPE", "2026-08-03", 1) is None


def test_ic_summary_recovers_planted_signal_and_nulls_noise():
    m = _harness()
    rng = np.random.default_rng(1)
    n_dates, n_names = 40, 30
    cal = [(datetime(2026, 1, 1) + timedelta(days=i)).date().isoformat() for i in range(n_dates + 25)]
    good, noise = {}, {}
    for i in range(n_dates):
        sc = rng.normal(size=n_names)
        good[cal[i]] = [(float(s), float(0.5 * s + rng.normal(scale=0.5))) for s in sc]
        noise[cal[i]] = [(float(s), float(rng.normal())) for s in sc]
    g = m.summarize(m.per_date_ic(good), cal, 5, draws=400)
    z = m.summarize(m.per_date_ic(noise), cal, 5, draws=400)
    assert g["n_dates"] == n_dates and g["n_nonoverlap"] == 8   # 40 dates / 5 apart
    assert g["mean_ic"] > 0.4 and g["t_nonoverlap"] > 3 and g["p_block_boot"] < 0.01
    assert abs(z["mean_ic"]) < 0.15 and abs(z["t_nonoverlap"]) < 2.5 and z["p_block_boot"] > 0.05
    assert g["reweight_ok"] is False  # < 60 dates: the review's re-weight gate stays shut


def test_run_study_end_to_end_synthetic_no_network():
    m = _harness()
    cal = [(datetime(2026, 3, 2) + timedelta(days=i)).date().isoformat() for i in range(60)]
    rng = np.random.default_rng(7)
    px = {"SPY": {d: 100.0 * (1 + 0.001 * i) for i, d in enumerate(cal)}}
    last = {}
    for j in range(12):
        sym = f"S{j}"
        drift = 0.002 * (j - 6)  # higher j -> higher forward return
        px[sym] = {d: float(50.0 * np.exp(drift * i + 0.001 * rng.normal())) for i, d in enumerate(cal)}
        for d in cal[:30]:
            last[(d, sym, "technical")] = float(j / 12.0 + 0.15 * rng.normal())  # ordered like drift, noisy
    res = m.run_study(last, px, horizons=(1, 5), draws=200)
    r = res["kinds"]["technical"]["h5"]
    assert r["n_dates"] == 30 and r["mean_ic"] > 0.7 and r["t_nonoverlap"] > 5
    md = m.render_markdown(res)
    assert "| technical | 5 |" in md
