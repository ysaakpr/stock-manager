"""D5: the §4.4 sync state machine — what we have for every (source, date), and whether we may act.

One row per `(source, logical_date)` moves `PENDING → FETCHED → VALIDATED → NORMALIZED →
PUBLISHED`, with two branches off the happy path: `FAILED`, carrying `retryable`, the running
`attempts` count and the `last_error` text, and `GAP`, meaning the exchange was shut and no file
was ever owed. Those two are what separate "we failed" from "there was nothing to fetch" — the
distinction the M1 gate's "gap report explains 100% of missing days" criterion rests on, and the
reason a missing bhavcopy is never a shrug.

The module is deliberately in two halves.

**Pure** — `SyncState`, `LEGAL_TRANSITIONS`, `SyncRecord.transition`, `expected_gap_kind` and
`evaluate_green` — knows nothing about Postgres and is where every rule lives. An illegal
transition raises here, naming both states, whether or not a database is anywhere nearby.

**Stored** — `SyncStateStore` — is the same rules against the `sync_state` table plus the C.2
calendar and an injected clock (B10). It never commits; the caller owns the transaction, exactly
as `dataplatform.store.db.connection` intends.

`is_green` is the one function the daily loop and the trading interlock call (invariant #10). It
answers a single question — *may today's decisions be made on this date's data?* — and answers it
closed: an unknown date, a missing row, an unpublished dataset or an open ERROR-severity quality
flag all mean "no". §4.4 says the interlock wants the core datasets `PUBLISHED` **and**
quality-green, so both halves are checked here rather than left for the caller to remember.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Any, Final

from dataplatform.clock import Clock, SystemClock
from dataplatform.config import Settings
from dataplatform.ingest.calendar import (
    CalendarCoverageError,
    DayKind,
    TradingCalendar,
    trading_calendar,
)
from dataplatform.logging import get_logger
from dataplatform.store.db import Connection, connection

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

_log = get_logger(__name__)


class SyncState(StrEnum):
    """Where one `(source, date)` pair has got to. Mirrors the CHECK constraint in 0001_init.sql."""

    PENDING = "PENDING"
    """An attempt has begun (or is about to). Nothing has been fetched yet."""

    FETCHED = "FETCHED"
    """The raw payload is in L0 with a checksum. Nothing has been parsed."""

    VALIDATED = "VALIDATED"
    """The payload parsed and passed its structural checks. Nothing is in L1 yet."""

    NORMALIZED = "NORMALIZED"
    """Rows are written to L1, ISIN-keyed. Not yet visible to readers."""

    PUBLISHED = "PUBLISHED"
    """Terminal, successful. This is the only state the trading interlock accepts."""

    FAILED = "FAILED"
    """The attempt broke. `attempts`, `retryable` and `last_error` say how badly."""

    GAP = "GAP"
    """The exchange was closed — a holiday or a weekend. No file was ever owed."""

    @property
    def is_terminal(self) -> bool:
        """Whether the ingestion pipeline is finished with a row in this state.

        `FAILED` is not terminal even though §4.4 draws it as a branch: a retryable failure is
        exactly the row a backfill run picks up again tomorrow.
        """
        return self in (SyncState.PUBLISHED, SyncState.GAP)


#: Every transition this system permits. Anything absent raises, naming both states.
#:
#: Three edges deserve their reason in writing:
#:
#: * `PENDING → PENDING` — a fresh attempt on a date already pending. A runner that died between
#:   `begin()` and the fetch must be able to start over, and that restart is a real attempt, so it
#:   increments `attempts` rather than pretending nothing happened.
#: * `FAILED → PENDING` — the retry, and only when the failure was marked `retryable`. A
#:   non-retryable failure (a 404 on a date the exchange never published, a parser that cannot
#:   handle this era) is a dead end on purpose: re-driving it forever is how a backfill run turns
#:   into a hot loop against a source that is telling it to stop.
#: * `GAP → PENDING` — the calendar was wrong. C.2's `Reconciliation.unexpected` is precisely the
#:   case where data exists on a date the holiday file calls closed; once the file is corrected the
#:   date owes us a fetch, and a strictly terminal `GAP` would leave that row stranded forever.
LEGAL_TRANSITIONS: Final[Mapping[SyncState, frozenset[SyncState]]] = {
    SyncState.PENDING: frozenset(
        {SyncState.PENDING, SyncState.FETCHED, SyncState.FAILED, SyncState.GAP}
    ),
    SyncState.FETCHED: frozenset({SyncState.VALIDATED, SyncState.FAILED}),
    SyncState.VALIDATED: frozenset({SyncState.NORMALIZED, SyncState.FAILED}),
    SyncState.NORMALIZED: frozenset({SyncState.PUBLISHED, SyncState.FAILED}),
    SyncState.PUBLISHED: frozenset(),
    SyncState.FAILED: frozenset({SyncState.PENDING}),
    SyncState.GAP: frozenset({SyncState.PENDING}),
}

#: The two states §4.4 draws as terminal — the ingestion pipeline is finished with a row in either
#: and will never drive it again. `PUBLISHED` is closed absolutely, because L0 is immutable
#: (invariant #1) and the same (source, date) can never yield different bytes, so there is nothing
#: to re-publish. `GAP` is closed to the pipeline but not to a correction of the calendar that put
#: it there, which is its one outgoing edge above.
TERMINAL_STATES: Final[frozenset[SyncState]] = frozenset({SyncState.PUBLISHED, SyncState.GAP})

#: States with no outgoing edge whatsoever. Separate from `TERMINAL_STATES` on purpose: the two
#: sets differ by exactly `GAP`, and conflating them is how the calendar-correction path would get
#: quietly deleted by someone tidying up.
CLOSED_STATES: Final[frozenset[SyncState]] = frozenset(
    state for state, allowed in LEGAL_TRANSITIONS.items() if not allowed
)


# ── errors ───────────────────────────────────────────────────────────────────────────────────


class SyncStateError(Exception):
    """Base for every sync-state failure, so callers can catch the family."""


class IllegalTransitionError(SyncStateError):
    """A transition the state machine does not permit.

    Always names both states and the `(source, date)` it was attempted on: a state-machine error
    that says only "invalid transition" costs an operator the twenty minutes of grepping that the
    message could have saved.
    """

    def __init__(
        self,
        source: str,
        logical_date: date,
        from_state: SyncState,
        to_state: SyncState,
        reason: str,
    ) -> None:
        self.source = source
        self.logical_date = logical_date
        self.from_state = from_state
        self.to_state = to_state
        super().__init__(
            f"{source} {logical_date.isoformat()}: "
            f"{from_state.value} -> {to_state.value} is not a legal sync transition ({reason})"
        )


class NotAGapError(SyncStateError):
    """`GAP` was claimed for a date the calendar says the exchange traded.

    This is the guard behind "GAP(expected) ... never for a real miss": without it, a fetch failure
    could be filed as a holiday and vanish from the gap report, which is the one outcome the M1
    gate exists to make impossible.
    """


class UnknownSyncRowError(SyncStateError):
    """A transition was attempted on a `(source, date)` that has no row yet."""


# ── the record ───────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SyncRecord:
    """One `sync_state` row, as a value.

    What it does: carries the state of a single `(source, logical_date)` pair and applies §4.4's
    transitions to it, returning a new record.
    What it assumes: `updated_at` came from an injected clock — nothing here reads the wall clock.
    What it never does: mutate. A transition returns a new record, so the caller always holds both
    the before and the after, which is what makes the log line and the test assertion honest.
    """

    source: str
    logical_date: date
    state: SyncState
    updated_at: datetime
    attempts: int = 0
    retryable: bool = True
    last_error: str | None = None
    checksum: str | None = None
    l0_path: str | None = None
    first_attempt_at: datetime | None = None

    @property
    def key(self) -> tuple[str, date]:
        """The primary key of the row, for logging and dict lookups."""
        return (self.source, self.logical_date)

    def can_transition_to(self, to_state: SyncState) -> bool:
        """Whether `transition` would succeed for `to_state`, without raising to find out."""
        if to_state not in LEGAL_TRANSITIONS[self.state]:
            return False
        return not (self.state is SyncState.FAILED and not self.retryable)

    def check_transition(self, to_state: SyncState) -> None:
        """Raise `IllegalTransitionError` unless `to_state` is reachable from here.

        Two distinct refusals, and the message says which: the edge does not exist at all, or the
        edge exists but this particular failure was marked non-retryable.
        """
        if self.state is SyncState.FAILED and to_state is SyncState.PENDING and not self.retryable:
            raise IllegalTransitionError(
                self.source,
                self.logical_date,
                self.state,
                to_state,
                "the failure was recorded as non-retryable; "
                f"last error: {self.last_error or 'unrecorded'}",
            )
        if to_state in LEGAL_TRANSITIONS[self.state]:
            return
        allowed = sorted(state.value for state in LEGAL_TRANSITIONS[self.state])
        raise IllegalTransitionError(
            self.source,
            self.logical_date,
            self.state,
            to_state,
            f"{self.state.value} is terminal and nothing leaves it"
            if self.state in CLOSED_STATES
            else f"legal from {self.state.value}: {', '.join(allowed)}",
        )

    def transition(
        self,
        to_state: SyncState,
        *,
        at: datetime,
        error: str | None = None,
        retryable: bool | None = None,
        checksum: str | None = None,
        l0_path: str | None = None,
    ) -> SyncRecord:
        """Apply one §4.4 transition, returning the resulting record.

        What it does: validates the edge, then carries the bookkeeping §4.4 requires —
        `attempts` counts every attempt *started* (so a fetch that failed on its first try shows
        `attempts == 1`, not 0), and `last_error` is never cleared, because a date that took three
        goes to land is worth knowing about long after it finally published.
        What it assumes: `at` is tz-aware and comes from an injected clock.
        What it never does: invent a reason. `FAILED` without an error message is rejected — a
        failure nobody can act on is barely better than a silent one (CLAUDE.md, "fail loud").
        """
        self.check_transition(to_state)
        if to_state is SyncState.FAILED:
            if not (error and error.strip()):
                raise ValueError(
                    f"{self.source} {self.logical_date.isoformat()}: a FAILED sync_state row "
                    f"needs a specific last_error; refusing to record a failure with no reason"
                )
        elif error is not None or retryable is not None:
            raise ValueError(
                f"`error` and `retryable` describe a failure and may only be passed with "
                f"FAILED, not {to_state.value}"
            )

        starting_attempt = to_state is SyncState.PENDING
        return replace(
            self,
            state=to_state,
            updated_at=at,
            attempts=self.attempts + 1 if starting_attempt else self.attempts,
            # `retryable` only means anything on a FAILED row; every other state resets it to the
            # default so a stale `false` from an old failure cannot block a later retry.
            retryable=(retryable if retryable is not None else True)
            if to_state is SyncState.FAILED
            else True,
            last_error=error if error is not None else self.last_error,
            checksum=checksum if checksum is not None else self.checksum,
            l0_path=l0_path if l0_path is not None else self.l0_path,
            first_attempt_at=(
                at if self.first_attempt_at is None and starting_attempt else self.first_attempt_at
            ),
        )


# ── pure rules the store leans on ────────────────────────────────────────────────────────────


def expected_gap_kind(logical_date: date, *, calendar: TradingCalendar) -> DayKind:
    """The reason `logical_date` legitimately has no data, or raise `NotAGapError`.

    What it does: classifies the date against the C.2 calendar and returns `WEEKEND` or `HOLIDAY`.
    What it assumes: the calendar covers the date — outside its coverage it raises
    `CalendarCoverageError` rather than guessing, because "probably not a trading day" is how a
    real miss gets filed as a holiday.
    What it never does: return a kind that expects data. A `SESSION` or `MUHURAT` date reaching
    here is a fetch that failed, and it is refused so it stays in the gap report where it belongs.
    """
    kind = calendar.classify(logical_date)
    if kind.expects_data:
        raise NotAGapError(
            f"{logical_date.isoformat()} is a {kind.value} — the exchange published that day, so "
            f"an absent file is a real miss, not a GAP. Record FAILED with the reason instead."
        )
    return kind


@dataclass(frozen=True, slots=True)
class GreenStatus:
    """The trading interlock's answer for one date (invariant #10, §4.4).

    Truthy exactly when every requested dataset is `PUBLISHED` and no ERROR-severity quality flag
    is open. `reason` is the one line the journal records when it is not — a `SKIPPED_DATA_RED`
    entry that does not say *why* the data was red is a heartbeat, not evidence.
    """

    logical_date: date
    green: bool
    reason: str
    datasets: tuple[str, ...]
    published: tuple[str, ...]
    missing: tuple[str, ...]
    not_published: tuple[tuple[str, SyncState], ...]
    open_error_flags: int
    day_kind: DayKind | None

    def __bool__(self) -> bool:
        """`if is_green(...)` reads the way the interlock is described in §4.4."""
        return self.green


def evaluate_green(
    logical_date: date,
    datasets: Sequence[str],
    records: Mapping[str, SyncRecord],
    *,
    day_kind: DayKind | None,
    open_error_flags: int = 0,
) -> GreenStatus:
    """Decide whether `logical_date` is safe to trade on, from already-gathered facts.

    Split out from the store so the rule is testable without a database, and so there is exactly
    one place where "green" is defined. Checks run in the order an operator would ask them, and
    the first failure wins the `reason`:

    1. the date is inside the calendar's coverage at all;
    2. the exchange actually traded that day;
    3. every requested dataset has a row;
    4. every one of those rows is `PUBLISHED`;
    5. no ERROR-severity quality flag for the date is open.

    Fails closed at every step: an unknown date is not green, and neither is a date whose datasets
    were never asked for. Passing no datasets is a caller bug, not an empty conjunction that
    trivially returns green.
    """
    wanted = tuple(dict.fromkeys(datasets))
    if not wanted:
        raise ValueError(
            "is_green needs the datasets the decision depends on; an empty list would make every "
            "date green by vacuous truth, which is the exact failure invariant #10 forbids"
        )

    published = tuple(
        name for name in wanted if name in records and records[name].state is SyncState.PUBLISHED
    )
    missing = tuple(name for name in wanted if name not in records)
    not_published = tuple(
        (name, records[name].state)
        for name in wanted
        if name in records and records[name].state is not SyncState.PUBLISHED
    )

    def status(green: bool, reason: str) -> GreenStatus:
        return GreenStatus(
            logical_date=logical_date,
            green=green,
            reason=reason,
            datasets=wanted,
            published=published,
            missing=missing,
            not_published=not_published,
            open_error_flags=open_error_flags,
            day_kind=day_kind,
        )

    if day_kind is None:
        return status(
            False,
            f"{logical_date.isoformat()} is outside the trading calendar's coverage, so whether "
            f"data is owed for it is unknown",
        )
    if not day_kind.expects_data:
        return status(
            False,
            f"{logical_date.isoformat()} is a {day_kind.value} — the exchange published nothing, "
            f"so there is no fresh data to decide on",
        )
    if missing:
        return status(
            False, f"no sync_state row for {', '.join(missing)} on {logical_date.isoformat()}"
        )
    if not_published:
        detail = ", ".join(f"{name}={state.value}" for name, state in not_published)
        return status(False, f"not PUBLISHED on {logical_date.isoformat()}: {detail}")
    if open_error_flags:
        return status(
            False,
            f"{open_error_flags} open ERROR-severity quality flag(s) for "
            f"{logical_date.isoformat()}",
        )
    return status(
        True, f"all {len(wanted)} dataset(s) PUBLISHED and quality-green on {logical_date}"
    )


@dataclass(frozen=True, slots=True)
class SourceStatus:
    """One source's line in `/status/sources`: last success, lag, failure streak.

    `lag_sessions` is the honest measure — calendar days count a long weekend as three days of
    lateness when the exchange was shut for two of them — and is `None` only when the span leaves
    the C.2 calendar's coverage, where a number would be a guess.
    """

    source: str
    last_success_date: date | None
    last_success_at: datetime | None
    latest_date: date | None
    lag_days: int | None
    lag_sessions: int | None
    failure_streak: int
    last_failure_date: date | None
    last_error: str | None
    last_failure_retryable: bool | None
    counts: Mapping[SyncState, int]

    @property
    def healthy(self) -> bool:
        """A source with a success, no current failure streak and nothing stuck mid-pipeline."""
        in_flight = sum(
            self.counts.get(state, 0)
            for state in (SyncState.FETCHED, SyncState.VALIDATED, SyncState.NORMALIZED)
        )
        return self.last_success_date is not None and self.failure_streak == 0 and in_flight == 0


# ── the store ────────────────────────────────────────────────────────────────────────────────

#: Column order shared by every read and the upsert, so the two can never drift apart.
_COLUMNS: Final[str] = (
    "source, logical_date, state, attempts, retryable, last_error, checksum, l0_path, "
    "first_attempt_at, updated_at"
)

_UPSERT: Final[str] = f"""
    INSERT INTO sync_state ({_COLUMNS})
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (source, logical_date) DO UPDATE SET
        state            = EXCLUDED.state,
        attempts         = EXCLUDED.attempts,
        retryable        = EXCLUDED.retryable,
        last_error       = EXCLUDED.last_error,
        checksum         = EXCLUDED.checksum,
        l0_path          = EXCLUDED.l0_path,
        first_attempt_at = EXCLUDED.first_attempt_at,
        updated_at       = EXCLUDED.updated_at
