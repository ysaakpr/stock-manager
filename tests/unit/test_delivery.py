"""The NSE `sec_bhavdata_full` delivery parser, checked against real exchange files (M1.6).

Three things have to be true of this parser, and the acceptance criteria name them:

* **`-` becomes `None`, distinguishable from `0`.** The trade-to-trade series publish `-` for
  delivery, and a parser that read it as `0` would hand T0 monitoring a stream of spurious
  "0 % delivered" spikes. So the real fixtures include a `BE` row whose delivery is `-`, and a
  crafted row proves `0` and `-` land as different values.
* **The leading-space column names are handled by name, not by position.** The fixtures carry the
  exchange's real header (` SERIES`, ` DELIV_QTY`, …); a reordered or renamed column is refused
  on line 1 rather than read out of the wrong slot.
* **The join to an ISIN goes through the D2 identity master, keyed on `(symbol, date)`.** The file
  has no ISIN; `resolve` is the only way one appears, and it never resolves a symbol by name alone.

Offline and deterministic: every byte read here comes from `tests/fixtures/`.
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, NamedTuple

import pytest
from pydantic import ValidationError

from dataplatform.clock import IST, FrozenClock
from dataplatform.identity.master import (
    Exchange,
    IdentityMaster,
    SymbolWindow,
)
from dataplatform.ingest import nse
from dataplatform.ingest.models import ParseError
from dataplatform.ingest.nse.delivery import (
    DELIVERY_COLUMNS,
    DELIVERY_SOURCE_ID,
    DeliveryRow,
    ResolvedDeliveryRow,
    parse,
    parse_l0,
    resolve,
)
from dataplatform.store import L0Store

FIXTURES: Final = Path(__file__).resolve().parents[1] / "fixtures" / "nse_delivery"

#: The header line the exchange actually writes, verbatim — a leading space in every name after
#: the first — for building malformed inputs the way the file really looks.
RAW_HEADER: Final = ", ".join(DELIVERY_COLUMNS)

#: A well-formed data line (EQ, delivery present) for building single-row malformed inputs on top
#: of a valid header. Values carry the file's leading space after each comma.
GOOD_ROW: Final = (
    "RELIANCE, EQ, 07-Aug-2026, 1325.00, 1320.00, 1337.00, 1316.60, 1334.80, "
    "1334.80, 1329.09, 9885638, 131388.79, 161575, 5187857, 52.48"
)


class Sample(NamedTuple):
    """A hand-checked row transcribed field by field from the raw CSV."""

    symbol: str
    series: str
    deliv_qty: int | None
    deliv_pct: Decimal | None


class Fixture(NamedTuple):
    """One frozen file and the facts checked by hand against its CSV (see PROVENANCE.md)."""

    filename: str
    trade_date: date
    data_rows: int
    absent_rows: int
    present: Sample
    absent: Sample


FIXTURE_FILES: Final = (
    Fixture(
        filename="sec_bhavdata_full_07082026.csv",
        trade_date=date(2026, 8, 7),
        data_rows=3299,
        absent_rows=318,
        present=Sample("RELIANCE", "EQ", 5187857, Decimal("52.48")),
        absent=Sample("AAREYDRUGS", "BE", None, None),
    ),
    Fixture(
        filename="sec_bhavdata_full_06082026.csv",
        trade_date=date(2026, 8, 6),
        data_rows=3287,
        absent_rows=317,
        present=Sample("RELIANCE", "EQ", 10551876, Decimal("51.87")),
        absent=Sample("AAREYDRUGS", "BE", None, None),
    ),
)


def payload_of(fixture: Fixture) -> bytes:
    """The frozen CSV exactly as the exchange served it."""
    return (FIXTURES / fixture.filename).read_bytes()


def raw_records_of(fixture: Fixture) -> list[list[str]]:
    """Every data record of the raw CSV, read with the stdlib and stripped, header dropped.

    `skipinitialspace=True` mirrors what the parser must cope with: the leading space after every
    comma, in both the header and the values.
    """
    reader = csv.reader(io.StringIO(payload_of(fixture).decode("utf-8")), skipinitialspace=True)
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


#: The counter-example fixture: a real payload the archive served for the *wrong* session. Held
#: apart from the frozen session set because it is not a well-formed example of the source — it is
#: the evidence for a guard, and folding it into the happy-path set would have it asserted against
#: as though its contents described the day its name claims.
MISDATED: Final = "sec_bhavdata_full_30092019_MISDATED.csv"

#: The other counter-example: the one payload in 1,712 that arrived as an XLSX workbook under a
#: `.csv` name with `Content-Type: text/csv`. Held apart for the same reason as the misdated one —
#: it is not a well-formed example of the source, it is the evidence that the decoder rescues a
#: container the source is not supposed to ship.
XLSX_ERA: Final = "sec_bhavdata_full_08082022_XLSX.csv"


def test_the_frozen_set_is_two_real_files_one_with_dashes() -> None:
    """2+ real fixtures including one with `-` values — the premise the rest of the file rests on.

    Asserted rather than assumed so that shrinking the fixture set to dodge a failure shows up as
    a failing test (AGENTIC_CONTEXT §7). The misdated counter-example is excluded by name and
    asserted separately, so neither set can be quietly emptied.
    """
    counter_examples = {MISDATED, XLSX_ERA}
    on_disk = sorted(p.name for p in FIXTURES.glob("*.csv") if p.name not in counter_examples)
    assert on_disk == sorted(fixture.filename for fixture in FIXTURE_FILES)
    assert (FIXTURES / MISDATED).is_file(), "the misdated payload is a guard's only evidence"
    assert (FIXTURES / XLSX_ERA).is_file(), "the xlsx payload is the decoder rescue's only evidence"
    assert len(FIXTURE_FILES) >= 2

    for fixture in FIXTURE_FILES:
        assert fixture.absent_rows > 0, "each frozen file must actually contain '-' delivery rows"
    assert len({fixture.trade_date for fixture in FIXTURE_FILES}) == len(FIXTURE_FILES)


def test_the_package_exports_the_parser() -> None:
    """`dataplatform.ingest.nse` is the surface other packages reach this parser through."""
    assert nse.parse_delivery is parse
    assert nse.parse_delivery_l0 is parse_l0
    assert nse.resolve_delivery is resolve
    assert nse.DELIVERY_SOURCE_ID == DELIVERY_SOURCE_ID == "nse_sec_bhavdata_full"


# ── acceptance 1: delivery parses; '-' becomes None, distinct from 0 ─────────────────────────


def test_every_fixture_parses_to_the_delivery_schema(real_file: Fixture) -> None:
    """Same fields, same types, whichever session the file is from."""
    rows = parse(payload_of(real_file), filename=real_file.filename)

    assert rows, "a session's delivery file is never empty"
    assert {type(row) for row in rows} == {DeliveryRow}
    assert set(DeliveryRow.model_fields) == {
        "symbol",
        "series",
        "trade_date",
        "deliv_qty",
        "deliv_pct",
    }
    assert {row.trade_date for row in rows} == {real_file.trade_date}


def test_row_count_matches_the_raw_csv(real_file: Fixture) -> None:
    """Counted twice: once by the parser, once by the stdlib reader over the same bytes."""
    rows = parse(payload_of(real_file), filename=real_file.filename)

    assert len(rows) == len(raw_records_of(real_file))
    assert len(rows) == real_file.data_rows, "the count transcribed into PROVENANCE.md"


def test_a_present_delivery_row_parses_exactly(real_file: Fixture) -> None:
    """The hand-checked EQ row: delivery quantity as an int, percentage as an exact Decimal."""
    rows = parse(payload_of(real_file), filename=real_file.filename)
    want = real_file.present

    matched = [r for r in rows if r.symbol == want.symbol and r.series == want.series]
    assert len(matched) == 1
    row = matched[0]
    assert row.deliv_qty == want.deliv_qty
    assert isinstance(row.deliv_qty, int) and not isinstance(row.deliv_qty, bool)
    assert row.deliv_pct == want.deliv_pct
    assert isinstance(row.deliv_pct, Decimal)


def test_a_dash_delivery_row_becomes_none_not_zero(real_file: Fixture) -> None:
    """The whole point of the parser: `-` is absence (`None`), never a delivered quantity of 0."""
    rows = parse(payload_of(real_file), filename=real_file.filename)
    want = real_file.absent

    matched = [r for r in rows if r.symbol == want.symbol and r.series == want.series]
    assert len(matched) == 1
    row = matched[0]
    assert row.deliv_qty is None
    assert row.deliv_pct is None
    # None is the fact "not stated"; a stated zero would compare equal to 0 and pass a truthiness
    # check differently — these assertions fail if '-' were ever parsed as 0.
    assert row.deliv_qty != 0
    assert row.deliv_pct != Decimal(0)


def test_dash_row_count_matches_the_raw_csv(real_file: Fixture) -> None:
    """Every `-` in the file becomes a `None`, in the count the raw file has."""
    rows = parse(payload_of(real_file), filename=real_file.filename)
    raw = raw_records_of(real_file)

    qty_i = DELIVERY_COLUMNS.index("DELIV_QTY")
    pct_i = DELIVERY_COLUMNS.index("DELIV_PER")
    raw_absent = sum(1 for record in raw if record[qty_i] == "-" or record[pct_i] == "-")
    parsed_absent = sum(1 for row in rows if row.deliv_qty is None or row.deliv_pct is None)

    assert parsed_absent == raw_absent == real_file.absent_rows


def test_every_delivery_field_matches_the_raw_csv(real_file: Fixture) -> None:
    """The samples generalised: a faithful transcription, row for row, dashes included."""
    rows = parse(payload_of(real_file), filename=real_file.filename)
    qty_i = DELIVERY_COLUMNS.index("DELIV_QTY")
    pct_i = DELIVERY_COLUMNS.index("DELIV_PER")

    for row, record in zip(rows, raw_records_of(real_file), strict=True):
        assert (row.symbol, row.series) == (record[0], record[1])
        raw_qty, raw_pct = record[qty_i], record[pct_i]
        assert row.deliv_qty == (None if raw_qty == "-" else int(raw_qty))
        assert row.deliv_pct == (None if raw_pct == "-" else Decimal(raw_pct))


def test_zero_and_dash_are_different_parsed_values() -> None:
    """A crafted session: a real 0 delivery and a '-' delivery must not collapse together."""
    body = (
        f"{RAW_HEADER}\n"
        "ZERODEL, EQ, 07-Aug-2026, 1, 1, 1, 1, 1, 1, 1, 10, 0.01, 2, 0, 0.00\n"
        "DASHDEL, BE, 07-Aug-2026, 1, 1, 1, 1, 1, 1, 1, 10, 0.01, 2, -, -\n"
    )
    rows = parse(body.encode("utf-8"), filename="crafted.csv")
    by_symbol = {row.symbol: row for row in rows}

    assert by_symbol["ZERODEL"].deliv_qty == 0
    assert by_symbol["ZERODEL"].deliv_pct == Decimal("0.00")
    assert by_symbol["DASHDEL"].deliv_qty is None
    assert by_symbol["DASHDEL"].deliv_pct is None


def test_delivery_percent_is_decimal_and_exact_not_binary() -> None:
    """`52.48` must survive as itself, not the nearest binary float."""
    fixture = FIXTURE_FILES[0]
    rows = parse(payload_of(fixture), filename=fixture.filename)
    (reliance,) = [r for r in rows if r.symbol == "RELIANCE" and r.series == "EQ"]

    assert reliance.deliv_pct == Decimal("52.48")
    assert str(reliance.deliv_pct) == "52.48"
    assert reliance.deliv_pct is not None
    assert Decimal(float(reliance.deliv_pct)) != reliance.deliv_pct


def test_a_float_cannot_be_constructed_into_a_delivery_row() -> None:
    """The schema refuses the mistake rather than trusting every future caller to avoid it."""
    base: dict[str, Any] = {
        "symbol": "X",
        "series": "EQ",
        "trade_date": date(2026, 8, 7),
        "deliv_qty": 1,
        "deliv_pct": None,
    }
    assert DeliveryRow(**base).deliv_qty == 1

    with pytest.raises(ValidationError):
        DeliveryRow(**{**base, "deliv_pct": 52.48})  # a float where a Decimal|None is required
    with pytest.raises(ValidationError):
        DeliveryRow(**{**base, "deliv_qty": -1})  # delivery is never negative


# ── acceptance 2: leading-space column names handled without positional indexing ─────────────


def test_the_real_header_carries_leading_spaces_the_parser_strips() -> None:
    """Documents what the fixtures contain: the exchange's spaced header is what is parsed."""
    header = payload_of(FIXTURE_FILES[0]).decode("utf-8").splitlines()[0]
    raw_names = header.split(",")
    assert raw_names[0] == "SYMBOL"
    # Every name after the first is written with a leading space in the real file.
    assert all(name.startswith(" ") for name in raw_names[1:])
    assert [name.strip() for name in raw_names] == list(DELIVERY_COLUMNS)
    # Parsing still succeeds despite the spaces — the names are stripped, not indexed blindly.
    assert parse(payload_of(FIXTURE_FILES[0]), filename=FIXTURE_FILES[0].filename)


