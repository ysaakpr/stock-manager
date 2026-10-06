"""The L0-only acquisition driver, offline (B8).

What it must prove: a payload lands in L0 under the exact key the backfill would have written (so a
later `backfill` derives L1 from it without a request), a key L0 already holds costs no request, an
archive's non-2xx is named with its status code rather than skipped, and a stored key is never
overwritten.
"""

from __future__ import annotations

import socket
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest

from dataplatform.alerts import AlertOutcome, Severity
from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.ingest.backfill import (
    BSE_BHAVCOPY_LEGACY,
    NSE_BHAVCOPY,
    NSE_DELIVERY,
    SOURCE_SETS,
)
from dataplatform.ingest.fetcher import Fetcher, RecordedResponse, RecordedTransport
from dataplatform.ingest.indices import TRI_SOURCE_ID, l0_tri_filename, tri_request_body
from dataplatform.ingest.l0_acquire import (
    AcquireStatus,
    acquire,
    price_units,
    tri_units,
)
from dataplatform.ingest.source_register import SourceRegister
from dataplatform.ingest.source_register import load as load_register
from dataplatform.ingest.tri_backfill import DEFAULT_INDEX_SET
from dataplatform.store.l0 import L0Store
from tests.conftest import SettingsLoader

NOW: Final = datetime(2026, 10, 5, 16, 0, tzinfo=IST)


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a test opened a socket; the acquisition tests are offline")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


class _Alerter:
    def send(self, severity: Severity, title: str, body: str, dedup_key: str) -> AlertOutcome:
        return AlertOutcome.SENT


@pytest.fixture
def register() -> SourceRegister:
    return load_register()


@pytest.fixture
def settings(load_settings: SettingsLoader) -> Settings:
    return load_settings(None)


def _wire(
    script: dict[str, Any], *, settings: Settings, register: SourceRegister, root: Path
) -> tuple[Fetcher, L0Store, RecordedTransport]:
    clock = FrozenClock(NOW)
    transport = RecordedTransport(script)
    l0 = L0Store(clock=clock, data_root=root)
    fetcher = Fetcher(
        transport=transport,
        l0=l0,
        alerter=_Alerter(),
        clock=clock,
        register=register,
        settings=settings,
        sleep=lambda seconds: clock.advance(timedelta(seconds=seconds)),
    )
    return fetcher, l0, transport


def test_units_are_the_backfills_own_requests(register: SourceRegister) -> None:
    """Same URL, register id and filename as `SOURCE_SETS[...].build_request` — never re-spelled."""
    for name in (NSE_BHAVCOPY, NSE_DELIVERY):
        units = price_units(name, date(2026, 9, 28), date(2026, 10, 1), register=register)
        assert [u.logical_date for u in units] == [
            date(2026, 9, 28),
            date(2026, 9, 29),
            date(2026, 9, 30),
            date(2026, 10, 1),
        ]
        for unit in units:
            request = SOURCE_SETS[name].build_request(unit.logical_date, register)
            assert (unit.source_id, unit.url, unit.filename) == (
                request.fetch_source,
                request.url,
                request.filename,
            )


def test_a_weekend_and_a_holiday_are_not_units(register: SourceRegister) -> None:
    # 2026-10-02 is Gandhi Jayanti, 10-03/04 a weekend: one session owed across five days.
    units = price_units(NSE_BHAVCOPY, date(2026, 10, 1), date(2026, 10, 4), register=register)
    assert [u.logical_date for u in units] == [date(2026, 10, 1)]


def test_tri_units_match_ingest_tri(register: SourceRegister) -> None:
    spec = DEFAULT_INDEX_SET[0]
    (unit,) = tri_units([spec], end=date(2026, 10, 5), register=register)
    assert unit.source_id == TRI_SOURCE_ID
    assert unit.logical_date == date(2026, 10, 5)
    assert unit.filename == l0_tri_filename(spec.slug, date(1990, 4, 1), date(2026, 10, 5))
    assert unit.body == tri_request_body(spec.name, date(1990, 4, 1), date(2026, 10, 5))


