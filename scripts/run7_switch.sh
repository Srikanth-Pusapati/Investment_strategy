#!/bin/sh
# Run-7 day-0 switch — run from the LIVE tree, AFTER 16:00 ET on the switch day.
#
#   DRY_RUN=1 sh scripts/run7_switch.sh    # every check, changes nothing
#   sh scripts/run7_switch.sh              # the switch
#
# What it does, in order (docs/RUN7_SWITCH.md section 0):
#   checks   market closed · merged change-set is on origin/feature/preview ·
#            .env has the twelve run-7 key lines once each · the keys in .env
#            belong to a NEW paper account with no positions and no orders
#   archive  old state -> runs/pre-final-test-run-6/state-post-window/
#   stop     the running bot (SIGTERM the lock-holding pid, wait <= 30 s)
#   code     stash the live tree's local docs, move feature/preview to origin's
#   tests    full suite must pass
#   start    scripts/fresh_cycle.py --yes (archives + clears state, relaunches)
#   verify   a basis='late' day-0 row for today + the new-code config line
# It never edits .env, never touches the kill switch, never force-pushes.
# FORCE_TIME=1 skips the after-the-bell check (weekends / holidays).
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"
PY="$ROOT/.venv/bin/python"
DRY=${DRY_RUN:-0}
say() { printf '%s\n' "$*"; }
die() { printf 'ABORT: %s\n' "$*" >&2; exit 1; }

say "== run-7 switch ($( [ "$DRY" = 1 ] && echo DRY RUN || echo LIVE )) in $ROOT"

# -- 1. after the bell ------------------------------------------------------- #
ET_HM=$(TZ=America/New_York date +%H%M); ET_DOW=$(TZ=America/New_York date +%u)
if [ "${FORCE_TIME:-0}" != 1 ] && [ "$ET_DOW" -le 5 ] && [ "$ET_HM" -lt 1600 ]; then
    die "it is $ET_HM ET on a weekday — run after 16:00 ET (the day-0 'late' row needs a post-bell restart)."
fi
say "ok  time: $ET_HM ET (dow $ET_DOW)"

# -- 2. the merged change-set is on origin ---------------------------------- #
git fetch -q origin feature/preview || die "git fetch failed"
for marker in CORE_FILL_BETA_CLAMP HEDGE_STARVED_CORE_TRIM REGIME_FALLING_TAPE_CAP; do
    git show origin/feature/preview:investment_strategy/config.py 2>/dev/null \
        | grep -q "$marker" \
        || die "origin/feature/preview lacks $marker — merge PR #59 and the A+ PR into feature/preview first."
done
git show origin/feature/preview:scripts/run7_switch.sh >/dev/null 2>&1 \
    || die "origin/feature/preview lacks scripts/run7_switch.sh — the A+ PR is not merged."
OLD_SHA=$(git rev-parse HEAD); NEW_SHA=$(git rev-parse origin/feature/preview)
say "ok  code: origin/feature/preview = $NEW_SHA (live tree now at $OLD_SHA)"

# -- 3. .env carries the run-7 block ---------------------------------------- #
[ -f .env ] || die ".env not found"
for k in HEDGE_BETA_ASSUMED PROXY_PUT_PREFER_MONTHLY OPTION_STRIKE_SNAP \
         OPTION_STRIKE_MAX_MONEYNESS_PCT REGIME_LOOSEN_MIN_CYCLES \
         TOPUP_MIN_CONVICTION_DELTA HEDGE_UNWIND_MIN_CYCLES \
         CORE_FILL_BETA_CLAMP HEDGE_STARVED_CORE_TRIM HEDGE_STARVED_TRIM_MAX_PCT \
         REGIME_FALLING_TAPE_CAP LEDGER_RESTATE_AT_FILL; do
    n=$(grep -cE "^${k}=" .env || true)
    [ "$n" = 1 ] || die ".env has $n line(s) for $k (need exactly 1) — paste the docs/RUN7_SWITCH.md 1c block."
done
if grep -qE '^EXPECTANCY_GATE(_ENABLED)?=on' .env; then
    die ".env has the expectancy gate ON — adaptive loops must be off (contract validity)."
fi
say "ok  .env: twelve run-7 key lines present once each; expectancy gate off"

