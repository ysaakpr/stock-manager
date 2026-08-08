"""The scheduler runner (§8.1) — one process, APScheduler in-process, one job at a time per job.

`SchedulerRunner.run_once` is the whole mechanism, and the scheduler is a thin wrapper that calls
it on a cron. Everything a scheduled job needs to be operable lives in that one method:

* **A singleton lock.** Postgres advisory lock keyed on the job name, taken on a dedicated
  connection for the duration of the run. Two processes — the container's scheduler and an
  operator's `run-once` — cannot both run the EOD pipeline, which for an ingestion job means they
  cannot both write the same L0 date. A run that loses the race is *recorded* as SKIPPED_LOCKED
  rather than discarded, because a job that silently did not run must not look like a day on
  which nothing was scheduled.
* **A heartbeat.** Every run and every scheduler tick upserts this scheduler's single
  `scheduler_heartbeat` row (declared by M0.5, written only from here), which is what GET /health
  reports. A scheduler that died between EOD runs is otherwise invisible until the morning
  someone notices yesterday's prices never landed.
* **Failure containment.** `run_once` never propagates an exception out of the job function. It
  records FAILED with the error, logs it, and returns — so one broken job cannot take down the
  process that runs the others.

The timeout in a `Job` is a *budget the runner reports on*, not a kill: a run that overruns is
recorded TIMED_OUT after it returns. Killing a Python thread mid-transaction is not something
CPython offers safely, and the singleton lock already gives the property that matters — an
overrunning job blocks its own next run instead of double-running beside itself.
"""

from __future__ import annotations

import os
import socket
import time
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from apscheduler.events import EVENT_JOB_ERROR, JobExecutionEvent
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from psycopg.types.json import Json

from dataplatform.clock import Clock, SystemClock
from dataplatform.config import Settings, get_settings
from dataplatform.logging import get_logger, log_context
from dataplatform.scheduler.registry import Job, JobContext, JobRegistry, default_registry
from dataplatform.store.db import Connection, connection

__all__ = [
    "ALIVE",
    "DEFAULT_HEARTBEAT_INTERVAL",
    "DEFAULT_SCHEDULER_ID",
    "TICK_JOB_ID",
    "Heartbeat",
    "JobRun",
    "JobState",
    "SchedulerRunner",
    "build_scheduler",
    "read_heartbeat",
]

#: `scheduler_heartbeat.scheduler_id` for this deployment's one scheduler. Stable across restarts
#: by design (0002_status_surface.sql): a PID-keyed id would leave a dead scheduler's last beat
#: behind as a permanently fresh-looking extra row.
DEFAULT_SCHEDULER_ID = "scheduler"

#: APScheduler's internal id for the tick job. Underscore-prefixed so it can never collide with a
#: registered job (`registry.JOB_NAME` forbids that prefix).
TICK_JOB_ID = "_heartbeat"

#: `detail.state` on a beat written by the tick — the scheduler is up and no job is running.
ALIVE = "ALIVE"

#: How often the scheduler proves it is alive when no job is due. A minute is far below any
#: plausible staleness threshold for a daily pipeline and costs one tiny UPSERT.
DEFAULT_HEARTBEAT_INTERVAL = timedelta(seconds=60)

#: Namespace for this module's two-key advisory locks. Postgres keeps the (int4, int4) lock space
#: separate from the (int8) one the migration runner uses, so the two cannot collide even by
#: accident; the constant exists so a `pg_locks` row is attributable on sight.
_LOCK_CLASS_ID = 8_240_600

log = get_logger(__name__)


class JobState(StrEnum):
    """The lifecycle of one attempt to run a job — `job_run.state` verbatim."""

    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    SKIPPED_LOCKED = "SKIPPED_LOCKED"


@dataclass(frozen=True, slots=True)
class JobRun:
    """One attempt to run a job, as recorded in `job_run`."""

    run_id: UUID
    job_name: str
    state: JobState
    instance: str
    started_at: datetime
    finished_at: datetime | None = None
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        """True only for a clean run — an overrun budget is not a success."""
        return self.state is JobState.SUCCEEDED


