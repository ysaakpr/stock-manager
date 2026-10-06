"""`Pd<date>.csv` — the bundle's price-detail report: every index level and every security's marks.

One file per session, ~1,300 rows in 2010 and ~3,800 in 2026, laid out as the newspaper page NSE
generated it for: the session's **index block** first, then the **NIFTY 50 constituents** under a
`… Sec` banner, then the rest of the market under section banners (`COMPULSORY ROLLING STOCKS`,
`TRADE FOR TRADE STOCKS`, `OTHER SECURITIES`, `LIMITED PHYSICAL MARKET`, …). It is the only member
that carries three facts the platform has no other source for:

* **Daily OHLC and 52-week range of every NSE index**, 2010-01-04 onward — 183 distinct index
  names over the corpus, including India VIX from 2010-07-19 (the 2010-01-04 file already prints
  it, with a blank `MKT`). The lake's only other index level is the computed NIFTY TRI proxy.
* **`CORP_IND`** — NSE's own ex-marker on the session a security trades ex an entitlement (`XD`
  dividend, `XB` bonus, `XR` rights, `XI` interest, `XO` other, and their combinations: `XDB`,
  `XDO`, `XDBO`, …). 36,552 marks across the corpus. It is a *same-session, exchange-printed*
  witness that a corporate action landed — exactly what the D7 sentinel needs to tell a missed
  adjustment from a real crash.
* **`IND_SEC`** — on a security row, `Y` marks a member of the NIFTY 50 on that session (50 rows
  per file, every file measured). On an index row it is always `Y` and carries no information.

Everything else on a security row (OHLC, traded value and quantity, trades) is the bhavcopy's own
number and is redundant with `prices_raw`; the 52-week high and low are NSE's published figures
and are carried as published, never recomputed — they are a witness, not a price series.

**Format.** One header on 4,155 of the 4,156 files measured on 2026-10-06; 2010-05-14 alone
appends one empty header cell. Columns are located **by header name** regardless, an unknown
header cell raises, and a trailing empty header cell is padding. Numbers are unpadded in the
early eras and space-padded later (`'      2901.00'`); quantities in 2010 are printed with a
`.00` (`'903330835.00'`) and must be integral. A blank numeric cell is `None`, never zero; a
published zero (`'0.00'`, the 52-week range of an index too new to have one) is kept as zero.

**Row classes**, decided by which cells are filled — never by position in the file:

| class | `SYMBOL` | `SERIES` | `CLOSE_PRICE` | becomes |
|---|---|---|---|---|
| furniture | blank | any | blank, no name | skipped, counted |
| banner | blank | any | blank, `SECURITY` named | the `section` of the security rows below it |
| index | blank | blank | filled | `PdIndexRow` |
| security | filled | filled | filled | `PdSecurityRow` |

Anything else — a symbol with no series, a series and a close with no symbol, a security row with
no `MKT` — raises `ParseError` naming the line. Nothing is skipped silently.

**Symbol-keyed, no ISIN**, like every member of this bundle. Resolution to an ISIN is the L1
builder's job (`dataplatform.ingest.pr_bundle_l1`), through the identity module.

Offline by construction: takes bytes, or a `PrBundle`. Never fetches, never reads a clock, never
writes. Rows are slotted dataclasses rather than pydantic models because the corpus is 8.6 M rows
and a validating constructor per row is most of a full rebuild's CPU.
"""

from __future__ import annotations

import csv
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from io import StringIO
from typing import Final

from dataplatform.ingest.models import ParseError
from dataplatform.ingest.nse.pr_bundle.bundle import PR_BUNDLE_SOURCE_ID, MemberKind, PrBundle
from dataplatform.logging import get_logger

__all__ = [
    "PD_COLUMNS",
    "PdFile",
    "PdIndexRow",
    "PdSecurityRow",
    "parse_pd",
    "parse_pd_bundle",
]

_LOG = get_logger(__name__)

#: The published header, every era. Located by name in each file, never by position.
PD_COLUMNS: Final[tuple[str, ...]] = (
    "MKT",
    "SERIES",
    "SYMBOL",
    "SECURITY",
    "PREV_CL_PR",
    "OPEN_PRICE",
    "HIGH_PRICE",
    "LOW_PRICE",
    "CLOSE_PRICE",
    "NET_TRDVAL",
    "NET_TRDQTY",
    "IND_SEC",
    "CORP_IND",
    "TRADES",
    "HI_52_WK",
    "LO_52_WK",
)

#: `CORP_IND` values are `X` followed by entitlement letters. Measured set on 2026-10-06:
#: XD XDO XO XI XB XR XDB XDBO XDR XBO XDRO XDIO. A value outside the shape raises; a new
#: *combination* of known letters is accepted, because NSE composes them.
_CORP_IND_RE: Final[re.Pattern[str]] = re.compile(r"^X[DBRIO]{1,4}$")

