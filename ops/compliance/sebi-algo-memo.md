# SEBI / Zerodha retail-algo rules — verification memo

**Task:** M8.2 · **Written:** 2026-08-08 · **All sources retrieved:** 2026-08-08 · **Status: research only.**
Satisfies the §6 "SEBI checkpoint" of [EXECUTION_PLAN.md](../../EXECUTION_PLAN.md).

> **Nothing was acted on.** No API key was created, no terms were accepted, no broker was contacted, no
> money was spent, no order was placed. Accepting third-party terms and anything with legal exposure are
> reserved to the owner (AGENTIC_CONTEXT §3.7, §3.8). Every item in §7 below is an owner decision.
> This memo is a factual reading of published circulars — it is not legal advice.

---

## 1. Bottom line

| Question | Answer as of 2026-08-08 |
|---|---|
| Is the retail-algo framework live? | **Yes.** Fully applicable to all stock brokers w.e.f. **April 01, 2026** (SEBI/HO/MIRSD/MIRSD-PoD/P/CIR/2025/132, para 8). |
| Are our orders "algo" orders? | **Yes, unavoidably.** *All* orders received via a broker API are algo orders by definition, regardless of frequency (NSE/INVG/69255 Annexure I para 2.8). "EOD frequency" buys no exemption from being an algo. |
| Does broker-side / exchange algo **registration** apply to us? | **No — on order-rate grounds.** Registration of a client-developed algo is required *only* if it crosses the threshold order-per-second (SEBI Feb 04 2025 circular, para I(c)). We fall in the exchange's named exception category "Tech Savvy retail investor (within OPS threshold)" (NSE/INVG/73992 §8.4.1). Below-threshold orders carry the generic algo ID `99999` supplied by the broker (§8.4.7). |
| What is the threshold? | **10 orders per second, per exchange/segment**, measured on the calendar clock second of the broker's server (NSE/INVG/73992 §8.3.2.2.2, §8.3.2.4). |
| Our order rate against it? | Design envelope **≤2 OPS peak, ≤50 orders/day** → **20% of the OPS threshold, 1% of Zerodha's daily order cap.** See §5. |
| Static IP? | **Mandatory**, and it is the one requirement that binds us regardless of order rate. Zerodha: "effective 1 April 2026, you must have a static IP for API-based order placement." |
| Blocking issue? | **Yes, one — and it is not the order rate.** Kite Connect's own terms say the APIs "are not meant for placing fully automated trades (without manual intervention)". See §4.3 and open question **Q1**. |

---

## 2. Primary sources

Every row was fetched from the issuer's own site on 2026-08-08. No blog, vendor explainer, or news summary
is relied on for any statement in this memo.

