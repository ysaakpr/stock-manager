"""The daily capture jobs (ops-daily-capture) — the sources whose failure mode was never running.

Laid out as the ways a daily capture of a latest-only endpoint quietly stops being worth anything:

1. **It files one session's payload as another's.** A late endpoint serves yesterday; a morning
   run sees today's intraday copy. The capture publishes the session the payload *states*, never
   publishes an intraday copy, and marks the owed session FAILED when it did not land.
2. **It runs twice and pays twice, or collides with itself in L0.** A second run of a published
   date makes no request; undated payloads are named for their capture instant.
3. **One source erases another.** NSE and BSE announcements, bulk and block deals, RSS and GDELT
   each share one L1 partition per date; landing the second never drops the first. A later
   shareholding poll never drops a company an earlier one landed.
4. **One busy host takes the job down.** A lease another driver holds fails that host's sources
   and leaves the rest to land.

Offline (B8): every response is a checked-in fixture or a scripted status; a socket is a test bug.
"""

from __future__ import annotations

import json
import socket
from collections.abc import Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pyarrow.parquet as pq
import pytest

from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.identity.master import Exchange, IdentityMaster, SymbolWindow
from dataplatform.ingest import announcements as ann
from dataplatform.ingest import daily_capture as dc
from dataplatform.ingest import shareholding
from dataplatform.ingest.calendar import trading_calendar
from dataplatform.ingest.daily_capture import CaptureContext, CaptureStatus
from dataplatform.ingest.fetcher import (
    Fetcher,
    RecordedResponse,
    RecordedTransport,
    ScriptedOutcome,
)
from dataplatform.ingest.lease import HostBusyError, LeaseHolder
from dataplatform.ingest.news import NEWS_DATASET
from dataplatform.ingest.nse import board_meetings, deals, fii_dii
from dataplatform.ingest.source_register import load as load_register
from dataplatform.quality.sentinel import QualityFinding
from dataplatform.status.sync_state import SyncState
from dataplatform.store import fo_aggregates
from dataplatform.store.l0 import L0Store
from dataplatform.store.paths import l1_partition_path, l2_partition_path
from tests.conftest import SettingsLoader
from tests.unit.test_daily_snapshot import RecordingTracker, SpyAlerter

FIXTURES: Final = Path("tests/fixtures")
WARM: Final = "https://www.nseindia.com/"
REGISTER: Final = load_register()


def _url(source_id: str) -> str:
    return next(row for row in REGISTER.sources if row.id == source_id).url_template


def _fixture(path: str) -> bytes:
    return (FIXTURES / path).read_bytes()


Script = Mapping[str, ScriptedOutcome | Sequence[ScriptedOutcome]]


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a test opened a socket; the capture tests are offline")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.fixture
def settings(load_settings: SettingsLoader) -> Settings:
    return load_settings(None)


class Harness:
    """A `CaptureContext` over a recorded transport, an in-memory tracker and a `tmp_path` lake."""

    def __init__(self, settings: Settings, root: Path, now: datetime) -> None:
        self.settings = settings
        self.root = root
        self.clock = FrozenClock(now)
        self.tracker = RecordingTracker(self.clock)
        self.alerter = SpyAlerter()
        self.l0 = L0Store(clock=self.clock, data_root=root)
        self.transport = RecordedTransport({})
        self.busy: set[str] = set()
        self.master = IdentityMaster(
            (
                SymbolWindow(
                    exchange=Exchange.NSE,
                    symbol="HATSUN",
                    valid_from=date(2000, 1, 1),
                    valid_to=None,
                    isin="INE473B01035",
                ),
            )
        )
        self.scrip_index = {"543210": "INE0ROBO1018", "500325": "INE002A01018"}

    def at(self, now: datetime, script: Script) -> CaptureContext:
        """The context for one run at `now`, answering from `script`."""
        self.clock = FrozenClock(now)
        self.tracker._clock = self.clock
        self.l0 = L0Store(clock=self.clock, data_root=self.root)
        self.transport = RecordedTransport(script)

        @contextmanager
        def fetchers(hosts: Sequence[str], command: str) -> Iterator[Fetcher]:
            for host in hosts:
                if host in self.busy:
                    raise HostBusyError(
                        LeaseHolder(
                            host=host,
                            pid=4242,
                            machine="test",
                            command="a campaign",
                            started_at=now,
                        )
                    )
            yield Fetcher(
                transport=self.transport,
                l0=self.l0,
                alerter=SpyAlerter(),
                clock=self.clock,
                register=REGISTER,
                settings=self.settings,
                sleep=lambda _seconds: None,
            )

        return CaptureContext(
            l0=self.l0,
            tracker=self.tracker,
            calendar=trading_calendar(),
            clock=self.clock,
            register=REGISTER,
            fetchers=fetchers,
            alerter=self.alerter,
            master=lambda: self.master,
            data_root=self.root,
            scrip_index=lambda: self.scrip_index,
        )

    def state(self, source: str, day: date) -> SyncState | None:
        record = self.tracker.get(source, day)
        return None if record is None else record.state

    def urls(self) -> list[str]:
        return [request.url for request in self.transport.requests if request.url != WARM]


def _harness(settings: Settings, tmp_path: Path, now: datetime) -> Harness:
    return Harness(settings, tmp_path, now)


def _ist(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=IST)


# ── the owed session ──────────────────────────────────────────────────────────────────────────


def test_the_evening_run_owes_tonight_and_a_daytime_run_owes_the_last_session() -> None:
    calendar = trading_calendar()
    assert dc.owed_session(calendar, _ist(2026, 8, 7, 20, 0)) == date(2026, 8, 7)
    assert dc.owed_session(calendar, _ist(2026, 8, 7, 13, 0)) == date(2026, 8, 6)
    # Monday morning owes Friday; a Saturday owes Friday too.
    assert dc.owed_session(calendar, _ist(2026, 8, 10, 9, 0)) == date(2026, 8, 7)
    assert dc.owed_session(calendar, _ist(2026, 8, 8, 21, 0)) == date(2026, 8, 7)


