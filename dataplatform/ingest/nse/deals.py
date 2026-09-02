"""NSE bulk & block deals (§4.1 row 10) — `content/equities/bulk.csv` and `block.csv` → L1 `deals`.

Two rolling current-session CSVs, one row per reportable deal: a *bulk* deal is a single client's
trades in a security exceeding 0.5 % of listed shares in a session; a *block* deal is a large
negotiated trade struck in the block window. Both name the client, the side, the quantity and the
price. T0 monitoring reads them as a flow-anomaly signal on a held name (§5.4) — the same client
appearing on the tape day after day, or a large block against a thesis — which is the whole reason
they earn a dataset of their own rather than being noise on the bhavcopy.

Four facts about these files drive every decision here:

* **Two column shapes, one row model.** `bulk.csv` has eight columns; `block.csv` has seven — no
  `Remarks`. Both parse into the same `DealRow`, so no consumer downstream of D1 carries a
  bulk-vs-block branch; `deal_type` is the only thing that distinguishes them afterward, and a
  block row's `remarks` is `None` because the column does not exist, not because it held `-`.
* **There is no ISIN in the file.** It states a symbol and a security *name*, and NSE symbols are
  recycled and renamed. So a deal cannot be joined to a holding until it has been resolved through
  the D2 identity master with its own trade date — `resolve` below is that step and the only
  sanctioned symbol→ISIN path for this source (invariant #2). A deal is never keyed by symbol.
* **The client name is messy, so the raw string is kept verbatim.** Real files carry a double
  space inside a name (`R G FAMILY  TRUST`) and stray trailing punctuation the exchange left in
  the field (`ISTAA SECURITIES PRIVATE LIMITED  -`). T0's "same client accumulating" signal needs
  a stable form to match on, and an audit needs the exact string the exchange published; losing
  either is a defect. So every row carries `client_name` exactly as published *and* a derived
  `client_name_normalized` (upper-cased, whitespace-collapsed). Normalization is deliberately
  conservative — it does not strip corporate suffixes or the stray `-`, because that is entity
  resolution, a T0 concern, not an ingestion one, and a wording change absorbed silently at
  ingest is a wording change nobody can see went missing.
* **A deals file has no natural unique key and can legitimately be empty.** One client can appear
  many times for one symbol (several tranches at different prices), so rows are a *list* of events,
  never deduplicated. And a session with no reportable deals serves a header-only file, which is
  zero deals — a fact, not a truncation. Both are the opposite of the delivery file's rules, and
  getting them wrong would either drop real deals or reject real quiet days.

The endpoints are rolling "current session" files with no date parameter — the same shape as the
FII/DII feed (M3.4): depth is one session and history accrues forward, so the session date comes
from the file's own `Date` column and the L0 filename carries it (`l0_filename`) so two sessions
do not collide in one month's L0 directory.

Output is `DealRow` — symbol-keyed, no ISIN yet — which `resolve` turns into `ResolvedDealRow`s;
`write_l1` lands a session (bulk and block together) in one L1 partition and `deals_for` answers
the T0 question: the deals in a held ISIN on a date. Offline by construction: this module takes
bytes, or an `L0Ref` it reads back through `L0Store`, and never opens a socket.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Final

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from dataplatform.identity.master import Exchange, IdentityMaster
from dataplatform.ingest.models import ISIN_PATTERN, IngestError, ParseError
from dataplatform.ingest.source_register import SourceRegister
from dataplatform.ingest.source_register import load as load_register
from dataplatform.logging import get_logger
from dataplatform.store.l0 import L0Ref, L0Store
from dataplatform.store.paths import l1_partition_path

__all__ = [
    "BLOCK_COLUMNS",
    "BLOCK_SOURCE_ID",
    "BULK_COLUMNS",
    "BULK_SOURCE_ID",
    "DEALS_DATASET",
    "DealResolution",
    "DealRow",
    "DealSide",
    "DealType",
    "DealsDay",
    "ResolvedDealRow",
    "deals_for",
    "deals_url",
    "l0_filename",
    "normalize_client_name",
    "parse",
    "parse_l0",
    "parse_text",
    "read_l1",
    "resolve",
    "write_l1",
]

_LOG = get_logger(__name__)

#: The register ids this parser serves (`source_register.yaml`, `parser.task: M3.5`).
BULK_SOURCE_ID: Final = "nse_bulk_deals"
BLOCK_SOURCE_ID: Final = "nse_block_deals"

#: The L1 dataset — `data/L1/deals/date=YYYY-MM-DD/part.parquet` (§4.2). Bulk and block share it;
#: the `deal_type` column tells them apart, so T0 asks one dataset "what happened in this name".
DEALS_DATASET: Final = "deals"

#: The bulk file's header, exactly. Compared by name so a reordered or renamed column fails on
#: line 1 rather than shifting every value one slot right and being read as a plausible number.
BULK_COLUMNS: Final = (
    "Date",
    "Symbol",
    "Security Name",
    "Client Name",
    "Buy/Sell",
    "Quantity Traded",
    "Trade Price / Wght. Avg. Price",
    "Remarks",
)

#: The block file's header — the bulk columns without `Remarks`. A separate constant rather than a
#: slice so a change to either file is a change to exactly one line here.
BLOCK_COLUMNS: Final = BULK_COLUMNS[:-1]

#: The marker the exchange writes in an empty `Remarks` field. Modelled as `None`.
_ABSENT: Final = "-"

#: `Date` is `DD-Mon-YYYY` (e.g. `01-SEP-2026`). Mapped explicitly rather than via
#: `strptime('%b')`, whose month names follow `LC_TIME` — a parser whose output depends on the
#: process locale is not reproducible (same reasoning as `bhavcopy_legacy` and `delivery`).
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

#: A whole positive share count — all `Quantity Traded` ever is. Narrower than `int()`, and
#: thousands separators are tolerated because Indian feeds add them without notice.
_INTEGER_LITERAL: Final = re.compile(r"^\d+$")

#: A plain positive decimal literal — all a trade price ever is. Narrower than `Decimal()`, which
#: accepts `NaN`/`Infinity` and would let a mis-framed field become a price greater than every
#: other price.
_DECIMAL_LITERAL: Final = re.compile(r"^\d+(\.\d+)?$")

#: Runs of whitespace, for collapsing a client name into its normalized form.
_WHITESPACE: Final = re.compile(r"\s+")

#: A share count in a deal: whole shares, strictly positive, and strict so a float that lost
#: precision cannot round itself into a quantity. A deal that moved zero shares is not a deal.
DealQuantity = Annotated[int, Field(gt=0, strict=True)]

#: A traded price: strictly positive `Decimal`, never a float, never `NaN`/`Infinity`. A reported
#: deal always has a price, so `0` is a mis-parse rather than a real value here.
DealPrice = Annotated[Decimal, Field(gt=0, strict=True, allow_inf_nan=False)]


class DealType(StrEnum):
    """Which of the two files a row came from. The only thing that distinguishes them afterward."""

    BULK = "BULK"
    """A single client's session trades exceeding the bulk-deal disclosure threshold."""

    BLOCK = "BLOCK"
    """A large negotiated trade struck in the exchange block-deal window."""


