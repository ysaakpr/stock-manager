"""A5 · T1's structured verdict — the schema the model must answer in, and the policy gate.

§5.4 says the T1 tier returns, over an evidence bundle, "a verdict per break condition:
`INTACT / WEAKENED / BROKEN` + a proposed action within ratified policies". This module is that
output, expressed as a schema rather than as free text, and the two rules the plan attaches to it:

* **A verdict is schema-validated, per break condition.** The model's answer is parsed into a
  `T1Verdict` — one `BreakConditionVerdict` per condition, each carrying one of the three closed
  verdicts and what was observed, plus a `ProposedAction` from a closed vocabulary. Anything that
  does not parse, or that does not name a verdict for exactly the thesis's break conditions, is a
  `MalformedVerdictError`: the caller retries, then escalates. A malformed answer is never read
  loosely — a decision that would end a position cannot rest on the model having *probably* meant
  BROKEN.

* **The proposed action is validated in code, not trusted from the model.** §5.4/§5.6 give the
  agent a choice *within* the ratified exit menu; they do not let it invent an exit the human never
  approved. `validate_action` is the deterministic gate that enforces that before any action reaches
  A7/A8: an exit strategy off the ratified menu, an `IMMEDIATE` exit on a break the menu does not
  unlock it for, an exit with no break behind it, or a `HOLD` sitting on a BROKEN core condition are
  each refused with a reason. The model proposes; the code disposes — which is invariant #6's shape
  at this tier (the rails are code, never an LLM's say-so).

Nothing here reads a clock, a database or the network, and there is no float anywhere: the verdict
is text and enums, and the only numbers involved (token cost) live in `t1.py`'s journal write.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from analyst.cases.policies import ExitMenu, ExitStrategy
from analyst.journal import Verdict
from analyst.thesis import BreakConditionType, Thesis

__all__ = [
    "BreakConditionVerdict",
    "MalformedVerdictError",
    "PolicyViolationError",
    "ProposedAction",
    "ProposedActionKind",
    "T1Verdict",
    "VerdictError",
    "check_covers_conditions",
    "parse_verdict",
    "validate_action",
]

#: ISO 6166 shape, as everywhere else. Shape only — the check digit is D2's job.
_ISIN_PATTERN: Final = r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$"


class VerdictError(Exception):
    """Base for every T1 verdict failure, so a caller can catch the module."""


class MalformedVerdictError(VerdictError):
    """The model's answer could not be read as a well-formed verdict.

    Raised for invalid JSON, a shape the schema rejects, or a verdict that does not evaluate
    exactly the thesis's break conditions. The caller retries and then escalates — it never guesses
    what the model meant, because a break-condition verdict decides whether a position is exited.
    """


class PolicyViolationError(VerdictError):
    """A well-formed verdict proposed an action the ratified policy does not allow.

    Distinct from `MalformedVerdictError`: the answer was readable, but the *action* falls outside
    the ratified exit menu (§5.6) or contradicts §5.5's rule that core membership changes on a
    break only. It is rejected here, in code, before it could reach A7/A8 — the model does not get
    to widen the menu the human ratified.
    """


class ProposedActionKind(StrEnum):
    """What T1 may propose doing about a flagged holding — a closed vocabulary.

    Closed, not free text, because the action is validated against ratified policy and journaled as
    a decision: a proposal the code cannot recognise is a proposal it cannot check.
    """

    HOLD = "HOLD"
    """Leave the position alone. Legitimate only when nothing broke (§5.5)."""

    EXIT = "EXIT"
    """Exit the position via A7, using a strategy from the ratified menu. Requires a break."""

    ESCALATE = "ESCALATE"
    """Hand the decision to the human. Always in policy — deferring up is never a breach."""


class ProposedAction(BaseModel):
    """The action T1 proposes, before the policy gate has cleared it.

    What it does: carries the kind, the exit strategy (only for an exit), and the one-line reason.
    What it assumes: the strategy, if present, is a member of §5.6's `ExitStrategy` — the ratified
    *menu* it must belong to is checked later, in `validate_action`, against the case's policy set.
    What it never does: carry an exit strategy for a non-exit action, or an exit with none — a shape
    that would make "which strategy" ambiguous at the moment it matters.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ProposedActionKind = Field(description="HOLD / EXIT / ESCALATE.")
    exit_strategy: ExitStrategy | None = Field(
        default=None, description="The §5.6 strategy, present exactly when kind is EXIT."
    )
    rationale: str = Field(min_length=1, description="Why this action, in one line.")

    @model_validator(mode="after")
    def _strategy_matches_kind(self) -> ProposedAction:
        if self.kind is ProposedActionKind.EXIT and self.exit_strategy is None:
            raise ValueError(
                "an EXIT action must name an exit strategy from the ratified menu (§5.6); "
                "an exit with no strategy is not an action A7 can carry out"
            )
        if self.kind is not ProposedActionKind.EXIT and self.exit_strategy is not None:
            raise ValueError(
                f"a {self.kind.value} action carries no exit strategy; only an EXIT does"
            )
        return self


