# EOD Trading Platform & Analyst Agent — Master Plan

**Version:** 1.0 · **Date:** 2026-08-08 · **Owner:** Vyshakh · **Status:** Thesis ratified → execution
**Build model:** Solo + AI pair · **Pace:** Dependency gates, no calendar dates · **Capital:** Personal first, product-ready architecture

---

## 0. Product Thesis

Two systems, one governance model. **System 1 (Data Platform):** a free-source, NSE+BSE, EOD market-data platform with 10 years of corporate-action-correct history, served for efficient internal querying, published as daily downloadable archives, with sync health exposed via a status API. **System 2 (Analyst Agent):** an agentic service that interviews the user to construct a thematic portfolio ("case"), writes falsifiable inclusion theses per holding, monitors daily through tiered checks (mechanical → LLM), and manages the case autonomously — in paper mode for evaluation, in real-money mode after discretionary graduation.

**Governance model (universal):** *Agent proposes policy → human ratifies policy → agent executes autonomously within ratified policy.* Applies to break conditions, exit strategies, cash management, rotation authority, and every other discretionary surface.

**The decision journal is the product.** Every agent action is reconstructable: evidence seen, conditions evaluated, reason acted. It is simultaneously the debugger, the graduation evidence, the future SEBI story, and the differentiation.

---

## 1. Decision Register (Constitution)

Changes to these require an explicit amendment (§12). Everything else in this document is derived and negotiable.

| # | Decision | Implication |
|---|----------|-------------|
| 1 | Personal capital first; public product later | Audit-grade journal from day one; multi-tenant data model, single-tenant deployment |
| 2 | Two modes, one brain | Paper and real money run the identical decision code path; only the broker adapter differs (`SimBroker` / `KiteBroker`) |
| 3 | Thesis + hard rails | Deterministic risk rails (position/sector caps, drawdown-triggered review) that the agent **cannot override** |
| 4 | Break conditions: agent drafts, user ratifies | Daily monitoring is mechanical evaluation against a ratified contract, not daily discretion |
| 5 | Governance: propose → ratify → execute | The human approval sits at the policy layer, never in the daily loop |
| 6 | All five data classes in v1 | Prices+delivery, corp actions, fundamentals, news/geo, flows & positioning, F&O EOD — sequenced by dependency, not built in parallel |
| 7 | Fundamentals via free-source waterfall | Exchange-official → structured free (Screener, within robots/ToS limits) → targeted crawling. Tracked in the Source Register |
| 8 | Graduation is discretionary | Journal auto-generates the evidence pack so discretion is informed; pack becomes a formal gate at product stage |
| 9 | Exit strategy: agent proposes, user ratifies | Staged/immediate/redeploy are policy options ratified per case |
| 10 | Idle cash parked productively | Liquid ETFs (on-exchange, same broker path) until redeployment; cash policy is ratified |
| 11 | Rotation: pure per-case dial (0–100%) | Tactical sleeve share ratified at case creation; agent recommends a setting from risk appetite; rails apply at every dial setting |
| 12 | Cost-blind for now | Quality-first model choices; per-decision token accounting in the journal so cost is measured, not unknown |
| 13 | Solo + AI pair | Monolith, boring tech, few moving parts, one-person maintainability |
| 14 | No deadline; dependency gates | Milestones are gates with acceptance criteria; the plan forces early contact with the agent problem |
| 15 | Sequencing spine | Data core → backtest → agent live on price/flows/news → fundamentals waterfall → F&O enrichment |

---

## 2. Architecture Overview

