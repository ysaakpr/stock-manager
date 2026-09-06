-- 0008_sync_state_unit — give `sync_state.source` back its low cardinality (audit finding N1).
--
-- 0001_init declared `sync_state` as one row per (source, logical_date), where `source` is a D1
-- Source Register id. Four ingest paths since then needed a *second* key — a filing, an index
-- slug, an index chunk, a BSE scrip — and each solved it by qualifying the source string:
--
--     nse_xbrl_filing/IF87614                 (fundamentals_backfill.FilingUnit)
--     nse_financial_results_index/Quarterly/2020-12-31   (IndexChunk, old feed)
--     nse_integrated_filing_index/2025-03-31/p06         (IndexChunk, integrated feed)
--     nifty_index_constituents:niftybank      (indices.constituents_state_source)
--     bse_corp_actions/500325                 (corp_actions_backfill.BSE_STATE_PREFIX)
--
-- Each choice is right on its own terms and each is documented where it is made; together they
-- turned a column the rest of the platform reads as an enumeration into a high-cardinality key.
-- The table now holds 71,853 distinct "sources" against 29 registered ones, and the two readers
-- that treat `source` as an enumeration both broke on it: `/status/sources` answers 27 MB in 65 s,
-- and `/status/gaps` — the M1 gate's "explains 100% of missing days" — raises PathLayoutError,
-- because `GapScanner` defaults to the sources sync_state holds rows for and probes each as a lake
-- dataset directory.
--
-- The fix is to give the sub-key a column of its own. `source` goes back to being a register id;
-- `unit` carries the filing / slug / chunk / scrip, empty for the per-session sources that never
-- needed one. The primary key gains it, so nothing that resumes today changes behaviour.
--
-- The backfill below splits on the FIRST '/' or ':'. That is safe because a register id is a lake
-- identifier — lower-case letters, digits, '.', '_' and '-' — so neither delimiter can occur in
-- the base, and every qualified writer above puts its delimiter immediately after the base. The
-- CHECK added at the end makes that a property of the table rather than of the writers' good
-- behaviour: the next subsystem that needs a sub-key gets a constraint violation at its first
-- write instead of a 500 in the status API eight months later.
--
-- Conventions are 0001_init's: the runner wraps this file in one transaction, so no BEGIN/COMMIT
-- and no IF NOT EXISTS; it runs exactly once.

ALTER TABLE sync_state ADD COLUMN unit text NOT NULL DEFAULT '';

COMMENT ON COLUMN sync_state.unit IS
    'The sub-key within a source, empty for the per-session sources that have none: a filing id '
    '(nse_xbrl_filing), an index slug (nifty_index_constituents), an index chunk label, a BSE '
    'scrip code. `source` stays a D1 Source Register id so the status API and the D7 gap report '
    'can keep reading that column as an enumeration; anything finer belongs here.';

-- The primary key comes off first: the UPDATE below collapses many qualified sources onto one
-- base, so two filings that share a logical_date would collide against the old (source, date) key
-- while the rows are mid-flight. Dropping it first is not a widening of what the table permits —
-- the new key goes on a few statements later, inside this same transaction.
ALTER TABLE sync_state DROP CONSTRAINT sync_state_pkey;

-- Split every qualified source written before this migration. `least` ignores NULLs, so with the
-- zeros `strpos` returns for "not found" folded to NULL it yields the FIRST delimiter's position,
-- or NULL when the source carries neither and is already a bare register id. DISTINCT keeps the
-- join over 71,853 names rather than 76,803 rows.
UPDATE sync_state s
SET source = left(s.source, split.at - 1),
    unit   = substr(s.source, split.at + 1)
FROM (
    SELECT DISTINCT
           source AS raw,
           least(nullif(strpos(source, '/'), 0), nullif(strpos(source, ':'), 0)) AS at
    FROM sync_state
) AS split
WHERE s.source = split.raw
  AND split.at IS NOT NULL;

ALTER TABLE sync_state ADD PRIMARY KEY (source, logical_date, unit);

-- The same predicate `dataplatform.store.paths` applies to a lake dataset name, enforced at write
-- time. `PathLayoutError` at read time was the symptom; this is the cause.
ALTER TABLE sync_state ADD CONSTRAINT sync_state_source_is_a_lake_identifier
    CHECK (source ~ '^[a-z0-9][a-z0-9._-]*$');

DROP INDEX sync_state_unfinished;
CREATE INDEX sync_state_unfinished ON sync_state (source, logical_date, unit)
    WHERE state <> 'PUBLISHED';

-- `/status/sources` groups by source and the gap report scans DISTINCT source; both were sequential
-- scans over 76,803 rows once the filing checkpoints landed.
CREATE INDEX sync_state_by_source ON sync_state (source);
