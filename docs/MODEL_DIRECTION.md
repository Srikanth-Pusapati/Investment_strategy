# Model direction — where the edge is, where it is not, and the plan to a profitable system

Written Tue 2026-10-27 (CT), the day after run-7 closed **FAIL** and the bot was stopped; the account was
flattened at the 09:31 ET open (`logs/flatten_only.log`: equity $977,048.52, 0 positions, 0 orders).
Audience: the owner, and every later session that has to decide what to build next. This is a direction
document: it says what the seven trial windows have shown, what that implies, and the plan. It does not
change any code, knob or contract by itself; every change it proposes goes through the normal path
(failing test → knob → handle → pre-registered metric → one change-set per window).

Evidence labels, as in every run document: `[V]` reproduced from the ledger, logs or the checker (usually
twice); `[S]` suggestive — one run or a small n; `[H]` hypothesis, not yet tested. Sources are listed at
the end; the numbers here are copied from them, not re-estimated.

---

## 1. Bottom line

1. **The system works; the strategy does not yet.** Risk layer, exits, hedge, measurement and ops grade
   A−/B+ (zero halts and zero crashes in runs 6–7, drawdown never worse than −3.3% since run-4, the
   verdict machinery is clean). Entry selection — the model picking names from the screener slate —
   grades D: seven windows, and in none of them has the satellite sleeve shown an edge that survives
   removing its three best trades `[V]`.
2. **Every completed window under-performed SPY over its own span except run-6, and run-6's +3.22% was
   three trips** (top-3 +$26,638 vs the other 28 at −$12,801) `[V]`. Run-7 then printed the clearest
   negative yet: 30 trips, 20% win rate, −$855 per trip, every ranking signal non-ranking `[V]`.
3. **What makes money is mechanical and systematic:** profit exits (trail / take), the index core, the
   beta hedge and defense (run-6 down-capture 30% at up-capture 130%), and — in run-7, for the first
   time — the credit/index put sleeve (+$3,801 on 6 trips) `[V]`. What loses money is discretionary:
   model decision-sells before `events_only` (−$32,303 pooled), bullish option structures (0 wins in
   every run that tried bull-call spreads), top-ups on restated conviction, and entries in the first
   15 minutes `[V]`.
4. **Direction.** Stop treating the model as a stock picker whose edge we hope to prove, and build the
   book as sleeves that each earn their allocation: an index core that carries the book's beta, a
   systematic defense overlay, a small defined-risk put sleeve, a satellite sleeve sized to its
   demonstrated edge (which may be zero), and a research sleeve that decides what is allowed to reach the
   prompt. The model moves from "picker and sizer" to "analyst, event specialist and veto", where its
   judgments can be scored.
5. **Run-8 as built is still the right next sample** — it fixes the verified mechanisms and is the second
   window the kill criterion needs — with one addition recommended below (a core-plus-defense control
   arm). The live-money pilot stays where the contract puts it: after two consecutive PASS windows on one
   fingerprint and pooled N ≥ 60, i.e. **January 2027 at the earliest**, on a shape that passed.

---

## 2. Where we stand — the scorecard

| Run | Window | Account result vs SPY | Satellite trips | WR | Exp/trip | PF | Verdict / grade |
|---|---|---|---|---|---|---|---|
| 1–3 (pre-contract) | Jun 26 → Jul 27 | −0.22% / −5.72% / −0.77% vs +1.10% / −2.12% / +0.02% | 35 + 52 (archives) | 51% / 35% | −$12 / −$116 | 0.64 / 0.16 | no contract; −$133/trade July baseline |
| 4 | Jul 27 → Aug 21 | +0.67% vs +3.60% | 42 | 31% | +$167 | 1.10 | reference sample; **B−** (capture 1.19 up / 2.31 down FAIL) |
| 5 | Aug 24 → Aug 31 | +0.79% vs +0.02% (2 days) | 11 | 55% | −$437 | 0.49 | **CONFOUNDED** (measurement basis, dirty state) → run-6 |
| 6 | Aug 31 → Sep 18 | **+3.22% vs −0.70%** | 31 | 38.7% | +$446 | — | NO-GO on a defective rule 7; every counted rule passes; **B** (B+ provisional at day 8) |
| 7 | Sep 22 → Oct 20 | **−2.32% vs −0.58%** | 30 | **20%** | **−$855** | 0.39 | **FAIL** — 5 of 7 counted rules (expectancy, alpha, β ex-ante, β OLS, capture) |

