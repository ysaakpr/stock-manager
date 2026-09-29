"""X2: the probabilistic and deflated Sharpe ratios the round-2 decision rule is struck with.

``ops/studies/preregistration-signals-2026-09-29.md`` §4 criterion 4 keeps an H-arm only if the
deflated Sharpe ratio (Bailey & López de Prado, "The Deflated Sharpe Ratio: Correcting for
Selection Bias, Backtest Overfitting and Non-Normality", *Journal of Portfolio Management* 40(5),
2014), computed on the daily after-tax NAV of the concatenated test windows with the recorded
trial count, gives probability >= 0.95 that the true Sharpe ratio exceeds the baseline's.

**The probabilistic Sharpe ratio (PSR)** is the probability that the true per-period Sharpe ratio
exceeds a threshold ``SR*``, given the estimate ``SR^`` over ``T`` returns with skewness ``g3`` and
(non-excess) kurtosis ``g4``::

    PSR(SR*) = Phi( (SR^ - SR*) * sqrt(T - 1) / sqrt(1 - g3 * SR^ + (g4 - 1) / 4 * SR^^2) )

**The deflated Sharpe ratio (DSR)** is the PSR at the threshold a best-of-``N`` search would clear
by luck alone. For ``N`` independent trials whose Sharpe estimates have variance ``V`` around a
true value of zero, the expected maximum is::

    E[max SR] = sqrt(V) * ((1 - gamma) * Z(1 - 1/N) + gamma * Z(1 - 1/(N e)))

with ``gamma`` the Euler-Mascheroni constant and ``Z`` the standard normal quantile. The paper's
null is a true Sharpe of zero; the pre-registration's is a true Sharpe *equal to the baseline's*,
so the threshold here is ``SR_baseline + E[max SR]`` — the same luck premium, measured from the
baseline rather than from zero (``benchmark_sharpe``; zero reproduces the paper exactly).

**Float, on purpose.** These are statistics of a return series, not money: nothing here is a price,
a quantity or a cost, and the normal distribution they are compared against is a float function.
The NAV the returns come from stays ``Decimal`` (``backtest.nav``); only the returns are floats.

**Per-period, never annualised.** ``T``, ``SR^`` and ``V`` are all in the units of the return
series (one sampled session); annualising one and not the others is the classic misuse.

What this module never does: default the trial count (it is a required input, and the verdict
prints the number used), or annualise a Sharpe ratio behind the caller's back.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from statistics import NormalDist

__all__ = [
    "EULER_MASCHERONI",
    "SharpeStats",
    "deflated_sharpe_ratio",
    "expected_max_sharpe",
    "probabilistic_sharpe_ratio",
    "sharpe_stats",
    "sharpe_variance",
]

#: gamma in Bailey & López de Prado (2014), eq. 5 — the Euler-Mascheroni constant.
EULER_MASCHERONI = 0.5772156649015329

_NORMAL = NormalDist()


@dataclass(frozen=True, slots=True)
class SharpeStats:
    """The moments of one return series that the PSR/DSR need, all per period.

    ``sharpe`` is mean / sample standard deviation (ddof = 1); ``skewness`` and ``kurtosis`` are
    the population (moment) estimators, kurtosis *non-excess* (a normal series has 3).
    """

    observations: int
    mean: float
    stdev: float
    sharpe: float
    skewness: float
    kurtosis: float


def sharpe_stats(returns: Sequence[float]) -> SharpeStats:
    """The per-period Sharpe ratio, skewness and kurtosis of ``returns``.

    Raises ``ValueError`` for fewer than three returns or a series with no variation — a Sharpe
    ratio over either is not an estimate of anything.
    """
    n = len(returns)
    if n < 3:
        raise ValueError(f"a Sharpe ratio needs at least three returns, got {n}")
    mean = math.fsum(returns) / n
    deviations = [r - mean for r in returns]
    m2 = math.fsum(d * d for d in deviations) / n
    if m2 <= 0.0:
        raise ValueError("the return series has no variation; its Sharpe ratio is undefined")
    m3 = math.fsum(d**3 for d in deviations) / n
    m4 = math.fsum(d**4 for d in deviations) / n
    stdev = math.sqrt(m2 * n / (n - 1))
    return SharpeStats(
        observations=n,
        mean=mean,
        stdev=stdev,
        sharpe=mean / stdev,
        skewness=m3 / m2**1.5,
        kurtosis=m4 / m2**2,
    )


def probabilistic_sharpe_ratio(
    sharpe: float, observations: int, skewness: float, kurtosis: float, *, threshold: float
) -> float:
    """P(true Sharpe > ``threshold``) given the estimate and its series' moments (PSR, eq. 2).

    All inputs per period; ``kurtosis`` non-excess. Raises ``ValueError`` when the variance term
    under the square root is not positive (extreme skew against a large Sharpe), where the
    statistic is undefined rather than zero or one.
    """
    if observations < 2:
        raise ValueError(f"PSR needs at least two observations, got {observations}")
    variance = 1.0 - skewness * sharpe + (kurtosis - 1.0) / 4.0 * sharpe * sharpe
    if variance <= 0.0:
        raise ValueError(f"PSR is undefined: the Sharpe estimator's variance term is {variance}")
    z = (sharpe - threshold) * math.sqrt(observations - 1) / math.sqrt(variance)
    return _NORMAL.cdf(z)


def expected_max_sharpe(*, trials: int, sharpe_variance: float) -> float:
    """E[max] of ``trials`` independent zero-mean Sharpe estimates of variance ``sharpe_variance``.

    Eq. 5 of the paper. One trial carries no selection, so its premium is zero. Raises
    ``ValueError`` for a non-positive trial count or a negative variance.
    """
    if trials < 1:
        raise ValueError(f"the trial count must be at least 1, got {trials}")
    if sharpe_variance < 0.0:
        raise ValueError(f"a variance cannot be negative, got {sharpe_variance}")
    if trials == 1:
        return 0.0
    first = _NORMAL.inv_cdf(1.0 - 1.0 / trials)
    second = _NORMAL.inv_cdf(1.0 - 1.0 / (trials * math.e))
    spread = (1.0 - EULER_MASCHERONI) * first + EULER_MASCHERONI * second
    return math.sqrt(sharpe_variance) * spread


def deflated_sharpe_ratio(
    stats: SharpeStats,
    *,
    trials: int,
    sharpe_variance: float,
    benchmark_sharpe: float,
) -> float:
    """PSR at ``benchmark_sharpe + E[max SR]`` — the DSR, measured from the benchmark's Sharpe.

    ``trials`` is the number of configurations that were looked at on this data (the
    pre-registration's recorded count), ``sharpe_variance`` the variance of their per-period
    Sharpe estimates, ``benchmark_sharpe`` the per-period Sharpe the candidate must beat (the
    frozen baseline's; ``0.0`` is the paper's own null). None has a default.
    """
    threshold = benchmark_sharpe + expected_max_sharpe(
        trials=trials, sharpe_variance=sharpe_variance
    )
    return probabilistic_sharpe_ratio(
        stats.sharpe, stats.observations, stats.skewness, stats.kurtosis, threshold=threshold
    )


def sharpe_variance(sharpes: Sequence[float]) -> float:
    """The sample variance (ddof = 1) of per-period Sharpe estimates across trials."""
    if len(sharpes) < 2:
        raise ValueError(f"a variance across trials needs at least two trials, got {len(sharpes)}")
    mean = math.fsum(sharpes) / len(sharpes)
    return math.fsum((s - mean) ** 2 for s in sharpes) / (len(sharpes) - 1)
