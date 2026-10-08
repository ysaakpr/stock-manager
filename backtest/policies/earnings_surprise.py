"""M16.3 — the earnings-surprise (PEAD) leg of the swing composite: arm A5, weight 0 by default.

Post-earnings-announcement drift is the oldest anomaly in the literature: a name whose latest
quarter beat its own history keeps drifting up for weeks after the filing, and a miss drifts down.
This module scores that surprise and nothing else. The definition is fixed by the task, not fitted:

* **Standardised unexpected earnings.** ``SUE = (EPS_t - EPS_t-4) / sd(EPS_k - EPS_k-4, k = t-8 ..
  t-1)`` — the latest quarter's year-over-year change in EPS over the sample standard deviation of
  the eight year-over-year changes before it. A seasonal random walk is the expectation, so the
  same quarter a year earlier is the forecast and the numerator is the surprise; the denominator
  puts a steady compounder's 10 % beat and a cyclical's 10 % beat on one scale. **All thirteen
  quarters** (the latest, the eight differences before it, and the four quarters each reaches back)
  must be present and consecutive, or the name gets no signal. The latest difference is *not* in
  its own denominator: that would shrink every large surprise toward the mean.
* **Active for 63 sessions from the filing.** The leg carries the SUE, unchanged, on the first
  session on or after the latest quarter's *first* filing and the 62 after it — a quarter of a
  year, the next filing's arrival — and ``0`` after that. Constant over the window, then 0: a step,
  not a decay curve, because a decay rate would be a parameter and this definition has none. The
  window opens on the earliest knowable filing of that quarter, never on a re-filing of it: the
  lake carries ~870 re-filings of a then-latest standalone quarter (median 37 days after the
  original, a handful 90+ days later and mostly at an unchanged value), and dating the window off
  the newest would switch a zeroed leg back on for news the market had for a quarter. The *value*
  is the latest knowable filing's, as every restatement elsewhere is (invariant #8).
  **A regular filer's leg is almost never zero**: 63 sessions is about one quarter, so the next
  quarter's results usually land before the window closes and open a new one. Zero is mostly the
  late filer's value, and the leg is in practice "the latest SUE", stale by up to a quarter.
* **Standalone quarterly figures, always.** The PIT store carries both natures, but a consolidated
  series begins later for most filers and switches on part-way through a company's history when it
  acquires its first subsidiary; a year-over-year difference across that switch is a change of
  perimeter, not a surprise. Standalone is filed by every listed company every quarter, so it is
  the one series that is comparable with itself across thirteen quarters. Never a mix.

**EPS is restated to the latest quarter's share count**, so a split, a face-value change and a bonus
issue all leave the series comparable. Each quarter's ``eps_basic`` is multiplied by that filing's
``shares_outstanding`` over the latest quarter's — ``EPS x shares`` is the earnings, so this is
earnings per *current* share. Face value alone would not do: Reliance's 2024 1:1 bonus halved its
EPS at an unchanged face value of 10, and a face-value rescale would read that as a 50 % collapse in
earnings. The share count is the parser's own (``paid_up / face_value``), published only when the
filing's EPS corroborates it, so the count and the EPS are on one basis in every quarter used. A
quarter whose filing carries no share count is a hole in the run, and a name with a hole gets no
signal — it is excluded, not guessed. A fresh issue (a QIP, a rights issue) moves the count too, so
its dilution is treated as a change of basis rather than as a miss: the leg scores the business,
not the capital raise. A filing stated at the wrong scale (``drop_misscaled_filings``) is dropped
whole first, so it cannot become a fake surprise.

**Coverage is thin, and a reader of any A5 result must know where.** Measured on the lake on
2026-10-08: the store's first reading of any ISIN is struck on 2021-04-22 (its filings open 2018-05,
and thirteen quarters must accumulate) and of a NIFTY 500 PIT member on 2021-04-27. Of the 500
members, 11 have one by 2021-05-03, 82 (16.4 %) by 2021-06-01 and 113 (22.6 %) by 2021-07-01;
about a quarter to 30 % from then to 2025; 40.2 % on 2026-01-01 and a month-start peak of 43.0 %
on 2026-03-02. **It never reaches 50 %.**
On 2025-07-01 the 345 members without one split as 172 with fewer than thirteen standalone quarters
in the store, 119 with a missing quarter in the run (mostly in 2022), 31 with a quarter that has no
corroborated share count, and 24 with no standalone filing at all. Those are gaps in the store, not
in the definition: the leg ranks the names it can score and gives the rest the leg's mean.

Point-in-time (invariant #7): :func:`standardised_unexpected_earnings` refuses — ``PitError``, not a
filter — any fact filed after the date it is asked about, and collapses a restatement to the latest
filing on or before it (invariant #8). :class:`EarningsSurprisePanel` hands it only the prefix of a
filing-date-sorted fact list, so the refusal is the backstop, not the mechanism.

What it never does: read a clock, a store or the network, mix natures, or fill a missing quarter.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise
from typing import Final

from backtest.policies.swing_composite import SwingCompositeParameters
from dataplatform.ingest.xbrl import Nature
from dataplatform.query.fundamentals_metrics import FactRow, drop_misscaled_filings
from dataplatform.query.pit import PitError

__all__ = [
    "ACTIVE_SESSIONS",
    "CONCEPTS",
    "HISTORY_DIFFERENCES",
    "M10_7_EARNINGS_SURPRISE",
    "EarningsSurprise",
    "EarningsSurprisePanel",
    "NoSignal",
    "standardised_unexpected_earnings",
    "surprise_readings",
]

# The definition's constants, fixed by the task before any evaluation. Not parameters: changing one
# is a new hypothesis, not a tuning of this one.
#: How many sessions the leg stays on after the filing (the filing session counts as the first).
ACTIVE_SESSIONS: Final = 63
#: The year-over-year differences the denominator's stdev is struck over.
HISTORY_DIFFERENCES: Final = 8
#: Quarters in a year: the seasonal lag of every difference.
_YEAR: Final = 4
#: The run every reading needs: the latest quarter, eight earlier ones, and four behind the oldest.
_QUARTERS_NEEDED: Final = 1 + HISTORY_DIFFERENCES + _YEAR

#: What this module reads from the PIT store. ``paid_up_equity_capital`` only feeds the mis-scale
#: detector; it never enters a figure.
CONCEPTS: Final[frozenset[str]] = frozenset(
    {"eps_basic", "shares_outstanding", "paid_up_equity_capital"}
)
_NATURE: Final = Nature.STANDALONE

# The same period conventions as dataplatform.query.fundamentals_metrics: a quarter is a period of
# 80..100 days, and two quarter-ends 85..97 days apart are consecutive (month-end drift allowed).
_QUARTER_DAYS: Final = (80, 100)
_CONSECUTIVE_DAYS: Final = (85, 97)
_ZERO: Final = Decimal("0")
_QUANTUM: Final = Decimal("0.00000001")  # 8 dp, as every other swing feature is quantised

#: The M10.7 composite with the earnings-surprise leg on at the existing legs' weight (1): an equal
#: fourth leg. Everything else is the M10.7 default, so a run of it reads directly against M10.7.
M10_7_EARNINGS_SURPRISE: Final = SwingCompositeParameters(weight_earnings_surprise=Decimal(1))


@dataclass(frozen=True, slots=True)
class EarningsSurprise:
    """One name's latest standardised unexpected earnings, and when it became knowable.

    ``filing_date`` is the filing that carried the latest quarter's EPS — the date the 63-session
    window opens on. ``sue`` is quantised to 8 dp. Never built from a fact filed after the date it
    was struck as of.
    """

    isin: str
    sue: Decimal
    latest_period_end: date
    filing_date: date


class NoSignal(StrEnum):
    """Why a name has no reading — counted and logged, never papered over with a value."""

    NO_FILINGS = "NO_FILINGS"
    SHORT_HISTORY = "SHORT_HISTORY"
    QUARTER_GAP = "QUARTER_GAP"
    NO_SHARE_COUNT = "NO_SHARE_COUNT"
    FLAT_HISTORY = "FLAT_HISTORY"


def _is_quarter(row: FactRow) -> bool:
    if row.period_start is None:
        return False
    return _QUARTER_DAYS[0] <= (row.period_end - row.period_start).days <= _QUARTER_DAYS[1]


def _consecutive(earlier: date, later: date) -> bool:
    return _CONSECUTIVE_DAYS[0] <= (later - earlier).days <= _CONSECUTIVE_DAYS[1]


def _sample_stdev(values: Sequence[Decimal]) -> Decimal:
    mean = sum(values, _ZERO) / Decimal(len(values))
    variance = sum(((v - mean) ** 2 for v in values), _ZERO) / Decimal(len(values) - 1)
    return variance.sqrt()


def _surprise_for(isin: str, rows: Sequence[FactRow]) -> EarningsSurprise | NoSignal:
    """The SUE of one ISIN's already PIT-checked standalone rows, or why there is none."""
    rows, _ = drop_misscaled_filings(rows)
    # Restatement collapse: per quarter, the latest knowable filing's EPS wins (invariant #8) —
    # ties on a date broken by filing id, so the choice never rests on read order. ``first`` keeps
    # each quarter's earliest filing: the date the market first had the number.
    eps: dict[tuple[date | None, date], FactRow] = {}
    first: dict[tuple[date | None, date], date] = {}
    shares: dict[tuple[str, date], Decimal] = {}
    for row in rows:
        if not _is_quarter(row):
            continue
        if row.concept == "eps_basic":
            key = (row.period_start, row.period_end)
            current = eps.get(key)
            if current is None or (row.filing_date, row.filing_id) > (
                current.filing_date,
                current.filing_id,
            ):
                eps[key] = row
            first[key] = min(first.get(key, row.filing_date), row.filing_date)
        elif row.concept == "shares_outstanding" and row.value > _ZERO:
            shares[(row.filing_id, row.period_end)] = row.value
    if not eps:
        return NoSignal.NO_FILINGS
    run = sorted(eps.values(), key=lambda r: r.period_end)[-_QUARTERS_NEEDED:]
    if len(run) < _QUARTERS_NEEDED:
        return NoSignal.SHORT_HISTORY
    if not all(_consecutive(a.period_end, b.period_end) for a, b in pairwise(run)):
        return NoSignal.QUARTER_GAP
    counts: list[Decimal] = []
    for row in run:
        count = shares.get((row.filing_id, row.period_end))
        if count is None:
            return NoSignal.NO_SHARE_COUNT  # not comparable across quarters: excluded
        counts.append(count)
    restated = [r.value * count / counts[-1] for r, count in zip(run, counts, strict=True)]
    differences = [restated[k] - restated[k - _YEAR] for k in range(_YEAR, _QUARTERS_NEEDED)]
    surprise, history = differences[-1], differences[:-1]
    spread = _sample_stdev(history)
    if spread == _ZERO:
        return NoSignal.FLAT_HISTORY  # eight identical changes: no scale to standardise against
    return EarningsSurprise(
        isin=isin,
        sue=(surprise / spread).quantize(_QUANTUM),
        latest_period_end=run[-1].period_end,
        filing_date=first[(run[-1].period_start, run[-1].period_end)],
    )


