# GA-0.4 — Vendor ToS / licensing audit (WORKSHEET)

**Status:** OPEN — this is a worksheet, not a determination. Nothing below is a
legal conclusion. Every "likely answer" is a planning assumption that stands
until its `[VERIFY]` item closes **in writing** (saved email, quote, or ToS
excerpt filed alongside this doc).
**Opened:** 2026-07-04. **Owner:** solo operator (that's you — every row).
**Feeds:** GA-0.2 (shape decision + pricing floor), GA-4.4 (cost/price floor for
shape (b)), shape-(a) BYO-keys documentation, GA-7.2 checklist ("GA-0.4/0.5
determinations on file").

## Ground rules

- **Todo-3 L.3 cleared the SINGLE-USER posture only.** One person, own keys, own
  account, paper trading. None of that transfers to a product.
- The two product shapes have very different exposure:
  - **(a) Self-hosted, BYO keys** — each user signs up with each vendor
    themselves and runs their own instance. Most ToS obligations land on the
    *user's* side of the line. Our exposure: shipping software that *uses* an
    API is different from *redistributing* its data — but we must confirm each
    vendor doesn't prohibit third-party clients / automated use, and our docs
    must not instruct users to violate terms.
  - **(b) Hosted SaaS, shared signal cycle** — we fetch data ONCE on our keys
    and fan derived signals out to N tenants. That is **redistribution of
    derived data**, the thing most market-data ToS restrict hardest. Every
    vendor below needs an explicit written answer for this shape.
- Model choice interacts with this: the shared cycle is the whole cost
  advantage of shape (b) (GA-4.2). If a feed can't be shared, shape (b) either
  drops the feed or buys per-tenant licenses — either way GA-0.2's pricing
  floor moves. That's why this audit gates GA-4.

---

## 1. Quiver Quant

**Today (single-user):** Paid personal tier (already subscribed). Feeds the
discovery screeners and signal layer: congressional trades
(`screener/congress_feed.py`, `signals/congress.py`), government contracts
(`signals/govcontracts.py`), off-exchange/dark-pool activity
(`signals/offexchange.py`), WSB sentiment (`screener/wallstreetbets_feed.py`).
Key: `QUIVER_API_KEY` in `.env`.

**Shape (a):** Each user buys their own Quiver subscription and pastes their
own key. Likely clean *if* Quiver's personal tier permits automated/API access
by a third-party client — that is not obviously true and must be confirmed
(some data vendors tie personal tiers to their own UI/first-party use).

**Shape (b):** The shared signal cycle takes one Quiver subscription and
redistributes derived scores to N paying tenants. This is near-certainly
outside a personal tier and needs a **commercial/redistribution license with a
quote** — goGA already assumes this. The quote is a direct input to the GA-4.4
price floor; if it's (say) $1k+/mo, a cheap tier without Quiver-derived
signals may need to exist (cross-ref GA-1.3 branch 2 / GA-0.2's
deterministic-only tier).

**[VERIFY] items:**
- [ ] `[VERIFY-Q1]` Does the current paid personal tier permit programmatic
  API use by user-run third-party software (our bot, user's key)? Where: Quiver
  Quantitative Terms of Service on quiverquant.com (footer link) — look for
  license-grant, "permitted use"/"restrictions", and API sections; also the API
  docs page's own terms. Save the excerpt.
- [ ] `[VERIFY-Q2]` What does a commercial license cost that covers: one
  API consumer, derived-signal (not raw-data) redistribution to N end users of
  a paid product? Ask exactly that. Where: Quiver's contact/sales channel on
  quiverquant.com (contact form or listed sales email). Get the quote in
  writing with the redistribution scope named.
- [ ] `[VERIFY-Q3]` Is "derived signals" (scores, not raw rows) treated
  differently from raw-data redistribution in their terms? Ask explicitly —
  don't infer it.

**Likely answer (planning assumption):** (a) OK with per-user keys; (b) needs
a commercial quote, price unknown — could be the largest line in the (b) unit
economics after the LLM.

**Feeds:** GA-0.2 pricing floor, GA-4.4, shape-(a) docs ("you need your own
Quiver subscription; here's which tier").

---

## 2. Alpaca (trading API + market data)

**Today (single-user):** The execution broker. Paper endpoint
(`https://paper-api.alpaca.markets/v2`), own keys, bracket/GTC orders,
positions, account state; also the source of price/quote data the bot trades
on. Live-mode interlock in `config.py` (mode/endpoint must agree).

**Shape (a):** Each user has their own Alpaca account + keys. This is Alpaca's
normal customer relationship; our software is a client the *user* runs. Likely
the cleanest vendor row — but confirm Alpaca's terms on third-party/automated
clients using customer keys, and note which market-data feed our code path
assumes (free IEX feed vs paid SIP subscription — behavior and entitlements
differ per user account).

**Shape (b):** Two distinct problems:
1. **Key custody / OAuth.** Users handing us raw API keys is likely the wrong
   (and possibly prohibited) integration path. goGA GA-5.2 already flags this:
   the sanctioned route is likely a **registered OAuth app**, which has an
   application/review lead time — start the application during GA-0 if shape
   (b) is chosen.
2. **Market-data redistribution.** SIP-derived data carries exchange-imposed
   redistribution restrictions. A shared cycle that fetches quotes on OUR data
   key and shows/acts on them for N tenants may require each tenant to be an
   entitled subscriber, or require us to hold a redistribution agreement.
   Possible mitigation to ask about: fetch market data per-tenant on the
   tenant's own (OAuth-linked) account entitlements, keeping only
   non-market-data feeds in the shared cycle.

**[VERIFY] items:**
- [ ] `[VERIFY-A1]` Which market-data feed do we actually consume today (IEX
  vs SIP), and what do the terms we've already accepted say about display vs
  non-display and redistribution? Where: Alpaca "Terms" / market data agreement
  accepted at account signup (alpaca.markets legal/disclosures pages; the
  dashboard's documents section) — look for the market-data agreement and any
  exchange-data addenda.
- [ ] `[VERIFY-A2]` For shape (b): what is Alpaca's supported path for a
  third party operating trading on customers' accounts — OAuth app
  registration requirements, review timeline, and any prohibition on holding
  customer API keys directly? Where: Alpaca docs (OAuth / "Connect" /
  broker-vs-trading-API sections) + a written question to Alpaca
  support/partnerships via their contact channel.
- [ ] `[VERIFY-A3]` Does anything in the trading-API terms restrict fully
  automated order flow from an LLM-assisted system? (Expected: no special
  restriction beyond standard automated-trading terms — but get it on file,
  it pairs with the GA-0.5 Anthropic determination.)

**Likely answer (planning assumption):** (a) clean, per-user accounts, note
the feed entitlement in docs; (b) OAuth application required (lead time!) and
SIP redistribution restricted — plan per-tenant data entitlement rather than
shared-cycle market data.

**Feeds:** GA-5.2 onboarding path, GA-4.6 capacity model (per-key rate
limits), shape-(a) docs, GA-0.2.

---

## 3. Robinhood agentic MCP

**Today (single-user):** Optional, read-only, context-only. OAuth/PKCE
handshake (`robinhood_auth login`), positions read for portfolio context, and
the discovery screener reads RH-curated lists ("Daily movers", "100 most
popular") via `screener/robinhood_feed.py`. Requires RH Gold. Orders NEVER go
through RH — Alpaca + RiskManager only. Known gotcha (memory:
robinhood-oauth-flow): RH advertises a **single scope** (`internal`) that is
trade-capable — there is no read-only scope, so even our read-only use holds a
token that *could* trade.

**Shape (a):** Per-user RH auth: each user runs the PKCE flow against their
own RH account, token stays on their machine. This mirrors today's posture ×N
individuals — but whether RH's agentic-MCP terms permit use inside
*distributed third-party software* (as opposed to a personal agent) is exactly
the open question. Also a product-safety question independent of ToS: shipping
software that holds a trade-capable RH token while promising "read-only" is a
liability we'd carry in docs and code review (the reader never calls trade
tools — say so, and keep it provable).

**Shape (b):** goGA's stated likely answer: **RH-derived signals CANNOT ship
in a product at all** — a hosted service holding N users' trade-capable RH
tokens, or redistributing RH-curated list data through a shared cycle, is
almost certainly outside the MCP's intended/permitted use. **Plan: feed
REMOVAL in shape (b).** The screener is already a no-op when
`ROBINHOOD_ENABLED` is off, so removal is a config default plus doc honesty
("the hosted product has no RH signals"), not an engineering task.

**[VERIFY] items:**
- [ ] `[VERIFY-R1]` What do the Robinhood agentic-MCP / API terms say about
  commercial use, third-party software distribution, and multi-user services?
  Where: the terms presented during the RH OAuth consent flow (re-run
  `robinhood_auth login` and capture them), robinhood.com legal/disclosure
  library, and any developer/agentic-platform terms page RH publishes. Look
  for "personal use", "commercial use", "resale/redistribution" language.
- [ ] `[VERIFY-R2]` Is there (or is there a roadmap for) a read-only scope?
  Ask RH support/developer channel in writing. A trade-capable-only token
  materially changes what shape-(a) docs must warn about.
- [ ] `[VERIFY-R3]` Does redistribution of RH-curated list contents (movers /
  most-popular constituents) to third parties appear in their restrictions?
  Expected: yes, restricted. Get the citation.

**Likely answer (planning assumption):** (a) per-user auth *may* be OK —
document the trade-capable-token risk loudly either way; (b) feed removed,
full stop. Do not build shape-(b) signal value on RH data.

**Feeds:** shape-(a) docs (RH section with the scope warning), shape-(b) feed
list (GA-4.2 "feeds allowed in the shared cycle are whatever GA-0.4 permits"),
GA-0.2.

---

## 4. SEC EDGAR

**Today (single-user):** Free, direct. Insider Form-4 discovery
(`screener/insider_feed.py`, latest-filings feed, scan limit 100) and
per-symbol Form-4 parsing (`signals/insider_edgar.py`) against sec.gov /
data.sec.gov, with a declared `User-Agent` from `SEC_USER_AGENT`.

**Shape (a):** Each instance hits EDGAR itself. EDGAR data is US-government
public-domain; the constraint is *fair access*, not licensing: published
rate limits and a User-Agent that identifies the requester with real contact
info. Problem in the shipped default: `config.py` ships
`SEC_USER_AGENT="investment-strategy-bot contact@example.com"` — a placeholder
contact. Shape-(a) docs (and preflight, GA-2.7) should require each user to
set their own real contact, per SEC's stated policy.

**Shape (b):** Redistribution of public-domain filings data is not a licensing
problem. The problem is operational: one host serving N tenants must stay
inside SEC's rate limits from shared IPs (feeds GA-4.6 capacity model), with
one honest User-Agent identifying the service.

**[VERIFY] items:**
- [ ] `[VERIFY-E1]` Confirm the current fair-access rules: max request rate,
  User-Agent requirements, and any automated-access registration. Where:
  sec.gov → "Accessing EDGAR Data" / webmaster FAQ pages (the developer-access
  guidance). This is a 15-minute read-and-file, not a negotiation.
- [ ] `[VERIFY-E2]` Confirm there is no restriction on commercial reuse of
  filing data (expected: none — public domain), and file the citation so the
  determination is "on file" per GA-7.2.

**Likely answer (planning assumption):** Cleanest row in the table. Public
domain; obey rate limits; fix the placeholder User-Agent default before
anything ships.

**Feeds:** shape-(a) docs (require real `SEC_USER_AGENT`), GA-2.7 preflight,
GA-4.6 capacity model.

---

## 5. Anthropic (cross-ref GA-0.5)

**Today (single-user):** The decision engine. `DECISION_MODEL` default
`claude-opus-4-8`, one decision call per 900s cycle (`DECISION_INTERVAL_SECONDS`),
effort `medium`, 90s timeout; the 30s watchdog (`MONITOR_INTERVAL_SECONDS`) is
deliberately LLM-free. LLM output is treated as untrusted and bounded by the
deterministic risk layer; kill-switch file + heartbeat exist.

This vendor's determination is **GA-0.5's job** — this worksheet holds the
licensing/commercial angle and defers the policy determination there. Do not
close this row without GA-0.5 closed.

**Shape (a):** BYOK — each user brings `ANTHROPIC_API_KEY`. The Usage Policy
obligations then attach to each user's own use; our exposure is shipping
software *designed* for automated financial decisions. goGA GA-0.5: Anthropic's
Usage Policy treats automated high-stakes/consumer-financial decisions as
requiring human oversight + disclosure — our argument is the kill switch +
deterministic risk layer + per-user opt-in, but **obtain the determination,
don't assume it**.

**Shape (b):** Metered org key — concentrates spend, rate-tier, and policy
exposure on us. We become the party running automated financial decision-making
for consumers at scale. Needs the written determination *before* GA-4 starts,
plus rate-tier/capacity math (GA-4.6: LLM traffic is linear in N tenants since
slates differ per holdings) and the token unit-cost model (GA-0.2).

**[VERIFY] items:**
- [ ] `[VERIFY-AN1]` (= GA-0.5) Written determination that this use (automated
  trading decisions, human-overridable, deterministic caps, per-user opt-in,
  disclosures) is permitted under the Usage Policy — for BOTH shapes. Where:
  Anthropic Usage Policy + Commercial Terms on anthropic.com/legal (look for
  high-risk / financial-decisions / human-oversight language); route the
  interpretation through the GA-0.1a attorney consult or Anthropic
  sales/support in writing.
- [ ] `[VERIFY-AN2]` Shape (b) resale angle: does providing Claude-generated
  output as part of a paid service to third parties require anything beyond
  standard Commercial Terms (reseller-ish provisions, disclosure requirements)?
  Where: Commercial Terms of Service, anthropic.com/legal.
- [ ] `[VERIFY-AN3]` Model-deprecation policy for a pinned `DECISION_MODEL`
  (feeds GA-7.4's fallback policy for self-hosted users). Where: Anthropic docs
  model-deprecation page.

**Likely answer (planning assumption):** Permitted with our control story, but
the disclosure/oversight requirements are real and shape the product (kill
switch stays a hard requirement, "human can intervene" must remain true in
both shapes). Shape (b) makes us the policy-exposed party.

**Feeds:** GA-0.5 (this row IS its worksheet twin), GA-0.2 pricing (BYOK vs
metered), GA-4.6, GA-7.4.

---

## 6. yfinance / Yahoo Finance

**Today (single-user):** Load-bearing and free: market-regime guard (SPY
200dma + VIX) in `regime.py`, sector classification for the concentration cap
(`sectors.py`), earnings-blackout dates (`earnings.py`), and the
technical/fundamentals signal providers (`signals/technical.py`,
`signals/fundamentals.py`). The code already treats it as unreliable
(`REGIME_DEGRADED_MULT=0.5` sizes down when it's dark) — that's an
availability hedge, not a licensing answer.

**The honest position:** yfinance is an unofficial library that scrapes
Yahoo's endpoints. It is **not a licensed data feed**, and Yahoo's terms are
generally understood to not permit this kind of automated/commercial use.
There is no sales contact to fix this with — there is no "yfinance commercial
tier". **Flag for replacement in any product shape**, and honestly, replace it
before the GA-1.1 record window leans on it any harder than it already does.

**Shape (a):** Shipping software whose default config depends on scraping
Yahoo puts every user in the same gray zone and puts us in the position of
instructing them into it. Weakest link of the BYO-keys story ("bring your own
keys" — to a feed that has no keys).

**Shape (b):** Out of the question as a shared-cycle feed for paying tenants.
Replace, don't license (there's nothing to license).

**Replacement candidates already keyed in `config.py`:** Alpaca data (prices,
already the broker), Polygon (`POLYGON_API_KEY`), FMP (`FMP_API_KEY`), Finnhub
(`FINNHUB_API_KEY`), FRED (`FRED_API_KEY`, for macro series like VIX
alternatives). Each replacement inherits its OWN row of this audit before it
ships (see §7).

**[VERIFY] items:**
- [ ] `[VERIFY-Y1]` Confirm the current Yahoo ToS position on automated
  access/scraping and commercial use. Where: Yahoo Terms of Service (yahoo.com
  legal/terms pages; look for automated-access / data-use restrictions), and
  the yfinance project README's own disclaimer (it states it's not affiliated
  with or endorsed by Yahoo). Expected answer: not permitted for a commercial
  product. File it.
- [ ] `[VERIFY-Y2]` Map each yfinance call site (regime, sectors, earnings,
  technical, fundamentals) to a licensed replacement and its cost/rate limits;
  produce a migration list. This is engineering desk work, not vendor contact.

**Likely answer (planning assumption):** Not licensed for a commercial
product in either shape. Replacement is a pre-GA engineering task; cost of the
replacement feeds goes into GA-0.2's unit economics.

**Feeds:** GA-0.2 (adds a real data line to unit cost), GA-1.1 (the record
window's regime guard should not rest on a scraper), shape-(a) docs.

---

## 7. Not covered here (follow-up rows)

`config.py` also carries keys for **FMP, Finnhub, FRED, Polygon** (news, macro,
options-flow, parsed insider transactions). Todo-3 L.3 covered the single-user
posture; none has a product-shape determination. Before any of them ships in a
product — especially as a yfinance replacement (§6) — it gets its own section
with the same four questions (personal-tier automated use, commercial use,
derived-signal redistribution, rate limits). `[VERIFY-F1]` Open a follow-up
row per vendor at the point it's chosen; FRED is likely public-data-friendly,
the other three are commercial APIs with tiered terms.

---

## Summary table

| Vendor | Shape (a) BYO-keys posture | Shape (b) hosted shared-cycle posture | Action | Owner | Status |
|---|---|---|---|---|---|
| Quiver Quant | Likely OK — each user subscribes; confirm personal tier allows third-party client use (Q1) | Needs commercial/redistribution license; get quote (Q2, Q3) | Write Quiver sales; file ToS excerpt + quote | operator | OPEN |
| Alpaca trading API | Clean — user's own account/keys; confirm automated-client terms (A3) | OAuth app likely required; start application in GA-0 if (b) chosen (A2) | File accepted terms; ask partnerships in writing | operator | OPEN |
| Alpaca market data | OK on user's own entitlement; document IEX-vs-SIP (A1) | SIP redistribution restricted; plan per-tenant entitlement, not shared-cycle quotes (A1) | Read the signed data agreement; confirm feed in use | operator | OPEN |
| Robinhood agentic MCP | Per-user auth maybe OK; single trade-capable scope — warn loudly in docs (R1, R2) | Likely CANNOT ship at all — plan feed REMOVAL (R3) | Capture consent-flow terms; ask about read-only scope | operator | OPEN |
| SEC EDGAR | OK — public domain; require real per-user `SEC_USER_AGENT` (E1) | OK — public domain; rate limits into GA-4.6 capacity model (E1, E2) | Read fair-access rules; fix placeholder UA default | operator | OPEN (easiest close) |
| Anthropic | BYOK; Usage Policy determination still required (AN1 = GA-0.5) | Metered org key concentrates policy + spend exposure on us (AN1, AN2) | Obtain written determination via GA-0.1a/GA-0.5 | operator → attorney | OPEN (gates GA-4) |
| yfinance / Yahoo | Not licensed — gray zone we'd be shipping users into; replace (Y1) | Not shippable; replace, nothing to license (Y1, Y2) | Migration list to licensed feeds; cost into GA-0.2 | operator | OPEN (engineering + desk) |
| FMP / Finnhub / FRED / Polygon | Undetermined — follow-up row when chosen (F1) | Undetermined — follow-up row when chosen (F1) | Open per-vendor row before product use | operator | DEFERRED |

**Close-out rule:** a row moves to CLOSED only when the written artifact
(quote, email, ToS excerpt with date) is filed in `docs/` and the summary-table
cell cites it. GA-7.2 requires the closed set on file before launch.
