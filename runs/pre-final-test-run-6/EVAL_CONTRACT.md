# EVAL CONTRACT — pre-final-test-run-6 — **DRAFT**

**Status: DRAFT (written 2026-08-26 with the run-6 change-set). The operator pre-registers it — replaces this header with "Pre-registered <date>, BEFORE day 1" and commits — before the 09:31 ET Aug 31 flatten. No post-hoc metrics are admitted after that commit.**

| | |
|---|---|
| Window | **Mon 2026-08-31 → Fri 2026-09-11** (Labor Day Mon Sep 7 closed; **10 sessions**: Aug 31, Sep 1–4, Sep 8–11). Sample-floor extension (below) may run it to Fri Sep 18. |
| Account | Alpaca paper `PA394CJ4TNL5` (the run-5 account). **Flattened at 09:31 ET Aug 31** by `scripts/flatten_and_restart.py` (cancels + closes everything, then `reset --yes` + preflight + relaunch), then fresh-cycled with **`RESET_CARRY_CHURN=off`** — empty exit/buy clocks, exit prices and loss streaks (clean window = clean state; run-5 day 1 was dirtied by run-4 carry). |
| Starting equity | the account's equity at the 09:31 flatten (record it in `Todo-4.txt` as day-0 equity; the first `basis='close'` row of Aug 31 is the curve's first point) |
| Config | **frozen at the run-6 change-set commit** (branch `feature/run6-changeset` merged into `feature/preview`; record the SHA here at pre-registration) + the `.env` keys in `docs/RUN6_SWITCH.md`. All 8 items ship together. |
| Verdict tool | `scripts/eval_contract_check.py --contract v2 --start 2026-08-31 --end 2026-09-11 --spy-csv spy.csv --bench-csv QQQ=qqq.csv --bench-csv IWM=iwm.csv --beta-target 1.0 --beta-json state/risk_state.json` (offline stdlib recompute of `trades.jsonl` + `equity_history.jsonl`; exit 0 GO / 2 NO-GO / 3 PENDING). |
| Equity basis | **`basis='close'` rows only** (fixed post-bell stamp, `EQUITY_CLOSE_FIXED_STAMP=on`). The checker prints the basis line; a window whose verdict rows are `legacy`/`intraday` is measured on the wrong basis → CONFOUNDED. |

## Pass rules — contract v2 (`--contract v2`)