"""

#: Per-source aggregate behind `/status/sources`. The `streak` CTE counts FAILED dates newer than
#: the newest date that is neither FAILED nor GAP — so a holiday in the middle of a bad run does
#: not reset the streak, while a single success does, which is what "failure streak" has to mean
#: for an operator deciding whether a source is broken right now.
_SOURCE_STATUS_SQL: Final[str] = """
    WITH agg AS (
        SELECT source,
               max(logical_date) FILTER (WHERE state = 'PUBLISHED') AS last_success_date,
               max(updated_at)   FILTER (WHERE state = 'PUBLISHED') AS last_success_at,
               max(logical_date)                                    AS latest_date,
               count(*) FILTER (WHERE state = 'PENDING')            AS pending,
               count(*) FILTER (WHERE state = 'FETCHED')            AS fetched,
               count(*) FILTER (WHERE state = 'VALIDATED')          AS validated,
               count(*) FILTER (WHERE state = 'NORMALIZED')         AS normalized,
               count(*) FILTER (WHERE state = 'PUBLISHED')          AS published,
               count(*) FILTER (WHERE state = 'FAILED')             AS failed,
               count(*) FILTER (WHERE state = 'GAP')                AS gap
        FROM sync_state
        GROUP BY source
    ),
    last_failure AS (
        SELECT DISTINCT ON (source) source, logical_date, last_error, retryable
        FROM sync_state
        WHERE state = 'FAILED'
        ORDER BY source, logical_date DESC
    ),
    streak AS (
        SELECT s.source, count(*) AS failure_streak
        FROM sync_state s
        WHERE s.state = 'FAILED'
          AND s.logical_date > COALESCE((
                  SELECT max(t.logical_date) FROM sync_state t
                  WHERE t.source = s.source AND t.state NOT IN ('FAILED', 'GAP')
              ), '-infinity'::date)
        GROUP BY s.source
    )
    SELECT agg.source, agg.last_success_date, agg.last_success_at, agg.latest_date,
           agg.pending, agg.fetched, agg.validated, agg.normalized, agg.published,
           agg.failed, agg.gap,
           COALESCE(streak.failure_streak, 0),
           last_failure.logical_date, last_failure.last_error, last_failure.retryable
    FROM agg
    LEFT JOIN streak       ON streak.source = agg.source
    LEFT JOIN last_failure ON last_failure.source = agg.source
    ORDER BY agg.source
