"""The W2 PR-bundle campaign driver: resume, 404-as-evidence, the hard stops, and the eras (W2).

Six claims are load-bearing enough that a ~4,100-request campaign should not be launched without
them proved offline, and each has a test here that would fail if the claim broke:

* **A second run over an acquired range spends zero requests.** Not "few" — zero, counted off the
  `RecordedTransport`, which is the only number the host's budget cares about. Both halves of
  resume are asserted: a payload already in L0, and a session the journal already proves has no
  bundle.
* **A 404 is data, and it is *this source's* kind of data.** It lands in the evidence journal under
  `NO_BUNDLE_PUBLISHED` and not under bhavcopy's `HOLIDAY_OR_NO_SESSION`, because the plan is
  built from sessions the calendar vouched for: an absence here is a fact about the archive, not
  about the market. It must not touch the error counter, and it must still have its own hard stop.
* **The two hard stops are two counters.** A run of 404s must not stop the campaign at five, and a
  run of real failures must not need twenty. Asserted separately, because one counter serving both
  is the bug.
* **The lake is named before the first request.** The one log line that stops a three-hour campaign
  from filling the wrong `data_root`, asserted to come before any request the transport saw.
* **Nothing is promoted.** No L1 partition, no `corporate_actions`, no `sync_state` — the driver
  takes no database handle at all, and the acquired tree is L0 and the journal and nothing else.
* **An era boundary is found by bisection, not by scanning.** Over a 124-session bracket the
  search costs single-digit requests, lands on the exact session the answer flips at, and refuses
  to report a boundary when the answer flips back.

Offline by construction (B8): the transport is a `RecordedTransport` script over the four frozen
bundle fixtures, and the lake is a `tmp_path`. No socket, no docker, no network.
"""

from __future__ import annotations

import ast
import zipfile
from datetime import date, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any, Final, cast

import pytest
from structlog.testing import capture_logs

from dataplatform.alerts import build_alerter
from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.ingest import pr_bundle_campaign as campaign
from dataplatform.ingest.calendar import (
    CalendarCoverageError,
    Holiday,
    Provenance,
    TradingCalendar,
    trading_calendar,
)
from dataplatform.ingest.fetcher import (
    Fetcher,
    ForbiddenError,
    RecordedResponse,
    RecordedTransport,
    TransportError,
)
from dataplatform.ingest.no_session_journal import HOLIDAY_OR_NO_SESSION, NoSessionJournal
from dataplatform.ingest.nse.pr_bundle import (
    ARCHIVE_START,
    PR_BUNDLE_SOURCE_ID,
    MemberKind,
    PrBundle,
)
from dataplatform.ingest.source_register import SourceRegister
from dataplatform.ingest.source_register import load as load_register
from dataplatform.store.l0 import L0Store
from dataplatform.store.paths import Layer, layer_root

FIXTURES: Final = Path(__file__).resolve().parents[1] / "fixtures" / "nse_pr_bundle"
IX_ERA: Final = FIXTURES / "ix_era" / "PR040110.zip"
CLASSIC: Final = FIXTURES / "classic" / "PR020113.zip"
MCAP_UPPER: Final = FIXTURES / "mcap_upper" / "PR010724.zip"
LOWERCASE: Final = FIXTURES / "lowercase" / "PR040926.zip"

CLOCK: Final = FrozenClock(date(2026, 9, 8))

#: The campaign's range: the pinned archive floor through the last session Phase 1 verified.
CAMPAIGN_START: Final = ARCHIVE_START
CAMPAIGN_END: Final = date(2026, 9, 4)

#: Three consecutive January 2013 sessions — an era whose fixture carries no mcap member.
SESSION_A: Final = date(2013, 1, 2)
SESSION_B: Final = date(2013, 1, 3)
SESSION_C: Final = date(2013, 1, 4)


# ── wiring ───────────────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def register() -> SourceRegister:
    """The checked-in source register — the same URL template production resolves."""
    return load_register()


