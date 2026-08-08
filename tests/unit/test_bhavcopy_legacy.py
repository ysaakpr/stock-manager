"""The legacy NSE bhavcopy parser, checked against real exchange files (M1.4).

Three things have to be true of a D1 parser, and none of them is provable against a hand-written
sample:

* **It reads what the exchange actually published.** So the fixtures are real archive files
  (`PROVENANCE.md`), fetched through the M1.2 crawl engine, spanning the era from 2016 to the last
  session before the 08-Jul-2024 UDiFF cutover — and the row counts and a hand-checked row per
  file are asserted against the raw CSV, recounted here independently of the parser.
* **It emits one schema.** Every fixture yields the same `PriceRow` fields with the same types, so
  M1.5 can converge on it and nothing downstream branches on era.
* **It fails loudly.** A truncated download, a header from a different era, a field that is not a
  number: each must name the file and, where a single record is at fault, the line. Silence on any
  of these is how a backfill produces a decade of quietly wrong rows.

Offline and deterministic (B8): every byte read here comes from `tests/fixtures/`.
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
from dataplatform.ingest.nse.bhavcopy_legacy import (
    LEGACY_COLUMNS,
    LEGACY_ERA_END,
    LEGACY_SOURCE_ID,
    parse,
    parse_l0,
)
from dataplatform.store import L0Store

FIXTURES: Final = Path(__file__).resolve().parents[1] / "fixtures" / "nse_bhavcopy" / "legacy"

#: The era's header line, verbatim including the trailing comma, for building malformed inputs.
HEADER: Final = ",".join(LEGACY_COLUMNS) + ","

#: The pre-ISIN sub-era of the same archive (`cm04JAN2010bhav.csv`) — eleven columns, no identity.
PRE_ISIN_HEADER: Final = (
    "SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,"
)

#: The format on the other side of the cutover, which belongs to M1.5 and must not be read here.
UDIFF_HEADER: Final = "TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs"


class Fixture(NamedTuple):
    """One frozen archive file and the facts about it that were checked by hand against the CSV."""

    filename: str
    trade_date: date
    data_rows: int
    sample: PriceRow


#: RELIANCE on each session, transcribed field by field from the raw CSV line (see the test that
#: re-reads that line): the "hand-checked sample row" of the acceptance criteria.
FIXTURE_FILES: Final = (
    Fixture(
        filename="cm01JAN2016bhav.csv.zip",
        trade_date=date(2016, 1, 1),
        data_rows=1607,
        sample=PriceRow(
            isin="INE002A01018",
            symbol="RELIANCE",
            series="EQ",
            trade_date=date(2016, 1, 1),
            open=Decimal("1009.8"),
            high=Decimal("1018.9"),
            low=Decimal("1008.2"),
            close=Decimal("1015.35"),
            last=Decimal("1013.6"),
            prev_close=Decimal("1014.6"),
            total_traded_qty=1238135,
            total_traded_value=Decimal("1257352825.3"),
            total_trades=30701,
        ),
    ),
    Fixture(
        filename="cm23MAR2020bhav.csv.zip",
        trade_date=date(2020, 3, 23),
        data_rows=1965,
        sample=PriceRow(
            isin="INE002A01018",
            symbol="RELIANCE",
            series="EQ",
            trade_date=date(2020, 3, 23),
            open=Decimal("916.2"),
            high=Decimal("950"),
            low=Decimal("875.65"),
            close=Decimal("884.05"),
            last=Decimal("891"),
            prev_close=Decimal("1017.95"),
            total_traded_qty=18593713,
            total_traded_value=Decimal("16721615106.25"),
            total_trades=569206,
        ),
    ),
    Fixture(
        filename="cm05JUL2024bhav.csv.zip",
        trade_date=date(2024, 7, 5),
        data_rows=2775,
        sample=PriceRow(
            isin="INE002A01018",
            symbol="RELIANCE",
            series="EQ",
            trade_date=date(2024, 7, 5),
            open=Decimal("3107.65"),
            high=Decimal("3197"),
            low=Decimal("3096"),
            close=Decimal("3177.25"),
            last=Decimal("3189.9"),
            prev_close=Decimal("3108.05"),
            total_traded_qty=6134855,
            total_traded_value=Decimal("19323716027.9"),
            total_trades=261494,
        ),
    ),
)

PRICE_FIELDS: Final = ("open", "high", "low", "close", "last", "prev_close", "total_traded_value")
COUNT_FIELDS: Final = ("total_traded_qty", "total_trades")


def payload_of(fixture: Fixture) -> bytes:
    """The frozen archive exactly as the exchange served it."""
    return (FIXTURES / fixture.filename).read_bytes()


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


@pytest.fixture(params=FIXTURE_FILES, ids=lambda fixture: fixture.filename)
def era_file(request: pytest.FixtureRequest) -> Fixture:
    """Each frozen file of the era in turn."""
    fixture: Fixture = request.param
    return fixture


# ── the fixture set itself ───────────────────────────────────────────────────────────────────


def test_the_frozen_set_really_spans_the_era() -> None:
    """Three or more real files, early to the cutover — the premise the schema test rests on.

    Asserted rather than assumed so that shrinking the fixture set to make something pass shows up
    as a failing test (AGENTIC_CONTEXT §7).
    """
    on_disk = sorted(path.name for path in FIXTURES.glob("*.zip"))
    assert on_disk == sorted(fixture.filename for fixture in FIXTURE_FILES)
    assert len(FIXTURE_FILES) >= 3

    sessions = sorted(fixture.trade_date for fixture in FIXTURE_FILES)
    assert sessions[0].year <= 2016, "the earliest fixture must be early in the era"
    assert sessions[-1] < LEGACY_ERA_END, "the latest fixture must precede the UDiFF cutover"
    assert (LEGACY_ERA_END - sessions[-1]).days <= 7, "and must be the run-up to it"
    assert len(set(sessions)) == len(sessions)


def test_the_package_exports_the_parser() -> None:
    """`dataplatform.ingest.nse` is the surface other packages reach this parser through."""
    assert nse.parse_legacy_bhavcopy is parse
    assert nse.parse_legacy_bhavcopy_l0 is parse_l0
    assert nse.LEGACY_SOURCE_ID == LEGACY_SOURCE_ID == "nse_bhavcopy_legacy"


# ── acceptance 1: one schema, Decimal prices, across every file of the era ───────────────────


def test_every_fixture_parses_to_the_identical_row_schema(era_file: Fixture) -> None:
    """Same fields, same types, whichever year the file is from."""
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


def test_prices_are_decimal_and_never_float(era_file: Fixture) -> None:
    """Invariant the whole cost model rests on: no float ever enters a price field."""
    rows = parse(payload_of(era_file), filename=era_file.filename)

    for row in rows:
        for field in PRICE_FIELDS:
            value = getattr(row, field)
            # Decimal and float are disjoint types, so this single check is the whole guarantee.
            assert isinstance(value, Decimal), f"{row.symbol}.{field} is {type(value).__name__}"
        for field in COUNT_FIELDS:
            value = getattr(row, field)
            assert isinstance(value, int) and not isinstance(value, bool)


def test_the_row_model_carries_no_adjusted_price() -> None:
    """Invariant #3: L1 holds raw traded prices, so there is nowhere to put an adjusted one."""
    assert not [name for name in PriceRow.model_fields if "adj" in name.lower()]


