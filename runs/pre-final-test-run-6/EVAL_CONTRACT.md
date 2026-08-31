# EVAL CONTRACT — pre-final-test-run-6

**Status: Pre-registered 2026-08-31, BEFORE day 1. Committed before the Aug 31 flatten; no post-hoc metrics are admitted after this commit. Deviation from the drafted plan, recorded at pre-registration: the operator's GO arrived Monday morning, so the flatten runs mid-session ~12:00 ET Aug 31 instead of 09:31 ET (same session; day-0 equity = equity at the actual flatten; Aug 31's `basis='close'` row is still the curve's first point).**

| | |
|---|---|
| Window | **Mon 2026-08-31 → Fri 2026-09-11** (Labor Day Mon Sep 7 closed; **10 sessions**: Aug 31, Sep 1–4, Sep 8–11). Sample-floor extension (below) may run it to Fri Sep 18. |
| Account | Alpaca paper `PA394CJ4TNL5` (the run-5 account). **Flattened at 09:31 ET Aug 31** by `scripts/flatten_and_restart.py` (cancels + closes everything, then `reset --yes` + preflight + relaunch), then fresh-cycled with **`RESET_CARRY_CHURN=off`** — empty exit/buy clocks, exit prices and loss streaks (clean window = clean state; run-5 day 1 was dirtied by run-4 carry). |
| Starting equity | the account's equity at the 09:31 flatten (record it in `Todo-4.txt` as day-0 equity; the first `basis='close'` row of Aug 31 is the curve's first point) |
| Config | **frozen at the run-6 change-set commit** — merge `580915de5a1c1e14837d27d66c389f3d70d0b61d` (`feature/run6-changeset` → `feature/preview`, merged 2026-08-31, 1221 tests passed) + the `.env` keys in `docs/RUN6_SWITCH.md`. All 8 items ship together. |
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

## Day-0 erratum — fresh-state condition vs the exchange-exit backfill (recorded 2026-08-31 ~12:20 ET, day 0)

The Fresh-state validity line reads "risk_state.json … has no `exit_times`/`exit_prices`/`loss_streaks`". The reset DID write clean state (the reset log printed `churn carry: OFF (clean state …)`; `loss_streaks`/`entry_times`/`stop_widths` are empty) — but the new bot's FIRST reconcile re-stamped `exit_times`/`exit_prices` for run-5's own broker sell fills via the exchange-exit backfill (`orchestrator._backfill_exchange_exits`, the Aug-23 "still stamp the exit cooldown (the exit DID happen)" rule — code that PREDATES the change-set). The backfill scans the broker's last 500 closed sell orders with no time filter and its skip-memory is per-process, so this re-stamp is deterministic on every restart, at the ORIGINAL fill times; it would have happened identically in the planned 09:31 flow, and clearing the file is futile. A real fix requires patching frozen code — barred by the Freeze condition.

**Resolution (recorded before the window's first `basis='close'` row):** the condition is read as written-by-RESET churn carry — the run-4→run-5 failure mode it names — which is satisfied. The backfill-derived stamps are accepted as frozen-code behavior with bounded, logged effect: a 24h LLM re-entry cooldown on the run-5 names (cited per cycle in `Buy-excluded`, e.g. "NOK (exited 0.3h ago (cooldown 24h))"), the price-aware re-entry guard for ≤7 days (blocks only re-buys ABOVE the exit price; composite ≥ 0.5 overrides), and empty `loss_streaks`. The QQQ core fill and the beta hedge do not consult these clocks (verified live day 0: core bought $49,462 QQQ in cycle 1). No code change; freeze holds.

## Amendment 1 — fresh account, day 1 moves to Sep 1 (pre-window, recorded 2026-08-31 ~17:30 ET, before any window trading)

Operator decision on day 0 evening: restart the window on a **fresh paper account** with a **full-session day 1** instead of the mid-session Aug 31 start. This supersedes the Account / Starting-equity / Window rows above:

- **Account: `PA3B09IK4MGS`** — created fresh 2026-08-31 evening, **$1,000,000**, options level 3, zero order history. The run-5 account `PA394CJ4TNL5` is abandoned (nothing carried).
- **Window: Tue 2026-09-01 → Fri 2026-09-11** (Labor Day Mon Sep 7 closed; **9 sessions**: Sep 1–4, Sep 8–11). The N<24 sample-floor extension to Fri Sep 18 is unchanged (and covers the lost session).
- **Starting equity: $1,000,000** (the account's opening balance; the one `basis='late'` row of Aug 31 records it — the curve's first point is Sep 1's `basis='close'` row).
- **The Aug 31 half-day of run-6 trading on the old account is DISCARDED** (+$1,943 vs its flatten baseline; one satellite entry). Its state is archived at `state/archive/20260831T221813Z/`; run-5's state remains at `runs/pre-final-test-run-5/state/`.
- **The Day-0 erratum above is VOIDED by this amendment**: the exchange-exit backfill scans the account's own closed orders, and a fresh account has none — `exit_times`/`exit_prices`/`loss_streaks` are genuinely empty at window start. The Fresh-state validity condition now holds as literally written (re-based to Sep 1).
- Config unchanged: same merge `580915d`, same `.env` profile (only the Alpaca keys swapped — a credential, not a knob). Freeze unchanged, now running to Sep 12.
- Noted for the Feeds condition: the **Robinhood OAuth refresh token died 2026-08-31 11:52 CT** (bot latched `auth_dead`, alert emailed). Robinhood is NOT one of the 3 contract feed sources (run-6 dropped it from `SCREENER_SOURCES`), so this does not touch the `FEEDS: 3/3` condition; re-login is an ops nicety, not a window dependency.
