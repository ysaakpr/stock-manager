"""D7: reading `prices_raw_quarantine` — the rows L1 refused, and whether today is worse.

`store/l1.py` has always written this dataset, and its docstring calls a quarantined row "a visible
gap, not silent data loss". The first half was true and the second half was not: as of the
2026-09-06 audit the dataset held **1,798,418 rows** across 2,467 of 2,469 partitions, and a grep
over the whole repository found one writer and two readers, both assertions in
`tests/integration/test_l1_writer.py`. No endpoint, no rule, no threshold, no alert, no runbook
step. Discovering the 1.8 M took opening DuckDB by hand. This module is the missing reader
(audit finding N3).

Two things it does, and the second is the one that matters.

**Count.** `read_quarantine` returns one row per `(trade_date, exchange, reason)` over a range,
straight from the parquet. That is the number a dashboard shows and the number a human can watch.

**Judge.** The absolute level is *known* and is somebody else's task: ~30 % of delivery rows fail
to resolve because the identity master is a single 2026-08-08 snapshot that knows 2,397 of the
7,536 ISINs that have traded (gap-plan Action 3). Alerting on the level would fire every day and
be muted within a week. What is actionable is a **step change** — a session where one reason
jumps against its own recent history, which is what a new identity break, a renamed symbol or a
changed file format looks like from here. `step_changes` is that comparison, against a trailing
median rather than a mean so one bad session does not raise the bar for the next.

Pure where it can be: `step_changes` takes counts and returns verdicts, no lake and no clock, so
the rule is testable without writing a parquet file. `read_quarantine` is the one function that
touches the lake.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final

import duckdb

from dataplatform.logging import get_logger
from dataplatform.store.paths import Layer, layer_root
from dataplatform.store.schemas import PRICES_RAW_QUARANTINE_DATASET

__all__ = [
    "DEFAULT_MIN_ROWS",
    "DEFAULT_MULTIPLE",
    "DEFAULT_WINDOW",
    "QuarantineCount",
    "QuarantineReport",
    "QuarantineStep",
    "read_quarantine",
    "step_changes",
]

_LOG = get_logger(__name__)

#: Sessions of history a step change is measured against. Twenty is about a trading month — long
#: enough that a single bad session does not set the baseline, short enough that a genuine regime
#: change (a new era of the delivery file, a master refresh) stops being flagged once it is the
#: new normal rather than staying flagged for a year.
DEFAULT_WINDOW: Final = 20

#: How many times the trailing median counts as a step. Deliberately blunt: the quantity being
#: watched is a count of refusals, which is noisy in the small and unambiguous in the large.
DEFAULT_MULTIPLE: Final = Decimal("2")

#: Below this, a "doubling" is two rows becoming four and means nothing. A floor is what keeps a
#: multiplicative rule from firing on noise in the tail reasons.
DEFAULT_MIN_ROWS: Final = 50


@dataclass(frozen=True, slots=True)
class QuarantineCount:
    """How many rows one session quarantined for one reason on one exchange."""

    trade_date: date
    exchange: str
    reason: str
    rows: int

    @property
    def key(self) -> tuple[str, str]:
        """The series this count belongs to — a step change is measured within one series."""
        return (self.exchange, self.reason)


@dataclass(frozen=True, slots=True)
class QuarantineStep:
    """A session where one reason's count stepped away from its own recent history."""

    trade_date: date
    exchange: str
    reason: str
    rows: int
    baseline: Decimal
    multiple: Decimal

    def detail(self) -> str:
        """The one line an alert or a `quality_flag` carries."""
        return (
            f"{self.reason} quarantined {self.rows} {self.exchange} row(s) on "
            f"{self.trade_date.isoformat()}, {self.multiple}x the trailing median of "
            f"{self.baseline} over the previous sessions — a step change, not the known level"
        )


@dataclass(frozen=True, slots=True)
class QuarantineReport:
    """Every quarantined row in a range, counted by session, exchange and reason.

    `counts` is the enumeration; `totals` is the same thing summed per reason, because the first
    question anyone asks of this dataset is "how much, and of what". `partitions_read` is carried
    so an empty report cannot be mistaken for a clean one — no partitions read and no rows found
    render identically otherwise, which is the mistake the gap report already refuses to make.
    """

    from_date: date
    to_date: date
    counts: tuple[QuarantineCount, ...]
    partitions_read: int

    @property
    def rows(self) -> int:
        """Every quarantined row in the range."""
        return sum(count.rows for count in self.counts)

    def totals(self) -> dict[str, int]:
        """Rows per reason, largest first."""
        out: dict[str, int] = {}
        for count in self.counts:
            out[count.reason] = out.get(count.reason, 0) + count.rows
        return dict(sorted(out.items(), key=lambda item: -item[1]))

    def summary(self) -> str:
        """One line for a log, a runbook or the daily EOD report."""
        detail = ", ".join(f"{reason}={rows}" for reason, rows in self.totals().items())
        return (
            f"{self.from_date}..{self.to_date}: {self.rows} row(s) quarantined over "
            f"{self.partitions_read} partition(s){f' [{detail}]' if detail else ''}"
        )


