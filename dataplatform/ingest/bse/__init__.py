"""D1: BSE source parsers.

BSE is the reconciliation counterpart to NSE across D3 (§4.1): the two exchanges describe the same
corporate actions differently, and both are ingested so M2.3 can match them. It is also a second
price source in its own right (§4.1 row 4), parsed to the same canonical `PriceRow` as NSE so a
BSE session lands in L1 under the identical schema (M3.1). One module per feed, each turning bytes
that came out of L0 into the platform's normalized rows. A parser never fetches: it takes a payload,
or an `L0Ref` it reads back through `L0Store`, so no L1 row can exist that was not derived from a
checksummed L0 payload (invariant #1).

BSE keys on `SC_CODE`, never on ISIN, so the scrip master (`scrip_master`) is the D2 resolver that
turns a scrip code into an ISIN before any BSE row can be joined (invariant #2).
"""

from dataplatform.ingest.bse.bhavcopy import LEGACY_SOURCE_ID as BHAVCOPY_LEGACY_SOURCE_ID
from dataplatform.ingest.bse.bhavcopy import UDIFF_SOURCE_ID as BHAVCOPY_UDIFF_SOURCE_ID
from dataplatform.ingest.bse.bhavcopy import BseLegacyQuote, LegacyResolution
from dataplatform.ingest.bse.bhavcopy import parse as parse_bhavcopy
from dataplatform.ingest.bse.bhavcopy import parse_l0 as parse_bhavcopy_l0
from dataplatform.ingest.bse.bhavcopy import parse_legacy as parse_bhavcopy_legacy
from dataplatform.ingest.bse.bhavcopy import resolve_legacy as resolve_bhavcopy_legacy
from dataplatform.ingest.bse.corp_actions import SOURCE_ID as CORP_ACTIONS_SOURCE_ID
from dataplatform.ingest.bse.corp_actions import parse as parse_corp_actions
from dataplatform.ingest.bse.corp_actions import parse_l0 as parse_corp_actions_l0
from dataplatform.ingest.bse.scrip_master import (
    BSE_SCRIP_MASTER_SOURCE,
    BseScrip,
    BseScripIngestReport,
    ingest_scrip_master,
    parse_scrip_master,
    scrip_to_isin,
)

__all__ = [
    "BHAVCOPY_LEGACY_SOURCE_ID",
    "BHAVCOPY_UDIFF_SOURCE_ID",
    "BSE_SCRIP_MASTER_SOURCE",
    "CORP_ACTIONS_SOURCE_ID",
    "BseLegacyQuote",
    "BseScrip",
    "BseScripIngestReport",
    "LegacyResolution",
    "ingest_scrip_master",
    "parse_bhavcopy",
    "parse_bhavcopy_l0",
    "parse_bhavcopy_legacy",
    "parse_corp_actions",
    "parse_corp_actions_l0",
    "parse_scrip_master",
    "resolve_bhavcopy_legacy",
    "scrip_to_isin",
]
