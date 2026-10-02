"""`bh<date>.csv` — the securities that hit their daily price band on one session (X2, H2).

One row per security that hit its upper (`H`) or lower (`L`) daily price band on the session the
bundle publishes. It is the source the round-2 pre-registration names for H2
(`ops/studies/preregistration-signals-2026-09-29.md` §3): no *new* buy of a name that hit a band in
any of the last five sessions. The member was registered in `MemberKind.BH` since W2 and parsed by
nothing until this module.

**Format eras**, measured by sniffing the member in every one of the 4,124 bundles L0 held on
2026-09-29 — 4,120 of them dated and carrying one, 836,698 rows, and not one parse failure:

| era | span | header (`FLAG` is `INDEX FLAG`) | rows |
|---|---|---|---|
| `sr`      | 2010-01-04 → 2012-02-27 | `SYMBOL,SR,SECURITY,HIGH/LOW,FLAG`     | CRLF; FLAG empty |
| `series`  | 2012-02-22 → 2025-10-10 | `SYMBOL,SERIES,SECURITY,HIGH/LOW,FLAG` | LF; four cells |
| `no_flag` | 2025-10-13 → open       | `SYMBOL,SERIES,SECURITY,HIGH/LOW`      | LF; four cells |

and four one-off shapes inside the `sr` span, each of which a positional reader gets wrong:

* 2010-09-13 and 2010-11-16 pad the header and every row with empty cells to ten columns, and
  2010-09-17 does the same with no `INDEX FLAG` in the header at all;
* **2010-11-09 swaps two columns** — `SYMBOL,SR,HIGH/LOW,SECURITY,INDEX FLAG`. A reader that took
  cell 3 as the band side reads every issuer name as one;
* 2011-04-05 spells the flag `INDEXFLAG`;
* 2012-02-22..2012-03-14 already say `SERIES` while the member name is still `DDMMYYYY`.

So columns are located **by header name, per file**, never by position, and the `SR` column is the
same fact as `SERIES` under its older name. Six bundles (2021-05-17 onward, 2024-08-12,
2026-01-12) carry a header and no rows: no security hit a band that is also listed here, which is
an empty session and not a failure. `INDEX FLAG`, when a row fills it at all, names an index the
security belongs to (`CNX Nifty Junior`, 2010-08); H2 does not read it, so it is not carried.

Four bundles are not read by this module because `PrBundle` will not date them, and each refusal
is right: `PR190811.zip` and `PR040613.zip` carry a stray previous-session member beside their own;
`PR100113.zip` carries no dated member at all (and no `bh`); and **`PR020118.zip` is the
2019-01-02 bundle served under the 2018-01-02 name** — dating it by its filename would put a
session's band hits into a decision a year before they happened. A consumer counts these rather
than dating them some other way.

**What `H`/`L` mean is NSE's claim, not ours**: the file lists securities that *hit* the band. The
pre-registration fixes this member as H2's source, so the reader passes the side through as
published and does not second-guess it against the day's close.

**Symbol-keyed, no ISIN**, like every other member of this bundle; `BandHitRow` has no `isin`.
Mapping a row to an ISIN is `backtest.band_hits`' job, and it does so only through the same
session's bhavcopy — see there.

Offline by construction: takes bytes, or a `PrBundle` opened from L0. It never fetches, never reads
a clock, and never writes.
"""

from __future__ import annotations

import csv
from collections.abc import Sequence
from datetime import date
from enum import StrEnum
from io import StringIO
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from dataplatform.ingest.models import ParseError
from dataplatform.ingest.nse.pr_bundle.bundle import PR_BUNDLE_SOURCE_ID, MemberKind, PrBundle
from dataplatform.logging import get_logger

__all__ = [
    "BH_COLUMNS",
    "BandHitRow",
    "BandSide",
    "BhFile",
    "parse_bh",
    "parse_bh_bundle",
]

_LOG = get_logger(__name__)

#: The four columns every era carries, in their modern names. Located by name in each file.
BH_COLUMNS: Final[tuple[str, ...]] = ("SYMBOL", "SERIES", "SECURITY", "HIGH/LOW")

#: Header spellings → the canonical column. `SR` is `SERIES` before 2012-02-22.
_ALIASES: Final[dict[str, str]] = {
    "SYMBOL": "SYMBOL",
    "SR": "SERIES",
    "SERIES": "SERIES",
    "SECURITY": "SECURITY",
    "HIGH/LOW": "HIGH/LOW",
}

#: Header cells that are published but never carry a value in any file measured.
_IGNORED: Final[frozenset[str]] = frozenset({"INDEX FLAG", "INDEXFLAG"})


class BandSide(StrEnum):
    """Which band the security hit: the upper (`H`) or the lower (`L`)."""

    UPPER = "H"
    LOWER = "L"


