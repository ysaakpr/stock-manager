"""M0.5 acceptance: the six §4.4 endpoints, against a real migrated database.

Three things are proved here, one per acceptance criterion:

1. Every endpoint answers 200 with a schema-valid *empty* payload on a freshly migrated database.
   Empty is the correct answer before anything has been ingested, and it is the answer the M0 gate
   asks for from a cold start — so the payloads are validated with the response models themselves,
   which forbid extra keys and therefore catch a route that drifted from its contract.
2. `/health` reports the heartbeat age and flips to 503 once it passes the configured threshold —
   including the boundary, both sides of it, and the three ways there can be no fresh beat.
3. No field is hardcoded. Every endpoint is asked once against an empty database and once against
   a row this test wrote, and the assertion is that the answer changed to match what was written.

`tests/integration/test_status_sync.py` (M1.3) owns the state machine's own semantics and
`tests/integration/test_scheduler.py` (M0.6) owns the heartbeat writer; this module owns the HTTP
contract over both. Everything runs against a scratch database created for the session and dropped
afterwards, never the developer's `trading` database. Needs the docker postgres (`make up`); if it
is unreachable the module skips with a loud reason.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Protocol

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Json

from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.quality.gaps import GapReason
from dataplatform.status.api import app, clock_source, db_connection, settings_source
from dataplatform.status.models import (
    ArchivesOut,
    GapsOut,
    HealthOut,
    QualityOut,
    SchedulerState,
    ServiceStatus,
    SourcesOut,
    SyncStatusOut,
)
from dataplatform.status.sync_state import SyncState
from dataplatform.store.db import Connection, connect, connection, with_dbname
from dataplatform.store.migrate import migrate

pytestmark = pytest.mark.integration

#: Dropped and recreated at the start of every session, so it is empty by construction. Keyed by
#: pid because the build runs several agents against one Postgres: a fixed name means one session's
#: `DROP DATABASE ... WITH (FORCE)` kills another session mid-test, which reads as a flaky suite.
SCRATCH_DB = f"trading_m0_5_status_api_{os.getpid()}"

#: The frozen instant every endpoint in this module answers at (B10). A heartbeat age is only
#: assertable as an exact number if "now" is a fact of the test rather than of the wall clock.
NOW = datetime(2026, 8, 10, 9, 15, tzinfo=IST)
TODAY = NOW.date()

#: A real NSE session, so `/status/sync`'s calendar fields are about the actual exchange (C.2).
SESSION = date(2026, 8, 7)

#: Small enough that the arithmetic below is obvious, and unrelated to the 300 s default — a test
#: that passed only on the default would not be testing that the threshold is configurable.
STALE_AFTER = 120

SOURCE = "nse_bhavcopy_udiff"


def _settings_for(dbname: str) -> Settings:
    """Settings for the configured server with a different database selected."""
    return Settings(database_url=with_dbname(Settings().database_url.get_secret_value(), dbname))


def _with_threshold(settings: Settings, seconds: int) -> Settings:
    """The same settings with a different staleness threshold, and nothing else changed."""
    return Settings(
        database_url=settings.database_url,
        scheduler_heartbeat_stale_after_seconds=seconds,
    )


def _with_flag_limit(settings: Settings, limit: int) -> Settings:
    """The same settings with a different `/status/quality` page size."""
    return Settings(database_url=settings.database_url, status_quality_flag_limit=limit)


@pytest.fixture(scope="session")
def scratch_settings() -> Iterator[Settings]:
    """A migrated scratch database for the session; dropped again afterwards."""
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

    settings = _with_threshold(_settings_for(SCRATCH_DB), STALE_AFTER)
    migrate(settings, clock=FrozenClock(NOW))
    yield settings

    conn = connect(admin, autocommit=True)
    try:
        conn.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)')
    finally:
        conn.close()


@pytest.fixture
def conn(scratch_settings: Settings) -> Iterator[Connection]:
    """A connection to the scratch database, rolled back at the end of the test.

    Rollback rather than DELETE, so each test sees only the rows it wrote itself and the "empty
    database" assertions stay true however many tests ran before them.
    """
    with connection(scratch_settings) as live:
        try:
            yield live
        finally:
            live.rollback()


@pytest.fixture
def clock() -> FrozenClock:
    """The clock every endpoint in this module reads (B10)."""
    return FrozenClock(NOW)


class ClientFactory(Protocol):
    """Builds the app for one `Settings`; `use_test_connection` picks which connection it reads."""

    def __call__(
        self, settings: Settings, *, use_test_connection: bool = ...
    ) -> AbstractContextManager[TestClient]: ...


@pytest.fixture
def make_client(conn: Connection, clock: FrozenClock) -> ClientFactory:
    """Point the real ASGI app at a chosen `Settings`, this test's connection and frozen clock.

    Overriding the connection dependency with *this test's* connection is deliberate: the
    endpoints then read inside the same uncommitted transaction the test wrote in, so every
    assertion is about rows the test actually created and nothing leaks into the next one.
    `use_test_connection=False` leaves the app opening its own, which is the only way to observe
    what it does when the database it is configured with is not there.

    `/health` never uses that dependency at all — it reads the heartbeat on its own connection, by
    design, so heartbeat rows must be committed (which the `heartbeat` fixture does, and cleans up).
    """

    @contextmanager
    def build(settings: Settings, *, use_test_connection: bool = True) -> Iterator[TestClient]:
        app.dependency_overrides[settings_source] = lambda: settings
        app.dependency_overrides[clock_source] = lambda: clock
        if use_test_connection:
            app.dependency_overrides[db_connection] = lambda: conn
        try:
            with TestClient(app) as live:
                yield live
        finally:
            app.dependency_overrides.clear()

    return build


@pytest.fixture
def client(scratch_settings: Settings, make_client: ClientFactory) -> Iterator[TestClient]:
    """The app on the scratch database, with the module's threshold."""
    with make_client(scratch_settings) as live:
        yield live


