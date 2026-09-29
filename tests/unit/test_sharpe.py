"""X2 — PSR and DSR against the published worked example and hand-computed values.

The worked example is Bailey & López de Prado (2014), "The Deflated Sharpe Ratio", §"A numerical
example": N = 100 trials whose annualised Sharpe ratios have variance 0.5, a best annualised Sharpe
of 2.5, T = 1250 daily observations, skewness -3, kurtosis 10, 250 sessions a year. The paper gives
the deflated threshold as 0.1132 per day and DSR = 0.9004. Every figure here is per period.
"""

from __future__ import annotations

import math

import pytest

from backtest.sharpe import (
    SharpeStats,
    deflated_sharpe_ratio,
    expected_max_sharpe,
    probabilistic_sharpe_ratio,
    sharpe_stats,
    sharpe_variance,
)

_PAPER_SR = 2.5 / math.sqrt(250)
_PAPER_V = 0.5 / 250
_PAPER = SharpeStats(
    observations=1250, mean=0.0, stdev=1.0, sharpe=_PAPER_SR, skewness=-3.0, kurtosis=10.0
)


def test_the_expected_maximum_matches_the_paper() -> None:
    assert expected_max_sharpe(trials=100, sharpe_variance=_PAPER_V) == pytest.approx(
        0.1132, abs=5e-5
    )


def test_the_deflated_sharpe_ratio_matches_the_paper() -> None:
    dsr = deflated_sharpe_ratio(_PAPER, trials=100, sharpe_variance=_PAPER_V, benchmark_sharpe=0.0)
    assert dsr == pytest.approx(0.9004, abs=5e-4)


def test_removing_the_trial_count_adjustment_changes_the_answer() -> None:
    # Without the E[max] premium the paper's strategy would be "certain" (PSR(0) ~ 0.999997); the
    # adjustment is what brings it to 0.90, below the 0.95 bar. A DSR that ignored `trials` fails.
    plain = probabilistic_sharpe_ratio(_PAPER_SR, 1250, -3.0, 10.0, threshold=0.0)
    deflated = deflated_sharpe_ratio(
        _PAPER, trials=100, sharpe_variance=_PAPER_V, benchmark_sharpe=0.0
    )
    assert plain > 0.9999
    assert deflated < 0.95 < plain


def test_more_trials_deflate_more() -> None:
    values = [
        deflated_sharpe_ratio(_PAPER, trials=n, sharpe_variance=_PAPER_V, benchmark_sharpe=0.0)
        for n in (1, 2, 10, 28, 100, 1000)
    ]
    assert values == sorted(values, reverse=True)
    assert len(set(values)) == len(values)


def test_one_trial_is_the_plain_psr_at_the_benchmark() -> None:
    assert deflated_sharpe_ratio(
        _PAPER, trials=1, sharpe_variance=_PAPER_V, benchmark_sharpe=0.05
    ) == probabilistic_sharpe_ratio(_PAPER_SR, 1250, -3.0, 10.0, threshold=0.05)


def test_the_benchmark_shifts_the_threshold() -> None:
    at_zero = deflated_sharpe_ratio(
        _PAPER, trials=100, sharpe_variance=_PAPER_V, benchmark_sharpe=0.0
    )
    over_baseline = deflated_sharpe_ratio(
        _PAPER, trials=100, sharpe_variance=_PAPER_V, benchmark_sharpe=0.02
    )
    assert over_baseline < at_zero


def test_psr_by_hand_for_a_normal_series() -> None:
    # SR = 0.1, T = 101, skew 0, kurtosis 3: z = 0.1 * 10 / sqrt(1 + 0.5 * 0.01) = 0.997509,
    # Phi(0.997509) = 0.840742.
    assert probabilistic_sharpe_ratio(0.1, 101, 0.0, 3.0, threshold=0.0) == pytest.approx(
        0.840742, abs=2e-6
    )
    assert probabilistic_sharpe_ratio(0.1, 101, 0.0, 3.0, threshold=0.1) == pytest.approx(0.5)


def test_moments_by_hand() -> None:
    # r = (0.01, -0.02, 0.03, 0.00): mean 0.005; sample var 1.3e-3 / 3; m2 = 3.25e-4, m3 = 0,
    # m4 = 1.95625e-7, so skew 0 and kurtosis 1.95625e-7 / 3.25e-4^2 = 1.852071.
    stats = sharpe_stats([0.01, -0.02, 0.03, 0.00])
    assert stats.observations == 4
    assert stats.mean == pytest.approx(0.005)
    assert stats.stdev == pytest.approx(math.sqrt(1.3e-3 / 3))
    assert stats.sharpe == pytest.approx(0.005 / math.sqrt(1.3e-3 / 3))
    assert stats.skewness == pytest.approx(0.0, abs=1e-12)
    assert stats.kurtosis == pytest.approx(1.852071, abs=1e-6)


def test_variance_across_trials_is_the_sample_variance() -> None:
    assert sharpe_variance([0.01, 0.03, 0.05]) == pytest.approx(0.0004)
    with pytest.raises(ValueError, match="two trials"):
        sharpe_variance([0.01])


@pytest.mark.parametrize("trials", [0, -3])
def test_a_trial_count_below_one_is_refused(trials: int) -> None:
    with pytest.raises(ValueError, match="at least 1"):
        expected_max_sharpe(trials=trials, sharpe_variance=_PAPER_V)


def test_degenerate_series_are_refused() -> None:
    with pytest.raises(ValueError, match="three returns"):
        sharpe_stats([0.01, 0.02])
    with pytest.raises(ValueError, match="no variation"):
        sharpe_stats([0.01, 0.01, 0.01])
    with pytest.raises(ValueError, match="undefined"):
        probabilistic_sharpe_ratio(1.0, 100, 5.0, 3.0, threshold=0.0)
