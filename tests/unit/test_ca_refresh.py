"""DQ — the scheduled corporate-action refresh: bounded, keyed apart from the backfill.

The CA store stopped at the backfill's last chunk (2026-09-01), and two September 2:1 splits reached
L2 only through the price-implied detector. These tests hold the refresh that replaces that
inference with the published record to the properties that make it safe to run weekly:

* its NSE unit is keyed on the refresh date, so the backfill's PUBLISHED `2026-09-01` chunk can
  never resume-skip it, and a second refresh the same day makes no request;
* its BSE plan is only the counterparts NSE has no twin for, not the ~6,700-scrip universe;
* it reconciles under the lake's ACCEPT policy — under QUEUE a single-feed split would never reach
  a factor — and recomputes, and so invalidates, only the ISINs whose chains moved;
* a failed unit makes the scheduled run FAILED after what landed was finalized.

Offline (B8): the backfill suite's recorded transport, in-memory sync store and SQL stand-in.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

from dataplatform.clock import IST, FrozenClock
from dataplatform.corpactions.reconcile import SingleSourcePolicy
from dataplatform.ingest import ca_refresh
from dataplatform.ingest import corp_actions_backfill as cab
from dataplatform.ingest.bse import corp_actions as bse_ca
from dataplatform.ingest.ca_refresh import (
    NSE_LOOKBACK_DAYS,
    NSE_REFRESH_STATE_SOURCE,
    REFRESH_POLICY,
    CaRefreshError,
    CaRefreshReport,
    build_bse_refresh_units,
    build_nse_refresh_unit,
    refresh_corporate_actions,
    unmatched_nse_isins,
)
from dataplatform.ingest.fetcher import RecordedResponse, RecordedTransport
from dataplatform.ingest.source_register import load as load_register
from dataplatform.scheduler.registry import BSE_CA_SWEEP, CA_REFRESH, JobContext
from dataplatform.status.sync_state import SyncState
from dataplatform.store.db import Connection
from dataplatform.store.l0 import L0Store
from tests.conftest import SettingsLoader
from tests.unit.test_ca_backfill import (
    CLOCK,
    NSE_FIXTURE,
    UNIVERSE,
    WARM_URL,
    _bse_payloads,
    _FakeConn,
    _FakeSync,
    _fetcher,
    _master,
    _settings,
)

#: One backfill chunk's worth of window around the fixture's 2024 ex-dates.
FROM = date(2024, 1, 1)
TO = date(2024, 12, 31)

#: The scrips whose NSE action falls in the window — HDFC's 2023 amalgamation does not.
IN_WINDOW_SCRIPS = sorted(scrip for isin, _, scrip in UNIVERSE if isin != "INE001A01036")


def _transport(nse_urls: Sequence[str], bse: dict[str, bytes]) -> RecordedTransport:
    """NSE fixture bytes for each NSE URL, and per-scrip BSE bytes (an empty list if none)."""
    json_ok = {"content-type": "application/json"}
    script: dict[str, Any] = {
        WARM_URL: RecordedResponse(status_code=200, body=b"", headers={"content-type": "text/html"})
    }
    for url in nse_urls:
        script[url] = RecordedResponse(
            status_code=200, body=NSE_FIXTURE.read_bytes(), headers=json_ok
        )
    template = next(s for s in load_register().sources if s.id == bse_ca.SOURCE_ID).url_template
    for _, _, scrip in UNIVERSE:
        script[template.replace("{SCRIP_CD}", scrip)] = RecordedResponse(
            status_code=200, body=bse.get(scrip, b"[]"), headers=json_ok
        )
    return RecordedTransport(script)


def _refresh(
    tmp_path: Path,
    conn: _FakeConn,
    sync: _FakeSync,
    *,
    to_date: date = TO,
    bse: dict[str, bytes] | None = None,
    drained: list[int] | None = None,
) -> tuple[CaRefreshReport, RecordedTransport]:
    register = load_register()
    unit = build_nse_refresh_unit(FROM, to_date, register=register)
    transport = _transport([unit.url], _bse_payloads() if bse is None else bse)
    settings = _settings(tmp_path)

    def drain() -> int:
        if drained is not None:
            drained.append(1)
        return 0

    report = refresh_corporate_actions(
        from_date=FROM,
        to_date=to_date,
        fetcher=_fetcher(transport, settings),
        l0=L0Store(clock=CLOCK, data_root=settings.data_root),
        sync=cast("Any", sync),
        conn=cast("Connection", conn),
        commit=lambda: None,
        master=_master(),
        clock=CLOCK,
        register=register,
        drain_l2=drain,
    )
    return report, transport


def _open_invalidations(conn: _FakeConn) -> set[str]:
    return {inv["isin"] for inv in conn._invalidations if not inv["resolved"]}


def _bse_requests(transport: RecordedTransport) -> list[str]:
    return [r.url for r in transport.requests if "bseindia" in r.url]


def _scrip(url: str) -> str:
    return url.split("scripcode=", 1)[1].split("&", 1)[0]


# ── the key: a refresh is never mistaken for a backfill chunk ────────────────────────────────


def test_the_refresh_is_keyed_on_its_own_date_not_the_backfill_chunk_start() -> None:
    register = load_register()
    unit = build_nse_refresh_unit(date(2026, 9, 1), date(2026, 10, 5), register=register)
    [chunk] = cab.build_nse_units(date(2026, 9, 1), date(2026, 10, 5), register=register)
    assert (unit.state_source, unit.logical_date) == (NSE_REFRESH_STATE_SOURCE, date(2026, 10, 5))
    assert (chunk.state_source, chunk.logical_date) == (cab.NSE_STATE_SOURCE, date(2026, 9, 1))
    assert "from_date=01-09-2026&to_date=05-10-2026" in unit.url


def test_a_published_backfill_chunk_on_the_window_start_does_not_skip_the_refresh(
    tmp_path: Path,
) -> None:
    """The live store's state: `nse_corp_actions` 2026-09-01 is PUBLISHED from the campaign."""
    sync = _FakeSync()
    sync.begin(cab.NSE_STATE_SOURCE, FROM)
    sync.mark_published(cab.NSE_STATE_SOURCE, FROM)
    report, _ = _refresh(tmp_path, _FakeConn(), sync)
    assert report.nse.published == 1
    assert report.nse.skipped_published == 0
    assert report.nse.actions_persisted > 0
    row = sync.get(NSE_REFRESH_STATE_SOURCE, TO)
    assert row is not None and row.state is SyncState.PUBLISHED


