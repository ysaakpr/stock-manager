"""`mcap<date>.csv` — daily issue size, market cap and last-trade-date per symbol (W2).

Ten columns, header stable across every date it appears on:
`Trade Date, Symbol, Series, Security Name, Category, Last Trade Date, Face Value(Rs.),
Issue Size, Close Price/Paid up value(Rs.), Market Cap(Rs.)`.

The member appears somewhere between 2024-01-02 (absent) and 2024-07-01 (present) — that boundary
is *not pinned*; the smoke fetch spent its budget on the boundaries that change parser behaviour.
The filename changed from `MCAP01072024.csv` to `mcap03112025.csv` at the ~2025-10 casing cutover;
the header did not change with it.

**On the delisting-date hypothesis — the measurement contradicts it, and says so here.** W2 opened
with the idea that `Last Trade Date` is a delisting-date series, which would close the gap of zero
delisting events recorded after 2003-03-27. It is not. Measured across nine `mcap` files:

* `Category` only ever takes the values `Listed` and `Permitted`. There is no `Delisted` value, no
  `Suspended` value, and no row survives its own delisting — a delisted security simply stops
  appearing in the file.
* `Last Trade Date` differs from `Trade Date` on 48-116 rows per file, and those rows are
  overwhelmingly thinly-traded SME-series names that did not trade *that day*. It is an
  illiquidity marker, not a terminal event. A further 3-6 rows per file carry the literal
  `Not Traded`, which marks a security with no trading history at all.

So `mcap` yields a delisting signal only by **disappearance** — the last bundle in which a symbol
appears bounds its delisting from below — which is a weaker and much more expensive claim than a
dated event, and one that needs the full daily series to make at all. It is reported, not acted on.

What the member *is* good for, and unambiguously: `Issue Size` is a daily shares-outstanding
series, which is the denominator the platform currently has no dated source for.

**Symbol-keyed, no ISIN**, like every other member of this bundle; `McapRow` has no `isin` field.

Offline by construction: takes bytes, or a `PrBundle` opened from L0. It never fetches.
"""

from __future__ import annotations

import csv
import re
from collections.abc import Sequence
from datetime import date
from decimal import Decimal, InvalidOperation
from io import StringIO
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from dataplatform.ingest.corp_actions import month_from_name
from dataplatform.ingest.models import ParseError
from dataplatform.ingest.nse.pr_bundle.bundle import PR_BUNDLE_SOURCE_ID, MemberKind, PrBundle
from dataplatform.logging import get_logger

__all__ = [
    "MCAP_COLUMNS",
    "McapFile",
    "McapRow",
    "parse_mcap",
    "parse_mcap_bundle",
]

_LOG = get_logger(__name__)

#: The published header, normalized by stripping whitespace from each cell — the last one ships
#: with fourteen trailing spaces (`'Market Cap(Rs.)              '`).
MCAP_COLUMNS: Final[tuple[str, ...]] = (
    "TRADE DATE",
    "SYMBOL",
    "SERIES",
    "SECURITY NAME",
    "CATEGORY",
    "LAST TRADE DATE",
    "FACE VALUE(RS.)",
    "ISSUE SIZE",
    "CLOSE PRICE/PAID UP VALUE(RS.)",
    "MARKET CAP(RS.)",
)

#: `DD MMM YYYY`, e.g. `04 SEP 2026`. The only date shape this member has used.
_DATE_RE: Final[re.Pattern[str]] = re.compile(r"^(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})$")

#: Values that mean "no date". `not traded` is NSE's own sentinel and is published, not blank:
#: it appears in `Last Trade Date` on 3-6 rows of every `mcap` file measured (2024-07 to 2026-09)
#: and marks a security that has never traded at all — typically a fresh listing. It maps to
#: `None` because that is exactly what it means; an empty `Last Trade Date` never occurs.
_EMPTY_MARKERS: Final[frozenset[str]] = frozenset(
    {"", "-", "--", "n/a", "na", "null", "none", "not traded"}
)