1. **Sample floor (reported, never pass/fail)**: N ≥ 24 closed round trips in the window. If N < 24 at Sep 11 close the window **extends to Sep 18**; it is not judged early.
2. **Expectancy — pooled**: mean realized_pl per closed trade > 0 at **95% one-sided** (t, df = N−1), judged on the **pool of same-config windows** (`--pool <prior trades.jsonl>`), and **only once pooled N ≥ 60**. Below 60 the checker prints per-window N / mean / t / bootstrap-95 CI and the verdict is **PENDING** (exit 3) — the next same-config window is pooled in. Run-5 is NOT same-config and is never pooled. Run-6 alone (≈10 sessions) is expected to end PENDING; that is the designed outcome, not a failure.
3. **Daily alpha (reported)**: OLS of daily book return on SPY daily return over the window — alpha/day, its t, realized beta. The t is printed but flagged "not interpretable" under **20 sessions**; it becomes a counted rule only when pooled windows reach 20 sessions (future contract revision, pre-registered then).
4. **Realized beta**: the OLS beta must lie within **±0.2 of the target 1.0** (`HEDGE_BETA_TARGET`), judged when the window has ≥ 5 sessions.
5. **Drawdown**: max drawdown of daily closing equity within the window stays **above −5%**.
6. **Capture**: judged when the window holds **≥ 4 SPY up-days AND ≥ 4 SPY down-days** (was 6/6): up-capture > down-capture vs SPY. Capture vs QQQ and IWM, and the beta-adjusted capture (book ÷ (β_ex-ante × index), β from the `book_beta_spy` stamped on each close row), are printed and recorded, not judged.
7. **Zero decision-sell losses below 0.5× the planned stop** (`(4e)` block: decision-sell rows with realized_pl_pct < 0 joined to their buy's `stop_loss_pct`). Item 2 (`LLM_SELL_AUTHORITY=events_only`) makes these impossible; a count > 0 means the gate that ran is not the one that was frozen → validity breach.

**Verdict**: GO only if every counted rule (2 when decided, 4, 5, 6 when qualified, 7) passes; PENDING if all counted rules pass but rule 2 is undecided; NO-GO otherwise.

## Validity conditions (violation ⇒ CONFOUNDED, not FAIL — fix, restart)

- **Feeds**: the per-cycle `FEEDS:` line reads `n/n healthy` every market day **with degraded modes named** (`FEEDS_DEGRADED_MODES=on`): an `insider UNHEALTHY (edgar: …)` or `DEAD` cycle is logged in `Todo-4.txt` with its time; > 2 such cycles on any day, or `news=vader-fallback` for the whole window without being pre-declared here, is a breach. Pre-declaration: **news = VADER fallback is accepted for run-6** (Finnhub sentiment has been 403 since Jul 13; the line must still say so).
- **Freeze**: `git log --since=2026-08-31 --until=2026-09-12 -- investment_strategy/ .env` shows zero strategy/risk/prompt/knob commits. Ops/report-only fixes for actual breakage are allowed and logged in `Todo-4.txt`. Adaptive loops are frozen by config (`COMPOSITE_PERF_WEIGHTS=off`, `EXPECTANCY_GATE_ENABLED=off`, `CURATED_LESSONS_INJECT=off`, `TRACK_RECORD_MIN_TRIPS=20`) — the "frozen config, drifting behaviour" confound of run-5 cannot recur.
- **Measurement**: the verdict rows are `basis='close'`; telescoping `|Δequity − day_pl| < $1` every day on that basis (checker block 3b); zero SELL rows with null `realized_pl` or symbol `None`; |ledger − broker realized| < $5 at window close; every FILLED line carries a fill price (`LEDGER_FILL_PRICES=on`).
- **Fresh state**: `state/archive/<ts>/` holds the run-5 state; the reset note reads `churn carry: OFF (clean state …)`; `risk_state.json` at 09:35 ET Aug 31 has no `exit_times`/`exit_prices`/`loss_streaks`.
- **Single instance**: one `bot.lock` holder; one growing log file; no dual-bot episode.

## Decision rule

- **PASS** → run-7 with the **same config** (pooled into rule 2). Two consecutive same-config passes with pooled N ≥ 60 ⇒ GO for the live pilot ($500–1,000, fractional, options OFF).
- **PENDING** → run-7 with the same config, same contract; the pooled test decides at the end of run-7 (or run-8 if N is still < 60).
- **FAIL** → one diagnostic review + ONE change-set (one commit train), then a fresh window. Never stack mid-window changes.
- **CONFOUNDED** → fix the validity breach, restart the window. Does not count as a fail.

## Watch items (recorded, NOT graded — log-greppable)

- `SELL AUTHORITY:` rejections — count, and each one's counterfactual (what the mechanical stack did with the position afterwards: stop / trail / still open at window end).
- `BOOK BETA CAP:` resizes and rejections, with the counterfactual post-trade beta printed in the line; the per-cycle `BOOK BETA:` line's range over the window.
- `AUTO-HEDGE: beta:` arms / unwinds / holds — how many cycles the hedge was on, the PSQ notional, and whether realized beta (rule 4) landed inside the band.
- Single-name option rejections: `single-name bullish option debits disabled` and `Earnings in Nd` on the option path — counts and the counterfactual debit each line prints.
- `PROXY PUT THESIS:` clamps — how many proxy puts were sized to `PROXY_PUT_UNTRANSFERRED_PCT` and whether any put was placed at all (bearish sleeve 0/83 last window).
- Screener liquidity floor: names dropped pre-cap per cycle; whether the slate ever fell below 10 names.
- `Expectancy gate (off, report-only): would have armed against:` — which families it would have gated (evidence for the item-6 freeze).
