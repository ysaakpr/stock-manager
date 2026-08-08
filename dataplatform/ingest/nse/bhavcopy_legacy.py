"""NSE cash-market bhavcopy, pre-UDiFF era (§4.1 row 1) — `cm{DD}{MON}{YYYY}bhav.csv.zip`.

The format the exchange published until 8 July 2024: one zip holding one CSV, fourteen physical
fields per line because the header ends in a comma, and one row per (symbol, series) that had a
session. Every trailing-comma-shaped surprise in this file is load-bearing, so the parser is
explicit about the shape it accepts rather than forgiving:

* **The header must be exactly the documented thirteen columns.** Not "must contain" — must be.
  The archive reaches back past this format into an older sub-era whose files carry eleven columns
  and no `ISIN` at all (`cm04JAN2010bhav.csv` is one), and a lenient parser would happily read
  those, drop the identity of every row on the floor, and hand D2 a symbol to guess with. A header
  this parser does not recognise is a `ParseError` naming the file, which is a backfill that stops
  rather than a decade of unjoinable rows.
* **Every row must be as wide as the header.** That is what makes a truncated download — the
  common failure when an archive host cuts a response short — a loud error on the line it stopped
  at instead of one silently short final row.
* **Numbers are read as text and converted exactly.** `Decimal(field)`, never `float(field)`, and
  the field must look like a plain decimal literal first: `Decimal` itself would accept `NaN` and
  `Infinity`, and a mis-framed field that spells one of those must not become a price that
  compares greater than every other price.
* **`SERIES` is kept verbatim and nothing is filtered.** `EQ`, `BE`, `BZ`, `SM`, `ST` and the long
  tail of debt, odd-lot and government-security series all become rows. Which of them a strategy
  is allowed to see is a query concern (D6); a parser that filtered here would make L1 no longer a
  faithful normalization of L0.

Output is `PriceRow`, the schema M1.5's UDiFF parser also emits, so nothing downstream branches on
which era a row came from.

Offline by construction: this module takes bytes, or an `L0Ref` it reads back through `L0Store`.
It never fetches — the crawl engine (`dataplatform.ingest.fetcher`) is the only thing that does.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Final

from pydantic import ValidationError

from dataplatform.ingest.models import ParseError, PriceRow
from dataplatform.logging import get_logger
from dataplatform.store.l0 import L0Ref, L0Store

__all__ = [
    "LEGACY_COLUMNS",
    "LEGACY_ERA_END",
    "LEGACY_SOURCE_ID",
    "parse",
    "parse_l0",
    "parse_text",
]

_LOG = get_logger(__name__)

#: The register id this parser serves (`source_register.yaml`, `parser.task: M1.4`).
LEGACY_SOURCE_ID: Final = "nse_bhavcopy_legacy"

#: The register row's `era.end`. Kept here so a caller can ask this module what it covers; which
#: parser serves the boundary date itself is M1.5's dispatcher to decide, not this file's.
LEGACY_ERA_END: Final = date(2024, 7, 8)

#: The era's header, exactly. The file writes a fourteenth, unnamed field (the trailing comma);
#: `_read_header` accounts for it without letting it become a column.
LEGACY_COLUMNS: Final = (
    "SYMBOL",
    "SERIES",
    "OPEN",
    "HIGH",
    "LOW",
    "CLOSE",
    "LAST",
    "PREVCLOSE",
    "TOTTRDQTY",
    "TOTTRDVAL",
    "TIMESTAMP",
    "TOTALTRADES",
    "ISIN",
)

#: A plain non-negative decimal literal, which is all this file ever contains. Deliberately
#: narrower than what `Decimal()` accepts: `NaN`, `Infinity`, `1E9` and `+3` are all rejected here
#: with a line number rather than smuggled into a price field.
_DECIMAL_LITERAL: Final = re.compile(r"^\d+(\.\d+)?$")

#: A plain non-negative integer literal. Narrower than `int()`, which accepts `1_000` and `+7`.
_INTEGER_LITERAL: Final = re.compile(r"^\d+$")

#: `TIMESTAMP` is `DD-MON-YYYY` with an English month abbreviation, zero-padding not guaranteed
#: across the era. Mapped explicitly rather than via `strptime('%b')`, whose month names follow the
#: process locale — a parser whose output depends on `LC_TIME` is not reproducible.
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

_TIMESTAMP: Final = re.compile(r"^(\d{1,2})-([A-Z]{3})-(\d{4})$")

#: The zip local-file-header magic. Used to tell "a zipped bhavcopy" from "the CSV inside one",
#: because both are things a caller legitimately has: L0 holds the zip the source served, and a
#: test or a manual recovery may hold the member.
_ZIP_MAGIC: Final = b"PK\x03\x04"


def parse(payload: bytes, *, filename: str) -> tuple[PriceRow, ...]:
    """Parse one legacy bhavcopy — the zip as served, or its CSV member — into canonical rows.

    Rows come back in file order, which is the order the exchange published them (symbol, then
    series), so re-parsing an L0 payload is byte-for-byte reproducible.

    Assumes `payload` is one complete file: this is an EOD artefact of a few hundred kilobytes,
    and holding it whole is what lets a truncation be detected rather than streamed past.
    `filename` is used only to name the file in errors and logs — it is not parsed for the date,
    because the rows carry their own and disagreeing with them silently would be worse than
    either.

    Raises `ParseError` — naming the file, and the line where the failure is attributable to one —
    for a corrupt archive, an unrecognised header, a short or wide row, a field that is not the
    number or date it must be, and a file whose rows do not all belong to one session.
    """
    text = _text_of(payload, filename=filename)
    rows = parse_text(text, filename=filename)
    _LOG.info(
        "bhavcopy.parsed",
        source=LEGACY_SOURCE_ID,
        era="legacy",
        filename=filename,
        trade_date=rows[0].trade_date.isoformat(),
        rows=len(rows),
        state="NORMALIZED",
    )
    return rows


def parse_l0(store: L0Store, ref: L0Ref) -> tuple[PriceRow, ...]:
    """Parse the L0 payload a fetch produced, re-verifying its checksum on the way in.

    The pipeline's entry point: `Fetcher.fetch` returns an `L0Ref` and never bytes, so this is how
    a fetched bhavcopy becomes rows. `L0Store.get` re-hashes the payload, which is what makes
    "every L1 value is derived from bytes that have not changed" true at the point of derivation
    rather than only at the point of fetch.
    """
    return parse(store.get(ref), filename=ref.filename)


def parse_text(text: str, *, filename: str) -> tuple[PriceRow, ...]:
    """Parse the decoded CSV body. Separated from `parse` so a caller can hand over text it
    already has (a recovery from a manually unzipped file), and so the archive handling above has
    exactly one job."""
    reader = csv.reader(io.StringIO(text))
    width = _header_width(next(reader, None), filename=filename)

    rows: list[PriceRow] = []
    for record in reader:
        if not record or not any(field.strip() for field in record):
            # A trailing newline, not a row. Anything with content in it must be a full record.
            continue
        rows.append(_row(record, line=reader.line_num, width=width, filename=filename))

    if not rows:
        raise ParseError(
            "no data rows after the header; a session's bhavcopy always has some", filename=filename
        )
    _one_session(rows, filename=filename)
    return tuple(rows)


# ── internals ────────────────────────────────────────────────────────────────────────────────


def _text_of(payload: bytes, *, filename: str) -> str:
    """Decode a payload to CSV text, unzipping it first when it is the archive the source serves.

    A zip must hold exactly one member. Zero means the archive is empty and several means the
    exchange changed what it ships, and both are things to stop on: picking "the first CSV" would
    turn a format change into a day of quietly wrong data.
    """
    if payload.startswith(_ZIP_MAGIC):
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                members = archive.namelist()
                if len(members) != 1:
                    raise ParseError(
                        f"expected exactly one member in the archive, found {len(members)}: "
                        f"{', '.join(members) or '(none)'}",
                        filename=filename,
                    )
                body = archive.read(members[0])
        except zipfile.BadZipFile as exc:
            raise ParseError(
                f"not a readable zip archive ({exc}); the download is corrupt or truncated — "
                "the payload in L0 is the evidence, do not re-fetch over it",
                filename=filename,
            ) from exc
    else:
        body = payload

    try:
        return body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ParseError(
            f"payload is not UTF-8 text at byte {exc.start} ({exc.reason})", filename=filename
        ) from exc


def _header_width(header: list[str] | None, *, filename: str) -> int:
    """Check the header is exactly this era's, and return the physical field count rows must have.

    The returned width includes the unnamed field the format's trailing comma produces, so a row
    that lost its tail — the shape a truncated transfer leaves behind — fails the width check.
    """
    if header is None:
        raise ParseError("file is empty; expected the bhavcopy header row", filename=filename)

    named = tuple(field.strip() for field in header)
    while named and named[-1] == "":
        named = named[:-1]

    if named != LEGACY_COLUMNS:
        raise ParseError(
            f"unexpected header {','.join(named)!r}; this parser reads the pre-Jul-2024 NSE "
            f"bhavcopy, whose header is exactly {','.join(LEGACY_COLUMNS)!r}. Files from the "
            "older pre-ISIN sub-era of the archive and UDiFF-era files (M1.5) both land here and "
            "are refused on purpose",
            filename=filename,
            line=1,
        )
    return len(header)


def _row(record: list[str], *, line: int, width: int, filename: str) -> PriceRow:
    """Turn one CSV record into a `PriceRow`, or say precisely which line and field was wrong."""
    if len(record) != width:
        raise ParseError(
            f"row has {len(record)} fields, header has {width}; a short row here is what a "
            "truncated download looks like",
            filename=filename,
            line=line,
        )
    for index in range(len(LEGACY_COLUMNS), width):
        if record[index].strip():
            raise ParseError(
                f"unnamed trailing field {index + 1} carries data {record[index]!r}; this era's "
                "trailing comma is always empty",
                filename=filename,
                line=line,
            )

    field = dict(zip(LEGACY_COLUMNS, (value.strip() for value in record), strict=False))
    try:
        return PriceRow(
            isin=field["ISIN"],
            symbol=field["SYMBOL"],
            series=field["SERIES"],
            trade_date=_date(field["TIMESTAMP"], column="TIMESTAMP", line=line, filename=filename),
            open=_decimal(field["OPEN"], column="OPEN", line=line, filename=filename),
            high=_decimal(field["HIGH"], column="HIGH", line=line, filename=filename),
            low=_decimal(field["LOW"], column="LOW", line=line, filename=filename),
            close=_decimal(field["CLOSE"], column="CLOSE", line=line, filename=filename),
            last=_decimal(field["LAST"], column="LAST", line=line, filename=filename),
            prev_close=_decimal(
                field["PREVCLOSE"], column="PREVCLOSE", line=line, filename=filename
            ),
            total_traded_qty=_integer(
                field["TOTTRDQTY"], column="TOTTRDQTY", line=line, filename=filename
            ),
            total_traded_value=_decimal(
                field["TOTTRDVAL"], column="TOTTRDVAL", line=line, filename=filename
            ),
            total_trades=_integer(
                field["TOTALTRADES"], column="TOTALTRADES", line=line, filename=filename
            ),
        )
    except ValidationError as exc:
        raise ParseError(
            f"row is not a valid price row: {exc.errors(include_url=False)}",
            filename=filename,
            line=line,
        ) from exc


def _decimal(value: str, *, column: str, line: int, filename: str) -> Decimal:
    """Exact `Decimal` for a plain decimal literal, and a located error for anything else."""
    if not _DECIMAL_LITERAL.match(value):
        raise ParseError(
            f"{column} is {value!r}, which is not a non-negative decimal literal",
            filename=filename,
            line=line,
        )
    try:
        return Decimal(value)
    except InvalidOperation as exc:  # pragma: no cover — the pattern already excludes these
        raise ParseError(
            f"{column} is {value!r}, which Decimal cannot represent", filename=filename, line=line
        ) from exc


def _integer(value: str, *, column: str, line: int, filename: str) -> int:
    """Exact `int` for a plain integer literal, and a located error for anything else."""
    if not _INTEGER_LITERAL.match(value):
        raise ParseError(
            f"{column} is {value!r}, which is not a non-negative integer literal",
            filename=filename,
            line=line,
        )
    return int(value)


def _date(value: str, *, column: str, line: int, filename: str) -> date:
    """Parse `DD-MON-YYYY` into a trading date, locale-independently."""
    match = _TIMESTAMP.match(value)
    if match is None or match.group(2) not in _MONTHS:
        raise ParseError(
            f"{column} is {value!r}, which is not a DD-MON-YYYY exchange date",
            filename=filename,
            line=line,
        )
    day, month, year = int(match.group(1)), _MONTHS[match.group(2)], int(match.group(3))
    try:
        return date(year, month, day)
    except ValueError as exc:
        raise ParseError(
            f"{column} is {value!r}, which is not a real calendar date",
            filename=filename,
            line=line,
        ) from exc


def _one_session(rows: list[PriceRow], *, filename: str) -> None:
    """Refuse a file whose rows do not all belong to the same session.

    A bhavcopy *is* one session. Two dates in one file means a concatenation or a mis-served
    payload, and downstream partitions by date (§4.2) — so accepting it would spread one file
    across two partitions and leave both looking complete.
    """
    dates = {row.trade_date for row in rows}
    if len(dates) > 1:
        raise ParseError(
            "rows span more than one session: "
            f"{', '.join(sorted(day.isoformat() for day in dates))}",
            filename=filename,
        )
