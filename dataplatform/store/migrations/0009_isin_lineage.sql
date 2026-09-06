-- 0009_isin_lineage — the predecessor/successor edge an NSE face-value split leaves behind.
--
-- The gap this closes. A face-value split on NSE usually *retires* the ISIN and issues a new one:
-- the 4-character issuer code survives, the 2-digit issue serial increments, the check digit
-- changes. GRASIM is INE047A0101(3) to 2016-10-06 and INE047A0102(1) from 2016-10-07; IRCTC is
-- INE335Y0101(2) to 2021-10-28 and INE335Y0102(0) from 2021-10-29. Measured over L1 (2016-09..
-- 2026-09) there are 445 such sequential transitions across 4,201 INE equity ISINs, 386 of them
-- with no trading gap at all.
--
-- The corporate action is filed against the ISIN that is being retired. 176 of those 445
-- transitions carry a SPLIT or BONUS filed on the effective date against the *predecessor* ISIN;
-- none are filed against the survivor. `corporate_actions.isin` REFERENCES `security_master`, the retired
-- ISIN was never written there, so the row cannot land — 338 of 355 SPLIT rows are refused this
-- way. No factor is computed, no L2 partition is materialized, and the momentum signal reads a
-- 2:1 split as a genuine ~-50% twelve-month return and drops the name. That is the whole failure
-- chain, and it starts here: the system has no way to say "these two ISINs are one company".
--
-- Why this table has no foreign key on `predecessor_isin`. Every other ISIN column in this schema
-- REFERENCES `security_master`. This one must not: the predecessor is retired and absent from the
-- master by construction — that absence is the very thing being recorded. A FK here would refuse
-- exactly the rows the table exists to hold. `successor_isin` does carry the FK, because the
-- survivor is a live security and a row pointing at an unknown survivor is a derivation bug.
--
-- What this table is NOT. It is not a merger/demerger map. One predecessor resolves to exactly one
-- successor and the chain is linear, so a demerger (one company becoming two) is out of scope and
-- must not be forced through this edge. The CHECK on (predecessor <> successor) and the unique
-- index on `predecessor_isin` keep it linear.
--
-- Conventions are 0001_init's: the runner wraps this file in one transaction, so no BEGIN/COMMIT
-- and no IF NOT EXISTS; it runs exactly once.

CREATE TABLE isin_lineage (
    predecessor_isin isin        NOT NULL,
    successor_isin   isin        NOT NULL REFERENCES security_master (isin),
    effective_date   date        NOT NULL,
    detected_by      text        NOT NULL CHECK (detected_by IN ('L1_CONTIGUITY', 'MANUAL')),
    confidence       text        NOT NULL CHECK (confidence IN ('CORROBORATED', 'DERIVED')),
    gap_sessions     integer     NOT NULL CHECK (gap_sessions >= 0),
    symbol_at_change text        NOT NULL,
    corroborating_action text    CHECK (corroborating_action IN ('SPLIT', 'BONUS')),
    computed_at      timestamptz NOT NULL,
    PRIMARY KEY (predecessor_isin, successor_isin),
    CONSTRAINT isin_lineage_not_self CHECK (predecessor_isin <> successor_isin),
    -- CORROBORATED means "a split/bonus sits on `effective_date`", so it must name which.
    CONSTRAINT isin_lineage_corroboration_is_evidenced CHECK (
        (confidence = 'CORROBORATED') = (corroborating_action IS NOT NULL))
);

COMMENT ON TABLE isin_lineage IS
    'D2 · One directed edge per ISIN reissue: the retired ISIN, the ISIN that replaced it, and the '
    'successor''s first trading session. Derived from L1 price contiguity (same INE issuer prefix, '
    'security type 01, one span ending as the next begins), corroborated where a split or bonus '
    'sits on the effective date. Read it through `dataplatform.identity.lineage`, never directly: '
    'the factor chain and the L2 stitch both need the transitive chain, not one edge.';

COMMENT ON COLUMN isin_lineage.predecessor_isin IS
    'The retired ISIN. Deliberately NOT a foreign key — it is absent from security_master by '
    'construction, and that absence is what this table exists to record.';
COMMENT ON COLUMN isin_lineage.corroborating_action IS
    'Which action explains the reissue, read from the L0 corporate-action payloads. Deliberately '
    'a kind and not a foreign key into `corporate_actions`: the corroborating rows are exactly '
    'the ones that never landed there, because they are filed against the retired ISIN and the '
    'FK to security_master refused them. Requiring an id here would make CORROBORATED '
    'unsatisfiable for every row this table exists to hold.';
COMMENT ON COLUMN isin_lineage.confidence IS
    'CORROBORATED — a SPLIT or BONUS falls on effective_date, so the reissue has an explanation. '
    'DERIVED — the contiguity holds but no action explains it (a face-value change or a rename '
    'whose action is outside the CA window). Consumers decide which they trust; the derivation '
    'records both rather than silently dropping the weaker half.';
COMMENT ON COLUMN isin_lineage.gap_sessions IS
    'Trading sessions between the predecessor''s last bar and the successor''s first. 0 is a '
    'seamless handover (386 of 445). A large gap is a suspension — or an issuer-code reuse that '
    'is not a lineage at all, which is why the number is kept rather than thresholded away here.';

-- The two access paths: forward from a retired ISIN (the factor chain resolving a CA), and
-- backward from a survivor (the L2 stitch collecting the history to splice). `predecessor_isin`
-- is unique on its own — one ISIN is retired exactly once — which is what keeps the chain linear.
CREATE UNIQUE INDEX isin_lineage_predecessor ON isin_lineage (predecessor_isin);
CREATE INDEX isin_lineage_successor ON isin_lineage (successor_isin);
