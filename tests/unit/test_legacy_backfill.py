"""The W1 deep-backfill driver: resume, the 404-is-data path, and the E1 quarantine (W1).

Four claims are load-bearing enough that the campaign should not be launched without them proved
offline, and each has a test here that would fail if the claim broke:

* **A second run over an acquired range spends zero requests.** Not "few" — zero. Proved by
  counting the `RecordedTransport`'s requests, which is the only number the host cares about.
* **A 404 is data.** It must land in the evidence journal, not in the error count, and not trip the
  hard stop. A run of 404s against a real error streak is asserted separately, because the two
  counters must not be the same counter.
* **The calendar guard is respected, not routed around.** With a calendar covering the range the
  plan comes from it; with one that refuses the range the plan widens to weekdays and says so.
  Both halves are asserted, and the refusing half is asserted against an **injected** calendar,
  never against the shipped `nse_holidays.yaml`. That file is data and is expected to keep
  growing: this suite once pinned the fallback to the real file refusing 2006, and went red the
  day `w0/era-coverage` extended the file back to 2006-01-01 without touching a line of driver
  code. A test that asserts the system *cannot* do something becomes a barrier the moment someone
  does it. The real file is still exercised at the boundary it actually has — below 2006, where
  the bhavcopy archive reaches and the calendar does not.
* **E1 rows land in quarantine, never in `prices_raw`.** Asserted by reading the parquet back:
  `prices_raw` has no partition for the date at all, and the quarantine partition has every row.

Offline by construction (B8): the transport is a `RecordedTransport` script, the lake is a
`tmp_path`, and the sync store is an in-memory stand-in. No socket, no docker, no network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Final, cast

import pyarrow.parquet as pq
import pytest
from structlog.testing import capture_logs

from dataplatform.alerts import build_alerter
from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.ingest import legacy_backfill as lb
from dataplatform.ingest.backfill import NSE_BHAVCOPY
from dataplatform.ingest.calendar import (
    CalendarCoverageError,
    DayKind,
    Holiday,
    Provenance,
    TradingCalendar,
    trading_calendar,
)
from dataplatform.ingest.fetcher import (
    Fetcher,
    RecordedResponse,
    RecordedTransport,
    TransportError,
)
from dataplatform.ingest.nse import eras
from dataplatform.ingest.nse.eras import ISIN_ERA_START, PRE_ISIN_ERA_LAST_SESSION
from dataplatform.ingest.source_register import SourceRegister
from dataplatform.ingest.source_register import load as load_register
from dataplatform.status.sync_state import SyncState
from dataplatform.store.l0 import L0Store
from dataplatform.store.paths import Layer, partition_path
from dataplatform.store.schemas import (
    PRICES_RAW_DATASET,
    PRICES_RAW_QUARANTINE_DATASET,
    PriceQuarantineReason,
)

FIXTURES: Final = Path(__file__).resolve().parents[1] / "fixtures" / "nse_bhavcopy"
PRE_ISIN_FIXTURE: Final = FIXTURES / "pre_isin" / "cm21JUN2011bhav.csv.zip"
ISIN_FIXTURE: Final = FIXTURES / "legacy" / "cm22JUN2011bhav.csv.zip"

CLOCK: Final = FrozenClock(date(2026, 9, 8))

#: Two consecutive sessions straddling the ISIN cutover, plus a weekday the exchange was shut.
E1_SESSION: Final = PRE_ISIN_ERA_LAST_SESSION
E2_SESSION: Final = ISIN_ERA_START
HOLIDAY_SESSION: Final = date(2011, 8, 15)  # Independence Day, a Monday — 404, verified live

#: The campaign's deep range: the first session the extended calendar covers through the last
#: session before the daily lake begins. The counts below are asserted, not described.
DEEP_START: Final = date(2006, 1, 2)
DEEP_END: Final = date(2016, 9, 1)

#: A range the archive serves and the calendar does not reach — the real file's actual floor.
PRE_CALENDAR_START: Final = date(2004, 1, 5)
PRE_CALENDAR_END: Final = date(2005, 12, 30)


# ── wiring ───────────────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def register() -> SourceRegister:
    """The checked-in source register — the same URL templates production resolves."""
    return load_register()


def url_for(day: date, register: SourceRegister) -> str:
    return lb.legacy_url(day, register=register)


def settings_for(tmp_path: Path) -> Settings:
    return Settings(data_root=tmp_path)


def fetcher_for(
    transport: RecordedTransport, settings: Settings, register: SourceRegister
) -> Fetcher:
    """A real `Fetcher` over a scripted transport: the policy engine runs, the socket does not."""
    return Fetcher(
        transport=transport,
        l0=L0Store(clock=CLOCK, data_root=settings.data_root),
        alerter=build_alerter(settings, clock=CLOCK),
        clock=CLOCK,
        register=register,
        settings=settings,
        sleep=lambda _seconds: None,
    )


def transport_for(
    register: SourceRegister, served: dict[date, bytes], not_found: tuple[date, ...] = ()
) -> RecordedTransport:
    """A transport that serves the given sessions' bytes and 404s the rest."""
    script: dict[str, RecordedResponse | list[RecordedResponse]] = {}
    for day, body in served.items():
        script[url_for(day, register)] = RecordedResponse(
            status_code=200, body=body, headers={"content-type": "application/zip"}
        )
    for day in not_found:
        script[url_for(day, register)] = RecordedResponse(
            status_code=404,
            body=b"<html>Not Found</html>",
            headers={"content-type": "text/html"},
        )
    return RecordedTransport(cast("Any", script))


