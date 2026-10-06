"""M13.7 — the same-evening TRI refresh, offline (B8).

The paper session decides session D on D's evening, and its regime filter reads the published
NIFTY 50 TRI for D. This proves the weekday job that lands it: D's level goes fetch -> L0 ->
parse -> L1 -> sync the evening it is disseminated; an answer fetched *before* dissemination parks
the row retryable and leaves L1 alone; a retry later that evening does not collide with the first
answer in L0; and a fire after the level landed makes no request.

Both payloads are real (`tests/fixtures/nifty_indices/tri/2026/PROVENANCE.md`): the 20:47 IST
answer of 2026-10-06 that carries D, and the 16:08 IST answer of 2026-10-05 that does not. Every
response is scripted through `RecordedTransport` and sockets are monkeypatched out.
"""

from __future__ import annotations

import json
import socket
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import pytest

from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.ingest.calendar import trading_calendar
from dataplatform.ingest.fetcher import RecordedResponse
from dataplatform.ingest.indices import (
    TRI_METHOD_PUBLISHED,
    TRI_SOURCE_ID,
    TriNotYetPublishedError,
    l0_tri_filename,
    parse_l0_tri_filename,
    read_tri_series,
    tri_state_source,
    tri_url,
)
from dataplatform.ingest.source_register import SourceRegister
from dataplatform.ingest.source_register import load as load_register
from dataplatform.ingest.tri_backfill import (
    EARLIEST_REQUESTED,
    EVENING_OVERLAP_DAYS,
    evening_window_start,
    latest_session_through,
    run_tri_backfill,
    run_tri_evening,
    stored_tri_payloads,
)
from dataplatform.status.sync_state import SyncState
from dataplatform.store.l0 import L0Store
from tests.conftest import SettingsLoader
from tests.unit.test_indices import RecordingTracker
from tests.unit.test_tri_backfill import NIFTY50, WINDOW_END, WINDOW_START, _ok, _wire

FIXTURES: Final = Path("tests/fixtures/nifty_indices/tri/2026")
#: Real answers either side of dissemination (PROVENANCE.md).
D_PRESENT: Final = FIXTURES / "tri_nifty50_20260925_20261006_at20261006T204737.json"
D_ABSENT: Final = FIXTURES / "tri_nifty50_20260921_20261005_at20261005T160846.json"

TUESDAY: Final = date(2026, 10, 6)
MONDAY: Final = date(2026, 10, 5)


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """A socket here is a bug: the live fetch is a driver run, never a test (B8)."""

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a test opened a socket; the TRI evening tests are offline")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.fixture
def register() -> SourceRegister:
    return load_register()


@pytest.fixture
def settings(load_settings: SettingsLoader) -> Settings:
    return load_settings(None)


def _answer(path: Path) -> RecordedResponse:
    return RecordedResponse(body=path.read_bytes(), headers={"content-type": "text/html"})


def _seed(settings: Settings, register: SourceRegister, data_root: Path) -> None:
    """L1 as the weekly refresh left it: the published series through 2026-03-30."""
    clock = FrozenClock(datetime(2026, 9, 8, 9, 15, tzinfo=IST))
    fetcher, l0, _ = _wire(
        {tri_url(register): _ok("nifty50")},
        clock=clock,
        settings=settings,
        register=register,
        data_root=data_root,
    )
    run_tri_backfill(
        fetcher=fetcher,
        l0=l0,
        tracker=RecordingTracker(clock),
        indices=(NIFTY50,),
        start=WINDOW_START,
        end=WINDOW_END,
        data_root=data_root,
    )


def _evening(
    answers: list[Path],
    *,
    at: datetime,
    settings: Settings,
    register: SourceRegister,
    data_root: Path,
) -> tuple[Any, ...]:
    clock = FrozenClock(at)
    fetcher, l0, transport = _wire(
        {tri_url(register): [_answer(path) for path in answers]},
        clock=clock,
        settings=settings,
        register=register,
        data_root=data_root,
    )
    return clock, fetcher, l0, transport


def _latest(data_root: Path) -> tuple[date, Decimal]:
    series = read_tri_series("nifty50", date.max, method=TRI_METHOD_PUBLISHED, data_root=data_root)
    assert series is not None
    return series.points[-1].as_of, series.points[-1].tri_value


# ── the evening of a session: D lands the same night ─────────────────────────────────────────


