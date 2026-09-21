# A+ roadmap: what run-6 proved, what it did not, and the steps from here

Written 2026-09-21 (Mon) ~10:30 CT. Docs only; no code, `.env` or position was touched.

**Evidence labels.** `[V]` = verified: reproduced by an adversarial refuter, by the Sep 17/18 fallback
sessions, or by a direct log/ledger read in this session. `[A]` = raised by one audit analyst on
Sep 16 and NOT independently checked (the audit's verify phase died on the usage cap twice; an empty
`refuted` list there means "never checked", not "survived"). Treat every `[A]` as a hypothesis to
reproduce before building on it.

## 1. Bottom line

1. **Not A+. Overall B, down from the B+ provisional of Sep 11.** The system side (risk layer, hedge,
   capture, ops discipline) is A-/B+. The strategy side (does entry selection have an edge) is unproven
   and, on the closed-trip distribution, currently negative outside three trades.
2. **A+ is a measured outcome, not a design property.** The A+ bars are statistics over >= 21 sessions.
   Design can remove every verified mechanism that caused a missed bar. It cannot guarantee the edge.
3. **Run-7 has not started.** On 2026-09-21 the live tree is still run-6 code (HEAD `1a82724`, no run-7
   files on disk), account `PA3B09IK4MGS`, bot restarted 09:17 CT today (pid 97525). The Sep 18 day-0
   switch did not happen.
4. **The sample-size problem is the binding constraint on "profitable".** See section 5.

## 2. Where run-6 ended (Sep 1 -> Sep 18, 14 sessions)

