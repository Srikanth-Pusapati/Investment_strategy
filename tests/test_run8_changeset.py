"""Tests for the run-8 change-set (Oct 26 2026) — items B-1 .. B-8, C-4/C-5,
C-9 and the new N-1/N-2 items from the run-7 close. Each block names the item
and the run-7 evidence that motivated it. Pure logic / fakes only — no
network, no broker. Watchdog items (N-1, B-7c) live in test_run8_watchdog.py;
feeds/scripts items (C-2, C-3, C-7) in test_run8_feeds.py."""
from __future__ import annotations

import os
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.config import RiskLimits
from investment_strategy.models import (
    AccountSnapshot, Action, Instrument, OptionLeg, OptionStrategy, Position,
    RiskVerdict, TradeProposal,
)
from investment_strategy.risk import RiskManager
from investment_strategy.state import PortfolioState

from test_risk import _limits as _base_limits, _opt_exp  # noqa: E402  (shared fixtures)


# ----------------------------------------------------------------- fixtures ---
def _state() -> PortfolioState:
    p = os.path.join(tempfile.gettempdir(), f"_run8_{uuid.uuid4().hex}.json")
    return PortfolioState(path=p)


def _rm(limits: RiskLimits, state: PortfolioState | None = None) -> RiskManager:
    return RiskManager(limits, kill_switch=False, state=state or _state())


def _limits(**over) -> RiskLimits:
    base = dict(min_add_interval_hours=0, kelly_fraction=0.0)   # kelly off -> cap path
    base.update(over)
    return _base_limits(**base)


def _acct(equity=100_000.0, positions=None) -> AccountSnapshot:
    return AccountSnapshot(equity=equity, last_equity=equity, cash=equity,
                           buying_power=equity, positions=positions or [])


def _pos(symbol, value, price=100.0) -> Position:
    qty = value / price
    return Position(symbol=symbol, qty=qty, avg_entry_price=price, current_price=price,
                    market_value=value, unrealized_pl=0.0, unrealized_pl_pct=0.0)


def _buy(symbol="LLY", conviction=0.66, weight=100.0, key_signals=None) -> TradeProposal:
    return TradeProposal(symbol=symbol, action=Action.BUY, conviction=conviction,
                         target_weight_pct=weight, rationale="test",
                         key_signals=key_signals or [])


def _opt(legs, symbol="AAPL") -> TradeProposal:
    return TradeProposal(symbol=symbol, action=Action.BUY, conviction=1.0,
                         target_weight_pct=2.0, rationale="opt",
                         instrument=Instrument.OPTION,
                         option_strategy=OptionStrategy.LONG_CALL, option_legs=legs)


def _call_leg():
    return [OptionLeg(expiry=_opt_exp(30), strike=200, right="call", side=Action.BUY)]


# --------------------------------------------------------------------------- #
# B-1 — TOPUP_AUTHORITY=events_only: a top-up needs an EVENT since the last
# buy, never a restated conviction (run-7: DBRG 0.60->0.66->0.72->0.77 on a
# falling composite; the 3-lot INTC alone lost $7,474).
# --------------------------------------------------------------------------- #
def _stamped_state(composite=1.50, price=100.0, families=("insider",), conviction=0.60):
    st = _state()
    st.register_buy("LLY", conviction=conviction, composite=composite, price=price,
                    families=list(families))
    return st


def _held():
    return _acct(positions=[_pos("LLY", 1_000.0)])


def test_b1_restated_conviction_alone_is_rejected():
    st = _stamped_state()
    rm = _rm(_limits(topup_authority="events_only", topup_min_conviction_delta=0.05), st)
    d = rm.evaluate(_buy(conviction=0.66), _held(), price=100.5, volatility=0.3,
                    composite_score=1.56, entry_families={"insider"})
    assert d.verdict is RiskVerdict.REJECTED
    assert d.reason.startswith("TOPUP AUTHORITY:"), d.reason
    assert "no event" in d.reason and "conviction delta +0.06 recorded" in d.reason
    assert d.topup_events == []


def test_b1_composite_up_is_an_event():
    rm = _rm(_limits(topup_authority="events_only"), _stamped_state(composite=1.50))
    d = rm.evaluate(_buy(conviction=0.60), _held(), price=100.0, volatility=0.3,
                    composite_score=1.81, entry_families={"insider"})
    assert d.verdict is not RiskVerdict.REJECTED, d.reason
    assert any(e.startswith("composite_up:") for e in d.topup_events), d.topup_events
    assert "Top-up authority:" in d.reason


