"""H1 — residual momentum: the 12-1 trend with the market's part taken out (X2, round 2).

Pre-registered in ``ops/studies/preregistration-signals-2026-09-29.md`` §3 before any evaluation,
and fixed there: this module implements that definition and has no parameter a run may move.

Plain 12-1 momentum ranks a name on its whole trailing return, so in a rising market it ranks the
high-beta names first and in a falling one the low-beta names — much of the leg is a bet on the
market's direction taken through each name's beta. Residual momentum ranks on the part of the
trailing return the market does not explain: regress the name's daily log returns on the NIFTY 50
TRI's over the trailing 252 sessions, sum the residuals over t-252 .. t-21 (the 12-1 span: the most
recent month is skipped, exactly as 12-1 skips it), and divide by the residual standard deviation
over the same span, so a steady idiosyncratic climb outranks a noisy one of the same size.

**The regression has no intercept.** The pre-registration names one regressor (the market) and a
regression window that *contains* the summed span. With an intercept, OLS residuals over the
252-session window sum to exactly zero, so the t-252 .. t-21 sum would be identically minus the
last 20 sessions' residuals — a one-month residual *reversal* signal wearing momentum's name, and
one that scores a steady idiosyncratic trend at about zero because the intercept absorbs the trend.
Through the origin, ``beta = sum(r * m) / sum(m * m)`` and the residual ``r - beta * m`` keeps the
name's own drift, which is the quantity the hypothesis is about.

**Sessions are the market's.** The window is the 252 published NIFTY 50 sessions strictly before
the decision session ``t``; a name's return on session ``s`` is ``ln(px_s / px_prev)`` against the
market session immediately before ``s``, and is *valid* only when the name printed on both. A name
with fewer than :data:`MIN_VALID_SESSIONS` valid sessions in the summed span is excluded from the
leg (``None``) — :func:`~backtest.policies.swing_composite.composite_scores` then gives it the
leg's mean rank rather than dropping it, so the candidate set is the baseline's.

Point-in-time (invariant #7): the window ends at t-1, so the decision session's own return never
enters the fit, and every market level used must carry a ``knowable_date`` on or before ``t`` —
:meth:`ResidualMomentumPanel.scores` raises rather than read one that does not.

**The H3 hook.** H3 is H1 + H2 and nothing else (pre-registration §3). H1's whole change to an arm
is :func:`with_residual_momentum`, a pure transform of the parameters, so H3 is that transform
applied to the H2 arm's parameters; the scores themselves come from :func:`residual_momentum` and
need no H2 input. Nothing here reads H2's band-hit data.

What it never does: read a clock or a store, fit anything to returns, or use a price it was not
handed (the caller supplies the seam-consistent adjusted path, never a raw close across a split).
"""

from __future__ import annotations

import math
from bisect import bisect_left
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from itertools import pairwise

from backtest.policies.swing_composite import SwingCompositeParameters

__all__ = [
    "MIN_VALID_SESSIONS",
    "REGRESSION_SESSIONS",
    "SKIP_SESSIONS",
    "MarketSession",
    "ResidualMomentumError",
    "ResidualMomentumPanel",
    "residual_momentum",
    "with_residual_momentum",
]

# The three H1 constants, fixed by ops/studies/preregistration-signals-2026-09-29.md §3 before any
# evaluation. They are not parameters: changing one is a new hypothesis with a new id there.
#: The regression window: the 252 sessions t-252 .. t-1.
REGRESSION_SESSIONS = 252
#: The most recent sessions fitted but not summed: the span is t-252 .. t-21, so t-20 .. t-1 drop.
SKIP_SESSIONS = 20
#: A name with fewer valid sessions than this in the summed span is excluded from the leg.
MIN_VALID_SESSIONS = 200

#: The summed span's length: t-252 .. t-21 inclusive.
_SPAN_SESSIONS = REGRESSION_SESSIONS - SKIP_SESSIONS

#: A floating-point floor, not a tunable: a residual stdev this small against the name's own return
#: scale is rounding noise (a name that is exactly beta x market), and its ratio would be noise over
#: noise. Such a name has no idiosyncratic return and scores 0.
_DEGENERATE = 1e-9

_QUANTUM = 8  # dp, as every other swing feature is quantised


class ResidualMomentumError(ValueError):
    """An input that would break point-in-time or the pre-registered window — never a fallback."""


