"""D1: NSE source parsers.

One module per file format the exchange has published, each turning bytes that came out of L0
into the canonical rows of `dataplatform.ingest.models`. A parser never fetches: it takes a
payload, or an `L0Ref` it reads back through `L0Store`, so no L1 row can exist that was not
derived from a checksummed L0 payload (invariant #1).
"""

from dataplatform.ingest.nse.bhavcopy_legacy import (
    LEGACY_COLUMNS,
    LEGACY_ERA_END,
    LEGACY_SOURCE_ID,
)
from dataplatform.ingest.nse.bhavcopy_legacy import parse as parse_legacy_bhavcopy
from dataplatform.ingest.nse.bhavcopy_legacy import parse_l0 as parse_legacy_bhavcopy_l0

__all__ = [
    "LEGACY_COLUMNS",
    "LEGACY_ERA_END",
    "LEGACY_SOURCE_ID",
    "parse_legacy_bhavcopy",
    "parse_legacy_bhavcopy_l0",
]
