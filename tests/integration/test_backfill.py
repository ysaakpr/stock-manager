"""M1.9 acceptance: the resumable, checkpointed backfill runner end to end.

Every acceptance criterion of the task is a test here, and every one runs offline (B8): the
network is a `RecordedTransport` scripted with the real checked-in bhavcopy fixtures, and the
`sync_state` checkpoint lives in a scratch Postgres created for the session and dropped afterwards.
No socket is opened to any exchange.

  1. `--dry-run` over ten years plans an accurate request count and touches nothing external
     (`test_dry_run_*`, `test_sample_*`) — these need neither the network nor a database.
  2. a sampled run spanning both bhavcopy eras lands every session in L1 with `sync_state`
     PUBLISHED (`test_backfill_publishes_across_both_eras`).
  3. killing mid-run and restarting resumes without re-fetching completed dates and without gaps
     (`test_graceful_stop_then_resume_*`, `test_crash_midrun_resumes_the_failed_date`).
  4. a 403 spike hard-stops the whole run rather than being routed around
     (`test_403_spike_hard_stops_the_run`).

The five fixture sessions span the 2024-07-08 UDiFF cutover — three legacy (2016, 2020, Jul-2024)
and two UDiFF (Jul-2024, Aug-2026) — so a run over them exercises both parsers, which is the
"spanning both eras" the sampling is built to guarantee on the live path.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Final

import psycopg
import pytest

from dataplatform.alerts import build_alerter
from dataplatform.clock import FrozenClock
from dataplatform.config import Settings
from dataplatform.ingest.backfill import (
    NSE_BHAVCOPY,
    SOURCE_SETS,
    BackfillRunner,
    FetchRequest,
    build_plan,
    sample_dates,
)
from dataplatform.ingest.calendar import trading_calendar
from dataplatform.ingest.fetcher import (
    Fetcher,
    RecordedResponse,
    RecordedTransport,
    TransportError,
)
from dataplatform.ingest.nse import bhavcopy
from dataplatform.ingest.source_register import SourceRegister
from dataplatform.ingest.source_register import load as load_register
from dataplatform.status.sync_state import SyncState, SyncStateStore
from dataplatform.store.db import Connection, connect, connection, with_dbname
from dataplatform.store.l0 import L0Store
from dataplatform.store.l1 import read_prices_raw
from dataplatform.store.migrate import migrate
from dataplatform.store.paths import l1_partition_path

REPO_ROOT: Final = Path(__file__).resolve().parent.parent.parent
FIXTURES: Final = REPO_ROOT / "tests" / "fixtures" / "nse_bhavcopy"

#: The five real fixture sessions, ascending, spanning the UDiFF cutover (2024-07-08).
FIXTURE_FILES: Final[dict[date, Path]] = {
    date(2016, 1, 1): FIXTURES / "legacy" / "cm01JAN2016bhav.csv.zip",
    date(2020, 3, 23): FIXTURES / "legacy" / "cm23MAR2020bhav.csv.zip",
    date(2024, 7, 5): FIXTURES / "legacy" / "cm05JUL2024bhav.csv.zip",
    date(2024, 7, 8): FIXTURES / "udiff" / "BhavCopy_NSE_CM_0_0_0_20240708_F_0000.csv.zip",
    date(2026, 8, 7): FIXTURES / "udiff" / "BhavCopy_NSE_CM_0_0_0_20260807_F_0000.csv.zip",
}
FIXTURE_DATES: Final = tuple(FIXTURE_FILES)

NOW: Final = date(2026, 8, 10)
SCRATCH_DB: Final = f"trading_m1_9_backfill_{os.getpid()}"


# ── offline planning tests (no database, no network) ─────────────────────────────────────────


def test_dry_run_plans_every_session_in_the_range() -> None:
    """`--dry-run`'s plan is one request per expected-data date — accurate and computed offline."""
    calendar = trading_calendar()
    register = load_register()
    plan = build_plan(
        SOURCE_SETS[NSE_BHAVCOPY],
        date(2016, 1, 1),
        date(2026, 8, 7),
        calendar=calendar,
        register=register,
    )
    expected = calendar.expected_data_dates(date(2016, 1, 1), date(2026, 8, 7))
    assert len(plan) == len(expected)
    assert [r.trade_date for r in plan] == expected


