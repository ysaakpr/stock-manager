"""C.2's acceptance criteria for the NSE trading calendar, asserted offline.

Three things are under test. First, the mechanical criterion: over 2016-01-01..2026-08-07,
`expected_sessions` contains no weekend and no declared holiday, and nothing else is dropped.
Second, the spot checks that catch a plausible-looking but wrong calendar — Republic Day, Good
Friday, a mid-week holiday, and the Muhurat case where the exchange is closed yet a bhavcopy
exists. Third, that an uncovered year raises instead of quietly reporting no holidays.

The Muhurat expectations are not folklore: `ARCHIVE_PROBES` freezes what the NSE archive actually
returned for those dates when this calendar was curated (task C.2, 2026-08-08). Keeping the
evidence here is what stops a later "simplification" that treats Muhurat as an ordinary holiday
from passing — that change would silently drop eleven real trading days from the backfill.

Never touches the network (B8).
"""

from __future__ import annotations

import copy
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from dataplatform.ingest.calendar import (
    HOLIDAYS_PATH,
    CalendarCoverageError,
    CalendarDataError,
    DayKind,
    SpecialSession,
    TradingCalendar,
    expected_sessions,
    load,
    trading_calendar,
)

#: The span C.2's first acceptance criterion names.
SPAN_START = date(2016, 1, 1)
SPAN_END = date(2026, 8, 7)

#: What nsearchives actually served when C.2 curated this file, 2026-08-08. `True` means the
#: bhavcopy URL for that date returned 200 with a zip body; `False` means 404.
#: Muhurat dates (weekend ones included) publish data; full closures do not.
ARCHIVE_PROBES: dict[date, bool] = {
    # Muhurat sessions — declared holidays that nonetheless traded and published.
    date(2016, 10, 30): True,  # Sunday
    date(2017, 10, 19): True,
    date(2018, 11, 7): True,
    date(2019, 10, 27): True,  # Sunday
    date(2020, 11, 14): True,  # Saturday
    date(2021, 11, 4): True,
    date(2022, 10, 24): True,
    date(2023, 11, 12): True,  # Sunday
    date(2024, 11, 1): True,
    date(2025, 10, 21): True,
    # Full closures — no file at all.
    date(2024, 1, 26): False,  # Republic Day
    date(2023, 4, 7): False,  # Good Friday
    date(2024, 11, 15): False,  # Guru Nanak Jayanti
    # Ordinary sessions the NIFTY index feed is missing; the exchange traded on every one.
    date(2016, 1, 1): True,
    date(2016, 8, 12): True,
    date(2018, 1, 1): True,
    date(2019, 1, 1): True,
    date(2019, 2, 13): True,
    date(2019, 3, 29): True,
}


@pytest.fixture(scope="module")
def calendar() -> TradingCalendar:
    return trading_calendar()


@pytest.fixture(scope="module")
def raw() -> dict[str, Any]:
    with HOLIDAYS_PATH.open(encoding="utf-8") as fh:
        parsed: dict[str, Any] = yaml.safe_load(fh)
    return parsed


def _every_day(start: date, end: date) -> list[date]:
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


# ── acceptance 1: weekends and every listed holiday are excluded ─────────────────────────────


def test_expected_sessions_excludes_every_weekend(calendar: TradingCalendar) -> None:
    for day in calendar.expected_sessions(SPAN_START, SPAN_END):
        assert day.weekday() < 5, f"{day} is a {day:%A}"


def test_expected_sessions_excludes_every_listed_holiday(calendar: TradingCalendar) -> None:
    """Every entry in the file — Muhurat dates included, since those are holidays too."""
    listed = {holiday.date for holiday in calendar.holidays(SPAN_START, SPAN_END)}
    assert listed, "the holiday file covered none of the span"
    overlap = listed & set(calendar.expected_sessions(SPAN_START, SPAN_END))
    assert overlap == set(), f"holidays reported as sessions: {sorted(overlap)}"


