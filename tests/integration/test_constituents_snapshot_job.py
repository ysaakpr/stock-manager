"""M10.2 acceptance: the forward sector-history accumulation job (the survivorship-bias killer).

niftyindices publishes constituents "as of today" only — there is no download of who was in a
sector index in a past year, so a static-today map applied backward is survivorship-biased. This
job snapshots the broad and sectoral lists with a capture date once a week, appending a dated
membership record each run so *real* point-in-time sector history accrues going forward. This suite
is every acceptance criterion of the task, each written so a plausible wrong implementation fails:

  1. **A weekly scheduled job appends a dated snapshot for every configured slug.** One sweep lands
     an L1 partition per slug at the week's anchor date, queryable back through `membership_asof`,
     and the job is registered on a genuinely weekly cron
     (`test_the_weekly_job_appends_a_dated_snapshot_for_every_configured_slug`,
     `test_the_job_is_registered_on_a_weekly_cron`).
  2. **`membership_asof` returns the snapshot in force on a date, and a re-run of the same week is a
     no-op.** The snapshot is stamped the ISO week's anchor, so a second run anywhere in the same
     week re-fetches nothing and writes nothing — the transport is never touched — and the read is
     unchanged; a decision date before the anchor sees nothing, not a future map
     (`test_membership_asof_is_in_force_and_a_rerun_of_the_same_week_is_a_no_op`).
  3. **A fetch failure for one slug is journaled and alerted, and does not abort the others.** One
     slug 404s: its `sync_state` row is FAILED (journaled), exactly one alert names it, and every
     other slug still publishes and stays queryable
     (`test_a_slug_failure_is_journaled_and_alerted_and_does_not_abort_the_others`).

Offline by construction (B8): the network is a `RecordedTransport` scripted with the real checked-in
constituent CSVs, and `sync_state` and the L1 lake live in a scratch Postgres and a `tmp_path` lake
made for the test and dropped afterwards. No socket is opened to niftyindices. Needs the docker
postgres (`make up`).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Final

import psycopg
import pytest

from dataplatform.alerts import AlertOutcome, Severity
from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.ingest.constituents_ingest import (
    IndexCategory,
    IndexSpec,
    ParkCause,
    SlugStatus,
)
from dataplatform.ingest.constituents_snapshot_job import (
    run_weekly_snapshot,
    week_anchor,
)
from dataplatform.ingest.fetcher import (
    Fetcher,
    RecordedResponse,
    RecordedTransport,
    ScriptedOutcome,
)
from dataplatform.ingest.indices import (
    constituents_state_source,
    constituents_url,
    membership_asof,
)
from dataplatform.ingest.source_register import SourceRegister
from dataplatform.ingest.source_register import load as load_register
from dataplatform.scheduler import CONSTITUENTS_SNAPSHOT, default_registry
from dataplatform.status.sync_state import SyncState, SyncStateStore
from dataplatform.store.db import Connection, connect, connection, with_dbname
from dataplatform.store.l0 import L0Store
from dataplatform.store.migrate import migrate

pytestmark = pytest.mark.integration

REPO_ROOT: Final = Path(__file__).resolve().parent.parent.parent
FIXTURES: Final = REPO_ROOT / "tests" / "fixtures" / "nifty_indices" / "constituents"

#: A Saturday 20:00 IST — the day the weekly job's cron fires. `week_anchor` maps it to that ISO
#: week's Sunday (2026-09-06), the date every snapshot in the run is stamped with.
NOW: Final = datetime(2026, 9, 5, 20, 0, tzinfo=IST)
#: A second run later in the *same* ISO week (its Sunday), used to prove the per-week no-op.
SAME_WEEK_LATER: Final = datetime(2026, 9, 6, 9, 0, tzinfo=IST)

#: The slugs we hold real fixtures for at this date — one broad list plus nine sectoral/thematic.
BROAD: Final = IndexSpec("nifty500", "NIFTY 500", IndexCategory.BROAD)
SECTORALS: Final[tuple[IndexSpec, ...]] = (
    IndexSpec("niftybank", "NIFTY BANK", IndexCategory.SECTORAL),
    IndexSpec("niftyit", "NIFTY IT", IndexCategory.SECTORAL),
    IndexSpec("niftyauto", "NIFTY AUTO", IndexCategory.SECTORAL),
    IndexSpec("niftypharma", "NIFTY PHARMA", IndexCategory.SECTORAL),
    IndexSpec("niftyfmcg", "NIFTY FMCG", IndexCategory.SECTORAL),
    IndexSpec("niftymetal", "NIFTY METAL", IndexCategory.SECTORAL),
    IndexSpec("niftyrealty", "NIFTY REALTY", IndexCategory.SECTORAL),
    IndexSpec("niftymedia", "NIFTY MEDIA", IndexCategory.SECTORAL),
    IndexSpec("niftyenergy", "NIFTY ENERGY", IndexCategory.THEMATIC),
)
SPECS: Final[tuple[IndexSpec, ...]] = (BROAD, *SECTORALS)

#: The date the fixture files are named for on disk — unrelated to the `as_of` we stamp the L1
#: snapshot with (the source URL carries no date; the bytes are the same list whatever week it is).
FIXTURE_DATE: Final = date(2026, 9, 1)

SCRATCH_DB: Final = f"trading_m10_2_snapshot_{os.getpid()}"


# ── an alerter that records rather than logs, so alerts are asserted, not observed ─────────────


class RecordingAlerter:
    """An `Alerter` that keeps every send in memory. Dedup is not exercised here."""

    def __init__(self) -> None:
        self.sent: list[tuple[Severity, str, str]] = []

    def send(self, severity: Severity, title: str, body: str, dedup_key: str) -> AlertOutcome:
        self.sent.append((severity, title, dedup_key))
        return AlertOutcome.SENT


# ── database + lake fixtures (mirror the M1.10 EOD suite) ──────────────────────────────────────


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

    migrate(_settings_for(SCRATCH_DB, Path("data")), clock=FrozenClock(NOW))
    yield SCRATCH_DB

    conn = connect(admin, autocommit=True)
    try:
        conn.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)')
    finally:
        conn.close()


@pytest.fixture
def settings(scratch_db: str, tmp_path: Path) -> Settings:
    """Scratch DB plus a per-test lake root under tmp_path, keeping L0/L1 off the repo."""
    return _settings_for(scratch_db, tmp_path)


@pytest.fixture
def conn(settings: Settings) -> Iterator[Connection]:
    """A committed connection to the scratch DB, with `sync_state` cleared.

    The sweep commits after every slug, so tests cannot lean on rollback for isolation — the table
    is truncated up front and each run's commits stand.
    """
    with connection(settings) as live:
        live.execute("TRUNCATE sync_state")
        live.commit()
        yield live


@pytest.fixture
def register() -> SourceRegister:
    return load_register()


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW)


# ── helpers ───────────────────────────────────────────────────────────────────────────────────


def _fixture_bytes(slug: str) -> bytes:
    return (FIXTURES / f"ind_{slug}list_{FIXTURE_DATE:%Y%m%d}.csv").read_bytes()


def _ok(slug: str) -> RecordedResponse:
    return RecordedResponse(
        body=_fixture_bytes(slug), headers={"content-type": "application/octet-stream"}
    )


def _all_ok(register: SourceRegister) -> dict[str, ScriptedOutcome | list[ScriptedOutcome]]:
    return {constituents_url(spec.slug, register): _ok(spec.slug) for spec in SPECS}


def _fetcher(
    transport: RecordedTransport,
    *,
    settings: Settings,
    clock: FrozenClock,
    register: SourceRegister,
) -> Fetcher:
    return Fetcher(
        transport=transport,
        l0=L0Store(clock=clock, data_root=settings.data_root),
        alerter=RecordingAlerter(),
        clock=clock,
        register=register,
        settings=settings,
        sleep=lambda _seconds: None,
    )


def _store(conn: Connection, clock: FrozenClock, register: SourceRegister) -> SyncStateStore:
    from dataplatform.ingest.calendar import trading_calendar

    return SyncStateStore(conn, clock=clock, calendar=trading_calendar())


def _sync_state(conn: Connection, source: str, logical_date: date) -> str | None:
    row = conn.execute(
        "SELECT state FROM sync_state WHERE source = %s AND logical_date = %s",
        (source, logical_date),
    ).fetchone()
    return None if row is None else str(row[0])


# ── week_anchor: the mechanism that makes the job idempotent per week ───────────────────────────


def test_week_anchor_collapses_a_whole_iso_week_to_one_sunday_on_or_after_capture() -> None:
    """Every day of one ISO week resolves to the same anchor — that week's Sunday.

    The anchor is on-or-after the capture day, never before it, so a Saturday capture stamped this
    anchor can never be read back as in force *before* the Saturday it was taken (invariant #7).
    """
    week = [date(2026, 8, 31) + timedelta(days=n) for n in range(7)]  # Mon .. Sun
    anchors = {week_anchor(d) for d in week}
    assert anchors == {date(2026, 9, 6)}  # the ISO week's Sunday
    assert date(2026, 9, 6).isoweekday() == 7
    # The anchor is never earlier than the day it was computed from.
    for d in week:
        assert week_anchor(d) >= d
    # The next ISO week gets its own anchor — a distinct week is a distinct snapshot.
    assert week_anchor(date(2026, 9, 7)) == date(2026, 9, 13)


# ── acceptance 1: a weekly job appends a dated snapshot for every configured slug ────────────────


def test_the_weekly_job_appends_a_dated_snapshot_for_every_configured_slug(
    conn: Connection, settings: Settings, clock: FrozenClock, register: SourceRegister
) -> None:
    transport = RecordedTransport(_all_ok(register))
    sync = _store(conn, clock, register)
    as_of = week_anchor(clock.now().date())

    report = run_weekly_snapshot(
        fetcher=_fetcher(transport, settings=settings, clock=clock, register=register),
        l0=L0Store(clock=clock, data_root=settings.data_root),
        tracker=sync,
        alerter=RecordingAlerter(),
        as_of=as_of,
        commit=conn.commit,
        specs=SPECS,
        data_root=settings.data_root,
        register=register,
    )

    assert as_of == date(2026, 9, 6)
    assert len(report.published) == len(SPECS)
    assert not report.parked
    # Every configured slug has a dated snapshot at the week anchor, queryable back out of L1.
    for spec in SPECS:
        view = membership_asof(spec.slug, as_of, data_root=settings.data_root)
        assert view is not None and view.rows, f"{spec.slug} not queryable after the sweep"
        assert view.as_of == as_of


def test_the_job_is_registered_on_a_weekly_cron() -> None:
    """The scheduler carries the job, and its cron fires exactly once a week (M0.6 registry)."""
    registry = default_registry()
    assert "constituents_snapshot" in registry, "the weekly snapshot job must be registered"

    trigger = CONSTITUENTS_SNAPSHOT.trigger(timezone=IST)
    base = datetime(2026, 9, 1, 0, 0, tzinfo=IST)
    first = trigger.get_next_fire_time(None, base)
    assert first is not None
    second = trigger.get_next_fire_time(first, first + timedelta(seconds=1))
    assert second is not None
    assert second - first == timedelta(days=7)  # weekly, not daily
    assert first.isoweekday() == 6  # Saturday


# ── acceptance 2: in-force reads, and a re-run of the same week is a no-op ───────────────────────


def test_membership_asof_is_in_force_and_a_rerun_of_the_same_week_is_a_no_op(
    conn: Connection, settings: Settings, register: SourceRegister
) -> None:
    first_clock = FrozenClock(NOW)
    as_of = week_anchor(first_clock.now().date())

    first = run_weekly_snapshot(
        fetcher=_fetcher(
            RecordedTransport(_all_ok(register)),
            settings=settings,
            clock=first_clock,
            register=register,
        ),
        l0=L0Store(clock=first_clock, data_root=settings.data_root),
        tracker=_store(conn, first_clock, register),
        alerter=RecordingAlerter(),
        as_of=as_of,
        commit=conn.commit,
        specs=SPECS,
        data_root=settings.data_root,
        register=register,
    )
    assert len(first.published) == len(SPECS)

    # `membership_asof` returns the snapshot in force on a date: the anchor and any later date see
    # it; a date *before* the anchor sees nothing, never a future map (the survivorship guard).
    on_anchor = membership_asof(BROAD.slug, as_of, data_root=settings.data_root)
    assert on_anchor is not None and on_anchor.as_of == as_of
    later = membership_asof(BROAD.slug, date(2026, 9, 10), data_root=settings.data_root)
    assert later is not None and later.as_of == as_of
    assert (
        membership_asof(BROAD.slug, as_of - timedelta(days=1), data_root=settings.data_root) is None
    )

    # A second run later in the SAME ISO week, with an EMPTY transport script: any re-fetch would
    # raise UnrecordedRequestError. It must not — the week's anchor is already in L1, so every slug
    # is skipped and no request is made at all.
    later_clock = FrozenClock(SAME_WEEK_LATER)
    assert week_anchor(later_clock.now().date()) == as_of
    empty_transport = RecordedTransport({})
    second = run_weekly_snapshot(
        fetcher=_fetcher(empty_transport, settings=settings, clock=later_clock, register=register),
        l0=L0Store(clock=later_clock, data_root=settings.data_root),
        tracker=_store(conn, later_clock, register),
        alerter=RecordingAlerter(),
        as_of=week_anchor(later_clock.now().date()),
        commit=conn.commit,
        specs=SPECS,
        data_root=settings.data_root,
        register=register,
    )
    assert len(second.skipped) == len(SPECS)
    assert not second.published and not second.parked
    assert empty_transport.requests == []  # the per-week no-op opened no request at all
    # The read is unchanged by the no-op re-run.
    again = membership_asof(BROAD.slug, as_of, data_root=settings.data_root)
    assert again is not None and again.as_of == as_of


# ── acceptance 3: one slug's failure is journaled and alerted, and does not abort the others ─────


def test_a_slug_failure_is_journaled_and_alerted_and_does_not_abort_the_others(
    conn: Connection, settings: Settings, clock: FrozenClock, register: SourceRegister
) -> None:
    failed = SECTORALS[0]  # niftybank
    script = _all_ok(register)
    # One slug 404s — a fetch failure no retry would change.
    script[constituents_url(failed.slug, register)] = RecordedResponse(
        status_code=404, body=b"not found"
    )
    transport = RecordedTransport(script)
    alerter = RecordingAlerter()
    as_of = week_anchor(clock.now().date())

    report = run_weekly_snapshot(
        fetcher=_fetcher(transport, settings=settings, clock=clock, register=register),
        l0=L0Store(clock=clock, data_root=settings.data_root),
        tracker=_store(conn, clock, register),
        alerter=alerter,
        as_of=as_of,
        commit=conn.commit,
        specs=SPECS,
        data_root=settings.data_root,
        register=register,
    )

    # The failure did not abort the sweep: every other slug published and stays queryable.
    assert len(report.published) == len(SPECS) - 1
    assert len(report.parked) == 1
    parked = report.parked[0]
    assert parked.spec.slug == failed.slug
    assert parked.status is SlugStatus.PARKED
    assert parked.cause is ParkCause.FETCH_FAILED
    for spec in SPECS:
        if spec.slug == failed.slug:
            assert membership_asof(spec.slug, as_of, data_root=settings.data_root) is None
            continue
        view = membership_asof(spec.slug, as_of, data_root=settings.data_root)
        assert view is not None and view.rows, f"{spec.slug} lost to a sibling's failure"

    # Journaled: the failed slug's own sync_state row is FAILED (per-slug source id, M10.1).
    assert _sync_state(conn, constituents_state_source(failed.slug), as_of) == SyncState.FAILED
    # And a sibling that succeeded is PUBLISHED — the failure is that slug's row alone.
    assert _sync_state(conn, constituents_state_source(BROAD.slug), as_of) == SyncState.PUBLISHED

    # Alerted: exactly one alert, naming the failed slug and this week, and nothing for the others.
    assert len(alerter.sent) == 1
    severity, title, dedup_key = alerter.sent[0]
    assert severity is Severity.WARNING
    assert failed.slug in title and as_of.isoformat() in title
    assert dedup_key == f"constituents:{failed.slug}:{as_of.isoformat()}:{ParkCause.FETCH_FAILED}"
