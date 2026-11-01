-- 0022_lineage_edge_conflict — let a lineage rebuild queue a derived edge that contradicts a MANUAL one.
--
-- `isin_lineage` holds two kinds of edge: L1_CONTIGUITY rows, which `identity.lineage_rebuild`
-- re-derives and replaces wholesale, and MANUAL rows recorded by a repair from evidence the
-- derivation cannot see (the first is AvenuesAI's INE483S01020 -> INE483S01038 across a suspension,
-- 2026-10-29). A rebuild keeps every MANUAL row. When it derives an edge that contradicts one — the
-- same predecessor to another successor or on another date, another predecessor into the MANUAL
-- successor, or a loop — it must not pick a side silently. It stops before writing anything and
-- queues the conflict here, the same human queue the rest of D2's ambiguities land in.
--
-- So `identity_reconciliation` gains one kind and one detector:
--   kind        LINEAGE_EDGE     — `isins` holds every ISIN the two edges name, `symbols` their
--                                  symbols at the change, `on_date` the derived effective date,
--                                  `detail.note` both edges in full.
--   detected_by LINEAGE_REBUILD  — raised by `identity.lineage_rebuild`.
-- Resolving such a row records that the MANUAL edge stands against that derived edge, and the next
-- rebuild drops the derived one; deleting the MANUAL row instead lets the derived edge be written.
--
-- Conventions are 0001_init's: the runner wraps this file in one transaction, so no BEGIN/COMMIT
-- and no IF NOT EXISTS; it runs exactly once.

ALTER TABLE identity_reconciliation
    DROP CONSTRAINT identity_reconciliation_kind_check,
    ADD CONSTRAINT identity_reconciliation_kind_check
        CHECK (kind IN ('SYMBOL_TO_ISIN', 'ISIN_TO_SYMBOL', 'LINEAGE_EDGE')),
    DROP CONSTRAINT identity_reconciliation_detected_by_check,
    ADD CONSTRAINT identity_reconciliation_detected_by_check
        CHECK (detected_by IN ('INGEST', 'RESOLVE', 'LINEAGE_REBUILD'));
