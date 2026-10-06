"""The bhavcopy format eras, and the one boundary a decade of prices hangs on (W1).

The ISIN column appears on 2011-06-22 and is absent on 2011-06-21. Nothing in the code can *derive*
that — it was measured, twice, from two directions: the study bisected the live archive on
2026-09-07, and the Phase 1 smoke run re-fetched both sessions minutes apart on 2026-09-08 and got
byte counts matching the study exactly. So the boundary is a constant, and a constant that decides
where a decade of rows lands needs a test that fails if it moves.

That is what most of this file is. `test_the_isin_cutover_is_a_single_day_and_has_not_moved` pins
the pair (2011-06-21 → E1, 2011-06-22 → E2) *and* pins the fixtures either side of it, so the
boundary cannot be shifted by a day in either direction without a red test — not by editing the
constant, and not by swapping the fixtures.

The rest asserts the two things that follow from an era having no ISIN: the production parser still
refuses E1 (unweakened), and the E1 reader can only ever produce rows that have no identity.

Offline and deterministic (B8): every byte read here comes from `tests/fixtures/`.
"""

from __future__ import annotations

import csv
import io
import zipfile
from datetime import date, timedelta
from pathlib import Path
from typing import Final

import pytest

from dataplatform.ingest.models import ParseError, UnidentifiedRow
from dataplatform.ingest.nse import bhavcopy, bhavcopy_legacy, eras
from dataplatform.ingest.nse.bhavcopy_udiff import UDIFF_SOURCE_ID
from dataplatform.ingest.nse.eras import (
    ARCHIVE_START,
    ERAS,
    ISIN_ERA_START,
    PRE_ISIN_ERA_LAST_SESSION,
    EraCoverageError,
)

FIXTURES: Final = Path(__file__).resolve().parents[1] / "fixtures" / "nse_bhavcopy"

#: The two consecutive sessions the cutover falls between, with the facts measured off the wire.
PRE_ISIN_BOUNDARY_FILE: Final = FIXTURES / "pre_isin" / "cm21JUN2011bhav.csv.zip"
ISIN_BOUNDARY_FILE: Final = FIXTURES / "legacy" / "cm22JUN2011bhav.csv.zip"

#: The E1a sub-era: `TIMESTAMP` day not zero-padded.
PRE_ISIN_UNPADDED_FILE: Final = FIXTURES / "pre_isin" / "cm02JAN2006bhav.csv.zip"


def csv_of(path: Path) -> str:
    """The zipped CSV member, unzipped here rather than by the parser — the reference read."""
    with zipfile.ZipFile(io.BytesIO(path.read_bytes())) as archive:
        (member,) = archive.namelist()
        return archive.read(member).decode("utf-8")


def header_of(path: Path) -> list[str]:
    """The fixture's header fields, read with the stdlib."""
    return next(csv.reader(io.StringIO(csv_of(path))))


def data_records_of(path: Path) -> list[list[str]]:
    """Every non-blank data record of the fixture, header dropped."""
    reader = csv.reader(io.StringIO(csv_of(path)))
    next(reader)
    return [rec for rec in reader if rec and any(field.strip() for field in rec)]


# ── the boundary ─────────────────────────────────────────────────────────────────────────────


def test_the_isin_cutover_is_a_single_day_and_has_not_moved() -> None:
    """2011-06-21 and 2011-06-22 route to different eras, and the fixtures prove which is which.

    The test the task packet asks for by name: it fails if the boundary moves by one day. Three
    independent things have to agree, so moving the constant alone is not enough to make it pass —
    the *files* would have to change too, and they are real archive bytes.
    """
    assert eras.era_for(PRE_ISIN_ERA_LAST_SESSION).label == "E1"
    assert eras.era_for(ISIN_ERA_START).label == "E2"
    assert eras.era_for(PRE_ISIN_ERA_LAST_SESSION) is not eras.era_for(ISIN_ERA_START)

    # Consecutive calendar days: the cutover is a clean single-day switch, not a fuzzy window.
    assert timedelta(days=1) == ISIN_ERA_START - PRE_ISIN_ERA_LAST_SESSION
    assert (date(2011, 6, 21), date(2011, 6, 22)) == (PRE_ISIN_ERA_LAST_SESSION, ISIN_ERA_START)

    # And the archive agrees, in bytes: no ISIN the day before, an ISIN the day of.
    assert "ISIN" not in header_of(PRE_ISIN_BOUNDARY_FILE)
    assert "ISIN" in header_of(ISIN_BOUNDARY_FILE)


