"""M14.3 (XBRL): the owner's rulings of 2026-11-02 — the class-A relabel and INDBANK's ISIN typo.

Every document here is a byte-for-byte copy of a production L0 payload (`tests/fixtures/xbrl/`,
B8). The prior quarters the relabel check reads are the company's real published values, as L1
holds them (filing ids and dates cited), handed in the way the runner hands them in. Each rule is
pinned from both sides, so a test fails if its logic is inverted:

* **Class A — `Q4_RELABEL_FY_MINUS_9M_CHECK`.** G M Breweries' Q4 FY25 standalone document states
  its quarter column as Oct→Mar under `Half Yearly`; its revenue and profit both equal the year
  minus the three quarters filed before it, to the rupee — stored as Jan→Mar, with the document's
  own values. Astec's consolidated Q4 column is a genuine second half-year (year minus two
  quarters) — refused, saying which check failed. Moving one prior quarter's filing date past the
  Q4 filing's turns the passing case into a look-ahead refusal (invariant #7).
* **Class G — INDBANK's one-character ISIN typo.** Accepted only when the stated ISIN is one
  character from the entry's, the symbol and BSE code are the entry ISIN's, and no other D2 ISIN
  is within one character; a sister company's real ISIN, and Ashapura's Orient Ceratech ISIN,
  stay refused.
"""

from __future__ import annotations

import socket
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, cast

import pytest

from dataplatform.clock import IST, FrozenClock
from dataplatform.ingest import fundamentals_backfill as fb
from dataplatform.ingest.models import ParseError
from dataplatform.ingest.xbrl import parser, q4_relabel
from dataplatform.ingest.xbrl.discovery import FilingIndexEntry
from dataplatform.ingest.xbrl.models import Filing, FundamentalFact, Nature, Taxonomy
from dataplatform.store.l0 import L0Store
from dataplatform.store.pit_fundamentals import write_pit

INTEGRATED: Final = Path(__file__).resolve().parents[1] / "fixtures" / "xbrl" / "integrated"

# Class A, passing: G M Breweries' Q4 FY25 standalone (IF84947, filed 2025-04-15). OneD states
# 2024-10-01..2025-03-31 under `Half Yearly`; FourD is the year.
GMBREW_DOC: Final = "INTEGRATED_FILING_INDAS_1414486_15042025011830_WEB.xml"
GMBREW: Final = "INE075D01018"
# Class A, a genuine H2: Astec LifeSciences' Q4 FY25 consolidated (IF86388, filed 2025-04-23).
ASTEC_DOC: Final = "INTEGRATED_FILING_INDAS_1423539_23042025110233_WEB.xml"
ASTEC: Final = "INE563J01010"
# Class G, the typo: IndBank Merchant Banking's Q1 FY27 (IF173185) states INE841801017 (B→8).
INDBANK_DOC: Final = "INTEGRATED_FILING_NBFC_INDAS_1689424_08072026062603_WEB.xml"
INDBANK: Final = "INE841B01017"
INDBANK_STATED: Final = "INE841801017"
INDBANK_BSE: Final = "511473"
# Class G, refused: Ashapura's Q1 FY26 — symbol and BSE code its own, ISIN Orient Ceratech's.
ASHAPURMIN_DOC: Final = "INTEGRATED_FILING_INDAS_1510335_12082025075003_WEB.xml"
ASHAPURMIN: Final = "INE348A01023"
ORIENT_CERATECH: Final = "INE569C01020"

_Q4FY25: Final = (date(2025, 1, 1), date(2025, 3, 31))
_FY25_QUARTERS: Final = (
    (date(2024, 4, 1), date(2024, 6, 30)),
    (date(2024, 7, 1), date(2024, 9, 30)),
    (date(2024, 10, 1), date(2024, 12, 31)),
)


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
    seq: str,
    nature: Nature = Nature.STANDALONE,
) -> FilingIndexEntry:
    return FilingIndexEntry(
        isin=isin,
        symbol=symbol,
        name=symbol,
        period_start=start,
        period_end=end,
        filing_date=filed,
        nature=nature,
        audited=True,
        period="Quarterly",
        xbrl_url=f"https://nsearchives.nseindia.com/corporate/xbrl/{seq}.xml",
        seq_number=seq,
    )


