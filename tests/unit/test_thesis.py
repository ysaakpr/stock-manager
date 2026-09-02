"""M5.6: a core buy needs a ratified thesis, an edit is a new version, and vague conditions are
refused.

The three claims this task makes, and how each is shown here:

1. **A CORE buy without a ratified thesis is impossible.** `authorize_buy` is the gate; it raises
   `UnratifiedCoreBuyError` for a holding with no ratified thesis, for one carrying only a proposal,
   and for one whose thesis was revised and not yet re-ratified — and returns an authorization
   pinned to the ratified version only when one is actually in force. The tactical and cash sleeves
   are checked too, because the rule is "core carries a thesis, tactical carries a rationale".
2. **Editing a ratified thesis creates v2 in PROPOSAL and does not touch v1.** `revise()` and the
   `ThesisBook` are walked: v1 is ratified, an edit produces v2 in `PROPOSAL` with
   `supersedes_version == 1`, and the v1 object still reads `RATIFIED` with its original content
   hash
   and ratification intact. v1 only becomes `SUPERSEDED` when v2 is itself ratified.
3. **A vague / unfalsifiable break condition is rejected at draft time with a reason.** The
   falsifiability check is exercised directly (opinion words, no checkable anchor, an unwatched
   condition) and end-to-end through `draft_thesis` against a `StubLLM` that drafts a bad condition
   —
   the model's answer is refused with a `UnfalsifiableBreakConditionError` naming what is wrong,
   never waved through to a human to catch.

Everything is offline: the model is `StubLLM` (B4), time is a `FrozenClock` (B10), and joins are on
ISIN (#2). No network, no database, no wall clock.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from analyst.cases import Ratification, RatificationKind
from analyst.llm import Message, Role, StopReason, StubLLM, StubReply, ToolCall, prompt_digest
from analyst.thesis import (
    BreakCondition,
    BreakConditionType,
    BuyAuthorization,
    CoreBuyError,
    DraftError,
    EvaluationTier,
    Sleeve,
    TacticalRationale,
    TacticalRationaleRequiredError,
    Thesis,
    ThesisBook,
    ThesisKey,
    ThesisRatificationMismatchError,
    ThesisStatus,
    ThesisVersionError,
    UnfalsifiableBreakConditionError,
    assert_falsifiable,
    authorize_buy,
    draft_thesis,
)
from analyst.thesis import engine as thesis_engine
from dataplatform.clock import FrozenClock

ISIN = "INE009A01021"
CASE = "case-ai-robotics"
MODEL = "claude-sonnet-4-5"


# ── fixtures / builders ──────────────────────────────────────────────────────────────────────


def _break_conditions() -> tuple[BreakCondition, ...]:
    """A falsifiable §5.3 break-condition set, one of each type, mirroring the plan's sketch."""
    return (
        BreakCondition(
            id="BC1",
            type=BreakConditionType.FUNDAMENTAL,
            condition="segment revenue declines for two consecutive quarters",
            evaluation_tier=EvaluationTier.T1,
            evaluation="T1 on results filing",
        ),
        BreakCondition(
            id="BC2",
            type=BreakConditionType.STRUCTURAL,
            condition="exit or divestment of the robotics business line",
            evaluation_tier=EvaluationTier.T0,
            evaluation="T0 announcement keywords -> T1",
        ),
        BreakCondition(
            id="BC3",
            type=BreakConditionType.INTEGRITY,
            condition="auditor resignation or promoter pledge above 50%",
            evaluation_tier=EvaluationTier.T0,
            evaluation="T0 -> immediate T1",
        ),
    )


def _proposal(*, isin: str = ISIN, version: int = 1, supersedes: int | None = None) -> Thesis:
    """A fresh thesis proposal for a core holding."""
    return Thesis(
        case_id=CASE,
        isin=isin,
        version=version,
        supersedes_version=supersedes,
        driver="Indian EMS capex cycle x robotics component localization",
        theme_purity=Decimal("0.6"),
        expected_evidence=("order-book growth >20% YoY", "robotics segment revenue disclosure"),
        break_conditions=_break_conditions(),
    )


def _ratification_for(
    thesis: Thesis, *, kind: RatificationKind = RatificationKind.HUMAN
) -> Ratification:
    """A ratification pinned to a thesis's exact content, timestamped from a FrozenClock (B10)."""
    clock = FrozenClock(date(2026, 9, 2))
    return Ratification(by="owner", at=clock.now(), kind=kind, content_hash=thesis.content_hash)


