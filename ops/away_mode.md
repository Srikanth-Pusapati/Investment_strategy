# Away-mode runbook (2026-08-13 → ~2026-08-27)

The operator is away; a scheduled Claude session runs each market weekday
evening and follows this file. The bot trades PAPER account PA3HBZ8ODAMD.
Everything here is subordinate to one rule: **the risk layer and watchdog are
never loosened unattended.**

## Standing context

- Bot: `python -m investment_strategy` under the control-panel spawn
  (`state/bot.lock` holds the pid). Restart ONLY via
  `curl -X POST http://127.0.0.1:8787/api/restart`.
- Self-healing: `ops/deadman.py` (launchd, 5-min cadence, market hours)
  auto-restarts a dead/wedged bot through the panel and pages by email.
  healthchecks.io pages externally if the whole machine goes dark.
- Status page the operator checks from their phone (update EVERY session):
  artifact URL `https://claude.ai/code/artifact/94bb36b9-af2e-423b-a524-3862e9912da8`
  — rewrite `ops/status_page.html` with fresh values, then republish passing
  that URL as the `url` parameter (a new session mints a new URL otherwise).
- Ledger gotcha: option trips in `state/trades.jsonl` carry the UNDERLYING
  ticker + `instrument: "option"` — never grep for OCC-shaped symbols.

## Daily session checklist

1. **Health**: pid in `state/bot.lock` alive and is the bot; `state/last_tick.stamp`
   fresh (<5 min during market hours); tail `logs/deadman.log`;
   `state/robinhood_health.json` (auth_dead=true is DEGRADED not broken — bot
   continues without RH signals; do not attempt OAuth re-login headless);
   does `state/KILL` exist?
2. **Incidents**: grep the day's log for `CRITICAL|ERROR|halt|FAILED`;
   grep `BEARISH FUNNEL` terminal stages (`-> IGNORED` = schema violation,
   escalate on the status page); grep `Option exit|OPTION close`.
3. **Numbers**: equity + day move (`state/equity_history.jsonl`, live via
   Alpaca `/v2/account` with `.venv/bin/python` + dotenv); closed-trip
   expectancy and win rate from `trades.jsonl` (window = since Aug 3);
   open option groups from `/v2/positions` (OCC symbols, net uP/L).
4. **Archive**: `git add logs/*_*_*.log` for any completed day, commit
   (`Archive <day> daily log`), push `feature/preview`. Keeps GitHub current
   so phone-side claude.ai/code sessions can read the latest state.
5. **Status page**: update `ops/status_page.html` (banner verdict, as-of
   stamp, Today, Window, Open option risk, Watch items) and republish to the
   artifact URL above. Banner: `ok` = healthy; `warn` = degraded signal/open
   risk worth watching; `crit` = halt latched, bot down >1h, or unprotected
   position — say plainly what happened and what was done.
6. **Fix authority (bugs only)**: crash loops, close-path failures, watchdog
   misfires, API-shape breaks. Full test suite must pass; commit/push to
   `feature/preview` (PR into main; the operator merges on return); restart
   via the panel. NO strategy/prompt/gate/knob changes, with one exception ↓.
7. **KILL file**: if `state/KILL` exists, reconcile broker vs ledger
   (positions + orders via Alpaca API). Clear it ONLY when they verifiably
   match; otherwise leave halted, set banner `crit`.

## Friday 2026-08-14 — window-end session (once)

Run the full observation-window evaluation (criteria in the
`aug-observation-window` memory: capture-ratio vs 0.29 July baseline,
expectancy vs −$133, funnel terminal stages, NAME FALLING latency,
expectancy-gate fires). Publish the verdict on the status page. Then ship the
two agreed roadmap items with tests:
1. **Put liquidity fallback** — when a put-eligible name's chain fails the
   OI≥100 floor, express the bearish read through a liquid proxy chain
   (sector ETF / index puts, reusing the PR #47 hedge plumbing).
2. **Entry-side leg-merge guard** — reject a new option structure on an
   underlying+expiry whose merged group would exceed 4 legs (Alpaca MLEG cap;
   the close-side chunking from PR #52 stays as the backstop).
Commit, push, PR, restart via panel. If the day produced an incident or the
suite fails, ship nothing — note it on the status page and stop.

## Hard guardrails

- Never flatten the book, never trade manually, never touch `.env`.
- Never disable or widen the watchdog, brakes, or risk gates.
- Never merge PRs; never force-push; never rewrite git history.
- If the panel AND a direct spawn both fail, set the banner `crit` and stop —
  the healthchecks email is the operator's signal.
