"""M1.3 acceptance: the §4.4 state machine against real rows, and the two endpoints that serve it.

`tests/unit/test_sync_state.py` owns the rules; this module owns the wiring — that a transition
really lands in `sync_state`, that `attempts` and `last_error` survive a round trip through
Postgres, that `/status/sync?date=` reflects those rows rather than a fixture, and that
`/status/sources` computes last success, lag and failure streak from them.

Everything runs against a scratch database created for the session and dropped afterwards, never
the developer's `trading` database — the state machine's own tests must not collide with rows a
backfill run left behind, and vice versa. Each test gets a connection whose transaction is rolled
back at the end, so the tests are order-independent.

Needs the docker postgres (`make up`); if it is unreachable the module skips with a loud reason.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import date, datetime, timedelta

import psycopg
import pytest
from fastapi.testclient import TestClient

from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.ingest.calendar import DayKind, trading_calendar
from dataplatform.status.api import app, clock_source, db_connection, settings_source
from dataplatform.status.sync_state import (
    IllegalTransitionError,
    NotAGapError,
    SyncState,
    SyncStateStore,
    UnknownSyncRowError,
    is_green,
)
from dataplatform.store.db import Connection, connect, connection, with_dbname
from dataplatform.store.migrate import migrate

pytestmark = pytest.mark.integration

#: Dropped and recreated at the start of every session, so it is empty by construction. Keyed by
#: pid because the build runs several agents against one Postgres: a fixed name means one session's
#: `DROP DATABASE ... WITH (FORCE)` kills another session mid-test, which reads as a flaky suite —
#: observed as `relation "sync_state" does not exist` across this module during M1.11.
SCRATCH_DB = f"trading_m1_3_sync_state_{os.getpid()}"

BHAVCOPY = "nse_bhavcopy_udiff"
DELIVERY = "nse_sec_bhavdata_full"
FLOWS = "nse_fii_dii_flows"

#: Real NSE dates, so the calendar assertions are about the actual exchange (C.2).
SESSION = date(2026, 8, 7)  # Friday, a full session
PREVIOUS_SESSION = date(2026, 8, 6)  # Thursday
WEEKEND = date(2026, 8, 8)  # Saturday
HOLIDAY = date(2026, 1, 26)  # Republic Day, a Monday — a weekday the exchange gave up
LATER_WEEKEND = date(2026, 8, 15)  # Saturday, after the mid-August dates used for streaks
UNCOVERED = date(2011, 6, 1)  # before the holiday file begins

#: `today` for every test — the session after the one being ingested, so lag is measurable.
NOW = datetime(2026, 8, 10, 9, 15, tzinfo=IST)


def _settings_for(dbname: str) -> Settings:
    """Settings for the configured server with a different database selected."""
    return Settings(database_url=with_dbname(Settings().database_url.get_secret_value(), dbname))


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
    """A connection to the scratch database, rolled back at the end of the test.

    Rollback rather than DELETE: it keeps every test seeing a `sync_state` table with only its own
    rows in it, which is what makes the `/status/sources` aggregates assertable at all.
    """
    with connection(scratch_settings) as live:
        try:
            yield live
        finally:
            live.rollback()


@pytest.fixture
def clock() -> FrozenClock:
    """The injected clock every store and endpoint in this module reads (B10)."""
    return FrozenClock(NOW)


@pytest.fixture
def store(conn: Connection, clock: FrozenClock) -> SyncStateStore:
    return SyncStateStore(conn, clock=clock, calendar=trading_calendar())


@pytest.fixture
def client(
    scratch_settings: Settings, conn: Connection, clock: FrozenClock
) -> Iterator[TestClient]:
    """The real ASGI app, pointed at this test's connection and frozen clock.

    Overriding the connection dependency with *this test's* connection is deliberate: the endpoint
    then reads inside the same uncommitted transaction the test wrote in, so the assertions are
    about rows the test actually created and nothing leaks into the next one.
    """
    app.dependency_overrides[settings_source] = lambda: scratch_settings
    app.dependency_overrides[db_connection] = lambda: conn
    app.dependency_overrides[clock_source] = lambda: clock
    try:
        with TestClient(app) as live:
            yield live
    finally:
        app.dependency_overrides.clear()


def publish(store: SyncStateStore, source: str, day: date, *, checksum: str = "sha") -> None:
    """Drive one (source, date) all the way down the happy path."""
    store.begin(source, day)
    store.mark_fetched(source, day, checksum=checksum, l0_path=f"L0/{source}/{day}.zip")
    store.mark_validated(source, day)
    store.mark_normalized(source, day)
    store.mark_published(source, day)


# ── acceptance 1: the transitions, persisted ────────────────────────────────────────────────


def test_the_happy_path_lands_one_row_per_source_and_date(
    store: SyncStateStore, conn: Connection
) -> None:
    publish(store, BHAVCOPY, SESSION, checksum="deadbeef")

    row = conn.execute(
        "SELECT state, attempts, retryable, checksum, l0_path, first_attempt_at, updated_at "
        "FROM sync_state WHERE source = %s AND logical_date = %s",
        (BHAVCOPY, SESSION),
    ).fetchone()
    assert row is not None, "the state machine must write through to sync_state"
    assert row[0] == SyncState.PUBLISHED.value
    assert row[1] == 1, "one clean run is one attempt"
    assert row[3] == "deadbeef", "the L0 checksum survives to the published row"
    assert row[5] == NOW and row[6] == NOW, "timestamps come from the injected clock (B10)"

    count = conn.execute(
        "SELECT count(*) FROM sync_state WHERE source = %s AND logical_date = %s",
        (BHAVCOPY, SESSION),
    ).fetchone()
    assert count is not None and count[0] == 1, "per-(source, date) means exactly one row"


def test_an_illegal_transition_raises_and_writes_nothing(store: SyncStateStore) -> None:
    """Acceptance 1. Both states are named, and the row is left exactly as it was."""
    publish(store, BHAVCOPY, SESSION)

    with pytest.raises(IllegalTransitionError) as caught:
        store.mark_fetched(BHAVCOPY, SESSION, checksum="other")
    assert "PUBLISHED" in str(caught.value) and "FETCHED" in str(caught.value)

    after = store.require(BHAVCOPY, SESSION)
    assert after.state is SyncState.PUBLISHED
    assert after.checksum == "sha", "a refused transition must not half-apply"


def test_skipping_a_step_is_refused(store: SyncStateStore) -> None:
    store.begin(BHAVCOPY, SESSION)
    with pytest.raises(IllegalTransitionError, match="legal from PENDING"):
        store.mark_published(BHAVCOPY, SESSION)


def test_advancing_a_row_that_was_never_begun_is_refused(store: SyncStateStore) -> None:
    with pytest.raises(UnknownSyncRowError, match="call begin"):
        store.mark_fetched(BHAVCOPY, SESSION, checksum="x")


def test_attempts_and_last_error_are_preserved_across_a_retry(
    store: SyncStateStore, conn: Connection
) -> None:
    """Acceptance 1's second half, through Postgres rather than in memory."""
    store.begin(BHAVCOPY, SESSION)
    store.mark_failed(BHAVCOPY, SESSION, "502 from nsearchives")
    store.begin(BHAVCOPY, SESSION)
    store.mark_failed(BHAVCOPY, SESSION, "connection reset by peer")
    store.begin(BHAVCOPY, SESSION)

    row = conn.execute(
        "SELECT state, attempts, last_error FROM sync_state "
        "WHERE source = %s AND logical_date = %s",
        (BHAVCOPY, SESSION),
    ).fetchone()
    assert row is not None
    assert row[0] == SyncState.PENDING.value
    assert row[1] == 3, "three attempts started, three counted"
    assert row[2] == "connection reset by peer", "the newest failure text survives the retry"

    publish_after_retry = store.mark_fetched(BHAVCOPY, SESSION, checksum="ok")
    assert publish_after_retry.last_error == "connection reset by peer"