def _ratified(thesis: Thesis, **kwargs: RatificationKind) -> Thesis:
    return thesis.ratified_with(_ratification_for(thesis, **kwargs))


# ── acceptance 1: a CORE buy without a ratified thesis is impossible ─────────────────────────


def test_core_buy_without_any_thesis_raises() -> None:
    """The headline invariant: a core holding with no thesis at all cannot be bought."""
    book = ThesisBook()
    with pytest.raises(CoreBuyError) as excinfo:
        authorize_buy(case_id=CASE, isin=ISIN, sleeve=Sleeve.CORE, book=book)
    assert ISIN in str(excinfo.value)
    assert "ratified thesis" in str(excinfo.value)


def test_core_buy_with_only_a_proposal_raises() -> None:
    """A drafted-but-unratified thesis is not a licence to buy — proposing is never ratifying
    (§5.1).
    """
    book = ThesisBook()
    book.propose(_proposal())
    assert book.current_ratified(ThesisKey(CASE, ISIN)) is None
    with pytest.raises(CoreBuyError):
        authorize_buy(case_id=CASE, isin=ISIN, sleeve=Sleeve.CORE, book=book)


def test_core_buy_with_ratified_thesis_is_authorized_and_pinned() -> None:
    """Once ratified, the buy is authorized and the authorization pins the exact thesis version."""
    book = ThesisBook()
    proposal = book.propose(_proposal())
    ratified = book.ratify(ThesisKey(CASE, ISIN), _ratification_for(proposal))

    auth = authorize_buy(case_id=CASE, isin=ISIN, sleeve=Sleeve.CORE, book=book)
    assert isinstance(auth, BuyAuthorization)
    assert auth.sleeve is Sleeve.CORE
    assert auth.thesis_version == 1
    assert auth.thesis_content_hash == ratified.content_hash


def test_core_buy_missing_book_raises() -> None:
    """No book means no thesis can be shown, so a core buy is refused rather than assumed safe."""
    with pytest.raises(CoreBuyError):
        authorize_buy(case_id=CASE, isin=ISIN, sleeve=Sleeve.CORE, book=None)


def test_core_buy_after_revision_without_reratification_raises() -> None:
    """Revising a ratified thesis drops the buy licence until the new version is itself ratified."""
    book = ThesisBook()
    proposal = book.propose(_proposal())
    book.ratify(ThesisKey(CASE, ISIN), _ratification_for(proposal))
    # A buy is fine right now.
    authorize_buy(case_id=CASE, isin=ISIN, sleeve=Sleeve.CORE, book=book)

    # Edit it: v2 is a proposal, but v1 is still the ratified one, so a buy still stands...
    book.revise(ThesisKey(CASE, ISIN), driver="revised: EMS capex cycle with defence robotics pull")
    still = authorize_buy(case_id=CASE, isin=ISIN, sleeve=Sleeve.CORE, book=book)
    assert still.thesis_version == 1  # the ratified version, not the fresh proposal


def test_tactical_buy_needs_a_rationale_not_a_thesis() -> None:
    """A tactical position carries a journaled rationale; without one, it is refused (§5.5)."""
    with pytest.raises(TacticalRationaleRequiredError):
        authorize_buy(case_id=CASE, isin=ISIN, sleeve=Sleeve.TACTICAL)
    with pytest.raises(TacticalRationaleRequiredError):
        authorize_buy(case_id=CASE, isin=ISIN, sleeve=Sleeve.TACTICAL, rationale="   ")

    auth = authorize_buy(
        case_id=CASE,
        isin=ISIN,
        sleeve=Sleeve.TACTICAL,
        rationale="momentum breakout, 3% sleeve trim",
    )
    assert auth.sleeve is Sleeve.TACTICAL
    assert auth.rationale == "momentum breakout, 3% sleeve trim"
    assert auth.thesis_content_hash is None


def test_cash_leg_needs_neither_thesis_nor_rationale() -> None:
    """The parking ETF is not a thesis-backed position; its buy is authorized bare."""
    auth = authorize_buy(case_id=CASE, isin=ISIN, sleeve=Sleeve.CASH)
    assert auth.sleeve is Sleeve.CASH
    assert auth.thesis_content_hash is None and auth.rationale is None


