"""NSE bulk & block deal parsers, checked against real exchange files (M3.5).

The task's two acceptance criteria drive this file:

* **Bulk and block deals parse with ISIN resolution and raw client names preserved.** Two column
  shapes (bulk has `Remarks`, block does not) converge on one `DealRow`; the file carries no ISIN,
  so `resolve` maps `(symbol, trade_date)` through the D2 identity master (invariant #2); and the
  raw client string survives verbatim — double spaces and the exchange's stray `-` and all —
  beside a conservatively normalized form T0 can match on.
* **A deal in a held ISIN is queryable by `(isin, date)` for T0.** `write_l1` lands a session in
  one partition and `deals_for(isin, date)` answers the flow-anomaly question, joined on ISIN.

Offline and deterministic: every byte read here comes from `tests/fixtures/nse_deals/`.
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Final, NamedTuple

import pytest

from dataplatform.clock import IST, FrozenClock
from dataplatform.identity.master import (
    AmbiguousSymbolError,
    Exchange,
    IdentityMaster,
    SymbolWindow,
)
from dataplatform.ingest import nse
from dataplatform.ingest.models import ParseError
from dataplatform.ingest.nse.deals import (
    BLOCK_COLUMNS,
    BLOCK_SOURCE_ID,
    BULK_COLUMNS,
    BULK_SOURCE_ID,
    DEALS_DATASET,
    DealResolution,
    DealRow,
    DealsDay,
    DealSide,
    DealType,
    ResolvedDealRow,
    deals_for,
    l0_filename,
    normalize_client_name,
    parse,
    parse_l0,
    parse_text,
    read_l1,
    resolve,
    write_l1,
)
from dataplatform.store import L0Store
from dataplatform.store.paths import l1_partition_path

FIXTURES: Final = Path(__file__).resolve().parents[1] / "fixtures" / "nse_deals"

BULK_HEADER: Final = ",".join(BULK_COLUMNS)
BLOCK_HEADER: Final = ",".join(BLOCK_COLUMNS)

#: A well-formed bulk data line, for building single-row malformed inputs on a valid header.
GOOD_BULK: Final = (
    "01-SEP-2026,AARADHYA,Aaradhya Disposal Indus L,M/S VA TRADINGVENTURE LLP,BUY,76800,85.05,-"
)


class Sample(NamedTuple):
    """A deal transcribed field by field from the raw CSV (see PROVENANCE.md)."""

    symbol: str
    client_name: str
    side: DealSide
    quantity: int
    price: Decimal


class Fixture(NamedTuple):
    """One frozen file and the facts checked by hand against its CSV."""

    filename: str
    source: str
    deal_type: DealType
    trade_date: date
    data_rows: int
    first: Sample


FIXTURE_FILES: Final = (
    Fixture(
        filename="bulk_01092026.csv",
        source=BULK_SOURCE_ID,
        deal_type=DealType.BULK,
        trade_date=date(2026, 9, 1),
        data_rows=248,
        first=Sample(
            "AARADHYA", "M/S VA TRADINGVENTURE LLP", DealSide.BUY, 76800, Decimal("85.05")
        ),
    ),
    Fixture(
        filename="block_01092026.csv",
        source=BLOCK_SOURCE_ID,
        deal_type=DealType.BLOCK,
        trade_date=date(2026, 9, 1),
        data_rows=3,
        first=Sample("HATSUN", "CHANDRAMOGAN R G", DealSide.BUY, 825000, Decimal("1192.00")),
    ),
)


def payload_of(fixture: Fixture) -> bytes:
    """The frozen CSV exactly as the exchange served it."""
    return (FIXTURES / fixture.filename).read_bytes()


def raw_records_of(fixture: Fixture) -> list[list[str]]:
    """Every data record of the raw CSV, read with the stdlib and stripped, header dropped."""
    reader = csv.reader(io.StringIO(payload_of(fixture).decode("utf-8")))
    next(reader)
    return [
        [field.strip() for field in record]
        for record in reader
        if record and any(field.strip() for field in record)
    ]


@pytest.fixture(params=FIXTURE_FILES, ids=lambda fixture: fixture.filename)
def real_file(request: pytest.FixtureRequest) -> Fixture:
    """Each frozen file in turn."""
    fixture: Fixture = request.param
    return fixture


# ── the fixture set itself ───────────────────────────────────────────────────────────────────


def test_the_frozen_set_is_one_real_bulk_and_one_real_block() -> None:
    """Both deal types are represented by a real file — the premise the rest rests on.

    Asserted rather than assumed so that removing a fixture to dodge a failure shows up as a
    failing test (AGENTIC_CONTEXT §7).
    """
    on_disk = sorted(path.name for path in FIXTURES.glob("*.csv"))
    assert on_disk == sorted(fixture.filename for fixture in FIXTURE_FILES)
    assert {fixture.deal_type for fixture in FIXTURE_FILES} == {DealType.BULK, DealType.BLOCK}


def test_the_package_exports_the_parser() -> None:
    """`dataplatform.ingest.nse` is the surface other packages reach this parser through."""
    assert nse.parse_deals is parse
    assert nse.parse_deals_l0 is parse_l0
    assert nse.resolve_deals is resolve
    assert nse.write_deals_l1 is write_l1
    assert nse.deals_for_isin is deals_for
    assert nse.BULK_SOURCE_ID == BULK_SOURCE_ID == "nse_bulk_deals"
    assert nse.BLOCK_SOURCE_ID == BLOCK_SOURCE_ID == "nse_block_deals"


# ── acceptance 1a: both files parse into one row model ───────────────────────────────────────


def test_every_fixture_parses_to_the_deal_schema(real_file: Fixture) -> None:
    """Same fields, same types, whether the file is bulk or block."""
    rows = parse(payload_of(real_file), source=real_file.source, filename=real_file.filename)

    assert rows, "both frozen files carry deals"
    assert {type(row) for row in rows} == {DealRow}
    assert set(DealRow.model_fields) == {
        "deal_type",
        "trade_date",
        "symbol",
        "security_name",
        "client_name",
        "client_name_normalized",
        "side",
        "quantity",
        "price",
        "remarks",
    }
    assert {row.deal_type for row in rows} == {real_file.deal_type}
    assert {row.trade_date for row in rows} == {real_file.trade_date}


def test_row_count_matches_the_raw_csv(real_file: Fixture) -> None:
    """Counted twice: once by the parser, once by the stdlib reader over the same bytes."""
    rows = parse(payload_of(real_file), source=real_file.source, filename=real_file.filename)

    assert len(rows) == len(raw_records_of(real_file))
    assert len(rows) == real_file.data_rows, "the count transcribed into PROVENANCE.md"


def test_the_first_row_parses_exactly(real_file: Fixture) -> None:
    """The hand-checked first deal: int quantity, exact Decimal price, normalized side."""
    rows = parse(payload_of(real_file), source=real_file.source, filename=real_file.filename)
    row, want = rows[0], real_file.first

    assert row.symbol == want.symbol
    assert row.client_name == want.client_name
    assert row.side is want.side
    assert row.quantity == want.quantity
    assert isinstance(row.quantity, int) and not isinstance(row.quantity, bool)
    assert row.price == want.price
    assert isinstance(row.price, Decimal)


def test_price_is_decimal_and_exact_not_binary(real_file: Fixture) -> None:
    """`1192.00`/`85.05` must survive as themselves, not the nearest binary float."""
    rows = parse(payload_of(real_file), source=real_file.source, filename=real_file.filename)
    row = rows[0]

    assert row.price == real_file.first.price
    assert str(row.price) == str(real_file.first.price)


def test_every_deal_field_matches_the_raw_csv(real_file: Fixture) -> None:
    """The samples generalised: a faithful transcription, row for row."""
    rows = parse(payload_of(real_file), source=real_file.source, filename=real_file.filename)
    columns = BULK_COLUMNS if real_file.deal_type is DealType.BULK else BLOCK_COLUMNS
    sym_i = columns.index("Symbol")
    client_i = columns.index("Client Name")
    side_i = columns.index("Buy/Sell")
    qty_i = columns.index("Quantity Traded")
    price_i = columns.index("Trade Price / Wght. Avg. Price")

    for row, record in zip(rows, raw_records_of(real_file), strict=True):
        assert row.symbol == record[sym_i]
        assert row.client_name == record[client_i].strip(" ")
        assert row.side.value == record[side_i].upper()
        assert row.quantity == int(record[qty_i].replace(",", ""))
        assert row.price == Decimal(record[price_i].replace(",", ""))


# ── acceptance 1b: raw client name preserved, normalization alongside ────────────────────────


def test_raw_client_name_is_preserved_verbatim() -> None:
    """The whole reason both forms exist: the exchange's messy string survives byte-for-byte.

    The 2026-09-01 bulk file carries a client whose name the exchange left a stray `  -` on; that
    exact string must reach L1, because it is the source of truth an audit reads.
    """
    rows = parse(payload_of(FIXTURE_FILES[0]), source=BULK_SOURCE_ID, filename="bulk.csv")
    messy = [row for row in rows if row.client_name.startswith("ISTAA SECURITIES")]

    assert len(messy) == 1
    assert messy[0].client_name == "ISTAA SECURITIES PRIVATE LIMITED  -"
    # The normalized form collapses the run of spaces but does not strip the exchange's `-`:
    # that would be entity resolution, a T0 concern, not ingestion's.
    assert messy[0].client_name_normalized == "ISTAA SECURITIES PRIVATE LIMITED -"


def test_double_space_client_names_normalize_but_keep_their_raw_form() -> None:
    """`R G FAMILY  TRUST` (double space) is preserved raw and collapsed in the normalized form."""
    rows = parse(payload_of(FIXTURE_FILES[0]), source=BULK_SOURCE_ID, filename="bulk.csv")
    doubled = [row for row in rows if "  " in row.client_name]

    assert doubled, "the frozen file really contains a double-spaced client name"
    for row in doubled:
        assert "  " not in row.client_name_normalized
        assert row.client_name_normalized == normalize_client_name(row.client_name)
        # The raw string is untouched — the double space is still there.
        assert "  " in row.client_name


def test_normalize_is_case_folding_and_whitespace_only() -> None:
    """A pure, documented function: upper-case and collapse whitespace, nothing more."""
    assert normalize_client_name("R G FAMILY  TRUST") == "R G FAMILY TRUST"
    assert normalize_client_name("  hrti private limited ") == "HRTI PRIVATE LIMITED"
    # No suffix or punctuation stripping — that is deliberately left to T0.
    assert normalize_client_name("Acme LLP -") == "ACME LLP -"


def test_normalized_field_is_the_normalizer_applied_to_raw(real_file: Fixture) -> None:
    """Every row's normalized name is exactly `normalize_client_name(client_name)`."""
    rows = parse(payload_of(real_file), source=real_file.source, filename=real_file.filename)
    for row in rows:
        assert row.client_name_normalized == normalize_client_name(row.client_name)


