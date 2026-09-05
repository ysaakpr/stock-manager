"""M10.5 — signal-ready fundamental metrics, computed point-in-time off the PIT store (D6, §4.5).

The raw PIT facts (`dataplatform.store.pit_fundamentals`) are what a filing *said*; a strategy needs
what those statements *imply* as of a decision date — trailing-twelve-month earnings, year-on-year
growth, a P/E against that day's price, book value and return on equity where the balance sheet is
disclosed. This module derives those, and it does so under one discipline that is enforced rather
than requested (invariant #7):

* **Every metric as-of a date reads only filings knowable then.** `compute_metrics` takes the facts
  *and* the `as_of` date, and refuses — `PitError`, not a silent drop — any fact whose
  `filing_date` is after it. A caller that feeds the full history with an `as_of` gets exactly the
  view the market had that evening; a caller that leaks a later filing is told, loudly. Restatements
  are collapsed to the latest filing *on or before* `as_of` — the restated number is invisible to a
  historical value until the day it was actually published (invariant #8).
* **A metric that cannot be computed says why, and never invents a value.** Each field is either a
  `Decimal` or an `Unavailable` reason: too few quarters, a gap in the quarterly run, a non-positive
  earnings base, no share count, no price, no balance sheet. P/B and ROE need
  `shareholders_equity_excl_revaluation`, which only ~19% of filings (the annual ones with a filled
  reserves tag) carry; where it is absent the metric is `BALANCE_SHEET_ABSENT`, not zero or a proxy.
* **Valuation goes through market capitalisation, not per-share EPS.** P/E is
  `price * shares_outstanding / TTM earnings`. Shares outstanding is derived by the parser from the
  paid-up capital and face value of the *latest* filing, so a split or bonus between two filings
  moves the share count and the price together and the ratio is unaffected; summing four quarterly
  EPS figures across a split would not be. `eps_ttm` is still reported (it is what Screener shows)
  but the policy-facing ratio is the market-cap one.
* **Consolidated is preferred where filed, standalone otherwise** — one nature per ISIN per as-of,
  never a mix: a TTM that summed two consolidated and two standalone quarters would be a number
  about nothing.

* **A filing stated at the wrong scale is excluded whole, and counted.** Roughly one filing in a
  hundred states every figure in the wrong unit — a document 100x too small (PFC, Dec 2023) or 100x
  too large (PTC, FY2018) against the same company's other filings, found by comparing our figures
  with an independent publisher's. Paid-up equity capital is the detector: it is the one figure that
  is stable across a company's filings, so a filing whose paid-up capital sits a clean power of ten
  (100x or more, either way) from the company's median is a scale error, and none of its facts may
  enter a metric. `filings_excluded_scale` says how many were dropped for the ISIN.

Quarters are the periods 80..100 days long; annual periods (350..380 days) are used only for the
balance sheet and are never summed into a TTM. Money is `Decimal`, identity is ISIN, the as-of date
is the caller's, and nothing here reads a clock, a store or the network — `metrics_asof` is the thin
convenience that reads the store and hands the facts here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise
from pathlib import Path
from typing import Final, Protocol

from dataplatform.ingest.xbrl import Nature
from dataplatform.query.pit import PitError
from dataplatform.store.pit_fundamentals import read_pit

__all__ = [
    "CONCEPTS_USED",
    "FactRow",
    "FundamentalMetrics",
    "MetricValue",
    "Unavailable",
    "compute_metrics",
    "metrics_asof",
]

_ZERO: Final = Decimal("0")
_ONE: Final = Decimal("1")
_QUARTER_DAYS: Final = (80, 100)
_ANNUAL_DAYS: Final = (350, 380)
#: Two quarter-ends are "consecutive" when they sit one calendar quarter apart (allowing month-end
#: drift: 89..92 days); a year apart is 358..372 days. Both windows are generous on purpose — a
#: filer whose quarter ends on the 30th rather than the 31st is not a gap.
_CONSECUTIVE_DAYS: Final = (85, 97)
_YEAR_APART_DAYS: Final = (355, 375)
_TTM_QUARTERS: Final = 4
#: A filing whose paid-up capital is within this relative distance of 10^k (|k| >= 2) times the
#: company's median paid-up capital is stated at the wrong scale. Two orders of magnitude is far
#: beyond any real capital event; 3% absorbs a small rights issue riding on top of the scale slip.
_SCALE_MIN_POWER: Final = 2
_SCALE_MAX_POWER: Final = 9
_SCALE_TOLERANCE: Final = Decimal("0.03")

#: The concept keys this module reads. Anything else in the store is ignored here, which is what
#: lets a caller pre-filter the fact stream for speed without changing a result.
CONCEPTS_USED: Final[frozenset[str]] = frozenset(
    {
        "paid_up_equity_capital",
        "revenue_from_operations",
        "profit_after_tax",
        "profit_attributable_to_owners",
        "eps_basic",
        "shares_outstanding",
        "shareholders_equity_excl_revaluation",
    }
)


class Unavailable(StrEnum):
    """Why a metric could not be computed as of the date — stated, never papered over."""

    NO_FILINGS = "NO_FILINGS"
    INSUFFICIENT_QUARTERS = "INSUFFICIENT_QUARTERS"
    QUARTER_GAP = "QUARTER_GAP"
    NO_PRIOR_YEAR = "NO_PRIOR_YEAR"
    NEGATIVE_BASE = "NEGATIVE_BASE"
    NON_POSITIVE_EARNINGS = "NON_POSITIVE_EARNINGS"
    NON_POSITIVE_REVENUE = "NON_POSITIVE_REVENUE"
    NO_SHARE_COUNT = "NO_SHARE_COUNT"
    NO_PRICE = "NO_PRICE"
    BALANCE_SHEET_ABSENT = "BALANCE_SHEET_ABSENT"
    NON_POSITIVE_EQUITY = "NON_POSITIVE_EQUITY"


#: A metric is a number or the reason there is none. Consumers must handle both; there is no
#: sentinel zero anywhere in this module.
MetricValue = Decimal | Unavailable


class FactRow(Protocol):
    """The slice of a `FundamentalFact` this module reads — so a lightweight row satisfies it too.

    `FundamentalFact` itself is a `FactRow`. A backtest that has half a million facts in memory may
    hand in plain records with these attributes rather than pydantic models; the arithmetic is the
    same and so is the point-in-time refusal.
    """

    @property
    def isin(self) -> str: ...
    @property
    def period_start(self) -> date | None: ...
    @property
    def period_end(self) -> date: ...
    @property
    def filing_date(self) -> date: ...
    @property
    def filing_id(self) -> str: ...
    @property
    def nature(self) -> Nature: ...
    @property
    def concept(self) -> str: ...
    @property
    def segment(self) -> str | None: ...
    @property
    def value(self) -> Decimal: ...


@dataclass(frozen=True, slots=True)
class FundamentalMetrics:
    """The signal-ready view of one ISIN as of one date — every field a value or a stated reason.

    `knowable_date` is the latest filing date that entered any field; it is never after `as_of`
    (the constructor path refuses later filings), and it is what a `Dataset` guard checks.
    `quarters` is how many distinct knowable quarters existed, so a consumer can require a minimum
    history.
    """

    isin: str
    as_of: date
    nature: Nature
    knowable_date: date
    latest_period_end: date
    quarters: int
    revenue_ttm: MetricValue
    earnings_ttm: MetricValue
    eps_ttm: MetricValue
    revenue_yoy: MetricValue
    earnings_yoy: MetricValue
    earnings_ttm_yoy: MetricValue
    net_margin_ttm: MetricValue
    net_margin_trend: MetricValue
    shares_outstanding: MetricValue
    market_cap: MetricValue
    pe_ttm: MetricValue
    earnings_yield: MetricValue
    book_value: MetricValue
    pb: MetricValue
    roe: MetricValue

    filings_excluded_scale: int = 0

    def days_since_filing(self) -> int:
        """How stale the newest filing is on `as_of` — a consumer's staleness screen input."""
        return (self.as_of - self.knowable_date).days


