"""The shape of a point-in-time fundamentals filing and its data, shared by parser and store.

The whole reason this dataset exists as a separate path from Screener (M7.1) is invariant #8: the
numbers here are *point-in-time truth*, tagged with the first date they were knowable, so a
backtest can read them without leaking the future. Two facts make that possible and are therefore
first-class, range-checked fields rather than notes:

* **Every datum carries `(period_end, filing_date)`, and neither is inferred from the other.**
  `period_end` is the quarter (or year) the numbers are *about*; `filing_date` is the exchange
  dissemination date — the first date the market could have known them (§4.1: "filing timestamp is
  first-knowable date"). A quarter that ended 30-Jun is only knowable weeks later when the results
  are filed, and storing one and deriving the other would either back-date the knowledge (a
  look-ahead leak, invariant #7) or lose the period a number describes.
* **Standalone and consolidated are distinct records, not a flag on one number.** A company reports
  the same line item twice — for the parent alone and for the group — and the two differ. Collapsing
  them would silently pick one; the analyst's segment-revenue break condition (§5.3 BC1) needs the
  one the thesis was written against, so `nature` is part of the identity of every fact.

Restatements are kept, never overwritten (acceptance 3). A later filing that restates an earlier
period is a *different* filing with a later `filing_date`, and both physically coexist in the store;
picking the latest-knowable version as of a decision date is a read-time concern (`read_latest`),
not a reason to destroy the version the market actually saw at the time.

Money is `Decimal` (CLAUDE.md): a fundamental value that arrived as a float would be a bug, so the
value field is `strict` and finite — a mis-framed field that spells `NaN` is a parse failure, not a
revenue that compares greater than everything.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, Field, model_validator

from dataplatform.ingest.models import ISIN_PATTERN

__all__ = [
    "BANKING_CONCEPTS",
    "COMMON_CONCEPT_KEYS",
    "CONCEPTS",
    "CONCEPT_KEYS",
    "CONDITIONAL_CONCEPT_KEYS",
    "DERIVED_CONCEPTS",
    "IND_AS_CONCEPTS",
    "NON_IND_AS_CONCEPTS",
    "SHAREHOLDERS_EQUITY",
    "SHARES_OUTSTANDING",
    "Filing",
    "FundamentalFact",
    "Nature",
    "Taxonomy",
    "concepts_for",
]


class Nature(StrEnum):
    """Whether a filing reports the parent alone or the whole group.

    Spelled as the *XBRL document* states it (`NatureOfReportStandaloneConsolidated`), because that
    is the value stored on every fact and round-tripped through the store and a log line unchanged.
    The announcements index spells the same distinction differently — `Non-Consolidated` for what
    the document calls `Standalone` — and `discovery` maps its vocabulary onto this one. Two
    spellings for one concept is exactly the kind of thing a `StrEnum` exists to collapse once.
    """

    STANDALONE = "Standalone"
    CONSOLIDATED = "Consolidated"


class Taxonomy(StrEnum):
    """Which in-bse-fin results taxonomy a filing was prepared against.

    NSE serves results filings against one of two BSE-published entry points, and they do not share
    a P&L vocabulary — a bank's operating revenue is `InterestEarned`, a manufacturer's is
    `RevenueFromOperations`, and neither element exists in the other's taxonomy. Reading a filing
    with the wrong vocabulary does not fail, it silently finds nothing, so the family is resolved
    once, explicitly, and the concept map is chosen from it (`concepts_for`).

    Three families, one per entry point NSE actually serves (counted over a decade of the index:
    `Ind-AS New` ~92%, `NBFC-IND` ~6%, `Non-Ind-AS` ~2%, the last split between banks and everyone
    else):

    * `IND_AS` covers the general Ind-AS entry point (`Ind-AS_entry_point_*.xsd`) and the NBFC one
      (`in-bse-fin-*.xsd`): NBFC filings add finance-specific elements (`InterestEarned`,
      `FeesAndCommissionIncome`) but still report the whole Ind-AS P&L spine, so one vocabulary
      reads both.
    * `BANKING` (`banking_entry_point_*.xsd`) is the RBI-format bank return.
    * `NON_IND_AS` (`other_than_banks_entry_point_*.xsd`) is the pre-Ind-AS Indian-GAAP form still
      filed by companies outside the Ind-AS net. Closest to `IND_AS`, but not the same: total
      income is `Revenue`, not `Income`, and the bottom line is `ProfitLossForThePeriod`, not
      `ProfitLossForPeriod`.

    The version in an entry point's filename is deliberately not part of the identity — matching is
    on the stem, so `Ind-AS_entry_point_2017-03-31` and `…_2020-03-31` read with one vocabulary. A
    taxonomy revision that renamed elements would surface as a column reporting no concepts, which
    is a hard failure, not silence.
    """

    IND_AS = "Ind-AS"
    BANKING = "Banking"
    NON_IND_AS = "Non-Ind-AS"


#: Balance-sheet elements the results filing does carry, and which the whitelist used to stop short
#: of. A quarterly filing is profit-and-loss only, but SEBI LODR requires a balance sheet alongside
#: *annual* results and the XBRL states it — measured over 2,500 captured documents, the two capital
#: elements appear in **100%** of filings of every family and the reserves line in 25-48% (which is
#: essentially "the annual ones"). Element names are identical across the three taxonomies here, so
#: this block is shared rather than repeated three times with the same strings.
#:
#: `reserves_excl_revaluation` is named for what it excludes, deliberately. A vendor reporting
#: "reserves" reports the total *including* any revaluation surplus, so the two legitimately differ
#: for an asset-heavy company — cross-checked against Screener, ours matched to the rupee on 8 of 13
#: companies and the two large gaps were exactly that surplus. Excluding it is the better basis for
#: a book-value ratio (a revaluation writeup moves no cash and is management's discretion), but a
#: reader must be able to see which basis they hold from the name alone.
_CAPITAL_CONCEPTS: Final[dict[str, str]] = {
    "PaidUpValueOfEquityShareCapital": "paid_up_equity_capital",
    "FaceValueOfEquityShareCapital": "face_value_per_share",
    "ReserveExcludingRevaluationReserves": "reserves_excl_revaluation",
}

#: The parent's own share of a consolidated profit — the figure the reported EPS is actually struck
#: on, and therefore the right numerator for any per-share ratio on a group filing.
#:
#: Not a refinement: for a holding company it is a different number entirely. GRASIM's consolidated
#: `ProfitLossForPeriod` for Q3 FY25 is ₹1,844 crore, but it consolidates UltraTech and Aditya Birla
#: Capital and most of that profit belongs to their minority holders — the parent's share is ₹901
#: crore, almost exactly half. A P/E built on the consolidated bottom line and a market cap built on
#: the parent's shares is comparing a group's earnings to a parent's equity, which understates the
#: multiple by 2x for exactly the companies where it matters most.
#:
#: Element per family, and the third one is a deliberate omission rather than an oversight: Ind-AS
#: states the parent's share directly (80% of consolidated filings, 11% of standalone ones where it
#: equals the bottom line anyway); banking states it as "after minority interest", the same quantity
#: named differently, in 100% of filings; and the pre-Ind-AS form states only
#: `ProfitLossForPeriodBeforeMinorityInterest`, which is the wrong side of the deduction. Mapping
#: that one would put the group's profit under a key promising the parent's, so it is left out and
#: those filings fall back to the bottom line — 3 consolidated filings in a 4,000-document sample.
_IND_AS_OWNERS_CONCEPTS: Final[dict[str, str]] = {
    "ProfitOrLossAttributableToOwnersOfParent": "profit_attributable_to_owners",
}
_BANKING_OWNERS_CONCEPTS: Final[dict[str, str]] = {
    "ProfitLossAfterTaxesMinorityInterestAndShareOfProfitLossOfAssociates": (
        "profit_attributable_to_owners"
    ),
}

#: Leverage, as the filer computed it. Ind-AS and the pre-Ind-AS form state it in ~27-43% of
#: filings; **no** bank filing does, which is correct rather than missing — a debt-to-equity ratio
#: is not a meaningful figure for a deposit-taking institution. Included despite being a ratio
#: someone else computed (normally a reason to refuse a number we cannot reconstruct) because
#: leverage has no other route out of this dataset: borrowings are not stated anywhere in it.
_LEVERAGE_CONCEPTS: Final[dict[str, str]] = {
    "DebtEquityRatio": "debt_equity_ratio",
}

#: Asset quality, in **100%** of bank filings and in no other family. This is the one place the
#: banking taxonomy is richer than the Ind-AS one rather than merely different, and it is what makes
#: a genuine quality factor possible for banks — where `reserves_excl_revaluation` gives book value
#: but says nothing about whether the loan book is sound.
_BANK_QUALITY_CONCEPTS: Final[dict[str, str]] = {
    "GrossNonPerformingAssets": "gross_npa",
    "NonPerformingAssets": "net_npa",
    "PercentageOfGrossNpa": "gross_npa_pct",
    "PercentageOfNpa": "net_npa_pct",
    "ReturnOnAssets": "return_on_assets",
    "CET1Ratio": "cet1_ratio",
}

#: Concepts the parser *computes* rather than reads, from elements above. Kept separate because a
#: derived fact has a provenance a mapped one does not: it can be wrong even when both its inputs
#: were read correctly, so each carries its own guard in the parser.
SHARES_OUTSTANDING: Final = "shares_outstanding"
SHAREHOLDERS_EQUITY: Final = "shareholders_equity_excl_revaluation"
DERIVED_CONCEPTS: Final[frozenset[str]] = frozenset({SHARES_OUTSTANDING, SHAREHOLDERS_EQUITY})

#: The monetary/EPS concepts this parser lifts out of an Ind-AS (and NBFC) results filing, keyed by
#: the stable snake_case name the rest of the platform uses, mapped from the in-bse-fin element
#: local-name it really appears as. A curated whitelist, not "every tag": these are the line items
#: §5 reasons about, and a concept a consumer cannot name is a concept it cannot use.
#:
#: The element names are taken from captured filings (`tests/fixtures/xbrl/`), not from reading the
#: taxonomy: the schema admits `TotalIncome`-style names that no real NSE filing uses, and the four
#: this map used to guess (`TotalIncome`, `TotalExpenses`, `BasicEarningsPerShare`,
#: `DilutedEarningsPerShare`) matched nothing in any of them.
IND_AS_CONCEPTS: Final[dict[str, str]] = {
    "RevenueFromOperations": "revenue_from_operations",
    "OtherIncome": "other_income",
    "Income": "total_income",
    "Expenses": "total_expenses",
    "ProfitBeforeTax": "profit_before_tax",
    "ProfitLossForPeriod": "profit_after_tax",
    # Continuing *and* discontinued is the headline EPS a bottom-line P/E wants — the same basis as
    # `ProfitLossForPeriod` above. The continuing-only variants are reported too and deliberately
    # left out: mixing bases across concepts is how a ratio quietly stops meaning anything.
    "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations": "eps_basic",
    "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations": "eps_diluted",
    **_CAPITAL_CONCEPTS,
    **_LEVERAGE_CONCEPTS,
    **_IND_AS_OWNERS_CONCEPTS,
}

#: The same platform concept keys against the banking taxonomy's own element names, so a bank's
#: facts land under the keys every consumer already reads rather than a parallel set nobody queries.
#:
#: Two mappings are judgement, not translation, and are called out because a ratio built on them
#: inherits the judgement: `revenue_from_operations` is `InterestEarned` — interest and discount on
#: advances, investments and inter-bank funds, which *is* a bank's operating revenue and excludes
#: `OtherIncome` exactly as the Ind-AS element does — and `total_expenses` is
#: `ExpenditureExcludingProvisionsAndContingencies`, which (as its name says) excludes provisions
#: and contingencies, so it is not comparable line-for-line with an Ind-AS `Expenses`. Bottom-line
#: concepts (`profit_before_tax`, `profit_after_tax`, EPS) are directly equivalent.
BANKING_CONCEPTS: Final[dict[str, str]] = {
    "InterestEarned": "revenue_from_operations",
    "OtherIncome": "other_income",
    "Income": "total_income",
    "ExpenditureExcludingProvisionsAndContingencies": "total_expenses",
    "ProfitLossFromOrdinaryActivitiesBeforeTax": "profit_before_tax",
    "ProfitLossForThePeriod": "profit_after_tax",
    "BasicEarningsPerShareAfterExtraordinaryItems": "eps_basic",
    "DilutedEarningsPerShareAfterExtraordinaryItems": "eps_diluted",
    **_CAPITAL_CONCEPTS,
    **_BANK_QUALITY_CONCEPTS,
    **_BANKING_OWNERS_CONCEPTS,
}

#: The pre-Ind-AS Indian-GAAP form (`other_than_banks_entry_point_*`). Ind-AS-shaped apart from two
#: elements, and both differences are naming rather than meaning: `Revenue` is the total-income line
#: (verified on captured filings — `RevenueFromOperations + OtherIncome == Revenue` exactly), and
#: `ProfitLossForThePeriod` is the same bottom line the Ind-AS form calls `ProfitLossForPeriod`.
#: Spelled out as its own map rather than folded into `IND_AS` with fallbacks: a document that turns
#: out to speak neither dialect must fail loudly, not quietly match a second choice.
NON_IND_AS_CONCEPTS: Final[dict[str, str]] = {
    "RevenueFromOperations": "revenue_from_operations",
    "OtherIncome": "other_income",
    "Revenue": "total_income",
    "Expenses": "total_expenses",
    "ProfitBeforeTax": "profit_before_tax",
    "ProfitLossForThePeriod": "profit_after_tax",
    "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations": "eps_basic",
    "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations": "eps_diluted",
    **_CAPITAL_CONCEPTS,
    **_LEVERAGE_CONCEPTS,
}

#: Element local-name → platform concept key, per taxonomy family.
CONCEPTS: Final[dict[Taxonomy, dict[str, str]]] = {
    Taxonomy.IND_AS: IND_AS_CONCEPTS,
    Taxonomy.BANKING: BANKING_CONCEPTS,
    Taxonomy.NON_IND_AS: NON_IND_AS_CONCEPTS,
}

#: Every platform concept key this parser can produce, across all taxonomies plus the derived ones.
#: A union rather than one family's values: the P&L spine is shared, but a bank reports asset
#: quality no other filer does and reports no debt-to-equity ratio, so no single family enumerates
#: the whole vocabulary any more.
CONCEPT_KEYS: Final[frozenset[str]] = (
    frozenset(
        key
        for family in (IND_AS_CONCEPTS, BANKING_CONCEPTS, NON_IND_AS_CONCEPTS)
        for key in family.values()
    )
    | DERIVED_CONCEPTS
)

#: Concepts a filing may legitimately omit, so their absence is data rather than a defect. Two
#: reasons, both measured over 3,000 captured documents:
#:
#: * **Period.** A results filing states a balance sheet only alongside *annual* results, so the
#:   reserves line appears in 24% of Ind-AS filings, 16% of banking and 41% of the pre-Ind-AS form —
#:   which is roughly "the annual ones" in each. `shareholders_equity_excl_revaluation` inherits
#:   that, being derived from it.
#: * **Filer discretion.** `DebtEquityRatio` is stated by 26% of Ind-AS filers and 47% of pre-Ind-AS
#:   ones, and by no bank at all.
#: * **Nature.** `profit_attributable_to_owners` is stated by 80% of consolidated Ind-AS filings and
#:   11% of standalone ones — a standalone filing has no minority to attribute away, so there the
#:   bottom line already *is* the owners' share and the separate element is redundant.
#:
#: `shares_outstanding` is here for a third reason: its inputs are universal but it is withheld when
#: the filing's own EPS does not corroborate it (~5% of filings), which is a refusal, not a gap.
#:
#: Everything in `CONCEPT_KEYS` outside this set is stated by every filing of the families that map
#: it, which is what makes a missing one a parse regression worth failing on.
CONDITIONAL_CONCEPT_KEYS: Final[frozenset[str]] = frozenset(
    {"reserves_excl_revaluation", "debt_equity_ratio", "profit_attributable_to_owners"}
    | DERIVED_CONCEPTS
)

#: The concepts every family states, and therefore the only ones a consumer may assume are present
#: for any filing. Everything else in `CONCEPT_KEYS` is family- or period-conditional.
COMMON_CONCEPT_KEYS: Final[frozenset[str]] = frozenset(
    set(IND_AS_CONCEPTS.values())
    & set(BANKING_CONCEPTS.values())
    & set(NON_IND_AS_CONCEPTS.values())
)


def concepts_for(taxonomy: Taxonomy) -> dict[str, str]:
    """The element local-name → concept key map for one taxonomy family."""
    return CONCEPTS[taxonomy]


#: A fundamental value. `strict` keeps floats out by construction; `allow_inf_nan=False` keeps a
#: mis-parsed field from becoming a plausible-looking number. No `ge=0`: a loss, a negative other
#: income or a negative EPS are all real, and rejecting them would fail on real filings.
Value = Annotated[Decimal, Field(strict=True, allow_inf_nan=False)]


class FundamentalFact(BaseModel):
    """One reported number from one filing — the atomic PIT datum.

    What it does: carry a single value for one `(isin, period_end, nature, concept, segment)`,
    tagged with the `filing_date` on which it first became knowable and the `filing_id` that
    distinguishes it from a later restatement of the same period.
    What it assumes: the parser already validated the filing's structure, so a fact that exists is
    one the filing really reported, filed strictly after the period it reports.
    What it never does: infer `filing_date` from `period_end`, hold a `float`, or merge standalone
    and consolidated — `nature` is part of its identity.

    Two fields exist to keep a consumer from having to guess where a number came from:

    * **`taxonomy`** — a bank's `revenue_from_operations` is `InterestEarned` and its
      `total_expenses` excludes provisions, so the same concept key does not mean the same thing
      across families. A cross-sectional screen that ranks banks and manufacturers on one key needs
      to *see* that in the row rather than re-derive it by joining back to the filing.
    * **`derived`** — whether the parser computed this number or read it. A derived value can be
      wrong even when both its inputs were read correctly, so it carries a different burden of proof
      than a stated one, and anything auditing the store must be able to separate the two.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    isin: str = Field(
        pattern=ISIN_PATTERN, description="ISO 6166 identifier — the only join key (invariant #2)"
    )
    period_start: date | None = Field(
        default=None, description="first day of the reporting period; None if the filing omitted it"
    )
    period_end: date = Field(description="the period the number is about (quarter or year end)")
    filing_date: date = Field(
        description="first date the number was knowable (exchange dissemination); after period_end"
    )
    nature: Nature = Field(description="Standalone or Consolidated — part of the fact's identity")
    taxonomy: Taxonomy = Field(
        description="entry point the filing was prepared against; concept keys are not cross-family"
    )
    filing_id: str = Field(
        min_length=1, description="stable id of the filing this came from; keys restatements apart"
    )
    concept: str = Field(min_length=1, description="snake_case concept key, or 'segment_revenue'")
    segment: str | None = Field(
        default=None, description="business-segment name for a segment datum; None at company level"
    )
    value: Value = Field(description="the reported value, exact and finite (CLAUDE.md: Decimal)")
    derived: bool = Field(
        default=False, description="computed by the parser from stated elements, not read from one"
    )
    source: str = Field(min_length=1, description="Source Register id the filing came from")
    l0_key: str | None = Field(
        default=None, description="`source/date/filename` of the L0 payload this was derived from"
    )

    @model_validator(mode="after")
    def _filing_after_period(self) -> FundamentalFact:
        """A filing cannot predate — or coincide with — the period it reports (§4.1, invariant #7).

        Results are disseminated after the period closes; a filing dated on or before its period end
        is a transposition, and letting it through would place a datum in the store as knowable
        before it could exist. If the two dates were one field this could not fail — which is
        exactly the single-date mistake this schema refuses.
        """
        if self.filing_date <= self.period_end:
            raise ValueError(
                f"{self.isin} {self.period_end.isoformat()}: filing_date "
                f"{self.filing_date.isoformat()} is not after the period end; a results filing is "
                "disseminated after the period it reports (§4.1)"
            )
        return self


