# Run-8 change-set — DRAFT from the run-7 session-4 backtrack

Written Sat 2026-09-26 ~02:00 CT. Source: `../pre-final-test-run-7/BACKTRACK_2026-09-26.md` (sessions 1-4),
`../pre-final-test-run-7/A_PLUS_ROADMAP.md` (packages P1-P6, Sep 21), and the run-6 verdict-day review.
**Nothing here ships in run-7.** The freeze holds through the Oct 20 close (Oct 27 if extended). Items are
authored in an isolated worktree on a `feature/run8-changeset` branch, each with a failing test first, an
env knob (default OFF in code, ON in the `docs/RUN8_SWITCH.md` STRATEGY KEY block), a log handle, and a
pre-registered metric with the result that would remove it. This draft is revised at the run-7 close with
the remaining 17 sessions of evidence; the evidence labels are `[V]` verified in run-7 logs/ledger,
`[R6]` verified in run-6, `[H]` hypothesis with n too small to act on alone.

## 0. Classification summary (drives pooling — see contract v3 "Same-config rule")

| # | Item | Class | Fingerprint | Roadmap |
|---|---|---|---|---|
| B-1 | Top-up authority = events (the conviction ratchet) | strategy | changes | new (P6-adjacent) |
| B-2 | Symbol exposure ≤ core; top-3 satellite bucket cap | strategy (.env key) | changes | P2 narrowed |
| B-3 | No fresh entries in the first 15 min | strategy | changes | new |
| B-4 | Bearish IGNORED → deterministic proxy-put fallback (model veto only) | strategy | changes | P4 (Aug 1 escalation trigger) |
| B-5 | Option entry limit buffer tiered by leg liquidity | risk/execution | changes | P4 |
| B-6 | Per-name put no-quote cooldown + honest reject text | strategy-adjacent | changes | P4 |
| B-7 | Entry spread check aligned with the watchdog mark cap; lot-size OI floor | risk | changes | P4 scenario 9 |
| B-8 | Premium-cap-bound underlying leaves the bearish slate for the day | strategy-adjacent | changes | P4 |
| C-1 | In-session check automated (09:55 / 15:35 ET) with a page on FAIL | ops (scripts/ops) | no | P6 — **can ship now** |
| C-2 | EDGAR resilience: retry + cached insider snapshot = `insider=cached` degraded mode | ops/feeds | no (code, run-8) | P6 |
| C-3 | yfinance "no fundamentals" for ETFs at DEBUG, not ERROR | ops | no | — |
| C-4 | Ledger/journal: prior conviction/composite/last-fill on top-up rows; raw `bearish_verdicts` + eligible list on IGNORED | measurement | no | P6 |
| C-5 | Ledger: `entry_minute` (minutes after the open) on every buy row | measurement | no | P6 |
| C-6 | Contract v4 text (pre-declared modes, new metrics, feeds breach class) | contract | — | P6 |
| C-7 | Robinhood token expiry warning 24 h ahead in preflight / morning check | ops (scripts) | no | — **can ship now** |
| D | Operator actions (host power, RH login, morning check, PR merge) | — | — | Step 0 |

Run-8 changes the fingerprint (any B item does), so pooling restarts at run-8 as the roadmap already
assumed (run-8 Oct 21 → Nov 18; run-9 → ~Dec 17).

## B. Strategy / risk items (fingerprint changes)

### B-1 Top-up authority: a top-up needs an event, not a restated number `[V]`
**Evidence.** `TOPUP_MIN_CONVICTION_DELTA=0.05` rejected 4.6 top-ups/session in run-6 and **0 in run-7's
32 cycles**. DBRG went 0.60 → 0.66 → 0.72 → 0.77 across four lots (composite 2.50 → 2.69 → 2.75 →
**2.53**), BRK.B 0.61 → 0.67, SMCI 0.62 → 0.68 — every step the minimum that clears the bar, the last
DBRG step on a falling composite. Top-ups were 5 buys / $296,479 / 38% of satellite dollars into 3 names;
they carry no starter haircut and no `MIN_NEW_NAME_CONVICTION`, and one lot was 4× its starter.
**Change.** `TOPUP_AUTHORITY=events_only` (default `legacy`): a top-up is approved only when, since the
symbol's last buy, at least one deterministic event holds — (a) composite up by ≥ `TOPUP_MIN_COMPOSITE_DELTA`
(0.25), (b) price ≥ last fill × (1 + `TOPUP_MIN_GAIN_PCT`/100) (winner add-on; 2%), or (c) a key-signal
family cited now that was not cited at the last buy (from `entry_key_signals`). The model's conviction
delta is recorded, not gating. `TOPUP_MAX_MULTIPLE_OF_HELD=1.0`: a single top-up may not exceed the
symbol's current cost basis (no 4× lots). Handle: `TOPUP AUTHORITY: <sym> allowed on event(s) …` /
`… rejected (no event; conviction delta +0.06 recorded)`. **Metric:** top-ups/session, top-up $ share of
satellite $, conviction-delta histogram, top-up-lot P&L vs starter-lot P&L. **Remove if** run-8 top-up
lots' mean P&L under the gate is below the run-7 top-up lots' mean (−$130/lot) at n ≥ 10.
**Test first:** replay the DBRG Sep 23/24/25 rows → 3 rejects (no event: composite +0.19, +0.06, −0.22;
price flat; same families cited); replay a synthetic +3% winner add-on → allowed on `price_gain`.