def test_b1_small_composite_move_is_not_an_event():
    rm = _rm(_limits(topup_authority="events_only"), _stamped_state(composite=1.50))
    d = rm.evaluate(_buy(conviction=0.60), _held(), price=100.0, volatility=0.3,
                    composite_score=1.69, entry_families={"insider"})   # +0.19 < 0.25
    assert d.verdict is RiskVerdict.REJECTED
    assert "composite +1.50 -> +1.69 (< +0.25)" in d.reason, d.reason


def test_b1_price_gain_is_an_event():
    rm = _rm(_limits(topup_authority="events_only"), _stamped_state(price=100.0))
    d = rm.evaluate(_buy(conviction=0.60), _held(), price=103.0, volatility=0.3,
                    composite_score=1.50, entry_families={"insider"})
    assert d.verdict is not RiskVerdict.REJECTED, d.reason
    assert any(e.startswith("price_gain:+3.0%") for e in d.topup_events), d.topup_events


def test_b1_new_family_is_an_event_only_when_the_prior_cited_one():
    rm = _rm(_limits(topup_authority="events_only"), _stamped_state(families=("insider",)))
    d = rm.evaluate(_buy(conviction=0.60), _held(), price=100.0, volatility=0.3,
                    composite_score=1.50, entry_families={"insider", "news"})
    assert d.verdict is not RiskVerdict.REJECTED, d.reason
    assert "new_family:news" in d.topup_events
    # A prior stamp with NO families cannot mint events from any family now.
    rm2 = _rm(_limits(topup_authority="events_only"), _stamped_state(families=()))
    d2 = rm2.evaluate(_buy(conviction=0.60), _held(), price=100.0, volatility=0.3,
                      composite_score=1.50, entry_families={"news"})
    assert d2.verdict is RiskVerdict.REJECTED


def test_b1_no_prior_stamp_fails_open_once():
    rm = _rm(_limits(topup_authority="events_only"), _state())    # never stamped
    d = rm.evaluate(_buy(conviction=0.50), _held(), price=100.0, volatility=0.3)
    assert d.verdict is not RiskVerdict.REJECTED, d.reason
    assert "first stamped lot" in d.reason


def test_b1_fresh_entry_is_not_a_top_up():
    rm = _rm(_limits(topup_authority="events_only"), _stamped_state())
    d = rm.evaluate(_buy(conviction=0.50), _acct(), price=100.0, volatility=0.3)
    assert d.verdict is not RiskVerdict.REJECTED, d.reason
    assert d.topup_events == []


def test_b1_legacy_mode_keeps_the_conviction_gate():
    st = _state()
    st.register_buy("LLY", conviction=0.60)
    rm = _rm(_limits(topup_authority="legacy", topup_min_conviction_delta=0.05), st)
    d = rm.evaluate(_buy(conviction=0.60), _held(), price=100.0, volatility=0.3)
    assert d.verdict is RiskVerdict.REJECTED
    assert "top-up conviction" in d.reason.lower()


def test_b1_lot_multiple_clamps_the_add_to_the_held_value():
    # Held $1,000; a 5% ($5k) add is clamped to 1.0x held = $1,000.
    rm = _rm(_limits(topup_max_multiple_of_held=1.0, topup_min_conviction_delta=0.0,
                     max_position_pct=5.0, max_symbol_exposure_pct=10.0))
    d = rm.evaluate(_buy(), _held(), price=100.0, volatility=0.3)
    assert d.verdict is not RiskVerdict.REJECTED, d.reason
    assert abs(d.approved_notional - 1_000.0) < 1e-6, d.approved_notional
    assert "lot clamped to 1x held" in d.reason


def test_b1_lot_multiple_off_at_zero_and_inert_for_starters():
    rm = _rm(_limits(topup_max_multiple_of_held=0.0, topup_min_conviction_delta=0.0,
                     max_position_pct=5.0))
    d = rm.evaluate(_buy(), _held(), price=100.0, volatility=0.3)
    assert abs(d.approved_notional - 5_000.0) < 1e-6
    rm2 = _rm(_limits(topup_max_multiple_of_held=1.0, max_position_pct=5.0))
    d2 = rm2.evaluate(_buy(), _acct(), price=100.0, volatility=0.3)   # starter
    assert abs(d2.approved_notional - 5_000.0) < 1e-6