def acquisition_for(
    tmp_path: Path,
    register: SourceRegister,
    transport: RecordedTransport,
    **kwargs: Any,
) -> tuple[lb.LegacyAcquisition, L0Store, lb.NoSessionJournal]:
    """The acquisition driver wired to a scratch lake, plus the lake and journal to assert on."""
    settings = settings_for(tmp_path)
    l0 = L0Store(clock=CLOCK, data_root=settings.data_root)
    journal = lb.NoSessionJournal(lb.journal_path_for(settings.data_root), clock=CLOCK)
    driver = lb.LegacyAcquisition(
        fetcher=fetcher_for(transport, settings, register),
        l0=l0,
        journal=journal,
        register=register,
        **kwargs,
    )
    return driver, l0, journal


# ── injected calendars: one that covers the range, one that refuses it ───────────────────────


def extended_calendar() -> TradingCalendar:
    """A `TradingCalendar` covering 2006-2016 — the shape the shipped holiday file now has.

    Built in memory rather than by reading `nse_holidays.yaml`, so a test of the *driver* asserts
    nothing about the file's contents. It carries exactly the two closures these tests need to
    distinguish, which is enough to prove the injection point works: production swaps the real
    file in at the same seam with no code change.
    """
    holidays = {
        day: Holiday(date=day, name="Independence Day")
        for day in (date(2011, 8, 15), date(2006, 8, 15))
    }
    return TradingCalendar(
        coverage_start=date(2006, 1, 1),
        coverage_end=date(2016, 12, 31),
        provenance=Provenance(
            curated_by="test",
            curated_on=date(2026, 9, 8),
            method="in-memory fixture standing in for the extended nse_holidays.yaml",
            bhavcopy_probes=0,
            known_limitation="carries only the closures these tests distinguish",
        ),
        _holidays=holidays,
        _sources=dict.fromkeys(range(2006, 2017), "test_fixture"),
    )


def refusing_calendar() -> TradingCalendar:
    """A `TradingCalendar` covering 2016 only — one that refuses every range this driver plans.

    The fallback in `plan_sessions` fires on `CalendarCoverageError`, and the seam that lets it be
    provoked is calendar injection. Pinning it to the *shipped* file refusing 2006 is what rotted:
    the file grew and the branch lost its only test. Nothing about this fixture can be invalidated
    by `nse_holidays.yaml` gaining a year, because it never reads it.
    """
    return TradingCalendar(
        coverage_start=date(2016, 1, 1),
        coverage_end=date(2016, 12, 31),
        provenance=Provenance(
            curated_by="test",
            curated_on=date(2026, 9, 8),
            method="in-memory fixture standing in for any calendar too narrow for the range",
            bhavcopy_probes=0,
            known_limitation="declares no closures; only its coverage span is load-bearing",
        ),
        _holidays={},
        _sources={2016: "test_fixture"},
    )


# ── planning: the calendar is an input, and the guard is not routed around ───────────────────


def test_the_real_calendar_covers_the_deep_range_and_the_plan_comes_from_it() -> None:
    """The shipped calendar reaches 2006, so the deep plan is a calendar plan and says so.

    The capability `w0/era-coverage` delivered, asserted through the driver rather than through
    the YAML: `nse_holidays.yaml` covering 2006-01-01..2026-12-31 is what makes this range take
    the narrow branch, with no edit to `plan_sessions` or to `calendar.py`. Declared holidays stop
    costing a request, so an absence the campaign does observe is one the calendar did not
    predict — which is the only kind worth reconciling.
    """
    plan = lb.plan_sessions(DEEP_START, DEEP_END, calendar=trading_calendar())

    assert plan.basis == "calendar"
    assert len(plan) == 2636, "the calendar's expected sessions across the deep range"
    assert plan.dates[0] == DEEP_START
    assert plan.dates[-1] == DEEP_END
    assert HOLIDAY_SESSION not in plan.dates, "a declared holiday owes no file and costs no request"
    assert date(2011, 8, 16) in plan.dates, "and the session after it is still a candidate"
    assert "spans the range" in plan.note
    assert [era.label for era in plan.eras] == ["E1", "E2"]


