-- 0007_auth_required_decision — two new decision kinds for the broker auth interlock (task M5.15).
--
-- §4.4's interlock covers red data with SKIPPED_DATA_RED; M5.15 adds the same failure class for a
-- dead broker session — the daily OAuth+2FA logout that Indian brokers force (NSE consolidated NNF
-- circular INVG/73992 §8.3.2.1.8) can leave the daily loop holding a lapsed session at market open.
-- On such a day the loop journals AUTH_REQUIRED (SYSTEM, no order placed, the same shape as
-- SKIPPED_DATA_RED) and DEFERRED for each staged decision it carried to the next valid session
-- rather than dropped — a decision that evaporates because of an auth failure would be a journal
-- lie (invariant #12).
--
-- decision_journal.decision is text + CHECK (0001_init, decision #13): a new kind is a widened
-- CHECK, not a new type. Postgres names an inline column CHECK `<table>_<column>_check`, so the
-- constraint dropped and re-added here is the one 0001_init created. APPEND-ONLY still holds: the
-- reject_mutation triggers guard rows, not the table's own constraint set, so a DDL widening of the
-- allowed values does not touch a single existing row.
--
-- Conventions are 0001_init's: the runner wraps this file in one transaction, so no BEGIN/COMMIT
-- and no IF NOT EXISTS; it runs exactly once.

ALTER TABLE decision_journal DROP CONSTRAINT decision_journal_decision_check;

ALTER TABLE decision_journal ADD CONSTRAINT decision_journal_decision_check CHECK (decision IN (
    'HOLD', 'BUY', 'SELL', 'ESCALATE', 'HEARTBEAT',
    'SKIPPED_DATA_RED', 'AUTH_REQUIRED', 'DEFERRED', 'RAIL_BLOCK',
    'POLICY_PROPOSAL'));
