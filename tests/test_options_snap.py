"""Run-7 S-4: single-name put strike snap (near-the-money, OI-qualified).

Evidence (run-6, dated logs): every single-name put failure shared ONE
mechanism — the model picked strikes far from the money although the
prompt asks for "strikes at/near the money" — HD 400P ~25% ITM (OI 2,
Sep 3/4 2026: `REJECT buy HD: Leg HD261016P00400000 open interest 2 <
100 floor`), LTH 30P 28% OTM (OI 26), LYV 150P/140P 12-18% OTM (OI
5/87), SCI 75P 6% OTM (OI 5), AAL 11P/10P 14-22% OTM with pennies of
premium (spreads 12-91%) — while each chain carried OI >= 100 within
~3-7% of spot. Root cause: the prompt rendered no spot price for a
candidate (the Sep 4 journal calls HD's 400 strike "the ATM put" with
HD at $321). These tests pin the deterministic snap between the model
and the gate, its exemptions (proxy / index / calls), the fail-open
paths (model legs stand -> existing reject path), and the journal /
ledger stamp of the ORIGINAL legs.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
from datetime import date
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from investment_strategy.execution.options import OptionsHelper, occ_symbol
from investment_strategy.models import (
    Action,
    Instrument,
    OptionLeg,
    OptionStrategy,
    RiskDecision,
    RiskVerdict,
    TradeProposal,
)
from investment_strategy.orchestrator import Orchestrator
from test_risk import _account

MONTHLY = "2026-10-16"   # third Friday
WEEKLY = "2026-10-09"
TODAY = date(2026, 9, 4)  # the second HD 400P reject


# --------------------------------------------------------------------------- #
# fixtures: a contract chain the trading API would return + an NBBO feed
# --------------------------------------------------------------------------- #
class _Trading:
    def __init__(self, contracts, token=None, fail=False):
        self._contracts, self._token, self._fail = contracts, token, fail
        self.requests: list = []

    def get_option_contracts(self, req):
        self.requests.append(req)
        if self._fail:
            raise RuntimeError("chain feed down")
        return SimpleNamespace(option_contracts=self._contracts, next_page_token=self._token)


def _chain(under: str, expiry: str, oi_by_strike: dict) -> list:
    """Contract rows as the trading API returns them: `open_interest` is a
    STRING (or None when the venue reports none)."""
    return [
        SimpleNamespace(
            expiration_date=expiry, strike_price=str(k),
            open_interest=None if oi is None else str(oi),
            symbol=occ_symbol(under, expiry, k, "put"),
        )
        for k, oi in oi_by_strike.items()
    ]


class _Data:
    """quotes: OCC symbol -> (bid, ask) | None (no NBBO). Unlisted symbols
    get a tight default two-sided market."""
    def __init__(self, quotes=None, fail=False):
        self._quotes, self._fail = quotes or {}, fail
        self.asked: list[str] = []

    def get_option_latest_quote(self, req):
        sym = req.symbol_or_symbols
        self.asked.append(sym)
        if self._fail:
            raise RuntimeError("quote feed down")
        q = self._quotes.get(sym, (1.00, 1.04))
        if q is None:
            return {}
        return {sym: SimpleNamespace(bid_price=q[0], ask_price=q[1])}


def _helper(contracts, quotes=None, fail_quotes=False, token=None, fail_chain=False):
    h = OptionsHelper.__new__(OptionsHelper)
    h._trading = _Trading(contracts, token=token, fail=fail_chain)
    h.data = _Data(quotes, fail=fail_quotes)
    return h


def _leg(strike, side=Action.BUY, expiry=MONTHLY, right="put", ratio=1):
    return OptionLeg(expiry=expiry, strike=strike, right=right, side=side, ratio=ratio)


def _snap(h, under, legs, spot, **kw):
    kw.setdefault("min_oi", 100.0)
    kw.setdefault("max_spread_pct", 10.0)
    kw.setdefault("max_moneyness_pct", 10.0)
    kw.setdefault("today", TODAY)
    kw.setdefault("min_dte", 7.0)
    kw.setdefault("max_dte", 60.0)
    return h.snap_legs_to_liquid(under, legs, spot, **kw)


# HD Oct-16 puts, Alpaca OI as read 2026-09-10 (data verifier A6-6)
HD_OI = {300: 1090, 305: 753, 310: 525, 315: 1825, 320: 1744, 325: 640,
         330: 410, 335: 300, 340: 210, 345: 122, 350: 22, 355: 3, 360: None, 400: 2}
HD_SPOT = 321.05  # Sep 4 close


# --------------------------------------------------------------------------- #
# the five spec tests
# --------------------------------------------------------------------------- #
def test_snap_clamps_deep_itm_put_to_near_atm(caplog):
    """HD 400P (25% ITM, OI 2) -> the ATM 320P (OI 1,744); a literal clamp
    to the band edge would have landed on 345P (8% ITM, OI 122) — still a
    synthetic short, not the near-ATM put the prompt describes."""
    h = _helper(_chain("HD", MONTHLY, HD_OI))
    with caplog.at_level(logging.INFO, logger="options"):
        out = _snap(h, "HD", [_leg(400)], HD_SPOT)
    assert [l.strike for l in out] == [320.0]
    assert out[0].expiry == MONTHLY and out[0].side is Action.BUY and out[0].right == "put"
    line = [r.getMessage() for r in caplog.records if r.getMessage().startswith("STRIKE SNAP:")]
    assert line == [
        "STRIKE SNAP: HD 2026-10-16 400P (OI 2, 24.6% ITM) -> 320P (OI 1744, "
        "0.3% OTM); unsnapped would be rejected: open interest 2 < 100"
    ]
    # One chain request, puts only, +/-7 d around the model's expiry, and the
    # strike window widened to include the ORIGINAL strike (for its OI).
    req = h._trading.requests[0]
    assert req.type == "put" and req.underlying_symbols == ["HD"]
    assert str(req.expiration_date_gte) == "2026-10-09" and str(req.expiration_date_lte) == "2026-10-23"
    assert float(req.strike_price_lte) >= 400.0


def test_snap_moves_deep_otm_put_up_to_qualified_strike():
    """LTH 30P (28% OTM, OI 26) with spot 41.83 -> 40P (OI 843): the OTM side
    of the two-sided rule (the refuter's correction to the ITM-only clamp)."""
    h = _helper(_chain("LTH", MONTHLY, {30: 26, 35: 1821, 40: 843, 45: 50}))
    out = _snap(h, "LTH", [_leg(30)], 41.83)
    assert [l.strike for l in out] == [40.0]
    assert out[0].expiry == MONTHLY


def test_snap_returns_none_when_no_qualified_strike(caplog):
    """A chain thin everywhere near the money: None -> the caller keeps the
    model's legs and the existing OI gate rejects them (then the proxy
    fallback fires exactly as in run-6)."""
    h = _helper(_chain("SCI", MONTHLY, {70: 5, 72.5: 8, 75: 5, 77.5: 40, 80: 60, 82.5: 70}))
    with caplog.at_level(logging.INFO, logger="options"):
        out = _snap(h, "SCI", [_leg(75)], 79.69)
    assert out is None
    msgs = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("STRIKE SNAP: SCI 2026-10-16 75P (OI 5, 5.9% OTM) none") for m in msgs)