class BreakConditionVerdict(BaseModel):
    """One break condition, as the T1 model judged it (§5.4's per-condition verdict).

    `id` ties the verdict to a break condition on the thesis, because the journal records a verdict
    *per break-condition id* (`analyst.journal.BreakConditionEvaluation`) and a verdict on an
    unnamed condition cannot be reviewed against the outcome later (§5.7).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1, description="Break-condition id from the thesis, e.g. 'BC1'.")
    verdict: Verdict = Field(description="INTACT / WEAKENED / BROKEN (§5.4).")
    observed: str = Field(
        min_length=1,
        description="What was observed in the evidence — the 'because' of the verdict.",
    )


class T1Verdict(BaseModel):
    """The whole T1 answer: a verdict per break condition and one proposed action (§5.4).

    What it does: carries the structured review a strong model returned over the evidence bundle.
    What it assumes: `verdicts` names exactly the thesis's break conditions — enforced not here but
    in `check_covers_conditions`, which has the thesis to compare against.
    What it never does: exist in a shape the schema does not accept. `extra="forbid"` means an
    invented field is a parse failure, not silently dropped context.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    isin: str = Field(pattern=_ISIN_PATTERN, description="The holding reviewed — ISIN only (#2).")
    verdicts: tuple[BreakConditionVerdict, ...] = Field(
        min_length=1, description="One verdict per break condition (§5.4)."
    )
    proposed_action: ProposedAction = Field(
        description="The action, within ratified policy (§5.6)."
    )
    summary: str = Field(min_length=1, description="The review in one line.")

    @model_validator(mode="after")
    def _ids_unique(self) -> T1Verdict:
        ids = [v.id for v in self.verdicts]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise ValueError(
                f"a break condition was judged twice: {', '.join(duplicates)}; one verdict per id"
            )
        return self

    @property
    def broken(self) -> tuple[BreakConditionVerdict, ...]:
        """The break conditions the model judged BROKEN — the exit trigger (§5.5)."""
        return tuple(v for v in self.verdicts if v.verdict is Verdict.BROKEN)

    @property
    def worst(self) -> Verdict:
        """The most adverse verdict across the conditions — the state of the thesis as a whole."""
        if any(v.verdict is Verdict.BROKEN for v in self.verdicts):
            return Verdict.BROKEN
        if any(v.verdict is Verdict.WEAKENED for v in self.verdicts):
            return Verdict.WEAKENED
        return Verdict.INTACT


