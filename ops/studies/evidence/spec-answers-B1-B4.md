# Targeted answers B1–B5 — in-repo evidence for the fund data study

Read-only investigation of `/home/ubuntu/stock-manager`. Nothing in the repo was changed.
Live measurements taken 2026-09-07 against the running Postgres and the on-disk lake.
Companion document: `spec-entity-baseline.md` (same directory).

---

# B1 — HISTORY DEPTH: 10 years is a *derived* number, not a constitutional one

## Every place the plan commits to a depth

| Where | Text |
|---|---|
| `EXECUTION_PLAN.md:10` | "a free-source, NSE+BSE, EOD market-data platform with **10 years of corporate-action-correct history**" |
| `EXECUTION_PLAN.md:342` | "**M1 — NSE price core, 10-year backfill**" |
| `EXECUTION_PLAN.md:344` | "10 years NSE equities in L1; gap report explains 100% of missing days" |
| `EXECUTION_PLAN.md:352` | "flows **queryable 10 years back where sources permit**" |
| `EXECUTION_PLAN.md:356` | "naive momentum backtest runs **end-to-end 10 years**" |
| `AGENTIC_CONTEXT.md:88` | "**The 10-year backfill go** (B1) and any other bulk-fetch campaign over ~200 requests to one source." |
| `AGENTIC_CONTEXT.md:43` | B1: "Agents verify every Source Register URL pattern for real, fetch a ~60-session fixture spread across all format eras... The **10-year backfill execution itself is `NEEDS_GO`** — parked for a one-word human go." |
| `TASK_GRAPH.yaml:13` | `M1: {title: NSE price core + 10y backfill, depends: [M0]}` |
| `TASK_GRAPH.yaml:233` | Calendar holidays covering "the last **10 years** + current year" |
| `TASK_GRAPH.yaml:239` | `expected_sessions(2016-01-01..2026-08-07)` |
| `TASK_GRAPH.yaml:433` | "Under B1 the agent may run --limit sampling (~60 sessions across eras) but NOT the full 10-year run." |
| `TASK_GRAPH.yaml:436, 440` | "--dry-run over 10 years"; verify `--from 2016-01-01 --to 2026-08-07 --dry-run` |
| `TASK_GRAPH.yaml:499-516` | M1.13 "Execute 10-year NSE backfill"; "~4-6 hours of rate-limited fetching"; escalation question quotes the same |
| `TASK_GRAPH.yaml:527` | M1.14 gate: "10y in L1" |
| `TASK_GRAPH.yaml:775-776` | M3.4: "queryable 10 years back where the source permits (M3 gate box 2 — **if depth is shorter, record the real depth in the source register rather than claiming 10y**)" |
| `TASK_GRAPH.yaml:877` | M3.10 gate: "flows queryable as far back as sources genuinely permit (**state the real depth per source, don't claim 10y if it isn't there**)" |
| `TASK_GRAPH.yaml:897` | M4.1: "a benchmark test asserting both shapes return in a sane time on the full 10-year store" |
| `TASK_GRAPH.yaml:1052-1067` | M4.10 "Naive momentum backtest, 10 years"; acceptance "10-year run completes"; **verify `--from 2016-04-01 --to 2026-03-31`** |
| `TASK_GRAPH.yaml:1094` | M4.12 gate: "naive momentum runs 10 years end-to-end" |
| `TASK_GRAPH.yaml:1765, 1771` | M9.1 CA backfill "over the L1 price window (**2016-09..2026-09**)" |
| `TASK_GRAPH.yaml:1787, 1792, 1812` | M9.2/M9.3 "the 10y run" |
| `TASK_GRAPH.yaml:1953` | M10.4 escalation: "Run the **10-year** NSE XBRL fundamentals backfill?" |
| `TASK_GRAPH.yaml:2194-2196` | M12.3 windows: "the full **decade 2016-09 to 2026-08**", the "**2019-07 to 2026-08**" window, walk-forward 2016-09..2021-08 / 2021-09..2026-08 |
| `HUMAN_DECISIONS.md:25, 34, 545, 553` | B1/D1 and the M1.13 entry: "all 10 years in L1, 5.68M rows" |

**No document anywhere says 20 years or twenty years.** Grep over `EXECUTION_PLAN.md`,
`AGENTIC_CONTEXT.md`, `TASK_GRAPH.yaml`, `HUMAN_DECISIONS.md`, `ops/BACKLOG.md`, `CLAUDE.md` returns
nothing for `20 year|twenty year`.

## Is 10 years reasoned or arbitrary?

**The repo answers this itself, and the answer is: arbitrary, and known to be arbitrary.** The
decisive line is in the owner-approved D8 memo:

> "**It is silent on depth. The binding constraint is 10 years, set by M4.10's verify line
> (`TASK_GRAPH.yaml:1063`)** — not the case fixture's 5-year horizon."
> — `HUMAN_DECISIONS.md:263-264`

That is the plan's own reading: the depth is not a constitutional decision, it is a **consequence of
one task's `verify` command**. Three further confirmations:

1. **It is not in §1, the Decision Register.** The 15 constitutional decisions
   (`EXECUTION_PLAN.md:22-38`) never mention history depth. Decision #6 covers *which data classes*
   ("All five data classes in v1"), #14 pace, #15 sequencing. §1's own preamble: "**Changes to these
   require an explicit amendment (§12). Everything else in this document is derived and
   negotiable**" (`:20`).
