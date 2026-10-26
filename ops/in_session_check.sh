#!/bin/sh
# Run-8 C-1 — the in-session health check, automated.
#
# WHY: on 2026-09-25 the FEEDS validity breach (insider UNHEALTHY in 3/8
# cycles) was visible in the log at 10:17 CT and undisclosed until the next
# morning because nobody ran the check in-session. launchd runs this at
# 09:55 and 15:35 ET on weekdays (ops/launchd/com.investment-strategy.session-check.plist),
# it appends the FAIL/WARN/VERDICT lines to logs/morning_check.log, and pages
# through the bot's own alert sink (SMTP/webhook, same as ops/deadman.py) when
# the check exits non-zero. Read-only: it never touches the bot or the broker.
#
# Install / uninstall (the plist header has the exact commands):
#   cp ops/launchd/com.investment-strategy.session-check.plist ~/Library/LaunchAgents/
#   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.investment-strategy.session-check.plist
# Run by hand: sh ops/in_session_check.sh
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT" || exit 1
PY="$ROOT/.venv/bin/python"
LOG="$ROOT/logs/morning_check.log"
CHECK="${SESSION_CHECK_SCRIPT:-scripts/run8_morning_check.py}"
STAMP=$(TZ=America/New_York date '+%Y-%m-%d %H:%M:%S ET')

OUT=$("$PY" "$CHECK" 2>&1)
RC=$?
LINES=$(printf '%s\n' "$OUT" | grep -E '^[[:space:]]*(FAIL|WARN)[[:space:]]|^VERDICT')
{
    printf '[%s] %s exit=%s\n' "$STAMP" "$CHECK" "$RC"
    printf '%s\n' "$LINES"
} >> "$LOG"

if [ "$RC" -ne 0 ]; then
    BODY=$(printf '%s\n' "$LINES" | head -30)
    "$PY" - "$BODY" "$STAMP" <<'PYEOF'
import sys
from investment_strategy.config import load_config
from investment_strategy.notify import Alerter
body, stamp = sys.argv[1], sys.argv[2]
Alerter(load_config().alerts).critical(
    "session-check:fail",
    "SESSION CHECK: run-8 in-session check FAILED",
    f"{body}\n\nChecked at {stamp} by ops/in_session_check.sh (read-only).",
)
PYEOF
fi
exit "$RC"
