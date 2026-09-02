"""M7.3 — XBRL results filings → the true point-in-time fundamentals store.

The file is laid out as the three acceptance criteria, because each is a property a plausible wrong
implementation would quietly violate:

1. **Every datum carries `(period_end, filing_date)`, and the two are genuinely independent.** The
   period end is read from the XBRL document; the filing date is the exchange dissemination date
   from the announcements index that pointed at it. Proved by parsing two filings that share a
   period end (Kaynes's original and its restatement, both for 31-Mar-2026) but were disseminated
   months apart — no arithmetic on the period end could yield both filing dates, so the two fields
   cannot be one value in two costumes. A filing dated on or before its period is rejected.
2. **Standalone and consolidated are distinct records; segments are extracted.** Proved by parsing
   TCS's standalone and consolidated filings for the same quarter and reading back two different
   revenues keyed by `nature`, and by reading Kaynes's robotics/EMS segment revenues as exact
   `Decimal`s — the inputs §5.3 BC1 (built in M7.4) compares across quarters.
3. **A restatement is a new record, not an overwrite.** Proved end to end against the store: after
   Kaynes files 31-Mar-2026 twice (15-May then 20-Aug), both versions are physically present, a PIT
   read dated 01-Jun sees only the original, and `read_latest` supersedes to the restated value only
   once its filing date is knowable — invariant #7 made structural by partitioning on the filing
   (knowable) date, never overwriting the number the market originally saw.

The money assertions are written so inverting the logic fails them: a value stays a `Decimal`, never
a `float`; a value that is not a plain decimal is a parse error; and a restatement never destroys
the record it revises.
"""

from __future__ import annotations

import socket
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import pyarrow.parquet as pq
import pytest

from dataplatform.ingest.models import ParseError
from dataplatform.ingest.xbrl import (
    Filing,
    FilingIndexEntry,
    Nature,
    parse,
    parse_index,
)
from dataplatform.ingest.xbrl.parser import SEGMENT_CONCEPT
from dataplatform.store.paths import l1_partition_path
from dataplatform.store.pit_fundamentals import (
    PIT_FUNDAMENTALS_DATASET,
    read_l1,
    read_latest,
    read_pit,
    write_pit,
)

INDEX: Final = Path("tests/fixtures/xbrl/index_v1/corporates-financial-results_20260901.json")
FILINGS_DIR: Final = Path("tests/fixtures/xbrl/filing_v1")

TCS: Final = "INE467B01029"
KAYNES: Final = "INE918Z01012"

Q1FY27_END: Final = date(2026, 6, 30)
Q4FY26_END: Final = date(2026, 3, 31)
KAYNES_ORIGINAL_FILED: Final = date(2026, 5, 15)
KAYNES_RESTATED_FILED: Final = date(2026, 8, 20)
TCS_FILED: Final = date(2026, 7, 10)


# ── fixtures ─────────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any socket in this module is a bug: every filing is a frozen fixture (B8)."""

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a test opened a socket; xbrl tests are offline")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.fixture(scope="session")
def index_bytes(repo_root: Path) -> bytes:
    return (repo_root / INDEX).read_bytes()


@pytest.fixture
def entries(index_bytes: bytes) -> tuple[FilingIndexEntry, ...]:
    return parse_index(index_bytes, filename=INDEX.name)


def _entry(entries: tuple[FilingIndexEntry, ...], isin: str, filed: date) -> FilingIndexEntry:
    return next(e for e in entries if e.isin == isin and e.filing_date == filed)


def _load(entry: FilingIndexEntry, *, repo_root: Path) -> Filing:
    """Parse the XBRL fixture an index entry points at, threading the entry's filing date.

    Mirrors production: the XBRL URL and the first-knowable filing date both come from the index;
    the document is never reached by guessing, and the filing date is never read from the document.
    """
    name = Path(entry.xbrl_url).name
    payload = (repo_root / FILINGS_DIR / name).read_bytes()
    return parse(
        payload,
        filing_date=entry.filing_date,
        filing_id=entry.filing_id,
        isin=entry.isin,
        l0_key=f"nse_xbrl_filing/{entry.filing_date.isoformat()}/{name}",
        filename=name,
    )


