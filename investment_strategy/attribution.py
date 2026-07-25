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
entry_signals. Closes are QTY-AWARE: any sell whose known qty is below the open
total is a partial close — the regime trim (1B.6), the take-profit scale-out
(1B.8), and a multi-lot flatten split across separate fill rows (the BRK.B
2026-07-23 flatten sold 7 sh + 16 sh as two rows; the old reason-based rule
popped everything on the first row and orphaned the second into a signal-less
trip) — and reduces the open lots FIFO by its qty, keeping the rest attributed.
Exchange-side bracket auto-fills — formerly a blind spot no code observed — are
backfilled into the ledger each cycle (F.1: exit_reason bracket_stop /
bracket_take / external), so round-trips cover every exit path, not just the
decision- and watchdog-driven ones.

Two attribution bases (Jul-24 analysis). PRESENCE — every kind in the bundle at
entry — dilutes: with ~7 kinds present on nearly every entry, all sources
converge on the book average (presence stats sat bunched within ~0.9pp while the
same trips' cited stats spread ~6pp). CITED scores only the sources the LLM
actually NAMED in key_signals when it proposed the trade, parsed by
`parse_cited`; it is the basis the perf-weight / track-record / subscription
consumers now use, with a per-trip fallback to presence when nothing was cited.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .ledger import TradeLedger, TradeRecord

# Cited-string -> source-bucket tables for `parse_cited`. The decision LLM
# consistently prefixes each key_signals citation with its source ("insider
# Form4 +1.00", "options_chain +0.39"), but the text is free-form, so this is
# substring matching over curated tables — extend them when unparsed idioms
# show up in the ledger. Buckets are SignalKind values plus two REPORT-ONLY
# extras that deliberately aren't kinds: "options_flow" (the flow provider
# emits under kind=news, but its citations behave differently — Jul-24:
# flow-cited trips -0.8% vs news-cited -3.0% — so folding them together would
# launder the news number) and "composite" (the LLM citing the deterministic
# index itself). Non-kind buckets never collide with a SignalKind value, so
# composite perf-weight lookups simply never see them.
#
# STRONG patterns are the source being NAMED. WEAK patterns are metric/
# indicator vocabulary that only implies a source ("margin", "RSI", "upgrade").
# Weak hits count only when a string has no strong hit at all: "news: AI
# margin windfall earnings surprise" (SMCI 2026-07-22) is a news citation that
# merely mentions margins — crediting fundamentals for it would be laundering.
_STRONG_PATTERNS: tuple[tuple[str, str], ...] = (
    ("options_chain", "options_chain"), ("options chain", "options_chain"),
    ("option chain", "options_chain"), ("chain positioning", "options_chain"),
    ("bullish chain", "options_chain"), ("bearish chain", "options_chain"),
    ("options flow", "options_flow"), ("option flow", "options_flow"),
    ("options_flow", "options_flow"), ("call flow", "options_flow"),
    ("put flow", "options_flow"), ("news flow", "options_flow"),
    ("flow c/p", "options_flow"), ("call imbalance", "options_flow"),
    ("put imbalance", "options_flow"),
    ("composite", "composite"),
    ("govcontract", "govcontracts"), ("gov contract", "govcontracts"),
    ("government contract", "govcontracts"), ("federal contract", "govcontracts"),
    ("offexchange", "offexchange"), ("off-exchange", "offexchange"),
    ("off exchange", "offexchange"), ("dark pool", "offexchange"),
    ("darkpool", "offexchange"), ("short volume", "offexchange"),
    ("fundamental", "fundamentals"),
    ("technical", "technical"),
    ("insider", "insider"), ("form4", "insider"), ("form 4", "insider"),
    ("congress", "congress"), ("senate", "congress"),
    ("news", "news"),
    ("macro", "macro"),
    ("discovery", "discovery"), ("scanner", "discovery"),
)
_WEAK_PATTERNS: tuple[tuple[str, str], ...] = (
    ("revenue", "fundamentals"), ("rev +", "fundamentals"),
    ("rev growth", "fundamentals"), ("eps", "fundamentals"),
    ("ebitda", "fundamentals"), ("margin", "fundamentals"),
    ("valuation", "fundamentals"), ("p/e", "fundamentals"),
    ("earnings", "fundamentals"),
    ("rsi", "technical"), ("macd", "technical"),
    ("golden cross", "technical"), ("breakout", "technical"),
    ("momentum", "technical"), ("trend", "technical"),
    ("oversold", "technical"), ("overbought", "technical"),
    ("sentiment", "news"), ("upgrade", "news"), ("downgrade", "news"),
    ("analyst", "news"), ("headline", "news"), ("catalyst", "news"),
    ("p/c", "options_chain"), ("put/call", "options_chain"),
    ("c/p", "options_flow"),
    ("fomc", "macro"), ("cpi", "macro"),
    ("screen", "discovery"),
)


def parse_cited(texts: list[str] | None) -> set[str]:
    """Source buckets named by the LLM's key_signals citations.

    Per string: strong (source-name) hits win outright; weak (metric-word)
    hits only count when the string named no source at all. Match-ALL among
    strong hits — "congress+insider net buying" credits both sources. One
    suppression: when a flow bucket matched, a same-string "news" hit is
    dropped, because the flow signal emits under the news kind and its
    kind-prefix ("news options flow C/P +0.76", "news flow +0.61 call
    imbalance") is an artifact of that, not a second source being cited.
    """
    out: set[str] = set()
    for t in texts or []:
        low = t.lower()
        strong = {bucket for pat, bucket in _STRONG_PATTERNS if pat in low}
        hits = strong or {bucket for pat, bucket in _WEAK_PATTERNS if pat in low}
        if "options_flow" in hits:
            hits = hits - {"news"}
        out |= hits
    return out


# A sell within this RELATIVE fraction of the open total is a full close. Buys
# ledger requested/estimated qty, sells ledger actual fills; observed deltas on
# broker-flat closes reach ~2e-3 shares (QQQ core-fill flatten) — far above any
# absolute dust margin — while genuine partial closes trim >=25% of a position.
_FULL_CLOSE_REL_TOL = 0.01


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
    # Source buckets the LLM NAMED in key_signals at entry (union across open
    # lots, parsed by parse_cited). Empty when nothing was cited/parseable —
    # cited-basis attribution then falls back to `signals`. Appended last so
    # positional construction of older fields keeps working.
    cited_signals: list[str] = field(default_factory=list)
    # "equity" | "option" — from the (symbol, instrument) lot key, so option
    # wipeouts can be split out of a source's read without re-walking the ledger.
    instrument: str = "equity"
    # Planned stop width (%) of the buy that OPENED this episode — the risk
    # unit the exit-discipline read is measured in (a decision-sell at less
    # than half of this locked a micro-loss before the stop tested the thesis).
    # None when the opener recorded no stop (core fills, pre-tracking rows).
    opening_stop_pct: float | None = None


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

    Only trips whose exit carried a realized P&L AND had open lots to close are
    emitted. An exit without an outcome (old records, or a sell we couldn't
    mark) can't be attributed; an exit with NO open lots — a duplicate retry
    row (the T option flatten of 2026-07-23 was ledgered twice, doubling a
    -$2,700 loss in every overall stat) or a sell whose buy predates the ledger
    — used to emit a signal-less trip that polluted the overall numbers while
    attributing to nothing, and is now skipped.

    Closes are QTY-AWARE: a sell with a known qty below the open total is a
    partial close (scale-out, regime trim, or a multi-lot flatten split across
    fill rows) and reduces the open lots FIFO by its qty; a sell whose qty
    covers the open total — or carries no qty at all — flattens the position.
    The partial/full boundary is RELATIVE (a sell within 1% of the open total
    flattens): buy rows ledger the REQUESTED/estimated qty while sell rows
    ledger the ACTUAL filled qty, and the live ledger's deltas run 1e-5..2e-3
    shares (JNJ/ORCL decision closes, the QQQ core-fill flatten) — an absolute
    dust margin left those broker-flat positions open forever as phantom lots.
    Genuine partials (25-50% trims and scale-outs) sit nowhere near 99%.

    Lots are keyed by (symbol, instrument): option rows ledger under the bare
    underlying ticker with qty in CONTRACTS (the 2026-07-23 T calls: qty=900
    alongside equity T rows of 372 shares), so a shared key would compare
    contracts against shares in the qty arithmetic and union option and equity
    signals into each other's trips.
    """
    ordered = sorted(records, key=lambda r: r.ts)
    # Per (symbol, instrument), a list of open lots as
    # [remaining_qty, entry_signals, cited_buckets, ts_date, conviction-or-None].
    open_lots: dict[tuple[str, str], list[list]] = {}
    # Per (symbol, instrument), the (conviction, composite) of the buy that
    # opened the CURRENT episode (flat -> open) — set once per episode, cleared
    # on full close, so a later top-up never overwrites the entry's own numbers.
    episode_open: dict[tuple[str, str], tuple[float | None, float | None]] = {}
    # Per (symbol, instrument), the signature of the last processed sell — a
    # sell row identical in (qty, P&L%, P&L$, reason) to the one before it is a
    # duplicate retry ledgering (the T option flatten of 2026-07-23 landed
    # twice, 3h apart), which the no-open-lots skip alone can't catch when the
    # first sell was PARTIAL and lots remain to double-consume.
    last_sell_sig: dict[tuple[str, str], tuple] = {}
    trips: list[RoundTrip] = []
    for r in ordered:
        key = (r.symbol, r.instrument or "equity")
        if r.action == "buy":
            last_sell_sig.pop(key, None)
            ts_date = str(r.ts)[:10] if r.ts else ""
            # 0.0 means "not recorded" (core fills, pre-tracking rows), not
            # "zero conviction" — store None so calibration skips it.
            conv = r.conviction if (r.conviction or 0.0) > 0 else None
            if not open_lots.get(key):
                stop = r.stop_loss_pct if (r.stop_loss_pct or 0.0) > 0 else None
                episode_open[key] = (conv, r.composite_score, stop)
            open_lots.setdefault(key, []).append([
                float(r.qty or 0.0), list(r.entry_signals),
                parse_cited(r.key_signals), ts_date, conv,
            ])
        elif r.action == "sell":
            sig = (float(r.qty or 0.0), r.realized_pl_pct, r.realized_pl,
                   r.exit_reason)
            if last_sell_sig.get(key) == sig:
                continue
            last_sell_sig[key] = sig
            lots = open_lots.get(key, [])
            if r.realized_pl_pct is not None and lots:
                signals = sorted({k for _q, sigs, _c, _d, _cv in lots for k in sigs})
                cited = sorted({k for _q, _s, cset, _d, _cv in lots for k in cset})
                dates = [d for _q, _s, _c, d, _cv in lots if d]
                same_day = len(dates) >= 2 and len(set(dates)) == 1
                convs = [c for _q, _s, _c, _d, c in lots if c is not None]
                open_conv, open_comp, open_stop = episode_open.get(
                    key, (None, None, None))
                trips.append(RoundTrip(
                    symbol=r.symbol, pl_pct=r.realized_pl_pct,
                    signals=signals, exit_reason=r.exit_reason,
                    n_lots=len(lots), same_day_repeat=same_day,
                    conviction=sum(convs) / len(convs) if convs else None,
                    opening_conviction=open_conv, opening_composite=open_comp,
                    realized_pl=r.realized_pl, exit_ts=str(r.ts) if r.ts else "",
                    cited_signals=cited, instrument=key[1],
                    opening_stop_pct=open_stop,
                ))
            qty = float(r.qty or 0.0)
            open_total = sum(lot[0] for lot in lots)
            if 0.0 < qty < open_total * (1.0 - _FULL_CLOSE_REL_TOL):
                _reduce_fifo(lots, qty)
                if not lots:
                    open_lots.pop(key, None)
                    episode_open.pop(key, None)
            else:
                open_lots.pop(key, None)
                episode_open.pop(key, None)
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
    [remaining_qty, signals, cited, date, conviction]), dropping fully-consumed
    lots. Mutates `lots` in place."""
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


def attribute(
    trips: list[RoundTrip], basis: str = "present",
) -> dict[str, SourceStats]:
    """Per-source win-rate and average realized P&L across round-trips.

    basis="present" scores every kind in the bundle at entry — kept for the
    citing-vs-presence comparison, but diluted as a ranking (~7 kinds ride
    every trade, so all sources converge on the book average).
    basis="cited" scores only the sources the LLM NAMED in key_signals when it
    made the trade — the sharp basis the perf-weight / track-record /
    subscription consumers use — falling back per-trip to the presence set when
    nothing was cited (core fills, pre-tracking rows), so those trips still
    count somewhere instead of vanishing.
    """
    stats: dict[str, SourceStats] = {}
    for t in trips:
        srcs = t.signals if basis == "present" else (t.cited_signals or t.signals)
        for src in srcs:
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
    stats = attribute(recent, basis="cited")
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
        "the entry signals you CITED as decisive — trusted, not market data)",
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
    bail = exit_discipline_lesson(recent)
    if bail:
        lines.append(bail)
    return "\n".join(lines)


def exit_discipline_lesson(trips: list[RoundTrip], min_trips: int = 3) -> str:
    """One line naming the early-bail leak, or "" below `min_trips` samples.

    Jul-25 calibration: 9 of 15 decision-sells exited at LESS than half the
    planned stop width (avg -1.6%) — micro-losses locked before the stop could
    test the thesis, while the stops themselves fired exactly as planned
    (bracket stops at 1.01x width). The deterministic trail geometry was fixed
    in code; this line targets the half the LLM controls: its own sells.
    """
    bails = [
        t for t in trips
        if t.exit_reason == "decision" and t.opening_stop_pct
        and -t.opening_stop_pct * 0.5 < t.pl_pct < 0
    ]
    if len(bails) < min_trips:
        return ""
    avg = sum(t.pl_pct for t in bails) / len(bails)
    return (
        f"Exit discipline: {len(bails)} of your decision-sells bailed at less "
        f"than HALF the planned stop (avg {avg:+.1f}%) — micro-losses locked "
        "before the stop could test the thesis. Sell ahead of the stop only on "
        "a broken thesis or a better use of the slot, not an adverse wiggle."
    )
