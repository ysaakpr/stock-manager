"""The UDiFF NSE bhavcopy parser and the era dispatcher, checked against real exchange files (M1.5).

The UDiFF era (§4.1 row 2) is the second half of a dual parser. Three things have to be true of it,
and the acceptance criteria for M1.5 name each:

* **It emits the M1.4 row model, with no era-specific field leaking out.** The UDiFF file carries
  thirty-four columns, twenty-one of which have no place in a `PriceRow`; a caller must not be able
  to tell a UDiFF row from a legacy one. Proven by parsing real files and asserting the exact
  `PriceRow` field set, the `Decimal` types, and that the frozen model refuses any extra.
* **The dispatcher picks the right parser by date, including at the exact cutover.** `bhavcopy.py`
  routes `< 2024-07-08` to legacy and `>= 2024-07-08` to UDiFF; 08-Jul-2024 itself is UDiFF because
  that is the day the UDiFF file first exists and the legacy one first 404s (`PROVENANCE.md`).
* **A symbol present on both sides of the cutover parses to identical semantics.** RELIANCE closed
  the last legacy session at 3177.25 and the first UDiFF session reports that as its `prev_close`;
  both eras produce the same `PriceRow` type with the same field types for it.

Real fixtures (`PROVENANCE.md`), fetched through the M1.2 crawl policy. Offline and deterministic
(B8): every byte read here comes from `tests/fixtures/`.
"""

from __future__ import annotations

import csv
import io
import zipfile
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Final, NamedTuple

import pytest
from pydantic import ValidationError

from dataplatform.clock import IST, FrozenClock
from dataplatform.ingest import nse
from dataplatform.ingest.models import ParseError, PriceRow
from dataplatform.ingest.nse import bhavcopy
from dataplatform.ingest.nse.bhavcopy_legacy import LEGACY_ERA_END
from dataplatform.ingest.nse.bhavcopy_legacy import parse as parse_legacy
from dataplatform.ingest.nse.bhavcopy_udiff import (
    UDIFF_COLUMNS,
    UDIFF_ERA_START,
    UDIFF_SOURCE_ID,
    parse,
    parse_l0,
)
from dataplatform.store import L0Store

UDIFF_FIXTURES: Final = Path(__file__).resolve().parents[1] / "fixtures" / "nse_bhavcopy" / "udiff"
LEGACY_FIXTURES: Final = (
    Path(__file__).resolve().parents[1] / "fixtures" / "nse_bhavcopy" / "legacy"
)

#: The era's header line, verbatim, for building malformed inputs — no trailing comma this era.
HEADER: Final = ",".join(UDIFF_COLUMNS)

#: The legacy era's header, which this parser must refuse (the other side of M1.4's own refusal).
LEGACY_HEADER: Final = (
    "SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,"
    "TOTTRDQTY,TOTTRDVAL,TIMESTAMP,TOTALTRADES,ISIN,"
)


class Fixture(NamedTuple):
    """One frozen UDiFF archive and the facts about it checked by hand against the CSV."""

    filename: str
    trade_date: date
    data_rows: int
    sample: PriceRow


#: RELIANCE on each UDiFF session, transcribed field by field from the raw CSV line.
FIXTURE_FILES: Final = (
    Fixture(
        filename="BhavCopy_NSE_CM_0_0_0_20240708_F_0000.csv.zip",
        trade_date=date(2024, 7, 8),
        data_rows=2815,
        sample=PriceRow(
            isin="INE002A01018",
            symbol="RELIANCE",
            series="EQ",
            trade_date=date(2024, 7, 8),
            open=Decimal("3178.00"),
            high=Decimal("3217.60"),
            low=Decimal("3165.05"),
            close=Decimal("3201.80"),
            last=Decimal("3201.50"),
            prev_close=Decimal("3177.25"),
            total_traded_qty=4750403,
            total_traded_value=Decimal("15181479272.55"),
            total_trades=240527,
        ),
    ),
    Fixture(
        filename="BhavCopy_NSE_CM_0_0_0_20260807_F_0000.csv.zip",
        trade_date=date(2026, 8, 7),
        data_rows=3473,
        sample=PriceRow(
            isin="INE002A01018",
            symbol="RELIANCE",
            series="EQ",
            trade_date=date(2026, 8, 7),
            open=Decimal("1320.00"),
            high=Decimal("1337.00"),
            low=Decimal("1316.60"),
            close=Decimal("1334.80"),
            last=Decimal("1334.80"),
            prev_close=Decimal("1325.00"),
            total_traded_qty=9885638,
            total_traded_value=Decimal("13138878797.80"),
            total_trades=161575,
        ),
    ),
)

