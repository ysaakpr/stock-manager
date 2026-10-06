"""D2: identity master - ISIN-keyed security identity and symbol history.

This package owns invariant #2: nothing in the system joins on a raw symbol, and
`IdentityMaster.resolve(symbol, on_date)` is the only legitimate symbol→ISIN path in the
codebase. A module that needs an ISIN takes an `IdentityMaster` (built by `IdentityStore` from
Postgres, or from windows directly in a test) and asks it — it does not query `symbol_history`
itself, and it certainly does not match a symbol column against a price table.

`master.py` is the model, the resolver and the four tables. `ingest.py` is the NSE side: parsing
`EQUITY_L.csv` and `symbolchange.csv` and reconstructing the history neither file states
outright. Everything exported here is one of those two; the parsers stay behind
`dataplatform.identity.ingest` because only the weekly refresh needs them.
"""

from dataplatform.identity.ingest import (
    IdentityIngestReport,
    IdentityParseError,
    ingest_snapshot,
)
from dataplatform.identity.lineage import (
    CORROBORATING_TYPES,
    LINEAGE_BACKFILL,
    EqPresence,
    IsinSpan,
    LineageEdge,
    LineageResolver,
    LineageStore,
    LineageWriteReport,
    RegistrationPlan,
    SkipReason,
    derive_edges,
    plan_registrations,
    read_corroboration,
    read_eq_presence,
    read_equity_spans,
)
from dataplatform.identity.master import (
    AmbiguousSymbolError,
    ConflictKind,
    DetectedBy,
    Exchange,
    HistoryPlan,
    HistoryRefusal,
    IdentityConflict,
    IdentityError,
    IdentityMaster,
    IdentityStore,
    InMemoryReconciliationQueue,
    Listing,
    ListingStatus,
    ReconciliationQueue,
    Security,
    SymbolWindow,
    UnknownIsinError,
    UnknownSymbolError,
    WriteCounts,
    detect_conflicts,
    plan_history,
)
from dataplatform.identity.primary import (
    Canonical,
    DailyLiquidity,
    ExchangeLiquidity,
    LiquidityMetric,
    ListingKeyed,
    NoLiquidityError,
    PrimaryDecision,
    PrimaryRule,
    canonical_daily,
    select_primary,
    select_primary_map,
)
from dataplatform.identity.session import SessionIdentity, SessionStatement

__all__ = [
    "CORROBORATING_TYPES",
    "LINEAGE_BACKFILL",
    "AmbiguousSymbolError",
    "Canonical",
    "ConflictKind",
    "DailyLiquidity",
    "DetectedBy",
    "EqPresence",
    "Exchange",
    "ExchangeLiquidity",
    "HistoryPlan",
    "HistoryRefusal",
    "IdentityConflict",
    "IdentityError",
    "IdentityIngestReport",
    "IdentityMaster",
    "IdentityParseError",
    "IdentityStore",
    "InMemoryReconciliationQueue",
    "IsinSpan",
    "LineageEdge",
    "LineageResolver",
    "LineageStore",
    "LineageWriteReport",
    "LiquidityMetric",
    "Listing",
    "ListingKeyed",
    "ListingStatus",
    "NoLiquidityError",
    "PrimaryDecision",
    "PrimaryRule",
    "ReconciliationQueue",
    "RegistrationPlan",
    "Security",
    "SessionIdentity",
    "SessionStatement",
    "SkipReason",
    "SymbolWindow",
    "UnknownIsinError",
    "UnknownSymbolError",
    "WriteCounts",
    "canonical_daily",
    "derive_edges",
    "detect_conflicts",
    "ingest_snapshot",
    "plan_history",
    "plan_registrations",
    "read_corroboration",
    "read_eq_presence",
    "read_equity_spans",
    "select_primary",
    "select_primary_map",
]
