"""X2 — the pre-registered decision rule (§4): every criterion's boundary, and FAIL on unknowns.

Each criterion is tested exactly at its bar (passes), just past it (fails), and on the other side;
criterion 4 is tested on real statistics and at the 0.95 boundary itself. The rule's constants are
pinned to the pre-registration's numbers.

Two round-2 audit findings are pinned here: an arm missing a fold is never compared with the
baseline on a different set of folds (it FAILs, and the like-for-like figures it is shown beside
are on the folds both have), and a Sharpe variance struck from too few trial Sharpes can never
PASS criterion 4.
"""

from __future__ import annotations

import math
from dataclasses import replace
from decimal import Decimal

import pytest

import backtest.decision_rule as rule
from backtest.decision_rule import (
    ArmFolds,
    FoldResult,
    Outcome,
    SharpeVariance,
    TrialSharpe,
    evaluate,
    render_decision,
    trial_sharpe_variance,
)
from backtest.sharpe import sharpe_stats

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


def _var(value: float | None, n: int = rule.MIN_VARIANCE_SHARPES) -> SharpeVariance:
    """A variance said to come from ``n`` distinct trial Sharpes (the fixture's own figures)."""
    trials = tuple(TrialSharpe(f"T{i}", i * 1e-3) for i in range(n))
    return SharpeVariance(value, "fixture", trials)


def _verdict(
    candidate: ArmFolds,
    *,
    trials: int = 28,
    variance: float = 1e-4,
    base: ArmFolds = _BASE,
    n: int = rule.MIN_VARIANCE_SHARPES,
) -> rule.ArmVerdict:
    return evaluate(candidate, base, trials=trials, sharpe_variance=_var(variance, n))


def _criterion(verdict: rule.ArmVerdict, number: int) -> rule.Criterion:
    return next(c for c in verdict.criteria if c.number == number)


def _passed(candidate: ArmFolds, number: int, **kw: object) -> bool:
    verdict = _verdict(candidate, **kw)  # type: ignore[arg-type]
    return next(c for c in verdict.criteria if c.number == number).passed


def test_the_constants_are_the_preregistered_ones() -> None:
    assert Decimal("0.010") == rule.MIN_MEAN_XIRR_IMPROVEMENT
    assert rule.MIN_FOLDS_IMPROVED == 2
    assert Decimal("0.030") == rule.MAX_DRAWDOWN_WORSENING
    assert rule.MIN_DSR == 0.95
    assert rule.PREREGISTERED_MIN_TRIALS == 28
    assert rule.MIN_VARIANCE_SHARPES == 20


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
    variance = trial_sharpe_variance(arms)
    assert variance.value == pytest.approx((a - b) ** 2 / 2)
    assert variance.n == 2 and not variance.sufficient


def test_fewer_trials_than_preregistered_is_refused() -> None:
    with pytest.raises(ValueError, match="at least 28"):
        _verdict(_arm("H", ("0.12", "0.12", "0.12")), trials=27)


def test_mismatched_folds_are_refused() -> None:
    short = ArmFolds("H", _arm("H", ("0.1", "0.1", "0.1")).folds[:2])
    with pytest.raises(ValueError, match="same folds"):
        evaluate(short, _BASE, trials=28, sharpe_variance=_var(1e-4))


def test_the_render_prints_pass_and_fail_per_criterion() -> None:
    good = _arm("H1", ("0.12", "0.11", "0.10"))
    bad = _arm("H2", ("0.09", "0.09", "0.09"))
    arms = [_BASE, good, bad]
    variance = 1e-4
    verdicts = [evaluate(a, _BASE, trials=28, sharpe_variance=_var(variance)) for a in (good, bad)]
    text = render_decision(
        verdicts, arms, trials=28, sharpe_variance=_var(variance), floor_label="₹10 crore/day"
    )
    assert "### H1 vs Swing composite (M10.7): **KEPT**" in text
    assert "### H2 vs Swing composite (M10.7): **NOT KEPT**" in text
    assert text.count("**PASS**") + text.count("**FAIL**") == 8
    assert "Trial count for the deflation: **28**" in text
    assert "₹10 crore/day" in text


def test_the_render_says_no_improvement_when_nothing_passes() -> None:
    bad = _arm("H2", ("0.09", "0.09", "0.09"))
    verdicts = [evaluate(bad, _BASE, trials=28, sharpe_variance=_var(1e-4))]
    text = render_decision(
        verdicts, [_BASE, bad], trials=28, sharpe_variance=_var(1e-4), floor_label="x"
    )
    assert "**Answer: no improvement found.**" in text


# ── audit finding (a): an arm missing a fold is never scored on fewer folds than the baseline ───


