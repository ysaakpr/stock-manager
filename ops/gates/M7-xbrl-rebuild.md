# M7.3 — XBRL fundamentals path, rebuilt against real filings

**Date:** 2026-09-03 · **Trigger:** owner, "Fix the M7 XBRL and rebuild this for the real filing
data" (HUMAN_DECISIONS, M10.4, option A) · **Status:** rebuilt and verified against captured data

M7's point-in-time fundamentals path was a false-DONE: built and gate-passed against hand-built
fixtures modelled on the in-bse-fin taxonomy, it could not ingest a single real NSE filing. This
records what the real format is, what was wrong, and what the fix is evidenced by.

## What the real format is

The mistake underneath all six defects was a wrong model, not six bugs. A results filing is **not**
one period per document. It is a **column-by-column transcription of the published results table**,
and the announcements index says which column a given filing is.

| Property | What the fixtures assumed | What real filings do |
|---|---|---|
| Periods per document | One | Two columns — `OneD` (the quarter) and `FourD` (the cumulative period) — *both usually carrying the same `xbrli:period`*. A column's real period is the `DateOfStartOfReportingPeriod`/`DateOfEndOfReportingPeriod` facts reported **inside** it. |
| Which period is "this filing" | The document's own period elements | The index entry's `(fromDate, toDate)`. One document is linked by more than one entry: Schaeffler's is named by both a Quarterly entry (01-Oct→31-Dec) and an Annual one (01-Jan→31-Dec). |
| Entity identity | Entity identifier **is** the ISIN | Entity identifier is the NSE **symbol** (`scheme=…/NSESymbol`). Ind-AS filings carry no ISIN element; the banking taxonomy's can be stale. |
| Segments | `xbrli:segment` + typed member + `SegmentName` | `xbrli:scenario` + explicit member on `in-bse-fin:ReportableSegmentsAxis` + sibling `DescriptionOfReportableSegment`. The column's own `SegmentRevenue` is the cross-segment **total**, not a segment. |
| P&L vocabulary | One set of eight element names | Two, chosen by `link:schemaRef`. Four of the eight guessed names (`TotalIncome`, `TotalExpenses`, `BasicEarningsPerShare`, `DilutedEarningsPerShare`) exist in **no** real filing. |
| Feed vocabularies | `Standalone`, `Unaudited` | `Non-Consolidated`, `Un-Audited`. Across a captured 3,816-record index, `Standalone` never appears. |

## The six defects

The first four are the ones the backlog named; the last two the rebuild surfaced.

1. **Discovery rejected every real entry.** `consolidated` is `Consolidated` (1,630) /
   `Non-Consolidated` (2,186) and never `Standalone`. Fixed: `_NATURE_SYNONYMS` maps the feed's
   spelling onto the document's, which stays the stored value.
