"""The Integrated Filing (Financials) feed: SEBI's post-March-2025 results path, on real documents.

Every XBRL fixture under `tests/fixtures/xbrl/integrated/` is a document captured verbatim from
`nsearchives.nseindia.com` on 2026-09-06 through the project's own fetch headers (B8, and the M7
lesson: a parser proved only on authored fixtures has proved nothing). The index-page fixture is
the first page of the May-2025 window, captured by the runner into L0 and copied unchanged.

What is pinned:

* the parser reads an Integrated Filing document with the same concept keys it reads the old
  `in-bse-fin` documents with — revenue, PAT, EPS, paid-up capital, face value, owners' profit —
  under the `in-capmkt-ent` entry point and SEBI's symbol scheme, in absolute rupees whatever the
  document's `LevelOfRounding` says;
* a fourth-quarter document yields both the quarter (`OneD`) and the financial year (`FourD`)
  through two index entries, and the annual column carries the balance-sheet reserves element;
* the `_NONINDAS_` variant parses with the same vocabulary;
* the document's own `ISIN` fact is cross-checked against the entry's — a mismatch is refused;
* the feed parser derives the quarter start from `qe_Date`, resolves the symbol through the
  injected resolver as of the dissemination date, names what it could not resolve, treats the
  archive's `-` placeholder as "no document", reads an empty page as zero entries, and prefixes
  ids so they cannot collide with the old feed's;
* the page planner covers every calendar month with the stated number of pages.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from dataplatform.ingest.models import ParseError
from dataplatform.ingest.source_register import load as load_register
from dataplatform.ingest.xbrl import integrated, parser
from dataplatform.ingest.xbrl.discovery import FilingIndexEntry
from dataplatform.ingest.xbrl.models import Nature, Taxonomy

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "xbrl" / "integrated"

MOLBIO_C = "INTEGRATED_FILING_INDAS_1721824_05092026073816_WEB.xml"
MOLBIO_S = "INTEGRATED_FILING_INDAS_1721823_05092026073748_WEB.xml"
GSS_Q4 = "INTEGRATED_FILING_INDAS_1461892_31052025112035_WEB.xml"
AHLUCONT_NONINDAS = "INTEGRATED_FILING_NONINDAS_1461661_31052025042804_WEB.xml"

MOLBIO = "INE869T01028"
GSS = "INE871H01011"
AHLUCONT = "INE758C01029"


def _entry(
    *,
    isin: str,
    symbol: str,
    period_start: date,
    period_end: date,
    filing_date: date,
    nature: Nature,
    seq: str,
    period: str = "Quarterly",
) -> FilingIndexEntry:
    return FilingIndexEntry(
        isin=isin,
        symbol=symbol,
        name=symbol,
        period_start=period_start,
        period_end=period_end,
        filing_date=filing_date,
        nature=nature,
        audited=False,
        period=period,
        xbrl_url=f"https://nsearchives.nseindia.com/corporate/xbrl/{seq}.xml",
        seq_number=seq,
    )


def _facts(filename: str, entry: FilingIndexEntry, symbol: str) -> dict[str, Decimal]:
    filing = parser.parse(
        (FIXTURES / filename).read_bytes(),
        entry=entry,
        known_symbols=frozenset({symbol}),
        filename=filename,
        l0_key=f"fixture/{filename}",
    )
    assert filing.taxonomy is Taxonomy.IND_AS
    return {fact.concept: fact.value for fact in filing.facts if fact.segment is None}


# ── the documents ───────────────────────────────────────────────────────────────────────────────


def test_a_first_quarter_integrated_filing_parses_to_the_stored_concepts_in_rupees() -> None:
    entry = _entry(
        isin=MOLBIO,
        symbol="MOLBIO",
        period_start=date(2026, 4, 1),
        period_end=date(2026, 6, 30),
        filing_date=date(2026, 9, 5),
        nature=Nature.CONSOLIDATED,
        seq="IF192487",
    )
    facts = _facts(MOLBIO_C, entry, "MOLBIO")
    # The document says LevelOfRounding = Millions; the values are nonetheless absolute rupees
    # (₹408.4 crore of quarterly revenue for a company of this size), so nothing is rescaled.
    assert facts["revenue_from_operations"] == Decimal("4083760000")
    assert facts["profit_after_tax"] == Decimal("527440000")
    assert facts["profit_attributable_to_owners"] == Decimal("587470000")
    assert facts["paid_up_equity_capital"] == Decimal("112760000")
    assert facts["face_value_per_share"] == Decimal("1")
    assert facts["eps_basic"] == Decimal("5.21")
    assert facts["shares_outstanding"] == Decimal("112760000")  # paid-up / face value, corroborated


def test_the_standalone_document_of_the_same_filing_parses_as_standalone() -> None:
    entry = _entry(
        isin=MOLBIO,
        symbol="MOLBIO",
        period_start=date(2026, 4, 1),
        period_end=date(2026, 6, 30),
        filing_date=date(2026, 9, 5),
        nature=Nature.STANDALONE,
        seq="IF192486",
    )
    facts = _facts(MOLBIO_S, entry, "MOLBIO")
    assert "revenue_from_operations" in facts and "profit_after_tax" in facts


def test_a_fourth_quarter_document_yields_the_quarter_and_the_year_through_two_entries() -> None:
    quarter = _entry(
        isin=GSS,
        symbol="GSS",
        period_start=date(2025, 1, 1),
        period_end=date(2025, 3, 31),
        filing_date=date(2025, 5, 31),
        nature=Nature.STANDALONE,
        seq="IF97542",
    )
    annual = _entry(
        isin=GSS,
        symbol="GSS",
        period_start=date(2024, 4, 1),
        period_end=date(2025, 3, 31),
        filing_date=date(2025, 5, 31),
        nature=Nature.STANDALONE,
        seq="IF97542-FY",
        period="Annual",
    )
    q = _facts(GSS_Q4, quarter, "GSS")
    fy = _facts(GSS_Q4, annual, "GSS")
    assert q["revenue_from_operations"] == Decimal("28197000")  # the OneD column
    assert fy["revenue_from_operations"] == Decimal("88776000")  # the FourD column
    assert q["profit_after_tax"] == Decimal("7034000") and fy["profit_after_tax"] == Decimal(
        "10995000"
    )
    # The annual column states the reserves element (here 0.00, which the derivation treats as
    # unfilled: no equity is derived from it — M10.5's zero-reserves rule).
    assert fy["reserves_excl_revaluation"] == Decimal("0")
    assert "shareholders_equity_excl_revaluation" not in fy


def test_the_non_ind_as_variant_parses_with_the_same_vocabulary() -> None:
    entry = _entry(
        isin=AHLUCONT,
        symbol="AHLUCONT",
        period_start=date(2025, 1, 1),
        period_end=date(2025, 3, 31),
        filing_date=date(2025, 5, 31),
        nature=Nature.CONSOLIDATED,
        seq="IF97485",
    )
    facts = _facts(AHLUCONT_NONINDAS, entry, "AHLUCONT")
    assert facts["revenue_from_operations"] == Decimal("12158363000")
    assert facts["profit_after_tax"] == Decimal("833324000")


def test_a_document_stating_another_companys_isin_is_refused_inversion() -> None:
    """The DTIL misattribution, in the form the document itself can refute."""
    entry = _entry(
        isin=GSS,  # the index says GSS...
        symbol="MOLBIO",
        period_start=date(2026, 4, 1),
        period_end=date(2026, 6, 30),
        filing_date=date(2026, 9, 5),
        nature=Nature.CONSOLIDATED,
        seq="IF192487",
    )
    with pytest.raises(ParseError, match="states ISIN INE869T01028"):
        _facts(MOLBIO_C, entry, "MOLBIO")  # ...but the document says it is Molbio's


# ── the feed ────────────────────────────────────────────────────────────────────────────────────


def test_quarter_start_is_the_calendar_quarters_first_day() -> None:
    assert integrated.quarter_start(date(2025, 3, 31)) == date(2025, 1, 1)
    assert integrated.quarter_start(date(2025, 6, 30)) == date(2025, 4, 1)
    assert integrated.quarter_start(date(2025, 9, 30)) == date(2025, 7, 1)
    assert integrated.quarter_start(date(2025, 12, 31)) == date(2025, 10, 1)


def _page(records: list[dict[str, object]], total: int | None = None) -> bytes:
    return json.dumps(
        {"data": records, "size": 1000, "page": 0, "totalCount": total or len(records)}
    ).encode()


_RECORD: dict[str, object] = {
    "audited": "Audited",
    "broadcast_Date": "31-May-2025 23:20:35",
    "cmName": "GSS Infotech Limited",
    "consolidated": "Standalone",
    "creation_Date": "31-May-2025 23:20:36",
    "qe_Date": "31-MAR-2025",
    "seq_Id": "97542",
    "symbol": "GSS",
    "type": "Integrated Filing- Financials",
    "type_Sub": "Original",
    "xbrl": "https://nsearchives.nseindia.com/corporate/xbrl/INTEGRATED_FILING_INDAS_1461892_31052025112035_WEB.xml",
}


def test_a_fourth_quarter_record_becomes_a_quarterly_and_an_annual_entry() -> None:
    parsed = integrated.parse_integrated_index(
        _page([_RECORD]), filename="p.json", resolve_isin=lambda s, on: GSS if s == "GSS" else None
    )
    assert [e.period for e in parsed.entries] == ["Annual", "Quarterly"]
    annual, quarterly = parsed.entries
    assert quarterly.period_start == date(2025, 1, 1) and quarterly.period_end == date(2025, 3, 31)
    assert annual.period_start == date(2024, 4, 1) and annual.period_end == date(2025, 3, 31)
    assert quarterly.seq_number == "IF97542" and annual.seq_number == "IF97542-FY"
    assert quarterly.filing_date == date(2025, 5, 31)  # creation_Date, the dissemination date
    assert quarterly.isin == GSS and quarterly.nature is Nature.STANDALONE
    assert quarterly.audited is True
    assert parsed.total_count == 1 and parsed.unresolved == ()


def test_a_non_fourth_quarter_record_is_one_quarterly_entry() -> None:
    record = {**_RECORD, "qe_Date": "30-JUN-2025", "creation_Date": "14-Aug-2025 18:00:00"}
    parsed = integrated.parse_integrated_index(
        _page([record]), filename="p.json", resolve_isin=lambda s, on: GSS
    )
    (entry,) = parsed.entries
    assert entry.period == "Quarterly" and entry.period_start == date(2025, 4, 1)


def test_an_unresolved_symbol_is_named_not_dropped_silently() -> None:
    seen: list[tuple[str, date]] = []

    def resolver(symbol: str, on: date) -> str | None:
        seen.append((symbol, on))
        return None

    parsed = integrated.parse_integrated_index(
        _page([_RECORD]), filename="p.json", resolve_isin=resolver
    )
    assert parsed.entries == ()
    assert parsed.unresolved == (("GSS", date(2025, 5, 31)),)
    assert seen == [("GSS", date(2025, 5, 31))]  # resolved as of the dissemination date


def test_a_revision_with_no_broadcast_date_uses_creation_date() -> None:
    record = {**_RECORD, "broadcast_Date": None, "type_Sub": "Revision"}
    parsed = integrated.parse_integrated_index(
        _page([record]), filename="p.json", resolve_isin=lambda s, on: GSS
    )
    assert parsed.entries[0].filing_date == date(2025, 5, 31)


def test_the_archive_placeholder_means_no_document() -> None:
    record = {**_RECORD, "xbrl": "https://nsearchives.nseindia.com/corporate/xbrl/-"}
    parsed = integrated.parse_integrated_index(
        _page([record]), filename="p.json", resolve_isin=lambda s, on: GSS
    )
    assert all(e.xbrl_url is None and not e.is_actionable for e in parsed.entries)


def test_an_empty_page_is_zero_entries_not_an_error() -> None:
    parsed = integrated.parse_integrated_index(
        _page([], total=3476), filename="p.json", resolve_isin=lambda s, on: GSS
    )
    assert parsed.entries == () and parsed.records == 0 and parsed.total_count == 3476


def test_a_soft_404_and_a_foreign_record_type_are_refused() -> None:
    with pytest.raises(ParseError, match="markup"):
        integrated.parse_integrated_index(
            b"<html>", filename="p.json", resolve_isin=lambda s, on: GSS
        )
    with pytest.raises(ParseError, match="type is"):
        integrated.parse_integrated_index(
            _page([{**_RECORD, "type": "Integrated Filing- Governance"}]),
            filename="p.json",
            resolve_isin=lambda s, on: GSS,
        )


def test_the_planner_covers_every_month_with_the_stated_pages() -> None:
    pages = integrated.build_integrated_pages(
        date(2025, 3, 15),
        date(2025, 5, 10),
        register=load_register(),
        page_size=1000,
        pages_per_month=3,
    )
    assert len(pages) == 9
    assert pages[0].from_date == date(2025, 3, 15) and pages[0].to_date == date(2025, 3, 31)
    assert pages[-1].from_date == date(2025, 5, 1) and pages[-1].to_date == date(2025, 5, 10)
    assert [p.page for p in pages[:3]] == [1, 2, 3]
    assert "from_date=15-03-2025&to_date=31-03-2025&page=1&size=1000" in pages[0].url
    assert pages[0].filename == "integrated-filing-results_20250315_20250331_p01.json"
    assert pages[0].state_source == "nse_integrated_filing_index/2025-03-31/p01"
    assert len({p.state_source for p in pages}) == len(pages)
    with pytest.raises(ValueError):
        integrated.build_integrated_pages(
            date(2025, 5, 1), date(2025, 4, 1), register=load_register()
        )


PAGE_FIXTURE = "integrated-filing-results_20250501_20250531_p01_size20.json"


def test_a_captured_page_of_the_feed_parses_every_record() -> None:
    """The first 20 records of May 2025, fetched through the project's fetcher, frozen verbatim."""
    known = {"GSS": GSS, "AHLUCONT": AHLUCONT}
    minted: dict[str, str] = {}

    def resolve(symbol: str, on: date) -> str:
        # Real ISINs for the two names the fixtures cover; a well-formed synthetic one per other
        # symbol, minted in first-seen order so the run is deterministic.
        return known.get(symbol) or minted.setdefault(symbol, f"INE{len(minted):08d}1")

    parsed = integrated.parse_integrated_index(
        (FIXTURES / PAGE_FIXTURE).read_bytes(), filename=PAGE_FIXTURE, resolve_isin=resolve
    )
    assert parsed.records == 20 and parsed.total_count == 3476
    # Every record on the page is a fourth-quarter filing (qe 31-MAR-2025), so each yields two
    # entries: the quarter and the financial year.
    assert len(parsed.entries) == 40
    assert {e.period for e in parsed.entries} == {"Quarterly", "Annual"}
    assert all(e.period_end == date(2025, 3, 31) for e in parsed.entries)
    assert all(e.filing_date.month == 5 and e.filing_date.year == 2025 for e in parsed.entries)
    gss = [e for e in parsed.entries if e.symbol == "GSS"]
    assert {e.isin for e in gss} == {GSS}
    assert {e.seq_number for e in gss} == {"IF97542", "IF97542-FY"}
    ahlu = [e for e in parsed.entries if e.symbol == "AHLUCONT"]
    # Ahluwalia filed both an Ind-AS and a Non-Ind-AS document for each nature: four records.
    assert len(ahlu) == 8 and {e.nature for e in ahlu} == {Nature.STANDALONE, Nature.CONSOLIDATED}
    assert all(e.is_actionable for e in parsed.entries)