def _gmbrew_entry() -> FilingIndexEntry:
    return _entry(
        isin=GMBREW,
        symbol="GMBREW",
        start=_Q4FY25[0],
        end=_Q4FY25[1],
        filed=date(2025, 4, 15),
        seq="IF84947",
    )


def _astec_entry() -> FilingIndexEntry:
    return _entry(
        isin=ASTEC,
        symbol="ASTEC",
        start=_Q4FY25[0],
        end=_Q4FY25[1],
        filed=date(2025, 4, 23),
        seq="IF86388",
        nature=Nature.CONSOLIDATED,
    )


def _candidate(filename: str, entry: FilingIndexEntry) -> q4_relabel.RelabelCandidate:
    with pytest.raises(q4_relabel.LabelledHalfYearError) as raised:
        parser.parse(
            (INTEGRATED / filename).read_bytes(),
            entry=entry,
            known_symbols=frozenset({entry.symbol}),
            filename=filename,
            l0_key=f"fixture/{filename}",
        )
    return raised.value.candidate


#: Each FY25 quarter as L1 holds it: (filing id, filing date, {concept: value}) — the real values.
_Quarter = tuple[str, date, dict[str, str]]

GMBREW_PRIORS: Final[tuple[_Quarter, ...]] = (
    (
        "1177449",
        date(2024, 7, 9),
        {
            "revenue_from_operations": "6005900000",
            "profit_after_tax": "249400000",
            "other_income": "36300000",
            "total_income": "6042200000",
            "total_expenses": "5709000000",
            "profit_before_tax": "333200000",
        },
    ),
    (
        "1181388",
        date(2024, 10, 11),
        {
            "revenue_from_operations": "5957800000",
            "profit_after_tax": "216700000",
            "other_income": "30300000",
            "total_income": "5988100000",
            "total_expenses": "5698400000",
            "profit_before_tax": "289700000",
        },
    ),
    (
        "1189585",
        date(2025, 1, 8),
        {
            "revenue_from_operations": "6441900000",
            "profit_after_tax": "219700000",
            "other_income": "10200000",
            "total_income": "6452100000",
            "total_expenses": "6158600000",
            "profit_before_tax": "293500000",
        },
    ),
)

ASTEC_PRIORS: Final[tuple[_Quarter, ...]] = (
    (
        "1178295",
        date(2024, 7, 29),
        {"revenue_from_operations": "693725000", "profit_after_tax": "-397154000"},
    ),
    (
        "1182734",
        date(2024, 10, 25),
        {"revenue_from_operations": "985456000", "profit_after_tax": "-385489000"},
    ),
    (
        "1190793",
        date(2025, 1, 27),
        {"revenue_from_operations": "938584000", "profit_after_tax": "-403683000"},
    ),
)


def _prior_facts(
    isin: str,
    nature: Nature,
    priors: tuple[_Quarter, ...],
    *,
    refiled: Callable[[int, date], date] = lambda index, filed: filed,
) -> list[FundamentalFact]:
    facts: list[FundamentalFact] = []
    for index, ((filing_id, filed, values), (start, end)) in enumerate(
        zip(priors, _FY25_QUARTERS, strict=True), start=1
    ):
        for concept, value in values.items():
            facts.append(
                FundamentalFact(
                    isin=isin,
                    period_start=start,
                    period_end=end,
                    filing_date=refiled(index, filed),
                    nature=nature,
                    taxonomy=Taxonomy.IND_AS,
                    filing_id=filing_id,
                    concept=concept,
                    value=Decimal(value),
                    source=parser.SOURCE_ID,
                )
            )
    return facts


def _gmbrew_priors(**kwargs: Any) -> list[FundamentalFact]:
    return _prior_facts(GMBREW, Nature.STANDALONE, GMBREW_PRIORS, **kwargs)


def _value(filing: Filing, concept: str) -> Decimal:
    return next(f.value for f in filing.company_facts() if f.concept == concept)


