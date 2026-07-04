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
    # Per symbol, a list of open lots as [remaining_qty, entry_signals].
    open_by_symbol: dict[str, list[list]] = {}
    trips: list[RoundTrip] = []
    for r in ordered:
        if r.action == "buy":
            open_by_symbol.setdefault(r.symbol, []).append(
                [float(r.qty or 0.0), list(r.entry_signals)]
            )
        elif r.action == "sell":
            lots = open_by_symbol.get(r.symbol, [])
            if r.realized_pl_pct is not None:
                signals = sorted({k for _, sigs in lots for k in sigs})
                trips.append(RoundTrip(
                    symbol=r.symbol, pl_pct=r.realized_pl_pct,
                    signals=signals, exit_reason=r.exit_reason,
                ))
            # A partial exit (with a known qty) trims the open lots and keeps the
            # remainder; anything else — or an unknown qty — fully closes.
            if r.exit_reason in _PARTIAL_EXIT_REASONS and (r.qty or 0.0) > 0:
                _reduce_fifo(lots, float(r.qty))
                if not lots:
                    open_by_symbol.pop(r.symbol, None)
            else:
                open_by_symbol.pop(r.symbol, None)
    return trips


def _reduce_fifo(lots: list[list], qty: float) -> None:
    """Consume `qty` shares from the front of `lots` (each [remaining_qty, signals]),
    dropping fully-consumed lots. Mutates `lots` in place."""
    remaining = qty
    while remaining > 1e-9 and lots:
        lot = lots[0]
        if lot[0] <= remaining + 1e-9:
            remaining -= lot[0]
            lots.pop(0)
        else:
            lot[0] -= remaining
            remaining = 0.0


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
    return "\n".join(lines)
