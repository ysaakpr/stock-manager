"""M7.3 — XBRL results filings → the true point-in-time fundamentals store.

Every fixture here is a **captured** NSE response (`tests/fixtures/xbrl/README.md`), and that is the
point of this rewrite. The first version of this suite was green against hand-built documents
modelled on the taxonomy, and the parser it certified could not read one real filing: real filings
put several periods in one document, identify the entity by NSE symbol rather than ISIN, hang
segments off `xbrli:scenario` rather than `xbrli:segment`, and use element names the taxonomy
permits but the fabricated fixtures had guessed wrongly. A suite that cannot fail on the real
format's quirks is not evidence, so these tests are written against the quirks:

1. **Every datum carries `(period_end, filing_date)`, and the two are genuinely independent.** The
   period comes from the document (which column of the results table the index entry names); the
   filing date is the exchange dissemination date from the index. Proved by V.S.T Tillers, whose
   31-Dec-2024 quarter was filed on 11-Feb-2025 and re-filed on 30-Jul-2026 — one period end, two
   filing dates seventeen months apart, so no arithmetic on one could yield the other.
2. **Standalone and consolidated are distinct; segments are extracted.** Proved on real natures
   whose bottom lines genuinely differ, and on real multi-segment disclosures (Reliance, ITC,
   Grasim) whose segment revenues reconcile against the filing's own cross-segment total.
3. **A restatement is a new record, not an overwrite.** Proved with a real restatement: V.S.T
   Tillers' original filing overstated the quarter by exactly 10x and the 2026 re-filing corrected
   it. A PIT read dated 2025 must still return the *wrong* number the market actually saw — that is
   what invariant #7 protects, and an implementation that "helpfully" corrected history fails here.

The column model gets its own section, because selecting the wrong column is the failure mode that
does not look like a failure: it returns a plausible number for the wrong period.
"""

from __future__ import annotations

import json
import socket
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import pyarrow.parquet as pq
import pytest

