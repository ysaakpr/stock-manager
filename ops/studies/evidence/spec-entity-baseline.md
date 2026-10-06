# ENTITY BASELINE — what this repo has already decided it needs

**Read-only investigation. Nothing was changed in /home/ubuntu/stock-manager.**
Sources: `EXECUTION_PLAN.md`, `AGENTIC_CONTEXT.md`, `TASK_GRAPH.yaml`, `BUILD_STATE.json`,
`HUMAN_DECISIONS.md`, `README.md`, the 10 SQL migrations, the Parquet schema declarations, and live
counts from the running Postgres + on-disk lake (measured 2026-09-07).

## 0. Two framing facts that condition everything below

1. **`BUILD_STATE.json` is stale.** It records 98 tasks, last written `2026-09-06T03:55:30+00:00`
   (`BUILD_STATE.json` -> `updated_at`). `TASK_GRAPH.yaml` holds 106. Eight tasks have **no state
   entry at all**: `M6.8, M8.3, M8.4, M10.7, M11.2, M12.1, M12.2, M12.3` — yet `M10.7`, `M12.1`,
   `M12.2`, `M12.3` are committed and merged (`git log`: `dea697d [M10.7]`, `c7ed5e6 [M12.1]`,
   `ac099ff/bb489a5/897eb0e [M12.2]`, `c9f0ed5 [M12.3]`). So for those, code+report is the only
   evidence; build state says nothing.
2. **Two off-graph packages exist.** `backtest/forecast.py`, `backtest/forecast_run.py`,
   `backtest/policies/forecast_daily.py` (1,827 lines) were committed under a bare `[X2]` tag
   (`bc5e559`, `eb94299`) with **no TASK_GRAPH task id**. `ops/gates/X2-forecast-daily-report.md` is
   their only spec.

---

## 1. ENTITY CATALOG

Legend for **Status**: `DONE` = task DONE in build state *and* code present; `CODE-ONLY` = code+tests
exist, **zero rows ever landed**; `PLANNED` = task exists, not started; `MENTIONED-ONLY` = named in a
doc, no task, no code.

### 1.1 Identity (D2) — the join spine

| Entity | Plan definition | Attributes as specified | Owner / path | Task(s) | Status | Evidence |
|---|---|---|---|---|---|---|
| **Instrument / security master** | `security_master(isin, symbol_history[], exchange_listings[], primary_exchange, series, status)` — EXECUTION_PLAN.md:148; "One row per instrument, keyed by ISIN... Delisted securities are kept with status DELISTED and never removed" | `isin` (PK, domain-checked), `name`, `primary_exchange` in {NSE,BSE}, `status` in {ACTIVE,SUSPENDED,DELISTED}, `face_value_inr`, `first_seen_date`, `last_seen_date`, `created_at`, `updated_at` | `dataplatform/identity/master.py`, `dataplatform/store/migrations/0001_init.sql:75-94` | M1.7, M3.2 | **DONE** | 8,598 rows (5,120 ACTIVE / 1,167 SUSPENDED / 2,311 DELISTED). Caveat on record: master built from `tests/fixtures/`, not a live `EQUITY_L.csv` — `ops/BACKLOG.md:57`, `ops/gates/data-catalogue-2026-09-06.md:63` |
| **Symbol history** | "Keep full change history, never overwrite" — EXECUTION_PLAN.md:126; "Rows are appended on a rename, never overwritten, because yesterday's file still says the old name" | `id`, `isin`->FK, `exchange`, `symbol`, `series`, `valid_from`, `valid_to` (NULL=current), `source`, `recorded_at`; UNIQUE(isin,exchange,symbol,valid_from) | `0001_init.sql:96-115`, `dataplatform/identity/master.py` | M1.7 | **DONE** | 11,359 rows. 52 of 2,886 windows clamped because NSE's listing date post-dates the rename chain — `ops/BACKLOG.md:58` |
| **Exchange listing** | "dual-exchange dedup; primary listing" — EXECUTION_PLAN.md:92 | `isin`,`exchange` (PK), `security_code` (BSE scrip / NSE token — "stored so a raw file can be traced back, never used as a join key", `0001_init.sql:134-136`), `series`, `lot_size`, `face_value_inr`, `listing_date`, `delisting_date`, `status`, `recorded_at` | `0001_init.sql:117-136`, `dataplatform/identity/primary.py` | M1.7, M3.1, M3.2 | **DONE** | 10,870 rows (NSE 2,397 / BSE 8,473). `ops/BACKLOG.md:90` recorded `exchange_listing` as NSE-only; BSE scrip master has since landed (6 L0 payloads) |
| **ISIN lineage** *(not in the plan; added at build time)* | "One directed edge per ISIN reissue: the retired ISIN, the ISIN that replaced it, and the successor's first trading session" — `0009_isin_lineage.sql:49-54` | `predecessor_isin` (deliberately **no FK** — "absent from security_master by construction", :56-58), `successor_isin`->FK, `effective_date`, `detected_by` in {L1_CONTIGUITY,MANUAL}, `confidence` in {CORROBORATED,DERIVED}, `gap_sessions`, `symbol_at_change`, `corroborating_action` in {SPLIT,BONUS}, `computed_at` | `dataplatform/identity/lineage.py`, `lineage_rebuild.py`, `0009_isin_lineage.sql:32-79` | *no task id* | **DONE (off-graph)** | 399 rows. Explicitly **not** a merger/demerger map — one predecessor -> exactly one successor, chain kept linear (`0009:24-27`) |
| **Identity reconciliation queue** | "A symbol resolving to two ISINs on one date is a hard error surfaced to a reconciliation queue, not a silent pick" — TASK_GRAPH.yaml:393-394 | `kind` in {SYMBOL_TO_ISIN,ISIN_TO_SYMBOL}, `exchange`, `on_date`, `symbols[]`, `isins[]` (both sorted, neither an FK), `detected_by` in {INGEST,RESOLVE}, `source`, `detail`, `resolved/resolved_at/resolution`, `raised_at` | `0004_identity_reconciliation.sql:27-75` | M1.7 | **DONE** | 10 rows |
| **Alias table** | "a curated alias table for the hard cases" — TASK_GRAPH.yaml:1429-1430 | YAML: ISIN -> name aliases | `dataplatform/identity/aliases.yaml` (3.2 KB) | M6.2 | **DONE** | Resolver derives *every* symbol an ISIN ever traded under, not the one valid on the item's date — `ops/BACKLOG.md:113` |
| **Fund / ETF scheme master** | **Named as absent**: "A **fund/ETF scheme master** for the 593 `INF` ISINs that trade on NSE... LIQUIDBEES is the default cash-parking instrument and D2 cannot name it" — `ops/gates/data-catalogue-2026-09-06.md:132` | — | — | *none* | **MENTIONED-ONLY** | 675 `INF%` ISINs now in `security_master` but no scheme attributes. Source unprobed — `ops/BACKLOG.md:165`. Blocks A7: `DEFAULT_PARKING_ISIN`/`LIQUIDBEES` is a module constant, not resolved through D2 — `ops/BACKLOG.md:102` |

### 1.2 Market data (D1/D4)

| Entity | Plan definition | Attributes as specified | Owner / path | Task(s) | Status | Evidence |
|---|---|---|---|---|---|---|
| **OHLCV bar (raw, L1)** | `prices_raw(isin, exchange, date, o,h,l,c, vwap, volume, delivery_qty, series)` — EXECUTION_PLAN.md:148. L1 rule: "Raw traded prices only — **no adjusted prices stored here**" — :145 | `isin`,`exchange`,`symbol`,`series`,`trade_date`,`open`,`high`,`low`,`close`,`last`,`prev_close`,`total_traded_qty`,`total_traded_value`,`total_trades`,`deliv_qty` (nullable),`deliv_pct` (nullable) — all money `decimal128` | `dataplatform/store/schemas.py:53,166-188`; writer `dataplatform/store/l1.py` | M1.4, M1.5, M1.6, M1.8, M1.13, M3.1 | **DONE** | 2,475 partitions, 2016-09-02 -> 2026-09-01. Both exchanges in one partition since the 2026-09-06 fix (`HUMAN_DECISIONS.md:525-529`). **Plan's `vwap` column is not in the schema.** Delivery joined on ~24% of rows sampled (2026-09-01: 2,140 of 8,661) |
| **Price quarantine row** | "unresolvable rows are quarantined and counted, never dropped silently" — TASK_GRAPH.yaml:419 | `symbol`,`series`,`trade_date`,`exchange`,`isin` (nullable),`deliv_qty`,`deliv_pct`,`reason` | `schemas.py:58,190-201` | M1.8 | **DONE** | 2,461 partitions |
| **OHLCV bar (adjusted, L2)** | "Adjusted series (via factors)... Fully recomputable; rebuilt retroactively when a new CA lands" — EXECUTION_PLAN.md:146; "derived on read/materialization, never primary" — :152 | `isin`,`exchange`,`trade_date`,`adj_open/high/low/close`,`adj_volume`,`tr_close` (total-return),`cum_price_factor`,`cum_qty_factor` | `dataplatform/store/l2.py:112,130-140` | M2.5, M9.2 | **DONE** | 3,349 ISINs, 4,276,035 rows, 2016-09-02 -> 2026-09-01. **BSE rows never reach L2** — the stitch filters `series='EQ'` and BSE series is a group letter (`ops/BACKLOG.md:89`) |
| **Trading calendar / expected session** | "expected trading sessions = weekdays minus exchange holidays"; makes "gap report explains 100% of missing days" checkable — TASK_GRAPH.yaml:231-236 | Holiday dates + `special_session: MUHURAT` (closed to normal trading but *publishes a bhavcopy*) — `nse_holidays.yaml:8-14`; coverage is a hard boundary, refuses uncovered ranges (:16-18) | `dataplatform/ingest/calendar.py`, `dataplatform/ingest/data/nse_holidays.yaml` | C.2 | **DONE** | Coverage ends 2026-12-31; the daily job hard-fails in Jan 2027 unless appended — `ops/BACKLOG.md:27` |
| **F&O contract row (L1)** | "Keep the raw contract-level rows in L1" — TASK_GRAPH.yaml:825 | `trade_date`,`underlying`,`instrument_type`,`expiry`,`strike`,`option_type`,`close`,`settle`,`underlying_price`,`open_interest`,`change_in_oi`,`total_traded_qty`,`total_traded_value`,`total_trades`,`isin` | `dataplatform/store/fo_aggregates.py:67,348-362` | M3.7 | **CODE-ONLY** | No `fo_contracts` in `data/L1`. "No runner; `fo_aggregates` store exists empty" — `data-catalogue:111` |
| **F&O aggregate (L2)** | "per-underlying EOD aggregates: total OI and change, put/call ratio, futures basis vs spot, rollover proxies. Sentiment context only — no derivatives trading" — TASK_GRAPH.yaml:823-824 | `trade_date`,`underlying`,`underlying_kind`,`isin`,`spot`,`total_oi`,`total_oi_change`,`call_oi`,`put_oi`,`pcr_oi`,`near_expiry`,`near_future_price`,`basis`,`basis_pct`,`near_month_oi`,`next_month_oi`,`rollover_pct` | `fo_aggregates.py:70,463-479` | M3.7 | **CODE-ONLY** | Same |

