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

from collections.abc import Sequence
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
from dataplatform.store.schemas import PRICES_RAW_DATASET

SOURCE = "nse_bhavcopy_udiff"
LEGACY = "nse_bhavcopy_legacy"

# A price source's sync-record id (SOURCE) and the L1 dataset its partitions actually land in are
# NOT the same name: the writer lands every exchange/era in the one canonical `prices_raw` dataset.
# Keeping these distinct here is deliberate — conflating them is exactly what hid the pre-M1.14
# presence-probe bug, where the report probed `data/L1/nse_bhavcopy_udiff/` (which never exists).
L1_DATASET = PRICES_RAW_DATASET

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
    partition = l1_partition_dir(L1_DATASET, MONDAY, data_root=tmp_path)
    partition.mkdir(parents=True)
    (partition / "part.parquet").write_bytes(b"PAR1rows")
    return tmp_path


def test_a_present_l1_partition_is_present(lake: Path) -> None:
    assert LakeL1Presence(lake).check(L1_DATASET, MONDAY).check is L1Check.PRESENT


def test_a_deleted_l1_partition_shows_up_as_unexplained(lake: Path) -> None:
    """Acceptance 3, end to end: PUBLISHED plus a missing partition is a missing day.

    The state machine's memory of a success is not evidence that the data still exists. Deleting
    the partition under a `PUBLISHED` row must flip the pair from complete to unexplained, or a
    lost L1 partition would be invisible to the one report that is supposed to find it.
    """
    published = rows(record(SyncState.PUBLISHED, logical_date=MONDAY))
    expectations = {SOURCE: expectation(l1_dataset=L1_DATASET)}
    presence = LakeL1Presence(lake)

    before = report_over(
        MONDAY, MONDAY, records=published, expectations=expectations, l1_presence=presence
    )
    assert before.fully_explained
    assert before.complete == 1
    assert before.l1_unchecked == 0

    partition = l1_partition_dir(L1_DATASET, MONDAY, data_root=lake)
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
    partition = l1_partition_dir(L1_DATASET, MONDAY, data_root=lake)
    (partition / "part.parquet").write_bytes(b"")

    assert LakeL1Presence(lake).check(L1_DATASET, MONDAY).check is L1Check.ABSENT


