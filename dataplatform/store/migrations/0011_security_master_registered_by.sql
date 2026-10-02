-- 0011_security_master_registered_by — record which master rows the lineage rebuild registered.
--
-- The gap this closes. `security_master` was built from the current NSE/BSE snapshots, so an ISIN
-- that was issued by one reissue and retired by the next is in neither: BAJFINANCE traded as
-- INE296A01016 to 2016-09-08, INE296A01024 from 2016-09-09 to 2025-06-13, and INE296A01032 after.
-- `isin_lineage.successor_isin` REFERENCES the master, so the 2016 edge (successor INE296A01024)
-- was dropped, the survivor's L2 started in 2016 instead of 2011 and INE296A01016 no longer
-- resolved to the survivor. Measured on 2026-09-29: 75 of 609 derived edges dropped this way.
--
-- The lineage rebuild now registers such an intermediate ISIN as a DELISTED master row, derived
-- from the NSE bhavcopy rows L1 holds for it (first/last seen are its first/last EQ session, the
-- name is its L1 symbol — L1 carries no company name). This column says so. NULL — every row
-- that existed before this migration, and every row the identity ingest writes — means the row
-- came from an exchange snapshot; 'lineage_backfill' means the rebuild inferred it from L1, and
-- a reader who needs the exchange's own word about a security can tell the two apart.
--
-- Conventions are 0001_init's: the runner wraps this file in one transaction, so no BEGIN/COMMIT
-- and no IF NOT EXISTS; it runs exactly once.

ALTER TABLE security_master
    ADD COLUMN registered_by text CHECK (registered_by IS NULL OR registered_by = 'lineage_backfill');

COMMENT ON COLUMN security_master.registered_by IS
    'D2 · Who inferred this row when no exchange snapshot listed it. NULL — the ordinary case — '
    'means an identity snapshot ingest wrote it. ''lineage_backfill'' means the ISIN-lineage '
    'rebuild registered it as a DELISTED intermediate of a reissue chain, from its own L1 NSE EQ '
    'rows: first/last_seen_date are its first/last session there and name is its L1 symbol.';
