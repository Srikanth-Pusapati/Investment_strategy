#!/bin/sh
# Run-8 day-0 switch — AFTER 16:00 ET on the switch day. The live tree is
# still on the OLD code (this file is not in it yet) and the script moves the
# tree to new code, so it must run from a copy OUTSIDE the tree:
#
#   cd ~/Personal/Investment_stratergy            # the live tree (branch main)
#   git fetch origin
#   git show origin/main:scripts/run8_switch.sh > /tmp/run8_switch.sh
#   DRY_RUN=1 sh /tmp/run8_switch.sh              # every check, changes nothing
#   sh /tmp/run8_switch.sh                        # the switch
#
# What it does, in order (docs/RUN8_SWITCH.md section 0):
#   checks   market closed · merged change-set is on origin/main · .env has the
#            twenty-four run-7 + run-8 strategy key lines once each · the keys
#            in .env open a FLAT paper account (no positions, no open orders —
#            run-7's own account is accepted once flattened; a new one too)
#   stop     the running bot (SIGTERM the lock-holding pid IF it is the bot, wait <= 30 s)
#   code     back up HEAD on a branch, stash the live tree's local docs, move
#            main to origin's (the live tree is on main since the Sep 30 repo move)
#   archive  old state -> runs/pre-final-test-run-7/state-post-window/
#   tests    full suite must pass
#   start    scripts/fresh_cycle.py --yes (archives + clears state, relaunches)
#   verify   a day-0 row for today (basis late or close) + the new-code config line
# It never edits .env, never touches the kill switch, never force-pushes.
# FORCE_TIME=1 skips the after-the-bell check (weekends / holidays).
set -eu

DRY=${DRY_RUN:-0}
say() { printf '%s\n' "$*"; }
die() { printf 'ABORT: %s\n' "$*" >&2; exit 1; }

# The live tree is the CURRENT directory (or LIVE_ROOT). The script itself must
# live outside it: step 6 stashes untracked files and checks out new code, and
# a shell script rewritten or removed while it runs misbehaves.
ROOT=$(cd "${LIVE_ROOT:-$(pwd)}" && pwd)
cd "$ROOT"
[ -d .git ] && [ -d investment_strategy ] && [ -f .env ] \
    || die "$ROOT is not the live tree (.git, investment_strategy/, .env) — cd into it first."
