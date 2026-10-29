"""M18.2: the reads behind the console's Data page — shared with `/status/*` where one exists.

`read_sync_status` and `read_sources_status` are the bodies of `GET /status/sync` and
`GET /status/sources`, moved here so the endpoint and the page call one function and cannot
disagree. The gaps, quality and quarantine panels call the endpoints' own functions
(`GapScanner.report` + `GapsOut.of`, `read_quality`, `read_quarantine_status`) directly.

What is new is read-only SQL the JSON surface never had: the archive list, and the ISIN lookup over
the D2 master, D3's actions and factor chain and the L2 invalidation queue. Every lookup query is
keyed on the ISIN (invariant #2). The one query that takes a symbol, `search_symbol`, answers
"which ISINs has this symbol been?" from `symbol_history` and nothing else: it is display only, so
an operator can pick an ISIN, and its result is never used to fetch data — the page links each
candidate to the ISIN lookup instead.

Like `queries.py`: every function takes an open connection, none opens one, reads the clock or
writes.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date, timedelta
from typing import Final

from dataplatform.ingest.calendar import CalendarCoverageError, TradingCalendar
from dataplatform.ingest.models import ISIN_PATTERN
from dataplatform.scheduler.registry import default_registry, lag_budgets
from dataplatform.status.deviations import DeviationJudge
from dataplatform.status.models import (
    DeclinedOut,
    SourcesOut,
    SourceStatusOut,
    SyncStatusOut,
)
from dataplatform.status.models_data import (
    ArchiveListOut,
    ArchiveSummaryOut,
    CorporateActionOut,
    FactorOut,
    InvalidationOut,
    IsinLookupOut,
    LineageEdgeOut,
    SecurityOut,
    SymbolCandidateOut,
    SymbolSearchOut,
    SymbolWindowOut,
)
from dataplatform.status.queries import split_accepted
from dataplatform.status.sync_state import SyncStateStore
from dataplatform.store.db import Connection

__all__ = [
    "ARCHIVE_LIST_LIMIT",
    "SYMBOL_SEARCH_LIMIT",
    "is_isin",
    "latest_session",
    "read_archive_list",
    "read_isin_lookup",
    "read_sources_status",
    "read_sync_status",
    "search_symbol",
    "sort_sources_worst_first",
]

ARCHIVE_LIST_LIMIT: Final = 30
SYMBOL_SEARCH_LIMIT: Final = 200

#: How far back `latest_session` looks before it gives up — longer than any closure on record.
_SESSION_LOOKBACK_DAYS: Final = 30

_ISIN: Final = re.compile(ISIN_PATTERN)


def is_isin(text: str) -> bool:
    """Whether `text` has the ISO 6166 shape the `isin` domain enforces (no check-digit test)."""
    return _ISIN.fullmatch(text) is not None


# ── /status/sync and /status/sources ───────────────────────────────────────────────────────


def read_sync_status(
    store: SyncStateStore, logical_date: date, datasets: Sequence[str] | None = None
) -> SyncStatusOut:
    """`GET /status/sync?date=` — every source's row for the date, its day kind, and the verdict.

    What it does: reads the date's `sync_state` rows and C.2 day kind, and — only when `datasets`
    names some — `is_green`'s verdict over exactly those.
    What it never does: pick "core" datasets itself. No datasets means no verdict (`green: null`).
    """
    rows = store.rows_for_date(logical_date)
    kind = store.day_kind(logical_date)
    green = store.is_green(logical_date, datasets) if datasets else None
    return SyncStatusOut.of(
        logical_date,
        rows,
        day_kind=None if kind is None else kind.value,
        expects_data=None if kind is None else kind.expects_data,
        green=green,
    )


def read_sources_status(
    store: SyncStateStore, as_of: date, *, judge: DeviationJudge | None = None
) -> SourcesOut:
    """`GET /status/sources` — each recorded source's last success, lag and failure streak.

    Lag budgets are the production registry's (`lag_budgets`), so a source a job keeps current is
    `overdue` once it is more sessions behind than that job allows. With a `judge`, an unhealthy
    source carries its accepted-deviation verdict and `accepted`/`errors` split the unhealthy
    count; every line's `healthy` is the store's, unchanged.
    """
    budgets = lag_budgets(default_registry())
    statuses = store.source_statuses(budgets)
    verdicts = [None if judge is None else judge.source(status) for status in statuses]
    # A paused source is counted apart (D29), neither an error nor an accepted deviation.
    live = [(status, verdict) for status, verdict in zip(statuses, verdicts, strict=True)
            if status.paused is None]  # fmt: skip
    accepted, errors = split_accepted([s.healthy for s, _ in live], [v for _, v in live])
    paused = sum(1 for status in statuses if status.paused is not None and not status.healthy)
    return SourcesOut(
        as_of=as_of,
        sources=[
            SourceStatusOut.of(status, verdict)
            for status, verdict in zip(statuses, verdicts, strict=True)
        ],
        accepted=accepted,
        errors=errors,
        paused=paused,
        declined={source: DeclinedOut.of(record) for source, record in store.declined.items()},
    )


def sort_sources_worst_first(sources: Sequence[SourceStatusOut]) -> list[SourceStatusOut]:
    """Unhealthy before healthy, then longest failure streak, then most sessions behind.

    An unhealthy source an accepted deviation covers sorts after every unaccepted one, so a new
    red is at the top. A lag the calendar cannot measure (`None`) sorts as the worst lag: "we
    cannot tell" must not round down to "fine". Declined sources land with the healthy.
    """

    def rank(line: SourceStatusOut) -> tuple[int, int, int, int, str]:
        lag = line.lag_sessions
        accepted = line.deviation is not None and line.deviation.accepted
        return (
            2 if line.healthy else (1 if accepted or line.paused is not None else 0),
            -line.failure_streak,
            0 if line.overdue else 1,
            -(10**9 if lag is None else lag),
            line.source,
        )

    return sorted(sources, key=rank)


def latest_session(calendar: TradingCalendar, on_or_before: date) -> date | None:
    """The newest date on or before `on_or_before` the exchange should have published for.

    None when the calendar cannot say (outside its coverage) or no such day is in the lookback —
    the caller then shows the date it was given rather than guessing one.
    """
    day = on_or_before
    for _ in range(_SESSION_LOOKBACK_DAYS):
        try:
            if calendar.classify(day).expects_data:
                return day
        except CalendarCoverageError:
            return None
        day -= timedelta(days=1)
    return None


# ── archives ────────────────────────────────────────────────────────────────────────────────

_ARCHIVE_LIST_SQL = """
SELECT logical_date, schema_version, bundle_path, manifest_sha256, file_count, total_bytes,
       published_at
