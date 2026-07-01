# Investment Strategy — Claude-driven trading bot

An automated equities trading system where **Claude makes the decisions** and
**Alpaca executes the orders**, with a deterministic risk layer and an always-on
watchdog standing between the model and your money.

> ⚠️ **This trades real money in `live` mode and can lose it.** It ships
> defaulted to **paper trading**. Read the safety section before changing that.
> Nothing here is financial advice; you are responsible for every order it places.

## How it works

Five loosely-coupled stages:

```
SCAN ──▶ SIGNALS ──▶ DECISION ──▶ EXECUTE ──▶ MONITOR
find     ingest      Claude +     Alpaca      24/5 watchdog
names    data        risk caps    brackets    stops / exits
```

0. **Scan** ([screener/](investment_strategy/screener/)) — the discovery layer.
   Before any per-symbol work, it scans the market for **smart-money** activity and
   surfaces NEW candidate tickers (on top of `WATCHLIST` + current holdings), so buy
   ideas can originate from the market instead of a hand-typed list:
   - **Insider Form-4 cluster buys** across all issuers via SEC EDGAR *(free)*
   - **Congressional buying** across all tickers via Quiver *(needs `QUIVER_API_KEY`)*
   - **WallStreetBets mention surges** (retail momentum) via Quiver *(opt-in: add
     `wallstreetbets` to `SCREENER_SOURCES`)*
   - **Unusual options flow** on the most-active names via Polygon *(needs `POLYGON_API_KEY`)*

   Candidates are deduped, confidence-weighted, ranked, and capped
   (`MAX_DISCOVERED_CANDIDATES`); each then flows through the exact same signal →
   Claude → **RiskManager** → execute path below. Gate it with `SCREENER_ENABLED`.
1. **Signals** ([signals/](investment_strategy/signals/)) — pluggable data
   sources, each normalized to a `Signal`:
   - **Fundamentals** (EBITDA, margins, leverage) via yfinance — works with no API key
   - **Technicals** (RSI, MACD, trend vs SMA50/200) via yfinance — no API key
   - **News & sentiment** via Alpaca news + Finnhub sentiment *(keyword fallback)*
   - **Insider trades** (Form 4 buys/sells) via Finnhub *(needs `FINNHUB_API_KEY`)*
   - **Options flow** (unusual call/put activity) via Polygon *(needs `POLYGON_API_KEY`)*
   - **Congress/senator trades** via Quiver *(needs `QUIVER_API_KEY`; lag ~45d)*
   - **Off-exchange / dark-pool short volume** via Quiver *(needs `QUIVER_API_KEY`;
     ~1d lag — the most timely Quiver signal)*
   - **Macro** (rates, unemployment, yield curve) via FRED *(needs `FRED_API_KEY`)*

   Quiver-backed sources share one [per-cycle-cached client](investment_strategy/signals/quiver_client.py)
   so each live feed is pulled once per cycle and serves both the signal and scan
   layers (no double-pull, with 429 backoff) — adding more Quiver datasets is cheap.
2. **Decision** ([decision/](investment_strategy/decision/)) — calls the Claude
   API with the signal bundle **+ benchmark-relative performance + any external
   (Robinhood) holdings** and gets back structured buy/sell/hold proposals — for
   **equities, ETFs, or defined-risk options**. Claude proposes; it never executes.
   The decision prompt also carries a **track record** ([attribution.py](investment_strategy/attribution.py)):
   the bot reconstructs closed round-trips from the ledger, scores each entry
   signal source by realized win-rate + average P&L, and feeds that back so Claude
   weights conviction toward the signals that have actually predicted P&L — a
   reflection loop that also tells you which paid data sources are worth keeping.
3. **Risk** ([risk.py](investment_strategy/risk.py)) — the safety core. Every
   proposal passes through deterministic hard caps the model **cannot** override:
   max position size, per-symbol exposure, daily-loss halt, cash buffer, kill
   switch — plus **vol-targeted, fractional-Kelly sizing** (survival-first: takes
   the smaller of what Claude wants and what the vol budget allows). Options are
   bounded by a defined-risk premium cap.
4. **Execute** ([execution/](investment_strategy/execution/)) — Alpaca orders:
   market/limit/stop, **fractional** (dollar-notional), **bracket** (entry+stop+
   take-profit), **ladders** (scale-in/out), and **defined-risk options** (long
   calls/puts, verticals).
5. **Monitor** ([monitor/](investment_strategy/monitor/)) — a fast always-on loop
   (its own thread, never gated) that enforces an account-wide emergency flatten, a
   **latched equity-floor halt**, hard stops/take-profits for fractional positions,
   **scale-out** (sell part at target, trail the rest), a **time-stop** (recycle
   dead/flat capital), and ratcheting **trailing stops** — and pages you
   ([notify.py](investment_strategy/notify.py)) on any CRITICAL it can't self-heal.

