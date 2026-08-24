# Pre-final-test-run-4 — Jul 27 → Aug 21, 2026 (CLOSED)

**Account** Alpaca paper `PA3IRR2BT9FT`, reset to $1,000,000 on 2026-07-27.
**Result** $1,006,732.28 (+0.67%) · max drawdown −2.98% (peak Aug 4 → trough Aug 18) · intraday peak equity $1,032,435.
**Grade B−** (vs C+ on Jul 30). Full evaluation artifact: https://claude.ai/code/artifact/ec7e9742-3e2e-4a44-b73a-dae27c49b399
13-agent forensic digest lives in `analysis/` (ledger, red-day autopsy, funnel, ops, config, lessons, research, 3 plans, judge roadmap).

## Headline numbers (verified twice, independent recomputes agree)

| Metric | Full run (42 trips) | Away window Aug 13–21 (18 trips) |
|---|---|---|
| Realized P&L | +$7,031 | +$3,403 |
| Win rate | 31.0% | 33.3% |
| Avg win / avg loss | $6,083 / −$2,485 | $5,897 / −$2,665 |
| Expectancy / trade | +$167 | +$189 |
| Profit factor | 1.098 | 1.106 |
| Up / down capture vs SPY | 1.19 / **2.31 (FAIL)** (Aug 3–21) | 2.03 / 1.71 (first passing sample) |

July baseline for comparison: −$133/trade over 127 trips. The observation-window pre-eval (+$1,455/trade at N=20) collapsed to +$189 on sample noise — win-rate CI at N=20 is ±21.5pp. That is why run-5 has a pre-registered contract.

## What earned and what bled

- **Mechanical exits earned everything:** bracket_take +$35.2k (5), trail +$28.7k (5), take +$11.6k (1). **Claude decision-exits net −$24.7k over 23 closes.** Watchdog *option* premium stops −$33.8k (4) — two on corrupted open-auction quotes.
- **Options were the loss engine:** −$22.1k total; bull_call_spread 0-for-4 (−$33.8k: AMZN −$14,956, PLTR, MSFT, HL); the lone long_call made +$11.6k (MSFT). HL documented its own gate bypass (equity blocked as overextended → re-expressed as spread → −67.6% in 21h).
- **Signal families:** fundamentals +$29.0k/26, news +$27.4k/6, discovery +$6.9k/5, options-chain +$3.9k/24, technical +$0.2k/37, congress −$1.4k/3, **insider −$18.7k/13 (worst)**.
- **Composite score inverted at the top:** Q4 (≥2.02) expectancy −$859 (cycle) and −$1,792 (away); Q3 (1.60–1.99) best at +$2,728. High-composite junk small-caps (FTK, QNT, LFTO, FCEL) drove it.
- **Red days were single-name reversals, not market days:** Aug 18 −$26.8k on SPY −0.68% (3.9× down-capture; IESC −$6.7k, CDE −$4.8k, NVDA −$3.8k close-to-close). The bearish sleeve executed **0 trades in 83 verdicts** — every index-keyed defense slept because SPY never fell past −1.1%.
- **The expectancy gate saved ~$18k on Aug 18 alone** (rejected $140k KALU before a −13.1% day; AEHR before −15.4%).

## Top winners / losers (for symbol backtracking)

Winners: PATH +$13,807 (bracket_take), MSFT call +$11,645, SMCI +$9,437 (trail), CDE +$15,860 across two trips (trail + bracket_take), AXTI +$7,632 (bracket_take), PLTR +$4,466 (trail).
Losers: AMZN 5-leg option group −$14,956 (watchdog stop), PLTR option −$9,940, MSFT spread −$5,248, VRRM −$4,085 (decision), HL spread −$3,650 (junk-quote stop), CDE-family churn small losses.

## Lessons this run teaches the model (curated for upskilling)

1. Corroboration beats conviction: every loser cleared the 0.6 conviction floor comfortably; single-soft-signal entries (insider/congress/options-flow alone) fail fast, multi-signal names (SMCI, AXTI, CDE) won. → corroboration gate shipped for run-5.
2. The model's exit judgment subtracts value vs its own brackets — protect winners from discretionary closes below the trail-arm (instrumented in run-5, gated change later).
3. Option exit marks need the same junk-quote hardening as entries (Jul 23 lesson resurfaced on the exit side). → 2-tick confirm + quote sanity shipped.
4. Calm-tape red days need breadth triggers, not index triggers. → PSQ/core-defense breadth OR-trigger shipped.
5. Postmortems measured entry-basis realized P&L and mislabeled the Aug 18 "winner" — attribution must be close-to-close. → shipped.
6. Aggressive sizing (full Kelly, 45% vol target) is what turned ordinary red days into −2.6% days. → reverted for run-5.
7. Sample-size discipline: no verdict below ~24 trades; freeze config for the whole window. → eval contract.

## Known data caveats in this archive

- `state/trades.jsonl` here is the post-repair copy (Aug 17 MLEG backfill rows fixed by `scripts/repair_mleg_ledger_rows.py`; original preserved as `trades.jsonl.bak-mleg-repair-*` in live state/archive).
- Options in this run are ledgered under the **underlying** ticker (OCC-symbol ledgering ships with run-5) — attribution here undercounts the options bucket.
- `lessons/curated.md` lines 8/11/14 are annotated SUPERSEDED (misattributed option stops / contradictory churn guidance).
- RH screener sources were dead from Aug 19 (OAuth) — discovery inputs were degraded for the last 3 sessions.
