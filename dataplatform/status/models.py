"""D5: the wire contracts for the §4.4 status API.

These models are the boundary other modules read — the agent's daily loop, the M1.11 gap report,
the future product UI — so they are declared once here rather than inlined in the route handlers.
Every field traces to a `sync_state`, `quality_flag`, `scheduler_heartbeat` or `archive_bundle`
row, or to the C.2 calendar; nothing on this surface is a placeholder, because a status API that
fabricates a value is worse than one that is down (§4.4's whole point is that bad data must never
silently become decisions).

Two conventions the whole file follows:

* **`None` means unknown or absent, never zero.** A source with no successful fetch has
  `last_success_date=None`, not an epoch date; a scheduler that has never beaten has
  `last_beat_at=None`, not the current instant. A status API that rounds "we do not know" up to a
  number is how a red day gets read as a green one.
* **Frozen, and closed to undeclared fields.** A route that quietly grows a key fails here rather
  than in whatever consumes it six months later.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from dataplatform.status.sync_state import GreenStatus, SourceStatus, SyncRecord, SyncState

#: `datetime.date` under a second name. Several models here carry a field called `date` — the
#: §4.4 query parameter is spelled that way on the wire — and a field shadows the type of the same
#: name for the rest of the class body, so any method signature inside such a class uses this.
type TradingDate = date

__all__ = [
    "ArchiveBundleOut",
    "ArchiveFileOut",
    "ArchivesOut",
    "DatabaseHealthOut",
    "GapEntryOut",
    "GapsOut",
    "HealthOut",
    "QualityFlagOut",
    "QualityOut",
    "QualitySeverity",
    "SchedulerHealthOut",
    "SchedulerState",
    "ServiceStatus",
    "SeverityCountOut",
    "SourceStatusOut",
    "SourcesOut",
    "SyncRowOut",
    "SyncStatusOut",
]


class SyncRowOut(BaseModel):
    """One `(source, date)` row of the state machine, as served."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: str
    state: SyncState
    attempts: int
    retryable: bool
    last_error: str | None
    checksum: str | None
    l0_path: str | None
    first_attempt_at: datetime | None
    updated_at: datetime

    @classmethod
    def of(cls, record: SyncRecord) -> SyncRowOut:
        """Project a stored record onto the wire contract."""
        return cls(
            source=record.source,
            state=record.state,
            attempts=record.attempts,
            retryable=record.retryable,
            last_error=record.last_error,
            checksum=record.checksum,
            l0_path=record.l0_path,
            first_attempt_at=record.first_attempt_at,
            updated_at=record.updated_at,
        )


