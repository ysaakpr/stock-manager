-- 0023_fundamentals_requeue_log — the journal of one-time XBRL filing re-queues.
--
-- `dataplatform/ingest/fundamentals_requeue.yaml` names filings the scheduled `fundamentals_forward`
-- job drives once more — outside D28's 7-day re-read window, which is the only other retry they
-- would ever get. One row per item the job reached, written as it finishes the item, records what
-- happened. The row is also what makes the re-queue one-time: an item with a row is never driven
-- again. Append-only by use — nothing updates or deletes a row.
--
--   requeue_id    the re-queue's id in the YAML
--   unit          the nse_xbrl_filing sync_state unit (IF<seq>[-FY])
--   logical_date  the filing date (the sync_state row's logical date)
--   action        REFETCH (may make one request) | RECORD_EVIDENCE (never makes one)
--   outcome       what the item ended as — see `fundamentals_requeue.RequeueOutcome`
--   detail        the sync_state error it left, or why nothing was done
--
-- Conventions are 0001_init's: the runner wraps this file in one transaction, so no BEGIN/COMMIT
-- and no IF NOT EXISTS; it runs exactly once.

CREATE TABLE fundamentals_requeue_log (
    requeue_id   text        NOT NULL,
    unit         text        NOT NULL,
    logical_date date        NOT NULL,
    action       text        NOT NULL CHECK (action IN ('refetch', 'record_evidence')),
    outcome      text        NOT NULL CHECK (outcome IN (
                                 'PUBLISHED', 'FAILED', 'ALREADY_PUBLISHED', 'ATTEMPTED_THIS_RUN',
                                 'NOT_IN_INDEX', 'EVIDENCE_RECORDED', 'NO_EVIDENCE')),
    detail       text,
    consumed_at  timestamptz NOT NULL,
    PRIMARY KEY (requeue_id, unit, logical_date)
);
