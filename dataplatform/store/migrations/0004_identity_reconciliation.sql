-- 0004_identity_reconciliation — the queue an ambiguous identity lands in (task M1.7, D2).
--
-- Invariant #2 says nothing joins on a raw symbol: everything goes through the identity master,
-- which turns (exchange, symbol, date) into an ISIN. That mapping is only a function if a symbol
-- names at most one security on a date. When it does not — two ISINs claiming SUEL on the same
-- day, or one ISIN carrying two symbols — the master must not pick. Picking silently is how a
-- year of one company's prices ends up under another company's thesis, and nothing downstream
-- could ever detect it.
--
-- So the ambiguity is raised to the caller *and* written here, because those are two different
-- jobs: the exception stops the current decision, and this row is what a human eventually reads
-- to fix the underlying data. An exception alone is a log line nobody reads (CLAUDE.md); a row
-- alone is a silent pick.
--
-- Deliberately not `quality_flag`. That table (D7) holds sentinel findings about *values* —
-- price spikes, volume outliers, CA mismatches — which are judged against thresholds and are
-- routinely informational. An unresolvable identity is categorical, blocks the join it was asked
-- for, and belongs to D2. Sharing the table would mean the identity master's hard failures
-- compete for attention with an INFO-severity volume outlier.
--
-- Conventions are 0001_init's: text + CHECK for enumerations, timestamptz for instants, no
-- BEGIN/COMMIT and no IF NOT EXISTS — the runner wraps the file in one transaction and it runs
-- exactly once against a database.

-- ── D2 · identity reconciliation queue ──────────────────────────────────────────────────────

CREATE TABLE identity_reconciliation (
    id          bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind        text        NOT NULL CHECK (kind IN ('SYMBOL_TO_ISIN', 'ISIN_TO_SYMBOL')),
    exchange    text        NOT NULL CHECK (exchange IN ('NSE', 'BSE')),
    on_date     date        NOT NULL,
    symbols     text[]      NOT NULL CHECK (cardinality(symbols) >= 1),
    isins       text[]      NOT NULL CHECK (cardinality(isins) >= 1),
    detected_by text        NOT NULL CHECK (detected_by IN ('INGEST', 'RESOLVE')),
    source      text        NOT NULL,
    detail      jsonb       NOT NULL DEFAULT '{}'::jsonb,
    resolved    boolean     NOT NULL DEFAULT false,
    resolved_at timestamptz,
    resolution  text,
    raised_at   timestamptz NOT NULL,
    CONSTRAINT identity_reconciliation_is_ambiguous
        CHECK (cardinality(symbols) > 1 OR cardinality(isins) > 1),
    CONSTRAINT identity_reconciliation_resolution
        CHECK (resolved = (resolved_at IS NOT NULL)),
    CONSTRAINT identity_reconciliation_unique
        UNIQUE (kind, exchange, on_date, symbols, isins)
);
COMMENT ON TABLE identity_reconciliation IS
    'D2 · One row per identity that could not be resolved to a single answer: a symbol naming '
    'two ISINs on a date (SYMBOL_TO_ISIN) or an ISIN carrying two symbols on a date '
    '(ISIN_TO_SYMBOL). Written by dataplatform.identity, read by a human. Rows are appended by '
    'both the ingest and the resolve path and are deduplicated by the UNIQUE constraint, so a '
    'backfill that hits the same bad symbol on nine hundred dates does not write nine hundred '
    'rows for one underlying defect.';
COMMENT ON COLUMN identity_reconciliation.on_date IS
    'The trading date the two candidates collide on — the first date both claims are valid for '
    'a conflict found at ingest, and the date that was asked about for one found at resolve.';
COMMENT ON COLUMN identity_reconciliation.symbols IS
    'Sorted, so the UNIQUE constraint deduplicates regardless of the order the candidates were '
    'discovered in. Symbols, not a foreign key: the whole point is that they resolve to nothing '
    'usable.';
COMMENT ON COLUMN identity_reconciliation.isins IS
    'Sorted, same reason. Not a foreign key to security_master either — an ambiguity found while '
    'ingesting the master itself can name an ISIN that has not been written yet, and that row is '
    'exactly the one that must survive.';
COMMENT ON COLUMN identity_reconciliation.detected_by IS
    'INGEST — the master was being built and two source rows disagreed. RESOLVE — a caller asked '
    'for a mapping the stored master could not answer. The same defect can raise both.';
COMMENT ON COLUMN identity_reconciliation.resolution IS
    'What a human decided and why. Free text on purpose: the fix is usually a correction to a '
    'source file or a hand-entered symbol window, not a state this table can enumerate.';

CREATE INDEX identity_reconciliation_open ON identity_reconciliation (on_date DESC)
    WHERE NOT resolved;
CREATE INDEX identity_reconciliation_by_symbol ON identity_reconciliation USING gin (symbols);
