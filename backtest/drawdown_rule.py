"""The drawdown-first decision rule (PE-drawdown study): five criteria, each PASS or FAIL.

``ops/studies/preregistration-pe-drawdown-2026-10-19.md`` §5 fixed the rule before any run. It is a
separate rule from :mod:`backtest.decision_rule` (round 2's return-first rule, which it does not
edit): that one asks whether an arm *earns more* than the baseline; this one asks whether an arm is
a better *low-drawdown* option than the baseline **and every comparator built to reach the same
lower risk without the arm's signal**. A candidate passes only if, on the fold test windows at the
deciding floor:

1. **ratio** — its pre-tax XIRR / max drawdown beats the baseline's and each comparator's,
   strictly, in at least :data:`MIN_WINDOWS_RATIO_BEATEN` of the windows — for every one of them;
2. **drawdown** — its worst max drawdown over the windows is at least :data:`MIN_DRAWDOWN_CUT`
   below the baseline's worst;
3. **cash hurdle** — its after-tax XIRR beats the after-tax return of holding the cash sleeve, in
   every window (strictly);
4. **return floor** — the arithmetic mean of its after-tax XIRRs is at least
   :data:`MIN_MEAN_AFTER_TAX_XIRR`;
5. **deflated ratio** — the probability that its XIRR/DD-type ratio (CAGR over max drawdown of its
   daily pre-tax returns, the windows laid end to end) beats the baseline's by more than the
   expected best of ``trials`` lucky trials is at least :data:`MIN_DEFLATED_RATIO`.

Criterion 5 is the deflated Sharpe ratio's construction (Bailey & López de Prado) carried over to
the metric this rule ranks on: the threshold is the baseline's ratio plus ``E[max]`` of ``trials``
zero-mean draws with the cross-trial variance of the ratio (``backtest.sharpe.expected_max_sharpe``
— the formula is the metric's, not the Sharpe's), and the probability is the share of stationary
block-bootstrap resamples of the candidate's returns whose ratio clears it. The ratio has no
closed-form sampling distribution, which is why it is bootstrapped; the seed is fixed, so the
figure is reproducible to the bit.

**Uncomputable is FAIL**, as in round 2: a missing after-tax figure, a zero drawdown, a variance
struck from fewer than :data:`MIN_VARIANCE_RATIOS` distinct trial ratios. Ratios compare as exact
``Decimal`` cross-products (no division rounds a boundary case); only criterion 5, a statistic,
runs in floats.

What it never does: choose the comparators or the trial count, rank arms, drop a window, or read
the robustness variants — those inform the study's verdict word (:func:`study_verdict`), never the
five criteria.
"""

from __future__ import annotations

import math
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from backtest.decision_rule import Criterion, Outcome
from backtest.sharpe import expected_max_sharpe

__all__ = [
    "BOOTSTRAP_BLOCK",
    "BOOTSTRAP_RESAMPLES",
    "BOOTSTRAP_SEED",
    "MIN_DEFLATED_RATIO",
    "MIN_DRAWDOWN_CUT",
    "MIN_MEAN_AFTER_TAX_XIRR",
    "MIN_VARIANCE_RATIOS",
    "MIN_WINDOWS_RATIO_BEATEN",
    "ArmWindows",
    "DrawdownVerdict",
    "RatioVariance",
    "StudyVerdict",
    "TrialRatio",
    "WindowFigures",
    "beats_on_ratio",
    "calmar",
    "deflated_ratio",
    "evaluate_drawdown_first",
    "ratio_variance",
    "robust_pass",
    "study_verdict",
]

