# Run-7 switch-time operator notes

Day 0 is **Fri 2026-09-18 after the bell** (run-6 Amendment 3, Option B); day 1
is **Mon 2026-09-21**. The day-0 checklist is section 3; the Monday verify list is
section 4. Sections 1-2 are the key inventory and the fingerprint block the
contract (`runs/pre-final-test-run-7/EVAL_CONTRACT.md`, "Config" row) points at.

Modelled on `docs/RUN6_SWITCH.md`. Everything in section 3 runs in the LIVE tree
`/Users/spusapati/Personal/Investment_stratergy`; nothing there is touched before
the run-6 Sep 18 close row exists (the freeze is re-registered to Sep 19 — zero
commits to `investment_strategy/` or `.env` on the live branch through Sep 18).

## 1. New `.env` keys introduced by the run-7 change-set

Compiled from every changelog (`A1..A7`, `B1..B4`, `C1..C3`, `S-1..S-8`,
`4a-15-16`, `4a-18`, `4a-20`, `FIX-PASS-*`) and cross-checked against
`investment_strategy/config.py` (`load_config`) and
`docs/env-variable-reference.md` (every key below has an entry there). Defaults
win where no line exists; write the STRATEGY KEYS explicitly so the profile is
self-describing (the fingerprint reads them from this file). Never commit `.env`.

### 1a. STRATEGY KEYS (enter the config fingerprint)

