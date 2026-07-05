# GA-0.2 — Shape decision, one-page PRD, unit economics

Status: DRAFT (2026-07-04). Companion to goGA.txt Phase GA-0. Nothing here is a
launch commitment; GA-1.3 (the 3-branch GO/NO-GO) and GA-0.1a (attorney consult)
gate everything below. Numbers marked [VERIFY] are estimates until checked
against the named source — do not quote them externally.

---

## 1. Shape decision

**Recommendation: shape (a) SELF-HOSTED is the GA-1 target.**

Per goGA GA-0: each user runs their own instance, on their own machine/VPS,
with their own keys (Alpaca, Anthropic, Quiver, optional Robinhood). We ship
software, not advice.

Why (a):

- Lightest legal surface: disclaimers + license, pending GA-0.1a verbal
  confirmation. No state-by-state RIA analysis, no custody questions.
- BYO-keys sidesteps most vendor-redistribution issues (GA-0.4): the user's
  Quiver/Alpaca/Anthropic usage is on their side of the ToS line. We never
  redistribute derived data.
- Robinhood: RH-derived signals likely cannot ship in a product at all
  (GA-0.4); shape (a) survives this — RH stays a per-user, opt-in, read-only
  feed under the user's own OAuth. Shape (b) would require feed removal.
- GA-4/5/6 (multi-tenant core, service infra, product-grade security,
  $10–30k pen test) do not exist for shape (a). Fastest real GA.

**Shape (b) hosted SaaS = Q1–Q2 2027 follow-on**, gated on: GA-0.1b written
legal opinion (multi-week, real fee), GA-0.4 redistribution licenses (Quiver
quote, Alpaca SIP terms), Alpaca OAuth app approval, GA-6.2 pen test, and
observed shape-(a) support load. Do not start GA-4/5/6 on verbal comfort.

**Shape (c) managed money: out of scope** (Todo-3 L.1 hard gate).

## 2. Target user

A technically capable self-hoster:

- Comfortable with a terminal, `.env` files, API keys, and running a Docker
  image on a $5–10/mo VPS (or an always-on Mac).
- Already has (or will open) an Alpaca account; understands paper vs live.
- Understands they own every order the bot places, and can read a risk
  disclosure without needing it translated.
- Wants disciplined, rules-bounded automation — not a returns promise.

Explicitly NOT the target: a hands-off consumer wanting a phone app, anyone
who can't articulate what a stop-loss is, anyone who would wire savings into
an unproven strategy. The docs say this out loud (GA-5.4 culture).

## 3. What "GA" literally means for shape (a)

- Public GitHub repo, tagged **v1.0**.
- Docker image (already in `ops/`) published per release.
- Docs site: quickstart, risk disclosure, "what this does NOT promise" page,
  key-handling guidance, upgrade procedure with open positions (GA-7.4).
- CI as a release gate: full test suite + paper-account smoke test required
  to tag (GA-7.3).
- Versioning/upgrade path: semver, schema-version stamps in state/ files,
  startup migration script, CHANGELOG + security-advisory channel,
  model-fallback policy for deprecated Claude model IDs (GA-7.4).
- **Invite-only beta first** (GA-7.1): 3–5 users, own keys, free, paper
  accounts ONLY, weekly check-ins, 4+ weeks. Live only behind a written risk
  acknowledgment + hard size cap enforced in config.
- Timeline: record window mid-July → mid-Oct at earliest; beta overlaps the
  window tail from ~Sept; **GA = late Oct–Nov 2026 at best** (Aug 1 → Nov 1
  if the pre-start gate slips). A GA-1.3 branch-3 result = no launch.

## 4. The honest value prop

**We sell the risk machinery, not returns.** "Autopilot with seatbelts":

- A deterministic risk layer (~12 layered caps) the LLM cannot override.
- Crash-path-proven account brakes (--stress: 5/5 PASS), kill-switch file,
  enforcing reconcile-halt, dead-man heartbeat, prompt-injection hardening.
- A benchmark-tracking core (QQQ) with bounded satellite experiments.

No alpha claim is permitted in any marketing until GA-1.3 **branch 1** passes
(satellites beat QQQ risk-adjusted over the frozen 3-month window). Our own
D.1 evidence: no satellite config has beaten QQQ; live record is 2 days.
Published research (StockBench, arXiv 2505.07078, Alpha Arena) agrees LLM
stock-picking alone doesn't beat buy-and-hold — the framework is the product.

