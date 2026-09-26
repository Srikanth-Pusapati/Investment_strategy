# Run-7 backtrack — sessions 1-4 (Tue Sep 22 → Fri Sep 25, 2026)

Written Sat 2026-09-26 ~02:00 CT by the resident session, between session 4 and session 5 of 21.
Docs only. The freeze holds: `investment_strategy/` tree id `2a6ec147…` on HEAD = the frozen id; `.env`
mtime 2026-09-21 16:10 (pre-switch); the running bot (pid 6804) started 2026-09-23 08:10 CT after both.
Every number below is read from `state/`, `logs/Sep_2[2-5]_2026.log`, a read-only Alpaca call, or the
checker; nothing is estimated. Evidence label `[V]` = reproduced here from the ledger/log/broker.

The companion documents are `CHECKER_2026-09-25_interim.txt` (the pinned checker run on the last close
row — an interim reading, not the verdict), `EVAL_CONTRACT.md` Amendment 1 (registered from this
backtrack), and `../pre-final-test-run-8/CHANGESET_DRAFT.md` (the fixes, none of which ship in run-7).

## 1. Status at the time of writing

| Item | Reading | Verdict |
|---|---|---|
| Bot | pid 6804, `python -m investment_strategy`, up since Sep 23 08:10 CT, ONE instance (flock), last tick 0.4 min | ok |
| Freeze | `investment_strategy/` = `2a6ec147…` (frozen); zero commits to frozen paths since the switch; `.env` unchanged | ok |
| Morning check (`run7_morning_check.py --no-network`, 01:45 CT) | NOT READY — **1 FAIL: robinhood NEEDS LOGIN** (auth-dead latch set 2026-09-24 11:09 CT), everything else PASS | user action |
| Robinhood | refresh failed Sep 24 11:09 CT (`OAuthFlowError: No redirect handler` = the refresh token was rejected and a browser grant is needed); CRITICAL emailed; context reads latched off; earnings on `yfinance-fallback` since. `rh=dead` is pre-declared non-confounding | user action, not urgent |
| Host power | unplugged ~20:23 CT Sep 25 at 65%; maintenance-sleep cycles all night (the 50–89 min DARK GAPs, market closed, correctly not paged); USB-C 94 W adapter attached 01:33 CT Sep 26, charging, 68% | third overnight on battery since the switch |
| Deadman / panel / heartbeat / alert spool / backups | exit 0 every 5 min; panel up; heartbeat delivered; spool empty; nightly 02:15 tarball 672K | ok |
| Broker (read-only, 01:47 CT) | PA3BGBBR2NB5 ACTIVE, equity $991,013.68, cash $185,610.09, 13 equity positions + 4 option positions (5 legs), 18 open orders = 17 bracket TP legs (stop legs `held`, invisible to status=open) + QQQ GTC stop 632.68; **UNPROTECTED: none** | ok |
| API cost | $5.93 over 4 sessions ($1.45 / $1.40 / $1.59 / $1.50), 9 calls/day (8 decision + 1 post-mortem), claude-opus-4-8, cache_read stable | ok |

## 2. Equity path vs the indexes (close rows; index closes from Alpaca daily bars)

| Date | Equity | Day P/L (self) | Book % | SPY | SPY % | QQQ % | IWM % |
|---|---|---|---|---|---|---|---|
| Sep 21 (day 0, `late`) | 1,000,000.00 | — | — | 773.50 | — | — | — |
| Sep 22 | 998,367.02 | −1,632.98 | −0.16% | 773.38 | −0.02% | +0.81% | +0.57% |
| Sep 23 | 990,645.69 | −7,721.33 | −0.77% | 767.81 | −0.72% | −0.84% | −1.84% |
| Sep 24 | 990,140.91 | −504.78 | −0.05% | 767.18 | −0.08% | −0.01% | −0.09% |
| Sep 25 | 990,896.20 | +755.29 | +0.08% | 771.35 | +0.54% | +0.46% | +0.11% |
| **4 sessions** | | **−9,103.80** | **−0.91%** | | **−0.28%** | **+0.41%** | **−1.26%** |

Telescoping gap $0.00 on every pair (validity PASS). Max drawdown −0.99% (rule 6 PASS). The book
matched the one real down day (−0.77% on SPY −0.72%) and captured 14% of the one up day; realized OLS
beta 0.68 on 4 pairs, mean ex-ante `book_beta_spy` 0.84 (range 0.73–1.03) — within the ±0.2 band.
Cash was 18.6% at the Sep 25 close and options are 1.6% of equity, which accounts for part of the
up-day lag; the rest is the stop-outs below.

## 3. Interim checker reading (`CHECKER_2026-09-25_interim.txt`, exit 3)

**VERDICT: PENDING (UNDER-FLOOR)** — every counted rule that reached its floor passes; N=6 < 24.

