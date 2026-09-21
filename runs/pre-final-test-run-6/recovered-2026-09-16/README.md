# Recovered from the dead primary session — Sep 15/16, 2026

**Provenance:** every file here was authored by the operator's interactive
session (`bd391b29`) on Sep 15–16 and left in
`/private/tmp/claude-501/.../bd391b29-.../scratchpad/`, which macOS clears.
The away-mode fallback found it on Sep 17 and committed it **verbatim** so it
would not be lost.

**Nothing here was written, completed or edited by the fallback.** Two files
are unfinished and one is mislabeled in a way that matters — read the warnings
below before using any of it.

---

## `AMENDMENT4_DRAFT.md` — UNFINISHED DRAFT, NOT REGISTERED

A well-developed three-part draft (freeze-by-the-letter disclosure, the rule 7
wording defect, and re-verification notes). **It still contains two unfilled
placeholders** and was never committed, registered or dated:

1. `RUNTIME-READ CAVEAT: <fill from verifier — which files the run-6 process
   reads from disk after start …>`
2. `Contract v3 rule 8 … must be corrected BEFORE run-7 day 1 (Mon Sep 21):
   "<fill: exact v3 rule-8 wording from the synthesizer>"`
3. The header carries a literal `~HH:MM CT` registration time.

**On placeholder 1** — the primary answered this itself, in its last message
before it died, though it never transferred the answer into the draft:

> "All facts for Amendment 4 are pinned: the running process re-reads no repo
> file after start, and after 15 sessions every cycle-path module is already
> resident, so the pull ban is the only residual control."

That is recorded here as recovery metadata only. Completing a contract
amendment is outside fallback authority, so the draft is untouched.

**On placeholder 2** — it awaited "the synthesizer", which never ran. See below.

## `AUDIT_WORKFLOW_RESULT.json` — the verify phase never ran

The 68-agent audit workflow the primary was waiting on **completed only in
name**. Of 58 log lines, **57 are `You've hit your session limit`**:

| Phase | Failures on the usage cap |
|---|---|
| `verify:*` | **53** |
| `audit:*` | 2 |
| `critic` | 1 |
| `synthesize` | 1 |

So `synth` is `null`, `critic_gaps` is `[]`, and — this is the trap — **`refuted`
is `[]` not because nothing was refuted but because nothing was ever checked.**

> **The two findings in the `confirmed` array carry `verdict: "confirmed"` from
> the review agents that raised them, NOT from adversarial verification. Every
> verifier died. Treat them as unverified claims.**

- **STR-1** — run-6 expectancy is a 2-name / 3-trip artefact; fresh-entry funnel
  0-for-11 since Sep 8; entry score is *negatively* rank-correlated with
  realized P&L (Spearman ρ −0.264 composite, −0.293 conviction); the A+
  strategy bar fails 4 of 5 substantive sub-bars; the run-7 change-set contains
  no entry-selection item.
- **STR-2** — the top-up evidence gate has a 7-day memory hole (`state.py`
  prunes `last_buy_convictions` past 7 days and the gate fails open on a
  missing prior), so two run-6 top-ups totaling $65,573 (SMCI Sep 10, SPCX
  Sep 15) passed a bar they would otherwise have failed; run-7 S-6 preserves
  the hole by design.

### One independent check the fallback did run

STR-1's core arithmetic **replicates** on the ledger through the Sep 17 close
(STR-1's own numbers were struck at Sep 15). This is arithmetic on
`state/trades.jsonl`, not verification of the wider claim:

| | Sep 17 ledger | STR-1 @ Sep 15 |
|---|---|---|
| Top 3 trips | **+$26,637.68 across 2 names** (BE ×2, INTC) | 2-name / 3-trip |
| Other 27 trips | **−$11,642.14**, mean −$431.19 | — |
| Satellite-only (system symbols excluded) | N=24, WR **29.2%**, ex-top-3 mean **−$859.86** | N=23, WR 30.4%, −$899.73 |

The window's headline (+3.85% vs SPY −0.58%) is real, but closed-trip
expectancy is carried by two names. The operator should weigh that before
Friday's verdict. STR-2 was **not** independently checked.

## `CHECKER_INTERIM_2026-09-15_*.txt`, `audit/`

The primary's Sep 15 interim checker runs and the audit evidence files
(`strategy_stats.py`, `verify_str1.py`, per-strand outputs, run-7 pytest runs,
a v3 dry-run on run-6 data). Superseded for the interim readout by
`../CHECKER_2026-09-17_interim.txt`, kept for the audit trail.

---

## What this means for Friday

The audit needs **re-running after the usage cap resets** if the operator wants
verified findings before the run-7 switch. Amendment 4 cannot be completed from
what survives: placeholder 2 depends on a synthesizer output that does not
exist.