@pytest.fixture
def heartbeat(scratch_settings: Settings) -> Iterator[Callable[[datetime], None]]:
    """Write a committed `scheduler_heartbeat` row, and clear the table afterwards.

    Committed because `/health` reads the heartbeat on its own connection (that is what makes it
    answerable when the request connection is unusable), so an uncommitted row would be invisible
    to exactly the code under test.
    """
    scheduler_id = "test-scheduler"

    def write(beat_at: datetime) -> None:
        with connection(scratch_settings, autocommit=True) as live:
            live.execute(
                "INSERT INTO scheduler_heartbeat (scheduler_id, beat_at, detail, updated_at) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (scheduler_id) DO UPDATE SET beat_at = EXCLUDED.beat_at, "
                "detail = EXCLUDED.detail, updated_at = EXCLUDED.updated_at",
                (scheduler_id, beat_at, Json({"state": "ALIVE"}), beat_at),
            )

    yield write

    with connection(scratch_settings, autocommit=True) as live:
        live.execute("DELETE FROM scheduler_heartbeat")


# ── seed helpers: plain SQL, so this module tests the API and not another module's writer ────


def seed_sync(
    conn: Connection,
    *,
    source: str = SOURCE,
    day: date = SESSION,
    state: str = "PUBLISHED",
    attempts: int = 1,
    retryable: bool = True,
    last_error: str | None = None,
    checksum: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO sync_state (source, logical_date, state, attempts, retryable, last_error, "
        "checksum, l0_path, first_attempt_at, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            source,
            day,
            state,
            attempts,
            retryable,
            last_error,
            checksum,
            None if checksum is None else f"L0/{source}/{day.isoformat()}.zip",
            NOW,
            NOW,
        ),
    )


def seed_flag(
    conn: Connection,
    *,
    check_name: str,
    severity: str,
    day: date = SESSION,
    resolved: bool = False,
    observed_value: Decimal | None = None,
    threshold: Decimal | None = None,
    raised_at: datetime | None = None,
) -> int:
    row = conn.execute(
        "INSERT INTO quality_flag (logical_date, check_name, severity, isin, source, detail, "
        "observed_value, threshold, resolved, resolved_at, raised_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (
            day,
            check_name,
            severity,
            "INE002A01018",
            SOURCE,
            Json({"note": check_name}),
            observed_value,
            threshold,
            resolved,
            NOW if resolved else None,
            NOW if raised_at is None else raised_at,
        ),
    ).fetchone()
    assert row is not None
    return int(row[0])


