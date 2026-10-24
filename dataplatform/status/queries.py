"""D5: the status-API reads that no other module already owns.

Three of the six §4.4 endpoints read through somebody else's module on purpose, because a second
implementation would be a second answer to the same question: `sync_state` is read through
`SyncStateStore` (M1.3), which the trading interlock also uses, the scheduler heartbeat through
`dataplatform.scheduler.read_heartbeat` (M0.6), which is written by the same module that beats,
and `/status/gaps` through `dataplatform.quality.gaps.GapScanner` (M1.11), which owns the one
definition of what an unexplained missing day is. This file is the remainder of the surface: the
open D7 flags behind `/status/quality`, and the published bundle behind `/archives`.

Split from `api.py` so the HTTP layer is only routing and status codes, and so "where does this
number come from" is answered by one file of plain SQL. Every function takes an open connection
and returns wire models; none opens a connection, reads the clock, or writes. The status surface
is read-only by construction — it reports the platform's state and is never a way to change it.

The instant a query is relative to is always passed in, never taken from the database's `now()`:
the clock is injected (B10, invariant #11), and a `/health` answering from the server clock would
ignore the frozen clock a test or a replay set.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from dataplatform.config import Settings
from dataplatform.ingest.source_register import Declined
from dataplatform.quality.quarantine import read_quarantine, step_changes
from dataplatform.redaction import mask_and_truncate
from dataplatform.scheduler.health import JobHealth, JobHealthState, read_job_health
from dataplatform.scheduler.registry import UNSCHEDULED, Job, JobRegistry
from dataplatform.scheduler.runner import JOB_ERROR_CHARS, Heartbeat
from dataplatform.status.deviations import DeviationJudge, DeviationOut
from dataplatform.status.models import (
    AlertConditionOut,
    AlertsOut,
    ArchiveBundleOut,
    ArchiveFileOut,
    ArchivesOut,
    BackupFileOut,
    BackupsOut,
    BackupStreamOut,
    CheckCountOut,
    DatabaseHealthOut,
    DeclinedOut,
    GraduationOut,
    HealthOut,
    JobHealthOut,
    JobLineOut,
    JobRunOut,
    JobRunsOut,
    JobsOut,
    KillSwitchOut,
    ManagerBookOut,
    ManagerDecisionOut,
    ManagerResultOut,
    ManagerScoreOut,
    ManagersOut,
    PaperBookStatusOut,
    PaperOut,
    QualityFlagOut,
    QualityOut,
    QualitySeverity,
    QuarantineCountOut,
    QuarantineOut,
    QuarantineStepOut,
    SchedulerHealthOut,
    SchedulerState,
    ServiceStatus,
    SeverityCountOut,
    StyleBookOut,
    SuspendedHoldingOut,
)
from dataplatform.store.backup import BackupError, list_backups, load_sidecar, postgres_dir
from dataplatform.store.db import Connection

__all__ = [
    "JOB_RUNS_LIMIT",
    "NO_RESTORE_DRILL_RECORD",
    "HeartbeatRead",
    "StatusQueryError",
    "assess_health",
    "is_accepted",
    "job_deviation",
    "job_lines",
    "read_alerts",
    "read_archives",
    "read_backups",
    "read_job_runs",
    "read_jobs_status",
    "read_paper_status",
    "read_quality",
    "sort_worst_first",
    "split_accepted",
]


class StatusQueryError(RuntimeError):
    """State in the database the status contract cannot describe — a defect, not a bad request."""


_QUALITY_FLAGS_SQL = """
SELECT id, logical_date, check_name, severity, isin, source, observed_value, threshold,
       detail, raised_at
FROM quality_flag
WHERE NOT resolved
ORDER BY raised_at DESC, id DESC
LIMIT %s
"""

_QUALITY_COUNTS_SQL = """
SELECT severity, count(*)
FROM quality_flag
WHERE NOT resolved
GROUP BY severity
ORDER BY severity
"""

#: Open flags grouped by what actually raised them. `/status/quality` reported one number and a
#: truncated page of rows, so a saturated queue ("2,487 open" every day, for two years) rendered
#: identically to a fresh incident. The two columns that separate them are the last two: how many
#: of these arrived today, and how many in the last week.
_QUALITY_BY_CHECK_SQL = """
SELECT check_name,
       severity,
       count(*),
       min(logical_date),
       max(logical_date),
       count(*) FILTER (WHERE raised_at >= %s),
       count(*) FILTER (WHERE raised_at >= %s)