# ── the computation ─────────────────────────────────────────────────────────────────────────────


def compute_metrics(
    facts: Iterable[FactRow],
    *,
    as_of: date,
    prices: Mapping[str, Decimal] | None = None,
    prefer: Nature = Nature.CONSOLIDATED,
) -> dict[str, FundamentalMetrics]:
    """Derive every ISIN's metrics as of `as_of` from facts knowable then.

    What it does: refuses any fact filed after `as_of` (`PitError` — invariant #7, the leak is
    named, not filtered), collapses restatements to the latest knowable filing per period, picks one
    nature per ISIN (`prefer` if it has quarterly earnings, else the other), and computes the
    trailing, growth and valuation metrics. `prices` (ISIN → close on `as_of`) is what turns
    earnings into a P/E; without it every valuation field is `NO_PRICE`.
    What it assumes: quarterly facts are periods of 80..100 days and annual facts 350..380; segment
    facts are ignored (`segment is None` is company level).
    What it never does: read a clock or a store, sum quarters across two natures, or return a number
    it could not derive.
    """
    price_of = prices if prices is not None else {}
    by_isin: dict[str, list[FactRow]] = {}
    for fact in facts:
        if fact.concept not in CONCEPTS_USED or fact.segment is not None:
            continue
        if fact.filing_date > as_of:
            raise PitError(
                f"fact for {fact.isin} {fact.concept} filed {fact.filing_date.isoformat()} is not "
                f"knowable as of {as_of.isoformat()}; a later filing leaked into a point-in-time "
                "metric (invariant #7)"
            )
        by_isin.setdefault(fact.isin, []).append(fact)
    out: dict[str, FundamentalMetrics] = {}
    for isin in sorted(by_isin):
        metrics = _metrics_for(
            isin, by_isin[isin], as_of=as_of, price=price_of.get(isin), prefer=prefer
        )
        if metrics is not None:
            out[isin] = metrics
    return out


