"""A10 Research Commons: the shared facts every M17 manager reads (pre-registration §1).

The market sheet, the universe sheet, the mechanical shortlist, factual filing digests and the
web-fetch cache are built here once per session and read by every manager alike. M17.9 adds the
screens S1-S5, the regime and the Amendment 1 (b) exclusions (`screens`), the per-name dossier
with citable field ids (`dossier`), and the frozen base-rate table (`base_rates`). The Commons
holds no recommendation, rank or opinion *from* a manager, so four managers stay four independent
judgments.

This package never imports `analyst.fundmanager`, directly or through anything it imports;
`tests/unit/test_commons_isolation.py` walks the import graph and fails the build if it does.
"""

from analyst.commons.base_rates import BaseRateCell, BaseRateTable, load_table
from analyst.commons.digests import (
    DIGEST_MODEL,
    DIGEST_OUTPUT_SCHEMA,
    AnnouncementText,
    DigestBody,
    DigestFailure,
    DigestRefusedError,
    DigestRun,
    DigestSource,
    DigestStore,
    FilingDigest,
    FilingInput,
    FilingKind,
    OnDemandDigests,
    build_digests,
    digest_for_isins,
)
from analyst.commons.dossier import (
    Dossier,
    DossierItem,
    UnknownFieldError,
    build_dossiers,
    resolve_field,
)
from analyst.commons.events import (
    EVENT_KEYWORDS_DIGEST,
    KeywordTable,
    classify,
    load_event_keywords,
)
from analyst.commons.exclusions import Exclusion, ExclusionReason, Exclusions
from analyst.commons.fetch import (
    FetchedPage,
    Fetcher,
    FetchError,
    FetchKind,
    FetchOutcome,
    FetchRequest,
    FetchResponse,
    Snapshot,
    SnapshotIntegrityError,
    SnapshotPage,
    SnapshotStore,
)
from analyst.commons.inputs import ScreenSource
from analyst.commons.regime import RegimeReading, RegimeState
from analyst.commons.screens import (
    SCREENS_RULE_HASH,
    CommonsScreens,
    EventFact,
    Screen,
    ScreenEntry,
    build_screens,
)
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
from analyst.commons.shortlist import (
    SHORTLIST_RULE_HASH,
    SHORTLIST_SIZE,
    Shortlist,
    ShortlistEntry,
    build_shortlist,
)
from analyst.commons.sources import LakeCommonsSource
from analyst.commons.store import (
    CommonsStore,
    CommonsStoreError,
    InMemoryCommonsStore,
    InMemoryDigestStore,
    InMemoryShortlistStore,
    PostgresCommonsStore,
    PostgresDigestStore,
    PostgresShortlistStore,
    ShortlistStore,
)

__all__ = [
    "DIGEST_MODEL",
    "DIGEST_OUTPUT_SCHEMA",
    "EVENT_KEYWORDS_DIGEST",
    "SCREENS_RULE_HASH",
    "SHORTLIST_RULE_HASH",
    "SHORTLIST_SIZE",
    "AnnouncementText",
    "BaseRateCell",
    "BaseRateTable",
    "CommonsRefusedError",
    "CommonsScreens",
    "CommonsSheets",
    "CommonsSource",
    "CommonsStore",
    "CommonsStoreError",
    "DigestBody",
    "DigestFailure",
    "DigestRefusedError",
    "DigestRun",
    "DigestSource",
    "DigestStore",
    "Dossier",
    "DossierItem",
    "EventFact",
    "Exclusion",
    "ExclusionReason",
    "Exclusions",
    "FetchError",
    "FetchKind",
    "FetchOutcome",
    "FetchRequest",
    "FetchResponse",
    "FetchedPage",
    "Fetcher",
    "FilingDigest",
    "FilingInput",
    "FilingKind",
    "Gap",
    "InMemoryCommonsStore",
    "InMemoryDigestStore",
    "InMemoryShortlistStore",
    "KeywordTable",
    "LakeCommonsSource",
    "MarketSheet",
    "OnDemandDigests",
    "PostgresCommonsStore",
    "PostgresDigestStore",
    "PostgresShortlistStore",
    "RegimeReading",
    "RegimeState",
    "Screen",
    "ScreenEntry",
    "ScreenSource",
    "Shortlist",
    "ShortlistEntry",
    "ShortlistStore",
    "Snapshot",
    "SnapshotIntegrityError",
    "SnapshotPage",
    "SnapshotStore",
    "SourceUnavailableError",
    "UniverseParameters",
    "UniverseRow",
    "UnknownFieldError",
    "build_commons_sheets",
    "build_digests",
    "build_dossiers",
    "build_screens",
    "build_shortlist",
    "classify",
    "digest_for_isins",
    "load_event_keywords",
    "load_table",
    "resolve_field",
]