PRICE_FIELDS: Final = ("open", "high", "low", "close", "last", "prev_close", "total_traded_value")
COUNT_FIELDS: Final = ("total_traded_qty", "total_trades")

#: Column index of each field this parser reads, into a raw UDiFF record — for re-reading the CSV.
_IDX: Final = {name: UDIFF_COLUMNS.index(name) for name in UDIFF_COLUMNS}


def payload_of(fixture: Fixture) -> bytes:
    """The frozen archive exactly as the exchange served it."""
    return (UDIFF_FIXTURES / fixture.filename).read_bytes()


def raw_csv_of(fixture: Fixture) -> str:
    """The CSV member, unzipped here rather than by the parser — the independent reference."""
    with zipfile.ZipFile(io.BytesIO(payload_of(fixture))) as archive:
        (member,) = archive.namelist()
        return archive.read(member).decode("utf-8")


def raw_records_of(fixture: Fixture) -> list[list[str]]:
    """Every data record of the raw CSV, read with the stdlib, header dropped."""
    reader = csv.reader(io.StringIO(raw_csv_of(fixture)))
    next(reader)
    return [record for record in reader if record and any(field.strip() for field in record)]


def a_valid_record(**overrides: str) -> str:
    """One well-formed UDiFF cash-equity CSV line (RELIANCE, 08-Jul-2024), with fields overridable.

    The synthetic base for the malformed-input tests: change one field, keep the other thirty-three
    correct, and assert the parser fails on exactly the field changed.
    """
    fields = dict.fromkeys(UDIFF_COLUMNS, "")
    fields.update(
        TradDt="2024-07-08",
        BizDt="2024-07-08",
        Sgmt="CM",
        Src="NSE",
        FinInstrmTp="STK",
        FinInstrmId="2885",
        ISIN="INE002A01018",
        TckrSymb="RELIANCE",
        SctySrs="EQ",
        FinInstrmNm="RELIANCE INDUSTRIES LTD",
        OpnPric="3178.00",
        HghPric="3217.60",
        LwPric="3165.05",
        ClsPric="3201.80",
        LastPric="3201.50",
        PrvsClsgPric="3177.25",
        SttlmPric="3201.80",
        TtlTradgVol="4750403",
        TtlTrfVal="15181479272.55",
        TtlNbOfTxsExctd="240527",
        SsnId="F1",
        NewBrdLotQty="1",
    )
    fields.update(overrides)
    return ",".join(fields[name] for name in UDIFF_COLUMNS)


def a_file(*records: str) -> bytes:
    """A UDiFF CSV body (header + given records) as bytes."""
    return ("\n".join([HEADER, *records]) + "\n").encode("utf-8")


@pytest.fixture(params=FIXTURE_FILES, ids=lambda fixture: fixture.filename)
def era_file(request: pytest.FixtureRequest) -> Fixture:
    """Each frozen UDiFF file in turn."""
    fixture: Fixture = request.param
    return fixture


# ── the fixture set itself ───────────────────────────────────────────────────────────────────