#: Criterion 1: windows (of the fold test windows) in which the ratio must strictly beat each rival.
MIN_WINDOWS_RATIO_BEATEN = 2
#: Criterion 2: how far the candidate's worst max drawdown must sit below the baseline's (5 pp).
MIN_DRAWDOWN_CUT = Decimal("0.050")
#: Criterion 4: the least mean after-tax XIRR over the windows (10 %).
MIN_MEAN_AFTER_TAX_XIRR = Decimal("0.100")
#: Criterion 5: the deflated ratio's probability bar.
MIN_DEFLATED_RATIO = 0.95
#: Criterion 5: the fewest distinct trial ratios the cross-trial variance may be struck from.
MIN_VARIANCE_RATIOS = 20
#: Criterion 5: stationary-bootstrap mean block length (sessions — about a month), resamples, seed.
BOOTSTRAP_BLOCK = 21
BOOTSTRAP_RESAMPLES = 2000
BOOTSTRAP_SEED = 20261019


@dataclass(frozen=True, slots=True)
class WindowFigures:
    """One arm on one window: the figures the rule reads."""

    window: str
    pre_tax_xirr: Decimal
    max_drawdown: Decimal
    #: ``None`` when it could not be struck; ``error`` then says why.
    after_tax_xirr: Decimal | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ArmWindows:
    """One arm on every decision window, in window order, and its daily pre-tax returns.

    ``pre_tax_returns`` are the windows' daily pre-tax NAV returns laid end to end (no return
    across a seam), ``years`` their summed calendar span (Actual/365) — criterion 5's inputs.
    """

    label: str
    windows: tuple[WindowFigures, ...]
    pre_tax_returns: tuple[float, ...] = ()
    years: float = 0.0

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(w.window for w in self.windows)


@dataclass(frozen=True, slots=True)
class TrialRatio:
    """One trial's criterion-5 ratio (CAGR / max DD of its concatenated daily returns)."""

    label: str
    ratio: float


@dataclass(frozen=True, slots=True)
class RatioVariance:
    """The cross-trial variance of the ratio criterion 5 deflates with, and where it came from."""

    value: float | None
    source: str
    ratios: tuple[TrialRatio, ...]
    excluded: tuple[str, ...] = ()

    @property
    def n(self) -> int:
        return len(self.ratios)

    @property
    def sufficient(self) -> bool:
        return self.value is not None and self.n >= MIN_VARIANCE_RATIOS

    def describe(self) -> str:
        value = "undefined" if self.value is None else f"{self.value:.6e}"
        return f"{value} from {self.n} distinct trial ratios ({self.source})"


@dataclass(frozen=True, slots=True)
class DrawdownVerdict:
    """The candidate against the baseline and the comparators: five criteria."""

    candidate: str
    baseline: str
    comparators: tuple[str, ...]
    criteria: tuple[Criterion, ...]

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.criteria)


class StudyVerdict(StrEnum):
    """The study's word (pre-registration §5): fixed before any run."""

    ADOPT = "ADOPT-AS-DEFENSIVE-VARIANT"
    NOT_ROBUST = "NOT ROBUST"
    REJECT = "REJECT"


def _pct(value: Decimal | None) -> str:
    return "n/a" if value is None else f"{value:.2%}"


def _ratio_text(w: WindowFigures) -> str:
    if w.max_drawdown <= 0:
        return "n/a (no drawdown)"
    return f"{w.pre_tax_xirr / w.max_drawdown:.3f}"


def beats_on_ratio(candidate: WindowFigures, rival: WindowFigures) -> bool | None:
    """Whether ``candidate``'s XIRR/DD is strictly above ``rival``'s; ``None`` when undefined.

    Compared as the cross-product ``x_c * dd_r > x_r * dd_c`` (both drawdowns positive), so no
    division rounds a tie into a win. A negative XIRR is a negative ratio, compared as such.
    """
    if candidate.max_drawdown <= 0 or rival.max_drawdown <= 0:
        return None
    return candidate.pre_tax_xirr * rival.max_drawdown > rival.pre_tax_xirr * candidate.max_drawdown


def _check_windows(candidate: ArmWindows, others: Sequence[ArmWindows]) -> None:
    for other in others:
        if other.names != candidate.names:
            raise ValueError(
                f"{candidate.label} and {other.label} were not run on the same windows"
            )
    if not candidate.windows:
        raise ValueError("no windows to evaluate")