class McapRow(BaseModel):
    """One security's issue size, close and market cap on one session, as published."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    trade_date: date = Field(description="the session the row describes")
    publication_date: date = Field(description="the bundle's own publication date")
    symbol: str = Field(min_length=1, description="NSE trading symbol; NOT a join key")
    series: str = Field(description="the trading series, e.g. EQ, BE, SM")
    security_name: str = Field(description="issuer name as published")
    category: str = Field(
        description="'Listed' or 'Permitted' — the only two values measured; never 'Delisted'"
    )
    last_trade_date: date | None = Field(
        default=None,
        description=(
            "the last session this security actually traded — an illiquidity marker, NOT a "
            "delisting date (see the module docstring). `None` means the security has never "
            "traded: NSE publishes the literal 'Not Traded' there, which is not the same fact "
            "as a stale date and is not a missing value"
        ),
    )
    face_value: Decimal = Field(description="Face Value(Rs.)")
    issue_size: int = Field(ge=0, description="Issue Size — shares outstanding, the useful column")
    close_price: Decimal = Field(description="Close Price / paid-up value in rupees")
    market_cap: Decimal = Field(description="Market Cap(Rs.)")
    source: str = Field(default=PR_BUNDLE_SOURCE_ID, description="Source Register id")
    l0_key: str | None = Field(default=None, description="the L0 payload this row derives from")

    @property
    def traded_on_trade_date(self) -> bool:
        """Whether the security traded on the session this row describes."""
        return self.last_trade_date is not None and self.last_trade_date == self.trade_date

    @property
    def never_traded(self) -> bool:
        """Whether NSE published 'Not Traded' — the security has no trading history at all."""
        return self.last_trade_date is None


class McapFile(BaseModel):
    """One `mcap` member: its security rows, plus the summary rows the file ends with.

    The file's last lines are subtotals, not securities: a `Listed` row, a `Permitted` row and a
    `Total` row, each carrying only a market cap. They are separated out rather than dropped, so a
    campaign can cross-check the parsed rows against the exchange's own total instead of trusting
    its own sum.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    publication_date: date
    rows: tuple[McapRow, ...]
    totals: dict[str, Decimal] = Field(
        default_factory=dict, description="summary label (e.g. 'Listed', 'Total') → market cap"
    )

    @property
    def total_market_cap(self) -> Decimal | None:
        """The exchange's own `Total` line, when the file carries one."""
        return self.totals.get("Total")

    @property
    def summed_market_cap(self) -> Decimal:
        """The sum of the parsed security rows, for comparison against `total_market_cap`."""
        return sum((row.market_cap for row in self.rows), Decimal(0))


def parse_mcap(
    payload: bytes,
    *,
    filename: str,
    publication_date: date,
    l0_key: str | None = None,
) -> McapFile:
    """Parse one `mcap` member.

    A line whose SERIES and SECURITY NAME are both blank is one of the file's trailing subtotals
    and goes to `totals` under its SYMBOL cell; every other line is a security. Raises `ParseError`
    naming the file and line for a header that is not `MCAP_COLUMNS`, a wrong column count, or a
    numeric or date field that does not parse.
    """
    text = _decode(payload, filename=filename)
    rows = list(csv.reader(StringIO(text)))
    if not rows:
        raise ParseError("file is empty", filename=filename)

    header = tuple(cell.strip().upper() for cell in rows[0])
    if header != MCAP_COLUMNS:
        raise ParseError(
            f"unexpected header {header!r}; expected {MCAP_COLUMNS!r}", filename=filename, line=1
        )

    parsed: list[McapRow] = []
    totals: dict[str, Decimal] = {}
    for offset, raw in enumerate(rows[1:], start=2):
        if not raw or not any(cell.strip() for cell in raw):
            continue
        if len(raw) != len(MCAP_COLUMNS):
            raise ParseError(
                f"expected {len(MCAP_COLUMNS)} columns, got {len(raw)}: {raw!r}",
                filename=filename,
                line=offset,
            )
        if not raw[2].strip() and not raw[3].strip():
            label = raw[1].strip()
            totals[label] = _decimal(
                raw[9], field="Market Cap(Rs.)", filename=filename, line=offset
            )
            continue
        parsed.append(
            _row(
                raw,
                filename=filename,
                line=offset,
                publication_date=publication_date,
                l0_key=l0_key,
            )
        )

    result = McapFile(publication_date=publication_date, rows=tuple(parsed), totals=totals)
    _LOG.info(
        "pr_bundle_mcap.parsed",
        source=PR_BUNDLE_SOURCE_ID,
        filename=filename,
        publication_date=publication_date.isoformat(),
        rows=len(result.rows),
        totals=sorted(totals),
        state="VALIDATED",
    )
    return result