FROM quality_flag
WHERE NOT resolved
GROUP BY check_name, severity
ORDER BY count(*) DESC, check_name
"""

_ARCHIVES_SQL = """
SELECT logical_date, schema_version, bundle_path, manifest_sha256, file_count, total_bytes,
       manifest, published_at
FROM archive_bundle
WHERE logical_date = %s
"""


def read_quality(
    conn: Connection, as_of: datetime, limit: int, *, judge: DeviationJudge | None = None
) -> QualityOut:
    """Open D7 sentinel flags, newest first, plus the totals the limit would otherwise hide.

    Grouped by check as well as by severity, because "how many are open" is not the operational
    question — "is today worse than yesterday" is, and a permanently saturated queue answers the
    first one identically every day. `raised_today`/`raised_last_7_days` are that difference.
    With a `judge`, a check's ERROR row carries its accepted-deviation verdict (display only).
    """
    midnight = datetime.combine(as_of.date(), time.min, tzinfo=as_of.tzinfo)
    week_ago = midnight - timedelta(days=7)
    counts = [
        SeverityCountOut(severity=row[0], count=int(row[1]))
        for row in conn.execute(_QUALITY_COUNTS_SQL).fetchall()
    ]
    by_check = [
        CheckCountOut(
            check_name=str(row[0]),
            severity=row[1],
            count=int(row[2]),
            first_date=row[3],
            last_date=row[4],
            raised_today=int(row[5]),
            raised_last_7_days=int(row[6]),
        )
        for row in conn.execute(_QUALITY_BY_CHECK_SQL, (midnight, week_ago)).fetchall()
    ]
    if judge is not None:
        by_check = [
            check.model_copy(update={"deviation": judge.quality(check.check_name)})
            if check.severity is QualitySeverity.ERROR
            else check
            for check in by_check
        ]
    flags = [
        QualityFlagOut(
            id=int(row[0]),
            date=row[1],
            check_name=str(row[2]),
            severity=row[3],
            isin=row[4],
            source=row[5],
            observed_value=row[6],
            threshold=row[7],
            detail=row[8],
            raised_at=row[9],
        )
        for row in conn.execute(_QUALITY_FLAGS_SQL, (limit,)).fetchall()
    ]
    return QualityOut(
        as_of=as_of,
        open_total=sum(entry.count for entry in counts),
        counts=counts,
        by_check=by_check,
        flags=flags,
        limit=limit,
    )


def read_archives(conn: Connection, logical_date: date) -> ArchivesOut:
    """The published archive bundle for one date, or `bundle=null` when there is none."""
    row = conn.execute(_ARCHIVES_SQL, (logical_date,)).fetchone()
    if row is None:
        return ArchivesOut(date=logical_date)
    return ArchivesOut(
        date=row[0],
        bundle=ArchiveBundleOut(
            date=row[0],
            schema_version=str(row[1]),
            bundle_path=str(row[2]),
            manifest_sha256=str(row[3]),
            file_count=int(row[4]),
            total_bytes=int(row[5]),
            published_at=row[7],
            files=_manifest_files(row[6], logical_date),
        ),
    )


def _manifest_files(manifest: object, logical_date: date) -> list[ArchiveFileOut]:
    """Project a stored manifest's `files` array onto the response contract.

    A manifest with no `files` key yields an empty list — that is a publisher that recorded a
    bundle without describing it, and `/archives` reports what is there. A `files` entry that does
    not match `ArchiveFileOut` raises instead: the manifest is the checksum record for a data
    archive, and dropping the entries we cannot read would turn a corrupt manifest into a shorter
    one that looks perfectly fine.
    """
    if not isinstance(manifest, dict):
        raise StatusQueryError(
            f"archive_bundle.manifest for {logical_date.isoformat()} is "
            f"{type(manifest).__name__}, not a JSON object"
        )
    entries = manifest.get("files", [])
    if not isinstance(entries, list):
        raise StatusQueryError(
            f"archive_bundle.manifest.files for {logical_date.isoformat()} is "
            f"{type(entries).__name__}, not a list"
        )
    try:
        return [ArchiveFileOut.model_validate(entry) for entry in entries]
    except ValidationError as error:
        raise StatusQueryError(
            f"archive_bundle.manifest.files for {logical_date.isoformat()} does not match the "
            f"ArchiveFileOut contract: {error}"
        ) from error


def read_quarantine_status(
    from_date: date, to_date: date, *, limit: int, data_root: Path | None = None
) -> QuarantineOut:
    """`GET /status/quarantine` — what L1 refused over a range, and which sessions are news.

    The per-session enumeration is capped the way `/status/quality`'s is: `rows`, `totals` and
    `steps` are computed over the whole range, so the cap changes how much you read and never what
    is true. `steps` is never capped — there are single digits of them over a decade, and a
    truncated list of the only actionable field would be worse than useless.
    """
    report = read_quarantine(from_date, to_date, data_root=data_root)
    steps = step_changes(report.counts)
    return QuarantineOut(
        from_date=from_date,
        to_date=to_date,
        rows=report.rows,
        partitions_read=report.partitions_read,
        totals=report.totals(),
        steps=[
            QuarantineStepOut(
                trade_date=step.trade_date,
                exchange=step.exchange,
                reason=step.reason,
                rows=step.rows,
                baseline=step.baseline,
                multiple=step.multiple,
                detail=step.detail(),
            )
            for step in steps
        ],
        counts=[
            QuarantineCountOut(
                trade_date=count.trade_date,
                exchange=count.exchange,
                reason=count.reason,
                rows=count.rows,
            )
            # Newest first: an operator opening this wants the recent sessions, and the ten-year
            # tail is what the cap is there to keep out.
            for count in sorted(report.counts, key=lambda c: c.trade_date, reverse=True)[:limit]
        ],
        limit=limit,
    )


# ── /status/paper ───────────────────────────────────────────────────────────────────────────

_PAPER_LATEST_SQL = """
SELECT DISTINCT ON (book_id) book_id, trading_date, outcome, reason, recon->>'status'
FROM paper_session ORDER BY book_id, trading_date DESC
"""

#: Every escalated corporate action and every reconciliation break with no resolution row — the
#: same (key, terms) the paper session itself refuses to trade past.
_PAPER_UNRESOLVED_SQL = """
WITH raised AS (
    SELECT p.book_id, a->>'key' AS key, a->>'terms' AS terms
    FROM paper_session p, jsonb_array_elements(p.actions) AS a
    WHERE a->>'status' = 'ESCALATED'
    UNION
    SELECT book_id, recon->>'key', recon->>'terms'
    FROM paper_session WHERE recon->>'status' = 'BREAK'
)
SELECT r.book_id, r.key || '@' || r.terms
FROM raised r
WHERE NOT EXISTS (
    SELECT 1 FROM paper_session_resolution s
    WHERE s.book_id = r.book_id AND s.action_key = r.key AND s.terms = r.terms
)
ORDER BY 1, 2
"""


def read_paper_status(conn: Connection, *, data_root: Path, as_of: datetime) -> PaperOut:
    """Each paper book's latest session, kill switch and unresolved blocks (M15.3).

    The books are the union of those with a ``paper_session`` row and those with a kill-switch file
    under ``<data_root>/kill_switch`` — a switch tripped before a book's first row still shows. A
    switch file that cannot be read is reported with its error and counts as unhealthy, never as
    armed (the switch itself refuses to treat it as armed, too).
    """
    from execution.kill_switch import KILL_SWITCH_DIRNAME, KillSwitch, kill_switch_path

    latest = {row[0]: row for row in conn.execute(_PAPER_LATEST_SQL).fetchall()}
    unresolved: dict[str, list[str]] = {}
    for book_id, item in conn.execute(_PAPER_UNRESOLVED_SQL).fetchall():
        unresolved.setdefault(book_id, []).append(item)
    switch_dir = data_root / KILL_SWITCH_DIRNAME
    with_switch = (
        {path.stem for path in switch_dir.glob("*.json")} if switch_dir.is_dir() else set()
    )
    books: list[PaperBookStatusOut] = []
    for book_id in sorted(set(latest) | with_switch):
        try:
            state = KillSwitch(kill_switch_path(data_root, book_id)).state
            switch = KillSwitchOut(
                tripped=state.tripped,
                source=None if state.source is None else state.source.value,
                reason=state.reason,
                tripped_at=state.tripped_at,
            )
        except (OSError, ValueError) as error:
            switch = KillSwitchOut(
                tripped=None, source=None, reason=None, tripped_at=None, error=str(error)
            )
        row = latest.get(book_id)
        blocks = unresolved.get(book_id, [])
        books.append(
            PaperBookStatusOut(
                book_id=book_id,
                healthy=switch.tripped is False
                and (row is None or row[2] != "RECON_BREAK")
                and not blocks,
                kill_switch=switch,
                latest_date=None if row is None else row[1],
                latest_outcome=None if row is None else row[2],
                latest_reason=None if row is None else row[3],
                latest_recon=None if row is None else row[4],
                unresolved=blocks,
            )
        )
    return PaperOut(as_of=as_of, healthy=all(book.healthy for book in books), books=books)


# ── /status/managers (M17.6) ───────────────────────────────────────────────────────────────────


def read_m17_roster() -> Any:
    """The M17 roster (`analyst.fundmanager.load_roster`), imported lazily."""
    from analyst.fundmanager import load_roster

    return load_roster()


def read_m17_journal(conn: Connection, *, data_root: Path, roster: Any) -> tuple[Any, ...]:
    """Every journal entry of the M17 books, in append order (S0 is journaled in each book's
    stream, beside its mandate hash).

    Read through `analyst.journal.Journal` — the one reader of `decision_journal` — and imported
    here lazily, like the kill switch above, so importing the status API stays free of System 2.
    """
    from analyst.journal import EvidenceStore, Journal, JournalFilter

    journal = Journal(conn, evidence=EvidenceStore(data_root / "evidence"))
    books = [book.id for book in roster.books]
    entries = [e for book in books for e in journal.entries(JournalFilter(case_id=book))]
    return tuple(sorted(entries, key=lambda e: e.id))


def read_managers_status(entries: Sequence[Any], *, roster: Any, as_of: datetime) -> ManagersOut:
    """The `/status/managers` body from the M17 journal ``entries`` (in append order).

    The scoreboard is rebuilt from the entries on every call, the same way the daily job builds
    it, so the page can never show a number the journal does not reproduce. A journal the
    scoreboard refuses (a missing mark, a malformed line) is reported in ``scoreboard_error`` with
    no scores, never papered over with partial numbers; the decisions still show.
    """
    from analyst.fundmanager.scoreboard import (
        ScoreboardError,
        ScoreboardInputs,
        build_scoreboard,
        inputs_from_journal,
        suspended_holdings_on,
        todays_decisions,
    )

    session, lines = todays_decisions(entries, roster)
    _, suspended = suspended_holdings_on(entries, roster, session=session)
    error: str | None = None
    try:
        scoreboard = build_scoreboard(roster, inputs_from_journal(entries, roster))
    except ScoreboardError as exc:
        error = str(exc)
        scoreboard = build_scoreboard(roster, ScoreboardInputs(s0=None))
    managers: list[ManagerScoreOut] = []
    for score in scoreboard.book_scores:
        window = score.extension or score.primary
        managers.append(
            ManagerScoreOut(
                book_id=score.book_id,
                manager_id=score.manager_id,
                role=score.role,
                control_id=score.control_id,
                phase=score.phase.value,
                verdict=score.verdict.value,
                window=None if window is None else window.label,
                window_sessions=None if window is None else window.sessions,
                window_end=None if window is None else window.end,
                excess_vs_control_pp=None if window is None else window.excess_vs_control_pp,
                excess_vs_bench_pp=(
                    None if window is None else window.secondary.excess_vs_bench_pp
                ),
                max_drawdown_pp=None if window is None else window.manager_max_drawdown_pp,
                bench_max_drawdown_pp=None if window is None else window.bench_max_drawdown_pp,
                brier=None if window is None else window.brier,
                resolved_decisions=None if window is None else window.resolved_decisions,
                suspended_resolved_decisions=(
                    None if window is None else window.suspended_resolved_decisions
                ),
                sleeve_return_pct=None if window is None else window.secondary.sleeve_return_pct,
                sleeve_control_return_pct=(
                    None if window is None else window.secondary.sleeve_control_return_pct
                ),
                sleeve_excess_vs_control_pp=(
                    None if window is None else window.secondary.sleeve_excess_vs_control_pp
                ),
                sleeve_sessions=None if window is None else window.secondary.sleeve_sessions,
                average_exposure_pct=(
                    None if window is None else window.secondary.average_exposure_pct
                ),
            )
        )
    return ManagersOut(
        as_of=as_of,
        s0=scoreboard.s0,
        scoreboard_as_of=scoreboard.as_of,
        sessions_elapsed=scoreboard.sessions_elapsed,
        k_of_n=scoreboard.k_of_n,
        passed=scoreboard.passed,
        scoreboard_digest=None if error is not None else scoreboard.digest(),
        scoreboard_error=error,
        books=[ManagerBookOut(**book.model_dump()) for book in scoreboard.books],
        managers=[] if error is not None else managers,
        manager_results=[]
        if error is not None
        else [ManagerResultOut(**r.model_dump()) for r in scoreboard.managers],
        style_books=[]
        if error is not None
        else [StyleBookOut(**b.model_dump()) for b in scoreboard.style_books],
        graduation=None
        if error is not None
        else GraduationOut(
            **{
                **scoreboard.graduation.model_dump(),
                "extension_confirmed": list(scoreboard.graduation.extension_confirmed),
            }
        ),
        decisions_session=session,
        decisions=[ManagerDecisionOut(**line.model_dump()) for line in lines],
        suspended_holdings=[SuspendedHoldingOut(**line.model_dump()) for line in suspended],
    )


# ── /health and the console's health banner ────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class HeartbeatRead:
    """The outcome of one attempt to read the heartbeat — including the attempt failing.

    Three outcomes, not two: a beat, no beat at all, and no answer. Collapsing the last two into
    `None` is exactly the conflation `/health` exists to avoid, because "no scheduler has ever
    run" and "we could not look" have opposite operational meanings.
    """

    reachable: bool
    beat: Heartbeat | None = None
    error: str | None = None


def assess_health(read: HeartbeatRead, *, now: datetime, stale_after: int) -> HealthOut:
    """The `/health` verdict for one heartbeat read, as of the caller's injected `now`.

    What it does: STALE once the newest beat is older than `stale_after` seconds, UNKNOWN when the
    heartbeat could not be read, NEVER_RAN when the table is empty; DEGRADED for the first two.
    What it never does: set a status code or log — `/health` does both, and the console's banner
    renders the same object, so the probe and the page cannot disagree.
    """
    database = DatabaseHealthOut(reachable=read.reachable, error=read.error)
    if read.error is not None:
        scheduler = SchedulerHealthOut(
            state=SchedulerState.UNKNOWN, stale_after_seconds=stale_after
        )
    elif read.beat is None:
        scheduler = SchedulerHealthOut(
            state=SchedulerState.NEVER_RAN, stale_after_seconds=stale_after
        )
    else:
        age = read.beat.age(now).total_seconds()
        scheduler = SchedulerHealthOut(
            state=SchedulerState.STALE if age > stale_after else SchedulerState.OK,
            scheduler_id=read.beat.name,
            last_beat_at=read.beat.beat_at,
            age_seconds=age,
            stale_after_seconds=stale_after,
        )
    degraded = read.error is not None or scheduler.state is SchedulerState.STALE
    return HealthOut(
        status=ServiceStatus.DEGRADED if degraded else ServiceStatus.OK,
        checked_at=now,
        database=database,
        scheduler=scheduler,
    )


# ── /status/jobs and the console's Ops page ────────────────────────────────────────────────

#: How many attempts the per-job drill-down lists.
JOB_RUNS_LIMIT: Final = 50

#: Worst first for an operator: what is broken, then what is late, then what never started.
_STATE_RANK: Final[Mapping[JobHealthState, int]] = {
    JobHealthState.FAILING: 0,
    JobHealthState.OVERDUE: 1,
    JobHealthState.NEVER_RAN: 2,
    JobHealthState.RUNNING: 3,
    JobHealthState.OK: 4,
}


def _masked(text: str | None) -> str | None:
    """`job_run.error` as it may be shown: masked, then bounded (invariant #13).

    The runner masks before it writes, but a row written before it did — or by a by-hand run —
    is read here too, so every reader masks again rather than trusting the writer.
    """
    return None if text is None else mask_and_truncate(text, JOB_ERROR_CHARS)


def job_deviation(judge: DeviationJudge | None, health: JobHealth) -> DeviationOut | None:
    """A FAILING job's accepted-deviation verdict about its newest attempt, or None.

    Only a newest attempt that FAILED can be a known cause: OVERDUE, NEVER_RAN and TIMED_OUT are
    other failure modes, and a healthy job needs no verdict. Display only.
    """
    if judge is None or health.state is not JobHealthState.FAILING:
        return None
    if health.runs.last_state != "FAILED":
        return None
    return judge.job(health.name, started_at=health.runs.last_started_at)


def split_accepted(
    healthy: Sequence[bool], verdicts: Sequence[DeviationOut | None]
) -> tuple[int, int]:
    """(accepted, errors) among the items that are not healthy."""
    accepted = sum(
        1
        for ok, v in zip(healthy, verdicts, strict=True)
        if not ok and v is not None and v.accepted
    )
    return accepted, sum(1 for ok in healthy if not ok) - accepted


def read_jobs_status(
    conn: Connection,
    registry: JobRegistry,
    *,
    now: datetime,
    timezone: ZoneInfo,
    declined: Mapping[str, Declined],
    judge: DeviationJudge | None = None,
) -> JobsOut:
    """`GET /status/jobs`: every job in `registry` assessed against `job_run` as of `now`.

    Registry order, every registered job whether or not it has rows, the `UNSCHEDULED` ledger and
    the declined sources. `last_error` is masked on the way out. With a `judge`, a FAILING job
    carries its accepted-deviation verdict and `accepted`/`errors` split the unhealthy count;
    `healthy` is computed exactly as before, from the jobs alone.
    """
    jobs = read_job_health(conn, registry, now=now, timezone=timezone)
    verdicts = [job_deviation(judge, job) for job in jobs]
    accepted, errors = split_accepted([job.healthy for job in jobs], verdicts)
    return JobsOut(
        as_of=now,
        healthy=all(job.healthy for job in jobs),
        accepted=accepted,
        errors=errors,
        jobs=[
            JobHealthOut.of(job).model_copy(
                update={"last_error": _masked(job.runs.last_error), "deviation": verdict}
            )
            for job, verdict in zip(jobs, verdicts, strict=True)
        ],
        unscheduled=dict(UNSCHEDULED),
        declined={source: DeclinedOut.of(record) for source, record in declined.items()},
    )


_NEWEST_ATTEMPT_SQL: Final = """
SELECT DISTINCT ON (job_name) job_name, started_at, finished_at
FROM job_run
ORDER BY job_name, started_at DESC
"""


def _seconds(start: datetime | None, end: datetime | None) -> float | None:
    return None if start is None or end is None else (end - start).total_seconds()


def _line(
    health: JobHealth,
    job: Job,
    finished: datetime | None,
    *,
    now: datetime,
    timezone: ZoneInfo,
    deviation: DeviationOut | None = None,
) -> JobLineOut:
    trigger = job.trigger(timezone)
    return JobLineOut(
        name=health.name,
        cron=health.cron,
        state=health.state,
        healthy=health.healthy,
        deviation=deviation,
        due_since=health.due_since,
        last_state=health.runs.last_state,
        last_started_at=health.runs.last_started_at,
        last_finished_at=finished,
        last_duration_seconds=_seconds(health.runs.last_started_at, finished),
        last_success_at=health.runs.last_success_at,
        last_error=_masked(health.runs.last_error),
        next_fire_at=trigger.get_next_fire_time(None, now),
        timeout_seconds=int(job.timeout.total_seconds()),
    )


def job_lines(
    conn: Connection,
    registry: JobRegistry,
    *,
    now: datetime,
    timezone: ZoneInfo,
    judge: DeviationJudge | None = None,
) -> list[JobLineOut]:
    """Every registered job's Ops line, in registry order (`sort_worst_first` orders them).

    State comes from `scheduler.health.read_job_health` — the call `/status/jobs` makes — and the
    newest attempt's `finished_at` from one more read, matched on its `started_at` so a run that
    began between the two reads is reported as still running rather than given a stale duration.
    The accepted-deviation verdict is `job_deviation`'s, the same as `/status/jobs`'.
    """
    healths = read_job_health(conn, registry, now=now, timezone=timezone)
    newest = {str(row[0]): (row[1], row[2]) for row in conn.execute(_NEWEST_ATTEMPT_SQL).fetchall()}
    lines: list[JobLineOut] = []
    for health in healths:
        started, finished = newest.get(health.name, (None, None))
        if started != health.runs.last_started_at:
            finished = None
        job = registry.get(health.name)
        deviation = job_deviation(judge, health)
        lines.append(_line(health, job, finished, now=now, timezone=timezone, deviation=deviation))
    return lines


def is_accepted(item: JobLineOut | AlertConditionOut) -> bool:
    """Whether an item carries an ACCEPTED deviation verdict (an EXPIRED one does not count)."""
    return item.deviation is not None and item.deviation.accepted


def sort_worst_first(lines: Sequence[JobLineOut]) -> list[JobLineOut]:
    """Errors first — FAILING, OVERDUE, NEVER_RAN — then accepted deviations, then RUNNING, OK.

    An accepted failure sorts below every unaccepted one, so a new red is the top line however
    many known ones there are. By name within a state.
    """

    def rank(line: JobLineOut) -> tuple[int, int, str]:
        bucket = 2 if line.healthy else (1 if is_accepted(line) else 0)
        return bucket, _STATE_RANK[line.state], line.name

    return sorted(lines, key=rank)


_JOB_RUNS_SQL: Final = """
SELECT run_id, state, instance, started_at, finished_at, error
FROM job_run
WHERE job_name = %s
ORDER BY started_at DESC
LIMIT %s
"""


def read_job_runs(
    conn: Connection, job: Job, *, now: datetime, limit: int = JOB_RUNS_LIMIT
) -> JobRunsOut:
    """The newest `limit` `job_run` rows of `job`, newest first, errors masked."""
    runs = [
        JobRunOut(
            run_id=str(row[0]),
            state=str(row[1]),
            instance=str(row[2]),
            started_at=row[3],
            finished_at=row[4],
            duration_seconds=_seconds(row[3], row[4]),
            error=_masked(None if row[5] is None else str(row[5])),
        )
        for row in conn.execute(_JOB_RUNS_SQL, (job.name, limit)).fetchall()
    ]
    return JobRunsOut(as_of=now, job=job.name, cron=job.cron, runs=runs, limit=limit)


# ── alerts ─────────────────────────────────────────────────────────────────────────────────

_OPEN_ALERTS_SQL: Final = """
SELECT dedup_key, trigger, severity, title, opened_at, last_seen_at
FROM alert_condition
WHERE resolved_at IS NULL
ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, opened_at, dedup_key
"""

#: The longest alert title shown; `alert_triggers` keeps titles to one line.
_TITLE_CHARS: Final = 400


def read_alerts(
    conn: Connection,
    *,
    now: datetime,
    provider: str,
    failure_alerts: JobLineOut | None,
    judge: DeviationJudge | None = None,
) -> AlertsOut:
    """Open `alert_condition` rows (critical first, then oldest), and the alerter's own state.

    `failure_alerts` is that job's Ops line, passed in so the panel shows the same verdict as the
    jobs table: a condition list is only as current as the last tick that evaluated it. With a
    `judge`, each row carries its accepted-deviation verdict — the same `DeviationJudge.alert` the
    evaluator asks — and accepted rows sort after every unaccepted one.
    """
    rows = [
        AlertConditionOut(
            dedup_key=str(row[0]),
            trigger=str(row[1]),
            severity=str(row[2]),
            title=mask_and_truncate(str(row[3]), _TITLE_CHARS),
            opened_at=row[4],
            last_seen_at=row[5],
            deviation=None if judge is None else judge.alert(str(row[0])),
        )
        for row in conn.execute(_OPEN_ALERTS_SQL).fetchall()
    ]
    return AlertsOut(
        as_of=now,
        provider=provider,
        open=sorted(rows, key=is_accepted),
        accepted=sum(1 for item in rows if is_accepted(item)),
        failure_alerts=failure_alerts,
    )


# ── backups ────────────────────────────────────────────────────────────────────────────────

#: There is no recorded drill to read: `python -m dataplatform.store.backup drill` prints its
#: report and exits. Said on the page rather than left blank, because a blank reads as "passed".
NO_RESTORE_DRILL_RECORD: Final = (
    "no restore-drill result is recorded: the drill prints its report and writes none"
)


def _count_lines(path: Path) -> int:
    count = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            count += chunk.count(b"\n")
    return count


def _postgres_stream(settings: Settings, now: datetime, line: JobLineOut | None) -> BackupStreamOut:
    directory = postgres_dir(settings)
    try:
        dumps = list_backups(directory, settings.tzinfo)
        if not dumps:
            return BackupStreamOut(job="postgres_backup", line=line, newest=None, files=0)
        stamp = max(dumps)
        path = dumps[stamp]
        sha = load_sidecar(path).get("sha256")
        newest = BackupFileOut(
            name=path.name,
            bytes=path.stat().st_size,
            written_at=stamp,
            age_seconds=(now - stamp).total_seconds(),
            sha256=sha if isinstance(sha, str) else None,
        )
    except (OSError, BackupError) as error:
        return BackupStreamOut(
            job="postgres_backup", line=line, newest=None, files=0, error=str(error)
        )
    return BackupStreamOut(job="postgres_backup", line=line, newest=newest, files=len(dumps))


def _l0_stream(settings: Settings, now: datetime, line: JobLineOut | None) -> BackupStreamOut:
    manifest = settings.backup_root / "l0" / "MANIFEST.sha256"
    try:
        if not manifest.is_file():
            return BackupStreamOut(job="l0_backup", line=line, newest=None, files=0)
        stat = manifest.stat()
        written = datetime.fromtimestamp(stat.st_mtime, tz=settings.tzinfo)
        newest = BackupFileOut(
            name=manifest.name,
            bytes=stat.st_size,
            written_at=written,
            age_seconds=(now - written).total_seconds(),
            entries=_count_lines(manifest),
        )
    except OSError as error:
        return BackupStreamOut(job="l0_backup", line=line, newest=None, files=0, error=str(error))
    return BackupStreamOut(job="l0_backup", line=line, newest=newest, files=1)


def read_backups(
    settings: Settings, *, now: datetime, lines: Mapping[str, JobLineOut]
) -> BackupsOut:
    """The backup panel: each stream's job line and the newest file it left under BACKUP_ROOT.

    What it does: lists finished dumps the way retention and the drill do
    (`store.backup.list_backups` — named like ours *and* with a valid sidecar) and stats the L0
    manifest. `lines` is the Ops page's job lines by name, so "last run" is the jobs table's.
    What it assumes: nothing about BACKUP_ROOT being mounted. The status container does not see the
    host's backup directory unless it is bound in, and `root_visible=false` says exactly that
    instead of reporting "no backups".
    What it never does: open, verify or restore a dump.
    """
    root = settings.backup_root
    return BackupsOut(
        as_of=now,
        backup_root=str(root),
        root_visible=root.is_dir(),
        postgres=_postgres_stream(settings, now, lines.get("postgres_backup")),
        l0=_l0_stream(settings, now, lines.get("l0_backup")),
        restore_drill=NO_RESTORE_DRILL_RECORD,
    )