2. **No cost, source-depth or licensing rationale is ever given for the number 10.** The only depth
   *cost* reasoning in the repo runs the other way: B1's threshold is about **request volume**, not
   years — "any other bulk-fetch campaign over ~200 requests to one source"
   (`AGENTIC_CONTEXT.md:88`). M1.13 sizes the run at "~4-6 hours of rate-limited fetching"
   (`TASK_GRAPH.yaml:505`) — a cost statement about *the run*, not a justification of *the horizon*.
3. **Where sources are shallower than 10 years, the plan instructs honesty rather than treating 10 as
   a target to hit** (`TASK_GRAPH.yaml:775-776`, `:877`). That is the posture of a convenient round
   number, not of a requirement.

## Does any doc discuss going deeper?

Yes — three places, all saying deeper is *available* and unexploited.

| Finding | Citation |
|---|---|
| **NSE bhavcopy has a deeper era, already fetched.** "The NSE historical archive holds a *third* cash-market era before the one M1.4 parses: `cm04JAN2010bhav.csv` (fetched during M1.4, kept in L0, not checked in) has eleven columns and **neither `TOTALTRADES` nor `ISIN`**. `bhavcopy_legacy.parse` refuses that header rather than inventing an identity for rows that have none (invariant #2)... Nothing needs it today — **the C.2 calendar only covers 2016-01-01 onwards, so no planned backfill reaches it** — but **a deeper history would need its own parser plus a symbol→ISIN resolution through D2, and the exact year the `ISIN` column appears is unpinned**." | `ops/BACKLOG.md:47` |
| Mirrored in the machine register, `nse_bhavcopy_legacy.pit_notes`: "Carries ISIN directly, so it joins without a master — **but only back to some point in the early 2010s**." `era.start: null`. | `dataplatform/ingest/source_register.yaml` |
| **NIFTY TRI reaches 2001 in one request.** Probed live 2026-08-10: "NIFTY 50, max depth · 200 · 880,867 B · 6,213 rows · **02 Apr 2001** → 30 Mar 2026"; "**25 years of all three indices is available in one request each**." Owner's binding consequence: "**Ingest daily TRI even though nothing downstream needs daily levels — 25 years is one request, and re-fetching later is the expensive path.**" | `HUMAN_DECISIONS.md:250, 254, 234-235` |
| **Fundamentals discovery reaches ~10 years, contradicting M7.3's "forward only" framing.** "**Index history goes back 10 years**, so the 'forward only' framing in M7.3's spec is too pessimistic for the discovery half... 135,197 announcements over FY2016-17..FY2026-27, of which 101,446 have an XBRL document (documents start around FY2018-19; the first two years are 100% placeholder)... **Worth amending the spec text so a future reader does not under-scope the backfill.**" | `ops/BACKLOG.md:138` |
| **The macro archive epoch was *measured*, not assumed** — 2012-10-01 for `ind_close_all` — showing the project does derive real epochs when it looks. | `TASK_GRAPH.yaml:2080-2081, 2099` |

## Verdict: is extending to 20 years an amendment?

**No §12 amendment is required.** The 10-year figure appears nowhere in §1's Decision Register, and §1
states everything outside it "is derived and negotiable" (`EXECUTION_PLAN.md:20`). D8 already
established on the record that depth is set by a task verify line, not by the constitution
(`HUMAN_DECISIONS.md:263-264`).

What extending *does* require, per the docs:

1. **A §1-external plan edit** to `EXECUTION_PLAN.md:10, 342, 344, 352, 356` and the M1/M4 gate text
   — negotiable, no amendment-log entry needed.
2. **A `NEEDS_GO` owner decision per campaign** — B1/§3.3 reserves any bulk fetch over ~200 requests
   (`AGENTIC_CONTEXT.md:88`). A 20-year price backfill is a second M1.13-shaped go.
3. **Two hard engineering prerequisites named in the repo**, both blocking below roughly 2013:
   - **A third bhavcopy parser** for the eleven-column pre-ISIN era, *plus* symbol→ISIN resolution
     through D2, because that era has no ISIN column and invariant #2 forbids joining on a symbol
     (`ops/BACKLOG.md:47`). "The exact year the `ISIN` column appears is unpinned" — that year, not
     10 or 20, is the real technical boundary.
   - **Calendar coverage.** `expected_sessions` "refuses a range that leaves the span below rather
     than assuming an uncovered year has no holidays"
     (`dataplatform/ingest/data/nse_holidays.yaml:16-18`) — so `nse_holidays.yaml` must be extended
     backwards before any pre-2016 backfill can even be planned, let alone gap-reported.
4. **BSE cannot go deep at all on ISIN.** `bse_bhavcopy_legacy.pit_notes`: "there is **NO ISIN column**
   in this era — only SC_CODE. Backfilled BSE rows must be resolved to ISIN through the BSE scrip
   master (D2); joining on SC_CODE or SC_NAME anywhere downstream breaks invariant #2." And D14
   records the BSE campaign started only at "**2024-07-08 (the UDiFF cutover — the legacy era has no
   ISIN and is refused)**" (`HUMAN_DECISIONS.md:519-520`).

---

# B2 — IS A "CASE" THE FUND ABSTRACTION?

Yes for investment policy and thesis. No for units, NAV, external capital, fees and tax lots.

## (a) What `case_` + `policy_set` + `thesis` ALREADY gives a themed fund

