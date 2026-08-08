"""D1: the NSE trading calendar — which dates the exchange was open, and which owe us a file.

Every ingestion gap is one of two things: a day the exchange never traded, or a day we failed to
fetch. Without a calendar the two are indistinguishable and a missing bhavcopy gets a shrug, which
is exactly what the M1 gate's "gap report explains 100% of missing days" criterion forbids. This
module is that calendar: `expected_sessions` is weekdays minus the declared holidays in
`data/nse_holidays.yaml`, and everything downstream (M1.3's backfill, M1.9's daily job, M1.11's gap
report) decides what to ask for from it.

Two things here are easy to get wrong and are handled deliberately.

**Muhurat.** On Diwali Laxmi Pujan the exchange is shut for normal trading and appears in the NSE
holiday master as a holiday — but it holds a short ceremonial session and publishes a bhavcopy for
that date, including when it lands on a Sunday. So "is this a trading session" and "should a file
exist" are not the same question. `expected_sessions` answers the first and excludes Muhurat dates;
`expected_data_dates` answers the second and includes them. A fetcher that used the first would
silently skip eleven real trading days over this decade.

**Coverage.** Asking for sessions in a year the holiday file does not cover is an error, never an
empty holiday list. Silently assuming a future year has no holidays invents about fifteen sessions
that never happened, and every one of them becomes a phantom gap for a human to chase.

Pure date arithmetic: no clock (the caller supplies the range — B10), no network, no I/O beyond
reading the checked-in YAML once.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Final

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

HOLIDAYS_PATH: Final[Path] = Path(__file__).parent / "data" / "nse_holidays.yaml"

#: Monday-Friday. The exchange's normal week; Muhurat is the documented exception.
_WEEKEND_START: Final[int] = 5

__all__ = [
    "HOLIDAYS_PATH",
    "CalendarCoverageError",
    "CalendarDataError",
    "CalendarError",
    "DayKind",
    "Holiday",
    "Reconciliation",
    "SpecialSession",
    "TradingCalendar",
    "expected_data_dates",
    "expected_sessions",
    "load",
    "trading_calendar",
]


class CalendarError(Exception):
    """Base for every calendar failure, so callers can catch the family."""


class CalendarDataError(CalendarError):
    """The holiday file is malformed, internally inconsistent, or has a hole in a covered year."""


class CalendarCoverageError(CalendarError):
    """A date or range was requested that the holiday file does not cover.

    Raised rather than answered, because the honest answer is "unknown" and the convenient one
    ("no holidays that year") is wrong in a way nothing downstream can detect.
    """


class SpecialSession(StrEnum):
    """A session the exchange holds on a day it is otherwise closed."""

    MUHURAT = "MUHURAT"
    """Diwali Laxmi Pujan ceremonial session. Short, real, and it publishes a bhavcopy."""


class DayKind(StrEnum):
    """What one calendar date is, for gap-reporting purposes.

    Exactly one applies to any date, so counts over a range partition it: `SESSION + MUHURAT`
    is the set of dates that owe us data, `WEEKEND + HOLIDAY` the set that does not, and
    `HOLIDAY` alone is the number of trading days the exchange actually gave up that year.
    """

    SESSION = "SESSION"
    """A normal full trading day. Data expected."""

    MUHURAT = "MUHURAT"
    """A ceremonial session on a declared holiday. Data expected."""

    WEEKEND = "WEEKEND"
    """Saturday or Sunday, with no special session. No data."""

    HOLIDAY = "HOLIDAY"
    """A weekday the exchange declared closed. No data."""

    @property
    def expects_data(self) -> bool:
        """Whether a bhavcopy should exist for a day of this kind."""
        return self in (DayKind.SESSION, DayKind.MUHURAT)


# ── the checked-in file ──────────────────────────────────────────────────────────────────────


class Holiday(BaseModel):
    """One declared non-trading day, and whether the exchange nonetheless held a session."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    date: date
    name: str
    special_session: SpecialSession | None = None

    @property
    def has_session(self) -> bool:
        """True when the exchange traded anyway, so a file exists despite the holiday."""
        return self.special_session is not None


class Coverage(BaseModel):
    """The inclusive span the file makes a claim about. Outside it, the calendar refuses."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    start: date
    end: date


class Provenance(BaseModel):
    """Where the dates came from, so a future maintainer can judge how far to trust them."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    curated_by: str
    curated_on: date
    method: str
    bhavcopy_probes: int
    known_limitation: str