| key | default | item | one-line doc |
|---|---|---|---|
| `HEDGE_BETA_ASSUMED` | `-1.0` | S-2 | SPY-beta assumed for `HEDGE_ETF` ONLY when its own series cannot be read (reader off/blind, short history) or reads outside `[-3.0, -0.5]`; the measured, shrunk, cycle-cached beta divides the hedge gap otherwise and the `AUTO-HEDGE:` line names which one sized the arm. Must lie in `[-3.0, -0.5]` (else WARN + `-1.0`). NEVER pin to today's -1.51. |
| `PROXY_PUT_PREFER_MONTHLY` | `on` | S-3 | proxy put spread (`PUT_PROXY_ETF`) picks the third-Friday expiry first inside the DTE window and only OI-qualified strikes (>= `MIN_OPTION_OPEN_INTEREST` on both legs); `off` = expiries ranked by nearest-mid-DTE only (strikes still OI-qualified); pure legacy pick only when the chain reports no OI or the floor is 0. |
| `OPTION_STRIKE_SNAP` | `on` | S-4 | snap a model-proposed SINGLE-NAME put (long_put / bear_put_spread; never the proxy, an index underlying or a call) to the nearest OI-qualified, two-sided strike within 5% of the target BEFORE premium sizing and the liquidity gate; `off` = legs judged exactly as proposed (run-6 behaviour). |
| `OPTION_STRIKE_MAX_MONEYNESS_PCT` | `10` | S-4 | max abs(strike-spot)/spot for a single-name put leg before the snap re-targets it to ATM (two-sided: ITM and OTM alike); `0` = every single-name put re-targets ATM; inert when `OPTION_STRIKE_SNAP=off`. |
| `REGIME_LOOSEN_MIN_CYCLES` | `2` | S-5 | consecutive LOOSER regime reads before the label may loosen (tightening is immediate; the held label's tier caps the multiplier meanwhile); `1` = legacy no-memory. In-memory: a restart forgets the held label. Sep 10 2026 flapped 5x in 8 reads on a partial QQQ bar. |
| `HEDGE_UNWIND_MIN_CYCLES` | `1` | S-8 | consecutive below-band beta readings (counted once per decision cycle) before the beta hedge is closed; `1` = today's one-read unwind (ship at 1 — fingerprint-neutral at the default, a STRATEGY KEY at any other value; run-6 counterfactual: no N <= 9 would have kept the Sep 9 hedge). |
| `TOPUP_MIN_CONVICTION_DELTA` | `0.05` | S-6 | EXISTING key, not new — S-6 prints the bar on the HELD line (`top-up needs conviction >= 0.71 (+0.05 over the last buy) — else HOLD`) and compares at 4 dp; listed here because the contract names S-6 strategy-affecting and the value must be pinned. Not in the live `.env` today (code default applies). |

S-1 (slot exemption) and S-7 (core-defense mechanics) add **no key**:
`RiskLimits.slot_exempt_symbols` is derived from the existing `CORE_ETF`,
`HEDGE_ETF`, `DEFENSIVE_CORE_ETF` lines (the same three that feed
`Orchestrator._system_managed_symbols`, so the two sets cannot drift;
`PUT_PROXY_ETF` is deliberately NOT exempt). Those three keys, plus
`MAX_OPEN_POSITIONS`, `PUT_PROXY_ETF`, `MIN_OPTION_OPEN_INTEREST` and
`MAX_OPTION_SPREAD_PCT` (S-3/S-4 read them), therefore enter the fingerprint
with their live values (section 2).

### 1b. Ops / measurement keys (do NOT enter the fingerprint)

| key | default | item | one-line doc |
|---|---|---|---|
| `ALERT_RETRY_BASE_S` | `60` | A1 | seconds a page key waits after its first failed delivery on every sink; doubles per consecutive failure (`base * 2**(n-1)`). Loaded in `notify.load_alert_config`. |
| `ALERT_RETRY_CAP_S` | `900` | A1 | cap on that wait (= the default 15-min cooldown); `0` on either = retry at caller cadence (the Sep 7 storm behaviour, not recommended). |
| `ALERT_SPOOL_FILE` | `<dir of STATE_FILE>/alerts_spool.jsonl` | A1 | JSONL of undelivered pages, flushed as ONE summary page on the next successful delivery; blank disables spooling. |
| `SESSION_CALENDAR_FILE` | `<dir of STATE_FILE>/session_calendar.json` | A2 | JSON cache of exchange sessions (open/close per date) refreshed once per ET date by the decision loop; read by the watchdog paging window and `ops/deadman.py` so holidays/early closes are not market hours. Leave the default — the deadman reads the FIXED `state/` path. |
| `LEDGER_RESTATE_AT_FILL` | `on` | B2 (+4a-18) | restate equity SELL rows' `exit_price`/`realized_pl` (and an option BUY row's `cost_usd`) at the broker fill, the decision quote kept as `quote_*`; `off` = run-6 annotation-only `set_fill`. Read in `TradeLedger.__init__`, not a `Config` field. **Disclosure (fix pass):** measurement-only for POOLING purposes (fingerprint unchanged) BUT the restated series feeds the attribution prompt block once a cited source has >= `TRACK_RECORD_MIN_TRIPS` (20) trips — recorded here as the run-7 fingerprint note, not hidden. `perf_weights` / `negative_expectancy_families` also read it but are knob-off in run-7. |
| `ROBINHOOD_LOGIN_TIMEOUT_S` | `600` | C1 | wall-clock seconds `python -m investment_strategy.portfolio.robinhood_auth login` waits for the consent redirect before exit 3 + the runbook line (token file untouched); blank / <= 0 / non-numeric -> 600; `login --timeout N` overrides per run. Login tool only — the running bot never reads it. (The spec named this `ROBINHOOD_OAUTH_TIMEOUT_S`; the shipped key is `ROBINHOOD_LOGIN_TIMEOUT_S`.) |

Shell-only overrides on `ops/claude_daily.sh` (C3; not `.env`, not `config.py`):
`DRY_RUN=1`, `CLAUDE_DAILY_ROOT`, `PS_CMD`, `LSOF_CMD` — see the script header.

Items with no key of any kind: A3 (`Orchestrator.BROKER_5XX_ESCALATE = 10`
class constant), A4, A5, A6, A7 (rides `FEEDS_DEGRADED_MODES=on`), B1, B3
(`V3_*` constants in the checker), B4 (checker flags `--control-momentum`,
`--control-universe`), C2 (script constants), S-1, S-7, 4a-15-16, 4a-18
(`PHANTOM_DUPE_WINDOW_H = 4.0`, pinned equal in ledger and checker), 4a-20,
both fix passes. `LEDGER_FILL_PRICES` still has no top-level entry in the env
reference (pre-existing gap, noted by A6; only the `LEDGER_RESTATE_AT_FILL`
entry references it).

