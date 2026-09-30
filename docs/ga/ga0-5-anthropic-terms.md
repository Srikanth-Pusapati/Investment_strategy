# GA-0.5 — Anthropic terms determination for a financial product

**Status:** DRAFT — determination NOT obtained. Nothing in this memo is a
legal conclusion or an Anthropic ruling. It is the argument we will submit
and the questions we must get answered in writing.
**Date:** 2026-07-04. **Owner:** solo operator.
**Feeds:** GA-0.2 (pricing), GA-4.4 (cost attribution), GA-7.2 (checklist item
"GA-0.4/0.5 determinations on file"), GA-7.4 (model-fallback policy).
**Blocks:** nothing today (paper, single-user, own key). Blocks GA-7.1 beta
entry for any shape, and blocks shape (b) entirely.

---

## 1. The question

Anthropic's Usage Policy treats automated decision-making in high-stakes /
consumer-financial domains as a heightened-risk use that carries obligations —
on our reading, at minimum human oversight and disclosure that an AI is
involved. We have not verified the current exact text, and we will not build
on a paraphrase (see §3, VERIFY-1).

Three sub-questions, one per product shape:

- **(a) Self-hosted, BYOK** — each user runs their own instance with their own
  `ANTHROPIC_API_KEY`. Does the design below satisfy the policy, and do the
  policy obligations rest on the end user (the API customer), on us (the
  developer shipping the software), or both?
- **(b) Hosted SaaS, metered org key** — our organization key makes every
  decision call for N tenants trading their own brokerage accounts. Same
  design question, plus: does routing all usage through one org key make us
  the single responsible party for every tenant's usage-policy compliance?
- **(c) Managed money** — out of scope for GA-1 (Todo-3 L.1 hard gate). Not
  analyzed here.

Today's posture (single user, own key, paper account) is the least-exposed
configuration possible; the question exists because GA changes that.

## 2. Our compliance argument, feature-by-feature

This is the case we will attach to the consult / the written request to
Anthropic. Every claim is verifiable in code or config.

| Policy concern | What the bot actually does | Where |
|---|---|---|
| LLM makes the financial decision | It doesn't, alone. Claude **proposes**; a deterministic `RiskManager` (~12 layered caps: position %, symbol/sector/gross exposure, daily-loss halt, drawdown halt, equity floor, per-trade $-risk cap, conviction floor, edge-ratio floor, PDT guard, correlation guard, cash buffer) **disposes**. The LLM is architecturally untrusted — it cannot exceed, disable, or see around any cap. | `investment_strategy/risk.py`, `config.py` `RiskLimits` |
| Human oversight | Kill-switch file (`state/KILL`) checked every loop — a human can halt new entries instantly without touching code. Reconcile mismatch vs the broker **latches a halt that only a human ack releases** (delete the file; `RECONCILE_HALT=on` by default). Dead-man heartbeat pages a human when either thread goes silent; email alerts on unprotected positions / floor breach. Going live at all requires three deliberate `.env` edits with a code interlock that refuses inconsistent settings. | `config.py` (`kill_switch_file`, `reconcile_halt_enabled`, `heartbeat_url`, live/paper interlock in `load_config`), GA-2.1/2.2 |
| Runaway automation | Account-level brakes are crash-path-proven (`--stress`: 5/5 PASS): daily-loss halt, max-drawdown halt, equity-floor flatten+latch. The 30s watchdog is LLM-independent and **risk-reducing only** — exits are never blocked, entries are what gets halted. | `risk.py`, watchdog loop, D.3 evidence |
| Disclosure | README leads with "Claude picks the trades" and "it can lose real money"; GA-1.2 bakes "paper trading / hypothetical results / past performance" disclaimers into the track-record generator so they cannot be omitted; GA-5.4 ships a "what this does NOT promise" page. | `README.md`, GA-1.2, GA-5.4 |
| Consumer opt-in | No trade touches an account the user didn't connect themselves. Shape (a): the user installs the software, supplies every key, and owns the account. Shape (b): users connect their own broker keys per account; per-user kill switch is a product requirement (GA-4.1/5.2). | GA-4.1, GA-5.2 |
| Cautious rollout | Paper mode is the default and the shipped configuration; live requires the deliberate interlock; beta is paper-only (GA-7.1); edge is publicly labeled unproven until GA-1.3 passes. | `README.md`, goGA GA-7.1 |

**Honest weakness in the argument, stated up front:** our "human oversight" is
*system-level* — a human supervises, can halt, and must ack divergence — but
there is **no pre-trade human approval of each order**. Between human
check-ins the loop is autonomous (decision every 900s, watchdog every 30s,
per `DECISION_INTERVAL_SECONDS` / `MONITOR_INTERVAL_SECONDS`). Whether the
policy requires per-decision human-in-the-loop or accepts supervisory control
with deterministic bounds is precisely the determination to obtain, not a
gap to argue around.

