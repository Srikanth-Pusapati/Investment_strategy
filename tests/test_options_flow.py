"""Tests for the options-flow signal (signals/options_flow.py).

Pure logic, no network: the provider's HTTP getter is swapped for a fake that
serves canned Alpaca option-snapshot pages. We assert the call/put imbalance
math, the stale-dailyBar filter, pagination, the thin-volume skip, and that
failed reads surface as ONE per-cycle WARNING — the fix for the Polygon-era
silent 403 that left this signal dead with zero log evidence.

Runnable two ways:
    .venv/bin/python tests/test_options_flow.py
    .venv/bin/pytest tests/
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import date, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.execution.options import occ_symbol
from investment_strategy.signals.options_flow import OptionsFlowProvider


def _cfg(key="k", secret="s", polygon=""):
    return SimpleNamespace(alpaca_api_key=key, alpaca_secret_key=secret,
                           polygon_api_key=polygon)


class _Resp:
    def __init__(self, status=200, body=None, text=""):
        self.status_code = status
        self._body = body or {}
        self.text = text

    def json(self):
        return self._body


def _occ(right, strike=100, days=30):
    exp = (date.today() + timedelta(days=days)).strftime("%Y-%m-%d")
    return occ_symbol("AAPL", exp, strike, "call" if right == "C" else "put")


def _snap(vol, bar_date=None):
    d = bar_date or date.today().isoformat()
    return {"dailyBar": {"v": vol, "t": f"{d}T04:00:00Z"}}


def _provider(pages):
    """Provider whose HTTP getter serves `pages` ({snapshots, next_page_token}
    dicts or _Resp errors) in order, recording the params of every call."""
    p = OptionsFlowProvider(_cfg())
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(dict(params or {}))
        page = pages[min(len(calls) - 1, len(pages) - 1)]
        return page if isinstance(page, _Resp) else _Resp(body=page)

    p._get = fake_get
    return p, calls


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


def _capture_signals_log():
    h = _Capture()
    logger = logging.getLogger("signals")
    logger.addHandler(h)
    logger.setLevel(logging.DEBUG)
    return h, logger


def test_imbalance_signal_from_day_volume():
    p, _ = _provider([{
        "snapshots": {
            _occ("C"): _snap(300),
            _occ("P", strike=95): _snap(100),
        },
        "next_page_token": None,
    }])
    sigs = p.fetch(["AAPL"])
    assert len(sigs) == 1
    s = sigs[0]
    assert s.source == "alpaca-options-flow"
    assert s.score == 0.5                     # (300-100)/400
    assert s.data == {"call_volume": 300, "put_volume": 100}


def test_stale_daily_bars_excluded():
    old = (date.today() - timedelta(days=9)).isoformat()
    p, _ = _provider([{
        "snapshots": {
            _occ("C"): _snap(200),                      # latest session
            _occ("P", strike=95): _snap(5000, old),     # illiquid, days-old bar
        },
        "next_page_token": None,
    }])
    sigs = p.fetch(["AAPL"])
    assert len(sigs) == 1
    assert sigs[0].data == {"call_volume": 200, "put_volume": 0}


def test_pagination_followed_and_summed():
    p, calls = _provider([
        {"snapshots": {_occ("C"): _snap(150)}, "next_page_token": "tok2"},
        {"snapshots": {_occ("P", strike=95): _snap(50)}, "next_page_token": None},
    ])
    assert p._call_put_volume("AAPL") == (150, 50)
    assert len(calls) == 2
    assert calls[1].get("page_token") == "tok2"          # token carried forward


def test_thin_volume_skipped():
    p, _ = _provider([{
        "snapshots": {_occ("C"): _snap(60), _occ("P", strike=95): _snap(30)},
        "next_page_token": None,
    }])
    assert p.fetch(["AAPL"]) == []                       # 90 < 100 floor


def test_failed_reads_warn_once_per_cycle():
    p, _ = _provider([_Resp(status=403, text="NOT_AUTHORIZED")])
    h, logger = _capture_signals_log()
    try:
        assert p.fetch(["AAPL", "MSFT", "NVDA"]) == []
        warnings = [r for r in h.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1                        # one summary, not 3 lines
        msg = warnings[0].getMessage()
        assert "options-flow (signal)" in msg
        assert "3 of 3" in msg
        assert "403" in msg
    finally:
        logger.removeHandler(h)


def test_clean_cycle_logs_no_warning():
    p, _ = _provider([{"snapshots": {_occ("C"): _snap(300)}, "next_page_token": None}])
    h, logger = _capture_signals_log()
    try:
        p.fetch(["AAPL"])
        assert [r for r in h.records if r.levelno == logging.WARNING] == []
    finally:
        logger.removeHandler(h)


def test_enabled_requires_alpaca_keys():
    assert OptionsFlowProvider(_cfg()).enabled
    assert not OptionsFlowProvider(_cfg(key="")).enabled
    assert not OptionsFlowProvider(_cfg(secret="")).enabled


# --------------------------------------------------------------------------- #
# Polygon backend (Options Starter plan, wired 2026-07-27)
# --------------------------------------------------------------------------- #
def _poly_row(side, vol, oi):
    return {"details": {"contract_type": side}, "day": {"volume": vol},
            "open_interest": oi}


def _poly_provider(pages):
    p = OptionsFlowProvider(_cfg(polygon="pk"))
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append((url, dict(params or {})))
        page = pages[min(len(calls) - 1, len(pages) - 1)]
        return page if isinstance(page, _Resp) else _Resp(body=page)

    p._get = fake_get
    return p, calls


def test_polygon_key_selects_polygon_backend():
    p, calls = _poly_provider([{"results": [_poly_row("call", 300, 400),
                                            _poly_row("put", 100, 400)]}])
    sigs = p.fetch(["AAPL"])
    assert calls[0][0].startswith("https://api.polygon.io/")
    assert len(sigs) == 1
    assert sigs[0].source == "polygon-options-flow"
    assert sigs[0].data["call_volume"] == 300
    assert sigs[0].data["put_volume"] == 100


def test_polygon_fresh_positioning_boosts_score():
    # vol 400 vs OI 150 -> vol/OI > 1 -> new positioning; 0.5 base * 1.2 = 0.6.
    p, _ = _poly_provider([{"results": [_poly_row("call", 300, 100),
                                        _poly_row("put", 100, 50)]}])
    s = p.fetch(["AAPL"])[0]
    assert s.score == 0.6
    assert "fresh positioning" in s.summary
    assert s.data["vol_oi"] == round(400 / 150, 3)


def test_polygon_stale_inventory_damps_score():
    # vol 150 vs OI 2000 -> churn in existing inventory; 0.333 base * 0.85.
    p, _ = _poly_provider([{"results": [_poly_row("call", 100, 1000),
                                        _poly_row("put", 50, 1000)]}])
    s = p.fetch(["AAPL"])[0]
    assert s.score == round(round(50 / 150, 3) * 0.85, 3)
    assert "existing inventory" in s.summary


def test_polygon_midrange_vol_oi_leaves_score_untouched():
    # vol 200 vs OI 400 -> 0.5x: neither fresh nor stale; base imbalance kept.
    p, _ = _poly_provider([{"results": [_poly_row("call", 150, 300),
                                        _poly_row("put", 50, 100)]}])
    s = p.fetch(["AAPL"])[0]
    assert s.score == 0.5
    assert "vol 0.50x OI" in s.summary


def test_polygon_pagination_follows_next_url():
    p, calls = _poly_provider([
        {"results": [_poly_row("call", 150, 100)],
         "next_url": "https://api.polygon.io/v3/snapshot/options/AAPL?cursor=c2"},
        {"results": [_poly_row("put", 50, 100)]},
    ])
    assert p._call_put_volume("AAPL") == (150, 50)
    assert len(calls) == 2
    assert "cursor=c2" in calls[1][0]
    assert calls[1][1] == {}                    # cursor URL carries the query


def test_polygon_failure_surfaces_in_cycle_warning():
    p, _ = _poly_provider([_Resp(status=403, text="NOT_AUTHORIZED")])
    h, logger = _capture_signals_log()
    try:
        assert p.fetch(["AAPL"]) == []
        warnings = [r for r in h.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1 and "403" in warnings[0].getMessage()
    finally:
        logger.removeHandler(h)


def test_polygon_enabled_without_alpaca_keys():
    assert OptionsFlowProvider(_cfg(key="", secret="", polygon="pk")).enabled


def test_screener_brackets_flow_failures():
    from investment_strategy.screener.options_flow_feed import OptionsFlowScreener

    cfg = SimpleNamespace(
        alpaca_api_key="k", alpaca_secret_key="s",
        screener=SimpleNamespace(options_flow_scan_limit=5),
    )
    scr = OptionsFlowScreener.__new__(OptionsFlowScreener)   # skip real clients
    scr.cfg = cfg
    scr._flow = OptionsFlowProvider(cfg)
    scr._flow._get = lambda *a, **k: _Resp(status=403, text="NOT_AUTHORIZED")
    scr._screener = SimpleNamespace(get_most_actives=lambda req: SimpleNamespace(
        most_actives=[SimpleNamespace(symbol="AAPL"), SimpleNamespace(symbol="TSLA")],
    ))

    h = _Capture()
    logger = logging.getLogger("signals")
    logger.addHandler(h)
    logger.setLevel(logging.DEBUG)
    try:
        assert scr.enabled
        assert scr.scan() == []
        warnings = [r.getMessage() for r in h.records
                    if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "options-flow (screener)" in warnings[0]
        assert "2 of 2" in warnings[0]
    finally:
        logger.removeHandler(h)


def _run_all():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS {fn.__name__}")
        except Exception as e:
            failed += 1
            print(f"  FAIL {fn.__name__}: {e!r}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