| # | Document | Ref. no. | Date | URL |
|---|---|---|---|---|
| S1 | SEBI circular — *Safer participation of retail investors in Algorithmic trading* | SEBI/HO/MIRSD/MIRSD-PoD/P/CIR/2025/0000013 | 2025-02-04 | [page](https://www.sebi.gov.in/legal/circulars/feb-2025/safer-participation-of-retail-investors-in-algorithmic-trading_91614.html) · [PDF](https://www.sebi.gov.in/sebi_data/attachdocs/feb-2025/1738665456458.pdf) |
| S2 | SEBI circular — extension of timeline for formulation of implementation standards | — | 2025-04-01 | [page](https://www.sebi.gov.in/legal/circulars/apr-2025/extension-of-timeline-for-formulation-of-implementation-standards-pertaining-to-sebi-circular-on-safer-participation-of-retail-investors-in-algorithmic-trading-_93166.html) |
| S3 | SEBI circular — extension of implementation timeline + glide path | SEBI/HO/MIRSD/MIRSD-PoD/P/CIR/2025/132 | 2025-09-30 | [page](https://www.sebi.gov.in/legal/circulars/sep-2025/extension-of-timeline-for-implementation-of-sebi-circular-dated-february-04-2025-on-safer-participation-of-retail-investors-in-algorithmic-trading-_96979.html) · [PDF](https://www.sebi.gov.in/sebi_data/attachdocs/sep-2025/1759232056254.pdf) |
| S4 | NSE circular — **Implementation Standards** | NSE/INVG/67858 | 2025-05-05 | [PDF](https://nsearchives.nseindia.com/content/circulars/INVG67858.pdf) |
| S5 | NSE circular — **Detailed Operational Modalities** (+ annexures) | NSE/INVG/69255 | 2025-07-22 | [ZIP](https://nsearchives.nseindia.com/content/circulars/INVG69255.zip) |
| S6 | NSE circular — corrigendum / update to S5 (algo-provider empanelment criteria) | NSE/INVG/70309 | 2025-09-19 | [ZIP](https://nsearchives.nseindia.com/content/circulars/INVG70309.zip) |
| S7 | NSE **FAQ** — Safer participation of retail investors in Algorithmic trading | — | 2025-11-03 | [PDF](https://nsearchives.nseindia.com/web/sites/default/files/inline-files/FAQ_Retail%20Algo_03112025_NSE.pdf) |
| S8 | NSE **consolidated NNF circular** — §8 is the current consolidated algo text; replaces NSE/MSD/67753 (2025-04-29) | NSE/INVG/73992 | **2026-04-30** | [ZIP](https://nsearchives.nseindia.com/content/circulars/INVG73992.zip) |
| Z1 | Zerodha support — *What is a static IP and how to add one to your developer account?* | — | undated page, current 2026-08-08 | [link](https://support.zerodha.com/category/trading-and-markets/general-kite/kite-api/articles/static-ip) |
| Z2 | Kite Connect v3 docs — *Exceptions and errors → API rate limit* | — | undated page, current 2026-08-08 | [link](https://kite.trade/docs/connect/v3/exceptions/) |
| Z3 | Kite Connect **Terms and conditions** | — | **no version or date stamp on the page** | [link](https://kite.trade/terms/) |
| Z4 | Kite Connect pricing (Personal free / Connect ₹500 per month) | — | undated page, current 2026-08-08 | [link](https://zerodha.com/products/api/) |

Currency check performed: SEBI's own "Algorithmic" circular index shows **no SEBI circular on this subject
after 2025-09-30**. A subject-line scan of the NSE circular index from 2026-01-01 to 2026-08-08 found no
change to the framework beyond S8 (the April 30, 2026 consolidation) and routine algo-provider empanelment
notices. **The
10 OPS threshold has not been revised.** S3 para 1 also references an intermediate extension circular dated
2025-07-29 (Aug 01 → Oct 01, 2025), which is superseded by S3 and not relied on here.

---

## 3. What the framework actually requires

### 3.1 Timeline (why this is live today, not a future problem)

S1 (2025-02-04) set the framework, effective 2025-08-01 → pushed to 2025-10-01 → S3 (2025-09-30) replaced
the hard date with a broker glide path (algo registration applications by 2025-10-31, registrations by
2025-11-30, mock participation by 2026-01-03, non-compliant brokers barred from onboarding new API-algo
clients from 2026-01-05) and set the final date: **"W.e.f. April 01, 2026, algo framework specified in
circular dated February 04, 2025 along with implementation standards and detailed operational modalities
(issued by exchanges) will be applicable for all stock brokers."** (S3 para 8.)

### 3.2 The threshold, and what sits on each side of it

- **TOPS = 10 orders/second**, "per exchange/segment", adjustable by exchanges with notice; "the threshold
  will be applied basis the calendar clock second of the broker server" (S8 §8.3.2.2.2 and §8.3.2.4; same
  text in S4 §B.2 and §F).
- **Below TOPS:** "the client will not be required to register for algorithmic trading from the broker's
  system" (S8 §8.3.2.2.2). Orders are still tagged as algo; the broker sends algo ID `99999` and `0` as the
  13th digit of the 15-digit NNF ID, through one predefined API key reserved for non-registered algos
  (S8 §8.4.7, §8.3.2.1.4).
- **Above TOPS:** the client must register the algorithm with each exchange, via the broker, and orders carry
  an exchange-issued algo ID (S8 §8.3.2.3, §8.4.6).
- **The exception is named explicitly for us:** members must register each algo with the exchange "with the
  only exception of 'Tech Savvy retail investor (within OPS threshold)'" (S8 §8.4.1).
- **The broker enforces it, not us:** brokers must "reject/ not accept/not process any orders exceeding the
  OPS limit" and must be able to monitor the threshold (S8 §8.3.2.2.5–6). A broker may also set a *lower*
  per-client limit (S8 §8.3.2.4).
- **Self-developed algos are for self and family only** — "self, spouse, dependent children and dependent
  parents", never other investors (S1 para I(c)).

### 3.3 What binds us regardless of order rate

| Requirement | Source | Effect on this system |
|---|---|---|
| Every API order is an algo order and must be tagged | S5 Annexure I §2.8; S7 Q8 | No "it's only EOD" exemption. Basket orders included. |
| **Static IP mandatory** for API access; client's own IP for client-generated algos | S8 §8.3.2.1.1, §8.3.2.1.5; S7 Q3/Q6 ("required only in case of Tech savvy Investor using API" — that is us) | Deployment host needs a fixed public IP. |
| Max **2 static IPs** (primary + optional secondary); changeable **at most once per calendar week** | S8 §8.3.2.1.2, §8.3.2.1.6 | Host/ISP migration is a compliance event, not a shrug. |
| Static IP shareable only within family (as defined in SEBI/HO/MIRSD/MIRSD-PoD1/P/CIR/2024/169, 2024-12-03) | S8 §8.3.2.1.7 | Never share the IP or key. |
| **All API sessions compulsorily logged out every day** before the next trading day; OAuth + 2FA only | S8 §8.3.2.1.8, §8.4.8.9, §8.4.8.21 | **A daily human-in-the-loop authentication step exists by regulation.** The daily EOD loop cannot hold a perpetual session. |
| **"Algo orders with order type as Market Order are not permitted"** | S8 §8.1.12 (commodity segment additionally bars IOC — S8 §8.2.1, S5 §11.4.6) | KiteBroker must not place bare MARKET orders. |
| Broker RMS validates every API order (quantity, price range, order value) before release | S5 §11.2–11.4 | Our A8 rails are *in addition to*, never instead of, broker RMS. |
| Tech-savvy client owns the strategy logic and its RMS consequences entirely | S5 §13.1.3 | The owner carries the outcome; the broker carries the plumbing. |
| Audit trail of all API orders retained 5 years, user identifiable | S8 §8.4.8.3 | Our append-only journal (invariant #9) is aligned, not a substitute. |
| Individual tech-savvy clients are **not** required to join monthly mock sessions | S7 Q9 (cf. S5 §12.1, which applies to *registered* retail algos) | One less operational obligation. |
| Multi-tenant / offering this to others would require **algo-provider empanelment**, and a non-replicable (LLM-driven) strategy would be a **Black Box** algo requiring **Research Analyst registration** | S1 paras III & V; S7 Q4 | Confirms EXECUTION_PLAN §11: multi-tenant is a different compliance regime entirely, not a deployment flag. |

### 3.4 One textual ambiguity, resolved

S8 §8.3.2.2.3 reads: below-threshold algo orders "shall also be tagged as 'Algo'. However, such orders tagged
as algos by the client would require registration with the Exchange and a generic algo ID shall be provided
by the Exchange for such Algos." Read alone this appears to contradict §8.3.2.2.2 ("will not be required to
register"). The resolution is §8.4.1 (tech-savvy-within-threshold is the *only* exception to per-algo
registration) plus §8.4.7 (non-registered client algos ride the generic ID `99999`): the "registration" in
§8.3.2.2.3 is the **broker's** one-time Client Direct API product registration, not a per-client-strategy
registration. In every reading the obligation is the broker's, not ours. Logged as **Q4** anyway.

### 3.5 One conflict, resolved only by an FAQ

S8 §8.4.8.16 says "All Retail Algorithms, including those provided by empanelled Algo providers should be
hosted on brokers' cloud servers/environment", and S5 §14 says "all the strategies shall be run on the
brokers servers. The order messages shall be originated from brokers server." Taken literally that is
incompatible with self-hosting our analyst on our own VM. S7 Q5 carves it out: "Tech Savvy client is required
to host the Algo using a static IP at their end where Algo logic resides instead of hosting the Algo on
Trading Member's cloud server." S8 itself carries the same note ("the requirement of static IP is restricted
only to 'Tech-savvy' Retails investors"). The carve-out is what makes this system legal to self-host — and it
lives in an FAQ whose own disclaimer says the circulars are "final and binding" in case of inconsistency.
Logged as **Q3**.

---

## 4. The broker layer (Zerodha / Kite Connect)

### 4.1 Static IP

Z1, verbatim: "As per regulations, effective 1 April 2026, you must have a static IP for API-based order
placement. This requirement applies to all API-based order placement as per NSE/SEBI algorithmic trading
regulations. The WebSocket market data stream and other APIs, such as orderbook and positions, can continue
to be accessed from any IP address." At least one primary IP, up to two, one modification per calendar week,
sharable only with immediate family via a single developer account.

### 4.2 Rate limits (Z2, verbatim numbers)

| Limit | Value |
|---|---|
| Order placement endpoint | 10 req/second |
| All other endpoints | 10 req/second |
| Quote endpoint | 1 req/second |
| Historical candle endpoint | 3 req/second |
| Orders per minute | 400 |
| Orders per second | 10 |
| Orders per day, per user/API key, across all segments and varieties | 5,000 |
| Modifications per order | 25, then cancel and re-place |

The broker's order limits are numerically identical to the exchange threshold, so complying with Kite's
documented limit *is* complying with TOPS. Exceeding it returns HTTP 429.

### 4.3 Terms and conditions (Z3) — the actual gate

Clause 2(e), *Automated trades*, verbatim: **"The APIs are not meant for placing fully automated trades
(without manual intervention). If you wish to use the APIs for full automation, you should seek necessary
approvals from the exchange. Zerodha may provide the necessary assistance in obtaining approvals."**

Also relevant in Z3:

- Clause 2 — API use is for personal customisation or platform-building; "You are responsible for ensuring
  You adhere to these platform guidelines and regulations, and seeking appropriate regulatory approvals if
  necessary."
- Clause 2(a) — live market data obtained via Kite Connect "cannot be displayed to the public at large";
  no reverse-engineering or redistribution. **This is a second, independent constraint on the archives /
  data-redistribution question in EXECUTION_PLAN §10** — any Kite-sourced data must stay out of published
  archives. (Our L0 is NSE/BSE-sourced, not Kite-sourced, which keeps the two questions separable.)
- Clause 2(b) — Zerodha may impose limits; exceeding them requires "express written consent from Zerodha".
- Preamble — "You agree to any and all changes to the Terms without specific communication from Zerodha, by
  Your continuing usage of the APIs"; continued use is acceptance.
- The page carries **no version number and no last-updated date**, so "the terms as of today" is only
  evidenced by a dated local capture. Logged as **Q6**.

Note the asymmetry that makes Q1 real: the *regulator* permits an unregistered self-developed algo below
10 OPS; the *broker's contract* says its API is not meant for fully automated trading and points at exchange
approval for full automation. Regulatory permission does not override a contract term, and this system is by
design fully automated order placement.

### 4.4 Cost (Z4)

"Personal (Free)" tier covers order/GTT/alert management, margin computation and portfolio; "Connect" is
₹500/month per API key and adds WebSocket streaming plus historical candle data. Since this platform's prices
come from NSE/BSE EOD files (§4.1) and not from the broker, the free tier may be sufficient for execution
alone. Spending money is the owner's call (AGENTIC_CONTEXT §3.9) — noted, not acted on.

---

## 5. Assessment: this system's order rate vs the stated thresholds

### 5.1 What the system does

EOD decisions produce **staged** orders that execute in the next session (EXECUTION_PLAN §6). There is no
intraday order generation, no quoting, no order-book reaction, no slicing engine, no cancel/replace loop.
The reference case (§5.2, and the `ai_robotics` fixture) is ₹10k/month SIP on the 1st, ≥8 holdings, 15% max
position, 30% tactical dial, T2 deep review monthly, staged exits. Segment: NSE cash, delivery.

### 5.2 Order counts, derived from the plan

| Event | Frequency | Orders |
|---|---|---|
| T0 daily check, nothing triggered (the overwhelmingly common day) | ~20 days/month | **0** (journal heartbeat only) |
| Monthly SIP deployment | 1 day/month | ≤10 (₹10k cannot be usefully split further) |
| T2 monthly review → tactical rotation | 1 day/month | ≤10 (sells + buys inside a 30% sleeve) |
| Break-condition staged exit | rare | 1 per position per day, over several days |
| Cash parking / unparking (LIQUIDCASE) | with SIP or exits | ≤2 |
| Kill-switch cancellations | never, by design | ≤ open orders |
| **Pessimistic worst day** (SIP + rotation + a staged exit + parking, all on one date) | ~1 day/quarter | **~25** |
| **Design envelope used below** (2× the worst realistic day) | — | **50 orders/day** |

### 5.3 Against the thresholds

| Threshold | Value | This system | Utilisation | Headroom |
|---|---|---|---|---|
| Exchange TOPS (S8 §8.3.2.4) | 10 orders/sec per exchange/segment | **2/sec** design cap (token bucket, aligned to wall-clock seconds) | **20%** | **5×** |
| Kite orders/second (Z2) | 10 | 2 | 20% | 5× |
| Kite orders/minute (Z2) | 400 | ≤50 (entire day's envelope inside one minute) | 12.5% | 8× |
| Kite orders/day per key (Z2) | 5,000 | ≤50 | **1%** | 100× |
| Kite modifications/order (Z2) | 25 | 0–1 (staged orders are placed, not chased) | ≤4% | — |
| Registration trigger (S1 I(c), S8 §8.4.1) | crossing TOPS | never crossed | — | — |

Two properties make the assessment robust rather than merely arithmetic:

1. **The measurement window is the broker server's calendar clock second** (S8 §8.3.2.2.2). The risk is not
   average rate but a burst straddling a second boundary. A token bucket capped at 2 orders per wall-clock
   second makes 10 unreachable *by construction*, not by luck of network latency.
2. **Scaling does not change the picture.** The threshold is a rate, not a volume. Ten cases on one account
   would still be ~500 orders/day (10% of the daily cap) at the same 2 OPS peak, because they share one
   paced order gateway.

**Conclusion: this system sits, with an order of magnitude to spare, in the "Tech Savvy retail investor
(within OPS threshold)" category. Exchange/broker algo registration is not triggered by our order rate.
The order rate is not the compliance risk here — Q1 (broker T&C on full automation), Q2 (static IP host) and
Q5 (daily session logout) are.** The EXECUTION_PLAN §10 risk-register line ("order-rate stays trivially low")
is confirmed as accurate.

### 5.4 Engineering consequences (recorded here; implemented by M8.1 / M8.3, not by this task)

1. **Paced order gateway.** KiteBroker needs a wall-clock-second token bucket, default cap 2 OPS, hard
   ceiling below 10, plus a daily order counter. It should refuse rather than queue-and-burst, and journal
   every refusal. This belongs on the single order path so paper and real share it (invariant #5).
2. **No bare MARKET orders** (S8 §8.1.12). Staged EOD orders should be LIMIT; if a market-type order is ever
   used, Kite requires non-zero market protection. This interacts with the SimBroker fill model
   ("next-day open or conservative VWAP band", §6) — the two must agree, or paper and live diverge.
3. **Static-IP-pinned host.** §8.1's single VM must carry a fixed public IP, and an IP change is limited to
   once per calendar week (S8 §8.3.2.1.6). Worth a runbook line before M8.3.
4. **Daily authentication is a designed-in manual step**, not a bug to engineer around: sessions must be
   logged out daily and re-established via OAuth + 2FA (S8 §8.3.2.1.8). The daily loop must treat "not
   authenticated" as a first-class, journaled, non-trading state — the same shape as `SKIPPED_DATA_RED`.
5. **Kite-sourced data must never reach published archives** (Z3 clause 2(a)).

---

## 6. What is *not* required of us (and why)

- **No algo registration** — order rate stays below TOPS (§5.3).
- **No algo-provider empanelment** — that applies to entities providing algos to others (S1 III); we trade
  one own account.
- **No Research Analyst registration** — the RA requirement attaches to *algo providers* distributing black-box
  algos (S1 V, S7 Q4), not to a person running their own non-replicable strategy for themselves.
- **No white-box/black-box categorisation filing** — the 5-level category disclosure is the member's
  application content for registered algos (S8 §8.4.1–8.4.2); the tech-savvy-within-threshold case is the
  carved-out exception.
- **No monthly mock session participation** — S7 Q9.
- **No system audit by us** — audit obligations sit on the trading member (S8 §8.4.8.11).

Each of these flips the moment the system serves anyone other than the owner and immediate family, or exceeds
10 OPS. Both are bright lines, and both are worth an explicit rail rather than a memo line.

---

## 7. Open questions for the owner

Owner items. No agent has acted, will act, or may act on any of them.

**Q1 — Broker T&C vs full automation (blocking, decide before M8.3).**
Kite Connect's terms say the APIs "are not meant for placing fully automated trades (without manual
intervention)" and direct anyone wanting full automation to "seek necessary approvals from the exchange"
(Z3 §2(e)). This system is fully automated order placement by design. SEBI's framework permits an
unregistered self-developed algo below 10 OPS, but that permission does not amend a contract with the broker.
*What would resolve it:* a written answer from Zerodha (support ticket or the Kite Connect forum) on whether
paced, below-TOPS, self-developed automated order placement for one's own account is within the terms as they
apply after 2026-04-01, and if not, exactly what "approval" is meant. Keep the reply; it is the compliance
artefact for the M8 gate and the graduation packet.
*Interim posture that costs nothing:* keep the human-in-the-loop confirm that M8.1 already mandates — a staged
order set that a human releases each day is not "without manual intervention" on any reading.

**Q2 — Static IP: where does the production host live?**
Mandatory for API-based order placement from 2026-04-01 (Z1), changeable once per calendar week
(S8 §8.3.2.1.6). Decide between an ISP static IP at home and a cloud VM with an elastic IP, knowing the
weekly-change limit makes migration a planned event. This also fixes where L0 and Postgres live, so it is a
deployment decision, not a networking detail. Costs money → AGENTIC_CONTEXT §3.9.

**Q3 — Self-hosting rests on an FAQ, not on circular text.**
S8 §8.4.8.16 and S5 §14 require retail algos to run on broker servers; S7 Q5 exempts tech-savvy clients who
self-host behind a static IP, and S8's own note repeats it. The exemption is what makes this architecture
lawful, and the FAQ's disclaimer subordinates itself to the circulars. *What would resolve it:* the same
broker/exchange confirmation sought in Q1, in writing, plus a dated local copy of S7 and S8 kept in this
directory.

**Q4 — "Generic algo ID" wording (low risk, confirm in passing).**
S8 §8.3.2.2.3 can be read as requiring registration for below-threshold algos, contradicting §8.3.2.2.2 and
§8.4.1. Reading adopted here: the registration is the broker's Client Direct API product registration and our
orders ride generic algo ID `99999` (§8.4.7). Worth one line of confirmation from Zerodha; the obligation is
theirs in every reading, so this does not block.

**Q5 — Daily authentication policy.**
"All API sessions shall be compulsorily logged out every day before the start of the next trading day"
(S8 §8.3.2.1.8), with OAuth + 2FA. Decide how the daily login happens and who performs it. Note that a
credential/TOTP handling decision is the owner's (AGENTIC_CONTEXT §3.4, §3.7) — agents will not touch it, and
the daily loop will treat an unauthenticated session as a journaled no-trade state.

**Q6 — Terms are undated and change silently.**
Z3 carries no version or date, and its preamble makes continued use acceptance of unannounced changes. Suggest a
quarterly dated capture of Z3, Z1 and Z2 into `ops/compliance/`, plus a re-read of this memo, so a term that
changes under us is detected rather than discovered. Candidate for the C-track continuous work.

**Q7 — Product-stage regime (not now, but do not forget).**
Serving anyone beyond self + immediate family converts this into algo-provider territory: exchange
empanelment, and — because an LLM-driven thesis is not replicable — Black Box classification with Research
Analyst registration for the provider (S1 III & V, S7 Q4). Combined with the SEBI RA/RIA path already deferred
in EXECUTION_PLAN §11, this is a workstream, not a checkbox. Recorded so the M8 evidence does not get
mistaken for product-stage clearance.

---

## 8. Revisit triggers

Re-verify this memo if any of these occur; nothing here is settled permanently.

- Any SEBI circular on algorithmic trading after 2025-09-30 (index:
  <https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListingAll=yes&search=Algorithmic>).
- Any NSE INVG/MSD circular revising TOPS, the Client Direct API category, or the NNF consolidated §8
  (S8 is itself a consolidation and will be superseded).
- A change to Z3 (silent by clause 1 — hence Q6).
- The system's own design changing shape: intraday decisions, order slicing, chasing/modifying orders,
  more than one account, or any tenant other than the owner.
- Before the first real order and again before graduation (decision #8): re-read §5.3 against measured
  numbers from the live-order sessions, not the design envelope used here.