class Filing(BaseModel):
    """One results filing — every fact one XBRL document reported, under one nature and period.

    What it does: hold the company-level and segment facts of a single filing, tagged with the
    `(period_end, filing_date, nature)` they share, plus the lineage of the L0 payload they came
    from so a store partition can name where it was derived.
    What it assumes: the parser validated that every fact shares this filing's period and nature,
    and that the filing reported at least the headline revenue — an empty filing is a parse failure.
    What it never does: mix natures or periods; a document that did is rejected, not split silently.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    isin: str = Field(pattern=ISIN_PATTERN, description="ISO 6166 identifier (invariant #2)")
    symbol: str = Field(
        min_length=1,
        description="the symbol the document filed under (as-of, not today's); never a join key",
    )
    taxonomy: Taxonomy = Field(
        description="which in-bse-fin entry point the filing was prepared against"
    )
    name: str = Field(min_length=1, description="company name as filed, for display only")
    period_start: date | None = Field(default=None, description="first day of the reporting period")
    period_end: date = Field(description="the period the filing reports (quarter or year end)")
    filing_date: date = Field(description="first-knowable date; strictly after period_end")
    nature: Nature = Field(description="Standalone or Consolidated for the whole filing")
    filing_id: str = Field(
        min_length=1, description="stable id; a restatement carries a different one"
    )
    audited: bool | None = Field(
        default=None, description="whether the results are audited; None if the filing did not say"
    )
    source: str = Field(min_length=1, description="Source Register id the filing came from")
    l0_key: str | None = Field(default=None, description="`source/date/filename` of the L0 payload")
    facts: tuple[FundamentalFact, ...] = Field(description="company-level and segment facts")

    @model_validator(mode="after")
    def _facts_agree_with_the_filing(self) -> Filing:
        """Every fact shares this filing's identity, and the filing is not empty.

        The store trusts that a filing's facts are homogeneous in `(isin, period_end, filing_date,
        nature, filing_id)`; enforcing it here means a downstream reader never has to re-check.
        """
        if not self.facts:
            raise ValueError(
                f"{self.isin} {self.period_end.isoformat()}: filing has no facts; a results filing "
                "reports at least revenue"
            )
        for fact in self.facts:
            if (
                fact.isin != self.isin
                or fact.period_end != self.period_end
                or fact.filing_date != self.filing_date
                or fact.nature != self.nature
                or fact.filing_id != self.filing_id
            ):
                raise ValueError(
                    f"fact {fact.concept}/{fact.segment} does not match the filing it belongs to "
                    f"({self.isin} {self.period_end.isoformat()} {self.nature} {self.filing_id})"
                )
        return self

    def company_facts(self) -> tuple[FundamentalFact, ...]:
        """The facts reported at the company level (no segment dimension)."""
        return tuple(fact for fact in self.facts if fact.segment is None)

    def segment_facts(self) -> tuple[FundamentalFact, ...]:
        """The facts reported per business segment — §5.3 BC1's inputs."""
        return tuple(fact for fact in self.facts if fact.segment is not None)

    def segments(self) -> tuple[str, ...]:
        """Segment names disclosed in this filing, in sorted order; empty if none were."""
        return tuple(sorted({fact.segment for fact in self.segment_facts() if fact.segment}))
