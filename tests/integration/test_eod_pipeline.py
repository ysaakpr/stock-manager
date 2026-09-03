"""M1.10 acceptance: the daily EOD pipeline job, end to end and offline.

Every acceptance criterion of the task is a test here, and every one runs offline (B8): the network
is a `RecordedTransport` scripted with the real checked-in bhavcopy fixtures, and `sync_state`, the
L1 lake and the archive bundle all live in a scratch Postgres and a `tmp_path` lake created for the
test and dropped afterwards. No socket is opened to any exchange.

  1. one invocation takes a session from PENDING to PUBLISHED for all daily NSE sources
     (`test_one_invocation_publishes_the_session`).
  2. an induced failure (simulated 500) is retried on the next run and self-heals to PUBLISHED
     (`test_induced_500_is_retried_and_self_heals`, and the cross-day form in
     `test_self_heals_a_prior_days_failure`).
  3. a second run for the same session is a no-op
     (`test_second_run_for_the_same_session_is_a_no_op`).

The two fixture sessions used straddle the 2024-07-08 UDiFF cutover — 2024-07-05 is legacy and
2024-07-08 is UDiFF — so the runs exercise both bhavcopy parsers exactly as the daily job would.

Beyond the three task criteria, `test_five_consecutive_sessions_run_unattended_with_one_self_heal`
is the driver the M1 gate's box 3 demands — "daily EOD job runs unattended 5 consecutive sessions
incl. self-heal on one induced failure" (EXECUTION_PLAN §9). One loop advances the clock through
five consecutive sessions with no human step; the middle session 500s and the next day's run
self-heals it. Five consecutive sessions need five days of bytes and the suite ships two, so
`_udiff_bytes_for` re-dates the real checked-in UDiFF fixture (only its two ISO date columns) rather
than fabricating a bhavcopy — the run stays offline and every row is the exchange's real shape.
"""

from __future__ import annotations

import io
import os
import zipfile
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path
from typing import Final

import psycopg
import pytest

from dataplatform.alerts import AlertOutcome, Severity
from dataplatform.clock import FrozenClock
from dataplatform.config import Settings
from dataplatform.ingest.backfill import NSE_BHAVCOPY, SOURCE_SETS
from dataplatform.ingest.calendar import trading_calendar
from dataplatform.ingest.eod import DAILY_NSE_SOURCES, EodPipeline
from dataplatform.ingest.fetcher import Fetcher, RecordedResponse, RecordedTransport
from dataplatform.ingest.source_register import SourceRegister
from dataplatform.ingest.source_register import load as load_register
from dataplatform.status.sync_state import SyncState, SyncStateStore
from dataplatform.store.db import Connection, connect, connection, with_dbname
from dataplatform.store.l0 import L0Store
from dataplatform.store.l1 import read_prices_raw
from dataplatform.store.migrate import migrate
from dataplatform.store.paths import l1_partition_path

pytestmark = pytest.mark.integration

REPO_ROOT: Final = Path(__file__).resolve().parent.parent.parent
FIXTURES: Final = REPO_ROOT / "tests" / "fixtures" / "nse_bhavcopy"

#: A legacy session (Fri) and the UDiFF cutover session (Mon), each with its real fixture bytes.
LEGACY_SESSION: Final = date(2024, 7, 5)
UDIFF_SESSION: Final = date(2024, 7, 8)
FIXTURE_FILES: Final[dict[date, Path]] = {
    LEGACY_SESSION: FIXTURES / "legacy" / "cm05JUL2024bhav.csv.zip",
    UDIFF_SESSION: FIXTURES / "udiff" / "BhavCopy_NSE_CM_0_0_0_20240708_F_0000.csv.zip",
}

SCRATCH_DB: Final = f"trading_m1_10_eod_{os.getpid()}"


# ── an alerter that records rather than logs, so alerts are asserted, not observed ─────────────


class RecordingAlerter:
    """An `Alerter` that keeps every send in memory. Deduplication is not exercised here."""

    def __init__(self) -> None:
        self.sent: list[tuple[Severity, str, str]] = []

    def send(self, severity: Severity, title: str, body: str, dedup_key: str) -> AlertOutcome:
        self.sent.append((severity, title, dedup_key))
        return AlertOutcome.SENT


# ── database + lake fixtures (mirror the M1.9 backfill suite) ─────────────────────────────────