def residual_momentum(
    stock: Sequence[float | None], market: Sequence[float | None]
) -> Decimal | None:
    """The pre-registered residual-momentum score from one window of aligned daily log returns.

    ``stock[i]`` and ``market[i]`` are the log returns of session ``t-252+i`` for ``i`` in
    0 .. 251, oldest first — exactly :data:`REGRESSION_SESSIONS` of each; a ``None`` (or non-finite)
    stock entry is a session the name did not print on both ends of. Fits ``stock = beta * market``
    through the origin over every valid session, then returns the residual sum over the first
    232 entries (t-252 .. t-21) over the residual sample stdev there, quantised to 8 dp.

    Returns ``None`` when the span has fewer than :data:`MIN_VALID_SESSIONS` valid sessions or the
    market did not move over the valid sessions (no beta is defined). Returns 0 for a name whose
    residuals are rounding noise. Raises :class:`ResidualMomentumError` on a window of the wrong
    length. Never reads a clock or a store.
    """
    if len(stock) != REGRESSION_SESSIONS or len(market) != REGRESSION_SESSIONS:
        raise ResidualMomentumError(
            f"a residual-momentum window is exactly {REGRESSION_SESSIONS} sessions (t-252 .. t-1), "
            f"got {len(stock)} stock and {len(market)} market returns"
        )
    pairs = [
        (i, r, m)
        for i, (r, m) in enumerate(zip(stock, market, strict=True))
        if r is not None and m is not None and math.isfinite(r) and math.isfinite(m)
    ]
    span = [(r, m) for i, r, m in pairs if i < _SPAN_SESSIONS]
    if len(span) < MIN_VALID_SESSIONS:
        return None
    market_sq = math.fsum(m * m for _, _, m in pairs)
    if market_sq == 0.0:
        return None
    beta = math.fsum(r * m for _, r, m in pairs) / market_sq
    residuals = [r - beta * m for r, m in span]
    k = len(residuals)
    total = math.fsum(residuals)
    mean = total / k
    stdev = math.sqrt(math.fsum((e - mean) ** 2 for e in residuals) / (k - 1))
    scale = math.sqrt(math.fsum(r * r for r, _ in span) / k)
    if stdev <= _DEGENERATE * scale:
        return Decimal(0)
    return Decimal(str(round(total / stdev, _QUANTUM)))


def with_residual_momentum(params: SwingCompositeParameters) -> SwingCompositeParameters:
    """``params`` with its 12-1 momentum leg replaced by residual momentum at the same weight (H1).

    The one change H1 makes to an arm, as a pure transform, so the H1 arm is this applied to the
    M10.7 default and H3 is this applied to the H2 arm's parameters. Every other knob — weights,
    screen, bands, stops, cadence, the regime gate — is carried unchanged.
    """
    return replace(
        params, weight_momentum=Decimal(0), weight_residual_momentum=params.weight_momentum
    )


@dataclass(frozen=True, slots=True)
class MarketSession:
    """One published market level: the session it is for, the level, and when it was knowable."""

    as_of: date
    level: Decimal
    knowable_date: date

    def __post_init__(self) -> None:
        if not isinstance(self.level, Decimal):
            raise TypeError("a market level is a Decimal")
        if self.level <= 0:
            raise ValueError(f"a market level must be positive, got {self.level} on {self.as_of}")


class ResidualMomentumPanel:
    """The residual-momentum cross-section for any decision session, from one market series.

    The session calendar is the market series' own. ``scores(as_of, closes)`` takes each name's
    signal closes by session (the seam-consistent adjusted path; ``None`` where it is undefined) and
    returns :func:`residual_momentum` per name over the 252 market sessions strictly before
    ``as_of``. Assumes ``sessions`` is the published series in ascending order.
    """

    def __init__(self, sessions: Sequence[MarketSession]) -> None:
        dates = [s.as_of for s in sessions]
        if dates != sorted(set(dates)):
            raise ResidualMomentumError("the market series must be strictly ascending by session")
        self._sessions = tuple(sessions)
        self._dates = tuple(dates)
        self._returns: tuple[float | None, ...] = (
            None,
            *(math.log(float(cur.level / prev.level)) for prev, cur in pairwise(self._sessions)),
        )

    def window(self, as_of: date) -> tuple[date, ...]:
        """The market sessions whose closes a score as of ``as_of`` reads: t-253 .. t-1.

        One more than the regression window, because the oldest return needs the close before it.
        Raises :class:`ResidualMomentumError` when the series has fewer sessions before ``as_of``
        or a level in the window was not knowable on ``as_of``.
        """
        end = bisect_left(self._dates, as_of)  # the first session on or after as_of is excluded
        start = end - REGRESSION_SESSIONS - 1
        if start < 0:
            raise ResidualMomentumError(
                f"the market series has {end} sessions before {as_of.isoformat()}, short of the "
                f"{REGRESSION_SESSIONS + 1} a residual-momentum window reads"
            )
        for session in self._sessions[start:end]:
            if session.as_of >= as_of or session.knowable_date > as_of:
                raise ResidualMomentumError(
                    f"market level for {session.as_of.isoformat()} is not knowable on "
                    f"{as_of.isoformat()}"
                )
        return self._dates[start:end]

    def scores(
        self, as_of: date, closes: Mapping[str, Mapping[date, float | None]]
    ) -> dict[str, Decimal | None]:
        """Each name's residual-momentum score as of ``as_of`` (``None`` where it is excluded).

        ``closes[isin]`` maps a session to the name's signal close; only the window's sessions are
        read, so a close dated ``as_of`` or later can never reach the score.
        """
        return {isin: self.score(as_of, by_session) for isin, by_session in closes.items()}

    def score(self, as_of: date, closes: Mapping[date, float | None]) -> Decimal | None:
        """One name's residual-momentum score as of ``as_of``, from its signal closes by session."""
        window = self.window(as_of)
        end = bisect_left(self._dates, as_of)
        market = list(self._returns[end - REGRESSION_SESSIONS : end])
        px = [closes.get(session) for session in window]
        stock: list[float | None] = []
        for prev, cur in pairwise(px):
            if prev is None or cur is None or prev <= 0 or cur <= 0:
                stock.append(None)
            else:
                stock.append(math.log(cur / prev))
        return residual_momentum(stock, market)
