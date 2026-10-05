"""D7: delivery coverage — how much of each session's `prices_raw` carries a delivery figure.

`quarantine.py` counts the delivery rows L1 *refused*; this counts the price rows that ended up
*without* one, which is the number a delivery-weighted signal actually suffers from. The two are
different questions: a refused delivery row is a symptom, a price row with `deliv_qty IS NULL` is
the damage. The 2026-10-05 audit measured the damage by hand — NSE EQ `deliv_qty` null on 1.69 M of
6.08 M rows, 48 % in 2011 falling to 15 % in 2025 — because nothing in the platform reported it.

Three things it does:

* **Count.** `read_delivery_coverage` reads `prices_raw` over a range and returns, per session,
  the in-scope price rows, how many carry `deliv_qty`, and how many state a delivered quantity
  larger than the session's traded quantity — an impossibility the exchange's MTO report published
  for six NSE EQ rows on 2019-06-17/18, kept verbatim in L1 (it is what the source said) and
  surfaced here rather than corrected.
* **Judge.** `coverage_findings` turns a session below `floor` into a WARN `QualityFinding`, and a
  session with any delivery-exceeds-traded row into another. WARN, not ERROR: a missing delivery
  figure degrades one signal and is not a reason to stop trading the session (invariant #10).
* **Report.** `python -m dataplatform.quality.delivery_coverage --from … --to …` prints coverage per
  year and the quarantine reason counts beside it — the before/after table a rebuild is judged on.

Read-only over the lake, and never touches Postgres.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final

import duckdb

from dataplatform.logging import get_logger
from dataplatform.quality.sentinel import QualityFinding, finding_fingerprint
from dataplatform.store.paths import Layer, layer_root
from dataplatform.store.schemas import PRICES_RAW_DATASET, PRICES_RAW_QUARANTINE_DATASET

__all__ = [
    "COVERAGE_CHECK_NAME",
    "DEFAULT_FLOOR_PCT",
    "DEFAULT_SERIES",
    "EQ_LIKE_SERIES",
    "EXCEEDS_CHECK_NAME",
    "DeliveryCoverage",
    "QuarantineReasonCount",
    "coverage_by_year",
    "coverage_findings",
    "main",
    "read_delivery_coverage",
    "read_quarantine_reasons",
]

_LOG = get_logger(__name__)

#: The series a delivery figure is expected on. `EQ` is the rolling-settlement equity series and
#: carries delivery on every row the exchange reports it for; `BE`/`BZ` are trade-to-trade, where
#: the exchange writes `-` (absence, by design — `delivery.py`), so counting them would read a
#: deliberate absence as a gap.
DEFAULT_SERIES: Final = ("EQ",)

#: Series counted as "EQ-like" when splitting quarantine reasons: the equity series plus the
#: trade-to-trade and SME ones, i.e. everything that is a share rather than debt or a warrant.
EQ_LIKE_SERIES: Final = ("EQ", "BE", "BZ", "SM", "ST")

#: Below this share of in-scope rows with a delivery figure, a session is a finding. Measured on the
#: 2026-10-05 /tmp re-ingest after the lineage and session-identity fixes: 3,762 of 3,763 ISIN-era
#: sessions clear it, the worst of them by a few dozen illiquid names the exchange's own delivery
#: file omits; the one below is 2021-11-04, whose stored delivery payload is another session's.
DEFAULT_FLOOR_PCT: Final = Decimal("95")

#: `quality_flag.check_name` for a session below the floor, and for an impossible delivery row.
COVERAGE_CHECK_NAME: Final = "delivery_coverage_below_floor"
EXCEEDS_CHECK_NAME: Final = "delivery_exceeds_traded"


@dataclass(frozen=True, slots=True)
class DeliveryCoverage:
    """One session's delivery coverage on one exchange over the in-scope series."""

    trade_date: date
    exchange: str
    rows: int
    with_delivery: int
    exceeds_traded: int

    @property
    def missing(self) -> int:
        """In-scope price rows with no delivery figure."""
        return self.rows - self.with_delivery

    @property
    def pct(self) -> Decimal:
        """Share of in-scope rows carrying `deliv_qty`, in percent, two places. 0 for no rows."""
        if self.rows == 0:
            return Decimal(0)
        return (Decimal(100) * self.with_delivery / self.rows).quantize(Decimal("0.01"))


@dataclass(frozen=True, slots=True)
class QuarantineReasonCount:
    """Quarantined rows for one reason, all series and EQ-like series only."""

    reason: str
    rows: int
    eq_like_rows: int