### 1.3 Corporate actions & adjustment (D3)

| Entity | Plan definition | Attributes as specified | Owner / path | Task(s) | Status | Evidence |
|---|---|---|---|---|---|---|
| **Corporate action** | `corporate_actions(isin, ex_date, type, ratio_terms, source, reconciled)` — EXECUTION_PLAN.md:148. Types (M2.1 taxonomy): SPLIT, BONUS, DIVIDEND, RIGHTS, MERGER, DEMERGER, SCHEME_OF_ARRANGEMENT, DVR_CONVERSION, NAME_CHANGE, FACE_VALUE_CHANGE, BUYBACK, DELISTING — TASK_GRAPH.yaml:547-548 | `id`,`isin`->FK,`ex_date`,`action_type` (12-value CHECK after `0005:30-33`),`ratio_terms` jsonb ("`{new:1, old:1}` for a 1:1 bonus... an entity/share map for a demerger", `0001:163-166`),`dividend_amount_inr`,`record_date`,`announcement_date`,**`knowable_date`**,`source`,`source_ref`,`reconciled`,`reconciliation_note`,`recorded_at`, + `raw_text`,`l0_key` (`0005:37-48`), + `filed_against_isin` (`0010:26-35`) | `0001_init.sql:140-173`; `dataplatform/corpactions/taxonomy.py`, `parse_terms.py`, `reconcile.py`; `dataplatform/ingest/{nse,bse}/corp_actions.py` | M2.1, M2.2, M2.3, M9.1 | **DONE** | 47,887 rows (BSE 35,382 / NSE 12,505); 47,322 reconciled, 565 not. By type: DIVIDEND 42,378 / BONUS 1,676 / SPLIT 1,544 / RIGHTS 1,051 / BUYBACK 671 / DEMERGER 260 / SCHEME_OF_ARRANGEMENT 253 / DELISTING 38 / MERGER 16 |
| **Adjustment factor** | `adjustment_factors(isin, ex_date, cum_price_factor, cum_qty_factor)` — EXECUTION_PLAN.md:148; "Factors computed per ISIN at each ex-date" — :152 | `isin`,`ex_date` (PK),`price_factor`,`qty_factor`,`cum_price_factor`,`cum_qty_factor`,`corporate_action_id`->FK,**`structural_break`** ("True for mergers and demergers, where the ex-date price gap is a structural event and not a return", `0001:191-194`),`computed_at` | `dataplatform/corpactions/factors.py`, `recompute.py`; `0001_init.sql:175-194` | M2.4, M9.1 | **DONE** | 2,784 rows — but **all 2,784 have `corporate_action_id` NULL** (by design: "the chain grain is per ex-date... two actions on one ex-date", `ops/BACKLOG.md:83`). **Rights issues are not in the factor chain** (TERP needs the cum-rights close) — `ops/BACKLOG.md:82` |
| **L2 invalidation queue** | "A new corporate action triggers retroactive recompute... + invalidation of L2" — EXECUTION_PLAN.md:153 | `id`,`isin`->FK,`reason`,`from_date` (NULL = rebuild whole history),`requested_at`,`resolved`,`resolved_at` | `0006_l2_invalidation.sql:22-44` | M2.4, M2.5 | **DONE** | 10,332 rows |
| **Golden CA case** | 7 named cases + "~13 more ugly cases collected during backfill" — EXECUTION_PLAN.md:155; two independent references (B2: yfinance + hand-computed) — AGENTIC_CONTEXT.md:44 | Published CA terms + literal hand-computed expected adjusted closes | `tests/golden/cases/`, `tests/golden/test_golden_ca.py`, `tests/golden/fixtures/yfinance/` | M2.6, M2.7 | **DONE (7 of ~20)** | The ~13 extra cases remain open — `ops/BACKLOG.md:13`. Reference A unavailable for `ltim_merger_2022` — `ops/BACKLOG.md:92` |

### 1.4 Reference / universe

| Entity | Plan definition | Attributes as specified | Owner / path | Task(s) | Status | Evidence |
|---|---|---|---|---|---|---|
| **Index membership snapshot** | "Snapshot per month -> historical constituents accumulate from day one" — EXECUTION_PLAN.md:127; "what kills survivorship bias in M4's PIT universe" — TASK_GRAPH.yaml:859 | `index_slug`,`index_name`,`as_of`,`isin`,`symbol`,`series`,`company_name`,`industry`,`source`,`l0_key` | `dataplatform/ingest/indices.py:126,381-390`, `constituents_ingest.py` | M3.9, M10.1, M10.2 | **CODE-ONLY** | 34 L0 payloads, `sync_state` 16 PUBLISHED — but **no `index_constituents` dataset in `data/L1`**. M9.3's as-of membership screen "is inert on the real store because `index_constituents` is empty" — `ops/BACKLOG.md:126`. Only ever one snapshot; the weekly accumulator "has never fired" — `data-catalogue:134` |
| **Sector / industry classification** | The `Industry` column of the constituent CSVs; "every liquid name resolves to an Industry as-of a date via `membership_asof`" — TASK_GRAPH.yaml:1885 | `industry` on the membership row (above). Consumed as `Lot.sector` — required, non-blank (`analyst/rails/policies.py:111-122`) | Same as above | M10.1 | **CODE-ONLY** | Same. **The max-sector rail (`RailId`, `analyst/rails/policies.py:69`) has no live sector source.** |
| **Benchmark TRI series** | "niftyindices historical TRI download"; fallback "Computed price-index proxy + dividend estimate" — EXECUTION_PLAN.md:128 | `index_slug`,`index_name`,`as_of`,`tri_value`,`price_close`,**`method`**,`source`,`l0_key` | `dataplatform/ingest/indices.py:127,948-955` | M3.9, M9.4 | **CODE-ONLY (proxy substituted)** | Source register `nifty_tri_history` = **FAILED** ("POST endpoint refuses without a browser session"). The live backtest benchmark is a proxy TRI computed from L1 — `ops/BACKLOG.md:115`, M9.4 acceptance explicitly requires the report to say so (TASK_GRAPH.yaml:1830) |
| **Source register row** | "one registry of source, fallback, license status" — EXECUTION_PLAN.md:91; "machine-readable register that mirrors §4.1" — AGENTIC_CONTEXT.md:293 | `id`,`url_template`,`cadence`,`era` ranges,`fallback`,robots notes,`pit_notes`,`status`,`verified_at`,`last_http_status`,`sample_bytes`,`content_type`,`parser` id,required headers — TASK_GRAPH.yaml:207-209 | `dataplatform/ingest/source_register.yaml` (81 KB), `source_register.py` | C.1 | **DONE** | 30 rows: 25 VERIFIED, 3 FAILED (`nifty_tri_history`, `gdelt_doc_api`, `alfred_series_vintage`), 1 BLOCKED_CREDENTIAL (`screener_company_fundamentals`), 1 VERIFIED-unfetched (`bse_bhavcopy_legacy`). The status enum cannot express "declined on policy grounds" — **open decision D12** |

### 1.5 Fundamentals

| Entity | Plan definition | Attributes as specified | Owner / path | Task(s) | Status | Evidence |
|---|---|---|---|---|---|---|
| **PIT fundamentals fact** | "True PIT — filing timestamp is first-knowable date" — EXECUTION_PLAN.md:136; "every datum tagged (period_end, filing_date)... a later filing restating an earlier period is stored as a new record, not an overwrite" — TASK_GRAPH.yaml:1619-1628 | `isin`,`period_start`,`period_end`,**`filing_date`** (partition key),`nature` (Standalone/Consolidated),`taxonomy`,`filing_id`,`concept`,`segment`,`value`,`derived`,`source`,`l0_key` | `dataplatform/store/pit_fundamentals.py:70,89-110`; `dataplatform/ingest/xbrl/`, `fundamentals_backfill.py` | M7.3, M10.4 | **DONE code; M10.4 = PENDING in build state** | 1,349,562 facts / 2,291 ISINs / 102,650 filings / 2,015 partitions / filing dates 2018-05-21 -> 2026-09-05. **22 concepts** measured: `segment_revenue, other_income, total_income, face_value_per_share, paid_up_equity_capital, total_expenses, eps_diluted, revenue_from_operations, eps_basic, profit_before_tax, profit_after_tax, shares_outstanding, profit_attributable_to_owners, debt_equity_ratio, reserves_excl_revaluation, shareholders_equity_excl_revaluation, gross_npa, net_npa, net_npa_pct, return_on_assets, gross_npa_pct, cet1_ratio`. `sync_state`: 102,650 PUBLISHED / **2,956 FAILED** |
| **Restated fundamentals** | "restated -> quarantined from backtests... monitoring use only" — EXECUTION_PLAN.md:135; "Lands in a physically separate RESTATED store — a different Parquet root and a different query surface" — TASK_GRAPH.yaml:1584-1585 | `isin`,`symbol`,`statement`,`metric`,`period`,`value`,`source`,`l0_source`,`l0_filename`,`l0_logical_date`,`l0_sha256`,`fetched_at`. **Deliberately carries no `knowable_date`** — `dataplatform/query/quarantine.py:115` | `dataplatform/store/restated.py:70,228-240`; `dataplatform/ingest/screener.py`; `dataplatform/query/quarantine.py` | M7.1, M7.2 | **CODE-ONLY** | No `screener_fundamentals` in `data/L1`. Register row `BLOCKED_CREDENTIAL` — export needs `/user/*`, which robots disallows (**D12, open**, `HUMAN_DECISIONS.md:466-499`; "the decision should land before M7.1 is built", :498) |
| **Derived fundamental metric** | "YoY revenue and PAT growth, trailing-twelve-month EPS, P/E (trailing EPS against the L1 price), and operating/net margin trend" — TASK_GRAPH.yaml:1965-1967 | Computed off `read_pit`; carries `as_of` + `knowable_date` ("the latest filing date that entered any field", `fundamentals_metrics.py:154,163`) | `dataplatform/query/fundamentals_metrics.py` | M10.5 | **DONE** | No cash-flow statement anywhere -> FCF and accruals impossible; quarterly equity unavailable -> ROE annual-only — `ops/gates/M10-data-gap-plan.md:40-43` |
| **Shareholding / promoter pledge** | "Filing date != quarter end — store both" — EXECUTION_PLAN.md:131; "Promoter pledge >50% is a BC3-class integrity break condition... so pledge must be a first-class field" — TASK_GRAPH.yaml:807-808 | `isin`,`name`,`period_end`,**`filing_date`** (partition key),`promoter_holding_pct`,`promoter_pledge_pct`,`public_pct`,`fii_pct`,`dii_pct`,`source`,`l0_key` | `dataplatform/ingest/shareholding.py:91,462-472` | M3.6 | **CODE-ONLY** | No `shareholding` in `data/L1`; "No runner" — `data-catalogue:110`. Fixture is hand-built, not a captured live payload — `ops/BACKLOG.md:60` |

