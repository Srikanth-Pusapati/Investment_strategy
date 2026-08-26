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