def robust_pass(
    candidate: ArmWindows, baseline: ArmWindows, rivals: Sequence[ArmWindows]
) -> tuple[bool, str]:
    """Criteria 1 and 2 only — what a robustness variant must pass (pre-registration §6)."""
    one = _ratio_criterion(candidate, baseline, rivals)
    two = _drawdown_criterion(candidate, baseline)
    return one.passed and two.passed, f"ratio {one.outcome.value}; drawdown {two.outcome.value}"


def _ratio_criterion(
    candidate: ArmWindows, baseline: ArmWindows, comparators: Sequence[ArmWindows]
) -> Criterion:
    rule = (
        f"XIRR/DD strictly beats the baseline's and every comparator's in at least "
        f"{MIN_WINDOWS_RATIO_BEATEN} of {len(candidate.windows)} windows"
    )
    parts: list[str] = []
    failed: list[str] = []
    for rival in (baseline, *comparators):
        won: list[str] = []
        undefined: list[str] = []
        for c, r in zip(candidate.windows, rival.windows, strict=True):
            beat = beats_on_ratio(c, r)
            if beat is None:
                undefined.append(c.window)
            elif beat:
                won.append(c.window)
        cells = ", ".join(
            f"{c.window} {_ratio_text(c)} vs {_ratio_text(r)}"
            for c, r in zip(candidate.windows, rival.windows, strict=True)
        )
        ok = len(won) >= MIN_WINDOWS_RATIO_BEATEN and not undefined
        if not ok:
            failed.append(rival.label)
        note = f"; undefined on {', '.join(undefined)}" if undefined else ""
        parts.append(f"vs {rival.label}: beaten in {len(won)} ({cells}){note}")
    outcome = Outcome.FAIL if failed else Outcome.PASS
    detail = "; ".join(parts) + (
        f" — not beaten often enough: {', '.join(failed)}" if failed else ""
    )
    return Criterion(1, rule, outcome, detail, candidate.names)


def _drawdown_criterion(candidate: ArmWindows, baseline: ArmWindows) -> Criterion:
    worst_c = max(w.max_drawdown for w in candidate.windows)
    worst_b = max(w.max_drawdown for w in baseline.windows)
    cut = worst_b - worst_c
    return Criterion(
        2,
        f"worst max drawdown at least {MIN_DRAWDOWN_CUT * 100:.1f} pp below the baseline's worst",
        Outcome.PASS if cut >= MIN_DRAWDOWN_CUT else Outcome.FAIL,
        f"worst {_pct(worst_c)} vs baseline {_pct(worst_b)}: {cut * 100:+.2f} pp lower "
        f"(bar {MIN_DRAWDOWN_CUT * 100:.1f} pp)",
        candidate.names,
    )


def _cash_criterion(candidate: ArmWindows, hurdle: Mapping[str, Decimal]) -> Criterion:
    rule = "after-tax XIRR above the cash sleeve's after-tax return in every window"
    cells: list[str] = []
    failed: list[str] = []
    for w in candidate.windows:
        bar = hurdle.get(w.window)
        if w.after_tax_xirr is None or bar is None:
            failed.append(w.window)
            why = w.error or ("no after-tax figure" if bar is not None else "no cash hurdle")
            cells.append(f"{w.window} not computable ({why})")
            continue
        if not w.after_tax_xirr > bar:
            failed.append(w.window)
        cells.append(f"{w.window} {_pct(w.after_tax_xirr)} vs cash {_pct(bar)}")
    detail = ", ".join(cells) + (f"; fails in {', '.join(failed)}" if failed else "")
    return Criterion(3, rule, Outcome.FAIL if failed else Outcome.PASS, detail, candidate.names)