1. **A named, themed mandate with its own lifecycle.** One `case_` row per fund-like book, moving
   through the §5.1 states `DRAFT → INTERVIEW → PROPOSAL → RATIFIED → FUNDED(paper|real) → ACTIVE ⇄
   SUSPENDED → CLOSED`, with illegal transitions raising and every transition journaled
   (`EXECUTION_PLAN.md:183`; M5.3 acceptance, `TASK_GRAPH.yaml:1157`).
2. **N funds genuinely coexist.** `case_id` is the partition key on `policy_set`, `thesis`, `order_`,
   `decision_journal` and `token_usage` (`0001_init.sql:289,331,363,415,472`), so two cases can hold
   the same ISIN under *different* theses, different rails and different sleeves at once
   (`thesis` UNIQUE `(case_id, isin, version)`).
3. **A versioned, immutable governing document per fund.** `policy_set` is append-only at the DB
   level; a policy change is a new version requiring fresh ratification, so "which rails were in
   force when that order was placed" stays answerable years later (`0001_init.sql:304-309`).
4. **Ratification provenance with a paper/real firewall.** `ratification_kind ∈ {HUMAN, FIXTURE}` —
   the B9 fixture is valid for paper and tests only; `FUNDED(real)` must refuse it, proven by test
   (M5.3 acceptance, `TASK_GRAPH.yaml:1159`).
5. **Per-fund risk limits as first-class scalars, enforceable and unbypassable.** `rotation_dial_pct`,
   `max_position_pct`, `max_sector_pct`, `min_holdings`, `drawdown_review_pct` promoted out of JSON
   so A8 reads scalars (`0001_init.sql:309`); invariant #6 makes them unoverridable, and a blocked
   order is a stored `RAIL_BLOCKED` row plus a `RAIL_BLOCK` journal line, not an absence.
6. **A per-fund capital plan.** SIP amount and day-of-month on the case row, driving
   `SipInstalment(case_id, due_date, amount_inr)` and the A7 park/deploy queue.
7. **A per-fund benchmark pair declaration** — `benchmark_primary` + `benchmark_secondary`, realised
   as `BenchmarkComparison(portfolio_xirr, benchmark_xirr, theme_xirr)` on one shared external
   cashflow schedule (`backtest/accounting.py:121-132`).
8. **A falsifiable, versioned investment case per holding.** `thesis` carries driver, theme purity
   (0–1), expected evidence and typed break conditions; every CORE holding must hold a RATIFIED
   thesis before its first buy, enforced in code (M5.6 acceptance, `TASK_GRAPH.yaml:1217`); an edit
   creates a new version and the old row keeps `SUPERSEDED`.
9. **Two-sleeve structure inside one fund.** `sleeve ∈ {CORE, TACTICAL}` on the thesis and
   `{CORE, TACTICAL, CASH}` on orders and journal lines, with CORE membership changing only on a
   `BROKEN` verdict and the boundary itself being a ratifiable policy.
10. **A per-fund audit trail sufficient for external scrutiny.** `decision_journal` is append-only
    (statement-level triggers), keyed by `case_id`, includes no-op HEARTBEAT days, and points at a
    content-addressed evidence snapshot of exactly what the model saw.
11. **A cross-fund concentration view.** `CrossCaseExposure(isin, quantity, by_case)` aggregates net
    executed quantity across every FUNDED/ACTIVE/SUSPENDED case
    (`analyst/cases/service.py:148-166, 229-244`).
12. **Per-fund LLM cost attribution.** `token_usage.case_id` + `decision_journal_id`, so burn is
    attributable per fund per decision.

## (b) What it does NOT model that a real themed fund needs