def url_for(day: date, register: SourceRegister) -> str:
    return campaign.bundle_url(day, register=register)


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
    register: SourceRegister,
    served: dict[date, bytes],
    *,
    not_found: tuple[date, ...] = (),
    failing: dict[date, Exception] | None = None,
) -> RecordedTransport:
    """A transport that serves the given sessions' bytes, 404s some, and breaks on others."""
    script: dict[str, Any] = {}
    for day, body in served.items():
        script[url_for(day, register)] = RecordedResponse(
            status_code=200, body=body, headers={"content-type": "application/zip"}
        )
    for day in not_found:
        script[url_for(day, register)] = RecordedResponse(
            status_code=404, body=b"<html>Not Found</html>", headers={"content-type": "text/html"}
        )
    for day, error in (failing or {}).items():
        script[url_for(day, register)] = error
    return RecordedTransport(cast("Any", script))


def driver_for(
    tmp_path: Path,
    register: SourceRegister,
    transport: RecordedTransport,
    **kwargs: Any,
) -> tuple[campaign.BundleAcquisition, L0Store, NoSessionJournal]:
    """The acquisition driver wired to a scratch lake, plus the lake and journal to assert on."""
    settings = Settings(data_root=tmp_path)
    l0 = L0Store(clock=CLOCK, data_root=settings.data_root)
    journal = NoSessionJournal(
        campaign.journal_path_for(settings.data_root),
        clock=CLOCK,
        evidence=campaign.EVIDENCE_NO_BUNDLE,
    )
    driver = campaign.BundleAcquisition(
        fetcher=fetcher_for(transport, settings, register),
        l0=l0,
        journal=journal,
        register=register,
        **kwargs,
    )
    return driver, l0, journal


