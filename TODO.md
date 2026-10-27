# TODO — the plan

Updated 2026-10-26. One line per item; `[ ]` open, `[~]` in progress, `[x]` done (kept two weeks, then pruned).
History and reasons live in [`docs/journal/JOURNAL.md`](docs/journal/JOURNAL.md). **Run-7 closed FAIL on Oct 20**
([`runs/pre-final-test-run-7/RUN_SUMMARY.md`](runs/pre-final-test-run-7/RUN_SUMMARY.md)); the bot was stopped Oct 26 and the
account flattens at the Oct 27 open. The run-8 change-set is built ([`CHANGESET_DRAFT.md`](runs/pre-final-test-run-8/CHANGESET_DRAFT.md),
revision at the close) and switches in via [`docs/RUN8_SWITCH.md`](docs/RUN8_SWITCH.md). **Freeze** resumes at the switch: no change to
`investment_strategy/` or `.env` through the run-8 close (session 21 ≈ Wed Nov 25; Dec 3 if extended).

## Now — the run-8 switch, in order

- [ ] S1 **Merge PR #2** `feature/run8-changeset` → `feature/preview` on GitHub (operator); `feature/preview` → `main` is a second PR opened at the switch (S6), so `main` stays the run-7 tree until then
- [ ] S2 **Tue Oct 27 ~09:40 ET:** `tail -5 logs/flatten_only.log` must end `account is flat`; on `INCOMPLETE`, rerun `nohup .venv/bin/python scripts/flatten_only.py >> logs/flatten_only.log 2>&1 &` during RTH or close the leftover by hand (operator or session)
- [ ] S3 **Account:** keep `PA3BGBBR2NB5` flat (~$977k) or create a fresh $1M paper account and swap `ALPACA_API_KEY`/`ALPACA_SECRET_KEY` in `.env` (operator)
- [ ] S4 **`.env`:** paste `docs/RUN8_SWITCH.md` §1c; change the existing `MAX_SYMBOL_EXPOSURE_PCT=18.0` to `12.0`; the §1c grep must count 12 (operator)
- [ ] S5 **Robinhood:** `.venv/bin/python -m investment_strategy.portfolio.robinhood_auth login` — auth dead since Oct 13 (operator)
- [ ] S6 **After 16:00 ET:** `git fetch origin && git show origin/main:scripts/run8_switch.sh > /tmp/run8_switch.sh`, `DRY_RUN=1 sh /tmp/run8_switch.sh`, then `sh /tmp/run8_switch.sh` (operator; the live tree is on `main`)
- [ ] S7 **Re-arm supervisors + unkill:** §0.5 of RUN8_SWITCH.md (deadman, claude-daily, the new session-check plist; `curl -s -X POST http://127.0.0.1:8787/api/unkill`) (operator or session)
- [ ] S8 **Pre-register contract v4** the same evening: Config SHA + tree id, Account, both dates in the pinned command, `FROZEN_TREE` in `scripts/run8_morning_check.py`, header flip; PR (session)
- [ ] S9 **Day 1 morning:** `.venv/bin/python scripts/run8_morning_check.py` READY; watch §3 handles (`ENTRY DEFERRED` at 09:30, `RESULT` at 09:45, `OPTION MARK FALLBACK:` on any wide put, zero `Option close FAILED` outside RTH)
- [ ] N5 Check `ANTHROPIC_API_KEY` at `runs/pre-final-test-run-4/analysis/config.json:84` **in the old repo**; rotate if live (operator)
- [ ] N7 Personal-data review before any public flip (e-mail in dated logs, paper account IDs, `docs/ga/`) (operator)
- [ ] N8 **Power:** charger attached through the window; always-on host (`ops/Dockerfile`) is the real fix — run-7 logged 32 battery CRITICALs and 9 in-hours dark gaps (operator)
- [ ] N9 Off-machine backup target for `ops/backup_state.sh` (operator)

## Routines through the run-8 close

