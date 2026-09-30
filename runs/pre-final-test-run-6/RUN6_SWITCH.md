# Run-6 switch-time operator notes

The Monday checklist is at the bottom (**"Item 8 — the switch checklist for Mon
2026-08-31"**); the per-item sections above it are the evidence for each key.

## state/lessons/curated.md (runtime state, untracked)

At switch time strike the Aug-24 QQQ line in the LIVE tree's
`state/lessons/curated.md` by prefixing it, in place, with:

    [SUPERSEDED 2026-08-26: QQQ is the system core]

i.e. the line

    When a single symbol (e.g. QQQ) is topped up to >2x any equity starter, cap aggregate exposure before adding more.

becomes

    [SUPERSEDED 2026-08-26: QQQ is the system core] When a single symbol (e.g. QQQ) is topped up to >2x any equity starter, cap aggregate exposure before adding more.

`postmortem.read_curated()` skips `[SUPERSEDED ...]` lines, so the history
stays in the file but is never rendered. With the run-6 default
`CURATED_LESSONS_INJECT=off` nothing from this file reaches the prompt anyway;
the strike matters the day injection is turned back on.

## Item 6 env keys (run-6 defaults; check .env for overriding lines)

    COMPOSITE_PERF_WEIGHTS=off
    EXPECTANCY_GATE_ENABLED=off      # remove/flip any legacy EXPECTANCY_GATE=on line
    TRACK_RECORD_MIN_TRIPS=20
    CURATED_LESSONS_INJECT=off

## Item 7 env keys (book beta + beta cap + beta-sized hedge; run-6 defaults)

The live `.env` (line 234) pins `AUTO_HEDGE_MAX_PCT=15`, which would cap the
beta-sized hedge below its target — change it. The others are new keys
(defaults win unless a line exists); write them explicitly so the profile is
self-describing.

    BOOK_BETA_ENABLED=on             # one 'BOOK BETA:' line per cycle + risk_state.json book_beta
    MAX_BOOK_BETA_SPY=1.2            # buy-path cap; 0 = off
    AUTO_HEDGE_MODE=beta             # 'falling' = the Jul-30 behaviour
    HEDGE_BETA_TARGET=1.0
    HEDGE_BETA_BAND=0.15             # arm above target+band, unwind below target-band
    HEDGE_BETA_FALLING_TARGET=0.8    # target while the falling-tape read holds
    AUTO_HEDGE_MAX_PCT=40            # was 15 in the live .env
    HEDGE_ETF=PSQ                    # unchanged (already set)

## Item 8 env keys (eval contract v2 + clean-window reset)

    RESET_CARRY_CHURN=off            # run-6 default; a fresh cycle starts with EMPTY
                                     # exit/buy clocks, exit prices and loss streaks.
                                     # 'on' = the Jul-28 carry (NU re-buy rationale)

No other item-8 knob. The checker (`scripts/eval_contract_check.py`) is offline;
the ex-ante beta it uses for beta-adjusted capture is the `book_beta_spy` field
the run-6 close row carries (needs `BOOK_BETA_ENABLED=on` +
`EQUITY_CLOSE_FIXED_STAMP=on`, both run-6 defaults).

## Item 8 — the switch checklist for Mon 2026-08-31

Everything below runs in the LIVE tree `/Users/spusapati/Personal/Investment_stratergy`
once the change-set is merged. Do it in this order.

### 0. Merge the change-set (weekend, before Monday)

    cd /Users/spusapati/Personal/Investment_stratergy
    git fetch origin
    git checkout feature/preview
    git merge --no-ff feature/run6-changeset -m "Run-6 change-set: items 1-8"
    .venv/bin/python -m pytest -q -x --no-header -p no:cacheprovider     # 1203 passed
    git push origin feature/preview
    # open the PR feature/preview -> main as usual; record the merge SHA in
    # runs/pre-final-test-run-6/EVAL_CONTRACT.md ("Config" row) and flip its
    # header from DRAFT to "Pre-registered <date>, BEFORE day 1"; commit.

The bot keeps running the OLD code until it is restarted — the flatten script
(step 3) does the restart, so nothing changes strategy before 09:31 ET Monday.

### 1. `.env` — every key the change-set introduced or re-defaulted

Defaults win ONLY where no line exists. Two live lines already override a
run-6 default and MUST change (marked CHANGE); the rest are new keys — write
them explicitly so the profile is self-describing. Never commit `.env`.

    # item 1 — measurement plumbing
    POSTMORTEM_OPTION_MARKS=on
    EQUITY_CLOSE_FIXED_STAMP=on
    LEDGER_FILL_PRICES=on
    # item 2 — sell authority
    LLM_SELL_AUTHORITY=events_only   # 'full' = legacy
    # item 3 — options bypass
    OPTIONS_SINGLE_NAME_BULLISH=off
    PROXY_PUT_THESIS_GATE=on
    PROXY_PUT_UNTRANSFERRED_PCT=0.25
    # item 4 — taxonomy / history / composite
    SIGNAL_HISTORY_RETENTION_DAYS=120
    SIGNAL_HISTORY_MAX_POINTS=480
    COMPOSITE_INCLUDE_DISCOVERY=off
    # item 5 — screener hygiene + FEEDS line
    SCREENER_SOURCES=congress,insider,options_flow     # CHANGE (live line 199 lists robinhood,robinhood_scans)
    SCREENER_PRICE_FLOOR_PRE_CAP=on
    SCREENER_MIN_ADV_USD=0           # 0 = ADV floor off (price floor still applies)
    FEEDS_DEGRADED_MODES=on
    # item 6 — freeze the loops
    COMPOSITE_PERF_WEIGHTS=off
    EXPECTANCY_GATE_ENABLED=off      # remove/flip any legacy EXPECTANCY_GATE=on line
    TRACK_RECORD_MIN_TRIPS=20
    CURATED_LESSONS_INJECT=off
    # item 7 — exposure
    BOOK_BETA_ENABLED=on
    MAX_BOOK_BETA_SPY=1.2
    AUTO_HEDGE_MODE=beta
    HEDGE_BETA_TARGET=1.0
    HEDGE_BETA_BAND=0.15
    HEDGE_BETA_FALLING_TARGET=0.8
    AUTO_HEDGE_MAX_PCT=40            # CHANGE (live line 234 = 15)
    # item 8 — clean window
    RESET_CARRY_CHURN=off
    # review fixes (Aug 26) — new keys, run-6 defaults
    FINNHUB_INSIDER_LAG_DAYS=30      # Finnhub Form-4 keeps its pre-taxonomy weight (~0.23)
    OPTIONS_BLACKOUT_PUTS=on         # single-name puts also blocked into a print

Verify: `grep -nE '^(SCREENER_SOURCES|AUTO_HEDGE_MAX_PCT|EXPECTANCY_GATE)' .env`
must show exactly the values above (and no `EXPECTANCY_GATE=on`).

### 2. Strike the curated QQQ lesson (see the top of this file)

Prefix live `state/lessons/curated.md` line 15 with
`[SUPERSEDED 2026-08-26: QQQ is the system core] ` — in place, nothing deleted.

### 3. Arm the 09:31 ET flatten + clean fresh-cycle (Sunday night or before 09:00 Mon)

`scripts/flatten_and_restart.py` waits for the next regular session (09:31 ET),
stops the running bot, cancels every order, closes every position, runs
`investment_strategy.reset --yes` (which with `RESET_CARRY_CHURN=off` writes
NO churn memory — its output must read `churn carry: OFF (clean state …)`),
runs preflight, and relaunches the bot on the merged code:

    cd /Users/spusapati/Personal/Investment_stratergy
    nohup .venv/bin/python scripts/flatten_and_restart.py >> logs/flatten_restart.log 2>&1 &

If you prefer the two-step form (flatten by hand, then fresh cycle), run
`scripts/fresh_cycle.py --yes` instead of the relaunch — it prints
`churn carry (RESET_CARRY_CHURN): OFF — clean state …` before stopping the bot.

Record the equity at the flatten in `Todo-4.txt` as day-0 equity.

### 4. Verify at the open (09:35–10:30 ET Mon)

    tail -f logs/bot.log

- `FEEDS: n/n healthy news=vader-fallback` (n = enabled sources, 3 after dropping the
  Robinhood entries; a `DEAD`/`UNHEALTHY` token means fix before 10:00 or log it).
- one `BOOK BETA: spy=… qqq=… iwm=… invested=…` line per decision cycle
  (`BOOK BETA: unavailable (…)` on the very first cycle is fine — no series yet).
- `Expectancy gate (off, report-only): would have armed against: …` — present, not armed.
- `state/risk_state.json` has no `exit_times` / `exit_prices` / `loss_streaks`.
- `state/archive/<ts>/` holds the run-5 state; copy `trades.jsonl`,
  `equity_history.jsonl`, `risk_state.json` into `runs/pre-final-test-run-5/state/`.
- `ls state/*.lock` / `ps` — one bot instance, one growing log.
- After 16:00 ET: `Equity close row stamped for 2026-08-31 (basis=close).`, and
  the row carries `book_beta_spy`.

### 5. The verdict, Fri Sep 11 (or Sep 18 if N < 24)

    .venv/bin/python scripts/eval_contract_check.py --contract v2 \
        --start 2026-08-31 --end 2026-09-11 \
        --spy-csv spy.csv --bench-csv QQQ=qqq.csv --bench-csv IWM=iwm.csv \
        --beta-target 1.0 --beta-json state/risk_state.json
    # export spy/qqq/iwm csv with the snippet in the script's --help epilog
    # exit 0 GO / 2 NO-GO / 3 PENDING (expected for a single window: pooled N < 60)

## Review fixes (Aug 26 review of the change-set)

All behind the run-6 defaults; the two new `.env` keys are listed in section 1.

- **Beta shrinkage toward sign(beta)** (`portfolio/beta.py::shrink`): an
  inverse ETF now shrinks toward -1, so PSQ measures ~-1.1 in the book instead
  of -0.76. The beta hedge no longer re-arms against its own under-measured
  hedge, and the buy-path `BOOK BETA CAP` reads the true book beta.
- **Bearish precheck without a composite** (`orchestrator._bear_precheck_names`):
  a slate name whose bearish lean lives only in the DISCOVERY score (insider-sell
  discovery, PR #41) now reaches `put_precheck` / `put_eligibility` via
  `_bearish_lean`, so excluding DISCOVERY from the composite does not shrink
  the bearish funnel.
- **Finnhub insider lag preserved** (`signals/history.py::SOURCE_LAG_DAYS`,
  `FINNHUB_INSIDER_LAG_DAYS=30`): the CONGRESS -> INSIDER taxonomy move no
  longer re-weights the Finnhub Form-4 signal ~4x in the composite (contract:
  no re-weighting before >= 60 IC dates). `lag_weight(kind, source)`; the
  composite weights per signal (identical to before when one lag per kind).
  sec-edgar insider signals unchanged (2d).
- **Option fallback queue** (`orchestrator._option_fallback_allowed`): with
  `OPTIONS_SINGLE_NAME_BULLISH=off` an overextended single-name buy no longer
  queues a `decide_option_fallback` LLM call the risk layer would reject
  unconditionally; index/configured-ETF underlyings still queue.
- **Close row timing**: only the 16:xx ET closed tick mints `basis='close'`; a
  later start writes ONE `basis='late'` row (visible to the checker, never
  overwritten). A non-session day (Labor Day Sep 7) writes nothing
  (`AlpacaClient.is_trading_day`, fails open on a calendar read error).
- **Config enum validation** (`config._choice`): a typo in
  `LLM_SELL_AUTHORITY` / `AUTO_HEDGE_MODE` logs a WARNING and uses the run-6
  default (`events_only` / `beta`) instead of silently taking the legacy branch.
- **Put blackout is explicit** (`OPTIONS_BLACKOUT_PUTS`, default on): single-
  name puts stay blocked into a print (a long-vol debit loses to the IV crush
  either way); `off` lets puts through while calls stay blocked.
- **Rotation guard**: a loss-locking rotation sell that has REACHED its planned
  stop goes through the release ladder again (it would be approved downstream
  as "stop reached"); only the never-executes case (no event, stop not
  reached) bypasses to the risk layer.
- **Ops**: `signals/history.py` env reads can no longer raise at import;
  reconcile does ONE `get_order_by_id` per FILLED order
  (`AlpacaClient.order_fill_full`); `ledger.set_fill` writes `fill_ts=None`
  when the broker gave no `filled_at` (never "now").

Not changed (disagreements noted in the change-set result): none — every
finding was applied.