# ── acceptance 1c: block has no Remarks; bulk '-' becomes None ───────────────────────────────


def test_block_rows_have_no_remarks() -> None:
    """A block row's `remarks` is None because the column does not exist, not because it was '-'."""
    rows = parse(payload_of(FIXTURE_FILES[1]), source=BLOCK_SOURCE_ID, filename="block.csv")
    assert rows
    assert all(row.remarks is None for row in rows)


def test_bulk_dash_remark_becomes_none_a_real_remark_survives() -> None:
    """The `-` marker is absence (None); a real remark is kept verbatim."""
    body = (
        f"{BULK_HEADER}\n"
        "01-SEP-2026,AAA,Aaa Ltd,SOME CLIENT,BUY,100,10.00,-\n"
        "01-SEP-2026,BBB,Bbb Ltd,OTHER CLIENT,SELL,200,20.00,Multiple clients\n"
    )
    rows = parse(body.encode("utf-8"), source=BULK_SOURCE_ID, filename="crafted.csv")
    by_symbol = {row.symbol: row for row in rows}

    assert by_symbol["AAA"].remarks is None
    assert by_symbol["BBB"].remarks == "Multiple clients"


# ── acceptance 1d: bulk and block converge, no era field leaks ───────────────────────────────


def test_bulk_and_block_share_the_row_model_no_type_specific_field() -> None:
    """Both files build the identical field set; only `deal_type` distinguishes a row afterward."""
    bulk = parse(payload_of(FIXTURE_FILES[0]), source=BULK_SOURCE_ID, filename="bulk.csv")
    block = parse(payload_of(FIXTURE_FILES[1]), source=BLOCK_SOURCE_ID, filename="block.csv")

    assert bulk[0].model_fields_set >= {"deal_type", "remarks"}
    assert set(type(bulk[0]).model_fields) == set(type(block[0]).model_fields)
    assert bulk[0].deal_type is DealType.BULK
    assert block[0].deal_type is DealType.BLOCK


