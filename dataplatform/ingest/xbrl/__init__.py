"""Point-in-time fundamentals from NSE/BSE results filings (XBRL) — the true PIT store's D1 half.

The public surface of the M7.3 filings path:

* `discovery` — parse the `corporates-financial-results` index into `FilingIndexEntry`s, each
  carrying the first-knowable `filing_date` and the absolute URL of a filing's XBRL document.
* `parser` — parse one XBRL document into a `Filing` of `FundamentalFact`s, each tagged with
  `(period_end, filing_date, nature)` and, for a segment breakdown, its segment.

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
    CONCEPTS,
    Filing,
    FundamentalFact,
    Nature,
)
from dataplatform.ingest.xbrl.parser import (
    SEGMENT_CONCEPT,
    SOURCE_ID,
    parse,
)

__all__ = [
    "CONCEPTS",
    "SEGMENT_CONCEPT",
    "SOURCE_ID",
    "Filing",
    "FilingIndexEntry",
    "FundamentalFact",
    "Nature",
    "parse",
    "parse_index",
    "parse_index_l0",
]