@dataclass(frozen=True, slots=True)
class Heartbeat:
    """One `scheduler_heartbeat` row, with its `detail` jsonb unpacked into named fields.

    `name` is the row's `scheduler_id`. `state`, `instance`, `job` and `run_id` come out of the
    scheduler-owned `detail` column, which is free-form in the schema precisely so that this shape
    can grow without a migration — so a beat written by an older process is read here with
    defaults rather than a KeyError.
    """

    name: str
    beat_at: datetime
    state: str = ALIVE
    instance: str = ""
    job: str | None = None
    run_id: UUID | None = None

    def age(self, now: datetime) -> timedelta:
        """How stale this beat is at `now`, which the caller reads from an injected clock."""
        return now - self.beat_at


class SchedulerRunner:
    """Runs registered jobs, one at a time per job, recording every attempt.

    What it does: owns the lock, the `job_run` history and the heartbeat. `run_once` is safe to
    call from anywhere — the scheduler thread, the CLI, a future agent tool — because the lock is
    what serialises them, not a convention about who is allowed to call it.
    What it assumes: the database is migrated (`make migrate`) and reachable. An unreachable
    database raises rather than being logged and skipped: a scheduler that cannot record what it
    did is not a scheduler anyone can trust to have run.
    What it never does: let a job's exception escape, and never decide the time for itself (B10).
    """

    __slots__ = ("clock", "instance", "registry", "scheduler_id", "settings")

    def __init__(
        self,
        registry: JobRegistry | None = None,
        *,
        settings: Settings | None = None,
        clock: Clock | None = None,
        instance: str | None = None,
        scheduler_id: str = DEFAULT_SCHEDULER_ID,
    ) -> None:
        self.settings = get_settings() if settings is None else settings
        self.registry = default_registry() if registry is None else registry
        self.clock = SystemClock(self.settings.tzinfo) if clock is None else clock
        self.instance = _instance_id() if instance is None else instance
        self.scheduler_id = scheduler_id

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.registry!r}, instance={self.instance!r})"

    # ── running ─────────────────────────────────────────────────────────────────────────────

    def run_once(self, job_name: str) -> JobRun:
        """Run one registered job now, under its singleton lock, and record the attempt.

        What it does: takes the job's advisory lock, writes a RUNNING row, calls the function,
        then finalises the row as SUCCEEDED, FAILED or TIMED_OUT and moves the job's heartbeat.
        Returns the recorded run; the caller reads `.state` rather than catching anything.
        What it assumes: nothing about who else is running. If another process holds the lock this
        returns a SKIPPED_LOCKED run immediately instead of queueing behind it — a daily job that
        is already running does not need to run twice in a row.
        What it never does: raise because the *job* failed. It does raise for an unknown job name
        and for a database that cannot be reached, both of which are operator errors rather than
        job outcomes.
        """
        job = self.registry.get(job_name)  # loud, and before any connection is opened
        run_id = uuid4()
        started_at = self.clock.now()

        with (
            log_context(job=job.name, run_id=str(run_id), instance=self.instance),
            connection(self.settings, autocommit=True) as conn,
        ):
            if not _try_lock(conn, job.name):
                log.warning("job.skipped_locked", cron=job.cron)
                run = JobRun(
                    run_id=run_id,
                    job_name=job.name,
                    state=JobState.SKIPPED_LOCKED,
                    instance=self.instance,
                    started_at=started_at,
                    finished_at=started_at,
                    error=None,
                )
                self._insert_run(conn, run)
                self._write_beat(conn, run.state, at=started_at, job=job.name, run_id=run_id)
                return run

            try:
                self._insert_run(
                    conn,
                    JobRun(
                        run_id=run_id,
                        job_name=job.name,
                        state=JobState.RUNNING,
                        instance=self.instance,
                        started_at=started_at,
                    ),
                )
                self._write_beat(conn, JobState.RUNNING, at=started_at, job=job.name, run_id=run_id)
                log.info("job.start", cron=job.cron, timeout_s=job.timeout.total_seconds())

                state, error = self._execute(job, run_id)
                finished_at = self.clock.now()
                run = JobRun(
                    run_id=run_id,
                    job_name=job.name,
                    state=state,
                    instance=self.instance,
                    started_at=started_at,
                    finished_at=finished_at,
                    error=error,
                )
                self._finish_run(conn, run)
                self._write_beat(conn, run.state, at=finished_at, job=job.name, run_id=run_id)
                return run
            finally:
                _unlock(conn, job.name)

    def beat(self) -> Heartbeat:
        """Write the idle process heartbeat and return it.

        The tick that proves the scheduler is alive on a day when nothing is due. A successful EOD
        run yesterday says nothing about whether the process is still up today, which is why the
        tick exists at all and why it is not derived from `job_run`.
        """
        at = self.clock.now()
        with connection(self.settings, autocommit=True) as conn:
            self._write_beat(conn, ALIVE, at=at, job=None, run_id=None)
        log.debug("scheduler.beat", beat_at=at.isoformat())
        return Heartbeat(name=self.scheduler_id, beat_at=at, state=ALIVE, instance=self.instance)

    def _execute(self, job: Job, run_id: UUID) -> tuple[JobState, str | None]:
        """Call the job function, converting its outcome into a state and an error string.

        Elapsed time is measured with `time.monotonic()` rather than the injected clock: this is
        an interval, not a date (see `dataplatform/clock.py`), and it must stay honest under a
        `FrozenClock` so a replay cannot accidentally report every job as instantaneous.
        """
        context = JobContext(
            job_name=job.name, run_id=run_id, clock=self.clock, settings=self.settings
        )
        budget = job.timeout.total_seconds()
        started = time.monotonic()
        try:
            job.fn(context)
        except Exception as error:
            elapsed = time.monotonic() - started
            log.error(
                "job.failed",
                elapsed_s=round(elapsed, 3),
                error_type=type(error).__name__,
                error=str(error),
                exc_info=True,
            )
            return JobState.FAILED, f"{type(error).__name__}: {error}"

        elapsed = time.monotonic() - started
        if elapsed > budget:
            log.error("job.over_budget", elapsed_s=round(elapsed, 3), timeout_s=budget)
            return (
                JobState.TIMED_OUT,
                f"ran for {elapsed:.3f}s against a {budget:.3f}s budget",
            )
        log.info("job.succeeded", elapsed_s=round(elapsed, 3))
        return JobState.SUCCEEDED, None

    # ── recording ───────────────────────────────────────────────────────────────────────────

    def _insert_run(self, conn: Connection, run: JobRun) -> None:
        conn.execute(
            "INSERT INTO job_run (run_id, job_name, state, instance, started_at, finished_at, "
            "error) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                run.run_id,
                run.job_name,
                run.state.value,
                run.instance,
                run.started_at,
                run.finished_at,
                run.error,
            ),
        )

    def _finish_run(self, conn: Connection, run: JobRun) -> None:
        conn.execute(
            "UPDATE job_run SET state = %s, finished_at = %s, error = %s WHERE run_id = %s",
            (run.state.value, run.finished_at, run.error, run.run_id),
        )

    def _write_beat(
        self, conn: Connection, state: str, *, at: datetime, job: str | None, run_id: UUID | None
    ) -> None:
        """Upsert this scheduler's single heartbeat row (0002_status_surface.sql).

        One row per logical scheduler, always holding the latest beat — the per-attempt history is
        `job_run`, and duplicating it here would give /health two sources that can disagree.
        `updated_at` and `beat_at` are the same injected instant: the beat is the write.
        """
        detail = {
            "state": str(state),
            "instance": self.instance,
            "job": job,
            "run_id": None if run_id is None else str(run_id),
            "jobs": list(self.registry.names()),
        }
        conn.execute(
            "INSERT INTO scheduler_heartbeat (scheduler_id, beat_at, detail, updated_at) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (scheduler_id) DO UPDATE SET beat_at = EXCLUDED.beat_at, "
            "detail = EXCLUDED.detail, updated_at = EXCLUDED.updated_at",
            (self.scheduler_id, at, Json(detail), at),
        )