```
                              ┌─────────────────────────────────────────────┐
                              │              SCHEDULER (daily EOD)          │
                              └──────┬──────────────────────────┬───────────┘
                                     ▼                          ▼
  FREE SOURCES                DATA PLATFORM               ANALYST AGENT SERVICE
┌──────────────┐   fetch   ┌────────────────┐          ┌─────────────────────────┐
│ NSE archives │──────────▶│ D1 Ingestion   │          │ A2 Interview / Builder  │
│ BSE archives │           │  (crawl policy │          │ A3 Theme Mapper         │
│ niftyindices │           │   engine)      │          │ A4 Thesis Engine        │
│ GDELT / RSS  │           └───────┬────────┘          │ A5 Monitor (T0/T1/T2)   │
│ Screener*    │                   ▼                   │ A6 Rotation (dial)      │
│ XBRL filings │           ┌────────────────┐          │ A7 Cash Manager         │
└──────────────┘           │ L0 RAW STORE   │          └───────────┬─────────────┘
                           │ (immutable)    │                      │ decisions
                           └───────┬────────┘                      ▼
                                   ▼                    ┌─────────────────────┐
                           ┌────────────────┐  reads    │ A8 RISK RAILS       │
                           │ D3 Corp Action │◀────┐     │ (deterministic,     │
                           │  + Adjustment  │     │     │  agent cannot skip) │
                           └───────┬────────┘     │     └─────────┬───────────┘
                                   ▼              │               ▼
                           ┌────────────────┐     │     ┌─────────────────────┐
                           │ L1/L2 CANONICAL│─────┴────▶│ X1 EXECUTION        │
                           │ Parquet+DuckDB │           │ SimBroker│KiteBroker│
                           │ + Postgres     │           └─────────┬───────────┘
                           └───┬───────┬────┘                     ▼
                               ▼       ▼               ┌─────────────────────┐
                     ┌──────────┐ ┌──────────┐         │ A9 DECISION JOURNAL │
                     │D5 Status │ │D6 Archive│         │ + evidence packs    │
                     │   API    │ │ Publisher│         │ + X3 token accounts │
                     └──────────┘ └──────────┘         └─────────────────────┘
                               ▲
                     X2 BACKTEST/REPLAY reads L0–L2 with point-in-time rules,
                     drives the SAME agent code through SimBroker.
```

\* Screener usage stays within the robots.txt limits mapped 2026-08-08 (no `?page=`, `?q=`, `/user/*`; per-company pages and Excel exports are permitted).

---

## 3. Module Catalog & Usage Map

"Consumed by" is the answer to *how each component gets used*.

| ID | Module | Purpose | Consumed by |
|----|--------|---------|-------------|
| D1 | Ingestion + Source Register | Fetch/validate every dataset; one registry of source, fallback, license status | D3, D4, D7; Source Register read by humans + crawl policy engine |
| D2 | Identity Master | ISIN-keyed security identity; symbol-change history; dual-exchange dedup; primary listing | Every other module — nothing joins on raw symbol, ever |
| D3 | Corp Actions + Adjustment Engine | CA ingestion; cumulative adjustment factors; derived adjusted series; retroactive recompute | D4 (views), X2 (returns), A5 (event awareness), D7 (validation) |
| D4 | Canonical Store + Query Service | Parquet+DuckDB time series; Postgres masters/state; internal query API | A3, A5, A6, X2, D6, status/reporting |
| D5 | Sync State + Status API | Per-(source,date) state machine; the mandated status API | Operator (you), D7 alerts, future product UI |
| D6 | Archive Publisher | Daily normalized Parquet/CSV archive bundles for external download | You, any downstream/external consumer |
| D7 | Data Quality Sentinel | Gap detection, cross-exchange price sanity, CA-correctness checks, anomaly flags | D5 (status), blocks agent trading on red data days |
| A1 | Case Service | Case configs, ratified policy sets, SIP scheduler, multi-case view, cross-case concentration | A5–A8, X1, journal |
| A2 | Interview / Case Builder | Elicits amount, horizon, theme, risk; recommends rotation dial + rails; produces the proposal | A1 (creates case), A3, A4 |
| A3 | Theme Mapper | Theme → value chain → listed NSE/BSE proxies with disclosed purity scores | A2 (construction), A5 T2 (universe refresh) |
| A4 | Thesis Engine | Drafts falsifiable break conditions per holding; ratification workflow; thesis versioning | A5 (evaluation targets), A9 (contracts on record) |
| A5 | Monitoring Engine (T0/T1/T2) | Daily mechanical checks → triggered LLM review → scheduled deep review | A6, A8, X1 via proposals; every output lands in A9 |
| A6 | Rotation Engine | Dial semantics: tactical sleeve sizing, new-money steering, sleeve trading | X1 (orders tagged CORE/TACTICAL), A9 |
| A7 | Cash Manager | Idle cash → liquid ETF parking; deployment queue for SIP + exit proceeds | X1, A9 |
| A8 | Risk Rails | Deterministic pre-trade checks + daily rail monitoring; drawdown-triggered forced review | Gates every order from A5/A6/A7; rail events → A9 |
| A9 | Decision Journal + Evidence Packs | Append-only decision log with evidence snapshots; monthly/graduation evidence packs | You (graduation), future audits/product |
| X1 | Execution Layer | Broker interface; order staging; reconciliation; kill switch | A5–A8 upstream; SimBroker (paper/backtest), KiteBroker (real) |
| X2 | Backtest/Replay Engine | Point-in-time replay of the same agent policies over history; SIP + cost simulation | Strategy validation, agent evaluation, rails calibration |
| X3 | Token & Cost Accounting | Per-decision LLM token/cost capture | A9 (journal lines), monthly burn report |

