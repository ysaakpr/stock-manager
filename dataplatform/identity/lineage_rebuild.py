"""Rebuild the ISIN lineage and everything downstream of it, offline from L0 and L1.

The four stages the lineage touches, in the only order they can run:

1. **Derive** the reissue edges from L1 contiguity and write `isin_lineage` (0009): equity
   reissues by issuer code (`lineage.derive_edges`), and fund unit splits by NSE symbol with the
   split corroborated in L0 (`fund_lineage.derive_fund_edges`).
2. **Replay** every stored `nse_corp_actions` L0 payload back through the parser *with* that
   lineage, so an action filed against a retired ISIN reaches the surviving security (0010)
   instead of being recorded unresolved.
3. **Reconcile and recompute**, the same finalize the CA backfill runs, turning the newly landed
   actions into `adjustment_factors` rows and `l2_invalidation` flags.
4. **Rebuild L2** for the invalidated ISINs, each over its whole lineage chain, so the adjusted
   series spans the reissue instead of starting at it. This is the one drain that runs with
   `floor_existing=False`: a survivor's partition built before its edge existed starts at the
   reissue, and keeping that start as a floor would drop the very history the edge stitches in.
5. **Fill L2** for every ISIN L1 has EQ bars for and no stage ever built — the names with no
   corporate action at all, which the invalidation queue never reaches (`materialize_missing`).

**What it does not do:** fetch. Every stage reads L0 or L1 off local disk, so this runs on a
laptop between campaigns and on the server without touching the request budget. `L0Store.iter_refs`
yields the payloads in a stable order, so two runs over the same lake do the same work.

**What it assumes:** L0 holds the corporate-action payloads and L1 the prices — a rebuild cannot
invent either. A successor the identity master does not know is registered as a DELISTED master row
when it is a chain's retired middle with NSE EQ bars in L1 (`registered_intermediates`); any other
unknown successor is skipped and counted per reason, not raised on: one unseen name must not cost
the other five hundred edges.

    uv run python -m dataplatform.identity.lineage_rebuild            # the whole thing
    uv run python -m dataplatform.identity.lineage_rebuild --derive-only   # stage 1, to inspect
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass

from dataplatform.clock import Clock, SystemClock
from dataplatform.config import get_settings
from dataplatform.corpactions.reconcile import SingleSourcePolicy
from dataplatform.identity.fund_lineage import derive_fund_edges
from dataplatform.identity.lineage import (
    LineageStore,
    derive_edges,
    read_corroboration,
    read_eq_presence,
    read_equity_spans,
)
from dataplatform.identity.master import IdentityStore
from dataplatform.ingest.corp_actions import write_corporate_actions
from dataplatform.ingest.corp_actions_backfill import finalize_reconcile_and_recompute
from dataplatform.ingest.nse import corp_actions as nse_ca
from dataplatform.logging import get_logger
from dataplatform.store.db import connect
from dataplatform.store.l0 import L0Store
from dataplatform.store.l2 import (
    materialize_missing,
    open_connection,
    prune_retired,
    rebuild_invalidated,
)

__all__ = ["LineageRebuildReport", "rebuild"]

_LOG = get_logger(__name__)

_CA_SOURCE = nse_ca.SOURCE_ID


@dataclass(frozen=True, slots=True)
class LineageRebuildReport:
    """What each stage of one rebuild did."""

    edges_derived: int
    fund_edges_derived: int
    edges_written: int
    registered_intermediates: int
    edges_still_skipped: int
    payloads_replayed: int
    actions_resolved_through_lineage: int
    actions_inserted: int
    isins_recomputed: int
    l2_partitions_rebuilt: int
    l2_partitions_stitched: int
    l2_partitions_filled: int
    #: `(predecessor, successor, reason)` for every edge not written — printed after the counts.
    skipped_edges: tuple[tuple[str, str, str], ...] = ()

    def counts(self) -> dict[str, int]:
        """Every per-stage count, in field order — what the CLI prints as its table."""
        return {k: v for k, v in asdict(self).items() if isinstance(v, int)}


def rebuild(*, clock: Clock | None = None, derive_only: bool = False) -> LineageRebuildReport:
    """Run the four stages against the configured store and lake; return what each did."""
    clock = SystemClock() if clock is None else clock
    settings = get_settings()
    data_root = settings.data_root

    # ── 1. derive ────────────────────────────────────────────────────────────────────────────
    spans, sessions = read_equity_spans(data_root=data_root)
    equity_edges = derive_edges(spans, sessions, read_corroboration(data_root=data_root))
    # A fund edge's predecessor is an INF ISIN, which `derive_edges` never emits, so the two sets
    # cannot claim the same predecessor and the table's one-successor-per-predecessor index holds.
    fund_edges = derive_fund_edges(clock=clock, data_root=data_root)
    edges = (*equity_edges, *fund_edges)
    presence = read_eq_presence({e.successor_isin for e in edges}, data_root=data_root)

    with connect() as conn:
        store = LineageStore(conn, clock=clock)
        lineage = store.replace_derived(edges, presence=presence)
        conn.commit()
        resolver = store.load()
        skipped_edges = tuple(
            (e.predecessor_isin, e.successor_isin, reason.value) for e, reason in lineage.skipped
        )

        if derive_only:
            report = LineageRebuildReport(
                edges_derived=lineage.derived,
                fund_edges_derived=len(fund_edges),
                edges_written=lineage.written,
                registered_intermediates=len(lineage.registered_intermediates),
                edges_still_skipped=lineage.still_skipped,
                payloads_replayed=0,
                actions_resolved_through_lineage=0,
                actions_inserted=0,
                isins_recomputed=0,
                l2_partitions_rebuilt=0,
                l2_partitions_stitched=0,
                l2_partitions_filled=0,
                skipped_edges=skipped_edges,
            )
            _LOG.info("lineage_rebuild.done", **report.counts())
            return report

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
        # Unfloored, unlike the scheduled drains: a newly derived edge must reach back into the
        # predecessor's history, which lies before the survivor's current partition start.
        history = {
            isin: chain
            for isin in {e.successor_isin for e in edges}
            if len(chain := resolver.chain_to(isin)) > 1
        }
        con = open_connection()
        try:
            reports = rebuild_invalidated(
                conn,
                clock=clock,
                con=con,
                data_root=data_root,
                history_for=history,
                survivor_of=resolver.survivor_of,
                floor_existing=False,
            )
            # ── 5. fill L2 for the names no invalidation ever reached ────────────────────────
            # The queue rebuilds what a corporate action touched; a name with no action never
            # gets a partition that way. Retired ISINs are skipped — their bars are in the
            # survivor's stitched partition above — so the fill and the stitch never overlap.
            fill = materialize_missing(
                conn,
                con=con,
                data_root=data_root,
                history_for=history,
                survivor_of=resolver.survivor_of,
            )
        finally:
            con.close()
        conn.commit()
    # A partition a now-retired ISIN was built with before the lineage named it is a second,
    # unadjusted copy of the survivor's history; the derived edges above are what retire it.
    prune_retired(resolver.survivor_of, data_root=data_root)

    stitched = sum(1 for r in reports if r.isin in history)
    report = LineageRebuildReport(
        edges_derived=lineage.derived,
        fund_edges_derived=len(fund_edges),
        edges_written=lineage.written,
        registered_intermediates=len(lineage.registered_intermediates),
        edges_still_skipped=lineage.still_skipped,
        payloads_replayed=replayed,
        actions_resolved_through_lineage=resolved,
        actions_inserted=inserted,
        isins_recomputed=finalize.isins_recomputed,
        l2_partitions_rebuilt=len(reports),
        l2_partitions_stitched=stitched,
        l2_partitions_filled=len(fill.written),
        skipped_edges=skipped_edges,
    )
    _LOG.info("lineage_rebuild.done", **report.counts())
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
    for field, value in report.counts().items():
        print(f"{field:<36} {value}")
    by_reason: dict[str, int] = {}
    for _, _, reason in report.skipped_edges:
        by_reason[reason] = by_reason.get(reason, 0) + 1
    for reason, count in sorted(by_reason.items()):
        print(f"  skipped: {reason:<32} {count}")
    for predecessor, successor, reason in report.skipped_edges:
        print(f"  skipped edge {predecessor} -> {successor}  {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