def test_a_dataset_that_was_never_normalised_is_counted_not_flagged(tmp_path: Path) -> None:
    """Before M1.8 there is no L1 at all; one false gap per published date would bury the real ones.

    The count is the honesty: `l1_unchecked` says how many PUBLISHED pairs went unverified, so an
    empty unexplained set can never be read as "the lake was checked and is fine".
    """
    report = report_over(
        MONDAY,
        MONDAY,
        records=rows(record(SyncState.PUBLISHED, logical_date=MONDAY)),
        expectations={SOURCE: expectation(l1_dataset=L1_DATASET)},
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
        expectation(l1_dataset=L1_DATASET),
        MONDAY,
        DayKind.SESSION,
        record(SyncState.FAILED, logical_date=MONDAY, last_error="HTTP 500"),
        l1=L1Result(L1Check.ABSENT, l1_partition_dir(L1_DATASET, MONDAY, data_root=lake)),
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


def test_price_sources_resolve_their_l1_dataset_to_prices_raw() -> None:
    """Regression (M1.14): a price source's L1 partitions land in the canonical `prices_raw`
    dataset, not under its own id. Probing `data/L1/<source_id>/` never finds them, so every
    published day silently reports NO_DATASET and a deleted partition can never surface as
    unexplained. Pin the mapping here so production and the deletion test can never drift apart
    again — both NSE eras and BSE must resolve to `prices_raw`.
    """
    expectations = expectations_from_register()
    for price_source in ("nse_bhavcopy_udiff", "nse_bhavcopy_legacy", "bse_bhavcopy_udiff"):
        assert expectations[price_source].l1_dataset == PRICES_RAW_DATASET, price_source
    # A non-price per-session source keeps its own id as its L1 dataset name.
    assert expectations["nse_fii_dii_flows"].l1_dataset == "nse_fii_dii_flows"


def test_the_price_state_source_names_match_the_ingest_source_sets() -> None:
    """The gap report scans the source names `sync_state` holds, and ingest tracks the price core
    under the era-independent source-set names (`backfill.SOURCE_SETS`), not the register's per-era
    ids. If `_PRICE_STATE_SOURCES` drifted from those names the report would probe a name production
    never writes — exactly the M1.14 defect, where the live `nse_bhavcopy` source fell through to
    the default and every published day went physically unchecked. Pin the two together.
    """
    from dataplatform.ingest.backfill import BSE_BHAVCOPY, NSE_BHAVCOPY, SOURCE_SETS
    from dataplatform.quality.gaps import _PRICE_STATE_SOURCES

    # Equality, not containment: every set name the backfill writes needs an expectation, and a
    # new one added to SOURCE_SETS without a row here is the M1.14 defect again. `nse_delivery`
    # was exactly that — wired in M1.6, missing here until the 2026-09-06 audit.
    assert set(_PRICE_STATE_SOURCES) == set(SOURCE_SETS)
    assert {NSE_BHAVCOPY, BSE_BHAVCOPY} <= set(SOURCE_SETS)
    for state_source in _PRICE_STATE_SOURCES:
        assert expectations_from_register()[state_source].l1_dataset == PRICES_RAW_DATASET


def test_l1_presence_is_verified_over_the_state_source_ingest_actually_writes(
    tmp_path: Path,
) -> None:
    """Regression (M1.14, take 2): acceptance #3 must hold on the path production runs.

    Ingest writes `sync_state` under the era-independent set name `nse_bhavcopy` and lands every
    session in `prices_raw` — not under the register's per-era ids. An earlier fix reconciled only
    the register-id path (which the live pipeline never exercises), so its tests passed while the
    live `GapScanner` still checked 0/2469 partitions. This test drives `build_report` over the real
    state-source name with the real `expectations_from_register()` mapping and a real lake, so it
    fails if the report ever again probes a dataset the writer does not use.
    """
    from dataplatform.ingest.backfill import NSE_BHAVCOPY

    start, end = date(2026, 8, 3), date(2026, 8, 7)  # a full UDiFF-era trading week
    published = published_every_session(start, end, source=NSE_BHAVCOPY)
    sessions = [d for (_src, d), r in published.items() if r.state is SyncState.PUBLISHED]
    assert sessions, "the sampled week must contain trading sessions"
    for day in sessions:
        part = l1_partition_dir(PRICES_RAW_DATASET, day, data_root=tmp_path)
        part.mkdir(parents=True)
        (part / "part.parquet").write_bytes(b"PAR1rows")

    presence = LakeL1Presence(tmp_path)
    real = expectations_from_register()  # the production mapping, not a hand-built one

    before = report_over(
        start,
        end,
        sources=(NSE_BHAVCOPY,),
        records=published,
        expectations=real,
        l1_presence=presence,
    )
    assert before.l1_unchecked == 0, (
        "every published prices_raw partition must be physically checked"
    )
    assert before.complete == len(sessions)

    # Acceptance #3: a deleted prices_raw partition surfaces the day as unexplained, not swallowed.
    victim = l1_partition_dir(PRICES_RAW_DATASET, sessions[0], data_root=tmp_path)
    (victim / "part.parquet").unlink()
    victim.rmdir()
    after = report_over(
        start,
        end,
        sources=(NSE_BHAVCOPY,),
        records=published,
        expectations=real,
        l1_presence=presence,
    )
    assert after.l1_unchecked == 0
    assert GapReason.L1_PARTITION_MISSING in [entry.reason for entry in after.unexplained]


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


# ── unit rows: the sub-keyed sources the 2026-09-06 audit found (finding N1) ────────────────────


def test_a_failed_unit_row_is_reported_and_names_its_unit() -> None:
    """A filing that failed is a miss the report must enumerate, with the id to go and look at.

    Before migration 0008 a filing's id lived in the `source` string, so it was neither a source
    the register knew nor a pair anything looked up — 2,392 stuck filings sat unreported behind a
    `/status/gaps` that answered 500.
    """
    row = record(
        SyncState.FAILED,
        source="nse_xbrl_filing/IF87652",
        logical_date=SESSION,
        retryable=False,
        last_error="parse failed: no results column covers 2025-01-01 to 2025-03-31",
    )
    report = build_report(
        SESSION,
        SESSION,
        sources=["nse_xbrl_filing"],
        records={},
        unit_records=[row],
        expectations={
            "nse_xbrl_filing": SourceExpectation(source="nse_xbrl_filing", per_session=False)
        },
    )

    (entry,) = report.unexplained
    assert entry.reason is GapReason.FAILED
    assert entry.source == "nse_xbrl_filing"
    assert entry.unit == "IF87652"
    assert entry.qualified_source == "nse_xbrl_filing/IF87652"
    assert entry.last_error is not None and "no results column" in entry.last_error


def test_a_published_unit_row_is_complete_and_not_enumerated() -> None:
    """The 69,000 filings that worked must not turn the report into a listing of everything."""
    published = [
        record(SyncState.PUBLISHED, source=f"nse_xbrl_filing/IF{n}", logical_date=SESSION)
        for n in range(50)
    ]
    report = build_report(
        SESSION,
        SESSION,
        sources=["nse_xbrl_filing"],
        records={},
        unit_records=published,
        expectations={
            "nse_xbrl_filing": SourceExpectation(source="nse_xbrl_filing", per_session=False)
        },
    )

    assert report.entries == ()
    assert report.complete == 50
    # 51: the fifty filings, plus the source's own (source, date) pair from the calendar walk,
    # which a non-per-session source owes nothing for and which therefore adds no entry.
    assert report.pairs_examined == 51
    assert report.fully_explained


def test_a_unit_row_for_a_source_outside_the_scan_is_not_examined() -> None:
    """`sources` still bounds the scan — a unit does not smuggle its source into the report."""
    row = record(SyncState.FAILED, source="nse_xbrl_filing/IF1", logical_date=SESSION)
    report = build_report(
        SESSION,
        SESSION,
        sources=[SOURCE],
        records={},
        unit_records=[row],
        expectations={SOURCE: SourceExpectation(source=SOURCE, per_session=False)},
    )

    assert report.entries == ()
    assert report.pairs_examined == 1  # the source's own pair, not the filing's


def test_a_unit_row_dated_outside_the_calendar_is_reported_rather_than_dropped() -> None:
    """A filing's date comes from its index entry and can leave C.2's coverage. Say so.

    The per-session walk cannot reach this — it is driven by the calendar — so this is the one
    path where a real row exists for a day nothing can classify. Silently skipping it would be
    the drop this module exists to prevent.
    """
    outside = date(2011, 3, 4)
    row = record(SyncState.PUBLISHED, source="nse_xbrl_filing/IFOLD", logical_date=outside)
    report = build_report(
        SESSION,
        SESSION,
        sources=["nse_xbrl_filing"],
        records={},
        unit_records=[row],
        expectations={
            "nse_xbrl_filing": SourceExpectation(source="nse_xbrl_filing", per_session=False)
        },
    )

    (entry,) = report.unexplained
    assert entry.reason is GapReason.OUTSIDE_CALENDAR
    assert entry.unit == "IFOLD"
    assert not GapReason.OUTSIDE_CALENDAR.explained


def test_a_range_fetched_source_owes_nothing_per_session() -> None:
    """`nse_corp_actions` publishes daily but is *fetched* in yearly ranges. Not 2,461 misses.

    The register's `cadence` describes the feed; `per_session` asks what ingest owes each day. For
    this source they differ, and taking the register's word for it filed one NEVER_ATTEMPTED per
    trading day — burying the real misses under an artefact of the reader's own assumption.
    """
    expectations = expectations_from_register()
    assert expectations["nse_corp_actions"].per_session is False
    assert expectations["nse_bhavcopy"].per_session is True


# ── L0_PRESENT_L1_ABSENT: is this a parser fix or a re-fetch? (finding N2, P1.2) ─────────────────


class _StubL0Presence:
    """An `L0Presence` that answers from a set of `(source, date)` pairs it was told about."""

    def __init__(self, *present: tuple[str, date], per_session_only: bool = True) -> None:
        self._present = set(present)
        self._per_session_only = per_session_only

    def holds(
        self,
        sources: Sequence[str],
        logical_date: date,
        l0_path: str | None,
        *,
        per_session: bool,
    ) -> bool:
        if self._per_session_only and not per_session and not l0_path:
            return False
        return any((source, logical_date) in self._present for source in sources)


def test_a_failed_pair_whose_payload_is_in_l0_says_so() -> None:
    """The difference between "fix the parser" and "fetch it again", made a reason of its own."""
    failed = record(SyncState.FAILED, last_error="TIMESTAMP is '13-Jul-20'")
    report = build_report(
        SESSION,
        SESSION,
        sources=[SOURCE],
        records=rows(failed),
        expectations={SOURCE: SourceExpectation(source=SOURCE, l1_dataset=L1_DATASET)},
        l0_presence=_StubL0Presence((SOURCE, SESSION)),
    )

    (entry,) = report.unexplained
    assert entry.reason is GapReason.L0_PRESENT_L1_ABSENT
    assert "not a re-fetch" in entry.detail
    assert entry.last_error == "TIMESTAMP is '13-Jul-20'"


def test_a_failed_pair_with_no_payload_stays_a_plain_failure() -> None:
    """Both directions. A FAILED that quietly became L0_PRESENT would send nobody to re-fetch."""
    report = build_report(
        SESSION,
        SESSION,
        sources=[SOURCE],
        records=rows(record(SyncState.FAILED, last_error="404")),
        expectations={SOURCE: SourceExpectation(source=SOURCE, l1_dataset=L1_DATASET)},
        l0_presence=_StubL0Presence(),
    )

    (entry,) = report.unexplained
    assert entry.reason is GapReason.FAILED


def test_the_directory_fallback_never_answers_for_a_unit_row(tmp_path: Path) -> None:
    """A month directory full of other filings is not evidence about the one that failed.

    The 774 filings whose documents were never fetched sit in months packed with documents that
    were. Answering L0_PRESENT for them would tell an operator not to re-fetch precisely the
    filings that need re-fetching.
    """
    from dataplatform.quality.gaps import LakeL0Presence
    from dataplatform.store.paths import l0_dir

    directory = l0_dir("nse_xbrl_filing", SESSION, data_root=tmp_path)
    directory.mkdir(parents=True)
    (directory / "SOMEONE_ELSES_FILING.xml").write_bytes(b"<xbrl/>")
    presence = LakeL0Presence(data_root=tmp_path)

    assert presence.holds(["nse_xbrl_filing"], SESSION, None, per_session=False) is False
    # …and the same directory *is* evidence for a per-session source, where the date is the key.
    assert presence.holds(["nse_xbrl_filing"], SESSION, None, per_session=True) is True


def test_the_recorded_l0_key_is_resolved_through_the_lake_layout(tmp_path: Path) -> None:
    """`L0Ref.key` is `<source>/<iso-date>/<file>`; the lake shards by year and month.

    Joining the key onto the L0 root would look for a directory that does not exist and report
    "no bytes" for a payload sitting right there.
    """
    from dataplatform.quality.gaps import LakeL0Presence
    from dataplatform.store.paths import l0_dir

    directory = l0_dir(SOURCE, SESSION, data_root=tmp_path)
    directory.mkdir(parents=True)
    (directory / "payload.csv").write_bytes(b"data")

    presence = LakeL0Presence(data_root=tmp_path)
    key = f"{SOURCE}/{SESSION.isoformat()}/payload.csv"
    assert presence.holds([SOURCE], SESSION, key, per_session=False) is True
    assert presence.holds([SOURCE], SESSION, f"{SOURCE}/x/missing.csv", per_session=False) is False
