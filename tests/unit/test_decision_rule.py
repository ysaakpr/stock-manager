"""X2 — the pre-registered decision rule (§4): every criterion's boundary, and FAIL on unknowns.

Each criterion is tested exactly at its bar (passes), just past it (fails), and on the other side;
criterion 4 is tested on real statistics and at the 0.95 boundary itself. The rule's constants are
pinned to the pre-registration's numbers.
"""

from __future__ import annotations

import math
from decimal import Decimal

import pytest

import backtest.decision_rule as rule
from backtest.decision_rule import (
    ArmFolds,
    FoldResult,
    evaluate,
    render_decision,
    trial_sharpe_variance,
)

_FOLDS = ("F1", "F2", "F3")
_N = 700


def _returns(drift: float, phase: int) -> tuple[float, ...]:
    return tuple(drift + 0.01 * math.sin(i + phase) for i in range(_N))


def _arm(
    label: str,
    xirrs: tuple[str | None, str | None, str | None],
    dds: tuple[str, str, str] = ("0.20", "0.20", "0.20"),
    drift: float = 0.0012,
    phase: int = 0,
) -> ArmFolds:
    folds = tuple(
        FoldResult(
            fold=name,
            after_tax_xirr=None if x is None else Decimal(x),
            max_drawdown=Decimal(dd),
            after_tax_returns=_returns(drift, phase + 7 * i),
            error=None if x is not None else "missing FMV",
        )
        for i, (name, x, dd) in enumerate(zip(_FOLDS, xirrs, dds, strict=True))
    )
    return ArmFolds(label, folds)


_BASE = _arm("Swing composite (M10.7)", ("0.10", "0.10", "0.10"), drift=0.0005, phase=1)


def _verdict(candidate: ArmFolds, *, trials: int = 28, variance: float = 1e-4) -> rule.ArmVerdict:
    return evaluate(candidate, _BASE, trials=trials, sharpe_variance=variance)


def _passed(candidate: ArmFolds, number: int, **kw: object) -> bool:
    verdict = _verdict(candidate, **kw)  # type: ignore[arg-type]
    return next(c for c in verdict.criteria if c.number == number).passed


def test_the_constants_are_the_preregistered_ones() -> None:
    assert Decimal("0.010") == rule.MIN_MEAN_XIRR_IMPROVEMENT
    assert rule.MIN_FOLDS_IMPROVED == 2
    assert Decimal("0.030") == rule.MAX_DRAWDOWN_WORSENING
    assert rule.MIN_DSR == 0.95
    assert rule.PREREGISTERED_MIN_TRIALS == 28


# ── criterion 1: mean after-tax XIRR improves by >= 1.0 pp ─────────────────────────────────────


def test_c1_exactly_one_pp_passes() -> None:
    assert _passed(_arm("H", ("0.12", "0.10", "0.11")), 1)  # +2, 0, +1 -> mean +1.0 pp


def test_c1_just_under_one_pp_fails() -> None:
    assert not _passed(_arm("H", ("0.12", "0.10", "0.10999999")), 1)


def test_c1_a_loss_fails() -> None:
    assert not _passed(_arm("H", ("0.09", "0.09", "0.09")), 1)


# ── criterion 2: improves in at least 2 of 3 test windows ──────────────────────────────────────


def test_c2_two_of_three_passes() -> None:
    assert _passed(_arm("H", ("0.1001", "0.1001", "0.05")), 2)


def test_c2_one_of_three_fails_even_with_a_large_mean_gain() -> None:
    candidate = _arm("H", ("0.30", "0.09", "0.09"))
    assert _passed(candidate, 1)
    assert not _passed(candidate, 2)


def test_c2_a_tie_is_not_an_improvement() -> None:
    assert not _passed(_arm("H", ("0.20", "0.10", "0.10")), 2)


# ── criterion 3: drawdown worsens by no more than 3.0 pp in any window ─────────────────────────


def test_c3_exactly_three_pp_worse_passes() -> None:
    assert _passed(_arm("H", ("0.12", "0.12", "0.12"), dds=("0.23", "0.23", "0.23")), 3)


def test_c3_just_over_three_pp_in_one_window_fails() -> None:
    candidate = _arm("H", ("0.12", "0.12", "0.12"), dds=("0.10", "0.2300001", "0.10"))
    verdict = _verdict(candidate)
    c3 = verdict.criteria[2]
    assert not c3.passed and "breached in F2" in c3.detail


def test_c3_a_better_drawdown_passes() -> None:
    assert _passed(_arm("H", ("0.12", "0.12", "0.12"), dds=("0.05", "0.05", "0.05")), 3)


# ── criterion 4: DSR >= 0.95 against the baseline's Sharpe ─────────────────────────────────────


