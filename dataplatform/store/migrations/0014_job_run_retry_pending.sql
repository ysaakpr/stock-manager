-- 0014_job_run_retry_pending — mark a FAILED run whose job said "not yet, a later fire will retry".
--
-- The gap this closes. `tri_evening` (M13.7) fires at 19:50, 20:50 and 21:30 IST because NSE
-- Indices publishes the session's TRI near 20:47. Its 19:50 fire usually raises
-- `TriNotYetPublishedError`, which is recorded FAILED, and `failure_alerts` (M13.2) then paged a
-- CRITICAL "job raised" most weekday evenings and resolved it after 20:50, which is alert fatigue
-- rather than news. The run is still a failure and stays FAILED here and on `/status/jobs`; this
-- column says *why*, as a type the runner decided (`isinstance(error, RetryPendingError)`), so the
-- trigger can hold the page while a later fire of the same job is still due that day and page
-- when it is not. Nothing reads the error text to decide this.
--
-- Conventions are 0001_init's: the runner wraps this file in one transaction, so no BEGIN/COMMIT
-- and no IF NOT EXISTS; it runs exactly once. Existing rows default to false, which is what every
-- one of them was.

ALTER TABLE job_run
    ADD COLUMN retry_pending boolean NOT NULL DEFAULT false,
    ADD CONSTRAINT job_run_retry_pending_needs_a_failure
        CHECK (NOT retry_pending OR state = 'FAILED');
COMMENT ON COLUMN job_run.retry_pending IS
    'True when the run raised a dataplatform.retry.RetryPendingError: its input was not available '
    'yet and a later fire is the remedy. The run is still FAILED; failure_alerts holds the page '
    'only while a later fire of the same job is due the same day.';
