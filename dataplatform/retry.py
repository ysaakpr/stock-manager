"""The "not yet — a later fire today will retry" marker a scheduled job's failure can carry.

Some jobs fire several times an evening because their input appears at an unmeasured moment inside
a window: `tri_evening` asks at 19:50, 20:50 and 21:30 for a level NSE Indices publishes near
20:47. Its 19:50 fire usually fails, correctly and loudly, with "the endpoint does not carry D
yet". That run is FAILED in `job_run` and `/status` says so; what it must not do is page someone
when the 20:50 fire is about to land it (`alert_triggers.failed_job_conditions`).

The marker is a type, not a phrase in the error text: an exception class opts in by subclassing
`RetryPendingError`, the scheduler runner records `job_run.retry_pending` from `isinstance`, and
the alert trigger reads that column. Nothing anywhere matches on the message.

This module imports nothing, so an ingest module can opt in without pulling in the scheduler.
"""

from __future__ import annotations

__all__ = ["RetryPendingError"]


class RetryPendingError(Exception):
    """The run failed because its input is not available *yet*, and retrying later is the remedy.

    What it says: nothing broke; the source has not published what the job needs, and a later fire
    of the same job is expected to find it. A subclass is still a failure — the run is recorded
    FAILED, the sync row parks retryable — and it still pages when no later fire is due that day
    (`alert_triggers.failed_job_conditions` decides that against the job's own schedule).
    What it never means: "ignore this". A job whose *last* fire of the day raises one is paged like
    any other failed job.
    """
