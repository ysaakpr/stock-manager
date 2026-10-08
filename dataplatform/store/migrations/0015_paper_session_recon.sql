-- 0015_paper_session_recon — the paper session's reconciliation and accounting book (M15.3).
--
-- M15.3 routes the daily paper session through the live path's own execution code: orders are
-- staged through the staging coordinator (kill switch first), filled through it into the paper
-- book's accounting book, and that book is reconciled against the paper SimBroker after every
-- session's fills. This migration records what that adds to a session:
--
--   * `recon` — the session's reconciliation: CLEAN, or BREAK with the breaks, keyed
--     `RECON:<date>` with a digest of the breaks as its terms, so the owner resolves a break with a
--     `paper_session_resolution` row exactly as an escalated corporate action is resolved.
--   * `expected_book` — the accounting book after the session (cash, positions), which the next
--     session restores so the two sides of the reconciliation are built independently.
--   * outcome `RECON_BREAK` — red: the session's fills happened (the row carries the book they
--     left, which the next session restores from), but the kill switch tripped and nothing was
--     staged, and the book trades no more until the break is resolved.
--
-- Backward compatible with every row already written: the new columns are NULL on them (the next
-- session seeds its accounting book from the restored broker once, and says so in its recon), and
-- the rewritten constraints say exactly what the 0012 ones said about COMPLETED and
-- SKIPPED_DATA_RED rows.
--
-- Conventions are 0001_init's: the runner wraps this file in one transaction, so no BEGIN/COMMIT
-- and no IF NOT EXISTS; it runs exactly once.

ALTER TABLE paper_session
    ADD COLUMN recon jsonb,
    ADD COLUMN expected_book jsonb,
    DROP CONSTRAINT paper_session_outcome_check,
    ADD CONSTRAINT paper_session_outcome_check
        CHECK (outcome IN ('COMPLETED', 'SKIPPED_DATA_RED', 'RECON_BREAK')),
    DROP CONSTRAINT paper_session_red_places_nothing,
    ADD CONSTRAINT paper_session_red_places_nothing CHECK (
        outcome <> 'SKIPPED_DATA_RED'
        OR (orders = '[]'::jsonb AND pending IS NULL AND NOT rebalanced AND book_digest IS NULL
            AND book_state IS NULL AND actions = '[]'::jsonb AND recon IS NULL
            AND expected_book IS NULL)
    ),
    DROP CONSTRAINT paper_session_completed_has_book,
    ADD CONSTRAINT paper_session_completed_has_book CHECK (
        outcome = 'SKIPPED_DATA_RED' OR (book_digest IS NOT NULL AND book_state IS NOT NULL)
    ),
    ADD CONSTRAINT paper_session_recon_break_stages_nothing CHECK (
        outcome <> 'RECON_BREAK'
        OR (orders = '[]'::jsonb AND recon IS NOT NULL AND recon->>'status' = 'BREAK')
    ),
    ADD CONSTRAINT paper_session_completed_recon_is_clean CHECK (
        outcome <> 'COMPLETED' OR recon IS NULL OR recon->>'status' = 'CLEAN'
    );

COMMENT ON COLUMN paper_session.outcome IS
    'COMPLETED: decided, reconciled clean. SKIPPED_DATA_RED: refused (red data, a tripped kill '
    'switch, or an unresolved escalation or reconciliation break); nothing placed. RECON_BREAK: '
    'the fills left the paper book and the SimBroker disagreeing; the kill switch tripped, nothing '
    'was staged, and the book is refused until the break is resolved (M15.3).';
COMMENT ON COLUMN paper_session.recon IS
    'The session''s reconciliation of the paper book against the SimBroker (M15.3): {status '
    'CLEAN|BREAK, key RECON:<date>, terms, breaks, staged, executed, seeded}. A BREAK is resolved '
    'by a paper_session_resolution row (action_key = key, terms = terms). NULL before M15.3.';
COMMENT ON COLUMN paper_session.expected_book IS
    'The paper book''s own accounting book after the session — {cash, positions: [{isin, '
    'quantity, cost_basis}]}, Decimals as strings — restored by the next session as the side '
    'reconciliation checks the broker against. NULL before M15.3.';
COMMENT ON TABLE paper_session_resolution IS
    'X1 · M13.1 paper trading. One row per escalated corporate action (key and terms), or per '
    'reconciliation break (key RECON:<date>, M15.3), the owner has resolved; the paper session '
    'trades a book again only once every escalation and break on it has a row here. Append-only.';