def test_the_frozen_set_is_two_or_more_real_udiff_files() -> None:
    """2+ real files (M1.5 acceptance), one of them the cutover session itself.

    Asserted rather than assumed so that shrinking the fixture set to make something pass shows up
    as a failing test (AGENTIC_CONTEXT §7).
    """
    on_disk = sorted(path.name for path in UDIFF_FIXTURES.glob("*.zip"))
    assert on_disk == sorted(fixture.filename for fixture in FIXTURE_FILES)
    assert len(FIXTURE_FILES) >= 2

    sessions = sorted(fixture.trade_date for fixture in FIXTURE_FILES)
    assert all(session >= UDIFF_ERA_START for session in sessions), "every fixture is UDiFF-era"
    assert sessions[0] == UDIFF_ERA_START, "the earliest fixture is the cutover session itself"
    assert len(set(sessions)) == len(sessions)


def test_the_package_exports_the_parser_and_dispatcher() -> None:
    """`dataplatform.ingest.nse` is the surface other packages reach these through."""
    assert nse.parse_udiff_bhavcopy is parse
    assert nse.parse_udiff_bhavcopy_l0 is parse_l0
    assert nse.parse_bhavcopy is bhavcopy.parse
    assert nse.parse_bhavcopy_l0 is bhavcopy.parse_l0
    assert nse.bhavcopy_era_of is bhavcopy.era_of
    assert nse.UDIFF_SOURCE_ID == UDIFF_SOURCE_ID == "nse_bhavcopy_udiff"
    assert nse.BHAVCOPY_CUTOVER == bhavcopy.CUTOVER


# ── acceptance 1: the M1.4 row model, no era-specific field leaking ───────────────────────────


def test_every_fixture_parses_to_the_m14_row_schema(era_file: Fixture) -> None:
    """Same `PriceRow` fields and types as the legacy parser — nothing UDiFF-specific escapes."""
    rows = parse(payload_of(era_file), filename=era_file.filename)

    assert rows, "a session's bhavcopy is never empty"
    assert {type(row) for row in rows} == {PriceRow}
    assert set(PriceRow.model_fields) == {
        "isin",
        "symbol",
        "series",
        "trade_date",
        *PRICE_FIELDS,
        *COUNT_FIELDS,
    }
    assert {row.trade_date for row in rows} == {era_file.trade_date}


def test_no_udiff_only_field_can_leak_through_the_row() -> None:
    """The 21 UDiFF columns with no `PriceRow` home cannot arrive as extra attributes."""
    udiff_only = set(UDIFF_COLUMNS) - {
        "ISIN",
        "TckrSymb",
        "SctySrs",
        "TradDt",
        "OpnPric",
        "HghPric",
        "LwPric",
        "ClsPric",
        "LastPric",
        "PrvsClsgPric",
        "TtlTradgVol",
        "TtlTrfVal",
        "TtlNbOfTxsExctd",
    }
    assert {"XpryDt", "StrkPric", "OptnTp", "OpnIntrst", "SttlmPric"} <= udiff_only
    # `PriceRow` is frozen with extra="forbid": an era-specific column has nowhere to go.
    with pytest.raises(ValidationError):
        PriceRow(**{**FIXTURE_FILES[0].sample.model_dump(), "OpnIntrst": "123"})


def test_prices_are_decimal_and_never_float(era_file: Fixture) -> None:
    """Invariant the whole cost model rests on: no float ever enters a price field."""
    rows = parse(payload_of(era_file), filename=era_file.filename)

    for row in rows:
        for field in PRICE_FIELDS:
            value = getattr(row, field)
            assert isinstance(value, Decimal), f"{row.symbol}.{field} is {type(value).__name__}"
        for field in COUNT_FIELDS:
            value = getattr(row, field)
            assert isinstance(value, int) and not isinstance(value, bool)


# ── acceptance 2: the dispatcher picks the right parser by date, boundary included ────────────


