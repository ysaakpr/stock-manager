"""The owner-ruled relabel of a quarter its own document labels a half-year (class A, 2026-11-02).

Some Q4 results documents state, in their quarter column, a period of October→March and a header
of `TypeOfReportingPeriod = Half Yearly` — while the index entry asks for January→March, and the
figures under that label are the January-March quarter's. Nothing inside the document tells such a
column apart from a genuine second half-year (ASTEC's is one): only arithmetic against the same
company's other filings does. The triage of the 287 refused class-A filings found the column's
revenue *and* profit equal to the document's own year-to-date column minus the quarters the
company had already published in about 130 of them, and the owner ruled (2026-11-02) that those —
and only those — are stored as the quarter, under the document's own stated values.

This module is that ruling's check, kept pure like `half_year`: the parser hands over a
`RelabelCandidate` (the document's half-year-labelled column already built as a quarter, plus the
same document's cumulative column), the runner supplies what was already published, and `check`
says whether the relabel is allowed and, if not, which check failed.

The check, `RULE`, passes only when **all** of these hold:

* every quarter of the financial year before the asked one (three for an April-March year; two
  for a July-June year, whose document's cumulative column is the nine months to March) is on file
  for the same ISIN and nature, stated (not derived), from a filing disseminated **on or before**
  this filing's date — a quarter filed later is look-ahead and does not count (invariant #7);
* for revenue from operations **and** profit after tax, the column's stated value equals the
  document's cumulative value minus those quarters, to within `tolerance` — rounding only;
* the quarter a genuine half-year would also contain (the one just before the asked quarter)
  carries revenue larger than that tolerance — otherwise revenue agrees with "half-year" as well
  as with "quarter" and corroborates nothing, and both witnesses must corroborate (a company with
  no revenue at all is refused, though its profit alone would tell the two apart).

What it never does: compute a number that is stored. `cumulative - Σ prior quarters` gates the
relabel and is recorded beside it as the check's result; the facts stored are the document's own.
It never reads a filing dated after this one, never prefers a looser match, and never passes on
one concept alone.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Final

from dataplatform.ingest.models import ParseError
from dataplatform.ingest.xbrl.models import Filing, FundamentalFact

__all__ = [
    "MAX_ROUNDING_STEP",
    "RULE",
    "WITNESSES",
    "LabelledHalfYearError",
    "RelabelCandidate",
    "RelabelCheck",
    "RelabelRecord",
    "check",
    "prior_quarters",
    "rounding_step",
    "tolerance",
]

#: The provenance rule name recorded beside every relabelled filing (owner, 2026-11-02).
RULE: Final = "Q4_RELABEL_FY_MINUS_9M_CHECK"

#: Both must agree; one alone never passes (owner, 2026-11-02).
WITNESSES: Final = ("revenue_from_operations", "profit_after_tax")

#: The monetary flows a filing's rounding step is read from. EPS and ratios are stated to paise or
#: a fraction and say nothing about the unit the statement's rupee amounts were rounded to.
_ROUNDING_FLOWS: Final = frozenset(
    {
        "revenue_from_operations",
        "other_income",
        "total_income",
        "total_expenses",
        "profit_before_tax",
        "profit_after_tax",
    }
)

#: ₹1 crore — the coarsest unit an Indian results statement is presented in. A value that happens
#: to end in more zeros than that does not make its filing's rounding any coarser.
MAX_ROUNDING_STEP: Final = Decimal(10) ** 7


def rounding_step(values: Iterable[Decimal]) -> Decimal:
    """The unit a filing's rupee amounts were rounded to, read off the amounts themselves.

    Every amount in one statement is presented in one unit (₹, thousand, lakh, million, crore), so
    the unit is at most the largest power of ten dividing each of them — and the smallest of those
    over the filing's flows is the tightest bound the filing itself proves. Zeros say nothing and
    are skipped. Capped at `MAX_ROUNDING_STEP`; a filing with no non-zero flow has step 1 (₹1).
    """
    step: Decimal | None = None
    for value in values:
        if value == 0:
            continue
        exponent = value.normalize().as_tuple().exponent
        assert isinstance(exponent, int)  # a finite Decimal, as FundamentalFact guarantees
        own = Decimal(10) ** min(exponent, 7)
        step = own if step is None else min(step, own)
    return Decimal(1) if step is None else step


def tolerance(document_step: Decimal, prior_steps: Iterable[Decimal]) -> Decimal:
    """How far `stated` may sit from `cumulative - Σ priors` and still be the same figure: rounding.

    One rounding step of its own filing per figure in the identity — the half-year-labelled column
    and the cumulative column (both this document's), and each prior quarter (its own filing's).
    A figure presented to a unit is off by at most half that unit, and a filer that derived it from
    other rounded figures (a quarter as year minus nine months) can be off by another half: one
    unit per figure is that, and nothing more. Measured on the 287 class-A filings, the agreeing
    ones sit within 8 units and the disagreeing ones at 54 units and beyond.
    """
    return document_step * 2 + sum(prior_steps, Decimal(0))


def _add_months(day: date, months: int) -> date:
    total = day.month - 1 + months
    return day.replace(year=day.year + total // 12, month=total % 12 + 1)


def prior_quarters(cumulative_start: date, quarter_start: date) -> tuple[tuple[date, date], ...]:
    """The three-month quarters from `cumulative_start` up to the day before `quarter_start`.

    Empty when `cumulative_start` is not on the asked quarter's three-month grid, or not before it.
    """
    quarters: list[tuple[date, date]] = []
    start = cumulative_start
    while start < quarter_start:
        end = _add_months(start, 3)
        quarters.append((start, end - timedelta(days=1)))
        start = end
    if start != quarter_start:
        return ()
    return tuple(quarters)


@dataclass(frozen=True, slots=True)
class RelabelCandidate:
    """A quarterly entry's document whose only column of that quarter's end is a labelled half-year.

    `filing` is the half-year-labelled column already built as the asked quarter — the document's
    own stated facts, tagged with the entry's three-month period — and is stored only if `check`
    passes. `stated` and `cumulative` are the witness concepts of that column and of the same
    document's cumulative (year-to-date) column; `document_step` is the document's rounding unit.
    """

    filing: Filing
    stated_start: date
    stated_end: date
    stated_column: str
    cumulative_start: date
    cumulative_end: date
    cumulative_column: str
    stated: Mapping[str, Decimal]
    cumulative: Mapping[str, Decimal]
    document_step: Decimal
    reporting_period_label: str


class LabelledHalfYearError(ParseError):
    """The parser's refusal of a quarterly entry, carrying the class-A shape it found instead.

    Raised in place of the plain "no results column covers …" refusal when the document has exactly
    that shape (`RelabelCandidate`); a runner that cannot or will not run `check` treats it as the
    refusal it still is — it *is* a `ParseError`.
    """

    def __init__(self, message: str, *, filename: str, candidate: RelabelCandidate) -> None:
        super().__init__(message, filename=filename)
        self.reason = message
        self.candidate = candidate


@dataclass(frozen=True, slots=True)
class RelabelCheck:
    """`check`'s verdict: whether the relabel is allowed, why, and every number it compared."""

    passed: bool
    reason: str
    result: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RelabelRecord:
    """The provenance row a relabelled filing is stored with (`fundamentals_period_relabel`)."""

    source: str
    unit: str
    logical_date: date
    filing_id: str
    isin: str
    nature: str
    stated_start: date
    stated_end: date
    stored_start: date
    stored_end: date
    rule: str
    check_result: Mapping[str, Any]
    l0_key: str | None


