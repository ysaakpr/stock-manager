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
    EOD_PIPELINE,
    JOB_NAME,
    Job,
    JobContext,
    JobFn,
    JobNotRegisteredError,
    JobRegistry,
    default_registry,
    eod_pipeline,
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
    "DEFAULT_HEARTBEAT_INTERVAL",
    "DEFAULT_SCHEDULER_ID",
    "EOD_PIPELINE",
    "JOB_NAME",
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
    "default_registry",
    "eod_pipeline",
    "read_heartbeat",
]
