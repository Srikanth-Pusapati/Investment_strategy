# Run-6 switch-time operator notes

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
