# Graph Report - /Users/spusapati/Personal/Investment_stratergy  (2026-07-02)

## Corpus Check
- 8 files · ~111,359 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 1186 nodes · 2861 edges · 56 communities (36 shown, 20 thin omitted)
- Extraction: 93% EXTRACTED · 7% INFERRED · 0% AMBIGUOUS · INFERRED: 195 edges (avg confidence: 0.59)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- [[_COMMUNITY_Backtest Harness|Backtest Harness]]
- [[_COMMUNITY_Screener Aggregator & Discovery|Screener Aggregator & Discovery]]
- [[_COMMUNITY_Watchdog Safety Loop|Watchdog Safety Loop]]
- [[_COMMUNITY_Risk Models & Tests|Risk Models & Tests]]
- [[_COMMUNITY_Robinhood MCP & Quiver Client|Robinhood MCP & Quiver Client]]
- [[_COMMUNITY_Config Loader & P&L|Config Loader & P&L]]
- [[_COMMUNITY_Earnings-Blackout Guard|Earnings-Blackout Guard]]
- [[_COMMUNITY_Alerting (emailwebhook)|Alerting (email/webhook)]]
- [[_COMMUNITY_Portfolio State|Portfolio State]]
- [[_COMMUNITY_Config & Data Models|Config & Data Models]]
- [[_COMMUNITY_Preflight Readiness Check|Preflight Readiness Check]]
- [[_COMMUNITY_WSB & GovContracts Signals|WSB & GovContracts Signals]]
- [[_COMMUNITY_Account-change Reset|Account-change Reset]]
- [[_COMMUNITY_Benchmark & Config|Benchmark & Config]]
- [[_COMMUNITY_Orchestrator & Core-Fill|Orchestrator & Core-Fill]]
- [[_COMMUNITY_Orchestrator Tests|Orchestrator Tests]]
- [[_COMMUNITY_Candidate  Discovery Signal|Candidate / Discovery Signal]]
- [[_COMMUNITY_Trading-Bot Architecture|Trading-Bot Architecture]]
- [[_COMMUNITY_Account P&L Dashboard|Account P&L Dashboard]]
- [[_COMMUNITY_Regime Filter|Regime Filter]]
- [[_COMMUNITY_Signal Attribution & Reflection|Signal Attribution & Reflection]]
- [[_COMMUNITY_Technical Signals (RSIMACD)|Technical Signals (RSI/MACD)]]
- [[_COMMUNITY_Beat-the-Index Roadmap (Todo-3)|Beat-the-Index Roadmap (Todo-3)]]
- [[_COMMUNITY_Track-record Lessons|Track-record Lessons]]
- [[_COMMUNITY_Alpaca Client|Alpaca Client]]
- [[_COMMUNITY_Decision Engine (Claude)|Decision Engine (Claude)]]
- [[_COMMUNITY_Options Helper (OCC)|Options Helper (OCC)]]
- [[_COMMUNITY_Subscription ROI Gate|Subscription ROI Gate]]
- [[_COMMUNITY_Order Submission (notionalbracket)|Order Submission (notional/bracket)]]
- [[_COMMUNITY_Trade Ledger|Trade Ledger]]
- [[_COMMUNITY_Benchmark Tracker (info ratio)|Benchmark Tracker (info ratio)]]
- [[_COMMUNITY_Risk Decision & Equity Path|Risk Decision & Equity Path]]
- [[_COMMUNITY_EDGAR Insider Signal|EDGAR Insider Signal]]
- [[_COMMUNITY_Account Snapshot|Account Snapshot]]
- [[_COMMUNITY_News Sentiment Signal|News Sentiment Signal]]
- [[_COMMUNITY_Fake Broker (tests)|Fake Broker (tests)]]
- [[_COMMUNITY_Insider-feed Screener|Insider-feed Screener]]
- [[_COMMUNITY_Congress Signal|Congress Signal]]
- [[_COMMUNITY_Fundamentals Signal|Fundamentals Signal]]
- [[_COMMUNITY_Off-exchange Short Signal|Off-exchange Short Signal]]
- [[_COMMUNITY_Quiver Access Probe|Quiver Access Probe]]
- [[_COMMUNITY_AlerterState glue|Alerter/State glue]]
- [[_COMMUNITY_Quiver Schema Probe|Quiver Schema Probe]]
- [[_COMMUNITY_Options Leg Submission|Options Leg Submission]]
- [[_COMMUNITY_Fake Ledger (tests)|Fake Ledger (tests)]]
- [[_COMMUNITY_Fake Watchdog (tests)|Fake Watchdog (tests)]]
- [[_COMMUNITY_Package Entry|Package Entry]]
- [[_COMMUNITY_Multi-User North Star|Multi-User North Star]]
- [[_COMMUNITY_Multi-Tenant & Legal Gate|Multi-Tenant & Legal Gate]]
- [[_COMMUNITY_OptionLegRequest|OptionLegRequest]]
- [[_COMMUNITY_datetime|datetime]]
- [[_COMMUNITY_Path|Path]]
- [[_COMMUNITY_misc|misc]]
- [[_COMMUNITY_misc|misc]]
- [[_COMMUNITY_misc|misc]]
- [[_COMMUNITY_misc|misc]]

