"""M1.11: the gap report's rules, offline — no database, no network, no real lake.

The M1 gate's "gap report explains 100% of missing days" is a claim nobody can check by hand over
ten years, so it has to be a property of the classifier. That is what this module tests:

* **acceptance 1** — over a sampled backfill range, *every* missing date comes out with a reason.
  Asserted exhaustively rather than by example: the test walks a real multi-week range against the
  real C.2 calendar and demands that the union of complete, explained and unexplained accounts for
  every `(source, date)` pair in it, with no pair falling through unclassified.
* **acceptance 2** — the unexplained set is exactly the pairs that owe an answer, and it is empty
  when the range is complete. Both directions, because an "unexplained" list that is empty for the
  wrong reason is the failure mode the M1 gate exists to prevent.
* **acceptance 3** — a deliberately deleted L1 partition shows up as unexplained. Written against
  a real directory tree under `tmp_path`: the partition is created, the pair reports complete, the
  partition is deleted, and the same pair must flip to `L1_PARTITION_MISSING`.

`build_report` is pure, so everything here is fact-in / fact-out. The live `GapScanner` and the
`/status/gaps` route are exercised against Postgres in `tests/integration/test_status_api.py`.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from dataplatform.clock import IST
from dataplatform.ingest.calendar import CalendarCoverageError, DayKind, trading_calendar
from dataplatform.quality.gaps import (
    PER_SESSION_CADENCES,
    GapEntry,
    GapReason,
    GapReport,
    GapReportError,
    L1Check,
    L1Result,
    LakeL1Presence,
    SourceExpectation,
    build_report,
    classify_pair,
    expectations_from_register,
)
from dataplatform.status.sync_state import SyncRecord, SyncState
from dataplatform.store.paths import l1_partition_dir

SOURCE = "nse_bhavcopy_udiff"
LEGACY = "nse_bhavcopy_legacy"

AT = datetime(2026, 8, 7, 18, 30, tzinfo=IST)

SESSION = date(2026, 8, 7)  # Friday
SATURDAY = date(2026, 8, 8)
SUNDAY = date(2026, 8, 9)
MONDAY = date(2026, 8, 10)
HOLIDAY = date(2026, 1, 26)  # Republic Day, a Monday
MUHURAT = date(2025, 10, 21)  # Diwali Laxmi Pujan — closed, but a bhavcopy exists

#: The sampled backfill range acceptance 1 is measured over: three weeks spanning a weekend run,
#: a mid-week holiday (15 Aug 2026 falls on a Saturday, so 2026-01 is used for the holiday cases)
#: and a full working week.
SAMPLE_START = date(2026, 7, 20)
SAMPLE_END = date(2026, 8, 7)


def expectation(
    source: str = SOURCE,
    *,
    per_session: bool = True,
    era_start: date | None = None,
    era_end: date | None = None,
    l1_dataset: str | None = None,
) -> SourceExpectation:
    """An expectation with everything but the interesting field defaulted."""
    return SourceExpectation(
        source=source,
        per_session=per_session,
        era_start=era_start,
        era_end=era_end,
        l1_dataset=l1_dataset,
    )


def record(
    state: SyncState,
    *,
    source: str = SOURCE,
    logical_date: date = SESSION,
    attempts: int = 1,
    retryable: bool = True,
    last_error: str | None = None,
) -> SyncRecord:
    """A `sync_state` row parked in `state`."""
    return SyncRecord(
        source=source,
        logical_date=logical_date,
        state=state,
        updated_at=AT,
        attempts=attempts,
        retryable=retryable,
        last_error=last_error,
        first_attempt_at=AT,
    )


def rows(*records: SyncRecord) -> dict[tuple[str, date], SyncRecord]:
    """Records keyed the way `build_report` wants them."""
    return {(row.source, row.logical_date): row for row in records}


def report_over(
    start: date,
    end: date,
    *,
    sources: tuple[str, ...] = (SOURCE,),
    records: dict[tuple[str, date], SyncRecord] | None = None,
    expectations: dict[str, SourceExpectation] | None = None,
    l1_presence: LakeL1Presence | None = None,
) -> GapReport:
    """A report over the real C.2 calendar, with only the facts a test cares about supplied."""
    return build_report(
        start,
        end,
        sources=sources,
        records={} if records is None else records,
        calendar=trading_calendar(),
        expectations={source: expectation(source) for source in sources}
        if expectations is None
        else expectations,
        l1_presence=l1_presence,
    )


def published_every_session(
    start: date, end: date, source: str = SOURCE
) -> dict[tuple[str, date], SyncRecord]:
    """A perfectly backfilled range: PUBLISHED on every date that owed data, GAP on the rest."""
    calendar = trading_calendar()
    return rows(
        *(
            record(
                SyncState.PUBLISHED if kind.expects_data else SyncState.GAP,
                source=source,
                logical_date=day,
            )
            for day, kind in calendar.days(start, end)
        )
    )


# ── acceptance 1: every missing date is classified with a reason ─────────────────────────────


def test_every_pair_in_the_sampled_range_is_accounted_for() -> None:
    """Complete + explained + unexplained must partition the range. Nothing may fall through.

    This is the acceptance criterion as an identity rather than as a spot check: a classifier with
    a hole in it silently drops the dates it cannot describe, and a dropped date looks exactly
    like a clean one in any count-based assertion.
    """
    report = report_over(
        SAMPLE_START,
        SAMPLE_END,
        records=rows(
            record(SyncState.PUBLISHED, logical_date=date(2026, 7, 20)),
            record(SyncState.FAILED, logical_date=date(2026, 7, 21), last_error="HTTP 500"),
            record(SyncState.FETCHED, logical_date=date(2026, 7, 22)),
        ),
    )

    days = (SAMPLE_END - SAMPLE_START).days + 1
    assert report.pairs_examined == days
    assert report.complete + len(report.explained) + len(report.unexplained) == days
    assert all(entry.reason is not None for entry in report.entries)
    assert all(entry.detail.strip() for entry in report.entries), "every entry needs a reason line"


def test_every_missing_date_carries_a_reason_and_a_day_kind() -> None:
    """No entry may be produced without naming both what the day was and why data is absent."""
    report = report_over(SAMPLE_START, SAMPLE_END)

    assert report.entries, "an unfetched range must produce entries, not silence"
    for entry in report.entries:
        assert isinstance(entry.reason, GapReason)
        assert isinstance(entry.day_kind, DayKind)
        assert entry.explained is entry.reason.explained


def test_weekends_and_holidays_are_explained_and_sessions_are_not() -> None:
    """The C.2 split, verbatim: a shut exchange explains itself, a trading day does not."""
    report = report_over(SESSION, MONDAY)

    by_date = {entry.logical_date: entry for entry in report.entries}
    assert by_date[SATURDAY].reason is GapReason.WEEKEND
    assert by_date[SUNDAY].reason is GapReason.WEEKEND
    assert by_date[SESSION].reason is GapReason.NEVER_ATTEMPTED
    assert by_date[MONDAY].reason is GapReason.NEVER_ATTEMPTED
    assert by_date[SATURDAY].explained and by_date[SUNDAY].explained
    assert not by_date[SESSION].explained


def test_a_declared_holiday_is_explained() -> None:
    entry = classify_pair(expectation(), HOLIDAY, DayKind.HOLIDAY, None)
    assert entry is not None
    assert entry.reason is GapReason.HOLIDAY
    assert entry.explained


def test_a_muhurat_session_owes_a_file_even_though_it_is_a_holiday() -> None:
    """C.2's subtle case: the exchange is shut, publishes anyway, so an absence is a real miss."""
    report = report_over(MUHURAT, MUHURAT)

    assert [entry.reason for entry in report.entries] == [GapReason.NEVER_ATTEMPTED]
    assert report.entries[0].day_kind is DayKind.MUHURAT
    assert not report.fully_explained


