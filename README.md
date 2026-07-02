# Investment Strategy — Claude-driven trading bot

**Claude picks the trades, Alpaca places them, and a safety layer guards your money.**

> ⚠️ It ships in **paper mode** (fake money). It can lose real money if you switch
> it to live. Not financial advice — you own every order it makes.

---

## Run it in 5 steps

Open a terminal in this folder and do these in order.

**Step 1 — Install (only the first time)**
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**Step 2 — Add your keys**
Open the `.env` file and paste in your **Alpaca paper** keys and your **Anthropic**
key. (If `.env` already has keys, skip this.) Get them from
[app.alpaca.markets](https://app.alpaca.markets) and
[console.anthropic.com](https://console.anthropic.com).

**Step 3 — Check it's ready**
```bash
python -m investment_strategy.preflight
```
This actually logs into Alpaca with your keys and tells you, in plain English,
whether you're good to go (`✅ Ready to run`) or what to fix. Do this before Step 4.

**Step 4 — Start the bot (fake money)**
```bash
python -m investment_strategy
```
Leave this running. It watches the market, asks Claude what to buy/sell, and places
paper trades — printing what it does. Press **Ctrl-C** to stop.

**Step 5 — See what it did**
Open a *second* terminal (keep the bot running in the first) and run:
```bash
python -m investment_strategy.dashboard --open
```
A page opens in your browser showing every trade, why it was made, your live
account value, and profit/loss. Re-run it any time to refresh.

That's it. 🎉

---

## Handy controls

| I want to… | Do this |
|---|---|
| **Stop the bot** | Press `Ctrl-C` in its terminal |
| **Pause new buys instantly** (keep protecting open trades) | `touch state/KILL` — delete the file to resume |
| **Test the strategy on past data first** | `python -m investment_strategy.backtest` |
| **See if paid data is worth buying** (later) | `python -m investment_strategy.subscriptions` |
| **Auto-refresh the dashboard while it runs** | it's already on (`DASHBOARD_FILE=dashboard.html` in `.env`) — just refresh the page |

---

## Made a new Alpaca account, or switching to live?

Your dashboard and safety memory (trades, peak equity, stops) belong to **one
account**. When you recreate the account or switch paper → live, start clean:

```bash
python -m investment_strategy.reset     # archives a backup, then resets to default
```

You usually don't even need to: on startup the bot **notices the account changed
and resets itself automatically**. Either way your old data isn't lost — it's
archived under `state/archive/`. After a reset, run `preflight` (Step 3) and go.

---

## What it does (the short version)

```
FIND names ─▶ READ signals ─▶ CLAUDE decides ─▶ ALPACA buys ─▶ WATCHDOG protects
scanner       market data      + risk caps       brackets       stops / exits 24/5
```

- **Finds** promising stocks from smart-money activity (no hand-typed list needed).
- **Reads** data on each (fundamentals, price trends, news, insider/congress trades).
- **Claude decides** buy/sell/hold — then a **risk layer it can't override** caps the size.
- **Alpaca places** the order with a built-in stop-loss and take-profit.
- A **watchdog** runs every 30s to cut losses, lock in gains, and flatten in an emergency.

You get emailed if something goes wrong (a failed exit or a big drawdown) — that's
already set up and tested.

---

## Before you ever use real money

1. Run it in **paper mode for weeks** and read what it does.
2. **Backtest** it: `python -m investment_strategy.backtest` (tests sizing + exits on history).
3. Start live with **$100–$1000, not your savings** — scale up only from a real track record.

To go live (deliberate, 3 changes in `.env`): `TRADING_MODE=live` **and**
`ALPACA_BASE_URL=https://api.alpaca.markets` **and** `KILL_SWITCH=off`, with your
**live** Alpaca keys. The bot refuses to start if these disagree, so you can't flip
it by accident.

---

## Safety (the important bits)

- **Paper by default.** Fake money against the real API.
- **Hard limits live in `.env`, enforced in code** — Claude cannot exceed them
  (max position size, daily-loss halt, equity floor, no-leverage cap, and more).
- **Kill switch.** `touch state/KILL` (or `KILL_SWITCH=on`) blocks new buys; the
  watchdog can still close positions — reducing risk is never blocked.
- **Alerts.** You get an email (already configured & tested) if a position is left
  unprotected or the account hits its floor.

---

## Want the deep dive?

The mechanics of each stage, every config knob, options trading, and the multi-user
roadmap live in [Todo-2.txt](Todo-2.txt) and [completed.txt](completed.txt), and the
code is small and commented — start at [orchestrator.py](investment_strategy/orchestrator.py)
(the loop) and [risk.py](investment_strategy/risk.py) (the safety core).
