-- 0006_l2_invalidation — the queue a retroactive factor recompute writes to (task M2.4, D3/D4).
--
-- §4.3 rule 2: "a new corporate action triggers retroactive recompute of the full factor chain
-- for that ISIN + invalidation of L2." The recompute itself (dataplatform.corpactions.recompute)
-- rewrites `adjustment_factors` for the ISIN in place, but the derived L2 series built from those
-- factors (M2.5) lives in Parquet the recompute does not own and cannot rebuild synchronously. So
-- the recompute records here that an ISIN's L2 is stale, and the L2 materializer (M2.5) drains the
-- queue, rebuilds that ISIN's adjusted partitions from L1 + the fresh factors, and marks the row
-- resolved.
--
-- Whole-ISIN, not per-date, on purpose. Back-adjustment expresses history in the current share
-- basis (see dataplatform.corpactions.factors), so a newly landed action re-scales the ISIN's
-- entire history up to its ex-date — the "full adjusted history" the task's acceptance names. The
-- grain that matters for §4.3 and for M2.5's "incremental per ISIN, not full-market" requirement
-- is the ISIN; which of its date partitions to rewrite is the materializer's decision, not this
-- queue's. `from_date` is kept as an optional hint (the earliest ex-date that moved) for a
-- materializer that wants to narrow the rewrite; NULL means the ISIN's whole series.
--
-- Conventions are 0001_init's: the runner wraps this file in one transaction, so no BEGIN/COMMIT
-- and no IF NOT EXISTS; it runs exactly once.

CREATE TABLE l2_invalidation (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    isin         isin        NOT NULL REFERENCES security_master (isin),
    reason       text        NOT NULL,
    from_date    date,
    requested_at timestamptz NOT NULL,
    resolved     boolean     NOT NULL DEFAULT false,
    resolved_at  timestamptz
);
COMMENT ON TABLE l2_invalidation IS
    'D4 · The D3/D4 stale-L2 queue (§4.3 rule 2). One open row per ISIN whose adjustment_factors were '
    'recomputed since its L2 was last built; M2.5''s materializer drains it, rebuilds that ISIN''s '
    'adjusted partitions, and sets resolved. Bad/stale L2 must never silently become decisions '
    '(invariant #10), so an unresolved row is a visible instruction to rebuild, not a log line.';
COMMENT ON COLUMN l2_invalidation.reason IS
    'Why the ISIN was invalidated — e.g. the corporate action or the recompute trigger that raised '
    'it. Free text for the operator and the materializer log, never parsed for control flow.';
COMMENT ON COLUMN l2_invalidation.from_date IS
    'Optional hint: the earliest ex-date whose factor changed, so a materializer may narrow the '
    'rewrite. NULL means rebuild the ISIN''s whole adjusted history — the safe default, since '
    'back-adjustment re-scales all history before a newly landed action.';
CREATE INDEX l2_invalidation_open ON l2_invalidation (isin)
    WHERE NOT resolved;
