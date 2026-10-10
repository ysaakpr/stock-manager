"""M17.10 acceptance: same-evening index levels and India VIX, offline.

Every payload is a frozen fixture — the close-all files under `tests/fixtures/nifty_index_close/`
(the April 2023 run is five consecutive real sessions around Good Friday) and the India VIX window
under `tests/fixtures/macro/india_vix/` — served by a `RecordedTransport`; the lake is a temp
directory and `sync_state` the backfill tests' in-memory stand-in. No socket opens.

  1. the job is registered with its covers, cron and lag budget, and never shares a host lease
     (`test_the_job_is_registered_*`, `test_every_fire_*`);
  2. a missed-sessions catch-up is one run: one ranged VIX POST, one request per missed close-all
     file and none for a session already landed (`test_*_catch_up_*`);
  3. a level for session D is released on D and is not knowable before it
     (`test_a_level_for_d_is_not_knowable_before_d`), and the M17 readiness probe is satisfied by
     exactly that release (`test_the_m17_readiness_probe_*`).
"""

from __future__ import annotations

import json
import socket
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Final

import pytest

from analyst.commons import LakeCommonsSource
from backtest.fm_world import READINESS_INDEX_SERIES, index_level_gaps
from dataplatform.alerts import build_alerter
from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.ingest.calendar import trading_calendar
from dataplatform.ingest.fetcher import Fetcher, RecordedResponse, RecordedTransport
from dataplatform.ingest.macro import backfill as mb
from dataplatform.ingest.macro import index_evening as ie
from dataplatform.ingest.macro.india_vix import INDIA_VIX_URL, parse_india_vix_history
from dataplatform.ingest.source_register import load as load_register
from dataplatform.scheduler.registry import (
    INDEX_CLOSE_EVENING,
    M17_FUND_MANAGERS,
    UNSCHEDULED,
    default_registry,
    lag_budgets,
)
from dataplatform.status.sync_state import SyncState
from dataplatform.store.l0 import L0Store
from dataplatform.store.macro_series import read_l1, read_pit, write_release
from tests.unit.test_macro_backfill import _FakeSync, _Row

FIXTURES: Final = Path(__file__).parents[1] / "fixtures"
CLOSE_ALL: Final = FIXTURES / "nifty_index_close" / "month_first_2023"
VIX_WINDOW: Final = (
    FIXTURES / "macro" / "india_vix" / "2026-10-06" / "india_vix_20260901_20260910.json"
)
#: The April 2023 fixture run: five real sessions, Good Friday (07) and the weekend between.
APRIL: Final = (
    date(2023, 4, 5),
    date(2023, 4, 6),
    date(2023, 4, 10),
    date(2023, 4, 11),
    date(2023, 4, 12),
)
TONIGHT: Final = date(2023, 4, 12)


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a test opened a socket; index-evening tests are offline")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def _clock(day: date, at: time = time(20, 35)) -> FrozenClock:
    return FrozenClock(datetime.combine(day, at, tzinfo=IST))


def _fetcher(transport: RecordedTransport, tmp_path: Path, clock: FrozenClock) -> Fetcher:
    settings = Settings(data_root=tmp_path)
    return Fetcher(
        transport=transport,
        l0=L0Store(clock=clock, data_root=tmp_path),
        alerter=build_alerter(settings, clock=clock),
        clock=clock,
        register=load_register(),
        settings=settings,
        sleep=lambda _seconds: None,
    )


def _close_all_url(session: date) -> str:
    return f"https://nsearchives.nseindia.com/content/indices/ind_close_all_{session:%d%m%Y}.csv"


def _close_all_script(*, absent: tuple[date, ...] = ()) -> dict[str, RecordedResponse]:
    script = {}
    for session in APRIL:
        if session in absent:
            script[_close_all_url(session)] = RecordedResponse(
                status_code=404, body=b"<html>not found</html>"
            )
        else:
            path = CLOSE_ALL / f"ind_close_all_{session:%d%m%Y}.csv"
            script[_close_all_url(session)] = RecordedResponse(body=path.read_bytes())
    return script