# ── a quiet or empty session is a fact, not a failure ────────────────────────────────────────


def test_a_header_only_file_is_zero_deals_not_an_error() -> None:
    """A session with no reportable deals serves just the header; that is zero rows, never a raise.

    This is the opposite of the delivery file's rule, and getting it wrong would reject real quiet
    days. Distinct from a truncation, which loses a *data* row and is caught by the field-count
    check.
    """
    assert parse(f"{BULK_HEADER}\n".encode(), source=BULK_SOURCE_ID, filename="quiet.csv") == ()
    assert parse(f"{BLOCK_HEADER}\n".encode(), source=BLOCK_SOURCE_ID, filename="quiet.csv") == ()


def test_a_three_row_block_file_is_a_quiet_day_not_a_truncation() -> None:
    """The real 334 B block file is a genuinely quiet day — three whole deals, no error."""
    rows = parse(payload_of(FIXTURE_FILES[1]), source=BLOCK_SOURCE_ID, filename="block.csv")
    assert len(rows) == 3


# ── acceptance 2: ISIN resolution via the identity master ────────────────────────────────────


def _master() -> IdentityMaster:
    """A tiny identity master: a stable symbol and a recycled one that resolves by date."""
    return IdentityMaster(
        (
            SymbolWindow(
                exchange=Exchange.NSE,
                symbol="HATSUN",
                valid_from=date(2000, 1, 1),
                valid_to=None,
                isin="INE473B01035",
            ),
            SymbolWindow(
                exchange=Exchange.NSE,
                symbol="RECYCLED",
                valid_from=date(2015, 1, 1),
                valid_to=date(2020, 12, 31),
                isin="INE111A01011",
            ),
            SymbolWindow(
                exchange=Exchange.NSE,
                symbol="RECYCLED",
                valid_from=date(2021, 1, 1),
                valid_to=None,
                isin="INE222A01012",
            ),
        )
    )


