# Ops — running the bot unattended (goGA GA-2.1 / GA-2.2 / GA-2.4)

The account-level brakes (daily-loss flatten, equity floor, drawdown halt) live
**inside the bot process**. A process that isn't running protects nothing, and
fractional/core positions carry no exchange-side stop. So unattended operation
needs three layers, all shipped here:

| Layer | What | Where |
|---|---|---|
| Supervisor | restart on crash/reboot | `ops/launchd/*.plist` (mac) or `ops/Dockerfile` + `--restart=always` (VPS) |
| Dead-man switch | external page when the loop goes silent | `HEARTBEAT_URL` env + a [healthchecks.io](https://healthchecks.io)-style monitor |
| Log persistence | post-mortems without terminal scrollback | rotating `logs/bot.log` (on by default) |

## Substrate choice (do this before the track-record window opens)

A **sleeping laptop is not a substrate**: launchd survives crashes but not
lid-close, and a monitor that pages on every lid-close trains you to ignore
pages. Use one of:

- **Small VPS ($5–10/mo)** running the Docker image — preferred for the
  3-month record window.
- **Always-on Mac**: `sudo pmset -a sleep 0 displaysleep 10`, then the launchd
  plist.

## New environment knobs

| Env | Default | Meaning |
|---|---|---|
| `HEARTBEAT_URL` | `""` (off) | Dead-man ping URL, GET-pinged every watchdog tick (~30s) **only while the main loop is also fresh** — either thread dying silences pings and the external monitor pages. Create a check at healthchecks.io with a ~2–5 min grace period and paste its ping URL here. |
| `RECONCILE_HALT` | `on` | A reject/partial found at reconcile = ledger/broker divergence → new buys halt via the kill-switch file (`state/KILL`, reason appended inside). **Delete the file to acknowledge and resume.** Sells and the watchdog are never gated. |
| `LOG_DIR` | `logs` | File log directory. `""` disables. See "Daily log archive" below. |

## Daily log archive (backward analysis)

`logs/bot.log` is the LIVE file. At local midnight — or on the first start of a
new day (rollover time is computed from the file's mtime, so a bot that isn't
running at midnight still rotates at the next startup) — the previous day's
content rotates to a per-day file named like `logs/Jul_06_2026.log`
(`__main__.dated_log_name`). These dated files are **committed to the repo**
(`.gitignore` re-includes `logs/*_*_*.log`; `bot.log` stays ignored) so any
trading day can be replayed later when tuning the model/risk knobs:

```sh
git add logs/*_*_*.log && git commit -m "log archive"   # end-of-day habit, or let a cron do it
grep -E "BUY|SELL|REJECT|watchdog" logs/Jul_06_2026.log # one day's trade story
```

Unlike a terminal copy/paste, the dated files carry full `YYYY-MM-DD HH:MM:SS`
timestamps and untruncated rationales (the console view wraps and trims).

## What fires when

- **Process dies / host sleeps** → pings stop → external monitor pages (nothing
  local can page you from a dead process — that's why the monitor is external).
- **Process wakes from a dark gap** (sleep/suspend/clock jump) → CRITICAL log +
  alert email; the watchdog re-checks every position on its next tick and the
  decision cycle reconciles pending orders before trading.
- **Reconcile finds a mismatch** (rejected/partial order the ledger recorded as
  intent) → new buys halt + page; you verify positions against the broker, then
  `rm state/KILL` to resume.

## GA-2.3 — no more stop-less positions

| Env | Default | Meaning |
|---|---|---|
| `WHOLE_SHARES_ONLY` | `off` | ON: satellite buys floor to whole shares so EVERY entry rests an exchange-side GTC bracket; a budget under one share is rejected, never downgraded to an unprotected fractional; partial sells (scale-out, trim) round to whole shares too. OFF (default — 2026-07-05 solo/small-float decision): fractional sizing for a $100–1000 live float; the watchdog + account brakes enforce the stops, and max loss is bounded by the float. Set ON for the paper RECORD account and any $10k+ live account. |
| `CORE_STOP_PCT` | `15` | Standalone GTC stop protecting the core ETF this % under its average basis (whole-share part; the sub-share residual stays watchdog-guarded). `0` = off — that is the explicit written-acceptance path: broad-ETF gap risk accepted, dead-man paging is the compensating control. |

Re-validated 2026-07-04 under whole-share sizing: `--stress` all 5 brake checks
PASS; `--sweep-stops` at 20- and 55-day lookbacks keeps the vol 2σ family on
top — the live `VOL_STOPS` config stands.

## GA-2.8 — nightly state/ backup

`state/` (ledger, equity history = the track record, risk latch) is gitignored
by design; git is not its backup. Run `ops/backup_state.sh` nightly:

- mac: `cp ops/launchd/com.investment-strategy.backup.plist ~/Library/LaunchAgents/`
  (edit the `REPLACE_ME` paths), then `launchctl load` it. Runs 02:15 daily.
- VPS/cron: `15 2 * * * /path/to/ops/backup_state.sh`
- Off-machine: point `BACKUP_DIR` at an iCloud/Dropbox-synced folder, or set
  `BACKUP_RCLONE_REMOTE=remote:bucket` (requires rclone). Keeps the newest 30
  tarballs locally (`BACKUP_KEEP`).

## GA-2.7 — preflight breadth

`python -m investment_strategy.preflight` now also exercises Quiver, the
yfinance regime feed (the sector cap is blind in the same outage), the
Robinhood MCP token (when enabled), and **sends a real test alert** through the
configured sink (`--no-alert-test` to skip). Run it before market open — a
broken pager should be discovered by the test page, not by the incident.