def metrics_asof(
    as_of: date,
    *,
    prices: Mapping[str, Decimal] | None = None,
    data_root: Path | None = None,
    prefer: Nature = Nature.CONSOLIDATED,
) -> dict[str, FundamentalMetrics]:
    """`compute_metrics` over the PIT store's facts knowable on `as_of` (`read_pit`)."""
    return compute_metrics(
        read_pit(as_of, data_root=data_root), as_of=as_of, prices=prices, prefer=prefer
    )


# ── per-ISIN derivation ──────────────────────────────────────────────────────────────────────────


_PeriodKey = tuple[date | None, date]


def _latest_per_period(rows: Sequence[FactRow]) -> dict[_PeriodKey, FactRow]:
    """Collapse restatements: per (period_start, period_end) the latest knowable filing wins."""
    latest: dict[_PeriodKey, FactRow] = {}
    for row in rows:
        key = (row.period_start, row.period_end)
        current = latest.get(key)
        if current is None or row.filing_date > current.filing_date:
            latest[key] = row
    return latest


def _is_quarter(row: FactRow) -> bool:
    if row.period_start is None:
        return False
    days = (row.period_end - row.period_start).days
    return _QUARTER_DAYS[0] <= days <= _QUARTER_DAYS[1]


def _is_annual(row: FactRow) -> bool:
    if row.period_start is None:
        return False
    days = (row.period_end - row.period_start).days
    return _ANNUAL_DAYS[0] <= days <= _ANNUAL_DAYS[1]


def _quarter_series(rows: Sequence[FactRow], concept: str) -> list[FactRow]:
    """Quarterly facts for `concept`, restatement-collapsed, ascending by period_end."""
    chosen = [r for r in rows if r.concept == concept and _is_quarter(r)]
    return sorted(_latest_per_period(chosen).values(), key=lambda r: r.period_end)


def _pick_nature(rows: Sequence[FactRow], prefer: Nature) -> Nature | None:
    """The nature to report: `prefer` if it has quarterly earnings, else the other, else none."""
    natures = [prefer, *(n for n in Nature if n is not prefer)]
    for nature in natures:
        if any(
            r.nature is nature and r.concept == "profit_after_tax" and _is_quarter(r) for r in rows
        ):
            return nature
    return None


def _consecutive(earlier: date, later: date) -> bool:
    return _CONSECUTIVE_DAYS[0] <= (later - earlier).days <= _CONSECUTIVE_DAYS[1]


def _year_apart(earlier: date, later: date) -> bool:
    return _YEAR_APART_DAYS[0] <= (later - earlier).days <= _YEAR_APART_DAYS[1]


def _ttm(series: Sequence[FactRow], *, ending_index: int) -> MetricValue:
    """Sum of the four consecutive quarters ending at `series[ending_index]`, or why not."""
    if ending_index + 1 < _TTM_QUARTERS:
        return Unavailable.INSUFFICIENT_QUARTERS
    window = series[ending_index - _TTM_QUARTERS + 1 : ending_index + 1]
    for earlier, later in pairwise(window):
        if not _consecutive(earlier.period_end, later.period_end):
            return Unavailable.QUARTER_GAP
    return sum((r.value for r in window), _ZERO)


def _yoy(series: Sequence[FactRow]) -> MetricValue:
    """Latest quarter against the same quarter a year earlier: (now - then) / |then|."""
    if not series:
        return Unavailable.INSUFFICIENT_QUARTERS
    latest = series[-1]
    prior = next(
        (r for r in reversed(series[:-1]) if _year_apart(r.period_end, latest.period_end)), None
    )
    if prior is None:
        return Unavailable.NO_PRIOR_YEAR
    if prior.value <= _ZERO:
        return Unavailable.NEGATIVE_BASE
    return (latest.value - prior.value) / prior.value