---

## 4. Data Platform Specification

### 4.1 Source Register v1

Every field the engine consumes maps to a row here. `VERIFIED` = URL pattern confirmed working during planning (Aug 2026); `VERIFY-AT-BUILD` = source known free, exact endpoint to be confirmed in its milestone.

| Dataset | Primary source (pattern) | Cadence | Fallback | License / robots notes | PIT notes | Status |
|---|---|---|---|---|---|---|
| NSE equity OHLCV (≤ ~08-Jul-2024) | `nsearchives.nseindia.com/content/historical/EQUITIES/{YYYY}/{MON}/cm{DD}{MON}{YYYY}bhav.csv.zip` | backfill only | — | Free; needs browser UA + session cookie | Immutable once published | VERIFIED |
| NSE equity OHLCV (UDiFF, ≥ Jul-2024) | `nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{YYYYMMDD}_F_0000.csv.zip` | daily | samco.in mirror; GetBhavcopy | Same; different column schema — dual parser required | Immutable | VERIFIED |
| NSE delivery % (sec_bhavdata_full) | `nsearchives.nseindia.com/products/content/sec_bhavdata_full_{DDMMYYYY}.csv` | daily | — | Free | Immutable | VERIFIED |
| BSE equity OHLCV | BSE UDiFF bhavcopy via bseindia.com archives | daily + backfill | GetBhavcopy | Free; UA handling similar to NSE | Immutable | VERIFY-AT-BUILD |
| Corporate actions | NSE corporates CA file + BSE corp actions | daily | Screener per-company page; manual entry queue | Free; the two exchanges describe the same action differently — reconciliation needed | Actions apply retroactively by design | VERIFY-AT-BUILD |
| Symbol / ISIN master | NSE `EQUITY_L.csv` + BSE scrip master + name-change history files | weekly | — | Free | Keep full change history, never overwrite | VERIFY-AT-BUILD |
| Index constituents | niftyindices.com list CSVs (`ind_nifty50list.csv`, sectoral, thematic) | monthly | — | Free | Snapshot per month → historical constituents accumulate from day one | VERIFY-AT-BUILD |
| Benchmark TRI series | niftyindices.com historical TRI download | daily | Computed price-index proxy + dividend estimate | Free | — | VERIFY-AT-BUILD |
| FII/DII daily flows | NSE reports (FII/DII trading activity) | daily | NSDL/CDSL monthly | Free | Immutable | VERIFY-AT-BUILD |
| Bulk & block deals | NSE archives daily CSVs | daily | BSE equivalent | Free | Immutable | VERIFY-AT-BUILD |
| Shareholding pattern | Exchange filings (quarterly) | quarterly | Screener company pages (robots-permitted) | Free | Filing date ≠ quarter end — store both | VERIFY-AT-BUILD |
| F&O EOD (OI, PCR, basis) | `BhavCopy_NSE_FO_0_0_0_{YYYYMMDD}_F_0000.csv.zip` | daily | — | Free; sentiment context only, no derivatives trading | Immutable | VERIFY-AT-BUILD |
| Corporate announcements | NSE + BSE announcements feeds | intraday poll @ EOD | — | Free | Timestamped at source — natural PIT | VERIFY-AT-BUILD |
| News / geopolitical | GDELT 2.0 + curated RSS (PIB, business press headlines) | daily | — | GDELT free/open; RSS headlines+links only | Timestamped | VERIFY-AT-BUILD |
| Fundamentals (restated) | Screener per-company Excel export (robots-permitted, per-company cadence) | quarterly refresh | — | Within robots limits; **restated → quarantined from backtests** | NOT point-in-time — monitoring use only | VERIFIED (limits mapped) |
| Fundamentals (point-in-time) | NSE/BSE results filings (XBRL) parsed from filing date forward | per filing | Announcement PDFs | Free | True PIT — filing timestamp is first-knowable date | VERIFY-AT-BUILD (M7) |

**Crawl policy engine (part of D1):** per-source robots/ToS register; browser UA + warm session cookie for NSE; rate limits (2–3 s spacing); exponential backoff; hard stop + alert on 403 spikes; raw responses checksummed into L0 before any parsing.