def test_era_of_splits_at_the_cutover() -> None:
    """08-Jul-2024 is UDiFF; the trading day before it is legacy. The boundary is exact."""
    assert date(2024, 7, 8) == bhavcopy.CUTOVER
    assert bhavcopy.CUTOVER == UDIFF_ERA_START == LEGACY_ERA_END
    assert bhavcopy.era_of(date(2024, 7, 5)) == "legacy"
    assert bhavcopy.era_of(date(2024, 7, 7)) == "legacy"
    assert bhavcopy.era_of(date(2024, 7, 8)) == "udiff", "the cutover date itself is UDiFF"
    assert bhavcopy.era_of(date(2024, 7, 9)) == "udiff"
    assert bhavcopy.era_of(date(2016, 1, 1)) == "legacy"
    assert bhavcopy.era_of(date(2026, 8, 7)) == "udiff"


def test_dispatch_routes_the_boundary_session_to_the_udiff_parser() -> None:
    """The cutover file, dispatched by its date, must be read by the UDiFF parser and succeed."""
    fixture = FIXTURE_FILES[0]
    assert fixture.trade_date == bhavcopy.CUTOVER

    dispatched = bhavcopy.parse(
        payload_of(fixture), filename=fixture.filename, trade_date=fixture.trade_date
    )
    direct = parse(payload_of(fixture), filename=fixture.filename)
    assert dispatched == direct
    assert dispatched[0].trade_date == bhavcopy.CUTOVER


def test_dispatch_routes_a_pre_cutover_session_to_the_legacy_parser() -> None:
    """The last legacy session, dispatched by its date, is read by the legacy parser."""
    legacy_payload = (LEGACY_FIXTURES / "cm05JUL2024bhav.csv.zip").read_bytes()

    dispatched = bhavcopy.parse(
        legacy_payload, filename="cm05JUL2024bhav.csv.zip", trade_date=date(2024, 7, 5)
    )
    direct = parse_legacy(legacy_payload, filename="cm05JUL2024bhav.csv.zip")
    assert dispatched == direct
    assert dispatched[0].trade_date == date(2024, 7, 5)


def test_dispatching_a_udiff_file_under_a_legacy_date_fails_loudly() -> None:
    """A misrouted payload — UDiFF bytes filed under a pre-cutover date — must not parse silently.

    The legacy parser would be handed a UDiFF header and refuse it; the mismatch is caught either
    way, which is what makes dispatch-by-date safe.
    """
    fixture = FIXTURE_FILES[0]
    with pytest.raises(ParseError):
        bhavcopy.parse(payload_of(fixture), filename=fixture.filename, trade_date=date(2024, 7, 5))


def test_dispatch_rejects_a_file_whose_rows_disagree_with_the_dispatch_date() -> None:
    """UDiFF bytes for 08-Jul routed under a different UDiFF date is a filing error, caught here."""
    fixture = FIXTURE_FILES[0]
    with pytest.raises(ParseError, match="does not match the date it was filed under"):
        bhavcopy.parse(payload_of(fixture), filename=fixture.filename, trade_date=date(2024, 7, 9))


# ── acceptance 3: a symbol on both sides of the cutover has identical semantics ───────────────


