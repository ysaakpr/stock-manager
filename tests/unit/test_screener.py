"""Screener ingestion (M7.1) — the crawler's URL policy, the parser, and the quarantined store.

The task's three acceptance criteria drive this file, one section each:

* **The crawler provably never requests a robots-disallowed path.** Screener's robots.txt (recorded
  by C.1 in the Source Register) disallows `/user/*`, search, sorting, listing pagination and the
  per-quarter source pages; AGENTIC_CONTEXT §8 makes those binding. The crawler resolves the same
  `CrawlPolicy` the fetcher uses, so every one of those shapes is refused before a request exists,
  and a company slug cannot smuggle a query string past it.
* **Restated data lands in its own store root, never in the PIT store.** The `RestatedStore` writes
  under `RESTATED/…`, a sibling of `L0`/`L1`/`L2` that no `paths.py` PIT path can reach — asserted
  here by walking both trees after a real write.
* **Every datum carries its source tag and L0 lineage.** Each stored row is tagged
  `screener_restated` and stamped with the sha256, source and date of the exact L0 payload it was
  derived from; the store refuses a row missing either.

Offline and deterministic: every byte read here is either the frozen fixture or bytes this test
builds. Nothing touches the network.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest
from pydantic import ValidationError

from dataplatform.clock import IST, FrozenClock
from dataplatform.identity.master import Exchange, IdentityMaster, SymbolWindow
from dataplatform.ingest.models import ParseError
from dataplatform.ingest.policy import RobotsDisallowedError
from dataplatform.ingest.screener import (
    SCREENER_HOST,
    SCREENER_SOURCE_ID,
    SCREENER_SOURCE_TAG,
    ScreenerCrawler,
    ScreenerDatum,
    ScreenerError,
    ScreenerPolicyError,
    company_symbol_of,
    parse,
    parse_html,
    parse_l0,
)
from dataplatform.store import L0Ref, L0Store
from dataplatform.store.paths import Layer, layer_root
from dataplatform.store.restated import (
    RESTATED_ROOT_NAME,
    SCREENER_FUNDAMENTALS_DATASET,
    RestatedFundamental,
    RestatedStore,
    build,
    restated_root,
)

FIXTURE: Final = (
    Path(__file__).resolve().parents[1] / "fixtures" / "screener" / "2026-08" / "RELIANCE.html"
)
RELIANCE_ISIN: Final = "INE002A01018"
FETCHED_AT: Final = datetime(2026, 8, 7, 19, 30, tzinfo=IST)
LOGICAL_DATE: Final = date(2026, 8, 7)


def _payload() -> bytes:
    return FIXTURE.read_bytes()


def _master() -> IdentityMaster:
    """RELIANCE → INE002A01018, open window covering the fetch date."""
    return IdentityMaster(
        (
            SymbolWindow(
                exchange=Exchange.NSE,
                symbol="RELIANCE",
                valid_from=date(2000, 1, 1),
                valid_to=None,
                isin=RELIANCE_ISIN,
            ),
        )
    )


# ── acceptance #1: the URL policy ─────────────────────────────────────────────────────────────


def _crawler() -> ScreenerCrawler:
    return ScreenerCrawler.from_register()


def test_company_url_is_the_one_permitted_shape() -> None:
    crawler = _crawler()
    assert crawler.host == SCREENER_HOST
    assert crawler.company_url("RELIANCE") == "https://www.screener.in/company/RELIANCE/"
    assert crawler.company_url("RELIANCE", consolidated=True) == (
        "https://www.screener.in/company/RELIANCE/consolidated/"
    )
    assert crawler.allows("https://www.screener.in/company/RELIANCE/")


#: Every robots-disallowed shape the register records for Screener. If the crawler ever built or
#: waved one of these through, the whole monitoring-only, polite-crawl posture (AGENTIC_CONTEXT §8)
#: would be a lie — so each must be refused.
DISALLOWED_URLS: Final = (
    "https://www.screener.in/user/watchlist/",
    "https://www.screener.in/company/RELIANCE/?q=reliance",
    "https://www.screener.in/company/RELIANCE/?page=2",
    "https://www.screener.in/company/RELIANCE/?sort=mcap",
    "https://www.screener.in/company/RELIANCE/?limit=100",
    "https://www.screener.in/company/source/quarter/1234/",
)


@pytest.mark.parametrize("url", DISALLOWED_URLS)
def test_the_crawler_refuses_every_robots_disallowed_path(url: str) -> None:
    crawler = _crawler()
    assert not crawler.allows(url)
    with pytest.raises(ScreenerPolicyError):
        crawler.guard(url)


def test_the_underlying_policy_is_the_shared_robots_engine() -> None:
    """The refusal is the register's robots rule, not a private list — kept in sync with C.1."""
    crawler = _crawler()
    with pytest.raises(ScreenerPolicyError) as exc:
        crawler.guard("https://www.screener.in/company/RELIANCE/?page=9")
    # The cause is the shared engine's RobotsDisallowedError, naming the rule that matched.
    assert isinstance(exc.value.__cause__, RobotsDisallowedError)


