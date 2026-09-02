-- 0005_ca_taxonomy_and_lineage — align corporate_actions with the M2.1 taxonomy and record
-- the raw feed text plus its L0 lineage (task M2.2, D3).
--
-- Two things 0001_init could not know, because the code that writes these rows did not exist yet:
--
--  1. `action_type`'s CHECK predates the M2.1 normalized taxonomy. It enumerated `CONSOLIDATION`
--     and `OTHER` — neither of which the taxonomy has (a consolidation is a SPLIT whose face
--     value rose; there is no catch-all bucket, on purpose) — and lacked `SCHEME_OF_ARRANGEMENT`,
--     `DVR_CONVERSION`, `NAME_CHANGE`, `BUYBACK` and `DELISTING`. Five of the twelve types an
--     M2.2 parser produces could therefore not be stored. This is the first task that writes the
--     table, so it owns the widening (ops/BACKLOG.md, spotted by M2.1). The set below is exactly
--     `dataplatform.corpactions.taxonomy.ActionType`; those two must not drift apart.
--
--  2. Ingestion needs to keep the exchange's own words and the payload they came from. `raw_text`
--     is the purpose string verbatim — the reconciliation queue (M2.3) and any later re-parse
--     against an improved normalizer both read the string that was actually published, not a
--     cleaned copy. `l0_key` names the checksummed L0 payload the row was derived from, so every
--     L1 corporate-action row can be traced back to immutable bytes (invariant #1); `source_ref`
--     already existed but is a free-text reference, so the lineage gets its own typed column.
--
-- Conventions are 0001_init's: text + CHECK for the enumeration, no BEGIN/COMMIT and no
-- IF NOT EXISTS — the runner wraps this file in one transaction and it runs exactly once.

-- ── widen the action-type enumeration to the M2.1 taxonomy ──────────────────────────────────

ALTER TABLE corporate_actions
    DROP CONSTRAINT corporate_actions_action_type_check;

ALTER TABLE corporate_actions
    ADD CONSTRAINT corporate_actions_action_type_check CHECK (action_type IN (
        'SPLIT', 'BONUS', 'DIVIDEND', 'RIGHTS', 'MERGER', 'DEMERGER',
        'SCHEME_OF_ARRANGEMENT', 'DVR_CONVERSION', 'NAME_CHANGE', 'FACE_VALUE_CHANGE',
        'BUYBACK', 'DELISTING'));

-- ── the exchange's own words, and the L0 payload they came out of ───────────────────────────

ALTER TABLE corporate_actions
    ADD COLUMN raw_text text,
    ADD COLUMN l0_key   text;

COMMENT ON COLUMN corporate_actions.raw_text IS
    'D3 · The exchange''s purpose string exactly as published — not stripped, case-folded or '
    'whitespace-collapsed. NSE writes "FV SPLIT FROM RS.10/- TO RS.2/-", BSE "Stock  Split From '
    'Rs.10/- to Rs.2/-"; reconciliation (M2.3) and any re-parse compare the string that was '
    'actually served, so a cleaned copy would be the wrong evidence.';
COMMENT ON COLUMN corporate_actions.l0_key IS
    'D3 · `source/date/filename` of the checksummed L0 payload this row was derived from '
    '(invariant #1). Nullable only for rows entered by hand rather than parsed from a feed.';