# ── 1. FII/DII: the payload's own session, never another ──────────────────────────────────────

FLOWS: Final = "nse_flows/json_v1/fiidiiTradeReact_20260807.json"


def _flows_script(body: bytes | None = None, *, owed: date = date(2026, 8, 7)) -> Script:
    """The www.nseindia.com leg of `nse_daily_capture`: the flows and the board meetings."""
    return {
        WARM: RecordedResponse(status_code=403),
        _url(fii_dii.SOURCE_ID): RecordedResponse(body=body or _fixture(FLOWS)),
        _bm_url(owed): RecordedResponse(body=BOARD_MEETINGS),
    }


BOARD_MEETINGS: Final = _fixture(
    "nse_board_meetings/json_v1/corporate_board_meetings_20261020_20261120.json"
)


def _bm_url(owed: date) -> str:
    return board_meetings.board_meetings_url(
        owed - timedelta(days=board_meetings.CAPTURE_DAYS_BEFORE),
        owed + timedelta(days=board_meetings.CAPTURE_DAYS_AFTER),
        REGISTER,
    )


def test_flows_land_under_the_session_they_state_and_a_rerun_makes_no_request(
    settings: Settings, tmp_path: Path
) -> None:
    friday_evening = _ist(2026, 8, 7, 20, 0)
    h = _harness(settings, tmp_path, friday_evening)
    outcome = dc.capture_fii_dii(
        h.at(friday_evening, _flows_script()), _fetcher(h), owed=date(2026, 8, 7)
    )
    assert outcome.status is CaptureStatus.CAPTURED
    assert h.state(fii_dii.SOURCE_ID, date(2026, 8, 7)) is SyncState.PUBLISHED
    assert fii_dii.read_l1(date(2026, 8, 7), data_root=tmp_path).trade_date == date(2026, 8, 7)
    # The L0 name is the capture instant — a second capture the same month cannot collide.
    record = h.tracker.get(fii_dii.SOURCE_ID, date(2026, 8, 7))
    assert record is not None and record.l0_path is not None
    assert "captured_20260807T200000" in record.l0_path

    report = dc.run_nse_daily_capture(h.at(_ist(2026, 8, 7, 23, 0), _flows_script()))
    flows = [o for o in report.outcomes if o.source == fii_dii.SOURCE_ID]
    assert flows[0].status is CaptureStatus.ALREADY_PUBLISHED
    assert _url(fii_dii.SOURCE_ID) not in h.urls()


def _fetcher(h: Harness) -> Fetcher:
    return Fetcher(
        transport=h.transport,
        l0=h.l0,
        alerter=SpyAlerter(),
        clock=h.clock,
        register=REGISTER,
        settings=h.settings,
        sleep=lambda _seconds: None,
    )


def test_a_late_endpoint_lands_the_old_session_and_fails_the_owed_one(
    settings: Settings, tmp_path: Path
) -> None:
    """Monday 20:00 and the endpoint still serves Friday: keep Friday, say Monday is owed."""
    monday = _ist(2026, 8, 10, 20, 0)
    h = _harness(settings, tmp_path, monday)
    ctx = h.at(monday, _flows_script())
    outcome = dc.capture_fii_dii(ctx, _fetcher(h), owed=date(2026, 8, 10))
    assert outcome.status is CaptureStatus.FAILED
    assert h.state(fii_dii.SOURCE_ID, date(2026, 8, 7)) is SyncState.PUBLISHED
    owed = h.tracker.get(fii_dii.SOURCE_ID, date(2026, 8, 10))
    assert owed is not None and owed.state is SyncState.FAILED and owed.retryable
    assert "still serves 2026-08-07" in (owed.last_error or "")


def test_an_intraday_copy_is_kept_but_never_published(settings: Settings, tmp_path: Path) -> None:
    """13:00 on the 7th, the endpoint already shows the 7th: provisional, and the 6th is lost."""
    noon = _ist(2026, 8, 7, 13, 0)
    h = _harness(settings, tmp_path, noon)
    outcome = dc.capture_fii_dii(h.at(noon, _flows_script()), _fetcher(h), owed=date(2026, 8, 6))
    assert h.state(fii_dii.SOURCE_ID, date(2026, 8, 7)) is None
    assert outcome.status is CaptureStatus.FAILED
    lost = h.tracker.get(fii_dii.SOURCE_ID, date(2026, 8, 6))
    assert lost is not None and lost.retryable is False
    assert len(list(h.l0.iter_refs(fii_dii.SOURCE_ID))) == 1  # the bytes are kept regardless


# ── deals: two files, one partition ───────────────────────────────────────────────────────────

BULK: Final = _fixture("nse_deals/bulk_01092026.csv")
BLOCK: Final = _fixture("nse_deals/block_01092026.csv")
BLOCK_EMPTY: Final = BLOCK.splitlines(keepends=True)[0]
BULK_EMPTY: Final = BULK.splitlines(keepends=True)[0]


def _deals_script(bulk: ScriptedOutcome, block: ScriptedOutcome) -> Script:
    return {_url(deals.BULK_SOURCE_ID): bulk, _url(deals.BLOCK_SOURCE_ID): block}


