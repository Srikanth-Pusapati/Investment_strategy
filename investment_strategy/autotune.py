"""Weekly ledger-driven auto-tune report — a deterministic (no-LLM) replay of
the trade ledger and decisions journal against the entry-quality risk knobs.

Born from the Jul 17-22 review: MIN_NEW_NAME_CONVICTION=0.5 was set by eyeballing
~2 weeks of trades, and it would ALSO have blocked small NU/SOFI winners at 0.46 —
a fact only visible by actually replaying the ledger. This module sweeps a small
candidate grid for each tunable knob and reports what each candidate would have
done to REALIZED trades (losses avoided vs winners missed) plus, count-only, how
many journal-rejected proposals a looser candidate would have admitted (their
outcome is unknown, so no P&L is fabricated for them).

Report-only: nothing here writes a knob back to .env. "Claude proposes, risk
disposes" extends to tuning — the human reviews the report and edits .env by
hand. Small windows (2-4 weeks) overfit easily, so every recommendation carries
its sample size and is suppressed below `min_sample` affected trades, and a
one-sidedness check (the win must not be mostly offset by what it would have
cost) guards against noise dressed up as a suggestion.

Run standalone:
  python -m investment_strategy.autotune [--days 14] [--min-sample 5]

Or triggered automatically by the orchestrator on the first market-closed tick
of the ET weekend (AUTOTUNE_ENABLED=True, default on) — see
orchestrator._maybe_run_weekly_autotune.
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .journal import _ET, DecisionJournal, DecisionRecord
from .attribution import RoundTrip, round_trips
from .ledger import TradeLedger, TradeRecord

log = logging.getLogger("autotune")

_AUTOTUNE_DIR = Path("state") / "autotune"
MIN_SAMPLE_DEFAULT = 5

# The rotation-guard veto reason is free text (orchestrator._apply_rotation_guard):
# "Rotation guard: selling {sym} at {pl:+.1f}% locks in a real loss, ..." — the
# loss % only exists inside this sentence, so it's regex-parsed rather than
# stored structurally. If the reason wording ever changes, matches silently drop
# to "unparseable" rather than producing a wrong number (see _rotation_chains).
_VETO_LOSS_RE = re.compile(r" at ([+-]?\d+(?:\.\d+)?)% locks in")
# The re-entry price-guard rejection embeds the composite score (or "n/a") the
# same way: "... composite {c:+.2f} doesn't clear the +{override:g} override".
_REENTRY_COMPOSITE_RE = re.compile(r"composite (n/a|[+-]?\d+\.\d+)")

DEFAULT_CANDIDATES_NEW_NAME_FLOOR = (0.35, 0.40, 0.45, 0.50, 0.55, 0.60)
DEFAULT_CANDIDATES_MIN_CONVICTION = (0.10, 0.15, 0.20, 0.25, 0.30)
DEFAULT_CANDIDATES_REENTRY_OVERRIDE = (0.5, 0.75, 1.0, 1.25, 1.5)
DEFAULT_CANDIDATES_MAX_LOSS = (5.0, 6.0, 7.0, 8.0)
DEFAULT_CANDIDATES_RELEASE = (0.25, 0.5, 0.75, 1.0)


@dataclass
class CandidateRow:
    """One row of a knob sweep table. Not every field applies to every sweep —
    dollar-based sweeps (conviction floors, re-entry override) use
    losses_avoided/winners_missed/net; percentage-point sweeps (rotation guard)
    use pp_saved. `admits_from_journal` and `unresolved` are count-only caveats,
    never folded into a dollar/pp total."""
    value: float
    n_affected: int = 0
    losses_avoided: float = 0.0
    winners_missed: float = 0.0
    net: float = 0.0
    pp_saved: float = 0.0
    # Rejected journal proposals a LOWER (more permissive) candidate would admit.
    # Outcome unknown for these — count only, never priced.
    admits_from_journal: int = 0
    # Affected trades/chains whose outcome isn't resolved yet (still held) —
    # excluded from the $ / pp totals above, reported for transparency.
    unresolved: int = 0


@dataclass
class KnobSweep:
    name: str
    env_var: str
    current: float
    rows: list[CandidateRow] = field(default_factory=list)
    kind: str = "dollar"          # "dollar" | "pp"
    recommendation: str | None = None
    note: str = ""

    def row_header(self) -> str:
        if self.kind == "pp":
            return "| Candidate | Chains affected | Unresolved | Net pp saved | Note |"
        return (
            "| Candidate | Trades affected | Losses avoided | Winners missed "
            "| Net | Journal admits (looser only) | Note |"
        )

    def row_line(self, r: CandidateRow) -> str:
        note = "current" if abs(r.value - self.current) < 1e-9 else ""
        if self.kind == "pp":
            return (
                f"| {r.value:g} | {r.n_affected} | {r.unresolved} | "
                f"{r.pp_saved:+.1f}pp | {note} |"
            )
        return (
            f"| {r.value:g} | {r.n_affected} | ${r.losses_avoided:,.0f} | "
            f"${r.winners_missed:,.0f} | ${r.net:,.0f} | {r.admits_from_journal} | {note} |"
        )


def _safe_parse_ts(ts: str) -> datetime | None:
    try:
        d = datetime.fromisoformat(ts)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d
    except Exception:
        return None


def _load_window(
    ledger: TradeLedger, journal: DecisionJournal, days: int, now: datetime,
) -> tuple[list[RoundTrip], list[TradeRecord], list[DecisionRecord]]:
    """Trailing-`days` round-trips (by exit time) + ALL ledger records (episode
    joins for the re-entry/rotation sweeps need buys/sells outside the window
    too) + the trailing-`days` decisions-journal records."""
    cutoff = now - timedelta(days=days)
    all_records = ledger.effective()
    all_trips = round_trips(all_records)
    trips = [
        t for t in all_trips
        if (ts := _safe_parse_ts(t.exit_ts)) is not None and ts >= cutoff
    ]

    journal_recs: list[DecisionRecord] = []
    anchor_day = now.astimezone(_ET).date()
    # +2 padding absorbs the UTC/ET boundary so the oldest day in the window
    # can't get silently dropped.
    for i in range(days + 2):
        d = anchor_day - timedelta(days=i)
        dt = datetime(d.year, d.month, d.day, 12, 0, tzinfo=_ET)
        journal_recs.extend(journal.today(dt))
    return trips, all_records, journal_recs


def _dollar_recommendation(rows: list[CandidateRow], current: float, min_sample: int) -> str | None:
    others = [r for r in rows if abs(r.value - current) > 1e-9]
    if not others:
        return None
    best = max(others, key=lambda r: r.net)
    if best.n_affected < min_sample or best.net <= 0:
        return None
    if best.losses_avoided < 2 * best.winners_missed:
        return None  # not one-sided enough to trust on this little data
    return (
        f"{best.value:g} nets ${best.net:,.0f} (${best.losses_avoided:,.0f} avoided "
        f"vs ${best.winners_missed:,.0f} missed, n={best.n_affected}) vs current "
        f"{current:g} — {min_sample}+ trades of evidence; treat as a prior."
    )


def _pp_recommendation(rows: list[CandidateRow], current: float, min_sample: int) -> str | None:
    others = [r for r in rows if abs(r.value - current) > 1e-9]
    if not others:
        return None
    best = max(others, key=lambda r: r.pp_saved)
    if best.n_affected < min_sample or best.pp_saved <= 0:
        return None
    return (
        f"{best.value:g} would have saved {best.pp_saved:+.1f}pp across "
        f"{best.n_affected} veto chain(s) vs current {current:g} — treat as a prior."
    )


# --------------------------------------------------------------------------- #
# Conviction-floor sweeps (new-name floor, min_conviction)
# --------------------------------------------------------------------------- #

def _sweep_conviction_floor(
    trips: list[RoundTrip], journal_recs: list[DecisionRecord], current: float,
    candidates: tuple[float, ...], min_sample: int, journal_marker: str,
) -> list[CandidateRow]:
    rows = []
    for c in candidates:
        blocked = [
            t for t in trips
            if t.opening_conviction is not None and t.opening_conviction < c
        ]
        known = [t for t in blocked if t.realized_pl is not None]
        losses_avoided = sum(-t.realized_pl for t in known if t.realized_pl < 0)
        winners_missed = sum(t.realized_pl for t in known if t.realized_pl > 0)
        admits = 0
        if c < current:
            admits = sum(
                1 for dr in journal_recs
                if dr.action == "buy" and dr.verdict == "rejected"
                and journal_marker in (dr.reason or "") and dr.conviction >= c
            )
        rows.append(CandidateRow(
            value=c, n_affected=len(blocked),
            losses_avoided=losses_avoided, winners_missed=winners_missed,
            net=losses_avoided - winners_missed, admits_from_journal=admits,
            unresolved=len(blocked) - len(known),
        ))
    return rows


def sweep_new_name_floor(
    trips: list[RoundTrip], journal_recs: list[DecisionRecord], current: float,
    candidates: tuple[float, ...] = DEFAULT_CANDIDATES_NEW_NAME_FLOOR,
    min_sample: int = MIN_SAMPLE_DEFAULT,
) -> KnobSweep:
    rows = _sweep_conviction_floor(
        trips, journal_recs, current, candidates, min_sample,
        journal_marker="new-position floor",
    )
    return KnobSweep(
        name="New-name conviction floor", env_var="MIN_NEW_NAME_CONVICTION",
        current=current, rows=rows, kind="dollar",
        recommendation=_dollar_recommendation(rows, current, min_sample),
        note=(
            "Covers FRESH entries only (the episode-opening buy's conviction). "
            "'Journal admits' counts proposals REJECTED under the current floor "
            "that a looser candidate would let through — their outcome is "
            "unknown and no P&L is guessed for them."
        ),
    )


def sweep_min_conviction(
    trips: list[RoundTrip], journal_recs: list[DecisionRecord], current: float,
    candidates: tuple[float, ...] = DEFAULT_CANDIDATES_MIN_CONVICTION,
    min_sample: int = MIN_SAMPLE_DEFAULT,
) -> KnobSweep:
    rows = _sweep_conviction_floor(
        trips, journal_recs, current, candidates, min_sample,
        journal_marker="below floor",
    )
    return KnobSweep(
        name="Global conviction floor", env_var="MIN_CONVICTION",
        current=current, rows=rows, kind="dollar",
        recommendation=_dollar_recommendation(rows, current, min_sample),
        note=(
            "This floor also gates TOP-UPS, whose per-lot conviction isn't "
            "recorded at trip granularity — the $ table below covers fresh "
            "entries only; top-up exposure isn't separately quantified here."
        ),
    )


# --------------------------------------------------------------------------- #
# Re-entry price-override sweep
# --------------------------------------------------------------------------- #

def sweep_reentry_override(
    all_records: list[TradeRecord], journal_recs: list[DecisionRecord], current: float,
    candidates: tuple[float, ...] = DEFAULT_CANDIDATES_REENTRY_OVERRIDE,
    min_sample: int = MIN_SAMPLE_DEFAULT,
) -> KnobSweep:
    """Replays the price-aware re-entry guard: a buy that opens a new episode
    AT OR ABOVE the symbol's last exit price is a re-entry; risk.py rejects it
    unless the entry composite clears the override (and rejects outright when
    composite is None — it does NOT fail open, despite the docstring's original
    wording). Each candidate's counterfactual $ is the SUM of realized P&L across
    every sell in that re-entry's episode (a top-up mid-episode doesn't start a
    new one)."""
    ordered = sorted(all_records, key=lambda r: r.ts)
    open_qty: dict[str, float] = {}
    last_exit_price: dict[str, float] = {}
    episode_of: dict[str, int] = {}
    reentry_episodes: dict[int, float | None] = {}   # episode id -> entry composite
    episode_pl: dict[int, float] = {}
    episode_still_open: dict[int, bool] = {}
    next_id = 0

    for r in ordered:
        if r.action == "buy":
            qty_before = open_qty.get(r.symbol, 0.0)
            if qty_before <= 1e-9:
                next_id += 1
                eid = next_id
                episode_of[r.symbol] = eid
                episode_pl[eid] = 0.0
                episode_still_open[eid] = True
                exit_px = last_exit_price.get(r.symbol)
                if exit_px is not None and exit_px > 0 and (r.entry_price or 0.0) >= exit_px:
                    reentry_episodes[eid] = r.composite_score
            open_qty[r.symbol] = qty_before + float(r.qty or 0.0)
        elif r.action == "sell":
            eid = episode_of.get(r.symbol)
            new_qty = max(0.0, open_qty.get(r.symbol, 0.0) - float(r.qty or 0.0))
            open_qty[r.symbol] = new_qty
            if eid is not None and r.realized_pl is not None:
                episode_pl[eid] = episode_pl.get(eid, 0.0) + r.realized_pl
            if eid is not None and new_qty <= 1e-9:
                episode_still_open[eid] = False
            if r.exit_price:
                last_exit_price[r.symbol] = r.exit_price

    rows = []
    for T in candidates:
        blocked = [eid for eid, comp in reentry_episodes.items() if comp is None or comp < T]
        resolved = [eid for eid in blocked if not episode_still_open.get(eid, True)]
        losses_avoided = sum(-episode_pl[eid] for eid in resolved if episode_pl[eid] < 0)
        winners_missed = sum(episode_pl[eid] for eid in resolved if episode_pl[eid] > 0)
        admits = 0
        if T < current:
            for dr in journal_recs:
                if dr.action != "buy" or dr.verdict != "rejected":
                    continue
                if "chasing above the exit" not in (dr.reason or ""):
                    continue
                m = _REENTRY_COMPOSITE_RE.search(dr.reason)
                if not m or m.group(1) == "n/a":
                    continue
                try:
                    comp = float(m.group(1))
                except ValueError:
                    continue
                if comp >= T:
                    admits += 1
        rows.append(CandidateRow(
            value=T, n_affected=len(resolved),
            losses_avoided=losses_avoided, winners_missed=winners_missed,
            net=losses_avoided - winners_missed, admits_from_journal=admits,
            unresolved=len(blocked) - len(resolved),
        ))

    return KnobSweep(
        name="Re-entry price override (composite)", env_var="REENTRY_PRICE_OVERRIDE_COMPOSITE",
        current=current, rows=rows, kind="dollar",
        recommendation=_dollar_recommendation(rows, current, min_sample),
        note=(
            "Faithful-but-not-bit-exact vs the live guard (state.exit_prices "
            "warmth/cooldown aren't replayed, only 'buy at/above last exit "
            "price'). A None composite counts as blocked at every candidate."
        ),
    )


# --------------------------------------------------------------------------- #
# Rotation-guard sweeps (max-loss depth release, repeat-veto persistence release)
# --------------------------------------------------------------------------- #

@dataclass
class _VetoChain:
    symbol: str
    day: str
    vetoes: list[tuple[str, float]]   # (ts, loss_pct), chronological
    final_pl_pct: float | None        # the eventual resolving sell's realized_pl_pct


def _rotation_chains(
    journal_recs: list[DecisionRecord], all_records: list[TradeRecord],
) -> list[_VetoChain]:
    """Group rotation_guard sell-vetoes into one chain per (symbol, ET day),
    join each chain to the next realized sell after its last veto — the fill
    that actually closed the position (a model-approved exit or the bracket
    stop). A chain with no such sell is still-held: unresolved, not counted in
    any $ / pp total."""
    # Timestamps come from TWO different string formats — DecisionRecord.ts is
    # datetime.isoformat() ('T' separator) while TradeRecord.ts is a pydantic
    # datetime whose str() uses a SPACE separator — so a raw string ">"
    # comparison between them is NOT chronological (the separator character
    # itself sorts differently) even though within one format it would be.
    # Always parse to real datetimes (_safe_parse_ts) before comparing across
    # the two record types.
    by_key: dict[tuple[str, str], list[tuple[datetime, float]]] = {}
    for dr in journal_recs:
        if dr.action != "sell" or dr.verdict != "rotation_guard":
            continue
        m = _VETO_LOSS_RE.search(dr.reason or "")
        if not m:
            continue
        try:
            loss = float(m.group(1))
        except ValueError:
            continue
        ts = _safe_parse_ts(dr.ts)
        if ts is None:
            continue
        day = dr.ts[:10]
        by_key.setdefault((dr.symbol, day), []).append((ts, loss))

    sells = sorted(
        (
            (r, ts) for r in all_records
            if r.action == "sell" and r.realized_pl_pct is not None
            and (ts := _safe_parse_ts(str(r.ts))) is not None
        ),
        key=lambda pair: pair[1],
    )
    chains = []
    for (symbol, day), vetoes in by_key.items():
        vetoes.sort(key=lambda v: v[0])
        last_veto_ts = vetoes[-1][0]
        resolving = next(
            (r for r, ts in sells if r.symbol == symbol and ts > last_veto_ts), None,
        )
        chains.append(_VetoChain(
            symbol=symbol, day=day,
            vetoes=[(ts.isoformat(), loss) for ts, loss in vetoes],
            final_pl_pct=resolving.realized_pl_pct if resolving else None,
        ))
    return chains


def sweep_rotation_max_loss(
    chains: list[_VetoChain], current: float,
    candidates: tuple[float, ...] = DEFAULT_CANDIDATES_MAX_LOSS,
    min_sample: int = MIN_SAMPLE_DEFAULT,
) -> KnobSweep:
    resolved = [c for c in chains if c.final_pl_pct is not None]
    unresolved_n = len(chains) - len(resolved)
    rows = []
    for M in candidates:
        pp_saved = 0.0
        n_affected = 0
        for c in resolved:
            qualifying = next((loss for _, loss in c.vetoes if loss <= -M), None)
            if qualifying is None:
                continue  # this chain's veto never got deep enough to release at M
            n_affected += 1
            pp_saved += qualifying - c.final_pl_pct  # positive = release would've helped
        rows.append(CandidateRow(value=M, n_affected=n_affected, pp_saved=pp_saved, unresolved=unresolved_n))
    return KnobSweep(
        name="Rotation guard — deterioration (depth) release", env_var="ROTATION_GUARD_MAX_LOSS_PCT",
        current=current, rows=rows, kind="pp",
        recommendation=_pp_recommendation(rows, current, min_sample),
        note=(
            "Replays only the OBSERVED veto loss% and the eventual realized "
            "exit — no intraday price path is invented. pp saved can be "
            "negative for an individual chain if the enforced hold happened to "
            "recover; the total nets across all affected chains."
        ),
    )


def sweep_rotation_release(
    chains: list[_VetoChain], current: float,
    candidates: tuple[float, ...] = DEFAULT_CANDIDATES_RELEASE,
    min_sample: int = MIN_SAMPLE_DEFAULT,
) -> KnobSweep:
    resolved = [c for c in chains if c.final_pl_pct is not None]
    unresolved_n = len(chains) - len(resolved)
    rows = []
    for R in candidates:
        pp_saved = 0.0
        n_affected = 0
        for c in resolved:
            if len(c.vetoes) < 2:
                continue  # persistence release needs a REPEAT veto to ever fire
            baseline = c.vetoes[0][1]
            qualifying = next(
                (loss for _, loss in c.vetoes[1:] if loss <= baseline - R), None,
            )
            if qualifying is None:
                continue
            n_affected += 1
            pp_saved += qualifying - c.final_pl_pct
        rows.append(CandidateRow(value=R, n_affected=n_affected, pp_saved=pp_saved, unresolved=unresolved_n))
    return KnobSweep(
        name="Rotation guard — persistence (repeat-veto) release", env_var="ROTATION_GUARD_REPEAT_RELEASE_PCT",
        current=current, rows=rows, kind="pp",
        recommendation=_pp_recommendation(rows, current, min_sample),
        note="Same observed-events-only replay as the depth-release sweep above.",
    )


# --------------------------------------------------------------------------- #
# Report assembly + scheduling entry point
# --------------------------------------------------------------------------- #

def _write_report(
    out_path: Path, window_days: int, trip_count: int, journal_day_count: int,
    sweeps: list[KnobSweep], min_sample: int,
) -> None:
    lines = [
        f"# Auto-tune report — {out_path.stem}",
        "",
        f"Window: trailing {window_days} days | {trip_count} closed round-trips | "
        f"{journal_day_count} journal day(s) of decisions.",
        "",
        "**Report only — no knob is changed automatically.** Claude proposes, "
        "risk disposes; a human reviews this and edits `.env` by hand. Every "
        "recommendation below needs at least "
        f"{min_sample} affected trades and a one-sided win margin — small "
        "windows overfit easily, so treat any suggestion as a prior, not a rule.",
        "",
    ]
    for sw in sweeps:
        lines.append(f"## {sw.name} (`{sw.env_var}`, current {sw.current:g})")
        if sw.note:
            lines.append(sw.note)
        lines.append("")
        header = sw.row_header()
        lines.append(header)
        n_cols = len(header.strip().strip("|").split("|"))
        lines.append("|" + "|".join(["---"] * n_cols) + "|")
        for r in sw.rows:
            lines.append(sw.row_line(r))
        lines.append("")
        lines.append(
            f"**Suggestion:** {sw.recommendation}" if sw.recommendation
            else f"No recommendation — insufficient one-sided sample (need >= {min_sample} affected trades)."
        )
        lines.append("")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")


def run_autotune(
    cfg, ledger: TradeLedger, journal: DecisionJournal, days: int = 14,
    now: datetime | None = None, out_dir: Path = _AUTOTUNE_DIR,
    min_sample: int = MIN_SAMPLE_DEFAULT,
) -> Path | None:
    """Deterministic weekly replay of the ledger + decisions journal against the
    entry-quality risk knobs. Writes a markdown report and returns its path, or
    None on no data / any failure — must never raise into the trading loop
    (same contract as postmortem.run_postmortem)."""
    try:
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        trips, all_records, journal_recs = _load_window(ledger, journal, days, now)
        if not trips and not journal_recs:
            log.info("Auto-tune: no ledger/journal data in the trailing %d days; skipping.", days)
            return None

        r = cfg.risk
        sweeps = [
            sweep_new_name_floor(
                trips, journal_recs, r.min_new_name_conviction, min_sample=min_sample,
            ),
            sweep_min_conviction(
                trips, journal_recs, r.min_conviction, min_sample=min_sample,
            ),
            sweep_reentry_override(
                all_records, journal_recs, r.reentry_price_override_composite,
                min_sample=min_sample,
            ),
        ]
        chains = _rotation_chains(journal_recs, all_records)
        sweeps.append(sweep_rotation_max_loss(
            chains, r.rotation_guard_max_loss_pct, min_sample=min_sample,
        ))
        sweeps.append(sweep_rotation_release(
            chains, r.rotation_guard_repeat_release_pct, min_sample=min_sample,
        ))

        iso_year, iso_week, _ = now.astimezone(_ET).isocalendar()
        out_path = Path(out_dir) / f"{iso_year}-W{iso_week:02d}.md"
        journal_days = len({dr.ts[:10] for dr in journal_recs if dr.ts})
        _write_report(out_path, days, len(trips), journal_days, sweeps, min_sample)

        summary = " | ".join(
            f"{sw.env_var}: {sw.recommendation or 'no rec'}" for sw in sweeps
        )
        log.info("Weekly auto-tune written to %s — %s", out_path, summary)
        return out_path
    except Exception as e:
        log.warning("Weekly auto-tune failed: %s", e)
        return None


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(description="Weekly ledger-driven auto-tune report (read-only).")
    parser.add_argument("--days", type=int, default=14, help="Trailing window size in days.")
    parser.add_argument("--min-sample", type=int, default=MIN_SAMPLE_DEFAULT,
                         help="Min affected trades before a recommendation is emitted.")
    args = parser.parse_args()

    from .config import load_config

    cfg = load_config()
    ledger = TradeLedger()
    journal = DecisionJournal()

    path = run_autotune(cfg, ledger, journal, days=args.days, min_sample=args.min_sample)
    if path:
        print(f"Auto-tune report written to {path}")
    else:
        print("No auto-tune report produced (insufficient data, or a run failure — check logs).")
    sys.exit(0)
