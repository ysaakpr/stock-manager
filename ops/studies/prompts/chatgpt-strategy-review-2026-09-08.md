# Copy-paste prompt — external strategy review (ChatGPT)

*Everything between the two `=====` rules is the prompt. Paste it whole. Numbers in it were measured
on this box between 2026-09-06 and 2026-09-08 and each one is traceable to a report named inline.*

=====================================================================================

# ROLE

You are acting as an independent quantitative reviewer and portfolio-strategy architect for a
**single-owner Indian equity fund run as an autonomous software system**. I own the fund, I wrote
(with AI pairing) the platform that runs it, and I want a hard, numerate, adversarial review plus a
concrete strategy design.

I do not want encouragement, and I do not want a factor-investing lecture. Everything below is
measured from my own data lake and my own backtests. **Engage with these specific numbers.** Where
you think a number is wrong, unreliable or unfalsifiable, say so and say what measurement would
settle it. Where you disagree with a decision already taken, argue against it explicitly.

Assume I am technically competent (I can implement anything you specify), that compute and
engineering time are cheap, that **data I do not already have is expensive**, and that my capital is
small and growing monthly.

---

# 1. WHAT I ACTUALLY WANT (the mandate)

I want a **personal fund manager**: an AI-based automated portfolio-management system that manages
my brokerage account end to end, with me ratifying policy rather than making daily decisions.

Requirements, in my own words:

1. **Monthly inflow.** I push a fixed amount into the brokerage account every month. The system must
   look at total balance + the new inflow and plan deployment for the best outcome — i.e. a monthly
   (or better-than-monthly, if the evidence supports it) rebalancing/deployment decision.
2. **Automated cash reserve.** Idle cash sits in liquid funds / liquid ETFs on the same broker path,
   not in a bank account, and is deployed on signal rather than on a calendar.
3. **The monthly inflow must be accounted for properly** in performance measurement (money-weighted
   return, not a fake time-weighted number on a growing book).
4. **Dividend and tax optimisation.** Optimise for dividend income where it is worth it, and manage
   the tax actually payable **year on year** (Indian FY, 1 Apr – 31 Mar), not just pre-tax gross
   return.
5. **Socio-economic trend / theme and regime swing.** The system should learn structural trends and
   rotate between booming themes and regimes. Concretely: the AI boom was identifiable 2–3 years
   before it was consensus, and a system worth having would have taken a small position into that
   theme early. I want that capability, honestly built and honestly tested.
6. **End-to-end decision support.** I want signals, *validated* signals, a record of the decisions
   taken, and an assessment of how well each decision is likely to go — so that when I am asked to
   ratify something, the evidence is in front of me.
7. **Everything justified by historical backtest.** The definitive method is out-of-sample historical
   testing. I want the best achievable design, proven that way, with the failure modes stated.

My stated bar so far has been a backtested **>25% XIRR** — see §6, where that bar has *not* been met
except in one flattering window. Part of your job is to tell me whether that bar is realistic at all
for this capital, this universe and this data, and if not, what the honest achievable target is,
after tax and after friction, with a confidence interval.

**Capital reality:** small book today (order of ₹10 lakh–₹1 crore), monthly SIP-style contributions,
growing over years. I trade Indian cash equities through Zerodha (Kite Connect). Long-only, delivery,
EOD decisions. No intraday data, no shorting, no derivatives today.

---

# 2. HARD CONSTRAINTS (do not propose designs that violate these)

## 2.1 Governance model (the constitution of the system)

- **Agent proposes policy → human ratifies policy → agent executes autonomously inside ratified
  policy.** The human approval sits at the *policy* layer, never inside the daily loop.
- **Thesis + hard rails.** Deterministic risk rails (position/sector caps, minimum holdings,
  drawdown-triggered review) that the agent **cannot override**. Rails are code, never a model
  judgement; unit tests AST-parse the rails package and fail if any function grows a parameter named
  `override`/`bypass`/`force`/`skip`, or if the rails package imports the LLM module.
- **Paper and real money run the identical decision code path**; only the broker adapter differs.
- **Every decision is journaled, including no-ops**, append-only. The journal is the product: for
  each action, the evidence seen, the conditions evaluated, the reason acted.
- **Break conditions are drafted by the model and ratified by me**, then evaluated mechanically.

## 2.2 Data invariants (enforced in code and tests)

1. **L0 (raw payloads) is immutable**, checksummed, write-once. Every derived value must be
   re-derivable from L0 alone.
2. **ISIN is the only join key.** A function that takes a `symbol` and queries prices is a defect.
3. **No adjusted prices in L1.** Adjustment factors are applied on read or materialised into L2;
   execution, sizing and marks always use *raw* prices.
4. **One cost model**, shared by the simulated broker and the backtest.
5. **No data with `knowable_date > decision_date` may reach a decision** (point-in-time; there is an
   automated PIT-leak test).
6. **Restated fundamentals are physically quarantined from backtests** (monitoring-only).
7. **Clock is injected**; replay must be byte-identical for identical inputs.
8. **Red data means no trading** — the daily loop reads the sync-status API first; not green means a
   journaled `SKIPPED_DATA_RED` and no orders.

## 2.3 India-specific reality

- **Costs, per delivery trade:** ~0.223% statutory (STT 0.1% each side, 0.015% stamp, exchange/SEBI
  fees, GST) plus ~0.22% modelled slippage → **~0.45% round trip**, plus a flat DP charge on every
  sell. Zerodha brokerage on delivery is zero, so statutory charges dominate. The cost model in the
  repo is time-varying but its rate card starts **2017-07-01** and encodes only 2 states (KA, MH).
- **Capital-gains tax:** STCG (≤12 months) **20%**, LTCG (>12 months) **12.5%** above the annual
  exemption, from 2024-07-23. Earlier regimes differ (LTCG exempt to 2018, then 10%). Dividends are
  taxed at slab in the recipient's hands since FY2020-21 (DDT abolished), with TDS above ₹5,000.
- **Settlement T+1** today; India was T+3 to 2003, T+2 2003–2022, and T+1 was phased in per security
  by market-cap rank between 2022-02-25 and 2023-01-27.
- **Broker T&C:** Kite Connect's terms say the APIs are "not meant for placing fully automated trades
  (without manual intervention)". My interim posture is therefore a **staged order set that a human
  releases each day** — policy discretion stays fully pre-ratified, but execution has one human click.
  Market orders are not permitted for algo orders.
- **Compliance:** single-tenant, own money, so no SEBI RA/RIA registration today. A non-replicable
  LLM-driven strategy would be a "black box" algo if ever offered to others; the mechanical path must
  stay replicable.

---

# 3. THE SYSTEM AS BUILT