Source: `CHECKER_2026-09-18_close.txt` as recorded by the Sep 18 fallback session (PR #59 branch) `[V]`.

| Measure | Result | A+ bar | Status |
|---|---|---|---|
| Book vs SPY | +3.22% vs -0.70% at beta ~1 | - | good |
| Capture (first time counted) | up 129.8% / down 30.1% | up >= 0.9, down <= 0.8 | pass |
| Max drawdown | -3.29% | >= -3% | miss |
| Worst session | -2.23% (Sep 14, SPY -0.45%) | >= -1.5% | miss |
| Closed trips | N=31, +$13,837, WR 38.7% | - | - |
| Expectancy t | 0.736 vs 1.697 | significant at 95% | miss |
| Top-3 trips vs the other 28 | +$26,638 vs -$12,801 | ex-top-3 mean > 0 | miss |
| Contract verdict | NO-GO on rule 7 only (one INTC row, 0.49x stop) | - | wording defect `[V]` |

Satellite-only through Sep 15 `[V, 2 of 3 refuters; corrections applied]`: N=23, WR 30.4%, PF 1.385,
ex-top-3 mean -$900. Every fresh position opened on or after Sep 8 that closed by Sep 15 lost (9 of 9,
-$16,339, lot-aware). Every satellite exit after the Sep 9 NVDA trail lost (16 of 16 through Sep 16).
Re-entries after an exit: 0 of 4. Entry composite and conviction do not rank outcomes (tie-corrected
Spearman -0.25 and -0.12 at n=23; losers' mean composite 1.44 vs winners' 1.16). Small n: this says
"no evidence the score separates winners", not "the score is inverted".

## 3. Scenario coverage

| # | Scenario | What run-6 showed | Run-7 change-set | Remaining gap |
|---|---|---|---|---|
| 1 | Rising tape | up-capture 129.8% `[V]` | unchanged | none known |
| 2 | Orderly decline | down-capture 30.1%; hedge arms ~50% oversized `[V]` | S-2 sizes by measured hedge beta | covered |
| 3 | Overnight gap cluster | Sep 14: 8 names, ~33% of equity, gapped -5..-10% on a -0.7% SPY open; loss was booked before the first cycle `[A numbers, V sells]` | nothing | no correlated-cluster cap, no gap-risk sizing |
| 4 | Hedge during a stop cascade | Sep 14 09:22 CT: stops cut invested %, hedged beta read 0.54 < 0.65, hedge fully closed into the down tape `[V line; A mechanism]` | S-8 default 1 = unchanged | unwind is judged on HEDGED beta; it closes instead of trimming |
| 5 | Falling-tape target | target steps 1.00 -> 0.80 (0.20) but band is 0.15 `[V arithmetic]` | pinned as is | step > band means arm/unwind zones overlap the moment the read flips |
| 6 | Fully invested melt-up | Sep 21: invested 98%, beta 1.20, "want $213,703 more PSQ but only $0 spendable" `[V]` | nothing | hedge has no reserved funding |
| 7 | Choppy regime | Sep 10 flapped 5x; Sep 14 read risk-on x1.00 for four cycles with the book -1.9% intraday `[V]` | S-5 loosen-slow (2 cycles) | breadth still computed on a partial bar; 2 cycles does not stop a half-day excursion |
| 8 | Single-name breakdown (puts) | model declined ~90% of gate-ELIGIBLE name-cycles `[A]`; hallucinated/illiquid contracts die as "no debit" and skip the proxy fallback `[A]`; Sep 17: 25% of eligible names got no verdict at all `[V]` | S-3/S-4 fix strikes the model does propose | the funnel's binding constraint is the model's decline rate, which no item touches |
| 9 | Options bookkeeping | HBAN put rendered to the model as "HBAN long", next put declined for "contradicting the long" `[V]`; illiquid put unmarkable, 72 watchdog warnings in 40 min, premium stop blind `[V]` | nothing | HELD-line rendering; entry spread check vs watchdog mark cap |
| 10 | Core ETF execution | core-fill cancels the GTC stop then buys; Alpaca wash-rejected the buy; QQQ ($137.6k) stopless 14:40 CT Sep 15 -> 08:35 CT Sep 16 `[V]`; watchdog does not backstop the core `[A]` | S-7 fixes the trim side only | core-fill buy path; in-process backstop |
| 11 | Exit blocked by own order | Sep 16 RIG decision sell failed against its resting take-profit, CRITICAL paged, retried OK in 14 s `[V]` | nothing | cancel/replace-then-close as one step |
| 12 | Top-up discipline | conviction memory pruned after 7 days, gate fails open, $65.6k of top-ups passed unjudged `[V code, 1 refuter]` | S-6 keeps it | anchor the bar on the ledger's last buy while the position is open |
| 13 | Host / ops | battery CRITICALs Sep 14, 18, 21; two dark gaps (18 and 21 min) Sep 21 `[V]`; headless fallback raced a live primary Sep 15 `[V]`; yfinance outage -> regime blind x0.50 Sep 21 `[V]`; git dead since the OS upgrade (Xcode license) `[V]` | C3 ps gate, A1 backoff | bot still lives on a laptop; regime has one data source |
| 14 | Measurement / contract | rule 7 = v3 rule 8 fails sanctioned sells `[V]`; two cost-basis conventions on one symbol `[A]`; "stopless window" and "cadence" bars have no definition or tool `[A]`; v3 verdict command has placeholders `[A]` | 4a items fix fill_price + telescoping | contract text must be fixed BEFORE pre-registration |
| 15 | Entry selection (the edge) | section 2 | nothing, by design | the dominant gap; see Step 4 |

## 4. Steps

**Step 0 - today, operator.** (a) Keep the host on AC; better, move the bot to the always-on host in
`ops/`. A sleeping laptop is the one failure no code fixes. (b) Run `sudo xcodebuild -license` so git
works again; nothing can be committed, pulled or switched until then. (c) Note the book is 98% invested
at beta 1.20 with an unfunded hedge; that is the running run-6 code behaving as designed, flagged here,
not changed.

**Step 1 - contract v3, docs only, before pre-registration.** Rule 8 counts only UNSANCTIONED decision
sells (no `SELL AUTHORITY ... allowed on event(s)` line). Define "stopless window" and the cadence marker.
Pin the literal verdict command with `--system-symbols`. Pin "fill-restated" as FIFO by ledger lots.
Add the bearish-funnel denominator as a recorded metric: eligible, proposed, declined by reason class,
ignored. New dates: day 0 = the evening of the switch, day 1 = next session, 21 sessions.

**Step 2 - start run-7 as built, on a fresh account.** The change-set is reviewed (1,517 tests) and fixes
verified defects 2, 7 (part), 10 (trim side), 12 (rounding), 14 (part). Do not add strategy items to it
now: more simultaneous changes means no attribution. Treat run-7 as the second sample of the SAME entry
engine with cleaner plumbing. It can confirm or kill the run-6 tail; it cannot reach A+ on the strategy bar
unless that tail was luck. If day 1 = Tue Sep 22: session 21 = Tue Oct 20, extension to Oct 27.

**Step 3 - build the A+ change-set for run-8 in an isolated worktree while run-7 runs.** One commit train,
each item with a failing test first, an env knob, a log handle and a pre-registered metric:

- P1 Hedge integrity: judge unwind on UNHEDGED beta and trim to target instead of closing (4); band >= the
  falling step or a one-way ratchet (5); reserve hedge funding so a full book can still arm (6).
- P2 Gap and cluster risk: correlated-bucket cap (~15% of equity per bucket) and a gap-history sizing
  haircut (3). Reproduce the `[A]` numbers first; "concentration harm" was voided once already in run-6.
- P3 Core execution: settle-poll between stop cancel and core-fill buy; register the core with the
  watchdog's hard-exit path (10). One-step cancel-and-close for exits blocked by a resting order (11).
- P4 Bearish sleeve: render option positions as options on the HELD line (9); treat "contract unknown /
  no debit" as a liquidity outcome so the proxy fallback runs (8); show spot and listed expiries on the
  candidate line so strikes are followable; align the entry spread check with the watchdog's mark cap.
  Then measure the decline rate before touching the prompt.
- P5 Regime: breadth confirm on the completed prior bar; cap the multiplier at neutral while a book-level
  FALLING-TAPE read is live (7); second data source for SPY/VIX so one vendor outage is not a blind regime (13).
- P6 Measurement: stamp the sell sanction and rationale on ledger rows; one cost basis per symbol; scripted
  ops-bar grading (14); top-up bar anchored on the ledger (12).

**Step 4 - the edge, as research with kill criteria, not as a patch.** Offline first, on the pooled ledger
plus `signal_history` with `scripts/signal_ic.py`: forward returns at 1, 3, 5 days by signal family and by
cohort. Hypotheses to test, none to ship untested: re-entry after an exit (0 of 4); later-wave entries
(9 of 9 lost) vs first-wave; cited families with negative per-trip expectancy (technical, insider,
options_chain in the report-only gate); conviction does not rank, so size flat until it does. Any rule that
survives ships in run-8 behind a knob with a pre-registered metric and a stated result that would remove it.

**Step 5 - calendar to a live pilot.** The contract needs two consecutive same-fingerprint PASS windows and
pooled N >= 60. Run-8 changes the fingerprint, so the count restarts there: run-8 Oct 21 -> Nov 18, run-9
-> about Dec 17. Earliest honest live-pilot date is mid-December, later on any FAIL.

## 5. What cannot be promised

Per-trip dispersion is about $3,400 to $3,800. At the observed mean ($446 final, $376 satellite) a 95%
one-sided test needs roughly **155 to 275 closed trips**; a 21-session window yields 25 to 45. At pooled
N=60 the mean must be about **$730 per trip** to pass, versus $446 observed with the top-3 included.
So either the edge is materially larger than run-6 showed, or proof takes several pooled windows. More,
smaller, less-correlated positions raise N per window and shrink the damage of any one name, which helps
the drawdown bars directly and the t-stat by the square root of the breadth gain. No change makes a
losing entry engine pass; only Step 4 addresses that, and it may come back negative.
