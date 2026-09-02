"""D1: BSE source parsers.

BSE is the reconciliation counterpart to NSE across D3 (§4.1): the two exchanges describe the same
corporate actions differently, and both are ingested so M2.3 can match them. One module per feed,
each turning bytes that came out of L0 into the platform's normalized rows. A parser never fetches:
it takes a payload, or an `L0Ref` it reads back through `L0Store`, so no L1 row can exist that was
not derived from a checksummed L0 payload (invariant #1).
"""

from dataplatform.ingest.bse.corp_actions import SOURCE_ID as CORP_ACTIONS_SOURCE_ID
from dataplatform.ingest.bse.corp_actions import parse as parse_corp_actions
from dataplatform.ingest.bse.corp_actions import parse_l0 as parse_corp_actions_l0

__all__ = [
    "CORP_ACTIONS_SOURCE_ID",
    "parse_corp_actions",
    "parse_corp_actions_l0",
]