### B-2 Symbol exposure never above the core; top-3 satellite bucket cap `[V]`
**Evidence.** DBRG 18.0% of equity > QQQ core 15.1%; top-3 satellites 38% of equity. `.env` comment on
`MAX_SYMBOL_EXPOSURE_PCT=18.0` reads "let a high-conviction name run" — run-6/7 show conviction does not
rank outcomes, so the premise is gone. Exposure math: −4% gap on DBRG = −0.72% of equity; −10% = −1.8%.
**Change.** `MAX_SYMBOL_EXPOSURE_PCT` 18 → **12** (= `MAX_POSITION_PCT`; a top-up can only refill to the
new-position cap, never above the core). New `MAX_TOP3_SATELLITE_PCT=30` (0 = off): a buy that would lift
the sum of the three largest satellite weights above 30% is resized to the headroom or rejected. Handle:
`BUCKET CAP: top-3 satellites x% + $y -> z% > 30%`. **Metric:** largest satellite weight at each close;
top-3 share; the day's largest single-name share of new-buy dollars (already pre-registered in v3).
**Test first:** the Sep 24 08:34 DBRG top-up → resized to the 12% headroom ($~40k, not $78k); the Sep 25
SMCI top-up with DBRG 18% + BRK.B 10.9% + SMCI 3% held → resized so top-3 ≤ 30%.

### B-3 No fresh entries in the first 15 minutes `[H, n=6]`
**Evidence.** The first cycle fires at 09:30 ET and its buys hit the tape at 09:33. Sep 22's six
open-print entries: 0 of 6 positive; CRML/AUR/SOFI realized −$4,772 = 84% of run-7's realized loss.
Later-cycle entries 5 of 12 positive (cohort means −$1,262 vs −$87). Jul 27's "$300k QQQ open-print
sweep" is the earlier instance. One session, six names — a hypothesis.
**Change.** `ENTRY_OPEN_DELAY_MIN=15` (0 = off): fresh BUYs (not sells, not the watchdog, not the hedge,
not core fills) proposed while the session is younger than 15 min are deferred and re-evaluated at a
short second pass 15 min after the open with fresh quotes (the same proposal, re-gated by the anti-chase /
overextension checks against the new print). Handle: `ENTRY DEFERRED (open window): <sym> $x at 09:33 ->
re-check 09:45` and `ENTRY DEFERRED RESULT: <sym> 09:33 px -> 09:45 px (+/-y%) bought|dropped`.
**Metric:** the deferred cohort's 09:33 → 09:45 move (the cost or saving of waiting) and the cohort's
trip P&L vs same-day later entries. **Remove if** the mean 09:33 → 09:45 move is ≥ +0.3% over n ≥ 20
deferrals (waiting cost more than it saved).
**Test first:** a 09:31 cycle with 3 fresh buys + 1 sell → 3 deferred, sell executes; a 09:46 pass →
the 3 re-gated; a name that gapped +4% in between → rejected by the existing overextension gate.

### B-4 Bearish IGNORED → deterministic fallback with a model veto `[V]`
**Evidence.** 15 IGNORED name-cycles in 4 sessions (TDY 5, MIDD 5, SOXS 2, AAL, EXPO, NU) after schema
forcing (Aug 1) and a `bearish_verdicts`-required prompt (Sep). The Aug 1 escalation trigger — "any
`-> IGNORED` ⇒ escalate to deterministic put proposal (model veto only)" — has fired in every run since
and never been acted on. The journal stores only `verdict: put_ignored`, so the raw array cannot be
audited (see C-4).
**Change.** `BEARISH_IGNORED_FALLBACK=proxy_put` (default `log_only`): when an ELIGIBLE name has no
verdict entry, the bot builds the deterministic proposal the S-3/S-4 path already knows how to build
(own chain if it passes the liquidity floor, else `PUT_PROXY_ETF`), sized at the starter option debit,
and asks the model ONE yes/no veto question with the candidate line (spot, expiry, strike, OI, spread,
debit). Approve on silence is NOT allowed: a missing veto answer = no trade, logged. Handle:
`BEARISH FALLBACK: <sym> IGNORED -> deterministic <strategy> <contract> -> veto:yes|no|none`.
**Metric:** fallback proposals, vetoes, fills, and their P&L vs model-proposed puts. **Remove if**
fallback fills' mean P&L < model-proposed puts' mean at n ≥ 8, or if IGNORED drops to 0 by itself once
C-4 exposes the cause (in which case the cause is fixed instead).