Solo-maintained Python monolith. Python 3.12 (uv), Postgres (docker-compose), Parquet lake.
`make check` = format + lint + `mypy --strict` on new packages + pytest, and is the gate on every
task. Money is `Decimal` everywhere (a float in the cost model is a defect); dates are
`datetime.date`, timestamps tz-aware Asia/Kolkata; structured logging.

```
dataplatform/   ingest/ identity/ corpactions/ store/ quality/ query/ archives/ status/   ~45,400 LOC
analyst/        cases/ interview/ mapper/ thesis/ monitor/ rotation/ cash/ rails/ journal/ ~16,700 LOC
backtest/       replay engine, policies/, sweep, walk-forward verdict, fitted forecast     ~12,200 LOC
execution/      broker.py sim_broker.py kite_broker.py costs/ recon.py kill_switch.py       ~3,500 LOC
accounting/     token/cost metering                                                            ~600 LOC
orchestrator/   the autonomous build system itself (not product code)                        ~1,400 LOC
tests/          unit/ integration/ golden/ fixtures/                                        ~57,800 LOC
data/           L0 (immutable raw) / L1 (normalised) / L2 (adjusted)  — 7.0 GB on disk
```

**System 1 (data platform)** — free-source NSE+BSE EOD platform: dual-format bhavcopy parsers,
delivery data, identity master (ISIN-keyed with symbol-change history), corporate-action ingestion +
reconciliation + adjustment-factor chains, an L2 back-adjusted price store, a sync state machine with
a status API, data-quality sentinels, PIT query layer, downloadable archives.

**System 2 (analyst agent)** — case (= themed fund mandate) lifecycle with an interview flow, theme
mapper with purity scores, thesis engine with ratification, versioned policy sets (append-only), a
deterministic rails engine, a rotation dial (0–100% tactical sleeve), a cash manager that parks idle
cash in a liquid ETF the same session it arrives and deploys it only against a ratified thesis or a
journaled tactical rationale, tiered monitoring (T0 mechanical → T1 triggered LLM review → T2
scheduled deep review), a decision journal, and a SimBroker/KiteBroker split behind one interface.

**Backtest/replay (X2)** — one-pass windowed lake reads, a shared cost model, SIP simulation, XIRR
portfolio accounting, benchmark comparison, a 46-arm strategy sweep, a walk-forward verdict runner,
and a fitted forward-return forecast policy.

**What has never actually run** (this matters more than the LOC):

- **No scheduler runs anything.** The app container runs only the read-only status API;
  `job_run` has 0 rows; `/health` reports `scheduler: NEVER_RAN`. Every "daily" or "weekly" job is
  in practice a manual campaign. The registered daily EOD job also fetches exactly **one** source.
- **System 2 has never made a decision outside a test.** `decision_journal`, `thesis`, `policy_set`,
  `case_`, `order_`, `token_usage` all hold **0 rows**. The paper loop exists only inside an
  integration test.
- **No language model has ever run.** The theme mapper runs on a deterministic `StubLLM`; there is no
  API key configured; T1/T2 LLM review has never met a real model. The "AI" is designed, coded,
  tested behind a stub, and unplugged.
- **The strategy search terminates in markdown.** No analyst module depends on any backtest result;
  the best strategy I have cannot currently be traded by my own system.
- **Alerts are log lines** nobody tails (email/Telegram alerters exist, unconfigured).
- **The price sentinel has never examined a production session.**

---

# 4. THE DATA — MEASURED, NOT CLAIMED

## 4.1 What is in the lake

- **L0:** 96,097 raw payloads + 96,097 sidecar receipts (1:1, zero orphans), 6.3–7.0 GiB, checksum
  sweep 96,097 checked / **0 defects**. 13 of 30 registered sources have ever been materialised; 77%
  of bytes are XBRL filings.
- **NSE daily prices:** 100% of every session 2016-09-02 → 2026-09-01 against the real NSE holiday
  calendar, **zero unexpected dates**. A deep backfill campaign on 2026-09-08 added 2006-01-02 →
  2016-09-01 (2,637 requests, **0 failures, 0 404s**). Era splices are exact to the session.
- **After that campaign, L1 `prices_raw` spans 2011-06-22 → 2026-09-04 (3,763 session partitions).**
  Everything fetched **before 2011-06-22 is quarantined, not usable**: NSE bhavcopy carries **no ISIN
  column before 2011-06-22** (pinned to the day: 2011-06-21 = 11 columns, 2011-06-22 = 13 with ISIN),
  and ISIN is the only legal join key. That quarantine is **1,358 sessions / 1,652,572 rows** covering
  2006–mid-2011.
- **L2 `prices_adjusted`:** 3,349 ISINs, NSE only. **No BSE row exists in L2 at all.**
- **NSE delivery %:** 2016-09-02 → today, complete over its span, two format eras spliced exactly.
  Delivery is present on ~58–59% of all NSE rows and **78.4% of NSE `EQ` rows** after a rebuild from
  L0 (it was 0% before). Free history reaches 2002 but is not fetched.
- **Corporate actions:** 47,887 reconciled actions / 3,624 ISINs, rows reaching 2000, **BSE-only
  before 2016** (35,382 BSE vs 12,505 NSE); 1,544 splits and 1,676 bonuses; adjustment factors on
  1,823 ISINs. A 7-case golden suite pins the factor convention and is direction-sensitive.
  **Delisting events stop at 2003-03-27 — zero recorded in 23 years**, despite 2,311 securities
  marked DELISTED. We know *who* died, not *when*.
- **Fundamentals (PIT, from NSE XBRL filings):** 1,349,562 facts / 102,650 filings / 2,291 ISINs,
  filings 2018-05-21 → 2026-09-05. Honestly usable **FY2018-19 onward**; FY2016-17 and FY2017-18 are
  100% placeholder. 22 XBRL concepts mapped; ~13 are dense. Book equity covers only ~21% of filings
  and skews large, so ROE and P/B are not rankable.
- **Benchmark:** the **published NIFTY 50 TRI, 2001-04-02 → 2026-09-04 (6,321 rows)**, landed
  2026-09-08 from a single unauthenticated POST. Before that date, every published excess-return
  figure in this project was struck against an L1 price-return proxy (see §7, D8).
- **Identity:** security master 8,598 securities (5,120 ACTIVE / 1,167 SUSPENDED / 2,311 DELISTED),
  symbol history 11,359 rows with `valid_from` back to 1995, ISIN lineage 399 reissue edges.
- **Trading calendar:** derived and validated, coverage extended back to 2006; validated against
  exchange circulars for 9 of 11 years (2012 and 2013 were derived from the archive itself, so for
  those two years the cross-check is partly circular). Ten non-Muhurat weekend sessions (Budget
  Saturdays, DR-site sessions) exist and the calendar schema cannot express them.

## 4.2 Survivorship

Of 1,870 EQ ISINs trading in 2017, the share still trading each later year: 95.7, 92.1, 88.1, 85.6,
80.6, 77.2, 74.4, 71.1, **68.6% by 2026 — 31.4% attrition over 9 years (~3.7%/yr).**