# ── the parser finds the class-A shape, and only it ──────────────────────────────────────────


def test_the_class_a_document_is_refused_as_a_quarter_but_carries_its_candidate() -> None:
    candidate = _candidate(GMBREW_DOC, _gmbrew_entry())
    assert (candidate.stated_start, candidate.stated_end) == (date(2024, 10, 1), _Q4FY25[1])
    assert (candidate.cumulative_start, candidate.cumulative_end) == (date(2024, 4, 1), _Q4FY25[1])
    assert candidate.reporting_period_label == "Half Yearly"
    assert (candidate.stated_column, candidate.cumulative_column) == ("OneD", "FourD")
    # Built as the asked quarter, from the document's own OneD values — nothing computed.
    filing = candidate.filing
    assert (filing.period_start, filing.period_end) == _Q4FY25
    assert _value(filing, "revenue_from_operations") == Decimal("6631300000")
    assert _value(filing, "profit_after_tax") == Decimal("604600000")
    assert all(f.period_start == _Q4FY25[0] for f in filing.facts)
    assert not any(f.derived and f.concept in q4_relabel.WITNESSES for f in filing.facts)
    assert candidate.cumulative["revenue_from_operations"] == Decimal("25036900000")
    assert candidate.document_step == Decimal("100000")


def test_the_annual_entry_on_the_same_document_is_untouched() -> None:
    """The `-FY` entry matches FourD exactly, as it always did: no candidate, no relabel."""
    entry = _gmbrew_entry().model_copy(
        update={"period_start": date(2024, 4, 1), "period": "Annual", "seq_number": "IF84947-FY"}
    )
    filing = parser.parse(
        (INTEGRATED / GMBREW_DOC).read_bytes(),
        entry=entry,
        known_symbols=frozenset({"GMBREW"}),
        filename=GMBREW_DOC,
    )
    assert (filing.period_start, filing.period_end) == (date(2024, 4, 1), _Q4FY25[1])


# ── the check: a passing relabel, a genuine H2, and look-ahead ───────────────────────────────


def test_a_q4_column_equal_to_the_year_minus_nine_months_passes() -> None:
    verdict = q4_relabel.check(_candidate(GMBREW_DOC, _gmbrew_entry()), _gmbrew_priors())
    assert verdict.passed, verdict.reason
    revenue = verdict.result["concepts"]["revenue_from_operations"]
    assert revenue["cumulative_minus_priors"] == "6631300000"
    assert revenue["difference"] == "0" and revenue["tolerance"] == "500000"
    assert verdict.result["concepts"]["profit_after_tax"]["difference"] == "0"
    assert [q["filing_id"] for q in revenue["prior_quarters"]] == ["1177449", "1181388", "1189585"]


def test_a_difference_beyond_rounding_is_refused_however_small_relative_to_the_year() -> None:
    """Tolerance is rounding only: one unit per figure (5 x ₹1 lakh here), not a percentage."""
    candidate = _candidate(GMBREW_DOC, _gmbrew_entry())
    at_tolerance = [
        f.model_copy(update={"value": f.value - Decimal("500000")})
        if f.filing_id == "1189585" and f.concept == "profit_after_tax"
        else f
        for f in _gmbrew_priors()
    ]
    assert q4_relabel.check(candidate, at_tolerance).passed
    beyond = [
        f.model_copy(update={"value": f.value - Decimal("600000")})
        if f.filing_id == "1189585" and f.concept == "profit_after_tax"
        else f
        for f in _gmbrew_priors()
    ]
    verdict = q4_relabel.check(candidate, beyond)
    assert not verdict.passed
    assert "profit_after_tax" in verdict.reason and "rounding tolerance 500000" in verdict.reason


def test_revenue_alone_or_profit_alone_never_passes() -> None:
    candidate = _candidate(GMBREW_DOC, _gmbrew_entry())
    for concept in q4_relabel.WITNESSES:
        skewed = [
            f.model_copy(update={"value": f.value + Decimal("10000000")})
            if f.filing_id == "1181388" and f.concept == concept
            else f
            for f in _gmbrew_priors()
        ]
        verdict = q4_relabel.check(candidate, skewed)
        assert not verdict.passed and concept in verdict.reason