def test_bulk_and_block_land_in_one_partition_resolved_to_isin(
    settings: Settings, tmp_path: Path
) -> None:
    evening = _ist(2026, 9, 1, 20, 0)
    h = _harness(settings, tmp_path, evening)
    ctx = h.at(evening, _deals_script(RecordedResponse(body=BULK), RecordedResponse(body=BLOCK)))
    outcomes = dc.capture_deals(ctx, _fetcher(h), owed=date(2026, 9, 1))
    assert {o.source: o.status for o in outcomes} == {
        deals.BULK_SOURCE_ID: CaptureStatus.CAPTURED,
        deals.BLOCK_SOURCE_ID: CaptureStatus.CAPTURED,
    }
    day = deals.read_l1(date(2026, 9, 1), data_root=tmp_path)
    assert {row.isin for row in day.rows} == {"INE473B01035"}  # only HATSUN is in the master
    assert {row.deal_type for row in day.rows} == {deals.DealType.BLOCK}
    assert all(row.l0_key and "captured_" in row.l0_key for row in day.rows)


def test_the_second_half_of_a_day_never_erases_the_first(
    settings: Settings, tmp_path: Path
) -> None:
    """Block fails at 20:00 and lands at 23:00; the partition then holds both files' rows."""
    h = _harness(settings, tmp_path, _ist(2026, 9, 1, 20, 0))
    h.master = IdentityMaster(
        (
            *(
                SymbolWindow(
                    exchange=Exchange.NSE,
                    symbol=symbol,
                    valid_from=date(2000, 1, 1),
                    valid_to=None,
                    isin=isin,
                )
                for symbol, isin in (("HATSUN", "INE473B01035"), ("AARADHYA", "INE0AARA1012"))
            ),
        )
    )
    first = h.at(
        _ist(2026, 9, 1, 20, 0),
        _deals_script(RecordedResponse(body=BULK), RecordedResponse(status_code=404)),
    )
    dc.capture_deals(first, _fetcher(h), owed=date(2026, 9, 1))
    assert h.state(deals.BULK_SOURCE_ID, date(2026, 9, 1)) is SyncState.PUBLISHED
    assert h.state(deals.BLOCK_SOURCE_ID, date(2026, 9, 1)) is SyncState.FAILED
    assert {r.deal_type for r in deals.read_l1(date(2026, 9, 1), data_root=tmp_path).rows} == {
        deals.DealType.BULK
    }

    second = h.at(
        _ist(2026, 9, 1, 23, 0),
        _deals_script(RecordedResponse(body=BULK), RecordedResponse(body=BLOCK)),
    )
    dc.capture_deals(second, _fetcher(h), owed=date(2026, 9, 1))
    assert _url(deals.BULK_SOURCE_ID) not in h.urls()  # already published: not re-fetched
    assert h.state(deals.BLOCK_SOURCE_ID, date(2026, 9, 1)) is SyncState.PUBLISHED
    types = {r.deal_type for r in deals.read_l1(date(2026, 9, 1), data_root=tmp_path).rows}
    assert types == {deals.DealType.BULK, deals.DealType.BLOCK}


def test_an_empty_file_is_a_quiet_day_only_on_its_own_evening(
    settings: Settings, tmp_path: Path
) -> None:
    evening = _ist(2026, 9, 1, 20, 0)
    h = _harness(settings, tmp_path, evening)
    script = _deals_script(RecordedResponse(body=BULK), RecordedResponse(body=BLOCK_EMPTY))
    dc.capture_deals(h.at(evening, script), _fetcher(h), owed=date(2026, 9, 1))
    assert h.state(deals.BLOCK_SOURCE_ID, date(2026, 9, 1)) is SyncState.PUBLISHED

    morning = _ist(2026, 9, 3, 10, 0)  # owes the 2nd; an undated empty file proves nothing
    h2 = _harness(settings, tmp_path / "other", morning)
    outcomes = dc.capture_deals(
        h2.at(
            morning,
            _deals_script(RecordedResponse(body=BULK_EMPTY), RecordedResponse(body=BLOCK_EMPTY)),
        ),
        _fetcher(h2),
        owed=date(2026, 9, 2),
    )
    assert CaptureStatus.SKIPPED in {o.status for o in outcomes}
    assert h2.state(deals.BLOCK_SOURCE_ID, date(2026, 9, 2)) is SyncState.FAILED


# ── F&O: the current session, reused from L0, never walked back ───────────────────────────────

FO: Final = _fixture("nse_fo/udiff/BhavCopy_NSE_FO_0_0_0_20260807_F_0000.csv.zip")
FO_URL: Final = _url("nse_fo_bhavcopy").replace("{YYYYMMDD}", "20260807")


def test_the_current_fo_session_lands_in_l1_once(settings: Settings, tmp_path: Path) -> None:
    evening = _ist(2026, 8, 7, 20, 0)
    h = _harness(settings, tmp_path, evening)
    outcome = dc.capture_fo_bhavcopy(
        h.at(evening, {FO_URL: RecordedResponse(body=FO)}), _fetcher(h), owed=date(2026, 8, 7)
    )
    assert outcome.status is CaptureStatus.CAPTURED and outcome.rows > 0
    assert fo_aggregates.read_l1(date(2026, 8, 7), data_root=tmp_path)
    again = dc.capture_fo_bhavcopy(
        h.at(_ist(2026, 8, 7, 23, 0), {FO_URL: RecordedResponse(body=FO)}),
        _fetcher(h),
        owed=date(2026, 8, 7),
    )
    assert again.status is CaptureStatus.ALREADY_PUBLISHED and h.urls() == []


def _fo_l2(root: Path, day: date) -> Path:
    return l2_partition_path(fo_aggregates.FO_AGGREGATES_DATASET, day, data_root=root)


