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
   - **Unusual options flow** on the most-active names via Polygon *(needs `POLYGON_API_KEY`)*

   Candidates are deduped, confidence-weighted, ranked, and capped
   (`MAX_DISCOVERED_CANDIDATES`); each then flows through the exact same signal →
   Claude → **RiskManager** → execute path below. Gate it with `SCREENER_ENABLED`.
1. **Signals** ([signals/](investment_strategy/signals/)) — pluggable data
   sources, each normalized to a `Signal`:
   - **Fundamentals** (EBITDA, margins, leverage) via yfinance — works with no API key
   - **News & sentiment** via Alpaca news + Finnhub sentiment *(keyword fallback)*
   - **Insider trades** (Form 4 buys/sells) via Finnhub *(needs `FINNHUB_API_KEY`)*
   - **Options flow** (unusual call/put activity) via Polygon *(needs `POLYGON_API_KEY`)*
   - **Congress/senator trades** via Quiver *(needs `QUIVER_API_KEY`; lag ~45d)*
   - **Macro** (rates, unemployment, yield curve) via FRED *(needs `FRED_API_KEY`)*
2. **Decision** ([decision/](investment_strategy/decision/)) — calls the Claude
   API with the signal bundle **+ benchmark-relative performance + any external
   (Robinhood) holdings** and gets back structured buy/sell/hold proposals — for
   **equities, ETFs, or defined-risk options**. Claude proposes; it never executes.
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
5. **Monitor** ([monitor/](investment_strategy/monitor/)) — a fast loop that
   enforces an account-wide emergency flatten and ratcheting trailing stops.

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

## Run

```bash
python -m investment_strategy
```

It starts the orchestrator: the watchdog ticks every `MONITOR_INTERVAL_SECONDS`,
and a full decision cycle runs every `DECISION_INTERVAL_SECONDS` while the market
is open. Set the candidate universe with `WATCHLIST=AAPL,MSFT,NVDA` (current
holdings are always re-evaluated too).

## Trade dashboard

Every order the bot executes is appended to a local **trade ledger**
(`state/trades.jsonl`, gitignored) with its full decision context — executed
date, volume, cost invested, conviction, the planned exit (take-profit /
stop-loss levels), and Claude's rationale + key signals (the *reason behind the
purchase*). Exits here are **price-triggered brackets, not calendar dates**, so
the dashboard shows the TP/SL targets as the "assumed sell" levels.

Render a self-contained HTML dashboard (summary cards, capital-per-symbol and
cumulative-invested charts, and a full trade table — no external/JS dependencies):

```bash
python -m investment_strategy.dashboard --open      # write dashboard.html + open it
python -m investment_strategy.dashboard --no-live   # offline (skip live-price P/L)
```

With Alpaca keys present it best-effort enriches open positions with the live
price to show unrealized P/L; without them it still renders the full ledger.

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

Going live is a deliberate, guarded change:
`TRADING_MODE=live` **and** `ALPACA_BASE_URL=https://api.alpaca.markets` **and**
`KILL_SWITCH=off`, using your live Alpaca keys.

## Status / next steps

Foundation + multi-instrument trading, benchmark tracking, smart-money signals,
and survival-first sizing are in place; the risk core (hard caps, vol sizing,
options premium gate) is unit-tested. Before real use:
- Get the optional keys (`FINNHUB_API_KEY`, `POLYGON_API_KEY`) to activate the
  insider + options-flow + model-sentiment signals.
- Executed orders are now persisted to a trade ledger (`state/trades.jsonl`) and
  visualized via `python -m investment_strategy.dashboard`; extend it to also log
  *rejected* `RiskDecision`s for a full audit trail.
- **Backtest** the decision logic against historical data before trusting sizing.
- Options: start with `OPTIONS_ENABLED=off`, paper-test the equity loop first,
  then enable with a small `MAX_OPTION_PREMIUM_PCT`.
- The options-flow provider is a coarse call/put-volume proxy — upgrade to a real
  sweep/block feed (Unusual Whales / CBOE) for genuine flow detection.
```
