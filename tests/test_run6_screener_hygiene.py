"""Run-6 item 5: screener universe hygiene + honest FEEDS line.

(a) the price/ADV floor runs BEFORE the slate cap and the bearish reserve
    (one batched broker read; unknown prices fail open; knob off = legacy);
(b) robinhood_scans is not a DEFAULT screener source but stays registered;
(c) the FEEDS line reports an EDGAR pull failure as UNHEALTHY and appends
    news=vader-fallback once the Finnhub 403 is latched — n/n shape kept.
"""
from __future__ import annotations

import logging
import os
from types import SimpleNamespace


from investment_strategy.config import ScreenerConfig, load_config
from investment_strategy.models import Candidate
from investment_strategy.orchestrator import Orchestrator
from investment_strategy.portfolio.robinhood import RobinhoodReader
from investment_strategy.screener import aggregator as agg_mod
from investment_strategy.screener.aggregator import ScreenerAggregator
from investment_strategy.screener.base import Screener
from investment_strategy.screener.insider_feed import InsiderFeedScreener


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
class _Fake(Screener):
    def __init__(self, name, candidates):
        self.name = name
        self._candidates = candidates

    def scan(self):
        return self._candidates


class _Broker:
    """Counts calls so the tests can prove the floor is ONE batched read."""

    def __init__(self, prices=None, advs=None, boom=False):
        self.prices = prices or {}
        self.advs = advs or {}
        self.boom = boom
        self.price_calls: list[list[str]] = []
        self.adv_calls: list[list[str]] = []

    def latest_prices(self, symbols):
        if self.boom:
            raise RuntimeError("data API down")
        self.price_calls.append(list(symbols))
        return {s: self.prices[s] for s in symbols if s in self.prices}

    def avg_dollar_volume(self, symbols, days=20):
        self.adv_calls.append(list(symbols))
        return {s: self.advs[s] for s in symbols if s in self.advs}


def _cfg(max_candidates=2, floor=5.0, pre_cap=True, min_adv=0.0,
         options_enabled=True, reserve=0):
    return SimpleNamespace(
        screener=ScreenerConfig(
            enabled=True, sources=(), max_candidates=max_candidates,
            min_score=0.2, options_flow_scan_limit=40, insider_scan_limit=100,
            bearish_reserve=reserve, bearish_reserve_bar=0.4,
            price_floor_pre_cap=pre_cap, min_adv_usd=min_adv,
        ),
        risk=SimpleNamespace(options_enabled=options_enabled,
                             min_trade_price_usd=floor),
    )


def _agg(cfg, screeners, broker):
    a = ScreenerAggregator.__new__(ScreenerAggregator)
    a.cfg = cfg
    a.screeners = screeners
    a.broker = broker
    return a


def _c(sym, score):
    return Candidate(symbol=sym, sources=["a"], reason="x", score=score)


_SLATE = [_c("PENNY", 0.95), _c("CHEAP", 0.90), _c("GOOD", 0.6), _c("OK", 0.5)]


# --------------------------------------------------------------------------- #
# (a) price floor BEFORE the cap
# --------------------------------------------------------------------------- #
def test_sub_floor_names_never_reach_the_capped_slate():
    broker = _Broker(prices={"PENNY": 2.2, "CHEAP": 4.99, "GOOD": 40.0, "OK": 12.0})
    out = _agg(_cfg(max_candidates=2), [_Fake("a", _SLATE)], broker).scan()
    # Legacy behaviour capped to PENNY+CHEAP (highest |score|) and let the
    # orchestrator reject both later; now the two real names fill the slots.
    assert [c.symbol for c in out] == ["GOOD", "OK"]
    # ONE batched read for the whole merged candidate set, before capping.
    assert broker.price_calls == [["PENNY", "CHEAP", "GOOD", "OK"]]
    assert broker.adv_calls == []                       # ADV floor off by default


