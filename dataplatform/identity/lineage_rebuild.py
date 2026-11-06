"""Rebuild the ISIN lineage and everything downstream of it, offline from L0 and L1.

The stages the lineage touches, in the only order they can run:

1. **Derive** the reissue edges from L1 contiguity and write `isin_lineage` (0009): equity
   reissues by issuer code (`lineage.derive_edges`), and fund unit splits by NSE symbol with the
   split corroborated in L0 (`fund_lineage.derive_fund_edges`). MANUAL rows are kept and applied
   alongside the derived ones (see *MANUAL edges* below).
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
6. **Re-key the filing feeds** (DQ-ISIN): L1 board meetings, announcements and shareholding carry
   NSE's stated, original ISIN, and each row is moved to the ISIN the new lineage says was live on
   its knowable date (`ingest.filing_rekey`). Only `isin` changes; the stated one stays beside it.

**MANUAL edges.** A row recorded by hand (`detected_by = 'MANUAL'`, e.g. `repair_avenuesai`'s
INE483S01020 → INE483S01038) is evidence the derivation cannot see. Every rebuild keeps it as
stored, walks it in the chain, and stitches its survivor's L2 over it exactly as a derived edge.
Precedence (`lineage.merge_with_manual`): a derived edge that restates a MANUAL one is not
written twice; one that contradicts a MANUAL one stops the rebuild *before anything is written*
— the conflict is queued in `identity_reconciliation` (kind `LINEAGE_EDGE`) and
`LineageConflictError` is raised.
Resolving that row decides for the MANUAL edge, and the next rebuild drops the derived one;
deleting the MANUAL row decides for the derived edge. Nothing ever picks a side silently.

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
    uv run python -m dataplatform.identity.lineage_rebuild --compare       # dry run: a diff
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass

from dataplatform.clock import Clock, SystemClock
from dataplatform.config import get_settings
from dataplatform.corpactions.reconcile import SingleSourcePolicy
from dataplatform.identity.fund_lineage import derive_fund_edges
from dataplatform.identity.lineage import (
    EqPresence,
    LineageConflict,
    LineageConflictError,
    LineageEdge,
    LineageStore,
    LineageWriteReport,
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
from dataplatform.store.db import Connection, connect
from dataplatform.store.l0 import L0Store
from dataplatform.store.l2 import (
    materialize_missing,
    open_connection,
    prune_retired,
    rebuild_invalidated,
)

__all__ = ["LineageDiff", "LineageRebuildReport", "compare", "rebuild"]

_LOG = get_logger(__name__)

_CA_SOURCE = nse_ca.SOURCE_ID


@dataclass(frozen=True, slots=True)
class LineageRebuildReport:
    """What each stage of one rebuild did."""

    edges_derived: int
    fund_edges_derived: int
    edges_written: int
    manual_edges_kept: int
    derived_agreeing_with_manual: int
    derived_overridden_by_manual: int
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
    #: Stage 6: filing-feed rows whose ISIN pair the re-key changed.
    filing_rows_rekeyed: int = 0

    def counts(self) -> dict[str, int]:
        """Every per-stage count, in field order — what the CLI prints as its table."""
        return {k: v for k, v in asdict(self).items() if isinstance(v, int)}


def _derive(
    clock: Clock,
) -> tuple[tuple[LineageEdge, ...], tuple[LineageEdge, ...], Mapping[str, EqPresence]]:
    """Stage 1's derivation, no writes: `(all edges, the fund edges among them, presence)`."""
    data_root = get_settings().data_root
    spans, sessions = read_equity_spans(data_root=data_root)
    equity_edges = derive_edges(spans, sessions, read_corroboration(data_root=data_root))
    # A fund edge's predecessor is an INF ISIN, which `derive_edges` never emits, so the two sets
    # cannot claim the same predecessor and the table's one-successor-per-predecessor index holds.
    fund_edges = derive_fund_edges(clock=clock, data_root=data_root)
    edges = (*equity_edges, *fund_edges)
    presence = read_eq_presence({e.successor_isin for e in edges}, data_root=data_root)
    return edges, fund_edges, presence


def write_lineage(
    conn: Connection,
    store: LineageStore,
    edges: Sequence[LineageEdge],
    *,
    presence: Mapping[str, EqPresence],
) -> LineageWriteReport:
    """Replace the derived edges; on a MANUAL conflict, queue it, commit the queue, and raise.

    `replace_derived` refuses before writing anything, so the rollback here discards nothing of
    the lineage; the conflict rows are then committed on their own, because a queue entry that
    rolled back with the failure would be the log line nobody reads. Re-raises
    `LineageConflictError` — the rebuild must not go on to replay or rebuild L2 over a lineage a
    human has not settled.
    """
    try:
        return store.replace_derived(edges, presence=presence)
    except LineageConflictError as error:
        conn.rollback()
        queued = store.queue_conflicts(error.conflicts)
        conn.commit()
        _LOG.error(
            "lineage_rebuild.manual_conflict",
            conflicts=len(error.conflicts),
            newly_queued=queued,
        )
        raise