def _ttm_yoy(series: Sequence[FactRow]) -> MetricValue:
    """TTM ending at the latest quarter against the TTM ending four quarters earlier."""
    if len(series) < 2 * _TTM_QUARTERS:
        return Unavailable.INSUFFICIENT_QUARTERS
    now = _ttm(series, ending_index=len(series) - 1)
    then = _ttm(series, ending_index=len(series) - 1 - _TTM_QUARTERS)
    if isinstance(now, Unavailable):
        return now
    if isinstance(then, Unavailable):
        return then
    if not _year_apart(series[-1 - _TTM_QUARTERS].period_end, series[-1].period_end):
        return Unavailable.QUARTER_GAP
    if then <= _ZERO:
        return Unavailable.NEGATIVE_BASE
    return (now - then) / then


def _ratio(
    numerator: MetricValue, denominator: MetricValue, *, non_positive: Unavailable
) -> MetricValue:
    if isinstance(numerator, Unavailable):
        return numerator
    if isinstance(denominator, Unavailable):
        return denominator
    if denominator <= _ZERO:
        return non_positive
    return numerator / denominator


def _earnings_series(rows: Sequence[FactRow]) -> list[FactRow]:
    """Owners' share of profit where every quarter states it, else the bottom line.

    A consolidated filer's EPS is struck on the parent's share; using the group bottom line against
    the parent's share count would overstate earnings for a company with large minorities (the
    GRASIM case in the data-gap plan). Fall back to `profit_after_tax` only when the owners' figure
    is not stated for the whole run — never mix the two inside one TTM.
    """
    owners = _quarter_series(rows, "profit_attributable_to_owners")
    pat = _quarter_series(rows, "profit_after_tax")
    owner_periods = {r.period_end for r in owners}
    if pat and all(r.period_end in owner_periods for r in pat):
        return owners
    return pat


def _latest_value(
    rows: Sequence[FactRow], concept: str, *, annual_only: bool
) -> tuple[Decimal, date] | None:
    """The newest knowable value of an entity-level concept: by filing date, then by period end."""
    candidates = [r for r in rows if r.concept == concept and (not annual_only or _is_annual(r))]
    if not candidates:
        return None
    best = max(candidates, key=lambda r: (r.filing_date, r.period_end))
    return best.value, best.filing_date