**The raw lake is not survivorship-biased** — a 2011 bhavcopy contains every security that traded that
day, dead ones included. The bias enters **downstream**, through any universe filter drawn from a
current-day list. Today the only BSE scrip master is **one snapshot (2026-09-06)** and the only index
constituent file is **one snapshot (2026-09-03)**. The disease is in the filters, not the data.

## 4.3 What we do NOT have (and what it costs to get)

| Missing | Free reality | Options |
|---|---|---|
| **Index constituent history** | as-of-today only (one snapshot); one 2010 file with weights covers ~9 months | Reconstruct backwards from NSE Indices' semi-annual reconstitution press releases (works for index members, days of desk work), or licence from IISL / CMIE Prowess / Capitaline |
| **Sector / theme classification history** | every free source carries **today's** sector, undated; NSE's taxonomy was itself overhauled ~2015 | Prowess (dated, NIC-based), Capitaline, GICS via LSEG/MSCI. Partial free reconstruction from sectoral-index membership covers only the 50–200 names in those indices — **nothing recovers the sector of a 2008 micro-cap** |
| **PIT fundamentals with restatements, pre-FY2019** | none. The only pre-2019 free source (Screener) is *restated with no as-of date*, so a 20-year fundamentals backtest is not shallow — **it is look-ahead-biased** | CMIE Prowess (~1989→, keeps as-filed *and* restated *and* dead companies), Capitaline, LSEG/Bloomberg PIT (~US$20k+/yr). **No free reconstruction path exists** |
| **Free float / shares outstanding** | total shares only from 2024-02; promoter/public split ~2022+ | Back out total shares from market cap ÷ close and walk the factor chain (gives total, **not free float**); or licence |
| **Bid-ask / intraday microstructure** | **zero, at any horizon** | Licensed tick data, or vendors reselling minute bars (~₹1–10k/mo). **No path from EOD to a spread** — slippage must be *modelled* conservatively enough that the edge survives the model being wrong |
| **Announcements / disclosures** | never fetched; verified available and ISIN-tagged back to **2006-01** | ~250 requests, ~20 min. Until then every break-condition check is blind and cancelled corporate actions are undetectable |
| **Macro series** | **no macro ingestion exists at all.** No FRED/RBI/MoSPI fetcher; the only vintage-capable source probed was unreachable | The one market-state series that exists is index P/E, P/B and dividend yield back to 2012-10-01 (fetchable, not yet ingested) |
| **News** | GDELT verified but **zero bytes** collected; the RBI RSS feed is a 10-item window with no archive | — |
| **FII/DII flows, bulk/block deals, ASM/GSM, price bands, AMFI cap classification** | snapshot-only endpoints with **no archive**. The FII/DII endpoint silently ignores a date parameter and always returns one session | **Their history is being destroyed at one day per day.** A missed poll is permanent |

**The single most important line above is sector/theme classification.** A themed fund is a
point-in-time universe rule, the theme label *is* the alpha, and there is **no free dated record of
what a company was classified as on a past date**. A company called "defence manufacturing" today
was filed under Industrial Products in 2012. Related trap: **thematic indices launched 2019–2024 and
their pre-launch "history" is back-computed using today's classification**, so it is usable as PIT
evidence only from live-launch date forward.

## 4.4 The three honest horizons

| Horizon | Span | Cost to reach | Supports |
|---|---|---|---|
| **A — held** | 2016-09 → 2026-09 (10.0 y) | done | prices, delivery, fundamentals FY2019+ |
| **B — reached 2026-09-08** | 2011-06-22 → 2026-09 (15.2 y) | done (~2,640 requests, ~2 h, zero new parser code) | prices, delivery, CAs, benchmark |
| **C — expensive** | 1995 → 2011-06-21 | new parser + a symbol→ISIN reconstruction with a permanent unresolved tail | price panel only, with a published refusal rate |

## 4.5 A recurring failure mode worth knowing about

**HTTP 200 carrying HTML is the dominant failure mode of these free sources.** BSE soft-404s with a
14 KB Angular shell; one index constituents file in the lake *is* an HTML shell; NSE's delivery
endpoint returns **HTTP 200 with the previous session's rows on a market holiday** (Republic Day 2026
served 3,102 rows all dated the prior session). Every parser now checks for a leading `<`, but the
sync layer will still mark such a day PUBLISHED.

---

# 5. MILESTONES — DONE AND NOT DONE

Gates (each = deliverables + acceptance criteria, no dates). Status from the task graph: **97 of 99
recorded tasks DONE**; the graph now holds 107 tasks and the recorded state is stale by ~8.

| Gate | Scope | Status |
|---|---|---|
| **M0** Foundations | repo, compose, Postgres+Parquet layout, scheduler skeleton, status API, backup/restore drill | **DONE** |
| **M1** NSE price core + 10-y backfill | dual-format parsers, delivery, identity master, sync state machine, crawl policy, archive publisher | **DONE** |
| **M2** Corporate actions & adjustment engine | CA ingestion + reconciliation queue, factor chains, L2 views, retroactive recompute, golden suite | **DONE** |
| **M3** BSE + flows + F&O EOD | BSE bhavcopy + dedup/primary-listing, announcements parser, index/TRI ingestion | **DONE except** the TRI half (landed 2026-09-08); flows/F&O/shareholding **coded, never fetched** |
| **M4** Query service + backtest engine | PIT query API, SimBroker + shared cost model, SIP simulation, XIRR accounting, benchmark, naive momentum, replay determinism in CI | **DONE** |
| **M5** Analyst agent v1, paper mode | interview, theme mapper, thesis engine, rails, rotation dial, cash manager, T0 monitoring, journal, data-red interlock | **DONE as a gate — "no language model ran anywhere in this gate"**; never operated forward |
| **M6** Monitoring depth T1/T2 + evidence packs | news/RSS ingestion, triggered LLM reviews, deep reviews, token accounting, evidence packs | **DONE as code**; the live fire drill is the one task blocked on an API key |
| **M7** Fundamentals waterfall | XBRL PIT store, restated store quarantined, fundamentals in evidence bundles | **DONE**, honestly FY2019+ |
| **M8** Real-money readiness | KiteBroker, order staging, daily recon, kill switch, compliance memo, graduation pack | **NOT STARTED** (2 tasks) |
| **M9** Trustworthy backtest inputs + momentum v2 | investable universe, benchmark, raw-vs-adjusted delta, momentum v2 toggles | **DONE** |
| **M10** Alternative signals | sector rotation, fundamentals signals, swing composite | **DONE** except a PIT membership dependency |
| **M11** Macro backdrop | PIT economic + market-state series | **probe only** — one task, no ingestion |
| **M12** Strategy search | 23-arm sweep × 2 floors × 2 windows, walk-forward verdict | **DONE** |

