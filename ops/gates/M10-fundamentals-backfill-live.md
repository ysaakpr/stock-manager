# M10.4 — Fundamentals backfill: live dry-run, era sampling, and verification

**Date:** 2026-09-03 · **Supersedes the PARKED status in
[`M10-fundamentals-backfill-report.md`](M10-fundamentals-backfill-report.md)**, whose blocking
dependency (the M7.3 XBRL path, [`M7-xbrl-rebuild.md`](M7-xbrl-rebuild.md)) is fixed.

The runner now ingests real NSE filings across every format era the feed serves, verified against
the bytes it fetched. The full 10-year campaign remains `NEEDS_GO` under B1; everything here is the
"verify + sample" half that B1 authorises.

## 1. Dry-run against the real feed

```bash
uv run python -m dataplatform.ingest.fundamentals_backfill --from 2024-01-01 --to 2026-09-03 --dry-run
```

22 index chunks planned (11 quarters × Quarterly and Annual), every URL fully substituted, no
fetch performed. Confirmed the date filter is on the **broadcast** date, not the reporting period,
and that it returns every reporting period disseminated in the window — which is what makes a
forward-walking PIT backfill possible rather than "latest quarter only".

## 2. Two defects the first live run exposed, in the runner itself

Both fired within a single run and are fixed with regression tests that fail if either is reverted.

1. **The two periods of one chunk shared a checkpoint.** `IndexUnit.state_source` was the bare
   register id and `logical_date` the chunk start, so the Quarterly and Annual chunks of one date
   range collided on one `sync_state` row. Publishing the Quarterly chunk made the Annual chunk
   resume-skip — and the resume path then looked for a payload under the Annual filename that
   nothing had ever fetched. Consequence: **every Annual filing of a backfill silently lost.** The
   key is now `nse_financial_results_index/<period>/<chunk end>`, one-to-one with the L0 filename
   the resume path reads by; a re-run with a different `--chunk-months` or `--to` correctly counts
   as a different chunk.
2. **A resume whose payload was missing crashed the run.** The handler caught
   `(FileNotFoundError, ParseError)`, but `L0Store.ref_for` raises `L0NotFoundError`, which is
   neither — so a decade-long resume died on the single case that handler exists for. Now caught,
   counted as a failed chunk, and named in the coverage report.

Verified by the first run's own data: the IL&FS Transportation **Annual** filing (8 facts) was
absent from the store, and the same document's Quarterly filing was present. After the fix it lands.

## 3. Era coverage: what the feed actually contains

Index-only probe of the whole window, one 12-month chunk per financial year per period:

| Financial year | Announcements | With an XBRL document | Taxonomies present |
|---|---|---|---|
| 2016-17 | 13,399 | **0** | `Non-Ind-AS`, `Ind-AS` |
| 2017-18 | 14,848 | **0** | `Ind-AS New`, `Non-Ind-AS` |
| 2018-19 | 14,464 | 11,354 (78%) | `Ind-AS New`, `Non-Ind-AS` |
| 2019-20 | 13,606 | 11,543 (85%) | + `NBFC-IND` |
| 2020-21 | 14,062 | 14,060 (100%) | all three |
| 2021-22 | 14,606 | 14,396 (99%) | all three |
| 2022-23 | 15,015 | 15,009 (100%) | all three |
| 2023-24 | 16,771 | 16,768 (100%) | all three |
| 2024-25 | 18,277 | 18,262 (100%) | all three |
| 2025-26 | 133 | 44 | `Ind-AS New` |
| 2026-27 (to date) | 16 | 10 | `Ind-AS New` |
| **Total** | **135,197** | **101,446** | |

The 2025-26 and 2026-27 rows are small because this feed's window is keyed on the broadcast date
and the captured lake's own clock sits in 2026 — those are stragglers and amended filings, not a
year's worth of results.

Two facts worth stating plainly, because they bound what this dataset can ever be:

* **The feed does serve ten years of index history** — it is not "recent quarters only", which is
  what the M7.3 spec's "history accumulates forward from now" implied.
* **XBRL documents exist only from about FY2018-19 onward.** All 28,247 announcements in FY2016-17
  and FY2017-18 carry the `-` placeholder instead of a document. So the honest depth of the PIT
  fundamentals store from this source is **FY2018-19 → now, ~101,446 filings** — a property of the
  source, not a policy choice.

## 4. Era sampling: one filing per (financial year × taxonomy × bank flag)

60 real filings sampled across the decade, 57 fetched (3 are 404 at the archive — see §6).

**First pass: 17 parsed, 40 failed.** Five distinct causes, none of them visible in the
2025-2026 documents the rebuilt fixtures came from:

| Cause | Filings | Fix |
|---|---|---|
| `other_than_banks_entry_point_*` — an entire **third vocabulary** (pre-Ind-AS Indian GAAP, non-bank): total income is `Revenue`, bottom line `ProfitLossForThePeriod` | 14 | `Taxonomy.NON_IND_AS` + `NON_IND_AS_CONCEPTS` |
| The 2018-2022 `…_WEB.xml` generation **never declares its column contexts** — facts reference `OneD`/`FourD`, which no `<context>` defines; some declare no `<context>` at all | ~20 | Columns are identified by the facts they carry, not by walking declared contexts (`_shapes` / `_columns`) |
| Those files also state **no per-column reporting period** — only a document-level financial-year header | (same) | The cumulative column's period is the document's financial year (`_declared_period`) |
| Entity identified by **BSE scrip code**, which cannot be compared to a symbol | 4 | Scheme recognised explicitly; cross-check falls to the document's `Symbol` fact |
| A **renamed** company: the filing states the symbol it had when filed | 1 | `parse(known_symbols=…)`, fed the D2 symbol history by the runner |

