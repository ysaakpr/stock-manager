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

__all__ = [
    "AmbiguousSymbolError",
    "ConflictKind",
    "DetectedBy",
    "Exchange",
    "HistoryPlan",
    "HistoryRefusal",
    "IdentityConflict",
    "IdentityError",
    "IdentityIngestReport",
    "IdentityMaster",
    "IdentityParseError",
    "IdentityStore",
    "InMemoryReconciliationQueue",
    "Listing",
    "ListingStatus",
    "ReconciliationQueue",
    "Security",
    "SymbolWindow",
    "UnknownIsinError",
    "UnknownSymbolError",
    "WriteCounts",
    "detect_conflicts",
    "ingest_snapshot",
    "plan_history",
]
