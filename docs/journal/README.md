# Engineering journal

This folder is the project's **history**, not its plan. The plan is [`TODO.md`](../../TODO.md) at the
repository root: short, scannable, one line per item, always current. When you want to know *what to do
next*, read `TODO.md`. When you want to know *why something is the way it is*, come here.

| File | What it is | Period |
|---|---|---|
| `JOURNAL.md` | the continuing journal — dated entries, newest at the bottom | 2026-09-30 → |
| `Todo-4.txt` | volume 4 of the original combined todo + journal (run-5 through run-7, the repo move) | 2026-08-20 → 2026-09-29 |
| `Todo-3.txt` | volume 3 (alpha track: beat-the-index posture, discovery feeds, options, run-4) | 2026-07-01 → 2026-08 |
| `Todo-2.txt` | volume 2 (the safety stack: risk limits, watchdog, alerting, backtest gate) | 2026-06-25 → 2026-07 |
| `completed.txt` | items closed out of volumes 2-3, with dates | 2026-06 → 2026-07 |
| `goGA.txt` | the go-live / productisation roadmap (GA-0 … GA-6), **parked** since 2026-07-05 | — |

The three `Todo-*.txt` volumes mixed the plan with the narrative, which is why they grew past 1,600 lines
and stopped being readable as a plan. They are kept verbatim because the evaluation contracts, the run
summaries and the memory notes cite them by name and line.

## How to write a journal entry (from 2026-09-30)

Append to `JOURNAL.md`. One entry per event, at most ~15 lines:

```
## 2026-10-01 15:40 CT — <what happened, in one line>
Facts: <2-6 lines: what was observed, with the numbers and the file/log handle>
Cause: <one line, or "unknown">
Action: <what was changed or decided; PR/commit if any>
Plan: <which TODO.md item this opens, moves or closes — or "none">
```

Detail that does not fit belongs in a document (`runs/…`, `docs/…`) linked from the entry. The
`Plan:` line is mandatory: every entry either touches `TODO.md` or says it does not.
