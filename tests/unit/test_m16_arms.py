"""M16.0: the M16 arm sets name their policy options, and fail loudly while one is missing."""

from __future__ import annotations

from dataclasses import fields, replace
from decimal import Decimal
from pathlib import Path

import pytest

from backtest.m12_rerun import ARM_SETS, RERUN_ARMS, main
from backtest.m16_arms import (
    A7_ARM,
    ARM_SET_UNITS,
    M10_7_BASELINE,
    M16_ALL_WINDOW,
    M16_FUNDAMENTALS,
    V2_ALL_ON_BASELINE,
    M16ArmError,
    OptionArm,
    m16_arms,
    m16_fundamentals_arms,
    missing_options,
    resolvable_m16_arms,
)
from backtest.policies.swing_composite import SwingCompositeParameters
from backtest.sweep import D13_PAPER_BASELINE

_ZERO = Decimal("0")


def test_default_sets_are_unchanged() -> None:
    assert ARM_SETS["m12"] == RERUN_ARMS
    assert set(ARM_SETS) == {"m12", "regime-daily", "m16", "m16-fundamentals"}


def test_baselines_are_the_existing_arms_exactly() -> None:
    assert M16_ALL_WINDOW[:3] == (D13_PAPER_BASELINE, V2_ALL_ON_BASELINE, M10_7_BASELINE)
    assert M10_7_BASELINE.swing == SwingCompositeParameters()
    assert V2_ALL_ON_BASELINE.v2 is not None and V2_ALL_ON_BASELINE.v2.vol_target_annual
    # Every table of the fundamentals set carries its references too.
    assert M16_FUNDAMENTALS[:2] == (D13_PAPER_BASELINE, M10_7_BASELINE)


def test_a7_is_the_volatility_leg_alone_on_m10_7_defaults() -> None:
    assert A7_ARM.swing is not None
    assert A7_ARM.swing.weight_volatility == Decimal("-1")
    weights = {f.name: getattr(A7_ARM.swing, f.name) for f in fields(A7_ARM.swing)}
    legs = {k: v for k, v in weights.items() if k.startswith("weight_")}
    assert {k for k, v in legs.items() if v != _ZERO} == {"weight_volatility"}
    # Undo the legs and nothing else differs from M10.7.
    undone = replace(
        A7_ARM.swing,
        weight_high=Decimal("1"),
        weight_delivery=Decimal("1"),
        weight_momentum=Decimal("1"),
        weight_volatility=_ZERO,
    )
    assert undone == SwingCompositeParameters()


@pytest.mark.parametrize(
    ("arm_set", "entries", "resolve"),
    [
        ("m16", M16_ALL_WINDOW, m16_arms),
        ("m16-fundamentals", M16_FUNDAMENTALS, m16_fundamentals_arms),
    ],
)
def test_an_incomplete_set_raises_naming_every_missing_option(
    arm_set: str, entries: tuple[object, ...], resolve: object
) -> None:
    # Written to hold both before and after M16.1-M16.3 land: either every option exists and the
    # set resolves to its labels, or the lookup raises and names each missing option.
    missing = missing_options(entries)  # type: ignore[arg-type]
    if missing:
        with pytest.raises(M16ArmError) as caught:
            ARM_SETS[arm_set]
        for item in missing:
            assert item in str(caught.value)
    else:
        arms = ARM_SETS[arm_set]
        assert [a.label for a in arms] == [e.label for e in entries]  # type: ignore[attr-defined]
        assert resolve() == arms  # type: ignore[operator]


def test_an_option_arm_is_its_reference_plus_the_option_and_nothing_else() -> None:
    # A stand-in with an option that exists today (the M14.5 switch), to pin the mechanics.
    arm = OptionArm(
        label="stand-in",
        reference=D13_PAPER_BASELINE,
        note="n",
        options=(("regime_daily_reentry", True),),
        owner="test",
    ).resolve()
    assert arm.reference == D13_PAPER_BASELINE.label
    paper = D13_PAPER_BASELINE.v2
    assert paper is not None
    assert arm.v2 == replace(paper, regime_daily_reentry=True)
    assert arm.v2 != paper


def test_a_missing_option_names_its_owner() -> None:
    entry = OptionArm(
        label="needs code",
        reference=M10_7_BASELINE,
        note="n",
        options=(("weight_not_yet_there", Decimal("1")),),
        owner="M16.9",
    )
    assert entry.missing() == ("weight_not_yet_there",)
    with pytest.raises(M16ArmError, match=r"weight_not_yet_there.*M16\.9"):
        entry.resolve()


def test_an_option_at_its_default_is_refused() -> None:
    entry = OptionArm(
        label="no-op",
        reference=D13_PAPER_BASELINE,
        note="n",
        options=(("top_n", 20),),
        owner="test",
    )
    with pytest.raises(M16ArmError, match="unchanged"):
        entry.resolve()


def test_resolvable_arms_list_the_missing_rather_than_drop_them() -> None:
    arms, missing = resolvable_m16_arms()
    labels = {a.label for a in arms} | set(missing)
    every = {e.label for e in (*M16_ALL_WINDOW, *M16_FUNDAMENTALS)}
    assert labels == every
    assert not {a.label for a in arms} & set(missing)
    assert A7_ARM in arms and D13_PAPER_BASELINE in arms


def test_fundamentals_set_refuses_windows_before_fundamentals_open(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    assert ARM_SET_UNITS["m16-fundamentals"] == ("six-year", "wf-verification")
    code = main(
        ["--out", str(tmp_path / "out"), "--arm-set", "m16-fundamentals", "--units", "decade"]
    )
    assert code == 2
    assert "runs only on units six-year, wf-verification" in capsys.readouterr().err
