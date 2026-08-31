# EVAL CONTRACT — pre-final-test-run-5

**Pre-registered 2026-08-23, BEFORE day 1. This file is the verdict. No post-hoc metrics are admitted.**

| | |
|---|---|
| Window | **Mon 2026-08-24 → Fri 2026-09-04** (10 trading days) |
| Account | Alpaca paper `PA394CJ4TNL5` — **brand-new account** (user-issued keys, pre-open Aug 24), options level 3, zero positions; local state fresh-cycled 00:16 ET Aug 24. Supersedes the flatten-at-open plan for old acct PA3IRR2BT9FT (amended pre-window, before day 1). |
| Starting equity | **$1,000,000.00 exactly** (clean account, no flatten noise) |
| Config | frozen at the run-5 ship commit on `feature/preview` (roadmap ranks 1–8); `.env`: KELLY_FRACTION=0.5, TARGET_ANNUAL_VOL_PCT=25.0, MAX_OPTION_POSITIONS=5, all new gates at config defaults |
| Verdict tool | `scripts/eval_contract_check.py --start 2026-08-24 --end 2026-09-04` (plain-python recompute of trades.jsonl + equity_history.jsonl; no bot imports) |

## Pass criteria (ALL must hold)

1. **Sample**: N ≥ 24 closed round trips. If N < 24 at Sep 4 close, the window **extends to Sep 11** — it is not judged early and not judged passing.
2. **Expectancy**: mean realized_pl per closed trade > 0 at **95% one-sided** (t-test, df = N−1), computed on the ledger only.
3. **Drawdown**: max drawdown of daily closing equity within the window stays **above −5%** of starting equity.
4. **Capture** (evaluated ONLY if the window contains ≥6 SPY-up days AND ≥6 SPY-down days; otherwise recorded but not judged): up_capture ÷ down_capture > 1.0.

## Validity conditions (violation ⇒ window is CONFOUNDED, not failed — diagnose, fix, re-run)

- Feeds: the per-cycle `FEEDS:` line reports all screener sources healthy every market day, **or** the window was pre-declared RH-less before day 1 (it was NOT — Robinhood re-auth is a day-1 user action).
- Freeze: zero commits touching `investment_strategy/` strategy/risk/prompt code or `.env` knobs inside the window (`git log --since=2026-08-24 --until=2026-09-05` is the proof). Ops/report-only fixes for actual breakage are allowed and must be logged in Todo-4.txt.
- Measurement: zero SELL rows with null realized_pl or symbol "None"; |ledger − broker realized| < $5 at window close; day_pl telescopes with equity snapshots to < $1.
- Single instance: no dual-bot episodes (flock log check).

## Decision rule

- **PASS** → run pre-final-test-run-6 (Sep 8–19 or Sep 14–25 if extended) with the SAME config. **Two consecutive passes ⇒ GO for the live pilot** ($500–1,000 real money, fractional, options OFF — see Todo-4.txt timeline).
- **FAIL** → one diagnostic review, ONE change-set (however many knobs, one commit train), then a fresh 2-week window. Never stack mid-window changes.
- **CONFOUNDED** → fix the validity breach, restart the window. Does not count as a fail.

## Watch items (recorded during the window, NOT graded — instrumentation for later decisions)

- Corroboration-gate trigger count and the haircut cohort's counterfactual P&L (log-greppable: `CORROBORATION GATE:`).
- Breadth-trigger arms (`auto_hedge=armed(breadth:...)`) and whether down-capture on SPY-down days improves vs the 2.31 baseline.
- Green-position decision-sells below the 1.5R trail-arm (winner-protection candidate, ships run-6 at the earliest).
- Option premium-stop fires: none may execute >5pp past width; none in the first 5 minutes unless the underlying moved adversely.
- Foregone top-ups on winners ≥+5% (pyramiding-release candidate).