def test_the_plan_widens_to_weekdays_when_the_calendar_refuses_the_range() -> None:
    """A calendar too narrow for the range widens the plan and names the gap — never narrows it.

    Deliberately named for the behaviour and not for the state of the shipped data file. The
    refusal is injected, so this stays a test of the driver however far `nse_holidays.yaml` grows.
    Widening is the only direction a fallback about missing data may go: it costs more requests
    and proves each absence with a 404 instead of assuming it.
    """
    refusing = refusing_calendar()
    with pytest.raises(CalendarCoverageError):
        refusing.expected_data_dates(DEEP_START, DEEP_END)

    with capture_logs() as entries:
        plan = lb.plan_sessions(DEEP_START, DEEP_END, calendar=refusing)

    assert plan.basis == "weekday"
    assert len(plan) == 2784, "every weekday in the range is a candidate"
    assert all(day.weekday() < 5 for day in plan.dates)
    assert HOLIDAY_SESSION in plan.dates, "an absence must be proved by a 404, not assumed"
    assert "calendar refused the range" in plan.note
    assert "2016-01-01" in plan.note, "the note must name the coverage the calendar does have"

    # Strictly wider than the covering calendar would have made it, over the same range.
    assert len(plan) > len(lb.plan_sessions(DEEP_START, DEEP_END, calendar=trading_calendar()))

    # And the gap reaches the log, not only the note: an operator reading the run must see that
    # this plan cost ~6% more requests because the calendar could not answer.
    (gap,) = [entry for entry in entries if entry["event"] == "legacy_backfill.calendar_gap"]
    assert gap["log_level"] == "warning"
    assert gap["coverage_start"] == "2016-01-01"
    assert gap["coverage_end"] == "2016-12-31"
    assert gap["basis"] == "weekday"
    assert gap["start"] == DEEP_START.isoformat()
    assert gap["end"] == DEEP_END.isoformat()


def test_a_range_below_the_calendars_floor_still_meets_the_real_guard() -> None:
    """The honest remaining refusal against the shipped file: the archive predates the calendar.

    `nse_holidays.yaml` starts at 2006 and the bhavcopy archive starts in 1995, so a 2004 plan is
    still a weekday plan. This keeps the real-file path tested at the boundary it actually has,
    rather than at one that has moved once and may move again.
    """
    plan = lb.plan_sessions(PRE_CALENDAR_START, PRE_CALENDAR_END, calendar=trading_calendar())

    assert plan.basis == "weekday"
    assert len(plan) == 520, "every weekday across the two years"
    assert all(day.weekday() < 5 for day in plan.dates)
    assert "calendar refused the range" in plan.note
    assert [era.label for era in plan.eras] == ["E1"]


def test_with_a_covering_calendar_the_plan_comes_from_it_instead() -> None:
    """The same narrowing through the injection seam, on a range small enough to enumerate.

    A declared holiday stops being a candidate, so the campaign spends fewer requests and the
    absences it does observe are the ones the calendar did not predict.
    """
    calendar = extended_calendar()
    plan = lb.plan_sessions(date(2011, 8, 12), date(2011, 8, 19), calendar=calendar)

    assert plan.basis == "calendar"
    assert HOLIDAY_SESSION not in plan.dates, "a declared holiday owes no file and costs no request"
    assert date(2011, 8, 16) in plan.dates
    assert len(plan) == 5


def test_a_sampled_plan_spans_both_eras_rather_than_the_first_n_days() -> None:
    """`--limit` samples across the whole range, so a smoke run crosses the ISIN boundary."""
    plan = lb.plan_sessions(date(2006, 1, 2), date(2016, 9, 1), calendar=None, limit=10)
    assert len(plan) == 10
    labels = {eras.era_for(day).label for day in plan.dates}
    assert labels == {"E1", "E2"}


def test_a_udiff_date_is_refused_rather_than_given_a_legacy_url(register: SourceRegister) -> None:
    """This driver is the pre-UDiFF archive's. A 2024-08 date must not get a `cm…bhav` URL."""
    with pytest.raises(ValueError, match=r"use `dataplatform\.ingest\.backfill"):
        lb.legacy_url(date(2024, 8, 1), register=register)
    assert lb.require_legacy_era(E1_SESSION).label == "E1"
    assert lb.require_legacy_era(E2_SESSION).label == "E2"


# ── acquisition: L0, and nothing else ────────────────────────────────────────────────────────