FROM archive_bundle
ORDER BY logical_date DESC
LIMIT %s
"""

_ARCHIVE_COUNT_SQL = "SELECT count(*) FROM archive_bundle"


def read_archive_list(conn: Connection, *, limit: int = ARCHIVE_LIST_LIMIT) -> ArchiveListOut:
    """The newest published bundles (no manifests — `/archives?date=` serves those)."""
    count = conn.execute(_ARCHIVE_COUNT_SQL).fetchone()
    bundles = [
        ArchiveSummaryOut(
            logical_date=row[0],
            schema_version=str(row[1]),
            bundle_path=str(row[2]),
            manifest_sha256=str(row[3]),
            file_count=int(row[4]),
            total_bytes=int(row[5]),
            published_at=row[6],
        )
        for row in conn.execute(_ARCHIVE_LIST_SQL, (limit,)).fetchall()
    ]
    return ArchiveListOut(total=0 if count is None else int(count[0]), bundles=bundles, limit=limit)


# ── ISIN lookup ─────────────────────────────────────────────────────────────────────────────

_SECURITY_SQL = """
SELECT isin, name, primary_exchange, status, face_value_inr, first_seen_date, last_seen_date,
       registered_by, updated_at
FROM security_master
WHERE isin = %s
"""

_SYMBOLS_SQL = """
SELECT exchange, symbol, series, valid_from, valid_to, source
FROM symbol_history
WHERE isin = %s
ORDER BY valid_from, exchange, symbol
"""

#: The whole linear chain through the ISIN: predecessors walked backwards, successors forwards.
#: The depth cap is a guard against a cycle the table's constraints do not rule out on their own.
_LINEAGE_SQL = """
WITH RECURSIVE
back (predecessor_isin, successor_isin, effective_date, detected_by, confidence, gap_sessions,
      symbol_at_change, corroborating_action, depth) AS (
    SELECT predecessor_isin, successor_isin, effective_date, detected_by, confidence,
           gap_sessions, symbol_at_change, corroborating_action, 1
    FROM isin_lineage WHERE successor_isin = %s
    UNION ALL
    SELECT l.predecessor_isin, l.successor_isin, l.effective_date, l.detected_by, l.confidence,
           l.gap_sessions, l.symbol_at_change, l.corroborating_action, b.depth + 1
    FROM isin_lineage l JOIN back b ON l.successor_isin = b.predecessor_isin
    WHERE b.depth < 64
),
fwd (predecessor_isin, successor_isin, effective_date, detected_by, confidence, gap_sessions,
     symbol_at_change, corroborating_action, depth) AS (
    SELECT predecessor_isin, successor_isin, effective_date, detected_by, confidence,
           gap_sessions, symbol_at_change, corroborating_action, 1
    FROM isin_lineage WHERE predecessor_isin = %s
    UNION ALL
    SELECT l.predecessor_isin, l.successor_isin, l.effective_date, l.detected_by, l.confidence,
           l.gap_sessions, l.symbol_at_change, l.corroborating_action, f.depth + 1
    FROM isin_lineage l JOIN fwd f ON l.predecessor_isin = f.successor_isin
    WHERE f.depth < 64
)
SELECT predecessor_isin, successor_isin, effective_date, detected_by, confidence, gap_sessions,
       symbol_at_change, corroborating_action FROM back