def _opt_orch(reason="1 contract(s), $1,200 debit (cap $5,100).",
              verdict=RiskVerdict.APPROVED, cfg=None, snapped=None, snap_raises=False):
    """Orchestrator stub in the shape tests/test_run6_options.py uses, plus a
    spy on the helper's snap and captures for journal / ledger / build_legs."""
    o = Orchestrator.__new__(Orchestrator)
    o.snap_calls: list[dict] = []

    def _snap(sym, legs, spot, **kw):
        o.snap_calls.append({"sym": sym, "legs": legs, "spot": spot, **kw})
        if snap_raises:
            raise RuntimeError("chain feed down")
        return snapped

    o.options = SimpleNamespace(
        estimate_net_premium=lambda p: 12.0,
        min_leg_premium=lambda p: 12.0,
        leg_liquidity=lambda p: [],
        snap_legs_to_liquid=_snap,
        build_legs=lambda p: [("built", l.expiry, l.strike) for l in p.option_legs],
    )
    o.cfg = cfg if cfg is not None else SimpleNamespace(
        core_etf="QQQ", hedge_etf="PSQ", put_proxy_etf="IWM",
        defensive_core_etf="", proxy_put_thesis_gate=True,
        option_strike_snap=True, option_strike_max_moneyness_pct=10.0,
    )
    o._regime_trend, o._regime_label, o._regime_mult = "down", "risk-off", 1.0
    o._hedge_symbol = ""
    o.earnings = None
    o.broker = SimpleNamespace(
        latest_price=lambda s: HD_SPOT,
        submit_option_legs=lambda legs, qty, est_premium_per_share: "oid-1",
    )
    o.risk = SimpleNamespace(
        limits=SimpleNamespace(min_option_open_interest=100.0, max_option_spread_pct=10.0,
                               min_option_dte=7.0, max_option_dte=60.0),
    )
    o.journal_rows: list[tuple] = []
    o._journal_decision = lambda *a, **k: o.journal_rows.append(a)
    o.ledger_rows: list = []
    o.ledger = SimpleNamespace(record=lambda rec: o.ledger_rows.append(rec))
    o.state = SimpleNamespace(
        add_pending_order=lambda *a, **k: None, register_buy=lambda *a, **k: None,
        register_daily_deploy=lambda *a, **k: None,
    )
    o._pending_oids = []
    o._trade_lock = threading.Lock()
    o.built = []
    o.options.build_legs = lambda p: o.built.append([(l.expiry, l.strike) for l in p.option_legs]) or [1]
    o.evaluated: list[TradeProposal] = []

    def _eval(proposal, account, *a, **k):
        o.evaluated.append(proposal)
        return RiskDecision(
            proposal=proposal, verdict=verdict, reason=reason,
            approved_qty=1.0 if verdict != RiskVerdict.REJECTED else 0.0,
            approved_notional=1200.0 if verdict != RiskVerdict.REJECTED else 0.0,
        )
    o.risk.evaluate_option = _eval
    o._propose_proxy_put = lambda *a, **k: o.__dict__.setdefault("proxied", []).append(a[0].symbol)
    return o