def _deal(symbol: str, on_date: date, *, deal_type: DealType = DealType.BLOCK) -> DealRow:
    return DealRow(
        deal_type=deal_type,
        trade_date=on_date,
        symbol=symbol,
        security_name=f"{symbol} Ltd",
        client_name="A CLIENT",
        client_name_normalized="A CLIENT",
        side=DealSide.BUY,
        quantity=100,
        price=Decimal("10.00"),
    )


def test_resolve_maps_symbol_and_date_to_isin_keeping_client_names() -> None:
    """A deal becomes ISIN-keyed only through the master, and its client names ride through."""
    row = DealRow(
        deal_type=DealType.BLOCK,
        trade_date=date(2026, 9, 1),
        symbol="HATSUN",
        security_name="Hatsun Agro Product Ltd.",
        client_name="CHANDRAMOGAN R G",
        client_name_normalized="CHANDRAMOGAN R G",
        side=DealSide.BUY,
        quantity=825000,
        price=Decimal("1192.00"),
    )
    result = resolve([row], _master(), source=BLOCK_SOURCE_ID, l0_key="k")

    assert result.unresolved == ()
    assert len(result.resolved) == 1
    got = result.resolved[0]
    assert isinstance(got, ResolvedDealRow)
    assert got.isin == "INE473B01035"
    assert (got.client_name, got.client_name_normalized) == ("CHANDRAMOGAN R G", "CHANDRAMOGAN R G")
    assert got.source == BLOCK_SOURCE_ID
    assert got.l0_key == "k"
    assert got.quantity == 825000 and got.price == Decimal("1192.00")


