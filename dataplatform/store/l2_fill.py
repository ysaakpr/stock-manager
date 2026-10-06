"""Materialize the L2 partitions L1 has EQ bars for and nothing ever built (M2.5, first-time fill).

`rebuild_invalidated` drains the corporate-action queue and so only ever builds the ISINs a
recompute flagged. A name with no reconciled action — no split, no bonus, no dividend, no reissue —
never got a partition, however long it traded: on the server on 2026-09-07 that was 793 of the
2,716 NSE EQ names then trading, ADANIGREEN, ADANIENSOL and ETERNAL among them. This is the
one-shot (and safely repeatable) fill for them. Retired ISINs are skipped — the D2 lineage puts
their bars in the survivor's stitched partition — and nothing already on disk is rewritten.

`--extend` (W3) is the other half: it rebuilds the partitions that *do* exist but start later than
the L1 history under them, which is every partition once W1 took L1 back to 2011-06-22 while L2
stayed at 2016-09-02 (`l2.rebuild_truncated`). Retired ISINs' partitions are skipped and counted
here too — extending one would duplicate the survivor's stitched history. `--dry-run` reports
what it would rebuild and how much of the newly exposed window the factor chain covers, and
writes nothing.

`--rebuild-all` rebuilds every partition from L1 + its factor chain — the pass that carries a
change in how a partition is *built* (the price-implied splits of `corpactions.implied`) onto the
partitions already on disk, which no other mode rewrites. `--prune-retired` only removes the
partitions of lineage-retired ISINs; every writing mode (fill, `--extend`, `--rebuild-all`) also
ends with that prune, because a retired ISIN's partition duplicates its survivor's stitched
history with the reissue split unadjusted.

`--rebuild-invalidated` drains the `l2_invalidation` queue on its own — the step after a
corporate-action refresh (`ingest.ca_refresh`) recomputed some factor chains, rebuilding exactly
those ISINs, each over its lineage chain, and nothing else.

Offline: reads L1 and Postgres, fetches nothing. `lineage_rebuild` runs the same fill as its last
stage; this entry point is for a lake where only the fill is wanted.

    uv run python -m dataplatform.store.l2_fill
    uv run python -m dataplatform.store.l2_fill --extend --dry-run
    uv run python -m dataplatform.store.l2_fill --extend
    uv run python -m dataplatform.store.l2_fill --rebuild-all
    uv run python -m dataplatform.store.l2_fill --prune-retired
    uv run python -m dataplatform.store.l2_fill --rebuild INE018I01017 INF846K01ZL0
    uv run python -m dataplatform.store.l2_fill --rebuild-invalidated
"""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

from dataplatform.clock import Clock, SystemClock
from dataplatform.config import get_settings
from dataplatform.identity.lineage import LineageResolver, LineageStore
from dataplatform.logging import get_logger
from dataplatform.store.db import Connection, connect
from dataplatform.store.l2 import (
    L2FillReport,
    L2RebuildReport,
    L2TruncatedReport,
    L2WriteReport,
    materialize_missing,
    open_connection,
    prune_retired,
    rebuild_all,
    rebuild_invalidated,
    rebuild_isins,
    rebuild_truncated,
)

__all__ = [
    "ExtensionCoverage",
    "drain_invalidated",
    "drain_invalidated_with",
    "extend",
    "extension_coverage",
    "fill",
    "prune",
    "rebuild_everything",
    "rebuild_named",
]

_LOG = get_logger(__name__)

#: Action types that move a price level; one left unreconciled inside a newly exposed window is a
#: cliff in the adjusted series there, not a missing dividend.
_LEVEL_ACTIONS: tuple[str, ...] = ("SPLIT", "BONUS", "RIGHTS", "DEMERGER", "SCHEME_OF_ARRANGEMENT")


def _history(conn: Connection) -> tuple[dict[str, tuple[str, ...]], LineageResolver]:
    """Each reissued survivor's lineage chain, oldest-first, and the resolver it came from."""
    resolver = LineageStore(conn).load()
    history = {
        isin: tuple(chain)
        for isin in resolver.survivors()
        if len(chain := resolver.chain_to(isin)) > 1
    }
    return history, resolver


def fill() -> L2FillReport:
    """Run the fill against the configured store and lake; return what it did."""
    settings = get_settings()
    with connect() as conn:
        history, resolver = _history(conn)
        con = open_connection()
        try:
            report = materialize_missing(
                conn,
                con=con,
                data_root=settings.data_root,
                history_for=history,
                survivor_of=resolver.survivor_of,
            )
        finally:
            con.close()
    prune_retired(resolver.survivor_of, data_root=settings.data_root)
    return report