def _opt(symbol, strategy, legs):
    return TradeProposal(
        symbol=symbol, action=Action.BUY, conviction=0.8, target_weight_pct=1.0,
        rationale="bearish", instrument=Instrument.OPTION,
        option_strategy=strategy, option_legs=legs,
    )


def _long_put(symbol="HD", strike=400):
    return _opt(symbol, OptionStrategy.LONG_PUT, [_leg(strike)])


def test_snap_skips_proxy_and_index_legs():
    """The proxy re-proposal, index underlyings (core/hedge/proxy/defensive
    ETFs) and call structures never reach the snap; a single-name put does,
    and the knob turns it off."""
    o = _opt_orch(snapped=None)
    spread = _opt("IWM", OptionStrategy.BEAR_PUT_SPREAD, [_leg(220), _leg(209, Action.SELL)])
    o._handle_option(spread, _account(), proxy_for="EXTR")      # proxy leg
    o._handle_option(_long_put("QQQ", 400), _account())          # index (core ETF)
    o._handle_option(_long_put("PSQ", 20), _account())           # index (hedge ETF)
    o._handle_option(_opt("NVDA", OptionStrategy.LONG_CALL,
                          [_leg(200, right="call")]), _account())  # call
    assert o.snap_calls == []
    o._handle_option(_long_put("HD", 400), _account(), tech={"price": 318.07})
    assert len(o.snap_calls) == 1
    c = o.snap_calls[0]
    assert c["sym"] == "HD" and c["spot"] == HD_SPOT      # broker price preferred
    assert c["min_oi"] == 100.0 and c["max_spread_pct"] == 10.0
    assert c["max_moneyness_pct"] == 10.0 and c["min_dte"] == 7.0 and c["max_dte"] == 60.0
    # None from the helper = model legs stand, judged as proposed, no stamp.
    assert [l.strike for l in o.evaluated[-1].option_legs] == [400.0]
    assert "STRIKE SNAP" not in o.journal_rows[-1][7]
    # Knob off: never called.
    off = _opt_orch(cfg=SimpleNamespace(
        core_etf="QQQ", hedge_etf="PSQ", put_proxy_etf="IWM", defensive_core_etf="",
        proxy_put_thesis_gate=True, option_strike_snap=False,
    ), snapped=[_leg(320)])
    off._handle_option(_long_put("HD", 400), _account())
    assert off.snap_calls == []
    assert [l.strike for l in off.evaluated[-1].option_legs] == [400.0]