### 1.6 Flows, events, macro

| Entity | Plan definition | Attributes | Owner / path | Task(s) | Status | Evidence |
|---|---|---|---|---|---|---|
| **FII/DII daily flow** | EXECUTION_PLAN.md:129 | `trade_date`,`category`,`raw_category`,`buy_value_inr_crore`,`sell_value_inr_crore`,`net_value_inr_crore`,`source`,`l0_key` | `dataplatform/ingest/nse/fii_dii.py:95,486-493` | M3.4 | **CODE-ONLY** | "No runner, **and a missed day is a permanent hole** — the feed has no date parameter and no archive" — `data-catalogue:112`; history unobtainable from NSE (measured) — `ops/BACKLOG.md:51` |
| **Bulk / block deal** | EXECUTION_PLAN.md:130; T0 flow-anomaly input, "keep the raw string alongside any normalized form" — TASK_GRAPH.yaml:792 | `isin`,`deal_type`,`trade_date`,`symbol`,`security_name`,`client_name`,`client_name_normalized`,`side`,`quantity`,`price`,`remarks`,`source`,`l0_key` | `dataplatform/ingest/nse/deals.py:103,651-663` | M3.5 | **CODE-ONLY** | No `deals` in `data/L1` — `data-catalogue:108` |
| **Corporate announcement** | "Timestamped at source — natural PIT" — EXECUTION_PLAN.md:133; normalize to `(isin, ts, category, subject, body/attachment ref)` + keyword index — TASK_GRAPH.yaml:841-843 | `ts` (tz-aware, source timestamp preserved),`source`,`isin`,`symbol`,`category`,`subject`,`body`,`attachment_ref`,`source_ref`,`logical_date`,`l0_key` | `dataplatform/ingest/announcements.py:92,630-640`; `dataplatform/query/announcement_search.py` | M3.8 | **CODE-ONLY** | "No poll runner; BSE page 1 only" — `data-catalogue:109`, `ops/BACKLOG.md:70` |
| **News / geopolitical item** | "GDELT 2.0 + curated RSS... RSS headlines+links only" — EXECUTION_PLAN.md:134 | `ts`,`source`,`title`,`url`,`entities[]`,`tone`,`logical_date`,`l0_key`. **No article bodies for RSS** — TASK_GRAPH.yaml:1414 | `dataplatform/ingest/news.py:62,186-195`, `gdelt.py`, `rss.py`, `data/rss_feeds.yaml` | M6.1 | **CODE-ONLY** | "No runner; the A5 monitor's news path has never seen a live headline" — `data-catalogue:114`. `gdelt_doc_api` FAILED (429); Business Standard dropped (**D7 answered**) |
| **Macro / index-valuation series** | **Proposed §4.1 row 17, awaiting ratification** — EXECUTION_PLAN.md:410. "partitioned by `release_date` (the knowable date, not the period the figure describes), a revision is a new record and never an overwrite" — TASK_GRAPH.yaml:2050-2052 | `series_id`,`period_start`,`period_end`,**`release_date`** (partition key),`frequency`,`unit`,`value`,**`revision_seq`**,`source`,`l0_key` | `dataplatform/store/macro_series.py:59,68-77`; `dataplatform/ingest/macro/{models,index_valuation,index_aliases.yaml}` | M11.1 (DONE), **M11.2 (NEEDS_GO, no state entry)** | **CODE-ONLY** | No `macro_series` in `data/L1`. M11.2 is the ~2,470-request B1 campaign awaiting the owner's go (TASK_GRAPH.yaml:2096-2104). Note the index-rename hazard: "NSE renamed 48 of 53 indices between 2015-11-06 and 2015-11-10" (:2056) |

### 1.7 Operational / quality (D5, D6, D7)

| Entity | Plan definition | Attributes | Owner / path | Task(s) | Status | Evidence |
|---|---|---|---|---|---|---|
| **Sync state** | `sync_state(source, date, state, attempts, last_error, checksum)` — EXECUTION_PLAN.md:148; machine `PENDING->FETCHED->VALIDATED->NORMALIZED->PUBLISHED`, + `FAILED(retryable,attempts)` and `GAP(expected)` — :159 | `source`,`logical_date`,**`unit`** (added `0008:35`, PK widened to `(source,logical_date,unit)` at `0008:65`),`state`,`attempts`,`retryable`,`last_error`,`checksum`,`l0_path`,`first_attempt_at`,`updated_at` | `dataplatform/status/sync_state.py`; `0001_init.sql:198-229`, `0008_sync_state_unit.sql` | M1.3 | **DONE** | 117,469 rows across 8 sources (see §2 for breakdown) |
| **L0 payload ref** | "Exact fetched files + checksums + fetch metadata... Immutable" — EXECUTION_PLAN.md:144 | `L0Ref`: sha256, size, fetched_at, content_type + a sidecar `.meta.json`; path `data/L0/{source}/{yyyy}/{mm}/{filename}` — TASK_GRAPH.yaml:70-71,273-275 | `dataplatform/store/l0.py`, `l0_verify.py`, `paths.py` | M1.1 | **DONE** | 13 L0 source trees, 202,410 payloads (largest: `nse_xbrl_filing` 163,504) |
| **Quality flag** | "Gap detection, cross-exchange price sanity, CA-correctness checks, anomaly flags" — EXECUTION_PLAN.md:97 | `id`,`logical_date`,`check_name`,`severity` in {INFO,WARN,ERROR},`isin` (**deliberately not an FK, may be NULL**, `0001:251-253`),`source`,`detail`,`observed_value`,`threshold`,`resolved/resolved_at/resolution`,`raised_at` | `dataplatform/quality/sentinel.py`, `rules/`, `gaps.py`; `0001_init.sql:233-255` | M1.11, M2.8, M3.3 | **DONE** | 21,154 rows — **all of one kind, `ca_reconciliation`**. Cross-exchange rule ships without an `exchange_closes` builder — `ops/BACKLOG.md:91` |
| **Archive bundle** | "Daily normalized Parquet/CSV archive bundles... manifest with checksums" — EXECUTION_PLAN.md:96,174 | `logical_date` (PK),`schema_version`,`bundle_path` (relative, never absolute — `0002:63-66`),`manifest_sha256`,`file_count`,`total_bytes`,`manifest` jsonb (per-file sha256, byte size, row count, **L0 lineage**),`published_at` | `dataplatform/archives/publisher.py`, `manifest.py`; `0002_status_surface.sql:48-74` | M1.12 | **DONE code, 1 bundle** | 1 row. Publishes `prices_raw` only — `ops/BACKLOG.md:77`. No wired daily publish step — `ops/BACKLOG.md:79`. Public redistribution is a HUMAN_GATE legal question (EXECUTION_PLAN.md:392) |
| **Scheduler heartbeat** | `/health` liveness — EXECUTION_PLAN.md:162 | `scheduler_id` (PK — "not its PID"),`beat_at` (from the **injected Clock**, `0002:37-40`),`detail`,`updated_at` | `0002_status_surface.sql:23-44` | M0.6 | **DONE code, 0 rows** | 0 rows — **the scheduler has never run**. "Scheduler runs: 0 / 0" — `data-catalogue:151` |
| **Job run** | Job history + observable lock skips | `run_id`,`job_name`,`state` in {RUNNING,SUCCEEDED,FAILED,TIMED_OUT,**SKIPPED_LOCKED**},`instance` (host:pid),`started_at`,`finished_at`,`error` | `0003_scheduler.sql:25-55`; `dataplatform/scheduler/{runner,registry}.py` | M0.6 | **DONE code, 0 rows** | 0 rows. 4 jobs registered in code (`eod_pipeline`, `constituents_snapshot`, `l0_verify`, `identity_refresh` — `registry.py:203,236,271,307`); **no analyst/paper-trading job is registered** (`HUMAN_DECISIONS.md:513-514`, `ops/BACKLOG.md:140`) |

### 1.8 Analyst domain (A1–A9)