def test_the_fo_capture_derives_l2_beside_l1(settings: Settings, tmp_path: Path) -> None:
    """M17.15: the nightly path wrote L1 only and nothing rebuilt L2 (stale from 2026-10-05).

    Fails if the capture stops deriving L2: the dossier's F&O block reads L2 alone.
    """
    evening = _ist(2026, 8, 7, 20, 0)
    h = _harness(settings, tmp_path, evening)
    day = date(2026, 8, 7)
    outcome = dc.capture_fo_bhavcopy(
        h.at(evening, {FO_URL: RecordedResponse(body=FO)}), _fetcher(h), owed=day
    )
    assert outcome.status is CaptureStatus.CAPTURED
    aggregates = fo_aggregates.read_l2(day, data_root=tmp_path)
    assert aggregates and all(a.trade_date == day for a in aggregates)
    # The same bytes the L1-only rebuild derives, so the two paths never disagree.
    first = _fo_l2(tmp_path, day).read_bytes()
    fo_aggregates.rebuild_l2_from_l1(day, master=h.master, data_root=tmp_path)
    assert _fo_l2(tmp_path, day).read_bytes() == first


def test_the_nightly_run_heals_an_l2_gap_from_l1_with_no_request(
    settings: Settings, tmp_path: Path
) -> None:
    evening = _ist(2026, 8, 7, 20, 0)
    h = _harness(settings, tmp_path, evening)
    day = date(2026, 8, 7)
    dc.capture_fo_bhavcopy(
        h.at(evening, {FO_URL: RecordedResponse(body=FO)}), _fetcher(h), owed=day
    )
    _fo_l2(tmp_path, day).unlink()  # the 2026-10-06..15 state: L1 landed, L2 never derived
    later = _ist(2026, 8, 10, 20, 0)
    ctx = h.at(later, {})
    healed = dc.heal_fo_aggregates(ctx, date(2026, 8, 10))
    assert [(o.source, o.logical_date, o.status) for o in healed] == [
        (fo_aggregates.FO_AGGREGATES_DATASET, day, CaptureStatus.CAPTURED)
    ]
    assert fo_aggregates.read_l2(day, data_root=tmp_path) and h.urls() == []
    assert dc.heal_fo_aggregates(ctx, date(2026, 8, 10)) == []  # nothing left to heal


def test_nse_daily_capture_runs_the_heal_even_when_every_host_is_busy(
    settings: Settings, tmp_path: Path
) -> None:
    """The scheduled job is what closes the gap; fails if the heal is dropped from it."""
    evening = _ist(2026, 8, 7, 20, 0)
    h = _harness(settings, tmp_path, evening)
    day = date(2026, 8, 7)
    dc.capture_fo_bhavcopy(
        h.at(evening, {FO_URL: RecordedResponse(body=FO)}), _fetcher(h), owed=day
    )
    _fo_l2(tmp_path, day).unlink()
    h.busy = {"nsearchives.nseindia.com", "www.nseindia.com"}
    report = dc.run_nse_daily_capture(h.at(_ist(2026, 8, 10, 20, 0), {}))
    derived = [o for o in report.outcomes if o.source == fo_aggregates.FO_AGGREGATES_DATASET]
    assert [(o.logical_date, o.status) for o in derived] == [(day, CaptureStatus.CAPTURED)]
    assert _fo_l2(tmp_path, day).exists()


def test_a_ranged_rederive_rebuilds_every_session_with_l1(
    settings: Settings, tmp_path: Path
) -> None:
    evening = _ist(2026, 8, 7, 20, 0)
    h = _harness(settings, tmp_path, evening)
    day = date(2026, 8, 7)
    ctx = h.at(evening, {FO_URL: RecordedResponse(body=FO)})
    dc.capture_fo_bhavcopy(ctx, _fetcher(h), owed=day)
    sessions = ctx.calendar.expected_sessions(date(2026, 8, 3), date(2026, 8, 11))
    outcomes = dc.rederive_fo_aggregates(ctx, sessions, only_missing=False)
    # Only the session with an L1 partition is touched; the others have nothing to derive from.
    assert [(o.logical_date, o.status) for o in outcomes] == [(day, CaptureStatus.CAPTURED)]


def test_an_unpublished_fo_file_is_a_retryable_failure(settings: Settings, tmp_path: Path) -> None:
    evening = _ist(2026, 8, 7, 20, 0)
    h = _harness(settings, tmp_path, evening)
    outcome = dc.capture_fo_bhavcopy(
        h.at(evening, {FO_URL: RecordedResponse(status_code=404)}),
        _fetcher(h),
        owed=date(2026, 8, 7),
    )
    record = h.tracker.get("nse_fo_bhavcopy", date(2026, 8, 7))
    assert outcome.failed and record is not None and record.retryable


# ── 4. a busy host fails only its own sources ─────────────────────────────────────────────────


def test_a_campaign_on_the_archive_host_costs_deals_and_fo_but_never_the_flows(
    settings: Settings, tmp_path: Path
) -> None:
    evening = _ist(2026, 8, 7, 20, 0)
    h = _harness(settings, tmp_path, evening)
    h.busy = {"nsearchives.nseindia.com"}
    report = dc.run_nse_daily_capture(h.at(evening, _flows_script()))
    by_source = {o.source: o for o in report.outcomes}
    assert by_source[fii_dii.SOURCE_ID].status is CaptureStatus.CAPTURED
    for source in (deals.BULK_SOURCE_ID, deals.BLOCK_SOURCE_ID, "nse_fo_bhavcopy"):
        assert by_source[source].failed
        record = h.tracker.get(source, date(2026, 8, 7))
        assert record is not None and "HostBusyError" in (record.last_error or "")
    assert len(report.failed) == 3


# ── board meetings (M17.16): landed on the flows' host, merged by knowable date ────────────────


