"""NSE F&O EOD bhavcopy (§4.1 row 12) - `BhavCopy_NSE_FO_0_0_0_{YYYYMMDD}_F_0000.csv.zip`.

The derivatives sibling of the cash UDiFF bhavcopy (M1.5). Same ISO-20022 "UDiFF" container: one
zip, one CSV member, the *same thirty-four column header byte-for-byte* - the cash file simply
leaves the derivative columns empty, and this file fills them in. That shared header is why the two
parsers must guard per row, not by header: `bhavcopy_udiff` refuses any row that is not
`Sgmt == CM`/`FinInstrmTp == STK`, and this parser refuses any row that is not `Sgmt == FO`. Hand
this parser a cash file and it stops on the first data line rather than mis-reading equities as
contracts, and vice-versa.

**This data is sentiment context only - it is never traded** (EXECUTION_PLAN §4.1 row 12, decision
set: "Free; sentiment context only, no derivatives trading"). The parser and the aggregates
(`dataplatform.store.fo_aggregates`) are pure derivation: bytes to contract rows to per-underlying
aggregates. Neither imports `execution`, and neither exposes anything that could place, size, or
route an order. That is the whole reason F&O earns a data module and not a strategy.

What the file carries that the cash file does not, and that the aggregates exist to read:

* **`OpnIntrst` / `ChngInOpnIntrst`** - open interest per contract and its one-day change. Summed
  per underlying they are the total-OI and OI-change aggregates; split by `OptnTp` they are the
  put/call ratio. `ChngInOpnIntrst` is the one field that is legitimately *signed* - OI falls as
  well as rises - so it alone is parsed with a signed-integer guard; `OpnIntrst` is a count and
  non-negative.
* **`UndrlygPric`** - the underlying's spot, stamped on every derivative row by the exchange. It
  lets futures basis be computed *inside this one file* (near-future settlement minus spot) with no
  cross-source join, so invariant #2 is never engaged: nothing here joins on a symbol to fetch a
  price. (The identity master still resolves a stock underlying's symbol to its ISIN so the
  *aggregate* is ISIN-addressable - that resolution lives in `fo_aggregates`, and it is the only
  symbol-to-ISIN step, through the sanctioned D2 path.)
* **`XpryDt` / `StrkPric` / `OptnTp`** - the contract's expiry, strike and call/put flag.
  `FinInstrmTp` is authoritative for whether a row is a future or an option, and `StrkPric`/`OptnTp`
  are validated *against* it: a future carrying a strike, or an option missing one, is a
  contradiction the parser refuses rather than reads.

Output is `FoContractRow` - the canonical contract-level L1 row, one per traded contract, raw values
exactly as published (invariant #3, no adjustment here). Offline by construction: this module takes
bytes, or an `L0Ref` it reads back through `L0Store`. It never fetches - the crawl engine
(`dataplatform.ingest.fetcher`) is the only thing that does.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from dataplatform.ingest.models import ISIN_PATTERN, ParseError
from dataplatform.ingest.nse.bhavcopy_udiff import UDIFF_COLUMNS
from dataplatform.logging import get_logger
from dataplatform.store.l0 import L0Ref, L0Store

__all__ = [
    "FO_COLUMNS",
    "FO_ERA_START",
    "FO_SOURCE_ID",
    "FoContractRow",
    "FoInstrumentType",
    "OptionType",
    "parse",
    "parse_l0",
    "parse_text",
]

_LOG = get_logger(__name__)

#: The register id this parser serves (`source_register.yaml` id `nse_fo_bhavcopy`, task M3.7).
FO_SOURCE_ID: Final = "nse_fo_bhavcopy"

#: The first session published in this UDiFF format - the same 08-Jul-2024 cutover as the cash file
#: (`source_register.yaml` `era.start`).
FO_ERA_START: Final = date(2024, 7, 8)

#: The era's header - the identical thirty-four UDiFF columns the cash bhavcopy ships (M1.5). Shared
#: verbatim on purpose: the derivative file populates the columns the cash file leaves empty, so the
#: header alone cannot tell the two apart and the `Sgmt == FO` guard below is what does.
FO_COLUMNS: Final = UDIFF_COLUMNS

#: The segment that defines "the NSE equity-derivatives bhavcopy". Every row must carry it; a cash
#: row (`CM`) shares this exact header and must not be read as a contract.
_FO_SEGMENT: Final = "FO"


class FoInstrumentType(StrEnum):
    """The `FinInstrmTp` values an equity-derivatives bhavcopy carries.

    Authoritative for whether a row is a future or an option and whether its underlying is an index
    or a single stock - `StrkPric`/`OptnTp` are validated against this, never the other way round.
    An `FO` row whose type is none of these is a format change the parser stops on, not guesses.
    """

    FUTIDX = "FUTIDX"  # index future
    FUTSTK = "FUTSTK"  # single-stock future
    FUTIVX = "FUTIVX"  # volatility-index (India VIX) future
    OPTIDX = "OPTIDX"  # index option
    OPTSTK = "OPTSTK"  # single-stock option


class OptionType(StrEnum):
    """Call or put - the `OptnTp` of an option row. Futures carry no option type."""

    CE = "CE"
    PE = "PE"


#: The instrument types that are futures, and the ones that are options. Every `FoInstrumentType` is
#: in exactly one set; the aggregate reads futures for basis/rollover and options for the PCR.
_FUTURE_TYPES: Final = frozenset(
    {FoInstrumentType.FUTIDX, FoInstrumentType.FUTSTK, FoInstrumentType.FUTIVX}
)
_OPTION_TYPES: Final = frozenset({FoInstrumentType.OPTIDX, FoInstrumentType.OPTSTK})

#: The instrument types whose underlying is an index (no ISIN - an index is not a security) versus a
#: single stock (which resolves to an ISIN through the identity master in `fo_aggregates`).
_INDEX_TYPES: Final = frozenset(
    {FoInstrumentType.FUTIDX, FoInstrumentType.FUTIVX, FoInstrumentType.OPTIDX}
)

#: `OptnTp` values that mean "this row is not an option" - blank in the UDiFF file, and `XX` in the
#: legacy convention some tooling still emits. Either is accepted on a futures row, refused on an
#: option row.
_NON_OPTION_MARKERS: Final = frozenset({"", "XX"})

#: A plain non-negative decimal literal - all a price or strike field ever holds. Narrower than
#: `Decimal()`, which would accept `NaN`, `Infinity`, `1E9` and `+3`.
_DECIMAL_LITERAL: Final = re.compile(r"^\d+(\.\d+)?$")

#: A plain non-negative integer literal (open interest, volumes, trade counts).
_INTEGER_LITERAL: Final = re.compile(r"^\d+$")

#: A signed integer literal - only `ChngInOpnIntrst` needs it, because open interest genuinely falls
#: as well as rises. Every other count is non-negative and uses `_INTEGER_LITERAL`.
_SIGNED_INTEGER_LITERAL: Final = re.compile(r"^-?\d+$")

#: `TradDt`/`BizDt`/`XpryDt` are ISO calendar dates, `YYYY-MM-DD`. Parsed explicitly so a lenient
#: variant (`2024-7-8`, a trailing time) is a located error rather than a silent read.
_ISO_DATE: Final = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")

#: The zip local-file-header magic - tells "a zipped bhavcopy" from "the CSV inside one". Both are
#: things a caller legitimately has: L0 holds the zip the source served, a recovery the member.
_ZIP_MAGIC: Final = b"PK\x03\x04"


class FoContractRow(BaseModel):
    """One traded derivative contract on one session - the canonical F&O L1 row.

    What it does: carry exactly the facts the F&O bhavcopy publishes about one contract, in the
    types the aggregates are allowed to compute with (`Decimal` for every price, `int` for counts).
    What it assumes: the parser that built it has already validated the file's structure and the
    row's internal consistency (a future has no strike, an option has one), so a row that exists is
    a contract the exchange really published.
    What it never does: hold an adjusted price, a derived field, or anything that could route an
    order. `underlying` is the exchange's own `TckrSymb` for the contract's underlier - grouping by
    it is grouping a file by its own field, not a cross-source symbol join; the ISIN of a stock
    underlier is attached later, by the aggregate, through the D2 identity master (invariant #2).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    trade_date: date = Field(description="the exchange session this row is about (Asia/Kolkata)")
    underlying: str = Field(min_length=1, description="TckrSymb - the underlier, e.g. NIFTY")
    instrument_type: FoInstrumentType = Field(
        description="FinInstrmTp - authoritative fut/opt flag"
    )
    expiry: date = Field(description="XpryDt - the contract's expiry date")
    strike: Decimal | None = Field(
        default=None,
        ge=0,
        description="StrkPric - the option strike; None for a future",
    )
    option_type: OptionType | None = Field(
        default=None, description="OptnTp - CE/PE; None for a future"
    )

    close: Decimal = Field(ge=0, description="ClsPric - closing price, unadjusted, as published")
    settle: Decimal = Field(
        ge=0, description="SttlmPric - settlement price, the canonical EOD mark"
    )
    underlying_price: Decimal = Field(ge=0, description="UndrlygPric - the underlier's spot")

    open_interest: int = Field(ge=0, description="OpnIntrst - open interest, non-negative")
    change_in_oi: int = Field(description="ChngInOpnIntrst - one-day OI change, signed")

    total_traded_qty: int = Field(ge=0, description="TtlTradgVol - contracts traded in the session")
    total_traded_value: Decimal = Field(ge=0, description="TtlTrfVal - turnover in rupees")
    total_trades: int = Field(ge=0, description="TtlNbOfTxsExctd - number of trades executed")

    isin: str | None = Field(
        default=None,
        pattern=ISIN_PATTERN,
        description="ISIN as the file states it - usually empty for a derivative, kept if present",
    )

    @property
    def is_future(self) -> bool:
        """True when this row is a future (and therefore carries no strike or option type)."""
        return self.instrument_type in _FUTURE_TYPES

    @property
    def is_option(self) -> bool:
        """True when this row is an option (and therefore carries a strike and a CE/PE flag)."""
        return self.instrument_type in _OPTION_TYPES

    @property
    def is_index_underlying(self) -> bool:
        """True when the underlier is an index (no ISIN) rather than a single stock."""
        return self.instrument_type in _INDEX_TYPES


def parse(payload: bytes, *, filename: str) -> tuple[FoContractRow, ...]:
    """Parse one F&O bhavcopy - the zip as served, or its CSV member - into contract rows.

    Rows come back in file order, so re-parsing an L0 payload is byte-for-byte reproducible.

    Assumes `payload` is one complete file: an EOD artefact of ~1 MB, held whole so a truncation is
    detected rather than streamed past. `filename` names the file in errors and logs only - the rows
    carry their own date and it is authoritative.

    Raises `ParseError` - naming the file, and the line where the failure is attributable to one -
    for a corrupt archive, an unrecognised header, a short or wide row, a row that is not an F&O
    contract (`Sgmt`), an unknown instrument type, a future carrying a strike or an option missing
    one, a field that is not the number or date it must be, and a file spanning several sessions.
    """
    text = _text_of(payload, filename=filename)
    rows = parse_text(text, filename=filename)
    _LOG.info(
        "fo_bhavcopy.parsed",
        source=FO_SOURCE_ID,
        era="udiff",
        filename=filename,
        trade_date=rows[0].trade_date.isoformat(),
        rows=len(rows),
        state="NORMALIZED",
    )
    return rows


def parse_l0(store: L0Store, ref: L0Ref) -> tuple[FoContractRow, ...]:
    """Parse the L0 payload a fetch produced, re-verifying its checksum on the way in.

    The pipeline's entry point for an F&O file: `Fetcher.fetch` returns an `L0Ref` and never bytes,
    so this is how a fetched bhavcopy becomes rows. `L0Store.get` re-hashes the payload, which is
    what makes "every L1 value is derived from bytes that have not changed" true at derivation time.
    """
    return parse(store.get(ref), filename=ref.filename)


def parse_text(text: str, *, filename: str) -> tuple[FoContractRow, ...]:
    """Parse the decoded CSV body. Separated from `parse` so a caller can hand over text it already
    has (a recovery from a manually unzipped file), and so the archive handling has one job."""
    reader = csv.reader(io.StringIO(text))
    _check_header(next(reader, None), filename=filename)

    rows: list[FoContractRow] = []
    for record in reader:
        if not record or not any(field.strip() for field in record):
            # A trailing newline, not a row. Anything with content in it must be a full record.
            continue
        rows.append(_row(record, line=reader.line_num, filename=filename))

    if not rows:
        raise ParseError(
            "no data rows after the header; a session's F&O bhavcopy always has some",
            filename=filename,
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
                f"not a readable zip archive ({exc}); the download is corrupt or truncated - "
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
    """Check the header is exactly this era's thirty-four columns - the shared UDiFF layout.

    Not "must contain" - must be, in order. Any column addition, removal or reorder is a format
    change to stop on rather than read past.
    """
    if header is None:
        raise ParseError("file is empty; expected the bhavcopy header row", filename=filename)

    named = tuple(field.strip() for field in header)
    if named != FO_COLUMNS:
        raise ParseError(
            f"unexpected header {','.join(named)!r}; this parser reads the NSE UDiFF F&O bhavcopy, "
            f"whose header is exactly {','.join(FO_COLUMNS)!r} (identical to the cash file's - the "
            "per-row Sgmt guard, not the header, is what refuses a cash file)",
            filename=filename,
            line=1,
        )


def _row(record: list[str], *, line: int, filename: str) -> FoContractRow:
    """Turn one CSV record into an `FoContractRow`, or say which line and field was wrong."""
    if len(record) != len(FO_COLUMNS):
        raise ParseError(
            f"row has {len(record)} fields, header has {len(FO_COLUMNS)}; a short row here is what "
            "a truncated download looks like",
            filename=filename,
            line=line,
        )

    field = dict(zip(FO_COLUMNS, (value.strip() for value in record), strict=False))

    if field["Sgmt"] != _FO_SEGMENT:
        raise ParseError(
            f"row is Sgmt={field['Sgmt']!r}, not the F&O segment ({_FO_SEGMENT!r}) this parser "
            "reads; the cash bhavcopy shares this exact header and must not be read as contracts",
            filename=filename,
            line=line,
        )

    instrument_type = _instrument_type(field["FinInstrmTp"], line=line, filename=filename)
    strike, option_type = _contract_shape(
        instrument_type, field["StrkPric"], field["OptnTp"], line=line, filename=filename
    )

    try:
        return FoContractRow(
            trade_date=_date(field["TradDt"], column="TradDt", line=line, filename=filename),
            underlying=field["TckrSymb"],
            instrument_type=instrument_type,
            expiry=_date(field["XpryDt"], column="XpryDt", line=line, filename=filename),
            strike=strike,
            option_type=option_type,
            close=_decimal(field["ClsPric"], column="ClsPric", line=line, filename=filename),
            settle=_decimal(field["SttlmPric"], column="SttlmPric", line=line, filename=filename),
            underlying_price=_decimal(
                field["UndrlygPric"], column="UndrlygPric", line=line, filename=filename
            ),
            open_interest=_integer(
                field["OpnIntrst"], column="OpnIntrst", line=line, filename=filename
            ),
            change_in_oi=_signed_integer(
                field["ChngInOpnIntrst"], column="ChngInOpnIntrst", line=line, filename=filename
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
            isin=field["ISIN"] or None,
        )
    except ValidationError as exc:
        raise ParseError(
            f"row is not a valid contract row: {exc.errors(include_url=False)}",
            filename=filename,
            line=line,
        ) from exc


def _instrument_type(value: str, *, line: int, filename: str) -> FoInstrumentType:
    """Map `FinInstrmTp` to the enum, refusing an F&O type this parser does not know."""
    try:
        return FoInstrumentType(value)
    except ValueError as exc:
        known = ", ".join(member.value for member in FoInstrumentType)
        raise ParseError(
            f"FinInstrmTp is {value!r}, not an equity-derivatives instrument type ({known}); an "
            "unknown type is a format change to stop on, not to read past",
            filename=filename,
            line=line,
        ) from exc


def _contract_shape(
    instrument_type: FoInstrumentType,
    strike_raw: str,
    option_raw: str,
    *,
    line: int,
    filename: str,
) -> tuple[Decimal | None, OptionType | None]:
    """Validate strike/option against the instrument type, returning the typed pair.

    `FinInstrmTp` is authoritative: a future must carry no strike and no option type, an option
    must carry both. A future with a strike, or an option without one, is a contradiction - the row
    is not the contract its type claims - and is refused rather than coerced, the F&O analogue of
    the cash parser's "a derivative column on an equity row is a defect".
    """
    if instrument_type in _FUTURE_TYPES:
        if option_raw not in _NON_OPTION_MARKERS:
            raise ParseError(
                f"{instrument_type.value} row carries OptnTp={option_raw!r}; a future has no "
                "option type",
                filename=filename,
                line=line,
            )
        if strike_raw not in {"", "0"} and _nonzero_decimal(strike_raw):
            raise ParseError(
                f"{instrument_type.value} row carries StrkPric={strike_raw!r}; a future has no "
                "strike",
                filename=filename,
                line=line,
            )
        return None, None

    # An option: strike required and positive, option type required and CE/PE.
    if not strike_raw:
        raise ParseError(
            f"{instrument_type.value} row has no StrkPric; an option must state its strike",
            filename=filename,
            line=line,
        )
    strike = _decimal(strike_raw, column="StrkPric", line=line, filename=filename)
    try:
        option_type = OptionType(option_raw)
    except ValueError as exc:
        raise ParseError(
            f"{instrument_type.value} row carries OptnTp={option_raw!r}, not CE or PE",
            filename=filename,
            line=line,
        ) from exc
    return strike, option_type


def _nonzero_decimal(value: str) -> bool:
    """True when `value` is a decimal literal that is not zero - used to accept `0`/`0.00` as "no
    strike" on a future while still refusing a real strike."""
    if not _DECIMAL_LITERAL.match(value):
        return True  # not a clean zero; let the caller flag it as an unexpected strike
    return Decimal(value) != 0


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
    except InvalidOperation as exc:  # pragma: no cover - the pattern already excludes these
        raise ParseError(
            f"{column} is {value!r}, which Decimal cannot represent", filename=filename, line=line
        ) from exc


def _integer(value: str, *, column: str, line: int, filename: str) -> int:
    """Exact non-negative `int` for a plain integer literal, and a located error otherwise."""
    if not _INTEGER_LITERAL.match(value):
        raise ParseError(
            f"{column} is {value!r}, which is not a non-negative integer literal",
            filename=filename,
            line=line,
        )
    return int(value)


def _signed_integer(value: str, *, column: str, line: int, filename: str) -> int:
    """Exact `int` allowing a leading minus - only `ChngInOpnIntrst` is legitimately negative."""
    if not _SIGNED_INTEGER_LITERAL.match(value):
        raise ParseError(
            f"{column} is {value!r}, which is not an integer literal",
            filename=filename,
            line=line,
        )
    return int(value)


def _date(value: str, *, column: str, line: int, filename: str) -> date:
    """Parse an ISO `YYYY-MM-DD` exchange date."""
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


def _one_session(rows: list[FoContractRow], *, filename: str) -> None:
    """Refuse a file whose rows do not all belong to the same session.

    A bhavcopy *is* one session. Two trade dates in one file means a concatenation or a mis-served
    payload, and downstream partitions by date (§4.2) - so accepting it would spread one file across
    two partitions and leave both looking complete. (`XpryDt` legitimately varies; `trade_date` may
    not.)
    """
    dates = {row.trade_date for row in rows}
    if len(dates) > 1:
        raise ParseError(
            "rows span more than one session: "
            f"{', '.join(sorted(day.isoformat() for day in dates))}",
            filename=filename,
        )
