#!/bin/sh
# Away-mode FALLBACK (2026-08-12). The primary daily babysit is a cron job
# inside the operator's long-running interactive Claude session; its daily
# log-archive commit is the liveness signal. This script (launchd, weekday
# 17:37) no-ops while that signal is fresh and only launches a headless
# `claude -p` run when the primary has gone quiet — two signals are checked
# (see below) so the two sessions don't collide in the shared working tree.
#
# Headless limits: a `claude -p` run has NO artifact tool and NO browser, so it
# can neither republish the phone status artifact nor re-auth Robinhood. It
# rewrites ops/status_page.html and commits its findings instead; the operator
# reads them from GitHub.
set -eu
ROOT="/Users/spusapati/Personal/Investment_stratergy"
export PATH="$HOME/.local/bin:/usr/local/bin:/opt/homebrew/bin:$PATH"
CLAUDE="$HOME/.local/bin/claude"
[ -x "$CLAUDE" ] || CLAUDE="$(command -v claude || true)"
[ -n "$CLAUDE" ] || { echo "claude CLI not found"; exit 1; }
cd "$ROOT"

now=$(date +%s)
# Liveness must come from the PRIMARY session only. Fallback runs prefix their
# commit messages "Away-mode fallback" (older runs: "Away-mode headless
# fallback"); on 2026-09-02 the Sep-1 fallback's own 23h-old archive commit
# read as "primary healthy" and a day of oversight was silently lost. Both
# signals therefore exclude fallback-authored commits.
last_log_ts=$(git log -1 --format=%ct --grep='Away-mode.*fallback' --invert-grep -- 'logs/*_*_*.log' 2>/dev/null || echo 0)
last_any_ts=$(git log -1 --format=%ct --grep='Away-mode.*fallback' --invert-grep 2>/dev/null || echo 0)
log_age_h=$(( (now - last_log_ts) / 3600 ))
any_age_h=$(( (now - last_any_ts) / 3600 ))

if [ "$log_age_h" -lt 30 ]; then
    echo "[$(date '+%F %T')] primary session healthy (last log-archive commit ${log_age_h}h ago) — skip"
    exit 0
fi

# Second liveness signal. The archive commit lands at most once a day, so it can
# drift past 30h while the primary is demonstrably alive — that fired a false
# fallback on 2026-08-13 that ran CONCURRENTLY with the live primary (both
# sessions did the checklist; the primary won the shared-tree race). Any commit
# at all in the last 12h means the primary is working, just late on the archive.
# (Fallback-authored commits are already excluded above, so they can never
# self-suppress the next day's run regardless of timing.)
if [ "$any_age_h" -lt 12 ]; then
    echo "[$(date '+%F %T')] archive stale (${log_age_h}h) but primary committed ${any_age_h}h ago — alive, skip"
    exit 0
fi

echo "[$(date '+%F %T')] primary quiet for ${log_age_h}h (no commits for ${any_age_h}h) — running headless fallback"
"$CLAUDE" -p "FALLBACK away-mode run: the operator's interactive Claude session appears dead (no dated-log commit for ${log_age_h}h, no commits at all for ${any_age_h}h). Read ops/away_mode.md and execute the daily checklist end-to-end. You are HEADLESS: you have no artifact tool and no browser, so do NOT attempt to republish the phone status artifact or re-auth Robinhood — instead rewrite ops/status_page.html with fresh values (it is gitignored — do NOT force-add it), commit a tracked HTML copy of it at runs/<current run dir>/STATUS_<YYYY-MM-DD>.html alongside your markdown report, note that the fallback ran and that the phone artifact is therefore stale, and push so the operator can read everything on GitHub. Prefix EVERY commit message with 'Away-mode fallback' — the next day's liveness check relies on that prefix to ignore your commits. Before doing anything, re-verify the primary really is dead (check for a recent ops/status_page.html mtime and recent commits); if it is alive, stop and report rather than racing it in the shared working tree. Honor every guardrail in the runbook." \
    --permission-mode bypassPermissions \
    --model opus 2>&1
