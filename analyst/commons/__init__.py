"""A10 Research Commons: the shared facts every M17 manager reads (pre-registration §1).

The market sheet, the universe sheet, the mechanical shortlist, factual filing digests and the
web-fetch cache are built here once per session and read by every manager alike. The Commons
holds no recommendation, rank or opinion *from* a manager, so four managers stay four independent
judgments.

This package never imports `analyst.fundmanager`, directly or through anything it imports;
`tests/unit/test_commons_isolation.py` walks the import graph and fails the build if it does.
"""

from analyst.commons.sheets import (
    CommonsRefusedError,
    CommonsSheets,
    CommonsSource,
    Gap,
    MarketSheet,
    SourceUnavailableError,
    UniverseParameters,
    UniverseRow,
    build_commons_sheets,
)
from analyst.commons.sources import LakeCommonsSource
from analyst.commons.store import (
    CommonsStore,
    CommonsStoreError,
    InMemoryCommonsStore,
    PostgresCommonsStore,
)

__all__ = [
    "CommonsRefusedError",
    "CommonsSheets",
    "CommonsSource",
    "CommonsStore",
    "CommonsStoreError",
    "Gap",
    "InMemoryCommonsStore",
    "LakeCommonsSource",
    "MarketSheet",
    "PostgresCommonsStore",
    "SourceUnavailableError",
    "UniverseParameters",
    "UniverseRow",
    "build_commons_sheets",
]