def test_acquisition_stores_both_eras_and_leaves_l0_immutable(
    tmp_path: Path, register: SourceRegister
) -> None:
    """Both sessions land under their own key with the checksum the sidecar records."""
    transport = transport_for(
        register,
        {E1_SESSION: PRE_ISIN_FIXTURE.read_bytes(), E2_SESSION: ISIN_FIXTURE.read_bytes()},
    )
    driver, l0, _ = acquisition_for(tmp_path, register, transport)

    report = driver.run([E1_SESSION, E2_SESSION])

    assert report.fetched == 2
    assert report.failed == 0
    assert report.requests_spent == 2
    assert [outcome.era for outcome in report.outcomes] == ["E1", "E2"]
    assert all(outcome.state is lb.SessionState.FETCHED for outcome in report.outcomes)

    for day, fixture in ((E1_SESSION, PRE_ISIN_FIXTURE), (E2_SESSION, ISIN_FIXTURE)):
        name = fixture.name
        assert l0.exists("nse_bhavcopy_legacy", day, name)
        ref = l0.ref_for("nse_bhavcopy_legacy", day, name)
        assert l0.get(ref) == fixture.read_bytes(), "L0 re-checksums on the way out"
        assert ref.size_bytes == len(fixture.read_bytes())


def test_a_second_run_over_an_acquired_range_performs_zero_fetches(
    tmp_path: Path, register: SourceRegister
) -> None:
    """The acceptance criterion, counted at the transport: zero requests, not merely fewer.

    Both resume paths are exercised at once — two sessions already in L0 and one date already
    proved a non-session — because a resume that only remembers successes would re-spend a request
    on every holiday, every run, for the life of the campaign.
    """
    served = {E1_SESSION: PRE_ISIN_FIXTURE.read_bytes(), E2_SESSION: ISIN_FIXTURE.read_bytes()}
    plan = [E1_SESSION, E2_SESSION, HOLIDAY_SESSION]

    transport = transport_for(register, served, not_found=(HOLIDAY_SESSION,))
    driver, _, _ = acquisition_for(tmp_path, register, transport)
    first = driver.run(plan)
    assert first.requests_spent == 3

    # A fresh driver over the same lake and the same journal file — a restarted campaign, not a
    # warm object with a cache.
    transport_again = transport_for(register, served, not_found=(HOLIDAY_SESSION,))
    driver_again, _, journal_again = acquisition_for(tmp_path, register, transport_again)
    second = driver_again.run(plan)

    assert transport_again.requests == [], "a resumed run must open nothing at all"
    assert second.requests_spent == 0
    assert second.fetched == 0
    assert second.already_in_l0 == 2
    assert second.known_no_session == 1
    assert second.failed == 0
    assert journal_again.dates == frozenset({HOLIDAY_SESSION})


# ── the 404 path: a closed exchange is data, not a failure ───────────────────────────────────


def test_a_404_is_recorded_as_evidence_and_is_not_an_error(
    tmp_path: Path, register: SourceRegister
) -> None:
    """`HOLIDAY_OR_NO_SESSION`, in an append-only journal, with the failure count still at zero."""
    transport = transport_for(
        register, {E2_SESSION: ISIN_FIXTURE.read_bytes()}, not_found=(HOLIDAY_SESSION,)
    )
    driver, _, journal = acquisition_for(tmp_path, register, transport)

    report = driver.run([E2_SESSION, HOLIDAY_SESSION])

    assert report.failed == 0, "a 404 must never increment the error counter"
    assert report.no_session == 1
    assert report.hard_stopped is False
    assert report.no_session_dates == (HOLIDAY_SESSION,)

    (record,) = journal.records
    assert record.trade_date == HOLIDAY_SESSION
    assert record.http_status == 404
    assert record.evidence == "HOLIDAY_OR_NO_SESSION"
    assert record.era == "E2"
    assert record.observed_at.tzinfo is not None
    assert journal.path.is_file(), "the evidence must be on disk, not only in memory"


def test_a_long_run_of_404s_never_trips_the_error_hard_stop(
    tmp_path: Path, register: SourceRegister
) -> None:
    """A holiday week must not look like a broken source. The two streaks are separate counters."""
    week = [date(2011, 8, 15) + timedelta(days=offset) for offset in range(5)]
    transport = transport_for(register, {}, not_found=tuple(week))
    driver, _, journal = acquisition_for(
        tmp_path, register, transport, error_streak_limit=2, no_session_streak_limit=20
    )

    report = driver.run(week)

    assert report.no_session == 5
    assert report.failed == 0
    assert report.hard_stopped is False
    assert len(journal.records) == 5


def test_an_implausible_run_of_404s_does_stop_the_campaign(
    tmp_path: Path, register: SourceRegister
) -> None:
    """ "The archive moved" and "the exchange was shut" look identical one request at a time.

    So there is a second, much larger streak limit. Without it a relocated archive would write a
    decade of phantom holidays into the highest-authority record of the trading calendar.
    """
    days = [date(2011, 8, 15) + timedelta(days=offset) for offset in range(6)]
    transport = transport_for(register, {}, not_found=tuple(days))
    driver, _, _ = acquisition_for(tmp_path, register, transport, no_session_streak_limit=3)

    report = driver.run(days)

    assert report.hard_stopped is True
    assert report.stop_reason is not None
    assert "consecutive 404s" in report.stop_reason
    assert report.no_session == 3, "it stops at the limit rather than finishing the plan"