def test_handle_option_stamps_original_legs_in_risk_note():
    """The gate, build_legs and the ledger see the SNAPPED legs; the journal
    `reason` and the ledger `risk_note` carry the model's ORIGINAL legs."""
    o = _opt_orch(snapped=[_leg(320)])
    o._handle_option(_long_put("HD", 400), _account(), tech={"price": 318.07})
    assert [l.strike for l in o.evaluated[-1].option_legs] == [320.0]
    stamp = "[STRIKE SNAP: model legs 2026-10-16 400P -> 320P (spot 321.05 broker)]"
    reason = o.journal_rows[-1][7]
    # A-6: the JOURNAL reason leads with the strategy so 'Today so far' can
    # never print a put as a share buy; the ledger risk_note is unchanged.
    assert reason.startswith("long_put: 1 contract(s), $1,200 debit (cap $5,100).") and reason.endswith(stamp)
    assert o.built == [[(MONTHLY, 320.0)]]
    rec = o.ledger_rows[-1]
    assert rec.risk_note.endswith(stamp)
    assert rec.occ_symbols == ["HD261016P00320000"] and rec.symbol == "HD261016P00320000"
    # A liquidity reject on the SNAPPED legs still feeds the proxy fallback
    # (the stamp must not mask the reject reason).
    o2 = _opt_orch(snapped=[_leg(320)], verdict=RiskVerdict.REJECTED,
                   reason="Leg HD261016P00320000 open interest 40 < 100 floor")
    o2._handle_option(_long_put("HD", 400), _account())
    assert o2.proxied == ["HD"]
    assert o2.journal_rows[-1][7].endswith(stamp)


# --------------------------------------------------------------------------- #
# refinements: structure, expiry fallback, spread/quote, fail-open, knob load
# --------------------------------------------------------------------------- #
def test_snap_keeps_in_band_qualified_legs_unchanged(caplog):
    h = _helper(_chain("HD", MONTHLY, HD_OI))
    with caplog.at_level(logging.INFO, logger="options"):
        out = _snap(h, "HD", [_leg(320)], HD_SPOT)
    assert [(l.expiry, l.strike) for l in out] == [(MONTHLY, 320.0)]
    assert any("STRIKE SNAP: HD 2026-10-16 320P (OI 1744, 0.3% OTM) kept" in r.getMessage()
               for r in caplog.records)


def test_snap_in_band_thin_strike_moves_to_nearest_qualified():
    """SCI 75P (6% OTM, inside the band, OI 5) keeps its own target and moves
    to the nearest qualified strike, 77.5P (OI 161) — not to ATM."""
    h = _helper(_chain("SCI", MONTHLY, {70: 5, 72.5: 8, 75: 5, 77.5: 161, 80: 900, 82.5: 70}))
    out = _snap(h, "SCI", [_leg(75)], 79.69)
    assert [l.strike for l in out] == [77.5]