def _settings_for(dbname: str, data_root: Path) -> Settings:
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

    migrate(_settings_for(SCRATCH_DB, Path("data")), clock=FrozenClock(UDIFF_SESSION))
    yield SCRATCH_DB

    conn = connect(admin, autocommit=True)
    try:
        conn.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)')
    finally:
        conn.close()


@pytest.fixture
def settings(scratch_db: str, tmp_path: Path) -> Settings:
    """Scratch DB plus a per-test lake root under tmp_path, keeping L0/L1/archives off the repo."""
    return _settings_for(scratch_db, tmp_path)


@pytest.fixture
def conn(settings: Settings) -> Iterator[Connection]:
    """A committed connection to the scratch DB, with the tables this job writes cleared.

    The pipeline commits after every session and after the bundle row, so tests cannot lean on
    rollback for isolation — they truncate the tables up front and let each run's commits stand.
    """
    with connection(settings) as live:
        live.execute("TRUNCATE sync_state")
        live.execute("TRUNCATE archive_bundle")
        live.commit()
        yield live


@pytest.fixture
def register() -> SourceRegister:
    return load_register()


# ── helpers ───────────────────────────────────────────────────────────────────────────────────


def _url_for(day: date, register: SourceRegister) -> str:
    """The exact URL the runner would fetch for one NSE cash session."""
    return SOURCE_SETS[NSE_BHAVCOPY].build_request(day, register).url


def _transport(
    register: SourceRegister,
    *,
    ok: tuple[date, ...] = (),
    fail_500: tuple[date, ...] = (),
) -> RecordedTransport:
    """Serve each `ok` date's real fixture as a 200 zip and each `fail_500` date as a 500."""
    script: dict[str, RecordedResponse] = {}
    for day in ok:
        script[_url_for(day, register)] = RecordedResponse(
            status_code=200,
            body=FIXTURE_FILES[day].read_bytes(),
            headers={"content-type": "application/zip"},
        )
    for day in fail_500:
        script[_url_for(day, register)] = RecordedResponse(status_code=500, body=b"upstream error")
    return RecordedTransport(script)


def _fetcher(
    transport: RecordedTransport, settings: Settings, clock: FrozenClock, register: SourceRegister
) -> Fetcher:
    """A fetcher wired to a recorded transport with a no-op sleep — offline and instant."""
    return Fetcher(
        transport=transport,
        l0=L0Store(clock=clock, data_root=settings.data_root),
        alerter=RecordingAlerter(),
        clock=clock,
        register=register,
        settings=settings,
        sleep=lambda _seconds: None,
    )


def _pipeline(
    transport: RecordedTransport,
    *,
    settings: Settings,
    conn: Connection,
    clock: FrozenClock,
    register: SourceRegister,
    alerter: RecordingAlerter,
    lookback: timedelta,
) -> EodPipeline:
    return EodPipeline(
        conn=conn,
        fetcher=_fetcher(transport, settings, clock, register),
        l0=L0Store(clock=clock, data_root=settings.data_root),
        alerter=alerter,
        clock=clock,
        calendar=trading_calendar(),
        register=register,
        archive_root=settings.data_root,
        data_root=settings.data_root,
        lookback=lookback,
    )


def _store(conn: Connection, clock: FrozenClock) -> SyncStateStore:
    return SyncStateStore(conn, clock=clock, calendar=trading_calendar())


def _urls_fetched(transport: RecordedTransport) -> list[str]:
    return [r.url for r in transport.requests]


# ── five-consecutive-session driver helpers (M1 gate box 3) ────────────────────────────────────

#: Five consecutive UDiFF-era sessions (Mon-Fri, 2024-07-08 .. 2024-07-12), all present in the C.2
#: calendar with no holiday between them — the run the M1 gate's "5 consecutive sessions" box needs.
CONSECUTIVE_SESSIONS: Final[tuple[date, ...]] = (
    date(2024, 7, 8),
    date(2024, 7, 9),
    date(2024, 7, 10),
    date(2024, 7, 11),
    date(2024, 7, 12),
)
#: The middle session, made to 500 on its own run so a later run must self-heal it unattended.
INDUCED_FAILURE_SESSION: Final = date(2024, 7, 10)


