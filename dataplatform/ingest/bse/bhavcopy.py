"""BSE cash-market bhavcopy across the UDiFF cutover (§4.1 row 4, "BSE equity OHLCV" — two eras).

BSE, like NSE, switched its EOD equity bhavcopy to the ISO-20022 "UDiFF" layout on 8 July 2024, and
the format either side of that date is different enough to need two parsers behind one dispatcher —
the exact shape M1.4/M1.5 solved for NSE, reproduced here for BSE. Both eras converge on the
platform's one canonical `PriceRow`, so nothing downstream of D1 can tell a BSE row from an NSE row,
or a UDiFF BSE row from a legacy one (invariant: one schema, `models.py`).

Two things differ from NSE and drive this module's shape:

* **The UDiFF file is a bare CSV, not a zip.** BSE serves
  `BhavCopy_BSE_CM_0_0_0_{YYYYMMDD}_F_0000.CSV` uncompressed, despite the sibling NSE URL shipping a
  `.csv.zip` (`source_register.yaml` → `bse_bhavcopy_udiff`, `parse_check`). The decoder here takes
  either — a bare CSV as served, or a zip a caller legitimately holds (L0 keeps bytes verbatim) — so
  a future BSE change to zipping does not silently break the parser. The header is the same 34
  UDiFF columns NSE emits, and the per-row `Sgmt`/`FinInstrmTp` guards match: a row must be
  `CM`/`STK`, the derivative columns must be empty, or the file is refused rather than mis-read.

* **The legacy file has no ISIN and no date.** `EQ{DDMMYY}_CSV.ZIP` is a zip of one 14-column CSV
  keyed on `SC_CODE` (the BSE scrip code), with `SC_NAME`/`SC_GROUP` but **no ISIN column and no
  timestamp column** (`source_register.yaml` → `bse_bhavcopy_legacy`, `pit_notes`). A legacy row
  therefore cannot become a `PriceRow` on its own — `PriceRow.isin` is required (invariant #2, ISIN
  is the only join key) — so this era parses to `BseLegacyQuote`, which carries the scrip code
  verbatim, and `resolve_legacy` turns those into `PriceRow`s by looking each `SC_CODE` up in the
  BSE scrip master (`scrip_master.py`, D2). A scrip the master does not know is quarantined and
  counted, never guessed — the same "never dropped silently" contract M1.8 holds for delivery rows.
  The session date, absent from the file, is supplied by the caller (it is `L0Ref.logical_date` on
  the real path, and it is what the `EQ{DDMMYY}` filename encodes).

The dispatcher (`parse`, `parse_l0`) serves the UDiFF era, whose rows carry ISIN natively and are
what the daily EOD pipeline and the recent-history backfill read. The legacy era is reached through
`parse_legacy`/`resolve_legacy` because it needs the scrip master; the full BSE legacy backfill is
gated behind B1/M1.13 (`AGENTIC_CONTEXT` §2 B1, §3.3) and joins that go, so wiring it into the
unattended runner is deliberately not done here.

Offline by construction: every entry point takes bytes, or an `L0Ref` it reads back through
`L0Store`. Nothing here fetches — the crawl engine (`dataplatform.ingest.fetcher`) is the only thing
that does.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Final, Literal

from pydantic import ValidationError

from dataplatform.ingest.models import ParseError, PriceRow
from dataplatform.logging import get_logger
from dataplatform.store.l0 import L0Ref, L0Store
from dataplatform.store.schemas import PriceQuarantineReason

__all__ = [
    "CUTOVER",
    "LEGACY_COLUMNS",
    "LEGACY_SOURCE_ID",
    "UDIFF_COLUMNS",
    "UDIFF_SOURCE_ID",
    "BseLegacyQuote",
    "Era",
    "LegacyParse",
    "LegacyResolution",
    "MalformedLine",
    "era_of",
    "parse",
    "parse_l0",
    "parse_legacy",
    "parse_legacy_report",
    "parse_legacy_text",
    "parse_legacy_text_report",
    "parse_udiff",
    "parse_udiff_text",
    "resolve_legacy",
    "split_merged_records",
]

_LOG = get_logger(__name__)

#: The register ids these parsers serve (`source_register.yaml`).
UDIFF_SOURCE_ID: Final = "bse_bhavcopy_udiff"
LEGACY_SOURCE_ID: Final = "bse_bhavcopy_legacy"

#: The first UDiFF session — the 08-Jul-2024 cutover, the same date as NSE's (`source_register.yaml`
#: `bse_bhavcopy_udiff` `era.start` / `bse_bhavcopy_legacy` `era.end`). On this date and after, BSE
#: publishes the UDiFF file; before it, the `EQ{DDMMYY}_CSV.ZIP` legacy file. The boundary belongs
#: to UDiFF: it is the first day the UDiFF file exists.
CUTOVER: Final = date(2024, 7, 8)

Era = Literal["legacy", "udiff"]

#: The UDiFF era's header, exactly — the same 34 columns NSE emits, in the same order
#: (`source_register.yaml` `bse_bhavcopy_udiff` `parse_check`: "identical UDiFF header to NSE").
#: The BSE cash file populates the equity columns and leaves the derivative ones empty; the per-row
#: `Sgmt`/`FinInstrmTp` guards below are what refuse a non-cash file, not this list.
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

#: The legacy era's header, exactly — 14 columns, `SC_CODE`-keyed, no ISIN, no timestamp
#: (`source_register.yaml` `bse_bhavcopy_legacy` `parse_check`).
LEGACY_COLUMNS: Final = (
    "SC_CODE",
    "SC_NAME",
    "SC_GROUP",
    "SC_TYPE",
    "OPEN",
    "HIGH",
    "LOW",
    "CLOSE",
    "LAST",
    "PREVCLOSE",
    "NO_TRADES",
    "NO_OF_SHRS",
    "NET_TURNOV",
    "TDCLOINDI",
)

#: The segment and instrument type that define "the BSE cash equity bhavcopy". Identical to NSE's:
#: the UDiFF header is shared with the F&O file, and only the per-row values separate them.
_CASH_SEGMENT: Final = "CM"
_EQUITY_INSTRUMENT: Final = "STK"

#: Columns only a derivative carries. On an equity (`STK`) row they are always empty; data in any of
#: them means the row is not the cash instrument it claims to be.
_DERIVATIVE_ONLY: Final = ("XpryDt", "StrkPric", "OptnTp")

#: A plain non-negative decimal literal — all a price field ever holds. Narrower than `Decimal()`,
#: which would accept `NaN`, `Infinity`, `1E9`, `+3`; each is rejected here with a line number.
_DECIMAL_LITERAL: Final = re.compile(r"^\d+(\.\d+)?$")

#: A plain non-negative integer literal. Narrower than `int()`, which accepts `1_000` and `+7`.
_INTEGER_LITERAL: Final = re.compile(r"^\d+$")

#: `TradDt`/`BizDt` are ISO calendar dates, `YYYY-MM-DD`, in the UDiFF era.
_ISO_DATE: Final = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")

#: A BSE scrip code — an integer, 6 digits in the modern range but shorter for old scrips. Kept as
#: text everywhere (leading digits matter as an identifier, never as a number).
_SCRIP_CODE: Final = re.compile(r"^\d{1,7}$")

#: The zip local-file-header magic — tells "a zipped bhavcopy" from "the bare CSV". Both are things
#: a caller legitimately has: BSE serves the UDiFF file bare and the legacy zipped, and L0 keeps
#: whichever the source served.
_ZIP_MAGIC: Final = b"PK\x03\x04"


@dataclass(frozen=True, slots=True)
class BseLegacyQuote:
    """One security's session in the legacy BSE bhavcopy — scrip-code-keyed, ISIN not yet known.

    What it is: the legacy file's row verbatim, with money as `Decimal` and counts as `int`. It is
    *not* a `PriceRow`: that era has no ISIN column, and a `PriceRow` without an ISIN would break
    invariant #2. `resolve_legacy` turns a batch of these into `PriceRow`s through the scrip master.

    `trade_date` is supplied by the caller, not read from the file: the legacy format has no
    timestamp column. On the real path it is `L0Ref.logical_date`, which is what the `EQ{DDMMYY}`
    filename encodes.
    """

    scrip_code: str
    scrip_name: str
    group: str
    trade_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    last: Decimal
    prev_close: Decimal
    total_trades: int
    total_traded_qty: int
    total_traded_value: Decimal
    #: `TDCLOINDI` verbatim — BSE's ex-event marker on the price row (`XD`, `XB`, `SS`, `SA`,
    #: `XR`, `CS`), `""` on an ordinary session. Never a factor: an independent witness to the
    #: corporate-action feed (`dataplatform.quality.ex_marker_witness`), carried into L1 by
    #: `dataplatform.ingest.session_attributes`.
    close_indicator: str = ""


@dataclass(frozen=True, slots=True)
class LegacyResolution:
    """The outcome of resolving a legacy batch to `PriceRow`s through the scrip master.

    `resolved` and `unresolved` reconcile to the input: `len(resolved) + len(unresolved)` equals the
    number of quotes in, so a caller can assert no row was dropped (the M1.8 "never silently"
    contract). `unresolved` holds the scrip codes the master did not know — a visible gap, counted,
    never a guessed ISIN.
    """

    resolved: tuple[PriceRow, ...]
    unresolved: tuple[BseLegacyQuote, ...]


@dataclass(frozen=True, slots=True)
class MalformedLine:
    """A legacy-file line the parser could not turn into quotes, kept whole with the reason why.

    What it is: the quarantine record for a line that is not one record — today only the
    run-together pair `split_merged_records` exists for, when the split cannot be proven. `text`
    is the line's fields re-joined on commas (the legacy file is unquoted, so that is the line as
    published), `scrip_code` and `group` are its first and third fields verbatim — possibly
    malformed, which is the point — and `detail` is the first check that failed.
    What it never is: a quote. Nothing on the line reaches `prices_raw`; the L0 payload keeps the
    bytes, and the L1 writer lands this in `prices_raw_quarantine` under `reason`.
    """

    line: int
    text: str
    scrip_code: str
    group: str
    reason: str
    detail: str


@dataclass(frozen=True, slots=True)
class LegacyParse:
    """One legacy session's parse: the quotes, and the lines that could not become quotes.

    Every data line of the payload is accounted for: it is one quote, or two quotes recovered from
    a merged line (`split_lines` names those lines), or one entry in `quarantined`. `len()` is the
    count a log line should report — quotes plus quarantined lines — so a quarantine never makes a
    session look smaller than the file it came from.
    """

    quotes: tuple[BseLegacyQuote, ...]
    quarantined: tuple[MalformedLine, ...] = ()
    split_lines: tuple[int, ...] = ()

    def __len__(self) -> int:
        return len(self.quotes) + len(self.quarantined)


def era_of(trade_date: date) -> Era:
    """Which format BSE published for `trade_date`. `< CUTOVER` is legacy, `>= CUTOVER` is UDiFF."""
    return "legacy" if trade_date < CUTOVER else "udiff"


# ── UDiFF era (native ISIN) ────────────────────────────────────────────────────────────────────


def parse(payload: bytes, *, filename: str, trade_date: date) -> tuple[PriceRow, ...]:
    """Parse one BSE UDiFF cash bhavcopy into canonical rows, checking it against `trade_date`.

    The dispatcher entry point for the current era. `trade_date` is the session the file is *for*
    (from the fetch — `L0Ref.logical_date` on the real path); the parsed rows carry their own date
    and this cross-checks the two agree, so a mis-served or misrouted payload is refused rather than
    scattered across the wrong L1 partition (§4.2). Legacy-era dates are refused here with a pointer
    to `parse_legacy`, because that era needs the scrip master to produce an ISIN.

    Raises `ParseError` for a legacy-era date, a corrupt archive, an unrecognised header, a short or
    wide row, a non-cash row, a derivative column carrying data, a malformed field, a multi-session
    file, and a contents/date mismatch.
    """
    if era_of(trade_date) == "legacy":
        raise ParseError(
            f"{trade_date.isoformat()} is before the BSE UDiFF cutover ({CUTOVER.isoformat()}); "
            "that era carries no ISIN and must go through parse_legacy + resolve_legacy",
            filename=filename,
        )
    rows = parse_udiff(payload, filename=filename)
    if rows[0].trade_date != trade_date:
        raise ParseError(
            f"file was dispatched as the {trade_date.isoformat()} session but its rows are dated "
            f"{rows[0].trade_date.isoformat()}; the payload does not match the date it was filed "
            "under",
            filename=filename,
        )
    return rows


def parse_l0(store: L0Store, ref: L0Ref) -> tuple[PriceRow, ...]:
    """Parse a BSE UDiFF L0 payload (UDiFF era only), re-checksumming it on the way in.

    The pipeline entry point for the current era: `L0Store.get` re-hashes the payload, so no row is
    derived from bytes that changed under L0. Dispatches on `ref.logical_date`, refusing a legacy
    date because that era's ISIN resolution needs the scrip master (`resolve_legacy`).
    """
    return parse(store.get(ref), filename=ref.filename, trade_date=ref.logical_date)


def parse_udiff(payload: bytes, *, filename: str) -> tuple[PriceRow, ...]:
    """Parse a BSE UDiFF payload — bare CSV as served, or a zip — into canonical rows, era-agnostic.

    Separated from `parse` so the format handling has one job and a caller that already knows the
    file is UDiFF (a fixture test) need not supply a date. Rows come back in file order, so
    re-parsing an L0 payload is byte-for-byte reproducible.
    """
    text = _text_of(payload, filename=filename)
    rows = parse_udiff_text(text, filename=filename)
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


def parse_udiff_text(text: str, *, filename: str) -> tuple[PriceRow, ...]:
    """Parse the decoded UDiFF CSV body into `PriceRow`s."""
    reader = csv.reader(io.StringIO(text))
    _check_udiff_header(next(reader, None), filename=filename)

    rows: list[PriceRow] = []
    for record in reader:
        if not record or not any(field.strip() for field in record):
            continue
        rows.append(_udiff_row(record, line=reader.line_num, filename=filename))

    if not rows:
        raise ParseError(
            "no data rows after the header; a session's bhavcopy always has some", filename=filename
        )
    _one_session([row.trade_date for row in rows], filename=filename)
    return tuple(rows)


# ── legacy era (scrip-code keyed, no ISIN) ───────────────────────────────────────────────────────


def parse_legacy(payload: bytes, *, filename: str, trade_date: date) -> tuple[BseLegacyQuote, ...]:
    """Parse one legacy BSE bhavcopy — the zip as served, or its CSV member — into scrip quotes.

    `trade_date` is required because the legacy file carries no timestamp column; it is the session
    the file is for, which the `EQ{DDMMYY}` filename encodes and which is `L0Ref.logical_date` on
    the real path. The quotes have no ISIN yet — feed them to `resolve_legacy` with the master.

    Two records run together on one line are split back apart when that is provably unambiguous
    (`split_merged_records`); when it is not, the line is quarantined and logged, never fatal. A
    caller that can persist the quarantine uses `parse_legacy_report`; this one returns the quotes.

    Raises `ParseError` for a UDiFF-era date, a corrupt archive, an unrecognised header, a short or
    wide row (other than the merged-pair width), and a malformed field.
    """
    return parse_legacy_report(payload, filename=filename, trade_date=trade_date).quotes


def parse_legacy_report(payload: bytes, *, filename: str, trade_date: date) -> LegacyParse:
    """`parse_legacy`, keeping the quarantined lines and the lines that were split.

    The backfill writer uses this so a quarantined line lands in `prices_raw_quarantine` rather
    than only in a log line.
    """
    if era_of(trade_date) == "udiff":
        raise ParseError(
            f"{trade_date.isoformat()} is on or after the BSE UDiFF cutover "
            f"({CUTOVER.isoformat()}); use parse (UDiFF) for this era",
            filename=filename,
        )
    text = _text_of(payload, filename=filename)
    parsed = parse_legacy_text_report(text, filename=filename, trade_date=trade_date)
    _LOG.info(
        "bhavcopy.parsed",
        source=LEGACY_SOURCE_ID,
        era="legacy",
        filename=filename,
        trade_date=trade_date.isoformat(),
        rows=len(parsed.quotes),
        split_lines=len(parsed.split_lines),
        quarantined=len(parsed.quarantined),
        state="NORMALIZED",
    )
    return parsed


def parse_legacy_text(text: str, *, filename: str, trade_date: date) -> tuple[BseLegacyQuote, ...]:
    """Parse the decoded legacy CSV body into scrip-keyed quotes for `trade_date`."""
    return parse_legacy_text_report(text, filename=filename, trade_date=trade_date).quotes


def parse_legacy_text_report(text: str, *, filename: str, trade_date: date) -> LegacyParse:
    """Parse the decoded legacy CSV body, splitting or quarantining merged-record lines."""
    reader = csv.reader(io.StringIO(text))
    _check_legacy_header(next(reader, None), filename=filename)

    quotes: list[BseLegacyQuote] = []
    quarantined: list[MalformedLine] = []
    split_lines: list[int] = []
    for record in reader:
        if not record or not any(field.strip() for field in record):
            continue
        line = reader.line_num
        if len(record) != _MERGED_WIDTH:
            quotes.append(_legacy_row(record, line=line, filename=filename, trade_date=trade_date))
            continue
        outcome = split_merged_records(record, line=line, filename=filename, trade_date=trade_date)
        if isinstance(outcome, MalformedLine):
            quarantined.append(outcome)
            _LOG.warning(
                "bhavcopy.legacy_line_quarantined",
                source=LEGACY_SOURCE_ID,
                filename=filename,
                trade_date=trade_date.isoformat(),
                line=line,
                reason=outcome.reason,
                detail=outcome.detail,
                text=outcome.text,
                state="QUARANTINED",
            )
        else:
            quotes.extend(outcome)
            split_lines.append(line)
            _LOG.warning(
                "bhavcopy.legacy_records_split",
                source=LEGACY_SOURCE_ID,
                filename=filename,
                trade_date=trade_date.isoformat(),
                line=line,
                scrip_codes=[quote.scrip_code for quote in outcome],
                state="VALIDATED",
            )

    if not quotes:
        raise ParseError(
            "no data rows after the header; a session's bhavcopy always has some", filename=filename
        )
    return LegacyParse(
        quotes=tuple(quotes), quarantined=tuple(quarantined), split_lines=tuple(split_lines)
    )


#: The width of two legacy records whose line terminator was lost. Every legacy row ends in
#: `TDCLOINDI`, which is empty on all but ~0.07% of rows, so losing the CRLF after an empty one
#: glues that empty field to the next row's `SC_CODE`: 13 + 14 = 27 fields. BSE published exactly
#: one such line in 1,943 legacy sessions (`EQ291221_CSV.ZIP`, line 1773, scrips 531358/531359).
_MERGED_WIDTH: Final = 2 * len(LEGACY_COLUMNS) - 1


def split_merged_records(
    record: Sequence[str], *, line: int, filename: str, trade_date: date
) -> tuple[BseLegacyQuote, BseLegacyQuote] | MalformedLine:
    """Split a line holding two run-together legacy records back into two quotes, or say why not.

    What it does: treats a `_MERGED_WIDTH`-field line as record one's first 13 fields, then record
    two's 14 — record one's empty `TDCLOINDI` is the empty string the lost terminator swallowed.
    It accepts the split only when it is unambiguous: field 14 must be a bare scrip code (a
    non-empty `TDCLOINDI` such as `XD` would have fused into `XD531359` and fails that), the two
    scrip codes must differ, and both halves must pass every check a normal row passes.
    What it assumes: the caller only hands it lines of exactly `_MERGED_WIDTH` fields.
    What it never does: guess. Any failed check returns a `MalformedLine` with reason
    `MERGED_RECORDS_UNSPLITTABLE` and the failure in `detail`, and neither half becomes a quote —
    half a line that is provably wrong is not trusted to be the right half.
    """
    width = len(LEGACY_COLUMNS)
    if len(record) != _MERGED_WIDTH:
        raise ValueError(f"expected a {_MERGED_WIDTH}-field line, got {len(record)}")

    def quarantine(detail: str) -> MalformedLine:
        return MalformedLine(
            line=line,
            text=",".join(record),
            scrip_code=record[0].strip(),
            group=record[2].strip(),
            reason=PriceQuarantineReason.MERGED_RECORDS_UNSPLITTABLE,
            detail=detail,
        )

    first = [*record[: width - 1], ""]
    second = list(record[width - 1 :])
    seam = second[0].strip()
    if not _SCRIP_CODE.match(seam):
        return quarantine(
            f"field {width} is {seam!r}, not a bare scrip code; the first record's TDCLOINDI and "
            "the second record's SC_CODE cannot be told apart"
        )
    if first[0].strip() == seam:
        return quarantine(f"both halves carry scrip {seam}; that is not two records")
    try:
        return (
            _legacy_row(first, line=line, filename=filename, trade_date=trade_date),
            _legacy_row(second, line=line, filename=filename, trade_date=trade_date),
        )
    except ParseError as exc:
        return quarantine(f"a half fails the row checks: {exc}")


def resolve_legacy(
    quotes: Sequence[BseLegacyQuote],
    scrip_to_isin: Mapping[str, str],
) -> LegacyResolution:
    """Turn legacy scrip-keyed quotes into `PriceRow`s by resolving each `SC_CODE` to an ISIN.

    What it does: looks each quote's scrip code up in `scrip_to_isin` (the BSE scrip master's
    `scrip_code → ISIN` map, `scrip_master.scrip_to_isin`) and builds a `PriceRow` with the resolved
    ISIN. The BSE `SC_GROUP` becomes the row's `series` — BSE's group ('A', 'B', 'T', 'X', …) is its
    analogue of NSE's series, and `PriceRow.series` requires a non-empty value.
    What it never does: guess an ISIN. A scrip code the map does not carry lands in `unresolved` and
    is counted — a visible gap for D7, never a `PriceRow` under a made-up identity (invariant #2).

    The `last` field is left exactly as published; BSE's legacy file, unlike NSE's UDiFF, always
    carries a numeric `LAST`, so no empty-to-zero normalisation applies here.
    """
    resolved: list[PriceRow] = []
    unresolved: list[BseLegacyQuote] = []
    for quote in quotes:
        isin = scrip_to_isin.get(quote.scrip_code)
        if isin is None:
            unresolved.append(quote)
            continue
        try:
            resolved.append(
                PriceRow(
                    isin=isin,
                    symbol=quote.scrip_name,
                    series=quote.group,
                    trade_date=quote.trade_date,
                    open=quote.open,
                    high=quote.high,
                    low=quote.low,
                    close=quote.close,
                    last=quote.last,
                    prev_close=quote.prev_close,
                    total_traded_qty=quote.total_traded_qty,
                    total_traded_value=quote.total_traded_value,
                    total_trades=quote.total_trades,
                )
            )
        except ValidationError as exc:
            raise ParseError(
                f"legacy quote for scrip {quote.scrip_code} ({quote.scrip_name}) does not form a "
                f"valid price row: {exc.errors(include_url=False)}",
                filename=LEGACY_SOURCE_ID,
            ) from exc

    if unresolved:
        _LOG.warning(
            "bhavcopy.legacy_unresolved",
            source=LEGACY_SOURCE_ID,
            unresolved=len(unresolved),
            resolved=len(resolved),
            scrip_codes=[q.scrip_code for q in unresolved[:20]],
        )
    return LegacyResolution(resolved=tuple(resolved), unresolved=tuple(unresolved))


# ── internals ────────────────────────────────────────────────────────────────────────────────


def _text_of(payload: bytes, *, filename: str) -> str:
    """Decode a payload to CSV text, unzipping it first when it is a zip.

    BSE serves the UDiFF file bare and the legacy file zipped, so both a zip and a bare CSV are
    payloads a caller legitimately has. A zip must hold exactly one member: zero means the archive
    is empty and several means the exchange changed what it ships, and both are things to stop on
    rather than pick "the first CSV" and turn a format change into a day of quietly wrong data.
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


def _check_udiff_header(header: list[str] | None, *, filename: str) -> None:
    """Check the header is exactly the UDiFF era's 34 columns, in order."""
    if header is None:
        raise ParseError("file is empty; expected the bhavcopy header row", filename=filename)
    named = tuple(field.strip() for field in header)
    if named != UDIFF_COLUMNS:
        raise ParseError(
            f"unexpected header {','.join(named)!r}; this parser reads the post-08-Jul-2024 BSE "
            f"UDiFF cash bhavcopy, whose header is exactly {','.join(UDIFF_COLUMNS)!r}. The legacy "
            "era and any other column layout are refused on purpose",
            filename=filename,
            line=1,
        )


def _check_legacy_header(header: list[str] | None, *, filename: str) -> None:
    """Check the header is exactly the legacy era's 14 columns, in order."""
    if header is None:
        raise ParseError("file is empty; expected the bhavcopy header row", filename=filename)
    named = tuple(field.strip().upper() for field in header)
    if named != LEGACY_COLUMNS:
        raise ParseError(
            f"unexpected header {','.join(named)!r}; this parser reads the pre-08-Jul-2024 BSE "
            f"legacy bhavcopy, whose header is exactly {','.join(LEGACY_COLUMNS)!r}",
            filename=filename,
            line=1,
        )


def _udiff_row(record: list[str], *, line: int, filename: str) -> PriceRow:
    """Turn one UDiFF CSV record into a `PriceRow`, or say which line and field was wrong."""
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
            trade_date=_iso_date(field["TradDt"], column="TradDt", line=line, filename=filename),
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


def _legacy_row(record: list[str], *, line: int, filename: str, trade_date: date) -> BseLegacyQuote:
    """Turn one legacy CSV record into a `BseLegacyQuote`, or a located error."""
    if len(record) != len(LEGACY_COLUMNS):
        raise ParseError(
            f"row has {len(record)} fields, header has {len(LEGACY_COLUMNS)}; a short row here is "
            "what a truncated download looks like",
            filename=filename,
            line=line,
        )
    field = dict(zip(LEGACY_COLUMNS, (value.strip() for value in record), strict=False))

    scrip_code = field["SC_CODE"]
    if not _SCRIP_CODE.match(scrip_code):
        raise ParseError(
            f"SC_CODE is {scrip_code!r}, which is not a BSE scrip code",
            filename=filename,
            line=line,
        )
    group = field["SC_GROUP"]
    if not group:
        raise ParseError(
            f"SC_GROUP is empty for scrip {scrip_code}; BSE always publishes a group",
            filename=filename,
            line=line,
        )

    return BseLegacyQuote(
        scrip_code=scrip_code,
        scrip_name=field["SC_NAME"],
        group=group,
        trade_date=trade_date,
        open=_decimal(field["OPEN"], column="OPEN", line=line, filename=filename),
        high=_decimal(field["HIGH"], column="HIGH", line=line, filename=filename),
        low=_decimal(field["LOW"], column="LOW", line=line, filename=filename),
        close=_decimal(field["CLOSE"], column="CLOSE", line=line, filename=filename),
        last=_decimal(field["LAST"], column="LAST", line=line, filename=filename),
        prev_close=_decimal(field["PREVCLOSE"], column="PREVCLOSE", line=line, filename=filename),
        total_trades=_integer(field["NO_TRADES"], column="NO_TRADES", line=line, filename=filename),
        total_traded_qty=_integer(
            field["NO_OF_SHRS"], column="NO_OF_SHRS", line=line, filename=filename
        ),
        total_traded_value=_decimal(
            field["NET_TURNOV"], column="NET_TURNOV", line=line, filename=filename
        ),
        close_indicator=field["TDCLOINDI"],
    )


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
    """`LastPric` as an exact `Decimal`, mapping the UDiFF era's empty "no last snapshot" to zero.

    Mirrors the NSE UDiFF parser: the format leaves `LastPric` empty for a security with no
    last-traded-price snapshot, which the legacy era wrote as `0`. Normalising empty to `Decimal(0)`
    keeps the field's meaning identical across both eras and exchanges. Only the genuinely-empty
    case is absorbed; any non-empty value goes through the strict decimal check.
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


def _iso_date(value: str, *, column: str, line: int, filename: str) -> date:
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


def _one_session(dates: Sequence[date], *, filename: str) -> None:
    """Refuse a file whose rows do not all belong to the same session (§4.2 partitions by date)."""
    distinct = set(dates)
    if len(distinct) > 1:
        raise ParseError(
            "rows span more than one session: "
            f"{', '.join(sorted(day.isoformat() for day in distinct))}",
            filename=filename,
        )