def test_a_genuine_second_half_year_is_refused_saying_which_check_failed() -> None:
    """Astec's OneD is the year minus two quarters: six months, not the January quarter."""
    candidate = _candidate(ASTEC_DOC, _astec_entry())
    priors = _prior_facts(ASTEC, Nature.CONSOLIDATED, ASTEC_PRIORS)
    verdict = q4_relabel.check(candidate, priors)
    assert not verdict.passed
    assert verdict.reason.startswith(f"{q4_relabel.RULE} refused: revenue_from_operations")
    assert "differ by 938584000" in verdict.reason  # exactly Q3: the half holds it
    assert "a genuine half-year" in verdict.reason
    # …and it is a half-year: the year minus Q1 and Q2 is the column, to the rupee.
    year = candidate.cumulative["revenue_from_operations"]
    assert (
        year - Decimal("693725000") - Decimal("985456000")
        == candidate.stated["revenue_from_operations"]
    )


def test_a_prior_quarter_filed_after_the_q4_filing_is_look_ahead_and_refused() -> None:
    """Invariant #7: Q3 republished only after 2025-04-15 is not known on that date."""
    candidate = _candidate(GMBREW_DOC, _gmbrew_entry())
    late = _gmbrew_priors(refiled=lambda index, filed: date(2025, 4, 16) if index == 3 else filed)
    verdict = q4_relabel.check(candidate, late)
    assert not verdict.passed
    assert "Q3 2024-10-01..2024-12-31" in verdict.reason
    assert "filed only after this filing (2025-04-16): look-ahead" in verdict.reason
    # The same day is "on or before": the same quarter filed 2025-04-15 is admissible.
    same_day = _gmbrew_priors(
        refiled=lambda index, filed: date(2025, 4, 15) if index == 3 else filed
    )
    assert q4_relabel.check(candidate, same_day).passed


def test_a_missing_prior_quarter_is_not_checkable() -> None:
    candidate = _candidate(GMBREW_DOC, _gmbrew_entry())
    without_q1 = [f for f in _gmbrew_priors() if f.filing_id != "1177449"]
    verdict = q4_relabel.check(candidate, without_q1)
    assert not verdict.passed and "Q1 2024-04-01..2024-06-30" in verdict.reason
    assert "not on file" in verdict.reason


def test_other_natures_isins_and_derived_facts_are_not_evidence() -> None:
    candidate = _candidate(GMBREW_DOC, _gmbrew_entry())
    for change in (
        {"nature": Nature.CONSOLIDATED},
        {"isin": ASTEC},
        {"derived": True},
    ):
        verdict = q4_relabel.check(
            candidate, [f.model_copy(update=change) for f in _gmbrew_priors()]
        )
        assert not verdict.passed and "not on file" in verdict.reason


def test_the_latest_version_on_or_before_the_filing_date_is_the_one_used() -> None:
    """A restated Q2 filed before the Q4 is what the year was built on; one filed after is not."""
    candidate = _candidate(GMBREW_DOC, _gmbrew_entry())
    restated = [
        f.model_copy(
            update={
                "filing_id": "RESTATED",
                "filing_date": date(2025, 2, 1),
                "value": f.value + Decimal("50000000"),
            }
        )
        for f in _gmbrew_priors()
        if f.filing_id == "1181388"
    ]
    assert not q4_relabel.check(candidate, _gmbrew_priors() + restated).passed
    after = [f.model_copy(update={"filing_date": date(2025, 5, 1)}) for f in restated]
    assert q4_relabel.check(candidate, _gmbrew_priors() + after).passed


def test_rounding_step_reads_the_unit_off_the_amounts() -> None:
    assert q4_relabel.rounding_step([Decimal("6631300000"), Decimal("604600000")]) == 100000
    assert q4_relabel.rounding_step([Decimal("507788000.0000"), Decimal("0")]) == 1000
    assert q4_relabel.rounding_step([Decimal("10000000000000")]) == q4_relabel.MAX_ROUNDING_STEP
    assert q4_relabel.rounding_step([Decimal("12.34")]) == Decimal("0.01")
    assert q4_relabel.rounding_step([]) == 1