def test_a_session_outside_the_sources_era_is_explained_not_missing() -> None:
    """The legacy bhavcopy stopped serving after the UDiFF cutover; it owes nothing after it."""
    entry = classify_pair(
        expectation(LEGACY, era_end=date(2024, 7, 8)), SESSION, DayKind.SESSION, None
    )

    assert entry is not None
    assert entry.reason is GapReason.OUTSIDE_SOURCE_ERA
    assert entry.explained
    assert "2024-07-08" in entry.detail


def test_a_source_with_no_per_session_cadence_produces_no_missing_days() -> None:
    """A quarterly source measured against the trading calendar would invent a gap every session."""
    assert classify_pair(expectation(per_session=False), SESSION, DayKind.SESSION, None) is None
    assert classify_pair(expectation(per_session=False), SATURDAY, DayKind.WEEKEND, None) is None


def test_a_broken_row_is_reported_even_for_a_source_that_owes_no_session_files() -> None:
    """A recorded failure is unexplained whatever the cadence — a row exists and it is not done."""
    entry = classify_pair(
        expectation(per_session=False),
        SESSION,
        DayKind.SESSION,
        record(SyncState.FAILED, last_error="HTTP 500"),
    )

    assert entry is not None
    assert entry.reason is GapReason.FAILED