def test_a_thesis_is_core_only() -> None:
    """A thesis cannot be filed against the tactical or cash sleeve (§5.5)."""
    payload = {**_proposal().model_dump(), "sleeve": Sleeve.TACTICAL.value}
    with pytest.raises(ValidationError):
        Thesis.model_validate(payload)
    tactical = TacticalRationale(isin=ISIN, rationale="short-term rotation")
    assert tactical.sleeve is Sleeve.TACTICAL


# ── acceptance 2: editing a ratified thesis creates v2 in PROPOSAL, v1 unchanged ─────────────


def test_revision_creates_v2_proposal_and_leaves_v1_untouched() -> None:
    """§5.1's immutability: an edit is a new version, and the old record does not move."""
    v1_ratified = _ratified(_proposal())
    original_hash = v1_ratified.content_hash

    v2 = v1_ratified.revise(driver="revised: EMS capex cycle with defence robotics pull-through")

    # v2 is a fresh proposal that supersedes v1.
    assert v2.version == 2
    assert v2.supersedes_version == 1
    assert v2.status is ThesisStatus.PROPOSAL
    assert v2.ratification is None
    assert v2.content_hash != original_hash

    # v1's record is exactly as it was — frozen, still ratified, same hash and ratification.
    assert v1_ratified.status is ThesisStatus.RATIFIED
    assert v1_ratified.content_hash == original_hash
    assert v1_ratified.ratification is not None


def test_ratified_thesis_is_immutable() -> None:
    """Immutability is a ValidationError, not a convention: you cannot assign into a ratified
    thesis.
    """
    v1 = _ratified(_proposal())
    with pytest.raises(ValidationError):
        v1.driver = "something else"


def test_revise_rejects_noop_and_unknown_fields() -> None:
    """A revision must change ratifiable content, and may only touch §5.3 content fields."""
    v1 = _ratified(_proposal())
    with pytest.raises(ThesisVersionError):
        v1.revise()  # nothing to re-ratify
    with pytest.raises(ThesisVersionError):
        v1.revise(driver=v1.driver)  # byte-identical
    with pytest.raises(ThesisVersionError):
        v1.revise(version=5)  # not a content field


def test_book_supersedes_v1_only_when_v2_is_ratified() -> None:
    """Through the book: v1 stays the ratified version until v2's own ratification retires it."""
    book = ThesisBook()
    p1 = book.propose(_proposal())
    book.ratify(ThesisKey(CASE, ISIN), _ratification_for(p1))

    p2 = book.revise(ThesisKey(CASE, ISIN), theme_purity=Decimal("0.7"))
    # v1 is still the one in force.
    current = book.current_ratified(ThesisKey(CASE, ISIN))
    assert current is not None and current.version == 1

    book.ratify(ThesisKey(CASE, ISIN), _ratification_for(p2))
    current = book.current_ratified(ThesisKey(CASE, ISIN))
    assert current is not None and current.version == 2

    versions = book.versions(ThesisKey(CASE, ISIN))
    statuses = {v.version: v.status for v in versions}
    assert statuses == {1: ThesisStatus.SUPERSEDED, 2: ThesisStatus.RATIFIED}
    # The superseded v1 keeps its ratification record (a governance trail is never deleted).
    assert versions[0].ratification is not None


def test_ratification_pinned_to_stale_content_is_refused() -> None:
    """A ratification for one draft cannot be moved onto an edited one (M5.8's guarantee)."""
    proposal = _proposal()
    stale = _ratification_for(proposal)
    edited = proposal.revise(theme_purity=Decimal("0.9"))
    with pytest.raises(ThesisRatificationMismatchError):
        edited.ratified_with(stale)


def test_a_version_is_ratified_only_once() -> None:
    """Re-ratifying is refused; the way to change a ratified thesis is revise()."""
    v1 = _ratified(_proposal())
    with pytest.raises(ThesisVersionError):
        v1.ratified_with(_ratification_for(v1))


# ── acceptance 3: a vague / unfalsifiable break condition is rejected at draft time ──────────


@pytest.mark.parametrize(
    ("condition", "why"),
    [
        ("the growth story stops working", "story"),
        ("the stock underperforms the benchmark", "underperform"),
        ("management sentiment turns negative", "sentiment"),
        ("results disappoint versus expectations", "disappoint"),
    ],
)
def test_opinion_break_conditions_are_rejected_with_a_reason(condition: str, why: str) -> None:
    """A condition that is a matter of opinion cannot be a break condition — and the reason names
    it.
    """
    with pytest.raises(UnfalsifiableBreakConditionError) as excinfo:
        BreakCondition(
            id="BC1",
            type=BreakConditionType.FUNDAMENTAL,
            condition=condition,
            evaluation_tier=EvaluationTier.T1,
            evaluation="T1 review",
        )
    assert why in str(excinfo.value).lower()


