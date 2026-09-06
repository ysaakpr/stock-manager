-- 0010_ca_filed_against_isin — let an action filed against a retired ISIN land on the survivor.
--
-- 0009 recorded the reissue edge. This is what consumes it on the corporate-action side.
--
-- The exchange files a split against the ISIN it is *retiring*: 290 of the 445 reissues derived
-- from L1 carry a SPLIT or BONUS on the boundary, and every one of them names the predecessor.
-- `corporate_actions.isin` REFERENCES `security_master (isin)`, the retired ISIN was never written
-- there, and so the row is refused at ingest — which is why only 17 SPLITs exist in a ten-year
-- store that saw 362 of them, and why `adjustment_factors` covers 252 ISINs out of ~3,900.
--
-- The fix is not to relax the foreign key. `isin` is the join key the factor chain, the L2
-- materializer and the query layer all read, and it must keep pointing at a security that exists
-- — the surviving one, whose prices the factor is going to rescale. What was missing is a place
-- to keep the *other* fact: which ISIN the exchange actually named. That is this column.
--
-- So after this migration a split across a reissue is stored as one row whose `isin` is the
-- survivor and whose `filed_against_isin` is the retired predecessor. Nothing downstream changes
-- shape; the factor chain keeps reading `isin` and now finds the action it was always missing.
--
-- No foreign key here, for 0009's reason: the retired ISIN is absent from `security_master` by
-- construction, and a FK would refuse exactly the rows this column exists to record.
--
-- Conventions are 0001_init's: the runner wraps this file in one transaction, so no BEGIN/COMMIT
-- and no IF NOT EXISTS; it runs exactly once.

ALTER TABLE corporate_actions
    ADD COLUMN filed_against_isin isin;

COMMENT ON COLUMN corporate_actions.filed_against_isin IS
    'D3 · The ISIN the exchange named, when that is not `isin`. NULL — the ordinary case — means '
    'the action was filed against the security it applies to. A value means the action was filed '
    'against an ISIN that has since been retired, and `isin` holds the survivor it was resolved '
    'to through `isin_lineage`. Deliberately not a foreign key: the retired ISIN is absent from '
    'security_master, which is the whole reason the row could not land before. Keep it for '
    'provenance — a re-parse from L0 must be able to see the ISIN the payload actually carried.';

-- `corporate_actions_unique` is (isin, ex_date, action_type, source). Two different predecessors
-- resolving onto one survivor with the same ex-date and type would collide on it — that would be
-- a lineage contradiction (one company cannot split twice on one day under two retired ISINs),
-- so leaving the constraint to catch it is deliberate rather than an oversight.

-- The re-ingest that follows this migration resolves through lineage, so this index answers
-- "which actions did we have to resolve, and from where" without a sequential scan.
CREATE INDEX corporate_actions_filed_against
    ON corporate_actions (filed_against_isin)
    WHERE filed_against_isin IS NOT NULL;