def read_quarantine(
    from_date: date,
    to_date: date,
    *,
    data_root: Path | None = None,
    con: duckdb.DuckDBPyConnection | None = None,
) -> QuarantineReport:
    """Count `prices_raw_quarantine` by session, exchange and reason over an inclusive range.

    What it does: reads the dataset's partitions for the range and groups them. Nothing is
    interpreted here — the level is what it is, and whether it is *news* is `step_changes`.
    What it assumes: the dataset's hive layout (`date=YYYY-MM-DD/part.parquet`), the same one
    `store/l1.py` writes.
    What it never does: fail because the dataset is absent. A lake with no quarantine partitions
    is a real and good state, and it answers zero rows over zero partitions rather than raising.
    """
    if from_date > to_date:
        raise ValueError(
            f"from={from_date.isoformat()} is after to={to_date.isoformat()}; "
            f"a quarantine report needs a range that runs forwards"
        )
    root = layer_root(Layer.L1, data_root=data_root) / PRICES_RAW_QUARANTINE_DATASET
    partitions = sorted(
        path
        for path in root.glob("date=*/*.parquet")
        if from_date <= _partition_date(path) <= to_date
    )
    if not partitions:
        return QuarantineReport(
            from_date=from_date, to_date=to_date, counts=(), partitions_read=0
        )

    connection = duckdb.connect(":memory:") if con is None else con
    listed = ", ".join(f"'{path}'" for path in partitions)
    rows = connection.execute(
        f"SELECT trade_date, exchange, reason, count(*) "  # noqa: S608 - paths, not user input
        f"FROM read_parquet([{listed}]) GROUP BY 1, 2, 3 ORDER BY 1, 2, 3"
    ).fetchall()
    counts = tuple(
        QuarantineCount(
            trade_date=row[0], exchange=str(row[1]), reason=str(row[2]), rows=int(row[3])
        )
        for row in rows
    )
    report = QuarantineReport(
        from_date=from_date,
        to_date=to_date,
        counts=counts,
        partitions_read=len(partitions),
    )
    _LOG.info(
        "quality.quarantine_read",
        dataset=PRICES_RAW_QUARANTINE_DATASET,
        from_date=from_date.isoformat(),
        to_date=to_date.isoformat(),
        partitions=len(partitions),
        rows=report.rows,
        totals=report.totals(),
    )
    return report


def step_changes(
    counts: Iterable[QuarantineCount],
    *,
    window: int = DEFAULT_WINDOW,
    multiple: Decimal = DEFAULT_MULTIPLE,
    min_rows: int = DEFAULT_MIN_ROWS,
) -> tuple[QuarantineStep, ...]:
    """Sessions where one `(exchange, reason)` series jumped against its own trailing median.

    What it does: walks each series in date order and compares every session against the median of
    the `window` sessions before it. A session at or above `multiple` times that median, and at
    least `min_rows` rows, is a step.
    What it assumes: the counts are the whole range. A session missing from the input is a session
    with no quarantined rows, and it correctly drags the median down.
    What it never does: flag the *level*. ~30 % of delivery rows fail to resolve today and that is
    a known, tracked, unchanging fact (gap-plan Action 3); a rule that fired on it would be muted
    within a week and would then be watching nothing.

    A session with fewer than `window` predecessors is never flagged: there is no history to be a
    step away from, and calling the first session of the lake a step change would be an artefact
    of where the data starts.
    """
    if window < 1:
        raise ValueError(f"window must be at least 1 session, got {window}")
    by_series: dict[tuple[str, str], list[QuarantineCount]] = {}
    for count in counts:
        by_series.setdefault(count.key, []).append(count)

    steps: list[QuarantineStep] = []
    for series in by_series.values():
        series.sort(key=lambda count: count.trade_date)
        for index, count in enumerate(series):
            if index < window or count.rows < min_rows:
                continue
            history = [previous.rows for previous in series[index - window : index]]
            baseline = Decimal(str(statistics.median(history)))
            if baseline <= 0 or Decimal(count.rows) < baseline * multiple:
                continue
            steps.append(
                QuarantineStep(
                    trade_date=count.trade_date,
                    exchange=count.exchange,
                    reason=count.reason,
                    rows=count.rows,
                    baseline=baseline,
                    multiple=(Decimal(count.rows) / baseline).quantize(Decimal("0.1")),
                )
            )
    steps.sort(key=lambda step: (step.trade_date, step.exchange, step.reason))
    return tuple(steps)


def _partition_date(path: Path) -> date:
    """The session a hive partition path is for (`…/date=2021-02-16/part.parquet`)."""
    return date.fromisoformat(path.parent.name.removeprefix("date="))


def totals_by_reason(counts: Sequence[QuarantineCount]) -> Mapping[str, int]:
    """Rows per reason — the shape the EOD report and the status endpoint both want."""
    out: dict[str, int] = {}
    for count in counts:
        out[count.reason] = out.get(count.reason, 0) + count.rows
    return out
