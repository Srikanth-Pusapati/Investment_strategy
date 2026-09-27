# Architecture and design decisions

An LLM-driven, paper-trading portfolio agent. Claude proposes trades from a bundle of independent
signals; a deterministic risk layer decides what is allowed; Alpaca executes; an always-on watchdog
manages exits; every order lands in an audit ledger; and a pre-registered evaluation contract decides,
window by window, whether the strategy has an edge. The system has run seven trial windows on fresh
paper accounts (`runs/`), each judged by a checker whose rules were fixed before the window opened.

This document is the design in the order a systems engineer would ask about it: the question, the
decision taken here, and where it lives. Sizes at the time of writing: ~28.6k lines in
`investment_strategy/` across ~60 modules, 73 test files with 1,535 test functions (~28.4k lines),
172 configuration knobs in `.env` (documented in `env-variable-reference.md`), plus `scripts/` and `ops/`.

## 1. The invariant everything else hangs on: the model proposes, the code disposes

**Question.** If a language model can be wrong, hallucinate a ticker, or talk itself into a trade, what
stops it from losing the account?

**Decision.** The model has no authority. It emits structured JSON proposals (`decision/engine.py`,
schema in `decision/prompts.py`; `TradeProposal` in `models.py`). Every proposal — buy, sell, option —
passes through `RiskManager.evaluate` / `evaluate_option` (`risk.py`), the only path to an order. The
gate is deterministic, its limits live in `.env` and are enforced in code, and every rejection carries a
greppable reason. Sells are the same: under `LLM_SELL_AUTHORITY=events_only` a decision sell is only
approved on a deterministic event the code itself observed (`SELL AUTHORITY:` lines), which is what lets
the evaluation contract audit that the frozen gate was the one that ran (contract rule 8).

Proposals are additionally confined to a whitelist of names the pipeline itself surfaced this cycle
(`Orchestrator._filter_to_slate`), so a hallucinated symbol cannot reach the gate at all.

## 2. Pipeline: four stages, two clocks

`orchestrator.py` ties the stages together and runs two loops (`run`, `_watchdog_loop`, `_tick`).

| Stage | What | Where |
|---|---|---|
| Screen | market-wide discovery of candidates: clustered insider buying/selling, congressional trades, unusual options flow, retail-crowd lists, saved broker scanners, WallStreetBets surges; merged and ranked | `screener/*.py`, `screener/aggregator.py` |
| Signals | one `SignalBundle` per symbol from independent providers — technical, fundamentals, news, insider (paid + free EDGAR), congress, lobbying, government contracts, macro, off-exchange short volume, options flow, options-chain positioning — plus a deterministic composite index and per-signal trend/lag-decay history | `signals/*.py`, `signals/composite.py`, `signals/history.py` |
| Decide | Claude reasons over the bundle and the book, returns proposals and an explicit verdict for every bearish-eligible name | `decision/engine.py`, `decision/prompts.py` |
| Risk + execute | gate, size, submit, ledger | `risk.py`, `execution/alpaca_client.py`, `execution/options.py`, `ledger.py` |

Decision cycles run every 52 minutes (`DECISION_INTERVAL_SECONDS`, chosen so consecutive calls land
inside the prompt-cache window); the watchdog runs every 30 seconds (`MONITOR_INTERVAL_SECONDS`)
independently of the model. A model outage degrades to "no new decisions"; exits keep working.

## 3. Risk limits as code

**Question.** Which limits must exist before the first real order, and who can change them?

**Decision.** Everything below is a knob in `.env`, read once into a frozen typed `Config`
(`config.py`), enforced in `risk.py` / `monitor/watchdog.py`, and greppable by its reject prefix.

- Account: daily-loss halt with a latch (`HALT LATCH set:`), equity floor (`_equity_floor_breached`),
  no-leverage guard, PDT guard, kill switch (`state/KILL`, `KILL_SWITCH`).
- Position: per-position cap (`MAX_POSITION_PCT`), per-symbol total exposure (`MAX_SYMBOL_EXPOSURE_PCT`),
  open-position slots (`SLOT COUNT:`, `At max open positions`), sector cap (`sectors.py`),
  pairwise-correlation guard (`correlation.py`, `Return correlation … >= cap`), earnings blackout
  (`earnings.py`), whole-share and minimum-order floors.
- Entry quality: conviction floors (`MIN_CONVICTION`, `MIN_NEW_NAME_CONVICTION`), a starter haircut for
  low-conviction fresh names, a top-up bar (`TOPUP_MIN_CONVICTION_DELTA`), an anti-chase/overextension
  gate (`Overextended:` — gap-day and ATR-multiple checks), a corroboration gate (`CORROBORATION GATE:`),
  and an evidence-based expectancy gate that is off inside evaluation windows.
- Book: a beta cap on the whole book (`BOOK BETA CAP:`), measured per name against SPY/QQQ/IWM
  (`portfolio/beta.py`).