def test_a_non_retryable_failure_stops_the_backfill_from_re_driving_it(
    store: SyncStateStore,
) -> None:
    store.begin(BHAVCOPY, SESSION)
    store.mark_failed(BHAVCOPY, SESSION, "404 — never published", retryable=False)
    with pytest.raises(IllegalTransitionError, match="non-retryable"):
        store.begin(BHAVCOPY, SESSION)


# ── acceptance 2: GAP(expected) from the calendar, never for a real miss ─────────────────────


@pytest.mark.parametrize(("day", "kind"), [(WEEKEND, DayKind.WEEKEND), (HOLIDAY, DayKind.HOLIDAY)])
def test_a_closed_day_is_recorded_as_a_gap(store: SyncStateStore, day: date, kind: DayKind) -> None:
    record = store.mark_gap(BHAVCOPY, day)
    assert record.state is SyncState.GAP
    assert record.attempts == 0, "nothing was attempted on a day the exchange was shut"
    assert store.calendar.classify(day) is kind


def test_a_session_can_never_be_filed_as_a_gap(store: SyncStateStore) -> None:
    """Acceptance 2. This is the assertion that keeps the M1 gate honest.

    If a failed fetch could be recorded as a holiday it would vanish from the gap report, and
    "100% of missing days explained" would be true only because the lie was well formatted.
    """
    with pytest.raises(NotAGapError, match="real miss"):
        store.mark_gap(BHAVCOPY, SESSION)
    assert store.get(BHAVCOPY, SESSION) is None, "a refused gap must not leave a row behind"