def test_expected_sessions_drops_nothing_else(calendar: TradingCalendar) -> None:
    """The other half of the criterion: a weekday that is not a listed holiday *is* a session.

    Without this, a calendar that returned the empty list would pass the two tests above.
    """
    listed = {holiday.date for holiday in calendar.holidays(SPAN_START, SPAN_END)}
    want = [
        day for day in _every_day(SPAN_START, SPAN_END) if day.weekday() < 5 and day not in listed
    ]
    assert calendar.expected_sessions(SPAN_START, SPAN_END) == want


def test_session_counts_per_year_are_plausible(calendar: TradingCalendar) -> None:
    """A structurally broken file usually shows up as a year with far too many or too few days."""
    for year in range(2016, 2026):
        count = len(calendar.expected_sessions(date(year, 1, 1), date(year, 12, 31)))
        assert 240 <= count <= 252, f"{year} has {count} sessions"


def test_the_span_is_returned_sorted_and_unique(calendar: TradingCalendar) -> None:
    sessions = calendar.expected_sessions(SPAN_START, SPAN_END)
    assert sessions == sorted(sessions)
    assert len(sessions) == len(set(sessions))


def test_module_level_helper_matches_the_calendar(calendar: TradingCalendar) -> None:
    assert expected_sessions(SPAN_START, SPAN_END) == calendar.expected_sessions(
        SPAN_START, SPAN_END
    )


# ── acceptance 2: the spot checks ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "day",
    [date(2016, 1, 26), date(2021, 1, 26), date(2022, 1, 26), date(2024, 1, 26)],
)
def test_republic_day_is_a_holiday_when_it_falls_on_a_weekday(
    calendar: TradingCalendar, day: date
) -> None:
    assert calendar.classify(day) is DayKind.HOLIDAY
    holiday = calendar.holiday(day)
    assert holiday is not None and holiday.name == "Republic Day"


def test_republic_day_on_a_weekend_is_still_not_a_session(calendar: TradingCalendar) -> None:
    """26-Jan-2019 was a Saturday: no session either way, and no phantom holiday-day either."""
    assert date(2019, 1, 26).weekday() == 5
    assert calendar.classify(date(2019, 1, 26)) is DayKind.WEEKEND
    assert date(2019, 1, 26) not in calendar.expected_sessions(date(2019, 1, 1), date(2019, 12, 31))


@pytest.mark.parametrize(
    "day",
    [
        date(2016, 3, 25),
        date(2018, 3, 30),
        date(2019, 4, 19),
        date(2020, 4, 10),
        date(2021, 4, 2),
        date(2022, 4, 15),
        date(2023, 4, 7),
        date(2024, 3, 29),
        date(2025, 4, 18),
        date(2026, 4, 3),
    ],
)
def test_good_friday_is_a_holiday_every_year(calendar: TradingCalendar, day: date) -> None:
    """Good Friday moves; a hardcoded date or a fixed-offset rule fails here."""
    assert day.weekday() == 4, f"{day} is not a Friday"
    assert calendar.classify(day) is DayKind.HOLIDAY
    holiday = calendar.holiday(day)
    assert holiday is not None and "Good Friday" in holiday.name


@pytest.mark.parametrize(
    ("day", "name_fragment"),
    [
        (date(2024, 7, 17), "Muharram"),  # Wednesday
        (date(2023, 6, 29), "Bakri Id"),  # Thursday
        (date(2022, 8, 31), "Ganesh Chaturthi"),  # Wednesday
        (date(2019, 4, 29), "Parliamentary Elections"),  # Monday, an unscheduled closure
        (date(2024, 1, 22), "Pran Pratishtha"),  # Monday, a one-off special holiday
    ],
)
def test_midweek_holidays_break_the_week(
    calendar: TradingCalendar, day: date, name_fragment: str
) -> None:
    """A mid-week closure is the case a naive weekday calendar gets wrong."""
    assert 0 <= day.weekday() <= 4
    assert calendar.classify(day) is DayKind.HOLIDAY
    holiday = calendar.holiday(day)
    assert holiday is not None and name_fragment in holiday.name
    # The gap is exactly one day wide: each neighbouring *weekday* is an ordinary session.
    # (A Monday closure's previous day is a Sunday, which is a weekend, not a second lost day.)
    for neighbour in (day - timedelta(days=1), day + timedelta(days=1)):
        if neighbour.weekday() < 5:
            assert calendar.is_session(neighbour), neighbour