class HolidayYear(BaseModel):
    """One year's declared holidays, tagged with how that year was established."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    year: int
    source: str
    holidays: list[Holiday] = Field(min_length=1)


class HolidayFile(BaseModel):
    """The whole `nse_holidays.yaml`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int
    coverage: Coverage
    provenance: Provenance
    years: list[HolidayYear] = Field(min_length=1)


# ── the calendar ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Reconciliation:
    """The result of checking the calendar against what the exchange actually published.

    `missing` is the gap report's real content — dates we expected data for and do not have, each
    one a fetch to retry or a failure to explain. `unexpected` is the calendar accusing itself:
    data exists on a date this file calls closed, so the file is wrong and must be corrected
    before it hides a genuine session again.
    """

    start: date
    end: date
    expected: tuple[date, ...]
    observed: tuple[date, ...]
    missing: tuple[date, ...]
    unexpected: tuple[date, ...]

    @property
    def ok(self) -> bool:
        """True when the calendar explains every observed and every absent date in the range."""
        return not self.missing and not self.unexpected

    def summary(self) -> str:
        """One line for a log or a status payload."""
        return (
            f"{self.start}..{self.end}: {len(self.expected)} expected, "
            f"{len(self.observed)} observed, {len(self.missing)} missing, "
            f"{len(self.unexpected)} unexpected"
        )