- Options: defined-risk only — uncovered short legs are refused; per-play and per-underlying premium
  caps (`PER-UNDERLYING PREMIUM CAP:`), liquidity floors (open interest, bid-ask spread), DTE bounds, a
  chase gate, single-name bullish debits switchable off, and a direction gate (calls only above the
  long-run trend, puts only below).
- Sizing: vol-targeted, fractional-Kelly, with vol-scaled ("ATR-style") stops (`R.1`) and a slippage
  edge check.

The rule for changing any of them: a knob defaults OFF in code and is turned ON in a documented `.env`
block, so a trial window's exact configuration ("fingerprint") is explicit.

## 4. The always-on safety loop

**Question.** What happens to open positions between model calls, or when the model is down?

**Decision.** `monitor/watchdog.py` owns exits and never consults the model. Every 30 s it: enforces
hard stops (`_enforce_hard_exits`, `_close_hard`), arms and ratchets trailing stops once a trade has
earned it (R-scaled arming, `_update_trailing_stop`), applies a time stop, checks the equity floor,
marks option positions and exits on premium stop / take-profit / DTE (`_check_option_positions`,
`_confirm_premium_breach`, `_exit_option_group`), and backfills exchange-side exits (bracket stops that
filled while nobody was looking) into the ledger with lot stamps (`_record_exit`). It knows that a
bracket's stop leg sits `held` at the broker and is invisible to an open-orders query, so protection is
audited with a full order scan. When it cannot verify something it pages a human (`notify.py`:
email/webhook CRITICALs, heartbeat withheld when the main loop is stale).

## 5. Broker execution realities

**Question.** Which broker behaviours will bite in production and how are they handled?

**Decision** (`execution/alpaca_client.py`, `orchestrator._reconcile_fills`):
- Entries are bracket orders (take-profit + stop as one OCO). Exits on a bracketed name are done by
  replacing the resting leg to a marketable price, never cancel-then-resell, because a cancelled OCO
  leaves a `pending_cancel` wedge and an unprotected position.
- The core ETF's GTC stop must be cancelled before a core buy or the broker rejects a wash trade; the
  cancel is polled to settle first and the stopless interval is logged (`CORE STOPLESS:`) so it can be
  graded (the ops bar is zero windows over 60 s).
- Fills are reconciled on the next cycle: pending orders persist across restarts, and the ledger row is
  restated at the fill price (`LEDGER_RESTATE_AT_FILL`), so realized P&L is computed from fills, not quotes.
- Options: OCC symbols are built, not trusted from the model; a strike is snapped to the nearest
  liquid one (`STRIKE SNAP:`); a name whose own chain fails liquidity is re-expressed on a liquid index
  ETF (`PROXY PUT PICK:`); multi-leg orders respect the broker's 4-leg cap; entry limits carry a bounded
  buffer over the quoted premium rather than going out as market orders.
- Whole-share flooring, partial-fill handling, transient-network retries with bounded timeouts, and a
  reboot-safe first tick are all explicit paths with tests.

## 6. Portfolio construction

**Question.** How does the book stay near a benchmark's risk while the satellites try to add alpha?

**Decision.** Core-satellite: idle cash is swept into an index core (`CORE_ETF`, capped at
`CORE_MAX_PCT`, target invested `TARGET_INVESTED_PCT`), satellites are the model's picks. Book beta is
measured continuously (`portfolio/beta.py`, `BOOK BETA:` pre- and post-execution) and hedged toward a
target with an inverse ETF that arms and unwinds inside bands (`AUTO-HEDGE:`); a starved hedge (no cash)
trims the core instead of failing silently (`AUTO-HEDGE STARVED:`); a core fill is clamped under the
hedge arm line (`CORE FILL BETA CLAMP:`). A market-regime read (SPY vs 200-day, VIX term structure;
`regime.py`) scales aggressiveness, with hysteresis so it does not flap, and a falling-tape read caps a
risk-on regime at neutral for the cycle. Name-level defense (`NAME FALLING:`) releases a loss cut when a
holding breaks down against a flat index; a rotation guard stops the model from churning a losing name
for a marginally better one; a core-defense path de-risks the core on a falling day.

## 7. Bearish expression

**Question.** Can it make money, or at least lose less, when the market falls?

**Decision.** Shorting is not available in the account, so downside is expressed with defined-risk
puts. The funnel is instrumented end to end (`BEARISH FUNNEL:` per cycle): eligibility is deterministic
(below the long-run trend or a breakdown vs the 20-day), the model must return a verdict for every
eligible name (schema-forced; a missing verdict is logged as `ELIGIBLE but IGNORED`), a proposal goes
through the same liquidity floors as any option, and an illiquid single-name chain falls back to a proxy
put on a liquid index ETF. The funnel's counts are a recorded metric in the evaluation contract.

## 8. Learning loops that are deliberately report-only

**Question.** Where does the system learn, and why is nothing self-tuning?

