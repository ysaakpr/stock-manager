"""M13.2 acceptance against real tables: one page per onset, none on repeat, one on resolution.

`tests/unit/test_alert_triggers.py` proves the evaluators and the diff in memory; this module
proves the wiring — that the streak comes from real `sync_state` rows, red from real
`quality_flag` rows, a failed job from real `job_run` rows, and that "already told them" is held
in `alert_condition` (0013), so a second tick from a *fresh* alerter — a restarted scheduler —
still pages nothing.

Runs against a scratch database created for the session and dropped afterwards; each test's
connection is rolled back. Needs the docker postgres (`make up`); skips loudly without it.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest

from dataplatform import alert_triggers
from dataplatform.alert_triggers import (
    CALENDAR_KEY,
    TickReport,
    TriggerEvaluationError,
    registry_retry_deadline,
    run_failure_alerts,
    run_failure_alerts_job,
)
from dataplatform.alerts import BaseAlerter, Severity
from dataplatform.clock import IST, FrozenClock
from dataplatform.config import AlertProvider, Settings
from dataplatform.ingest.calendar import CalendarDataError, TradingCalendar
from dataplatform.scheduler.registry import JobContext, default_registry
from dataplatform.store.db import Connection, connect, connection, with_dbname
from dataplatform.store.migrate import migrate

pytestmark = pytest.mark.integration

SCRATCH_DB = f"trading_m13_2_failure_alerts_{os.getpid()}"

#: Inside the calendar's 60-day lead window (coverage ends 2026-12-31): 46 days out.
BROKEN_NOW = datetime(2026, 11, 16, 9, 0, tzinfo=IST)
#: Outside it: 86 days out, the day this task was written.
HEALTHY_NOW = datetime(2026, 10, 6, 9, 0, tzinfo=IST)

SOURCE = "nse_bhavcopy"
STREAK_KEY = f"ingest:{SOURCE}:failed_streak"
QUALITY_KEY = "quality:price_spike:red"
JOB_KEY = "job:eod_pipeline:failed"


class RecordingAlerter(BaseAlerter):
    """A real `BaseAlerter` whose wire is a list; zero window, so silence is the ledger's doing."""

    channel = "test"

    def __init__(self, clock: FrozenClock) -> None:
        super().__init__(clock=clock, dedup_window=timedelta(0))
        self.sent: list[tuple[Severity, str, str, str]] = []

    def _deliver(self, severity: Severity, title: str, body: str, dedup_key: str) -> None:
        self.sent.append((severity, title, body, dedup_key))

    @property
    def keys(self) -> list[str]:
        return [key for *_, key in self.sent]


def _settings_for(dbname: str) -> Settings:
    return Settings(database_url=with_dbname(Settings().database_url, dbname))


@pytest.fixture(scope="session")
def scratch_settings() -> Iterator[Settings]:
    admin = _settings_for("postgres")
    try:
        conn = connect(admin, autocommit=True)
    except psycopg.OperationalError as error:  # pragma: no cover - environment, not logic
        pytest.skip(f"postgres is not reachable — run `make up` first: {error}")
    try:
        conn.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{SCRATCH_DB}"')
    finally:
        conn.close()

    settings = _settings_for(SCRATCH_DB)
    migrate(settings, clock=FrozenClock(HEALTHY_NOW))
    yield settings

    conn = connect(admin, autocommit=True)
    try:
        conn.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)')
    finally:
        conn.close()


@pytest.fixture
def conn(scratch_settings: Settings) -> Iterator[Connection]:
    with connection(scratch_settings) as live:
        try:
            yield live
        finally:
            live.rollback()


def _sync(conn: Connection, day: date, state: str, error: str | None = None) -> None:
    conn.execute(
        "INSERT INTO sync_state (source, unit, logical_date, state, attempts, retryable, "
        "last_error, updated_at) VALUES (%s, '', %s, %s, 1, true, %s, %s)",
        (SOURCE, day, state, error, BROKEN_NOW),
    )


def _flag(conn: Connection, severity: str = "ERROR") -> None:
    conn.execute(
        "INSERT INTO quality_flag (logical_date, check_name, severity, raised_at) "
        "VALUES (%s, 'price_spike', %s, %s)",
        (date(2026, 11, 13), severity, BROKEN_NOW),
    )


def _job_run(
    conn: Connection,
    state: str,
    started: datetime,
    job: str = "eod_pipeline",
    *,
    retry_pending: bool = False,
) -> None:
    finished = None if state == "RUNNING" else started + timedelta(minutes=1)
    error = "EodPipelineError: postgresql://u:hunter2@db/x refused" if state == "FAILED" else None
    conn.execute(
        "INSERT INTO job_run (run_id, job_name, state, instance, started_at, finished_at, error, "
        "retry_pending) VALUES (%s, %s, %s, 'test:1', %s, %s, %s, %s)",
        (uuid4(), job, state, started, finished, error, retry_pending),
    )