**Never gated, and material:** an operated forward paper run (so **no paper track record is
accumulating**, and M8's real-money gate would have nothing behind it), the scheduler, alerting, the
price sentinel on production sessions, and any live LLM.

---

# 6. EVERY ALGORITHM RESULT SO FAR

Common setup unless stated: NSE `EQ` only, long-only, equal-weight top-20 basket, opening capital
₹10,00,000 deposited once (the single external cashflow), shared cost model with modelled slippage,
signal on L2 back-adjusted closes, execution and marks on raw prices, universe = all NSE EQ above a
trailing-365-day median-turnover floor. Two floors are reported: **₹1 crore/day** (discovery) and
**₹10 crore/day** (what a real book could reach). Mean investable universe ≈ 890–910 names/decision.
Ranking objective (my decision, 2026-09-07): **XIRR ÷ max drawdown**, because ranked on return alone
the winner is whichever arm carried the most risk.

**Benchmark, published NIFTY 50 TRI (money-weighted on the same cashflow):**
decade 2016-09→2026-08 **11.92%**; 2019-07→2026-08 **11.70%**; 2016-09→2021-08 **15.65%**;
2021-09→2026-08 **8.39%**.

## 6.1 The 23-arm sweep — decade, 2016-09-02 → 2026-08-31 (2,470 sessions)

₹1 crore floor, ranked on XIRR/DD. Excess is now vs the **published TRI**:

| # | Strategy | XIRR | Max DD | XIRR/DD | Round trips | Median hold | Cost | Excess vs TRI |
|---|---|---|---|---|---|---|---|---|
| 1 | Swing composite (52w-high proximity + delivery share + 12-1 momentum) | 17.11% | 24.74% | 0.69 | 1476 | 43d | ₹229,169 | **+5.19%** |
| 2 | Swing composite + regime gate | 14.12% | 20.76% | 0.68 | 1054 | 43d | ₹154,823 | +2.20% |
| 3 | Momentum v2, all toggles on | 14.74% | 23.04% | 0.64 | 876 | 30d | ₹128,927 | +2.82% |
| 4 | Swing composite + delivery acceleration | 14.47% | 24.53% | 0.59 | 2419 | 28d | ₹322,440 | +2.55% |
| 5 | Swing composite + 1-month trend | 13.91% | 32.94% | 0.42 | 2059 | 30d | ₹219,831 | +1.99% |
| 6 | Short composite, 3-month holds | 9.65% | 27.36% | 0.35 | 1838 | 31d | ₹198,210 | −2.27% |
| 9 | Naive momentum (top-20 by trailing 12-m return, monthly) | 11.52% | 49.36% | 0.23 | 739 | 59d | ₹95,573 | −0.40% |
| … | 14 further short-horizon/concentration/overlay arms | 6.7% → −2.2% | 21–62% | 0.16 → −0.04 | up to 8,375 | 7–17d | up to ₹509,952 | all negative |

At the **₹10 crore floor** the same decade: momentum v2 all-on **13.49% / 24.87% DD (0.54)**, swing
composite **11.98% / 26.70% (0.45)**, swing+regime **9.16% / 23.02% (0.40)**, naive momentum
**10.98% / 46.88% (0.23)**. **Only 2 of 23 arms beat the real TRI at that floor** (before the
restrike it looked like six).

## 6.2 The same 23 arms — six-year window, 2019-07-01 → 2026-08-31 (1,773 sessions)

₹1 crore floor: **Swing composite 27.91% XIRR / 23.86% DD (1.17)** — the only place my >25% bar has
ever been cleared. Then: swing+1-month-trend 23.96%/28.66%, momentum v2 all-on 21.19%/31.82%,
swing+delivery-accel 20.94%/23.96%, swing+regime 19.42%/20.82%, 1-month trend alone 19.85%/26.97%,
50-day mean proximity 18.64%/25.94%, naive momentum 16.58%/29.80%.
At the **₹10 crore floor**: swing composite **21.08%/23.23% (0.91)**, momentum v2 **22.19%/28.76%**,
swing+regime 15.36%/17.68%.

**The window is doing a lot of work.** The same policies earn 12–17% over the decade and 19–28% over
2019–2026. Every headline number needs its window attached.

## 6.3 Walk-forward — the only out-of-sample test run

Selection window 2016-09-01 → 2021-08-31; verification 2021-09-01 → 2026-08-31; ₹1 crore floor;
winner named **before** any verification figure was read.

- **Chosen: swing composite + regime gate.** Selection 14.68% / 19.13% DD (0.77, rank 1 of 23).
  Verification **16.01% / 17.25% DD (0.93), rank 4 of 23.**
- Best on the verification window: plain swing composite **18.55%** (0.97); swing+delivery-accel
  18.16%; 50-day mean proximity **20.69%** — which had ranked **20th of 23** on selection.
- **Spearman rank correlation between selection and verification orderings: 0.372 — weak.** Three of
  the five arms chosen as best stayed in the top five. Read individual ranks as noisy.
- **What is stable is the family, not the arm.** The swing-composite family takes ranks 1, 2, 3, 6 on
  selection and 4, 1, 2, 6 on verification — the only group holding the top of both tables. Every
  short-horizon "short composite" arm sits in the bottom half of both.
- **The >25% bar was not cleared on either window** (best 14.68% selection, 18.55% verification).
- Survivorship in the *universe* is handled; **survivorship in the *signals* is not** — the legs swept
  are the ones this repo built because earlier work suggested they worked, which is a selection effect
  no split inside this lake can undo.

## 6.4 Momentum v2 — toggle by toggle over the decade (raw vs L2-adjusted signal)

| Configuration | Raw | Adjusted | Δ | Adjusted max DD |
|---|---|---|---|---|
| Naive (all off) | 12.21% | 11.56% | −0.65 | 48.84% |
| **+ 12-1 momentum** | 13.32% | **14.92%** | **+1.60** | 39.96% |
| + Turnover banding | 11.11% | 11.07% | −0.04 | 54.00% |
| + Regime filter (no new buys below the 200-session mean) | 10.49% | 9.80% | −0.69 | 24.86% |
| + Vol-scaled weights | 9.51% | 9.84% | +0.33 | 50.43% |
| + Redeploy proceeds next session | 14.23% | 13.71% | −0.52 | 53.71% |
| All four toggles on | 12.01% | 12.02% | +0.01 | 22.59% |
| All on + redeploy | 13.97% | 13.04% | −0.93 | 25.87% |
| + 15% vol target alone | 5.94% | 6.39% | +0.45 | 33.25% |
| **All on + redeploy + 15% vol target** | 14.95% | **14.74%** | −0.21 | **23.04%** |

**12-1 momentum is the one toggle the adjusted signal clearly rewards** (+3.36 pp over naive on
adjusted closes vs +1.11 pp on raw) — and it has a mechanism: both endpoints are in the past and
back-adjustment puts them in one share basis, exactly the ratio a split corrupts worst.

## 6.5 Fundamentals arms, 2019-07-01 → 2026-08-31 (86 monthly rebalances)

| Strategy | XIRR | Max DD | Risk-on cum. | Risk-off cum. | Trades | Cost |
|---|---|---|---|---|---|---|
| Fundamentals: VALUE | **21.53%** | **45.27%** | 279.35% | +6.73% | 1,243 | ₹79,197 |
| Momentum: v2 all-on | 18.52% (17.13% on adjusted) | **21.95%** | 202.79% | +11.73% | 1,453 | ₹66,891 |
| Momentum: naive | 17.85% (16.29% adjusted) | 28.90% | 241.76% | −4.95% | 2,359 | ₹99,697 |
| Fundamentals: GROWTH | 17.55% | 30.45% | 115.27% | **+48.17%** | 1,152 | ₹79,317 |
| Fundamentals: QUALITY_VALUE | 12.82% | 45.63% | 157.31% | −7.70% | 954 | ₹48,903 |
| Fundamentals: MOMENTUM_VALUE | 12.23% | 38.28% | 145.91% | −6.94% | 1,587 | ₹88,390 |

Notes: the two composite arms (QUALITY_VALUE, MOMENTUM_VALUE) **fail to beat their own components**.
GROWTH is the only arm that earns real money with the trend against it (+48.17% risk-off) — the
diversifier in the set. VALUE keeps a ~45% drawdown in every window. On the corrected (adjusted)
signal, GROWTH (17.55%) edges past momentum v2 (17.13%), so the second-best arm becomes a pure
fundamentals one. All fundamentals arms are limited to FY2019+ data by construction.

## 6.6 Sector rotation

Decade, on a sector map that is **42 ISINs from a test fixture, dated 2026, applied backwards**:
sector rotation 8.72% raw / **9.69% adjusted**, plain momentum on the identical universe 9.36% /
9.42%, proxy market 8.56%. Only **~39 sector-mapped names per rebalance**. The sector gate's sign
flips between raw and adjusted closes. **This result is engine plumbing, not evidence about sector
rotation.**

## 6.7 The fitted-forecast experiment (the closest thing to ML I have run)

An expanding-window PIT-clean linear forward-return model: 9 features, **1,604,652 matured
(features, forward-return) pairs**, scores every session, buys when projected 63-session return
clears twice the ~0.3% round trip, releases below zero expected return, 25% trailing stop, max 2
fills/session.

| Strategy | XIRR | Max DD | Fills | Cost |
|---|---|---|---|---|
| **Forecast, daily** | **−2.71%** | 36.28% | 3,076 | ₹15,070 |
| Momentum: naive, monthly | 16.29% | 29.80% | 2,367 | ₹91,352 |
| Momentum: v2 all-on, monthly | **17.13%** | **22.06%** | 1,511 | ₹62,523 |

In-sample R² 0.0077. Fitted coefficients: `mom_12_1` +0.01716, `deliv_ratio` +0.00993, `vol_63`
**+0.00601 (a-priori expected negative)**, `mom_1` **+0.00520 (a-priori expected negative —
short-term reversal)**, `high_prox` +0.00293, `mom_6` +0.00316, `turnover_ratio` +0.00477,
`earnings_yield` dropped as constant, `market_state` +0.13379 (a level, not a rank). Same universe,
same costs, same window as the arms it lost to.

## 6.8 Signal decay and why the hold is weeks, not days

Top-decile swing-composite excess vs the equal-weight investable universe, measured offline:
0.35% @5 sessions, 0.68% @10, 1.29% @21, 2.36% @42, **3.35% @63 (t = 13.3)**, accruing at ~0.2%/week
out to 125 sessions; power-law fit `0.0834·t^0.8915`. Round trip 0.45%. So:

| Hold | Excess | Round trip | Net per turn |
|---|---|---|---|
| 5 sessions | 0.35% | 0.45% | **−0.10%** |
| 10 sessions | 0.68% | 0.45% | +0.23% |
| 21 sessions | 1.29% | 0.45% | +0.84% |
| 63 sessions | 3.35% | 0.45% | +2.90% |

**Alpha is linear in time held; friction is paid per trade.** The bottom of a 7–90 day band is
underwater before the trade settles. Identical entries exited purely on time net ~20%/yr at a
10-session hold vs ~28%/yr at 63. And the composite's own edge **is weaker recently**: 2.95% in the
most recent stretch vs ~6% in the two before it.

## 6.9 Where the edge lives, and capacity

- The edge is concentrated in the **thinner half** of the investable set: excess 5.16% vs 2.78% when
  the floor is raised. Raising the floor 10× costs ~5 pp of XIRR on the swing composite (17.11% →
  11.98% over the decade).
- Median pick ADV at the ₹1 crore floor is **₹4.3 crore/day**. A ₹5 lakh position is 1.16% of ADV →
  **28.0 bp** one-way under a standard √-impact model, vs the fill model's **10.1 bp**. A ₹25 lakh
  position is 5.81% of ADV → **62.7 bp**. So the fill model is optimistic and increasingly so as the
  book grows.
- Computed crossover: **≈₹3.3 crore book** — below it the ₹1 crore floor is right, above it the
  ₹10 crore floor is.
- Turnover is running at **~48%/month** against a literature survivability line of <50%/month, i.e.
  on the line with no margin; implied drag 1.6–2.5%/yr.
- **Structural advantage:** ₹3.7 trillion of Indian small-cap mutual-fund money keeps 83% of holdings
  inside the top 750 stocks and caps sub-rank-1000 exposure at ~2%. Below ~₹5 crore I can own the
  rank-750–2,500 tail no institution can follow me into.

## 6.10 After-tax reality — none of the numbers above are after tax

The backtest has **no holding-period tracking and no capital-gains rates at all**. Median hold 43
days at ~8.6 turns/year means essentially the whole book realises at **20% STCG rather than 12.5%
LTCG**. Applying the tax and measured (not modelled) slippage to the 27.91% six-year headline:

| Book size | After-tax XIRR (six-year, ₹1cr floor) | Decade window |
|---|---|---|
| ₹10 lakh | **21.8%** | 13.5% |
| ₹1 crore | **19.5%** | 11.4% |
| ₹5 crore | **14.9%** | **7.4% — loses to a ~9.9–11.9% TRI** |

**The tax haircut alone is 6.1 pp on the six-year run, and nothing in the repo reports it.**

---

# 7. KNOWN DEFECTS THAT BOUND OR INVALIDATE EVERYTHING IN §6

Read every number in §6 as an **upper bound** for these reasons. All were found by internal audit,
none is disputed.

## 7.1 Silently wrong today

| # | Defect | Effect |
|---|---|---|
| **D1** | All 47,887 corporate actions carry **one `knowable_date` — today**; the announcement date is NULL on every row, and the exchange's own broadcast-date field is null at source on all 22,838 records | A backtest honouring the PIT invariant sees **zero corporate actions on every historical decision date**; one joining on ex-date uses look-ahead. The PIT invariant is satisfied *vacuously* |
| **D2** | Adjustment factors have **no link to the action that caused them** (NULL on all 2,784 rows) | No factor traces to its cause; the adjusted series cannot be audited or rebuilt from first principles |
| **D3** | **Corporate actions are never applied to the book during a walk.** Split/bonus/demerger handlers exist with **no call site in any driver** | A holding through a 2:1 split keeps its pre-split share count and is marked at the post-split close — **the position silently halves.** Only the signal is corrected |
| **D4** | Settlement hard-coded **T+1 for all history** | Wrong for the entire pre-2022 window, and wrong in a way correlated with exactly the small caps a theme fund holds |
| **D9** | **Dividends never reach cash.** No dividend method in the accounting module; the total-return close is computed, stored, and consumed by nothing | Long-window total return understated by the entire cumulative yield — and my mandate explicitly asks for dividend optimisation |
| **D11** | The delivery feed returns HTTP 200 with the *previous* session's rows on a holiday; the parser reads the date from the rows (safe) but the sync layer still marks the day PUBLISHED | Stale-data exposure today |

## 7.2 Results already invalidated

| # | Defect | Effect |
|---|---|---|
| **D6** | The sector map is **42 ISINs from a test fixture, dated 2026, applied backwards**, with no as-of dimension; the PIT guard stamps `knowable_date = as_of` unconditionally so it **passes trivially on 2026 labels asserted knowable in 2017**; the universe is narrowed to mapped names; the documented `membership_asof` path **was never written** | Every sector-rotation result is engine plumbing, not evidence |
| **D7** | The index-membership screen is a **verified no-op** — `membership_asof` returns `None` at all 18 probed dates, and `None` means "do not narrow" | **Every "investable universe" figure ever reported was turnover-floor-only.** The reports say "nifty500"; the universe was actually all NSE EQ above the floor — wider and more mid-cap. Fails safe, but undisclosed |
| **D8** | The benchmark had **never** been the real TRI until 2026-09-08; every M9/M10/M12 excess figure was struck against an L1 price-return proxy. Measured, the proxy's error is **not** a constant offset: it understated the decade by 3.37 pp/yr, the walk-forward selection window by **10.53 pp/yr**, and **overstated** the verification window by 3.19 pp/yr — a 13.72 pp swing across the split | Had the walk-forward ranked on *excess* rather than on return/drawdown, the verdict would have been rewritten. It survives by a design choice, not because the error was benign |
| **D10** | **Rails are wired into no backtest at all.** The replay engine is literally `for request in decision.orders: broker.place(request)` | Under my own ratified risk profile (15% position cap, 35% sector cap, minimum 8 holdings, 25% drawdown review): the **minimum-holdings rail makes the regime gate structurally unimplementable** (parking the basket refuses the 13th sell), and regime overlays hold **three of the top four slots** in the best table; the sector cap would bite a sector-blind 20-name book (8 names in one sector = 40% > 35%) and **no momentum policy in the repo is sector-aware**; the max-order-value rail is SIP-derived and the wrong scale for a lump-sum test; and **12 of 23 arms breach the 25% drawdown-review trigger**, with the winner clearing the decade limit by 26 basis points |
| **D12** | Four float casts in the swing-signal path (ranking-order risk); cost model and drawdown math are clean `Decimal` | Small but real |
| **—** | **Zombie universe:** the delisting-date column is NULL in all 10,870 rows, so the tradeable-on check returns True for all 8,598 securities including the 2,311 DELISTED. Backtests escape only by accident (they derive listing windows from actual prints instead) — **the production path is the one with no test coverage** | A live scoring pipeline would rank and size positions in companies that stopped trading years ago, with no error and no flag |

## 7.3 Governance failure worth noting

D8's history is instructive: on 2026-08-10 I probed the TRI endpoint myself, confirmed 6,213 rows in
one keyless request, and recorded three binding consequences (fix the register URL, split the task,
ingest the daily TRI). **None of the three happened for four weeks.** The task was recorded DONE with
a note asserting the source was "session-gated/FAILED" — the premise my own probe had disproved. A
later task then built on that disproved premise *by design*, dutifully disclosing the proxy in its
report. **The disclosure happened; the fix didn't.** Also: 1,827 lines of the forecast code were
committed off-graph under a bare tag with no task id, and the build-state file is stale by 8 tasks.

---

# 8. THE MULTI-FUND / THEMATIC GAP (relevant to my mandate directly)

I want themed sleeves ("AI", "defence", "power") and eventually several funds. Measured state:

- **A "case" is already most of a themed-fund mandate**: it carries a theme, horizon, primary and
  secondary benchmarks, a funding mode (paper/real), a SIP schedule and an 8-state lifecycle;
  policies are versioned per case, append-only, DB-enforced, so *"which rails were in force when that
  order was placed"* stays answerable years later; a core buy is impossible without a ratified thesis;
  N cases genuinely coexist, and two cases may hold the same ISIN under different theses and rails.
- **But the backtest stack is single-book.** `fund_id` appears **zero** times across the analyst,
  backtest, execution, accounting and data-platform packages. ~23 structures need a fund dimension.
  A cross-case concentration rail exists and takes a household-exposure object **nothing builds**.
- **Absent entirely:** a unit ledger / NAV per unit (a case is a rupee book with no denominator);
  external subscriptions/redemptions and any investor entity; per-fund fee accrual; benchmark
  attribution beyond a single excess number; **investor-level tax lots** — positions deliberately
  store one blended cost basis per ISIN, which makes FIFO/LIFO and STCG/LTCG classification
  impossible as built; and persisted position/cash (position is derived by summing fills, cash is
  in-memory).
- **The entity that makes it multi-fund is missing: cross-fund exposure.** Six thematic funds each
  taking 8% of a small cap's ADTV is 48% of ADTV. **Six individually plausible backtests that were
  never simultaneously achievable is the defining failure mode of this product, and it is invisible
  to per-fund testing.**
- **Turning a theme into a PIT rule** would need four layers: structural (taxonomy as-of date — the
  missing one), economic (segment-revenue share — the only real measure of theme exposure), textual
  (keyword/embedding over **dated** documents, never today's company description), relational
  (supply-chain linkage, for ranking only). One cheap guard proposed: a theme vocabulary may contain
  concepts, products and programmes but **never company names** — a validator can enforce that.

---

# 9. THE PLAN ALREADY ON MY TABLE — CRITIQUE IT

An internal review (2026-09-07, unratified) concluded, in one sentence: *"The AI you want in the
buy/sell decision is worth approximately nothing on top of the composite you already have, because
your composite already harvests the price-trend and liquidity signals every ML paper keeps
rediscovering; the AI that is worth building reads documents, fixes your corporate actions and
recovers your identity history; and the 3–7 percentage points of XIRR you are actually missing are
sitting in the tax code, the execution schedule, and four unrailed, tax-blind, undisclosed-universe
backtest reports."*

Its evidence for the "AI adds ~0" claim: my own fitted-forecast result (§6.7); the fact that every ML
asset-pricing paper reporting variable importances finds the same three dominant clusters (price
trends, liquidity, volatility) and my composite already harvests cluster 1 twice and cluster 2 once;
an honest US upper bound of +0.30 net Sharpe for ex-post-optimal long-short ML over 60 years and 200+
characteristics, discounted for ~40× less data, 2–4× the Indian cost floor, no short leg and no
microcaps → +0.05 to +0.15, minus cluster overlap → **+0.00 to +0.10**, against a standard error on
annualised Sharpe over my 216 months of **≈0.24**; Indian active management's aggregate alpha of
**+0.07%, t = 0.04** once profitability is added to the model (425 funds, 2013–2024), and 76.3% of
Indian large-cap funds losing to benchmark over 10 years; and the Nifty200 Momentum 30 index whose
backfilled history implies +5 pp/yr over Nifty 200 but whose **live record since Aug-2020 is
+0.60 pp/yr**.

Its proposed sequence:

- **Phase 0 (no AI, prerequisite):** rail the backtest; add after-tax accounting; disclose that the
  universe screen was a no-op; fix the zombie universe; then **re-run the whole sweep railed and
  after-tax** and re-publish the walk-forward verdict, ranking on **after-tax XIRR ÷ max drawdown**.
- **Phase 1 (no AI, where the rupees are):** long-hold arms and an **LTCG-aware exit** (a lot within
  45 sessions of 12 months is held to the boundary unless the stop fires) → **+3.02 pp** after tax at
  ₹1 crore (+7.01 pp at ₹5 crore), costing 1.84 pp of signal decay to buy 5.30 pp of tax headroom; an
  **execution scheduler** with per-name ADV participation caps splitting clips over 2–3 sessions →
  **+1.56 pp** (+3.51 pp at ₹5 crore); floor/book matching at the ₹3.3 crore crossover (prevents a
  −2.88 pp error); measure cash drag before fixing it (+0.3 to +0.8 pp, *estimated*); one honest test
  of continuous inverse-volatility scaling of gross exposure (literature: momentum Sharpe 0.53 → 0.97
  and max DD −96.7% → −45.2% at unchanged turnover, and momentum's realised variance has 57.8%
  out-of-sample R²; **my lake has said no three times to adjacent ideas** — binary regime gate Calmar
  1.17 → 0.93 six-year, 0.69 → 0.68 decade, and a standalone 15% vol target at 6.39% XIRR); and add a
  **quality/profitability leg** from the dense XBRL concepts (earnings yield, revenue/PAT growth,
  margins, effective tax rate, other-income share, dilution — not ROE or P/B).
- **Phase 2 (the real AI):** start monthly capture of index constituents and announcements **now**
  because those gaps get strictly worse with time; **LLM-assisted corporate-action reconciliation**
  over the 162 open ERROR and 241 open WARN reconciliation flags (propose, never auto-apply, golden
  fixtures validate); **LLM-assisted identity recovery** over the **1,358,650 unresolved-symbol**
  quarantined rows (a different set from the pre-2011 no-ISIN quarantine in §4.1 — these are delivery
  rows whose symbol never resolved to an ISIN), which recovers years of history with **no new fetch**; extend the XBRL concept map
  beyond 22 concepts with a golden fixture per taxonomy era; and finally run a live LLM fire drill.
- **Phase 3 (the bridge):** extract a single `weights_from_scores` seam; one shared score type
  carrying ISIN, a `Decimal` score, raw price, `knowable_date` and a provenance triple; and **the live
  policy driver** that runs the same policy object the sweep replays, through the same rails, into
  staged orders, journaling everything — because **until it exists, nothing (model or rule) can reach
  my portfolio**.
- **Phase 4 (capped ML experiment, pre-registered):** the model as ONE capped leg, evaluated on the
  Deflated Sharpe Ratio with trials honestly counted (including the 46 arm-runs already swept),
  turnover capped below 50%/month, measured against the quality sleeve rather than against cash, with
  a pre-committed kill criterion. Expected value stated as **−2.5 to +1.5 pp, modally ≈ 0**, for
  months of work — versus **+3.2 to +5 pp after tax at ₹1 crore** for Phases 0–1.
  It also flags the label trap: at a 12-month horizon **114,533 rows (3.2%)** are names that died
  mid-horizon and are systematically the worst outcomes, so the naive `shift(-H).dropna()` yields
  survivorship-free features with a survivorship-biased label set — *the worse combination because it
  looks clean*.

Two India-specific facts it leans on, from a survivorship-adjusted Indian factor library: momentum
**+12.6%/yr** over 20 years, value **+8.1%**, and **size is −2.1% — negative in India, consistently**
(small-cap outperformance in India is illiquidity and beta, not a size premium). And: momentum's worst
long-only drawdown is **−70.5% with 65 months to recover**, and its worst year is **2009, not 2008** —
the *rebound* kills momentum.

**I want you to attack this plan.** Is the ordering right? Is "AI adds ~0 to the buy/sell decision"
correct, or is it an artefact of one badly-specified linear model on 9 features? Is the +3–5 pp tax
and execution claim robust or is it double-counting? What is it missing entirely — in particular
about my mandate's *thematic/regime* requirement, which it barely addresses?

---

# 10. WHAT I WANT FROM YOU

Answer all of the following. Be concrete, numerate and falsifiable. Where you need an assumption,
state it inline and continue — do not stop to ask me questions, and do not pad with caveats.

1. **Verdict on the evidence.** Given §6 and §7, what do I actually know? Rank my results by how much
   I should believe them. Is the swing composite a real edge or a window artefact? What is the honest
   central estimate and confidence interval for its **after-tax, railed, post-friction** forward
   XIRR at a ₹25 lakh, ₹1 crore and ₹5 crore book? Is my >25% bar achievable at all, and if not, what
   should the bar be?
2. **The target architecture for the mandate in §1.** Design the fund manager I actually asked for,
   as a specification I can build: sleeves and their target weights (core / tactical / theme /
   liquid), the monthly inflow deployment rule (lump on arrival vs staged vs signal-gated — say which
   and prove it with a testable claim), the cash-reserve policy (how much, in what instrument, on what
   trigger it deploys), the rebalance cadence and why, the exit rules including the tax boundary, and
   the rails that must bound all of it. State every parameter and where its value should come from.
3. **Tax and dividends, properly.** Specify the tax-aware layer: lot accounting (FIFO vs specific-lot
   identification — say what Indian rules actually permit), the LTCG boundary rule and its interaction
   with a 25% trailing stop, harvesting of losses and of the annual LTCG exemption near financial-year
   end, the treatment of the annual exemption, and whether dividend yield deserves to be a signal leg
   at all under slab-rate taxation, or only a tie-break. Give me the arithmetic, not the principle.
4. **The theme / regime engine — the part nobody has solved here.** My mandate wants the system to
   have spotted the AI theme 2–3 years early. Given that I have **no dated sector or theme
   classification at any horizon** (§4.3), no macro series, and no news archive:
   (a) Is a PIT-honest thematic sleeve buildable on free data? If yes, specify it exactly — what
   defines a theme, what dated evidence assigns a company to it, how the assignment is timestamped,
   and how it is backtested without look-ahead. If no, say so plainly and say what the minimum
   purchase is (CMIE Prowess? Capitaline? something cheaper?) and what it buys me in return.
   (b) Design the falsification test: what backtest would distinguish "the system found the theme
   early" from "the system rode price momentum into whatever was already going up"? I suspect the
   honest answer is that a 12-1 momentum sleeve already captures most of the AI trade, two years late
   but cheaply. Tell me if I am right, with a measurement that would settle it.
   (c) Regime: my lake has rejected binary regime gates three times (§9). Is regime *sizing*
   (continuous exposure scaling) different in kind, or the same idea with a smoother edge? Specify
   the one experiment worth running and its pre-committed kill criterion.
5. **A prioritised, sequenced plan** — table with columns: item, what it changes, expected effect in
   pp of after-tax XIRR or in Calmar (with your confidence: high / medium / low), effort, and what it
   depends on. Separate **"fix before believing any number"** from **"new capability."** If you think
   the internal plan's Phase 0/1 ordering is right, say so and add what it is missing; if not, give
   your own ordering and say what it displaces.
6. **The backtest programme.** Specify the experiments that would actually settle my open questions —
   as pre-registered designs: hypothesis, window(s), universe, arms, ranking objective, the exact
   kill criterion, and how many trials the multiple-testing correction must account for. Include how
   to test the monthly-inflow (SIP) path, which is materially different from the single-cashflow
   backtests I have run so far: contributions land 120+ times over a decade, so cash drag,
   deployment timing and the tax clock all behave differently from anything in §6.
7. **What data to buy, and what to stop paying attention to.** Rank the gaps in §4.3 by rupees of
   expected return per rupee (or hour) of acquisition cost, for *my* mandate specifically. Be explicit
   about which gaps are irreversible (snapshot-only sources losing history daily) and therefore
   deserve action today regardless of priority.
8. **Where I am fooling myself.** Name the specific ways this whole project could be wrong that §7
   does not already cover. Include at least: signal-selection bias inside my own repo (23 arms chosen
   because earlier work in the same lake suggested they worked), the fact that my best result is one
   window at the optimistic liquidity floor, the ~48%/month turnover against a <50% survivability
   line, the recency of the composite's own decay (2.95% vs ~6%), and anything structural about my
   universe or costs that I have mis-modelled. For each, say what measurement would expose it.
9. **The capacity plan.** My edge lives in the thin half of my universe and I have a growing book with
   monthly inflows. Give me the schedule: at what book size does each strategy component stop working,
   what changes at each threshold, and what the terminal capacity of this whole design is in rupees.
10. **The one thing I should do first**, and what evidence would tell me it worked, within 90 days.

---

# 11. RULES OF ENGAGEMENT

- **Use my numbers.** Every recommendation must connect to a figure above or to a named public
  source. If you introduce a number from outside, say where it comes from and how much to trust it.
- **Disagree explicitly.** If a decision recorded above (ranking on XIRR ÷ max drawdown; the ₹1 crore
  floor; long-only cash equities; ISIN-only joins; extending "case" instead of building a "fund"
  abstraction; the >25% bar) is wrong, argue against it by name.
- **No generic advice.** "Diversify", "control risk", "consider index funds", "past performance does
  not guarantee" — I know. If the honest answer *is* "a large part of this book belongs in a
  low-cost index fund and here is the fraction and why", say that with the arithmetic.
- **Distinguish claims by strength.** Mark each as: *measured in my lake* / *published evidence* /
  *your inference* / *speculation*. I will treat them differently.
- **Prefer arithmetic over prediction.** Where an intervention's value is a cost or tax computation
  rather than a forecast, show the computation.
- **Respect the constraints in §2.** A recommendation that needs an override on the rails, a
  look-ahead classification, intraday data I do not have, or a human in the daily decision loop is
  not usable; say so if you think a constraint is the actual problem.
- **State what would change your mind**, for each major recommendation.

# 12. OUTPUT FORMAT

1. **Verdict** — 200 words maximum. The honest state of this project and the achievable target.
2. **The answers to §10.1–§10.10**, in that order, with headings. Tables wherever a table is clearer
   than prose.
3. **The specification** — the fund-manager design from §10.2 written tightly enough to implement,
   with every parameter and its provenance.
4. **The pre-registered experiment list** from §10.6.
5. **The risk register** — from §10.8, ranked by how much of my reported return it could remove.
6. **Open questions for me** — anything you could not resolve, with the decision I need to make and
   the information that would settle it.

Take as much length as the answer needs. Do not summarise your way out of the numbers.

=====================================================================================

## Notes for me (not part of the prompt)

- Every figure in the prompt is traceable to: `ops/gates/M12-strategy-sweep-decade.md`,
  `ops/gates/M12-strategy-sweep-sixyear.md`, `ops/gates/M12-strategy-verdict.md`,
  `ops/gates/M9-benchmark-restrike-2026-09-08.md`, `ops/gates/algo-reevaluation-2026-09-07.md`,
  `ops/gates/M10-swing-composite-report.md`, `ops/gates/X2-forecast-daily-report.md`,
  `ops/gates/W1-legacy-backfill-report.md`, `ops/gates/project-weaknesses-2026-09-06.md`,
  `ops/gates/ai-analyst-plan-2026-09-07.md`, `ops/studies/multi-fund-data-study-2026-09-07.md`,
  `docs/RAW_DATA_CATALOG.md`, `EXECUTION_PLAN.md`, `AGENTIC_CONTEXT.md`.
- The prompt deliberately contains **no** host, key path, credential, or private endpoint, and no
  proprietary data — only measured aggregates about my own lake. Safe to paste into a third-party
  model.
- Deliberately included rather than hidden: the defect list (§7), the fact that the >25% bar was met
  in exactly one window, and the internal plan's negative conclusion about AI in the decision path.
  A reviewer given only the flattering half would return a flattering answer.
- If the reply is thin on §10.4 (the theme/regime engine), push back with: *"answer 4(a) as a
  buildable specification or state plainly that it is not buildable on free data, and price the
  minimum purchase."*