@pytest.fixture
def all_filings(entries: tuple[FilingIndexEntry, ...], repo_root: Path) -> tuple[Filing, ...]:
    return tuple(_load(entry, repo_root=repo_root) for entry in entries)


def _company_value(filing: Filing, concept: str) -> Decimal:
    return next(f.value for f in filing.company_facts() if f.concept == concept)


def _segment_value(filing: Filing, segment: str) -> Decimal:
    return next(
        f.value
        for f in filing.segment_facts()
        if f.segment == segment and f.concept == SEGMENT_CONCEPT
    )


# ── acceptance 1: every datum carries (period_end, filing_date), independent ───────────────────


def test_every_datum_carries_both_dates(all_filings: tuple[Filing, ...]) -> None:
    """No fact exists without both a period end and a filing date."""
    assert all_filings  # the fixture set is non-empty
    for filing in all_filings:
        assert filing.facts
        for fact in filing.facts:
            assert isinstance(fact.period_end, date)
            assert isinstance(fact.filing_date, date)


def test_period_end_comes_from_the_document_filing_date_from_the_index(
    entries: tuple[FilingIndexEntry, ...], repo_root: Path
) -> None:
    """The two dates come from two independent places, so neither is derived from the other.

    Kaynes's original and restated filings share a period end (31-Mar-2026, from the XBRL) but were
    disseminated on different dates (15-May and 20-Aug, from the index). No function of the period
    end could produce both filing dates.
    """
    original = _load(_entry(entries, KAYNES, KAYNES_ORIGINAL_FILED), repo_root=repo_root)
    restated = _load(_entry(entries, KAYNES, KAYNES_RESTATED_FILED), repo_root=repo_root)

    assert original.period_end == restated.period_end == Q4FY26_END
    assert original.filing_date == KAYNES_ORIGINAL_FILED
    assert restated.filing_date == KAYNES_RESTATED_FILED
    for fact in (*original.facts, *restated.facts):
        assert fact.filing_date != fact.period_end


def test_discovery_reads_the_broadcast_date_not_the_period(
    entries: tuple[FilingIndexEntry, ...],
) -> None:
    """The index's `filing_date` is the dissemination date; `period_end` is the reporting period."""
    tcs = _entry(entries, TCS, TCS_FILED)
    assert tcs.filing_date == TCS_FILED
    assert tcs.period_end == Q1FY27_END
    assert tcs.period_start == date(2026, 4, 1)
    assert tcs.filing_date > tcs.period_end


def test_a_filing_dated_on_or_before_its_period_is_rejected(repo_root: Path) -> None:
    """Results are disseminated after the period closes; a filing dated on/before it is a leak."""
    payload = (repo_root / FILINGS_DIR / "INDAS_TCS_STANDALONE_10072026.xml").read_bytes()
    for bad in (Q1FY27_END, date(2026, 5, 1)):
        with pytest.raises(ParseError, match="not after the period end"):
            parse(payload, filing_date=bad, filing_id="x", filename="x.xml")


def test_the_reporting_period_is_read_from_the_document(repo_root: Path) -> None:
    """`period_end` is the document's own reporting-period element, not a context guess."""
    filing = _load_by_name("INDAS_KAYNES_STANDALONE_15052026.xml", repo_root=repo_root)
    assert filing.period_start == date(2026, 1, 1)
    assert filing.period_end == Q4FY26_END


def _load_by_name(name: str, *, repo_root: Path) -> Filing:
    payload = (repo_root / FILINGS_DIR / name).read_bytes()
    # A filing date after the period, so the PIT validator is satisfied; the point is the period.
    return parse(payload, filing_date=date(2026, 12, 31), filing_id=name, filename=name)


# ── acceptance 2: standalone vs consolidated distinguished; segments extracted ─────────────────


