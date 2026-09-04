"""The point-in-time macro store (M11.1, §4.2 L1) — economic series as they were published.

The direct counterpart of `pit_fundamentals`, and deliberately the same shape, because it solves the
same problem one level up: a figure describes a period but becomes knowable on a *later* date, and
joining on the period instead of the release is look-ahead bias that looks entirely plausible.
April CPI released on 12 May was not available to a decision taken on 5 May.

Two design decisions carry the guarantees, both lifted straight from the fundamentals store:

* **Partitioned by `release_date`, not by period.** A partition holds exactly what became knowable
  on one date, so `read_pit(on_date)` answers "what did we know then" by *choosing partitions* — a
  release made after the as-of date is physically absent from the result rather than filtered out by
  a `WHERE` clause a caller might forget. Invariant #7 made structural.
* **A revision is a new record, never an overwrite.** India's IIP and GDP are revised routinely. A
  revision carries a later `release_date` (a different partition) or, if republished the same day, a
  higher `revision_seq` — so both versions coexist forever. `read_pit` returns every version
  knowable as of a date; `read_latest` collapses to the best knowledge then, without the store
  having discarded history. Overwriting would destroy the number the market actually saw, which is
  the fabrication invariant #8 forbids for restated fundamentals.

**What this store never holds is a regime, a label or a tag.** Those are derived and belong in L2,
rebuildable from these primitives and carrying the version of the rule that produced them. A stored
label you cannot reconstruct from its inputs cannot answer the only question that matters when it
changes — did the world move, or did the threshold? — which is the argument
`ops/gates/M10-data-gap-plan.md` already makes about filer-stated ratios.

Values are `Decimal` throughout: `decimal128(38, 6)`, wider in scale than the money store because a
yield, a rate and an index level need more than two places while still fitting a P/E and a crore.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import pyarrow as pa
import pyarrow.parquet as pq

from dataplatform.ingest.macro.models import Frequency, MacroFact, MacroRelease, Unit
from dataplatform.logging import get_logger
from dataplatform.store.paths import Layer, l1_partition_path, layer_root, partition_date_of

__all__ = [
    "MACRO_SERIES_DATASET",
    "read_l1",
    "read_latest",
    "read_pit",
    "series_history",
    "write_release",
]

_LOG = get_logger(__name__)

#: The L1 dataset name. Partitioned by `release_date` (the knowable date):
#: `data/L1/macro_series/date=YYYY-MM-DD/part.parquet`.
MACRO_SERIES_DATASET: Final = "macro_series"

#: Six decimal places: a P/E needs two, a yield two, a policy rate two, but a rebased index level
#: and an FX rate need more. A source that started stating a seventh fails the write loudly rather
#: than having a value silently rounded.
_VALUE_TYPE: Final = pa.decimal128(38, 6)

_L1_SCHEMA: Final = pa.schema(
    [
        pa.field("series_id", pa.string(), nullable=False),
        pa.field("period_start", pa.date32(), nullable=True),
        pa.field("period_end", pa.date32(), nullable=False),
        pa.field("release_date", pa.date32(), nullable=False),
        pa.field("frequency", pa.string(), nullable=False),
        pa.field("unit", pa.string(), nullable=False),
        pa.field("value", _VALUE_TYPE, nullable=False),
        pa.field("revision_seq", pa.int32(), nullable=False),
        pa.field("source", pa.string(), nullable=False),
        pa.field("l0_key", pa.string(), nullable=True),
    ]
)

#: What makes one fact distinct *within a release partition*: the same series and period published
#: twice on one day is a same-day revision, kept apart by `revision_seq`.
_FACT_KEY = ("series_id", "period_start", "period_end", "revision_seq")

#: What identifies one observation across releases — the key `read_latest` collapses on. Excludes
#: `revision_seq` and the release date, which are exactly what distinguishes versions of it.
_OBSERVATION_KEY = ("series_id", "period_start", "period_end")


def write_release(release: MacroRelease, *, data_root: Path | None = None) -> Path:
    """Write one release's facts into its `release_date` partition, and return the path.

    Revision-safe by construction: a later release of the same period lands in a later partition, or
    — republished the same day — under a higher `revision_seq`, so this never overwrites an earlier
    number. A partition already holding facts from other releases that day is merged and rewritten
    together; re-writing the same release is idempotent.

    Refuses to merge two different values under one fact key: L0 is immutable, so a re-derivation
    that produces a different number is a parser regression, not a revision, and it fails loudly
    here rather than silently changing history.

    Rows are sorted by the fact key so a re-derivation is byte-identical (the M1.5 determinism
    rule), and the file is written whole to a temporary name then renamed over the target, so a
    crash mid-write cannot leave a half file readable.
    """
    path = l1_partition_path(MACRO_SERIES_DATASET, release.release_date, data_root=data_root)
    path.parent.mkdir(parents=True, exist_ok=True)

    merged: dict[tuple[Any, ...], MacroFact] = {}
    if path.exists():
        for existing in _rows_of(path):
            merged[_key_of(existing)] = existing
    for fact in release.facts:
        key = _key_of(fact)
        prior = merged.get(key)
        if prior is not None and prior.value != fact.value:
            raise ValueError(
                f"re-deriving {key} in partition {path} produced a different value "
                f"({prior.value} → {fact.value}); L0 is immutable, so a stable parse must be "
                "stable. A genuine revision carries a later release_date or a higher revision_seq"
            )
        merged[key] = fact

    rows = sorted(merged.values(), key=_key_of)
    table = pa.Table.from_pylist([_to_record(row) for row in rows], schema=_L1_SCHEMA)
    staging = path.with_name(f".{path.name}.partial")
    pq.write_table(table, staging, compression="snappy", version="2.6")
    staging.replace(path)

    _LOG.info(
        "macro_series.written",
        source=release.source,
        release_date=release.release_date.isoformat(),
        dataset=MACRO_SERIES_DATASET,
        path=str(path),
        facts=len(release.facts),
        rows=len(rows),
        state="NORMALIZED",
    )
    return path


def read_l1(release_date: date, *, data_root: Path | None = None) -> tuple[MacroFact, ...]:
    """Read one release-date partition back out of L1.

    Raises `FileNotFoundError` when the partition was never written — an absent partition is a gap
    for D7 to explain, not a day on which nothing was published.
    """
    path = l1_partition_path(MACRO_SERIES_DATASET, release_date, data_root=data_root)
    if not path.exists():
        raise FileNotFoundError(
            f"no {MACRO_SERIES_DATASET} partition for {release_date.isoformat()}: {path}"
        )
    return _rows_of(path)


def read_pit(on_date: date, *, data_root: Path | None = None) -> tuple[MacroFact, ...]:
    """Every macro fact knowable on `on_date` — the point-in-time query (invariant #7).

    What it does: reads only the partitions whose `release_date` is on or before `on_date`, so a
    figure published afterwards is physically absent. A period revised by a later release appears
    once per release made on or before `on_date` — the original and any revision the market had
    seen by then, because that is exactly what was knowable.
    What it assumes: `on_date` is the decision date in Asia/Kolkata.
    What it never does: return a fact released after `on_date`, or invent a partition — an as-of
    date before the earliest release yields an empty result, not an error.
    """
    rows: list[MacroFact] = []
    for path in _partitions_through(on_date, data_root=data_root):
        rows.extend(_rows_of(path))
    rows.sort(key=lambda fact: (*_key_of(fact), fact.release_date))
    return tuple(rows)


def read_latest(
    on_date: date, *, series: Iterable[str] | None = None, data_root: Path | None = None
) -> tuple[MacroFact, ...]:
    """The most-recently-released value per observation, among releases knowable on `on_date`.

    Collapses revisions to the best knowledge as of the as-of date — for each
    `(series_id, period_start, period_end)`, the fact from the latest `release_date` on or before
    `on_date`, breaking a same-day tie on the higher `revision_seq` — without the store having
    discarded the earlier version, which `read_pit` still returns.

    `series` narrows to a set of `series_id`s. This is the read a signal uses; it never reaches past
    `on_date`, so a revision the market had not yet seen cannot change a historical decision.
    """
    wanted = None if series is None else set(series)
    latest: dict[tuple[Any, ...], MacroFact] = {}
    for fact in read_pit(on_date, data_root=data_root):
        if wanted is not None and fact.series_id not in wanted:
            continue
        key = _observation_of(fact)
        current = latest.get(key)
        if current is None or (fact.release_date, fact.revision_seq) > (
            current.release_date,
            current.revision_seq,
        ):
            latest[key] = fact
    return tuple(sorted(latest.values(), key=_observation_of))


def series_history(
    series_id: str, on_date: date, *, data_root: Path | None = None
) -> tuple[MacroFact, ...]:
    """One series as it stood on `on_date`, oldest period first — the shape a signal reads.

    The as-of-date walk a level or trend needs: every period of `series_id` that had been published
    by `on_date`, each at its then-latest revision, ordered by the period it describes. A
    series that
    had not started publishing by `on_date` returns empty.
    """
    facts = read_latest(on_date, series=(series_id,), data_root=data_root)
    return tuple(sorted(facts, key=lambda fact: fact.period_end))


def _partitions_through(on_date: date, *, data_root: Path | None) -> Iterator[Path]:
    """The `part.parquet` files whose `release_date` partition is on or before `on_date`."""
    dataset_dir = layer_root(Layer.L1, data_root=data_root) / MACRO_SERIES_DATASET
    if not dataset_dir.is_dir():
        return
    for partition_dir in sorted(dataset_dir.iterdir()):
        if not partition_dir.is_dir():
            continue
        try:
            release_date = partition_date_of(partition_dir)
        except ValueError:
            continue
        if release_date <= on_date:
            path = partition_dir / "part.parquet"
            if path.exists():
                yield path


def _rows_of(path: Path) -> tuple[MacroFact, ...]:
    """Parse one L1 partition file into facts, enforcing the declared schema on read."""
    records = pq.read_table(path, schema=_L1_SCHEMA).to_pylist()
    return tuple(
        MacroFact(
            series_id=str(record["series_id"]),
            period_start=record["period_start"],
            period_end=record["period_end"],
            release_date=record["release_date"],
            frequency=Frequency(record["frequency"]),
            unit=Unit(record["unit"]),
            value=_as_decimal(record["value"]),
            revision_seq=int(record["revision_seq"]),
            source=str(record["source"]),
            l0_key=None if record["l0_key"] is None else str(record["l0_key"]),
        )
        for record in records
    )


def _observation_of(fact: MacroFact) -> tuple[Any, ...]:
    """What identifies one observation across releases — the key `read_latest` collapses on.

    `period_start` is nullable and `None` will not sort against a `date`, so an absent start reads
    as `date.min` here. That is a sort convention only: it never reaches the stored row.
    """
    return (fact.series_id, fact.period_start or date.min, fact.period_end)


def _key_of(fact: MacroFact) -> tuple[Any, ...]:
    """The within-partition identity of one fact. `period_start` is nullable; sort it stably."""
    return (
        fact.series_id,
        fact.period_start or date.min,
        fact.period_end,
        fact.revision_seq,
    )


def _as_decimal(value: Any) -> Decimal:
    """A parquet decimal comes back as `Decimal` already; guard the money invariant explicitly."""
    if not isinstance(value, Decimal):  # pragma: no cover - schema guarantees Decimal
        raise TypeError(f"value read from L1 is {type(value).__name__}, not Decimal")
    return value


def _to_record(fact: MacroFact) -> dict[str, Any]:
    """One fact as the dict `pa.Table.from_pylist` writes against `_L1_SCHEMA`."""
    return {
        "series_id": fact.series_id,
        "period_start": fact.period_start,
        "period_end": fact.period_end,
        "release_date": fact.release_date,
        "frequency": fact.frequency.value,
        "unit": fact.unit.value,
        "value": fact.value,
        "revision_seq": fact.revision_seq,
        "source": fact.source,
        "l0_key": fact.l0_key,
    }
