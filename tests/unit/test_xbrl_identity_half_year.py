"""M14.3 (XBRL): the class E and F1 identity rules and the half-year guards, on real filings.

Every document here is copied byte-for-byte from the production L0 lake (`tests/fixtures/xbrl/`,
B8); none is authored. Each rule is pinned from both sides, so a test fails if it is inverted:

* **E — the company's own BSE code beside its symbol.** A multi-entity standalone filing (Sep-25 /
  Dec-25) whose results contexts name the symbol and two `StatementStatic*` contexts name a BSE
  scrip code is accepted only when that code is the one D2 holds for the entry's ISIN — or the
  placeholder `000000`; any other code, or none on file, stays refused.
* **F1 — a mistyped symbol under the entry's own ISIN.** Accepted when the document's ISIN equals
  the entry's exactly (and its BSE code, if stated, is D2's); a different ISIN, or a BSE code D2
  does not hold, stays refused. Class G (a sister company's ISIN) is untouched and still refused.
* **The labelled half-year (class M).** A 2018-2023 `…_WEB.xml` column whose six-month period is
  read off a `ReportingQuarter = Half yearly` header label is kept only when the document's own
  cumulative column states the same figures; the quarter of a half-year results document is not.
* **The cross-filing half-year guard.** A six-month filing that states exactly one of its quarters'
  revenue as already published, or less than its first quarter's, is refused by the runner.
"""

from __future__ import annotations

import re
import socket
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, cast

import pytest

from dataplatform.clock import IST, FrozenClock
from dataplatform.identity.master import Exchange, IdentityMaster, Listing, ListingStatus
from dataplatform.ingest import fundamentals_backfill as fb
from dataplatform.ingest.models import ParseError
from dataplatform.ingest.xbrl import half_year, parser
from dataplatform.ingest.xbrl.discovery import FilingIndexEntry
from dataplatform.ingest.xbrl.models import Filing, Nature
from dataplatform.store.l0 import L0Store
from dataplatform.store.pit_fundamentals import write_pit

INTEGRATED: Final = Path(__file__).resolve().parents[1] / "fixtures" / "xbrl" / "integrated"
FILINGS: Final = Path(__file__).resolve().parents[1] / "fixtures" / "xbrl" / "filings"

# Class E: Vikram Solar's Q3 FY26 standalone filing — 9 contexts say VIKRAMSOLR, two say 544488.
VIKRAMSOLR_DOC: Final = "INTEGRATED_FILING_INDAS_1606052_20012026061129_WEB.xml"
VIKRAMSOLR: Final = "INE078V01014"
VIKRAMSOLR_BSE: Final = "544488"
# Class E with the placeholder: GVP Infotech's Q2 FY26 — 16 contexts say GVPTECH, two say 000000.
GVPTECH_DOC: Final = "INTEGRATED_FILING_INDAS_1568467_07112025071648_WEB.xml"
GVPTECH: Final = "INE382T01030"
# Class E refused: Panache Digilife's Q3 FY26 — two contexts say 123456, no BSE code of its.
PANACHE_DOC: Final = "INTEGRATED_FILING_INDAS_1624687_12022026091347_WEB.xml"
PANACHE: Final = "INE895W01019"
# Class F1: Coforge's Q3 FY26 standalone — Symbol NUCLEUS, but ISIN INE591G01025 and BSE 532541.
COFORGE_DOC: Final = "INTEGRATED_FILING_INDAS_1608771_22012026114516_WEB.xml"
COFORGE: Final = "INE591G01025"
COFORGE_BSE: Final = "532541"
# Class G (owner decision, stays refused): Ashapura's Q1 FY26 — symbol and BSE code its own, ISIN
# Orient Ceratech's.
ASHAPURMIN_DOC: Final = "INTEGRATED_FILING_INDAS_1510335_12082025075003_WEB.xml"
ASHAPURMIN: Final = "INE348A01023"
# Class M: Muthoot Capital's Sep-2018 document — `ReportingQuarter = Half yearly` pinned to OneD,
# which holds the July-September quarter (131.8 cr); FourD holds the six months (256.0 cr).
MUTHOOTCAP_DOC: Final = "NONINDAS_39357_52711_18102018055921_WEB.xml"
MUTHOOTCAP: Final = "INE296G01013"
# The cross-filing guard: Arihant Superstructures filed, on 2025-11-12, a one-column "half-year"
# document (IF126904, OneD 2025-04-01→09-30) whose revenue is the 131,692,000 its quarterly
# document of the same day (IF126892) states for July-September — that document's six months are
# 206,292,000. Globus Spirits' half-year (IF130670) is genuine: 18,233,360,000 against a first
# quarter of 9,515,519,000 (IF108344).
ARIHANT_H1_DOC: Final = "INTEGRATED_FILING_INDAS_1572905_12112025082817_WEB.xml"
ARIHANT_Q2_DOC: Final = "INTEGRATED_FILING_INDAS_1572886_12112025082158_WEB.xml"
ARIHANT: Final = "INE643K01018"
GLOBUS_H1_DOC: Final = "INTEGRATED_FILING_INDAS_1586681_17122025122452_WEB.xml"
GLOBUS_Q1_DOC: Final = "INTEGRATED_FILING_INDAS_1502003_04082025062112_WEB.xml"
GLOBUS: Final = "INE615I01010"


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a test opened a socket; these tests are offline")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def _entry(
    *,
    isin: str,
    symbol: str,
    start: date,
    end: date,
    filed: date,
    nature: Nature = Nature.STANDALONE,
    seq: str,
) -> FilingIndexEntry:
    return FilingIndexEntry(
        isin=isin,
        symbol=symbol,
        name=symbol,
        period_start=start,
        period_end=end,
        filing_date=filed,
        nature=nature,
        audited=False,
        period="Quarterly",
        xbrl_url=f"https://nsearchives.nseindia.com/corporate/xbrl/{seq}.xml",
        seq_number=seq,
    )


