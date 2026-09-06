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
    "CONSTITUENTS_SNAPSHOT",
    "EOD_PIPELINE",
    "JOB_NAME",
    "Job",
    "JobContext",
    "JobFn",
    "JobNotRegisteredError",
    "JobRegistry",
    "constituents_snapshot",
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
    """The daily end-of-day pipeline (M1.10): the latest session, fetched to PUBLISHED, archived.

    What it does: drives every daily NSE source for the latest trading session down
    `fetch → L0 → parse → L1 → sync_state`, self-heals any FAILED(retryable) date in the lookback
    window first, runs the D7 gap check, publishes the day's archive bundle, and alerts on any
    source left FAILED. It raises `EodPipelineError` when the target session did not publish, so the
    run is recorded FAILED and the next run self-heals it.
    What it assumes: the injected clock and settings are the run's (B10), the database is migrated,
    and the network is reachable — the real wiring is built inside `run_eod_pipeline`.
    What it never does: decide the time for itself, or report a green run on a day its session never
    landed. The import is deferred so this module (loaded by the scheduler) does not pull in the
    whole ingest stack at import time, and so `dataplatform.ingest.eod` can name `JobContext`
    without an import cycle.
    """
    from dataplatform.ingest.eod import run_eod_pipeline

    run_eod_pipeline(context)


#: The one job M0.6 registers (§8.1: one daily EOD pipeline). 18:30 IST on weekdays — after the
#: 15:30 close and after NSE publishes the day's bhavcopy, with the timezone supplied by the
#: scheduler from `Settings`, never assumed to be the host's.
EOD_PIPELINE = Job(
    name="eod_pipeline",
    cron="30 18 * * mon-fri",
    fn=eod_pipeline,
    timeout=timedelta(minutes=45),
    description="Daily EOD ingest → validate → normalize → publish → archive (M1.10)",
)


def constituents_snapshot(context: JobContext) -> None:
    """The weekly index-constituents snapshot (M10.2): the survivorship-bias killer.

    What it does: snapshots the broad and sectoral niftyindices constituent lists with this week's
    capture date, appending a dated membership record per slug so real point-in-time sector history
    accumulates going forward — niftyindices publishes "as of today" only, so a static-today map
    applied backward is survivorship-biased, and this is the mechanism that builds true history
    over time. Idempotent per (slug, week): a re-run of the same week is a no-op. One slug's fetch
    failure is journaled, alerted and skipped; it never aborts the others.
    What it assumes: the injected clock and settings are the run's (B10), the database is migrated,
    and the network is reachable — the real wiring is built inside `run_constituents_snapshot`.
    What it never does: manufacture past history, or silently fill a missed week (a gap stays a
    gap). The import is deferred so this module (loaded by the scheduler) does not pull in the
    ingest stack at import time, and so `constituents_ingest` can name `JobContext` without a cycle.
    """
    from dataplatform.ingest.constituents_snapshot_job import run_constituents_snapshot

    run_constituents_snapshot(context)


#: The weekly constituents snapshot (M10.2). 20:00 IST every Saturday — the market is closed and the
#: published lists are stable for the week, and the Saturday capture is stamped `as_of` that ISO
#: week's Sunday (`week_anchor`) so a second run of the same week is a true no-op. The timezone is
#: supplied by the scheduler from `Settings`, never the host's.
CONSTITUENTS_SNAPSHOT = Job(
    name="constituents_snapshot",
    cron="0 20 * * sat",
    fn=constituents_snapshot,
    timeout=timedelta(minutes=30),
    description="Weekly dated snapshot of index constituents — forward sector history (M10.2)",
)


def l0_verify(context: JobContext) -> None:
    """The weekly L0 integrity sweep (2026-09-06 audit, finding N6).

    What it does: re-hashes stored payloads against their sidecars and raises an ERROR
    `quality_flag` plus a CRITICAL alert for every one that does not match. Rolling over a trailing
    window most weeks, and over the whole lake in the first week of each month — L0 is write-once,
    so what changes is what was written since, but bit-rot in the old tail is exactly what nothing
    else would ever read.
    What it assumes: the injected clock and settings are the run's (B10) and the database is
    migrated.
    What it never does: repair. `L0Store` refuses to modify a stored payload for any reason,
    including corruption (AGENTIC_CONTEXT §3.10); a defect is a human's call, and re-fetching over
    it would destroy the evidence. The import is deferred for the same reason the others are.
    """
    from dataplatform.store.db import connection
    from dataplatform.store.l0_verify import run_l0_verify

    with connection(context.settings) as conn:
        result = run_l0_verify(conn=conn, settings=context.settings, clock=context.clock)
        conn.commit()
    if not result.ok:
        raise RuntimeError(result.summary())


#: The weekly L0 sweep. 03:00 IST on Sunday — no market, no campaign window, and the quietest hour
#: for a pass that reads gigabytes off disk. The timezone comes from `Settings`, never the host's.
L0_VERIFY = Job(
    name="l0_verify",
    cron="0 3 * * sun",
    fn=l0_verify,
    timeout=timedelta(hours=2),
    description="Weekly L0 checksum sweep; full pass in the first week of each month",
)


def identity_refresh(context: JobContext) -> None:
    """The weekly identity-master refresh (2026-09-06 audit, finding N4).

    What it does: fetches `EQUITY_L.csv` and `symbolchange.csv` into L0 under the host lease, then
    re-derives `security_master`, `symbol_history` and `exchange_listing` by reading both payloads
    back out of the lake. `ops/runbooks/identity-master.md` has said "weekly" since M1.7; nothing
    did it weekly, and until this job the two files had never been fetched at all — the production
    master was built from `tests/fixtures/`, outside L0's checksums and outside the backup.
    What it assumes: the injected clock and settings are the run's (B10). A re-run on the same day
    re-fetches nothing, because L0 already holds that date's payloads.
    What it never does: overwrite a snapshot. Every week's copy is kept, which is the only way the
    accumulated series can ever reconstruct the symbol history one snapshot cannot show
    (`pit_notes` on both register rows).
    """
    from dataplatform.ingest.identity_refresh import refresh_identity
    from dataplatform.store.db import connection

    with connection(context.settings) as conn:
        report = refresh_identity(conn=conn, clock=context.clock, settings=context.settings)
        conn.commit()
    if not report.ingest.is_clean:
        raise RuntimeError(report.summary())


#: The weekly identity refresh. 07:00 IST on Saturday — after the week's last session, before the
#: constituents snapshot at 20:00, and well clear of any weekday campaign window. The timezone
#: comes from `Settings`, never the host's.
IDENTITY_REFRESH = Job(
    name="identity_refresh",
    cron="0 7 * * sat",
    fn=identity_refresh,
    timeout=timedelta(minutes=15),
    description="Weekly NSE identity-master refresh: fetch to L0, re-derive from L0 (M1.7)",
)


def default_registry() -> JobRegistry:
    """The registry a production scheduler process runs.

    A fresh object each call rather than a module-level singleton: two processes in one test, or a
    test that registers an extra job, must not be able to mutate what the next one sees.
    """
    return JobRegistry([EOD_PIPELINE, CONSTITUENTS_SNAPSHOT, L0_VERIFY, IDENTITY_REFRESH])