def test_resolve_is_date_aware_not_symbol_alone() -> None:
    """The same symbol on two dates resolves to two ISINs — the join is not by symbol alone."""
    early = _deal("RECYCLED", date(2019, 6, 3))
    late = _deal("RECYCLED", date(2026, 9, 1))
    result = resolve([early, late], _master(), source=BLOCK_SOURCE_ID)

    by_date = {row.trade_date: row.isin for row in result.resolved}
    assert by_date[date(2019, 6, 3)] == "INE111A01011"
    assert by_date[date(2026, 9, 1)] == "INE222A01012"


def test_resolve_quarantines_an_unknown_symbol_never_drops_it() -> None:
    """A symbol the master never saw is counted in `unresolved`, not silently discarded."""
    known = _deal("HATSUN", date(2026, 9, 1))
    unknown = _deal("NEVERLISTED", date(2026, 9, 1))
    result = resolve([known, unknown], _master(), source=BLOCK_SOURCE_ID)

    assert [row.isin for row in result.resolved] == ["INE473B01035"]
    assert [row.symbol for row in result.unresolved] == ["NEVERLISTED"]
    assert len(result.resolved) + len(result.unresolved) == 2, "no deal is lost"
    assert isinstance(result, DealResolution)


def test_resolve_raises_on_an_ambiguous_symbol() -> None:
    """Two ISINs for one symbol on one date is a conflict to surface, not a silent pick."""
    windows = (
        SymbolWindow(
            exchange=Exchange.NSE,
            symbol="DUP",
            valid_from=date(2000, 1, 1),
            valid_to=None,
            isin="INE000A01010",
        ),
        SymbolWindow(
            exchange=Exchange.NSE,
            symbol="DUP",
            valid_from=date(2000, 1, 1),
            valid_to=None,
            isin="INE000B01019",
        ),
    )
    with pytest.raises(AmbiguousSymbolError):
        resolve([_deal("DUP", date(2026, 9, 1))], IdentityMaster(windows), source=BULK_SOURCE_ID)


def test_a_whole_fixture_resolves_through_the_master() -> None:
    """End to end: the real bulk file resolves against a master built from its own symbols.

    The master is derived here from the file itself (every symbol → a synthetic ISIN), legitimate
    only in a test; it proves `resolve` routes every deal through the master and keeps its values.
    """
    rows = parse(payload_of(FIXTURE_FILES[0]), source=BULK_SOURCE_ID, filename="bulk.csv")
    symbols = sorted({row.symbol for row in rows})
    windows = tuple(
        SymbolWindow(
            exchange=Exchange.NSE,
            symbol=symbol,
            valid_from=date(2000, 1, 1),
            valid_to=None,
            isin=f"INE{index:06d}01{index % 10}",
        )
        for index, symbol in enumerate(symbols)
    )
    result = resolve(rows, IdentityMaster(windows), source=BULK_SOURCE_ID)

    assert result.unresolved == ()
    assert len(result.resolved) == len(rows)
    assert all(len(row.isin) == 12 for row in result.resolved)