def test_a_failed_session_stays_failed_rather_than_becoming_a_gap(
    store: SyncStateStore,
) -> None:
    store.begin(BHAVCOPY, SESSION)
    store.mark_failed(BHAVCOPY, SESSION, "timeout")
    with pytest.raises(NotAGapError):
        store.mark_gap(BHAVCOPY, SESSION)
    assert store.require(BHAVCOPY, SESSION).state is SyncState.FAILED


# ── invariant #10: is_green over real rows ──────────────────────────────────────────────────


def test_is_green_when_every_named_dataset_published(store: SyncStateStore) -> None:
    publish(store, BHAVCOPY, SESSION)
    publish(store, DELIVERY, SESSION)
    status = store.is_green(SESSION, [BHAVCOPY, DELIVERY])
    assert status.green is True
    assert status.day_kind is DayKind.SESSION


def test_is_not_green_when_one_dataset_lags(store: SyncStateStore) -> None:
    publish(store, BHAVCOPY, SESSION)
    store.begin(DELIVERY, SESSION)
    status = store.is_green(SESSION, [BHAVCOPY, DELIVERY])
    assert not status
    assert status.not_published == ((DELIVERY, SyncState.PENDING),)


def test_is_not_green_when_a_dataset_was_never_asked_for(store: SyncStateStore) -> None:
    """The dangerous case: two of three datasets are perfect and the third has no row at all."""
    publish(store, BHAVCOPY, SESSION)
    publish(store, DELIVERY, SESSION)
    status = store.is_green(SESSION, [BHAVCOPY, DELIVERY, FLOWS])
    assert not status
    assert status.missing == (FLOWS,)


def test_an_open_error_quality_flag_turns_the_day_red(
    store: SyncStateStore, conn: Connection
) -> None:
    """§4.4 wants PUBLISHED *and* quality-green — published-but-wrong is still not tradeable."""
    publish(store, BHAVCOPY, SESSION)
    assert store.is_green(SESSION, [BHAVCOPY]).green is True

    conn.execute(
        "INSERT INTO quality_flag (logical_date, check_name, severity, source, raised_at) "
        "VALUES (%s, 'close_outside_high_low', 'ERROR', %s, %s)",
        (SESSION, BHAVCOPY, NOW),
    )
    status = store.is_green(SESSION, [BHAVCOPY])
    assert not status
    assert status.open_error_flags == 1


def test_a_warning_flag_does_not_stop_trading(store: SyncStateStore, conn: Connection) -> None:
    publish(store, BHAVCOPY, SESSION)
    conn.execute(
        "INSERT INTO quality_flag (logical_date, check_name, severity, source, raised_at) "
        "VALUES (%s, 'volume_outlier', 'WARN', %s, %s)",
        (SESSION, BHAVCOPY, NOW),
    )
    assert store.is_green(SESSION, [BHAVCOPY]).green is True


def test_a_resolved_error_flag_does_not_stop_trading(
    store: SyncStateStore, conn: Connection
) -> None:
    publish(store, BHAVCOPY, SESSION)
    conn.execute(
        "INSERT INTO quality_flag (logical_date, check_name, severity, source, resolved, "
        "resolved_at, resolution, raised_at) "
        "VALUES (%s, 'ca_mismatch', 'ERROR', %s, true, %s, 'reconciled by hand', %s)",
        (SESSION, BHAVCOPY, NOW, NOW),
    )
    assert store.is_green(SESSION, [BHAVCOPY]).green is True


def test_a_market_wide_flag_counts_for_every_dataset(
    store: SyncStateStore, conn: Connection
) -> None:
    """A flag about the whole date has a NULL source and must not be filtered away."""
    publish(store, BHAVCOPY, SESSION)
    conn.execute(
        "INSERT INTO quality_flag (logical_date, check_name, severity, raised_at) "
        "VALUES (%s, 'breadth_impossible', 'ERROR', %s)",
        (SESSION, NOW),
    )
    assert not store.is_green(SESSION, [BHAVCOPY])


