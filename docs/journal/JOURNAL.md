# Journal

Dated entries, newest at the bottom. Format and rules: [`README.md`](README.md). The plan is
[`TODO.md`](../../TODO.md); every entry ends with a `Plan:` line naming the item it touches.

## 2026-09-30 16:38 CT — remote branches repaired; repository move complete
Facts: operator ran `repo-move/fix_branches.sh`: feature/preview on the personal repo replaced
(f31c6b2 → 572beef, lease on the old SHA), stray `backup/pre-run7-switch-20260921-1614` and
`feature/run7-changeset` deleted, local pointers repaired, remote heads == mirror (5 branches, the diff
lines differed only by tab vs space), trailer/identity hits across all remote commits = 0, frozen tree
`2a6ec147` unchanged. Bot pid 24288 (started 2026-09-29 22:00 CT from the re-pointed checkout), one
instance. Staging mirror and retired move tooling deleted; `switch_live_tree.sh`, `fix_branches.sh` and
`sha_map.txt` kept for the record.
Cause: the first `push --all` had carried three old-history branches (stale worktree pin; see the
2026-09-29 22:00 entry in `Todo-4.txt`).
Action: none further; the previous owner's repository is untouched (archive/delete is theirs).
Plan: closes "repo move" in TODO.md; opens nothing.

## 2026-09-30 17:10 CT — repository cleanup; plan split from journal
Facts: root held five journal/todo text files (Todo-2/3/4, completed, goGA; 1,650+ lines in Todo-4
alone), a 1 MB diagram, a 5 MB tracked knowledge-graph cache (`graphify-out/`), an obsolete run-6
runbook in `docs/`, and 72 exact-duplicate tracked files (71 run-4 lesson/decision/autotune files
carried into `runs/pre-final-test-run-6/state/`, plus one checker alias). README ended in a stray
pasted shell fragment.
Action: `TODO.md` created at the root as the plan (one line per item; Now / Routines / Run-8 / Later /
Parked / Done). Journals moved to `docs/journal/` unchanged, with a README and this file as the
continuing journal. `MODEL_SEQUENCE.md` + diagram → `docs/`; GA/tax notes → `docs/ga/`;
`RUN6_SWITCH.md` → `runs/pre-final-test-run-6/`; `graphify-out/` untracked and git-ignored; the 72
duplicates removed (hash-verified); `runs/README.md` added; README rewritten past the safety section
(where-things-are table + manual operations). Live references redirected (`docs/RUN7_SWITCH.md`,
run-8 change-set). Not touched: `investment_strategy/`, `.env`, `tests/` (run-7 freeze to Oct 20) —
code-level cleanup is a run-8 item.
Plan: opens TODO.md C-8 "code hygiene after the freeze"; closes "repo cleanup".