| Missing capability | Present? | Nearest thing in the repo |
|---|---|---|
| **Unit ledger / NAV per unit** | **NO** | Nothing comes close. Grep over all 10 migrations finds no `nav`, `unit`, `units_outstanding`, `unit_price`, `nav_per_unit`, or `fund` column. The only total-value construct is `Portfolio.total_value` — "Case value: deployed plus idle. What every percentage rail is a fraction of" (`analyst/rails/policies.py:170-172`) — a computed property of a frozen **in-memory** dataclass built per rail check, with **no valuation-date column and no persistence**. Nothing anywhere records "the case was worth X on date D." A case is a rupee book with no denominator. |
| **External subscriptions / redemptions from third parties** | **NO — single-investor only** | Two partial analogues, neither usable. (1) `case_.sip_amount_inr` + `sip_day_of_month` and `SipInstalment(case_id, due_date, amount_inr)` (`analyst/cases/service.py:215-226`) model exactly **one** owner-funded capital stream on a fixed monthly schedule (day capped at 28). (2) `PortfolioBook._external` — "External cashflows in XIRR sign convention: pay-in negative, pay-out positive" (`backtest/accounting.py:173-174`) — is the right *shape* for subscriptions and redemptions, but it is an unattributed list on an in-memory backtest object with **no `case_id` and no investor id**, and it exists only for the duration of one replay. There is no subscription entity, no redemption request, no investor, no allotment, no cut-off time, and no dilution/equalisation logic. |
| **Per-fund fee accrual** | **NO** | Nothing comes close. The one cost model is per-*trade* transaction charges only, and its own scope note says so: "Delivery equity on NSE and BSE only. Intraday, F&O, currency and commodity have a different rate card and are deliberately absent" (`execution/costs/rates.yaml:27-29`). Components are brokerage, STT, exchange txn, SEBI turnover fee, stamp duty, GST, DP charge — **no management fee, no expense ratio, no performance fee, no daily accrual**. Decision #12 is "Cost-blind for now" and meters only LLM tokens (`EXECUTION_PLAN.md:35`; `token_usage`). |
| **Per-fund benchmark attribution** | **PARTIAL — comparison yes, attribution no** | Closest: `BenchmarkComparison(portfolio_xirr, benchmark_xirr, theme_xirr)` with `excess_over_benchmark` and `excess_over_theme`, all three computed "from the *same* external cashflow schedule — the investor's actual instalments and withdrawals — so the excess figures are a like-for-like read" (`backtest/accounting.py:121-142`). That is a **single excess number**, not attribution: there is no allocation/selection/interaction decomposition, no sector or factor contribution, no per-holding contribution-to-return. Two further limits on record: only benchmark **names** are stored, as strings — "the only TRI-aware code in the tree stores benchmark *names* as strings (`analyst/cases/policies.py:176-181`)" (`HUMAN_DECISIONS.md:269`) — and the live series is a **computed proxy TRI, not the licensed NIFTY-TRI** (`ops/BACKLOG.md:115`; M9.4 acceptance forces the report to say so, `TASK_GRAPH.yaml:1830`). |
| **Investor-level tax lots** | **NO — and the fund-level lot is not a tax lot either** | Two near-misses. (1) `BookPosition(isin, quantity, cost_basis)` deliberately stores **one blended basis per ISIN**, not lots: "the average price is derived from it rather than stored, so a split (which changes the count but not the basis) needs to touch only two numbers and cannot leave the two inconsistent" (`backtest/accounting.py:98-108`). That design choice makes FIFO/LIFO lot tracking, holding-period determination and STCG/LTCG classification impossible as built. (2) `analyst/rails/policies.py:100-130` defines a class literally named `Lot`, but it is a **risk-concentration unit** — "One settled holding, valued at a reference price — the unit A8 measures concentration in" — one per ISIN, marked at a current price, with **no acquisition date and no basis**. The only tax-adjacent artefact specified anywhere is a *summary* line in the evidence pack: "turnover + tax-event summary by sleeve" (`EXECUTION_PLAN.md:260`) — fund-level, sleeve-grained, never investor-level, and never generated because `decision_journal` has 0 rows. |
| *(bonus gap, same family)* **Investor / owner identity** | **NO** | Only `policy_set.ratified_by text` and `thesis.ratified_by text` (`0001_init.sql:298,342`) — free text, no FK, no table. Decision #1's implication is "multi-tenant data model, single-tenant deployment" (`EXECUTION_PLAN.md:24`) and §11 defers "multi-tenant activation (model is ready, deployment is not)" (`:398`), but **no tenant/user/account entity was ever specified or built**. |
| *(bonus gap)* **Any persisted position or cash balance at all** | **NO** | Position is *derived*: `CaseService.cross_case_exposure()` sums `order_.filled_quantity` signed by side over `EXECUTED` orders, grouped by `(isin, case_id)` (`analyst/cases/service.py:148-158`), with the rationale "Quantities, not values: valuing a position needs a price, the price layer is D4's, and a case service that reached into it would own two jobs" (`:144-147`). Cash lives only on the in-memory `Portfolio.cash` / `PortfolioBook._cash`. The cash **ledger** is a protocol return, not a table: `Broker.ledger() -> tuple[LedgerEntry, ...]` (`execution/broker.py:334-335`); for `KiteBroker` it reads a placeholder path because Kite has no order-independent cash ledger (`ops/BACKLOG.md:119`). |

## Operational reality check

The multi-case machinery has **never held a single case**. Measured 2026-09-07:
`case_` = 0 rows · `policy_set` = 0 · `thesis` = 0 · `decision_journal` = 0 · `order_` = 0 ·
`token_usage` = 0 · `scheduler_heartbeat` = 0 · `job_run` = 0. Corroborated by D13: "**the
paper-trading job that runs this configuration daily is not yet built**: paper mode today is the
M5.13 harness with injected triggers and the scheduler registers no analyst session job; building it
is the next decision" (`HUMAN_DECISIONS.md:513-514`).

---

# B3 — INDEX CONSTITUENT HISTORY

## Correction: `AGENTIC_CONTEXT §4.1` does not exist

`ops/BACKLOG.md:126` cites "AGENTIC_CONTEXT §4.1". That section is not there. `AGENTIC_CONTEXT.md` §4
is **"Autonomy classes"**, lines 115–125, quoted here in full so the absence is verifiable:

```
115  ## 4. Autonomy classes
116
117  Every task in `TASK_GRAPH.yaml` carries one.
118
119  | Class | Meaning |
120  |---|---|
121  | `AUTO` | Build it. No human contact. The default and the overwhelming majority. |
122  | `NEEDS_GO` | Fully built and dry-run-verified by agents; *execution* needs a human go (B1). |
123  | `NEEDS_SECRET` | Blocked only by a missing credential (B4). Build everything behind the
     interface; park the live-exercise step. |
124  | `HUMAN_GATE` | Irreducibly human — ratification, graduation, real orders, legal. Agents
     prepare the decision packet and park. |
125  | `GATE_AUDIT` | Machine-verifiable milestone audit. Runs every acceptance criterion of the
     milestone and reports pass/fail per box; may not mark a gate passed on partial evidence. |
```