def refusing_calendar() -> TradingCalendar:
    """A calendar covering 2016 only — one that refuses every range this driver plans.

    Injected rather than read from `nse_holidays.yaml`, so this stays a test of the driver
    however far that data file grows. W1's suite learned this the hard way: a test pinned to the
    shipped file refusing 2006 went red the day the file was extended back to 2006.
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


def narrow_calendar(*, start: date, end: date, closures: tuple[date, ...] = ()) -> TradingCalendar:
    """A calendar covering exactly one span, with the closures a test needs to distinguish."""
    return TradingCalendar(
        coverage_start=start,
        coverage_end=end,
        provenance=Provenance(
            curated_by="test",
            curated_on=date(2026, 9, 8),
            method="in-memory fixture for one bracket",
            bhavcopy_probes=0,
            known_limitation="carries only the closures this test distinguishes",
        ),
        _holidays={day: Holiday(date=day, name="closed") for day in closures},
        _sources=dict.fromkeys(range(start.year, end.year + 1), "test_fixture"),
    )


# ── the URL, and the floor below which there is nothing ──────────────────────────────────────


def test_the_url_comes_from_the_register_and_matches_the_archives_own_names(
    register: SourceRegister,
) -> None:
    """`PR{DDMMYY}.zip`, filled from the register row — and the two ends Phase 1 verified live."""
    assert url_for(ARCHIVE_START, register).endswith("/PR040110.zip")
    assert url_for(CAMPAIGN_END, register).endswith("/PR040926.zip")
    assert url_for(date(2013, 1, 2), register).endswith("/PR020113.zip")


def test_a_register_that_drifts_from_the_parser_package_is_refused(
    register: SourceRegister,
) -> None:
    """One address, named in one place. A register edit that leaves the readers behind is a defect.

    The driver would otherwise fetch happily from a template the package it hands payloads to
    documents differently, and the mismatch would only surface as a parse failure thousands of
    requests later.
    """
    drifted = register.model_copy(
        update={
            "sources": [
                row.model_copy(update={"url_template": "https://example.invalid/PR{DDMMYY}.zip"})
                if row.id == PR_BUNDLE_SOURCE_ID
                else row
                for row in register.sources
            ]
        }
    )
    with pytest.raises(ValueError, match="must name one address"):
        campaign.bundle_url(SESSION_A, register=drifted)


def test_nothing_below_the_pinned_floor_is_planned_or_addressed(
    register: SourceRegister,
) -> None:
    """2010-01-04 is a measurement, not a bracket, so a range below it is a loud refusal.

    Phase 1 spent nine requests proving nothing older is published. Re-proving that by planning
    2006-2009 would cost ~1,000 requests to learn what the register already records.
    """
    with pytest.raises(ValueError, match="pinned floor"):
        campaign.bundle_url(ARCHIVE_START - timedelta(days=1), register=register)
    with pytest.raises(ValueError, match="archive begins at 2010-01-04"):
        campaign.plan_sessions(date(2006, 1, 2), CAMPAIGN_END, calendar=trading_calendar())


# ── planning ─────────────────────────────────────────────────────────────────────────────────


def test_the_campaign_range_plans_from_the_calendar_and_counts_4124_sessions() -> None:
    """The whole campaign, planned: the calendar covers the archive's life, so it narrows the plan.

    The count is asserted rather than described — it is the request budget the owner signed off,
    and a plan that silently grew by a year of weekends is exactly the drift worth a red test.
    """
    plan = campaign.plan_sessions(CAMPAIGN_START, CAMPAIGN_END, calendar=trading_calendar())

    assert plan.basis == "calendar"
    assert len(plan) == 4124
    assert plan.dates[0] == CAMPAIGN_START
    assert plan.dates[-1] == CAMPAIGN_END
    assert date(2013, 1, 26) not in plan.dates, "Republic Day owes no bundle and costs no request"
    assert "spans the range" in plan.note


def test_the_plan_widens_to_weekdays_when_the_calendar_refuses_the_range() -> None:
    """A calendar too narrow for the range widens the plan and says what an absence stops proving.

    Widening is the only direction a fallback about missing data may go, and the note must record
    that a 404 under this basis no longer distinguishes an unpublished bundle from a closed
    exchange — the distinction this source's whole evidence label rests on.
    """
    refusing = refusing_calendar()
    with pytest.raises(CalendarCoverageError):
        refusing.expected_data_dates(CAMPAIGN_START, CAMPAIGN_END)

    with capture_logs() as entries:
        plan = campaign.plan_sessions(CAMPAIGN_START, CAMPAIGN_END, calendar=refusing)

    assert plan.basis == "weekday"
    assert all(day.weekday() < 5 for day in plan.dates)
    assert len(plan) > len(
        campaign.plan_sessions(CAMPAIGN_START, CAMPAIGN_END, calendar=trading_calendar())
    )
    assert "calendar refused the range" in plan.note
    assert "no longer distinguishes" in plan.note

    (gap,) = [e for e in entries if e["event"] == "pr_bundle_campaign.calendar_gap"]
    assert gap["log_level"] == "warning"
    assert gap["coverage_end"] == "2016-12-31"


def test_a_sampled_plan_spreads_across_the_whole_archive() -> None:
    """`--limit` samples across the span, so a smoke run crosses every format era."""
    plan = campaign.plan_sessions(
        CAMPAIGN_START, CAMPAIGN_END, calendar=trading_calendar(), limit=8
    )
    assert len(plan) == 8
    assert plan.dates[0] == CAMPAIGN_START
    assert plan.dates[-1] == CAMPAIGN_END
    assert len({day.year for day in plan.dates}) >= 6


# ── acquisition, and the resume that makes the second run free ───────────────────────────────


def test_acquisition_stores_bundles_with_a_browser_ua_and_the_nse_referer(
    tmp_path: Path, register: SourceRegister
) -> None:
    """Each bundle lands under its own L0 key, fetched with the headers the archive requires."""
    transport = transport_for(register, {SESSION_A: CLASSIC.read_bytes()})
    driver, l0, _ = driver_for(tmp_path, register, transport)

    report = driver.run([SESSION_A])

    assert report.fetched == 1
    assert report.requests_spent == 1
    assert l0.exists(PR_BUNDLE_SOURCE_ID, SESSION_A, "PR020113.zip")
    ref = l0.ref_for(PR_BUNDLE_SOURCE_ID, SESSION_A, "PR020113.zip")
    assert ref.size_bytes == CLASSIC.stat().st_size
    assert l0.get(ref) == CLASSIC.read_bytes(), "the checksum is re-verified on the way out"

    (request,) = transport.requests
    assert "Mozilla/5.0" in request.headers["User-Agent"]
    assert request.headers["Referer"] == "https://www.nseindia.com/"


def test_a_second_run_over_an_acquired_range_performs_zero_fetches(
    tmp_path: Path, register: SourceRegister
) -> None:
    """The claim the whole campaign's restartability rests on — zero, not few.

    Both halves of resume are exercised in one range: two sessions whose payloads are in L0, and
    one the journal already proves published no bundle. The second run must not touch the host
    for any of the three.
    """
    transport = transport_for(
        register,
        {SESSION_A: CLASSIC.read_bytes(), SESSION_C: CLASSIC.read_bytes()},
        not_found=(SESSION_B,),
    )
    driver, _, journal = driver_for(tmp_path, register, transport)
    sessions = [SESSION_A, SESSION_B, SESSION_C]

    first = driver.run(sessions)
    assert first.fetched == 2
    assert first.no_bundle == 1
    assert first.requests_spent == 3
    assert len(transport.requests) == 3

    # A fresh driver over the same lake — the resume a restarted campaign actually performs.
    resumed, _, resumed_journal = driver_for(tmp_path, register, transport)
    second = resumed.run(sessions)

    assert len(transport.requests) == 3, "the second run must not reach the host at all"
    assert second.requests_spent == 0
    assert second.already_in_l0 == 2
    assert second.known_no_bundle == 1
    assert second.fetched == 0
    assert second.failed == 0
    assert resumed_journal.dates == journal.dates == frozenset({SESSION_B})


def test_a_404_is_evidence_about_the_archive_and_not_about_the_market(
    tmp_path: Path, register: SourceRegister
) -> None:
    """The distinction this source's journal exists to keep: no bundle, not a closed exchange.

    The plan is built from sessions the calendar already vouched for, so an absence cannot mean
    "the exchange was shut" — and recording it under bhavcopy's label would quietly assert exactly
    that, into an append-only file, forever.
    """
    transport = transport_for(register, {}, not_found=(SESSION_A,))
    driver, _, journal = driver_for(tmp_path, register, transport)

    with capture_logs() as entries:
        report = driver.run([SESSION_A])

    assert report.no_bundle == 1
    assert report.failed == 0, "a 404 is never a failure"
    assert not report.hard_stopped
    (record,) = journal.records
    assert record.trade_date == SESSION_A
    assert record.http_status == 404
    assert record.evidence == campaign.EVIDENCE_NO_BUNDLE == "NO_BUNDLE_PUBLISHED"
    assert record.evidence != HOLIDAY_OR_NO_SESSION
    assert record.era is None, "this source's readers sniff format per file; it has no era to log"
    assert record.observed_at.tzinfo is IST, "injected clock, never datetime.now() (B10)"

    (event,) = [e for e in entries if e["event"] == "pr_bundle_campaign.no_bundle"]
    assert event["evidence"] == "NO_BUNDLE_PUBLISHED"
    assert event["progress"] == "1/1"

    # And it is written through, so a restart in another process sees it.
    reread = NoSessionJournal(journal.path, clock=CLOCK, evidence=campaign.EVIDENCE_NO_BUNDLE)
    assert reread.knows(SESSION_A)


def test_a_run_of_404s_does_not_trip_the_error_stop_but_does_trip_its_own(
    tmp_path: Path, register: SourceRegister
) -> None:
    """Two hard stops, two counters. Neither may be the other.

    Six 404s must not stop a run whose error limit is five — otherwise a Diwali week ends the
    campaign. Twenty-one must stop it, because an archive that moved answers 404 for everything
    and would otherwise write years of phantom evidence into an append-only journal.
    """
    days = [ARCHIVE_START + timedelta(days=offset) for offset in range(30)]

    tolerant = transport_for(register, {}, not_found=tuple(days[:6]))
    driver, _, _ = driver_for(tmp_path / "short", register, tolerant, no_session_streak_limit=20)
    short = driver.run(days[:6])
    assert short.no_bundle == 6
    assert not short.hard_stopped, "six unpublished sessions is a holiday week, not a source change"

    long_run = transport_for(register, {}, not_found=tuple(days))
    stopper, _, _ = driver_for(tmp_path / "long", register, long_run, no_session_streak_limit=20)
    stopped = stopper.run(days)
    assert stopped.hard_stopped
    assert stopped.stop_reason is not None
    assert "20 consecutive 404s" in stopped.stop_reason
    assert stopped.no_bundle == 20, "it stops at the limit rather than finishing the range"
    assert len(long_run.requests) == 20


def test_consecutive_unexpected_failures_stop_the_run(
    tmp_path: Path, register: SourceRegister
) -> None:
    """Five broken sessions in a row is a broken host, and the campaign stops rather than grinding.

    The failures stay retryable: nothing is journaled for them, so the next run tries them again.
    A 404 in the middle clears the streak, because a completed round-trip is proof the host is
    answering.
    """
    days = [ARCHIVE_START + timedelta(days=offset) for offset in range(10)]
    transport = transport_for(
        register,
        {},
        failing=dict.fromkeys(days, TransportError("connection reset")),
    )
    driver, _, journal = driver_for(tmp_path, register, transport, error_streak_limit=5)

    report = driver.run(days)

    assert report.hard_stopped
    assert report.stop_reason == "5 consecutive unexpected failures"
    assert report.failed == 5
    assert journal.records == (), "a transport failure is not evidence about the archive"


def test_a_404_between_failures_clears_the_error_streak(
    tmp_path: Path, register: SourceRegister
) -> None:
    """A completed round-trip proves the host is answering, so it resets the failure counter."""
    days = [ARCHIVE_START + timedelta(days=offset) for offset in range(8)]
    broken = {days[0], days[1], days[2], days[4], days[5], days[6]}
    transport = transport_for(
        register,
        {},
        not_found=(days[3], days[7]),
        failing={day: TransportError("connection reset") for day in broken},
    )
    driver, _, _ = driver_for(tmp_path, register, transport, error_streak_limit=4)

    report = driver.run(days)

    assert not report.hard_stopped, "three failures, a 404, then three more is never four in a row"
    assert report.failed == 6
    assert report.no_bundle == 2


def test_a_403_is_a_counted_failure_and_never_evidence(
    tmp_path: Path, register: SourceRegister
) -> None:
    """A refusal is about us, not about the archive's publication history."""
    transport = transport_for(
        register,
        {},
        failing={SESSION_A: ForbiddenError("refused", status_code=403, url="x")},
    )
    driver, _, journal = driver_for(tmp_path, register, transport)

    report = driver.run([SESSION_A])

    assert report.failed == 1
    assert report.no_bundle == 0
    assert journal.records == ()