def _floor_criterion(candidate: ArmWindows) -> Criterion:
    rule = f"mean after-tax XIRR at least {MIN_MEAN_AFTER_TAX_XIRR:.1%}"
    missing = [w.window for w in candidate.windows if w.after_tax_xirr is None]
    if missing:
        return Criterion(4, rule, Outcome.FAIL, f"FAIL: not computable on {', '.join(missing)}", ())
    values = [w.after_tax_xirr for w in candidate.windows if w.after_tax_xirr is not None]
    total = sum(values, Decimal(0))
    n = Decimal(len(values))
    return Criterion(
        4,
        rule,
        Outcome.PASS if total >= MIN_MEAN_AFTER_TAX_XIRR * n else Outcome.FAIL,
        f"arithmetic mean {_pct(total / n)} (bar {MIN_MEAN_AFTER_TAX_XIRR:.1%})",
        candidate.names,
    )


def calmar(returns: Sequence[float], *, years: float) -> float:
    """CAGR over max drawdown of the path ``returns`` compound to, over ``years`` years.

    Raises ``ValueError`` for an empty series, a non-positive span, or a path with no drawdown
    (the ratio is then undefined, not infinite).
    """
    if not returns or years <= 0:
        raise ValueError("the ratio needs returns over a positive span")
    level = peak = 1.0
    worst = 0.0
    for r in returns:
        level *= 1.0 + r
        if level > peak:
            peak = level
        else:
            worst = max(worst, 1.0 - level / peak)
    if worst <= 0.0:
        raise ValueError("the path has no drawdown; its ratio is undefined")
    if level <= 0.0:
        raise ValueError("the path loses everything; its ratio is undefined")
    growth: float = level ** (1.0 / years)
    return (growth - 1.0) / worst


def _stationary_resample(returns: Sequence[float], rng: random.Random, block: int) -> list[float]:
    """One Politis-Romano stationary-bootstrap resample (geometric blocks, wrapping)."""
    n = len(returns)
    out: list[float] = []
    p = 1.0 / block
    i = rng.randrange(n)
    while len(out) < n:
        out.append(returns[i])
        i = rng.randrange(n) if rng.random() < p else (i + 1) % n
    return out


def deflated_ratio(
    returns: Sequence[float],
    *,
    years: float,
    benchmark: float,
    trials: int,
    variance: float,
    resamples: int = BOOTSTRAP_RESAMPLES,
    block: int = BOOTSTRAP_BLOCK,
    seed: int = BOOTSTRAP_SEED,
) -> tuple[float, float]:
    """``(probability, threshold)``: P(ratio > benchmark + E[max of trials]) by bootstrap.

    The threshold is ``benchmark + sqrt(variance) * spread(trials)`` (``expected_max_sharpe``'s
    formula, applied to this ratio's cross-trial variance). The probability is the share of
    ``resamples`` stationary-bootstrap resamples whose ratio exceeds it; a resample with no
    drawdown counts as exceeding it (its ratio is unbounded above), one that loses everything as
    not. Deterministic for a given ``seed``.
    """
    threshold = benchmark + expected_max_sharpe(trials=trials, sharpe_variance=variance)
    rng = random.Random(seed)
    above = 0
    for _ in range(resamples):
        sample = _stationary_resample(returns, rng, block)
        try:
            value = calmar(sample, years=years)
        except ValueError as error:
            if "no drawdown" in str(error):
                above += 1
            continue
        if value > threshold:
            above += 1
    return above / resamples, threshold


