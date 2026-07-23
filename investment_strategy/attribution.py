"""Signal attribution — close the learning loop the ledger opened.

The ledger records every entry (with the signal kinds present at the time) and
every exit (with the realized P&L at close). This module joins them into closed
*round-trips* and scores each signal source by how its trades actually turned out,
then renders a terse "track record" block that gets injected back into the decision
prompt. That's the reflection/memory idea from TauricResearch/TradingAgents, adapted
to our numeric-signal architecture: Claude is told which sources have paid off so
far and can weight conviction accordingly (and the operator learns which Quiver
datasets to keep paying for).

Round-trip reconstruction walks the ledger in time order; buys open a position and
a sell/exit realizes an outcome attributed to the union of the open buys'
entry_signals. Most closes flatten the whole position, but the regime trim (1B.6)
and take-profit scale-out (1B.8) are PARTIAL sells: they realize an outcome on a
slice while the remainder stays open, so a partial exit reduces the open lots FIFO
by its qty and keeps the rest open (otherwise the remainder's later exit would
orphan into a signal-less trip). Exchange-side bracket auto-fills — formerly a
blind spot no code observed — are backfilled into the ledger each cycle (F.1:
exit_reason bracket_stop / bracket_take / external), so round-trips now cover
every exit path, not just the decision- and watchdog-driven ones.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .ledger import TradeLedger, TradeRecord

# Exit reasons that only PARTIALLY close a position (a slice is sold, the rest
# stays open). Every other close — decision / stop / take / trail / flatten / time
# / thesis_decay — flattens the whole position.
_PARTIAL_EXIT_REASONS = {"scale", "regime_trim"}


@dataclass
class RoundTrip:
    symbol: str
    pl_pct: float
    signals: list[str]            # entry SignalKind values this outcome is attributed to
    exit_reason: str = ""
    n_lots: int = 1               # how many open lots contributed (proxy for top-up depth)
    same_day_repeat: bool = False  # True when ≥2 lots opened on the same calendar date
    # Mean entry conviction of the open lots (None when no lot recorded one —
    # core fills and pre-tracking records write conviction 0.0, treated as
    # unknown). Feeds the conviction-calibration block.
    conviction: float | None = None
    # --- episode-entry context (for the weekly auto-tuner) --------------------
    # The conviction/composite of the buy that OPENED this episode (position going
    # flat→open), not the mean of remaining lots — a partial scale-out can consume
    # the opening lot FIFO and leave a top-up's number behind, which would misjudge
    # a conviction floor. None when the opener carried the 0.0 sentinel (core fills,
    # pre-tracking) or predated the composite.
    opening_conviction: float | None = None
    opening_composite: float | None = None
    realized_pl: float | None = None  # dollars from the realizing sell (r.realized_pl)
    exit_ts: str = ""                 # str(ts) of the realizing sell — window filter + veto join


@dataclass
class SourceStats:
    source: str
    trips: int = 0
    wins: int = 0
    pl_pcts: list[float] = field(default_factory=list)

    @property
    def win_rate(self) -> float:
        return self.wins / self.trips if self.trips else 0.0

    @property
    def avg_pl_pct(self) -> float:
        return sum(self.pl_pcts) / len(self.pl_pcts) if self.pl_pcts else 0.0


def round_trips(records: list[TradeRecord]) -> list[RoundTrip]:
    """Reconstruct closed round-trips from ledger records (chronological).

    Only trips whose exit carried a realized P&L are emitted — an exit without an
    outcome (old records, or a sell we couldn't mark) can't be attributed. A PARTIAL
    exit (scale-out / regime trim) realizes an outcome on a slice and leaves the
    remainder open, so it reduces the open lots FIFO rather than flattening them.
    """
    ordered = sorted(records, key=lambda r: r.ts)
    # Per symbol, a list of open lots as
    # [remaining_qty, entry_signals, ts_date, conviction-or-None].
    open_by_symbol: dict[str, list[list]] = {}
    # Per symbol, the (conviction, composite) of the buy that opened the CURRENT
    # episode (flat -> open) — set once per episode, cleared on full close, so a
    # later top-up never overwrites the entry's own numbers.
    episode_open: dict[str, tuple[float | None, float | None]] = {}
    trips: list[RoundTrip] = []
    for r in ordered:
        if r.action == "buy":
            ts_date = str(r.ts)[:10] if r.ts else ""
            # 0.0 means "not recorded" (core fills, pre-tracking rows), not
            # "zero conviction" — store None so calibration skips it.
            conv = r.conviction if (r.conviction or 0.0) > 0 else None
            if r.symbol not in open_by_symbol or not open_by_symbol[r.symbol]:
                episode_open[r.symbol] = (conv, r.composite_score)
            open_by_symbol.setdefault(r.symbol, []).append(
                [float(r.qty or 0.0), list(r.entry_signals), ts_date, conv]
            )
        elif r.action == "sell":
            lots = open_by_symbol.get(r.symbol, [])
            if r.realized_pl_pct is not None:
                signals = sorted({k for _, sigs, _d, _c in lots for k in sigs})
                dates = [d for _, _, d, _c in lots if d]
                same_day = len(dates) >= 2 and len(set(dates)) == 1
                convs = [c for _, _, _, c in lots if c is not None]
                open_conv, open_comp = episode_open.get(r.symbol, (None, None))
                trips.append(RoundTrip(
                    symbol=r.symbol, pl_pct=r.realized_pl_pct,
                    signals=signals, exit_reason=r.exit_reason,
                    n_lots=len(lots), same_day_repeat=same_day,
                    conviction=sum(convs) / len(convs) if convs else None,
                    opening_conviction=open_conv, opening_composite=open_comp,
                    realized_pl=r.realized_pl, exit_ts=str(r.ts) if r.ts else "",
                ))
            # A partial exit (with a known qty) trims the open lots and keeps the
            # remainder; anything else — or an unknown qty — fully closes.
            if r.exit_reason in _PARTIAL_EXIT_REASONS and (r.qty or 0.0) > 0:
                _reduce_fifo(lots, float(r.qty))
                if not lots:
                    open_by_symbol.pop(r.symbol, None)
                    episode_open.pop(r.symbol, None)
            else:
                open_by_symbol.pop(r.symbol, None)
                episode_open.pop(r.symbol, None)
    return trips


def concentration_lessons(trips: list[RoundTrip], min_trips: int = 3) -> list[str]:
    """Compare single-entry vs multi-lot outcomes and same-day-repeat outcomes.
    Only emits a lesson when BOTH cohorts have enough data to be meaningful."""
    lines = []

    single = [t.pl_pct for t in trips if t.n_lots == 1]
    multi = [t.pl_pct for t in trips if t.n_lots >= 2]
    if len(single) >= min_trips and len(multi) >= min_trips:
        single_avg = sum(single) / len(single)
        multi_avg = sum(multi) / len(multi)
        direction = "underperform" if multi_avg < single_avg else "outperform"
        lines.append(
            f"Same-symbol multi-lot entries: {len(multi)} trips, {multi_avg:+.1f}% avg "
            f"vs {single_avg:+.1f}% single-entry — repeats {direction}."
        )

    repeat = [t.pl_pct for t in trips if t.same_day_repeat]
    non_repeat = [t.pl_pct for t in trips if not t.same_day_repeat]
    if len(repeat) >= min_trips and len(non_repeat) >= min_trips:
        repeat_avg = sum(repeat) / len(repeat)
        nr_avg = sum(non_repeat) / len(non_repeat)
        direction = "underperform" if repeat_avg < nr_avg else "outperform"
        lines.append(
            f"Same-day repeat buys of one symbol: {len(repeat)} trips, "
            f"{repeat_avg:+.1f}% avg vs {nr_avg:+.1f}% non-repeat — {direction}."
        )
    return lines


def _reduce_fifo(lots: list[list], qty: float) -> None:
    """Consume `qty` shares from the front of `lots` (each
    [remaining_qty, signals, date, conviction]), dropping fully-consumed lots.
    Mutates `lots` in place."""
    remaining = qty
    while remaining > 1e-9 and lots:
        lot = lots[0]
        if lot[0] <= remaining + 1e-9:
            remaining -= lot[0]
            lots.pop(0)
        else:
            lot[0] -= remaining
            remaining = 0.0


def conviction_calibration(trips: list[RoundTrip], min_trips: int = 3) -> list[str]:
    """Win rate + avg realized P&L by entry-conviction bucket — the mirror the
    LLM needs when its confidence stops predicting outcomes (week of
    2026-07-13: the 0.6+ picks were the biggest losers while the 0.4 picks
    won). Buckets under `min_trips` closed trips are suppressed; an explicit
    inversion flag is prepended when the top bucket underperforms the bottom
    one (both populated)."""
    buckets = [
        ("0.2-0.4", 0.2, 0.4),
        ("0.4-0.6", 0.4, 0.6),
        ("0.6+", 0.6, 1.01),
    ]
    stats: list[tuple[str, int, float, float]] = []  # (label, n, win%, avg)
    for label, lo, hi in buckets:
        pls = [
            t.pl_pct for t in trips
            if t.conviction is not None and lo <= t.conviction < hi
        ]
        if len(pls) < min_trips:
            continue
        win = sum(1 for p in pls if p > 0) / len(pls) * 100
        avg = sum(pls) / len(pls)
        stats.append((label, len(pls), win, avg))
    if not stats:
        return []
    lines = [
        f"- conviction {label}: {n} trades, {win:.0f}% win, {avg:+.1f}% avg"
        for label, n, win, avg in stats
    ]
    lows = next((s for s in stats if s[0] == "0.2-0.4"), None)
    highs = next((s for s in stats if s[0] == "0.6+"), None)
    if lows and highs and highs[3] < lows[3]:
        lines.insert(0, (
            "CONVICTION INVERTED: your highest-conviction entries "
            f"({highs[3]:+.1f}% avg) are LOSING to your lowest "
            f"({lows[3]:+.1f}% avg) — your confidence is currently "
            "miscalibrated; demand stronger corroboration before sizing up."
        ))
    return lines


def behavior_diagnostics(ledger: TradeLedger, min_trips: int = 3) -> list[str]:
    """Numeric behavior metrics for the nightly post-mortem — the deterministic
    counterpart to the LLM's prose diagnosis (which only eyeballs these from raw
    trade lines). Computed from the CORRECTED ledger, so they span all closed
    history, not just the day under review.

    - Disposition effect: are LOSERS held longer than WINNERS? The classic
      behavioral leak (ride losers, cut winners short). Live risk for THIS book:
      the rotation loss guard can institutionalize loss-holding, and only a
      hold-duration split surfaces it.
    - Overtrading: average opening BUYS per active trading day.

    Both cohorts are suppressed below `min_trips` samples — the same small-sample
    guard the attribution blocks use, because the account is fresh.
    """
    from .lots import build_lot_history

    records = ledger.effective()
    lines: list[str] = []

    # -- disposition effect (FIFO realized lots carry entry/exit timestamps) -- #
    _open, realized = build_lot_history(records)
    winners = [r for r in realized if r.pl_pct > 0]
    losers = [r for r in realized if r.pl_pct <= 0]
    if len(winners) >= min_trips and len(losers) >= min_trips:
        def _avg_days(lots: list) -> float:
            return sum(
                (r.exit_ts - r.entry_ts).total_seconds() for r in lots
            ) / len(lots) / 86400.0
        win_days = _avg_days(winners)
        lose_days = _avg_days(losers)
        flag = ""
        # Losers meaningfully longer-held than winners = the disposition effect.
        if win_days > 0 and lose_days > win_days * 1.25:
            flag = (
                f" — DISPOSITION EFFECT: losers held {lose_days / win_days:.1f}x "
                "longer than winners (riding losers, cutting winners short)."
            )
        lines.append(
            f"Avg hold: winners {win_days:.1f}d ({len(winners)} lots) vs losers "
            f"{lose_days:.1f}d ({len(losers)} lots).{flag}"
        )

    # -- overtrading: opening buys per active trading day -------------------- #
    buys_per_day: dict[str, int] = {}
    for t in records:
        if t.action == "buy":
            d = str(t.ts)[:10]
            if d:
                buys_per_day[d] = buys_per_day.get(d, 0) + 1
    if len(buys_per_day) >= min_trips:
        total = sum(buys_per_day.values())
        lines.append(
            f"Buys/active-day: {total / len(buys_per_day):.1f} "
            f"({total} buys over {len(buys_per_day)} active days)."
        )

    if not lines:
        return []
    return [
        "## Behavior diagnostics (deterministic, from your full ledger — trusted)",
        *lines,
    ]


def attribute(trips: list[RoundTrip]) -> dict[str, SourceStats]:
    """Per-source win-rate and average realized P&L across round-trips."""
    stats: dict[str, SourceStats] = {}
    for t in trips:
        for src in t.signals:
            s = stats.setdefault(src, SourceStats(source=src))
            s.trips += 1
            s.wins += 1 if t.pl_pct > 0 else 0
            s.pl_pcts.append(t.pl_pct)
    return stats


def render_lessons(
    ledger: TradeLedger, max_trips: int = 40, min_source_trips: int = 2,
) -> str:
    """Build the trusted 'track record' block for the decision prompt, or "" if
    there isn't enough closed history yet to say anything useful.

    Keep it terse: this lands in every decision prompt, so it costs output tokens
    each cycle (the Quiver notes warn about exactly this). Sources are sorted by
    realized avg P&L so the best/worst performers read first.
    """
    # effective(): reconcile corrections applied, so a rejected/partial order's
    # phantom intent can't count as a round-trip (GA-2.5).
    trips = round_trips(ledger.effective())
    if not trips:
        return ""
    recent = trips[-max_trips:]
    stats = attribute(recent)
    ranked = sorted(
        (s for s in stats.values() if s.trips >= min_source_trips),
        key=lambda s: s.avg_pl_pct, reverse=True,
    )
    if not ranked:
        return ""

    overall_win = sum(1 for t in recent if t.pl_pct > 0) / len(recent) * 100
    overall_avg = sum(t.pl_pct for t in recent) / len(recent)
    lines = [
        f"## Track record (last {len(recent)} closed trades; your realized P&L by "
        "entry signal — trusted, not market data)",
        f"Overall: {overall_win:.0f}% win, {overall_avg:+.1f}% avg per trade.",
    ]
    for s in ranked:
        lines.append(
            f"- {s.source}: {s.trips} trades, {s.win_rate*100:.0f}% win, "
            f"{s.avg_pl_pct:+.1f}% avg"
        )
    lines.append(
        "Weight conviction toward sources that have actually paid off and away "
        "from those that haven't — but samples are small and noisy, so treat this "
        "as a prior, never a hard rule, and never act on it against the thesis."
    )
    conc = concentration_lessons(recent)
    if conc:
        lines.extend(conc)
    calib = conviction_calibration(recent)
    if calib:
        lines.append("Conviction calibration (win rate by YOUR stated conviction):")
        lines.extend(calib)
    return "\n".join(lines)
