"""The job registry — the complete list of what the scheduler is allowed to run.

A `Job` is a name, a cron expression, a function, and a time budget. Nothing else: the runner owns
locking, logging and recording, so a job body is only the work itself and can be tested without a
scheduler anywhere near it.

Two properties are deliberate. First, **the registry is explicit** — a job exists because it is
constructed here and handed to a `JobRegistry`, never because a decorator ran on an import that
happened to be reached. A scheduler whose contents depend on import order is a scheduler nobody can
audit. Second, **a job is validated at construction**: the name shape and the cron expression are
checked the moment the object exists, so a typo in a schedule fails at import — in `make check` —
rather than at 18:30 on a trading day when the job silently never fires.

Jobs take a `JobContext` rather than no arguments, because a job needs the clock and the settings
and must not go and get them itself (B10, invariant #11).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger

from dataplatform.clock import Clock
from dataplatform.config import Settings
from dataplatform.logging import get_logger

__all__ = [
    "EOD_PIPELINE",
    "JOB_NAME",
    "Job",
    "JobContext",
    "JobFn",
    "JobNotRegisteredError",
    "JobRegistry",
    "default_registry",
    "eod_pipeline",
]

#: Job names are lower snake_case and never start with an underscore, which is what keeps them
#: disjoint from the scheduler's own internal ids (`runner.TICK_JOB_ID`).
JOB_NAME = re.compile(r"^[a-z][a-z0-9_]{2,63}$")

log = get_logger(__name__)


class JobNotRegisteredError(LookupError):
    """A job was asked for by a name the registry does not know."""


@dataclass(frozen=True, slots=True)
class JobContext:
    """Everything a job function is handed, and the only way it learns any of it.

    A job that wants the current date reads `context.clock`, and a job that wants the database
    reads `context.settings` — neither is fetched from module state, so a replay or a test can run
    the same function against a frozen clock and a scratch database without patching anything.
    """

    job_name: str
    run_id: UUID
    clock: Clock
    settings: Settings


#: What the scheduler calls. A job reports failure by raising; the runner catches it, records the
#: run FAILED and keeps the scheduler alive, so no job needs its own try/except to be safe.
JobFn = Callable[[JobContext], None]


@dataclass(frozen=True, slots=True)
class Job:
    """One scheduled unit of work, validated at construction.

    What it does: binds a callable to a cron schedule under a stable name, with a duration budget
    the runner reports against.
    What it assumes: `fn` is safe to run concurrently with *other* jobs — the runner's advisory
    lock only serialises a job against itself.
    What it never does: run anything. Constructing a `Job` has no side effect beyond validation.
    """

    name: str
    cron: str
    fn: JobFn
    timeout: timedelta
    description: str = ""

    def __post_init__(self) -> None:
        if not JOB_NAME.match(self.name):
            raise ValueError(
                f"job name {self.name!r} must be lower snake_case, 3-64 chars, and must not "
                "start with an underscore (that prefix is reserved for the scheduler's own "
                "internal jobs, such as the heartbeat tick)"
            )
        if self.timeout <= timedelta(0):
            raise ValueError(f"job {self.name!r} needs a positive timeout, got {self.timeout!r}")
        self.trigger()  # validate the cron now, not on the morning it was supposed to fire

    def trigger(self, timezone: ZoneInfo | None = None) -> Any:
        """This job's cron expression as an APScheduler trigger, in `timezone`.

        Raises `ValueError` naming the job when the expression is not a valid 5-field crontab —
        the whole reason this is called from `__post_init__`.
        """
        try:
            return CronTrigger.from_crontab(self.cron, timezone=timezone)
        except ValueError as error:
            raise ValueError(
                f"job {self.name!r} has an invalid cron expression {self.cron!r}: {error}"
            ) from error


class JobRegistry:
    """The set of jobs one scheduler process may run, keyed by name.

    What it does: holds jobs, rejects a duplicate name, and fails loud on an unknown one.
    What it assumes: it is built once at startup and not mutated afterwards.
    What it never does: create a job implicitly. A name that is not in here cannot be run, which
    is the property that makes `run-once` safe to expose to an agent.
    """

    __slots__ = ("_jobs",)

    def __init__(self, jobs: Iterable[Job] = ()) -> None:
        self._jobs: dict[str, Job] = {}
        for job in jobs:
            self.register(job)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({list(self.names())!r})"

    def __iter__(self) -> Iterator[Job]:
        """Jobs in registration order — the order the scheduler adds them in."""
        return iter(self._jobs.values())

    def __len__(self) -> int:
        return len(self._jobs)

    def __contains__(self, name: object) -> bool:
        return name in self._jobs

    def register(self, job: Job) -> Job:
        """Add `job`; raise if a job of that name is already registered.

        Silently replacing would mean two schedules for one name and no way to tell which one is
        live, so a collision is an error rather than a last-writer-wins.
        """
        if job.name in self._jobs:
            raise ValueError(f"job {job.name!r} is already registered")
        self._jobs[job.name] = job
        return job

    def get(self, name: str) -> Job:
        """The job called `name`, or `JobNotRegisteredError` listing what is registered."""
        try:
            return self._jobs[name]
        except KeyError:
            known = ", ".join(self.names()) or "none"
            raise JobNotRegisteredError(
                f"no job named {name!r}; registered jobs: {known}"
            ) from None

    def names(self) -> tuple[str, ...]:
        """Registered job names, in registration order."""
        return tuple(self._jobs)


# ── the registered jobs ─────────────────────────────────────────────────────────────────────


def eod_pipeline(context: JobContext) -> None:
    """The daily end-of-day pipeline. A no-op placeholder until M1.11 wires the real one.

    What it does today: emits one structured event naming the trading date it would have
    processed, which is enough for the run to be observable end to end — the run is recorded, the
    heartbeat moves, and `/health` goes fresh.
    What it assumes: the trading date is the injected clock's today. M1.11 replaces that with the
    calendar's most recent expected session (C.2), because a Monday run processes Friday.
    What it never does: fetch, write L0, or touch a decision. Wiring D1 into this function is
    M1.11's task and nothing before it should depend on this body.
    """
    log.info(
        "job.eod_pipeline.placeholder",
        trading_date=context.clock.today().isoformat(),
        run_id=str(context.run_id),
        note="no-op until M1.11 wires the ingestion pipeline",
    )


#: The one job M0.6 registers (§8.1: one daily EOD pipeline). 18:30 IST on weekdays — after the
#: 15:30 close and after NSE publishes the day's bhavcopy, with the timezone supplied by the
#: scheduler from `Settings`, never assumed to be the host's.
EOD_PIPELINE = Job(
    name="eod_pipeline",
    cron="30 18 * * mon-fri",
    fn=eod_pipeline,
    timeout=timedelta(minutes=45),
    description="Daily EOD ingest → validate → normalize → publish (placeholder until M1.11)",
)


def default_registry() -> JobRegistry:
    """The registry a production scheduler process runs.

    A fresh object each call rather than a module-level singleton: two processes in one test, or a
    test that registers an extra job, must not be able to mutate what the next one sees.
    """
    return JobRegistry([EOD_PIPELINE])