#: Register id → deal type, so `parse` reads the source and never a caller's guess.
_TYPE_BY_SOURCE: Final = {BULK_SOURCE_ID: DealType.BULK, BLOCK_SOURCE_ID: DealType.BLOCK}


class DealSide(StrEnum):
    """The client's side of the deal. Normalized, so consumers never match on feed wording."""

    BUY = "BUY"
    SELL = "SELL"


#: Feed spellings → normalized side. Only `BUY`/`SELL` have been observed; a new token is a schema
#: change and must be looked at, not silently bucketed (`_side` raises on anything else).
_SIDE_ALIASES: Final = {"BUY": DealSide.BUY, "SELL": DealSide.SELL}


def normalize_client_name(raw: str) -> str:
    """Upper-case and collapse whitespace — the conservative normalization T0 matches on.

    What it does: `"R G FAMILY  TRUST"` → `"R G FAMILY TRUST"`; case-folds so `"HRTI"` and
    `"hrti"` are one client.
    What it assumes: nothing — it is a pure function of the string.
    What it never does: strip corporate suffixes (`LIMITED`, `LLP`), punctuation, or the stray `-`
    the exchange sometimes leaves in the field. That is entity resolution, a T0 concern; doing it
    here would discard signal ingestion has no business discarding, and the raw string is kept
    beside this so a consumer that wants to go further still can.
    """
    return _WHITESPACE.sub(" ", raw).strip().upper()


