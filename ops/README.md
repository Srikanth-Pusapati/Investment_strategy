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
| `LOG_DIR` | `logs` | Rotating file log directory (`bot.log`, 5 MB × 5). `""` disables. |

## What fires when

- **Process dies / host sleeps** → pings stop → external monitor pages (nothing
  local can page you from a dead process — that's why the monitor is external).
- **Process wakes from a dark gap** (sleep/suspend/clock jump) → CRITICAL log +
  alert email; the watchdog re-checks every position on its next tick and the
  decision cycle reconciles pending orders before trading.
- **Reconcile finds a mismatch** (rejected/partial order the ledger recorded as
  intent) → new buys halt + page; you verify positions against the broker, then
  `rm state/KILL` to resume.
