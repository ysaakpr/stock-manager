"""Holding-period study: the pure arithmetic — horizons, outcomes, probabilities, timing tests.

Everything here is a function of numbers already struck (NAV points, XIRRs, session calendars), so
it is tested on synthetic inputs without a lake. The driver (:mod:`backtest.holding_period`) feeds
it the persisted runs. ``ops/studies/preregistration-holding-period-2026-10-19.md`` fixes every
definition below before any study figure exists.

**Horizon cells.** A grid start is the first session on or after a month's *anchor day* (1 for the
month-start grid, 15 for the mid-month grid). A cell ``(start month m, h months)`` ends on the
session *before* month ``m + h``'s own anchor session, so a 12-month cell from 2020-03-02 ends
2021-02-26 and the next start's first session is never inside it. A cell exists only when that
next anchor session is on or before the span end: a horizon that would end inside an incomplete
month is never scored.

**Minimum horizon.** For a hurdle and a confidence ``c``, the smallest grid horizon ``h`` with
``P(meet) >= c`` at ``h`` *and at every longer grid horizon* — a horizon that clears the bar once
and then falls back below it is not a minimum holding period. ``None`` when no horizon qualifies.

What this module never does: read a clock or the lake, round a figure before it is compared, or
take a float for money (statistics such as the Wilson interval and the permutation test are
computed on ratios, not rupees).
"""

from __future__ import annotations

import math
import random
from bisect import bisect_left
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from decimal import Decimal
from typing import Final

from backtest.xirr import Cashflow, XIRRError, npv, xirr

__all__ = [
    "Z90",
    "anchor_sessions",
    "chain_growth",
    "growth",
    "highest_root",
    "horizon_end",
    "independent_windows",
    "longest_under_water",
    "max_drawdown",
    "min_horizon",
    "month_add",
    "parked_share",
    "permutation_spread_pvalue",
    "share_meeting",
    "sip_xirr",
    "wilson_interval",
]

_ZERO: Final = Decimal("0")
_ONE: Final = Decimal("1")
_DAYS: Final = Decimal("365")
#: The two-sided 90 % normal quantile, for the Wilson interval on a share.
Z90: Final = 1.6448536269514722


def month_add(year: int, month: int, months: int) -> tuple[int, int]:
    """``(year, month)`` moved ``months`` calendar months (may be negative)."""
    index = year * 12 + (month - 1) + months
    return index // 12, index % 12 + 1


def anchor_sessions(sessions: Sequence[date], anchor_day: int) -> dict[tuple[int, int], date]:
    """Each month's first session on or after its ``anchor_day``, keyed ``(year, month)``.

    A month with no session on or after the anchor day (impossible for 1 and 15 on NSE's calendar,
    but stated) has no entry. ``sessions`` must be ascending.
    """
    if not 1 <= anchor_day <= 28:
        raise ValueError(f"anchor_day must be 1..28, got {anchor_day}")
    out: dict[tuple[int, int], date] = {}
    for session in sessions:
        key = (session.year, session.month)
        if key not in out and session.day >= anchor_day:
            out[key] = session
    return out


def horizon_end(
    sessions: Sequence[date],
    anchors: Mapping[tuple[int, int], date],
    start: date,
    months: int,
    span_end: date,
) -> date | None:
    """The last session of the ``months``-month cell opening on the anchor session ``start``.

    It is the session immediately before the anchor session ``months`` months later; ``None`` when
    that anchor session is after ``span_end`` (the cell would end inside an incomplete month) or
    does not exist. Never returns a session after ``span_end``.
    """
    if months <= 0:
        raise ValueError(f"months must be positive, got {months}")
    key = month_add(start.year, start.month, months)
    target = anchors.get(key)
    if target is None or target > span_end:
        return None
    position = bisect_left(sessions, target)
    if position == 0 or sessions[position - 1] < start:
        return None
    return sessions[position - 1]


def growth(rate: Decimal, start: date, end: date) -> Decimal:
    """The growth factor an annual (Actual/365) rate compounds to over ``[start, end]``."""
    days = (end - start).days
    base = _ONE + rate
    if base <= _ZERO:
        raise ValueError(f"rate {rate} is a total loss or worse; no growth factor")
    return (base.ln() * Decimal(days) / _DAYS).exp()


