"""D4: query service - the internal point-in-time query API.

`QueryService` is the one entrypoint every consumer (A3, A5, A6, X2, D6) reads market data through.
It serves the four canonical query shapes of §4.5: adjusted OHLCV series per ISIN (a) and
full-market cross-sections (b) from M4.1, plus screen filters (c) and the point-in-time universe (d)
added by M4.2 — all over DuckDB/Parquet, deduped to the primary listing. The request/response models
it speaks in live in `shapes`; the screen filter algebra and PIT-only fundamentals join surface in
`screen`; the survivorship-safe universe in `universe`. The announcement index is the
searchable-disclosure half of D4 (M3 box 3).
"""

from dataplatform.query.announcement_search import (
    AnnouncementIndex,
    CompiledQuery,
    KeywordQuery,
    build_from_l1,
    normalize,
)
from dataplatform.query.errors import QueryError
from dataplatform.query.pit import Dataset, PitContext, PitError
from dataplatform.query.quarantine import (
    PIT_FUNDAMENTALS_VIEW,
    RESTATED_FUNDAMENTALS_VIEW,
    ProvenancedFundamental,
    QueryContext,
    RestatedReader,
    StoreCatalog,
    backtest_catalog,
    monitoring_catalog,
)
from dataplatform.query.screen import (
    Compare,
    Filter,
    FundamentalDatum,
    FundamentalsSource,
    PitFundamentals,
    QuarantineError,
    ScreenRow,
    Selector,
    all_of,
    any_of,
    between,
    eq,
    fundamental,
    ge,
    gt,
    le,
    lt,
    metric,
    run_screen,
)
from dataplatform.query.service import QueryService
from dataplatform.query.shapes import (
    AdjustedPoint,
    AdjustedSeries,
    AdjustedSeriesRequest,
    CrossSection,
    CrossSectionRequest,
)
from dataplatform.query.universe import (
    InMemoryListingCalendar,
    ListingCalendar,
    ListingWindow,
    PitUniverse,
    index_membership_asof,
    pit_universe,
    store_listing_calendar,
)

__all__ = [
    "PIT_FUNDAMENTALS_VIEW",
    "RESTATED_FUNDAMENTALS_VIEW",
    "AdjustedPoint",
    "AdjustedSeries",
    "AdjustedSeriesRequest",
    "AnnouncementIndex",
    "Compare",
    "CompiledQuery",
    "CrossSection",
    "CrossSectionRequest",
    "Dataset",
    "Filter",
    "FundamentalDatum",
    "FundamentalsSource",
    "InMemoryListingCalendar",
    "KeywordQuery",
    "ListingCalendar",
    "ListingWindow",
    "PitContext",
    "PitError",
    "PitFundamentals",
    "PitUniverse",
    "ProvenancedFundamental",
    "QuarantineError",
    "QueryContext",
    "QueryError",
    "QueryService",
    "RestatedReader",
    "ScreenRow",
    "Selector",
    "StoreCatalog",
    "all_of",
    "any_of",
    "backtest_catalog",
    "between",
    "build_from_l1",
    "eq",
    "fundamental",
    "ge",
    "gt",
    "index_membership_asof",
    "le",
    "lt",
    "metric",
    "monitoring_catalog",
    "normalize",
    "pit_universe",
    "run_screen",
    "store_listing_calendar",
]