def test_a_stop_request_ends_the_run_between_sessions(
    tmp_path: Path, register: SourceRegister
) -> None:
    """SIGINT's seam: the flag is read between sessions, so an in-flight payload still lands.

    The remaining sessions are simply not attempted — no failure rows, nothing journaled — so the
    next run picks them up where this one left off.
    """
    transport = transport_for(
        register, {SESSION_A: CLASSIC.read_bytes(), SESSION_B: CLASSIC.read_bytes()}
    )
    driver, l0, journal = driver_for(tmp_path, register, transport)
    # Exactly what the handler does: flip a flag the loop reads between sessions. Here it flips
    # once the first bundle is on disk, which is the state a real Ctrl-C mid-campaign leaves.
    driver = campaign.BundleAcquisition(
        fetcher=fetcher_for(transport, Settings(data_root=tmp_path), register),
        l0=l0,
        journal=journal,
        register=register,
        should_stop=lambda: l0.exists(PR_BUNDLE_SOURCE_ID, SESSION_A, "PR020113.zip"),
    )

    with capture_logs() as entries:
        report = driver.run([SESSION_A, SESSION_B])

    assert report.hard_stopped
    assert report.stop_reason == "stop requested (SIGINT)"
    assert report.fetched == 1
    assert report.failed == 0
    assert not l0.exists(PR_BUNDLE_SOURCE_ID, SESSION_B, "PR030113.zip")
    assert journal.records == (), "an unattempted session is not evidence of anything"
    (stopping,) = [e for e in entries if e["event"] == "pr_bundle_campaign.stopping"]
    assert stopping["remaining"] == 1
    assert stopping["progress"] == "2/2"