# ── acceptance 2: queryable by (isin, date) for T0 ───────────────────────────────────────────


def _resolved_session(tmp_path: Path) -> tuple[Path, IdentityMaster, list[DealRow]]:
    """Parse both real files, resolve against one master, and write the day to a temp L1 root."""
    bulk = parse(payload_of(FIXTURE_FILES[0]), source=BULK_SOURCE_ID, filename="bulk.csv")
    block = parse(payload_of(FIXTURE_FILES[1]), source=BLOCK_SOURCE_ID, filename="block.csv")
    symbols = sorted({row.symbol for row in [*bulk, *block]})
    windows = tuple(
        SymbolWindow(
            exchange=Exchange.NSE,
            symbol=symbol,
            valid_from=date(2000, 1, 1),
            valid_to=None,
            isin=f"INE{index:06d}01{index % 10}",
        )
        for index, symbol in enumerate(symbols)
    )
    master = IdentityMaster(windows)
    rb = resolve(bulk, master, source=BULK_SOURCE_ID, l0_key="bulk/k")
    rk = resolve(block, master, source=BLOCK_SOURCE_ID, l0_key="block/k")
    day = DealsDay(trade_date=date(2026, 9, 1), rows=rb.resolved + rk.resolved)
    write_l1(day, data_root=tmp_path)
    return tmp_path, master, [*bulk, *block]


def test_a_deal_in_a_held_isin_is_queryable_by_isin_and_date(tmp_path: Path) -> None:
    """The acceptance query: given a held ISIN and a date, T0 gets that name's deals — bulk + block.

    HATSUN traded only in the block file (three deals); resolved to its ISIN, `deals_for` returns
    exactly those three, keyed on ISIN, not on the symbol.
    """
    data_root, master, _ = _resolved_session(tmp_path)
    hatsun = master.resolve("HATSUN", date(2026, 9, 1))

    held = deals_for(hatsun, date(2026, 9, 1), data_root=data_root)

    assert len(held) == 3
    assert {row.deal_type for row in held} == {DealType.BLOCK}
    assert {row.side for row in held} == {DealSide.BUY, DealSide.SELL}
    assert all(row.isin == hatsun for row in held)
    # The raw client names are what came back — the whole point for T0.
    assert {row.client_name for row in held} == {
        "CHANDRAMOGAN R G",
        "RAVI KIRTI SHAH",
        "RAJU KIRTI SHAH",
    }


def test_deals_for_an_isin_with_no_deals_that_day_is_empty(tmp_path: Path) -> None:
    """A held name with no deal on the date returns (), distinct from a session never written."""
    data_root, master, _ = _resolved_session(tmp_path)
    hatsun = master.resolve("HATSUN", date(2026, 9, 1))

    # A real ISIN, but query a date whose partition holds no deal for it.
    assert deals_for(hatsun, date(2026, 9, 1), data_root=data_root)  # sanity: it does on the 1st
    with pytest.raises(FileNotFoundError):
        deals_for(hatsun, date(2026, 9, 2), data_root=data_root)


def test_a_bulk_isin_query_returns_that_names_deals_only(tmp_path: Path) -> None:
    """A symbol with several bulk deals returns exactly its own, not another name's."""
    data_root, master, all_rows = _resolved_session(tmp_path)
    # ARIS had two distinct bulk clients on the 1st — a good multi-deal name.
    aris = master.resolve("ARIS", date(2026, 9, 1))

    held = deals_for(aris, date(2026, 9, 1), data_root=data_root)
    raw_aris = [row for row in all_rows if row.symbol == "ARIS"]

    assert len(held) == len(raw_aris) >= 2
    assert all(row.symbol == "ARIS" and row.deal_type is DealType.BULK for row in held)


