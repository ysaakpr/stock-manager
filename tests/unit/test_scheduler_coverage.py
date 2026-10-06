"""Every live source is scheduled or explicitly not — and a stop surfaces on the status surface.

The 2026-10-05 audit's root cause, as tests. The register said `cadence: daily` for the whole
bhavcopy family; `eod_pipeline` was registered but no process ever fired it, its source tuple held
one of the four, the TRI resume check never looked at the series' end, and `/status/sources` called
a source 21 sessions stale `healthy`. Each test below fails if one of those comes back:

* a live register row that no registered job `covers` and `UNSCHEDULED` does not explain;
* a job's declared coverage drifting from what its body actually fetches;
* a job with no `job_run` row reported as anything but NEVER_RAN, or a job whose newest success
  predates its newest due fire reported as anything but OVERDUE;
* a scheduled source behind its lag budget reported `healthy`.

Offline: no database, no network, no scheduler started.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Final

import pytest

from dataplatform.clock import IST
from dataplatform.ingest import daily_capture
from dataplatform.ingest.backfill import SOURCE_SETS
from dataplatform.ingest.daily_snapshot import DEFAULT_SNAPSHOT_SET
from dataplatform.ingest.eod import DAILY_NSE_SOURCES
from dataplatform.ingest.source_register import load as load_register
from dataplatform.scheduler import SchedulerRunner, build_scheduler
from dataplatform.scheduler.health import JobHealthState, LastRuns, assess, last_due_fire
from dataplatform.scheduler.registry import (
    ANNOUNCEMENTS_CAPTURE,
    DAILY_SNAPSHOT,
    EOD_PIPELINE,
    NEWS_CAPTURE,
    NSE_DAILY_CAPTURE,
    SHAREHOLDING_POLL,
    TRI_EVENING,
    TRI_REFRESH,
    UNSCHEDULED,
    Job,
    default_registry,
    lag_budgets,
)
from dataplatform.status.sync_state import SourceStatus, SyncState
from tests.conftest import SettingsLoader

#: Monday 2026-10-05, 20:00 IST — after the 18:30 EOD fire's 45-minute budget has elapsed.
MONDAY_EVENING: Final = datetime(2026, 10, 5, 20, 0, tzinfo=IST)


def _live_register_ids() -> set[str]:
    return {
        source.id
        for source in load_register().sources
        if source.era.end is None and source.cadence != "backfill_only"
    }


def _covered() -> set[str]:
    return {source for job in default_registry() for source in job.covers}


# ── unscheduled ─────────────────────────────────────────────────────────────────────────────


def test_every_live_source_is_scheduled_or_explained() -> None:
    """The guard the audit needed: `cadence: daily` with no job behind it fails the gate."""
    orphans = _live_register_ids() - _covered() - set(UNSCHEDULED)
    assert not orphans, (
        f"live register sources no job covers and UNSCHEDULED does not explain: {sorted(orphans)}"
    )


def test_coverage_and_the_ledger_name_real_sources_and_do_not_overlap() -> None:
    known = {source.id for source in load_register().sources}
    assert _covered() <= known, sorted(_covered() - known)
    assert set(UNSCHEDULED) <= known, sorted(set(UNSCHEDULED) - known)
    both = _covered() & set(UNSCHEDULED)
    assert not both, f"scheduled *and* listed unscheduled: {sorted(both)}"
    for source, reason in UNSCHEDULED.items():
        assert reason.strip(), f"{source} is unscheduled with no reason recorded"


@pytest.mark.parametrize(
    "source", ["nse_bhavcopy_udiff", "nse_sec_bhavdata_full", "bse_bhavcopy_udiff", "nse_pr_bundle"]
)
def test_the_bhavcopy_family_is_scheduled_daily(source: str) -> None:
    """Named outright: the four sources the audit found stopped. A weekly job does not count."""
    daily = [
        job for job in default_registry() if source in job.covers and job.cron.endswith("mon-fri")
    ]
    assert daily, f"{source} has no weekday job"


@pytest.mark.parametrize(
    ("source", "job_name"),
    [("nse_corp_actions", "ca_refresh"), ("bse_corp_actions", "bse_ca_sweep")],
)
def test_the_corporate_action_feeds_are_scheduled_with_a_lag_budget(
    source: str, job_name: str
) -> None:
    """Held back from PR #35 until the lake rebuild; the store stopped at 2026-09-01 meanwhile."""
    registry = default_registry()
    assert source not in UNSCHEDULED
    assert source in registry.get(job_name).covers
    assert source in lag_budgets(registry)


