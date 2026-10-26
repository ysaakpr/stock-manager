"""NOT_YET_DUE and sub-unit days in the gap report — and that neither can hide a miss.

The report owed a trading day the moment it began and judged every source by a whole-source row,
so today's announcements (landed at 00:30 tomorrow) and every news and TRI day (one row per
sub-unit, never a whole-source row) were NEVER_ATTEMPTED. The fixes, each pinned here both ways:

1. NOT_YET_DUE comes from the fire that covers *that* date, plus budget and grace — never the
   job's next fire. Once that time passes with no row, the day is NEVER_ATTEMPTED as before.
2. A unit-keyed day is complete only when every expected sub-unit is PUBLISHED; one missing
   sub-unit is its own unexplained entry, named.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Final

from dataplatform.clock import IST
from dataplatform.ingest.calendar import trading_calendar
from dataplatform.quality.gap_schedule import GRACE, RegistrySchedule
from dataplatform.quality.gaps import (
    ExpectedUnit,
    GapReason,
    GapReport,
    SourceExpectation,
    build_report,
)
from dataplatform.status.sync_state import SyncRecord, SyncState

DAY: Final = date(2026, 10, 23)  # Friday, a session
ANN: Final = "nse_announcements"
RSS: Final = "curated_rss"
AT: Final = datetime(2026, 10, 23, 1, 0, tzinfo=IST)


@dataclass
class StubSchedule:
    """A `DueSchedule` from fixed answers."""

    due: Mapping[tuple[str, date], datetime] = field(default_factory=dict)
    units: Mapping[tuple[str, date], tuple[ExpectedUnit, ...]] = field(default_factory=dict)

    def due_at(self, source: str, logical_date: date) -> datetime | None:
        return self.due.get((source, logical_date))

    def expected_units(self, source: str, logical_date: date) -> tuple[ExpectedUnit, ...] | None:
        return self.units.get((source, logical_date))


def _report(
    source: str,
    *,
    now: datetime,
    schedule: StubSchedule,
    records: Mapping[tuple[str, date], SyncRecord] | None = None,
    unit_records: tuple[SyncRecord, ...] = (),
) -> GapReport:
    return build_report(
        DAY,
        DAY,
        sources=[source],
        records={} if records is None else records,
        unit_records=unit_records,
        calendar=trading_calendar(),
        expectations={source: SourceExpectation(source=source, l1_dataset=None)},
        schedule=schedule,
        now=now,
    )


def _unit(source: str, unit: str, state: SyncState = SyncState.PUBLISHED) -> SyncRecord:
    return SyncRecord(
        source=f"{source}/{unit}",
        logical_date=DAY,
        state=state,
        attempts=1,
        retryable=True,
        last_error=None if state is SyncState.PUBLISHED else "FetchHTTPError: 503",
        updated_at=AT,
    )


# ── 1. NOT_YET_DUE: from the covering fire of that date, never the next fire ─────────────────

DUE: Final = datetime(2026, 10, 24, 1, 30, tzinfo=IST)  # D+1 00:30 fire + 45 min + grace


def test_a_day_before_its_covering_fire_is_not_yet_due() -> None:
    report = _report(ANN, now=DUE - timedelta(minutes=1), schedule=StubSchedule({(ANN, DAY): DUE}))
    (entry,) = report.entries
    assert entry.reason is GapReason.NOT_YET_DUE and entry.explained
    assert report.fully_explained


def test_a_day_whose_covering_fire_passed_with_no_row_stays_unexplained() -> None:
    """The guard against hiding a missed run: no row after the due instant is NEVER_ATTEMPTED."""
    for now in (DUE, DUE + timedelta(days=3)):
        report = _report(ANN, now=now, schedule=StubSchedule({(ANN, DAY): DUE}))
        (entry,) = report.entries
        assert entry.reason is GapReason.NEVER_ATTEMPTED and not entry.explained, now
        assert not report.fully_explained


def test_a_source_no_job_covers_is_owed_at_once() -> None:
    report = _report(ANN, now=DUE - timedelta(hours=20), schedule=StubSchedule())
    assert [entry.reason for entry in report.entries] == [GapReason.NEVER_ATTEMPTED]


def test_a_failed_row_is_never_excused_by_the_schedule() -> None:
    failed = SyncRecord(
        source=ANN, logical_date=DAY, state=SyncState.FAILED, attempts=1, retryable=True,
        last_error="503", updated_at=AT,
    )  # fmt: skip
    report = _report(
        ANN,
        now=DUE - timedelta(hours=1),
        schedule=StubSchedule({(ANN, DAY): DUE}),
        records={(ANN, DAY): failed},
    )
    assert [entry.reason for entry in report.unexplained] == [GapReason.FAILED]


def _slots() -> tuple[ExpectedUnit, ...]:
    return tuple(
        ExpectedUnit(f"rbi_press_releases-{hhmm}", datetime(2026, 10, 23, h, 45, tzinfo=IST))
        for hhmm, h in (("0015", 0), ("0615", 6), ("1215", 12), ("1815", 18))
    )


EVENING: Final = datetime(2026, 10, 23, 23, 0, tzinfo=IST)


# ── 2. sub-unit days: every expected sub-unit, or the day is not complete ────────────────────


def test_a_day_with_every_sub_unit_published_is_complete() -> None:
    units = tuple(_unit(RSS, u.unit) for u in _slots())
    report = _report(
        RSS, now=EVENING, schedule=StubSchedule(units={(RSS, DAY): _slots()}), unit_records=units
    )
    assert report.unexplained == () and report.complete == 5  # the day, and its four units


def test_a_day_with_one_sub_unit_missing_stays_unexplained_and_names_it() -> None:
    units = tuple(_unit(RSS, u.unit) for u in _slots()[:3])  # the 18:15 poll never ran
    report = _report(
        RSS, now=EVENING, schedule=StubSchedule(units={(RSS, DAY): _slots()}), unit_records=units
    )
    (entry,) = report.unexplained
    assert entry.reason is GapReason.NEVER_ATTEMPTED
    assert entry.unit == "rbi_press_releases-1815"
    assert not report.fully_explained


def test_any_one_row_present_is_not_enough() -> None:
    """The old shape of the bug's opposite: one landed poll must not stand for the day."""
    report = _report(
        RSS,
        now=EVENING,
        schedule=StubSchedule(units={(RSS, DAY): _slots()}),
        unit_records=(_unit(RSS, "rbi_press_releases-0015"),),
    )
    assert sorted(entry.unit for entry in report.unexplained) == [
        "rbi_press_releases-0615",
        "rbi_press_releases-1215",
        "rbi_press_releases-1815",
    ]