def test_sample_spreads_across_both_eras() -> None:
    """`--limit` samples evenly, so a decade sampled to 60 spans legacy and UDiFF alike (B1)."""
    calendar = trading_calendar()
    plan = build_plan(
        SOURCE_SETS[NSE_BHAVCOPY],
        date(2016, 1, 1),
        date(2026, 8, 7),
        calendar=calendar,
        register=load_register(),
        limit=60,
    )
    assert len(plan) == 60
    eras = {bhavcopy.era_of(r.trade_date) for r in plan}
    assert eras == {"legacy", "udiff"}
    # First and last of the range are always in the sample, so the span is never truncated.
    assert plan[0].trade_date == date(2016, 1, 1)
    assert plan[-1].trade_date == date(2026, 8, 7)


def test_sample_dates_edges_and_degenerate_limits() -> None:
    """The sampler keeps endpoints, dedupes, and rejects a non-positive limit."""
    dates = [date(2020, 1, d) for d in range(1, 11)]
    assert sample_dates(dates, None) == dates
    assert sample_dates(dates, 100) == dates
    assert sample_dates(dates, 1) == [dates[0]]
    picked = sample_dates(dates, 4)
    assert picked[0] == dates[0] and picked[-1] == dates[-1]
    assert picked == sorted(set(picked))
    with pytest.raises(ValueError):
        sample_dates(dates, 0)


# ── database-backed run tests ────────────────────────────────────────────────────────────────
#
# These need the docker postgres; the `scratch_db` fixture skips loudly when it is unreachable.


def _settings_for(dbname: str, data_root: Path) -> Settings:
    """Settings pointing at a named database on the configured server, with a chosen lake root."""
    return Settings(database_url=with_dbname(Settings().database_url, dbname), data_root=data_root)


@pytest.fixture(scope="session")
def scratch_db() -> Iterator[str]:
    """A scratch database created for the session and dropped afterwards (never the dev DB)."""
    admin = Settings(database_url=with_dbname(Settings().database_url, "postgres"))
    try:
        conn = connect(admin, autocommit=True)
    except psycopg.OperationalError as error:  # pragma: no cover - environment, not logic
        pytest.skip(f"postgres is not reachable — run `make up` first: {error}")
    try:
        conn.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{SCRATCH_DB}"')
    finally:
        conn.close()

    migrate(_settings_for(SCRATCH_DB, Path("data")), clock=FrozenClock(NOW))
    yield SCRATCH_DB

    conn = connect(admin, autocommit=True)
    try:
        conn.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)')
    finally:
        conn.close()


@pytest.fixture
def settings(scratch_db: str, tmp_path: Path) -> Settings:
    """Scratch DB plus a per-test lake root under tmp_path, so L0 and L1 never touch the repo."""
    return _settings_for(scratch_db, tmp_path)


@pytest.fixture
def conn(settings: Settings) -> Iterator[Connection]:
    """A committed connection to the scratch DB, with `sync_state` truncated for isolation.

    The runner commits after every session (that commit is its checkpoint), so tests cannot lean
    on transaction rollback for isolation the way the state-machine tests do — they truncate the
    table up front and let each run's commits stand.
    """
    with connection(settings) as live:
        live.execute("TRUNCATE sync_state")
        live.commit()
        yield live


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW)


@pytest.fixture
def register() -> SourceRegister:
    return load_register()


@pytest.fixture
def store(conn: Connection, clock: FrozenClock) -> SyncStateStore:
    return SyncStateStore(conn, clock=clock, calendar=trading_calendar())


def _requests(
    register: SourceRegister, days: tuple[date, ...] = FIXTURE_DATES
) -> list[FetchRequest]:
    """The fetch requests for the given sessions, built exactly as the runner builds them."""
    return [SOURCE_SETS[NSE_BHAVCOPY].build_request(day, register) for day in days]


def _ok_transport(days: tuple[date, ...] = FIXTURE_DATES) -> RecordedTransport:
    """A transport that serves each session's real fixture bytes as a 200 zip."""
    register = load_register()
    script: dict[str, RecordedResponse] = {}
    for day in days:
        request = SOURCE_SETS[NSE_BHAVCOPY].build_request(day, register)
        script[request.url] = RecordedResponse(
            status_code=200,
            body=FIXTURE_FILES[day].read_bytes(),
            headers={"content-type": "application/zip"},
        )
    return RecordedTransport(script)


