# Gap report — 2016-09-01 → 2026-09-05

The first full-history run of D7's gap report since the fundamentals campaign, and the first that
completes at all: until migration 0008 (`9a0a3f9`) the default scan raised `PathLayoutError` and
`/status/gaps` answered 500. Reproduce with:

```bash
uv run python -c "from datetime import date; from dataplatform.config import get_settings; from dataplatform.store.db import connection; from dataplatform.quality.gaps import GapScanner; c=connection(get_settings()).__enter__(); print(GapScanner(c).report(date(2016,9,1), date(2026,9,5)).summary())"
```

```
2016-09-01..2026-09-05: 7 source(s), 97,445 pair(s)
  74,398 complete · 2,364 explained · 2,409 UNEXPLAINED
  [WEEKEND=2082, HOLIDAY=282, NEVER_ATTEMPTED=8, FAILED=2401]
  L1 unverified for 11 published pair(s)
```

**`fully_explained` is false.** The M1 gate criterion does not hold today, and this is the
enumeration it asks for. 1.1 s over ten years.

---

## The 16 non-XBRL misses, in full

| Date | Source | Reason | What it is |
|---|---|---|---|
| 2016-09-01 | `nse_bhavcopy`, `nse_delivery` | NEVER_ATTEMPTED | One session before the backfill's first date. A range boundary, not a hole — the lake starts 2016-09-02. |
| **2020-07-13** | `nse_bhavcopy`, `nse_delivery` | **FAILED** | `TIMESTAMP is '13-Jul-20', which is not a DD-MON-Y` — a third timestamp variant inside the legacy era. **The L0 payload is present and healthy (71,297 bytes).** |
| **2021-02-16** | `nse_bhavcopy`, `nse_delivery` | **FAILED** | `row is not a valid price row` at line 27. **L0 payload present (74,595 bytes).** |
| 2021-11-04 | `nse_delivery` | FAILED | The Diwali Muhurat session: NSE served `sec_bhavdata_full_04112021.csv` containing 2021-11-03's rows. The parser's own date check caught it — working as designed, but the session has no delivery. |
| 2022-08-08 | `nse_delivery` | FAILED | `payload is not UTF-8 text at byte 22` — the archive served a corrupt body. |
| 2026-04-01 | `nse_financial_results_index/Annual/2026-06-30` | FAILED | `JSON array` parse failure on the Annual chunk. |
| 2026-09-02/03/04 | `nse_bhavcopy`, `nse_delivery` | NEVER_ATTEMPTED | Three sessions never fetched. The lake ends 2026-09-01 and the scheduler reports `NEVER_RAN`. |
| 2026-09-03 | `nifty_index_constituents/niftyprivatebank` | FAILED | `body is markup, not CSV` — the site answered a bad path with its Angular shell and a 200. Correctly refused. |

The two **bold** rows are audit finding N2: two full trading sessions absent from `prices_raw`
whose bytes we already hold.

**Both are closed.** P1.1 froze the two payloads as fixtures, taught the legacy parser the
two-digit year and the placeholder ISIN, and re-derived both partitions from L0 — no fetching.
`prices_raw` now holds 2,471 partitions and `expected_sessions` reports nothing absent:

| | rows | with delivery |
|---|---|---|
| 2020-07-13 | 2,001 | 1,083 |
| 2021-02-16 | 2,025 | 1,217 |

`ABFRLPP1` (series `E1`, ISIN `DUMMY`) is in `prices_raw_quarantine` for 2021-02-16 under the new
`isin_not_published` reason — refused, not dropped, and not a reason to lose the session.

P1.2 also landed, so a re-run of this report now separates the two questions an operator actually
has. `L0_PRESENT_L1_ABSENT` means the bytes are on disk and the fix is a parser change plus a
re-derive; plain `FAILED` means a re-fetch. Today that splits as **2 · L0_PRESENT** (the two
delivery sessions below) against **2,394 · FAILED**, and the split is deliberately conservative
for unit-keyed sources: a month directory full of *other* filings is not evidence about the one
that failed, so the 774 filings whose documents were never fetched stay in the re-fetch column
where they belong.

## The 2,392 stuck filings

| Class | Filings | Fixable by |
|---|---|---|
| `no results column covers <period>` — integrated feed period guess | 1,463 | Recorded already (`e0440e5`); the Jan–Mar 2025 quarter is where the PIT store stops |
| `MissingPayloadError: no L0 payload for <doc>` | **774** | A scoped re-fetch of a known list — the bytes were never fetched, so no re-derivation reaches them (P7.1) |
| `index says this filing is X but the document states Y` — D2 symbol history | 156 | Identity history (gap-plan Action 3). The backlog records this as "21 filings, 2 companies"; it is 156 |

Range 2018-05-25 → 2025-04-28. 2,392 distinct filings across 2,393 (source, date, unit) rows.

## What the report could not check

`L1 unverified for 11 published pair(s)` — pairs claiming PUBLISHED whose L1 dataset the presence
probe has no directory for. Counted, never silently passed: "we found nothing wrong" and "we
checked nothing" do not render the same.

## What is *not* in this report, and why

Only 7 sources have ever written a `sync_state` row, so those are the 7 the default scan covers.
The other 22 registered sources have never been attempted and therefore have no rows to be missing
— which the report says in `GapReport.sources` rather than folding into a clean bill of health.
Naming them explicitly (`?source=`) would enumerate their whole era as NEVER_ATTEMPTED, which is
true and is a scope question for the owner, not a defect.