def test_moving_the_boundary_a_day_either_way_would_misroute_a_real_file() -> None:
    """The day *before* the cutover has no ISIN, so E2's parser cannot read it — and vice versa.

    This is the boundary test stated as consequence rather than as equality: if the constant slid a
    day earlier, `parse` would be handed the eleven-column file; a day later, `parse_pre_isin` would
    be handed the ISIN-bearing one. Both are asserted to fail, so neither slide can be silent.
    """
    with pytest.raises(ParseError, match="unexpected header"):
        bhavcopy_legacy.parse(
            PRE_ISIN_BOUNDARY_FILE.read_bytes(), filename=PRE_ISIN_BOUNDARY_FILE.name
        )
    with pytest.raises(ParseError, match="unexpected header"):
        bhavcopy_legacy.parse_pre_isin(
            ISIN_BOUNDARY_FILE.read_bytes(), filename=ISIN_BOUNDARY_FILE.name
        )


def test_the_isin_era_parses_with_the_parser_that_already_works() -> None:
    """No new parser for E2: the production dispatcher reads the cutover session as it stands.

    The measured claim from the study, re-asserted against the frozen payload — `LEGACY_COLUMNS`
    matches, nothing is refused, and the first row is `20MICRONS` at `INE144J01019`.
    """
    parsed = bhavcopy.parse_report(
        ISIN_BOUNDARY_FILE.read_bytes(),
        filename=ISIN_BOUNDARY_FILE.name,
        trade_date=ISIN_ERA_START,
    )
    assert len(parsed.refused) == 0
    assert len(parsed.rows) == 1502
    first = parsed.rows[0]
    assert (first.symbol, first.isin) == ("20MICRONS", "INE144J01019")
    assert str(first.close) == "46.65"
    assert all(row.isin for row in parsed.rows), "every E2 row must carry an ISIN"


# ── the era registry itself ──────────────────────────────────────────────────────────────────


def test_the_eras_tile_the_archive_without_gaps() -> None:
    """Every date from 1995 on falls in exactly one era, and the last era is open-ended."""
    assert [era.label for era in ERAS] == ["E1", "E2", "E3"]
    for day in (
        ARCHIVE_START,
        date(2006, 1, 2),
        PRE_ISIN_ERA_LAST_SESSION,
        ISIN_ERA_START,
        date(2016, 9, 1),
        bhavcopy.CUTOVER - timedelta(days=1),
        bhavcopy.CUTOVER,
        date(2026, 9, 1),
    ):
        matching = [era for era in ERAS if era.covers(day)]
        assert len(matching) == 1, f"{day} is covered by {[e.label for e in matching]}"


def test_a_date_before_the_archive_is_refused_not_guessed() -> None:
    """Below 1995-01-02 there is no file, so there is no era — and no request worth spending."""
    with pytest.raises(EraCoverageError, match="before the NSE bhavcopy archive begins"):
        eras.era_for(ARCHIVE_START - timedelta(days=1))
    with pytest.raises(EraCoverageError):
        eras.eras_in(date(1990, 1, 1), date(2000, 1, 1))


def test_the_registry_agrees_with_the_udiff_dispatcher() -> None:
    """E3's start is `bhavcopy.CUTOVER` itself, so the two dispatchers cannot drift apart."""
    e1, e2, e3 = ERAS
    assert e2.end == bhavcopy.CUTOVER == e3.start
    assert e3.source_id == UDIFF_SOURCE_ID
    # Both pre-UDiFF eras are served by the same URL template and the same register row: they
    # differ in the file's columns, not in its address.
    assert e1.source_id == e2.source_id == bhavcopy_legacy.LEGACY_SOURCE_ID
    # And the coarse dispatcher still answers what it always did, for every era's own dates.
    assert bhavcopy.era_of(ISIN_ERA_START) == "legacy"
    assert bhavcopy.era_of(PRE_ISIN_ERA_LAST_SESSION) == "legacy"
    assert bhavcopy.era_of(bhavcopy.CUTOVER) == "udiff"


