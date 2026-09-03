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
| `corporates-financial-results_slice.json` | A 27-record slice drawn from the captured index responses, keeping every record that links a document frozen under `filings/` plus one real no-XBRL-document record. Records are verbatim; only the selection is ours. |

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

Twenty-one documents, chosen to span all three taxonomy vocabularies, both natures, quarterly and
annual entries, single- and multi-segment disclosure, a company reporting zero revenue, a real
restatement, and every **format era** the feed serves. The era spread came from probing the whole
10-year index and sampling one filing per (financial year x `indAs` label x `bank` flag) cell: 57
real filings across 8 distinct taxonomy entry points, whose failures are what the second half of
this table exists to prevent recurring.

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
| `INDAS_119528_…11022025120304.xml` | VSTTILLERS | Ind-AS | The **original** Q3 FY25 filing, overstating revenue 10x; standalone. |
| `INDAS_119531_…11022025120916.xml` | VSTTILLERS | Ind-AS | Consolidated pair of the above. |
| `INDAS_120087_…12022025094622.xml` | STANLEY | Ind-AS | Discloses a segment its own re-filing later withdraws. |
| `INDAS_121074_…21032025010118.xml` | STANLEY | Ind-AS | The re-filing, with no segment disclosure. |

### The older format eras

Everything above was broadcast in 2025-2026. These are the generations before it, and each breaks
an assumption the modern documents let you get away with.

| File | Company | Era | What it proves |
|---|---|---|---|
| `BANKING_48497_…14092019023146_WEB.xml` | ALBK | `banking_entry_point_2018-03-31` | The 2018-2022 `…_WEB` generation **never declares its column contexts** — facts reference `OneD`/`FourD`, which no `<context>` defines. Also: the header block (financial year, `ReportingQuarter`) is pinned to `OneD` but describes the *document*; `OneD` itself is zero-filled and the year's numbers are in `FourD`. Reading the header as `OneD`'s period stores a bank's revenue as zero. |
| `NONINDAS_63114_…05112020010920_WEB.xml` | MCL | `other_than_banks_entry_point_2018-03-31` | Declares **no `<context>` at all**, so it carries no `<xbrli:entity>` either. Its `Symbol` fact is the only identity it states. |
| `NONINDAS_108914_…02072024094147.xml` | TARACHAND | `other_than_banks_entry_point_2019-09-30` | The third vocabulary: total income is `Revenue`, not `Income`; the bottom line is `ProfitLossForThePeriod`. Multi-segment. |
| `NONINDAS_48504_…14092019033403_WEB.xml` | EMKAY | `other_than_banks_entry_point_2018-03-31` | Holds **only** the cumulative column, yet the index lists it under both an Annual and a Quarterly entry. The quarterly entry must be *refused* — a quarter's start is stated nowhere in it. Also the only captured filing with no `ProfitBeforeTax` element (the old form reports `ProfitBeforeExtraordinaryItemsAndTax`, a different basis, left deliberately unmapped). |
| `BANKING_601183_…25072022110219_WEB.xml` | J&KBANK | `banking_entry_point_2019-09-30` | Identifies its entity by **BSE scrip code** (`532209`), not NSE symbol, so the cross-check falls to the `Symbol` fact. Declares its columns' periods, which is what independently confirms `Four` = cumulative and `One` = the quarter. |
| `NBFC_INDAS_94201_…15072023024353.xml` | HEALTHX | `Ind-AS_entry_point_2020-03-31` | A **renamed** company: files as `SASTASUNDR` under an index entry that now says `HEALTHX`. The ISIN is unchanged, so the symbol cross-check needs the D2 symbol history. |

The two banking disagreements are the evidence for a rule, not a curiosity: the ISIN a fact is
stored under comes from the index (D2's key, invariant #2), and the document's own ISIN element is
never read. The document is cross-checked on **symbol** — which is itself only reliable given the
ISIN's symbol *history*, per the rename case above.

## Availability, and the real depth of this dataset

Probing the whole window (index requests only) found 135,197 announcements over ten financial
years — the feed is not "recent quarters only". But **XBRL documents exist only from about FY2018-19
onward**: every one of the 28,247 announcements in FY2016-17 and FY2017-18 has the `-` placeholder
in place of a document, FY2018-19 is 78% covered and FY2019-20 85%, and from FY2020-21 coverage is
effectively complete. About 101,446 filings are therefore ingestible. That is the honest depth of
the PIT fundamentals store from this source, and it is a fact about the source rather than a policy
choice.

## Re-capturing

`parse_index` and `parse` are offline and take bytes; the tests never open a socket. To refresh,
fetch the URLs in the Source Register rows `nse_financial_results_index` and `nse_xbrl_filing`
through `dataplatform.ingest.fetcher.build_fetcher` and copy the L0 payloads here verbatim — never
edit a captured file to make a test pass.