What the two contract windows agree on `[V]`:

| Question | Run-6 | Run-7 |
|---|---|---|
| Mean ex-top-3 satellite trip | −$457 (28 trips) | −$1,422 (27 trips) |
| Do composite / conviction rank outcomes? | No (Spearman −0.25 / −0.12, n=23; losers' mean composite 1.44 vs winners' 1.16) | No (every composite and conviction bucket negative; 0% WR below conviction 0.62) |
| Later-wave / top-up entries | 9 of 9 fresh entries after Sep 8 lost; re-entries 0 of 4 | 3-lot INTC −$7,474 = a quarter of the window's loss |
| Mechanical profit exits | 10 of 14 trips, 98% of P&L | take / bracket_take / trail: 6 trips, +$16,865, 100% WR |
| Max drawdown | −3.29% | −3.20% |
| Hedge / defense | down-capture 30.1%, green on 3 of 5 SPY red days | β drifted to 0.27 (cash 33% after Oct 8); up-capture −2.5% |

The statistical reality behind every grade `[V]`: per-trip dispersion is $3,400–3,800. Proving the
run-6 mean (+$446) at 95% one-sided needs roughly **155–275 closed trips**; a 21-session window yields
25–45. At pooled N = 60 the mean must be about **+$730 per trip** to pass. No design change makes a
losing entry engine pass; only a real edge, or much lower dispersion, shortens the proof.

### 2b. Independent cross-run ledger check (Oct 27, read-only recompute)

One stdlib script ([`scripts/ledger_crossrun.py`](../scripts/ledger_crossrun.py), output
[`runs/LEDGER_CROSSRUN_2026-10-27.md`](../runs/LEDGER_CROSSRUN_2026-10-27.md)) over the four archived
ledgers, with the checker's trip definition (a sell row with non-null `realized_pl`; satellite = exit
reason outside the system set). It is **not** the verdict: run-6 runs to Sep 21 and run-7 to Oct 26 here
(post-window trips included), so N differs from the contract tables above. The sign pattern is the point.

| Run | Satellite N | WR | Mean | Median | 95% one-sided LB | Mean ex-top-3 (WR) | Top-3 ÷ net |
|---|---|---|---|---|---|---|---|
| 4 | 42 | 31% | +$167 | −$720 | −$1,194 | −$714 (26%) | 4.96× |
| 5 | 8 | 50% | −$924 | −$524 | −$2,247 | −$1,861 (20%) | net ≤ 0 |
| 6 | 27 | 33% | +$940 | −$773 | −$480 | −$371 (25%) | 1.35× |
| 7 | 33 | 21% | −$842 | −$732 | −$1,543 | −$1,352 (13%) | net ≤ 0 |
| **Pooled 5–7** | **68** | **29%** | **−$144** | **−$752** | **−$820** | **−$678 (26%)** | net ≤ 0 |

What holds in **every** run (4 of 4), ranked by consistency:

1. The ex-top-3 satellite mean is negative, the median trip loses $500–800, the win rate never exceeds
   50%, and no run clears the 95% bar — including run-6, whose satellite total was +$25,373.
2. The three best trips exceed each run's entire net satellite P&L (run-4 4.96×, run-6 1.35×; runs 5
   and 7 are negative with the top-3 in). The sleeve's result is three names per window.
3. Trips closed within 3 days of the first fill lose in every run (pooled 5–7: N=18, WR 28%, mean −$918);
   holds ≥ 3 days were +$44,642 (run-4), +$26,740 (run-6), −$1,801 (run-5), −$18,237 (run-7). This is
   an outcome of the exit stack, not a rule to trade on (the Aug 25 finding reproduces).
4. `decision` exits carry a negative mean in every run (pooled 5–7: N=25, mean −$841, Σ −$21,028).
   Expected for loss-cuts; whether they beat holding to the stop is the counterfactual question (Aug 25:
   `name_falling` cuts were right, thesis-decay / rotation cuts were wrong).
