# Pre-final-test-run-6 — Sep 1 → Sep 11 (scheduled) / extended to Sep 18 under Amendment 3, 2026 (INTERIM as of the Sep 11 close)

**This is the interim summary, written 2026-09-12 after the last SCHEDULED session (Sep 11).** The window continues Sep 14–18 under the freeze (Amendment 3, Option B). The final verdict is `scripts/eval_contract_check.py` run once on the Sep 18 `basis='close'` row (`--end 2026-09-18`, both pinned `--start` commands, `--start 2026-08-31` primary). A **"FINAL"** section is appended to this file then. Every number below is from the Sep 11 `basis='close'` row and supersedes every intraday figure in the analyst texts.

**Account** Alpaca paper `PA3B09IK4MGS` (fresh, Amendment 1), opening balance **$1,000,000** (the Aug 31 `basis='late'` row, `state/equity_history.jsonl`).
**Result (interim)** **$1,038,988.75 (+3.90%)** at the Sep 11 close vs SPY −0.36% (767.05 → 764.29) / QQQ −0.26% (716.76 → 714.88) / IWM −1.71% (293.93 → 288.89), Aug 31 close basis · max drawdown **−1.41%** (peak Sep 8 → trough Sep 10) · 8 sessions, 8/8 verdict rows `basis='close'`.
**Checker verdict** **PENDING** (contract v2, `--start 2026-09-01 --end 2026-09-11`, exit 3): every counted rule passes; N=14 < 24 → rule-1 extension to Sep 18 applies as pre-registered. Copy: `runs/pre-final-test-run-6/CHECKER_2026-09-11_close.txt` (byte-identical to the analysis copy).
**Grade B+ (provisional)** — up from B− (run-4, Aug 23). Ceiling at this N is B+; every component grade below is provisional for the same reason (14 rows, 7 pairs, 3 trips = 98.8% of realized).
**Config** frozen at merge `580915d` + the `.env` STRATEGY KEYS in `docs/RUN6_SWITCH.md`; freeze re-registered to Fri Sep 19 (Amendment 3). Freeze verified empty on `investment_strategy/ .env` through Sep 12.
**Sep 11 session** book +1.043% ($1,028,264.64 → $1,038,988.75) vs SPY +0.852%.

## Headline numbers (Sep 11 close row; checker v2 + `autopsy_run6_out.txt`; ✓✓ = verified twice, independent recomputes agree)

| Metric | Run-6 through Sep 11 (N=14) | Satellite-only (N=12) | Run-4 for scale (42 trips) |
|---|---|---|---|
| Closed rows with non-null `realized_pl` | **14** (12 satellite + PSQ `hedge_unwind` + QQQ `core_defense`) ✓✓ | 12 | 42 |
| Realized P&L | **+$26,962.66** ✓✓ | +$26,893.34 | +$7,031 |
| Win rate | 64.3% (9W / 5L / 0 flat) ✓✓ | — | 31.0% |
| Avg win / avg loss | $3,459.43 / −$834.44 | — | $6,083 / −$2,485 |
| Expectancy / trade | **+$1,925.90** | +$2,241.11 | +$167 |
| One-sided 95% t vs crit (df) | **1.733 vs 1.771 (df 13) — misses by 0.04** ✓✓ | 1.750 vs 1.796 | — |
| Bootstrap-95 CI on the mean | [$118.17, $4,203.90] | — | — |
| Profit factor | **7.46** ✓✓ | — | 1.10 |
| Top-3 share / ex-top-3 mean | **98.8%** ($26,637.68: BE +$12,525.11, INTC +$8,930.46, BE +$5,182.11) / **+$29.54** (other 11 sum +$324.98) ✓✓ | — | — |
| Avg hold, winners / losers | 6.58 d / 6.57 d | — | — |
| Max drawdown (close rows) | **−1.41%** (Sep 8 → Sep 10) | — | −2.98% |
| Worst session | −$7,446.11 Sep 9 (self-consistent Δequity) / −$7,830.19 Sep 10 (broker `day_pl`) | — | −$26.8k (Aug 18) |
| Ex-ante `book_beta_spy`, mean over 8 close rows | **0.96** (0.77..1.11) → rule 4 **PASS** (Amendment 2) | — | — |
| OLS alpha / beta (7 pairs, recorded, NOT counted) | +0.513%/day, t 1.46, beta 0.50, r² 0.15 | — | — |
| Capture vs SPY | 3 up / 4 down → **INSUFFICIENT SAMPLE, not counted** (need 4/4) | — | 1.19 / 2.31 FAIL |
| Capture vs QQQ / IWM (informational, 4u/3d) | QQQ up 142.6% / down −17.3%; IWM up 154.5% / down −8.7% | — | — |
| Beta-adjusted SPY capture (4d, informational) | up 118.0% / down −57.7% (3u/4d, 7 stamped-beta days) | — | — |
| Rule 7 (decision-sell losses < 0.5× stop) | **0** (PASS) | — | — |
| Telescoping (validity) | max gap **$1,421.49** Sep 3 → FAIL, disclosed + accepted (Amendment 3 (i)) | — | — |
| Pooled expectancy (rule 2) | N=14 < 60 → PENDING | — | — |

Sum-of-`day_pl` $34,992.01 vs equity diff $38,045.11 over 7 pairs — the telescoping breach (see Measurement).

## Daily table (8 sessions, close-to-close; `basis='close'` rows; index closes from `spy.csv` / `qqq.csv` / `iwm.csv`)

| Session | Equity (close) | Book % | SPY % | QQQ % | IWM % | ex-ante β | broker `day_pl` | Δequity (self-consistent) | gap | Hedge state |
|---|---|---|---|---|---|---|---|---|---|---|
| Sep 1 | 1,000,943.64 | +0.094 | −0.687 | −1.272 | −1.143 | 0.77 | +943.64 | +943.64 | 0.00 | none |
| Sep 2 | 1,005,629.55 | +0.468 | +0.444 | +0.226 | +1.184 | 1.04 | +5,462.68 | +4,685.91 | −776.77 | none (HD Oct-16 350P bought, $3,213) |
| Sep 3 | 1,016,452.05 | +1.076 | +1.047 | +1.189 | +0.401 | 1.11 | +9,401.01 | +10,822.50 | +1,421.49 | PSQ armed 10:14 CT $203,604.82 + 12:51 CT $62,173.65 (10,283.18 sh, avg 25.8459) |
| Sep 4 | 1,025,915.86 | +0.931 | −0.385 | +0.180 | +0.278 | 1.08 | +8,318.03 | +9,463.81 | +1,145.78 | PSQ on (IWM Oct-9 295/280 put spread bought, $5,083) |
| Sep 8 | 1,042,978.24 | +1.663 | −0.549 | −0.083 | −0.453 | 0.92 | +15,645.11 | +17,062.38 | +1,417.27 | PSQ on |
| Sep 9 | 1,035,532.13 | −0.714 | −0.465 | −0.285 | −1.368 | 0.99 | −7,641.31 | −7,446.11 | +195.20 | PSQ unwound (order 09:22 CT, fill logged 10:14:41; 10,283.18 sh @25.84); off rest of day |
| Sep 10 | 1,028,264.64 | −0.702 | −0.599 | −1.064 | −1.012 | 0.89 | −7,830.19 | −7,267.49 | +562.70 | off until the 11:06 CT cycle re-arm ($204,391; 7,834.07 sh @26.09; `Sep_10_2026.log:202/205`), then on |
| Sep 11 | 1,038,988.75 | +1.043 | +0.852 | +0.873 | +0.414 | 0.89 | +11,636.68 | +10,724.11 | −912.57 | PSQ on (open lot marked 25.93 = −$1,253) |