def test_a_float_cannot_be_constructed_into_a_price_row() -> None:
    """The schema refuses the mistake rather than trusting every future parser to avoid it."""
    fields = FIXTURE_FILES[0].sample.model_dump()
    assert PriceRow(**fields) == FIXTURE_FILES[0].sample

    with pytest.raises(ValidationError, match="Decimal"):
        PriceRow(**{**fields, "open": 1009.8})  # a float where a Decimal is required
    with pytest.raises(ValidationError, match="Decimal"):
        PriceRow(**{**fields, "close": "1015.35"})  # and a string is not a shortcut either


# ── acceptance 2: counts and a hand-checked row match the raw CSV exactly ────────────────────


def test_row_count_matches_the_raw_csv(era_file: Fixture) -> None:
    """Counted twice: once by the parser, once by the stdlib csv reader over the same member."""
    rows = parse(payload_of(era_file), filename=era_file.filename)

    assert len(rows) == len(raw_records_of(era_file))
    assert len(rows) == era_file.data_rows, "the count transcribed into PROVENANCE.md"


def test_hand_checked_sample_row_matches_the_raw_csv(era_file: Fixture) -> None:
    """Every field of one row, against both the literal transcription and the raw CSV line."""
    rows = parse(payload_of(era_file), filename=era_file.filename)
    expected = era_file.sample

    parsed = [
        row for row in rows if row.symbol == expected.symbol and row.series == expected.series
    ]
    assert len(parsed) == 1
    assert parsed[0] == expected

    raw = [
        record
        for record in raw_records_of(era_file)
        if record[0] == expected.symbol and record[1] == expected.series
    ]
    assert len(raw) == 1
    fields = dict(zip(LEGACY_COLUMNS, raw[0], strict=False))
    assert fields["ISIN"] == expected.isin
    assert Decimal(fields["OPEN"]) == expected.open
    assert Decimal(fields["HIGH"]) == expected.high
    assert Decimal(fields["LOW"]) == expected.low
    assert Decimal(fields["CLOSE"]) == expected.close
    assert Decimal(fields["LAST"]) == expected.last
    assert Decimal(fields["PREVCLOSE"]) == expected.prev_close
    assert int(fields["TOTTRDQTY"]) == expected.total_traded_qty
    assert Decimal(fields["TOTTRDVAL"]) == expected.total_traded_value
    assert int(fields["TOTALTRADES"]) == expected.total_trades