def test_a_slug_that_could_carry_a_query_string_is_rejected() -> None:
    crawler = _crawler()
    for bad in ("RELIANCE?page=2", "RELIANCE/../user", "a b", "x?q=1", ""):
        with pytest.raises(ScreenerPolicyError):
            crawler.company_url(bad)


def test_a_request_to_another_host_is_refused() -> None:
    crawler = _crawler()
    with pytest.raises(ScreenerPolicyError):
        crawler.guard("https://www.nseindia.com/company/RELIANCE/")


def test_real_tickers_with_punctuation_are_permitted() -> None:
    crawler = _crawler()
    assert crawler.company_url("M&M") == "https://www.screener.in/company/M&M/"
    assert crawler.company_url("BAJAJ-AUTO") == "https://www.screener.in/company/BAJAJ-AUTO/"
    assert crawler.company_url("3MINDIA").endswith("/company/3MINDIA/")


def test_company_symbol_of_round_trips_and_rejects_non_company_urls() -> None:
    crawler = _crawler()
    assert company_symbol_of(crawler.company_url("RELIANCE")) == "RELIANCE"
    with pytest.raises(ScreenerError):
        company_symbol_of("https://www.screener.in/screens/12/top/")


# ── the parser ────────────────────────────────────────────────────────────────────────────────


def test_parse_extracts_tagged_datums_from_the_company_page() -> None:
    datums = parse(_payload(), filename=FIXTURE.name)
    assert datums, "the fixture page carries data tables"
    assert all(d.source == SCREENER_SOURCE_TAG for d in datums)
    assert all(d.symbol == "RELIANCE" for d in datums)
    assert all(isinstance(d.value, Decimal) for d in datums)


def test_parse_lines_each_value_up_with_its_period_and_metric() -> None:
    datums = parse(_payload(), filename=FIXTURE.name)
    by_key = {(d.statement, d.metric, d.period): d.value for d in datums}
    assert by_key[("profit_loss", "Sales", "Mar 2024")] == Decimal("900000")
    assert by_key[("profit_loss", "Net Profit", "Mar 2022")] == Decimal("60000")
    assert by_key[("profit_loss", "EPS in Rs", "TTM")] == Decimal("111.60")
    assert by_key[("balance_sheet", "Borrowings", "Mar 2024")] == Decimal("210000")


def test_top_ratios_are_parsed_with_units_stripped() -> None:
    datums = parse(_payload(), filename=FIXTURE.name)
    ratios = {d.metric: d.value for d in datums if d.statement == "ratios"}
    assert ratios["Market Cap"] == Decimal("1820450")
    assert ratios["Stock P/E"] == Decimal("24.5")
    assert ratios["ROCE"] == Decimal("9.80")


def test_negative_and_parenthesised_values_are_signed_not_dropped() -> None:
    datums = parse(_payload(), filename=FIXTURE.name)
    by_key = {(d.statement, d.metric, d.period): d.value for d in datums}
    # A leading-minus cell and an accounting-parenthesis cell both read as negative.
    assert by_key[("cash_flow", "Net Cash Flow", "Mar 2022")] == Decimal("-5000")
    assert by_key[("cash_flow", "Cash from Investing Activity", "Mar 2023")] == Decimal("-90000")


def test_datum_value_rejects_a_float() -> None:
    """The money rule holds at the model boundary: a float is a construction error."""
    with pytest.raises(ValidationError):
        ScreenerDatum(
            symbol="RELIANCE",
            statement="profit_loss",
            metric="Sales",
            period="Mar 2024",
            value=900000.0,  # type: ignore[arg-type]
        )


def test_a_page_with_no_identity_is_a_parse_error() -> None:
    html = (
        "<html><body><table class='data-table'><tr><td>x</td><td>1</td></tr></table></body></html>"
    )
    with pytest.raises(ParseError):
        parse_html(html, filename="noidentity.html")


def test_a_page_with_no_data_table_is_a_parse_error() -> None:
    html = '<html><head><link rel="canonical" href="/company/RELIANCE/"></head><body></body></html>'
    with pytest.raises(ParseError):
        parse_html(html, filename="empty.html")


def test_a_non_utf8_body_is_a_parse_error() -> None:
    with pytest.raises(ParseError):
        parse(b"\xff\xfe not utf-8", filename="bad.html")


def test_parse_l0_reads_the_payload_back_re_checksummed(tmp_path: Path) -> None:
    store = L0Store(clock=FrozenClock(FETCHED_AT), data_root=tmp_path)
    ref = store.put(
        SCREENER_SOURCE_ID, LOGICAL_DATE, "RELIANCE.html", _payload(), content_type="text/html"
    )
    datums = parse_l0(store, ref)
    assert datums == parse(_payload(), filename="RELIANCE.html", symbol="RELIANCE")


# ── acceptance #3: source tag + L0 lineage ──────────────────────────────────────────────────────


