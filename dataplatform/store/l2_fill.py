"""Materialize the L2 partitions L1 has EQ bars for and nothing ever built (M2.5, first-time fill).

`rebuild_invalidated` drains the corporate-action queue and so only ever builds the ISINs a
recompute flagged. A name with no reconciled action — no split, no bonus, no dividend, no reissue —
never got a partition, however long it traded: on the server on 2026-09-07 that was 793 of the
2,716 NSE EQ names then trading, ADANIGREEN, ADANIENSOL and ETERNAL among them. This is the
one-shot (and safely repeatable) fill for them. Retired ISINs are skipped — the D2 lineage puts
their bars in the survivor's stitched partition — and nothing already on disk is rewritten.

Offline: reads L1 and Postgres, fetches nothing. `lineage_rebuild` runs the same fill as its last
stage; this entry point is for a lake where only the fill is wanted.

    uv run python -m dataplatform.store.l2_fill
"""

from __future__ import annotations

from dataclasses import asdict

from dataplatform.config import get_settings
from dataplatform.identity.lineage import LineageStore
from dataplatform.logging import get_logger
from dataplatform.store.db import connect
from dataplatform.store.l2 import L2FillReport, materialize_missing, open_connection

__all__ = ["fill"]

_LOG = get_logger(__name__)


def fill() -> L2FillReport:
    """Run the fill against the configured store and lake; return what it did."""
    settings = get_settings()
    with connect() as conn:
        resolver = LineageStore(conn).load()
        history = {
            isin: chain
            for isin in resolver.survivors()
            if len(chain := resolver.chain_to(isin)) > 1
        }
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


def main() -> int:
    """CLI entry point; prints the fill's counts."""
    report = fill()
    counts = {k: v for k, v in asdict(report).items() if k != "written"}
    counts["written"] = len(report.written)
    counts["rows_written"] = report.rows_written
    for field, value in counts.items():
        print(f"{field:<24} {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
