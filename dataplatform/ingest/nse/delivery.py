"""NSE per-security delivery data (§4.1 row 3) — `sec_bhavdata_full_{DDMMYYYY}.csv`.

One plain-CSV file per session, one row per `(symbol, series)` that traded, carrying the two
numbers this dataset exists for: `DELIV_QTY`, the shares that actually changed hands for delivery
rather than being squared off intraday, and `DELIV_PER`, that quantity as a percentage of the
session's traded volume. T0 monitoring reads a jump in delivery percentage as an accumulation
signal (§5), which is why the file earns a parser and a dataset of its own rather than being a
column on the bhavcopy.

Three properties of this file drive every decision here:

* **The delivery fields are `-`, not `0`, on the series that do not report them.** The
  trade-to-trade series (`BE`, `BZ`) publish `-` for both delivery columns because delivery is
  not a meaningful concept where intraday squaring-off is barred. That absence is modelled as
  `None` and *never* as `0`: a zero would read downstream as "0 % delivered", the strongest
  possible distribution signal, and a delivery-spike detector fed a stream of spurious zeros
  would fire on every `BE` name every day. `None` and `0` are different facts — one is "the
  exchange did not state this", the other is "the exchange stated it delivered nothing" — and
  keeping them distinct is the whole point of this parser.
* **Every column name after the first carries a leading space.** The header really is
  `SYMBOL, SERIES, DATE1, …` with the space inside each field, and the data rows are spaced the
  same way. The parser strips names and values rather than indexing by position, so a column the
  exchange reorders is caught by the header check instead of silently read from the wrong slot.
* **There is no ISIN in the file.** It states a symbol and a series, and NSE symbols are recycled
  and renamed (`IdentityMaster`'s reason to exist). So a delivery row cannot be joined to a price
  until it has been resolved through the D2 identity master with its own trade date — `resolve`
  below is that step, and it is the only sanctioned symbol→ISIN path for this source (invariant
  #2). The file's natural key is `(symbol, series, date)`; the ISIN it resolves to is a function
  of `(symbol, date)`, never of the symbol alone.

Output is `DeliveryRow` — symbol-keyed, no ISIN yet — and `resolve` turns a batch of them into
`ResolvedDeliveryRow`s the L1 writer (M1.8) joins onto prices. Offline by construction: this
module takes bytes, or an `L0Ref` it reads back through `L0Store`, and never fetches.
"""

from __future__ import annotations

import csv
import io
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from dataplatform.identity.master import Exchange, IdentityMaster
from dataplatform.ingest.models import ParseError
from dataplatform.logging import get_logger
from dataplatform.store.l0 import L0Ref, L0Store

__all__ = [
    "DELIVERY_COLUMNS",
    "DELIVERY_SOURCE_ID",
    "DeliveryResolution",
    "DeliveryRow",
    "ResolvedDeliveryRow",
    "parse",
    "parse_l0",
    "parse_text",
    "resolve",
]

_LOG = get_logger(__name__)

#: The register id this parser serves (`source_register.yaml`, `parser.task: M1.6`).
DELIVERY_SOURCE_ID: Final = "nse_sec_bhavdata_full"

#: The file's header, exactly, with the leading spaces already stripped. The parser compares the
#: file's stripped names against this rather than trusting field order, so a reordered or renamed
#: column is a `ParseError` naming the file, not a value read out of the wrong position.
DELIVERY_COLUMNS: Final = (
    "SYMBOL",
    "SERIES",
    "DATE1",
    "PREV_CLOSE",
    "OPEN_PRICE",
    "HIGH_PRICE",
    "LOW_PRICE",
    "LAST_PRICE",
    "CLOSE_PRICE",
    "AVG_PRICE",
    "TTL_TRD_QNTY",
    "TURNOVER_LACS",
    "NO_OF_TRADES",
    "DELIV_QTY",
    "DELIV_PER",
)

#: The marker the exchange writes where a series does not report delivery. Modelled as `None`.
_ABSENT: Final = "-"

#: A plain non-negative decimal literal — all `DELIV_PER` ever is. Narrower than `Decimal()`,
#: which would accept `NaN`/`Infinity` and let a mis-framed field become a percentage.
_DECIMAL_LITERAL: Final = re.compile(r"^\d+(\.\d+)?$")

#: A plain non-negative integer literal — all `DELIV_QTY` ever is.
_INTEGER_LITERAL: Final = re.compile(r"^\d+$")