class BandHitRow(BaseModel):
    """One security that hit a daily price band on one session, as published."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session: date = Field(description="the session the hit happened on — the bundle's own date")
    publication_date: date = Field(
        description="the bundle's own publication date; the first date the hit is knowable"
    )
    symbol: str = Field(min_length=1, description="NSE trading symbol; NOT a join key")
    series: str = Field(min_length=1, description="the trading series, e.g. EQ, BE, SM")
    security_name: str = Field(description="issuer name as published")
    side: BandSide
    source: str = Field(default=PR_BUNDLE_SOURCE_ID, description="Source Register id")
    l0_key: str | None = Field(default=None, description="the L0 payload this row derives from")


class BhFile(BaseModel):
    """One `bh` member: every band hit of its session. May be empty (a header and no rows)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    publication_date: date
    rows: tuple[BandHitRow, ...]


def parse_bh(
    payload: bytes,
    *,
    filename: str,
    publication_date: date,
    l0_key: str | None = None,
) -> BhFile:
    """Parse one `bh` member, locating its columns by header name.

    What it does: reads the header, maps each cell to a canonical column (`SR` → `SERIES`), and
    turns every non-blank line into a `BandHitRow` dated by `publication_date`, which is both the
    session and the first date the row is knowable.
    What it assumes: `publication_date` is the bundle's own (`PrBundle.publication_date`), never a
    clock or an ingest date.
    What it never does: read a column by position, guess a band side, or resolve a symbol. Raises
    `ParseError` naming the file and line for an unknown or missing header column, a row wider than
    its header with a value in the overflow, a blank symbol or series, and a side other than
    `H`/`L`.
    """
    text = _decode(payload, filename=filename)
    lines = list(csv.reader(StringIO(text)))
    if not lines:
        raise ParseError("file is empty", filename=filename)

    index, width = _header(lines[0], filename=filename)
    rows: list[BandHitRow] = []
    for offset, raw in enumerate(lines[1:], start=2):
        if not any(cell.strip() for cell in raw):
            continue
        overflow = [cell for cell in raw[width:] if cell.strip()]
        if overflow:
            raise ParseError(
                f"row carries values beyond its {width}-column header: {raw!r}",
                filename=filename,
                line=offset,
            )
        rows.append(
            _row(
                raw,
                index,
                filename=filename,
                line=offset,
                publication_date=publication_date,
                l0_key=l0_key,
            )
        )

    result = BhFile(publication_date=publication_date, rows=tuple(rows))
    _LOG.info(
        "pr_bundle_bh.parsed",
        source=PR_BUNDLE_SOURCE_ID,
        filename=filename,
        publication_date=publication_date.isoformat(),
        rows=len(result.rows),
        state="VALIDATED",
    )
    return result


def parse_bh_bundle(bundle: PrBundle, *, l0_key: str | None = None) -> BhFile:
    """Parse the `bh` member of an opened bundle, dated by that bundle.

    Raises `ParseError` when the bundle has no `bh` member (`PR100113.zip` is the one measured
    case). Callers sweeping a range should test `bundle.has(MemberKind.BH)`.
    """
    member = bundle.member(MemberKind.BH)
    return parse_bh(
        bundle.read(MemberKind.BH),
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


def _header(cells: Sequence[str], *, filename: str) -> tuple[dict[str, int], int]:
    """Canonical column → its position in this file, and the header's width without padding."""
    names = [cell.strip().upper() for cell in cells]
    while names and not names[-1]:
        names.pop()
    index: dict[str, int] = {}
    for position, name in enumerate(names):
        if name in _IGNORED:
            continue
        canonical = _ALIASES.get(name)
        if canonical is None or canonical in index:
            raise ParseError(
                f"unexpected header cell {name!r} in {names!r}; expected the columns "
                f"{BH_COLUMNS!r} (SERIES may be spelled SR)",
                filename=filename,
                line=1,
            )
        index[canonical] = position
    missing = [column for column in BH_COLUMNS if column not in index]
    if missing:
        raise ParseError(f"header {names!r} is missing {missing!r}", filename=filename, line=1)
    return index, len(names)


def _row(
    raw: Sequence[str],
    index: dict[str, int],
    *,
    filename: str,
    line: int,
    publication_date: date,
    l0_key: str | None,
) -> BandHitRow:
    """One security line → one `BandHitRow`."""

    def cell(column: str) -> str:
        position = index[column]
        return raw[position].strip() if position < len(raw) else ""

    symbol, series, name, side = (cell(column) for column in BH_COLUMNS)
    if not symbol or not series:
        raise ParseError(f"row has no symbol or series: {raw!r}", filename=filename, line=line)
    try:
        band = BandSide(side.upper())
    except ValueError as exc:
        raise ParseError(
            f"HIGH/LOW for {symbol!r} is {side!r}, not H or L", filename=filename, line=line
        ) from exc
    return BandHitRow(
        session=publication_date,
        publication_date=publication_date,
        symbol=symbol,
        series=series,
        security_name=name,
        side=band,
        l0_key=l0_key,
    )
