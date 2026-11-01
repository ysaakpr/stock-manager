"""The one rule by which an ex-date the source left blank may be derived: T+1, ex = record.

NSE's book-closure file (``bc<ddmmyyyy>.csv``) states a corporate action's terms and its record
date, and usually its ex-date. Sometimes the EX_DT column is blank: AvenuesAI's 2026-10-13
consolidation (``CNSLDATNRE1 TO RS10``, record date 2026-10-13, EX_DT empty) is the case that
forced this module. Under rolling settlement the ex-date is fixed by the record date and the
settlement cycle: a buyer must have settled by the record date to be on the register, so the
ex-date is the record date minus the settlement lag. India moved every stock to T+1 settlement
in phases from 2022-02-25, completing on 2023-01-27; from that date T+1 means the last cum day
is the session before the record date and **the ex-date is the record date itself**. Every other
row of the same NSE bundles that states both dates shows exactly that.

The rule, and only the rule:

* derive only from a **stated** record date — never from a price, never from an announcement's
  prose;
* only when the record date is **on or after 2023-01-27** (`T1_SETTLEMENT_FROM`). Before it the
  cycle was T+2 (and varied by stock through the 2022 phase-in), so the lag cannot be read off a
  date; such a row is never derived here and is left to the continuity check's share-basis rule;
* only when the record date is a **trading session** (`is_session`). A record date on a holiday
  or weekend under T+1 still puts the ex-date on a session, but which one is a convention this
  rule does not assume.

Every derived ex-date carries the rule's name (`ExDateRule.T1_RECORD_DATE`) wherever it is
recorded, so it can never be mistaken for one the source stated.

Pure: no clock, no I/O, no network. The caller passes the session predicate; `nse_session` is the
production one (the checked-in NSE calendar, read once).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Final

__all__ = [
    "T1_SETTLEMENT_FROM",
    "DerivedExDate",
    "ExDateRule",
    "derive_ex_date",
    "nse_session",
]

#: The day every Indian equity settled T+1 (the last phase of the 2022-02-25 rollout). From here
#: the ex-date equals the record date; before it, never derive.
T1_SETTLEMENT_FROM: Final = date(2023, 1, 27)


class ExDateRule(StrEnum):
    """How a derived ex-date was obtained. A closed set: one rule today."""

    #: Record date, T+1 settlement: ex = record, for a record date that is a session on or after
    #: 2023-01-27.
    T1_RECORD_DATE = "T1_RECORD_DATE"


@dataclass(frozen=True, slots=True)
class DerivedExDate:
    """An ex-date the source did not state, and the rule that derived it."""

    ex_date: date
    rule: ExDateRule


def derive_ex_date(
    record_date: date | None, *, is_session: Callable[[date], bool]
) -> DerivedExDate | None:
    """The T+1 ex-date for a stated `record_date`, or `None` when the rule does not apply.

    What it does: returns ``record_date`` tagged `ExDateRule.T1_RECORD_DATE` when the record date
    is a trading session on or after `T1_SETTLEMENT_FROM`. What it assumes: the caller has
    checked that the source stated no ex-date (a stated one always wins) and that `record_date`
    was stated, not inferred. What it never does: derive before 2023-01-27, derive from a
    non-session record date, or guess when `record_date` is absent.
    """
    if record_date is None or record_date < T1_SETTLEMENT_FROM:
        return None
    if not is_session(record_date):
        return None
    return DerivedExDate(ex_date=record_date, rule=ExDateRule.T1_RECORD_DATE)


def nse_session(day: date) -> bool:
    """Whether NSE held a trading session on `day`, by the checked-in calendar (`ingest.calendar`).

    A date the calendar does not cover yet (a record date in a year whose holidays are not
    declared) answers False, so the rule declines to derive rather than guess a session.
    """
    from dataplatform.ingest.calendar import CalendarCoverageError, trading_calendar

    try:
        return trading_calendar().is_session(day)
    except CalendarCoverageError:
        return False
