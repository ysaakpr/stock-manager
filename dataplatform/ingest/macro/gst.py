"""GSTN's gross and net GST collection statistics → monthly collection series.

`www.gst.gov.in/download/gststatistics` links `Gross_Net_Tax_collection.xlsx` on
`tutorial.gst.gov.in`, one sheet per month (`Apr-24` … `Aug-26`; one is spelt `Mar_26`). Each sheet
is the month's published collection statement in crore rupees: domestic and import revenue, refunds,
and net revenue, with the same month a year earlier beside it for growth.

**Not a vintage archive, and hand-laid-out.** The file is replaced in place every month under one
name, its figures are labelled provisional, and the sheets were evidently built by hand: some carry
an extra "Daily" column, some date their header with an Excel serial number and others with text.
So the parser keys on what is stable across all of them — the row *label* in column A and the
`% Growth` column, whose left neighbour is always the current month — and refuses a sheet where
either is missing rather than guessing. Tier B forward capture: `release_date` is the capture date
(GSTN states no release date in the file), and the caller writes only values that are new or
changed against what the store already knew, so a later revision of a provisional month lands as a
second record rather than overwriting the first.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime
from decimal import InvalidOperation
from typing import Final

from dataplatform.ingest.macro.models import (
    Frequency,
    MacroFact,
    MacroRelease,
    Unit,
    series_id,
    store_value,
)
from dataplatform.ingest.macro.xlsx import read_workbook
from dataplatform.ingest.models import ParseError

__all__ = [
    "GST_COLLECTION_ROWS",
    "GST_COLLECTION_URL",
    "GST_SOURCE_ID",
    "gst_filename",
    "parse_gst_collections",
]

#: The register id whose bytes this parser reads.
GST_SOURCE_ID: Final = "gstn_tax_collection"

GST_COLLECTION_URL: Final = (
    "https://tutorial.gst.gov.in/offlineutilities/gst_statistics/Gross_Net_Tax_collection.xlsx"
)

#: Column-A label prefix → `series_id` subject. Both must be on every sheet.
GST_COLLECTION_ROWS: Final[tuple[tuple[str, str], ...]] = (
    ("total gross gst revenue", "GST_GROSS_REVENUE"),
    ("total net gst revenue", "GST_NET_REVENUE"),
)

_SHEET_MONTH: Final = re.compile(r"^([A-Za-z]{3})[A-Za-z]?[-_ ](\d{2})$")
_GROWTH: Final = re.compile(r"%\s*growth", re.I)


def gst_filename(captured: date) -> str:
    """L0 filename for one capture of the workbook."""
    return f"Gross_Net_Tax_collection_{captured:%Y%m%d}.xlsx"


def parse_gst_collections(
    payload: bytes, *, captured: date, filename: str, l0_key: str | None = None
) -> MacroRelease:
    """Parse the collection workbook into one release dated `captured`, one fact per sheet and row.

    What it does: for each month sheet, take the month from the sheet name, the current-month
    column from the first `% Growth` header in the first five rows, and the gross and net totals
    from the rows labelled in `GST_COLLECTION_ROWS`.
    What it assumes: `captured` is the L0 logical date of the fetch.
    What it never does: read the prior-year comparison column, sum components itself, or let a
    sheet whose layout it cannot place contribute a number.

    Raises `ParseError` for a non-xlsx body, an unrecognisable sheet name, a sheet with no
    `% Growth` header or missing a total row, a non-numeric total, or a month not yet ended on the
    capture date.
    """
    try:
        sheets = read_workbook(payload)
    except ValueError as error:
        raise ParseError(str(error), filename=filename) from error
    facts: list[MacroFact] = []
    for name, grid in sheets:
        start = _sheet_month(name, filename=filename)
        end = start.replace(day=calendar.monthrange(start.year, start.month)[1])
        if end > captured:
            raise ParseError(
                f"sheet {name!r} is for a month not ended by {captured}", filename=filename
            )
        column = _current_column(grid, sheet=name, filename=filename)
        for prefix, subject in GST_COLLECTION_ROWS:
            row = next(
                (r for (r, c), text in sorted(grid.items()) if c == 1 and _label(text) == prefix),
                None,
            )
            if row is None:
                raise ParseError(f"sheet {name!r} has no {prefix!r} row", filename=filename)
            text = grid.get((row, column))
            if text is None:
                raise ParseError(
                    f"sheet {name!r}: {prefix!r} has no current value", filename=filename
                )
            try:
                value = store_value(text)
            except (InvalidOperation, ArithmeticError) as error:
                raise ParseError(
                    f"sheet {name!r}: {prefix!r} = {text!r}", filename=filename
                ) from error
            facts.append(
                MacroFact(
                    series_id=series_id("IN", "GSTN", subject, "MONTHLY"),
                    period_start=start,
                    period_end=end,
                    release_date=captured,
                    frequency=Frequency.MONTHLY,
                    unit=Unit.INR_CRORE,
                    value=value,
                    source=GST_SOURCE_ID,
                    l0_key=l0_key,
                )
            )
    if not facts:
        raise ParseError("workbook has no month sheets", filename=filename)
    return MacroRelease(
        release_date=captured, source=GST_SOURCE_ID, facts=tuple(facts), l0_key=l0_key
    )


def _label(text: str) -> str:
    return " ".join(text.lower().split())


def _sheet_month(name: str, *, filename: str) -> date:
    match = _SHEET_MONTH.match(name.strip())
    if match is None:
        raise ParseError(f"sheet {name!r} is not named Mon-YY", filename=filename)
    try:
        return datetime.strptime(f"{match.group(1).title()}-{match.group(2)}", "%b-%y").date()
    except ValueError as error:
        raise ParseError(f"sheet {name!r}: {error}", filename=filename) from error


def _current_column(grid: dict[tuple[int, int], str], *, sheet: str, filename: str) -> int:
    """The column left of the first `% Growth` header — the month the sheet reports."""
    growth = sorted((r, c) for (r, c), text in grid.items() if r <= 5 and _GROWTH.search(text))
    if not growth:
        raise ParseError(f"sheet {sheet!r} has no '% Growth' header", filename=filename)
    _row, col = growth[0]
    if col < 3:
        raise ParseError(f"sheet {sheet!r}: '% Growth' at column {col}", filename=filename)
    return col - 1
