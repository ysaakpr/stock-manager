"""X2: XIRR — the internal rate of return of an irregular, dated cashflow stream.

A SIP puts money in on whatever dates the instalments actually landed — the 1st that was a
trading day, the next that slipped to the 3rd over a long weekend — and takes it out (or marks it
to market) on one final date. Those intervals are not equal, so the ordinary IRR, which assumes
evenly-spaced periods, is the wrong tool: it would treat a January instalment and a December one
as the same age. XIRR is IRR with real calendar dates, discounting each flow by its actual
day-count fraction of a year. It is the number the plan compares a portfolio and its benchmarks on
(EXECUTION_PLAN §5.2), so paper and benchmark returns are one apples-to-apples figure.

Sign convention (the same one a spreadsheet's ``XIRR`` uses): money the investor *pays in* is
negative, money that *comes back* — a withdrawal, or the terminal mark-to-market value — is
positive. A stream with no sign change has no rate and is refused rather than returned as a
meaningless root.

Money is ``Decimal`` at the boundary (CLAUDE.md), but the root-find itself runs in ``float``: a
rate is a dimensionless ratio, not a rupee amount, and solving ``sum(cf / (1+r)**t) = 0`` needs a
transcendental power that ``Decimal`` cannot take exactly anyway. The returned rate is a
``Decimal`` quantized to a fixed precision so two runs on the same stream compare equal — the
cashflows that feed it stay ``Decimal`` end to end, and no rupee value is ever a float.

Day count is ACT/365F (actual days over a fixed 365-day year), matching Excel/LibreOffice ``XIRR``
so a hand-check in a spreadsheet reconciles to the same value (acceptance criterion 1).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

__all__ = [
    "Cashflow",
    "XIRRError",
    "npv",
    "xirr",
]

#: Fixed-year day count, matching a spreadsheet's ``XIRR``. A leap year is still 365 here — that is
#: what makes the result reconcile to Excel/LibreOffice rather than drift by a day a year.
_DAYS_PER_YEAR = 365.0

#: The rate is quantized to this many places so the same stream yields a byte-identical figure on
#: every run (replay determinism, invariant #11) and two rates compare equal without a tolerance.
_RATE_QUANTUM = Decimal("0.00000001")


class XIRRError(ValueError):
    """The cashflow stream has no well-defined internal rate of return.

    Raised loudly (CLAUDE.md: fail specific) rather than returning a nonsense number: an empty
    stream, a stream that is all one sign (no rate makes a set of same-signed flows sum to zero),
    or a solve that will not converge to a real root.
    """


@dataclass(frozen=True, slots=True)
class Cashflow:
    """One dated cash movement in the stream, from the investor's point of view.

    ``amount`` is negative for money paid in (a SIP instalment), positive for money received (a
    withdrawal or the terminal portfolio value). ``Decimal`` always — a float here would be a bug
    (CLAUDE.md). The date is a trading/calendar date in Asia/Kolkata; XIRR only ever looks at the
    gap between dates, never at a wall clock.
    """

    when: date
    amount: Decimal


def npv(rate: float, cashflows: Sequence[Cashflow]) -> float:
    """Net present value of ``cashflows`` discounted at annual ``rate``, in ``float``.

    Each flow is discounted by ``(1 + rate) ** (days_since_first / 365)``. The first flow's date is
    the anchor (its exponent is zero), so NPV is measured as of the stream's start. Assumes
    ``cashflows`` is non-empty and sorted ascending by date — ``xirr`` guarantees both before it
    calls here. Never mutates its input.
    """
    anchor = cashflows[0].when
    total = 0.0
    for flow in cashflows:
        years = (flow.when - anchor).days / _DAYS_PER_YEAR
        total += float(flow.amount) / (1.0 + rate) ** years
    return total


def _npv_derivative(rate: float, cashflows: Sequence[Cashflow]) -> float:
    """d(NPV)/d(rate) — the slope Newton's method steps along. Same assumptions as ``npv``."""
    anchor = cashflows[0].when
    total = 0.0
    for flow in cashflows:
        years = (flow.when - anchor).days / _DAYS_PER_YEAR
        if years == 0.0:
            continue
        total += -years * float(flow.amount) / (1.0 + rate) ** (years + 1.0)
    return total


