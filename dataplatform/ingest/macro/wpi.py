"""The Office of the Economic Adviser's monthly WPI file → headline wholesale price indices.

`eaindustry.nic.in` (DPIIT's Office of the Economic Adviser) publishes the Wholesale Price Index as
one `.xlsx` per release, `indx_download_2223/wpi_monthly_index_<YYYYMM>.xlsx`, where `YYYYMM` is the
*release* month — the 202609 file carries index values through Aug-26 — and every month of the
2022-23-base series (Apr-23 onward) is a column. The download page names the current file, so a
capture reads the page first and fetches whichever file it links.

**Not a vintage archive.** The file is a full current-vintage table: the latest two months are
provisional and are replaced by final figures two releases later, silently, in the next file. So
this is Tier B forward capture. Each capture lands with `release_date` = the capture date — the
file states no release *day*, only its month, and the capture date is never earlier than the
publication — and the caller writes only the observations whose value is new or changed against what
the store already knew (`capture.new_or_revised`). A provisional print and its later final figure
therefore coexist as two records, exactly as `macro_series` intends, from the first capture forward.
Nothing before the first capture is knowable from here, and the store has no partition to say
otherwise.

Read: the all-commodities index, the three major groups and the food index. The 1,100 item rows are
left in L0 — a parse that wants them later re-derives from the stored bytes.
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
    "WPI_DOWNLOAD_PAGE_URL",
    "WPI_HEADLINE_CODES",
    "WPI_SOURCE_ID",
    "parse_wpi_download_page",
    "parse_wpi_monthly_index",
    "wpi_page_filename",
]

#: The register id whose bytes this parser reads.
WPI_SOURCE_ID: Final = "oea_wpi_monthly_index"

WPI_DOWNLOAD_PAGE_URL: Final = "https://eaindustry.nic.in/download_data_2223.asp"

#: Commodity code → `series_id` subject for the headline rows. The food index has no code in the
#: file; it is matched by its name in the "Commodity Name" column.
WPI_HEADLINE_CODES: Final[dict[str, str]] = {
    "1000000000": "WPI_ALL_COMMODITIES",
    "1100000000": "WPI_PRIMARY_ARTICLES",
    "1200000000": "WPI_FUEL_AND_POWER",
    "1300000000": "WPI_MANUFACTURED_PRODUCTS",
}
_FOOD_INDEX_NAME: Final = "FOOD INDEX"
_FOOD_INDEX_SUBJECT: Final = "WPI_FOOD_INDEX"
#: The measure names the base, so a future 2032-33 rebase lands as a different series rather than
#: being spliced onto this one by accident.
_MEASURE: Final = "INDEX_2022_23"

_FILE_LINK: Final = re.compile(
    r"""href=["']?(indx_download_2223/wpi_monthly_index_(\d{6})\.xlsx)"""
)
_MONTH_HEADER: Final = re.compile(r"^[A-Za-z]{3}-\d{2}$")


def wpi_page_filename(captured: date) -> str:
    """L0 filename for one capture of the download page."""
    return f"download_data_2223_{captured:%Y%m%d}.html"


def parse_wpi_download_page(payload: bytes, *, filename: str) -> tuple[str, str]:
    """The current monthly WPI file the download page links: `(absolute URL, YYYYMM)`.

    Raises `ParseError` when the page links no monthly WPI file, or links two — which one is
    "current" would then be a guess.
    """
    text = payload.decode("utf-8", "replace")
    found = {(path, month) for path, month in _FILE_LINK.findall(text)}
    if len(found) != 1:
        raise ParseError(
            f"expected exactly one monthly WPI file link, found {sorted(found)!r}",
            filename=filename,
        )
    path, month = found.pop()
    return f"https://eaindustry.nic.in/{path}", month


def parse_wpi_monthly_index(
    payload: bytes, *, captured: date, filename: str, l0_key: str | None = None
) -> MacroRelease:
    """Parse a monthly WPI workbook into one release dated `captured`, every month as a fact.

    What it does: find the header row (`Level`, `Commodity Name`, `Commodity Code`, then one
    `Mon-YY` column per month), and emit a monthly fact per headline row per month with a value.
    What it assumes: `captured` is the L0 logical date of the fetch, on or after the publication.
    What it never does: date a month by itself, read a blank as zero, or skip a headline row — each
    of the five must be present.

    Raises `ParseError` for a body that is not an xlsx, a missing header or headline row, a
    malformed month header, or a non-numeric index value.
    """
    try:
        sheets = read_workbook(payload)
    except ValueError as error:
        raise ParseError(str(error), filename=filename) from error
    if not sheets:
        raise ParseError("workbook has no sheets", filename=filename)
    _name, grid = sheets[0]

    months: dict[int, tuple[date, date]] = {}
    for (row, col), text in grid.items():
        if row == 1 and col >= 5:
            if not _MONTH_HEADER.match(text.strip()):
                raise ParseError(f"header column {col} is {text!r}, not Mon-YY", filename=filename)
            start = datetime.strptime(text.strip(), "%b-%y").date()
            end = start.replace(day=calendar.monthrange(start.year, start.month)[1])
            months[col] = (start, end)
    if grid.get((1, 1), "").strip() != "Level" or not months:
        raise ParseError("no 'Level … Mon-YY' header row", filename=filename)

    wanted: dict[int, str] = {}
    for (row, col), text in grid.items():
        if col == 3 and text.strip() in WPI_HEADLINE_CODES:
            wanted[row] = WPI_HEADLINE_CODES[text.strip()]
        elif col == 2 and text.strip().upper() == _FOOD_INDEX_NAME:
            wanted[row] = _FOOD_INDEX_SUBJECT
    missing = set(WPI_HEADLINE_CODES.values()) | {_FOOD_INDEX_SUBJECT}
    missing -= set(wanted.values())
    if missing:
        raise ParseError(f"headline rows missing: {sorted(missing)}", filename=filename)

    facts: list[MacroFact] = []
    for row, subject in sorted(wanted.items()):
        sid = series_id("IN", "OEA", subject, _MEASURE)
        for col, (start, end) in sorted(months.items()):
            cell = grid.get((row, col))
            if cell is None:
                continue
            try:
                value = store_value(cell)
            except (InvalidOperation, ArithmeticError) as error:
                raise ParseError(
                    f"{subject} {start:%b-%y}: {cell!r} is not a number", filename=filename
                ) from error
            if end > captured:
                raise ParseError(
                    f"{subject} has a value for {start:%b-%y}, which had not ended by the capture "
                    f"date {captured}",
                    filename=filename,
                )
            facts.append(
                MacroFact(
                    series_id=sid,
                    period_start=start,
                    period_end=end,
                    release_date=captured,
                    frequency=Frequency.MONTHLY,
                    unit=Unit.INDEX,
                    value=value,
                    source=WPI_SOURCE_ID,
                    l0_key=l0_key,
                )
            )
    return MacroRelease(
        release_date=captured, source=WPI_SOURCE_ID, facts=tuple(facts), l0_key=l0_key
    )
