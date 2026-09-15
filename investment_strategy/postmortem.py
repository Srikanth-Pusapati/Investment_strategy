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


_SUPERSEDED_MARK = "[SUPERSEDED"


def read_curated(max_lines: int = 15) -> str:
    """Read the curated lessons file for injection into the decision prompt.

    Lines the operator has struck by prefixing '[SUPERSEDED ...]' stay in the
    file (history) but are never rendered. Whether the result is injected at
    all is the caller's knob (Config CURATED_LESSONS_INJECT, run-6 off).
    """
    try:
        if not _CURATED_FILE.exists():
            return ""
        lines = [
            l for l in _CURATED_FILE.read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.lstrip().startswith(_SUPERSEDED_MARK)
        ]
        if not lines:
            return ""
        if max_lines > 0:
            lines = lines[-max_lines:]  # newest max_lines (FIFO, same as the writer)
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


def _option_leg_signs(rec) -> list[tuple[str, float]] | None:
    """[(occ, +1|-1), ...] for an option BUY row, or None when the legs can't
    be signed (no occ_symbols, or a multi-leg row without occ_sides)."""
    occs = list(getattr(rec, "occ_symbols", None) or [])
    if not occs:
        return None
    sides = list(getattr(rec, "occ_sides", None) or [])
    if len(occs) == 1 and not sides:
        return [(occs[0], 1.0)]  # single leg: only debit (long) structures exist
    if len(sides) != len(occs):
        return None
    return [(o, -1.0 if str(sd).lower().startswith("sell") else 1.0)
            for o, sd in zip(occs, sides)]


def _option_key(rec) -> str:
    under = getattr(rec, "underlying", None)
    if under:
        return under
    sym = rec.symbol or ""
    return sym[:-15] if len(sym) > 15 and sym[-15:-9].isdigit() else sym


def option_day_marks(
    records, day: str, option_close_series,
) -> tuple[list[str], list[tuple[str, float, float, bool]]]:
    """Close-to-close DAY P&L per option GROUP (keyed by underlying — the
    same grouping the watchdog exits under), run-6 item 1a. Each leg is
    valued from daily option bars (`option_close_series(occ, days)` ->
    [(iso_date, close)] per-share, the shape of AlpacaClient.option_close_series)
    with the identity

        day_pl = mark_end - mark_start - buys_cost + sell_proceeds

    where marks are Σ sign·contracts·100·close and a sell's proceeds are its
    closed basis + realized_pl (option exit rows carry no per-leg price).
    Groups that can't be valued are reported as UNMARKED with the reason —
    never silently dropped. Returns (lines, marked) where marked mirrors
    day_marks' (name, day_pl, realized, held_eod) tuples."""
    groups: dict[str, dict] = {}
    for r in sorted(records, key=lambda x: str(x.ts)):
        if (getattr(r, "instrument", "equity") or "equity") == "equity":
            continue
        d = str(r.ts)[:10]
        if d > day:
            continue
        g = groups.setdefault(_option_key(r), {
            "open": [], "day_buy_cost": 0.0, "proceeds": 0.0,
            "realized": 0.0, "unpriced": None, "traded_today": False,
        })
        if r.action == "buy":
            lot = {"qty": float(r.qty or 0.0), "cost": float(r.cost_usd or 0.0),
                   "legs": _option_leg_signs(r), "opened": d}
            if lot["qty"] <= 0:
                continue
            g["open"].append(lot)
            if d == day:
                g["traded_today"] = True
                g["day_buy_cost"] += lot["cost"]
        elif r.action == "sell":
            q = float(r.qty or 0.0)
            remaining = q if q > 0 else float("inf")  # qty 0 = full close
            closed_basis = 0.0
            for lot in g["open"]:
                if remaining <= 0 or lot["qty"] <= 0:
                    continue
                take = min(lot["qty"], remaining)
                closed_basis += lot["cost"] * (take / lot["qty"])
                lot["cost"] -= lot["cost"] * (take / lot["qty"])
                lot["qty"] -= take
                remaining -= take
            g["open"] = [lot for lot in g["open"] if lot["qty"] > 1e-9]
            if d == day:
                g["traded_today"] = True
                if r.realized_pl is None:
                    g["unpriced"] = "sell without realized_pl"
                else:
                    g["realized"] += r.realized_pl
                    # Cash proceeds of the close = closed basis + realized.
                    # The identity then charges a lot opened earlier at its
                    # PRIOR-close mark (start_mark) and a lot opened today at
                    # its cost (day_buy_cost) — same shape as the equity path.
                    g["proceeds"] += closed_basis + r.realized_pl

    lines: list[str] = []
    marked: list[tuple[str, float, float, bool]] = []
    for key in sorted(groups):
        g = groups[key]
        held = g["open"]
        if not held and not g["traded_today"]:
            continue  # closed before today — nothing to say
        if not callable(option_close_series):
            lines.append(
                f"  {key:8} (option) UNMARKED — no option close source; "
                f"realized today (premium-basis): {g['realized']:+,.0f} USD"
            )
            continue
        why = g["unpriced"]
        cache: dict[str, list] = {}

        def _px(occ: str, when: str) -> float | None:
            if occ not in cache:
                try:
                    cache[occ] = option_close_series(occ, 12) or []
                except Exception:
                    cache[occ] = []
            ser = cache[occ]
            if when == "day":
                return next((c for dd, c in reversed(ser) if dd == day), None)
            return next((c for dd, c in reversed(ser) if dd < day), None)

        def _mark(lots, when: str) -> float | None:
            nonlocal why
            total = 0.0
            for lot in lots:
                if lot["legs"] is None:
                    why = why or "leg sides unknown on the ledger row"
                    return None
                for occ, sign in lot["legs"]:
                    c = _px(occ, when)
                    if c is None:
                        why = why or f"no {'today' if when == 'day' else 'prior'} bar for {occ}"
                        return None
                    total += sign * lot["qty"] * 100.0 * c
            return total

        start_lots = _lots_open_before(records, key, day)  # open at start of day
        start_mark = _mark(start_lots, "prior") if start_lots else 0.0
        end_lots = held
        end_mark = _mark(end_lots, "day") if end_lots else 0.0
        if why or start_mark is None or end_mark is None:
            lines.append(
                f"  {key:8} (option) UNMARKED ({why or 'missing marks'}) — "
                f"realized today (premium-basis): {g['realized']:+,.0f} USD"
            )
            continue
        day_pl = end_mark - start_mark - g["day_buy_cost"] + g["proceeds"]
        marked.append((key, day_pl, g["realized"], bool(end_lots)))
        lines.append(
            f"  {key:8} (option) day {day_pl:+,.0f} USD (close-to-close on "
            f"option daily bars"
            + (", held into close" if end_lots else ", flat at close")
            + f") | realized today (premium-basis): {g['realized']:+,.0f} USD"
        )
    return lines, marked


