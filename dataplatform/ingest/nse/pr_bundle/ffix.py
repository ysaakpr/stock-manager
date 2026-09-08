"""`ffix<DDMMYY>.csv` — dated index constituent membership with free-float weightage (W2).

**This member is not what our own registry said it was.** `MemberKind.FFIX` shipped with the
docstring *"Fixed income"*, and that one wrong word is why nobody looked here for three years. The
payload is the **free-float index file**: for every index NSE published that session, every
constituent security, its investible (free-float) factor, its close, its free-float market cap and
its **weightage in the index**. `ffix` is `f`ree-`f`loat `i`nde`x`, not fixed income. Nothing in
it is a bond.

What that corrects, measured off the whole L0 corpus on 2026-09-08 rather than sampled:

| | |
|---|---|
| bundles carrying an `ffix` member | **827**, and every one of them parses |
| span | **2010-01-04 .. 2013-04-30** (nothing before, nothing after — swept all 4,124 bundles) |
| distinct indices | **17** |
| constituent rows | **711,344** |
| header shapes across 3.3 years | **one** |

`ops/BACKLOG.md:126`, `AGENTIC_CONTEXT §4.1` and the multi-fund study all record that historical
index membership "does not exist to fetch" and can only accrue forward from today. For
**2010-01-04 .. 2013-04-30 that is now false**, and `membership.py` measures exactly how false.
The premise survives for everything outside those 3.3 years.

**Format eras: there are none the parser can see, and that is a measurement.** The brief for this
task assumed the format changes somewhere across 3.3 years. It does not. All 827 files carry the
identical 9-column header, the identical row shape, and `INDEX_FLG` values that are stable strings
for the whole life of each index. What *does* change is:

* the **index set**, in five arrival cohorts — 3 indices from 2010-01-04, +4 on 2010-02-26,
  +1 on 2010-07-19, +2 on 2010-10-11, +7 on 2011-01-31 — and no index ever leaves;
* one **banner string**, when `S&P CNX Nifty Sec.` becomes `CNX Nifty Sec.` on 2013-03-04, relapses
  to the old text for exactly one session on 2013-04-09, and settles from 2013-04-10.

Neither is a parse era, because this reader dispatches on neither: membership comes from
`INDEX_FLG`, which the rename never touched. The banner is read for `announced_indices` and is
never allowed to decide what an index is called. Four fixtures are frozen anyway — one per cohort
edge and one on each side of the banner relapse — because "the format never changed" is a claim
that has to keep being true, and a fixture is how it fails loudly when it stops being.

**File quirks, handled by rule and not by line number.** A constituent row has 9 fields and a
non-blank `INDEX_FLG`. Anything else in the body is furniture:

* a **banner** — 8 fields, blank `INDEX_FLG`, the index's display name sitting in `SECURITY`
  (`' , , ,S&P CNX Nifty Sec., , , , '`);
* a **separator** — every field blank (`' , , , , , , , '`). 22,175 of them corpus-wide, eight in
  the first file alone.

Both are skipped on the *blank `INDEX_FLG`* rule, never on a row index: the count of separators
per file varies, so anything positional would silently eat a constituent the day a separator
moved. Numeric fields carry leading pad (`'       765.40'`) and are stripped before conversion.

**Money and weights are `Decimal`.** `weightage`, `investible_factor`, `close_price`,
`ff_market_cap` and `issue_cap` all come from the published text straight into `Decimal` and never
through `float` — a weight column that has to sum to 100 cannot be held in binary floating point,
and `ISSUE_CAP` is a nine-to-ten digit share count that `float` would start rounding.

**`knowable_date` is the bundle's own publication date, and no clock is involved.** It comes from
`PrBundle.publication_date`, which derives it from the digits the members carry in their own
names. This module imports no clock and takes none. The defect this avoids is already in the
repo and visible: all 47,887 rows in `corporate_actions` share one `knowable_date` because a
sibling ingester stamped `clock.now().date()`, which satisfies invariant #7 vacuously.

**Symbol-keyed, and deliberately unjoinable.** `FfixRow` has **no `isin` field** and this module
never touches `security_master`. ISIN is the only join key (invariant #2) and these are 2010-2013
symbols, reused since across different issuers; resolving them through today's `EQUITY_L.csv`
would invent a mapping biased precisely toward the survivors. Symbol→ISIN here is W4 identity
work, gated on a point-in-time symbol master we do not hold. Until then this dataset is a
symbol-keyed dated membership series and is named that way everywhere it is used.

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
    "FFIX_BANNER_FIELDS",
    "FFIX_COLUMNS",
    "FFIX_FIRST_SESSION",
    "FFIX_LAST_SESSION",
    "FfixFile",
    "FfixRow",
    "parse_ffix",
    "parse_ffix_bundle",
]

_LOG = get_logger(__name__)

#: The published header, in order. **Identical in all 827 files** — swept, not sampled.
FFIX_COLUMNS: Final[tuple[str, ...]] = (
    "INDEX_FLG",
    "SYMBOL",
    "SERIES",
    "SECURITY",
    "ISSUE_CAP",
    "INVESTIBLE_FACTOR",
    "CLOSE_PRIC",
    "FF_MKT_CAP",
    "WEIGHTAGE",
)

#: Banner and separator lines carry **8** fields, not 9 — the writer omits the trailing one. So a
#: short row is furniture rather than a truncated constituent, and the blank-`INDEX_FLG` rule
#: below is what decides that; this constant only spares the length check from rejecting it.
FFIX_BANNER_FIELDS: Final = 8

#: The first and last sessions any bundle carries an `ffix` member for, pinned by sweeping every
#: one of the 4,124 bundles in L0 on 2026-09-08 — not bracketed, and not extrapolated from the
#: ends. `ffix` appears in the very first bundle the archive serves (`ARCHIVE_START`) and stops
#: dead after 2013-04-30, with no member on any later session.
FFIX_FIRST_SESSION: Final = date(2010, 1, 4)
FFIX_LAST_SESSION: Final = date(2013, 4, 30)


class FfixRow(BaseModel):
    """One security's membership of one index on one session, with its free-float weight.

    **No `isin` field, by design.** See the module docstring: these are 2010-2013 symbols and
    inventing a mapping to today's ISINs would be survivorship-biased in the one direction that
    matters to a backtest. `symbol` is an identifier for *this dataset only* and is not a join key.

    Every numeric field is `Decimal`. `weightage` is a percentage as published; the constituents
    of one index on one date sum to ~100 within the source's own rounding, and that check is only
    meaningful because nothing here passed through `float`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    knowable_date: date = Field(
        description="the bundle's own publication date — the date this membership became public"
    )
    index_name: str = Field(min_length=1, description="INDEX_FLG verbatim, e.g. 'NIFTY', 'CNX IT'")
    symbol: str = Field(min_length=1, description="NSE trading symbol; NOT a join key, NOT an ISIN")
    series: str = Field(description="the trading series; EQ on 708,808 rows, BE on 2,536")
    security_name: str = Field(description="issuer name as published")
    issue_cap: Decimal = Field(description="ISSUE_CAP — shares counted toward the index")
    investible_factor: Decimal = Field(
        description="INVESTIBLE_FACTOR — the free-float fraction, 0..1"
    )
    close_price: Decimal = Field(description="CLOSE_PRIC in rupees")
    ff_market_cap: Decimal = Field(description="FF_MKT_CAP — free-float market cap in rupees")
    weightage: Decimal = Field(description="WEIGHTAGE in the index, as published, in percent")
    source: str = Field(default=PR_BUNDLE_SOURCE_ID, description="Source Register id")
    l0_key: str | None = Field(default=None, description="the L0 payload this row derives from")