def _sync_landed_before(first_missed: date) -> _FakeSync:
    """`sync_state` with every expected data date in the catch-up window before `first_missed`
    already published — the store as M11.2 left it."""
    sync = _FakeSync()
    window_start = TONIGHT - timedelta(days=ie.CATCHUP_DAYS)
    for day in trading_calendar().expected_data_dates(window_start, first_missed - timedelta(1)):
        sync.rows[(mb.SOURCE_ID, day)] = _Row(SyncState.PUBLISHED)
    return sync


def _close_all(
    transport: RecordedTransport, tmp_path: Path, sync: _FakeSync, through: date = TONIGHT
) -> ie.StepOutcome:
    clock = _clock(through)
    return ie.close_all_evening(
        fetcher=_fetcher(transport, tmp_path, clock),
        l0=L0Store(clock=clock, data_root=tmp_path),
        sync=sync,
        commit=lambda: None,
        through=through,
        calendar=trading_calendar(),
        register=load_register(),
        data_root=tmp_path,
    )


# ── 1. registration ──────────────────────────────────────────────────────────────────────────


def test_the_job_is_registered_with_its_covers_cron_and_lag_budget() -> None:
    registry = default_registry()
    assert registry.get("index_close_evening") is INDEX_CLOSE_EVENING
    assert INDEX_CLOSE_EVENING.cron == "35 20 * * mon-fri; 10 21 * * mon-fri; 45 21 * * mon-fri"
    assert INDEX_CLOSE_EVENING.covers == ie.EVENING_SOURCES
    assert set(ie.EVENING_SOURCES) == {"nse_index_close_snapshot", "nifty_india_vix_history"}
    for source in ie.EVENING_SOURCES:
        assert source not in UNSCHEDULED, source
    # Owed every weekday: a session behind is already one evening late.
    assert lag_budgets(registry)["nse_index_close_snapshot"] == 1
    # The niftyindices.com copy of the same bytes stays explained, not silently dropped.
    assert "nifty_index_close_snapshot" in UNSCHEDULED
    # Each step leases the host its register row names, and nothing else.
    hosts = {row.id: row.host for row in load_register().sources}
    assert hosts[ie.CLOSE_ALL_SOURCE_ID] == ie.CLOSE_ALL_HOST
    assert hosts["nifty_india_vix_history"] == ie.INDIA_VIX_HOST


def _fires(job: Any, start: datetime, end: datetime) -> list[datetime]:
    trigger = job.trigger(IST)
    fires: list[datetime] = []
    previous: datetime | None = None
    cursor = start
    while (fire := trigger.get_next_fire_time(previous, cursor)) is not None and fire <= end:
        fires.append(fire)
        previous, cursor = fire, fire + timedelta(seconds=1)
    return fires


def test_every_fire_ends_before_the_m17_desk_and_clear_of_every_other_lease_on_its_hosts() -> None:
    """A lease is refused, not queued. Worst case = latest start (misfire grace) + whole budget."""
    monday = datetime(2026, 10, 5, 0, 0, tzinfo=IST)
    mine = _fires(INDEX_CLOSE_EVENING, monday, monday + timedelta(days=7))
    assert [f"{f:%a %H:%M}" for f in mine] == [
        f"{day} {at}"
        for day in ("Mon", "Tue", "Wed", "Thu", "Fri")
        for at in ("20:35", "21:10", "21:45")
    ]
    worst = INDEX_CLOSE_EVENING.latest_start + INDEX_CLOSE_EVENING.timeout
    desk = M17_FUND_MANAGERS.cron.split()
    for fire in mine:
        assert (fire + worst).time() <= time(int(desk[1]), int(desk[0])), f"{fire:%a %H:%M}"
    register = {row.id: row.host for row in load_register().sources}
    my_hosts = {ie.CLOSE_ALL_HOST, ie.INDIA_VIX_HOST}
    # `daily_snapshot` leases niftyindices.com without covering a row there; name it explicitly.
    by_body = {"daily_snapshot": {"niftyindices.com", "nsearchives.nseindia.com"}}
    for other in default_registry():
        if other.name == INDEX_CLOSE_EVENING.name:
            continue
        hosts = {register[s] for s in other.covers if s in register} | by_body.get(
            other.name, set()
        )
        if not hosts & my_hosts:
            continue
        for theirs in _fires(other, monday - timedelta(days=1), monday + timedelta(days=7)):
            theirs_end = theirs + other.timeout
            for fire in mine:
                assert fire + worst <= theirs or theirs_end <= fire, (
                    f"{fire:%a %H:%M} overlaps {other.name} {theirs:%a %H:%M}-{theirs_end:%H:%M}"
                )


