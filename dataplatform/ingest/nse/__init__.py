"""D1: NSE source parsers.

One module per file format the exchange has published, each turning bytes that came out of L0
into the canonical rows of `dataplatform.ingest.models`. A parser never fetches: it takes a
payload, or an `L0Ref` it reads back through `L0Store`, so no L1 row can exist that was not
derived from a checksummed L0 payload (invariant #1).
"""

from dataplatform.ingest.nse.bhavcopy import CUTOVER as BHAVCOPY_CUTOVER
from dataplatform.ingest.nse.bhavcopy import Era as BhavcopyEra
from dataplatform.ingest.nse.bhavcopy import era_of as bhavcopy_era_of
from dataplatform.ingest.nse.bhavcopy import parse as parse_bhavcopy
from dataplatform.ingest.nse.bhavcopy import parse_l0 as parse_bhavcopy_l0
from dataplatform.ingest.nse.bhavcopy_legacy import (
    LEGACY_COLUMNS,
    LEGACY_ERA_END,
    LEGACY_SOURCE_ID,
)
from dataplatform.ingest.nse.bhavcopy_legacy import parse as parse_legacy_bhavcopy
from dataplatform.ingest.nse.bhavcopy_legacy import parse_l0 as parse_legacy_bhavcopy_l0
from dataplatform.ingest.nse.bhavcopy_udiff import (
    UDIFF_COLUMNS,
    UDIFF_ERA_START,
    UDIFF_SOURCE_ID,
)
from dataplatform.ingest.nse.bhavcopy_udiff import parse as parse_udiff_bhavcopy
from dataplatform.ingest.nse.bhavcopy_udiff import parse_l0 as parse_udiff_bhavcopy_l0
from dataplatform.ingest.nse.corp_actions import SOURCE_ID as CORP_ACTIONS_SOURCE_ID
from dataplatform.ingest.nse.corp_actions import parse as parse_corp_actions
from dataplatform.ingest.nse.corp_actions import parse_l0 as parse_corp_actions_l0
from dataplatform.ingest.nse.deals import (
    BLOCK_SOURCE_ID,
    BULK_SOURCE_ID,
    DEALS_DATASET,
    DealResolution,
    DealRow,
    DealsDay,
    DealSide,
    DealType,
    ResolvedDealRow,
)
from dataplatform.ingest.nse.deals import deals_for as deals_for_isin
from dataplatform.ingest.nse.deals import parse as parse_deals
from dataplatform.ingest.nse.deals import parse_l0 as parse_deals_l0
from dataplatform.ingest.nse.deals import resolve as resolve_deals
from dataplatform.ingest.nse.deals import write_l1 as write_deals_l1
from dataplatform.ingest.nse.delivery import (
    DELIVERY_COLUMNS,
    DELIVERY_SOURCE_ID,
    DeliveryResolution,
    DeliveryRow,
    ResolvedDeliveryRow,
)
from dataplatform.ingest.nse.delivery import parse as parse_delivery
from dataplatform.ingest.nse.delivery import parse_l0 as parse_delivery_l0
from dataplatform.ingest.nse.delivery import resolve as resolve_delivery
from dataplatform.ingest.nse.fii_dii import (
    FLOWS_DATASET,
    FlowCategory,
    FlowDay,
    FlowRow,
    StaleSessionError,
)
from dataplatform.ingest.nse.fii_dii import ingest_day as ingest_flows_day
from dataplatform.ingest.nse.fii_dii import parse as parse_fii_dii
from dataplatform.ingest.nse.fii_dii import parse_l0 as parse_fii_dii_l0

__all__ = [
    "BHAVCOPY_CUTOVER",
    "BLOCK_SOURCE_ID",
    "BULK_SOURCE_ID",
    "CORP_ACTIONS_SOURCE_ID",
    "DEALS_DATASET",
    "DELIVERY_COLUMNS",
    "DELIVERY_SOURCE_ID",
    "FLOWS_DATASET",
    "LEGACY_COLUMNS",
    "LEGACY_ERA_END",
    "LEGACY_SOURCE_ID",
    "UDIFF_COLUMNS",
    "UDIFF_ERA_START",
    "UDIFF_SOURCE_ID",
    "BhavcopyEra",
    "DealResolution",
    "DealRow",
    "DealSide",
    "DealType",
    "DealsDay",
    "DeliveryResolution",
    "DeliveryRow",
    "FlowCategory",
    "FlowDay",
    "FlowRow",
    "ResolvedDealRow",
    "ResolvedDeliveryRow",
    "StaleSessionError",
    "bhavcopy_era_of",
    "deals_for_isin",
    "ingest_flows_day",
    "parse_bhavcopy",
    "parse_bhavcopy_l0",
    "parse_corp_actions",
    "parse_corp_actions_l0",
    "parse_deals",
    "parse_deals_l0",
    "parse_delivery",
    "parse_delivery_l0",
    "parse_fii_dii",
    "parse_fii_dii_l0",
    "parse_legacy_bhavcopy",
    "parse_legacy_bhavcopy_l0",
    "parse_udiff_bhavcopy",
    "parse_udiff_bhavcopy_l0",
    "resolve_deals",
    "resolve_delivery",
    "write_deals_l1",
]