| Rule | Reading |
|---|---|
| 1 sample | N=6 satellite trips (0 system rows) — needs 24 by the session-21 close or the window extends ≤ 5 sessions |
| 2 expectancy | sum **−$5,656.14**, WR **16.7%** (1W/5L), mean **−$942.69**, t=−2.148 (df 5), PF 0.01, bootstrap-95 [−$1,703, −$218]; mean ex-top-3 −$1,707 — not counted (N<24) |
| 4 alpha | −0.181%/day, t=−2.10 on 4 pairs — not counted |
| 5 beta | ex-ante mean 0.84 WITHIN; OLS 0.68, 90% CI [0.36, 0.99] overlaps [0.8, 1.2] — not counted |
| 6 drawdown | −0.99% — **PASS** |
| 7 capture | 1 up / 3 down SPY pairs: up 14.0% / down 120.8% — insufficient sample |
| 8 decision sells | **PASS** — 0 unsanctioned; 3 sanctioned sub-0.5× sells (CRML 0.13×, SNXX 0.42×, BB 0.37×, all `name_falling`) printed, not counted. A-5's `sell_events` stamping works: the run-6 NO-GO cannot recur on this wording |

**Validity: one breach, undisclosed until now → Amendment 1.** The Feeds condition says "> 2
UNHEALTHY/DEAD cycles on a day is a breach". `FEEDS: 2/3 — insider UNHEALTHY (edgar: timeout)` printed
1× on Sep 23 (12:52 CT), 1× on Sep 24 (08:33), and **3× on Sep 25 (10:17, 12:53, 14:39 CT)**. The
cause is sec.gov latency (the EDGAR Form-4 fetch timed out), external and not pre-declared. The
decisions taken in those cycles are listed in Amendment 1. Nobody ran the in-session morning check on
Sep 24 or Sep 25; it grades exactly this ("> 2 = breach", replayed Sep 18) and would have flagged it
at 15:35 CT. That process gap is run-8 item C-1.

## 4. Closed trips — every one a starter, five of six lost `[V]`

| Sym | Entry (CT) | Fill | Exit | Exit reason | Realized | Conv | Comp | Planned stop |
|---|---|---|---|---|---|---|---|---|
| CRML | Sep 22 08:33 | 8.73 | 8.62 same day | decision, `name_falling:-8.3% vs SPY -0.1%` | −156 | 0.60 | 1.89 | 10.0% |
| AUR | Sep 22 08:33 | 6.46 | 5.95 Sep 23 08:47 | **bracket_stop −7.7%** (exchange) | −2,321 | 0.60 | 1.66 | 7.7% |
| SOFI | Sep 22 08:33 | 17.78 | 16.67 Sep 23 12:40 | **bracket_stop −5.7%** (exchange) | −2,295 | 0.60 | 0.85 | 5.7% |
| SNXX | Sep 22 10:18 | 19.04 | 18.24 Sep 23 | decision, `name_falling:-7.4% vs SPY -0.7%` | −419 | 0.60 | 1.90 | 10.0% |
| VKTX | Sep 24 09:25 | 35.66 | 35.72 same day | decision, `name_falling:-14.2% vs SPY -0.5%` | +40 | 0.62 | 0.70 | 10.0% |
| BB | Sep 25 08:38 | 8.26 | 8.05 same day | decision, `name_falling:-7.8% vs SPY +0.5%` | −504 | 0.60 | 1.62 | 6.9% |

What the table says:

1. **All six were starters** at conviction 0.60–0.62 (the ×0.5 haircut applied to every one). The
   losers' mean entry composite is 1.58; the one +$40 "winner" had 0.70. Run-6's finding — composite
   and conviction do not rank outcomes (losers 1.44 vs winners 1.16, Spearman ≈ −0.2) — reproduces.
2. **The name-level defense did its job.** Four `NAME FALLING` reads → four sanctioned decision sells,
   mean realized −2.6% against a mean planned stop of 9.2% (0.13×–0.42×). The two full-width losses
   (AUR, SOFI) were exchange bracket stops that fired before or between cycles; `name_falling` reads
   intraday only, and both names gapped.
3. **The open-print cohort.** The first cycle runs at 08:30 CT and its six buys went out at
   **08:33 CT = 09:33 ET, three minutes after the open** (NVDA, NOK, SOFI, AUR, CRML, IBIT). Realized:
   CRML + AUR + SOFI = **−$4,772 = 84% of run-7's realized loss**. Unrealized on the other three at
   01:47 CT Sep 26: NVDA −$1,422, NOK −$980, IBIT −$202. **0 of 6 positive.** The twelve satellite
   entries made in later cycles: 5 of 12 positive (SMCI +1,254, IONQ +856, ADC +192, DFDV +175,
   VKTX +40 realized; ONON −892, BB −504r, SNXX −419r, BRK.B −302, DBRG −111, IEMG −81, AKAM −1,249).
   Cohort means −$1,262 vs −$87. n=6 from one session — a hypothesis with a pre-registered metric
   (run-8 B-3), not a proven rule. The Jul 27 "$300k QQQ open-print sweep" is the earlier instance.