def test_a_range_outside_the_calendar_coverage_raises_rather_than_guessing() -> None:
    """A year the holiday file does not cover would otherwise become ~250 phantom missing days."""
    with pytest.raises(CalendarCoverageError):
        report_over(date(2011, 6, 1), date(2011, 6, 30))


def test_an_inverted_range_is_refused() -> None:
    with pytest.raises(GapReportError, match="runs forwards"):
        report_over(MONDAY, SESSION)


# ── the unexplained reasons, one per failure mode ────────────────────────────────────────────


def test_a_never_attempted_session_is_unexplained_and_has_no_history() -> None:
    """The entry only D7 can produce: there is no row, so no query over sync_state could see it."""
    entry = classify_pair(expectation(), SESSION, DayKind.SESSION, None)

    assert entry is not None
    assert entry.reason is GapReason.NEVER_ATTEMPTED
    assert not entry.explained
    assert (entry.state, entry.attempts, entry.updated_at) == (None, 0, None)


def test_a_failed_row_carries_its_whole_sync_state_history() -> None:
    """The spec's phrase in full: every unexplained pair is listed *with its sync_state history*."""
    entry = classify_pair(
        expectation(),
        SESSION,
        DayKind.SESSION,
        record(SyncState.FAILED, attempts=3, retryable=False, last_error="HTTP 403"),
    )

    assert entry is not None
    assert entry.reason is GapReason.FAILED
    assert (entry.state, entry.attempts, entry.retryable) == (SyncState.FAILED, 3, False)
    assert entry.last_error == "HTTP 403"
    assert entry.first_attempt_at == AT and entry.updated_at == AT
    assert "HTTP 403" in entry.detail


@pytest.mark.parametrize(
    "state",
    [SyncState.PENDING, SyncState.FETCHED, SyncState.VALIDATED, SyncState.NORMALIZED],
)
def test_a_row_stuck_mid_pipeline_is_unexplained(state: SyncState) -> None:
    """Started and never finished is a missing day, not a day in progress forever."""
    entry = classify_pair(expectation(), SESSION, DayKind.SESSION, record(state))

    assert entry is not None
    assert entry.reason is GapReason.IN_PROGRESS
    assert not entry.explained
    assert state.value in entry.detail


def test_a_gap_filed_on_a_trading_day_is_unexplained() -> None:
    """A real miss dressed as a holiday is the exact lie the M1 gate exists to catch."""
    entry = classify_pair(expectation(), SESSION, DayKind.SESSION, record(SyncState.GAP))

    assert entry is not None
    assert entry.reason is GapReason.GAP_ON_A_TRADING_DAY
    assert not entry.explained
    assert entry.state is SyncState.GAP


def test_a_gap_filed_on_a_closed_day_is_explained() -> None:
    entry = classify_pair(
        expectation(), SATURDAY, DayKind.WEEKEND, record(SyncState.GAP, logical_date=SATURDAY)
    )

    assert entry is not None
    assert entry.reason is GapReason.WEEKEND
    assert entry.explained


def test_a_published_row_with_no_l1_check_is_complete() -> None:
    assert (
        classify_pair(expectation(), SESSION, DayKind.SESSION, record(SyncState.PUBLISHED)) is None
    )


