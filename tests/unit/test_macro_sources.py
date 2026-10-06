"""The macro-probes parsers and capture steps, against the real 2026-10-06 captures.

Offline: every payload is a frozen fixture under `tests/fixtures/macro/` and every fetch goes
through `RecordedTransport`. The PIT properties are asserted the way they would fail if inverted —
a World Bank figure must be *absent* from an as-of date before its vintage, a capture of an
unchanged table must write *nothing*, and a revised provisional month must land as a second record.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest

from dataplatform.alerts import AlertOutcome, Severity
from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.ingest.fetcher import Fetcher, RecordedResponse, RecordedTransport
from dataplatform.ingest.macro.capture import (
    backfill_fbil,
    capture_fbil,
    capture_gst,
    capture_india_vix,
    capture_rbi_rates,
    capture_wpi,
    new_or_revised,
)
from dataplatform.ingest.macro.fbil import (
    fbil_series_id,
    fbil_url,
    parse_fbil_reference_rates,
)
from dataplatform.ingest.macro.gst import GST_COLLECTION_URL, parse_gst_collections
from dataplatform.ingest.macro.india_vix import INDIA_VIX_URL, parse_india_vix_history
from dataplatform.ingest.macro.models import Frequency, MacroFact, MacroRelease, Unit, store_value
from dataplatform.ingest.macro.rbi_rates import RBI_HOME_URL, parse_rbi_current_rates
from dataplatform.ingest.macro.worldbank import WORLDBANK_SERIES, parse_worldbank
from dataplatform.ingest.macro.wpi import (
    WPI_DOWNLOAD_PAGE_URL,
    parse_wpi_download_page,
    parse_wpi_monthly_index,
)
from dataplatform.ingest.models import ParseError
from dataplatform.ingest.source_register import load as load_register
from dataplatform.store.l0 import L0Store
from dataplatform.store.macro_series import read_latest, read_pit, write_release
from tests.conftest import SettingsLoader

FIX: Final = Path("tests/fixtures/macro")
CAPTURED: Final = date(2026, 10, 6)


def _bytes(*parts: str) -> bytes:
    return FIX.joinpath(*parts).read_bytes()


# ── World Bank ──────────────────────────────────────────────────────────────────────────────


def test_worldbank_dates_every_fact_by_the_envelope_not_the_year() -> None:
    release = parse_worldbank(
        _bytes("worldbank", "2026-10-06", "IND_FP.CPI.TOTL.ZG.json"),
        spec=WORLDBANK_SERIES[0],
        filename="wb.json",
    )
    assert release.release_date == date(2026, 7, 13)
    assert len(release.facts) == 66
    assert {fact.release_date for fact in release.facts} == {date(2026, 7, 13)}
    by_year = {fact.period_end.year: fact for fact in release.facts}
    assert by_year[2025].value == Decimal("2.398850")
    assert by_year[1960].series_id == "IN.WB.CPI_INFLATION.ANNUAL_PCT"


def test_a_worldbank_figure_is_invisible_before_its_vintage(tmp_path: Path) -> None:
    """The Grade C property, structurally: 2009's CPI is not knowable in 2010 from this source."""
    release = parse_worldbank(
        _bytes("worldbank", "2026-10-06", "IND_FP.CPI.TOTL.ZG.json"),
        spec=WORLDBANK_SERIES[0],
        filename="wb.json",
    )
    write_release(release, data_root=tmp_path)
    assert read_pit(date(2010, 6, 30), data_root=tmp_path) == ()
    assert read_pit(date(2026, 7, 12), data_root=tmp_path) == ()
    assert len(read_pit(date(2026, 7, 13), data_root=tmp_path)) == 66