def test_fetches_the_missing_skips_the_present_and_names_the_absent(
    settings: Settings, register: SourceRegister, tmp_path: Path
) -> None:
    present, fresh, absent = price_units(
        NSE_BHAVCOPY, date(2026, 9, 29), date(2026, 10, 1), register=register
    )
    fetcher, l0, transport = _wire(
        {
            fresh.url: RecordedResponse(body=b"fresh-bytes"),
            absent.url: RecordedResponse(status_code=404, body=b"not here"),
        },
        settings=settings,
        register=register,
        root=tmp_path,
    )
    l0.put(present.source_id, present.logical_date, present.filename, b"already-held")

    report = acquire([present, fresh, absent], fetcher=fetcher, l0=l0)

    statuses = [o.status for o in report.outcomes]
    assert statuses == [AcquireStatus.PRESENT, AcquireStatus.FETCHED, AcquireStatus.MISSING]
    assert report.outcomes[2].http_status == 404
    assert not report.clean
    # The present key cost no request; only the two absent ones were asked for.
    assert [r.url for r in transport.requests] == [fresh.url, absent.url]
    stored = l0.get(l0.ref_for(fresh.source_id, fresh.logical_date, fresh.filename))
    assert stored == b"fresh-bytes"
    # And the held key is untouched — acquisition never overwrites.
    held = l0.get(l0.ref_for(present.source_id, present.logical_date, present.filename))
    assert held == b"already-held"
    assert not l0.exists(absent.source_id, absent.logical_date, absent.filename)


def test_a_clean_run_is_clean(settings: Settings, register: SourceRegister, tmp_path: Path) -> None:
    (unit,) = price_units(NSE_BHAVCOPY, date(2026, 10, 1), date(2026, 10, 1), register=register)
    fetcher, l0, _ = _wire(
        {unit.url: RecordedResponse(body=b"x")}, settings=settings, register=register, root=tmp_path
    )
    report = acquire([unit], fetcher=fetcher, l0=l0)
    assert report.clean
    # A second run is a no-op: zero requests, everything PRESENT.
    fetcher2, l0b, transport2 = _wire({}, settings=settings, register=register, root=tmp_path)
    again = acquire([unit], fetcher=fetcher2, l0=l0b)
    assert again.clean and transport2.requests == []
    assert [o.status for o in again.outcomes] == [AcquireStatus.PRESENT]


# ── BSE legacy: the archive answers a missing date with 200 and its HTML shell ───────────────

BSE_2006: Final = Path(__file__).resolve().parents[1] / "fixtures" / "bse_bhavcopy" / "legacy-2006"


def test_bse_legacy_units_are_the_backfills_own_requests(register: SourceRegister) -> None:
    (unit,) = price_units(
        BSE_BHAVCOPY_LEGACY, date(2006, 4, 3), date(2006, 4, 3), register=register
    )
    request = SOURCE_SETS[BSE_BHAVCOPY_LEGACY].build_request(unit.logical_date, register)
    assert (unit.source_id, unit.url, unit.filename) == (
        request.fetch_source,
        request.url,
        request.filename,
    )
    assert unit.filename == "EQ030406_CSV.ZIP"


def test_a_bse_html_shell_is_a_soft_404_not_a_fetched_session(
    settings: Settings, register: SourceRegister, tmp_path: Path
) -> None:
    """Frozen 2026-10-06: EQ030106 (2006-01-03) answered 200 text/html; EQ030406 a real zip.

    Counting the shell as FETCHED would report a session acquired that BSE never published.
    """
    (shell_unit,) = price_units(
        BSE_BHAVCOPY_LEGACY, date(2006, 1, 3), date(2006, 1, 3), register=register
    )
    (real_unit,) = price_units(
        BSE_BHAVCOPY_LEGACY, date(2006, 4, 3), date(2006, 4, 3), register=register
    )
    fetcher, l0, _ = _wire(
        {
            shell_unit.url: RecordedResponse(
                body=(BSE_2006 / "EQ030106_soft404.html").read_bytes(),
                headers={"content-type": "text/html"},
            ),
            real_unit.url: RecordedResponse(body=(BSE_2006 / "EQ030406_CSV.ZIP").read_bytes()),
        },
        settings=settings,
        register=register,
        root=tmp_path,
    )

    report = acquire([shell_unit, real_unit], fetcher=fetcher, l0=l0)

    assert [o.status for o in report.outcomes] == [AcquireStatus.SOFT_404, AcquireStatus.FETCHED]
    assert report.outcomes[0].http_status == 200
    assert not report.clean

    # Resume re-reads the stored shell and still calls it absent, at zero requests.
    fetcher2, l0b, transport2 = _wire({}, settings=settings, register=register, root=tmp_path)
    again = acquire([shell_unit, real_unit], fetcher=fetcher2, l0=l0b)
    assert transport2.requests == []
    assert [o.status for o in again.outcomes] == [AcquireStatus.SOFT_404, AcquireStatus.PRESENT]