class DealRow(BaseModel):
    """One reported deal, before it has an ISIN.

    What it does: carry the facts one bulk- or block-deal line states — who, which side, how many,
    at what price — in the types the rest of the platform computes with, plus the normalized client
    name alongside the raw one.
    What it assumes: the parser has already checked the file's structure, so a `DealRow` that
    exists is a deal the exchange really published.
    What it never does: hold an ISIN (this source has none — see `resolve`), a price as a float, or
    a client name stripped of its raw form. `remarks` is `None` for every block row (the column
    does not exist) and for a bulk row whose `Remarks` was the `-` marker.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    deal_type: DealType = Field(description="BULK or BLOCK — which file this row came from")
    trade_date: date = Field(description="the session this deal was reported for (Asia/Kolkata)")
    symbol: str = Field(min_length=1, description="exchange ticker on `trade_date`, as published")
    security_name: str = Field(min_length=1, description="the exchange's security name, verbatim")
    client_name: str = Field(
        min_length=1, description="the deal's client, exactly as published — the source of truth"
    )
    client_name_normalized: str = Field(
        min_length=1, description="upper-cased, whitespace-collapsed client name for T0 matching"
    )
    side: DealSide = Field(description="normalized side: BUY or SELL")
    quantity: DealQuantity = Field(description="shares traded in the deal (Quantity Traded)")
    price: DealPrice = Field(description="trade price / weighted-average price, unadjusted")
    remarks: str | None = Field(
        default=None, description="bulk Remarks; None on a block row or a '-' bulk row"
    )


class ResolvedDealRow(BaseModel):
    """A `DealRow` after its symbol has become an ISIN through the D2 identity master.

    The shape T0 queries by `(isin, trade_date)` and the L1 partition stores. Carries its own
    `source` (the register id it came from) and `l0_key` so a row in a shared bulk+block partition
    still names the exact payload it was derived from (invariant #1).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    isin: str = Field(
        pattern=ISIN_PATTERN, description="resolved via IdentityMaster.resolve(symbol, trade_date)"
    )
    deal_type: DealType
    trade_date: date
    symbol: str = Field(min_length=1)
    security_name: str = Field(min_length=1)
    client_name: str = Field(min_length=1)
    client_name_normalized: str = Field(min_length=1)
    side: DealSide
    quantity: DealQuantity
    price: DealPrice
    remarks: str | None = None
    source: str = Field(min_length=1, description="Source Register id this row was fetched under")
    l0_key: str | None = Field(
        default=None, description="`source/date/filename` of the L0 payload this came from"
    )


@dataclass(frozen=True, slots=True)
class DealResolution:
    """The outcome of resolving a batch of deals to ISINs.

    `unresolved` holds the deals whose `(symbol, date)` the master had never seen — quarantined and
    counted rather than dropped, so a deal naming a security the identity master has not ingested
    yet is a visible gap, not silent data loss. An *ambiguous* symbol is not quarantined here:
    `IdentityMaster.resolve` raises `AmbiguousSymbolError` and queues the conflict, because "we
    know two contradictory ISINs" is a different fact from "we know none".
    """

    resolved: tuple[ResolvedDealRow, ...]
    unresolved: tuple[DealRow, ...]