def test_cross_era_continuity_for_a_symbol_present_in_both_formats() -> None:
    """RELIANCE spans the cutover: same row type, same field types, and prev_close carries across.

    The legacy fixture for the last pre-cutover session (2024-07-05) and the UDiFF fixture for the
    first post-cutover session (2024-07-08) both carry RELIANCE (INE002A01018, EQ). The two parsers
    must produce a byte-compatible `PriceRow` for it — and the UDiFF session's `prev_close` must be
    the legacy session's `close`, which is the continuity a dual parser exists to preserve.
    """
    legacy_rows = parse_legacy(
        (LEGACY_FIXTURES / "cm05JUL2024bhav.csv.zip").read_bytes(),
        filename="cm05JUL2024bhav.csv.zip",
    )
    udiff_rows = parse(payload_of(FIXTURE_FILES[0]), filename=FIXTURE_FILES[0].filename)

    isin = "INE002A01018"
    (legacy_reliance,) = [r for r in legacy_rows if r.isin == isin and r.series == "EQ"]
    (udiff_reliance,) = [r for r in udiff_rows if r.isin == isin and r.series == "EQ"]

    # Identical schema: same type, same fields, same field types — no era branch downstream.
    assert type(legacy_reliance) is type(udiff_reliance) is PriceRow
    assert type(legacy_reliance).model_fields.keys() == type(udiff_reliance).model_fields.keys()
    for field in PriceRow.model_fields:
        assert type(getattr(legacy_reliance, field)) is type(getattr(udiff_reliance, field))

    # Identical semantics: same instrument, same identity, and the price carried across the break.
    assert legacy_reliance.isin == udiff_reliance.isin
    assert legacy_reliance.symbol == udiff_reliance.symbol
    assert legacy_reliance.series == udiff_reliance.series
    assert legacy_reliance.trade_date < udiff_reliance.trade_date
    assert udiff_reliance.prev_close == legacy_reliance.close == Decimal("3177.25")


# ── the fixtures match the raw CSV exactly ───────────────────────────────────────────────────


def test_row_count_matches_the_raw_csv(era_file: Fixture) -> None:
    """Counted twice: once by the parser, once by the stdlib csv reader over the same member."""
    rows = parse(payload_of(era_file), filename=era_file.filename)

    assert len(rows) == len(raw_records_of(era_file))
    assert len(rows) == era_file.data_rows, "the count transcribed into PROVENANCE.md"


def test_hand_checked_sample_row_matches_the_raw_csv(era_file: Fixture) -> None:
    """Every field of one row, against both the literal transcription and the raw CSV line."""
    rows = parse(payload_of(era_file), filename=era_file.filename)
    expected = era_file.sample

    parsed = [r for r in rows if r.symbol == expected.symbol and r.series == expected.series]
    assert len(parsed) == 1
    assert parsed[0] == expected

    raw = [
        record
        for record in raw_records_of(era_file)
        if record[_IDX["TckrSymb"]] == expected.symbol
        and record[_IDX["SctySrs"]] == expected.series
    ]
    assert len(raw) == 1
    record = raw[0]
    assert record[_IDX["ISIN"]] == expected.isin
    assert Decimal(record[_IDX["OpnPric"]]) == expected.open
    assert Decimal(record[_IDX["HghPric"]]) == expected.high
    assert Decimal(record[_IDX["LwPric"]]) == expected.low
    assert Decimal(record[_IDX["ClsPric"]]) == expected.close
    assert Decimal(record[_IDX["LastPric"]]) == expected.last
    assert Decimal(record[_IDX["PrvsClsgPric"]]) == expected.prev_close
    assert int(record[_IDX["TtlTradgVol"]]) == expected.total_traded_qty
    assert Decimal(record[_IDX["TtlTrfVal"]]) == expected.total_traded_value
    assert int(record[_IDX["TtlNbOfTxsExctd"]]) == expected.total_trades


def test_every_field_of_every_row_matches_the_raw_csv(era_file: Fixture) -> None:
    """The sample generalised: the parser is a faithful transcription, row for row."""
    rows = parse(payload_of(era_file), filename=era_file.filename)

    for row, record in zip(rows, raw_records_of(era_file), strict=True):
        assert (row.symbol, row.series, row.isin) == (
            record[_IDX["TckrSymb"]],
            record[_IDX["SctySrs"]],
            record[_IDX["ISIN"]],
        )
        assert row.open == Decimal(record[_IDX["OpnPric"]])
        assert row.close == Decimal(record[_IDX["ClsPric"]])
        # LastPric is empty for a no-snapshot row; the parser normalises that to 0 (legacy parity).
        raw_last = record[_IDX["LastPric"]]
        assert row.last == (Decimal(0) if raw_last == "" else Decimal(raw_last))
        assert row.total_traded_qty == int(record[_IDX["TtlTradgVol"]])
        assert row.total_traded_value == Decimal(record[_IDX["TtlTrfVal"]])
        assert row.total_trades == int(record[_IDX["TtlNbOfTxsExctd"]])


