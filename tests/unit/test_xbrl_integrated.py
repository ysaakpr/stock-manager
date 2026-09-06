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
IREDA_NBFC = "INTEGRATED_FILING_NBFC_INDAS_1415182_15042025065703_WEB.xml"
#: The four scheme spellings the first 32 pages of the live campaign refused 87 documents over,
#: one document each, and one document whose stated ISIN is the company's own mistyped.
GLOBALE_COLON_SCRIP = "INTEGRATED_FILING_INDAS_1455492_28052025083730_WEB.xml"
CEREBRAINT_SEBI_HTTPS_SCRIP = "INTEGRATED_FILING_INDAS_1455952_28052025111831_WEB.xml"
RAJMET_SEBI_HTTPS_SYMBOL = "INTEGRATED_FILING_INDAS_1458322_29052025103356_WEB.xml"
MTEDUCARE_COLON_SYMBOL = "INTEGRATED_FILING_INDAS_1461551_31052025015633_WEB.xml"
OBEROI_TYPO_ISIN = "INTEGRATED_FILING_INDAS_1427053_28042025115303_WEB.xml"

MOLBIO = "INE869T01028"
GSS = "INE871H01011"
AHLUCONT = "INE758C01029"
IREDA = "INE202E01016"


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


def test_the_nbfc_variant_parses_under_bses_in_capmkt_symbol_scheme() -> None:
    """Met on the first live sample: `INTEGRATED_FILING_NBFC_INDAS_*` identifies the entity under
    `http://www.bseindia.com/in-capmkt/Symbol` and states both `InterestEarned` and
    `RevenueFromOperations`; the Ind-AS map reads the latter."""
    entry = _entry(
        isin=IREDA,
        symbol="IREDA",
        period_start=date(2025, 1, 1),
        period_end=date(2025, 3, 31),
        filing_date=date(2025, 4, 15),
        nature=Nature.STANDALONE,
        seq="IF85010",
    )
    facts = _facts(IREDA_NBFC, entry, "IREDA")
    assert facts["revenue_from_operations"] == Decimal("19041700000")
    assert "profit_after_tax" in facts and "eps_basic" in facts


def test_the_same_companys_previous_isin_in_the_document_is_accepted() -> None:
    """A split gives a company a new ISIN under the same issuer code and filers keep the old one in
    their template for a while (Tata Investment, Angel One, E2E on the first live campaign)."""
    entry = _entry(
        isin="INE869T01036",  # a hypothetical post-split ISIN: same issuer 869T, new suffix
        symbol="MOLBIO",
        period_start=date(2026, 4, 1),
        period_end=date(2026, 6, 30),
        filing_date=date(2026, 9, 5),
        nature=Nature.CONSOLIDATED,
        seq="IF192487",
    )
    facts = _facts(MOLBIO_C, entry, "MOLBIO")  # the document states INE869T01028
    assert facts["revenue_from_operations"] == Decimal("4083760000")


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
    with pytest.raises(ParseError, match="states ISIN INE869T01028 — a different issuer"):
        _facts(MOLBIO_C, entry, "MOLBIO")  # ...but the document says it is Molbio's


@pytest.mark.parametrize(
    ("filename", "isin", "symbol", "nature", "filing_date", "revenue"),
    [
        # `http://www.bseindia.com/in-capmkt:/ScripCode` — a stray colon (41 documents)
        (
            GLOBALE_COLON_SCRIP,
            "INE0URU01010",
            "GLOBALE",
            Nature.STANDALONE,
            date(2025, 5, 28),
            "370485000",
        ),
        # BSE's ScripCode beside `https://www.sebi.gov.in/in-capmkt/ScripCode` (22 documents)
        (
            CEREBRAINT_SEBI_HTTPS_SCRIP,
            "INE345B01019",
            "CEREBRAINT",
            Nature.CONSOLIDATED,
            date(2025, 5, 28),
            "25226000",
        ),
        # BSE's Symbol beside `https://www.sebi.gov.in/in-capmkt/Symbol` (8 documents)
        (
            RAJMET_SEBI_HTTPS_SYMBOL,
            "INE00KV01022",
            "RAJMET",
            Nature.STANDALONE,
            date(2025, 5, 29),
            "2085300000",
        ),
        # `http://www.bseindia.com/in-capmkt:/Symbol` — the colon again (12 documents)
        (
            MTEDUCARE_COLON_SYMBOL,
            "INE472M01018",
            "MTEDUCARE",
            Nature.CONSOLIDATED,
            date(2025, 5, 31),
            "128965000",
        ),
    ],
)
def test_the_spelling_variants_of_an_accepted_scheme_are_folded_onto_it(
    filename: str, isin: str, symbol: str, nature: Nature, filing_date: date, revenue: str
) -> None:
    """87 of the first 412 live refusals were over the *spelling* of a scheme the parser accepts:
    `in-capmkt:/` with a stray colon, and SEBI's root under `https://`. Identity is unchanged, so
    the document parses; an unknown scheme is still refused (the NBFC test above covers the
    canonical form, `test_xbrl` the refusal)."""
    entry = _entry(
        isin=isin,
        symbol=symbol,
        period_start=date(2025, 1, 1),
        period_end=date(2025, 3, 31),
        filing_date=filing_date,
        nature=nature,
        seq=filename.split("_")[3],
    )
    facts = _facts(filename, entry, symbol)
    assert facts["revenue_from_operations"] == Decimal(revenue)