def test_board_meetings_land_on_the_evening_run_and_a_rerun_makes_no_request(
    settings: Settings, tmp_path: Path
) -> None:
    evening = _ist(2026, 10, 19, 20, 0)
    owed = date(2026, 10, 19)
    h = _harness(settings, tmp_path, evening)
    report = dc.run_nse_daily_capture(h.at(evening, _flows_script(owed=owed)))
    by_source = {o.source: o for o in report.outcomes}
    assert by_source[board_meetings.SOURCE_ID].status is CaptureStatus.CAPTURED
    assert by_source[board_meetings.SOURCE_ID].rows == 17
    assert h.state(board_meetings.SOURCE_ID, owed) is SyncState.PUBLISHED
    # The window asked is the owed session minus a week through 90 days ahead.
    assert _bm_url(owed) in h.urls()
    assert "from_date=12-10-2026&to_date=17-01-2027" in _bm_url(owed)
    # Every row lands under its knowable date: MPHASIS' intimations of 21-Sep (20:25 and 20:34,
    # after the 20:00 capture) are knowable on 22-Sep.
    held = board_meetings.read_pit(owed, data_root=tmp_path)
    assert len(held) == 17
    mphasis = [r for r in held if r.symbol == "MPHASIS"]
    assert {r.knowable_date for r in mphasis} == {date(2026, 9, 22)}

    rerun = dc.run_nse_daily_capture(h.at(_ist(2026, 10, 19, 23, 0), _flows_script(owed=owed)))
    again = [o for o in rerun.outcomes if o.source == board_meetings.SOURCE_ID]
    assert again[0].status is CaptureStatus.ALREADY_PUBLISHED
    assert h.urls().count(_bm_url(owed)) == 0  # the 23:00 transport saw no board-meetings request


def test_a_later_capture_never_drops_an_intimation_an_earlier_one_landed(
    settings: Settings, tmp_path: Path
) -> None:
    """The feed is a current-state list: a row it no longer serves stays in L1."""
    first = _ist(2026, 10, 19, 20, 0)
    h = _harness(settings, tmp_path, first)
    dc.capture_board_meetings(
        h.at(first, _flows_script(owed=date(2026, 10, 19))), _fetcher(h), owed=date(2026, 10, 19)
    )
    payload = json.loads(BOARD_MEETINGS)
    shrunk = json.dumps([r for r in payload if r["bm_symbol"] != "INFY"]).encode()
    later = _ist(2026, 10, 21, 20, 0)
    script = {
        WARM: RecordedResponse(status_code=403),
        _bm_url(date(2026, 10, 21)): RecordedResponse(body=shrunk),
    }
    outcome = dc.capture_board_meetings(h.at(later, script), _fetcher(h), owed=date(2026, 10, 21))
    assert outcome.status is CaptureStatus.CAPTURED
    held = board_meetings.read_pit(date(2026, 10, 21), data_root=tmp_path)
    assert len(held) == 17 and any(r.symbol == "INFY" for r in held)


def test_a_bad_board_meetings_payload_fails_its_own_row_and_leaves_the_flows(
    settings: Settings, tmp_path: Path
) -> None:
    evening = _ist(2026, 10, 19, 20, 0)
    owed = date(2026, 10, 19)
    h = _harness(settings, tmp_path, evening)
    script = dict(_flows_script(owed=owed))
    script[_bm_url(owed)] = RecordedResponse(body=b'{"error": "not an array"}')
    h.busy = {"nsearchives.nseindia.com"}
    report = dc.run_nse_daily_capture(h.at(evening, script))
    by_source = {o.source: o for o in report.outcomes}
    assert by_source[board_meetings.SOURCE_ID].failed
    record = h.tracker.get(board_meetings.SOURCE_ID, owed)
    assert record is not None and record.state is SyncState.FAILED and record.retryable
    assert "JSON array" in (record.last_error or "")


def test_the_backfill_is_one_lease_and_one_request_per_month(
    settings: Settings, tmp_path: Path
) -> None:
    now = _ist(2026, 10, 20, 10, 0)
    h = _harness(settings, tmp_path, now)
    windows = [
        (date(2026, 9, 15), date(2026, 9, 30)),
        (date(2026, 10, 1), date(2026, 10, 31)),
        (date(2026, 11, 1), date(2026, 11, 20)),
    ]
    script: dict[str, Any] = {WARM: RecordedResponse(status_code=403)}
    for first, last in windows:
        script[board_meetings.board_meetings_url(first, last, REGISTER)] = RecordedResponse(
            body=BOARD_MEETINGS if first.month == 10 else b"[]"
        )
    leases: list[tuple[str, ...]] = []
    ctx = h.at(now, script)
    inner = ctx.fetchers

    def counting(hosts: Sequence[str], command: str) -> AbstractContextManager[Fetcher]:
        leases.append(tuple(hosts))
        return inner(hosts, command)

    report = dc.backfill_board_meetings(
        replace(ctx, fetchers=counting), date(2026, 9, 15), date(2026, 11, 20)
    )
    assert leases == [("www.nseindia.com",)]
    assert [o.status for o in report.outcomes] == [CaptureStatus.CAPTURED] * 3
    assert h.urls() == [board_meetings.board_meetings_url(a, b, REGISTER) for a, b in windows]
    assert len(board_meetings.read_pit(date(2026, 10, 20), data_root=tmp_path)) == 17
    assert h.tracker.get(board_meetings.SOURCE_ID, date(2026, 10, 20)) is None  # no sync row


# ── shareholding: a later poll never drops an earlier filing ──────────────────────────────────

MASTER: Final = _fixture("nse_shareholding/json_v1/corporate-share-holdings-master_20260807.json")
MASTER_URL: Final = _url(shareholding.SOURCE_ID)