def test_the_eod_jobs_coverage_is_what_its_body_fetches() -> None:
    """`covers` is a claim; this holds it to the source sets the pipeline actually drives."""
    register = load_register()
    session = date(2026, 10, 1)
    fetched = {
        SOURCE_SETS[name].build_request(session, register).fetch_source
        for name in DAILY_NSE_SOURCES
    }
    assert set(EOD_PIPELINE.covers) == fetched | {"nse_pr_bundle"}
    assert EOD_PIPELINE.sync_sources == DAILY_NSE_SOURCES


def test_the_snapshot_jobs_coverage_is_its_snapshot_set() -> None:
    assert set(DAILY_SNAPSHOT.covers) == {spec.source_id for spec in DEFAULT_SNAPSHOT_SET}


# ── the capture jobs (ops-daily-capture, 2026-10-06) ─────────────────────────────────────────

#: Register rows that serve only the latest session: a weekday without a capture is a day of
#: history nothing can recover (their `pit_notes`). Named outright, like the bhavcopy family.
PERISHABLE: Final = ("nse_fii_dii_flows", "nse_bulk_deals", "nse_block_deals")

#: The nine VERIFIED rows that had a parser and had never landed a byte, as of 2026-10-06.
NEVER_FETCHED: Final = (
    "nse_fii_dii_flows",
    "nse_bulk_deals",
    "nse_block_deals",
    "nse_shareholding_pattern",
    "nse_fo_bhavcopy",
    "nse_announcements",
    "bse_announcements",
    "curated_rss",
    "gdelt_v2_event_files",
)


@pytest.mark.parametrize(
    ("job", "sources"),
    [
        (NSE_DAILY_CAPTURE, daily_capture.NSE_DAILY_SOURCES),
        (SHAREHOLDING_POLL, daily_capture.SHAREHOLDING_SOURCES),
        (ANNOUNCEMENTS_CAPTURE, daily_capture.ANNOUNCEMENT_SOURCES),
        (NEWS_CAPTURE, daily_capture.NEWS_SOURCES),
    ],
    ids=lambda value: value.name if isinstance(value, Job) else "",
)
def test_each_capture_jobs_coverage_is_what_its_body_drives(
    job: Job, sources: tuple[str, ...]
) -> None:
    """`covers` is a claim; the body's own source tuple is what it actually fetches."""
    assert job.covers == sources
    assert job.sync_sources == sources


@pytest.mark.parametrize("source", NEVER_FETCHED)
def test_the_never_fetched_sources_are_scheduled_with_a_lag_budget(source: str) -> None:
    budgets = lag_budgets(default_registry())
    assert source not in UNSCHEDULED
    assert source in _covered()
    assert source in budgets


@pytest.mark.parametrize("source", PERISHABLE)
def test_a_perishable_source_is_captured_every_weekday_on_a_one_session_budget(
    source: str,
) -> None:
    jobs = [job for job in default_registry() if source in job.covers]
    assert any(job.cron.endswith("mon-fri") for job in jobs), f"{source} has no weekday job"
    assert lag_budgets(default_registry())[source] == 1


def _hosts_of(job: Job) -> set[str]:
    register = {row.id: row for row in load_register().sources}
    return {register[source].host for source in job.covers if source in register}


