# XBRL fixtures — captured, not authored

Every file here is a **byte-frozen copy of a real NSE response** (B8), captured 2026-09-03 through
the project's own `Fetcher` against the two Source Register rows of the point-in-time fundamentals
path. Nothing here is hand-built.

That distinction is the whole reason this directory was re-cut. The first version of these fixtures
was *modelled* on the in-bse-fin taxonomy rather than captured from it, and the M7.3 parser it
validated could not read a single real filing: the fabricated documents used `xbrli:segment` with a
typed `SegmentName` member, one period per document, and an ISIN as the entity identifier — none of
which occur in real NSE filings. A parser is only exercised by the format it will actually meet, so
these are the format it will actually meet, warts included.

## `index/` — the discovery feed

| File | What it is |
|---|---|
| `corporates-financial-results_Quarterly_20260701_20260903.json` | One whole live response for a date window, verbatim (6 records). |
| `corporates-financial-results_slice.json` | A 14-record slice of the full 3,816-record undated response, keeping every record that links a document frozen under `filings/` plus one real no-XBRL-document record. Records are verbatim; only the selection is ours. |

Real-world properties these carry, each of which broke the first parser:

- `consolidated` is **`Consolidated` / `Non-Consolidated`** — never `Standalone`. Across the full
  3,816-record response: 1,630 / 2,186 and no third value.
- `audited` is **`Audited` / `Un-Audited`** — hyphenated, so an un-squashed match on `unaudited`
  reads all 3,598 unaudited entries as "did not say".
- `xbrl` is sometimes the archive path ending in a bare **`-`**: the announcement exists, the
  document does not (18 of 3,816).
- **One document is linked by more than one entry.** `SCHAEFFLER`'s `INDAS_121084…xml` is named by
  both an Annual entry (01-Jan-2024→31-Dec-2024, Audited) and a Quarterly one
  (01-Oct-2024→31-Dec-2024, Un-Audited). The entry's period is what picks the column.

## `filings/` — the XBRL documents

Eleven documents, chosen to span both taxonomy vocabularies, both natures, quarterly and annual
entries, single- and multi-segment disclosure, and a company reporting zero revenue.

| File | Company | Taxonomy | Notes |
|---|---|---|---|
| `INDAS_121276_…30072026051555.xml` | VSTTILLERS | Ind-AS | Standalone, single segment. The filing the M7 defect report was written against. |
| `INDAS_121277_…30072026051753.xml` | VSTTILLERS | Ind-AS | Consolidated pair of the above. |
| `INDAS_117297_…16012025081520.xml` | RELIANCE | Ind-AS | Multi-segment; the scale check (2438650000000 = the 243,865 crore its statement prints). |
| `INDAS_118941_…06022025072019.xml` | ITC | Ind-AS | Multi-segment. |
| `INDAS_119491_…10022025092957.xml` | GRASIM | Ind-AS | Multi-segment, incl. segment assets/liabilities. |
| `INDAS_119596_…11022025040549.xml` | KANANIIND | Ind-AS | Tiny filing, 5 contexts, **zero revenue** — a real filing that a `revenue > 0` assumption would reject. |
| `INDAS_121083_…25032025122711.xml` | SCHAEFFLER | Ind-AS | December year-end, standalone; annual column audited, quarter column not. |
| `INDAS_121084_…25032025122849.xml` | SCHAEFFLER | Ind-AS | Consolidated pair of the above. |
| `NBFC_INDAS_118070_…29012025074729.xml` | BAJFINANCE | Ind-AS (NBFC entry point) | Finance vocabulary layered on the Ind-AS P&L spine. |
| `BANKING_117524_…23012025122553.xml` | HDFCBANK | Banking | Different P&L vocabulary; its `ISIN` element (`INE040A01034`) **disagrees** with the index (`INE040A01018`). |
| `BANKING_118874_…06022025045242.xml` | SBIN | Banking | Ditto (`INE062A01020` vs index `INE062A01012`). |

The two banking disagreements are the evidence for a rule, not a curiosity: the ISIN a fact is
stored under comes from the index (D2's key, invariant #2), and the document's own ISIN element is
never read. The document is cross-checked on **symbol**, which every filing states reliably.

## Re-capturing

`parse_index` and `parse` are offline and take bytes; the tests never open a socket. To refresh,
fetch the URLs in the Source Register rows `nse_financial_results_index` and `nse_xbrl_filing`
through `dataplatform.ingest.fetcher.build_fetcher` and copy the L0 payloads here verbatim — never
edit a captured file to make a test pass.