def surprise_readings(
    facts: Iterable[FactRow], *, as_of: date
) -> dict[str, EarningsSurprise | NoSignal]:
    """Every ISIN's SUE as of ``as_of``, or the :class:`NoSignal` reason it has none.

    The PIT refusal and the fact selection of :func:`standardised_unexpected_earnings`, which is
    this with the reasons left out. An ISIN with no standalone fact at all is absent.
    """
    by_isin: dict[str, list[FactRow]] = {}
    for fact in facts:
        if fact.filing_date > as_of:
            raise PitError(
                f"fact for {fact.isin} {fact.concept} filed {fact.filing_date.isoformat()} is not "
                f"knowable as of {as_of.isoformat()}; a later filing leaked into the earnings "
                "surprise (invariant #7)"
            )
        if fact.concept in CONCEPTS and fact.segment is None and fact.nature is _NATURE:
            by_isin.setdefault(fact.isin, []).append(fact)
    return {isin: _surprise_for(isin, by_isin[isin]) for isin in sorted(by_isin)}


def standardised_unexpected_earnings(
    facts: Iterable[FactRow], *, as_of: date
) -> dict[str, EarningsSurprise]:
    """Every ISIN's latest SUE as of ``as_of``, from standalone quarterly facts knowable then.

    What it does: refuses any fact filed after ``as_of`` (``PitError`` — the leak is named, not
    filtered), keeps company-level standalone ``eps_basic`` / ``shares_outstanding`` /
    ``paid_up_equity_capital``, and computes each ISIN's SUE per the module docstring.
    What it assumes: quarterly facts are periods of 80..100 days.
    What it never does: return a name without thirteen consecutive quarters, each with a share
    count, or one whose eight-difference stdev is zero — those are absent from the result.
    """
    return {
        isin: reading
        for isin, reading in surprise_readings(facts, as_of=as_of).items()
        if isinstance(reading, EarningsSurprise)
    }