def test_a_failed_sub_unit_keeps_the_day_unexplained() -> None:
    units = (
        *[_unit(RSS, u.unit) for u in _slots()[:3]],
        _unit(RSS, _slots()[3].unit, SyncState.FAILED),
    )
    report = _report(
        RSS, now=EVENING, schedule=StubSchedule(units={(RSS, DAY): _slots()}), unit_records=units
    )
    (entry,) = report.unexplained
    assert entry.reason is GapReason.FAILED and entry.unit == "rbi_press_releases-1815"


def test_a_later_sub_unit_is_not_yet_due_until_its_own_fire() -> None:
    noon = datetime(2026, 10, 23, 12, 0, tzinfo=IST)
    units = tuple(_unit(RSS, u.unit) for u in _slots()[:2])
    report = _report(
        RSS, now=noon, schedule=StubSchedule(units={(RSS, DAY): _slots()}), unit_records=units
    )
    assert report.unexplained == ()
    assert sorted((e.unit, e.reason) for e in report.entries) == [
        ("rbi_press_releases-1215", GapReason.NOT_YET_DUE),
        ("rbi_press_releases-1815", GapReason.NOT_YET_DUE),
    ]


def test_without_a_schedule_nothing_changes() -> None:
    report = build_report(
        DAY, DAY, sources=[RSS], records={},
        unit_records=tuple(_unit(RSS, u.unit) for u in _slots()),
        calendar=trading_calendar(),
        expectations={RSS: SourceExpectation(source=RSS, l1_dataset=None)},
    )  # fmt: skip
    assert [entry.reason for entry in report.unexplained] == [GapReason.NEVER_ATTEMPTED]


# ── the production schedule, read off the registry ───────────────────────────────────────────

SCHEDULE: Final = RegistrySchedule(IST)


def test_announcements_are_due_after_the_next_mornings_fire() -> None:
    """`announcements_capture` lands D at 00:30 on D+1 (logical_date_lag_days=1)."""
    assert (
        SCHEDULE.due_at(ANN, DAY)
        == datetime(2026, 10, 24, 0, 30, tzinfo=IST) + timedelta(minutes=45) + GRACE
    )


def test_the_eod_sources_are_due_the_same_evening() -> None:
    assert (
        SCHEDULE.due_at("nse_bhavcopy", DAY)
        == datetime(2026, 10, 23, 18, 30, tzinfo=IST) + timedelta(minutes=45) + GRACE
    )


def test_the_covering_fire_is_fixed_by_the_date_not_by_now() -> None:
    """A week-old date is due a week ago, whatever the job's next fire is."""
    week_ago = DAY - timedelta(days=7)
    assert (
        SCHEDULE.due_at("nse_bhavcopy", week_ago)
        == datetime(2026, 10, 16, 18, 30, tzinfo=IST) + timedelta(minutes=45) + GRACE
    )


def test_the_configured_sub_units_are_listed_and_gdelt_is_not() -> None:
    rss = SCHEDULE.expected_units(RSS, DAY)
    assert rss is not None
    assert [u.unit for u in rss] == [
        "rbi_press_releases-0015",
        "rbi_press_releases-0615",
        "rbi_press_releases-1215",
        "rbi_press_releases-1815",
    ]
    tri = SCHEDULE.expected_units("nifty_tri_history", DAY)
    assert tri is not None
    assert sorted(u.unit for u in tri) == ["nifty50", "nifty500", "niftycpse", "niftyit"]
    assert {u.due_at for u in tri} == {
        datetime(2026, 10, 23, 19, 50, tzinfo=IST) + timedelta(minutes=10) + GRACE
    }
    assert SCHEDULE.expected_units("gdelt_v2_event_files", DAY) is None  # not listable
    assert SCHEDULE.expected_units(ANN, DAY) is None
