# Run-8 switch-time operator notes

Run-7 closed **FAIL** on Oct 20 2026 (`runs/pre-final-test-run-7/RUN_SUMMARY.md`); the operator
stopped the bot Mon 2026-10-26 15:31 CT and the account is flattened at the Tue Oct 27 09:31 ET open
(`scripts/flatten_only.py`, log `logs/flatten_only.log`). Day 0 is **the evening the switch runs**
(after 16:00 ET); day 1 = the next session. Modelled on `docs/RUN7_SWITCH.md`; the contract is
`runs/pre-final-test-run-8/EVAL_CONTRACT.md` (v4), the change-set is
`runs/pre-final-test-run-8/CHANGESET_DRAFT.md` (revised at the close).

## 0. The switch (any evening after 16:00 ET)

Pre-conditions: PR `feature/run8-changeset` → `main` merged on GitHub; `logs/flatten_only.log` ends
with `account is flat`; the live tree `/Users/spusapati/Personal/Investment_stratergy` is on `main`.

1. **Alpaca account.** Either keep `PA3BGBBR2NB5` (flat, ~$977k) or create a fresh $1M paper
   account and swap `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` in the live `.env`. The switch script
   accepts the same account when it has no positions and no orders. Never commit `.env`.
2. **`.env` block.** Paste section 1c below into the live `.env`, then **change the existing line**
   `MAX_SYMBOL_EXPOSURE_PCT=18.0` → `MAX_SYMBOL_EXPOSURE_PCT=12.0` (B-2; every other run-7 line stays).
   Verify with the section-1c grep (each key exactly once).
3. **Robinhood.** `.venv/bin/python -m investment_strategy.portfolio.robinhood_auth login` (browser;
   auth has been dead since Oct 13). Not required to trade; restores `earnings=rh`.
4. **Run the switch from a copy OUTSIDE the tree** (it moves the tree):

       cd ~/Personal/Investment_stratergy
       git fetch origin
       git show origin/main:scripts/run8_switch.sh > /tmp/run8_switch.sh
       DRY_RUN=1 sh /tmp/run8_switch.sh        # every check, changes nothing
       sh /tmp/run8_switch.sh                  # the switch

   It refuses before the bell, with positions/orders on the account, with a missing/duplicated
   key line, or when the merged code is not on `origin/main`. It archives run-7 state to
   `runs/pre-final-test-run-7/state-post-window/`, moves the live tree to `origin/main`, runs the
   suite, runs `scripts/fresh_cycle.py --yes` (archives + clears local state, relaunches) and
   verifies the day-0 row + the new-code config line. Rollback is printed by the script.
5. **Re-arm the supervisors** (unloaded Oct 26 so nothing relaunched the stopped run-7 bot):

       launchctl enable gui/$(id -u)/com.investment-strategy.deadman
       launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.investment-strategy.deadman.plist
       launchctl enable gui/$(id -u)/com.investment-strategy.claude-daily
       launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.investment-strategy.claude-daily.plist
       # C-1 in-session check (new):
       cp ops/launchd/com.investment-strategy.session-check.plist ~/Library/LaunchAgents/
       launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.investment-strategy.session-check.plist
       launchctl list | grep investment      # deadman, claude-daily, session-check, panel, awake, backup

   Then `curl -s -X POST http://127.0.0.1:8787/api/unkill` — the kill switch was ENGAGED on Oct 26
   (`state/KILL`); `fresh_cycle` does not clear it and the preflight will say so.
6. **Pre-register** (same evening, before day 1): in `runs/pre-final-test-run-8/EVAL_CONTRACT.md`
   fill the Config row (merge SHA = `git rev-parse HEAD`, tree id = `git rev-parse
   HEAD:investment_strategy`), the Account row, both dates of the pinned verdict command (day 1 =
   next session; session 21 counted on the NYSE calendar), flip the header to "Pre-registered
   <date>, BEFORE day 1", and pin `FROZEN_TREE` in `scripts/run8_morning_check.py`. Commit, push,
   PR, merge, `git pull --ff-only` in the live tree (docs/scripts only: not a fingerprint change).
