"""X2: the round-2 decision rule — four criteria, each PASS or FAIL, against a frozen baseline.

``ops/studies/preregistration-signals-2026-09-29.md`` §4 fixed the rule before any evaluation. An
H-arm is **kept** only if all four hold against the named frozen baseline arm, at the ₹10 crore
floor, on the fold test windows (``backtest.folds``):

1. its mean after-tax XIRR across the test windows improves on the baseline's by **>= 1.0 pp**;
2. it improves in **at least 2 of the 3** test windows (strictly: a tie is not an improvement);
3. its max drawdown in **no** test window is worse than the baseline's by **more than 3.0 pp**
   (exactly 3.0 pp passes);
4. the deflated Sharpe ratio (``backtest.sharpe``) of its daily after-tax NAV returns over the
   concatenated test windows, deflated for the recorded trial count and measured from the
   baseline's Sharpe on the same basis, is **>= 0.95**.

**The thresholds are constants here and nowhere else**, and every criterion prints its own
figures beside its verdict, so a reader can check the arithmetic rather than trust the word PASS.

**Uncomputable is FAIL, never PASS.** A fold whose after-tax XIRR could not be struck (a missing
grandfathering price, an unsolvable stream) fails criteria 1 and 2 with the reason; a return series
the Sharpe statistics are undefined on fails criterion 4. A verdict that passed an arm because a
number was missing would be the one error this rule exists to prevent.

**Which figures.** After-tax XIRR is the realised-gains figure (``after_tax_xirr_realised``: 30 %
slab, no surcharge, tax paid at FY end, cash interest on — the pre-registration's metric). Max
drawdown is each run's own, struck from its pre-tax NAV path, the figure every report here carries.
Concatenated returns are each test window's daily returns laid end to end — no return is computed
across a seam between two windows, because the two NAVs belong to different runs.

What this module never does: choose the baseline (the caller names it), default the trial count,
rank arms, or round a figure before comparing it — XIRR and drawdown compare as exact ``Decimal``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from backtest.sharpe import SharpeStats, deflated_sharpe_ratio, sharpe_stats, sharpe_variance

__all__ = [
    "MAX_DRAWDOWN_WORSENING",
    "MIN_DSR",
    "MIN_FOLDS_IMPROVED",
    "MIN_MEAN_XIRR_IMPROVEMENT",
    "PREREGISTERED_MIN_TRIALS",
    "ArmFolds",
    "ArmVerdict",
    "Criterion",
    "FoldResult",
    "evaluate",
    "render_decision",
    "trial_sharpe_variance",
]

#: Criterion 1: mean after-tax XIRR improvement, as a ratio (1.0 pp).
MIN_MEAN_XIRR_IMPROVEMENT = Decimal("0.010")
#: Criterion 2: test windows in which after-tax XIRR must strictly improve.
MIN_FOLDS_IMPROVED = 2
#: Criterion 3: the most max drawdown may worsen in any test window, as a ratio (3.0 pp).
MAX_DRAWDOWN_WORSENING = Decimal("0.030")
#: Criterion 4: the deflated Sharpe ratio's probability bar.
MIN_DSR = 0.95
#: §2: the round-1 sweep's 23 arms plus the two baselines and H1-H3 — the least the trial count
#: can be. A smaller count would deflate less than the search that was actually run.
PREREGISTERED_MIN_TRIALS = 28


@dataclass(frozen=True, slots=True)
class FoldResult:
    """One arm on one fold's test window: the figures the rule reads."""

    fold: str
    #: ``None`` when it could not be struck; ``error`` then says why.
    after_tax_xirr: Decimal | None
    max_drawdown: Decimal
    #: Daily after-tax NAV returns over the test window (``backtest.nav.daily_returns``).
    after_tax_returns: tuple[float, ...]
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ArmFolds:
    """One arm's results on every fold, in fold order."""

    label: str
    folds: tuple[FoldResult, ...]

    @property
    def concatenated_returns(self) -> tuple[float, ...]:
        return tuple(r for fold in self.folds for r in fold.after_tax_returns)

    def stats(self) -> SharpeStats:
        return sharpe_stats(self.concatenated_returns)