from dataplatform.ingest.models import ParseError
from dataplatform.ingest.xbrl import (
    CONCEPT_KEYS,
    Filing,
    FilingIndexEntry,
    Nature,
    Taxonomy,
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

FIXTURES: Final = Path("tests/fixtures/xbrl")
INDEX: Final = FIXTURES / "index" / "corporates-financial-results_slice.json"
WINDOW_INDEX: Final = (
    FIXTURES / "index" / "corporates-financial-results_Quarterly_20260701_20260903.json"
)
FILINGS_DIR: Final = FIXTURES / "filings"

VSTTILLERS: Final = "INE764D01017"
RELIANCE: Final = "INE002A01018"
GRASIM: Final = "INE047A01013"
HDFCBANK: Final = "INE040A01018"
SCHAEFFLER: Final = "INE513A01014"
STANLEY: Final = "INE01A001028"
#: The older format eras, one company each (`tests/fixtures/xbrl/README.md`).
ALBK: Final = "INE428A01015"  # old banking `_WEB`: column contexts never declared
MCL: Final = "INE813V01014"  # `_WEB` with *no* declared context at all
TARACHAND: Final = "INE555Z01012"  # the Non-Ind-AS non-bank taxonomy
JKBANK: Final = "INE168A01017"  # identifies its entity by BSE scrip code
HEALTHX: Final = "INE019J01013"  # renamed: files as SASTASUNDR, indexed as HEALTHX
EMKAY: Final = "INE296H01011"  # an annual-only document listed under a quarterly entry
#: A sub-annual column whose period is stated only by `ReportingQuarter` + the financial year.
CAPTRUST: Final = "INE707C01018"

Q3FY25_START: Final = date(2024, 10, 1)
Q3FY25_END: Final = date(2024, 12, 31)
#: The cumulative column that shares Q3's end date — what an end-only period match would admit.
YTD_FY25_START: Final = date(2024, 4, 1)

VST_ORIGINAL_FILED: Final = date(2025, 2, 11)
VST_RESTATED_FILED: Final = date(2026, 7, 30)


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


def _entry(
    entries: tuple[FilingIndexEntry, ...],
    *,
    isin: str,
    nature: Nature | None = None,
    filed: date | None = None,
    period: str | None = None,
) -> FilingIndexEntry:
    """The one entry matching the given coordinates; a non-unique match is a broken fixture."""
    found = [
        entry
        for entry in entries
        if entry.isin == isin
        and (nature is None or entry.nature is nature)
        and (filed is None or entry.filing_date == filed)
        and (period is None or entry.period == period)
    ]
    assert len(found) == 1, f"expected 1 entry for {isin}/{nature}/{filed}/{period}, got {found}"
    return found[0]


#: Stands in for the D2 identity master, which the runner queries for an ISIN's symbol history and
#: which a unit test has no business opening a database for. Only one captured company has been
#: renamed; the rest resolve to the symbol their entry already carries.
KNOWN_SYMBOLS: Final[dict[str, frozenset[str]]] = {
    HEALTHX: frozenset({"HEALTHX", "SASTASUNDR"}),
}

#: Entries the parser must *refuse*, and why. Kept as data so a fixture that silently starts
#: parsing (or stops) shows up as a failure rather than as a quietly smaller corpus.
MUST_NOT_PARSE: Final[dict[str, str]] = {
    # Emkay's document holds only the cumulative column; its quarter's start is stated nowhere,
    # so answering the quarterly entry would mean storing a year's numbers under three months.
    "1068472": "no results column covers",
}


def _known_symbols(entry: FilingIndexEntry) -> frozenset[str]:
    return KNOWN_SYMBOLS.get(entry.isin, frozenset({entry.symbol}))


def _load(entry: FilingIndexEntry, *, repo_root: Path) -> Filing:
    """Parse the XBRL fixture an index entry points at, exactly as production does.

    The entry supplies everything the document cannot: the ISIN, the first-knowable filing date, and
    the reporting period that selects the column. `known_symbols` mirrors what the runner passes
    from the D2 master. Nothing is passed that production would not have.
    """
    assert entry.xbrl_url is not None
    name = entry.xbrl_url.rsplit("/", 1)[-1]
    payload = (repo_root / FILINGS_DIR / name).read_bytes()
    return parse(
        payload,
        entry=entry,
        known_symbols=_known_symbols(entry),
        l0_key=f"nse_xbrl_filing/{entry.filing_date.isoformat()}/{name}",
        filename=name,
    )


@pytest.fixture
def all_filings(entries: tuple[FilingIndexEntry, ...], repo_root: Path) -> tuple[Filing, ...]:
    """Every actionable entry the parser is meant to read, parsed.

    Excludes the entries in `MUST_NOT_PARSE` — and asserts each of them really does refuse, with
    the expected reason, so "the corpus got smaller" can never pass for "the corpus is clean".
    """
    filings: list[Filing] = []
    for entry in entries:
        if not entry.is_actionable:
            continue
        expected = MUST_NOT_PARSE.get(entry.seq_number)
        if expected is not None:
            with pytest.raises(ParseError, match=expected):
                _load(entry, repo_root=repo_root)
            continue
        filings.append(_load(entry, repo_root=repo_root))
    return tuple(filings)


@pytest.fixture
def vst_original(entries: tuple[FilingIndexEntry, ...], repo_root: Path) -> Filing:
    return _load(
        _entry(entries, isin=VSTTILLERS, nature=Nature.STANDALONE, filed=VST_ORIGINAL_FILED),
        repo_root=repo_root,
    )


@pytest.fixture
def vst_restated(entries: tuple[FilingIndexEntry, ...], repo_root: Path) -> Filing:
    return _load(
        _entry(entries, isin=VSTTILLERS, nature=Nature.STANDALONE, filed=VST_RESTATED_FILED),
        repo_root=repo_root,
    )


def _company_value(filing: Filing, concept: str) -> Decimal:
    return next(f.value for f in filing.company_facts() if f.concept == concept)


def _segment_value(filing: Filing, segment: str) -> Decimal:
    return next(
        f.value
        for f in filing.segment_facts()
        if f.segment == segment and f.concept == SEGMENT_CONCEPT
    )


def _mutate(repo_root: Path, name: str, old: str, new: str, *, count: int = -1) -> bytes:
    """A captured filing with one substitution — for the failure modes real data does not contain.

    Used only to break a *real* document in one named way. Fixtures are never edited on disk to
    make a test pass; the mutation lives in the test that needs it, where it is visible.
    """
    text = (repo_root / FILINGS_DIR / name).read_text(encoding="utf-8")
    assert old in text, f"{name} does not contain {old!r}; the fixture changed"
    return text.replace(old, new, count).encode("utf-8")


# ── acceptance 1: every datum carries (period_end, filing_date), independent ───────────────────


def test_every_datum_carries_both_dates(all_filings: tuple[Filing, ...]) -> None:
    """No fact exists without both a period end and a filing date, and never the same value."""
    assert len(all_filings) == 26
    for filing in all_filings:
        assert filing.facts
        for fact in filing.facts:
            assert isinstance(fact.period_end, date)
            assert isinstance(fact.filing_date, date)
            assert fact.filing_date > fact.period_end


def test_the_two_dates_come_from_two_independent_places(
    vst_original: Filing, vst_restated: Filing
) -> None:
    """One period end, two real filing dates 17 months apart — neither date derives the other."""
    assert vst_original.period_end == vst_restated.period_end == Q3FY25_END
    assert vst_original.period_start == vst_restated.period_start == Q3FY25_START
    assert vst_original.filing_date == VST_ORIGINAL_FILED
    assert vst_restated.filing_date == VST_RESTATED_FILED


def test_the_filing_date_is_the_broadcast_date_not_the_period(
    entries: tuple[FilingIndexEntry, ...],
) -> None:
    """`filing_date` is dissemination (`broadCastDate`); `period_end` is the reporting period."""
    entry = _entry(entries, isin=VSTTILLERS, nature=Nature.STANDALONE, filed=VST_RESTATED_FILED)
    assert entry.filing_date == VST_RESTATED_FILED
    assert entry.period_end == Q3FY25_END
    assert entry.period_start == Q3FY25_START
    assert entry.filing_date > entry.period_end


def test_a_filing_dated_on_or_before_its_period_is_rejected(
    entries: tuple[FilingIndexEntry, ...], repo_root: Path
) -> None:
    """Results are disseminated after the period closes; a filing dated on/before it is a leak."""
    entry = _entry(entries, isin=VSTTILLERS, nature=Nature.STANDALONE, filed=VST_RESTATED_FILED)
    assert entry.xbrl_url is not None
    name = entry.xbrl_url.rsplit("/", 1)[-1]
    payload = (repo_root / FILINGS_DIR / name).read_bytes()
    for bad in (Q3FY25_END, date(2024, 11, 1)):
        with pytest.raises(ParseError, match="not after the period end"):
            parse(payload, entry=entry.model_copy(update={"filing_date": bad}), filename=name)


# ── the column model: which period of the document this filing is ─────────────────────────────


def test_one_document_serves_a_quarterly_and_an_annual_entry(
    entries: tuple[FilingIndexEntry, ...], repo_root: Path
) -> None:
    """Schaeffler's December year-end document is linked by two entries, one per column.

    This is the format property the first parser had no model for. The *same bytes* must yield the
    quarter for the Quarterly entry and the full year for the Annual one — and the difference is
    not small: the annual revenue is ~4x the quarter's.
    """
    quarterly = _entry(entries, isin=SCHAEFFLER, nature=Nature.CONSOLIDATED, period="Quarterly")
    annual = _entry(entries, isin=SCHAEFFLER, nature=Nature.CONSOLIDATED, period="Annual")
    assert quarterly.xbrl_url == annual.xbrl_url  # one document, two entries

    quarter = _load(quarterly, repo_root=repo_root)
    year = _load(annual, repo_root=repo_root)

    assert (quarter.period_start, quarter.period_end) == (Q3FY25_START, Q3FY25_END)
    assert (year.period_start, year.period_end) == (date(2024, 1, 1), date(2024, 12, 31))
    assert _company_value(quarter, "revenue_from_operations") == Decimal("21360600000.00")
    assert _company_value(year, "revenue_from_operations") == Decimal("82323800000.00")


def test_the_cumulative_column_is_not_stored_as_the_quarter(
    vst_restated: Filing, repo_root: Path
) -> None:
    """A quarter's figures are the quarter's, not the year-to-date that shares its end date.

    The document holds both columns and gives them the *same* `xbrli:period`, so a parser that
    filtered on `period_end` alone would store the nine-month revenue as the quarter's. The check
    is exact: Q3 revenue is 2,191.0 crore against a 6,931.2 crore nine months.
    """
    assert _company_value(vst_restated, "revenue_from_operations") == Decimal("2191000000.00")
    for fact in vst_restated.facts:
        assert fact.period_start == Q3FY25_START
        assert fact.period_start != YTD_FY25_START


def test_a_period_no_column_reports_is_a_named_error(
    entries: tuple[FilingIndexEntry, ...], repo_root: Path
) -> None:
    """Asking a document for a period it does not carry fails loudly, naming what it does carry."""
    entry = _entry(entries, isin=VSTTILLERS, nature=Nature.STANDALONE, filed=VST_RESTATED_FILED)
    assert entry.xbrl_url is not None
    name = entry.xbrl_url.rsplit("/", 1)[-1]
    payload = (repo_root / FILINGS_DIR / name).read_bytes()
    wrong_period = entry.model_copy(
        update={"period_start": date(2023, 10, 1), "period_end": date(2023, 12, 31)}
    )
    with pytest.raises(ParseError, match="no results column covers"):
        parse(payload, entry=wrong_period, filename=name)


def test_the_column_period_is_read_from_facts_not_the_context_period(repo_root: Path) -> None:
    """The proof that `xbrli:period` is *not* what selects a column.

    Both of V.S.T Tillers' columns carry `xbrli:period` 2024-10-01→2024-12-31; only their in-context
    `DateOf…ReportingPeriod` facts differ. So the cumulative column is reachable — by asking for
    01-Apr→31-Dec, a period no `xbrli:period` in the document mentions.
    """
    name = "INDAS_121276_1705279_30072026051555.xml"
    payload = (repo_root / FILINGS_DIR / name).read_bytes()
    entries = parse_index((repo_root / INDEX).read_bytes(), filename=INDEX.name)
    entry = _entry(entries, isin=VSTTILLERS, nature=Nature.STANDALONE, filed=VST_RESTATED_FILED)
    cumulative = entry.model_copy(update={"period_start": YTD_FY25_START})
    filing = parse(payload, entry=cumulative, filename=name)
    assert filing.period_start == YTD_FY25_START
    assert filing.period_end == Q3FY25_END
    assert _company_value(filing, "revenue_from_operations") == Decimal("6931200000.00")


# ── acceptance 2: standalone vs consolidated distinguished; segments extracted ─────────────────


def test_standalone_and_consolidated_are_distinct_records(
    entries: tuple[FilingIndexEntry, ...], repo_root: Path
) -> None:
    """The same company and quarter, filed two ways, are two records with different bottom lines."""
    standalone = _load(
        _entry(entries, isin=VSTTILLERS, nature=Nature.STANDALONE, filed=VST_RESTATED_FILED),
        repo_root=repo_root,
    )
    consolidated = _load(
        _entry(entries, isin=VSTTILLERS, nature=Nature.CONSOLIDATED, filed=VST_RESTATED_FILED),
        repo_root=repo_root,
    )
    assert standalone.nature is Nature.STANDALONE
    assert consolidated.nature is Nature.CONSOLIDATED
    assert standalone.period_end == consolidated.period_end == Q3FY25_END
    # The group and the parent differ on the bottom line, which is why nature is part of identity.
    assert _company_value(standalone, "profit_after_tax") == Decimal("17000000.00")
    assert _company_value(consolidated, "profit_after_tax") == Decimal("12800000.00")


def test_the_feeds_non_consolidated_is_the_documents_standalone(
    entries: tuple[FilingIndexEntry, ...],
) -> None:
    """The defect that rejected every real entry: the feed never says `Standalone`.

    `consolidated` is `Non-Consolidated`/`Consolidated` in every one of a captured 3,816-record
    index; matching the enum's own spelling raised on all of them.
    """
    raw = json.loads(INDEX.read_text(encoding="utf-8"))
    assert {record["consolidated"] for record in raw} == {"Non-Consolidated", "Consolidated"}
    assert {entry.nature for entry in entries} == {Nature.STANDALONE, Nature.CONSOLIDATED}


def test_the_feeds_hyphenated_audited_flag_is_read(entries: tuple[FilingIndexEntry, ...]) -> None:
    """`Un-Audited` is the feed's spelling; matching `unaudited` read every one as "did not say"."""
    raw = json.loads(INDEX.read_text(encoding="utf-8"))
    assert "Un-Audited" in {record["audited"] for record in raw}
    assert {entry.audited for entry in entries} == {True, False}


def test_every_fact_carries_its_nature(all_filings: tuple[Filing, ...]) -> None:
    """Nature is part of a fact's identity, so a query cannot confuse the two."""
    for filing in all_filings:
        for fact in filing.facts:
            assert fact.nature is filing.nature


def test_real_segment_disclosures_are_extracted(
    entries: tuple[FilingIndexEntry, ...], repo_root: Path
) -> None:
    """Grasim's five reportable segments, named as it names them — §5.3 BC1's inputs."""
    filing = _load(_entry(entries, isin=GRASIM, nature=Nature.CONSOLIDATED), repo_root=repo_root)
    assert filing.segments() == (
        "Building Material",
        "Cellulosic Fibres",
        "Chemicals",
        "Financial Services",
        "Others",
    )
    assert _segment_value(filing, "Building Material") == Decimal("187840000000.00")
    assert _segment_value(filing, "Cellulosic Fibres") == Decimal("39340900000.00")


def test_the_company_level_segment_total_is_not_taken_as_a_segment(
    entries: tuple[FilingIndexEntry, ...], repo_root: Path
) -> None:
    """The column's own `SegmentRevenue` is the cross-segment total, not a segment's figure.

    Grasim's anchor column reports `SegmentRevenue` 351,546.8 — the gross total, which nets to the
    347,928.5 `RevenueFromOperations` after 3,618.3 of inter-segment revenue. Reading it as a
    segment would file the whole company under one segment name, so the segment facts must sum to
    the gross total and none of them may equal it.
    """
    filing = _load(_entry(entries, isin=GRASIM, nature=Nature.CONSOLIDATED), repo_root=repo_root)
    gross = Decimal("351546800000.00")
    segments = [f.value for f in filing.segment_facts()]
    assert sum(segments) == gross
    assert gross not in segments
    assert _company_value(filing, "revenue_from_operations") == Decimal("347928500000.00")


def test_segment_facts_are_scoped_to_the_selected_column(
    entries: tuple[FilingIndexEntry, ...], repo_root: Path
) -> None:
    """Each segment appears once per filing — not twice, once per column of the document."""
    filing = _load(_entry(entries, isin=RELIANCE, nature=Nature.CONSOLIDATED), repo_root=repo_root)
    names = [f.segment for f in filing.segment_facts()]
    assert len(names) == len(set(names)) == 5
    for fact in filing.segment_facts():
        assert fact.concept == SEGMENT_CONCEPT
        assert fact.period_start == filing.period_start
        assert fact.period_end == filing.period_end
        assert fact.filing_date == filing.filing_date
        assert fact.nature is filing.nature


def test_a_single_segment_company_reports_no_segments(vst_restated: Filing) -> None:
    """V.S.T Tillers declares itself single-segment; no segment facts is correct, not a failure."""
    assert vst_restated.segments() == ()
    assert vst_restated.segment_facts() == ()
    assert vst_restated.company_facts()


def test_a_filing_that_reports_zero_revenue_is_accepted(
    entries: tuple[FilingIndexEntry, ...], repo_root: Path
) -> None:
    """Kanani Industries really filed a zero revenue; a `revenue > 0` guard would reject it."""
    filing = _load(
        _entry(entries, isin="INE879E01029", nature=Nature.STANDALONE), repo_root=repo_root
    )
    assert _company_value(filing, "revenue_from_operations") == Decimal("0.00")
    assert _company_value(filing, "profit_after_tax") == Decimal("1757000.00")


# ── identity: the ISIN comes from the index, the symbol is cross-checked ──────────────────────


def test_the_isin_comes_from_the_index_never_the_document(
    entries: tuple[FilingIndexEntry, ...], repo_root: Path
) -> None:
    """HDFC Bank's document states an ISIN that disagrees with the index; the index wins.

    The document says `INE040A01034`, the index `INE040A01018`. Reading identity from a filer's
    typing would key a bank's facts under an ISIN the D2 master does not carry (invariant #2), so
    the document's ISIN element is never read at all.
    """
    document = (repo_root / FILINGS_DIR / "BANKING_117524_1359008_23012025122553.xml").read_text(
        encoding="utf-8"
    )
    assert "INE040A01034" in document
    assert "INE040A01018" not in document

    filing = _load(_entry(entries, isin=HDFCBANK, nature=Nature.STANDALONE), repo_root=repo_root)
    assert filing.isin == HDFCBANK
    assert all(fact.isin == HDFCBANK for fact in filing.facts)


def test_the_document_is_cross_checked_on_symbol(
    entries: tuple[FilingIndexEntry, ...], repo_root: Path
) -> None:
    """The symbol is the identity every real filing states reliably, so it must agree."""
    filing = _load(
        _entry(entries, isin=VSTTILLERS, nature=Nature.STANDALONE, filed=VST_RESTATED_FILED),
        repo_root=repo_root,
    )
    assert filing.symbol == "VSTTILLERS"


def test_a_symbol_mismatch_between_index_and_document_is_rejected(
    entries: tuple[FilingIndexEntry, ...], repo_root: Path
) -> None:
    """A document about another company must not be filed under this entry's ISIN."""
    entry = _entry(entries, isin=VSTTILLERS, nature=Nature.STANDALONE, filed=VST_RESTATED_FILED)
    assert entry.xbrl_url is not None
    name = entry.xbrl_url.rsplit("/", 1)[-1]
    payload = (repo_root / FILINGS_DIR / name).read_bytes()
    with pytest.raises(ParseError, match="name the same company"):
        parse(payload, entry=entry.model_copy(update={"symbol": "RELIANCE"}), filename=name)


def test_an_entity_identified_by_something_other_than_a_symbol_is_rejected(
    entries: tuple[FilingIndexEntry, ...], repo_root: Path
) -> None:
    """If NSE ever switched the identifier scheme, this must fail rather than compare wrong keys."""
    entry = _entry(entries, isin=VSTTILLERS, nature=Nature.STANDALONE, filed=VST_RESTATED_FILED)
    payload = _mutate(
        repo_root,
        "INDAS_121276_1705279_30072026051555.xml",
        'scheme="http://www.nseindia.com/NSESymbol"',
        'scheme="http://www.nseindia.com/ISIN"',
    )
    with pytest.raises(ParseError, match="identifier scheme"):
        parse(payload, entry=entry, filename="mutated.xml")


def test_a_filing_naming_two_entities_is_rejected(
    entries: tuple[FilingIndexEntry, ...], repo_root: Path
) -> None:
    entry = _entry(entries, isin=VSTTILLERS, nature=Nature.STANDALONE, filed=VST_RESTATED_FILED)
    payload = _mutate(
        repo_root,
        "INDAS_121276_1705279_30072026051555.xml",
        ">VSTTILLERS</xbrli:identifier>",
        ">RELIANCE</xbrli:identifier>",
        count=1,
    )
    with pytest.raises(ParseError, match="more than one entity"):
        parse(payload, entry=entry, filename="mutated.xml")


def test_a_nature_disagreement_between_index_and_document_is_rejected(
    entries: tuple[FilingIndexEntry, ...], repo_root: Path
) -> None:
    """The index and the selected column must agree on standalone/consolidated."""
    entry = _entry(entries, isin=VSTTILLERS, nature=Nature.STANDALONE, filed=VST_RESTATED_FILED)
    assert entry.xbrl_url is not None
    name = entry.xbrl_url.rsplit("/", 1)[-1]
    payload = (repo_root / FILINGS_DIR / name).read_bytes()
    with pytest.raises(ParseError, match="must describe the same filing"):
        parse(
            payload, entry=entry.model_copy(update={"nature": Nature.CONSOLIDATED}), filename=name
        )


# ── taxonomy families: one set of concept keys over two vocabularies ──────────────────────────


def test_the_banking_taxonomy_maps_onto_the_same_concept_keys(
    entries: tuple[FilingIndexEntry, ...], repo_root: Path
) -> None:
    """A bank's P&L has different element names; consumers still read one set of keys.

    HDFC Bank Q3 FY25: `InterestEarned` 76,006.88 crore is the operating revenue and `Income`
    87,460.44 crore the total — the two are distinct, which is what makes the mapping meaningful
    rather than an alias.
    """
    filing = _load(_entry(entries, isin=HDFCBANK, nature=Nature.STANDALONE), repo_root=repo_root)
    assert filing.taxonomy is Taxonomy.BANKING
    assert _company_value(filing, "revenue_from_operations") == Decimal("760068800000.00")
    assert _company_value(filing, "total_income") == Decimal("874604400000.00")
    assert _company_value(filing, "profit_after_tax") == Decimal("167355000000.00")


def test_the_nbfc_entry_point_reads_with_the_ind_as_vocabulary(
    entries: tuple[FilingIndexEntry, ...], repo_root: Path
) -> None:
    """Bajaj Finance files against the NBFC entry point but reports the Ind-AS P&L spine."""
    filing = _load(
        _entry(entries, isin="INE296A01016", nature=Nature.STANDALONE), repo_root=repo_root
    )
    assert filing.taxonomy is Taxonomy.IND_AS
    assert _company_value(filing, "revenue_from_operations") == Decimal("153710200000.00")
    assert _company_value(filing, "profit_after_tax") == Decimal("37058100000.00")


def test_every_filing_reports_the_core_concepts(all_filings: tuple[Filing, ...]) -> None:
    """Revenue, other income, total income, PAT and EPS resolve in every captured filing.

    The regression this pins: four of the original eight element names (`TotalIncome`,
    `TotalExpenses`, `BasicEarningsPerShare`, `DilutedEarningsPerShare`) exist in no real filing,
    so a filing "parsed successfully" with half its concepts silently missing.

    `profit_before_tax` is not core, and deliberately so. The oldest Non-Ind-AS form has no
    `ProfitBeforeTax` element at all — it reports `ProfitBeforeExtraordinaryItemsAndTax`, which is
    a *different* basis. Mapping that onto the same key would make one filing's PBT quietly
    incomparable with another's, so the fact is simply absent and a consumer gets nothing rather
    than a wrong number. `_concept_gaps` names exactly which filings that is true of, so a new gap
    appearing anywhere else fails.
    """
    core = {
        "revenue_from_operations",
        "other_income",
        "total_income",
        "total_expenses",
        "profit_after_tax",
        "eps_basic",
        "eps_diluted",
    }
    assert core < CONCEPT_KEYS
    for filing in all_filings:
        got = {f.concept for f in filing.company_facts()}
        assert core <= got, f"{filing.symbol} {filing.taxonomy.value} missing {core - got}"


def test_the_only_concept_any_captured_filing_lacks_is_the_documented_one(
    all_filings: tuple[Filing, ...],
) -> None:
    """The corpus's concept coverage is pinned, so a silent regression cannot hide as a gap."""
    gaps = {
        (filing.symbol, concept)
        for filing in all_filings
        for concept in CONCEPT_KEYS - {f.concept for f in filing.company_facts()}
    }
    assert gaps == {("EMKAY", "profit_before_tax")}


def test_an_unknown_taxonomy_entry_point_is_rejected(
    entries: tuple[FilingIndexEntry, ...], repo_root: Path
) -> None:
    """An unrecognised taxonomy must fail, not read zero concepts and call it a filing."""
    entry = _entry(entries, isin=VSTTILLERS, nature=Nature.STANDALONE, filed=VST_RESTATED_FILED)
    payload = _mutate(
        repo_root,
        "INDAS_121276_1705279_30072026051555.xml",
        'xlink:href="Ind-AS_entry_point_2020-03-31.xsd"',
        'xlink:href="martian_entry_point_2099-01-01.xsd"',
    )
    with pytest.raises(ParseError, match="not a results taxonomy"):
        parse(payload, entry=entry, filename="mutated.xml")


def test_eps_is_parsed_as_an_exact_decimal(vst_restated: Filing) -> None:
    """A fractional value round-trips exactly, not through a float."""
    assert _company_value(vst_restated, "eps_basic") == Decimal("1.97")
    assert _company_value(vst_restated, "eps_diluted") == Decimal("1.96")


def test_every_value_is_a_decimal_never_a_float(all_filings: tuple[Filing, ...]) -> None:
    for filing in all_filings:
        for fact in filing.facts:
            # `type() is` (not isinstance) so the check is exact and not statically narrowed away:
            # the value must be precisely a Decimal, never a float.
            value: object = fact.value
            assert type(value) is Decimal


def test_values_are_absolute_rupees_not_the_statements_rounding(
    entries: tuple[FilingIndexEntry, ...], repo_root: Path
) -> None:
    """`LevelOfRoundingUsedInFinancialStatements` describes the statement, not the XBRL.

    Reliance's filing says `Crores` and tags 2,438,650,000,000 for a quarter its statement prints
    as 243,865 crore — i.e. absolute rupees. A parser that scaled by the rounding label would be
    wrong by 1e7 here and by 1e5 on a `Lakhs` filing.
    """
    document = (repo_root / FILINGS_DIR / "INDAS_117297_1348248_16012025081520.xml").read_text(
        encoding="utf-8"
    )
    assert "<in-bse-fin:LevelOfRoundingUsedInFinancialStatements" in document
    assert ">Crores<" in document

    filing = _load(_entry(entries, isin=RELIANCE, nature=Nature.CONSOLIDATED), repo_root=repo_root)
    assert _company_value(filing, "revenue_from_operations") == Decimal("2438650000000.00")
    assert _company_value(filing, "profit_after_tax") == Decimal("219300000000.00")


# ── format eras: the same parser over a decade of the real feed ───────────────────────────────


def test_a_filing_whose_column_contexts_are_never_declared_parses(
    entries: tuple[FilingIndexEntry, ...], repo_root: Path
) -> None:
    """The 2018-2022 `…_WEB.xml` generation declares only its *dimensioned* contexts.

    Its facts reference `OneD`/`FourD`, which no `<context>` element defines. That is malformed
    XBRL and it is what NSE served for four years, covering tens of thousands of filings. Nothing
    is lost by reading it: a column's period, nature and audited flag are facts *inside* the
    column, never attributes of the `<context>`.
    """
    filing = _load(_entry(entries, isin=ALBK, nature=Nature.CONSOLIDATED), repo_root=repo_root)
    document = (repo_root / FILINGS_DIR / "BANKING_48497_136132_14092019023146_WEB.xml").read_text(
        encoding="utf-8"
    )
    assert 'contextRef="FourD"' in document
    assert '<xbrli:context id="FourD"' not in document  # the column is used but never declared

    assert filing.taxonomy is Taxonomy.BANKING
    assert (filing.period_start, filing.period_end) == (date(2018, 4, 1), date(2019, 3, 31))
    assert _company_value(filing, "profit_after_tax") == Decimal("-84573800000.00")


def test_a_filing_with_no_declared_contexts_at_all_parses(
    entries: tuple[FilingIndexEntry, ...], repo_root: Path
) -> None:
    """Some `…_WEB.xml` filings declare no `<context>` whatever — and so carry no entity element.

    Their `Symbol` fact is then the only identity they state, and it is enough to cross-check.
    """
    document = (repo_root / FILINGS_DIR / "NONINDAS_63114_354860_05112020010920_WEB.xml").read_text(
        encoding="utf-8"
    )
    assert "<xbrli:context" not in document
    assert "<xbrli:identifier" not in document

    filing = _load(_entry(entries, isin=MCL, nature=Nature.STANDALONE), repo_root=repo_root)
    assert filing.symbol == "MCL"
    assert (filing.period_start, filing.period_end) == (date(2019, 4, 1), date(2020, 3, 31))
    assert _company_value(filing, "revenue_from_operations") == Decimal("2022783000.00")


def test_the_header_block_is_not_read_as_the_quarter_columns_period(
    entries: tuple[FilingIndexEntry, ...], repo_root: Path
) -> None:
    """`DateOfStartOfFinancialYear` describes the *document*; filers pin it to the `OneD` context.

    Reading it as `OneD`'s own period is the subtle way this era goes wrong, and it is not a near
    miss: on an annual-only filing `OneD` is zero-filled and the cumulative `FourD` holds the year,
    so the mistake stores a bank's revenue as zero. Both columns carry the full concept set, so
    nothing about their contents distinguishes them either — only the period does.
    """
    document = (repo_root / FILINGS_DIR / "BANKING_48497_136132_14092019023146_WEB.xml").read_text(
        encoding="utf-8"
    )
    assert '<in-bse-fin:DateOfStartOfFinancialYear contextRef="OneD">2018-04-01' in document
    assert '<in-bse-fin:InterestEarned contextRef="OneD" unitRef="INR" decimals="-5">0.00' in (
        document
    )

    filing = _load(_entry(entries, isin=ALBK, nature=Nature.CONSOLIDATED), repo_root=repo_root)
    assert _company_value(filing, "revenue_from_operations") == Decimal("169157700000.00")
    assert _company_value(filing, "revenue_from_operations") != Decimal(0)


def test_an_annual_only_document_refuses_to_answer_for_a_quarter(
    entries: tuple[FilingIndexEntry, ...], repo_root: Path
) -> None:
    """A quarterly entry pointing at a document that holds only the year must fail, not guess.

    Emkay's document is named by both an Annual entry and a Quarterly one, but carries a single
    cumulative column. Its quarter's start appears nowhere in the document, so there is no honest
    period to store — and storing twelve months of numbers under three would be a silent error no
    downstream check could catch.
    """
    annual_entry = _entry(entries, isin=EMKAY, nature=Nature.CONSOLIDATED, period="Annual")
    quarterly_entry = _entry(entries, isin=EMKAY, nature=Nature.CONSOLIDATED, period="Quarterly")
    assert annual_entry.xbrl_url == quarterly_entry.xbrl_url  # one document, two entries

    annual = _load(annual_entry, repo_root=repo_root)
    assert (annual.period_start, annual.period_end) == (date(2018, 4, 1), date(2019, 3, 31))
    assert _company_value(annual, "revenue_from_operations") == Decimal("1479248000.00")

    with pytest.raises(ParseError, match="no results column covers"):
        _load(quarterly_entry, repo_root=repo_root)


def test_a_sub_annual_columns_period_comes_from_its_reporting_quarter(
    entries: tuple[FilingIndexEntry, ...], repo_root: Path
) -> None:
    """A quarter column that states no period still says *which* quarter it is.

    The 2018-2022 generation writes no `DateOf…ReportingPeriod` on a sub-annual filing. It writes
    `ReportingQuarter` — `First quarter` here — and the financial year, which together state the
    period completely. Reading them recovers filings that would otherwise be refused outright: 98
    of them in the FY2018-19 segment alone.

    The derivation cannot mislabel a period, and that is what makes it safe rather than a guess:
    whatever it produces must still equal the index entry's own period *exactly* before the column
    is selected, so a wrong derivation matches nothing and fails loudly.
    """
    document = (
        repo_root / FILINGS_DIR / "NONINDAS_38571_34193_11082018045941_WEB_2.xml"
    ).read_text(encoding="utf-8")
    assert "DateOfStartOfReportingPeriod" not in document  # no per-column period at all
    assert '<in-bse-fin:ReportingQuarter contextRef="OneD">First quarter' in document
    assert '<in-bse-fin:DateOfStartOfFinancialYear contextRef="OneD">2018-04-01' in document

    filing = _load(_entry(entries, isin=CAPTRUST, nature=Nature.CONSOLIDATED), repo_root=repo_root)
    # Q1 of a financial year starting 01-Apr — derived, then confirmed against the entry.
    assert (filing.period_start, filing.period_end) == (date(2018, 4, 1), date(2018, 6, 30))
    assert _company_value(filing, "revenue_from_operations") == Decimal("506491000.00")


def test_a_yearly_label_on_a_quarter_column_is_not_read_as_a_period(
    entries: tuple[FilingIndexEntry, ...], repo_root: Path
) -> None:
    """`ReportingQuarter = Yearly` on the quarter column says nothing about that column.

    This is the asymmetry that makes the rule above safe. On an annual-only filing the header
    block — financial year and `Yearly` — is pinned to `OneD`, but `OneD` is zero-filled and the
    year's numbers sit in the cumulative column. Treating `Yearly` as `OneD`'s own period would
    make two columns claim the same period, and the parser would either report a company's revenue
    as zero or refuse an annual filing that currently works. So `Yearly` is excluded from the
    sub-annual map, and Allahabad Bank's annual filing still resolves through the cumulative column.
    """
    document = (repo_root / FILINGS_DIR / "BANKING_48497_136132_14092019023146_WEB.xml").read_text(
        encoding="utf-8"
    )
    assert '<in-bse-fin:ReportingQuarter contextRef="OneD">Yearly' in document

    filing = _load(_entry(entries, isin=ALBK, nature=Nature.CONSOLIDATED), repo_root=repo_root)
    assert (filing.period_start, filing.period_end) == (date(2018, 4, 1), date(2019, 3, 31))
    assert _company_value(filing, "profit_after_tax") == Decimal("-84573800000.00")


def test_the_non_ind_as_taxonomy_maps_onto_the_same_concept_keys(
    entries: tuple[FilingIndexEntry, ...], repo_root: Path
) -> None:
    """The pre-Ind-AS form calls total income `Revenue`; consumers still read one set of keys."""
    filing = _load(_entry(entries, isin=TARACHAND, nature=Nature.STANDALONE), repo_root=repo_root)
    assert filing.taxonomy is Taxonomy.NON_IND_AS
    assert _company_value(filing, "revenue_from_operations") == Decimal("1410594000.00")
    assert _company_value(filing, "other_income") == Decimal("35014000.00")
    assert _company_value(filing, "total_income") == Decimal("1445608000.00")
    assert _company_value(filing, "profit_after_tax") == Decimal("93530000.00")
    # `Revenue` is the total-income line, not another name for operating revenue.
    assert _company_value(filing, "revenue_from_operations") + _company_value(
        filing, "other_income"
    ) == _company_value(filing, "total_income")
    assert filing.segments()


def test_an_entity_identified_by_bse_scrip_code_is_checked_on_its_symbol_fact(
    entries: tuple[FilingIndexEntry, ...], repo_root: Path
) -> None:
    """Some filings identify the entity by scrip code, which cannot be compared to a symbol.

    J&K Bank's document says `532209`. The `Symbol` fact says `J&KBANK`, which is what the
    cross-check uses — and the ISIN still comes from the index, never from either.
    """
    document = (repo_root / FILINGS_DIR / "BANKING_601183_475_25072022110219_WEB.xml").read_text(
        encoding="utf-8"
    )
    assert 'scheme="http://www.bseindia.com/bse-fin/ScripCode">532209' in document

    filing = _load(
        _entry(entries, isin=JKBANK, nature=Nature.CONSOLIDATED, period="Annual"),
        repo_root=repo_root,
    )
    assert filing.isin == JKBANK
    assert filing.symbol == "J&KBANK"
    assert _company_value(filing, "profit_after_tax") == Decimal("5044400000")


def test_a_renamed_company_is_accepted_against_its_symbol_history(
    entries: tuple[FilingIndexEntry, ...], repo_root: Path
) -> None:
    """A filing states the symbol it had when filed, not the one the index reports today.

    Sastasundar Ventures files as `SASTASUNDR` under an index entry that now says `HEALTHX`. The
    ISIN is unchanged — a rename does not touch it — so the D2 symbol history the caller passes is
    what lets the cross-check stay strict without rejecting every renamed company.
    """
    entry = _entry(entries, isin=HEALTHX, nature=Nature.CONSOLIDATED, period="Annual")
    assert entry.xbrl_url is not None
    name = entry.xbrl_url.rsplit("/", 1)[-1]
    payload = (repo_root / FILINGS_DIR / name).read_bytes()

    # Without the history, today's symbol is all there is to compare against, and it disagrees.
    with pytest.raises(ParseError, match="must name the same company"):
        parse(payload, entry=entry, filename=name)

    filing = parse(
        payload,
        entry=entry,
        known_symbols=frozenset({"HEALTHX", "SASTASUNDR"}),
        filename=name,
    )
    assert filing.isin == HEALTHX  # the join key is the index's, unaffected by the rename
    assert filing.symbol == "SASTASUNDR"  # what the filing itself said
    assert _company_value(filing, "profit_after_tax") == Decimal("-994692000.00")


def test_every_captured_filing_satisfies_its_own_income_identity(
    all_filings: tuple[Filing, ...],
) -> None:
    """`revenue + other income == total income`, exactly, in all three taxonomies.

    The strongest check available without outside knowledge of any company, and the one that
    catches a mis-mapped concept immediately: reading a bank's `Income` as its operating revenue,
    or the Non-Ind-AS `Revenue` as anything but the total, breaks it. So does selecting the wrong
    column, since a quarter's revenue against a year's other income does not add up.
    """
    checked = 0
    for filing in all_filings:
        v = {f.concept: f.value for f in filing.company_facts()}
        if not {"revenue_from_operations", "other_income", "total_income"} <= v.keys():
            continue
        checked += 1
        assert v["revenue_from_operations"] + v["other_income"] == v["total_income"], (
            f"{filing.symbol} {filing.taxonomy.value} {filing.period_end}"
        )
    assert checked == len(all_filings)


def test_a_banks_total_expenses_excludes_provisions_by_construction(
    entries: tuple[FilingIndexEntry, ...], repo_root: Path
) -> None:
    """Pins the one mapping caveat a ratio built on `total_expenses` inherits.

    For a bank the element is `ExpenditureExcludingProvisionsAndContingencies`, so
    `total_income - total_expenses` is *operating* profit and the gap to PBT is the provisions
    line. That is documented at `BANKING_CONCEPTS`; this makes it testable, so nobody later
    "fixes" the mapping to make the two agree.
    """
    filing = _load(
        _entry(entries, isin=JKBANK, nature=Nature.CONSOLIDATED, period="Annual"),
        repo_root=repo_root,
    )
    operating = _company_value(filing, "total_income") - _company_value(filing, "total_expenses")
    assert operating == Decimal("13734000000")
    # PBT is lower by the provisions the expense line leaves out — the two must not be equal.
    assert _company_value(filing, "profit_before_tax") == Decimal("7467200000")
    assert operating > _company_value(filing, "profit_before_tax")


# ── acceptance 3: a restatement is a new record, not an overwrite ──────────────────────────────


def test_the_real_restatement_changed_the_numbers(
    vst_original: Filing, vst_restated: Filing
) -> None:
    """V.S.T Tillers' original filing overstated the quarter by exactly 10x.

    Not a contrived pair: the 11-Feb-2025 filing reports 21,910,000,000 of revenue for a quarter
    the 30-Jul-2026 re-filing puts at 2,191,000,000. The later figure is the right one, and that
    is precisely why the earlier one must survive in the store — a 2025 backtest could only have
    seen the wrong number.
    """
    assert vst_original.filing_id != vst_restated.filing_id
    original = _company_value(vst_original, "revenue_from_operations")
    restated = _company_value(vst_restated, "revenue_from_operations")
    assert original == Decimal("21910000000.00")
    assert restated == Decimal("2191000000.00")
    assert original == restated * 10


def test_a_restatement_is_stored_alongside_the_original(
    all_filings: tuple[Filing, ...], tmp_path: Path
) -> None:
    """Both V.S.T Tillers filings for 31-Dec-2024 are physically present after both are written."""
    for filing in all_filings:
        write_pit(filing, data_root=tmp_path)

    revenue = [
        f
        for f in read_pit(date(2026, 9, 1), data_root=tmp_path)
        if f.isin == VSTTILLERS
        and f.nature is Nature.STANDALONE
        and f.concept == "revenue_from_operations"
    ]
    assert len({f.filing_id for f in revenue}) == 2
    assert {f.value for f in revenue} == {Decimal("21910000000.00"), Decimal("2191000000.00")}


def test_the_restatement_does_not_overwrite_the_original_partition(
    all_filings: tuple[Filing, ...], tmp_path: Path
) -> None:
    """The two filings land in different filing-date partitions — the original is untouched."""
    for filing in all_filings:
        write_pit(filing, data_root=tmp_path)

    def standalone_revenue(filed: date) -> Decimal:
        rows = [
            f
            for f in read_l1(filed, data_root=tmp_path)
            if f.isin == VSTTILLERS
            and f.nature is Nature.STANDALONE
            and f.concept == "revenue_from_operations"
        ]
        assert len(rows) == 1
        return rows[0].value

    assert standalone_revenue(VST_ORIGINAL_FILED) == Decimal("21910000000.00")
    assert standalone_revenue(VST_RESTATED_FILED) == Decimal("2191000000.00")


def test_pit_read_before_the_restatement_sees_only_the_original(
    all_filings: tuple[Filing, ...], tmp_path: Path
) -> None:
    """Invariant #7: in 2025 the market had seen only the (wrong) original figure."""
    for filing in all_filings:
        write_pit(filing, data_root=tmp_path)

    as_of = [
        f
        for f in read_pit(date(2025, 6, 1), data_root=tmp_path)
        if f.isin == VSTTILLERS
        and f.nature is Nature.STANDALONE
        and f.concept == "revenue_from_operations"
    ]
    assert len(as_of) == 1
    assert as_of[0].value == Decimal("21910000000.00")
    assert as_of[0].filing_date == VST_ORIGINAL_FILED


def test_read_latest_supersedes_only_once_the_restatement_is_knowable(
    all_filings: tuple[Filing, ...], tmp_path: Path
) -> None:
    """`read_latest` gives the best knowledge as of a date, never reaching past it."""
    for filing in all_filings:
        write_pit(filing, data_root=tmp_path)

    def revenue(on: date) -> Decimal:
        rows = [
            f
            for f in read_latest(on, data_root=tmp_path)
            if f.isin == VSTTILLERS
            and f.nature is Nature.STANDALONE
            and f.concept == "revenue_from_operations"
        ]
        assert len(rows) == 1
        return rows[0].value

    assert revenue(date(2025, 6, 1)) == Decimal("21910000000.00")  # original only
    assert revenue(date(2026, 9, 1)) == Decimal("2191000000.00")  # correction now knowable


def test_a_segment_disclosure_withdrawn_by_a_refiling_keeps_its_history(
    entries: tuple[FilingIndexEntry, ...], repo_root: Path, tmp_path: Path
) -> None:
    """Stanley Lifestyles disclosed a segment, then re-filed without it — BC1 needs both states.

    A real case the fabricated fixtures could not have produced: the segment history must not be
    rewritten by a later filing that simply stopped disclosing it.
    """
    original = _load(
        _entry(entries, isin=STANLEY, nature=Nature.CONSOLIDATED, filed=date(2025, 2, 12)),
        repo_root=repo_root,
    )
    refiled = _load(
        _entry(entries, isin=STANLEY, nature=Nature.CONSOLIDATED, filed=date(2025, 3, 21)),
        repo_root=repo_root,
    )
    assert "Manufacture of Furniture" in original.segments()
    assert "Manufacture of Furniture" not in refiled.segments()

    write_pit(original, data_root=tmp_path)
    write_pit(refiled, data_root=tmp_path)
    stored = [
        f
        for f in read_pit(date(2025, 6, 1), data_root=tmp_path)
        if f.isin == STANLEY and f.segment == "Manufacture of Furniture"
    ]
    assert [f.value for f in stored] == [Decimal("1154000000.00")]


# ── the PIT store: partitioning, round trip, lineage ───────────────────────────────────────────


def test_two_filings_of_the_same_day_share_a_partition_kept_apart_by_nature(
    all_filings: tuple[Filing, ...], tmp_path: Path
) -> None:
    """Standalone and consolidated, both filed 30-Jul-2026, coexist in one partition."""
    for filing in all_filings:
        write_pit(filing, data_root=tmp_path)

    rows = [
        f
        for f in read_l1(VST_RESTATED_FILED, data_root=tmp_path)
        if f.isin == VSTTILLERS and f.concept == "profit_after_tax"
    ]
    by_nature = {f.nature: f.value for f in rows}
    assert by_nature == {
        Nature.STANDALONE: Decimal("17000000.00"),
        Nature.CONSOLIDATED: Decimal("12800000.00"),
    }


def test_the_partition_key_is_the_filing_date_not_the_period_end(
    all_filings: tuple[Filing, ...], tmp_path: Path
) -> None:
    """Partitioning on the knowable date is what makes `read_pit` a partition prune."""
    for filing in all_filings:
        write_pit(filing, data_root=tmp_path)

    dataset_dir = tmp_path / "L1" / PIT_FUNDAMENTALS_DATASET
    partitions = {p.name for p in dataset_dir.iterdir() if p.is_dir()}
    assert f"date={VST_ORIGINAL_FILED.isoformat()}" in partitions
    assert f"date={VST_RESTATED_FILED.isoformat()}" in partitions
    # Every partition is a filing date; the period end (31-Dec-2024) is data inside the rows.
    assert not (dataset_dir / "date=2024-12-31").exists()
    assert partitions == {f"date={f.filing_date.isoformat()}" for f in all_filings}


def test_a_partition_reads_back_identically(vst_original: Filing, tmp_path: Path) -> None:
    write_pit(vst_original, data_root=tmp_path)
    back = read_l1(VST_ORIGINAL_FILED, data_root=tmp_path)
    assert {(f.concept, f.segment) for f in back} == {
        (f.concept, f.segment) for f in vst_original.facts
    }
    assert next(f for f in back if f.concept == "profit_after_tax").value == Decimal("170000000.00")


def test_rewriting_a_partition_from_the_same_filings_is_byte_identical(
    all_filings: tuple[Filing, ...], tmp_path: Path
) -> None:
    """Idempotent per (dataset, filing_date): the M1.5 determinism rule (§4.2)."""
    same_day = [f for f in all_filings if f.filing_date == VST_RESTATED_FILED]
    assert len(same_day) == 2
    first = _write_and_read_bytes(same_day, tmp_path, VST_RESTATED_FILED)
    second = _write_and_read_bytes(same_day, tmp_path, VST_RESTATED_FILED)
    assert first == second


def _write_and_read_bytes(filings: list[Filing], root: Path, filed: date) -> bytes:
    for filing in filings:
        write_pit(filing, data_root=root)
    return l1_partition_path(PIT_FUNDAMENTALS_DATASET, filed, data_root=root).read_bytes()


def test_the_partition_carries_its_l0_lineage(vst_original: Filing, tmp_path: Path) -> None:
    write_pit(vst_original, data_root=tmp_path)
    table = pq.read_table(
        l1_partition_path(PIT_FUNDAMENTALS_DATASET, VST_ORIGINAL_FILED, data_root=tmp_path)
    )
    assert set(table.column("l0_key").to_pylist()) == {
        f"nse_xbrl_filing/{VST_ORIGINAL_FILED.isoformat()}/INDAS_119528_1377589_11022025120304.xml"
    }


def test_the_l1_value_column_is_decimal(vst_original: Filing, tmp_path: Path) -> None:
    write_pit(vst_original, data_root=tmp_path)
    schema = pq.read_schema(
        l1_partition_path(PIT_FUNDAMENTALS_DATASET, VST_ORIGINAL_FILED, data_root=tmp_path)
    )
    assert str(schema.field("value").type) == "decimal128(38, 4)"


def test_a_three_decimal_eps_survives_the_write(vst_original: Filing, tmp_path: Path) -> None:
    """Scale 4, not 2 — and the shortfall was silent in the worst way.

    `pyarrow` refuses to rescale a `Decimal` that would lose data, so a filing reporting EPS to
    three places parsed perfectly and then failed its *write* with `ArrowInvalid`. It cost 250
    filings of a decade-long campaign and was invisible until the failure classes were tallied.
    Rounding would have been the wrong fix: 1.234 truncated to 1.23 is a 0.3% error in the
    denominator of every P/E built on it.
    """
    eps = next(f for f in vst_original.facts if f.concept == "eps_basic")
    three_dp = eps.model_copy(update={"value": Decimal("1.234")})
    filing = vst_original.model_copy(
        update={"facts": tuple(f for f in vst_original.facts if f is not eps) + (three_dp,)}
    )
    write_pit(filing, data_root=tmp_path)

    back = read_l1(VST_ORIGINAL_FILED, data_root=tmp_path)
    stored = next(f for f in back if f.concept == "eps_basic")
    assert stored.value == Decimal("1.234")  # exact, not 1.23


def test_read_pit_on_an_empty_lake_is_empty_not_an_error(tmp_path: Path) -> None:
    assert read_pit(date(2026, 9, 1), data_root=tmp_path) == ()


def test_reading_a_partition_that_was_never_written_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_l1(VST_RESTATED_FILED, data_root=tmp_path)


def test_writing_the_same_filing_twice_is_idempotent(vst_original: Filing, tmp_path: Path) -> None:
    write_pit(vst_original, data_root=tmp_path)
    write_pit(vst_original, data_root=tmp_path)
    rows = read_l1(VST_ORIGINAL_FILED, data_root=tmp_path)
    assert len(rows) == len(vst_original.facts)


# ── the index parser: shape and failure modes ─────────────────────────────────────────────────


def test_the_index_parses_every_real_record(entries: tuple[FilingIndexEntry, ...]) -> None:
    """Every record in the captured slice parses — the whole point of the rewrite."""
    raw = json.loads(INDEX.read_text(encoding="utf-8"))
    assert len(entries) == len(raw) == 28


def test_a_whole_live_index_response_parses(repo_root: Path) -> None:
    """A second captured response, taken verbatim over a date window, parses end to end."""
    entries = parse_index((repo_root / WINDOW_INDEX).read_bytes(), filename=WINDOW_INDEX.name)
    assert len(entries) == 6
    assert all(entry.filing_date > entry.period_end for entry in entries)
    assert {entry.symbol for entry in entries} == {
        "AHLWEST",
        "KANANIIND",
        "VSTTILLERS",
        "IL&FSTRANS",
    }


def test_an_announcement_with_no_xbrl_document_is_not_actionable(
    entries: tuple[FilingIndexEntry, ...],
) -> None:
    """The feed spells "no attachment" as an archive path ending in `-`; that is not a URL.

    It parses (the record is well-formed and its dates are real) but reports itself
    non-actionable, so an ingest runner counts it rather than the parser dropping it silently.
    """
    missing = [entry for entry in entries if not entry.is_actionable]
    assert len(missing) == 1
    assert missing[0].xbrl_url is None
    assert all(entry.xbrl_url is not None for entry in entries if entry.is_actionable)


def test_the_index_keys_restatements_apart_by_seq_number(
    entries: tuple[FilingIndexEntry, ...],
) -> None:
    vst = [e for e in entries if e.isin == VSTTILLERS and e.nature is Nature.STANDALONE]
    assert len({e.seq_number for e in vst}) == len(vst) == 2
    assert {e.filing_date for e in vst} == {VST_ORIGINAL_FILED, VST_RESTATED_FILED}
    assert len({e.filing_id for e in vst}) == 2


def test_index_entries_are_sorted_deterministically(index_bytes: bytes) -> None:
    """A re-parse of the same payload yields the same order (replay determinism)."""
    once = parse_index(index_bytes, filename=INDEX.name)
    twice = parse_index(index_bytes, filename=INDEX.name)
    assert once == twice


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


def test_an_unknown_consolidated_value_is_a_named_error() -> None:
    """A third value in `consolidated` must fail loudly, not be guessed at."""
    record = json.loads(INDEX.read_text(encoding="utf-8"))[0]
    record["consolidated"] = "Semi-Consolidated"
    with pytest.raises(ParseError, match="expected one of"):
        parse_index(json.dumps([record]).encode("utf-8"), filename="index.json")


# ── the XBRL parser: failure modes ────────────────────────────────────────────────────────────


@pytest.fixture
def any_entry(entries: tuple[FilingIndexEntry, ...]) -> FilingIndexEntry:
    return _entry(entries, isin=VSTTILLERS, nature=Nature.STANDALONE, filed=VST_RESTATED_FILED)


def test_a_body_that_is_not_xml_is_a_named_error(any_entry: FilingIndexEntry) -> None:
    with pytest.raises(ParseError, match="not well-formed XML"):
        parse(b"<html>Access Denied", entry=any_entry, filename="x.xml")


def test_an_empty_body_is_a_named_error(any_entry: FilingIndexEntry) -> None:
    with pytest.raises(ParseError, match="empty response body"):
        parse(b"   ", entry=any_entry, filename="x.xml")


def test_a_non_xbrl_root_is_rejected(any_entry: FilingIndexEntry) -> None:
    with pytest.raises(ParseError, match="not an XBRL"):
        parse(b"<?xml version='1.0'?><root/>", entry=any_entry, filename="x.xml")


def test_an_entry_with_no_period_start_cannot_select_a_column(
    any_entry: FilingIndexEntry, repo_root: Path
) -> None:
    """Without a period start there is no way to say which column the entry means."""
    assert any_entry.xbrl_url is not None
    name = any_entry.xbrl_url.rsplit("/", 1)[-1]
    payload = (repo_root / FILINGS_DIR / name).read_bytes()
    with pytest.raises(ParseError, match="cannot be identified"):
        parse(payload, entry=any_entry.model_copy(update={"period_start": None}), filename=name)


def test_a_missing_nature_element_is_rejected(any_entry: FilingIndexEntry, repo_root: Path) -> None:
    """No column declares a nature → there is no results column at all."""
    payload = _mutate(
        repo_root,
        "INDAS_121276_1705279_30072026051555.xml",
        "NatureOfReportStandaloneConsolidated",
        "NatureOfReportSomethingElse",
    )
    with pytest.raises(ParseError, match="no results column"):
        parse(payload, entry=any_entry, filename="mutated.xml")


def test_a_non_decimal_value_is_rejected(any_entry: FilingIndexEntry, repo_root: Path) -> None:
    payload = _mutate(
        repo_root, "INDAS_121276_1705279_30072026051555.xml", ">2191000000.00<", ">NaN<"
    )
    with pytest.raises(ParseError, match="not a plain decimal"):
        parse(payload, entry=any_entry, filename="mutated.xml")


def test_a_column_reporting_none_of_the_concepts_is_rejected(
    any_entry: FilingIndexEntry, repo_root: Path
) -> None:
    """An empty parse must fail, not record "we looked and found nothing" as a successful ingest."""
    payload = _mutate(
        repo_root, "INDAS_121276_1705279_30072026051555.xml", "<in-bse-fin:", "<in-bse-unknown:"
    )
    with pytest.raises(ParseError):
        parse(payload, entry=any_entry, filename="mutated.xml")


def test_a_duplicate_concept_in_one_column_is_rejected(
    any_entry: FilingIndexEntry, repo_root: Path
) -> None:
    """One column reports each concept once; two values means neither can be trusted."""
    original = '<in-bse-fin:ProfitLossForPeriod contextRef="OneD" unitRef="INR" decimals="-5">'
    payload = _mutate(
        repo_root,
        "INDAS_121276_1705279_30072026051555.xml",
        original,
        original + "1.00</in-bse-fin:ProfitLossForPeriod>" + original,
        count=1,
    )
    with pytest.raises(ParseError, match="reports ProfitLossForPeriod 2 times"):
        parse(payload, entry=any_entry, filename="mutated.xml")