def _strip_code_fence(text: str) -> str:
    """Drop a leading/trailing ``` fence a model may wrap JSON in, leaving the JSON body.

    Tolerant of exactly the one thing a model routinely adds — a ```json … ``` wrapper — and
    nothing more: anything else that is not JSON stays not-JSON and is caught as malformed.
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    # Drop the opening fence (``` or ```json) and a closing fence if present.
    body = lines[1:]
    if body and body[-1].strip().startswith("```"):
        body = body[:-1]
    return "\n".join(body).strip()


def parse_verdict(text: str) -> T1Verdict:
    """Parse a model's raw answer into a schema-validated `T1Verdict`.

    What it does: strips a code fence if the model added one, then validates the JSON against the
    schema.
    What it assumes: the answer is meant to be the whole verdict — there is no partial parse.
    What it never does: guess. Invalid JSON or a shape the schema rejects raises
    `MalformedVerdictError` with the reason, so the caller retries or escalates rather than reading
    a half-formed answer as a decision.
    """
    body = _strip_code_fence(text)
    if not body:
        raise MalformedVerdictError(
            "the model returned an empty answer; there is no verdict to read"
        )
    try:
        return T1Verdict.model_validate_json(body)
    except ValidationError as error:
        raise MalformedVerdictError(
            f"the model's answer is not a well-formed verdict: {error}"
        ) from error


def check_covers_conditions(verdict: T1Verdict, thesis: Thesis) -> None:
    """Refuse a verdict that does not evaluate exactly the thesis's break conditions.

    What it does: compares the ids the model returned against the thesis's, and raises if they
    differ in either direction.
    What it assumes: the thesis is the version in force on the session — the same one shown in the
    bundle.
    What it never does: accept a verdict that skipped a condition or invented one. A skipped
    condition is a break the review never looked at; an invented one is a verdict on nothing. Both
    are `MalformedVerdictError`, retried then escalated — not read as a partial review.
    """
    expected = {bc.id for bc in thesis.break_conditions}
    got = {v.id for v in verdict.verdicts}
    if got == expected:
        return
    missing = sorted(expected - got)
    extra = sorted(got - expected)
    parts: list[str] = []
    if missing:
        parts.append(f"did not judge {', '.join(missing)}")
    if extra:
        parts.append(f"judged unknown condition(s) {', '.join(extra)}")
    raise MalformedVerdictError(
        "the verdict does not cover the thesis's break conditions exactly: " + "; ".join(parts)
    )


def _type_of(condition_id: str, thesis: Thesis) -> BreakConditionType:
    """The break-condition type for an id on the thesis, or a malformed-verdict refusal."""
    for bc in thesis.break_conditions:
        if bc.id == condition_id:
            return bc.type
    raise MalformedVerdictError(
        f"break condition {condition_id!r} is not on the thesis; its type cannot be resolved"
    )


def validate_action(verdict: T1Verdict, thesis: Thesis, exit_menu: ExitMenu) -> None:
    """Refuse a proposed action that falls outside the ratified policy (§5.5/§5.6).

    What it does: checks the model's proposed action against the ratified exit menu and the break
    it claims to act on, in deterministic code.
    What it assumes: `verdict` already covers the thesis's break conditions
    (`check_covers_conditions` ran) and `exit_menu` is the ratified one from the case's policy set.
    What it never does: trust the model's choice. The four rules it enforces are the whole of "a
    proposed action outside ratified policy is rejected by code before reaching rails":

    * a BROKEN verdict may not resolve to HOLD — core membership changes on a break (§5.5);
    * an EXIT needs a break behind it — the same rule, the other direction;
    * an EXIT's strategy must be on the ratified menu — the agent may pick, not invent (§5.6);
    * an IMMEDIATE exit is allowed only on a break of a type the menu unlocks it for (§5.6).

    Raises `PolicyViolationError` with a reason; returns None when the action is within policy.
    """
    action = verdict.proposed_action
    broken = verdict.broken

    if broken and action.kind is ProposedActionKind.HOLD:
        broken_ids = ", ".join(v.id for v in broken)
        raise PolicyViolationError(
            f"a BROKEN verdict ({broken_ids}) cannot resolve to HOLD: core membership changes on a "
            "break (§5.5). Propose an exit from the ratified menu, or escalate to the human"
        )

    if action.kind is not ProposedActionKind.EXIT:
        # HOLD with nothing broken, or ESCALATE — both always within policy.
        return

    if not broken:
        raise PolicyViolationError(
            "an EXIT was proposed with no BROKEN break condition: core membership changes on a "
            "break only (§5.5), so an exit with the thesis intact is outside policy"
        )

    strategy = action.exit_strategy
    if strategy not in exit_menu.allowed:
        allowed = ", ".join(s.value for s in exit_menu.allowed)
        raise PolicyViolationError(
            f"exit strategy {strategy.value if strategy else None} is not on the ratified menu "
            f"({allowed}); the agent may choose among ratified strategies, not invent one (§5.6)"
        )

    if strategy is ExitStrategy.IMMEDIATE:
        broken_types = {_type_of(v.id, thesis).value for v in broken}
        if not (broken_types & set(exit_menu.immediate_allowed_on)):
            unlocks = ", ".join(exit_menu.immediate_allowed_on) or "(none)"
            raise PolicyViolationError(
                "an IMMEDIATE exit is not unlocked: no broken condition is of a type the menu "
                f"allows it for (broke on {', '.join(sorted(broken_types))}; menu unlocks "
                f"{unlocks}). Use a staged exit, or escalate (§5.6)"
            )
