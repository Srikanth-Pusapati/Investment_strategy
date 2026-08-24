"""Nightly post-mortem (B2).

After market close, feed the day's decision journal + ledger to Claude and
distill ≤3 one-line operating lessons. Lessons accumulate in a size-capped
curated file that is injected into every future decision prompt — this is the
structural answer to "keep a self-learning hat on always."

Run standalone:
  python -m investment_strategy.postmortem [--date 2026-07-06] [--dry-run]

Or triggered automatically by the orchestrator on the first market-closed
decision tick of each day (postmortem_enabled=True, default on).
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .usage import record_usage

log = logging.getLogger("postmortem")

_LESSONS_DIR = Path("state") / "lessons"
_CURATED_FILE = _LESSONS_DIR / "curated.md"

POSTMORTEM_PROMPT = """\
You are reviewing ONE trading day of your own automated decisions. \
The input below shows every equity buy decision (approved, resized, or rejected), \
slate exclusions, the final ledger for executed trades, the day's CLOSED \
positions with their realized P&L, exit reason, and original entry thesis, and \
(when price marks are available) a per-name DAY P&L block on CLOSE-TO-CLOSE \
marks. When that block is present, judge each name's day — and pick the day's \
winners and losers — by the CLOSE-TO-CLOSE number: the entry-basis realized \
column is secondary context and hides give-back on held winners (a name can \
"realize" a gain on a partial exit while losing far more on the shares still \
held).

Your task:
1. FIRST: find the largest DAY losers (close-to-close when available, else \
   realized) and diagnose WHY each ENTRY failed — \
   chased an extended move near a local high? entry thesis contradicted by the \
   exit (e.g. "bullish momentum" stopped out in 2 days)? conviction \
   miscalibrated (high conviction, bad outcome)? Dollar-loss patterns OUTRANK \
   ops observations: a lesson about what keeps losing money beats a lesson \
   about process noise.
2. Identify the 1–3 most important operating lessons for TOMORROW — concrete, \
   actionable, one-line imperatives (≤140 chars each).
3. Secondary checks: same-symbol concentration (did one name dominate?), \
   rejected-proposal waste (good ideas turned away for budget?), \
   churn (did the same name get re-proposed many times?), \
   missed diversification, or any guard that should have been tighter/looser.
4. IGNORE normal volatility and small losses — only flag repeatable patterns \
   worth changing.
5. Before emitting each lesson, FILTER it (do NOT write lessons ABOUT bias — use \
   this only to DISCARD weak ones): drop it if it is a one-day or single-name \
   blip dressed up as a repeatable pattern (recency/availability), a tidy story \
   the exit P&L does not actually support (narrative), or a mere re-assertion of \
   your existing style that the evidence does not force (confirmation). Each \
   surviving lesson is injected into EVERY future decision via a size-capped \
   file, so a bad one misdirects weeks of trading — when in doubt, drop it.

Return JSON: {"summary_md": "<concise markdown summary, ≤300 words>", \
"lessons": ["<lesson 1>", "<lesson 2>"]}  (0–3 lessons; empty list if nothing \
notable happened).

Lessons must be one-line imperatives ≤140 chars, e.g.:
  "When one symbol takes >50% of day's buy dollars, flag it and diversify next \
session."