def _parse(
    folder: Path,
    filename: str,
    entry: FilingIndexEntry,
    *,
    bse_codes: frozenset[str] | None = None,
) -> Filing:
    return parser.parse(
        (folder / filename).read_bytes(),
        entry=entry,
        known_symbols=frozenset({entry.symbol}),
        bse_codes=bse_codes,
        filename=filename,
        l0_key=f"fixture/{filename}",
    )


def _revenue(filing: Filing) -> Decimal:
    return next(f.value for f in filing.company_facts() if f.concept == "revenue_from_operations")


# ── class E: the company's own BSE code beside its symbol ─────────────────────────────────────

_Q3FY26: Final = (date(2025, 10, 1), date(2025, 12, 31))


def _vikram(**kwargs: Any) -> Filing:
    entry = _entry(
        isin=VIKRAMSOLR,
        symbol="VIKRAMSOLR",
        filed=date(2026, 1, 20),
        seq="IF136147",
        start=_Q3FY26[0],
        end=_Q3FY26[1],
    )
    return _parse(INTEGRATED, VIKRAMSOLR_DOC, entry, **kwargs)


def test_the_document_really_names_two_identifiers() -> None:
    """The precondition, read off the bytes: without it the next tests prove nothing."""
    body = (INTEGRATED / VIKRAMSOLR_DOC).read_text(encoding="utf-8")
    identifiers = re.findall(r"<xbrli:identifier[^>]*>([^<]*)<", body)
    assert sorted(set(identifiers)) == [VIKRAMSOLR_BSE, "VIKRAMSOLR"]
    assert identifiers.count(VIKRAMSOLR_BSE) == 2


def test_a_bse_code_d2_holds_for_the_isin_is_the_same_company() -> None:
    filing = _vikram(bse_codes=frozenset({VIKRAMSOLR_BSE}))
    # The exact column the entry asks for — a stated number, nothing computed.
    assert (filing.period_start, filing.period_end) == (date(2025, 10, 1), date(2025, 12, 31))
    assert filing.isin == VIKRAMSOLR and filing.symbol == "VIKRAMSOLR"
    assert _revenue(filing) == Decimal("11057700000")


@pytest.mark.parametrize(
    "codes",
    [None, frozenset(), frozenset({"532541"})],
    ids=["no-master-codes", "isin-has-no-bse-code", "another-companys-code"],
)
def test_a_bse_code_d2_does_not_hold_for_the_isin_is_still_two_entities(
    codes: frozenset[str] | None,
) -> None:
    with pytest.raises(ParseError, match="filing names more than one entity"):
        _vikram(bse_codes=codes)


def test_the_000000_placeholder_names_nobody_and_is_set_aside() -> None:
    entry = _entry(
        isin=GVPTECH,
        symbol="GVPTECH",
        start=date(2025, 7, 1),
        end=date(2025, 9, 30),
        filed=date(2025, 11, 7),
        seq="IF125223",
    )
    body = (INTEGRATED / GVPTECH_DOC).read_text(encoding="utf-8")
    assert re.findall(r"<xbrli:identifier[^>]*>(000000)<", body) == ["000000", "000000"]
    filing = _parse(INTEGRATED, GVPTECH_DOC, entry, bse_codes=frozenset())
    assert (filing.period_start, filing.period_end) == (date(2025, 7, 1), date(2025, 9, 30))
    assert _revenue(filing) == Decimal("18195000")