def test_a_window_the_backfill_would_chunk_is_refused() -> None:
    with pytest.raises(ValueError, match="at most twelve months"):
        build_nse_refresh_unit(date(2016, 9, 1), date(2026, 9, 1), register=load_register())


def test_two_bse_refreshes_in_one_month_never_share_an_l0_payload() -> None:
    """L0 is partitioned by month; the backfill's bare `defaultdata_<scrip>.json` would collide."""
    register = load_register()
    first = build_bse_refresh_units(["500470"], as_of=date(2026, 10, 3), register=register)
    second = build_bse_refresh_units(["500470"], as_of=date(2026, 10, 10), register=register)
    assert first[0].filename != second[0].filename
    assert (first[0].state_source, first[0].logical_date) == (
        "bse_corp_actions/500470",
        date(2026, 10, 3),
    )


# ── what it fetches, reconciles and invalidates ─────────────────────────────────────────────


def test_a_refresh_lands_both_feeds_recomputes_and_drains(tmp_path: Path) -> None:
    conn = _FakeConn()
    drained: list[int] = []
    report, transport = _refresh(tmp_path, conn, _FakeSync(), drained=drained)

    assert report.clean, report.summary()
    # BSE is asked only for the counterparts of the window's NSE actions, never the universe.
    assert report.bse_scrips == len(IN_WINDOW_SCRIPS)
    assert sorted(_scrip(u) for u in _bse_requests(transport)) == IN_WINDOW_SCRIPS
    assert report.finalize is not None and report.finalize.factor_rows > 0
    assert conn.factor_rows_for("INE081A01020"), "TATASTEEL's 1:10 split must reach a factor"
    assert "INE081A01020" in _open_invalidations(conn)
    assert drained == [1]


def test_a_refresh_that_changes_nothing_invalidates_nothing(tmp_path: Path) -> None:
    conn, sync = _FakeConn(), _FakeSync()
    _refresh(tmp_path, conn, sync)
    for inv in conn._invalidations:  # the first refresh's queue, drained
        inv["resolved"] = True

    # Same day: the NSE unit is PUBLISHED, and every BSE twin already exists — no request at all.
    same_day, transport = _refresh(tmp_path, conn, sync)
    assert transport.requests == []
    assert same_day.finalize is not None and same_day.finalize.isins_recomputed == 0

    # The next week re-reads the window (new key, one request), finds every action known and
    # matched, asks BSE for nothing, and moves no chain — so it raises no L2 invalidation.
    next_week, transport = _refresh(tmp_path, conn, sync, to_date=date(2024, 12, 30))
    assert next_week.nse.published == 1 and next_week.nse.actions_persisted == 0
    assert _bse_requests(transport) == []
    assert next_week.finalize is not None
    assert next_week.finalize.isins_recomputed == 0
    assert next_week.finalize.l2_invalidated == 0
    assert _open_invalidations(conn) == set()