def _lots_open_before(records, key: str, day: str) -> list[dict]:
    """Option lots of group `key` still open at the START of `day`."""
    lots: list[dict] = []
    for r in sorted(records, key=lambda x: str(x.ts)):
        if (getattr(r, "instrument", "equity") or "equity") == "equity":
            continue
        if str(r.ts)[:10] >= day or _option_key(r) != key:
            continue
        if r.action == "buy" and float(r.qty or 0.0) > 0:
            lots.append({"qty": float(r.qty or 0.0), "legs": _option_leg_signs(r)})
        elif r.action == "sell":
            q = float(r.qty or 0.0)
            remaining = q if q > 0 else float("inf")
            for lot in lots:
                take = min(lot["qty"], remaining)
                lot["qty"] -= take
                remaining -= take
            lots = [lot for lot in lots if lot["qty"] > 1e-9]
    return lots


def day_marks(
    records, day: str, close_series, max_symbols: int = 25,
    option_close_series=None,
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
    `option_close_series` (run-6 item 1a) is the same shape for OCC contracts
    (AlpacaClient.option_close_series); when given, open option groups get a
    close-to-close line too (see option_day_marks) and compete for the day's
    winner/loser; when None, option lines stay realized-only and say so.
    Returns (lines, winner_line, loser_line); empty lines list = nothing to say.
    The '  -> Day WINNER…' summary line is NOT a name — use name_count().
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
    if option_close_series is not None:
        opt_lines, opt_marked = option_day_marks(records, day, option_close_series)
        lines.extend(opt_lines)
        marked.extend(opt_marked)
    else:
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


def name_count(marks_lines: list[str]) -> int:
    """Number of NAME lines in a day_marks block (the trailing
    '  -> Day WINNER…' summary is not a name — the old len() counted it)."""
    return sum(1 for l in marks_lines if not l.lstrip().startswith("->"))


def exclude_core_fill(records) -> tuple[list, list[str]]:
    """Drop core-satellite fill rows AND every other row of the symbols they
    filled (run-6 item 1a). A `core_fill` buy is not a model decision — it
    deploys idle cash into the CORE_ETF under CORE_MAX_PCT — so it must not
    feed the post-mortem's buy/concentration inputs (Aug 24 wrote a wrong
    'cap QQQ' lesson from $150k of core fills). The symbol's sells go with it:
    a core stop with no matching buy would read as a full-proceeds 'gain' in
    the close-to-close identity. Returns (kept_records, excluded_symbols)."""
    core_syms = sorted({
        r.symbol for r in records
        if r.action == "buy" and "core_fill" in (getattr(r, "entry_signals", None) or [])
    })
    if not core_syms:
        return list(records), []
    keep = [r for r in records if r.symbol not in core_syms]
    return keep, core_syms


def _no_option_marks(_occ: str, _days: int) -> list:
    """Stand-in option close source: the broker has none, so every option
    group is reported UNMARKED (explicit line) rather than silently
    realized-only."""
    return []


# --------------------------------------------------------------------------- #
# Run-7 S-8 / 4a-17: deterministic hedge counters (log + ledger; no LLM)
# --------------------------------------------------------------------------- #
HEDGE_WHIPSAW_SESSIONS = 2


def _weekday_sessions_between(d0, d1) -> int:
    """Mon-Fri days strictly after date d0 up to and including d1 (0 for the
    same day; a weekend gap adds nothing). Exchange holidays are NOT
    subtracted: a re-arm two weekdays after an unwind is a whipsaw whether
    or not one of them was a holiday — the counter errs conservative."""
    from datetime import timedelta
    if d1 <= d0:
        return 0
    n, d = 0, d0
    while d < d1:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def _day_log_lines(day: str, log_dir=None) -> list[str]:
    """Every bot-log line stamped with ET date `day` ('YYYY-MM-DD ...').
    The live file is logs/bot.log until local midnight, then the
    Sep_10_2026.log-style rotated name (__main__.dated_log_name); the
    post-mortem normally runs after the close (live file) but may be
    re-run next day (rotated file), so both are read and lines are kept
    by their own date stamp. Unreadable = []."""
    import os
    from datetime import datetime
    from pathlib import Path
    base = Path(log_dir or os.getenv("LOG_DIR", "logs") or "logs")
    try:
        d = datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        return []
    out: list[str] = []
    for name in ("bot.log", d.strftime("%b_%d_%Y") + ".log"):
        p = base / name
        try:
            if not p.exists():
                continue
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith(day + " "):
                    out.append(line)
        except OSError:
            continue
    return out


def hedge_diagnostics(day: str, ledger, log_dir=None) -> list[str]:
    """Deterministic hedge counters for the nightly post-mortem (4a-17):

    - `HEDGE WHIPSAW`: auto-hedge ARMS on `day` (ledger buy rows carrying
      entry_signals ['auto_hedge']) whose latest preceding hedge_unwind
      sell lies within HEDGE_WHIPSAW_SESSIONS weekday sessions. Run-6:
      Sep 9 09:22 unwind -> Sep 10 11:06 re-arm (1 session) at 26.09 vs
      the 25.84 exit — the fixture this counter is pinned to.
    - `BOOK BETA CAP -> next-cycle arm`: decision cycles (delimited by the
      ONE 'BOOK BETA:' reading line each) that logged a 'BOOK BETA CAP:'
      resize/reject and whose NEXT cycle armed the beta hedge
      ('AUTO-HEDGE: beta:'). Cap 1.20 vs arm line 1.15 means every
      cap-bound buy lands inside the arm zone (run-6: Sep 3 09:25 cap ->
      10:14 arm; Sep 10 10:19 cap -> 11:06 arm); operator decision 6 keeps
      MAX_BOOK_BETA_SPY at 1.2 until this count says otherwise.
    - the day's last `HEDGE COUNTERFACTUAL:` line, verbatim, when present.

    Reads the ledger (cross-day: an unwind may be sessions old) and the
    day's log; never raises; a missing log yields an 'n/a' line, never a
    silent zero (a handle that matches nothing is a measurement breach)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    lines: list[str] = []
    try:
        day_date = datetime.strptime(day, "%Y-%m-%d").date()
    except ValueError:
        return lines

    # -- whipsaw (ledger) ------------------------------------------------- #
    try:
        rows = ledger.effective() if hasattr(ledger, "effective") else ledger.all()
    except Exception as e:  # noqa: BLE001
        rows = []
        lines.append(f"HEDGE WHIPSAW: n/a (ledger unreadable: {e})")
    if rows or not lines:
        def _et(ts):
            try:
                return ts.astimezone(et)
            except Exception:  # noqa: BLE001 — naive/odd stamps: treat as ET
                return ts
        arms = [r for r in rows if getattr(r, "action", "") == "buy"
                and "auto_hedge" in (getattr(r, "entry_signals", None) or [])]
        unwinds = [r for r in rows if getattr(r, "action", "") == "sell"
                   and getattr(r, "exit_reason", "") == "hedge_unwind"]
        today_arms = [a for a in arms if _et(a.ts).date() == day_date]
        whip = 0
        details: list[str] = []
        try:
            for a in today_arms:
                prior = [u for u in unwinds if u.ts < a.ts]
                if not prior:
                    continue
                u = max(prior, key=lambda r: r.ts)
                gap = _weekday_sessions_between(_et(u.ts).date(), _et(a.ts).date())
                if gap <= HEDGE_WHIPSAW_SESSIONS:
                    whip += 1
                    details.append(
                        f"{a.symbol} unwound {_et(u.ts).strftime('%Y-%m-%d %H:%M')} ET "
                        f"-> re-armed {_et(a.ts).strftime('%Y-%m-%d %H:%M')} ET, "
                        f"{gap} session(s), ${float(getattr(a, 'cost_usd', 0.0) or 0.0):,.0f}"
                    )
        except TypeError as e:      # naive vs aware stamps on pre-field rows
            lines.append(f"HEDGE WHIPSAW: n/a (ledger stamps not comparable: {e})")
            today_arms = []
            whip = -1
        if whip < 0:
            pass
        elif not today_arms:
            lines.append("HEDGE WHIPSAW: 0 (no auto-hedge arm today).")
        else:
            tail = "; ".join(details) if details else "no unwind within the window"
            lines.append(
                f"HEDGE WHIPSAW: {whip} of {len(today_arms)} arm(s) today re-armed "
                f"within {HEDGE_WHIPSAW_SESSIONS} sessions of an unwind ({tail})."
            )

    # -- cap -> next-cycle arm pairs (day's log) --------------------------- #
    log_lines = _day_log_lines(day, log_dir)
    if not log_lines:
        lines.append(
            f"BOOK BETA CAP -> next-cycle arm: n/a (no log lines for {day})."
        )
    else:
        cycles: list[dict] = []
        for line in log_lines:
            if "| BOOK BETA: " in line:
                cycles.append({"cap": False, "arm": False})
                continue
            if not cycles:
                continue
            if "BOOK BETA CAP:" in line:
                cycles[-1]["cap"] = True
            if "AUTO-HEDGE: beta:" in line:
                cycles[-1]["arm"] = True
        pairs = sum(
            1 for i in range(len(cycles) - 1)
            if cycles[i]["cap"] and cycles[i + 1]["arm"]
        )
        lines.append(
            f"BOOK BETA CAP -> next-cycle arm: {pairs} pair(s) today "
            f"(cycles {len(cycles)}; cap-bound cycles "
            f"{sum(1 for c in cycles if c['cap'])}; arms "
            f"{sum(1 for c in cycles if c['arm'])})."
        )
        cf = [l for l in log_lines if "HEDGE COUNTERFACTUAL:" in l]
        if cf:
            lines.append(cf[-1].split(" | ", 1)[-1])
    return lines


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
    core_excluded: list[str] = []
    try:
        records, core_excluded = exclude_core_fill(ledger.effective())
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
    if core_excluded:
        user_text += (
            f"\n  (core ETF rows excluded — {', '.join(core_excluded)} is the "
            "system core, filled by rule under CORE_MAX_PCT, not a decision: "
            "do not write lessons about its size)"
        )
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
            opt_src = None
            if getattr(cfg, "postmortem_option_marks", True):
                opt_src = getattr(b, "option_close_series", None)
                if not callable(opt_src):
                    # Explicit hook so a non-Alpaca broker can supply marks;
                    # a missing source degrades to an explicit UNMARKED line.
                    opt_src = getattr(b, "option_close_marks", None)
                    opt_src = opt_src if callable(opt_src) else _no_option_marks
            marks_lines, winner_line, loser_line = day_marks(
                records, day, b.daily_close_series,
                option_close_series=opt_src)
            if marks_lines:
                log.info(
                    "Postmortem day attribution (close-to-close): %d name(s); "
                    "%s; %s", name_count(marks_lines),
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
    # Run-7 S-8 / 4a-17: hedge counters — whipsaw, cap->arm pairs, and the
    # day's counterfactual line. Deterministic; a failure never costs the
    # post-mortem.
    try:
        hedge_lines = hedge_diagnostics(day, ledger)
    except Exception as e:  # noqa: BLE001
        log.warning("Post-mortem hedge diagnostics failed: %s", e)
        hedge_lines = []
    if hedge_lines:
        if not diag:
            diag = ["## Behavior diagnostics (deterministic — trusted)"]
        diag.extend(hedge_lines)
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
