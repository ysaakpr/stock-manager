# Derived-Data Inventory — `stock-manager`, measured 2026-09-07

**Environment:** Postgres 16 is **UP** (`trading-platform-postgres-1`, host port 5433). The `app` container (status API) is **DOWN in a crash loop** — see §5. All queries below were run read-only via `docker exec … psql`; all L1/L2 measurements via DuckDB 1.5.5 over the parquet files. Nothing was written.

## Headline answer

| Dataset | Have it? | Complete for last 20 years (2006→2026)? |
|---|---|---|
| L1 `prices_raw` (OHLCV) | **YES** | **NO — 10 years.** 2016-09-01 → 2026-09-04 |
| L2 `prices_adjusted` | **YES, NSE-only** | **NO — 10 years**, and only 3,349 of 3,731 NSE-EQ ISINs |
| L1 `prices_raw_quarantine` | YES (1.79M rejected rows) | n/a — 2016→2026 |
| L1 `pit_fundamentals` | **YES** | **NO — 8 years.** filings 2018-05-21 → 2026-09-05 |
| PG `corporate_actions` | YES (47,887) | **Rows reach back to 2000, but pre-2007 is 1–3% density. And the PIT column is degenerate — see §5.** |
| PG `adjustment_factors` | YES (2,784) | 2000→2026, but only 1,823 ISINs and **zero CA lineage** |
| PG `security_master` / `symbol_history` / `exchange_listing` | YES | **NO history** — built from a today-snapshot, `first_seen_date` min = 2026-08-08 |
| PG `isin_lineage` | YES (399 edges) | 2016→2026 |
| PG `quality_flag` | YES (21,154) | only one check ever ran |
| PG `case_`, `thesis`, `policy_set`, `decision_journal`, `order_`, `token_usage`, `job_run`, `scheduler_heartbeat` | **NO — 0 rows** | Declared, never written |
| L1 `fo_contracts`, `macro_series`, `announcements`, `index_constituents`, `benchmark_tri`, `shareholding`, `fii_dii_flows`, `news`, `deals`; L2 `fo_aggregates`; `RESTATED/screener_fundamentals` | **NO — directory does not exist** | Planned, not had |

---

## 1. Full logical schema

### 1a. Domains — the type-level invariants

`dataplatform/store/migrations/0001_init.sql:31-53`:

```
dom       | base           | check
isin      | text           | CHECK (VALUE ~ '^[A-Z]{2}[A-Z0-9]{9}[0-9]$')
money_inr | numeric(20,6)  |
factor    | numeric(30,15) | CHECK (VALUE > 0)
percent   | numeric(9,6)   | CHECK (VALUE >= 0 AND VALUE <= 100)
```

> Query: `SELECT t.typname, format_type(t.typbasetype,t.typtypmod), pg_get_constraintdef(c.oid) FROM pg_type t LEFT JOIN pg_constraint c ON c.contypid=t.oid WHERE t.typtype='d' AND t.typnamespace='public'::regnamespace;`

**Money invariant is structurally enforced** — there is no `float`/`double precision`/`real` column anywhere in the schema. Every price, cost and factor is `numeric`. Same in parquet: every price column is `DECIMAL(…)`, verified by `DESCRIBE` below.