def test_standalone_and_consolidated_are_distinct_records(
    entries: tuple[FilingIndexEntry, ...], repo_root: Path
) -> None:
    """The same company and quarter, filed two ways, are two records with different figures."""
    standalone = next(
        _load(e, repo_root=repo_root)
        for e in entries
        if e.isin == TCS and e.nature is Nature.STANDALONE
    )
    consolidated = next(
        _load(e, repo_root=repo_root)
        for e in entries
        if e.isin == TCS and e.nature is Nature.CONSOLIDATED
    )
    assert standalone.nature is Nature.STANDALONE
    assert consolidated.nature is Nature.CONSOLIDATED
    assert standalone.period_end == consolidated.period_end == Q1FY27_END
    assert _company_value(standalone, "revenue_from_operations") == Decimal("630000000000")
    assert _company_value(consolidated, "revenue_from_operations") == Decimal("645000000000")
    assert _company_value(standalone, "revenue_from_operations") != _company_value(
        consolidated, "revenue_from_operations"
    )


def test_every_fact_carries_its_nature(all_filings: tuple[Filing, ...]) -> None:
    """Nature is part of a fact's identity, so a query cannot confuse the two."""
    for filing in all_filings:
        for fact in filing.facts:
            assert fact.nature is filing.nature


def test_segment_disclosures_are_extracted(repo_root: Path) -> None:
    """Kaynes discloses a robotics and an EMS segment — §5.3's example, BC1's inputs."""
    filing = _load_by_name("INDAS_KAYNES_STANDALONE_15052026.xml", repo_root=repo_root)
    assert filing.segments() == ("Automation and Robotics", "EMS")
    assert _segment_value(filing, "Automation and Robotics") == Decimal("3000000000")
    assert _segment_value(filing, "EMS") == Decimal("5000000000")


def test_segment_facts_share_the_filings_period_and_dates(repo_root: Path) -> None:
    """A segment datum is a first-class PIT fact — same period and filing date as the filing."""
    filing = _load_by_name("INDAS_TCS_STANDALONE_10072026.xml", repo_root=repo_root)
    for fact in filing.segment_facts():
        assert fact.concept == SEGMENT_CONCEPT
        assert fact.segment is not None
        assert fact.period_end == filing.period_end
        assert fact.filing_date == filing.filing_date


def test_every_value_is_a_decimal_never_a_float(all_filings: tuple[Filing, ...]) -> None:
    for filing in all_filings:
        for fact in filing.facts:
            # `type() is` (not isinstance) so the check is exact and not statically narrowed away:
            # the value must be precisely a Decimal even after a parquet round trip, never a float.
            value: object = fact.value
            assert type(value) is Decimal


def test_eps_is_parsed_as_an_exact_decimal(repo_root: Path) -> None:
    """A fractional value (EPS) round-trips exactly, not through a float."""
    filing = _load_by_name("INDAS_TCS_STANDALONE_10072026.xml", repo_root=repo_root)
    assert _company_value(filing, "eps_basic") == Decimal("33.50")
    assert _company_value(filing, "eps_diluted") == Decimal("33.45")


def test_a_comparative_prior_period_is_not_stored_as_this_filings_datum(repo_root: Path) -> None:
    """The standalone TCS filing carries a prior-year revenue context; only this period is kept."""
    filing = _load_by_name("INDAS_TCS_STANDALONE_10072026.xml", repo_root=repo_root)
    revenues = [f for f in filing.facts if f.concept == "revenue_from_operations"]
    assert len(revenues) == 1
    assert revenues[0].period_end == Q1FY27_END
    assert revenues[0].value == Decimal("630000000000")


# ── acceptance 3: a restatement is a new record, not an overwrite ──────────────────────────────


def test_a_restatement_is_stored_alongside_the_original(
    all_filings: tuple[Filing, ...], tmp_path: Path
) -> None:
    """Both Kaynes filings for 31-Mar-2026 are physically present after both are written."""
    for filing in all_filings:
        write_pit(filing, data_root=tmp_path)

    revenue = [
        f
        for f in read_pit(date(2026, 9, 1), data_root=tmp_path)
        if f.isin == KAYNES and f.concept == "revenue_from_operations"
    ]
    assert {f.filing_id for f in revenue} == {
        "KAYNES-Q4FY26-SA-3980551",
        "KAYNES-Q4FY26-SA-4290887",
    }
    assert {f.value for f in revenue} == {Decimal("8000000000"), Decimal("7800000000")}