def _fold(name: str, returns: tuple[float, ...], xirr: str | None = "0.10") -> FoldResult:
    return FoldResult(
        fold=name,
        after_tax_xirr=None if xirr is None else Decimal(xirr),
        max_drawdown=Decimal("0.20"),
        after_tax_returns=returns,
        error=None if xirr is not None else "no L1 bar for the FMV",
    )


def _series(drift: float, phase: int) -> tuple[float, ...]:
    return tuple(drift + 0.01 * math.sin(i + phase) for i in range(_N))


#: The baseline has a bad F1 and good F2-F3; the candidate has no after-tax F1 at all and is
#: *worse* than the baseline on F2-F3. Over its own two folds it out-Sharpes the baseline's three.
_HARD_F1_BASE = ArmFolds(
    "Swing composite (M10.7)",
    (
        _fold("F1", _series(-0.0030, 1)),
        _fold("F2", _series(0.0040, 8)),
        _fold("F3", _series(0.0040, 15)),
    ),
)
_MISSING_F1 = ArmFolds(
    "H1",
    (
        _fold("F1", (), xirr=None),
        _fold("F2", _series(0.0030, 8)),
        _fold("F3", _series(0.0030, 15)),
    ),
)


def test_the_fixture_is_the_trap_the_audit_found() -> None:
    # Unaligned, the candidate looks better; on the same folds it is worse. Both must hold, or the
    # tests below would not be testing the misalignment.
    unaligned_cand = sharpe_stats(
        [r for f in _MISSING_F1.folds for r in f.after_tax_returns]
    ).sharpe
    assert unaligned_cand > _HARD_F1_BASE.stats().sharpe
    common = ("F2", "F3")
    assert (
        sharpe_stats(_MISSING_F1.returns_on(common)).sharpe
        < sharpe_stats(_HARD_F1_BASE.returns_on(common)).sharpe
    )


def test_a_missing_fold_fails_criterion_four_instead_of_dropping_the_fold() -> None:
    # Before the fix the DSR set the candidate's F2-F3 Sharpe against the baseline's F1-F3 one and
    # PASSED this arm (DSR ~1.0). It must FAIL, name the fold, and read no folds.
    verdict = _verdict(_MISSING_F1, base=_HARD_F1_BASE, variance=1e-6)
    c4 = _criterion(verdict, 4)
    assert c4.outcome is Outcome.FAIL
    assert c4.folds == ()
    assert "not computable" in c4.detail and "H1 F1" in c4.detail
    assert not verdict.kept and verdict.label == "NOT KEPT"


def test_the_like_for_like_aside_shows_the_arm_worse_on_the_shared_folds() -> None:
    c4 = _criterion(_verdict(_MISSING_F1, base=_HARD_F1_BASE), 4)
    aside = c4.detail.split("for information only, on F2, F3: ")[1]
    cand = float(aside.split("daily Sharpe ")[1].split(" vs")[0])
    base = float(aside.split("vs baseline ")[1].split(" over")[0])
    assert cand < base


def test_a_missing_fold_fails_criteria_one_and_two_with_no_folds_used() -> None:
    verdict = _verdict(_MISSING_F1, base=_HARD_F1_BASE)
    for number in (1, 2):
        c = _criterion(verdict, number)
        assert c.outcome is Outcome.FAIL and c.folds == ()
        assert "H1 F1" in c.detail


def test_a_missing_baseline_fold_fails_too() -> None:
    gapped_base = ArmFolds(_BASE.label, (_fold("F1", (), xirr=None), *_BASE.folds[1:]))
    verdict = _verdict(_arm("H", ("0.12", "0.12", "0.12")), base=gapped_base)
    assert [c.outcome for c in verdict.criteria] == [
        Outcome.FAIL,
        Outcome.FAIL,
        Outcome.PASS,
        Outcome.FAIL,
    ]


def test_return_series_of_different_lengths_on_a_fold_fail_criterion_four() -> None:
    full = _arm("H", ("0.12", "0.12", "0.12"))
    f2 = replace(full.folds[1], after_tax_returns=full.folds[1].after_tax_returns[1:])
    trimmed = ArmFolds("H", (full.folds[0], f2, full.folds[2]))
    c4 = _criterion(_verdict(trimmed), 4)
    assert c4.outcome is Outcome.FAIL and "same sessions" in c4.detail


def test_every_criterion_names_the_folds_it_read() -> None:
    verdict = _verdict(_arm("H", ("0.12", "0.11", "0.10")))
    assert all(c.folds == _FOLDS for c in verdict.criteria)


def test_concatenating_a_missing_fold_is_refused() -> None:
    with pytest.raises(ValueError, match="no after-tax returns on F1"):
        _MISSING_F1.stats()