def _udiff_bytes_for(day: date) -> bytes:
    """Real UDiFF fixture bytes re-dated to `day` — bytes for an arbitrary consecutive session.

    A five-session run needs five days of bhavcopy and the suite ships two real dates. Rather than
    fabricate a bhavcopy from nothing, this re-dates the checked-in UDiFF fixture: it rewrites only
    the two ISO date columns (`TradDt`, `BizDt`) — the ones the parser reads as the session date and
    the L1 writer partitions by — and keeps every other column, every one of the real 2,815 symbol
    rows, and the exchange's exact shape. The parser reads the single zip member by position, so the
    member name is left descriptive and never load-bearing.
    """
    with zipfile.ZipFile(FIXTURE_FILES[UDIFF_SESSION]) as archive:
        member = archive.namelist()[0]
        lines = archive.read(member).decode("utf-8").splitlines()
    header = lines[0].split(",")
    trad_dt, biz_dt = header.index("TradDt"), header.index("BizDt")
    iso = day.isoformat()
    out = [lines[0]]
    for line in lines[1:]:
        if not line.strip():
            continue
        fields = line.split(",")
        fields[trad_dt] = iso
        fields[biz_dt] = iso
        out.append(",".join(fields))
    body = ("\n".join(out) + "\n").encode("utf-8")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zipped:
        zipped.writestr(f"BhavCopy_NSE_CM_0_0_0_{day:%Y%m%d}_F_0000.csv", body)
    return buffer.getvalue()


def _consecutive_transport(
    register: SourceRegister, *, fail_500: tuple[date, ...] = ()
) -> RecordedTransport:
    """Serve each consecutive session's re-dated UDiFF bytes as 200, except `fail_500` dates as 500.

    Scripting all five URLs every run is harmless — the runner only fetches the dates its plan names
    — and it models a real upstream honestly: the failing day returns 500 on its own run and 200
    afterwards, which is exactly what lets the next run's self-heal step land it.
    """
    script: dict[str, RecordedResponse] = {}
    for day in CONSECUTIVE_SESSIONS:
        url = _url_for(day, register)
        if day in fail_500:
            script[url] = RecordedResponse(status_code=500, body=b"upstream error")
        else:
            script[url] = RecordedResponse(
                status_code=200,
                body=_udiff_bytes_for(day),
                headers={"content-type": "application/zip"},
            )
    return RecordedTransport(script)


# ── acceptance 1: one invocation, PENDING → PUBLISHED for every daily NSE source ──────────────


def test_one_invocation_publishes_the_session(
    settings: Settings, conn: Connection, register: SourceRegister
) -> None:
    """One run lands the latest session in L1, PUBLISHED, with a clean gap report and archive."""
    clock = FrozenClock(UDIFF_SESSION)
    alerter = RecordingAlerter()
    pipeline = _pipeline(
        _transport(register, ok=(UDIFF_SESSION,)),
        settings=settings,
        conn=conn,
        clock=clock,
        register=register,
        alerter=alerter,
        lookback=timedelta(days=2),  # window = Sat, Sun, the target Monday — no un-run sessions
    )

    report = pipeline.run()

    assert report.logical_date == UDIFF_SESSION
    assert report.session_published
    assert report.sources == DAILY_NSE_SOURCES

    # Every daily NSE source is PUBLISHED for the session, and the data really landed in L1.
    store = _store(conn, clock)
    for source in DAILY_NSE_SOURCES:
        record = store.get(source, UDIFF_SESSION)
        assert record is not None and record.state is SyncState.PUBLISHED
    assert l1_partition_path("prices_raw", UDIFF_SESSION, data_root=settings.data_root).exists()
    assert len(read_prices_raw(UDIFF_SESSION, data_root=settings.data_root)) > 0

    # The gap check ran and found nothing to explain over the clean window.
    assert report.gap_report is not None and report.gap_report.fully_explained
    assert alerter.sent == []  # nothing failed, so nothing was alerted

    # The archive bundle was published for the session and is on disk with its manifest.
    assert report.archive is not None
    assert report.archive.logical_date == UDIFF_SESSION
    assert (report.archive.bundle_dir / "manifest.json").exists()
    row = conn.execute(
        "SELECT count(*) FROM archive_bundle WHERE logical_date = %s", (UDIFF_SESSION,)
    ).fetchone()
    assert row is not None and row[0] == 1


# ── acceptance 2: an induced 500 is retried on the next run and self-heals to PUBLISHED ────────