def test_the_restatement_does_not_overwrite_the_original_partition(
    all_filings: tuple[Filing, ...], tmp_path: Path
) -> None:
    """The two filings land in different filing-date partitions — the original is untouched."""
    for filing in all_filings:
        write_pit(filing, data_root=tmp_path)

    original = read_l1(KAYNES_ORIGINAL_FILED, data_root=tmp_path)
    restated = read_l1(KAYNES_RESTATED_FILED, data_root=tmp_path)
    original_rev = next(f for f in original if f.concept == "revenue_from_operations")
    restated_rev = next(f for f in restated if f.concept == "revenue_from_operations")
    assert original_rev.value == Decimal("8000000000")
    assert restated_rev.value == Decimal("7800000000")


def test_pit_read_before_the_restatement_sees_only_the_original(
    all_filings: tuple[Filing, ...], tmp_path: Path
) -> None:
    """Invariant #7: as of 01-Jun the market had seen only the original filing."""
    for filing in all_filings:
        write_pit(filing, data_root=tmp_path)

    as_of_june = [
        f
        for f in read_pit(date(2026, 6, 1), data_root=tmp_path)
        if f.isin == KAYNES and f.concept == "revenue_from_operations"
    ]
    assert [f.filing_id for f in as_of_june] == ["KAYNES-Q4FY26-SA-3980551"]
    assert as_of_june[0].value == Decimal("8000000000")


def test_read_latest_supersedes_only_once_the_restatement_is_knowable(
    all_filings: tuple[Filing, ...], tmp_path: Path
) -> None:
    """`read_latest` gives the best knowledge as of a date, never reaching past it."""
    for filing in all_filings:
        write_pit(filing, data_root=tmp_path)

    def kaynes_revenue(on: date) -> Decimal:
        rows = [
            f
            for f in read_latest(on, data_root=tmp_path)
            if f.isin == KAYNES and f.concept == "revenue_from_operations"
        ]
        assert len(rows) == 1
        return rows[0].value

    assert kaynes_revenue(date(2026, 6, 1)) == Decimal("8000000000")  # original only
    assert kaynes_revenue(date(2026, 9, 1)) == Decimal("7800000000")  # restated now knowable


def test_the_robotics_segment_restatement_is_kept_distinct(
    all_filings: tuple[Filing, ...], tmp_path: Path
) -> None:
    """The segment §5.3 turns on is restated too, and both values survive (BC1 needs history)."""
    for filing in all_filings:
        write_pit(filing, data_root=tmp_path)

    robotics = [
        f
        for f in read_pit(date(2026, 9, 1), data_root=tmp_path)
        if f.isin == KAYNES and f.segment == "Automation and Robotics"
    ]
    assert {f.value for f in robotics} == {Decimal("3000000000"), Decimal("2800000000")}


# ── the PIT store: partitioning, round trip, lineage ───────────────────────────────────────────


def test_two_filings_of_the_same_day_share_a_partition_kept_apart_by_nature(
    all_filings: tuple[Filing, ...], tmp_path: Path
) -> None:
    """TCS standalone and consolidated, both filed 10-Jul, coexist in one partition."""
    for filing in all_filings:
        write_pit(filing, data_root=tmp_path)

    rows = read_l1(TCS_FILED, data_root=tmp_path)
    natures = {(f.nature, f.concept == "revenue_from_operations") for f in rows}
    assert (Nature.STANDALONE, True) in natures
    assert (Nature.CONSOLIDATED, True) in natures
    standalone_rev = next(
        f for f in rows if f.nature is Nature.STANDALONE and f.concept == "revenue_from_operations"
    )
    consolidated_rev = next(
        f
        for f in rows
        if f.nature is Nature.CONSOLIDATED and f.concept == "revenue_from_operations"
    )
    assert standalone_rev.value != consolidated_rev.value