### B-5 Option entry limit buffer tiered by leg liquidity `[V]`
**Evidence.** `ENTRY_LIMIT_BUFFER_PCT=20` (Jul 23 regression fix for a $0.01 → $0.03 thin-book fill).
HYG 78.5P ×50 filled 0.89 vs 0.79 estimated (**+12.7%, $500 on $3,950**) inside that buffer; LQD +4.1%;
IWM spread +0.25%; HYG ×3 +1.3%. Credit/index ETF puts do not need 20% headroom.
**Change.** Tiered buffer: 5% when every leg has quoted spread ≤ 5% AND OI ≥ 1,000, else 20% (existing).
Optionally a resting limit at mid + one tick for tier-1 legs with a 60 s fill-or-cancel before falling
back to the buffered limit. Handle: `OPTION ENTRY SLIP: <contract> est x fill y (+z%) tier=1|2`.
**Metric:** mean/max entry slippage by tier; unfilled-and-dropped count.
**Test first:** the HYG 50-lot quote → tier-1 limit 0.83, not 0.95.

### B-6 Per-name put no-quote cooldown + honest reject text `[V]`
**Evidence.** STE long_put proposed 9× and LH 3× over Sep 22–23, each rejected as "Net credit / no
debit" when the cause was `no quote available for <contract> — contract unknown to feed` (Oct expiry
absent from the Alpaca options feed); on Sep 24 with the Nov expiry quoted they died honestly (spread
27.1%, OI 5). A proposal slot was burned every cycle for two sessions.
**Change.** Reject text: `no NBBO on feed for <contract> (STRIKE SNAP found no OI-qualified strike
within 5%)`. `PUT_NOQUOTE_COOLDOWN_CYCLES=8` (rest of the day): a name whose put died on no-quote is
excluded from the bearish slate for that many cycles, printed on the `Buy-excluded from slate` line.
Handle: `PUT NO-QUOTE: <sym> cooldown 8 cycles`. **Metric:** repeat proposals of the same dead contract
per session (run-7 baseline: 5/day on Sep 22).

### B-7 Entry spread check ↔ watchdog mark cap; OI floor scaled by lot size `[V][R6]`
**Evidence.** The HYG 50-lot passed the 10% entry spread cap after the snap, then produced 16 `premium
mark unreliable (absurd spread)` warnings on Sep 24 — the watchdog could not mark it, so its premium stop
was blind intraday (run-6 scenario 9 reproduced with a different contract).
**Change.** (a) The entry check computes the spread the same way the watchdog does and requires it under
the SAME cap; (b) `MIN_OPTION_OPEN_INTEREST` scales with lot size: OI ≥ max(100, 20 × contracts) so a
50-lot needs OI ≥ 1,000; (c) when the watchdog's NBBO is absurd it marks from the underlying move × delta
(from the entry snapshot) instead of skipping, and logs `OPTION MARK FALLBACK:`; the premium stop then
still evaluates. **Metric:** `premium mark unreliable` count per session (baseline 16); premium-stop
evaluations skipped (target 0).

### B-8 A premium-cap-bound underlying leaves the bearish slate for the day `[V]`
**Evidence.** Once `PER-UNDERLYING PREMIUM CAP` bound on HYG ($4,912 of $4,956), Sep 24–25 produced 2 cap
rejects and 6 HYG DECLINED verdicts — the model spent bearish attention on a name it could not add to.
**Change.** On a cap reject, add the underlying to the bearish slate exclusion for the day (same
mechanism as the buy-side exclusion). Handle on the `Buy-excluded` line: `HYG (option premium cap)`.

## C. Ops / measurement items (fingerprint-neutral)