### 1c. The `.env` block to paste (run-7 defaults, every line explicit)

    # ---- run-7 change-set (2026-09-18) ----
    # S-2 hedge sizing by the hedge ETF's MEASURED SPY-beta
    HEDGE_BETA_ASSUMED=-1.0
    # S-3 proxy put: monthly-first, OI-qualified legs
    PROXY_PUT_PREFER_MONTHLY=on
    # S-4 single-name put strike snap
    OPTION_STRIKE_SNAP=on
    OPTION_STRIKE_MAX_MONEYNESS_PCT=10
    # S-5 regime label persistence (tighten fast, loosen slow)
    REGIME_LOOSEN_MIN_CYCLES=2
    # S-6 top-up bar printed on the HELD line (existing key, now pinned)
    TOPUP_MIN_CONVICTION_DELTA=0.05
    # S-8 beta-hedge unwind noise guard — ship at 1 (= run-6 behaviour)
    HEDGE_UNWIND_MIN_CYCLES=1
    # ops / measurement (not in the fingerprint)
    ALERT_RETRY_BASE_S=60
    ALERT_RETRY_CAP_S=900
    # ALERT_SPOOL_FILE, SESSION_CALENDAR_FILE: leave unset (state/ defaults)
    LEDGER_RESTATE_AT_FILL=on
    ROBINHOOD_LOGIN_TIMEOUT_S=600

Verify after pasting:

    grep -nE '^(HEDGE_BETA_ASSUMED|PROXY_PUT_PREFER_MONTHLY|OPTION_STRIKE_SNAP|OPTION_STRIKE_MAX_MONEYNESS_PCT|REGIME_LOOSEN_MIN_CYCLES|TOPUP_MIN_CONVICTION_DELTA|HEDGE_UNWIND_MIN_CYCLES|LEDGER_RESTATE_AT_FILL)=' .env
    # exactly the eight lines above, once each; and no legacy EXPECTANCY_GATE=on

## 2. STRATEGY KEY block for the run-7 fingerprint

Fingerprint = (sha256 of the `investment_strategy/` tree at the frozen merge;
the sorted keys below with their values; adaptive loops off). Commits touching
only `scripts/`, `ops/`, `docs/`, `runs/`, `Todo-4.txt` or logs do not enter it.
Run-6's fingerprint differs (S-1..S-7), so run-6 is a reference sample beside
run-7, never summed.