Day data:
"""

# Structured-output schema for the nightly call. The API's json_schema subset
# rejects array constraints like maxItems (400 "property 'maxItems' is not
# supported") — the ≤3 limit lives in the prompt text and is enforced by
# truncation after parsing.
_POSTMORTEM_SCHEMA = {
    "type": "object",
    "properties": {
        "summary_md": {"type": "string"},
        "lessons": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary_md", "lessons"],
    # The API rejects object schemas without this (400
    # "additionalProperties must be explicitly set to false").
    "additionalProperties": False,
}


def read_curated(max_lines: int = 15) -> str:
    """Read the curated lessons file for injection into the decision prompt."""
    try:
        if not _CURATED_FILE.exists():
            return ""
        lines = [
            l for l in _CURATED_FILE.read_text(encoding="utf-8").splitlines()
            if l.strip()
        ]
        if not lines:
            return ""
        return "## Operating lessons (from your own nightly post-mortems — trusted)\n" + "\n".join(lines)
    except Exception:
        return ""


def _append_curated(lessons: list[str], max_lines: int = 15) -> None:
    """Append new lessons to the curated file, deduping and FIFO-capping."""
    try:
        _LESSONS_DIR.mkdir(parents=True, exist_ok=True)
        existing: list[str] = []
        if _CURATED_FILE.exists():
            existing = [
                l for l in _CURATED_FILE.read_text(encoding="utf-8").splitlines()
                if l.strip()
            ]
        seen = set(existing)
        for lesson in lessons:
            lesson = lesson.strip()
            if lesson and lesson not in seen:
                existing.append(lesson)
                seen.add(lesson)
        # FIFO: keep the newest max_lines
        kept = existing[-max_lines:]
        _CURATED_FILE.write_text("\n".join(kept) + "\n", encoding="utf-8")
    except Exception as e:
        log.warning("Could not update curated lessons: %s", e)


def day_marks(
    records, day: str, close_series, max_symbols: int = 25,
) -> tuple[list[str], str, str]:
    """Per-name DAY P&L on CLOSE-TO-CLOSE marks (Aug-23 measurement integrity).

    The old nightly review judged names by ENTRY-basis realized P&L, which
    named IESC 'the winner +$1,194' (a partial exit realized against a stale
    entry) on the exact day IESC was the book's biggest hit close-to-close
    (-$6.7k on the shares still held). This computes, per equity name active
    on `day`, the mark-to-market identity

        day_pl = qty_end*close_D - qty_start*close_{D-1} - buys_cost + sell_proceeds

    from the corrected ledger stream plus dated daily closes, keeping the old
    entry-basis realized number as a secondary column. Options have no equity
    close series — their day line is realized-only and labeled as such.

    `close_series(symbol, days)` -> [(iso_date, close), ...] ascending — the
    signature of AlpacaClient.daily_close_series, injectable for tests.
    Returns (lines, winner_line, loser_line); empty lines list = nothing to say.
    """
    start_qty: dict[str, float] = {}
    day_buys: dict[str, list] = {}
    day_sells: dict[str, list] = {}
    opt_realized: dict[str, float] = {}
    for r in sorted(records, key=lambda x: str(x.ts)):
        d = str(r.ts)[:10]
        if d > day:
            continue
        if (getattr(r, "instrument", "equity") or "equity") != "equity":
            if r.action == "sell" and d == day and r.realized_pl is not None:
                key = getattr(r, "underlying", None) or r.symbol
                opt_realized[key] = opt_realized.get(key, 0.0) + r.realized_pl
            continue
        if r.action == "buy":
            if d < day:
                start_qty[r.symbol] = start_qty.get(r.symbol, 0.0) + float(r.qty or 0.0)
            else:
                day_buys.setdefault(r.symbol, []).append(r)
        elif r.action == "sell":
            if d < day:
                held = start_qty.get(r.symbol, 0.0)
                q = float(r.qty or 0.0)
                # qty 0/unknown = full close (same rule as lots.py).
                start_qty[r.symbol] = max(0.0, held - q) if q > 0 else 0.0
            else:
                day_sells.setdefault(r.symbol, []).append(r)

    names = sorted(
        {s for s, q in start_qty.items() if q > 1e-9}
        | set(day_buys) | set(day_sells),
        key=lambda s: (
            -(1 if s in day_buys or s in day_sells else 0),
            -start_qty.get(s, 0.0), s,
        ),
    )[:max_symbols]

    marked: list[tuple[str, float, float, bool]] = []  # (sym, day_pl, realized, held_eod)
    lines: list[str] = []
    for sym in names:
        q0 = start_qty.get(sym, 0.0)
        buys = day_buys.get(sym, [])
        sells = day_sells.get(sym, [])
        buy_qty = sum(float(b.qty or 0.0) for b in buys)
        buy_cost = sum(
            float(b.qty or 0.0) * b.entry_price if (b.entry_price or 0) > 0
            else float(b.cost_usd or 0.0)
            for b in buys
        )
        sell_qty = proceeds = realized = 0.0
        priced = True
        for srec in sells:
            q = float(srec.qty or 0.0) or max(0.0, q0 + buy_qty - sell_qty)
            sell_qty += q
            if srec.exit_price and srec.exit_price > 0:
                proceeds += q * srec.exit_price
            else:
                priced = False
            if srec.realized_pl is not None:
                realized += srec.realized_pl
        q1 = max(0.0, q0 + buy_qty - sell_qty)
        try:
            series = close_series(sym, 12) or []
        except Exception:
            series = []
        day_close = next((c for dd, c in reversed(series) if dd == day), None)
        prior_close = next((c for dd, c in reversed(series) if dd < day), None)
        if (q0 > 1e-9 and prior_close is None) or (q1 > 1e-9 and day_close is None):
            priced = False
        if not priced:
            lines.append(
                f"  {sym:8} day P&L n/a (missing close/exit marks) | "
                f"realized today (entry-basis): {realized:+,.0f} USD"
            )
            continue
        day_pl = (
            q1 * (day_close or 0.0) - q0 * (prior_close or 0.0)
            - buy_cost + proceeds
        )
        marked.append((sym, day_pl, realized, q1 > 1e-9))
        lines.append(
            f"  {sym:8} day {day_pl:+,.0f} USD (close-to-close"
            + (", held into close" if q1 > 1e-9 else ", flat at close")
            + f") | realized today (entry-basis): {realized:+,.0f} USD"
        )
    for sym in sorted(opt_realized):
        lines.append(
            f"  {sym:8} (option) realized today (premium-basis): "
            f"{opt_realized[sym]:+,.0f} USD — no close-to-close mark for options"
        )
    winner_line = loser_line = ""
    if marked:
        w = max(marked, key=lambda t: t[1])
        l = min(marked, key=lambda t: t[1])
        winner_line = f"Day WINNER by close-to-close: {w[0]} {w[1]:+,.0f} USD"
        loser_line = f"Day LOSER by close-to-close: {l[0]} {l[1]:+,.0f} USD"
        lines.append(f"  -> {winner_line}; {loser_line}")
    return lines, winner_line, loser_line


def run_postmortem(
    cfg,
    ledger,
    journal,
    day: str,
    dry_run: bool = False,
    max_lessons: int = 15,
    broker=None,
) -> dict | None:
    """Core post-mortem logic. Returns parsed output dict or None on failure."""
    import json
    from datetime import datetime

    import anthropic

    from .journal import _ET

    # `day` labels an ET trading day, so anchor the lookup datetime in ET.
    # Anchored at UTC midnight it lands on the PREVIOUS ET date and reads a
    # journal file that doesn't exist (2026-07-13's post-mortem skipped with
    # "No journal records" despite a 22 KB decisions file).
    recs = journal.today(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=_ET))
    if not recs:
        log.info("No journal records for %s; skipping post-mortem.", day)
        return None

    # Build input text from journal records
    buy_lines = []
    guard_lines = []
    for r in recs:
        if r.action == "buy":
            buy_lines.append(
                f"  {r.ts[11:16]} {r.verdict.upper():15} {r.symbol:8} "
                f"conv={r.conviction:.2f} ${r.approved_notional:,.0f} | {r.reason[:80]}"
            )
        elif r.action == "sell":
            # Guard-vetoed sells (e.g. rotation_guard) — the postmortem must
            # weigh 'the guard pinned a loser that kept falling' against 'the
            # guard saved a bottom-tick sale', or the guard's real-world cost
            # is invisible to the exact loop built to catch it.
            guard_lines.append(
                f"  {r.ts[11:16]} {r.verdict.upper():15} {r.symbol:8} "
                f"conv={r.conviction:.2f} | {r.reason[:100]}"
            )
    ledger_lines = []
    sell_lines = []
    records = []
    try:
        records = ledger.effective()
        # Entry-rationale head per symbol (latest buy wins) so a closed trade
        # shows its thesis next to its outcome — the thesis-vs-exit mismatch is
        # the pattern task 1 exists to catch.
        entry_rationale: dict[str, str] = {}
        for t in records:
            if t.action == "buy" and getattr(t, "rationale", ""):
                entry_rationale[t.symbol] = t.rationale[:90]
        for t in records:
            if hasattr(t, "ts") and str(t.ts)[:10] == day and t.action == "buy":
                ledger_lines.append(
                    f"  {str(t.ts)[11:16]} BUY {t.symbol:8} "
                    f"${t.cost_usd if hasattr(t, 'cost_usd') and t.cost_usd else 0:,.0f}"
                )
        day_sells = [
            t for t in records
            if hasattr(t, "ts") and str(t.ts)[:10] == day
            and t.action == "sell" and t.realized_pl is not None
        ]
        # Worst first — task 1 leads with the largest realized losers.
        day_sells.sort(key=lambda t: t.realized_pl)
        for t in day_sells[:15]:
            why = entry_rationale.get(t.symbol, "")
            sell_lines.append(
                f"  {str(t.ts)[11:16]} CLOSE {t.symbol:8} "
                f"{t.realized_pl:+,.0f} USD ({(t.realized_pl_pct or 0):+.1f}%) "
                f"exit={t.exit_reason or '?'}"
                + (f" | entry thesis: {why}" if why else "")
            )
    except Exception:
        pass

    user_text = POSTMORTEM_PROMPT + f"Date: {day}\n\nDecisions:\n"
    user_text += "\n".join(buy_lines[:100]) or "  (none)"
    if guard_lines:
        user_text += "\n\nSell decisions blocked/vetoed by guards:\n"
        user_text += "\n".join(guard_lines[:20])
    user_text += "\n\nExecuted trades:\n"
    user_text += "\n".join(ledger_lines[:50]) or "  (none)"
    user_text += "\n\nClosed positions (realized P&L, worst first):\n"
    user_text += "\n".join(sell_lines) or "  (none)"

    # Per-name CLOSE-TO-CLOSE day attribution (Aug-23): entry-basis realized
    # P&L alone crowned IESC 'the winner +$1,194' on the day it was the book's
    # biggest close-to-close hit (-$6.7k on the held shares). Best-effort: no
    # broker / no marks -> the block is simply absent and the prompt says so.
    marks_lines: list[str] = []
    try:
        b = broker
        if b is None and cfg is not None:
            from .execution.alpaca_client import AlpacaClient
            b = AlpacaClient(cfg)
        if b is not None and records:
            marks_lines, winner_line, loser_line = day_marks(
                records, day, b.daily_close_series)
            if marks_lines:
                log.info(
                    "Postmortem day attribution (close-to-close): %d name(s); "
                    "%s; %s", len(marks_lines),
                    winner_line or "no winner", loser_line or "no loser",
                )
    except Exception as e:
        log.warning("Postmortem close-to-close marks unavailable: %s", e)
    if marks_lines:
        user_text += (
            "\n\nPer-name DAY P&L (CLOSE-TO-CLOSE marks: prior close -> "
            "today's close/exit on today's position — judge winners/losers by "
            "THIS number; the entry-basis realized column is secondary):\n"
        )
        user_text += "\n".join(marks_lines)

    # Deterministic behavior diagnostics — the numeric counterpart to the prose
    # diagnosis above (which only eyeballs these from raw trade lines).
    # Disposition effect + overtrading come from the full ledger; the anti-chase
    # gate bind-rate comes from TODAY's journal and measures whether the gate
    # built for this book's dominant loss pattern is actually binding.
    from .attribution import behavior_diagnostics
    diag = behavior_diagnostics(ledger)
    buy_props = [r for r in recs if r.action == "buy"]
    if buy_props:
        gate_hits = sum(1 for r in buy_props if "overext" in (r.reason or "").lower())
        gate_line = (
            f"Anti-chase gate: {gate_hits}/{len(buy_props)} buy proposals hit the "
            f"overextension gate today ({gate_hits / len(buy_props) * 100:.0f}%)."
        )
        if not diag:
            diag = ["## Behavior diagnostics (deterministic — trusted)"]
        diag.append(gate_line)
    if diag:
        user_text += "\n\n" + "\n".join(diag)

    if dry_run:
        print(user_text)
        return {"summary_md": "(dry-run)", "lessons": []}

    try:
        client = anthropic.Anthropic(
            api_key=cfg.anthropic_api_key, timeout=60.0, max_retries=1,
        )
        resp = client.messages.create(
            model=cfg.decision_model,
            max_tokens=1000,
            output_config={
                "effort": "low",
                "format": {"type": "json_schema", "schema": _POSTMORTEM_SCHEMA},
            },
            messages=[{"role": "user", "content": user_text}],
        )
    except Exception as e:
        log.error("Post-mortem Claude call failed: %s", e)
        return None

    record_usage(resp, cfg.decision_model, "postmortem")

    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        result = json.loads(text)
    except Exception:
        log.warning("Post-mortem response not valid JSON; skipping.")
        return None

    # Write the full summary
    try:
        _LESSONS_DIR.mkdir(parents=True, exist_ok=True)
        day_file = _LESSONS_DIR / f"{day}.md"
        day_file.write_text(result.get("summary_md", ""), encoding="utf-8")
        log.info("Post-mortem written to %s", day_file)
    except Exception as e:
        log.warning("Could not write post-mortem file: %s", e)

    lessons = [l for l in result.get("lessons", []) if isinstance(l, str) and l.strip()][:3]
    if lessons:
        log.info("Post-mortem lessons: %s", " | ".join(lessons))
        _append_curated(lessons, max_lessons)
    else:
        log.info("Post-mortem found no notable patterns for %s.", day)

    return result


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(description="Run the nightly post-mortem for a trading day.")
    parser.add_argument("--date", default=None, help="YYYY-MM-DD (default: today ET)")
    parser.add_argument("--dry-run", action="store_true", help="Print prompt; don't call Claude.")
    args = parser.parse_args()

    from .config import load_config
    from .journal import DecisionJournal, _trading_day
    from .ledger import TradeLedger

    cfg = load_config()
    ledger = TradeLedger()
    journal = DecisionJournal()
    day = args.date or _trading_day()

    result = run_postmortem(cfg, ledger, journal, day, dry_run=args.dry_run,
                            max_lessons=cfg.postmortem_max_lessons)
    if result and not args.dry_run:
        print(f"Summary written to state/lessons/{day}.md")
        print("Curated lessons updated.")
    sys.exit(0)
