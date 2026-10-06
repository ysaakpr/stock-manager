"""The F&O history backfill runner, offline: resume, 404 closure, L0 reuse, 403 park, era guard.

The network is a `RecordedTransport` serving the captured 2026-08-07 UDiFF F&O file
(`tests/fixtures/nse_fo/udiff/`); the lake is a temp directory and `sync_state` an in-memory
stand-in.
"""

from __future__ import annotations

import socket
from datetime import date, datetime
from pathlib import Path
from typing import Any, Final

import pytest

from dataplatform.alerts import build_alerter
from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.ingest import fo_backfill as fob
from dataplatform.ingest.calendar import trading_calendar
from dataplatform.ingest.fetcher import Fetcher, RecordedResponse, RecordedTransport
from dataplatform.ingest.source_register import load as load_register
from dataplatform.status.sync_state import SyncState
from dataplatform.store.fo_aggregates import read_l1, read_l2
from dataplatform.store.l0 import L0Store

FIXTURE: Final = (
    Path(__file__).parents[1]
    / "fixtures"
    / "nse_fo"
    / "udiff"
    / "BhavCopy_NSE_FO_0_0_0_20260807_F_0000.csv.zip"
)
SESSION: Final = date(2026, 8, 7)
MISSING: Final = date(2026, 8, 6)
CLOCK: Final = FrozenClock(datetime(2026, 10, 6, 14, 0, tzinfo=IST))


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a test opened a socket; fo-backfill tests are offline")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


class _Row:
    def __init__(self, state: SyncState) -> None:
        self.state = state
        self.retryable = True


class _FakeSync:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, date], _Row] = {}

    def get(self, source: str, logical_date: date) -> _Row | None:
        return self.rows.get((source, logical_date))

    def begin(self, source: str, logical_date: date) -> _Row:
        prior = self.rows.get((source, logical_date))
        assert prior is None or (prior.state is not SyncState.PUBLISHED and prior.retryable)
        self.rows[(source, logical_date)] = _Row(SyncState.PENDING)
        return self.rows[(source, logical_date)]

    def _to(self, source: str, logical_date: date, state: SyncState) -> _Row:
        self.rows[(source, logical_date)].state = state
        return self.rows[(source, logical_date)]

    def mark_fetched(
        self, source: str, logical_date: date, *, checksum: str, l0_path: str | None = None
    ) -> _Row:
        return self._to(source, logical_date, SyncState.FETCHED)

    def mark_validated(self, source: str, logical_date: date) -> _Row:
        return self._to(source, logical_date, SyncState.VALIDATED)

    def mark_normalized(self, source: str, logical_date: date) -> _Row:
        return self._to(source, logical_date, SyncState.NORMALIZED)

    def mark_published(self, source: str, logical_date: date) -> _Row:
        return self._to(source, logical_date, SyncState.PUBLISHED)

    def mark_failed(
        self, source: str, logical_date: date, error: str, *, retryable: bool = True
    ) -> _Row:
        row = self.rows.setdefault((source, logical_date), _Row(SyncState.PENDING))
        row.state, row.retryable = SyncState.FAILED, retryable
        return row


def _plan(*sessions: date) -> list[fob.FoSessionUnit]:
    plan = fob.build_plan(
        date(2026, 8, 3), date(2026, 8, 14), calendar=trading_calendar(), register=load_register()
    )
    return [unit for unit in plan if unit.session in set(sessions)]


def _runner(transport: RecordedTransport, tmp_path: Path, sync: _FakeSync) -> fob.FoBackfillRunner:
    settings = Settings(data_root=tmp_path)
    fetcher = Fetcher(
        transport=transport,
        l0=L0Store(clock=CLOCK, data_root=tmp_path),
        alerter=build_alerter(settings, clock=CLOCK),
        clock=CLOCK,
        register=load_register(),
        settings=settings,
        sleep=lambda _seconds: None,
    )
    return fob.FoBackfillRunner(
        fetcher=fetcher,
        l0=L0Store(clock=CLOCK, data_root=tmp_path),
        sync=sync,
        commit=lambda: None,
        data_root=tmp_path,
    )


def _transport(plan: list[fob.FoSessionUnit]) -> RecordedTransport:
    return RecordedTransport(
        {
            unit.url: RecordedResponse(body=FIXTURE.read_bytes())
            if unit.session == SESSION
            else RecordedResponse(status_code=404, body=b"<html>no</html>")
            for unit in plan
        }
    )


