"""M3.7 — F&O EOD aggregates (OI, PCR, basis).

Three acceptance criteria drive this file:

1. **Contract rows parse; PCR/OI/basis aggregates reconcile to a hand check.** The parser reads the
   UDiFF F&O bhavcopy fixture into `FoContractRow`s (and refuses a cash file that shares its
   header), and `build_aggregates` produces per-underlier numbers that match values worked out by
   hand in `tests/fixtures/nse_fo/udiff/PROVENANCE.md`.
2. **Aggregates live in L2 and rebuild from L1.** Contract rows round-trip through L1, aggregates
   round-trip through L2, and `rebuild_l2_from_l1` re-derives L2 from the L1 partition byte-wise.
3. **No code path can place a derivatives order.** The F&O modules are pure derivation — they
   import nothing from `execution` and expose no order-placing callable — asserted structurally.

Offline and deterministic (B8): every byte read here comes from `tests/fixtures/`.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest

from dataplatform.clock import FrozenClock
from dataplatform.identity.master import Exchange, IdentityMaster, SymbolWindow
from dataplatform.ingest.models import ParseError
from dataplatform.ingest.nse.fo_bhavcopy import (
    FO_COLUMNS,
    FoContractRow,
    FoInstrumentType,
    OptionType,
    parse,
    parse_l0,
    parse_text,
)
from dataplatform.store.fo_aggregates import (
    FO_AGGREGATES_DATASET,
    FO_CONTRACTS_DATASET,
    UnderlyingAggregate,
    UnderlyingKind,
    build_aggregates,
    read_l1,
    read_l2,
    rebuild_l2_from_l1,
    write_l1,
    write_l2,
)
from dataplatform.store.l0 import L0Store
from dataplatform.store.paths import l1_partition_path, l2_partition_path

SESSION: Final = date(2026, 8, 7)
NEAR: Final = date(2026, 8, 27)
NEXT: Final = date(2026, 9, 24)
FIXTURE_DIR: Final = Path(__file__).resolve().parents[1] / "fixtures" / "nse_fo" / "udiff"
FIXTURE_FILE: Final = "BhavCopy_NSE_FO_0_0_0_20260807_F_0000.csv.zip"


@pytest.fixture
def payload() -> bytes:
    return (FIXTURE_DIR / FIXTURE_FILE).read_bytes()


@pytest.fixture
def rows(payload: bytes) -> tuple[FoContractRow, ...]:
    return parse(payload, filename=FIXTURE_FILE)


@pytest.fixture
def master() -> IdentityMaster:
    """A tiny identity master that resolves the stock underlier RELIANCE to its ISIN."""
    return IdentityMaster(
        (
            SymbolWindow(
                exchange=Exchange.NSE,
                symbol="RELIANCE",
                valid_from=date(2000, 1, 1),
                valid_to=None,
                isin="INE002A01018",
            ),
        )
    )


def _by_underlying(
    aggregates: tuple[UnderlyingAggregate, ...],
) -> dict[str, UnderlyingAggregate]:
    return {agg.underlying: agg for agg in aggregates}


# ── acceptance 1a: contract rows parse ──────────────────────────────────────────────────────


def test_all_contracts_parse(rows: tuple[FoContractRow, ...]) -> None:
    """The fixture's eleven contracts all parse, each on the session's trade date."""
    assert len(rows) == 11
    assert {row.trade_date for row in rows} == {SESSION}
    assert {row.underlying for row in rows} == {"NIFTY", "RELIANCE"}


def test_a_future_row_carries_no_strike_or_option_type(rows: tuple[FoContractRow, ...]) -> None:
    """The NIFTY near future is a future: expiry and settlement, no strike, no CE/PE."""
    near = next(
        r
        for r in rows
        if r.underlying == "NIFTY"
        and r.instrument_type is FoInstrumentType.FUTIDX
        and r.expiry == NEAR
    )
    assert near.is_future and not near.is_option
    assert near.strike is None
    assert near.option_type is None
    assert near.settle == Decimal("24050.00")
    assert near.underlying_price == Decimal("24000.00")
    assert near.open_interest == 1000
    assert near.change_in_oi == 100
    assert near.is_index_underlying


def test_an_option_row_carries_a_strike_and_side(rows: tuple[FoContractRow, ...]) -> None:
    """A RELIANCE put option carries its strike and PE flag, and a signed OI change."""
    put = next(
        r
        for r in rows
        if r.underlying == "RELIANCE"
        and r.option_type is OptionType.PE
        and r.strike == Decimal("3000.00")
    )
    assert put.is_option and not put.is_future
    assert put.instrument_type is FoInstrumentType.OPTSTK
    assert put.change_in_oi == -20  # OI genuinely falls; the signed field carries it
    assert not put.is_index_underlying


def test_isin_column_is_empty_for_derivatives(rows: tuple[FoContractRow, ...]) -> None:
    """The file leaves ISIN empty on derivative rows; the parser keeps it None, not ''."""
    assert all(row.isin is None for row in rows)


# ── acceptance 1b: the parser refuses what it must ──────────────────────────────────────────


def _cash_row_text() -> str:
    """A one-row cash (CM/STK) file with the shared UDiFF header — refused by the FO parser."""
    header = ",".join(FO_COLUMNS)
    values = [""] * len(FO_COLUMNS)
    field = dict(zip(FO_COLUMNS, values, strict=True))
    field.update(
        {
            "TradDt": "2026-08-07",
            "BizDt": "2026-08-07",
            "Sgmt": "CM",
            "Src": "NSE",
            "FinInstrmTp": "STK",
            "FinInstrmId": "1",
            "ISIN": "INE002A01018",
            "TckrSymb": "RELIANCE",
            "SctySrs": "EQ",
            "OpnPric": "3000.00",
            "HghPric": "3000.00",
            "LwPric": "3000.00",
            "ClsPric": "3000.00",
            "LastPric": "3000.00",
            "PrvsClsgPric": "3000.00",
            "SttlmPric": "3000.00",
            "TtlTradgVol": "1",
            "TtlTrfVal": "3000.00",
            "TtlNbOfTxsExctd": "1",
            "SsnId": "F1",
        }
    )
    return header + "\n" + ",".join(field[col] for col in FO_COLUMNS) + "\n"


def test_a_cash_file_is_refused_despite_the_shared_header() -> None:
    """The FO parser reads the same header as the cash file; the Sgmt=FO guard separates them."""
    with pytest.raises(ParseError, match="not the F&O segment"):
        parse_text(_cash_row_text(), filename="cash.csv")


def test_an_unknown_instrument_type_is_refused() -> None:
    """A FinInstrmTp outside the equity-derivatives set is a format change to stop on."""
    text = _fo_line(fin_tp="FUTCUR", strike="", optn="")
    with pytest.raises(ParseError, match="not an equity-derivatives instrument type"):
        parse_text(text, filename="fo.csv")


def test_a_future_carrying_a_strike_is_refused() -> None:
    """A future with a strike is a contradiction — the row is not the contract its type claims."""
    text = _fo_line(fin_tp="FUTIDX", strike="24000.00", optn="")
    with pytest.raises(ParseError, match="a future has no strike"):
        parse_text(text, filename="fo.csv")


def test_an_option_missing_its_strike_is_refused() -> None:
    """An option with no strike is refused rather than read as a zero-strike contract."""
    text = _fo_line(fin_tp="OPTIDX", strike="", optn="CE")
    with pytest.raises(ParseError, match="must state its strike"):
        parse_text(text, filename="fo.csv")


def test_a_negative_open_interest_is_refused() -> None:
    """OpnIntrst is a count and non-negative; only its change may be signed."""
    text = _fo_line(fin_tp="FUTIDX", strike="", optn="", oi="-5")
    with pytest.raises(ParseError, match="non-negative integer"):
        parse_text(text, filename="fo.csv")


def test_a_wrong_header_is_refused() -> None:
    with pytest.raises(ParseError, match="unexpected header"):
        parse_text("a,b,c\n1,2,3\n", filename="fo.csv")


def test_rows_from_two_sessions_are_refused() -> None:
    line_a = _fo_data_line(trad="2026-08-07")
    line_b = _fo_data_line(trad="2026-08-10")
    text = ",".join(FO_COLUMNS) + "\n" + line_a + "\n" + line_b + "\n"
    with pytest.raises(ParseError, match="more than one session"):
        parse_text(text, filename="fo.csv")


def _fo_line(*, fin_tp: str, strike: str, optn: str, oi: str = "10") -> str:
    """Header + one FO data row, for the single-row rejection tests."""
    return (
        ",".join(FO_COLUMNS) + "\n" + _fo_data_line(fin_tp=fin_tp, strike=strike, optn=optn, oi=oi)
    )


def _fo_data_line(
    *,
    trad: str = "2026-08-07",
    fin_tp: str = "FUTIDX",
    strike: str = "",
    optn: str = "",
    oi: str = "10",
) -> str:
    field = dict(zip(FO_COLUMNS, [""] * len(FO_COLUMNS), strict=True))
    field.update(
        {
            "TradDt": trad,
            "BizDt": trad,
            "Sgmt": "FO",
            "Src": "NSE",
            "FinInstrmTp": fin_tp,
            "FinInstrmId": "1",
            "TckrSymb": "NIFTY",
            "XpryDt": "2026-08-27",
            "FininstrmActlXpryDt": "2026-08-27",
            "StrkPric": strike,
            "OptnTp": optn,
            "FinInstrmNm": "NIFTY FUT",
            "ClsPric": "24050.00",
            "LastPric": "24050.00",
            "PrvsClsgPric": "24050.00",
            "UndrlygPric": "24000.00",
            "SttlmPric": "24050.00",
            "OpnIntrst": oi,
            "ChngInOpnIntrst": "0",
            "TtlTradgVol": "1",
            "TtlTrfVal": "1.00",
            "TtlNbOfTxsExctd": "1",
            "SsnId": "F1",
            "NewBrdLotQty": "50",
        }
    )
    return ",".join(field[col] for col in FO_COLUMNS)


# ── acceptance 1c: aggregates reconcile to the hand check ────────────────────────────────────


def test_nifty_aggregates_match_the_hand_check(
    rows: tuple[FoContractRow, ...], master: IdentityMaster
) -> None:
    """NIFTY, an index underlier — no ISIN, PCR 1000/800, basis +50 = 0.208333 %."""
    nifty = _by_underlying(build_aggregates(rows, master=master))["NIFTY"]
    assert nifty.underlying_kind is UnderlyingKind.INDEX
    assert nifty.isin is None
    assert nifty.spot == Decimal("24000.0000")
    assert nifty.total_oi == 3200
    assert nifty.total_oi_change == 215
    assert nifty.call_oi == 800
    assert nifty.put_oi == 1000
    assert nifty.pcr_oi == Decimal("1.250000")
    assert nifty.near_expiry == NEAR
    assert nifty.near_future_price == Decimal("24050.0000")
    assert nifty.basis == Decimal("50.0000")
    assert nifty.basis_pct == Decimal("0.208333")
    assert nifty.near_month_oi == 1000
    assert nifty.next_month_oi == 400
    assert nifty.rollover_pct == Decimal("0.285714")


def test_reliance_aggregates_match_the_hand_check(
    rows: tuple[FoContractRow, ...], master: IdentityMaster
) -> None:
    """RELIANCE, a stock underlier — ISIN via the master, negative OI change, basis +10."""
    rel = _by_underlying(build_aggregates(rows, master=master))["RELIANCE"]
    assert rel.underlying_kind is UnderlyingKind.STOCK
    assert rel.isin == "INE002A01018"
    assert rel.spot == Decimal("3000.0000")
    assert rel.total_oi == 1800
    assert rel.total_oi_change == -35
    assert rel.call_oi == 400
    assert rel.put_oi == 400
    assert rel.pcr_oi == Decimal("1.000000")
    assert rel.near_expiry == NEAR
    assert rel.near_future_price == Decimal("3010.0000")
    assert rel.basis == Decimal("10.0000")
    assert rel.basis_pct == Decimal("0.333333")
    assert rel.near_month_oi == 800
    assert rel.next_month_oi == 200
    assert rel.rollover_pct == Decimal("0.200000")


def test_aggregates_are_sorted_by_underlying(rows: tuple[FoContractRow, ...]) -> None:
    """Deterministic output order — the parquet written from it is byte-stable."""
    aggregates = build_aggregates(rows)
    assert [agg.underlying for agg in aggregates] == ["NIFTY", "RELIANCE"]


def test_stock_underlying_is_unresolved_without_a_master(rows: tuple[FoContractRow, ...]) -> None:
    """No master means no symbol→ISIN step — the stock aggregate keeps isin None, not a guess."""
    rel = _by_underlying(build_aggregates(rows))["RELIANCE"]
    assert rel.underlying_kind is UnderlyingKind.STOCK
    assert rel.isin is None


def _synthetic(
    underlying: str,
    instrument_type: FoInstrumentType,
    *,
    expiry: date = NEAR,
    strike: Decimal | None = None,
    option_type: OptionType | None = None,
    settle: Decimal = Decimal("100.00"),
    spot: Decimal = Decimal("100.00"),
    oi: int = 0,
    change: int = 0,
) -> FoContractRow:
    return FoContractRow(
        trade_date=SESSION,
        underlying=underlying,
        instrument_type=instrument_type,
        expiry=expiry,
        strike=strike,
        option_type=option_type,
        close=settle,
        settle=settle,
        underlying_price=spot,
        open_interest=oi,
        change_in_oi=change,
        total_traded_qty=1,
        total_traded_value=Decimal("1.00"),
        total_trades=1,
    )


def test_options_only_underlying_has_no_futures_fields() -> None:
    """An underlier with only options has no basis/rollover — the fields are None, not zero."""
    rows = (
        _synthetic(
            "X", FoInstrumentType.OPTIDX, strike=Decimal("100"), option_type=OptionType.CE, oi=10
        ),
        _synthetic(
            "X", FoInstrumentType.OPTIDX, strike=Decimal("100"), option_type=OptionType.PE, oi=15
        ),
    )
    agg = build_aggregates(rows)[0]
    assert agg.near_expiry is None
    assert agg.near_future_price is None
    assert agg.basis is None
    assert agg.basis_pct is None
    assert agg.near_month_oi is None
    assert agg.next_month_oi is None
    assert agg.rollover_pct is None
    assert agg.pcr_oi == Decimal("1.500000")  # 15 / 10


def test_pcr_is_none_when_there_is_no_call_oi() -> None:
    """A division by zero is an absent PCR, not a crash and not a zero."""
    rows = (
        _synthetic(
            "Y", FoInstrumentType.OPTIDX, strike=Decimal("100"), option_type=OptionType.PE, oi=5
        ),
    )
    agg = build_aggregates(rows)[0]
    assert agg.put_oi == 5
    assert agg.call_oi == 0
    assert agg.pcr_oi is None


def test_rollover_is_none_with_a_single_expiry() -> None:
    """One futures expiry means nothing to roll into — rollover absent, near_month_oi still set."""
    rows = (_synthetic("Z", FoInstrumentType.FUTIDX, oi=100),)
    agg = build_aggregates(rows)[0]
    assert agg.near_month_oi == 100
    assert agg.next_month_oi is None
    assert agg.rollover_pct is None


# ── acceptance 2: L1 round-trip, L2 round-trip, rebuild L2 from L1 ───────────────────────────


def test_contract_rows_round_trip_through_l1(
    rows: tuple[FoContractRow, ...], tmp_path: Path
) -> None:
    written = write_l1(rows, data_root=tmp_path)
    assert written == l1_partition_path(FO_CONTRACTS_DATASET, SESSION, data_root=tmp_path)
    assert read_l1(SESSION, data_root=tmp_path) == rows


def test_l1_write_is_deterministic(rows: tuple[FoContractRow, ...], tmp_path: Path) -> None:
    first = write_l1(rows, data_root=tmp_path).read_bytes()
    second = write_l1(rows, data_root=tmp_path).read_bytes()
    assert first == second


def test_aggregates_round_trip_through_l2(
    rows: tuple[FoContractRow, ...], master: IdentityMaster, tmp_path: Path
) -> None:
    aggregates = build_aggregates(rows, master=master)
    written = write_l2(aggregates, data_root=tmp_path)
    assert written == l2_partition_path(FO_AGGREGATES_DATASET, SESSION, data_root=tmp_path)
    assert read_l2(SESSION, data_root=tmp_path) == aggregates


def test_l2_rebuilds_from_l1_identically(
    rows: tuple[FoContractRow, ...], master: IdentityMaster, tmp_path: Path
) -> None:
    """Acceptance 2 made executable: L2 re-derived from the L1 partition, byte-for-byte stable."""
    write_l1(rows, data_root=tmp_path)
    direct = write_l2(build_aggregates(rows, master=master), data_root=tmp_path).read_bytes()

    rebuilt_path = rebuild_l2_from_l1(SESSION, master=master, data_root=tmp_path)
    rebuilt_bytes = rebuilt_path.read_bytes()

    assert rebuilt_bytes == direct
    assert read_l2(SESSION, data_root=tmp_path) == build_aggregates(rows, master=master)


def test_reading_a_missing_partition_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_l1(SESSION, data_root=tmp_path)
    with pytest.raises(FileNotFoundError):
        read_l2(SESSION, data_root=tmp_path)


def test_parse_l0_round_trips_through_the_store(payload: bytes, tmp_path: Path) -> None:
    """The pipeline entry point: bytes land in L0 and parse_l0 reads them back, checksum checked."""
    store = L0Store(clock=FrozenClock(date(2026, 8, 7)), data_root=tmp_path)
    ref = store.put(
        "nse_fo_bhavcopy",
        SESSION,
        FIXTURE_FILE,
        payload,
        content_type="application/zip",
    )
    assert parse_l0(store, ref) == parse(payload, filename=FIXTURE_FILE)


# ── acceptance 3: no code path can place a derivatives order ─────────────────────────────────

_FO_MODULE_FILES: Final = (
    Path(__file__).resolve().parents[2] / "dataplatform" / "ingest" / "nse" / "fo_bhavcopy.py",
    Path(__file__).resolve().parents[2] / "dataplatform" / "store" / "fo_aggregates.py",
)
_ORDER_VERBS: Final = ("order", "place", "buy", "sell", "trade", "broker", "execute", "position")


def test_fo_modules_do_not_import_execution() -> None:
    """F&O is sentiment context only: the modules never reach the execution/broker layer."""
    for module_file in _FO_MODULE_FILES:
        source = module_file.read_text()
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")):
                assert "execution" not in stripped, f"{module_file.name}: {stripped}"
                assert "broker" not in stripped, f"{module_file.name}: {stripped}"


def test_fo_public_api_exposes_no_order_placing_callable() -> None:
    """Nothing an importer can call is named for placing, sizing, or routing an order."""
    from dataplatform.ingest.nse import fo_bhavcopy
    from dataplatform.store import fo_aggregates

    for module in (fo_bhavcopy, fo_aggregates):
        for name in module.__all__:
            lowered = name.lower()
            assert not any(verb in lowered for verb in _ORDER_VERBS), f"{module.__name__}.{name}"