def test_every_field_of_every_row_matches_the_raw_csv(era_file: Fixture) -> None:
    """The sample row generalised: the parser is a faithful transcription, row for row.

    Cheap enough to run over the whole file, and it is what makes "row counts and a sample match"
    a statement about the parser rather than about one lucky line.
    """
    rows = parse(payload_of(era_file), filename=era_file.filename)

    for row, record in zip(rows, raw_records_of(era_file), strict=True):
        assert (row.symbol, row.series, row.isin) == (record[0], record[1], record[12])
        assert row.open == Decimal(record[2])
        assert row.close == Decimal(record[5])
        assert row.total_traded_qty == int(record[8])
        assert row.total_traded_value == Decimal(record[9])
        assert row.total_trades == int(record[11])


def test_decimal_conversion_is_exact_not_binary() -> None:
    """`1257352825.3` is not representable in binary floating point; the parsed value must be it."""
    fixture = FIXTURE_FILES[0]
    rows = parse(payload_of(fixture), filename=fixture.filename)
    (reliance,) = [row for row in rows if row.symbol == "RELIANCE" and row.series == "EQ"]

    assert reliance.total_traded_value == Decimal("1257352825.3")
    assert str(reliance.total_traded_value) == "1257352825.3"
    # What a float round-trip would have cost, had the parser used float() on the field.
    assert Decimal(float(reliance.total_traded_value)) != reliance.total_traded_value


# ── SERIES is kept: filtering is a query concern, not a parser's ─────────────────────────────


def test_non_eq_series_survive_the_parser(era_file: Fixture) -> None:
    """Every series the exchange published is present, in the count the raw file has."""
    rows = parse(payload_of(era_file), filename=era_file.filename)
    records = raw_records_of(era_file)

    assert {row.series for row in rows} == {record[1] for record in records}
    assert {"EQ", "BE"} <= {row.series for row in rows}
    assert len({row.series for row in rows}) > 5, "the era's long tail of debt/odd-lot series"
    assert sum(1 for row in rows if row.series != "EQ") == sum(
        1 for record in records if record[1] != "EQ"
    )


def test_rows_keep_the_exchange_file_order(era_file: Fixture) -> None:
    """Deterministic output: re-parsing an L0 payload must reproduce the same sequence."""
    payload = payload_of(era_file)
    first = parse(payload, filename=era_file.filename)
    assert first == parse(payload, filename=era_file.filename)
    assert [row.symbol for row in first] == [record[0] for record in raw_records_of(era_file)]


# ── acceptance 3: malformed input raises a specific error naming the file and line ───────────


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
    fixture = FIXTURE_FILES[0]
    keep = raw_csv_of(fixture).splitlines()[:5]
    body = "\n".join([*keep, "ZZTRUNCATED,EQ,10,11"])

    with pytest.raises(ParseError) as caught:
        parse(body.encode("utf-8"), filename="cm01JAN2016bhav.csv")

    assert caught.value.filename == "cm01JAN2016bhav.csv"
    assert caught.value.line == 6, "header is line 1, so the short record is line 6"
    assert "cm01JAN2016bhav.csv:6" in str(caught.value)
    assert "4 fields" in str(caught.value)


def test_a_row_wider_than_the_header_is_refused() -> None:
    """The other half of the width check: an extra field is a format change, not a curiosity."""
    body = f"{HEADER}\nX,EQ,1,2,1,2,2,1,10,20,01-JAN-2016,3,INE002A01018,extra\n"
    with pytest.raises(ParseError) as caught:
        parse(body.encode("utf-8"), filename="wide.csv")

    assert caught.value.line == 2


@pytest.mark.parametrize(
    ("header", "why"),
    [
        (PRE_ISIN_HEADER, "the pre-2011 sub-era carries no ISIN"),
        (UDIFF_HEADER, "the post-cutover format belongs to M1.5"),
        ("SYMBOL,SERIES,OPEN", "a header cut short"),
    ],
)
def test_a_header_from_another_format_is_refused(header: str, why: str) -> None:
    """A parser that read these would produce rows with no identity, or nonsense (invariant #2)."""
    with pytest.raises(ParseError) as caught:
        parse(f"{header}\n".encode(), filename="other_era.csv")

    assert caught.value.filename == "other_era.csv", why
    assert caught.value.line == 1
    assert "header" in str(caught.value)