def test_a_code_that_is_not_the_isins_stays_refused_and_says_why() -> None:
    """Panache's 123456 is the one E filing of 182 the rule must go on refusing."""
    entry = _entry(
        isin=PANACHE,
        symbol="PANACHE",
        filed=date(2026, 2, 12),
        seq="IF142724",
        start=_Q3FY26[0],
        end=_Q3FY26[1],
    )
    with pytest.raises(ParseError, match=r"123456 is not the BSE code D2 holds for INE895W01019"):
        _parse(INTEGRATED, PANACHE_DOC, entry, bse_codes=frozenset())


def test_two_symbols_are_still_two_entities_whatever_the_codes() -> None:
    """Only six-digit codes are set aside: a second *symbol* is a second company."""
    body = (
        (INTEGRATED / VIKRAMSOLR_DOC)
        .read_bytes()
        .replace(f">{VIKRAMSOLR_BSE}<".encode(), b">VIKRAMSOL2<")
    )
    entry = _entry(
        isin=VIKRAMSOLR,
        symbol="VIKRAMSOLR",
        filed=date(2026, 1, 20),
        seq="IF136147",
        start=_Q3FY26[0],
        end=_Q3FY26[1],
    )
    with pytest.raises(ParseError, match="filing names more than one entity"):
        parser.parse(
            body,
            entry=entry,
            known_symbols=frozenset({"VIKRAMSOLR"}),
            bse_codes=frozenset({VIKRAMSOLR_BSE}),
            filename=VIKRAMSOLR_DOC,
        )


# ── class F1: a mistyped symbol in a document whose ISIN is the entry's ──────────────────────


def _coforge_entry(isin: str = COFORGE) -> FilingIndexEntry:
    return _entry(
        isin=isin,
        symbol="COFORGE",
        filed=date(2026, 1, 22),
        seq="IF136896",
        start=_Q3FY26[0],
        end=_Q3FY26[1],
    )


def test_a_symbol_typo_under_the_entrys_own_isin_is_accepted() -> None:
    body = (INTEGRATED / COFORGE_DOC).read_text(encoding="utf-8")
    assert '<in-capmkt:Symbol contextRef="OneD">NUCLEUS<' in body  # another listed company's name
    assert f'<in-capmkt:ISIN contextRef="OneD">{COFORGE}<' in body
    filing = _parse(INTEGRATED, COFORGE_DOC, _coforge_entry(), bse_codes=frozenset({COFORGE_BSE}))
    # The symbol kept is the index's, which D2 accepts; NUCLEUS is the filer's typing.
    assert filing.isin == COFORGE and filing.symbol == "COFORGE"
    assert _revenue(filing) == Decimal("20736000000")


def test_a_different_isin_does_not_vouch_for_a_mismatched_symbol() -> None:
    """The inversion: the same document under an entry naming another ISIN stays refused."""
    with pytest.raises(ParseError, match="but the document says NUCLEUS"):
        _parse(
            INTEGRATED,
            COFORGE_DOC,
            _coforge_entry(isin="INE096B01018"),
            bse_codes=frozenset({COFORGE_BSE}),
        )


@pytest.mark.parametrize(
    "codes", [frozenset({"500500"}), frozenset()], ids=["another-code", "no-code-on-file"]
)
def test_a_bse_code_d2_does_not_hold_withdraws_the_isins_vouching(codes: frozenset[str]) -> None:
    with pytest.raises(ParseError, match="but the document says NUCLEUS"):
        _parse(INTEGRATED, COFORGE_DOC, _coforge_entry(), bse_codes=codes)


def test_a_sister_companys_isin_is_still_refused() -> None:
    """Class G is an owner decision: symbol and BSE code agree, the ISIN names Orient Ceratech."""
    entry = _entry(
        isin=ASHAPURMIN,
        symbol="ASHAPURMIN",
        start=date(2025, 4, 1),
        end=date(2025, 6, 30),
        filed=date(2025, 8, 12),
        seq="IF110561",
    )
    with pytest.raises(ParseError, match="states ISIN INE569C01020 — a different issuer"):
        _parse(INTEGRATED, ASHAPURMIN_DOC, entry, bse_codes=frozenset({"527001"}))


# ── class M: a half-year read off a header label must be the document's six months ──────────────


def _muthoot_entry() -> FilingIndexEntry:
    return _entry(
        isin=MUTHOOTCAP,
        symbol="MUTHOOTCAP",
        start=date(2018, 7, 1),
        end=date(2018, 9, 30),
        filed=date(2018, 10, 22),
        seq="1049750",
    )