def _fetcher(
    transport: RecordedTransport, settings: Settings, clock: FrozenClock, register: SourceRegister
) -> Fetcher:
    """A fetcher wired to a recorded transport with a no-op sleep — offline and instant."""
    return Fetcher(
        transport=transport,
        l0=L0Store(clock=clock, data_root=settings.data_root),
        alerter=build_alerter(settings, clock=clock),
        clock=clock,
        register=register,
        settings=settings,
        sleep=lambda _seconds: None,
    )


def _runner(
    transport: RecordedTransport,
    *,
    settings: Settings,
    conn: Connection,
    clock: FrozenClock,
    register: SourceRegister,
    should_stop: object = None,
) -> BackfillRunner:
    store = SyncStateStore(conn, clock=clock, calendar=trading_calendar())
    kwargs = {} if should_stop is None else {"should_stop": should_stop}
    return BackfillRunner(
        SOURCE_SETS[NSE_BHAVCOPY],
        fetcher=_fetcher(transport, settings, clock, register),
        l0=L0Store(clock=clock, data_root=settings.data_root),
        sync=store,
        commit=conn.commit,
        **kwargs,  # type: ignore[arg-type]
    )


def _urls_fetched(transport: RecordedTransport) -> list[str]:
    return [r.url for r in transport.requests]


# ── acceptance 2: a sampled run spanning both eras lands in L1, PUBLISHED ─────────────────────


def test_backfill_publishes_across_both_eras(
    settings: Settings,
    conn: Connection,
    clock: FrozenClock,
    register: SourceRegister,
    store: SyncStateStore,
) -> None:
    transport = _ok_transport()
    report = _runner(transport, settings=settings, conn=conn, clock=clock, register=register).run(
        _requests(register)
    )

    assert report.published == len(FIXTURE_DATES)
    assert report.failed == 0
    assert not report.hard_stopped and not report.stopped_early

    for day in FIXTURE_DATES:
        record = store.get(NSE_BHAVCOPY, day)
        assert record is not None and record.state is SyncState.PUBLISHED
        # The session really landed in L1, not merely a state row.
        partition = l1_partition_path("prices_raw", day, data_root=settings.data_root)
        assert partition.exists()
        assert len(read_prices_raw(day, data_root=settings.data_root)) > 0

    # Both eras were actually exercised (legacy and UDiFF URL patterns both requested).
    fetched = _urls_fetched(transport)
    assert any("historical/EQUITIES" in u for u in fetched)  # legacy
    assert any("BhavCopy_NSE_CM" in u for u in fetched)  # udiff


def test_second_run_refetches_nothing_and_is_a_no_op(
    settings: Settings,
    conn: Connection,
    clock: FrozenClock,
    register: SourceRegister,
) -> None:
    """Re-running a completed backfill fetches nothing: every date is already PUBLISHED."""
    _runner(_ok_transport(), settings=settings, conn=conn, clock=clock, register=register).run(
        _requests(register)
    )

    second = _ok_transport()
    report = _runner(second, settings=settings, conn=conn, clock=clock, register=register).run(
        _requests(register)
    )
    assert report.published == 0
    assert report.skipped_published == len(FIXTURE_DATES)
    assert _urls_fetched(second) == []


# ── acceptance 3: kill mid-run, resume without re-fetch and without gaps ──────────────────────


def test_graceful_stop_then_resume_covers_every_date_once(
    settings: Settings,
    conn: Connection,
    clock: FrozenClock,
    register: SourceRegister,
    store: SyncStateStore,
) -> None:
    requests = _requests(register)

    # First run: a SIGINT-style stop after three sessions are committed.
    processed: dict[str, int] = {"n": 0}

    def stop_after_three() -> bool:
        stop = processed["n"] >= 3
        processed["n"] += 1
        return stop

    first_transport = _ok_transport()
    first = _runner(
        first_transport,
        settings=settings,
        conn=conn,
        clock=clock,
        register=register,
        should_stop=stop_after_three,
    ).run(requests)
    assert first.stopped_early
    assert first.published == 3
    first_urls = _urls_fetched(first_transport)
    assert len(first_urls) == 3
    # The last two dates were never touched — no partial row, no fetch.
    for day in FIXTURE_DATES[3:]:
        assert store.get(NSE_BHAVCOPY, day) is None

    # Second run: resumes, re-fetches none of the first three, completes the rest, no gaps.
    second_transport = _ok_transport()
    second = _runner(
        second_transport, settings=settings, conn=conn, clock=clock, register=register
    ).run(requests)
    assert second.skipped_published == 3
    assert second.published == 2
    second_urls = _urls_fetched(second_transport)
    assert len(second_urls) == 2
    assert not set(first_urls) & set(second_urls)  # nothing re-fetched

    # Every planned session is now PUBLISHED — no gaps.
    for day in FIXTURE_DATES:
        record = store.get(NSE_BHAVCOPY, day)
        assert record is not None and record.state is SyncState.PUBLISHED


