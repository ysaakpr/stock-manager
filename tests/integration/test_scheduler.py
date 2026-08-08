"""M0.6 acceptance: a job runs once, under a lock, and says so — even when it raises.

Everything runs against a scratch database created for the session and dropped afterwards, for
the same reason `test_migrations.py` does: `job_run` accumulates rows, and a test that asserts on
"the runs of this job" must not be reading a developer's real history.

The lock test uses two threads with two connections rather than a mocked lock. A Postgres advisory
lock is session-scoped, so two connections behave exactly as two processes do — which is the
property the acceptance criterion is about, and the one a mock would assume rather than prove.

Needs the docker postgres (`make up`).
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from datetime import datetime, timedelta
from uuid import UUID

import psycopg
import pytest
from fastapi.testclient import TestClient

from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.scheduler import (
    ALIVE,
    DEFAULT_SCHEDULER_ID,
    TICK_JOB_ID,
    Job,
    JobContext,
    JobNotRegisteredError,
    JobRegistry,
    JobState,
    SchedulerRunner,
    build_scheduler,
    default_registry,
    read_heartbeat,
)
from dataplatform.status.api import app, clock_source, settings_source
from dataplatform.store.db import Connection, connect, connection, with_dbname
from dataplatform.store.migrate import migrate

pytestmark = pytest.mark.integration

#: Created at the start of every session and dropped afterwards, so the run history starts empty.
#: Suffixed with the pid because two pytest sessions do run at once here — an autonomous build
#: wave has several agents in the suite simultaneously — and a fixed name means one session's
#: `DROP DATABASE ... WITH (FORCE)` deletes the database another is mid-test against. A session
#: killed outright leaks one empty database; that is strictly better than a flaky suite.
SCRATCH_DB = f"trading_m0_6_scheduler_{os.getpid()}"

#: The instant the runner's clock is frozen at, so recorded timestamps are asserted, not observed.
NOW = datetime(2026, 8, 8, 18, 30, tzinfo=IST)

#: A cron that is valid but will not fire during a test run.
NEVER_SOON = "0 4 1 1 *"


def _settings_for(dbname: str) -> Settings:
    """Settings for the configured server with a different database selected."""
    return Settings(database_url=with_dbname(Settings().database_url, dbname))


@pytest.fixture(scope="session")
def scratch_settings() -> Iterator[Settings]:
    """An empty, migrated scratch database for the session; dropped afterwards."""
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
    migrate(settings, clock=FrozenClock(NOW))
    yield settings

    conn = connect(admin, autocommit=True)
    try:
        conn.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)')
    finally:
        conn.close()


@pytest.fixture
def conn(scratch_settings: Settings) -> Iterator[Connection]:
    """A read connection to the scratch database, closed at the end of the test."""
    with connection(scratch_settings) as live:
        yield live


def _job(name: str, fn: object, *, timeout: timedelta = timedelta(minutes=5)) -> Job:
    """A registered job wrapping `fn`, on a cron that will not fire while the test runs."""
    return Job(name=name, cron=NEVER_SOON, fn=fn, timeout=timeout)  # type: ignore[arg-type]


def _runner(settings: Settings, *jobs: Job) -> SchedulerRunner:
    """A runner over exactly `jobs`, with a frozen clock so recorded instants are deterministic."""
    return SchedulerRunner(
        JobRegistry(jobs), settings=settings, clock=FrozenClock(NOW), instance="test:1"
    )


def _run_row(conn: Connection, run_id: UUID) -> tuple[object, ...]:
    row = conn.execute(
        "SELECT job_name, state, instance, started_at, finished_at, error "
        "FROM job_run WHERE run_id = %s",
        (run_id,),
    ).fetchone()
    assert row is not None, f"no job_run row for {run_id}"
    return row


# ── acceptance 1: run-once executes a job and writes a heartbeat visible via /health ─────────


def test_run_once_executes_the_job_and_records_it(
    scratch_settings: Settings, conn: Connection
) -> None:
    seen: list[JobContext] = []
    runner = _runner(scratch_settings, _job("records_it", seen.append))

    run = runner.run_once("records_it")

    assert [context.job_name for context in seen] == ["records_it"]
    assert seen[0].run_id == run.run_id, "the job sees the run id its attempt was recorded under"
    assert seen[0].clock.now() == NOW, "the job gets the injected clock, not a fresh one"
    assert run.state is JobState.SUCCEEDED and run.succeeded and run.error is None

    job_name, state, instance, started_at, finished_at, error = _run_row(conn, run.run_id)
    assert (job_name, state, instance) == ("records_it", "SUCCEEDED", "test:1")
    assert (started_at, finished_at, error) == (NOW, NOW, None)


def test_run_once_writes_a_heartbeat_that_health_reports(scratch_settings: Settings) -> None:
    """Acceptance 1, end to end: the run moves the heartbeat and `/health` shows that beat."""
    runner = _runner(scratch_settings, _job("beats", lambda context: None))
    run = runner.run_once("beats")

    beat = read_heartbeat(scratch_settings)
    assert beat is not None, "the run wrote no heartbeat"
    assert (beat.name, beat.state, beat.job) == (DEFAULT_SCHEDULER_ID, "SUCCEEDED", "beats")
    assert (beat.run_id, beat.beat_at) == (run.run_id, NOW)

    # The API measures staleness with its own injected clock (B10), so freezing it two seconds
    # after the beat makes the age assertion exact instead of "whatever the wall clock said".
    app.dependency_overrides[settings_source] = lambda: scratch_settings
    app.dependency_overrides[clock_source] = lambda: FrozenClock(NOW + timedelta(seconds=2))
    try:
        with TestClient(app) as client:
            response = client.get("/health")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "OK"
    assert payload["scheduler"]["state"] == "OK"
    assert payload["scheduler"]["scheduler_id"] == DEFAULT_SCHEDULER_ID
    assert datetime.fromisoformat(payload["scheduler"]["last_beat_at"]) == NOW
    assert payload["scheduler"]["age_seconds"] == 2.0


def test_the_idle_tick_beats_without_a_job(scratch_settings: Settings) -> None:
    """A day with nothing due still has to prove the process is up — the tick's whole purpose."""
    runner = _runner(scratch_settings, _job("stale_job", lambda context: None))
    runner.run_once("stale_job")

    runner.beat()

    beat = read_heartbeat(scratch_settings)
    assert beat is not None
    assert (beat.name, beat.state, beat.beat_at) == (DEFAULT_SCHEDULER_ID, ALIVE, NOW)
    assert (beat.job, beat.run_id) == (None, None), "the process tick is not a job run"