| Entity | Plan definition | Attributes as specified | Owner / path | Task(s) | Status | Evidence |
|---|---|---|---|---|---|---|
| **Case** | Lifecycle `DRAFT -> INTERVIEW -> PROPOSAL -> RATIFIED -> FUNDED(paper|real) -> ACTIVE <-> SUSPENDED -> CLOSED` — EXECUTION_PLAN.md:183 | `case_id` (PK),`title`,`state` (8-value CHECK),`funding_mode` in {PAPER,REAL} ("it selects which Broker is injected and nothing else", `0001:280-282`),`theme`,`horizon_years`,`benchmark_primary`,`benchmark_secondary`,`sip_amount_inr`,`sip_day_of_month` (1-28, "so the instalment date exists in February"),`config` jsonb,`created_at`,`updated_at` | `analyst/cases/{service,lifecycle}.py`; `0001_init.sql:259-285` | M5.3, M5.13 | **DONE code, 0 rows** | **0 rows.** Built + tested; never operated |
| **Ratified policy set** | §5.2's seven policies: capital plan, horizon & benchmarks, rotation dial, risk rails, exit menu, cash policy, monitoring cadence — EXECUTION_PLAN.md:192-200 | `id`,`case_id`,`version`,`supersedes_version`,`policy` jsonb, + **rails promoted to scalars** `rotation_dial_pct`,`max_position_pct`,`max_sector_pct`,`min_holdings`,`drawdown_review_pct`,`ratified_by`,`ratified_at`,**`ratification_kind` in {HUMAN,FIXTURE}**,`recorded_at`. **APPEND-ONLY** (triggers `0001:316-325`) | `analyst/cases/policies.py`; `0001_init.sql:287-325` | M5.3, M5.8 | **DONE code, 0 rows** | 0 rows. `FIXTURE` = B9 paper/test only; real money requires a `HUMAN` row (`0001:310-313`) |
| **Thesis** | §5.3 object — EXECUTION_PLAN.md:204-221 | `id`,`case_id`,`isin`,`version`,`sleeve` in {CORE,TACTICAL},`driver`,`theme_purity` (0-1),`expected_evidence` jsonb,`break_conditions` jsonb,`status` in {DRAFT,RATIFIED,SUPERSEDED,BROKEN},`ratified_by`,`ratified_at`,`recorded_at` | `analyst/thesis/{engine,models,ratify}.py`; `0001_init.sql:329-355` | M5.6 | **DONE code, 0 rows** | 0 rows |
| **Break condition** | Typed `fundamental` / `structural` / `integrity`, each with an evaluation tier T0/T1; "A break condition that cannot be mechanically or evidentially evaluated must be rejected at draft time" — TASK_GRAPH.yaml:1213-1214 | `{id, type, condition, evaluation}` — EXECUTION_PLAN.md:211-215 | Nested in `thesis.break_conditions`; `analyst/thesis/models.py`, `analyst/monitor/matcher.py`, `analyst/monitor/fundamentals.py` | M5.6, M6.2, M7.4 | **DONE code, 0 rows** | Segment-revenue-decline evaluator built (`analyst/monitor/fundamentals.py`) but re-parses the whole 1.3M-fact store per call (~17 s) — `ops/BACKLOG.md:156` |
| **Decision journal record** | §5.7 append-only entry, "every decision, including no-ops" — EXECUTION_PLAN.md:246-258. **"The decision journal is the product"** — :14 | `id`,`ts`,`trading_date`,`case_id`,`actor` in {T0,T1,T2,RAILS,EXEC,USER,**SYSTEM**},`decision` in {HOLD,BUY,SELL,ESCALATE,**HEARTBEAT**,SKIPPED_DATA_RED,**AUTH_REQUIRED**,**DEFERRED**,RAIL_BLOCK,POLICY_PROPOSAL} (`0007:22-25`),`isin` (**not an FK**, `0001:390-394`),`sleeve` in {CORE,TACTICAL,CASH},`evidence_snapshot_ref`,`break_conditions_evaluated` jsonb,`rationale`,`model`,`tokens_in`,`tokens_out`,`cost_inr`,`orders_ref`,`payload`,`recorded_at`. **APPEND-ONLY** (triggers `0001:402-408`) | `analyst/journal/{writer,models}.py`; `0001_init.sql:359-408`, `0007_auth_required_decision.sql` | M5.1, M5.15 | **DONE code, 0 rows** | **0 rows.** No lifecycle decision verb exists -> transitions filed as `POLICY_PROPOSAL` with `payload["event"]` — `ops/BACKLOG.md:54` |
| **Evidence snapshot** | "content-addressed bundle (prices, filings, news items actually shown to the model)" — EXECUTION_PLAN.md:253; "the bundle that was shown, not the bundle that could have been assembled later" — `0001:395-398` | Content hash -> write-once bundle under `data/evidence/`; token budget bounded and reported — TASK_GRAPH.yaml:1450-1451 | `analyst/journal/evidence.py`, `analyst/monitor/bundle.py` | M5.1, M6.3 | **DONE code, no bundles** | `data/evidence/` does not exist. Two gaps on record: it is **outside `ops/backup.sh`** (`ops/BACKLOG.md:43`) and has **no integrity sweep** unlike `L0Store.verify_checksums` (:44) |
| **Verdict (T1/T2)** | `INTACT / WEAKENED / BROKEN` per break condition + a proposed action "WITHIN ratified policies (validated in code, not trusted from the model)" — TASK_GRAPH.yaml:1467-1468 | Schema-validated structured output | `analyst/monitor/{t1,t2,verdicts}.py` | M6.4, M6.5 | **DONE code, 0 rows** | Live-model quality (**M6.8, NEEDS_SECRET, no state entry**) is outstanding pending an Anthropic key (B4) |
| **Evidence pack** | Monthly + on-graduation: returns vs both benchmarks (XIRR since SIP), drawdown profile, rail-breach count, decision review, turnover + tax-event summary by sleeve, token/cost burn, data-quality skips — EXECUTION_PLAN.md:260 | Built **entirely from journal rows** — `0001:388-389`; "every number in the pack traces to journal entries (no separate accounting)" — TASK_GRAPH.yaml:1513 | `analyst/journal/evidence_pack.py` (1,022 lines) | M6.6 | **DONE code, 0 rows** | Nothing to generate from — journal is empty |
| **Theme -> value chain -> proxy (with purity)** | "listed NSE/BSE proxies with disclosed purity scores" — EXECUTION_PLAN.md:100; "a number with no evidence trail is not acceptable output" — TASK_GRAPH.yaml:1192 | Candidate ISIN + purity score + evidence refs; candidates drawn from the **PIT universe**, never a hardcoded list (:1198) | `analyst/mapper/{engine,purity}.py` | M5.5 | **DONE code, 0 rows** | Runs on StubLLM (B4) |
| **Sleeve target / allocation** | Dial `d%` -> tactical = d% of case value, core = 100-d% — EXECUTION_PLAN.md:235 | `SleeveTargets`, `SleeveAllocation`; CORE membership changes on `BROKEN` only; every order tagged CORE/TACTICAL; resizing the boundary is a **policy change** — TASK_GRAPH.yaml:1271 | `analyst/rotation/{engine,sleeves}.py` | M5.9 | **DONE code, 0 rows** | Rotation SELL journal entries omit gross/costs — `ops/BACKLOG.md:11` |
| **Risk rails** | "Max position %, max sector %, min holdings, drawdown-review trigger, per-order sanity caps" + cross-case concentration — EXECUTION_PLAN.md:197, :105 | `RailId` enum; `Lot(isin, sector, quantity, price)`; `Portfolio(case_id, lots, cash)`; `ProposedOrder(+price, sector)`; `HouseholdExposure(isin, household_value_in_isin, household_total_value)`; `RailBreach(rail, limit, observed, detail)`; `RailAssessment`; `DrawdownStatus(peak, trough, drawdown_pct, limit_pct)` | `analyst/rails/{engine,policies}.py`; property tests `tests/property/test_rails_property.py` | M5.2 | **DONE code, 0 rows** | **The cross-case rail reuses `max_position_pct` as the household ceiling — §5.2 defines no separate ratified number** (`ops/BACKLOG.md:98`) |
| **Cash / deployment queue** | "Idle cash -> liquid ETF parking; deployment queue for SIP + exit proceeds" — EXECUTION_PLAN.md:104,242 | `CashSource` enum, `QueuedCash`, `DeploymentQueue`, `ParkingDecision`, `DeploymentDecision`. Whole-share constraint applies to the ETF too — TASK_GRAPH.yaml:1290 | `analyst/cash/{manager,queue}.py` | M5.10 | **DONE code, 0 rows** | `deploy_within_sessions` is read but **not enforced** — `ops/BACKLOG.md:107`. Parking ISIN is a constant, not a D2 lookup — :102 |
| **SIP instalment** | "SIP amount, day-of-month, top-up rules" — EXECUTION_PLAN.md:194 | `SipInstalment(case_id, due_date, amount_inr)` — `analyst/cases/service.py:215-226` | `analyst/cases/service.py`, `backtest/sip.py` | M5.3, M4.7 | **DONE code, 0 rows** | Drift denominator mismatch on record — `ops/BACKLOG.md:100` |

### 1.9 Execution & accounting (X1, X2, X3)

