#!/bin/sh
# Away-mode FALLBACK (2026-08-12). The primary daily babysit is a cron job
# inside the operator's long-running interactive Claude session; its daily
# log-archive commit is the liveness signal. This script (launchd, weekday
# 17:37) no-ops while that signal is fresh and only launches a headless
# `claude -p` run when the primary has gone quiet — a process gate and two
# staleness signals are checked (see below) so the two sessions don't collide
# in the shared working tree.
#
# Headless limits: a `claude -p` run has NO artifact tool and NO browser, so it
# can neither republish the phone status artifact nor re-auth Robinhood. It
# rewrites ops/status_page.html and commits its findings instead; the operator
# reads them from GitHub.
#
# Verifying by hand (no side effects — the launch stamp is not written and
# claude is not started):
#   DRY_RUN=1 sh ops/claude_daily.sh
# Overrides exist only so the gate can be exercised against stub commands and
# a scratch checkout; launchd never sets them:
#   CLAUDE_DAILY_ROOT  repo root to judge (default: the live tree below)
#   PS_CMD / LSOF_CMD  commands used by the process gate (default: ps / lsof)
set -eu
ROOT="${CLAUDE_DAILY_ROOT:-/Users/spusapati/Personal/Investment_stratergy}"
export PATH="$HOME/.local/bin:/usr/local/bin:/opt/homebrew/bin:$PATH"
CLAUDE="$HOME/.local/bin/claude"
[ -x "$CLAUDE" ] || CLAUDE="$(command -v claude || true)"
[ -n "$CLAUDE" ] || { echo "claude CLI not found"; exit 1; }
cd "$ROOT"
DRY_RUN="${DRY_RUN:-0}"
PS_CMD="${PS_CMD:-ps}"
LSOF_CMD="${LSOF_CMD:-lsof}"
tag=""
if [ "$DRY_RUN" != 0 ]; then tag="[DRY_RUN] "; fi

now=$(date +%s)