@dataclass(frozen=True, slots=True)
class TradingCalendar:
    """Expected NSE trading sessions over a bounded span of dates.

    What it does: classifies any date in its coverage as a session, a Muhurat session, a weekend
    or a declared holiday, and enumerates each of those over a range.
    What it assumes: the holiday file is complete for *weekdays* across its coverage — that is the
    property `reconcile` exists to keep true against real bhavcopy availability.
    What it never does: guess. A date outside coverage raises; it is never treated as a session.
    """

    coverage_start: date
    coverage_end: date
    provenance: Provenance
    _holidays: dict[date, Holiday]
    _sources: dict[int, str]

    # ── coverage ─────────────────────────────────────────────────────────────────────────────

    def covers(self, day: date) -> bool:
        """Whether this calendar makes any claim about `day`."""
        return self.coverage_start <= day <= self.coverage_end

    def _require_coverage(self, start: date, end: date) -> None:
        """Reject a range that leaves the covered span, naming the fix.

        The whole point of the C.2 acceptance criterion: an uncovered year must fail loudly
        instead of silently resolving to "no holidays", which would fabricate sessions.
        """
        if start > end:
            raise ValueError(f"start {start} is after end {end}")
        if self.covers(start) and self.covers(end):
            return
        raise CalendarCoverageError(
            f"{start}..{end} leaves the holiday calendar's coverage "
            f"({self.coverage_start}..{self.coverage_end}); "
            f"add the missing year(s) to {HOLIDAYS_PATH.name} rather than assuming no holidays"
        )

    # ── classification ───────────────────────────────────────────────────────────────────────

    def holiday(self, day: date) -> Holiday | None:
        """The declared holiday on `day`, or None. Raises outside coverage."""
        self._require_coverage(day, day)
        return self._holidays.get(day)

    def classify(self, day: date) -> DayKind:
        """What `day` is. Raises `CalendarCoverageError` outside the covered span.

        Order matters: a special session outranks everything (the exchange traded, so a file
        exists), then the weekend, then a declared closure. That ordering is what makes
        `HOLIDAY` mean "a weekday the exchange gave up" rather than "a date on the holiday list",
        which is the count anyone actually wants.
        """
        self._require_coverage(day, day)
        declared = self._holidays.get(day)
        if declared is not None and declared.has_session:
            return DayKind.MUHURAT
        if day.weekday() >= _WEEKEND_START:
            return DayKind.WEEKEND
        if declared is not None:
            return DayKind.HOLIDAY
        return DayKind.SESSION

    def is_session(self, day: date) -> bool:
        """Whether `day` is a normal full trading session. Muhurat is not one."""
        return self.classify(day) is DayKind.SESSION

    def expects_data(self, day: date) -> bool:
        """Whether a bhavcopy should exist for `day` — sessions and Muhurat alike."""
        return self.classify(day).expects_data

    # ── ranges ───────────────────────────────────────────────────────────────────────────────

    def days(self, start: date, end: date) -> Iterator[tuple[date, DayKind]]:
        """Every date in the inclusive range with its kind, ascending."""
        self._require_coverage(start, end)
        day = start
        while day <= end:
            yield day, self.classify(day)
            day += timedelta(days=1)

    def expected_sessions(self, start: date, end: date) -> list[date]:
        """Normal trading sessions in the inclusive range: weekdays minus declared holidays.

        Excludes weekends, every declared holiday, and Muhurat dates — a Muhurat date is a
        declared holiday, so it is not a session even though data exists for it. Use
        `expected_data_dates` when the question is "should a file exist".
        """
        return [day for day, kind in self.days(start, end) if kind is DayKind.SESSION]

    def expected_data_dates(self, start: date, end: date) -> list[date]:
        """Every date in the range the exchange should have published a bhavcopy for.

        Sessions plus Muhurat sessions. This is what a fetcher iterates and what the gap report
        measures against; anything absent from it is not a gap, it is a closed exchange.
        """
        return [day for day, kind in self.days(start, end) if kind.expects_data]

    def holidays(self, start: date, end: date) -> list[Holiday]:
        """Declared holidays in the inclusive range, ascending, Muhurat dates included."""
        self._require_coverage(start, end)
        return [self._holidays[day] for day in sorted(self._holidays) if start <= day <= end]

    def source_for(self, year: int) -> str:
        """How `year`'s holidays were established (see the file's provenance block)."""
        try:
            return self._sources[year]
        except KeyError:
            raise CalendarCoverageError(
                f"no holiday record for {year}; coverage is "
                f"{self.coverage_start.year}..{self.coverage_end.year}"
            ) from None

    # ── validation against reality ───────────────────────────────────────────────────────────

    def reconcile(self, observed: Iterable[date], start: date, end: date) -> Reconciliation:
        """Compare this calendar against the dates the exchange actually published data for.

        This is the "validated against actual bhavcopy availability during backfill" half of the
        C.2 spec, and M1.11's gap report in one call. `observed` is whatever L0 holds for the
        range. Two failure directions, and they mean opposite things:

          * `missing` — expected but absent. A fetch to retry, or a genuine source failure.
          * `unexpected` — present but the calendar calls the date closed. The calendar is wrong;
            correcting it is urgent, because the same entry is suppressing a real session.

        Observed dates outside the range are ignored rather than counted against the calendar.
        """
        self._require_coverage(start, end)
        in_range = {day for day in observed if start <= day <= end}
        expected = set(self.expected_data_dates(start, end))
        return Reconciliation(
            start=start,
            end=end,
            expected=tuple(sorted(expected)),
            observed=tuple(sorted(in_range)),
            missing=tuple(sorted(expected - in_range)),
            unexpected=tuple(sorted(in_range - expected)),
        )


# ── loading ──────────────────────────────────────────────────────────────────────────────────


def _check(parsed: HolidayFile) -> None:
    """Reject a holiday file that would make the calendar quietly wrong.

    Each of these is a way the file can look fine and still lie: a year listed but empty (pydantic
    catches that one), a year inside coverage with no entry at all, a date filed under the wrong
    year, a duplicate, or an entry outside the span the file claims to cover.
    """
    if parsed.coverage.start > parsed.coverage.end:
        raise CalendarDataError(
            f"coverage {parsed.coverage.start}..{parsed.coverage.end} is inverted"
        )

    by_year = {entry.year: entry for entry in parsed.years}
    if len(by_year) != len(parsed.years):
        raise CalendarDataError("a year appears twice in `years:`")

    covered = range(parsed.coverage.start.year, parsed.coverage.end.year + 1)
    missing_years = [year for year in covered if year not in by_year]
    if missing_years:
        raise CalendarDataError(
            f"coverage claims {parsed.coverage.start}..{parsed.coverage.end} but "
            f"{missing_years} have no holiday list; either add them or shrink coverage — "
            f"a covered year with no holidays would silently become ~250 phantom sessions"
        )

    seen: set[date] = set()
    for entry in parsed.years:
        for holiday in entry.holidays:
            if holiday.date.year != entry.year:
                raise CalendarDataError(
                    f"{holiday.date} ({holiday.name!r}) is filed under year {entry.year}"
                )
            if holiday.date in seen:
                raise CalendarDataError(f"{holiday.date} appears twice")
            seen.add(holiday.date)
            if not (parsed.coverage.start <= holiday.date <= parsed.coverage.end):
                raise CalendarDataError(
                    f"{holiday.date} ({holiday.name!r}) lies outside coverage "
                    f"{parsed.coverage.start}..{parsed.coverage.end}"
                )