def test_crash_midrun_resumes_the_failed_date(
    settings: Settings,
    conn: Connection,
    clock: FrozenClock,
    register: SourceRegister,
    store: SyncStateStore,
) -> None:
    """A session whose fetch dies is recorded FAILED, and the next run picks exactly it up."""
    broken_day = FIXTURE_DATES[1]
    broken_request = SOURCE_SETS[NSE_BHAVCOPY].build_request(broken_day, register)

    # A transport that serves everything but times out on one session's URL.
    script: dict[str, object] = {}
    for day in FIXTURE_DATES:
        request = SOURCE_SETS[NSE_BHAVCOPY].build_request(day, register)
        if day == broken_day:
            script[request.url] = TransportError("connection reset")
        else:
            script[request.url] = RecordedResponse(
                status_code=200,
                body=FIXTURE_FILES[day].read_bytes(),
                headers={"content-type": "application/zip"},
            )
    first_transport = RecordedTransport(script)  # type: ignore[arg-type]

    first = _runner(
        first_transport, settings=settings, conn=conn, clock=clock, register=register
    ).run(_requests(register))
    assert first.failed == 1
    assert first.published == len(FIXTURE_DATES) - 1
    broken = store.get(NSE_BHAVCOPY, broken_day)
    assert broken is not None and broken.state is SyncState.FAILED and broken.retryable

    # Second run with a healthy transport: only the failed date is fetched again.
    second_transport = _ok_transport()
    second = _runner(
        second_transport, settings=settings, conn=conn, clock=clock, register=register
    ).run(_requests(register))
    assert second.published == 1
    assert second.skipped_published == len(FIXTURE_DATES) - 1
    assert _urls_fetched(second_transport) == [broken_request.url]

    for day in FIXTURE_DATES:
        record = store.get(NSE_BHAVCOPY, day)
        assert record is not None and record.state is SyncState.PUBLISHED


# ── acceptance 4: a 403 spike is a hard stop ─────────────────────────────────────────────────


def test_403_spike_hard_stops_the_run(
    settings: Settings,
    conn: Connection,
    clock: FrozenClock,
    register: SourceRegister,
    store: SyncStateStore,
) -> None:
    """Consecutive 403s trip the fetcher's spike hard stop; the run ends and later dates are left.

    The forbidden-streak limit defaults to 3, so the third session's 403 raises
    `ForbiddenSpikeError` inside the fetcher and the runner stops the whole run rather than lowering
    the rate or rotating the agent (AGENTIC_CONTEXT §8).
    """
    assert settings.http_forbidden_streak_limit == 3
    script: dict[str, RecordedResponse] = {}
    for day in FIXTURE_DATES:
        request = SOURCE_SETS[NSE_BHAVCOPY].build_request(day, register)
        script[request.url] = RecordedResponse(status_code=403, body=b"forbidden")
    transport = RecordedTransport(script)

    report = _runner(transport, settings=settings, conn=conn, clock=clock, register=register).run(
        _requests(register)
    )

    assert report.hard_stopped
    # Three sessions were attempted (the third trips the spike); the rest were never reached.
    assert report.failed == 3
    assert report.published == 0
    for day in FIXTURE_DATES[3:]:
        assert store.get(NSE_BHAVCOPY, day) is None
    # The tripping session is non-retryable: the process will not talk to this host again.
    tripped = store.get(NSE_BHAVCOPY, FIXTURE_DATES[2])
    assert tripped is not None and tripped.state is SyncState.FAILED and not tripped.retryable
