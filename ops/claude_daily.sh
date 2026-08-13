#!/bin/sh
# Away-mode FALLBACK (2026-08-12). The primary daily babysit is a cron job
# inside the operator's long-running interactive Claude session; its daily
# log-archive commit is the liveness signal. This script (launchd, weekday
# 17:37) no-ops while that signal is fresh and only launches a headless
# `claude -p` run when the primary has gone quiet (>30h without a dated-log
# commit) — so the two never run concurrently in the shared working tree.
set -eu
ROOT="/Users/spusapati/Personal/Investment_stratergy"
export PATH="$HOME/.local/bin:/usr/local/bin:/opt/homebrew/bin:$PATH"
CLAUDE="$HOME/.local/bin/claude"
[ -x "$CLAUDE" ] || CLAUDE="$(command -v claude || true)"
[ -n "$CLAUDE" ] || { echo "claude CLI not found"; exit 1; }
cd "$ROOT"

last_commit_ts=$(git log -1 --format=%ct -- 'logs/*_*_*.log' 2>/dev/null || echo 0)
age_h=$(( ($(date +%s) - last_commit_ts) / 3600 ))
if [ "$age_h" -lt 30 ]; then
    echo "[$(date '+%F %T')] primary session healthy (last log-archive commit ${age_h}h ago) — skip"
    exit 0
fi

echo "[$(date '+%F %T')] primary quiet for ${age_h}h — running headless fallback"
"$CLAUDE" -p "FALLBACK away-mode run: the operator's interactive Claude session appears dead (no dated-log commit for ${age_h}h). Read ops/away_mode.md and execute the daily checklist end-to-end, including republishing the status artifact (URL inside the runbook) and noting on the status page that the fallback ran. Honor every guardrail in the runbook." \
    --permission-mode bypassPermissions \
    --model opus 2>&1