def test_an_arm_missing_a_fold_is_left_out_of_the_trial_variance_and_named() -> None:
    variance = trial_sharpe_variance([_HARD_F1_BASE, _MISSING_F1, _arm("H2", ("0.1",) * 3)])
    assert [t.label for t in variance.sharpes] == [_HARD_F1_BASE.label, "H2"]
    assert "H1" not in {t.label for t in variance.sharpes}
    assert any(e.startswith("H1: no after-tax returns on F1") for e in variance.excluded)


# ── audit finding (b): too few trial Sharpes never PASS criterion 4 ─────────────────────────────


def test_too_few_trial_sharpes_make_a_passing_dsr_inconclusive() -> None:
    candidate = _arm("H", ("0.12", "0.12", "0.12"))
    # With enough trial Sharpes the same figures pass (test_c4_a_clearly_better_series_passes).
    assert _criterion(_verdict(candidate, n=20), 4).outcome is Outcome.PASS
    thin = _verdict(candidate, n=5)
    c4 = _criterion(thin, 4)
    assert c4.outcome is Outcome.INCONCLUSIVE
    assert "from 5 trial Sharpes" in c4.detail and "fewer than the 20" in c4.detail
    assert not thin.kept
    assert thin.label == "INCONCLUSIVE — NOT KEPT"


def test_too_few_trial_sharpes_still_fail_an_arm_no_variance_could_pass() -> None:
    # Its Sharpe is below the baseline's: even undeflated, P(SR > baseline) < 0.5.
    worse = _arm("H", ("0.12", "0.12", "0.12"), drift=0.0001)
    c4 = _criterion(_verdict(worse, n=5), 4)
    assert c4.outcome is Outcome.FAIL and "whatever the variance" in c4.detail


def test_no_variance_at_all_is_never_a_pass() -> None:
    candidate = _arm("H", ("0.12", "0.12", "0.12"))
    verdict = evaluate(candidate, _BASE, trials=28, sharpe_variance=_var(None, n=1))
    assert _criterion(verdict, 4).outcome is Outcome.INCONCLUSIVE


def test_five_near_identical_arms_do_not_make_a_sufficient_variance() -> None:
    arms = [_arm(f"A{i}", ("0.1", "0.1", "0.1"), drift=0.0005 + i * 1e-6) for i in range(5)]
    variance = trial_sharpe_variance(arms)
    assert variance.n == 5 and not variance.sufficient
    verdict = evaluate(
        _arm("H", ("0.12", "0.12", "0.12")), _BASE, trials=28, sharpe_variance=variance
    )
    assert not _criterion(verdict, 4).passed


def test_supplied_round_one_sharpes_are_pooled_into_the_variance() -> None:
    arms = [_BASE, _arm("H", ("0.1", "0.1", "0.1"))]
    supplied = [TrialSharpe(f"R{i}", 0.05 + 0.01 * i) for i in range(23)]
    variance = trial_sharpe_variance(arms, supplied=supplied, supplied_source="round-1 file")
    assert variance.n == 25 and variance.sufficient
    assert "23 supplied by round-1 file" in variance.source
    every = [a.stats().sharpe for a in arms] + [t.sharpe for t in supplied]
    mean = sum(every) / len(every)
    assert variance.value == pytest.approx(sum((x - mean) ** 2 for x in every) / (len(every) - 1))


def test_a_duplicate_trial_is_counted_once() -> None:
    twin = ArmFolds("twin", _BASE.folds)
    variance = trial_sharpe_variance(
        [_BASE, twin], supplied=[TrialSharpe(_BASE.label, 0.3)], supplied_source="f"
    )
    assert [t.label for t in variance.sharpes] == [_BASE.label]
    assert any("twin: same Sharpe as" in e for e in variance.excluded)
    assert any("counted once already" in e for e in variance.excluded)


def test_the_render_prints_the_variance_source_n_and_the_folds_used() -> None:
    thin = trial_sharpe_variance([_BASE, _MISSING_F1, _arm("H2", ("0.1", "0.1", "0.1"))])
    verdicts = [
        evaluate(_MISSING_F1, _BASE, trials=28, sharpe_variance=thin),
        evaluate(_arm("H2", ("0.12", "0.12", "0.12")), _BASE, trials=28, sharpe_variance=thin),
    ]
    text = render_decision(
        verdicts, [_BASE, _MISSING_F1], trials=28, sharpe_variance=thin, floor_label="x"
    )
    assert "from 2 trial Sharpes (3 arms evaluated in this round)" in text
    assert "too few: criterion 4 cannot PASS" in text
    assert "Left out of the variance: H1: no after-tax returns on F1" in text
    assert "| Folds used |" in text
    assert "| **FAIL** | none |" in text
    assert "| **PASS** | F1, F2, F3 |" in text  # criterion 3 of H2
    assert "| 4 | deflated Sharpe ratio" in text and "**INCONCLUSIVE**" in text