_WS: Final[re.Pattern[str]] = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class PdIndexRow:
    """One index's session, as the `Pd` index block printed it. `index_name` is verbatim
    (whitespace collapsed); NSE renamed the headline indices twice (2013-03-04, 2015-11-09)."""

    session: date
    index_name: str
    prev_close: Decimal | None
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal
    traded_value: Decimal | None
    traded_qty: int | None
    trades: int | None
    hi_52wk: Decimal | None
    lo_52wk: Decimal | None


@dataclass(frozen=True, slots=True)
class PdSecurityRow:
    """One security's session marks, as published. `symbol` is NOT a join key."""

    session: date
    mkt: str
    series: str
    symbol: str
    security_name: str
    section: str | None
    prev_close: Decimal | None
    close: Decimal
    trades: int | None
    nifty50_flag: bool | None
    corp_ind: str | None
    hi_52wk: Decimal | None
    lo_52wk: Decimal | None


@dataclass(frozen=True, slots=True)
class PdFile:
    """One `Pd` member: its index rows, its security rows, and what was skipped as furniture."""

    publication_date: date
    index_rows: tuple[PdIndexRow, ...]
    security_rows: tuple[PdSecurityRow, ...]
    banners: tuple[str, ...]
    furniture_rows: int


def parse_pd(payload: bytes, *, filename: str, publication_date: date) -> PdFile:
    """Parse one `Pd` member into index rows and security rows, dated by `publication_date`.

    What it does: locates every column by header name, classifies each line (see the module
    table), and types its cells. What it assumes: `publication_date` is the bundle's own
    (`PrBundle.publication_date`), which is both the session and the first date a row is knowable.
    What it never does: read a column by position, resolve a symbol, treat a blank number as zero,
    or skip a line it cannot classify. Raises `ParseError` naming the file and line.
    """
    text = _decode(payload, filename=filename)
    try:
        lines = list(csv.reader(StringIO(text)))
    except csv.Error as exc:
        raise ParseError(f"not readable as CSV: {exc}", filename=filename) from exc
    if not lines:
        raise ParseError("file is empty", filename=filename)
    index, width = _header(lines[0], filename=filename)

    index_rows: list[PdIndexRow] = []
    security_rows: list[PdSecurityRow] = []
    banners: list[str] = []
    furniture = 0
    section: str | None = None
    for line_no, raw in enumerate(lines[1:], start=2):
        overflow = [cell for cell in raw[width:] if cell.strip()]
        if overflow:
            raise ParseError(
                f"row carries values beyond its {width}-column header: {raw!r}",
                filename=filename,
                line=line_no,
            )
        if len(raw) < width and any(cell.strip() for cell in raw):
            raise ParseError(
                f"expected {width} columns, got {len(raw)}: {raw!r}",
                filename=filename,
                line=line_no,
            )

        def cell(column: str, raw: Sequence[str] = raw) -> str:
            position = index[column]
            return raw[position].strip() if position < len(raw) else ""

        symbol, series, name, close = (
            cell("SYMBOL"),
            cell("SERIES"),
            _WS.sub(" ", cell("SECURITY")),
            cell("CLOSE_PRICE"),
        )
        if not symbol and not close:
            if name:
                banners.append(name)
                section = name
            else:
                furniture += 1
            continue
        if not symbol:
            if series:
                raise ParseError(
                    f"a priced row with series {series!r} and no symbol: {raw!r}",
                    filename=filename,
                    line=line_no,
                )
            if not name:
                raise ParseError(
                    f"an index row with no name: {raw!r}", filename=filename, line=line_no
                )
            index_rows.append(
                PdIndexRow(
                    session=publication_date,
                    index_name=name,
                    prev_close=_dec(
                        cell("PREV_CL_PR"), "PREV_CL_PR", filename=filename, line=line_no
                    ),
                    open=_dec(cell("OPEN_PRICE"), "OPEN_PRICE", filename=filename, line=line_no),
                    high=_dec(cell("HIGH_PRICE"), "HIGH_PRICE", filename=filename, line=line_no),
                    low=_dec(cell("LOW_PRICE"), "LOW_PRICE", filename=filename, line=line_no),
                    close=_required(
                        _dec(close, "CLOSE_PRICE", filename=filename, line=line_no),
                        "CLOSE_PRICE",
                        filename=filename,
                        line=line_no,
                    ),
                    traded_value=_dec(
                        cell("NET_TRDVAL"), "NET_TRDVAL", filename=filename, line=line_no
                    ),
                    traded_qty=_int(
                        cell("NET_TRDQTY"), "NET_TRDQTY", filename=filename, line=line_no
                    ),
                    trades=_int(cell("TRADES"), "TRADES", filename=filename, line=line_no),
                    hi_52wk=_dec(cell("HI_52_WK"), "HI_52_WK", filename=filename, line=line_no),
                    lo_52wk=_dec(cell("LO_52_WK"), "LO_52_WK", filename=filename, line=line_no),
                )
            )
            continue
        mkt = cell("MKT")
        if not series or not mkt:
            raise ParseError(
                f"security {symbol!r} has no SERIES or no MKT: {raw!r}",
                filename=filename,
                line=line_no,
            )
        if not close:
            raise ParseError(
                f"security {symbol!r} has no CLOSE_PRICE: {raw!r}", filename=filename, line=line_no
            )
        security_rows.append(
            PdSecurityRow(
                session=publication_date,
                mkt=mkt,
                series=series,
                symbol=symbol,
                security_name=name,
                section=section,
                prev_close=_dec(cell("PREV_CL_PR"), "PREV_CL_PR", filename=filename, line=line_no),
                close=_required(
                    _dec(close, "CLOSE_PRICE", filename=filename, line=line_no),
                    "CLOSE_PRICE",
                    filename=filename,
                    line=line_no,
                ),
                trades=_int(cell("TRADES"), "TRADES", filename=filename, line=line_no),
                nifty50_flag=_flag(cell("IND_SEC"), filename=filename, line=line_no),
                corp_ind=_corp_ind(cell("CORP_IND"), filename=filename, line=line_no),
                hi_52wk=_dec(cell("HI_52_WK"), "HI_52_WK", filename=filename, line=line_no),
                lo_52wk=_dec(cell("LO_52_WK"), "LO_52_WK", filename=filename, line=line_no),
            )
        )

    result = PdFile(
        publication_date=publication_date,
        index_rows=tuple(index_rows),
        security_rows=tuple(security_rows),
        banners=tuple(banners),
        furniture_rows=furniture,
    )
    _LOG.info(
        "pr_bundle_pd.parsed",
        source=PR_BUNDLE_SOURCE_ID,
        filename=filename,
        publication_date=publication_date.isoformat(),
        index_rows=len(index_rows),
        security_rows=len(security_rows),
        furniture=furniture,
        state="VALIDATED",
    )
    return result