@dataclass(frozen=True, slots=True)
class LineageDiff:
    """What stage 1 *would* change in `isin_lineage` and `security_master`, rolled back."""

    #: `isin_lineage` rows (without `computed_at`) the rebuild would add / remove. A changed edge
    #: is one of each with the same predecessor.
    added: tuple[tuple[object, ...], ...]
    removed: tuple[tuple[object, ...], ...]
    registered_intermediates: tuple[str, ...]
    manual_kept: int
    agreeing: tuple[LineageEdge, ...]
    overridden: tuple[LineageConflict, ...]
    #: Non-empty means the rebuild would stop here (and, run for real, queue these).
    conflicts: tuple[LineageConflict, ...]


_LINEAGE_ROWS_SQL = (
    "SELECT predecessor_isin, successor_isin, effective_date, detected_by, confidence, "
    "gap_sessions, symbol_at_change, corroborating_action FROM isin_lineage"
)


def compare(*, clock: Clock | None = None) -> LineageDiff:
    """Run stage 1 inside a transaction that is always rolled back; return the difference.

    The dry run of a rebuild: derives from the same L0/L1 as `rebuild`, writes through the same
    `replace_derived`, diffs the table before and after, and rolls back — nothing is committed,
    and a conflict is reported, not queued. Read this before a real rebuild.
    """
    clock = SystemClock() if clock is None else clock
    edges, _, presence = _derive(clock)
    with connect() as conn:
        try:
            before = set(conn.execute(_LINEAGE_ROWS_SQL).fetchall())
            store = LineageStore(conn, clock=clock)
            try:
                lineage = store.replace_derived(edges, presence=presence)
            except LineageConflictError as error:
                return LineageDiff((), (), (), len(store.load_manual()), (), (), error.conflicts)
            after = set(conn.execute(_LINEAGE_ROWS_SQL).fetchall())
        finally:
            conn.rollback()
    return LineageDiff(
        added=tuple(sorted(after - before, key=str)),
        removed=tuple(sorted(before - after, key=str)),
        registered_intermediates=lineage.registered_intermediates,
        manual_kept=lineage.manual_kept,
        agreeing=lineage.agreeing,
        overridden=lineage.overridden,
        conflicts=(),
    )


def _print_diff(diff: LineageDiff) -> int:
    print(f"manual_edges_kept                    {diff.manual_kept}")
    print(f"derived_agreeing_with_manual         {len(diff.agreeing)}")
    for e in diff.agreeing:
        print(f"  agrees {e.predecessor_isin} -> {e.successor_isin} {e.effective_date}")
    print(f"derived_overridden_by_manual         {len(diff.overridden)}")
    for c in diff.overridden:
        print(f"  overridden {c}")
    print(f"conflicts                            {len(diff.conflicts)}")
    for c in diff.conflicts:
        print(f"  CONFLICT {c}")
    print(f"rows_added                           {len(diff.added)}")
    for row in diff.added:
        print("  + " + " ".join(str(v) for v in row))
    print(f"rows_removed                         {len(diff.removed)}")
    for row in diff.removed:
        print("  - " + " ".join(str(v) for v in row))
    print(f"registered_intermediates             {len(diff.registered_intermediates)}")
    for isin in diff.registered_intermediates:
        print(f"  register {isin}")
    print("rolled back: nothing was written")
    return 1 if diff.conflicts else 0


def rebuild(*, clock: Clock | None = None, derive_only: bool = False) -> LineageRebuildReport:
    """Run the four stages against the configured store and lake; return what each did."""
    clock = SystemClock() if clock is None else clock
    data_root = get_settings().data_root

    # ── 1. derive ────────────────────────────────────────────────────────────────────────────
    edges, fund_edges, presence = _derive(clock)

    with connect() as conn:
        store = LineageStore(conn, clock=clock)
        lineage = write_lineage(conn, store, edges, presence=presence)
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
                manual_edges_kept=lineage.manual_kept,
                derived_agreeing_with_manual=len(lineage.agreeing),
                derived_overridden_by_manual=len(lineage.overridden),
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
        # Every stored edge's successor, MANUAL ones included — not just this run's derived set,
        # which would leave a MANUAL survivor's partition starting at its reissue.
        history = {
            isin: chain
            for isin in resolver.survivors()
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

    # ── 6. re-key the filing feeds through the lineage just written (DQ-ISIN) ────────────────
    # Imported here: `ingest` imports `identity`, and the package must not import back at load.
    from dataplatform.identity.filing_isin import load_filing_resolver
    from dataplatform.ingest.filing_rekey import rekey_lake

    with connect() as conn:
        filing = load_filing_resolver(conn)
    rekeyed = rekey_lake(filing, data_root=data_root)

    stitched = sum(1 for r in reports if r.isin in history)
    report = LineageRebuildReport(
        edges_derived=lineage.derived,
        fund_edges_derived=len(fund_edges),
        edges_written=lineage.written,
        manual_edges_kept=lineage.manual_kept,
        derived_agreeing_with_manual=len(lineage.agreeing),
        derived_overridden_by_manual=len(lineage.overridden),
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
        filing_rows_rekeyed=sum(r.tally.changed for r in rekeyed),
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
    parser.add_argument(
        "--compare",
        action="store_true",
        help="dry run: derive, write stage 1 in a transaction, print the diff, roll back",
    )
    args = parser.parse_args()
    if args.compare:
        return _print_diff(compare())
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