def test_consecutive_real_errors_do_stop_the_campaign(
    tmp_path: Path, register: SourceRegister
) -> None:
    """An unexpected failure is counted, stays retryable, and a streak of them ends the run."""
    days = [date(2011, 6, 22) + timedelta(days=offset) for offset in range(5)]
    script: dict[str, Any] = {
        url_for(day, register): TransportError(f"connection reset for {day}") for day in days
    }
    transport = RecordedTransport(script)
    driver, _, journal = acquisition_for(tmp_path, register, transport, error_streak_limit=2)

    report = driver.run(days)

    assert report.hard_stopped is True
    assert report.stop_reason == "2 consecutive unexpected failures"
    assert report.failed == 2
    assert journal.records == (), "a transport error is not evidence about the trading calendar"
    assert all(outcome.error for outcome in report.outcomes)


def test_a_non_404_http_status_is_a_failure_not_a_holiday(
    tmp_path: Path, register: SourceRegister
) -> None:
    """A 400 from the archive says nothing about whether the exchange traded."""
    script: dict[str, Any] = {
        url_for(E2_SESSION, register): RecordedResponse(
            status_code=400, body=b"bad request", headers={"content-type": "text/plain"}
        )
    }
    driver, _, journal = acquisition_for(tmp_path, register, RecordedTransport(script))

    report = driver.run([E2_SESSION])

    assert report.failed == 1
    assert report.no_session == 0
    assert journal.dates == frozenset()


def test_a_corrupt_journal_line_is_refused_rather_than_skipped(tmp_path: Path) -> None:
    """The 404 journal is evidence: reading past a bad line turns a lost 404 into a re-fetch."""
    path = lb.journal_path_for(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"trade_date": "2011-08-15"}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="append-only"):
        lb.NoSessionJournal(path, clock=CLOCK)


# ── reconciliation: report the diff, never patch the calendar ────────────────────────────────


def test_the_diff_reports_both_directions_of_disagreement() -> None:
    """A 404 on a declared session, and a served file on a declared holiday, are opposite bugs."""
    calendar = extended_calendar()
    diff = lb.reconcile_calendar(
        start=date(2011, 8, 12),
        end=date(2011, 8, 19),
        served=[HOLIDAY_SESSION, date(2011, 8, 16)],
        no_session=[date(2011, 8, 17)],
        calendar=calendar,
    )

    assert diff.covered is True
    assert diff.agrees is False
    # The calendar declares 2011-08-15 a holiday, but the archive served a file for it.
    assert diff.unexpected_sessions == (HOLIDAY_SESSION,)
    # And it calls 2011-08-17 a session, which the archive answered 404 for.
    assert diff.undeclared_closures == (date(2011, 8, 17),)


def test_the_covered_deep_range_reconciles_to_a_real_claim_against_the_shipped_calendar() -> None:
    """Two independently derived sources agreeing about 2006, asserted rather than assumed.

    `w0/era-coverage` derived the year from NSE Indices' NIFTY 50 daily history and the exchange's
    own circulars; this driver proves a closure by asking the bhavcopy archive and getting a 404.
    2006-08-15 is Independence Day in the file and a 404 in the archive, and 2006-01-02 is a
    session in both — so the diff is empty in both directions and, crucially, `covered` is True.
    An empty diff from an *uncovered* range would mean nothing at all, which is why `covered` is
    asserted first.
    """
    diff = lb.reconcile_calendar(
        start=DEEP_START,
        end=date(2006, 12, 29),
        served=[date(2006, 1, 2)],
        no_session=[date(2006, 8, 15)],
        calendar=trading_calendar(),
    )
    assert diff.covered is True, "the shipped calendar reaches 2006 and must make a claim"
    assert diff.agrees is True
    assert diff.undeclared_closures == ()
    assert diff.unexpected_sessions == ()
    assert "0 undeclared closures" in diff.summary()


def test_reconcile_makes_no_claim_when_the_calendar_refuses_the_range() -> None:
    """ "We cannot compare yet" is a legitimate answer and must not stop a campaign.

    Injected refusal, for the same reason the planning fallback uses one: this branch must stay
    covered no matter how far the shipped holiday file eventually reaches.
    """
    diff = lb.reconcile_calendar(
        start=DEEP_START,
        end=DEEP_END,
        served=[E2_SESSION],
        no_session=[HOLIDAY_SESSION],
        calendar=refusing_calendar(),
    )
    assert diff.covered is False
    assert diff.agrees is False
    assert diff.undeclared_closures == ()
    assert diff.unexpected_sessions == ()
    assert "makes no claim" in diff.summary()