| Entity | Plan definition | Attributes as specified | Owner / path | Task(s) | Status | Evidence |
|---|---|---|---|---|---|---|
| **Order (staged / executed)** | "decisions produce staged orders EOD -> executed next session" — EXECUTION_PLAN.md:268 | `id`,**`order_uid`** (idempotency key, UNIQUE — "a retried placement after an ambiguous broker response cannot become two real orders", `0001:452-454`),`case_id`,`isin`,`exchange`,`sleeve`,`side`,`order_type` in {MARKET,LIMIT},`quantity` (int > 0),`limit_price_inr`,`state` in {STAGED,**RAIL_BLOCKED**,SENT,PARTIAL,EXECUTED,CANCELLED,REJECTED},`broker` in {SIM,KITE},`broker_order_id`,`staged_at`,`staged_for_date`,`executed_at`,`filled_quantity`,`avg_fill_price_inr`,`gross_value_inr`,`costs_inr`,`net_value_inr`,`cost_breakdown` jsonb,`decision_journal_id`->FK,`rejection_reason`,`updated_at` | `execution/staging.py`, `execution/broker.py:213-229`; `0001_init.sql:412-462` | M4.5, M5.12, M8.1 | **DONE code, 0 rows** | **0 rows.** A broker-*rejected* staged order is not transitioned — `ops/BACKLOG.md:103` |
| **Fill** | SimBroker fill model: "next-day execution... slippage in bps scaled by liquidity, full Indian cost model" — EXECUTION_PLAN.md:267 | `isin`,`session`,`side`,`quantity`,`exchange`,**`reference_price`**,**`slippage_bps`**,**`fill_price`**,`cost: CostBreakdown` — "the reference is a market fact, the slippage is the model's assumption, and the two together explain the fill price exactly" (`execution/broker.py:176-195`) | `execution/broker.py:176`, `execution/sim_broker.py` | M4.5 | **DONE** | Whole-order fills only, no partials/book depth — `ops/BACKLOG.md:94`. Paisa-quantised; NSE's Rs 0.05 tick above Rs 250 unmodelled — :149 |
| **Position (unsettled)** | T+1: "a buy filled today is a *position* today and a *holding* the next session" — `execution/broker.py:233-238` | `isin`,`exchange`,`quantity`,`average_price` (ex-costs),`session` | `execution/broker.py:232-244` | M4.5 | **DONE** | Protocol-level; not persisted in Postgres |
| **Holding (settled)** | "A settled delivery holding. Keyed by ISIN" | `isin`,`exchange`,`quantity`,`average_price` (cost basis, ex-charges) | `execution/broker.py:248-254` | M4.5 | **DONE** | Not persisted |
| **Cash ledger entry** | "reconciliation job compares broker positions/ledger vs internal book every day" — EXECUTION_PLAN.md:268 | `seq`,`session`,`isin`,`description`,`debit`,`credit`,`balance` — "append-only and never rewritten in place (invariant #12)" (`broker.py:258-272`) | `execution/broker.py:258-272`, `backtest/accounting.py` | M4.5, M4.6 | **DONE** | **In-memory only** — no `ledger` table in any migration. `KiteBroker.ledger()` reads a placeholder path; Kite has no order-independent cash ledger — `ops/BACKLOG.md:119` |
| **Margins / funds** | Broker interface `margins` — EXECUTION_PLAN.md:266 | `available`,`utilised`,`total` (derived) | `execution/broker.py:276-288` | M4.5 | **DONE** | — |
| **Portfolio / book state** | "portfolio accounting (XIRR)" — EXECUTION_PLAN.md:355 | `PortfolioBook`: `cash`, `positions: BookPosition(isin, quantity, cost_basis)`, `realized_pnl`, `_ledger: list[LedgerEntry]`, `_external: list[Cashflow]`. CA handling: splits change quantity, demergers create positions, no CA may change total book value | `backtest/accounting.py:96-108,148-176`, `backtest/xirr.py` | M4.6 | **DONE** | **Not a persisted entity.** In-memory, per-run, reconstructed each replay. No `position`/`cash_balance`/`nav` table exists in any migration |
| **Benchmark comparison** | "returns vs both benchmarks (XIRR, since SIP)" — EXECUTION_PLAN.md:260 | `portfolio_xirr`,`benchmark_xirr`,`theme_xirr` — "all three XIRRs computed from the *same* external cashflow schedule" (`backtest/accounting.py:121-132`) | `backtest/accounting.py:121-142` | M4.6, M9.4 | **DONE** | Benchmark is the computed proxy TRI, not the licensed feed |
| **Cost model parameters** | "One cost model module shared by SimBroker and backtest — never two implementations" — EXECUTION_PLAN.md:267; "Rates in a dated config so historical backtests use the rates in force then" — TASK_GRAPH.yaml:952-953 | Dated `schedules[]`, each with `id`,`effective_from`,`label`,**`provenance`** in {verified, reconstructed},`sources_read_on`,`sources[]`,`notes`. Components: brokerage, STT, exchange txn, SEBI turnover fee, stamp duty (state-wise, buy-side), GST, DP charge on sells. **Every rate a quoted string** so YAML cannot make it a float (`rates.yaml:9-11`) | `execution/costs/model.py`, `execution/costs/rates.yaml` | M4.4 | **DONE** | 2017–2024 schedules are `reconstructed` from secondary sources — `ops/BACKLOG.md:30`. Only KA + MH stamp duty; capped states (TG, HR) unmodelled — :31. Card starts 2017-07-01; a pre-GST backtest **raises** — :32. Delivery equity only — :34 |
| **Kill switch state** | "one command/endpoint halts all order placement" — EXECUTION_PLAN.md:269 | State must **survive a restart** — TASK_GRAPH.yaml:1334 | `execution/kill_switch.py` | M5.12 | **DONE code** | — |
| **Broker session** | `AUTH_REQUIRED` interlock; `session_valid()` + `SessionExpired` — EXECUTION_PLAN.md:408, TASK_GRAPH.yaml:1369 | Protocol method; a bad session journals `AUTH_REQUIRED`, places zero orders, and **defers** (not drops) the day's decisions | `execution/session.py`, `analyst/monitor/interlock.py`, `execution/broker.py:83-94` | M5.15 | **DONE code** | Ships as a composable component; **no single daily-loop module wires it** — `ops/BACKLOG.md:111` |
| **Token usage** | "Per-decision LLM token/cost capture" — EXECUTION_PLAN.md:109 | `id`,`ts`,`provider`,`model`,`purpose`,`case_id`,`decision_journal_id`->FK,`tokens_in`,`tokens_out`,`cached_tokens`,`cost_inr`,`cost_usd` ("kept because the FX rate applied is otherwise unrecoverable", `0001:481-485`) | `accounting/tokens.py`, `analyst/llm/{client,stub,anthropic}.py`; `0001_init.sql:466-487` | M5.4 | **DONE code, 0 rows** | Unknown model -> loud error, never silent Rs 0 (TASK_GRAPH.yaml:1174) |
| **Backtest arm / sweep result** | M12.2: "at least twenty distinct strategy arms, each differing from a named reference by one stated change"; ranked on **XIRR / max drawdown** (owner decision 2026-09-07) — TASK_GRAPH.yaml:2166-2172 | Per arm: XIRR, max drawdown, round trips (closed round trips, not journal entries), cost, benchmark excess — on **two liquidity floors** (Rs 1 cr and Rs 10 cr median turnover) | `backtest/sweep.py`, `backtest/verdict.py`, `ops/gates/M12-strategy-sweep-{decade,sixyear}.md`, `M12-strategy-verdict.md` | M12.1, M12.2, M12.3 | **DONE (no state entry)** | 23 arms run; committed `ac099ff`...`c9f0ed5` |

---

## 2. BUILD STATUS SUMMARY

**Task states (`BUILD_STATE.json`, 98 entries):** 96 DONE, 1 PENDING (`M10.4`), 8 absent.

| Task | Autonomy | State | Why not DONE |
|---|---|---|---|
| `M10.4` Fundamentals backfill runner | NEEDS_GO | **PENDING** | B1 bulk campaign; *but the data is largely there* — 1.35M facts, 102,650 filings. The campaign ran past the state record. |
| `M6.8` Live-model fire drill | NEEDS_SECRET | *absent* | B4 — no Anthropic key (`HUMAN_DECISIONS.md:539`) |
| `M8.3` Live-account order sessions | HUMAN_GATE | *absent* | Real orders reserved to the human (AGENTIC_CONTEXT.md:90-91); Kite T&C 2(e) question open (`HUMAN_DECISIONS.md:540`) |
| `M8.4` Graduation packet | HUMAN_GATE | *absent* | Decision #8, discretionary |
| `M11.2` Index-valuation backfill | NEEDS_GO | *absent* | ~2,470-request B1 campaign, un-gone |
| `M10.7`, `M12.1`, `M12.2`, `M12.3` | AUTO | *absent* | **Committed and merged; build state never written** |

**The load-bearing observation:** the split is not between built and unbuilt code. It is between
**built code with data** and **built code with none.**

*Live counts, measured 2026-09-07:*

```
Postgres — has rows                Postgres — ZERO rows
  sync_state          117,469        case_                 0
  corporate_actions    47,887        policy_set            0
  quality_flag         21,154        thesis                0
  l2_invalidation      10,332        decision_journal      0   <- "the journal is the product"
  symbol_history       11,359        order_                0
  exchange_listing     10,870        token_usage           0
  security_master       8,598        job_run               0
  adjustment_factors    2,784        scheduler_heartbeat   0   <- scheduler never ran
  isin_lineage            399
  identity_reconciliation  10
  archive_bundle            1

Lake — has partitions              Lake — declared, EMPTY
  L1/prices_raw           2,475      index_constituents   benchmark_tri
  L1/prices_raw_quarantine 2,461     fii_dii_flows        deals
  L1/pit_fundamentals     2,015      shareholding         announcements
  L2/prices_adjusted      3,349      news                 fo_contracts
                                     fo_aggregates        macro_series
                                     screener_fundamentals
```

`sync_state` by source: `nse_xbrl_filing` 102,650 PUBLISHED / **2,956 FAILED** ·
`bse_corp_actions` 6,683/1 · `nse_bhavcopy` 2,471 · `bse_bhavcopy_legacy` 1,938/1 ·
`bse_bhavcopy` 536 · `nse_integrated_filing_index` 114 · `nse_financial_results_index` 90/1 ·
`nifty_index_constituents` 16/1 · `nse_corp_actions` 11.

**The repo's own name for this:** *"Twenty register rows describe data nothing has ever captured. For
nine of them the code is built and waiting on a runner and a scheduler; for five the source itself is
the blocker; the rest need a register row before anything can be said about them."* —
`ops/gates/data-catalogue-2026-09-06.md:167-169`.

> **Note on that catalogue.** It is dated 2026-09-06 and frames everything as a laptop/server split.
> `CLAUDE.md` records that split as **superseded 2026-09-07** ("everything runs here"). My on-disk
> measurements confirm this box now holds the union — 13 L0 trees including `nse_bhavcopy_*`,
> `nse_mto`, `nse_sec_bhavdata_full`, `nse_corp_actions`, `bse_corp_actions` (13,378 payloads),
> `bse_scrip_master`. The catalogue's §1–§3 machine attribution is stale; its §4 "Captured nowhere"
> is still accurate and is the authoritative statement of coverage.

---

## 3. THE INVARIANTS AS DATA CONSTRAINTS

Each restated as the concrete data requirement it imposes. Source: `AGENTIC_CONTEXT.md` §6, lines 190–229.