Beat SPY on 6 of 8 sessions; green on 3 of the 5 SPY red days (Sep 1, 4, 8); lost only Sep 9–10 (−$14,714 c2c combined) with the hedge off. The autopsy's Sep 11 row (1,034,842.49, +0.640%, SPY +1.057%) is intraday and is superseded by the close row above.

## What earned and what bled

- **Mechanical exits earned everything (10 of 14 trips, 98% of P&L):**
  - trail +$16,495.47 (3): BE lot-2 +$12,525.11, NVDA +$2,893.44, NU +$1,076.92 — all fired within 33 min of the Sep 9 gap-down open, giving back $9,830 = 37% of peak open profit (geometry, not defect);
  - bracket_take +$14,112.57 (2): INTC +$8,930.46, BE lot-1 +$5,182.11;
  - events-only decision sells +$457.51 (2): F +$348.61, MKL +$108.90 — both winners; both would have been worse held to Sep 11 (F −$321.00, MKL −$487.08);
  - system rows: PSQ hedge_unwind +$41.76, QQQ core_defense +$27.56.
- **Bled:** bracket_stop −$4,172.20 over 5 (ABT −$951.47, AAPL −$923.92, BLK −$835.92, SNXX −$772.68, RIG −$688.22). All 5 cited `technical`, 4/5 cited options_flow/chain — n=5, informational. 0 of the tight-stop names were back above fill by T+10. **0 decision-sell losses** — `LLM_SELL_AUTHORITY=events_only` removed the pre-run-6 loss engine (58 decision exits / 45 losses / −$32,303 pooled).
- **Signal families (split credit, cited on the matched buy):** options_flow +$5,805 (n=9), technical +$4,199 (n=10), options_chain +$3,825 (n=6), fundamentals +$3,246 (n=6), offexchange +$2,293 (n=3), insider +$2,248 (n=6), news −$190 (n=1, ABT). No cited family is net-negative with a CI excluding zero in ≥ 2 runs. Composite inversion absent in run-6 (rho +0.24).
- **Two red days (Sep 9 −$7,446, Sep 10 −$7,267, self-consistent):** SPCX −3,860, SMCI −3,325 (legacy lot −2,447 / Sep-10 top-up −878), QQQ −1,629, SEI −1,598, DRAM −885, NOK −709, INTC −693, AVGO −719, NVDA −662, NU −587, ONON −516 (c2c). The hedge was off from the Sep 9 unwind (fill 10:14:41 CT) to the Sep 10 11:06 CT re-arm — rule-correct: ex-hedge beta read 0.82–0.99 and the hedged-beta counterfactual read 0.44–0.81 < 0.85 on all 13 reads, so no unwind-persistence N ≤ 9 (docstring beta) / ≤ 13 (measured) keeps it.
- **Hedge P&L (PSQ):**
  - ledger realized +$41.76 (−$61.07 at the 25.84 fill vs 25.8459 avg cost); open lot 7,834.07 sh @26.09 marked 25.93 at the Sep 11 close = −$1,253 → **PSQ net ≈ −$1,212** for the window;
  - day P&L while held: Sep 3 −$575.23 (from fills), Sep 4 −$205.66, Sep 8 +$102.83, Sep 9 to unwind +$616.99, Sep 10 from re-arm +$548.38, Sep 11 −$1,802 at the 25.93 close (the autopsy's −$2,311.05 is at the 25.865 intraday mark);
  - counterfactual hold of the Sep 3 tranche through the Sep 10 close: +$3,229.54 vs realized +$41.76 → foregone +$3,187.78;
  - **whipsaw cost is definition-dependent (corrected):** (a) fill-to-fill forfeited on the unwound shares = 10,283.18 × (26.09 − 25.84) = **$2,571**; (b) original lot held Sep 8 close → Sep 11 close (+$1,542.48 at 25.93) vs actual (+$41.76 realized − $1,253.45 open) = **$2,754** (the synthesis's figure; $2,595 at the 25.865 intraday mark); (c) original lot from the 25.85 exit → 25.93 close vs the new lot's mark only = **$2,076** (critic's recompute; $1,917 at 25.865);
  - two of three arms were ~50% over-sized: Sep 3 1.20 → $203,605 PSQ → 0.89 (implied PSQ beta −1.55), Sep 10 1.20 → $204,391 → 0.90 (implied −1.51); bot's own 60-d shrunk PSQ beta −1.506 (raw −1.633). Correct Sep 3 size ≈ (1.20 − 1.00) × $1.016M / 1.51 = $134.6k → ~$68k excess cash per arm (the Sep 10 arithmetic is not printed in the inputs — critic #14; the Sep 3 12:51 top-up "cash-capped" claim is uncited).
- **Options sleeve (unrealized, N=0 closed):**
  - HD Oct-16 350P ×1 ($3,213 debit) +$545; IWM Oct-9 295/280 put spread ×13 ($5,083 debit) +$2,457 net (long 295P +$3,705 / short 280P −$1,248) ≈ +$3.0k;
  - **mark source:** the analyst context pack's broker live-position snapshot, taken intraday Sep 11 (PSQ marked ~25.87 in the same snapshot vs the 25.93 close) — not re-marked at the close;
  - on Sep 9 the sleeve contributed +$2,758 (28% of the −$9,920 equity-side c2c loss) from $8.5k of premium vs +$617 from $266k of PSQ; 0 gate bypasses; pre-run-6 sleeve expectancy −$4,142/trip (N=6) remains unanswered.
- **Bearish sleeve:**
  - 2 fills ($8.3k debit), both green at last (intraday) mark;
  - 2 of 3 proxy attempts self-rejected on OI — Sep 1 11:10:39 IWM 291P OI 38; Sep 3 14:42:24 294P OI 47; Sep 4 13:46:35 295P approved (OI 1,011) — the builder's own pick;
  - 5 of 5 single-name put attempts died at far-from-money strikes (HD 400P 25% ITM OI 2; LTH 30P 28% OTM OI 26; LYV 150P/140P 12–18% OTM OI 5/87; SCI 75P 6% OTM OI 5; AAL 11P/10P 14–22% OTM, $0.03–0.14 premium, spreads 12–91%) while each chain had OI ≥ 100 within ~3–7% of spot;
  - 3 "ELIGIBLE but IGNORED" on Sep 10 invisible to the `-> IGNORED` handle (the funnel line truncates).
- **Concentration:** one name took 56–71% of a day's ex-QQQ/PSQ buy dollars on 5 sessions (Sep 2 SMCI 56%, Sep 3 BE 56%, Sep 8 AVGO 71%, Sep 10 INTC 65%, Sep 11 NU 60%; Sep 4's only buy was the $5,083 IWM spread). Those names produced the two largest winners (BE +$11,828 to date, SMCI +$4,698 to date) and no identifiable harm (AVGO −$728, NU −$1,089). No cap change.
- **Slot / rotation:** "At max open positions" rejects = **3 verified + 1 unverified (corrected)** — Sep 3 HOOD ×2 (10:19:17, 13:46:34) and Sep 4 MU (09:26:07,076) verified by ledger replay at 16 equity rows incl. QQQ + PSQ; Sep 2 PCG (14:39:52) not reproduced by replay. Every blocked name went down or flat afterwards (HOOD −9.1%, MU −3.7%, PCG +4.1%) — correctness defect, no run-6 P&L cost.
- **Fresh buys by entry-day SPY sign (c3, run-6):** red n=20, $623,865, same-day −$2,871 (−0.46%), next-day +$3,854, to-Sep-11 +$9,707; green n=8, $276,831, same-day +$501, next-day +$7,365, to-Sep-11 +$19,336 (to-date marks use the `ic_prices` last row — may be intraday). Pooled ledger: red-day entries +$88/trip, 46.7% WR (n=105) vs green +$187, 38.6% (n=57) — item 8 "red-tape entries lose" is refuted.

## Closed trips (14 rows; `exit_ts` = fill_ts when stamped; `bf`=Y rows are exchange-backfilled bracket exits whose ledger time is later than the real fill)

| sym | entry (UTC) | exit (UTC) | hold d | qty | entry | exit | comp | conv | stop % | exit_reason | realized $ | % | bf |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| MKL | 09-02 17:02 | 09-04 14:26 | 1.89 | 9 | 1824.290 | 1836.390 | 0.83 | 0.60 | 4.0 | decision | +108.90 | +0.66 | |
| BE | 09-02 14:25 | 09-08 13:31 | 5.96 | 83 | 209.900 | 272.130 | 0.90 | 0.60 | 10.0 | bracket_take | +5,182.11 | +29.77 | Y |
| F | 09-01 13:35 | 09-08 16:10 | 7.11 | 2,140 | 13.990 | 14.153 | 0.86 | 0.60 | 4.0 | decision | +348.61 | +1.16 | |
| INTC | 09-01 13:36 | 09-08 16:50 | 7.13 | 462 | 86.773 | 105.890 | 1.09 | 0.62 | 8.92 | bracket_take | +8,930.46 | +22.33 | Y |
| BE | 09-03 17:03 | 09-09 13:32 | 5.85 | 299 | 234.390 | 276.280 | 1.66 | 0.66 | 10.0 | trail | +12,525.11 | +17.87 | |
| NU | 09-01 13:36 | 09-09 13:37 | 8.00 | 2,061 | 14.523 | 15.045 | 0.73 | 0.60 | 5.4 | trail | +1,076.92 | +3.60 | |
| BLK | 09-01 17:02 | 09-09 13:49 | 7.87 | 18 | 1130.080 | 1082.480 | 0.61 | 0.60 | 4.0 | bracket_stop | −835.92 | −4.11 | Y |
| NVDA | 09-01 13:35 | 09-09 14:03 | 8.02 | 370 | 216.120 | 223.940 | 1.31 | 0.73 | 5.54 | trail | +2,893.44 | +3.62 | |
| PSQ | 09-03 15:14 | 09-09 14:22 | 5.96 | 10,283.2 | 25.846 | 25.850 | — | — | — | hedge_unwind | +41.76 | +0.02 | |
| RIG | 09-02 19:39 | 09-09 17:10 | 6.90 | 1,419 | 6.210 | 5.720 | 1.42 | 0.62 | 7.82 | bracket_stop | −688.22 | −7.82 | Y |
| AAPL | 09-01 14:25 | 09-09 18:14 | 8.16 | 64 | 324.938 | 310.654 | 1.12 | 0.63 | 4.43 | bracket_stop | −923.92 | −4.44 | Y |
| ABT | 09-01 15:17 | 09-10 13:33 | 8.93 | 192 | 109.540 | 104.574 | 1.10 | 0.60 | 4.5 | bracket_stop | −951.47 | −4.52 | Y |
| SNXX | 09-09 14:26 | 09-10 13:53 | 0.98 | 425 | 18.065 | 16.222 | 1.58 | 0.60 | 10.0 | bracket_stop | −772.68 | −10.08 | Y |
| QQQ | 09-01 13:36 | 09-10 19:38 | 9.25 | 54 | 707.524 | 709.230 | — | — | — | core_defense | +27.56 | +0.07 | |

Winners 9 / +$31,134.86 · losers 5 / −$4,172.20 · sum **+$26,962.66**. Winners for backtracking: BE ×2 (+$17,707 realized; +$11,828 to date on the open remainder), INTC, NVDA, NU. Losers: all five are bracket stops on Sep 1–2 entries plus the SNXX same-week stop.

### Pooled history for scale (197 ledger sells → 158 FIFO-joined trips; 39 phantoms/dupes, all pre-run-5)

| run | N | sum | exp/trip | WR | PF |
|---|---|---|---|---|---|
| archive_0711 | 35 | −$426 | −$12 | 51% | 0.64 |
| archive_0727 | 52 | −$6,054 | −$116 | 35% | 0.16 |
| run4 | 42 | +$7,031 | +$167 | 31% | 1.10 |
| run5 | 11 | −$4,802 | −$437 | 55% | 0.49 |
| run6 (Sep 1–11) | 14 | +$26,963 | +$1,926 | 64% | 7.46 |

Exit classes pooled: mech_profit +$2,243/trip CI [1,227, 3,369]; mech_stop −$2,511 CI [−4,029, −1,332]; decision −$434 CI [−702, −179]. Same-day exits are the only hold bucket with a CI excluding zero (N=22, −$754, CI [−1,336, −203]).

## Grade by component (provisional; critic corrections applied where marked)

| Component | Grade | Evidence (Sep 11 close numbers) |
|---|---|---|
| Strategy (expectancy, alpha) | **B** | +$26,963 on 14 rows, PF 7.46, WR 64%, t 1.733 (misses 1.771 by 0.04); alpha +0.51%/day t 1.46 (7 pairs, not interpretable); but 98.8% in 3 trips, ex-top-3 mean +$29.54. Run-4: +$167/trade, PF 1.10. |
| Downside / capture | **A−** | +3.90% vs SPY −0.36%; beat SPY 6/8 sessions; green on 3/5 SPY red days; DD −1.41% (run-4 −2.98%); worst session −$7,446 Sep 9 (self-consistent) / −$7,830 Sep 10 (broker). Beta-adjusted SPY down-capture −57.7% (7 pairs, INSUFFICIENT SAMPLE, informational; CHECKER_close.txt (4d)). **Corrected:** the synthesis's "−47% (8 pairs)" is the beta-adjusted −46.6% from `verifierC5_v2_start2026-08-31_end2026-09-11.txt:37`, a run whose end row is the Sep 11 intraday row — do not cite. Raw SPY capture prints no number. Lost Sep 9–10 (−$14,714 c2c) with the hedge off. |
| Bearish sleeve | **C+** | 2 fills ($8.3k debit), both green at last mark (+$545 / +$2,457, intraday marks); 2/3 proxy attempts self-rejected on OI; 5/5 single-name put attempts died at far-from-money strikes; 3 ELIGIBLE-but-IGNORED on Sep 10 invisible to the handle. |
| Options sleeve | **B (unrealized, N=0 closed)** | No closed option trips; marks ≈ +$3.0k from the context pack's intraday Sep 11 live-position snapshot (not the close); 0 bypasses; Sep 9 cushion +$2,758 vs +$617 from $266k PSQ. Pre-run-6 −$4,142/trip (N=6) unanswered. |
| System / ops | **B** (analyst B+, downgraded) | 62/62 cycles on cadence, 0 halts, 0 CRITICALs in-window, freeze held. Verified defects: hedge sizer ignores PSQ's measured beta (2/3 arms ~50% over-hedged); slot cap counts QQQ/PSQ (3 verified + 1 unverified rejects — corrected); core defense fired on a 17h52m-stale map and left QQQ stopless 4m53s on Sep 11; regime label flapped 5× in 8 reads (Sep 10); Sep 7 holiday storm 75/75 alerts undelivered; 3 liveness false-fires; OAuth wedge 2h44m. |
| Measurement | **B−** | Basis fixed + immutable (good). Telescoping broken **7 of 8** close-row pairs (corrected; max $1,421.49 Sep 3, Sep 11 −$912.57); `realized_pl` at pre-fill quote on 7/14 rows (−$72.24 net); `fill_price` null on 7/14; checker admits intraday rows as sessions, drops day 1 under one command, counts system rows in N; two verdict commands; funnel line truncates IGNORED. |
| Process | **B** | **0** freeze commits on `investment_strategy/ .env` (Aug 31 → Sep 12); **1** mid-window amendment to a counted rule with readings known (Amendment 2, Sep 9); **1** verdict-command ambiguity never resolved in-window (both readings now recorded); Amendment 3 registered Sep 12 after the scheduled window's last close row, admissible on rule 1's pre-registered extension clause, not on timing. Todo-4 discipline good. |
| **Overall** | **B+ (provisional)** | Up from B−. Ceiling at this N is B+. |

**Explicit A+ bars (measurable over ≥ 21 run-7 sessions, all on the `basis='close'` series):**
- Strategy: satellite N ≥ 24 in-window; mean > 0 at 95% one-sided; bootstrap-95 lower bound > 0; **mean ex-top-3 > 0**; PF ≥ 1.5; alpha point estimate > 0 at ≥ 20 pairs.
- Downside: capture counted (≥ 6/6) with up ≥ 0.9 AND down ≤ 0.8; ≥ 60% of SPY red days at or above SPY; max DD ≥ −3%; worst session ≥ −1.5%.
- Hedge: every arm lands within ±0.05 of target on the next read; 0 arm→unwind→re-arm inside 2 sessions unless the unhedged beta crossed the band; armed on ≥ 80% of cycles where ex-ante beta > target+band.
- Bearish: ≥ 1 fill/week; ≥ 5 closed bearish trips summing ≥ 0; 0 ELIGIBLE-but-IGNORED; 0 proxy-put OI self-rejects; 0 single-name puts rejected for strike moneyness.
- Options: ≥ 3 closed option trips summing ≥ 0; 0 bypasses.
- Ops: 100% cycles within 5 min of cadence; 0 halts; every CRITICAL delivered or spooled; 0 pages on closed days; 0 liveness false-fires; 0 stopless windows on the core ETF; 0 OAuth waits > 300 s.
- Measurement: telescoping < $1 on every close row; 100% closed rows with `fill_price`; |ledger − fill-restated| < $5; every watch-item handle greps in code (test); one verdict command.
- Process: 0 mid-window amendments to counted rules; verdict run once, after the last close row.

## Verified load-bearing findings (the re-rootings; verified 2× or more unless noted)

1. **Slot cap counts system rows** — `risk.py:368-376` filters `not p.is_option` with no exemption; QQQ core / PSQ hedge rows are opened via `_apply_pending_buy` (`orchestrator.py:3537/3727`) outside the sole equity `risk.evaluate(` at `:3902`; "Book FULL" is rendered at `engine.py:375` (corrected from 368-372). Ledger replay: 16 equity rows at Sep 3 15:19Z / Sep 4 14:26Z → `logs/Sep_03_2026.log` 10:19:17 / 13:46:34 HOOD rejects; `logs/Sep_04_2026.log` 09:26:07,076 `REJECT buy MU: At max open positions (15)` six seconds after the MKL fold (09:26:01,416). With the exemption MU passes at 14/15 without the MKL sale — the MKL decision exit and its 24h cooldown were artifacts. Deterministic, one code path.
2. **Hedge notional ignores the hedge ETF's beta** — `portfolio/beta.py:142-152` `gap = max(0, beta_spy − target) × equity`, no hedge-beta term (docstring "~ −1.1"); `orchestrator.py:3446-3449` passes none although `BookBeta.beta_of` (`beta.py:270`) exists and the reading prices PSQ at its shrunk beta (`beta.py:100-108`). Log deltas `Sep_03_2026.log:117/120/166/168` and `Sep_10_2026.log:202/205/253/255` imply −1.55 / −1.51. Deterministic formula bug.
3. **Proxy-put builder never reads OI** — `execution/options.py:261-318` picks argmin |dte − mid| (the Oct-9 weekly) and highest strike ≤ spot; `risk.py:1636-1652` then rejects on OI. Replay reproduces every pick; the Oct-16 monthly was inside [25,50] DTE each time with OI ≥ 5,099 (290P) / 8,829 (291P) / 14,135 (275P). The "$5 grid" heuristic is NOT supported (Oct-9 289/292/293 had OI 743/822/1,142).
4. **Single-name puts fail for ONE reason** — strikes far from the money although `prompts.py:133` asks for at/near the money; no strike-sanity step precedes `leg_liquidity`. Critic #7 adds the input defect: the model is never shown spot (`engine.py:610-624` renders only signal summaries), so the instruction is un-followable as rendered.
5. **Regime label flaps on a partial bar** — `regime.py:144-156` breadth confirm uses a fresh yfinance pull with the live partial bar; `new_cycle()` (`regime.py:73-86`) drops the only cache. `logs/Sep_10_2026.log` lines 12/86/139/201/254/304/356/403: 5 transitions across 8 reads; QQQ's Sep 9 close was +0.72% above its 50dma, Sep 10 close −0.27%. Ladder (`risk.py:850-870`) and the ×0.70 multiplier (`risk.py:772-773`) applied to different buys under different labels within one hour. Attributable P&L ≈ −$878 (the 10:19 SMCI top-up, "likely" rejected under persistence).
6. **Top-up bar is never stated to the model** — 37 of 77 risk-judged equity BUY proposals (48%; 4.6/session) died on `Top-up conviction ... no new edge` (`risk.py:590-608`; `TOPUP_MIN_CONVICTION_DELTA=0.05`, `config.py:1011`); `_held_notes` (`orchestrator.py:2031-2033`) prints the anchor without the rule. Float edge at `risk.py:600` (`prev+0.05` = 0.7100000000000001; production `logs/Jul_10` 08:57:44 LASR).
7. **Core defense fired on a stale map and left the core stopless** — `_falling_names` set only at `orchestrator.py:1018`, read inside `_apply_core_defense` (defined at `:3150`; corrected from ":897"); the Sep 10 14:38 map {DRAM, INTC, SEI} (17h52m old) fired on the Sep 11 up-open; stale guard `:3035-3039` inert in beta mode (`_breadth_counted_cycle` set only on the legacy path `:3268`); `cancel_open_orders_for` (`alpaca_client.py:800-811`) is fire-and-forget; `open_stop_sells` (`:975-998`) has no status filter so `_ensure_core_stop` (`:3809-3815`) saw the pending_cancel stop as live. `bot.log` Sep 11 08:30:18,262 `reduce_position(QQQ, 40) failed ... available 0.4975`; 08:35:11,656 new stop → **stopless up to 4m53s**; trim silently not retried. n=1, reproducible from the trace.
8. **Unwind acts on one reading** — `orchestrator.py:3418-3428`; `hedge_signal` stateless. Counterfactual: 13 consecutive reads below 0.85 (Sep 9 09:22 → Sep 10 12:50) at the measured beta → no persistence N ≤ 9 changes Sep 9; 0 noise-driven out-of-band reads in run-6.
9. **Telescoping mechanism** — `models.py:214` `day_pl = equity − last_equity`, our stamp 16:28 ET vs Alpaca's own `last_equity` mark; direction of causality unverified ("restates overnight" must not be asserted). No counted rule reads `day_pl`.
10. **Pre-run-6 loss engine was LLM loss-cutting** — 58 decision exits (40% of trips), 45 losses, −$32,303; run-4 alone 21 losses −$28,262 with a hold-to-stop/T+10 counterfactual of +$4,767 (delta +$33,032). `events_only` removed it: 0 decision losses in run-6.

**Refuted or voided claims — excluded from the plan; do not re-cite:**
1. "Rotation not atomic / pre-sale snapshot" — refuted 4×; the fold IS atomic (`orchestrator.py:4014-4023` → `2800-2809`, covered by `tests/test_risk.py:368`). Re-rooted to the slot count (finding 1).
2. "Fresh entries on SPY-red days lose" (item 8) — refuted on the pooled ledger (red +$88 / 46.7% WR vs green +$187 / 38.6%; date-clustered diff CI [−249, +1,907]); same-day drift is beta; the ex-ante proxy (prior-day SPY down) is the BEST bucket. No haircut/defer; shadow fields only.
3. "Cap 1.20 above the 1.15 arm line CAUSES the arms" — refuted as cause (without the cap the book reads 1.28/1.34 and arms larger; the INTC Sep 10 cap-bound buy did NOT arm). Knob-consistency observation only.
4. "Hedge ordering (sized before the cycle's buys) caused the Sep 9 unwind" — refuted: same-cycle buys were $46.6k (not $67k); post-buy hedged read ~0.58–0.60 < 0.85 → same unwind; P&L delta $0. Observability only.
5. "N-cycle unwind persistence would have kept the hedge" — refuted for any N ≤ 9 / ≤ 13. Ships only as a behaviour-preserving knob (S-8).
6. "Three different liquidity problems" — refuted: one mechanism (strikes far from the money) across all five single-name failures. Fix retained, generalized (S-4).
7. Voided: composite inversion in run-6 (rho +0.24); concentration harm (none); BLK sell-authority cost (n=1, −$571..−$694); MIN_CASH_BUFFER_PCT as the cash-lock lever (14% of the shortfall); technical-signal inversion on the slate (market-wide, neutral control also negative); stop-sweep rankings (inside the resample band); ledger replay as validation (smoke test only); "MU never re-proposed because of the reject" (MU left the slate).

## Run-7 change-set (authored/tested only in the isolated worktree; merges only after the Sep 18 close)

### 4a. Ops / measurement-only — fingerprint-neutral, pooling-safe

**Built and tested:**
1. Self-consistent close-row `day_pl` = equity − previous close/late row; broker figure kept as `broker_day_pl` (`status.py` `EquityHistory.snapshot`).
2. Robinhood OAuth wall-clock deadline (`wait_for_code`; `ROBINHOOD_OAUTH_TIMEOUT_S`). File is `investment_strategy/portfolio/robinhood_auth.py` — cite corrected (critic #9).
3. Flatten script: short option legs first, per-position response printed, leftovers retried ×3.

**In progress (built, being tested):**
4. Alert delivery: per-key exponential backoff + durable spool + one flush summary (`notify.py`).
5. Session-calendar-aware paging window and deadman (cached `state/session_calendar.json`).
6. Alpaca 5xx treated as transient in `_retry_read`; one-line WARNING instead of tracebacks.
7. Liveness stamps inside the book-beta read fan-out (heartbeat no longer withheld on slow data days).
8. Bearish funnel line renders every ELIGIBLE name, literal `-> IGNORED`, plus `ignored=N`.
9. Backfilled bracket exits stamp `fill_price/fill_qty/fill_ts`.
10. `FEEDS` line carries the earnings-calendar fallback (`rh=dead`, yfinance fallback).
11. Ledger `realized_pl` restated at `set_fill` (knob; quote kept as `decision_exit_price`).
12. Checker `--contract v3`: day-0 predecessor row (close/late only; loud line if absent), VOID on a non-close end row, satellite-only N (+ system rows on their own line), "pairs" wording, concentration lines (PF, median, ex-top-3 mean, top-3 share), capture 6/6 with down < 1.0, beta 90% CI at ≥ 20 pairs, worst day on Δequity with broker `day_pl` beside it.
13. `signal_ic.py`: calibrated `p_nov`, `p_boot` only at h=1, trailing-20d residual and neutral-basket controls, `reweight_ok` requires n_dates ≥ 60 AND n_nonoverlap ≥ 12.
14. `claude_daily.sh` ps+lsof cwd liveness gate.

**Still to build (small, ops-only):**
15. Buy-row shadow fields `spy_intraday_ret_at_decision`, `regime_label`, `falling_names`, `would_haircut_usd` — fields on `TradeRecord` in `ledger.py:55-121`, buy helper ~208 (critic #20); re-evaluate only at ≥ 15 independent down dates.
16. Clamp-floor shadow: buy rows stamp `stop_pct_if_floor_6`; stop exits stamp `floor6_would_survive`. NOT a change.
17. Hedge observability: `BOOK BETA:` gains `hedge=PSQ w=… beta=… unhedged=…`; `BOOK BETA (post-exec):` after `_execute_proposals`; `HEDGE COUNTERFACTUAL:` for 5 sessions after an unwind; postmortem `HEDGE WHIPSAW` and `BOOK BETA CAP -> next-cycle arm` counters; config-load INFO line printing `max_book_beta_spy − (target+band)`.
18. Sell rows stamp lot attributes at close (entry_ts, entry_fill, composite, conviction, stop_pct, key_signals); loader dedups phantoms (qty ≤ 0, replaced-order dupes within 4h).
19. Checker informational lines: decision-loss hold-to-stop counterfactual $, rejected-sell (events_only) counterfactual $, trail give-back per exit, concentrated name's to-date P&L, `re-entry after exit` tag count.
20. `tests/test_contract_handles.py`: every watch-item handle greps a literal string in the code.
21. *(removed — the QQQ sub-share residual lives only as the S-7 rider; critic #8: it changes an order size.)*
22. Cosmetics: notional fills print filled/notional; core-trim ledger row carries realized P/L.

**Open, no disposition yet (critic #13):** option `cost_usd` restated at fill (HD debit recorded as the proposal, not the $3,430 fill); postmortem option marks at close; the Sep 10 lesson mis-attributes the legacy SMCI lot.

### 4b. Strategy-affecting items (each changes the fingerprint; ranked)

**S-1 — Slot cap counts only model-opened equity rows** (B-1, confirmed 2/2 + 3 prior votes).
- Spec: `config.py` `RiskLimits` (frozen dataclass) gains `slot_exempt_symbols: tuple[str, ...] = ()`, derived in `load_config` from `CORE_ETF`/`HEDGE_ETF`/`DEFENSIVE_CORE_ETF` (de-duplicated; no new env key; same three keys as `_system_managed_symbols` at `orchestrator.py:3227-3238`; never `put_proxy_etf`). `risk.py:368` excludes exempt symbols; `engine.py:375` "Book FULL (n/cap)" counts the same way; `orchestrator.py:4023` partial branch logs `ROTATION: %s slot + $%.0f folded into this cycle pending fill`. Docs: `MAX_OPEN_POSITIONS` counts only model-opened rows; fix the stale `risk.py:233-241` cite → `risk.py:368-376`.
- Knob / default: none new; exemption derived from the env (on).
- Handle: `SLOT COUNT: 14/15 model rows (exempt: QQQ, PSQ; raw 16)` on every cap evaluation that differs from the raw count.
- Tests: `tests/test_risk.py::test_max_open_positions_ignores_system_managed_rows`, `::test_sep4_rotation_replay_hedge_and_core_held`, `::test_load_config_slot_exempt_from_system_etfs`; `tests/test_decision_prompt.py::test_rotation_block_ignores_system_managed_rows`; existing rotation/cap tests unchanged at default `()`.
- Blast radius / fingerprint: effective model book 13 → 15 names with core + hedge on (17 broker equity rows); position/gross caps and ladder still bound exposure. Changes what is bought → strategy-affecting. Operator decision 2.

**S-2 — Hedge notional divided by the MEASURED hedge-ETF beta** (A6-1, confirmed 2/2).
- Spec: `beta.py` `hedge_target_notional(beta_spy, target, equity, ceiling_pct, hedge_beta=-1.0)` → `gap = max(0, beta_spy − target) × equity / |hedge_beta|`, ceiling unchanged. `_apply_beta_hedge` uses the shrunk measured `book_beta.beta_of(etf, 'SPY')` only when in [−3.0, −0.5], else `cfg.hedge_beta_assumed`; stamps `hedge_beta`, `hedge_beta_source` into `risk_state`. Stale comments at `beta.py:100-105/147-149`, `config.py:637-639` corrected ("PSQ ≈ −1.0 × QQQ; QQQ's SPY-beta, 1.51 shrunk on 60d as of Sep 11, is what drifts").
- Knob / default: `HEDGE_BETA_ASSUMED=-1.0` (fallback only when the ETF's own series cannot be read or reads outside [−3, −0.5]).
- Handle: `AUTO-HEDGE: beta: book spy-beta 1.20 > target 1.00 + 0.15 band — bought $135,000 of PSQ (hedge beta -1.51 measured; at -1.0 would be $204,000)`.
- Tests (`tests/test_run6_beta.py`): divides by hedge beta ((1.30,1.0,1e6,40,−1.5) → 200,000; (1.20,1.0,1.02e6,40,−1.51) → ~135,100); positional 4-arg default −1.0 unchanged ($300k); measured beta used + "measured" logged; fallback on +0.4 / None / AttributeError → "assumed"; ceiling (1.6,1.0,1e6,40,−1.0) → $400k unchanged.
- Blast radius / fingerprint: −33% notional per arm at current QQQ beta; post-arm lands ~1.00 instead of 0.89–0.90; less cash burn; the buy-path cap ratchet loosens. Changes how big the hedge is → strategy-affecting.

**S-3 — Proxy-put builder: OI-aware, monthly-first strike/expiry selection** (A6-5, confirmed 3/3).
- Spec: `build_proxy_put_spread(underlying, spot, min_dte, max_dte, width_pct, min_oi=None, prefer_monthly=True, max_below_spot_pct=2.0, today=None)`: one contract call as today (WARNING if `next_page_token` is set); candidate expiries in [lo,hi] ranked `(is_third_friday desc, |dte−mid| asc)` — **add an `_is_third_friday(expiry)` helper + unit test; it does not exist in `execution/options.py` today (critic #11)**; per expiry long = highest strike in `[spot×(1−max_below), spot]` with `oi ≥ min_oi`, short = highest strike ≤ long×(1−width) with `oi ≥ min_oi` (skip non-qualifying strikes downward); first qualifying pair wins. None-OI legs excluded whenever any contract in the chain carries OI; legacy pick only when the whole chain has no OI. Orchestrator passes `min_oi = limits.min_option_open_interest` (100).
- Knob / default: `PROXY_PUT_PREFER_MONTHLY=on` (off = legacy nearest-mid-DTE pick).
- Handle: `PROXY PUT PICK: IWM 2026-10-16 291/276 (OI 9781/14135, monthly, 45d) over legacy 2026-10-09 291/276 (OI 38)`.
- Tests (`tests/test_put_proxy.py`, fixtures on explicit dates): prefers OI-qualified strike (291 OI 38 / 290 OI 1,800 → 290); prefers monthly when both qualify; excludes None-OI when the chain has OI; legacy pick when no OI data; both legs clear min OI; returns None when no pair qualifies.
- Blast radius / fingerprint: proxy-put path only (3 attempts in run-6); more fills of the same structure and size (0.5% per-underlying cap, OI floor and spread cap untouched). Changes which contracts are bought → strategy-affecting.

**S-4 — Single-name put strike snap: near-the-money, OI-qualified** (A6-6 — contested; objection addressed).
- Spec: `execution/options.py` `snap_legs_to_liquid(underlying, legs, spot, min_oi, max_spread_pct, max_moneyness_pct, strike_tol_pct)`: for single-name put plays (not proxy, not index) clamp each long-put strike into `[spot×(1−m), spot×(1+m)]` — two-sided, ITM and OTM — then move each leg to the nearest OI-qualified strike on the same expiry (else the nearest third-Friday within ±7 d) within `strike_tol_pct`; keep the structure; return None → existing reject path. Called in `orchestrator._handle_option` before `leg_liquidity` when `cfg.option_strike_snap`; original legs stamped in the journal/ledger `risk_note`. Prompt text NOT changed (do-not-do: no put-persuasion).
- Knobs / defaults: `OPTION_STRIKE_SNAP=on`; `OPTION_STRIKE_MAX_MONEYNESS_PCT=10`.
- Handle: `STRIKE SNAP: HD 2026-10-16 400P (OI 2, 25% ITM) -> 320P (OI 1744, 0.3% OTM); unsnapped would be rejected: open interest 2 < 100`.
- Tests (`tests/test_options_snap.py`): clamps deep-ITM put to near-ATM; moves deep-OTM put up to a qualified strike; returns None when none qualifies; skips proxy and index legs; `_handle_option` stamps original legs in `risk_note`; existing OI/spread gate tests unchanged.
- Blast radius / fingerprint: bearish single-name fills become possible (run-6: 1 fill from 6 attempts); size still bounded by the 0.5% per-underlying cap and the OI/spread gates; snapped-leg delta ≈ −0.4/−0.5 instead of the synthetic-short −0.95 asked for (intended per `prompts.py:133`). **Critic #7 gap, not yet dispositioned:** rendering spot on the option candidate line (`engine.py:610-624`) is the input fix — ship as a classified sub-item or record its deferral explicitly. Operator decision 7.

**S-5 — Regime label persistence: tighten fast, loosen slow** (A6-4, confirmed 2/2).
- Spec: `regime.py` `RegimeReader.assess()` keeps `_eff_label`, `_loosen_streak` (in-memory; restart only delays loosening); rank risk-off < neutral < risk-on; `unknown` passes through, does not count toward the streak, preserves `_eff_label`; tighter → adopt now, streak 0; looser → only after `REGIME_LOOSEN_MIN_CYCLES` consecutive looser reads; while holding, `multiplier = min(fresh.multiplier, 0.70 if held == 'neutral' else 0.40)` with `, held neutral (1/2 clean reads)` appended; trend/day_change pass through. Does NOT reuse `state.regime_label` (`orchestrator.py:2824-2829` needs the raw prior label for the risk-off flip trim).
- Knob / default: `REGIME_LOOSEN_MIN_CYCLES=2` (1 = legacy no-memory).
- Handle: `REGIME HOLD: neutral held (1/2 clean reads); fresh read risk-on x1.00 -> applied neutral x0.70`.
- Tests (`tests/test_regime.py`): tightens immediately; loosens only after min cycles (neutral, risk-on, risk-on → neutral, neutral, risk-on); unknown does not reset the streak; hold multiplier is the min; `tests/test_all_weather.py` per-test fresh readers unaffected. The synthesis's "existing `test_regime.py:109-116` tightening test unchanged" cite is wrong (line 109 is `test_assess_is_cached_until_new_cycle`; critic #10) — name the actual test or drop the claim.
- Blast radius / fingerprint: introduces **no new gross rule** (critic #23) — it stabilises the input to the existing ladder (`risk.py:850-870`); the label only ever holds tighter. Sep 10 counterfactual: neutral held from 09:22 through the close; the 10:19 SMCI top-up (−$878) *likely* rejected by the 60% ladder (invested 60.9% at the 10:14 read, 0.9 pp margin); INTC (08:35, completed-bar risk-on) unchanged. The ±0.5% band alternative would have read risk-on all day (opposite outcome). Changes how big buys are on some cycles → strategy-affecting. Operator decision 3.

**S-6 — HELD-line top-up bar in the prompt + float-rounding fix in the gate** (B-2, confirmed 2/2).
- Spec: `risk.py:600`: `bar = round(prev + topup_min_conviction_delta, 4)`; reject on `round(conviction, 4) < bar`; reason prints `bar`. `orchestrator._held_notes` (~2033): when `delta > 0 and prev is not None` append `top-up needs conviction >= 0.71 (+0.05 over the last buy) — else HOLD` (state only, never the ledger fallback, so printed == enforced); a same-day rejected top-up renders as `your last verdict today: BUY conv 0.66 — rejected at the top-up bar`. `engine._risk_contract` stable block gains one line stating the bar and that re-proposing the entry number or +0.01–0.04 over it is rejected every cycle.
- Knob / default: none new; `.env` doc under `TOPUP_MIN_CONVICTION_DELTA` (bar printed on the HELD line, compared at 4 dp).
- Handle / metrics: existing `REJECT buy X: Top-up conviction 0.66 shows no new edge over prior entry 0.66 (bar 0.71)`; pre-registered run-7 metrics: top-up rejections/session (run-6 baseline 4.6; target < 2) AND approved top-ups/session with their conviction distribution (conviction-inflation counter-metric; essential).
- Tests: `tests/test_risk.py::test_topup_evidence_exact_bar_passes` (prev 0.66: 0.71 approved, 0.70 rejected, reason contains "bar 0.71"); `tests/test_orchestrator.py::test_held_notes_state_the_topup_bar`, `::test_held_notes_topup_bar_absent_when_state_pruned`, `::test_held_notes_marks_rejected_topup_verdict`; `tests/test_decision_prompt.py::test_risk_contract_states_topup_bar`. **Run the five existing `test_topup_evidence_*` (`tests/test_risk.py:1007-1044`, `test_replay_jul06.py:231`) in the worktree before claiming "pass unchanged" (critic #15).**
- Blast radius / fingerprint: prompt-only plus a 4-dp rounding at the gate (a proposal exactly at the bar now passes — documented); one cache invalidation at deploy. Prompt text change → strategy-affecting by the v3 classification.

**S-7 — Core defense: cross-day stale-map reset, cancel/sell race, pending_cancel stop filter** (LF-1 + OPS-NEW-1, confirmed 2/2).
- Spec: (1) `alpaca_client.open_stop_sells` skips `_order_status in {pending_cancel, canceled, expired, replaced}`; (2) the falling-names map carries `(map_cycle, map_et_date)`; the breadth leg — and `_market_falling` for the hedge target — ignore a map whose `map_et_date != today` (the deliberate within-day carry stays; `tests/test_breadth_trigger.py:210-265`); (3) trim mechanics, preferred: `ReplaceOrderRequest(qty = stop_qty − trim_qty)` on the resting GTC stop (atomic at the venue), then `reduce_position`; fallback if the replace is refused: cancel, poll `open_stop_sells(etf)` every 0.5 s ≤ 5 s until empty, then reduce; (4) on any trim failure keep `_core_stop_gap=True`, do NOT call `_ensure_core_stop` inline at `:3199` while a cancel may be settling (the 30 s watchdog re-places), WARNING with the broker's `available` qty; (5) rider: fold the sub-share residual into the trim (`sell_qty = int(qty×frac) + frac_part`) so the stop covers 100%.
- Knob / default: none new.
- Handles: `CORE DEFENSE: stale falling map (2026-09-10 14:35, 3 names) ignored at new-day open; would have trimmed 40 QQQ ($29k)`; `CORE DEFENSE: stop 10951625 replaced 163 -> 123 sh, trimming 40`.
- Tests (`tests/test_core_defense.py`): new day ignores yesterday's map (hedge target 1.00); same-day map still carries; `open_stop_sells` filters pending_cancel; `_core_stop_gap` stays armed when the trim fails; trim replaces stop qty then reduces (fake broker records the order); falls back to cancel-poll-reduce; trim includes the sub-share residual (163.4975 × 0.25 → 40.4975 sold).
- Blast radius / fingerprint: prevents wrong-day trims of the core ETF and stopless windows; a real falling day still trims within its own session. Changes when the core is sold → strategy-affecting (conservative). Operator decision 4.

**S-8 — Beta-mode unwind noise guard, behaviour-preserving** (A6-2 confirmed as guard; LF-6 refuted → observability only).
- Spec: dedicated `self._unwind_reads = (cycle_id, n)` counted at most once per decision cycle (the breadth re-arm at `:3123-3148` re-runs `_apply_auto_hedge` in-cycle — `Sep_10` 14:35:37 / 14:38:52); reset on arm, on hold-with-hedge, and on the beta-unavailable early return (`:3411-3415`). Plus the `BOOK BETA (post-exec)` line (4a-17) so a future window can quantify the ordering gap (a second hedge pass only after ≥ 5 in-window post-exec crossings the pre-exec read did not see; run-6 has zero).
- Knob / default: `HEDGE_UNWIND_MIN_CYCLES=1` (= today's behaviour; run-6 counterfactual: any N ≤ 9 would not have kept the Sep 9 hedge).
- Handle: `Auto-hedge: beta: unwind read 1/2 — holding $X PSQ`.
- Tests: min_cycles=2 holds the first read, closes on the second across cycles; streak counts once per cycle on breadth re-arm; streak resets on arm and unavailable; existing `tests/test_run6_beta.py:373-393` one-read unwind unchanged at default 1.
- Blast radius / fingerprint: none at default 1 (knob only). Operator decision 5.

**Fingerprint consequence:** S-1..S-7 each change what is bought/sold/hedged, when, or how big → run-7 has a new fingerprint (merge SHA + new STRATEGY KEYS `HEDGE_BETA_ASSUMED`, `PROXY_PUT_PREFER_MONTHLY`, `OPTION_STRIKE_SNAP`, `OPTION_STRIKE_MAX_MONEYNESS_PCT`, `REGIME_LOOSEN_MIN_CYCLES`, `HEDGE_UNWIND_MIN_CYCLES`); the pooled counter restarts at 0 on run-7 day 1; run-6 (Sep 1–18) is a reference sample reported beside run-7, never summed. Critic #2 (open for v3): as drafted, "fingerprint = merge SHA" means any ops-only merge breaks pooling — define it as (hash of the `investment_strategy/` tree at merge + sorted STRATEGY KEYS + adaptive loops off) with `scripts/ ops/ docs/` commits excluded.

**Explicitly NOT shipped (reason):**
- red-tape haircut/defer — refuted; shadow fields only (4a-15);
- clamp-floor change — do-not-do; shadow column only (4a-16);
- `MAX_BOOK_BETA_SPY` move — refuted as cause; INFO line + counter only (decision 6);
- `MIN_CASH_BUFFER_PCT` — explains 14% of the shortfall; S-2 is the lever;
- `MAX_UNHEDGED_BETA_SPY` — deferred; measure the ratchet via the `unhedged=` line first;
- per-name daily deploy ceiling — no realized harm, n=5;
- sell authority — keep `events_only`; the −$32k pre-run-6 loss engine stays removed;
- composite re-weighting / technical weight — log-only, < 60 dates; IC-2/IC-3 pre-registered for the run-8 close;
- a second hedge pass after execution — refuted (LF-6);
- trim-to-target unwind — n=1 observation → roadmap;
- any cadence/model change.

## Contract status

- **Amendment 3 (Option B) registered** Sat 2026-09-12 ~01:00 CT — after the Sep 11 close row (readings known and committed, `10e2296`), before the Sep 14 session. Stated plainly in the amendment: admissibility rests on rule 1's pre-registered extension clause ("if N < 24 at the Sep 11 close the window extends to Sep 18; it is not judged early"), not on timing (critic #12 applied).
- **Window:** Sep 1 → Fri Sep 18 (14 sessions). **Freeze re-registered to Fri Sep 19**: zero commits to `investment_strategy/` or `.env` on the live branch through Sep 18; no bot restart on new strategy code; the run-7 change-set lives only on `feature/run7-changeset` in a worktree outside the live tree; `scripts/ ops/ docs/ runs/ Todo-4.txt` and log archiving allowed and logged. No counted rule's statistic or threshold changes → the window is not AMENDED by this text.
- **Two verdict commands, both recorded (critic #3 applied):** the contract-pinned `--start 2026-08-31` (Aug 31 `late` row as predecessor, 8 pairs) stays PRIMARY; the `--start 2026-09-01` reading (every in-window report; 7 pairs) is printed as secondary. The verdict, DD, the graded rule-4 statistic, rule 7 and capture qualification are identical under both; only the OLS alpha/t/beta, the pair count and the informational QQQ/IWM captures differ. **Note:** no close-row `--start 2026-08-31` readout exists in the analysis inputs yet — the only 8-pair file (`verifierC5_v2_start2026-08-31_end2026-09-11.txt`: alpha +0.439%/day, t 1.45, beta 0.38, r² 0.11) ends on the Sep 11 intraday row and must not be quoted as the primary reading. Both readings are produced once, on the Sep 18 close row.
- **Validity breaches disclosed and accepted as non-confounding:**
  - (i) telescoping on **7 of 8** close-row pairs (−$776.77 Sep 2, +$1,421.49 Sep 3, +$1,145.78 Sep 4, +$1,417.27 Sep 8, +$195.20 Sep 9, +$562.70 Sep 10, −$912.57 Sep 11; Sep 1 $0.00) — the equity series is one immutable stamp/day and no counted rule reads `day_pl`;
  - (ii) ledger vs fill-restated realized −$72.24 (0.27% of the sum; 7 rows at the pre-fill quote, 7 bracket rows `fill_price=null`; "broker realized" undefined in v2);
  - (iii) two verdict commands (above);
  - (iv) Amendment 2's disclosed "+0.13 over 5 sessions" was an intraday Sep 9 value (close-row 0.17); Sep 7 (+0.06) and Sep 8 (−0.31) reproduce exactly;
  - (v) v2 N counts PSQ `hedge_unwind` and QQQ `core_defense` (satellite-only N=12, mean $2,241.11, t 1.750 vs 1.796 reported alongside).
  - Every other condition re-verified 2026-09-12: `FEEDS: 3/3 healthy news=vader-fallback` every cycle; freeze empty; fresh state (Amendment 1); single instance pid 7536, one `bot.lock`; 45/45 FILLED lines priced; 0 SELL rows with null `realized_pl`/`symbol None`; 8/8 verdict rows `basis='close'`.
- **Outcome at Sep 18:** judged whatever N is; UNDER-FLOOR if N < 24 (projection, unverified: 1.75 closes/session → N ≈ 23; 2.8/session since the first close → ≈ 28). Capture becomes judgable with one more SPY up-day. Option A (close at N=14 → PENDING-TERMINATED + CONFOUNDED-by-letter, never pooled) is recorded in the contract as the untaken alternative.
- **Contract v3 draft:** scratchpad `analysis/agentC_CONTRACT_V3_DRAFT.md` + synthesis §5 → to be pre-registered as `runs/pre-final-test-run-7/EVAL_CONTRACT.md` before day 1 (Mon Sep 21; day 0 = Fri Sep 18 after the bell on a fresh account with a `basis='late'` row). Open critic corrections to fold in before registration: #2 fingerprint definition; #4 a rule that never reaches its qualification floor is reported N/A and does not block PASS; #5 satellite exclusion set pinned to literals that exist (`hedge_unwind, core_defense, regime_trim, defensive_rotate, correction`; `core_fill` is an entry mark, `core_trim`/`flatten` do not exist) with `tests/test_contract_handles.py` asserting each; #22 switch sequencing (stamp run-6 close row → run checker → switch account → restart → verify `basis: late`).

## Operator decisions (7) — defaults in bold

1. **Amendment 3: Option B — TAKEN** (registered Sep 12). Freeze to Sep 19; verdict on the Sep 18 close; run-7 day 0 = Fri Sep 18 after the bell, day 1 = Mon Sep 21. Option A remains in the contract only as the recorded, untaken alternative.
2. `MAX_OPEN_POSITIONS` semantics after S-1: **keep 15 = 15 model names** (MAX_POSITION_PCT 12 / gross caps / ladder still bound exposure) vs lower to 13 to preserve today's effective book.
3. Regime stabiliser (S-5): **persistence, tighten-fast/loosen-slow** (only ever tightens; Sep 10 would have held neutral) vs a ±0.5% band (Sep 10 would have read risk-on all day).
4. Core-defense trim mechanics (S-7): **replace-qty-down on the resting GTC stop** (the "replace, never cancel-then-resell" rule) vs cancel → poll ≤ 5 s → reduce.
5. `HEDGE_UNWIND_MIN_CYCLES`: **1** (behaviour-preserving; n=0 evidence for 2) vs 2.
6. `MAX_BOOK_BETA_SPY`: **keep 1.2** (refuted as cause; 1.10 shrinks high-beta buys — SPCX $55k → ~$38k, SMCI $27k → ~$7k — on n=3 arms) vs 1.10.
7. S-4 strike snap: **ship in run-7** (one deterministic mechanism, 5 of 5 attempts) vs defer to run-8 to keep the options change-set to S-3 only.

## Known data caveats in this archive

- **Intraday-vs-close leakage in the analyst texts.** The autopsy's Sep 11 row (1,034,842.49, +0.640%, SPY +1.057%) is intraday; its PSQ Sep 11 mark (25.865) and the `to_now` column in (c3) are intraday; the option marks (+$545 / +$2,457) are the context pack's intraday live-position snapshot; Amendment 2's "+0.13" was an intraday Sep 9 value; `verifierC5_v2_start2026-08-31_end2026-09-11.txt` ends on the intraday row; the agent6 file's Sep 11 QQQ/IWM closes (716.51 / 289.40) were an intraday pull (the csv closes are 714.88 / 288.89). The scratch `equity_run6.jsonl` and `psq.csv` have since been refreshed with the Sep 11 close row (1,038,988.75 / 25.93). Only `CHECKER_2026-09-11_close.txt`, the daily table above and the Sep 11 close figures are close-row readings.
- **Pooled history carries phantoms.** 197 ledger sells → 158 FIFO-joined trips; 39 phantom/duplicate rows (qty ≤ 0, replaced-order dupes), all pre-run-5; the run-6 rows are clean. The loader dedup ships in 4a-18.
- **Ledger `realized_pl` is realized-at-quote on 7 of 14 closed rows** (submission-time quote, not the fill; net −$72.24 vs fill-restated; PSQ +$41.76 ledgered vs −$61.07 at the 25.84 fill). Restated at `set_fill` in 4a-11.
- **`fill_price` is null on the 7 exchange-backfilled bracket rows** (`bf`=Y above); their `exit_ts` is the ledger row time, later than the real fill. Stamped by 4a-9.
- **The hedge whipsaw dollar figure is definition-dependent** ($2,571 fill-to-fill / $2,754 original-lot-held Sep 8 → Sep 11 close at 25.93 / $2,076 from-the-exit vs new-lot-only) — always name the definition; the critic's refuter votes quoted $2,595 and $1,542 under yet other endpoints/marks.
- **v2 checker conventions** admit intraday rows as sessions, drop day 1 under `--start 2026-09-01`, and count the two system rows in N; v3 fixes all three. The "beat SPY 6/8 / green on 3/5 red days" tallies are on close rows and hold under both commands.
- `runs/pre-final-test-run-6/state/` is the Sep 12 00:35 snapshot (equity_history, trades, risk_state, decisions, lessons); it will be re-snapshotted after the Sep 18 close.

## FINAL (to be appended after the Sep 18 close row)

_Pending. Append: the checker readout on the Sep 18 `basis='close'` row under both commands; final equity / DD / N / verdict (PASS, PENDING UNDER-FLOOR, FAIL, or CONFOUNDED); Sep 14–18 rows added to the daily table; closed trips added; grade confirmed or revised._
