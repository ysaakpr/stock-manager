"""A4: the thesis engine — drafting a thesis with a model, and the gate on a core buy.

Two jobs live here, at the two ends of a thesis's life:

* **Drafting (§5.3).** `draft_thesis` asks an `LLM` (the X3 protocol, so `StubLLM` in tests and a
  real client in production — B4, invariant #5) to propose a thesis for a holding, offering it a
  single tool whose schema *is* the §5.3 object. The model's answer is parsed into a `Thesis` in
  `PROPOSAL` — which means every break condition it wrote passes `assert_falsifiable` on the way
  in, so a model that drafts "the story stops working" is refused *at draft time, with a reason*,
  not waved through to a human to catch. Drafting produces a proposal; it never ratifies (§5.1).

* **The core-buy gate (§5.5).** `authorize_buy` is the one function the order path must call before
  a buy, and it is where "every core holding carries a ratified thesis before its first buy" is
  enforced in code rather than in documentation: a `CORE` buy with no ratified thesis for that ISIN
  raises `UnratifiedCoreBuyError` and returns no authorization, so there is nothing for the order
  path to act on. A `TACTICAL` buy needs a journaled rationale instead (§5.5), and a `CASH` leg —
  the parking ETF — needs neither.

The gate returns a `BuyAuthorization`: the small, checkable token that says *this* buy is backed by
*that* ratified thesis version (pinned by its content hash) or *that* rationale. A9 journals it;
A8's rails act on the same order but on price/exposure grounds — the two guards are independent, and
neither is an LLM judgement.

Nothing here reads a clock or a database. The model call is a pure function of its inputs (the stub
makes that a replayable fact), and the timestamp on any ratification arrived from an injected
`Clock` (B10) long before a buy is authorized against it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final, assert_never

from analyst.llm.client import LLM, Message, Role, ToolCall, ToolSpec
from analyst.thesis.models import (
    BreakCondition,
    Sleeve,
    Thesis,
    ThesisError,
    ThesisStatus,
)
from analyst.thesis.ratify import ThesisBook, ThesisKey

__all__ = [
    "THESIS_TOOL",
    "BuyAuthorization",
    "CoreBuyError",
    "DraftError",
    "TacticalRationaleRequiredError",
    "UnratifiedCoreBuyError",
    "authorize_buy",
    "draft_thesis",
]


class DraftError(ThesisError):
    """The model's drafted thesis could not be turned into a valid `Thesis`.

    Distinct from `UnfalsifiableBreakConditionError`: that one means the model wrote a specific,
    named-and-reasoned bad break condition; this one means the answer was not a thesis at all — no
    tool call, unparseable arguments, missing fields.
    """


class CoreBuyError(ThesisError):
    """Base for a refused buy authorization, so the order path can catch the gate."""


class UnratifiedCoreBuyError(CoreBuyError):
    """A CORE buy was attempted for a holding with no ratified thesis (§5.5).

    The one this task exists to make un-bypassable: a core holding without a ratified thesis cannot
    be bought, full stop. Names the ISIN because that is what the operator or the drafting flow acts
    on next.
    """


class TacticalRationaleRequiredError(CoreBuyError):
    """A TACTICAL buy was attempted with no rationale to journal (§5.5).

    The tactical sleeve trades on the agent's discretion, but that discretion is still recorded: a
    tactical position with no rationale is an unexplained trade, which the journal exists to
    prevent.
    """


#: The tool the model is offered when drafting — its input schema is the §5.3 thesis object. The
#: model answers by calling it, so the arguments arrive structured rather than as prose to be
#: scraped. `evaluation_tier` and `type` are `enum`-constrained so the model cannot invent a fourth
#: break-condition type or a T2 per-condition tier; falsifiability itself is not expressible in JSON
#: Schema and is enforced when the `Thesis` is built.
THESIS_TOOL: Final[ToolSpec] = ToolSpec(
    name="propose_thesis",
    description=(
        "Propose a §5.3 investment thesis for a core holding. Every break condition must be "
        "falsifiable: state a concrete corporate event or a measurable threshold, never an opinion "
        "like 'the story weakens'. Return one call to this tool."
    ),
    input_schema={
        "type": "object",
        "additionalProperties": False,
        "required": ["driver", "theme_purity", "expected_evidence", "break_conditions"],
        "properties": {
            "driver": {"type": "string", "description": "The thesis in one line."},
            "theme_purity": {
                "type": "string",
                "description": "0..1 theme purity as a decimal string, e.g. '0.6'.",
            },
            "expected_evidence": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string"},
                "description": "What, if seen, would confirm the driver.",
            },
            "break_conditions": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["id", "type", "condition", "evaluation_tier", "evaluation"],
                    "properties": {
                        "id": {"type": "string"},
                        "type": {"enum": ["fundamental", "structural", "integrity"]},
                        "condition": {"type": "string"},
                        "evaluation_tier": {"enum": ["T0", "T1"]},
                        "evaluation": {"type": "string"},
                    },
                },
            },
        },
    },
)

_SYSTEM: Final[str] = (
    "You are drafting a falsifiable investment thesis for a core holding, per §5.3 of the plan. "
    "A thesis a human cannot disagree with is worthless: every break condition must name something "
    "that either happened or did not — a concrete corporate event or a measurable threshold over a "
    "stated period. Never write a break condition that is a matter of opinion."
)


def draft_thesis(
    llm: LLM,
    *,
    case_id: str,
    isin: str,
    model: str,
    brief: str,
    version: int = 1,
    supersedes_version: int | None = None,
) -> Thesis:
    """Draft a §5.3 thesis for a holding by asking a model, returned in `PROPOSAL`.

    What it does: offers `THESIS_TOOL` to `llm`, reads the tool call it makes, and builds a `Thesis`
    from the arguments — which validates falsifiability, so an unfalsifiable break condition is
    rejected here, at draft time, with a reason (acceptance criterion 3).
    What it assumes: `llm` honours the tool (the real client and `StubLLM` both do); `brief` carries
    what the model needs to reason about the holding — the theme, the company, the evidence gathered
    so far. `model` names the model to bill the call to (§5.7).
    What it never does: ratify. The result is a proposal; a human ratifies it (§5.1). It also never
    invents a thesis when the model declines — a refusal or a non-tool answer raises `DraftError`
    rather than fabricating one.

    `version`/`supersedes_version` let a caller draft a revision (v2+) directly from a model; the
    default is a fresh v1.
    """
    response = llm.complete(
        [Message(role=Role.USER, content=brief)],
        model=model,
        tools=[THESIS_TOOL],
        system=_SYSTEM,
    )
    arguments = _thesis_arguments(response.tool_calls, isin=isin)
    return _build_thesis(
        arguments,
        case_id=case_id,
        isin=isin,
        version=version,
        supersedes_version=supersedes_version,
    )


def _thesis_arguments(tool_calls: Sequence[ToolCall], *, isin: str) -> Mapping[str, Any]:
    """The arguments of the single `propose_thesis` call, or raise `DraftError`."""
    proposals = [call for call in tool_calls if call.name == THESIS_TOOL.name]
    if not proposals:
        raise DraftError(
            f"the model returned no {THESIS_TOOL.name} call for {isin}; a thesis was requested and "
            "none was drafted (a refusal is not a thesis)"
        )
    if len(proposals) > 1:
        raise DraftError(
            f"the model returned {len(proposals)} {THESIS_TOOL.name} calls for {isin}; a holding "
            "has one thesis, so which one to record is ambiguous"
        )
    return proposals[0].arguments


def _build_thesis(
    arguments: Mapping[str, Any],
    *,
    case_id: str,
    isin: str,
    version: int,
    supersedes_version: int | None,
) -> Thesis:
    """Turn drafted arguments into a `Thesis`; falsifiability is enforced as the break conditions
    build.
    """
    try:
        raw_conditions = arguments["break_conditions"]
        break_conditions = tuple(
            BreakCondition.model_validate(condition) for condition in raw_conditions
        )
        purity = arguments["theme_purity"]
        return Thesis(
            case_id=case_id,
            isin=isin,
            sleeve=Sleeve.CORE,
            version=version,
            status=ThesisStatus.PROPOSAL,
            supersedes_version=supersedes_version,
            driver=arguments["driver"],
            # A decimal string keeps the exact value; a float would be rejected by the model
            # (#money).
            theme_purity=Decimal(str(purity)),
            expected_evidence=tuple(arguments["expected_evidence"]),
            break_conditions=break_conditions,
            ratification=None,
        )
    except ThesisError:
        # Falsifiability and thesis-shape refusals are already typed and reasoned — surface them.
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise DraftError(
            f"the drafted thesis for {isin} is not a valid §5.3 object: {exc}"
        ) from exc


# ── the core-buy gate (§5.5) ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class BuyAuthorization:
    """Proof that a buy is backed by what §5.5 requires of its sleeve — the token the order path
    needs.

    For a `CORE` buy it pins the ratified thesis version and its content hash, so the journal
    records
    *which* thesis backed the buy and a later reader can reconstruct it. For a `TACTICAL` buy it
    carries the rationale to journal. For a `CASH` leg it carries neither — the parking ETF is not a
    thesis-backed position.

    It exists only when the gate passed: `authorize_buy` returns one or raises. An order path that
    has a `BuyAuthorization` in hand has, by construction, cleared §5.5.
    """

    case_id: str
    isin: str
    sleeve: Sleeve
    thesis_version: int | None = None
    thesis_content_hash: str | None = None
    rationale: str | None = None


def authorize_buy(
    *,
    case_id: str,
    isin: str,
    sleeve: Sleeve,
    book: ThesisBook | None = None,
    rationale: str | None = None,
) -> BuyAuthorization:
    """Authorize a buy, or raise — the §5.5 gate the order path must pass before any buy.

    What it does:
      * `CORE`   — requires a ratified thesis for this ISIN in `book`; raises
      `UnratifiedCoreBuyError`
        if there is none, so a core holding cannot be bought before its thesis is ratified.
      * `TACTICAL` — requires a non-blank `rationale` to journal; raises
      `TacticalRationaleRequiredError`
        otherwise.
      * `CASH`   — the parking leg; authorized without a thesis or rationale.
    What it assumes: `book` is the case's thesis history (`ThesisBook`); it is required for a core
    buy and ignored otherwise. Money and quantity are the order path's and A8's concern, not this
    gate's — this gate answers only "is this holding allowed a buy at all".
    What it never does: consult an LLM (the gate is deterministic, invariant #6-adjacent), or
    authorize a core buy on a mere proposal — only a `RATIFIED` thesis counts.
    """
    match sleeve:
        case Sleeve.CORE:
            if book is None:
                raise UnratifiedCoreBuyError(
                    f"cannot authorize a CORE buy of {isin} in case {case_id}: no thesis book was "
                    "supplied, so no ratified thesis can be shown (§5.5)"
                )
            ratified = book.current_ratified(ThesisKey(case_id, isin))
            if ratified is None:
                raise UnratifiedCoreBuyError(
                    f"cannot authorize a CORE buy of {isin} in case {case_id}: it has no ratified "
                    "thesis, and every core holding carries a ratified thesis before its first "
                    "buy (§5.5)"
                )
            return BuyAuthorization(
                case_id=case_id,
                isin=isin,
                sleeve=Sleeve.CORE,
                thesis_version=ratified.version,
                thesis_content_hash=ratified.content_hash,
            )
        case Sleeve.TACTICAL:
            if rationale is None or not rationale.strip():
                raise TacticalRationaleRequiredError(
                    f"cannot authorize a TACTICAL buy of {isin} in case {case_id}: a tactical "
                    "position carries a journaled lightweight rationale, and none was given (§5.5)"
                )
            return BuyAuthorization(
                case_id=case_id, isin=isin, sleeve=Sleeve.TACTICAL, rationale=rationale.strip()
            )
        case Sleeve.CASH:
            return BuyAuthorization(case_id=case_id, isin=isin, sleeve=Sleeve.CASH)
        case _:  # pragma: no cover — exhaustive over Sleeve
            assert_never(sleeve)