def test_muhurat_days_are_holidays_but_still_expect_data(calendar: TradingCalendar) -> None:
    """The Diwali case: closed to normal trading, yet the exchange publishes a bhavcopy.

    Treating these as ordinary holidays would drop eleven real trading days from the backfill;
    treating them as ordinary sessions would misreport the trading-day count.
    """
    muhurat = [
        holiday
        for holiday in calendar.holidays(calendar.coverage_start, calendar.coverage_end)
        if holiday.special_session is SpecialSession.MUHURAT
    ]
    assert len(muhurat) == 11, [h.date for h in muhurat]

    for holiday in muhurat:
        assert "Diwali" in holiday.name
        assert calendar.classify(holiday.date) is DayKind.MUHURAT
        assert not calendar.is_session(holiday.date), holiday.date
        assert calendar.expects_data(holiday.date), holiday.date


def test_muhurat_is_absent_from_sessions_and_present_in_data_dates(
    calendar: TradingCalendar,
) -> None:
    """2024-11-01 was a Friday: a holiday, not a session, but a bhavcopy exists for it."""
    diwali = date(2024, 11, 1)
    window = (date(2024, 10, 28), date(2024, 11, 8))
    assert diwali not in calendar.expected_sessions(*window)
    assert diwali in calendar.expected_data_dates(*window)


def test_weekend_muhurat_expects_data_despite_being_a_saturday(
    calendar: TradingCalendar,
) -> None:
    """14-Nov-2020 was a Saturday that traded — the case a weekday-first calendar cannot see."""
    saturday = date(2020, 11, 14)
    assert saturday.weekday() == 5
    assert calendar.classify(saturday) is DayKind.MUHURAT
    assert saturday in calendar.expected_data_dates(date(2020, 11, 9), date(2020, 11, 20))


def test_diwali_balipratipada_is_a_full_closure(calendar: TradingCalendar) -> None:
    """The day *after* Laxmi Pujan is an ordinary holiday — the two must not be conflated."""
    assert calendar.classify(date(2024, 11, 2)) is DayKind.WEEKEND  # Saturday in 2024
    assert calendar.classify(date(2022, 10, 26)) is DayKind.HOLIDAY
    assert not calendar.expects_data(date(2022, 10, 26))


def test_the_calendar_agrees_with_what_the_archive_served(calendar: TradingCalendar) -> None:
    """Acceptance, against frozen evidence: `expects_data` matches real bhavcopy availability."""
    for day, archive_had_it in ARCHIVE_PROBES.items():
        assert calendar.expects_data(day) is archive_had_it, (
            f"{day} ({day:%a}): calendar says expects_data="
            f"{calendar.expects_data(day)}, archive returned "
            f"{'200' if archive_had_it else '404'}"
        )


# ── acceptance 3: an uncovered year fails loud ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (date(2027, 1, 1), date(2027, 12, 31)),  # wholly past coverage
        (date(2026, 12, 1), date(2027, 1, 15)),  # straddling the end
        (date(2015, 12, 1), date(2016, 1, 31)),  # straddling the start
        (date(2010, 1, 1), date(2010, 12, 31)),  # wholly before coverage
    ],
)
def test_a_range_outside_coverage_raises(calendar: TradingCalendar, start: date, end: date) -> None:
    with pytest.raises(CalendarCoverageError):
        calendar.expected_sessions(start, end)


def test_an_uncovered_year_never_silently_reports_no_holidays(
    calendar: TradingCalendar,
) -> None:
    """The failure this criterion exists to prevent: 2027 resolving to 261 phantom sessions."""
    with pytest.raises(CalendarCoverageError, match="2027"):
        calendar.expected_sessions(date(2027, 1, 1), date(2027, 12, 31))
    with pytest.raises(CalendarCoverageError):
        calendar.classify(date(2027, 3, 15))
    with pytest.raises(CalendarCoverageError):
        calendar.is_session(date(2027, 3, 15))
    with pytest.raises(CalendarCoverageError):
        calendar.source_for(2027)