def test_prior_quarters_cover_a_july_june_year_too() -> None:
    """P&G Hygiene-type filers: the cumulative column is the nine months from July."""
    assert q4_relabel.prior_quarters(date(2024, 7, 1), date(2025, 1, 1)) == (
        (date(2024, 7, 1), date(2024, 9, 30)),
        (date(2024, 10, 1), date(2024, 12, 31)),
    )
    assert q4_relabel.prior_quarters(date(2024, 5, 1), date(2025, 1, 1)) == ()


# ── the runner: stored only with its provenance, from what L1 held by the filing date ────────


def _runner(tmp_path: Path, record: Callable[[q4_relabel.RelabelRecord], None] | None) -> Any:
    clock = FrozenClock(datetime(2026, 11, 3, 2, 30, tzinfo=IST))
    l0 = L0Store(clock=clock, data_root=tmp_path)
    runner = fb.FundamentalsBackfillRunner(
        fetcher=cast(Any, None),
        l0=l0,
        sync=cast(Any, None),
        universe={GMBREW},
        commit=lambda: None,
        data_root=tmp_path,
        clock=clock,
        record_relabel=record,
    )
    entry = _gmbrew_entry()
    ref = l0.put(
        parser.SOURCE_ID, entry.filing_date, GMBREW_DOC, (INTEGRATED / GMBREW_DOC).read_bytes()
    )
    unit = fb.FilingUnit(entry=entry, url=str(entry.xbrl_url), filename=GMBREW_DOC)
    return runner, unit, ref


def _write_priors(tmp_path: Path, facts: list[FundamentalFact]) -> None:
    for filing_id in sorted({f.filing_id for f in facts}):
        own = tuple(f for f in facts if f.filing_id == filing_id)
        write_pit(
            Filing(
                isin=own[0].isin,
                symbol="GMBREW",
                taxonomy=Taxonomy.IND_AS,
                name="G M Breweries",
                period_start=own[0].period_start,
                period_end=own[0].period_end,
                filing_date=own[0].filing_date,
                nature=own[0].nature,
                filing_id=filing_id,
                source=parser.SOURCE_ID,
                facts=own,
            ),
            data_root=tmp_path,
        )


def test_the_runner_stores_the_relabel_with_its_provenance(tmp_path: Path) -> None:
    _write_priors(tmp_path, _gmbrew_priors())
    runner, unit, ref = _runner(tmp_path, record=lambda row: None)
    filing, relabel = runner._parse(unit, ref)
    assert (filing.period_start, filing.period_end) == _Q4FY25
    assert relabel is not None and relabel.rule == "Q4_RELABEL_FY_MINUS_9M_CHECK"
    assert (relabel.source, relabel.unit, relabel.logical_date) == (
        "nse_xbrl_filing",
        "IF84947",
        date(2025, 4, 15),
    )
    assert (relabel.stated_start, relabel.stored_start) == (date(2024, 10, 1), date(2025, 1, 1))
    assert relabel.check_result["concepts"]["profit_after_tax"]["agrees"] is True


def test_the_runner_refuses_a_relabel_it_has_nowhere_to_record(tmp_path: Path) -> None:
    _write_priors(tmp_path, _gmbrew_priors())
    runner, unit, ref = _runner(tmp_path, record=None)
    with pytest.raises(ParseError, match="no provenance record"):
        runner._parse(unit, ref)


def test_the_runner_reads_no_quarter_filed_after_the_filing(tmp_path: Path) -> None:
    late = _gmbrew_priors(refiled=lambda index, filed: date(2025, 4, 16) if index == 3 else filed)
    _write_priors(tmp_path, late)
    runner, unit, ref = _runner(tmp_path, record=lambda row: None)
    with pytest.raises(ParseError) as refused:
        runner._parse(unit, ref)
    message = str(refused.value)
    assert "no results column covers 2025-01-01→2025-03-31" in message  # the original refusal
    assert "Q4_RELABEL_FY_MINUS_9M_CHECK refused: prior quarter Q3" in message
    assert "not on file" in message  # the runner never even reads the later partition