def test_unknown_price_fails_open():
    broker = _Broker(prices={"PENNY": 2.2})            # others unpriced
    out = _agg(_cfg(max_candidates=3), [_Fake("a", _SLATE)], broker).scan()
    assert [c.symbol for c in out] == ["CHEAP", "GOOD", "OK"]


def test_knob_off_is_legacy_no_read():
    broker = _Broker(prices={"PENNY": 2.2, "CHEAP": 4.99})
    out = _agg(_cfg(max_candidates=2, pre_cap=False), [_Fake("a", _SLATE)], broker).scan()
    assert [c.symbol for c in out] == ["PENNY", "CHEAP"]
    assert broker.price_calls == []


def test_no_broker_is_legacy():
    out = _agg(_cfg(max_candidates=2), [_Fake("a", _SLATE)], None).scan()
    assert [c.symbol for c in out] == ["PENNY", "CHEAP"]


def test_broker_error_leaves_slate_untouched():
    out = _agg(_cfg(max_candidates=2), [_Fake("a", _SLATE)], _Broker(boom=True)).scan()
    assert [c.symbol for c in out] == ["PENNY", "CHEAP"]


def test_floor_runs_before_bearish_reserve():
    # A sub-$5 bearish name must not consume the reserved short slot.
    slate = [_c("AAA", 0.9), _c("BBB", 0.8), _c("CCC", 0.7),
             _c("PENNYBEAR", -0.95), _c("BEAR", -0.5)]
    broker = _Broker(prices={"AAA": 20, "BBB": 20, "CCC": 20,
                             "PENNYBEAR": 1.5, "BEAR": 30})
    out = _agg(_cfg(max_candidates=3, reserve=1), [_Fake("a", slate)], broker).scan()
    syms = [c.symbol for c in out]
    assert "PENNYBEAR" not in syms
    assert "BEAR" in syms and len(syms) == 3


def test_adv_floor_knob_drops_thin_names_with_one_bars_call():
    broker = _Broker(prices={"GOOD": 40.0, "OK": 12.0},
                     advs={"GOOD": 50e6, "OK": 2e6})
    out = _agg(_cfg(max_candidates=5, min_adv=20e6),
               [_Fake("a", [_c("GOOD", 0.6), _c("OK", 0.5)])], broker).scan()
    assert [c.symbol for c in out] == ["GOOD"]
    assert len(broker.adv_calls) == 1


def test_orchestrator_passes_broker_to_aggregator():
    import investment_strategy.orchestrator as orch
    src = open(orch.__file__).read()
    assert "ScreenerAggregator(cfg, self.quiver, broker=self.broker)" in src


# --------------------------------------------------------------------------- #
# (b) default sources
# --------------------------------------------------------------------------- #
def test_default_sources_exclude_robinhood_scans(monkeypatch):
    monkeypatch.delenv("SCREENER_SOURCES", raising=False)
    monkeypatch.setenv("SCREENER_PRICE_FLOOR_PRE_CAP", "on")
    for k in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.setenv(k, os.environ.get(k, "x"))
    cfg = load_config()
    assert "robinhood_scans" not in cfg.screener.sources
    assert cfg.screener.sources == ("congress", "insider", "options_flow")
    assert cfg.screener.price_floor_pre_cap is True
    assert cfg.screener.min_adv_usd == 0.0
    assert cfg.feeds_degraded_modes is True
    # Still registered so a .env SCREENER_SOURCES can re-enable it.
    assert "robinhood_scans" in agg_mod._REGISTRY


# --------------------------------------------------------------------------- #
# (c) honest FEEDS line
# --------------------------------------------------------------------------- #
_SRC = ("congress", "insider", "options_flow", "robinhood", "wallstreetbets")