def test_worldbank_refuses_a_year_not_ended_by_its_vintage() -> None:
    doc = json.loads(_bytes("worldbank", "2026-10-06", "IND_FP.CPI.TOTL.ZG.json"))
    doc[1][0]["date"] = "2026"
    doc[1][0]["value"] = 3.1
    with pytest.raises(ParseError, match="cannot be measured before it ends"):
        parse_worldbank(json.dumps(doc).encode(), spec=WORLDBANK_SERIES[0], filename="wb.json")


@pytest.mark.parametrize(
    "body",
    [
        b'[{"message":[{"id":"120","key":"Invalid value"}]}]',
        b'[{"page":1,"pages":2,"per_page":50,"lastupdated":"2026-07-13"},[]]',
        b"<html>blocked</html>",
    ],
)
def test_worldbank_refuses_an_error_a_partial_page_and_markup(body: bytes) -> None:
    with pytest.raises(ParseError):
        parse_worldbank(body, spec=WORLDBANK_SERIES[0], filename="wb.json")


# ── FBIL ────────────────────────────────────────────────────────────────────────────────────


def test_fbil_groups_rates_by_publication_day_from_the_epoch() -> None:
    releases = parse_fbil_reference_rates(
        _bytes("fbil", "2026-10-06", "refrates_20180709_20180713.json"), filename="f.json"
    )
    assert [r.release_date for r in releases] == [date(2018, 7, d) for d in (10, 11, 12, 13)]
    first = {fact.series_id: fact.value for fact in releases[0].facts}
    assert first["IN.FBIL.INR_PER_USD.REFERENCE"] == Decimal("68.794200")
    assert first["IN.FBIL.INR_PER_100_JPY.REFERENCE"] == Decimal("61.930000")
    assert all(f.period_end == f.release_date for r in releases for f in r.facts)


def test_fbil_quote_quantity_lives_in_the_series_id() -> None:
    releases = parse_fbil_reference_rates(
        _bytes("fbil", "2026-10-06", "refrates_latest.json"), filename="f.json"
    )
    ids = {fact.series_id for release in releases for fact in release.facts}
    assert "IN.FBIL.INR_PER_10000_IDR.REFERENCE" in ids
    assert "IN.FBIL.INR_PER_RUB.REFERENCE" in ids
    assert fbil_series_id("INR / 1 USD") == "IN.FBIL.INR_PER_USD.REFERENCE"


def test_fbil_refuses_a_rate_published_on_another_day_and_a_same_day_conflict() -> None:
    row = {
        "processRunDate": "2026-09-29 00:00:00",
        "subProdName": "INR / 1 USD",
        "displayTime": "2026-09-30 13:00:00",
        "rate": 96.0321,
        "comments": "",
    }
    with pytest.raises(ParseError, match="not the processRunDate"):
        parse_fbil_reference_rates(json.dumps([row]).encode(), filename="f.json")
    same = dict(row, displayTime="2026-09-29 13:00:00")
    other = dict(same, rate=96.5)
    with pytest.raises(ParseError, match="published twice"):
        parse_fbil_reference_rates(json.dumps([same, other]).encode(), filename="f.json")
    assert parse_fbil_reference_rates(b"[]", filename="f.json") == ()


# ── RBI current rates ───────────────────────────────────────────────────────────────────────


def test_rbi_panel_reads_all_seven_rates_dated_by_the_capture() -> None:
    release = parse_rbi_current_rates(
        _bytes("rbi_home", "2026-10-06", "Home.html"), captured=CAPTURED, filename="h.html"
    )
    rates = {fact.series_id: fact.value for fact in release.facts}
    assert rates["IN.RBI.POLICY_REPO_RATE.RATE"] == Decimal("5.25")
    assert rates["IN.RBI.CASH_RESERVE_RATIO.RATE"] == Decimal("3.00")
    assert rates["IN.RBI.STATUTORY_LIQUIDITY_RATIO.RATE"] == Decimal("18.00")
    assert len(rates) == 7
    assert {f.release_date for f in release.facts} == {CAPTURED}