def _tick(
    conn: Connection, settings: Settings, now: datetime
) -> tuple[TickReport, RecordingAlerter]:
    clock = FrozenClock(now)
    alerter = RecordingAlerter(clock)
    report = run_failure_alerts(
        conn,
        settings=settings,
        clock=clock,
        alerter=alerter,
        job_names=default_registry().names(),
    )
    assert report.ok, report.errors
    return report, alerter


def _break_everything(conn: Connection) -> None:
    _sync(conn, date(2026, 11, 9), "PUBLISHED")
    for day in (date(2026, 11, 10), date(2026, 11, 11), date(2026, 11, 12)):
        _sync(conn, day, "FAILED", error="HTTP 503 from nsearchives")
    _flag(conn)
    _job_run(conn, "SUCCEEDED", BROKEN_NOW - timedelta(days=2))
    _job_run(conn, "FAILED", BROKEN_NOW - timedelta(days=1))


def test_a_healthy_platform_pages_nothing(conn: Connection, scratch_settings: Settings) -> None:
    """The inverted-comparison guard against real rows: green data, green jobs, calendar far off."""
    _sync(conn, date(2026, 10, 5), "PUBLISHED")
    _flag(conn, severity="WARN")  # WARN is not red
    _job_run(conn, "SUCCEEDED", HEALTHY_NOW - timedelta(hours=1))

    report, alerter = _tick(conn, scratch_settings, HEALTHY_NOW)
    assert alerter.sent == []
    assert report.opened == report.resolved == report.still_open == []
    assert conn.execute("SELECT count(*) FROM alert_condition").fetchone() == (0,)


def test_each_trigger_pages_once_at_onset_and_never_on_repeat(
    conn: Connection, scratch_settings: Settings
) -> None:
    _break_everything(conn)

    first, alerter = _tick(conn, scratch_settings, BROKEN_NOW)
    assert sorted(alerter.keys) == sorted([STREAK_KEY, QUALITY_KEY, CALENDAR_KEY, JOB_KEY])
    assert sorted(first.opened) == sorted(alerter.keys)
    for _, title, body, _ in alerter.sent:
        assert "hunter2" not in body and "hunter2" not in title

    # A fresh alerter is a restarted scheduler: its in-process window is empty, so silence here
    # can only come from the persisted ledger.
    for minutes in (15, 30, 45):
        again, restarted = _tick(conn, scratch_settings, BROKEN_NOW + timedelta(minutes=minutes))
        assert restarted.sent == [], "an open condition paged again"
        assert sorted(again.still_open) == sorted(first.opened)

    open_rows = conn.execute(
        "SELECT dedup_key, last_seen_at FROM alert_condition WHERE resolved_at IS NULL"
    ).fetchall()
    assert {row[0] for row in open_rows} == set(first.opened)
    assert all(row[1] == BROKEN_NOW + timedelta(minutes=45) for row in open_rows)


def test_a_longer_streak_is_not_a_new_onset(conn: Connection, scratch_settings: Settings) -> None:
    _break_everything(conn)
    _tick(conn, scratch_settings, BROKEN_NOW)
    _sync(conn, date(2026, 11, 13), "FAILED", error="HTTP 503 again")
    _, alerter = _tick(conn, scratch_settings, BROKEN_NOW + timedelta(hours=1))
    assert alerter.sent == []


def test_clearing_sends_one_resolution_per_condition(
    conn: Connection, scratch_settings: Settings
) -> None:
    _break_everything(conn)
    _tick(conn, scratch_settings, BROKEN_NOW)

    _sync(conn, date(2026, 11, 13), "PUBLISHED")  # one success ends the streak
    conn.execute(
        "UPDATE quality_flag SET resolved = true, resolved_at = %s, resolution = 'fixed'",
        (BROKEN_NOW,),
    )
    _job_run(conn, "SUCCEEDED", BROKEN_NOW + timedelta(minutes=5))

    later = BROKEN_NOW + timedelta(minutes=15)
    report, alerter = _tick(conn, scratch_settings, later)
    assert sorted(report.resolved) == sorted([STREAK_KEY, QUALITY_KEY, JOB_KEY])
    assert all(severity is Severity.INFO for severity, *_ in alerter.sent)
    assert sorted(alerter.keys) == sorted(f"{key}:resolved" for key in report.resolved)
    assert report.still_open == [CALENDAR_KEY], "the calendar is still inside its window"

    _, quiet = _tick(conn, scratch_settings, later + timedelta(minutes=15))
    assert quiet.sent == [], "a resolution is sent once"