7. **Next morning:** `.venv/bin/python scripts/run8_morning_check.py` (~08:50 CT and again after
   09:50 ET). The C-1 launchd job repeats it at 09:55 / 15:35 ET and pages on FAIL.

## 1. Keys introduced by the run-8 change-set

Every knob is OFF / legacy in code; this block turns it on. Defaults and evidence are in the field
comments of `investment_strategy/config.py` (RiskLimits and Config "Run-8 change-set" blocks) and
in `runs/pre-final-test-run-8/CHANGESET_DRAFT.md`.

### 1a. STRATEGY KEYS (enter the fingerprint)

| key | run-8 value | item | one line |
|---|---|---|---|
| `TOPUP_AUTHORITY` | `events_only` | B-1 | a top-up needs an event since the last buy: composite +≥`TOPUP_MIN_COMPOSITE_DELTA`, price ≥ last fill +`TOPUP_MIN_GAIN_PCT`%, or a newly cited family; conviction delta recorded, never gating |
| `TOPUP_MAX_MULTIPLE_OF_HELD` | `1.0` | B-1 | one top-up lot ≤ 1× the held market value |
| `MAX_SYMBOL_EXPOSURE_PCT` | `12.0` (was 18) | B-2 | existing key, new value: no single name above the core |
| `MAX_TOP3_SATELLITE_PCT` | `30` | B-2 | the three largest satellites together ≤ 30% of equity (resize to headroom / reject) |
| `ENTRY_OPEN_DELAY_MIN` | `15` | B-3 | fresh equity buys in the first 15 min are deferred to a re-check cycle at open+15 |
| `BEARISH_IGNORED_FALLBACK` | `proxy_put` | B-4 | one scoped follow-up call per cycle for the most bearish IGNORED eligible name; HOLD/silence = no trade |
| `OPTION_ENTRY_TIER1_BUFFER_PCT` | `5` | B-5 | entry limit buffer 5% when every leg ≤ 5% wide with OI ≥ 1,000, else 20% |
| `PUT_NOQUOTE_COOLDOWN_CYCLES` | `8` | B-6 | a put that died on "no NBBO on feed" leaves the bearish slate for 8 cycles |
| `OPTION_OI_PER_CONTRACT` | `20` | B-7b | contracts ≤ thinnest-leg OI / 20 |
| `OPTION_MARK_FALLBACK` | `conservative` | B-7c | after `OPTION_MARK_FALLBACK_TICKS` (10) absurd-spread ticks, mark at liquidation (long@bid/short@ask) and let the premium stop evaluate |
| `OPTION_EXITS_RTH_ONLY` | `on` | N-1 | option stop/take/expiry closes only in the regular session; outside it one `OPTION EXIT QUEUED` line |
| `PREMIUM_CAP_SLATE_EXCLUDE` | `on` | B-8 | a premium-cap-bound underlying leaves the bearish slate for the day |

Keyless strategy items (enter the fingerprint through the code hash): B-6 honest `no NBBO on feed`
reject text; B-7a entry spread cap clamped to `OPTION_EXIT_MAX_SPREAD_PCT`.

### 1b. Ops / measurement keys (NOT in the fingerprint)

| key | value | item |
|---|---|---|
| `INSIDER_CACHE_MAX_AGE_H` | `24` | C-2: an EDGAR failure serves the last snapshot ≤ 24 h old as `insider=cached(<h>h)`; > 2 cached cycles/day is still a breach (contract v4) |

Keyless ops/measurement: C-3 yfinance ETF/no-data lines at DEBUG; C-4 `prior_*`/`topup_n`/
`topup_events` + IGNORED audit text; C-5 `entry_minute`; C-9 post-mortem not re-run after a
closed-day restart; C-1 `ops/in_session_check.sh` + plist; C-7 Robinhood < 24 h WARN;
B-5 `entry_limit_tier` + `OPTION ENTRY SLIP:`.

