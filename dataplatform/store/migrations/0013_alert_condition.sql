-- 0013_alert_condition — which alert conditions are open, so an onset pages once (M13.2).
--
-- The gap this closes. `BaseAlerter` suppresses repeats of a dedup_key for a window, in process
-- memory (C.3). That is the right shape for a burst inside one run, and the wrong one for a
-- condition that lasts: a source FAILED for five days, a quality check red for a week, a holiday
-- file two months from running out. Re-checked every few minutes by `failure_alerts`, each of
-- those would page again every time the window lapsed and again on every scheduler restart. This
-- table is what "already told them" means across ticks and restarts: one row per condition key,
-- open from the tick that first saw it (the onset, which is the only tick that pages) until the
-- first tick that no longer sees it (which sends the resolution and stamps `resolved_at`).
--
-- One row per key rather than one per episode: the episode history is the alert channel's and the
-- log's, and what the trigger needs to ask — "is this key open right now" — is a primary-key read.
-- A re-onset after a resolution reopens the same row with a new `opened_at`.
--
-- Conventions are 0001_init's: the runner wraps this file in one transaction, so no BEGIN/COMMIT
-- and no IF NOT EXISTS; it runs exactly once. Number 0013 is this task's reservation.

CREATE TABLE alert_condition (
    dedup_key    text        PRIMARY KEY,
    trigger      text        NOT NULL CHECK (trigger IN (
                                 'ingest_failed_streak', 'quality_red', 'calendar_expiry',
                                 'job_failed')),
    severity     text        NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
    title        text        NOT NULL,
    opened_at    timestamptz NOT NULL,
    last_seen_at timestamptz NOT NULL,
    resolved_at  timestamptz,
    CONSTRAINT alert_condition_seen_after_open CHECK (last_seen_at >= opened_at),
    CONSTRAINT alert_condition_resolved_after_open
        CHECK (resolved_at IS NULL OR resolved_at >= opened_at)
);
COMMENT ON TABLE alert_condition IS
    'D5 · One row per alert condition the failure_alerts job has seen, written only by '
    'dataplatform.alert_triggers. resolved_at IS NULL means the condition is open and has already '
    'paged; a tick that still sees it pages nothing. Survives restarts, unlike the alerter''s '
    'in-process dedup window.';
COMMENT ON COLUMN alert_condition.dedup_key IS
    'The key the onset alert was sent under, e.g. ingest:nse_bhavcopy:failed_streak. Stable across '
    'repeats of the same problem; never contains a timestamp.';
COMMENT ON COLUMN alert_condition.last_seen_at IS
    'The latest tick that still saw the condition — how long a silent open condition has lasted.';

CREATE INDEX alert_condition_open ON alert_condition (trigger) WHERE resolved_at IS NULL;
