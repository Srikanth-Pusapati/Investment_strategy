# Trial-run archives

One folder per evaluation window (`pre-final-test-run-N`). Each holds the pre-registered contract
(`EVAL_CONTRACT.md`), the checker outputs, the reviews and any recovered material from fallback sessions.
`pre-final-test-run-8/CHANGESET_DRAFT.md` is the next window's change-set, revised at the run-7 close.

| Run | Window | Contract | Verdict |
|---|---|---|---|
| 4 | Jul 13 → Aug 2026 | v1 | reference sample; +$167/trade, PF 1.10 |
| 5 | Aug 24 → Aug 31 2026 | v1 | switched to run-6 mid-window (day-2 review) |
| 6 | Aug 31 → Sep 18 2026 | v2 | NO-GO on rule 7 (a wording defect; every counted rule passes sanction-aware); +3.22% vs SPY −0.70% |
| 7 | Sep 22 → Oct 20 2026 | v3 | in progress — interim PENDING (UNDER-FLOOR) at session 4 |
| 8 | after the run-7 close | v4 (draft) | — |

`SHA_MAP_2026-09-26_repo-move.md` maps the commit SHAs pinned in these contracts from the previous
repository to this one (the code tree ids are unchanged).

**De-duplication note (2026-09-30):** `pre-final-test-run-6/state/` was a full copy of the bot's `state/`
at the run-6 switch and therefore carried the `lessons/`, `decisions/` and `autotune/` files written during
run-4 and run-5 (Jul 7 → Aug 21). Those 71 files are byte-identical to the copies under
`pre-final-test-run-4/` and were removed from the run-6 folder; the run-6 folder keeps only files dated
inside or after its own window. `pre-final-test-run-4/state/` is git-ignored (`state/` pattern) and lives on
disk only.
