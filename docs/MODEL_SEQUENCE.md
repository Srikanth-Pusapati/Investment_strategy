# Model flow & timelines

Two independent loops run concurrently from one process (`orchestrator.run`):

| Loop | Cadence | Thread | Gated by kill switch / market-open? | Job |
|------|---------|--------|-------------------------------------|-----|
| **Decision cycle** | `DECISION_INTERVAL_SECONDS` (default **900s = 15m**; production runs **3600s = 60m** per the 2026-07 API-cost decision) | main | YES — only runs when market is open; new buys gated by kill switch | Discover → judge → size → place / thesis-exit |
| **Watchdog** | `MONITOR_INTERVAL_SECONDS` (default **30s**) | daemon | NO — closing is *never* gated | Hard stop / take-profit / trailing / emergency flatten / equity floor |

The watchdog is the fast safety net; the decision cycle is the slow brain. The
watchdog can sell at any time even while the decision thread is blocked on the LLM.

```mermaid
sequenceDiagram
    autonumber
    participant Clock as Timers
    participant Orch as Orchestrator (main)
    participant WD as Watchdog (30s thread)
    participant Scan as Screeners
    participant Sig as Signals
    participant Claude as Claude (decision)
    participant Risk as RiskManager (deterministic)
    participant Brk as Alpaca
    participant St as State (peak / halt / exits)

    Note over Orch,St: STARTUP — load persisted state (peak equity, halt latch, fractional exits)

    par Watchdog loop — every ~30s, market open or not
        loop every 30s
            WD->>Brk: get_account()
            WD->>St: update_equity() (ratchet peak)
            alt equity <= floor (% of peak)
                WD->>Brk: flatten ALL + cancel orders
                WD->>St: LATCH halt (no auto-resume)
            else day loss >= MAX_DAILY_LOSS_PCT
                WD->>Brk: emergency flatten ALL
            else per position
                WD->>WD: hard stop / take-profit (fractional only)
                WD->>WD: ratcheting trailing stop
                WD-->>Brk: close_position() if breached
            end
        end
    and Decision cycle — every ~15m, only when market open
        loop every 15m
            Clock->>Orch: decision due?
            Orch->>Brk: is_market_open()? (skip if closed)
            Orch->>Orch: reconcile last cycle's fills
            Orch->>St: record equity snapshot + read regime multiplier
            Orch->>Scan: scan() -> NEW candidate tickers
            Orch->>Sig: gather(watchlist? + HELD + discovered)
            Sig-->>Orch: per-symbol signal bundles
            Orch->>Claude: decide(bundles, account, benchmark, track-record)
            Claude-->>Orch: proposals (buy / sell / hold) — a REQUEST, not an order
            loop each proposal
                Orch->>Brk: latest price + annualized vol + pending buys
                Orch->>Risk: evaluate(proposal, account, price, vol, sector, regime)
                alt REJECTED
                    Risk-->>Orch: veto (reason logged)
                else APPROVED / RESIZED
                    Risk-->>Orch: approved qty / notional (<= every hard cap)
                    alt SELL
                        Orch->>Brk: cancel orders + close_position
                    else BUY whole-share
                        Orch->>Brk: bracket order (stop+take rest at EXCHANGE)
                    else BUY fractional (small live balance)
                        Orch->>Brk: notional order (NO bracket)
                        Orch->>St: register watchdog stop/take (the only guard)
                    end
                end
            end
        end
    end
```

## How a HELD position is decided keep-vs-sell (note 3)

A bought position is re-judged on **two clocks at once**:

1. **Every 30s (watchdog, price-driven, deterministic):** hard stop, take-profit,
   ratcheting trailing stop, account-wide daily-loss flatten, and the latched
   equity floor. This is what caps loss intraday — no LLM, no waiting.
2. **Every ~15m (decision cycle, thesis-driven, market open only):** every held
   symbol is re-fed through the full signal stack to Claude alongside fresh
   candidates, and Claude can return `sell` if the thesis broke. That sell still
   passes through RiskManager (always allowed — risk reduction) before it executes.

So "is this still good to keep?" is answered continuously by price (30s) and every
15 minutes by thesis. **Gap today (Phase 1B.4):** there is no max-hold age and no
explicit stale-thesis exit — a name that quietly stops working but never hits a
price stop and that Claude never flags is held indefinitely. That deterministic
time-stop / thesis-decay exit is the next item to add.
