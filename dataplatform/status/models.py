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
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from dataplatform.ingest.source_register import Declined, Paused
from dataplatform.quality.gaps import GapEntry, GapReason, GapReport
from dataplatform.scheduler.health import JobHealth, JobHealthState
from dataplatform.status.deviations import DeviationJudge, DeviationOut, GapItem
from dataplatform.status.sync_state import GreenStatus, SourceStatus, SyncRecord, SyncState


def _deviation_field() -> Any:
    """The `deviation` field every item that can be accepted carries — one wording for all."""
    return Field(
        default=None,
        description="Set when an accepted-deviation entry recognises this item: ACCEPTED (a known, "
        "agreed failure — shown as a warning, with the remark) or EXPIRED (it was, but its "
        "review_by date has passed). Display only: `healthy` keeps its strict meaning",
    )


#: `datetime.date` under a second name. Several models here carry a field called `date` — the
#: §4.4 query parameter is spelled that way on the wire — and a field shadows the type of the same
#: name for the rest of the class body, so any method signature inside such a class uses this.
type TradingDate = date

__all__ = [
    "AlertConditionOut",
    "AlertsOut",
    "ArchiveBundleOut",
    "ArchiveFileOut",
    "ArchivesOut",
    "BackupFileOut",
    "BackupStreamOut",
    "BackupsOut",
    "DatabaseHealthOut",
    "DeclinedOut",
    "GapEntryOut",
    "GapReasonCountOut",
    "GapsOut",
    "HealthOut",
    "JobHealthOut",
    "JobLineOut",
    "JobRunOut",
    "JobRunsOut",
    "JobsOut",
    "ManagerBookOut",
    "ManagerDecisionOut",
    "ManagerScoreOut",
    "ManagersOut",
    "PausedOut",
    "QualityFlagOut",
    "QualityOut",
    "QualitySeverity",
    "SchedulerHealthOut",
    "SchedulerState",
    "ServiceStatus",
    "SeverityCountOut",
    "SourceStatusOut",
    "SourcesOut",
    "SuspendedHoldingOut",
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
        description="SESSION | MUHURAT | SPECIAL | WEEKEND | HOLIDAY from the C.2 calendar; "
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
    declined: list[str] = Field(
        default_factory=list,
        description="Requested datasets set aside because the register DECLINED them on policy "
        "grounds; never fetched, so they neither make the date red nor count towards green",
    )
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
            declined=list(green.declined) if green is not None else [],
            open_error_flags=green.open_error_flags if green is not None else 0,
            rows=[SyncRowOut.of(record) for record in rows],
        )


class DeclinedOut(BaseModel):
    """Why a source is DECLINED on policy grounds (HUMAN_DECISIONS D12/D19), as served.

    Declined is its own state — not failed, not blocked, not stale: the source is never scheduled
    or fetched, and is never reported red. This says why, and which decision to read.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: str
    decision: str = Field(description="The decision reference, e.g. a HUMAN_DECISIONS entry")
    decided_on: date
    declined_url: str | None
    robots_rule: str | None = Field(description="The host's robots.txt rule it rests on, if any")

    @classmethod
    def of(cls, record: Declined) -> DeclinedOut:
        """Project a register decline record onto the wire contract."""
        return cls(
            reason=record.reason,
            decision=record.decision,
            decided_on=record.decided_on,
            declined_url=record.declined_url,
            robots_rule=record.robots_rule,
        )


class PausedOut(BaseModel):
    """Why a source is paused by the owner (register `paused`, HUMAN_DECISIONS D29), as served.

    Paused is its own state — not failed, not healthy: nothing is requested until the owner lifts
    it, its old failures stay on record, and no gate counts it as fresh or published.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: str
    since: date
    decision: str

    @classmethod
    def of(cls, record: Paused) -> PausedOut:
        """Project a register pause record onto the wire contract."""
        return cls(reason=record.reason, since=record.since, decision=record.decision)


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
    max_lag_sessions: int | None = Field(
        description="The scheduled job's lag budget for this source; null when no job keeps it "
        "current"
    )
    overdue: bool = Field(description="More sessions behind than its scheduled budget")
    healthy: bool = Field(description="Not failing and not behind; always true when declined")
    final_state: SyncState = Field(
        default=SyncState.PUBLISHED,
        description="The state a success ends in: FETCHED for a capture-only source (register "
        "`final_state`), whose FETCHED rows are its successes, not rows stuck mid-pipeline",
    )
    deviation: DeviationOut | None = _deviation_field()
    paused: PausedOut | None = Field(
        default=None,
        description="Set while the owner has paused the source (D29): not requested, shown as "
        "PAUSED, never counted as fresh or published. `healthy` is unchanged",
    )
    declined: DeclinedOut | None = Field(
        default=None,
        description="Set when the register DECLINED this source on policy grounds: never "
        "fetched, never red. Its counts are history from before the decision",
    )
    counts: dict[SyncState, int]

    @classmethod
    def of(cls, status: SourceStatus, deviation: DeviationOut | None = None) -> SourceStatusOut:
        """Project a computed source status (and its deviation verdict) onto the wire contract."""
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
            max_lag_sessions=status.max_lag_sessions,
            overdue=status.overdue,
            healthy=status.healthy,
            final_state=status.final_state,
            deviation=deviation,
            paused=None if status.paused is None else PausedOut.of(status.paused),
            declined=None if status.declined is None else DeclinedOut.of(status.declined),
            counts=dict(status.counts),
        )


