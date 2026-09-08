"""Explicit format-era registration for the NSE cash bhavcopy archive (W1).

`bhavcopy.era_of` answers the only question the *daily* pipeline has ever had to ask — legacy file
or UDiFF file — because the lake began in September 2016 and everything before the 2024 cutover
looked the same. A backfill to 2006 breaks that assumption: the archive reaches 1995, and somewhere
inside the "legacy" half the exchange started publishing an ISIN. So a backfill needs a finer
question than `era_of` can answer, and this module is that question asked once.

Three eras, each a half-open `[start, end)` span, each measured rather than believed:

| era | span | header | identity |
|---|---|---|---|
| **E1** `pre_isin` | 1995-01-02 → 2011-06-22 | 11 names + trailing comma | **none** |
| **E2** `legacy`   | 2011-06-22 → 2024-07-08 | 13 names + trailing comma | `ISIN` column |
| **E3** `udiff`    | 2024-07-08 → *(open)*   | 34 UDiFF columns          | `ISIN` column |

The E1/E2 boundary is a clean single-day cutover, bisected against the live archive on 2026-09-07
(`ops/studies/evidence/india-acquisition-atlas.md` §A1): `cm21JUN2011bhav.csv.zip` is 11 columns and
42,395 bytes, `cm22JUN2011bhav.csv.zip` is 14 tokens with an `ISIN` and 53,301 bytes, both HTTP 200
in the same session. `tests/unit/test_bhavcopy_eras.py` pins that pair against the frozen fixtures,
so the boundary cannot move by a day without a test going red.

**E1 is retained, not rescued.** It carries no ISIN, and ISIN is the only join key (invariant #2),
so an E1 row can never become a `PriceRow` — `carries_isin` is the flag that says so, and the
backfill driver reads it to decide whether a session's rows go to `prices_raw` or to
`prices_raw_quarantine`. Resolving E1 through a current-day listing would be survivorship-biased in
exactly the direction that matters (every company delisted before today is simply absent from
`EQUITY_L.csv`), so nothing here offers a symbol→ISIN path. That is W4 identity work.

This module holds no dates of its own that another module also holds: `E2`'s end and `E3`'s start
are `bhavcopy.CUTOVER`, asserted below, so the two dispatchers can never drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from itertools import pairwise
from typing import Final

from dataplatform.ingest.nse import bhavcopy
from dataplatform.ingest.nse.bhavcopy_legacy import LEGACY_SOURCE_ID
from dataplatform.ingest.nse.bhavcopy_udiff import UDIFF_SOURCE_ID

__all__ = [
    "ARCHIVE_START",
    "ERAS",
    "ISIN_ERA_START",
    "PRE_ISIN_ERA_LAST_SESSION",
    "BhavcopyEra",
    "EraCoverageError",
    "era_for",
    "eras_in",
]


class EraCoverageError(ValueError):
    """A date the NSE bhavcopy archive makes no claim about — before it begins."""


#: The first session the archive serves. `cm02JAN1995bhav.csv.zip` returned HTTP 200 / 4,177 B /
#: 204 rows on 2026-09-07. The source register's `era.start: null` understates this; correcting the
#: register is a W0 file-scope change, not this module's.
ARCHIVE_START: Final = date(1995, 1, 2)

#: The first session whose bhavcopy carries an `ISIN` column, and therefore the first session this
#: platform can promote to `prices_raw` at all. Measured, not assumed — see the module docstring.
ISIN_ERA_START: Final = date(2011, 6, 22)

#: The last session published *without* an ISIN column. Stated as its own constant rather than
#: derived, so a test can assert the pair `(2011-06-21, 2011-06-22)` and fail if either moves.
PRE_ISIN_ERA_LAST_SESSION: Final = date(2011, 6, 21)


@dataclass(frozen=True, slots=True)
class BhavcopyEra:
    """One format era of the NSE cash bhavcopy: its span, its file, and whether it has an identity.

    What it does: name the half-open span `[start, end)` a single header shape covers, the source
    register id whose URL template and crawl policy serve it, and whether its rows can be keyed.
    What it assumes: eras tile the archive without gaps or overlaps — `ERAS` is checked for that at
    import.
    What it never does: describe how to *resolve* an era that has no ISIN. There is no legal
    symbol→ISIN path that is not the D2 master (invariant #2).
    """

    label: str
    id: str
    start: date
    end: date | None
    source_id: str
    carries_isin: bool
    header_note: str

    def covers(self, day: date) -> bool:
        """Whether this era published `day` — inclusive of `start`, exclusive of `end`."""
        return self.start <= day and (self.end is None or day < self.end)

    @property
    def fixture_dir(self) -> str:
        """Where this era's frozen payloads live, relative to `tests/fixtures/`."""
        return f"nse_bhavcopy/{self.id}"


#: The archive's format eras, ascending and contiguous. `end` is exclusive; `None` is open-ended.
ERAS: Final[tuple[BhavcopyEra, ...]] = (
    BhavcopyEra(
        label="E1",
        id="pre_isin",
        start=ARCHIVE_START,
        end=ISIN_ERA_START,
        source_id=LEGACY_SOURCE_ID,
        carries_isin=False,
        header_note="11 names + trailing comma; no TOTALTRADES, no ISIN",
    ),
    BhavcopyEra(
        label="E2",
        id="legacy",
        start=ISIN_ERA_START,
        end=bhavcopy.CUTOVER,
        source_id=LEGACY_SOURCE_ID,
        carries_isin=True,
        header_note="13 names + trailing comma; ISIN native",
    ),
    BhavcopyEra(
        label="E3",
        id="udiff",
        start=bhavcopy.CUTOVER,
        end=None,
        source_id=UDIFF_SOURCE_ID,
        carries_isin=True,
        header_note="34 UDiFF columns; ISIN native",
    ),
)

# The eras must tile the archive: every date from 1995 on falls in exactly one. A gap would let a
# session be planned with no parser and no quarantine path, and an overlap would let two eras claim
# the same file — both of which are silent until a decade of rows lands in the wrong place.
assert ERAS[0].start == ARCHIVE_START, "the first era must start where the archive does"
assert all(earlier.end == later.start for earlier, later in pairwise(ERAS)), (
    "bhavcopy eras must tile without gaps or overlaps"
)
assert ERAS[-1].end is None, "the last era must be open-ended"
assert PRE_ISIN_ERA_LAST_SESSION < ISIN_ERA_START, "the pre-ISIN era must end before the ISIN one"


def era_for(day: date) -> BhavcopyEra:
    """The format era the exchange published `day` in.

    Raises `EraCoverageError` below `ARCHIVE_START` rather than guessing: the archive serves
    nothing there, and a planner that quietly treated 1990 as "probably E1" would spend requests
    proving it.
    """
    for era in ERAS:
        if era.covers(day):
            return era
    raise EraCoverageError(
        f"{day.isoformat()} is before the NSE bhavcopy archive begins "
        f"({ARCHIVE_START.isoformat()}); there is no era and no file to fetch"
    )


def eras_in(start: date, end: date) -> tuple[BhavcopyEra, ...]:
    """Every era that published at least one day of the inclusive range, ascending.

    What a range spans is a planning fact an operator wants before a campaign starts — "this run
    crosses the ISIN boundary" is the difference between a price backfill and a retention exercise.
    """
    if start > end:
        raise ValueError(f"start {start} is after end {end}")
    era_for(
        start
    )  # raises below ARCHIVE_START, so an impossible range never returns an empty tuple
    return tuple(era for era in ERAS if era.start <= end and (era.end is None or start < era.end))