def _ref_and_datums(tmp_path: Path) -> tuple[L0Ref, tuple[ScreenerDatum, ...]]:
    store = L0Store(clock=FrozenClock(FETCHED_AT), data_root=tmp_path)
    ref = store.put(
        SCREENER_SOURCE_ID, LOGICAL_DATE, "RELIANCE.html", _payload(), content_type="text/html"
    )
    datums = parse_l0(store, ref)
    return ref, datums


def test_build_resolves_to_isin_and_stamps_source_tag_and_lineage(tmp_path: Path) -> None:
    ref, datums = _ref_and_datums(tmp_path)
    result = build(datums, ref=ref, master=_master())

    assert result.unresolved == ()
    assert result.resolved, "RELIANCE resolves, so every datum lands"
    for row in result.resolved:
        assert row.isin == RELIANCE_ISIN
        assert row.source == SCREENER_SOURCE_TAG
        # Lineage is copied straight off the L0Ref — the exact immutable bytes derived from.
        assert row.l0_sha256 == ref.sha256
        assert row.l0_source == SCREENER_SOURCE_ID
        assert row.l0_filename == "RELIANCE.html"
        assert row.l0_logical_date == LOGICAL_DATE
        assert row.fetched_at == FETCHED_AT


def test_an_unknown_symbol_is_quarantined_not_dropped(tmp_path: Path) -> None:
    ref, datums = _ref_and_datums(tmp_path)
    empty_master = IdentityMaster(())  # knows nothing
    result = build(datums, ref=ref, master=empty_master)
    assert result.resolved == ()
    assert len(result.unresolved) == len(datums)  # counted, not lost


def test_store_write_read_round_trips_with_lineage(tmp_path: Path) -> None:
    ref, datums = _ref_and_datums(tmp_path)
    result = build(datums, ref=ref, master=_master())
    store = RestatedStore(data_root=tmp_path)

    store.write(result.resolved)
    rows = store.read_isin(RELIANCE_ISIN)

    assert len(rows) == len(result.resolved)
    assert all(r["source"] == SCREENER_SOURCE_TAG for r in rows)
    assert all(r["l0_sha256"] == ref.sha256 for r in rows)


def test_store_refuses_a_row_missing_its_source_tag(tmp_path: Path) -> None:
    ref, datums = _ref_and_datums(tmp_path)
    good = build(datums, ref=ref, master=_master()).resolved[0]
    # Forge a row whose tag was lost. The model pins `source` to the literal, so bypass validation
    # with model_copy(update=...) — exactly the smuggling the store's boundary check catches.
    mistagged = good.model_copy(update={"source": "pit"})
    store = RestatedStore(data_root=tmp_path)
    with pytest.raises(ValueError):
        store.write([mistagged])


# ── acceptance #2: physical separation from the PIT store ───────────────────────────────────────


def test_restated_root_is_a_sibling_of_the_pit_layers_never_a_child(tmp_path: Path) -> None:
    r_root = restated_root(data_root=tmp_path)
    assert r_root.name == RESTATED_ROOT_NAME
    for layer in (Layer.L0, Layer.L1, Layer.L2):
        pit_root = layer_root(layer, data_root=tmp_path)
        assert r_root != pit_root
        assert pit_root not in r_root.parents
        assert r_root not in pit_root.parents


def test_written_restated_files_live_only_under_the_restated_root(tmp_path: Path) -> None:
    ref, datums = _ref_and_datums(tmp_path)
    result = build(datums, ref=ref, master=_master())
    store = RestatedStore(data_root=tmp_path)
    written = store.write(result.resolved)

    assert written, "a write produced at least one partition"
    r_root = restated_root(data_root=tmp_path)
    for path in written:
        assert r_root in path.parents

    # And nothing landed in any PIT layer directory.
    for layer in (Layer.L0, Layer.L1, Layer.L2):
        pit_root = layer_root(layer, data_root=tmp_path)
        strays = (
            [p for p in pit_root.rglob("*") if p.suffix == ".parquet"] if pit_root.exists() else []
        )
        assert strays == [], f"restated data must not appear under {layer.value}"


def test_restated_partition_is_keyed_on_isin_not_symbol(tmp_path: Path) -> None:
    ref, datums = _ref_and_datums(tmp_path)
    result = build(datums, ref=ref, master=_master())
    store = RestatedStore(data_root=tmp_path)
    written = store.write(result.resolved)

    dataset_dir = restated_root(data_root=tmp_path) / SCREENER_FUNDAMENTALS_DATASET
    assert written[0].parent == dataset_dir / f"isin={RELIANCE_ISIN}"


def test_a_restated_fundamental_requires_tz_aware_fetched_at() -> None:
    with pytest.raises(ValidationError):
        RestatedFundamental(
            isin=RELIANCE_ISIN,
            symbol="RELIANCE",
            statement="profit_loss",
            metric="Sales",
            period="Mar 2024",
            value=Decimal("900000"),
            source=SCREENER_SOURCE_TAG,
            l0_source=SCREENER_SOURCE_ID,
            l0_filename="RELIANCE.html",
            l0_logical_date=LOGICAL_DATE,
            l0_sha256="0" * 64,
            fetched_at=datetime(2026, 8, 7, 19, 30),  # naive
        )
