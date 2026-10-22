"""A malformed ISIN excludes one filing, not the whole day (`shareholding_poll`, 2026-10-16..21).

From 2026-10-16 NSE's shareholding master carried two filings whose `isin` is not an ISIN —
`'INE 439A0401'` (Asahi India Glass: an embedded space) and `'INE07180108'` (Procter & Gamble
Health: eleven characters) — and the parser rejected every poll whole for them, six days running.
The fixture is a record-verbatim cut of the 2026-10-21 L0 payload (PROVENANCE.md): its first 39
records, one of them published with a null ISIN, and those two.

1. **Excluded and flagged, never repaired.** The two records leave `rows`, each raises a WARN under
   `shareholding_bad_isin` carrying the literal as published and its record number, and every
   other record publishes. No row carries an ISIN the payload did not state.
2. **A large share still fails whole.** Over `MAX_BAD_ISIN_PCT` of the records malformed is a format
   change in the identifier, and the file fails; exactly at the threshold it publishes.
3. **Inverting the logic fails here.** A well-formed ISIN is never flagged or dropped — over the
   fixture, over generated ISINs (padded too), and by the excluded set's own validation.
4. **Structural faults stay loud.** A non-string `isin`, or a malformed-ISIN record whose own dates
   do not parse, still fails the file.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from dataplatform.ingest.models import ISIN_PATTERN, ParseError
from dataplatform.ingest.shareholding import (
    BAD_ISIN_CHECK,
    MAX_BAD_ISIN_PCT,
    NO_ISIN_CHECK,
    SOURCE_ID,
    MalformedIsinFiling,
    ShareholdingRow,
    ShareholdingSnapshot,
    parse,
    quality_findings,
    read_pit,
    write_l1,
)

SUBSET: Final = Path(
    "tests/fixtures/nse_shareholding/master_2026_10/"
    "corporate-share-holdings-master_20261021_subset.json"
)
#: sha256 of the frozen cut, as PROVENANCE.md records it.
SUBSET_SHA256: Final = "a88e7c0f4ede7ebd61f7e449f025665e51f2cc7ad84fffeb057a9084acfd5a56"
#: The L0 payload the cut was taken from — what `parse_l0` would pass as `l0_key`.
L0_KEY: Final = f"{SOURCE_ID}/2026-10-21/corporate-share-holdings-master_20261021.json"

ASAHI_RAW: Final = "INE 439A0401"
PGHL_RAW: Final = "INE07180108"
IMFA: Final = "INE919H01018"  # a well-formed ISIN from the 2026-10-06 live fixture

_ISIN: Final = re.compile(ISIN_PATTERN)


@pytest.fixture(scope="module")
def subset_bytes(repo_root: Path) -> bytes:
    return (repo_root / SUBSET).read_bytes()


@pytest.fixture(scope="module")
def records(subset_bytes: bytes) -> list[dict[str, Any]]:
    loaded: list[dict[str, Any]] = json.loads(subset_bytes)
    return loaded


@pytest.fixture(scope="module")
def snapshot(subset_bytes: bytes) -> ShareholdingSnapshot:
    return parse(subset_bytes, filename=SUBSET.name, l0_key=L0_KEY)


def _body(records: list[dict[str, Any]]) -> bytes:
    return json.dumps(records).encode("utf-8")


def _well_formed(record: dict[str, Any]) -> bool:
    value = record["isin"]
    return isinstance(value, str) and _ISIN.match(value) is not None


def _bad_isin_flags(snapshot: ShareholdingSnapshot) -> list[Any]:
    return [f for f in quality_findings(snapshot) if f.check_name == BAD_ISIN_CHECK]


def test_the_fixture_is_the_frozen_cut(subset_bytes: bytes, records: list[dict[str, Any]]) -> None:
    assert hashlib.sha256(subset_bytes).hexdigest() == SUBSET_SHA256
    assert len(records) == 41
    assert [r["isin"] for r in records[-2:]] == [ASAHI_RAW, PGHL_RAW]
    assert sum(1 for r in records if r["isin"] is None) == 1


# ── 1. excluded and flagged, never repaired ───────────────────────────────────────────────────


def test_malformed_isin_rows_are_excluded_and_the_good_rows_publish(
    snapshot: ShareholdingSnapshot, records: list[dict[str, Any]], tmp_path: Path
) -> None:
    assert [(m.record, m.raw_isin, m.symbol) for m in snapshot.skipped_bad_isin] == [
        (39, ASAHI_RAW, "ASAHIINDIA"),
        (40, PGHL_RAW, "PGHL"),
    ]
    assert len(snapshot.skipped_no_isin) == 1
    assert len(snapshot.rows) == 38

    # Never repaired: the rows are exactly the well-formed ISINs the payload stated, nothing more.
    stated = {r["isin"] for r in records if _well_formed(r)}
    assert {row.isin for row in snapshot.rows} == stated
    excluded = {m.name for m in snapshot.skipped_bad_isin}
    assert excluded == {"Asahi India Glass Limited", "Procter & Gamble Health Limited"}
    assert not excluded & {row.name for row in snapshot.rows}

    write_l1(snapshot, data_root=tmp_path)
    published = read_pit(date(2026, 10, 21), data_root=tmp_path)
    assert {row.isin for row in published} == stated
    assert not excluded & {row.name for row in published}


def test_each_excluded_row_raises_a_bad_isin_flag_with_its_raw_value_and_record_number(
    snapshot: ShareholdingSnapshot,
) -> None:
    flags = _bad_isin_flags(snapshot)
    assert sorted((f.detail["record"], f.detail["raw_isin"]) for f in flags) == [
        (39, ASAHI_RAW),
        (40, PGHL_RAW),
    ]
    for flag in flags:
        assert flag.severity == "WARN"
        assert flag.isin is None  # the literal is not an ISIN; it lives in `detail`
        assert flag.source == SOURCE_ID
        assert flag.logical_date == date(2026, 10, 16)  # both were broadcast on 16-Oct
        assert flag.detail["l0_key"] == L0_KEY
        assert flag.detail["period_end"] == "2026-09-30"
    assert {f.check_name for f in flags} == {"shareholding_bad_isin"}  # a sibling of no_isin
    assert len([f for f in quality_findings(snapshot) if f.check_name == NO_ISIN_CHECK]) == 1
    all_findings = quality_findings(snapshot)
    assert len({f.fingerprint for f in all_findings}) == len(all_findings)


def test_the_same_bad_filing_in_a_later_poll_is_the_same_flag(
    records: list[dict[str, Any]],
) -> None:
    """The cumulative master moves a filing's record number every day; the flag must not move."""
    first = parse(_body(records), filename="a.json")
    later = parse(_body(records[-2:] + records[:-2]), filename="b.json")
    assert {m.record for m in later.skipped_bad_isin} == {0, 1}
    assert {f.fingerprint for f in _bad_isin_flags(first)} == {
        f.fingerprint for f in _bad_isin_flags(later)
    }


