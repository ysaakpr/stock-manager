"""A point-in-time forward-return forecast: features, an expanding-window fit, a prediction (X2).

Every policy in this repo so far *ranks* — 12-1 momentum, earnings yield, a composite of three
signals. A rank answers "which name", never "how much", and "how much" is the question a daily
policy has to answer, because the thing a trade must clear is a number: about 0.29 % of position
value the round trip (0.2542 % statutory off `execution/costs/rates.yaml` plus ~2 bps of slippage
each way). Deciding every session instead of every month multiplies the chances to pay that without
multiplying the edge, so a daily policy needs an estimate in *return units* to compare against it.
This module produces one: an expected return over the next ``horizon`` sessions, fitted rather than
asserted.

**The fit is an expanding window, and that is what makes it point-in-time.** A pair (features at
`t`, realized return over `[t, t + horizon]`) may enter the fit only once its target window has
*closed* on or before the decision session — otherwise the coefficients would be told the future the
policy is being asked to predict, which is invariant #7 broken in the one place it is hardest to
see. So at each session the model is refitted on every pair matured by then and no other:
`t + horizon <= session`. The consequence is honest and worth stating plainly — the first
``horizon`` sessions of any window have no model at all, and the early years have a model fitted on
very little, so a run's opening period is weaker by construction rather than by accident.

**Name-level features are cross-sectional ranks; market state is a level.** Each name-level feature
is converted to its rank scaled onto ``[-1, +1]`` (:func:`rank_scale`) rather than a z-score,
because every one of these distributions is fat-tailed — one name at a 400 % trailing return would
otherwise dominate the fit — and because a rank makes a missing value expressible as exactly 0, the
neutral middle, with no imputation and no distributional assumption. Market state cannot be a rank:
it has no cross-section (every name shares it on a session), so a rank would be constant and carry
nothing. It enters as a level, which is also what lets the intercept plus that term act as the
model's view of *when*, not just *which*.

**The solve is pure Python on purpose.** Normal equations accumulated in a fixed order, then
Gaussian elimination with partial pivoting on a 10x10 system. Not numpy: a replay must be
byte-identical between the laptop and the server (§8.3.3), and a LAPACK path can reorder a reduction
across BLAS builds and thread counts. Nine features and one intercept make the hand solve trivially
cheap, and accumulating `XᵀX` and `Xᵀy` incrementally means the expanding window costs one `add`
per newly-matured pair and one small solve per session, not a refit over the whole history.

**Floats here, Decimal at the money boundary.** A forecast is a statistic, not a cash amount, and
the accumulation of millions of cross-products is exactly what `Decimal` is wrong for. Nothing in
this module is a price, a quantity, a cost or a P&L: it takes ratios in, returns a ratio out, and
:class:`~backtest.policies.forecast_daily.ForecastRecord` quantises that ratio to `Decimal` at the
seam where a decision is made against money (CLAUDE.md, invariant on money).

What it never does: read a clock, touch a lake, hold a cost model, or see a price it was not handed.
The look-back lengths are constants here so the extraction that feeds it and the definition it
implements cannot drift apart.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

__all__ = [
    "FEATURE_NAMES",
    "HORIZON_1M",
    "HORIZON_3M",
    "LOOKBACK_1M",
    "LOOKBACK_6M",
    "LOOKBACK_12M",
    "LOOKBACK_52W_HIGH",
    "RIDGE",
    "VOL_WINDOW",
    "Features",
    "ForecastAccumulator",
    "ForecastModel",
    "rank_scale",
]

# ── a-priori constants: stated once, never tuned ─────────────────────────────────────────────────
#: Trading sessions in a year, a month, six months. Round, conventional, and shared with the
#: extraction that feeds this module so a feature's definition lives in one place.
LOOKBACK_12M: Final = 252
LOOKBACK_6M: Final = 126
LOOKBACK_1M: Final = 21
#: The 52-week-high window the proximity feature measures against.
LOOKBACK_52W_HIGH: Final = 252
#: Sessions of daily returns the realised-volatility feature is struck over (a quarter).
VOL_WINDOW: Final = 63
#: The two forecast horizons — "next one to three months" in sessions.
HORIZON_1M: Final = 21
HORIZON_3M: Final = 63
#: Ridge penalty, applied in *correlation* space so it is scale-free and one number serves every
#: feature. 0.05 is a light touch: enough to keep two collinear momentum features from trading
#: enormous opposite coefficients, far too small to shrink a real signal away.
RIDGE: Final = 0.05
#: Below this many matured pairs there is no model. 500 is a round floor at which a ten-parameter
#: fit is not fitting noise; before it, `fit` returns None and a policy holds no view.
MIN_OBSERVATIONS: Final = 500

#: The feature vector, in the one order every part of this module uses. Name-level features are
#: ranks on [-1, +1]; `market_state` is a level.
FEATURE_NAMES: Final = (
    "mom_12_1",
    "mom_1",
    "mom_6",
    "high_prox",
    "vol_63",
    "deliv_ratio",
    "turnover_ratio",
    "earnings_yield",
    "market_state",
)
_N_FEATURES: Final = len(FEATURE_NAMES)


def rank_scale(values: Sequence[float | None]) -> list[float]:
    """Cross-sectional ranks of `values` scaled onto ``[-1, +1]``; ``None`` becomes exactly 0.

    ``2 * rank / (n + 1) - 1`` over the *present* values, so the lowest sits near -1, the highest
    near +1 and the median at 0. Ties take their average rank, so two identical values cannot be
    ordered by an accident of input order and the result is deterministic.

    ``None`` — a loss-maker with no earnings yield, a name whose symbol the identity master could
    not resolve so it has no delivery share — maps to 0, the neutral middle. That is the whole
    reason the features are ranks: a missing value is expressible without imputing a mean, which
    would need the window's distribution and so could not be done point-in-time inside a session.

    Returns a list positionally aligned with `values`.
    """
    present = sorted((value, index) for index, value in enumerate(values) if value is not None)
    out = [0.0] * len(values)
    n = len(present)
    if n == 0:
        return out
    if n == 1:
        out[present[0][1]] = 0.0  # a lone value has no cross-section to rank against
        return out
    # Average rank within each tie group, so equal values get equal scores.
    start = 0
    while start < n:
        end = start
        while end + 1 < n and present[end + 1][0] == present[start][0]:
            end += 1
        # Ranks are 1-based; the group spans [start, end] inclusive.
        mean_rank = (start + end) / 2.0 + 1.0
        scaled = 2.0 * mean_rank / (n + 1.0) - 1.0
        for position in range(start, end + 1):
            out[present[position][1]] = scaled
        start = end + 1
    return out


@dataclass(frozen=True, slots=True)
class Features:
    """One name's feature vector on one session — ranks, plus the market's level.

    What each field is, and the sign a person would expect before any fit is run (the fit is free to
    disagree, and where it does the report says so):

    * ``mom_12_1`` — rank of the `t-252 .. t-21` return, the classical trend, skipping the
      short-term-reversal month. Expected positive.
    * ``mom_1`` — rank of the last 21 sessions' return. Expected *negative*: short-horizon
      reversal is the best-documented effect at this frequency.
    * ``mom_6`` — rank of the `t-126 .. t` return. Correlated with ``mom_12_1`` on purpose; the
      ridge exists so the two can share credit instead of fighting over it.
    * ``high_prox`` — rank of `close / 252-session high`. Expected positive.
    * ``vol_63`` — rank of annualised realised volatility over 63 daily returns. Expected
      negative (the low-volatility effect), and it is the feature most likely to be a proxy for
      size in this universe.
    * ``deliv_ratio`` — rank of the 5-session mean delivery share over its 63-session mean:
      accumulation that intends to hold overnight rather than intraday churn. Available only where
      the identity master resolved the symbol, and its coverage is 64.6 % in 2016 rising to 85.8 %
      in 2026 (`ops/gates/delivery-rebuild-2026-09-07.md`) — the unresolved names are
      disproportionately those later renamed or delisted, so this feature is thinner and
      survivor-tilted in the early history. `None` where absent.
    * ``turnover_ratio`` — rank of session turnover over its 63-session mean: today's interest
      against the name's own normal.
    * ``earnings_yield`` — rank of TTM earnings over market cap, read point-in-time off the M10.4
      store. `None` for a loss-maker or a name with no knowable filing.
    * ``market_state`` — `index level / its 200-session moving average - 1`, a *level* and not a
      rank, because it is the same for every name on a session. This is the term through which the
      model can say the next month is worse for everything, which no cross-sectional rank can.

    A `None` field is a stated absence, never a zero: `rank_scale` maps it to the neutral middle.
    """

    mom_12_1: float
    mom_1: float
    mom_6: float
    high_prox: float
    vol_63: float
    deliv_ratio: float
    turnover_ratio: float
    earnings_yield: float
    market_state: float

    def as_tuple(self) -> tuple[float, ...]:
        """The vector in `FEATURE_NAMES` order — the one order the fit and the predict share."""
        return (
            self.mom_12_1,
            self.mom_1,
            self.mom_6,
            self.high_prox,
            self.vol_63,
            self.deliv_ratio,
            self.turnover_ratio,
            self.earnings_yield,
            self.market_state,
        )


@dataclass(frozen=True, slots=True)
class ForecastModel:
    """Solved coefficients and the prediction they make — a fit as of one session.

    `observations` is how many matured pairs the fit saw, `dropped` names any feature that was
    constant across the window and therefore carries a zero coefficient rather than an arbitrary
    one, and `r_squared` is *in-sample* — it says how much of the past this fit explains, which is
    an upper bound on what it will do next and must never be reported as predictive accuracy.

    `predict` returns an expected return over the model's horizon as a plain ratio: 0.031 is
    +3.1 % over the next `horizon` sessions.
    """

    horizon: int
    coefficients: tuple[float, ...]
    intercept: float
    observations: int
    r_squared: float
    dropped: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if len(self.coefficients) != _N_FEATURES:
            raise ValueError(
                f"expected {_N_FEATURES} coefficients for {_N_FEATURES} features, "
                f"got {len(self.coefficients)}"
            )
        if self.horizon <= 0:
            raise ValueError(f"horizon must be positive, got {self.horizon}")

    def predict(self, features: Features) -> float:
        """Expected return over `horizon` sessions for one name, as a ratio."""
        total = self.intercept
        for coefficient, value in zip(self.coefficients, features.as_tuple(), strict=True):
            total += coefficient * value
        return total

    def described(self) -> tuple[tuple[str, float], ...]:
        """`(feature, coefficient)` pairs in feature order — what the report prints."""
        return tuple(zip(FEATURE_NAMES, self.coefficients, strict=True))


class ForecastAccumulator:
    """Normal equations for one horizon, accumulated pair by pair as targets mature.

    Holds `n`, `Σx`, `Σxxᵀ`, `Σy`, `Σxy` and `Σy²` — every moment the fit needs — so a pair can be
    added the session its target window closes and never revisited. `fit` centres and scales from
    those moments, applies :data:`RIDGE` in correlation space, solves, and maps the coefficients
    back to raw feature units.

    Determinism: pairs are added in the caller's order and summed in that order, in float64, so two
    machines that add the same pairs in the same order hold bit-identical sums. That is what makes a
    replay's journal digest reproducible across the laptop and the server, and it is why this is not
    a numpy least-squares call.
    """

    __slots__ = ("_n", "_sum_x", "_sum_xx", "_sum_xy", "_sum_y", "_sum_yy")

    def __init__(self) -> None:
        self._n = 0
        self._sum_x = [0.0] * _N_FEATURES
        self._sum_xx = [[0.0] * _N_FEATURES for _ in range(_N_FEATURES)]
        self._sum_y = 0.0
        self._sum_xy = [0.0] * _N_FEATURES
        self._sum_yy = 0.0

    @property
    def observations(self) -> int:
        """Matured pairs added so far."""
        return self._n

    def add(self, features: Features, target: float) -> None:
        """Add one matured pair: this name's features at `t`, its realised return to `t + horizon`.

        The caller owns the point-in-time rule — only pairs whose target window has closed on or
        before the decision session may be added. This method cannot check that: it never sees a
        date. The rule is enforced where the dates are, and stated in the module docstring.
        """
        x = features.as_tuple()
        self._n += 1
        self._sum_y += target
        self._sum_yy += target * target
        for i in range(_N_FEATURES):
            xi = x[i]
            self._sum_x[i] += xi
            self._sum_xy[i] += xi * target
            row = self._sum_xx[i]
            for j in range(i, _N_FEATURES):
                row[j] += xi * x[j]

    def extend(self, pairs: Iterable[tuple[Features, float]]) -> None:
        """Add many pairs, in the order given."""
        for features, target in pairs:
            self.add(features, target)

    def fit(
        self, *, horizon: int, ridge: float = RIDGE, minimum: int = MIN_OBSERVATIONS
    ) -> ForecastModel | None:
        """Solve for the coefficients as of now, or return `None` while there is too little data.

        `None` is a real answer and not a failure: before `minimum` pairs have matured there is no
        model, and a policy that holds no view should hold no position rather than act on a fit of
        forty points. The caller reports how often that happened.
        """
        if self._n < minimum:
            return None
        n = float(self._n)
        mean_x = [total / n for total in self._sum_x]
        mean_y = self._sum_y / n
        # Covariance from the raw moments; the upper triangle was accumulated, mirror it.
        cov = [[0.0] * _N_FEATURES for _ in range(_N_FEATURES)]
        for i in range(_N_FEATURES):
            for j in range(i, _N_FEATURES):
                value = self._sum_xx[i][j] / n - mean_x[i] * mean_x[j]
                cov[i][j] = value
                cov[j][i] = value
        sd = [math.sqrt(cov[i][i]) if cov[i][i] > 0.0 else 0.0 for i in range(_N_FEATURES)]
        live = [i for i in range(_N_FEATURES) if sd[i] > 0.0]
        dropped = tuple(FEATURE_NAMES[i] for i in range(_N_FEATURES) if sd[i] <= 0.0)
        if not live:
            return None  # every feature constant: there is nothing to fit

        # Correlation matrix and the standardised covariance with y, over the live features only.
        size = len(live)
        matrix = [[0.0] * size for _ in range(size)]
        rhs = [0.0] * size
        for a, i in enumerate(live):
            for b, j in enumerate(live):
                matrix[a][b] = cov[i][j] / (sd[i] * sd[j])
            matrix[a][a] += ridge
            rhs[a] = (self._sum_xy[i] / n - mean_x[i] * mean_y) / sd[i]

        solved = _solve(matrix, rhs)
        if solved is None:
            return None  # singular even with the ridge: refuse rather than return a fitted artefact

        coefficients = [0.0] * _N_FEATURES
        for a, i in enumerate(live):
            coefficients[i] = solved[a] / sd[i]
        intercept = mean_y - sum(coefficients[i] * mean_x[i] for i in range(_N_FEATURES))

        var_y = self._sum_yy / n - mean_y * mean_y
        explained = sum(
            coefficients[i] * (self._sum_xy[i] / n - mean_x[i] * mean_y) for i in range(_N_FEATURES)
        )
        r_squared = explained / var_y if var_y > 0.0 else 0.0
        return ForecastModel(
            horizon=horizon,
            coefficients=tuple(coefficients),
            intercept=intercept,
            observations=self._n,
            r_squared=r_squared,
            dropped=dropped,
        )


def _solve(matrix: list[list[float]], rhs: list[float]) -> list[float] | None:
    """Gaussian elimination with partial pivoting. `None` if the system is singular.

    Deterministic by construction: a fixed elimination order, a pivot chosen by magnitude with the
    lowest index winning a tie, and no library reduction whose order could differ between machines.
    Mutates copies, never the caller's lists.
    """
    size = len(rhs)
    a = [row[:] for row in matrix]
    b = rhs[:]
    for column in range(size):
        pivot = column
        best = abs(a[column][column])
        for row in range(column + 1, size):
            candidate = abs(a[row][column])
            if candidate > best:
                best, pivot = candidate, row
        if best <= 1e-12:
            return None
        if pivot != column:
            a[column], a[pivot] = a[pivot], a[column]
            b[column], b[pivot] = b[pivot], b[column]
        inverse = 1.0 / a[column][column]
        for row in range(column + 1, size):
            factor = a[row][column] * inverse
            if factor == 0.0:
                continue
            for k in range(column, size):
                a[row][k] -= factor * a[column][k]
            b[row] -= factor * b[column]
    out = [0.0] * size
    for column in range(size - 1, -1, -1):
        total = b[column]
        for k in range(column + 1, size):
            total -= a[column][k] * out[k]
        out[column] = total / a[column][column]
    return out