### 1c. The `.env` block to paste (every line explicit)

    # ---- run-8 change-set (2026-10-26) — STRATEGY KEYS ----
    # B-1 top-up authority: events, not restated conviction
    TOPUP_AUTHORITY=events_only
    TOPUP_MIN_COMPOSITE_DELTA=0.25
    TOPUP_MIN_GAIN_PCT=2.0
    TOPUP_MAX_MULTIPLE_OF_HELD=1.0
    # B-2 concentration (ALSO change the existing MAX_SYMBOL_EXPOSURE_PCT line to 12.0)
    MAX_TOP3_SATELLITE_PCT=30
    # B-3 no fresh entries in the first 15 minutes
    ENTRY_OPEN_DELAY_MIN=15
    # B-4 bearish IGNORED -> one scoped follow-up (model veto)
    BEARISH_IGNORED_FALLBACK=proxy_put
    # B-5 / B-6 / B-7 / B-8 option plumbing
    OPTION_ENTRY_TIER1_BUFFER_PCT=5
    PUT_NOQUOTE_COOLDOWN_CYCLES=8
    OPTION_OI_PER_CONTRACT=20
    OPTION_MARK_FALLBACK=conservative
    OPTION_MARK_FALLBACK_TICKS=10
    PREMIUM_CAP_SLATE_EXCLUDE=on
    # N-1 option exits only in the regular session
    OPTION_EXITS_RTH_ONLY=on
    # ---- ops / measurement (not in the fingerprint) ----
    INSIDER_CACHE_MAX_AGE_H=24

Verify after pasting (twelve run-8 strategy keys + the twelve run-7 keys, each once):

    grep -cE '^(TOPUP_AUTHORITY|TOPUP_MAX_MULTIPLE_OF_HELD|MAX_TOP3_SATELLITE_PCT|ENTRY_OPEN_DELAY_MIN|BEARISH_IGNORED_FALLBACK|OPTION_ENTRY_TIER1_BUFFER_PCT|PUT_NOQUOTE_COOLDOWN_CYCLES|OPTION_OI_PER_CONTRACT|OPTION_MARK_FALLBACK|OPTION_EXITS_RTH_ONLY|PREMIUM_CAP_SLATE_EXCLUDE|MAX_SYMBOL_EXPOSURE_PCT)=' .env   # 12
    grep -nE '^MAX_SYMBOL_EXPOSURE_PCT=' .env                                                       # =12.0, once
    # scripts/run8_switch.sh runs the same check and refuses otherwise

## 2. STRATEGY KEY block for the run-8 fingerprint