def max_drawdown(values: Sequence[Decimal]) -> Decimal:
    """Largest peak-to-trough decline of ``values`` as a positive ratio (0.25 = -25 %)."""
    peak = _ZERO
    worst = _ZERO
    for value in values:
        peak = max(peak, value)
        if peak > _ZERO:
            worst = max(worst, (peak - value) / peak)
    return worst


def longest_under_water(points: Sequence[tuple[date, Decimal]]) -> int:
    """The longest spell, in calendar days, the path spent below its running peak.

    A spell runs from the last peak session to the first session that regains it; one still open at
    the end is counted to the last point. The opening value is the first peak, so a book that never
    recovers its deposit is under water for the whole window.
    """
    if not points:
        return 0
    peak_value = points[0][1]
    peak_day = points[0][0]
    longest = 0
    under = False
    for day, value in points[1:]:
        if value >= peak_value:
            if under:
                # The spell ends the session the peak is regained; that session is counted.
                longest = max(longest, (day - peak_day).days)
                under = False
            peak_value, peak_day = value, day
        else:
            under = True
            longest = max(longest, (day - peak_day).days)
    return longest


def parked_share(
    sessions: Sequence[date], rebalances: Sequence[date], risk_on: Mapping[date, bool]
) -> Decimal:
    """Share of ``sessions`` on which the latest rebalance at or before it read risk-off.

    ``rebalances`` are the cell's rebalance sessions (its first session included), ascending;
    ``risk_on`` the regime reading on each. A session before the first rebalance counts as parked
    (the book is still cash). Zero for an empty window.
    """
    if not sessions:
        return _ZERO
    parked = 0
    index = -1
    for session in sessions:
        while index + 1 < len(rebalances) and rebalances[index + 1] <= session:
            index += 1
        if index < 0 or not risk_on[rebalances[index]]:
            parked += 1
    return Decimal(parked) / Decimal(len(sessions))


def share_meeting(
    pairs: Sequence[tuple[Decimal | None, Decimal | None]],
) -> tuple[int, int]:
    """``(met, scored)``: how many ``(value, hurdle)`` pairs have ``value >= hurdle``.

    A pair with either side ``None`` (an after-tax figure the tax layer withheld) is *not scored*
    and is excluded from both counts — the caller reports how many were dropped.
    """
    met = scored = 0
    for value, hurdle in pairs:
        if value is None or hurdle is None:
            continue
        scored += 1
        if value >= hurdle:
            met += 1
    return met, scored


def min_horizon(shares: Mapping[int, tuple[int, int]], confidence: Decimal) -> int | None:
    """The smallest horizon from which ``met / scored >= confidence`` holds at every longer one.

    ``shares`` maps horizon (months) to ``(met, scored)``; a horizon with nothing scored is ignored.
    Returns ``None`` when even the longest scored horizon misses the bar.
    """
    horizons = sorted(h for h, (_, n) in shares.items() if n > 0)
    answer: int | None = None
    for h in reversed(horizons):
        met, scored = shares[h]
        if Decimal(met) >= confidence * Decimal(scored):
            answer = h
        else:
            break
    return answer


def independent_windows(span_months: int, horizon_months: int) -> int:
    """How many non-overlapping ``horizon_months`` windows fit in ``span_months``."""
    if horizon_months <= 0:
        raise ValueError("horizon must be positive")
    return span_months // horizon_months


def wilson_interval(met: int, scored: int, effective: int, z: float = Z90) -> tuple[float, float]:
    """A Wilson score interval on the share ``met / scored``, sized by ``effective`` observations.

    Overlapping windows are not independent, so the interval is struck as if only ``effective``
    (the non-overlapping count) had been seen — wide on purpose. ``(0, 1)`` when nothing is scored.
    """
    if scored == 0 or effective <= 0:
        return 0.0, 1.0
    p = met / scored
    n = float(effective)
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(0.0, centre - half), min(1.0, centre + half)


