-- 0012_paper_session — the daily paper-trading session ledger (M13.1).
--
-- One row per (paper book, trading date) the daily paper session job has *finished* with: either
-- it decided the session (COMPLETED) or it refused to because the data was red (SKIPPED_DATA_RED).
-- The row is what makes the job idempotent — a rerun on a COMPLETED date is a no-op — and it is
-- the book's only durable state. `book_digest` is the sha256 of `book_state`'s canonical bytes;
-- the restored broker must reproduce it before it is used.
--
-- The decisions themselves live in `decision_journal` (append-only, invariant #12); this table
-- holds what the next session resumes from: the paper broker's whole state after the session
-- (`book_state`, Decimal-exact strings), the policy's carried state, and the corporate actions the
-- session booked. The next run restores the broker from the latest COMPLETED row and rolls forward
-- one session — it never replays history — so a corporate action learnt late is booked when it
-- becomes known instead of rewriting the past. A SKIPPED_DATA_RED row may later be superseded by COMPLETED for the same date (the data
-- turned green and the job was rerun); a COMPLETED row is final — the writer only ever updates the
-- red row, and the CHECK below keeps a red row from claiming orders.
--
-- Conventions are 0001_init's: the runner wraps this file in one transaction, so no BEGIN/COMMIT
-- and no IF NOT EXISTS; it runs exactly once.

CREATE TABLE paper_session (
    book_id        text        NOT NULL CHECK (book_id ~ '^[a-z][a-z0-9_]{2,63}$'),
    trading_date   date        NOT NULL,
    outcome        text        NOT NULL CHECK (outcome IN ('COMPLETED', 'SKIPPED_DATA_RED')),
    reason         text        NOT NULL,
    rebalanced     boolean     NOT NULL,
    orders         jsonb       NOT NULL DEFAULT '[]'::jsonb,
    pending        jsonb,
    journal_digest text        NOT NULL,
    book_state     jsonb,
    book_digest    text,
    actions        jsonb       NOT NULL DEFAULT '[]'::jsonb,
    recorded_at    timestamptz NOT NULL,
    PRIMARY KEY (book_id, trading_date),
    CONSTRAINT paper_session_red_places_nothing CHECK (
        outcome = 'COMPLETED'
        OR (orders = '[]'::jsonb AND pending IS NULL AND NOT rebalanced AND book_digest IS NULL
            AND book_state IS NULL AND actions = '[]'::jsonb)
    ),
    CONSTRAINT paper_session_completed_has_book CHECK (
        outcome <> 'COMPLETED' OR (book_digest IS NOT NULL AND book_state IS NOT NULL)
    )
);

COMMENT ON TABLE paper_session IS
    'X1 · M13.1 paper trading. One row per paper book per trading date the daily paper session finished with. '
    'COMPLETED: decided, with the orders placed on SimBroker and the policy state carried to the '
    'next session. SKIPPED_DATA_RED: the interlock refused the day (invariant #10). The paper book '
    'is restored from the latest COMPLETED row each run; it is never routed to a real broker.';
COMMENT ON COLUMN paper_session.orders IS
    'The OrderRequests the session placed on the paper broker after A8 cleared them, in placement '
    'order. Decimals are strings — a JSON number read back out of jsonb is a float.';
COMMENT ON COLUMN paper_session.pending IS
    'The momentum policy''s redeploy-next-session target (ISIN -> weight, strings), or NULL.';
COMMENT ON COLUMN paper_session.book_state IS
    'The paper SimBroker after the session placed its orders (SimBrokerState: cash, lots, pending '
    'buys and sales with trade date and cycle, staged orders, id counters), Decimals as strings, '
    'plus the session''s cash-ledger lines. The next session restores from it.';
COMMENT ON COLUMN paper_session.book_digest IS
    'sha256 of book_state''s broker part, canonical bytes; a restore must reproduce it exactly.';
COMMENT ON COLUMN paper_session.actions IS
    'The corporate actions this session booked (identity keys), on time or late. An action is '
    'booked once per book: on the first decided session at which it is known.';