def test_plan_refuses_the_legacy_era_and_names_the_udiff_url() -> None:
    plan = _plan(SESSION)
    assert plan[0].url.endswith("/content/fo/BhavCopy_NSE_FO_0_0_0_20260807_F_0000.csv.zip")
    with pytest.raises(ValueError, match="UDiFF F&O era"):
        fob.build_plan(
            date(2024, 7, 1),
            date(2024, 7, 10),
            calendar=trading_calendar(),
            register=load_register(),
        )


def test_backfill_lands_l1_and_l2_and_a_rerun_redoes_nothing(tmp_path: Path) -> None:
    plan = _plan(MISSING, SESSION)
    sync = _FakeSync()
    first = _runner(_transport(plan), tmp_path, sync).run(plan)
    assert first.published == 1 and first.not_published == 1 and first.requests == 2
    assert read_l1(SESSION, data_root=tmp_path) and read_l2(SESSION, data_root=tmp_path)

    second = _runner(RecordedTransport({}), tmp_path, sync).run(plan)
    assert second.requests == 0 and second.resumed == 1 and second.closed == 1


def test_a_payload_in_l0_is_reparsed_not_refetched(tmp_path: Path) -> None:
    plan = _plan(SESSION)
    L0Store(clock=CLOCK, data_root=tmp_path).put(
        fob.FO_SOURCE_ID, SESSION, plan[0].filename, FIXTURE.read_bytes()
    )
    report = _runner(RecordedTransport({}), tmp_path, _FakeSync()).run(plan)
    assert report.published == 1 and report.requests == 0 and report.l0_reused == 1


def test_403_spike_parks_with_enumerated_cause(tmp_path: Path) -> None:
    plan = fob.build_plan(
        date(2026, 8, 3), date(2026, 8, 31), calendar=trading_calendar(), register=load_register()
    )
    forbidden = RecordedTransport(
        {unit.url: RecordedResponse(status_code=403, body=b"no") for unit in plan}
    )
    sync = _FakeSync()
    report = _runner(forbidden, tmp_path, sync).run(plan)
    assert report.parked and report.park_reason is fob.ParkReason.FORBIDDEN_SPIKE
    assert sync.get(fob.FO_SOURCE_ID, plan[-1].session) is None


#: Four rows copied verbatim from the first real UDiFF F&O file fetched (2024-07-08, L0 payload
#: `nse_fo_bhavcopy/2024/07/BhavCopy_NSE_FO_0_0_0_20240708_F_0000.csv.zip`), one per instrument
#: code. The real file writes `STO`/`STF`/`IDO`/`IDF`; the synthetic M3.7 fixture wrote the legacy
#: `OPTSTK`/`FUTSTK`/`OPTIDX`/`FUTIDX`, which is why the parser refused every real session.
REAL_EXCERPT: Final = (
    "TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,"
    "FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,"
    "PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,"
    "TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4\n"
    "2024-07-08,2024-07-08,FO,NSE,STO,151808,,RELIANCE,,2024-09-26,2024-09-26,3440.00,PE,"
    "RELIANCE24SEP3440PE,0.00,0.00,0.00,378.30,0.00,378.30,3201.80,275.85,0,0,0,0.00,0,F1,250,"
    ",,,,\n"
    "2024-07-08,2024-07-08,FO,NSE,STF,63806,,RELIANCE,,2024-07-25,2024-07-25,,,RELIANCE24JULFUT,"
    "3190.00,3224.30,3172.00,3208.10,3206.15,3187.35,3201.80,3208.10,28804250,-1499500,39175,"
    "31353890650.00,32467,F1,250,,,,,\n"
    "2024-07-08,2024-07-08,FO,NSE,IDO,56755,,NIFTY,,2024-08-29,2024-08-29,23350.00,PE,"
    "NIFTY24AUG23350PE,141.35,148.40,138.00,147.30,147.30,132.85,24320.55,181.40,5075,25,6,"
    "3524092.50,4,F1,25,,,,,\n"
    "2024-07-08,2024-07-08,FO,NSE,IDF,35000,,NIFTY,,2024-09-26,2024-09-26,,,NIFTY24SEPFUT,"
    "24630.00,24640.00,24534.10,24620.85,24618.00,24624.45,24320.55,24620.85,188450,12750,2551,"
    "1568144165.00,1651,F1,25,,,,,\n"
)


def test_the_real_udiff_instrument_codes_parse() -> None:
    from dataplatform.ingest.nse.fo_bhavcopy import FoInstrumentType, parse_text

    rows = parse_text(REAL_EXCERPT, filename="BhavCopy_NSE_FO_0_0_0_20240708_F_0000.csv")
    assert [row.instrument_type for row in rows] == [
        FoInstrumentType.OPTSTK,
        FoInstrumentType.FUTSTK,
        FoInstrumentType.OPTIDX,
        FoInstrumentType.FUTIDX,
    ]