@pytest.mark.parametrize(
    ("bad", "column"),
    [
        ("X,EQ,1,2,1,2,2,1,10,20,01-JAN-2016,3,NOTANISIN,", "isin"),
        ("X,EQ,one,2,1,2,2,1,10,20,01-JAN-2016,3,INE002A01018,", "OPEN"),
        ("X,EQ,1,2,1,2,2,1,10.5,20,01-JAN-2016,3,INE002A01018,", "TOTTRDQTY"),
        ("X,EQ,1,2,1,2,2,1,10,20,01-JAN-2016,three,INE002A01018,", "TOTALTRADES"),
        ("X,EQ,1,2,1,2,2,1,10,20,32-JAN-2016,3,INE002A01018,", "TIMESTAMP"),
        ("X,EQ,1,2,1,2,2,1,10,20,01-XXX-2016,3,INE002A01018,", "TIMESTAMP"),
        ("X,EQ,-1,2,1,2,2,1,10,20,01-JAN-2016,3,INE002A01018,", "OPEN"),
        ("X,EQ,NaN,2,1,2,2,1,10,20,01-JAN-2016,3,INE002A01018,", "OPEN"),
        ("X,EQ,Infinity,2,1,2,2,1,10,20,01-JAN-2016,3,INE002A01018,", "OPEN"),
        (",EQ,1,2,1,2,2,1,10,20,01-JAN-2016,3,INE002A01018,", "symbol"),
    ],
)
def test_a_bad_field_names_the_column_the_file_and_the_line(bad: str, column: str) -> None:
    """Each of these is a value that must never become a price, a count or an identity."""
    body = f"{HEADER}\nX,EQ,1,2,1,2,2,1,10,20,01-JAN-2016,3,INE002A01018,\n{bad}\n"
    with pytest.raises(ParseError) as caught:
        parse(body.encode("utf-8"), filename="corrupt.csv")

    assert caught.value.filename == "corrupt.csv"
    assert caught.value.line == 3
    assert "corrupt.csv:3" in str(caught.value)
    assert column in str(caught.value)


def test_an_empty_file_and_a_header_only_file_are_both_refused() -> None:
    """Both mean "the fetch worked and there is no data" — never a quiet zero rows."""
    with pytest.raises(ParseError, match=r"empty\.csv"):
        parse(b"", filename="empty.csv")

    with pytest.raises(ParseError, match="no data rows"):
        parse(f"{HEADER}\n".encode(), filename="header_only.csv")


def test_a_file_spanning_two_sessions_is_refused() -> None:
    """One bhavcopy is one session; two dates would be split across L1 partitions unnoticed."""
    body = (
        f"{HEADER}\n"
        "X,EQ,1,2,1,2,2,1,10,20,01-JAN-2016,3,INE002A01018,\n"
        "Y,EQ,1,2,1,2,2,1,10,20,04-JAN-2016,3,INE002A01018,\n"
    )
    with pytest.raises(ParseError, match="more than one session"):
        parse(body.encode("utf-8"), filename="two_days.csv")


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


# ── the pipeline entry point: bytes come back out of L0, verified ────────────────────────────


def test_parse_l0_reads_the_payload_back_through_the_store(tmp_path: Path) -> None:
    """The real path: a fetch returns an `L0Ref`, and the parser reads it back re-checksummed."""
    fixture = FIXTURE_FILES[2]
    store = L0Store(clock=FrozenClock(datetime(2024, 7, 5, 19, 30, tzinfo=IST)), data_root=tmp_path)
    ref = store.put(
        LEGACY_SOURCE_ID,
        fixture.trade_date,
        fixture.filename,
        payload_of(fixture),
        content_type="application/zip",
    )

    rows = parse_l0(store, ref)

    assert rows == parse(payload_of(fixture), filename=fixture.filename)
    assert len(rows) == fixture.data_rows


def test_a_corrupted_l0_payload_never_becomes_rows(tmp_path: Path) -> None:
    """Damage under L0's feet must stop at the store, not arrive as plausible prices."""
    fixture = FIXTURE_FILES[0]
    store = L0Store(clock=FrozenClock(datetime(2016, 1, 1, 19, 30, tzinfo=IST)), data_root=tmp_path)
    ref = store.put(LEGACY_SOURCE_ID, fixture.trade_date, fixture.filename, payload_of(fixture))

    path = store.path_of(ref)
    path.chmod(0o644)
    path.write_bytes(b"PK\x03\x04 not the bytes that were checksummed")

    with pytest.raises(Exception, match="hashes to"):
        parse_l0(store, ref)
