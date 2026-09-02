"""The RESTATED store — a physically separate lake for monitoring-only restated data (M7.1).

Screener's restated fundamentals must never reach a backtest or a decision (decision #7, invariant
#8). The plan's word for "never" here is *physical*: not a flag on a row that a query could forget
to filter, but a different Parquet root and a different reader, so a PIT/backtest context has no
path to this data even if it tries (the deliberate-attempt test is M7.2). This module is that
separate root and that separate reader.

The separation is structural, not cosmetic:

* **A different root.** Everything here lives under `<data_root>/RESTATED/…`, never under `L0`, `L1`
  or `L2`. `restated_root` is the only place that name is formed, and `paths.py` — the module every
  PIT reader builds its paths through — cannot produce a path under it. The two trees do not
  overlap, which `tests/unit/test_screener.py` asserts.
* **A different row type.** A `RestatedFundamental` is not a `PriceRow` and carries no OHLC; it
  carries a restated figure, its statement/metric/period, the `screener_restated` source tag, and
  the L0 lineage it was derived from. Both the tag and the lineage are stamped from the `L0Ref` at
  write time (`build`), so neither can be forged or forgotten.
* **Partitioned by ISIN.** Monitoring reads one company across its history (§4.5 for the PIT side,
  and the same access shape here), so the partition is the ISIN — the only join key (invariant #2).
  A restated datum with no ISIN is not written; it is resolved through the D2 identity master first
  (`build`), and an unresolved one is quarantined and counted, never dropped.

What this module never does: expose a reader that a backtest could import by accident (that is
M7.2's structural guarantee), store an adjusted price, or resolve a symbol by name alone.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Final, Literal

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import BaseModel, ConfigDict, Field, field_validator

from dataplatform.config import get_settings
from dataplatform.identity.master import AmbiguousSymbolError, Exchange, IdentityMaster
from dataplatform.ingest.models import ISIN_PATTERN
from dataplatform.ingest.screener import SCREENER_SOURCE_ID, SCREENER_SOURCE_TAG, ScreenerDatum
from dataplatform.logging import get_logger
from dataplatform.store.l0 import L0Ref
from dataplatform.store.paths import DEFAULT_PART_FILENAME, PathLayoutError

__all__ = [
    "RESTATED_ROOT_NAME",
    "SCREENER_FUNDAMENTALS_DATASET",
    "RestatedBuild",
    "RestatedFundamental",
    "RestatedStore",
    "build",
    "restated_dataset_dir",
    "restated_isin_partition_path",
    "restated_root",
]

_LOG = get_logger(__name__)

#: The top-level directory that quarantines restated data, a sibling of `L0`/`L1`/`L2` and never a
#: child of any of them. Upper-case like the PIT layers, and the *only* literal of this name in the
#: codebase — a second one would be a second root the quarantine does not cover.
RESTATED_ROOT_NAME: Final = "RESTATED"

#: The one dataset this store holds today: Screener per-company fundamentals.
SCREENER_FUNDAMENTALS_DATASET: Final = "screener_fundamentals"

#: A lake identifier for the dataset directory — same rule as `paths.py` (lower-case, no seps).
_IDENTIFIER: Final = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_ISIN: Final = re.compile(ISIN_PATTERN)

#: Restated figures are quantised to two places so a re-derivation is byte-identical (as L1 does).
_VALUE_Q: Final = Decimal("0.01")


class RestatedFundamental(BaseModel):
    """One restated figure, ISIN-keyed, tagged and carrying the L0 lineage it was derived from.

    What it does: carry a single restated cell — statement, metric, period and value — for one ISIN,
    with the `screener_restated` tag and the full identity of the L0 payload it came from.
    What it assumes: the value was resolved to an ISIN through the D2 identity master (`build` is
    the only sanctioned path), and the lineage fields were copied off a real `L0Ref`.
    What it never does: hold a PIT/adjusted figure, a float, or a knowable date (restated data has
    none). `source` is fixed to the one tag; `l0_sha256` ties the row to the exact immutable bytes
    it derives from, so invariant #1's "re-derivable from L0 alone" holds for this store too.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    isin: str = Field(pattern=ISIN_PATTERN, description="the only join key (invariant #2)")
    symbol: str = Field(min_length=1, description="Screener slug the figure was published under")
    statement: str = Field(
        min_length=1, description="statement the cell came from, e.g. profit_loss"
    )
    metric: str = Field(min_length=1, description="row label, verbatim")
    period: str = Field(min_length=1, description="column label, verbatim, e.g. 'Mar 2024'")
    value: Decimal = Field(
        strict=True, allow_inf_nan=False, description="the restated figure; may be negative"
    )
    source: Literal["screener_restated"] = Field(
        description="the source tag (invariant #8 / acceptance #3); no other value is legal here"
    )
    l0_source: str = Field(min_length=1, description="L0 lineage: the source id of the raw payload")
    l0_filename: str = Field(min_length=1, description="L0 lineage: the payload's own filename")
    l0_logical_date: date = Field(description="L0 lineage: the date the payload is about")
    l0_sha256: str = Field(
        pattern=r"^[0-9a-f]{64}$", description="L0 lineage: sha256 of the exact bytes derived from"
    )
    fetched_at: datetime = Field(
        description="tz-aware instant the payload was fetched (from L0Ref)"
    )

    @field_validator("fetched_at")
    @classmethod
    def _must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError(f"fetched_at must be tz-aware, got naive {value.isoformat()!r}")
        return value

    @property
    def l0_key(self) -> str:
        """The L0 write-once key this row derives from — `source/date/filename`."""
        return f"{self.l0_source}/{self.l0_logical_date.isoformat()}/{self.l0_filename}"