# ── 2. catch-up ──────────────────────────────────────────────────────────────────────────────


def test_a_close_all_catch_up_requests_only_the_missed_sessions_in_one_run(tmp_path: Path) -> None:
    sync = _sync_landed_before(date(2023, 4, 6))
    transport = RecordedTransport(_close_all_script())
    outcome = _close_all(transport, tmp_path, sync)
    asked = [request.url for request in transport.requests]
    # The archive has no ranged form: one file per missed session, none for the landed one.
    assert asked == [_close_all_url(s) for s in APRIL[1:]]
    assert outcome.requests == 4 and outcome.latest == TONIGHT
    for session in APRIL[1:]:
        assert sync.get(mb.SOURCE_ID, session).state is SyncState.PUBLISHED  # type: ignore[union-attr]
    # A second fire the same evening makes no request at all.
    again = _close_all(transport, tmp_path, sync)
    assert again.requests == 0 and len(transport.requests) == 4


def test_tonights_404_is_not_yet_published_and_the_next_fire_lands_it(tmp_path: Path) -> None:
    sync = _sync_landed_before(date(2023, 4, 6))
    early = RecordedTransport(_close_all_script(absent=(TONIGHT,)))
    with pytest.raises(ie.IndexLevelsNotYetPublishedError, match="not published yet"):
        _close_all(early, tmp_path, sync)
    # The missed sessions still landed; tonight's is open to the next fire, not closed as a gap.
    for session in APRIL[1:-1]:
        assert sync.get(mb.SOURCE_ID, session).state is SyncState.PUBLISHED  # type: ignore[union-attr]
    row = sync.get(mb.SOURCE_ID, TONIGHT)
    assert row is not None and row.state is SyncState.FAILED and row.retryable is True
    later = RecordedTransport(_close_all_script())
    outcome = _close_all(later, tmp_path, sync)
    assert [r.url for r in later.requests] == [_close_all_url(TONIGHT)]
    assert outcome.latest == TONIGHT


def test_an_older_404_still_closes_its_session_as_the_backfill_does(tmp_path: Path) -> None:
    """The inversion guard: only the owed session's 404 is "not yet"; an older one is a gap."""
    sync = _sync_landed_before(date(2023, 4, 6))
    transport = RecordedTransport(_close_all_script(absent=(date(2023, 4, 10),)))
    _close_all(transport, tmp_path, sync)
    row = sync.get(mb.SOURCE_ID, date(2023, 4, 10))
    assert row is not None and row.state is SyncState.FAILED and row.retryable is False


def _seed_vix_through(last: date, tmp_path: Path) -> None:
    for release in parse_india_vix_history(VIX_WINDOW.read_bytes(), filename="seed.json"):
        if release.release_date <= last:
            write_release(release, data_root=tmp_path)


def _vix(transport: RecordedTransport, tmp_path: Path, through: date, at: time) -> ie.StepOutcome:
    clock = _clock(through, at)
    return ie.india_vix_evening(
        fetcher=_fetcher(transport, tmp_path, clock),
        l0=L0Store(clock=clock, data_root=tmp_path),
        through=through,
        attempt_at=clock.now(),
        data_root=tmp_path,
    )


def _vix_transport() -> RecordedTransport:
    return RecordedTransport(
        {
            INDIA_VIX_URL: RecordedResponse(
                body=VIX_WINDOW.read_bytes(), headers={"content-type": "text/html; charset=utf-8"}
            )
        }
    )


