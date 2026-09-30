# Tax & wash-sale note (Todo-3 L.2 / goGA GA-3.1)

Status: WRITTEN 2026-07-14. This is the operator's tax note for a high-churn
bot, NOT tax advice. One rule dominates everything below: **the broker's
1099-B is the source of truth for filing.** Everything the ledger computes is
a heads-up, not a filing number.

## Why this note exists

The bot is deliberately high-churn: 8%/20% (or vol-scaled) stops and takes,
a 30-second watchdog, time stops at MAX_HOLD_DAYS=30, and a 24-hour re-entry
cooldown. That churn has two tax consequences a buy-and-hold investor never
meets:

1. **Every gain is short-term.** Positions held under a year are taxed as
   ordinary income (federal bracket + state), not the long-term capital-gains
   rate. Mentally discount the dashboard's realized P&L by your marginal rate
   when judging live performance — a +10% realized year is materially less
   after tax, while QQQ held passively defers nearly all of it.

2. **The stop-out-then-rebuy pattern is a wash-sale machine.** A loss sale
   with a replacement buy of the same (or "substantially identical") security
   within ±30 **calendar days** disallows the loss for that filing year and
   rolls it into the replacement lot's basis. The bot's REENTRY_COOLDOWN_HOURS
   is 24 — that guard exists to stop churn, and it does nothing for wash
   sales; the IRS window is 30 days, not 24 hours. Expect a large fraction of
   the bot's realized losses to be wash-flagged in any active month.

## What the ledger does (and does not do)

- `lots.py` (GA-2.5) tracks FIFO lots and flags a realized **loss** with
  another BUY of the same symbol within ±30 calendar days as
  `wash_sale=True`. The track record renders it as "wash-sale?".
- The flag is a **heads-up only**: the ledger does not compute the disallowed
  amount, does not adjust the replacement lot's basis, and cannot see other
  accounts (an IRA rebuy of the same name also washes the loss — only you
  know your other accounts).
- The broker (Alpaca) reports adjusted basis and disallowed wash-sale amounts
  on the **1099-B**. File from the 1099-B. Use the ledger flag only to
  anticipate that "realized loss" on the dashboard ≠ deductible loss.

## Practical rules for this project

- **Paper trading creates no tax events.** Nothing below matters until the
  live float (planned $100–1000, fractional) is funded.
- **Options**: single-leg equity options (the only kind the bot trades) are
  ordinary short-term gains/losses like the stock legs; they are NOT §1256
  contracts (that's broad-based index options/futures). Options on a name can
  also wash a stock loss in the same name and vice versa — again, 1099-B
  decides.
- **December**: wash-sale disallowance bites hardest across the year
  boundary (loss in late December, rebuy in January = loss deferred a full
  filing year). If the live float is running in December, consider letting
  the re-entry cooldown effectively be "next year" for names sold at a loss
  in the last week — an operator decision, not a bot change.
- **Record keeping**: state/trades.jsonl + the equity history are the audit
  trail; keep the nightly off-machine backup (GA-2.8) running once live.
  If the 1099-B ever disagrees with the ledger, the ledger is the one that's
  wrong.
- **Posture** (L.1): single user, own money, own keys — no adviser/BD
  registration required. Revisit before ANY multi-user step (Phase G).