def test_reconcile_below_the_calendars_floor_makes_no_claim_either() -> None:
    """The same abstention against the shipped file, at the boundary the shipped file has."""
    diff = lb.reconcile_calendar(
        start=PRE_CALENDAR_START,
        end=PRE_CALENDAR_END,
        served=[PRE_CALENDAR_START],
        no_session=[date(2004, 1, 26)],
        calendar=trading_calendar(),
    )
    assert diff.covered is False
    assert diff.undeclared_closures == ()
    assert "makes no claim" in diff.summary()


def test_the_coverage_report_tabulates_what_l0_and_the_journal_know(
    tmp_path: Path, register: SourceRegister
) -> None:
    """The PR artefact, generated offline from the lake rather than typed by hand."""
    transport = transport_for(
        register,
        {E1_SESSION: PRE_ISIN_FIXTURE.read_bytes(), E2_SESSION: ISIN_FIXTURE.read_bytes()},
        not_found=(HOLIDAY_SESSION,),
    )
    driver, l0, journal = acquisition_for(tmp_path, register, transport)
    driver.run([E1_SESSION, E2_SESSION, HOLIDAY_SESSION])

    plan = lb.SessionPlan(
        start=E1_SESSION,
        end=HOLIDAY_SESSION,
        dates=(E1_SESSION, E2_SESSION, HOLIDAY_SESSION),
        basis="explicit",
        note="three dates",
    )
    text = lb.coverage_report(
        plan,
        l0=l0,
        journal=journal,
        register=register,
        calendar=extended_calendar(),
        data_root=tmp_path,
    )

    # Two sessions in L0, one 404, nothing unattempted, and no L1 yet — the row counts are the
    # lake's, not a run's, so they are zero until promotion has actually written something.
    assert "| 2011 | E1/E2 | 3 | 2 | 1 | 0 | 0 | 0 |" in text
    assert HOLIDAY_SESSION.isoformat() in text
    assert "Calendar diff vs nse_holidays.yaml" in text


def test_the_coverage_report_reads_row_counts_off_l1_after_promotion(
    lake: _Lake, register: SourceRegister
) -> None:
    """Promoted and unresolved rows come from the parquet footers, so the artefact is regenerable.

    This is the packet's per-year table in one call: the E2 session contributes 1,502 promoted rows
    and none unresolved, the E1 session contributes 1,503 unresolved and none promoted, and nobody
    had to transcribe either number out of a log.
    """
    promotion_for(lake, register).promote([E1_SESSION, E2_SESSION])

    assert lb.l1_row_counts(E2_SESSION, data_root=lake.root) == (1502, 0)
    assert lb.l1_row_counts(E1_SESSION, data_root=lake.root) == (0, 1503)
    assert lb.l1_row_counts(date(2011, 6, 20), data_root=lake.root) == (0, 0)

    journal = lb.NoSessionJournal(lb.journal_path_for(lake.root), clock=CLOCK)
    plan = lb.SessionPlan(
        start=E1_SESSION,
        end=E2_SESSION,
        dates=(E1_SESSION, E2_SESSION),
        basis="explicit",
        note="the two boundary sessions",
    )
    text = lb.coverage_report(
        plan,
        l0=lake.l0,
        journal=journal,
        register=register,
        calendar=extended_calendar(),
        data_root=lake.root,
    )
    assert "| 2011 | E1/E2 | 2 | 2 | 0 | 0 | 1502 | 1503 |" in text
    assert "unresolved rows" in text


# ── promotion: ISIN era to prices_raw, pre-ISIN era to quarantine and nowhere else ───────────


@dataclass
class _Row:
    state: SyncState
    retryable: bool = True
    error: str | None = None


class _FakeSync:
    """In-memory `SyncStateStore` stand-in, keyed `(source, date)` exactly as the real one is.

    Models only what the promotion driver calls, so an unmodelled call is an `AttributeError` — the
    loud failure a fake should give (B8). Deliberately has no calendar: the promotion path must not
    need one, and if it started calling `mark_gap` this fake would break.
    """

    def __init__(self) -> None:
        self.rows: dict[tuple[str, date], _Row] = {}
        self.commits = 0

    def get(self, source: str, logical_date: date) -> _Row | None:
        return self.rows.get((source, logical_date))

    def begin(self, source: str, logical_date: date) -> _Row:
        row = _Row(SyncState.PENDING)
        self.rows[(source, logical_date)] = row
        return row

    def _advance(self, source: str, logical_date: date, state: SyncState) -> _Row:
        row = self.rows[(source, logical_date)]
        row.state = state
        return row

    def mark_fetched(
        self, source: str, logical_date: date, *, checksum: str, l0_path: str | None = None
    ) -> _Row:
        return self._advance(source, logical_date, SyncState.FETCHED)

    def mark_validated(self, source: str, logical_date: date) -> _Row:
        return self._advance(source, logical_date, SyncState.VALIDATED)

    def mark_normalized(self, source: str, logical_date: date) -> _Row:
        return self._advance(source, logical_date, SyncState.NORMALIZED)

    def mark_published(self, source: str, logical_date: date) -> _Row:
        return self._advance(source, logical_date, SyncState.PUBLISHED)

    def mark_failed(
        self, source: str, logical_date: date, error: str, *, retryable: bool = True
    ) -> _Row:
        row = self.rows.setdefault((source, logical_date), _Row(SyncState.PENDING))
        row.state = SyncState.FAILED
        row.retryable = retryable
        row.error = error
        return row