def test_the_quarter_of_a_half_year_results_document_is_not_stored_as_the_half() -> None:
    body = (FILINGS / MUTHOOTCAP_DOC).read_text(encoding="utf-8")
    assert "DateOfStartOfReportingPeriod" not in body  # the column states no period of its own
    assert re.search(r'ReportingQuarter contextRef="OneD">Half yearly<', body)
    assert re.search(r'RevenueFromOperations contextRef="OneD"[^>]*>1318200000.00<', body)
    assert re.search(r'RevenueFromOperations contextRef="FourD"[^>]*>2559500000.00<', body)
    with pytest.raises(ParseError, match="no results column covers 2018-07-01→2018-09-30"):
        _parse(FILINGS, MUTHOOTCAP_DOC, _muthoot_entry())


def test_a_labelled_half_year_the_cumulative_column_agrees_with_is_kept() -> None:
    """The other side. No captured document has this shape — every one of the 117 old-era columns
    labelled `Half yearly` is a quarter — so the genuine half-yearly reporter is the same real
    document with its `One` headline set to its `Four` figures, which is what such a filer's
    document states (its only column of results is the six months to date)."""
    body = (FILINGS / MUTHOOTCAP_DOC).read_text(encoding="utf-8")
    replaced = 0
    for element in ("RevenueFromOperations", "Revenue", "Income", "ProfitLossForThePeriod"):
        four = re.search(rf':{element} contextRef="FourD"[^>]*>([^<]+)<', body)
        if four is None:
            continue
        body, n = re.subn(
            rf'(:{element} contextRef="OneD"[^>]*>)[^<]+(<)', rf"\g<1>{four.group(1)}\g<2>", body
        )
        replaced += n
    assert replaced >= 2  # revenue and profit at least
    filing = parser.parse(
        body.encode(),
        entry=_muthoot_entry(),
        known_symbols=frozenset({"MUTHOOTCAP"}),
        filename=MUTHOOTCAP_DOC,
    )
    assert (filing.period_start, filing.period_end) == (date(2018, 4, 1), date(2018, 9, 30))
    assert _revenue(filing) == Decimal("2559500000.00")


# ── the cross-filing guard: a half-year that states one quarter's revenue ──────────────────────

_H1_FY26: Final = (date(2025, 7, 1), date(2025, 9, 30))


def _arihant_h1() -> Filing:
    entry = _entry(
        isin=ARIHANT,
        symbol="ARIHANTSUP",
        filed=date(2025, 11, 12),
        seq="IF126904",
        start=_H1_FY26[0],
        end=_H1_FY26[1],
    )
    return _parse(INTEGRATED, ARIHANT_H1_DOC, entry)


def _arihant_q2() -> Filing:
    entry = _entry(
        isin=ARIHANT,
        symbol="ARIHANTSUP",
        filed=date(2025, 11, 12),
        seq="IF126892",
        start=_H1_FY26[0],
        end=_H1_FY26[1],
    )
    return _parse(INTEGRATED, ARIHANT_Q2_DOC, entry)


def _globus_h1() -> Filing:
    entry = _entry(
        isin=GLOBUS,
        symbol="GLOBUSSPR",
        filed=date(2025, 12, 17),
        nature=Nature.CONSOLIDATED,
        seq="IF130670",
        start=_H1_FY26[0],
        end=_H1_FY26[1],
    )
    return _parse(INTEGRATED, GLOBUS_H1_DOC, entry)


def _globus_q1(*, filed: date = date(2025, 8, 4)) -> Filing:
    entry = _entry(
        isin=GLOBUS,
        symbol="GLOBUSSPR",
        start=date(2025, 4, 1),
        end=date(2025, 6, 30),
        filed=filed,
        nature=Nature.CONSOLIDATED,
        seq="IF108344",
    )
    return _parse(INTEGRATED, GLOBUS_Q1_DOC, entry)


def test_a_half_year_stating_its_second_quarters_revenue_is_one_quarter() -> None:
    h1, q2 = _arihant_h1(), _arihant_q2()
    assert (h1.period_start, h1.period_end) == (date(2025, 4, 1), date(2025, 9, 30))
    assert (q2.period_start, q2.period_end) == (date(2025, 7, 1), date(2025, 9, 30))
    reason = half_year.single_quarter_reason(h1, q2.facts)
    assert reason is not None and "exactly the 2025-07-01→2025-09-30 quarter's" in reason


def test_a_genuine_half_year_is_kept() -> None:
    h1, q1 = _globus_h1(), _globus_q1()
    assert _revenue(h1) == Decimal("18233360000.00") and _revenue(q1) == Decimal("9515519000")
    assert half_year.single_quarter_reason(h1, q1.facts) is None


