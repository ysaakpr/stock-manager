"""The declared-once L1 schemas the canonical writers enforce on every write (M1.8).

A parquet dataset's schema is a contract with every reader downstream of it, and the cheapest
place for that contract to rot is a writer that builds its table column-by-column and lets the
column set drift one field at a time. So the schema for a dataset lives here, exactly once, and
the writer in `l1.py` builds against it and refuses to write a table whose schema is not it
(`enforce_schema`). A field added, dropped, renamed or retyped is then a loud failure at the
write, not a surprise a backtest discovers a year of rows later.

Two invariants are enforced structurally in this module rather than trusted to a code review:

* **No adjusted prices in L1 (invariant #3).** `prices_raw` is raw traded data exactly as the
  exchange published it; adjusted series are derived on read or materialised into L2, and are
  always recomputable from raw + factors. `assert_raw_only` rejects a schema that carries a
  column whose name looks like an adjustment (`adj…`, `…factor`, `cum_*_factor`), so the day
  someone tries to widen this table with an `adj_close` the write fails instead of quietly
  storing a value that must never live here.
* **ISIN is the only join key (invariant #2).** Every `prices_raw` row is ISIN-keyed and the
  `isin` column is non-nullable — a row cannot reach L1 without one. The delivery join that the
  writer performs resolves symbols to ISINs through the D2 identity master; a symbol it cannot
  resolve is quarantined into `prices_raw_quarantine`, never joined by symbol and never dropped.

Both schemas are `decimal128` for money (never float) and `date32` for trading dates, matching the
sibling `fo_aggregates` L1 schema so the whole lake reads with one set of column types.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Final

import pyarrow as pa
from pydantic import BaseModel, ConfigDict, Field

from dataplatform.ingest.models import ISIN_PATTERN, Price, Quantity

__all__ = [
    "PRICES_RAW_DATASET",
    "PRICES_RAW_QUARANTINE_DATASET",
    "PRICES_RAW_QUARANTINE_SCHEMA",
    "PRICES_RAW_SCHEMA",
    "PriceQuarantineReason",
    "PricesRawQuarantineRow",
    "PricesRawRow",
    "SchemaError",
    "assert_raw_only",
    "column_looks_adjusted",
    "enforce_schema",
]

#: L1 dataset name — `data/L1/prices_raw/date=YYYY-MM-DD/part.parquet` (§4.2).
PRICES_RAW_DATASET: Final = "prices_raw"

#: The quarantine dataset for delivery rows the writer could not place on a price row — its own
#: dataset, *not* a second file inside the `prices_raw` partition, so a DuckDB/pyarrow scan of the
#: `prices_raw` partition directory never reads a quarantined row back as if it were canonical.
PRICES_RAW_QUARANTINE_DATASET: Final = "prices_raw_quarantine"

#: Decimal quantum for a price or a rupee amount (four places) and for a percentage (four).
#: Fixing the scale is what makes the parquet byte-deterministic: `100.5` and `100.50` are stored
#: as the identical unscaled integer at scale 4, so re-deriving a partition from L0 is byte-equal.
_PRICE_Q: Final = Decimal("0.0001")
_PCT_Q: Final = Decimal("0.0001")

#: Substrings that mark a column as an *adjusted* or *factor* value — the thing invariant #3 forbids
#: from L1. Matched case-insensitively against a column name in `column_looks_adjusted`.
_ADJUSTED_TOKENS: Final = ("adj", "factor", "adjusted", "cum_price", "cum_qty")


class SchemaError(ValueError):
    """A table's schema is not the declared schema for its dataset, or carries a forbidden column.

    Raised on write, never swallowed: a schema drift that reached disk would be discovered by a
    reader, far from the writer that caused it, so the writer fails loud here instead.
    """


class PriceQuarantineReason:
    """Why a row was quarantined instead of landing in `prices_raw`.

    Not a `StrEnum` because these are stored verbatim in a parquet string column and read back as
    plain strings; the two values are named here so the writer and any consumer agree on them.
    """

    #: The delivery file named a `(symbol, date)` the identity master has never seen — no ISIN.
    SYMBOL_UNRESOLVED: Final = "symbol_unresolved"

    #: The row resolved to an ISIN, but no price row in the session carried that `(isin, series)`.
    NO_MATCHING_PRICE: Final = "no_matching_price"

    #: A *price* row, not a delivery one: the bhavcopy carried a placeholder where the ISIN
    #: belongs (`bhavcopy_legacy.PLACEHOLDER_ISINS`). ISIN is the only join key, so the row cannot
    #: enter `prices_raw` — and the session must not be refused for it either, which is what
    #: happened to 2021-02-16 until the 2026-09-06 audit.
    ISIN_NOT_PUBLISHED: Final = "isin_not_published"

    #: A price row from a format era that had **no ISIN column at all** — the NSE bhavcopy before
    #: 2011-06-22 (`eras.ERAS`, era E1). Distinct from `ISIN_NOT_PUBLISHED` on purpose: there the
    #: exchange stated an instrument has no ISIN, here it never stated any instrument's, so the
    #: fix is different (identity lineage work, not a per-instrument exception) and the counts must
    #: not be summed as if they were the same problem.
    ISIN_COLUMN_ABSENT: Final = "isin_column_absent"


class PricesRawRow(BaseModel):
    """One security's raw traded session on one exchange — the canonical `prices_raw` L1 row.

    What it does: carry a `PriceRow`'s facts (verbatim, unadjusted) plus the exchange it traded on
    and the delivery figures joined in from the delivery file by `(isin, series, date)`.
    What it assumes: `isin` was carried by the source bhavcopy (both eras publish it) or resolved
    through the D2 master; it is required, because a row that reached L1 without one would have to
    be joined on a symbol (invariant #2).
    What it never does: hold an adjusted price or a derived field. `deliv_qty`/`deliv_pct` are
    `None` — never `0` — when the session had no delivery figure for this security, preserving the
    absence the delivery file states with `-`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    isin: str = Field(
        pattern=ISIN_PATTERN, description="the only legitimate join key (invariant #2)"
    )
    exchange: str = Field(min_length=1, description="the exchange this session traded on: NSE/BSE")
    symbol: str = Field(min_length=1, description="exchange ticker on `trade_date`, as published")
    series: str = Field(min_length=1, description="NSE series: EQ, BE, BZ, SM…; verbatim")
    trade_date: date = Field(description="the exchange session this row is about (Asia/Kolkata)")

    open: Price = Field(description="first traded price of the session, unadjusted")
    high: Price = Field(description="highest traded price of the session, unadjusted")
    low: Price = Field(description="lowest traded price of the session, unadjusted")
    close: Price = Field(description="closing price as the exchange published it, unadjusted")
    last: Price = Field(description="last traded price, unadjusted")
    prev_close: Price = Field(description="previous session's close, unadjusted")

    total_traded_qty: Quantity = Field(description="shares traded in the session")
    total_traded_value: Price = Field(description="turnover in rupees")
    total_trades: Quantity = Field(description="number of trades executed")

    deliv_qty: int | None = Field(
        default=None, ge=0, description="shares taken to delivery; None when the file wrote '-'"
    )
    deliv_pct: Decimal | None = Field(
        default=None, ge=0, description="delivery as a % of volume; None when the file wrote '-'"
    )


class PricesRawQuarantineRow(BaseModel):
    """A delivery row the writer could not place on a price row — quarantined, counted, not dropped.

    Either the identity master could not resolve its `(symbol, date)` to an ISIN
    (`SYMBOL_UNRESOLVED`, `isin` is `None`), or it resolved but no price row in the session carried
    that `(isin, series)` (`NO_MATCHING_PRICE`). Both are visible gaps a downstream check can act
    on, which is the whole point of quarantining rather than silently discarding them.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str = Field(min_length=1)
    series: str = Field(min_length=1)
    trade_date: date
    exchange: str = Field(min_length=1)
    isin: str | None = Field(default=None, description="resolved ISIN, or None when unresolved")
    deliv_qty: int | None = Field(default=None, ge=0)
    deliv_pct: Decimal | None = Field(default=None, ge=0)
    reason: str = Field(min_length=1, description="a PriceQuarantineReason value")


#: The `prices_raw` parquet schema — declared once, enforced on every write. Raw traded columns
#: only: there is deliberately no adjusted-price column here (invariant #3), which `assert_raw_only`
#: verifies structurally. `deliv_*` are nullable because a session need not report delivery for
#: every series; every other column is non-nullable because a bhavcopy always states it.
PRICES_RAW_SCHEMA: Final = pa.schema(
    [
        pa.field("isin", pa.string(), nullable=False),
        pa.field("exchange", pa.string(), nullable=False),
        pa.field("symbol", pa.string(), nullable=False),
        pa.field("series", pa.string(), nullable=False),
        pa.field("trade_date", pa.date32(), nullable=False),
        pa.field("open", pa.decimal128(20, 4), nullable=False),
        pa.field("high", pa.decimal128(20, 4), nullable=False),
        pa.field("low", pa.decimal128(20, 4), nullable=False),
        pa.field("close", pa.decimal128(20, 4), nullable=False),
        pa.field("last", pa.decimal128(20, 4), nullable=False),
        pa.field("prev_close", pa.decimal128(20, 4), nullable=False),
        pa.field("total_traded_qty", pa.int64(), nullable=False),
        pa.field("total_traded_value", pa.decimal128(28, 4), nullable=False),
        pa.field("total_trades", pa.int64(), nullable=False),
        pa.field("deliv_qty", pa.int64(), nullable=True),
        pa.field("deliv_pct", pa.decimal128(12, 4), nullable=True),
    ]
)

#: The quarantine parquet schema. `isin` is nullable here (an unresolved row has none), which is the
#: one column that differs from `prices_raw` — a reminder that a quarantined row is exactly a row
#: that could not satisfy `prices_raw`'s non-null ISIN.
PRICES_RAW_QUARANTINE_SCHEMA: Final = pa.schema(
    [
        pa.field("symbol", pa.string(), nullable=False),
        pa.field("series", pa.string(), nullable=False),
        pa.field("trade_date", pa.date32(), nullable=False),
        pa.field("exchange", pa.string(), nullable=False),
        pa.field("isin", pa.string(), nullable=True),
        pa.field("deliv_qty", pa.int64(), nullable=True),
        pa.field("deliv_pct", pa.decimal128(12, 4), nullable=True),
        pa.field("reason", pa.string(), nullable=False),
    ]
)


def price_quantum() -> Decimal:
    """The price/amount quantum the writer quantises to for byte-determinism."""
    return _PRICE_Q


def pct_quantum() -> Decimal:
    """The percentage quantum the writer quantises to for byte-determinism."""
    return _PCT_Q


def column_looks_adjusted(name: str) -> bool:
    """Whether a column name looks like an adjusted price or an adjustment factor (invariant #3).

    A pure name check — matched case-insensitively against `_ADJUSTED_TOKENS` — so the guard is
    cheap and usable both here (on write) and in a test asserting `prices_raw` has no such column.
    """
    lowered = name.lower()
    return any(token in lowered for token in _ADJUSTED_TOKENS)


def assert_raw_only(schema: pa.Schema) -> None:
    """Raise `SchemaError` if any column name looks like an adjusted price (invariant #3).

    Enforced on the declared schema at write time so L1 can never grow an `adj_close`/`*_factor`
    column: adjusted series are derived on read or materialised into L2, never stored raw here.
    """
    offenders = [name for name in schema.names if column_looks_adjusted(name)]
    if offenders:
        raise SchemaError(
            f"prices_raw carries adjusted/factor column(s) {offenders!r}; L1 stores raw traded "
            "prices only (invariant #3). Adjusted series are derived on read or materialised in L2."
        )


def enforce_schema(table: pa.Table, expected: pa.Schema, *, dataset: str) -> None:
    """Raise `SchemaError` unless `table.schema` is exactly `expected` — a loud drift check.

    The write path calls this after building the table and before touching disk. It compares the
    full schema (names, types and nullability) so a reordered, renamed, retyped or added column is
    caught at the writer rather than by a reader that trusted the dataset's contract.
    """
    if not table.schema.equals(expected, check_metadata=False):
        raise SchemaError(
            f"{dataset} table schema drifted from the declared schema.\n"
            f"expected: {expected}\n"
            f"got:      {table.schema}"
        )