# ── acceptance 2: /status/gaps' payload — the unexplained set, empty when complete ────────────


def test_a_fully_backfilled_range_has_nothing_unexplained() -> None:
    """Every session PUBLISHED and every closed day GAP: the M1 gate's pass condition."""
    report = report_over(
        SAMPLE_START, SAMPLE_END, records=published_every_session(SAMPLE_START, SAMPLE_END)
    )

    assert report.unexplained == ()
    assert report.fully_explained
    assert report.complete == len(trading_calendar().expected_data_dates(SAMPLE_START, SAMPLE_END))


def test_one_missing_session_breaks_the_gate_and_names_itself() -> None:
    """The inverse of the test above — if this passed too, `fully_explained` would be a constant."""
    complete = published_every_session(SAMPLE_START, SAMPLE_END)
    del complete[(SOURCE, date(2026, 7, 30))]

    report = report_over(SAMPLE_START, SAMPLE_END, records=complete)

    assert not report.fully_explained
    assert [(entry.source, entry.logical_date, entry.reason) for entry in report.unexplained] == [
        (SOURCE, date(2026, 7, 30), GapReason.NEVER_ATTEMPTED)
    ]


def test_the_report_enumerates_every_unexplained_pair_rather_than_counting_them() -> None:
    """The spec is explicit: enumerate, do not summarise."""
    complete = published_every_session(SAMPLE_START, SAMPLE_END)
    for day in (date(2026, 7, 22), date(2026, 7, 23), date(2026, 8, 5)):
        del complete[(SOURCE, day)]

    report = report_over(SAMPLE_START, SAMPLE_END, records=complete)

    assert [entry.logical_date for entry in report.unexplained] == [
        date(2026, 7, 22),
        date(2026, 7, 23),
        date(2026, 8, 5),
    ]
    assert report.counts_by_reason()[GapReason.NEVER_ATTEMPTED] == 3


def test_entries_are_ordered_as_a_timeline_across_sources() -> None:
    report = report_over(
        SESSION,
        MONDAY,
        sources=(SOURCE, LEGACY),
        expectations={SOURCE: expectation(SOURCE), LEGACY: expectation(LEGACY)},
    )

    keys = [(entry.logical_date, entry.source) for entry in report.entries]
    assert keys == sorted(keys)


def test_the_report_says_which_sources_it_examined() -> None:
    """An empty set over no sources means "we track nothing", not "nothing is missing"."""
    empty = build_report(SESSION, MONDAY, sources=(), records={}, calendar=trading_calendar())

    assert empty.sources == ()
    assert empty.pairs_examined == 0
    assert empty.unexplained == ()
    assert "0 source(s)" in empty.summary()


def test_duplicate_sources_are_examined_once() -> None:
    report = report_over(SESSION, SESSION, sources=(SOURCE, SOURCE))

    assert report.sources == (SOURCE,)
    assert report.pairs_examined == 1


# ── acceptance 3: a deliberately deleted L1 partition shows up as unexplained ────────────────


@pytest.fixture
def lake(tmp_path: Path) -> Path:
    """A scratch data root with an L1 dataset that already holds one partition."""
    partition = l1_partition_dir(SOURCE, MONDAY, data_root=tmp_path)
    partition.mkdir(parents=True)
    (partition / "part.parquet").write_bytes(b"PAR1rows")
    return tmp_path


def test_a_present_l1_partition_is_present(lake: Path) -> None:
    assert LakeL1Presence(lake).check(SOURCE, MONDAY).check is L1Check.PRESENT


