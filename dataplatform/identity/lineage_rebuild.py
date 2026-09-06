"""Rebuild the ISIN lineage and everything downstream of it, offline from L0 and L1.

The four stages the lineage touches, in the only order they can run:

1. **Derive** the reissue edges from L1 contiguity and write `isin_lineage` (0009).
2. **Replay** every stored `nse_corp_actions` L0 payload back through the parser *with* that
   lineage, so an action filed against a retired ISIN reaches the surviving security (0010)
   instead of being recorded unresolved.
3. **Reconcile and recompute**, the same finalize the CA backfill runs, turning the newly landed
   actions into `adjustment_factors` rows and `l2_invalidation` flags.
4. **Rebuild L2** for the invalidated ISINs, each over its whole lineage chain, so the adjusted
   series spans the reissue instead of starting at it.

**What it does not do:** fetch. Every stage reads L0 or L1 off local disk, so this runs on a
laptop between campaigns and on the server without touching the request budget. `L0Store.iter_refs`
yields the payloads in a stable order, so two runs over the same lake do the same work.

**What it assumes:** L0 holds the corporate-action payloads and L1 the prices — a rebuild cannot
invent either. An ISIN whose successor the identity master does not know is skipped with a count,
not raised on: one unseen name must not cost the other four hundred edges.

    uv run python -m dataplatform.identity.lineage_rebuild            # the whole thing
    uv run python -m dataplatform.identity.lineage_rebuild --derive-only   # stage 1, to inspect
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass

from dataplatform.clock import Clock, SystemClock
from dataplatform.config import get_settings
from dataplatform.corpactions.reconcile import SingleSourcePolicy
from dataplatform.identity.lineage import (
    LineageStore,
    derive_edges,
    read_corroboration,
    read_equity_spans,
)
from dataplatform.identity.master import IdentityStore
from dataplatform.ingest.corp_actions import write_corporate_actions
from dataplatform.ingest.corp_actions_backfill import finalize_reconcile_and_recompute
from dataplatform.ingest.nse import corp_actions as nse_ca
from dataplatform.logging import get_logger
from dataplatform.store.db import connect
from dataplatform.store.l0 import L0Store
from dataplatform.store.l2 import open_connection, rebuild_invalidated

__all__ = ["LineageRebuildReport", "rebuild"]

_LOG = get_logger(__name__)

_CA_SOURCE = nse_ca.SOURCE_ID


@dataclass(frozen=True, slots=True)
class LineageRebuildReport:
    """What each stage of one rebuild did."""

    edges_derived: int
    edges_written: int
    payloads_replayed: int
    actions_resolved_through_lineage: int
    actions_inserted: int
    isins_recomputed: int
    l2_partitions_rebuilt: int
    l2_partitions_stitched: int


def rebuild(*, clock: Clock | None = None, derive_only: bool = False) -> LineageRebuildReport:
    """Run the four stages against the configured store and lake; return what each did."""
    clock = SystemClock() if clock is None else clock
    settings = get_settings()
    data_root = settings.data_root

    # ── 1. derive ────────────────────────────────────────────────────────────────────────────
    spans, sessions = read_equity_spans(data_root=data_root)
    edges = derive_edges(spans, sessions, read_corroboration(data_root=data_root))

    with connect() as conn:
        store = LineageStore(conn, clock=clock)
        written = store.replace_derived(edges)
        conn.commit()
        resolver = store.load()

        if derive_only:
            return LineageRebuildReport(len(edges), written, 0, 0, 0, 0, 0, 0)

        # ── 2. replay L0 through the parser, now with the lineage ────────────────────────────
        # The source's rows go first. `write_corporate_actions` is ON CONFLICT DO NOTHING, so a
        # replay over rows that already exist keeps the *old* parse — and a rebuild whose whole
        # point may be a corrected parser would then silently change nothing. (It bit exactly
        # that way here: a split left UnquantifiedTerms by a regex gap survived the re-parse that
        # fixed it and went on failing the factor chain.) Scoped to this source, and safe because
        # `adjustment_factors.corporate_action_id` is NO ACTION and stage 3 rebuilds the factors
        # anyway; a delete that would orphan a factor row raises rather than cascading.
        deleted = conn.execute(
            "DELETE FROM corporate_actions WHERE source = %s", (_CA_SOURCE,)
        ).rowcount
        _LOG.info("lineage_rebuild.cleared", source=_CA_SOURCE, rows=deleted)

        master = IdentityStore(conn, clock=clock).load_master()
        l0 = L0Store(clock=clock, data_root=data_root)
        replayed = resolved = inserted = 0
        for ref in l0.iter_refs(_CA_SOURCE):
            result = nse_ca.parse_l0(l0, ref, master=master, clock=clock, lineage=resolver)
            replayed += 1
            resolved += sum(1 for a in result.actions if a.filed_against_isin is not None)
            inserted += write_corporate_actions(conn, result.actions, clock=clock).inserted
        conn.commit()
        _LOG.info(
            "lineage_rebuild.replayed",
            payloads=replayed,
            through_lineage=resolved,
            inserted=inserted,
        )

        # ── 3. reconcile + recompute ─────────────────────────────────────────────────────────
        finalize = finalize_reconcile_and_recompute(
            conn,
            clock=clock,
            commit=conn.commit,
            single_source_policy=SingleSourcePolicy.ACCEPT,
        )
        conn.commit()

        # ── 4. rebuild L2, each ISIN over its whole chain ────────────────────────────────────
        history = {
            isin: chain
            for isin in {e.successor_isin for e in edges}
            if len(chain := resolver.chain_to(isin)) > 1
        }
        con = open_connection()
        try:
            reports = rebuild_invalidated(
                conn, clock=clock, con=con, data_root=data_root, history_for=history
            )
        finally:
            con.close()
        conn.commit()

    stitched = sum(1 for r in reports if r.isin in history)
    report = LineageRebuildReport(
        edges_derived=len(edges),
        edges_written=written,
        payloads_replayed=replayed,
        actions_resolved_through_lineage=resolved,
        actions_inserted=inserted,
        isins_recomputed=finalize.isins_recomputed,
        l2_partitions_rebuilt=len(reports),
        l2_partitions_stitched=stitched,
    )
    _LOG.info("lineage_rebuild.done", **asdict(report))
    return report


def main() -> int:
    """CLI entry point; prints the per-stage counts."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--derive-only",
        action="store_true",
        help="derive and write the lineage edges, then stop (no replay, recompute or L2)",
    )
    args = parser.parse_args()
    report = rebuild(derive_only=args.derive_only)
    for field, value in asdict(report).items():
        print(f"{field:<36} {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