## 5. Concentration — the top-up conviction ratchet `[V]`

`TOPUP_MIN_CONVICTION_DELTA=0.05` requires a top-up's conviction to exceed the last buy's by 0.05.
In run-6 that gate rejected 4.6 top-ups per session. **In run-7 it rejected 0 in 32 cycles.** The
ledger shows why:

| Name | Lot | Date | $ | Conv | Composite | Rationale head |
|---|---|---|---|---|---|---|
| DBRG | starter | Sep 22 | 19,972 | 0.60 | 2.50 | starter, ×0.5 haircut, 4% vol-floor stop |
| DBRG | top-up 1 | Sep 23 | **79,390** | **0.66** | 2.69 | "Top-up on strongest composite in book" |
| DBRG | top-up 2 | Sep 24 | **78,063** | **0.72** | 2.75 | "Top-up on strongest composite in book" |
| DBRG | top-up 3 | Sep 25 | 831 | **0.77** | **2.53** | "Top-up on strongest composite in book" — hit the 18% symbol cap |
| BRK.B | starter → top-up | Sep 22 → 24 | 29,766 → **78,709** | 0.61 → **0.67** | 1.18 → 1.68 | same wording |
| SMCI | starter → top-up | Sep 23 → 25 | 29,717 → **59,486** | 0.62 → **0.68** | 1.47 → 1.80 | same wording |

Every top-up's conviction is the previous lot's plus 0.05–0.06 — the minimum that clears the bar —
and DBRG's third top-up came on a **lower** composite than the second. The gate is fully learned: the
model restates its own number upward and the 4× and 2.6× lots ride on it. Top-ups were **5 buys,
$296,479, 38% of all satellite dollars, into 3 names**; fresh entries were 18 buys, $484,686
(mean $26.9k). Top-ups carry no starter haircut and no `MIN_NEW_NAME_CONVICTION`; they are the largest
orders in the book and the least gated.

Resulting book (01:47 CT Sep 26): **DBRG 18.0% of equity ($178,145) — larger than the QQQ core
(15.1%)**, BRK.B 10.9%, SMCI 9.1%; top-3 satellites = 38% of equity. P&L on the top-up lots is flat so
far (DBRG −$111, BRK.B lot 2 −$360, SMCI lot 2 +$83): this is a concentration engine, not (yet) a loss
engine. The exposure math: DBRG's four lots sit on 4% vol-floor stops; a −4% gap = −$7.1k = −0.72% of
equity from one $16 REIT; a −10% gap = −1.8%. The Sep 23 post-mortem flagged the day-level version
("DBRG $79k of $153k, >50% of the day's buy dollars") on its own.

**Nothing is done about it in run-7.** Trimming DBRG would be an operator strategy intervention
mid-window (the window would be AMENDED at best). It is protected; it stays. Fixes: run-8 B-1/B-2.

## 6. Bearish sleeve — puts filled for the first time in any run `[V]`

Funnel over 4 sessions (`BEARISH FUNNEL:` lines): **18 put proposals, 79 DECLINED, 15 IGNORED,
4 approved.** Decline classes: no confirmation 30, oversold/RSI 27, liquidity self-decline 17,
holds-the-name 2, other 3. IGNORED by name: TDY 5, MIDD 5, SOXS 2, AAL 1, EXPO 1, NU 1 (Sep 22: 6,
Sep 23: 5, Sep 24: 4, Sep 25: 0) — the model omits the `bearish_verdicts` entry for an ELIGIBLE name
despite schema forcing (Aug 1). The decision journal stores only `verdict: put_ignored`, so the raw
array the model returned cannot be read from disk — that is why run-8 B-4 has a measurement half.

| Filled | Contract | Qty | Est → fill | Debit | Note |
|---|---|---|---|---|---|
| Sep 23 12:01 CT | HYG 2026-10-30 78.5P | 50 | 0.79 → **0.89 (+12.7%)** | $4,450 | STRIKE SNAP 78P (OI 15) → 78.5P (OI 3,203); the 20% entry limit buffer admitted the slip |
| Sep 23 12:53 | HYG 2026-11-20 79P | 3 | 1.52 → 1.54 | $462 | STRIKE SNAP 78P → 79P (spread 14.8% → ok) |
| Sep 24 08:34 | IWM 2026-10-30 281/266 bear put spread | 12 | 4.04 → 4.05 | $4,860 | **SYSTEM PROXY PUT for HYG** after HYG Nov 78P failed the 10% spread cap at 10.1% — S-3 fired and worked |
| Sep 25 14:39 | LQD 2026-11-20 103P | 33 | 1.46 → 1.52 (+4.1%) | $4,818 | fill stamp lands Monday (pending order a9abf848; same shape as BRK.B Sep 22→23) |

