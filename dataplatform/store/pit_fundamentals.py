"""The true point-in-time fundamentals store (M7.3, §4.2 L1) — filings, tagged and never restated.

This is the legitimate, backtest-safe half of the fundamentals waterfall: values parsed from the
exchange's own results filings, each tagged with the `filing_date` on which it first became knowable
(§4.1, invariant #7). It is physically and by name separate from the Screener *restated* store
(M7.1), which is monitoring-only and quarantined from backtests (invariant #8) — the two never share
a dataset root, so a query pointed at this dataset cannot reach restated numbers by accident.

Two design decisions carry the point-in-time and restatement guarantees:

* **Partitioned by `filing_date`, not by period end.** A partition holds exactly the facts that
  became knowable on one date, so `read_pit(on_date)` answers "what did we know then" by *choosing
  partitions* — a quarter filed after the as-of date is physically absent from the result, rather
  than filtered out by a `WHERE` clause a caller might forget. That is invariant #7 made structural.
* **A restatement is a new record, never an overwrite (acceptance 3).** A later filing that restates
  an earlier period carries a later `filing_date` and a distinct `filing_id`, so it lands in a
  different partition (or, if filed the same day, a distinct row keyed by `filing_id`) and both
  versions coexist forever. `read_pit` returns every version knowable as of a date; `read_latest`
  collapses to the most-recently-filed value per `(isin, period_start, period_end, nature,
  concept, segment)` for a consumer that wants the current best knowledge — without the store
  having discarded history. The period *start* belongs in that key: a December-year-end company's
  fourth quarter and its full year end on the same day and are separately announced, so without it
  one would masquerade as a restatement of the other.

This store is *never* backfilled from a **restated** source: a value that was restated has lost the
number the market originally saw, and writing that into the PIT store would fabricate a
knowable-date the fact never had. That remains a defect, not a feature — invariant #8.

Backfilling from the *filings* path is a different thing and is legitimate. The announcements index
filters on **broadcast** date and serves roughly a decade of it, so every historical filing carries
its own genuine first-knowable timestamp; replaying that history forwards is not fabricating a
knowable date, it is reading the one the exchange stamped. The depth is therefore not "since M7.3
went live" but "as far back as the exchange still serves a document" — about FY2018-19, the two
earlier years being index entries with no XBRL attached — see
`ops/gates/M10-fundamentals-backfill-live.md`.

Money is `Decimal`: the parquet schema stores values as `decimal128`, and a float never touches the
value on the way in or out.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import pyarrow as pa
import pyarrow.parquet as pq

from dataplatform.ingest.xbrl.models import Filing, FundamentalFact, Nature, Taxonomy
from dataplatform.logging import get_logger
from dataplatform.store.paths import Layer, l1_partition_path, layer_root, partition_date_of

__all__ = [
    "PIT_FUNDAMENTALS_DATASET",
    "PitFundamentalsBatch",
    "read_l1",
    "read_latest",
    "read_pit",
    "write_pit",
]

_LOG = get_logger(__name__)

#: The L1 dataset name. Partitioned by `filing_date` (the knowable date):
#: `data/L1/pit_fundamentals/date=YYYY-MM-DD/part.parquet`. The name is distinct from the restated
#: store's root (M7.1) precisely so the two cannot be confused for one another.
PIT_FUNDAMENTALS_DATASET: Final = "pit_fundamentals"

#: Values are stored to two decimal places in a wide precision: revenues run to lakhs/crores of
#: rupees while EPS is a few rupees, and 38 digits comfortably holds both. A source that started
#: stating a third decimal would fail the write loudly rather than have a value silently rounded.
#: Two decimal places is not enough, and the shortfall was silent: `pyarrow` refuses to rescale a
#: `Decimal` that would lose data, so a filing reporting EPS to three places failed its *write* with
#: `ArrowInvalid` after parsing perfectly — 250 filings of a decade-long campaign, invisible until
#: the failure classes were tallied. Measured over 3,000 captured documents, the only concepts ever
#: carrying more than two places are `eps_basic`/`eps_diluted`, always at exactly three (0.9% of
#: documents). Scale 4 holds those exactly with headroom, and 34 integer digits is far more than the
#: largest value in the corpus needs. Rounding instead was the wrong fix: an EPS of 1.234 truncated
#: to 1.23 is a 0.3% error in the denominator of every P/E built on it, and CLAUDE.md keeps money
#: exact precisely so a number is never quietly degraded on the way into storage.
_VALUE_TYPE: Final = pa.decimal128(38, 4)

#: The L1 schema, declared once and enforced on write and on read (§4.2, M1.8's rule).
_L1_SCHEMA: Final = pa.schema(
    [
        pa.field("isin", pa.string(), nullable=False),
        pa.field("period_start", pa.date32(), nullable=True),
        pa.field("period_end", pa.date32(), nullable=False),
        pa.field("filing_date", pa.date32(), nullable=False),
        pa.field("nature", pa.string(), nullable=False),
        # Which taxonomy family the filing was prepared against. Not decoration: a bank's
        # `revenue_from_operations` is `InterestEarned` and its `total_expenses` excludes provisions
        # and contingencies, so a screen that ranks banks beside manufacturers on one concept key is
        # comparing two different measurements. Carried on the row so that is visible where the
        # number is read, instead of requiring a join back to the filing to find out.
        pa.field("taxonomy", pa.string(), nullable=False),
        pa.field("filing_id", pa.string(), nullable=False),
        pa.field("concept", pa.string(), nullable=False),
        pa.field("segment", pa.string(), nullable=True),
        pa.field("value", _VALUE_TYPE, nullable=False),
        # Whether the parser computed this value or read it from an element. A derived number can be
        # wrong while both its inputs were read correctly, so an audit has to be able to separate
        # the two populations — and `shares_outstanding` in particular is only published when the
        # filing's own EPS corroborates it, which is a fact about the row worth keeping.
        pa.field("derived", pa.bool_(), nullable=False),
        pa.field("source", pa.string(), nullable=False),
        pa.field("l0_key", pa.string(), nullable=True),
    ]
)

#: What makes one fact distinct from another *within a filing partition*: the same period reported
#: for two different filings (a restatement) is kept apart by `filing_id`, and the same concept for
#: the parent and the group is kept apart by `nature`.
_FACT_KEY = ("isin", "period_end", "nature", "filing_id", "concept", "segment")


def write_pit(filing: Filing, *, data_root: Path | None = None) -> Path:
    """Write one filing's facts to L1, into its `filing_date` partition, and return the path.

    Restatement-safe by construction: a later filing restating the same period has a later
    `filing_date` (a different partition) or, if filed the same day, a distinct `filing_id` (a
    distinct row), so this never overwrites an earlier filing's facts. When a partition already
    holds facts for *other* filings dated that day, they are merged and rewritten together;
    re-writing the *same* filing is idempotent. Refuses to merge two different values for the
    identical fact key — that is a corrupt input, not a restatement.

    Rows within a partition are sorted by the fact key so a re-derivation is byte-identical (the
    M1.5 determinism rule), and the file is written whole to a temporary name then renamed over the
    target so a crash mid-write cannot leave a half file readable.

    One filing per write is the right shape for the daily path, where one filing arrives and the
    checkpoint behind it is per filing. Re-deriving the whole store is the other shape, and pays
    O(n²) row-writes per partition for it — `PitFundamentalsBatch` is that path's entry point.
    """
    path = l1_partition_path(PIT_FUNDAMENTALS_DATASET, filing.filing_date, data_root=data_root)
    path.parent.mkdir(parents=True, exist_ok=True)

    merged = _existing_rows(path)
    _stage(filing, into=merged, path=path)
    rows = _write_partition(path, merged)

    _LOG.info(
        "pit_fundamentals.written",
        source=filing.source,
        isin=filing.isin,
        period_end=filing.period_end.isoformat(),
        filing_date=filing.filing_date.isoformat(),
        nature=filing.nature.value,
        filing_id=filing.filing_id,
        dataset=PIT_FUNDAMENTALS_DATASET,
        path=str(path),
        rows=rows,
        state="NORMALIZED",
    )
    return path


class PitFundamentalsBatch:
    """A buffered `write_pit`: hold many filings by `filing_date` and write each partition once.

    Why it exists: `write_pit` reads, merges and rewrites a whole partition per filing. That is
    right for the daily path — one filing arrives, one file is rewritten, and the `sync_state`
    checkpoint behind it is per filing, so a killed run loses one filing. It is the wrong shape for
    a re-derivation: a results-season `filing_date` holds several hundred filings, so its partition
    is rewritten several hundred times and the run pays O(n²) row-writes per partition. Measured on
    the real corpus — 68,839 filings over ~1,600 partitions — that is about ninety minutes at ~14
    filings/sec, essentially all of it in the write path, and every schema change forces it again.

    What it does: `add` merges one filing's facts into the buffer for its `filing_date`, applying
    exactly the checks `write_pit` applies — the partition's stored rows are read once, on the
    first `add` that touches it, so a conflicting fact is refused against both the rows on disk and
    the rows already buffered, and the filing that raised is the one named. `flush` writes every
    touched partition once, through the same staging-file rename `write_pit` uses, and empties the
    buffer. The rows written are the same set, sorted the same way, so a partition built this way
    is byte-identical to one built a filing at a time.
    What it assumes: nothing else writes these partitions while the buffer is open — the buffer's
    view of a partition is the snapshot it read on first touch. That holds for the re-derivation
    this exists for, which is a single process over a store it is rebuilding.
    What it never does: touch the filesystem before `flush` (beyond reading a partition it is about
    to buffer), or leave half a refused filing behind — `add` validates all of a filing's facts
    before applying any of them, so a filing that raises leaves the buffer exactly as it was.

    **A buffered fact is not a published fact.** Facts live only in memory between `add` and
    `flush`, so a caller that checkpoints must defer its checkpoint to after `flush` returns —
    a checkpoint claiming PUBLISHED for a buffered filing would be a lie a crash makes permanent.
    `FundamentalsBackfillRunner` does exactly that, and is the only caller.
    """

    def __init__(self, *, data_root: Path | None = None) -> None:
        self._data_root = data_root
        #: `filing_date` → that partition's rows by fact key: the rows read from disk on first
        #: touch, plus every fact buffered for it since. Insertion-ordered, but the write sorts by
        #: the fact key regardless, which is what keeps the output independent of `add` order.
        self._partitions: dict[date, dict[tuple[Any, ...], FundamentalFact]] = {}
        self._filings = 0
        self._buffered_facts = 0

    @property
    def pending_filings(self) -> int:
        """Filings `add`ed since the last `flush` — none of whose facts are on disk yet."""
        return self._filings

    @property
    def pending_facts(self) -> int:
        """Facts `add`ed since the last `flush`, counting a re-derived fact once per `add`."""
        return self._buffered_facts

    @property
    def pending_partitions(self) -> int:
        """Distinct `filing_date` partitions the buffer will rewrite on the next `flush`."""
        return len(self._partitions)

    def add(self, filing: Filing) -> None:
        """Buffer one filing's facts for its `filing_date` partition, refusing a conflict loudly.

        Raises `ValueError` on a fact whose key is already held with a different value (the same
        refusal `write_pit` makes: L0 is immutable, so a stable parse must be stable), and on a
        value the L1 decimal column cannot hold. Neither leaves anything behind.
        """
        path = l1_partition_path(
            PIT_FUNDAMENTALS_DATASET, filing.filing_date, data_root=self._data_root
        )
        partition = self._partitions.get(filing.filing_date)
        if partition is None:
            partition = _existing_rows(path)
            self._partitions[filing.filing_date] = partition
        _reject_unwritable_values(filing)
        _stage(filing, into=partition, path=path)
        self._filings += 1
        self._buffered_facts += len(filing.facts)

    def flush(self) -> tuple[Path, ...]:
        """Write every buffered partition once, empty the buffer, and return the paths written.

        Ordered by `filing_date` so a run's writes are deterministic in sequence as well as in
        content. Only after this returns may a caller checkpoint the filings it `add`ed.
        """
        written: list[Path] = []
        for filing_date in sorted(self._partitions):
            partition = self._partitions[filing_date]
            path = l1_partition_path(
                PIT_FUNDAMENTALS_DATASET, filing_date, data_root=self._data_root
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            rows = _write_partition(path, partition)
            written.append(path)
            _LOG.info(
                "pit_fundamentals.partition_written",
                filing_date=filing_date.isoformat(),
                dataset=PIT_FUNDAMENTALS_DATASET,
                path=str(path),
                rows=rows,
                state="NORMALIZED",
            )
        _LOG.info(
            "pit_fundamentals.flushed",
            partitions=len(written),
            filings=self._filings,
            facts=self._buffered_facts,
            dataset=PIT_FUNDAMENTALS_DATASET,
            state="NORMALIZED",
        )
        self._partitions.clear()
        self._filings = 0
        self._buffered_facts = 0
        return tuple(written)


def read_l1(filing_date: date, *, data_root: Path | None = None) -> tuple[FundamentalFact, ...]:
    """Read one filing-date partition back out of L1.

    Raises `FileNotFoundError` when the partition was never written — an absent partition is a gap
    for D7 to explain, not an empty filing date.
    """
    path = l1_partition_path(PIT_FUNDAMENTALS_DATASET, filing_date, data_root=data_root)
    if not path.exists():
        raise FileNotFoundError(
            f"no {PIT_FUNDAMENTALS_DATASET} partition for {filing_date.isoformat()}: {path}"
        )
    return _rows_of(path)


def read_pit(on_date: date, *, data_root: Path | None = None) -> tuple[FundamentalFact, ...]:
    """Every fundamental fact knowable on `on_date` — the point-in-time query (invariant #7).

    What it does: reads only the partitions whose `filing_date` is on or before `on_date`, so a
    filing disseminated *after* that date is physically absent from the result. A period restated by
    a later filing appears once per filing made on or before `on_date` — both the original and any
    restatement the market had seen by then, because that is exactly what was knowable.
    What it assumes: `on_date` is the decision date in Asia/Kolkata.
    What it never does: return a fact filed after `on_date`, or invent a partition — an as-of date
    before the earliest filing yields an empty result, not an error.

    Rows are returned sorted by the fact key then `filing_date`.
    """
    rows: list[FundamentalFact] = []
    for path in _partitions_through(on_date, data_root=data_root):
        rows.extend(_rows_of(path))
    rows.sort(key=lambda fact: (*_key_of(fact), fact.filing_date))
    return tuple(rows)


def read_latest(on_date: date, *, data_root: Path | None = None) -> tuple[FundamentalFact, ...]:
    """The most-recently-filed value per fact, among filings knowable on `on_date`.

    Collapses restatements to the best knowledge as of the as-of date — for each
    `(isin, period_start, period_end, nature, concept, segment)`, the fact from the latest
    `filing_date` on or before `on_date` — without the store having discarded the earlier version
    (which `read_pit` still returns). Two facts differing only in `period_start` are two periods,
    not two versions of one, and both survive. This is the read a break-condition evaluator uses;
    it never reaches back past `on_date`, so a restatement the market had not yet seen cannot
    change a historical decision.
    """
    latest: dict[tuple[Any, ...], FundamentalFact] = {}
    for fact in read_pit(on_date, data_root=data_root):
        key = _key_without_filing(fact)
        current = latest.get(key)
        if current is None or fact.filing_date > current.filing_date:
            latest[key] = fact
    return tuple(sorted(latest.values(), key=_key_without_filing))


def _partitions_through(on_date: date, *, data_root: Path | None) -> Iterator[Path]:
    """The `part.parquet` files whose `filing_date` partition is on or before `on_date`."""
    dataset_dir = layer_root(Layer.L1, data_root=data_root) / PIT_FUNDAMENTALS_DATASET
    if not dataset_dir.is_dir():
        return
    for partition_dir in sorted(dataset_dir.iterdir()):
        if not partition_dir.is_dir():
            continue
        try:
            filing_date = partition_date_of(partition_dir)
        except ValueError:
            continue
        if filing_date <= on_date:
            path = partition_dir / "part.parquet"
            if path.exists():
                yield path


def _existing_rows(path: Path) -> dict[tuple[Any, ...], FundamentalFact]:
    """A partition's stored rows by fact key, or an empty map when it was never written."""
    if not path.exists():
        return {}
    return {_key_of(row): row for row in _rows_of(path)}


def _stage(filing: Filing, *, into: dict[tuple[Any, ...], FundamentalFact], path: Path) -> None:
    """Merge one filing's facts into a partition's rows, refusing a conflicting value.

    `into` is the partition as the writer currently sees it — the rows read from disk, plus
    anything a batch has already buffered for it — so a fact key coming round twice is checked
    against the earlier copy wherever it came from.

    The facts are validated against a private staging map first and applied only once all of them
    pass. `write_pit` gets that for free by discarding its local map on the way out; a batch does
    not, and half a refused filing left in a buffer would reach a parquet file on the next flush.
    """
    staged: dict[tuple[Any, ...], FundamentalFact] = {}
    for fact in filing.facts:
        key = _key_of(fact)
        prior = staged.get(key)
        if prior is None:
            prior = into.get(key)
        if prior is not None and prior.filing_id != fact.filing_id:
            # Two filings claiming the identical fact key on one day is impossible — filing_id is
            # in the key — so this is a same-filing re-derivation; a value change is corruption.
            raise ValueError(
                f"conflicting value for {key} in partition {path}: {prior.value} vs {fact.value}"
            )
        if prior is not None and prior.value != fact.value:
            raise ValueError(
                f"re-deriving {key} produced a different value ({prior.value} → {fact.value}); L0 "
                "is immutable, so a stable parse must be stable"
            )
        staged[key] = fact
    into.update(staged)


def _reject_unwritable_values(filing: Filing) -> None:
    """Refuse a filing whose values the L1 decimal column cannot hold, before it is buffered.

    `pa.Table.from_pylist` is where an out-of-scale `Decimal` is caught, and in the one-filing-at-
    a-time path that fails exactly the filing that carried it — which is how the three-decimal EPS
    era was eventually found. Buffering moves that conversion to the flush, where it would take a
    whole partition's filings down with it, so the same conversion runs here over this filing's
    values alone and the blast radius stays one filing wide.
    """
    try:
        pa.array([fact.value for fact in filing.facts], type=_VALUE_TYPE)
    except pa.ArrowInvalid as exc:
        raise ValueError(
            f"{filing.isin} {filing.period_end.isoformat()} ({filing.filing_id}): a value does not "
            f"fit the L1 {_VALUE_TYPE} column: {exc}"
        ) from exc


def _write_partition(path: Path, rows: dict[tuple[Any, ...], FundamentalFact]) -> int:
    """Write one partition whole and return its row count.

    Sorted by the fact key so a re-derivation is byte-identical (the M1.5 determinism rule), and
    written to a temporary name then renamed over the target so a crash mid-write cannot leave a
    half file readable.
    """
    ordered = sorted(rows.values(), key=_key_of)
    table = pa.Table.from_pylist([_to_record(row) for row in ordered], schema=_L1_SCHEMA)
    staging = path.with_name(f".{path.name}.partial")
    pq.write_table(table, staging, compression="snappy", version="2.6")
    staging.replace(path)
    return len(ordered)


def _rows_of(path: Path) -> tuple[FundamentalFact, ...]:
    """Parse one L1 partition file into facts, enforcing the declared schema on read."""
    records = pq.read_table(path, schema=_L1_SCHEMA).to_pylist()
    return tuple(
        FundamentalFact(
            isin=str(record["isin"]),
            period_start=record["period_start"],
            period_end=record["period_end"],
            filing_date=record["filing_date"],
            nature=Nature(record["nature"]),
            taxonomy=Taxonomy(record["taxonomy"]),
            filing_id=str(record["filing_id"]),
            concept=str(record["concept"]),
            segment=None if record["segment"] is None else str(record["segment"]),
            value=_as_decimal(record["value"]),
            derived=bool(record["derived"]),
            source=str(record["source"]),
            l0_key=None if record["l0_key"] is None else str(record["l0_key"]),
        )
        for record in records
    )


def _as_decimal(value: Any) -> Decimal:
    """A parquet decimal comes back as `Decimal` already; guard the money invariant explicitly."""
    if not isinstance(value, Decimal):  # pragma: no cover - schema guarantees Decimal
        raise TypeError(f"value read from L1 is {type(value).__name__}, not Decimal")
    return value


def _to_record(fact: FundamentalFact) -> dict[str, Any]:
    """One fact as the dict `pa.Table.from_pylist` writes against `_L1_SCHEMA`."""
    return {
        "isin": fact.isin,
        "period_start": fact.period_start,
        "period_end": fact.period_end,
        "filing_date": fact.filing_date,
        "nature": fact.nature.value,
        "taxonomy": fact.taxonomy.value,
        "filing_id": fact.filing_id,
        "concept": fact.concept,
        "segment": fact.segment,
        "value": fact.value,
        "derived": fact.derived,
        "source": fact.source,
        "l0_key": fact.l0_key,
    }


def _key_of(fact: FundamentalFact) -> tuple[Any, ...]:
    """The within-partition identity of a fact (`_FACT_KEY`)."""
    return (
        fact.isin,
        fact.period_end,
        fact.nature.value,
        fact.filing_id,
        fact.concept,
        fact.segment or "",
    )


def _key_without_filing(fact: FundamentalFact) -> tuple[Any, ...]:
    """The identity of a fact *across* filings — the key a restatement supersedes.

    `period_start` is part of it, not decoration. A results document reports each concept for
    several periods that can share an end date — a December-year-end company's fourth quarter and
    its full year both end 31-Dec — and each is named by its own announcement, so both land in the
    store legitimately. Keyed on `period_end` alone they look like two versions of one fact, and
    `read_latest` would drop the annual figure (or the quarterly one, on filing-date order) rather
    than return both. The period a number describes is part of what the number *is*.
    """
    return (
        fact.isin,
        fact.period_end,
        fact.period_start or date.min,
        fact.nature.value,
        fact.concept,
        fact.segment or "",
    )
