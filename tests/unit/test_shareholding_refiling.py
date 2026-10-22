"""The same quarter filed twice with the same numbers is one row, dated to the earliest filing.

NSE's 2026-10-21 master lists AAREYDRUGS (`INE198H01019`) twice for the 30-Sep-2026 quarter: filed
19-Oct 19:21:15 and again 21-Oct 18:02:52 IST, neither marked revised, every number equal — only
the filing metadata differs (`broadcastDate`, `submissionDate`, `systemDate`, `recordId`, `xbrl`).
The snapshot refused that payload as "listed more than once", and that refusal was what still
failed the 2026-10-21 poll once the malformed ISINs were excluded. The fixture is that pair, record
for record as L0 holds it, in payload order: the *later* filing comes first.

(a) identical re-filings collapse to one row, dated to the earliest filing, and the collapse is
    logged with both timestamps and the one kept;
(b) the earliest wins whatever the payload order — inverting it would back-date nothing but would
    hide two days of PIT visibility, and these tests fail if it is inverted;
(c) filings whose numbers differ are a restatement, and the file still fails loud;
(d) distinct quarters of one ISIN are never collapsed.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import pytest
from structlog.testing import capture_logs

from dataplatform.ingest.models import ParseError
from dataplatform.ingest.shareholding import parse, read_pit, write_l1
from dataplatform.store.paths import l1_partition_path

PAIR: Final = Path(
    "tests/fixtures/nse_shareholding/master_2026_10/"
    "corporate-share-holdings-master_20261021_aareydrugs_pair.json"
)
#: sha256 of the frozen pair, as PROVENANCE.md records it.
PAIR_SHA256: Final = "0389a01d055031991c3af6afedbfec1f261ac86c0282a789a0426a8c83abe5c9"
SUBSET: Final = Path(
    "tests/fixtures/nse_shareholding/master_2026_10/"
    "corporate-share-holdings-master_20261021_subset.json"
)

AAREYDRUGS: Final = "INE198H01019"
EARLIEST: Final = "19-OCT-2026 19:21:15"
LATEST: Final = "21-OCT-2026 18:02:52"
QUARTER: Final = date(2026, 9, 30)


@pytest.fixture(scope="module")
def pair_bytes(repo_root: Path) -> bytes:
    return (repo_root / PAIR).read_bytes()


@pytest.fixture
def pair(pair_bytes: bytes) -> list[dict[str, Any]]:
    loaded: list[dict[str, Any]] = json.loads(pair_bytes)
    return loaded


def _body(records: list[dict[str, Any]]) -> bytes:
    return json.dumps(records).encode("utf-8")


def _collapses(entries: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [e for e in entries if e["event"] == "shareholding.refiling_collapsed"]


def test_the_fixture_is_the_real_pair(pair_bytes: bytes, pair: list[dict[str, Any]]) -> None:
    assert hashlib.sha256(pair_bytes).hexdigest() == PAIR_SHA256
    assert [r["broadcastDate"] for r in pair] == [LATEST, EARLIEST]  # later filing listed first
    assert {r["isin"] for r in pair} == {AAREYDRUGS}
    differing = sorted(k for k in pair[0] if pair[0][k] != pair[1][k])
    assert differing == [
        "broadcastDate",
        "recordId",
        "submissionDate",
        "systemDate",
        "xbrl",
        "xbrlFileSize",
    ]


# ── (a) identical re-filings collapse to the earliest ─────────────────────────────────────────


def test_identical_refilings_collapse_to_one_row_dated_to_the_earliest_filing(
    pair_bytes: bytes,
) -> None:
    with capture_logs() as entries:
        snapshot = parse(pair_bytes, filename=PAIR.name)
    (row,) = snapshot.rows
    assert (row.isin, row.period_end, row.filing_date) == (AAREYDRUGS, QUARTER, date(2026, 10, 19))
    assert (row.promoter_holding_pct, row.public_pct) == (Decimal("41.45"), Decimal("58.55"))

    (event,) = _collapses(entries)
    assert event["isin"] == AAREYDRUGS
    assert event["period_end"] == "2026-09-30"
    assert event["filings"] == [EARLIEST, LATEST]
    assert event["kept"] == EARLIEST
    assert event["kept_record"] == 1
    assert event["filing_date"] == "2026-10-19"


def test_three_identical_filings_are_still_one_row(pair: list[dict[str, Any]]) -> None:
    third = {**pair[0], "broadcastDate": "20-OCT-2026 11:00:00", "recordId": "215900"}
    (row,) = parse(_body([pair[0], third, pair[1]]), filename="x.json").rows
    assert row.filing_date == date(2026, 10, 19)


# ── (b) the earliest wins, whatever the order ─────────────────────────────────────────────────


@pytest.mark.parametrize("order", [(0, 1), (1, 0)], ids=["as-served", "reversed"])
def test_the_earliest_filing_is_kept_in_either_payload_order(
    pair: list[dict[str, Any]], order: tuple[int, int], tmp_path: Path
) -> None:
    """Keeping the first, the last, or the latest fails one of these two orders, or both."""
    snapshot = parse(_body([pair[i] for i in order]), filename="x.json")
    (row,) = snapshot.rows
    assert row.filing_date == date(2026, 10, 19)
    assert row.filing_date != date(2026, 10, 21)

    # PIT: the numbers are visible from the first filing, and only one partition carries them.
    write_l1(snapshot, data_root=tmp_path)
    assert [r.isin for r in read_pit(date(2026, 10, 19), data_root=tmp_path)] == [AAREYDRUGS]
    assert read_pit(date(2026, 10, 18), data_root=tmp_path) == ()
    assert not l1_partition_path("shareholding", date(2026, 10, 21), data_root=tmp_path).exists()


def test_two_filings_on_one_day_keep_the_earlier_clock(pair: list[dict[str, Any]]) -> None:
    """Same date: ordered by the time of day, as numbers, so 9:05 is before 18:02."""
    late = {**pair[0], "broadcastDate": "19-OCT-2026 18:02:52"}
    early = {**pair[1], "broadcastDate": "19-OCT-2026 9:05:00"}
    with capture_logs() as entries:
        parse(_body([late, early]), filename="x.json")
    (event,) = _collapses(entries)
    assert event["kept"] == "19-OCT-2026 9:05:00"
    assert event["kept_record"] == 1


# ── (c) different numbers are a restatement: the file still fails ─────────────────────────────


@pytest.mark.parametrize(
    "change",
    [
        {"pr_and_prgrp": "41.46", "public_val": "58.54"},
        {"employeeTrusts": "0.01"},
        {"revisedData": "Revised"},
        {"name": "Aarey Drugs and Pharmaceuticals Limited"},
    ],
    ids=["holding", "employee-trusts", "revision-marker", "name"],
)
def test_refilings_that_differ_in_any_published_field_still_fail_the_file(
    pair: list[dict[str, Any]], change: dict[str, str]
) -> None:
    restated = [pair[0], {**pair[1], **change}]
    with capture_logs() as entries, pytest.raises(ParseError, match="more than once"):
        parse(_body(restated), filename="x.json")
    assert _collapses(entries) == []


# ── (d) distinct quarters are never collapsed ─────────────────────────────────────────────────


def test_distinct_quarters_of_one_isin_are_never_collapsed(pair: list[dict[str, Any]]) -> None:
    """Same ISIN, same numbers, different quarter: two rows, each on its own filing date."""
    june = {**pair[1], "date": "30-JUN-2026"}
    with capture_logs() as entries:
        snapshot = parse(_body([pair[0], june]), filename="x.json")
    assert sorted((row.period_end, row.filing_date) for row in snapshot.rows) == [
        (date(2026, 6, 30), date(2026, 10, 19)),
        (date(2026, 9, 30), date(2026, 10, 21)),
    ]
    assert _collapses(entries) == []


# ── both rules on one payload, as the real 2026-10-21 master has them ─────────────────────────


def test_the_2026_10_21_shape_parses_with_both_rules(
    repo_root: Path, pair: list[dict[str, Any]]
) -> None:
    """Two malformed ISINs and a re-filed company in one file: 38 rows, 2 excluded, 1 collapsed.

    The subset is L0 records 0-38, which already holds the 21-Oct filing (L0 record 4); adding the
    19-Oct one (L0 record 386) after it gives the real file's shape.
    """
    subset: list[dict[str, Any]] = json.loads((repo_root / SUBSET).read_bytes())
    assert pair[0] in subset and pair[1] not in subset
    snapshot = parse(_body([*subset, pair[1]]), filename="x.json")
    assert len(snapshot.rows) == 38
    assert len(snapshot.skipped_bad_isin) == 2
    assert [r.filing_date for r in snapshot.rows if r.isin == AAREYDRUGS] == [date(2026, 10, 19)]
