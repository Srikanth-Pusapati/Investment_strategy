# GA-0.1a — Securities-attorney outreach email + consult prep

Status: DRAFT, ready to send once an attorney is picked (see "Finding the
attorney" below). Ranked next-step #1 in goGA.txt — calendars are the long
pole; send before anything else on the list. The answers gate whether
GA-4/5/6 (the hosted-SaaS phases) exist at all.

---

## 1. The email (ready to send)

**Subject:** Paid 1-hour consult — regulatory posture for LLM-assisted trading software (solo developer)

**Body:**

Hello,

I'm a solo software developer seeking a paid 1-hour consultation on the
regulatory posture of an algorithmic trading product I may release. Brief
background so you can confirm fit before we book:

I've built software in which an LLM (Anthropic's Claude) proposes stock
trades and a deterministic risk layer — hard-coded position caps, loss
halts, and a kill switch the model cannot override — constrains and
executes them via the Alpaca brokerage API. It currently runs paper-only
(simulated money) on my own account. No clients, no managed funds, no
performance claims yet.

I'm evaluating three product shapes and want to spend the hour on the
first two:

(a) Self-hosted software: users download and run it themselves, with their
own brokerage and API keys, on their own machines. We ship software, not
advice.
(b) Hosted SaaS: we run per-user instances; trades execute on the users'
own brokerage accounts via their keys. No custody.
(c) Managed/pooled money: explicitly out of scope — not asking about RIA/BD
registration for this.

What I'd like from one hour:

1. Verbal confirmation (or correction) that shape (a) needs only proper
   disclaimers and a software license — and what those must say.
2. Scope and a quote for a written analysis of shape (b): whether
   "investment advice delivered via software" attaches adviser status, and
   the state-by-state RIA picture.
3. A review of a draft track-record page and landing-page claims (paper /
   hypothetical results) against the SEC marketing rule and FTC deception
   standards. I'll send both documents ahead of the call.

Do you handle this kind of engagement, and what is your rate and earliest
availability? Happy to sign an engagement letter and pay for the hour up
front.

Thank you,
Srikanth Pusapati
srikanthpusapati1@gmail.com

*(~260 words. Trim the background paragraph if an intake form asks for it
separately.)*

---

## 2. Consult agenda — questions in priority order

One hour is short. Ask in this order; if time runs out, the bottom items
are the droppable ones. Deliverables 1–3 map to goGA GA-0.1a.

**Priority 1 — shape (a) verbal clearance (target: ~15 min)**
1. Does distributing self-hosted trading software — user's own keys, own
   machine, own broker account — attach investment-adviser status in any
   state, or is it a pure software/publisher posture?
2. What must the disclaimer and license actually say? Is "not financial
   advice / you own every order" sufficient, or is specific risk-disclosure
   language required?
3. Does the LLM component change anything? The model proposes; a
   deterministic risk layer disposes; a human can kill it at any time. Is
   "the software decides trades for you" treated differently from a stock
   screener?
4. Any bright lines that would flip shape (a) into advice territory
   (e.g., shipping a default configuration, publishing signal feeds,
   charging a subscription vs. one-time license)?

**Priority 2 — shape (b) scoping, not answers (target: ~15 min)**
5. For hosted execution on users' own accounts (no custody): what is the
   written analysis you'd recommend — scope, state-by-state RIA question,
   internet-adviser exemption applicability — and what does it cost and
   take, start to finish? (I need the quote for budgeting, not the opinion
   today.)
6. Is there a cheaper intermediate posture worth analyzing (e.g.,
   notice-filing states only, or single-state launch)?

**Priority 3 — marketing/track-record review (target: ~20 min)**
7. Walk through the attached track-record page mock: paper-trading equity
   curve vs. QQQ/SPY benchmarks, per-source attribution, max drawdown,
   baked-in "paper trading / hypothetical results / past performance"
   disclaimers. What's missing or non-compliant under the SEC marketing
   rule (if adviser status ever attaches) and FTC deception standards
   (which apply regardless)?
8. Specific claims check: may I say "crash-tested risk controls,"
   "deterministic caps the AI cannot override," "we trade our own money on
   it"? Which phrasings are safe, which are regulated performance claims?
9. If the record turns out negative, are there rules about publishing it
   selectively? (Our internal rule: publish whatever it shows. Confirm
   there's no problem with the inverse — cherry-picking is what's banned.)

