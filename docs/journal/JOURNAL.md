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

<<<<<<< HEAD
## 2026-10-26 15:10 CT — run-7 verdict taken six days late: FAIL
Facts: the pinned v3 command (`--start 2026-09-22 --end 2026-10-20`, CSVs from yfinance) printed
**VERDICT: FAIL** — rule 2 expectancy (N=30, mean −$855, t −1.90 vs 1.70, PF 0.39, WR 20%, ex-top-3
−$1,422), rule 4 alpha (−0.103%/day), rule 5a/5b beta (ex-ante 0.73; OLS 0.27, CI [−0.13, 0.66]),
rule 7 capture (up −2.5% / down 45.1%); rules 6 (DD −3.20%) and 8 (0 unsanctioned) pass. Book
$1,000,000 → $976,827 (−2.32%) vs SPY −0.58%, QQQ −3.22%. Saved as
`runs/pre-final-test-run-7/CHECKER_2026-10-20_close.txt`; diagnostic in `RUN_SUMMARY.md`.
Cause: nobody ran the verdict on Oct 20; the bot kept trading the frozen fingerprint to Oct 26.
Backfill: two unjournaled restarts in the window — Oct 8 09:42 CT (battery sleep mid-session,
`On BATTERY during market hours` page) and Oct 9 07:38 CT (14-min gap, manual) — plus 32 battery
CRITICALs and 9 in-hours dark gaps over the run; Robinhood auth dead since Oct 13 15:16 CT.
Action: none on the window (it is closed); the operator asked for a stop and a rebuild.
Plan: closes TODO X1/X5; opens the run-8 switch items.

## 2026-10-26 15:31 CT — run-7 bot stopped; account flattens at the Oct 27 open
Facts: deadman and claude-daily launchd agents disabled + booted out (nothing may relaunch the
stopped bot); kill switch ENGAGED (`state/KILL`); SIGTERM to pid 13607 (zombie until the panel
reaps it; no bot process). `scripts/flatten_only.py` launched detached (pid 31829,
`logs/flatten_only.log`): waits for 09:31 ET Tue Oct 27, cancels every order, closes every
position short-legs-first (the Oct 26 pre-market showed Alpaca rejects option market orders
outside RTH, code 42210000, 113×), and does NOT relaunch. Positions at the stop: 7 equities
($635k), 6 option legs (HYG 78/79P, AAL 12P, ROK 440P, IWM 277/263 spread), 12 open orders.
Action: operator decision ("stop the run, close old options spreads, fix the model for run-8").
Plan: TODO "Run-8 switch" N-items; re-arm the supervisors at the switch (`docs/RUN8_SWITCH.md` §0.5).

## 2026-10-26 22:30 CT — run-8 change-set built on feature/run8-changeset (worktree)
Facts: B-1..B-8, N-1 (new: option exits RTH-only), C-1..C-5, C-7, C-9 implemented with failing
tests first, knobs OFF in code / ON in `docs/RUN8_SWITCH.md` §1c, handles pinned in
`tests/test_contract_handles.py` against contract v4 (`runs/pre-final-test-run-8/EVAL_CONTRACT.md`,
rules = v3 unchanged, dates pinned for an Oct 28 day 1 → Nov 25 session 21). Ledger evidence
that shaped the set: open-window entries 0/12 (−$17,135), the 3-lot INTC −$7,474, no
composite/conviction/family bucket positive, 977–3,055 unmarkable option ticks/day Oct 8–20.
B-4 ships as a scoped model follow-up (put or HOLD; silence = no trade), not a code-built contract
with a veto — the draft's variant needed a second strike-picker. New scripts: `run8_switch.sh`
(same flat account accepted; live tree on `main`), `run8_morning_check.py` (FROZEN_TREE pinned at
pre-registration), `flatten_only.py`; C-1 `ops/in_session_check.sh` + plist. Suite: see the PR.
Action: PR `feature/run8-changeset` → `main`; the operator merges, then runs §0 of RUN8_SWITCH.md.
Plan: closes TODO B-1..B-8, C-1..C-7, C-9, N10–N12, X3, X4 (draft); opens the run-8 switch list.
=======
## 2026-09-30 18:05 CT — PR #1 merged; branches reduced to main + feature/preview
Facts: owner merged PR #1 (`feature/preview` → `main`, 8e1c901) on the new repo under their own account;
live tree pulled (frozen tree `2a6ec147` unchanged, bot pid 24288). The old repository no longer resolves
on GitHub; the empty duplicate clone under `~/PersonalProjects` (0 commits, 0 files) deleted.
`feature/run6-changeset`, `feature/run7-aplus`, `docs/run6-amendment4-freeze-disclosure` verified as
ancestors of `main` and deleted on the remote and locally; `feature/preview` fast-forwarded to `main`.
Cause: owner's instruction — keep `main` and `feature/preview` only; merge into preview, into main when done.
Action: branch policy written into README (two permanent branches; next-window code on a short-lived
`feature/runN-changeset` so preview stays mergeable); TODO N1 done, N6 dropped (nothing left to archive).
Plan: closes N1, N6; opens nothing.
>>>>>>> feature/preview