### 4.2 Storage Layers

| Layer | Contents | Technology | Rule |
|---|---|---|---|
| **L0 Raw** | Exact fetched files (zips/CSVs) + checksums + fetch metadata | Object-store layout on disk | Immutable. Never edited, never deleted. Every L1/L2 value must be re-derivable from L0 |
| **L1 Canonical** | Normalized prices, CA table, masters, flows — ISIN-keyed, both exchanges | Parquet partitioned by `dataset/date`; Postgres for masters, sync state, journal | Raw traded prices only — **no adjusted prices stored here** |
| **L2 Derived** | Adjusted series (via factors), indicators, sector aggregates, breadth | Parquet views / materialized DuckDB | Fully recomputable; rebuilt retroactively when a new CA lands |

Core tables (sketch): `security_master(isin, symbol_history[], exchange_listings[], primary_exchange, series, status)` · `prices_raw(isin, exchange, date, o,h,l,c, vwap, volume, delivery_qty, series)` · `corporate_actions(isin, ex_date, type, ratio_terms, source, reconciled)` · `adjustment_factors(isin, ex_date, cum_price_factor, cum_qty_factor)` · `sync_state(source, date, state, attempts, last_error, checksum)`.

### 4.3 Adjustment Engine Rules

1. Factors computed per ISIN at each ex-date; adjusted series = raw × cumulative factor, **derived on read/materialization, never primary**.
2. A new corporate action triggers retroactive recompute of the full factor chain for that ISIN + invalidation of L2.
3. Mergers/demergers modeled explicitly: price gaps on ex-date are structural events, not returns; return series must bridge them correctly.
4. **Golden test suite** (gate for M2 — suite must pass before anything downstream trusts adjusted data): LTIM merger (Mindtree+LTI 2022), Jio Financial demerger from RIL (2023), HDFC–HDFC Bank merger (2023), RIL 1:1 bonus (2024), IRCTC 1:5 split (2021), Tata Motors DVR conversion (2024), Tata Motors CV/PV demerger (2025), plus ~13 more ugly cases collected during backfill. Validate adjusted closes against two independent references.

### 4.4 Sync State Machine & Status API

State machine per (source, date): `PENDING → FETCHED → VALIDATED → NORMALIZED → PUBLISHED`, with `FAILED(retryable, attempts)` and `GAP(expected — holiday/weekend)` terminal branches.

```
GET /health                      liveness + scheduler heartbeat
GET /status/sync?date=           all sources for a date, with states
GET /status/sources              per-source: last success, lag, failure streak
GET /status/gaps?from=&to=       unexplained missing (source,date) pairs
GET /status/quality              D7 sentinel flags (price anomalies, CA mismatches)
GET /archives?date=              manifest + download links for daily archive bundle
```

**Trading interlock:** the agent's daily loop reads `/status/sync` first; if the trading date's core datasets aren't `PUBLISHED` and quality-green, the agent records `SKIPPED_DATA_RED` in the journal and does not trade. Bad data must never silently become decisions.

### 4.5 Query Service & Archives

Internal API over DuckDB/Parquet. Canonical query shapes it must serve efficiently: (a) adjusted OHLCV series per ISIN across years; (b) full-market cross-section for a date; (c) screen-style filters over cross-sections joined to fundamentals/flows; (d) point-in-time universe as of a historical date (via index-constituent history + listing status — kills survivorship bias). Archive publisher (D6) emits a daily bundle: normalized Parquet + CSV, manifest with checksums, downloadable via the status API host.

---

## 5. Analyst Agent Specification

### 5.1 Case Lifecycle

```
DRAFT → INTERVIEW → PROPOSAL → RATIFIED → FUNDED(paper | real) → ACTIVE ⇄ SUSPENDED → CLOSED
```

- **INTERVIEW (A2):** elicits capital plan (SIP amount/day), horizon, theme, risk appetite, exclusions. Agent then *recommends* the full ratified policy set.
- **PROPOSAL:** universe with purity scores, per-holding theses with break conditions, rotation dial, rails, exit menu, cash policy, benchmark pair. One document, one ratification.
- **RATIFIED → FUNDED:** paper mode by default. Real-money funding only via explicit graduation action (§5.7).

### 5.2 Ratified Policy Set (per case)