**Decision.** Four loops observe; none changes a knob on its own. A nightly post-mortem
(`postmortem.py`) writes a dated lesson file; signal attribution (`attribution.py`) scores the signals
the model actually cited on closed trips; a weekly auto-tune (`autotune.py`) replays the ledger against
candidate knob values and prints a recommendation only when the sample supports it; an intraday
decision journal (`journal.py`) lets the model see its own earlier decisions. Adaptive behaviour
(curated-lesson injection, performance-weighted composite, expectancy gate) exists but is switched off
by config inside every evaluation window, because a loop that changes the strategy mid-window makes the
window unmeasurable.

## 9. Measurement and evaluation discipline

**Question.** How would you know it works, and how do you keep yourself from fooling yourself?

**Decision.**
- The ledger (`ledger.py`, FIFO lots in `lots.py`) is the source of truth; realized P&L is restated at
  fills; exchange-side exits are backfilled; duplicate or phantom rows are detected.
- A daily close row (`equity_history.jsonl`, written by `_refresh_closing_snapshot`) carries the day's
  P&L and a telescoping check (sum of day P&L must equal the equity difference to the dollar).
- Each trial window has a **pre-registered contract** (`runs/pre-final-test-run-N/EVAL_CONTRACT.md`):
  counted rules (sample floor, expectancy at 95% one-sided, alpha, beta band, drawdown, up/down capture,
  decision-sell validity), validity conditions (feeds health, freeze, measurement integrity, single
  instance), a pinned verdict command (`scripts/eval_contract_check.py`), and an amendment rule that
  forces mid-window changes to be disclosed with interim readings. The configuration is fingerprinted
  (git tree hash of the package + the strategy keys) so only same-fingerprint windows pool.
- Sample-size arithmetic is written down: at the observed per-trip dispersion a 95% test needs roughly
  155–275 closed trips, so a single 21-session window cannot prove profitability and the roadmap says so.
- A kill criterion is stated in advance for the entry edge.

## 10. Operations

**Question.** What keeps a long-running process honest on a laptop?

**Decision.** One instance (a file lock on `state/bot.lock`); a preflight that exercises every
dependency for real (`preflight.py`); a network-free exchange calendar (`session_calendar.py`) so
paging windows do not depend on an API; dark-gap detection after sleep; battery and clamshell paging
during market hours; an external dead-man (`ops/deadman.py`) that restarts a dead bot and pages when it
cannot; a local control panel (`ops/control_panel.py`) for restart/kill; nightly state backups
(`ops/backup_state.sh`); dated log archives; a headless fallback session for unattended days
(`ops/claude_daily.sh`, `ops/away_mode.md`); one-command readiness checks (`scripts/run7_morning_check.py`);
switch scripts that refuse to run in an unsafe state (`scripts/run7_switch.sh`, `scripts/fresh_cycle.py`).
Every operational incident is written up in the engineering journal (`Todo-*.txt`) with the fix.

## 11. Cost

Decision cadence is set so consecutive model calls hit the prompt cache; a usage ledger (`usage.py`)
records every call with cache statistics and cost. Run-7 runs at about $1.50 per session (9 calls).

## 12. Security

Secrets (`.env`) and account state (`state/`) have never been tracked; the full history is scanned with
gitleaks. Broker context from a second brokerage is read-only and enforced in code, not by convention.
Proposals are confined to the cycle's slate. A kill switch blocks new orders without stopping exits.

## 13. Testing and change discipline

1,535 tests. Every change ships with a failing test first; knobs default off; strategy-affecting items are
classified before a window opens so pooling stays honest; the live tree is frozen during a window and
work happens in isolated worktrees; an independent adversarial review pass precedes a strategy
change-set (it caught a core-fill / hedge-trim sawtooth that unit tests had missed).

## 14. What is not proven

The system side is mature; the entry edge is not. Through seven windows the closed-trip distribution has
been carried by a few large winners; conviction and composite scores have not ranked outcomes. The
roadmap (`runs/pre-final-test-run-7/A_PLUS_ROADMAP.md`) and the run-8 change-set treat that as research
with kill criteria, not as a patch. Saying this plainly is part of the design.

## 15. Where an interviewer's questions land

| Question | Answer lives in |
|---|---|
| How do you stop the model from doing something dangerous? | §1, `risk.py`, `_filter_to_slate` |
| What if the model or the API is down mid-day? | §2, §4 — the watchdog never depends on it |
| Show me the limits. Who can change them? | §3, `env-variable-reference.md`, fingerprint in §9 |
| What broker edge cases did you hit? | §5 and the dated journal entries |
| How do you keep beta near the benchmark? | §6, `portfolio/beta.py` |
| How do you know it has an edge? | §9 — contracts, checker, sample-size math, §14 |
| How do you prevent overfitting to last week? | §8 — report-only loops, freeze windows |
| What happens when the laptop sleeps? | §10 — dark-gap detection, dead-man, paging |
| What did it cost to run? | §11 |
| What would you build next? | run-8 change-set, A+ roadmap |