5. Mechanical exits alone were net positive pooled: 29 stops at −$1,855 vs 13 take/trail at +$5,033
   (Σ +$11,637; break-even hit rate 27% vs 29% realised). Since run-5 stops outnumber take/trail ≥ 2:1.
   The geometry is right; the hit rate is at break-even and the discretionary exits turn the sleeve
   negative.
6. Trips citing `options_chain` have a positive mean in every run (pooled 5–7: N=24, WR 46%, mean
   +$1,223) — the only family with a 4/4 sign; cells are small (run-5 N=2). A candidate for R-1, not a
   rule. Trips citing `news` had zero wins in runs 5–7 (N=10) against 75% in run-4 — the sign flips.
7. Conviction is effectively constant (67 of 68 pooled satellite trips sit in 0.60–0.75) and composite
   does not rank (≥ 2.0: WR 20%, −$859 in run-4; 1.5–2.0: WR 0%, −$1,717 in run-7).
8. Bearish option trips were net positive in both runs that had them (run-6 +$5,135, N=1; run-7
   N=7, WR 43%, +$1,331); run-4's bullish calls and call spreads went 1W/4L, Σ −$22,149.
9. Peak-to-trough drawdown stayed within 3.3% in every run (−2.98%, −1.96%, −3.29%, −3.20%).

Caveats that matter: entry attributes for runs 4–6 are FIFO-reconstructed (validated on run-7's stamped
rows, 0 of 34 mismatches); most sub-buckets have N < 10; the ledger's realized P&L and the equity file's
`realized_pl` disagree by $0.4k–2.5k per run (run-7: −$29,580 vs −$32,079) and run-4 carries two Aug 17
`external` option rows with P&L 0.0 that were a real loss — both are measurement items (TODO R-7).

---

## 3. What the evidence says

### 3a. Proven assets — protect these, do not re-litigate them

- **The invariant "the model proposes, the code disposes."** Every order passes `RiskManager`; every
  rejection is greppable; the kill switch, flock, deadman and equity-floor latch have all fired for real
  and worked `[V]`.
- **Mechanical exits.** Pooled 158 trips: mechanical profit exits +$2,243/trip (CI [1,227, 3,369]),
  mechanical stops −$2,511 (CI [−4,029, −1,332]), discretionary decision exits −$434 (CI [−702, −179]).
  Jun 26 → Aug 25: trail/take +$77,026 on 44 trips at 86% WR vs model decision-sells −$26,144 on 48 at
  15% WR `[V]`. The Jul 25 R-scaled trail is the single clearest positive change in the history.
- **`LLM_SELL_AUTHORITY=events_only`** removed the loss engine: 58 decision exits / 45 losses / −$32,303
  pooled before run-6; 0 decision-sell losses in run-6; run-7's 13 decision-sell losses were all
  `name_falling`-sanctioned and the rule-8 audit passed `[V]`.
- **Beta hedge and defense.** Run-6: PSQ armed on the Sep 3 read, down-capture 30.1% at up-capture
  129.8%; the one window the hedge was on for most of the time is the one window that beat SPY `[V]`.
  Known defects are mechanical and listed (sizer ignores PSQ's own beta → arms ~50% oversized; unwind
  judged on hedged beta; no reserved funding when 98% invested) `[V]`.
- **Measurement and process.** Pre-registered contracts since run-5, a checker whose rules are fixed
  before the window, telescoping clean in run-7 (max gap $0.00), every watch-item handle pinned by a
  test, one diagnostic + one change-set per FAIL `[V]`.

### 3b. Proven liabilities — the mechanisms run-8 removes

- **Open-print entries:** 0 for 12 in run-7 (−$17,135, mean −$1,428) vs 38% WR for entries 15–60 min
  after the open; the Jul 27 $300k QQQ open-print sweep was the earlier instance `[V]` → B-3.
- **Top-ups on restated conviction and single-name concentration:** DBRG 0.60 → 0.77 across four lots
  on a falling composite, 18% of equity above the 15% core; INTC three lots −$7,474 `[V]` → B-1, B-2.
- **Options as a bullish vehicle:** bull-call spreads 0 wins in every run that tried them (run-4: 0 for
  4, −$33.8k incl. junk-quote premium stops); the earnings/overextension gate was bypassed by design via
  the option fallback `[V]`. Single-name bullish debits are switched off in run-7/8 config.