| Policy | Contents | Example (AI/Robotics case) |
|---|---|---|
| Capital plan | SIP amount, day-of-month, top-up rules | ₹10k monthly, 1st |
| Horizon & benchmarks | Target years; NIFTY-TRI + theme proxy | 5 yr; NIFTY-TRI + NIFTY IT/CPSE blend |
| Rotation dial | Tactical sleeve % of case capital (0–100) | Agent recommends 30%, user ratifies |
| Risk rails | Max position %, max sector %, min holdings, drawdown-review trigger, per-order sanity caps | 15% / 35% / 8 / −25% peak-to-trough |
| Exit menu | Ratified strategies the agent may choose among (staged / immediate / exit-and-redeploy) | Staged default; immediate allowed on integrity events |
| Cash policy | Parking instrument + deployment queue rules | Liquid ETF (e.g. LIQUIDCASE/LIQUIDBEES) |
| Monitoring cadence | T2 deep-review frequency; T1 trigger sensitivity | T2 monthly; standard triggers |

### 5.3 Thesis Object (schema sketch)

```json
{
  "isin": "INE...", "case_id": "...", "sleeve": "CORE",
  "thesis": {
    "driver": "Indian EMS capex cycle × robotics component localization",
    "theme_purity": 0.6,
    "expected_evidence": ["order-book growth >20% YoY", "robotics segment revenue disclosure"],
    "break_conditions": [
      {"id": "BC1", "type": "fundamental", "condition": "two consecutive quarters of segment revenue decline", "evaluation": "T1 on results filing"},
      {"id": "BC2", "type": "structural", "condition": "exit/divestment of robotics business line", "evaluation": "T0 announcement keywords → T1"},
      {"id": "BC3", "type": "integrity", "condition": "auditor resignation / fraud investigation / promoter pledge >50%", "evaluation": "T0 → immediate T1"}
    ],
    "ratified_by_user_at": "...", "version": 2, "prior_versions": ["..."]
  }
}
```

Rules: every holding (core sleeve) carries a ratified thesis before first buy. Thesis edits create a new version requiring re-ratification. Tactical-sleeve positions carry a lightweight rationale (journaled) but not a ratified thesis — that's the point of the sleeve.

### 5.4 Tiered Monitoring (A5)

| Tier | Cadence | Cost | Inputs | Output |
|---|---|---|---|---|
| **T0 Mechanical** | Every trading day | ~₹0 | Prices vs rails, drawdown triggers, CA events, announcement keyword hits against break conditions, flow anomalies (delivery spikes, bulk deals in holdings), data-quality flags | Pass → journal heartbeat. Flag → escalate to T1 |
| **T1 Triggered LLM review** | On T0 flag or filing event | Strong model | Evidence bundle: the flag, thesis + break conditions, recent filings/news, price/flow context | Verdict per break condition: `INTACT / WEAKENED / BROKEN` + proposed action within ratified policies |
| **T2 Scheduled deep review** | Per case cadence (e.g. monthly) | Strong model | Full case: every thesis, cycle/rotation context (sector RS, breadth, flows), theme development scan, universe refresh from A3 | Case health report; rotation-steering updates; candidate bench refresh |

"Daily balancing check" = T0 every day, by design cheap; escalation buys depth only when evidence demands it. Every tier writes to the journal, including "checked, nothing happened."

### 5.5 Rotation Engine & Sleeves (A6)