"""


class SyncStateStore:
    """The §4.4 state machine over the `sync_state` table.

    What it does: creates, advances and reads one row per `(source, logical_date)`, applying
    `SyncRecord`'s rules, and answers the two status-API questions and the interlock question.
    What it assumes: the schema is migrated, and the caller owns the transaction — nothing here
    commits, matching `dataplatform.store.db.connection`, so a runner can drive a whole date
    through the pipeline and roll the lot back if a later step explodes.
    What it never does: read the wall clock, or decide that a missing file was a holiday. The
    first comes from the injected `Clock` (B10); the second comes from the C.2 calendar.
    """

    def __init__(
        self,
        conn: Connection,
        *,
        clock: Clock | None = None,
        calendar: TradingCalendar | None = None,
    ) -> None:
        self._conn = conn
        self._clock = SystemClock() if clock is None else clock
        self._calendar = trading_calendar() if calendar is None else calendar

    @property
    def calendar(self) -> TradingCalendar:
        """The calendar this store classifies dates against (C.2)."""
        return self._calendar

    # ── reads ────────────────────────────────────────────────────────────────────────────────

    def get(self, source: str, logical_date: date) -> SyncRecord | None:
        """The row for one `(source, date)`, or None when the pair was never touched."""
        row = self._conn.execute(
            f"SELECT {_COLUMNS} FROM sync_state WHERE source = %s AND logical_date = %s",
            (source, logical_date),
        ).fetchone()
        return None if row is None else _record(row)

    def require(self, source: str, logical_date: date) -> SyncRecord:
        """The row for one `(source, date)`, or raise. For callers that know it must exist."""
        record = self.get(source, logical_date)
        if record is None:
            raise UnknownSyncRowError(
                f"no sync_state row for ({source}, {logical_date.isoformat()}); "
                f"call begin() before advancing it"
            )
        return record

    def rows_for_date(self, logical_date: date) -> tuple[SyncRecord, ...]:
        """Every source's row for one date, source-ordered. Backs `/status/sync?date=`."""
        rows = self._conn.execute(
            f"SELECT {_COLUMNS} FROM sync_state WHERE logical_date = %s ORDER BY source",
            (logical_date,),
        ).fetchall()
        return tuple(_record(row) for row in rows)

    def day_kind(self, logical_date: date) -> DayKind | None:
        """What the calendar calls this date, or None when it is outside coverage.

        None rather than an exception: `/status/sync` for a 2011 date is a reasonable question
        with an honest answer ("the calendar makes no claim"), and `evaluate_green` treats that
        answer as not-green.
        """
        try:
            return self._calendar.classify(logical_date)
        except CalendarCoverageError:
            return None

    def open_error_flags(self, logical_date: date, sources: Sequence[str]) -> int:
        """Unresolved ERROR-severity D7 flags for the date, market-wide or on these sources.

        The "quality-green" half of §4.4's interlock. `WARN` and `INFO` are informational and do
        not stop trading; a flag with a NULL source is about the whole market date and counts for
        every dataset.
        """
        row = self._conn.execute(
            "SELECT count(*) FROM quality_flag "
            "WHERE logical_date = %s AND severity = 'ERROR' AND NOT resolved "
            "AND (source IS NULL OR source = ANY(%s))",
            (logical_date, list(sources)),
        ).fetchone()
        return 0 if row is None else int(row[0])

    # ── writes ───────────────────────────────────────────────────────────────────────────────

    def begin(self, source: str, logical_date: date) -> SyncRecord:
        """Start (or restart) an ingestion attempt, leaving the row `PENDING`.

        Creates the row when the pair is new, and otherwise transitions the existing one, which is
        what makes a backfill run resumable: a `PUBLISHED` date refuses to be re-begun, a
        retryable `FAILED` one comes back to `PENDING`, and either way `attempts` counts the try.
        """
        existing = self.get(source, logical_date)
        now = self._clock.now()
        if existing is None:
            record = SyncRecord(
                source=source,
                logical_date=logical_date,
                state=SyncState.PENDING,
                updated_at=now,
                attempts=1,
                first_attempt_at=now,
            )
        else:
            record = existing.transition(SyncState.PENDING, at=now)
        return self._write(record, previous=existing)

    def advance(
        self,
        source: str,
        logical_date: date,
        to_state: SyncState,
        *,
        error: str | None = None,
        retryable: bool | None = None,
        checksum: str | None = None,
        l0_path: str | None = None,
    ) -> SyncRecord:
        """Move an existing row to `to_state`, persisting the result.

        The general form; the `mark_*` helpers below are the readable spellings the ingestion
        code should use. Raises `IllegalTransitionError` (naming both states) rather than writing
        anything when the edge is not permitted.
        """
        current = self.require(source, logical_date)
        record = current.transition(
            to_state,
            at=self._clock.now(),
            error=error,
            retryable=retryable,
            checksum=checksum,
            l0_path=l0_path,
        )
        return self._write(record, previous=current)

    def mark_fetched(
        self, source: str, logical_date: date, *, checksum: str, l0_path: str | None = None
    ) -> SyncRecord:
        """The payload is in L0. The checksum is recorded so corruption is detectable later."""
        return self.advance(
            source, logical_date, SyncState.FETCHED, checksum=checksum, l0_path=l0_path
        )

    def mark_validated(self, source: str, logical_date: date) -> SyncRecord:
        """The payload parsed and passed its structural checks."""
        return self.advance(source, logical_date, SyncState.VALIDATED)

    def mark_normalized(self, source: str, logical_date: date) -> SyncRecord:
        """The rows are in L1, ISIN-keyed."""
        return self.advance(source, logical_date, SyncState.NORMALIZED)

    def mark_published(self, source: str, logical_date: date) -> SyncRecord:
        """Readers may now see this date. The only state the trading interlock accepts."""
        return self.advance(source, logical_date, SyncState.PUBLISHED)

    def mark_failed(
        self, source: str, logical_date: date, error: str, *, retryable: bool = True
    ) -> SyncRecord:
        """Record a specific failure. `retryable=False` closes the date to further attempts."""
        return self.advance(
            source, logical_date, SyncState.FAILED, error=error, retryable=retryable
        )

    def mark_gap(self, source: str, logical_date: date) -> SyncRecord:
        """Record that the exchange was shut on `logical_date`, so no file was ever owed.

        What it does: asks the C.2 calendar first and files the weekend or holiday as `GAP`.
        What it assumes: the calendar is right — which is why C.2 ships `reconcile()` to check it
        against real bhavcopy availability.
        What it never does: accept a `GAP` for a date the calendar calls a session. That raises
        `NotAGapError`, because a real miss dressed as a holiday is invisible to the gap report
        and the M1 gate would pass on a lie.
        """
        kind = expected_gap_kind(logical_date, calendar=self._calendar)
        existing = self.get(source, logical_date)
        now = self._clock.now()
        if existing is None:
            # Never attempted, so `attempts` stays 0: nothing was tried and nothing failed.
            record = SyncRecord(
                source=source,
                logical_date=logical_date,
                state=SyncState.GAP,
                updated_at=now,
                last_error=None,
            )
        else:
            record = existing.transition(SyncState.GAP, at=now)
        _log.info("sync_state.gap", source=source, date=logical_date.isoformat(), kind=kind.value)
        return self._write(record, previous=existing)

    def _write(self, record: SyncRecord, *, previous: SyncRecord | None) -> SyncRecord:
        """Persist one record and log the transition. One event per meaningful step (CLAUDE.md)."""
        self._conn.execute(
            _UPSERT,
            (
                record.source,
                record.logical_date,
                record.state.value,
                record.attempts,
                record.retryable,
                record.last_error,
                record.checksum,
                record.l0_path,
                record.first_attempt_at,
                record.updated_at,
            ),
        )
        _log.info(
            "sync_state.transition",
            source=record.source,
            date=record.logical_date.isoformat(),
            from_state=None if previous is None else previous.state.value,
            to_state=record.state.value,
            attempts=record.attempts,
            retryable=record.retryable,
            error=record.last_error if record.state is SyncState.FAILED else None,
        )
        return record

    # ── the status surface ───────────────────────────────────────────────────────────────────

    def is_green(self, logical_date: date, datasets: Iterable[str]) -> GreenStatus:
        """Invariant #10: may the agent trade on `logical_date`'s data?

        What it does: gathers this date's rows and open quality flags and applies
        `evaluate_green`. Green means every named dataset is `PUBLISHED` and no ERROR-severity
        flag is open for the date.
        What it assumes: `datasets` are the `source` ids the decision actually depends on — the
        caller names them, because "core" is a policy the data platform has no business inventing.
        What it never does: return green on incomplete evidence. Every unknown is a red.
        """
        wanted = tuple(dict.fromkeys(datasets))
        records = {
            record.source: record
            for record in self.rows_for_date(logical_date)
            if record.source in set(wanted)
        }
        flags = self.open_error_flags(logical_date, wanted) if wanted else 0
        status = evaluate_green(
            logical_date,
            wanted,
            records,
            day_kind=self.day_kind(logical_date),
            open_error_flags=flags,
        )
        _log.info(
            "sync_state.is_green",
            date=logical_date.isoformat(),
            green=status.green,
            reason=status.reason,
            datasets=list(wanted),
        )
        return status

    def source_statuses(self) -> tuple[SourceStatus, ...]:
        """Per-source last success, lag and failure streak — the `/status/sources` payload.

        Reports the sources that have rows. A source the platform has never touched has nothing
        true to say about its lag, and inventing a line for it would be the fabricated data the
        status API is not allowed to serve.
        """
        as_of = self._clock.today()
        rows = self._conn.execute(_SOURCE_STATUS_SQL).fetchall()
        return tuple(self._source_status(row, as_of=as_of) for row in rows)

    def _source_status(self, row: tuple[Any, ...], *, as_of: date) -> SourceStatus:
        last_success: date | None = row[1]
        return SourceStatus(
            source=str(row[0]),
            last_success_date=last_success,
            last_success_at=row[2],
            latest_date=row[3],
            lag_days=None if last_success is None else max((as_of - last_success).days, 0),
            lag_sessions=self._lag_sessions(last_success, as_of),
            failure_streak=int(row[11]),
            last_failure_date=row[12],
            last_error=None if row[13] is None else str(row[13]),
            last_failure_retryable=row[14],
            counts={
                SyncState.PENDING: int(row[4]),
                SyncState.FETCHED: int(row[5]),
                SyncState.VALIDATED: int(row[6]),
                SyncState.NORMALIZED: int(row[7]),
                SyncState.PUBLISHED: int(row[8]),
                SyncState.FAILED: int(row[9]),
                SyncState.GAP: int(row[10]),
            },
        )

    def _lag_sessions(self, last_success: date | None, as_of: date) -> int | None:
        """Trading sessions owed since the last success, or None when the span is unknowable.

        Counts dates that *expected data* — so a source last successful on Friday is 0 sessions
        behind on Saturday, not 1. `None` when the span leaves the calendar's coverage: a source
        whose last success predates the holiday file cannot be measured, and a fabricated number
        there would be read as fact.
        """
        if last_success is None or last_success >= as_of:
            return None if last_success is None else 0
        try:
            return len(self._calendar.expected_data_dates(last_success + timedelta(days=1), as_of))
        except CalendarCoverageError:
            return None