| # | Invariant (line) | The data requirement it imposes | Where it lives / is enforced |
|---|---|---|---|
| **1** | L0 is immutable, write-once, checksummed; every L1/L2 value re-derivable from L0 alone (`:190-191`) | Every ingested payload persists as bytes + sha256 + fetch metadata under a `(source, date, filename)` key, **before parsing**. Every derived row carries an `l0_key`/lineage column back to those bytes. A conflicting `put` for an existing key raises rather than overwrites. Deleting or rewriting L0 is a **human-only** decision (`:97`) | `dataplatform/store/l0.py` (`L0ImmutabilityError`, TASK_GRAPH.yaml:275); `l0_key` on `corporate_actions` (`0005:39`), `prices_raw`... every L1 schema; `sync_state.checksum` ("a checksum that no longer matches is a corruption alert, never a reason to rewrite L0", `0001:223-226`) |
| **2** | Nothing joins on a raw symbol, ever. ISIN via D2 is the only join key (`:192`) | `isin` is the key column on every instrument-scoped table. Symbols exist only in `symbol_history` with `(valid_from, valid_to)` windows and in raw-provenance columns marked non-joinable. A symbol->ISIN ambiguity on a date **raises to a queue**, never picks | `security_master.isin` PK; `exchange_listing.security_code` "never used as a join key" (`0001:134-136`); `identity_reconciliation`; `Lot`/`OrderRequest`/`BookPosition` all reject symbols |
| **3** | **No adjusted prices in L1.** Adjusted series derived on read or materialized into L2, always recomputable from raw + factors (`:193`) | **Two physically separate series.** (a) unadjusted OHLCV keyed `(isin, exchange, trade_date)`; (b) a factor series keyed `(isin, ex_date)` with its own effective dates and `cum_*` chain. L2 must be deletable and rebuild byte-identically. A schema-level assertion must reject an adjusted-looking column in L1 | `schemas.py:214 column_looks_adjusted()`, `:224 assert_raw_only()`; `adjustment_factors` PK `(isin, ex_date)`; `l2.py` `prices_adjusted`; M2.5 acceptance "wiping L2 entirely and rebuilding produces identical output" (TASK_GRAPH.yaml:634) |
| **4** | **One cost model.** SimBroker and the backtest import the *same* module (`:195-196`) | Cost rates live in **one** dated rate card; a trade is priced with the last schedule whose `effective_from` <= trade date; a trade before the first schedule **raises** rather than borrowing a later card. `cost_breakdown` stored per-order "as the model emitted it so a reconciliation break can be attributed to a component" | `execution/costs/rates.yaml:1-19`; `order_.cost_breakdown` (`0001:455-458`); M4.4 acceptance includes "a test asserts no second cost implementation exists in the repo (grep-based)" (TASK_GRAPH.yaml:959) |
| **5** | **One decision code path.** Paper and real differ only by injected `Broker`. No `if paper:` anywhere in `analyst/` (`:197-198`) | The book must record **which** implementation acted: `order_.broker in {SIM, KITE}` — "the only difference between paper and real" (`0001:448-451`). `case_.funding_mode` "selects which Broker is injected and nothing else" (`0001:280-282`) | `execution/broker.py:295 Broker(Protocol)`; M4.5 acceptance "a grep test asserts nothing in `analyst/` imports a concrete broker module" |
| **6** | **Rails cannot be bypassed.** Every order from A5/A6/A7 passes A8. Deterministic code, never an LLM, no override (`:199-200`) | A blocked order is a **stored row**, not an absence: `order_.state = 'RAIL_BLOCKED'` is a first-class state — "a blocked order is a journaled event, not an order that never existed" (`0001:450-451`) — plus a `RAIL_BLOCK` journal line naming the breached rail, its limit and the observed value | `analyst/rails/engine.py`; `RailBreach(rail, limit, observed, detail)`; M5.2 acceptance "a grep test asserts no bypass parameter exists" |
| **7** | **No data with `knowable_date > decision_date` reaches a decision.** Enforced in the query layer, asserted by the PIT leak test (`:201-202`) | **Every queryable dataset must declare a `knowable_date` extractor.** A dataset declaring none is *unusable* in PIT mode (raises, does not default to "always knowable"). A per-record `None` is refused the same way. The guard **raises** on a leak rather than silently filtering | `dataplatform/query/pit.py:11-29,71-106` (`Dataset.declaring` / `Dataset.undeclared`, `PitContext.admit`, `PitError`); `corporate_actions.knowable_date` (`0001:167-170`); `tests/integration/test_pit_leak.py` |
| **8** | **Restated fundamentals unreachable from backtests** — physically quarantined (`:203-204`) | Two stores with **different Parquet roots and different query surfaces**. The restated store carries no `knowable_date` at all (`quarantine.py:115`). Every restated datum tagged `source=screener_restated`. Monitoring may read it, and the journal must record **which store each datum came from** | `dataplatform/store/restated.py`; `dataplatform/query/quarantine.py`, `dataplatform/quality/quarantine.py`; M7.2 acceptance "a deliberate attempt to read restated data from a PIT/backtest context raises" |
| **9** | **Every decision journaled, including no-ops.** A day with nothing to do still writes a heartbeat with the evidence considered (`:205-206`) | `HEARTBEAT` is a **decision value in the CHECK constraint**, not a log line — "a first-class entry, not an absence" (TASK_GRAPH.yaml:1121). Every entry carries `evidence_snapshot_ref` to the content-addressed bundle actually shown | `0001_init.sql:367-370`; `analyst/journal/writer.py` |
| **10** | **Bad data never becomes decisions.** Daily loop reads `/status/sync` first; not-green => `SKIPPED_DATA_RED` and no trading (`:207-208`) | A single queryable predicate `is_green(date, datasets)` over `sync_state` + open `quality_flag`s. An unresolved `l2_invalidation` row is likewise "a visible instruction to rebuild, not a log line" (`0006:34-35`). `SKIPPED_DATA_RED` and `AUTH_REQUIRED` are journal decision values | `dataplatform/status/sync_state.py is_green()`; `analyst/monitor/interlock.py`; `0007:22-25` |
| **11** | **The clock is injected.** Replay byte-for-byte reproducible (`:209`) | No timestamp may originate from a wall clock inside a module. Even `scheduler_heartbeat.beat_at` is "taken from its injected Clock and not from the database's `now()`: a replayed or frozen-clock run has to write the time it claims to be running at" (`0002:37-40`). PIT `as_of` is an explicit argument, never `now()` (`pit.py:31`) | `dataplatform/clock.py`; `tests/unit/test_clock_guard.py` fails if a `datetime.now()` is introduced anywhere else |
| **12** | **Append-only means append-only.** Journal and amendment log never updated or deleted in place (`:210`) | DB-level enforcement, not convention: `BEFORE UPDATE OR DELETE ... FOR EACH **STATEMENT**` triggers — statement-level "so that a statement matching zero rows fails too — `DELETE FROM decision_journal WHERE false` must not look like it succeeded" (`0001:69-71`). Applies to `decision_journal` and `policy_set`; both also `REVOKE UPDATE, DELETE ... FROM PUBLIC`. The cash ledger is append-only by the same rule (`broker.py:263`) | `0001_init.sql:58-71,316-325,402-408` |
| **13** | **A secret never enters the repo, a log, or an artifact.** The repo is public, so this is the one breach a later commit cannot undo (`:211-229`) | Credentials live **only** in process env / untracked `.env`. Every credential-bearing setting is a `SecretStr`, **including a Postgres DSN**. No secret in a log, the status API, or the journal — and the journal is append-only, so one written there is **permanent**. Never interpolate into a URL, argv, or exception message. Checked-in fixtures must be credential-free — a recorder must strip auth headers. **`BUILD_STATE.json` is tracked and published**: a task's `reason`/`note` is often a raw error string and must be scrubbed; never let a DSN, token, argv or response body reach build state | `dataplatform/config.py`; the gate's secret scan (`orchestrator/checks.py`, AGENTIC_CONTEXT.md:162-163); `ops/runbooks/secret-leak.md` |

---

## 4. THE POINT-IN-TIME MODEL

### 4.1 How the plan says PIT correctness is achieved

Four mechanisms, stated in the plan and built:

1. **A universal per-dataset declaration.** "Every queryable dataset declares a `knowable_date`
   (source timestamp / filing_date / publication date). A PIT-mode query context carries `as_of`; the
   query layer filters `knowable_date <= as_of` and **raises if a dataset has no `knowable_date`
   declared**" — TASK_GRAPH.yaml:932-934. Implemented as `Dataset[R]` + `PitContext.admit`
   (`dataplatform/query/pit.py`). Crucially it **refuses** rather than defaulting: `Dataset.undeclared`
   exists so a caller can be honest and be blocked (`pit.py:98-106`).
2. **Raise, never filter.** "an attempt to read future data in PIT mode raises rather than returning
   filtered-but-silent results" — TASK_GRAPH.yaml:939; rationale at `pit.py:19-29`: "a silent filter
   would turn *you leaked future data* into *this query happened to return fewer rows*".
3. **A survivorship-free universe.** "point-in-time universe as of a historical date (via
   index-constituent history + listing status)" — EXECUTION_PLAN.md:174; delisted names retained
   forever (`0001:88-92`).
4. **A leak test with a deliberate positive control.** "an automated harness asserting no datum with
   `knowable_date > decision_date` ever reaches a decision — implemented by instrumenting the query
   layer during a replay and auditing every access against the session's `as_of`. Add a deliberate
   leak in a test double and prove the harness catches it" — TASK_GRAPH.yaml:1076-1079.

### 4.2 Which entities carry as-of / knowledge-date / restatement columns

| Entity | PIT column(s) | Restatement handling | Verdict |
|---|---|---|---|
| `corporate_actions` | **`knowable_date`** (explicit, NOT NULL) | — | **Strongest.** "Corporate actions apply retroactively to prices but must NOT apply retroactively to decisions: a backtest as of D may only see rows with `knowable_date <= D`, even though `ex_date` may be earlier" (`0001:167-170`) |
| `pit_fundamentals` | **`filing_date`** (partition key) + `period_start`/`period_end`; also `nature`, `taxonomy`, `filing_id` | **A restatement is a new record keyed by filing, never an overwrite** (TASK_GRAPH.yaml:1628, 1949) | Strong |
| `macro_series` | **`release_date`** (partition key, "the knowable date, not the period the figure describes") + `period_start`/`period_end` | **`revision_seq`** — a revision is a new record; both versions survive (TASK_GRAPH.yaml:2069) | Strong (no data yet) |
| `shareholding` | **`period_end` + `filing_date`**, "neither is inferred from the other" (TASK_GRAPH.yaml:811) | Multiple filings of one `(isin, period_end)` are all returned; picking the latest is left to the caller — `ops/BACKLOG.md:63` | Strong (no data yet) |
| `announcements`, `news` | **`ts`**, source timestamp — "no re-stamping with ingest time" (TASK_GRAPH.yaml:847) | — | Strong (natural PIT; no data yet) |
| `index_constituents`, `benchmark_tri` | **`as_of`** snapshot date; "never overwrite a prior month's membership" (TASK_GRAPH.yaml:861) | Snapshot-per-month *is* the versioning | Adequate by design — **but only one snapshot has ever been taken** |
| `symbol_history` | `valid_from` / `valid_to` (**valid time**) + `recorded_at` | Appended on rename, never overwritten | See silence #2 below |
| `screener_fundamentals` (restated) | **None, deliberately** | — | Correct: quarantine is the mechanism (`quarantine.py:115`) |
| `prices_raw` | `trade_date` only | — | See silence #1 |
| `prices_adjusted` (L2) | `trade_date` only | — | See silence #3 |
| `adjustment_factors` | `ex_date` + `computed_at` — **no `knowable_date`** | Full-chain recompute overwrites `(isin, ex_date)` in place | See silence #3 |
| `fii_dii_flows`, `deals` | `trade_date` only | — | Silence #4 |
| `fo_contracts`, `fo_aggregates` | `trade_date` only | — | Silence #4 |

