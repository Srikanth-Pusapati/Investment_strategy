# Run-7 — close summary and diagnostic (contract v3)

Window: **Tue 2026-09-22 → Tue 2026-10-20** (21 sessions; day 0 = Mon Sep 21 `late` row, $1,000,000).
Account `PA3BGBBR2NB5`, frozen tree `2a6ec147` (merge `eec9727` → `main` 8e1c901), the `.env`
STRATEGY KEY block of `docs/RUN7_SWITCH.md` section 2. Verdict taken Mon 2026-10-26 (the first
session the pinned command was run; nobody ran it on Oct 20 — see "Process" below).
Checker output: [`CHECKER_2026-10-20_close.txt`](CHECKER_2026-10-20_close.txt) (pinned command,
exit 2).

## Verdict: **FAIL** — five of seven counted rules

| rule | result | reading |
|---|---|---|
| 1 sample floor N ≥ 24 | met | N = 30 satellite trips (no extension needed) |
| 2 window expectancy > 0 @ 95% | **FAIL** | mean −$855/trip, t = −1.90 (crit 1.70), bootstrap-95 [−$1,768, +$6], PF 0.39, WR 20% |
| 3 pooled N ≥ 60 (live-pilot bar) | pending | N = 30, one window |
| 4 daily alpha > 0 | **FAIL** | −0.103%/day (t −0.78, 21 pairs, r² 0.06) |
| 5a ex-ante beta within 1.0 ± 0.2 | **FAIL** | mean stamped 0.73 (range 0.17..1.09) |
| 5b realized-beta 90% CI overlaps [0.8, 1.2] | **FAIL** | β 0.27, CI [−0.13, 0.66] |
| 6 max drawdown > −5% | PASS | −3.20% (peak Sep 21 → trough Oct 15) |
| 7 capture: up > down and down < 100% | **FAIL** | up −2.5% vs down 45.1% (9 up / 12 down SPY days) |
| 8 zero unsanctioned sub-0.5× decision sells | PASS | 0 unsanctioned; 7 sanctioned (`name_falling`) |

Telescoping: sum(day_pl) = equity diff = −$23,172.99, max per-day gap $0.00 → the measurement
is clean. No validity breach beyond the pre-declared Amendment 1 class (EDGAR-only days Oct 13
and Oct 16, each 1 cycle — under the 2-cycle bar).

## The numbers

| | run-7 | run-6 (reference, different fingerprint) |
|---|---|---|
| Book | $1,000,000 → $976,827 (**−2.32%**) | +3.22% |
| SPY / QQQ / IWM over the window | −0.58% / −3.22% / −3.43% | SPY −0.70% |
| Satellite trips / win rate | 30 / **20%** | 31 / 38.7% |
| Expectancy per trip | **−$855** | +$446 |
| Mean ex-top-3 (27 trips) | **−$1,422** | negative |
| Equity sleeve | 24 trips, **−$29,442**, mean −$1,227 | — |
| Options sleeve | 6 trips, +$3,801, mean +$634 | — |
| Realized β (target 1.0) | 0.27 | 0.98 |
| Cash after Oct 8 | 33% of equity | — |

The book "beat QQQ" only because it sat in a third cash after the Oct 8 purge and ran a β of
0.27 on a down tape, not because it picked well (alpha is negative). Contract rule 2 forbids a
grade above B when the ex-top-3 mean is ≤ 0; the strategy side reads **D**, the system side
A− (zero halts, zero crashes, drawdown and sell-validity pass).

## Diagnostic — what the ledger says (equity sleeve, 26 trips incl. post-window Oct 21–26)

Every bucket is negative; the ranking signals do not rank:

