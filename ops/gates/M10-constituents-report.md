# M10.1 — index-constituents ingest (sector classification map)

**Task:** M10.1 — Index-constituents ingest — sector classification map
**Module:** D1 · **Autonomy:** AUTO · **Depends on:** M3.9 (parser, immutable writer,
`membership_asof`, single-index driver)
**Deliverables:** `dataplatform/ingest/constituents_ingest.py`,
`tests/unit/test_constituents_ingest.py`, this report.

## What this task landed

M3.9 built everything to ingest *one* index list — the five-column CSV parser
(`Company,Industry,Symbol,Series,ISIN`), the immutable monthly writer, `membership_asof`, and the
single-index driver `ingest_constituents` (fetch → L0 → parse → L1 → sync). What it never had was
the runner that *fills the map*: a sweep over the whole set of lists the analyst reasons about.
This task is that runner, `run_constituents_ingest`.

For one `as_of` snapshot date it drives every configured `IndexSpec` through M3.9's single-index
driver, once, and returns a `CoverageReport` partitioning the outcome per slug into **published**,
**skipped** (already in L1 — the resume path) and **parked** (a failure, tagged with an enumerated
`ParkCause`). One slug's failure never aborts the others.

### The one M3.9 fix inside this task's blast radius

M3.9's `ingest_constituents` filed its sync-state transitions under the bare register id
`nifty_index_constituents`. That is fine for one list, but a whole sweep files ~17 lists under one
logical date, and a sync row is keyed `(source, logical_date)` — so the second slug's `begin`
collided with the first's terminal `PUBLISHED` row (`PUBLISHED → PENDING` is illegal). Fixed by
qualifying the *sync source* per slug (`constituents_state_source(slug)` →
`nifty_index_constituents:<slug>`) while the *fetch* keeps the bare register id (URL, headers and
the 403 watch are per-endpoint, not per-slug). Each list now files its own sync row, which is also
what M10.2's per-slug journaling builds on. M3.9's own tests are unchanged and still pass.

## Acceptance

1. **Every liquid name resolves to an Industry as-of a date via `membership_asof`.** After a sweep,
   every constituent read back through `membership_asof(slug, date)` carries a non-empty Industry,
   and a name is findable through its sector list by ISIN (invariant #2). Proved by
   `test_every_liquid_name_resolves_to_an_industry_asof_a_date`.
2. **The broad list plus ≥8 sectoral lists are ingested and queryable.** The shipped default set is
   2 broad + 15 sectoral/thematic lists; the coverage bar is 1 broad + 8 sectoral. A sweep over the
   test fixtures covers **1 broad + 9 sectoral/thematic** — see the table below.
3. **Resumable, reports coverage, and a gated/failed slug parks with an enumerated cause.** A second
   sweep over already-ingested slugs re-fetches nothing (the transport is never touched) and reports
   them skipped; an HTML soft-404 (200 + markup) and a 403 both park **GATED**, a 404 parks
   **FETCH_FAILED**, and the good slugs still publish. `ParkCause` enumerates
   GATED / FETCH_FAILED / PARSE_FAILED / IMMUTABLE_CONFLICT / UNKNOWN.

## Hard limit for the caller (§4.1, register `pit_notes`)

Each CSV is **always "as of today"** — niftyindices publishes no historical constituents download.
This runner ingests the **current** snapshot only. Accumulating dated snapshots into real
point-in-time sector history is **M10.2's** job; a static "today" map applied to a past date is
survivorship-biased, which is exactly why `membership_asof` returns *nothing* before the first
snapshot rather than today's list. The constituents endpoint is VERIFIED at C.1 (unlike the
session-gated TRI, which stays FAILED in the register).

## Coverage from the fixture sweep (as-of 2026-09-01)

**1 broad + 9 sectoral/thematic queryable (bar: 1 broad + 8 sectoral) — PASS.**
10 published, 0 skipped, 0 parked.

| Slug | Index | Category | Status | Rows |
| --- | --- | --- | --- | --- |
| nifty500 | NIFTY 500 | broad | published | 16 |
| niftybank | NIFTY BANK | sectoral | published | 5 |
| niftyit | NIFTY IT | sectoral | published | 5 |
| niftyauto | NIFTY AUTO | sectoral | published | 5 |
| niftypharma | NIFTY PHARMA | sectoral | published | 4 |
| niftyfmcg | NIFTY FMCG | sectoral | published | 5 |
| niftymetal | NIFTY METAL | sectoral | published | 4 |
| niftyrealty | NIFTY REALTY | sectoral | published | 4 |
| niftymedia | NIFTY MEDIA | sectoral | published | 3 |
| niftyenergy | NIFTY ENERGY | thematic | published | 5 |

The fixture set is a representative slice; the shipped `DEFAULT_INDEX_SET` (2 broad + 15
sectoral/thematic, the ~15-20 CSVs the spec names) is what the operator CLI
(`python -m dataplatform.ingest.constituents_ingest --report <path>`) sweeps against the live
endpoint. `render_coverage_markdown` regenerates this table from the same `CoverageReport` the
runner returns, so the gate artefact is produced from the run, not hand-kept.

## Verification

```
uv run pytest tests/unit/test_constituents_ingest.py -q   # 10 passed
uv run pytest tests/unit/test_indices.py -q               # M3.9 unchanged, 29 passed
```