def _windows(job: Job, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    trigger = job.trigger(IST)
    windows: list[tuple[datetime, datetime]] = []
    previous: datetime | None = None
    cursor = start
    while True:
        fire = trigger.get_next_fire_time(previous, cursor)
        if fire is None or fire > end:
            return windows
        windows.append((fire, fire + job.timeout))
        previous, cursor = fire, fire + timedelta(seconds=1)


@pytest.mark.parametrize(
    "job",
    [NSE_DAILY_CAPTURE, SHAREHOLDING_POLL, ANNOUNCEMENTS_CAPTURE, NEWS_CAPTURE, TRI_EVENING],
    ids=lambda job: job.name,
)
def test_a_capture_job_never_runs_while_another_job_holds_one_of_its_hosts(job: Job) -> None:
    """A host lease is refused, not queued: an overlap is a capture that silently does not happen.

    Checked over five weeks from a Monday, so the monthly first-Sunday BSE sweep is in the span.
    Hosts come from the register rows each job `covers`.
    """
    start = datetime(2026, 10, 5, 0, 0, tzinfo=IST)
    end = start + timedelta(weeks=5)
    mine = _windows(job, start, end)
    for other in default_registry():
        if other.name == job.name or not (_hosts_of(job) & _hosts_of(other)):
            continue
        for theirs_start, theirs_end in _windows(other, start - timedelta(days=1), end):
            for my_start, my_end in mine:
                assert my_end <= theirs_start or theirs_end <= my_start, (
                    f"{job.name} {my_start:%a %H:%M} overlaps {other.name} "
                    f"{theirs_start:%a %H:%M}-{theirs_end:%H:%M} on "
                    f"{sorted(_hosts_of(job) & _hosts_of(other))}"
                )


def test_the_nse_capture_fires_after_the_evening_publication_and_retries_the_same_night() -> None:
    """20:00 owes tonight's session; 23:00 is the last same-night chance before the files roll."""
    fires = [
        fire
        for fire, _ in _windows(
            NSE_DAILY_CAPTURE,
            MONDAY_EVENING.replace(hour=0),
            MONDAY_EVENING.replace(hour=23, minute=59),
        )
    ]
    assert [fire.hour for fire in fires] == [20, 23]
    assert all(fire.time() >= daily_capture.CAPTURE_CUTOFF for fire in fires)


def test_the_evening_tri_fires_each_weekday_clear_of_the_snapshots_niftyindices_lease() -> None:
    """M13.7: D's TRI the evening of D, never while `daily_snapshot` holds niftyindices.com.

    `daily_snapshot` leases niftyindices.com for the constituent files without listing that source
    in `covers`, so the generic overlap check above cannot see the clash; this one names it. A
    weekend fire would owe nothing new (the Saturday `tri_refresh` is the backstop, unchanged).
    """
    week = MONDAY_EVENING.replace(hour=0)
    fires = [fire for fire, _ in _windows(TRI_EVENING, week, week + timedelta(days=7))]
    assert [(fire.strftime("%a"), fire.hour, fire.minute) for fire in fires] == [
        (day, hour, 50) for day in ("Mon", "Tue", "Wed", "Thu", "Fri") for hour in (19, 20)
    ]
    snapshot = _windows(DAILY_SNAPSHOT, week, week + timedelta(days=7))
    for fire, end in _windows(TRI_EVENING, week, week + timedelta(days=7)):
        for theirs_start, theirs_end in snapshot:
            assert end <= theirs_start or theirs_end <= fire, f"{fire:%a %H:%M} overlaps"
    assert TRI_REFRESH.cron == "0 8 * * sat"
    # The weekday job owes the source daily, so its one-session budget is the one that binds.
    assert lag_budgets(default_registry())["nifty_tri_history"] == 1


def test_the_scheduler_fires_every_registered_job(load_settings: SettingsLoader) -> None:
    """A job in the registry that `build_scheduler` drops would be scheduled only on paper."""
    runner = SchedulerRunner(settings=load_settings(None))
    scheduler = build_scheduler(runner)
    ids = {job.id for job in scheduler.get_jobs()}
    assert set(default_registry().names()) <= ids


# ── silently skipped: job health ─────────────────────────────────────────────────────────────


def test_a_job_that_never_ran_is_never_ran() -> None:
    """`eod_pipeline` on this deployment until the audit: registered, never fired, invisible."""
    health = assess(EOD_PIPELINE, LastRuns(), now=MONDAY_EVENING, timezone=IST)
    assert health.state is JobHealthState.NEVER_RAN
    assert not health.healthy


def test_a_job_whose_last_success_predates_its_due_fire_is_overdue() -> None:
    three_weeks_ago = datetime(2026, 9, 14, 19, 15, tzinfo=IST)
    runs = LastRuns(
        last_state="SUCCEEDED", last_started_at=three_weeks_ago, last_success_at=three_weeks_ago
    )
    health = assess(EOD_PIPELINE, runs, now=MONDAY_EVENING, timezone=IST)
    assert health.state is JobHealthState.OVERDUE
    assert health.due_since == datetime(2026, 10, 5, 18, 30, tzinfo=IST)


def test_a_job_that_ran_at_its_fire_is_ok_and_one_inside_its_budget_is_not_late() -> None:
    fired = datetime(2026, 10, 5, 18, 30, 0, 50_000, tzinfo=IST)
    runs = LastRuns(last_state="SUCCEEDED", last_started_at=fired, last_success_at=fired)
    assert assess(EOD_PIPELINE, runs, now=MONDAY_EVENING, timezone=IST).state is JobHealthState.OK

    # At 18:40 today's fire is still inside its 45-minute budget, so Friday's run is what is owed.
    friday = datetime(2026, 10, 2, 18, 30, 1, tzinfo=IST)
    earlier = LastRuns(last_state="SUCCEEDED", last_started_at=friday, last_success_at=friday)
    at_1840 = datetime(2026, 10, 5, 18, 40, tzinfo=IST)
    assert last_due_fire(EOD_PIPELINE, at_1840, IST) == datetime(2026, 10, 2, 18, 30, tzinfo=IST)
    assert assess(EOD_PIPELINE, earlier, now=at_1840, timezone=IST).state is JobHealthState.OK


def test_a_failed_newest_attempt_is_failing_even_with_an_older_success() -> None:
    runs = LastRuns(
        last_state="FAILED",
        last_started_at=datetime(2026, 10, 5, 18, 30, tzinfo=IST),
        last_error="EodPipelineError: ...",
        last_success_at=datetime(2026, 10, 1, 18, 30, tzinfo=IST),
    )
    health = assess(EOD_PIPELINE, runs, now=MONDAY_EVENING, timezone=IST)
    assert health.state is JobHealthState.FAILING


def test_a_lock_skip_is_judged_by_the_success_behind_it() -> None:
    """SKIPPED_LOCKED means another process ran it; with no success since the fire, still late."""
    skipped = datetime(2026, 10, 5, 18, 30, tzinfo=IST)
    stale = LastRuns(
        last_state="SKIPPED_LOCKED",
        last_started_at=skipped,
        last_success_at=datetime(2026, 9, 1, 18, 30, tzinfo=IST),
    )
    assert assess(EOD_PIPELINE, stale, now=MONDAY_EVENING, timezone=IST).state is (
        JobHealthState.OVERDUE
    )


# ── silently skipped: source lag ─────────────────────────────────────────────────────────────


def _status(*, lag: int | None, budget: int | None) -> SourceStatus:
    return SourceStatus(
        source="nse_bhavcopy",
        last_success_date=date(2026, 9, 1),
        last_success_at=None,
        latest_date=date(2026, 9, 1),
        lag_days=None,
        lag_sessions=lag,
        failure_streak=0,
        last_failure_date=None,
        last_error=None,
        last_failure_retryable=None,
        counts={SyncState.PUBLISHED: 3760},
        max_lag_sessions=budget,
    )


def test_a_scheduled_source_three_weeks_behind_is_not_healthy() -> None:
    """The audit's exact reading: 21 sessions behind, no failures — and it said healthy."""
    stale = _status(lag=21, budget=lag_budgets(default_registry())["nse_bhavcopy"])
    assert stale.overdue
    assert not stale.healthy


def test_a_scheduled_source_one_session_behind_is_healthy() -> None:
    """Before tonight's run, yesterday's success is one session behind — and on time."""
    assert _status(lag=1, budget=1).healthy
    assert not _status(lag=2, budget=1).healthy


def test_an_unmeasurable_lag_on_a_scheduled_source_is_overdue() -> None:
    assert _status(lag=None, budget=1).overdue


def test_an_unscheduled_source_is_not_overdue_by_lag() -> None:
    """No job owes it, so lag alone cannot make it late; its failures still can."""
    assert _status(lag=21, budget=None).healthy


def test_every_daily_price_source_has_a_one_session_budget() -> None:
    budgets = lag_budgets(default_registry())
    for source in ("nse_bhavcopy", "nse_delivery", "bse_bhavcopy"):
        assert budgets[source] == 1, source
    assert timedelta(minutes=45) == EOD_PIPELINE.timeout


# ── the process that fires them ──────────────────────────────────────────────────────────────

REPO: Final = Path(__file__).resolve().parents[2]


def test_a_unit_runs_the_whole_scheduler_and_restarts_it() -> None:
    """The audit's ops root cause: the only unit installed ran `run-once daily_snapshot`.

    A registry is a schedule only if a process fires it. This holds the checked-in unit to running
    `scheduler run` — every registered job — and to coming back when it dies.
    """
    unit = (REPO / "ops/systemd/scheduler.service").read_text()
    lines = {
        line.split("=", 1)[0]: line.split("=", 1)[1] for line in unit.splitlines() if "=" in line
    }
    assert lines["Restart"] == "always"
    assert "Environment=DATA_ROOT=/" in unit and "Environment=SNAPSHOT_EXPECT_LAKE_ROOT=/" in unit
    wrapper = REPO / "ops" / Path(lines["ExecStart"]).name
    assert wrapper.is_file() and os.access(wrapper, os.X_OK)
    assert "-m dataplatform.scheduler run\n" in wrapper.read_text()
    assert "run-once" not in wrapper.read_text()


def test_installing_the_scheduler_retires_the_single_job_timer() -> None:
    script = (REPO / "ops/systemd/install-scheduler.sh").read_text()
    assert "disable --now daily-snapshot.timer" in script
    assert "enable --now scheduler.service" in script