def test_c4_a_clearly_better_series_passes() -> None:
    assert _passed(_arm("H", ("0.12", "0.12", "0.12")), 4, variance=1e-4)


def test_c4_the_baselines_own_series_fails() -> None:
    same = ArmFolds("H", _BASE.folds)
    assert not _passed(same, 4)


def test_c4_the_trial_count_is_what_deflates_it() -> None:
    # Same series, same trial variance: 28 trials clears the bar (DSR ~0.998), a million does not
    # (~0.77). A criterion that ignored the trial count would pass both.
    candidate = _arm("H", ("0.12", "0.12", "0.12"))
    assert _passed(candidate, 4, trials=28, variance=3e-4)
    assert not _passed(candidate, 4, trials=10**6, variance=3e-4)


@pytest.mark.parametrize(("dsr", "passed"), [(0.95, True), (0.9499999, False), (0.99, True)])
def test_c4_boundary_is_inclusive(
    monkeypatch: pytest.MonkeyPatch, dsr: float, passed: bool
) -> None:
    monkeypatch.setattr(rule, "deflated_sharpe_ratio", lambda *a, **k: dsr)
    assert _passed(_arm("H", ("0.12", "0.12", "0.12")), 4) is passed


def test_c4_uses_the_baseline_sharpe_as_benchmark(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, float] = {}

    def spy(
        stats: object, *, trials: int, sharpe_variance: float, benchmark_sharpe: float
    ) -> float:
        seen.update(trials=trials, variance=sharpe_variance, benchmark=benchmark_sharpe)
        return 1.0

    monkeypatch.setattr(rule, "deflated_sharpe_ratio", spy)
    _verdict(_arm("H", ("0.12", "0.12", "0.12")), trials=31, variance=2e-4)
    assert seen == {"trials": 31, "variance": 2e-4, "benchmark": _BASE.stats().sharpe}


# ── the whole rule ────────────────────────────────────────────────────────────────────────────


def test_all_four_passing_keeps_the_arm() -> None:
    verdict = _verdict(_arm("H", ("0.12", "0.11", "0.10")))
    assert [c.passed for c in verdict.criteria] == [True, True, True, True]
    assert verdict.kept


def test_an_uncomputable_xirr_fails_criteria_one_and_two() -> None:
    verdict = _verdict(_arm("H", ("0.30", None, "0.30")))
    assert not verdict.criteria[0].passed and not verdict.criteria[1].passed
    assert (
        "not computable" in verdict.criteria[0].detail
        and "missing FMV" in verdict.criteria[0].detail
    )
    assert not verdict.kept


def test_the_trial_variance_is_across_every_evaluated_arm() -> None:
    arms = [_BASE, _arm("H", ("0.1", "0.1", "0.1"))]
    a, b = (arm.stats().sharpe for arm in arms)
    assert trial_sharpe_variance(arms) == pytest.approx((a - b) ** 2 / 2)


def test_fewer_trials_than_preregistered_is_refused() -> None:
    with pytest.raises(ValueError, match="at least 28"):
        _verdict(_arm("H", ("0.12", "0.12", "0.12")), trials=27)


def test_mismatched_folds_are_refused() -> None:
    short = ArmFolds("H", _arm("H", ("0.1", "0.1", "0.1")).folds[:2])
    with pytest.raises(ValueError, match="same folds"):
        evaluate(short, _BASE, trials=28, sharpe_variance=1e-4)


def test_the_render_prints_pass_and_fail_per_criterion() -> None:
    good = _arm("H1", ("0.12", "0.11", "0.10"))
    bad = _arm("H2", ("0.09", "0.09", "0.09"))
    arms = [_BASE, good, bad]
    variance = 1e-4
    verdicts = [evaluate(a, _BASE, trials=28, sharpe_variance=variance) for a in (good, bad)]
    text = render_decision(
        verdicts, arms, trials=28, sharpe_variance=variance, floor_label="₹10 crore/day"
    )
    assert "### H1 vs Swing composite (M10.7): **KEPT**" in text
    assert "### H2 vs Swing composite (M10.7): **NOT KEPT**" in text
    assert text.count("**PASS**") + text.count("**FAIL**") == 8
    assert "Trial count for the deflation: **28**" in text
    assert "₹10 crore/day" in text


def test_the_render_says_no_improvement_when_nothing_passes() -> None:
    bad = _arm("H2", ("0.09", "0.09", "0.09"))
    verdicts = [evaluate(bad, _BASE, trials=28, sharpe_variance=1e-4)]
    text = render_decision(verdicts, [_BASE, bad], trials=28, sharpe_variance=1e-4, floor_label="x")
    assert "**Answer: no improvement found.**" in text