def _has_both_signs(cashflows: Iterable[Cashflow]) -> bool:
    """True only if the stream contains at least one inflow and one outflow.

    A rate exists only when the flows can be made to cancel; a stream that is all pay-in or all
    pay-out never sums to zero at any rate, so this is the guard that keeps ``xirr`` from chasing a
    root that is not there.
    """
    saw_positive = False
    saw_negative = False
    for flow in cashflows:
        if flow.amount > 0:
            saw_positive = True
        elif flow.amount < 0:
            saw_negative = True
        if saw_positive and saw_negative:
            return True
    return False


def xirr(
    cashflows: Iterable[Cashflow],
    *,
    guess: float = 0.1,
    max_iterations: int = 100,
    tolerance: float = 1e-9,
) -> Decimal:
    """The annualized internal rate of return of a dated, irregular cashflow stream.

    What it does: finds the rate ``r`` at which ``npv(r, cashflows) == 0``, using Newton's method
    seeded at ``guess`` and falling back to bisection when Newton leaves the sensible domain
    (``rate <= -1`` makes the discount factor undefined) or stalls on a flat slope. Returns the
    rate as a ``Decimal`` — e.g. ``Decimal("0.12")`` for 12 % — quantized to eight places.

    What it assumes: at least one inflow and one outflow (a stream of one sign has no rate) and at
    least two flows. Order does not matter to the caller; the stream is sorted by date here.

    What it never does: return a made-up number when the solve fails. No sign change, or no
    convergence inside ``max_iterations``, raises ``XIRRError`` — a silent wrong return rate would
    poison every comparison built on it.
    """
    ordered = sorted(cashflows, key=lambda flow: flow.when)
    if len(ordered) < 2:
        raise XIRRError("XIRR needs at least two cashflows")
    if not _has_both_signs(ordered):
        raise XIRRError(
            "XIRR needs both an inflow and an outflow; a same-signed stream has no rate"
        )

    rate = _solve_newton(ordered, guess, max_iterations, tolerance)
    if rate is None:
        rate = _solve_bisection(ordered, max_iterations, tolerance)
    if rate is None:
        raise XIRRError("XIRR did not converge to a rate for this cashflow stream")
    return Decimal(repr(rate)).quantize(_RATE_QUANTUM)


def _solve_newton(
    cashflows: Sequence[Cashflow], guess: float, max_iterations: int, tolerance: float
) -> float | None:
    """Newton-Raphson from ``guess``; returns the rate, or ``None`` to hand off to bisection.

    Bails to ``None`` (rather than diverging) the moment a step would push the rate to ``-1`` or
    below — where the discount factor is undefined — or the slope goes flat, so the caller's
    bracketed fallback can take over on the hard streams Newton cannot handle from this seed.
    """
    rate = guess
    for _ in range(max_iterations):
        value = npv(rate, cashflows)
        if abs(value) < tolerance:
            return rate
        slope = _npv_derivative(rate, cashflows)
        if slope == 0.0:
            return None
        next_rate = rate - value / slope
        if next_rate <= -1.0:
            return None
        rate = next_rate
    return None


def _solve_bisection(
    cashflows: Sequence[Cashflow], max_iterations: int, tolerance: float
) -> float | None:
    """Bracketed fallback: widen a bracket over ``(-1, ∞)`` until NPV changes sign, then bisect.

    Slower but far more robust than Newton for a steep or awkwardly-seeded stream: as long as a
    real root exists above ``-100 %`` it is found. Returns ``None`` only if no sign change can be
    bracketed within the search range, which for a stream that already has both flow signs means
    the root is outside any economically-meaningful rate.
    """
    low, high = -0.9999999, 1.0
    value_low = npv(low, cashflows)
    value_high = npv(high, cashflows)
    # Grow the upper bound until the bracket straddles a sign change.
    expansions = 0
    while value_low * value_high > 0.0 and expansions < 200:
        high *= 2.0
        value_high = npv(high, cashflows)
        expansions += 1
    if value_low * value_high > 0.0:
        return None

    for _ in range(max(max_iterations, 200)):
        mid = (low + high) / 2.0
        value_mid = npv(mid, cashflows)
        if abs(value_mid) < tolerance:
            return mid
        if value_low * value_mid < 0.0:
            high = mid
        else:
            low, value_low = mid, value_mid
    return (low + high) / 2.0