def test_session_d_lands_in_l0_l1_and_sync_the_same_evening(
    settings: Settings, register: SourceRegister, tmp_path: Path
) -> None:
    _seed(settings, register, tmp_path)
    at = datetime(2026, 10, 6, 19, 50, tzinfo=IST)
    clock, fetcher, l0, transport = _evening(
        [D_PRESENT], at=at, settings=settings, register=register, data_root=tmp_path
    )
    tracker = RecordingTracker(clock)

    (outcome,) = run_tri_evening(
        fetcher=fetcher,
        l0=l0,
        tracker=tracker,
        today=TUESDAY,
        attempt_at=at,
        indices=(NIFTY50,),
        data_root=tmp_path,
        calendar=trading_calendar(),
    )

    assert outcome.skipped is False
    assert outcome.latest == TUESDAY
    # The level the paper session's regime filter reads, knowable on D itself — not D+1.
    assert _latest(tmp_path) == (TUESDAY, Decimal("34608.14"))
    series = read_tri_series("nifty50", TUESDAY, method=TRI_METHOD_PUBLISHED, data_root=tmp_path)
    assert series is not None and series.points[-1].knowable_date == TUESDAY

    # The sync row is dated by the session, and walked the whole §4.4 path.
    row = tracker.rows[(tri_state_source("nifty50"), TUESDAY)]
    assert row.state is SyncState.PUBLISHED
    assert tracker.history[-1] is SyncState.PUBLISHED

    # One short POST: from the stored series' last level (it is older than the overlap floor)
    # through D, filed in L0 under its attempt instant.
    (request,) = transport.requests
    assert json.loads(request.payload or b"{}")["cinfo"] == (
        "{'name':'NIFTY 50','startDate':'30-Mar-2026',"
        "'endDate':'06-Oct-2026','indexName':'NIFTY 50'}"
    )
    assert row.l0_path is not None
    assert row.l0_path.endswith("tri_nifty50_20260330_20261006_at20261006T195000.json")


def test_a_fetch_before_dissemination_parks_retryable_and_leaves_l1_alone(
    settings: Settings, register: SourceRegister, tmp_path: Path
) -> None:
    """The real 16:08 IST answer of 2026-10-05 stops at 01-Oct: session D is not out yet.

    Inverted — the `require_through` check removed — the row would read PUBLISHED for a level the
    endpoint did not carry, and the paper session would decide D against D-1's benchmark.
    """
    _seed(settings, register, tmp_path)
    before = _latest(tmp_path)
    at = datetime(2026, 10, 5, 16, 8, 46, tzinfo=IST)
    clock, fetcher, l0, transport = _evening(
        [D_ABSENT], at=at, settings=settings, register=register, data_root=tmp_path
    )
    tracker = RecordingTracker(clock)
    commits: list[SyncState] = []

    with pytest.raises(TriNotYetPublishedError, match="2026-10-05 is not yet published"):
        run_tri_evening(
            fetcher=fetcher,
            l0=l0,
            tracker=tracker,
            today=MONDAY,
            attempt_at=at,
            indices=(NIFTY50,),
            data_root=tmp_path,
            commit=lambda: commits.append(
                tracker.rows[(tri_state_source("nifty50"), MONDAY)].state
            ),
            calendar=trading_calendar(),
        )

    # The FAILED row is committed before the error propagates, or it would roll back with the run.
    assert commits == [SyncState.FAILED]

    row = tracker.rows[(tri_state_source("nifty50"), MONDAY)]
    assert row.state is SyncState.FAILED
    assert row.retryable is True
    assert SyncState.VALIDATED not in tracker.history
    assert _latest(tmp_path) == before  # L1 untouched
    # …but the answer is kept: what the endpoint said at 16:08 is a true raw record.
    assert len(transport.requests) == 1
    assert [ref.filename for ref in l0.iter_refs(TRI_SOURCE_ID)][-1] == (
        "tri_nifty50_20260330_20261005_at20261005T160846.json"
    )


def test_a_retry_the_same_evening_lands_without_colliding_in_l0(
    settings: Settings, register: SourceRegister, tmp_path: Path
) -> None:
    """19:50 sees no D, 20:50 does: two answers for one window, both kept, one row.

    The first answer here is the pre-dissemination payload standing in for 19:50's; what matters is
    its shape (a valid series ending before D). Without the attempt suffix the second answer's
    filename would equal the first's and `L0Store.put` would refuse it as an overwrite, so the
    session could never land that night.
    """
    _seed(settings, register, tmp_path)
    first = datetime(2026, 10, 6, 19, 50, tzinfo=IST)
    clock, fetcher, l0, transport = _evening(
        [D_ABSENT, D_PRESENT], at=first, settings=settings, register=register, data_root=tmp_path
    )
    tracker = RecordingTracker(clock)
    kwargs: dict[str, Any] = {
        "fetcher": fetcher,
        "l0": l0,
        "tracker": tracker,
        "today": TUESDAY,
        "indices": (NIFTY50,),
        "data_root": tmp_path,
        "calendar": trading_calendar(),
    }

    with pytest.raises(TriNotYetPublishedError):
        run_tri_evening(**kwargs, attempt_at=first)

    clock.advance(timedelta(hours=1))
    (outcome,) = run_tri_evening(**kwargs, attempt_at=clock.now())

    assert outcome.latest == TUESDAY
    assert _latest(tmp_path) == (TUESDAY, Decimal("34608.14"))
    row = tracker.rows[(tri_state_source("nifty50"), TUESDAY)]
    assert row.state is SyncState.PUBLISHED
    assert row.attempts == 2
    assert len(transport.requests) == 2
    evening = [ref.filename for ref in l0.iter_refs(TRI_SOURCE_ID) if ref.logical_date == TUESDAY]
    assert evening == [
        "tri_nifty50_20260330_20261006_at20261006T195000.json",
        "tri_nifty50_20260330_20261006_at20261006T205000.json",
    ]