### 4.3 Where the plan is SILENT on PIT — findings

**S1 — `prices_raw` carries no `knowable_date` column.** The source register calls bhavcopies
"Immutable once published" (EXECUTION_PLAN.md:121-123), so the trade date is treated as the knowable
date. But nothing in the L1 schema (`schemas.py:166-188`) says so: the declaration is made *at read
time*, by each consumer, as `knowable_date=as_of` (`backtest/run.py:753`, `:999`) or
`knowable_date=session` (`forecast_run.py:446`). Invariant #7's "every queryable dataset declares..."
is satisfied by convention at the call site for the platform's single largest dataset, not by the
dataset. A consumer that forgets is not caught by the schema.

**S2 — the identity master has valid time but no knowledge time.** `security_master.status` is
current-only, with the comment "Historical status transitions live in `exchange_listing`"
(`0001:93-94`) — but `exchange_listing` is PK `(isin, exchange)`, one row per pair, so it too holds
only the current status alongside `listing_date`/`delisting_date`. There is no way to ask *what did we
believe this security's status was on date D*. `symbol_history` has `valid_from`/`valid_to` (when the
symbol was true) plus `recorded_at` (when we wrote it), which is closer to bitemporal, but nothing
queries on `recorded_at`. Two consequences are already on the record: status is **last-writer-wins on
re-ingest**, so whichever exchange was ingested last decides it (`ops/BACKLOG.md:59`), and the master
being NSE-only meant `status` was always ACTIVE (`ops/BACKLOG.md:57`).

**S3 — `adjustment_factors` has no `knowable_date`, and this is the sharpest silence.**
`corporate_actions` carries one precisely because "a backtest as of D may only see rows with
`knowable_date <= D`" (`0001:167-170`). But the factor chain derived from those actions carries only
`computed_at`, and §4.3 rule 2 mandates that a new CA "triggers retroactive recompute of the full
factor chain for that ISIN" (EXECUTION_PLAN.md:153) — an **in-place rewrite** of `(isin, ex_date)`. So
the knowability discipline the plan installs on the *input* does not survive into the *derived* series
a backtest actually reads. `prices_adjusted` inherits the same gap: `cum_price_factor` is a column of
the L2 row with no statement of when that value became knowable. Nothing in EXECUTION_PLAN §4.3,
TASK_GRAPH M2.4/M2.5/M9.2, or the migrations addresses it. Compounding it: all 2,784
`adjustment_factors` rows have `corporate_action_id` NULL, so a factor cannot currently be traced to
the action whose `knowable_date` would answer the question.

**S4 — the daily EOD datasets have no publication-lag model.** `fii_dii_flows`, `deals`,
`fo_contracts`/`fo_aggregates`, and `prices_raw` all key on `trade_date`. The plan's Source Register
marks them "Immutable" (EXECUTION_PLAN.md:121-132) but never states *when on or after the trade date*
each becomes knowable. For a T0 monitor that runs at 18:30 IST (`registry.py:199`) this may be benign;
for a replay that treats trade date as knowable date it is an untested assumption. The plan does not
distinguish them.

**S5 — the F&O rollover proxy has no PIT statement.** `rollover_pct`, `near_month_oi`,
`next_month_oi` (`fo_aggregates.py:477-479`) are derived across expiries. Neither §4.1 nor M3.7 says
whether a rollover figure is knowable on its own trade date.

**S6 — restated-vs-PIT is enforced for *fundamentals* only.** Invariant #8 and the quarantine
machinery are fundamentals-specific (`dataplatform/query/quarantine.py`,
`dataplatform/quality/quarantine.py`, `screen.PitFundamentals`). No equivalent structural boundary
exists for any other retroactively-revised source. Screener's re-basing of historical EPS for later
bonuses/splits is measured — "6.8% of quarter pairs differ by a clean CA ratio" — and logged as
backlog, not as an invariant (`ops/BACKLOG.md:153`).

**S7 — segment identity is unstable and unmodelled.** §5.3's BC1 ("two consecutive quarters of
segment revenue decline") evaluates over `pit_fundamentals.segment`, but "Segment names drift across a
company's filings — RELIANCE: `Oil & Gas` / `Oil and Gas`, `Organised Retail` / `Organized Retail` /
`Retail`" (`ops/BACKLOG.md:157`). There is no segment master and no plan text about one, so a
PIT-correct segment time series is not currently constructible.

---

## 5. MULTI-FUND READINESS

**The spec models multiple simultaneous cases sharing one household book. It does not model multiple
funds with independent NAV, and it does not model multiple tenants.** All three of those distinctions
are live in the code, so the answer needs all three parts.

### 5.1 Multiple simultaneous cases: YES — specified, schema'd, coded

| Evidence | Citation |
|---|---|
| A1's stated purpose includes "**multi-case view, cross-case concentration**" | EXECUTION_PLAN.md:98 |
| Decision #11: "Rotation: **pure per-case dial** (0–100%)... Tactical sleeve share ratified at case creation" | EXECUTION_PLAN.md:34 |
| Rails include "**cross-case concentration**" as a first-class check | EXECUTION_PLAN.md:105, TASK_GRAPH.yaml:1132 |
| M5.3 spec: "versioned ratified policy sets..., SIP scheduler, **multi-case view, cross-case concentration input for rails**" | TASK_GRAPH.yaml:1152 |
| `case_` is PK'd on `case_id`; `policy_set`, `thesis`, `order_`, `decision_journal`, `token_usage` **all carry `case_id`** | `0001_init.sql:260,289,331,363,415,472` |
| **Per-case funding mode**: `case_.funding_mode in {PAPER, REAL}` — one case may be paper while another is real, and it "selects which Broker is injected and nothing else" | `0001_init.sql:265-266,280-282` |
| **Per-case capital plan**: `sip_amount_inr`, `sip_day_of_month` on the case row | `0001_init.sql:271-273` |
| **Per-case rails**, versioned: `rotation_dial_pct`, `max_position_pct`, `max_sector_pct`, `min_holdings`, `drawdown_review_pct` promoted to scalars on `policy_set` so "A8 reads scalars, not JSON" | `0001_init.sql:293-297,309` |
| **Per-case benchmarks**: `benchmark_primary`, `benchmark_secondary` | `0001_init.sql:269-270` |
| **Per-case book value** for rail math: `Portfolio(case_id, lots, cash)` — "Case value: deployed plus idle. What every percentage rail is a fraction of" | `analyst/rails/policies.py:145-172` |
| **Cross-case aggregation is implemented, with a stated rationale**: `CrossCaseExposure(isin, quantity, by_case)` — "two cases each holding 12% of themselves in one stock is a 24% household exposure that neither case's own rails can see" | `analyst/cases/service.py:229-244`, `analyst/rails/policies.py:237-246` |
| The cross-case SQL joins `order_` to `case_` and counts only `FUNDED`/`ACTIVE`/`SUSPENDED` cases — "A suspended case still holds stock, so its positions still crowd another case's cap; a closed one does not" | `analyst/cases/service.py:148-166` |
| A `CaseSummary` type exists precisely as "**One line of the multi-case view**" | `analyst/cases/service.py:247-260` |

### 5.2 Per-fund NAV / per-fund cash: NO persisted entity, for cases *or* funds

There is **no `position`, `holding`, `cash_balance`, `nav`, or `portfolio` table in any of the 10
migrations.** Confirmed by grep across all migration files: no `nav`, no `fund`, no `tenant`, no
`user_id`, no `owner` column anywhere.

What exists instead:

- **Per-case position is *derived*, not stored.** `CaseService.cross_case_exposure()` computes it by
  summing `order_.filled_quantity` signed by side over `EXECUTED` orders, grouped by
  `(isin, case_id)` — `analyst/cases/service.py:148-158`. The order journal is the book of record.
- **The comment says why quantities, not values**: "Quantities, not values: valuing a position needs
  a price, the price layer is D4's, and a case service that reached into it would own two jobs. A8
  multiplies by the price it is already holding" — `service.py:144-147`.
- **Per-case cash and NAV are runtime values, assembled by a caller.** `Portfolio.cash` and
  `Portfolio.total_value` are fields of a frozen dataclass constructed per rail check
  (`analyst/rails/policies.py:145-172`); `HouseholdExposure.household_total_value` is likewise
  "assembled from the case service's exposure plus current prices" by the daily loop
  (`policies.py:244-245`). Nothing persists either.
- **`PortfolioBook` — the only thing with cash, realized P&L, a ledger and XIRR — has no `case_id` at
  all** and is in-memory per run: `__init__(opening_cash)`, `self._cash`, `self._positions`,
  `self._realized`, `self._ledger`, `self._external` (`backtest/accounting.py:164-175`). It is a
  backtest artefact, reconstructed each replay, not a fund state.
- **The cash ledger is a protocol return, not a table**: `Broker.ledger() -> tuple[LedgerEntry, ...]`
  (`execution/broker.py:334-335`). For `KiteBroker` it reads a placeholder path because Kite has no
  order-independent cash ledger (`ops/BACKLOG.md:119`).

### 5.3 Per-fund rails: per-case YES, household PARTIAL — an acknowledged spec hole

Per-case rails are versioned scalars on `policy_set` (§5.1 above). The household layer is **built but
under-specified**:

> "The cross-case concentration rail **reuses `RiskRails.max_position_pct` as the household ceiling —
> §5.2 defines no separate ratified number** for household concentration." — `ops/BACKLOG.md:98`

> "A8 consumes `analyst.cases.RiskRails` and `HouseholdExposure` is assembled by the caller from
> `CrossCaseExposure` (quantity-based) plus current prices. **No** [assembler ships]." —
> `ops/BACKLOG.md:99`