def test_the_strict_pattern_stays_on_the_model() -> None:
    """The tolerance is the parser's decision about which records publish, not a looser row."""
    with pytest.raises(ValidationError, match=r"string_pattern_mismatch|should match pattern"):
        ShareholdingRow(
            isin=ASAHI_RAW,
            name="Asahi India Glass Limited",
            period_end=date(2026, 9, 30),
            filing_date=date(2026, 10, 16),
            promoter_holding_pct=Decimal("51.57"),
            public_pct=Decimal("48.43"),
            source=SOURCE_ID,
        )


# ── 2. a large share still fails whole ────────────────────────────────────────────────────────


def _without_good(records: list[dict[str, Any]], drop: int) -> list[dict[str, Any]]:
    """`records` less its first `drop` well-formed ones, order otherwise kept."""
    good = [i for i, r in enumerate(records) if _well_formed(r)][:drop]
    return [r for i, r in enumerate(records) if i not in good]


def test_the_threshold_is_five_percent() -> None:
    assert Decimal(5) == MAX_BAD_ISIN_PCT


def test_at_exactly_the_threshold_the_file_publishes(records: list[dict[str, Any]]) -> None:
    at = _without_good(records, 1)
    assert Decimal(100) * 2 / len(at) == MAX_BAD_ISIN_PCT  # 2 of 40
    parsed = parse(_body(at), filename="x.json")
    assert len(parsed.skipped_bad_isin) == 2
    assert len(parsed.rows) == 37


def test_a_file_over_the_threshold_still_fails(records: list[dict[str, Any]]) -> None:
    over = _without_good(records, 2)  # 2 of 39: 5.13%
    with pytest.raises(ParseError, match=r"2 of 39 records \(5\.13%\) state a malformed 'isin'"):
        parse(_body(over), filename="x.json")