def chain_growth(legs: Sequence[tuple[Decimal, date, date]]) -> Decimal:
    """The product of each leg's growth factor; a leg is ``(annual rate, start, end)``."""
    total = _ONE
    for rate, start, end in legs:
        total *= growth(rate, start, end)
    return total


def sip_xirr(
    instalments: Sequence[tuple[date, Decimal]],
    unit_values: Mapping[date, Decimal],
    terminal: tuple[date, Decimal],
) -> Decimal | None:
    """Pre-tax XIRR of equal rupee instalments bought as units of a NAV path, sold at ``terminal``.

    ``instalments`` are ``(date, rupees)``; ``unit_values`` the path's value on each instalment
    date; ``terminal`` ``(date, value)``. Each instalment buys ``rupees / value`` units, every unit
    is valued at the terminal value — a fund-like SIP, without the whole-share rounding a small
    book would face. ``None`` when the stream has no rate.
    """
    units = _ZERO
    flows: list[Cashflow] = []
    for when, amount in instalments:
        units += amount / unit_values[when]
        flows.append(Cashflow(when, -amount))
    flows.append(Cashflow(terminal[0], units * terminal[1]))
    try:
        return xirr(flows)
    except XIRRError:
        return None


def permutation_spread_pvalue(
    groups: Mapping[int, Sequence[float]],
    *,
    iterations: int = 2000,
    seed: int = 20261019,
    statistic: Callable[[Sequence[float]], float] | None = None,
) -> tuple[float, float]:
    """``(observed spread, p-value)`` of the range of group medians under random relabelling.

    The spread is max - min of the per-group medians (or of ``statistic``). The p-value is the
    share of ``iterations`` shuffles of all values across the same group sizes whose spread is at
    least the observed one (with the +1 correction). Seeded, so the figure is reproducible.
    """
    stat = statistic or _median
    keys = sorted(groups)
    sizes = [len(groups[k]) for k in keys]
    pooled = [v for k in keys for v in groups[k]]
    if not pooled or any(size == 0 for size in sizes):
        return 0.0, 1.0

    def spread(values: Sequence[float]) -> float:
        out: list[float] = []
        index = 0
        for size in sizes:
            out.append(stat(values[index : index + size]))
            index += size
        return max(out) - min(out)

    observed = spread(pooled)
    rng = random.Random(seed)
    shuffled = list(pooled)
    hits = 0
    for _ in range(iterations):
        rng.shuffle(shuffled)
        if spread(shuffled) >= observed - 1e-15:
            hits += 1
    return observed, (hits + 1) / (iterations + 1)


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    return ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def highest_root(flows: Sequence[Cashflow]) -> Decimal | None:
    """The economically meaningful rate of a deposit-then-proceeds stream the solver missed.

    ``backtest.xirr`` documents the case it cannot bracket: a small tax paid *after* the terminal
    value makes NPV negative near -100 % as well as at high rates, so a stream with a deep
    short-window loss has two roots and no bracket straddles the real one. Scanning down from a
    high rate, the first sign change of NPV (negative above, positive below) is the rate the
    stream would have without the late tax's distortion near -100 %; it is then bisected. ``None``
    when no sign change exists above -99.99 %. Used only where ``xirr`` raised.
    """
    ordered = sorted(flows, key=lambda flow: flow.when)

    def value(rate: float) -> float | None:
        try:
            return npv(rate, ordered)
        except (ArithmeticError, ValueError):
            return None

    upper = 10.0
    upper_value = value(upper)
    if upper_value is None or upper_value >= 0:
        return None
    # Step down in (1 + r) by a constant ratio: fine near -100 %, coarse at high rates.
    growth_step = 0.98
    rate = upper
    while rate > -0.9999:
        lower = (1.0 + rate) * growth_step - 1.0
        lower_value = value(lower)
        if lower_value is None:
            return None
        if lower_value >= 0:
            lo, hi = lower, rate
            for _ in range(200):
                mid = (lo + hi) / 2
                mid_value = value(mid)
                if mid_value is None:
                    return None
                if mid_value >= 0:
                    lo = mid
                else:
                    hi = mid
                if hi - lo < 1e-10:
                    break
            return Decimal(repr((lo + hi) / 2)).quantize(Decimal("0.00000001"))
        rate = lower
    return None
