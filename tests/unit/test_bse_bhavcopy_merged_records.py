"""BSE legacy bhavcopy: two records published run together on one line (EQ291221_CSV.ZIP:1773).

BSE's own file for 2021-12-29 lost the CRLF after scrip 531358, whose `TDCLOINDI` was empty, so
that empty field fused with the next row's `SC_CODE` and the line came out 27 fields wide. Until
this fix the parser read that as a truncated download and refused the whole session — 3,737 real
quotes lost for one line. These tests pin the repair against a byte-for-byte excerpt of the real
file (`tests/fixtures/bse_bhavcopy/legacy/EQ291221_merged_records.CSV`, provenance in that
directory's PROVENANCE.md):

* the line splits back into exactly the two records BSE meant, when every check lines up;
* when a check does not line up it is quarantined with a named reason — never split on a guess,
  never dropped silently, never fatal to the session — and the L1 writer lands it in
  `prices_raw_quarantine`;
* a wrong-width line that is *not* merged-pair shaped is still the session-fatal truncation it was.

Offline: every test reads the frozen fixture or writes under `tmp_path`.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final

import pyarrow.parquet as pq
import pytest
from structlog.testing import capture_logs

from dataplatform.clock import FrozenClock
from dataplatform.identity.master import IdentityMaster, ListingStatus
from dataplatform.ingest import backfill, source_register
from dataplatform.ingest.bse import bhavcopy, scrip_master
from dataplatform.ingest.bse.bhavcopy import LegacyParse, MalformedLine
from dataplatform.ingest.bse.scrip_master import BseScrip
from dataplatform.ingest.models import ParseError, is_isin_check_digit_valid
from dataplatform.store.l0 import L0Store
from dataplatform.store.l1 import read_prices_raw
from dataplatform.store.paths import Layer, partition_path
from dataplatform.store.schemas import PRICES_RAW_QUARANTINE_DATASET, PriceQuarantineReason

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
FIXTURE: Final = (
    REPO_ROOT / "tests" / "fixtures" / "bse_bhavcopy" / "legacy" / "EQ291221_merged_records.CSV"
)
FILENAME: Final = "EQ291221_CSV.ZIP"
SESSION: Final = date(2021, 12, 29)

#: The merged line's position in the excerpt (header is line 1). It is line 1773 in BSE's file.
MERGED_LINE: Final = 3

#: The excerpt's scrips in file order: the row before, the merged pair, the row after.
SCRIPS_IN_ORDER: Final = ("531352", "531358", "531359", "531360")


def _isin(n: int) -> str:
    """A synthetic ISIN with a valid ISO 6166 check digit."""
    body = f"INE{n:08d}"
    for digit in "0123456789":
        if is_isin_check_digit_valid(body + digit):
            return body + digit
    raise AssertionError(body)


@pytest.fixture
def text() -> str:
    return FIXTURE.read_bytes().decode("utf-8")


def _report(text: str) -> LegacyParse:
    return bhavcopy.parse_legacy_text_report(text, filename=FILENAME, trade_date=SESSION)


def _merged_line(text: str) -> str:
    return text.splitlines()[MERGED_LINE - 1]


def _with_merged_line(text: str, replacement: str) -> str:
    return text.replace(_merged_line(text), replacement)


# ── the fixture is what it claims to be ──────────────────────────────────────────────────────────


def test_fixture_is_the_real_defect_with_crlf_line_endings() -> None:
    payload = FIXTURE.read_bytes()
    lines = payload.split(b"\r\n")
    assert lines[-1] == b"", "BSE terminates every line, the last included, with CRLF"
    assert len(lines[MERGED_LINE - 1].split(b",")) == 27
    assert b",2504395.00,531359,SHRIRAM ASSE," in lines[MERGED_LINE - 1]


def test_without_the_split_the_merged_line_is_session_fatal(text: str) -> None:
    """The pre-fix behaviour, kept reachable: `_legacy_row` alone refuses a 27-field line."""
    record = _merged_line(text).split(",")
    with pytest.raises(ParseError, match="27 fields"):
        bhavcopy._legacy_row(record, line=MERGED_LINE, filename=FILENAME, trade_date=SESSION)


# ── the unambiguous split ───────────────────────────────────────────────────────────────────────


def test_merged_line_splits_into_the_two_records_bse_published(text: str) -> None:
    report = _report(text)

    assert report.quarantined == ()
    assert report.split_lines == (MERGED_LINE,)
    assert tuple(q.scrip_code for q in report.quotes) == SCRIPS_IN_ORDER  # file order kept
    assert len(report) == 4

    first = report.quotes[1]
    assert (first.scrip_code, first.scrip_name, first.group) == ("531358", "CHOICE INT.", "X")
    assert (first.open, first.high, first.low, first.close) == (
        Decimal("152.50"),
        Decimal("156.80"),
        Decimal("150.55"),
        Decimal("153.05"),
    )
    assert (first.last, first.prev_close) == (Decimal("155.00"), Decimal("152.45"))
    assert (first.total_trades, first.total_traded_qty) == (77, 16197)
    # The last money field of record one must not have absorbed record two's scrip code.
    assert first.total_traded_value == Decimal("2504395.00")

    second = report.quotes[2]
    assert (second.scrip_code, second.scrip_name, second.group) == ("531359", "SHRIRAM ASSE", "XT")
    assert (second.open, second.high, second.low, second.close) == (
        Decimal("98.20"),
        Decimal("105.00"),
        Decimal("98.20"),
        Decimal("104.40"),
    )
    assert (second.last, second.prev_close) == (Decimal("101.85"), Decimal("102.70"))
    assert (second.total_trades, second.total_traded_qty) == (15, 188)
    assert second.total_traded_value == Decimal("19638.00")
    assert {q.trade_date for q in report.quotes} == {SESSION}


def test_parse_legacy_returns_the_split_quotes_from_the_zip_path(text: str) -> None:
    quotes = bhavcopy.parse_legacy(text.encode("utf-8"), filename=FILENAME, trade_date=SESSION)
    assert tuple(q.scrip_code for q in quotes) == SCRIPS_IN_ORDER


def test_the_split_is_logged_with_source_date_and_line(text: str) -> None:
    with capture_logs() as entries:
        _report(text)
    [event] = [e for e in entries if e["event"] == "bhavcopy.legacy_records_split"]
    assert event["source"] == "bse_bhavcopy_legacy"
    assert event["trade_date"] == "2021-12-29"
    assert event["line"] == MERGED_LINE
    assert event["filename"] == FILENAME
    assert event["scrip_codes"] == ["531358", "531359"]


# ── ambiguous: quarantined, never guessed, never fatal ──────────────────────────────────────────


AMBIGUOUS: Final = [
    pytest.param(
        # Record one carried a non-empty TDCLOINDI: it fuses with the next SC_CODE and the seam
        # between the two records is no longer a field boundary anyone can point at.
        lambda line: line.replace(",531359,", ",XD531359,"),
        "not a bare scrip code",
        id="non-empty-tdcloindi-fused-into-the-seam",
    ),
    pytest.param(
        lambda line: line.replace(",104.40,", ",1O4.40,"),
        "a half fails the row checks",
        id="second-half-has-a-non-numeric-price",
    ),
    pytest.param(
        lambda line: line.replace(",2504395.00,", ",2504395.00.1,"),
        "a half fails the row checks",
        id="first-half-has-a-non-numeric-turnover",
    ),
    pytest.param(
        lambda line: line.replace(",531359,", ",531358,"),
        "both halves carry scrip 531358",
        id="same-scrip-on-both-sides",
    ),
]


@pytest.mark.parametrize(("corrupt", "detail"), AMBIGUOUS)
def test_an_ambiguous_merged_line_is_quarantined_and_the_session_survives(
    text: str, corrupt: object, detail: str
) -> None:
    assert callable(corrupt)
    report = _report(_with_merged_line(text, corrupt(_merged_line(text))))

    # Neither half of the line becomes a quote — half of a provably wrong line is not trusted.
    assert tuple(q.scrip_code for q in report.quotes) == ("531352", "531360")
    assert report.split_lines == ()
    [malformed] = report.quarantined
    assert isinstance(malformed, MalformedLine)
    assert malformed.reason == PriceQuarantineReason.MERGED_RECORDS_UNSPLITTABLE
    assert malformed.line == MERGED_LINE
    assert (malformed.scrip_code, malformed.group) == ("531358", "X")
    assert detail in malformed.detail
    assert malformed.text == corrupt(_merged_line(text))
    # Nothing dropped: every data line is a quote or a quarantined line.
    assert len(report) == 3


def test_the_quarantine_is_logged_with_source_date_line_and_reason(text: str) -> None:
    corrupted = _with_merged_line(text, _merged_line(text).replace(",531359,", ",XD531359,"))
    with capture_logs() as entries:
        _report(corrupted)
    [event] = [e for e in entries if e["event"] == "bhavcopy.legacy_line_quarantined"]
    assert event["log_level"] == "warning"
    assert event["source"] == "bse_bhavcopy_legacy"
    assert event["trade_date"] == "2021-12-29"
    assert event["line"] == MERGED_LINE
    assert event["reason"] == "merged_records_unsplittable"
    assert "XD531359" in event["text"]


def test_a_wrong_width_line_that_is_not_a_merged_pair_still_refuses_the_session(text: str) -> None:
    """Only the merged-pair width is repaired; a short row is still what truncation looks like."""
    short = _with_merged_line(text, ",".join(_merged_line(text).split(",")[:10]))
    with pytest.raises(ParseError, match="10 fields"):
        _report(short)


def test_split_merged_records_refuses_a_line_of_another_width() -> None:
    with pytest.raises(ValueError, match="27-field"):
        bhavcopy.split_merged_records(["1"] * 14, line=2, filename=FILENAME, trade_date=SESSION)


# ── the backfill lands it: split quotes in prices_raw, quarantined line in the quarantine ──────


def _master(known: tuple[str, ...] = SCRIPS_IN_ORDER) -> IdentityMaster:
    scrips = [
        BseScrip(
            scrip_code=code,
            symbol=f"S{code}",
            name=f"SCRIP {code}",
            isin=_isin(int(code)),
            status=ListingStatus.ACTIVE,
            group="X",
            face_value_inr=Decimal(10),
        )
        for code in known
    ]
    derived = scrip_master.derive_master(scrips, snapshot_date=SESSION)
    return IdentityMaster(derived.windows, securities=derived.securities, listings=derived.listings)


def _write(text: str, tmp_path: Path, known: tuple[str, ...] = SCRIPS_IN_ORDER) -> LegacyParse:
    l0 = L0Store(clock=FrozenClock(SESSION), data_root=tmp_path)
    ref = l0.put(bhavcopy.LEGACY_SOURCE_ID, SESSION, FILENAME, text.encode("utf-8"))
    source_set = backfill.SOURCE_SETS[backfill.BSE_BHAVCOPY_LEGACY]
    parsed: LegacyParse = source_set.parse(l0, ref)
    ctx = backfill.WriteContext(
        l0=l0, data_root=tmp_path, master=_master(known), register=source_register.load()
    )
    source_set.write(parsed, ctx)
    return parsed


def test_backfill_writes_both_split_records_to_prices_raw(text: str, tmp_path: Path) -> None:
    parsed = _write(text, tmp_path)
    assert len(parsed) == 4

    rows = read_prices_raw(SESSION, data_root=tmp_path)
    assert sorted([str(row["isin"]) for row in rows]) == sorted(
        _isin(int(code)) for code in SCRIPS_IN_ORDER
    )
    quarantine = partition_path(
        Layer.L1, PRICES_RAW_QUARANTINE_DATASET, SESSION, data_root=tmp_path
    )
    assert not quarantine.exists()


def test_backfill_quarantines_an_unsplittable_line_with_its_reason(
    text: str, tmp_path: Path
) -> None:
    corrupted = _with_merged_line(text, _merged_line(text).replace(",531359,", ",XD531359,"))
    _write(corrupted, tmp_path)

    rows = read_prices_raw(SESSION, data_root=tmp_path)
    assert sorted([str(row["isin"]) for row in rows]) == sorted([_isin(531352), _isin(531360)])
    quarantine = partition_path(
        Layer.L1, PRICES_RAW_QUARANTINE_DATASET, SESSION, data_root=tmp_path
    )
    [record] = pq.read_table(quarantine).to_pylist()
    assert record["reason"] == "merged_records_unsplittable"
    assert (record["symbol"], record["series"]) == ("531358", "X")
    assert record["exchange"] == "BSE"
    assert record["trade_date"] == SESSION
    assert record["isin"] is None


def test_backfill_quarantines_a_scrip_the_master_cannot_resolve(text: str, tmp_path: Path) -> None:
    """An unresolved scrip code is enumerated in the quarantine, not counted in a log and dropped.

    Fails on the pre-2026-10-06 writer, which discarded `resolve_legacy`'s `unresolved` (catalog
    B1 defect (b)) and so left no quarantine partition at all for this session.
    """
    _write(text, tmp_path, known=("531352", "531358", "531360"))

    rows = read_prices_raw(SESSION, data_root=tmp_path)
    assert sorted(str(row["isin"]) for row in rows) == sorted(
        [_isin(531352), _isin(531358), _isin(531360)]
    )
    quarantine = partition_path(
        Layer.L1, PRICES_RAW_QUARANTINE_DATASET, SESSION, data_root=tmp_path
    )
    [record] = pq.read_table(quarantine).to_pylist()
    assert record["reason"] == "scrip_unresolved"
    assert (record["symbol"], record["exchange"], record["isin"]) == ("531359", "BSE", None)