def _versions(
    published: Iterable[FundamentalFact],
    *,
    candidate: RelabelCandidate,
    concept: str,
    quarter: tuple[date, date],
) -> list[FundamentalFact]:
    filing = candidate.filing
    return sorted(
        (
            fact
            for fact in published
            if fact.isin == filing.isin
            and fact.nature is filing.nature
            and fact.concept == concept
            and fact.segment is None
            and not fact.derived
            and fact.period_start == quarter[0]
            and fact.period_end == quarter[1]
            and fact.filing_id != filing.filing_id
        ),
        key=lambda fact: (fact.filing_date, fact.filing_id),
    )


def _step_of(published: list[FundamentalFact], fact: FundamentalFact) -> Decimal:
    """The rounding step of the filing `fact` came from, over that filing's flows for its period."""
    return rounding_step(
        other.value
        for other in published
        if other.filing_id == fact.filing_id
        and other.isin == fact.isin
        and other.nature is fact.nature
        and other.period_start == fact.period_start
        and other.period_end == fact.period_end
        and other.segment is None
        and not other.derived
        and other.concept in _ROUNDING_FLOWS
    )


def _refused(reason: str, result: dict[str, Any]) -> RelabelCheck:
    return RelabelCheck(passed=False, reason=f"{RULE} refused: {reason}", result=result)