class FfixFile(BaseModel):
    """One `ffix` member: its constituent rows, and which indices it actually carried.

    `indices` answers the rotation question for one date and is what the census's availability
    table is built from — an index absent from this tuple was **not published that session**, which
    is a fact about the source and not a hole in the parse.

    `announced_indices` is the banner text, kept separately and never merged into `indices`,
    because the two vocabularies disagree on purpose: the banner said `S&P CNX Nifty Sec.` while
    `INDEX_FLG` said `NIFTY`, and in March 2013 the banner renamed while `INDEX_FLG` did not.
    Keying membership off the banner would have manufactured a spurious index that year.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    knowable_date: date
    rows: tuple[FfixRow, ...]
    indices: tuple[str, ...] = Field(description="distinct INDEX_FLG values, in first-seen order")
    announced_indices: tuple[str, ...] = Field(
        default=(), description="banner display names, in first-seen order; not index identities"
    )
    separator_rows: int = Field(
        default=0, ge=0, description="all-blank furniture lines skipped, for the census"
    )

    def constituents(self, index_name: str) -> tuple[FfixRow, ...]:
        """Every row of one index, in published order. Empty tuple if it was not published."""
        return tuple(row for row in self.rows if row.index_name == index_name)

    def symbols(self, index_name: str) -> frozenset[str]:
        """The constituent symbol set of one index — the unit a membership change is diffed on."""
        return frozenset(row.symbol for row in self.rows if row.index_name == index_name)


def parse_ffix(
    payload: bytes,
    *,
    filename: str,
    knowable_date: date,
    l0_key: str | None = None,
) -> FfixFile:
    """Parse one `ffix` member into typed, symbol-keyed constituent rows.

    Assumes `knowable_date` was derived from the payload — normally by `parse_ffix_bundle` from
    `PrBundle.publication_date`. This function has no clock and will not invent one.

    Skips the file's two kinds of furniture by rule and never by line number: a **separator** (every
    field blank) and a **banner** (blank `INDEX_FLG`, display name in `SECURITY`). A banner naming
    an index is recorded in `announced_indices`; membership itself comes only from `INDEX_FLG`.

    Raises `ParseError` naming the file and the 1-based line for a header that is not
    `FFIX_COLUMNS`, a constituent row with the wrong field count, a constituent row with no
    `SYMBOL`, or a numeric field that is not a number. It never coerces and never skips a row it
    failed to understand — a silently dropped constituent is an invented reconstitution event.
    """
    text = _decode(payload, filename=filename)
    lines = list(csv.reader(StringIO(text)))
    if not lines:
        raise ParseError("file is empty", filename=filename)

    header = tuple(cell.strip().upper() for cell in lines[0])
    if header != FFIX_COLUMNS:
        raise ParseError(
            f"unexpected header {header!r}; expected {FFIX_COLUMNS!r}", filename=filename, line=1
        )

    parsed: list[FfixRow] = []
    order: list[str] = []
    announced: list[str] = []
    separators = 0
    for line_no, raw in enumerate(lines[1:], start=2):
        if not raw or not any(cell.strip() for cell in raw):
            # A separator: 22,175 of these corpus-wide, and their count per file varies, which is
            # why nothing here is positional.
            separators += 1
            continue
        if not raw[0].strip():
            # A banner: the index's display name sits in SECURITY and nothing else is filled in.
            name = raw[3].strip() if len(raw) > 3 else ""
            if name and name not in announced:
                announced.append(name)
            continue
        if len(raw) != len(FFIX_COLUMNS):
            raise ParseError(
                f"expected {len(FFIX_COLUMNS)} columns, got {len(raw)}: {raw!r}",
                filename=filename,
                line=line_no,
            )
        row = _row(
            raw,
            filename=filename,
            line=line_no,
            knowable_date=knowable_date,
            l0_key=l0_key,
        )
        if row.index_name not in order:
            order.append(row.index_name)
        parsed.append(row)

    result = FfixFile(
        knowable_date=knowable_date,
        rows=tuple(parsed),
        indices=tuple(order),
        announced_indices=tuple(announced),
        separator_rows=separators,
    )
    _LOG.info(
        "pr_bundle_ffix.parsed",
        source=PR_BUNDLE_SOURCE_ID,
        filename=filename,
        knowable_date=knowable_date.isoformat(),
        rows=len(result.rows),
        indices=list(result.indices),
        separator_rows=separators,
        state="VALIDATED",
    )
    return result


def parse_ffix_bundle(bundle: PrBundle, *, l0_key: str | None = None) -> FfixFile:
    """Parse the `ffix` member of an opened bundle, dated by that bundle's own publication date.

    `bundle.publication_date` is the only source of `knowable_date` here — derived from the digits
    the bundle's members carry in their own names, so it survives re-download and mirroring. No
    clock is read (`tests/unit/test_pr_bundle_ffix.py` fails if that changes).

    Raises `ParseError` when the bundle has no `ffix` member, which is the normal case for every
    session after `FFIX_LAST_SESSION`. Callers sweeping a range should test
    `bundle.has(MemberKind.FFIX)` first.
    """
    member = bundle.member(MemberKind.FFIX)
    return parse_ffix(
        bundle.read(MemberKind.FFIX),
        filename=bundle.filename if member is None else member.name,
        knowable_date=bundle.publication_date,
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
    knowable_date: date,
    l0_key: str | None,
) -> FfixRow:
    """One constituent line → one `FfixRow`. Every numeric field strips its pad, then `Decimal`."""
    index_name, symbol, series, security, issue_cap, factor, close, ff_cap, weight = (
        cell.strip() for cell in raw
    )
    if not symbol:
        raise ParseError(
            f"constituent row of {index_name!r} has no SYMBOL", filename=filename, line=line
        )
    return FfixRow(
        knowable_date=knowable_date,
        index_name=index_name,
        symbol=symbol,
        series=series,
        security_name=security,
        issue_cap=_decimal(issue_cap, field="ISSUE_CAP", filename=filename, line=line),
        investible_factor=_decimal(factor, field="INVESTIBLE_FACTOR", filename=filename, line=line),
        close_price=_decimal(close, field="CLOSE_PRIC", filename=filename, line=line),
        ff_market_cap=_decimal(ff_cap, field="FF_MKT_CAP", filename=filename, line=line),
        weightage=_decimal(weight, field="WEIGHTAGE", filename=filename, line=line),
        l0_key=l0_key,
    )


def _decimal(text: str, *, field: str, filename: str, line: int) -> Decimal:
    """A money, weight or share-count field, as `Decimal` and never through `float`.

    Raises rather than coercing. A weightage that is not a number means the row's index share is
    unknown, and a zero substituted for it would rebalance a reconstructed index silently.
    """
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise ParseError(
            f"{field} is {text!r}, not a number", filename=filename, line=line
        ) from exc