def test_a_reordered_column_is_refused_not_read_from_the_wrong_slot() -> None:
    """If DELIV_QTY and DELIV_PER swap places, positional indexing would read each as the other."""
    swapped = list(DELIVERY_COLUMNS)
    swapped[-2], swapped[-1] = swapped[-1], swapped[-2]
    header = ", ".join(swapped)
    with pytest.raises(ParseError) as caught:
        parse(f"{header}\n{GOOD_ROW}\n".encode(), filename="reordered.csv")

    assert caught.value.line == 1
    assert "header" in str(caught.value)


@pytest.mark.parametrize(
    ("header", "why"),
    [
        ("SYMBOL, SERIES, DATE1", "a header cut short"),
        (RAW_HEADER + ", EXTRA", "an added column"),
        (
            "SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,TOTALTRADES,ISIN",
            "the bhavcopy header, not this file",
        ),
    ],
)
def test_a_header_from_another_shape_is_refused(header: str, why: str) -> None:
    """A parser that read these would address delivery out of the wrong column."""
    with pytest.raises(ParseError) as caught:
        parse(f"{header}\n".encode(), filename="other.csv")

    assert caught.value.filename == "other.csv", why
    assert caught.value.line == 1
    assert "header" in str(caught.value)


# ── acceptance 3: join key is (symbol, series, date) → ISIN via the identity master ──────────


