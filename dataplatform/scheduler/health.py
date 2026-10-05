"""Whether each registered job is actually running on schedule — read off `job_run`, not assumed.

The heartbeat (`runner.read_heartbeat`) answers "is a scheduler process alive". It cannot answer
"did the EOD pipeline run", and on this deployment the two came apart completely: a systemd timer
fired `daily_snapshot` every weekday, so `job_run` was busy and the snapshot sources were current,
while `eod_pipeline` had never run once and the bhavcopy family fell three weeks behind (2026-10-05
audit). Nothing on the status surface could tell those apart. This module can: for each registered
job it compares the newest successful run against the newest fire its cron says should already have
finished, and calls the job `OVERDUE` when the success is older — or `NEVER_RAN` when there is no
run at all.

Pure where it can be: `assess` takes the rows and an instant and is tested without a database;
`read_job_health` is the one query.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final
from zoneinfo import ZoneInfo

from dataplatform.scheduler.registry import Job
from dataplatform.store.db import Connection

__all__ = [
    "LOOKBACK",
    "JobHealth",
    "JobHealthState",
    "LastRuns",
    "assess",
    "last_due_fire",
    "read_job_health",
]

#: How far back `last_due_fire` searches. Every registered cron fires at least weekly, so a month
#: always finds one; a job whose schedule is rarer than that has no "due" fire and cannot be
#: OVERDUE, only NEVER_RAN — which is the honest answer for a schedule this module cannot see.
LOOKBACK: Final = timedelta(days=31)


class JobHealthState(StrEnum):
    """One job's standing, worst-first in what an operator should do about it."""

    NEVER_RAN = "NEVER_RAN"  # no job_run row at all: scheduled in code, never fired anywhere
    FAILING = "FAILING"  # the newest attempt failed or overran
    OVERDUE = "OVERDUE"  # no success since a fire that should have finished by now
    RUNNING = "RUNNING"  # an attempt is in flight and the last due fire is covered or pending
    OK = "OK"


@dataclass(frozen=True, slots=True)
class LastRuns:
    """The two `job_run` facts health needs: the newest attempt, and the newest success."""

    last_state: str | None = None
    last_started_at: datetime | None = None
    last_error: str | None = None
    last_success_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class JobHealth:
    name: str
    cron: str
    state: JobHealthState
    due_since: datetime | None
    runs: LastRuns

    @property
    def healthy(self) -> bool:
        return self.state in (JobHealthState.OK, JobHealthState.RUNNING)


def last_due_fire(job: Job, now: datetime, timezone: ZoneInfo) -> datetime | None:
    """The newest fire time of `job` whose budget has elapsed by `now`, or `None` in `LOOKBACK`.

    A fire still inside its own timeout is not yet owed a success — a 18:30 job is not late at
    18:31 — so the instant compared is `fire + timeout <= now`.
    """
    trigger = job.trigger(timezone)
    cursor = now - LOOKBACK
    due: datetime | None = None
    previous: datetime | None = None
    while True:
        fire = trigger.get_next_fire_time(previous, cursor)
        if fire is None or fire + job.timeout > now:
            return due
        due = fire
        previous = fire
        cursor = fire + timedelta(seconds=1)


def assess(job: Job, runs: LastRuns, *, now: datetime, timezone: ZoneInfo) -> JobHealth:
    """Classify one job from its newest attempt and newest success.

    What it does: NEVER_RAN with no attempt at all; FAILING when the newest attempt is FAILED or
    TIMED_OUT; OVERDUE when no success started at or after the newest due fire; RUNNING when an
    attempt is in flight; OK otherwise. A SKIPPED_LOCKED newest attempt is judged by the success
    behind it — another process held the lock, which is the job running, not the job broken.
    What it never does: read a clock (`now` is the caller's injected instant).
    """
    due = last_due_fire(job, now, timezone)
    state: JobHealthState
    if runs.last_state is None:
        state = JobHealthState.NEVER_RAN
    elif runs.last_state in ("FAILED", "TIMED_OUT"):
        state = JobHealthState.FAILING
    elif due is not None and (runs.last_success_at is None or runs.last_success_at < due):
        state = JobHealthState.RUNNING if runs.last_state == "RUNNING" else JobHealthState.OVERDUE
    elif runs.last_state == "RUNNING":
        state = JobHealthState.RUNNING
    else:
        state = JobHealthState.OK
    return JobHealth(name=job.name, cron=job.cron, state=state, due_since=due, runs=runs)


_LAST_RUNS_SQL: Final = """
    SELECT j.job_name, j.state, j.started_at, j.error, s.last_success_at
    FROM (
        SELECT DISTINCT ON (job_name) job_name, state, started_at, error
        FROM job_run
        ORDER BY job_name, started_at DESC
    ) j
    LEFT JOIN (
        SELECT job_name, max(started_at) AS last_success_at
        FROM job_run WHERE state = 'SUCCEEDED' GROUP BY job_name
    ) s ON s.job_name = j.job_name
"""


def read_job_health(
    conn: Connection, jobs: Iterable[Job], *, now: datetime, timezone: ZoneInfo
) -> tuple[JobHealth, ...]:
    """Every job in `jobs`, assessed against `job_run` as of `now`, in registry order.

    Reports the *registered* jobs, not the job names `job_run` happens to hold: a job that has never
    fired has no row, and listing only rows is exactly how a never-run job stays invisible.
    """
    rows = {
        str(row[0]): LastRuns(
            last_state=str(row[1]),
            last_started_at=row[2],
            last_error=None if row[3] is None else str(row[3]),
            last_success_at=row[4],
        )
        for row in conn.execute(_LAST_RUNS_SQL).fetchall()
    }
    return tuple(
        assess(job, rows.get(job.name, LastRuns()), now=now, timezone=timezone) for job in jobs
    )