- **Option protection blind on wide quotes:** HYG 78/79P unmarkable 13 sessions, 977–3,055 skipped
  premium-stop ticks per day; IWM stop fired 113× pre-market into Alpaca's RTH-only rule `[V]` → B-7c, N-1.
- **Beta drift as a side-effect of satellite count:** run-7 ran β 0.27 with a third in cash after the
  Oct 8 purge; run-4's ex-ante beta ranged 0.41 → 1.87 inside one window `[V]`. The core at 15% cannot
  hold the book at β 1 when satellites come and go — a structural point, see §5.
- **Host and feeds:** 32 battery CRITICALs and 9 in-hours dark gaps in run-7; Robinhood auth dead from
  Oct 13; EDGAR stalls `[V]`. None is strategy, all cost sessions and attention.

### 3c. Unproven — the questions the plan is built to answer

- **Does any signal family have forward-return IC?** The Aug 25 study: composite IC ≈ 0 at every
  horizon; slate win-rate vs SPY 47–50% (the pool the model picks from has no measurable edge) `[V]`;
  news/VADER +0.09/+0.12 at 1–3 d `[S]`; Robinhood "daily movers" −100 to −150 bps at 5 d — a contrarian
  source `[S]`; `uptrend`-cited entries in run-7: 13 trips, −$24,305, 8% WR `[V]`. `signal_history` keeps
  14 days, so the question cannot be answered on more than ~12 dates at a time.
- **Does the model's conviction carry information?** 81–82% of convictions sit in [0.60, 0.65) `[V]`;
  no calibration has ever been computed. Until it is, sizing on conviction is sizing on noise.
- **Does the bearish / credit-put sleeve pay over a full cycle?** First fills Sep 25; run-7 +$3,801 on 6
  trips with the protection blind `[S]`. The funnel still drops 1–30 eligible names per session as
  IGNORED `[V]`.
- **Is the model better used as an event reader than as a picker?** The nine `name_falling` cuts in the
  Aug 25 study were right (median post-exit 5-day return −10%); thesis-decay and rotation cuts were wrong
  (+2.3% / +4.6% after exit) `[V]`. Suggestive that the model reads *events* better than it forecasts
  *returns* `[S]`.

---

## 4. Why the current approach cannot reach "profitable" on its own

1. **No forecast target, no label, no learning loop.** The decision schema asks the model to pick from a
   slate; it never states what return over what horizon counts as right. Nothing downstream can be
   validated against a label, so prompt and signal changes are judged on trip P&L alone — a signal with
   $3,500 of noise per observation.
2. **Signals reach the prompt before they are validated.** Eleven providers feed the bundle; the one
   forward-return study found none with a reliable IC and one that is inverted. Feeding the model more
   inputs of unknown sign cannot help it.
3. **The model holds the authorities that lose and the code holds the ones that earn.** Entry selection
   and sizing (conviction) are the model's; exits, hedge and core are the code's. The P&L split in §3
   follows that line exactly.
4. **Too many simultaneous changes for attribution.** Eight to nine strategy items per window (run-7:
   S-1..S-8 + A-1..A-9; run-8: B-1..B-8 + N-1) means a PASS or FAIL cannot be assigned to an item. The
   contract's "one change-set per window" rule is necessary but not sufficient.
5. **The arithmetic.** At the observed dispersion the satellite sleeve needs 150–275 trips to prove a
   positive mean. That is 5–8 windows of the same fingerprint, or a sleeve with far lower per-trip
   variance (more, smaller, less correlated positions). Variance reduction is the only lever that
   shortens the proof; mean-seeking by adding rules does not.

---

## 5. Vision — what we are building (6–9 months)

> An all-weather, measured book: an index core that carries the book's beta, a systematic defense
> overlay, a small defined-risk put sleeve, and a satellite sleeve whose size is a function of its
> demonstrated, out-of-sample expectancy — zero if it has none. The model is the analyst, the event
> specialist and the gatekeeper's advisor; it never sizes. Every sleeve has its own pre-registered
> metric, kill criterion and allocation rule. Live money follows two consecutive PASS windows on one
> fingerprint, in a shape that passed.

Principles that follow from §3–4:

1. **The model proposes, the code disposes** — unchanged, extended to allocation: sleeve weights are set
   by evidence, not by the model and not by hand mid-window.
2. **Allocation follows evidence.** A sleeve's budget rises only after a PASS on its own metric and falls
   on a FAIL. Nothing is grandfathered.
3. **Variance before mean.** More, smaller, less-correlated satellite positions with flat sizing until
   conviction is shown to be calibrated. This helps the drawdown bars directly and the t-stat by the
   square root of the breadth gain.
4. **Measure first, prompt later.** No signal reaches the prompt before it shows forward IC offline; no
   prompt change ships without a score on the offline decision-eval set (§6, R-3).
5. **One strategy change-set per window, at most four strategy items**, each with a removal metric.
   Ops/measurement items are unlimited (fingerprint-neutral).
6. **Protect what works.** Mechanical exits, `events_only` sells, the beta hedge and the core are not
   subject to discretionary override; changes to them are mechanical fixes with tests, never prompt text.

### Target shape, by sleeve

| Sleeve | Role | Today (run-7) | Direction | Metric / kill |
|---|---|---|---|---|
| **Core** (QQQ, later QQQ+SPY) | carries the book's beta to the 1.0 target | 15% core; satellites carry most beta, so β drifts with satellite count (0.27 in run-7) | core becomes the **beta carrier**: beta-targeting fills from the core, satellites are an overlay; core 40–60% depending on the satellite budget | ex-ante β within 1.0 ± 0.2 on ≥ 90% of close rows (contract rule 5a) |
| **Defense** (PSQ hedge, core defense, regime, index puts) | cut down-capture without giving up up-capture | proven in run-6 (30% / 130%); P1 defects known | fix P1 (unwind on *unhedged* beta, trim-to-target, band ≥ step, reserved funding); then measure the asymmetry on ≥ 2 windows | down-capture ≤ 0.8 AND up-capture ≥ 0.9 with ≥ 6/6 pairs; whipsaw cost per arm |
| **Credit / index puts** (HYG, LQD, IWM, SPY/QQQ) | convex, defined-risk cushion; the only positive sleeve in run-7 | $14.6k premium; protection blind on wide quotes | keep small (≤ 1–2% of equity in premium); require markable contracts (B-7, OI-scaled lots, RTH exits); score the funnel | ≥ 5 closed trips per window summing ≥ 0; 0 IGNORED; 0 blind-stop sessions |
| **Satellite** (model-picked names) | the research sleeve until it earns more | 20+ names, 12–18% single-name cap, carries the window's result | **size to evidence**: if the pooled ex-top-3 mean is ≤ 0 after run-8, cap the sleeve (≤ 25% of equity, ≤ 8 names, flat sizing, IC-gated families only) and grow it only on PASS; entries only outside the open window, with corroboration, no top-ups without an event | pooled satellite mean > 0 at 95%; ex-top-3 mean > 0; conviction calibration (Brier) improving |
| **Event specialist** (the model on bearish / event names) | where the model demonstrably adds value | declines ~90% of eligible names; IGNORED 1–30/session | B-4 scoped follow-up; render spot + listed expiries; measure accept/decline/veto and the P&L of each | fallback fills' mean ≥ model-proposed puts' mean; IGNORED → 0 |
| **Research** (offline) | decides what is allowed into the prompt | one IC study (Aug 25), 14-day signal history | the program in §6; signal-history retention ≥ 120 days; a replay harness; a decision-eval set | a signal enters the prompt only with IC > 0 at p < 0.05 on ≥ 60 dates |

---

## 6. Roadmap

### Phase 0 — now → the run-8 switch (Oct 27 → ~Oct 29)

Operator: merge PR #2 into `feature/preview`; choose the account (keep `PA3BGBBR2NB5` flat at ~$977k, or
a fresh $1M); `.env` block + `MAX_SYMBOL_EXPOSURE_PCT` 18 → 12; Robinhood login; host on AC or, better,
the always-on host (TODO N8 — the recurring root cause since September). Session: pre-register contract
v4 at the switch (SHA, tree id, dates, `FROZEN_TREE`), open `feature/preview` → `main`.