def _orch(degraded=None, vader=False, knob=True, sources=_SRC):
    o = Orchestrator.__new__(Orchestrator)
    o.cfg = SimpleNamespace(
        robinhood_enabled=False, feeds_degraded_modes=knob,
        screener=SimpleNamespace(sources=tuple(sources)),
    )
    scrs = []
    for n in sources:
        s = SimpleNamespace(name=n, enabled=True)
        if n == "insider":
            s.degraded = degraded
        scrs.append(s)
    o.screeners = SimpleNamespace(screeners=scrs)
    o.signals = SimpleNamespace(per_symbol=[
        SimpleNamespace(name="news", _finnhub_gated=vader),
        SimpleNamespace(name="technical"),
    ])
    return o


def test_feeds_edgar_timeout_is_unhealthy(monkeypatch):
    monkeypatch.setattr(RobinhoodReader, "_auth_dead", False)
    line = _orch(degraded="edgar: timeout")._feed_health_line()
    assert line.startswith("FEEDS: 4/5 — insider UNHEALTHY (edgar: timeout)")
    assert "EVAL WINDOW VALIDITY AT RISK" in line


def test_feeds_vader_fallback_suffix_keeps_nn_shape(monkeypatch):
    monkeypatch.setattr(RobinhoodReader, "_auth_dead", False)
    assert _orch(vader=True)._feed_health_line() == "FEEDS: 5/5 healthy news=vader-fallback"
    assert _orch()._feed_health_line() == "FEEDS: 5/5 healthy"
    both = _orch(degraded="edgar: http 503", vader=True)._feed_health_line()
    assert both.startswith("FEEDS: 4/5 — insider UNHEALTHY (edgar: http 503)")
    assert both.endswith("news=vader-fallback")


def test_feeds_knob_off_is_legacy(monkeypatch):
    monkeypatch.setattr(RobinhoodReader, "_auth_dead", False)
    assert _orch(degraded="edgar: timeout", vader=True, knob=False)._feed_health_line() \
        == "FEEDS: 5/5 healthy"


def test_unhealthy_feeds_log_at_warning(caplog, monkeypatch):
    monkeypatch.setattr(RobinhoodReader, "_auth_dead", False)
    o = _orch(degraded="edgar: timeout")
    with caplog.at_level(logging.INFO, logger="orchestrator"):
        o._check_robinhood_health()
    hit = next(r for r in caplog.records if "FEEDS:" in r.getMessage())
    assert hit.levelno == logging.WARNING


# --------------------------------------------------------------------------- #
# EDGAR screener sets/clears the degraded flag
# --------------------------------------------------------------------------- #
def _insider():
    return InsiderFeedScreener(SimpleNamespace(
        sec_user_agent="test agent", screener=SimpleNamespace(insider_scan_limit=5),
    ))


def test_edgar_timeout_marks_degraded_and_warns(monkeypatch, caplog):
    import requests

    def _timeout(*a, **k):
        raise requests.exceptions.ReadTimeout("read timed out")

    monkeypatch.setattr("investment_strategy.screener.insider_feed.requests.get", _timeout)
    s = _insider()
    with caplog.at_level(logging.WARNING, logger="screener"):
        assert s.scan() == []
    assert s.degraded == "edgar: timeout"
    assert any("UNHEALTHY this cycle" in r.getMessage() for r in caplog.records)


def test_edgar_http_error_marks_degraded(monkeypatch):
    monkeypatch.setattr(
        "investment_strategy.screener.insider_feed.requests.get",
        lambda *a, **k: SimpleNamespace(status_code=503, text=""),
    )
    s = _insider()
    assert s.scan() == []
    assert s.degraded == "edgar: http 503"


def test_edgar_fast_empty_200_is_not_degraded(monkeypatch):
    # A quick, genuinely empty feed is "no filings", not an outage.
    monkeypatch.setattr(
        "investment_strategy.screener.insider_feed.requests.get",
        lambda *a, **k: SimpleNamespace(status_code=200, text="<feed/>"),
    )
    s = _insider()
    s.degraded = "edgar: timeout"          # stale from a previous cycle
    assert s.scan() == []
    assert s.degraded is None              # cleared at the start of each scan