def test_the_run_once_cli_runs_the_placeholder_eod_job(scratch_settings: Settings) -> None:
    """The §8.1 invocation verbatim, as a real process — argv parsing, wiring and exit code."""
    assert "eod_pipeline" in default_registry(), "M0.6 must register the placeholder EOD job"

    completed = subprocess.run(
        [sys.executable, "-m", "dataplatform.scheduler", "run-once", "eod_pipeline"],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "DATABASE_URL": scratch_settings.database_url},
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "eod_pipeline SUCCEEDED" in completed.stdout

    beat = read_heartbeat(scratch_settings, name=DEFAULT_SCHEDULER_ID)
    assert beat is not None and (beat.state, beat.job) == ("SUCCEEDED", "eod_pipeline")
    assert beat.instance != "test:1", "the CLI ran in its own process, with its own instance id"


def test_an_unknown_job_is_refused_by_name(scratch_settings: Settings) -> None:
    runner = _runner(scratch_settings, _job("the_only_one", lambda context: None))
    with pytest.raises(JobNotRegisteredError, match="the_only_one"):
        runner.run_once("not_registered")


# ── acceptance 2: the singleton lock prevents a second concurrent run ────────────────────────


def test_the_singleton_lock_prevents_a_second_concurrent_run(
    scratch_settings: Settings, conn: Connection
) -> None:
    """Two runners, two connections, one job — the second must not enter the function body.

    Two threads rather than two processes because a Postgres advisory lock is scoped to the
    session, so a second connection is indistinguishable from a second process to the lock; and
    the barrier makes the overlap real instead of hoping the scheduling happens to interleave.
    """
    inside = threading.Event()
    release = threading.Event()
    entries: list[str] = []

    def slow(context: JobContext) -> None:
        entries.append(context.job_name)
        inside.set()
        assert release.wait(timeout=10), "the second run never returned"

    first = _runner(scratch_settings, _job("only_once", slow))
    second = SchedulerRunner(
        JobRegistry([_job("only_once", slow)]),
        settings=scratch_settings,
        clock=FrozenClock(NOW),
        instance="test:2",
    )

    holder = threading.Thread(target=first.run_once, args=("only_once",), daemon=True)
    holder.start()
    try:
        assert inside.wait(timeout=10), "the first run never started"
        blocked = second.run_once("only_once")
    finally:
        release.set()
        holder.join(timeout=10)

    assert entries == ["only_once"], "the locked-out run executed the job body anyway"
    assert blocked.state is JobState.SKIPPED_LOCKED

    job_name, state, instance, started_at, finished_at, error = _run_row(conn, blocked.run_id)
    assert (job_name, state, instance) == ("only_once", "SKIPPED_LOCKED", "test:2")
    assert (started_at, finished_at, error) == (NOW, NOW, None)


def test_the_lock_is_released_so_the_next_run_can_take_it(scratch_settings: Settings) -> None:
    """A lock that is never released turns one crashed run into a permanently dead job."""
    runner = _runner(scratch_settings, _job("sequential", lambda context: None))
    states = [runner.run_once("sequential").state for _ in range(3)]
    assert states == [JobState.SUCCEEDED] * 3