All four are credit/duration-ETF puts on one thesis (HYG/LQD below their 200dma with put-heavy flow).
No single-name put filled: STE (9 proposals) and LH (3) died on Sep 22–23 as "Net credit / no debit"
— the honest cause is *no NBBO on the feed* for the Oct strikes (`no quote available for STE… —
contract unknown to feed` ×6) — and on Sep 24, when the Nov expiry had quotes, as spread 27.1% / OI 5.
Unrealized at 01:47 CT: HYG +$551, IWM spread −$444, LQD −$396 → −$289 on $14.6k premium.

Two run-6 defects reproduced: (a) `Option HYG … premium mark unreliable (absurd … spread)` ×16 on
Sep 24 — the 50-lot passed the entry spread check after the snap, then the watchdog could not mark it
intraday, so its premium stop was blind (scenario 9); (b) once the per-underlying premium cap bound on
HYG (~$4,950 = 0.5%), the model kept proposing HYG puts → 2 cap rejects + 6 HYG DECLINED verdicts on
Sep 24–25, pure churn.

## 7. Hedge, beta, core, regime `[V]`

- Regime read risk-on ×1.00 in all 32 cycles (SPY 763–775 vs 200dma 713–715; VIX 14–16, contango).
  `REGIME FALLING-TAPE CAP:` 0, `REGIME HOLD:` 0, `Market regime:` flips 0.
- Book beta (ex-ante) 0.52 → 1.04, invested 21.6% → 80.7%. The hedge never armed (arm line 1.15;
  PSQ w=0 throughout). `CORE FILL BETA CLAMP:` 0, `AUTO-HEDGE STARVED:` 0, `HEDGE WHIPSAW` 0.
- The Sep 25 SMCI top-up ($59.5k at β 3.29) moved book beta **+0.20 in one cycle** (0.84 → 1.04):
  satellite buys pass under the 1.20 cap by design; recorded, not a defect.
- QQQ core: 3 fills on Sep 22 to the 15% ceiling ($149,649 now). `CORE STOPLESS:` 4.1 s (the known
  wash-trade reject on day 1), 1.6 s, 2.6 s (core_fill) — all < 60 s → **0 stopless windows** on the
  ops bar. GTC core stop 632.68 resting.

## 8. Ops and feeds `[V]`

- Cadence: 8 decision cycles per session at 52 min (08:30, 09:22, 10:14, 11:07, 11:59, 12:51, 13:43,
  14:35 CT) — on cadence every day.
- Severity: CRITICAL 1 (Sep 23 08:25 battery — correct). ERROR: Sep 22 1 (QQQ wash reject),
  Sep 24 2 (RH OAuth dead; yfinance 404 "no fundamentals for HYG"), Sep 25 3 (yfinance 404 for the
  ETFs HYG/BITO/LQD — noise logged at ERROR level; run-8 C-3). Tracebacks 0.
- FEEDS healthy cycles: Sep 22 8/8, Sep 23 7/8, Sep 24 7/8, **Sep 25 5/8** (section 3).
  `news=vader-fallback` every cycle (Finnhub 403, pre-declared); `earnings=rh` → `yfinance-fallback`
  from Sep 24 11:09 (pre-declared).
- Overnight Sep 23 02:06–02:54 CT: 16 watchdog ticks skipped on Alpaca ReadTimeout while on battery;
  not paged by design (in-hours only). Sep 25 evening: `Heartbeat withheld` ×1 in a dark gap.
- Whole-share flooring left small remainders undeployed on 6 buys (cosmetic; core sweep absorbs).
- `Buy-excluded from slate` printed every cycle (4-h top-up cooldown, 18% cap) — working as intended.

## 9. Where this leaves the run

Too early to grade (4 of 21 sessions; N=6 of 24), but the closed-trip distribution is the worst start
of any run: 1W/5L, PF 0.01, mean ex-top-3 −$1,707. The Sep 21 kill criterion reads "pooled run-7 +
run-8 satellite ex-top-3 mean ≤ 0 ⇒ stop treating stock-picking as the edge". Run-7 is currently
deep on the wrong side of it. The system side is doing what it was built to do: sanctioned exits,
protected book, puts through the proxy path, freeze intact, cost $1.50/day.

Nothing changes in run-7. The freeze holds through the Oct 20 close. The change-set for run-8 is in
`../pre-final-test-run-8/CHANGESET_DRAFT.md`.