## God Nodes (most connected - your core abstractions)
1. `Config` - 64 edges
2. `PortfolioState` - 52 edges
3. `_rm()` - 51 edges
4. `_limits()` - 49 edges
5. `_account()` - 49 edges
6. `QuiverClient` - 41 edges
7. `TradeProposal` - 39 edges
8. `AlpacaClient` - 38 edges
9. `AccountSnapshot` - 37 edges
10. `_buy()` - 37 edges

## Surprising Connections (you probably didn't know these)
- `Market-regime Risk Multiplier` --references--> `RegimeReader`  [INFERRED]
  Todo-2.txt → investment_strategy/regime.py
- `RiskManager (deterministic, participant)` --conceptually_related_to--> `RiskManager`  [INFERRED]
  sequence_1.png → investment_strategy/risk.py
- `Watchdog CRITICAL Alerting` --references--> `Alerter`  [EXTRACTED]
  completed.txt → investment_strategy/notify.py
- `Backtest Harness (edge-proving)` --references--> `RiskManager`  [INFERRED]
  completed.txt → investment_strategy/risk.py
- `Min-conviction Gate` --references--> `RiskManager`  [INFERRED]
  Todo-2.txt → investment_strategy/risk.py

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **Phase-C discovery-feed remediation set** — todo_3_c1_dead_feed_diagnosis, todo_3_c2_robinhood_movers_screener, todo_3_c4_robinhood_options_signal, todo_3_c5_robinhood_fundamentals [INFERRED 0.85]
- **Core-satellite fill mechanism** — todo_3_core_satellite_fill, todo_3_submit_notional_buy, todo_3_from_core_fill, todo_3_core_etf_target_invested [INFERRED 0.85]
- **Aggressive beat-the-index pivot** — todo_3_tier1_risk_knob_loosening, todo_3_tier2_prompt_de_timid, todo_3_core_satellite_fill, todo_3_cash_drag_diagnosis [INFERRED 0.85]
- **Capital-Preservation Stack (layered risk caps)** — todo_2_no_leverage_cap, todo_2_pdt_guard, todo_2_sector_cap, todo_2_regime_filter, todo_2_time_stop, todo_2_scale_out, completed_earnings_blackout [INFERRED 0.85]
- **Prove-the-edge-before-spending Workflow** — completed_backtest_harness, completed_subscription_gate, completed_signal_attribution [INFERRED 0.75]
- **Decision-cycle Participant Flow** — sequence_1_orchestrator, sequence_1_screeners, sequence_1_signals, sequence_1_claude, sequence_1_riskmanager, sequence_1_alpaca [EXTRACTED 1.00]

## Communities (56 total, 20 thin omitted)

### Community 0 - "Backtest Harness"
Cohesion: 0.06
Nodes (55): BaseModel, Backtest Harness (edge-proving), Enum, BacktestEngine, BacktestResult, ClosedTrade, _demo(), _demo_limits() (+47 more)

