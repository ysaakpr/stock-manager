"""Point-in-time fundamentals from NSE results filings (XBRL) — the true PIT store's D1 half.

The public surface of the M7.3 filings path:

* `discovery` — parse the `corporates-financial-results` index into `FilingIndexEntry`s, each
  carrying the first-knowable `filing_date`, the ISIN the facts are stored under, the reporting
  period that selects a column of the filing, and the absolute URL of its XBRL document.
* `parser` — parse one XBRL document *as named by one index entry* into a `Filing` of
  `FundamentalFact`s, each tagged with `(period_end, filing_date, nature)` and, for a segment
  breakdown, its segment.

The two halves are not independent: a results document transcribes the published table column by
column (a quarter and a cumulative period), and it is the index entry that says which column a
given filing is — so `parser.parse` takes a `FilingIndexEntry` rather than loose scalars. See
`parser`'s module docstring for the format's four load-bearing properties.

The store the parsed filings land in is `dataplatform.store.pit_fundamentals`; the restated,
monitoring-only store (M7.1) is a separate root, quarantined from backtests (invariant #8).
"""

from __future__ import annotations

from dataplatform.ingest.xbrl.discovery import (
    FilingIndexEntry,
    parse_index,
    parse_index_l0,
)
from dataplatform.ingest.xbrl.models import (
    BANKING_CONCEPTS,
    COMMON_CONCEPT_KEYS,
    CONCEPT_KEYS,
    CONCEPTS,
    CONDITIONAL_CONCEPT_KEYS,
    DERIVED_CONCEPTS,
    IND_AS_CONCEPTS,
    NON_IND_AS_CONCEPTS,
    SHAREHOLDERS_EQUITY,
    SHARES_OUTSTANDING,
    Filing,
    FundamentalFact,
    Nature,
    Taxonomy,
    concepts_for,
)
from dataplatform.ingest.xbrl.parser import (
    SEGMENT_CONCEPT,
    SOURCE_ID,
    parse,
)

__all__ = [
    "BANKING_CONCEPTS",
    "COMMON_CONCEPT_KEYS",
    "CONCEPTS",
    "CONCEPT_KEYS",
    "CONDITIONAL_CONCEPT_KEYS",
    "DERIVED_CONCEPTS",
    "IND_AS_CONCEPTS",
    "NON_IND_AS_CONCEPTS",
    "SEGMENT_CONCEPT",
    "SHAREHOLDERS_EQUITY",
    "SHARES_OUTSTANDING",
    "SOURCE_ID",
    "Filing",
    "FilingIndexEntry",
    "FundamentalFact",
    "Nature",
    "Taxonomy",
    "concepts_for",
    "parse",
    "parse_index",
    "parse_index_l0",
]