def test_state_buy_meta_counts_lots_and_clears_on_exit():
    st = _state()
    st.register_buy("LLY", conviction=0.60, composite=1.0, price=100.0, families=["insider"])
    st.register_buy("LLY", conviction=0.66, composite=1.3, price=103.0, families=["insider"],
                    events=["price_gain:+3.0%"])
    m = st.last_buy_meta_for("LLY")
    assert m["n"] == 2 and m["events"] == ["price_gain:+3.0%"] and m["price"] == 103.0
    # persisted
    again = PortfolioState(path=st.path)
    assert again.last_buy_meta_for("LLY")["n"] == 2
    st.register_exit("LLY", pl_pct=-2.0)
    assert st.last_buy_meta_for("LLY") is None
    st.register_buy("LLY", conviction=0.60)
    assert st.last_buy_meta_for("LLY")["n"] == 1


# --------------------------------------------------------------------------- #
# B-2 — MAX_TOP3_SATELLITE_PCT: the three largest satellites together stay
# under the cap (run-7: 38% of equity in three names, DBRG 18% > the core).
# --------------------------------------------------------------------------- #
def test_b2_buy_that_breaches_the_top3_cap_is_resized_to_the_headroom():
    acct = _acct(positions=[_pos("A", 20_000.0), _pos("B", 9_000.0), _pos("C", 8_000.0)])
    rm = _rm(_limits(max_top3_satellite_pct=30.0, max_position_pct=10.0))
    d = rm.evaluate(_buy("D"), acct, price=100.0, volatility=0.3)
    assert d.verdict is RiskVerdict.RESIZED, d.reason
    # top-2 others 29k + D 10k = 39% > 30% -> room 1k
    assert abs(d.approved_notional - 1_000.0) < 1e-6, d.approved_notional
    assert "Bucket cap" in d.reason


def test_b2_buy_outside_the_top3_is_unconstrained():
    acct = _acct(positions=[_pos("A", 20_000.0), _pos("B", 9_000.0), _pos("C", 8_000.0)])
    rm = _rm(_limits(max_top3_satellite_pct=30.0, max_position_pct=5.0))
    d = rm.evaluate(_buy("D"), acct, price=100.0, volatility=0.3)     # $5k < third ($8k)
    assert abs(d.approved_notional - 5_000.0) < 1e-6, d.reason


def test_b2_no_headroom_rejects_with_the_handle():
    acct = _acct(positions=[_pos("A", 20_000.0), _pos("B", 10_000.0)])
    rm = _rm(_limits(max_top3_satellite_pct=30.0, max_position_pct=10.0))
    d = rm.evaluate(_buy("D"), acct, price=100.0, volatility=0.3)
    assert d.verdict is RiskVerdict.REJECTED
    assert d.reason.startswith("BUCKET CAP:"), d.reason


def test_b2_system_managed_and_option_rows_do_not_count():
    opt = Position(symbol="HYG261120P00078000", qty=37.0, avg_entry_price=1.26,
                   current_price=0.44, market_value=1_628.0, unrealized_pl=-3_034.0,
                   unrealized_pl_pct=-65.0)
    acct = _acct(positions=[_pos("QQQ", 50_000.0), _pos("A", 12_000.0), _pos("B", 9_000.0), opt])
    rm = _rm(_limits(max_top3_satellite_pct=30.0, max_position_pct=10.0,
                     slot_exempt_symbols=("QQQ",)))
    d = rm.evaluate(_buy("C"), acct, price=100.0, volatility=0.3)
    # QQQ ignored: top-2 others 21k + C 10k = 31% -> room 9k
    assert abs(d.approved_notional - 9_000.0) < 1e-6, (d.approved_notional, d.reason)


def test_b2_off_at_zero():
    acct = _acct(positions=[_pos("A", 20_000.0), _pos("B", 10_000.0)])
    rm = _rm(_limits(max_top3_satellite_pct=0.0, max_position_pct=10.0))
    d = rm.evaluate(_buy("D"), acct, price=100.0, volatility=0.3)
    assert abs(d.approved_notional - 10_000.0) < 1e-6, d.reason


