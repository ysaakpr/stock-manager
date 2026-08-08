"""D5: sync state machine + status API.

The §4.4 state machine is this package's public surface: every module that ingests a
`(source, date)` drives it through `SyncStateStore`, and every module that wants to know whether
a date is safe to act on calls `is_green` — the single entry point behind invariant #10, so that
"bad data never becomes decisions" is one function rather than a convention each caller
re-implements.

`api` is deliberately not re-exported: importing it pulls in FastAPI and builds the ASGI app,
which a library caller asking for the state machine has no use for.
"""

from dataplatform.status.sync_state import (
    CLOSED_STATES,
    LEGAL_TRANSITIONS,
    TERMINAL_STATES,
    GreenStatus,
    IllegalTransitionError,
    NotAGapError,
    SourceStatus,
    SyncRecord,
    SyncState,
    SyncStateError,
    SyncStateStore,
    UnknownSyncRowError,
    evaluate_green,
    expected_gap_kind,
    is_green,
)

__all__ = [
    "CLOSED_STATES",
    "LEGAL_TRANSITIONS",
    "TERMINAL_STATES",
    "GreenStatus",
    "IllegalTransitionError",
    "NotAGapError",
    "SourceStatus",
    "SyncRecord",
    "SyncState",
    "SyncStateError",
    "SyncStateStore",
    "UnknownSyncRowError",
    "evaluate_green",
    "expected_gap_kind",
    "is_green",
]
