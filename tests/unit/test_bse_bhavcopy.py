"""M3.1 — the BSE bhavcopy parser, the scrip master, and their landing in L1 and identity.

BSE is the platform's second price source (§4.1 row 4) and a second listing surface for the identity
master (§4.1 row 6). This task's three acceptance criteria each become tests here, all offline and
deterministic against the frozen fixtures (AGENTIC_CONTEXT B8 — no test touches the network):

  1. **The BSE source-register rows are VERIFIED with real evidence** — `test_register_*`. The rows
     carry a recorded successful fetch (status, bytes, content-type, checksum) and a now-frozen
     fixture, and the register as a whole still passes its C.1 acceptance checks.

  2. **BSE rows land in L1 under the same schema as NSE, ISIN-keyed** — `test_l1_*`. The UDiFF BSE
     bhavcopy parses to the identical `PriceRow` NSE emits, and `rebuild_prices_raw_from_l0` with
     `exchange=BSE` writes a `prices_raw` partition with the exact NSE schema, every row carrying
     its native ISIN, byte-identical on re-run.

  3. **The scrip master merges into the identity master without clobbering NSE listings** —
     `test_scrip_*` and `test_merge_*`. A dual-listed ISIN gains a BSE `exchange_listing` alongside
     its NSE one; a BSE-only ISIN gets its own security; a scrip with no ISIN is skipped, counted.

Plus the parser's own strictness (era dispatch, header/segment guards, the empty-`LastPric`
normalisation) and the legacy era's scrip-code→ISIN resolution, which is what lets a BSE file
with no ISIN column ever reach an ISIN-keyed store (invariant #2).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final

import pyarrow.parquet as pq
import pytest

from dataplatform.clock import FrozenClock
from dataplatform.identity.master import (
    Exchange,
    IdentityMaster,
    Listing,
    ListingStatus,
    Security,
    SymbolWindow,
    detect_conflicts,
)
from dataplatform.ingest import source_register
from dataplatform.ingest.backfill import BSE_BHAVCOPY, SOURCE_SETS
from dataplatform.ingest.bse import bhavcopy, scrip_master
from dataplatform.ingest.models import ParseError, PriceRow
from dataplatform.store.l0 import L0Store
from dataplatform.store.l1 import (
    PRICES_RAW_DATASET,
    read_prices_raw,
    rebuild_prices_raw_from_l0,
)
from dataplatform.store.paths import l1_partition_path
from dataplatform.store.schemas import PRICES_RAW_SCHEMA, column_looks_adjusted

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
FIXTURES: Final = REPO_ROOT / "tests" / "fixtures"

UDIFF_DIR: Final = FIXTURES / "bse_bhavcopy" / "udiff"
LEGACY_DIR: Final = FIXTURES / "bse_bhavcopy" / "legacy"
SCRIP_DIR: Final = FIXTURES / "bse_scrip_master" / "2026-08-08"

UDIFF_FILE: Final = "BhavCopy_BSE_CM_0_0_0_20260807_F_0000.CSV"
LEGACY_FILE: Final = "EQ020124_CSV.ZIP"
SCRIP_FILE: Final = "ListofScripData.json"

UDIFF_SESSION: Final = date(2026, 8, 7)
LEGACY_SESSION: Final = date(2024, 1, 2)
SNAPSHOT: Final = date(2026, 8, 8)

#: ISINs the fixtures share with the NSE identity fixtures, so a dual-listing is genuinely dual.
RELIANCE: Final = "INE002A01018"
TCS: Final = "INE467B01029"
BSE_ONLY: Final = "INE0J1Y01017"


# ── fixtures ─────────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def udiff_bytes() -> bytes:
    return (UDIFF_DIR / UDIFF_FILE).read_bytes()


@pytest.fixture
def legacy_bytes() -> bytes:
    return (LEGACY_DIR / LEGACY_FILE).read_bytes()


@pytest.fixture
def scrip_json() -> str:
    return (SCRIP_DIR / SCRIP_FILE).read_text(encoding="utf-8")


@pytest.fixture
def udiff_rows(udiff_bytes: bytes) -> tuple[PriceRow, ...]:
    return bhavcopy.parse(udiff_bytes, filename=UDIFF_FILE, trade_date=UDIFF_SESSION)


# ── acceptance 1: the register is VERIFIED with real evidence ──────────────────────────────────


@pytest.mark.parametrize(
    "source_id",
    ["bse_bhavcopy_udiff", "bse_bhavcopy_legacy", "bse_scrip_master"],
)
def test_register_row_is_verified_with_real_evidence(source_id: str) -> None:
    reg = source_register.load()
    source = next(s for s in reg.sources if s.id == source_id)
    assert source.status is source_register.Status.VERIFIED
    assert source.fetch_succeeded, "VERIFIED must rest on a recorded successful fetch"
    assert source.sample_sha256, "the fetch evidence must include a payload checksum"
    assert source.content_type
    assert source.sample_bytes and source.sample_bytes > 0
    assert source.failure_note is None


@pytest.mark.parametrize(
    "source_id",
    ["bse_bhavcopy_udiff", "bse_bhavcopy_legacy", "bse_scrip_master"],
)
def test_register_fixture_is_now_frozen(source_id: str) -> None:
    reg = source_register.load()
    source = next(s for s in reg.sources if s.id == source_id)
    assert source.fixture.frozen, "M3.1 freezes a fixture for each BSE format era"
    assert source.fixture.path is not None
    assert (REPO_ROOT / source.fixture.path).is_dir()


def test_register_still_holds_after_the_edits() -> None:
    """Flipping the fixtures to frozen must not break any C.1 acceptance criterion."""
    assert source_register.problems(source_register.load()) == []


# ── the parser: era dispatch ───────────────────────────────────────────────────────────────────


def test_era_split_is_the_udiff_cutover() -> None:
    assert bhavcopy.era_of(date(2024, 7, 7)) == "legacy"
    assert bhavcopy.era_of(bhavcopy.CUTOVER) == "udiff"
    assert bhavcopy.era_of(date(2024, 7, 8)) == "udiff"


def test_parse_refuses_a_legacy_date(udiff_bytes: bytes) -> None:
    with pytest.raises(ParseError, match="before the BSE UDiFF cutover"):
        bhavcopy.parse(udiff_bytes, filename=UDIFF_FILE, trade_date=date(2020, 1, 1))


def test_parse_legacy_refuses_a_udiff_date(legacy_bytes: bytes) -> None:
    with pytest.raises(ParseError, match="on or after the BSE UDiFF cutover"):
        bhavcopy.parse_legacy(legacy_bytes, filename=LEGACY_FILE, trade_date=UDIFF_SESSION)


def test_parse_rejects_a_contents_date_mismatch(udiff_bytes: bytes) -> None:
    with pytest.raises(ParseError, match="does not match the date it was filed under"):
        bhavcopy.parse(udiff_bytes, filename=UDIFF_FILE, trade_date=date(2026, 8, 6))


# ── acceptance 2: BSE UDiFF rows are the shared PriceRow, ISIN-native ───────────────────────────


def test_udiff_parses_to_pricerow_with_native_isin(udiff_rows: tuple[PriceRow, ...]) -> None:
    assert len(udiff_rows) == 5
    assert all(isinstance(row, PriceRow) for row in udiff_rows)
    reliance = next(row for row in udiff_rows if row.isin == RELIANCE)
    assert reliance.symbol == "RELIANCE"
    assert reliance.series == "A"  # BSE group, kept verbatim as the row's series
    assert reliance.trade_date == UDIFF_SESSION
    assert reliance.close == Decimal("3170.25")
    assert isinstance(reliance.close, Decimal)


def test_udiff_row_has_no_era_specific_field(udiff_rows: tuple[PriceRow, ...]) -> None:
    """A caller must not be able to tell a BSE UDiFF row from an NSE or legacy one."""
    assert set(udiff_rows[0].model_dump()) == {
        "isin",
        "symbol",
        "series",
        "trade_date",
        "open",
        "high",
        "low",
        "close",
        "last",
        "prev_close",
        "total_traded_qty",
        "total_traded_value",
        "total_trades",
    }


def test_empty_last_price_normalises_to_zero(udiff_rows: tuple[PriceRow, ...]) -> None:
    ncd = next(row for row in udiff_rows if row.symbol == "RELNCD26")
    assert ncd.last == Decimal(0)


def test_udiff_refuses_a_non_cash_row(udiff_bytes: bytes) -> None:
    text = udiff_bytes.decode("utf-8").replace(",CM,BSE,STK,", ",FO,BSE,FUTSTK,", 1)
    with pytest.raises(ParseError, match="not the cash equity"):
        bhavcopy.parse_udiff_text(text, filename=UDIFF_FILE)


def test_udiff_refuses_an_unknown_header() -> None:
    with pytest.raises(ParseError, match="unexpected header"):
        bhavcopy.parse_udiff_text("A,B,C\n1,2,3\n", filename="x.csv")


def test_udiff_refuses_a_multi_session_file(udiff_bytes: bytes) -> None:
    text = udiff_bytes.decode("utf-8").replace(
        "2026-08-07,2026-08-07,CM,BSE,STK,532540", "2026-08-06,2026-08-06,CM,BSE,STK,532540", 1
    )
    with pytest.raises(ParseError, match="span more than one session"):
        bhavcopy.parse_udiff_text(text, filename=UDIFF_FILE)


def test_l1_partition_is_written_with_bse_exchange_and_nse_schema(
    udiff_bytes: bytes, tmp_path: Path
) -> None:
    l0 = L0Store(clock=FrozenClock(UDIFF_SESSION), data_root=tmp_path)
    l0.put("bse_bhavcopy", UDIFF_SESSION, UDIFF_FILE, udiff_bytes)
    ref = l0.ref_for("bse_bhavcopy", UDIFF_SESSION, UDIFF_FILE)

    report = rebuild_prices_raw_from_l0(l0, ref, exchange=Exchange.BSE, data_root=tmp_path)
    assert report.exchange is Exchange.BSE
    assert report.rows_written == 5

    path = l1_partition_path(PRICES_RAW_DATASET, UDIFF_SESSION, data_root=tmp_path)
    # The partition schema is exactly the one NSE writes — same dataset, same columns.
    assert pq.read_schema(path).equals(PRICES_RAW_SCHEMA)

    records = read_prices_raw(UDIFF_SESSION, data_root=tmp_path)
    assert {r["exchange"] for r in records} == {"BSE"}
    assert all(r["isin"] for r in records)
    assert not any(column_looks_adjusted(name) for name in PRICES_RAW_SCHEMA.names)
    by_isin = {r["isin"]: r for r in records}
    assert by_isin[RELIANCE]["close"] == Decimal("3170.25")


def test_l1_rebuild_from_l0_is_byte_identical(udiff_bytes: bytes, tmp_path: Path) -> None:
    l0 = L0Store(clock=FrozenClock(UDIFF_SESSION), data_root=tmp_path)
    l0.put("bse_bhavcopy", UDIFF_SESSION, UDIFF_FILE, udiff_bytes)
    ref = l0.ref_for("bse_bhavcopy", UDIFF_SESSION, UDIFF_FILE)

    first = rebuild_prices_raw_from_l0(l0, ref, exchange=Exchange.BSE, data_root=tmp_path).path
    first_bytes = first.read_bytes()
    second = rebuild_prices_raw_from_l0(l0, ref, exchange=Exchange.BSE, data_root=tmp_path).path
    assert second.read_bytes() == first_bytes


# ── the legacy era: scrip-code → ISIN resolution ───────────────────────────────────────────────


def test_legacy_parses_to_scrip_keyed_quotes(legacy_bytes: bytes) -> None:
    quotes = bhavcopy.parse_legacy(legacy_bytes, filename=LEGACY_FILE, trade_date=LEGACY_SESSION)
    assert len(quotes) == 3
    reliance = next(q for q in quotes if q.scrip_code == "500325")
    assert reliance.scrip_name == "RELIANCE"
    assert reliance.trade_date == LEGACY_SESSION  # supplied — the file has no date column
    assert reliance.close == Decimal("2595.40")


def test_legacy_resolves_known_scrips_and_quarantines_the_unknown(
    legacy_bytes: bytes, scrip_json: str
) -> None:
    quotes = bhavcopy.parse_legacy(legacy_bytes, filename=LEGACY_FILE, trade_date=LEGACY_SESSION)
    scrips, _ = scrip_master.parse_scrip_master(scrip_json)
    resolution = bhavcopy.resolve_legacy(quotes, scrip_master.scrip_to_isin(scrips))

    assert len(resolution.resolved) + len(resolution.unresolved) == len(quotes)  # nothing dropped
    assert {r.isin for r in resolution.resolved} == {RELIANCE, TCS}
    assert [q.scrip_code for q in resolution.unresolved] == ["999999"]
    reliance = next(r for r in resolution.resolved if r.isin == RELIANCE)
    assert isinstance(reliance, PriceRow)
    assert reliance.series == "A"  # SC_GROUP becomes the series


# ── acceptance 3: the scrip master, and merging without clobbering NSE ──────────────────────────


def test_scrip_master_parses_and_skips_the_blank_isin(scrip_json: str) -> None:
    scrips, skipped = scrip_master.parse_scrip_master(scrip_json)
    assert skipped == 1  # the NOISIN suspended scrip
    assert {s.isin for s in scrips} == {RELIANCE, TCS, "INE009A01021", BSE_ONLY}
    reliance = next(s for s in scrips if s.isin == RELIANCE)
    assert reliance.scrip_code == "500325"
    assert reliance.status is ListingStatus.ACTIVE


def test_scrip_to_isin_map(scrip_json: str) -> None:
    scrips, _ = scrip_master.parse_scrip_master(scrip_json)
    mapping = scrip_master.scrip_to_isin(scrips)
    assert mapping["500325"] == RELIANCE
    assert "590999" not in mapping  # blank ISIN never enters the map


def test_derive_master_tags_every_row_bse(scrip_json: str) -> None:
    scrips, _ = scrip_master.parse_scrip_master(scrip_json)
    derived = scrip_master.derive_master(scrips, snapshot_date=SNAPSHOT)

    assert {s.primary_exchange for s in derived.securities} == {Exchange.BSE}
    assert {listing.exchange for listing in derived.listings} == {Exchange.BSE}
    reliance = next(listing for listing in derived.listings if listing.isin == RELIANCE)
    assert reliance.security_code == "500325"  # BSE keys listings on the scrip code
    # BSE gives no rename history: exactly one open window per scrip.
    assert all(w.valid_to is None for w in derived.windows)
    assert all(w.valid_from == SNAPSHOT for w in derived.windows)


def test_bse_windows_do_not_clobber_nse_in_a_combined_master(scrip_json: str) -> None:
    """A dual-listed ISIN keeps its NSE listing and gains a BSE one; both resolve independently."""
    scrips, _ = scrip_master.parse_scrip_master(scrip_json)
    derived = scrip_master.derive_master(scrips, snapshot_date=SNAPSHOT)

    # An NSE listing/window for the same ISIN, as M1.7 would have written it.
    nse_window = SymbolWindow(
        exchange=Exchange.NSE,
        symbol="RELIANCE",
        valid_from=date(2002, 1, 1),
        valid_to=None,
        isin=RELIANCE,
        source="nse_equity_list",
    )
    nse_listing = Listing(
        isin=RELIANCE, exchange=Exchange.NSE, status=ListingStatus.ACTIVE, series="EQ"
    )
    nse_security = Security(
        isin=RELIANCE,
        name="RELIANCE INDUSTRIES LTD",
        primary_exchange=Exchange.NSE,
        status=ListingStatus.ACTIVE,
        first_seen_date=date(2002, 1, 1),
    )

    master = IdentityMaster(
        (nse_window, *derived.windows),
        securities=(nse_security, *derived.securities),
        listings=(nse_listing, *derived.listings),
    )

    # Both exchanges' listings survive side by side — neither clobbers the other.
    assert master.listing(RELIANCE, Exchange.NSE) is nse_listing
    bse_listing = master.listing(RELIANCE, Exchange.BSE)
    assert bse_listing is not None and bse_listing.security_code == "500325"

    # The ISIN resolves from either exchange's symbol.
    assert master.resolve("RELIANCE", SNAPSHOT, exchange=Exchange.NSE) == RELIANCE
    assert master.resolve("RELIANCE", SNAPSHOT, exchange=Exchange.BSE) == RELIANCE
    # The BSE-only ISIN keeps its BSE primary exchange.
    assert master.security(BSE_ONLY).primary_exchange is Exchange.BSE


def test_dual_listing_is_not_a_conflict(scrip_json: str) -> None:
    """The same ISIN on NSE and BSE is a dual listing, not an ambiguous identity."""
    scrips, _ = scrip_master.parse_scrip_master(scrip_json)
    derived = scrip_master.derive_master(scrips, snapshot_date=SNAPSHOT)
    nse_window = SymbolWindow(
        exchange=Exchange.NSE,
        symbol="RELIANCE",
        valid_from=date(2002, 1, 1),
        valid_to=None,
        isin=RELIANCE,
        source="nse_equity_list",
    )
    conflicts = detect_conflicts((nse_window, *derived.windows), source="test")
    assert conflicts == ()


# ── wiring into the backfill runner ────────────────────────────────────────────────────────────


def test_bse_bhavcopy_is_a_backfill_source_set() -> None:
    assert BSE_BHAVCOPY in SOURCE_SETS
    assert SOURCE_SETS[BSE_BHAVCOPY].name == BSE_BHAVCOPY


def test_backfill_request_builds_the_udiff_url() -> None:
    register = source_register.load()
    request = SOURCE_SETS[BSE_BHAVCOPY].build_request(UDIFF_SESSION, register)
    assert request.state_source == BSE_BHAVCOPY
    assert request.fetch_source == "bse_bhavcopy_udiff"
    assert request.url.endswith("BhavCopy_BSE_CM_0_0_0_20260807_F_0000.CSV")
    assert request.filename == "BhavCopy_BSE_CM_0_0_0_20260807_F_0000.CSV"


def test_backfill_refuses_a_legacy_bse_date() -> None:
    register = source_register.load()
    with pytest.raises(ValueError, match="before the BSE UDiFF cutover"):
        SOURCE_SETS[BSE_BHAVCOPY].build_request(LEGACY_SESSION, register)