def test_a_deleted_l1_partition_shows_up_as_unexplained(lake: Path) -> None:
    """Acceptance 3, end to end: PUBLISHED plus a missing partition is a missing day.

    The state machine's memory of a success is not evidence that the data still exists. Deleting
    the partition under a `PUBLISHED` row must flip the pair from complete to unexplained, or a
    lost L1 partition would be invisible to the one report that is supposed to find it.
    """
    published = rows(record(SyncState.PUBLISHED, logical_date=MONDAY))
    expectations = {SOURCE: expectation(l1_dataset=SOURCE)}
    presence = LakeL1Presence(lake)

    before = report_over(
        MONDAY, MONDAY, records=published, expectations=expectations, l1_presence=presence
    )
    assert before.fully_explained
    assert before.complete == 1
    assert before.l1_unchecked == 0

    partition = l1_partition_dir(SOURCE, MONDAY, data_root=lake)
    (partition / "part.parquet").unlink()
    partition.rmdir()

    after = report_over(
        MONDAY, MONDAY, records=published, expectations=expectations, l1_presence=presence
    )

    assert not after.fully_explained
    assert [entry.reason for entry in after.unexplained] == [GapReason.L1_PARTITION_MISSING]
    entry = after.unexplained[0]
    assert entry.state is SyncState.PUBLISHED, "the sync_state history travels with the entry"
    assert entry.l1_partition == str(partition)
    assert after.complete == 0


def test_an_empty_partition_file_is_not_a_partition(lake: Path) -> None:
    """A zero-byte part file is a failed write. Counting it as data would hide the failure."""
    partition = l1_partition_dir(SOURCE, MONDAY, data_root=lake)
    (partition / "part.parquet").write_bytes(b"")

    assert LakeL1Presence(lake).check(SOURCE, MONDAY).check is L1Check.ABSENT


def test_a_dataset_that_was_never_normalised_is_counted_not_flagged(tmp_path: Path) -> None:
    """Before M1.8 there is no L1 at all; one false gap per published date would bury the real ones.

    The count is the honesty: `l1_unchecked` says how many PUBLISHED pairs went unverified, so an
    empty unexplained set can never be read as "the lake was checked and is fine".
    """
    report = report_over(
        MONDAY,
        MONDAY,
        records=rows(record(SyncState.PUBLISHED, logical_date=MONDAY)),
        expectations={SOURCE: expectation(l1_dataset=SOURCE)},
        l1_presence=LakeL1Presence(tmp_path),
    )

    assert report.fully_explained
    assert report.l1_unchecked == 1
    assert "L1 unverified for 1" in report.summary()


def test_the_l1_partition_is_only_looked_up_for_a_row_that_claims_the_data_is_there(
    lake: Path,
) -> None:
    """A FAILED pair is already unexplained for a better reason; the lake has nothing to add."""
    entry = classify_pair(
        expectation(l1_dataset=SOURCE),
        MONDAY,
        DayKind.SESSION,
        record(SyncState.FAILED, logical_date=MONDAY, last_error="HTTP 500"),
        l1=L1Result(L1Check.ABSENT, l1_partition_dir(SOURCE, MONDAY, data_root=lake)),
    )

    assert entry is not None
    assert entry.reason is GapReason.FAILED


# ── what the source register contributes ─────────────────────────────────────────────────────


def test_expectations_come_from_the_checked_in_source_register() -> None:
    """The eras and cadences live in C.1's register; restating them here would be a second truth."""
    expectations = expectations_from_register()

    udiff = expectations[SOURCE]
    legacy = expectations[LEGACY]
    assert udiff.per_session and legacy.per_session
    assert legacy.era_end == date(2024, 7, 8)
    assert udiff.era_start == date(2024, 7, 8)
    assert not udiff.in_era(date(2024, 7, 7))
    assert legacy.in_era(date(2024, 7, 7))


def test_a_non_per_session_cadence_is_not_measured_against_the_trading_calendar() -> None:
    quarterly = [
        entry
        for entry in expectations_from_register().values()
        if not entry.per_session and entry.l1_dataset is None
    ]

    assert quarterly, "the register carries weekly/quarterly sources; none may owe a daily file"
    assert all(entry.source for entry in quarterly)
    assert "quarterly" not in PER_SESSION_CADENCES


def test_a_source_the_register_does_not_know_still_owes_its_sessions() -> None:
    """An unregistered source defaults to owing a file every session — the loud direction."""
    report = build_report(
        SESSION,
        SESSION,
        sources=("mystery_source",),
        records={},
        calendar=trading_calendar(),
        expectations={},
    )

    assert [entry.reason for entry in report.entries] == [GapReason.NEVER_ATTEMPTED]