def test_only_the_pre_isin_era_lacks_an_identity() -> None:
    """`carries_isin` is the flag the promotion path branches on; it must be true for E2 and E3."""
    assert [era.carries_isin for era in ERAS] == [False, True, True]


def test_eras_in_reports_what_a_range_crosses() -> None:
    """An operator asks "does this campaign cross the ISIN boundary?" before spending requests."""
    assert [e.label for e in eras.eras_in(date(2006, 1, 2), date(2016, 9, 1))] == ["E1", "E2"]
    assert [e.label for e in eras.eras_in(ISIN_ERA_START, date(2016, 9, 1))] == ["E2"]
    assert [e.label for e in eras.eras_in(date(2006, 1, 2), date(2026, 9, 1))] == ["E1", "E2", "E3"]
    with pytest.raises(ValueError, match="is after end"):
        eras.eras_in(date(2016, 1, 1), date(2006, 1, 1))


def test_fixture_dir_names_the_directory_the_payloads_really_live_in() -> None:
    """The registry's `fixture_dir` is a claim about the repo; it is checked, not assumed."""
    by_label = {era.label: era for era in ERAS}
    assert (FIXTURES.parent / by_label["E1"].fixture_dir).is_dir()
    assert (FIXTURES.parent / by_label["E2"].fixture_dir).is_dir()
    assert (FIXTURES.parent / by_label["E3"].fixture_dir).is_dir()


# ── the pre-ISIN reader: retention, never rescue ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("path", "session", "rows"),
    [
        (PRE_ISIN_BOUNDARY_FILE, date(2011, 6, 21), 1503),
        (PRE_ISIN_UNPADDED_FILE, date(2006, 1, 2), 876),
    ],
    ids=["2011-06-21 (the last pre-ISIN session)", "2006-01-02 (unpadded TIMESTAMP)"],
)
def test_the_pre_isin_reader_enumerates_every_row_and_drops_none(
    path: Path, session: date, rows: int
) -> None:
    """One `UnidentifiedRow` per data row, counted against an independent read of the CSV.

    "Drops nothing" is the whole contract of the quarantine path, so it is asserted by count and
    by symbol, not described.
    """
    enumerated = bhavcopy_legacy.parse_pre_isin(path.read_bytes(), filename=path.name)
    records = data_records_of(path)

    assert len(enumerated) == rows == len(records)
    assert [row.symbol for row in enumerated] == [rec[0] for rec in records]
    assert [row.series for row in enumerated] == [rec[1] for rec in records]
    assert {row.trade_date for row in enumerated} == {session}
    assert all(row.line >= 2 for row in enumerated), "line numbers are 1-based, header at line 1"


def test_the_pre_isin_reader_cannot_produce_an_identity() -> None:
    """Every row comes back `UnidentifiedRow` with no stated ISIN — there was no column to state.

    The mechanical guarantee behind "E1 is retained, not rescued": the reader's return type has no
    ISIN field to fill, so there is no code path from an E1 file to a `prices_raw` row.
    """
    enumerated = bhavcopy_legacy.parse_pre_isin(
        PRE_ISIN_BOUNDARY_FILE.read_bytes(), filename=PRE_ISIN_BOUNDARY_FILE.name
    )
    assert all(isinstance(row, UnidentifiedRow) for row in enumerated)
    assert {row.stated_isin for row in enumerated} == {""}
    assert "ISIN" not in bhavcopy_legacy.PRE_ISIN_COLUMNS


def test_the_pre_isin_reader_fails_loudly_on_a_truncated_row() -> None:
    """A short row is what a cut-off transfer leaves behind, and it names the line."""
    text = csv_of(PRE_ISIN_BOUNDARY_FILE)
    lines = text.splitlines()
    lines[3] = ",".join(lines[3].split(",")[:6])
    with pytest.raises(ParseError, match="truncated download") as caught:
        bhavcopy_legacy.parse_pre_isin("\n".join(lines).encode("utf-8"), filename="truncated.csv")
    assert caught.value.line == 4


def test_the_pre_isin_reader_refuses_a_file_with_no_rows() -> None:
    """A header and nothing else is not a session; the archive never serves one."""
    header = ",".join(bhavcopy_legacy.PRE_ISIN_COLUMNS) + ",\n"
    with pytest.raises(ParseError, match="no data rows"):
        bhavcopy_legacy.parse_pre_isin(header.encode("utf-8"), filename="empty.csv")
