"""NSE cash-market bhavcopy, UDiFF era (§4.1 row 2) — `BhavCopy_NSE_CM_0_0_0_{YYYYMMDD}_F_0000`.

The format the exchange switched to on 8 July 2024: an ISO-20022-flavoured ("UDiFF") CSV inside one
zip, thirty-four named columns, no trailing comma. It carries the same facts the legacy file did but
under new names (`TckrSymb`, `SctySrs`, `OpnPric`, `TtlTradgVol`, `TtlNbOfTxsExctd`, …) and dates in
ISO `YYYY-MM-DD` instead of `DD-MON-YYYY`.

Two things about this file drive the parser's strictness:

* **The header is byte-for-byte identical to the F&O bhavcopy's** (`BhavCopy_NSE_FO_…`, M3.7). Both
  are UDiFF and both ship the derivative columns `XpryDt`/`StrkPric`/`OptnTp`/`OpnIntrst`/…; the
  cash file simply leaves them empty. So the header alone cannot tell a cash file from an F&O file,
  and a parser that checked only the header would happily read an F&O file and emit futures as if
  they were equities. What separates them is *per row*: this parser requires `Sgmt == CM` and
  `FinInstrmTp == STK` on every record, so an F&O file (whose rows are `Sgmt == FO`,
  `FinInstrmTp == FUTSTK`/`OPTIDX`/…) is refused on its first data line rather than mis-read.
* **The derivative columns must be empty on an equity row.** A `STK` row that carried an expiry or a
  strike would be a contradiction — the file claiming to be cash while shaped like derivatives — so
  those three fields are asserted empty. This is the UDiFF analogue of the legacy parser's
  trailing-comma check: a structural guard that the row really is what the segment says it is, and
  the reason no era-specific column can leak into the canonical row.

Everything else mirrors `bhavcopy_legacy`: the header must be *exactly* this era's thirty-four
columns, every row as wide as the header, numbers read as text and converted to exact `Decimal`
(never `float`, and rejecting `NaN`/`Infinity`/signs before `Decimal` can accept them), `SctySrs`
kept verbatim with nothing filtered, and a file whose rows span more than one session refused.

One field crosses the eras differently, and reconciling it is what makes the two schemas truly
identical rather than merely similar. The legacy file always wrote `LAST`, using `0` for a security
with no last-traded-price snapshot (models.py: "0 on series where nothing traded late"). The UDiFF
file publishes the *same* fact as an **empty `LastPric`** — seen on real exchange rows (e.g. an `AT`
debt line on 08-Jul-2024 that traded a little but carries no last snapshot). So an empty `LastPric`
is normalised to `Decimal(0)`: the identical canonical value the legacy era produced for the
identical condition, which is the whole point of the dual parser. This normalisation is deliberately
narrow — it applies to `LastPric` alone. Every other price and count is required, because those are
never empty in a real cash file and an empty one there is the signature of a truncated download, not
a "did not trade" state, and must fail loudly rather than become a silent zero.

Output is `PriceRow`, the identical schema `bhavcopy_legacy` (M1.4) emits, so nothing downstream can
tell which era a row came from — which is the whole point of a dual parser (§4.1, "dual parser
required"). The dispatcher in `bhavcopy.py` chooses this parser or the legacy one by date.

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
    "UDIFF_COLUMNS",
    "UDIFF_ERA_START",
    "UDIFF_SOURCE_ID",
    "parse",
    "parse_l0",
    "parse_text",
]

_LOG = get_logger(__name__)

#: The register id this parser serves (`source_register.yaml`, `parser.task: M1.5`).
UDIFF_SOURCE_ID: Final = "nse_bhavcopy_udiff"

#: The first session published in this format — the 08-Jul-2024 cutover (`source_register.yaml`
#: `era.start`). The legacy `cm{DD}{MON}{YYYY}` URL 404s from this date on; this one begins here.
#: Which parser serves the boundary date is the dispatcher's call (`bhavcopy.py`), not this file's.
UDIFF_ERA_START: Final = date(2024, 7, 8)

#: The era's header, exactly — all thirty-four UDiFF columns in order, shared verbatim with the F&O
#: bhavcopy. The cash file populates the equity columns and leaves the derivative ones empty; the
#: per-row `Sgmt`/`FinInstrmTp` guards below, not this list, are what refuse an F&O file.
UDIFF_COLUMNS: Final = (
    "TradDt",
    "BizDt",
    "Sgmt",
    "Src",
    "FinInstrmTp",
    "FinInstrmId",
    "ISIN",
    "TckrSymb",
    "SctySrs",
    "XpryDt",
    "FininstrmActlXpryDt",
    "StrkPric",
    "OptnTp",
    "FinInstrmNm",
    "OpnPric",
    "HghPric",
    "LwPric",
    "ClsPric",
    "LastPric",
    "PrvsClsgPric",
    "UndrlygPric",
    "SttlmPric",
    "OpnIntrst",
    "ChngInOpnIntrst",
    "TtlTradgVol",
    "TtlTrfVal",
    "TtlNbOfTxsExctd",
    "SsnId",
    "NewBrdLotQty",
    "Rmks",
    "Rsvd1",
    "Rsvd2",
    "Rsvd3",
    "Rsvd4",
)

#: The segment and instrument type that define "the NSE cash equity bhavcopy". Every row must carry
#: these; anything else (an F&O file's `FO`/`FUTSTK`, an index row) is a format the cash parser
#: refuses rather than coerces.
_CASH_SEGMENT: Final = "CM"
_EQUITY_INSTRUMENT: Final = "STK"

#: Columns that only a derivative has. On an equity (`STK`) row they are always empty; data in any
#: of them means the row is not the cash instrument it claims to be.
_DERIVATIVE_ONLY: Final = ("XpryDt", "StrkPric", "OptnTp")

#: A plain non-negative decimal literal — all a price field ever holds. Narrower than `Decimal()`,
#: which would accept `NaN`, `Infinity`, `1E9` and `+3`; each is rejected here with a line number
#: rather than smuggled into a price. (Identical to the legacy parser's; the fields are the same
#: shape across eras even though their column names changed.)
_DECIMAL_LITERAL: Final = re.compile(r"^\d+(\.\d+)?$")

#: A plain non-negative integer literal. Narrower than `int()`, which accepts `1_000` and `+7`.
_INTEGER_LITERAL: Final = re.compile(r"^\d+$")

#: `TradDt`/`BizDt` are ISO calendar dates, `YYYY-MM-DD`. Parsed explicitly rather than via
#: `date.fromisoformat`, so `2024-7-8` or `2024-07-08T00:00` is a located error, not a lenient read.
_ISO_DATE: Final = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")

#: The zip local-file-header magic — tells "a zipped bhavcopy" from "the CSV inside one". Both are
#: things a caller legitimately has: L0 holds the zip the source served, a recovery the member.
_ZIP_MAGIC: Final = b"PK\x03\x04"


def parse(payload: bytes, *, filename: str) -> tuple[PriceRow, ...]:
    """Parse one UDiFF cash bhavcopy — the zip as served, or its CSV member — into canonical rows.

    Rows come back in file order, which is the order the exchange published them, so re-parsing an
    L0 payload is byte-for-byte reproducible.

    Assumes `payload` is one complete file: an EOD artefact of a few hundred kilobytes, held whole
    so a truncation is detected rather than streamed past. `filename` is used only to name the file
    in errors and logs — the rows carry their own date and it is authoritative.

    Raises `ParseError` — naming the file, and the line where the failure is attributable to one —
    for a corrupt archive, an unrecognised header, a short or wide row, a row that is not a cash
    equity (`Sgmt`/`FinInstrmTp`), a derivative column carrying data, a field that is not the number
    or date it must be, and a file whose rows do not all belong to one session.

    Never emits an era-specific field: only the thirteen facts of `PriceRow` are read, so a caller
    cannot tell a UDiFF row from a legacy one.
    """
    text = _text_of(payload, filename=filename)
    rows = parse_text(text, filename=filename)
    _LOG.info(
        "bhavcopy.parsed",
        source=UDIFF_SOURCE_ID,
        era="udiff",
        filename=filename,
        trade_date=rows[0].trade_date.isoformat(),
        rows=len(rows),
        state="NORMALIZED",
    )
    return rows


def parse_l0(store: L0Store, ref: L0Ref) -> tuple[PriceRow, ...]:
    """Parse the L0 payload a fetch produced, re-verifying its checksum on the way in.

    The pipeline's entry point for a UDiFF file: `Fetcher.fetch` returns an `L0Ref` and never bytes,
    so this is how a fetched bhavcopy becomes rows. `L0Store.get` re-hashes the payload, which is
    what makes "every L1 value is derived from bytes that have not changed" true at the point of
    derivation rather than only at the point of fetch.
    """
    return parse(store.get(ref), filename=ref.filename)


def parse_text(text: str, *, filename: str) -> tuple[PriceRow, ...]:
    """Parse the decoded CSV body. Separated from `parse` so a caller can hand over text it already
    has (a recovery from a manually unzipped file), so the archive handling above has one job."""
    reader = csv.reader(io.StringIO(text))
    _check_header(next(reader, None), filename=filename)

    rows: list[PriceRow] = []
    for record in reader:
        if not record or not any(field.strip() for field in record):
            # A trailing newline, not a row. Anything with content in it must be a full record.
            continue
        rows.append(_row(record, line=reader.line_num, filename=filename))

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


def _check_header(header: list[str] | None, *, filename: str) -> None:
    """Check the header is exactly this era's thirty-four columns.

    Not "must contain" — must be, in order. The legacy `cm…bhav.csv` header and the pre-ISIN
    sub-era header both differ here and are refused, and so is any future column addition, which is
    a format change to stop on rather than to read past.
    """
    if header is None:
        raise ParseError("file is empty; expected the bhavcopy header row", filename=filename)

    named = tuple(field.strip() for field in header)
    if named != UDIFF_COLUMNS:
        raise ParseError(
            f"unexpected header {','.join(named)!r}; this parser reads the post-08-Jul-2024 NSE "
            f"UDiFF cash bhavcopy, whose header is exactly {','.join(UDIFF_COLUMNS)!r}. The legacy "
            "era (M1.4) and any other column layout are refused on purpose",
            filename=filename,
            line=1,
        )


def _row(record: list[str], *, line: int, filename: str) -> PriceRow:
    """Turn one CSV record into a `PriceRow`, or say precisely which line and field was wrong."""
    if len(record) != len(UDIFF_COLUMNS):
        raise ParseError(
            f"row has {len(record)} fields, header has {len(UDIFF_COLUMNS)}; a short row here is "
            "what a truncated download looks like",
            filename=filename,
            line=line,
        )

    field = dict(zip(UDIFF_COLUMNS, (value.strip() for value in record), strict=False))

    if field["Sgmt"] != _CASH_SEGMENT or field["FinInstrmTp"] != _EQUITY_INSTRUMENT:
        raise ParseError(
            f"row is Sgmt={field['Sgmt']!r} FinInstrmTp={field['FinInstrmTp']!r}, not the cash "
            f"equity ({_CASH_SEGMENT}/{_EQUITY_INSTRUMENT}) this parser reads; a derivatives "
            "bhavcopy shares this exact header and must not be read as equities",
            filename=filename,
            line=line,
        )
    for column in _DERIVATIVE_ONLY:
        if field[column]:
            raise ParseError(
                f"equity row carries {column}={field[column]!r}; a cash instrument has no expiry, "
                "strike or option type — this is not the file it claims to be",
                filename=filename,
                line=line,
            )

    try:
        return PriceRow(
            isin=field["ISIN"],
            symbol=field["TckrSymb"],
            series=field["SctySrs"],
            trade_date=_date(field["TradDt"], column="TradDt", line=line, filename=filename),
            open=_decimal(field["OpnPric"], column="OpnPric", line=line, filename=filename),
            high=_decimal(field["HghPric"], column="HghPric", line=line, filename=filename),
            low=_decimal(field["LwPric"], column="LwPric", line=line, filename=filename),
            close=_decimal(field["ClsPric"], column="ClsPric", line=line, filename=filename),
            last=_last_price(field["LastPric"], line=line, filename=filename),
            prev_close=_decimal(
                field["PrvsClsgPric"], column="PrvsClsgPric", line=line, filename=filename
            ),
            total_traded_qty=_integer(
                field["TtlTradgVol"], column="TtlTradgVol", line=line, filename=filename
            ),
            total_traded_value=_decimal(
                field["TtlTrfVal"], column="TtlTrfVal", line=line, filename=filename
            ),
            total_trades=_integer(
                field["TtlNbOfTxsExctd"], column="TtlNbOfTxsExctd", line=line, filename=filename
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


def _last_price(value: str, *, line: int, filename: str) -> Decimal:
    """`LastPric` as an exact `Decimal`, mapping the era's empty "no last snapshot" to `Decimal(0)`.

    UDiFF leaves `LastPric` empty for a security with no last-traded-price snapshot; the legacy era
    wrote `0` for the same condition. Normalising empty to `0` here keeps the two eras' `last` field
    byte-for-byte identical in meaning. Any *non-empty* value goes through the strict decimal check,
    so a malformed last price is still a located error — only the genuinely-empty case is absorbed,
    and only for this one field (see the module docstring).
    """
    if value == "":
        return Decimal(0)
    return _decimal(value, column="LastPric", line=line, filename=filename)


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
    """Parse an ISO `YYYY-MM-DD` exchange date into a trading date."""
    match = _ISO_DATE.match(value)
    if match is None:
        raise ParseError(
            f"{column} is {value!r}, which is not a YYYY-MM-DD exchange date",
            filename=filename,
            line=line,
        )
    year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
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