def test_a_later_poll_merges_into_the_filing_date_partitions(
    settings: Settings, tmp_path: Path
) -> None:
    first = _ist(2026, 8, 8, 12, 0)
    h = _harness(settings, tmp_path, first)
    script = {WARM: RecordedResponse(status_code=403), MASTER_URL: RecordedResponse(body=MASTER)}
    outcome = dc.capture_shareholding(h.at(first, script), _fetcher(h), poll_date=date(2026, 8, 8))
    assert outcome.status is CaptureStatus.CAPTURED and outcome.rows == 5

    # A week on, INE467B01029 has dropped off the master (it filed again elsewhere, say).
    records = [r for r in json.loads(MASTER) if r["isin"] != "INE467B01029"]
    later = _ist(2026, 8, 15, 12, 0)
    script = {
        WARM: RecordedResponse(status_code=403),
        MASTER_URL: RecordedResponse(body=json.dumps(records).encode()),
    }
    dc.capture_shareholding(h.at(later, script), _fetcher(h), poll_date=date(2026, 8, 15))
    assert h.state(shareholding.SOURCE_ID, date(2026, 8, 15)) is SyncState.PUBLISHED
    kept = shareholding.read_l1(date(2026, 4, 18), data_root=tmp_path)
    assert [row.isin for row in kept] == ["INE467B01029"]
    assert len(shareholding.read_pit(date(2026, 8, 15), data_root=tmp_path)) == 5


LIVE_MASTER: Final = _fixture(
    "nse_shareholding/master_2026_10/corporate-share-holdings-master_20261006.json"
)


def test_a_live_era_poll_lands_and_raises_bc3_not_applicable(
    settings: Settings, tmp_path: Path
) -> None:
    """D17 on the live path: the poll publishes, and every pledge-less row reaches the D7 sink."""
    now = _ist(2026, 10, 6, 13, 45)
    h = _harness(settings, tmp_path, now)
    raised: list[QualityFinding] = []
    script = {
        WARM: RecordedResponse(status_code=403),
        MASTER_URL: RecordedResponse(body=LIVE_MASTER),
    }
    ctx = replace(h.at(now, script), raise_findings=raised.extend)
    outcome = dc.capture_shareholding(ctx, _fetcher(h), poll_date=date(2026, 10, 6))

    assert outcome.status is CaptureStatus.CAPTURED and outcome.rows == 30
    assert h.state(shareholding.SOURCE_ID, date(2026, 10, 6)) is SyncState.PUBLISHED
    bc3 = [f for f in raised if f.check_name == shareholding.BC3_CHECK]
    assert len(bc3) == 30
    assert {f.detail["bc3_status"] for f in bc3} == {"not_applicable"}
    assert len([f for f in raised if f.check_name == shareholding.NO_ISIN_CHECK]) == 2


def test_rederive_rebuilds_l1_from_l0_with_no_fetcher(settings: Settings, tmp_path: Path) -> None:
    """The post-merge recovery path: L0 already holds the poll, so nothing is requested.

    The context's fetcher factory raises if it is ever entered, and the module's socket guard is
    still on — a re-derivation that reached for the network fails here twice over.
    """
    now = _ist(2026, 10, 7, 9, 0)
    h = _harness(settings, tmp_path, now)
    poll = date(2026, 10, 6)
    h.l0.put(shareholding.SOURCE_ID, poll, shareholding.l0_filename(poll), LIVE_MASTER)

    def no_fetchers(hosts: Sequence[str], command: str) -> AbstractContextManager[Fetcher]:
        raise AssertionError(f"rederive asked for a fetcher for {hosts}")

    raised: list[QualityFinding] = []
    ctx = replace(h.at(now, {}), fetchers=no_fetchers, raise_findings=raised.extend)
    # The live poll on 2026-10-06 failed on the old parser; the row is FAILED, retryable.
    h.tracker.begin(shareholding.SOURCE_ID, poll)
    h.tracker.mark_failed(shareholding.SOURCE_ID, poll, "ParseError: no 'public_prcnt' field")

    report = dc.rederive_shareholding(ctx)
    assert [o.status for o in report.outcomes] == [CaptureStatus.CAPTURED]
    assert report.requests == 0
    assert h.state(shareholding.SOURCE_ID, poll) is SyncState.PUBLISHED
    assert len(shareholding.read_l1(poll, data_root=tmp_path)) == 14
    assert len(shareholding.read_pit(poll, data_root=tmp_path)) == 30
    assert len(raised) == 32

    # A second pass merges the same rows again, leaves the date PUBLISHED, and fails nothing.
    again = dc.rederive_shareholding(ctx)
    assert [o.status for o in again.outcomes] == [CaptureStatus.ALREADY_PUBLISHED]
    assert len(shareholding.read_pit(poll, data_root=tmp_path)) == 30


#: A record-verbatim cut of the 2026-10-21 master: 41 records, two with a malformed ISIN.
BAD_ISIN_MASTER: Final = _fixture(
    "nse_shareholding/master_2026_10/corporate-share-holdings-master_20261021_subset.json"
)


def test_rederive_recovers_a_poll_a_malformed_isin_failed(
    settings: Settings, tmp_path: Path
) -> None:
    """2026-10-16..21: one bad ISIN failed the whole poll. L0 holds it, so recovery re-parses."""
    now = _ist(2026, 10, 22, 13, 0)
    h = _harness(settings, tmp_path, now)
    poll = date(2026, 10, 21)
    h.l0.put(shareholding.SOURCE_ID, poll, shareholding.l0_filename(poll), BAD_ISIN_MASTER)

    def no_fetchers(hosts: Sequence[str], command: str) -> AbstractContextManager[Fetcher]:
        raise AssertionError(f"rederive asked for a fetcher for {hosts}")

    raised: list[QualityFinding] = []
    ctx = replace(h.at(now, {}), fetchers=no_fetchers, raise_findings=raised.extend)
    h.tracker.begin(shareholding.SOURCE_ID, poll)
    h.tracker.mark_failed(
        shareholding.SOURCE_ID,
        poll,
        "ParseError: corporate-share-holdings-master_20261021.json: record 982: 1 validation "
        "error for ShareholdingRow",
    )

    report = dc.rederive_shareholding(ctx)
    assert [(o.status, o.rows) for o in report.outcomes] == [(CaptureStatus.CAPTURED, 38)]
    assert report.requests == 0
    assert h.state(shareholding.SOURCE_ID, poll) is SyncState.PUBLISHED
    assert len(shareholding.read_pit(poll, data_root=tmp_path)) == 38
    bad = [f for f in raised if f.check_name == shareholding.BAD_ISIN_CHECK]
    assert sorted(str(f.detail["raw_isin"]) for f in bad) == ["INE 439A0401", "INE07180108"]
    assert {f.detail["l0_key"] for f in bad} == {
        f"{shareholding.SOURCE_ID}/2026-10-21/corporate-share-holdings-master_20261021.json"
    }