def test_the_partition_key_is_the_filing_date_not_the_period_end(
    all_filings: tuple[Filing, ...], tmp_path: Path
) -> None:
    """Partitioning on the knowable date is what makes `read_pit` a partition prune."""
    for filing in all_filings:
        write_pit(filing, data_root=tmp_path)

    dataset_dir = tmp_path / "L1" / PIT_FUNDAMENTALS_DATASET
    partitions = {p.name for p in dataset_dir.iterdir() if p.is_dir()}
    assert partitions == {"date=2026-05-15", "date=2026-07-10", "date=2026-08-20"}
    # The period end (31-Mar) is data inside the rows, never a partition.
    assert not (dataset_dir / "date=2026-03-31").exists()


def test_a_partition_reads_back_identically(
    all_filings: tuple[Filing, ...], tmp_path: Path
) -> None:
    original = next(
        f for f in all_filings if f.isin == KAYNES and f.filing_date == KAYNES_ORIGINAL_FILED
    )
    write_pit(original, data_root=tmp_path)
    back = read_l1(KAYNES_ORIGINAL_FILED, data_root=tmp_path)
    assert {(f.concept, f.segment) for f in back} == {
        (f.concept, f.segment) for f in original.facts
    }
    assert next(f for f in back if f.concept == "profit_after_tax").value == Decimal("900000000")


def test_rewriting_a_partition_from_the_same_filings_is_byte_identical(
    all_filings: tuple[Filing, ...], tmp_path: Path
) -> None:
    """Idempotent per (dataset, filing_date): the M1.5 determinism rule (§4.2)."""
    tcs = [f for f in all_filings if f.isin == TCS]
    first = _write_and_read_bytes(tcs, tmp_path, TCS_FILED)
    second = _write_and_read_bytes(tcs, tmp_path, TCS_FILED)
    assert first == second


def _write_and_read_bytes(filings: list[Filing], root: Path, filed: date) -> bytes:
    for filing in filings:
        write_pit(filing, data_root=root)
    return l1_partition_path(PIT_FUNDAMENTALS_DATASET, filed, data_root=root).read_bytes()


def test_the_partition_carries_its_l0_lineage(
    all_filings: tuple[Filing, ...], tmp_path: Path
) -> None:
    for filing in all_filings:
        write_pit(filing, data_root=tmp_path)
    table = pq.read_table(
        l1_partition_path(PIT_FUNDAMENTALS_DATASET, KAYNES_ORIGINAL_FILED, data_root=tmp_path)
    )
    keys = set(table.column("l0_key").to_pylist())
    assert keys == {
        f"nse_xbrl_filing/{KAYNES_ORIGINAL_FILED.isoformat()}/INDAS_KAYNES_STANDALONE_15052026.xml"
    }


def test_the_l1_value_column_is_decimal(all_filings: tuple[Filing, ...], tmp_path: Path) -> None:
    write_pit(all_filings[0], data_root=tmp_path)
    schema = pq.read_schema(
        l1_partition_path(PIT_FUNDAMENTALS_DATASET, all_filings[0].filing_date, data_root=tmp_path)
    )
    assert str(schema.field("value").type) == "decimal128(38, 2)"


def test_read_pit_on_an_empty_lake_is_empty_not_an_error(tmp_path: Path) -> None:
    assert read_pit(date(2026, 9, 1), data_root=tmp_path) == ()


def test_reading_a_partition_that_was_never_written_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_l1(TCS_FILED, data_root=tmp_path)


def test_writing_the_same_filing_twice_is_idempotent(
    all_filings: tuple[Filing, ...], tmp_path: Path
) -> None:
    filing = all_filings[0]
    write_pit(filing, data_root=tmp_path)
    write_pit(filing, data_root=tmp_path)
    rows = read_l1(filing.filing_date, data_root=tmp_path)
    assert len(rows) == len(filing.facts)


# ── the index parser: shape and failure modes ─────────────────────────────────────────────────


