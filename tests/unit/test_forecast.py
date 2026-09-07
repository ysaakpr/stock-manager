"""The forward-return forecast: ranks, an incremental fit, and the refusals that keep it honest.

Three things are worth a test here and the rest is arithmetic: that `rank_scale` puts a missing
value at the neutral middle without inventing one, that the accumulated normal equations recover a
relationship that is really there, and that the fit refuses — returns `None` — rather than handing
back a number in the three situations where a number would be a fiction: too little data, a
constant feature, and a singular system.

Determinism is tested directly, because the whole reason the solve is hand-rolled rather than a
numpy call is that a replay's journal digest must match between the laptop and the server.
"""

from __future__ import annotations

import random
from typing import Final

from backtest.forecast import (
    FEATURE_NAMES,
    MIN_OBSERVATIONS,
    Features,
    ForecastAccumulator,
    rank_scale,
)

#: A relationship a person would recognise: trend positive, last month negative (reversal),
#: volatility negative, turnover carrying nothing at all, and the market level dominating.
TRUE_COEFFICIENTS: Final = (0.02, -0.03, 0.01, 0.015, -0.01, 0.008, 0.0, 0.005, 0.30)
TRUE_INTERCEPT: Final = 0.01


def _sample(rng: random.Random) -> tuple[Features, float]:
    """One synthetic pair from `TRUE_COEFFICIENTS`, with noise a real cross-section would have."""
    values = [rng.uniform(-1.0, 1.0) for _ in FEATURE_NAMES]
    values[-1] = rng.uniform(-0.15, 0.15)  # market_state is a level, not a rank
    target = (
        TRUE_INTERCEPT
        + sum(c * v for c, v in zip(TRUE_COEFFICIENTS, values, strict=True))
        + rng.gauss(0.0, 0.01)
    )
    return Features(*values), target


def _fitted(n: int, *, seed: int = 7) -> ForecastAccumulator:
    rng = random.Random(seed)
    accumulator = ForecastAccumulator()
    accumulator.extend(_sample(rng) for _ in range(n))
    return accumulator


# ── rank_scale ───────────────────────────────────────────────────────────────────────────────────


def test_a_missing_value_ranks_at_the_neutral_middle() -> None:
    """`None` becomes exactly 0 — no imputation, and no distributional assumption.

    This is why the features are ranks at all. A quarter of this universe has no earnings yield
    (loss-makers) and up to a third of the early history has no delivery share (the identity master
    could not resolve the symbol), and mean-imputing either inside a session would need the window's
    distribution, which a point-in-time fit cannot see.
    """
    # Three present values -> ranks 1, 2, 3 over n = 3 -> 2*r/(n+1) - 1 = -0.5, 0.0, +0.5. Exact
    # arithmetic on small integers, so these are equalities and not approximations.
    assert rank_scale([5.0, 1.0, None, 3.0]) == [0.5, -0.5, 0.0, 0.0]


def test_ties_share_their_average_rank() -> None:
    """Equal values score equally, so input order cannot become a ranking."""
    scores = rank_scale([2.0, 1.0, 2.0, 3.0])
    assert scores[0] == scores[2]
    assert scores[1] < scores[0] < scores[3]
    # Reversing the input reverses the output positionally and changes nothing else.
    assert rank_scale([3.0, 2.0, 1.0, 2.0]) == [scores[3], scores[0], scores[1], scores[0]]


def test_a_degenerate_cross_section_scores_zero() -> None:
    """Nothing to rank against — an empty session, or one lone name — is 0, never a guess."""
    assert rank_scale([]) == []
    assert rank_scale([None, None]) == [0.0, 0.0]
    assert rank_scale([4.0]) == [0.0]


# ── the fit ──────────────────────────────────────────────────────────────────────────────────────


def test_the_fit_recovers_a_relationship_that_is_there() -> None:
    """Coefficients and intercept come back close to the truth, ridge-shrunk toward zero.

    The tolerance is loose on purpose: this asserts the normal equations and the solve are right,
    not that a 4,000-point sample estimates a coefficient precisely. The one directional claim is
    the ridge's: a coefficient with real signal behind it comes back *slightly* smaller in magnitude
    than the truth. A truly-zero coefficient is exempt from that claim — there is nothing to shrink,
    so it is only asserted to be near zero, which is the more interesting property anyway: a feature
    that carries nothing is given nothing.
    """
    model = _fitted(4000).fit(horizon=21)

    assert model is not None
    assert model.observations == 4000
    assert model.dropped == ()
    assert abs(model.intercept - TRUE_INTERCEPT) < 0.002
    for (name, estimate), truth in zip(model.described(), TRUE_COEFFICIENTS, strict=True):
        assert abs(estimate - truth) < 0.02, name
        if truth == 0.0:
            assert abs(estimate) < 0.002, f"{name} carries nothing and should be given nothing"
        else:
            assert abs(estimate) <= abs(truth) + 1e-9, f"{name} was not shrunk toward zero"
    assert 0.0 < model.r_squared <= 1.0


