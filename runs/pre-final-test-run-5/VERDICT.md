# VERDICT — pre-final-test-run-5: **CONFOUNDED**

**Window**: Mon 2026-08-24 → (planned) Fri 2026-09-04, account `PA394CJ4TNL5`, contract `runs/pre-final-test-run-5/EVAL_CONTRACT.md` (pre-registered 2026-08-23).
**Called**: 2026-08-26, after the day-2 review (`REVIEW_2026-08-25.md`), ahead of the window's end. The run-6 change-set replaces this window from Mon 2026-08-31.

This is **not a FAIL**. The contract's own validity conditions were breached in ways that cannot be repaired inside the window, so the window is CONFOUNDED under the pre-registered decision rule ("fix the validity breach, restart the window; does not count as a fail").

## Validity breaches (each maps to a pre-registered condition)

1. **Measurement basis / telescoping** (condition: "day_pl telescopes with equity snapshots to < $1"). `equity_history` rows were re-stamped on every closed-market tick with after-hours marks: the Aug 24 row moved to $1,000,109.73 at 23:56Z against Alpaca's official close $1,000,456; the Aug 25 row moved from $1,006,145 to $1,007,879 (later $1,007,025.74 in the file). Day-2 telescoping gap: **$346** vs the $1 condition, and the checker had no telescoping check. The contract never named its basis. Fixed for run-6 by `EQUITY_CLOSE_FIXED_STAMP` (basis='close' row stamped once at the first closed tick ≥ 16:00 ET) and checker block 3b.
2. **Non-fresh state** (condition implied by "brand-new account… local state fresh-cycled"). `reset.py::_churn_carryover` carried run-4 exit clocks, exit prices and loss streaks into the fresh $1M account: SOFI/SMCI/NOK slate-excluded all of day 1 (SMCI was #2 composite), SPCX rejected on day 2 against a run-4 exit. Day 1 was not the clean day the contract assumed. Fixed for run-6 by `RESET_CARRY_CHURN=off`.
3. **Behavioural drift under a frozen config** (condition: "Freeze: zero commits… inside the window"). The commit freeze held, but the adaptive loops were not frozen: composite perf-weights swing ±50% on 3 trips, the expectancy gate re-arms from the ledger (~Sep 1–3), curated lessons written nightly by frozen code changed the prompt (the Aug-24 "cap QQQ" lesson — QQQ is the core the model cannot act on — entered the live prompt). The config was frozen; the behaviour was not. Frozen for run-6 by `COMPOSITE_PERF_WEIGHTS=off`, `EXPECTANCY_GATE_ENABLED=off`, `CURATED_LESSONS_INJECT=off`, `TRACK_RECORD_MIN_TRIPS=20`.
4. **Feeds** (condition: "all screener sources healthy every market day"). Literally met (`FEEDS: 5/5`), but the line counted `robinhood_scans` (a dead source) as healthy, EDGAR returned 0 filings twice on 20 s timeouts, and news was VADER-fallback for the whole process lifetime. Recorded as a caveat rather than a breach; the run-6 line names degraded modes (`FEEDS_DEGRADED_MODES=on`).

## The numbers that were recorded (days 1–2; from `REVIEW_2026-08-25.md` §5 and the ledgers)

| | |
|---|---|
| Cycles | 16/16 on schedule, no CRITICAL, no halt, one bot instance, deadman `ok` every 5 min |
| Cost | $0.10–$0.23 per decision, cache warm |
| Aug 24 (day 1) | equity $1,000,109.73 (file, 23:56Z; official close $1,000,456), day_pl +$109.73; 0 closes; Aug-24 postmortem lesson sums +$995 vs day_pl +$110 (options unmarked) |
| Aug 25 (day 2) | equity $1,007,025.74 (file) / $1,007,879 (review, +0.79%), day_pl +$6,569.90; 49.8% invested; 10 satellites + QQQ 15% core + IWM 295/280 Sep-30 put spread ×30 ($9,930) + NVDA call bought 1 day before earnings |
| Closed trips | 0 after 2 sessions (N < 24 was the base case for Sep 4; extension to Sep 11 would have been needed) |
| SPY | +0.02% over the same two sessions |

Nothing here is admissible as a pass or fail of the run-5 config. The window's value is the diagnosis it produced (`REVIEW_2026-08-25.md` §0–§4, all verified) and the run-6 change-set built from it.

## What carries forward

- The run-5 account `PA394CJ4TNL5` is flattened at 09:31 ET Aug 31 and reused for run-6 (fresh-cycled, clean state).
- Run-5 ledger rows are **not** pooled into the run-6 expectancy test (different config).
- The run-5 state is archived under `state/archive/<ts>/` by the reset; copy `trades.jsonl` / `equity_history.jsonl` / `risk_state.json` into `runs/pre-final-test-run-5/state/` at switch time for the record.
