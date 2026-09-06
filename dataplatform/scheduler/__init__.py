"""The in-process scheduler (§8.1) — job registry, singleton locking, heartbeat.

`registry` says what may run and when; `runner` runs it, serialises it against itself with a
Postgres advisory lock, records every attempt in `job_run` and moves the heartbeat GET /health
reports. `python -m dataplatform.scheduler` is the entrypoint for both the long-running process
and a one-off `run-once <job>`.

Other packages use this through `read_heartbeat` (the status API) and `SchedulerRunner.run_once`
(anything that wants to trigger a job); nothing outside here writes `job_run` or
`scheduler_heartbeat`.
"""

from dataplatform.scheduler.registry import (
    CONSTITUENTS_SNAPSHOT,
    EOD_PIPELINE,
    JOB_NAME,
    L0_VERIFY,
    Job,
    JobContext,
    JobFn,
    JobNotRegisteredError,
    JobRegistry,
    constituents_snapshot,
    default_registry,
    eod_pipeline,
    identity_refresh,
    l0_verify,
)
from dataplatform.scheduler.runner import (
    ALIVE,
    DEFAULT_HEARTBEAT_INTERVAL,
    DEFAULT_SCHEDULER_ID,
    TICK_JOB_ID,
    Heartbeat,
    JobRun,
    JobState,
    SchedulerRunner,
    build_scheduler,
    read_heartbeat,
)

__all__ = [
    "ALIVE",
    "CONSTITUENTS_SNAPSHOT",
    "DEFAULT_HEARTBEAT_INTERVAL",
    "DEFAULT_SCHEDULER_ID",
    "EOD_PIPELINE",
    "JOB_NAME",
    "L0_VERIFY",
    "TICK_JOB_ID",
    "Heartbeat",
    "Job",
    "JobContext",
    "JobFn",
    "JobNotRegisteredError",
    "JobRegistry",
    "JobRun",
    "JobState",
    "SchedulerRunner",
    "build_scheduler",
    "constituents_snapshot",
    "default_registry",
    "eod_pipeline",
    "identity_refresh",
    "l0_verify",
    "read_heartbeat",
]