SELF=$(cd "$(dirname "$0")" && pwd)
case "$SELF/" in
    "$ROOT"/*) [ "$DRY" = 1 ] || die "this script sits inside the live tree ($SELF) — copy it out first: git show origin/main:scripts/run8_switch.sh > /tmp/run8_switch.sh" ;;
esac
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || die "$PY not found"

say "== run-8 switch ($( [ "$DRY" = 1 ] && echo DRY RUN || echo LIVE )) in $ROOT"

# -- 1. after the bell ------------------------------------------------------- #
ET_HM=$(TZ=America/New_York date +%H%M); ET_DOW=$(TZ=America/New_York date +%u)
if [ "${FORCE_TIME:-0}" != 1 ] && [ "$ET_DOW" -le 5 ] && [ "$ET_HM" -lt 1600 ]; then
    die "it is $ET_HM ET on a weekday — run after 16:00 ET (the day-0 'late' row needs a post-bell restart)."
fi
say "ok  time: $ET_HM ET (dow $ET_DOW)"

# -- 2. the merged change-set is on origin/main ------------------------------ #
# The live tree runs `main` (Sep 30 2026 repo move); the old preview branch is
# no longer the live line.
git fetch -q origin main || die "git fetch failed"
for marker in TOPUP_AUTHORITY ENTRY_OPEN_DELAY_MIN BEARISH_IGNORED_FALLBACK INSIDER_CACHE_MAX_AGE_H; do
    git show origin/main:investment_strategy/config.py 2>/dev/null \
        | grep -q "$marker" \
        || die "origin/main lacks $marker — merge the run-8 change-set PR into main first."
done
git show origin/main:scripts/run8_switch.sh >/dev/null 2>&1 \
    || die "origin/main lacks scripts/run8_switch.sh — the run-8 change-set PR is not merged."
OLD_SHA=$(git rev-parse HEAD); NEW_SHA=$(git rev-parse origin/main)
say "ok  code: origin/main = $NEW_SHA (live tree now at $OLD_SHA)"

# -- 3. .env carries the run-7 + run-8 strategy-key block -------------------- #
[ -f .env ] || die ".env not found"
for k in HEDGE_BETA_ASSUMED PROXY_PUT_PREFER_MONTHLY OPTION_STRIKE_SNAP \
         OPTION_STRIKE_MAX_MONEYNESS_PCT REGIME_LOOSEN_MIN_CYCLES \
         TOPUP_MIN_CONVICTION_DELTA HEDGE_UNWIND_MIN_CYCLES \
         CORE_FILL_BETA_CLAMP HEDGE_STARVED_CORE_TRIM HEDGE_STARVED_TRIM_MAX_PCT \
         REGIME_FALLING_TAPE_CAP LEDGER_RESTATE_AT_FILL \
         TOPUP_AUTHORITY TOPUP_MAX_MULTIPLE_OF_HELD MAX_TOP3_SATELLITE_PCT \
         ENTRY_OPEN_DELAY_MIN BEARISH_IGNORED_FALLBACK OPTION_ENTRY_TIER1_BUFFER_PCT \
         PUT_NOQUOTE_COOLDOWN_CYCLES OPTION_OI_PER_CONTRACT OPTION_MARK_FALLBACK \
         OPTION_EXITS_RTH_ONLY PREMIUM_CAP_SLATE_EXCLUDE MAX_SYMBOL_EXPOSURE_PCT; do
    n=$(grep -cE "^${k}=" .env || true)
    [ "$n" = 1 ] || die ".env has $n line(s) for $k (need exactly 1) — paste the docs/RUN8_SWITCH.md STRATEGY KEY block."
done
if grep -qE '^EXPECTANCY_GATE(_ENABLED)?=on' .env; then
    die ".env has the expectancy gate ON — adaptive loops must be off (contract validity)."
fi
say "ok  .env: twenty-four run-7 + run-8 key lines present once each; expectancy gate off"

# -- 4. the keys belong to a FLAT paper account (new, or run-7's emptied one) - #
OLD_ACCT=$(sed 's/^paper://' state/account.json 2>/dev/null || true)
# Retried: the SDK call has no timeout of its own, and on Sep 21 2026 one TLS
# handshake to paper-api timed out and aborted a switch whose bot was already
# stopped (the same read had passed two minutes earlier).
read_account() {
    "$PY" - <<'PYEOF'
from dotenv import dotenv_values
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.enums import QueryOrderStatus
env = dotenv_values(".env")
c = TradingClient(env["ALPACA_API_KEY"], env["ALPACA_SECRET_KEY"], paper=True)
a = c.get_account()
npos = len(c.get_all_positions())
nord = len(c.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=50)))
print(f"{a.account_number} {a.equity} {npos} {nord}")
PYEOF
}
ACCT_ERR=$(mktemp "${TMPDIR:-/tmp}/run8_switch_acct.XXXXXX"); try=0
until ACCT_LINE=$(read_account 2>"$ACCT_ERR"); do
    try=$((try + 1))
    why=$(tail -1 "$ACCT_ERR" | cut -c1-160)
    [ "$try" -lt 3 ] || { rm -f "$ACCT_ERR"; die "could not read the Alpaca account with the keys in .env (3 attempts): $why"; }
    say "..  Alpaca account read failed ($why) — retry $try/2 in 5 s"
    sleep 5
done
rm -f "$ACCT_ERR"
set -- $ACCT_LINE
NEW_ACCT=$1; NEW_EQ=$2; NPOS=$3; NORD=$4
# Run-8 accepts the SAME paper account run-7 traded, provided it is flat (every
# position closed and no working order — scripts/flatten_only.py after the
# bell); a fresh account works as before. Anything open or working aborts.
[ "$NPOS" = 0 ] && [ "$NORD" = 0 ] \
    || die "account $NEW_ACCT has $NPOS position(s) / $NORD open order(s) — flatten it (scripts/flatten_only.py) or paste a fresh paper account's keys."
if [ "$NEW_ACCT" = "$OLD_ACCT" ]; then
    say "ok  account: $NEW_ACCT equity \$$NEW_EQ — same paper account, flat — reusing"
else
    say "ok  account: $NEW_ACCT equity \$$NEW_EQ, 0 positions, 0 orders (old: ${OLD_ACCT:-none})"
fi

if [ "$DRY" = 1 ]; then
    say "== DRY RUN: all checks passed. Would now: archive state, stop the bot, stash local docs,"
    say "   move main $OLD_SHA -> $NEW_SHA, run the tests, fresh_cycle, verify the late row."
    exit 0
fi

ROLLBACK="git checkout -B main $OLD_SHA   # then restore the old keys in .env and restart from the control panel"

# -- 5. stop the bot --------------------------------------------------------- #
# Only ever signal a pid that IS the bot: a stale lock with a recycled pid must
# not SIGTERM an unrelated process (ops.deadman.bot_alive makes the same check).
PID=$(cat state/bot.lock 2>/dev/null || true)
# "Running" is judged by the command line, never by `kill -0` alone: a bot that
# has exited but is not yet reaped by its parent (the control panel) is a
# ZOMBIE — `kill -0` still succeeds on it while ps shows `<defunct>`. On Sep 21
# 2026 the wait loop below sat 30 s on exactly that and aborted a good stop.
bot_running() { ps -o command= -p "$1" 2>/dev/null | grep -q 'investment_strategy'; }
if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
    if bot_running "$PID"; then
        say "..  stopping bot pid $PID"
        kill -TERM "$PID"
        i=0
        while bot_running "$PID"; do
            i=$((i + 1)); [ "$i" -le 60 ] || die "bot pid $PID did not exit in 30 s — nothing else was changed."
            sleep 0.5
        done
    else
        say "..  state/bot.lock names pid $PID but that is not the bot — stale lock, nothing to stop"
    fi
fi
say "ok  bot stopped"

# -- 6. move the live tree to the merged code -------------------------------- #
# Local-only commits stay reachable on a backup branch, not just in the reflog.
BACKUP="backup/pre-run8-switch-$(date +%Y%m%d-%H%M)"
git branch -f "$BACKUP" HEAD || die "could not create $BACKUP — bot is STOPPED. Restart it from the control panel; nothing else changed."
if [ -n "$(git status --porcelain)" ]; then
    git stash push -q -u -m "pre-run8-switch local docs $(date +%F_%H%M) (already on origin)" \
        || die "git stash failed — bot is STOPPED, code unchanged. Restart from the control panel."
    say "ok  stashed local changes (git stash list)"
fi
git checkout -q -B main origin/main \
    || die "git checkout failed — bot is STOPPED. Rollback: $ROLLBACK"
say "ok  main -> $(git rev-parse --short HEAD)   (old head kept on $BACKUP)"
say "    rollback: $ROLLBACK"

# -- 7. archive the old account's state -------------------------------------- #
# AFTER the stop (the shutdown save is in it) and AFTER the stash (an untracked
# runs/ directory created earlier would be swept into the stash); state/ is
# git-ignored, so it survived the checkout untouched.
ARCH=runs/pre-final-test-run-7/state-post-window
mkdir -p "$ARCH"
cp state/trades.jsonl state/equity_history.jsonl state/risk_state.json "$ARCH"/ 2>/dev/null || true
say "ok  archived state -> $ARCH (uncommitted; commit it with the pre-registration)"

# -- 8. tests ----------------------------------------------------------------- #
"$PY" -m pytest -q --no-header -p no:cacheprovider tests >/tmp/run8_switch_tests.txt 2>&1 \
    || { tail -15 /tmp/run8_switch_tests.txt; die "test suite failed on the merged code — bot is STOPPED. Rollback: $ROLLBACK"; }
say "ok  tests: $(tail -1 /tmp/run8_switch_tests.txt)"

# -- 9. fresh cycle (archives + clears state, preflight, relaunch) ----------- #
"$PY" scripts/fresh_cycle.py --yes || die "fresh_cycle failed — see its output above; the bot may be stopped."

# -- 10. verify --------------------------------------------------------------- #
# The day-0 anchor is a row dated today with basis 'late' OR 'close': the
# writer stamps 'close' for a tick inside the 16:00 ET hour and 'late' after
# it, and the v3 checker accepts either as the predecessor of day 1. It writes
# NO row on a weekend / exchange holiday — then the anchor is the first
# session's own predecessor and this check is skipped.
TODAY=$(TZ=America/New_York date +%F)
if [ "$ET_DOW" -gt 5 ]; then
    say "..  weekend: no day-0 row is written today — verify the anchor after the next session's close"
else
    i=0
    until grep -E "\"date\": \"$TODAY\".*\"basis\": \"(late|close)\"" state/equity_history.jsonl >/dev/null 2>&1; do
        i=$((i + 1))
        if [ "$i" -gt 36 ]; then
            [ "${FORCE_TIME:-0}" = 1 ] \
                && { say "WARN no day-0 row for $TODAY after 3 min (FORCE_TIME: market holiday?) — check logs/bot.log"; break; } \
                || die "no basis late/close row for $TODAY after 3 min — the day-0 anchor is missing; check logs/bot.log (the bot IS running on the new code)."
        fi
        sleep 5
    done
    say "ok  day-0 row: $(grep "\"date\": \"$TODAY\"" state/equity_history.jsonl | tail -1 | cut -c1-160)"
fi
grep -q 'BOOK BETA CAP vs hedge arm line' logs/bot.log \
    && say "ok  new code is running (config-load line present)" \
    || say "WARN new-code config line not seen yet in logs/bot.log — check 'Starting orchestrator'."
say "== DONE. Account $NEW_ACCT, day-0 equity \$$NEW_EQ."
say "   Record in the contract's Config row: merge SHA $(git rev-parse HEAD)"
say "   Then flip the header to Pre-registered, commit, push (docs/RUN8_SWITCH.md section 0 step 4)."