# -- 4. the keys belong to a NEW, empty paper account ------------------------ #
OLD_ACCT=$(sed 's/^paper://' state/account.json 2>/dev/null || true)
ACCT_LINE=$("$PY" - <<'PYEOF'
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
) || die "could not read the Alpaca account with the keys in .env"
set -- $ACCT_LINE
NEW_ACCT=$1; NEW_EQ=$2; NPOS=$3; NORD=$4
[ "$NEW_ACCT" != "$OLD_ACCT" ] || die ".env still holds the OLD account ($OLD_ACCT) — paste the fresh paper account's keys."
[ "$NPOS" = 0 ] && [ "$NORD" = 0 ] || die "account $NEW_ACCT has $NPOS position(s) / $NORD open order(s) — the contract needs a fresh account."
say "ok  account: $NEW_ACCT equity \$$NEW_EQ, 0 positions, 0 orders (old: ${OLD_ACCT:-none})"

if [ "$DRY" = 1 ]; then
    say "== DRY RUN: all checks passed. Would now: archive state, stop the bot, stash local docs,"
    say "   move feature/preview $OLD_SHA -> $NEW_SHA, run the tests, fresh_cycle, verify the late row."
    exit 0
fi

# -- 5. archive the old account's state -------------------------------------- #
ARCH=runs/pre-final-test-run-6/state-post-window
mkdir -p "$ARCH"
cp state/trades.jsonl state/equity_history.jsonl state/risk_state.json "$ARCH"/ 2>/dev/null || true
say "ok  archived state -> $ARCH (uncommitted; commit it with the pre-registration)"

# -- 6. stop the bot --------------------------------------------------------- #
PID=$(cat state/bot.lock 2>/dev/null || true)
if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
    say "..  stopping bot pid $PID"
    kill -TERM "$PID"
    i=0
    while kill -0 "$PID" 2>/dev/null; do
        i=$((i + 1)); [ "$i" -le 60 ] || die "bot pid $PID did not exit in 30 s — nothing else was changed."
        sleep 0.5
    done
fi
say "ok  bot stopped"

# -- 7. move the live tree to the merged code -------------------------------- #
if [ -n "$(git status --porcelain)" ]; then
    git stash push -q -u -m "pre-run7-switch local docs $(date +%F_%H%M) (already on origin)"
    say "ok  stashed local changes (git stash list)"
fi
git checkout -q -B feature/preview origin/feature/preview
say "ok  feature/preview -> $(git rev-parse --short HEAD)   (rollback: git checkout -B feature/preview $OLD_SHA)"

# -- 8. tests ----------------------------------------------------------------- #
"$PY" -m pytest -q --no-header -p no:cacheprovider tests >/tmp/run7_switch_tests.txt 2>&1 \
    || { tail -15 /tmp/run7_switch_tests.txt; die "test suite failed on the merged code — bot is STOPPED; roll back with the command above and restart from the control panel."; }
say "ok  tests: $(tail -1 /tmp/run7_switch_tests.txt)"

# -- 9. fresh cycle (archives + clears state, preflight, relaunch) ----------- #
"$PY" scripts/fresh_cycle.py --yes || die "fresh_cycle failed — see its output above; the bot may be stopped."

# -- 10. verify --------------------------------------------------------------- #
TODAY=$(TZ=America/New_York date +%F)
i=0
until grep -q "\"date\": \"$TODAY\".*\"basis\": \"late\"" state/equity_history.jsonl 2>/dev/null \
   || grep -q "\"basis\": \"late\".*\"date\": \"$TODAY\"" state/equity_history.jsonl 2>/dev/null; do
    i=$((i + 1)); [ "$i" -le 36 ] || die "no basis='late' row for $TODAY after 3 min — the day-0 anchor is missing; check logs/bot.log."
    sleep 5
done
say "ok  day-0 row: $(grep "$TODAY" state/equity_history.jsonl | tail -1)"
grep -q 'BOOK BETA CAP vs hedge arm line' logs/bot.log \
    && say "ok  new code is running (config-load line present)" \
    || say "WARN new-code config line not seen yet in logs/bot.log — check 'Starting orchestrator'."
say "== DONE. Account $NEW_ACCT, day-0 equity \$$NEW_EQ."
say "   Record in the contract's Config row: merge SHA $(git rev-parse HEAD)"
say "   Then flip the header to Pre-registered, commit, push (docs/RUN7_SWITCH.md section 0 step 4)."