## 3. What we must NOT assume — obtain the determination

Rule for this whole section: **a plausible reading of a policy page is not a
determination.** We get it reviewed in the GA-0.1a attorney consult and/or in
writing from Anthropic (support or sales), and we file the answer for GA-7.2.

- **[VERIFY-1] The policy citation itself.** Exact question: *"Cite the
  current Usage Policy language governing automated decision-making in
  high-risk / consumer-financial domains (automated trading specifically, if
  addressed), and the specific human-oversight and disclosure requirements it
  imposes. Does a system where an LLM proposes trades but a deterministic
  code layer bounds every proposal, with a human kill switch and halt-on-
  divergence, meet the oversight requirement — or is per-decision human
  approval required?"* Where to look: Anthropic Usage Policy
  (anthropic.com/legal/aup) and Commercial Terms of Service
  (anthropic.com/legal/commercial-terms); then confirm interpretation via
  Anthropic support (support.claude.com) or sales, and the GA-0.1a attorney.
- **[VERIFY-2] Is end-user BYOK permitted for our use case?** Exact question:
  *"We ship self-hosted software; each end user supplies their own
  `ANTHROPIC_API_KEY` and runs the software on their own machine for their own
  brokerage account. (i) Is this distribution model consistent with the
  Commercial Terms (key usage, no key sharing — each user is their own
  Anthropic customer)? (ii) In this model, do the high-risk-use obligations
  (oversight, disclosure) attach to the end user, to us as the software
  developer, or both? (iii) Is there anything we as the developer must ship
  (disclosures, defaults, controls) for our users' usage to be compliant?"*
  Where to look: Commercial ToS + Usage Policy; ask support/sales for the
  developer-vs-customer responsibility split in writing.
- **[VERIFY-3] Does shape (b)'s metered org key concentrate policy exposure
  on us?** Exact question: *"If our organization key executes automated
  trading decisions for N end users' own brokerage accounts, (i) are we the
  sole responsible party for usage-policy compliance across all tenants?
  (ii) Are there requirements for consumer-facing AI financial products —
  end-user disclosure, human-oversight attestations, a high-risk-use review
  or approval process? (iii) Is this use case one Anthropic will support at
  scale, and at what usage tier / with what rate limits?"* Where to look:
  sales (this is an org-level commercial question, not a docs lookup);
  usage-tier and rate-limit docs at platform.claude.com/docs (api/rate-limits)
  for the capacity numbers feeding GA-4.6.
- **[VERIFY-4] Deprecation notice guarantees.** Exact question: *"What notice
  window does Anthropic commit to between model deprecation and retirement,
  and are aliases (e.g. `claude-opus-4-8`) retired on the same schedule as
  dated snapshots?"* Where to look: the model deprecations page under
  platform.claude.com/docs (about-claude/models); feeds §5.

Until VERIFY-1..3 return, GA-0.5 stays open and "determination on file" in
GA-7.2 stays unmet. Verbal comfort does not close this item — same standard
as GA-0.1b.

## 4. BYOK (shape a) vs metered org key (shape b)

| Dimension | (a) BYOK — user's `ANTHROPIC_API_KEY` | (b) Metered org key — ours |
|---|---|---|
| Spend | On the user, at their own Anthropic pricing. Our LLM COGS: $0. | On us. LLM spend is COGS, linear in N (slates differ per holdings — no cross-tenant prompt sharing of the decision call). |
| Rate tier | Each user's own tier. A new user's low tier may throttle their own instance — a support/docs problem, not an outage for others. | One org tier serves everyone. 429s at peak are a **correlated cross-tenant outage of the decision layer** (watchdog unaffected — it makes no LLM calls). GA-4.6 capacity model must be built against our actual tier limits and a synthetic-tenant load test is a GA-7.1 entry criterion. |
| Policy / ToS exposure | Each user is an Anthropic customer; obligations plausibly rest mostly on them — **but see VERIFY-2**; we do not assume the developer is out of scope. | Concentrated on us: one org, N users' automated financial decisions. One tenant's misuse is our org's compliance problem. See VERIFY-3. |
| ToS pass-through | Self-host docs must tell users they are bound by Anthropic's terms for their own key, and what this product does with it (decision calls only; key never leaves their machine; never logged — GA-7.3 shape-(a) security checklist). | We need end-user terms that pass through Anthropic's usage restrictions to tenants, plus whatever disclosure VERIFY-3 requires. Attorney work (GA-0.1a/0.1b scope). |
| Key custody | User's problem, on their machine (our docs cover `chmod 600` / keychain, per GA-2.6 guidance). | Ours: org key in our infra, per-tenant metering (GA-4.4), never in logs or prompts (GA-6.3 audit). |
| GA-0.2 pricing consequence | Price is a **software subscription** (or one-time / open-core). Unit economics are clean: our marginal cost per tenant ≈ $0 LLM. User-facing docs should include an honest "what Claude costs you" estimate: `tokens/cycle × ~26 cycles/market-day × model price` at `DECISION_INTERVAL_SECONDS=900` over a 6.5h session, tunable down via `DECISION_EFFORT` (default `medium`) and a longer interval. Use current list prices from platform.claude.com/docs pricing — do not hardcode them in marketing. | Price floor = measured `$/tenant/month` LLM + data spend (GA-4.4) + margin. The GA-0.2 back-of-envelope must use our org tier's real limits and current per-MTok list pricing, then be validated by real metering. A "deterministic-only" tier (LLM off, core + brakes) has near-zero marginal LLM cost in either shape and may be the honest flagship (GA-1.3 branch 2). |