def test_a_vix_catch_up_over_missed_sessions_is_one_ranged_request(tmp_path: Path) -> None:
    _seed_vix_through(date(2026, 9, 3), tmp_path)
    transport = _vix_transport()
    outcome = _vix(transport, tmp_path, date(2026, 9, 10), time(20, 35))
    assert len(transport.requests) == 1
    body = transport.requests[0].payload or b""
    assert b"'startDate':'04-Sep-2026'" in body and b"'endDate':'10-Sep-2026'" in body
    assert outcome.sessions_landed == (
        date(2026, 9, 4),
        date(2026, 9, 7),
        date(2026, 9, 8),
        date(2026, 9, 9),
        date(2026, 9, 10),
    )
    # Landed: the next fire makes no request.
    again = _vix(transport, tmp_path, date(2026, 9, 10), time(21, 10))
    assert again.requests == 0 and len(transport.requests) == 1


def test_a_vix_window_short_of_tonight_raises_and_the_retry_does_not_collide(
    tmp_path: Path,
) -> None:
    _seed_vix_through(date(2026, 9, 8), tmp_path)
    transport = _vix_transport()  # carries through 09-10 only
    with pytest.raises(ie.IndexLevelsNotYetPublishedError, match="does not carry 2026-09-11"):
        _vix(transport, tmp_path, date(2026, 9, 11), time(20, 35))
    # What the endpoint did carry is written, inside the window only.
    assert read_l1(date(2026, 9, 10), data_root=tmp_path)
    with pytest.raises(ie.IndexLevelsNotYetPublishedError):
        _vix(transport, tmp_path, date(2026, 9, 11), time(21, 10))
    names = {
        ref.filename
        for ref in L0Store(clock=_clock(TONIGHT), data_root=tmp_path).iter_refs(
            "nifty_india_vix_history"
        )
    }
    assert len(names) == 2, names  # one payload per attempt, never a refused overwrite


# ── 3. point-in-time ─────────────────────────────────────────────────────────────────────────


def test_a_level_for_d_is_not_knowable_before_d(tmp_path: Path) -> None:
    sync = _sync_landed_before(date(2023, 4, 6))
    _close_all(RecordedTransport(_close_all_script()), tmp_path, sync)
    _seed_vix_through(date(2026, 9, 9), tmp_path)
    _vix(_vix_transport(), tmp_path, date(2026, 9, 10), time(20, 35))
    for session, series in (
        (TONIGHT, "IN.NSE.NIFTY_50.CLOSE"),
        (TONIGHT, "IN.NSE.NIFTY_500.PE"),
        (TONIGHT, "IN.NSE.INDIA_VIX.CLOSE"),
        (date(2026, 9, 10), "IN.NSE.INDIA_VIX.HIGH"),
    ):
        released = [f for f in read_l1(session, data_root=tmp_path) if f.series_id == series]
        assert released, (session, series)
        assert all(f.release_date == f.period_end == session for f in released)
        before = read_pit(session - timedelta(days=1), data_root=tmp_path)
        assert not [f for f in before if f.series_id == series and f.period_end == session]
        on = read_pit(session, data_root=tmp_path)
        assert [f for f in on if f.series_id == series and f.period_end == session]


def test_the_m17_readiness_probe_is_satisfied_by_the_evening_release_and_not_before(
    tmp_path: Path,
) -> None:
    sync = _sync_landed_before(date(2023, 4, 6))
    early = RecordedTransport(_close_all_script(absent=(TONIGHT,)))
    with pytest.raises(ie.IndexLevelsNotYetPublishedError):
        _close_all(early, tmp_path, sync)
    with LakeCommonsSource(clock=_clock(TONIGHT), data_root=tmp_path) as commons:
        waiting = index_level_gaps(commons, TONIGHT)
        assert len(waiting) == len(READINESS_INDEX_SERIES)
        assert all("latest 2023-04-11" in line for line in waiting), waiting
        _close_all(RecordedTransport(_close_all_script()), tmp_path, sync)
        assert index_level_gaps(commons, TONIGHT) == ()
        # The day before, the same lake is complete for that day too — and the level for D does
        # not stand in for D+1.
        assert index_level_gaps(commons, date(2023, 4, 11)) == ()
        assert len(index_level_gaps(commons, date(2023, 4, 13))) == len(READINESS_INDEX_SERIES)


def test_the_vix_fixture_is_the_shape_the_endpoint_answers() -> None:
    rows = json.loads(VIX_WINDOW.read_bytes())
    assert rows and {"HistoricalDate", "CLOSE", "INDEX_NAME"} <= set(rows[0])