Values marked `<live>` are pre-existing lines whose value must be copied
verbatim from the live `.env` at pre-registration (`grep -nE '^KEY=' .env`) into
the contract's Config row — they were not re-defaulted by the change-set but
S-1/S-3/S-4 now read them.

    # --- carried from run-6 (docs/RUN6_SWITCH.md section 1), strategy-affecting ---
    LLM_SELL_AUTHORITY=events_only
    OPTIONS_SINGLE_NAME_BULLISH=off
    PROXY_PUT_THESIS_GATE=on
    PROXY_PUT_UNTRANSFERRED_PCT=0.25
    SIGNAL_HISTORY_RETENTION_DAYS=120
    SIGNAL_HISTORY_MAX_POINTS=480
    COMPOSITE_INCLUDE_DISCOVERY=off
    SCREENER_SOURCES=congress,insider,options_flow
    SCREENER_PRICE_FLOOR_PRE_CAP=on
    SCREENER_MIN_ADV_USD=0
    COMPOSITE_PERF_WEIGHTS=off          # adaptive loop off (validity condition)
    EXPECTANCY_GATE_ENABLED=off         # adaptive loop off (validity condition)
    TRACK_RECORD_MIN_TRIPS=20
    CURATED_LESSONS_INJECT=off          # adaptive loop off (validity condition)
    BOOK_BETA_ENABLED=on
    MAX_BOOK_BETA_SPY=1.2               # run-7 decision 6: stays 1.2 (cap - arm line = +0.05, logged at config load)
    AUTO_HEDGE_MODE=beta
    HEDGE_BETA_TARGET=1.0
    HEDGE_BETA_BAND=0.15
    HEDGE_BETA_FALLING_TARGET=0.8
    AUTO_HEDGE_MAX_PCT=40
    HEDGE_ETF=PSQ
    RESET_CARRY_CHURN=off
    FINNHUB_INSIDER_LAG_DAYS=30
    OPTIONS_BLACKOUT_PUTS=on
    # --- pre-existing keys the run-7 items now read (copy live values) ---
    CORE_ETF=<live>                     # S-1 slot exemption; S-7 core defense
    DEFENSIVE_CORE_ETF=<live>           # S-1 slot exemption
    MAX_OPEN_POSITIONS=<live>           # S-1: now = model names (keep 15, or 13 to match the pre-S-1 effective book)
    PUT_PROXY_ETF=<live>                # S-3 (NOT slot-exempt: an option underlying)
    MIN_OPTION_OPEN_INTEREST=<live>     # S-3 / S-4 OI floor
    MAX_OPTION_SPREAD_PCT=<live>        # S-4 two-sided NBBO cap
    # --- new in run-7 (section 1a) ---
    HEDGE_BETA_ASSUMED=-1.0
    PROXY_PUT_PREFER_MONTHLY=on
    OPTION_STRIKE_SNAP=on
    OPTION_STRIKE_MAX_MONEYNESS_PCT=10
    REGIME_LOOSEN_MIN_CYCLES=2
    TOPUP_MIN_CONVICTION_DELTA=0.05
    HEDGE_UNWIND_MIN_CYCLES=1

Measurement keys carried from run-6 that are NOT in the fingerprint (keep them
on): `POSTMORTEM_OPTION_MARKS=on`, `EQUITY_CLOSE_FIXED_STAMP=on`,
`LEDGER_FILL_PRICES=on`, `FEEDS_DEGRADED_MODES=on`; plus run-7
`LEDGER_RESTATE_AT_FILL=on` with the B2 disclosure above.