The seatbelt has a measurable cost (whipsaw drag; D.3 pass-1 fired the
daily-loss flatten 2x in a normal year). The GA-1.2 track-record page shows it
against QQQ, SPY, **and QQQ + a naive 15% trailing stop** — the honest
competitor — with baked-in disclaimers that cannot be omitted.

## 5. The deterministic-only cheap tier (GA-1.3 branch 2)

Question: does a no-alpha tier — core ETF fill + all brakes + watchdog, LLM
off — deserve to exist, and is it the honest flagship?

**Argument for flagship:** it is the only tier whose value proposition does
not depend on an unproven edge. Zero LLM cost, zero Anthropic-policy surface
(GA-0.5 moot for this tier), no Quiver dependency (screener off), fewest
vendor-ToS entanglements. It is GA-1.3 branch-2 positioning made literal:
proven brakes + deterministic caps around a benchmark-tracking core. If the
window ends on branch 2, this tier IS the product and the LLM mode is the
research add-on.

**Argument against:** thin differentiation — "QQQ plus a trailing stop" is
nearly free anywhere, which is exactly why GA-1.2 plots that line as the
competitor. The tier's real value is the ops discipline (exchange-resident
brackets, reconcile-halt, dead-man fail-to-safe, equity floor, drawdown
latch), not returns.

**Verdict: yes — ship it as the default mode and the honest flagship until
branch 1 passes.** The LLM satellite mode ships as an explicit opt-in labeled
experimental, with its live record published whatever it shows. If branch 1
later passes, re-flag the LLM mode; if branch 3, the deterministic tier is
what survives.

Mechanically cheap: `CORE_ETF` + `TARGET_INVESTED_PCT` + watchdog already
exist; the tier is roughly `SCREENER_ENABLED=off` + skip the decision call.
Needs a small amount of work to make "LLM off" a first-class supported config
(preflight, docs, tests) rather than a degraded state.

## 6. Unit-cost model (back-of-envelope; GA-4.4 validates with real metering)

### Assumptions (stated, not verified)

| Assumption | Value | Source |
|---|---|---|
| Decision model | `claude-opus-4-8` | config.py default `DECISION_MODEL` |
| Model price | $5 / MTok input, $25 / MTok output | Anthropic price list [VERIFY — see below] |
| Prompt size / decision cycle | 10k–25k input tokens | estimate: signals + holdings + risk state for ≤18 candidates; NOT yet measured — measure with `count_tokens` before quoting |
| Output / decision cycle | 1k–3k tokens | estimate; includes thinking tokens (billed as output; `DECISION_EFFORT=medium` default — higher effort raises this) |
| Cycles / market day | 6.5 h × 60 / 15 min = **26** | `DECISION_INTERVAL_SECONDS=900` |
| Market days / month | ~21 | US calendar |
| Watchdog (30s tick) | $0 LLM | `MONITOR_INTERVAL_SECONDS=30`, no LLM calls |
| Quiver paid tier | user's own sub | [VERIFY — see below] |
| Alpaca | $0 | paper API free; live basic free [VERIFY data-tier terms in GA-0.4] |
| VPS | $5–10/mo | goGA GA-2.1 |

**[VERIFY] Anthropic pricing:** confirm current $/MTok for `claude-opus-4-8`
(input and output, plus prompt-caching read/write rates) against
https://platform.claude.com/docs/en/pricing.md before publishing any number.
The $5/$25 figures used below are from a cached price table and must be
re-checked at publication time.

**[VERIFY] Quiver pricing:** confirm the exact Quiver Quant tier the bot's
feeds (congress, insider, options flow) require and its current monthly price
at https://www.quiverquant.com/pricing — and, for GA-0.4, whether personal-use
API terms permit a self-hosted user to consume it this way. Do not quote a
dollar figure until checked.

### Arithmetic (LLM-on satellite mode)

Tokens/month: 26 cycles × 21 days = **546 cycles/month**.

- Input: 546 × 10k = 5.46 MTok … 546 × 25k = 13.65 MTok
- Output: 546 × 1k = 0.546 MTok … 546 × 3k = 1.638 MTok

Cost/month at $5 in / $25 out [VERIFY]:

- Input: 5.46 × $5 = **$27.30** … 13.65 × $5 = **$68.25**
- Output: 0.546 × $25 = **$13.65** … 1.638 × $25 = **$40.95**
- **LLM total: ~$41–$109 / month** (call it $40–110)