def test_an_identifier_format_change_fails_the_file_rather_than_publishing_it_empty(
    records: list[dict[str, Any]],
) -> None:
    """Every ISIN lower-cased — a format change, not 40 bad filings. Never repaired by upper()."""
    lowered = [
        {**r, "isin": r["isin"].lower()} if isinstance(r["isin"], str) else r for r in records
    ]
    with pytest.raises(ParseError, match=r"40 of 41 records .* malformed 'isin'") as caught:
        parse(_body(lowered), filename="x.json")
    assert "record 0:" in str(caught.value)  # names the literals it saw


# ── 3. inverting the logic fails here ─────────────────────────────────────────────────────────


def test_no_well_formed_isin_in_the_fixture_is_flagged_or_dropped(
    snapshot: ShareholdingSnapshot, records: list[dict[str, Any]]
) -> None:
    stated = {r["isin"] for r in records if _well_formed(r)}
    assert len(stated) == 38
    assert stated <= {row.isin for row in snapshot.rows}
    assert not stated & {f.detail["raw_isin"] for f in _bad_isin_flags(snapshot)}
    assert not stated & {m.raw_isin for m in snapshot.skipped_bad_isin}


def _live_record(isin: object) -> dict[str, Any]:
    return {
        "broadcastDate": "06-OCT-2026 10:33:01",
        "cgTimeStamp": None,
        "date": "30-SEP-2026",
        "employeeTrusts": "0",
        "isin": isin,
        "name": "Indian Metals & Ferro Alloys Limited",
        "pr_and_prgrp": "58.69",
        "public_val": "41.31",
        "revisedData": "N",
        "symbol": "IMFA",
    }


@given(
    isin=st.from_regex(r"\A[A-Z]{2}[A-Z0-9]{9}[0-9]\Z", fullmatch=True),
    pad=st.sampled_from(["", " ", "\t", "  "]),
)
@settings(max_examples=200)
def test_a_well_formed_isin_is_never_flagged_or_dropped(isin: str, pad: str) -> None:
    parsed = parse(_body([_live_record(f"{pad}{isin}{pad}")]), filename="x.json")
    assert [row.isin for row in parsed.rows] == [isin]
    assert parsed.skipped_bad_isin == ()
    assert _bad_isin_flags(parsed) == []


@given(raw=st.text(min_size=1).filter(lambda s: bool(s.strip()) and not _ISIN.match(s.strip())))
@example(raw=ASAHI_RAW)
@example(raw=PGHL_RAW)
@example(raw="ine919h01018")
@example(raw="INE919H0101")
@settings(max_examples=200, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_a_malformed_isin_is_excluded_exactly_as_published_and_never_repaired(
    raw: str, records: list[dict[str, Any]]
) -> None:
    """One generated literal in place of a good record's ISIN, in a file of 39 (2.6%)."""
    base = [r for r in records if r["isin"] not in (ASAHI_RAW, PGHL_RAW)]
    target = next(i for i, r in enumerate(base) if _well_formed(r))
    replaced = base[target]["isin"]
    body = [{**r, "isin": raw} if i == target else r for i, r in enumerate(base)]
    parsed = parse(_body(body), filename="x.json")
    assert [(m.record, m.raw_isin) for m in parsed.skipped_bad_isin] == [(target, raw)]
    assert replaced not in {row.isin for row in parsed.rows}
    assert {row.isin for row in parsed.rows} == {r["isin"] for r in body if _well_formed(r)}


def test_the_excluded_set_refuses_a_well_formed_isin() -> None:
    for literal in (IMFA, f" {IMFA} "):
        with pytest.raises(ValidationError, match="well-formed ISIN"):
            MalformedIsinFiling(
                record=0,
                raw_isin=literal,
                name="Indian Metals & Ferro Alloys Limited",
                period_end=date(2026, 9, 30),
                filing_date=date(2026, 10, 6),
            )


# ── 4. structural faults stay loud ────────────────────────────────────────────────────────────


def test_a_non_string_isin_is_still_a_format_error(records: list[dict[str, Any]]) -> None:
    body = [*records[:-1], {**records[-1], "isin": 439}]
    with pytest.raises(ParseError, match="record 40: 'isin' is"):
        parse(_body(body), filename="x.json")


def test_a_bad_isin_record_whose_own_dates_do_not_parse_still_fails(
    records: list[dict[str, Any]],
) -> None:
    body = [*records[:-1], {**records[-1], "date": "30/09/2026"}]
    with pytest.raises(ParseError, match="record 40: date '30/09/2026' is not DD-Mon-YYYY"):
        parse(_body(body), filename="x.json")