@dataclass(frozen=True, slots=True)
class RestatedBuild:
    """The outcome of turning parsed datums into ISIN-keyed restated rows.

    `unresolved` holds datums whose symbol the identity master could not place as of the lineage
    date — quarantined and counted rather than dropped, so a company the master has not ingested yet
    is a visible gap, not silent loss. An *ambiguous* symbol is not silently quarantined here:
    `IdentityMaster.resolve` raises and queues the conflict, because "we know two ISINs" is a
    different fact from "we know none".
    """

    resolved: tuple[RestatedFundamental, ...]
    unresolved: tuple[ScreenerDatum, ...]


def restated_root(*, data_root: Path | None = None) -> Path:
    """The RESTATED root, a sibling of the PIT layers and never a child of one."""
    root = get_settings().data_root if data_root is None else data_root
    return root / RESTATED_ROOT_NAME


def restated_dataset_dir(dataset: str, *, data_root: Path | None = None) -> Path:
    """The directory holding one restated dataset, e.g. `RESTATED/screener_fundamentals`."""
    return restated_root(data_root=data_root) / _identifier(dataset)


def restated_isin_partition_path(
    dataset: str,
    isin: str,
    *,
    filename: str = DEFAULT_PART_FILENAME,
    data_root: Path | None = None,
) -> Path:
    """The parquet file for one ISIN in a restated dataset — partitioned by ISIN, not by date."""
    return restated_dataset_dir(dataset, data_root=data_root) / f"isin={_isin(isin)}" / filename


def build(
    datums: Iterable[ScreenerDatum],
    *,
    ref: L0Ref,
    master: IdentityMaster,
    as_of: date | None = None,
    exchange: Exchange = Exchange.NSE,
) -> RestatedBuild:
    """Resolve parsed datums to ISINs and stamp each with its source tag and L0 lineage.

    What it does: for each datum, resolves `datum.symbol` to an ISIN as of `as_of` (default: the L0
    payload's logical date) through the D2 identity master — the only sanctioned symbol→ISIN path
    (invariant #2) — and builds a `RestatedFundamental` carrying the `screener_restated` tag and the
    lineage copied off `ref`. Datums whose symbol cannot be placed are returned unresolved.
    What it assumes: `ref` is the L0 payload these datums were parsed from, so its checksum, source
    and date are this data's true lineage.
    What it never does: resolve a symbol by name alone, invent a value, or drop an unresolved datum.
    """
    on_date = ref.logical_date if as_of is None else as_of
    resolved: list[RestatedFundamental] = []
    unresolved: list[ScreenerDatum] = []
    for datum in datums:
        try:
            isin = master.try_resolve(datum.symbol, on_date, exchange=exchange)
        except AmbiguousSymbolError:
            # An ambiguous symbol is a reconciliation conflict the master has already queued; it is
            # not a routine "unknown", so it is left unresolved here and surfaced by the queue.
            unresolved.append(datum)
            continue
        if isin is None:
            unresolved.append(datum)
            continue
        resolved.append(
            RestatedFundamental(
                isin=isin,
                symbol=datum.symbol,
                statement=datum.statement,
                metric=datum.metric,
                period=datum.period,
                value=datum.value,
                source=datum.source,
                l0_source=ref.source,
                l0_filename=ref.filename,
                l0_logical_date=ref.logical_date,
                l0_sha256=ref.sha256,
                fetched_at=ref.fetched_at,
            )
        )
    _LOG.info(
        "restated.built",
        source=SCREENER_SOURCE_ID,
        l0_key=ref.key,
        resolved=len(resolved),
        unresolved=len(unresolved),
        state="RESOLVED",
    )
    return RestatedBuild(resolved=tuple(resolved), unresolved=tuple(unresolved))


_SCHEMA: Final = pa.schema(
    [
        pa.field("isin", pa.string()),
        pa.field("symbol", pa.string()),
        pa.field("statement", pa.string()),
        pa.field("metric", pa.string()),
        pa.field("period", pa.string()),
        # Restated figures span paise to lakhs of crores; a wide fixed decimal keeps them exact.
        pa.field("value", pa.decimal128(30, 2)),
        pa.field("source", pa.string()),
        pa.field("l0_source", pa.string()),
        pa.field("l0_filename", pa.string()),
        pa.field("l0_logical_date", pa.date32()),
        pa.field("l0_sha256", pa.string()),
        pa.field("fetched_at", pa.timestamp("us", tz="Asia/Kolkata")),
    ]
)