def test_snap_spread_keeps_width_and_single_expiry(caplog):
    """LYV 150/140P (12-18% OTM, OI 5/87) -> 170/160P: the long re-targets ATM,
    the short keeps the original $10 width below it, both on ONE expiry,
    sides/ratios untouched."""
    h = _helper(_chain("LYV", MONTHLY, {140: 87, 145: 60, 150: 5, 155: 368, 160: 1422,
                                        165: 398, 170: 368, 175: 200}))
    legs = [_leg(150, ratio=2), _leg(140, Action.SELL, ratio=2)]
    with caplog.at_level(logging.INFO, logger="options"):
        out = _snap(h, "LYV", legs, 170.08)
    assert [(l.strike, l.side, l.ratio, l.expiry) for l in out] == [
        (170.0, Action.BUY, 2, MONTHLY), (160.0, Action.SELL, 2, MONTHLY),
    ]
    line = [r.getMessage() for r in caplog.records if "->" in r.getMessage()][0]
    assert line.startswith("STRIKE SNAP: LYV 2026-10-16 150/140P (OI 5/87, 11.8% OTM/17.7% OTM) "
                           "-> 170/160P (OI 368/1422, ATM/5.9% OTM); unsnapped would be rejected: "
                           "open interest 5 < 100")


def test_snap_falls_back_to_monthly_within_7d(caplog):
    """No qualified strike on the model's weekly -> the nearest third Friday
    within +/-7 d (inside the DTE window); the log shows the new expiry."""
    chain = _chain("HD", WEEKLY, {315: 20, 320: 30, 325: 10}) + _chain("HD", MONTHLY, HD_OI)
    h = _helper(chain)
    with caplog.at_level(logging.INFO, logger="options"):
        out = _snap(h, "HD", [_leg(320, expiry=WEEKLY)], HD_SPOT)
    assert [(l.expiry, l.strike) for l in out] == [(MONTHLY, 320.0)]
    assert any("-> 2026-10-16 320P (OI 1744, 0.3% OTM)" in r.getMessage() for r in caplog.records)
    # ...but never onto an expiry outside the gate's DTE window (Oct 16 is 42 d from Sep 4).
    assert _snap(_helper(chain), "HD", [_leg(320, expiry=WEEKLY)], HD_SPOT, max_dte=40.0) is None
    # ...and a monthly more than 7 d away is not a substitute either.
    far = _chain("HD", WEEKLY, {320: 30}) + _chain("HD", "2026-11-20", HD_OI)
    assert _snap(_helper(far), "HD", [_leg(320, expiry=WEEKLY)], HD_SPOT) is None


def test_snap_skips_wide_spread_and_quote_less_strikes():
    """AAL: 13P has a 12%+ spread, 12.5P has no NBBO -> 12P (bid .31/ask .33,
    OI 9,581). A quote FEED error fails open (the gate's rule), so the
    nearest OI-qualified strike is taken."""
    chain = _chain("AAL", MONTHLY, {10: 5000, 11: 9000, 12: 9581, 12.5: 3000, 13: 4606, 14: 1000})
    quotes = {
        occ_symbol("AAL", MONTHLY, 13, "put"): (0.20, 0.30),   # 40% spread
        occ_symbol("AAL", MONTHLY, 12.5, "put"): None,        # no NBBO
        occ_symbol("AAL", MONTHLY, 12, "put"): (0.31, 0.33),
    }
    out = _snap(_helper(chain, quotes), "AAL", [_leg(11)], 12.85, strike_tol_pct=7.0)
    assert [l.strike for l in out] == [12.0]
    out = _snap(_helper(chain, fail_quotes=True), "AAL", [_leg(11)], 12.85, strike_tol_pct=7.0)
    assert [l.strike for l in out] == [13.0]


def test_snap_none_oi_is_qualified_only_when_chain_has_no_oi():
    """The gate fails open on None OI; the snap leans on that ONLY when no
    contract in the chain carries OI (nothing to rank by)."""
    no_oi = _chain("HD", MONTHLY, {k: None for k in HD_OI})
    out = _snap(_helper(no_oi), "HD", [_leg(400)], HD_SPOT)
    assert [l.strike for l in out] == [320.0]
    mixed = _chain("HD", MONTHLY, {315: 5, 320: None, 325: 640, 400: 2})
    out = _snap(_helper(mixed), "HD", [_leg(400)], HD_SPOT)
    assert [l.strike for l in out] == [325.0]