**Second pass: 57 parsed, 0 parse failures**, across 8 distinct taxonomy entry points
(`Ind-AS_entry_point_2017-03-31` and `_2020-03-31`, `in-bse-fin-2019-03-31` and `-2020-03-31`,
`banking_entry_point_2018-03-31` and `_2019-09-30`, `other_than_banks_entry_point_2018-03-31` and
`_2019-09-30`) and all three vocabularies (Ind-AS 32, Banking 11, Non-Ind-AS 14).

One subtlety cost two attempts and is worth recording, because getting it wrong was silent rather
than loud: the financial-year header is pinned to the `OneD` context but describes the *document*.
On an annual-only filing `OneD` is **zero-filled** and the year's numbers sit in the cumulative
`FourD` — and both columns carry the full concept set, so nothing about their contents
distinguishes them. Reading the header as `OneD`'s own period stored Allahabad Bank's FY19 revenue
as **zero** while parsing "successfully". It now reads ₹16,916 cr against a ₹8,457 cr loss, which
is the published figure.

## 5. Verification: the store against the bytes

Parsing without error proves only that the parser found *something*. Two independent checks prove
it found the right things.

**Internal arithmetic, no outside knowledge required.** A results statement is self-consistent, so
`revenue_from_operations + other_income == total_income` must hold exactly — it is the definition of
the total-income line in all three vocabularies, and a mis-mapped concept or a wrongly-selected
column breaks it immediately (a quarter's revenue against a year's other income does not add up).

* **57/57 filings hold the income identity exactly**, across all three vocabularies.
* `total_income - total_expenses ≈ profit_before_tax`: **45/45** for non-banks. Banks are excluded
  by construction — their `total_expenses` is `ExpenditureExcludingProvisionsAndContingencies`, so
  the difference is *operating* profit and the gap to PBT is the provisions line. That caveat is
  documented at `BANKING_CONCEPTS` and now pinned by a test, so nobody later "fixes" the mapping to
  make the two agree.

**The store against L0.** A separate verifier walks the L0 lake, re-derives every fact from the
raw bytes, and compares against `pit_fundamentals` — scoped to the filings `sync_state` reports
`PUBLISHED`, so a bounded sample's cap is not mistaken for a defect:

```
filings sync_state reports PUBLISHED: 34
pit_fundamentals rows: 323
re-derived from L0:    323
  filing_date > period_end on every row: OK
  every value is a Decimal:              OK
  every row carries an l0_key:           OK
VERIFIED — the store agrees with the L0 bytes
```

Zero missing, zero extra, zero value mismatches, over 21 ISINs.

## 6. Live runs

| Window | Discovered | In universe | Published | Failed | Facts | Notes |
|---|---|---|---|---|---|---|
| 2026-07-01 .. 2026-09-01 | 9 | 8 | 8 | 0 | 64 | Complete, uncapped. Includes one document answering both an Annual and a Quarterly entry. |
| 2019-04-01 .. 2020-03-31 | 13,606 | 7,638 | 22 | 3 | 225 | `--max-filings 25`. The old `_WEB` era. |
| 2025-04-01 .. 2026-03-31 | 133 | 4 | 4 | 0 | 34 | Complete, uncapped. |

Failures are enumerated on each coverage report, never swallowed. Two source-side classes, both
handled rather than worked around:

* **404 at the archive** — the index names a document `nsearchives` does not serve (3 of 57 in the
  era sample). Recorded `FAILED` (retryable) and the run continues.
* **An annual-only document listed under a fourth-quarter entry** (3 of 25 in the FY2019-20 run).
  The parser **refuses** it: the quarter's start is stated nowhere in such a document, so storing
  twelve months of numbers under three would be a silent error nothing downstream could catch. The
  Annual entry for the same document lands normally. The quarter is recoverable later as
  (annual − nine-month cumulative) in a *derived* layer, which is where a computed figure belongs —
  not in a store whose contract is "what the filing said".

A third class is data quality at the filer, and is deliberately **not** repaired: Union Bank's FY18
return entered provisions with the wrong sign, so its own `ProfitLossFromOrdinaryActivitiesBeforeTax`
reads +₹21,092 cr while the same document's EPS reads −69.45. The parser stores what the exchange
published. L0 is immutable and the store's contract is fidelity to it; silently "correcting" a
filer would make the store disagree with its own lineage.

## 7. What is still reserved to the owner

The full campaign — ~101,446 per-filing fetches — is the bulk execution B1 reserves for a human go
(`NEEDS_GO`), unchanged. Everything above cost **33 index requests and 113 document requests**
(22 + 3 + 8 index; 22 fixture captures, 57 era samples, 34 through the live runner), which is the
verify-and-sample scope B1 authorises explicitly — two orders of magnitude below the campaign.

## Verification

```bash
uv run pytest tests/unit/test_fundamentals_backfill.py tests/unit/test_xbrl.py -q
```

16 + 75 tests, all green and offline. `make check`: 2,474 passed, 13 skipped, 0 failed.