def _master() -> IdentityMaster:
    """A tiny identity master: RELIANCE stable, and a recycled symbol that resolves by date."""
    windows = (
        SymbolWindow(
            exchange=Exchange.NSE,
            symbol="RELIANCE",
            valid_from=date(2000, 1, 1),
            valid_to=None,
            isin="INE002A01018",
        ),
        # A symbol that named one ISIN before a cutover date and another after it — the exact
        # reason a delivery row must be resolved with its own trade date, not by symbol alone.
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
    return IdentityMaster(windows)


def test_resolve_maps_symbol_and_date_to_isin() -> None:
    """A delivery row becomes ISIN-keyed only through the master; the ISIN is a fact of the date."""
    row = DeliveryRow(
        symbol="RELIANCE",
        series="EQ",
        trade_date=date(2026, 8, 7),
        deliv_qty=5187857,
        deliv_pct=Decimal("52.48"),
    )
    result = resolve([row], _master())

    assert result.unresolved == ()
    assert len(result.resolved) == 1
    resolved = result.resolved[0]
    assert isinstance(resolved, ResolvedDeliveryRow)
    assert resolved.isin == "INE002A01018"
    # The (symbol, series, date) key survives; delivery values are carried through untouched.
    assert (resolved.symbol, resolved.series, resolved.trade_date) == (
        "RELIANCE",
        "EQ",
        date(2026, 8, 7),
    )
    assert resolved.deliv_qty == 5187857
    assert resolved.deliv_pct == Decimal("52.48")


def test_resolve_is_date_aware_not_symbol_alone() -> None:
    """Same symbol, two dates, two ISINs — proof the join is not by symbol alone."""
    early = DeliveryRow(
        symbol="RECYCLED", series="EQ", trade_date=date(2019, 6, 3), deliv_qty=None, deliv_pct=None
    )
    late = DeliveryRow(
        symbol="RECYCLED",
        series="EQ",
        trade_date=date(2026, 8, 7),
        deliv_qty=1,
        deliv_pct=Decimal(1),
    )
    result = resolve([early, late], _master())

    by_date = {r.trade_date: r.isin for r in result.resolved}
    assert by_date[date(2019, 6, 3)] == "INE111A01011"
    assert by_date[date(2026, 8, 7)] == "INE222A01012"


def test_resolve_quarantines_an_unknown_symbol_never_drops_it() -> None:
    """A symbol the master never saw is counted in `unresolved`, not silently discarded (M1.8)."""
    known = DeliveryRow(
        symbol="RELIANCE",
        series="EQ",
        trade_date=date(2026, 8, 7),
        deliv_qty=1,
        deliv_pct=Decimal(1),
    )
    unknown = DeliveryRow(
        symbol="NEVERLISTED",
        series="EQ",
        trade_date=date(2026, 8, 7),
        deliv_qty=1,
        deliv_pct=Decimal(1),
    )
    result = resolve([known, unknown], _master())

    assert [r.isin for r in result.resolved] == ["INE002A01018"]
    assert [r.symbol for r in result.unresolved] == ["NEVERLISTED"]
    assert len(result.resolved) + len(result.unresolved) == 2, "no row is lost"


def test_a_whole_fixture_resolves_through_the_master() -> None:
    """End to end: the real file's rows resolve against a master built from its own symbols.

    The master is derived here from the file itself (every (symbol) → a synthetic ISIN), which is
    only legitimate in a test; it proves `resolve` routes every row through the master and keeps
    the delivery values, not that these are the real ISINs.
    """
    fixture = FIXTURE_FILES[0]
    rows = parse(payload_of(fixture), filename=fixture.filename)
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
    result = resolve(rows, IdentityMaster(windows))

    assert result.unresolved == ()
    assert len(result.resolved) == len(rows)
    assert all(len(r.isin) == 12 for r in result.resolved)
    # The dash rows keep their None delivery through resolution.
    absent = fixture.absent
    resolved_absent = [
        r for r in result.resolved if r.symbol == absent.symbol and r.series == absent.series
    ]
    assert len(resolved_absent) == 1
    assert resolved_absent[0].deliv_qty is None


# ── failure modes: loud and located ─────────────────────────────────────────────────────────


def test_a_short_row_names_the_file_and_the_line() -> None:
    """A body cut mid-record fails on the record it was cut in, not silently one row short."""
    body = f"{RAW_HEADER}\n{GOOD_ROW}\nZZTRUNC, EQ, 07-Aug-2026, 1, 2\n"
    with pytest.raises(ParseError) as caught:
        parse(body.encode("utf-8"), filename="short.csv")

    assert caught.value.filename == "short.csv"
    assert caught.value.line == 3
    assert "short.csv:3" in str(caught.value)
    assert "5 fields" in str(caught.value)


def test_a_wide_row_is_refused() -> None:
    """An extra field is a format change, not a curiosity."""
    body = f"{RAW_HEADER}\n{GOOD_ROW}, extra\n"
    with pytest.raises(ParseError) as caught:
        parse(body.encode("utf-8"), filename="wide.csv")

    assert caught.value.line == 2


@pytest.mark.parametrize(
    ("bad", "column"),
    [
        ("X, EQ, 07-Aug-2026, 1, 1, 1, 1, 1, 1, 1, 10, 0.1, 2, ten, 5.0", "DELIV_QTY"),
        ("X, EQ, 07-Aug-2026, 1, 1, 1, 1, 1, 1, 1, 10, 0.1, 2, 10.5, 5.0", "DELIV_QTY"),
        ("X, EQ, 07-Aug-2026, 1, 1, 1, 1, 1, 1, 1, 10, 0.1, 2, 10, five", "DELIV_PER"),
        ("X, EQ, 07-Aug-2026, 1, 1, 1, 1, 1, 1, 1, 10, 0.1, 2, 10, NaN", "DELIV_PER"),
        ("X, EQ, 07-Aug-2026, 1, 1, 1, 1, 1, 1, 1, 10, 0.1, 2, -5, 5.0", "DELIV_QTY"),
        ("X, EQ, 32-Aug-2026, 1, 1, 1, 1, 1, 1, 1, 10, 0.1, 2, 10, 5.0", "DATE1"),
        ("X, EQ, 07-Zzz-2026, 1, 1, 1, 1, 1, 1, 1, 10, 0.1, 2, 10, 5.0", "DATE1"),
        (", EQ, 07-Aug-2026, 1, 1, 1, 1, 1, 1, 1, 10, 0.1, 2, 10, 5.0", "SYMBOL"),
    ],
)
def test_a_bad_field_names_the_column_the_file_and_the_line(bad: str, column: str) -> None:
    """Each of these must never become a delivery figure, a date or an identity."""
    body = f"{RAW_HEADER}\n{GOOD_ROW}\n{bad}\n"
    with pytest.raises(ParseError) as caught:
        parse(body.encode("utf-8"), filename="corrupt.csv")

    assert caught.value.filename == "corrupt.csv"
    assert caught.value.line == 3
    assert "corrupt.csv:3" in str(caught.value)
    assert column in str(caught.value)


def test_an_empty_and_a_header_only_file_are_both_refused() -> None:
    """Both mean "the fetch worked and there is no data" — never a quiet zero rows."""
    with pytest.raises(ParseError, match=r"empty\.csv"):
        parse(b"", filename="empty.csv")

    with pytest.raises(ParseError, match="no data rows"):
        parse(f"{RAW_HEADER}\n".encode(), filename="header_only.csv")


def test_a_file_spanning_two_sessions_is_refused() -> None:
    """One delivery file is one session; two dates would split across L1 partitions unnoticed."""
    body = (
        f"{RAW_HEADER}\n"
        "A, EQ, 07-Aug-2026, 1, 1, 1, 1, 1, 1, 1, 10, 0.1, 2, 10, 5.0\n"
        "B, EQ, 06-Aug-2026, 1, 1, 1, 1, 1, 1, 1, 10, 0.1, 2, 10, 5.0\n"
    )
    with pytest.raises(ParseError, match="more than one session"):
        parse(body.encode("utf-8"), filename="two_days.csv")


def test_a_duplicate_symbol_series_key_is_refused() -> None:
    """The natural key must be unique, or one security's delivery silently overwrites another's."""
    body = (
        f"{RAW_HEADER}\n"
        "DUP, EQ, 07-Aug-2026, 1, 1, 1, 1, 1, 1, 1, 10, 0.1, 2, 10, 5.0\n"
        "DUP, EQ, 07-Aug-2026, 1, 1, 1, 1, 1, 1, 1, 10, 0.1, 2, 20, 6.0\n"
    )
    with pytest.raises(ParseError, match="duplicate"):
        parse(body.encode("utf-8"), filename="dupe.csv")


def test_a_same_symbol_in_two_series_is_allowed() -> None:
    """(symbol, series) is the key — one symbol in EQ and BE on a day is not a duplicate."""
    body = (
        f"{RAW_HEADER}\n"
        "SYM, EQ, 07-Aug-2026, 1, 1, 1, 1, 1, 1, 1, 10, 0.1, 2, 10, 5.0\n"
        "SYM, BE, 07-Aug-2026, 1, 1, 1, 1, 1, 1, 1, 10, 0.1, 2, -, -\n"
    )
    rows = parse(body.encode("utf-8"), filename="two_series.csv")
    assert {(r.symbol, r.series) for r in rows} == {("SYM", "EQ"), ("SYM", "BE")}


def test_non_utf8_bytes_are_refused() -> None:
    """A binary body served with a 200 is a real failure mode, not text to muddle through."""
    with pytest.raises(ParseError, match="not UTF-8"):
        parse(b"\xff\xfe\x00binary", filename="binary.csv")


def test_rows_keep_the_exchange_file_order(real_file: Fixture) -> None:
    """Deterministic output: re-parsing the same payload reproduces the same sequence."""
    payload = payload_of(real_file)
    first = parse(payload, filename=real_file.filename)
    assert first == parse(payload, filename=real_file.filename)
    assert [row.symbol for row in first] == [record[0] for record in raw_records_of(real_file)]


# ── the pipeline entry point: bytes come back out of L0, verified ────────────────────────────


def test_parse_l0_reads_the_payload_back_through_the_store(tmp_path: Path) -> None:
    """The real path: a fetch returns an `L0Ref`, and the parser reads it back re-checksummed."""
    fixture = FIXTURE_FILES[0]
    store = L0Store(clock=FrozenClock(datetime(2026, 8, 7, 19, 30, tzinfo=IST)), data_root=tmp_path)
    ref = store.put(
        DELIVERY_SOURCE_ID,
        fixture.trade_date,
        fixture.filename,
        payload_of(fixture),
        content_type="text/csv",
    )

    rows = parse_l0(store, ref)

    assert rows == parse(payload_of(fixture), filename=fixture.filename)
    assert len(rows) == fixture.data_rows


def test_a_corrupted_l0_payload_never_becomes_rows(tmp_path: Path) -> None:
    """Damage under L0's feet must stop at the store, not arrive as plausible delivery figures."""
    fixture = FIXTURE_FILES[0]
    store = L0Store(clock=FrozenClock(datetime(2026, 8, 7, 19, 30, tzinfo=IST)), data_root=tmp_path)
    ref = store.put(DELIVERY_SOURCE_ID, fixture.trade_date, fixture.filename, payload_of(fixture))

    path = store.path_of(ref)
    path.chmod(0o644)
    path.write_bytes(b"not the bytes that were checksummed")

    with pytest.raises(Exception, match="hashes to"):
        parse_l0(store, ref)


def test_a_file_whose_body_is_another_session_is_refused() -> None:
    """The archive answers some dated URLs with a different session's file — loudly, now.

    `sec_bhavdata_full_30092019.csv` returns HTTP 200 and 210KB of **27-Jun-2019** rows. This is
    the captured payload, byte for byte. Before this check a backfill took it at face value: it
    joined June's delivery onto June's prices, rewrote a partition for a session nobody asked for,
    and marked 2019-09-30 PUBLISHED — so that session silently never got its delivery figures while
    its checkpoint claimed it had. Caught only because a spot check showed 0% delivery on a
    "published" session, which is not a way to find defects.
    """
    name = "sec_bhavdata_full_30092019_MISDATED.csv"
    payload = (FIXTURES / name).read_bytes()

    # It parses perfectly well — the file is valid, it is simply not the session it was named for.
    rows = parse(payload, filename=name)
    assert rows[0].trade_date == date(2019, 6, 27)

    with pytest.raises(ParseError, match="was fetched as"):
        parse(payload, filename=name, trade_date=date(2019, 9, 30))


# ── the one payload that came back as a spreadsheet ──────────────────────────────────────────────


def test_an_xlsx_workbook_served_as_csv_is_decoded_not_refused() -> None:
    """2022-08-08: the archive answered the `.csv` URL with a workbook, Content-Type and all.

    Nothing about the data was wrong — same header with its leading spaces, same DATE1, the whole
    session present. Only the container was, and a plain utf-8 decode died on the zip header at
    byte 22, costing the lake a day of delivery figures.
    """
    payload = (FIXTURES / XLSX_ERA).read_bytes()
    assert payload.startswith(b"PK\x03\x04"), "the fixture must still be the workbook, not a CSV"

    rows = parse(payload, filename=XLSX_ERA, trade_date=date(2022, 8, 8))

    assert len(rows) == 2255
    assert rows[0].symbol == "20MICRONS"
    assert rows[0].trade_date == date(2022, 8, 8)
    assert rows[0].deliv_qty == 571768
    assert rows[0].deliv_pct == Decimal("33.89")
    # The `-` rows this source writes for a series with no delivery figure survive the conversion
    # rather than becoming empty strings that parse as zero.
    assert any(row.deliv_qty is None for row in rows)


def test_a_zip_that_is_not_a_workbook_is_refused_clearly() -> None:
    """The rescue is for one malformed container, not a licence to accept any archive."""
    with pytest.raises(ParseError, match="not a readable xlsx workbook"):
        parse(b"PK\x03\x04 and then nothing useful", filename="junk.csv", trade_date=None)
