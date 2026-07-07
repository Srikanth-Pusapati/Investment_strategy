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

log = logging.getLogger("postmortem")

_LESSONS_DIR = Path("state") / "lessons"
_CURATED_FILE = _LESSONS_DIR / "curated.md"

POSTMORTEM_PROMPT = """\
You are reviewing ONE trading day of your own automated decisions. \
The input below shows every equity buy decision (approved, resized, or rejected), \
slate exclusions, and the final ledger for executed trades.

Your task:
1. Identify the 1–3 most important operating lessons for TOMORROW — concrete, \
   actionable, one-line imperatives (≤140 chars each).
2. Focus on: same-symbol concentration (did one name dominate?), \
   rejected-proposal waste (good ideas turned away for budget?), \
   churn (did the same name get re-proposed many times?), \
   missed diversification, or any guard that should have been tighter/looser.
3. IGNORE normal volatility or small losses — only flag systemic patterns \
   worth changing.

Return JSON: {"summary_md": "<concise markdown summary, ≤300 words>", \
"lessons": ["<lesson 1>", "<lesson 2>"]}  (0–3 lessons; empty list if nothing \
notable happened).

Lessons must be one-line imperatives ≤140 chars, e.g.:
  "When one symbol takes >50% of day's buy dollars, flag it and diversify next \
session."

Day data:
"""


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


def run_postmortem(
    cfg,
    ledger,
    journal,
    day: str,
    dry_run: bool = False,
    max_lessons: int = 15,
) -> dict | None:
    """Core post-mortem logic. Returns parsed output dict or None on failure."""
    import json
    import anthropic

    recs = journal.today(
        __import__("datetime").datetime.strptime(day, "%Y-%m-%d").replace(
            tzinfo=__import__("datetime").timezone.utc
        )
    )
    if not recs:
        log.info("No journal records for %s; skipping post-mortem.", day)
        return None

    # Build input text from journal records
    buy_lines = []
    for r in recs:
        if r.action == "buy":
            buy_lines.append(
                f"  {r.ts[11:16]} {r.verdict.upper():15} {r.symbol:8} "
                f"conv={r.conviction:.2f} ${r.approved_notional:,.0f} | {r.reason[:80]}"
            )
    ledger_lines = []
    try:
        for t in ledger.effective():
            if hasattr(t, "ts") and str(t.ts)[:10] == day and t.action == "buy":
                ledger_lines.append(
                    f"  {str(t.ts)[11:16]} BUY {t.symbol:8} "
                    f"${t.cost_usd if hasattr(t, 'cost_usd') and t.cost_usd else 0:,.0f}"
                )
    except Exception:
        pass

    user_text = POSTMORTEM_PROMPT + f"Date: {day}\n\nDecisions:\n"
    user_text += "\n".join(buy_lines[:100]) or "  (none)"
    user_text += "\n\nExecuted trades:\n"
    user_text += "\n".join(ledger_lines[:50]) or "  (none)"

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
                "format": {
                    "type": "json_schema",
                    "schema": {
                        "type": "object",
                        "properties": {
                            "summary_md": {"type": "string"},
                            "lessons": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
                        },
                        "required": ["summary_md", "lessons"],
                    },
                },
            },
            messages=[{"role": "user", "content": user_text}],
        )
    except Exception as e:
        log.error("Post-mortem Claude call failed: %s", e)
        return None

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

    lessons = [l for l in result.get("lessons", []) if isinstance(l, str) and l.strip()]
    if lessons:
        log.info("Post-mortem lessons: %s", " | ".join(lessons))
        _append_curated(lessons, max_lines)
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