def test_a_master_over_the_bad_isin_threshold_fails_the_day_and_writes_nothing(
    settings: Settings, tmp_path: Path
) -> None:
    """Every ISIN lower-cased is a format change: the date is FAILED and L1 is not touched."""
    now = _ist(2026, 10, 22, 18, 5)
    h = _harness(settings, tmp_path, now)
    poll = date(2026, 10, 22)
    lowered = [
        {**r, "isin": r["isin"].lower()} if isinstance(r["isin"], str) else r
        for r in json.loads(BAD_ISIN_MASTER)
    ]
    h.l0.put(
        shareholding.SOURCE_ID, poll, shareholding.l0_filename(poll), json.dumps(lowered).encode()
    )
    raised: list[QualityFinding] = []
    ctx = replace(h.at(now, {}), raise_findings=raised.extend)

    outcome = dc.capture_shareholding(ctx, _fetcher(h), poll_date=poll)
    assert outcome.status is CaptureStatus.FAILED
    assert "malformed 'isin'" in outcome.detail
    assert h.state(shareholding.SOURCE_ID, poll) is SyncState.FAILED
    assert h.urls() == []  # the payload was already in L0
    assert shareholding.read_pit(poll, data_root=tmp_path) == ()
    assert raised == []


# ── announcements: two exchanges, one partition ───────────────────────────────────────────────

NSE_ANN: Final = _fixture("announcements/nse/2026-08-07/corporate-announcements.json")
BSE_ANN: Final = _fixture("announcements/bse/2026-08-07/AnnSubCategoryGetData.json")


def _ann_urls(day: date) -> tuple[str, str]:
    nse = _url(ann.NSE_SOURCE_ID).replace("{DD-MM-YYYY}", f"{day:%d-%m-%Y}")
    bse = _url(ann.BSE_SOURCE_ID).replace("{YYYYMMDD}", f"{day:%Y%m%d}").replace("{N}", "1")
    return nse, bse


def _ann_fetchers(h: Harness, ctx: CaptureContext) -> dict[str, Fetcher | HostBusyError]:
    fetcher = _fetcher(h)
    return dict.fromkeys(dc.ANNOUNCEMENT_SOURCES, fetcher)


def test_nse_and_bse_land_in_one_partition_whichever_lands_second(
    settings: Settings, tmp_path: Path
) -> None:
    day = date(2026, 8, 7)
    nse_url, bse_url = _ann_urls(day)
    h = _harness(settings, tmp_path, _ist(2026, 8, 8, 0, 30))
    ctx = h.at(
        _ist(2026, 8, 8, 0, 30),
        {WARM: RecordedResponse(status_code=403), nse_url: RecordedResponse(body=NSE_ANN)},
    )
    busy = HostBusyError(
        LeaseHolder(
            host="api.bseindia.com",
            pid=1,
            machine="test",
            command="bse campaign",
            started_at=_ist(2026, 8, 8, 0, 0),
        )
    )
    first = dc.capture_announcements(
        ctx, {ann.NSE_SOURCE_ID: _fetcher(h), ann.BSE_SOURCE_ID: busy}, day=day
    )
    assert {o.source: o.status for o in first} == {
        ann.BSE_SOURCE_ID: CaptureStatus.FAILED,
        ann.NSE_SOURCE_ID: CaptureStatus.CAPTURED,
    }
    nse_only = ann.read_l1(day, data_root=tmp_path).rows

    ctx = h.at(_ist(2026, 8, 9, 0, 30), {bse_url: RecordedResponse(body=BSE_ANN)})
    second = dc.capture_announcements(ctx, _ann_fetchers(h, ctx), day=day)
    assert {o.source: o.status for o in second} == {
        ann.NSE_SOURCE_ID: CaptureStatus.ALREADY_PUBLISHED,
        ann.BSE_SOURCE_ID: CaptureStatus.CAPTURED,
    }
    rows = ann.read_l1(day, data_root=tmp_path).rows
    assert {row.source for row in rows} == {ann.NSE_SOURCE_ID, ann.BSE_SOURCE_ID}
    assert set(nse_only) <= set(rows)
    assert all(row.l0_key for row in rows)
    assert h.urls() == [bse_url]  # NSE re-derived from L0, not re-fetched


def test_a_shut_day_with_no_announcements_is_an_empty_capture(
    settings: Settings, tmp_path: Path
) -> None:
    sunday = date(2026, 8, 9)
    nse_url, bse_url = _ann_urls(sunday)
    h = _harness(settings, tmp_path, _ist(2026, 8, 10, 0, 30))
    empty_bse = json.dumps({"Table": [], "Table1": [{"ROWCNT": 0}]}).encode()
    ctx = h.at(
        _ist(2026, 8, 10, 0, 30),
        {
            WARM: RecordedResponse(status_code=403),
            nse_url: RecordedResponse(body=b"[]"),
            bse_url: RecordedResponse(body=empty_bse),
        },
    )
    outcomes = dc.capture_announcements(ctx, _ann_fetchers(h, ctx), day=sunday)
    assert {o.status for o in outcomes} == {CaptureStatus.CAPTURED}
    assert ann.read_l1(sunday, data_root=tmp_path).rows == ()


