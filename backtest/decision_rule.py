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

**Same folds, or no verdict.** Every comparison is struck on the same test windows for the
candidate and the baseline. §4 names those windows — F1-F3, concatenated — so a fold either side
is missing is not dropped to compare the rest: it makes the criterion that needs it FAIL, with the
fold named. (Dropping it was the round-2 audit's finding: H1, with no after-tax F1, had its Sharpe
over F2-F3 set against the baseline's over F1-F3, a different set of windows.) The
like-for-like figures on the folds both do have are printed beside the FAIL, for information only.

**The Sharpe variance is an input with a provenance.** The DSR's luck premium scales with the
standard deviation of the trial Sharpes, so a variance taken from a handful of near-identical arms
deflates almost nothing. :class:`SharpeVariance` carries the value, where it came from and how many
distinct trial Sharpes it was struck from; below :data:`MIN_VARIANCE_SHARPES` criterion 4 cannot
PASS — it is INCONCLUSIVE, or FAIL when the arm would fail even with no deflation at all (the DSR
only falls as the variance rises, so that FAIL holds whatever the true variance is).

What this module never does: choose the baseline (the caller names it), default the trial count,
rank arms, or round a figure before comparing it — XIRR and drawdown compare as exact ``Decimal``.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from backtest.sharpe import (
    SharpeStats,
    deflated_sharpe_ratio,
    probabilistic_sharpe_ratio,
    sharpe_stats,
    sharpe_variance,
)

__all__ = [
    "MAX_DRAWDOWN_WORSENING",
    "MIN_DSR",
    "MIN_FOLDS_IMPROVED",
    "MIN_MEAN_XIRR_IMPROVEMENT",
    "MIN_VARIANCE_SHARPES",
    "PREREGISTERED_MIN_TRIALS",
    "ArmFolds",
    "ArmVerdict",
    "Criterion",
    "FoldResult",
    "Outcome",
    "SharpeVariance",
    "TrialSharpe",
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
#: Criterion 4: the fewest distinct trial Sharpes a variance may be struck from before the DSR it
#: feeds can PASS. A sample variance over ``n`` values has a relative standard error of about
#: sqrt(2 / (n - 1)): ~0.71 at the five arms round 2 evaluates itself, ~0.32 at twenty. Twenty is
#: within reach of the round-1 sweep's 23 arms run on the same folds, which §2 counts as trials.
MIN_VARIANCE_SHARPES = 20


class Outcome(StrEnum):
    """A criterion's result. Only PASS keeps an arm; INCONCLUSIVE is never a pass."""

    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"


@dataclass(frozen=True, slots=True)
class FoldResult:
    """One arm on one fold's test window: the figures the rule reads."""

    fold: str
    #: ``None`` when it could not be struck; ``error`` then says why.
    after_tax_xirr: Decimal | None
    max_drawdown: Decimal
    #: Daily after-tax NAV returns over the test window (``backtest.nav.daily_returns``). Empty
    #: when the after-tax NAV could not be struck — the fold is then *missing* for criterion 4.
    after_tax_returns: tuple[float, ...]
    error: str | None = None

    @property
    def has_returns(self) -> bool:
        return bool(self.after_tax_returns)


@dataclass(frozen=True, slots=True)
class ArmFolds:
    """One arm's results on every fold, in fold order."""

    label: str
    folds: tuple[FoldResult, ...]

    @property
    def fold_names(self) -> tuple[str, ...]:
        return tuple(f.fold for f in self.folds)

    @property
    def missing_returns(self) -> tuple[str, ...]:
        """The folds with no after-tax returns: any one leaves the concatenation undefined."""
        return tuple(f.fold for f in self.folds if not f.has_returns)

    def returns_on(self, folds: Sequence[str]) -> tuple[float, ...]:
        """The daily returns of ``folds``, laid end to end in this arm's fold order.

        Raises ``ValueError`` if any named fold has no returns: a concatenation that skipped one
        would be a different set of windows from the one it is labelled with.
        """
        wanted = set(folds)
        empty = [f.fold for f in self.folds if f.fold in wanted and not f.has_returns]
        if empty:
            raise ValueError(f"{self.label} has no after-tax returns on {', '.join(empty)}")
        return tuple(r for f in self.folds if f.fold in wanted for r in f.after_tax_returns)

    @property
    def concatenated_returns(self) -> tuple[float, ...]:
        """Every fold's returns end to end; ``ValueError`` if any fold has none."""
        return self.returns_on(self.fold_names)

    def stats(self) -> SharpeStats:
        return sharpe_stats(self.concatenated_returns)


@dataclass(frozen=True, slots=True)
class TrialSharpe:
    """One trial's per-period Sharpe on the concatenated fold test windows, and whose it is."""

    label: str
    sharpe: float


@dataclass(frozen=True, slots=True)
class SharpeVariance:
    """The cross-trial Sharpe variance criterion 4 deflates with, and where it came from.

    ``sharpes`` are the distinct trial Sharpes it was struck from; ``excluded`` says, per trial
    left out, why (a missing fold, a duplicate). ``value`` is ``None`` below two Sharpes.
    """

    value: float | None
    source: str
    sharpes: tuple[TrialSharpe, ...]
    excluded: tuple[str, ...] = ()

    @property
    def n(self) -> int:
        return len(self.sharpes)

    @property
    def sufficient(self) -> bool:
        return self.value is not None and self.n >= MIN_VARIANCE_SHARPES

    def describe(self) -> str:
        value = "undefined" if self.value is None else f"{self.value:.6e}"
        return f"{value} from {self.n} trial Sharpes ({self.source})"


@dataclass(frozen=True, slots=True)
class Criterion:
    """One of §4's four criteria for one arm: its outcome, the folds it read, and its figures."""

    number: int
    rule: str
    outcome: Outcome
    detail: str
    #: The folds the outcome was struck on — empty when it could not be struck on any.
    folds: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.outcome is Outcome.PASS


@dataclass(frozen=True, slots=True)
class ArmVerdict:
    """A candidate against the baseline: the four criteria, and whether the arm is kept."""

    candidate: str
    baseline: str
    criteria: tuple[Criterion, ...]

    @property
    def kept(self) -> bool:
        return all(c.passed for c in self.criteria)

    @property
    def label(self) -> str:
        """KEPT; NOT KEPT on any FAIL; otherwise INCONCLUSIVE (still not kept)."""
        if self.kept:
            return "KEPT"
        if any(c.outcome is Outcome.FAIL for c in self.criteria):
            return "NOT KEPT"
        return "INCONCLUSIVE — NOT KEPT"


def _pp(value: Decimal) -> str:
    return f"{value * 100:+.2f} pp"


def _pct(value: Decimal | None) -> str:
    return "n/a" if value is None else f"{value:.2%}"


def _outcome(passed: bool) -> Outcome:
    return Outcome.PASS if passed else Outcome.FAIL


def trial_sharpe_variance(
    arms: Sequence[ArmFolds],
    *,
    supplied: Sequence[TrialSharpe] = (),
    supplied_source: str | None = None,
) -> SharpeVariance:
    """The variance of the per-period trial Sharpes on the concatenated fold test windows.

    Pools every evaluated arm with returns on every fold and every ``supplied`` Sharpe (the
    round-1 sweep's arms run on the same folds, say). An arm missing a fold is *excluded and
    named*, never scored on the folds it has; a label seen twice, or a Sharpe equal to one already
    counted (the same series under two labels), is counted once — it is not an independent trial.
    """
    counted: list[TrialSharpe] = []
    excluded: list[str] = []

    def add(trial: TrialSharpe) -> None:
        if not math.isfinite(trial.sharpe):
            excluded.append(f"{trial.label}: Sharpe {trial.sharpe} is not finite")
            return
        for seen in counted:
            if seen.label == trial.label:
                excluded.append(f"{trial.label}: counted once already")
                return
            if math.isclose(seen.sharpe, trial.sharpe, rel_tol=0.0, abs_tol=1e-12):
                excluded.append(f"{trial.label}: same Sharpe as {seen.label}")
                return
        counted.append(trial)

    for arm in arms:
        if arm.missing_returns:
            excluded.append(
                f"{arm.label}: no after-tax returns on {', '.join(arm.missing_returns)}"
            )
            continue
        try:
            add(TrialSharpe(arm.label, arm.stats().sharpe))
        except ValueError as error:
            excluded.append(f"{arm.label}: {error}")
    for trial in supplied:
        add(trial)
    parts = [f"{len(arms)} arms evaluated in this round"]
    if supplied_source is not None:
        parts.append(f"{len(supplied)} supplied by {supplied_source}")
    value = sharpe_variance([t.sharpe for t in counted]) if len(counted) >= 2 else None
    return SharpeVariance(value, " + ".join(parts), tuple(counted), tuple(excluded))


def _known_xirr(arm: ArmFolds) -> dict[str, Decimal]:
    return {f.fold: f.after_tax_xirr for f in arm.folds if f.after_tax_xirr is not None}


def _missing(candidate: ArmFolds, baseline: ArmFolds, *, xirr: bool) -> list[str]:
    out = []
    for arm in (candidate, baseline):
        for fold in arm.folds:
            gone = fold.after_tax_xirr is None if xirr else not fold.has_returns
            if gone:
                out.append(f"{arm.label} {fold.fold}: {fold.error or 'no after-tax figure'}")
    return out


def _both_have(candidate: ArmFolds, baseline: ArmFolds, *, xirr: bool) -> tuple[str, ...]:
    def ok(f: FoldResult) -> bool:
        return f.after_tax_xirr is not None if xirr else f.has_returns

    return tuple(
        c.fold for c, b in zip(candidate.folds, baseline.folds, strict=True) if ok(c) and ok(b)
    )


def _folds(names: Sequence[str]) -> str:
    return ", ".join(names) or "none"


def _xirr_criteria(candidate: ArmFolds, baseline: ArmFolds) -> tuple[Criterion, Criterion]:
    """Criteria 1 and 2, which read the same per-fold after-tax XIRRs."""
    one = "mean after-tax XIRR across the test windows improves by >= 1.0 pp"
    two = (
        f"after-tax XIRR improves in at least {MIN_FOLDS_IMPROVED} of "
        f"{len(baseline.folds)} test windows"
    )
    missing = _missing(candidate, baseline, xirr=True)
    if missing:
        why = "FAIL: not computable on every test window — " + "; ".join(missing)
        common = _both_have(candidate, baseline, xirr=True)
        if common:
            cand_k, base_k = _known_xirr(candidate), _known_xirr(baseline)
            why += f"; for information only, on {_folds(common)}: " + ", ".join(
                f"{f} {_pp(cand_k[f] - base_k[f])}" for f in common
            )
        return (
            Criterion(1, one, Outcome.FAIL, why, ()),
            Criterion(2, two, Outcome.FAIL, why, ()),
        )
    used = candidate.fold_names
    cand_k, base_k = _known_xirr(candidate), _known_xirr(baseline)
    cand_x, base_x = [cand_k[f] for f in used], [base_k[f] for f in used]
    gains = [c - b for c, b in zip(cand_x, base_x, strict=True)]
    n = Decimal(len(gains))
    total = sum(gains, Decimal(0))
    # Compared as a sum against n x the bar, so no division rounds a boundary case either way.
    first = Criterion(
        1,
        one,
        _outcome(total >= MIN_MEAN_XIRR_IMPROVEMENT * n),
        f"mean {_pct(sum(cand_x, Decimal(0)) / n)} vs baseline "
        f"{_pct(sum(base_x, Decimal(0)) / n)}: "
        f"{_pp(total / n)} (bar {_pp(MIN_MEAN_XIRR_IMPROVEMENT)})",
        used,
    )
    improved = [f for f, g in zip(used, gains, strict=True) if g > 0]
    second = Criterion(
        2,
        two,
        _outcome(len(improved) >= MIN_FOLDS_IMPROVED),
        f"improved in {len(improved)} ({_folds(improved)}): "
        + ", ".join(f"{f} {_pp(g)}" for f, g in zip(used, gains, strict=True)),
        used,
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
        _outcome(not breached),
        detail + (f"; breached in {', '.join(breached)}" if breached else ""),
        candidate.fold_names,
    )


def _sharpe_line(cand: SharpeStats, base: SharpeStats) -> str:
    return (
        f"daily Sharpe {cand.sharpe:.5f} vs baseline {base.sharpe:.5f} over "
        f"{cand.observations} vs {base.observations} returns"
    )


def _dsr_criterion(
    candidate: ArmFolds, baseline: ArmFolds, *, trials: int, variance: SharpeVariance
) -> Criterion:
    rule = (
        f"deflated Sharpe ratio vs the baseline's Sharpe, {trials} trials, gives probability "
        f">= {MIN_DSR:.2f}"
    )
    source = f"Sharpe variance {variance.describe()}"
    missing = _missing(candidate, baseline, xirr=False)
    if missing:
        why = (
            "FAIL: not computable — §4 reads the concatenated test windows of every fold; "
            + "; ".join(missing)
        )
        common = _both_have(candidate, baseline, xirr=False)
        if common:
            try:
                info = _sharpe_line(
                    sharpe_stats(candidate.returns_on(common)),
                    sharpe_stats(baseline.returns_on(common)),
                )
                why += f"; for information only, on {_folds(common)}: {info}"
            except ValueError:
                pass  # the FAIL stands on its own; the like-for-like aside is optional
        return Criterion(4, rule, Outcome.FAIL, f"{why}; {source}", ())
    used = candidate.fold_names
    lengths = [
        f"{c.fold} {len(c.after_tax_returns)} vs {len(b.after_tax_returns)}"
        for c, b in zip(candidate.folds, baseline.folds, strict=True)
        if len(c.after_tax_returns) != len(b.after_tax_returns)
    ]
    if lengths:
        return Criterion(
            4,
            rule,
            Outcome.FAIL,
            "FAIL: not computable — the two arms' return series do not cover the same sessions "
            f"({'; '.join(lengths)}); {source}",
            (),
        )
    try:
        cand, base = candidate.stats(), baseline.stats()
        undeflated = probabilistic_sharpe_ratio(
            cand.sharpe, cand.observations, cand.skewness, cand.kurtosis, threshold=base.sharpe
        )
        dsr = (
            None
            if variance.value is None
            else deflated_sharpe_ratio(
                cand, trials=trials, sharpe_variance=variance.value, benchmark_sharpe=base.sharpe
            )
        )
    except ValueError as error:
        return Criterion(4, rule, Outcome.FAIL, f"FAIL: not computable — {error}; {source}", ())
    moments = f"{_sharpe_line(cand, base)} (skew {cand.skewness:.3f}, kurtosis {cand.kurtosis:.3f})"
    shown = "n/a" if dsr is None else f"{dsr:.4f}"
    if variance.sufficient:
        assert dsr is not None  # sufficient implies a value
        return Criterion(
            4, rule, _outcome(dsr >= MIN_DSR), f"DSR {shown}; {moments}; {source}", used
        )
    thin = (
        f"the variance is struck from {variance.n} trial Sharpes, fewer than the "
        f"{MIN_VARIANCE_SHARPES} it needs"
    )
    if undeflated < MIN_DSR:
        # The DSR falls as the variance rises, so no variance could lift it above this.
        return Criterion(
            4,
            rule,
            Outcome.FAIL,
            f"FAIL whatever the variance: even undeflated, P(SR > baseline) is "
            f"{undeflated:.4f} < {MIN_DSR:.2f} ({thin}; DSR on it {shown}); {moments}; {source}",
            used,
        )
    return Criterion(
        4,
        rule,
        Outcome.INCONCLUSIVE,
        f"INCONCLUSIVE: {thin} (DSR on it {shown}, undeflated {undeflated:.4f}); supply the "
        f"round-1 trial Sharpes on these folds; {moments}; {source}",
        used,
    )


def evaluate(
    candidate: ArmFolds, baseline: ArmFolds, *, trials: int, sharpe_variance: SharpeVariance
) -> ArmVerdict:
    """§4's four criteria for ``candidate`` against ``baseline``, each with its figures.

    Assumes both carry the same folds in the same order (``ValueError`` otherwise) and that
    ``trials`` is the recorded count, at least :data:`PREREGISTERED_MIN_TRIALS` (``ValueError``
    below it). ``sharpe_variance`` is the cross-trial variance with its provenance
    (:func:`trial_sharpe_variance`). Never scores the two arms on different folds.
    """
    if candidate.fold_names != baseline.fold_names:
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
    sharpe_variance: SharpeVariance,
    floor_label: str,
    assumptions: Sequence[str] = (),
    command: str = "round2-signals",
) -> str:
    """The round-2 decision as markdown: per-fold figures, then the outcome per criterion per arm.

    Every criterion row names the folds it was struck on; the header names the Sharpe variance's
    source, its n and every trial left out of it.
    """
    baselines = sorted({v.baseline for v in verdicts})
    variance = sharpe_variance
    sufficient = "" if variance.sufficient else " — **too few: criterion 4 cannot PASS**"
    lines = [
        "# Round 2 — the pre-registered decision rule",
        "",
        "*Rule: `ops/studies/preregistration-signals-2026-09-29.md` §4, fixed before evaluation. "
        f"Generated by `python -m backtest.fold_campaign {command}`.*",
        "",
        f"- Floor: **{floor_label}**",
        f"- Frozen baseline: **{', '.join(baselines) or '(none)'}**",
        f"- Trial count for the deflation: **{trials}**",
        f"- Sharpe variance for the deflation: **{variance.describe()}**; at least "
        f"{MIN_VARIANCE_SHARPES} distinct trial Sharpes required{sufficient}",
        f"- Trial Sharpes counted (n = {variance.n}): "
        + (", ".join(f"{t.label} {t.sharpe:.5f}" for t in variance.sharpes) or "none"),
        *(
            [f"- Left out of the variance: {'; '.join(variance.excluded)}"]
            if variance.excluded
            else []
        ),
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
            f"### {verdict.candidate} vs {verdict.baseline}: **{verdict.label}**",
            "",
            "| # | Criterion | Result | Folds used | Figures |",
            "| --- | --- | --- | --- | --- |",
        ]
        for c in verdict.criteria:
            lines.append(
                f"| {c.number} | {c.rule} | **{c.outcome}** | {_folds(c.folds)} | {c.detail} |"
            )
        lines.append("")
    kept = [v.candidate for v in verdicts if v.kept]
    lines.append(
        f"**Answer: kept — {', '.join(kept)}.**"
        if kept
        else "**Answer: no improvement found.** No arm met all four criteria."
    )
    return "\n".join(lines) + "\n"