class EarningsSurprisePanel:
    """The leg's value per (session, ISIN) over a trading calendar — what the swing features read.

    Built once from the whole fact history; :meth:`value` hands
    :func:`standardised_unexpected_earnings` only the facts filed on or before the session, so a
    later filing cannot reach an earlier decision. A reading only changes on a filing date, so it
    is cached per (ISIN, newest filing date knowable) and a decade of fortnightly decisions costs
    one computation per filing, not one per decision.
    """

    def __init__(self, facts: Iterable[FactRow], calendar: Sequence[date]) -> None:
        self._calendar = sorted(calendar)
        self._facts: dict[str, list[FactRow]] = {}
        for fact in facts:
            if fact.concept in CONCEPTS and fact.segment is None and fact.nature is _NATURE:
                self._facts.setdefault(fact.isin, []).append(fact)
        for rows in self._facts.values():
            rows.sort(key=lambda r: r.filing_date)
        self._dates = {isin: [r.filing_date for r in rows] for isin, rows in self._facts.items()}
        self._cache: dict[tuple[str, int], EarningsSurprise | NoSignal] = {}

    def explain(self, isin: str, session: date) -> EarningsSurprise | NoSignal:
        """The ISIN's latest SUE knowable on ``session``, or the reason it has none."""
        dates = self._dates.get(isin)
        cutoff = 0 if not dates else bisect_right(dates, session)
        if cutoff == 0:
            return NoSignal.NO_FILINGS
        key = (isin, cutoff)
        if key not in self._cache:
            prefix = self._facts[isin][:cutoff]
            self._cache[key] = surprise_readings(prefix, as_of=session).get(
                isin, NoSignal.NO_FILINGS
            )
        return self._cache[key]

    def reading(self, isin: str, session: date) -> EarningsSurprise | None:
        """The ISIN's latest SUE knowable on ``session``, or ``None`` for no signal."""
        found = self.explain(isin, session)
        return found if isinstance(found, EarningsSurprise) else None

    def value(self, isin: str, session: date) -> Decimal | None:
        """The leg on ``session``: the SUE inside its 63-session window, 0 after, ``None`` if none.

        ``session`` must be a session of the calendar the panel was built on. The window opens on
        the first session on or after the latest quarter's earliest filing date, which counts as
        session 1 of 63; a later re-filing of that quarter never reopens or extends it.
        """
        reading = self.reading(isin, session)
        if reading is None:
            return None
        here = bisect_left(self._calendar, session)
        if here == len(self._calendar) or self._calendar[here] != session:
            raise ValueError(f"{session.isoformat()} is not a session of the panel's calendar")
        opened = bisect_left(self._calendar, reading.filing_date)
        return reading.sue if here - opened < ACTIVE_SESSIONS else _ZERO