def test_rbi_panel_missing_a_rate_fails_rather_than_storing_a_partial_panel() -> None:
    page = _bytes("rbi_home", "2026-10-06", "Home.html").replace(b"Bank Rate", b"Bank Ratio")
    with pytest.raises(ParseError, match="Bank Rate"):
        parse_rbi_current_rates(page, captured=CAPTURED, filename="h.html")


# ── WPI ─────────────────────────────────────────────────────────────────────────────────────


def test_wpi_download_page_names_the_current_file() -> None:
    url, month = parse_wpi_download_page(
        _bytes("wpi", "2026-10-06", "download_data_2223.html"), filename="p.html"
    )
    assert url == "https://eaindustry.nic.in/indx_download_2223/wpi_monthly_index_202609.xlsx"
    assert month == "202609"


def test_wpi_headline_rows_every_month_released_on_capture() -> None:
    release = parse_wpi_monthly_index(
        _bytes("wpi", "2026-10-06", "wpi_monthly_index_202609.xlsx"),
        captured=CAPTURED,
        filename="w.xlsx",
    )
    by_key = {(f.series_id, f.period_end): f.value for f in release.facts}
    all_id = "IN.OEA.WPI_ALL_COMMODITIES.INDEX_2022_23"
    assert by_key[(all_id, date(2023, 4, 30))] == Decimal("99")
    assert by_key[(all_id, date(2026, 8, 31))] == Decimal("110.8")
    assert len({f.series_id for f in release.facts}) == 5
    assert len(release.facts) == 5 * 41


def test_wpi_refuses_a_month_not_ended_on_the_capture_date() -> None:
    with pytest.raises(ParseError, match="had not ended"):
        parse_wpi_monthly_index(
            _bytes("wpi", "2026-10-06", "wpi_monthly_index_202609.xlsx"),
            captured=date(2026, 8, 20),
            filename="w.xlsx",
        )


# ── GST ─────────────────────────────────────────────────────────────────────────────────────


def test_gst_reads_the_current_month_column_on_every_layout() -> None:
    release = parse_gst_collections(
        _bytes("gst", "2026-10-06", "Gross_Net_Tax_collection.xlsx"),
        captured=CAPTURED,
        filename="g.xlsx",
    )
    by_key = {(f.series_id, f.period_start): f.value for f in release.facts}
    gross = "IN.GSTN.GST_GROSS_REVENUE.MONTHLY"
    assert by_key[(gross, date(2024, 4, 1))] == Decimal("210267.064585")
    # Jan-25's sheet carries an extra "Daily" column; the current month is MTD 31 Jan 2025, not
    # the prior-year column — reading the one left of it would return the January 2024 figure.
    assert by_key[(gross, date(2025, 1, 1))] == Decimal("195505.940793")
    assert by_key[("IN.GSTN.GST_NET_REVENUE.MONTHLY", date(2026, 8, 1))] == Decimal("168057.349027")
    assert len(release.facts) == 2 * 29


# ── India VIX ───────────────────────────────────────────────────────────────────────────────


def test_india_vix_sessions_land_as_the_close_all_series() -> None:
    releases = parse_india_vix_history(
        _bytes("india_vix", "2026-10-06", "india_vix_20260901_20260910.json"), filename="v.json"
    )
    last = releases[-1]
    assert last.release_date == date(2026, 9, 10)
    values = {fact.series_id: fact.value for fact in last.facts}
    assert values["IN.NSE.INDIA_VIX.CLOSE"] == Decimal("11.80")
    assert values["IN.NSE.INDIA_VIX.LOW"] == Decimal("11.5275")
    older = parse_india_vix_history(
        _bytes("india_vix", "2026-10-06", "india_vix_20200302_20200306.json"), filename="v.json"
    )
    assert older[0].release_date == date(2020, 3, 2)
    empty = _bytes("india_vix", "2026-10-06", "india_vix_20141001_20141010.json")
    assert parse_india_vix_history(empty, filename="v.json") == ()