def test_the_coverage_boundaries_themselves_are_answerable(calendar: TradingCalendar) -> None:
    assert calendar.covers(calendar.coverage_start)
    assert calendar.covers(calendar.coverage_end)
    assert not calendar.covers(calendar.coverage_start - timedelta(days=1))
    assert not calendar.covers(calendar.coverage_end + timedelta(days=1))
    calendar.classify(calendar.coverage_start)
    calendar.classify(calendar.coverage_end)


def test_coverage_spans_ten_years_plus_the_current_one(calendar: TradingCalendar) -> None:
    """The spec's scope: last 10 years + current year, through the end of the current year."""
    assert calendar.coverage_start == date(2016, 1, 1)
    assert calendar.coverage_end == date(2026, 12, 31)
    for year in range(2016, 2027):
        assert calendar.source_for(year)


def test_an_inverted_range_is_rejected(calendar: TradingCalendar) -> None:
    with pytest.raises(ValueError, match="after"):
        calendar.expected_sessions(date(2024, 5, 1), date(2024, 4, 1))


# ── reconciliation: the backfill's validation hook ───────────────────────────────────────────


def test_reconcile_reports_nothing_when_the_calendar_matches(calendar: TradingCalendar) -> None:
    start, end = date(2024, 10, 25), date(2024, 11, 10)
    observed = calendar.expected_data_dates(start, end)
    result = calendar.reconcile(observed, start, end)
    assert result.ok
    assert result.missing == ()
    assert result.unexpected == ()


def test_reconcile_names_a_missing_day_as_a_failure(calendar: TradingCalendar) -> None:
    start, end = date(2024, 10, 25), date(2024, 11, 10)
    observed = [d for d in calendar.expected_data_dates(start, end) if d != date(2024, 10, 30)]
    result = calendar.reconcile(observed, start, end)
    assert result.missing == (date(2024, 10, 30),)
    assert not result.ok


def test_reconcile_flags_data_on_a_day_the_calendar_calls_closed(
    calendar: TradingCalendar,
) -> None:
    """The direction that means the *calendar* is wrong, not the fetcher."""
    start, end = date(2024, 1, 20), date(2024, 1, 31)
    observed = [*calendar.expected_data_dates(start, end), date(2024, 1, 26)]
    result = calendar.reconcile(observed, start, end)
    assert result.unexpected == (date(2024, 1, 26),)
    assert result.missing == ()


def test_reconcile_ignores_observations_outside_the_range(calendar: TradingCalendar) -> None:
    start, end = date(2024, 3, 1), date(2024, 3, 15)
    observed = [*calendar.expected_data_dates(start, end), date(2023, 5, 4), date(2025, 5, 6)]
    assert calendar.reconcile(observed, start, end).ok


def test_reconcile_counts_muhurat_as_expected(calendar: TradingCalendar) -> None:
    """A backfill that fetched Diwali is correct, not anomalous."""
    start, end = date(2022, 10, 20), date(2022, 10, 28)
    result = calendar.reconcile(calendar.expected_data_dates(start, end), start, end)
    assert date(2022, 10, 24) in result.expected
    assert result.ok


# ── the file itself ──────────────────────────────────────────────────────────────────────────


def test_every_covered_year_has_holidays(calendar: TradingCalendar, raw: dict[str, Any]) -> None:
    years = {entry["year"] for entry in raw["years"]}
    assert years == set(range(2016, 2027))
    for entry in raw["years"]:
        assert entry["holidays"], entry["year"]


def test_the_current_year_comes_from_the_exchange_itself(calendar: TradingCalendar) -> None:
    assert calendar.source_for(2026) == "nse_holiday_master"


def test_provenance_records_how_the_dates_were_established(calendar: TradingCalendar) -> None:
    provenance = calendar.provenance
    assert provenance.curated_by == "C.2"
    assert provenance.bhavcopy_probes > 0
    assert "holiday-master" in provenance.method
    assert provenance.known_limitation.strip()