#: `DATE1` is `DD-Mon-YYYY` (e.g. `07-Aug-2026`). Mapped explicitly rather than via
#: `strptime('%b')`, whose month names follow the process locale — a parser whose output depends
#: on `LC_TIME` is not reproducible (same reasoning as `bhavcopy_legacy`).
_MONTHS: Final = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}

_DATE: Final = re.compile(r"^(\d{1,2})-([A-Za-z]{3})-(\d{4})$")

#: A delivered-share count: whole shares, non-negative, and strict so a float that lost precision
#: cannot round itself into a delivery figure. `0` is legal and meaningful; absence is `None`.
DeliveryQuantity = Annotated[int, Field(ge=0, strict=True)]

#: A delivery percentage: non-negative `Decimal`, never a float, never `NaN`/`Infinity`. `0` is a
#: real value the exchange can publish; absence is `None`.
DeliveryPercent = Annotated[Decimal, Field(ge=0, strict=True, allow_inf_nan=False)]


class DeliveryRow(BaseModel):
    """One security's delivery facts for one session, before it has an ISIN.

    What it does: carry the `(symbol, series, date)` natural key and the two delivery numbers the
    file states, in the types the rest of the platform computes with.
    What it assumes: the parser has already checked the file's structure, so a `DeliveryRow` that
    exists is a row the exchange really published.
    What it never does: hold an ISIN (this source has none — see `resolve`), turn a `-` into a
    `0`, or hold a delivery figure as a float. `deliv_qty`/`deliv_pct` are `None` exactly when the
    exchange wrote `-`, which is a different fact from a delivered quantity of zero.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str = Field(min_length=1, description="exchange ticker on `trade_date`, as published")
    series: str = Field(min_length=1, description="NSE series: EQ, BE, BZ, SM, ST, GS…; verbatim")
    trade_date: date = Field(description="the exchange session this row is about (Asia/Kolkata)")
    deliv_qty: DeliveryQuantity | None = Field(
        description="shares taken to delivery (DELIV_QTY); None when the file wrote '-'"
    )
    deliv_pct: DeliveryPercent | None = Field(
        description="delivery as a % of traded volume (DELIV_PER); None when the file wrote '-'"
    )


class ResolvedDeliveryRow(BaseModel):
    """A `DeliveryRow` after its symbol has become an ISIN through the D2 identity master.

    The shape M1.8 joins onto `PriceRow` by `(isin, trade_date)`. `series` is kept because the
    delivery file's key is `(symbol, series, date)` and the same ISIN can trade in more than one
    series on a date; the join to prices is still ISIN-keyed (invariant #2), never symbol-keyed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    isin: str = Field(description="resolved via IdentityMaster.resolve(symbol, trade_date)")
    symbol: str = Field(min_length=1)
    series: str = Field(min_length=1)
    trade_date: date
    deliv_qty: DeliveryQuantity | None
    deliv_pct: DeliveryPercent | None


@dataclass(frozen=True, slots=True)
class DeliveryResolution:
    """The outcome of resolving a session's delivery rows to ISINs.

    `unresolved` holds the rows whose `(symbol, date)` the master had never seen — quarantined and
    counted rather than dropped, so a delivery file naming a security the identity master has not
    ingested yet is a visible gap, not silent data loss (the M1.8 contract). An *ambiguous* symbol
    is not quarantined here: `IdentityMaster.resolve` raises `AmbiguousSymbolError` and queues the
    conflict, because "we know two contradictory ISINs" is a different fact from "we know none".
    """

    resolved: tuple[ResolvedDeliveryRow, ...]
    unresolved: tuple[DeliveryRow, ...]


def parse(payload: bytes, *, filename: str) -> tuple[DeliveryRow, ...]:
    """Parse one `sec_bhavdata_full` CSV into delivery rows, in the file's own order.

    Assumes `payload` is one complete file — an EOD artefact of a few hundred kilobytes — so a
    truncation is detected as a short row rather than streamed past. `filename` names the file in
    errors and logs and is not parsed for the date: the rows carry `DATE1` and disagreeing with it
    silently would be worse.

    Raises `ParseError` — naming the file, and the line when the fault is one record's — for a
    non-UTF-8 body, an unrecognised header, a short or wide row, a field that is not the number or
    date it must be, a duplicate `(symbol, series)` key, or rows spanning more than one session.
    """
    text = _text_of(payload, filename=filename)
    rows = parse_text(text, filename=filename)
    _LOG.info(
        "delivery.parsed",
        source=DELIVERY_SOURCE_ID,
        filename=filename,
        trade_date=rows[0].trade_date.isoformat(),
        rows=len(rows),
        absent=sum(1 for row in rows if row.deliv_qty is None),
        state="NORMALIZED",
    )
    return rows


def parse_l0(store: L0Store, ref: L0Ref) -> tuple[DeliveryRow, ...]:
    """Parse the L0 payload a fetch produced, re-verifying its checksum on the way in.

    The pipeline entry point: `L0Store.get` re-hashes the payload, so "every L1 value derives from
    bytes that have not changed" holds at the point of derivation, not only at fetch.
    """
    return parse(store.get(ref), filename=ref.filename)


def parse_text(text: str, *, filename: str) -> tuple[DeliveryRow, ...]:
    """Parse the decoded CSV body. Separated from `parse` so a caller can hand over text it already
    has and the archive/decode handling above keeps exactly one job."""
    reader = csv.reader(io.StringIO(text))
    _check_header(next(reader, None), filename=filename)

    rows: list[DeliveryRow] = []
    for record in reader:
        if not record or not any(field.strip() for field in record):
            # A trailing newline, not a row.
            continue
        rows.append(_row(record, line=reader.line_num, filename=filename))

    if not rows:
        raise ParseError(
            "no data rows after the header; a session's delivery file always has some",
            filename=filename,
        )
    _one_session(rows, filename=filename)
    _unique_keys(rows, filename=filename)
    return tuple(rows)


def resolve(
    rows: Iterable[DeliveryRow],
    master: IdentityMaster,
    *,
    exchange: Exchange = Exchange.NSE,
) -> DeliveryResolution:
    """Resolve a batch of delivery rows to ISINs through the D2 identity master.

    What it does: for each row, looks up the ISIN that traded as `row.symbol` on `row.trade_date`
    via `IdentityMaster.try_resolve` — the *only* sanctioned symbol→ISIN path (invariant #2) — and
    returns the rows it could place alongside the ones it could not.
    What it assumes: `row.trade_date` is the session the symbol should be read as-of; a symbol is
    never resolved by name alone, because a recycled symbol resolves to different ISINs on
    different dates.
    What it never does: guess an ISIN. An unknown `(symbol, date)` is quarantined into
    `unresolved` and counted; an ambiguous one raises `AmbiguousSymbolError` from the master.
    """
    resolved: list[ResolvedDeliveryRow] = []
    unresolved: list[DeliveryRow] = []
    for row in rows:
        isin = master.try_resolve(row.symbol, row.trade_date, exchange=exchange)
        if isin is None:
            unresolved.append(row)
            continue
        resolved.append(
            ResolvedDeliveryRow(
                isin=isin,
                symbol=row.symbol,
                series=row.series,
                trade_date=row.trade_date,
                deliv_qty=row.deliv_qty,
                deliv_pct=row.deliv_pct,
            )
        )
    if unresolved:
        _LOG.warning(
            "delivery.unresolved",
            source=DELIVERY_SOURCE_ID,
            exchange=exchange.value,
            unresolved=len(unresolved),
            resolved=len(resolved),
        )
    return DeliveryResolution(resolved=tuple(resolved), unresolved=tuple(unresolved))


# ── internals ────────────────────────────────────────────────────────────────────────────────


def _text_of(payload: bytes, *, filename: str) -> str:
    """Decode a payload to CSV text. This source ships a bare CSV, not an archive."""
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ParseError(
            f"payload is not UTF-8 text at byte {exc.start} ({exc.reason})", filename=filename
        ) from exc


def _check_header(header: list[str] | None, *, filename: str) -> None:
    """Refuse anything but this file's exact header, whitespace stripped.

    Comparing stripped names to `DELIVERY_COLUMNS` is what lets the rest of the parser address
    fields by name: a column the exchange adds, drops or reorders fails here, on line 1, rather
    than shifting every value one slot to the right and being read as a plausible number.
    """
    if header is None:
        raise ParseError("file is empty; expected the delivery header row", filename=filename)

    named = tuple(field.strip() for field in header)
    if named != DELIVERY_COLUMNS:
        raise ParseError(
            f"unexpected header {','.join(named)!r}; this parser reads the NSE sec_bhavdata_full "
            f"file, whose columns are exactly {','.join(DELIVERY_COLUMNS)!r} (each name carries a "
            "leading space in the file, stripped here)",
            filename=filename,
            line=1,
        )


def _row(record: list[str], *, line: int, filename: str) -> DeliveryRow:
    """Turn one CSV record into a `DeliveryRow`, or say precisely which line and field was wrong."""
    if len(record) != len(DELIVERY_COLUMNS):
        raise ParseError(
            f"row has {len(record)} fields, header has {len(DELIVERY_COLUMNS)}; a short row here "
            "is what a truncated download looks like",
            filename=filename,
            line=line,
        )

    field = dict(zip(DELIVERY_COLUMNS, (value.strip() for value in record), strict=True))
    symbol = field["SYMBOL"]
    series = field["SERIES"]
    if not symbol:
        raise ParseError("SYMBOL is empty", filename=filename, line=line)
    if not series:
        raise ParseError("SERIES is empty", filename=filename, line=line)

    try:
        return DeliveryRow(
            symbol=symbol,
            series=series,
            trade_date=_date(field["DATE1"], column="DATE1", line=line, filename=filename),
            deliv_qty=_optional_integer(
                field["DELIV_QTY"], column="DELIV_QTY", line=line, filename=filename
            ),
            deliv_pct=_optional_decimal(
                field["DELIV_PER"], column="DELIV_PER", line=line, filename=filename
            ),
        )
    except ValidationError as exc:
        raise ParseError(
            f"row is not a valid delivery row: {exc.errors(include_url=False)}",
            filename=filename,
            line=line,
        ) from exc


def _optional_integer(value: str, *, column: str, line: int, filename: str) -> int | None:
    """`None` for the `-` marker, an exact non-negative `int` otherwise, a located error for junk.

    The `-` → `None` mapping is load-bearing: a series that does not report delivery must not read
    as "0 delivered", which a spike detector would treat as the strongest distribution signal.
    """
    if value == _ABSENT:
        return None
    if not _INTEGER_LITERAL.match(value):
        raise ParseError(
            f"{column} is {value!r}, which is neither the '-' absence marker nor a non-negative "
            "integer literal",
            filename=filename,
            line=line,
        )
    return int(value)


def _optional_decimal(value: str, *, column: str, line: int, filename: str) -> Decimal | None:
    """`None` for the `-` marker, an exact `Decimal` otherwise, a located error for junk."""
    if value == _ABSENT:
        return None
    if not _DECIMAL_LITERAL.match(value):
        raise ParseError(
            f"{column} is {value!r}, which is neither the '-' absence marker nor a non-negative "
            "decimal literal",
            filename=filename,
            line=line,
        )
    try:
        return Decimal(value)
    except InvalidOperation as exc:  # pragma: no cover — the pattern already excludes these
        raise ParseError(
            f"{column} is {value!r}, which Decimal cannot represent", filename=filename, line=line
        ) from exc


def _date(value: str, *, column: str, line: int, filename: str) -> date:
    """Parse `DD-Mon-YYYY` into a trading date, locale-independently."""
    match = _DATE.match(value)
    month = match.group(2).upper() if match else ""
    if match is None or month not in _MONTHS:
        raise ParseError(
            f"{column} is {value!r}, which is not a DD-Mon-YYYY exchange date",
            filename=filename,
            line=line,
        )
    day, year = int(match.group(1)), int(match.group(3))
    try:
        return date(year, _MONTHS[month], day)
    except ValueError as exc:
        raise ParseError(
            f"{column} is {value!r}, which is not a real calendar date",
            filename=filename,
            line=line,
        ) from exc


def _one_session(rows: Sequence[DeliveryRow], *, filename: str) -> None:
    """Refuse a file whose rows do not all belong to the same session.

    A delivery file *is* one session, and L1 partitions by date (§4.2); accepting two dates would
    spread one file across two partitions and leave both looking complete.
    """
    dates = {row.trade_date for row in rows}
    if len(dates) > 1:
        raise ParseError(
            "rows span more than one session: "
            f"{', '.join(sorted(day.isoformat() for day in dates))}",
            filename=filename,
        )


def _unique_keys(rows: Sequence[DeliveryRow], *, filename: str) -> None:
    """Refuse a duplicate `(symbol, series)` within the file.

    The file's natural key is `(symbol, series, date)`; two rows sharing it would make the join to
    prices ambiguous and let one security's delivery figure silently overwrite another's.
    """
    counts = Counter((row.symbol, row.series) for row in rows)
    dupes = sorted(f"{symbol}/{series}" for (symbol, series), n in counts.items() if n > 1)
    if dupes:
        raise ParseError(
            f"duplicate (symbol, series) keys in one session: {', '.join(dupes)}",
            filename=filename,
        )