### C-1 In-session check automated — **ships now on scripts/ops** `[V]`
The Sep 25 feeds breach was visible at 10:17 CT and undisclosed until Sep 26 02:00 because nobody ran
`run7_morning_check.py` in-session on Sep 24/25. Add a launchd job (or the deadman's 5-min loop) that
runs the check at 09:55 ET and 15:35 ET, appends the verdict line to `logs/morning_check.log`, and pages
(the existing notify path) on any FAIL — including `FEEDS … > 2` and `power`. Read-only; no fingerprint.

### C-2 EDGAR resilience: retry + cached insider snapshot (run-8 code) `[V]`
Three sec.gov timeouts in one day made insider UNHEALTHY in 3/8 cycles. Retry with backoff (2×) and,
on failure, serve the last good insider snapshot if ≤ 24 h old as `insider=cached` — a pre-declared
degraded mode in contract v4 (C-6) — instead of UNHEALTHY. Handle: `FEEDS: 3/3 healthy insider=cached(4h)`.
Metric: cached cycles/session (target ≤ 2).

### C-3 ETF "no fundamentals" at DEBUG
yfinance 404 `No fundamentals data found for symbol: HYG|BITO|LQD` is logged at ERROR (3 lines Sep 25) and
pollutes the severity count. ETFs have no fundamentals; log at DEBUG when the symbol is an ETF.

### C-4 Journal and ledger fields that make B-1 and B-4 auditable
Top-up rows: `prior_conviction`, `prior_composite`, `prior_fill_price`, `topup_n`, `topup_events`
(what B-1 approved on). IGNORED: log the raw `bearish_verdicts` array (symbols only) and the ELIGIBLE list
on every `ELIGIBLE but IGNORED` warning; store both in the decision journal row. Today's journal has only
`verdict: put_ignored` — the Sep 24 TDY row cannot say whether the model returned the entry under another
key, truncated the array, or skipped it.

### C-5 `entry_minute` on every buy row
Minutes after the open at decision time, so B-3's cohort metric (and the run-7 open-print finding) is a
one-line ledger query rather than a log join.

### C-6 Contract v4 text
Pre-declare `insider=cached` (cap: > 2 cached cycles/day is still a breach); define the feeds breach by
class (which feed, which fallback); add recorded metrics: top-up count/share/conviction-delta (B-1),
largest satellite ≤ core and top-3 share (B-2), open-window deferrals and their 15-min move (B-3),
fallback puts (B-4), option entry slippage by tier (B-5), `premium mark unreliable` count (B-7). Add the
`TOPUP AUTHORITY:`, `BUCKET CAP:`, `ENTRY DEFERRED`, `BEARISH FALLBACK:`, `OPTION ENTRY SLIP:`,
`PUT NO-QUOTE:`, `OPTION MARK FALLBACK:` handles to `tests/test_contract_handles.py`. Keep rule 8 as v3.
Re-state the kill criterion: pooled run-7 + run-8 satellite ex-top-3 mean ≤ 0 ⇒ stock-picking is not the
edge; keep index core + systematic hedge + credit-ETF put sleeve.

### C-7 Robinhood token expiry warning — **ships now on scripts** `[V]`
The access token from the Sep 18 login died Sep 24 11:09 CT mid-session; the refresh needed a browser
(`OAuthFlowError: No redirect handler`). The morning check / preflight should compute the token file's age
and `expires_in` and print `WARN robinhood: token expires in <h> h — re-login today` from 24 h out, so the
handshake happens on the operator's schedule. `rh=dead` stays pre-declared; this is convenience.

## D. Operator actions (now, none of them touch the frozen surface)

1. **Power:** keep the 94 W adapter attached through Monday's open; the host was on battery overnight
   Sep 22, Sep 23 (1% hibernate) and Sep 25. The standing fix is the always-on host in `ops/`.
2. **Robinhood:** `.venv/bin/python -m investment_strategy.portfolio.robinhood_auth login` (browser). Not
   needed to trade; restores `earnings=rh` and the holdings context. Expect the next expiry ~6 days later.
3. **Monday Sep 28:** `.venv/bin/python scripts/run7_morning_check.py` at ~08:50 CT and again at
   ~15:35 CT; append its FAIL/WARN lines to `Todo-4.txt`. If FEEDS prints `insider UNHEALTHY` in > 2
   cycles, append the day to Amendment 1's table the same evening.
4. **Merge the PR** carrying this draft + `BACKTRACK_2026-09-26.md` + Amendment 1 + the Sep 23–25 logs.
   The merge is the acceptance step for Amendment 1. Then `git pull` on the live tree (the untracked log
   copies are removed by the session after the push, so the pull is not refused).
5. **Do not trim DBRG.** It is protected (4 bracket legs) and a mid-window trim is a strategy
   intervention. Its risk is quantified in the backtrack.

## E. What run-7 must still answer before this draft is final

- Does the open-print cohort keep losing (B-3) once n ≥ 20 entries in the first 15 min?
- Do the top-up lots (DBRG/BRK.B/SMCI) end positive or negative? B-1/B-2 stand on concentration alone,
  but the P&L decides how hard the multiple cap is set.
- Does the credit-ETF put sleeve (HYG/IWM/LQD, $14.6k premium) make money on a real down tape? It is the
  first evidence in seven runs that the bearish funnel can fill; its result decides whether B-4 is worth
  building or the sleeve stays model-proposed.
- Does IGNORED persist at ~4/session (B-4) or vanish (as on Sep 25)?
- Does EDGAR stall again (C-2 priority)?