def test_induced_500_is_retried_and_self_heals(
    settings: Settings, conn: Connection, register: SourceRegister
) -> None:
    """First run 500s the session (FAILED, retryable, alerted); the next run heals it."""
    clock = FrozenClock(UDIFF_SESSION)

    # Run 1: the source 500s. The session is left FAILED(retryable) and an alert is emitted.
    failing = _transport(register, fail_500=(UDIFF_SESSION,))
    alerter_1 = RecordingAlerter()
    first = _pipeline(
        failing,
        settings=settings,
        conn=conn,
        clock=clock,
        register=register,
        alerter=alerter_1,
        lookback=timedelta(days=2),
    ).run()

    assert not first.session_published
    assert first.archive is None  # nothing to archive for a session that never published
    store = _store(conn, clock)
    failed = store.get(NSE_BHAVCOPY, UDIFF_SESSION)
    assert failed is not None and failed.state is SyncState.FAILED and failed.retryable
    assert any(
        key == f"eod:{NSE_BHAVCOPY}:{UDIFF_SESSION.isoformat()}:FAILED"
        for _sev, _title, key in alerter_1.sent
    ), "a FAILED source must be alerted"

    # Run 2: the source is healthy again. The pipeline self-heals the retryable date to PUBLISHED.
    healthy = _transport(register, ok=(UDIFF_SESSION,))
    alerter_2 = RecordingAlerter()
    second = _pipeline(
        healthy,
        settings=settings,
        conn=conn,
        clock=clock,
        register=register,
        alerter=alerter_2,
        lookback=timedelta(days=2),
    ).run()

    assert second.session_published
    healed = store.get(NSE_BHAVCOPY, UDIFF_SESSION)
    assert healed is not None and healed.state is SyncState.PUBLISHED
    assert healed.attempts >= 2, "the retry is a second attempt on the same row, not a fresh row"
    assert len(read_prices_raw(UDIFF_SESSION, data_root=settings.data_root)) > 0
    assert second.archive is not None  # the now-complete session is archived
    assert alerter_2.sent == []  # healed: nothing left to alert


def test_self_heals_a_prior_days_failure(
    settings: Settings, conn: Connection, register: SourceRegister
) -> None:
    """A session that failed on an earlier day is picked up by a later run's self-heal step.

    This is the case the same-session retry cannot prove: Friday's fetch failed, and *Monday's* run
    — for a different target — re-attempts Friday within the lookback window before doing its own
    work, so both land PUBLISHED without a human.
    """
    # Friday's run: 2024-07-05 (legacy era) 500s and is left FAILED(retryable).
    friday_clock = FrozenClock(LEGACY_SESSION)
    first = _pipeline(
        _transport(register, fail_500=(LEGACY_SESSION,)),
        settings=settings,
        conn=conn,
        clock=friday_clock,
        register=register,
        alerter=RecordingAlerter(),
        lookback=timedelta(days=3),
    ).run()
    assert first.logical_date == LEGACY_SESSION and not first.session_published

    # Monday's run: target is 2024-07-08 (UDiFF era); the lookback reaches back to Friday.
    monday_clock = FrozenClock(UDIFF_SESSION)
    second = _pipeline(
        _transport(register, ok=(LEGACY_SESSION, UDIFF_SESSION)),
        settings=settings,
        conn=conn,
        clock=monday_clock,
        register=register,
        alerter=RecordingAlerter(),
        lookback=timedelta(days=3),
    ).run()

    assert second.logical_date == UDIFF_SESSION
    assert second.session_published
    assert LEGACY_SESSION in second.healed, "the prior day's failure was re-attempted this run"

    store = _store(conn, monday_clock)
    for day in (LEGACY_SESSION, UDIFF_SESSION):
        record = store.get(NSE_BHAVCOPY, day)
        assert record is not None and record.state is SyncState.PUBLISHED
    # Both eras really landed in L1, and the window now has nothing to explain.
    assert len(read_prices_raw(LEGACY_SESSION, data_root=settings.data_root)) > 0
    assert len(read_prices_raw(UDIFF_SESSION, data_root=settings.data_root)) > 0
    assert second.gap_report is not None and second.gap_report.fully_explained


# ── acceptance 3: a second run for the same session is a no-op ─────────────────────────────────