**Recommended addition — a control arm.** Run the same merged code on a **second paper account with
the satellite sleeve off** (core + defense + credit puts only), judged by the same checker over the same
dates. It answers "is the satellite sleeve worth anything" inside one window instead of waiting for two,
and it is the natural first measurement of the §5 target shape. Pre-conditions, each a day or less: a
verified configuration that disables fresh equity satellite entries while leaving core fills, the hedge
and put proposals intact — the candidate is `MAX_OPEN_POSITIONS=0` (since S-1 the slot count covers only
model-opened equity rows and `risk.py` rejects every new name once the count is at the cap; core fills and
the PSQ hedge enter via `_apply_pending_buy`, puts via `evaluate_option`, neither slot-gated), to be proved
by a test that a fresh-name BUY is rejected with `At max open positions (0/0 …)` while a core fill, a hedge
arm and a put proposal still pass; a second checkout with its own `state/` (the flock is per state
directory) and its own `.env`; the host must carry two processes (an always-on host makes this trivial; a
laptop on battery does not). If the
pre-conditions are not met by the switch, run-8 goes as built and the control arm becomes run-9's design.
Cost: roughly +$20–40 of API spend per window (the defense arm makes fewer decision calls).

### Phase 1 — run-8 window (first session ~Oct 29 → session 21 ~Nov 26; extension to Dec 4 if N < 24)

Frozen: no change to `investment_strategy/` or `.env`. The window measures the B-items against their
pre-registered removal metrics (contract v4 "Recorded metrics") and contributes the second sample to the
kill criterion (run-7: 27 trips at −$1,422; run-8 needs its own ex-top-3 mean above about +$1,400 to flip
the pooled sign).

Offline research in parallel (fingerprint-neutral, `scripts/`, nothing touches the bot):

- **R-1 Signal IC program.** Forward excess returns at 1/3/5/10 days by family and by cohort on the
  pooled ledger + every `signal_history` snapshot we have, with date-clustered errors. Output: a table of
  families with sign, IC and n; the decision rule for run-9 is written before the numbers are looked at
  (IC-2/IC-3 in TODO L8: technical weight → 0; discovery re-entry).
- **R-2 Satellite budget replay.** Replay the run-6/7 ledgers through the existing exit stack at
  satellite budgets of 0 / 25 / 50% with the core as beta carrier; report return, drawdown, capture and
  β for each. This is the quantitative input to the §5 "size to evidence" rule.
- **R-3 Decision-eval set.** Build a scored replay set from the `decisions/*.jsonl` contexts we already
  log (Jul 7 onward) joined to realized forward returns, so a prompt or model change can be scored
  offline (hit rate, calibration, expected P&L under the live sizing) before it ships. This is the
  missing learning loop of §4.1 and the only honest way to test the Claude 5 models or a prompt rewrite.
- **R-4 Conviction calibration.** Brier score and a reliability table of conviction vs realized win
  rate, per run and pooled. Decides whether conviction may size anything in run-9.
- **R-5 Always-on host.** Container (`ops/Dockerfile`) on a small VPS or a plugged-in Mac mini; the
  deadman, panel and launchd jobs move with it. Removes the battery/sleep class entirely.

### Phase 2 — run-8 close → run-9 (verdict the same evening session 21 closes; run-9 switch ~Dec 1)

Decision tree, written now so the verdict is read against it:

- **PASS** → run-9 on the same fingerprint (pooling toward N ≥ 60); research results are applied only
  to the run-10 change-set.
- **FAIL and the kill criterion fires** (pooled ex-top-3 mean ≤ 0) → run-9 is the **all-weather shape**:
  core as beta carrier (40–60%), defense with P1 fixed, credit-put sleeve, satellite ≤ 25% with flat
  sizing and IC-gated families only; the control arm (if it ran) becomes the main arm.
- **FAIL without the kill criterion firing** → one diagnostic, one change-set of ≤ 4 strategy items
  targeting the top verified mechanism, fresh window.
- In every branch: C-8 code hygiene (split `orchestrator.py`), P3 core execution, P5 regime second
  source ride along as fingerprint-changing but strategy-neutral items.

### Phase 3 — live pilot (Jan–Feb 2027 at the earliest)