def _metrics_for(
    isin: str,
    rows: Sequence[FactRow],
    *,
    as_of: date,
    price: Decimal | None,
    prefer: Nature,
) -> FundamentalMetrics | None:
    rows, excluded = _drop_misscaled_filings(rows)
    nature = _pick_nature(rows, prefer)
    if nature is None:
        return None  # no quarterly earnings under either nature: nothing to derive
    own = [r for r in rows if r.nature is nature]
    earnings = _earnings_series(own)
    revenue = _quarter_series(own, "revenue_from_operations")
    eps = _quarter_series(own, "eps_basic")
    if not earnings:
        return None

    latest_period_end = earnings[-1].period_end
    knowable = max(r.filing_date for r in own)

    earnings_ttm = _ttm(earnings, ending_index=len(earnings) - 1)
    revenue_ttm = (
        _ttm(revenue, ending_index=len(revenue) - 1)
        if revenue
        else Unavailable.INSUFFICIENT_QUARTERS
    )
    eps_ttm = _ttm(eps, ending_index=len(eps) - 1) if eps else Unavailable.INSUFFICIENT_QUARTERS

    net_margin_ttm = _ratio(
        earnings_ttm, revenue_ttm, non_positive=Unavailable.NON_POSITIVE_REVENUE
    )
    net_margin_trend: MetricValue
    if len(earnings) >= 2 * _TTM_QUARTERS and len(revenue) >= 2 * _TTM_QUARTERS:
        prior_margin = _ratio(
            _ttm(earnings, ending_index=len(earnings) - 1 - _TTM_QUARTERS),
            _ttm(revenue, ending_index=len(revenue) - 1 - _TTM_QUARTERS),
            non_positive=Unavailable.NON_POSITIVE_REVENUE,
        )
        if isinstance(net_margin_ttm, Unavailable):
            net_margin_trend = net_margin_ttm
        elif isinstance(prior_margin, Unavailable):
            net_margin_trend = prior_margin
        else:
            net_margin_trend = net_margin_ttm - prior_margin
    else:
        net_margin_trend = Unavailable.INSUFFICIENT_QUARTERS

    # Shares are entity-level: take the newest across natures (a bonus is a bonus for both books).
    shares = _latest_value(rows, "shares_outstanding", annual_only=False)
    shares_outstanding: MetricValue = (
        shares[0] if shares is not None else Unavailable.NO_SHARE_COUNT
    )
    if isinstance(shares_outstanding, Decimal) and shares_outstanding <= _ZERO:
        shares_outstanding = Unavailable.NO_SHARE_COUNT

    market_cap: MetricValue
    if price is None:
        market_cap = Unavailable.NO_PRICE
    elif isinstance(shares_outstanding, Unavailable):
        market_cap = shares_outstanding
    else:
        market_cap = price * shares_outstanding

    pe_ttm = _ratio(market_cap, earnings_ttm, non_positive=Unavailable.NON_POSITIVE_EARNINGS)
    earnings_yield: MetricValue
    if isinstance(market_cap, Unavailable):
        earnings_yield = market_cap
    elif isinstance(earnings_ttm, Unavailable):
        earnings_yield = earnings_ttm
    else:
        earnings_yield = earnings_ttm / market_cap if market_cap > _ZERO else Unavailable.NO_PRICE

    equity = _latest_value(own, "shareholders_equity_excl_revaluation", annual_only=True)
    book_value: MetricValue = equity[0] if equity is not None else Unavailable.BALANCE_SHEET_ABSENT
    pb = _ratio(market_cap, book_value, non_positive=Unavailable.NON_POSITIVE_EQUITY)
    roe = _ratio(earnings_ttm, book_value, non_positive=Unavailable.NON_POSITIVE_EQUITY)

    return FundamentalMetrics(
        isin=isin,
        as_of=as_of,
        nature=nature,
        knowable_date=knowable,
        latest_period_end=latest_period_end,
        quarters=len(earnings),
        revenue_ttm=revenue_ttm,
        earnings_ttm=earnings_ttm,
        eps_ttm=eps_ttm,
        revenue_yoy=_yoy(revenue),
        earnings_yoy=_yoy(earnings),
        earnings_ttm_yoy=_ttm_yoy(earnings),
        net_margin_ttm=net_margin_ttm,
        net_margin_trend=net_margin_trend,
        shares_outstanding=shares_outstanding,
        market_cap=market_cap,
        pe_ttm=pe_ttm,
        earnings_yield=earnings_yield,
        book_value=book_value,
        pb=pb,
        roe=roe,
        filings_excluded_scale=excluded,
    )


def _drop_misscaled_filings(rows: Sequence[FactRow]) -> tuple[list[FactRow], int]:
    """Remove every fact of a filing whose paid-up capital is a clean 10^k off the company's median.

    Needs at least three filings stating paid-up capital to have a median worth trusting; with fewer
    nothing is dropped. Returns the surviving rows and how many filings were excluded.
    """
    paid_up: dict[str, Decimal] = {}
    for row in rows:
        if row.concept == "paid_up_equity_capital" and row.value > _ZERO:
            paid_up.setdefault(row.filing_id, row.value)
    if len(paid_up) < 3:
        return list(rows), 0
    ordered = sorted(paid_up.values())
    median = ordered[len(ordered) // 2]
    suspect = {fid for fid, value in paid_up.items() if _is_power_of_ten_off(value / median)}
    if not suspect:
        return list(rows), 0
    return [r for r in rows if r.filing_id not in suspect], len(suspect)


def _is_power_of_ten_off(ratio: Decimal) -> bool:
    for power in range(_SCALE_MIN_POWER, _SCALE_MAX_POWER + 1):
        for target in (Decimal(10) ** power, Decimal(1) / Decimal(10) ** power):
            if abs(ratio / target - _ONE) <= _SCALE_TOLERANCE:
                return True
    return False


def quarter_ends_between(start: date, end: date) -> list[date]:
    """Calendar quarter-ends in [start, end] — a test/report helper for building fact runs."""
    ends: list[date] = []
    cursor = date(start.year, 3, 31)
    while cursor <= end:
        if cursor >= start:
            ends.append(cursor)
        month = cursor.month + 3
        year = cursor.year + (1 if month > 12 else 0)
        month = month - 12 if month > 12 else month
        last = date(
            year + (1 if month == 12 else 0), 1 if month == 12 else month + 1, 1
        ) - timedelta(days=1)
        cursor = last
    return ends