def test_a_stated_isin_that_is_the_entrys_mistyped_is_accepted() -> None:
    """Oberoi Realty's Q4 FY25 document states `INE903I01010` for `INE093I01010` — two adjacent
    characters swapped. A different issuer code, so the plain issuer comparison refused it (and
    12 other companies' typos in the first 32 live pages); the check-digit-and-transposition rule
    recognises a typo of the entry's own ISIN and accepts the filing under the entry's."""
    entry = _entry(
        isin="INE093I01010",
        symbol="OBEROIRLTY",
        period_start=date(2025, 1, 1),
        period_end=date(2025, 3, 31),
        filing_date=date(2025, 4, 28),
        nature=Nature.CONSOLIDATED,
        seq="IF87657",
    )
    facts = _facts(OBEROI_TYPO_ISIN, entry, "OBEROIRLTY")
    assert facts["revenue_from_operations"] == Decimal("11501400000")
    assert facts["profit_after_tax"] == Decimal("4331700000")
    assert facts["eps_basic"] == Decimal("11.91")


#: Every company the first 32 pages of the live campaign refused over its stated ISIN, with the
#: verdict the rule must give. Thirteen are typos (a wrong character, `O` for `0`, `1` for `I`, an
#: adjacent swap); two state a *sister company's real ISIN* — Ashapura Minechem stating Orient
#: Ceratech's, Gillette stating P&G Hygiene's — and must stay refused.
LIVE_STATED_ISINS = [
    ("INE093I01010", "INE903I01010", True),  # OBEROIRLTY: adjacent swap, check digit still valid
    ("INE024D01016", "INE024E01016", True),  # PRUDMOULI
    ("INE576I01022", "INE576101022", True),  # JKIL: 1 for I
    ("INE348A01023", "INE569C01020", False),  # ASHAPURMIN → Orient Ceratech's ISIN
    ("INE0FS801015", "INEOFS801015", True),  # MSUMI: O for 0
    ("INE834I01025", "INE834101025", True),  # KHADIM
    ("INE497S01012", "INE479S01012", True),  # GODAVARIB: adjacent swap
    ("INE139I01011", "INE139101011", True),  # BVCL
    ("INE0D6701023", "INE0T6701023", True),  # IPL
    ("INE03JI01017", "INEO3JI01017", True),  # DGCONTENT
    ("INE0FHS01024", "INEOFHS01024", True),  # DEEPINDS
    ("INE0N7W01012", "INEON7W01012", True),  # BLAL
    ("INE661I01014", "INR661I01014", True),  # BGRENERGY: a wrong country prefix
    ("INE398A01010", "INE388A01010", True),  # VENKEYS
    ("INE322A01010", "INE179A01014", False),  # GILLETTE → P&G Hygiene's ISIN
]


@pytest.mark.parametrize(("expected", "stated", "typo"), LIVE_STATED_ISINS)
def test_the_typo_rule_gives_the_right_verdict_on_every_live_refusal(
    expected: str, stated: str, typo: bool
) -> None:
    assert parser.is_isin_typo_of(stated, expected) is typo