def test_snap_leaves_unrecognised_structures_alone():
    """Calls, mixed expiries, non-bear spreads: None WITHOUT a chain read —
    the gate judges them as proposed."""
    h = _helper(_chain("HD", MONTHLY, HD_OI))
    assert _snap(h, "HD", [_leg(400, right="call")], HD_SPOT) is None
    assert _snap(h, "HD", [_leg(400), _leg(390, Action.SELL, expiry=WEEKLY)], HD_SPOT) is None
    assert _snap(h, "HD", [_leg(390), _leg(400, Action.SELL)], HD_SPOT) is None  # short above long
    assert _snap(h, "HD", [_leg(400)], 0.0) is None
    assert _snap(h, "HD", [], HD_SPOT) is None
    assert h._trading.requests == []


def test_snap_chain_failure_and_pagination(caplog):
    with caplog.at_level(logging.WARNING, logger="options"):
        assert _snap(_helper([], fail_chain=True), "HD", [_leg(400)], HD_SPOT) is None
        out = _snap(_helper(_chain("HD", MONTHLY, HD_OI), token="p2"), "HD", [_leg(400)], HD_SPOT)
    assert [l.strike for l in out] == [320.0]
    msgs = [r.getMessage() for r in caplog.records]
    assert any("chain lookup failed" in m and "model legs stand" in m for m in msgs)
    assert any("paginated" in m for m in msgs)


def test_handle_option_helper_failure_keeps_model_legs():
    """A helper exception, a non-list return, or a missing spot never blocks
    the proposal — the model's legs go to the gate unchanged, unstamped."""
    o = _opt_orch(snap_raises=True)
    o._handle_option(_long_put("HD", 400), _account())
    assert [l.strike for l in o.evaluated[-1].option_legs] == [400.0]
    assert "STRIKE SNAP" not in o.journal_rows[-1][7]
    o = _opt_orch(snapped="not a list")
    o._handle_option(_long_put("HD", 400), _account())
    assert [l.strike for l in o.evaluated[-1].option_legs] == [400.0]
    o = _opt_orch(snapped=[_leg(320)])
    o.broker = SimpleNamespace(latest_price=lambda s: (_ for _ in ()).throw(RuntimeError("down")),
                               submit_option_legs=lambda *a, **k: "oid")
    o._handle_option(_long_put("HD", 400), _account())           # no tech -> no spot
    assert o.snap_calls == [] and [l.strike for l in o.evaluated[-1].option_legs] == [400.0]
    o = _opt_orch(snapped=[_leg(320)])
    o.broker = SimpleNamespace(latest_price=lambda s: 0.0, submit_option_legs=lambda *a, **k: "oid")
    o._handle_option(_long_put("HD", 400), _account(), tech={"price": 318.07})
    assert o.snap_calls[0]["spot"] == 318.07                     # technical fallback
    assert o.journal_rows[-1][7].endswith("(spot 318.07 technical)]")
    # A stub helper without the method (tests/test_run6_options.py shape) is a no-op.
    o = _opt_orch()
    o.options = SimpleNamespace(estimate_net_premium=lambda p: 1.0, min_leg_premium=lambda p: 0.5,
                                leg_liquidity=lambda p: [], build_legs=lambda p: [1])
    o._handle_option(_long_put("HD", 400), _account())
    assert [l.strike for l in o.evaluated[-1].option_legs] == [400.0]


def test_load_config_option_strike_snap(monkeypatch):
    from investment_strategy.config import Config, load_config
    assert Config.__dataclass_fields__["option_strike_snap"].default is True
    assert Config.__dataclass_fields__["option_strike_max_moneyness_pct"].default == 10.0
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    monkeypatch.setenv("OPTION_STRIKE_SNAP", "off")
    monkeypatch.setenv("OPTION_STRIKE_MAX_MONEYNESS_PCT", "7.5")
    cfg = load_config()
    assert cfg.option_strike_snap is False and cfg.option_strike_max_moneyness_pct == 7.5
    monkeypatch.delenv("OPTION_STRIKE_SNAP")
    monkeypatch.delenv("OPTION_STRIKE_MAX_MONEYNESS_PCT")
    cfg = load_config()
    assert cfg.option_strike_snap is True and cfg.option_strike_max_moneyness_pct == 10.0