Fingerprint note (record in the contract's Config row): B2's fill-restated
`realized_pl` feeds `orchestrator._attribution_lessons()` (the attribution
prompt block) once a source has >= 20 pooled trips — a strategy INPUT delta
disclosed at pre-registration; the pooling series and the prompt block read
the same (broker-correct) numbers by design.

## 3. Day-0 switch checklist — Fri 2026-09-18

Sequenced exactly as the contract's "Day-0 switch sequence": run-6 close row ->
run-6 checker (both pinned commands) -> archive run-6 state -> swap `.env` to
the fresh account -> `fresh_cycle` -> restart after 16:00 ET -> verify a
`basis='late'` day-0 row. The merge sits between the checker and the account
swap so the run-6 verdict is computed on the run-6 code and the fresh account
never runs a cycle on old code.

### 0. Before Sep 18 — nothing in the live tree

- The change-set stays on `feature/run7-changeset` (isolated worktree). No
  merge into `feature/preview`, no bot restart, no `.env` edit before the
  Sep 18 close row (Amendment 3 freeze through Sep 18).
- Prepare offline: export `spy.csv` / `qqq.csv` / `iwm.csv` through Sep 18
  (snippet in the checker's `--help` epilog) on the evening of Sep 18; create
  the fresh paper account in the Alpaca dashboard and note its id + opening
  balance (contract "Account" row); have the section-1c block ready to paste.
- Optional: `rm state/alerts_spool.jsonl` is NOT done here — see section 5 for
  why a stale spool is harmless and when to delete it.

### 1. ~16:28 ET — wait for run-6's Sep 18 close row (old account, old code)

    cd /Users/spusapati/Personal/Investment_stratergy
    grep -n 'Equity close row stamped for 2026-09-18 (basis=close)' logs/bot.log
    grep '"2026-09-18"' state/equity_history.jsonl | grep '"basis": "close"'

Do not proceed until BOTH show the row. (A `late` row here would mean the bot
was restarted after the bell on the old account — it must not have been.)

### 2. Run the run-6 checker under BOTH pinned commands (still the run-6 script)

Per Amendment 3 point 1: `--end 2026-09-18`, run once, after the row exists;
`--start 2026-08-31` is PRIMARY (contract-pinned), `--start 2026-09-01` secondary.

    .venv/bin/python scripts/eval_contract_check.py --contract v2 \
        --start 2026-08-31 --end 2026-09-18 \
        --spy-csv spy.csv --bench-csv QQQ=qqq.csv --bench-csv IWM=iwm.csv \
        --beta-target 1.0 --beta-json state/risk_state.json \
        | tee runs/pre-final-test-run-6/CHECKER_2026-09-18_close_start0831.txt
    .venv/bin/python scripts/eval_contract_check.py --contract v2 \
        --start 2026-09-01 --end 2026-09-18 \
        --spy-csv spy.csv --bench-csv QQQ=qqq.csv --bench-csv IWM=iwm.csv \
        --beta-target 1.0 --beta-json state/risk_state.json \
        | tee runs/pre-final-test-run-6/CHECKER_2026-09-18_close_start0901.txt
    # exit 0 GO / 2 NO-GO / 3 PENDING (UNDER-FLOOR if N < 24 — judged as-is, no second extension)

Record the verdict + both exit codes in `runs/pre-final-test-run-6/RUN_SUMMARY.md`
and `Todo-4.txt`.

### 3. Archive run-6 state (before anything moves)

    mkdir -p runs/pre-final-test-run-6/state
    cp state/trades.jsonl state/equity_history.jsonl state/risk_state.json runs/pre-final-test-run-6/state/
    cp -r state/decisions runs/pre-final-test-run-6/state/ 2>/dev/null || true
    cp logs/Sep_1[4-8]_2026.log runs/pre-final-test-run-6/logs/ 2>/dev/null || true   # dated logs (runs/ archive convention)
    git add runs/pre-final-test-run-6 && git commit -m "Run-6 verdict: Sep 18 close checker + state archive"

`reset.py` (step 6) will ALSO move the live copies to `state/archive/<ts>/`; the
`runs/` copy is the committed one.

### 4. Merge the change-set and run the full suite (bot still on old code)

    git fetch origin
    git checkout feature/preview
    git merge --no-ff feature/run7-changeset -m "Run-7 change-set: A1-A7, B1-B4, C1-C3, S-1..S-8, 4a-15-20, fix passes"
    .venv/bin/python -m pytest -q --no-header -p no:cacheprovider tests     # expect ~1370+ passed, 0 failed
    git push origin feature/preview
    git rev-parse HEAD        # = the merge SHA for the contract's Config row
    date                      # = merge time; the process start time must be later (step 8)

Then pre-register: in `runs/pre-final-test-run-7/EVAL_CONTRACT.md` record the
merge SHA and the section-2 block (with the `<live>` values filled in) in the
Config row, flip the header from DRAFT to "Pre-registered 2026-09-18, BEFORE
day 1", and commit. `tests/test_contract_handles.py` un-skips
`test_pinned_handles_match_contract_file` the moment that file is on the branch —
run it once more after the commit (`.venv/bin/python -m pytest -q --no-header
-p no:cacheprovider tests/test_contract_handles.py`).

### 5. `.env` — fresh account + the run-7 block

- Swap `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` to the fresh paper account
  (`ALPACA_BASE_URL` unchanged, paper).
- Paste the section-1c block; run the section-1c grep.
- Run-6 lines stay as they are (section 2 carried block) — re-run the run-6
  verify grep too: `grep -nE '^(SCREENER_SOURCES|AUTO_HEDGE_MAX_PCT|EXPECTANCY_GATE)' .env`.
- Do NOT run `scripts/flatten_and_restart.py`: there is nothing to flatten on a
  fresh account and it is never the code-reload path.

### 6. Fresh cycle (stops the bot, archives + clears state, preflight, relaunch)

    nohup .venv/bin/python scripts/fresh_cycle.py --yes >> logs/fresh_cycle.log 2>&1 &
    tail -f logs/fresh_cycle.log

Expect `churn carry (RESET_CARRY_CHURN): OFF — clean state …`, three `archived
state/...` notes (risk_state.json, trades.jsonl, equity_history.jsonl [+
dashboard file]), `backup at state/archive/<ts>/`, preflight OK, `bot restarted
(pid N)`. The relaunch IS the day-0 restart — it happens after 16:00 ET by
construction (step 1 gated it), which is what mints the `late` row in step 8.
`state/session_calendar.json` and `state/alerts_spool.jsonl` are NOT
per-account and are left in place (section 5).

### 7. Restart verification (the control-panel rule, applied to the fresh-cycle relaunch)

    ps -o pid,lstart,command -p "$(cat state/bot.lock 2>/dev/null || pgrep -f 'python -m investment_strategy')"
    # process start time must be LATER than the merge time from step 4
    grep -n 'Starting orchestrator' logs/bot.log | tail -1
    grep -n 'BOOK BETA CAP vs hedge arm line' logs/bot.log | tail -1     # new config-load line = new code
    ls state/*.lock; ls -la logs/*.log | tail -3                          # one instance, one growing log

If the relaunch failed or a second restart is ever needed for a code reload:
stop the lock-holding pid, then relaunch via the control panel
(`ops/control_panel.py`, http://127.0.0.1:8787 -> Restart, `POST /api/restart`,
`restart_bot()`) — never the flatten script.

### 8. Verify the day-0 row on the NEW account (after the first closed tick, ~1-2 min)

    grep '"basis": "late"' state/equity_history.jsonl
    grep -n 'Equity close row stamped for 2026-09-18 (basis=late)' logs/bot.log

Exactly one `late` row dated 2026-09-18 with the fresh account's equity, carrying
`book_beta_spy` (may be null on the very first tick — acceptable for day 0; the
checker only reads ex-ante betas from rows on/after day 1). If the row is absent
by 17:00 ET the switch is defective: the checker would print `NO DAY-0 ANCHOR —
first session dropped`. Fix before Monday (restart again the same evening; a
second restart writes no second `late` row — the writer stamps ONE late row per
date).

Record day-0 equity (the `late` row's `equity`) in `Todo-4.txt` as run-7 day 0,
plus the fresh account id.

### 9. First-cycle handles (still Fri evening, any cycle after the restart)

    grep -nE 'FEEDS: |BOOK BETA CAP vs hedge arm line|Session calendar refreshed' logs/bot.log | tail -5

`FEEDS: 3/3 healthy news=vader-fallback earnings=<rh|yfinance-fallback>` (the
`earnings=` token is new — A7; `earnings=none` counts as UNHEALTHY), one
`BOOK BETA CAP vs hedge arm line: cap 1.20 - (target 1.00 + band 0.15) = +0.05`
line, and `Session calendar refreshed: N session(s) ...` with
`state/session_calendar.json` present. A closed-market cycle logs no `BOOK
BETA:` — that is Monday's check.

## 4. Verify at the open — Mon 2026-09-21 (09:35-10:30 ET, then through the day)

    tail -f logs/bot.log

Every handle below is a literal in the code (`tests/test_contract_handles.py`
pins the 22 contract handles; the rest are item handles). A handle that never
appears on a day where its trigger occurred is a measurement breach, not a zero.

| handle | expected on Monday | item |
|---|---|---|
| `SLOT COUNT: 14/15 model rows (exempt: QQQ, PSQ; raw 16)` | only on cycles where core/hedge rows changed the count; `At max open positions (N/15 model rows).` prints the judged count | S-1 |
| `AUTO-HEDGE: beta: ... hedge beta -1.51 measured; at -1.0 would be $...` | on every arm; `measured` expected (an `assumed` tail means the PSQ series was unreadable — check `Auto-hedge: PSQ SPY-beta ...` line); landing beta on the next read within +/-0.05 of target | S-2 |
| `REGIME HOLD: neutral held (1/2 clean reads); fresh read risk-on x1.00 -> applied neutral x0.70` | only after a tighter read; `Market regime:` line carries `held ... -> applied ...`; `REGIME LOOSEN:` / `REGIME TIGHTEN:` on label changes | S-5 |
| `PROXY PUT PICK: IWM 2026-10-16 291/276 (OI .../..., monthly, 45d) over legacy ...` | on every proxy-put build; target 0 proxy OI self-rejects | S-3 |
| `STRIKE SNAP: HD ... 400P (OI 2, 24.6% ITM) -> 320P (OI 1744, 0.3% OTM); unsnapped would be rejected: ...` | on every single-name put proposal (`kept — in band` / `none — no OI-qualified` otherwise); journal `reason` carries `[STRIKE SNAP: ...]` | S-4 |
| `ENTRY TAPE: NU spy_intraday=-0.42% regime=risk-on falling=2 would_haircut=$... stop=5.71% stop_if_floor6=6.00%` | one per equity buy (`n/a` fields when a read was unavailable) | 4a-15/16 |
| `CORE DEFENSE:` | ONLY on a genuine falling read; `CORE DEFENSE: stale falling map ... ignored at new-day open` must be 0 on Monday (no map from Friday can trim — target 0 wrong-day trims); `Core stop:` lines show a resting stop (stopless-window target 0) | S-7 |
| `BOOK BETA: spy=... hedge=PSQ w=... beta=... unhedged=...` | one per decision cycle (first cycle may read `unavailable` — no series yet) | 4a-17 / S-8 |
| `BOOK BETA (post-exec): spy=... (pre-exec spy=... delta=...; CROSSING ...)` | after every `_execute_proposals` with fills; crossings not seen pre-exec are the pre-registered metric | 4a-17 |
| `HEDGE COUNTERFACTUAL:` | only in the 5 sessions after an unwind — absent Monday is correct | 4a-17 / S-8 |
| `FEEDS: 3/3 healthy news=vader-fallback earnings=rh` | every cycle; `earnings=yfinance-fallback` is pre-declared non-confounding; > 2 `UNHEALTHY`/`DEAD`/`earnings=none` cycles in a day is a breach | A7 |
| `Session calendar refreshed: ...` + `ls -la state/session_calendar.json` | one refresh on the first cycle of the ET date; NO `Session calendar fallback:` WARNING after it | A2 |
| `ls state/alerts_spool.jsonl` | ABSENT (or emptied by the first successful page); its presence means pages are undeliverable — check `not delivered` / `Watchdog BLIND` and the alert sinks | A1 |
| `Top-up conviction ... (bar 0.71 = last buy +0.05)` | on top-up rejects; the HELD line in the journal prompt shows `top-up needs conviction >= ...` | S-6 |
| `Auto-hedge: beta: unwind read 1/2 — holding` | never at `HEDGE_UNWIND_MIN_CYCLES=1` (the line exists only for N >= 2) | S-8 |
| `LOT STAMP:` / `LEDGER PHANTOMS:` | `LOT STAMP:` on every sell; `LEDGER PHANTOMS:` should NOT print on a fresh ledger | 4a-18 |
| `Expectancy gate (off, report-only)` | present, not armed (adaptive loops off) | run-6 |

Also on Monday: `state/risk_state.json` has no `exit_times` / `exit_prices` /
`loss_streaks` (clean fresh cycle); `ls state/*.lock` + `ps` = one instance, one
growing log; after 16:00 ET `Equity close row stamped for 2026-09-21
(basis=close).` with `book_beta_spy`, `day_pl` and `broker_day_pl` on the row
and the log line `Equity close row 2026-09-21 day_pl self-consistent: ...`
(telescoping check; a `keeps the broker figure` line means the day-0 anchor was
unreadable — investigate).

## 5. State files the change-set adds (and what reset / fresh_cycle does with them)

| file / field | writer | reader | reset.py / fresh_cycle |
|---|---|---|---|
| `state/session_calendar.json` (+ `.tmp` during a write) | decision thread only (`Orchestrator._refresh_session_calendar`, once per ET date, tmp + `os.replace`); `{fetched, fetched_day, start, end, sessions{date:{open,close}}}` | watchdog paging window (same process) and `ops/deadman.py` at the FIXED `<repo>/state/session_calendar.json` (a non-default `SESSION_CALENDAR_FILE` is invisible to the deadman) | NOT per-account: left in place (not in `reset.per_account_paths`). After a restart the first decision cycle refreshes it; until then weekday 09:25-16:05 math applies with one `Session calendar fallback:` WARNING per date per process. Deleting it is harmless. |
| `state/alerts_spool.jsonl` (+ `.tmp` during a drop) | BOTH the watchdog thread (`critical()` held path) and the alerter worker | `Alerter.__init__` (logs pending count) and the first successful delivery (ONE catch-up summary page) | NOT per-account: left in place. A spool left over from before the switch emits one stale catch-up page on the first successful delivery afterwards (by design — those pages WERE undeliverable). `rm state/alerts_spool.jsonl` by hand before step 6 only if that page is unwanted. |
| `risk_state.json` -> `hedge_beta` (float), `hedge_beta_source` (`measured` / `assumed` / `""`) | `_apply_beta_hedge` at each arm resolution (cash-clamped/declined arms included) | eval checker (each arm's notional = gap x equity / abs(hedge_beta)); informational | per-account: archived + cleared with `risk_state.json`; `null` / `""` = never armed on this account |
| `risk_state.json` -> `book_beta.{hedge_etf, hedge_w, hedge_beta, unhedged_spy}` (new sub-keys beside the run-6 `spy/qqq/iwm/invested`) | `BOOK BETA:` stamp each cycle | dashboard / checker `--beta-json` | archived + cleared with `risk_state.json` |
| `risk_state.json` -> `last_unwind` `{symbol, qty, price, date, at, sessions[]}` | `set_last_unwind` at a hedge unwind; `mark_unwind_session` each of the next 5 sessions the `HEDGE COUNTERFACTUAL:` line is logged (persisted so a restart does not re-count) | orchestrator counterfactual line; postmortem `HEDGE WHIPSAW:` | archived + cleared with `risk_state.json` (`{}` on a fresh account) |
| ledger row fields (`trades.jsonl`): buy rows `spy_intraday_ret_at_decision`, `regime_label`, `falling_names`, `would_haircut_usd`, `stop_pct_if_floor_6`; sell rows `floor6_would_survive`, lot attributes (`entry_ts`, `entry_fill`, `composite`, `conviction`, `stop_pct`, `key_signals`, `lots_n`), `quote_exit_price` / `quote_realized_pl` / `quote_cost_usd`, backfilled `fill_price/fill_qty/fill_ts` | ledger (`set_fill`, `set_floor_shadow`, lot stamp) | checker v3, postmortem, dashboard | `trades.jsonl` archived + cleared; rows already on disk are never rewritten (phantom dedup happens at READ time in every consumer) |

Not persisted (in-memory, a restart forgets them): the S-5 held regime label
(the first post-restart read is applied as read — deliberately NOT stored in
`regime_label`, which keys the once-per-downturn risk-off trim), the S-8 unwind
read streak (`_unwind_reads`, also reset on arm/hold/unavailable), the S-7
breadth-map date stamp (`_breadth_map_date`; a fresh process has no map, so no
stale-map trim is possible on the restart day), the A1 per-key backoff table, and
the A3 5xx skip counter. `RESET_CARRY_CHURN=off` (run-6 default) keeps the fresh
`risk_state.json` free of `exit_times` / `exit_prices` / `loss_streaks`.

`reset.py` archives to `state/archive/<UTC ts>/` and never deletes (`--delete`
aside); `maybe_reset_on_account_change` would do the same automatically at
startup when the account id changes, so a forgotten step 6 self-heals — but
then the relaunch is the control-panel path, not `fresh_cycle`, and the
`churn carry: OFF` note lands in `bot.log` instead of `fresh_cycle.log`.