def read_heartbeat(
    settings: Settings | None = None, *, name: str | None = None
) -> Heartbeat | None:
    """The freshest heartbeat, or the one called `name`; `None` when nothing has beaten yet.

    What it does: one read of `scheduler_heartbeat`, with the scheduler-owned `detail` jsonb
    unpacked. This is the function GET /health calls, so it stays a single query with no fallback
    and no cache.
    What it assumes: the schema is migrated. An unmigrated database raises `UndefinedTable`, which
    is the truth an operator needs, not an empty heartbeat that looks like a dead scheduler.
    What it never does: decide whether the beat is *stale*. That threshold is policy (`Settings`)
    and belongs to the caller.
    """
    sql = "SELECT scheduler_id, beat_at, detail FROM scheduler_heartbeat"
    params: tuple[object, ...] = ()
    if name is None:
        sql += " ORDER BY beat_at DESC LIMIT 1"
    else:
        sql += " WHERE scheduler_id = %s"
        params = (name,)
    with connection(settings) as conn:
        row = conn.execute(sql, params).fetchone()
    if row is None:
        return None
    detail = row[2] if isinstance(row[2], dict) else {}
    run_id = detail.get("run_id")
    return Heartbeat(
        name=str(row[0]),
        beat_at=row[1],
        state=str(detail.get("state", ALIVE)),
        instance=str(detail.get("instance", "")),
        job=detail.get("job"),
        run_id=None if run_id is None else UUID(str(run_id)),
    )


