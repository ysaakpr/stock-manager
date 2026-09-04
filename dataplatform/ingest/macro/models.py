"""The canonical shape of one macro observation, and the `series_id` taxonomy it is keyed by.

One model carries every macro series — an index P/E published the same evening, a CPI print
released six weeks after the month it measures, a policy rate that changes on a meeting day. What
makes that possible is keeping two dates apart and never letting them collapse:

* **`period_end`** (with optional `period_start`) is *what the number describes*. April CPI has a
  April period.
* **`release_date`** is *when the number became knowable*. April CPI released on 12 May has a May
  release date, and a decision taken on 5 May could not have used it.

Joining a macro series on its period instead of its release date is the purest form of look-ahead
bias, and it is invisible because every number looks plausible. The store partitions by
`release_date` for exactly the reason `pit_fundamentals` partitions by `filing_date`: it makes
invariant #7 structural rather than a `WHERE` clause a caller can forget.

**A revision is a new record, never an overwrite** (the same rule invariant #8 sets for restated
fundamentals). India's IIP and GDP are revised routinely; a store that overwrote them would destroy
the number the market actually saw and fabricate a knowable date it never had. A revision lands
with a later `release_date` — or, if it is republished the same day, a higher `revision_seq`.

`value` is `Decimal`, never `float`. A macro series is not money, but it feeds signals that are, and
a yield that round-trips through binary floating point is a defect of the same shape.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "SERIES_ID_PATTERN",
    "Frequency",
    "MacroFact",
    "MacroRelease",
    "Unit",
    "series_id",
]


class Frequency(StrEnum):
    """How often a series is published — the cadence, not the lag.

    `EVENT` is for series with no cadence at all: a policy rate changes when the committee decides,
    and forward-filling it between meetings is the reader's job, not the store's.
    """

    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"
    QUARTERLY = "QUARTERLY"
    ANNUAL = "ANNUAL"
    EVENT = "EVENT"


class Unit(StrEnum):
    """What the number is denominated in — so a consumer never has to infer it from the name.

    Kept small and explicit. `RATIO` is a pure multiple (a P/E of 22.08), `PCT` is already in
    percentage points (a yield of 1.35 means 1.35 %, not 135 %), and `INDEX` is a level whose
    absolute value means nothing without its base.
    """

    PCT = "PCT"
    BPS = "BPS"
    RATIO = "RATIO"
    INDEX = "INDEX"
    INR_CRORE = "INR_CRORE"
    COUNT = "COUNT"


#: A `series_id` is dotted, upper-case and hierarchical:
#: `<country>.<publisher>.<subject>.<measure>`.
#: Dotted rather than free text because the prefix is how a consumer selects a family — every NSE
#: index valuation series is `IN.NSE.*.PE`, and a query for "the market's P/E history" is a prefix
#: match rather than a hand-maintained list.
#:
#: The 90-character per-level bound is set by real data, not taste: NSE publishes index names like
#: "NIFTY India Corporate Group Index - Tata Group 25% Cap", which is 51 characters once
#: separator-safe. A tighter bound rejected a real 2026 index outright.
SERIES_ID_PATTERN: Final = re.compile(r"^[A-Z0-9]{2,}(?:\.[A-Z0-9_]{1,90}){2,5}$")

SeriesId = Annotated[str, Field(min_length=5, max_length=200)]
MacroValue = Annotated[Decimal, Field(description="the published value, exact")]


def series_id(country: str, publisher: str, subject: str, measure: str) -> str:
    """Build a canonical `series_id`, upper-cased and separator-safe.

    What it does: join the four levels with `.`, upper-case them, and replace any character that is
    not `[A-Z0-9_]` inside a level with `_` — so a published index name like "Nifty Bank" becomes
    `NIFTY_BANK` and cannot smuggle a `.` into the hierarchy.
    What it assumes: the caller already resolved the subject to its *canonical* form — an index that
    has been renamed must arrive here under one name, or its history splits in two.
    What it never does: invent a level. All four are required.
    """
    levels = [country, publisher, subject, measure]
    if not all(level and level.strip() for level in levels):
        raise ValueError(f"every series_id level must be non-empty, got {levels!r}")
    cleaned = [re.sub(r"[^A-Z0-9_]+", "_", level.strip().upper()).strip("_") for level in levels]
    built = ".".join(cleaned)
    if not SERIES_ID_PATTERN.match(built):
        raise ValueError(
            f"{built!r} is not a valid series_id (pattern {SERIES_ID_PATTERN.pattern})"
        )
    return built


class MacroFact(BaseModel):
    """One macro observation: a value, the period it describes, and the date it became knowable.

    What it does: carry a single published number under its canonical `series_id`, tagged with both
    dates the point-in-time machinery needs and the `source`/`l0_key` that make it traceable back to
    the bytes it was parsed from.
    What it assumes: `release_date` is the first date the figure could honestly have been used — the
    publication date, not the date we fetched it. A fetch-time stamp would make every backfilled
    fact look knowable on the day of the backfill, which is the fabrication invariant #8 forbids.
    What it never does: hold a `float`, carry a `release_date` earlier than the period it describes
    (a number cannot be published before the period it measures has ended), or default
    `revision_seq` to anything but the first print.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    series_id: SeriesId = Field(description="canonical dotted id, e.g. IN.NSE.NIFTY_50.PE")
    period_start: date | None = Field(
        default=None, description="first day the number describes; None for a point observation"
    )
    period_end: date = Field(description="last day the number describes")
    release_date: date = Field(description="the date it became knowable — the partition key")
    frequency: Frequency
    unit: Unit
    value: MacroValue
    revision_seq: int = Field(
        default=0, ge=0, description="0 is the first print; a same-day republication increments it"
    )
    source: str = Field(min_length=1, description="source register id the bytes came from")
    l0_key: str | None = Field(default=None, description="L0 object this was parsed from")

    @model_validator(mode="after")
    def _check_dates_and_id(self) -> MacroFact:
        if not SERIES_ID_PATTERN.match(self.series_id):
            raise ValueError(
                f"series_id {self.series_id!r} is not canonical "
                f"(pattern {SERIES_ID_PATTERN.pattern}); build it with macro.series_id()"
            )
        if self.period_start is not None and self.period_start > self.period_end:
            raise ValueError(
                f"period_start {self.period_start} is after period_end {self.period_end}"
            )
        if self.release_date < self.period_end:
            raise ValueError(
                f"{self.series_id}: release_date {self.release_date} precedes period_end "
                f"{self.period_end} — a figure cannot be knowable before the period it measures "
                "has ended; if the source really dates it that way, the parser read the "
                "wrong column"
            )
        return self


class MacroRelease(BaseModel):
    """Every fact one publication made knowable on one date — the unit the store writes.

    What it does: group the facts of a single release so they land in one `release_date` partition
    together, the way a `Filing` groups an issuer's facts for `pit_fundamentals`.
    What it assumes: every fact shares this release's `release_date` and `source`; the validator
    enforces it rather than trusting the caller, because a fact that drifted into the wrong
    partition is a silent PIT leak.
    What it never does: hold zero facts — an empty release is a parse that found nothing, which is a
    `ParseError` at the parser, not a release to store.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    release_date: date
    source: str = Field(min_length=1)
    facts: tuple[MacroFact, ...] = Field(min_length=1)
    l0_key: str | None = None

    @model_validator(mode="after")
    def _facts_agree(self) -> MacroRelease:
        for fact in self.facts:
            if fact.release_date != self.release_date:
                raise ValueError(
                    f"{fact.series_id} is dated {fact.release_date} but sits in the "
                    f"{self.release_date} release — a fact in the wrong partition is a PIT leak"
                )
            if fact.source != self.source:
                raise ValueError(
                    f"{fact.series_id} carries source {fact.source!r}, release is {self.source!r}"
                )
        return self
