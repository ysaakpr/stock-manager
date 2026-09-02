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
    "CONCEPTS",
    "Filing",
    "FundamentalFact",
    "Nature",
]


class Nature(StrEnum):
    """Whether a filing reports the parent alone or the whole group.

    Spelled exactly as the in-bse-fin taxonomy states it (`Standalone` / `Consolidated`) so the
    value round-trips through XBRL, the store and a log line unchanged.
    """

    STANDALONE = "Standalone"
    CONSOLIDATED = "Consolidated"


#: The monetary/EPS concepts this parser lifts out of a results filing, keyed by a stable snake_case
#: name the rest of the platform uses, mapped from the in-bse-fin element local-name it appears as.
#: A curated whitelist, not "every tag": these are the line items §5 reasons about, and a concept a
#: consumer cannot name is a concept it cannot use. Segment revenue is handled separately — it is
#: one element (`SegmentRevenue`) disaggregated by a context dimension, not a fixed set of names.
CONCEPTS: Final[dict[str, str]] = {
    "RevenueFromOperations": "revenue_from_operations",
    "OtherIncome": "other_income",
    "TotalIncome": "total_income",
    "TotalExpenses": "total_expenses",
    "ProfitBeforeTax": "profit_before_tax",
    "ProfitLossForPeriod": "profit_after_tax",
    "BasicEarningsPerShare": "eps_basic",
    "DilutedEarningsPerShare": "eps_diluted",
}

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
    filing_id: str = Field(
        min_length=1, description="stable id of the filing this came from; keys restatements apart"
    )
    concept: str = Field(min_length=1, description="snake_case concept key, or 'segment_revenue'")
    segment: str | None = Field(
        default=None, description="business-segment name for a segment datum; None at company level"
    )
    value: Value = Field(description="the reported value, exact and finite (CLAUDE.md: Decimal)")
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