class SyncStatusOut(BaseModel):
    """`GET /status/sync?date=` — every source's state for one date, plus the interlock verdict.

    `green` is `null` when the caller named no datasets: whether a date is safe to trade on
    depends on which datasets the decision needs, and answering without being asked would be a
    guess dressed as a fact. The daily loop always passes `dataset=`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    date: date
    day_kind: str | None = Field(
        description="SESSION | MUHURAT | WEEKEND | HOLIDAY from the C.2 calendar; "
        "null outside its coverage"
    )
    expects_data: bool | None = Field(
        description="Whether the exchange should have published for this date; "
        "null outside calendar coverage"
    )
    datasets: list[str] = Field(description="The datasets `green` was evaluated over")
    green: bool | None = Field(description="Invariant #10's verdict; null when no dataset asked")
    reason: str | None = Field(description="Why the verdict came out that way, for the journal")
    missing: list[str] = Field(description="Requested datasets with no row for this date")
    open_error_flags: int = Field(description="Unresolved ERROR-severity D7 flags for this date")
    rows: list[SyncRowOut]

    @classmethod
    def of(
        cls,
        logical_date: TradingDate,
        rows: tuple[SyncRecord, ...],
        *,
        day_kind: str | None,
        expects_data: bool | None,
        green: GreenStatus | None,
    ) -> SyncStatusOut:
        """Assemble the payload from the store's answers.

        Every test below is `green is not None`, never `if green`: `GreenStatus` is falsy exactly
        when the date is red, so the shorter spelling would report a red day as "no verdict" — the
        one mistranslation this endpoint must never make.
        """
        return cls(
            date=logical_date,
            day_kind=day_kind,
            expects_data=expects_data,
            datasets=list(green.datasets) if green is not None else [],
            green=green.green if green is not None else None,
            reason=green.reason if green is not None else None,
            missing=list(green.missing) if green is not None else [],
            open_error_flags=green.open_error_flags if green is not None else 0,
            rows=[SyncRowOut.of(record) for record in rows],
        )


class SourceStatusOut(BaseModel):
    """One source's line in `GET /status/sources`: last success, lag, failure streak."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: str
    last_success_date: date | None
    last_success_at: datetime | None
    latest_date: date | None
    lag_days: int | None
    lag_sessions: int | None = Field(
        description="Sessions that expected data since the last success; null when the span "
        "leaves the calendar's coverage"
    )
    failure_streak: int = Field(description="Consecutive FAILED dates since the last non-failure")
    last_failure_date: date | None
    last_error: str | None
    last_failure_retryable: bool | None
    healthy: bool
    counts: dict[SyncState, int]

    @classmethod
    def of(cls, status: SourceStatus) -> SourceStatusOut:
        """Project a computed source status onto the wire contract."""
        return cls(
            source=status.source,
            last_success_date=status.last_success_date,
            last_success_at=status.last_success_at,
            latest_date=status.latest_date,
            lag_days=status.lag_days,
            lag_sessions=status.lag_sessions,
            failure_streak=status.failure_streak,
            last_failure_date=status.last_failure_date,
            last_error=status.last_error,
            last_failure_retryable=status.last_failure_retryable,
            healthy=status.healthy,
            counts=dict(status.counts),
        )


class SourcesOut(BaseModel):
    """`GET /status/sources` — every source the platform has ever recorded a row for."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    as_of: date = Field(description="The trading date lag is measured against (injected clock)")
    sources: list[SourceStatusOut]


# ── /health ─────────────────────────────────────────────────────────────────────────────────


class ServiceStatus(StrEnum):
    """The one-word verdict of `GET /health` — what an uptime check reads."""

    OK = "OK"
    DEGRADED = "DEGRADED"


class SchedulerState(StrEnum):
    """What `/health` can say about the scheduler, and why that is three values and not two.

    `NEVER_RAN` and `STALE` both mean "no recent heartbeat", but they are different operational
    facts: a scheduler that has never beaten is a deployment where none has started yet (the state
    of every freshly migrated database, and of this stack until M0.6 runs), while a scheduler whose
    newest beat has aged out is one that started and then stopped. Only the second is an outage, so
    only the second fails the probe on its own account.

    `UNKNOWN` is the fourth because the heartbeat lives in Postgres: when the database is
    unreachable there is no evidence either way, and reporting that as `NEVER_RAN` would be this
    API inventing the one fact it was unable to check.
    """

    OK = "OK"
    STALE = "STALE"
    NEVER_RAN = "NEVER_RAN"
    UNKNOWN = "UNKNOWN"


class DatabaseHealthOut(BaseModel):
    """Whether this process can reach Postgres, and what it hit if it cannot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reachable: bool
    error: str | None = Field(
        default=None, description="The connection failure verbatim; null when reachable"
    )


class SchedulerHealthOut(BaseModel):
    """Age of the newest scheduler heartbeat, measured against the injected clock."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    state: SchedulerState
    scheduler_id: str | None = Field(
        default=None, description="Which scheduler wrote the newest beat; null if none has"
    )
    last_beat_at: datetime | None = Field(
        default=None, description="Instant of the newest beat; null if no scheduler ever beat"
    )
    age_seconds: float | None = Field(
        default=None,
        description="clock.now() minus last_beat_at; null if no scheduler ever beat. Negative if "
        "a beat is stamped in the future, which is reported rather than clamped because it means "
        "two clocks disagree and that is worth seeing",
    )
    stale_after_seconds: int = Field(
        description="The configured threshold this age is judged against"
    )


class HealthOut(BaseModel):
    """`GET /health` — this process's liveness plus the scheduler heartbeat age (§4.4).

    Served with 503 when `status` is `DEGRADED`, so a probe reading only the HTTP code and a
    dashboard reading the body cannot disagree.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: ServiceStatus
    checked_at: datetime = Field(description="When this answer was computed (injected clock)")
    database: DatabaseHealthOut
    scheduler: SchedulerHealthOut


