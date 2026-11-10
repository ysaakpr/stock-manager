-- 0026_alert_condition_ops_guardian — the box's ops guardian pages through the same ledger.
--
-- The ops guardian is a systemd user service outside this repo's process tree (it watches the
-- scheduler, the status API, the backtest logs and a durable agenda of planned work). It writes
-- its open alerts to a heartbeat file; the `ops_guardian` scheduler job reads that file and
-- reconciles those alerts into `alert_condition` under the new trigger value, so a guardian
-- onset pages once through the configured alerter and its clearing pages one resolution — the
-- same contract as the four M13.2 triggers (dataplatform.ops_guardian).
--
-- Widening a CHECK list only: no row changes. Conventions are 0001_init's: the runner wraps this
-- file in one transaction, so no BEGIN/COMMIT and no IF NOT EXISTS; it runs exactly once.

ALTER TABLE alert_condition
    DROP CONSTRAINT alert_condition_trigger_check,
    ADD CONSTRAINT alert_condition_trigger_check CHECK (trigger IN (
        'ingest_failed_streak', 'quality_red', 'calendar_expiry', 'job_failed', 'ops_guardian'));