@dataclass(frozen=True, slots=True)
class Criterion:
    """One of §4's four criteria for one arm: whether it held, and the figures that decided it."""

    number: int
    rule: str
    passed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class ArmVerdict:
    """A candidate against the baseline: the four criteria, and whether the arm is kept."""

    candidate: str
    baseline: str
    criteria: tuple[Criterion, ...]

    @property
    def kept(self) -> bool:
        return all(c.passed for c in self.criteria)


def _pp(value: Decimal) -> str:
    return f"{value * 100:+.2f} pp"


def _pct(value: Decimal | None) -> str:
    return "n/a" if value is None else f"{value:.2%}"


def trial_sharpe_variance(arms: Sequence[ArmFolds]) -> float:
    """Variance of every evaluated arm's per-period Sharpe on its concatenated test returns."""
    return sharpe_variance([arm.stats().sharpe for arm in arms])


def _known(arm: ArmFolds) -> list[Decimal]:
    return [f.after_tax_xirr for f in arm.folds if f.after_tax_xirr is not None]


def _xirr_criteria(candidate: ArmFolds, baseline: ArmFolds) -> tuple[Criterion, Criterion]:
    """Criteria 1 and 2, which read the same per-fold after-tax XIRRs."""
    one = "mean after-tax XIRR across the test windows improves by >= 1.0 pp"
    two = (
        f"after-tax XIRR improves in at least {MIN_FOLDS_IMPROVED} of "
        f"{len(baseline.folds)} test windows"
    )
    missing = [
        f"{arm.label} {fold.fold}: {fold.error or 'no after-tax XIRR'}"
        for arm in (candidate, baseline)
        for fold in arm.folds
        if fold.after_tax_xirr is None
    ]
    if missing:
        why = "FAIL: not computable — " + "; ".join(missing)
        return Criterion(1, one, False, why), Criterion(2, two, False, why)
    cand, base = _known(candidate), _known(baseline)
    gains = [c - b for c, b in zip(cand, base, strict=True)]
    n = Decimal(len(gains))
    total = sum(gains, Decimal(0))
    # Compared as a sum against n x the bar, so no division rounds a boundary case either way.
    first = Criterion(
        1,
        one,
        total >= MIN_MEAN_XIRR_IMPROVEMENT * n,
        f"mean {_pct(sum(cand, Decimal(0)) / n)} vs baseline {_pct(sum(base, Decimal(0)) / n)}: "
        f"{_pp(total / n)} (bar {_pp(MIN_MEAN_XIRR_IMPROVEMENT)})",
    )
    improved = [f.fold for f, g in zip(candidate.folds, gains, strict=True) if g > 0]
    second = Criterion(
        2,
        two,
        len(improved) >= MIN_FOLDS_IMPROVED,
        f"improved in {len(improved)} ({', '.join(improved) or 'none'}): "
        + ", ".join(f"{f.fold} {_pp(g)}" for f, g in zip(candidate.folds, gains, strict=True)),
    )
    return first, second


def _drawdown_criterion(candidate: ArmFolds, baseline: ArmFolds) -> Criterion:
    pairs = list(zip(candidate.folds, baseline.folds, strict=True))
    breached = [
        c.fold for c, b in pairs if c.max_drawdown - b.max_drawdown > MAX_DRAWDOWN_WORSENING
    ]
    detail = ", ".join(
        f"{c.fold} {_pct(c.max_drawdown)} vs {_pct(b.max_drawdown)} "
        f"({_pp(c.max_drawdown - b.max_drawdown)})"
        for c, b in pairs
    )
    return Criterion(
        3,
        "max drawdown worsens by no more than 3.0 pp in any test window",
        not breached,
        detail + (f"; breached in {', '.join(breached)}" if breached else ""),
    )


def _dsr_criterion(
    candidate: ArmFolds, baseline: ArmFolds, *, trials: int, variance: float
) -> Criterion:
    rule = (
        f"deflated Sharpe ratio vs the baseline's Sharpe, {trials} trials, gives probability "
        f">= {MIN_DSR:.2f}"
    )
    try:
        cand, base = candidate.stats(), baseline.stats()
        dsr = deflated_sharpe_ratio(
            cand, trials=trials, sharpe_variance=variance, benchmark_sharpe=base.sharpe
        )
    except ValueError as error:
        return Criterion(4, rule, False, f"FAIL: not computable — {error}")
    return Criterion(
        4,
        rule,
        dsr >= MIN_DSR,
        f"DSR {dsr:.4f}; daily Sharpe {cand.sharpe:.5f} vs baseline {base.sharpe:.5f} over "
        f"{cand.observations} returns (skew {cand.skewness:.3f}, kurtosis {cand.kurtosis:.3f}); "
        f"trial Sharpe variance {variance:.3e}",
    )


