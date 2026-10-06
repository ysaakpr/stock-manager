"""`Ix<date>.csv` — historical index membership with issue-cap, market cap and weightage (W2).

The repo's standing position is that index constituent *history* "does not exist to license"
(ops/BACKLOG.md, AGENTIC_CONTEXT §4.1). This member partially contradicts that, and the honest
statement of how far is the point of this module.

**Measured 2026-09-08, 11 probes across 2010** (`ops/studies/evidence/nse-pr-bundle.md`):

| session | indices carried |
|---|---|
| 2010-01-04 … 2010-01-08 (five consecutive) | BANK Nifty (12), CNX IT (20), CNX 500 (500), |
| | CNX Midcap (100), Nifty Midcap 50 (50) |
| 2010-04-08, 2010-04-09 | CNX 500 only |
| 2010-07-01 | CNX 500 only |
| 2010-10-01, 2010-10-04 | CNX 500 (500), CNX Infrastructure (25) |
| 2010-10-18 onward | **member absent entirely** |

So `Ix` is **intermittent, not daily-complete, and not a full snapshot**. The member itself was
published every session we probed in 2010 and vanished between 2010-10-04 and 2010-10-18, never to
reappear in 2011, 2013, 2016, 2019, 2022, 2024 or 2026. Within its lifetime the *set of indices* it
carries changes over the year while staying stable across consecutive days — CNX 500 is present on
every date measured; the others come and go.

**What that means for reconstructing membership: it does not reconstruct.** Nine months of an
incomplete, varying index set, ending in 2010, cannot produce a point-in-time constituent history
for a backtest. What it *can* do is give a small number of dated, verifiable anchor points —
notably CNX 500 on ~every session of 2010 — against which a reconstruction from another source can
be checked. That is a validation asset, not a membership series, and this module is written to
serve exactly that: it parses faithfully and claims nothing more.

**Symbol-keyed, no ISIN.** Same identity problem as `bc.py`, and worse here: these are 2010
symbols, many belonging to companies since delisted, renamed or merged. Resolving them through a
current listing would be survivorship-biased in precisely the direction that matters. `IxRow`
therefore carries no `isin` field.

Offline by construction: takes bytes, or a `PrBundle` opened from L0. It never fetches.
"""

from __future__ import annotations

import csv
from collections.abc import Sequence
from datetime import date
from decimal import Decimal, InvalidOperation
from io import StringIO
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from dataplatform.ingest.models import ParseError
from dataplatform.ingest.nse.pr_bundle.bundle import PR_BUNDLE_SOURCE_ID, MemberKind, PrBundle
from dataplatform.logging import get_logger

__all__ = [
    "IX_COLUMNS",
    "IxFile",
    "IxRow",
    "parse_ix",
    "parse_ix_bundle",
]

_LOG = get_logger(__name__)

#: The published header, in order. Identical across every `Ix` file measured.
IX_COLUMNS: Final[tuple[str, ...]] = (
    "INDEX_FLG",
    "SYMBOL",
    "SERIES",
    "SECURITY",
    "ISSUE_CAP",
    "CLOSE_PRIC",
    "MKT_CAP",
    "WEIGHTAGE",
)


class IxRow(BaseModel):
    """One security's membership of one index on one session, as published.

    Money is `Decimal` throughout: `close_price`, `market_cap` and `weightage` are read from the
    published text and never through `float`, which cannot represent a two-decimal rupee price
    exactly and would make a weight sum drift.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    publication_date: date = Field(description="the bundle's own publication date")
    index_name: str = Field(min_length=1, description="INDEX_FLG, e.g. 'CNX 500'")
    symbol: str = Field(min_length=1, description="NSE trading symbol; NOT a join key")
    series: str = Field(description="the trading series, e.g. EQ, BE")
    security_name: str = Field(description="issuer name as published")
    issue_cap: int = Field(ge=0, description="ISSUE_CAP — shares counted toward the index")
    close_price: Decimal = Field(description="CLOSE_PRIC in rupees")
    market_cap: Decimal = Field(description="MKT_CAP in rupees")
    weightage: Decimal = Field(description="WEIGHTAGE as published, in percent")
    source: str = Field(default=PR_BUNDLE_SOURCE_ID, description="Source Register id")
    l0_key: str | None = Field(default=None, description="the L0 payload this row derives from")


class IxFile(BaseModel):
    """One `Ix` member: its rows, and which indices it actually carried.

    `indices` is the answer to the rotation question for one date, and is what a campaign's
    availability table is built from — an index absent from this tuple was not published that day,
    which is a fact about the source rather than a gap in the parse.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    publication_date: date
    rows: tuple[IxRow, ...]
    indices: tuple[str, ...] = Field(description="distinct INDEX_FLG values, in first-seen order")
    empty_indices: tuple[str, ...] = Field(
        default=(),
        description="index names announced by a banner line but carrying no constituent rows",
    )

    def constituents(self, index_name: str) -> tuple[IxRow, ...]:
        """Every row of one index, in published order."""
        return tuple(row for row in self.rows if row.index_name == index_name)