@dataclass
class _Lake:
    """A scratch lake with the two boundary payloads already in L0, ready to promote."""

    root: Path
    l0: L0Store
    sync: _FakeSync = field(default_factory=_FakeSync)


@pytest.fixture
def lake(tmp_path: Path) -> _Lake:
    store = L0Store(clock=CLOCK, data_root=tmp_path)
    for day, fixture in ((E1_SESSION, PRE_ISIN_FIXTURE), (E2_SESSION, ISIN_FIXTURE)):
        store.put(
            "nse_bhavcopy_legacy",
            day,
            fixture.name,
            fixture.read_bytes(),
            content_type="application/zip",
        )
    return _Lake(root=tmp_path, l0=store)


def promotion_for(lake: _Lake, register: SourceRegister) -> lb.LegacyPromotion:
    def commit() -> None:
        lake.sync.commits += 1

    return lb.LegacyPromotion(
        l0=lake.l0,
        sync=cast("Any", lake.sync),
        commit=commit,
        register=register,
        data_root=lake.root,
    )


def read_partition(root: Path, dataset: str, day: date) -> list[dict[str, Any]]:
    path = partition_path(Layer.L1, dataset, day, data_root=root)
    return cast("list[dict[str, Any]]", pq.read_table(path).to_pylist())


def test_the_isin_era_promotes_to_prices_raw_with_decimal_prices(
    lake: _Lake, register: SourceRegister
) -> None:
    """2011-06-22 extends the price spine, keyed on ISIN, with no adjusted column anywhere."""
    report = promotion_for(lake, register).promote([E2_SESSION])

    assert report.published == 1
    assert report.failed == 0
    assert report.price_rows == 1502
    assert report.unresolved_rows == 0
    assert lake.sync.rows[(NSE_BHAVCOPY, E2_SESSION)].state is SyncState.PUBLISHED
    assert lake.sync.commits == 1

    rows = read_partition(lake.root, PRICES_RAW_DATASET, E2_SESSION)
    assert len(rows) == 1502
    assert all(row["isin"] for row in rows), "no symbol-keyed row may reach L1"
    (reliance,) = [row for row in rows if row["symbol"] == "RELIANCE" and row["series"] == "EQ"]
    assert reliance["isin"] == "INE002A01018"
    # Decimal, exactly — a float round trip through 845.8 is what this rules out.
    assert str(reliance["close"]) == "845.8000"
    assert not any("adj" in name for name in rows[0]), "invariant #3: no adjusted price in L1"


def test_the_pre_isin_era_lands_in_quarantine_and_never_in_prices_raw(
    lake: _Lake, register: SourceRegister
) -> None:
    """The E1 acceptance criterion, read off the parquet rather than described.

    `prices_raw` must have no partition at all for 2011-06-21 — not an empty one — and every one of
    the session's 1,503 rows must be in the quarantine dataset with the reason that says *why*.
    """
    report = promotion_for(lake, register).promote([E1_SESSION])

    assert report.published == 1
    assert report.price_rows == 0, "an E1 session can never contribute a price row"
    assert report.unresolved_rows == 1503
    assert report.unresolved_by_year() == {2011: 1503}

    assert not partition_path(
        Layer.L1, PRICES_RAW_DATASET, E1_SESSION, data_root=lake.root
    ).exists(), "invariant #2: a row with no ISIN must not reach prices_raw"

    quarantined = read_partition(lake.root, PRICES_RAW_QUARANTINE_DATASET, E1_SESSION)
    assert len(quarantined) == 1503
    assert {row["reason"] for row in quarantined} == {PriceQuarantineReason.ISIN_COLUMN_ABSENT}
    assert {row["isin"] for row in quarantined} == {None}, "there was no ISIN column to quote"
    assert {row["trade_date"] for row in quarantined} == {E1_SESSION}
    assert "RELIANCE" in {row["symbol"] for row in quarantined}


