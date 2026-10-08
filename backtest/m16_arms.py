"""M16 — the strategy-exploration arms, resolved by preset name against the policies that exist.

``ops/studies/preregistration-m16-2026-10-08.md`` fixes seven arms before any of them ran. Two
need no new code: A6 is a report-side blend of two baselines' saved runs
(``backtest.m16_report.blend``) and A7 is a swing-engine weighting that exists today. A1-A5 need
policy code that M16.1 (momentum v2: A1, A3, A4; PR #98), M16.2 (the industry gate: A2; #97) and
M16.3 (the swing earnings-surprise leg: A5; #96) implement, each publishing a named parameter
preset. This module names those presets (Appendix B of the pre-registration) and imports each only
when its arm is resolved, checking it drives the same parameter class as its reference and differs
from it.

**A missing preset is a loud error, never a silent skip.** Until the owning PR lands,
:func:`m16_arms` raises :class:`M16ArmError` naming every missing preset and the PR that owns
it; a campaign that ran without A1 would report a table with a hole the reader could not see.
:func:`resolvable_m16_arms` is the lenient form the report uses, so baselines and A7 can be rendered
(and tested) before the rest lands, with the missing arms listed rather than dropped.

**Two arm sets, because the pre-registration's windows differ by arm.** ``m16`` holds the
baselines and the arms that run on every window (A1, A2, A3, A7); ``m16-fundamentals`` holds A4
and A5, which read PIT XBRL fundamentals (open 2018-05) and run on the six-year and verification
windows only, beside D13 and M10.7 so each of their tables carries its reference.
:data:`ARM_SET_UNITS` states that restriction where ``backtest.m12_rerun`` enforces it.

What this module never does: build a preset itself, change a baseline's parameters, or
define an arm the pre-registration does not list.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, fields, replace
from decimal import Decimal
from typing import Final

from backtest.campaign import CampaignError
from backtest.policies.swing_composite import SwingCompositeParameters
from backtest.sweep import ARMS, D13_PAPER_BASELINE, Arm

__all__ = [
    "A1_ABSOLUTE_MOMENTUM",
    "A2_INDUSTRY_GATE",
    "A3_RESIDUAL_MOMENTUM_V2",
    "A4_PROFITABILITY",
    "A5_EARNINGS_SURPRISE",
    "A6_BLEND",
    "A7_ARM",
    "A7_LOW_VOL",
    "ARM_SET_UNITS",
    "M10_7_BASELINE",
    "M16_ALL_WINDOW",
    "M16_FUNDAMENTALS",
    "PREREGISTRATION",
    "V2_ALL_ON_BASELINE",
    "M16ArmError",
    "PresetArm",
    "m16_arms",
    "m16_fundamentals_arms",
    "missing_presets",
    "resolvable_m16_arms",
]

PREREGISTRATION: Final = "ops/studies/preregistration-m16-2026-10-08.md"

A1_ABSOLUTE_MOMENTUM: Final = "D13 + absolute momentum (A1)"
A2_INDUSTRY_GATE: Final = "D13 + industry gate (A2)"
A3_RESIDUAL_MOMENTUM_V2: Final = "Residual momentum v2 (A3)"
A4_PROFITABILITY: Final = "D13 + profitability filter (A4)"
A5_EARNINGS_SURPRISE: Final = "M10.7 + earnings-surprise leg (A5)"
A6_BLEND: Final = "50/50 blend of D13 and M10.7 (A6)"
A7_LOW_VOL: Final = "Pure low volatility (A7)"

_FAMILY = "M16 arm"
_ZERO = Decimal("0")
_ONE = Decimal("1")

M10_7_BASELINE: Final = next(arm for arm in ARMS if arm.label == "Swing composite (M10.7)")
V2_ALL_ON_BASELINE: Final = next(arm for arm in ARMS if arm.label == "Momentum v2, all on (M9.5)")


class M16ArmError(CampaignError):
    """An M16 arm names a parameter preset the code does not have yet (or a malformed one)."""


@dataclass(frozen=True, slots=True)
class PresetArm:
    """An M16 arm as a named parameter preset its owning PR publishes (pre-registration App. B).

    ``module`` and ``preset`` name the constant (``backtest.policies.momentum_v2.D13_ABS_MOM``,
    say); it is imported when the arm is resolved, never at import of this module. ``reference``
    is the arm the preset is built on: the preset must equal ``reference`` with exactly
    ``options`` applied (Amendment 1 (d)), so a preset that drifted from the pre-registered switch,
    or a PR that renamed the switch, is refused rather than run. ``owner`` is the task (and PR)
    that publishes it.
    """

    label: str
    reference: Arm
    note: str
    module: str
    preset: str
    options: tuple[tuple[str, object], ...]
    owner: str

    def _reference_params(self) -> object:
        params = self.reference.v2 or self.reference.swing
        if params is None:
            raise M16ArmError(f"{self.label}: its reference drives neither v2 nor the swing engine")
        return params

    def _lookup(self) -> object | None:
        try:
            module = importlib.import_module(self.module)
        except ModuleNotFoundError:
            return None
        return getattr(module, self.preset, None)

    def missing(self) -> tuple[str, ...]:
        """``("module.PRESET",)`` while the preset does not exist yet, else ``()``."""
        return () if self._lookup() is not None else (f"{self.module}.{self.preset}",)

    def resolve(self) -> Arm:
        """The arm, driving the preset; :class:`M16ArmError` if it is missing or malformed."""
        params = self._lookup()
        if params is None:
            raise M16ArmError(
                f"{self.label}: {self.module}.{self.preset} does not exist yet — "
                f"{self.owner} publishes it ({PREREGISTRATION})"
            )
        reference = self._reference_params()
        if type(params) is not type(reference):
            raise M16ArmError(
                f"{self.label}: {self.preset} is a {type(params).__name__}, "
                f"not the {type(reference).__name__} {self.reference.label} drives"
            )
        known = {f.name for f in fields(reference)}  # type: ignore[arg-type]
        unknown = [name for name, _ in self.options if name not in known]
        if unknown:
            raise M16ArmError(
                f"{self.label}: {type(reference).__name__} has no option {', '.join(unknown)} "
                f"({self.owner}; {PREREGISTRATION}, Amendment 1)"
            )
        expected = replace(reference, **dict(self.options))  # type: ignore[type-var]
        if expected == reference:
            raise M16ArmError(
                f"{self.label}: {self.preset} leaves {self.reference.label} unchanged"
            )
        if params != expected:
            raise M16ArmError(
                f"{self.label}: {self.preset} is not {self.reference.label} with exactly "
                f"{dict(self.options)} ({PREREGISTRATION}, Amendment 1)"
            )
        driving = "v2" if self.reference.v2 is not None else "swing"
        return Arm(
            label=self.label,
            family=_FAMILY,
            reference=self.reference.label,
            note=self.note,
            **{driving: params},  # type: ignore[arg-type]
        )


_V2 = "backtest.policies.momentum_v2"

#: A1-A5, by preset name (Appendix B). Behaviour is the pre-registration's §2; the presets are the
#: owning PRs' (#98 M16.1, #97 M16.2, #96 M16.3).
_A1 = PresetArm(
    label=A1_ABSOLUTE_MOMENTUM,
    reference=D13_PAPER_BASELINE,
    note="a slot is held only if the name's 12-1 return beats repo - 0.5% over the same span",
    module=_V2,
    preset="D13_ABS_MOM",
    options=(("absolute_momentum", True),),
    owner="M16.1 (#98)",
)
_A2 = PresetArm(
    label=A2_INDUSTRY_GATE,
    reference=D13_PAPER_BASELINE,
    note="eligible only in an industry mapped to a top-5 sectoral index by 6-1 month return",
    module=_V2,
    preset="D13_INDUSTRY_GATE",
    # A boolean switch; K = 5 is a fixed constant of the gate, not a parameter (Amendment 1 (c)).
    options=(("industry_gate", True),),
    owner="M16.2 (#97)",
)
_A3 = PresetArm(
    label=A3_RESIDUAL_MOMENTUM_V2,
    reference=D13_PAPER_BASELINE,
    note="ranks on round 2's H1 residual momentum instead of 12-1; everything else D13",
    module=_V2,
    preset="D13_RESID_MOM",
    options=(("residual_ranking", True),),
    owner="M16.1 (#98)",
)
_A4 = PresetArm(
    label=A4_PROFITABILITY,
    reference=D13_PAPER_BASELINE,
    note="eligible only with TTM PAT > 0 (PIT XBRL) and a filing at most 200 days old",
    module=_V2,
    preset="D13_PROFIT_FILTER",
    options=(("profitability_filter", True),),
    owner="M16.1 (#98)",
)
_A5 = PresetArm(
    label=A5_EARNINGS_SURPRISE,
    reference=M10_7_BASELINE,
    note="a fourth, equal-weight leg: standardised YoY EPS surprise, live 63 sessions",
    module="backtest.policies.earnings_surprise",
    preset="M10_7_EARNINGS_SURPRISE",
    options=(("weight_earnings_surprise", _ONE),),
    owner="M16.3 (#96)",
)

#: A7 needs no new code: the swing engine's volatility leg alone, sign -1.
A7_ARM: Final = Arm(
    label=A7_LOW_VOL,
    family=_FAMILY,
    reference=M10_7_BASELINE.label,
    note="scores on -1 x trailing volatility alone; M10.7's cadence, band, stop and screen",
    swing=SwingCompositeParameters(
        weight_high=_ZERO,
        weight_delivery=_ZERO,
        weight_momentum=_ZERO,
        weight_volatility=-_ONE,
    ),
)

_BASELINES: tuple[Arm, ...] = (D13_PAPER_BASELINE, V2_ALL_ON_BASELINE, M10_7_BASELINE)

#: What each arm set runs, in table order. Baselines first: every table carries D13.
M16_ALL_WINDOW: tuple[Arm | PresetArm, ...] = (*_BASELINES, _A1, _A2, _A3, A7_ARM)
M16_FUNDAMENTALS: tuple[Arm | PresetArm, ...] = (D13_PAPER_BASELINE, M10_7_BASELINE, _A4, _A5)

#: The units an arm set may run (``backtest.m12_rerun`` refuses others). A4 and A5 read PIT XBRL
#: fundamentals, which open 2018-05: the decade and selection windows would be all-cash noise.
ARM_SET_UNITS: dict[str, tuple[str, ...]] = {"m16-fundamentals": ("six-year", "wf-verification")}


def missing_presets(entries: tuple[Arm | PresetArm, ...]) -> list[str]:
    """Every ``arm: module.PRESET (owner)`` in ``entries`` the code does not have yet."""
    return [
        f"{entry.label}: {name} ({entry.owner})"
        for entry in entries
        if isinstance(entry, PresetArm)
        for name in entry.missing()
    ]


def _resolve(entries: tuple[Arm | PresetArm, ...]) -> tuple[Arm, ...]:
    missing = missing_presets(entries)
    if missing:
        raise M16ArmError(
            "the M16 arm set is incomplete — these presets do not exist yet: "
            + "; ".join(missing)
            + f" ({PREREGISTRATION}, Appendix B)"
        )
    return tuple(e.resolve() if isinstance(e, PresetArm) else e for e in entries)


def m16_arms() -> tuple[Arm, ...]:
    """The ``m16`` arm set: baselines, A1, A2, A3, A7. Raises :class:`M16ArmError` if incomplete."""
    return _resolve(M16_ALL_WINDOW)


def m16_fundamentals_arms() -> tuple[Arm, ...]:
    """The ``m16-fundamentals`` set: D13, M10.7, A4, A5.

    Raises :class:`M16ArmError` while a preset is missing.
    """
    return _resolve(M16_FUNDAMENTALS)


def resolvable_m16_arms() -> tuple[tuple[Arm, ...], tuple[str, ...]]:
    """Every M16 engine arm that resolves today, and the labels of those that do not.

    Never raises for a missing preset; the caller prints the missing labels.
    """
    arms: list[Arm] = []
    missing: list[str] = []
    seen: set[str] = set()
    for entry in (*M16_ALL_WINDOW, *M16_FUNDAMENTALS):
        if entry.label in seen:
            continue
        seen.add(entry.label)
        if isinstance(entry, PresetArm):
            if entry.missing():
                missing.append(entry.label)
                continue
            arms.append(entry.resolve())
        else:
            arms.append(entry)
    return tuple(arms), tuple(missing)