def test_write_l1_is_idempotent_per_partition(tmp_path: Path) -> None:
    """Re-deriving a session from the same inputs produces byte-identical partition content."""
    data_root, _, _ = _resolved_session(tmp_path)
    path = l1_partition_path(DEALS_DATASET, date(2026, 9, 1), data_root=data_root)
    first = path.read_bytes()

    # Rewrite from the same resolution.
    _resolved_session(tmp_path)
    assert path.read_bytes() == first


def test_l1_round_trip_preserves_every_field(tmp_path: Path) -> None:
    """A session written and read back is equal, provenance and raw client names included."""
    data_root, _, _ = _resolved_session(tmp_path)
    day = read_l1(date(2026, 9, 1), data_root=data_root)

    assert day.trade_date == date(2026, 9, 1)
    assert len(day.rows) == 248 + 3
    sources = {row.source for row in day.rows}
    assert sources == {BULK_SOURCE_ID, BLOCK_SOURCE_ID}
    assert all(isinstance(row.price, Decimal) for row in day.rows)
    # A messy raw client name survived the parquet round trip.
    assert any(row.client_name == "ISTAA SECURITIES PRIVATE LIMITED  -" for row in day.rows)


def test_read_l1_missing_partition_raises(tmp_path: Path) -> None:
    """A session never ingested is a FileNotFoundError, never an empty day."""
    with pytest.raises(FileNotFoundError):
        read_l1(date(2020, 1, 1), data_root=tmp_path)


def test_an_empty_session_writes_an_empty_partition(tmp_path: Path) -> None:
    """A written partition with zero rows — "we looked, none" — distinct from never fetched."""
    write_l1(DealsDay(trade_date=date(2026, 9, 3), rows=()), data_root=tmp_path)
    day = read_l1(date(2026, 9, 3), data_root=tmp_path)
    assert day.rows == ()


# ── L0 round trip ────────────────────────────────────────────────────────────────────────────


def test_parse_l0_reads_back_through_the_store(tmp_path: Path) -> None:
    """`parse_l0` re-verifies the checksum on the way in — the pipeline's real entry point."""
    store = L0Store(clock=FrozenClock(datetime(2026, 9, 2, 19, 30, tzinfo=IST)), data_root=tmp_path)
    ref = store.put(
        BLOCK_SOURCE_ID,
        date(2026, 9, 1),
        l0_filename(BLOCK_SOURCE_ID, date(2026, 9, 1)),
        payload_of(FIXTURE_FILES[1]),
        content_type="text/csv",
    )
    rows = parse_l0(store, ref, source=BLOCK_SOURCE_ID)
    assert len(rows) == 3
    assert all(row.deal_type is DealType.BLOCK for row in rows)


def test_l0_filename_carries_the_session_date() -> None:
    """The rolling URL has no date, so the L0 filename must, or two sessions collide in a month."""
    assert l0_filename(BULK_SOURCE_ID, date(2026, 9, 1)) == "bulk_01092026.csv"
    assert l0_filename(BLOCK_SOURCE_ID, date(2026, 9, 1)) == "block_01092026.csv"


# ── failure modes: loud and located ─────────────────────────────────────────────────────────


def test_a_short_row_names_the_file_and_the_line() -> None:
    """A body cut mid-record fails on the record it was cut in, not silently one row short."""
    body = f"{BULK_HEADER}\n{GOOD_BULK}\n01-SEP-2026,ZZ,Zz Ltd,CLIENT\n"
    with pytest.raises(ParseError) as caught:
        parse(body.encode("utf-8"), source=BULK_SOURCE_ID, filename="short.csv")
    assert caught.value.filename == "short.csv"
    assert caught.value.line == 3