def test_edgar_slow_empty_200_is_degraded(monkeypatch):
    monkeypatch.setattr(
        "investment_strategy.screener.insider_feed.requests.get",
        lambda *a, **k: SimpleNamespace(status_code=200, text="<feed/>"),
    )
    clock = iter([0.0, 16.0, 16.0, 16.0])
    monkeypatch.setattr("investment_strategy.screener.insider_feed.time.monotonic",
                        lambda: next(clock))
    s = _insider()
    assert s.scan() == []
    assert s.degraded == "edgar: timeout"


# --------------------------------------------------------------------------- #
# Run-7 A7: FEEDS carries the earnings-blackout gate's source. Sep 10 2026 the
# RH OAuth token died at 08:34; the gate ran on per-symbol yfinance for 12
# decision cycles and all 8 FEEDS lines that day still read '3/3 healthy
# news=vader-fallback' (RH is not a SCREENER_SOURCE, so the n/n cannot see it).
# --------------------------------------------------------------------------- #
from investment_strategy.earnings import EarningsCalendar


class _RHReader:
    """RobinhoodReader shape for the calendar: enabled + a callable auth_dead."""
    def __init__(self, enabled=True, dead=False, payload=None):
        self.enabled = enabled
        self._dead = dead
        self.payload = payload

    def auth_dead(self):
        return self._dead

    def call_json(self, tool, arguments=None):
        return self.payload


def _orch_with_calendar(reader, **kw):
    o = _orch(**kw)
    o.earnings = EarningsCalendar(reader=reader)
    return o


def test_feeds_earnings_rh_when_robinhood_healthy(monkeypatch):
    monkeypatch.setattr(RobinhoodReader, "_auth_dead", False)
    o = _orch_with_calendar(_RHReader(enabled=True))
    assert o._feed_health_line() == "FEEDS: 5/5 healthy earnings=rh"
    # Order with the existing degraded token is fixed: news first, earnings last.
    o = _orch_with_calendar(_RHReader(enabled=True), vader=True)
    assert o._feed_health_line() == "FEEDS: 5/5 healthy news=vader-fallback earnings=rh"


def test_feeds_earnings_yfinance_fallback_when_rh_calendar_unavailable(monkeypatch):
    monkeypatch.setattr(RobinhoodReader, "_auth_dead", False)
    monkeypatch.setattr(EarningsCalendar, "_yf_importable", staticmethod(lambda: True))
    # Dead-auth latch (the Sep 10 shape) — the token flips on the reader's
    # enabled flag alone, before any calendar call this cycle.
    o = _orch_with_calendar(_RHReader(enabled=False, dead=True), vader=True)
    assert o._feed_health_line() \
        == "FEEDS: 5/5 healthy news=vader-fallback earnings=yfinance-fallback"
    # A failed calendar read on an enabled reader is reported from the next
    # line onward (the read happens after FEEDS in the cycle).
    o = _orch_with_calendar(_RHReader(enabled=True, payload=None))
    o.earnings._yf_lookup = lambda sym, today: None
    assert o._feed_health_line().endswith("earnings=rh")
    o.earnings.days_until_earnings("AAPL")
    assert o._feed_health_line().endswith("earnings=yfinance-fallback")
    # The n/n and the outage suffix keep their shape; earnings stays last.
    both = _orch_with_calendar(_RHReader(enabled=False, dead=True),
                               degraded="edgar: timeout")._feed_health_line()
    assert both.startswith("FEEDS: 4/5 — insider UNHEALTHY (edgar: timeout)")
    assert "EVAL WINDOW VALIDITY AT RISK" in both
    assert both.endswith("earnings=yfinance-fallback")


def test_feeds_earnings_token_absent_when_knob_off_or_no_calendar(monkeypatch):
    monkeypatch.setattr(RobinhoodReader, "_auth_dead", False)
    assert _orch_with_calendar(_RHReader(enabled=False, dead=True), knob=False) \
        ._feed_health_line() == "FEEDS: 5/5 healthy"
    assert _orch()._feed_health_line() == "FEEDS: 5/5 healthy"   # no calendar wired