**Priority 4 — if time remains (~10 min)**
10. Anthropic's usage policy treats automated consumer-financial decisions
    as requiring human oversight + disclosure — does our kill-switch +
    deterministic-risk-layer + per-user-opt-in design plausibly satisfy a
    "human oversight" requirement from the legal side? (goGA GA-0.5.)
11. Entity/insurance: at what point do I need an LLC and E&O coverage —
    shape (a) at release, or only shape (b)? (goGA GA-0.3.)
12. CCPA/GDPR exposure for shape (b) (broker credentials + trading history
    = financial PII)? A pointer is enough; full treatment goes in the
    written engagement. (goGA GA-6.4.)

---

## 3. Documents to attach (send 2–3 days before the call)

- [ ] **Track-record page mock** — the GA-1.2 page as it will actually
      render: equity curve vs. QQQ, SPY, and QQQ + naive trailing stop;
      per-source attribution; max DD; all costs included; the baked-in
      disclaimers visible. If GA-1.2 isn't built yet, a static mock with
      real 2-day data + placeholder curve is fine — the attorney is
      reviewing claims and disclaimers, not the numbers.
- [ ] **Disclaimer + license text** — current README warning block ("paper
      mode… not financial advice… you own every order") plus the intended
      software license, as one document.
- [ ] **One-page product description** — the three shapes in the email,
      plus a diagram-level description of the LLM-proposes /
      risk-layer-disposes / kill-switch architecture. One page, no code.
- [ ] **Draft landing-page claims list** — every marketing sentence we'd
      want to use, as a bullet list, so the review is claim-by-claim.

Do NOT send: source code, API keys, the .env schema, or vendor contracts
(vendor ToS questions are GA-0.4, a separate track — mention them only if
the attorney volunteers overlap).

---

## 4. After the call — capture in writing

Within 24 hours, email the attorney a summary and ask for a one-line
"confirmed" reply. Verbal comfort evaporates; goGA's rule is explicit: do
not start GA-4/5/6 on verbal comfort.

- [ ] Shape (a) determination: adviser status yes/no, and the exact
      conditions it depends on (pricing model? default config? marketing
      language?).
- [ ] Required disclaimer/license language for shape (a) — verbatim if
      given, or a pointer to a template.
- [ ] Shape (b) written-analysis quote: scope, fee, timeline. Feeds the
      GA-0.3 budget line.
- [ ] Track-record page: itemized list of required changes; which claims
      on the landing-page list are cleared / conditionally cleared /
      rejected.
- [ ] Any bright-line "do not do this" statements, quoted exactly.
- [ ] Whether they'll take the GA-0.1b engagement, or a referral if not.
- [ ] Open questions the hour didn't cover (expect Priority-4 items) —
      logged back into goGA.txt against GA-0.3/0.5/6.4.
- [ ] Invoice + engagement letter filed (establishes privilege and the
      relationship for GA-0.1b).

---

## 5. Finding the attorney

Target profile: a **securities/RIA boutique** or solo practitioner who
works with small advisers, fintech startups, or fund launches — not
BigLaw (wrong cost structure for a 1-hour consult) and not a generalist
business lawyer (will punt on the marketing-rule questions).

Where to look, in order:
1. **State bar lawyer-referral service** for your state — ask for
   "securities regulatory / investment adviser compliance." Referral
   consults are often cheap or flat-fee for the first half hour. [VERIFY]
   Exact program and fee: check your state bar's website under
   "lawyer referral."
2. **RIA-compliance-adjacent directories** — attorneys who speak/write on
   the SEC marketing rule and internet-adviser exemption are findable via
   their published client alerts; search "SEC marketing rule hypothetical
   performance attorney." A published alert on exactly this topic is a
   strong fit signal.
3. **Referral from a fintech founder** — anyone who's launched a
   copy-trading, robo-advisor, or signals product has already done this
   search once.

Fit-check questions before booking (free, by email): have you advised
software-only (non-custody, non-discretionary… or discretionary-via-
user's-own-account) trading products before? Do you handle SEC marketing
rule reviews? Will you do a paid 1-hour scoping consult without a
retainer?

Cost expectation: [VERIFY] typical hourly rates for boutique securities
counsel — ask directly in the fit-check email ("what is your hourly rate
for a scoping consult?"); do not anchor on a guessed number. Budget the
hour plus 30–60 min of their document-review time (the attachments in
section 3), so assume 1.5–2 billable hours total.

Booking note: per goGA, book NOW even if the attachments aren't final —
calendars are the long pole, and the section-3 documents only need to
exist 2–3 days before the slot.
