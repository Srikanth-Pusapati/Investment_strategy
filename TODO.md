# TODO — the plan

Updated 2026-09-30 (evening). One line per item; `[ ]` open, `[~]` in progress, `[x]` done (kept two weeks, then pruned).
History and reasons live in [`docs/journal/JOURNAL.md`](docs/journal/JOURNAL.md). **Freeze:** no change to
`investment_strategy/` or `.env` until the run-7 close (Tue Oct 20; Oct 27 if extended). Detail for the run-8
items is in [`runs/pre-final-test-run-8/CHANGESET_DRAFT.md`](runs/pre-final-test-run-8/CHANGESET_DRAFT.md).

## Now (this week)

- [ ] N2 **Review sessions 5–6 (Sep 28–29)**: the battery night + dead-man relaunch, −$6,228 day, book 98% invested, the 09:26 CT 503 — done when a journal entry exists and any new run-8 item is on this list
- [ ] N3 Add `"includeCoAuthoredBy": false` to `~/.claude/settings.json` (operator; the session's edit was refused)
- [ ] N4 `gh auth login` for `Srikanth-Pusapati` with SSH so PRs open in your name; until then PRs go via the compare URL (operator)
- [ ] N5 Check `ANTHROPIC_API_KEY` at `runs/pre-final-test-run-4/analysis/config.json:84` **in the old repo**; rotate if live (operator; the file is gone from this repo's history)
- [ ] N7 Personal-data review before any public flip: your e-mail in the dated logs, paper account IDs, `docs/ga/` notes (operator)
- [ ] N8 **Power**: 94 W charger stays attached through Oct 20; the real fix is an always-on host (`ops/Dockerfile`) — 4 battery incidents so far in run-7 (operator)
- [ ] N9 Off-machine backup target for `ops/backup_state.sh` (`BACKUP_DIR` on iCloud/Dropbox, or an rclone remote) — done when a nightly tarball lands off this disk
- [ ] N10 `ops/away_mode.md` steps 77/86 still say the fallback pushes `feature/preview` while the live tree is on `main` — fix the wording (docs, allowed in the freeze)
- [ ] N11 **Ship C-1 now** (ops only, no fingerprint): launchd job runs `scripts/run7_morning_check.py` at 09:55 and 15:35 ET, appends to `logs/morning_check.log`, pages on FAIL — the Sep 25 feeds breach went unseen for a day without it
- [ ] N12 **Ship C-7 now** (scripts only): morning check / preflight WARN when the Robinhood token has < 24 h left (it prints the hours, never warns)

## Routines until the run-7 close

- Morning check `.venv/bin/python scripts/run7_morning_check.py` at ~08:50 CT and ~15:35 CT; any FAIL/WARN → one journal line.
- A day whose only degraded feed is `insider UNHEALTHY (edgar: timeout)` in > 2 cycles → append it to the Amendment 1 table in `runs/pre-final-test-run-7/EVAL_CONTRACT.md` the same evening. Any other degraded class confounds the window: raise it immediately.
- Every bot restart in the window → one journal line (freeze clause). Restart only via `curl -X POST http://127.0.0.1:8787/api/restart`.
- Robinhood token dies ~6 days after login; `NEEDS LOGIN` in the check → `robinhood_auth login` (non-confounding, not urgent).
- Do not trim DBRG (18% of equity, protected by four bracket legs); do not push `--all` from the live tree.

## Run-7 close (Tue Oct 20 after the `close` row; Oct 27 if extended)

- [ ] X1 Run the pinned verdict command from the contract's Verdict-tool row; save as `runs/pre-final-test-run-7/CHECKER_2026-10-20_close.txt`; verdict → journal
- [ ] X2 If satellite N < 24, extend once by ≤ 5 sessions (to Oct 27) and judge whatever N is then
- [ ] X3 Revise `CHANGESET_DRAFT.md` with the full window: answer section E (open-print cohort at n ≥ 20, top-up-lot P&L, credit-ETF put sleeve, IGNORED rate, EDGAR stalls)
- [ ] X4 Write `docs/RUN8_SWITCH.md` + contract v4 (pre-register BEFORE run-8 day 1); archive run-7 state under `runs/pre-final-test-run-7/state-post-window/`; fresh paper account; switch after the bell
- [ ] X5 Evaluate the kill criterion input so far: run-7 satellite ex-top-3 mean (contract rule 2 print-out) — the pooled run-7 + run-8 test happens at the run-8 close

## Run-8 change-set (build on `feature/run8-changeset` in a worktree; each item: failing test first, knob OFF in code / ON in RUN8_SWITCH, log handle, pre-registered removal metric)

- [ ] B-1 Top-up authority = events (`TOPUP_AUTHORITY=events_only`, `TOPUP_MAX_MULTIPLE_OF_HELD=1.0`) — done when the DBRG Sep 23–25 replay yields 3 rejects and a +3% winner add-on passes; handle `TOPUP AUTHORITY:`
- [ ] B-2 `MAX_SYMBOL_EXPOSURE_PCT` 18 → 12 and `MAX_TOP3_SATELLITE_PCT=30` — done when the DBRG/SMCI replays resize; handle `BUCKET CAP:`
- [ ] B-3 `ENTRY_OPEN_DELAY_MIN=15` for fresh buys only — handles `ENTRY DEFERRED`; remove if the 09:33→09:45 move averages ≥ +0.3% at n ≥ 20
- [ ] B-4 `BEARISH_IGNORED_FALLBACK=proxy_put` (deterministic proposal, model veto only, silence = no trade) — handle `BEARISH FALLBACK:`
- [ ] B-5 Option entry limit buffer tiered 5% / 20% by leg liquidity — done when the HYG 50-lot case prices a 0.83 limit, not 0.95; handle `OPTION ENTRY SLIP:`
- [ ] B-6 Per-name put no-quote cooldown (`PUT_NOQUOTE_COOLDOWN_CYCLES=8`) + honest "no NBBO on feed" reject text — handle `PUT NO-QUOTE:`
- [ ] B-7 Entry spread check = watchdog mark cap; OI ≥ 20 × contracts; delta-mark fallback instead of a blind premium stop — handle `OPTION MARK FALLBACK:`
- [ ] B-8 Premium-cap-bound underlying leaves the bearish slate for the day — shows as `HYG (option premium cap)` on the Buy-excluded line
- [ ] C-2 EDGAR retry + `insider=cached` degraded mode (≤ 24 h snapshot) — `FEEDS: 3/3 healthy insider=cached(4h)`
- [ ] C-3 yfinance "no fundamentals" for ETFs at DEBUG, not ERROR
- [ ] C-4 Ledger/journal fields: `prior_conviction`, `prior_composite`, `prior_fill_price`, `topup_n`, `topup_events`; raw `bearish_verdicts` + eligible list on every IGNORED
- [ ] C-5 `entry_minute` on every buy row
- [ ] C-6 Contract v4 text: `insider=cached` pre-declared with a cap, feeds breach by class, the new metrics/handles in `tests/test_contract_handles.py`, kill criterion restated
- [ ] C-8 Code hygiene after the freeze: split `orchestrator.py` (5,955 lines), remove dead paths, keep behaviour byte-identical (tests green before/after)
- [ ] C-9 A restart on a closed day must not re-run the post-mortem and overwrite `state/lessons/<date>.md` (noted Sep 21, not fixed)

## Later / research (after run-8 is pre-registered)

- [ ] L1 P1 hedge integrity: judge unwind on UNHEDGED beta; band ≥ the falling step (zones must not overlap)
- [ ] L2 P2 gap/cluster risk: reproduce the Sep 14 cluster numbers offline first, then a correlated-bucket cap and gap-history haircut behind a knob
- [ ] L3 P3 core execution: register the core with the watchdog's hard-exit path; one-step cancel-and-close for exits blocked by a resting order
- [ ] L4 P4 bearish sleeve: route "contract unknown / no debit" to the proxy path; show spot + listed expiries on candidate lines (overlaps B-4/B-6)
- [ ] L5 P5 regime: breadth on the completed prior bar; a second SPY/VIX source so one vendor outage is not a blind regime
- [ ] L6 P6 measurement: one cost basis per symbol; scripted grading of the ops bars (stopless windows, cadence)
- [ ] L7 Entry-edge research with kill criteria (`scripts/signal_ic.py`): re-entry after exit (0/4), later-wave entries (9/9 lost), negative families, flat sizing while scores do not rank
- [ ] L8 Kill criterion at the run-8 close: pooled run-7 + run-8 satellite ex-top-3 mean ≤ 0 ⇒ stock-picking is not the edge; keep index core + hedge + credit-ETF put sleeve
- [ ] L9 IC-2 / IC-3 pre-registered tests at the run-8 close (technical weight → 0 for run-9; discovery re-entry) — apply to run-9 only
- [ ] L10 Old later-track, each built with a metric or dropped: winner-protection veto; cadence 3120 → 1560 s after a cache audit; RVOL ≥ 1.5–2× pre-filter; `NAME_DROP_DEFENSE_PCT` 4.0 → 2.5–3.0; prior-winner re-scan source; stop/target calibration from the realized-loss distribution; portfolio vol targeting; bear-market backtest; funnel counters on the dashboard
- [ ] L11 Re-run `subscriptions.py` when any paid source shows edge (last verdict: pay for nothing); swap yfinance fundamentals only then

## Parked

- Live-money pilot: needs two consecutive same-fingerprint PASS windows + pooled N ≥ 60 → mid-December at the earliest. Shape when it comes: $500–1,000, fractional, options off, `MAX_DAILY_LOSS_PCT=2`, tighter single-name cap, kill switch drilled, paper account as the control arm. Pre-live: key rotation to Keychain (GA-2.6), live-vs-paper friction (GA-3.x), T+1 settlement awareness, re-run the live-float API cost math.
- GA / productisation programme (`docs/journal/goGA.txt`, `docs/ga/`): parked ~1 year from 2026-07-05.
- Leverage (X.6): gated on proof of edge.
- Data-plan-blocked feeds (Quiver Hobbyist 403s: WSB, senate/house, lobbying, flights, 13F; Finnhub news 403 → VADER): unblock only with a tier upgrade justified by `subscriptions.py`.

## Decide (leftovers with two readings — pick one, then move or delete the line)

- Run-6 Amendment 4 was drafted, never registered: register it for the record, or leave run-6 as the reference sample it already is
- Sep 16 audit findings are unverified `[A]`: re-run the verify phase once, or keep treating them as hypotheses (the roadmap already does)
- Old paper account PA3B09IK4MGS still holds positions with resting stops: flatten it, or leave it abandoned
- Phone status artifact (stale since Sep 7): republish from a resident session, or retire it in favour of GitHub + the morning check
- Post-mortem is blind to option P&L / day marks: fix as a measurement item, or leave since post-mortems are not inputs while `CURATED_LESSONS_INJECT=off`
- A separate options-income project (IV rank, greeks, assignment, its own eval): start it, or drop the idea

## Done (last two weeks)

- [x] 2026-09-30 PR #1 merged by the owner; live tree pulled; old repo already gone from GitHub; empty duplicate clone deleted; three fully-merged branches deleted — remote is `main` + `feature/preview` only (branch policy in README)
- [x] 2026-09-30 Repo cleanup: plan split from journal (`TODO.md`, `docs/journal/`), 72 duplicates removed, `graphify-out/` untracked, README rewritten past Safety; `fix_branches.sh` run by the operator — remote == mirror, 0 trailer/identity hits; staging mirror deleted
- [x] 2026-09-29 Live tree re-pointed to `Srikanth-Pusapati/Investment_strategy` in place; bot restarted (pid 24288); 5 branches pushed with the rewritten history (tree ids unchanged)
- [x] 2026-09-27 `docs/ARCHITECTURE.md`; LICENSE in the author's name; commit-SHA map for the move
- [x] 2026-09-26 Session-4 backtrack; Amendment 1 (Sep 25 feeds breach) accepted by merge; run-8 change-set draft; Robinhood login restored
- [x] 2026-09-23 "Shutdown" diagnosed as a 1% battery hibernate; Sep 21–22 logs read end to end
- [x] 2026-09-21 Run-7 live on account PA3BGBBR2NB5 at $1,000,000; contract v3 pre-registered; freeze to Oct 20
