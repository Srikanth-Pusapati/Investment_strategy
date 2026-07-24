"""Data-subscription evaluation (2.2) — decide what to PAY for from evidence.

The discipline the Todo insists on: "don't pay for data before you can measure it
helps." This module makes that decision mechanical instead of a hunch. It takes the
same signal attribution the ledger already produces (realized P&L per entry-signal
source) and, for each candidate paid subscription, aggregates the MEASURED track
record of the free signals that subscription would upgrade — then applies a gate:

  * INSUFFICIENT DATA — too few attributed round-trips to judge -> stay on the free
    tier and gather more (paying now would be buying blind).
  * KEEP MEASURING    — enough trips but no positive edge in that source yet ->
    there's nothing to amplify; don't pay.
  * SUBSCRIBE         — a real, measured edge exists -> paying to make it cleaner /
    faster / broader is justified.

It is advisory: it ranks and recommends, a human still buys. Runnable as
`python -m investment_strategy.subscriptions` to print the report from the live
ledger. Ordering of the candidates follows the Todo's ranked ROI.
"""
from __future__ import annotations

from dataclasses import dataclass

from .attribution import SourceStats, attribute, round_trips
from .ledger import TradeLedger


@dataclass(frozen=True)
class Subscription:
    """A candidate paid data source and the FREE signal kinds whose measured edge
    would justify (or not) paying to upgrade it."""
    name: str
    rank: int                          # 1 = best ROI per the Todo
    improves: str                      # what quality it buys
    upgrades_sources: tuple[str, ...]  # SignalKind values it would clean up / extend
    cost_hint: str
    rationale: str


# The three candidates from the Todo's ranked ROI, in order.
CANDIDATES: tuple[Subscription, ...] = (
    Subscription(
        name="Fundamentals + earnings (FMP or Finnhub paid)",
        rank=1,
        improves="thesis + earnings-guard quality (yfinance is free but flaky)",
        upgrades_sources=("fundamentals",),
        cost_hint="~$20-50/mo",
        rationale=(
            "Hooks already exist (FMP_API_KEY / FINNHUB_API_KEY). Also hardens the "
            "earnings-blackout guard, which yfinance flakiness currently weakens."
        ),
    ),
    Subscription(
        name="Better market data (Polygon paid / Alpaca SIP)",
        rank=2,
        improves="full-market real-time + clean history for honest backtests + whole-market scanning",
        upgrades_sources=("technical", "discovery"),
        cost_hint="~$30-200/mo",
        rationale=(
            "Underlies the technical signals AND the 2.1 backtest's fidelity and the "
            "scanner's reach. Judge it on the technical/discovery track record."
        ),
    ),
    Subscription(
        name="Quiver tier up (WSB / 13F)",
        rank=3,
        improves="unlocks new smart-money datasets (incremental; insiders already free)",
        upgrades_sources=("congress", "offexchange", "govcontracts"),
        cost_hint="incremental on the existing plan",
        rationale=(
            "Current Quiver-sourced signals are the read on whether more of the same "
            "family pays. Insiders are already free via Finnhub + SEC EDGAR."
        ),
    ),
)


@dataclass
class SubscriptionVerdict:
    subscription: Subscription
    trips: int                 # attributed round-trips across the upgraded sources
    avg_pl_pct: float          # measured average realized P&L of those trips
    win_rate: float            # 0..1
    recommendation: str        # SUBSCRIBE | KEEP MEASURING | INSUFFICIENT DATA
    reason: str


def _aggregate(stats: dict[str, SourceStats], sources: tuple[str, ...]) -> tuple[int, float, float]:
    """(trips, avg_pl_pct, win_rate) pooled across `sources` present in `stats`.
    A trip counted under two upgraded sources is pooled per-source (a source-level
    read), which is the granularity attribution provides."""
    pls: list[float] = []
    wins = 0
    trips = 0
    for s in sources:
        st = stats.get(s)
        if not st:
            continue
        trips += st.trips
        wins += st.wins
        pls.extend(st.pl_pcts)
    avg = sum(pls) / len(pls) if pls else 0.0
    win_rate = wins / trips if trips else 0.0
    return trips, avg, win_rate


def evaluate_subscriptions(
    ledger: TradeLedger, min_trips: int = 5, min_avg_pl_pct: float = 0.0,
) -> list[SubscriptionVerdict]:
    """Score every candidate subscription against the ledger's measured signal
    attribution and apply the pay/don't-pay gate. Sorted by the Todo's ROI rank."""
    # effective(): corrections reconciled — same view every other consumer uses.
    # Cited basis: judge a paid upgrade on the trades where its free signals
    # were actually DECISIVE, not on every trade they happened to ride along in.
    stats = attribute(round_trips(ledger.effective()), basis="cited")
    verdicts: list[SubscriptionVerdict] = []
    for sub in sorted(CANDIDATES, key=lambda s: s.rank):
        trips, avg, win_rate = _aggregate(stats, sub.upgrades_sources)
        if trips < min_trips:
            rec = "INSUFFICIENT DATA"
            reason = (
                f"only {trips} attributed trip(s) in {'/'.join(sub.upgrades_sources)} "
                f"(< {min_trips}) — keep the free tier and gather more before paying."
            )
        elif avg > min_avg_pl_pct:
            rec = "SUBSCRIBE"
            reason = (
                f"{trips} trips at {avg:+.1f}% avg ({win_rate*100:.0f}% win) — the "
                f"{sub.improves} already pays; upgrading should sharpen it."
            )
        else:
            rec = "KEEP MEASURING"
            reason = (
                f"{trips} trips but only {avg:+.1f}% avg — no measured edge to "
                f"amplify yet; don't pay to clean up a source that isn't earning."
            )
        verdicts.append(SubscriptionVerdict(
            subscription=sub, trips=trips, avg_pl_pct=avg,
            win_rate=win_rate, recommendation=rec, reason=reason,
        ))
    return verdicts


def render_report(
    ledger: TradeLedger, min_trips: int = 5, min_avg_pl_pct: float = 0.0,
) -> str:
    """Human-readable decision report ranking the candidate subscriptions with a
    measured, evidence-based recommendation for each."""
    verdicts = evaluate_subscriptions(ledger, min_trips, min_avg_pl_pct)
    lines = [
        "== Data-subscription evaluation (2.2) ==",
        "Discipline: pay only for a source whose FREE signals already show a "
        f"measured edge (>= {min_trips} round-trips, avg P&L > {min_avg_pl_pct:.1f}%).",
        "",
    ]
    for v in verdicts:
        s = v.subscription
        lines += [
            f"#{s.rank}  {s.name}  [{s.cost_hint}]",
            f"     upgrades: {s.improves}",
            f"     measured: {v.trips} trips, {v.avg_pl_pct:+.1f}% avg, "
            f"{v.win_rate*100:.0f}% win  ->  {v.recommendation}",
            f"     {v.reason}",
            "",
        ]
    lines.append(
        "Re-run after the 2.1 backtest and more live round-trips: the recommendation "
        "moves from INSUFFICIENT DATA to a real BUY/HOLD as the evidence accumulates."
    )
    return "\n".join(lines)


def _main() -> None:
    import logging

    logging.basicConfig(level="INFO", format="%(message)s")
    print(render_report(TradeLedger()))


if __name__ == "__main__":
    _main()