def test_the_lake_is_named_in_the_log_before_the_first_request(
    tmp_path: Path, register: SourceRegister
) -> None:
    """The line that prevents a three-hour campaign filling the wrong lake.

    Asserted as an *ordering*: the resolved L0 root must be logged before anything the transport
    saw. A previous campaign on this box built a second lake inside a worktree, and the payloads
    had to be moved by hand afterwards.
    """
    transport = transport_for(register, {SESSION_A: CLASSIC.read_bytes()})
    driver, l0, journal = driver_for(tmp_path, register, transport)

    with capture_logs() as entries:
        driver.run([SESSION_A])

    events = [entry["event"] for entry in entries]
    assert events[0] == "pr_bundle_campaign.lake", "before any fetch, not after four thousand"
    assert events.index("pr_bundle_campaign.lake") < events.index("fetch.stored")
    lake = entries[0]
    assert lake["l0_root"] == str(l0.root) == str(tmp_path / "L0")
    assert lake["data_root"] == str(tmp_path)
    assert lake["journal"] == str(journal.path)
    assert lake["sessions"] == 1


def test_progress_is_readable_off_the_log_alone(tmp_path: Path, register: SourceRegister) -> None:
    """Every per-session line carries `progress=N/TOTAL` — the supervisor's only handle on a
    detached run.
    """
    days = [SESSION_A, SESSION_B, SESSION_C]
    transport = transport_for(
        register,
        {SESSION_A: CLASSIC.read_bytes(), SESSION_C: CLASSIC.read_bytes()},
        not_found=(SESSION_B,),
    )
    driver, _, _ = driver_for(tmp_path, register, transport)

    with capture_logs() as entries:
        driver.run(days)

    assert [entry["progress"] for entry in entries if "progress" in entry] == [
        "1/3",
        "2/3",
        "3/3",
    ], "one line per session, in order, each locating itself in the whole plan"