def test_decimal_conversion_is_exact_not_binary() -> None:
    """A turnover not representable in binary floating point must be kept exactly."""
    fixture = FIXTURE_FILES[0]
    rows = parse(payload_of(fixture), filename=fixture.filename)
    (reliance,) = [r for r in rows if r.symbol == "RELIANCE" and r.series == "EQ"]

    assert reliance.total_traded_value == Decimal("15181479272.55")
    assert str(reliance.total_traded_value) == "15181479272.55"
    assert Decimal(float(reliance.total_traded_value)) != reliance.total_traded_value


def test_an_empty_last_price_becomes_zero_matching_the_legacy_era() -> None:
    """UDiFF spells "no last-traded-price snapshot" as an empty field; legacy spelled it 0.

    Normalising the empty to 0 is what keeps the `last` field identical across the two eras. Checked
    against the real row that carries it (IBHFZC25B / AT on the cutover session) so the behaviour is
    pinned to actual exchange data, not only to a synthetic line.
    """
    fixture = FIXTURE_FILES[0]
    rows = parse(payload_of(fixture), filename=fixture.filename)

    (row,) = [r for r in rows if r.symbol == "IBHFZC25B"]
    (record,) = [r for r in raw_records_of(fixture) if r[_IDX["TckrSymb"]] == "IBHFZC25B"]
    assert record[_IDX["LastPric"]] == "", "the fixture row really has an empty LastPric"
    assert row.last == Decimal(0)
    # The rest of the row is the real (non-empty) data, unaffected by the normalisation.
    assert row.close == Decimal(record[_IDX["ClsPric"]])
    assert row.total_traded_qty == int(record[_IDX["TtlTradgVol"]])

    # A synthetic row confirms the same, in isolation.
    (synthetic,) = parse(a_file(a_valid_record(LastPric="")), filename="empty_last.csv")
    assert synthetic.last == Decimal(0)


# ── SctySrs is kept: filtering is a query concern, not a parser's ─────────────────────────────


def test_every_series_survives_the_parser(era_file: Fixture) -> None:
    """Every series the exchange published is present, in the count the raw file has."""
    rows = parse(payload_of(era_file), filename=era_file.filename)
    records = raw_records_of(era_file)

    assert {row.series for row in rows} == {record[_IDX["SctySrs"]] for record in records}
    assert {"EQ", "BE"} <= {row.series for row in rows}
    assert len({row.series for row in rows}) > 5, "the era's long tail of debt/odd-lot series"
    assert sum(1 for row in rows if row.series != "EQ") == sum(
        1 for record in records if record[_IDX["SctySrs"]] != "EQ"
    )


def test_rows_keep_the_exchange_file_order(era_file: Fixture) -> None:
    """Deterministic output: re-parsing an L0 payload must reproduce the same sequence."""
    payload = payload_of(era_file)
    first = parse(payload, filename=era_file.filename)
    assert first == parse(payload, filename=era_file.filename)
    assert [row.symbol for row in first] == [
        record[_IDX["TckrSymb"]] for record in raw_records_of(era_file)
    ]


# ── malformed input raises a specific error naming the file and line ──────────────────────────


def test_truncated_archive_names_the_file() -> None:
    """The shape a cut-off download leaves behind."""
    fixture = FIXTURE_FILES[0]
    with pytest.raises(ParseError) as caught:
        parse(payload_of(fixture)[:4096], filename=fixture.filename)

    assert caught.value.filename == fixture.filename
    assert fixture.filename in str(caught.value)
    assert "zip" in str(caught.value)


