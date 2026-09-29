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

Offline: reads L1 and Postgres, fetches nothing. `lineage_rebuild` runs the same fill as its last
stage; this entry point is for a lake where only the fill is wanted.

    uv run python -m dataplatform.store.l2_fill
    uv run python -m dataplatform.store.l2_fill --extend --dry-run
    uv run python -m dataplatform.store.l2_fill --extend
"""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date

from dataplatform.config import get_settings
from dataplatform.identity.lineage import LineageResolver, LineageStore
from dataplatform.logging import get_logger
from dataplatform.store.db import Connection, connect
from dataplatform.store.l2 import (
    L2FillReport,
    L2TruncatedReport,
    materialize_missing,
    open_connection,
    rebuild_truncated,
)

__all__ = ["ExtensionCoverage", "extend", "extension_coverage", "fill"]

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
    return report


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
    args = parser.parse_args(argv)
    if args.dry_run and not args.extend:
        parser.error("--dry-run applies to --extend")
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
