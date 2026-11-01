"""The cross-filing guard against a half-year that states a single quarter's figures.

The parser takes a six-month column when the index asks for a quarter and the document offers
only the half-year ending that day (`parser._select_column`'s widening fallback), and it stores
the column under the document's own April→September span. What the parser cannot see is whether
the numbers under that label really are six months': nothing inside a single-column document says
otherwise. Measured over the 418 six-month filings in L1 on 2026-11-02, a handful state exactly
the revenue of one of the half's quarters as the same company filed it elsewhere, or less revenue
than the half's own first quarter — a quarter mislabelled as a half-year, as the triage of the
class-A Q4 documents found for the year-end half.

This module is the arithmetic that catches those, kept pure so the runner supplies what was
already published and a test supplies a hand-held list. A half-year is refused when, against the
quarters inside it that the same ISIN and nature had published by its filing date:

* its revenue from operations **equals** one quarter's (any version, either quarter) — the half
  of two quarters cannot be one of them unless the other had no revenue at all, and that case is
  worth refusing rather than storing a quarter as six months; or
* its revenue is **less than** the first quarter's in every version on file, both positive — a
  half-year includes its first quarter, and revenue from operations is not negative.

What it never does: refuse on a ratio or a tolerance (a seasonal business's second quarter can be
a sliver of its first), use a quarter filed after the half-year (invariant #7), compute a number,
or judge a filing that is not exactly two quarters long.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, timedelta
from typing import Final

from dataplatform.ingest.xbrl.models import Filing, FundamentalFact

__all__ = ["REVENUE", "is_half_year", "quarters_of", "single_quarter_reason"]

#: The concept the guard compares: the headline every taxonomy family maps.
REVENUE: Final = "revenue_from_operations"


def _add_months(day: date, months: int) -> date:
    total = day.month - 1 + months
    return day.replace(year=day.year + total // 12, month=total % 12 + 1)


def quarters_of(start: date, end: date) -> tuple[tuple[date, date], tuple[date, date]] | None:
    """The two quarters `(start, end)` consists of, or None when it is not exactly six months."""
    if start.day != 1 or _add_months(start, 6) - timedelta(days=1) != end:
        return None
    middle = _add_months(start, 3)
    return (start, middle - timedelta(days=1)), (middle, end)


def is_half_year(filing: Filing) -> bool:
    """Whether the filing covers exactly six months from the first of a month."""
    return filing.period_start is not None and (
        quarters_of(filing.period_start, filing.period_end) is not None
    )


def single_quarter_reason(filing: Filing, published: Iterable[FundamentalFact]) -> str | None:
    """Why this half-year is really one quarter's figures, or None if nothing says so.

    `published` is any set of stored facts; only the stated (not derived), company-level revenue
    of the same ISIN and nature, for one of the half's two quarters, filed on or before this
    filing's date, is looked at. Returns None for a filing that is not a half-year or states no
    non-zero revenue.
    """
    if filing.period_start is None:
        return None
    halves = quarters_of(filing.period_start, filing.period_end)
    if halves is None:
        return None
    revenue = next((fact.value for fact in filing.company_facts() if fact.concept == REVENUE), None)
    if revenue is None or revenue == 0:
        return None
    first, second = halves
    inside: dict[tuple[date, date], list[FundamentalFact]] = {first: [], second: []}
    for fact in published:
        period = (fact.period_start, fact.period_end)
        if (
            fact.isin == filing.isin
            and fact.nature is filing.nature
            and fact.concept == REVENUE
            and fact.segment is None
            and not fact.derived
            and fact.filing_date <= filing.filing_date
            and fact.filing_id != filing.filing_id
            and period in inside
        ):
            inside[period].append(fact)
    for period, facts in inside.items():
        for fact in facts:
            if fact.value == revenue:
                return (
                    f"the {filing.period_start.isoformat()}→{filing.period_end.isoformat()} "
                    f"half-year states revenue {revenue}, exactly the "
                    f"{period[0].isoformat()}→{period[1].isoformat()} quarter's as filed on "
                    f"{fact.filing_date.isoformat()} ({fact.filing_id}): one quarter's figures "
                    "under a half-year label, not stored"
                )
    firsts = [fact.value for fact in inside[first]]
    if revenue > 0 and firsts and all(value > revenue for value in firsts):
        smallest = min(firsts)
        return (
            f"the {filing.period_start.isoformat()}→{filing.period_end.isoformat()} half-year "
            f"states revenue {revenue}, less than its own first quarter's ({smallest} or more in "
            f"every version filed for {first[0].isoformat()}→{first[1].isoformat()}): a "
            "half-year includes that quarter, so these are one quarter's figures, not stored"
        )
    return None