def test_the_prediction_is_a_return_over_the_horizon() -> None:
    """`predict` returns a ratio in the target's units, which is what a cost bar compares against.

    The point of a fitted forecast over a rank: a name at the top of every feature should project a
    return of a size a person can compare to 0.3 % of friction, not a rank of 1.0.
    """
    model = _fitted(4000).fit(horizon=63)
    assert model is not None

    best = Features(1.0, -1.0, 1.0, 1.0, -1.0, 1.0, 1.0, 1.0, 0.10)
    worst = Features(-1.0, 1.0, -1.0, -1.0, 1.0, -1.0, -1.0, -1.0, -0.10)

    assert model.predict(best) > model.predict(worst)
    assert 0.0 < model.predict(best) < 1.0  # a plausible return, not a rank and not a price


def test_the_same_pairs_in_the_same_order_fit_bit_identically() -> None:
    """Two accumulators fed identically produce identical floats, not merely close ones.

    The reason the solve is hand-rolled: a replay must be byte-identical between the laptop and the
    server, and a LAPACK reduction can reorder across BLAS builds and thread counts. `==` on floats
    is the correct assertion here, not an approximation.
    """
    first = _fitted(2000).fit(horizon=21)
    second = _fitted(2000).fit(horizon=21)

    assert first is not None and second is not None
    assert first.coefficients == second.coefficients
    assert first.intercept == second.intercept
    assert first.r_squared == second.r_squared


# ── the three refusals ───────────────────────────────────────────────────────────────────────────


def test_too_little_data_is_no_model_rather_than_a_bad_one() -> None:
    """Below the observation floor `fit` returns None, so a policy holds no view.

    An expanding window necessarily starts empty, so this is the state every run passes through.
    Returning a ten-parameter fit of forty points would put a confident number in front of a policy
    at exactly the moment there is nothing to be confident about.
    """
    assert ForecastAccumulator().fit(horizon=21) is None
    assert _fitted(MIN_OBSERVATIONS - 1).fit(horizon=21) is None
    assert _fitted(MIN_OBSERVATIONS).fit(horizon=21) is not None


def test_a_constant_feature_is_dropped_and_named() -> None:
    """A feature with no variance carries a zero coefficient and appears in `dropped`.

    It happens for real: `deliv_ratio` is 0 for every name on a session where the identity master
    resolved nothing, and a rank of a single-valued cross-section is constant. Dividing by its zero
    standard deviation would be the alternative.
    """
    rng = random.Random(11)
    accumulator = ForecastAccumulator()
    for _ in range(1000):
        features, target = _sample(rng)
        values = list(features.as_tuple())
        values[6] = 0.0  # turnover_ratio, constant across the whole window
        accumulator.add(Features(*values), target)

    model = accumulator.fit(horizon=21)

    assert model is not None
    assert model.dropped == ("turnover_ratio",)
    assert model.coefficients[6] == 0.0
    assert model.coefficients[0] != 0.0  # the others were still fitted


def test_a_singular_system_is_refused_rather_than_fitted() -> None:
    """Two identical features with no ridge is singular, and the answer is None, not an artefact.

    With the default ridge this system is solvable and the two collinear features share the credit —
    which is what the ridge is for. Setting it to zero removes that protection, and the fit must
    then refuse rather than return whichever of the infinitely many solutions the elimination
    happened to reach.
    """
    rng = random.Random(13)
    accumulator = ForecastAccumulator()
    for _ in range(1000):
        features, target = _sample(rng)
        values = list(features.as_tuple())
        values[2] = values[0]  # mom_6 is now exactly mom_12_1
        accumulator.add(Features(*values), target)

    assert accumulator.fit(horizon=21, ridge=0.0) is None
    shared = accumulator.fit(horizon=21)
    assert shared is not None
    assert shared.coefficients[0] != 0.0 and shared.coefficients[2] != 0.0