There is no §4.1 and nothing in §4 concerns constituents. The substantive text the backlog row means
is **`EXECUTION_PLAN.md` §4.1** — the Source Register — plus the machine register's `pit_notes`.
A reader chasing the printed citation finds nothing; worth fixing.

## The authoritative texts, verbatim

**`EXECUTION_PLAN.md:127` — §4.1 Source Register, row 7, all seven columns:**

> | Index constituents | niftyindices.com list CSVs (`ind_nifty50list.csv`, sectoral, thematic) |
> monthly | — | Free | **Snapshot per month → historical constituents accumulate from day one** |
> VERIFY-AT-BUILD |

**`dataplatform/ingest/source_register.yaml`, `nifty_index_constituents.pit_notes`, in full:**

> "**The file is always "as of today" — there is no historical constituents download.** Snapshot every
> month from day one; membership history accumulates and is what kills survivorship bias in M4's PIT
> universe."

Register metadata for that row: `status: VERIFIED` · `host: niftyindices.com` ·
`url_template: https://niftyindices.com/IndexConstituent/ind_{index_slug}list.csv` ·
`cadence: monthly` · `verified_at: 2026-08-08T18:11:10+05:30` · `last_http_status: 200` ·
`sample_bytes: 3352` · `owner_task: M3.9` · `era.start: null`, `era.end: null` ·
`robots_notes: "Allow: / with two disallowed report paths, neither of which is /IndexConstituent/."`

**`TASK_GRAPH.yaml:851-867` — M3.9's spec and acceptance, verbatim:**

```yaml
- id: M3.9
  title: Index constituents history + benchmark TRI
  milestone: M3
  module: D1
  autonomy: AUTO
  deps: [M1.2, M1.7]
  spec: |
    Ingest niftyindices constituent CSVs (NIFTY 50, sectoral, thematic) as monthly snapshots —
    constituent history accumulates from day one (§4.1) and is what kills survivorship bias in M4's
    PIT universe.
    Ingest benchmark TRI series (with the computed price-index + dividend-estimate fallback noted).
    Snapshot semantics: never overwrite a prior month's membership.
  deliverables: [dataplatform/ingest/indices.py, tests/unit/test_indices.py]
  acceptance:
    - "a monthly snapshot is stored immutably with its as-of date"
    - "membership as-of a historical date is queryable and returns the snapshot in force then"
    - "NIFTY-TRI series ingested and spot-checked against a published value"
  verify: "uv run pytest tests/unit/test_indices.py -q"
```

**`TASK_GRAPH.yaml:1880-1882` — M10.1, the hard limit stated to the caller:**

> "Note the hard limit for the caller: **each CSV is "as of today" — this ingests the *current*
> snapshot only; historical membership is M10.2's job.**"

**`TASK_GRAPH.yaml:1896-1903` — M10.2, the full reasoning and the only mechanism proposed:**

> "**niftyindices publishes constituents "as of today" only — there is no history of which names were
> in a sector index in past years, and a static-today map applied backward is survivorship-biased**
> (the research caveat). Wire a scheduled weekly job (M0.6 scheduler) that snapshots the broad and
> sectoral constituent lists with the capture date, appending a dated membership record each run so
> real point-in-time sector history accumulates going forward. Idempotent per (slug, week); a missed
> week is a visible gap, never silently filled. **Usable multi-year history accrues over time — this
> task builds the mechanism, it does not manufacture past history.**"

**`ops/BACKLOG.md:126` — the consequence, in full:**

> "M9.3's as-of index-membership screen (`UniverseParameters.index_slug`, default `nifty500`) **is
> inert on the real store because `index_constituents` is empty** — M3.9's snapshot history accrues
> one month at a time from day one and **no ten-year backfill of prior months exists to fetch**
> (AGENTIC_CONTEXT §4.1). So **the ten-year universe run is narrowed by the liquidity floor alone**;
> the membership intersection is only exercised on the integration fixture. **Once monthly constituent
> snapshots have accumulated (or a licensed history is sourced), the same screen constrains the live
> universe with no code change.** Also: the universe thresholds (`index_slug`,
> `median_turnover_floor`, `liquidity_lookback_days`) are a-priori constants with no CLI override on
> `--universe-report`; expose them if a sensitivity sweep is wanted."

**`TASK_GRAPH.yaml:1919-1920, 1929` — M10.3, forced to run on a biased map:**

> "PIT-safe via `membership_asof` (**never today's map for a past date once M10.2 history exists;
> until then, a static map with the survivorship caveat stated in the report**)" · acceptance:
> "**the survivorship/static-map limitation is stated explicitly until M10.2 history matures**"

## SOURCE limitation or LICENSING limitation?

**SOURCE, unambiguously — and the docs are explicit about the distinction, because they draw it
elsewhere for a different niftyindices endpoint.**

Evidence that it is a source limitation:

- "The file is always "as of today" — **there is no historical constituents download**"
  (register `pit_notes`). No historical download *exists* to be licensed.
- "niftyindices publishes constituents "as of today" only — **there is no history of which names were
  in a sector index in past years**" (`TASK_GRAPH.yaml:1897-1898`).
- The endpoint is **VERIFIED, free, and robots-permitted**: HTTP 200, 3,352 bytes,
  "Allow: / with two disallowed report paths, **neither of which is /IndexConstituent/**". Nothing is
  gated, refused, paywalled or credentialled.