def ratio_variance(trials: Sequence[TrialRatio], *, source: str) -> RatioVariance:
    """The sample variance (ddof = 1) of the distinct trial ratios, with what was left out.

    A label seen twice or a ratio equal to one already counted is the same trial under two names
    and counts once, as in ``backtest.decision_rule.trial_sharpe_variance``.
    """
    counted: list[TrialRatio] = []
    excluded: list[str] = []
    for trial in trials:
        if not math.isfinite(trial.ratio):
            excluded.append(f"{trial.label}: ratio {trial.ratio} is not finite")
            continue
        dup = next(
            (
                seen
                for seen in counted
                if seen.label == trial.label
                or math.isclose(seen.ratio, trial.ratio, rel_tol=0.0, abs_tol=1e-12)
            ),
            None,
        )
        if dup is not None:
            excluded.append(f"{trial.label}: same as {dup.label}")
            continue
        counted.append(trial)
    value: float | None = None
    if len(counted) >= 2:
        mean = math.fsum(t.ratio for t in counted) / len(counted)
        value = math.fsum((t.ratio - mean) ** 2 for t in counted) / (len(counted) - 1)
    return RatioVariance(value, source, tuple(counted), tuple(excluded))


def _deflated_criterion(
    candidate: ArmWindows, baseline: ArmWindows, *, trials: int, variance: RatioVariance
) -> Criterion:
    rule = (
        f"deflated XIRR/DD ratio vs the baseline's, {trials} trials, gives probability "
        f">= {MIN_DEFLATED_RATIO:.2f}"
    )
    source = f"ratio variance {variance.describe()}"
    if not variance.sufficient or variance.value is None:
        return Criterion(
            5,
            rule,
            Outcome.FAIL,
            f"FAIL: the variance is struck from {variance.n} distinct trial ratios, fewer than "
            f"{MIN_VARIANCE_RATIOS}; {source}",
            (),
        )
    try:
        observed = calmar(candidate.pre_tax_returns, years=candidate.years)
        bench = calmar(baseline.pre_tax_returns, years=baseline.years)
        probability, threshold = deflated_ratio(
            candidate.pre_tax_returns,
            years=candidate.years,
            benchmark=bench,
            trials=trials,
            variance=variance.value,
        )
    except ValueError as error:
        return Criterion(5, rule, Outcome.FAIL, f"FAIL: not computable — {error}; {source}", ())
    return Criterion(
        5,
        rule,
        Outcome.PASS if probability >= MIN_DEFLATED_RATIO else Outcome.FAIL,
        f"probability {probability:.4f}; ratio {observed:.4f} vs baseline {bench:.4f}, threshold "
        f"{threshold:.4f} ({BOOTSTRAP_RESAMPLES} stationary-bootstrap resamples, mean block "
        f"{BOOTSTRAP_BLOCK} sessions, seed {BOOTSTRAP_SEED}); {source}",
        candidate.names,
    )


def evaluate_drawdown_first(
    candidate: ArmWindows,
    baseline: ArmWindows,
    comparators: Sequence[ArmWindows],
    *,
    cash_hurdle: Mapping[str, Decimal],
    trials: int,
    variance: RatioVariance,
) -> DrawdownVerdict:
    """The five criteria for ``candidate`` against ``baseline`` and ``comparators``.

    Assumes every arm carries the same windows in the same order (``ValueError`` otherwise) and
    that ``cash_hurdle`` maps each window to the cash sleeve's after-tax return there. ``trials``
    is the recorded trial count (no default).
    """
    _check_windows(candidate, (baseline, *comparators))
    if trials < 1:
        raise ValueError(f"the trial count must be positive, got {trials}")
    return DrawdownVerdict(
        candidate=candidate.label,
        baseline=baseline.label,
        comparators=tuple(c.label for c in comparators),
        criteria=(
            _ratio_criterion(candidate, baseline, comparators),
            _drawdown_criterion(candidate, baseline),
            _cash_criterion(candidate, cash_hurdle),
            _floor_criterion(candidate),
            _deflated_criterion(candidate, baseline, trials=trials, variance=variance),
        ),
    )


def study_verdict(main: DrawdownVerdict, robustness: Sequence[tuple[str, bool]]) -> StudyVerdict:
    """REJECT on any failed criterion; else NOT ROBUST on a failed robustness check; else ADOPT."""
    if not main.passed:
        return StudyVerdict.REJECT
    if not all(ok for _, ok in robustness):
        return StudyVerdict.NOT_ROBUST
    return StudyVerdict.ADOPT
