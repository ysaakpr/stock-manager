"""A5 · Fundamentals into break conditions and T1/T2 evidence (M7.4, §5.3/§5.4).

The fundamentals waterfall (M7) ends in two consumers, and this module is the wiring between the
two stores and the two consumers — each store to the one consumer it is allowed to feed:

* **PIT fundamentals → break-condition evaluation.** The mechanical §5.3 BC1-style condition "two
  consecutive quarters of segment revenue decline" is evaluated here, over the *point-in-time*
  store only (`dataplatform.store.pit_fundamentals`). That read is as-of the decision date and
  restatement-collapsed (`read_latest`), so a filing disseminated after the decision date, or a
  restatement the market had not yet seen, cannot change a historical verdict (invariant #7). This
  module imports **nothing** that reads the restated (Screener) store — the break-condition path has
  no symbol that could reach it, which is invariant #8 for this consumer stated as an import fact,
  not a runtime flag. Acceptance 3 ("break-condition evaluation cannot read the restated store").

* **Restated fundamentals → T1/T2 monitoring evidence, provenance visible.** Monitoring (T1/T2) may
  read restated data (§5.4), but every restated datum that reaches an evidence bundle is labelled
  with the store it came from and its dates, so the model — and the §5.7 evidence pack after the
  fact — can tell a restated number apart from a point-in-time one. `fundamental_evidence` builds
  those labelled `EvidenceItem`s for both stores: a PIT fact carries its `(period_end, filing_date)`
  and the PIT store name; a restated datum carries its period and the `RESTATED` store name (from
  the `ProvenancedFundamental` the quarantine's monitoring catalog hands back). Acceptance 2
  ("evidence bundles label every fundamental datum with its store and dates").

The evaluator is *mechanical*: it returns a `Verdict` from arithmetic over filed segment-revenue
figures, not a model judgment. That is deliberate — §5.4 puts BC1 at "T1 on results filing", but the
decision of *whether two consecutive quarters declined* is a fact a rule can settle, and a rule that
may end a position must be auditable and stable (the same principle as `thesis.assert_falsifiable`).
The verdict this produces is what a T1 review is handed as the evaluated break condition, not a
substitute for the review's reading of *why*.

Money is `Decimal` throughout (a revenue compared as a float is a bug, CLAUDE.md); identity is ISIN
(invariant #2); the as-of date is supplied by the caller (the decision date), never a wall clock;
and nothing here reads the network.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Final

from analyst.journal import BreakConditionEvaluation, EvidenceItem, EvidenceKind, Verdict
from dataplatform.clock import IST
from dataplatform.ingest.xbrl import SEGMENT_CONCEPT, FundamentalFact, Nature
from dataplatform.logging import get_logger
from dataplatform.query.quarantine import ProvenancedFundamental
from dataplatform.store.pit_fundamentals import PIT_FUNDAMENTALS_DATASET, read_latest

__all__ = [
    "CONSECUTIVE_QUARTERS_REQUIRED",
    "PIT_STORE",
    "QUARTER_GAP_DAYS",
    "QuarterObservation",
    "SegmentDeclineResult",
    "evaluate_segment_revenue_decline",
    "fundamental_evidence",
    "pit_evidence_item",
    "restated_evidence_item",
    "segments_disclosed",
]

_LOG = get_logger(__name__)

#: The store label recorded on a PIT fundamental evidence item, so a restated figure and a
#: point-in-time figure are told apart by name wherever they surface (acceptance 2). The restated
#: side's label is `RESTATED_ROOT_NAME`, carried on the `ProvenancedFundamental` itself.
PIT_STORE: Final[str] = PIT_FUNDAMENTALS_DATASET

#: How many consecutive quarter-over-quarter declines meet the §5.3 BC1 condition. Two: three
#: consecutive quarters with each lower than the one before. Named (not a literal 2 in the code) so
#: the condition's arithmetic reads as the condition's words.
CONSECUTIVE_QUARTERS_REQUIRED: Final[int] = 2

#: The inclusive day-gap window that makes two period-ends *consecutive quarters*. Indian quarter
#: ends (Mar/Jun/Sep/Dec) are 90-92 days apart; the band is widened a little at each end to absorb
#: leap years and month-length variation while still excluding a half-year (≈182 d) or a skipped
#: quarter. Consecutiveness matters: "two consecutive quarters of decline" is not met by two
#: declines with a missing quarter between them, so a gap outside this band ends the trailing run.
QUARTER_GAP_DAYS: Final[tuple[int, int]] = (80, 100)

#: A quarterly period spans about a quarter; a period whose start-to-end duration is longer than
#: this is an annual (or half-year) disclosure and is excluded from a *quarterly* trend. Applied
#: only when `period_start` is known — a filing that omitted it is assumed quarterly, which is what
#: a results-filing segment disclosure is.
_QUARTER_MAX_DAYS: Final[int] = 100


@dataclass(frozen=True, slots=True)
class QuarterObservation:
    """One quarter's segment-revenue figure, as it was knowable on the decision date.

    Carries the value plus the tags that make it point-in-time and auditable: the `period_end` the
    number is about, and the `filing_date`/`filing_id` of the filing it was read from (the latest
    filing for that period knowable as of the decision date — a restatement the market had seen).
    """

    period_end: date
    period_start: date | None
    value: Decimal
    filing_date: date
    filing_id: str


@dataclass(frozen=True, slots=True)
class SegmentDeclineResult:
    """The mechanical verdict on "two consecutive quarters of segment revenue decline".

    What it does: pair the `Verdict` with the exact trailing run of consecutive quarters it was
    computed from and a one-line `observed` string naming the figures — so the journal records the
    "because" behind the verdict (§5.7's `break_conditions_evaluated`) and a reader recomputes it.
    What it assumes: `quarters` is the trailing run of *consecutive* quarters ending at the latest
    one knowable on `on_date`, ascending by `period_end`.
    What it never does: hold a float, or claim BROKEN without `CONSECUTIVE_QUARTERS_REQUIRED`
    consecutive declines in that run.

    `verdict` is BROKEN when the run's last `CONSECUTIVE_QUARTERS_REQUIRED` quarter-over-quarter
    steps are all strictly declining, WEAKENED when only the most recent step declines (one quarter
    of decline — moving against the thesis but not the condition met), else INTACT (including when
    there is not enough consecutive history to judge — an unmet condition, not an error).
    """

    isin: str
    segment: str
    nature: Nature
    on_date: date
    verdict: Verdict
    trailing_declines: int
    observed: str
    quarters: tuple[QuarterObservation, ...]

    def as_evaluation(self, condition_id: str) -> BreakConditionEvaluation:
        """This result as the per-condition journal record for break-condition id `condition_id`."""
        return BreakConditionEvaluation(
            id=condition_id, verdict=self.verdict, observed=self.observed
        )


def evaluate_segment_revenue_decline(
    isin: str,
    segment: str,
    on_date: date,
    *,
    nature: Nature = Nature.CONSOLIDATED,
    data_root: Path | None = None,
    quarters_required: int = CONSECUTIVE_QUARTERS_REQUIRED,
) -> SegmentDeclineResult:
    """Evaluate §5.3 BC1 for one segment: two consecutive quarters of segment revenue decline.

    What it does: reads the point-in-time fundamentals store as of `on_date` (`read_latest` —
    restatement-collapsed, nothing filed after `on_date`), takes the segment-revenue figures for
    `isin`/`segment`/`nature`, builds the trailing run of *consecutive* quarters ending at the
    latest one, and returns BROKEN when its last `quarters_required` quarter-over-quarter steps are
    all strictly declining.
    What it assumes: `on_date` is the decision date (Asia/Kolkata); segment revenue is filed
    quarterly (an annual/half-year disclosure, identifiable by its period length, is excluded).
    What it never does: read the restated store — the figures come only from `read_latest` over the
    PIT store, which has no path to the RESTATED root (invariant #8, acceptance 3); return BROKEN on
    a decline across a gap where a quarter is missing; or compare values as floats.

    Raises `ValueError` for a non-positive `quarters_required` — a condition of "zero consecutive
    declines" is not a condition.
    """
    if quarters_required < 1:
        raise ValueError(f"quarters_required must be at least 1, got {quarters_required}")

    quarterly = _segment_quarters(isin, segment, on_date, nature=nature, data_root=data_root)
    run = _trailing_consecutive_run(quarterly)
    trailing_declines = _trailing_declines(run)

    if trailing_declines >= quarters_required:
        verdict = Verdict.BROKEN
    elif trailing_declines >= 1:
        verdict = Verdict.WEAKENED
    else:
        verdict = Verdict.INTACT

    observed = _observed(segment, run, trailing_declines, quarters_required, verdict)
    _LOG.info(
        "fundamentals.segment_decline",
        isin=isin,
        segment=segment,
        nature=nature.value,
        on_date=on_date.isoformat(),
        verdict=verdict.value,
        trailing_declines=trailing_declines,
        quarters_in_run=len(run),
        quarters_seen=len(quarterly),
    )
    return SegmentDeclineResult(
        isin=isin,
        segment=segment,
        nature=nature,
        on_date=on_date,
        verdict=verdict,
        trailing_declines=trailing_declines,
        observed=observed,
        quarters=run,
    )


def segments_disclosed(
    isin: str,
    on_date: date,
    *,
    nature: Nature = Nature.CONSOLIDATED,
    data_root: Path | None = None,
) -> tuple[str, ...]:
    """The business segments `isin` disclosed segment revenue for, knowable on `on_date`, sorted.

    Lets a T1 review evaluate BC1 across every segment the company reports without the caller having
    to name them; reads the PIT store only (never the restated one).
    """
    facts = read_latest(on_date, data_root=data_root)
    segments = {
        fact.segment
        for fact in facts
        if fact.isin == isin
        and fact.nature is nature
        and fact.concept == SEGMENT_CONCEPT
        and fact.segment is not None
    }
    return tuple(sorted(segments))


# ── evidence labelling (acceptance 2) ─────────────────────────────────────────────────────────────


def pit_evidence_item(fact: FundamentalFact) -> EvidenceItem:
    """A PIT fundamental fact as a labelled evidence item — its store and both its dates.

    Records the PIT store name and the fact's `(period_end, filing_date)` on the item, so a
    monitoring bundle that also carries restated figures can tell the point-in-time number apart
    from a restated one (acceptance 2). `knowable_at` is the filing date at IST start-of-day — the
    conservative first instant the number could be known — which keeps the item PIT-auditable.
    """
    label = fact.concept if fact.segment is None else f"{fact.concept}:{fact.segment}"
    detail = {
        "store": PIT_STORE,
        "period_end": fact.period_end.isoformat(),
        "filing_date": fact.filing_date.isoformat(),
        "nature": fact.nature.value,
        "concept": fact.concept,
        "filing_id": fact.filing_id,
        "source": fact.source,
    }
    if fact.period_start is not None:
        detail["period_start"] = fact.period_start.isoformat()
    if fact.segment is not None:
        detail["segment"] = fact.segment
    return EvidenceItem(
        kind=EvidenceKind.FUNDAMENTAL,
        source=PIT_STORE,
        label=label,
        isin=fact.isin,
        as_of=fact.period_end,
        knowable_at=datetime(
            fact.filing_date.year, fact.filing_date.month, fact.filing_date.day, tzinfo=IST
        ),
        value=fact.value,
        detail=detail,
    )


def restated_evidence_item(datum: ProvenancedFundamental) -> EvidenceItem:
    """A restated (Screener) fundamental as a labelled evidence item — its store and period.

    The restated store carries no `knowable_date` (that is exactly why it is quarantined from PIT),
    so this item records the store, the period the figure is about, and the full restated provenance
    (source tag and L0 lineage) in `detail`, but no `knowable_at`. The store name on `source` and in
    `detail["store"]` is what marks the figure as restated wherever it surfaces (acceptance 2).
    """
    return EvidenceItem(
        kind=EvidenceKind.FUNDAMENTAL,
        source=datum.store,
        label=f"{datum.statement}.{datum.metric}.{datum.period}",
        isin=datum.isin,
        value=datum.value,
        detail=dict(datum.as_evidence_fields()),
    )


def fundamental_evidence(
    *,
    pit: tuple[FundamentalFact, ...] = (),
    restated: tuple[ProvenancedFundamental, ...] = (),
) -> tuple[EvidenceItem, ...]:
    """Labelled evidence items for a mix of PIT and restated fundamentals — store and dates on each.

    The one place a T1/T2 bundle turns fundamentals into evidence: every item names the store it
    came from (`PIT_STORE` or the `RESTATED` root) and the dates that place it in time, so the model
    sees the restated figures *as restated* and the §5.7 evidence pack can slice by store after the
    fact. PIT items come first, then restated, each block in the order supplied.
    """
    return tuple(pit_evidence_item(fact) for fact in pit) + tuple(
        restated_evidence_item(datum) for datum in restated
    )


# ── internals ─────────────────────────────────────────────────────────────────────────────────────


def _segment_quarters(
    isin: str,
    segment: str,
    on_date: date,
    *,
    nature: Nature,
    data_root: Path | None,
) -> tuple[QuarterObservation, ...]:
    """Segment-revenue quarters for one segment as of `on_date`, ascending by period end.

    Reads the PIT store only (`read_latest`), keeps the segment-revenue facts for this
    `isin`/`segment`/`nature`, drops any period whose length identifies it as annual/half-year, and
    returns one observation per period end (the latest-knowable filing for it, which `read_latest`
    has already collapsed to).
    """
    observations = [
        QuarterObservation(
            period_end=fact.period_end,
            period_start=fact.period_start,
            value=fact.value,
            filing_date=fact.filing_date,
            filing_id=fact.filing_id,
        )
        for fact in read_latest(on_date, data_root=data_root)
        if fact.isin == isin
        and fact.nature is nature
        and fact.concept == SEGMENT_CONCEPT
        and fact.segment == segment
        and _is_quarterly(fact)
    ]
    observations.sort(key=lambda obs: obs.period_end)
    return tuple(observations)


def _is_quarterly(fact: FundamentalFact) -> bool:
    """Whether a fact's period is a quarter — an annual/half-year disclosure is not part of a QoQ
    trend. Decided by period length when `period_start` is known; assumed quarterly when it is not,
    since a results-filing segment disclosure is quarterly."""
    if fact.period_start is None:
        return True
    return (fact.period_end - fact.period_start).days <= _QUARTER_MAX_DAYS


def _trailing_consecutive_run(
    quarters: tuple[QuarterObservation, ...],
) -> tuple[QuarterObservation, ...]:
    """The longest run of consecutive quarters ending at the latest observation.

    A "consecutive quarter" is one whose period end is `QUARTER_GAP_DAYS` after the previous one; a
    gap outside that band (a missing quarter, or an annual jump) ends the run. Returning the tail
    run — not the whole history — is what makes "two *consecutive* quarters of decline" mean what it
    says: two declines separated by an unfiled quarter do not meet the condition.
    """
    if not quarters:
        return ()
    run = [quarters[-1]]
    for earlier in reversed(quarters[:-1]):
        if _consecutive(earlier.period_end, run[0].period_end):
            run.insert(0, earlier)
        else:
            break
    return tuple(run)


def _consecutive(earlier: date, later: date) -> bool:
    """Whether `later` is the quarter end right after `earlier` (gap within `QUARTER_GAP_DAYS`)."""
    low, high = QUARTER_GAP_DAYS
    return low <= (later - earlier).days <= high


def _trailing_declines(run: tuple[QuarterObservation, ...]) -> int:
    """How many quarter-over-quarter steps at the *end* of the run are strictly declining.

    Counts backfrom the latest quarter and stops at the first step that is flat or up, so a decline
    two quarters ago that has since reversed does not count toward "consecutive quarters decline".
    """
    declines = 0
    for index in range(len(run) - 1, 0, -1):
        if run[index].value < run[index - 1].value:
            declines += 1
        else:
            break
    return declines


def _observed(
    segment: str,
    run: tuple[QuarterObservation, ...],
    trailing_declines: int,
    quarters_required: int,
    verdict: Verdict,
) -> str:
    """A one-line, recomputable summary of the verdict — the figures it rests on, named."""
    if not run:
        return (
            f"no quarterly segment revenue for {segment!r} knowable as of the decision date; "
            "condition cannot be met (INTACT)"
        )
    series = ", ".join(f"{obs.period_end.isoformat()}={obs.value}" for obs in run)
    if verdict is Verdict.BROKEN:
        head = (
            f"{trailing_declines} consecutive quarters of decline in {segment!r} segment revenue "
            f"(condition: {quarters_required}); BROKEN"
        )
    elif verdict is Verdict.WEAKENED:
        head = (
            f"1 quarter of decline in {segment!r} segment revenue, short of the "
            f"{quarters_required} required; WEAKENED"
        )
    else:
        head = f"no trailing decline in {segment!r} segment revenue; INTACT"
    return f"{head} — trailing quarters: [{series}]"