def prune() -> tuple[str, ...]:
    """Remove the configured lake's lineage-retired L2 partitions; return their ISINs."""
    settings = get_settings()
    with connect() as conn:
        resolver = LineageStore(conn).load()
    return prune_retired(resolver.survivor_of, data_root=settings.data_root)


def drain_invalidated_with(
    conn: Connection, *, clock: Clock, data_root: Path | None
) -> tuple[L2WriteReport, ...]:
    """Drain the `l2_invalidation` queue over `conn`, each ISIN over its D2 lineage chain.

    What it does: `rebuild_invalidated` with the lineage history and survivor map every other
    writing mode here uses, so a reissued name is rebuilt across its reissue and a retired ISIN is
    resolved without being built. Rebuilds exactly the flagged ISINs.
    What it assumes: the caller owns `conn`'s transaction and commits it.
    What it never does: touch an ISIN with no open invalidation, or fetch.
    """
    history, resolver = _history(conn)
    con = open_connection()
    try:
        return rebuild_invalidated(
            conn,
            clock=clock,
            con=con,
            data_root=data_root,
            history_for=history,
            survivor_of=resolver.survivor_of,
        )
    finally:
        con.close()


def drain_invalidated(clock: Clock | None = None) -> tuple[L2WriteReport, ...]:
    """Drain the configured store's `l2_invalidation` queue against the configured lake; commit."""
    settings = get_settings()
    with connect() as conn:
        reports = drain_invalidated_with(
            conn, clock=SystemClock() if clock is None else clock, data_root=settings.data_root
        )
        conn.commit()
    return reports


def rebuild_everything() -> L2RebuildReport:
    """Rebuild every L2 partition of the configured lake and prune the retired ones."""
    settings = get_settings()
    with connect() as conn:
        history, resolver = _history(conn)
        con = open_connection()
        try:
            return rebuild_all(
                conn,
                con=con,
                data_root=settings.data_root,
                history_for=history,
                survivor_of=resolver.survivor_of,
            )
        finally:
            con.close()


def rebuild_named(isins: Sequence[str]) -> tuple[L2WriteReport, ...]:
    """Rebuild only the named partitions of the configured lake (`l2.rebuild_isins`)."""
    settings = get_settings()
    with connect() as conn:
        history, resolver = _history(conn)
        con = open_connection()
        try:
            return rebuild_isins(
                conn,
                isins,
                con=con,
                data_root=settings.data_root,
                history_for=history,
                survivor_of=resolver.survivor_of,
            )
        finally:
            con.close()


@dataclass(frozen=True, slots=True)
class ExtensionCoverage:
    """How much of the newly exposed window the factor chain covers, over the truncated ISINs.

    `with_factors` have at least one `adjustment_factors` row dated inside their new window;
    `with_unreconciled_level_action` have a split/bonus/rights/demerger/scheme there that never
    reached the chain, so their adjusted series carries an unadjusted step until it is reconciled.
    `new_first_year` counts ISINs by the year their series now starts.
    """

    truncated: int
    with_factors: int
    factor_rows_in_window: int
    with_unreconciled_level_action: int
    unreconciled_level_actions: int
    new_first_year: Mapping[int, int]


def extension_coverage(
    conn: Connection, truncated: Mapping[str, tuple[date, date]]
) -> ExtensionCoverage:
    """Measure factor coverage over each truncated ISIN's new window `[L1 first, L2 first)`.

    Read-only. Reads only ISIN-keyed rows (`adjustment_factors`, `corporate_actions.isin`); a
    symbol-keyed record that never resolved to an ISIN is, by construction, not counted as coverage.
    """
    with_factors = factor_rows = with_open = open_actions = 0
    for isin, (l2_first, l1_first) in truncated.items():
        row = conn.execute(
            "SELECT count(*) FROM adjustment_factors WHERE isin = %s "
            "AND ex_date >= %s AND ex_date < %s",
            (isin, l1_first, l2_first),
        ).fetchone()
        n = 0 if row is None else int(row[0])
        factor_rows += n
        with_factors += n > 0
        row = conn.execute(
            "SELECT count(*) FROM corporate_actions WHERE isin = %s AND NOT reconciled "
            "AND action_type = ANY(%s) AND ex_date >= %s AND ex_date < %s",
            (isin, list(_LEVEL_ACTIONS), l1_first, l2_first),
        ).fetchone()
        m = 0 if row is None else int(row[0])
        open_actions += m
        with_open += m > 0
    years = Counter(l1_first.year for _, l1_first in truncated.values())
    return ExtensionCoverage(
        truncated=len(truncated),
        with_factors=with_factors,
        factor_rows_in_window=factor_rows,
        with_unreconciled_level_action=with_open,
        unreconciled_level_actions=open_actions,
        new_first_year=dict(sorted(years.items())),
    )