def test_a_closed_day_is_never_green(store: SyncStateStore) -> None:
    store.mark_gap(BHAVCOPY, WEEKEND)
    status = store.is_green(WEEKEND, [BHAVCOPY])
    assert not status
    assert "WEEKEND" in status.reason


def test_the_module_level_is_green_opens_its_own_connection(
    scratch_settings: Settings, clock: FrozenClock
) -> None:
    """The daily loop's one-liner (§4.4). Committed rows, because it uses its own connection."""
    with connection(scratch_settings) as writer:
        SyncStateStore(writer, clock=clock).begin(FLOWS, SESSION)
        writer.commit()
    try:
        status = is_green(SESSION, [FLOWS], settings=scratch_settings, clock=clock)
        assert not status
        assert status.not_published == ((FLOWS, SyncState.PENDING),)
    finally:
        with connection(scratch_settings) as cleanup:
            cleanup.execute("DELETE FROM sync_state WHERE source = %s", (FLOWS,))
            cleanup.commit()


# ── acceptance 3: the two endpoints serve real rows ─────────────────────────────────────────


def test_status_sync_reflects_the_rows_that_exist(
    client: TestClient, store: SyncStateStore
) -> None:
    """Acceptance 3, first half: what the endpoint returns is what the table holds."""
    publish(store, BHAVCOPY, SESSION, checksum="abc")
    store.begin(DELIVERY, SESSION)
    store.mark_failed(DELIVERY, SESSION, "502 from nsearchives")

    body = client.get("/status/sync", params={"date": SESSION.isoformat()}).json()
    assert body["date"] == SESSION.isoformat()
    assert body["day_kind"] == "SESSION" and body["expects_data"] is True

    rows = {row["source"]: row for row in body["rows"]}
    assert set(rows) == {BHAVCOPY, DELIVERY}
    assert rows[BHAVCOPY]["state"] == "PUBLISHED" and rows[BHAVCOPY]["checksum"] == "abc"
    assert rows[DELIVERY]["state"] == "FAILED"
    assert rows[DELIVERY]["last_error"] == "502 from nsearchives"
    assert rows[DELIVERY]["attempts"] == 1


def test_status_sync_carries_the_interlock_verdict(
    client: TestClient, store: SyncStateStore
) -> None:
    publish(store, BHAVCOPY, SESSION)
    publish(store, DELIVERY, SESSION)

    green = client.get(
        "/status/sync", params={"date": SESSION.isoformat(), "dataset": [BHAVCOPY, DELIVERY]}
    ).json()
    assert green["green"] is True
    assert green["datasets"] == [BHAVCOPY, DELIVERY]

    red = client.get(
        "/status/sync", params={"date": SESSION.isoformat(), "dataset": [BHAVCOPY, FLOWS]}
    ).json()
    assert red["green"] is False
    assert red["missing"] == [FLOWS]
    assert FLOWS in red["reason"]


def test_status_sync_refuses_to_guess_which_datasets_matter(client: TestClient) -> None:
    """No dataset named means no verdict — a vacuous green is the failure #10 forbids."""
    body = client.get("/status/sync", params={"date": SESSION.isoformat()}).json()
    assert body["green"] is None and body["datasets"] == []


def test_status_sync_on_an_empty_date_is_empty_not_invented(client: TestClient) -> None:
    body = client.get("/status/sync", params={"date": PREVIOUS_SESSION.isoformat()}).json()
    assert body["rows"] == []
    assert body["day_kind"] == "SESSION"


def test_status_sync_says_when_a_date_is_outside_the_calendar(client: TestClient) -> None:
    body = client.get("/status/sync", params={"date": UNCOVERED.isoformat()}).json()
    assert body["day_kind"] is None and body["expects_data"] is None


def test_status_sources_reports_last_success_lag_and_failure_streak(
    client: TestClient, store: SyncStateStore
) -> None:
    """Acceptance 3, second half — every number computed from the rows above it."""
    publish(store, BHAVCOPY, PREVIOUS_SESSION)
    store.mark_gap(BHAVCOPY, WEEKEND)
    store.begin(BHAVCOPY, SESSION)
    store.mark_failed(BHAVCOPY, SESSION, "502 from nsearchives")
    publish(store, DELIVERY, SESSION)

    body = client.get("/status/sources").json()
    assert body["as_of"] == NOW.date().isoformat()
    by_source = {entry["source"]: entry for entry in body["sources"]}
    assert set(by_source) == {BHAVCOPY, DELIVERY}

    bhavcopy = by_source[BHAVCOPY]
    assert bhavcopy["last_success_date"] == PREVIOUS_SESSION.isoformat()
    assert bhavcopy["failure_streak"] == 1
    assert bhavcopy["last_error"] == "502 from nsearchives"
    assert bhavcopy["last_failure_date"] == SESSION.isoformat()
    assert bhavcopy["healthy"] is False
    # 2026-08-06 published; 08-07 is a session, 08-08/09 a weekend, 08-10 (today) a session.
    assert bhavcopy["lag_days"] == 4
    assert bhavcopy["lag_sessions"] == 2
    assert bhavcopy["counts"]["PUBLISHED"] == 1 and bhavcopy["counts"]["GAP"] == 1

    delivery = by_source[DELIVERY]
    assert delivery["last_success_date"] == SESSION.isoformat()
    assert delivery["failure_streak"] == 0
    assert delivery["lag_sessions"] == 1, "today is a session and has not published yet"
    assert delivery["healthy"] is True