# --------------------------------------------------------------------------- #
# B-6 — a quote-less leg is refused as 'no NBBO on feed', not as a net credit
# (run-7: IWM Nov-21 weekly 10x, STE 9x, LH 3x).
# --------------------------------------------------------------------------- #
def test_b6_quoteless_leg_is_an_honest_reject():
    rm = _rm(_limits(options_enabled=True))
    d = rm.evaluate_option(_opt(_call_leg()), _acct(), est_premium_per_contract=0.0,
                           leg_liquidity=[{"symbol": "AAPL261121C00200000", "oi": 500,
                                           "rel_spread_pct": None}])
    assert d.verdict is RiskVerdict.REJECTED
    assert d.reason.startswith("no NBBO on feed for AAPL261121C00200000"), d.reason
    assert "net credit" not in d.reason.lower().split("not a")[0]


def test_b6_real_net_credit_keeps_the_old_text():
    rm = _rm(_limits(options_enabled=True))
    d = rm.evaluate_option(_opt(_call_leg()), _acct(), est_premium_per_contract=-1.0,
                           leg_liquidity=[{"symbol": "X", "oi": 500, "rel_spread_pct": 3.0}])
    assert d.verdict is RiskVerdict.REJECTED
    assert d.reason.startswith("Net credit"), d.reason


# --------------------------------------------------------------------------- #
# B-7a — the entry spread cap is never looser than the watchdog's exit cap
# (run-7: HYG 78.5P admitted at <= 10%, unmarkable for 13 sessions).
# --------------------------------------------------------------------------- #
def test_b7a_entry_cap_is_clamped_to_the_exit_cap():
    rm = _rm(_limits(options_enabled=True, max_option_spread_pct=20.0,
                     option_exit_max_spread_pct=10.0))
    d = rm.evaluate_option(_opt(_call_leg()), _acct(), est_premium_per_contract=2.0,
                           leg_liquidity=[{"symbol": "X", "oi": 5_000, "rel_spread_pct": 15.0}])
    assert d.verdict is RiskVerdict.REJECTED
    assert "10.0% cap" in d.reason, d.reason


# --------------------------------------------------------------------------- #
# B-7b — OPTION_OI_PER_CONTRACT: contracts <= thinnest-leg OI / ratio.
# --------------------------------------------------------------------------- #
def test_b7b_lot_is_clamped_to_the_oi_ratio():
    rm = _rm(_limits(options_enabled=True, max_option_premium_pct=1.0,
                     per_underlying_premium_pct=0.0, option_oi_per_contract=20.0,
                     min_option_open_interest=50.0))
    # $1,000 budget / $200 per contract = 5; thinnest OI 60 / 20 = 3
    d = rm.evaluate_option(_opt(_call_leg()), _acct(), est_premium_per_contract=2.0,
                           leg_liquidity=[{"symbol": "X", "oi": 60.0, "rel_spread_pct": 2.0},
                                          {"symbol": "Y", "oi": 900.0, "rel_spread_pct": 2.0}])
    assert d.verdict is RiskVerdict.APPROVED, d.reason
    assert d.approved_qty == 3.0, d.approved_qty
    assert "Lot cap: 5 -> 3 contracts" in d.reason, d.reason


def test_b7b_rejects_when_not_even_one_contract_clears():
    rm = _rm(_limits(options_enabled=True, max_option_premium_pct=1.0,
                     per_underlying_premium_pct=0.0, option_oi_per_contract=20.0,
                     min_option_open_interest=0.0))
    d = rm.evaluate_option(_opt(_call_leg()), _acct(), est_premium_per_contract=2.0,
                           leg_liquidity=[{"symbol": "X", "oi": 15.0, "rel_spread_pct": 2.0}])
    assert d.verdict is RiskVerdict.REJECTED
    assert d.reason.startswith("OPTION LOT CAP:"), d.reason


def test_b7b_off_at_zero_and_inert_without_oi_data():
    rm = _rm(_limits(options_enabled=True, max_option_premium_pct=1.0,
                     per_underlying_premium_pct=0.0, option_oi_per_contract=0.0,
                     min_option_open_interest=50.0))
    d = rm.evaluate_option(_opt(_call_leg()), _acct(), est_premium_per_contract=2.0,
                           leg_liquidity=[{"symbol": "X", "oi": 60.0, "rel_spread_pct": 2.0}])
    assert d.approved_qty == 5.0, d.reason
    rm2 = _rm(_limits(options_enabled=True, max_option_premium_pct=1.0,
                      per_underlying_premium_pct=0.0, option_oi_per_contract=20.0))
    d2 = rm2.evaluate_option(_opt(_call_leg()), _acct(), est_premium_per_contract=2.0,
                             leg_liquidity=[{"symbol": "X", "oi": None, "rel_spread_pct": 2.0}])
    assert d2.approved_qty == 5.0, d2.reason