def test_a_half_year_below_its_first_quarter_is_one_quarter() -> None:
    """Revenue is not negative, so six months cannot be less than the first three."""
    h1 = _globus_h1()
    bigger_q1 = [
        f.model_copy(update={"value": _revenue(h1) + 1})
        for f in _globus_q1().facts
        if f.concept == "revenue_from_operations" and f.segment is None
    ]
    reason = half_year.single_quarter_reason(h1, bigger_q1)
    assert reason is not None and "less than its own first quarter's" in reason


def test_a_quarter_filed_after_the_half_year_is_not_evidence() -> None:
    """Invariant #7: what the guard knows is what was published by the half-year's filing date."""
    h1 = _arihant_h1()
    later = [f.model_copy(update={"filing_date": date(2025, 11, 13)}) for f in _arihant_q2().facts]
    assert half_year.single_quarter_reason(h1, later) is None


def test_other_natures_and_isins_are_not_evidence() -> None:
    h1 = _arihant_h1()
    q2 = _arihant_q2().facts
    other_nature = [f.model_copy(update={"nature": Nature.CONSOLIDATED}) for f in q2]
    other_isin = [f.model_copy(update={"isin": GLOBUS}) for f in q2]
    assert half_year.single_quarter_reason(h1, other_nature) is None
    assert half_year.single_quarter_reason(h1, other_isin) is None


def test_a_quarter_is_never_judged_as_a_half_year() -> None:
    q2 = _arihant_q2()
    assert not half_year.is_half_year(q2)
    assert half_year.single_quarter_reason(q2, q2.facts) is None


# ── the runner: the guard reads what L1 already holds ─────────────────────────────────────────


def _runner(tmp_path: Path) -> tuple[Any, L0Store]:
    clock = FrozenClock(datetime(2026, 11, 3, 2, 30, tzinfo=IST))
    l0 = L0Store(clock=clock, data_root=tmp_path)
    runner = fb.FundamentalsBackfillRunner(
        fetcher=cast(Any, None),
        l0=l0,
        sync=cast(Any, None),
        universe={ARIHANT},
        commit=lambda: None,
        data_root=tmp_path,
        clock=clock,
    )
    return runner, l0


def _drive_parse(tmp_path: Path) -> Filing:
    runner, l0 = _runner(tmp_path)
    entry = _entry(
        isin=ARIHANT,
        symbol="ARIHANTSUP",
        filed=date(2025, 11, 12),
        seq="IF126904",
        start=_H1_FY26[0],
        end=_H1_FY26[1],
    )
    ref = l0.put(
        parser.SOURCE_ID,
        entry.filing_date,
        ARIHANT_H1_DOC,
        (INTEGRATED / ARIHANT_H1_DOC).read_bytes(),
    )
    unit = fb.FilingUnit(entry=entry, url=str(entry.xbrl_url), filename=ARIHANT_H1_DOC)
    filing, relabel = runner._parse(unit, ref)
    assert relabel is None
    return cast(Filing, filing)


def test_the_runner_refuses_the_half_year_once_its_quarter_is_published(tmp_path: Path) -> None:
    write_pit(_arihant_q2(), data_root=tmp_path)
    with pytest.raises(ParseError, match="one quarter's figures under a half-year label"):
        _drive_parse(tmp_path)


def test_the_runner_keeps_the_half_year_when_nothing_contradicts_it(tmp_path: Path) -> None:
    filing = _drive_parse(tmp_path)
    assert (filing.period_start, filing.period_end) == (date(2025, 4, 1), date(2025, 9, 30))


# ── D2's BSE code, as the runner hands it to the parser ───────────────────────────────────────


def test_bse_codes_of_reads_the_bse_listing_only() -> None:
    master = IdentityMaster(
        windows=[],
        listings=[
            Listing(
                isin=COFORGE,
                exchange=Exchange.BSE,
                status=ListingStatus.ACTIVE,
                security_code=COFORGE_BSE,
            ),
            Listing(
                isin=COFORGE,
                exchange=Exchange.NSE,
                status=ListingStatus.ACTIVE,
                security_code="11543",
            ),
            Listing(isin=PANACHE, exchange=Exchange.NSE, status=ListingStatus.ACTIVE),
        ],
    )
    assert fb.bse_codes_of(master, COFORGE) == frozenset({COFORGE_BSE})
    assert fb.bse_codes_of(master, PANACHE) == frozenset()
    assert fb.bse_codes_of(master, "INE000000000") == frozenset()
