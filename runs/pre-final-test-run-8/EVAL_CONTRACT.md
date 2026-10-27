# EVAL CONTRACT v4 — pre-final-test-run-8

**Status: DRAFT — pre-register BEFORE day 1** (fill the Config and Account rows and both dates of
the pinned verdict command on switch day, then flip this line to "Pre-registered <date>, BEFORE
day 1"). Written Mon 2026-10-26 from the run-7 close (`../pre-final-test-run-7/RUN_SUMMARY.md`,
verdict **FAIL**, five of seven counted rules). The rules are **contract v3 unchanged** — same
checker (`scripts/eval_contract_check.py --contract v3`), same thresholds, same system set — so
run-8 is judged on the bar run-7 failed. What is new: the pre-declared degraded modes, the
recorded (not counted) metrics and handles of the run-8 change-set, and the restated kill
criterion. Change-set: `CHANGESET_DRAFT.md` (revised 2026-10-26). Switch runbook:
`docs/RUN8_SWITCH.md`.

| | |
|---|---|
| Window | **21 sessions** from day 1 (NYSE calendar). Day 0 = the last `basis in ('close','late')` row dated before day 1 (the switch-evening `late` row). Day 1 = the first `basis='close'` row. Extension: if satellite N < 24 at session 21, extend ONCE by ≤ 5 sessions and judge whatever N is then. |
| Account | `<fill at pre-registration>` (either `PA3BGBBR2NB5` flattened Oct 27, or a fresh paper account) — opening equity `<fill>`. |
| Config | frozen at the run-8 merge SHA `<fill>` (`investment_strategy/` tree id `<fill>`), with the STRATEGY KEY block of `docs/RUN8_SWITCH.md` section 2 (run-7's keys + `MAX_SYMBOL_EXPOSURE_PCT=12.0` + the twelve run-8 keys). **Fingerprint** = (sha256 of the tree; sorted STRATEGY KEYS with values; adaptive loops off: `COMPOSITE_PERF_WEIGHTS=off`, `EXPECTANCY_GATE_ENABLED=off`, `CURATED_LESSONS_INJECT=off`). Commits touching only `scripts/`, `ops/`, `docs/`, `runs/`, logs do not enter it. Freeze: no commit to `investment_strategy/` or `.env` on the live tree through the last close row. |
| Verdict tool | exactly one command, pinned here (dates for a Wed 2026-10-28 day 1 → session 21 = Wed 2026-11-25; re-write both if day 1 moves): `.venv/bin/python scripts/eval_contract_check.py --contract v3 --start 2026-10-28 --end 2026-11-25 --trades state/trades.jsonl --equity state/equity_history.jsonl --spy-csv spy.csv --bench-csv QQQ=qqq.csv --bench-csv IWM=iwm.csv --beta-target 1.0 --system-symbols PSQ,QQQ` — run ONLY after the last `basis='close'` row is stamped (VOID / exit 4 otherwise); CSVs exported per the checker's `--help` epilog. Save the output as `CHECKER_<end>_close.txt` here and the verdict to `docs/journal/JOURNAL.md` **the same evening** — run-7's verdict was not taken until six days after the close. |

## Rules (v3, unchanged — counted rules decide the verdict)

1. **Sample floor** (reported; drives the extension only): in-window **satellite** closed round trips N >= 24. Satellite = `action='sell'`, non-null `realized_pl`, `exit_reason` not in the system set **{hedge_unwind, core_defense, regime_trim, defensive_rotate, correction, beta_trim}**, and symbol not in `--system-symbols PSQ,QQQ`; duplicate/phantom rows dropped and listed.
2. **Window expectancy** (counted once N >= 24): mean satellite `realized_pl` > 0 at 95% one-sided (t, df = N-1). Printed, not counted: bootstrap-95 CI, PF, median, **mean ex-top-3, top-3 share**, equity-only vs options-only split. No grade above B may be claimed on a window whose ex-top-3 mean is <= 0.
3. **Pooled expectancy (live-pilot bar only)**: pooled same-fingerprint satellite N >= 60, mean > 0 at 95% one-sided. Not required for a window PASS. Run-8 starts a new pool (its fingerprint differs from run-7's).
4. **Daily alpha**: OLS of book return on SPY return over all pairs incl. day 1; counted at >= 20 pairs: alpha point estimate > 0.
5. **Beta** (counted at >= 5 pairs): mean stamped ex-ante `book_beta_spy` within +/-0.2 of 1.0; AND at >= 20 pairs the realized OLS beta's 90% CI overlaps [0.8, 1.2].
6. **Drawdown**: max drawdown of close-row equity > -5%.
7. **Capture** (counted at >= 6 SPY up-days AND >= 6 SPY down-days): up-capture > down-capture AND down-capture < 1.0.
8. **Decision-sell validity**: zero **UNSANCTIONED** decision-sell losses below 0.5x the planned stop (a row with a non-empty `sell_events` list is sanctioned and not counted).

**Window verdict**: a counted rule that never reaches its qualification floor is N/A and does not block PASS; PASS if every counted rule that reached its floor passes and rule 2 is decided; PENDING (UNDER-FLOOR) if every counted rule passes but N < 24 after the extension; FAIL if any counted rule fails; CONFOUNDED on any validity breach not disclosed-and-accepted in an amendment registered BEFORE the last close row.

**Decision rule**: PASS -> run-9 same fingerprint (pooled); live-pilot GO = two consecutive same-fingerprint PASS windows AND rule 3 PASS, with no amended counted rule inside either. PENDING -> run-9 same fingerprint, pooled. FAIL -> one diagnostic + ONE change-set + fresh window. CONFOUNDED -> fix, restart; not a fail. Amendments before day 1 are free; mid-window only for measurement-tool defects, registered with interim readings and the counterfactual; a mid-window change to a counted rule's statistic or threshold marks the window AMENDED. A window closed early by operator decision is PENDING-TERMINATED and never pooled.

## Validity conditions and pre-declared degraded modes

- **Feeds, by class.** A session day is a breach when, in > 2 decision cycles, any configured screener source is `DEAD` or `UNHEALTHY` on the `FEEDS:` line **or** `earnings=none`. Pre-declared and non-confounding: `news=vader-fallback`, `earnings=yfinance-fallback`, `rh=dead` (Robinhood is not a `SCREENER_SOURCE`), and — new — **`insider=cached(<h>h)`** (C-2: an EDGAR failure served a snapshot <= `INSIDER_CACHE_MAX_AGE_H` old) **in at most 2 cycles per day**; a third cached cycle on one day is a breach of the same class as `insider UNHEALTHY`. Any breach is appended to the Amendment table below the same evening with the counterfactual (which decisions were insider-anchored).
- **Measurement.** Every close row carries `day_pl` and `broker_day_pl`; telescoping gap < $1 on every pair (the checker prints it). One bot instance, one growing log; every restart in the window gets a journal line.
- **Adaptive loops off** for the whole window (fingerprint).
- **Handles.** A watch-item handle that matches nothing on a day its trigger occurred is a measurement breach, not a zero. Every handle below is a string literal in `investment_strategy/` (`tests/test_contract_handles.py`).

**Watch items (handles verified by test)**: `SELL AUTHORITY:` · `BOOK BETA CAP:` · `BOOK BETA (post-exec):` · `AUTO-HEDGE: beta:` · `AUTO-HEDGE UNWIND:` · `HEDGE COUNTERFACTUAL:` · `PROXY PUT PICK:` · `STRIKE SNAP:` · `OPTIONS SINGLE-NAME BULLISH:` · `ELIGIBLE but IGNORED` · `-> IGNORED` · `ignored=` · `Top-up conviction` · `SLOT COUNT:` · `At max open positions` · `REGIME HOLD:` · `Market regime:` · `CORE DEFENSE:` · `Heartbeat withheld` · `Watchdog BLIND` · `not delivered` · `ROTATION:` · `CORE STOPLESS:` · `CORE FILL: BUY` · `CORE FILL BETA CLAMP:` · `AUTO-HEDGE STARVED:` · `REGIME FALLING-TAPE CAP:` · `Options opened` · `TOPUP AUTHORITY:` · `BUCKET CAP:` · `ENTRY DEFERRED (open window):` · `ENTRY DEFERRED RESULT:` · `BEARISH FALLBACK:` · `OPTION ENTRY SLIP:` · `PUT NO-QUOTE:` · `Bearish-slate excluded this cycle:` · `(option premium cap)` · `no NBBO on feed for` · `OPTION LOT CAP:` · `OPTION MARK FALLBACK:` · `OPTION EXIT QUEUED` · `INSIDER CACHED:` · `already on disk`.

## Recorded metrics (printed/queried, never counted) — one per change-set item, with its pre-registered removal result

| item | metric (source) | remove the item at the run-8 close if |
|---|---|---|
| B-1 top-up authority | top-ups/session, top-up $ share of satellite $, `topup_events` histogram, top-up-lot P&L vs starter-lot P&L (ledger `topup_n`, `topup_events`, `prior_*`) | top-up lots' mean P&L < run-7's top-up lots' mean (−$2,724/lot over 2) at n ≥ 10 |
| B-2 concentration | largest satellite weight at each close; top-3 share; `BUCKET CAP:` count | — (a cap; judged with B-1) |
| B-3 open-window delay | deferred cohort's 09:33 → 09:45 move (`ENTRY DEFERRED RESULT:`); cohort trip P&L vs same-day later entries (ledger `entry_minute`) | mean 09:33 → 09:45 move ≥ +0.3% over n ≥ 20 deferrals (waiting cost more than it saved) |
| B-4 IGNORED fallback | `BEARISH FALLBACK:` proposals / vetoes / fills and their P&L vs model-proposed puts; `ignored=` rate | fallback fills' mean P&L < model-proposed puts' mean at n ≥ 8, or IGNORED falls to 0 by itself |
| B-5 entry buffer tier | `OPTION ENTRY SLIP:` mean/max by tier; unfilled-and-dropped count | tier-1 unfilled rate > 25% at n ≥ 8 |
| B-6 no-quote cooldown | repeat proposals of the same dead contract per session (run-7 baseline 10/day) | — |
| B-7 marks & lots | `premium mark unreliable` ticks/session (run-7 baseline 977–3,055); `OPTION MARK FALLBACK:` count; premium-stop evaluations skipped (target 0); `OPTION LOT CAP:` count | a fallback-marked exit fills > 15% worse than its mark at n ≥ 5 (the mark was not conservative enough) |
| B-8 premium-cap exclusion | `(option premium cap)` exclusions/session; DECLINED verdicts on excluded names (target 0) | — |
| N-1 RTH-only exits | `OPTION EXIT QUEUED` count; `Option close FAILED` outside RTH (target 0); fill price of a queued exit at the open vs the pre-market mark | — |
| C-2 cached insider | cached cycles/session (cap 2) | — |

**Kill criterion (restated from Sep 21):** pooled run-7 + run-8 satellite **ex-top-3 mean <= 0 ⇒ stock-picking is not the edge**; the run-9 change-set then shrinks the satellite sleeve and keeps the index core + systematic beta hedge + credit-ETF put sleeve. Run-7 contributes 27 trips at −$1,422.

## Amendments
(none — register before day 1 freely; mid-window only per the decision rule)

## Pre-registration checklist (switch evening)
- [ ] Config row: merge SHA + tree id; Account row; both dates in the pinned command (session 21 on the NYSE calendar; Nov 26 is a holiday, Nov 27 a half day).
- [ ] `scripts/run8_morning_check.py` `FROZEN_TREE` pinned to the same tree id (its test then asserts the pin appears here).
- [ ] Header flipped to "Pre-registered <date>, BEFORE day 1"; committed and pushed before day 1's open.