def test_feeds_earnings_token_failure_drops_token_not_line(monkeypatch):
    monkeypatch.setattr(RobinhoodReader, "_auth_dead", False)
    o = _orch()
    o.earnings = SimpleNamespace(source=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert o._feed_health_line() == "FEEDS: 5/5 healthy"


def test_feeds_earnings_none_logs_at_warning_fallback_stays_info(caplog, monkeypatch):
    monkeypatch.setattr(RobinhoodReader, "_auth_dead", False)
    monkeypatch.setattr(EarningsCalendar, "_yf_importable", staticmethod(lambda: False))
    o = _orch_with_calendar(_RHReader(enabled=False, dead=True))
    with caplog.at_level(logging.INFO, logger="orchestrator"):
        o._check_robinhood_health()
    hit = next(r for r in caplog.records if "FEEDS:" in r.getMessage())
    assert hit.getMessage().endswith("earnings=none")
    assert hit.levelno == logging.WARNING
    caplog.clear()
    monkeypatch.setattr(EarningsCalendar, "_yf_importable", staticmethod(lambda: True))
    with caplog.at_level(logging.INFO, logger="orchestrator"):
        o._check_robinhood_health()
    hit = next(r for r in caplog.records if "FEEDS:" in r.getMessage())
    assert hit.getMessage().endswith("earnings=yfinance-fallback")
    assert hit.levelno == logging.INFO      # the alarm is earnings.py's own line


def test_feeds_earnings_fallback_with_the_real_reader_under_dead_latch(monkeypatch):
    # The duck-typed seam (a callable `auth_dead` classmethod) proven against
    # the REAL RobinhoodReader: the class-wide dead-auth latch alone flips the
    # token, no calendar call is attempted, and the fallback line names OAuth.
    # Nonexistent token path -> _maybe_recover() returns at sig is None; the
    # `enabled` property short-circuits on the latch before has_tokens().
    monkeypatch.setattr(RobinhoodReader, "_auth_dead", True)
    monkeypatch.setattr(RobinhoodReader, "_auth_dead_token_sig", None)
    monkeypatch.setattr(RobinhoodReader, "_last_recovery_check", 0.0)
    monkeypatch.setattr(EarningsCalendar, "_yf_importable", staticmethod(lambda: True))
    rr = RobinhoodReader(SimpleNamespace(
        robinhood_enabled=True, robinhood_mcp_url="https://rh.invalid/mcp",
        robinhood_mcp_token="", robinhood_oauth_file="/nonexistent/rh_oauth.json",
        state_file="/nonexistent/state.json",
    ))
    calls = []
    monkeypatch.setattr(rr, "call_json", lambda *a, **k: calls.append(a) or None)
    o = _orch_with_calendar(rr)
    assert o._feed_health_line() == "FEEDS: 5/5 healthy earnings=yfinance-fallback"
    o.earnings._yf_lookup = lambda sym, today: None
    with caplog_for("earnings") as recs:
        o.earnings.days_until_earnings("AAPL")
    assert calls == []
    assert o.earnings.rh_status() == "oauth-dead"
    hit = next(r for r in recs if r.getMessage().startswith("Earnings calendar:"))
    assert hit.levelno == logging.WARNING and "OAuth dead" in hit.getMessage()


import contextlib as _contextlib


@_contextlib.contextmanager
def caplog_for(name):
    recs = []

    class _H(logging.Handler):
        def emit(self, r):
            recs.append(r)

    lg = logging.getLogger(name)
    h = _H(level=logging.DEBUG)
    lvl = lg.level
    lg.addHandler(h)
    lg.setLevel(logging.DEBUG)
    try:
        yield recs
    finally:
        lg.removeHandler(h)
        lg.setLevel(lvl)