class SourcesOut(BaseModel):
    """`GET /status/sources` — every source the platform has ever recorded a row for."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    as_of: date = Field(description="The trading date lag is measured against (injected clock)")
    sources: list[SourceStatusOut]
    accepted: int = Field(
        default=0, description="Sources not healthy, but an accepted deviation (a warning)"
    )
    errors: int = Field(
        default=0, description="Sources not healthy and not accepted — the ones that are news"
    )
    paused: int = Field(
        default=0, description="Sources not healthy because the owner paused them (D29); apart"
    )
    declined: dict[str, DeclinedOut] = Field(
        default_factory=dict,
        description="Every Source Register id DECLINED on policy grounds, whether or not it has "
        "rows — declined, not failed, blocked or stale",
    )


# ── /status/jobs ────────────────────────────────────────────────────────────────────────────


class JobHealthOut(BaseModel):
    """One registered job's line in `GET /status/jobs`, read off `job_run` (never assumed)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    cron: str
    state: JobHealthState
    healthy: bool
    deviation: DeviationOut | None = _deviation_field()
    due_since: datetime | None = Field(
        description="The newest fire whose budget has elapsed — the run a success must postdate"
    )
    last_state: str | None
    last_started_at: datetime | None
    last_success_at: datetime | None
    last_error: str | None

    @classmethod
    def of(cls, health: JobHealth) -> JobHealthOut:
        return cls(
            name=health.name,
            cron=health.cron,
            state=health.state,
            healthy=health.healthy,
            due_since=health.due_since,
            last_state=health.runs.last_state,
            last_started_at=health.runs.last_started_at,
            last_success_at=health.runs.last_success_at,
            last_error=health.runs.last_error,
        )