def _record(row: tuple[Any, ...]) -> SyncRecord:
    """Build a `SyncRecord` from a row selected in `_COLUMNS` order."""
    return SyncRecord(
        source=str(row[0]),
        logical_date=row[1],
        state=SyncState(row[2]),
        attempts=int(row[3]),
        retryable=bool(row[4]),
        last_error=None if row[5] is None else str(row[5]),
        checksum=None if row[6] is None else str(row[6]),
        l0_path=None if row[7] is None else str(row[7]),
        first_attempt_at=row[8],
        updated_at=row[9],
    )


def is_green(
    logical_date: date,
    datasets: Iterable[str],
    *,
    settings: Settings | None = None,
    clock: Clock | None = None,
    calendar: TradingCalendar | None = None,
) -> GreenStatus:
    """The trading interlock, as one call the daily loop can make (§4.4, invariant #10).

    What it does: opens a connection, asks `SyncStateStore.is_green` and closes it again.
    What it assumes: the database is reachable. It is not caught here — a status check that
    reports green when it could not reach the store would be the worst possible failure mode, and
    the loop must see the exception and journal `SKIPPED_DATA_RED`.
    What it never does: write anything. This is a read, and it holds no transaction open.
    """
    with connection(settings) as conn:
        return SyncStateStore(conn, clock=clock, calendar=calendar).is_green(logical_date, datasets)