### Community 1 - "Screener Aggregator & Discovery"
Cohesion: 0.07
Nodes (43): Market-discovery layer: scans for smart-money activity and surfaces NEW     cand, ScreenerConfig, Candidate, Runs every enabled screener, merges their candidates into a single ranked, dedup, Discover candidate symbols, excluding names already being evaluated         (cur, ScreenerAggregator, Market-discovery layer.  Screeners scan the market for smart-money activity and, InsiderFeedScreener (+35 more)

### Community 2 - "Watchdog Safety Loop"
Cohesion: 0.08
Nodes (43): Position, _partial(), AccountSnapshot, Position, Position watchdog — the always-on safety loop.  Alpaca's bracket orders already, Close every position. Only forget trailing state on CONFIRMED close;         a f, Market-close `pos`; if that can't fill (market closed / LULD-halted — a, Close `pos` if it has breached the stop/take registered for it (only         fra (+35 more)

### Community 3 - "Risk Models & Tests"
Cohesion: 0.14
Nodes (59): OptionLeg, One leg of an options order. `right` is C/P, OCC-style symbol resolved     at ex, Claude's recommendation for a symbol. Sizing is a REQUEST, not a guarantee     —, TradeProposal, _account(), _buy(), _limits(), _opt() (+51 more)

### Community 4 - "Robinhood MCP & Quiver Client"
Cohesion: 0.06
Nodes (31): Any, Quiver Paid-tier Shared Client + Datasets, Exception, ExternalHolding, Read-only Robinhood holdings via the OFFICIAL Agentic Trading MCP — CONTEXT ONLY, Map an already-unwrapped positions payload (list, or a dict carrying a         p, Sync entry point for the orchestrator. Returns [] if disabled/failed., Call any Robinhood MCP READ tool and return its parsed JSON payload         (dic (+23 more)

### Community 5 - "Config Loader & P&L"
Cohesion: 0.08
Nodes (38): Account P&L Tracker (Alpaca truth), _f(), _flag(), _i(), load_config(), Env flag -> bool. Accepts on/true/1/yes (case-insensitive)., main(), Entry point: `python -m investment_strategy`.  Loads config (which validates the (+30 more)

### Community 6 - "Earnings-Blackout Guard"
Cohesion: 0.06
Nodes (30): Earnings-blackout Guard, date, EarningsCalendar, Next-earnings-date lookup for the earnings-blackout guard.  Gap risk around an e, Drop the per-cycle cache so the next lookup re-fetches. Call once at the, Calendar days until the next scheduled earnings report, or None if no         FU, Per-symbol sector lookup for the sector-concentration cap.  Discovery is now the, Drop the per-cycle cache so the next lookup re-fetches. Call once at the (+22 more)

### Community 7 - "Alerting (email/webhook)"
Cohesion: 0.09
Nodes (29): AlertConfig, Alerter, load_alert_config(), Out-of-band alerting for watchdog CRITICALs — page a human when the safety loop, Build AlertConfig from an env-getter (kept out of config.py's giant     load_con, Where CRITICAL alerts go. Both sinks optional; unset => log-only (inert)., Sends CRITICAL alerts to email and/or a webhook, throttled per key.      Never r, Page a human about a CRITICAL condition. `key` de-dupes recurring         events (+21 more)

### Community 8 - "Portfolio State"
Cohesion: 0.08
Nodes (23): PortfolioState, datetime, Path, Ratchet the all-time-high equity. Persists only on a new high., Peak-to-current drawdown as a positive % (0 if at/above peak)., Record the hard stop / take-profit (% from entry) the watchdog must         enfo, Stamp the FIRST time we saw this position, starting its hold clock.         Idem, Calendar days since the position's first entry, or None if unknown. (+15 more)

### Community 9 - "Config & Data Models"
Cohesion: 0.13
Nodes (24): Central configuration. Reads .env once and exposes a typed, validated Config.  T, _now(), datetime, Shared data models that flow between the four stages.  Signals (per symbol)  ->, One normalized observation about a symbol (or the market, for macro)., Signal, SignalKind, Gathers every provider's signals into one SignalBundle per symbol, with market-w (+16 more)

### Community 10 - "Preflight Readiness Check"
Cohesion: 0.10
Nodes (33): _check_alerts(), _check_alpaca(), _check_anthropic(), _check_config(), main(), Preflight readiness check — does everything the bot needs ACTUALLY work?      py, Config loads + the paper/live interlock is satisfied and required keys set., Actually authenticate against Alpaca — the check the import trick can't do. (+25 more)

### Community 11 - "WSB & GovContracts Signals"
Cohesion: 0.15
Nodes (33): Discovery screener: WallStreetBets mention surges, across all tickers.  Source:, WallStreetBetsScreener, GovContractsProvider, OffExchangeProvider, _cfg(), _FakeQuiver, _old(), Tests for the off-exchange signal and WallStreetBets screener.  Pure logic, no n (+25 more)

### Community 12 - "Account-change Reset"
Cohesion: 0.15
Nodes (30): account_marker_path(), current_fingerprint(), main(), maybe_reset_on_account_change(), per_account_paths(), Path, Reset local per-account state — for a recreated Alpaca account or a paper<->live, Startup hook: if the connected account differs from the one the local state (+22 more)

### Community 13 - "Benchmark & Config"
Cohesion: 0.08
Nodes (9): Benchmark-relative performance tracking.  The bot's job is EXCESS return over SP, Config, True only when new orders are permitted to be placed at all., SignalAggregator, FundamentalsProvider, Crude composite in [-1, 1]: healthy margins + growth bullish,         heavy leve, InsiderProvider, MacroProvider (+1 more)

### Community 14 - "Orchestrator & Core-Fill"
Cohesion: 0.08
Nodes (16): A core-satellite (Todo 1.6) top-up buy of the broad CORE_ETF. It is NOT, Orchestrator, Candidate, Independent safety loop: closing positions is never gated, so this runs, Loudly flag safety nets that are disabled, so an off-by-default setting, Let an operator halt NEW buys WITHOUT a restart by creating the         kill-swi, Regenerate the live dashboard HTML after a cycle so the tracker stays         fr, Persist a once-per-day account P&L snapshot (true total return from the (+8 more)

### Community 15 - "Orchestrator Tests"
Cohesion: 0.20
Nodes (30): Reflect a just-submitted BUY into the snapshot: add/extend the position, _acct(), _bundle(), _held(), _orch(), _pos(), Tests for the intra-cycle running-tally over-deploy fix (1B.3).  The account is, _regime() (+22 more)

### Community 16 - "Candidate / Discovery Signal"
Cohesion: 0.09
Nodes (15): Candidate, A ticker surfaced by the market scanner BEFORE any per-symbol signals are     ga, Render as a DISCOVERY signal so the candidate's rationale rides through, Candidate, Screener interface. Each source scans the market and returns Candidate symbols i, Base for all discovery sources., Return candidate symbols surfaced from a market-wide scan., Override to gate on the presence of an API key, etc. (+7 more)

### Community 17 - "Trading-Bot Architecture"
Cohesion: 0.08
Nodes (26): Watchdog CRITICAL Alerting, Decision Cycle (15m slow brain), Held-position Keep-vs-Sell on Two Clocks, Watchdog Loop (30s fast safety net), Account-change Auto-reset, Claude-driven Trading Bot, FIND-READ-DECIDE-BUY-PROTECT Pipeline, Five-step Run Guide (+18 more)

### Community 18 - "Account P&L Dashboard"
Cohesion: 0.14
Nodes (24): _account_panel(), _area_chart(), _bar_chart(), build_html(), _card(), _exit_cell(), _fmt_dt(), generate() (+16 more)

### Community 19 - "Regime Filter"
Cohesion: 0.13
Nodes (14): Market-regime filter — scale aggressiveness to the market backdrop.  Buying the, Drop the cached read so the next assess() recomputes. Call once per         deci, Regime, RegimeReader, _FakeRegime, Tests for the market-regime filter's scoring logic.  Pure logic, no network: the, Injects SPY closes + a VIX level instead of hitting yfinance., test_assess_is_cached_until_new_cycle() (+6 more)

### Community 20 - "Signal Attribution & Reflection"
Cohesion: 0.13
Nodes (21): Signal Attribution / Reflection Loop, Data-subscription Decision Gate, TradingAgents Review (what to adopt/reject), attribute(), Signal attribution — close the learning loop the ledger opened.  The ledger reco, Per-source win-rate and average realized P&L across round-trips., Consume `qty` shares from the front of `lots` (each [remaining_qty, signals]),, _reduce_fifo() (+13 more)

### Community 21 - "Technical Signals (RSI/MACD)"
Cohesion: 0.10
Nodes (5): Composite momentum lean in [-1, 1]: trend 0.4, MACD 0.4, RSI 0.2.          Crude, Wilder's RSI in [0, 100]. 50 is neutral; <30 oversold, >70 overbought., MACD line (EMA12-EMA26) and its 9-period signal EMA., TechnicalProvider, Tests for the technical-signal indicator math (RSI, MACD, trend score).  Pure fu

### Community 22 - "Beat-the-Index Roadmap (Todo-3)"
Cohesion: 0.11
Nodes (23): Active TODO (Todo-3.txt), Beat-the-benchmark goal (QQQ/SPY, high-risk OK), C.1 Dead-feed diagnosis (insider low-yield; options_flow structurally dead), C.2 RobinhoodMoversScreener (Daily movers + 100 most popular), C.4 Robinhood options-sentiment signal (open), C.5 Route fundamentals/earnings/sector via Robinhood get_equity_fundamentals (open), Cash-drag diagnosis (90% idle cash = structural short vs benchmark), CORE_ETF + TARGET_INVESTED_PCT config (+15 more)

### Community 23 - "Track-record Lessons"
Cohesion: 0.25
Nodes (20): Build the trusted 'track record' block for the decision prompt, or "" if     the, Reconstruct closed round-trips from ledger records (chronological).      Only tr, render_lessons(), round_trips(), _buy(), _ledger_with(), Tests for the signal-attribution / reflection loop.  Pure logic, no network: bui, _sell() (+12 more)

### Community 24 - "Alpaca Client"
Cohesion: 0.12
Nodes (7): AlpacaClient, Simple price return over `days` calendar days — for benchmark compare., Market-SELL `qty` shares (whole or fractional) of an existing long — a         P, Fallback exit when a plain MARKET close can't fill — e.g. the market is, (status, filled_qty, qty) for an order — for post-hoc fill reconciliation., $ value of OPEN (unfilled) BUY orders for `symbol`. The risk layer         count, Realized annualized volatility from daily closes — used for         vol-targeted

### Community 25 - "Decision Engine (Claude)"
Cohesion: 0.18
Nodes (8): DecisionEngine, Claude decision engine: signal bundles -> structured trade proposals.  Calls the, Neutralize the delimiter so a crafted headline can't close the         <market_d, Ask Claude for proposals across all candidate symbols at once.          One call, System prompt and JSON schema for the Claude decision engine., ExternalHolding, All signals relevant to a single symbol, plus shared market context., SignalBundle

### Community 26 - "Options Helper (OCC)"
Cohesion: 0.17
Nodes (8): Thin wrapper over alpaca-py: account/positions, prices, volatility, and orders., occ_symbol(), OptionsHelper, OptionLegRequest, Options helpers — OCC symbol construction, premium estimation, leg building.  Ke, Build an OCC option symbol, e.g. AAPL 2026-01-16 C 150 -> AAPL260116C00150000., Convert a proposal's OptionLeg list to broker OptionLegRequests., Net debit per share (×100 = per contract) for the strategy. Buys add         to

### Community 27 - "Subscription ROI Gate"
Cohesion: 0.31
Nodes (13): evaluate_subscriptions(), Score every candidate subscription against the ledger's measured signal     attr, _ledger(), Tests for the data-subscription evaluation (2.2).  Builds a small ledger of clos, Record a buy (carrying the entry signal source) then a sell with an outcome., _round_trip(), test_empty_ledger_is_all_insufficient(), test_insufficient_data_when_too_few_trips() (+5 more)

### Community 28 - "Order Submission (notional/bracket)"
Cohesion: 0.21
Nodes (8): Action, RiskDecision, Place any equity order described by an OrderRequest. Returns order id., Scale-in/out ladder: split total_qty across `rungs` limit orders evenly, Build a BUY from a risk-approved equity decision.          Returns (order_id, is, Plain dollar-notional MARKET buy (no bracket) — used by the core-ETF         fil, OrderRequest, TIF

### Community 29 - "Trade Ledger"
Cohesion: 0.20
Nodes (7): datetime, _now(), Trade ledger — the durable audit log of every order the bot actually places.  Or, One executed order, with the decision context that produced it., TradeRecord, Ties the four stages together and runs the two timed loops.    decision cycle (s, Thesis-decay / Signal-freshness Exit

### Community 30 - "Benchmark Tracker (info ratio)"
Cohesion: 0.29
Nodes (4): BenchmarkTracker, Excess daily return mean / std vs benchmark daily returns, annualized.         A, BenchmarkStats, Account performance RELATIVE to a benchmark over a lookback window. The     poin

### Community 31 - "Risk Decision & Equity Path"
Cohesion: 0.22
Nodes (4): RiskDecision, (sector of `symbol`, $ already held in that sector) for the risk         sector-, Reflect a decision SELL into the snapshot: drop the position and return, TradeProposal

### Community 33 - "Account Snapshot"
Cohesion: 0.25
Nodes (4): AccountSnapshot, Position, (base_value, net_cashflows) since account inception, for true         total-retu, Stable identifier for the connected Alpaca account. It changes if the         ac

### Community 34 - "News Sentiment Signal"
Cohesion: 0.36
Nodes (3): NewsProvider, Mean VADER compound score over the headlines, already in [-1, 1].         Return, _vader_analyzer()

### Community 40 - "Quiver Access Probe"
Cohesion: 0.53
Nodes (5): _classify(), main(), _probe(), Report which Quiver datasets your API token can actually access.  Probes each kn, Session

### Community 42 - "Quiver Schema Probe"
Cohesion: 0.67
Nodes (3): _load_key(), main(), Probe the Quiver endpoints we use and print the REAL response schema.  Loads the

## Knowledge Gaps
- **25 isolated node(s):** `Kill Switch (blocks new buys, closing never blocked)`, `Five-step Run Guide`, `Account-change Auto-reset`, `Thesis-decay / Signal-freshness Exit`, `Let-winners-run Scale-out` (+20 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **20 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `Config` connect `Benchmark & Config` to `Screener Aggregator & Discovery`, `Watchdog Safety Loop`, `Robinhood MCP & Quiver Client`, `Config Loader & P&L`, `Config & Data Models`, `WSB & GovContracts Signals`, `Account-change Reset`, `Orchestrator & Core-Fill`, `Candidate / Discovery Signal`, `Technical Signals (RSI/MACD)`, `Alpaca Client`, `Decision Engine (Claude)`, `Options Helper (OCC)`, `Trade Ledger`, `Benchmark Tracker (info ratio)`, `EDGAR Insider Signal`, `News Sentiment Signal`, `Congress Signal`, `Alerter/State glue`?**
  _High betweenness centrality (0.181) - this node is a cross-community bridge._
- **Why does `AlpacaClient` connect `Alpaca Client` to `Account Snapshot`, `Config Loader & P&L`, `Alerter/State glue`, `Preflight Readiness Check`, `Options Leg Submission`, `Account-change Reset`, `Benchmark & Config`, `Account P&L Dashboard`, `Options Helper (OCC)`, `Order Submission (notional/bracket)`, `Benchmark Tracker (info ratio)`?**
  _High betweenness centrality (0.097) - this node is a cross-community bridge._
- **Why does `Watchdog` connect `Watchdog Safety Loop` to `Alerter/State glue`, `Trading-Bot Architecture`, `Alerting (email/webhook)`?**
  _High betweenness centrality (0.079) - this node is a cross-community bridge._
- **Are the 2 inferred relationships involving `Config` (e.g. with `BenchmarkTracker` and `Orchestrator`) actually correct?**
  _`Config` has 2 INFERRED edges - model-reasoned connections that need verification._
- **Are the 9 inferred relationships involving `PortfolioState` (e.g. with `BacktestEngine` and `BacktestResult`) actually correct?**
  _`PortfolioState` has 9 INFERRED edges - model-reasoned connections that need verification._
- **What connects `AI-assisted trading system: signals -> Claude decision -> Alpaca -> monitor.`, `Entry point: `python -m investment_strategy`.  Loads config (which validates the`, `Map the WATCHLIST env var to a watchlist.      None  (unset)                 ->` to the rest of the system?**
  _256 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Backtest Harness` be split into smaller, more focused modules?**
  _Cohesion score 0.05640203154236835 - nodes in this community are weakly interconnected._