def test_truncated_row_names_the_file_and_the_line() -> None:
    """A body cut mid-record fails on the record it was cut in, not silently one row short."""
    body = a_file(a_valid_record(), "2024-07-08,2024-07-08,CM,NSE,STK,1,INE002A01018,ZZ,EQ")
    with pytest.raises(ParseError) as caught:
        parse(body, filename="short.csv")

    assert caught.value.filename == "short.csv"
    assert caught.value.line == 3, "header is line 1, the valid row 2, the short row 3"
    assert "short.csv:3" in str(caught.value)
    assert "9 fields" in str(caught.value)


def test_a_row_wider_than_the_header_is_refused() -> None:
    """An extra field is a format change, not a curiosity."""
    body = a_file(a_valid_record() + ",extra")
    with pytest.raises(ParseError) as caught:
        parse(body, filename="wide.csv")

    assert caught.value.line == 2
    assert "35 fields" in str(caught.value)


@pytest.mark.parametrize(
    ("header", "why"),
    [
        (LEGACY_HEADER, "the pre-cutover legacy format belongs to M1.4"),
        ("TradDt,BizDt,Sgmt", "a header cut short"),
        (
            HEADER.replace("TtlTradgVol", "Volume"),
            "a single renamed column is still a format break",
        ),
    ],
)
def test_a_header_from_another_format_is_refused(header: str, why: str) -> None:
    """A parser reading these would produce rows with the wrong identity or none (invariant #2)."""
    with pytest.raises(ParseError) as caught:
        parse(f"{header}\n".encode(), filename="other_era.csv")

    assert caught.value.filename == "other_era.csv", why
    assert caught.value.line == 1
    assert "header" in str(caught.value)


def test_an_fo_shaped_row_is_refused_though_the_header_matches() -> None:
    """The F&O bhavcopy shares this exact header; its rows (FO/FUTSTK) must not read as equities."""
    future = a_valid_record(Sgmt="FO", FinInstrmTp="FUTSTK", XpryDt="2024-07-25", StrkPric="0")
    with pytest.raises(ParseError) as caught:
        parse(a_file(future), filename="fo.csv")

    assert caught.value.line == 2
    assert "FinInstrmTp" in str(caught.value)


def test_an_equity_row_carrying_a_derivative_column_is_refused() -> None:
    """A STK row with an expiry or strike is a contradiction — not the cash file it claims to be."""
    for column, value in [("XpryDt", "2024-07-25"), ("StrkPric", "3200"), ("OptnTp", "CE")]:
        with pytest.raises(ParseError) as caught:
            parse(a_file(a_valid_record(**{column: value})), filename="contradiction.csv")
        assert caught.value.line == 2
        assert column in str(caught.value)


@pytest.mark.parametrize(
    ("overrides", "needle"),
    [
        ({"ISIN": "NOTANISIN"}, "isin"),
        ({"OpnPric": "one"}, "OpnPric"),
        ({"TtlTradgVol": "10.5"}, "TtlTradgVol"),
        ({"TtlNbOfTxsExctd": "three"}, "TtlNbOfTxsExctd"),
        ({"TradDt": "08-07-2024"}, "TradDt"),
        ({"TradDt": "2024-13-08"}, "TradDt"),
        ({"OpnPric": "-1"}, "OpnPric"),
        ({"OpnPric": "NaN"}, "OpnPric"),
        ({"OpnPric": "Infinity"}, "OpnPric"),
        ({"TckrSymb": ""}, "symbol"),
        ({"ClsPric": ""}, "ClsPric"),
    ],
)
def test_a_bad_field_names_the_column_the_file_and_the_line(
    overrides: dict[str, str], needle: str
) -> None:
    """Each is a value that must never become a price, a count or an identity."""
    body = a_file(a_valid_record(), a_valid_record(**overrides))
    with pytest.raises(ParseError) as caught:
        parse(body, filename="corrupt.csv")

    assert caught.value.filename == "corrupt.csv"
    assert caught.value.line == 3
    assert "corrupt.csv:3" in str(caught.value)
    assert needle in str(caught.value)