def test_india_vix_refuses_another_index_and_markup() -> None:
    rows = json.loads(_bytes("india_vix", "2026-10-06", "india_vix_20260901_20260910.json"))
    rows[0]["INDEX_NAME"] = "Nifty 50"
    with pytest.raises(ParseError, match="not India VIX"):
        parse_india_vix_history(json.dumps(rows).encode(), filename="v.json")
    with pytest.raises(ParseError):
        parse_india_vix_history(b"<html>home</html>", filename="v.json")


# ── the store side: only new or revised facts are written ───────────────────────────────────


def _fact(value: str, release: date) -> MacroFact:
    return MacroFact(
        series_id="IN.GSTN.GST_GROSS_REVENUE.MONTHLY",
        period_start=date(2026, 8, 1),
        period_end=date(2026, 8, 31),
        release_date=release,
        frequency=Frequency.MONTHLY,
        unit=Unit.INR_CRORE,
        value=Decimal(value),
        source="gstn_tax_collection",
    )


def test_an_unchanged_restatement_writes_nothing_and_a_revision_is_a_second_record(
    tmp_path: Path,
) -> None:
    first = MacroRelease(
        release_date=date(2026, 9, 6),
        source="gstn_tax_collection",
        facts=(_fact("100", date(2026, 9, 6)),),
    )
    write_release(first, data_root=tmp_path)

    same = MacroRelease(
        release_date=date(2026, 9, 13),
        source="gstn_tax_collection",
        facts=(_fact("100", date(2026, 9, 13)),),
    )
    assert new_or_revised(same, data_root=tmp_path) is None

    revised = MacroRelease(
        release_date=date(2026, 9, 20),
        source="gstn_tax_collection",
        facts=(_fact("101", date(2026, 9, 20)),),
    )
    kept = new_or_revised(revised, data_root=tmp_path)
    assert kept is not None and len(kept.facts) == 1
    write_release(kept, data_root=tmp_path)
    assert [f.value for f in read_pit(date(2026, 9, 30), data_root=tmp_path)] == [
        Decimal("100"),
        Decimal("101"),
    ]
    # The market saw 100 until the revision was captured, and 101 after.
    assert read_latest(date(2026, 9, 19), data_root=tmp_path)[0].value == Decimal("100")
    assert read_latest(date(2026, 9, 20), data_root=tmp_path)[0].value == Decimal("101")


def test_store_value_strips_binary_residue_without_a_float() -> None:
    assert store_value("172738.89553549999") == Decimal("172738.895535")
    assert store_value("102.30000000000001") == Decimal("102.300000")
    with pytest.raises(ArithmeticError):
        store_value("NaN")


# ── capture steps over a recorded transport ─────────────────────────────────────────────────


class _SpyAlerter:
    def send(self, severity: Severity, title: str, body: str, dedup_key: str) -> AlertOutcome:
        return AlertOutcome.SENT


@pytest.fixture
def settings(load_settings: SettingsLoader) -> Settings:
    return load_settings(None)


def _wire(
    script: dict[str, RecordedResponse], *, settings: Settings, data_root: Path
) -> tuple[Fetcher, L0Store, RecordedTransport, FrozenClock]:
    clock = FrozenClock(datetime(2026, 10, 6, 10, 0, tzinfo=IST))
    transport = RecordedTransport(script)
    l0 = L0Store(clock=clock, data_root=data_root)
    fetcher = Fetcher(
        transport=transport,
        l0=l0,
        alerter=_SpyAlerter(),
        clock=clock,
        register=load_register(),
        settings=settings,
        sleep=lambda seconds: clock.advance(timedelta(seconds=seconds)),
    )
    return fetcher, l0, transport, clock