def test_the_two_eras_publish_under_different_sync_sources(
    lake: _Lake, register: SourceRegister
) -> None:
    """E1 must not tell the trading interlock that a 2011-06-21 price partition exists."""
    promotion_for(lake, register).promote([E1_SESSION, E2_SESSION])

    assert (NSE_BHAVCOPY, E2_SESSION) in lake.sync.rows
    assert (lb.NSE_BHAVCOPY_PRE_ISIN, E1_SESSION) in lake.sync.rows
    assert (NSE_BHAVCOPY, E1_SESSION) not in lake.sync.rows
    # That the two source names differ is proved statically, not here: both are `Final` literals
    # and mypy rejects comparing them as a non-overlapping equality check.


def test_promotion_is_idempotent_and_skips_a_published_session(
    lake: _Lake, register: SourceRegister
) -> None:
    """Re-running promotion neither rewrites a partition nor re-advances a published row."""
    promotion = promotion_for(lake, register)
    promotion.promote([E1_SESSION, E2_SESSION])
    commits_after_first = lake.sync.commits

    again = promotion.promote([E1_SESSION, E2_SESSION])

    assert again.published == 0
    assert again.skipped == 2
    assert lake.sync.commits == commits_after_first, "a skip commits nothing"


def test_a_session_with_no_payload_is_reported_not_failed(
    lake: _Lake, register: SourceRegister
) -> None:
    """Fetching is acquisition's job; promotion says "not in L0" rather than filing a failure."""
    report = promotion_for(lake, register).promote([date(2011, 6, 20)])

    assert report.missing == 1
    assert report.failed == 0
    assert lake.sync.rows == {}


def test_a_payload_that_will_not_parse_reaches_the_status_api(
    tmp_path: Path, register: SourceRegister
) -> None:
    """A broken source must land as a FAILED sync row, not a log line nobody reads."""
    store = L0Store(clock=CLOCK, data_root=tmp_path)
    store.put(
        "nse_bhavcopy_legacy",
        E2_SESSION,
        "cm22JUN2011bhav.csv.zip",
        b"this is not a zip and not a csv",
        content_type="application/zip",
    )
    lake = _Lake(root=tmp_path, l0=store)

    report = promotion_for(lake, register).promote([E2_SESSION])

    assert report.failed == 1
    row = lake.sync.rows[(NSE_BHAVCOPY, E2_SESSION)]
    assert row.state is SyncState.FAILED
    assert row.retryable is True
    assert row.error is not None and "parse failed" in row.error


def test_the_report_publishes_the_unresolved_count_per_year(
    lake: _Lake, register: SourceRegister
) -> None:
    """The honest bound on how far back the platform reaches, per year, not as one total."""
    report = promotion_for(lake, register).promote([E1_SESSION, E2_SESSION])

    assert report.unresolved_by_year() == {2011: 1503}
    assert report.price_rows_by_year() == {2011: 1502}
    assert "1503 unresolved rows quarantined" in report.summary()


# ── the day kinds the driver never asks the calendar about ───────────────────────────────────


def test_the_extended_calendar_fixture_is_a_real_trading_calendar() -> None:
    """The injected fixture must behave like the file it stands in for, or it proves nothing."""
    calendar = extended_calendar()
    assert calendar.covers(date(2006, 1, 2))
    assert calendar.classify(HOLIDAY_SESSION) is DayKind.HOLIDAY
    assert calendar.classify(date(2011, 6, 22)) is DayKind.SESSION
    assert calendar.classify(date(2011, 6, 25)) is DayKind.WEEKEND
    assert calendar.expected_data_dates(date(2011, 8, 12), date(2011, 8, 19)) == [
        date(2011, 8, 12),
        date(2011, 8, 16),
        date(2011, 8, 17),
        date(2011, 8, 18),
        date(2011, 8, 19),
    ]


def test_the_refusing_calendar_fixture_refuses_by_the_real_guard() -> None:
    """The narrow fixture must fail the way `calendar.py` does, or the fallback tests prove nothing.

    Its refusal has to be a `CalendarCoverageError` raised by `_require_coverage` — not a stub that
    raises something the driver would let escape — and it must still answer normally inside its own
    span, so the branch under test is coverage and nothing else.
    """
    calendar = refusing_calendar()
    assert calendar.covers(date(2016, 6, 1))
    assert not calendar.covers(DEEP_START)
    assert calendar.classify(date(2016, 6, 1)) is DayKind.SESSION
    with pytest.raises(CalendarCoverageError, match="leaves the holiday calendar's coverage"):
        calendar.classify(DEEP_START)


def test_the_driver_module_names_ist_for_its_timestamps() -> None:
    """The journal's `observed_at` is tz-aware on the injected clock (B10), never `now()`."""
    assert CLOCK.now().tzinfo is IST
    record = lb.NoSessionRecord(
        trade_date=E1_SESSION,
        era="E1",
        url="https://example.invalid/x.zip",
        http_status=404,
        observed_at=CLOCK.now(),
    )
    assert record.observed_at.utcoffset() is not None