### Benchmark goal
The bot targets **excess return over SPY/QQQ**, not raw return.
[benchmark.py](investment_strategy/benchmark.py) computes your return vs. the
benchmark + information ratio each cycle and feeds it to Claude, so decisions are
explicitly selection-driven. Beating the index by 10–20%/yr is top-decile fund
performance — treat it as a target, not a promise; sizing prioritizes survival.

### Instruments & order types
| Capability | Where |
|---|---|
| Stocks, ETFs | `OrderRequest` (equity) |
| Fractional (dollar amount) | `OrderRequest.notional` *(no bracket — watchdog covers the stop)* |
| Limit / stop / stop-limit | `OrderRequest.order_type` |
| Bracket (OCO exits) | `submit_from_decision` / bracket fields |
| Ladders (scale in/out) | `AlpacaClient.submit_ladder` |
| Defined-risk options | `OptionsHelper` + `submit_option_legs` *(gated `OPTIONS_ENABLED`)* |

### Robinhood (read-only, optional) — via official Agentic MCP
With `ROBINHOOD_ENABLED=on`, [portfolio/robinhood.py](investment_strategy/portfolio/robinhood.py)
connects to Robinhood's **official Agentic Trading MCP** endpoint (OAuth) as a
plain MCP client and calls **read tools only** to import positions for context —
Claude sees what you hold, but **never gets the trade tool**; all execution stays
on Alpaca behind the RiskManager.

Two caveats:
- The agentic MCP is scoped to a **separate, dedicated RH account** (the one you
  fund for the agent), **not your main portfolio**.