def _partitions(dataset: str, from_date: date, to_date: date, data_root: Path | None) -> list[str]:
    root = layer_root(Layer.L1, data_root=data_root) / dataset
    return sorted(
        str(path)
        for path in root.glob("date=*/*.parquet")
        if from_date <= date.fromisoformat(path.parent.name.removeprefix("date=")) <= to_date
    )


def _require_range(from_date: date, to_date: date) -> None:
    if from_date > to_date:
        raise ValueError(
            f"from={from_date.isoformat()} is after to={to_date.isoformat()}; "
            "a coverage report needs a range that runs forwards"
        )


def read_delivery_coverage(
    from_date: date,
    to_date: date,
    *,
    exchange: str = "NSE",
    series: Sequence[str] = DEFAULT_SERIES,
    data_root: Path | None = None,
    con: duckdb.DuckDBPyConnection | None = None,
) -> tuple[DeliveryCoverage, ...]:
    """Per-session delivery coverage of `prices_raw` over an inclusive range, oldest first.

    What it does: one grouped scan of the range's partitions, counting in-scope rows, rows with
    `deliv_qty`, and rows whose `deliv_qty` exceeds `total_traded_qty`.
    What it assumes: the hive layout `store/l1.py` writes. A session with no in-scope rows is not
    returned — there is nothing for delivery to cover.
    What it never does: fail for an absent dataset (zero sessions is an answer), or write.
    """
    _require_range(from_date, to_date)
    partitions = _partitions(PRICES_RAW_DATASET, from_date, to_date, data_root)
    if not partitions:
        return ()
    connection = duckdb.connect(":memory:") if con is None else con
    listed = ", ".join(f"'{path}'" for path in partitions)
    rows = connection.execute(
        f"SELECT trade_date, count(*), count(deliv_qty), "
        f"       count(*) FILTER (WHERE deliv_qty > total_traded_qty) "
        f"FROM read_parquet([{listed}]) "
        f"WHERE exchange = ? AND list_contains(?, series) "
        f"GROUP BY 1 ORDER BY 1",
        [exchange, list(series)],
    ).fetchall()
    coverage = tuple(
        DeliveryCoverage(
            trade_date=row[0],
            exchange=exchange,
            rows=int(row[1]),
            with_delivery=int(row[2]),
            exceeds_traded=int(row[3]),
        )
        for row in rows
    )
    _LOG.info(
        "quality.delivery_coverage_read",
        dataset=PRICES_RAW_DATASET,
        exchange=exchange,
        from_date=from_date.isoformat(),
        to_date=to_date.isoformat(),
        partitions=len(partitions),
        sessions=len(coverage),
        rows=sum(c.rows for c in coverage),
        with_delivery=sum(c.with_delivery for c in coverage),
    )
    return coverage


def read_quarantine_reasons(
    from_date: date,
    to_date: date,
    *,
    data_root: Path | None = None,
    con: duckdb.DuckDBPyConnection | None = None,
) -> tuple[QuarantineReasonCount, ...]:
    """`prices_raw_quarantine` rows per reason over a range, with the EQ-like share split out."""
    _require_range(from_date, to_date)
    partitions = _partitions(PRICES_RAW_QUARANTINE_DATASET, from_date, to_date, data_root)
    if not partitions:
        return ()
    connection = duckdb.connect(":memory:") if con is None else con
    listed = ", ".join(f"'{path}'" for path in partitions)
    rows = connection.execute(
        f"SELECT reason, count(*), count(*) FILTER (WHERE list_contains(?, series)) "
        f"FROM read_parquet([{listed}]) GROUP BY 1 ORDER BY 1",
        [list(EQ_LIKE_SERIES)],
    ).fetchall()
    return tuple(
        QuarantineReasonCount(reason=str(r[0]), rows=int(r[1]), eq_like_rows=int(r[2]))
        for r in rows
    )


def coverage_by_year(coverage: Iterable[DeliveryCoverage]) -> tuple[DeliveryCoverage, ...]:
    """The per-session coverage summed per calendar year, each dated 1 January of its year."""
    years: dict[int, list[int]] = {}
    exchange = ""
    for session in coverage:
        exchange = session.exchange
        bucket = years.setdefault(session.trade_date.year, [0, 0, 0])
        bucket[0] += session.rows
        bucket[1] += session.with_delivery
        bucket[2] += session.exceeds_traded
    return tuple(
        DeliveryCoverage(
            trade_date=date(year, 1, 1),
            exchange=exchange,
            rows=rows,
            with_delivery=with_delivery,
            exceeds_traded=exceeds,
        )
        for year, (rows, with_delivery, exceeds) in sorted(years.items())
    )