def test_a_fire_after_the_session_landed_makes_no_request(
    settings: Settings, register: SourceRegister, tmp_path: Path
) -> None:
    _seed(settings, register, tmp_path)
    at = datetime(2026, 10, 6, 19, 50, tzinfo=IST)
    clock, fetcher, l0, transport = _evening(
        [D_PRESENT], at=at, settings=settings, register=register, data_root=tmp_path
    )
    kwargs: dict[str, Any] = {
        "fetcher": fetcher,
        "l0": l0,
        "tracker": RecordingTracker(clock),
        "today": TUESDAY,
        "indices": (NIFTY50,),
        "data_root": tmp_path,
        "calendar": trading_calendar(),
    }
    run_tri_evening(**kwargs, attempt_at=at)
    (again,) = run_tri_evening(**kwargs, attempt_at=at + timedelta(hours=1))

    assert again.skipped is True
    assert len(transport.requests) == 1


# ── which session, and which window ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("today", "owed"),
    [
        (TUESDAY, TUESDAY),  # a session: tonight's own level
        (date(2026, 10, 2), date(2026, 10, 1)),  # Gandhi Jayanti: the previous session's
        (date(2026, 10, 10), date(2026, 10, 9)),  # a Saturday (manual run): Friday's
    ],
)
def test_the_owed_session_is_the_latest_on_or_before_today(today: date, owed: date) -> None:
    assert latest_session_through(today, trading_calendar()) == owed


def test_the_window_overlaps_l1_and_never_leaves_a_hole(
    settings: Settings, register: SourceRegister, tmp_path: Path
) -> None:
    """No series: whole history. A stale one: from its last level. A current one: the floor."""
    assert evening_window_start(NIFTY50, TUESDAY, tmp_path) == EARLIEST_REQUESTED

    _seed(settings, register, tmp_path)
    assert evening_window_start(NIFTY50, TUESDAY, tmp_path) == date(2026, 3, 30)
    assert evening_window_start(NIFTY50, date(2026, 4, 7), tmp_path) == date(2026, 3, 24)
    assert date(2026, 4, 7) - timedelta(days=EVENING_OVERLAP_DAYS) == date(2026, 3, 24)


# ── L0 names and the rebuild that reads them ─────────────────────────────────────────────────


def test_an_attempt_stamped_filename_round_trips_and_old_names_still_parse() -> None:
    at = datetime(2026, 10, 6, 20, 47, 37, tzinfo=IST)
    name = l0_tri_filename("nifty_next_50", date(2026, 9, 25), TUESDAY, attempt=at)
    assert name == "tri_nifty_next_50_20260925_20261006_at20261006T204737.json"
    assert parse_l0_tri_filename(name) == ("nifty_next_50", date(2026, 9, 25), TUESDAY)
    assert parse_l0_tri_filename("tri_nifty50_19900401_20261005.json") == (
        "nifty50",
        EARLIEST_REQUESTED,
        MONDAY,
    )


def test_a_rebuild_replays_payloads_in_fetch_order_not_filename_order(tmp_path: Path) -> None:
    """A weekly whole-history payload fetched *after* an evening one must be written after it.

    By filename the whole-history window (`…_19900401_…`) sorts first, so a rebuild in filename
    order would let the older evening answer overwrite the newer weekly one on every date they
    share — a restated level would silently revert.
    """
    clock = FrozenClock(datetime(2026, 10, 6, 19, 50, tzinfo=IST))
    l0 = L0Store(clock=clock, data_root=tmp_path)
    evening = l0.put(
        TRI_SOURCE_ID,
        TUESDAY,
        "tri_nifty50_20260925_20261006_at20261006T195000.json",
        D_PRESENT.read_bytes(),
    )
    clock.advance(timedelta(days=4))
    weekly = l0.put(
        TRI_SOURCE_ID,
        date(2026, 10, 10),
        "tri_nifty50_19900401_20261010.json",
        (FIXTURES / "tri_nifty50_20210401_20260331.json").read_bytes(),
    )

    assert [ref.filename for ref in l0.iter_refs(TRI_SOURCE_ID)] == [
        weekly.filename,
        evening.filename,
    ]
    assert [ref.filename for ref in stored_tri_payloads(l0, (NIFTY50,))["nifty50"]] == [
        evening.filename,
        weekly.filename,
    ]