class JobsOut(BaseModel):
    """`GET /status/jobs` — every registered job, and every live source no job keeps current."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    as_of: datetime
    healthy: bool = Field(
        description="Every registered job is OK or RUNNING. Strict: an accepted failure is still "
        "a failure here; `accepted` and `errors` split the rest"
    )
    accepted: int = Field(
        default=0, description="Jobs not healthy, but an accepted deviation (a warning)"
    )
    errors: int = Field(
        default=0, description="Jobs not healthy and not accepted — the ones that are news"
    )
    jobs: list[JobHealthOut]
    unscheduled: dict[str, str] = Field(
        description="Source Register ids no registered job covers, with the recorded reason"
    )
    declined: dict[str, DeclinedOut] = Field(
        default_factory=dict,
        description="Source Register ids DECLINED on policy grounds — never scheduled by design, "
        "so neither a gap nor a failure",
    )


# ── the operator console's Ops page (M18.1) ───────────────────────────────────────────────
# Read by `dataplatform.status.console`; shaped as wire models so a later JSON endpoint can serve
# the same objects. Every free-text field arrives already masked (`dataplatform.redaction`).


class JobLineOut(BaseModel):
    """One registered job as the Ops page lists it: `/status/jobs`'s line plus its schedule.

    `state`, `due_since` and the `last_*` attempt fields are `scheduler.health`'s verdict verbatim —
    the same `read_job_health` call `/status/jobs` makes — so the page and the endpoint cannot
    disagree about whether a job is FAILING or OVERDUE. `next_fire_at` and `timeout_seconds` come
    from the registry's cron and budget; the duration is the newest attempt's.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    cron: str
    state: JobHealthState
    healthy: bool
    deviation: DeviationOut | None = _deviation_field()
    due_since: datetime | None
    last_state: str | None
    last_started_at: datetime | None
    last_finished_at: datetime | None
    last_duration_seconds: float | None = Field(
        description="finished_at minus started_at of the newest attempt; null while it runs"
    )
    last_success_at: datetime | None
    last_error: str | None = Field(description="The newest attempt's error, masked and bounded")
    next_fire_at: datetime | None = Field(description="The cron's next fire after as_of")
    timeout_seconds: int


class JobRunOut(BaseModel):
    """One `job_run` row: an attempt, its outcome and how long it took."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    state: str
    instance: str
    started_at: datetime
    finished_at: datetime | None
    duration_seconds: float | None = Field(description="null while the attempt is RUNNING")
    error: str | None = Field(description="Masked and bounded; null unless FAILED or TIMED_OUT")


class JobRunsOut(BaseModel):
    """The newest `limit` attempts of one registered job, newest first."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    as_of: datetime
    job: str
    cron: str
    runs: list[JobRunOut]
    limit: int


class AlertConditionOut(BaseModel):
    """One open `alert_condition` row (0013): a condition that has paged and not yet cleared."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dedup_key: str
    trigger: str
    severity: str
    title: str = Field(description="The title the onset paged with, masked")
    opened_at: datetime
    last_seen_at: datetime
    deviation: DeviationOut | None = _deviation_field()


class AlertsOut(BaseModel):
    """Open alert conditions, worst first, and the state of the machinery that raises them."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    as_of: datetime
    provider: str = Field(description="ALERT_PROVIDER: the channel an onset is delivered on")
    open: list[AlertConditionOut]
    accepted: int = Field(default=0, description="Open conditions an accepted deviation covers")
    failure_alerts: JobLineOut | None = Field(
        description="The `failure_alerts` job's line — a stopped evaluator raises no alert at all"
    )


class BackupFileOut(BaseModel):
    """The newest file of one backup stream under `BACKUP_ROOT`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    bytes: int
    written_at: datetime = Field(
        description="The dump's own IST stamp for Postgres; the file's mtime for the L0 manifest"
    )
    age_seconds: float = Field(description="as_of minus written_at")
    sha256: str | None = Field(default=None, description="From the dump's sidecar, when it has one")
    entries: int | None = Field(default=None, description="Lines in the L0 manifest")


class BackupStreamOut(BaseModel):
    """One backup job (`postgres_backup` or `l0_backup`): its last run and what it left on disk."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    job: str
    line: JobLineOut | None = Field(description="The job's Ops line; null if not registered")
    newest: BackupFileOut | None = Field(description="null when the stream has written nothing")
    files: int = Field(description="Finished files of this stream under BACKUP_ROOT")
    error: str | None = Field(default=None, description="Why the directory could not be read")