def parse_ix(
    payload: bytes,
    *,
    filename: str,
    publication_date: date,
    l0_key: str | None = None,
) -> IxFile:
    """Parse one `Ix` member.

    The file interleaves banner lines (`' , , ,BANK Nifty, , , , '` — the index name in the
    SECURITY column, everything else blank), blank separator lines, and constituent rows whose
    INDEX_FLG names their index. INDEX_FLG is taken as authoritative; a banner naming an index that
    then carries no rows is reported in `empty_indices` rather than dropped.

    Raises `ParseError` naming the file and line for a header that is not `IX_COLUMNS`, a row with
    the wrong column count, or a numeric field that is not a number.
    """
    text = _decode(payload, filename=filename)
    rows = list(csv.reader(StringIO(text)))
    if not rows:
        raise ParseError("file is empty", filename=filename)

    header = tuple(cell.strip().upper() for cell in rows[0])
    if header != IX_COLUMNS:
        raise ParseError(
            f"unexpected header {header!r}; expected {IX_COLUMNS!r}", filename=filename, line=1
        )

    parsed: list[IxRow] = []
    order: list[str] = []
    announced: list[str] = []
    for offset, raw in enumerate(rows[1:], start=2):
        if not raw or not any(cell.strip() for cell in raw):
            continue
        if len(raw) != len(IX_COLUMNS):
            raise ParseError(
                f"expected {len(IX_COLUMNS)} columns, got {len(raw)}: {raw!r}",
                filename=filename,
                line=offset,
            )
        if not raw[0].strip():
            # A banner line: the index name sits in SECURITY and nothing else is filled in.
            banner = raw[3].strip()
            if banner and banner not in announced:
                announced.append(banner)
            continue
        row = _row(
            raw,
            filename=filename,
            line=offset,
            publication_date=publication_date,
            l0_key=l0_key,
        )
        if row.index_name not in order:
            order.append(row.index_name)
        parsed.append(row)

    result = IxFile(
        publication_date=publication_date,
        rows=tuple(parsed),
        indices=tuple(order),
        empty_indices=tuple(name for name in announced if name not in order),
    )
    _LOG.info(
        "pr_bundle_ix.parsed",
        source=PR_BUNDLE_SOURCE_ID,
        filename=filename,
        publication_date=publication_date.isoformat(),
        rows=len(result.rows),
        indices=list(result.indices),
        empty_indices=list(result.empty_indices),
        state="VALIDATED",
    )
    return result


def parse_ix_bundle(bundle: PrBundle, *, l0_key: str | None = None) -> IxFile:
    """Parse the `Ix` member of an opened bundle, dated by that bundle.

    Raises `ParseError` when the bundle has no `Ix` member — which is the normal case for every
    session after 2010-10-04. Callers sweeping a range should test `bundle.has(MemberKind.IX)`.
    """
    member = bundle.member(MemberKind.IX)
    return parse_ix(
        bundle.read(MemberKind.IX),
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
            "body is markup, not CSV — an HTML error page answered with a 200",
            filename=filename,
        )
    return text


def _row(
    raw: Sequence[str],
    *,
    filename: str,
    line: int,
    publication_date: date,
    l0_key: str | None,
) -> IxRow:
    """One constituent line → one `IxRow`."""
    index_name, symbol, series, security, issue_cap, close, mkt_cap, weight = (
        cell.strip() for cell in raw
    )
    if not symbol:
        raise ParseError(
            f"constituent row of {index_name!r} has no SYMBOL", filename=filename, line=line
        )
    return IxRow(
        publication_date=publication_date,
        index_name=index_name,
        symbol=symbol,
        series=series,
        security_name=security,
        issue_cap=_int(issue_cap, field="ISSUE_CAP", filename=filename, line=line),
        close_price=_decimal(close, field="CLOSE_PRIC", filename=filename, line=line),
        market_cap=_decimal(mkt_cap, field="MKT_CAP", filename=filename, line=line),
        weightage=_decimal(weight, field="WEIGHTAGE", filename=filename, line=line),
        l0_key=l0_key,
    )


def _int(text: str, *, field: str, filename: str, line: int) -> int:
    """A whole-share count. Raises rather than coercing — a share count is never fractional."""
    try:
        return int(text)
    except ValueError as exc:
        raise ParseError(
            f"{field} is {text!r}, not an integer", filename=filename, line=line
        ) from exc


def _decimal(text: str, *, field: str, filename: str, line: int) -> Decimal:
    """A money or weight field, as `Decimal` and never through `float` (invariant: money rule)."""
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise ParseError(
            f"{field} is {text!r}, not a number", filename=filename, line=line
        ) from exc
