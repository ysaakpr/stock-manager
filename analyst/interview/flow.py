"""A2: the §5.1 INTERVIEW flow — elicit the case's inputs, then RECOMMEND the §5.2 policy set.

§5.1 splits the analyst's opening act in two. First it *elicits*: the capital plan (SIP amount and
day-of-month), the horizon, the theme, the risk appetite and the exclusions — the small set of facts
only the human can supply. Then it *recommends*: from those facts it derives the seven ratified
policies of §5.2 (`analyst.cases.policies`), each with the reasoning that ties the recommendation to
what was stated, so a reviewer sees not just "dial 30%" but "dial 30% because you said AGGRESSIVE".

This module owns both halves and nothing else — the assembly of the recommendations, the universe
and the theses into one ratifiable `Proposal` is `proposal.py`. Three things it guarantees:

* **Every §5.2 policy is derived, none is left blank.** `recommend_policies` returns all seven
  policy objects populated (`RecommendedPolicies`), so a proposal built from it cannot be missing a
  dial, a set of rails or a cash policy — the pydantic models refuse a partial one, and the reviewer
  ratifies a complete document (acceptance 1).

* **The dial and the rails trace to the stated risk appetite, on the record.** Each recommendation
  carries a `Recommendation` note naming the input it was derived from, the value stated, the value
  chosen, and why — the rotation dial from the stated `risk_appetite`, the risk rails from the
  stated `concentration_tolerance` (which itself defaults from the risk appetite when not separately
  stated, and the note says so). The mapping from a stated level to a policy is a deterministic
  table, not an LLM judgement, so the same interview always recommends the same policies
  (acceptance 2).

* **Elicitation fails loud on a missing or unparseable answer.** `conduct_interview` turns a
  scripted transcript of raw answers into a validated `InterviewAnswers`, and a missing answer or an
  unparseable one raises with the field named — an interview that silently defaulted a risk appetite
  would recommend policies the human never stated (CLAUDE.md: fail loud and specific).

Money is `Decimal` (the SIP amount is `Money`, never a float — CLAUDE.md). Percentages recommended
into the rails are exact `Decimal`s for the same reason `policies.py` insists on them: a rail off by
a float epsilon is a rail that did not hold. Nothing here reads a clock, a database or the network —
the recommendation is a pure function of the elicited answers, which is what lets a test assert on
it and a replay reproduce it (§8.3.3, B10).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, field_validator

from analyst.cases.policies import (
    CapitalPlan,
    CashPolicy,
    ExitMenu,
    ExitStrategy,
    HorizonAndBenchmarks,
    MonitoringCadence,
    RiskRails,
    RotationDial,
    T2Cadence,
    TriggerSensitivity,
)
from analyst.journal import Money

__all__ = [
    "DEFAULT_BENCHMARK_PRIMARY",
    "DEFAULT_PARKING_ISIN",
    "DEFAULT_PARKING_SYMBOL",
    "INTERVIEW_QUESTIONS",
    "ConcentrationTolerance",
    "IncompleteInterviewError",
    "InterviewAnswers",
    "InterviewError",
    "InterviewField",
    "InterviewParseError",
    "InterviewQuestion",
    "Recommendation",
    "RecommendedPolicies",
    "RiskAppetite",
    "conduct_interview",
    "recommend_capital_plan",
    "recommend_cash_policy",
    "recommend_exit_menu",
    "recommend_horizon",
    "recommend_monitoring",
    "recommend_policies",
    "recommend_rails",
    "recommend_rotation_dial",
]

#: The broad-market total-return benchmark every case is measured against unless the human names
#: another. §5.2's primary benchmark; the secondary is the theme proxy, which is theme-specific.
DEFAULT_BENCHMARK_PRIMARY: Final[str] = "NIFTY-TRI"

#: The liquid ETF idle cash parks in by default (§5.2/§5.6, decision #10). Identified by ISIN
#: because the cash leg is an on-exchange order like any other (invariant #2); the symbol is the
#: human-readable label for the proposal. LIQUIDBEES' ISIN, overridable per case.
DEFAULT_PARKING_ISIN: Final[str] = "INF204KB14I2"
DEFAULT_PARKING_SYMBOL: Final[str] = "LIQUIDBEES"


class InterviewError(Exception):
    """Base for every interview failure, so a caller can catch the module."""


class IncompleteInterviewError(InterviewError):
    """A required answer was not supplied, so the interview cannot be completed.

    Names the missing field: an interview that defaulted a risk appetite silently would recommend a
    dial and rails the human never stated, which is exactly the un-consented policy the ratification
    act exists to prevent (§5.1).
    """


class InterviewParseError(InterviewError):
    """An answer was supplied but could not be parsed into the value its field requires.

    Distinct from `IncompleteInterviewError`: the human answered, but "sometime next decade" is not
    a horizon in years and "quite risky" is not one of the stated risk levels. Fail loud with the
    field and the raw answer rather than guessing (CLAUDE.md).
    """


class RiskAppetite(StrEnum):
    """How much volatility and rotation the human is willing to run (§5.1's "risk appetite").

    The dial derives from this: a higher appetite buys a larger tactical sleeve (§5.5). Three
    levels, not a slider, because it is a stated preference the human ratifies, not a tuned
    parameter — and a closed vocabulary is what lets the recommendation be a deterministic table.
    """

    CONSERVATIVE = "CONSERVATIVE"
    """Prefer stability: a small tactical sleeve, tight drawdown review, watchful monitoring."""

    MODERATE = "MODERATE"
    """A balanced sleeve and standard triggers — the middle of the dial."""

    AGGRESSIVE = "AGGRESSIVE"
    """Accept volatility for return: the largest tactical sleeve and the widest drawdown band."""


class ConcentrationTolerance(StrEnum):
    """How concentrated the human will let the book get (§5.1's risk appetite, the rails facet).

    §5.2's risk rails — max position %, max sector %, minimum holdings — are the concentration
    policy, so they derive from this rather than from the rotation appetite: a person can want an
    aggressive sleeve and still refuse to hold 20% in one name. When it is not separately stated it
    defaults from `RiskAppetite` (a more aggressive appetite tolerates more concentration), and the
    recommendation note records that it did.
    """

    LOW = "LOW"
    """Diversify hard: small position and sector caps, many holdings."""

    MEDIUM = "MEDIUM"
    """A balanced concentration profile — the §5.2 reference case's 15% / 35% / 8."""

    HIGH = "HIGH"
    """Accept concentration: larger position and sector caps, fewer holdings."""


#: A stated risk appetite implies a concentration tolerance when the human does not separately state
#: one — more appetite for volatility travels with more tolerance for concentration. Kept explicit
#: (not `RiskAppetite`'s ordinal) so the mapping is auditable and can be changed without touching
#: the enum order.
_CONCENTRATION_FROM_APPETITE: Final[Mapping[RiskAppetite, ConcentrationTolerance]] = {
    RiskAppetite.CONSERVATIVE: ConcentrationTolerance.LOW,
    RiskAppetite.MODERATE: ConcentrationTolerance.MEDIUM,
    RiskAppetite.AGGRESSIVE: ConcentrationTolerance.HIGH,
}


class InterviewField(StrEnum):
    """The facts §5.1 elicits — the keys of a scripted interview transcript.

    Ordered as the interview asks them. `SIP_AMOUNT` through `RISK_APPETITE` are required; the rest
    refine the recommendation and default sensibly, so a minimal interview still yields a complete
    proposal.
    """

    SIP_AMOUNT = "sip_amount"
    SIP_DAY = "sip_day"
    HORIZON = "horizon"
    THEME = "theme"
    RISK_APPETITE = "risk_appetite"
    CONCENTRATION = "concentration"
    EXCLUSIONS = "exclusions"
    BENCHMARK_SECONDARY = "benchmark_secondary"
    TOP_UP = "top_up"


@dataclass(frozen=True, slots=True)
class InterviewQuestion:
    """One question the interview asks: the field it fills, its prompt, and whether it is required.

    A `required` question with no answer fails the interview; an optional one falls back to a
    documented default. Carried as data (not hardcoded prose in `conduct_interview`) so the
    ratification UX (M5.8) can render the same questions the flow parses.
    """

    field: InterviewField
    prompt: str
    required: bool


#: The §5.1 interview script, in order. The five required questions are exactly §5.1's list —
#: capital plan (amount + day), horizon, theme, risk appetite; exclusions and the refinements are
#: optional.
INTERVIEW_QUESTIONS: Final[tuple[InterviewQuestion, ...]] = (
    InterviewQuestion(
        InterviewField.SIP_AMOUNT, "How much will you invest each instalment (rupees)?", True
    ),
    InterviewQuestion(
        InterviewField.SIP_DAY, "On which day of the month does the instalment fall (1-28)?", True
    ),
    InterviewQuestion(InterviewField.HORIZON, "Over how many years are you investing?", True),
    InterviewQuestion(InterviewField.THEME, "What theme do you want to express?", True),
    InterviewQuestion(
        InterviewField.RISK_APPETITE,
        "What is your risk appetite (conservative / moderate / aggressive)?",
        True,
    ),
    InterviewQuestion(
        InterviewField.CONCENTRATION,
        "How concentrated may the book get (low / medium / high)? "
        "Leave blank to match your risk appetite.",
        False,
    ),
    InterviewQuestion(
        InterviewField.EXCLUSIONS,
        "Anything to exclude (ISINs, sectors, keywords), comma-separated?",
        False,
    ),
    InterviewQuestion(
        InterviewField.BENCHMARK_SECONDARY,
        "A theme benchmark to measure against, besides the broad market?",
        False,
    ),
    InterviewQuestion(
        InterviewField.TOP_UP, "Any discretionary top-up rule, e.g. 'bonus in April'?", False
    ),
)


class InterviewAnswers(BaseModel):
    """The validated result of the §5.1 interview — everything the recommendation is derived from.

    What it does: holds the elicited capital plan, horizon, theme, risk profile and exclusions in
    typed, validated form.
    What it assumes: `concentration_tolerance` is optional — when None the recommendation derives it
    from `risk_appetite` and records that it did.
    What it never does: hold money as a float (`sip_amount_inr` is `Money`), or carry a blank theme
    or exclusion — a proposal derived from a blank input would be un-reviewable.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    sip_amount_inr: Money = Field(gt=0, description="Rupees per instalment. Decimal, never float.")
    sip_day_of_month: int = Field(
        ge=1, le=28, description="Nominal instalment day, 1..28 so it exists in February."
    )
    horizon_years: int = Field(gt=0, le=50, description="Target holding horizon, in years.")
    theme: str = Field(
        min_length=1, description="The theme the case expresses, e.g. 'AI/Robotics'."
    )
    risk_appetite: RiskAppetite = Field(description="Stated appetite — drives the rotation dial.")
    concentration_tolerance: ConcentrationTolerance | None = Field(
        default=None,
        description="Stated concentration tolerance — drives the rails; defaults from appetite.",
    )
    exclusions: tuple[str, ...] = Field(
        default=(), description="ISINs, sectors or keywords the human refuses to hold."
    )
    benchmark_primary: str = Field(
        default=DEFAULT_BENCHMARK_PRIMARY,
        min_length=1,
        description="Broad-market total-return benchmark (§5.2).",
    )
    benchmark_secondary: str | None = Field(
        default=None, min_length=1, description="Theme proxy benchmark; None if none stated."
    )
    top_up_rule: str | None = Field(
        default=None, min_length=1, description="Discretionary top-up rule, free text."
    )

    @field_validator("theme")
    @classmethod
    def _theme_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("a case must state a theme; a themeless universe cannot be mapped")
        return value.strip()

    @field_validator("exclusions")
    @classmethod
    def _exclusions_clean(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Strip, drop blanks, de-duplicate case-insensitively, and order — so the same exclusions
        typed in a different order do not change the proposal's content hash.
        """
        seen: dict[str, str] = {}
        for raw in value:
            token = raw.strip()
            if token and token.casefold() not in seen:
                seen[token.casefold()] = token
        return tuple(sorted(seen.values()))

    @property
    def resolved_concentration(self) -> ConcentrationTolerance:
        """The concentration tolerance in force — stated, or derived from the risk appetite."""
        if self.concentration_tolerance is not None:
            return self.concentration_tolerance
        return _CONCENTRATION_FROM_APPETITE[self.risk_appetite]

    @property
    def concentration_was_derived(self) -> bool:
        """Whether the concentration tolerance was derived from the appetite rather than stated."""
        return self.concentration_tolerance is None


def conduct_interview(responses: Mapping[InterviewField, str]) -> InterviewAnswers:
    """Turn a scripted interview transcript into validated `InterviewAnswers` (§5.1's elicit step).

    What it does: reads the raw answer for each `InterviewField`, parses it to the type the field
    needs, and builds an `InterviewAnswers`. A required field with no answer raises
    `IncompleteInterviewError`; an answer that will not parse raises `InterviewParseError`; both
    name the field.
    What it assumes: `responses` maps a field to the human's raw answer as a string — the shape a UI
    or a test transcript produces. A missing optional field falls back to its documented default.
    What it never does: default a required answer, or reach the network — elicitation is pure
    parsing of what the human said.
    """
    missing = [
        question.field
        for question in INTERVIEW_QUESTIONS
        if question.required and not _answered(responses, question.field)
    ]
    if missing:
        raise IncompleteInterviewError(
            "the interview is missing required answers: "
            + ", ".join(field.value for field in missing)
            + " (§5.1 elicits the capital plan, horizon, theme and risk appetite)"
        )

    payload: dict[str, object] = {
        "sip_amount_inr": _parse_decimal(responses, InterviewField.SIP_AMOUNT),
        "sip_day_of_month": _parse_int(responses, InterviewField.SIP_DAY),
        "horizon_years": _parse_int(responses, InterviewField.HORIZON),
        "theme": responses[InterviewField.THEME].strip(),
        "risk_appetite": _parse_enum(responses, InterviewField.RISK_APPETITE, RiskAppetite),
    }
    if _answered(responses, InterviewField.CONCENTRATION):
        payload["concentration_tolerance"] = _parse_enum(
            responses, InterviewField.CONCENTRATION, ConcentrationTolerance
        )
    if _answered(responses, InterviewField.EXCLUSIONS):
        payload["exclusions"] = _parse_list(responses[InterviewField.EXCLUSIONS])
    if _answered(responses, InterviewField.BENCHMARK_SECONDARY):
        payload["benchmark_secondary"] = responses[InterviewField.BENCHMARK_SECONDARY].strip()
    if _answered(responses, InterviewField.TOP_UP):
        payload["top_up_rule"] = responses[InterviewField.TOP_UP].strip()

    try:
        return InterviewAnswers.model_validate(payload)
    except ValueError as exc:  # pydantic ValidationError is a ValueError
        raise InterviewParseError(
            f"the interview answers do not form a valid case input: {exc}"
        ) from exc


# ── the recommendation: a note plus a policy, one per §5.2 row ───────────────────────────────────


class Recommendation(BaseModel):
    """Why one policy was recommended — the recorded reasoning behind a §5.2 recommendation.

    What it does: pins a recommendation to the stated input it was derived from (`traced_from`), the
    value the human stated (`stated_value`), the value chosen (`recommended`), and the one-line
    reasoning — so a reviewer sees a recommendation's provenance, not just its value (acceptance 2).
    What it never does: carry the policy object itself; the policy travels alongside in
    `RecommendedPolicies`. This is the audit note, deliberately strings so it renders and hashes
    plainly.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy: str = Field(min_length=1, description="Which §5.2 policy, e.g. 'rotation_dial'.")
    traced_from: str = Field(
        min_length=1, description="The stated input it derives from, e.g. 'risk_appetite'."
    )
    stated_value: str = Field(
        min_length=1, description="What the human stated, verbatim as a label."
    )
    recommended: str = Field(min_length=1, description="The value recommended, as a label.")
    reasoning: str = Field(min_length=1, description="One line: why this input yields this value.")


@dataclass(frozen=True, slots=True)
class RecommendedPolicies:
    """The seven §5.2 policies recommended from an interview, each with its reasoning.

    Every §5.2 row is present and populated (the type has no optional policy), so a proposal built
    from this cannot be missing one (acceptance 1). `recommendations` holds one `Recommendation`
    per policy, in §5.2 order, so the reviewer sees the whole derivation.
    """

    capital_plan: CapitalPlan
    horizon: HorizonAndBenchmarks
    rotation_dial: RotationDial
    rails: RiskRails
    exit_menu: ExitMenu
    cash_policy: CashPolicy
    monitoring: MonitoringCadence
    recommendations: tuple[Recommendation, ...]


#: Rotation dial (tactical sleeve %) per stated risk appetite (§5.5). AGGRESSIVE → 30 is the §5.2
#: reference case (B9). Kept as a table so the recommendation is deterministic and auditable.
_DIAL_BY_APPETITE: Final[Mapping[RiskAppetite, Decimal]] = {
    RiskAppetite.CONSERVATIVE: Decimal("10"),
    RiskAppetite.MODERATE: Decimal("20"),
    RiskAppetite.AGGRESSIVE: Decimal("30"),
}

#: Peak-to-trough drawdown that forces a review, per appetite (§5.2's "-25%" is AGGRESSIVE). A
#: magnitude in 0..100, direction in the field name (matches `RiskRails.drawdown_review_pct`).
_DRAWDOWN_BY_APPETITE: Final[Mapping[RiskAppetite, Decimal]] = {
    RiskAppetite.CONSERVATIVE: Decimal("15"),
    RiskAppetite.MODERATE: Decimal("20"),
    RiskAppetite.AGGRESSIVE: Decimal("25"),
}

#: T1 trigger sensitivity per appetite (§5.4/§5.2). A more cautious human watches more closely; the
#: §5.2 reference (AGGRESSIVE) runs STANDARD triggers.
_SENSITIVITY_BY_APPETITE: Final[Mapping[RiskAppetite, TriggerSensitivity]] = {
    RiskAppetite.CONSERVATIVE: TriggerSensitivity.HIGH,
    RiskAppetite.MODERATE: TriggerSensitivity.STANDARD,
    RiskAppetite.AGGRESSIVE: TriggerSensitivity.STANDARD,
}


@dataclass(frozen=True, slots=True)
class _RailProfile:
    """The concentration caps a `ConcentrationTolerance` maps to (§5.2 risk rails)."""

    max_position_pct: Decimal
    max_sector_pct: Decimal
    min_holdings: int


#: Concentration caps per tolerance. MEDIUM → 15% / 35% / 8 is the §5.2 reference case (B9). Each
#: profile satisfies `RiskRails`' own consistency rule (position x holdings >= 100, sector >=
#: position), so a recommended rail set never rejects itself.
_RAILS_BY_CONCENTRATION: Final[Mapping[ConcentrationTolerance, _RailProfile]] = {
    ConcentrationTolerance.LOW: _RailProfile(Decimal("10"), Decimal("25"), 12),
    ConcentrationTolerance.MEDIUM: _RailProfile(Decimal("15"), Decimal("35"), 8),
    ConcentrationTolerance.HIGH: _RailProfile(Decimal("20"), Decimal("40"), 5),
}

#: A single order may not exceed this many months of SIP in rupees — the fat-finger cap of §5.2's
#: per-order sanity rails, expressed relative to the capital plan so it scales with the case.
_ORDER_VALUE_SIP_MULTIPLE: Final[int] = 12

#: Sessions proceeds may sit in the liquid ETF before deployment is forced (§5.6). A short leash so
#: cash does not idle, but long enough to wait for a valid trigger.
_DEPLOY_WITHIN_SESSIONS: Final[int] = 5


def recommend_rotation_dial(answers: InterviewAnswers) -> tuple[RotationDial, Recommendation]:
    """Recommend the §5.2 rotation dial from the stated risk appetite (acceptance 2)."""
    tactical_pct = _DIAL_BY_APPETITE[answers.risk_appetite]
    dial = RotationDial(tactical_pct=tactical_pct)
    note = Recommendation(
        policy="rotation_dial",
        traced_from="risk_appetite",
        stated_value=answers.risk_appetite.value,
        recommended=f"tactical_pct={tactical_pct}",
        reasoning=(
            f"a {answers.risk_appetite.value} appetite sizes the tactical sleeve at {tactical_pct}%"
            f" of case capital; the core takes the remaining {dial.core_pct}% (§5.5)"
        ),
    )
    return dial, note


def recommend_rails(answers: InterviewAnswers) -> tuple[RiskRails, Recommendation]:
    """Recommend the §5.2 risk rails from the concentration tolerance (acceptance 2).

    The rails are the concentration policy, so they derive from `resolved_concentration`. When that
    was not separately stated it came from the risk appetite, and the note says so — so the rails
    trace to the stated risk appetite either way.
    """
    concentration = answers.resolved_concentration
    profile = _RAILS_BY_CONCENTRATION[concentration]
    drawdown = _DRAWDOWN_BY_APPETITE[answers.risk_appetite]
    order_cap = answers.sip_amount_inr * _ORDER_VALUE_SIP_MULTIPLE
    rails = RiskRails(
        max_position_pct=profile.max_position_pct,
        max_sector_pct=profile.max_sector_pct,
        min_holdings=profile.min_holdings,
        drawdown_review_pct=drawdown,
        max_order_value_inr=order_cap,
        max_order_pct_of_case=profile.max_position_pct,
    )
    if answers.concentration_was_derived:
        source = (
            f"concentration tolerance {concentration.value} (derived from the stated "
            f"{answers.risk_appetite.value} risk appetite)"
        )
    else:
        source = f"the stated concentration tolerance {concentration.value}"
    note = Recommendation(
        policy="risk_rails",
        traced_from="concentration_tolerance",
        stated_value=concentration.value,
        recommended=(
            f"max_position={profile.max_position_pct}% max_sector={profile.max_sector_pct}% "
            f"min_holdings={profile.min_holdings} drawdown_review={drawdown}%"
        ),
        reasoning=(
            f"{source} caps a single holding at {profile.max_position_pct}%, a sector at "
            f"{profile.max_sector_pct}% and requires at least {profile.min_holdings} holdings; the "
            f"{answers.risk_appetite.value} appetite sets the drawdown trigger at {drawdown}%"
            f" and the per-order cap at {_ORDER_VALUE_SIP_MULTIPLE}x the SIP (§5.2)"
        ),
    )
    return rails, note


def recommend_exit_menu(answers: InterviewAnswers) -> tuple[ExitMenu, Recommendation]:
    """Recommend the §5.6 exit menu: staged by default, immediate unlocked on integrity events."""
    menu = ExitMenu(
        allowed=(
            ExitStrategy.STAGED,
            ExitStrategy.IMMEDIATE,
            ExitStrategy.EXIT_AND_REDEPLOY,
        ),
        default=ExitStrategy.STAGED,
        immediate_allowed_on=("integrity",),
    )
    note = Recommendation(
        policy="exit_menu",
        traced_from="§5.6 exit policy",
        stated_value="standard",
        recommended="default=STAGED, immediate on integrity",
        reasoning=(
            "staged exit over 2-3 sessions is the default (it respects liquidity); an immediate "
            "exit is unlocked only on an integrity break, where waiting is the risk (§5.6)"
        ),
    )
    return menu, note


def recommend_cash_policy(
    answers: InterviewAnswers,
    *,
    parking_isin: str = DEFAULT_PARKING_ISIN,
    parking_symbol: str = DEFAULT_PARKING_SYMBOL,
) -> tuple[CashPolicy, Recommendation]:
    """Recommend the §5.6 cash policy: park in a liquid ETF, deploy at least one instalment."""
    policy = CashPolicy(
        parking_isin=parking_isin,
        parking_symbol=parking_symbol,
        deploy_within_sessions=_DEPLOY_WITHIN_SESSIONS,
        min_deployment_inr=answers.sip_amount_inr,
    )
    note = Recommendation(
        policy="cash_policy",
        traced_from="capital_plan",
        stated_value=f"SIP {answers.sip_amount_inr}",
        recommended=(
            f"park in {parking_symbol}, deploy within {_DEPLOY_WITHIN_SESSIONS} sessions, "
            f"min tranche {answers.sip_amount_inr}"
        ),
        reasoning=(
            f"idle cash and proceeds park in {parking_symbol} same-day and deploy on a valid "
            f"trigger within {_DEPLOY_WITHIN_SESSIONS} sessions; a tranche below one instalment "
            f"({answers.sip_amount_inr}) waits rather than trade sub-scale (§5.6, decision #10)"
        ),
    )
    return policy, note


def recommend_monitoring(answers: InterviewAnswers) -> tuple[MonitoringCadence, Recommendation]:
    """Recommend the §5.4 monitoring cadence: T2 by horizon, T1 sensitivity by appetite."""
    cadence = T2Cadence.QUARTERLY if answers.horizon_years >= 10 else T2Cadence.MONTHLY
    sensitivity = _SENSITIVITY_BY_APPETITE[answers.risk_appetite]
    monitoring = MonitoringCadence(t2_cadence=cadence, t1_sensitivity=sensitivity)
    note = Recommendation(
        policy="monitoring",
        traced_from="risk_appetite",
        stated_value=answers.risk_appetite.value,
        recommended=f"T2 {cadence.value}, T1 {sensitivity.value}",
        reasoning=(
            f"a {answers.horizon_years}-year horizon sets the deep review to {cadence.value}; a "
            f"{answers.risk_appetite.value} appetite runs {sensitivity.value} T1 triggers (§5.4)"
        ),
    )
    return monitoring, note


def recommend_capital_plan(answers: InterviewAnswers) -> tuple[CapitalPlan, Recommendation]:
    """The §5.2 capital plan — taken verbatim from what the human stated, with a recording note."""
    plan = CapitalPlan(
        sip_amount_inr=answers.sip_amount_inr,
        day_of_month=answers.sip_day_of_month,
        top_up_rule=answers.top_up_rule,
    )
    note = Recommendation(
        policy="capital_plan",
        traced_from="capital_plan",
        stated_value=f"{answers.sip_amount_inr} on day {answers.sip_day_of_month}",
        recommended=f"{answers.sip_amount_inr} on day {answers.sip_day_of_month}",
        reasoning=(
            f"the capital plan is the stated instalment: {answers.sip_amount_inr} on day "
            f"{answers.sip_day_of_month} of each month"
            + (f", top-up rule '{answers.top_up_rule}'" if answers.top_up_rule else "")
        ),
    )
    return plan, note


def recommend_horizon(answers: InterviewAnswers) -> tuple[HorizonAndBenchmarks, Recommendation]:
    """The §5.2 horizon and benchmark pair — the stated horizon, the broad market plus theme proxy.

    The secondary benchmark is the theme proxy: the stated one if the human gave one, otherwise a
    label derived from the theme so the pair is always populated (§5.2 measures against both).
    """
    secondary = answers.benchmark_secondary or f"{answers.theme} proxy"
    horizon = HorizonAndBenchmarks(
        horizon_years=answers.horizon_years,
        benchmark_primary=answers.benchmark_primary,
        benchmark_secondary=secondary,
    )
    note = Recommendation(
        policy="horizon",
        traced_from="horizon",
        stated_value=f"{answers.horizon_years} years",
        recommended=(f"{answers.horizon_years} years vs {answers.benchmark_primary} + {secondary}"),
        reasoning=(
            f"a {answers.horizon_years}-year horizon measured against the broad market "
            f"({answers.benchmark_primary}) and the theme proxy ({secondary}), so the case is "
            "judged both on 'was equity right' and 'was this theme right' (§5.2)"
        ),
    )
    return horizon, note


def recommend_policies(
    answers: InterviewAnswers,
    *,
    parking_isin: str = DEFAULT_PARKING_ISIN,
    parking_symbol: str = DEFAULT_PARKING_SYMBOL,
) -> RecommendedPolicies:
    """Recommend all seven §5.2 policies from the interview, each with its recorded reasoning.

    What it does: derives every §5.2 policy from `answers` — the dial from the risk appetite, the
    rails from the concentration tolerance, and the rest per §5.2/§5.4/§5.6 — and returns them with
    one `Recommendation` note apiece, in §5.2 order (acceptance 1, 2).
    What it assumes: `answers` is a validated `InterviewAnswers`. The parking instrument defaults to
    the liquid ETF but is overridable per case.
    What it never does: leave a policy blank or reach an LLM — the mapping is a deterministic table,
    so the same interview always recommends the same policies (§8.3.3).
    """
    capital_plan, capital_note = recommend_capital_plan(answers)
    horizon, horizon_note = recommend_horizon(answers)
    dial, dial_note = recommend_rotation_dial(answers)
    rails, rails_note = recommend_rails(answers)
    exit_menu, exit_note = recommend_exit_menu(answers)
    cash_policy, cash_note = recommend_cash_policy(
        answers, parking_isin=parking_isin, parking_symbol=parking_symbol
    )
    monitoring, monitoring_note = recommend_monitoring(answers)
    return RecommendedPolicies(
        capital_plan=capital_plan,
        horizon=horizon,
        rotation_dial=dial,
        rails=rails,
        exit_menu=exit_menu,
        cash_policy=cash_policy,
        monitoring=monitoring,
        recommendations=(
            capital_note,
            horizon_note,
            dial_note,
            rails_note,
            exit_note,
            cash_note,
            monitoring_note,
        ),
    )


# ── parsing helpers ──────────────────────────────────────────────────────────────────────────────


def _answered(responses: Mapping[InterviewField, str], field: InterviewField) -> bool:
    """Whether a field has a non-blank answer in the transcript."""
    value = responses.get(field)
    return value is not None and value.strip() != ""


def _parse_decimal(responses: Mapping[InterviewField, str], field: InterviewField) -> Decimal:
    """Parse a rupee amount into an exact `Decimal`, or raise `InterviewParseError`.

    Parsed from the string the human typed (not `float`), so the SIP amount keeps its exact value
    (CLAUDE.md: money is Decimal, never float).
    """
    raw = responses[field].strip().replace(",", "")
    try:
        return Decimal(raw)
    except (ValueError, ArithmeticError) as exc:
        raise InterviewParseError(
            f"answer to {field.value} ({responses[field]!r}) is not a rupee amount"
        ) from exc


def _parse_int(responses: Mapping[InterviewField, str], field: InterviewField) -> int:
    """Parse a whole number, or raise `InterviewParseError`."""
    raw = responses[field].strip()
    try:
        return int(raw)
    except ValueError as exc:
        raise InterviewParseError(
            f"answer to {field.value} ({responses[field]!r}) is not a whole number"
        ) from exc


def _parse_enum[E: StrEnum](
    responses: Mapping[InterviewField, str], field: InterviewField, enum: type[E]
) -> E:
    """Parse a stated level into `enum`, case-insensitively, or raise `InterviewParseError`."""
    raw = responses[field].strip()
    for member in enum:
        if member.value.casefold() == raw.casefold():
            return member
    allowed = ", ".join(member.value for member in enum)
    raise InterviewParseError(
        f"answer to {field.value} ({responses[field]!r}) is not one of: {allowed}"
    )


def _parse_list(raw: str) -> tuple[str, ...]:
    """Split a comma/semicolon-separated answer into tokens (blanks dropped)."""
    parts = raw.replace(";", ",").split(",")
    return tuple(token.strip() for token in parts if token.strip())
