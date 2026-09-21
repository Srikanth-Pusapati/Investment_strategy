# Profitability reality check — run-6 through the Sep 15 close (audit written Sep 16 ~11:15 CT; the Sep 16 session is in progress and is NOT included)

Sources: runs/pre-final-test-run-4/RUN_SUMMARY.md, runs/pre-final-test-run-5/VERDICT.md, runs/pre-final-test-run-6/RUN_SUMMARY.md (+ pooled-history table), scratchpad CHECKER_INTERIM_2026-09-15_start0831.txt (v2), audit/CHECKER_V3_2026-09-15_start0901.txt (v3), audit/strategy_stats.txt, audit/alpha_residual.txt, logs/*.log "API spend today" lines, spy.csv, yfinance SPY closes for the run-4 window.

## 1. Multi-run realized record

| Run | Window (sessions) | Account return | Benchmark (same closes) | N closed trips | Expectancy/trip | PF | WR | Max DD | Verdict / grade |
|---|---|---|---|---|---|---|---|---|---|
| archive_0711 (Jul, pre-contract config) | – | – | – | 35 | -$12 | 0.64 | 51% | – | pooled-history row (run-6 RUN_SUMMARY) |
| archive_0727 (late Jul, pre-run-4 config) | – | – | – | 52 | -$116 | 0.16 | 35% | – | pooled-history row |
| run-4 | Jul 27 -> Aug 21 (19) | **+0.67%** ($1,006,732) | **SPY +3.60%** (739.09 -> 765.72, yfinance) -> **-2.93pp vs SPY** (never recorded in the RUN_SUMMARY) | 42 | +$167 | 1.10 | 31.0% | -2.98% | no contract; grade B-; capture Aug 3-21 up 1.19 / down 2.31 FAIL |
| run-5 | Aug 24 -> 26 (2 of 10 planned) | +0.70% at day 2 (file $1,007,026) | SPY +0.02% | 0 in-window (11 flatten rows: -$4,802, -$437/trip, PF 0.49 in the pooled table) | – | – | – | – | **CONFOUNDED** (telescoping, carried churn state, adaptive loops) |
| run-6 (interim) | Aug 31 -> Sep 15 (11) | **+0.86%** ($1,008,613) | SPY -1.26% / QQQ -1.70% / IWM -2.99% -> **+2.12pp raw, +2.05pp beta-adjusted** (beta 0.94) | **23 satellite** (28 with 5 system rows = +$1,280) | **+$376** (v3) / +$354 (v2) | **1.39** | 30.4% (v3) / 39.3% (v2) | **-3.29%** | interim checker **NO-GO** under BOTH --start (rule 7: INTC 0.49x); B+ provisional (Sep 11) no longer supported — see section 2 |
| all-time pooled ledger | Jul -> Sep 15 | – | – | **163** | **+$27** | – | – | – | **+$4,392 net over 163 trips on a $1M account** (-$4,251 pre-run-6 + $8,643 run-6 satellite) |

## 2. Run-6 through Sep 15 — what the numbers support (v3 satellite definition)

| Statistic | Value | Bar (RUN_SUMMARY A+) | Reads |
|---|---|---|---|
| N | 23 | >= 24 | UNDER-FLOOR (3 sessions left; pace 2.1/session) |
| mean / SD / SE | +$376 / $3,785 / $789 | – | – |
| t one-sided vs crit(df 22) | **0.476 vs 1.717** | mean > 0 at 95% | **FAIL** — needs mean > **$1,355** (3.6x current) |
| bootstrap-95 CI on the mean | [-$941, +$2,045]; P(mean<=0) = 0.33 | lower bound > 0 | FAIL |
| top-3 share / ex-top-3 mean | $26,638 = **308%** of the sum / **-$900** over 20 rows | ex-top-3 > 0 | FAIL (v3 rule 2 text caps the grade at B) |
| PF | **1.39** (gross +$31,066 / -$22,423) | >= 1.5 | FAIL (payoff 3.17 -> needs WR >= 32.1%; have 30.4%) |
| OLS alpha/day (10 pairs) | +0.194%, SE 0.361%, **95% CI [-0.51%, +0.90%]**, t 0.54 | alpha > 0 at >= 20 pairs | N/A (< 20 pairs; not interpretable) |
| beta-adjusted excess, Aug 31 -> Sep 15 | +2.05pp; daily residuals Sep 4 +1.35pp and Sep 8 +2.17pp = 3.5pp are the BE/INTC lots | – | the same 3 trades carry the expectancy AND the alpha; cumulative residual +3.93pp (Sep 11) -> +1.75pp (Sep 15) |
| exits since 2026-09-09T14:03Z (NVDA trail) | **15 consecutive losses, -$21,587**; entries dated Sep 8+ closed: 11/11 losses -$17,736; bracket_stop 14/14 losses -$18,918 | – | the post-Sep-8 book has produced no winner |
| max DD (close rows) | -3.29% (Sep 8 -> Sep 15) | >= -3% | FAIL |
| worst session | -2.23% Sep 14 (broker day_pl -$23,587) | >= -1.5% | FAIL |
| SPY red days at/above SPY | **3 of 7 = 43%** (Sep 1, 4, 8 yes; Sep 9, 10, 14, 15 no — all four worse than SPY) | >= 60% | FAIL |

Statistically: the sample cannot reject zero edge (t 0.48) and cannot reject a large positive edge either (CI top +$2,045/trip). What it does say: three trades in the first three sessions made +$26.6k; everything closed since has lost. "+0.86% vs SPY -1.26%" is real and rests on 3 trades and 2 sessions.

## 3. What the contract requires before "profitable" can be claimed

- v3 decision rule: live-pilot GO = **two consecutive same-fingerprint PASS windows AND rule 3 (pooled satellite N >= 60, mean > 0 at 95% one-sided)**, no amended counted rule inside either window.
- NYSE calendar (Columbus/Veterans Day open; Nov 26 closed): **run-7** day 1 Mon **Sep 21** -> session 21 Mon **Oct 19** (+<=5 extension -> Mon Oct 26). **run-8** (same fingerprint) day 1 Tue Oct 20 -> session 21 Tue **Nov 17** (with run-7 extended: Oct 27 -> Nov 24; both extended: Dec 2).
- **Earliest live-pilot GO: the evening of Tue Nov 17, 2026** — only if both windows PASS, pooled N >= 60 (>= 1.43 satellite trips/session; run-6 pace 2.09, run-4 2.21) and the pooled t clears ~1.67.
- Any FAIL -> one change-set -> new fingerprint -> the two-window count restarts: run-8 (new fp) ends Nov 17, run-9 ends Thu **Dec 17, 2026** = earliest GO after one FAIL.
- **Run-6's own verdict on Sep 18 is already determined: NO-GO.** The INTC row (2026-09-14T14:25:43Z, exit_reason=decision, realized -4.334% vs stop 8.906% = **0.487x**) is in the ledger; rule 7 counts it; no later close row can change it. Run-6 is a reference sample under v3 in any case (new fingerprint).

## 4. Rule 7 (v2) / rule 8 (v3) — the verdict turns on a sanctioned defensive sell (must fix before day 1)

- Rule 7 text (run-6 EVAL_CONTRACT.md): "Item 2 (LLM_SELL_AUTHORITY=events_only) makes these impossible; a count > 0 means the gate that ran is not the one that was frozen". The frozen gate says otherwise: run-6 tree `investment_strategy/risk.py:309-326` (run-7 tree `:331-348`, identical) allows a decision sell short of the stop whenever CODE attached an event tag — `Sep_14_2026.log` 09:25:43,409 `SELL AUTHORITY: INTC decision-sell allowed on event(s) name_falling:-5.6% today vs SPY -0.7% (unrealized -4.5%)` (NOK at 09:25:42,978 likewise). The docstring names the "name-falling defense" as part of the mechanical stack that owns losers.
- The checker (run-7 tree `scripts/eval_contract_check.py:579-616`, `decision_sell_losses_below_stop`, used by both v2 and v3 at `:1134` and `:1555`) keys on `exit_reason == "decision"` only; the sell row carries no sanction tag (`sell_events` is built at `orchestrator.py:4931` and passed only to risk; the 4a-18 lot stamp in `ledger.py:702-768` stamps entry_ts/fill/composite/conviction/stop, not the event). INTC 0.487x FAILs; NOK -4.123% vs 7.948% = 0.519x escapes by 0.15pp. With NAME_DROP_DEFENSE_PCT = 4.0 (`config.py:632/1226`) and 8-10% stops, sanctioned cuts land in the 0.4-0.6x zone by design — v3 rule 8 (same text, same function) will FAIL run-7 on the first such cut, independent of P&L.
- Fix (contract text, free before day 1; optional measurement-only stamp): re-scope rule 8 to decision-sell losses that carry NO event sanction, and give the checker the tag (stamp `sell_events` on the sell row at the 4a-18 lot stamp, then exempt `name_falling:`/`regime:` rows; or, with zero code, print sanctioned 0.5x hits as informational). Record run-6's final verdict as NO-GO-by-letter on a rule whose premise the frozen code contradicts — not as evidence the frozen gate was bypassed.

## 5. Plain answers

- **Is it A+? No.** As of Sep 11 the grade was B+ provisional; on Sep 15 close data the RUN_SUMMARY's own bars read: strategy fails 4 of 5 measurable bars (t, bootstrap LB, ex-top-3, PF), downside fails all three (DD -3.29%, worst -2.23%, red-day beat 43%). By that rubric: strategy C+, downside B-, overall **B-** on the current sample; the interim checker verdict is NO-GO.
- **Is it profitable?** On paper, in dollars, yes so far: run-6 +$8,613 on $1M (+0.86%) while SPY fell 1.26%; run-4 +$6,732 (+0.67%) while SPY rose 3.60%. Statistically, no claim is supportable: t 0.48 at N=23, P(mean<=0) 0.33, 308% of the P&L in three trades, 15 straight losing exits since Sep 9, and the all-time ledger is +$27/trip over 163 trips. The honest sentence: "two windows slightly positive in dollars, indistinguishable from zero edge, with the second window's gain concentrated in its first three sessions."
- **Cost.** API spend Aug 31 -> Sep 15 = **$18.99 over 11 sessions = $1.73/session** (8 decision calls at $0.17-0.27 each with 6,485-token cache reads after the first call, + $0.04 postmortem; Sep 15 $1.79, Sep 14 $1.74). That is 0.22% of the window P&L and $435/yr = 0.043% of $1M — negligible at this size. It is NOT negligible at the v2 live-pilot size: at $500-1,000 the API hurdle is 43-87%/yr against run-6's annualized gross pace of 21.7%; at $25k it is 1.7%. The logged ledger is Anthropic-only (Quiver Hobbyist and any other paid feeds are outside the logs).
- **What would change the answer to "profitable" (in order):** (1) run-7 satellite N >= 24 with mean > 0 at t > 1.72 AND ex-top-3 mean > 0 AND PF >= 1.5 — i.e. a window whose P&L does not depend on three trades; (2) run-8 the same, pooled N >= 60 with pooled t > 1.67 — earliest Nov 17; (3) alpha point estimate > 0 at >= 20 pairs with down-capture < 1.0 on >= 6 SPY red days. **What would change "A+":** every bar in section 2 passing on the 21-session close series plus the hedge/bearish/options/ops/measurement/process bars from the RUN_SUMMARY. Nothing in the Sep 16-18 sessions can change either answer; they can only change the run-6 N and the size of the drawdown.