def test_the_driver_writes_l0_and_the_journal_and_nothing_else(
    tmp_path: Path, register: SourceRegister
) -> None:
    """No L1, no L2, no database handle — promotion is a separate, separately-reviewed task.

    Every bundle member is symbol-keyed with no ISIN and symbols are reused across issuers over
    sixteen years, so these rows cannot be joined under invariant #2 yet. A driver that could
    promote them behind a flag is a driver that will.
    """
    transport = transport_for(register, {SESSION_A: CLASSIC.read_bytes()}, not_found=(SESSION_B,))
    driver, _, _ = driver_for(tmp_path, register, transport)

    driver.run([SESSION_A, SESSION_B])

    assert not layer_root(Layer.L1, data_root=tmp_path).exists()
    assert not layer_root(Layer.L2, data_root=tmp_path).exists()
    # A stored bundle and a journalled absence between them touch two directories, and the whole
    # lake this driver is allowed to write is those two.
    assert sorted(path.name for path in tmp_path.iterdir()) == ["L0", "campaign"]

    # And there is no flag that could: the module imports none of the promotion machinery, so
    # adding promotion here would be a visible import in a reviewed diff rather than a keyword.
    # Asserted over the parsed imports rather than the file's text, because the docstring
    # *explains* what it does not promote and would otherwise trip a grep.
    tree = ast.parse(Path(campaign.__file__).read_text(encoding="utf-8"))
    imported = {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert "dataplatform.store.l1" not in imported
    assert "dataplatform.store.db" not in imported
    assert "dataplatform.status.sync_state" not in imported
    assert "dataplatform.corpactions" not in imported


# ── the era boundaries Phase 1 left as brackets ──────────────────────────────────────────────


def synthetic_bundle(day: date, *, mcap: bool, lowercase: bool = False) -> bytes:
    """A minimal PR bundle for one session, in whichever naming era a test needs.

    The frozen fixtures cannot stand in here: `PrBundle` cross-checks the archive filename
    against the dates its members carry, and rightly refuses a 2013 payload served under a 2024
    name — so a search over a 124-session bracket needs bundles named for those sessions. The
    split is deliberate: this builds the *shape* the search reads (which members exist, and how
    they are named), and `test_the_probe_predicates_read_the_frozen_fixtures_…` proves the same
    predicates read real archive bytes the way the register's history says they do.
    """
    header = b"SERIES,SYMBOL,SECURITY,RECORD_DT,PURPOSE\nEQ,ACME,Acme Ltd,01/01/2024,DIVIDEND\n"
    # The cutover renamed the members and widened their year: `Bc010724.csv` -> `bc01072024.csv`.
    bc_name = f"bc{day:%d%m%Y}.csv" if lowercase else f"Bc{day:%d%m%y}.csv"
    members = {bc_name: header}
    if mcap:
        name = f"{'mcap' if lowercase else 'MCAP'}{day:%d%m%Y}.csv"
        members[name] = b"Trade Date,Symbol,Series,Market Cap(Rs.)\n"
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, body in members.items():
            archive.writestr(name, body)
    return buffer.getvalue()


def bisection_transport(
    register: SourceRegister, sessions: list[date], *, flips_at: date
) -> RecordedTransport:
    """Serve a bundle without an mcap member before `flips_at`, and one with it from there on."""
    return transport_for(
        register,
        {day: synthetic_bundle(day, mcap=day >= flips_at) for day in sessions},
    )


def test_an_era_boundary_is_pinned_by_bisection_in_single_digit_requests(
    tmp_path: Path, register: SourceRegister
) -> None:
    """The shipped mcap probe, over its own bracket, against fixtures that answer honestly.

    The bracket is Phase 1's — 2024-01-02..2024-07-01, ~124 sessions — and the search must land on
    the exact session the answer flips at while spending single-digit requests. A linear scan
    would be just as correct and ~124 requests; that is the whole reason this is a bisection.
    """
    calendar = narrow_calendar(start=date(2024, 1, 1), end=date(2024, 12, 31))
    sessions = calendar.expected_data_dates(date(2024, 1, 2), date(2024, 7, 15))
    flip = sessions[73]
    transport = bisection_transport(register, sessions, flips_at=flip)
    driver, l0, _ = driver_for(tmp_path, register, transport)

    result = campaign.bisect_boundary(
        campaign.MCAP_ARRIVAL, driver=driver, l0=l0, calendar=calendar
    )

    bracket = calendar.expected_data_dates(date(2024, 1, 2), date(2024, 7, 1))
    assert len(bracket) > 100, "a bracket a linear scan would have paid ~124 requests for"
    assert result.unresolved is None
    assert result.first_true == flip
    assert result.last_false == sessions[72]
    assert result.confirmed_next == sessions[74], "the flip is checked to hold past the boundary"
    assert result.requests_spent <= 12, "~log2(124), plus the two ends and one confirmation"
    assert "first true" in result.summary()


def test_the_bisection_probes_go_through_l0_so_a_rerun_is_free(
    tmp_path: Path, register: SourceRegister
) -> None:
    """Each probe is an ordinary acquisition, so the campaign that follows inherits its payloads."""
    calendar = narrow_calendar(start=date(2024, 1, 1), end=date(2024, 12, 31))
    sessions = calendar.expected_data_dates(date(2024, 1, 2), date(2024, 7, 15))
    transport = bisection_transport(register, sessions, flips_at=sessions[40])
    probe = campaign.BoundaryProbe(
        name="mcap_arrival",
        question=campaign.MCAP_ARRIVAL.question,
        predicate=campaign.MCAP_ARRIVAL.predicate,
        after=date(2024, 1, 2),
        until=date(2024, 7, 1),
        monotone_because="synthetic",
    )
    driver, l0, _ = driver_for(tmp_path, register, transport)

    first = campaign.bisect_boundary(probe, driver=driver, l0=l0, calendar=calendar)
    spent = len(transport.requests)
    assert first.requests_spent == spent > 0

    again = campaign.bisect_boundary(probe, driver=driver, l0=l0, calendar=calendar)

    assert again.first_true == first.first_true
    assert again.requests_spent == 0
    assert len(transport.requests) == spent, "every probe was already in L0"


def test_a_boundary_whose_answer_flips_back_is_refused_rather_than_reported(
    tmp_path: Path, register: SourceRegister
) -> None:
    """Bisection assumes monotonicity, so it checks it — and reports NOT PINNED when it fails.

    A question that flips twice inside a bracket makes bisection return *a* flip rather than *the*
    boundary. Publishing that as a pinned era boundary would be asserting a measurement we did not
    make, into a register other tasks read as fact.

    Six sessions, hand-traceable: the search converges on 2024-03-06 (yes) against 2024-03-05
    (no), and the confirmation probe at 2024-03-07 answers no again.
    """
    calendar = narrow_calendar(start=date(2024, 1, 1), end=date(2024, 12, 31))
    sessions = calendar.expected_data_dates(date(2024, 3, 4), date(2024, 3, 11))
    assert len(sessions) == 6
    answers = [False, False, True, False, True, True]
    transport = transport_for(
        register,
        {
            day: synthetic_bundle(day, mcap=answer)
            for day, answer in zip(sessions, answers, strict=True)
        },
    )
    driver, l0, _ = driver_for(tmp_path, register, transport)
    probe = campaign.BoundaryProbe(
        name="flips_back",
        question=campaign.MCAP_ARRIVAL.question,
        predicate=campaign.MCAP_ARRIVAL.predicate,
        after=sessions[0],
        until=sessions[-1],
        monotone_because="deliberately false for this fixture",
    )

    result = campaign.bisect_boundary(probe, driver=driver, l0=l0, calendar=calendar)

    assert result.unresolved is not None
    assert "flipped back at 2024-03-07" in result.unresolved
    assert "NOT PINNED" in result.summary()


def test_the_shipped_brackets_are_the_ones_phase_one_measured() -> None:
    """The two probes address Phase 1's brackets exactly — not a re-guess of them."""
    assert (campaign.MCAP_ARRIVAL.after, campaign.MCAP_ARRIVAL.until) == (
        date(2024, 1, 2),
        date(2024, 7, 1),
    )
    assert (campaign.NAMING_CUTOVER.after, campaign.NAMING_CUTOVER.until) == (
        date(2025, 10, 1),
        date(2025, 11, 3),
    )


def test_the_probe_predicates_read_the_frozen_fixtures_the_way_the_eras_describe() -> None:
    """Each era's fixture answers its boundary question the way the register's history says."""
    with PrBundle(CLASSIC.read_bytes(), filename="PR020113.zip") as classic:
        assert not campaign.MCAP_ARRIVAL.predicate(classic)
        assert not campaign.NAMING_CUTOVER.predicate(classic)
    with PrBundle(MCAP_UPPER.read_bytes(), filename="PR010724.zip") as upper:
        assert campaign.MCAP_ARRIVAL.predicate(upper), "2024-07-01 carries mcap"
        assert not campaign.NAMING_CUTOVER.predicate(upper), "and still names it MCAP…"
    with PrBundle(LOWERCASE.read_bytes(), filename="PR040926.zip") as lower:
        assert campaign.MCAP_ARRIVAL.predicate(lower)
        assert campaign.NAMING_CUTOVER.predicate(lower), "…until the lowercase era"
    with PrBundle(IX_ERA.read_bytes(), filename="PR040110.zip") as ix:
        assert ix.has(MemberKind.IX)
        assert not campaign.MCAP_ARRIVAL.predicate(ix)


# ── the coverage artefact ────────────────────────────────────────────────────────────────────


def test_the_report_reads_the_lake_and_names_what_is_not_yet_attempted(
    tmp_path: Path, register: SourceRegister
) -> None:
    """Offline, regenerable, and honest about the gap — no promoted-rows column, because there
    is no promotion.
    """
    transport = transport_for(
        register,
        {SESSION_A: CLASSIC.read_bytes()},
        not_found=(SESSION_B,),
    )
    driver, l0, journal = driver_for(tmp_path, register, transport)
    driver.run([SESSION_A, SESSION_B])

    plan = campaign.SessionPlan(
        start=SESSION_A,
        end=SESSION_C,
        dates=(SESSION_A, SESSION_B, SESSION_C),
        basis="explicit",
        note="three sessions",
    )
    text = campaign.coverage_report(plan, l0=l0, journal=journal, register=register)

    assert "| 2013 | 3 | 1 | 1 | 1 |" in text
    assert SESSION_B.isoformat() in text
    assert "nothing here is promoted to L1" in text
    assert "promoted rows" not in text