class RestatedStore:
    """Write and read the restated lake — the monitoring-only surface, physically apart from PIT.

    What it does: writes `RestatedFundamental`s to `RESTATED/<dataset>/isin=<ISIN>/part.parquet`
    (one file per ISIN, rewritten whole so a partition is never half-written) and reads them back.
    What it assumes: every row it is handed already carries the `screener_restated` tag and complete
    L0 lineage — it re-checks both on write and refuses a batch that does not, so a mis-tagged or
    lineage-less row cannot enter the store.
    What it never does: write under `L0`/`L1`/`L2`, and — by design — offer any reader a PIT or
    backtest context could reach. The structural proof of that unreachability is M7.2; this class is
    the separate root it builds on.
    """

    def __init__(self, *, data_root: Path | None = None) -> None:
        self.data_root = get_settings().data_root if data_root is None else data_root

    def __repr__(self) -> str:
        return f"{type(self).__name__}(root={str(self.root)!r})"

    @property
    def root(self) -> Path:
        """The `RESTATED` directory this store owns."""
        return restated_root(data_root=self.data_root)

    def write(
        self, rows: Sequence[RestatedFundamental], *, dataset: str = SCREENER_FUNDAMENTALS_DATASET
    ) -> tuple[Path, ...]:
        """Write `rows` to their per-ISIN partitions and return the files written, sorted.

        Rows are grouped by ISIN and each ISIN's partition is written whole. Every row's source tag
        and L0 lineage are re-verified first — a defensive check on top of the model, because this
        is the boundary the quarantine's "every datum carries its tag and lineage" (acceptance #3)
        has to hold at. Raises `ValueError` on a mis-tagged row; does nothing for an empty batch.
        """
        if not rows:
            return ()
        by_isin: dict[str, list[RestatedFundamental]] = {}
        for row in rows:
            _check_lineage(row)
            by_isin.setdefault(row.isin, []).append(row)

        written: list[Path] = []
        for isin, isin_rows in sorted(by_isin.items()):
            path = restated_isin_partition_path(dataset, isin, data_root=self.data_root)
            path.parent.mkdir(parents=True, exist_ok=True)
            # Total order so the file is byte-identical across re-derivations, whatever the order.
            isin_rows.sort(key=lambda r: (r.statement, r.metric, r.period))
            table = pa.Table.from_pylist(
                [
                    {
                        "isin": r.isin,
                        "symbol": r.symbol,
                        "statement": r.statement,
                        "metric": r.metric,
                        "period": r.period,
                        "value": r.value.quantize(_VALUE_Q, rounding=ROUND_HALF_UP),
                        "source": r.source,
                        "l0_source": r.l0_source,
                        "l0_filename": r.l0_filename,
                        "l0_logical_date": r.l0_logical_date,
                        "l0_sha256": r.l0_sha256,
                        "fetched_at": r.fetched_at,
                    }
                    for r in isin_rows
                ],
                schema=_SCHEMA,
            )
            _write_table(table, path)
            written.append(path)
            _LOG.info(
                "restated.written",
                dataset=dataset,
                isin=isin,
                rows=len(isin_rows),
                path=str(path),
                state="PUBLISHED",
            )
        return tuple(sorted(written))

    def read_isin(
        self, isin: str, *, dataset: str = SCREENER_FUNDAMENTALS_DATASET
    ) -> tuple[dict[str, object], ...]:
        """Read one ISIN's restated rows back as plain records; empty tuple if none are stored."""
        path = restated_isin_partition_path(dataset, isin, data_root=self.data_root)
        if not path.exists():
            return ()
        return tuple(pq.read_table(path, schema=_SCHEMA).to_pylist())


def _check_lineage(row: RestatedFundamental) -> None:
    """Refuse a row missing its source tag or its L0 lineage — the store's boundary check."""
    if row.source != SCREENER_SOURCE_TAG:
        raise ValueError(
            f"restated row for {row.isin} carries source {row.source!r}, not "
            f"{SCREENER_SOURCE_TAG!r}; a datum without the restated tag would be "
            "indistinguishable from PIT data (invariant #8)"
        )
    if not row.l0_sha256 or not row.l0_source:
        raise ValueError(
            f"restated row for {row.isin} has no L0 lineage; every datum must name the immutable "
            "bytes it derives from (invariant #1, acceptance #3)"
        )


def _write_table(table: pa.Table, path: Path) -> None:
    """Write a parquet table whole via a staging file, so a partition is never half-written."""
    staging = path.with_name(f".{path.name}.partial")
    pq.write_table(table, staging, compression="snappy", version="2.6")
    staging.replace(path)


def _identifier(value: str) -> str:
    if not _IDENTIFIER.match(value):
        raise PathLayoutError(
            f"dataset {value!r} is not a valid lake identifier: lower-case letters, digits, '.', "
            "'_' and '-' only, starting with a letter or digit"
        )
    return value


def _isin(value: str) -> str:
    if not _ISIN.match(value):
        raise PathLayoutError(
            f"{value!r} is not a valid ISIN; a restated partition is keyed on the ISIN join key, "
            "never a symbol (invariant #2)"
        )
    return value
