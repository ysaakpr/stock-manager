"""M16.0: the M16 arm sets name their parameter presets, and fail loudly while one is missing."""

from __future__ import annotations

import sys
import types
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
    PresetArm,
    m16_arms,
    m16_fundamentals_arms,
    missing_presets,
    resolvable_m16_arms,
)
from backtest.policies.swing_composite import SwingCompositeParameters
from backtest.sweep import D13_PAPER_BASELINE, Arm

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
def test_an_incomplete_set_raises_naming_every_missing_preset(
    arm_set: str, entries: tuple[object, ...], resolve: object
) -> None:
    # Written to hold both before and after M16.1-M16.3 land: either every option exists and the
    # set resolves to its labels, or the lookup raises and names each missing option.
    missing = missing_presets(entries)  # type: ignore[arg-type]
    if missing:
        with pytest.raises(M16ArmError) as caught:
            ARM_SETS[arm_set]
        for item in missing:
            assert item in str(caught.value)
    else:
        arms = ARM_SETS[arm_set]
        assert [a.label for a in arms] == [e.label for e in entries]  # type: ignore[attr-defined]
        assert resolve() == arms  # type: ignore[operator]


def _stand_in(
    monkeypatch: pytest.MonkeyPatch,
    preset: object,
    reference: Arm = D13_PAPER_BASELINE,
    options: tuple[tuple[str, object], ...] = (("regime_daily_reentry", True),),
) -> PresetArm:
    module = types.ModuleType("m16_stand_in")
    module.PRESET = preset  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "m16_stand_in", module)
    return PresetArm(
        label="stand-in",
        reference=reference,
        note="n",
        module="m16_stand_in",
        preset="PRESET",
        options=options,
        owner="test",
    )


def test_a_preset_arm_drives_the_preset_against_its_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paper = D13_PAPER_BASELINE.v2
    assert paper is not None
    preset = replace(paper, regime_daily_reentry=True)
    arm = _stand_in(monkeypatch, preset).resolve()
    assert arm.reference == D13_PAPER_BASELINE.label
    assert arm.v2 is preset and arm.swing is None


def test_a_missing_preset_or_module_names_its_owner() -> None:
    for module, preset in (("backtest.m16_arms", "NOT_YET"), ("backtest.not_a_module", "X")):
        entry = PresetArm(
            label="needs code",
            reference=M10_7_BASELINE,
            note="n",
            module=module,
            preset=preset,
            options=(("weight_volatility", Decimal("-1")),),
            owner="M16.9",
        )
        assert entry.missing() == (f"{module}.{preset}",)
        with pytest.raises(M16ArmError, match=rf"{preset}.*M16\.9"):
            entry.resolve()


def test_a_preset_equal_to_its_reference_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(M16ArmError, match="unchanged"):
        _stand_in(monkeypatch, D13_PAPER_BASELINE.v2, options=(("top_n", 20),)).resolve()


def test_a_preset_that_is_not_exactly_the_preregistered_switch_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paper = D13_PAPER_BASELINE.v2
    assert paper is not None
    # The switch on, plus a second change nobody pre-registered.
    drifted = replace(paper, regime_daily_reentry=True, top_n=10, sell_band=30)
    with pytest.raises(M16ArmError, match="with exactly"):
        _stand_in(monkeypatch, drifted).resolve()


def test_an_option_the_parameter_class_lacks_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    paper = D13_PAPER_BASELINE.v2
    assert paper is not None
    preset = replace(paper, regime_daily_reentry=True)
    with pytest.raises(M16ArmError, match="has no option industry_gate_top"):
        _stand_in(monkeypatch, preset, options=(("industry_gate_top", 5),)).resolve()


def test_the_arms_name_the_preregistered_presets_and_switches() -> None:
    # Amendment 1 (c)/(d): A2 is the boolean industry_gate (K = 5 fixed), A3 is residual_ranking.
    entries = [e for e in (*M16_ALL_WINDOW, *M16_FUNDAMENTALS) if isinstance(e, PresetArm)]
    assert {e.label: (e.preset, dict(e.options)) for e in entries} == {
        "D13 + absolute momentum (A1)": ("D13_ABS_MOM", {"absolute_momentum": True}),
        "D13 + industry gate (A2)": ("D13_INDUSTRY_GATE", {"industry_gate": True}),
        "Residual momentum v2 (A3)": ("D13_RESID_MOM", {"residual_ranking": True}),
        "D13 + profitability filter (A4)": ("D13_PROFIT_FILTER", {"profitability_filter": True}),
        "M10.7 + earnings-surprise leg (A5)": (
            "M10_7_EARNINGS_SURPRISE",
            {"weight_earnings_surprise": Decimal("1")},
        ),
    }


def test_a_preset_of_the_wrong_engine_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(M16ArmError, match="SwingCompositeParameters"):
        _stand_in(monkeypatch, SwingCompositeParameters(top_n=10, sell_band=30)).resolve()


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
