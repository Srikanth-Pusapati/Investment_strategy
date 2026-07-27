"""Tests for the signal-attribution / reflection loop.

Pure logic, no network: build synthetic ledger records and assert round-trips
reconstruct correctly, per-source stats aggregate, and the rendered lessons block
behaves (terse, gated on enough history).

Runnable two ways:
    .venv/bin/python tests/test_attribution.py     # standalone, no pytest
    .venv/bin/pytest tests/                          # if pytest is installed
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.attribution import (
    attribute, behavior_diagnostics, render_lessons, round_trips,
)
from investment_strategy.ledger import TradeLedger, TradeRecord

_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _buy(symbol, signals, t, qty=0.0):
    return TradeRecord(symbol=symbol, action="buy", entry_signals=signals,
                       qty=qty, ts=_T0 + timedelta(hours=t))


def _sell(symbol, pl_pct, t, reason="decision", qty=0.0):
    return TradeRecord(symbol=symbol, action="sell", realized_pl_pct=pl_pct,
                       exit_reason=reason, qty=qty, ts=_T0 + timedelta(hours=t))


def test_round_trip_basic():
    recs = [_buy("AAPL", ["technical", "fundamentals"], 0), _sell("AAPL", 5.0, 1)]
    trips = round_trips(recs)
    assert len(trips) == 1
    assert trips[0].pl_pct == 5.0
    assert trips[0].signals == ["fundamentals", "technical"]   # sorted union


def test_round_trip_requires_outcome():
    # A sell with no realized P&L (old record) is not attributable.
    recs = [_buy("AAPL", ["technical"], 0),
            TradeRecord(symbol="AAPL", action="sell", ts=_T0 + timedelta(hours=1))]
    assert round_trips(recs) == []


def test_round_trip_orders_by_time():
    # Records out of order still pair correctly once sorted by ts.
    recs = [_sell("MSFT", -2.0, 3), _buy("MSFT", ["news"], 2)]
    trips = round_trips(recs)
    assert len(trips) == 1 and trips[0].pl_pct == -2.0


def test_scale_in_unions_signals():
    recs = [_buy("NVDA", ["technical"], 0), _buy("NVDA", ["congress"], 1),
            _sell("NVDA", 3.0, 2)]
    trips = round_trips(recs)
    assert trips[0].signals == ["congress", "technical"]


def test_sell_without_open_is_ignored():
    # An exit with no matching open buy emits NO trip at all (no crash): it can't
    # be attributed, and counting it would double duplicate retry rows (the T
    # option flatten of 2026-07-23 was ledgered twice) in every overall stat.
    assert round_trips([_sell("TSLA", 1.0, 0)]) == []


def test_scale_out_partial_keeps_remainder_attributed():
    # 1B.8: a scale-out sells half at +12%, the rest exits later at +20%. BOTH
    # trips must attribute to the entry signal — the remainder isn't orphaned.
    recs = [
        _buy("NVDA", ["congress"], 0, qty=2.0),
        _sell("NVDA", 12.0, 1, reason="scale", qty=1.0),   # partial
        _sell("NVDA", 20.0, 2, reason="trail", qty=1.0),   # remainder, full close
    ]
    trips = round_trips(recs)
    assert len(trips) == 2
    assert all(t.signals == ["congress"] for t in trips)   # neither is signal-less


def test_regime_trim_partial_is_not_a_full_close():
    # 1B.6: a 25% regime trim must not flatten the attribution for the rest.
    recs = [
        _buy("AAPL", ["technical"], 0, qty=4.0),
        _sell("AAPL", -2.0, 1, reason="regime_trim", qty=1.0),  # partial
        _sell("AAPL", -5.0, 2, reason="stop", qty=3.0),         # remainder
    ]
    trips = round_trips(recs)
    assert len(trips) == 2
    assert all(t.signals == ["technical"] for t in trips)


def test_full_close_still_flattens_all_lots():
    # An exit whose qty covers the whole open total clears the position; a later
    # sell with nothing open is a duplicate/orphan and emits no trip.
    recs = [
        _buy("MSFT", ["news"], 0, qty=3.0),
        _sell("MSFT", 4.0, 1, reason="take", qty=3.0),
        _sell("MSFT", 9.0, 2, reason="decision", qty=1.0),  # nothing open -> skipped
    ]
    trips = round_trips(recs)
    assert len(trips) == 1
    assert trips[0].signals == ["news"]


def test_multi_lot_flatten_split_fills_both_attributed():
    # BRK.B 2026-07-23: a flatten of a 2-lot position lands as two sell rows
    # seconds apart (7 sh then 16 sh). The reason-based rule popped everything
    # on the first row and orphaned the second; the qty-aware close must keep
    # the remainder open so BOTH trips attribute.
    recs = [
        _buy("BRK.B", ["fundamentals"], 0, qty=16.0),
        _buy("BRK.B", ["congress"], 1, qty=7.0),
        _sell("BRK.B", -1.1, 2, reason="flatten", qty=7.0),   # split fill 1
        _sell("BRK.B", -1.1, 3, reason="flatten", qty=16.0),  # split fill 2
    ]
    trips = round_trips(recs)
    assert len(trips) == 2
    assert trips[0].signals == ["congress", "fundamentals"]  # both lots still open
    # FIFO consumed 7 sh of the OLDEST lot (16-sh fundamentals), so the second
    # fill still sees 9 sh fundamentals + 7 sh congress — fully attributed.
    assert trips[1].signals == ["congress", "fundamentals"]


def test_duplicate_full_close_not_double_counted():
    # The same -100% option close recorded twice (flatten retry) must count ONCE.
    recs = [
        _buy("T", ["insider"], 0, qty=900.0),
        _sell("T", -100.0, 1, reason="flatten", qty=900.0),
        _sell("T", -100.0, 2, reason="flatten", qty=900.0),  # duplicate row
    ]
    trips = round_trips(recs)
    assert len(trips) == 1
    assert trips[0].signals == ["insider"]


def test_attribute_winrate_and_avg():
    recs = [
        _buy("A", ["technical"], 0), _sell("A", 10.0, 1),     # win
        _buy("B", ["technical"], 2), _sell("B", -4.0, 3),     # loss
        _buy("C", ["congress"], 4), _sell("C", -1.0, 5),      # loss
    ]
    stats = attribute(round_trips(recs))
    assert stats["technical"].trips == 2
    assert stats["technical"].wins == 1
    assert stats["technical"].win_rate == 0.5
    assert abs(stats["technical"].avg_pl_pct - 3.0) < 1e-9
    assert stats["congress"].win_rate == 0.0


def _ledger_with(recs) -> TradeLedger:
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    led = TradeLedger(path)
    for r in recs:
        led.record(r)
    return led


def test_render_empty_when_no_history():
    led = _ledger_with([])
    assert render_lessons(led) == ""


def test_render_gates_on_min_source_trips():
    # One trip per source -> below the default min_source_trips (2) -> empty.
    led = _ledger_with([_buy("A", ["technical"], 0), _sell("A", 5.0, 1)])
    assert render_lessons(led) == ""


def test_render_includes_source_lines():
    recs = [
        _buy("A", ["technical"], 0), _sell("A", 6.0, 1),
        _buy("B", ["technical"], 2), _sell("B", 2.0, 3),
        _buy("C", ["congress"], 4), _sell("C", -3.0, 5),
        _buy("D", ["congress"], 6), _sell("D", -1.0, 7),
    ]
    out = render_lessons(_ledger_with(recs))
    assert "Track record" in out
    assert "technical:" in out and "congress:" in out
    # Best performer (technical, +4% avg) ranks above the loser (congress).
    assert out.index("technical:") < out.index("congress:")


def test_round_trip_carries_mean_entry_conviction():
    from investment_strategy.attribution import round_trips as rt
    recs = [
        TradeRecord(symbol="A", action="buy", qty=1.0, conviction=0.4,
                    entry_signals=["technical"], ts=_T0),
        TradeRecord(symbol="A", action="buy", qty=1.0, conviction=0.6,
                    entry_signals=["technical"], ts=_T0 + timedelta(hours=1)),
        _sell("A", 5.0, 2),
    ]
    trips = rt(recs)
    assert len(trips) == 1
    assert abs(trips[0].conviction - 0.5) < 1e-9


def test_round_trip_conviction_none_when_unrecorded():
    # conviction 0.0 means "not recorded" (core fills, pre-tracking rows).
    recs = [_buy("A", ["technical"], 0), _sell("A", 5.0, 1)]
    assert round_trips(recs)[0].conviction is None


def test_conviction_calibration_buckets_and_min_trips():
    from investment_strategy.attribution import RoundTrip, conviction_calibration
    trips = (
        [RoundTrip("X", +3.0, [], conviction=0.3) for _ in range(3)]
        + [RoundTrip("Y", -5.0, [], conviction=0.7) for _ in range(3)]
        + [RoundTrip("Z", +9.0, [], conviction=0.5)]          # n=1 -> suppressed
        + [RoundTrip("W", +9.0, [], conviction=None)]         # unknown -> ignored
    )
    lines = conviction_calibration(trips, min_trips=3)
    joined = "\n".join(lines)
    assert "conviction 0.2-0.4: 3 trades, 100% win, +3.0% avg" in joined
    assert "conviction 0.6+: 3 trades, 0% win, -5.0% avg" in joined
    assert "0.4-0.6" not in joined
    # High-conviction losing to low-conviction -> the inversion flag leads.
    assert lines[0].startswith("CONVICTION INVERTED")


def test_conviction_calibration_no_inversion_when_high_wins():
    from investment_strategy.attribution import RoundTrip, conviction_calibration
    trips = (
        [RoundTrip("X", -2.0, [], conviction=0.3) for _ in range(3)]
        + [RoundTrip("Y", +6.0, [], conviction=0.8) for _ in range(3)]
    )
    lines = conviction_calibration(trips, min_trips=3)
    assert lines and not lines[0].startswith("CONVICTION INVERTED")


def test_render_lessons_includes_calibration_block():
    from investment_strategy.ledger import TradeLedger
    led = TradeLedger.__new__(TradeLedger)
    recs = []
    for i, (conv, pl) in enumerate([(0.7, -5.0)] * 3 + [(0.3, 3.0)] * 3):
        sym = f"S{i}"
        recs.append(TradeRecord(symbol=sym, action="buy", qty=1.0,
                                conviction=conv, entry_signals=["technical"],
                                ts=_T0 + timedelta(hours=2 * i)))
        recs.append(_sell(sym, pl, 2 * i + 1))
    led.effective = lambda: recs  # type: ignore[method-assign]
    out = render_lessons(led)
    assert "Conviction calibration" in out
    assert "CONVICTION INVERTED" in out


# -- behavior diagnostics: disposition effect + overtrading ------------------- #
def _lot_buy(symbol, entry, day):
    return TradeRecord(symbol=symbol, action="buy", qty=1.0, entry_price=entry,
                       cost_usd=entry, order_id=f"b-{symbol}",
                       ts=_T0 + timedelta(days=day))


def _lot_sell(symbol, price, day):
    return TradeRecord.for_sell(symbol, "exit", f"s-{symbol}", qty=1.0,
                                exit_price=price, exit_reason="decision",
                                ts=_T0 + timedelta(days=day))


def _ledger_of(recs):
    led = TradeLedger(path=tempfile.mktemp())
    led.effective = lambda: recs  # type: ignore[method-assign]
    return led


def test_behavior_diagnostics_flags_disposition_effect():
    # 3 winners held ~1 day, 3 losers held ~10 days -> losers held far longer,
    # so the disposition-effect flag fires. (Distinct symbols so FIFO lots don't
    # interfere; all buys on day 0 so the overtrading line stays suppressed.)
    recs = []
    for i in range(3):
        s = f"WIN{i}"
        recs += [_lot_buy(s, 100.0, 0), _lot_sell(s, 110.0, 1)]     # +10%, 1d
    for i in range(3):
        s = f"LOSE{i}"
        recs += [_lot_buy(s, 100.0, 0), _lot_sell(s, 90.0, 10)]     # -10%, 10d
    lines = behavior_diagnostics(_ledger_of(recs))
    text = "\n".join(lines)
    assert "DISPOSITION EFFECT" in text
    assert "winners 1.0d" in text and "losers 10.0d" in text


def test_behavior_diagnostics_suppressed_below_min_trips():
    # Only 2 winners / 2 losers -> below min_trips (3): no disposition line, and
    # all buys land on one day so no overtrading line either -> empty block.
    recs = []
    for i in range(2):
        recs += [_lot_buy(f"W{i}", 100.0, 0), _lot_sell(f"W{i}", 110.0, 1)]
        recs += [_lot_buy(f"L{i}", 100.0, 0), _lot_sell(f"L{i}", 90.0, 5)]
    assert behavior_diagnostics(_ledger_of(recs)) == []


# -- episode-opening conviction/composite (weekly auto-tuner inputs) --------- #
def test_opening_conviction_survives_partial_exit_and_topup():
    # Buy opens at 0.45, a later top-up buys more at 0.70 — a scale-out consumes
    # the OPENING lot FIFO, so both resulting trips' opening_conviction must
    # stay 0.45 (the entry's own number), never drift to the top-up's.
    recs = [
        TradeRecord(symbol="SPCX", action="buy", qty=1.0, conviction=0.45,
                    composite_score=0.66, ts=_T0),
        TradeRecord(symbol="SPCX", action="buy", qty=1.0, conviction=0.70,
                    composite_score=1.10, ts=_T0 + timedelta(hours=1)),
        _sell("SPCX", 3.0, 2, reason="scale", qty=1.0),   # consumes the OPENING lot
        _sell("SPCX", -8.0, 3, reason="stop", qty=1.0),   # remainder, full close
    ]
    trips = round_trips(recs)
    assert len(trips) == 2
    assert all(abs(t.opening_conviction - 0.45) < 1e-9 for t in trips)
    assert all(abs(t.opening_composite - 0.66) < 1e-9 for t in trips)


def test_opening_conviction_none_when_opener_unrecorded():
    # conviction 0.0 (core fills, pre-tracking) -> None, same sentinel as the
    # existing mean-conviction field.
    recs = [_buy("A", ["technical"], 0), _sell("A", 5.0, 1)]
    trip = round_trips(recs)[0]
    assert trip.opening_conviction is None
    assert trip.opening_composite is None


def test_opening_conviction_resets_across_episodes():
    # A full close then a fresh re-entry: the SECOND episode's opening numbers
    # must be its OWN entry, not the first episode's leftover.
    recs = [
        TradeRecord(symbol="A", action="buy", qty=1.0, conviction=0.40, ts=_T0),
        _sell("A", 5.0, 1, reason="take", qty=1.0),
        TradeRecord(symbol="A", action="buy", qty=1.0, conviction=0.80,
                    ts=_T0 + timedelta(hours=2)),
        _sell("A", -3.0, 3, reason="stop", qty=1.0),
    ]
    trips = round_trips(recs)
    assert len(trips) == 2
    assert abs(trips[0].opening_conviction - 0.40) < 1e-9
    assert abs(trips[1].opening_conviction - 0.80) < 1e-9


def test_realized_pl_and_exit_ts_carried():
    ts_exit = _T0 + timedelta(hours=1)
    recs = [
        TradeRecord(symbol="A", action="buy", qty=1.0, ts=_T0),
        TradeRecord(symbol="A", action="sell", realized_pl_pct=5.0,
                    realized_pl=123.45, ts=ts_exit),
    ]
    trip = round_trips(recs)[0]
    assert trip.realized_pl == 123.45
    assert trip.exit_ts == str(ts_exit)


def test_new_roundtrip_fields_default_for_backcompat():
    from investment_strategy.attribution import RoundTrip
    trip = RoundTrip(symbol="X", pl_pct=1.0, signals=[])
    assert trip.opening_conviction is None
    assert trip.opening_composite is None
    assert trip.realized_pl is None
    assert trip.exit_ts == ""


# -- cited-signal parsing + cited attribution basis (Jul-24 prune) ----------- #
def test_parse_cited_real_ledger_strings():
    from investment_strategy.attribution import parse_cited
    # Verbatim citations from state/trades.jsonl — the parser must cover them.
    assert parse_cited(["insider Form4 +1.00"]) == {"insider"}
    assert parse_cited(["technical MACD bull"]) == {"technical"}
    assert parse_cited(["news MS upgrade"]) == {"news"}
    assert parse_cited(["options_chain +0.39"]) == {"options_chain"}
    assert parse_cited(["govcontracts +0.40"]) == {"govcontracts"}
    assert parse_cited(["$66.2M federal contracts"]) == {"govcontracts"}
    assert parse_cited(["fundamentals rev +42.5%"]) == {"fundamentals"}
    assert parse_cited(["composite +1.18"]) == {"composite"}
    assert parse_cited(["offexchange short pressure 71% (bearish caveat)"]) == {"offexchange"}


def test_parse_cited_flow_variants_and_news_prefix_suppression():
    from investment_strategy.attribution import parse_cited
    # The flow signal emits under kind=news, so citations often carry a "news"
    # prefix — that prefix must NOT credit the news source.
    assert parse_cited(["news options flow C/P +0.76"]) == {"options_flow"}
    assert parse_cited(["call/put flow +0.76"]) == {"options_flow"}
    assert parse_cited(["flow C/P +0.45"]) == {"options_flow"}
    assert parse_cited(["options flow +0.41"]) == {"options_flow"}
    # ...but a genuinely separate news citation still counts.
    assert parse_cited(["news sentiment +0.32", "options flow +0.41"]) == {
        "news", "options_flow",
    }


def test_parse_cited_weak_metric_words_lose_to_named_source():
    from investment_strategy.attribution import parse_cited
    # SMCI 2026-07-22: a news citation that merely MENTIONS margins/earnings —
    # crediting fundamentals for it would launder the news number.
    assert parse_cited(["news: AI margin windfall earnings surprise"]) == {"news"}
    # GOOGL 2026-07-16: flow citation in the LLM's news-prefixed flow idiom.
    assert parse_cited(["news flow +0.61 call imbalance"]) == {"options_flow"}
    # CVX 2026-07-17: one string naming BOTH flow and the chain read.
    assert parse_cited(["options flow +0.62 bullish chain"]) == {
        "options_flow", "options_chain",
    }
    # Metric words still stand alone when no source is named.
    assert parse_cited(["bullish MACD with RSI 63"]) == {"technical"}


def test_parse_cited_multi_source_and_unparsed():
    from investment_strategy.attribution import parse_cited
    # One citation naming two sources credits both.
    assert parse_cited(["congress+insider net buying"]) == {"congress", "insider"}
    # Unparseable text contributes nothing (and never crashes).
    assert parse_cited(["vibes are good"]) == set()
    assert parse_cited(None) == set()


def test_attribute_cited_basis_with_presence_fallback():
    recs = [
        # Cited trade: insider decisive; technical merely present.
        TradeRecord(symbol="A", action="buy", qty=1.0,
                    entry_signals=["insider", "technical"],
                    key_signals=["insider Form4 +1.00"], ts=_T0),
        _sell("A", -10.0, 1, qty=1.0),
        # Nothing cited (core fill): falls back to presence.
        TradeRecord(symbol="B", action="buy", qty=1.0,
                    entry_signals=["core_fill"], ts=_T0 + timedelta(hours=2)),
        _sell("B", 2.0, 3, qty=1.0),
    ]
    trips = round_trips(recs)
    cited = attribute(trips, basis="cited")
    assert cited["insider"].trips == 1 and cited["insider"].avg_pl_pct == -10.0
    assert "technical" not in cited            # present but never cited
    assert cited["core_fill"].trips == 1       # fallback keeps the trip counted
    present = attribute(trips, basis="present")
    assert present["technical"].trips == 1     # presence basis still sees it


def test_qty_less_sell_flattens_and_resets_episode():
    # A sell ledgered WITHOUT a qty (pre-tracking rows, older sell paths) must
    # flatten: if it didn't, phantom lots would leak stale signals and a frozen
    # opening_conviction into the next episode's trips.
    recs = [
        TradeRecord(symbol="A", action="buy", qty=3.0, conviction=0.4,
                    entry_signals=["insider"], ts=_T0),
        _sell("A", -2.0, 1, qty=0.0),                       # no qty -> flatten
        TradeRecord(symbol="A", action="buy", qty=2.0, conviction=0.8,
                    entry_signals=["news"], ts=_T0 + timedelta(hours=2)),
        _sell("A", 5.0, 3, qty=2.0),
    ]
    trips = round_trips(recs)
    assert len(trips) == 2
    assert trips[1].signals == ["news"]        # no stale lot from episode 1
    assert trips[1].n_lots == 1
    assert abs(trips[1].opening_conviction - 0.8) < 1e-9


def test_full_close_tolerates_fill_vs_requested_dust():
    # Buys ledger the REQUESTED/estimated qty, sells the ACTUAL fill — live
    # deltas run up to ~2e-3 sh (QQQ core-fill flatten: buy 40.639472 vs fills
    # 0.637409 + 40.0). A sell within 1% of the open total must FLATTEN, not
    # leave a phantom dust lot that pollutes the next episode.
    recs = [
        TradeRecord(symbol="QQQ", action="buy", qty=40.639472,
                    entry_signals=["core_fill"], ts=_T0),
        _sell("QQQ", -3.4, 1, reason="flatten", qty=0.637409),   # split fill 1
        _sell("QQQ", -3.4, 2, reason="flatten", qty=40.0),       # split fill 2
        TradeRecord(symbol="QQQ", action="buy", qty=5.0, conviction=0.7,
                    entry_signals=["technical"], ts=_T0 + timedelta(hours=3)),
        _sell("QQQ", 2.0, 4, qty=5.0),
    ]
    trips = round_trips(recs)
    assert len(trips) == 3
    assert trips[2].signals == ["technical"]   # fresh episode, no dust lot
    assert trips[2].n_lots == 1
    assert abs(trips[2].opening_conviction - 0.7) < 1e-9


def test_genuine_partial_still_partial_under_relative_tolerance():
    # A 50% trim is nowhere near the 1% full-close band and must stay partial.
    recs = [
        _buy("NVDA", ["congress"], 0, qty=2.0),
        _sell("NVDA", 12.0, 1, reason="scale", qty=1.0),
        _sell("NVDA", 20.0, 2, reason="trail", qty=1.0),
    ]
    trips = round_trips(recs)
    assert len(trips) == 2
    assert all(t.signals == ["congress"] for t in trips)


def test_option_and_equity_lots_never_commingle():
    # Option rows ledger under the bare ticker with qty in CONTRACTS. A 9-lot
    # option flatten must not be treated as a partial sale of 381 mixed units,
    # must not consume equity shares, and must attribute ONLY to the option
    # entry's signals.
    recs = [
        TradeRecord(symbol="T", action="buy", instrument="equity", qty=372.0,
                    conviction=0.5, entry_signals=["fundamentals"], ts=_T0),
        TradeRecord(symbol="T", action="buy", instrument="option", qty=9.0,
                    conviction=0.7, entry_signals=["insider"],
                    ts=_T0 + timedelta(hours=1)),
        TradeRecord(symbol="T", action="sell", instrument="option", qty=9.0,
                    realized_pl_pct=-100.0, exit_reason="flatten",
                    ts=_T0 + timedelta(hours=2)),
        TradeRecord(symbol="T", action="sell", instrument="equity", qty=372.0,
                    realized_pl_pct=4.9, exit_reason="trail",
                    ts=_T0 + timedelta(hours=3)),
    ]
    trips = round_trips(recs)
    assert len(trips) == 2
    assert trips[0].signals == ["insider"]       # option loss -> option entry only
    assert trips[0].n_lots == 1
    assert abs(trips[0].conviction - 0.7) < 1e-9  # no equity conviction mixed in
    assert trips[0].instrument == "option"
    assert trips[1].signals == ["fundamentals"]  # equity win -> equity entry only
    assert trips[1].instrument == "equity"


def test_duplicate_partial_sell_retry_not_double_counted():
    # A retry row identical to the PARTIAL sell before it (same qty/P&L/reason)
    # must not emit a second trip or consume more FIFO — the no-open-lots skip
    # alone can't catch it because lots legitimately remain after a partial.
    recs = [
        _buy("X", ["insider"], 0, qty=10.0),
        _sell("X", 8.0, 1, reason="scale", qty=6.0),
        _sell("X", 8.0, 2, reason="scale", qty=6.0),   # duplicate retry row
        _sell("X", 3.0, 3, reason="trail", qty=4.0),   # real close of the rest
    ]
    trips = round_trips(recs)
    assert [t.pl_pct for t in trips] == [8.0, 3.0]


def test_render_lessons_uses_cited_basis():
    # The prompt block must rank by CITED sources: technical rides along in the
    # bundle on every trade but is never cited, so it must not appear.
    recs = []
    for i in range(2):
        recs.append(TradeRecord(
            symbol=f"C{i}", action="buy", qty=1.0,
            entry_signals=["insider", "technical"],
            key_signals=["insider Form4 +1.00"],
            ts=_T0 + timedelta(hours=2 * i)))
        recs.append(_sell(f"C{i}", -6.0, 2 * i + 1, qty=1.0))
    led = _ledger_with(recs)
    out = render_lessons(led, min_source_trips=2)
    assert "- insider: 2 trades" in out
    assert "technical" not in out


def test_perf_weights_use_cited_basis():
    from investment_strategy.signals.composite import perf_weights
    recs = []
    for i in range(3):
        recs.append(TradeRecord(
            symbol=f"S{i}", action="buy", qty=1.0,
            entry_signals=["insider", "fundamentals"],
            key_signals=["insider Form4 +1.00"],
            ts=_T0 + timedelta(hours=2 * i)))
        recs.append(_sell(f"S{i}", -8.0, 2 * i + 1, qty=1.0))
    led = _ledger_with(recs)
    w = perf_weights(led)
    # insider was cited on all 3 losing trips -> clamped to the 0.5 floor
    # (1.0 + (-8.0)/10.0 = 0.2 -> clamp).
    assert abs(w["insider"] - 0.5) < 1e-9
    # fundamentals was only PRESENT, never cited -> no weight entry (inert 1.0).
    assert "fundamentals" not in w


# -- exit-discipline lesson (Jul-25 stop/target calibration) ----------------- #
def test_opening_stop_pct_carried_and_none_when_unset():
    recs = [
        TradeRecord(symbol="A", action="buy", qty=1.0, stop_loss_pct=6.0, ts=_T0),
        _sell("A", -2.0, 1, qty=1.0),
        _buy("B", ["technical"], 2),          # helper sets no stop -> None
        _sell("B", 1.0, 3),
    ]
    trips = round_trips(recs)
    assert trips[0].opening_stop_pct == 6.0
    assert trips[1].opening_stop_pct is None


def test_exit_discipline_lesson_flags_early_bails():
    from investment_strategy.attribution import exit_discipline_lesson
    recs = []
    for i, pl in enumerate((-1.0, -1.5, -2.0)):   # all < half the 6% stop
        recs.append(TradeRecord(symbol=f"E{i}", action="buy", qty=1.0,
                                stop_loss_pct=6.0, ts=_T0 + timedelta(hours=2 * i)))
        recs.append(_sell(f"E{i}", pl, 2 * i + 1, reason="decision", qty=1.0))
    trips = round_trips(recs)
    line = exit_discipline_lesson(trips)
    assert "3 of your decision-sells" in line
    assert "-1.5%" in line                        # the average


def test_exit_discipline_boundaries_exact_half_and_profits_excluded():
    from investment_strategy.attribution import exit_discipline_lesson
    recs = []
    # Exactly HALF the stop is NOT an early bail (strict bound) ...
    for i in range(3):
        recs.append(TradeRecord(symbol=f"H{i}", action="buy", qty=1.0,
                                stop_loss_pct=6.0, ts=_T0 + timedelta(hours=4 * i)))
        recs.append(_sell(f"H{i}", -3.0, 4 * i + 1, reason="decision", qty=1.0))
    # ... and PROFITABLE decision-sells never count as bails.
    for i in range(3):
        recs.append(TradeRecord(symbol=f"P{i}", action="buy", qty=1.0,
                                stop_loss_pct=6.0,
                                ts=_T0 + timedelta(hours=4 * i + 2)))
        recs.append(_sell(f"P{i}", +2.0, 4 * i + 3, reason="decision", qty=1.0))
    assert exit_discipline_lesson(round_trips(recs)) == ""


def test_exit_discipline_suppressed_below_min_and_for_real_stops():
    from investment_strategy.attribution import exit_discipline_lesson
    # A decision-sell near the FULL stop width is not an early bail.
    recs = [
        TradeRecord(symbol="A", action="buy", qty=1.0, stop_loss_pct=6.0, ts=_T0),
        _sell("A", -5.5, 1, reason="decision", qty=1.0),
    ]
    assert exit_discipline_lesson(round_trips(recs)) == ""


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {fn.__name__}: {e!r}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