class BackupsOut(BaseModel):
    """The backup panel: both streams, and whether a restore drill has a recorded result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    as_of: datetime
    backup_root: str
    root_visible: bool = Field(description="BACKUP_ROOT is a directory this process can see")
    postgres: BackupStreamOut
    l0: BackupStreamOut
    restore_drill: str = Field(
        description="The latest recorded restore-drill result, or why there is none to show"
    )


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
    """One `(source, date)` pair with no usable data, with the evidence for its reason.

    Every history field is nullable because the most important entry of all — `NEVER_ATTEMPTED` —
    has no `sync_state` row behind it. That is the point of D7's gap report: an absence a query
    over the table alone can never see.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: str
    date: date
    reason: GapReason
    explained: bool = Field(description="False means somebody owes an answer for this day")
    day_kind: str = Field(
        description="SESSION | MUHURAT | SPECIAL | WEEKEND | HOLIDAY from the C.2 calendar"
    )
    detail: str = Field(description="One line naming the reason concretely, for an operator")
    state: SyncState | None = Field(description="Null when the pair has no sync_state row at all")
    attempts: int
    retryable: bool | None
    last_error: str | None
    first_attempt_at: datetime | None
    updated_at: datetime | None
    l1_partition: str | None = Field(
        description="The L1 partition that was checked; null when the lake was not the problem"
    )
    deviation: DeviationOut | None = _deviation_field()

    @classmethod
    def of(cls, entry: GapEntry, deviation: DeviationOut | None = None) -> GapEntryOut:
        """Project a classified entry (and its deviation verdict) onto the wire contract."""
        return cls(
            source=entry.source,
            date=entry.logical_date,
            reason=entry.reason,
            explained=entry.explained,
            day_kind=entry.day_kind.value,
            detail=entry.detail,
            state=entry.state,
            attempts=entry.attempts,
            retryable=entry.retryable,
            last_error=entry.last_error,
            first_attempt_at=entry.first_attempt_at,
            updated_at=entry.updated_at,
            l1_partition=entry.l1_partition,
            deviation=deviation,
        )


