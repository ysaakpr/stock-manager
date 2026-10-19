"""The drawdown-first rule (PE-drawdown study, ``backtest.drawdown_rule``), offline.

Every criterion is pinned at its boundary and against its inversion: a test fails if the rule
compares the wrong way round (the candidate's ratio against the rival's swapped, the drawdown cut
measured from the wrong side, the cash hurdle read as a ceiling).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from backtest.decision_rule import Outcome
from backtest.drawdown_rule import (
    MIN_VARIANCE_RATIOS,
    ArmWindows,
    RatioVariance,
    StudyVerdict,
    TrialRatio,
    WindowFigures,
    beats_on_ratio,
    calmar,
    deflated_ratio,
    evaluate_drawdown_first,
    ratio_variance,
    robust_pass,
    study_verdict,
)

FOLDS = ("F1", "F2", "F3")
HURDLE = {f: Decimal("0.04") for f in FOLDS}


def _w(fold: str, xirr: str, dd: str, after: str | None = "0.12") -> WindowFigures:
    return WindowFigures(
        window=fold,
        pre_tax_xirr=Decimal(xirr),
        max_drawdown=Decimal(dd),
        after_tax_xirr=None if after is None else Decimal(after),
        error=None if after is not None else "missing grandfathering price",
    )


def _returns(drift: float, shock: float, n: int = 750) -> tuple[float, ...]:
    """A deterministic daily path: a steady drift with a periodic shock (so it has drawdowns)."""
    return tuple(drift - (shock if i % 50 == 49 else 0.0) for i in range(n))


def _arm(
    label: str,
    rows: tuple[tuple[str, str, str | None], ...],
    returns: tuple[float, ...] = (),
) -> ArmWindows:
    windows = tuple(_w(f, x, d, a) for f, (x, d, a) in zip(FOLDS, rows, strict=True))
    return ArmWindows(label, windows, returns or _returns(0.0006, 0.01), 3.0)


#: The candidate: ratio 1.0 everywhere, worst DD 15 %, after-tax 12 %.
CAND = _arm(
    "candidate",
    (("0.15", "0.15", "0.12"), ("0.15", "0.15", "0.12"), ("0.15", "0.15", "0.12")),
    _returns(0.0009, 0.004),
)
#: The baseline: ratio 0.8, worst DD 25 %.
BASE = _arm(
    "D13",
    (("0.20", "0.25", "0.17"), ("0.16", "0.20", "0.13"), ("0.16", "0.20", "0.13")),
    _returns(0.0009, 0.02),
)
#: A comparator: ratio 0.5.
COMP = _arm(
    "equal exposure",
    (("0.10", "0.20", "0.08"), ("0.10", "0.20", "0.08"), ("0.10", "0.20", "0.08")),
)


def _variance(n: int = MIN_VARIANCE_RATIOS) -> RatioVariance:
    trials = [TrialRatio(f"t{i}", 0.5 + 0.01 * i) for i in range(n)]
    return ratio_variance(trials, source="synthetic")


def _evaluate(
    candidate: ArmWindows = CAND,
    baseline: ArmWindows = BASE,
    comparators: tuple[ArmWindows, ...] = (COMP,),
    *,
    variance: RatioVariance | None = None,
    trials: int = 126,
) -> list[Outcome]:
    verdict = evaluate_drawdown_first(
        candidate,
        baseline,
        comparators,
        cash_hurdle=HURDLE,
        trials=trials,
        variance=variance or _variance(),
    )
    return [c.outcome for c in verdict.criteria]


def test_beats_on_ratio_is_strict_and_uses_cross_products() -> None:
    a = _w("F1", "0.15", "0.15")
    b = _w("F1", "0.20", "0.20")  # the same ratio, 1.0
    assert beats_on_ratio(a, b) is False  # a tie is not a win
    assert beats_on_ratio(_w("F1", "0.16", "0.15"), b) is True
    assert beats_on_ratio(a, _w("F1", "0.10", "0.00")) is None


def test_ratio_criterion_inversion() -> None:
    """The candidate (ratio 1.0) beats the comparator (0.5); swapped, the same rule fails it."""
    assert _evaluate()[0] is Outcome.PASS
    swapped = evaluate_drawdown_first(
        COMP, BASE, (CAND,), cash_hurdle=HURDLE, trials=126, variance=_variance()
    )
    assert swapped.criteria[0].outcome is Outcome.FAIL


def test_ratio_must_beat_every_rival_in_two_of_three_windows() -> None:
    # A rival that the candidate beats only in F1 (rival ratio 1.2 in F2 and F3).
    strong = _arm(
        "vol target",
        (("0.10", "0.20", "0.08"), ("0.18", "0.15", "0.14"), ("0.18", "0.15", "0.14")),
    )
    assert _evaluate(comparators=(COMP, strong))[0] is Outcome.FAIL
    # Beaten in exactly two of three windows passes.
    two = _arm(
        "price band",
        (("0.10", "0.20", "0.08"), ("0.10", "0.20", "0.08"), ("0.18", "0.15", "0.14")),
    )
    assert _evaluate(comparators=(COMP, two))[0] is Outcome.PASS


def test_drawdown_cut_boundary_and_direction() -> None:
    # Worst DD 15 % vs baseline 25 %: 10 pp lower, passes.
    assert _evaluate()[1] is Outcome.PASS
    # Baseline worst exactly 20 %: a 5.0 pp cut passes (the bar is "at least").
    edge = _arm(
        "D13",
        (("0.16", "0.20", "0.13"), ("0.16", "0.20", "0.13"), ("0.16", "0.20", "0.13")),
    )
    assert _evaluate(baseline=edge)[1] is Outcome.PASS
    # 4.9 pp is not enough.
    short = _arm(
        "D13",
        (("0.16", "0.199", "0.13"), ("0.16", "0.18", "0.13"), ("0.16", "0.18", "0.13")),
    )
    assert _evaluate(baseline=short)[1] is Outcome.FAIL
    # Inversion: the deeper-drawdown arm as the candidate fails.
    swapped = evaluate_drawdown_first(
        BASE, CAND, (COMP,), cash_hurdle=HURDLE, trials=126, variance=_variance()
    )
    assert swapped.criteria[1].outcome is Outcome.FAIL


def test_cash_hurdle_is_a_floor_strictly_and_in_every_window() -> None:
    assert _evaluate()[2] is Outcome.PASS
    at_cash = _arm(
        "candidate",
        (("0.15", "0.15", "0.04"), ("0.15", "0.15", "0.12"), ("0.15", "0.15", "0.12")),
    )
    assert _evaluate(candidate=at_cash)[2] is Outcome.FAIL  # equal is not above
    missing = _arm(
        "candidate",
        (("0.15", "0.15", None), ("0.15", "0.15", "0.12"), ("0.15", "0.15", "0.12")),
    )
    assert _evaluate(candidate=missing)[2] is Outcome.FAIL  # uncomputable is FAIL


def test_return_floor_boundary() -> None:
    at_floor = _arm(
        "candidate",
        (("0.15", "0.15", "0.08"), ("0.15", "0.15", "0.10"), ("0.15", "0.15", "0.12")),
    )
    assert _evaluate(candidate=at_floor)[3] is Outcome.PASS  # mean exactly 10 %
    below = _arm(
        "candidate",
        (("0.15", "0.15", "0.08"), ("0.15", "0.15", "0.10"), ("0.15", "0.15", "0.1199")),
    )
    assert _evaluate(candidate=below)[3] is Outcome.FAIL


def test_calmar_on_a_known_path() -> None:
    # Up 10 %, down to 0.99 (a 10 % drawdown from 1.10), over one year: (0.99 - 1) / 0.10.
    value = calmar((0.10, -0.1), years=1.0)
    assert value == pytest.approx(-0.01 / 0.1)
    with pytest.raises(ValueError, match="no drawdown"):
        calmar((0.01, 0.01), years=1.0)


def test_deflated_ratio_is_deterministic_and_inverts() -> None:
    good = _returns(0.0009, 0.004)
    bad = _returns(0.0002, 0.02)
    years = 3.0
    p_good, threshold = deflated_ratio(good, years=years, benchmark=0.5, trials=126, variance=0.01)
    again, _ = deflated_ratio(good, years=years, benchmark=0.5, trials=126, variance=0.01)
    assert p_good == again  # seeded: the same figure to the bit
    assert threshold > 0.5  # deflated: above the benchmark
    p_bad, _ = deflated_ratio(bad, years=years, benchmark=0.5, trials=126, variance=0.01)
    assert p_good > 0.95 > p_bad


def test_deflated_criterion_fails_on_a_thin_variance() -> None:
    assert _evaluate(variance=_variance(MIN_VARIANCE_RATIOS - 1))[4] is Outcome.FAIL


def test_ratio_variance_counts_a_duplicate_once() -> None:
    v = ratio_variance(
        [TrialRatio("a", 0.5), TrialRatio("b", 0.5), TrialRatio("c", 0.7)], source="x"
    )
    assert v.n == 2
    assert v.excluded == ("b: same as a",)


def test_study_verdict_words() -> None:
    passing = evaluate_drawdown_first(
        CAND, BASE, (COMP,), cash_hurdle=HURDLE, trials=126, variance=_variance()
    )
    assert passing.passed
    assert study_verdict(passing, [("R1", True)]) is StudyVerdict.ADOPT
    assert study_verdict(passing, [("R1", True), ("R2", False)]) is StudyVerdict.NOT_ROBUST
    failing = evaluate_drawdown_first(
        COMP, BASE, (CAND,), cash_hurdle=HURDLE, trials=126, variance=_variance()
    )
    assert study_verdict(failing, [("R1", True)]) is StudyVerdict.REJECT


def test_robust_pass_reads_criteria_one_and_two_only() -> None:
    ok, _ = robust_pass(CAND, BASE, (COMP,))
    assert ok
    ok, _ = robust_pass(COMP, BASE, (CAND,))
    assert not ok


def test_windows_must_match() -> None:
    other = ArmWindows("x", (_w("F1", "0.1", "0.1"),), (), 1.0)
    with pytest.raises(ValueError, match="same windows"):
        evaluate_drawdown_first(
            CAND, other, (), cash_hurdle=HURDLE, trials=126, variance=_variance()
        )