- Dial `d%` → tactical sleeve target = `d%` of case value; core = `100−d%`.
- **Core:** membership changes on `BROKEN` verdict only (decision #4's contract). New SIP money steering within core = allowed (tilt toward cycle-favored holdings).
- **Tactical:** agent has full buy/sell authority inside the sleeve — rotation trades, cycle expressions, temporary positions. All orders tagged `TACTICAL`, rails still binding, every trade journaled with rationale.
- Sleeve rebalancing across the boundary (resizing core vs tactical) is a *policy change* → requires ratification.

### 5.6 Exits & Cash (A7)

On `BROKEN`: agent selects a strategy from the ratified exit menu (staged over 2–3 sessions default; immediate for integrity breaks). Proceeds + monthly SIP land in the deployment queue → parked in the liquid ETF same day → deployed when the agent has a ratified-thesis replacement (core) or a tactical opportunity (sleeve). India constraint honored: no fractional shares — accumulation/rotation logic decides which holdings each ₹10k instalment actually buys; tracking drift vs model weights is journaled.

### 5.7 Decision Journal & Evidence Packs (A9)

Append-only journal entry (every decision, including no-ops):

```json
{
  "ts": "...", "case_id": "...", "actor": "T0|T1|T2|RAILS|EXEC|USER",
  "decision": "HOLD|BUY|SELL|ESCALATE|SKIP_DATA_RED|RAIL_BLOCK|POLICY_PROPOSAL",
  "instrument": "INE.../null", "sleeve": "CORE|TACTICAL|CASH",
  "evidence_snapshot_ref": "content-addressed bundle (prices, filings, news items actually shown to the model)",
  "break_conditions_evaluated": [{"id": "BC1", "verdict": "INTACT"}],
  "rationale": "...", "model": "...", "tokens": {"in": 0, "out": 0, "cost_inr": 0},
  "orders_ref": "..."
}
```

**Evidence pack** (auto-generated monthly + on graduation request): returns vs both benchmarks (XIRR, since SIP), drawdown profile, rail-breach count (target: zero — rails block, so breaches mean bugs), decision review (T1/T2 verdicts vs subsequent outcomes), turnover + tax-event summary by sleeve, token/cost burn, data-quality skips. Graduation (decision #8) = you reading this pack and flipping the switch; the pack format is the future formal gate.

---

## 6. Execution Layer (X1)

- **Broker interface:** `place / modify / cancel / positions / holdings / ledger / margins` — implemented by `SimBroker` and `KiteBroker`. The agent never imports a broker directly.
- **SimBroker fill model:** next-day execution for EOD decisions (open or conservative VWAP band), slippage in bps scaled by liquidity, full Indian cost model — brokerage (₹0 delivery on Zerodha), STT, exchange txn charges, SEBI fee, stamp duty, GST, DP charge on sells. **One cost model module shared by SimBroker and backtest** — never two implementations.
- **Order staging:** decisions produce staged orders EOD → executed next session → reconciliation job compares broker positions/ledger vs internal book every day; any mismatch freezes trading and alerts.
- **Kill switch:** one command/endpoint halts all order placement (rails and reconciliation can trip it automatically).
- **SEBI checkpoint (M8):** verify current exchange/broker rules for API order automation (retail algo framework: order-rate thresholds, algo registration via broker, static IP). Low-frequency EOD rebalancing likely under thresholds — verify, document in journal, revisit at product stage.

---

## 7. Backtest / Replay Engine (X2)

- Replays the **same agent policy code** over L0–L2 history through SimBroker — not a parallel implementation of the strategy.
- **Point-in-time discipline:** universe from index-constituent history + listing status (delisted included — bhavcopy backfill preserves them); fundamentals only from filing-date-tagged PIT store; **restated Screener data is quarantined out of backtests** (monitoring use only); news replay from timestamped stores where available.
- Simulates SIP mechanics with lot constraints and the shared cost model.
- LLM-dependent decisions (T1/T2) can't be truly replayed historically — backtests validate the *mechanical* skeleton (rails, rotation math, SIP, exits, accounting); agent judgment is evaluated forward, in paper mode. This is exactly why paper mode is the primary evaluation harness (decision #2) and why the backtest gate (M4) precedes agent build (M5).

---

## 8. Engineering Plan

### 8.1 Stack (decision #13: boring, solo-maintainable)

| Concern | Choice | Notes |
|---|---|---|
| Language | Python 3.12+, single monolith | One repo, one deployable, module boundaries as packages |
| State / journal | Postgres | Masters, sync state, cases, policies, journal (append-only tables) |
| Time series | Parquet on disk + DuckDB | L1/L2; trivially handles this scale (~20M rows) |
| Scheduler | APScheduler (in-process) or systemd timers | One daily EOD pipeline + intraday announcement polls |
| API | FastAPI | Status API, internal query API, case/ratification endpoints |
| Ratification UX | Minimal web page (FastAPI + HTMX) or CLI first | Proposal review + approve/edit; product UI comes later |
| LLM | Strong model for T1/T2 + cheap model for triage/keywords | Cost-blind (#12) but metered by X3 |
| Broker | Kite Connect (KiteBroker) | Verify current pricing/registration at M8 |
| Deploy | Single VM, docker-compose (app + Postgres), object-storage backup for L0 + Postgres dumps | Nightly backups; restore drill in M1 acceptance |
| Observability | Structured logs, sync dashboard = status API, alerts via email/Telegram on FAILED streaks, red quality, reconciliation breaks | |

### 8.2 Repo Layout

```
trading-platform/
├── platform/            # System 1
│   ├── ingest/          # D1: per-source fetchers + crawl policy engine
│   ├── identity/        # D2
│   ├── corpactions/     # D3 + golden test suite
│   ├── store/           # L0/L1/L2 management, Parquet layout
│   ├── quality/         # D7
│   ├── query/           # D4 API
│   ├── archives/        # D6
│   └── status/          # D5 API
├── analyst/             # System 2
│   ├── cases/           # A1  ├── interview/  # A2  ├── mapper/ # A3
│   ├── thesis/          # A4  ├── monitor/    # A5  ├── rotation/ # A6
│   ├── cash/            # A7  ├── rails/      # A8  └── journal/  # A9
├── execution/           # X1: broker interface, SimBroker, KiteBroker, recon, costs/
├── backtest/            # X2
├── accounting/          # X3 token/cost metering
└── ops/                 # compose, backups, deploy, runbooks
```

### 8.3 Testing Strategy

1. **Golden CA suite** (§4.3) — the highest-value tests in the system; M2 gate.
2. **Ingestion contract tests** — frozen sample files per source per format era; parser changes must pass all eras.
3. **Replay determinism** — same inputs → identical journal + book, byte-for-byte; run in CI.
4. **Rails property tests** — generated order streams can never breach caps.
5. **Reconciliation drills** — inject broker/book mismatches, assert freeze + alert.
6. **PIT leak test** — backtest harness asserts no data with knowable-date > decision-date ever reaches a decision.

---

## 9. Execution Plan — Milestone Gates

No dates (decision #14). Each gate = deliverables + acceptance criteria; a gate is passed only when every box ticks. **Agent-contact rule:** M5 starts the moment M4's core replay passes; M7 (fundamentals) is explicitly NOT a blocker for paper mode — this is the anti-perfectionism guardrail.

**M0 — Foundations** *(depends: —)*
Repo, compose stack, Postgres + Parquet layout, scheduler skeleton, status API skeleton, backup/restore scripts.
✅ `docker compose up` → healthy; `/health` + empty `/status/sync` return real state; backup + restore drill documented and executed once.

**M1 — NSE price core, 10-year backfill** *(depends: M0)*
Dual-format bhavcopy parsers (legacy + UDiFF), sec_bhavdata_full (delivery), identity master v1 (ISIN-keyed, symbol-change history), sync state machine live, crawl policy engine, archive publisher v1.
✅ 10 years NSE equities in L1; gap report explains 100% of missing days (holidays/weekends); daily EOD job runs unattended 5 consecutive sessions incl. self-heal on one induced failure; archives downloadable; delisted symbols present in history.

**M2 — Corporate actions & adjustment engine** *(depends: M1)*
CA ingestion + reconciliation queue, factor chain computation, L2 adjusted views, retroactive recompute path, golden test suite.
✅ All golden cases pass against two independent references; a newly inserted CA correctly rewrites the ISIN's full adjusted history; D7 flags any close-to-close move >20% lacking a CA or circuit explanation.

**M3 — BSE + flows + F&O EOD** *(depends: M1; parallel with M2)*
BSE bhavcopy (backfill + daily), dual-exchange dedup + primary-listing selection, FII/DII, bulk/block deals, shareholding, F&O EOD aggregates (OI, PCR), announcements ingestion (raw + keyword index).
✅ Cross-exchange price sanity checker live; flows queryable 10 years back where sources permit; announcements searchable within 1 hour of EOD poll.

**M4 — Query service + backtest engine** *(depends: M2)*
Query API (4 canonical shapes §4.5), PIT access rules, SimBroker + shared cost model, SIP simulation, portfolio accounting (XIRR), benchmark series.
✅ Hand-computed reference case (3 stocks, 5 years, SIP, one split + one merger + one demerger) reproduced exactly; naive momentum backtest runs end-to-end 10 years; PIT leak test passes; replay determinism in CI.

**M5 — Analyst agent v1, paper mode** *(depends: M4; gate of maximum interest)*
Interview flow, theme mapper with purity scores, thesis engine + ratification UX, rails engine, rotation dial + sleeves, cash manager, T0 monitoring, decision journal, SimBroker wiring, data-red interlock.
✅ The AI/Robotics case (₹10k/mo, 5 yr, high risk) created end-to-end through the interview; proposal ratified; runs unattended in paper mode 10 consecutive sessions; journal complete for every day incl. heartbeats; rails demonstrably block an injected oversized order; SIP instalment correctly parked then deployed.

**M6 — Monitoring depth (T1/T2) + evidence packs** *(depends: M5)*
News/geo ingestion (GDELT/RSS/PIB), T1 triggered reviews with evidence bundles, T2 scheduled deep reviews, token accounting, evidence pack generator.
✅ Thesis-break fire drill: injected news matching a break condition escalates T0→T1, produces a verdict + journaled action within policy; monthly evidence pack auto-generates; token cost per decision visible in journal.

**M7 — Fundamentals waterfall** *(depends: M3; parallel with M5/M6)*
Screener export ingestion (restated store, monitoring-only), XBRL filings parser building the PIT store from now forward, fundamentals wired into T1/T2 evidence bundles and break-condition evaluation.
✅ Source Register rows for fundamentals flip to VERIFIED; every fundamental datum carries (period_end, filing_date); backtest harness proves restated store is unreachable from PIT queries.

**M8 — Real-money readiness** *(depends: M5 + M6 stable in paper)*
KiteBroker adapter, order staging against live account, daily reconciliation, kill switch, SEBI/Zerodha rules verification memo, graduation evidence pack review.
✅ 10 sessions of tiny-capital live orders reconcile clean; kill switch fired and verified mid-session; rules memo journaled; **graduation itself remains your discretionary call (decision #8) — this gate makes the call informed and the machinery safe.**

**Continuous tracks (no gate):** data-quality sentinel rules grow every time something surprises; golden CA suite grows during backfill; runbooks; monthly restore drill; monthly burn report.

---

## 10. Risk Register

| Risk | L | I | Mitigation |
|---|---|---|---|
| Corporate-action errors corrupt adjusted history | M | **H** | L0 immutability + derived-only adjusted data + golden suite + D7 20%-move tripwire; two-source reconciliation |
| NSE/BSE change URLs, formats, or anti-bot posture | **H** | M | Crawl policy engine isolates per-source fetchers; L0 means never re-fetch history; fallback mirrors in Source Register; failures loud via status API |
| Agent judgment is poor (the novel risk) | M | **H** | Paper mode as primary harness; falsifiable break conditions limit discretion; journal + evidence packs make quality measurable; discretionary graduation |
| Look-ahead bias flatters backtests | **H** | **H** | PIT store keyed on filing date; restated-data quarantine; automated PIT leak test in CI |
| Scope creep (solo + no deadline + full data scope) | **H** | M | Agent-contact rule (M5 unblocked by M7); gates require acceptance, not polish; this register reviewed at every gate |
| Silent drift between paper and real behavior | M | **H** | One code path, broker interface only difference; daily reconciliation; kill switch |
| LLM cost blowout under cost-blind policy | M | L→M | X3 metering per decision; tiered monitoring caps the structural burn; monthly burn report converts "blind" to "measured" |
| SEBI/exchange algo rules shift | M | M | M8 verification memo; order-rate stays trivially low; product phase gets its own compliance workstream |
| Key-person: only you | **H** | M | Boring stack, runbooks, this document, journal as institutional memory |
| Data redistribution rights at product stage | M | M | Personal use fine; **public archive downloads of exchange data likely need an exchange data license — legal check before product launch** |

---

## 11. Deferred Decisions (product phase — deliberately not now)

SEBI RA/RIA registration path · multi-tenant activation (model is ready, deployment is not) · product UI beyond ratification pages · pricing · exchange data-license for redistributing archives · formal graduation gate replacing discretionary · marketing name for the two systems.

---

## 12. Amendment Log

| Date | Decision # | Change | Reason |
|---|---|---|---|
| 2026-08-08 | — | v1.0 ratified | Initial constitution (15 decisions) |
| 2026-08-08 | — | **PROPOSED** (M0.1): §8.2 System 1 package `platform/` → `dataplatform/` | `platform` is a stdlib module name, so it cannot be a top-level package: after anything imports the stdlib module (pytest does) `platform.config` raises *'platform' is not a package*, and when the local package wins the path race `import pandas` fails on `platform.python_implementation()`. Both reproduced at M0.1. Only the directory name changes — module boundaries, IDs and ownership per §8.2 are untouched, and no §1 decision is affected. Two `verify` commands in TASK_GRAPH.yaml (C.1, M1.9) were updated to `python -m dataplatform.ingest.…`; other `platform/…` strings in the plan and graph read as `dataplatform/…` per CLAUDE.md. |

*Amendments to §1 require an entry here. Everything else evolves freely.*