def test_an_empty_bse_session_is_the_empty_success_gotcha_not_a_quiet_day(
    settings: Settings, tmp_path: Path
) -> None:
    friday = date(2026, 8, 7)
    nse_url, bse_url = _ann_urls(friday)
    h = _harness(settings, tmp_path, _ist(2026, 8, 8, 0, 30))
    ctx = h.at(
        _ist(2026, 8, 8, 0, 30),
        {
            WARM: RecordedResponse(status_code=403),
            nse_url: RecordedResponse(body=NSE_ANN),
            bse_url: RecordedResponse(
                body=json.dumps({"Table": [], "Table1": [{"ROWCNT": 0}]}).encode()
            ),
        },
    )
    outcomes = {
        o.source: o for o in dc.capture_announcements(ctx, _ann_fetchers(h, ctx), day=friday)
    }
    assert outcomes[ann.BSE_SOURCE_ID].failed
    assert outcomes[ann.NSE_SOURCE_ID].status is CaptureStatus.CAPTURED


def test_bse_is_paged_by_its_own_row_count(settings: Settings, tmp_path: Path) -> None:
    friday = date(2026, 8, 7)
    nse_url, bse_page1 = _ann_urls(friday)
    bse_page2 = bse_page1.replace("pageno=1", "pageno=2")
    document = json.loads(BSE_ANN)
    page1 = {"Table": document["Table"][:2], "Table1": [{"ROWCNT": 3}]}
    page2 = {"Table": document["Table"][2:], "Table1": [{"ROWCNT": 3}]}
    h = _harness(settings, tmp_path, _ist(2026, 8, 8, 0, 30))
    ctx = h.at(
        _ist(2026, 8, 8, 0, 30),
        {
            WARM: RecordedResponse(status_code=403),
            nse_url: RecordedResponse(body=NSE_ANN),
            bse_page1: RecordedResponse(body=json.dumps(page1).encode()),
            bse_page2: RecordedResponse(body=json.dumps(page2).encode()),
        },
    )
    dc.capture_announcements(ctx, _ann_fetchers(h, ctx), day=friday)
    assert bse_page2 in h.urls()
    bse_rows = [
        r for r in ann.read_l1(friday, data_root=tmp_path).rows if r.source == ann.BSE_SOURCE_ID
    ]
    assert {row.isin for row in bse_rows} == {"INE0ROBO1018", "INE002A01018"}  # 999999 quarantined


# ── news: RSS + GDELT, one partition, four polls adding up ────────────────────────────────────

RSS: Final = _fixture("rss/rbi/2026-09-02/pressreleases_rss.xml")
MANIFEST: Final = _fixture("gdelt/v2/lastupdate.txt")
EXPORT: Final = _fixture("gdelt/v2/20260902074500.export.CSV.zip")
EXPORT_URL: Final = "http://data.gdeltproject.org/gdeltv2/20260902074500.export.CSV.zip"


def _news_script(export: bytes = EXPORT) -> Script:
    return {
        "https://www.rbi.org.in/pressreleases_rss.xml": RecordedResponse(body=RSS),
        _url("gdelt_v2_event_files"): RecordedResponse(body=MANIFEST),
        EXPORT_URL: RecordedResponse(body=export),
    }


def _news_partition(root: Path, day: date) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = pq.read_table(
        l1_partition_path(NEWS_DATASET, day, data_root=root)
    ).to_pylist()
    return rows


def test_rss_and_gdelt_share_the_day_and_a_second_poll_adds_rather_than_overwrites(
    settings: Settings, tmp_path: Path
) -> None:
    day = date(2026, 9, 2)
    h = _harness(settings, tmp_path, _ist(2026, 9, 2, 12, 15))
    first = dc.capture_news(h.at(_ist(2026, 9, 2, 12, 15), _news_script()))
    assert {o.status for o in first} == {CaptureStatus.CAPTURED}
    rows = _news_partition(tmp_path, day)
    assert {row["source"] for row in rows} == {"rbi_press_releases", "gdelt"}
    assert all(row["l0_key"] for row in rows)

    second = dc.capture_news(h.at(_ist(2026, 9, 2, 18, 15), _news_script()))
    statuses = {o.source.split("/", 1)[0]: o.status for o in second}
    assert statuses == {
        "curated_rss": CaptureStatus.CAPTURED,  # a new poll, its own sync row
        "gdelt_v2_event_files": CaptureStatus.ALREADY_PUBLISHED,  # the same slot as at 12:15
    }
    assert EXPORT_URL not in h.urls()
    assert len(_news_partition(tmp_path, day)) == len(rows)  # RSS repeats deduplicated


def test_a_corrupt_gdelt_export_never_becomes_news(settings: Settings, tmp_path: Path) -> None:
    day = date(2026, 9, 2)
    h = _harness(settings, tmp_path, _ist(2026, 9, 2, 12, 15))
    outcomes = dc.capture_news(h.at(_ist(2026, 9, 2, 12, 15), _news_script(export=EXPORT[:-10])))
    gdelt_outcome = next(o for o in outcomes if o.source.startswith("gdelt_v2_event_files"))
    assert gdelt_outcome.failed and "MD5" in gdelt_outcome.detail
    assert {row["source"] for row in _news_partition(tmp_path, day)} == {"rbi_press_releases"}


def test_only_ratified_feeds_are_polled(settings: Settings, tmp_path: Path) -> None:
    """Business Standard is a 403 WAF and was ordered removed (D7): it is never requested."""
    h = _harness(settings, tmp_path, _ist(2026, 9, 2, 12, 15))
    dc.capture_news(h.at(_ist(2026, 9, 2, 12, 15), _news_script()))
    assert not any("business-standard" in url for url in h.urls())
    assert [feed.id for feed in dc._ratified_feeds(h.at(_ist(2026, 9, 2, 12, 15), {}))] == [
        "rbi_press_releases"
    ]