| cut | n | sum | mean | WR |
|---|---|---|---|---|
| **entry 0–15 min after the open** | 12 | **−$17,135** | −$1,428 | **0%** |
| entry 15–60 min | 8 | −$9,702 | −$1,213 | 38% |
| entry 60–180 min | 5 | −$1,122 | −$224 | 20% |
| lots_n = 1 (starter only) | 24 | −$20,954 | −$873 | 17% |
| lots_n = 3 (INTC, topped up twice) | 1 | **−$7,474** | | 0% |
| composite 0.5–1.0 / 1.0–2.0 | 6 / 20 | −$9,684 / −$19,446 | −$1,614 / −$972 | 17% / 15% |
| conviction < 0.62 / 0.62–0.70 | 11 / 15 | −$10,820 / −$18,310 | | 0% / 27% |
| cites `uptrend` (momentum) | 13 | **−$24,305** | −$1,870 | 8% |
| cites `fundamentals` | 9 | −$4,577 | −$509 | 33% |
| exit = `decision` (name_falling sanctioned) | 14 | −$17,459 | −$1,247 | 7% |
| exit = `bracket_stop` | 8 | −$16,935 | −$2,117 | 0% |
| exit = take / bracket_take / trail | 6 | +$16,865 | | 100% |

Realized % distribution: 22 of 26 trips negative (−1.2% … −9.7%); the three winners were
+10.0/+10.2/+11.4% takes. A 20% win rate at roughly 1:1 payoff cannot be positive; the
September-21 roadmap finding stands — composite and conviction do not rank outcomes, and
later-wave/top-up entries lose.

**Open-print cohort (B-3) is now evidence, not hypothesis:** 0 for 12.
**Top-ups (B-1/B-2):** the one 3-lot name produced a quarter of the window's realized loss.
**Options:** the sleeve is small and net positive, but its *protection* failed: the HYG 78/79 puts
quoted bid 0.42 / ask 1.31–1.39 (100%+ spread) for 13 sessions; the watchdog logged 977–3,055
`premium mark unreliable` skipped ticks **per day** Oct 8–20, so the premium stop never
evaluated (HYG 78P −65% unrealized at the close). Pre-market Oct 26 the IWM spread's stop
fired 113 times into Alpaca's "options market orders are only allowed during market hours"
(code 42210000), each a CRITICAL page, before closing at 09:16 CT for −$2,470.
**Bearish funnel:** `-> IGNORED` 1–30 name-cycles per session, every session (B-4).
**No-quote rejects:** the IWM Nov-21 weekly (`contract unknown to feed`) was re-proposed 10×
and refused as "Net credit" (B-6).

## Kill-criterion input (Sep 21 definition)

Pooled run-7 + run-8 satellite **ex-top-3 mean ≤ 0 ⇒ stock-picking is not the edge**; keep the
index core + systematic hedge + credit-ETF put sleeve. Run-7 contributes 27 trips at −$1,422.
Run-8 would need its own ex-top-3 mean above roughly +$1,400 over a similar N to flip the
pooled sign.

## Process (facts, not blame)

- The window's verdict command was not run on Oct 20; the live bot kept trading the frozen
  fingerprint through Oct 26 (post-window trips pool, they do not change the window).
- No journal entry was written in October; two restarts (Oct 8 09:42 CT after a battery
  sleep with "On BATTERY during market hours", Oct 9 07:38 CT after a 14-minute gap) went
  unjournaled; Oct 1–25 dated logs were uncommitted until this PR.
- Host: 32 battery CRITICALs and 9 in-hours dark gaps over the run (TODO N8 stands).
- Robinhood auth dead since Oct 13 15:16 CT (earnings fell back to yfinance; RH screener
  sources were not in run-7's `SCREENER_SOURCES`, so no feeds breach).
- Run-7 API spend: $40.10 over 230 calls.

## Decision (contract v3 "Decision rule": FAIL → one diagnostic + ONE change-set + fresh window)

This file is the diagnostic. The change-set is `runs/pre-final-test-run-8/CHANGESET_DRAFT.md`
as revised at the close (B-1..B-8, C-1..C-9, N-1/N-2), built on `feature/run8-changeset`; the
fresh window is run-8 under contract v4 (`runs/pre-final-test-run-8/EVAL_CONTRACT.md`).
Operator decision Oct 26: stop run-7, flatten the account at the Oct 27 open, switch after the
bell once the PR is merged.