def test_a_failing_run_still_releases_the_lock(scratch_settings: Settings) -> None:
    def explode(context: JobContext) -> None:
        raise RuntimeError("boom")

    registry = JobRegistry([_job("fails_then_frees", explode)])
    runner = SchedulerRunner(
        registry, settings=scratch_settings, clock=FrozenClock(NOW), instance="test:1"
    )
    assert runner.run_once("fails_then_frees").state is JobState.FAILED
    assert runner.run_once("fails_then_frees").state is JobState.FAILED, "the lock was not freed"


def test_two_different_jobs_do_not_block_each_other(scratch_settings: Settings) -> None:
    """The lock is per job name; a single global lock would serialise the whole platform."""
    inside = threading.Event()
    release = threading.Event()

    def slow(context: JobContext) -> None:
        inside.set()
        assert release.wait(timeout=10)

    runner = _runner(
        scratch_settings, _job("slow_one", slow), _job("quick_one", lambda context: None)
    )
    holder = threading.Thread(target=runner.run_once, args=("slow_one",), daemon=True)
    holder.start()
    try:
        assert inside.wait(timeout=10)
        assert runner.run_once("quick_one").state is JobState.SUCCEEDED
    finally:
        release.set()
        holder.join(timeout=10)


# ── acceptance 3: a raising job is logged, recorded failed, and does not kill the scheduler ──


def test_a_raising_job_is_recorded_failed_and_does_not_propagate(
    scratch_settings: Settings, conn: Connection
) -> None:
    def explode(context: JobContext) -> None:
        raise ValueError("the parser did not recognise the header row")

    runner = _runner(scratch_settings, _job("explodes", explode))

    run = runner.run_once("explodes")  # must not raise: that is what keeps the scheduler alive

    assert run.state is JobState.FAILED and not run.succeeded
    assert run.error == "ValueError: the parser did not recognise the header row"

    job_name, state, _instance, _started, finished_at, error = _run_row(conn, run.run_id)
    assert (job_name, state, finished_at) == ("explodes", "FAILED", NOW)
    assert error == run.error, "the error text must survive into the row, not just the log"

    beat = read_heartbeat(scratch_settings)
    assert beat is not None and (beat.state, beat.job) == ("FAILED", "explodes")


def test_the_scheduler_keeps_running_after_a_job_raises(scratch_settings: Settings) -> None:
    """The failure never reaches APScheduler's executor, so it cannot take the process down.

    Invoking the registered callable exactly as the executor would is the strongest form of this
    that does not involve waiting for a cron to fire: if it returns rather than raises, no job
    exception can reach the scheduler thread.
    """

    def explode(context: JobContext) -> None:
        raise RuntimeError("boom")

    runner = _runner(
        scratch_settings, _job("bad_job", explode), _job("good_job", lambda context: None)
    )
    # Built, never started: nothing here waits for a cron, and an unstarted scheduler has no
    # thread to shut down. What is under test is the callable the executor would invoke.
    scheduler = build_scheduler(runner)
    registered = {job.id: job for job in scheduler.get_jobs()}
    assert set(registered) == {"bad_job", "good_job", TICK_JOB_ID}

    bad = registered["bad_job"]
    assert bad.func(*bad.args).state is JobState.FAILED  # exactly what the executor calls

    good = registered["good_job"]
    assert good.func(*good.args).state is JobState.SUCCEEDED, "one bad job poisoned the runner"


def test_the_scheduler_ticks_the_heartbeat_in_the_exchange_timezone(
    scratch_settings: Settings,
) -> None:
    """A container in UTC must still fire the EOD job after the Indian close."""
    runner = _runner(scratch_settings, _job("timed", lambda context: None))
    scheduler = build_scheduler(runner, heartbeat_interval=timedelta(seconds=5))
    assert scheduler.get_job(TICK_JOB_ID).trigger.interval == timedelta(seconds=5)
    assert str(scheduler.get_job("timed").trigger.timezone) == "Asia/Kolkata"


# ── the registered timeout is a budget the runner reports against ────────────────────────────


def test_a_job_that_overruns_its_budget_is_recorded_timed_out(
    scratch_settings: Settings, conn: Connection
) -> None:
    """Elapsed time is measured with a monotonic interval, so a frozen clock cannot hide it."""

    def slow(context: JobContext) -> None:
        threading.Event().wait(0.02)

    runner = _runner(scratch_settings, _job("overruns", slow, timeout=timedelta(microseconds=1)))

    run = runner.run_once("overruns")

    assert run.state is JobState.TIMED_OUT and not run.succeeded
    _job_name, state, _instance, _started, _finished, error = _run_row(conn, run.run_id)
    assert state == "TIMED_OUT"
    assert error is not None and "budget" in str(error)