2. **The parser read the ISIN from the document.** It took the entity identifier as an ISIN and
   validated it against `ISIN_PATTERN`, so every real filing raised. Fixed: the ISIN comes from the
   index entry (invariant #2, via D2) and the **symbol** is cross-checked. The document's ISIN
   element is never read — HDFCBANK's says `INE040A01034` where the index says `INE040A01018`, and
   SBIN's `INE062A01020` against the index's `INE062A01012`.
3. **Multi-period documents.** Filtering contexts by `period_end` alone admitted the cumulative
   column, whose end date equals the quarter's. Fixed: the full `(period_start, period_end)` must
   match, and no-match / ambiguous-match are both hard `ParseError`s naming what the document offers.
4. **"Duplicate same-period contexts".** Not duplicates — `OneD` and `FourD` are *different
   columns* that happen to share an `xbrli:period`. Fixed by the column model: the period is read
   from each column's own in-context reporting-period facts.
5. **`Un-Audited` read as "did not say"** (new). `_optional_audited` matched `unaudited`, so all
   3,598 unaudited entries in a captured index returned `None`. Fixed: hyphens squashed before
   comparison. `audited` is also per-column, not per-document — Schaeffler's annual column is
   audited while its Q4 column is not.
6. **`read_latest` collapsed a quarter into a year** (new, in the store). Its restatement-collapse
   key was `(isin, period_end, nature, concept, segment)`, so a December-year-end company's Q4 and
   its full year — same end date, separately announced, both legitimately stored — looked like two
   versions of one fact and the annual figure was silently dropped. Fixed: `period_start` is part
   of the key. This would have quietly corrupted M10.5/M10.6 signals for every Dec-year-end name.

A seventh real-world state, previously unhandled: 18 of 3,816 announcements state `xbrl` as the
archive path ending in a bare `-` — the announcement exists, the document does not, and the
register forbids constructing the URL. `FilingIndexEntry.is_actionable` reports it and the M10.4
runner counts it as `skipped_no_document` on the coverage report rather than dropping the row.

## Evidence

**Fixtures are captured, never authored** (`tests/fixtures/xbrl/README.md`). 15 real XBRL documents
and 2 real index responses, fetched 2026-09-03 through the project's own `Fetcher`. The register's
recorded `sample_sha256` for `nse_xbrl_filing`
(`89651604…5b85b84e`, 19,935 bytes) matches the captured
`INDAS_121277_1705282_30072026051753.xml` byte for byte, so the register's verification evidence was
real all along — only the `parse_check` describing the format was wrong. Both rows' `parse_check`
now state the column model, the symbol identity, the segment axis and the two vocabularies.

Coverage of the fixture set: both taxonomy vocabularies (Ind-AS, NBFC entry point, Banking), both
natures, quarterly and annual entries over the same documents, single- and multi-segment
disclosure, a filing reporting zero revenue, a segment withdrawn by a re-filing, and a real
restatement.

**All 17 actionable entries parse: 136 company facts and 35 segment facts, no failures.** Headline
figures were checked against the companies' own published results:

| Company | Taxonomy | Revenue (Q3 FY25) | PAT | Segments |
|---|---|---|---|---|
| RELIANCE | Ind-AS | ₹243,865 cr | ₹21,930 cr | 5 |
| SBIN (consolidated) | Banking | ₹124,654 cr interest earned | ₹19,175 cr | 7 |
| HDFCBANK | Banking | ₹76,007 cr interest earned | ₹16,736 cr | 4 |
| ITC | Ind-AS | ₹20,350 cr | ₹5,013 cr | 5 |
| GRASIM | Ind-AS | ₹34,793 cr | ₹1,844 cr | 5 |
| BAJFINANCE | Ind-AS (NBFC) | ₹15,371 cr | ₹3,706 cr | 0 |
| KANANIIND | Ind-AS | ₹0 | ₹0.18 cr | 0 |

Two structural checks the fabricated fixtures could not have supported:

* **Grasim's segments reconcile against the filing's own arithmetic.** The five segment revenues sum
  to the column's `SegmentRevenue` total (351,546.8), which nets to `RevenueFromOperations`
  (347,928.5) after `InterSegmentRevenue` (3,618.3).
* **Values are absolute rupees.** Reliance's filing says `Crores` and tags 2,438,650,000,000 for a
  quarter its statement prints as 243,865 crore. Nothing scales by the rounding label.

**The restatement acceptance is proved on a real restatement.** V.S.T Tillers filed its 31-Dec-2024
quarter on 11-Feb-2025 reporting ₹2,191.0 cr of revenue, then re-filed on 30-Jul-2026 reporting
₹219.1 cr — the original overstated the quarter by exactly 10×, corrected seventeen months later.
Both versions are physically present; `read_pit(2025-06-01)` returns the wrong original figure the
market actually had, and `read_latest` supersedes only once the correction is knowable. That is
invariant #7 demonstrated rather than asserted — an implementation that "helpfully" corrected
history fails the test.

## Acceptance criteria (M7.3)

| Criterion | Verdict |
|---|---|
| real XBRL fixtures parse with `(period_end, filing_date)` on every datum | **PASS** — 15 captured documents, 17 entries, 171 facts; every fact carries both dates and `filing_date > period_end`. The two dates come from two places, proved by one period end with two filing dates 17 months apart. |
| standalone and consolidated distinguished; segment disclosures extracted where present | **PASS** — both natures parse to different bottom lines; 35 segment facts across 5 multi-segment filings, names as filed, reconciling to the filings' own totals. |
| a later filing restating an earlier period is stored as a new record, not an overwrite | **PASS** — on the real V.S.T Tillers restatement above, and on a real segment withdrawal (Stanley Lifestyles). |

## Verification

```bash
uv run pytest tests/unit/test_xbrl.py tests/unit/test_fundamental_bc.py tests/unit/test_fundamentals_backfill.py -q
```

65 + 20 + 10 = 95 tests, all green, all offline (sockets are monkeypatched to raise, B8).
`make check` is clean apart from one pre-existing unrelated failure,
`test_scheduler_registry.py::test_the_default_registry_holds_the_placeholder_eod_job`, which has
been red since M10.2 added `constituents_snapshot` to the default registry (on the backlog).

## Blast radius

* `dataplatform/ingest/xbrl/{models,parser,discovery,__init__}.py` — rewritten/corrected.
  `parse()` now takes the `FilingIndexEntry` rather than loose scalars, which is what makes
  "the index selects the column" impossible for a caller to bypass. `Filing` gains `symbol` and
  `taxonomy`. `FilingIndexEntry.xbrl_url` is now optional, gated by `is_actionable`.
* `dataplatform/store/pit_fundamentals.py` — the `read_latest` collapse key (defect 6).
* `dataplatform/ingest/fundamentals_backfill.py` (M10.4) — the new parse call, plus
  `skipped_no_document` on the coverage report. No other logic change; the runner's resume,
  checkpoint and park behaviour is untouched.
* `analyst/monitor/fundamentals.py` (M7.4, BC1) — **unchanged**. The snake_case concept keys and
  `SEGMENT_CONCEPT` are the stable interface, and both survived the rewrite deliberately: only the
  element-name → key mapping was wrong, never the keys themselves.

## What this does not do

* **The PIT store still holds no real data.** This fixes the path; filling it is the M10.4 backfill
  campaign, which is `NEEDS_GO` under B1 (thousands of per-filing fetches). 25 requests were made
  here — 3 index responses and 22 filing documents, of which the 15 most informative were kept —
  which is the "verify + sample" fetching B1 permits explicitly.
* **`total_expenses` is not comparable across the two vocabularies.** For a bank it maps to
  `ExpenditureExcludingProvisionsAndContingencies`, which excludes provisions and contingencies; an
  Ind-AS `Expenses` does not. Bottom-line concepts (`profit_before_tax`, `profit_after_tax`, EPS)
  are directly equivalent, and `revenue_from_operations` is a bank's `InterestEarned`. Both
  judgements are stated at `BANKING_CONCEPTS` so a ratio built on them inherits them knowingly.
* **Only P&L concepts are extracted.** Balance-sheet items for ROE/ROCE/P-B are not in a results
  filing at all; segment results/assets/liabilities *are* and are now cheap to add (backlog).
* **History accumulates forward only.** Unchanged and deliberate: this store is never backfilled
  from a restated source.