UNION
SELECT predecessor_isin, successor_isin, effective_date, detected_by, confidence, gap_sessions,
       symbol_at_change, corroborating_action FROM fwd
ORDER BY effective_date, predecessor_isin
"""

_ACTIONS_SQL = """
SELECT id, ex_date, action_type, ratio_terms, dividend_amount_inr, record_date, knowable_date,
       source, reconciled, raw_text, filed_against_isin
FROM corporate_actions
WHERE isin = %s
ORDER BY ex_date DESC, action_type, source, id
"""

_FACTORS_SQL = """
SELECT ex_date, price_factor, qty_factor, cum_price_factor, cum_qty_factor, corporate_action_id,
       structural_break, computed_at
FROM adjustment_factors
WHERE isin = %s
ORDER BY ex_date DESC
"""

_INVALIDATIONS_SQL = """
SELECT id, reason, from_date, requested_at
FROM l2_invalidation
WHERE isin = %s AND NOT resolved
ORDER BY requested_at, id
"""

_SYMBOL_SEARCH_SQL = """
SELECT h.isin, m.name, m.status, h.exchange, h.symbol, h.series, h.valid_from, h.valid_to
FROM symbol_history h
JOIN security_master m ON m.isin = h.isin
WHERE upper(h.symbol) = upper(%s)
ORDER BY h.valid_from DESC, h.isin, h.exchange
LIMIT %s
"""


def read_isin_lookup(conn: Connection, isin: str) -> IsinLookupOut:
    """One ISIN's master row, symbol history, lineage chain, actions, factors and stale L2.

    What it assumes: `isin` is ISIN-shaped (`is_isin`); the caller checks, because a malformed
    value would only ever match nothing and an empty page would read as "no history".
    What it never does: take a symbol. Every query here is keyed on the ISIN (invariant #2).
    """
    row = conn.execute(_SECURITY_SQL, (isin,)).fetchone()
    security = (
        None
        if row is None
        else SecurityOut(
            isin=str(row[0]),
            name=str(row[1]),
            primary_exchange=str(row[2]),
            status=str(row[3]),
            face_value_inr=row[4],
            first_seen_date=row[5],
            last_seen_date=row[6],
            registered_by=row[7],
            updated_at=row[8],
        )
    )
    symbols = [
        SymbolWindowOut(
            exchange=str(r[0]),
            symbol=str(r[1]),
            series=r[2],
            valid_from=r[3],
            valid_to=r[4],
            source=str(r[5]),
        )
        for r in conn.execute(_SYMBOLS_SQL, (isin,)).fetchall()
    ]
    lineage = [
        LineageEdgeOut(
            predecessor_isin=str(r[0]),
            successor_isin=str(r[1]),
            effective_date=r[2],
            detected_by=str(r[3]),
            confidence=str(r[4]),
            gap_sessions=int(r[5]),
            symbol_at_change=str(r[6]),
            corroborating_action=r[7],
        )
        for r in conn.execute(_LINEAGE_SQL, (isin, isin)).fetchall()
    ]
    actions = [
        CorporateActionOut(
            id=int(r[0]),
            ex_date=r[1],
            action_type=str(r[2]),
            ratio_terms=r[3],
            dividend_amount_inr=r[4],
            record_date=r[5],
            knowable_date=r[6],
            source=str(r[7]),
            reconciled=bool(r[8]),
            raw_text=r[9],
            filed_against_isin=r[10],
        )
        for r in conn.execute(_ACTIONS_SQL, (isin,)).fetchall()
    ]
    factors = [
        FactorOut(
            ex_date=r[0],
            price_factor=r[1],
            qty_factor=r[2],
            cum_price_factor=r[3],
            cum_qty_factor=r[4],
            corporate_action_id=r[5],
            structural_break=bool(r[6]),
            computed_at=r[7],
        )
        for r in conn.execute(_FACTORS_SQL, (isin,)).fetchall()
    ]
    pending = [
        InvalidationOut(id=int(r[0]), reason=str(r[1]), from_date=r[2], requested_at=r[3])
        for r in conn.execute(_INVALIDATIONS_SQL, (isin,)).fetchall()
    ]
    return IsinLookupOut(
        isin=isin,
        security=security,
        symbols=symbols,
        lineage=lineage,
        actions=actions,
        factors=factors,
        pending_invalidations=pending,
    )


def search_symbol(
    conn: Connection, symbol: str, *, limit: int = SYMBOL_SEARCH_LIMIT
) -> SymbolSearchOut:
    """Every ISIN `symbol` has ever been valid for, on either exchange — candidates, not a join.

    What it does: matches `symbol_history.symbol` case-insensitively across all windows, because a
    symbol is reused across ISINs over time (a rename, a relisting) and the operator needs to see
    each one with the window it held the name.
    What it never does: resolve to one ISIN or read anything keyed by the symbol. Picking the ISIN
    is the operator's, and everything after that is `read_isin_lookup`.
    """
    query = symbol.strip()
    rows = conn.execute(_SYMBOL_SEARCH_SQL, (query, limit)).fetchall()
    return SymbolSearchOut(
        query=query,
        candidates=[
            SymbolCandidateOut(
                isin=str(r[0]),
                name=str(r[1]),
                status=str(r[2]),
                exchange=str(r[3]),
                symbol=str(r[4]),
                series=r[5],
                valid_from=r[6],
                valid_to=r[7],
            )
            for r in rows
        ],
        limit=limit,
    )