# Gate 0 — process gate (2026-09-12, run-7 item C3). A live `claude` process
# whose cwd is the repo IS the primary session: quiet or wedged, it is not
# dead, and launching a headless run beside it races it in the shared working
# tree. The commit/status-page signals below cannot see such a process and
# false-fired three times in run-6 against one true fire:
#   Sep 4  fire (102h/90h)  — primary alive, stood down
#   Sep 7  fire (174h/162h) — primary genuinely dead (TRUE)
#   Sep 8  fire (198h/186h) — primary alive, had edited status_page after us
#   Sep 10 fire (30h/69h)   — primary alive but wedged 2h44m on the Robinhood
#                             OAuth wait (its foreground Bash never returned)
# Both stand-down reports named `ps` as the only signal that worked, so it now
# runs FIRST. Mechanics:
#   * enumerate with `ps`, never `pgrep`: BSD pgrep hides its own ancestors,
#     so a manual run from inside a Claude session would miss the very session
#     it was launched from (measured 2026-09-11: `pgrep -x claude` returned 1
#     of 2 binaries, `ps -axo comm=` both). Ancestors are deliberately NOT
#     excluded for the same reason — a session that ran this script by hand
#     in the repo is, by construction, alive in the repo.
#   * `comm=` is the binary path (…/native-binary/claude or ~/.local/bin/
#     claude); the desktop app's "Claude Helper" processes never match.
#   * cwd via `lsof -a -p PID -d cwd -Fn` (≈6 ms per pid); only cwd == $ROOT
#     or a directory beneath it counts — a session in another project (Sep 11
#     probe: pid 82870 in another checkout) must not suppress the fallback.
#   * a still-running headless fallback (`claude -p …`, cwd == $ROOT) also
#     matches, which is the right answer: never start a second one beside it.
# Residual (documented in ops/away_mode.md): a session left open but idle
# (its in-session cron dead) suppresses the fallback indefinitely. The
# staleness numbers therefore stay in the skip line so logs/claude_daily.log
# still shows the drift; the phone status page going stale is the operator's
# signal in that case.
live_claude_pids() {
    "$PS_CMD" -ax -o pid=,etime=,comm= 2>/dev/null \
    | awk '{ cmd = $3; for (i = 4; i <= NF; i++) cmd = cmd " " $i
             if (cmd ~ /(^|\/)claude$/ || cmd ~ /native-binary\/claude/) print $1, $2 }' \
    | while read -r pid etime; do
        cwd=$("$LSOF_CMD" -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1) || true
        case "$cwd" in
            "$ROOT"|"$ROOT"/*) echo "${pid}[${etime}]" ;;
        esac
      done
}

# Staleness signals (decided AFTER the process gate; measured here so the
# gate's skip line can still report them).
#
# Liveness must come from the PRIMARY session only. Fallback runs prefix their
# commit messages "Away-mode fallback" (older runs: "Away-mode headless
# fallback"); on 2026-09-02 the Sep-1 fallback's own 23h-old archive commit
# read as "primary healthy" and a day of oversight was silently lost. The
# commit signal therefore excludes fallback-authored commits.
#
# 2026-09-09 rework (guards were structurally unsatisfiable — Sep 4/7 reports):
# the old guard 1 (dated-log archive commit < 30h) could never fire again once
# the fallbacks took over all log archiving, and the old guard 2 (any commit
# < 12h) always missed because the primary works ~23:58 CT while this check
# fires 17:37 — a built-in ~17.6h gap. Both failed every weekday, so the
# fallback launched daily regardless of primary state. Also fixed: the old
# `--grep --invert-grep` exclusion matched the commit BODY, so a primary
# commit that merely *discussed* the fallback excluded itself (that is what
# false-fired Sep 4 against commit 9a9b05e); the subject-only awk match below
# cannot. Now:
#   * one freshness threshold T: 30h normally, 78h on Mondays (the primary's
#     last session is Friday night; the weekend is not evidence of death)
#   * signal 1: any non-fallback commit younger than T
#   * signal 2: ops/status_page.html mtime younger than T — the runbook makes
#     the primary rewrite it every session — UNLESS the rewrite falls inside
#     a window started by our own last fallback launch (the fallback rewrites
#     the same file; state/claude_daily_run.stamp records each launch so the
#     fallback can never count its own rewrite as primary liveness)
[ "$(date +%u)" = 1 ] && T=78 || T=30

last_any_ts=$(git log -300 --format='%ct%x09%s' 2>/dev/null \
    | awk -F'\t' '$2 !~ /^Away-mode.*fallback/ {print $1; exit}')
[ -n "$last_any_ts" ] || last_any_ts=0
any_age_h=$(( (now - last_any_ts) / 3600 ))
page_ts=$(stat -f %m "$ROOT/ops/status_page.html" 2>/dev/null || echo 0)
page_age_h=$(( (now - page_ts) / 3600 ))

if command -v "$LSOF_CMD" >/dev/null 2>&1; then
    alive=$(live_claude_pids | tr '\n' ' ')
    alive=${alive% }
    if [ -n "$alive" ]; then
        echo "${tag}[$(date '+%F %T')] primary process alive (pid ${alive}) with cwd=$ROOT — skip (commit ${any_age_h}h ago, status page ${page_age_h}h old)"
        exit 0
    fi
    ps_gate="no claude process with cwd=$ROOT"
else
    ps_gate="lsof unavailable, process gate skipped"   # fail open to the staleness signals
fi

if [ "$any_age_h" -lt "$T" ]; then
    echo "${tag}[$(date '+%F %T')] primary committed ${any_age_h}h ago (< ${T}h) — alive, skip (${ps_gate})"
    exit 0
fi

stamp_ts=$(cat "$ROOT/state/claude_daily_run.stamp" 2>/dev/null || echo 0)
fallback_wrote_page=0
if [ "$page_ts" -ge "$stamp_ts" ] && [ "$page_ts" -lt $(( stamp_ts + 14400 )) ]; then
    fallback_wrote_page=1   # rewrite landed within 4h of our own launch
fi
if [ "$page_age_h" -lt "$T" ] && [ "$fallback_wrote_page" = 0 ]; then
    echo "${tag}[$(date '+%F %T')] no primary commit for ${any_age_h}h but status page rewritten ${page_age_h}h ago (< ${T}h, not by a fallback) — alive, skip (${ps_gate})"
    exit 0
fi

echo "${tag}[$(date '+%F %T')] primary quiet (${ps_gate}; no commit for ${any_age_h}h; status page ${page_age_h}h old$([ "$fallback_wrote_page" = 1 ] && echo ', last rewrite was our own fallback')) — running headless fallback"
if [ "$DRY_RUN" != 0 ]; then
    echo "${tag}stamp not written, claude not launched"
    exit 0
fi
echo "$now" > "$ROOT/state/claude_daily_run.stamp"
"$CLAUDE" -p "FALLBACK away-mode run: the operator's interactive Claude session appears dead (no claude process with cwd=$ROOT; no non-fallback commit for ${any_age_h}h; ops/status_page.html untouched by it for ${page_age_h}h). Read ops/away_mode.md and execute the daily checklist end-to-end. You are HEADLESS: you have no artifact tool and no browser, so do NOT attempt to republish the phone status artifact or re-auth Robinhood — instead rewrite ops/status_page.html with fresh values (it is gitignored — do NOT force-add it), commit a tracked HTML copy of it at runs/<current run dir>/STATUS_<YYYY-MM-DD>.html alongside your markdown report, note that the fallback ran and that the phone artifact is therefore stale, and push so the operator can read everything on GitHub. Prefix EVERY commit message with 'Away-mode fallback' — the next day's liveness check relies on that prefix to ignore your commits. Before doing anything, re-verify the primary really is dead — check ps FIRST (any 'claude' process whose cwd is this repo means it is alive, even if it looks quiet or wedged), then a recent ops/status_page.html mtime and recent commits; if it is alive, stop and report rather than racing it in the shared working tree. Honor every guardrail in the runbook." \
    --permission-mode bypassPermissions \
    --model opus 2>&1
