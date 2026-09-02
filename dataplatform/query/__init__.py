"""D4: query service - the internal point-in-time query API.

`QueryService` (M4.1) is the one entrypoint every consumer (A3, A5, A6, X2, D6) reads market data
through: adjusted OHLCV series per ISIN and full-market cross-sections, over DuckDB/Parquet, deduped
to the primary listing. The request/response models it speaks in live in `shapes`. The announcement
index is the searchable-disclosure half of D4 (M3 box 3).
"""

from dataplatform.query.announcement_search import (
    AnnouncementIndex,
    CompiledQuery,
    KeywordQuery,
    build_from_l1,
    normalize,
)
from dataplatform.query.service import QueryError, QueryService
from dataplatform.query.shapes import (
    AdjustedPoint,
    AdjustedSeries,
    AdjustedSeriesRequest,
    CrossSection,
    CrossSectionRequest,
)

__all__ = [
    "AdjustedPoint",
    "AdjustedSeries",
    "AdjustedSeriesRequest",
    "AnnouncementIndex",
    "CompiledQuery",
    "CrossSection",
    "CrossSectionRequest",
    "KeywordQuery",
    "QueryError",
    "QueryService",
    "build_from_l1",
    "normalize",
]
