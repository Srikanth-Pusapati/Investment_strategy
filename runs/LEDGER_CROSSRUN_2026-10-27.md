# Cross-run ledger analysis (runs 4–7) — generated 2026-10-27, read-only

## (a) Schema notes

- All four ledgers are **per-fill** rows (`action` = buy|sell), one JSON object per line. A **trip** = one `sell` row with non-null `realized_pl` (the project's own checker, `scripts/eval_contract_check.py`, uses the same definition). Sell rows carry `realized_pl`, `realized_pl_pct`, `exit_reason`, `exit_price`, `qty`, `rationale`; they carry **no** entry attributes (`entry_price`=0, `conviction`=0) in runs 4–6, so each sell was FIFO-matched to the open buy lot(s) of the same `(symbol, instrument)` to recover conviction / composite / signals / entry time / lot count.
- Common keys (all runs): ts, symbol, action, instrument (equity|option), qty, entry_price, cost_usd, conviction, take_profit_pct, stop_loss_pct, take_profit_price, stop_loss_price, exit_price, rationale, key_signals (cited, free text), entry_signals (families fed), realized_pl_pct, realized_pl, verdict, risk_note, exit_reason, option_strategy, order_id, composite_score.
- Drift: run-4 has `underlying`/`occ_symbols`/`repair_note` on only 2 rows and keys options by **underlying** symbol (MSFT, AMZN, PLTR, HL; no OCC symbols on buys); run-5 adds `underlying`, `occ_symbols`, `repair_note` on every row and keys single-leg options by OCC symbol, spreads by underlying; run-6 adds `occ_sides`, `fill_price`, `fill_qty`, `fill_ts`; run-7 adds `sell_events`, `quote_exit_price`, `quote_realized_pl(_pct)`, `spy_intraday_ret_at_decision`, `regime_label`, `falling_names`, `would_haircut_usd`, `stop_pct_if_floor_6`, `vol_stop_raw_pct`, `floor6_*`, and on **sell rows** `entry_ts`, `entry_fill_price`, `entry_fill_source`, `entry_composite`, `entry_conviction`, `entry_stop_pct`, `entry_key_signals`, `lots_n`, `quote_cost_usd`.
- `entry_signals` lists the signal **families available/fed** at decision (news/technical/fundamentals appear on ~95% of buys, so it is weakly discriminating); `key_signals` is the model's **cited** list — used here for 'primary/first-cited' (first element, keyword-mapped to a family) and 'cited-any'.
- System core/hedge fills (QQQ, PSQ, SGOV) are buy rows with conviction 0.0, composite null, `entry_signals` = [core_fill|auto_hedge|defensive_fill].
- Equity history: one row per session {date, ts, equity, cash, unrealized_pl, realized_pl, total_return, day_pl}; run-6+ add `basis`, `book_beta_spy`; run-7 adds `broker_day_pl`, `day_pl_basis`.
- Actual ledger date spans: run4: 2026-07-28 → 2026-08-21 (111 rows = 67 buys + 44 sells); run5: 2026-08-24 → 2026-08-31 (41 rows = 30 buys + 11 sells); run6: 2026-09-01 → 2026-09-21 (93 rows = 61 buys + 32 sells); run7: 2026-09-22 → 2026-10-26 (96 rows = 62 buys + 34 sells). Run-4's ledger starts Jul 28 (the Jul 27 fresh-cycle reset), not Jul 7. run-6 `state/` (52 rows, to Sep 11) is a strict prefix of `state-post-window/` (93 rows, to Sep 21); the latter was used.
- Exclusions: run-4 2 rows `exit_reason=external` with realized_pl 0.0 (Aug 17 AMZN option legs that vanished broker-side; the 2-contract AMZN call shows exit_price 22.0 vs entry 46.75 but pl recorded 0.0 — unrecorded loss). run-7: **0** rows dated 2026-10-27 and **0** rows with exit_reason 'flatten' — the Oct 27 flatten is not in this ledger. run4: sells unmatched to any buy=0, negative-qty sells=0; run5: sells unmatched to any buy=0, negative-qty sells=0; run6: sells unmatched to any buy=0, negative-qty sells=0; run7: sells unmatched to any buy=0, negative-qty sells=0.

Example rows (one per file, truncated):

- run4 sell: `{"ts": "2026-07-29T14:48:32.248491Z", "symbol": "NU", "action": "sell", "instrument": "equity", "qty": 5439.0, "entry_price": 0.0, "cost_usd": 0.0, "conviction": 0.0, "take_profit_pct": 0.0, "stop_loss_pct": 0.0, "take_profit_price": null, "stop_loss_price": null, "exit_price": 14.17, "rationale": "exchange-side exit backfill (stop sell)", "key_signals": [], "entry_signals": [], "realized_pl_pct": -4.094754653130295,…`
- run5 sell: `{"ts": "2026-08-26T14:25:39.209798Z", "symbol": "SNAP", "action": "sell", "instrument": "equity", "qty": 4549.0, "entry_price": 0.0, "cost_usd": 0.0, "conviction": 0.0, "take_profit_pct": 0.0, "stop_loss_pct": 0.0, "take_profit_price": null, "stop_loss_price": null, "exit_price": 5.52, "rationale": "watchdog decision", "key_signals": [], "entry_signals": [], "realized_pl_pct": 0.5459999999999999, "realized_pl": 136.4…`
- run6 sell: `{"ts": "2026-09-04T14:26:01.410562Z", "symbol": "MKL", "action": "sell", "instrument": "equity", "qty": 9.0, "entry_price": 0.0, "cost_usd": 0.0, "conviction": 0.0, "take_profit_pct": 0.0, "stop_loss_pct": 0.0, "take_profit_price": null, "stop_loss_price": null, "exit_price": 1836.39, "rationale": "watchdog decision", "key_signals": [], "entry_signals": [], "realized_pl_pct": 0.6629999999999999, "realized_pl": 108.9,…`
- run7 sell: `{"ts": "2026-09-22T16:09:25.865171Z", "symbol": "CRML", "action": "sell", "instrument": "equity", "qty": 1421.0, "entry_price": 0.0, "cost_usd": 0.0, "conviction": 0.0, "take_profit_pct": 0.0, "stop_loss_pct": 0.0, "take_profit_price": null, "stop_loss_price": null, "exit_price": 8.62, "rationale": "watchdog decision", "key_signals": [], "entry_signals": [], "realized_pl_pct": -1.260023, "realized_pl": -156.31, "verd…`
- run7 buy: `{"ts": "2026-09-22T13:33:33.998418Z", "symbol": "NVDA", "action": "buy", "instrument": "equity", "qty": 349.0, "entry_price": 229.11, "cost_usd": 79980.33, "conviction": 0.68, "take_profit_pct": 12.573794212240323, "stop_loss_pct": 5.029517684896129, "take_profit_price": 257.92, "stop_loss_price": 217.59, "exit_price": null, "rationale": "Best-corroborated name: fundamentals +0.80 (77% margin, +65.5% rev growth), tec…`
- run4 option buy: `{"ts": "2026-07-30T14:25:35.261223Z", "symbol": "MSFT", "action": "buy", "instrument": "option", "qty": 17.0, "entry_price": 5.59, "cost_usd": 9503.0, "conviction": 0.66, "take_profit_pct": 0.0, "stop_loss_pct": 0.0, "take_profit_price": null, "stop_loss_price": null, "exit_price": null, "rationale"…`

## (b) Tables

Cell format: N / win rate / mean P&L per trip / total P&L. Pooled = runs 5+6+7. Run-4 is kept separate (several change-sets inside it).

### 1. Headline — all trips, satellite (model) trips, system trips

| scope | run | N | WR | mean | median | total | sd | 95% one-sided LB of mean |
|---|---|---|---|---|---|---|---|---|
| all | run4 | 42 | 31% | $167 | −$720 | $7,031 | $5,241 | −$1,194 |
| all | run5 | 11 | 55% | −$437 | $136 | −$4,802 | $1,895 | −$1,472 |
| all | run6 | 32 | 41% | $833 | −$385 | $26,653 | $3,976 | −$359 |
| all | run7 | 34 | 21% | −$870 | −$819 | −$29,580 | $2,345 | −$1,551 |
| all | pool5-7 | 77 | 34% | −$100 | −$665 | −$7,729 | $3,155 | −$699 |
| all | run4 Aug3–14 window | 20 | 40% | $1,455 | −$204 | $29,103 | $5,551 | −$691 |
| satellite (exit_reason ∉ system set) | run4 | 42 | 31% | $167 | −$720 | $7,031 | $5,241 | −$1,194 |
| satellite (exit_reason ∉ system set) | run5 | 8 | 50% | −$924 | −$524 | −$7,394 | $1,975 | −$2,247 |
| satellite (exit_reason ∉ system set) | run6 | 27 | 33% | $940 | −$773 | $25,373 | $4,326 | −$480 |
| satellite (exit_reason ∉ system set) | run7 | 33 | 21% | −$842 | −$732 | −$27,799 | $2,376 | −$1,543 |
| satellite (exit_reason ∉ system set) | pool5-7 | 68 | 29% | −$144 | −$752 | −$9,821 | $3,339 | −$820 |
| satellite (exit_reason ∉ system set) | run4 Aug3–14 window | 20 | 40% | $1,455 | −$204 | $29,103 | $5,551 | −$691 |
| system exits | run4 | 0 | — | — | — | — | — | — |
| system exits | run5 | 3 | 67% | $864 | $797 | $2,592 | $903 | −$659 |
| system exits | run6 | 5 | 80% | $256 | $28 | $1,280 | $605 | −$321 |
| system exits | run7 | 1 | 0% | −$1,780 | −$1,780 | −$1,780 | — | — |
| system exits | pool5-7 | 9 | 67% | $232 | $28 | $2,092 | $1,022 | −$401 |
| system exits | run4 Aug3–14 window | 0 | — | — | — | — | — | — |

Satellite with the project checker's extra symbol filter (QQQ/PSQ rows dropped regardless of exit_reason):

| run | N | WR | mean | total | 95% LB |
|---|---|---|---|---|---|
| run4 | 42 | 31% | $167 | $7,031 | −$1,194 |
| run5 | 8 | 50% | −$924 | −$7,394 | −$2,247 |
| run6 | 27 | 33% | $940 | $25,373 | −$480 |
| run7 | 33 | 21% | −$842 | −$27,799 | −$1,543 |
| pool5-7 | 68 | 29% | −$144 | −$9,821 | −$820 |

### 3. Concentration of satellite P&L (top-3 winners; ex-top-3 mean = kill-criterion input)

| run | N sat | net total sat | top-3 trips | top-3 sum | top-3 ÷ net total | top-3 share of gross wins | mean ex-top-3 | WR ex-top-3 | 95% LB ex-top-3 |
|---|---|---|---|---|---|---|---|---|---|
| run4 | 42 | $7,031 | PATH $13,807 (bracket_take), MSFT $11,645 (take), SMCI $9,437 (trail) | $34,889 | 4.96× | 44% of $79,084 | −$714 | 26% | −$1,867 |
| run5 | 8 | −$7,394 | SNDK $1,251 (decision), SOFI $453 (decision), META $205 (decision) | $1,909 | net total ≤ 0 | 93% of $2,046 | −$1,861 | 20% | −$3,709 |
| run6 | 27 | $25,373 | INTC $12,816 (bracket_take), BE $12,525 (trail), INTC $8,930 (bracket_take) | $34,272 | 1.35× | 70% of $49,017 | −$371 | 25% | −$1,122 |
| run7 | 33 | −$27,799 | IWM $4,872 (take), HYG261030P00078500 $4,450 (take), ONON $3,424 (trail) | $12,746 | net total ≤ 0 | 75% of $16,905 | −$1,352 | 13% | −$1,910 |
| pool5-7 | 68 | −$9,821 | INTC $12,816 (bracket_take), BE $12,525 (trail), INTC $8,930 (bracket_take) | $34,272 | net total ≤ 0 | 50% of $67,967 | −$678 | 26% | −$1,140 |
| run4 Aug3–14 window | 20 | $29,103 | PATH $13,807 (bracket_take), MSFT $11,645 (take), SMCI $9,437 (trail) | $34,889 | 1.20× | 66% of $53,139 | −$340 | 29% | −$1,877 |

Same, satellite with QQQ/PSQ symbol filter (checker definition): run4: N=42, ex-top-3 mean=−$714; run5: N=8, ex-top-3 mean=−$1,861; run6: N=27, ex-top-3 mean=−$371; run7: N=33, ex-top-3 mean=−$1,352; pool5-7: N=68, ex-top-3 mean=−$678

Mirror — bottom-3 losers share of satellite losses: run4: bottom-3 −$30,144 = 42% of all losses −$72,053; run5: bottom-3 −$8,255 = 87% of all losses −$9,440; run6: bottom-3 −$9,009 = 38% of all losses −$23,644; run7: bottom-3 −$15,305 = 34% of all losses −$44,704; pool5-7: bottom-3 −$17,624 = 23% of all losses −$77,788

### 4. By exit reason


**exit_reason (system set marked †)**

| bucket | run4 | run5 | run6 | run7 | pool5-7 |
|---|---|---|---|---|---|
| bracket_stop | N=4 WR=0% μ=−$2,499 Σ=−$9,997 | N=2 WR=0% μ=−$3,467 Σ=−$6,935 | N=15 WR=0% μ=−$1,338 Σ=−$20,077 | N=8 WR=0% μ=−$2,117 Σ=−$16,935 | N=25 WR=0% μ=−$1,758 Σ=−$43,946 |
| bracket_take | N=5 WR=100% μ=$7,039 Σ=$35,197 | — | N=3 WR=100% μ=$8,976 Σ=$26,929 | N=2 WR=100% μ=$1,134 Σ=$2,269 | N=5 WR=100% μ=$5,839 Σ=$29,197 |
| decision | N=23 WR=9% μ=−$1,074 Σ=−$24,695 | N=6 WR=67% μ=−$77 Σ=−$460 | N=5 WR=40% μ=−$622 Σ=−$3,110 | N=14 WR=7% μ=−$1,247 Σ=−$17,459 | N=25 WR=28% μ=−$841 Σ=−$21,028 |
| stop | N=4 WR=0% μ=−$8,448 Σ=−$33,794 | — | — | N=4 WR=0% μ=−$2,460 Σ=−$9,841 | N=4 WR=0% μ=−$2,460 Σ=−$9,841 |
| take | N=1 WR=100% μ=$11,645 Σ=$11,645 | — | N=1 WR=100% μ=$5,135 Σ=$5,135 | N=3 WR=100% μ=$3,724 Σ=$11,172 | N=4 WR=100% μ=$4,077 Σ=$16,307 |
| time | — | — | — | N=1 WR=0% μ=−$429 Σ=−$429 | N=1 WR=0% μ=−$429 Σ=−$429 |
| trail | N=5 WR=100% μ=$5,735 Σ=$28,674 | — | N=3 WR=100% μ=$5,498 Σ=$16,495 | N=1 WR=100% μ=$3,424 Σ=$3,424 | N=4 WR=100% μ=$4,980 Σ=$19,920 |
| core_defense † | — | N=1 WR=100% μ=$797 Σ=$797 | N=2 WR=50% μ=−$51 Σ=−$101 | — | N=3 WR=67% μ=$232 Σ=$696 |
| defensive_rotate † | — | N=1 WR=0% μ=−$4 Σ=−$4 | N=1 WR=100% μ=$8 Σ=$8 | — | N=2 WR=50% μ=$2 Σ=$4 |
| hedge_unwind † | — | N=1 WR=100% μ=$1,799 Σ=$1,799 | N=2 WR=100% μ=$687 Σ=$1,374 | N=1 WR=0% μ=−$1,780 Σ=−$1,780 | N=4 WR=75% μ=$348 Σ=$1,393 |

**'decision' exits split by rationale source**

| bucket | run4 | run5 | run6 | run7 | pool5-7 |
|---|---|---|---|---|---|
| watchdog decision (rationale starts 'watchdog decision') | N=20 WR=10% μ=−$969 Σ=−$19,374 | N=6 WR=67% μ=−$77 Σ=−$460 | N=4 WR=50% μ=−$762 Σ=−$3,047 | N=12 WR=8% μ=−$778 Σ=−$9,331 | N=22 WR=32% μ=−$584 Σ=−$12,838 |
| model/other decision text | N=3 WR=0% μ=−$1,774 Σ=−$5,321 | — | N=1 WR=0% μ=−$62 Σ=−$62 | N=2 WR=0% μ=−$4,064 Σ=−$8,128 | N=3 WR=0% μ=−$2,730 Σ=−$8,191 |
| exchange-side backfill rows (any reason) | N=9 WR=56% μ=$2,800 Σ=$25,200 | N=2 WR=0% μ=−$3,467 Σ=−$6,935 | N=18 WR=17% μ=$381 Σ=$6,852 | N=10 WR=20% μ=−$1,467 Σ=−$14,666 | N=30 WR=17% μ=−$492 Σ=−$14,749 |

**Exit mechanism groups**

| bucket | run4 | run5 | run6 | run7 | pool5-7 |
|---|---|---|---|---|---|
| stops (bracket_stop+stop) | N=8 WR=0% μ=−$5,474 Σ=−$43,791 | N=2 WR=0% μ=−$3,467 Σ=−$6,935 | N=15 WR=0% μ=−$1,338 Σ=−$20,077 | N=12 WR=0% μ=−$2,231 Σ=−$26,776 | N=29 WR=0% μ=−$1,855 Σ=−$53,787 |
| takes (bracket_take+take) | N=6 WR=100% μ=$7,807 Σ=$46,842 | — | N=4 WR=100% μ=$8,016 Σ=$32,064 | N=5 WR=100% μ=$2,688 Σ=$13,441 | N=9 WR=100% μ=$5,056 Σ=$45,504 |
| trail | N=5 WR=100% μ=$5,735 Σ=$28,674 | — | N=3 WR=100% μ=$5,498 Σ=$16,495 | N=1 WR=100% μ=$3,424 Σ=$3,424 | N=4 WR=100% μ=$4,980 Σ=$19,920 |
| decision | N=23 WR=9% μ=−$1,074 Σ=−$24,695 | N=6 WR=67% μ=−$77 Σ=−$460 | N=5 WR=40% μ=−$622 Σ=−$3,110 | N=14 WR=7% μ=−$1,247 Σ=−$17,459 | N=25 WR=28% μ=−$841 Σ=−$21,028 |
| time | — | — | — | N=1 WR=0% μ=−$429 Σ=−$429 | N=1 WR=0% μ=−$429 Σ=−$429 |
| system † | — | N=3 WR=67% μ=$864 Σ=$2,592 | N=5 WR=80% μ=$256 Σ=$1,280 | N=1 WR=0% μ=−$1,780 Σ=−$1,780 | N=9 WR=67% μ=$232 Σ=$2,092 |

### 5. Equity vs options; bullish vs bearish


**Asset class (all trips)**

| bucket | run4 | run5 | run6 | run7 | pool5-7 |
|---|---|---|---|---|---|
| equity | N=37 WR=32% μ=$789 Σ=$29,180 | N=11 WR=55% μ=−$437 Σ=−$4,802 | N=31 WR=39% μ=$694 Σ=$21,518 | N=27 WR=15% μ=−$1,145 Σ=−$30,911 | N=69 WR=32% μ=−$206 Σ=−$14,195 |
| option (any) | N=5 WR=20% μ=−$4,430 Σ=−$22,149 | — | N=1 WR=100% μ=$5,135 Σ=$5,135 | N=7 WR=43% μ=$190 Σ=$1,331 | N=8 WR=50% μ=$808 Σ=$6,466 |
| option: long_call | N=2 WR=50% μ=−$1,656 Σ=−$3,311 | — | — | — | — |
| option: bull_call_spread | N=3 WR=0% μ=−$6,279 Σ=−$18,838 | — | — | — | — |
| option: long_put | — | — | — | N=5 WR=40% μ=−$214 Σ=−$1,071 | N=5 WR=40% μ=−$214 Σ=−$1,071 |
| option: bear_put_spread | — | — | N=1 WR=100% μ=$5,135 Σ=$5,135 | N=2 WR=50% μ=$1,201 Σ=$2,402 | N=3 WR=67% μ=$2,512 Σ=$7,537 |
| option: strategy unknown (unmatched buy) | — | — | — | — | — |

**Direction (all trips)**

| bucket | run4 | run5 | run6 | run7 | pool5-7 |
|---|---|---|---|---|---|
| bullish (equity long + calls) | N=42 WR=31% μ=$167 Σ=$7,031 | N=10 WR=50% μ=−$660 Σ=−$6,601 | N=29 WR=34% μ=$695 Σ=$20,144 | N=26 WR=15% μ=−$1,120 Σ=−$29,130 | N=65 WR=29% μ=−$240 Σ=−$15,588 |
| bearish (puts + inverse-ETF hedge PSQ) | — | N=1 WR=100% μ=$1,799 Σ=$1,799 | N=3 WR=100% μ=$2,170 Σ=$6,509 | N=8 WR=38% μ=−$56 Σ=−$449 | N=12 WR=58% μ=$655 Σ=$7,859 |
| bearish, satellite only (puts) | — | — | N=1 WR=100% μ=$5,135 Σ=$5,135 | N=7 WR=43% μ=$190 Σ=$1,331 | N=8 WR=50% μ=$808 Σ=$6,466 |
| bullish, satellite only | N=42 WR=31% μ=$167 Σ=$7,031 | N=8 WR=50% μ=−$924 Σ=−$7,394 | N=26 WR=31% μ=$778 Σ=$20,238 | N=26 WR=15% μ=−$1,120 Σ=−$29,130 | N=60 WR=27% μ=−$271 Σ=−$16,287 |
| bullish equity, satellite only | N=37 WR=32% μ=$789 Σ=$29,180 | N=8 WR=50% μ=−$924 Σ=−$7,394 | N=26 WR=31% μ=$778 Σ=$20,238 | N=26 WR=15% μ=−$1,120 Σ=−$29,130 | N=60 WR=27% μ=−$271 Σ=−$16,287 |

### 6. By signal family (satellite trips only; first-lot attributes)


**Primary = family of the FIRST cited key_signal (keyword-mapped)**

| bucket | run4 | run5 | run6 | run7 | pool5-7 |
|---|---|---|---|---|---|
| congress | — | — | — | — | — |
| insider | N=7 WR=0% μ=−$1,447 Σ=−$10,131 | — | N=6 WR=33% μ=$1,141 Σ=$6,846 | N=6 WR=33% μ=−$30 Σ=−$182 | N=12 WR=33% μ=$555 Σ=$6,664 |
| lobbying | — | — | — | — | — |
| govcontracts | — | — | — | — | — |
| options_flow | N=11 WR=27% μ=$411 Σ=$4,516 | N=3 WR=67% μ=−$243 Σ=−$730 | N=4 WR=25% μ=−$708 Σ=−$2,832 | N=5 WR=20% μ=−$59 Σ=−$295 | N=12 WR=33% μ=−$321 Σ=−$3,857 |
| options_chain | N=1 WR=0% μ=−$821 Σ=−$821 | — | — | — | — |
| technical | N=8 WR=25% μ=$76 Σ=$610 | — | N=6 WR=33% μ=$1,983 Σ=$11,900 | N=17 WR=18% μ=−$1,138 Σ=−$19,345 | N=23 WR=22% μ=−$324 Σ=−$7,445 |
| fundamentals | N=8 WR=62% μ=$3,765 Σ=$30,117 | N=1 WR=100% μ=$1,251 Σ=$1,251 | N=8 WR=25% μ=−$951 Σ=−$7,607 | N=3 WR=33% μ=−$1,691 Σ=−$5,073 | N=12 WR=33% μ=−$952 Σ=−$11,429 |
| news | — | N=1 WR=0% μ=−$1,867 Σ=−$1,867 | — | — | N=1 WR=0% μ=−$1,867 Σ=−$1,867 |
| discovery | — | — | — | — | — |
| offexchange | — | — | — | — | — |
| composite | N=7 WR=43% μ=−$2,466 Σ=−$17,260 | N=3 WR=33% μ=−$2,016 Σ=−$6,049 | N=3 WR=67% μ=$5,689 Σ=$17,066 | N=2 WR=0% μ=−$1,453 Σ=−$2,905 | N=8 WR=38% μ=$1,014 Σ=$8,112 |
| other | — | — | — | — | — |
| none cited / no match | — | — | — | — | — |

**Cited-any = family appears anywhere in key_signals**

| bucket | run4 | run5 | run6 | run7 | pool5-7 |
|---|---|---|---|---|---|
| congress | N=3 WR=0% μ=−$474 Σ=−$1,421 | — | — | N=1 WR=0% μ=−$2,080 Σ=−$2,080 | N=1 WR=0% μ=−$2,080 Σ=−$2,080 |
| insider | N=12 WR=0% μ=−$2,765 Σ=−$33,176 | N=2 WR=0% μ=−$1,526 Σ=−$3,052 | N=16 WR=38% μ=$951 Σ=$15,209 | N=18 WR=22% μ=−$1,215 Σ=−$21,873 | N=36 WR=28% μ=−$270 Σ=−$9,716 |
| lobbying | — | — | — | — | — |
| govcontracts | N=4 WR=25% μ=−$1,972 Σ=−$7,888 | — | N=1 WR=100% μ=$349 Σ=$349 | N=1 WR=0% μ=−$654 Σ=−$654 | N=2 WR=50% μ=−$153 Σ=−$306 |
| options_flow | N=30 WR=30% μ=−$186 Σ=−$5,588 | N=5 WR=60% μ=−$1,119 Σ=−$5,593 | N=20 WR=35% μ=$1,097 Σ=$21,937 | N=29 WR=24% μ=−$789 Σ=−$22,881 | N=54 WR=31% μ=−$121 Σ=−$6,536 |
| options_chain | N=14 WR=36% μ=$475 Σ=$6,657 | N=2 WR=100% μ=$295 Σ=$590 | N=14 WR=43% μ=$1,731 Σ=$24,239 | N=8 WR=38% μ=$564 Σ=$4,513 | N=24 WR=46% μ=$1,223 Σ=$29,342 |
| technical | N=37 WR=32% μ=$251 Σ=$9,277 | N=7 WR=43% μ=−$1,086 Σ=−$7,600 | N=19 WR=37% μ=$1,100 Σ=$20,892 | N=28 WR=18% μ=−$1,040 Σ=−$29,122 | N=54 WR=28% μ=−$293 Σ=−$15,830 |
| fundamentals | N=26 WR=38% μ=$558 Σ=$14,520 | N=6 WR=50% μ=−$944 Σ=−$5,664 | N=12 WR=25% μ=$234 Σ=$2,807 | N=9 WR=33% μ=−$509 Σ=−$4,577 | N=27 WR=33% μ=−$275 Σ=−$7,435 |
| news | N=4 WR=75% μ=$3,917 Σ=$15,667 | N=1 WR=0% μ=−$1,867 Σ=−$1,867 | N=3 WR=0% μ=−$1,459 Σ=−$4,376 | N=6 WR=0% μ=−$706 Σ=−$4,238 | N=10 WR=0% μ=−$1,048 Σ=−$10,480 |
| discovery | — | — | — | — | — |
| offexchange | N=3 WR=33% μ=$3,652 Σ=$10,955 | N=2 WR=100% μ=$694 Σ=$1,387 | N=8 WR=25% μ=$220 Σ=$1,762 | — | N=10 WR=40% μ=$315 Σ=$3,150 |
| composite | N=17 WR=29% μ=−$1,265 Σ=−$21,511 | N=4 WR=25% μ=−$1,979 Σ=−$7,915 | N=10 WR=30% μ=$1,766 Σ=$17,661 | N=7 WR=0% μ=−$1,318 Σ=−$9,224 | N=21 WR=19% μ=$25 Σ=$522 |

**Fed-any = family present in entry_signals (available at decision)**

| bucket | run4 | run5 | run6 | run7 | pool5-7 |
|---|---|---|---|---|---|
| congress | N=40 WR=30% μ=$210 Σ=$8,401 | N=7 WR=57% μ=−$868 Σ=−$6,074 | N=15 WR=47% μ=$2,417 Σ=$36,251 | N=8 WR=25% μ=−$2,151 Σ=−$17,212 | N=30 WR=43% μ=$432 Σ=$12,965 |
| insider | N=31 WR=29% μ=−$92 Σ=−$2,850 | N=5 WR=80% μ=−$604 Σ=−$3,022 | N=22 WR=41% μ=$1,378 Σ=$30,322 | N=25 WR=20% μ=−$1,245 Σ=−$31,128 | N=52 WR=35% μ=−$74 Σ=−$3,829 |
| lobbying | N=21 WR=24% μ=−$1,246 Σ=−$26,159 | N=4 WR=100% μ=$511 Σ=$2,046 | N=16 WR=44% μ=$2,251 Σ=$36,013 | N=14 WR=14% μ=−$1,940 Σ=−$27,154 | N=34 WR=38% μ=$321 Σ=$10,905 |
| govcontracts | N=13 WR=31% μ=−$119 Σ=−$1,542 | N=1 WR=100% μ=$136 Σ=$136 | N=6 WR=33% μ=−$590 Σ=−$3,540 | N=2 WR=0% μ=−$780 Σ=−$1,559 | N=9 WR=33% μ=−$551 Σ=−$4,963 |
| options_flow | — | — | N=27 WR=33% μ=$940 Σ=$25,373 | N=31 WR=23% μ=−$844 Σ=−$26,174 | N=58 WR=28% μ=−$14 Σ=−$801 |
| options_chain | N=35 WR=34% μ=$256 Σ=$8,956 | N=6 WR=67% μ=−$724 Σ=−$4,342 | N=25 WR=36% μ=$1,096 Σ=$27,403 | N=28 WR=21% μ=−$870 Σ=−$24,346 | N=59 WR=32% μ=−$22 Σ=−$1,286 |
| technical | N=42 WR=31% μ=$167 Σ=$7,031 | N=8 WR=50% μ=−$924 Σ=−$7,394 | N=27 WR=33% μ=$940 Σ=$25,373 | N=32 WR=22% μ=−$840 Σ=−$26,895 | N=67 WR=30% μ=−$133 Σ=−$8,916 |
| fundamentals | N=41 WR=29% μ=$141 Σ=$5,772 | N=8 WR=50% μ=−$924 Σ=−$7,394 | N=24 WR=38% μ=$1,205 Σ=$28,924 | N=26 WR=19% μ=−$1,241 Σ=−$32,256 | N=58 WR=31% μ=−$185 Σ=−$10,726 |
| news | N=42 WR=31% μ=$167 Σ=$7,031 | N=8 WR=50% μ=−$924 Σ=−$7,394 | N=27 WR=33% μ=$940 Σ=$25,373 | N=33 WR=21% μ=−$842 Σ=−$27,799 | N=68 WR=29% μ=−$144 Σ=−$9,821 |
| discovery | N=40 WR=28% μ=−$142 Σ=−$5,679 | N=8 WR=50% μ=−$924 Σ=−$7,394 | N=25 WR=28% μ=$309 Σ=$7,713 | N=30 WR=20% μ=−$995 Σ=−$29,837 | N=63 WR=27% μ=−$469 Σ=−$29,519 |
| offexchange | N=39 WR=31% μ=$210 Σ=$8,207 | N=8 WR=50% μ=−$924 Σ=−$7,394 | N=24 WR=38% μ=$1,205 Σ=$28,924 | N=25 WR=16% μ=−$1,299 Σ=−$32,468 | N=57 WR=30% μ=−$192 Σ=−$10,939 |
| congress NOT fed | N=2 WR=50% μ=−$685 Σ=−$1,370 | N=1 WR=0% μ=−$1,320 Σ=−$1,320 | N=12 WR=17% μ=−$906 Σ=−$10,878 | N=25 WR=20% μ=−$424 Σ=−$10,588 | N=38 WR=18% μ=−$600 Σ=−$22,786 |
| insider NOT fed | N=11 WR=36% μ=$898 Σ=$9,881 | N=3 WR=0% μ=−$1,457 Σ=−$4,372 | N=5 WR=0% μ=−$990 Σ=−$4,949 | N=8 WR=25% μ=$416 Σ=$3,329 | N=16 WR=12% μ=−$374 Σ=−$5,992 |

### 7. By conviction and composite (satellite trips; first-lot values)


**Conviction bucket (requested)**

| bucket | run4 | run5 | run6 | run7 | pool5-7 |
|---|---|---|---|---|---|
| <0.6 | — | — | — | N=1 WR=100% μ=$4,872 Σ=$4,872 | N=1 WR=100% μ=$4,872 Σ=$4,872 |
| 0.6–0.75 | N=42 WR=31% μ=$167 Σ=$7,031 | N=8 WR=50% μ=−$924 Σ=−$7,394 | N=27 WR=33% μ=$940 Σ=$25,373 | N=32 WR=19% μ=−$1,021 Σ=−$32,671 | N=67 WR=28% μ=−$219 Σ=−$14,693 |
| 0.75–0.9 | — | — | — | — | — |
| ≥0.9 | — | — | — | — | — |
| 0 (system fill) | — | — | — | — | — |
| unknown | — | — | — | — | — |

**Conviction, finer (nearly all mass is 0.60–0.75)**

| bucket | run4 | run5 | run6 | run7 | pool5-7 |
|---|---|---|---|---|---|
| =0.60 exactly | N=14 WR=36% μ=$1,359 Σ=$19,029 | N=3 WR=33% μ=−$994 Σ=−$2,981 | N=14 WR=36% μ=$293 Σ=$4,099 | N=12 WR=17% μ=−$301 Σ=−$3,616 | N=29 WR=28% μ=−$86 Σ=−$2,498 |
| 0.61–0.63 | N=20 WR=15% μ=−$1,287 Σ=−$25,738 | N=4 WR=75% μ=$164 Σ=$655 | N=6 WR=17% μ=$556 Σ=$3,336 | N=15 WR=27% μ=−$816 Σ=−$12,243 | N=25 WR=32% μ=−$330 Σ=−$8,252 |
| 0.64–0.69 | N=8 WR=62% μ=$1,718 Σ=$13,741 | N=1 WR=0% μ=−$5,068 Σ=−$5,068 | N=6 WR=33% μ=$2,508 Σ=$15,045 | N=5 WR=0% μ=−$3,363 Σ=−$16,813 | N=12 WR=17% μ=−$570 Σ=−$6,836 |
| ≥0.70 | — | — | N=1 WR=100% μ=$2,893 Σ=$2,893 | — | N=1 WR=100% μ=$2,893 Σ=$2,893 |

**Composite score bucket**

| bucket | run4 | run5 | run6 | run7 | pool5-7 |
|---|---|---|---|---|---|
| <1.0 | N=9 WR=33% μ=$1,147 Σ=$10,327 | N=3 WR=67% μ=−$160 Σ=−$479 | N=6 WR=67% μ=$873 Σ=$5,240 | N=6 WR=17% μ=−$1,614 Σ=−$9,684 | N=15 WR=47% μ=−$328 Σ=−$4,923 |
| 1.0–1.5 | N=7 WR=29% μ=$533 Σ=$3,732 | N=4 WR=50% μ=−$1,432 Σ=−$5,730 | N=12 WR=17% μ=−$148 Σ=−$1,781 | N=10 WR=30% μ=−$227 Σ=−$2,273 | N=26 WR=27% μ=−$376 Σ=−$9,784 |
| 1.5–2.0 | N=11 WR=45% μ=$2,156 Σ=$23,714 | N=1 WR=0% μ=−$1,185 Σ=−$1,185 | N=5 WR=20% μ=$1,663 Σ=$8,314 | N=10 WR=0% μ=−$1,717 Σ=−$17,174 | N=16 WR=6% μ=−$628 Σ=−$10,045 |
| ≥2.0 | N=10 WR=20% μ=−$859 Σ=−$8,592 | — | N=3 WR=33% μ=$2,822 Σ=$8,465 | — | N=3 WR=33% μ=$2,822 Σ=$8,465 |
| null (options/system) | N=5 WR=20% μ=−$4,430 Σ=−$22,149 | — | N=1 WR=100% μ=$5,135 Σ=$5,135 | N=7 WR=43% μ=$190 Σ=$1,331 | N=8 WR=50% μ=$808 Σ=$6,466 |

### 8. By entry time of day (ET, first lot's buy timestamp; satellite trips)


**Entry ET bucket**

| bucket | run4 | run5 | run6 | run7 | pool5-7 |
|---|---|---|---|---|---|
| 09:30–09:45 | N=12 WR=50% μ=$2,028 Σ=$24,339 | N=2 WR=50% μ=−$592 Σ=−$1,183 | N=10 WR=40% μ=$544 Σ=$5,441 | N=13 WR=8% μ=−$943 Σ=−$12,263 | N=25 WR=24% μ=−$320 Σ=−$8,005 |
| 09:45–10:30 | N=9 WR=33% μ=−$272 Σ=−$2,449 | N=3 WR=67% μ=−$1,204 Σ=−$3,612 | N=4 WR=25% μ=$337 Σ=$1,348 | N=8 WR=38% μ=−$1,213 Σ=−$9,702 | N=15 WR=40% μ=−$798 Σ=−$11,966 |
| 10:30–12:00 | N=4 WR=25% μ=−$1,236 Σ=−$4,943 | — | N=2 WR=0% μ=−$911 Σ=−$1,822 | N=5 WR=0% μ=−$1,080 Σ=−$5,400 | N=7 WR=0% μ=−$1,032 Σ=−$7,222 |
| 12:00–14:00 | N=11 WR=9% μ=−$853 Σ=−$9,384 | N=3 WR=33% μ=−$866 Σ=−$2,599 | N=7 WR=43% μ=$2,809 Σ=$19,665 | N=3 WR=100% μ=$2,609 Σ=$7,828 | N=13 WR=54% μ=$1,915 Σ=$24,894 |
| 14:00–16:00 | N=6 WR=33% μ=−$89 Σ=−$533 | — | N=4 WR=25% μ=$185 Σ=$741 | N=4 WR=0% μ=−$2,066 Σ=−$8,263 | N=8 WR=12% μ=−$940 Σ=−$7,521 |
| unknown | — | — | — | — | — |

**Entry ET bucket — all trips incl. system**

| bucket | run4 | run5 | run6 | run7 | pool5-7 |
|---|---|---|---|---|---|
| 09:30–09:45 | N=12 WR=50% μ=$2,028 Σ=$24,339 | N=3 WR=67% μ=−$129 Σ=−$387 | N=12 WR=42% μ=$445 Σ=$5,340 | N=13 WR=8% μ=−$943 Σ=−$12,263 | N=28 WR=29% μ=−$261 Σ=−$7,309 |
| 09:45–10:30 | N=9 WR=33% μ=−$272 Σ=−$2,449 | N=4 WR=50% μ=−$904 Σ=−$3,616 | N=5 WR=40% μ=$271 Σ=$1,355 | N=8 WR=38% μ=−$1,213 Σ=−$9,702 | N=17 WR=41% μ=−$704 Σ=−$11,963 |
| 10:30–12:00 | N=4 WR=25% μ=−$1,236 Σ=−$4,943 | N=1 WR=100% μ=$1,799 Σ=$1,799 | N=3 WR=33% μ=−$594 Σ=−$1,781 | N=6 WR=0% μ=−$1,197 Σ=−$7,180 | N=10 WR=20% μ=−$716 Σ=−$7,162 |
| 12:00–14:00 | N=11 WR=9% μ=−$853 Σ=−$9,384 | N=3 WR=33% μ=−$866 Σ=−$2,599 | N=8 WR=50% μ=$2,625 Σ=$20,997 | N=3 WR=100% μ=$2,609 Σ=$7,828 | N=14 WR=57% μ=$1,873 Σ=$26,226 |
| 14:00–16:00 | N=6 WR=33% μ=−$89 Σ=−$533 | — | N=4 WR=25% μ=$185 Σ=$741 | N=4 WR=0% μ=−$2,066 Σ=−$8,263 | N=8 WR=12% μ=−$940 Σ=−$7,521 |
| unknown | — | — | — | — | — |

### 9. By holding period (exit ts − first-lot buy ts; satellite trips)


**Holding bucket**

| bucket | run4 | run5 | run6 | run7 | pool5-7 |
|---|---|---|---|---|---|
| <1 day | N=14 WR=14% μ=−$1,757 Σ=−$24,601 | — | N=3 WR=0% μ=−$492 Σ=−$1,476 | N=4 WR=25% μ=−$319 Σ=−$1,275 | N=7 WR=14% μ=−$393 Σ=−$2,751 |
| 1–3 d | N=10 WR=10% μ=−$1,301 Σ=−$13,010 | N=5 WR=60% μ=−$1,119 Σ=−$5,593 | N=1 WR=100% μ=$109 Σ=$109 | N=5 WR=0% μ=−$1,657 Σ=−$8,287 | N=11 WR=36% μ=−$1,252 Σ=−$13,772 |
| 3–7 d | N=9 WR=67% μ=$4,075 Σ=$36,671 | N=3 WR=33% μ=−$600 Σ=−$1,801 | N=11 WR=27% μ=$1,355 Σ=$14,910 | N=7 WR=14% μ=−$1,066 Σ=−$7,463 | N=21 WR=24% μ=$269 Σ=$5,646 |
| >7 d | N=9 WR=44% μ=$886 Σ=$7,971 | — | N=12 WR=42% μ=$986 Σ=$11,830 | N=17 WR=29% μ=−$634 Σ=−$10,774 | N=29 WR=34% μ=$36 Σ=$1,056 |
| unknown | — | — | — | — | — |

Mean holding days, satellite winners vs losers: run4: winners 6.0 d (n=13), losers 3.4 d (n=29); run5: winners 2.3 d (n=4), losers 3.5 d (n=4); run6: winners 6.8 d (n=9), losers 6.5 d (n=18); run7: winners 12.7 d (n=7), losers 9.0 d (n=26); pool5-7: winners 8.0 d (n=20), losers 7.6 d (n=48)

### 10. By lot sequence (open buy lots for the symbol at exit time, FIFO-derived; satellite trips)


**Lots open at exit**

| bucket | run4 | run5 | run6 | run7 | pool5-7 |
|---|---|---|---|---|---|
| single lot (no top-up) | N=39 WR=31% μ=$623 Σ=$24,312 | N=8 WR=50% μ=−$924 Σ=−$7,394 | N=24 WR=33% μ=$900 Σ=$21,588 | N=28 WR=18% μ=−$699 Σ=−$19,576 | N=60 WR=28% μ=−$90 Σ=−$5,382 |
| topped-up (≥2 lots open) | N=3 WR=33% μ=−$5,760 Σ=−$17,281 | — | N=3 WR=33% μ=$1,261 Σ=$3,784 | N=5 WR=40% μ=−$1,645 Σ=−$8,223 | N=8 WR=38% μ=−$555 Σ=−$4,439 |
| unmatched (0 lots) | — | — | — | — | — |

run-7 ledger `lots_n` vs FIFO-derived open-lot count: 3/34 sells differ (ledger counts lots per bracket leg for bracket exits; e.g. INTC bracket_stop lots_n=1 while 3 lots were open). run-7 ledger lots_n distribution: {1: 32, 3: 1, 2: 1}.
run-7 cross-check of FIFO matching against the ledger's own `entry_conviction`/`entry_ts`: conviction mismatches 0/34, entry_ts mismatches (>2 min) 0/34.

### 11. Equity curve (equity_history.jsonl, one row per session)

| run | first row (date, equity) | last row (date, equity) | return first→last | return vs $1,000,000 nominal | peak equity (date) | max drawdown (peak date → trough date) | sessions | realized_pl at last row (equity file) |
|---|---|---|---|---|---|---|---|---|
| run4 | 2026-07-28 $1,004,491 | 2026-08-21 $1,006,732 | +0.22% | +0.67% | $1,024,972 (2026-08-04) | -2.98% (2026-08-04 → 2026-08-18) | 19 | $5,966 |
| run5 | 2026-08-24 $1,000,110 | 2026-08-31 $990,051 | -1.01% | -0.99% | $1,009,812 (2026-08-26) | -1.96% (2026-08-26 → 2026-08-31) | 6 | −$5,198 |
| run6 | 2026-08-31 $1,000,000 | 2026-09-21 $1,044,164 | +4.42% | +4.42% | $1,044,164 (2026-09-21) | -3.29% (2026-09-08 → 2026-09-15) | 15 | $25,803 |
| run7 | 2026-09-21 $1,000,000 | 2026-10-26 $977,418 | -2.26% | -2.26% | $1,000,000 (2026-09-21) | -3.20% (2026-09-21 → 2026-10-15) | 26 | −$32,079 |

Ledger realized P&L (sum of trips, all exits) for comparison: run4: $7,031 (satellite $7,031, system $0); run5: −$4,802 (satellite −$7,394, system $2,592); run6: $26,653 (satellite $25,373, system $1,280); run7: −$29,580 (satellite −$27,799, system −$1,780); pool5-7: −$7,729 (satellite −$9,821, system $2,092)

## (c) Ten most robust cross-run facts (ranked by consistency of direction across runs 4, 5, 6, 7)

1. **[4/4] Satellite ex-top-3 mean is negative in every run**: −$714 (run-4, N=39 rest), −$1,861 (run-5), −$371 (run-6), −$1,352 (run-7); pooled 5+6+7 −$678 on 65 remaining trips (WR 26%, 95% LB −$1,140); the run-4 Aug 3–14 window alone is −$340. Run-7's −$1,352 (WR ex-top-3 13%) is the kill-criterion input so far.
2. **[4/4] The three best satellite trips exceed each run's entire net satellite P&L**: run-4 $34,889 vs net $7,031 (4.96×); run-6 $34,272 vs $25,373 (1.35×); run-5 and run-7 net totals are negative even with their top-3 in; the top-3 are 44% / 93% / 70% / 75% of gross wins.
3. **[4/4] The median satellite trip loses $500–800 and the win rate never reaches above 50%**: medians −$720 / −$524 / −$773 / −$732 (pooled −$752); win rates 31% / 50% / 33% / 21% (pooled 29%).
4. **[4/4] No run or window clears the project's 95% one-sided expectancy bar**: the lower bound of the mean satellite P&L is −$1,194 / −$2,247 / −$480 / −$1,543 (pooled −$820; run-4 Aug 3–14 window −$691), including run-6 whose satellite total was +$25,373.
5. **[4/4] Satellite trips closed within 3 days of the first fill lose in every run**: run-4 N=24 Σ −$37,611, run-5 N=5 Σ −$5,593, run-6 N=4 Σ −$1,367, run-7 N=9 Σ −$9,562 (pooled 5–7 N=18, WR 28%, mean −$918); holds ≥3 d were +$44,642 (run-4) and +$26,740 (run-6) but −$1,801 (run-5) and −$18,237 (run-7).
6. **[4/4] `decision` exits carry a negative mean in every run**: −$1,074 (N=23, WR 9%), −$77 (N=6, WR 67%), −$622 (N=5), −$1,247 (N=14, WR 7%); pooled 5–7 N=25, WR 28%, mean −$841, Σ −$21,028 — the only exit reason with a consistent sign that is not fixed by construction (stops 0% WR, takes/trails 100% WR by definition).
7. **[4/4] Trips whose cited `key_signals` mention options_chain have a positive mean in every run**: +$475 (N=14), +$295 (N=2), +$1,731 (N=14), +$564 (N=8); pooled 5–7 N=24, WR 46%, mean +$1,223, Σ +$29,342 — the only signal family with a 4/4 positive sign (run-5 N=2).
8. **[4/4] Peak-to-trough drawdown stayed within 3.3% in every run** (−2.98%, −1.96%, −3.29%, −3.20% on daily closes) while end-to-end returns vs the $1,000,000 nominal start were +0.67% (19 sessions), −0.99% (6), +4.42% (15), −2.26% (26).
9. **[3/4] Trips citing `news` had zero wins in runs 5, 6 and 7** (N=1, 3, 6; pooled N=10, WR 0%, Σ −$10,480), the reverse of run-4 (N=4, WR 75%, Σ +$15,667).
10. **[3/4] From run-5 onward stop exits outnumber take+trail exits by at least 2:1** (run-5 2 vs 0, run-6 15 vs 7, run-7 12 vs 6; pooled 29 stops Σ −$53,787 at −$1,855 each vs 13 take/trail Σ +$65,424 at +$5,033 each); run-4 was the reverse (8 stops vs 11 take/trail, Σ −$43,791 vs +$75,516).

Also noted (sign flips, so not ranked): conviction is effectively constant — 67 of 68 pooled satellite trips sit in 0.60–0.75; the 0.64–0.69 sub-band was the best bucket in run-4 (+$1,718, WR 62%) and run-6 (+$2,508) and the worst in run-7 (N=5, WR 0%, −$3,363). Composite does not rank outcomes either: ≥2.0 was WR 20% / −$859 in run-4 (N=10) and 1.5–2.0 was WR 0% / −$1,717 in run-7 (N=10). Bearish option trips were net positive in both runs that had them (run-6 N=1 +$5,135; run-7 N=7 WR 43% +$1,331) while run-4's bullish calls/call spreads were 1W/4L, Σ −$22,149; bullish-equity satellite trips pooled 5–7 are N=60, WR 27%, mean −$271, with run-7 at WR 15% / −$1,120 per trip (N=26).

## (d) Caveats

- **Sample sizes**: satellite N = 42 / 8 / 27 / 33 (pooled 5–7 = 68); run-5 is 6 sessions. Most sub-buckets (signal, time-of-day, holding, lots, options sub-type) have N < 10 and are descriptive only; the 4/4 sign consistency in facts 7 and 9 rests on cells with N = 1–3 in run-5.
- **Trip = one sell row**, matching `scripts/eval_contract_check.py`. A multi-lot position can emit two sell rows at the same instant (run-7 SMCI 2026-10-08 19:06, both priced off the blended average cost), so N is overstated by 1 there; partial trims (core_defense 25%) count as trips.
- **Excluded**: run-4's two Aug 17 `external` rows with realized_pl 0.0 (AMZN 4-leg spread and 2-contract call that vanished broker-side; the call shows exit_price 22.0 vs entry 46.75 but 0.0 P&L — an unrecorded loss not in any table). Run-7 has **no** 2026-10-27 rows and **no** `flatten` rows, so the Oct 27 flatten is absent from both trip stats and the equity file (last row Oct 26, $977,418).
- **Entry attributes for runs 4–6 are reconstructed** by FIFO-matching each sell to open buy lots of the same (symbol, instrument); the first open lot supplies conviction / composite / signals / entry time. Validated only on run-7, whose sells carry their own `entry_ts`/`entry_conviction` (0/34 mismatches); 0 unmatched sells and 0 negative-qty rows in any file. Run-4 options are keyed by underlying symbol, later runs by OCC symbol (single leg) or underlying (spreads).
- **Satellite/system split** follows the given exit-reason set; the project checker additionally drops QQQ/PSQ by symbol — that yields identical numbers here because no QQQ/PSQ row left via a non-system reason. Run-4 has no system exit reasons at all (introduced in run-5), so run-4 "all" = "satellite".
- **Run-4 ledger spans Jul 28 → Aug 21 only** (the Jul 27 fresh-cycle reset), not Jul 7, and covers several change-sets (PR #44–#52); the Aug 3–14 sub-sample is 20 trips. Run-6 figures use `state-post-window/` (93 rows to Sep 21); `state/` (52 rows to Sep 11) is a strict prefix.
- **Ledger vs equity-file realized P&L do not reconcile exactly**: run-4 $7,031 vs $5,966; run-5 −$4,802 vs −$5,198; run-6 $26,653 vs $25,803; run-7 −$29,580 vs −$32,079. The gap was not investigated here.
- **Signal families**: "cited" = keyword mapping of free-text `key_signals` (e.g. "call flow" → options_flow, "MACD/RSI/uptrend" → technical, "composite +x" counted as its own pseudo-family); congress and lobbying never appear as the first-cited signal, so no primary-cited stats exist for them. `entry_signals` lists families fed, not used: news/technical/fundamentals are on ~95% of buys, and `options_flow` is absent from `entry_signals` before run-6 (family added then), so the "fed-any" table is weakly discriminating.
- **Conviction/composite buckets** as requested collapse (all satellite trips 0.55–0.73); composite is null for every option and system fill, so option trips fall in the "null" row.
- **Entry-time buckets** reflect the bot's fixed decision cadence (ticks ≈ 09:33, 10:25, 11:17, 12:09, 13:01, 13:53, 14:46, 15:38 ET), so they are really "which tick"; ET = UTC−4 throughout (all dates in EDT).
- **Holding period and top-up flag** use the first lot: hold is overstated for later lots of topped-up positions; "topped-up" = ≥2 open lots at exit (FIFO-derived) — only 11 such trips across all runs; run-7's own `lots_n` differs on 3/34 rows (bracket legs count their own lot).
- **Statistics**: sample sd; 95% one-sided lower bound = mean − t(0.95, N−1)·sd/√N with an own t-quantile implementation (no scipy in the venv) checked against table values (t₀.₉₅,₁₀ = 1.812, t₀.₉₅,₃₀ = 1.697). Realized P&L taken as recorded (`realized_pl`), including the contract multiplier for options; option sells have `exit_price` null.
- **Not analysed**: `realized_pl_pct`, `quote_realized_pl` (run-7 only), `regime_label` (always "risk-on" on run-7 buys), `spy_intraday_ret_at_decision`, and the phantom/dupe filter (no duplicate (order_id, action) pairs exist, so it would drop nothing).
