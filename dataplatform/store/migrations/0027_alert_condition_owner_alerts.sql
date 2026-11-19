-- 0027_alert_condition_owner_alerts — the owner's plain-English alerts page through the same ledger.
--
-- dataplatform.owner_alerts sends the owner short Telegram messages: a kill switch tripped or
-- reset, a book past its loss limits, a key dataset gone stale (states, reconciled onset/clear like
-- the M13.2 triggers), and one-shot messages — the morning summary, a paper book's activity, the
-- M17 nightly summary, a degraded night, the weekly decisions list — recorded as rows opened and
-- resolved at the same instant, so each is sent once per key, across restarts.
--
-- Widening a CHECK list only: no row changes. Conventions are 0001_init's: the runner wraps this
-- file in one transaction, so no BEGIN/COMMIT and no IF NOT EXISTS; it runs exactly once.

ALTER TABLE alert_condition
    DROP CONSTRAINT alert_condition_trigger_check,
    ADD CONSTRAINT alert_condition_trigger_check CHECK (trigger IN (
        'ingest_failed_streak', 'quality_red', 'calendar_expiry', 'job_failed', 'ops_guardian',
        'owner_kill_switch', 'owner_loss', 'owner_stale_data', 'owner_event'));