class GapReasonCountOut(BaseModel):
    """How many entries in a report carry one reason."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: GapReason
    count: int


class GapsOut(BaseModel):
    """`GET /status/gaps?from=&to=` — the unexplained set over an inclusive date range (M1.11).

    `unexplained` is the answer, and the M1 gate's criterion is that it is empty. Everything
    beside it exists so that an empty list cannot be misread: `sources` says what was examined,
    `pairs_examined` how much, and `l1_unchecked` how many `PUBLISHED` pairs could not be checked
    against the lake. A status API that reported "nothing wrong" without saying what it looked at
    would be the fabricated green §4.4 exists to prevent.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    from_date: date
    to_date: date
    sources: list[str] = Field(
        description="The sources examined. Empty means the platform tracks none — which is not "
        "the same claim as 'nothing is missing'"
    )
    unexplained: list[GapEntryOut] = Field(
        description="Pairs that owed data and do not have it: never attempted, failed, stuck "
        "mid-pipeline, filed as a GAP on a trading day, or PUBLISHED with the L1 partition gone. "
        "Truncated to `limit`; `unexplained_total` is always the real count"
    )
    unexplained_total: int = Field(
        description="Every unexplained pair in the range, counted before the limit truncated"
    )
    accepted_total: int = Field(
        default=0,
        description="Of `unexplained_total`, the pairs an accepted deviation covers (a warning). "
        "They stay unexplained: `fully_explained` is the M1 gate and does not read this",
    )
    explained_total: int = Field(
        description="Absences the calendar or the source register accounts for (weekend, holiday, "
        "outside this source's era)"
    )
    complete: int = Field(description="Pairs that are PUBLISHED with their data present")
    pairs_examined: int = Field(description="sources x dates in the range")
    l1_unchecked: int = Field(
        description="PUBLISHED pairs whose L1 partition could not be verified because the "
        "dataset has never been written. Reported rather than assumed present"
    )
    counts: list[GapReasonCountOut] = Field(description="Entry counts per reason, explained first")
    fully_explained: bool = Field(description="The M1 gate criterion: no unexplained pair at all")
    limit: int = Field(description="How many unexplained entries this response was allowed to hold")

    @classmethod
    def of(cls, report: GapReport, *, limit: int, judge: DeviationJudge | None = None) -> GapsOut:
        """Project a gap report onto the wire, truncating only the enumeration.

        With a `judge`, every unexplained entry — the truncated ones included, so the count is
        true — carries its accepted-deviation verdict. Nothing about `fully_explained` changes.
        """
        unexplained = report.unexplained
        verdicts = [
            None
            if judge is None
            else judge.gap(
                GapItem(
                    source=entry.source,
                    logical_date=entry.logical_date,
                    reason=entry.reason.value,
                    last_error=entry.last_error,
                    retryable=entry.retryable,
                    first_attempt_at=entry.first_attempt_at,
                )
            )
            for entry in unexplained
        ]
        return cls(
            from_date=report.from_date,
            to_date=report.to_date,
            sources=list(report.sources),
            unexplained=[
                GapEntryOut.of(entry, verdict)
                for entry, verdict in zip(unexplained[:limit], verdicts, strict=False)
            ],
            unexplained_total=len(unexplained),
            accepted_total=sum(1 for v in verdicts if v is not None and v.accepted),
            explained_total=len(report.explained),
            complete=report.complete,
            pairs_examined=report.pairs_examined,
            l1_unchecked=report.l1_unchecked,
            counts=[
                GapReasonCountOut(reason=reason, count=count)
                for reason, count in report.counts_by_reason().items()
            ],
            fully_explained=report.fully_explained,
            limit=limit,
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
    deviation: DeviationOut | None = _deviation_field()


class SeverityCountOut(BaseModel):
    """How many open flags carry one severity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    severity: QualitySeverity
    count: int


class CheckCountOut(BaseModel):
    """Open flags from one check, with the two numbers that say whether it is news.

    A queue nobody drains reports the same `count` every day; `raised_today` is what separates
    "2,487 open, all of them old" from "2,487 open and 40 of them arrived this morning".
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    check_name: str
    severity: QualitySeverity
    count: int
    first_date: date = Field(description="Earliest logical date any open flag of this check names")
    last_date: date = Field(description="Latest logical date any open flag of this check names")
    raised_today: int = Field(description="Raised since midnight on the injected clock's today")
    raised_last_7_days: int
    accepted: int = Field(
        default=0,
        description="Of `count`, the open ERROR flags an accepted deviation holds in its set; the "
        "rest are errors. `deviation` is set on the check only when all of them are",
    )
    deviation: DeviationOut | None = _deviation_field()


class QualityOut(BaseModel):
    """`GET /status/quality` — open D7 flags, newest first, with the true totals beside them."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    as_of: datetime = Field(description="When this answer was computed (injected clock)")
    open_total: int = Field(
        description="Every unresolved flag, counted in the database — not len(flags), which the "
        "limit truncates. A status endpoint whose total is its own page size cannot report a flood"
    )
    counts: list[SeverityCountOut]
    by_check: list[CheckCountOut] = Field(
        default_factory=list,
        description="Open flags grouped by check, largest first — the view that survives a "
        "saturated queue, where the capped `flags` page does not",
    )
    flags: list[QualityFlagOut]
    limit: int = Field(description="How many flags this response was allowed to carry")


# ── /status/quarantine ──────────────────────────────────────────────────────────────────────


class QuarantineCountOut(BaseModel):
    """Rows one session refused into `prices_raw_quarantine`, for one reason on one exchange."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    trade_date: date
    exchange: str
    reason: str
    rows: int


class QuarantineStepOut(BaseModel):
    """A session where one reason stepped away from its own trailing median."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    trade_date: date
    exchange: str
    reason: str
    rows: int
    baseline: Decimal = Field(description="Trailing median of the window before this session")
    multiple: Decimal | None = Field(
        description="rows / baseline — exact, never a float. Null when the baseline is zero: "
        "a series that quarantined nothing and now quarantines something has no ratio, and that "
        "is the strongest form of the signal rather than a missing value"
    )
    detail: str


class QuarantineOut(BaseModel):
    """`GET /status/quarantine` — what L1 refused, and whether any session is news.

    The absolute level is known and tracked elsewhere (the identity master knows 2,397 of the
    7,536 ISINs that have traded), so `steps` is the field to look at: a jump against a series'
    own recent history is a new break, where the level is an old one.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    from_date: date
    to_date: date
    rows: int = Field(description="Every quarantined row in the range")
    partitions_read: int = Field(
        description="Partitions the answer was computed over. Zero rows over zero partitions is "
        "not a clean bill of health, and this is what tells the two apart"
    )
    totals: dict[str, int] = Field(description="Rows per reason, largest first")
    steps: list[QuarantineStepOut]
    counts: list[QuarantineCountOut]
    limit: int = Field(description="How many per-session counts this response was allowed to hold")


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


# ── /status/paper ───────────────────────────────────────────────────────────────────────────


class KillSwitchOut(BaseModel):
    """A paper book's kill switch, as its state file says (M15.3) — never assumed armed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tripped: bool | None = Field(description="None when the state file could not be read")
    source: str | None = Field(description="MANUAL, RAILS or RECON while tripped")
    reason: str | None
    tripped_at: datetime | None
    error: str | None = Field(
        default=None, description="Why the state could not be read — an unreadable switch is red"
    )


class PaperBookStatusOut(BaseModel):
    """One paper book: its latest session, its kill switch, what blocks it (M15.3)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    book_id: str
    healthy: bool = Field(
        description="Switch armed, latest session not a reconciliation break, nothing unresolved"
    )
    kill_switch: KillSwitchOut
    latest_date: date | None
    latest_outcome: str | None = Field(description="COMPLETED, SKIPPED_DATA_RED or RECON_BREAK")
    latest_reason: str | None
    latest_recon: str | None = Field(description="CLEAN, BREAK, or None before M15.3")
    unresolved: list[str] = Field(
        description="Escalations and reconciliation breaks with no resolution row, as key@terms"
    )


class PaperOut(BaseModel):
    """`GET /status/paper` — every paper book the ledger or the kill-switch directory knows."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    as_of: datetime
    healthy: bool = Field(description="Every book is healthy (true when there are none)")
    books: list[PaperBookStatusOut]


# ── /status/managers (M17.6) ─────────────────────────────────────────────────────────────────


class ManagerBookOut(BaseModel):
    """One M17 book's latest journaled mark: a manager book, a control, a style book, the bench."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    book_id: str
    kind: str = Field(description="MANAGER, CONTROL, STYLE or BENCH")
    opening_capital_inr: Decimal
    latest_session: date | None = Field(description="None before the book's first mark")
    nav: Decimal | None
    cash: Decimal | None
    positions: int | None
    return_pct: Decimal | None = Field(description="Latest mark over opening capital, in percent")


class ManagerDecisionOut(BaseModel):
    """One journaled decision of an M17 book on the latest session: what, never why.

    Carries no rationale, prompt or evidence text — only the decision's structured fields.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    book_id: str
    trading_date: date
    decision: str
    event: str | None
    isin: str | None
    action: str | None
    target_weight: str | None
    p_beat_bench: str | None
    horizon_sessions: str | None
    rails: str | None = Field(description="The rails that refused it, for a RAIL_BLOCK")
    refused_for: str | None = Field(
        default=None,
        description="The decision contract's reason codes, for an FM_DECISION_REFUSED",
    )


class ManagerScoreOut(BaseModel):
    """One manager book on the pre-registration §6 scoreboard against its own control, over its
    current window (Amendment 2 e: the rule applies per book)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    book_id: str = Field(description="The manager book: <manager>-10L (primary) or -1CR (mirror)")
    manager_id: str
    role: str = Field(description="PRIMARY or MIRROR")
    control_id: str
    phase: str = Field(description="NOT_STARTED, PRIMARY, EXTENSION or FINAL")
    verdict: str = Field(description="NOT_STARTED, IN_PROGRESS, PASS, CLEAR_FAIL or INCONCLUSIVE")
    window: str | None = Field(description="primary (S0..S0+62) or extension (S0..S0+125)")
    window_sessions: int | None
    window_end: date | None
    excess_vs_control_pp: Decimal | None
    excess_vs_bench_pp: Decimal | None
    max_drawdown_pp: Decimal | None
    bench_max_drawdown_pp: Decimal | None
    brier: Decimal | None = Field(description="The manager's Brier, computed once on its decisions")
    resolved_decisions: int | None
    suspended_resolved_decisions: int | None = Field(
        default=None,
        description="Of the resolved decisions, how many were scored at a suspended name's last "
        "traded close (M17.13)",
    )
    # Amendment 4 (c): secondary, reported only, never in a verdict
    sleeve_return_pct: Decimal | None = Field(
        default=None,
        description="Return on the capital the book had in names, net of costs, without cash or "
        "its interest, chained over the sessions it held any (secondary; never in a verdict)",
    )
    sleeve_control_return_pct: Decimal | None = Field(
        default=None, description="The control book's NAV return over those same sessions"
    )
    sleeve_excess_vs_control_pp: Decimal | None = Field(
        default=None, description="sleeve_return_pct less sleeve_control_return_pct"
    )
    sleeve_sessions: int | None = Field(
        default=None, description="Sessions of the window on which the book held any name"
    )
    average_exposure_pct: Decimal | None = Field(
        default=None, description="Mean share of NAV in names over the window's sessions"
    )


class ManagerResultOut(BaseModel):
    """One manager across its two books (Amendment 2 e): passed on both, one or neither."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    manager_id: str
    primary_book: str
    mirror_book: str
    style_book: str
    primary_verdict: str
    mirror_verdict: str
    passed_on: str = Field(description="BOTH, ONE or NEITHER")
    decisions: int = Field(description="Decisions journaled, counted once (primary stream only)")
    brier: Decimal | None
    resolved_decisions: int | None
    suspended_resolved_decisions: int | None
    extension_confirmed: bool = Field(
        description="Its primary book passed across the extension window as well"
    )


class StyleBookOut(BaseModel):
    """A STYLE-<manager> book over its manager's primary window: secondary, never pass/fail."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    style_id: str
    manager_id: str
    primary_book: str
    window: str | None
    sessions: int | None
    style_return_pct: Decimal | None
    style_max_drawdown_pp: Decimal | None
    primary_return_pct: Decimal | None
    primary_excess_vs_style_pp: Decimal | None


class GraduationOut(BaseModel):
    """Amendment 2 (e)'s graduation floor: what the evidence must at least show (decision #8)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rule: str
    primary_passes: int
    primary_passes_needed: int
    extension_confirmed: list[str]
    met: bool


class SuspendedHoldingOut(BaseModel):
    """One held M17 name that is still listed but did not trade on the session (M17.13)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    book_id: str
    trading_date: date
    isin: str
    last_trade_date: date = Field(description="Held, not trading since this session")
    sessions_suspended: int


class ManagersOut(BaseModel):
    """`GET /status/managers` — the M17 books, the latest session's decisions, the scoreboard.

    Everything is rebuilt from the M17 journal streams on each request. No secret, no prompt text.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    as_of: datetime
    s0: date | None = Field(description="The journaled S0; None before the clock starts")
    scoreboard_as_of: date | None = Field(description="The latest mark any book journaled")
    sessions_elapsed: int
    k_of_n: str = Field(
        description="'k of 8 books passed' — never a lone winner (§6, Amendment 2 e)"
    )
    passed: int = Field(description="Manager books that passed, of 8")
    scoreboard_digest: str | None = Field(
        description="sha256 of the scoreboard's canonical bytes; None when it could not be built"
    )
    scoreboard_error: str | None = Field(
        description="Why the journal could not be scored (a missing mark, a malformed line)"
    )
    books: list[ManagerBookOut]
    managers: list[ManagerScoreOut] = Field(description="The 8 manager books, each vs its control")
    manager_results: list[ManagerResultOut] = Field(default_factory=list)
    style_books: list[StyleBookOut] = Field(default_factory=list)
    graduation: GraduationOut | None = None
    decisions_session: date | None
    decisions: list[ManagerDecisionOut]
    suspended_holdings: list[SuspendedHoldingOut] = Field(
        default_factory=list,
        description="Held names suspended on the latest session, each 'held, not trading since'",
    )