# ── the wire projection ──────────────────────────────────────────────────────────────────────


def test_the_payload_carries_the_full_count_even_when_the_list_is_truncated() -> None:
    """A status endpoint whose total is its own page size cannot report a flood."""
    from dataplatform.status.models import GapsOut

    report = report_over(SAMPLE_START, SAMPLE_END)
    body = GapsOut.of(report, limit=2)

    assert len(body.unexplained) == 2
    assert body.unexplained_total == len(report.unexplained) > 2
    assert body.fully_explained is False
    assert body.sources == [SOURCE]


def test_the_payload_is_empty_and_says_so_when_the_range_is_complete() -> None:
    from dataplatform.status.models import GapsOut

    report = report_over(
        SAMPLE_START, SAMPLE_END, records=published_every_session(SAMPLE_START, SAMPLE_END)
    )
    body = GapsOut.of(report, limit=500)

    assert body.unexplained == []
    assert body.unexplained_total == 0
    assert body.fully_explained is True
    assert body.pairs_examined == (SAMPLE_END - SAMPLE_START).days + 1


def test_an_entry_with_no_row_projects_nulls_rather_than_zeros() -> None:
    """`None` means "there was never a row", which is not the same claim as "zero attempts"."""
    from dataplatform.status.models import GapEntryOut

    entry = classify_pair(expectation(), SESSION, DayKind.SESSION, None)
    assert isinstance(entry, GapEntry)

    out = GapEntryOut.of(entry)
    assert out.state is None
    assert out.updated_at is None
    assert out.retryable is None
    assert out.reason is GapReason.NEVER_ATTEMPTED


# ── the ten-year shape, cheaply ──────────────────────────────────────────────────────────────


def test_a_decade_wide_range_classifies_every_day_without_leaving_one_behind() -> None:
    """The M1.13 shape: 10 years, two eras, one report. Nothing may go unclassified."""
    start, end = date(2016, 1, 1), date(2025, 12, 31)
    sources = (LEGACY, SOURCE)
    report = build_report(
        start,
        end,
        sources=sources,
        records={},
        calendar=trading_calendar(),
        expectations=expectations_from_register(),
    )

    days = (end - start).days + 1
    assert report.pairs_examined == days * len(sources)
    assert report.complete == 0
    assert len(report.entries) == len(report.explained) + len(report.unexplained)

    # Each source explains the other's era: every session in the decade is owed by exactly one of
    # them, except 2024-07-08 — the cutover, which both eras name and both patterns served. That
    # identity is what makes "100% of missing days explained" a checkable claim rather than a hope.
    counts = report.counts_by_reason()
    owed = len(trading_calendar().expected_data_dates(start, end))
    cutover = date(2024, 7, 8)
    assert counts[GapReason.NEVER_ATTEMPTED] + counts[GapReason.OUTSIDE_SOURCE_ERA] == owed * 2
    assert counts[GapReason.NEVER_ATTEMPTED] == owed + 1
    assert [entry.source for entry in report.entries if entry.logical_date == cutover] == [
        LEGACY,
        SOURCE,
    ]


def test_the_summary_line_names_the_unexplained_count() -> None:
    report = report_over(SESSION, MONDAY)

    assert "UNEXPLAINED" in report.summary()
    assert str(len(report.unexplained)) in report.summary()


def test_for_source_narrows_the_enumeration() -> None:
    report = report_over(
        SESSION,
        MONDAY,
        sources=(SOURCE, LEGACY),
        expectations={SOURCE: expectation(SOURCE), LEGACY: expectation(LEGACY)},
    )

    assert {entry.source for entry in report.for_source(LEGACY)} == {LEGACY}
    assert len(report.for_source(LEGACY)) + len(report.for_source(SOURCE)) == len(report.entries)


def test_a_report_over_a_single_day_is_a_single_pair() -> None:
    report = report_over(SATURDAY, SATURDAY)

    assert report.pairs_examined == 1
    assert [entry.reason for entry in report.entries] == [GapReason.WEEKEND]
    assert report.fully_explained
    assert report.to_date - report.from_date == timedelta(0)