def test_the_check_digit_accepts_real_isins_and_refuses_a_single_wrong_character() -> None:
    """ISO 6166: letters to base-36 values, then Luhn over the digits. Every real ISIN passes; any
    single wrong character fails (the swap of two adjacent digits is the one error it can miss,
    which is why `is_isin_typo_of` tests for that separately)."""
    for real in ("INE002A01018", "INE009A01021", "INE467B01029", "INE093I01010", "US0378331005"):
        assert parser.is_isin_check_digit_valid(real), real
    assert not parser.is_isin_check_digit_valid("INE002A01017")
    assert not parser.is_isin_check_digit_valid("INEOFS801015")  # MSUMI's, O for 0
    assert not parser.is_isin_check_digit_valid("INE002A0101")  # not twelve characters
    assert not parser.is_isin_check_digit_valid("ine002a01018")  # not upper-case: not an ISIN
    # The same ISIN is never a typo of itself, and a real other ISIN is never a typo.
    assert not parser.is_isin_typo_of("INE002A01018", "INE002A01018")
    assert not parser.is_isin_typo_of("INE009A01021", "INE002A01018")


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


# ── half-yearly reporters: the index says a quarter, the document reports six months ─────────────

#: Globus Spirits, filed 2025-12-17. The index entry (IF130670) asks for 2025-07-01→2025-09-30 and
#: the document reports one column, 2025-04-01→2025-09-30, whose own `ReportingQuarter` reads
#: "Half yearly". Captured from the live campaign's L0, not built here: this shape is 695 of the
#: 2,084 filings the exact-period rule was refusing, and a synthetic stand-in would have agreed
#: with whatever the parser already did.
_HALF_YEARLY = "INTEGRATED_FILING_INDAS_1586681_17122025122452_WEB.xml"
GLOBUS = "INE615I01010"


def _globus_entry(*, period_start: date, period_end: date) -> FilingIndexEntry:
    return _entry(
        isin=GLOBUS,
        symbol="GLOBUSSPR",
        period_start=period_start,
        period_end=period_end,
        filing_date=date(2025, 12, 17),
        nature=Nature.CONSOLIDATED,
        seq="IF130670",
    )


def test_a_half_yearly_filing_the_index_calls_quarterly_is_stored_as_a_half_year() -> None:
    """The recovery — and its point: the period stored is the document's, not the index's."""
    filing = parser.parse(
        (FIXTURES / _HALF_YEARLY).read_bytes(),
        entry=_globus_entry(period_start=date(2025, 7, 1), period_end=date(2025, 9, 30)),
        known_symbols=frozenset({"GLOBUSSPR"}),
        filename=_HALF_YEARLY,
        l0_key=f"fixture/{_HALF_YEARLY}",
    )
    # Not 2025-07-01. The index asked for a quarter; six months is what the company reported and
    # six months is what a consumer must see, or it would read this as a quarter's revenue.
    assert (filing.period_start, filing.period_end) == (date(2025, 4, 1), date(2025, 9, 30))
    assert filing.nature is Nature.CONSOLIDATED
    facts = {f.concept: f.value for f in filing.facts if f.segment is None}
    assert facts["revenue_from_operations"] == Decimal("18233360000.00")


def test_the_annual_entry_for_the_same_document_is_still_refused() -> None:
    """The condition that stops the fallback duplicating what an exact match already captures.

    23.7% of documents are named by both an Annual and a Quarterly index entry. Six months is not
    longer than the twelve this entry asks for, so this one gets nothing and only the quarterly
    entry recovers the document.
    """
    with pytest.raises(ParseError, match="no results column covers"):
        parser.parse(
            (FIXTURES / _HALF_YEARLY).read_bytes(),
            entry=_globus_entry(period_start=date(2025, 4, 1), period_end=date(2026, 3, 31)),
            known_symbols=frozenset({"GLOBUSSPR"}),
            filename=_HALF_YEARLY,
            l0_key=f"fixture/{_HALF_YEARLY}",
        )


def test_a_column_ending_elsewhere_is_never_recovered() -> None:
    """The end date must be the one the index named; a near miss is still a miss."""
    with pytest.raises(ParseError, match="no results column covers"):
        parser.parse(
            (FIXTURES / _HALF_YEARLY).read_bytes(),
            entry=_globus_entry(period_start=date(2025, 7, 1), period_end=date(2025, 12, 31)),
            known_symbols=frozenset({"GLOBUSSPR"}),
            filename=_HALF_YEARLY,
            l0_key=f"fixture/{_HALF_YEARLY}",
        )