def test_the_index_lists_every_filing(entries: tuple[FilingIndexEntry, ...]) -> None:
    assert len(entries) == 4
    assert {e.xbrl_url.rsplit("/", 1)[-1] for e in entries} == {
        "INDAS_TCS_STANDALONE_10072026.xml",
        "INDAS_TCS_CONSOLIDATED_10072026.xml",
        "INDAS_KAYNES_STANDALONE_15052026.xml",
        "INDAS_KAYNES_STANDALONE_20082026.xml",
    }


def test_the_index_distinguishes_the_two_kaynes_filings_by_seq(
    entries: tuple[FilingIndexEntry, ...],
) -> None:
    kaynes = [e for e in entries if e.isin == KAYNES]
    assert len({e.seq_number for e in kaynes}) == 2
    assert {e.filing_date for e in kaynes} == {KAYNES_ORIGINAL_FILED, KAYNES_RESTATED_FILED}


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b"", "empty response body"),
        (b"<html>Access Denied</html>", "markup, not JSON"),
        (b"{not json", "not valid JSON"),
        (b"{}", "expected a JSON array"),
        (b"[]", "JSON array is empty"),
        (b"[1, 2]", "not an object"),
    ],
)
def test_an_index_body_that_is_not_this_format_is_a_named_error(body: bytes, expected: str) -> None:
    with pytest.raises(ParseError, match=expected):
        parse_index(body, filename="index.json")


# ── the XBRL parser: failure modes ────────────────────────────────────────────────────────────


def _valid_xbrl(repo_root: Path) -> bytes:
    return (repo_root / FILINGS_DIR / "INDAS_TCS_STANDALONE_10072026.xml").read_bytes()


def test_a_body_that_is_not_xml_is_a_named_error() -> None:
    with pytest.raises(ParseError, match="not well-formed XML"):
        parse(
            b"<html>Access Denied", filing_date=date(2026, 12, 31), filing_id="x", filename="x.xml"
        )


def test_an_empty_body_is_a_named_error() -> None:
    with pytest.raises(ParseError, match="empty response body"):
        parse(b"   ", filing_date=date(2026, 12, 31), filing_id="x", filename="x.xml")


def test_a_non_xbrl_root_is_rejected() -> None:
    with pytest.raises(ParseError, match="not an XBRL"):
        parse(
            b"<?xml version='1.0'?><root/>",
            filing_date=date(2026, 12, 31),
            filing_id="x",
            filename="x.xml",
        )


def test_an_isin_mismatch_between_index_and_document_is_rejected(repo_root: Path) -> None:
    """The index and the document must name the same company (invariant #2 discipline)."""
    with pytest.raises(ParseError, match="name the same company"):
        parse(
            _valid_xbrl(repo_root),
            filing_date=TCS_FILED,
            filing_id="x",
            isin=KAYNES,
            filename="x.xml",
        )


def test_a_missing_nature_element_is_rejected(repo_root: Path) -> None:
    text = _valid_xbrl(repo_root).decode("utf-8")
    stripped = "\n".join(
        line for line in text.splitlines() if "NatureOfReportStandaloneConsolidated" not in line
    ).encode("utf-8")
    with pytest.raises(ParseError, match="NatureOfReportStandaloneConsolidated"):
        parse(stripped, filing_date=TCS_FILED, filing_id="x", filename="x.xml")


def test_a_filing_reporting_two_entities_is_rejected(repo_root: Path) -> None:
    text = _valid_xbrl(repo_root).decode("utf-8").replace("INE467B01029", "INE009A01021", 1)
    with pytest.raises(ParseError, match="more than one entity"):
        parse(text.encode("utf-8"), filing_date=TCS_FILED, filing_id="x", filename="x.xml")


def test_a_non_decimal_value_is_rejected(repo_root: Path) -> None:
    text = _valid_xbrl(repo_root).decode("utf-8").replace("630000000000", "NaN")
    with pytest.raises(ParseError, match="not a plain decimal"):
        parse(text.encode("utf-8"), filing_date=TCS_FILED, filing_id="x", filename="x.xml")