def evaluate(
    candidate: ArmFolds, baseline: ArmFolds, *, trials: int, sharpe_variance: float
) -> ArmVerdict:
    """§4's four criteria for ``candidate`` against ``baseline``, each with its figures.

    Assumes both carry the same folds in the same order (``ValueError`` otherwise) and that
    ``trials`` is the recorded count, at least :data:`PREREGISTERED_MIN_TRIALS` (``ValueError``
    below it). ``sharpe_variance`` is the variance of the per-period Sharpe across the arms
    evaluated (:func:`trial_sharpe_variance`).
    """
    if [f.fold for f in candidate.folds] != [f.fold for f in baseline.folds]:
        raise ValueError(f"{candidate.label} and {baseline.label} were not run on the same folds")
    if not candidate.folds:
        raise ValueError("no folds to evaluate")
    if trials < PREREGISTERED_MIN_TRIALS:
        raise ValueError(
            f"the trial count is at least {PREREGISTERED_MIN_TRIALS} (pre-registration §2), "
            f"got {trials}"
        )
    first, second = _xirr_criteria(candidate, baseline)
    return ArmVerdict(
        candidate=candidate.label,
        baseline=baseline.label,
        criteria=(
            first,
            second,
            _drawdown_criterion(candidate, baseline),
            _dsr_criterion(candidate, baseline, trials=trials, variance=sharpe_variance),
        ),
    )


def render_decision(
    verdicts: Sequence[ArmVerdict],
    arms: Sequence[ArmFolds],
    *,
    trials: int,
    sharpe_variance: float,
    floor_label: str,
    assumptions: Sequence[str] = (),
) -> str:
    """The round-2 decision as markdown: per-fold figures, then PASS/FAIL per criterion per arm."""
    baselines = sorted({v.baseline for v in verdicts})
    lines = [
        "# Round 2 — the pre-registered decision rule",
        "",
        "*Rule: `ops/studies/preregistration-signals-2026-09-29.md` §4, fixed before evaluation. "
        "Generated by `python -m backtest.fold_campaign round2-signals`.*",
        "",
        f"- Floor: **{floor_label}**",
        f"- Frozen baseline: **{', '.join(baselines) or '(none)'}**",
        f"- Trial count for the deflation: **{trials}**",
        f"- Variance of per-period Sharpe across the {len(arms)} arms evaluated: "
        f"**{sharpe_variance:.6e}**",
        *(f"- {line}" for line in assumptions),
        "",
        "## Test-window figures",
        "",
        "| Arm | Fold | After-tax XIRR (realised) | Max drawdown | Daily returns |",
        "| --- | --- | --- | --- | --- |",
    ]
    for arm in arms:
        for fold in arm.folds:
            xirr = _pct(fold.after_tax_xirr) if fold.error is None else f"n/a ({fold.error})"
            lines.append(
                f"| {arm.label} | {fold.fold} | {xirr} | {_pct(fold.max_drawdown)} | "
                f"{len(fold.after_tax_returns)} |"
            )
    lines += ["", "## Verdict per arm", ""]
    for verdict in verdicts:
        lines += [
            f"### {verdict.candidate} vs {verdict.baseline}: "
            f"**{'KEPT' if verdict.kept else 'NOT KEPT'}**",
            "",
            "| # | Criterion | Result | Figures |",
            "| --- | --- | --- | --- |",
        ]
        for c in verdict.criteria:
            lines.append(
                f"| {c.number} | {c.rule} | **{'PASS' if c.passed else 'FAIL'}** | {c.detail} |"
            )
        lines.append("")
    kept = [v.candidate for v in verdicts if v.kept]
    lines.append(
        f"**Answer: kept — {', '.join(kept)}.**"
        if kept
        else "**Answer: no improvement found.** No arm met all four criteria."
    )
    return "\n".join(lines) + "\n"