def test_condition_with_no_checkable_anchor_is_rejected() -> None:
    """A grammatical statement with nothing measurable or discrete to check is unfalsifiable."""
    with pytest.raises(UnfalsifiableBreakConditionError):
        assert_falsifiable(condition="the company changes its plans", evaluation="watch it")


def test_condition_with_no_evaluation_is_rejected() -> None:
    """A condition nobody watches cannot be evaluated mechanically or evidentially (§5.3)."""
    with pytest.raises(UnfalsifiableBreakConditionError):
        assert_falsifiable(
            condition="segment revenue falls two consecutive quarters", evaluation="   "
        )


def test_falsifiable_conditions_are_accepted() -> None:
    """The positive case: concrete events and measurable thresholds both pass."""
    # A discrete corporate event.
    assert_falsifiable(condition="auditor resignation is announced", evaluation="T0 keyword")
    # A measurable threshold.
    assert_falsifiable(
        condition="order book falls more than 20% year on year", evaluation="T1 on results filing"
    )


def _draft_reply(arguments: Mapping[str, object]) -> StubReply:
    return StubReply(
        text="drafted",
        tool_calls=(ToolCall(id="call-1", name="propose_thesis", arguments=arguments),),
        stop_reason=StopReason.TOOL_USE,
    )


def _register_draft(stub: StubLLM, brief: str, arguments: Mapping[str, object]) -> None:
    """Register a canned tool call for exactly the request draft_thesis will make."""
    digest = prompt_digest(
        [Message(role=Role.USER, content=brief)],
        model=MODEL,
        tools=[thesis_engine.THESIS_TOOL],
        system=thesis_engine._SYSTEM,
    )
    stub.register(digest, _draft_reply(arguments))


def test_draft_thesis_builds_a_proposal_from_the_model() -> None:
    """Drafting (LLM): a well-formed model answer becomes a PROPOSAL thesis for ratification."""
    brief = "Draft a thesis for the EMS/robotics holding."
    good = {
        "driver": "EMS capex cycle x robotics localization",
        "theme_purity": "0.6",
        "expected_evidence": ["order-book growth >20% YoY"],
        "break_conditions": [
            {
                "id": "BC1",
                "type": "integrity",
                "condition": "auditor resignation or promoter pledge above 50%",
                "evaluation_tier": "T0",
                "evaluation": "T0 -> immediate T1",
            }
        ],
    }
    stub = StubLLM(synthesize_unknown=False)
    _register_draft(stub, brief, good)

    thesis = draft_thesis(stub, case_id=CASE, isin=ISIN, model=MODEL, brief=brief)
    assert thesis.status is ThesisStatus.PROPOSAL
    assert thesis.version == 1
    assert thesis.theme_purity == Decimal("0.6")
    assert thesis.break_conditions[0].type is BreakConditionType.INTEGRITY


def test_draft_thesis_rejects_a_vague_break_condition_at_draft_time() -> None:
    """A model that drafts an opinion is refused when the thesis is built, with the reason
    (acceptance
    3).
    """
    brief = "Draft a thesis for the EMS/robotics holding."
    vague = {
        "driver": "EMS capex cycle x robotics localization",
        "theme_purity": "0.6",
        "expected_evidence": ["order-book growth"],
        "break_conditions": [
            {
                "id": "BC1",
                "type": "fundamental",
                "condition": "the growth story stops working",
                "evaluation_tier": "T1",
                "evaluation": "T1 review",
            }
        ],
    }
    stub = StubLLM(synthesize_unknown=False)
    _register_draft(stub, brief, vague)

    with pytest.raises(UnfalsifiableBreakConditionError):
        draft_thesis(stub, case_id=CASE, isin=ISIN, model=MODEL, brief=brief)


def test_draft_thesis_raises_when_the_model_returns_no_tool_call() -> None:
    """A refusal is not a thesis: draft_thesis raises rather than fabricating one."""
    brief = "Draft a thesis."
    stub = StubLLM({}, synthesize_unknown=True)  # synthesizes a text-only answer, no tool call
    with pytest.raises(DraftError):
        draft_thesis(stub, case_id=CASE, isin=ISIN, model=MODEL, brief=brief)
