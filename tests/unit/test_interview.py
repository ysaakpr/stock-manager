"""M5.7: the §5.1 INTERVIEW / case builder — a scripted transcript becomes one ratifiable proposal.

The three claims this task makes, and how each is shown here:

1. **A scripted interview transcript produces a complete proposal with every §5.2 policy
   populated.** `conduct_interview` parses a transcript of raw answers into `InterviewAnswers`, and
   `build_proposal` folds them with the theme map's purity-scored universe (A3) and a §5.3 thesis
   per holding (A4) into a `Proposal` whose `policy_set` carries all seven §5.2 policies, whose
   universe carries purity scores, and whose theses cover every holding.
2. **The recommended dial and rails trace to the stated risk appetite, with recorded reasoning.**
   The rotation dial derives from `risk_appetite` (AGGRESSIVE → 30%) and the rails from the
   concentration tolerance (MEDIUM → 15% / 35% / 8), each with a `Recommendation` note naming the
   input, the stated value and the reasoning; when concentration is not stated it derives from the
   appetite and the note says so.
3. **The proposal is one ratifiable document, not a series of approvals.** The proposal has a single
   `content_hash` over the whole case and a single `ratified_with`; one `Ratification` pins the
   universe, the theses and all seven policies at once, and a ratification for a proposal that
   changed is refused.

Everything is offline: the theme map runs against a `StubLLM` (B4), the theses are built directly,
time is a `FrozenClock` (B10), joins are on ISIN (#2), and money is `Decimal`. No network, no
database, no wall clock.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal

import pytest

from analyst.cases import (
    ExitStrategy,
    Ratification,
    RatificationKind,
    T2Cadence,
    TriggerSensitivity,
)
from analyst.cases.policies import POLICY_FIELDS
from analyst.interview import (
    ConcentrationTolerance,
    EmptyUniverseError,
    IncompleteInterviewError,
    IncompleteProposalError,
    InterviewField,
    InterviewParseError,
    Proposal,
    ProposalError,
    ProposalRatificationMismatchError,
    ProposalStatus,
    RiskAppetite,
    build_proposal,
    conduct_interview,
    recommend_policies,
)
from analyst.llm import Message, Role, StopReason, StubLLM, StubReply, ToolCall, prompt_digest
from analyst.mapper import ThemeMap
from analyst.mapper import engine as mapper_engine
from analyst.thesis import BreakConditionType, EvaluationTier, Thesis, ThesisStatus
from analyst.thesis.models import BreakCondition
from dataplatform.clock import FrozenClock
from dataplatform.query.universe import PitUniverse

THEME = "AI/Robotics"
MODEL = "claude-opus-5"  # on the checked-in price card
AS_OF = date(2026, 9, 2)
CASE_ID = "case-ai-robotics"

COMPUTE = "INE009A01021"
SENSORS = "INE467B01029"
INTEG = "INE040A01034"


# ── building the inputs the interview stitches together (A3 map + A4 theses) ─────────────────────


def _universe(*isins: str) -> PitUniverse:
    filler = {"INE002A01018", "INE030A01027"}
    return PitUniverse(as_of=AS_OF, isins=frozenset({*isins, *filler}))


def _map_arguments() -> dict[str, object]:
    """A well-formed `map_theme` answer for the three candidates."""
    return {
        "value_chain": [
            {"name": "compute & silicon", "description": "AI accelerators and processors"},
            {"name": "sensors & actuators", "description": "perception and motion hardware"},
            {"name": "systems integration", "description": "assembling autonomous systems"},
        ],
        "candidates": [
            {
                "isin": COMPUTE,
                "name": "Compute Silicon Ltd",
                "value_chain_stage": "compute & silicon",
                "evidence": [
                    {
                        "kind": "segment_disclosure",
                        "source": "FY26 annual report segment note",
                        "label": "AI accelerators",
                        "revenue_fraction": "0.6",
                        "expresses_theme": True,
                        "note": None,
                    },
                    {
                        "kind": "segment_disclosure",
                        "source": "FY26 annual report segment note",
                        "label": "Legacy switchgear",
                        "revenue_fraction": "0.4",
                        "expresses_theme": False,
                        "note": None,
                    },
                ],
            },
            {
                "isin": SENSORS,
                "name": "Sensor Actuator Corp",
                "value_chain_stage": "sensors & actuators",
                "evidence": [
                    {
                        "kind": "revenue_mix",
                        "source": "Q3FY26 investor ppt",
                        "label": "Robotics integration",
                        "revenue_fraction": "0.25",
                        "expresses_theme": True,
                        "note": None,
                    },
                    {
                        "kind": "revenue_mix",
                        "source": "Q3FY26 investor ppt",
                        "label": "EPC",
                        "revenue_fraction": "0.75",
                        "expresses_theme": False,
                        "note": None,
                    },
                ],
            },
            {
                "isin": INTEG,
                "name": "Integration Systems Ltd",
                "value_chain_stage": "systems integration",
                "evidence": [
                    {
                        "kind": "segment_disclosure",
                        "source": "FY26 annual report segment note",
                        "label": "Autonomous systems",
                        "revenue_fraction": "0.1",
                        "expresses_theme": True,
                        "note": None,
                    },
                    {
                        "kind": "segment_disclosure",
                        "source": "FY26 annual report segment note",
                        "label": "Industrial services",
                        "revenue_fraction": "0.2",
                        "expresses_theme": False,
                        "note": None,
                    },
                ],
            },
        ],
    }


def _theme_map() -> ThemeMap:
    universe = _universe(COMPUTE, SENSORS, INTEG)
    stub = StubLLM(synthesize_unknown=False)
    brief = mapper_engine._brief(THEME, universe)
    digest = prompt_digest(
        [Message(role=Role.USER, content=brief)],
        model=MODEL,
        tools=[mapper_engine.MAPPER_TOOL],
        system=mapper_engine._SYSTEM,
    )
    stub.register(
        digest,
        StubReply(
            text="mapped",
            tool_calls=(ToolCall(id="c1", name="map_theme", arguments=_map_arguments()),),
            stop_reason=StopReason.TOOL_USE,
        ),
    )
    return mapper_engine.map_theme(stub, theme=THEME, universe=universe, model=MODEL)


def _thesis(isin: str, *, purity: str = "0.6") -> Thesis:
    """A minimal §5.3 thesis in PROPOSAL for a holding, with one falsifiable break condition."""
    return Thesis(
        case_id=CASE_ID,
        isin=isin,
        version=1,
        status=ThesisStatus.PROPOSAL,
        driver="Indian EMS capex cycle x robotics component localization",
        theme_purity=Decimal(purity),
        expected_evidence=("order-book growth >20% YoY",),
        break_conditions=(
            BreakCondition(
                id="BC1",
                type=BreakConditionType.FUNDAMENTAL,
                condition="segment revenue falls two consecutive quarters",
                evaluation_tier=EvaluationTier.T1,
                evaluation="T1 on results filing",
            ),
        ),
    )


def _theses(*isins: str) -> dict[str, Thesis]:
    return {isin: _thesis(isin) for isin in isins}


# ── the scripted transcript ──────────────────────────────────────────────────────────────────────


def _transcript(**overrides: str) -> dict[InterviewField, str]:
    """A full, well-formed §5.1 interview transcript; `overrides` tweak individual answers."""
    base: dict[InterviewField, str] = {
        InterviewField.SIP_AMOUNT: "10000",
        InterviewField.SIP_DAY: "1",
        InterviewField.HORIZON: "5",
        InterviewField.THEME: THEME,
        InterviewField.RISK_APPETITE: "aggressive",
        InterviewField.CONCENTRATION: "medium",
        InterviewField.BENCHMARK_SECONDARY: "NIFTY IT/CPSE blend",
    }
    base.update(overrides)  # type: ignore[arg-type]
    return base


def _proposal(
    *,
    transcript: Mapping[InterviewField, str] | None = None,
    theses: Mapping[str, Thesis] | None = None,
) -> Proposal:
    answers = conduct_interview(dict(transcript) if transcript is not None else _transcript())
    theme_map = _theme_map()
    supplied = dict(theses) if theses is not None else _theses(COMPUTE, SENSORS, INTEG)
    return build_proposal(case_id=CASE_ID, answers=answers, theme_map=theme_map, theses=supplied)


# ── acceptance 1: a scripted transcript → a complete proposal with every §5.2 policy ─────────────


def test_scripted_transcript_produces_complete_proposal() -> None:
    proposal = _proposal()

    assert proposal.case_id == CASE_ID
    assert proposal.theme == THEME
    assert proposal.as_of == AS_OF
    assert proposal.status is ProposalStatus.PROPOSAL

    # Every §5.2 policy is present and populated.
    policies = proposal.policy_set.policies
    assert set(policies) == set(POLICY_FIELDS)
    assert proposal.policy_set.capital_plan.sip_amount_inr == Decimal("10000")
    assert proposal.policy_set.capital_plan.day_of_month == 1
    assert proposal.policy_set.horizon.horizon_years == 5
    # The benchmark pair is populated (§5.2).
    assert proposal.policy_set.horizon.benchmark_primary == "NIFTY-TRI"
    assert proposal.policy_set.horizon.benchmark_secondary == "NIFTY IT/CPSE blend"
    assert proposal.policy_set.rotation_dial.tactical_pct == Decimal("30")
    assert proposal.policy_set.rails.max_position_pct == Decimal("15")
    assert proposal.policy_set.exit_menu.default == ExitStrategy.STAGED
    assert proposal.policy_set.cash_policy.parking_symbol == "LIQUIDBEES"
    assert proposal.policy_set.monitoring.t2_cadence is T2Cadence.MONTHLY

    # The universe carries purity scores, one per holding.
    assert [c.isin for c in proposal.universe] == sorted([COMPUTE, SENSORS, INTEG])
    by_isin = {c.isin: c for c in proposal.universe}
    assert by_isin[COMPUTE].purity.score == Decimal("0.600000")
    assert by_isin[SENSORS].purity.score == Decimal("0.250000")
    for candidate in proposal.universe:
        assert candidate.purity.evidence_refs

    # A thesis per holding, each in PROPOSAL (ratified with the proposal, not before).
    assert [t.isin for t in proposal.theses] == sorted([COMPUTE, SENSORS, INTEG])
    assert all(t.status is ThesisStatus.PROPOSAL for t in proposal.theses)
    assert all(t.break_conditions for t in proposal.theses)

    # A recommendation note per §5.2 policy.
    assert {r.policy for r in proposal.recommendations} == {
        "capital_plan",
        "horizon",
        "rotation_dial",
        "risk_rails",
        "exit_menu",
        "cash_policy",
        "monitoring",
    }


# ── acceptance 2: dial and rails trace to the stated risk appetite, with recorded reasoning ──────


def test_dial_traces_to_risk_appetite() -> None:
    proposal = _proposal()
    dial_note = next(r for r in proposal.recommendations if r.policy == "rotation_dial")
    assert dial_note.traced_from == "risk_appetite"
    assert dial_note.stated_value == RiskAppetite.AGGRESSIVE.value
    assert "30" in dial_note.recommended
    assert dial_note.reasoning  # a non-empty recorded reason
    assert proposal.policy_set.rotation_dial.tactical_pct == Decimal("30")


def test_rails_trace_to_concentration_tolerance() -> None:
    proposal = _proposal()
    rails_note = next(r for r in proposal.recommendations if r.policy == "risk_rails")
    assert rails_note.traced_from == "concentration_tolerance"
    assert rails_note.stated_value == ConcentrationTolerance.MEDIUM.value
    rails = proposal.policy_set.rails
    assert rails.max_position_pct == Decimal("15")
    assert rails.max_sector_pct == Decimal("35")
    assert rails.min_holdings == 8
    assert rails.drawdown_review_pct == Decimal("25")
    # The per-order sanity cap is 12x the SIP (§5.2), traced to the capital plan.
    assert rails.max_order_value_inr == Decimal("10000") * 12


def test_derivations_move_with_the_stated_appetite() -> None:
    """A conservative interview recommends a smaller dial and tighter rails — proof of the trace."""
    conservative = _proposal(
        transcript=_transcript(risk_appetite="conservative", concentration="low")
    )
    assert conservative.policy_set.rotation_dial.tactical_pct == Decimal("10")
    assert conservative.policy_set.rails.max_position_pct == Decimal("10")
    assert conservative.policy_set.rails.min_holdings == 12
    assert conservative.policy_set.rails.drawdown_review_pct == Decimal("15")
    assert conservative.policy_set.monitoring.t1_sensitivity is TriggerSensitivity.HIGH


def test_unstated_concentration_derives_from_appetite_and_says_so() -> None:
    """When concentration is left blank it derives from the risk appetite, on the record."""
    transcript = _transcript()
    del transcript[InterviewField.CONCENTRATION]
    proposal = _proposal(transcript=transcript)

    # AGGRESSIVE → HIGH concentration when not separately stated.
    assert proposal.policy_set.rails.max_position_pct == Decimal("20")
    rails_note = next(r for r in proposal.recommendations if r.policy == "risk_rails")
    assert "derived" in rails_note.reasoning.casefold()
    assert RiskAppetite.AGGRESSIVE.value.casefold() in rails_note.reasoning.casefold()


def test_recommendation_is_deterministic() -> None:
    """The same answers recommend byte-identical policies (§8.3.3) — a table, not a guess."""
    answers = conduct_interview(_transcript())
    first = recommend_policies(answers)
    second = recommend_policies(answers)
    assert first.rotation_dial == second.rotation_dial
    assert first.rails == second.rails
    assert first.recommendations == second.recommendations


# ── acceptance 3: one ratifiable document, not a series of approvals ──────────────────────────────


def test_proposal_is_ratified_in_one_act() -> None:
    proposal = _proposal()
    clock = FrozenClock(AS_OF)

    # One hash over the whole case; one ratification pins it.
    ratification = Ratification(
        by="B9 fixture",
        at=clock.now(),
        kind=RatificationKind.FIXTURE,
        content_hash=proposal.content_hash,
    )
    ratified = proposal.ratified_with(ratification)

    assert ratified.status is ProposalStatus.RATIFIED
    assert ratified.ratification is not None
    assert ratified.ratification.content_hash == proposal.content_hash
    # The single act covered every policy and every thesis — nothing was ratified piecemeal.
    assert set(ratified.policy_set.policies) == set(POLICY_FIELDS)
    assert len(ratified.theses) == 3


def test_content_hash_is_stable_across_builds() -> None:
    """The same interview and inputs hash to the same document — the ratification key is stable."""
    assert _proposal().content_hash == _proposal().content_hash


def test_ratification_for_a_changed_proposal_is_refused() -> None:
    """A ratification pinned to an earlier draft cannot ratify a proposal that changed (§5.1)."""
    original = _proposal()
    clock = FrozenClock(AS_OF)
    stale = Ratification(
        by="owner",
        at=clock.now(),
        kind=RatificationKind.FIXTURE,
        content_hash=original.content_hash,
    )

    # A different interview (one holding excluded) is a different document with a different hash.
    changed = _proposal(
        transcript=_transcript(exclusions="Sensor Actuator Corp"),
        theses=_theses(COMPUTE, INTEG),
    )
    assert changed.content_hash != original.content_hash
    with pytest.raises(ProposalRatificationMismatchError):
        changed.ratified_with(stale)


def test_editing_the_universe_changes_the_hash() -> None:
    """Excluding a holding removes it and changes the single content hash (one document)."""
    full = _proposal()
    trimmed = _proposal(
        transcript=_transcript(exclusions=SENSORS),
        theses=_theses(COMPUTE, INTEG),
    )
    assert SENSORS not in {c.isin for c in trimmed.universe}
    assert {c.isin for c in trimmed.universe} == {COMPUTE, INTEG}
    assert trimmed.content_hash != full.content_hash


# ── elicitation and assembly fail loud rather than fabricate ─────────────────────────────────────


def test_missing_required_answer_raises() -> None:
    transcript = _transcript()
    del transcript[InterviewField.RISK_APPETITE]
    with pytest.raises(IncompleteInterviewError, match="risk_appetite"):
        conduct_interview(transcript)


def test_unparseable_risk_appetite_raises() -> None:
    with pytest.raises(InterviewParseError, match="risk_appetite"):
        conduct_interview(_transcript(risk_appetite="quite risky"))


def test_unparseable_sip_amount_raises() -> None:
    with pytest.raises(InterviewParseError, match="sip_amount"):
        conduct_interview(_transcript(sip_amount="a lot"))


def test_exclusions_that_empty_the_universe_raise() -> None:
    with pytest.raises(EmptyUniverseError):
        _proposal(
            transcript=_transcript(exclusions=f"{COMPUTE}, {SENSORS}, {INTEG}"),
            theses=_theses(COMPUTE, SENSORS, INTEG),
        )


def test_missing_thesis_for_a_holding_raises() -> None:
    with pytest.raises(IncompleteProposalError, match=SENSORS):
        _proposal(theses=_theses(COMPUTE, INTEG))  # SENSORS has no thesis


def test_thesis_for_a_non_holding_raises() -> None:
    extra = _theses(COMPUTE, SENSORS, INTEG)
    extra["INE002A01018"] = _thesis("INE002A01018")  # not in the proposed universe
    with pytest.raises(IncompleteProposalError):
        _proposal(theses=extra)


def test_theme_mismatch_raises() -> None:
    """A theme map for a different theme is a mismatched universe, refused."""
    answers = conduct_interview(_transcript(theme="Defence"))
    with pytest.raises(ProposalError, match="does not match"):
        build_proposal(
            case_id=CASE_ID,
            answers=answers,
            theme_map=_theme_map(),  # mapped for AI/Robotics
            theses=_theses(COMPUTE, SENSORS, INTEG),
        )