Fingerprint = (sha256 of the `investment_strategy/` tree at the merge; the sorted keys below with
their values; adaptive loops off). Run-7's fingerprint differs (B-1..B-8, N-1), so run-7 is a
reference sample beside run-8, never pooled (the kill criterion pools the two deliberately — see
the contract).

    # --- carried from run-6/run-7 (docs/RUN7_SWITCH.md section 2) — unchanged values ---
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
    MAX_BOOK_BETA_SPY=1.2
    AUTO_HEDGE_MODE=beta
    HEDGE_BETA_TARGET=1.0
    HEDGE_BETA_BAND=0.15
    HEDGE_BETA_FALLING_TARGET=0.8
    AUTO_HEDGE_MAX_PCT=40
    HEDGE_ETF=PSQ
    RESET_CARRY_CHURN=off
    FINNHUB_INSIDER_LAG_DAYS=30
    OPTIONS_BLACKOUT_PUTS=on
    CORE_ETF=<live>  DEFENSIVE_CORE_ETF=<live>  MAX_OPEN_POSITIONS=<live>  PUT_PROXY_ETF=<live>
    MIN_OPTION_OPEN_INTEREST=<live>  MAX_OPTION_SPREAD_PCT=<live>
    HEDGE_BETA_ASSUMED=-1.0  PROXY_PUT_PREFER_MONTHLY=on  OPTION_STRIKE_SNAP=on
    OPTION_STRIKE_MAX_MONEYNESS_PCT=10  REGIME_LOOSEN_MIN_CYCLES=2  TOPUP_MIN_CONVICTION_DELTA=0.05
    HEDGE_UNWIND_MIN_CYCLES=1  CORE_FILL_BETA_CLAMP=on  HEDGE_STARVED_CORE_TRIM=on
    HEDGE_STARVED_TRIM_MAX_PCT=50  REGIME_FALLING_TAPE_CAP=on
    # --- changed / new in run-8 (section 1a) ---
    MAX_SYMBOL_EXPOSURE_PCT=12.0        # was 18.0
    TOPUP_AUTHORITY=events_only
    TOPUP_MIN_COMPOSITE_DELTA=0.25
    TOPUP_MIN_GAIN_PCT=2.0
    TOPUP_MAX_MULTIPLE_OF_HELD=1.0
    MAX_TOP3_SATELLITE_PCT=30
    ENTRY_OPEN_DELAY_MIN=15
    BEARISH_IGNORED_FALLBACK=proxy_put
    OPTION_ENTRY_TIER1_BUFFER_PCT=5
    PUT_NOQUOTE_COOLDOWN_CYCLES=8
    OPTION_OI_PER_CONTRACT=20
    OPTION_MARK_FALLBACK=conservative
    OPTION_MARK_FALLBACK_TICKS=10
    PREMIUM_CAP_SLATE_EXCLUDE=on
    OPTION_EXITS_RTH_ONLY=on

`TOPUP_MIN_CONVICTION_DELTA` stays pinned at 0.05 but is inert under `events_only` (the legacy
gate only runs in `legacy` mode) — recorded here so the fingerprint is self-describing.

## 3. Verify at the open — day 1 (then every session via C-1)

| handle | expected | item |
|---|---|---|
| `ENTRY DEFERRED (open window):` | on the 09:30 cycle for every fresh equity buy; `ENTRY DEFERRED RESULT:` one per name on the 09:45 re-check cycle | B-3 |
| `TOPUP AUTHORITY:` | on every top-up verdict (`allowed on event(s) …` / `rejected (no event …)`); `lot clamped` when the 1× cap binds | B-1 |
| `BUCKET CAP:` | only when a buy would lift the top-3 satellites above 30% | B-2 |
| `BEARISH FALLBACK:` | one line per cycle that had an IGNORED eligible name (`veto:none/hold` or `<strategy> proposed`) | B-4 |
| `OPTION ENTRY SLIP:` | on every option BUY fill restatement, `tier=1|2` | B-5 |
| `PUT NO-QUOTE:` / `Bearish-slate excluded this cycle:` | on a quote-less put reject / the following cycles | B-6, B-8 |
| `OPTION LOT CAP:` | when OI / 20 binds a lot | B-7b |
| `OPTION MARK FALLBACK:` | WARNING on the first absurd-spread fallback per structure per day — expect it on any wide credit-ETF put; `premium mark unreliable` counts should drop toward 0 | B-7c |
| `OPTION EXIT QUEUED (closed market):` | only pre/post-market when a breach would have fired; **zero** `Option close FAILED` lines outside RTH | N-1 |
| `FEEDS: 3/3 healthy insider=cached(<h>h)` | only on an EDGAR failure; > 2 cached cycles/day = breach | C-2 |
| `Nightly post-mortem for <day> already on disk` | only after a closed-day restart | C-9 |
| `logs/morning_check.log` | two stamped blocks per session (09:55 / 15:35 ET) | C-1 |

Plus the run-7 section-4 handles (unchanged): `FEEDS:`, `BOOK BETA:`, `AUTO-HEDGE: beta:`,
`CORE DEFENSE:`, `SLOT COUNT:`, `STRIKE SNAP:`, `PROXY PUT PICK:`, `LOT STAMP:`, one bot instance,
one growing log, a `basis='late'` day-0 row with the account's equity.