- Contrast the *sibling* row on the *same host*: `nifty_tri_history` is `FAILED` with an explicit
  access diagnosis — "an **application-level session gate**, not a robots or rate-limit block" — and
  a fourth status value exists for credential blocks (`BLOCKED_CREDENTIAL`, used for Screener). The
  register therefore *has* the vocabulary to say "we are blocked from getting this", and it does not
  use it here. Constituents are not blocked; the history simply is not published.

The word "licensed" appears in this context exactly once, and as a **hypothetical alternative supply**,
not as a description of the current block: "(or a **licensed history is sourced**)"
(`ops/BACKLOG.md:126`).

## Does any doc propose a workaround?

Three responses appear in the docs, and no others. **Reconstructing membership from index-change press
releases is proposed nowhere** — I searched the plan, charter, task graph, decisions queue, backlog and
gate reports; there is no mention of index reconstitution circulars, press releases, or change
notifications as a membership source.

1. **Accumulate forward** (the only *built* mechanism). M10.2's weekly snapshot job —
   `dataplatform/ingest/constituents_snapshot_job.py`, 11,539 bytes, tests at
   `tests/integration/test_constituents_snapshot_job.py`. **Never run.** Explicitly disclaimed in its
   own spec: "this task builds the mechanism, **it does not manufacture past history**"
   (`TASK_GRAPH.yaml:1902-1903`).
2. **Buy a licensed history** (named once, no task, no supplier, no register row):
   "Once monthly constituent snapshots have accumulated (**or a licensed history is sourced**), the
   same screen constrains the live universe **with no code change**" (`ops/BACKLOG.md:126`). This is
   the only escape from the ten-year wait, and it is a purchase — §3.9 reserves "Spending money —
   paid data sources, paid API tiers, cloud resources" to the human (`AGENTIC_CONTEXT.md:96`).
3. **Substitute the liquidity floor and disclose the bias** (what every shipped report actually does).
   M9.3's universe is "narrowed by the liquidity floor alone" (`ops/BACKLOG.md:126`); M10.3 runs on a
   static-today map with a mandatory written caveat (`TASK_GRAPH.yaml:1929`); M12.2 defines its
   universe by two turnover floors (₹1 crore / ₹10 crore median turnover) rather than by membership
   (`TASK_GRAPH.yaml:2170-2172`).

## Quantified — measured 2026-09-07

| Measure | Value |
|---|---|
| `data/L0/nifty_index_constituents` payloads | **34** |
| `sync_state` for `nifty_index_constituents` | **16 PUBLISHED, 1 FAILED** (all one logical date) |
| `index_constituents` dataset in `data/L1` | **ABSENT** — `data/L1` holds only `pit_fundamentals`, `prices_raw`, `prices_raw_quarantine` |
| Distinct membership snapshots ever taken | **1** — "`index_constituents` 794 rows, 16 indices, **one snapshot**" (`ops/gates/data-catalogue-2026-09-06.md:86`), snapshot date 2026-09-03 (`:150`) |
| Weekly accumulator runs | **0** — "One snapshot; **the weekly job that would accumulate it has never fired**" (`data-catalogue:134`); `job_run` = 0 rows, `scheduler_heartbeat` = 0 rows |
| PIT membership history accrued | **~1 day**, against a 10-year price lake |
| Years to reach 10-year PIT membership at 1 snapshot/week from now | **10** |

## Compounding consequence for a theme fund