- Morning check `.venv/bin/python scripts/run8_morning_check.py` ~08:50 CT; the C-1 launchd job repeats it 09:55 / 15:35 ET and pages on FAIL; any FAIL/WARN → one journal line.
- `insider=cached(…)` in > 2 cycles of a day, or ANY other `UNHEALTHY`/`DEAD`/`earnings=none` class → append to the contract's Amendment table the same evening with the counterfactual.
- Every bot restart in the window → one journal line. Restart only via `curl -X POST http://127.0.0.1:8787/api/restart`.
- Robinhood token dies ~6 days after login; the check WARNs at < 24 h → re-login that day.
- **The verdict is run THE SAME EVENING session 21 closes** (run-7's was six days late).

## Run-8 close (Wed Nov 25 after the `close` row; Dec 3 if extended)

- [ ] X1 Run the pinned v4 verdict command; save `runs/pre-final-test-run-8/CHECKER_<end>_close.txt`; verdict → journal
- [ ] X2 If satellite N < 24, extend once by ≤ 5 sessions and judge whatever N is then
- [ ] X3 Walk the contract's "Recorded metrics" table: keep / remove each B-item per its pre-registered result
- [ ] X4 Kill criterion: pooled run-7 + run-8 satellite ex-top-3 mean (run-7 = 27 trips at −$1,422) — ≤ 0 ⇒ run-9 shrinks the satellite sleeve, keeps index core + beta hedge + credit-ETF puts
- [ ] X5 PASS → run-9 same fingerprint (pooled); FAIL → one diagnostic + one change-set; C-8 code hygiene rides along either way
- [ ] C-8 Code hygiene after the window: split `orchestrator.py` (6,300 lines), remove dead paths, behaviour byte-identical (tests green before/after)

## Later / research (after run-8 is pre-registered)

- [ ] L1 P1 hedge integrity: judge unwind on UNHEDGED beta; band ≥ the falling step (zones must not overlap)
- [ ] L2 P2 gap/cluster risk: reproduce the Sep 14 cluster numbers offline first, then a correlated-bucket cap and gap-history haircut behind a knob
- [ ] L3 P3 core execution: register the core with the watchdog's hard-exit path; one-step cancel-and-close for exits blocked by a resting order
- [ ] L4 P4 bearish sleeve: show spot + listed expiries on candidate lines (B-4/B-6 shipped the routing half)
- [ ] L5 P5 regime: breadth on the completed prior bar; a second SPY/VIX source so one vendor outage is not a blind regime
- [ ] L6 P6 measurement: one cost basis per symbol; scripted grading of the ops bars (stopless windows, cadence)
- [ ] L7 Entry-edge research with kill criteria (`scripts/signal_ic.py`): re-entry after exit (0/4), later-wave entries (9/9 lost), negative families, flat sizing while scores do not rank — run-7 adds: `uptrend`-cited entries 13 trips −$24,305 (8% WR)
- [ ] L8 IC-2 / IC-3 pre-registered tests at the run-8 close (technical weight → 0 for run-9; discovery re-entry) — apply to run-9 only
- [ ] L9 Old later-track, each built with a metric or dropped: winner-protection veto; cadence 3120 → 1560 s after a cache audit; RVOL ≥ 1.5–2× pre-filter; `NAME_DROP_DEFENSE_PCT` 4.0 → 2.5–3.0; prior-winner re-scan source; stop/target calibration from the realized-loss distribution; portfolio vol targeting; bear-market backtest; funnel counters on the dashboard
- [ ] L10 Re-run `subscriptions.py` when any paid source shows edge (last verdict: pay for nothing)
- [ ] L11 B-7c follow-up: soften the `_mark_degraded` WARNING that still prints before `INSIDER CACHED:`; re-point `run8_switch.sh` step 10 to a run-8-specific config-load line

## Parked

- Live-money pilot: needs two consecutive same-fingerprint PASS windows + pooled N ≥ 60 → **January 2027 at the earliest** (run-8 Oct 28–Nov 25, run-9 Dec). Shape when it comes: $500–1,000, fractional, options off, `MAX_DAILY_LOSS_PCT=2`, tighter single-name cap, kill switch drilled, paper account as the control arm; note run-7's API spend ($40/window) vs a small float.
- GA / productisation programme (`docs/journal/goGA.txt`, `docs/ga/`): parked ~1 year from 2026-07-05.
- Leverage (X.6): gated on proof of edge.
- Data-plan-blocked feeds (Quiver Hobbyist 403s; Finnhub news 403 → VADER): unblock only with a tier upgrade justified by `subscriptions.py`.

## Decide (leftovers with two readings — pick one, then move or delete the line)

- Run-6 Amendment 4 was drafted, never registered: register it for the record, or leave run-6 as the reference sample it already is
- Sep 16 audit findings are unverified `[A]`: re-run the verify phase once, or keep treating them as hypotheses
- Old paper account PA3B09IK4MGS still holds positions with resting stops: flatten it, or leave it abandoned
- Phone status artifact (stale since Sep 7): republish from a resident session, or retire it in favour of GitHub + the morning check
- Post-mortem is blind to option P&L / day marks: fix as a measurement item, or leave since post-mortems are not inputs while `CURATED_LESSONS_INJECT=off`
- A separate options-income project (IV rank, greeks, assignment, its own eval): start it, or drop the idea

## Done (last two weeks)

- [x] 2026-10-26 Run-7 verdict FAIL (five of seven counted rules) taken and filed; stop + flatten armed; run-8 change-set built with tests (B-1..B-8, N-1, C-1..C-5, C-7, C-9), contract v4 drafted, RUN8_SWITCH.md, `run8_switch.sh`, `run8_morning_check.py`, `flatten_only.py`, C-1 in-session check; N10 wording fixed; Oct 1–25 logs committed
- [x] 2026-10-26 X1/X3/X5 of the run-7 close list (verdict, change-set revision, kill-criterion input); X2 n/a (N = 30); X4 drafted (pre-registration at the switch)
- [x] 2026-09-30 Repo cleanup; `fix_branches.sh` run by the operator — remote == mirror; N1 merged, N4 (`gh auth` as Srikanth-Pusapati) done
- [x] 2026-09-29 Live tree re-pointed to `Srikanth-Pusapati/Investment_strategy`; bot restarted (pid 24288)
- [x] 2026-09-26 Session-4 backtrack; Amendment 1 accepted by merge; run-8 change-set draft; Robinhood login restored
- [x] 2026-09-21 Run-7 live on account PA3BGBBR2NB5 at $1,000,000; contract v3 pre-registered