Levers not counted (upside, don't bake into pricing): prompt caching on the
stable prefix (system prompt + tool defs) could cut a large share of input
cost, but most of the prompt is per-cycle volatile market data, so treat
savings as bonus; `DECISION_EFFORT=low` cuts output cost; a cheaper model is
the user's call in shape (a), not ours. Batch API doesn't fit (decisions are
latency-bound to the cycle).

### $/user/month totals

| Line | BYOK, LLM on | BYOK, deterministic tier | Metered-org-key (shape b) |
|---|---|---|---|
| Anthropic | $41–109 (user pays) | $0 | $41–109 **we pay** |
| Quiver | user's own sub [VERIFY $] | $0 (not needed) | redistribution license [VERIFY quote, GA-0.4] |
| Alpaca | $0 | $0 | $0 + OAuth-app terms |
| VPS | $5–10 (user pays) | $5–10 (user pays) | our infra, ~$5–10/N tenants |
| **Run cost** | **~$46–120 + Quiver** | **~$5–10** | **>$110/user before margin, + license floor** |

Read: in shape (a) BYOK, the user's own run cost dominates and is theirs;
our price is for software + docs + updates only. Metered-org-key concentrates
$41–109/user/mo of LLM spend, the rate-limit tier, the Quiver redistribution
license, and the entire GA-0.5 Anthropic-policy exposure on us — that is a
shape (b) cost structure and stays gated with it. GA-4.6's capacity model and
GA-4.4's real metering supersede this table when they exist.

## 7. Pricing hypothesis — shape (a)

- **Deterministic tier: free.** The repo is public; this tier is the honest
  flagship and the funnel. User's out-of-pocket ≈ VPS only.
- **Paid: subscription, order-of $10–20/mo (or ~$100–200 one-time),** for the
  maintained artifact around the code: versioned releases + tested upgrade
  path with open positions (GA-7.4), Docker image, docs site, security
  advisories, model-deprecation fallbacks, best-effort support (solo
  operator — say so, GA-5.4). User pays their own API costs (BYOK).
- **Honesty constraint: the repo is MIT-licensed today.** Under MIT the code
  itself cannot be the paid artifact — anyone may redistribute it. Either the
  price is genuinely for maintenance/docs/support (defensible, common in
  self-host land), or the license changes before v1.0 (dual-license/BSL).
  Decide in GA-0.2 final; existing MIT-published code stays MIT regardless.
- No pricing on performance, ever, and no "beat the market" framing anywhere
  until GA-1.3 branch 1 — and even then only with the GA-1.2 page's
  disclaimers baked in.
- Sanity check vs value: at $10–20/mo we are a small fraction of the user's
  own $46–120/mo run cost — priced as tooling, not as advice. That asymmetry
  is deliberate; it is also the legal posture.

## 8. Open decisions

1. **License posture** (MIT stays + charge for maintenance vs re-license
   before v1.0). Blocks the pricing page.
2. **GA-1.3 thresholds X and Y** — must be written down before the window
   opens (candidate: X=3pp, Y=75%). Pre-registration, not post-hoc.
3. **Deterministic tier as default-on** — confirm the "LLM off" config is
   first-class (preflight, tests, docs) and whether the beta runs it or the
   satellite mode.
4. **Substrate for the record account** (VPS vs always-on Mac) — GA-2.1
   remaining action; also the reference deployment the docs describe.
5. **[VERIFY] Anthropic Usage Policy determination (GA-0.5):** does BYOK
   shape (a) — user's own key, kill switch, deterministic risk layer,
   per-user opt-in — satisfy the human-oversight + disclosure expectations
   for automated consumer-financial decisions? Ask in the GA-0.1a consult or
   get it in writing from Anthropic; check the current Usage Policy at
   https://www.anthropic.com/legal/aup.
6. **[VERIFY] Quiver personal-API terms for self-hosters (GA-0.4):** may a
   shape-(a) user's own key power this bot? Where: Quiver API terms of
   service / a direct ask to Quiver support.
7. **[VERIFY] Alpaca terms (GA-0.4):** basic (IEX) data tier sufficiency and
   any restrictions on third-party software using a customer's keys.
   Where: Alpaca market-data agreement + API terms.
8. **Robinhood feed:** confirm per-user OAuth read-only survives GA-0.4
   review; otherwise ship with the feed off by default.
9. **Measure real tokens/cycle** (`count_tokens` on live prompts) to replace
   the 10–25k estimate before any public pricing math.
10. **Beta → GA promotion criteria** already listed in GA-7.2; confirm the
    invite-only beta is publicly described as beta, not GA.
