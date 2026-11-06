"""DQ-ISIN: re-key the filing feeds already in L1 to the ISIN live on each row's knowable date.

NSE's board-meeting, announcement and shareholding feeds state a company's original ISIN
(`dataplatform.identity.filing_isin`). The nightly captures re-key what they land; this driver
re-keys what is already in L1, and is the step to run after anything that changes `isin_lineage`
(`identity.lineage_rebuild`, a MANUAL edge), because a partition the captures do not touch again
keeps the lineage it was written with.

**What it does.** For each of the three datasets, reads every partition, resolves each row's
*stated* ISIN (`isin_as_filed`, or `isin` on a partition no re-key has touched, which holds it
verbatim) at the row's own knowable date, and rewrites the partition when any row's ISIN pair
changed. Every other column, and the partition a row sits in, is left exactly as it was. It
reports, per dataset, rows by status and every stated ISIN left unmapped with its reason.

**Why from L1 and not a re-parse of L0.** The stated ISIN is a column L1 already holds verbatim
from L0, and the re-key is a pure function of it, the row's knowable date and the lineage. Re-
parsing L0 would re-run every merge as well (board meetings keep the first capture's row; the
shareholding master's later poll wins), which this change does not touch and should not risk.

**What it never does.** Fetch, write L0, or move a row between partitions. Running it twice over
the same lineage rewrites nothing the second time, and two runs over the same lake and lineage
write byte-identical partitions.

    uv run python -m dataplatform.ingest.filing_rekey --dry-run     # counts only, no write
    uv run python -m dataplatform.ingest.filing_rekey               # re-key L1 in place
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Final

from dataplatform.config import get_settings
from dataplatform.identity.filing_isin import (
    FilingIsinResolver,
    RekeyTally,
    load_filing_resolver,
    unmapped_counts,
)
from dataplatform.ingest import announcements, shareholding
from dataplatform.ingest.nse import board_meetings
from dataplatform.logging import configure_logging, get_logger
from dataplatform.store.db import connection
from dataplatform.store.paths import Layer, layer_root, partition_date_of

__all__ = ["DATASETS", "DatasetRekey", "main", "rekey_lake"]

_LOG = get_logger(__name__)


@dataclass(slots=True)
class DatasetRekey:
    """What a re-key did to one dataset."""

    dataset: str
    partitions: int = 0
    rewritten: int = 0
    tally: RekeyTally = field(default_factory=RekeyTally)

    def as_dict(self) -> dict[str, object]:
        return {
            "dataset": self.dataset,
            "partitions": self.partitions,
            "rewritten": self.rewritten,
            **self.tally.as_dict(),
            "unmapped_by_reason": unmapped_counts(self.tally),
            "unmapped": dict(sorted(self.tally.unmapped.items())),
        }


def _rekey_board_meetings(
    day: date, resolver: FilingIsinResolver, data_root: Path | None, write: bool
) -> RekeyTally:
    rows, tally = board_meetings.rekey(board_meetings.read_l1(day, data_root=data_root), resolver)
    if write and tally.changed:
        board_meetings.write_partition(day, rows, data_root=data_root)
    return tally


def _rekey_announcements(
    day: date, resolver: FilingIsinResolver, data_root: Path | None, write: bool
) -> RekeyTally:
    batch = announcements.read_l1(day, data_root=data_root)
    rows, tally = announcements.rekey(batch.rows, resolver)
    if write and tally.changed:
        announcements.write_l1(batch.model_copy(update={"rows": rows}), data_root=data_root)
    return tally


def _rekey_shareholding(
    day: date, resolver: FilingIsinResolver, data_root: Path | None, write: bool
) -> RekeyTally:
    held = shareholding.read_l1(day, data_root=data_root)
    rows, tally = shareholding.rekey(held, resolver)
    if write and tally.changed:
        shareholding.write_l1(
            shareholding.ShareholdingSnapshot(
                source=rows[0].source,
                rows=tuple(sorted(rows, key=lambda row: (row.isin, row.period_end))),
            ),
            data_root=data_root,
        )
    return tally


_Rekey = Callable[[date, FilingIsinResolver, Path | None, bool], RekeyTally]

#: Each dataset this applies to, with its partition re-key. All three carry NSE's stated ISIN.
DATASETS: Final[Mapping[str, _Rekey]] = {
    board_meetings.BOARD_MEETINGS_DATASET: _rekey_board_meetings,
    announcements.ANNOUNCEMENTS_DATASET: _rekey_announcements,
    shareholding.SHAREHOLDING_DATASET: _rekey_shareholding,
}


def rekey_lake(
    resolver: FilingIsinResolver,
    *,
    data_root: Path | None = None,
    dry_run: bool = False,
    datasets: Sequence[str] = tuple(DATASETS),
) -> tuple[DatasetRekey, ...]:
    """Re-key every partition of ``datasets`` through ``resolver``; see the module docstring.

    ``dry_run`` resolves and counts everything and writes nothing.
    """
    reports: list[DatasetRekey] = []
    for name in datasets:
        run = DATASETS[name]
        report = DatasetRekey(dataset=name)
        root = layer_root(Layer.L1, data_root=data_root) / name
        for part in sorted(root.glob("date=*/part.parquet")) if root.is_dir() else ():
            day = partition_date_of(part.parent)
            tally = run(day, resolver, data_root, not dry_run)
            report.partitions += 1
            if tally.changed:
                report.rewritten += 1
            report.tally.merge(tally)
        _LOG.info(
            "filing_rekey.dataset_done",
            dataset=name,
            dry_run=dry_run,
            partitions=report.partitions,
            rewritten=0 if dry_run else report.rewritten,
            **report.tally.as_dict(),
            unmapped_by_reason=unmapped_counts(report.tally),
            state="DRY_RUN" if dry_run else "NORMALIZED",
        )
        reports.append(report)
    return tuple(reports)


def main(argv: Sequence[str] | None = None) -> int:
    """`python -m dataplatform.ingest.filing_rekey [--dry-run] [--dataset NAME ...]`.

    Reads `isin_lineage` (SELECT only) and re-keys the configured lake's L1. Prints one JSON report.
    """
    parser = argparse.ArgumentParser(
        prog="python -m dataplatform.ingest.filing_rekey",
        description="Re-key L1 board meetings, announcements and shareholding through the lineage.",
    )
    parser.add_argument("--dry-run", action="store_true", help="count only; write nothing")
    parser.add_argument(
        "--dataset", action="append", choices=sorted(DATASETS), help="limit to these datasets"
    )
    args = parser.parse_args(argv)
    configure_logging()
    settings = get_settings()
    with connection(settings) as conn:
        resolver = load_filing_resolver(conn)
        conn.rollback()
    reports = rekey_lake(
        resolver,
        data_root=settings.data_root,
        dry_run=args.dry_run,
        datasets=tuple(args.dataset or DATASETS),
    )
    print(
        json.dumps(
            {
                "dry_run": args.dry_run,
                "edges": len(resolver),
                "datasets": [r.as_dict() for r in reports],
            },
            indent=1,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