def parse_mcap_bundle(bundle: PrBundle, *, l0_key: str | None = None) -> McapFile:
    """Parse the `mcap` member of an opened bundle, dated by that bundle.

    Raises `ParseError` when the bundle has no `mcap` member — the normal case before ~2024-07.
    Callers sweeping a range should test `bundle.has(MemberKind.MCAP)`.
    """
    member = bundle.member(MemberKind.MCAP)
    return parse_mcap(
        bundle.read(MemberKind.MCAP),
        filename=bundle.filename if member is None else member.name,
        publication_date=bundle.publication_date,
        l0_key=l0_key,
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


def _row(
    raw: Sequence[str],
    *,
    filename: str,
    line: int,
    publication_date: date,
    l0_key: str | None,
) -> McapRow:
    """One security line → one `McapRow`."""
    trade, symbol, series, name, category, last, face, size, close, cap = (
        cell.strip() for cell in raw
    )
    if not symbol:
        raise ParseError("row has no Symbol", filename=filename, line=line)
    trade_date = _date(trade, field="Trade Date", filename=filename, line=line)
    if trade_date is None:
        raise ParseError(f"row for {symbol!r} has no Trade Date", filename=filename, line=line)
    return McapRow(
        trade_date=trade_date,
        publication_date=publication_date,
        symbol=symbol,
        series=series,
        security_name=name,
        category=category,
        last_trade_date=_date(last, field="Last Trade Date", filename=filename, line=line),
        face_value=_decimal(face, field="Face Value(Rs.)", filename=filename, line=line),
        issue_size=_int(size, field="Issue Size", filename=filename, line=line),
        close_price=_decimal(close, field="Close Price", filename=filename, line=line),
        market_cap=_decimal(cap, field="Market Cap(Rs.)", filename=filename, line=line),
        l0_key=l0_key,
    )


def _date(text: str, *, field: str, filename: str, line: int) -> date | None:
    """`DD MMM YYYY` → a date, locale-independently; `None` for the empty markers."""
    value = text.strip()
    if value.lower() in _EMPTY_MARKERS:
        return None
    match = _DATE_RE.match(value)
    if match is None:
        raise ParseError(
            f"{field} is {value!r}, not a DD MMM YYYY date", filename=filename, line=line
        )
    month = month_from_name(match.group(2))
    if month is None:
        raise ParseError(
            f"{field} is {value!r}: {match.group(2)!r} is not a month name",
            filename=filename,
            line=line,
        )
    try:
        return date(int(match.group(3)), month, int(match.group(1)))
    except ValueError as exc:
        raise ParseError(f"{field} is {value!r}: {exc}", filename=filename, line=line) from exc


def _int(text: str, *, field: str, filename: str, line: int) -> int:
    """A whole-share count. Raises rather than coercing — a share count is never fractional."""
    try:
        return int(text)
    except ValueError as exc:
        raise ParseError(
            f"{field} is {text!r}, not an integer", filename=filename, line=line
        ) from exc


def _decimal(text: str, *, field: str, filename: str, line: int) -> Decimal:
    """A money field, as `Decimal` and never through `float` (a float in a money field is a bug)."""
    try:
        return Decimal(text.strip())
    except InvalidOperation as exc:
        raise ParseError(
            f"{field} is {text!r}, not a number", filename=filename, line=line
        ) from exc