def check(candidate: RelabelCandidate, published: Iterable[FundamentalFact]) -> RelabelCheck:
    """Whether the candidate's labelled half-year is the asked quarter, by `RULE` (module doc).

    `published` is any collection of stored facts; only the stated, company-level witness concepts
    of the same ISIN and nature, for exactly the prior quarters, are used — and of those only the
    ones disseminated on or before this filing's date, the latest such version of each quarter
    (two different values on that latest day is undecidable, and refused). Deterministic: the same
    inputs give the same verdict and the same `result`.
    """
    filing = candidate.filing
    facts = list(published)
    quarter_start = filing.period_start
    assert quarter_start is not None  # the parser builds a candidate only for a dated quarter
    quarters = prior_quarters(candidate.cumulative_start, quarter_start)
    result: dict[str, Any] = {
        "rule": RULE,
        "stated_column": candidate.stated_column,
        "stated_period": (
            f"{candidate.stated_start.isoformat()}..{candidate.stated_end.isoformat()}"
        ),
        "stored_period": f"{quarter_start.isoformat()}..{filing.period_end.isoformat()}",
        "reporting_period_label": candidate.reporting_period_label,
        "cumulative_column": candidate.cumulative_column,
        "cumulative_period": (
            f"{candidate.cumulative_start.isoformat()}..{candidate.cumulative_end.isoformat()}"
        ),
        "filing_date": filing.filing_date.isoformat(),
        "document_rounding_step": str(candidate.document_step),
        "concepts": {},
    }
    if len(quarters) not in (2, 3):
        return _refused(
            f"the cumulative column {result['cumulative_period']} does not end the financial year "
            f"in whole quarters before {quarter_start.isoformat()}",
            result,
        )

    passed: dict[str, bool] = {}
    preceding_revenue: Decimal | None = None
    shared_tolerance: Decimal | None = None
    failures: list[str] = []
    for concept in WITNESSES:
        stated = candidate.stated.get(concept)
        cumulative = candidate.cumulative.get(concept)
        if stated is None or cumulative is None:
            missing = "half-year-labelled" if stated is None else "cumulative"
            return _refused(f"the document's {missing} column states no {concept}", result)
        used: list[dict[str, str]] = []
        steps: list[Decimal] = []
        total = Decimal(0)
        for index, quarter in enumerate(quarters, start=1):
            versions = _versions(facts, candidate=candidate, concept=concept, quarter=quarter)
            known = [fact for fact in versions if fact.filing_date <= filing.filing_date]
            span = f"{quarter[0].isoformat()}..{quarter[1].isoformat()}"
            if not known:
                later = sorted({fact.filing_date.isoformat() for fact in versions})
                why = (
                    f"filed only after this filing ({', '.join(later)}): look-ahead, not used"
                    if later
                    else "not on file"
                )
                return _refused(
                    f"prior quarter Q{index} {span} {concept} is {why}; not checkable", result
                )
            latest_day = known[-1].filing_date
            latest = [fact for fact in known if fact.filing_date == latest_day]
            if len({fact.value for fact in latest}) > 1:
                return _refused(
                    f"prior quarter Q{index} {span} {concept} has {len(latest)} versions filed "
                    f"{latest_day.isoformat()} that disagree; not checkable",
                    result,
                )
            chosen = latest[-1]
            step = _step_of(facts, chosen)
            steps.append(step)
            total += chosen.value
            used.append(
                {
                    "quarter": span,
                    "filing_id": chosen.filing_id,
                    "filing_date": chosen.filing_date.isoformat(),
                    "value": str(chosen.value),
                    "rounding_step": str(step),
                }
            )
            if concept == "revenue_from_operations" and index == len(quarters):
                preceding_revenue = chosen.value
        allowed = tolerance(candidate.document_step, steps)
        implied = cumulative - total
        delta = stated - implied
        ok = abs(delta) <= allowed
        passed[concept] = ok
        if concept == "revenue_from_operations":
            shared_tolerance = allowed
        result["concepts"][concept] = {
            "stated": str(stated),
            "cumulative": str(cumulative),
            "prior_quarters": used,
            "prior_sum": str(total),
            "cumulative_minus_priors": str(implied),
            "difference": str(delta),
            "tolerance": str(allowed),
            "agrees": ok,
        }
        if not ok:
            failures.append(
                f"{concept}: stated {stated} vs cumulative - prior quarters {implied} differ by "
                f"{delta}, beyond the rounding tolerance {allowed}"
            )
    if failures:
        return _refused(
            "; ".join(failures) + " — the column is not the asked quarter (a genuine half-year, "
            "or restated inputs)",
            result,
        )
    assert preceding_revenue is not None and shared_tolerance is not None
    if abs(preceding_revenue) <= shared_tolerance:
        return _refused(
            f"the quarter before the asked one has revenue {preceding_revenue}, within the "
            f"tolerance {shared_tolerance}, so revenue cannot tell a half-year from a quarter "
            "and both witnesses must",
            result,
        )
    result["passed"] = all(passed.values())
    return RelabelCheck(
        passed=True,
        reason=(
            f"{RULE} passed: revenue and profit after tax of {candidate.stated_column} equal "
            f"{candidate.cumulative_column} minus {len(quarters)} prior quarters filed by "
            f"{filing.filing_date.isoformat()}, within rounding"
        ),
        result=result,
    )