`Lot.sector` is a **required, non-blank** field on every rail check
(`analyst/rails/policies.py:111-122`, "a lot must name its sector"), and the sector classification's
only source in the whole repo is the `Industry` column of these same constituent CSVs
(`dataplatform/ingest/indices.py:388`; M10.1 acceptance "every liquid name resolves to an Industry
as-of a date via `membership_asof`", `TASK_GRAPH.yaml:1885`). So the **max-sector rail**
(`EXECUTION_PLAN.md:197`; `RailId`, `analyst/rails/policies.py:69`) has no live point-in-time sector
source either — the same single snapshot is its only input.

---

# B4 — THE SOURCE REGISTER, ONE ROW PER SOURCE

`dataplatform/ingest/source_register.yaml` — **1,557 lines, 30 sources** (not 28; count verified by
parsing the file). `era.start: null` means the register declares **no measured start**, i.e. the depth
is unpinned rather than unbounded. "In L0 today" = a directory of that name exists under `data/L0` on
this host, measured 2026-09-07.

| source_id | entity yielded | verified? | era.start → era.end | in L0 today? |
|---|---|---|---|---|
| `nse_bhavcopy_legacy` | OHLCV bar, NSE pre-UDiFF | VERIFIED | null → 2024-07-08 | **Y** (3,880) |
| `nse_bhavcopy_udiff` | OHLCV bar, NSE current | VERIFIED | 2024-07-08 → open | **Y** (1,066) |
| `nse_mto` | Delivery qty/pct (pre-2019 route) | VERIFIED | null → open | **Y** (1,518) |
| `nse_sec_bhavdata_full` | Delivery qty/pct | VERIFIED | 2019-09-30 → open | **Y** (3,424) |
| `bse_bhavcopy_udiff` | OHLCV bar, BSE current | VERIFIED | 2024-07-08 → open | **Y** (1,072) |
| `bse_bhavcopy_legacy` | OHLCV bar, BSE pre-UDiFF (no ISIN) | VERIFIED | null → 2024-07-08 | **Y** (3,878) |
| `nse_corp_actions` | Corporate action | VERIFIED | null → open | **Y** (22) |
| `bse_corp_actions` | Corporate action | VERIFIED | null → open | **Y** (13,378) |
| `nse_equity_list` | Instrument master (today's listings) | VERIFIED | null → open | **N** |
| `nse_symbol_changes` | Symbol history (cumulative renames) | VERIFIED | null → open | **N** |
| `bse_scrip_master` | Exchange listing, BSE (scrip_code→ISIN) | VERIFIED | null → open | **Y** (6) |
| `nifty_index_constituents` | Index membership + sector/industry | VERIFIED | null → open | **Y** (34) |
| `nifty_tri_history` | Benchmark TRI series | **FAILED** | null → open | **N** |
| `nifty_index_close_snapshot` | Index close + P/E, P/B, Div Yield | VERIFIED | null → open | **N** |
| `nse_fii_dii_flows` | FII/DII daily flow | VERIFIED | null → open | **N** |
| `nse_bulk_deals` | Bulk deal | VERIFIED | null → open | **N** |
| `nse_block_deals` | Block deal | VERIFIED | null → open | **N** |
| `nse_shareholding_pattern` | Shareholding / promoter pledge | VERIFIED | null → open | **N** |
| `nse_fo_bhavcopy` | F&O contract row (OI, PCR, basis) | VERIFIED | 2024-07-08 → open | **N** |
| `nse_announcements` | Corporate announcement, NSE | VERIFIED | null → open | **N** |
| `bse_announcements` | Corporate announcement, BSE | VERIFIED | null → open | **N** |
| `gdelt_v2_event_files` | News / geopolitical item | VERIFIED | null → open | **N** |
| `gdelt_doc_api` | News item (article search) | **FAILED** (429) | null → open | **N** |
| `curated_rss` | News item (RBI press releases) | VERIFIED | null → open | **N** |
| `screener_company_fundamentals` | Restated fundamentals (monitoring-only) | **BLOCKED_CREDENTIAL** | null → open | **N** |
| `nse_financial_results_index` | PIT fundamentals — filing discovery | VERIFIED | null → open | **Y** (184) |
| `nse_integrated_filing_index` | PIT fundamentals — filing discovery (post-SEBI IF) | VERIFIED | 2025-04-01 → open | **Y** (228) |
| `nse_xbrl_filing` | PIT fundamentals fact (the document) | VERIFIED | null → open | **Y** (163,504) |
| `worldbank_indicator_api` | Macro series, annual (current vintage only) | VERIFIED | null → open | **N** |
| `alfred_series_vintage` | Macro series, vintaged (true PIT) | **FAILED** (unreachable) | null → open | **N** |

## Roll-up

- **25 VERIFIED · 3 FAILED** (`nifty_tri_history`, `gdelt_doc_api`, `alfred_series_vintage`) ·
  **1 BLOCKED_CREDENTIAL** (`screener_company_fundamentals`) · **1 VERIFIED but never fetched**
  (`bse_bhavcopy_legacy` has L0 payloads from the D14 campaign, but the register notes pre-2024 files
  carry no ISIN so the L1 backfill is refused).
- **13 of 30 have an L0 tree; 17 have never been fetched.** Total 202,410 L0 payloads.
- Status enum is `VERIFIED | FAILED | BLOCKED_CREDENTIAL` (`source_register.py:54-64`), and **D12 is
  open** precisely because that enum cannot express "declined on policy grounds"
  (`HUMAN_DECISIONS.md:466-499`).
- **Depth is bounded by the source, not by the plan, in five rows:** delivery before 2019-09-30 only
  via `nse_mto` · BSE OHLCV with ISIN only from 2024-07-08 · F&O only from 2024-07-08 · index
  membership **today only** · integrated filings only from 2025-04-01.
- **Three sources are deeper than the plan's 10 years:** NIFTY TRI to 2001-04-02 (25 y, one request —
  see B5), the index close snapshot to a measured 2012-10-01, and the NSE legacy bhavcopy into the
  early 2010s (with no ISIN column below some unpinned year).
- Sweep provenance: task C.1, `swept_at 2026-08-08T18:16:38+05:30`, 47 requests total, max 11 to any
  one host, against a ~200-request human-authorisation threshold. "**No retry with a different UA, no
  attempt to evade any block, no login anywhere.**"

---

# B5 — THE 25-YEAR BENCHMARK: one unexecuted request, confirmed

## The decision

D8, answered by the owner 2026-08-10 (`HUMAN_DECISIONS.md:214-239`). Probe results, live, 4 POSTs
≥2.5 s apart, no 403 and no 429 (`:245-254`):

| index | HTTP | bytes | rows | earliest | latest |
|---|---|---|---|---|---|
| NIFTY 50 | 200 | 177,036 | 1,239 | 01 Apr 2021 | 30 Mar 2026 |
| NIFTY IT | 200 | 168,505 | 1,239 | 01 Apr 2021 | 30 Mar 2026 |
| NIFTY CPSE | 200 | 170,170 | 1,239 | 01 Apr 2021 | 30 Mar 2026 |
| **NIFTY 50, max depth** | **200** | **880,867** | **6,213** | **02 Apr 2001** | **30 Mar 2026** |

> "`POST https://niftyindices.com/BackPage/getTotalReturnIndexString` — **no `.aspx`** — body
> `{"cinfo": "{'name':'NIFTY 50','startDate':'...','endDate':'...','indexName':'NIFTY 50'}"}`,
> **no session cookie, no Referer**. **25 years of all three indices is available in one request
> each.**" (`:252-254`)

Binding consequences the owner recorded (`:230-239`):

> - "The register row **is corrected to the real path** `POST /BackPage/getTotalReturnIndexString`
>   (no `.aspx`) with the `cinfo` string envelope. **That correction is what actually unblocks the
>   graph; it must not wait on the parser being finished.**"
> - "**Ingest daily TRI** even though nothing downstream needs daily levels — **25 years is one
>   request, and re-fetching later is the expensive path.**"
> - "`nifty_tri_history` is marked `FAILED` because the register recorded a **stale URL path**, not
>   because the source is gated." (`:241-242`)
> - "**Split approved.** M3.9 becomes a constituents task and a TRI task, so that no single task owns
>   one VERIFIED and one FAILED source row." (`:226-228`)

Three parser traps recorded as build contract, not folklore (`:290-293`): rows arrive
**newest-first**; index names must be sent in **CAPS** and echo back title-cased; and a 5th key
`RequestNumber` **regenerates on every request** — "hash the payload as-is and determinism dies".

## Confirmed: the decision was never executed

| Check | Result |
|---|---|
| `data/L1/benchmark_tri` | **Does not exist.** `data/L1` holds only `pit_fundamentals`, `prices_raw`, `prices_raw_quarantine`. `find data -iname '*benchmark*' -o -iname '*tri*'` returns nothing. |
| `sync_state` rows for TRI | **None.** Sources present: `nse_xbrl_filing`, `bse_corp_actions`, `nse_bhavcopy`, `bse_bhavcopy_legacy`, `bse_bhavcopy`, `nse_integrated_filing_index`, `nse_financial_results_index`, `nifty_index_constituents`, `nse_corp_actions`. No TRI source of any name. |
| `data/L0` tree for `nifty_tri_history` | **Absent.** Not among the 13 L0 trees. |
| **The register URL was never corrected** | `source_register.yaml:715-716` still reads `url_template: "https://niftyindices.com/Backpage.aspx/getTotalReturnIndexString"` and `verified_url:` the same — **still `.aspx`**, the exact stale path D8 identified. Status still `FAILED`. The one consequence the owner said "must not wait" is the one that never happened. |
| **The approved split was never made** | `grep -n 'M3.9' TASK_GRAPH.yaml` shows a single `- id: M3.9` at line 851 and no `M3.9.a`/`M3.9.b`. `BUILD_STATE.json` holds exactly one `M3.9` entry. The task that "can be neither honestly done nor honestly blocked" was closed as done instead of split. |

## The task that owns it, and its recorded status

**`M3.9` — "Index constituents history + benchmark TRI"** (`TASK_GRAPH.yaml:851-867`), milestone M3,
module D1, autonomy `AUTO`, deps `[M1.2, M1.7]`. Its third acceptance criterion is "**NIFTY-TRI series
ingested and spot-checked against a published value**".

**Recorded status: `DONE`**, attempts 1, commit `3b24ae7`. Its `BUILD_STATE.json` note reads:

> "Immutable monthly index-constituent snapshots (`index_constituents` dataset) with PIT
> `membership_asof`; **TRI via §4.1 computed fallback (`nifty_tri_history` is session-gated/FAILED)
> seeded to published close, native `getTotalReturnIndexString` parser kept ready.** 29 te[sts]..."

That note asserts the source is "session-gated/FAILED" — **the premise D8 disproved on 2026-08-10**.
The task was recorded DONE against the stale-URL diagnosis, satisfying the acceptance box with the
computed fallback rather than the real series, and the correction that would have reopened it was
never applied to the register.

Downstream, `M9.4` (also `DONE`) built on that: its spec says "The licensed niftyindices TRI endpoint
is **session-gated and FAILED at C.1**, so use M3.9's computed TRI (seeded to the published close) as
the benchmark... **State plainly in the report that it is the computed TRI, not the licensed feed, and
what that means for reading excess return**" (`TASK_GRAPH.yaml:1822-1826`). Its state note: "Benchmark
now reads M3.9's computed TRI (`read_tri_series`)... Report states computed-not-licensed provenance."
So every benchmark figure in every M9/M10/M12 report — including the M12.3 verdict on the owner's >25%
bar — is measured against a **proxy** built from L1, not against the real total-return index that a
single POST would supply.

## Answer

**Yes — 25 years of NIFTY 50, NIFTY IT and NIFTY CPSE total-return history is one unexecuted request
away, per index.** Verified live, keyless, cookieless, robots-clean, at ~880 KB and 6,213 rows for the
full-depth NIFTY 50 pull. The owner authorised ingesting it on 2026-08-10. Nothing fetched it, the
register still carries the stale `.aspx` path that made it look impossible, `M3.9` is recorded `DONE`
on the computed fallback, and the parser is described in that task's own note as "kept ready".

Three things stand between the request and usable data, all named in the docs and none of them a
source block: the **register URL correction** (`source_register.yaml:715-716`); the **three parser
traps** (newest-first rows, CAPS index names, regenerating `RequestNumber` — the last one is a
determinism hazard under invariant #11, not a nicety); and the fact that **`benchmark_tri`'s writer
exists** (`dataplatform/ingest/indices.py:127, 948-955` — `index_slug`, `index_name`, `as_of`,
`tri_value`, `price_close`, `method`, `source`, `l0_key`) with a `method` column already designed to
record computed-vs-licensed provenance. The store is built and empty.