def test_a_second_capture_the_same_day_makes_no_request(settings: Settings, tmp_path: Path) -> None:
    fetcher, l0, transport, _ = _wire(
        {RBI_HOME_URL: RecordedResponse(body=_bytes("rbi_home", "2026-10-06", "Home.html"))},
        settings=settings,
        data_root=tmp_path,
    )
    first = capture_rbi_rates(fetcher, l0, on=CAPTURED, data_root=tmp_path)
    again = capture_rbi_rates(fetcher, l0, on=CAPTURED, data_root=tmp_path)
    assert (first.requests, again.requests) == (1, 0)
    assert len(transport.requests) == 1
    assert len(read_pit(CAPTURED, data_root=tmp_path)) == 7


def test_wpi_and_gst_captures_write_once_then_nothing(settings: Settings, tmp_path: Path) -> None:
    xlsx_url = "https://eaindustry.nic.in/indx_download_2223/wpi_monthly_index_202609.xlsx"
    fetcher, l0, transport, _ = _wire(
        {
            WPI_DOWNLOAD_PAGE_URL: RecordedResponse(
                body=_bytes("wpi", "2026-10-06", "download_data_2223.html")
            ),
            xlsx_url: RecordedResponse(
                body=_bytes("wpi", "2026-10-06", "wpi_monthly_index_202609.xlsx")
            ),
            GST_COLLECTION_URL: RecordedResponse(
                body=_bytes("gst", "2026-10-06", "Gross_Net_Tax_collection.xlsx")
            ),
        },
        settings=settings,
        data_root=tmp_path,
    )
    wpi = capture_wpi(fetcher, l0, on=CAPTURED, data_root=tmp_path)
    gst = capture_gst(fetcher, l0, on=CAPTURED, data_root=tmp_path)
    assert (wpi.requests, wpi.facts_written) == (2, 205)
    assert (gst.requests, gst.facts_written) == (1, 58)
    # A week later the same tables come back unchanged: new requests, nothing new to write.
    later = CAPTURED + timedelta(days=7)
    assert capture_wpi(fetcher, l0, on=later, data_root=tmp_path).facts_written == 0
    assert capture_gst(fetcher, l0, on=later, data_root=tmp_path).facts_written == 0
    assert len(transport.requests) == 6


def test_fbil_backfill_windows_and_resumes(settings: Settings, tmp_path: Path) -> None:
    epoch_window = fbil_url(date(2018, 7, 10), date(2018, 7, 13))
    fetcher, l0, transport, _ = _wire(
        {
            epoch_window: RecordedResponse(
                body=_bytes("fbil", "2026-10-06", "refrates_20180709_20180713.json")
            )
        },
        settings=settings,
        data_root=tmp_path,
    )
    outcomes = backfill_fbil(
        fetcher, l0, start=date(2018, 7, 1), end=date(2018, 7, 13), data_root=tmp_path
    )
    assert [(o.requests, o.releases, o.facts_written) for o in outcomes] == [(1, 4, 16)]
    again = capture_fbil(
        fetcher, l0, start=date(2018, 7, 10), end=date(2018, 7, 13), data_root=tmp_path
    )
    assert again.requests == 0 and len(transport.requests) == 1


def test_india_vix_capture_posts_the_cinfo_body(settings: Settings, tmp_path: Path) -> None:
    fetcher, l0, transport, _ = _wire(
        {
            INDIA_VIX_URL: RecordedResponse(
                body=_bytes("india_vix", "2026-10-06", "india_vix_20260901_20260910.json"),
                headers={"content-type": "text/html; charset=utf-8"},
            )
        },
        settings=settings,
        data_root=tmp_path,
    )
    outcome = capture_india_vix(
        fetcher, l0, start=date(2026, 9, 1), end=date(2026, 9, 10), data_root=tmp_path
    )
    assert outcome.releases == 8
    sent = transport.requests[0]
    assert sent.method == "POST" and sent.payload is not None
    assert b"'name':'INDIA VIX'" in sent.payload and b"01-Sep-2026" in sent.payload