def test_a_skipped_or_running_attempt_does_not_hide_a_failure(
    conn: Connection, scratch_settings: Settings
) -> None:
    """Neither a lock-skip nor an in-flight retry is an outcome; the FAILED run is still newest."""
    _job_run(conn, "FAILED", HEALTHY_NOW - timedelta(hours=3))
    _job_run(conn, "SKIPPED_LOCKED", HEALTHY_NOW - timedelta(hours=2))
    _job_run(conn, "RUNNING", HEALTHY_NOW - timedelta(minutes=1))
    _job_run(conn, "FAILED", HEALTHY_NOW - timedelta(hours=1), job="retired_job_not_registered")

    report, alerter = _tick(conn, scratch_settings, HEALTHY_NOW)
    assert alerter.keys == [JOB_KEY]
    assert report.opened == [JOB_KEY]


# ── the scheduler job body itself ────────────────────────────────────────────────────────────


@pytest.fixture
def committed(scratch_settings: Settings) -> Iterator[Connection]:
    """An autocommit connection for the job-wrapper tests, which open their own connection and so
    only see committed rows. Everything it (or the job) wrote is deleted afterwards, so the
    rollback-isolated tests above still start from empty tables."""
    with connection(scratch_settings, autocommit=True) as live:
        try:
            yield live
        finally:
            for table in ("alert_condition", "job_run", "quality_flag", "sync_state"):
                live.execute(f"DELETE FROM {table}")


def _context(settings: Settings, now: datetime) -> JobContext:
    """Pinned to the log channel: a developer `.env` selecting telegram must never make a test
    page a real phone."""
    log_only = settings.model_copy(update={"alert_provider": AlertProvider.LOG})
    return JobContext(
        job_name="failure_alerts", run_id=uuid4(), clock=FrozenClock(now), settings=log_only
    )


def _open_keys(conn: Connection) -> set[str]:
    rows = conn.execute("SELECT dedup_key FROM alert_condition WHERE resolved_at IS NULL")
    return {str(row[0]) for row in rows.fetchall()}


def test_the_job_body_pages_through_the_configured_alerter(
    committed: Connection, scratch_settings: Settings
) -> None:
    """`run_failure_alerts_job` end to end: real wiring, the log alerter, the ledger."""
    _break_everything(committed)
    run_failure_alerts_job(_context(scratch_settings, BROKEN_NOW))
    assert _open_keys(committed) == {STREAK_KEY, QUALITY_KEY, CALENDAR_KEY, JOB_KEY}

    # A second tick from a new process is silent and still green.
    run_failure_alerts_job(_context(scratch_settings, BROKEN_NOW + timedelta(minutes=15)))
    assert _open_keys(committed) == {STREAK_KEY, QUALITY_KEY, CALENDAR_KEY, JOB_KEY}


def test_a_malformed_holiday_file_still_lets_the_other_triggers_page(
    committed: Connection, scratch_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The review's blocking case through the job body: a bad YAML fails one trigger, loudly."""

    def malformed() -> TradingCalendar:
        raise CalendarDataError("nse_holidays.yaml does not match the holiday schema")

    monkeypatch.setattr(alert_triggers, "load_calendar", malformed)
    _break_everything(committed)

    with pytest.raises(TriggerEvaluationError, match="calendar_expiry: not evaluated"):
        run_failure_alerts_job(_context(scratch_settings, BROKEN_NOW))
    assert _open_keys(committed) == {STREAK_KEY, QUALITY_KEY, JOB_KEY}


def test_tri_evening_pages_only_when_its_last_fire_fails(
    conn: Connection, scratch_settings: Settings
) -> None:
    """#73 and #65 together, against real rows: the 19:50 not-yet-published failure is held by the
    production deadline (the registry's own cron), and the 21:30 one pages once."""
    evening = datetime(2026, 11, 3, tzinfo=IST)  # a Tuesday inside calendar coverage
    deadline = registry_retry_deadline(default_registry())

    def tick_at(hour: int, minute: int) -> RecordingAlerter:
        clock = FrozenClock(evening.replace(hour=hour, minute=minute))
        alerter = RecordingAlerter(clock)
        report = run_failure_alerts(
            conn,
            settings=scratch_settings,
            clock=clock,
            alerter=alerter,
            job_names=["tri_evening"],
            retry_deadline=deadline,
        )
        assert report.ok, report.errors
        return alerter

    _job_run(conn, "FAILED", evening.replace(hour=19, minute=50), "tri_evening", retry_pending=True)
    assert [key for key in tick_at(20, 0).keys if "tri_evening" in key] == []

    _job_run(conn, "FAILED", evening.replace(hour=21, minute=30), "tri_evening", retry_pending=True)
    paged = [key for key in tick_at(21, 45).keys if "tri_evening" in key]
    assert paged == ["job:tri_evening:failed"]