def coverage_findings(
    coverage: Iterable[DeliveryCoverage], *, floor_pct: Decimal = DEFAULT_FLOOR_PCT
) -> tuple[QualityFinding, ...]:
    """D7 findings for sessions below `floor_pct` coverage and sessions with impossible rows.

    Pure: counts in, findings out. Both WARN, scoped to `prices_raw`. Fingerprinted on
    `(check, exchange, date)`, so re-running over a session already flagged adds nothing.
    """
    findings: list[QualityFinding] = []
    for session in coverage:
        if session.rows and session.pct < floor_pct:
            findings.append(
                QualityFinding(
                    logical_date=session.trade_date,
                    check_name=COVERAGE_CHECK_NAME,
                    severity="WARN",
                    source=PRICES_RAW_DATASET,
                    observed_value=session.pct,
                    threshold=floor_pct,
                    detail={
                        "exchange": session.exchange,
                        "rows": session.rows,
                        "with_delivery": session.with_delivery,
                        "message": (
                            f"{session.missing} of {session.rows} {session.exchange} price rows "
                            f"on {session.trade_date.isoformat()} carry no delivery figure "
                            f"({session.pct}% covered, floor {floor_pct}%)"
                        ),
                    },
                    fingerprint=finding_fingerprint(
                        f"{COVERAGE_CHECK_NAME}:{session.exchange}", None, session.trade_date
                    ),
                )
            )
        if session.exceeds_traded:
            findings.append(
                QualityFinding(
                    logical_date=session.trade_date,
                    check_name=EXCEEDS_CHECK_NAME,
                    severity="WARN",
                    source=PRICES_RAW_DATASET,
                    observed_value=Decimal(session.exceeds_traded),
                    threshold=Decimal(0),
                    detail={
                        "exchange": session.exchange,
                        "message": (
                            f"{session.exceeds_traded} {session.exchange} row(s) on "
                            f"{session.trade_date.isoformat()} state a delivered quantity above "
                            "the traded quantity — kept as published, not corrected"
                        ),
                    },
                    fingerprint=finding_fingerprint(
                        f"{EXCEEDS_CHECK_NAME}:{session.exchange}", None, session.trade_date
                    ),
                )
            )
    return tuple(findings)


def render(
    coverage: Sequence[DeliveryCoverage],
    reasons: Sequence[QuarantineReasonCount],
    *,
    floor_pct: Decimal = DEFAULT_FLOOR_PCT,
) -> str:
    """The markdown report: coverage per year, sessions below the floor, and quarantine reasons."""
    lines = [
        "| year | rows | with delivery | null | null % | deliv > traded |",
        "|---:|---:|---:|---:|---:|---:|",
    ]
    for year in coverage_by_year(coverage):
        null_pct = (Decimal(100) - year.pct) if year.rows else Decimal(0)
        lines.append(
            f"| {year.trade_date.year} | {year.rows} | {year.with_delivery} | {year.missing} | "
            f"{null_pct} | {year.exceeds_traded} |"
        )
    total_rows = sum(c.rows for c in coverage)
    total_with = sum(c.with_delivery for c in coverage)
    below = [c for c in coverage if c.pct < floor_pct]
    lines += [
        "",
        f"total: {total_rows} rows, {total_with} with delivery, {total_rows - total_with} null; "
        f"{len(below)} of {len(coverage)} session(s) below the {floor_pct}% floor",
        "",
        "| quarantine reason | rows | EQ-like rows |",
        "|:---|---:|---:|",
        *(f"| {r.reason} | {r.rows} | {r.eq_like_rows} |" for r in reasons),
    ]
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    """CLI: print the coverage report for a range. Exit 0 always: it reports, it does not gate."""
    parser = argparse.ArgumentParser(prog="delivery-coverage", description=__doc__)
    parser.add_argument("--from", dest="from_date", type=date.fromisoformat, required=True)
    parser.add_argument("--to", dest="to_date", type=date.fromisoformat, required=True)
    parser.add_argument("--exchange", default="NSE")
    parser.add_argument(
        "--data-root", type=Path, default=None, help="lake root (default: settings.data_root)"
    )
    args = parser.parse_args(argv)
    data_root: Path | None = args.data_root
    if data_root is None:
        from dataplatform.config import get_settings

        data_root = get_settings().data_root
    con = duckdb.connect(":memory:")
    con.execute("SET enable_progress_bar = false")
    coverage = read_delivery_coverage(
        args.from_date, args.to_date, exchange=args.exchange, data_root=data_root, con=con
    )
    reasons = read_quarantine_reasons(args.from_date, args.to_date, data_root=data_root, con=con)
    print(render(coverage, reasons))
    return 0


if __name__ == "__main__":
    sys.exit(main())
