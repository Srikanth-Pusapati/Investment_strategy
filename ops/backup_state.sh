#!/bin/sh
# Nightly off-machine backup of state/ (goGA GA-2.8).
#
# state/ is the bot's only memory that money depends on: the trade ledger
# (trades.jsonl), the equity history (the GA-1.2 track record!), and the risk
# state (peak equity / halt latch). It is gitignored by design, so git is NOT a
# backup for it. This script snapshots it to a timestamped tarball and, when a
# destination is configured, pushes it off the machine.
#
# Usage:
#   ops/backup_state.sh                    # snapshot into ./backups
#   BACKUP_DIR=~/Backups/bot ops/backup_state.sh
#   BACKUP_RCLONE_REMOTE=gdrive:bot-state ops/backup_state.sh   # + rclone copy
#
# Off-machine options (pick one, in increasing effort):
#   - BACKUP_DIR on an iCloud/Dropbox-synced folder (zero new tools),
#   - BACKUP_RCLONE_REMOTE with rclone configured (any cloud bucket),
#   - run this ON the VPS and rclone back to your machine.
#
# Schedule it nightly with ops/launchd/com.investment-strategy.backup.plist
# (mac) or a cron entry on the VPS:  15 2 * * * /path/to/ops/backup_state.sh
#
# Retention: keeps the newest $BACKUP_KEEP (default 30) local tarballs.
set -eu

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
STATE_DIR="${STATE_DIR:-$REPO_DIR/state}"
BACKUP_DIR="${BACKUP_DIR:-$REPO_DIR/backups}"
BACKUP_KEEP="${BACKUP_KEEP:-30}"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$BACKUP_DIR/state-$STAMP.tar.gz"

if [ ! -d "$STATE_DIR" ]; then
    echo "backup_state: no state dir at $STATE_DIR — nothing to back up." >&2
    exit 0
fi

mkdir -p "$BACKUP_DIR"
# -C so the tarball holds "state/..." paths regardless of where this runs.
tar -czf "$OUT" -C "$(dirname "$STATE_DIR")" "$(basename "$STATE_DIR")"
echo "backup_state: wrote $OUT ($(du -h "$OUT" | cut -f1))"

# Optional off-machine copy. rclone must be installed + configured.
if [ -n "${BACKUP_RCLONE_REMOTE:-}" ]; then
    if command -v rclone >/dev/null 2>&1; then
        rclone copy "$OUT" "$BACKUP_RCLONE_REMOTE" && \
            echo "backup_state: copied to $BACKUP_RCLONE_REMOTE"
    else
        echo "backup_state: BACKUP_RCLONE_REMOTE set but rclone not installed." >&2
        exit 1
    fi
fi

# Local retention: newest $BACKUP_KEEP tarballs survive.
ls -1t "$BACKUP_DIR"/state-*.tar.gz 2>/dev/null | tail -n +"$((BACKUP_KEEP + 1))" |
while IFS= read -r old; do
    rm -f "$old"
    echo "backup_state: pruned $old"
done