def test_second_run_for_the_same_session_is_a_no_op(
    settings: Settings, conn: Connection, register: SourceRegister
) -> None:
    """Re-running a published session fetches nothing, changes no state, and re-writes no bundle."""
    clock = FrozenClock(UDIFF_SESSION)

    first = _pipeline(
        _transport(register, ok=(UDIFF_SESSION,)),
        settings=settings,
        conn=conn,
        clock=clock,
        register=register,
        alerter=RecordingAlerter(),
        lookback=timedelta(days=2),
    ).run()
    assert first.session_published and first.archive is not None

    store = _store(conn, clock)
    before = store.get(NSE_BHAVCOPY, UDIFF_SESSION)
    assert before is not None

    # Second run, same session, a fresh transport that must be asked for nothing.
    second_transport = _transport(register, ok=(UDIFF_SESSION,))
    second = _pipeline(
        second_transport,
        settings=settings,
        conn=conn,
        clock=clock,
        register=register,
        alerter=RecordingAlerter(),
        lookback=timedelta(days=2),
    ).run()

    assert second.session_published
    assert _urls_fetched(second_transport) == [], "a no-op run opens no socket"
    assert second.archive is None, "the bundle already exists, so it is not re-written"
    for outcome in second.outcomes.values():
        assert outcome.report.published == 0
        assert outcome.report.skipped_published == 1

    # The sync_state row is byte-for-byte what the first run left — no new attempt, no re-publish.
    after = store.get(NSE_BHAVCOPY, UDIFF_SESSION)
    assert after is not None
    assert (after.state, after.attempts, after.updated_at) == (
        before.state,
        before.attempts,
        before.updated_at,
    )


# ── M1 gate box 3: 5 consecutive sessions unattended, incl. self-heal on one induced failure ────


def test_five_consecutive_sessions_run_unattended_with_one_self_heal(
    settings: Settings, conn: Connection, register: SourceRegister
) -> None:
    """One loop drives five consecutive sessions unattended; a mid-run failure self-heals next day.

    This is the driver the M1 gate's box 3 demands (EXECUTION_PLAN §9): the daily EOD job as an
    operable unattended loop, not a one-shot script. The clock advances one session at a time and
    the *same* pipeline runs each day with no human step in between. The middle session (2024-07-10)
    500s on its own run and is left FAILED(retryable) and alerted; the very next day's run
    re-attempts it within the lookback window while also publishing its own session — the self-heal
    happens with no human in the loop. At the end all five consecutive sessions are PUBLISHED in L1.
    """
    # Consecutive sessions are one day apart, so a two-day lookback reaches yesterday's straggler
    # and keeps the gap window to weekends — no un-run pre-fixture history to noise the run (matches
    # the tuned windows the other acceptance tests use).
    lookback = timedelta(days=2)
    healed_across_runs: set[date] = set()
    saw_induced_failure = False

    for session in CONSECUTIVE_SESSIONS:
        clock = FrozenClock(session)
        alerter = RecordingAlerter()
        fail_500 = (INDUCED_FAILURE_SESSION,) if session == INDUCED_FAILURE_SESSION else ()
        report = _pipeline(
            _consecutive_transport(register, fail_500=fail_500),
            settings=settings,
            conn=conn,
            clock=clock,
            register=register,
            alerter=alerter,
            lookback=lookback,
        ).run()

        healed_across_runs.update(report.healed)
        critical = [key for sev, _title, key in alerter.sent if sev is Severity.CRITICAL]

        if session == INDUCED_FAILURE_SESSION:
            saw_induced_failure = True
            assert not report.session_published, "the 500 must leave this session unpublished"
            assert report.archive is None, "a session that never published is not archived"
            assert f"eod:{NSE_BHAVCOPY}:{session.isoformat()}:FAILED" in critical, (
                "the induced failure must be alerted CRITICAL"
            )
        else:
            assert report.session_published, f"{session} should publish on its own run"
            assert critical == [], f"a healthy run for {session} raises no source-failed alert"

    # The failure really self-healed on a later run, with no human intervention.
    assert saw_induced_failure
    assert INDUCED_FAILURE_SESSION in healed_across_runs, (
        "the induced failure must be re-attempted by a later run's self-heal step, not by a human"
    )

    # Every one of the five consecutive sessions is now PUBLISHED and its data is in L1.
    store = _store(conn, FrozenClock(CONSECUTIVE_SESSIONS[-1]))
    for session in CONSECUTIVE_SESSIONS:
        record = store.get(NSE_BHAVCOPY, session)
        assert record is not None and record.state is SyncState.PUBLISHED, (
            f"{session} did not reach PUBLISHED after the unattended run"
        )
        assert len(read_prices_raw(session, data_root=settings.data_root)) > 0
