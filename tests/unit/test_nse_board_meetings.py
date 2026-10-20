"""NSE board-meeting intimations (M17.16): the parser, the knowable date, the PIT read.

Offline (B8): the payload is the frozen `json_v1` fixture (tests/fixtures/nse_board_meetings).
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Final

import pytest

from dataplatform.clock import IST
from dataplatform.ingest.models import ParseError
from dataplatform.ingest.nse import board_meetings as bm

FIXTURE: Final = Path(
    "tests/fixtures/nse_board_meetings/json_v1/corporate_board_meetings_20261020_20261120.json"
)


def _rows() -> tuple[bm.BoardMeetingRow, ...]:
    return bm.parse(FIXTURE.read_bytes(), filename=FIXTURE.name, l0_key="k")


def test_the_json_v1_payload_parses_every_record_with_its_isin_and_dates() -> None:
    rows = _rows()
    assert len(rows) == 17
    infy = [r for r in rows if r.isin == "INE009A01021"]
    assert {r.meeting_date for r in infy} == {date(2026, 10, 23)}
    assert {r.purpose for r in infy} == {"Financial Results/Dividend", "Board Meeting Intimation"}
    first = min(infy, key=lambda r: r.disseminated_at)
    assert first.disseminated_at == datetime(2026, 9, 15, 18, 26, 43, tzinfo=IST)
    assert all(r.l0_key == "k" and r.source == bm.SOURCE_ID for r in rows)


def test_results_are_read_from_the_purpose_or_the_companys_words() -> None:
    by_symbol: dict[str, set[bool]] = {}
    for row in _rows():
        by_symbol.setdefault(row.symbol or "", set()).add(row.is_results)
    # Generic purpose, "financial results" only in bm_desc.
    for symbol in ("CENTRUM", "GILLETTE", "MUKANDLTD", "PGHH", "CUMMINSIND"):
        assert by_symbol[symbol] == {True}, symbol
    # Fund raising and other business are not results.
    assert by_symbol["MANBA"] == {False}
    assert by_symbol["GENUSPOWER"] == {False}


def test_an_intimation_after_the_evening_capture_is_knowable_the_next_day() -> None:
    """Fails if the 20:00 cut-off is dropped or inverted: a row the 20:00 run could not have
    seen must not be dated into that evening's dossier."""
    assert bm.knowable_date_of(datetime(2026, 9, 21, 19, 59, 59, tzinfo=IST)) == date(2026, 9, 21)
    assert bm.knowable_date_of(datetime(2026, 9, 21, 20, 0, 0, tzinfo=IST)) == date(2026, 9, 22)
    assert bm.knowable_date_of(datetime(2026, 9, 21, 20, 34, 23, tzinfo=IST)) == date(2026, 9, 22)
    mphasis = [r for r in _rows() if r.symbol == "MPHASIS"]
    assert {r.knowable_date for r in mphasis} == {date(2026, 9, 22)}
    abb = [r for r in _rows() if r.symbol == "ABB"]
    assert {r.knowable_date for r in abb} == {date(2026, 9, 10)}


def test_a_row_with_no_dissemination_time_is_refused() -> None:
    record = json.loads(FIXTURE.read_bytes())[0]
    record["bm_timestamp"] = None
    with pytest.raises(ParseError, match="point-in-time"):
        bm.parse(json.dumps([record]).encode(), filename="x.json")
    with pytest.raises(ParseError, match="JSON array"):
        bm.parse(b"{}", filename="x.json")
    assert bm.parse(b"[]", filename="x.json") == ()


def test_the_pit_read_never_opens_a_later_partition(tmp_path: Path) -> None:
    rows = _rows()
    assert bm.merge_l1(rows, data_root=tmp_path) == 17
    assert bm.merge_l1(rows, data_root=tmp_path) == 0  # idempotent: nothing new, nothing rewritten
    through = date(2026, 10, 6)
    seen = bm.read_pit(through, data_root=tmp_path)
    assert seen and all(r.knowable_date <= through for r in seen)
    # Mukand (15-Oct) and Genus (19-Oct) were intimated after the 6th.
    assert {"MUKANDLTD", "GENUSPOWER", "MANBA"}.isdisjoint({r.symbol for r in seen})
    assert {r.symbol for r in bm.read_pit(date(2026, 10, 19), data_root=tmp_path)} >= {
        "MUKANDLTD",
        "GENUSPOWER",
    }
    with pytest.raises(FileNotFoundError):
        bm.read_pit(through, data_root=tmp_path / "empty")


def test_the_url_names_both_ends_of_the_meeting_window() -> None:
    url = bm.board_meetings_url(date(2026, 10, 12), date(2027, 1, 17))
    assert url.endswith("from_date=12-10-2026&to_date=17-01-2027")
    with pytest.raises(ValueError):
        bm.board_meetings_url(date(2026, 10, 12), date(2026, 10, 11))
