"""Tests for investment_strategy.autotune (weekly ledger-driven auto-tune).

Pure logic where possible: synthetic RoundTrip/TradeRecord/DecisionRecord
objects feed the sweep functions directly, so most tests don't touch disk.
A couple of integration tests exercise run_autotune end-to-end against a real
TradeLedger + DecisionJournal in a tmp dir, mirroring test_postmortem.py.

Runnable two ways:
    .venv/bin/python -m pytest tests/test_autotune.py
    .venv/bin/pytest tests/
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.attribution import RoundTrip
from investment_strategy.autotune import (
    _rotation_chains,
    _safe_parse_ts,
    run_autotune,
    sweep_min_conviction,
    sweep_new_name_floor,
    sweep_reentry_override,
    sweep_rotation_max_loss,
    sweep_rotation_release,
)
from investment_strategy.journal import DecisionJournal, DecisionRecord
from investment_strategy.ledger import TradeLedger, TradeRecord

_T0 = datetime(2026, 7, 20, tzinfo=timezone.utc)


def _trip(opening_conv, realized_pl, exit_ts="2026-07-20 10:00:00+00:00"):
    return RoundTrip(
        symbol="X", pl_pct=0.0, signals=[], opening_conviction=opening_conv,
        realized_pl=realized_pl, exit_ts=exit_ts,
    )


def _rejected(symbol, conv, reason):
    return DecisionRecord(
        ts="2026-07-20T10:00:00+00:00", symbol=symbol, action="buy",
        instrument="equity", conviction=conv, target_weight_pct=0.0,
        verdict="rejected", approved_notional=0.0, reason=reason,
    )


# -- new-name conviction floor sweep ----------------------------------------- #

def test_new_name_floor_blocks_losers_and_counts_missed_winners():
    trips = [
        _trip(0.45, -441.0),   # MU-style loser
        _trip(0.46, 30.0),     # NU-style small winner
        _trip(0.46, 15.0),     # SOFI-style small winner
        _trip(0.70, 200.0),    # clears every candidate, never blocked
    ]
    sw = sweep_new_name_floor(trips, [], current=0.5, candidates=(0.5,), min_sample=1)
    row = sw.rows[0]
    assert row.n_affected == 3
    assert row.losses_avoided == 441.0
    assert row.winners_missed == 45.0
    assert row.net == 396.0


def test_new_name_floor_ignores_unknown_opening_conviction():
    # opening_conviction=None (core fill / pre-tracking) never counts as blocked.
    trips = [_trip(None, -100.0)]
    sw = sweep_new_name_floor(trips, [], current=0.5, candidates=(0.9,), min_sample=1)
    assert sw.rows[0].n_affected == 0


def test_new_name_floor_journal_admits_count_only_looser_candidate():
    dr = _rejected(
        "AMD", 0.48,
        "Fresh-name conviction 0.48 below the new-position floor 0.50 — "
        "starter positions need better than coin-flip conviction.",
    )
    sw = sweep_new_name_floor([], [dr], current=0.5, candidates=(0.45, 0.55), min_sample=1)
    by_val = {r.value: r for r in sw.rows}
    assert by_val[0.45].admits_from_journal == 1   # looser than current -> counted
    assert by_val[0.55].admits_from_journal == 0   # stricter -> not counted
    assert by_val[0.45].net == 0.0                 # never priced


def test_new_name_floor_and_min_conviction_markers_dont_cross_count():
    dr_new_name = _rejected(
        "AMD", 0.48,
        "Fresh-name conviction 0.48 below the new-position floor 0.50 — "
        "starter positions need better than coin-flip conviction.",
    )
    dr_min_conv = _rejected("XOM", 0.15, "Conviction 0.15 below floor 0.20 — no real edge; skip.")
    sw_floor = sweep_new_name_floor(
        [], [dr_new_name, dr_min_conv], current=0.5, candidates=(0.45,), min_sample=1,
    )
    sw_conv = sweep_min_conviction(
        [], [dr_new_name, dr_min_conv], current=0.2, candidates=(0.10,), min_sample=1,
    )
    assert sw_floor.rows[0].admits_from_journal == 1
    assert sw_conv.rows[0].admits_from_journal == 1


# -- recommendation gating: sample size + one-sidedness ----------------------- #

def test_recommendation_suppressed_below_min_sample():
    trips = [_trip(0.45, -441.0)]  # n=1
    sw = sweep_new_name_floor(trips, [], current=0.5, candidates=(0.5,), min_sample=5)
    assert sw.recommendation is None


def test_recommendation_emitted_when_one_sided_and_sampled():
    trips = [_trip(0.45, -100.0) for _ in range(5)]  # 5 clean losers, no winners missed
    sw = sweep_new_name_floor(trips, [], current=0.3, candidates=(0.5,), min_sample=5)
    assert sw.recommendation is not None
    assert "0.5" in sw.recommendation


def test_recommendation_suppressed_when_not_one_sided():
    # losses_avoided=10 vs winners_missed=8 -> 10 < 2*8=16 -> not one-sided.
    trips = [_trip(0.45, -10.0), _trip(0.46, 8.0)]
    sw = sweep_new_name_floor(trips, [], current=0.3, candidates=(0.5,), min_sample=2)
    assert sw.recommendation is None


# -- re-entry price-override sweep -------------------------------------------- #

def test_reentry_sweep_blocks_none_composite_and_sums():
    recs = [
        TradeRecord(symbol="SPCX", action="buy", qty=1.0, entry_price=3.20,
                    composite_score=0.9, ts=_T0),
        TradeRecord(symbol="SPCX", action="sell", qty=1.0, exit_price=3.30,
                    realized_pl=10.0, realized_pl_pct=3.1, ts=_T0 + timedelta(hours=5)),
        # Re-entry AT/ABOVE the prior exit, composite None -> blocked at every candidate.
        TradeRecord(symbol="SPCX", action="buy", qty=1.0, entry_price=3.58,
                    ts=_T0 + timedelta(days=4)),
        TradeRecord(symbol="SPCX", action="sell", qty=1.0, exit_price=3.23,
                    realized_pl=-35.0, realized_pl_pct=-9.8, ts=_T0 + timedelta(days=5)),
    ]
    sw = sweep_reentry_override(recs, [], current=1.25, candidates=(1.0,), min_sample=1)
    row = sw.rows[0]
    assert row.n_affected == 1
    assert row.losses_avoided == 35.0
    assert row.net == 35.0


def test_reentry_sweep_ignores_buy_below_prior_exit():
    recs = [
        TradeRecord(symbol="Y", action="buy", qty=1.0, entry_price=10.0, ts=_T0),
        TradeRecord(symbol="Y", action="sell", qty=1.0, exit_price=10.0,
                    realized_pl=5.0, ts=_T0 + timedelta(hours=1)),
        # BELOW the prior exit price -> not a re-entry, not blocked.
        TradeRecord(symbol="Y", action="buy", qty=1.0, entry_price=9.0, ts=_T0 + timedelta(hours=2)),
        TradeRecord(symbol="Y", action="sell", qty=1.0, exit_price=8.0,
                    realized_pl=-50.0, ts=_T0 + timedelta(hours=3)),
    ]
    sw = sweep_reentry_override(recs, [], current=1.25, candidates=(1.0,), min_sample=1)
    assert sw.rows[0].n_affected == 0


def test_reentry_sweep_unresolved_excluded_from_totals():
    recs = [
        TradeRecord(symbol="Z", action="buy", qty=1.0, entry_price=10.0, ts=_T0),
        TradeRecord(symbol="Z", action="sell", qty=1.0, exit_price=10.0,
                    realized_pl=5.0, ts=_T0 + timedelta(hours=1)),
        # Re-entry still open — no resolving sell.
        TradeRecord(symbol="Z", action="buy", qty=1.0, entry_price=10.0, ts=_T0 + timedelta(hours=2)),
    ]
    sw = sweep_reentry_override(recs, [], current=1.25, candidates=(1.0,), min_sample=1)
    row = sw.rows[0]
    assert row.n_affected == 0
    assert row.unresolved == 1


# -- rotation-guard veto-chain parsing + joins -------------------------------- #

def _veto(symbol, day, hhmmss, loss):
    return DecisionRecord(
        ts=f"{day}T{hhmmss}+00:00", symbol=symbol, action="sell", instrument="equity",
        conviction=0.5, target_weight_pct=0.0, verdict="rotation_guard",
        approved_notional=0.0,
        reason=(
            f"Rotation guard: selling {symbol} at {loss:+.1f}% locks in a real "
            "loss, and the best incoming buy (conviction 0.60) doesn't clear "
            "the incumbent's entry 0.50 by +0.10 — holding instead."
        ),
    )


def test_rotation_chains_parse_and_join_to_resolving_sell():
    day = "2026-07-22"
    vetoes = [
        _veto("SPCX", day, "10:00:00", -5.4),
        _veto("SPCX", day, "11:00:00", -5.6),
        _veto("SPCX", day, "12:00:00", -6.6),
        _veto("SPCX", day, "13:00:00", -9.3),
    ]
    resolving = TradeRecord(
        symbol="SPCX", action="sell", qty=1.0, realized_pl_pct=-9.8,
        ts=datetime(2026, 7, 22, 14, 0, tzinfo=timezone.utc),
    )
    chains = _rotation_chains(vetoes, [resolving])
    assert len(chains) == 1
    c = chains[0]
    assert c.symbol == "SPCX" and c.day == day
    assert [round(loss, 1) for _, loss in c.vetoes] == [-5.4, -5.6, -6.6, -9.3]
    assert c.final_pl_pct == -9.8


def test_rotation_chains_unresolved_when_no_later_sell():
    day = "2026-07-22"
    vetoes = [_veto("SPCX", day, "10:00:00", -5.4)]
    # A sell exists but BEFORE the veto — must not be picked as the resolver.
    earlier_sell = TradeRecord(
        symbol="SPCX", action="sell", qty=1.0, realized_pl_pct=1.0,
        ts=datetime(2026, 7, 22, 9, 0, tzinfo=timezone.utc),
    )
    chains = _rotation_chains(vetoes, [earlier_sell])
    assert chains[0].final_pl_pct is None


def test_rotation_max_loss_release_boundary_and_pinning_cost():
    day = "2026-07-22"
    vetoes = [
        _veto("SPCX", day, "10:00:00", -5.4),
        _veto("SPCX", day, "11:00:00", -5.6),
        _veto("SPCX", day, "12:00:00", -6.6),
    ]
    resolving = TradeRecord(
        symbol="SPCX", action="sell", qty=1.0, realized_pl_pct=-9.8,
        ts=datetime(2026, 7, 22, 14, 0, tzinfo=timezone.utc),
    )
    chains = _rotation_chains(vetoes, [resolving])
    # Depth release at 8.0 never qualifies (deepest veto is -6.6) -> chain unaffected.
    sw8 = sweep_rotation_max_loss(chains, current=8.0, candidates=(8.0,), min_sample=1)
    assert sw8.rows[0].n_affected == 0
    # Depth release at 5.0 qualifies at the FIRST veto (-5.4 <= -5.0) -> pinning
    # cost = -5.4 - (-9.8) = 4.4pp saved.
    sw5 = sweep_rotation_max_loss(chains, current=8.0, candidates=(5.0,), min_sample=1)
    row5 = sw5.rows[0]
    assert row5.n_affected == 1
    assert abs(row5.pp_saved - 4.4) < 1e-9


def test_rotation_release_boundary_fires_at_the_right_veto():
    day = "2026-07-22"
    vetoes = [
        _veto("SPCX", day, "10:00:00", -5.4),
        _veto("SPCX", day, "11:00:00", -5.6),
        _veto("SPCX", day, "12:00:00", -6.6),
    ]
    resolving = TradeRecord(
        symbol="SPCX", action="sell", qty=1.0, realized_pl_pct=-9.8,
        ts=datetime(2026, 7, 22, 14, 0, tzinfo=timezone.utc),
    )
    chains = _rotation_chains(vetoes, [resolving])
    # Release 0.75: baseline -5.4, threshold -6.15. -5.6 doesn't qualify
    # (-5.6 > -6.15); -6.6 does (-6.6 <= -6.15) -> releases at -6.6.
    sw = sweep_rotation_release(chains, current=0.75, candidates=(0.75,), min_sample=1)
    row = sw.rows[0]
    assert row.n_affected == 1
    assert abs(row.pp_saved - (-6.6 - (-9.8))) < 1e-9
    # Release 1.5 never qualifies (no veto reaches -5.4-1.5=-6.9).
    sw_none = sweep_rotation_release(chains, current=0.75, candidates=(1.5,), min_sample=1)
    assert sw_none.rows[0].n_affected == 0


def test_rotation_release_needs_a_repeat_veto():
    # A single-veto chain can never fire the PERSISTENCE release (needs a
    # second, worse veto to compare against the baseline).
    day = "2026-07-22"
    vetoes = [_veto("SPCX", day, "10:00:00", -5.4)]
    resolving = TradeRecord(
        symbol="SPCX", action="sell", qty=1.0, realized_pl_pct=-9.8,
        ts=datetime(2026, 7, 22, 14, 0, tzinfo=timezone.utc),
    )
    chains = _rotation_chains(vetoes, [resolving])
    sw = sweep_rotation_release(chains, current=0.75, candidates=(0.25,), min_sample=1)
    assert sw.rows[0].n_affected == 0


# -- timestamp parsing across the two record formats -------------------------- #

def test_safe_parse_ts_handles_both_decisionrecord_and_traderecord_formats():
    # DecisionRecord.ts: datetime.isoformat() (T separator).
    assert _safe_parse_ts("2026-07-22T14:00:00+00:00") is not None
    # TradeRecord.ts: str(pydantic datetime) (space separator).
    assert _safe_parse_ts("2026-07-22 14:00:00+00:00") is not None
    assert _safe_parse_ts("not-a-timestamp") is None


def test_rotation_chains_cross_format_ordering_is_chronological():
    """Regression: DecisionRecord.ts ('T' separator) and str(TradeRecord.ts)
    (' ' separator) do NOT compare correctly as raw strings — the separator
    character itself sorts differently, so a naive string '>' comparison can
    silently treat a LATER sell as if it came before the veto. This chain's
    resolving sell is only ~4 minutes after the veto; a broken comparison
    would misclassify it as unresolved."""
    veto = _veto("SPCX", "2026-07-22", "10:00:00", -5.4)
    resolving = TradeRecord(
        symbol="SPCX", action="sell", qty=1.0, realized_pl_pct=-6.0,
        ts=datetime(2026, 7, 22, 10, 4, 0, tzinfo=timezone.utc),
    )
    chains = _rotation_chains([veto], [resolving])
    assert chains[0].final_pl_pct == -6.0


# -- run_autotune: end-to-end, never raises, writes a report ------------------ #

class _Risk(SimpleNamespace):
    pass


def _cfg():
    risk = _Risk(
        min_new_name_conviction=0.5, min_conviction=0.2,
        reentry_price_override_composite=1.25,
        rotation_guard_max_loss_pct=8.0, rotation_guard_repeat_release_pct=0.75,
    )
    return SimpleNamespace(risk=risk)


def test_run_autotune_empty_data_returns_none_without_raising():
    empty_ledger = SimpleNamespace(effective=lambda: [])
    empty_journal = DecisionJournal(base_dir=Path(tempfile.mkdtemp(prefix="_autotune_empty_")))
    out_dir = Path(tempfile.mkdtemp(prefix="_autotune_out_"))
    result = run_autotune(
        _cfg(), empty_ledger, empty_journal, days=14,
        now=datetime(2026, 7, 25, tzinfo=timezone.utc),  # a Saturday
        out_dir=out_dir,
    )
    assert result is None


def test_run_autotune_never_raises_on_malformed_journal():
    """A journal file with a garbage line must not crash the weekly job — same
    best-effort contract as postmortem.run_postmortem."""
    base = Path(tempfile.mkdtemp(prefix="_autotune_bad_"))
    day = "2026-07-20"
    (base / f"{day}.jsonl").write_text("not json at all\n", encoding="utf-8")
    journal = DecisionJournal(base_dir=base)
    ledger = SimpleNamespace(effective=lambda: [])
    out_dir = Path(tempfile.mkdtemp(prefix="_autotune_out2_"))
    result = run_autotune(
        _cfg(), ledger, journal, days=14,
        now=datetime(2026, 7, 25, tzinfo=timezone.utc), out_dir=out_dir,
    )
    # Malformed lines are skipped by DecisionJournal.today() itself, so this
    # just resolves to "no journal records" -> None, not an exception.
    assert result is None


def test_run_autotune_writes_report_with_realized_trades():
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    ledger = TradeLedger(path)
    for i in range(5):
        ts = _T0 + timedelta(days=i, hours=1)
        ledger.record(TradeRecord(
            symbol=f"S{i}", action="buy", qty=1.0, conviction=0.45,
            entry_price=10.0, ts=ts,
        ))
        ledger.record(TradeRecord(
            symbol=f"S{i}", action="sell", qty=1.0, realized_pl_pct=-9.0,
            realized_pl=-90.0, ts=ts + timedelta(hours=2),
        ))
    journal = DecisionJournal(base_dir=Path(tempfile.mkdtemp(prefix="_autotune_journal_")))
    out_dir = Path(tempfile.mkdtemp(prefix="_autotune_out3_"))
    result = run_autotune(
        _cfg(), ledger, journal, days=14,
        now=datetime(2026, 7, 25, tzinfo=timezone.utc), out_dir=out_dir,
    )
    assert result is not None
    assert result.exists()
    text = result.read_text(encoding="utf-8")
    assert "MIN_NEW_NAME_CONVICTION" in text
    assert "Report only" in text


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