def load(path: Path = HOLIDAYS_PATH) -> TradingCalendar:
    """Read and validate the holiday file, returning the calendar it describes.

    Assumes the file is checked in and trusted. Raises `CalendarDataError` on anything malformed
    or internally inconsistent — there is no partial-credit mode, because a calendar that is 95%
    right produces gap reports nobody can act on.
    """
    try:
        with path.open(encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
    except OSError as exc:
        raise CalendarDataError(f"cannot read the holiday calendar at {path}: {exc}") from exc

    try:
        parsed = HolidayFile.model_validate(raw)
    except ValidationError as exc:
        raise CalendarDataError(f"{path} does not match the holiday schema: {exc}") from exc

    _check(parsed)
    return TradingCalendar(
        coverage_start=parsed.coverage.start,
        coverage_end=parsed.coverage.end,
        provenance=parsed.provenance,
        _holidays={h.date: h for entry in parsed.years for h in entry.holidays},
        _sources={entry.year: entry.source for entry in parsed.years},
    )


@lru_cache(maxsize=1)
def trading_calendar() -> TradingCalendar:
    """The checked-in NSE calendar, parsed once per process."""
    return load()


def expected_sessions(start: date, end: date) -> list[date]:
    """Normal NSE trading sessions in the inclusive range, from the checked-in calendar."""
    return trading_calendar().expected_sessions(start, end)


def expected_data_dates(start: date, end: date) -> list[date]:
    """Dates in the inclusive range the NSE should have published a bhavcopy for."""
    return trading_calendar().expected_data_dates(start, end)


# ── CLI ──────────────────────────────────────────────────────────────────────────────────────


def _validate(calendar: TradingCalendar) -> int:
    span_days = (calendar.coverage_end - calendar.coverage_start).days + 1
    sessions = calendar.expected_sessions(calendar.coverage_start, calendar.coverage_end)
    data_dates = calendar.expected_data_dates(calendar.coverage_start, calendar.coverage_end)
    holidays = calendar.holidays(calendar.coverage_start, calendar.coverage_end)
    muhurat = [h for h in holidays if h.has_session]
    print(
        f"calendar OK — {calendar.coverage_start}..{calendar.coverage_end} "
        f"({span_days} days): {len(sessions)} sessions, {len(muhurat)} Muhurat sessions, "
        f"{len(data_dates)} dates expecting a bhavcopy, {len(holidays)} declared holidays"
    )
    for year in range(calendar.coverage_start.year, calendar.coverage_end.year + 1):
        first = max(date(year, 1, 1), calendar.coverage_start)
        last = min(date(year, 12, 31), calendar.coverage_end)
        closed = [day for day, kind in calendar.days(first, last) if kind is DayKind.HOLIDAY]
        print(
            f"  {year}: {len(calendar.expected_sessions(first, last)):>3} sessions, "
            f"{len(closed):>2} weekday closures  [{calendar.source_for(year)}]"
        )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. `validate` is C.2's operator-facing check on the checked-in file."""
    parser = argparse.ArgumentParser(prog="calendar", description=__doc__)
    parser.add_argument("command", choices=["validate", "sessions"])
    parser.add_argument("--from", dest="start", type=date.fromisoformat)
    parser.add_argument("--to", dest="end", type=date.fromisoformat)
    parser.add_argument("--path", type=Path, default=HOLIDAYS_PATH)
    args = parser.parse_args(argv)

    try:
        calendar = load(args.path)
    except CalendarDataError as exc:
        print(f"holiday calendar does not load: {exc}", file=sys.stderr)
        return 2

    if args.command == "validate":
        return _validate(calendar)

    start = args.start or calendar.coverage_start
    end = args.end or calendar.coverage_end
    try:
        for day, kind in calendar.days(start, end):
            if kind.expects_data:
                print(f"{day.isoformat()}\t{kind.value}")
    except CalendarCoverageError as exc:
        print(f"{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