def extend(*, dry_run: bool) -> tuple[L2TruncatedReport, ExtensionCoverage]:
    """Rebuild (or, on a dry run, only find) every truncated partition; measure factor coverage."""
    settings = get_settings()
    with connect() as conn:
        history, resolver = _history(conn)
        con = open_connection()
        try:
            report = rebuild_truncated(
                conn,
                con=con,
                data_root=settings.data_root,
                history_for=history,
                survivor_of=resolver.survivor_of,
                dry_run=dry_run,
            )
        finally:
            con.close()
        coverage = extension_coverage(conn, report.truncated)
    if not dry_run:
        prune_retired(resolver.survivor_of, data_root=settings.data_root)
    _LOG.info(
        "l2_fill.extension_coverage",
        dry_run=dry_run,
        **{k: v for k, v in asdict(coverage).items() if k != "new_first_year"},
    )
    return report, coverage


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; prints the fill's (or the extension's) counts."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--extend",
        action="store_true",
        help="rebuild existing partitions that start later than their L1 EQ history",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="with --extend: report only, write nothing"
    )
    parser.add_argument(
        "--rebuild-all",
        action="store_true",
        help="rebuild every partition from L1 + factors (and implied splits); prune retired",
    )
    parser.add_argument(
        "--rebuild",
        nargs="+",
        metavar="ISIN",
        help="rebuild only these partitions (lineage survivors), e.g. after a curated action",
    )
    parser.add_argument(
        "--prune-retired",
        action="store_true",
        help="only remove the partitions of lineage-retired ISINs",
    )
    parser.add_argument(
        "--rebuild-invalidated",
        action="store_true",
        help="only drain the l2_invalidation queue: rebuild the ISINs a CA recompute flagged",
    )
    args = parser.parse_args(argv)
    if args.dry_run and not args.extend:
        parser.error("--dry-run applies to --extend")
    modes = (
        args.extend,
        args.rebuild_all,
        args.prune_retired,
        bool(args.rebuild),
        args.rebuild_invalidated,
    )
    if sum(modes) > 1:
        parser.error(
            "--extend, --rebuild-all, --rebuild, --prune-retired and --rebuild-invalidated are "
            "separate modes"
        )
    if args.rebuild_invalidated:
        drained = drain_invalidated()
        print(f"{'rebuilt':<24} {len(drained)}")
        print(f"{'rows_written':<24} {sum(r.rows_written for r in drained)}")
        print(f"{'implied_splits':<24} {sum(len(r.implied_splits) for r in drained)}")
        for drained_isin in drained:
            for split in drained_isin.implied_splits:
                print(f"{'  implied':<24} {split.isin} {split.ex_date.isoformat()}")
        return 0
    if args.rebuild:
        written = rebuild_named(args.rebuild)
        for r in written:
            print(
                f"{r.isin} rows={r.rows_written} curated={len(r.curated)} "
                f"implied={len(r.implied_splits)}"
            )
        return 0
    if args.prune_retired:
        pruned = prune()
        print(f"{'pruned_retired':<24} {len(pruned)}")
        return 0
    if args.rebuild_all:
        rebuilt = rebuild_everything()
        print(f"{'candidates':<24} {rebuilt.candidates}")
        print(f"{'skipped_retired':<24} {rebuilt.skipped_retired}")
        print(f"{'pruned_retired':<24} {len(rebuilt.pruned_retired)}")
        print(f"{'written':<24} {len(rebuilt.written)}")
        print(f"{'rows_written':<24} {rebuilt.rows_written}")
        print(f"{'implied_splits':<24} {len(rebuilt.implied_splits)}")
        print(f"{'curated_actions':<24} {len(rebuilt.curated)}")
        return 0
    if args.extend:
        report, coverage = extend(dry_run=args.dry_run)
        print(f"{'partitions':<32} {report.partitions}")
        print(f"{'truncated':<32} {len(report.truncated)}")
        print(f"{'skipped_retired':<32} {report.skipped_retired}")
        print(f"{'written':<32} {len(report.written)}")
        print(f"{'rows_written':<32} {report.rows_written}")
        for field, value in asdict(coverage).items():
            print(f"{field:<32} {value}")
        return 0
    fill_report = fill()
    counts = {k: v for k, v in asdict(fill_report).items() if k != "written"}
    counts["written"] = len(fill_report.written)
    counts["rows_written"] = fill_report.rows_written
    for field, value in counts.items():
        print(f"{field:<24} {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
