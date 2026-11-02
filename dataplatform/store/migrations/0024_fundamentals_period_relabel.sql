-- 0024_fundamentals_period_relabel — the provenance of an XBRL filing stored under another period.
--
-- Owner ruling (2026-11-02, class A of the parse-failure triage): a Q4 results document whose
-- quarter column states October→March under a `Half Yearly` header, but whose revenue and profit
-- both equal the same document's year-to-date column minus the quarters the company had already
-- filed, is stored as the January→March quarter — with the document's own stated values, never a
-- computed one. That period is *inferred*, not stated, so every such filing carries a row here.
--
-- The L1 `pit_fundamentals` parquet has no notes column, and adding one changes the schema every
-- reader globs across partitions; the marker therefore lives here, keyed exactly like the filing's
-- `sync_state` row and naming the L1 rows it describes (isin, filing_id, nature, stored period).
--
--   source, unit, logical_date   the nse_xbrl_filing sync_state key of the filing (IF<seq>, date)
--   filing_id, isin, nature      the L1 rows' identity
--   stated_start, stated_end     the period the document's column states (Oct→Mar)
--   stored_start, stored_end     the period it is stored under (Jan→Mar)
--   rule                         the owner-approved rule (Q4_RELABEL_FY_MINUS_9M_CHECK)
--   check_result                 every number the check compared: stated, cumulative, the prior
--                                quarters used (filing id and date), difference and tolerance
--   l0_key                       the L0 payload the facts were read from
--
-- Written in the same transaction as the filing's PUBLISHED checkpoint; a re-derivation of the
-- same filing rewrites the same row. Conventions are 0001_init's: the runner wraps this file in one
-- transaction, so no BEGIN/COMMIT and no IF NOT EXISTS; it runs exactly once.

CREATE TABLE fundamentals_period_relabel (
    source        text        NOT NULL,
    unit          text        NOT NULL,
    logical_date  date        NOT NULL,
    filing_id     text        NOT NULL,
    isin          isin        NOT NULL,
    nature        text        NOT NULL CHECK (nature IN ('Standalone', 'Consolidated')),
    stated_start  date        NOT NULL,
    stated_end    date        NOT NULL,
    stored_start  date        NOT NULL,
    stored_end    date        NOT NULL,
    rule          text        NOT NULL,
    check_result  jsonb       NOT NULL,
    l0_key        text,
    recorded_at   timestamptz NOT NULL,
    PRIMARY KEY (source, unit, logical_date),
    CHECK (stored_start > stated_start AND stored_end = stated_end)
);

CREATE INDEX fundamentals_period_relabel_by_isin ON fundamentals_period_relabel (isin, stored_end);

COMMENT ON TABLE fundamentals_period_relabel IS
    'D1 (M14.3, owner 2026-11-02): one row per XBRL filing stored under a period its document did not '
    'state — the class-A Q4 relabel. Its rule and check result are the provenance of those L1 rows.';