Practical read: BYOK is not just lighter ToS surface (per goGA GA-0's framing)
— it also decouples our GA from Anthropic rate-tier growth and from carrying
regulated-adjacent spend for strangers. That is an argument, not a ruling;
VERIFY-2 decides it.

## 5. Model-deprecation risk (GA-7.4)

Facts, from Anthropic's published model lifecycle: models get deprecated with
a retirement date and then **404** (`not_found_error`) — e.g. `claude-3-opus`
retired 2026-01-05; `claude-opus-4-1` is deprecated with a 2026-08-05
retirement. Our default `DECISION_MODEL=claude-opus-4-8` (`config.py`) is an
alias, which is the right thing to pin (never a dated snapshot), but aliases
retire eventually too. A self-hosted user on an old tag **will** one day start
the bot against a model that no longer exists.

Fallback policy to document (and mostly already cheap to implement):

1. **Fail loud before market open, never mid-session.** GA-2.7 preflight
   validates `DECISION_MODEL` at startup via the Models API
   (`client.models.retrieve(model_id)`); a 404 is a startup failure with a
   plain-English message naming the env knob and the current recommended
   model, not a mid-session surprise.
2. **Fail safe if it happens mid-run anyway** (e.g. retirement lands on a
   long-running process): a `NotFoundError` from the decision call halts new
   LLM-driven entries via the existing kill-switch path and pages via alerts.
   The deterministic watchdog keeps protecting the open book — that layer has
   no LLM dependency and is unaffected. The bot degrades to
   deterministic-only, it does not die with positions open.
3. **Never silently substitute a model.** A model change is a material config
   change: on the record account it restarts the GA-1.1 clock (config-freeze
   policy); for a user it must be their deliberate `.env` edit. Auto-fallback
   to a different model would silently change the decision engine the track
   record was earned on.
4. **Ship the upgrade path, not just the knob.** `DECISION_MODEL` is already
   env-configurable — the missing pieces are documentation and comms:
   CHANGELOG entry + security-advisory channel note whenever the shipped
   default moves; a "model deprecated — what to do" section in the self-host
   docs (set `DECISION_MODEL` to the new default, restart, reconcile-first
   posture applies post-upgrade per GA-2.1); and [VERIFY-4]'s notice window
   documented so users know how much runway a deprecation notice gives them.
5. **The floor is LLM-off.** The deterministic-only mode (core + brakes,
   no Claude) is the terminal fallback for a user who cannot or will not
   upgrade. It is also independently a candidate product tier (GA-0.2).

## 6. Recommendation + next actions

**Recommendation:** proceed on shape (a)/BYOK as the working assumption — it
is the configuration where the compliance argument in §2 is strongest and the
exposure smallest — but treat that as unconfirmed until VERIFY-1 and VERIFY-2
return in writing. Do not self-certify against a paraphrased policy. Shape (b)
does not start (GA-4/5/6 stay gated) until VERIFY-3 has an answer alongside
the GA-0.1b written opinion. Document the §5 fallback policy now; it is a few
hours of docs + one preflight check and it de-risks GA-7.4 regardless of
which shape wins.

Next actions (desk work; parallel with the GA-2 pre-start gate, per ranked
step #5):

- [ ] Pull the current Usage Policy + Commercial ToS text; extract the exact
      clauses for VERIFY-1..3 (no paraphrase survives into this memo's next
      revision — quote or link).
- [ ] Add the three VERIFY questions + the §2 feature table to the GA-0.1a
      attorney-consult agenda (that consult is already ranked step #1).
- [ ] Send VERIFY-2 (and VERIFY-3 if shape (b) is still live after GA-0.2) to
      Anthropic support/sales in writing; file the response.
- [ ] Wire the model-existence check into GA-2.7 preflight; write the
      "model deprecated" runbook section into the self-host docs skeleton.
- [ ] Fold §4's pricing consequences into the GA-0.2 unit-cost model
      (BYOK: subscription pricing + user-cost estimate; org-key: metered
      floor from GA-4.4).
- [ ] Record the outcome here and mark the GA-7.2 checklist line
      ("GA-0.5 determination on file") only when a written answer exists.