def seed_bundle(conn: Connection, *, day: date = SESSION) -> None:
    conn.execute(
        "INSERT INTO archive_bundle (logical_date, schema_version, bundle_path, manifest_sha256, "
        "file_count, total_bytes, manifest, published_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (
            day,
            "1",
            f"archives/{day.isoformat()}",
            "a" * 64,
            2,
            4096,
            Json(
                {
                    "files": [
                        {
                            "name": "prices_raw.parquet",
                            "path": "prices_raw.parquet",
                            "sha256": "b" * 64,
                            "bytes": 4000,
                            "rows": 2100,
                        },
                        {
                            "name": "manifest.csv",
                            "path": "manifest.csv",
                            "sha256": "c" * 64,
                            "bytes": 96,
                        },
                    ]
                }
            ),
            NOW,
        ),
    )


# ── acceptance 1: every endpoint answers 200 with a schema-valid empty payload ───────────────


def test_health_is_200_and_schema_valid_on_an_empty_database(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200

    body = HealthOut.model_validate(response.json())
    assert body.status is ServiceStatus.OK
    assert body.database.reachable and body.database.error is None
    # A migrated database with no scheduler yet is not an outage — it is a deployment that has
    # not started one. Reporting it as STALE would make every cold start fail its own gate.
    assert body.scheduler.state is SchedulerState.NEVER_RAN
    assert body.scheduler.last_beat_at is None
    assert body.scheduler.age_seconds is None
    assert body.scheduler.stale_after_seconds == STALE_AFTER
    assert body.checked_at == NOW


def test_status_sync_is_200_and_empty_on_an_empty_database(client: TestClient) -> None:
    response = client.get("/status/sync", params={"date": SESSION.isoformat()})
    assert response.status_code == 200

    body = SyncStatusOut.model_validate(response.json())
    assert body.date == SESSION
    assert body.rows == []
    assert body.datasets == []
    # No dataset was named, so there is no verdict to give — not a green one.
    assert body.green is None


def test_status_sources_is_200_and_empty_on_an_empty_database(client: TestClient) -> None:
    response = client.get("/status/sources")
    assert response.status_code == 200

    body = SourcesOut.model_validate(response.json())
    assert body.sources == []
    assert body.as_of == TODAY


def test_status_gaps_is_200_and_empty_on_an_empty_database(client: TestClient) -> None:
    response = client.get(
        "/status/gaps", params={"from": SESSION.isoformat(), "to": TODAY.isoformat()}
    )
    assert response.status_code == 200

    body = GapsOut.model_validate(response.json())
    assert body.unexplained == []
    assert (body.from_date, body.to_date) == (SESSION, TODAY)


def test_status_quality_is_200_and_empty_on_an_empty_database(client: TestClient) -> None:
    response = client.get("/status/quality")
    assert response.status_code == 200

    body = QualityOut.model_validate(response.json())
    assert body.flags == []
    assert body.counts == []
    assert body.open_total == 0
    assert body.as_of == NOW


def test_archives_is_200_and_empty_on_an_empty_database(client: TestClient) -> None:
    response = client.get("/archives", params={"date": SESSION.isoformat()})
    assert response.status_code == 200

    body = ArchivesOut.model_validate(response.json())
    assert body.date == SESSION
    assert body.bundle is None


def test_the_defaulting_endpoints_answer_without_any_parameters(client: TestClient) -> None:
    """`/status/gaps` and `/archives` default to today rather than to all of history."""
    gaps = client.get("/status/gaps")
    archives = client.get("/archives")
    assert (gaps.status_code, archives.status_code) == (200, 200)
    assert GapsOut.model_validate(gaps.json()).from_date == TODAY
    assert ArchivesOut.model_validate(archives.json()).date == TODAY


# ── acceptance 2: heartbeat age, and the 503 when it goes stale ──────────────────────────────


def test_health_reports_the_heartbeat_age_from_the_injected_clock(
    client: TestClient, heartbeat: Callable[[datetime], None]
) -> None:
    heartbeat(NOW - timedelta(seconds=45))

    response = client.get("/health")
    assert response.status_code == 200

    body = HealthOut.model_validate(response.json())
    assert body.scheduler.state is SchedulerState.OK
    assert body.scheduler.age_seconds == 45.0
    assert body.scheduler.last_beat_at == NOW - timedelta(seconds=45)
    assert body.scheduler.scheduler_id == "test-scheduler"
    assert body.status is ServiceStatus.OK


def test_health_turns_503_once_the_heartbeat_is_stale(
    client: TestClient, heartbeat: Callable[[datetime], None]
) -> None:
    heartbeat(NOW - timedelta(seconds=STALE_AFTER + 1))

    response = client.get("/health")
    assert response.status_code == 503

    # The body still has to be the contract, not an error string: the probe reads the code and the
    # dashboard reads the body, and they must not be able to disagree.
    body = HealthOut.model_validate(response.json())
    assert body.status is ServiceStatus.DEGRADED
    assert body.scheduler.state is SchedulerState.STALE
    assert body.scheduler.age_seconds == float(STALE_AFTER + 1)
    assert body.database.reachable


@pytest.mark.parametrize(
    ("age_seconds", "expected_state", "expected_code"),
    [
        (STALE_AFTER - 1, SchedulerState.OK, 200),
        (STALE_AFTER, SchedulerState.OK, 200),  # at the threshold, not past it
        (STALE_AFTER + 1, SchedulerState.STALE, 503),
    ],
)
def test_the_staleness_boundary_is_exact(
    client: TestClient,
    heartbeat: Callable[[datetime], None],
    age_seconds: int,
    expected_state: SchedulerState,
    expected_code: int,
) -> None:
    """Both sides of the threshold, so an inverted comparison cannot pass."""
    heartbeat(NOW - timedelta(seconds=age_seconds))

    response = client.get("/health")
    assert response.status_code == expected_code
    assert HealthOut.model_validate(response.json()).scheduler.state is expected_state


def test_the_threshold_is_configured_not_hardcoded(
    scratch_settings: Settings,
    make_client: ClientFactory,
    heartbeat: Callable[[datetime], None],
) -> None:
    """The same beat is healthy or stale depending only on the configured threshold."""
    heartbeat(NOW - timedelta(seconds=STALE_AFTER + 1))

    with make_client(_with_threshold(scratch_settings, STALE_AFTER * 10)) as tolerant:
        forgiving = tolerant.get("/health")
    with make_client(_with_threshold(scratch_settings, 1)) as strict:
        impatient = strict.get("/health")

    assert forgiving.status_code == 200
    assert HealthOut.model_validate(forgiving.json()).scheduler.state is SchedulerState.OK
    assert impatient.status_code == 503
    assert HealthOut.model_validate(impatient.json()).scheduler.state is SchedulerState.STALE


def test_a_beat_stamped_in_the_future_is_reported_not_clamped(
    client: TestClient, heartbeat: Callable[[datetime], None]
) -> None:
    """Two clocks disagreeing is worth seeing; a negative age is how it becomes visible."""
    heartbeat(NOW + timedelta(seconds=30))

    response = client.get("/health")
    assert response.status_code == 200
    assert HealthOut.model_validate(response.json()).scheduler.age_seconds == -30.0


def test_health_answers_503_and_says_so_when_the_database_is_unreachable(
    make_client: ClientFactory,
) -> None:
    """The one endpoint that has to work while everything else is broken.

    `UNKNOWN`, never `NEVER_RAN`: a heartbeat that could not be read is not evidence that no
    scheduler ever ran, and /health may not invent the fact it failed to check.
    """
    dead = Settings(database_url="postgresql://trading:trading@127.0.0.1:1/trading")
    with make_client(dead) as client:
        response = client.get("/health")

    assert response.status_code == 503
    body = HealthOut.model_validate(response.json())
    assert body.status is ServiceStatus.DEGRADED
    assert body.database.reachable is False
    assert body.database.error
    assert body.scheduler.state is SchedulerState.UNKNOWN
    assert body.scheduler.age_seconds is None


# ── acceptance 3: every field traces to a query, on every endpoint ───────────────────────────


def test_status_sync_reflects_the_rows_that_are_there(client: TestClient, conn: Connection) -> None:
    seed_sync(conn, state="PUBLISHED", checksum="deadbeef")
    seed_sync(conn, source="nse_fii_dii_flows", state="FAILED", attempts=3, last_error="HTTP 503")

    body = SyncStatusOut.model_validate(
        client.get("/status/sync", params={"date": SESSION.isoformat()}).json()
    )

    by_source = {row.source: row for row in body.rows}
    assert set(by_source) == {SOURCE, "nse_fii_dii_flows"}
    assert by_source[SOURCE].state.value == "PUBLISHED"
    assert by_source[SOURCE].checksum == "deadbeef"
    assert by_source["nse_fii_dii_flows"].attempts == 3
    assert by_source["nse_fii_dii_flows"].last_error == "HTTP 503"
    # A different date shares none of it — proof the date parameter reaches the query.
    other = SyncStatusOut.model_validate(
        client.get("/status/sync", params={"date": TODAY.isoformat()}).json()
    )
    assert other.rows == []


def test_status_sync_green_is_the_interlocks_verdict_over_the_named_datasets(
    client: TestClient, conn: Connection
) -> None:
    """Invariant #10: a dataset that is not PUBLISHED means the date is not green."""
    seed_sync(conn, state="PUBLISHED", checksum="deadbeef")
    seed_sync(conn, source="nse_sec_bhavdata_full", state="FAILED", last_error="HTTP 500")

    green = SyncStatusOut.model_validate(
        client.get("/status/sync", params={"date": SESSION.isoformat(), "dataset": SOURCE}).json()
    )
    red = SyncStatusOut.model_validate(
        client.get(
            "/status/sync",
            params={"date": SESSION.isoformat(), "dataset": [SOURCE, "nse_sec_bhavdata_full"]},
        ).json()
    )

    assert green.green is True
    assert red.green is False
    assert red.reason


def test_status_sources_computes_last_success_lag_and_failure_streak(
    client: TestClient, conn: Connection
) -> None:
    seed_sync(conn, day=SESSION, state="PUBLISHED", checksum="deadbeef")
    seed_sync(conn, day=date(2026, 8, 10), state="FAILED", attempts=2, last_error="HTTP 500")

    body = SourcesOut.model_validate(client.get("/status/sources").json())

    assert [entry.source for entry in body.sources] == [SOURCE]
    entry = body.sources[0]
    assert entry.last_success_date == SESSION
    assert entry.lag_days == (TODAY - SESSION).days
    assert entry.failure_streak == 1
    assert entry.last_error == "HTTP 500"
    assert entry.healthy is False


def test_status_gaps_lists_only_the_incomplete_pairs_in_the_range(
    client: TestClient, conn: Connection
) -> None:
    """PUBLISHED is done and GAP is explained; FAILED and mid-pipeline are what is owed."""
    seed_sync(conn, day=SESSION, state="PUBLISHED", checksum="deadbeef")
    seed_sync(conn, day=date(2026, 8, 8), state="GAP")
    seed_sync(conn, day=date(2026, 8, 10), state="FAILED", attempts=2, last_error="HTTP 500")
    seed_sync(conn, day=date(2026, 8, 11), state="FETCHED")
    seed_sync(conn, day=date(2026, 7, 1), state="FAILED", last_error="out of range")

    body = GapsOut.model_validate(
        client.get("/status/gaps", params={"from": SESSION.isoformat(), "to": "2026-08-11"}).json()
    )

    assert [(entry.date, entry.state) for entry in body.unexplained] == [
        (date(2026, 8, 10), SyncState.FAILED),
        (date(2026, 8, 11), SyncState.FETCHED),
    ]
    assert body.unexplained[0].last_error == "HTTP 500"
    # 2026-08-09 is a Sunday with no row: the calendar explains it, so it is not owed.
    assert body.fully_explained is False


def test_status_gaps_reports_a_session_nobody_ever_attempted(
    client: TestClient, conn: Connection
) -> None:
    """M1.11's whole point: an absent row is invisible to a query over `sync_state` alone.

    The source is tracked (it has a row for one session) and the range holds another session it
    never reached — which must surface as `NEVER_ATTEMPTED`, with no history to show for it.
    """
    seed_sync(conn, day=date(2026, 8, 10), state="PUBLISHED", checksum="deadbeef")

    body = GapsOut.model_validate(
        client.get("/status/gaps", params={"from": SESSION.isoformat(), "to": "2026-08-10"}).json()
    )

    assert body.sources == [SOURCE]
    assert [(entry.date, entry.reason) for entry in body.unexplained] == [
        (SESSION, GapReason.NEVER_ATTEMPTED)
    ]
    assert body.unexplained[0].state is None
    assert body.fully_explained is False
    # Friday, Saturday, Sunday, Monday — one pair each, and the weekend is explained.
    assert (body.pairs_examined, body.complete, body.explained_total) == (4, 1, 2)


def test_status_gaps_is_empty_when_the_range_is_complete(
    client: TestClient, conn: Connection
) -> None:
    """The M1 gate's pass condition, over the wire: every day accounted for, nothing owed."""
    seed_sync(conn, day=SESSION, state="PUBLISHED", checksum="deadbeef")
    seed_sync(conn, day=date(2026, 8, 8), state="GAP")
    seed_sync(conn, day=date(2026, 8, 9), state="GAP")

    body = GapsOut.model_validate(
        client.get("/status/gaps", params={"from": SESSION.isoformat(), "to": "2026-08-09"}).json()
    )

    assert body.unexplained == []
    assert body.unexplained_total == 0
    assert body.fully_explained is True


def test_status_gaps_refuses_an_inverted_range(client: TestClient) -> None:
    response = client.get("/status/gaps", params={"from": "2026-08-11", "to": "2026-08-07"})
    assert response.status_code == 400


def test_status_gaps_refuses_a_range_the_trading_calendar_does_not_cover(
    client: TestClient,
) -> None:
    """ "No holidays that year" would invent ~250 sessions and report every one as missing."""
    response = client.get("/status/gaps", params={"from": "2011-06-01", "to": "2011-06-30"})
    assert response.status_code == 400
    assert "coverage" in response.json()["detail"]


def test_status_quality_reports_open_flags_with_their_measured_values(
    client: TestClient, conn: Connection
) -> None:
    seed_flag(
        conn,
        check_name="price_jump",
        severity="ERROR",
        observed_value=Decimal("0.421000"),
        threshold=Decimal("0.200000"),
    )
    seed_flag(conn, check_name="thin_volume", severity="WARN")
    seed_flag(conn, check_name="already_fixed", severity="ERROR", resolved=True)

    body = QualityOut.model_validate(client.get("/status/quality").json())

    assert body.open_total == 2
    assert {(entry.severity.value, entry.count) for entry in body.counts} == {
        ("ERROR", 1),
        ("WARN", 1),
    }
    assert {flag.check_name for flag in body.flags} == {"price_jump", "thin_volume"}
    jump = next(flag for flag in body.flags if flag.check_name == "price_jump")
    # Exact decimals, not floats: a sentinel that fires on a price is comparing money.
    assert jump.observed_value == Decimal("0.421000")
    assert jump.threshold == Decimal("0.200000")
    assert jump.detail == {"note": "price_jump"}
    assert jump.isin == "INE002A01018"


def test_status_quality_caps_the_list_without_understating_the_total(
    scratch_settings: Settings,
    make_client: ClientFactory,
    conn: Connection,
) -> None:
    """A status endpoint whose total is its own page size cannot report a flood."""
    for index in range(5):
        seed_flag(
            conn,
            check_name=f"check_{index}",
            severity="WARN",
            raised_at=NOW - timedelta(minutes=index),
        )

    with make_client(_with_flag_limit(scratch_settings, 2)) as capped:
        body = QualityOut.model_validate(capped.get("/status/quality").json())

    assert body.limit == 2
    assert len(body.flags) == 2
    assert body.open_total == 5
    # Newest first, so the cap keeps the flags an operator most needs to see.
    assert [flag.check_name for flag in body.flags] == ["check_0", "check_1"]


def test_archives_returns_the_stored_manifest_for_the_date(
    client: TestClient, conn: Connection
) -> None:
    seed_bundle(conn)

    body = ArchivesOut.model_validate(
        client.get("/archives", params={"date": SESSION.isoformat()}).json()
    )

    assert body.bundle is not None
    assert body.bundle.date == SESSION
    assert body.bundle.bundle_path == f"archives/{SESSION.isoformat()}"
    assert body.bundle.manifest_sha256 == "a" * 64
    assert body.bundle.file_count == 2
    assert body.bundle.total_bytes == 4096
    assert [file.name for file in body.bundle.files] == ["prices_raw.parquet", "manifest.csv"]
    assert body.bundle.files[0].sha256 == "b" * 64
    assert body.bundle.files[0].rows == 2100
    assert body.bundle.files[1].rows is None
    # A date with no bundle is not this one with a different label.
    empty = ArchivesOut.model_validate(
        client.get("/archives", params={"date": TODAY.isoformat()}).json()
    )
    assert empty.bundle is None


def test_a_status_endpoint_answers_503_when_the_database_is_gone(
    make_client: ClientFactory,
) -> None:
    """An unreachable Postgres is infrastructure, and says so — it is not an anonymous 500."""
    dead = Settings(database_url="postgresql://trading:trading@127.0.0.1:1/trading")
    with make_client(dead, use_test_connection=False) as client:
        response = client.get("/status/sources")

    assert response.status_code == 503
    assert "database unreachable" in response.json()["detail"]