Only after two consecutive PASS windows on one fingerprint and pooled N ≥ 60. Shape: $500–1,000,
fractional shares, options off, `MAX_DAILY_LOSS_PCT=2`, a tighter single-name cap, kill switch drilled,
the paper account kept running as the control arm. Any FAIL pushes the date by one window. Note the API
spend ($40/window) against a float of that size — the pilot proves execution and discipline, not income.

---

## 7. How we work — process changes that cost nothing

1. **The verdict is run the evening session 21 closes** (run-7's was six days late and the bot traded
   on). The in-session check (C-1) now pages on FAIL at 09:55 and 15:35 ET.
2. **Every restart, every amendment, every FAIL/WARN → one journal line the same day.** October had none.
3. **One RUN_SUMMARY per window with the same table** (§2's columns) so windows compare without
   re-reading 300 lines.
4. **Strategy change-sets ≤ 4 items; prompt changes are strategy changes** and need the R-3 score first.
5. **Reports state the sample behind every number** (n, pairs, dates); a number without n is not cited.
6. **The repository is public.** TODO N7 (personal-data review before any public flip) was written for a
   private repo; the flip has already happened (`isPrivate: false`). The scrubbed history carries no key
   values (verified Oct 27: every key line in the historical `.env.example` is empty), the author e-mail is
   in commit metadata regardless, and the dated logs hold paper-account IDs only. Decide whether to keep
   it public or flip it private until N7 is done — one command either way.

---

## 8. Risks to the plan, and what would change it

- **Regime.** A strong bull tape flatters any long satellite sleeve; a bear tape flatters the defense.
  Every verdict is read beta-adjusted and against the control arm, never on raw return.
- **Small N stays small.** If run-8 yields < 24 satellite trips the window extends once; if two windows
  cannot reach pooled N ≥ 60, the satellite sleeve is judged on the control-arm comparison instead.
- **The research comes back empty.** If R-1 finds no family with IC > 0, the satellite sleeve is held at
  the research size indefinitely and the book is core + defense + puts. That is an acceptable outcome:
  a measured all-weather index book with a 30% down-capture is a result, not a failure.
- **Host.** Until R-5 lands, every window carries battery and sleep risk; a window with > 2 dark gaps in
  hours is recorded as degraded under the contract's feeds class.
- **Measurement rot.** Any handle that greps nothing is a breach, not a zero (the run-6 lesson, now a
  test). The checker's exit-reason set and the ledger's fields are pinned by tests; keep them pinned.

---

## 9. Decisions for the owner (each one line in TODO.md "Decide" until taken)

1. **Run-8 scope:** as built (recommended), with or without the control arm, or shrink the satellite
   sleeve now (not recommended: it pre-empts the kill test the contract designed).
2. **Account:** keep `PA3BGBBR2NB5` flat at ~$977k, or a fresh $1M (cleaner day-0 row).
3. **Repository visibility:** public as is, or private until N7 is done.
4. **Always-on host:** approve a small VPS / Mac mini for R-5, or accept the battery risk for one more
   window.
5. **Live-pilot bar:** confirm the contract's bar (two PASS windows, pooled N ≥ 60, shape that passed)
   as the standing rule, so no session relitigates it.

---

## Sources

`runs/pre-final-test-run-4/RUN_SUMMARY.md` (B−, 42 trips) · `runs/pre-final-test-run-5/REVIEW_2026-08-25.md`
(129-trip backtest, IC study, exit-class split) and `VERDICT.md` (CONFOUNDED) ·
`runs/pre-final-test-run-6/RUN_SUMMARY.md` and `CHECKER_2026-09-18_close.txt` (B, NO-GO rule 7) ·
`runs/pre-final-test-run-7/A_PLUS_ROADMAP.md` (scenario table, P1–P6, the sample-size arithmetic),
`BACKTRACK_2026-09-26.md`, `RUN_SUMMARY.md` and `CHECKER_2026-10-20_close.txt` (FAIL) ·
`runs/pre-final-test-run-8/CHANGESET_DRAFT.md` (B-1..B-8, N-1, C-items) and `EVAL_CONTRACT.md` (v4) ·
`docs/ARCHITECTURE.md` · `docs/journal/JOURNAL.md` · `runs/LEDGER_CROSSRUN_2026-10-27.md` (the §2b
recompute, generated by `scripts/ledger_crossrun.py`).