def test_an_empty_file_and_a_header_only_file_are_both_refused() -> None:
    """Both mean "the fetch worked and there is no data" — never a quiet zero rows."""
    with pytest.raises(ParseError, match=r"empty\.csv"):
        parse(b"", filename="empty.csv")

    with pytest.raises(ParseError, match="no data rows"):
        parse(f"{HEADER}\n".encode(), filename="header_only.csv")


def test_a_file_spanning_two_sessions_is_refused() -> None:
    """One bhavcopy is one session; two dates would be split across L1 partitions unnoticed."""
    body = a_file(a_valid_record(TradDt="2024-07-08"), a_valid_record(TradDt="2024-07-09"))
    with pytest.raises(ParseError, match="more than one session"):
        parse(body, filename="two_days.csv")


def test_an_archive_with_more_than_one_member_is_refused() -> None:
    """Picking "the first CSV" would turn a format change into a day of quietly wrong data."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("a.csv", HEADER)
        archive.writestr("b.csv", HEADER)

    with pytest.raises(ParseError, match="exactly one member"):
        parse(buffer.getvalue(), filename="two_members.csv.zip")


def test_non_utf8_bytes_are_refused() -> None:
    """A binary body served with a 200 is a real failure mode, not text to muddle through."""
    with pytest.raises(ParseError, match="not UTF-8"):
        parse(b"\xff\xfe\x00binary", filename="binary.csv")


# ── the pipeline entry point: bytes come back out of L0, verified ─────────────────────────────


def test_parse_l0_reads_the_payload_back_through_the_store(tmp_path: Path) -> None:
    """The real path: a fetch returns an `L0Ref`, and the parser reads it back re-checksummed."""
    fixture = FIXTURE_FILES[0]
    store = L0Store(clock=FrozenClock(datetime(2024, 7, 8, 19, 30, tzinfo=IST)), data_root=tmp_path)
    ref = store.put(
        UDIFF_SOURCE_ID,
        fixture.trade_date,
        fixture.filename,
        payload_of(fixture),
        content_type="application/zip",
    )

    rows = parse_l0(store, ref)

    assert rows == parse(payload_of(fixture), filename=fixture.filename)
    assert len(rows) == fixture.data_rows


def test_dispatch_parse_l0_chooses_the_parser_from_the_refs_date(tmp_path: Path) -> None:
    """`bhavcopy.parse_l0` needs no era hint: the ref's logical date selects the parser."""
    fixture = FIXTURE_FILES[0]
    store = L0Store(clock=FrozenClock(datetime(2024, 7, 8, 19, 30, tzinfo=IST)), data_root=tmp_path)
    ref = store.put(UDIFF_SOURCE_ID, fixture.trade_date, fixture.filename, payload_of(fixture))

    rows = bhavcopy.parse_l0(store, ref)

    assert rows == parse(payload_of(fixture), filename=fixture.filename)


def test_a_corrupted_l0_payload_never_becomes_rows(tmp_path: Path) -> None:
    """Damage under L0's feet must stop at the store, not arrive as plausible prices."""
    fixture = FIXTURE_FILES[0]
    store = L0Store(clock=FrozenClock(datetime(2024, 7, 8, 19, 30, tzinfo=IST)), data_root=tmp_path)
    ref = store.put(UDIFF_SOURCE_ID, fixture.trade_date, fixture.filename, payload_of(fixture))

    path = store.path_of(ref)
    path.chmod(0o644)
    path.write_bytes(b"PK\x03\x04 not the bytes that were checksummed")

    with pytest.raises(Exception, match="hashes to"):
        parse_l0(store, ref)
