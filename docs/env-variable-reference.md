# `.env` Reference — Claude/Alpaca Paper-Trading Bot

This is a knob-by-knob reference for every variable in the bot's `.env` file (~117 variables across ~24 sections). It is meant to be **consulted, not read start-to-finish** — look up the variable you're about to change, read its purpose/impact/interactions/verdict, and check the linked variables it interacts with before editing. All secret values (API keys, the SMTP app password, OAuth tokens/URLs, the brokerage account number) are redacted as `<redacted>` throughout; only variable names, non-secret current values, and behavior are documented.

## Table of contents

1. [Trading Mode, Alpaca Credentials, and Claude/Anthropic](#1-trading-mode-alpaca-credentials-and-claudeanthropic)
2. [Risk Limits: Position/Exposure Caps, Vol-Scaled Stops, Correlation Guard, Trade Risk, Conviction Floor, Slippage Edge](#2-risk-limits-positionexposure-caps-vol-scaled-stops-correlation-guard-trade-risk-conviction-floor-slippage-edge)
3. [Exit Management (Watchdog), Market Regime Sizing, Thesis-Decay Exit](#3-exit-management-watchdog-market-regime-sizing-thesis-decay-exit)
4. [Capital-Preservation Stack: Equity Floor, No-Leverage Guard, Sector Cap, Earnings Blackout, PDT Guard, Fractional Shares/Churn Guards](#4-capital-preservation-stack-equity-floor-no-leverage-guard-sector-cap-earnings-blackout-pdt-guard-fractional-shareschurn-guards)
5. [Position Sizing (Kelly/Vol-Target), Options (Defined-Risk) Gates](#5-position-sizing-kellyvol-target-options-defined-risk-gates)
6. [Market Scanner/Discovery, Benchmark, Core-Satellite Fill](#6-market-scannerdiscovery-benchmark-core-satellite-fill)
7. [Signal-Source API Keys, Read-Only Robinhood MCP Integration](#7-signal-source-api-keys-read-only-robinhood-mcp-integration)
8. [Loop Cadence, Universe & Runtime Control Files, Live Dashboard](#8-loop-cadence-universe-runtime-control-files-live-dashboard)
9. [Watchdog CRITICAL Alerting (Email/Webhook), Dead-Man External Paging](#9-watchdog-critical-alerting-emailwebhook-dead-man-external-paging)
10. [Anti-Chasing Overextension Gate, Composite Signal Index, Rotation Loss Guard, Entry-Quality Gates, Weekly Auto-Tune Report](#10-anti-chasing-overextension-gate-composite-signal-index-rotation-loss-guard-entry-quality-gates-weekly-auto-tune-report)
- [Completeness check](#completeness-check)

---

## 1. Trading Mode, Alpaca Credentials, and Claude/Anthropic

### Trading mode

**`TRADING_MODE`** — current: `paper`
- **Purpose:** Selects which Alpaca account family the bot talks to — `paper` (fake money) or `live` (real money).
- **Loaded as:** `Config.mode` (`TradingMode` enum, `config.py:407`); drives `Config.is_live`.
- **Impact:** Only two accepted values; anything else raises `ValueError` at startup (fails closed, won't silently default). Flipping to `live` changes real-money exposure everywhere `cfg.is_live` is read — `AlpacaClient.__init__` (`execution/alpaca_client.py:133`) passes `paper=not cfg.is_live` to Alpaca's `TradingClient`, and the same flag gates `signals/options_chain.py` and `execution/options.py`.
- **Interactions:** Hard-linked to `ALPACA_BASE_URL` — `load_config()` raises at startup if `TRADING_MODE=live` and the URL string contains `"paper"` (or vice versa), so a half-edited `.env` refuses to boot rather than silently mixing modes. Also layered under `KILL_SWITCH`: even `live` mode places no orders while the kill switch is on.
- **Verdict:** Inline comment says "start here" for paper — treat `live` as a deliberate, rare flip, not a routine toggle. There's no separate "confirm live" prompt beyond the URL interlock, so double-check `ALPACA_BASE_URL` is updated in the same edit.

**`KILL_SWITCH`** — current: `off`
- **Purpose:** Global new-order block, independent of trading mode — "on" stops all new buy/sell placement; the watchdog can still close existing positions for safety.
- **Loaded as:** `Config.kill_switch` via `_flag("KILL_SWITCH")` (accepts on/true/1/yes case-insensitive, else off); becomes `RiskManager.kill_switch` (`risk.py:43-47`).
- **Impact:** When true, `RiskManager.trading_halted()` returns `True` immediately with reason `"KILL_SWITCH is on"` (`risk.py:57`) — no new positions of any kind. It also short-circuits the rotation-loss guard (`orchestrator.py:1496`, "buys are halted — a paired sell funds no rotation"). Turning it off resumes normal risk-gated trading.
- **Special values:** Any of `on/true/1/yes` (case-insensitive) = on; everything else = off.
- **Interactions:** This is only one of three inputs `orchestrator._refresh_runtime_controls()` ORs together every cycle (`orchestrator.py:659-667`): the `.env` value at startup, the existence of `KILL_SWITCH_FILE` (a separate runtime lever elsewhere in `.env`, default `state/KILL` — no restart needed to flip it), and an in-memory `_forced_halt` latch set by things like a reconcile-halt event. It's also distinct from the persisted `state.halted` "HALT LATCH" (equity-floor / drawdown breaches) — `trading_halted()` checks the halt latch *before* the kill switch, and clearing `KILL_SWITCH` in `.env` does **not** clear a halt caused by the file or the latch; those need separate action.
- **Verdict:** Leave off for normal operation. To pause the bot without a restart, use the `KILL_SWITCH_FILE` runtime lever, not an `.env` edit + restart.

### Alpaca credentials

**`ALPACA_API_KEY`** — current: `ALPACA_API_KEY=<redacted>`
**`ALPACA_SECRET_KEY`** — current: `ALPACA_SECRET_KEY=<redacted>`
- **Purpose:** Auth pair for the Alpaca Trading/Data API. Paper and live are entirely separate key pairs from app.alpaca.markets — not the same key with a mode flag.
- **Loaded as:** `Config.alpaca_api_key` / `alpaca_secret_key` (plain `os.getenv`, default `""`); passed straight into `TradingClient(...)` / `StockHistoricalDataClient(...)` in `execution/alpaca_client.py:133-137`.
- **Impact:** Blank at startup → `load_config()` raises `"Missing required env vars"` (hard-required alongside `ANTHROPIC_API_KEY`, `config.py:650-658`) — the bot cannot start. Present but wrong/rotated → boots fine (config only checks presence) and then 401s on the first real call; `preflight.py`'s `_check_alpaca` actually authenticates and gives a targeted hint ("check for a stray '#' or extra characters, and that paper keys match the paper URL").
- **Interactions:** Must correspond to the same account family selected by `TRADING_MODE`/`ALPACA_BASE_URL` — the code's paper/live interlock only checks the *URL string* against `TRADING_MODE`, not which literal key is pasted in, so a paper key against a live-mode config is only caught by Alpaca's own 401, not by `load_config()`.
- **Verdict:** Rotate immediately if ever exposed. Paper keys are low-stakes; live keys are real-money and deserve the same handling discipline as the Anthropic key.

**`ALPACA_BASE_URL`** — current: `https://paper-api.alpaca.markets/v2`
- **Purpose:** Nominally the REST endpoint for Alpaca calls; comment says leave default for paper, set to `https://api.alpaca.markets` for live.
- **Loaded as:** `Config.alpaca_base_url` (`config.py:408,428`).
- **Impact:** Grepping the codebase shows this string is **never actually passed** to `TradingClient`/`StockHistoricalDataClient` — the real SDK client picks its endpoint from the `paper: bool` derived from `TRADING_MODE` (`cfg.is_live`), not from this URL. Its only functional role is the textual safety check in `load_config()`: it's substring-checked for `"paper"` against `TRADING_MODE` to decide whether to raise the live/paper mismatch `ValueError` at startup.
- **Interactions:** 1:1 paired with `TRADING_MODE` for that startup interlock only. Because the SDK ignores its value otherwise, the interlock can technically be satisfied by any string containing/not-containing `"paper"` matching the mode — it isn't validated as a real URL.
- **Verdict:** Don't delete it — the live/paper crossed-wires guard depends on it — but don't expect changing it to redirect actual API traffic; that's controlled entirely by `TRADING_MODE`.

### Claude / Anthropic

**`ANTHROPIC_API_KEY`** — current: `ANTHROPIC_API_KEY=<redacted>`
- **Purpose:** Auth for the Anthropic API calls the decision engine and the nightly postmortem make directly via the `anthropic` Python SDK.
- **Loaded as:** `Config.anthropic_api_key`; used as `api_key=cfg.anthropic_api_key` in both `decision/engine.py:38` (`timeout=cfg.decision_timeout_s, max_retries=1`) and `postmortem.py:238` (fixed `timeout=60.0`).
- **Impact:** Blank → `load_config()` raises at startup (hard-required). Malformed → `preflight.py`'s `_check_anthropic` catches obviously-wrong values (must start `sk-ant-`) without spending money. A genuinely wrong/revoked key passes that format check and fails at the first real decision call — caught as `anthropic.APIError`, logged, and treated as "no proposals this cycle" (`decision/engine.py`'s except block) rather than crashing the bot.
- **Special values:** Comment explicitly warns a Claude Pro/Max subscription does **not** cover this — it's separate pay-as-you-go billing at console.anthropic.com.
- **Interactions:** Shared by two call sites — the per-cycle decision engine and the nightly postmortem — both billed the same way, one key.
- **Verdict:** This is the one credential with metered, usage-based real-world cost (see the repo's API-cost-baseline notes) rather than an all-or-nothing account breach — still keep it tightly scoped and rotatable.

**`DECISION_MODEL`** — current: `claude-opus-4-8`
- **Purpose:** Which model the decision engine (and postmortem) call for every trade decision and the nightly review.
- **Loaded as:** `Config.decision_model` (`os.getenv("DECISION_MODEL", "claude-opus-4-8")`, no validation); becomes `self.model` in `DecisionEngine` and is passed as `model=` on every `messages.create` call, and reused verbatim by `postmortem.py`.
- **Impact:** Inline comment: all Opus tiers (4.8/4.7/4.6) are priced identically ($5/$25 per 1M in/out), so dropping to a lower Opus tier **saves nothing** — the real cost lever is `DECISION_EFFORT`. There's no whitelist in code; a typo'd or unsupported model string isn't caught at config-load time, only at the first API call (`APIError` → that cycle's proposals are dropped, same graceful-degrade path as a bad key).
- **Interactions:** Reused as-is by the nightly postmortem, but that call independently hardcodes `effort="low"` and a fixed 60s timeout — it does **not** inherit `DECISION_EFFORT` or `DECISION_TIMEOUT_SECONDS` from this same `.env`.
- **Verdict:** Per the comment's own reasoning, there's no cost incentive to downgrade the Opus tier — keep 4.8 for best reasoning; use `DECISION_EFFORT` to manage spend instead.

**`DECISION_EFFORT`** — current: `medium`
- **Purpose:** Claude's thinking-depth/output-effort level per decision call (`output_config.effort`) — the primary cost dial, since output tokens (including adaptive thinking) dominate spend.
- **Loaded as:** `Config.decision_effort`; validated against the set `{low, medium, high, xhigh, max}` — if the env value isn't one of those, it **silently falls back to `"medium"`** rather than raising (`config.py:434-439`). Passed into `decision/engine.py` as `"effort": self.cfg.decision_effort` alongside a hardcoded `thinking={"type": "adaptive"}`.
- **Impact:** Raising toward `high`/`xhigh`/`max` spends more thinking + output tokens per cycle (higher API cost, presumably deeper reasoning); lowering to `low` cuts spend fastest. The inline comment calls `medium` "a good cost/quality balance."
- **Special values:** Only the five listed strings are meaningful; anything else is silently coerced to `medium` — a typo here won't error, it'll just quietly run at the default, which is worth knowing when debugging an unexpected cost/quality level.
- **Interactions:** Independent of `DECISION_MODEL`'s price (Opus tiers cost the same regardless), so this is the one lever that actually moves $-per-decision-call. Not read by `postmortem.py`, which hardcodes `effort="low"` regardless of this setting.
- **Verdict:** `medium` is the shipped, comment-endorsed balance. Drop to `low` if budget-constrained; the comment frames anything above `medium` as a pure spend increase without a stated quality payoff having been measured for this bot specifically.

**Files referenced:** `/Users/spusapati/Personal/Investment_stratergy/.env` (lines 5-33), `/Users/spusapati/Personal/Investment_stratergy/investment_strategy/config.py`, `/Users/spusapati/Personal/Investment_stratergy/investment_strategy/risk.py`, `/Users/spusapati/Personal/Investment_stratergy/investment_strategy/orchestrator.py`, `/Users/spusapati/Personal/Investment_stratergy/investment_strategy/decision/engine.py`, `/Users/spusapati/Personal/Investment_stratergy/investment_strategy/postmortem.py`, `/Users/spusapati/Personal/Investment_stratergy/investment_strategy/preflight.py`, `/Users/spusapati/Personal/Investment_stratergy/investment_strategy/execution/alpaca_client.py`

---

## 2. Risk Limits: Position/Exposure Caps, Vol-Scaled Stops, Correlation Guard, Trade Risk, Conviction Floor, Slippage Edge

*(HARD caps enforced in code, not by the LLM — `.env` lines 34-75 — `investment_strategy/config.py` §RiskLimits, enforced in `risk.py`, `monitor/watchdog.py`)*

### Position / exposure / account-halt caps

**`MAX_POSITION_PCT`** — current **12.0**
- Purpose: hard ceiling on a single *new* position's target weight. `risk.py:508-510` takes `min(LLM's target_weight_pct, Kelly-sized weight, this)`.
- ↑ bigger single-name bets, faster to "fully invested"; ↓ forces smaller/more diversified entries, can leave cash idle if `MAX_OPEN_POSITIONS` isn't loosened too.
- Interacts: whichever of this, `kelly_fraction`×`target_annual_vol_pct` sizing, or the LLM's ask is smallest wins; layered under `MAX_SYMBOL_EXPOSURE_PCT` (must be ≥ this or top-ups get blocked immediately) and `MAX_GROSS_EXPOSURE_PCT` (not in range, backstops total leverage).
- Verdict: code default is 5%; 12% is a deliberate aggressive override — comment notes "~8 names = fully invested," i.e. an intentionally concentrated book.

**`MAX_SYMBOL_EXPOSURE_PCT`** — current **18.0**
- Purpose: ceiling on *total* $ in one symbol across all buys/top-ups combined (vs. `MAX_POSITION_PCT`, which only bounds one new buy). `risk.py:530-538`: rejects once held+pending ≥ cap; counts *pending* (unfilled) buy notional too, so repeated decision cycles can't stack past it.
- ↑ lets a high-conviction winner keep being added to ("let it run"); ↓ forces diversification / selling instead of adding.
- Interacts: must sit above `MAX_POSITION_PCT` to allow any top-up at all; conceptually paired with `MAX_PAIRWISE_CORR` (that guard stops a *different* ticker from being the same bet in disguise).
- Verdict: default 10%; 18% here needs ~2 adds at the 12% single-buy cap to actually bind — consistent, not reckless.

**`MAX_DAILY_LOSS_PCT`** — current **3.0**
- Purpose: halts *new* buys once today's loss reaches this % of equity (`risk.py:59-64`) **and** independently triggers `Watchdog._emergency_flatten` (`watchdog.py:189-196`), which force-closes every position — same threshold, two enforcement points.
- ↑ more room to ride out an intraday dip before either the buy-halt or the flatten fires; ↓ hair-trigger halts/flattens on ordinary volatility, can lock in losses on a temporary wick.
- Interacts: independent of `MAX_DRAWDOWN_PCT` (that one is peak-to-trough and never resets daily; this resets each session).
- Verdict: 3% matches code default — a tight, conservative day-loss breaker; reasonable range ~2-5%.

**`MAX_DRAWDOWN_PCT`** — current **15.0**
- Purpose: halts new buys once equity is this % below its all-time peak — catches slow bleeds the daily cap can't see (`risk.py:67-72`).
- ↑ tolerates a deeper correction before freezing new entries; ↓ freezes buying sooner, more prone to false halts during normal chop.
- Special: 0 ≈ halts almost immediately on any drawdown.
- Interacts: distinct from `EQUITY_FLOOR_PCT` (outside this range — a stricter watchdog trip at 60% of peak that actively flattens); this one only blocks *new* buys, doesn't close existing positions.
- Verdict: 15% matches code default — a standard conservative-to-moderate setting.

**`MAX_OPEN_POSITIONS`** — current **15**
- Purpose: caps concurrent *model-opened equity* holdings (option rows have their own cap). `risk.py:361-401`: blocks a brand-new symbol once model equity rows ≥ cap; a top-up of an *already-held* name is exempt (2026-07-13 NU incident — counting top-ups froze all buying at 15/15).
- Counts only model-opened equity rows; `CORE_ETF` / `HEDGE_ETF` / `DEFENSIVE_CORE_ETF` rows never consume a slot (S-1, run-7 — Sep 3-4 2026: QQQ + PSQ made a 14-satellite book read 16/15, rejecting HOOD x2 and MU; MU six seconds after a partial MKL fold had already freed a slot). The exemption (`RiskLimits.slot_exempt_symbols`) has no env key of its own — `load_config` derives it from those three keys, de-duplicated, so it cannot drift from the orchestrator's system-managed set; `PUT_PROXY_ETF` is an option underlying and is deliberately NOT exempt. Greppable: `SLOT COUNT: 14/15 model rows (exempt: QQQ, PSQ; raw 16)` on every new-name cap check where the exemption changed the count; the reject reason now prints the count, `At max open positions (15/15 model rows).`; a partial decision-sell fold logs `ROTATION: MKL slot + $16528 folded into this cycle pending fill`.
- ↑ more names to diversify across but more to monitor/LLM-scan each cycle (cost); ↓ forces concentration, can block a good new idea when the book is full of stale names. With core + hedge on, 15 now means 15 model names (17 broker equity rows) — lower to 13 to keep the pre-S-1 effective book.
- Interacts: with `MAX_POSITION_PCT`, sets the theoretical gross ceiling (15×12%=180%) — actually capped for real by `MAX_GROSS_EXPOSURE_PCT` (not in range, =100 no-leverage guard).
- Verdict: 15 slots vs. the "~8 names fully invested" comment on `MAX_POSITION_PCT` — headroom is intentional, not all 15 is expected to fill.

**`MIN_CASH_BUFFER_PCT`** — current **2.0**
- Purpose: floor % of equity that must stay uninvested; `risk.py:614-617` clamps deployable cash to `cash - min_cash` (also bounded by Alpaca's real-time `buying_power`).
- ↑ more idle cash / cushion; ↓ book runs closer to fully deployed, less slack for a sudden opportunity or a PDT/margin edge case.
- Special: 0 = no reserve at all.
- Verdict: code default is 10%; 2% is a notably aggressive override, sensible only because the comment frames it as a small-float "stay near-fully invested" choice.

**`MIN_TRADE_PRICE_USD`** — current **5.0**
- Purpose: liquidity guard — refuses buys below this share price (`risk.py:479-484`).
- ↑ excludes more low-priced/volatile/wide-spread names; ↓/0 opens up penny stocks — more slippage, stops that gap through.
- Special: 0 disables.
- Verdict: matches code default; a light-touch filter, rarely needs tuning.

**`DEFAULT_STOP_LOSS_PCT` / `DEFAULT_TAKE_PROFIT_PCT`** — current **8.0 / 20.0**
- Purpose: fallback fixed stop/take used when vol-scaled stops are off *or* volatility is unknown, or when the LLM didn't propose its own levels (`risk.py:189-191`).
- ↑ stop: fewer premature chop-outs but bigger loss per losing trade (also shrinks position size via `MAX_TRADE_RISK_PCT`, since $ risk is held ~constant). ↑ take: lets winners run further before the scale-out. ↓ either: tighter/faster exits, more whipsaw or smaller average win.
- Special: these are the **fallback only** — with `VOL_STOPS_ENABLED=on` below, they mostly sit dormant except on a volatility-data outage.
- Verdict: code defaults are 5/12; the 8/20 override is explicitly evidence-based — inline comment cites the "D.1 backtest": 5% stop chopped out at every lookback, 20% take beat 15/12 across the whole sweep. Don't revert without re-running that sweep.

### R.1 — vol-scaled ("ATR-style") stops

**`VOL_STOPS_ENABLED`** — current **on**
- Purpose: switches exit calc from the fixed default pair to a per-name volatility-scaled stop/take (`risk.py:182-188`).
- ON: stop = `clamp(VOL_STOP_MULT × daily-sigma%, MIN, MAX)`, deterministically **overriding** whatever stop/take the LLM proposed ("Claude's numbers are untrusted input" — code comment). OFF: reverts to `DEFAULT_STOP_LOSS_PCT`/`DEFAULT_TAKE_PROFIT_PCT` (or the LLM's own levels) for every name regardless of its real volatility.
- Special: code default is **off**; falls back to fixed levels safely if volatility is unavailable (a data outage never silently changes the exit regime).
- Interacts: when on, `VOL_STOP_MULT`/`VOL_STOP_TAKE_RATIO`/`VOL_STOP_MIN_PCT`/`VOL_STOP_MAX_PCT` take over; the resulting stop% still feeds `MAX_TRADE_RISK_PCT` sizing.
- Verdict: this .env turns ON the backtest-preferred mode — `--sweep-stops` (365d, 2026-07-03) evidence: 2.0σ was top-2 at both 20d and 55d breakout lookbacks, beating fixed 8/20 by ~2x return at half the drawdown at 20d.

**`VOL_STOP_MULT`** — current **2.0**
- Purpose: stop distance = this many standard deviations of the name's realized daily sigma.
- ↑ wider stops everywhere (fewer whipsaws, bigger loss when it fires, smaller size via the $-risk cap); ↓ tighter stops — comment: 1.5σ "chops out."
- Interacts: result clamped to `[VOL_STOP_MIN_PCT, VOL_STOP_MAX_PCT]`; only active with `VOL_STOPS_ENABLED=on`.
- Verdict: 2.0 is the evidenced sweet spot per the sweep (top-2 at both lookbacks; 3.0σ "gives back too much") — don't retune casually without re-running `--sweep-stops`.

**`VOL_STOP_TAKE_RATIO`** — current **2.5**
- Purpose: take-profit = this multiple of the vol-scaled stop distance — sets reward:risk for every vol-scaled exit (`risk.py:188`).
- ↑ bigger targets relative to risk (fewer take-profit hits, more reliance on trailing); ↓ smaller targets, take-profit fires sooner, lower avg win.
- Verdict: comment calls 2.5 "the robust middle (2nd at both lookbacks)" — a deliberately conservative pick over a possibly-better-but-less-robust alternative.

**`VOL_STOP_MIN_PCT`** — commented out, code default **4.0** in effect
- Purpose: floor clamp — even a very quiet name gets at least this much stop room, so normal noise doesn't stop it out.
- ↑ raises floor (fewer noise stop-outs on calm names, bigger loss-per-trade there); ↓ lets very calm names get very tight stops.
- Verdict: unlike MULT/RATIO, this default isn't itself called out as evidenced in the .env comment — inherited, not specifically validated.

**`VOL_STOP_MAX_PCT`** — commented out; **comment claims default 15.0 — actual code default is 10.0**
- Purpose: ceiling clamp — caps how wide the stop can get on a volatile name.
- **Discrepancy flag**: `config.py:563` wires `_f("VOL_STOP_MAX_PCT", 10.0)`, not 15.0 as the `.env` comment states. Since the line is commented out, the bot is *actually* running a 10% ceiling, not the 15% the file documents.
- ↑ allows wider stops on volatile names (bigger max loss before the $-risk cap shrinks the position to compensate); ↓ tighter cap, more forced stop-outs on genuinely choppy names.
- Verdict: fix the comment, or uncomment `VOL_STOP_MAX_PCT=15.0` to actually get the documented behavior — right now intent and reality disagree.

**`TRAIL_GIVEBACK_PCT`** — commented out, code default **3.0** in effect
- Purpose: live trailing-stop run by the Watchdog (not `risk.py`) — once a position's peak unrealized gain exceeds this %, closes it if it gives back this many points from that peak (`watchdog.py:70,495-502`).
- ↑ more room for a winner to breathe before closing (bigger giveback of an already-good gain); ↓ locks in gains faster, more early exits on ordinary pullback noise.
- Special: only arms once peak gain > the giveback amount; runs every monitor cycle, independent of the exchange-resident stop bracket.
- Interacts: works alongside `SCALE_OUT_ENABLED`/`TRAIL_RTH_ONLY` (outside this range).
- Verdict: 3% is a fairly tight trail — sane default for a book built to protect gains quickly.

### R.2 — pairwise-correlation guard

**`MAX_PAIRWISE_CORR`** — current **0.85**
- Purpose: rejects a NEW buy whose 90-day daily-return correlation with an already-held satellite (core ETF excluded) is ≥ this — two names that move together are one bet wearing different tickers (`risk.py:576-586`).
- ↑ (toward 1.0) more permissive — allows near-duplicate names simultaneously, hidden concentration despite ticker-count diversification; ↓ (toward 0) stricter — normal tech megacaps sit ~0.6-0.8 per the comment, so too-low a threshold starts blocking ordinary sector co-movement, not just near-clones.
- Special: **0 = off**, and skips the correlation computation entirely (`orchestrator.py:2241`). Fails **open** (size-down via `MISSING_DATA_MULT`, not reject) when 90d data is missing.
- Interacts: the finer-grained sibling of `MAX_SECTOR_EXPOSURE_PCT` (not in this range) — sector cap catches "same sector," this catches "same price behavior" regardless of label.
- Verdict: matches code default and the comment's own stated rationale ("0.85 = effectively the same trade") — this is the considered value, not an aggressive override.

### Per-trade dollar risk, conviction floor, slippage edge

**`MAX_TRADE_RISK_PCT`** — current **3.0**
- Purpose: caps the ABSOLUTE $ lost if the stop fires — `notional × stop% ≤ equity × this%` — the classic "risk N% per trade" rule (`risk.py:608-611`).
- ↑ bigger position allowed for a given stop width (especially on wide-stop/volatile names); ↓ smaller positions, more conservative loss-per-trade, can undersize legitimately good but volatile setups.
- Special: 0 disables the $ risk cap; sizing then relies only on `MAX_POSITION_PCT`/`MAX_SYMBOL_EXPOSURE_PCT`/Kelly sizing.
- Interacts: denominator is `stop_pct`, sourced from `VOL_STOPS_ENABLED`'s vol-scaled stop or `DEFAULT_STOP_LOSS_PCT` — moves together with those knobs (wider stop → smaller notional, keeping $ risk ~flat per the R.1 comment).
- Verdict: code ships with 1% (traditional capital-preservation number); 3.0 here is an explicit, documented aggressive choice per the inline comment. ~1-3% is a sane band; 5%+ risks real damage from a short losing streak.

**`MIN_CONVICTION`** — current **0.2**
- Purpose: hard floor — rejects any equity buy where Claude's stated conviction (0-1) is below this, no matter what else looks good (`risk.py:246-251`).
- ↑ filters more marginal ideas pre-sizing (higher quality but fewer trades, can leave cash undeployed); ↓ lets weaker "barely clears friction" ideas through, diluting the book.
- Special: 0 = off. `autotune.py` can *sweep and recommend* a new value from trade history but never writes it back to `.env` — report-only.
- Interacts: distinct from (and stacks with) `MIN_NEW_NAME_CONVICTION` — a separate, stricter, new-name-only floor added after the Jul 17-22 postmortem — and `MIN_COMPOSITE_SCORE`/`composite_gate_enabled`, both outside this range.
- Verdict: matches code default. The Jul 17-22 losses (MU, SPCX) were fresh-name entries at 0.45-0.50 conviction — the fix was the new stricter `MIN_NEW_NAME_CONVICTION` gate, not raising this global floor.

**`EST_SLIPPAGE_PCT`** — current **0.10**
- Purpose: estimated one-way friction (spread+slippage) as % of notional; round-trip cost = 2× this, used by the edge-floor gate (`risk.py:495-503`).
- ↑ more take-profit targets rejected as "can't clear cost" (more conservative, fewer marginal-edge trades); ↓ fewer rejections, more thin-edge trades slip through.
- Special: 0 disables the entire edge-floor gate (this *and* `MIN_EDGE_RATIO` become moot).
- Interacts: paired 1:1 with `MIN_EDGE_RATIO`; also reused as the default fill-cost estimate in `backtest.py`.
- Verdict: matches code default; 0.10% one-way is realistic for liquid large/mid-caps via Alpaca market orders — raise it if the screener starts surfacing thinner names.

**`MIN_EDGE_RATIO`** — current **1.5**
- Purpose: take-profit must beat the round-trip friction (`2 × EST_SLIPPAGE_PCT`) by this multiple or the trade is rejected as negative-expectancy on entry (`risk.py:495-503`).
- ↑ stricter — requires a bigger cushion over cost, rejects more tight-target setups; ↓ looser — lets tighter targets through even when they barely clear cost.
- Special: moot if `EST_SLIPPAGE_PCT=0`.
- Interacts: multiplies `EST_SLIPPAGE_PCT`'s round-trip figure; effectively bites hardest on the smallest take-profit source active (a tight vol-scaled take on a low-vol name), not on `DEFAULT_TAKE_PROFIT_PCT`'s 20%.
- Verdict: code default is 2.0; 1.5 here is a documented aggressive loosening ("fewer friction rejections"). Reasonable given the book's 20%+ typical take-profits rarely get close to this gate anyway — would start mattering more if targets shrink toward the vol-stop floor.

---

## 3. Exit Management (Watchdog), Market Regime Sizing, Thesis-Decay Exit

### Exit management (watchdog)

**`MAX_HOLD_DAYS`** — current: `30`
- Purpose: deterministic time-stop — a position held this many days that never reached the min gain below is force-closed by the watchdog so capital rotates out of "dead money," independent of the LLM.
- Impact: higher = more patience with stalled names (ties up capital longer, fewer forced exits); lower = recycles capital faster but can force out slow-burn theses before they play out. Measured in **calendar** days in live/watchdog (`state.entry_age_days`), but **trading** days in `backtest.py` — the two aren't directly comparable when eyeballing backtest results against live behavior.
- Special values: `0` (or ≤0) = off, position can be held indefinitely by this mechanism.
- Interactions: gated jointly with `TIME_STOP_MIN_GAIN_PCT` (both must be past-threshold to fire); explicitly skips `core_etf` (the passive core-satellite holding never times out). Independent of `THESIS_DECAY_ENABLED` — this is price/age-based, decay is signal-based; a name can be caught by either.
- Verdict: 30 days is the shipped default and reads as moderate — long enough to let a normal swing thesis develop, short enough to stop the book accumulating orphaned flat positions.

**`TIME_STOP_MIN_GAIN_PCT`** — current: `2.0`
- Purpose: the "still working" bar — at `MAX_HOLD_DAYS` age, a position under this unrealized-gain % is judged dead money and closed; at/above it, it's left alone to run to its trailing stop/take.
- Impact: raise it and more aging-but-modestly-green positions get force-closed (stricter, more turnover); lower it (toward 0) and almost any non-losing position survives the time-stop, closer to "only kill outright losers held too long."
- Special values: effectively `0` = time-stop fires on any position not in profit at max age.
- Interactions: only meaningful with `MAX_HOLD_DAYS > 0` (time-stop off entirely otherwise). Winners clearing this bar fall through to the existing trailing-stop/scale-out machinery, not this gate.
- Verdict: 2% is a low bar — it's asking "is this at least marginally working," not "is this a big winner," so it mainly culls truly flat/dead names rather than modest gainers.

**`SCALE_OUT_ENABLED`** — current: `on`
- Purpose: at the take-profit target, sell only `SCALE_OUT_PCT` of a fractional (watchdog-managed) position and let the remainder ride the trailing stop, instead of closing the whole thing at the first target.
- Impact: `on` = winners aren't capped at the default +12% take-profit, can compound further under the trail; `off` = every winner is fully closed the instant it hits its take-profit target (simpler, but caps upside).
- Special values: boolean; `off` reverts to legacy full-close-at-take behavior.
- Interactions: only applies to **fractional/unbracketed** positions — whole-share buys rest an exchange bracket and take the full profit there regardless of this flag (i.e. this setting is inert unless `FRACTIONAL_ENABLED=on` and `WHOLE_SHARES_ONLY=off`, which is this repo's current posture). Fires once per position (`scaled` flag) so it can't re-trigger.
- Verdict: `on` is the intended steady-state per the code comment ("so winners aren't capped at +12%") — this is the designed-for setting, not an aggressive override.

**`SCALE_OUT_PCT`** — current: `50.0`
- Purpose: what fraction of the position to sell at the first take-profit target when scale-out fires; the rest keeps riding under the trailing stop.
- Impact: higher % = takes more profit off the table immediately (more locked-in gain, less left to compound); lower % = leaves more exposed to the trail (more upside potential, more given-back risk if it reverses).
- Special values: `0` or the resulting sell qty rounding to `0` shares (can happen under `WHOLE_SHARES_ONLY` on small fractional slices) both fall back to a full take-profit close.
- Interactions: only active when `SCALE_OUT_ENABLED=on`; the sold slice's stop is dropped and only the trailing-stop mechanics (`TRAIL_GIVEBACK_PCT`, `TRAIL_RTH_ONLY`, in the Risk section) govern the remainder afterward.
- Verdict: 50% is the balanced default — a straight coin-flip split between "bank the win" and "let it run"; more aggressive would be a low % (e.g. 20-30%) that lets most of the position ride.

### Market regime (size to the backdrop)

**`REGIME_FILTER_ENABLED`** — current: `on`
- Purpose: master switch scaling **new** position size to the market backdrop — SPY vs its 200-day MA (trend) and VIX level/term-structure (fear) combine into a multiplier (0.25-1.0) applied to every new buy's notional.
- Impact: `on` = automatically shrinks position sizes in a downtrend/vol-spike (down to a 0.25 floor) without needing an LLM call; `off` = every buy sizes at full weight regardless of market conditions, no automatic risk-off de-leveraging.
- Special values: boolean; `off` also disables the regime-trim logic below (it's gated behind this same flag) and the degraded-data warning path.
- Interactions: gates `REGIME_TRIM_ENABLED` (trim only runs when this is `on`); feeds `regime_multiplier` used alongside the anti-chase overextension haircut (multiplicative, stacks). `REGIME_DEGRADED_MULT` only matters while this is `on`.
- Verdict: `on` is the intended safety posture — turning it off removes an automatic risk-off brake with no LLM-independent replacement.

**`REGIME_DEGRADED_MULT`** — current: `0.5`
- Purpose: when the regime data feed (yfinance: SPY history or VIX) fails to return, size new buys to this fraction instead of assuming full-size "risk-on," because the same outage almost certainly also blinds the sector-concentration cap.
- Impact: higher (toward 1.0) = closer to fail-open (trusts the market is fine when data is missing — riskier); lower (toward the 0.25 floor) = more defensive during outages, but can meaningfully undersize every new trade during a data blip that has nothing to do with actual market risk.
- Special values: clamped to `[0.25, 1.0]` in code (`_FLOOR = 0.25`) — it can never fully block or invert sizing, only shrink it.
- Interactions: purely a `REGIME_FILTER_ENABLED`-gated fallback; conceptually mirrors `MISSING_DATA_MULT` (both are "fail-safe-small, never fail-open" fallbacks for different blind data sources) — the code comments explicitly call out the shared posture (1B.7).
- Verdict: 0.5 (half-size) is the shipped default — a reasonable middle ground; the historical bug this exists to prevent was a silent full-size fail-open during exactly the kind of vol spike that also killed the feed.

**`MISSING_DATA_MULT`** — current: `0.5`
- Purpose: when the **sector-exposure cap** or the **pairwise-correlation guard** can't get data for a new buy candidate (lookup/data miss), size the trade down by this fraction instead of silently letting it through at full size.
- Impact: higher (toward 1.0) = closer to the old fail-open behavior (trusts unknown-sector/uncorrelated names are fine); lower (toward 0) = strongly punishes buys the bot can't fully vet, shrinking them a lot or effectively skipping them at 0.
- Special values: clamped to `[0, 1]` in code. Code default if unset is `1.0` (fail-open) — but this `.env` explicitly overrides that to `0.5`, per the inline comment dated 2026-07-14.
- Interactions: shared fallback multiplier for **two independent guards** (sector cap and correlation guard) — same value applies to both; same "size down, don't fail open" philosophy as `REGIME_DEGRADED_MULT` but for a different data source (sector/correlation lookups vs. yfinance regime feed).
- Verdict: 0.5 is a deliberate tightening from the old unset default of 1.0 (full fail-open) — the comment frames it as closing a real gap, so treat 1.0 here as the "unusually permissive / legacy-risky" setting, not the safe baseline.

**`REGIME_TRIM_ENABLED`** — current: `on`
- Purpose: on a **flip into** risk-off (not every risk-off cycle, just the transition), sell `REGIME_TRIM_PCT` of every held equity name once to de-risk the *existing* book — the regime multiplier alone only throttles new buys, not what's already held.
- Impact: `on` = the book actively shrinks risk-off exposure by force-selling a slice of every position on the transition; `off` = existing holdings ride out a downturn unchanged except for their own stops (multiplier only affects new entries).
- Special values: boolean; code default is `off` — this `.env` explicitly flips it `on` (per the 2026-07-12 dated comment, described as an "operator-delegated decision").
- Interactions: inert unless `REGIME_FILTER_ENABLED=on`; uses `REGIME_TRIM_PCT` for the size; fires once per transition via a persisted `regime_label` latch (won't re-trim every cycle spent in risk-off); the trimmed remainder loses its exchange bracket and is re-protected by the watchdog's default stop/take instead, and its re-entry is subject to `REENTRY_COOLDOWN_HOURS`/`REENTRY_PRICE_GUARD_ENABLED` churn guards (Risk section) if the bot wants back in.
- Verdict: `on` is a deliberately aggressive, human-opted-in posture beyond the shipped default (`off`) — the comment frames it as one leg of a "profit on lows" strategy paired with the risk-off downside-put mandate; treat `off` as the conservative/default baseline.

**`REGIME_TRIM_PCT`** — current: `25.0`
- Purpose: what % of each held equity position to sell in the one-shot regime trim above.
- Impact: higher = more aggressive de-risking on the risk-off flip (locks in more of the existing book's exposure reduction, more realized gains/losses crystallized); lower = a lighter trim, closer to a token gesture that leaves most of the book's risk intact.
- Special values: `0` (or ≤0) effectively disables the trim's effect even if `REGIME_TRIM_ENABLED=on` (nothing to sell).
- Interactions: only fires when `REGIME_TRIM_ENABLED=on`; options positions are explicitly skipped (partial option-structure trims don't make sense — they're already premium-capped/DTE-managed by the watchdog); respects `WHOLE_SHARES_ONLY` (a sub-share trim is skipped rather than leaving fractional dust).
- Verdict: 25% ("bounded" per the inline comment) is framed as a moderate, deliberately capped trim — not a full liquidation — designed to reduce risk without fully exiting winning theses on a regime flip.

**`REGIME_LOOSEN_MIN_CYCLES`** — current: `2` (code default; S-5, run-7)
- Purpose: label **persistence** for the regime read — tighten fast, loosen slow. A fresh read that is *tighter* than the applied label (risk-off < neutral < risk-on) is applied on the cycle it appears; a *looser* read is applied only after this many **consecutive** looser reads. While a tighter label is held its tier caps the multiplier (`min(fresh, 0.70)` for neutral, `min(fresh, 0.40)` for risk-off) and the reason gains `, held neutral (1/2 clean reads)`; the long-run `trend` and `day_change_pct` always pass through from the fresh read. An `unknown` (degraded yfinance) read passes through unchanged, counts for nothing and keeps the held label.
- Evidence: `logs/Sep_10_2026.log` — 08:30 risk-on, 09:22 neutral, 10:14 risk-on, 11:06 neutral, 11:58 risk-on, 12:50/13:43/14:35 neutral = 5 transitions in 8 reads. The breadth confirm is a single-bar threshold on yfinance's LIVE partial bar and the reader had no cross-cycle memory; QQQ oscillated a few tenths of a percent around its 50dma (Sep 9 close +0.72% above, Sep 10 close −0.27% below) while IWM sat −2..−3% below all day, so QQQ was the single swing vote. The 60% exposure ladder and the x0.70 multiplier were applied to different buys under different labels within one hour. With `2`, neutral would have held from 09:22 through the close (the 10:19 SMCI top-up is *likely* ladder-rejected, ~−$878 same day); the 08:35 INTC buy in the first cycle is unchanged (a genuine completed-bar risk-on read).
- Impact: higher = a knife-edge day stays in the tighter tier longer (every buy after the first tighter read is sized at that tier and the existing ladder applies until N clean reads) — cost is missed upside on a genuine reversal; `1` = legacy no-memory (every fresh read applied as-is). `0`/negative clamp to `1`. Never delays a tightening, never loosens a stressed read.
- Special values: in-memory state — a restart FORGETS the held label (the first post-restart read is applied as read, which can be an immediate loosening; tightening is unaffected). Deliberately NOT the persisted `regime_label` latch (that slot keys the once-per-downturn risk-off trim). Greppable: `REGIME HOLD: neutral held (1/2 clean reads); fresh read risk-on x1.00 -> applied neutral x0.70`, `REGIME LOOSEN: neutral -> risk-on after 2/2 clean reads; ...`, `REGIME TIGHTEN: risk-on -> neutral adopted now (...)`.
- Interactions: inert unless `REGIME_FILTER_ENABLED=on`. Stabilises the input to `EXPOSURE_LADDER` / `EXPOSURE_NEUTRAL_PCT` / `EXPOSURE_RISK_OFF_PCT` and the regime multiplier — introduces **no new gross rule**. Side effect on `REGIME_TRIM_ENABLED`: a held risk-off label means a risk-off → neutral → risk-off flap no longer re-trims (the trim fires once per genuine downturn, which is its documented intent). A held `risk-off` also keeps the risk-off put mandate / call gate posture one extra read. Not a hysteresis band: a ±0.5% band on the breadth threshold would have read risk-on ALL of Sep 10 — the opposite outcome.
- Verdict: `2` is the operator-chosen default (decision 3, persistence over band). It changes how big some buys are on knife-edge days → strategy-affecting for pooling; set `1` only to reproduce the pre-run-7 fingerprint.

### Thesis-decay exit (LLM-independent)

**`THESIS_DECAY_ENABLED`** — current: `off`
- Purpose: deterministically SELL a held name once its entry signals are no longer corroborated by fresh bullish data (past a grace age) — a way to exit stale theses without needing the LLM to be up or to explicitly decide "sell."
- Impact: `on` = names can get closed purely because their signal feed stopped confirming the thesis, even if price hasn't hit any stop (proactive thesis hygiene, but signal-outage risk); `off` = only price-based exits (stop/take/trailing/time-stop) and explicit LLM sells can close a position — a stale-but-not-losing name can sit indefinitely.
- Special values: boolean; shipped default is `off` and the inline comment explains why: a transient data outage that blanks signals could force spurious exits.
- Interactions: independent of `MAX_HOLD_DAYS`/`TIME_STOP_MIN_GAIN_PCT` (different trigger — signal absence vs. age+price) but a position can be caught by either; also skips options positions (their bundle lookup is keyed by underlying, so an OCC symbol would always look "uncorroborated" — options already have their own DTE-bounded exits) and the `core_etf`.
- Verdict: keep `off` until the signal feed's reliability is trusted — the comment is explicit that this should only be turned on deliberately, not as a default-on setting.

**`THESIS_DECAY_MIN_AGE_DAYS`** — current: `3`
- Purpose: grace period after entry before a held name becomes eligible for a decay-exit, so a brand-new buy isn't dumped on a single quiet/no-signal day.
- Impact: higher = more tolerance for a fresh position to go quiet before it's at risk of a decay exit (fewer false-positive exits on new buys, slower reaction to genuinely stale theses); lower = decay exit can fire almost immediately after entry (faster reaction, but more risk of exiting a normal short lull in signal coverage).
- Special values: `0` = no grace period, a position is decay-eligible starting day one.
- Interactions: only relevant when `THESIS_DECAY_ENABLED=on`; conceptually parallels `MIN_ADD_INTERVAL_HOURS`/`REENTRY_COOLDOWN_HOURS` churn-guard philosophy elsewhere in the file but is unrelated mechanically.
- Verdict: 3 days is a short, sensible grace window — enough to rule out one noisy data day without giving a genuinely dead thesis a long free pass.

**`THESIS_MIN_SCORE`** — current: `0.1`
- Purpose: the bar a fresh signal must clear to still count as "corroborating" the entry thesis; a held name with no bundle, or only signals below this score, is judged decayed.
- Impact: higher = stricter corroboration requirement (more names will be judged decayed and exited, since weak/neutral signals no longer count); lower (toward 0) = almost any signal at all (even barely positive) keeps the thesis alive, so decay exits become rare — only fires on names with literally no signal or outright bearish ones.
- Special values: `0` = any non-negative signal keeps the thesis corroborated (decay fires mainly on a missing bundle / stale name entirely dropped from coverage).
- Interactions: only relevant when `THESIS_DECAY_ENABLED=on`; conceptually similar in spirit to `MIN_CONVICTION` (a score floor) but applies to ongoing signal corroboration for holdings, not to entry conviction for new buys — the two are not the same gate and can be tuned independently.
- Verdict: 0.1 is a low bar by design — this mechanism is meant to catch names with essentially no remaining signal support, not to second-guess modest ongoing conviction.

**Files referenced:** `/Users/spusapati/Personal/Investment_stratergy/.env` (lines 114-148), `/Users/spusapati/Personal/Investment_stratergy/investment_strategy/config.py`, `risk.py`, `orchestrator.py`, `monitor/watchdog.py`, `decision/engine.py`, `execution/alpaca_client.py`, `backtest.py`.

---

## 4. Capital-Preservation Stack: Equity Floor, No-Leverage Guard, Sector Cap, Earnings Blackout, PDT Guard, Fractional Shares/Churn Guards

*(the "don't blow up" guards)*

| | |
|---|---|
| **Name** | `EQUITY_FLOOR_PCT` |
| **Current value** | `60` |
| **Purpose** | Terminal catastrophe backstop: watchdog flattens every position and **latches** a halt if equity drops to this % of the peak high-water mark. |
| **Impact** | Higher (e.g. 80) = trips after a shallower drawdown (only 20% peak-to-trough tolerated) → more likely to fire on ordinary volatility. Lower (e.g. 40) = tolerates a deeper hole before stopping. `0` = off, no backstop at all. |
| **Special values** | `0` = disabled. |
| **Interactions** | Independent of `MAX_DRAWDOWN_PCT`/`MAX_DAILY_LOSS_PCT` (soft, non-latching risk limits) — this is the hard, one-way one. Once latched, nothing in this file auto-clears it; only deleting `state/risk_state.json` resumes trading. Re-reads and confirms via a second `get_account()` call before flattening, specifically to survive the known equity==cash false-read glitch (2026-07-07 incident) — a single bad snapshot cannot trigger it. |
| **Verdict** | 60 (40% max drawdown from peak) is the deliberate current setting — auto-scales by %, so it's identical in risk terms on the $100k paper book or a $100 live float. Going much above 70-80 risks false-triggering on normal swings; going below ~50 risks masking a real blow-up for too long. |

| | |
|---|---|
| **Name** | `MAX_GROSS_EXPOSURE_PCT` |
| **Current value** | `100.0` |
| **Purpose** | Explicit no-leverage guard: caps total $ deployed across ALL positions as a % of equity, so Alpaca's ~2x margin buying power is never used. |
| **Impact** | 100 = can deploy up to (but not past) 100% of equity, no borrowed money. Lower (e.g. 80) always holds back a cash reserve on top of `MIN_CASH_BUFFER_PCT`. Raising above 100 would deliberately permit leverage/margin — not currently supported by the guard's own comment intent. Rejects a buy outright once at/over the cap rather than resizing to what little gross room remains, when room is ≤ 0; otherwise clamps the notional down to the remaining room. |
| **Special values** | 100 = "no leverage" ceiling; effectively no cap-driven behavior differs above 100 except turning off the leverage guard's whole purpose. |
| **Interactions** | Computed alongside `MIN_CASH_BUFFER_PCT` and `MAX_OPEN_POSITIONS`/`MAX_POSITION_PCT`/`MAX_SYMBOL_EXPOSURE_PCT` — this is the outermost, whole-book cap; the others cap a single trade or symbol first. Also fed into the LLM's decision prompt so Claude is told the ceiling directly and shouldn't even propose a target weight that would breach it. |
| **Verdict** | 100 is the conservative/correct default for a margin account per the inline comment ("keep at 100 for a small margin account so a drawdown can't be amplified by borrowed money"). |

| | |
|---|---|
| **Name** | `MAX_SECTOR_EXPOSURE_PCT` |
| **Current value** | `50.0` (comment: "aggressive... was 30") |
| **Purpose** | Concentration cap — max % of equity the scanner can stack into any one sector, so it can't quietly build one correlated mega-bet (e.g. all big-tech). |
| **Impact** | Higher = allows a heavier single-sector tilt (more correlated risk, higher beta to that sector's drawdowns) in exchange for potentially beating a sector-heavy benchmark like QQQ. Lower = forces diversification, smoother equity curve, likely lower absolute return in a trending sector. Rejects the buy outright once the sector is at/over cap; on missing sector data it fails closed by shrinking the order via `missing_data_mult` rather than skipping the check. |
| **Special values** | `0` would disable the cap entirely (code checks `> 0`) — not currently used, since 50 is set. |
| **Interactions** | Works alongside the pairwise-correlation guard (`MAX_PAIRWISE_CORR`, not in this range) which is the "finer-grained sibling" catching same-factor bets that dodge the sector label. Also reported in the LLM prompt as a hard clamp on any proposed `target_weight_pct`. |
| **Verdict** | Was raised from 30→50 deliberately ("aggressive: allow a real tech tilt to beat QQQ") — this is a knowingly loosened guard, not an oversight; 30 is the more conservative historical baseline. |

| | |
|---|---|
| **Name** | `EARNINGS_BLACKOUT_DAYS` |
| **Current value** | `3` |
| **Purpose** | Blocks NEW opening buys within N days of a name's scheduled earnings report, since gap risk through a print can blow straight past any stop. |
| **Impact** | Higher = safer but excludes more of the calendar from new entries (more missed setups). Lower = allows entries closer to earnings, more gap-risk exposure. Fails **open** — only blocks when an actual earnings date is known; if the date feed is missing, the guard is silently inert for that name. |
| **Special values** | `0` = off (no blackout). |
| **Interactions** | Only affects NEW buys, not top-ups on already-held names or exits. Also injected into the LLM's guardrail prompt so Claude is told not to even propose such a buy. |
| **Verdict** | 3 days is a modest, sensible buffer; the bot's other earnings-related feature (risk-off downside puts, per recent PR) is a separate mandate, not a substitute for this blackout. |

| | |
|---|---|
| **Name** | `PDT_GUARD_ENABLED` |
| **Current value** | `on` |
| **Purpose** | Prevents a sub-$25k **margin** account from tripping the FINRA pattern-day-trader flag (4+ day-trades in 5 business days), which would freeze the account to closing-only. Pauses new opening buys at the line; closes are still allowed. |
| **Impact** | `off` = the guard never checks day-trade count or the PDT flag — a margin account under $25k could get flagged and locked to closing-only by the broker itself, outside the bot's control. `on` proactively pauses buys before that happens. |
| **Special values** | Inert (returns immediately) once `account.equity >= $25,000` (hardcoded `_PDT_MIN_EQUITY`), and inert for **cash** accounts (which report `pattern_day_trader=False`/`daytrade_count=0` and are PDT-exempt by design — the recommended setup for small accounts). |
| **Interactions** | Paired with `MAX_DAY_TRADES_UNDER_25K` — this flag is the on/off switch, that one is the threshold. Both only matter on a margin account under $25k equity. |
| **Verdict** | Leave `on` for any margin account under $25k (which is exactly this bot's live-float regime, per the solo small-float plan); safe to leave on unconditionally since it self-disables above $25k equity or on a cash account. |

| | |
|---|---|
| **Name** | `MAX_DAY_TRADES_UNDER_25K` |
| **Current value** | `3` |
| **Purpose** | Threshold: once day-trades in the trailing 5 business days hit this count (on a sub-$25k margin account), pause new opening buys before the broker's own PDT flag (which trips at 4) can freeze the account. |
| **Impact** | Higher (e.g. 4) = waits until literally at the PDT line, riskier — one more same-day round-trip could flag the account before the bot reacts. Lower (e.g. 1-2) = more conservative, pauses new buys sooner, more false "pauses" on accounts that were never going to hit 4 that week. |
| **Special values** | None documented; it's compared with `>=`, so setting it above the real regulatory trigger (4) would defeat the guard's purpose. |
| **Interactions** | Only active when `PDT_GUARD_ENABLED=on` and equity < $25,000. |
| **Verdict** | 3 is the sane choice — it pauses one trade *before* the actual regulatory line at 4, giving margin for a same-day stop-out that counts as a day-trade. |

*(Note: `MAX_DAY_TRADES_UNDER_25K` is not itself one of the ~117 variables this document is scoped to verify, but it is `PDT_GUARD_ENABLED`'s direct sibling knob in the same `.env` block, so it is documented here for completeness — see it also flagged in the [Completeness check](#completeness-check).)*

| | |
|---|---|
| **Name** | `FRACTIONAL_ENABLED` |
| **Current value** | `on` |
| **Purpose** | Allows sub-share, notional-dollar buys — essential for a small ($100-$1000) account to diversify into many names instead of being limited to whichever stocks are cheap enough to buy a whole share of. |
| **Impact** | `off` = every buy must afford at least one whole share; on a small account this excludes most screened names and shrinks the tradable universe sharply. `on` = any budget above the min-order floor can be deployed, but a fractional position carries **no exchange-side bracket** — its only stop-loss protection is the watchdog polling loop in a killable process. |
| **Special values** | n/a (boolean). |
| **Interactions** | Directly overridden by `WHOLE_SHARES_ONLY` (defined elsewhere in .env, defaults off) — when that's `on`, it forces whole-share flooring for every NEW buy regardless of this flag, specifically to close the "unprotected fractional position" gap (GA-2.3). The two are mutually exclusive in effect: this repo's solo/small-float posture keeps `WHOLE_SHARES_ONLY=off` and relies on `FRACTIONAL_ENABLED=on` + the watchdog instead. |
| **Verdict** | `on` is correct and necessary at the current $100-1000 live-float size per the accepted trade-off (max loss bounded by float size + per-trade risk cap); should flip to whole-shares-only once the account is $10k+, per the code comment's own guidance. |

| | |
|---|---|
| **Name** | `MIN_ORDER_USD` |
| **Current value** | `1.0` |
| **Purpose** | Absolute-dollar floor below which an order isn't worth placing (Alpaca's own minimum is $1); prevents dust orders that pay spread/fees for near-zero notional. |
| **Impact** | Raising it blocks smaller top-ups/starter positions outright (rejected, not resized down) once available budget falls under this floor. Lowering toward 0 would let genuinely tiny/dust orders through, which is exactly what the churn guards below exist to stop. |
| **Special values** | Effectively the account-size-agnostic floor; combines with `MIN_ORDER_PCT` as a `max()` — whichever is bigger wins. |
| **Interactions** | Paired with `MIN_ORDER_PCT`: real floor used everywhere (risk.py sizing, orchestrator budget checks, `execution/alpaca_client.py` pre-submit check) is `max(MIN_ORDER_USD, equity * MIN_ORDER_PCT / 100)`. Also gates the daily concentration brake's headroom check (a day's remaining per-symbol budget below this floor is a hard reject, not a resize). |
| **Verdict** | 1.0 matches Alpaca's own floor — safe as-is; raising it mainly matters once combined with a bigger `MIN_ORDER_PCT` on a larger book. |

| | |
|---|---|
| **Name** | `MIN_ORDER_PCT` |
| **Current value** | `0.05` |
| **Purpose** | Dust guard that scales the minimum order size with equity (e.g. ≈$49 on a $98k book) so a large account can't fire $2-8 top-ups that just bleed to spread — the exact failure the 2026-07-06 log documented (10 same-day LLY top-ups while every diversifying buy starved on "Budget $0.00"). |
| **Impact** | Higher = fewer, larger top-ups; small accounts may get blocked from adding at all if per-trade budgets can't clear the %. Lower/0 = reverts to the flat `MIN_ORDER_USD` floor regardless of account size, re-opening the dust-order failure mode on bigger books. |
| **Special values** | `0` = inert, floor becomes just `MIN_ORDER_USD`. |
| **Interactions** | Combined via `max()` with `MIN_ORDER_USD` everywhere sizing happens (risk.py, orchestrator.py, alpaca_client.py). Also feeds the daily-symbol-deploy concentration brake's "headroom < min order = reject, don't resize" rule. |
| **Verdict** | 0.05% is the value introduced specifically to fix the 2026-07-06 churn incident — don't drop it back toward 0 without re-checking that log; a $500 float still trades fine at this level because `MIN_ORDER_USD` is the floor there. |

| | |
|---|---|
| **Name** | `MIN_ADD_INTERVAL_HOURS` |
| **Current value** | `4` |
| **Purpose** | Churn guard: refuses a top-up buy of a symbol already bought less than this many hours ago, so adds are spaced deliberate decisions rather than a reflex fired every 30-min decision cycle. |
| **Impact** | Higher = fewer, more deliberate top-ups, slower to compound into a winner intraday. Lower/0 = allows rapid repeated top-ups of the same name every cycle — the direct cause of the 2026-07-06 "LLY bought 10x in one day" incident this guard was built to stop. |
| **Special values** | `0` = off. |
| **Interactions** | Sibling guard to `REENTRY_COOLDOWN_HOURS` (same incident, same comment block) — this one covers adding to a still-held position; that one covers re-buying after a full exit. Both fail open on missing clocks (first-ever entry) and are checked in both `risk.py` (final gate) and `orchestrator.py` (pre-sizing early-exit, cheaper). |
| **Verdict** | 4h is the fix-commit value for the LLY churn incident — don't reduce without expecting a repeat of that failure mode; safe to raise for a more patient book. |

| | |
|---|---|
| **Name** | `REENTRY_COOLDOWN_HOURS` |
| **Current value** | `24` |
| **Purpose** | Churn guard: blocks a fresh BUY of a symbol that was exited (via trail/stop/take/time/decision) less than this many hours ago — an instant re-buy pays the spread twice and usually chases the same falling knife the exit just avoided. |
| **Impact** | Higher = longer lockout after any exit, more missed genuine re-entries on a fast reversal. Lower/0 = re-entries allowed almost immediately, reopening the "instant re-buy of a just-stopped-out name" failure mode. |
| **Special values** | `0` = off. Only applies when the symbol isn't currently held (`account.position_for(...) is None`). |
| **Interactions** | Paired with `REENTRY_PRICE_GUARD_ENABLED`/`REENTRY_PRICE_OVERRIDE_COMPOSITE` (outside this range): even after the 24h clock lapses, re-buying **at or above** the exit price is separately blocked unless the composite score clears a high override bar (raised to 1.25 after the Jul 21 SPCX incident where a mild +0.66 composite let a re-entry chase price straight into a stop). Also surfaced in the LLM's guardrail prompt as "a name you SELL is locked out... rotate deliberately, not for churn." |
| **Verdict** | 24h is the fix-commit default from the same LLY churn incident; treat it as a floor, not a suggestion — the price-guard override was subsequently tightened (0.5→1.25) specifically because 24h alone wasn't catching same-day-adjacent chase re-entries once the clock expired. |

**Files referenced:** `/Users/spusapati/Personal/Investment_stratergy/.env` (lines 114-148), `/Users/spusapati/Personal/Investment_stratergy/investment_strategy/config.py`, `risk.py`, `orchestrator.py`, `monitor/watchdog.py`, `decision/engine.py`, `execution/alpaca_client.py`, `backtest.py`.

---

## 5. Position Sizing (Kelly/Vol-Target), Options (Defined-Risk) Gates

### Position sizing (survival-first: vol-targeted, fractional-Kelly)

**`KELLY_FRACTION`** — current: `1.0`
- Purpose: multiplier in `RiskManager._sized_weight_pct` that scales a new position's target weight by conviction × a volatility ratio — the "how much of the Kelly-optimal bet to actually take" knob.
- Increase → bigger $ per position (linear: 1.0 sizes exactly 2× what 0.5 half-Kelly would). Decrease → smaller positions, slower compounding, more names needed to deploy capital. Disable (≤0) → the whole vol/conviction sizing model turns off and every buy just takes the flat `MAX_POSITION_PCT` weight.
- Special values: `0` or negative = sizing model bypassed entirely (`if kelly_fraction <= 0: return max_position_pct`).
- Interactions: feeds `weight_pct = min(proposal.target_weight_pct, sized_pct, MAX_POSITION_PCT)` — `MAX_POSITION_PCT` (12.0 in this file) is the hard ceiling that always wins regardless of how big Kelly sizing wants to go. Multiplies with `TARGET_ANNUAL_VOL_PCT`'s vol_ratio in the same formula. `MAX_TRADE_RISK_PCT` (3.0) is a separate, stop-width-dependent $ cap applied further downstream.
- Verdict: config.py's shipped default is `0.5` (textbook half-Kelly). This file runs full Kelly and says so in-line ("aggressive... was 0.5") — a deliberately more aggressive-than-default setting, not a conservative one.

**`TARGET_ANNUAL_VOL_PCT`** — current: `45.0`
- Purpose: annualized volatility budget (%) a position is sized to; `vol_ratio = min(target/vol, 1.5)` — quiet names get upsized toward the budget, volatile names get downsized.
- Increase → same-volatility names consume a smaller share of the (bigger) budget, so vol_ratio rises toward its 1.5 cap more often → bigger average sizes across the book. Decrease → tighter sizing everywhere, volatile names penalized harder.
- Special values: vol_ratio is hard-capped at `1.5x` no matter how low a name's vol is ("cap upsizing on calm names"). If realized volatility is missing/zero, falls back to an assumed 60% annualized vol so it fails safe (sizes DOWN, never up).
- Interactions: multiplies with `KELLY_FRACTION` and conviction in the same sizing formula; result is still clamped by `MAX_POSITION_PCT`.
- Verdict: config.py default is `25.0`; this file's `45.0` is nearly 2× that, flagged in-line as "aggressive... (was 25)". Combined with full Kelly above, this is a materially higher risk-budget stance than the shipped defaults — reasonable only for a deliberately aggressive small/experimental account, not a default-safe posture.

### Options (DEFINED-RISK only; off by default)

**`OPTIONS_ENABLED`** — current: `on`
- Purpose: master gate for the entire options path — data eligibility, LLM prompt guidance, `OptionsHelper` construction, and whether `risk.evaluate_option` does anything but reject.
- On → `OptionsHelper` is built (orchestrator.py), Claude is told the DTE window + premium cap in the prompt, the screener keeps bearish-only candidates it would otherwise drop (a bearish name with options off is "nothing to do but HOLD," per aggregator.py's own comment), and `preflight.py` checks the Alpaca account is options-level-2+ approved at startup. Off (default) → every option proposal is rejected with "Options trading disabled," bearish-only screener hits are dropped pre-prompt to save tokens, `OptionsHelper` is never constructed.
- Special values: `off` is the documented default — this .env explicitly flips it on.
- Interactions: gates all 8 knobs below plus `OPTIONS_CHAIN_SIGNAL` — config.py's comment says to "flip together with OPTIONS_ENABLED." Deliberately has **no** shared slot cap with equity's `MAX_OPEN_POSITIONS`; a code comment explains the old shared cap structurally starved every option proposal (equity fills 15/15 first), so options concurrency is bounded only by its own gates below.
- Verdict: capital-risk from enabling this is small and bounded — every leg must pass the defined-risk check (max loss = premium paid, no naked/uncovered shorts allowed) before `MAX_OPTION_PREMIUM_PCT` even applies.

**`MAX_OPTION_PREMIUM_PCT`** — current: `1.0`
- Purpose: caps the net debit (premium paid) of one options play at this % of equity — since it's a debit-only structure, that % *is* the play's max loss.
- Increase → bigger $ options bets, bigger worst-case loss per play (still fully bounded). Decrease → fewer contracts fit the budget, more "premium exceeds budget" rejections, especially on higher-priced names.
- Special values: effective cap is `min(this %, proposal.max_premium_usd)` if the LLM proposes an even tighter dollar cap itself.
- Interactions: combines with `MAX_OPTION_POSITIONS` for worst-case aggregate exposure ≈ `MAX_OPTION_PREMIUM_PCT × MAX_OPTION_POSITIONS` of equity (≈1%×3 = ~3% here, all-loss scenario).
- Verdict: `1.0` is config.py's own default — a conservative per-play size given the whole premium can go to zero.

**`OPTION_DIRECTION_GATE`** — current: `on`
- Purpose: deterministic direction discipline for option debits — they must trade WITH the long-run market trend (SPY vs its 200-day SMA, the regime reader's `trend` field): CALL structures only while the trend is up, PUT structures only while it's down.
- On → `risk.evaluate_option` rejects a bullish call structure when the trend is "down", and a bearish put structure when the trend is "up" — with three carve-outs for puts in an uptrend: (a) the NAME's own long-run trend is broken (price below its own 200-day per the technical signal — the insider-sell/bearish-slate pipeline shorts single-name breakdowns in any tape), (b) the put hedges an equity position this account HOLDS in the same name (insurance), and (c) the regime label reads `risk-off` (a vol spike inside an uptrend — the risk-off put mandate asks for puts, so its own gate never fights it). Mixed call+put structures are rejected outright (no approved defined-risk shape mixes rights). Off → direction is unconstrained, the pre-Jul-27 behavior.
- Special values: an "unknown" trend (degraded yfinance read) passes everything — act only on data we have; the degraded regime multiplier already shrinks the premium budget.
- Interactions: **requires `REGIME_FILTER_ENABLED=on`** — with the filter off the orchestrator never computes a trend (it stays ""), so this gate passes everything and the premium-cap scaling is off too (a startup WARNING flags the combination). Direction is derived from the LEGS' rights, never the declared `option_strategy` name (the shape check doesn't verify a "bear_put_spread" actually uses puts). The prompt tells Claude the same rule (static guidance + a per-cycle "## Market regime" block), so gate rejections should be rare — the gate is the backstop, not the primary mechanism. The premium cap is also scaled by the regime multiplier whenever `REGIME_FILTER_ENABLED=on`, so option debits shrink in risk-off exactly like equity sizing.
- Verdict: `on` matches the account's mandate — calls in an up market, puts in a down market, single-name breakdowns and hedges always allowed.

**`OPTIONS_CHAIN_SIGNAL`** — current: `on` (C.4 signal)
- Purpose: pulls per-name ATM IV, put/call IV skew, and put/call OI lean from Alpaca's free option snapshots so Claude proposes options from real chain data instead of guessing blind.
- On → 2 extra REST calls/name/cycle (capped by `OPTIONS_CHAIN_MAX_SYMBOLS=25`, not in this range but same gate group). Off → options proposals (if enabled) happen with no IV/skew context — more likely to misjudge premium richness or get rejected downstream by DTE/liquidity gates.
- Special values: also silently no-ops if `alpaca_api_key` is unset (fails to `False`) — moot here since Alpaca creds are configured.
- Interactions: config.py explicitly documents this as meant to move in lockstep with `OPTIONS_ENABLED` — enabling one without the other is either wasted API calls (signal on, options off) or blind options trading (options on, signal off). Both are on here, matching the intended pairing.
- Verdict: no material downside to leaving on whenever `OPTIONS_ENABLED=on`; it's pure informational upside for the LLM's option picks.

**The ten per-gate knobs are all commented out here** (`#OPTION_STOP_LOSS_PCT` … `#MAX_OPTION_CONTRACTS`, lines 160–169) — every one falls through to its `config.py` default. Uncommenting overrides the live default without touching code.

| Var (default) | Purpose | Increase / Decrease / 0 | Interactions / verdict |
|---|---|---|---|
| `OPTION_STOP_LOSS_PCT` (50) | Watchdog closes the WHOLE option structure (options have no exchange bracket — this loop is their only protection) when group P&L ≤ −stop% of premium paid. | Higher = more room before stop-out (bigger realized losses on failed theses); lower = cuts losers earlier, risks noise-driven exits. `0` disables the stop check entirely. | Checked in the same if/elif chain as take-profit and close-DTE (stop checked first); ratio to take-profit sets the sleeve's reward:risk (default 2:1). 50% is wide relative to typical premium decay but reasonable given the ~1%-of-equity premium cap already bounds absolute loss. |
| `OPTION_TAKE_PROFIT_PCT` (100) | Watchdog closes the structure at +100% of premium (doubled). | Higher = lets winners run longer (risks giving back gains to theta/IV crush, no trailing logic on options); lower = locks in profit sooner, caps upside. | Same if/elif chain, checked after stop. 100/50 = 2:1 default reward:risk is a sane baseline for a decaying-premium instrument. |
| `OPTION_CLOSE_DTE` (3) | Force-closes any structure once DTE ≤ this, ahead of assignment/terminal-theta risk. | Higher = closes earlier (sacrifices time value, avoids gamma/pin risk); lower = holds closer to expiry (more theta captured if right, more assignment risk). `0` disables the expiry-based close — positions could ride to expiry unmanaged except by stop/take. | Must sit below `MIN_OPTION_DTE` or a freshly entered position could be force-closed almost immediately; here 3 < 7 leaves a 4-day buffer — no conflict. |
| `MIN_OPTION_DTE` (7) | Entry gate: refuses legs expiring sooner than this (too close, theta/assignment dominate). | Higher = only calmer, pricier, longer-dated legs qualify; lower = allows cheap near-term (theta/gamma-heavy) legs. | Also injected verbatim into the LLM prompt as the legal expiry-date window (`decision/engine.py`). Must be < `MAX_OPTION_DTE` or the accepted window is empty. |
| `MAX_OPTION_DTE` (60) | Entry gate: refuses legs expiring further out (mostly buying unneeded time value). | Higher = allows longer-dated/cheaper-theta contracts; lower = forces near-term-only, faster theta burn. | **Gotcha**: `signals/options_chain.py`'s own DTE window (7–45d) is *hardcoded to match these defaults*, not read live from this env var — changing `MAX_OPTION_DTE` here does not move the IV/skew signal's scan window, a latent mismatch if this is ever overridden. |
| `MIN_OPTION_OPEN_INTEREST` (100) | Per-leg liquidity floor in `_legs_liquid` — rejects illiquid legs that can't be exited near mid. | Higher = only well-traded chains pass, fewer proposals (most screener-discovered small/micro-caps have no chain at all); lower = tolerates thin books, worse exit fills if the thesis reverses. `0` disables the OI check. | Fails **open** (passes) when liquidity data or the OI field itself is missing — only acts on data actually available. Paired with `MAX_OPTION_SPREAD_PCT` in the same gate; both must pass. |
| `MAX_OPTION_SPREAD_PCT` (10) | Per-leg max relative bid-ask spread — round-trip friction ceiling for options legs. | Higher = tolerates wider markets (more slippage, and options spreads are proportionally wider than equities to begin with); lower = only tight markets qualify. `0` disables the check. | Same gate as OI floor. 10% is loose by equity standards (equities' `EST_SLIPPAGE_PCT` default is 0.10% one-way) but is a realistic "not egregious" ceiling for options specifically. |
| `MAX_OPTION_POSITIONS` (3) | Caps concurrent **underlyings** with open option structures (adding legs to an already-held underlying doesn't cost a new slot). | Higher = more simultaneous theta-decaying bets, more watchdog monitoring surface; lower = forces selectivity. `0` disables the cap (unlimited concurrent underlyings). | **Deliberately independent** of the equity `MAX_OPEN_POSITIONS` slot cap — a code comment documents this as a fix for a 2026-07-13/14 incident where options were structurally starved by a shared cap. Combines with `MAX_OPTION_PREMIUM_PCT` to bound worst-case aggregate options loss (~3% of equity at 1%×3 here). |
| `MIN_OPTION_PREMIUM` (0.10) | Per-leg mid floor ($/share) in `evaluate_option`: the CHEAPEST leg must price at/above this. Sub-floor = a deep-OTM / illiquid junk contract whose penny price mints a huge, un-exitable contract count. | Higher = only real, near-the-money, delta-bearing contracts qualify (fewer, higher-quality option plays); lower = tolerates cheaper/further-OTM legs. `0` disables the floor. | **The core 2026-07-23 T blowup fix.** Floors the per-leg mid, not the net debit, so a legitimately tight debit spread still passes. Fails **open** on `None` (no per-leg data) — the `est_premium<=0` gate is the backstop for a quote-less leg. Upstream, `_mid_price` now returns 0.0 on a one-sided NBBO, so a stale one-sided quote never even reaches this floor. |
| `MAX_OPTION_CONTRACTS` (50) | Hard ceiling on the contract count per structure, applied AFTER the debit-cap sizing. Even within `MAX_OPTION_PREMIUM_PCT`, a cheap premium can size a monster order that itself moves a thin book or can't fill. | Higher = allows larger lots on cheap contracts; lower = tighter size discipline. `0` disables the cap. | Clamps (does not reject) — deploying **less** than the debit cap is always safe. The T blowup sized **900 contracts** off a $0.01 premium; this caps that at 50 regardless of how cheap the premium reads. |
| `PROXY_PUT_PREFER_MONTHLY` (on) | Run-7 S-3: the proxy put spread (`PUT_PROXY_ETF`) is built OI-aware — third-Friday (monthly) expiry first inside the [25,50] DTE window, and only strikes whose open interest clears `MIN_OPTION_OPEN_INTEREST` on BOTH legs (non-qualifying strikes are skipped downward; None-OI legs are excluded whenever the chain carries any OI). | `off` = expiries ranked by nearest-mid-DTE only (strikes still OI-qualified). The pure legacy pick (nearest-mid expiry, highest strike <= spot, no OI read) applies only when the whole chain reports no OI or the OI floor is `0`. | Run-6 evidence: the legacy pick handed the gate the thin Oct-9 weekly three times (Sep 1 IWM 291P OI 38, Sep 3 294P OI 47 -> `REJECT ... open interest < 100 floor`) while the Oct-16 monthly carried >= 5,000 OI on every candidate strike — 2 of 3 in-window proxy attempts were self-inflicted. Greppable: `PROXY PUT PICK: IWM 2026-10-16 291/276 (OI 9781/14135, monthly, 45d) over legacy 2026-10-09 291/276 (OI 38/1827)`; a paginated chain logs a WARNING. |
| `OPTION_STRIKE_SNAP` (on) | Run-7 S-4: a model-proposed SINGLE-NAME put structure (long_put / bear_put_spread; not the proxy, not index underlyings, not calls) is re-struck in `_handle_option` BEFORE premium sizing and the liquidity gate (`OptionsHelper.snap_legs_to_liquid`): a long-put strike farther than `OPTION_STRIKE_MAX_MONEYNESS_PCT` from spot re-targets the at-the-money strike, then each leg moves to the nearest strike on the same expiry (else the nearest third Friday within +/-7 d, inside the DTE window) whose open interest clears `MIN_OPTION_OPEN_INTEREST` and whose NBBO is two-sided within `MAX_OPTION_SPREAD_PCT`, within 5% of spot; a spread keeps its width, both legs on ONE expiry; right/side/ratio never change. | `off` = run-6 behaviour, legs judged exactly as proposed. When no qualifying strike exists, the chain read fails, or no spot is available, the model's legs stand and the existing OI/spread gate rejects them (the proxy-put fallback then fires as before). | Run-6 evidence: all five single-name put failures shared one mechanism — strikes far from the money on chains liquid near ATM (HD 400P ~25% ITM OI 2; LTH 30P 28% OTM OI 26; LYV 150/140P 12-18% OTM OI 5/87; SCI 75P 6% OTM OI 5; AAL 11P/10P 14-22% OTM, spreads 12-91%) while HD 320P carried 1,744 OI, LTH 40P 843, LYV 170P 368, SCI 77.5P 161, AAL 12P 9,581 — the prompt rendered no spot price. Greppable: `STRIKE SNAP: HD 2026-10-16 400P (OI 2, 24.6% ITM) -> 320P (OI 1744, 0.3% OTM); unsnapped would be rejected: open interest 2 < 100` (`... kept — in band and OI-qualified` / `... none — no OI-qualified put strike within ...` otherwise); the journal `reason` and ledger `risk_note` carry `[STRIKE SNAP: model legs 2026-10-16 400P -> 320P (spot 321.05 broker)]`. The prompt's candidate header now also shows `— spot $321.05` (the technical feed's price) whenever options are on, so the model can pick ATM itself. |
| `OPTION_STRIKE_MAX_MONEYNESS_PCT` (10) | Run-7 S-4: max abs(strike − spot) / spot for a single-name long-put leg before the snap re-targets it to the at-the-money strike (two-sided: ITM and OTM alike). | Higher = the model's strike preference survives farther from the money (at 15 a 12%-OTM LYV-style pick keeps its own target and still dies on OI unless a qualified strike sits within 5% of it); lower = more picks land at ATM; `0` = every single-name put re-targets ATM. | Inert when `OPTION_STRIKE_SNAP=off`. HD 400P at ~25% ITM and LTH 30P at 28% OTM both died on OI in run-6. |

**Files referenced**: `/Users/spusapati/Personal/Investment_stratergy/.env` (lines 149–170), `/Users/spusapati/Personal/Investment_stratergy/investment_strategy/config.py` (lines 393, 477, 545–558), `/Users/spusapati/Personal/Investment_stratergy/investment_strategy/risk.py` (lines 154–192, 684–875), `/Users/spusapati/Personal/Investment_stratergy/investment_strategy/monitor/watchdog.py` (lines 551–586), `/Users/spusapati/Personal/Investment_stratergy/investment_strategy/decision/engine.py` (lines 163–174, 204, 263–268, 419–423), `/Users/spusapati/Personal/Investment_stratergy/investment_strategy/screener/aggregator.py` (lines 80–90), `/Users/spusapati/Personal/Investment_stratergy/investment_strategy/preflight.py` (lines 116–130), `/Users/spusapati/Personal/Investment_stratergy/investment_strategy/orchestrator.py` (lines 141, 242), `/Users/spusapati/Personal/Investment_stratergy/investment_strategy/signals/options_chain.py` (lines 27–51).

---

## 6. Market Scanner/Discovery, Benchmark, Core-Satellite Fill

### Market scanner (discovery)

**`SCREENER_ENABLED`** — current: `on`
- Purpose: master on/off switch for the whole discovery layer (`ScreenerConfig.enabled`, loaded via `_flag("SCREENER_ENABLED", "on")`).
- Read in `orchestrator.py`: gates both the initial "do we even need a scan" check (line 146: `if not self.watchlist and not cfg.screener.enabled` — refuses to run with nothing to trade) and the actual scan call (`self.screeners.scan(...) if self.cfg.screener.enabled else []`).
- Increasing/enabling: bot surfaces brand-new tickers every cycle beyond WATCHLIST/holdings — more opportunity, more signal-gathering time (~3+ min across providers) and more Claude input tokens per cycle.
- Disabling (`off`): bot becomes a pure watchlist/holdings manager — no new names ever enter the book, only ones already in `WATCHLIST` or already held. Safe fallback if scanning costs (tokens, API load, or noisy candidates) become a problem.
- Interactions: if this is `off`, `SCREENER_SOURCES`, `MAX_DISCOVERED_CANDIDATES`, `SCREENER_MIN_SCORE`, `OPTIONS_FLOW_SCAN_LIMIT`, and `INSIDER_SCAN_LIMIT` are all inert.
- Verdict: leave `on` unless debugging token spend or chasing a noisy-candidate incident — this is the entire "discovery" half of the bot's edge.

**`SCREENER_SOURCES`** — current: `congress,insider,options_flow,robinhood,robinhood_scans`
- Purpose: which individual screeners (`screener/aggregator.py`'s `_REGISTRY`) run each cycle: `congress` (Quiver congressional trading), `insider` (SEC EDGAR Form-4 open-market buy clusters), `options_flow` (Alpaca most-actives + call/put imbalance), `robinhood` (Daily Movers + 100 Most Popular), `robinhood_scans` (RH custom/agentic scans). A 6th source, `wallstreetbets`, exists in the registry but isn't listed here.
- Impact: each name added widens discovery breadth but adds a data source that can fail/degrade independently (each source is wrapped in `safe_scan()` and just contributes `[]` on failure — a dead source silently produces zero candidates, not a crash). Unknown source strings are logged as a warning and ignored, not an error.
- Special values: `options_flow` needs a **paid** Polygon options plan per the inline comment (free tier → 0 candidates, confirmed independently by the Jul-2026 API tier audit which rewired the *signal* path off Polygon to Alpaca's free daily bar — this *screener*'s options_flow source, however, actually reads via Alpaca's own indicative snapshots per `options_flow_feed.py`'s docstring, so re-check this comment's Polygon claim against current code if candidates stay at 0). `robinhood`/`robinhood_scans` need `ROBINHOOD_ENABLED=on` plus a working OAuth token — otherwise those two sources' `enabled` property returns `False` and they contribute nothing.
- Interactions: `options_flow` here reuses `signals/options_flow.py`'s `OptionsFlowProvider`; `congress` shares a cached Quiver pull with the *signal* layer's congress screener (not a duplicate API call). `robinhood`/`robinhood_scans` depend on the Robinhood OAuth block elsewhere in `.env`.
- Verdict: current 5-source mix is broad/aggressive discovery; dropping to `congress,insider` alone is the conservative, zero-incremental-API-cost baseline (both are free/keyless-ish).

**`MAX_DISCOVERED_CANDIDATES`** — current: `18`
- Purpose: hard cap on new names the aggregator surfaces per cycle after scoring/ranking (`ScreenerConfig.max_candidates`, read in `aggregator.py` line 91: `cands[: self.cfg.screener.max_candidates]`).
- Impact of increasing: more discovered names reach Claude's decision slate — better breadth, but the inline comment (X.4 note) says this was explicitly "lowered to trim Claude input tokens," so raising it directly increases prompt size/cost per cycle. Impact of decreasing: fewer, only the highest-conviction (by `abs(score)`) names get through — cheaper cycles, but a wide multi-source scan can get throttled down to a handful, discarding valid corroborated candidates.
- Special values: comment history notes 12 was tried and "re-throttled the now-3-feed discovery at the aggregator" — i.e., 12 was found too tight once more sources were added; 18 was chosen to "let the full breadth actually reach the model."
- Interactions: works together with `SCREENER_MIN_SCORE` (score filter happens first, then this cap on the survivors) and indirectly with `SCREENER_SOURCES` (more sources → more candidates competing for the same 18 slots).
- Verdict: 18 is a tuned-by-incident middle ground for a 5-source setup; don't drop below ~12 without also trimming sources, or good candidates get silently cut.

**`SCREENER_MIN_SCORE`** — current: `0.2`
- Purpose: minimum absolute smart-money score a candidate needs to survive the aggregator's filter (`ScreenerConfig.min_score`; `aggregator.py` line 84-89: kept if `score >= min_score`, or if options are enabled and `score <= -min_score` for put candidates).
- Impact of increasing: fewer, higher-conviction discovered names — reduces noise but can also filter out legitimate weaker-signal names (e.g., a single Robinhood "100 most popular" hit scores only 0.35, so raising the bar much above that drops that source's contribution almost entirely). Impact of decreasing/0: nearly everything scanned gets through, flooding the candidate slate and burning `MAX_DISCOVERED_CANDIDATES` slots on weak names.
- Interactions: reused a second time in `orchestrator.py`'s `_bearish_lean` (line 1788: `bar = max(0.2, self.cfg.screener.min_score)`), which decides whether a not-held, equity-blocked name stays on the slate as a PUT candidate — so this one knob tunes both discovery breadth AND the bearish-put admission bar. Also gated by `options_enabled` (`self.cfg.risk.options_enabled`) for the negative-score branch.
- Special values: floored at 0.2 wherever reused as the bearish-lean bar, even if you set the env var lower.
- Verdict: 0.2 is the practical floor already baked into the code (`max(0.2, ...)`); setting it lower than 0.2 only affects the discovery-side filter, not the bearish-lean reuse — expect a widening gap between "candidates discovered" and "candidates treated as put-worthy" if you do.

**`OPTIONS_FLOW_SCAN_LIMIT`** — current: `40`
- Purpose: size of the Alpaca most-actives pool the `options_flow` screener scans for unusual call/put imbalance (`ScreenerConfig.options_flow_scan_limit`; used in `options_flow_feed.py` line 78 as `top=` on the `MostActivesRequest`).
- Impact of increasing: wider net over the day's most-active names → more chances to catch unusual flow, but proportionally more per-symbol option-snapshot probes (API calls), all against the free/indicative Alpaca options feed already in use elsewhere.
- Impact of decreasing: cheaper/faster scan, but may miss the day's genuine flow leaders if they fall just outside the shrunk top-N.
- Interactions: independent of `INSIDER_SCAN_LIMIT` despite a shared history — the insider screener used to wrongly reuse this same value (see next entry) before being split out.
- Verdict: 40 is a reasonable default; this only matters if `options_flow` is in `SCREENER_SOURCES` and Alpaca's options data is actually returning content.

**`INSIDER_SCAN_LIMIT`** — current: `100`
- Purpose: how many of SEC EDGAR's most-recent Form-4 filings (across ALL issuers) the insider screener parses per cycle to find clustered open-market buys (`ScreenerConfig.insider_scan_limit`; read in `insider_feed.py` line 48).
- Impact of increasing: wider lookback across the "latest filings" feed → better odds of catching a genuine buy cluster (the inline comment stresses open-market buys are rare, so scanning wide matters), at the cost of more EDGAR XML fetches (rate-limited to ~10 req/s per the module's own politeness note).
- Impact of decreasing: faster scan, but higher chance of missing a cluster that's just outside the smaller window, especially on a high-filing-volume day.
- Special values / history: this used to silently share `OPTIONS_FLOW_SCAN_LIMIT`'s value of 40 (a bug — "wrongly sharing options_flow_scan_limit=40" per the code comment) before being split into its own knob and raised to 100.
- Interactions: no direct overlap with other vars now that it's split from `OPTIONS_FLOW_SCAN_LIMIT`; still bounded by `SCREENER_MIN_SCORE`/`MAX_DISCOVERED_CANDIDATES` downstream in the aggregator.
- Verdict: 100 is the corrected, intentionally-wide setting; don't drop it back toward 40 — that regressed to the known bug's behavior.

### Benchmark to beat

**`BENCHMARK_SYMBOL`** — current: `QQQ`
- Purpose: the index the bot measures itself against — `Config.benchmark_symbol` (`os.getenv("BENCHMARK_SYMBOL", "QQQ").upper()`), consumed by `BenchmarkTracker` (`benchmark.py`) to compute account return vs. benchmark return, excess return, and an information ratio, fed to Claude every decision cycle as a one-line context string ("Aim for positive excess return — selection alpha, not just market beta").
- Impact of changing: switching `SPY` ↔ `QQQ` (the two options named in the inline comment) changes what "beating the market" means to the model — QQQ (tech-heavy, higher beta) sets a higher bar in bull runs and a harsher one in tech drawdowns than SPY's broader mix. It's purely a comparison/prompting input — it does not by itself change position sizing or risk limits.
- Interactions: also passed to `preflight.py`'s config sanity check (just for display/logging) and conventionally kept equal to `CORE_ETF` ("matches the benchmark" per the `.env` comment) so the passive core sleeve tracks the same yardstick the LLM is graded against — but the two variables are independent; nothing enforces they match.
- Verdict: no "safe range" — this is a philosophical choice. Keeping it aligned with `CORE_ETF` avoids a confusing situation where the core holding and the excess-return yardstick diverge.

### Core-satellite fill (Todo 1.6)

**`CORE_ETF`** — current: `QQQ`
- Purpose: the broad passive ETF that idle cash gets swept into after each decision cycle (`Config.core_etf`, `_apply_core_fill` in `orchestrator.py`), so the book is never a structural cash-short against the benchmark. Held passively — excluded from Claude's decision slate entirely (`orchestrator.py` line 800-801: discarded from `base` before the screener/signal pass), never churned, no per-name stop from the normal risk path (it gets its own dedicated GTC stop mechanism, `CORE_STOP_PCT`, configured elsewhere in the file outside this range).
- Impact of setting: enables the whole core-satellite fill feature — buys `CORE_ETF` with any equity cash beyond the target/buffer at the end of every cycle. Impact of blank (`""`): feature fully off — `_apply_core_fill` returns immediately (`if not etf or self.cfg.target_invested_pct <= 0: return`), cash simply sits idle beyond whatever the single-name book deploys.
- Special values: blank string disables it entirely — the documented, supported way to turn this off.
- Interactions: exempt from `MAX_POSITION_PCT`/sector caps (single-name risk limits don't apply to it) but still bounded by `MIN_CASH_BUFFER_PCT` and `MAX_GROSS_EXPOSURE_PCT` (both in `RiskLimits`, outside this range) and by `TARGET_INVESTED_PCT` below. Also interacts with `core_max_pct`/`core_stop_pct` (env vars `CORE_MAX_PCT`/`CORE_STOP_PCT`, not present in this `.env` slice so both run on code defaults: 30% ceiling, 15% stop) which cap how large the core can grow and protect it with a standalone exchange-side GTC stop.
- Verdict: leaving it equal to `BENCHMARK_SYMBOL` (both `QQQ` here) is the intended, coherent setup per the inline comments.

**`TARGET_INVESTED_PCT`** — current: `75.0`
- Purpose: the % of equity the core-satellite sweep tops the book up to using `CORE_ETF`, once the single-name book's own deployment falls short (`Config.target_invested_pct`; `_apply_core_fill` computes `target = min(self.cfg.target_invested_pct, r.max_gross_exposure_pct)`, then buys the gap between current invested % and that target, capped by spendable cash after the `MIN_CASH_BUFFER_PCT` reserve).
- Impact of increasing: less idle cash, more market beta via the core ETF — directly raises structural exposure to `CORE_ETF`'s single-name/single-ETF risk. Impact of decreasing: more dry powder held back, less beta drag from one ETF, but a larger persistent cash/structural-short-vs-benchmark gap.
- Special values: `0` (or below) disables the sweep entirely, same effect as leaving `CORE_ETF` blank (both conditions are checked with an `or` in `_apply_core_fill`).
- Interactions: clamped downward by `RiskLimits.max_gross_exposure_pct` (the no-leverage gross cap) — so raising this above that ceiling has no effect beyond the ceiling itself. Also interacts with `MIN_CASH_BUFFER_PCT` (spendable cash floor) and, once the core position exists, with `CORE_MAX_PCT` (the per-position ceiling that caps further core buys once the position itself gets large, documented elsewhere in the file) — the two together are what stop the core from running away even at a high target %.
- History/verdict per the inline comments: this has been whipsawed by an actual incident — 90% ran through 2026-07-13 (a "Phase-B record value" run), was quietly dropped to a then-unrecorded 60 (which "re-opened the cash-drag hole" after a satellite unwind), restored to 90 on 07-14, then cut to 75 on 07-16 after QQQ alone reached ~46% of the book at 90 — explicitly trading some excess-return upside for less single-ETF concentration. Treat 90 as the aggressive/"was default" end and 60 as the empirically-bad, cash-drag-prone end; 75 is the current deliberate middle.

**Files referenced (this section):** `/Users/spusapati/Personal/Investment_stratergy/.env`, `/Users/spusapati/Personal/Investment_stratergy/investment_strategy/config.py`, `orchestrator.py`, `screener/aggregator.py`, `screener/insider_feed.py`, `screener/options_flow_feed.py`, `benchmark.py`.

---

## 7. Signal-Source API Keys, Read-Only Robinhood MCP Integration

### Signal source API keys (optional; providers degrade gracefully)

**`FMP_API_KEY`** — value: *(blank)*
- Loaded: `Config.fmp_api_key = os.getenv("FMP_API_KEY", "")` (plain passthrough, no helper).
- Purpose: reserved hook for Financial Modeling Prep fundamentals.
- Impact: **dead config.** `signals/fundamentals.py` never reads `cfg.fmp_api_key` — the provider is hardcoded to yfinance. Setting this to anything does nothing until a dev wires it into `fundamentals.py`'s `fetch()`.
- Special values: none — no code path checks it.
- Interactions: mentioned alongside `FINNHUB_API_KEY` in comments/docstrings but functions independently (and currently not-at-all).
- Verdict: leave blank; don't bother getting a key for this one.

**`FINNHUB_API_KEY`** — value: `<redacted>`
- Loaded: `Config.finnhub_api_key = os.getenv("FINNHUB_API_KEY", "")`.
- Purpose: powers `signals/insider.py` (Form-4 insider buy/sell tally) and is the preferred sentiment source in `signals/news.py`.
- Impact: present → insider signal turns on (`InsiderProvider.enabled = bool(key)`) and news uses Finnhub's model-based sentiment. Absent → insider contributes nothing; news auto-falls-back to VADER (if installed) then a keyword scan — no crash, just cruder sentiment.
- Special values: blank = graceful double-degrade (insider off, news uses fallback). News also self-latches on a 403 (`_finnhub_gated`) so a plan-gated endpoint logs once instead of re-probing every symbol every cycle.
- Interactions: independent of Quiver/FRED/Polygon.
- Verdict: per the 2026-07-12 tier audit, free tier already covers insider-transactions + fundamentals `metric`; only `news-sentiment` is 403-gated, and the code's fallback already handles it — no upgrade needed.

**`QUIVER_API_KEY`** — value: `<redacted>`
- Loaded: `Config.quiver_api_key`; fed into one shared `QuiverClient` used by `orchestrator.py`, `signals/congress.py`, `signals/govcontracts.py`, `signals/offexchange.py`, `signals/aggregator.py`, and screener's `congress_feed.py`/`wallstreetbets_feed.py`.
- Purpose: unlocks congressional trading (STOCK Act, ~45-day lag), gov contracts, off-exchange/dark-pool volume, WSB mentions.
- Impact: present → those feeds turn on; `preflight.py`'s `_check_quiver` also live-tests it before market open and **fails loudly** if it returns zero rows (expired key/plan change/outage) instead of silently trading blind. Absent → all Quiver signals contribute nothing, preflight passes trivially.
- Special values: blank = fully off, no preflight gate.
- Interactions: `QuiverClient` caches each dataset's bulk `live/{dataset}` pull once per cycle (`new_cycle()` clears it) and shares it across every consumer specifically to avoid double-pulling congresstrading and blowing the rate limit.
- Verdict: account is Hobbyist tier ($30/mo) — congresstrading/offexchange/govcontractsall/lobbying/flights work; insiders/wallstreetbets/twitter/spacs/13F-changes 403 on this tier. Don't upgrade to Trader ($75/mo) without a measured-edge case (insiders would be redundant with free Finnhub+EDGAR anyway).

**`FRED_API_KEY`** — value: `<redacted>`
- Loaded: `Config.fred_api_key`.
- Purpose: feeds `signals/macro.py` — Fed funds rate, unemployment, 10y-2y spread as market-wide risk backdrop.
- Impact: present → macro signal fires each cycle (`MacroProvider.enabled = bool(key)`); curve inversion scores -0.5 (bearish). Absent → no macro context at all — the decision model reasons blind to rates/recession backdrop (no fallback source exists here).
- Special values: blank = signal fully off, not a partial degrade.
- Interactions: none with other keys; `market_wide=True`, symbol-agnostic unlike the Quiver/Finnhub signals.
- Verdict: FRED has no paid tier — free API, ~120 req/min headroom, nothing to upgrade.

**`POLYGON_API_KEY`** — value: `<redacted>`
- Loaded: `Config.polygon_api_key` — field comment literally reads `# unused: options-flow reads Alpaca now; kept as the paid-upgrade hook`.
- Purpose: historically fed options-flow "unusual activity"; now unused.
- Impact: **no-op.** Nothing reads `cfg.polygon_api_key` outside its declaration. Free-tier Polygon 403s the options-snapshot endpoint it was written for (silently dead from day one per the 2026-07-12 audit); PR #25 rewired `signals/options_flow.py` to Alpaca's `dailyBar` volume instead (source `alpaca-options-flow`).
- Special values: none — set or blank, identical behavior.
- Interactions: none currently; kept only as a hook for a future paid Options Starter tier ($29/mo, OPRA-grade flow).
- Verdict: leave as-is; don't pay for Polygon Options Starter unless the Alpaca-based signal first shows measured edge in the ledger.

**`SEC_USER_AGENT`** — value: `<redacted — name + personal email; not a trading credential, but PII, so withheld here too>`
- Loaded: `Config.sec_user_agent = os.getenv("SEC_USER_AGENT", "investment-strategy-bot contact@example.com")` — one of the few vars with a non-empty library default.
- Purpose: SEC EDGAR requires a descriptive `User-Agent` (name+contact) on every request; used by `signals/insider_edgar.py` and `screener/insider_feed.py`.
- Impact: any non-empty value → both EDGAR readers enabled (`bool(sec_user_agent)`). Blank → both disable. A placeholder/fake value risks EDGAR rate-limiting or blocking the source IP, not just a degraded signal — this is a compliance string, not decoration.
- Special values: blank = off; the library's fallback default doesn't identify a real contact and shouldn't be left in place for real traffic.
- Interactions: no key required (EDGAR is free) — the only var in this group that isn't a credential.
- Verdict: keep it accurate/current; don't run real traffic on the placeholder default.

### Read-only Robinhood via official Agentic Trading MCP (context only)

**`ROBINHOOD_ENABLED`** — value: `on`
- Loaded: `Config.robinhood_enabled = _flag("ROBINHOOD_ENABLED")` (default off; accepts on/true/1/yes).
- Purpose: master switch for RH context-import (holdings-as-context, screener feeds, earnings-calendar reader).
- Impact: on → `RobinhoodReader.enabled` becomes reachable (still gated below); orchestrator pulls external RH holdings each cycle and `preflight._check_robinhood` exercises the token before market open. Off → `holdings()` always `[]`, zero MCP calls, preflight trivially passes.
- Special values: off = fully inert.
- Interactions: needs `ROBINHOOD_MCP_URL` set plus either a completed OAuth handshake (`ROBINHOOD_OAUTH_FILE`) or `ROBINHOOD_MCP_TOKEN`. Orchestrator also warns at startup if this is on **and** `ROBINHOOD_MCP_TOKEN` is non-empty (see below).
- Verdict: safe to leave on — read-only is enforced in code (`_is_read_tool` allowlists only `get_*`/`search`/`run_scan`, blocks everything else pre-dispatch), so the real risk is *which* RH account gets authorized, not this flag.

**`ROBINHOOD_MCP_URL`** — value: `https://agent.robinhood.com/mcp/trading`
- Loaded: default equals current value — effectively unset.
- Purpose: MCP server endpoint for the OAuth handshake and every read call.
- Impact: wrong/blank → `enabled` false (`bool(url)` check), integration goes dark; also used by `robinhood_auth login/status` for discovery.
- Special values: blank disables like `ROBINHOOD_ENABLED=off`.
- Interactions: paired with `ROBINHOOD_SCOPE`/`ROBINHOOD_CLIENT_NAME` at handshake time.
- Verdict: leave at official default.

**`ROBINHOOD_OAUTH_FILE`** — value: `state/robinhood_oauth.json`
- Loaded: `Config.robinhood_oauth_file`.
- Purpose: where OAuth access+refresh tokens persist (gitignored); also the file the self-healing auth-dead latch watches.
- Impact: `robinhood_auth login` writes here; every call reads it via `has_tokens()`/`build_provider()`. `_maybe_recover()` fingerprints (mtime, size) and clears the class-wide "auth dead" latch within ~60s of a relogin rewriting it — no bot restart needed.
- Special values: pointing it outside `state/` still works for auth, but the separate `robinhood_health.json` (control-panel card) is deliberately anchored to the canonical state dir regardless.
- Interactions: if this file has no tokens, `ROBINHOOD_MCP_TOKEN` is used as fallback.
- Verdict: leave default; change only when isolating multiple bot instances' credentials.

**`ROBINHOOD_CALLBACK_PORT`** — value: `8765`
- Loaded: `Config.robinhood_callback_port = _i("ROBINHOOD_CALLBACK_PORT", 8765)`.
- Purpose: localhost port the OAuth PKCE redirect lands on during the one-time `login` handshake.
- Impact: only matters during interactive login; a port collision fails the handshake with a bind error. Zero effect on runtime reads afterward.
- Special values: none; any free local port works.
- Interactions: independent of everything else here.
- Verdict: default fine; change only if 8765 is already taken locally.

**`ROBINHOOD_LOGIN_TIMEOUT_S`** — value: *(unset → 600)*
- Loaded: `robinhood_auth.login_timeout_from_env()` at the CLI boundary (after `load_config()` has loaded `.env`) — deliberately **not** a `Config` field: `login` is a one-shot operator command, never a bot-runtime path. `login --timeout SECONDS` overrides it per run.
- Purpose: wall-clock deadline for the operator to approve Robinhood's consent screen during `robinhood_auth login`. Added after the Sep 10 2026 incident where the redirect wait had only a per-request socket timeout and looped forever — a re-auth sat 2h44m holding port 8765 and wedged the resident session that launched it.
- Impact: on expiry `login` raises `OAuthConsentTimeout`, prints the runbook line (`consent stalled — leaving Robinhood DEGRADED; the bot trades without RH context`), exits **3** (1 = other failure, 2 = not configured) and leaves the token file untouched, so the reader's existing latch/self-heal state is intact. A code arriving before the deadline still completes normally.
- Special values: blank, non-numeric or ≤0 → 600 with a warning (0s would never wait). Incidental hits on the callback port (favicon/prefetch) consume budget rather than restart the clock.
- Interactions: only the interactive login path; `build_provider(interactive=False)` used by the running bot never starts the callback server. Per `ops/away_mode.md`, after two stalls stop and leave RH DEGRADED.
- Verdict: leave default; shorten (e.g. 120) when driving the login from an automated session that must not block.

**`ROBINHOOD_SCOPE`** — value: `internal`
- Loaded: `Config.robinhood_scope = os.getenv("ROBINHOOD_SCOPE", "internal")`.
- Purpose: OAuth scope requested at handshake.
- Impact: mostly cosmetic — RH's server advertises exactly one scope (`internal`); **there is no read-only scope**, so requesting anything else would just fail.
- Special values: blank → `robinhood_auth.py` passes `None` (server default, effectively the same).
- Interactions: this is *why* read-only enforcement lives in code (`_is_read_tool`) rather than the token — the token issued under this scope is trade-capable regardless.
- Verdict: don't change; nothing safer to request.

**`ROBINHOOD_CLIENT_NAME`** — value: `Investment Strategy Bot`
- Loaded: `Config.robinhood_client_name`.
- Purpose: display name on RH's OAuth consent screen.
- Impact: cosmetic only, no functional effect on token capability.
- Special values: none. Interactions: none.
- Verdict: any descriptive string is fine.

**`ROBINHOOD_MCP_TOKEN`** — value: *(blank)*
- Loaded: `Config.robinhood_mcp_token = os.getenv("ROBINHOOD_MCP_TOKEN", "")`.
- Purpose: legacy fallback — paste a pre-obtained Bearer token to skip the OAuth handshake.
- Impact: blank (current) → reader requires completed OAuth (`has_tokens()`), the preferred auto-refreshing path in actual use. If populated, used only when no OAuth tokens exist, as a static header with no refresh/self-heal.
- Special values: blank = OAuth-only (the healthier mode).
- Interactions: `orchestrator._warn_on_weak_safety_config()` warns at startup specifically when `ROBINHOOD_ENABLED=on` **and** this is non-empty — a pasted token is trade-capable (same single-scope gotcha) with none of the OAuth self-heal machinery, so leaving it set alongside OAuth is redundant risk, not redundancy-as-safety.
- Verdict: keep blank — this is the healthy state, not an oversight.

**`ROBINHOOD_POSITIONS_TOOL`** — value: `get_equity_positions`
- Loaded: `Config.robinhood_positions_tool = os.getenv("ROBINHOOD_POSITIONS_TOOL", "")`.
- Purpose: names the MCP tool `holdings()` calls to fetch external positions for context.
- Impact: blank → can't know which tool to call, so `holdings()` calls `__list_tools__`, logs available names once, returns `[]` (no crash, no holdings). Set correctly → positions imported, enriched with live prices via batched `get_equity_quotes`, handed to the decision engine as `ExternalHolding` context.
- Special values: blank = import degrades to a one-time discovery no-op.
- Interactions: requires a resolvable account (`ROBINHOOD_ACCOUNT_NUMBER` or auto-pick) — without one, also returns `[]`.
- Verdict: current value matches RH's real tool name; change only if RH renames it.

**`ROBINHOOD_ACCOUNT_NUMBER`** — value: `<redacted — a real brokerage account number>`
- Loaded: `Config.robinhood_account_number = os.getenv(...).strip()`.
- Purpose: pins which RH account `holdings()` reads, since the OAuth token can see every account under the login.
- Impact: set (current) → skips auto-discovery, reads this account directly. Blank → `_resolve_account_number()` calls `get_accounts` and auto-picks the one flagged `agentic_allowed=true` (the dedicated funded agentic account) — and explicitly **never** falls back to the main/default account; if none found, warns and no-ops.
- Special values: blank = auto-pick (safe by design — refuses to guess your main account). Multiple agentic accounts + blank → picks the first, logs a suggestion to pin one.
- Interactions: only meaningful together with `ROBINHOOD_POSITIONS_TOOL` — both required for import to do anything.
- Verdict: pinning it explicitly (current setup) is more deterministic than relying on upstream `agentic_allowed` flagging, at the cost of a manual update if the dedicated account ever changes.

---

## 8. Loop Cadence, Universe & Runtime Control Files, Live Dashboard

### Loop cadence

#### `DECISION_INTERVAL_SECONDS`
- **Current value:** `3120` (52 min)
- **Purpose:** cadence between full Claude decision cycles (signal gather → LLM call → trade proposals). This is the expensive, LLM-billed loop.
- **Increase:** fewer LLM calls/day, lower API spend, but slower to act on new theses; between cycles only the watchdog's mechanical stops/take-profits protect the book, not fresh judgment.
- **Decrease:** faster reaction to new opportunities, but higher spend — and per the inline comment's own backtest note, going too short risks landing calls outside the ~60‑min prompt-cache TTL, dropping cache-hit rate (~50% at the edge) and losing the `cache_read` discount on the ~5,669‑token stable block.
- **Special values:** none (positive seconds only); coded default 900s (15 min) if unset — this .env overrides that default with a value tuned from `state/api_usage.jsonl`.
- **Interactions:** enforced inside the tick loop at `MONITOR_INTERVAL_SECONDS` granularity, so a decision can fire up to one monitor-interval late. A timed-out call (see `DECISION_TIMEOUT_SECONDS`) never stamps `_last_decision_at`, so the *next* tick retries almost immediately rather than waiting a full 52 min again. Independent of the watchdog loop, which always runs on its own cadence regardless of this value.
- **Verdict:** 3120s is a deliberately tuned value for cache economics, not a round number — treat any change as needing a fresh look at `state/api_usage.jsonl` cache-hit rates, not a guess.

#### `MONITOR_INTERVAL_SECONDS`
- **Current value:** `30`
- **Purpose:** cadence of the independent watchdog safety loop (checks stops/take-profits/naked positions) and the polling granularity the decision thread uses to check whether a decision is due.
- **Increase:** slower response to a failed close/naked position (watchdog literally logs "retrying every ~Ns"), slower to notice a decision is due, slower heartbeat/liveness stamps.
- **Decrease:** tighter safety response and cadence lock, at the cost of more broker-API polling (this loop never calls Claude, so no LLM cost impact).
- **Special values:** none; must be positive, default 30 if unset.
- **Interactions:** feeds several derived thresholds elsewhere — watchdog-blind paging escalates after 5 consecutive skipped ticks (`WATCHDOG_SKIP_ESCALATE`, i.e. ~5× this value of blindness before it pages a human, then again only at the doubling rungs 10, 20, 40, 80 … of the same run — run-7 change after the Sep 7 2026 storm); stale-heartbeat/dark-gap and post-sleep-wake detection both use `monitor_interval_s * 3 + 60`. Raising this value quietly loosens all of those too.
- **Verdict:** 30s is intentionally tight — it's the core protective loop ("how often the watchdog checks positions" per the comment). Don't raise it to save cost; it isn't the cost driver.

#### `DECISION_TIMEOUT_SECONDS`
- **Current value:** `90`
- **Purpose:** hard cap on a single Claude decision API call, passed straight into the `anthropic.Anthropic(timeout=...)` client.
- **Increase:** more headroom for slow/complex completions (large candidate slate, higher `DECISION_EFFORT`) before the call is aborted.
- **Decrease:** calls fail faster on slow responses ("Claude decision call timed out"), causing more skipped/retried cycles — riskier if `DECISION_EFFORT` is high/xhigh/max, which routinely takes longer.
- **Special values:** none named as off; any positive float passes through to the SDK.
- **Interactions:** combined with `max_retries=1` on the same client (one automatic retry inside this budget). A timeout means `_last_decision_at` is never stamped, so the decision retries at the next `MONITOR_INTERVAL_SECONDS` tick instead of waiting for the next full `DECISION_INTERVAL_SECONDS`.
- **Verdict:** 90s (the coded default) is fine for `medium` effort; raise it if `DECISION_EFFORT` is pushed to high/xhigh/max. Should always stay far below `DECISION_INTERVAL_SECONDS`.

### Universe & runtime files

#### `WATCHLIST`
- **Current value:** *(blank)*
- **Purpose:** comma list of symbols to always include in the decision slate, on top of whatever the scanner discovers and whatever is currently held.
- **Increase (add names):** guarantees those tickers get evaluated every cycle regardless of what the screener finds, at the cost of extra signal-fetch time/cost per cycle. **Blank (current):** pure discovery mode — only screener finds + current holdings are evaluated.
- **Special values:** unset (`None` in Python) → orchestrator's own empty default list; blank `""` or literal `NONE` → explicitly forced empty (same effective result, but distinguishes "never configured" from "deliberately emptied"); comma list → uppercased and force-included.
- **Interactions:** unioned with currently-held symbols (`base = watchlist ∪ held`) — held positions are evaluated no matter what this is set to. That combined base is then *excluded* from the screener's own discovery scan, so watchlist names don't get double-counted as "new finds." If this is empty **and** `SCREENER_ENABLED=off`, the bot logs a warning that there is nothing to trade. `CORE_ETF` (if set) is dropped from the slate regardless.
- **Verdict:** blank + `SCREENER_ENABLED=on` (current setup) matches the documented design intent — "discovery-driven by design." Setting an explicit list is a deliberate override for forcing coverage of specific tickers, not the normal mode.

#### `LOG_LEVEL`
- **Current value:** `INFO`
- **Purpose:** root Python logging level (`logging.basicConfig(level=...)`).
- **Increase verbosity (`DEBUG`):** much noisier console + file logs (every signal fetch, cache decision, etc.); useful for a specific incident, but bloats `logs/bot.log` and the per-day rotated files that get committed to the repo.
- **Decrease (`WARNING`/`ERROR`):** quieter, but loses the INFO-level narrative (decision reasoning, per-cycle summaries) that manual review and the nightly post-mortem lean on.
- **Special values:** standard Python levels; comment explicitly calls out "DEBUG for verbose logs."
- **Interactions:** independent of `LOG_DIR` (not set here, defaults to `logs/`), which controls the rotating file sink's existence/location. Two sub-loggers (`mcp.client.streamable_http`, `httpx`) are hard-pinned to WARNING regardless of this setting to suppress known RH-MCP retry noise; `yfinance` gets a truncation filter too — `DEBUG` won't un-suppress either.
- **Verdict:** `INFO` is the right steady-state default given logs are committed for backward analysis; treat `DEBUG` as temporary.

#### `KILL_SWITCH_FILE`
- **Current value:** `state/KILL`
- **Purpose:** sentinel file whose mere existence halts NEW buys without a process restart; checked every tick.
- **Increase/decrease:** N/A — binary. Creating the file sets `risk.kill_switch=True` within one `MONITOR_INTERVAL_SECONDS`; deleting it clears *this* halt source (only).
- **Special values:** presence/absence is what matters, not content — though the bot itself writes a timestamped reason line when it self-triggers a halt (e.g. a reconcile mismatch between ledger and broker).
- **Interactions:** ORs together with two other sources into the effective kill switch: the startup `KILL_SWITCH` env flag (`off` in this .env, restart-only) and an in-memory `_forced_halt` fallback (used if the file write itself fails, e.g. read-only disk). **Distinct from `STATE_FILE`'s halt latch** — reconcile-mismatch halts go through this file; equity-floor/drawdown halts go through `state.halted` in `STATE_FILE` instead. The two "clear a halt" procedures are not interchangeable.
- **Verdict:** default path is fine. The operationally important fact is which halt each file clears — mixing them up leaves a halt you think you cleared still active.

#### `STATE_FILE`
- **Current value:** `state/risk_state.json`
- **Purpose:** JSON-backed persisted risk memory shared by `RiskManager` and `Watchdog` — peak equity (drawdown high-water mark), the equity-floor/drawdown **halt latch**, per-position trailing-stop high-water marks, entry times, churn-guard cooldown clocks, exit prices/convictions, daily deploy accumulators, pending-order state, and the postmortem-done-day marker.
- **Increase/decrease (i.e., pointing elsewhere):** effectively wipes all of that memory — a new/missing file starts from a fresh peak equity with the halt latch cleared and all cooldowns forgotten.
- **Deleting the file (documented operator action):** the explicit way to clear a latched equity-floor/drawdown halt — by design this halt does **not** auto-resume.
- **Special values:** none; must be a writable path. Its directory (`state/`) is gitignored by design — this memory never gets committed.
- **Interactions:** one shared `PortfolioState` instance is constructed from this path and handed to both `RiskManager` and `Watchdog` — they must never be pointed at different files. The single-instance `flock` lock (`bot.lock`) also lives next to this path's parent directory, so it indirectly determines where the double-bot guard lives.
- **Verdict:** keep at default. Changing the path is a rare, deliberate "wipe risk memory" operation — not a tuning knob.

### Live tracking

#### `DASHBOARD_FILE`
- **Current value:** `dashboard.html`
- **Purpose:** if set, regenerates a live HTML account/P&L dashboard after every decision cycle for a human to open in a browser.
- **Increase/decrease:** N/A — presence toggles a best-effort regeneration step after each cycle (plus once on the open→closed transition); failures are caught and logged as warnings, never crash the loop.
- **Special values:** blank = off (per comment). When off, the same data is still available on demand via `python -m investment_strategy.status` (text) or `python -m investment_strategy.dashboard` (one-off HTML).
- **Interactions:** refreshed at the same call site as `track_record_file` (not set in this .env, so that half is currently dormant) — the two are independent toggles sharing one refresh step. Regeneration only happens while the market is open, plus exactly once on the open→closed transition, to avoid repainting byte-identical HTML overnight.
- **Verdict:** on (current) is a sensible default for a solo operator who wants to glance at account state without a terminal — purely read-only reporting, no safety implication.

---

## 9. Watchdog CRITICAL Alerting (Email/Webhook), Dead-Man External Paging

### Alerting on watchdog CRITICAL (page a human)

**`ALERTS_ENABLED`**
- Current value: `on`
- Loaded: `notify.load_alert_config()` → `flag("ALERTS_ENABLED")` (accepts on/true/1/yes) → `AlertConfig.enabled`, consumed by `Alerter.critical()` in `notify.py`.
- Purpose: master switch for out-of-band paging when the watchdog hits a CRITICAL it can't self-heal — a failed close (naked, unmonitored position) or a latched equity-floor halt (`monitor/watchdog.py`, `orchestrator.py`).
- Impact: `on` + a configured sink pages you by email/webhook; `off` (or unconfigured) means the CRITICAL still gets logged (and `preflight.py`/`Alerter.__init__` will warn if `on` but no sink exists) but nobody is paged — you'd only find out by reading logs.
- Special values: only `on/true/1/yes` (case-insensitive) count as enabled; anything else, including blank, is off.
- Interactions: gates every other flag in this block — `ALERT_SMTP_*`, `ALERT_EMAIL_TO`, `ALERT_WEBHOOK_URL`, `ALERT_COOLDOWN_SECONDS` are all inert unless this is `on`. Independent of `HEARTBEAT_URL` below (different failure mode: this is "bot alive but a position/halt needs you"; heartbeat is "bot/laptop might be dead").
- Verdict: inline comment marks this "Highly recommended before LIVE"; leaving it off is a deliberate degrade-to-log-only choice, fine for early paper testing but risky once real capital naked positions are possible.

**`ALERT_SMTP_HOST` / `ALERT_SMTP_PORT` / `ALERT_SMTP_USER` / `ALERT_SMTP_PASSWORD`**
- Current values: `smtp.gmail.com`, `587`, `srikanth09112@gmail.com`, `ALERT_SMTP_PASSWORD=<redacted>` (16-char Gmail app password — never your account login).
- Loaded: `notify.load_alert_config()` → `AlertConfig.smtp_host/smtp_port/smtp_user/smtp_password`, used in `Alerter._send_email()` (stdlib `smtplib`, STARTTLS + login).
- Purpose: one of the two alert delivery sinks — sends the CRITICAL as an email.
- Impact: email sink is only considered "configured" (`Alerter._email_configured`) when host, user, password, AND `ALERT_EMAIL_TO` are ALL non-empty; missing any one silently drops email delivery (falls back to webhook if set, else log-only). Wrong port/host/password → `_send_email` retries once after a 5s sleep, then logs a warning and (if webhook also fails) parks the key behind the `ALERT_RETRY_BASE_S`/`ALERT_RETRY_CAP_S` backoff and spools the page to `ALERT_SPOOL_FILE` (it no longer un-throttles the key for a next-tick retry — that was the Sep 7 2026 storm).
- Special values: blank host/user/password/port disables the email sink specifically (webhook can still work independently).
- Interactions: all four must be set together to count as "configured"; combines with `ALERT_WEBHOOK_URL` (either sink firing counts as "sent") and gated by `ALERTS_ENABLED`.
- Verdict: standard Gmail app-password setup per the inline comment link; nothing unusual to tune here beyond keeping the app password rotated if ever exposed.

**`ALERT_EMAIL_TO`**
- Current value: `srikanthpusapati1@gmail.com` (comma-separated recipients; not a secret but is a personal address — kept as-is here per redaction rule which only covers *secret* values).
- Loaded: `AlertConfig.email_to`, split on commas in `_send_email` to build the `To:` header and recipient list.
- Purpose: who actually gets paged by email.
- Impact: empty disables the email sink (see above); add more comma-separated addresses to fan out paging to multiple people.
- Interactions: required alongside the four SMTP_* vars for email to fire; if both this and `ALERT_WEBHOOK_URL` are empty while `ALERTS_ENABLED=on`, `Alerter.__init__` logs a startup warning that alerts will only be logged.
- Verdict: comment notes this path was "Verified working 2026-07-01" with a real test delivery — treat an unverified change to this address as untrusted until you re-run the preflight test send.

**`ALERT_WEBHOOK_URL`**
- Current value: blank (disabled).
- Loaded: `AlertConfig.webhook_url` (`.strip()`ed), used in `Alerter._send_webhook()` — POSTs `{"text": "*subject*\nbody"}` as JSON to the URL.
- Purpose: second, independent paging sink (Slack/Discord/PagerDuty incoming-webhook style) — doesn't require Gmail at all.
- Impact: blank = webhook sink inert (email-only, if configured). Setting it adds a second delivery channel; both sinks fire independently and either succeeding counts as delivered.
- Special values: blank = off.
- Interactions: fully independent of the SMTP_*/EMAIL_TO vars — you can run webhook-only, email-only, both, or (if `ALERTS_ENABLED=on` with neither) log-only with a startup warning.
- Verdict: not configured in this deployment; email is the sole live channel, so a Gmail outage or app-password expiry would silently degrade paging to log-only — worth adding a webhook as a redundant sink given "Highly recommended before LIVE."

**`ALERT_COOLDOWN_SECONDS`**
- Current value: `900` (15 minutes; matches `notify.DEFAULT_COOLDOWN_S`).
- Loaded: `float(getenv("ALERT_COOLDOWN_SECONDS", str(DEFAULT_COOLDOWN_S)))` → `AlertConfig.cooldown_s`, enforced in `Alerter._should_send()`.
- Purpose: minimum time between re-sent pages for the *same* alert key (e.g. the same symbol failing to close every ~30s watchdog tick), so a stuck position pages once rather than spamming.
- Impact: lower = more frequent re-pages for a persisting problem (more noise, faster re-notification if you missed the first page); higher = fewer duplicate pages but longer gaps of silence on an unresolved CRITICAL. `0` would effectively page on every tick (no throttling) since any elapsed time exceeds a 0s window.
- Interactions: bypassed by the hardcoded `SEVERITY_ESCALATION = 2.0` logic — a same-key alert whose severity is ≥2x the last sent severity fires immediately regardless of cooldown (added after a Jul 17 incident where a small dark-gap alert's cooldown window suppressed a much larger gap that followed). Also: the throttle uses wall-clock time (`time.time()`), specifically because monotonic time froze across laptop sleep on Jul 17 and stretched a 15-min window into ~6.2 real hours, swallowing pages — this is a fixed behavior, not a tunable.
- Verdict: 15 min (the coded default) is the calibrated value post-incident; shortening it much below that risks true alert-spam given the watchdog's 30s tick retrying the same failure every cycle.

**`ALERT_RETRY_BASE_S` / `ALERT_RETRY_CAP_S`**
- Current value: unset → defaults `60` / `900` (`notify.DEFAULT_RETRY_BASE_S` / `DEFAULT_RETRY_CAP_S`).
- Loaded: `float(getenv("ALERT_RETRY_BASE_S", "60"))` / `float(getenv("ALERT_RETRY_CAP_S", "900"))` → `AlertConfig.retry_base_s` / `retry_cap_s`, applied in `Alerter._dispatch()` and gated in `Alerter._should_send()`.
- Purpose: retry schedule for an alert key whose delivery FAILED on every sink. The n-th consecutive failure parks the key for `min(cap, base * 2**(n-1))` seconds (60, 120, 240, 480, 900, 900, …) before another attempt. Added after the Sep 7 2026 storm: DNS died on the host, the alerter un-stamped its throttle on every failed send, and the watchdog-blind key made 75 attempts (150 SMTP tries) in 56 minutes with nothing delivered.
- Impact: lower base = a dead sink is re-probed sooner (each probe costs the worker ~5–35s of SMTP timeouts); higher cap = a long outage probes less often. `0` for either disables the wait (retry at caller cadence — the pre-fix behaviour, not recommended).
- Special values: the cap defaults to the cooldown so a dead sink never retries less often than a live one re-pages. `SEVERITY_ESCALATION` (≥2x worse same-key event) still bypasses the backoff, so doubling severities earn at most a handful of extra probes.
- Interactions: the backoff replaces the cooldown as the gate only while the key's LAST attempt failed everywhere; a delivery clears it and the key is back on `ALERT_COOLDOWN_SECONDS`. Pages held by the backoff (no attempt) are spooled to `ALERT_SPOOL_FILE`.
- Verdict: leave at defaults; the watchdog-blind caller now pages only at doubling skip counts anyway, so a 1-hour outage costs ≤ 8 attempts instead of ~75.

**`ALERT_SPOOL_FILE`**
- Current value: unset → default `<dir of STATE_FILE>/alerts_spool.jsonl` (i.e. `state/alerts_spool.jsonl`; `state/` is git-ignored). A relative path — the default included — is anchored at the REPO ROOT, not the caller's cwd, so `ops/deadman.py` and `preflight` run by hand from any directory append to and flush the same file the bot uses.
- Loaded: `getenv("ALERT_SPOOL_FILE", str(Path(STATE_FILE).parent / "alerts_spool.jsonl")).strip()`, relative → `<repo root>/<path>` → `AlertConfig.spool_path`, used by `Alerter._spool_append()` / `_flush_spool()` (truncation is tmp + `os.replace`, never a torn file).
- Purpose: durable record of every CRITICAL page that could NOT be delivered (a failed attempt or a page held by the retry backoff): one JSON line per page with `ts, key, subject, body[:400], attempts, reason`. On the next SUCCESSFUL delivery of any alert — from any process sharing the path (orchestrator, `ops/deadman.py`, `preflight`) — the alerter sends ONE summary page ("N alerts were undeliverable between T1 and T2" + distinct subjects with counts + the latest body) and truncates the file, so a sink outage ends with a catch-up instead of silence.
- Impact: blank disables spooling (undelivered pages are only logged). Appends stop past 5 MB until a flush. Reads/writes are best-effort and never raise into the watchdog.
- Special values: a restart still flushes — `Alerter.__init__` logs how many records are pending and the first delivery sends the summary.
- Interactions: the summary bypasses the cooldown (it is not keyed) but is only sent right after a real delivery succeeded; if the summary itself fails the spool is kept for the next success. Two processes appending and flushing at the same instant can drop a record — acceptable for an ops catch-up page.
- Verdict: leave at default; `python -m investment_strategy.preflight` (test send) or the next real page flushes it.

**`SESSION_CALENDAR_FILE`** (run-7 A2)
- Current value: unset → default `<dir of STATE_FILE>/session_calendar.json` (i.e. `state/session_calendar.json`; `state/` is git-ignored). Relative to the bot's cwd (the repo root), like `STATE_FILE`; `ops/deadman.py` reads the fixed `<repo root>/state/session_calendar.json`, so a non-default value must keep that name/location for the dead-man to see it.
- Loaded: `getenv("SESSION_CALENDAR_FILE", "").strip() or <dir of STATE_FILE>/session_calendar.json` → `Config.session_calendar_file` → `investment_strategy.session_calendar.SessionCalendar` (loaded in `Orchestrator.__init__`, so the cache survives a restart).
- Purpose: the exchange calendar (session open/close per date, holidays absent) that the paging window (`Orchestrator._overlaps_paging_hours` — watchdog-blind, dark-gap and on-battery pages) and `ops/deadman.py market_hours()` consult INSTEAD of weekday 09:25-16:05 ET clock math. The DECISION loop refreshes it at most once per ET date (`_refresh_session_calendar`, today ±10 days via Alpaca `get_calendar`; a failed fetch keeps the old cache and retries next cycle); the watchdog thread only ever reads it (no network there). Added after Labor Day 2026-09-07: the bot logged "Market closed" all day yet paged 75 CRITICAL "positions unwatched during market hours" during a DNS outage, and the dead-man ran 80 in-hours checks.
- Impact: the paging window becomes (open−5min, close+5min) per session, so holidays are silent and early closes (13:00) end the window at 13:05. A date the cache does not cover (missing file, first run, bot offline >10 days) falls back to the old weekday math with ONE `Session calendar fallback` WARNING per ET date.
- Special values: blank is impossible via `.env` (the default fills in); a corrupt file is ignored (fallback) until the next successful refresh. Responses with fewer than 5 sessions in the 21-day range are rejected, never adopted.
- Interactions: no trading-path effect — `is_market_open` / `is_trading_day` still come from the broker clock. Gates only whether `watchdog_blind` / `dark_gap` / `on_battery` pages and the dead-man's restart+page fire.
- Verdict: leave at default.

### Dead-man external paging (GA-2.2)

**`HEARTBEAT_URL`**
- Current value: `https://hc-ping.com/<redacted>` (healthchecks.io ping URL — treat as a low-sensitivity secret; redacted here anyway since it's a bearer-style unique URL).
- Loaded: `config.py` line 470: `os.getenv("HEARTBEAT_URL", "").strip()` → `Config.heartbeat_url`. Read in `orchestrator.py`'s `_maybe_heartbeat()` (calls `notify.ping_heartbeat(url)`, a plain GET) and in a startup sanity check (~line 620-640) that warns if it's empty or if it points back at the same machine.
- Purpose: "dead-man's switch" — the bot GETs this external monitor URL on every healthy tick; the *external* monitor (healthchecks.io) pages you when the pings **stop**, which is the only mechanism that can alert you if the laptop itself dies (power loss, crash, network drop) — `ALERTS_ENABLED`'s Alerter can't do this because it depends on the same dying process.
- Impact: blank = external dead-man paging disabled — only the local `ops/deadman.py` watchdog still covers "bot process died but laptop's alive" (per inline comment); the "laptop died entirely" failure mode goes fully unmonitored. Set = pings fire each tick and a monitor gap (missed period+grace) pages you externally.
- Special values: empty string = disabled. Must NOT be a localhost/dashboard/control-panel URL — the inline comment and the orchestrator startup check both explicitly warn that pinging something on the same laptop can never detect the laptop dying.
- Interactions: gated by `_maybe_heartbeat()`'s own freshness check — the ping is *withheld* (not sent) if the main decision loop hasn't stamped liveness within `monitor_interval_s * 3 + 60` seconds, so a hung-but-alive process still lets the external monitor page even though the process never technically died. Distinct from and complementary to `ALERTS_ENABLED`: that's for "watchdog caught a problem," this is for "nothing is calling in at all."
- Verdict: set up per the healthchecks.io free tier with period 10 min / grace 10 min, as the comment instructs; this is the correct, cheap way to cover the single biggest blind spot (dead laptop) that no in-process alerting can ever cover — leaving it blank on a live account is the "unusually risky" configuration the comment is guarding against.

**Files consulted:** `/Users/spusapati/Personal/Investment_stratergy/.env` (lines 261-289), `/Users/spusapati/Personal/Investment_stratergy/investment_strategy/notify.py`, `/Users/spusapati/Personal/Investment_stratergy/investment_strategy/config.py` (lines 355-362, 466-470, 640), `/Users/spusapati/Personal/Investment_stratergy/investment_strategy/orchestrator.py` (lines 100-139, 281-410, 450-475, 620-640, 990-1260), `/Users/spusapati/Personal/Investment_stratergy/investment_strategy/monitor/watchdog.py` (lines 55-145, 375-404), `/Users/spusapati/Personal/Investment_stratergy/investment_strategy/preflight.py` (lines 155-190).

---

## 10. Anti-Chasing Overextension Gate, Composite Signal Index, Rotation Loss Guard, Entry-Quality Gates, Weekly Auto-Tune Report

### Anti-chasing overextension gate (risk.py, RiskManager.evaluate)
Fires at buy-time on two independent legs: (a) RSI-hot **and** extended past the 20d SMA, (b) extension alone past an extreme threshold (added because the actual Jul-13 losers were RSI 61-64 — under a naive RSI floor — but 3.4-4.0 ATRs extended). Fails open if technicals are missing.

- **`OVEREXTENSION_GATE_ENABLED=on`** — Master switch for *both* legs (the extreme-leg check lives inside the same `if enabled and tech:` block, so it isn't independently gateable from here). Purpose: block/haircut momentum buys chasing a local top. Off → the CDW/SOFI/PATH pattern (68% of a week's realized losses) returns unblocked. Interacts with: every `OVEREXT_*` var below is inert while this is off. Verdict: keep on — this is the primary fix, not a nice-to-have.
- **`OVEREXTENSION_MODE=haircut`** — Action for the non-extreme ("hot and extended") trigger only: `haircut` halves size, `block` rejects outright. Any string other than the literal `"block"` behaves as haircut. Increasing safety = switch to `block` (fewer chasing entries at all, more missed continuations); current `haircut` still takes half size. Interacts with: only the mild leg — the extreme leg has its own independent `OVEREXT_EXTREME_MODE` (not present in this .env, code default `"block"`). Verdict: haircut is a reasonable middle ground for the mild leg now that the extreme leg hard-blocks separately.
- **`OVEREXT_RSI=65`** — RSI14 threshold, one half of the "hot and extended" AND condition. Raise → fewer names read as "hot," more chasing entries slip through unhaircut; lower → catches more moderate momentum too (more false-positive haircuts on legitimate strength). Interacts with: must co-fire with the ATR/pct extension leg (`OVEREXT_ATR_MULT`/`OVEREXT_PCT`) to trigger; independent of the extreme leg. Verdict: 65 is classic-overbought-adjacent, but note the actual Jul-13 losers entered at RSI 61-64 — this leg alone would **not** have caught them; the extreme ATR leg exists precisely to close that gap.
- **`OVEREXT_ATR_MULT=2.0`** — Extension leg (ATRs above 20d SMA) for the combined trigger. Higher → only very stretched names trigger; lower → catches milder extensions too. Interacts with: `OVEREXT_PCT` is a *fallback only*, used solely when ATR data (`ext_atr`) is unavailable. Distinct from, and looser than, `OVEREXT_EXTREME_ATR_MULT` (3.0). Verdict: 2.0x is a moderate line, not aggressive.
- **`OVEREXT_PCT=8.0`** — % above 20d SMA fallback, used only when ATR is missing. Higher/lower shifts how often the fallback fires during an ATR data outage; otherwise dormant. No effect on the extreme leg (which requires ATR data and has no % fallback). Verdict: ~8% roughly tracks 2x ATR for a typical name — sane fallback, rarely exercised.
- **`OVEREXT_HAIRCUT=0.5`** — Size multiplier applied in haircut mode. Clamped to [0,1] in code regardless of what's set. Lower (e.g. 0.25) = more conservative residual size; 1.0 = haircut mode does nothing; 0.0 = haircut mode ≈ block. Interacts with: only applies when the resolved mode (`OVEREXTENSION_MODE` or `OVEREXT_EXTREME_MODE`) isn't `"block"`. Verdict: 0.5 ("wrong top costs half") matches the documented design intent.
- **`OVEREXT_EXTREME_ATR_MULT=3.0`** — RSI-independent trigger: extension alone at/above this many ATRs fires regardless of RSI. 0 = leg off. Higher → only blow-off-top moves flagged; lower → converges toward duplicating `OVEREXT_ATR_MULT`'s job. Interacts with: has its own mode knob `OVEREXT_EXTREME_MODE` (not set in this .env — code defaults it to `"block"`), independent of `OVEREXTENSION_MODE`. Verdict: this is the direct fix for the CVX Jul-17 incident ($2,799 bought at 3.2x ATR under the old shared haircut mode) — don't soften below ~3.0x or default its mode back to haircut without revisiting that incident.

### Deterministic weighted composite signal index (signals/composite.py)
`composite = Σ over signal kinds: mean(kind scores) × freshness-lag weight × realized-perf weight`. Rendered per candidate in the prompt, optionally blended into cycle cash allocation, optionally a hard buy floor.

- **`COMPOSITE_ENABLED=on`** — Master switch for the whole pipeline. Off → no composite computed at all, which **cascades**: `COMPOSITE_BUDGET_BLEND` becomes a no-op (empty scores dict), `COMPOSITE_GATE_ENABLED` can never fire (gate check requires `composite_score is not None`), `ROTATION_REQUIRE_COMPOSITE_EDGE` has nothing to compare, and `REENTRY_PRICE_OVERRIDE_COMPOSITE`'s override can never be satisfied — meaning the (separate) re-entry price guard becomes an **unconditional** block on above-exit-price re-buys. Verdict: keep on; it's read-only context until `COMPOSITE_GATE_ENABLED` is also flipped.
- **`COMPOSITE_BUDGET_BLEND=on`** — Tilts each cycle's cash split toward composite-corroborated names: weight = conviction × max(composite, 0.1) (floored so a weak composite shrinks, never zeroes, a share). Off → allocation is conviction-only. No-ops if `COMPOSITE_ENABLED=off` or only one buy candidate that cycle. Verdict: low-risk to leave on — never rejects a trade, only resizes.
- **`COMPOSITE_GATE_ENABLED=off`** — Opt-in **hard** reject: composite below `MIN_COMPOSITE_SCORE` blocks the buy outright even at high LLM conviction. Off (current) = composite is advisory only. Interacts with: needs `COMPOSITE_ENABLED=on` to ever have a non-None score to check. Verdict: correctly off per the file's own comment — "the hard buy floor stays OFF until backtested"; don't flip without a backtest run first.
- **`MIN_COMPOSITE_SCORE=0.0`** — Floor value used only when `COMPOSITE_GATE_ENABLED=on`. Higher = stricter corroboration required; irrelevant while the gate is off. Verdict: 0.0 is an untuned placeholder, not a considered threshold.
- **`COMPOSITE_PERF_MIN_TRIPS=3`** — Closed round-trips a signal source (congress/insider/options_flow/etc.) needs before its realized-P&L performance weight (0.5x–1.5x) engages; below this it stays neutral (1.0x). Higher = slower, more sample-safe weighting; lower = faster but noisier ("two lucky trades must not double a source's say" — code comment). Verdict: 3 is a sane minimum; don't go below 2.

### Rotation loss guard (orchestrator.py, `_apply_rotation_guard`)
Vetoes a loss-locking SELL that only exists to fund a paired not-held BUY in the same decision, unless the incoming idea clearly beats the incumbent. Never touches watchdog stops/trails/flattens or standalone risk-off sells (no paired buy = nothing to veto).

- **`ROTATION_LOSS_GUARD_ENABLED=on`** — Master switch. Off → the UNH -$204 / HUBB -$158 pattern (selling a loser purely to free a slot for a no-better idea) returns unchecked. Also inert whenever the kill switch is on (buys halted → nothing to fund). Interacts with: gates every other `ROTATION_*` var below. Alternative lever per the file comment: raising `MAX_OPEN_POSITIONS` 15→20 makes the slot cap bind less (fewer forced rotations to begin with) at the cost of thinner per-name weight.
- **`ROTATION_GUARD_MIN_LOSS_PCT=4.0`** — Bottom of the guarded band: only a sell losing worse than this % is even scrutinized. Lower = guard reviews shallower losses too; higher = only deep losses guarded. Interacts with: must stay below `ROTATION_GUARD_MAX_LOSS_PCT` for the depth-escape to have any band to operate in (code requires `max_loss > min_loss_pct`).
- **`ROTATION_MIN_CONVICTION_EDGE=0.10`** — Incoming buy's best conviction must beat the incumbent's *entry-time* conviction by this much for the rotation to pass. Higher = harder to approve rotations (more held-through-losses); near 0 = almost no guard. Interacts with: combined with `ROTATION_REQUIRE_COMPOSITE_EDGE` (adds a second, composite-based condition) and bypassed entirely by `ROTATION_GUARD_EXEMPT_SELL_CONVICTION`. Verdict: 0.10 is literally "enforce what the prompt only asked nicely for" — not a large edge to demand.
- **`ROTATION_REQUIRE_COMPOSITE_EDGE=off`** — Opt-in second condition: incoming composite must *also* beat the incumbent's composite score. On = stricter (both LLM and deterministic signal must agree); needs `COMPOSITE_ENABLED=on` and both scores present, else fails open. Verdict: off is the current conservative default; consider on once composite scores are trusted.
- **`ROTATION_GUARD_EXEMPT_SELL_CONVICTION=0.65`** — A sell with its OWN conviction at/above this is a risk-off exit and is never vetoed, checked before the edge comparison. Lower = more sells auto-exempt (fewer vetoes, but an easier loophole for a rotation dressed as high conviction); higher = fewer exemptions. 0 = exemption off. Verdict: 0.65 is comfortably above coin-flip — a defensible "the model really means this" line.
- **`ROTATION_GUARD_MAX_LOSS_PCT=8.0`** — Depth escape: a loss-locking sell losing worse than this % is never vetoed — the guard concedes only the bracket stop should be relied on past this point. 0 = off. Direct fix for the SPCX Jul-22 incident (guard vetoed the exit at -5.4%, position rode into a -9.8% stop). Must stay clearly above `ROTATION_GUARD_MIN_LOSS_PCT` (4.0) or the escape never fires.
- **`ROTATION_GUARD_REPEAT_RELEASE_PCT=0.75`** — Persistence escape: a sell already vetoed once today is released once the loss has deteriorated by this many additional percentage points (treated as a thesis-break, not churn). 0 = off. Tracked in-memory per symbol/day — a bot restart clears it (one more veto before release). Verdict: 0.75pp is a tight, responsive threshold, appropriate given the SPCX ride it was built to stop.

### Entry-quality gates (risk.py, RiskManager.evaluate)
- **`MIN_NEW_NAME_CONVICTION=0.6`** — Fresh (not-currently-held) buys must clear this; top-ups on an existing position only need the lower general `MIN_CONVICTION` (0.2 elsewhere in this .env) since the position already earned its slot at entry. 0 = off (new/top-up distinction disappears). Note: the code's own dataclass default is **0.5** — this .env explicitly overrides to 0.6, a deliberate tightening. Higher = fewer marginal starter positions (the realized losses this targets — MU -$441, SPCX -$83/-$228 — were all fresh entries at 0.45–0.50); lower = more coin-flip-conviction starters (the exact pattern being fixed). Interacts with: swept weekly by `AUTOTUNE` (`sweep_new_name_floor`) against the realized ledger. Verdict: per the autotune module's own docstring, 0.5 would *also* have blocked small NU/SOFI winners at 0.46 — this knob is empirically ledger-tuned, not vibes-tuned; don't raise further without checking the weekly report first.
- **`REENTRY_PRICE_OVERRIDE_COMPOSITE=1.25`** — When the (separate, not-in-this-range) `REENTRY_PRICE_GUARD_ENABLED` blocks re-buying above a recent exit price, a composite score at/above this overrides the block. Higher = harder to override (fewer re-buys above the exit price get through); lower = easier — the incident this fixes (SPCX re-entered Jul 21 at composite +0.66, $3.58 above its Jul 17 exit, straight to a -9.8% stop) happened at the *old* value of 0.5. Interacts with: needs `COMPOSITE_ENABLED=on` — with composites off, `composite_score` is always `None` so the override can never clear, making the price guard unconditional. Also swept weekly by autotune (`sweep_reentry_override`). Verdict: 1.25 is meant as "top-decile new edge," per the code comment, not "mildly positive" — treat as closer to a floor than a target.

### Weekly ledger-driven auto-tune report (autotune.py)
Deterministic, no-LLM replay of the ledger + decision journal against the five knobs above; writes `state/autotune/{iso-week}.md`. Report-only — never writes a knob back; a human edits `.env` by hand.

- **`AUTOTUNE_ENABLED=on`** — Master switch. Fires once per ET calendar week on the first market-closed weekend tick (latched in state so restarts don't re-run it). Off = no report at all; someone must manually eyeball the ledger to judge whether `MIN_NEW_NAME_CONVICTION`, `MIN_CONVICTION`, `REENTRY_PRICE_OVERRIDE_COMPOSITE`, `ROTATION_GUARD_MAX_LOSS_PCT`, `ROTATION_GUARD_REPEAT_RELEASE_PCT` are still well-tuned. Verdict: safe to always leave on — deterministic, no LLM cost, zero trading-path impact.
- **`AUTOTUNE_DAYS=14`** — Trailing lookback window swept. Shorter = fresher but noisier/more overfit-prone (the module's own docstring warns 2-4 week windows overfit easily); longer = steadier stats but slower to reflect a recent knob change or regime shift. Interacts with: a shorter window makes `AUTOTUNE_MIN_SAMPLE` harder to clear, suppressing more recommendations. Verdict: 14 days sits right at the module's own stated overfitting caution line — don't shorten without also lowering the min-sample.
- **`AUTOTUNE_MIN_SAMPLE=5`** — Minimum affected trades required before any sweep candidate is reported, regardless of how favorable it looks; applies uniformly across all 5 sweeps plus an independent one-sidedness check (a win must not be mostly offset by its cost). Higher = fewer but safer recommendations (more "no rec" weeks); lower = more suggestions on thinner samples. Verdict: 5 is conservative for a low-trade-count solo/small-float book — appropriate given how easily small windows here overfit.

---

## Completeness check

The 117 variable names listed in the task brief (extracted from `.env` in file order) were cross-checked against the assembled document above. **All 117 are present** — every single one appears at least once, documented with purpose/impact/interactions/verdict in its respective section. No backfilling from the codebase was required.

For traceability, here is the section each variable landed in:

- **Section 1:** `TRADING_MODE`, `KILL_SWITCH`, `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ALPACA_BASE_URL`, `ANTHROPIC_API_KEY`, `DECISION_MODEL`, `DECISION_EFFORT`
- **Section 2:** `MAX_POSITION_PCT`, `MAX_SYMBOL_EXPOSURE_PCT`, `MAX_DAILY_LOSS_PCT`, `MAX_DRAWDOWN_PCT`, `MAX_OPEN_POSITIONS`, `MIN_CASH_BUFFER_PCT`, `MIN_TRADE_PRICE_USD`, `DEFAULT_STOP_LOSS_PCT`, `DEFAULT_TAKE_PROFIT_PCT`, `VOL_STOPS_ENABLED`, `VOL_STOP_MULT`, `VOL_STOP_TAKE_RATIO`, `MAX_PAIRWISE_CORR`, `MAX_TRADE_RISK_PCT`, `MIN_CONVICTION`, `EST_SLIPPAGE_PCT`, `MIN_EDGE_RATIO`
- **Section 3:** `MAX_HOLD_DAYS`, `TIME_STOP_MIN_GAIN_PCT`, `SCALE_OUT_ENABLED`, `SCALE_OUT_PCT`, `REGIME_FILTER_ENABLED`, `REGIME_DEGRADED_MULT`, `MISSING_DATA_MULT`, `REGIME_TRIM_ENABLED`, `REGIME_TRIM_PCT`, `REGIME_LOOSEN_MIN_CYCLES`, `THESIS_DECAY_ENABLED`, `THESIS_DECAY_MIN_AGE_DAYS`, `THESIS_MIN_SCORE`
- **Section 4:** `EQUITY_FLOOR_PCT`, `MAX_GROSS_EXPOSURE_PCT`, `MAX_SECTOR_EXPOSURE_PCT`, `EARNINGS_BLACKOUT_DAYS`, `PDT_GUARD_ENABLED`, `FRACTIONAL_ENABLED`, `MIN_ORDER_USD`, `MIN_ORDER_PCT`, `MIN_ADD_INTERVAL_HOURS`, `REENTRY_COOLDOWN_HOURS`
- **Section 5:** `KELLY_FRACTION`, `TARGET_ANNUAL_VOL_PCT`, `OPTIONS_ENABLED`, `MAX_OPTION_PREMIUM_PCT`, `OPTIONS_CHAIN_SIGNAL`
- **Section 6:** `SCREENER_ENABLED`, `SCREENER_SOURCES`, `MAX_DISCOVERED_CANDIDATES`, `SCREENER_MIN_SCORE`, `OPTIONS_FLOW_SCAN_LIMIT`, `INSIDER_SCAN_LIMIT`, `BENCHMARK_SYMBOL`, `CORE_ETF`, `TARGET_INVESTED_PCT`
- **Section 7:** `FMP_API_KEY`, `FINNHUB_API_KEY`, `QUIVER_API_KEY`, `FRED_API_KEY`, `POLYGON_API_KEY`, `SEC_USER_AGENT`, `ROBINHOOD_ENABLED`, `ROBINHOOD_MCP_URL`, `ROBINHOOD_OAUTH_FILE`, `ROBINHOOD_CALLBACK_PORT`, `ROBINHOOD_LOGIN_TIMEOUT_S`, `ROBINHOOD_SCOPE`, `ROBINHOOD_CLIENT_NAME`, `ROBINHOOD_MCP_TOKEN`, `ROBINHOOD_POSITIONS_TOOL`, `ROBINHOOD_ACCOUNT_NUMBER`
- **Section 8:** `DECISION_INTERVAL_SECONDS`, `MONITOR_INTERVAL_SECONDS`, `DECISION_TIMEOUT_SECONDS`, `WATCHLIST`, `LOG_LEVEL`, `KILL_SWITCH_FILE`, `STATE_FILE`, `DASHBOARD_FILE`, `SESSION_CALENDAR_FILE` (run-7 addition, unset in `.env`)
- **Section 9:** `ALERTS_ENABLED`, `ALERT_SMTP_HOST`, `ALERT_SMTP_PORT`, `ALERT_SMTP_USER`, `ALERT_SMTP_PASSWORD`, `ALERT_EMAIL_TO`, `ALERT_WEBHOOK_URL`, `ALERT_COOLDOWN_SECONDS`, `ALERT_RETRY_BASE_S`, `ALERT_RETRY_CAP_S`, `ALERT_SPOOL_FILE` (run-7 additions, unset in `.env`), `HEARTBEAT_URL`
- **Run-7 addenda (2026-09-12, strategy keys):** `HEDGE_BETA_ASSUMED` (S-2 addendum), `PROXY_PUT_PREFER_MONTHLY` / `OPTION_STRIKE_SNAP` / `OPTION_STRIKE_MAX_MONEYNESS_PCT` (Section 5 options table; S-3 / S-4), `TOPUP_MIN_CONVICTION_DELTA` (S-6 addendum), `HEDGE_UNWIND_MIN_CYCLES` (S-8 addendum); `REGIME_LOOSEN_MIN_CYCLES` (S-5) sits in Section 3 above
- **Section 10:** `OVEREXTENSION_GATE_ENABLED`, `OVEREXTENSION_MODE`, `OVEREXT_RSI`, `OVEREXT_ATR_MULT`, `OVEREXT_PCT`, `OVEREXT_HAIRCUT`, `OVEREXT_EXTREME_ATR_MULT`, `COMPOSITE_ENABLED`, `COMPOSITE_BUDGET_BLEND`, `COMPOSITE_GATE_ENABLED`, `MIN_COMPOSITE_SCORE`, `COMPOSITE_PERF_MIN_TRIPS`, `ROTATION_LOSS_GUARD_ENABLED`, `ROTATION_GUARD_MIN_LOSS_PCT`, `ROTATION_MIN_CONVICTION_EDGE`, `ROTATION_REQUIRE_COMPOSITE_EDGE`, `ROTATION_GUARD_EXEMPT_SELL_CONVICTION`, `ROTATION_GUARD_MAX_LOSS_PCT`, `ROTATION_GUARD_REPEAT_RELEASE_PCT`, `MIN_NEW_NAME_CONVICTION`, `REENTRY_PRICE_OVERRIDE_COMPOSITE`, `AUTOTUNE_ENABLED`, `AUTOTUNE_DAYS`, `AUTOTUNE_MIN_SAMPLE`

**Note on one extra variable:** `MAX_DAY_TRADES_UNDER_25K` appears in Section 4 (as `PDT_GUARD_ENABLED`'s paired threshold knob) but was **not** part of the required 117-variable list in the task brief. It is documented anyway since it lives in the same `.env` block and dropping it would leave `PDT_GUARD_ENABLED`'s cross-reference dangling — flagged here so its presence is visible rather than silently smuggled in as if it had been on the original list.

**Secret-scan confirmation:** all API keys (`ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ANTHROPIC_API_KEY`, `FINNHUB_API_KEY`, `QUIVER_API_KEY`, `FRED_API_KEY`, `POLYGON_API_KEY`), the SMTP app password (`ALERT_SMTP_PASSWORD`), the Robinhood OAuth-adjacent secret (`ROBINHOOD_MCP_TOKEN`, blank in this deployment), the brokerage account number (`ROBINHOOD_ACCOUNT_NUMBER`), the SEC contact string (`SEC_USER_AGENT`), and the healthchecks.io ping URL (`HEARTBEAT_URL`) are all rendered as `<redacted>` (or partially redacted for `HEARTBEAT_URL`'s unique path). Non-secret personal contact info (`ALERT_SMTP_USER`, `ALERT_EMAIL_TO` — both Gmail addresses) was left in cleartext per the task's own scope, which restricts redaction to secret *values* (API keys, passwords, tokens), not incidental PII already present in the source drafts.
---

## Addendum 2026-07-30 — all-weather knobs (PR: complete-review implementation)

New variables shipped with the Jul-30 all-weather upgrade. Code defaults in parentheses; `.env` activates the two sleeves.

- **`HEDGE_ETF=PSQ`** (code default "" = off) — 1x inverse ETF the orchestrator buys DETERMINISTICALLY when the falling read holds `AUTO_HEDGE_MIN_CYCLES` consecutive cycles; unwound after the same number of clear cycles. Plain-equity path: works with `OPTIONS_ENABLED=off`, no junk-quote risk. Use 1x (PSQ/SH) only — 3x products decay. System-managed: slate-excluded, trail/time-stop exempt, model proposals ignored.
- **`AUTO_HEDGE_RATIO=0.30`** (0.30) — hedge notional as a fraction of net-long exposure. Higher = flatter book in declines but more drag on bounces.
- **`AUTO_HEDGE_MIN_CYCLES=2`** (2) — persistence filter in decision cycles, both for arming and unwinding. 1 = react to a single red tick (noisy); 3+ = slower, misses fast declines at a 60-min cadence.
- **`AUTO_HEDGE_MAX_PCT=15`** (15) — hedge ceiling as % of equity, independent of the ratio.
- **`DEFENSIVE_CORE_ETF=SGOV`** (code default "" = off) — while the falling read holds, the core DCA redirects into this T-bill ETF instead of pausing; auto-rotated back to cash when the read clears. Exempt from the exposure ladder (cash proxy) and from trail/time-stop.
- **`EXPOSURE_LADDER=on`** (on) with **`EXPOSURE_NEUTRAL_PCT=60`** / **`EXPOSURE_RISK_OFF_PCT=30`** — regime-label caps on gross RISK exposure (defensive sleeve excluded). Applies to satellite buys AND the core fill. Existing positions are never force-sold by the ladder; the regime trim / core defense do that.
- **`EXPECTANCY_GATE=on`** (on) with **`EXPECTANCY_GATE_MIN_TRIPS=8`** / **`EXPECTANCY_GATE_WINDOW_DAYS=14`** — fresh entries whose CITED signal families are ALL negative-expectancy over the trailing window are rejected. Top-ups exempt; uncited proposals fail open; families under the trip floor are never judged.
- **`ROTATION_GUARD_RED_DAY_RELEASE=on`** (on) — on a negative day-P&L book, a requested loss-cut past `ROTATION_GUARD_MIN_LOSS_PCT` is never vetoed.
- **`PUT_BREAKDOWN_EXT_PCT=5`** (5; 0 = off) — direction-gate carve-out: a name at least this % below its 20d SMA keeps put candidacy in an up market (broken momentum names sit above their 200dma, so the old carve-out never fired).
- **`THESIS_DECAY_ENABLED`** — code default flipped **off -> on** this date (losers held 3.8d vs winners 2.5d); `.env` updated to `on` to match.

---

## Addendum 2026-08-01 — bearish-verdict forcing + per-name falling read

- **`NAME_DROP_DEFENSE_PCT=4`** (4; 0 = off) — per-NAME falling read: a HELD name down this % on the day (live price vs prior daily close) arms a name-level defense, whatever the index reads. Effects: the HELD prompt line tells the model the read fired and that a loss-cut passes the guard; the rotation guard releases loss-cut SELLs on that name even on a green book day (the red-day release only covers losing sessions). Jul 29 fixture: NOK -4.8% sat guard-pinned ~2h on a day SPY bottomed -1.2% — under every index trigger.
- *(no new variable)* **bearish_verdicts** — the decision output schema now REQUIRES a verdict per put-ELIGIBLE bearish name (`put_proposed` or `declined` + the missing evidence). Declines/omissions land in the decision journal as `put_declined` / `put_ignored` and on the BEARISH FUNNEL log line (`ELIGIBLE (...) -> declined: ...` / `-> put proposed` / `-> IGNORED`), so put-path dormancy is queryable instead of silent. Run-7 (Sep 12 2026, item A5): the funnel line renders EVERY put-eligible name's terminal stage — only gate-blocked/off-slate names are capped at the 6 most bearish (with a `…+N more` tail) — an eligible name with no reconciled verdict renders `-> IGNORED`, reason text is clipped to 60 chars, and the summary tail carries `ignored=N` beside `put_proposals`/`put_approved`. Before this, the top-6-by-score cut dropped the 7th/8th-ranked names, so the three `ELIGIBLE but IGNORED` verdicts of Sep 10 2026 (ABT, KORU, STE) never appeared as `-> IGNORED` and the run-6 window was scored "0 IGNORED" on a truncated line. Grep handles: `-> IGNORED` (funnel line) and `ELIGIBLE but IGNORED` (per-name WARNING) now agree.

---

## Addendum 2026-09-12 — run-7 measurement knobs (ledger)

- **`LEDGER_RESTATE_AT_FILL`** — value: *(unset → `on`)*
  - Loaded: `TradeLedger.__init__(restate_at_fill=None)` resolves the env key with `config._flag` semantics (`on/true/1/yes`) — deliberately **not** a `Config` field: every consumer builds the ledger bare (`TradeLedger()` in the orchestrator, post-mortem, dashboard, track record, autotune) with no `Config` in hand. Read after `.env` is loaded; a caller may pass the bool explicitly, and `set_fill(..., restate=)` overrides per call.
  - Purpose: when the FILLED reconcile stamps the broker's `filled_avg_price` onto an **equity SELL** row (`LEDGER_FILL_PRICES`, run-6 item 1e), also restate that row's `exit_price` / `realized_pl` / `realized_pl_pct` at the fill. The figures as first recorded survive on the row as `quote_exit_price` / `quote_realized_pl` / `quote_realized_pl_pct`, so `realized_pl − quote_realized_pl` is the row's fill slippage. Run-6 evidence: 7/14 closed rows carried the submission-time quote, net −$72.24 vs the fills (PSQ hedge_unwind read +$41.76 at the quote, −$61.07 filled), so the contract's "|ledger − broker realized| < $5" condition was unmeasurable.
  - Impact: measurement-only — no runtime gate reads a ledger row's `realized_pl` at decision time; the eval checker, attribution, track record, post-mortem and FIFO lots see fill-based exits. The restatement is a pure function of the `quote_*` figures (a refined fill re-restates from the original, never compounds), is computed on the ROW qty (a partial-fill correction still scales it in `effective()`), and never touches `qty`, `cost_usd`, BUY rows (`entry_price` stays the decision quote) or **option rows** (annotation-only: a multi-leg `filled_avg_price` is a per-spread net debit/credit that does not map onto the group's realized $). Rows with no usable basis (no realized $, qty 0, basis ≤ 0) are stamped but not restated, with a `WITHOUT restatement (reason)` log line. Greppable: `Ledger: RESTATED SELL <sym> at fill <px> (quote <px>): realized $a -> $b (+/-d) [order <oid>]`; exchange-backfill rows (run-7 A6) are written with `fill_price` / `fill_qty` / `fill_ts` already stamped from the broker's closed order (`filled_avg_price` / `filled_qty` / `filled_at`; log `Backfilled exchange exit: ... [fill stamped <ts>]`) regardless of `LEDGER_FILL_PRICES` — no `set_fill` call and nothing to restate; re-stamping one at the same fill logs `fill <px> matches recorded exit_price ... unchanged`.
  - Special values: `off` = the run-6 behaviour exactly (`set_fill` stamps `fill_price/fill_qty/fill_ts` and touches nothing else; no `quote_*` fields written).
  - Interactions: only acts when `LEDGER_FILL_PRICES=on` gets `set_fill` called; the checker's ledger-vs-broker reconciliation (contract v3) should read `quote_realized_pl` for the pre-fill figure rather than recomputing it. Run-7 4a-18 (critic #13) extends the same knob to **option BUY** rows' `cost_usd` (the proposal's estimated debit → the fill; as-recorded debit kept as `quote_cost_usd`) — see the 4a-18 addendum below; equity BUY rows are still never restated.
  - Verdict: leave `on`; flip `off` only to reproduce a run-6-style ledger for a like-for-like comparison.

---

## Addendum 2026-09-12 — run-7 strategy knobs (S-2: hedge notional divided by the measured hedge-ETF beta)

- **`HEDGE_BETA_ASSUMED`** — value: *(unset → `-1.0`)*
  - Loaded: `Config.hedge_beta_assumed` (`config._hedge_beta_assumed()`); must lie in `[-3.0, -0.5]`, else `load_config` WARNs (`HEDGE_BETA_ASSUMED=... is not a SPY-beta in [-3.0, -0.5]; using the default -1.0.`) and uses `-1.0`.
  - `.env` doc line: `# SPY-beta assumed for HEDGE_ETF ONLY when its own series cannot be read or reads outside [-3,-0.5]; the measured, shrunk, cycle-cached beta is used otherwise`
  - Purpose: the beta hedge (`AUTO_HEDGE_MODE=beta`) now sizes the inverse ETF to `max(0, book_spy_beta - target) x equity / |hedge_beta|` (`portfolio/beta.py: hedge_target_notional(..., hedge_beta=-1.0)`), where `hedge_beta` is the hedge ETF's OWN shrunk, cycle-cached SPY-beta from the book-beta reader (`BookBeta.beta_of(HEDGE_ETF, 'SPY')` — the same number the reading prices a held hedge at, so a fresh arm lands ON target). This knob is the **fallback only**: used when the reader is absent/blind, the ETF's history is too short (`None`), or the measured value reads outside `[-3.0, -0.5]` (a positive read would size a "hedge" that adds exposure; a spurious `-0.3` would double the order). Run-6 evidence: the gap was never divided while PSQ read `-1.51` vs SPY (PSQ is `-1.0 x QQQ`; QQQ's SPY-beta — 1.51 shrunk / 1.64 raw on 60 d as of Sep 11 2026 — is what drifts), so Sep 3 10:14 and Sep 10 11:06 a 1.20 read bought $203,605 / $204,391 of PSQ and the next reading landed at 0.89 / 0.90, 0.05 above the 0.85 unwind line instead of ~1.00; correct size ~$135k, ~$68k idle per arm, and the over-hedged reading handed the buy-path beta cap extra room (Sep 3 12:51 re-arm hit the cash lock).
  - Impact: hedge notional per arm ≈ -33% at today's QQQ beta (hedge dollars hedge the SPY exposure they claim); post-arm reading ≈ target; less cash burn; the cap/hedge ratchet loosens. Strategy-affecting (changes how big the hedge is) → new run-7 fingerprint key.
  - Greppable: `AUTO-HEDGE: beta: book spy-beta 1.20 > target 1.00 + 0.15 band — bought $135099 of PSQ (hedge $135099/$135099, ceiling 40% of equity; hedge beta -1.51 measured; at -1.0 would be $204000).` — the divisor, its source (`measured` | `assumed`) and the counterfactual notional the undivided formula would have sent under the same ceiling/cash clamps. Fallbacks log one INFO line at arm time: `Auto-hedge: PSQ SPY-beta unmeasured (no reader/short history) — using assumed -1.00.` / `Auto-hedge: PSQ measured SPY-beta 0.40 outside [-3.0, -0.5] — using assumed -1.00.` / `Auto-hedge: PSQ SPY-beta read failed (...) — using assumed -1.00.` `state/risk_state.json` carries `hedge_beta` / `hedge_beta_source` beside `book_beta` (stamped at each arm resolution, cash-clamped arms included); the ledger row's `risk_note` reads `... at hedge beta -1.51 (measured) ...`.
  - Special values: `-1.0` (default) = the pre-S-2 sizer's implicit assumption, so a blind reader reproduces run-6 sizing exactly. Do NOT pin it to today's `-1.51` — it drifts with QQQ's SPY-beta; the measured path exists precisely so no constant goes stale again.
  - Interactions: `AUTO_HEDGE_MAX_PCT` still caps NOTIONAL (undivided); `HEDGE_BETA_TARGET` / `HEDGE_BETA_BAND` unchanged. `MAX_BOOK_BETA_SPY` stays `1.2` (run-7 decision 6); `load_config` now logs ONE INFO line making the cap/arm geometry greppable per run: `BOOK BETA CAP vs hedge arm line: cap 1.20 - (target 1.00 + band 0.15) = +0.05 (cap-bound buys land inside the arm zone)` (negative margin renders `cap-bound buys stay at/below the arm line`; suppressed when the cap is `0` = off).
  - Verdict: leave unset. Tests: `tests/test_run6_beta.py` (`test_hedge_target_notional_divides_by_hedge_beta`, `..._default_is_minus_one`, `..._ceiling_unchanged_by_hedge_beta`, `test_beta_hedge_uses_measured_beta_and_logs_source`, `test_beta_hedge_measured_beta_lands_reading_on_target`, `test_beta_hedge_falls_back_on_bad_measured_beta`, `test_beta_hedge_counterfactual_and_topup_respect_ceiling_and_cash`, `test_hedge_beta_state_round_trip_and_legacy_file`, `test_hedge_beta_assumed_config_default_env_and_validation`, `test_config_logs_beta_cap_vs_hedge_arm_line`).

---

## Addendum 2026-09-12 — run-7 S-6: `TOPUP_MIN_CONVICTION_DELTA` (existing key — the bar is now printed in the prompt and compared at 4 dp)

- **`TOPUP_MIN_CONVICTION_DELTA`** — value: *(not in `.env` → `0.05` from `load_config`; the `RiskLimits` dataclass default is `0.0` = gate off)*
  - `.env` doc line: `# a top-up must beat the last buy's conviction by this; the bar is printed on the HELD line and compared at 4 dp`
  - Purpose: the **top-up evidence gate** (`risk.py`, "Top-up evidence gate"): a BUY of a name already held is rejected unless `round(conviction, 4) >= round(last_buy_conviction + delta, 4)`. "Adding to a winner" with the same number is a reflex, not new evidence. Fails **open** when no prior stamp exists (`state.last_buy_conviction` rides the 7-day buy clock, `state._CLOCK_RETENTION_DAYS`), so a name last bought > 7 days ago can be topped up at any conviction.
  - Evidence (run-6, Sep 1–11 2026): 37 of 77 risk-judged equity BUYs (48%; 16% of all 235 emitted incl. slate-excluded) died here — 12 re-proposed the entry number exactly, 20 were +0.01..+0.04 over, 5 were BELOW the prior; ~4.6 rejections/session. The prompt showed the anchor (`entry conviction 0.66`) but never the rule, then echoed `your last verdict today: BUY conv 0.66` without saying it had been rejected. Float edge: raw `prev + 0.05` is `0.7100000000000001` for prev 0.66, so a proposal exactly at the printed bar was rejected (`logs/Jul_10_2026.log` 08:57:44 LASR 0.60 vs prior 0.55).
  - What S-6 changed (no new key, value unchanged): (1) the gate computes `bar = round(prev + delta, 4)` via `risk.topup_bar()` and compares `round(conviction, 4) < bar` — a proposal **exactly at the bar now passes**; (2) the reject reason prints the bar; (3) `orchestrator._held_notes` appends the bar to every HELD line, read from the **state** stamp only (never the ledger fallback), so printed == enforced and nothing prints once the stamp is pruned; (4) a same-day BUY the gate rejected is echoed as such; (5) the cached risk-contract block states the rule once.
  - Greppable: `REJECT buy NOK: Top-up conviction 0.66 shows no new edge over prior entry 0.66 (bar 0.71 = last buy +0.05) — 'adding to a winner' is not a signal.` (risk); HELD line `top-up needs conviction >= 0.71 (+0.05 over the last buy) — else HOLD`; prior-verdict echo `your last verdict today: BUY conv 0.66 — rejected at the top-up bar`; contract line `A top-up BUY of a held name is rejected unless its conviction clears the bar printed on that name's HELD line (last buy + 0.05)`.
  - Impact: ↑ = a top-up needs a bigger conviction lift (fewer adds, more HOLDs); ↓ = easier adds; `0` = gate off and every S-6 prompt line disappears with it. Pre-registered run-7 metrics: top-up rejections per session (run-6 baseline 4.6; target < 2) **and** approved top-ups per session + their conviction distribution — the conviction-inflation counter-metric is essential, since the fix could simply teach the model to print 0.71.
  - Interactions: independent of `MIN_ADD_INTERVAL_HOURS` (4h spacing — both must pass); `MIN_NEW_NAME_CONVICTION` exempts top-ups, this gate is the top-up-side bar; `MAX_DAILY_BUYS_PER_SYMBOL` still caps the count. Prompt text changed → strategy-affecting for pooling (new run-7 fingerprint).
  - Verdict: keep `0.05`. Changing the value changes both the gate and every HELD line in one place.

---

## Addendum 2026-09-12 — run-7 S-7: core-defense trim mechanics (no new variable)

- *(no new variable)* **`CORE_DEFENSE_TRIM_PCT`** (code default `25`, not in `.env`) / **`CORE_DEFENSE_ENABLED`** (`on`) / **`BREADTH_FALLING_NAMES_MIN`** — values unchanged; what the trim DOES changed (`orchestrator._apply_core_defense`, `_core_trim_sell`, `_market_falling`; `alpaca_client.open_stop_sells`, `replace_order_qty`).
  - Evidence (`logs/Sep_11_2026.log` 08:30:15-08:35:11): the Sep 10 14:38 falling-names map {DRAM, INTC, SEI} (17h52m old) fired the breadth leg at the next morning's first cycle on a risk-on +0.9% open; the trim canceled the 163-sh QQQ GTC stop and sold in the same instant — `reduce_position(QQQ, 40) failed ... available 0.4975 ... held_for_orders 163` (the cancel was still `pending_cancel`); `_ensure_core_stop` read the pending_cancel stop back as "already right" and cleared the retry flag — **core stopless 4m53s**, trim never retried; the beta hedge held against a 0.80 target read from the same stale map.
  - (1) `open_stop_sells` skips `pending_cancel` / canceled / expired / replaced / any terminal status (`resting_only=True` default; the cancel-fallback poll passes `False` to see the settling cancel).
  - (2) The falling-names map carries the ET date + `HH:MM` it was computed on; `_market_falling`'s breadth leg (core defense AND the beta-hedge falling target) ignores a map from a previous session. The within-day carry (Aug-23 double-count guard) is untouched.
  - (3) Trim = `ReplaceOrderRequest(qty = stop_qty - trim)` on the resting GTC stop, then the sell (operator decision 4: replace-qty-down; "replace on live legs, never cancel-then-resell"). If the venue refuses the replace: cancel → poll `open_stop_sells(resting_only=False)` every 0.5 s ≤ 5 s until the cancel settled → sell.
  - (4) Any failure keeps `_core_stop_gap=True` for the 30 s watchdog and never re-places a stop inline while a cancel may be settling; the WARNING carries the broker's `available` qty. `_ensure_core_stop` also no longer cancels a smaller-but-correct stop when the missing shares are held by a working sell (pre-market DAY trim waiting for the bell).
  - (5) Rider: the sub-share residual (163.4975 → 0.4975) rides along with the trim so the integer stop covers 100% of what is left.
  - Greppable: `CORE DEFENSE: stale falling map (2026-09-10 14:38, 3 names) ignored at new-day open; would have trimmed 40.4975 QQQ ($29k).` · `CORE DEFENSE: stop 10951625 replaced 163 -> 123 sh, trimming 40 (new stop …).` · `CORE DEFENSE: replace of stop … refused — falling back to cancel -> poll -> sell.` · `CORE DEFENSE: trim of 40.4975 QQQ NOT submitted (…; broker available=0.4975 of 163.498 sh) — GTC stop re-placement left to the ~30s watchdog retry …` · `BREADTH STALE MAP: 3-name falling map from 2026-09-10 14:38 predates today's ET session — ignored …` · `Core stop: resting 123-sh stop … left alone rather than canceled into a reserved-qty reject; watchdog retry stays armed until the sell resolves.` (fix-pass: the retry flag is KEPT armed here — the snapshot's `qty_available` may be minutes stale) · `Core stop: 163-sh stop … is pending_replace — left alone this pass (replace settling at the venue); watchdog retry stays armed.` · `CORE DEFENSE: waiting for the replace to free 40.4975 QQQ sh (broker available=0.4975) — polling up to 5s.` · `CORE DEFENSE: trim sell of 40.4975 QQQ refused after the replace — stop … restored 123 -> 163 sh (broker available=…).` · `CORE DEFENSE: trim of 40.4975 QQQ NOT submitted (working QQQ BUY ($5,000) would wash-block the trim sell — stop untouched, trim deferred to next cycle) …` · `Auto-hedge: beta: unwind deferred — 3-name map predates today; deciding on this cycle's fresh breadth read (book spy-beta 0.84 < target 1.00 - 0.15 band; holding $203000 PSQ this pass).` (fix-pass: a held hedge is never sold on the stale-map pass and re-bought minutes later on the fresh map). The cross-day map is also invisible to the HELD-line note, the sell-authority `name_falling:` tag, the loss-cut release and the 4a-15 `falling_names` stamp (`Orchestrator._falling_names_today()`).
  - Classification: strategy-affecting (changes WHEN the core is sold: a wrong-day trim no longer fires; a real falling day still trims within its own session). Tests: `tests/test_core_defense.py` (8).

---

## Addendum 2026-09-12 — run-7 S-8: `HEDGE_UNWIND_MIN_CYCLES` (beta-mode unwind noise guard) + 4a-17 hedge observability

- **`HEDGE_UNWIND_MIN_CYCLES`** — value: *(unset → `1` = today's one-read unwind; fingerprint-neutral at the default)*
  - Loaded: `Config.hedge_unwind_min_cycles` (`config._hedge_unwind_min_cycles()`); must be a whole number `>= 1`, else `load_config` WARNs (`HEDGE_UNWIND_MIN_CYCLES=... is not a whole number >= 1; using the default 1 (one-read unwind).`) and uses `1`.
  - `.env` doc line: `# consecutive below-band beta readings (one per decision cycle) before the beta hedge is closed; 1 = legacy; run-6 counterfactual: any N<=9 would not have kept the Sep 9 hedge`
  - Purpose: `AUTO_HEDGE_MODE=beta` closes the whole hedge on ONE reading below `HEDGE_BETA_TARGET - HEDGE_BETA_BAND` (`orchestrator._apply_beta_hedge`, `hedge_signal` is stateless). Run-6 Sep 9 09:22 ET: the reading fell to 0.44 after the same-cycle exits, 10,283 PSQ closed at 25.84, and the hedge re-armed Sep 10 11:06 at 26.09. The counterfactual (VERIFY A6-2): 13 consecutive below-band reads followed at the measured PSQ beta (9 at −1.1), so **no `N <= 9` would have kept that hedge** — the knob guards a *noise-driven* unwind (no-trade drift `<= 0.04`/cycle vs the 0.15 band; n=0 such reads in run-6), not the Sep 9 whipsaw. Default 1 ships (operator decision 5); `2` would be a live change on no evidence.
  - Mechanics: a dedicated `Orchestrator._unwind_reads = (cycle_seq, n)` — **not** `_clear_cycles` (the falling-mode counter) — increments at most once per decision cycle, because `_breadth_rearm` re-runs `_apply_auto_hedge` inside one cycle (`logs/Sep_10_2026.log` 14:35:37 and 14:38:52 are the same cycle); reset on an arm, on a hold, and on the beta-unavailable early return; deliberately NOT reset on a declined close (no order id), so the next cycle retries at once. In-memory: a restart only ever delays an unwind by re-counting.
  - Greppable: `Auto-hedge: beta: unwind read 1/2 — holding $204156 PSQ (book spy-beta 0.80 < target 1.00 - 0.15 band; would have closed 7834.07 PSQ at HEDGE_UNWIND_MIN_CYCLES=1).` — the counterfactual on every held read; the close line is unchanged (`AUTO-HEDGE UNWIND: beta: ...`).
  - Verdict: leave `1` for run-7. Pre-register a switch to `2` only if run-7's `HEDGE COUNTERFACTUAL:` lines show an unwind that a second read would have reversed.
  - Classification: ops-only at the default (a knob; no order changes); a STRATEGY KEY once set `>= 2`.
- *(no new variable)* **4a-17 hedge observability** (`portfolio/beta.py`, `orchestrator.py`, `state.py`, `postmortem.py`):
  - `BOOK BETA:` gains `hedge=PSQ w=0.258 beta=-1.51 unhedged=1.20` — `unhedged` = the same reading with the hedge ETF's weight removed (`BookBetaReading.hedge_view()`); `w=0` when `HEDGE_ETF` is configured but not held; segment absent with `HEDGE_ETF` unset. `state/risk_state.json` `book_beta` carries `hedge_etf` / `hedge_w` / `hedge_beta` / `unhedged_spy`. Run-6's Sep 9 0.44 read was an ex-hedge 0.82 and nothing printed it.
  - `BOOK BETA (post-exec): spy=1.21 qqq=0.85 iwm=0.99 invested=64.0% hedge=PSQ w=0.201 beta=-1.51 unhedged=1.51 (pre-exec spy=1.13 delta=+0.08; CROSSING pre=hold post=arm)` — after `_execute_proposals` every cycle, re-read against the mutated snapshot (cycle-cached series; at most the new names fetch). `CROSSING pre=… post=…` appears only when the hysteresis signal differs between the two reads at the cycle's own target (LF-6: run-6 had zero; a second hedge pass ships only after `>= 5` in-window crossings). `unavailable (…)` when the reader is blind post-exec; no line when it was blind pre-exec.
  - `HEDGE COUNTERFACTUAL: last unwind lot 10283.2 sh @25.84 would be +$2,571 today (PSQ 26.09 now; unwound 2026-09-09 09:22 ET; session 1/5).` — one line per decision cycle for the unwind day (session 0) and the next five ET sessions. Definition pinned (critic 6): `qty x (price now - the unwind's DECISION quote)`, priced at the held ETF's snapshot price when the hedge is on again, else one `latest_price` fetch. `risk_state.json` `last_unwind` = `{symbol, qty, price, date, at, sessions}` (sessions counted in state so a restart cannot re-count).
  - Nightly post-mortem (`postmortem.hedge_diagnostics`, under "Behavior diagnostics"): `HEDGE WHIPSAW: 1 of 1 arm(s) today re-armed within 2 sessions of an unwind (PSQ unwound 2026-09-09 09:22 ET -> re-armed 2026-09-10 11:06 ET, 1 session(s), $204,391).` (ledger `auto_hedge` buys vs `hedge_unwind` sells; weekday sessions, holidays not subtracted) and `BOOK BETA CAP -> next-cycle arm: 1 pair(s) today (cycles 8; cap-bound cycles 2; arms 1).` (day's log split into cycles at the one `BOOK BETA:` line each; `n/a (no log lines for …)` when the log is unreadable — never a silent zero), plus the day's last `HEDGE COUNTERFACTUAL:` line verbatim. The config-load geometry line `BOOK BETA CAP vs hedge arm line: …` (S-2 addendum) is what these counts are read against.
  - Tests: `tests/test_hedge_unwind_guard.py` (12).

---

## Addendum 2026-09-12 — run-7 4a-15 / 4a-16: buy-row shadow fields (no new variable)

- *(no new variable)* **decision-time shadow fields on ledger BUY rows + `floor6_would_survive` on STOP exits** (`ledger.py` `TradeRecord` / `EntryTape`, `orchestrator._entry_tape`, `orchestrator._backfill_exchange_exits`, `monitor/watchdog._floor_shadow_job` + `orchestrator._drain_floor_shadow_jobs` + `ledger.TradeLedger.set_floor_shadow`, `regime.RegimeReader.current`). Fix-pass: the watchdog's hard-stop row is recorded with the shadow None and a job queued (`FLOOR6 SHADOW: NU stop deferred to the decision thread (basis 100.00; no bars fetch on the safety loop).`); the decision thread fetches the closes right after the exchange-exit backfill and stamps the row — no network call on the safety loop under the trade lock. Measurement only — nothing sizes, stops or sells differently; the rule parameters (`SHADOW_STOP_FLOOR_PCT=6.0`, `SHADOW_HAIRCUT_SPY_PCT=-0.3`, `SHADOW_HAIRCUT_FALLING_MIN=2`, `SHADOW_HAIRCUT_MULT=0.5`) are module constants, deliberately NOT `.env` knobs, so a counterfactual cannot be tuned into a guard.
  - Evidence: run-6 buy rows carried no intraday-SPY / regime / name-falling field, so the refuted "red-tape entries lose" claim (pooled: red-day entries +$88 vs green +$187; date-clustered diff CI [-249, +1,907]; prior-day-down is the BEST bucket) could not be tested ex ante (`logs/Sep_09_2026.log` 08:30:30 / 09:22:40 / 10:14:43 `Market regime: ... today -0.3%/-0.4%/-0.6% ... -> risk-on` with the NOK/DRAM/SNXX/ONON buys under it); the 4% clamp floor "do-not-do" (4 vs 4 stop-outs, Fisher p=0.18) likewise had no per-row column.
  - BUY rows (`TradeRecord.from_equity(..., tape=EntryTape)`): `spy_intraday_ret_at_decision` (= `Regime.day_change_pct`, the read the `Market regime:` line printed, via the no-fetch `RegimeReader.current()`), `regime_label` (applied label), `falling_names` (sorted keys of the cycle's NAME FALLING map), `would_haircut_usd` (0.5 x submitted notional when SPY intraday <= -0.3% AND (narrow breadth OR >= 2 NAME FALLING); 0 otherwise; None when SPY is unknown), `stop_pct_if_floor_6` (= `min(max(2 x sigma_d, 6), 10)` then the STOP_COVER_EXTENSION widening — `risk._exit_levels` arithmetic with the floor swapped; None when vol stops are off / vol unknown), `vol_stop_raw_pct` (the unclamped `mult x sigma_d`, so ANY floor replays ex post).
  - STOP exits (`bracket_stop` via the backfill, `stop` via the watchdog's fractional hard stop): `floor6_would_survive` = the trip's worst CLOSE-to-close drawdown from the FIFO basis (window: opening lot's date .. exit date, `AlpacaClient.daily_close_series`) never reached 6%; `floor6_worst_close_pct` beside it. Close-based proxy (bracket stops fire intraday, so it is a lower bound on touches); at the floor = touched. None (excluded from the paired test) when the series is unreadable or the ledger holds no lot — never a false "survived". Every other exit reason leaves both None. Legacy rows load with every field None / `[]`.
  - Greppable: `ENTRY TAPE: NU spy_intraday=-0.42% regime=risk-on falling=2 would_haircut=$15,458 stop=5.71% stop_if_floor6=6.00%` (one per equity buy; `n/a` where a read was unavailable) · `FLOOR6 SHADOW: NU bracket_stop worst_close=-5.00% vs basis 100.00 (live stop 4.00%, floor under test 6%) -> would_survive=True (close-based proxy)` (one per stop exit with a readable series).
  - Re-evaluation bars (pre-registered): the haircut only at >= 15 independent SPY-down dates with closed trips (pooled today: 10-11); the clamp floor only on a paired `floor6_would_survive` test over >= 20 tight-stop exits, widening only. Not shadowed: the size a 6% floor would imply under the per-trade $-risk cap (derive offline from `cost_usd`, `stop_loss_pct`, `stop_pct_if_floor_6` and the row's `risk_note`).
  - Classification: ops / measurement-only (fingerprint unchanged). Tests: `tests/test_ledger.py::test_buy_row_carries_decision_context`, `::test_shadow_stop_equals_min_max`, `::test_legacy_rows_without_shadow_fields_still_load`, `::test_would_haircut_rule_and_unknown_spy`, `::test_floor_would_survive_from_closes`; `tests/test_entry_tape.py` (9).

---

## Addendum 2026-09-12 — run-7 4a-18 (+ critic #13): sell rows stamp their entry lot; the loader drops phantom/duplicate sells; option BUY `cost_usd` restated at fill (no new variable)

- *(no new variable)* **SELL rows carry their entry lot's attributes at close** (`ledger.py` `TradeLedger.record()` → `_stamp_entry_lots`, one implementation for every sell path: decision sells, watchdog exits/trims/flattens, hedge unwinds, core defense, the exchange-exit backfill). Fields (None / `[]` on BUY rows and on rows predating them): `entry_ts`, `entry_fill_price` + `entry_fill_source` (`fill` when `set_fill` stamped the buy under `LEDGER_FILL_PRICES`, else `quote` = the decision quote), `entry_composite`, `entry_conviction`, `entry_stop_pct`, `entry_key_signals`, `lots_n`. Source = the FIFO lots the sell consumes (`lots.py` `Lot` now carries `fill_price` / `conviction` / `composite_score` / `stop_loss_pct` / `key_signals`; `fifo_lots()` is the non-consuming slice); a multi-lot exit takes the OLDEST lot (the buy that opened the episode) and counts the lots. Options: the oldest still-open option BUY on the same key (same symbol, or an OCC leg in common), attribution's episode rule. No lot → `lots_n=0`, every field None, never a guess. Evidence: the pooled analysis had to re-join 197 sells to their buys offline and 39 pre-run-5 rows never joined.
  - Greppable: `LOT STAMP: NU decision entry_ts=2026-09-01 14:00 entry_fill=100.3700 (fill) conviction=0.66 composite=+1.42 stop=5.71% lots_n=1 key_signals=[...]` · `LOT STAMP: MSFT external has no ledger lot (pre-ledger shares or phantom) — sell row carries no entry attributes (lots_n=0)`.
- *(no new variable)* **Phantom / duplicate SELL rows dropped by the loader** (`ledger.py` `dedup_sells`, applied inside `TradeLedger.effective()` AFTER the reconcile corrections, `effective(dedup=False)` returns the corrected stream; `phantoms()` lists the drops; the file is never rewritten). Rule: a SELL row is dropped when its qty is **negative**, or when a LATER sell on the same (symbol, instrument, exit_reason) with the same qty lands within `PHANTOM_DUPE_WINDOW_H = 4` h and its `realized_pl` matches to the cent OR differs by exactly qty × the `exit_price` delta (same shares, same basis, re-marked). The EARLIER row is the phantom (the replaced / expired submission), the later row carries the exit; a row with a broker fill stamped (`fill_price`) is never dropped. `qty == 0` is KEPT — the pre-qty legacy shape means "full close, size unknown" (`lots.py` consumes every open lot for it); a deliberate refinement of the contract's "qty <= 0" wording (the pooled history holds no qty-0 sell with a P&L, so the two readings agree on every real row). Pooled replay (529 rows): 26 rows dropped = AVAV 2026-07-07 flatten qty=-37 (+$365.04, a snapshot that read the position short after the Jul-7 bracket double-fill), AVAV 2026-07-08 trail re-replaced 36 s later (+$212.01 → +$213.98 = 37 × $0.0532), the LLY Jul-9 storm (`REDUCE LLY qty=0.349754` re-submitted every minute against the same available fraction, 25 rows → 1), T 2026-07-23 option flatten -$2,700 ledgered twice (expired DAY close resubmitted); realized sum 18,144.16 → 20,206.00. Run-4/5/6 ledgers: 0 drops (the watchdog's `_supersede_exit_record` has voided replaced ids by correction since Aug).
  - Greppable: `LEDGER PHANTOMS: dropped 2 SELL row(s) worth $+577.05 that the realized sum would otherwise carry (kept 5 rows): AVAV equity flatten 2026-07-07 14:54 qty=-37 $+365.04 [negative_qty]; AVAV equity trail 2026-07-08 13:23 qty=37 $+212.01 [replaced_dupe] (exit carried by order bf06...)` — once per distinct set per process, capped at 10 rows (`; ... and N more (ledger.phantoms() lists all)`).
  - Checker (`scripts/eval_contract_check.py`, v3 only; v1/v2 untouched): `drop_phantom_sells` is a dict-level mirror of the same rule (the script stays import-free), applied to the window ledger and every `--pool` file before rule 1; always prints `phantom/dupe sell rows dropped (this ledger): n=3 sum=$-2,122.95 [negative_qty=1 replaced_dupe=2]  (--show-dropped lists them)`; **`--show-dropped`** adds one `  dropped: 2026-07-08T13:23 AVAV equity trail qty=37.0 $212.01 [replaced_dupe] (exit carried by order bf06)` line per row. `tests/test_ledger.py::test_checker_phantom_rule_matches_the_ledger_rule` pins the two rules to the same dropped set.
- **`LEDGER_RESTATE_AT_FILL`** (existing key, extended): `set_fill` on an **option BUY** row also restates `cost_usd` from the proposal's estimated debit to `fill_price × 100 × row qty`, keeping the as-recorded figure as `quote_cost_usd` (pure function of it — a refined fill re-restates, never compounds; `effective()` scales both on a partial-fill correction). Only when the recorded cost IS `entry_price × 100 × qty` (within $1 / 0.5%, the identity risk sizes every option debit with); otherwise stamped without restatement and the reason logged. `entry_price` (the decision premium) stays; equity BUY rows are never restated; option SELL rows keep the annotation-only convention. Evidence (critic #13): HD261016P00350000 2026-09-02 ledgered at $3,213 (32.13 × 100), filled at 34.30 = $3,430 — $217 light in every premium-cap / option-P&L read off the ledger.
  - Greppable: `Ledger: RESTATED OPTION BUY HD261016P00350000 cost at fill 34.3000 (quote 32.1300): $3213.00 -> $3430.00 (+217.00) [order <oid>]` · `Ledger: fill 32.1300 matches recorded premium on OPTION BUY <sym>: cost $3213.00 unchanged [order <oid>]` · `Ledger: fill <px> stamped on OPTION BUY <sym> WITHOUT cost restatement (<reason>) [order <oid>]`.
  - Special values: `off` = annotation only for option buys too (the run-6 behaviour).
- Classification: ops / measurement-only (fingerprint unchanged; nothing sizes, stops or sells differently). Disclosure, as for B2: `effective()` feeds `attribution.render_lessons` (the prompt's track-record block, gated at `TRACK_RECORD_MIN_TRIPS`) — a phantom-free series changes that INPUT only where a source's pooled trips included a phantom (none in run-4/5/6). Tests: `tests/test_ledger.py` section "run-7 4a-18" (14), `tests/test_lots.py` (2), checker `--selftest` block (10).

---

## Addendum 2026-09-21 — A+ change-set: `CORE_FILL_BETA_CLAMP`, `HEDGE_STARVED_CORE_TRIM`, `HEDGE_STARVED_TRIM_MAX_PCT`, `REGIME_FALLING_TAPE_CAP`

All four are STRATEGY KEYS (they enter the run-7 fingerprint). Code defaults preserve run-7-as-reviewed behaviour; the run-7 `.env` block (`docs/RUN7_SWITCH.md` 1c) turns them on. Tests: `tests/test_aplus_changeset.py`.

- **`CORE_FILL_BETA_CLAMP`** — value: *(unset → `off`; run-7 block: `on`)*
  - Loaded: `Config.core_fill_beta_clamp`. Read in `Orchestrator._core_fill_beta_clamp` from `_apply_core_fill` (risk-core fill only; the defensive T-bill fill is exempt).
  - Purpose: the core-ETF sweep never passed through the buy-path beta cap, so it bought a ~1.5-beta ETF toward `TARGET_INVESTED_PCT` whatever the book read. Sep 16-18 2026: cash $339k → $20k, book spy-beta 1.09 → 1.14; a weekend drift then read 1.20 with nothing spendable for the hedge. On: the fill is sized to `(HEDGE_BETA_TARGET − book beta) × equity / core beta` — the sweep never adds beta beyond the level the hedge steers to (an arm-line clamp re-bought a starved trim back up to 1.15 in simulation); below the min order it is skipped, and it is skipped outright in a cycle that trimmed the core for an unfunded hedge. No book reading, or `AUTO_HEDGE_MODE` ≠ `beta` → unchanged (fail open).
  - Greppable: `CORE FILL BETA CLAMP: QQQ fill $50720 -> $33333 (book spy-beta 0.95, beta target 1.00, QQQ beta 1.50).` / `... skipped — book spy-beta 1.20 leaves $0 of room ...` / `Core fill skipped: this cycle trimmed QQQ to fund the beta target (AUTO-HEDGE STARVED) — no same-cycle re-buy.`
- **`HEDGE_STARVED_CORE_TRIM`** — value: *(unset → `off`; run-7 block: `on`)* and **`HEDGE_STARVED_TRIM_MAX_PCT`** — value: *(unset → `50`)*
  - Loaded: `Config.hedge_starved_core_trim`, `Config.hedge_starved_trim_max_pct` (clamped to 0..100). Read in `Orchestrator._starved_hedge_core_trim`, called from `_apply_beta_hedge` on the existing `Auto-hedge: want $X more PSQ but only $Y spendable` branch.
  - Purpose: a hedge that cannot be funded is not a hedge (Sep 21 2026 09:33 CT: book 1.20, invested 98%, `want $213703 more PSQ but only $0 spendable`). The core ETF carries the book's beta, so the bot sells `(beta − target) × equity / core beta` dollars of it — whole shares, at most `HEDGE_STARVED_TRIM_MAX_PCT` of the core per decision cycle, once per cycle — through the S-7 trim mechanics (replace the GTC stop qty-down, then sell; cancel → poll → sell fallback), so the remainder is never stopless. It also runs after a PARTIAL arm when the book would still read above the arm line once that buy fills (run-6 Sep 3 / Sep 18: cash covered $62k of $142k and $35k of $157k). Ledgered `exit_reason="beta_trim"` (a system exit: excluded from the contract's satellite N). Not a thesis exit — no cooldown or loss-streak stamp.
  - Greppable: `AUTO-HEDGE STARVED: book spy-beta 1.20 > target 1.00 + 0.15 band and the hedge is unfunded — sold 107 QQQ ($74900, QQQ beta 1.50) to land near 1.09 (wanted $140000; cap 50% of the core).`
- **`REGIME_FALLING_TAPE_CAP`** — value: *(unset → `off`; run-7 block: `on`)*
  - Loaded: `Config.regime_falling_tape_cap`. Read in `Orchestrator._apply_falling_tape_regime_cap`, right after `_apply_core_defense` each cycle.
  - Purpose: Sep 14 2026 11:59-14:36 CT the regime read risk-on ×1.00 for four cycles (QQQ/IWM back above their 50dma on a partial bar) while the FALLING-TAPE trigger was live and the book was −1.9% intraday. On: while `_market_falling()` is true a risk-on label is applied as neutral ×0.70 for that cycle's sizing, exposure ladder and prompt. Only ever tightens; nothing is persisted (S-5's held label is untouched); clears with the read.
  - Greppable: `REGIME FALLING-TAPE CAP: risk-on x1.00 -> neutral x0.70 while the falling read is live (book:-1.9%).`
- *(no new variable)* **A-1** core fill waits ≤ 5 s for its stop cancel to settle before buying and logs `CORE FILL: BUY of $X QQQ NOT submitted (...)` when skipped/refused · **A-2** `CORE STOPLESS: QQQ 6.2s without a resting GTC stop (cause core_fill)` (WARNING above 60 s) · **A-5** decision-sell ledger rows carry `sell_events`; `scripts/eval_contract_check.py --contract v3` rule 8 counts only unsanctioned rows · **A-6** the decision journal's option rows lead with the strategy and 'Today so far' prints `Options opened (... NOT shares ...): HBAN long_put (BEARISH) 1x (...)` · **A-8** `PortfolioState.last_buy_convictions` is bounded by count (400), not by the 7-day buy clock, so the top-up bar outlives a week-long hold.