# ── class G: INDBANK's one-character ISIN typo, and the sister companies that stay refused ────


def _indbank_entry() -> FilingIndexEntry:
    return _entry(
        isin=INDBANK,
        symbol="INDBANK",
        start=date(2026, 4, 1),
        end=date(2026, 6, 30),
        filed=date(2026, 7, 8),
        seq="IF173185",
    )


def _parse_indbank(
    *, bse_codes: frozenset[str] | None, isins_near: Callable[[str], frozenset[str]] | None
) -> Filing:
    return parser.parse(
        (INTEGRATED / INDBANK_DOC).read_bytes(),
        entry=_indbank_entry(),
        known_symbols=frozenset({"INDBANK"}),
        bse_codes=bse_codes,
        isins_near=isins_near,
        filename=INDBANK_DOC,
    )


def _only_indbank_near(stated: str) -> frozenset[str]:
    return frozenset({INDBANK}) if stated == INDBANK_STATED else frozenset()


def test_indbanks_one_character_isin_typo_is_accepted() -> None:
    filing = _parse_indbank(bse_codes=frozenset({INDBANK_BSE}), isins_near=_only_indbank_near)
    assert filing.isin == INDBANK  # the join key is the index's, never the typo
    assert (filing.period_start, filing.period_end) == (date(2026, 4, 1), date(2026, 6, 30))


@pytest.mark.parametrize(
    ("bse_codes", "isins_near"),
    [
        # A sister company: D2 holds the stated ISIN itself, as another company's.
        (frozenset({INDBANK_BSE}), lambda s: frozenset({INDBANK, INDBANK_STATED})),
        # Another D2 ISIN within one character of the stated one: which was meant is unknowable.
        (frozenset({INDBANK_BSE}), lambda s: frozenset({INDBANK, "INE841C01017"})),
        # The BSE code the document states is not the one D2 holds for the entry's ISIN.
        (frozenset({"999999"}), _only_indbank_near),
        # No D2 code to corroborate the document's.
        (None, _only_indbank_near),
        # No master to ask.
        (frozenset({INDBANK_BSE}), None),
    ],
)
def test_the_isin_typo_stays_refused_unless_every_condition_holds(
    bse_codes: frozenset[str] | None, isins_near: Callable[[str], frozenset[str]] | None
) -> None:
    with pytest.raises(ParseError, match="states ISIN INE841801017 — a different issuer"):
        _parse_indbank(bse_codes=bse_codes, isins_near=isins_near)


def test_ashapura_stating_orient_ceratechs_isin_stays_refused() -> None:
    """Symbol and BSE code are Ashapura's; the ISIN is a sister company's, many characters off."""
    entry = _entry(
        isin=ASHAPURMIN,
        symbol="ASHAPURMIN",
        start=date(2025, 4, 1),
        end=date(2025, 6, 30),
        filed=date(2025, 8, 12),
        seq="IF110561",
    )
    with pytest.raises(ParseError, match=f"states ISIN {ORIENT_CERATECH} — a different issuer"):
        parser.parse(
            (INTEGRATED / ASHAPURMIN_DOC).read_bytes(),
            entry=entry,
            known_symbols=frozenset({"ASHAPURMIN"}),
            bse_codes=frozenset({"527001"}),
            isins_near=lambda stated: frozenset({stated}),
            filename=ASHAPURMIN_DOC,
        )


def test_isins_near_of_finds_one_character_neighbours_in_d2() -> None:
    # INE841B01018 is one character from INDBANK's ISIN but two from the stated typo.
    master = cast(Any, type("M", (), {"securities": {INDBANK: None, "INE841B01018": None}})())
    near = fb.isins_near_of(master)
    assert near(INDBANK_STATED) == frozenset({INDBANK})
    assert near(INDBANK) == frozenset({INDBANK, "INE841B01018"})
    assert fb.isins_near_of(cast(Any, type("M", (), {"securities": {INDBANK_STATED: None}})()))(
        INDBANK_STATED
    ) == frozenset({INDBANK_STATED})
    assert near("INE000000000") == frozenset()