- On first run, leave `ROBINHOOD_POSITIONS_TOOL` blank — the reader connects,
  **logs the MCP's available tools**, and returns nothing; set that var to the
  real holdings-tool name, then re-run. (Verify the endpoint + OAuth flow on
  Robinhood's own docs.)

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then fill it in
```

Get **paper** API keys from [app.alpaca.markets](https://app.alpaca.markets)
(paper and live use different keys) and an Anthropic key from
[console.anthropic.com](https://console.anthropic.com). Put them in `.env`.
`.env` is gitignored — never commit it.

## Run the system

Once `.env` has your **paper** Alpaca keys + an Anthropic key (see Setup), start
the bot from the repo root with the venv active:

```bash
# 1. (optional) prove the config loads and the interlock is happy
python -c "from investment_strategy.config import load_config; load_config(); print('config OK')"

# 2. (optional) narrow the universe — discovery is on by default, so this is only
#    needed if you want to force-include names. Holdings are always re-evaluated.
export WATCHLIST=AAPL,MSFT,NVDA        # or leave unset for pure discovery

# 3. start the orchestrator (Ctrl-C to stop; it's a long-lived process)
python -m investment_strategy
```

What that does — the [orchestrator](investment_strategy/orchestrator.py) runs two
timed loops on their own threads:

| Loop | Cadence | Job |
|---|---|---|
| **Watchdog** (safety) | `MONITOR_INTERVAL_SECONDS` (30s) | emergency flatten, equity-floor halt, hard stops/takes, trailing stops, **scale-out**, **time-stop** — never gated, always running |
| **Decision** (slow) | `DECISION_INTERVAL_SECONDS` (900s), market hours only | scan → signals → Claude → **RiskManager** → orders; regime trim + thesis-decay exits |

It logs each cycle to the console (regime read, proposals, risk verdicts, fills).
Leave it running; everything it does is captured in the ledger + dashboard below.

**Halt it any time without a restart:** `touch state/KILL` (blocks *new* buys; the
watchdog can still close). Delete the file to resume. `KILL_SWITCH=on` in `.env`
does the same from boot. A latched equity-floor halt clears by deleting
`state/risk_state.json`.

### Before live money — prove the edge, then flip the switch

```bash
# Backtest the SAME risk knobs (sizing + exits) on price history — no keys needed
python -m investment_strategy.backtest          # demo run + summary metrics

# Once you have paper round-trips, see which paid data (if any) is worth buying
python -m investment_strategy.subscriptions     # SUBSCRIBE / KEEP MEASURING / …
```

The [backtest harness](investment_strategy/backtest.py) replays entry signals
through the real `RiskManager` and the same stop/take/scale-out/trailing/time-stop
lifecycle the watchdog runs, and reports return, max drawdown, Sharpe, win rate,
profit factor, and excess vs a benchmark — so you tune `KELLY_FRACTION`,
`TARGET_ANNUAL_VOL_PCT`, and the stop/take levels on evidence. The
[subscription evaluator](investment_strategy/subscriptions.py) refuses to
recommend paying for a data source until its *free* signals show a measured edge
in your ledger ("don't pay before you can measure it helps").

## Visualize the executions — the dashboard

Every order the bot places is appended to a local **trade ledger**
(`state/trades.jsonl`, gitignored) with its full decision context — executed
date, volume, cost invested, conviction, the planned exit (take-profit /
stop-loss levels), the exit reason on closes (`stop` / `take` / `scale` / `trail`
/ `time` / `thesis_decay` / `regime_trim` / `flatten`), and Claude's rationale +
key signals (the *reason behind the purchase*). Exits are **price-triggered
brackets, not calendar dates**, so the dashboard shows the TP/SL targets as the
"assumed sell" levels.

Render a self-contained HTML dashboard — summary cards, a **live account panel**
(real Alpaca equity / today's P&L / unrealized P&L / total return, net of
deposits), capital-per-symbol and cumulative-invested charts, and a full trade
table — with **no external/JS dependencies** (opens offline, prints, emails):

```bash
python -m investment_strategy.dashboard --open       # write dashboard.html + open it
python -m investment_strategy.dashboard -o out.html  # custom output path
python -m investment_strategy.dashboard --no-live    # offline (skip live-price P/L)
```

With Alpaca keys present it best-effort enriches open positions with the live
price to show unrealized P/L and adds the live account panel; without them it
still renders the full ledger offline.

**Keep it fresh automatically:** set `DASHBOARD_FILE=dashboard.html` in `.env` and
the orchestrator regenerates it after every decision cycle while it runs — open
the file in a browser and refresh to watch executions land in near-real-time.
Nothing to serve; it's a static file.

## Safety — read this

- **Paper first.** `TRADING_MODE=paper` (default) trades fake money against the
  same API. Run it for weeks and inspect every decision before considering live.
- **The paper/live interlock** in [config.py](investment_strategy/config.py)
  refuses to start if `TRADING_MODE` and `ALPACA_BASE_URL` disagree, so a
  half-edited `.env` can't silently trade real money.
- **Kill switch.** `KILL_SWITCH=on` blocks all new orders immediately. The
  watchdog can still *close* positions — reducing risk is never gated.
- **Hard risk limits** live in `.env` and are enforced in code, not by the LLM.
  Tune `MAX_POSITION_PCT`, `MAX_DAILY_LOSS_PCT`, etc. to your tolerance.
- **Capital-preservation stack** (all deterministic, LLM cannot override):
  percentage equity floor, peak-to-trough drawdown halt, no-leverage gross cap,
  sector-concentration cap, per-trade `MAX_TRADE_RISK_PCT`, `MIN_CONVICTION` floor,
  PDT guard, earnings blackout, a market-regime size multiplier that **sizes down
  when its data feed is degraded** (never blindly full-size), and the watchdog
  guards above. Small live accounts run mostly **fractional** — those carry no
  exchange bracket, so the watchdog stop is their guard (an overnight-gap residual
  is bounded, not removable).
- **Alerts.** Set `ALERTS_ENABLED=on` + `ALERT_EMAIL_TO` (Gmail SMTP) and/or
  `ALERT_WEBHOOK_URL` to get paged on a naked position or a latched halt.

Going live is a deliberate, guarded change: **run the backtest first**, then
`TRADING_MODE=live` **and** `ALPACA_BASE_URL=https://api.alpaca.markets` **and**
`KILL_SWITCH=off`, using your live Alpaca keys. Start with $100–$1000, not your
savings — scale up only from a real track record.

## Status / next steps

Foundation + multi-instrument trading, benchmark tracking, smart-money signals,
and survival-first sizing are in place; the risk core (hard caps, vol sizing,
options premium gate) is unit-tested. Before real use:
- Get the optional keys (`FINNHUB_API_KEY`, `POLYGON_API_KEY`) to activate the
  insider + options-flow + model-sentiment signals.
- Executed orders are persisted to a trade ledger (`state/trades.jsonl`) and
  visualized via `python -m investment_strategy.dashboard`; extend it to also log
  *rejected* `RiskDecision`s for a full audit trail.
- **Backtest** ([backtest.py](investment_strategy/backtest.py)) replays sizing +
  exits on price history so you can tune `KELLY_FRACTION` / `TARGET_ANNUAL_VOL_PCT`
  / stops before trusting them; next step is wiring real Alpaca history + a
  ledger-sourced signal stream into it.
- Options: start with `OPTIONS_ENABLED=off`, paper-test the equity loop first,
  then enable with a small `MAX_OPTION_PREMIUM_PCT`.
- The options-flow provider is a coarse call/put-volume proxy — upgrade to a real
  sweep/block feed (Unusual Whales / CBOE) for genuine flow detection.
```