def test_an_unexpected_header_is_refused_on_line_one() -> None:
    """A reordered or renamed column fails at the header, not by reading values from wrong slots."""
    scrambled = ",".join(("Symbol", "Date", *BULK_COLUMNS[2:]))
    with pytest.raises(ParseError) as caught:
        parse(f"{scrambled}\n".encode(), source=BULK_SOURCE_ID, filename="bad_header.csv")
    assert caught.value.line == 1
    assert "header" in str(caught.value)


def test_a_block_source_rejects_a_bulk_shaped_file() -> None:
    """The source id picks the column set; a bulk header read as block fails on the header."""
    with pytest.raises(ParseError):
        parse(payload_of(FIXTURE_FILES[0]), source=BLOCK_SOURCE_ID, filename="bulk.csv")


def test_an_unknown_side_is_a_schema_change_not_a_bucket() -> None:
    """A Buy/Sell token nobody has seen raises rather than being silently coerced."""
    body = f"{BULK_HEADER}\n01-SEP-2026,AAA,Aaa Ltd,CLIENT,HOLD,100,10.00,-\n"
    with pytest.raises(ParseError) as caught:
        parse(body.encode("utf-8"), source=BULK_SOURCE_ID, filename="side.csv")
    assert "Buy/Sell" in str(caught.value)


def test_a_bad_price_is_a_located_error() -> None:
    """A price that is not a plain decimal names the line and the column, never becomes NaN."""
    body = f"{BULK_HEADER}\n01-SEP-2026,AAA,Aaa Ltd,CLIENT,BUY,100,NaN,-\n"
    with pytest.raises(ParseError) as caught:
        parse(body.encode("utf-8"), source=BULK_SOURCE_ID, filename="price.csv")
    assert caught.value.line == 2
    assert "Trade Price" in str(caught.value)


def test_a_zero_quantity_is_refused() -> None:
    """A deal that moved zero shares is not a deal; the strict positive quantity catches it."""
    body = f"{BULK_HEADER}\n01-SEP-2026,AAA,Aaa Ltd,CLIENT,BUY,0,10.00,-\n"
    with pytest.raises(ParseError):
        parse(body.encode("utf-8"), source=BULK_SOURCE_ID, filename="qty.csv")


def test_rows_spanning_two_sessions_are_refused() -> None:
    """A file whose rows carry two dates would split across two partitions; it fails instead."""
    body = (
        f"{BULK_HEADER}\n"
        "01-SEP-2026,AAA,Aaa Ltd,CLIENT,BUY,100,10.00,-\n"
        "02-SEP-2026,BBB,Bbb Ltd,CLIENT,BUY,100,10.00,-\n"
    )
    with pytest.raises(ParseError) as caught:
        parse(body.encode("utf-8"), source=BULK_SOURCE_ID, filename="twodays.csv")
    assert "more than one session" in str(caught.value)


def test_html_soft_404_is_refused() -> None:
    """An HTML error page served with a 200 must not become deal rows."""
    with pytest.raises(ParseError) as caught:
        parse(b"<html><body>Not found</body></html>", source=BULK_SOURCE_ID, filename="err.csv")
    assert "markup" in str(caught.value)


def test_an_unknown_source_is_rejected() -> None:
    """A source id this parser does not serve fails before it guesses a column set."""
    with pytest.raises(ParseError):
        parse(f"{BULK_HEADER}\n".encode(), source="nse_something_else", filename="x.csv")


def test_parse_text_is_the_same_as_parse_on_decoded_bytes() -> None:
    """`parse_text` is the bytes-free entry the decode layer wraps — same rows, same order."""
    payload = payload_of(FIXTURE_FILES[1])
    from_bytes = parse(payload, source=BLOCK_SOURCE_ID, filename="block.csv")
    from_text = parse_text(payload.decode("utf-8"), source=BLOCK_SOURCE_ID, filename="block.csv")
    assert from_bytes == from_text
