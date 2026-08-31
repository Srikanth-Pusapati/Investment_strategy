#!/usr/bin/env python
"""Repair the two corrupt SELL rows the Aug-17 AMZN 5-leg MLEG unwind left in
state/trades.jsonl (Aug-23 measurement-integrity bundle, roadmap rank 6).

WHAT HAPPENED
The watchdog premium-stopped the merged 5-leg AMZN call group on 2026-08-17 and
ledgered the WHOLE group's realized P&L (-$14,956, -50.19%) on ONE row under
order 3404958e (rationale lists all five OCC contracts). Alpaca caps MLEG
orders at 4 legs, so the actual close went out as two broker orders: a 4-leg
MLEG parent (a1a6f7b7…) plus a single-leg order for AMZN260918C00240000
(a2614fc8…). Those two order ids were never ledgered by the watchdog, so the
F.1 exchange-exit backfill later picked them up blind and wrote two corrupt
rows:
  - the MLEG parent: symbol="None" (str() of the broker's null symbol),
    exit_price=-1.19 (the MLEG net print, junk as a per-share price),
    realized_pl=null
  - the single leg: symbol=AMZN260918C00240000 but instrument="equity",
    realized_pl=null

DECISION (from inspecting the data, per the task):
The -$14,956 on order 3404958e IS the whole group's realized P&L — the
watchdog computes it from every leg's unrealized P&L at close, and its
rationale names all five contracts. The two stray rows are therefore
INFORMATIONAL DUPLICATES of an exit that is already fully counted. Rewriting
them with a second copy of the loss would double-count it (the exact bug the
2026-07-23 T flatten had). So this script marks them informational:
  - realized_pl = 0.0 (contributes nothing to sum(realized_pl))
  - realized_pl_pct stays null (attribution's round_trips only emits a trip
    when pct is present — these rows stay invisible to attribution)
  - correct symbol/instrument/underlying/occ_symbols (never "None")
  - a repair_note documenting all of the above
  - the MLEG parent's junk exit_price (-1.19) is cleared to null

Idempotent: repaired rows no longer match the selection criteria (and rows
with a repair_note are skipped outright). Backs up the ledger to
trades.jsonl.bak-mleg-repair-<date> before the first write.

Run:  .venv/bin/python scripts/repair_mleg_ledger_rows.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LEDGER = REPO_ROOT / "state" / "trades.jsonl"

TARGET_DAY = "2026-08-17"
_OCC_RE = re.compile(r"\b([A-Z][A-Z0-9.]{0,5}\d{6}[CP]\d{8})\b")


def _stats(rows: list[dict]) -> tuple[float, int, int]:
    """(sum of realized_pl over SELL rows, null-P&L SELL count, symbol=='None' count)."""
    total = sum(
        r.get("realized_pl") or 0.0 for r in rows if r.get("action") == "sell"
    )
    nulls = sum(
        1 for r in rows
        if r.get("action") == "sell" and r.get("realized_pl") is None
    )
    nones = sum(1 for r in rows if r.get("symbol") == "None")
    return total, nulls, nones


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="Report what would change; write nothing.")
    args = ap.parse_args()

    if not LEDGER.exists():
        print(f"ERROR: {LEDGER} not found")
        return 1
    raw_lines = LEDGER.read_text(encoding="utf-8").splitlines()
    # (row dict, index into raw_lines) — only MODIFIED lines are reserialized;
    # untouched lines keep their exact original bytes.
    indexed = [(json.loads(l), i) for i, l in enumerate(raw_lines) if l.strip()]
    rows = [r for r, _ in indexed]
    line_of = {id(r): i for r, i in indexed}

    before = _stats(rows)

    # The authoritative group-close row: a watchdog option SELL on the target
    # day whose rationale names >= 2 OCC contracts. Its OCC set defines which
    # stray rows belong to the group.
    group_row = next(
        (r for r in rows
         if r.get("action") == "sell" and r.get("instrument") == "option"
         and str(r.get("ts", "")).startswith(TARGET_DAY)
         and len(_OCC_RE.findall(r.get("rationale", ""))) >= 2),
        None,
    )
    if group_row is None:
        print(f"No multi-leg option group close found on {TARGET_DAY}; "
              "nothing to repair.")
        return 0
    group_occs = _OCC_RE.findall(group_row["rationale"])
    group_under = group_row["symbol"]
    group_oid = group_row.get("order_id")
    group_pl = group_row.get("realized_pl")

    # Stray rows: SELLs on the target day that are corrupt (symbol=="None") or
    # carry no realized_pl — the backfill duplicates of the chunked close.
    changed = 0
    single_leg_occs: set[str] = set()
    strays: list[dict] = []
    for r in rows:
        if r.get("action") != "sell" or r.get("repair_note"):
            continue
        if not str(r.get("ts", "")).startswith(TARGET_DAY):
            continue
        if r.get("symbol") != "None" and r.get("realized_pl") is not None:
            continue
        if r is group_row:
            continue
        sym = r.get("symbol") or ""
        if sym != "None" and sym not in group_occs:
            print(f"WARNING: leaving unmatched stray SELL row alone: "
                  f"symbol={sym!r} order={r.get('order_id')}")
            continue
        strays.append(r)
        if sym in group_occs:
            single_leg_occs.add(sym)

    note_tail = (
        f"informational duplicate of the {group_under} 5-leg group close "
        f"(order {group_oid}): the group's full realized P&L "
        f"({group_pl:+,.0f} USD) is already ledgered there; this broker-side "
        "chunk fill is marked P&L-0 by scripts/repair_mleg_ledger_rows.py "
        "so it can never double-count."
    )
    for r in strays:
        if r.get("symbol") == "None":
            r["symbol"] = group_under
            r["instrument"] = "option"
            r["underlying"] = group_under
            # The MLEG parent chunk carried every group leg EXCEPT the ones
            # closed by their own single-leg orders.
            r["occ_symbols"] = [o for o in group_occs
                                if o not in single_leg_occs]
            if (r.get("exit_price") or 0) < 0:
                r["exit_price"] = None  # MLEG net print — junk per-share price
            r["repair_note"] = ("MLEG parent (symbol=None at broker); "
                                + note_tail)
        else:
            r["instrument"] = "option"
            r["underlying"] = group_under
            r["occ_symbols"] = [r["symbol"]]
            r["repair_note"] = ("single-leg chunk of the group close; "
                                + note_tail)
        r["realized_pl"] = 0.0
        changed += 1

    after = _stats(rows)
    print(f"Group close: {group_under} {group_oid} realized_pl={group_pl:+,.0f} "
          f"USD across {len(group_occs)} legs")
    print(f"Stray rows repaired: {changed}")
    print(f"BEFORE  sum(realized_pl)={before[0]:+,.2f}  "
          f"null-P&L SELLs={before[1]}  symbol=='None'={before[2]}")
    print(f"AFTER   sum(realized_pl)={after[0]:+,.2f}  "
          f"null-P&L SELLs={after[1]}  symbol=='None'={after[2]}")

    if changed == 0:
        print("Nothing to write (already repaired?).")
        return 0
    if args.dry_run:
        print("--dry-run: not writing.")
        return 0

    backup = LEDGER.with_name(
        f"trades.jsonl.bak-mleg-repair-{date.today().isoformat()}")
    if not backup.exists():
        shutil.copy2(LEDGER, backup)
        print(f"Backup written: {backup}")
    for r in strays:
        raw_lines[line_of[id(r)]] = json.dumps(
            r, separators=(",", ":"), ensure_ascii=False)
    LEDGER.write_text("\n".join(raw_lines) + "\n", encoding="utf-8")
    print(f"Ledger rewritten: {LEDGER} ({changed} line(s) changed, "
          f"{len(rows)} rows total)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