# ── /status/gaps ────────────────────────────────────────────────────────────────────────────


class GapEntryOut(BaseModel):
    """One `(source, date)` pair in the range that is neither PUBLISHED nor an explained GAP."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: str
    date: date
    state: SyncState
    attempts: int
    retryable: bool
    last_error: str | None
    updated_at: datetime


class GapsOut(BaseModel):
    """`GET /status/gaps?from=&to=` — the unexplained set over an inclusive date range."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    from_date: date
    to_date: date
    unexplained: list[GapEntryOut] = Field(
        description="Pairs the platform has a row for and has not completed. A date no source ever "
        "attempted cannot appear yet: turning an absence into GAP-or-UNEXPLAINED needs the "
        "calendar and the per-source cadence, which is M1.11's gap report. Empty therefore means "
        "'nothing tracked is stuck', not yet 'every missing day is explained'"
    )


# ── /status/quality ─────────────────────────────────────────────────────────────────────────


class QualitySeverity(StrEnum):
    """`quality_flag.severity`. ERROR is what invariant #10 means by data that is not green."""

    INFO = "INFO"
    WARN = "WARN"
    ERROR = "ERROR"


class QualityFlagOut(BaseModel):
    """One open D7 sentinel finding."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: int
    date: date
    check_name: str
    severity: QualitySeverity
    isin: str | None = Field(
        default=None, description="The instrument, when the finding is about one"
    )
    source: str | None = None
    observed_value: Decimal | None = Field(
        default=None, description="What the check measured — exact, never a float"
    )
    threshold: Decimal | None = Field(default=None, description="What it was measured against")
    detail: JsonValue = Field(
        default=None, description="Check-specific context, as the sentinel recorded it"
    )
    raised_at: datetime


class SeverityCountOut(BaseModel):
    """How many open flags carry one severity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    severity: QualitySeverity
    count: int


class QualityOut(BaseModel):
    """`GET /status/quality` — open D7 flags, newest first, with the true totals beside them."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    as_of: datetime = Field(description="When this answer was computed (injected clock)")
    open_total: int = Field(
        description="Every unresolved flag, counted in the database — not len(flags), which the "
        "limit truncates. A status endpoint whose total is its own page size cannot report a flood"
    )
    counts: list[SeverityCountOut]
    flags: list[QualityFlagOut]
    limit: int = Field(description="How many flags this response was allowed to carry")


# ── /archives ───────────────────────────────────────────────────────────────────────────────


class ArchiveFileOut(BaseModel):
    """One file inside a published bundle, as its manifest describes it.

    This is the shape D6's publisher (M1.12) writes into `archive_bundle.manifest.files`, so it is
    a contract in both directions: the publisher fills it, this API projects it. `path` is relative
    to the bundle, which is what makes a manifest survive a restore to a different root.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    path: str
    sha256: str
    bytes: int
    rows: int | None = Field(
        default=None, description="Row count for a tabular file; null for anything else"
    )


class ArchiveBundleOut(BaseModel):
    """The daily archive bundle published for one date (§4.5)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    date: date
    schema_version: str
    bundle_path: str = Field(description="Bundle location relative to the archive root")
    manifest_sha256: str
    file_count: int = Field(description="Files the publisher recorded — its own count")
    total_bytes: int
    published_at: datetime
    files: list[ArchiveFileOut] = Field(
        description="The manifest's file entries. Empty when the stored manifest carries none — "
        "a publisher that recorded a bundle without describing it, which is reported as it is "
        "rather than quietly reconciled against file_count"
    )


class ArchivesOut(BaseModel):
    """`GET /archives?date=` — the manifest of that date's bundle, or nothing published yet."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    date: date
    bundle: ArchiveBundleOut | None = Field(
        default=None, description="Null when no bundle has been published for this date"
    )