# ── the scheduler itself ────────────────────────────────────────────────────────────────────


def build_scheduler(
    runner: SchedulerRunner | None = None,
    *,
    heartbeat_interval: timedelta = DEFAULT_HEARTBEAT_INTERVAL,
) -> Any:
    """An APScheduler `BackgroundScheduler` wired to `runner`'s registry, not yet started.

    What it does: adds one cron job per registered job — every one of them calling `run_once`, so
    the containment and locking above are not something a job can opt out of — plus the interval
    tick that writes the process heartbeat, and a listener that reports any scheduler-level
    failure through structlog rather than stdlib logging.
    What it assumes: cron expressions are in the configured exchange timezone (Asia/Kolkata), not
    the host's. A container in UTC must still run the EOD pipeline after the Indian close.
    What it never does: start itself, or run two instances of one job (`max_instances=1` locally,
    and the advisory lock across processes).
    """
    runner = SchedulerRunner() if runner is None else runner
    timezone = runner.settings.tzinfo
    scheduler = BackgroundScheduler(timezone=timezone)

    for job in runner.registry:
        scheduler.add_job(
            runner.run_once,
            trigger=job.trigger(timezone),
            args=[job.name],
            id=job.name,
            name=job.name,
            max_instances=1,
            # A missed fire is coalesced into one run and is still worth running late: yesterday's
            # bhavcopy is the same file at 19:00 as it was at 18:30. The grace window is the job's
            # own budget, past which a run that has not started yet is a run the next tick owns.
            coalesce=True,
            misfire_grace_time=max(1, int(job.timeout.total_seconds())),
        )

    scheduler.add_job(
        runner.beat,
        trigger=IntervalTrigger(seconds=heartbeat_interval.total_seconds(), timezone=timezone),
        id=TICK_JOB_ID,
        name=TICK_JOB_ID,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_listener(_log_scheduler_error, EVENT_JOB_ERROR)
    log.info(
        "scheduler.built",
        jobs=list(runner.registry.names()),
        timezone=str(timezone),
        heartbeat_interval_s=heartbeat_interval.total_seconds(),
    )
    return scheduler


def _log_scheduler_error(event: JobExecutionEvent) -> None:
    """Report a failure APScheduler itself saw — the heartbeat tick, or a broken trigger.

    A job function's own exception never reaches here: `run_once` catches it and records it. What
    does reach here is infrastructure failing (an unreachable database on a tick), which would
    otherwise land in stdlib logging in a different format from everything else this process logs.
    """
    log.error("scheduler.job_error", job=event.job_id, error=str(event.exception), exc_info=False)


# ── the singleton lock ──────────────────────────────────────────────────────────────────────


def _lock_key(job_name: str) -> int:
    """A stable signed int32 for `pg_try_advisory_lock(classid, objid)`.

    CRC32 rather than `hash()`: the key has to be the same in every process, and Python's string
    hash is salted per process, so `hash()` here would make the lock silently per-process — which
    is exactly the bug this lock exists to prevent.
    """
    digest = zlib.crc32(job_name.encode("utf-8"))
    return digest - 2**32 if digest >= 2**31 else digest


def _try_lock(conn: Connection, job_name: str) -> bool:
    """Take the job's session-level advisory lock, or return False without waiting."""
    row = conn.execute(
        "SELECT pg_try_advisory_lock(%s::int4, %s::int4)", (_LOCK_CLASS_ID, _lock_key(job_name))
    ).fetchone()
    return bool(row is not None and row[0])


def _unlock(conn: Connection, job_name: str) -> None:
    """Release the job's advisory lock.

    Closing the connection would release it anyway; doing it explicitly means a pooled or reused
    connection cannot carry the lock past the run that took it.
    """
    conn.execute(
        "SELECT pg_advisory_unlock(%s::int4, %s::int4)", (_LOCK_CLASS_ID, _lock_key(job_name))
    )


def _instance_id() -> str:
    """`host:pid` — enough to tell two scheduler processes apart in `job_run` and in the log."""
    return f"{socket.gethostname()}:{os.getpid()}"