def test_holiday_names_are_never_blank(calendar: TradingCalendar) -> None:
    for holiday in calendar.holidays(calendar.coverage_start, calendar.coverage_end):
        assert holiday.name.strip(), holiday.date


def test_day_kinds_partition_the_span(calendar: TradingCalendar) -> None:
    """Every date is exactly one kind, and the two data-expecting kinds are the two we think."""
    kinds = [kind for _, kind in calendar.days(SPAN_START, SPAN_END)]
    assert len(kinds) == (SPAN_END - SPAN_START).days + 1
    expecting = {kind for kind in DayKind if kind.expects_data}
    assert expecting == {DayKind.SESSION, DayKind.MUHURAT}
    assert len(calendar.expected_data_dates(SPAN_START, SPAN_END)) == sum(
        kind.expects_data for kind in kinds
    )


# ── the loader rejects a file that would make the calendar quietly wrong ─────────────────────


def _write(tmp_path: Path, doc: dict[str, Any]) -> Path:
    path = tmp_path / "holidays.yaml"
    with path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(doc, fh)
    return path


def test_loader_rejects_a_covered_year_with_no_entry(raw: dict[str, Any], tmp_path: Path) -> None:
    """The exact silent failure acceptance 3 is about, one level lower."""
    doc = copy.deepcopy(raw)
    doc["years"] = [entry for entry in doc["years"] if entry["year"] != 2020]
    with pytest.raises(CalendarDataError, match="2020"):
        load(_write(tmp_path, doc))


def test_loader_rejects_an_empty_holiday_list(raw: dict[str, Any], tmp_path: Path) -> None:
    doc = copy.deepcopy(raw)
    for entry in doc["years"]:
        if entry["year"] == 2021:
            entry["holidays"] = []
    with pytest.raises(CalendarDataError):
        load(_write(tmp_path, doc))


def test_loader_rejects_a_date_filed_under_the_wrong_year(
    raw: dict[str, Any], tmp_path: Path
) -> None:
    doc = copy.deepcopy(raw)
    for entry in doc["years"]:
        if entry["year"] == 2022:
            entry["holidays"][0]["date"] = date(2021, 6, 1)
    with pytest.raises(CalendarDataError, match="filed under"):
        load(_write(tmp_path, doc))


def test_loader_rejects_a_duplicate_date(raw: dict[str, Any], tmp_path: Path) -> None:
    doc = copy.deepcopy(raw)
    for entry in doc["years"]:
        if entry["year"] == 2023:
            entry["holidays"].append(copy.deepcopy(entry["holidays"][0]))
    with pytest.raises(CalendarDataError, match="twice"):
        load(_write(tmp_path, doc))


def test_loader_rejects_an_entry_outside_coverage(raw: dict[str, Any], tmp_path: Path) -> None:
    doc = copy.deepcopy(raw)
    doc["coverage"]["end"] = date(2025, 12, 31)
    with pytest.raises(CalendarDataError):
        load(_write(tmp_path, doc))


def test_loader_rejects_an_unknown_field(raw: dict[str, Any], tmp_path: Path) -> None:
    """extra='forbid': a typo'd key must not be silently ignored."""
    doc = copy.deepcopy(raw)
    doc["years"][0]["holidays"][0]["speciall_session"] = "MUHURAT"
    with pytest.raises(CalendarDataError, match="speciall_session"):
        load(_write(tmp_path, doc))


def test_loader_rejects_an_unknown_special_session(raw: dict[str, Any], tmp_path: Path) -> None:
    doc = copy.deepcopy(raw)
    doc["years"][0]["holidays"][0]["special_session"] = "HALF_DAY"
    with pytest.raises(CalendarDataError):
        load(_write(tmp_path, doc))


def test_loader_reports_a_missing_file_by_path(tmp_path: Path) -> None:
    with pytest.raises(CalendarDataError, match="cannot read"):
        load(tmp_path / "absent.yaml")