def test_a_failure_streak_counts_consecutive_failures_and_ignores_holidays(
    store: SyncStateStore,
) -> None:
    """A closed exchange in the middle of a bad run must not reset the streak."""
    publish(store, BHAVCOPY, date(2026, 8, 11))
    for day in (date(2026, 8, 12), date(2026, 8, 13), date(2026, 8, 14)):
        store.begin(BHAVCOPY, day)
        store.mark_failed(BHAVCOPY, day, "502")
    store.mark_gap(BHAVCOPY, LATER_WEEKEND)  # 2026-08-15, a Saturday in the middle of the run

    status = next(s for s in store.source_statuses() if s.source == BHAVCOPY)
    assert status.failure_streak == 3
    assert status.last_success_date == date(2026, 8, 11)


def test_one_success_ends_the_failure_streak(store: SyncStateStore) -> None:
    for day in (date(2026, 8, 11), date(2026, 8, 12)):
        store.begin(BHAVCOPY, day)
        store.mark_failed(BHAVCOPY, day, "502")
    publish(store, BHAVCOPY, date(2026, 8, 13))

    status = next(s for s in store.source_statuses() if s.source == BHAVCOPY)
    assert status.failure_streak == 0
    assert status.healthy is True


def test_a_source_that_never_succeeded_reports_unknown_lag_not_zero(
    store: SyncStateStore,
) -> None:
    """`None` means unknown; a 0 here would read as "perfectly up to date"."""
    store.begin(FLOWS, SESSION)
    store.mark_failed(FLOWS, SESSION, "the endpoint moved")

    status = next(s for s in store.source_statuses() if s.source == FLOWS)
    assert status.last_success_date is None
    assert status.lag_days is None and status.lag_sessions is None
    assert status.failure_streak == 1
    assert status.healthy is False


def test_lag_is_zero_on_the_day_the_source_published(store: SyncStateStore) -> None:
    """Counting sessions, not calendar days: a source current to today is not a day behind."""
    publish(store, BHAVCOPY, NOW.date())
    status = next(s for s in store.source_statuses() if s.source == BHAVCOPY)
    assert status.lag_days == 0 and status.lag_sessions == 0


def test_lag_sessions_is_unknown_rather_than_wrong_outside_the_calendar(
    store: SyncStateStore,
) -> None:
    """A source whose last success predates the holiday file cannot be measured in sessions."""
    conn_days = (NOW.date() - UNCOVERED).days
    publish(store, FLOWS, UNCOVERED)
    status = next(s for s in store.source_statuses() if s.source == FLOWS)
    assert status.lag_days == conn_days
    assert status.lag_sessions is None


def test_status_sources_lists_only_sources_with_rows(client: TestClient) -> None:
    """Nothing ingested means nothing to report — not a fabricated line per registered source."""
    assert client.get("/status/sources").json()["sources"] == []


def test_the_endpoints_do_not_disturb_the_liveness_probe(client: TestClient) -> None:
    """Wiring a database into the app must not cost it the probe that says it is up.

    Asserts only that `/health` still answers and still carries a `status` — its payload is M0.5's
    contract and is asserted in that task's own suite. What M1.3 owes is that adding two
    DB-backed routes did not turn the liveness probe into a database dependency.
    """
    response = client.get("/health")
    assert response.status_code == 200
    assert "status" in response.json()


def test_lag_never_goes_negative_for_a_future_dated_publish(store: SyncStateStore) -> None:
    """A resumed backfill can publish a date ahead of the frozen clock in a replay."""
    publish(store, BHAVCOPY, NOW.date() + timedelta(days=7))
    status = next(s for s in store.source_statuses() if s.source == BHAVCOPY)
    assert status.lag_days == 0 and status.lag_sessions == 0