class DealsDay(BaseModel):
    """One session's resolved deals — bulk and block together — ready to write to L1.

    What it does: hold every resolved deal for a single trade date, whichever file it came from, so
    the L1 partition for that date is written whole and once.
    What it assumes: every row is about the same session; the caller has resolved both files first.
    What it never does: exist spanning two dates. A row dated other than `trade_date` raises here,
    so a `DealsDay` that exists is a day that can be written to exactly one partition. Empty is
    legal: a session with no reportable deals is a real, writable fact.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    trade_date: date = Field(description="the session all rows are about")
    rows: tuple[ResolvedDealRow, ...] = Field(
        description="resolved deals in file order, bulk then block"
    )

    @model_validator(mode="after")
    def _one_session(self) -> DealsDay:
        wrong = [row for row in self.rows if row.trade_date != self.trade_date]
        if wrong:
            raise ValueError(
                f"{self.trade_date.isoformat()}: a deal dated "
                f"{wrong[0].trade_date.isoformat()} is in this day's rows"
            )
        return self


# ── parsing ──────────────────────────────────────────────────────────────────────────────────


def parse(payload: bytes, *, source: str, filename: str) -> tuple[DealRow, ...]:
    """Parse one bulk or block deals CSV into rows, in the file's own order.

    `source` is the register id (`nse_bulk_deals` / `nse_block_deals`), which selects the column
    set and sets `deal_type` — the caller states which file it fetched, never the parser guessing
    from the bytes. Assumes `payload` is one complete file (an EOD artefact of a few kilobytes), so
    a truncation is detected as a short row rather than streamed past. The session date comes from
    the file's `Date` column, never from `filename`.

    Raises `ParseError`, naming the file and the line when the fault is one record's, for a
    non-UTF-8 body, an unrecognised header, a short or wide row, a field that is not the number,
    date or side it must be, or rows spanning more than one session. An empty file (header only) is
    **not** an error — it is a session with no deals, and returns an empty tuple.
    """
    text = _text_of(payload, filename=filename)
    rows = parse_text(text, source=source, filename=filename)
    _LOG.info(
        "deals.parsed",
        source=source,
        filename=filename,
        deal_type=_TYPE_BY_SOURCE[_known_source(source, filename=filename)].value,
        rows=len(rows),
        trade_date=rows[0].trade_date.isoformat() if rows else None,
        state="VALIDATED",
    )
    return rows


def parse_l0(store: L0Store, ref: L0Ref, *, source: str) -> tuple[DealRow, ...]:
    """Parse the L0 payload a fetch produced, re-verifying its checksum on the way in.

    The pipeline entry point: `L0Store.get` re-hashes the payload, so "every L1 value derives from
    bytes that have not changed" holds where the derivation happens, not only at fetch.
    """
    return parse(store.get(ref), source=source, filename=ref.filename)


def parse_text(text: str, *, source: str, filename: str) -> tuple[DealRow, ...]:
    """Parse the decoded CSV body. Separated from `parse` so a caller can hand over text it already
    has and the decode handling above keeps exactly one job."""
    deal_type = _TYPE_BY_SOURCE[_known_source(source, filename=filename)]
    columns = BULK_COLUMNS if deal_type is DealType.BULK else BLOCK_COLUMNS

    reader = csv.reader(io.StringIO(text))
    _check_header(next(reader, None), columns=columns, deal_type=deal_type, filename=filename)

    rows: list[DealRow] = []
    for record in reader:
        if not record or not any(field.strip() for field in record):
            # A trailing newline, not a row.
            continue
        rows.append(
            _row(
                record,
                deal_type=deal_type,
                columns=columns,
                line=reader.line_num,
                filename=filename,
            )
        )

    # An empty result is legal and expected on a quiet day — deals files have no minimum row count,
    # unlike a delivery file, so returning () here is a fact, not a swallowed failure.
    _one_session(rows, filename=filename)
    return tuple(rows)


def resolve(
    rows: Iterable[DealRow],
    master: IdentityMaster,
    *,
    source: str,
    l0_key: str | None = None,
    exchange: Exchange = Exchange.NSE,
) -> DealResolution:
    """Resolve a batch of deals to ISINs through the D2 identity master.

    What it does: for each deal, looks up the ISIN that traded as `row.symbol` on `row.trade_date`
    via `IdentityMaster.try_resolve` — the only sanctioned symbol→ISIN path (invariant #2) — and
    stamps the row's `source`/`l0_key` provenance onto the resolved row, so a shared bulk+block
    partition still names the exact payload each row came from.
    What it assumes: `row.trade_date` is the session the symbol should be read as-of; a symbol is
    never resolved by name alone, because a recycled symbol resolves to different ISINs by date.
    What it never does: guess an ISIN. An unknown `(symbol, date)` is quarantined into `unresolved`
    and counted; an ambiguous one raises `AmbiguousSymbolError` from the master.
    """
    resolved: list[ResolvedDealRow] = []
    unresolved: list[DealRow] = []
    for row in rows:
        isin = master.try_resolve(row.symbol, row.trade_date, exchange=exchange)
        if isin is None:
            unresolved.append(row)
            continue
        resolved.append(
            ResolvedDealRow(
                isin=isin,
                deal_type=row.deal_type,
                trade_date=row.trade_date,
                symbol=row.symbol,
                security_name=row.security_name,
                client_name=row.client_name,
                client_name_normalized=row.client_name_normalized,
                side=row.side,
                quantity=row.quantity,
                price=row.price,
                remarks=row.remarks,
                source=source,
                l0_key=l0_key,
            )
        )
    if unresolved:
        _LOG.warning(
            "deals.unresolved",
            source=source,
            exchange=exchange.value,
            unresolved=len(unresolved),
            resolved=len(resolved),
        )
    return DealResolution(resolved=tuple(resolved), unresolved=tuple(unresolved))


# ── internals ────────────────────────────────────────────────────────────────────────────────


def _known_source(source: str, *, filename: str) -> str:
    """Reject a source id this parser does not serve, before it selects a column set."""
    if source not in _TYPE_BY_SOURCE:
        raise ParseError(
            f"unknown deals source {source!r}; this parser serves "
            f"{', '.join(sorted(_TYPE_BY_SOURCE))}",
            filename=filename,
        )
    return source


def _text_of(payload: bytes, *, filename: str) -> str:
    """Decode a payload to CSV text. This source ships a bare CSV, not an archive."""
    if payload.lstrip()[:1] == b"<":
        raise ParseError(
            "body is markup, not CSV — an HTML error page answered with a 200; it must not "
            "become deal rows",
            filename=filename,
        )
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ParseError(
            f"payload is not UTF-8 text at byte {exc.start} ({exc.reason})", filename=filename
        ) from exc


def _check_header(
    header: list[str] | None,
    *,
    columns: tuple[str, ...],
    deal_type: DealType,
    filename: str,
) -> None:
    """Refuse anything but this file's exact header.

    Comparing names to `columns` is what lets the rest of the parser address fields by name: a
    column the exchange adds, drops or reorders fails here, on line 1, rather than shifting every
    value one slot to the right and being read as a plausible number.
    """
    if header is None:
        raise ParseError(
            f"file is empty; expected the {deal_type.value.lower()}-deals header row",
            filename=filename,
        )
    named = tuple(field.strip() for field in header)
    if named != columns:
        raise ParseError(
            f"unexpected header {','.join(named)!r}; this parser reads the NSE "
            f"{deal_type.value.lower()}-deals file, whose columns are exactly "
            f"{','.join(columns)!r}",
            filename=filename,
            line=1,
        )


def _row(
    record: list[str],
    *,
    deal_type: DealType,
    columns: tuple[str, ...],
    line: int,
    filename: str,
) -> DealRow:
    """Turn one CSV record into a `DealRow`, or say precisely which line and field was wrong."""
    if len(record) != len(columns):
        raise ParseError(
            f"row has {len(record)} fields, header has {len(columns)}; a short row here is what a "
            "truncated download looks like",
            filename=filename,
            line=line,
        )

    field = dict(zip(columns, (value.strip() for value in record), strict=True))
    symbol = field["Symbol"]
    security_name = field["Security Name"]
    client_name = _raw(record, columns.index("Client Name"))
    if not symbol:
        raise ParseError("Symbol is empty", filename=filename, line=line)
    if not security_name:
        raise ParseError("Security Name is empty", filename=filename, line=line)
    if not client_name.strip():
        raise ParseError("Client Name is empty", filename=filename, line=line)

    try:
        return DealRow(
            deal_type=deal_type,
            trade_date=_date(field["Date"], line=line, filename=filename),
            symbol=symbol,
            security_name=security_name,
            client_name=client_name,
            client_name_normalized=normalize_client_name(client_name),
            side=_side(field["Buy/Sell"], line=line, filename=filename),
            quantity=_integer(
                field["Quantity Traded"], column="Quantity Traded", line=line, filename=filename
            ),
            price=_decimal(
                field["Trade Price / Wght. Avg. Price"],
                column="Trade Price / Wght. Avg. Price",
                line=line,
                filename=filename,
            ),
            remarks=_remarks(field.get("Remarks")),
        )
    except ValidationError as exc:
        raise ParseError(
            f"row is not a valid deal row: {exc.errors(include_url=False)}",
            filename=filename,
            line=line,
        ) from exc


def _raw(record: list[str], index: int) -> str:
    """The client-name field with only surrounding whitespace trimmed — internal spaces and the
    exchange's stray characters are kept, because the raw string is this dataset's source of truth
    (a double space or a trailing `-` is real and must survive into L1)."""
    return record[index].strip(" ")


def _remarks(value: str | None) -> str | None:
    """A bulk row's `Remarks`, or `None`. `None` for a block row (no column) and for the `-`
    marker; a real remark is kept verbatim."""
    if value is None:
        return None
    stripped = value.strip()
    if not stripped or stripped == _ABSENT:
        return None
    return stripped


def _side(value: str, *, line: int, filename: str) -> DealSide:
    """`BUY`/`SELL` → `DealSide`. A token nobody has seen is a schema change, not a bucket."""
    side = _SIDE_ALIASES.get(value.upper())
    if side is None:
        raise ParseError(
            f"Buy/Sell is {value!r}, expected one of {', '.join(sorted(_SIDE_ALIASES))}",
            filename=filename,
            line=line,
        )
    return side


def _integer(value: str, *, column: str, line: int, filename: str) -> int:
    """An exact positive share count. Thousands separators tolerated; anything else is located."""
    literal = value.replace(",", "")
    if not _INTEGER_LITERAL.match(literal):
        raise ParseError(
            f"{column} is {value!r}, which is not a non-negative integer",
            filename=filename,
            line=line,
        )
    return int(literal)


def _decimal(value: str, *, column: str, line: int, filename: str) -> Decimal:
    """An exact price. Thousands separators tolerated; `NaN`/`Infinity`/junk is a located error."""
    literal = value.replace(",", "")
    if not _DECIMAL_LITERAL.match(literal):
        raise ParseError(
            f"{column} is {value!r}, which is not a plain decimal price",
            filename=filename,
            line=line,
        )
    try:
        return Decimal(literal)
    except InvalidOperation as exc:  # pragma: no cover — the pattern already excludes these
        raise ParseError(
            f"{column} is {value!r}, which Decimal cannot represent", filename=filename, line=line
        ) from exc


def _date(value: str, *, line: int, filename: str) -> date:
    """Parse `DD-Mon-YYYY` into a trading date, locale-independently."""
    match = _DATE.match(value)
    month = match.group(2).upper() if match else ""
    if match is None or month not in _MONTHS:
        raise ParseError(
            f"Date is {value!r}, which is not a DD-Mon-YYYY exchange date",
            filename=filename,
            line=line,
        )
    day, year = int(match.group(1)), int(match.group(3))
    try:
        return date(year, _MONTHS[month], day)
    except ValueError as exc:
        raise ParseError(
            f"Date is {value!r}, which is not a real calendar date", filename=filename, line=line
        ) from exc


def _one_session(rows: Sequence[DealRow], *, filename: str) -> None:
    """Refuse a file whose rows do not all belong to the same session.

    A deals file is one session, and L1 partitions by date (§4.2); accepting two dates would spread
    one file across two partitions and leave both looking complete.
    """
    dates = {row.trade_date for row in rows}
    if len(dates) > 1:
        raise ParseError(
            "rows span more than one session: "
            f"{', '.join(sorted(day.isoformat() for day in dates))}",
            filename=filename,
        )


# ── L1 ───────────────────────────────────────────────────────────────────────────────────────

#: The L1 schema, declared once and enforced on write (§4.2, M1.8's rule). `decimal128(20, 4)`
#: carries the two-decimal price with headroom; a feed that started stating more precision than
#: that would fail the write loudly rather than have a price quietly rounded into L1.
_L1_SCHEMA: Final = pa.schema(
    [
        pa.field("isin", pa.string(), nullable=False),
        pa.field("deal_type", pa.string(), nullable=False),
        pa.field("trade_date", pa.date32(), nullable=False),
        pa.field("symbol", pa.string(), nullable=False),
        pa.field("security_name", pa.string(), nullable=False),
        pa.field("client_name", pa.string(), nullable=False),
        pa.field("client_name_normalized", pa.string(), nullable=False),
        pa.field("side", pa.string(), nullable=False),
        pa.field("quantity", pa.int64(), nullable=False),
        pa.field("price", pa.decimal128(20, 4), nullable=False),
        pa.field("remarks", pa.string(), nullable=True),
        pa.field("source", pa.string(), nullable=False),
        pa.field("l0_key", pa.string(), nullable=True),
    ]
)


def write_l1(day: DealsDay, *, data_root: Path | None = None) -> Path:
    """Write one session's deals to its L1 partition and return the file's path.

    Idempotent per `(dataset, date)`: rows go in the day's own order and the file is written whole
    to a temporary name then renamed over the target, so re-deriving a session from L0 produces the
    same bytes and a crash mid-write cannot leave a half partition readable.

    Raw prices only — this dataset has no adjusted analogue, so invariant #3 has nothing to breach
    here — and each row carries its `source`/`l0_key`, so a shared bulk+block partition still names
    the payload every row came from. An empty day writes an empty partition: "we looked, none" is a
    fact worth recording, distinct from a partition that was never written.
    """
    path = l1_partition_path(DEALS_DATASET, day.trade_date, data_root=data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(
        [
            {
                "isin": row.isin,
                "deal_type": row.deal_type.value,
                "trade_date": row.trade_date,
                "symbol": row.symbol,
                "security_name": row.security_name,
                "client_name": row.client_name,
                "client_name_normalized": row.client_name_normalized,
                "side": row.side.value,
                "quantity": row.quantity,
                "price": row.price,
                "remarks": row.remarks,
                "source": row.source,
                "l0_key": row.l0_key,
            }
            for row in day.rows
        ],
        schema=_L1_SCHEMA,
    )
    staging = path.with_name(f".{path.name}.partial")
    pq.write_table(table, staging, compression="snappy", version="2.6")
    staging.replace(path)
    _LOG.info(
        "deals.l1_written",
        dataset=DEALS_DATASET,
        trade_date=day.trade_date.isoformat(),
        path=str(path),
        rows=len(day.rows),
        state="NORMALIZED",
    )
    return path


def read_l1(trade_date: date, *, data_root: Path | None = None) -> DealsDay:
    """Read one session's deals back out of L1.

    The round trip `write_l1` is verified against, and the read path `deals_for` filters. Raises
    `FileNotFoundError` when the partition was never written — an absent partition is a gap for D7
    to explain, not an empty session (which is a written partition with zero rows).
    """
    path = l1_partition_path(DEALS_DATASET, trade_date, data_root=data_root)
    if not path.exists():
        raise FileNotFoundError(
            f"no {DEALS_DATASET} partition for {trade_date.isoformat()}: {path}"
        )
    records = pq.read_table(path, schema=_L1_SCHEMA).to_pylist()
    return DealsDay(
        trade_date=trade_date,
        rows=tuple(
            ResolvedDealRow(
                isin=record["isin"],
                deal_type=DealType(record["deal_type"]),
                trade_date=record["trade_date"],
                symbol=record["symbol"],
                security_name=record["security_name"],
                client_name=record["client_name"],
                client_name_normalized=record["client_name_normalized"],
                side=DealSide(record["side"]),
                quantity=record["quantity"],
                price=record["price"],
                remarks=record["remarks"],
                source=record["source"],
                l0_key=record["l0_key"],
            )
            for record in records
        ),
    )


def deals_for(
    isin: str, trade_date: date, *, data_root: Path | None = None
) -> tuple[ResolvedDealRow, ...]:
    """Every deal in `isin` on `trade_date` — the T0 flow-anomaly query (acceptance #2).

    What it does: reads the date's L1 partition and returns the deals whose resolved ISIN matches,
    in the partition's order. The join is on ISIN, never on symbol (invariant #2).
    What it assumes: the partition was written; a session never ingested raises `FileNotFoundError`
    rather than returning `()` and letting T0 mistake "not fetched" for "no deals".
    What it never does: resolve a symbol itself — the rows it returns were already ISIN-resolved at
    write time, so this is a pure lookup.
    """
    return tuple(row for row in read_l1(trade_date, data_root=data_root).rows if row.isin == isin)


# ── source register helpers ────────────────────────────────────────────────────────────────────


def deals_url(source: str, register: SourceRegister | None = None) -> str:
    """The endpoint for a deals source, read from the Source Register rather than repeated here.

    The register is where a URL is verified and where a change is recorded (C.1), so a second copy
    in code is a second thing to keep true. Neither template carries a placeholder — these are
    rolling current-session files, which is the whole reason their history is one session deep.
    """
    _known_source(source, filename=source)
    reg = load_register() if register is None else register
    entry = next((item for item in reg.sources if item.id == source), None)
    if entry is None:
        raise IngestError(f"no {source!r} entry in the Source Register")
    if "{" in entry.url_template:
        raise IngestError(
            f"{source}: url_template {entry.url_template!r} now carries a placeholder; if this "
            "feed has gained a date parameter, re-measure its history depth before using it"
        )
    return entry.url_template


def l0_filename(source: str, trade_date: date) -> str:
    """The L0 filename for one session's deals response.

    The URL carries no date, so L0 would otherwise be handed `bulk.csv` every day and the second
    session of a month would collide with the first (`L0Store.put`). The date goes in the name
    here, which is the only place it can.
    """
    stem = _TYPE_BY_SOURCE[_known_source(source, filename=source)].value.lower()
    return f"{stem}_{trade_date:%d%m%Y}.csv"