def test_only_the_isin_whose_chain_moved_is_recomputed(tmp_path: Path) -> None:
    """A BSE twin arriving later re-reconciles one ISIN; the others' L2 is left alone."""
    conn, sync = _FakeConn(), _FakeSync()
    # First week: BSE has nothing yet, so every in-window action is single-source (ACCEPTed).
    _refresh(tmp_path, conn, sync, bse={})
    for inv in conn._invalidations:
        inv["resolved"] = True
    # Next week BSE publishes a dividend NSE does not carry, for TATASTEEL only.
    [split] = json.loads(_bse_payloads()["500470"])
    dividend = {
        **split,
        "Ex_date": "20 Jun 2024",
        "exdate": "2024-06-20T00:00:00",
        "Purpose": "Final Dividend - Rs. - 3.6000",
    }
    tata_bse = json.dumps([split, dividend]).encode("utf-8")
    report, _ = _refresh(tmp_path, conn, sync, to_date=date(2024, 12, 30), bse={"500470": tata_bse})
    assert report.bse.actions_persisted >= 1, report.summary()
    assert report.finalize is not None
    assert report.finalize.isins_recomputed == 1
    assert _open_invalidations(conn) == {"INE081A01020"}


def test_the_refresh_reconciles_under_the_lakes_accept_policy(tmp_path: Path) -> None:
    """Inverted to QUEUE, a split only NSE has published yet would never reach a factor."""
    assert REFRESH_POLICY is SingleSourcePolicy.ACCEPT
    conn = _FakeConn()
    report, _ = _refresh(tmp_path, conn, _FakeSync(), bse={})
    assert report.bse.actions_persisted == 0
    assert conn.factor_rows_for("INE081A01020")


def test_unmatched_isins_are_the_nse_actions_in_window_with_no_bse_twin(tmp_path: Path) -> None:
    conn = _FakeConn()
    _refresh(tmp_path, conn, _FakeSync(), bse={})
    unmatched = unmatched_nse_isins(cast("Connection", conn), FROM, TO)
    assert unmatched == {isin for isin, _, _ in UNIVERSE if isin != "INE001A01036"}
    assert "INE001A01036" in unmatched_nse_isins(cast("Connection", conn), date(2023, 7, 1), TO)


# ── the job: bounded window, fails loud ─────────────────────────────────────────────────────

SATURDAY = datetime(2026, 10, 10, 10, 0, tzinfo=IST)


def _report(failures: list[tuple[str, str]]) -> CaRefreshReport:
    empty = cab.CaBackfillReport(requested=0)
    return CaRefreshReport(
        window=(date(2026, 9, 5), date(2026, 10, 10)),
        nse=empty,
        bse=empty,
        bse_scrips=0,
        failures=failures,
    )


@pytest.mark.parametrize(("job", "sweep"), [(CA_REFRESH, False), (BSE_CA_SWEEP, True)])
def test_the_jobs_refresh_the_trailing_window_ending_today(
    job: Any, sweep: bool, load_settings: SettingsLoader, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def fake_run(**kwargs: Any) -> CaRefreshReport:
        seen.update(kwargs)
        return _report([])

    monkeypatch.setattr(ca_refresh, "_run", fake_run)
    context = JobContext(
        job_name=job.name, run_id=uuid4(), clock=FrozenClock(SATURDAY), settings=load_settings(None)
    )
    job.fn(context)
    assert seen["to_date"] == date(2026, 10, 10)
    assert (seen["to_date"] - seen["from_date"]).days == NSE_LOOKBACK_DAYS
    assert seen["sweep"] is sweep
    assert seen["rebuild_l2"] is True
    assert seen["command"] == job.name


def test_a_failed_unit_fails_the_job(
    load_settings: SettingsLoader, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ca_refresh, "_run", lambda **_: _report([("BSE scrip 500470", "HTTP 500")]))
    context = JobContext(
        job_name="ca_refresh",
        run_id=uuid4(),
        clock=FrozenClock(SATURDAY),
        settings=load_settings(None),
    )
    with pytest.raises(CaRefreshError, match="500470"):
        CA_REFRESH.fn(context)