def parse_pd_bundle(bundle: PrBundle) -> PdFile:
    """Parse the `Pd` member of an opened bundle, dated by that bundle.

    Raises `ParseError` when the bundle carries no `Pd` member; callers sweeping a range test
    `bundle.has(MemberKind.PD)` first.
    """
    member = bundle.member(MemberKind.PD)
    return parse_pd(
        bundle.read(MemberKind.PD),
        filename=bundle.filename if member is None else member.name,
        publication_date=bundle.publication_date,
    )


# ── internals ────────────────────────────────────────────────────────────────────────────────


def _decode(payload: bytes, *, filename: str) -> str:
    """Latin-1 the body, refusing an empty one and markup wearing a 200."""
    if not payload.strip():
        raise ParseError("empty response body", filename=filename)
    text = payload.decode("latin-1")
    if text.lstrip()[:1] == "<":
        raise ParseError(
            "body is markup, not CSV — an HTML error page answered with a 200", filename=filename
        )
    return text


def _header(cells: Sequence[str], *, filename: str) -> tuple[dict[str, int], int]:
    """Column → position in this file, and the header's width without trailing padding."""
    names = [cell.strip().upper() for cell in cells]
    while names and not names[-1]:
        names.pop()
    index: dict[str, int] = {}
    for position, name in enumerate(names):
        if name not in PD_COLUMNS or name in index:
            raise ParseError(
                f"unexpected header cell {name!r} in {names!r}; expected {PD_COLUMNS!r}",
                filename=filename,
                line=1,
            )
        index[name] = position
    missing = [column for column in PD_COLUMNS if column not in index]
    if missing:
        raise ParseError(f"header {names!r} is missing {missing!r}", filename=filename, line=1)
    return index, len(names)


def _dec(text: str, column: str, *, filename: str, line: int) -> Decimal | None:
    if not text:
        return None
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise ParseError(
            f"{column} is {text!r}, not a number", filename=filename, line=line
        ) from exc
    if not value.is_finite():
        raise ParseError(f"{column} is {text!r}, not a finite number", filename=filename, line=line)
    return value


def _int(text: str, column: str, *, filename: str, line: int) -> int | None:
    value = _dec(text, column, filename=filename, line=line)
    if value is None:
        return None
    if value != value.to_integral_value():
        raise ParseError(f"{column} is {text!r}, not a whole count", filename=filename, line=line)
    return int(value)


def _required(value: Decimal | None, column: str, *, filename: str, line: int) -> Decimal:
    if value is None:
        raise ParseError(f"{column} is blank", filename=filename, line=line)
    return value


def _flag(text: str, *, filename: str, line: int) -> bool | None:
    if not text:
        return None
    if text.upper() in ("Y", "N"):
        return text.upper() == "Y"
    raise ParseError(f"IND_SEC is {text!r}, not Y or N", filename=filename, line=line)


def _corp_ind(text: str, *, filename: str, line: int) -> str | None:
    if not text:
        return None
    value = text.upper()
    if not _CORP_IND_RE.match(value):
        raise ParseError(
            f"CORP_IND is {text!r}, not an X-prefixed entitlement marker",
            filename=filename,
            line=line,
        )
    return value