**Append-only (invariant #12)** is enforced by trigger on exactly two tables:
```
decision_journal | decision_journal_append_only, decision_journal_no_truncate
policy_set       | policy_set_append_only, policy_set_no_truncate
```
> Query: `SELECT c.relname, t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid WHERE NOT t.tgisinternal;`
> Function defined at `0001_init.sql:58` (`reject_mutation()`).

### 1b. Postgres tables (20)

Column list obtained from `pg_attribute` (full dump run; abbreviated here to types + invariants). PKs/FKs from `pg_constraint`.

| Table | Migration | PK | ISIN col | Money cols | Rows |
|---|---|---|---|---|---|
| `security_master` | `0001_init.sql:75` | `(isin)` | `isin` (domain) | `face_value_inr money_inr` | 8,598 |
| `symbol_history` | `0001_init.sql:96` | `(id)`, UQ `(isin,exchange,symbol,valid_from)` | `isin` FK→security_master | — | 11,359 |
| `exchange_listing` | `0001_init.sql:117` | `(isin, exchange)` | `isin` FK | `face_value_inr` | 10,870 |
| `corporate_actions` | `0001_init.sql:140` | `(id)`, UQ `(isin,ex_date,action_type,source)` | `isin` FK, `filed_against_isin` (`0010:…`) | `dividend_amount_inr` | 47,887 |
| `adjustment_factors` | `0001_init.sql:175` | `(isin, ex_date)` | `isin` FK | `factor` ×4 | 2,784 |
| `sync_state` | `0001_init.sql:198`, PK widened by `0008_sync_state_unit.sql` | `(source, logical_date, unit)` | — | — | 117,469 |
| `quality_flag` | `0001_init.sql:233` | `(id)` | `isin` nullable | `numeric` observed/threshold | 21,154 |
| `case_` | `0001_init.sql:259` | `(case_id)` | — | `sip_amount_inr` | **0** |
| `policy_set` | `0001_init.sql:287` | `(id)`, UQ `(case_id,version)` | — | `percent` ×4 | **0** |
| `thesis` | `0001_init.sql:329` | `(id)`, UQ `(case_id,isin,version)` | `isin` FK | — | **0** |
| `decision_journal` | `0001_init.sql:359` | `(id)` | `isin` nullable | `cost_inr` | **0** |
| `order_` | `0001_init.sql:412` | `(id)`, UQ `(order_uid)` | `isin` FK | 6× `money_inr` | **0** |
| `token_usage` | `0001_init.sql:466` | `(id)` | — | `cost_inr`, `cost_usd numeric(20,6)` | **0** |
| `scheduler_heartbeat` | `0002_status_surface.sql:23` | `(scheduler_id)` | — | — | **0** |
| `archive_bundle` | `0002_status_surface.sql:48` | `(logical_date)` | — | — | **1** |
| `job_run` | `0003_scheduler.sql:25` | `(run_id uuid)` | — | — | **0** |
| `identity_reconciliation` | `0004_identity_reconciliation.sql:27` | `(id)` | `isins text[]` | — | 10 |
| `l2_invalidation` | `0006_l2_invalidation.sql:22` | `(id)` | `isin` FK | — | 10,332 |
| `isin_lineage` | `0009_isin_lineage.sql:32` | `(predecessor_isin, successor_isin)` | both `isin` | — | 399 |
| `schema_migrations` | (runner) | `(version)` | — | — | 10 |

> Row counts: `SELECT count(*) FROM <t>;` run individually per table (exact, not `n_live_tup`).

**PIT columns present:** `corporate_actions.knowable_date NOT NULL` + `announcement_date`; `security_master.first_seen_date/last_seen_date`; `symbol_history.valid_from/valid_to`; `exchange_listing.listing_date/delisting_date`; `sync_state.logical_date`; `quality_flag.logical_date`; `decision_journal.trading_date`.

**ISIN join key:** enforced by the `isin` domain + FK to `security_master` on 9 tables. `symbol` appears only in `symbol_history` (the mapping table itself) and in the L1 parquet as a carried-through attribute, never as a join key.

#### Full column dump (from `pg_attribute`)

```
adjustment_factors | 1 | isin | isin | NOT NULL
adjustment_factors | 2 | ex_date | date | NOT NULL
adjustment_factors | 3 | price_factor | factor | NOT NULL
adjustment_factors | 4 | qty_factor | factor | NOT NULL
adjustment_factors | 5 | cum_price_factor | factor | NOT NULL
adjustment_factors | 6 | cum_qty_factor | factor | NOT NULL
adjustment_factors | 7 | corporate_action_id | bigint |
adjustment_factors | 8 | structural_break | boolean | NOT NULL
adjustment_factors | 9 | computed_at | timestamp with time zone | NOT NULL
archive_bundle | 1 | logical_date | date | NOT NULL
archive_bundle | 2 | schema_version | text | NOT NULL
archive_bundle | 3 | bundle_path | text | NOT NULL
archive_bundle | 4 | manifest_sha256 | text | NOT NULL
archive_bundle | 5 | file_count | integer | NOT NULL
archive_bundle | 6 | total_bytes | bigint | NOT NULL
archive_bundle | 7 | manifest | jsonb | NOT NULL
archive_bundle | 8 | published_at | timestamp with time zone | NOT NULL
case_ | 1 | case_id | text | NOT NULL
case_ | 2 | title | text | NOT NULL
case_ | 3 | state | text | NOT NULL
case_ | 4 | funding_mode | text | NOT NULL
case_ | 5 | theme | text |
case_ | 6 | horizon_years | integer |
case_ | 7 | benchmark_primary | text |
case_ | 8 | benchmark_secondary | text |
case_ | 9 | sip_amount_inr | money_inr |
case_ | 10 | sip_day_of_month | integer |
case_ | 11 | config | jsonb | NOT NULL
case_ | 12 | created_at | timestamp with time zone | NOT NULL
case_ | 13 | updated_at | timestamp with time zone | NOT NULL
corporate_actions | 1 | id | bigint | NOT NULL
corporate_actions | 2 | isin | isin | NOT NULL
corporate_actions | 3 | ex_date | date | NOT NULL
corporate_actions | 4 | action_type | text | NOT NULL
corporate_actions | 5 | ratio_terms | jsonb | NOT NULL
corporate_actions | 6 | dividend_amount_inr | money_inr |
corporate_actions | 7 | record_date | date |
corporate_actions | 8 | announcement_date | date |
corporate_actions | 9 | knowable_date | date | NOT NULL
corporate_actions | 10 | source | text | NOT NULL
corporate_actions | 11 | source_ref | text |
corporate_actions | 12 | reconciled | boolean | NOT NULL
corporate_actions | 13 | reconciliation_note | text |
corporate_actions | 14 | recorded_at | timestamp with time zone | NOT NULL
corporate_actions | 15 | raw_text | text |
corporate_actions | 16 | l0_key | text |
corporate_actions | 17 | filed_against_isin | isin |
decision_journal | 1 | id | bigint | NOT NULL
decision_journal | 2 | ts | timestamp with time zone | NOT NULL
decision_journal | 3 | trading_date | date | NOT NULL
decision_journal | 4 | case_id | text |
decision_journal | 5 | actor | text | NOT NULL
decision_journal | 6 | decision | text | NOT NULL
decision_journal | 7 | isin | isin |
decision_journal | 8 | sleeve | text |
decision_journal | 9 | evidence_snapshot_ref | text |
decision_journal | 10 | break_conditions_evaluated | jsonb | NOT NULL
decision_journal | 11 | rationale | text |
decision_journal | 12 | model | text |
decision_journal | 13 | tokens_in | integer |
decision_journal | 14 | tokens_out | integer |
decision_journal | 15 | cost_inr | money_inr |
decision_journal | 16 | orders_ref | text |
decision_journal | 17 | payload | jsonb | NOT NULL
decision_journal | 18 | recorded_at | timestamp with time zone | NOT NULL
exchange_listing | 1 | isin | isin | NOT NULL
exchange_listing | 2 | exchange | text | NOT NULL
exchange_listing | 3 | security_code | text |
exchange_listing | 4 | series | text |
exchange_listing | 5 | lot_size | integer |
exchange_listing | 6 | face_value_inr | money_inr |
exchange_listing | 7 | listing_date | date |
exchange_listing | 8 | delisting_date | date |
exchange_listing | 9 | status | text | NOT NULL
exchange_listing | 10 | recorded_at | timestamp with time zone | NOT NULL
identity_reconciliation | 1 | id | bigint | NOT NULL
identity_reconciliation | 2 | kind | text | NOT NULL
identity_reconciliation | 3 | exchange | text | NOT NULL
identity_reconciliation | 4 | on_date | date | NOT NULL
identity_reconciliation | 5 | symbols | text[] | NOT NULL
identity_reconciliation | 6 | isins | text[] | NOT NULL
identity_reconciliation | 7 | detected_by | text | NOT NULL
identity_reconciliation | 8 | source | text | NOT NULL
identity_reconciliation | 9 | detail | jsonb | NOT NULL
identity_reconciliation | 10 | resolved | boolean | NOT NULL
identity_reconciliation | 11 | resolved_at | timestamp with time zone |
identity_reconciliation | 12 | resolution | text |
identity_reconciliation | 13 | raised_at | timestamp with time zone | NOT NULL
isin_lineage | 1 | predecessor_isin | isin | NOT NULL
isin_lineage | 2 | successor_isin | isin | NOT NULL
isin_lineage | 3 | effective_date | date | NOT NULL
isin_lineage | 4 | detected_by | text | NOT NULL
isin_lineage | 5 | confidence | text | NOT NULL
isin_lineage | 6 | gap_sessions | integer | NOT NULL
isin_lineage | 7 | symbol_at_change | text | NOT NULL
isin_lineage | 8 | corroborating_action | text |
isin_lineage | 9 | computed_at | timestamp with time zone | NOT NULL
job_run | 1 | run_id | uuid | NOT NULL
job_run | 2 | job_name | text | NOT NULL
job_run | 3 | state | text | NOT NULL
job_run | 4 | instance | text | NOT NULL
job_run | 5 | started_at | timestamp with time zone | NOT NULL
job_run | 6 | finished_at | timestamp with time zone |
job_run | 7 | error | text |
l2_invalidation | 1 | id | bigint | NOT NULL
l2_invalidation | 2 | isin | isin | NOT NULL
l2_invalidation | 3 | reason | text | NOT NULL
l2_invalidation | 4 | from_date | date |
l2_invalidation | 5 | requested_at | timestamp with time zone | NOT NULL
l2_invalidation | 6 | resolved | boolean | NOT NULL
l2_invalidation | 7 | resolved_at | timestamp with time zone |
order_ | 1 | id | bigint | NOT NULL
order_ | 2 | order_uid | text | NOT NULL
order_ | 3 | case_id | text | NOT NULL
order_ | 4 | isin | isin | NOT NULL
order_ | 5 | exchange | text | NOT NULL
order_ | 6 | sleeve | text | NOT NULL
order_ | 7 | side | text | NOT NULL
order_ | 8 | order_type | text | NOT NULL
order_ | 9 | quantity | integer | NOT NULL
order_ | 10 | limit_price_inr | money_inr |
order_ | 11 | state | text | NOT NULL
order_ | 12 | broker | text | NOT NULL
order_ | 13 | broker_order_id | text |
order_ | 14 | staged_at | timestamp with time zone | NOT NULL
order_ | 15 | staged_for_date | date | NOT NULL
order_ | 16 | executed_at | timestamp with time zone |
order_ | 17 | filled_quantity | integer | NOT NULL
order_ | 18 | avg_fill_price_inr | money_inr |
order_ | 19 | gross_value_inr | money_inr |
order_ | 20 | costs_inr | money_inr |
order_ | 21 | net_value_inr | money_inr |
order_ | 22 | cost_breakdown | jsonb |
order_ | 23 | decision_journal_id | bigint |
order_ | 24 | rejection_reason | text |
order_ | 25 | updated_at | timestamp with time zone | NOT NULL
policy_set | 1 | id | bigint | NOT NULL
policy_set | 2 | case_id | text | NOT NULL
policy_set | 3 | version | integer | NOT NULL
policy_set | 4 | supersedes_version | integer |
policy_set | 5 | policy | jsonb | NOT NULL
policy_set | 6 | rotation_dial_pct | percent | NOT NULL
policy_set | 7 | max_position_pct | percent | NOT NULL
policy_set | 8 | max_sector_pct | percent | NOT NULL
policy_set | 9 | min_holdings | integer | NOT NULL
policy_set | 10 | drawdown_review_pct | percent | NOT NULL
policy_set | 11 | ratified_by | text | NOT NULL
policy_set | 12 | ratified_at | timestamp with time zone | NOT NULL
policy_set | 13 | ratification_kind | text | NOT NULL
policy_set | 14 | recorded_at | timestamp with time zone | NOT NULL
quality_flag | 1 | id | bigint | NOT NULL
quality_flag | 2 | logical_date | date | NOT NULL
quality_flag | 3 | check_name | text | NOT NULL
quality_flag | 4 | severity | text | NOT NULL
quality_flag | 5 | isin | isin |
quality_flag | 6 | source | text |
quality_flag | 7 | detail | jsonb | NOT NULL
quality_flag | 8 | observed_value | numeric |
quality_flag | 9 | threshold | numeric |
quality_flag | 10 | resolved | boolean | NOT NULL
quality_flag | 11 | resolved_at | timestamp with time zone |
quality_flag | 12 | resolution | text |
quality_flag | 13 | raised_at | timestamp with time zone | NOT NULL
scheduler_heartbeat | 1 | scheduler_id | text | NOT NULL
scheduler_heartbeat | 2 | beat_at | timestamp with time zone | NOT NULL
scheduler_heartbeat | 3 | detail | jsonb | NOT NULL
scheduler_heartbeat | 4 | updated_at | timestamp with time zone | NOT NULL
schema_migrations | 1 | version | text | NOT NULL
schema_migrations | 2 | name | text | NOT NULL
schema_migrations | 3 | checksum | text | NOT NULL
schema_migrations | 4 | applied_at | timestamp with time zone | NOT NULL
security_master | 1 | isin | isin | NOT NULL
security_master | 2 | name | text | NOT NULL
security_master | 3 | primary_exchange | text | NOT NULL
security_master | 4 | status | text | NOT NULL
security_master | 5 | face_value_inr | money_inr |
security_master | 6 | first_seen_date | date | NOT NULL
security_master | 7 | last_seen_date | date |
security_master | 8 | created_at | timestamp with time zone | NOT NULL
security_master | 9 | updated_at | timestamp with time zone | NOT NULL
symbol_history | 1 | id | bigint | NOT NULL
symbol_history | 2 | isin | isin | NOT NULL
symbol_history | 3 | exchange | text | NOT NULL
symbol_history | 4 | symbol | text | NOT NULL
symbol_history | 5 | series | text |
symbol_history | 6 | valid_from | date | NOT NULL
symbol_history | 7 | valid_to | date |
symbol_history | 8 | source | text | NOT NULL
symbol_history | 9 | recorded_at | timestamp with time zone | NOT NULL
sync_state | 1 | source | text | NOT NULL
sync_state | 2 | logical_date | date | NOT NULL
sync_state | 3 | state | text | NOT NULL
sync_state | 4 | attempts | integer | NOT NULL
sync_state | 5 | retryable | boolean | NOT NULL
sync_state | 6 | last_error | text |
sync_state | 7 | checksum | text |
sync_state | 8 | l0_path | text |
sync_state | 9 | first_attempt_at | timestamp with time zone |
sync_state | 10 | updated_at | timestamp with time zone | NOT NULL
sync_state | 11 | unit | text | NOT NULL
thesis | 1 | id | bigint | NOT NULL
thesis | 2 | case_id | text | NOT NULL
thesis | 3 | isin | isin | NOT NULL
thesis | 4 | version | integer | NOT NULL
thesis | 5 | sleeve | text | NOT NULL
thesis | 6 | driver | text | NOT NULL
thesis | 7 | theme_purity | numeric(5,4) |
thesis | 8 | expected_evidence | jsonb | NOT NULL
thesis | 9 | break_conditions | jsonb | NOT NULL
thesis | 10 | status | text | NOT NULL
thesis | 11 | ratified_by | text |
thesis | 12 | ratified_at | timestamp with time zone |
thesis | 13 | recorded_at | timestamp with time zone | NOT NULL
token_usage | 1 | id | bigint | NOT NULL
token_usage | 2 | ts | timestamp with time zone | NOT NULL
token_usage | 3 | provider | text | NOT NULL
token_usage | 4 | model | text | NOT NULL
token_usage | 5 | purpose | text | NOT NULL
token_usage | 6 | case_id | text |
token_usage | 7 | decision_journal_id | bigint |
token_usage | 8 | tokens_in | bigint | NOT NULL
token_usage | 9 | tokens_out | bigint | NOT NULL
token_usage | 10 | cached_tokens | bigint | NOT NULL
token_usage | 11 | cost_inr | money_inr | NOT NULL
token_usage | 12 | cost_usd | numeric(20,6) |
token_usage | 13 | recorded_at | timestamp with time zone | NOT NULL
(226 rows)
```

#### Constraints (PK / UNIQUE / FK)

```
tbl|typ|conname|def
adjustment_factors|f|adjustment_factors_corporate_action_id_fkey|FOREIGN KEY (corporate_action_id) REFERENCES corporate_actions(id)
adjustment_factors|f|adjustment_factors_isin_fkey|FOREIGN KEY (isin) REFERENCES security_master(isin)
adjustment_factors|p|adjustment_factors_pkey|PRIMARY KEY (isin, ex_date)
archive_bundle|p|archive_bundle_pkey|PRIMARY KEY (logical_date)
case_|p|case__pkey|PRIMARY KEY (case_id)
corporate_actions|f|corporate_actions_isin_fkey|FOREIGN KEY (isin) REFERENCES security_master(isin)
corporate_actions|p|corporate_actions_pkey|PRIMARY KEY (id)
corporate_actions|u|corporate_actions_unique|UNIQUE (isin, ex_date, action_type, source)
decision_journal|f|decision_journal_case_id_fkey|FOREIGN KEY (case_id) REFERENCES case_(case_id)
decision_journal|p|decision_journal_pkey|PRIMARY KEY (id)
exchange_listing|f|exchange_listing_isin_fkey|FOREIGN KEY (isin) REFERENCES security_master(isin)
exchange_listing|p|exchange_listing_pkey|PRIMARY KEY (isin, exchange)
identity_reconciliation|p|identity_reconciliation_pkey|PRIMARY KEY (id)
identity_reconciliation|u|identity_reconciliation_unique|UNIQUE (kind, exchange, on_date, symbols, isins)
isin_lineage|f|isin_lineage_successor_isin_fkey|FOREIGN KEY (successor_isin) REFERENCES security_master(isin)
isin_lineage|p|isin_lineage_pkey|PRIMARY KEY (predecessor_isin, successor_isin)
job_run|p|job_run_pkey|PRIMARY KEY (run_id)
l2_invalidation|f|l2_invalidation_isin_fkey|FOREIGN KEY (isin) REFERENCES security_master(isin)
l2_invalidation|p|l2_invalidation_pkey|PRIMARY KEY (id)
order_|f|order__isin_fkey|FOREIGN KEY (isin) REFERENCES security_master(isin)
order_|f|order__case_id_fkey|FOREIGN KEY (case_id) REFERENCES case_(case_id)
order_|f|order__decision_journal_id_fkey|FOREIGN KEY (decision_journal_id) REFERENCES decision_journal(id)
order_|p|order__pkey|PRIMARY KEY (id)
order_|u|order__order_uid_key|UNIQUE (order_uid)
policy_set|f|policy_set_case_id_fkey|FOREIGN KEY (case_id) REFERENCES case_(case_id)
policy_set|p|policy_set_pkey|PRIMARY KEY (id)
policy_set|u|policy_set_version_unique|UNIQUE (case_id, version)
quality_flag|p|quality_flag_pkey|PRIMARY KEY (id)
scheduler_heartbeat|p|scheduler_heartbeat_pkey|PRIMARY KEY (scheduler_id)
schema_migrations|p|schema_migrations_pkey|PRIMARY KEY (version)
security_master|p|security_master_pkey|PRIMARY KEY (isin)
symbol_history|f|symbol_history_isin_fkey|FOREIGN KEY (isin) REFERENCES security_master(isin)
symbol_history|p|symbol_history_pkey|PRIMARY KEY (id)
symbol_history|u|symbol_history_unique|UNIQUE (isin, exchange, symbol, valid_from)
sync_state|p|sync_state_pkey|PRIMARY KEY (source, logical_date, unit)
thesis|f|thesis_case_id_fkey|FOREIGN KEY (case_id) REFERENCES case_(case_id)
thesis|f|thesis_isin_fkey|FOREIGN KEY (isin) REFERENCES security_master(isin)
thesis|p|thesis_pkey|PRIMARY KEY (id)
thesis|u|thesis_version_unique|UNIQUE (case_id, isin, version)
token_usage|f|token_usage_decision_journal_id_fkey|FOREIGN KEY (decision_journal_id) REFERENCES decision_journal(id)
token_usage|f|token_usage_case_id_fkey|FOREIGN KEY (case_id) REFERENCES case_(case_id)
token_usage|p|token_usage_pkey|PRIMARY KEY (id)
(42 rows)
```

#### Applied migrations

```sql
SELECT version,name,applied_at FROM schema_migrations ORDER BY version;
0001|init|2026-09-02 07:48:42.510879+00
0002|status_surface|2026-09-02 07:48:42.625039+00
0003|scheduler|2026-09-02 07:48:42.634706+00
0004|identity_reconciliation|2026-09-02 07:48:42.64578+00
0005|ca_taxonomy_and_lineage|2026-09-02 08:30:21.052835+00
0006|l2_invalidation|2026-09-02 11:13:53.94855+00
0007|auth_required_decision|2026-09-06 13:26:40.12637+00
0008|sync_state_unit|2026-09-06 13:26:40.129175+00
0009|isin_lineage|2026-09-06 15:11:50.177045+00
0010|ca_filed_against_isin|2026-09-06 15:11:50.196144+00
```

### 1c. L1 / L2 parquet datasets

Schemas via `DESCRIBE SELECT * FROM read_parquet('…', hive_partitioning=1) LIMIT 0`:

**`data/L1/prices_raw/date=YYYY-MM-DD/part.parquet`** — declared `dataplatform/store/schemas.py:53`, written `dataplatform/store/l1.py:116` (`write_prices_raw`).
```
isin VARCHAR | exchange VARCHAR | symbol VARCHAR | series VARCHAR | trade_date DATE
open/high/low/close/last/prev_close DECIMAL(20,4)
total_traded_qty BIGINT | total_traded_value DECIMAL(28,4) | total_trades BIGINT
deliv_qty BIGINT | deliv_pct DECIMAL(12,4)
date DATE  (hive partition)
```
Logical key: `(isin, exchange, series, trade_date)`. **Invariant #3 (no adjusted prices in L1) enforced structurally** — `schemas.py:12` states the writer refuses a table whose schema is not the declared one (`enforce_schema`); there is no adjusted column in the schema. All money = `DECIMAL`, no float.

**`data/L1/prices_raw_quarantine/date=…/part.parquet`** — `schemas.py:58`, written `l1.py:382` (`_write_quarantine`).
```
symbol | series | trade_date | exchange | isin | deliv_qty | deliv_pct | reason | date
```
Holds delivery rows that could not be placed onto a price row. `l1.py:139` — *"never … drop a delivery row"*.

**`data/L1/pit_fundamentals/date=<filing_date>/part.parquet`** — `dataplatform/store/pit_fundamentals.py:70`, written `pit_fundamentals.py:120` (`write_pit`).
```
isin | period_start DATE | period_end DATE | filing_date DATE | nature | taxonomy
filing_id | concept | segment | value DECIMAL(38,4) | derived BOOLEAN | source | l0_key | date
```
Key: `(filing_id, concept, segment)`. **Partitioned by `filing_date`, not period end** — that is the PIT guarantee (`pit_fundamentals.py:10-12`): a restatement is a *new record in a later partition*, so `read_pit(on_date)` selects partitions rather than filtering rows.

**`data/L2/prices_adjusted/isin=<ISIN>/part.parquet`** — `dataplatform/store/l2.py:112`, written `l2.py:451`.
```
isin | exchange | trade_date | adj_open/high/low/close DECIMAL(28,4) | adj_volume DECIMAL(38,4)
tr_close DECIMAL(28,4) | cum_price_factor DECIMAL(38,18) | cum_qty_factor DECIMAL(38,18)
```
Key: `(isin, trade_date)`. Partitioned per-ISIN because *"a corporate action re-scales exactly one"* (`l2.py:33`).

---

## 2. Row counts and temporal coverage

### Postgres

```sql
SELECT count(*), count(DISTINCT isin), min(ex_date), max(ex_date),
       count(DISTINCT ex_date), count(*) FILTER (WHERE reconciled)
FROM corporate_actions;
-- 47887 | 3624 | 2000-06-30 | 2026-09-30 | 4723 | 47322
```
```sql
SELECT count(*), count(DISTINCT isin), min(ex_date), max(ex_date),
       count(*) FILTER (WHERE structural_break), count(*) FILTER (WHERE corporate_action_id IS NULL)
FROM adjustment_factors;
-- 2784 | 1823 | 2000-08-02 | 2026-09-22 | 414 | 2784
```
```sql
SELECT count(*), min(first_seen_date), max(first_seen_date), min(last_seen_date), max(last_seen_date)
FROM security_master;
-- 8598 | 2026-08-08 | 2026-09-06 | 2026-08-08 | 2026-09-06
```
```sql
SELECT count(*), count(DISTINCT isin), min(valid_from), max(valid_from),
       count(*) FILTER (WHERE valid_to IS NOT NULL) FROM symbol_history;
-- 11359 | 8598 | 1995-02-08 | 2026-09-06 | 489
```
```sql
SELECT count(*), min(effective_date), max(effective_date) FROM isin_lineage;
-- 399 | 2016-09-14 | 2026-09-01
```
```sql
SELECT severity, min(logical_date), max(logical_date), count(DISTINCT logical_date), count(*)
FROM quality_flag GROUP BY 1;
-- ERROR | 2009-10-09 | 2026-09-18 | 1612 | 8532
-- WARN  | 2000-06-30 | 2026-09-30 | 2862 | 12622
```
```sql
SELECT reason, resolved, count(*), count(DISTINCT isin), min(from_date), max(from_date)
FROM l2_invalidation GROUP BY 1,2;
-- M9.1 corporate-action backfill | t | 10332 | 3447 | 2000-07-03 | 2026-09-22   (queue fully drained)
```
```sql
SELECT source, state, count(*), count(DISTINCT logical_date), count(DISTINCT unit), min(logical_date), max(logical_date)
FROM sync_state GROUP BY 1,2 ORDER BY 1,2;
```
| source | state | rows | dates | units | min | max |
|---|---|---|---|---|---|---|
| bse_bhavcopy | PUBLISHED | 536 | 536 | 1 | 2024-07-08 | 2026-09-04 |
| bse_bhavcopy_legacy | PUBLISHED | 1938 | 1938 | 1 | 2016-09-01 | 2024-07-05 |
| bse_bhavcopy_legacy | **FAILED** | 1 | 1 | 1 | 2021-12-29 | 2021-12-29 |
| bse_corp_actions | PUBLISHED | 6683 | 1 | 6683 | 2016-09-01 | 2016-09-01 |
| bse_corp_actions | **FAILED** | 1 | 1 | 1 | 2016-09-01 | 2016-09-01 |
| nifty_index_constituents | PUBLISHED / **FAILED** | 16 / 1 | 1 | 16 | 2026-09-03 | 2026-09-03 |
| nse_bhavcopy | PUBLISHED | 2471 | 2471 | 1 | 2016-09-02 | 2026-09-01 |
| nse_corp_actions | PUBLISHED | 11 | 11 | 1 | 2016-09-01 | 2026-09-01 |
| nse_financial_results_index | PUBLISHED / **FAILED** | 90 / 1 | 42 | 86 | 2016-04-01 | 2026-07-01 |
| nse_integrated_filing_index | PUBLISHED | 114 | 19 | 114 | 2025-03-01 | 2026-09-01 |
| nse_xbrl_filing | PUBLISHED | 102650 | 2015 | 102650 | 2018-05-21 | 2026-09-05 |
| nse_xbrl_filing | **FAILED** | 2956 | 553 | 2956 | 2018-05-25 | 2026-08-25 |

Totals: `SELECT state, count(*) FROM sync_state GROUP BY 1;` → `FAILED 2960 | PUBLISHED 114509`.

Exact row counts, all 20 tables (`SELECT count(*) FROM <t>;`):
```
adjustment_factors: 2784        job_run: 0                  security_master: 8598
archive_bundle: 1               l2_invalidation: 10332      symbol_history: 11359
case_: 0                        order_: 0                   sync_state: 117469
corporate_actions: 47887        policy_set: 0               thesis: 0
decision_journal: 0             quality_flag: 21154         token_usage: 0
exchange_listing: 10870         scheduler_heartbeat: 0
identity_reconciliation: 10     schema_migrations: 10
isin_lineage: 399
```

### L1 / L2

```python
duckdb: SELECT count(*), count(DISTINCT isin), min(trade_date), max(trade_date),
        count(DISTINCT trade_date), count(DISTINCT exchange)
        FROM read_parquet('data/L1/prices_raw/date=*/part.parquet', hive_partitioning=1)
-- 14,298,626 | 14,023 | 2016-09-01 | 2026-09-04 | 2,475 | 2
```
| dataset | rows | ISINs | min | max | distinct dates | on-disk |
|---|---|---|---|---|---|---|
| L1 `prices_raw` | **14,298,626** | 14,023 | 2016-09-01 | 2026-09-04 | 2,475 | 958M / 2,475 partitions |
| L1 `prices_raw_quarantine` | **1,794,742** | (3,042 symbols) | 2016-09-02 | 2026-09-01 | 2,461 | 56M / 2,461 |
| L1 `pit_fundamentals` | **1,349,562 facts** / 102,650 filings | 2,291 | filing 2018-05-21 | 2026-09-05 | 2,015 filing dates | 35M / 2,015 |
| L2 `prices_adjusted` | **4,276,035** | 3,349 | 2016-09-02 | 2026-09-01 | 2,471 | 192M / 3,349 |

`prices_raw` split by exchange:
```
BSE | 8,612,535 | 11,129 isins | 2016-09-01 → 2026-09-04 | 2,474 dates
NSE | 5,686,091 |  7,536 isins | 2016-09-02 → 2026-09-01 | 2,471 dates
```
`prices_adjusted` split by exchange: **NSE only** — `('NSE', 4276035, 3349)`. There is no BSE row in L2 at all.

Quarantine breakdown:
```sql
SELECT reason, exchange, count(*), count(DISTINCT symbol), min(trade_date), max(trade_date) FROM <quarantine> GROUP BY 1,2;
-- symbol_unresolved  | NSE | 1,358,650 | 2,706 | 2016-09-02 | 2026-09-01
-- no_matching_price  | NSE |   436,091 |   401 | 2016-09-02 | 2026-09-01
-- isin_not_published | NSE |         1 |     1 | 2021-02-16 | 2021-02-16
```
Quarantine per year (rows / distinct symbols): `2016 51,348/669 · 2017 155,722/796 · 2018 154,931/824 · 2019 158,343/975 · 2020 175,129/906 · 2021 173,032/952 · 2022 174,771/963 · 2023 187,432/1,087 · 2024 200,061/1,210 · 2025 212,851/1,287 · 2026 151,122/1,406`.

---

## 3. Per-entity completeness

### 3a. Price store — rows and ISINs per calendar year

```sql
SELECT year(trade_date), count(*), count(DISTINCT isin), count(DISTINCT trade_date), count(DISTINCT exchange) FROM <prices_raw> GROUP BY 1 ORDER BY 1
```

| Year | L1 rows | L1 ISINs | L1 sessions | L2 rows | L2 ISINs | L2 sessions |
|---|---|---|---|---|---|---|
| **2006–2015** | **0** | **0** | **0** | **0** | **0** | **0** |
| 2016 (from 09-01) | 373,293 | 4,507 | 82 | 123,644 | 1,601 | 81 |
| 2017 | 1,152,370 | 4,979 | 248 | 368,382 | 1,674 | 248 |
| 2018 | 1,158,978 | 5,139 | 246 | 367,971 | 1,658 | 246 |
| 2019 | 1,133,267 | 5,206 | 245 | 368,887 | 1,688 | 245 |
| 2020 | 1,192,550 | 5,220 | 251 | 378,645 | 1,729 | 251 |
| 2021 | 1,333,224 | 5,417 | 248 | 389,863 | 1,888 | 248 |
| 2022 | 1,436,944 | 5,644 | 248 | 438,894 | 2,015 | 248 |
| 2023 | 1,524,188 | 6,196 | 246 | 447,161 | 2,120 | 246 |
| 2024 | 1,718,992 | 8,355 | 246 | 462,175 | 2,282 | 246 |
| 2025 | 1,902,027 | 9,401 | 248 | 530,006 | 2,506 | 248 |
| 2026 (to 09-04) | 1,372,793 | 9,494 | 167 | 400,407 | 2,896 | 164 |

**2006→2015 is entirely absent.** The requested 20-year window is 50% empty.

Series mix in L1 (top): `EQ 4,274,581 | B 2,726,234 | X 1,981,790 | A 1,437,435 | XT 1,055,586 | BE 427,904 | T 425,561 | SM 319,265 | M 258,642 | XD 198,697`.

**Delivery columns are ~25% populated** — `count(*) FILTER (WHERE deliv_qty IS NOT NULL)` per year:
```
2016: 79,965/373,293     2020: 288,838/1,192,550   2024: 381,864/1,718,992
2017: 251,999/1,152,370  2021: 301,646/1,333,224   2025: 450,632/1,902,027
2018: 267,231/1,158,978  2022: 347,374/1,436,944   2026: 343,359/1,372,793
2019: 278,439/1,133,267  2023: 360,799/1,524,188
```
`total_trades`, `total_traded_value` and `prev_close` are 100% non-null in every year.

### 3b. L2 coverage gap against L1

```python
L1 NSE EQ distinct ISINs : 3,731   (rows 4,274,581)
L2 distinct ISINs        : 3,349
in L1-EQ not in L2       : 388     ← never materialized
in L2 not in L1-EQ       : 6       ← lineage-stitched survivors
Latest session 2026-09-01: L1 NSE EQ 2,646 ISINs | L2 2,646 ISINs  ← current names are complete
```
So **388 historical NSE-EQ ISINs have raw bars and no adjusted series**. `l2_fill.py:1-8` is exactly the driver for this class and its docstring names the same shape of gap.

Factor activity in L2: `SELECT count(*), count(DISTINCT isin) FROM <l2> WHERE cum_price_factor <> 1` → `535,391 | 553`. Only **553 of 3,349 ISINs** carry any non-unit adjustment.

### 3c. Corporate actions — rows per year by type

```sql
SELECT action_type, count(*), count(DISTINCT isin), min(ex_date), max(ex_date) FROM corporate_actions GROUP BY 1 ORDER BY 2 DESC;
```
| type | rows | ISINs | min ex_date | max ex_date |
|---|---|---|---|---|
| DIVIDEND | 42,378 | 2,997 | 2000-07-03 | 2026-09-30 |
| BONUS | 1,676 | 1,012 | 2000-08-02 | 2026-09-04 |
| SPLIT | 1,544 | 1,122 | 2001-10-03 | 2026-09-22 |
| RIGHTS | 1,051 | 654 | 2000-06-30 | 2026-09-03 |
| BUYBACK | 671 | 238 | 2012-06-07 | 2026-09-04 |
| DEMERGER | 260 | 157 | 2014-01-09 | 2026-09-07 |
| SCHEME_OF_ARRANGEMENT | 253 | 222 | 2000-11-06 | 2026-09-08 |
| DELISTING | 38 | 35 | 2000-12-08 | **2003-03-27** |
| MERGER | 16 | 16 | 2001-06-01 | 2026-07-24 |

By source: `bse_corp_actions 35,382 (2000-06-30→2026-09-30)`, `nse_corp_actions 12,505 (2016-09-01→2026-09-21)`.

Rows per year by type (`GROUP BY year(ex_date), action_type`) — the density collapse before 2007 is the headline:

| Year | DIV | BONUS | SPLIT | RIGHTS | BUYBACK | DEMERGER | SoA | MERGER | DELIST |
|---|---|---|---|---|---|---|---|---|---|
| 2000 | **108** | 8 | – | 3 | – | – | 1 | – | 1 |
| 2001 | **813** | 15 | 1 | 8 | – | – | 10 | 3 | 12 |
| 2002 | **271** | 5 | 2 | 9 | – | – | 8 | 1 | 21 |
| 2003 | **89** | 5 | 3 | 16 | – | – | 18 | 2 | 4 |
| 2004 | **85** | 10 | 8 | 16 | – | – | 17 | – | – |
| 2005 | **52** | 15 | – | 19 | – | – | 13 | – | – |
| 2006 | **54** | 12 | 4 | 33 | – | – | 19 | – | – |
| 2007 | 967 | 41 | 9 | 21 | – | – | 19 | – | – |
| 2008 | 1,348 | 52 | 8 | 24 | – | – | 17 | – | – |
| 2009 | 1,247 | 33 | 48 | 16 | – | – | 9 | – | – |
| 2010 | 1,442 | 81 | 89 | 16 | – | – | 32 | – | – |
| 2011 | 1,469 | 34 | 62 | 17 | – | – | 14 | – | – |
| 2012 | 1,385 | 38 | 49 | 15 | 1 | – | 18 | – | – |
| 2013 | 1,413 | 45 | 57 | 11 | 7 | – | 16 | – | – |
| 2014 | 1,388 | 29 | 61 | 17 | 2 | 4 | 9 | – | – |
| 2015 | 1,370 | 50 | 66 | 11 | 8 | 10 | 6 | – | – |
| 2016 | 1,701 | 62 | 72 | 13 | 37 | 16 | – | – | – |
| 2017 | 2,373 | 104 | 85 | 32 | 69 | 11 | 8 | 1 | – |
| 2018 | 2,467 | 97 | 64 | 13 | 84 | 25 | 10 | – | – |
| 2019 | 2,524 | 65 | 37 | 25 | 79 | 32 | 3 | 1 | – |
| 2020 | 2,122 | 51 | 37 | 36 | 57 | 21 | – | – | – |
| 2021 | 2,562 | 96 | 93 | 62 | 52 | 6 | 1 | 1 | – |
| 2022 | 2,827 | 177 | 145 | 79 | 62 | 28 | – | – | – |
| 2023 | 3,006 | 126 | 130 | 85 | 58 | 28 | 2 | – | – |
| 2024 | 3,173 | 177 | 181 | 182 | 80 | 27 | – | – | – |
| 2025 | 3,352 | 162 | 157 | 181 | 26 | 38 | 1 | – | – |
| 2026 | 2,770 | 86 | 76 | 91 | 49 | 14 | 2 | 7 | – |

**2003–2006 averages 70 dividends a year across the whole market.** That is not a corporate-action history; it is a handful of scrips. DELISTING stops entirely after 2003 — the store records **zero delistings for the last 23 years**, which is a survivorship-bias hazard for anything built on it.

`adjustment_factors` per year (`GROUP BY year(ex_date)` — rows, distinct ISINs):
```
2000: 9/9      2007: 60/59    2014: 95/90    2021: 139/134
2001: 28/28    2008: 69/68    2015: 124/121  2022: 241/229
2002: 14/14    2009: 79/75    2016: 120/118  2023: 191/184
2003: 24/23    2010: 184/175  2017: 128/125  2024: 248/235
2004: 27/26    2011: 92/90    2018: 133/132  2025: 243/228
2005: 28/28    2012: 87/87    2019: 96/95    2026: 126/121
2006: 31/31    2013: 93/90    2020: 75/73
```

### 3d. Fundamentals — coverage by fiscal period and ISIN

```python
SELECT count(*), count(DISTINCT isin), min(filing_date), max(filing_date), count(DISTINCT filing_date), count(DISTINCT filing_id) FROM <pit_fundamentals>
-- 1,349,562 | 2,291 | 2018-05-21 | 2026-09-05 | 2,015 | 102,650
SELECT min(period_end), max(period_end), count(DISTINCT period_end)
-- 2017-03-31 | 2026-06-30 | 36
```
By `year(period_end)`:
| FY-end year | facts | ISINs | filings |
|---|---|---|---|
| 2017 | 2,482 | 177 | 193 |
| 2018 | 79,997 | 1,024 | 5,843 |
| 2019 | 97,795 | 1,066 | 7,279 |
| 2020 | 125,582 | 1,128 | 9,435 |
| 2021 | 129,962 | 1,242 | 9,850 |
| 2022 | 139,734 | 1,338 | 10,604 |
| 2023 | 158,520 | 1,458 | 12,091 |
| 2024 | 176,908 | 1,583 | 13,466 |
| 2025 | 261,673 | 2,215 | 20,372 |
| 2026 (to Q2) | 176,909 | 2,266 | 13,517 |

Recent quarters (ISINs / filings): `2026Q2 2,248/4,164 · 2026Q1 2,240/9,353 · 2025Q4 2,189/4,164 · 2025Q3 2,142/4,330 · 2025Q2 2,102/3,866 · 2025Q1 2,026/8,012 · 2024Q4 1,574/2,868 · 2024Q3 1,544/2,737 · 2024Q2 1,506/2,687 · 2024Q1 1,474/5,174 · 2023Q4 1,455/2,560 · 2023Q3 1,410/2,471`.

By nature × taxonomy: `Standalone/Ind-AS 737,683 facts, 2,291 ISINs · Consolidated/Ind-AS 592,977, 1,846 · Standalone/Banking 10,294, 28 · Consolidated/Banking 4,789, 12 · Standalone/Non-Ind-AS 2,407, 97 · Consolidated/Non-Ind-AS 1,412, 66`.

**22 distinct concepts.** Top: `segment_revenue 123,614 · other_income 102,580 · total_income 102,578 · paid_up_equity_capital 102,511 · face_value_per_share 102,511 · total_expenses 102,020 · revenue_from_operations 101,950 · eps_diluted 101,950 · eps_basic 101,949 · profit_before_tax 101,949 · profit_after_tax 101,709 · shares_outstanding 96,569 · profit_attributable_to_owners 39,658 · debt_equity_ratio 26,765 · reserves_excl_revaluation 21,867`. `derived=True` on 112,030 of 1,349,562. Source: 100% `nse_xbrl_filing`.

**PIT integrity holds here:** `SELECT count(*) WHERE filing_date < period_end` → **0**. Median filing lag 44 days, p90 76 days. This is the one dataset whose point-in-time column is real.

Join to prices: `fund ISINs 2,291 · with an L2 series 2,285 · L2 ISINs with no fundamentals 1,064`.

### 3e. Identity master

```sql
SELECT status, primary_exchange, count(*) FROM security_master GROUP BY 1,2 ORDER BY 3 DESC;
-- ACTIVE|BSE 2732 · ACTIVE|NSE 2388 · DELISTED|BSE 2305 · SUSPENDED|BSE 1164 · DELISTED|NSE 6 · SUSPENDED|NSE 3
```
- **8,598 ISINs** total. L1 `prices_raw` holds **14,023 distinct ISINs** — the master knows 61% of what the price lake has traded.
- **Symbol history: 11,359 rows over 8,598 ISINs**, `valid_from` 1995-02-08 → 2026-09-06, 489 closed intervals.
  - By source: `BSE/bse_scrip_master 8,473 rows / 8,473 ISINs` (one row each — no history), `NSE/nse_equity_list 2,397 / 2,397`, `NSE/nse_symbol_change 489 rows / 400 ISINs`.
  - **Symbol-change events: 400 ISINs** have >1 symbol on an exchange (`SELECT count(*) FROM (SELECT isin,exchange FROM symbol_history GROUP BY 1,2 HAVING count(DISTINCT symbol)>1)`).
- **ISIN-change events: 399** (`isin_lineage`), all `detected_by = L1_CONTIGUITY`: 276 `CORROBORATED`, 123 `DERIVED`. Range 2016-09-14 → 2026-09-01.
- `exchange_listing`: NSE rows carry `listing_date` (1995-02-08 → 2026-08-06); **every BSE row has NULL listing_date** and **zero rows of any exchange carry a `delisting_date`**.
  ```
  BSE|ACTIVE    4995 | 4995 isins | listing_date NULL       | delisted 0
  NSE|ACTIVE    2397 | 2397 isins | 1995-02-08 → 2026-08-06 | delisted 0
  BSE|DELISTED  2311 | 2311 isins | listing_date NULL       | delisted 0
  BSE|SUSPENDED 1167 | 1167 isins | listing_date NULL       | delisted 0
  ```

> ⚠️ `security_master.first_seen_date` ranges only **2026-08-08 → 2026-09-06** — it is the date the master was *built*, not the date the security first existed. `dataplatform/identity/ingest.py:5-8` confirms the input (`EQUITY_L.csv`) is *"a snapshot with no history in it whatsoever."* Any survivorship-bias correction that leans on `first_seen_date`/`last_seen_date` will be wrong.

---

## 4. Lineage and rebuildability

L0 inventory (`du -sh data/L0/*`, 6.9G total):

| L0 source | size | year dirs | feeds |
|---|---|---|---|
| `nse_bhavcopy_legacy` | 154M | 2010, 2016–2024 | L1 `prices_raw` (NSE, pre-cutover) |
| `nse_bhavcopy_udiff` | 94M | 2024–2026 | L1 `prices_raw` (NSE, UDiFF era) |
| `bse_bhavcopy_legacy` | 208M | 2016–2024 | L1 `prices_raw` (BSE) |
| `bse_bhavcopy_udiff` | 411M | 2024–2026 | L1 `prices_raw` (BSE) |
| `nse_sec_bhavdata_full` | 462M | 2019–2026 | `deliv_qty`/`deliv_pct` columns |
| `nse_mto` | 50M | 2016–2019 | `deliv_qty`/`deliv_pct` (pre-2019-09-30 era) |
| `bse_corp_actions` | 59M | 2016, 2026 | PG `corporate_actions` |
| `nse_corp_actions` | 6.9M | 2016–2026 | PG `corporate_actions`, `filed_against_isin` |
| `bse_scrip_master` | 3.7M | 2026 | PG `security_master`, `exchange_listing` (BSE) |
| `nse_xbrl_filing` | **5.4G** | 2018–2026 | L1 `pit_fundamentals` |
| `nse_financial_results_index` | 110M | 2016–2026 | discovery index for the above |
| `nse_integrated_filing_index` | 21M | 2025–2026 | (not yet feeding an L1 dataset) |
| `nifty_index_constituents` | 252K | 2026 | **nothing — L1 `index_constituents` does not exist** |

> ⚠️ The `nse_bhavcopy_legacy/2010` directory holds **2 files** (`find data/L0/nse_bhavcopy_legacy/2010 -type f | wc -l` → 2). Per-year file counts: `2016:164, 2017:496, 2018:492, 2019:490, 2020:502, 2021:496, 2022:496, 2023:492, 2024:250`. **The L0 lake is itself a 10-year lake.** 2006–2015 cannot be rebuilt from L0 because L0 does not have it — it would require a fresh fetch campaign.

**Rebuild drivers — all implemented, all offline:**

| Derived store | Driver | Cite |
|---|---|---|
| L1 `prices_raw` from L0 | `uv run python -m dataplatform.ingest.price_rebuild --from … --to …` | `dataplatform/ingest/price_rebuild.py:1-3` |
| L1 `prices_raw` (fetch path) | `dataplatform.ingest.backfill` | `backfill.py:1-5` |
| PG `corporate_actions` + `adjustment_factors` | `dataplatform.ingest.corp_actions_backfill` | `corp_actions_backfill.py:1-8` |
| PG `isin_lineage` → CA replay → reconcile → L2 rebuild → L2 fill (5 stages, all from L0/L1) | `dataplatform.identity.lineage_rebuild` | `identity/lineage_rebuild.py:1-16` — *"What it does not do: fetch."* |
| L2 `prices_adjusted` first-time fill | `uv run python -m dataplatform.store.l2_fill` | `store/l2_fill.py:1-13` |
| L1 `pit_fundamentals` | `dataplatform.ingest.fundamentals_backfill` (two-phase, needs network for new XBRL) | `fundamentals_backfill.py:1-12` |
| PG identity master | `dataplatform.identity.ingest`, `identity_refresh`, `bse_scrip_refresh` | `identity/ingest.py:1-8` |
| PG schema | `dataplatform.store.migrate` | `store/migrate.py` |

**Verdict: REBUILDABLE today, for the window L0 covers (2016-09 →).** Everything derived can be regenerated offline from `data/L0` without a single request, except `pit_fundamentals` (whose XBRL payloads *are* in L0 at 5.4G, so the parse stage is offline; only new filings need the network).

Full driver list (`grep -rln "__main__"`):
```
analyst/cases/cli.py                      dataplatform/ingest/backfill.py
backtest/forecast_run.py                  dataplatform/ingest/bse_scrip_refresh.py
backtest/run.py                           dataplatform/ingest/calendar.py
backtest/sweep.py                         dataplatform/ingest/constituents_ingest.py
backtest/verdict.py                       dataplatform/ingest/corp_actions_backfill.py
dataplatform/archives/__main__.py         dataplatform/ingest/fundamentals_backfill.py
dataplatform/archives/publisher.py        dataplatform/ingest/identity_refresh.py
dataplatform/identity/ingest.py           dataplatform/ingest/price_rebuild.py
dataplatform/identity/lineage_rebuild.py  dataplatform/ingest/source_register.py
dataplatform/scheduler/__main__.py        dataplatform/store/l2_fill.py
dataplatform/store/migrate.py
```

---

## 5. Quality signal

**What `dataplatform/quality/` checks — three registered rules:**

| check_name | severity | what it flags | cite |
|---|---|---|---|
| `unexplained_move` | **ERROR** | close-to-close move > 20% with no CA and no circuit band to explain it | `quality/rules/unexplained_move.py:40,57-59` |
| `cross_exchange_divergence` | WARN | NSE vs BSE closes diverge beyond threshold, both legs liquid (thin-print gated by `min_turnover`/`thin_fraction`) | `quality/rules/cross_exchange.py:68,101-103` |
| `quarantine_step_change` | WARN | a step change in quarantined row counts per session/reason | `quality/quarantine.py:289,306-307` |

Plus `quality/gaps.py` — the L0↔L1 presence reconciler behind `/status/gaps`, classifying every missing `(source, date)` pair against the trading calendar, and `ca_reconciliation` (`corpactions/reconcile.py:99`), which is not a sentinel rule but writes to the same table.

> ⚠️ **The sentinel has never run.** All 21,154 `quality_flag` rows are `check_name = 'ca_reconciliation'`:
> ```sql
> SELECT check_name, severity, count(*), count(*) FILTER (WHERE NOT resolved) FROM quality_flag GROUP BY 1,2;
> -- ca_reconciliation | WARN  | 12622 | 241
> -- ca_reconciliation | ERROR |  8532 | 162
> ```
> Zero rows for `unexplained_move`, `cross_exchange_divergence` or `quarantine_step_change`. The three rules the quality module actually implements have produced no findings, ever — so **the price data has never been quality-checked**.

Quality flags per year (`GROUP BY year(logical_date)`):
```
2000: 69     2007: 527   2014: 911   2021: 920
2001: 445    2008: 762   2015: 900   2022: 982
2002: 80     2009: 729   2016: 953   2023: 1039
2003: 26     2010: 830   2017: 867   2024: 1500
2004: 36     2011: 855   2018: 888   2025: 2370
2005: 21     2012: 845   2019: 926   2026: 1959
2006: 34     2013: 903   2020: 777
```

**What the status API reports** (`dataplatform/status/api.py`): `/health` (heartbeat age, 503 when stale), `/status/sync` (per-date source states + the `is_green` interlock verdict), `/status/sources` (last success, lag, failure streak), `/status/gaps`, `/status/quality` (open flags), `/status/quarantine`, `/archives`, `/archives/download`.

> ⚠️ **The status API is DOWN.** `docker logs trading-platform-app-1` shows a 60-second crash loop:
> ```
> [error] migrate.failed  error="database has migrations this checkout does not:
>   ['0005','0006','0007','0008','0009','0010']. Running an older checkout against a
>   newer database would corrupt it."  stage=schema
> ```
> `curl http://127.0.0.1:8000/health` → no response. The image is stale relative to the repo. **Nothing is currently reading or serving the status surface.**

**Is anything red right now? YES.**

```sql
SELECT severity, count(*) FROM quality_flag WHERE NOT resolved GROUP BY 1;
-- ERROR | 162      WARN | 241
SELECT severity, min(logical_date), max(logical_date), count(DISTINCT logical_date) FROM quality_flag WHERE NOT resolved GROUP BY 1;
-- ERROR | 2009-10-09 | 2026-09-04 | 155
-- WARN  | 2001-05-10 | 2026-09-23 | 231
```
**155 distinct dates carry an unresolved ERROR flag**, including the most recent session, 2026-09-04. `sync_state.py:729` — WARN/INFO are informational, **only ERROR blocks quality-green** — so those 155 dates are red to `is_green` and a decision loop would journal `SKIPPED_DATA_RED` on each. All are `RATIO_MISMATCH` between BSE and NSE dividend terms; sample:
```
2026-09-04 | INE529A01010 | BSE says "Special Dividend - Rs. - 86.5000" (amount 86.5)
                          | NSE says "Interim Dividend Rs 7.50 & Special Dividend Rs 86.50" (amount null)
2026-08-21 | INE619A01035 | BSE says "Interim Dividend - Rs. - 2.3000" (amount null after parse)
                          | NSE says "Interim Dividend - Re 0.80 Per Share" (amount 0.8)
```

Open ERROR flags by date, latest 15: `2026-09-04:1 · 2026-08-21:1 · 2026-08-07:1 · 2026-07-24:1 · 2026-07-17:1 · 2026-07-10:1 · 2026-06-30:1 · 2026-06-18:1 · 2026-06-05:1 · 2026-02-12:2 · 2026-02-04:1 · 2026-01-16:1 · 2025-09-04:1 · 2025-08-07:1 · 2025-08-05:1`.

Also: **2,960 FAILED `sync_state` rows** — 2,956 `nse_xbrl_filing` unit failures (`parse failed: … no results column covers <period>`), plus `bse_bhavcopy_legacy 2021-12-29` (27-field row against a 14-field header), `bse_corp_actions` unit 517380 (`ValidationError: RatioTerms.new_shares`), `nse_financial_results_index 2026-04-01` (JSON array), and `nifty_index_constituents/niftyprivatebank` (*"body is markup, not CSV"*).

`scheduler_heartbeat` has **0 rows** → `/health` would return 503 even if the app were up.

### The `knowable_date` degeneracy

```sql
SELECT min(knowable_date), max(knowable_date), count(DISTINCT knowable_date),
       count(*) FILTER (WHERE announcement_date IS NOT NULL) FROM corporate_actions;
-- 2026-09-07 | 2026-09-07 | 1 | 0

SELECT knowable_date, count(*) FROM corporate_actions GROUP BY 1 ORDER BY 2 DESC LIMIT 8;
-- 2026-09-07 | 47887

SELECT min(recorded_at), max(recorded_at) FROM corporate_actions;
-- 2026-09-07 03:34:24.895771+00 | 2026-09-07 06:09:47.497795+00

SELECT source, min(recorded_at)::date, max(recorded_at)::date,
       count(*) FILTER (WHERE announcement_date IS NOT NULL),
       count(*) FILTER (WHERE record_date IS NOT NULL),
       count(*) FILTER (WHERE filed_against_isin IS NOT NULL)
FROM corporate_actions GROUP BY 1;
-- bse_corp_actions | 2026-09-07 | 2026-09-07 | 0 | 16009 | 0
-- nse_corp_actions | 2026-09-07 | 2026-09-07 | 0 |  7921 | 2556
```

All 47,887 rows carry `knowable_date = 2026-09-07`; `announcement_date` is NULL on every row of both sources. The whole table was ingested today (03:34–06:09 UTC) and `knowable_date` is stamped from the injected clock at `dataplatform/ingest/bse/corp_actions.py:214` (`knowable_date=clock.now().date()`), which that module documents as deliberate and conservative at `:19-23`. NSE was supposed to carry `caBroadcastDate` as the real knowable date (`dataplatform/ingest/nse/corp_actions.py:17-19` — *"stored as both `announcement_date` and `knowable_date`"*), but `announcement_date` is NULL on all 12,505 NSE rows, so the broadcast date is not landing and the ingest-date fallback applies there too.

**What it means for PIT-correct backtesting:** invariant #7 says no data with `knowable_date > decision_date` reaches a decision. A backtest that honours it sees **zero corporate actions for every decision date before 2026-09-07**. Either the backtest silently drops the CA-awareness it thinks it has (no split/bonus/dividend context on any historical decision), or, if it joins on `ex_date` instead to get results at all, it is using look-ahead data and invariant #7 is bypassed rather than satisfied. The column is safe in the conservative direction and carries no usable information. This is the largest gap between what the schema promises and what the data supports.

Related: **`adjustment_factors.corporate_action_id` is NULL on all 2,784 rows** — the FK exists (`0001_init.sql:175`, `adjustment_factors_corporate_action_id_fkey`) and is unused, so no adjustment factor can be traced to the action that caused it.

---

## 6. Declared but empty — planned, not had

### Postgres tables with zero rows (8 of 20)

`case_` · `policy_set` · `thesis` · `decision_journal` · `order_` · `token_usage` · `job_run` · `scheduler_heartbeat`

**That is the entire System-2 analyst surface and the entire execution surface.** No case has ever been opened, no thesis written, no policy ratified, no decision journaled, no order staged, no token metered, no job run recorded, no scheduler heartbeat. The two append-only tables the invariants care most about (`decision_journal`, `policy_set`) have their triggers installed and nothing to protect.

`archive_bundle` holds **1 row** — a single bundle for `2024-01-02`, 2 files, 539,382 bytes, published 2026-09-03. One smoke test, not an archive series.

`identity_reconciliation` holds **10 rows**, all `SYMBOL_TO_ISIN` / BSE / `2026-09-06`, **all unresolved**.

### L1/L2 datasets declared in code with no directory on disk

Verified by `[ -d data/L1/<ds> ]`:

```
ABSENT  data/L1/macro_series           (declared store/macro_series.py:59)
ABSENT  data/L1/fo_contracts           (declared store/fo_aggregates.py:67)
ABSENT  data/L2/fo_aggregates          (declared store/fo_aggregates.py:70)
ABSENT  data/L1/announcements          (declared ingest/announcements.py:92)
ABSENT  data/L1/index_constituents     (declared ingest/indices.py:126)  ← L0 payload EXISTS, unprocessed
ABSENT  data/L1/benchmark_tri          (declared ingest/indices.py:127)
ABSENT  data/L1/shareholding           (declared ingest/shareholding.py:91)
ABSENT  data/L1/fii_dii_flows          (declared ingest/nse/fii_dii.py:95)
ABSENT  data/L1/news                   (declared ingest/news.py:62)
ABSENT  data/L1/deals                  (declared ingest/nse/deals.py:103)
ABSENT  data/RESTATED  (screener_fundamentals, declared store/restated.py:70)
```

`data/` contains exactly `L0/`, `L1/`, `L2/`, `.host-lease/`. **11 declared datasets, zero bytes.** Notably `index_constituents` and `benchmark_tri` are absent, which means **there is no benchmark series in the lake** — nothing to measure a strategy's excess return against.

---

## 7. Loudest findings

1. **The lake is 10 years, not 20.** L1 prices begin 2016-09-01; L0 begins there too (the 2010 directory has 2 files). 2006–2015 is not a rebuild away — it is a fetch campaign away.

2. **`corporate_actions.knowable_date` is degenerate — every one of the 47,887 rows says `2026-09-07`.** See §5 for the full query set. A PIT-correct backtest sees zero corporate actions before today.

3. **`adjustment_factors.corporate_action_id` is NULL on all 2,784 rows.** No factor can be traced to the action that caused it.

4. **The three quality rules that check *prices* have never produced a finding.** Every flag in the table came from CA reconciliation. Price data is unverified.

5. **The status API is in a crash loop** on a stale image; `scheduler_heartbeat` is empty. There is no live monitoring surface.

6. **155 dates are currently RED** on open ERROR flags, including the latest session.

7. **No delistings after 2003, no `delisting_date` anywhere, and `first_seen_date` is the master's build date.** Nothing in the store supports a survivorship-bias-free universe reconstruction.

8. **L2 is NSE-only and 388 historical NSE-EQ ISINs are unmaterialized.** Current names (2,646 on 2026-09-01) are complete; the back-book is not. `dataplatform.store.l2_fill` is the driver for this and it is implemented.

## UNKNOWN

- **Whether the 388 missing L2 ISINs are all genuinely fillable.** `l2_fill.py:6` says retired ISINs are deliberately skipped (their bars live in the survivor's stitched partition), so some fraction of the 388 is correct behaviour, not a gap. I did not join the 388 against `isin_lineage.predecessor_isin` to split them — that needs a write-free but non-trivial cross-store query I judged outside a read-only inventory.
- **Live `/status/*` output.** The app container will not start; I did not restart it (read-only mandate), so every status figure here is read from the underlying tables rather than the API that serves them.
- **Whether `nse_xbrl_filing`'s 2,956 FAILED units represent lost fundamentals or duplicate/irrelevant documents.** The errors are all `no results column covers <period>`, which reads like a parser-era mismatch on old filings, but confirming would mean parsing the L0 payloads.