And `analyst/rails/engine.py:133`: "when it is `None` the cross-case rail is **not** [applied]" — i.e.
the household rail is opt-in on a caller supplying the exposure, and no shipped component supplies it.

### 5.4 Multi-tenant: modelled at the decision level, absent from the schema

- Decision #1's implication is explicit: "**multi-tenant data model, single-tenant deployment**" —
  EXECUTION_PLAN.md:24.
- §11 defers it: "**multi-tenant activation (model is ready, deployment is not)**" —
  EXECUTION_PLAN.md:398.
- But **no tenant, user, account, or owner entity exists.** The only identity of a person anywhere is
  `policy_set.ratified_by text` and `thesis.ratified_by text` (`0001_init.sql:298,342`) — free text,
  no FK, no table. `ratification_kind in {HUMAN, FIXTURE}` distinguishes a real ratification from B9's
  fixture, but not *whose*.

### 5.5 The answer, stated plainly

**The spec models N cases inside one household, owned by one implicit person, with no persisted fund
state.** A "case" is the plan's unit of thematic portfolio — its own theme, horizon, SIP, benchmarks,
rails, dial, exit menu, cash policy and funding mode, versioned and ratified independently — and cases
are explicitly designed to coexist and to be aggregated for concentration. That is genuine
multi-portfolio modelling, and the schema carries `case_id` on every decision-side table.

Three things it is not:

1. **It is not a fund.** There is no NAV, no unit, no subscription/redemption, no valuation date, and
   no persisted cash or position. `order_` plus a price is the only book; every portfolio view is
   recomputed in memory. Nothing in EXECUTION_PLAN, TASK_GRAPH, or the migrations names a `fund` entity.
2. **It is not multi-tenant.** Decision #1 and §11 both say the *data model* is meant to be
   multi-tenant, but no tenant/user/account entity was ever specified or built. `ratified_by` is free text.
3. **The household layer is the thinnest specified surface.** The rail exists in code with a
   documented rationale, but §5.2 ratifies **no household number** and no shipped component assembles
   the household exposure — both recorded as defects, not as design (`ops/BACKLOG.md:98-99`).

**And nothing has ever been operated at any scale**: `case_` = 0 rows, `policy_set` = 0,
`decision_journal` = 0, `order_` = 0, `scheduler_heartbeat` = 0. The multi-case machinery has never
held a single case.

---

## 6. OPEN AMENDMENTS AND UNRESOLVED DECISIONS BEARING ON DATA

### 6.1 Formal amendment log (EXECUTION_PLAN §12)

| Status | Amendment | Data consequence |
|---|---|---|
| **RATIFIED** 2026-08-08 | `platform/` -> `dataplatform/` (EXECUTION_PLAN.md:407) | Path-only; all 71 TASK_GRAPH strings swept. §8.2's diagram at line 304 **still reads `platform/`** — kept as historical text |
| **RATIFIED** 2026-08-08 | New task M5.15, `AUTH_REQUIRED` interlock (EXECUTION_PLAN.md:408) | Added two journal decision values (`AUTH_REQUIRED`, `DEFERRED`) via `0007_auth_required_decision.sql`; "a decision that evaporates because of an auth failure is a journal lie" (TASK_GRAPH.yaml:1375) |
| **PROPOSED — awaiting owner ratification** 2026-09-04 | **§4.1 row 17 "Macro / economic backdrop"** (EXECUTION_PLAN.md:410) | **Directly blocking.** "The plan has no row for the economic and market-state backdrop the analyst reasons against, so the daily index valuation series (P/E, P/B, dividend yield per index...) and any macro series have no §4.1 home and **cannot be registered**." Two entries staged: `worldbank_indicator_api` (VERIFIED) and `alfred_series_vintage` (FAILED — unreachable, cause undetermined). Note `nifty_index_close_snapshot` is already VERIFIED in the register while its plan row is unratified |

### 6.2 Open decisions in HUMAN_DECISIONS.md

| ID | Question | Data bearing | Status |
|---|---|---|---|
| **D12** | "The source register cannot say *we are declining this source on policy grounds*" — `HUMAN_DECISIONS.md:466-499` | The status enum is `VERIFIED / FAILED / BLOCKED_CREDENTIAL`. `screener_company_fundamentals` is labelled `BLOCKED_CREDENTIAL` but is actually blocked by **robots policy** (`/user/*` disallowed) — "the label actively points at the prohibited action" (:480). Recommendation is option 1, add `BLOCKED_POLICY`/`DECLINED`. **"this row is `M7.1`'s input, so the decision should land before M7.1 is built"** (:498) — M7.1 is already DONE, so it was built under the wrong label | **OPEN** |
| **D10** | Wave A merge order (`:359-425`) | Historical build-machinery hygiene; no entity impact | OPEN (largely superseded) |
| D13 | Momentum sleeve policy for paper mode | **ANSWERED** 2026-09-06 "all on + redeploy". Ratified params = `momentum_v2.PAPER_RATIFIED_2026_09_06`. Explicitly **paper only**. Carries a live gap: "**the paper-trading job that runs this configuration daily is not yet built**... the scheduler registers no analyst session job; building it is the next decision" (`:513-514`) | ANSWERED |
| D14 | BSE bhavcopy campaign go | **ANSWERED** 2026-09-06 go. Surfaced and fixed a data-destroying defect: `write_prices_raw` "would have overwritten every NSE `prices_raw` partition from the cutover onward with BSE rows — one file per date, no exchange in the path — and on the server, where the price L1 has no L0 behind it, **unrecoverably**" (`:525-529`). Explicitly **not** covered: BSE scrip master, BSE corporate actions, legacy-era BSE bhavcopy | ANSWERED |

### 6.3 Coming up — reserved to the human, will block data work

| Task | Decision | Blocks |
|---|---|---|
| `M6.8` | An Anthropic API key, to exercise T1/T2 and measure real cost (`HUMAN_DECISIONS.md:539`) | Live-model quality evidence for the M6 gate; every verdict entity is StubLLM-only until then |
| `M8.3` | Whether to run tiny-capital live-order sessions. "**Read `ops/compliance/sebi-algo-memo.md` Q1 first** — Kite's terms 2(e) say the APIs are not intended for fully automated trading without manual intervention, which is a question about the product's shape, not just this gate" (`:540`) | M8 gate. See also the risk-register row at EXECUTION_PLAN.md:390 rating this **H/H** |
| `M8.4` | Graduation (decision #8, discretionary) | — |
| `M10.4` | The XBRL bulk campaign go (still `PENDING`) | Nominally `M10.5`/`M10.6` — though both are DONE and the store holds 1.35M facts, so the state record trails reality |
| `M11.2` | The ~2,470-request index-valuation backfill go. Note the explicit constraint: "**Do not run it alongside the M10.4 fundamentals campaign**: both target NSE hosts, and two concurrent runners halve the effective per-host spacing through a side channel" (TASK_GRAPH.yaml:2100-2102) | `macro_series` has no data without it |

### 6.4 Deferred by design (EXECUTION_PLAN §11, :396-398)

SEBI RA/RIA registration · **multi-tenant activation** · product UI beyond ratification pages ·
pricing · **exchange data-license for redistributing archives** · formal graduation gate · marketing
name. Two of these bear on data: multi-tenant (see §5.4) and the archive redistribution licence, which
is why `/archives` serves a manifest but no per-file download link (`ops/BACKLOG.md:42`,
EXECUTION_PLAN.md:392).

### 6.5 Entity-level gaps the repo names against itself

Not speculation — each is a statement in a repo document:

| Gap | Source |
|---|---|
| **Fund/ETF scheme master** for 593 `INF` ISINs; "LIQUIDBEES is the default cash-parking instrument and **D2 cannot name it**" | `data-catalogue:132`; source unprobed, `ops/BACKLOG.md:165` |
| **No cash-flow statement** anywhere -> FCF and accruals impossible; ROCE approximable only; ROE annual-only | `ops/gates/M10-data-gap-plan.md:40-43` |
| **Historical index membership**: one snapshot; the weekly accumulator "has never fired" | `data-catalogue:134`; `ops/BACKLOG.md:126` |
| **Historical FII/DII unobtainable** from NSE (measured); NSDL/CDSL carry FPI-only history and **are not registered** | `data-catalogue:135`; `ops/BACKLOG.md:51` |
| **`symbolchange.csv` has no register row** — §4.1 row 6 names "name-change history files" but C.1's sweep never registered it | `ops/BACKLOG.md:56` |
| **A third pre-2016 bhavcopy era exists** (`cm04JAN2010bhav.csv`, eleven columns) — fetched, kept in L0, not checked in, not parsed | `ops/BACKLOG.md:47` |
| **Manual-entry queue for unparseable CA terms is not persisted** — "it lives for the duration of one parse run" | `ops/BACKLOG.md:40,65` |
| **Rights issues absent from the factor chain** (TERP needs the cum-rights close) | `ops/BACKLOG.md:82` |
| **BSE rows never reach L2** (series filter is `'EQ'`; BSE series is a group letter) | `ops/BACKLOG.md:89` |
| **Golden CA suite is 7 of ~20** cases | `ops/BACKLOG.md:13`; EXECUTION_PLAN.md:155 |
| **`prices_raw` has no `--rebuild-from-l0`** on the price backfills | `ops/BACKLOG.md:163` |
| **774 filings whose XBRL document is not in L0**, enumerated and ready to fetch | `ops/BACKLOG.md:164`, `ops/gates/missing-xbrl-payloads-2026-09-06.json` |
| **Intraday anything: out of scope by design** (EOD platform) — "stated so a reader does not look for it" | `data-catalogue:136` |

---

## 7. What did not fit this task

- I did not audit whether the code is *correct* against its specs — only whether it exists and whether
  data landed. Judging correctness is a review task.
- `execution/costs/rates.yaml` has more schedules than the 50 lines I read; I cited its provenance
  discipline and the four backlog caveats, not every rate.
- The three M12 gate reports (`M12-strategy-sweep-decade.md`, `-sixyear.md`,
  `M12-strategy-verdict.md`) I confirmed exist and are committed but did not read — they carry
  strategy results, not entity definitions.
- `.github/workflows/ci.yml` I did not open; `ops/BACKLOG.md:117` records that CI provisions no
  Postgres, so `integration`-marker tests skip there.
