"""A5 · T1 — the triggered LLM review that reads a flag's evidence and returns a verdict.

§5.4's second tier: on a T0 flag or a filing event, a strong model reads the evidence bundle
(`bundle.py`, M6.3) — the flag, the thesis and its break conditions, recent filings/news, the
price/flow context — and returns a verdict per break condition (`INTACT / WEAKENED / BROKEN`) plus
a proposed action within the ratified exit menu. This module is that tier, and it holds four
properties the plan is explicit about:

* **Structured output, schema-validated.** The model answers in the `T1Verdict` schema
  (`verdicts.py`), which is parsed and validated per break condition. A malformed answer — invalid
  JSON, the wrong shape, a verdict that skips or invents a break condition — is *retried*, and if
  it still will not parse, *escalated to the human*. It is never read loosely: a verdict that would
  end a position cannot rest on the model having probably meant BROKEN.

* **The proposed action is validated in code, not trusted.** Before any action could reach A7/A8 it
  passes `validate_action` — a deterministic gate against the ratified exit menu (§5.6) and §5.5's
  rule that core membership changes on a break only. An out-of-policy action is rejected here and
  escalated to the human; it never becomes an exit directive. That is invariant #6 at this tier: the
  model proposes, the code disposes, the rails are never an LLM's say-so.

* **A BROKEN verdict on a core holding triggers the A7 exit path.** T1 does not place the order —
  it has no broker and touches no rail — it journals an `ESCALATE` carrying the validated exit
  directive for A7 to carry out. `T1Result.exit_triggered` is the signal the exit path reads.

* **Every verdict is journaled with tokens.** The review is a `MeteredLLM` call, so its model and
  token/rupee cost land on the journal line for the decision it informed (§5.7, decision #12) —
  including the cost of any malformed attempts, which is spend that happened and must not vanish
  from the burn report.

Money is `Decimal` (the token cost), time is injected (B10: the journal `ts` comes from a `Clock`),
identity is ISIN (#2), and nothing here reads the network — under `StubLLM` the whole tier runs
deterministically, which is what M6.7's fire drill and this module's tests depend on.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Final

from accounting.tokens import MeteredCompletion, MeteredLLM
from analyst.cases.policies import ExitMenu, ExitStrategy
from analyst.journal import (
    Actor,
    BreakConditionEvaluation,
    Decision,
    Journal,
    JournalEntry,
    Sleeve,
    TokenSpend,
    Verdict,
)
from analyst.llm import DEFAULT_MODEL, Message, Role
from analyst.monitor.bundle import BuiltBundle
from analyst.monitor.verdicts import (
    MalformedVerdictError,
    PolicyViolationError,
    ProposedAction,
    ProposedActionKind,
    T1Verdict,
    check_covers_conditions,
    parse_verdict,
    validate_action,
)
from analyst.thesis import Thesis
from dataplatform.clock import Clock, SystemClock
from dataplatform.logging import get_logger

__all__ = [
    "SYSTEM_PROMPT",
    "T1_MODEL",
    "T1_PURPOSE",
    "T1Outcome",
    "T1Request",
    "T1Result",
    "T1Reviewer",
    "build_messages",
]

_LOG = get_logger(__name__)

#: The strong model §5.4 calls for at T1. The default of the analyst's client (opus-tier), priced
#: on the dated card like any other call. Named here so a test can compute the prompt digest the
#: reviewer will send and register a deterministic reply under `StubLLM`.
T1_MODEL: Final[str] = DEFAULT_MODEL

#: What the burn report groups this spend under (§5.7). One label per tier, so "what did T1 cost
#: this quarter" is a single sum.
T1_PURPOSE: Final[str] = "t1_review"

#: The system prompt that fixes the model's role and its answer schema. Part of the prompt digest,
#: so it is a module constant a test can reference verbatim.
SYSTEM_PROMPT: Final[str] = (
    "You are the T1 reviewer of an equity monitoring system. You are shown an evidence bundle for "
    "one holding on one trading session: a triggering flag, the ratified thesis and its break "
    "conditions, and the recent filings, news and price/flow context. Judge each break condition "
    "on the evidence as INTACT (not met), WEAKENED (evidence moving against it, not met) or BROKEN "
    "(met), and propose one action.\n\n"
    "Answer with a single JSON object and nothing else, in this shape:\n"
    "{\n"
    '  "isin": "<the holding\'s ISIN>",\n'
    '  "verdicts": [ {"id": "<break-condition id>", "verdict": "INTACT|WEAKENED|BROKEN", '
    '"observed": "<one line: what in the evidence supports this>"} ],\n'
    '  "proposed_action": {"kind": "HOLD|EXIT|ESCALATE", '
    '"exit_strategy": "STAGED|IMMEDIATE|EXIT_AND_REDEPLOY|null", '
    '"rationale": "<one line>"},\n'
    '  "summary": "<one line>"\n'
    "}\n\n"
    "Judge exactly the break conditions in the bundle — one verdict per condition, no more, no "
    "fewer. Only an EXIT action names an exit_strategy; HOLD and ESCALATE set it to null. Propose "
    "EXIT only when a break condition is BROKEN. When unsure, ESCALATE rather than guess."
)

#: The one-line instruction appended to the bundle in the user turn, restating the output contract
#: at the point the model answers.
_USER_INSTRUCTION: Final[str] = (
    "\n\nReturn only the JSON verdict object described in the system prompt."
)


def build_messages(rendered_prompt: str) -> tuple[Message, ...]:
    """The conversation a T1 review sends — the rendered bundle as the single user turn.

    Exposed (not private) so a test can reconstruct the exact request and register a deterministic
    `StubLLM` reply against its digest — the prompt is `build_messages(...)` + `T1_MODEL` +
    `SYSTEM_PROMPT`, which is what `prompt_digest` hashes.
    """
    return (Message(role=Role.USER, content=rendered_prompt + _USER_INSTRUCTION),)


class T1Outcome(StrEnum):
    """What one T1 review concluded — the state of the thesis, or an escalation.

    `INTACT`/`WEAKENED` resolve to a journaled `HOLD`; `BROKEN` to an `ESCALATE` that carries the
    exit directive to A7; `ESCALATED` is the malformed-or-out-of-policy path that hands the decision
    to the human.
    """

    INTACT = "INTACT"
    """No break condition met; the thesis stands. Journaled HOLD."""

    WEAKENED = "WEAKENED"
    """Evidence moving against the thesis, no break met. Journaled HOLD."""

    BROKEN = "BROKEN"
    """A break condition met on a core holding. Journaled ESCALATE; A7's exit path is triggered."""

    ESCALATED = "ESCALATED"
    """The model's answer was malformed after retries, or its action was out of policy — handed to
    the human. Journaled ESCALATE."""


@dataclass(frozen=True, slots=True)
class T1Request:
    """Everything one T1 review reads: the built bundle, its thesis, and the ratified exit menu.

    What it does: pairs the M6.3 bundle (which carries the exact rendered prompt the model sees)
    with the two things the policy gate needs — the thesis (for break-condition types) and the
    ratified exit menu.
    What it assumes: `built` was assembled for this holding on this session, `thesis` is the version
    in force then (the one shown in the bundle), and `exit_menu` comes from the case's *ratified*
    policy set — validating an action against a proposal would gate it on rules nobody approved.
    What it never does: hold a broker or a rail. T1 proposes; A7/A8 dispose.
    """

    built: BuiltBundle
    thesis: Thesis
    exit_menu: ExitMenu


@dataclass(frozen=True, slots=True)
class T1Result:
    """The outcome of one T1 review — what it concluded, what it proposed, and what it recorded.

    `verdict` is None only on the malformed-escalate path (nothing parsed). `proposed_action_kind`
    and `exit_strategy` carry the *validated* action, or None when it was rejected. `exit_triggered`
    is True exactly when a BROKEN verdict produced an in-policy exit for A7 to carry out.
    `token_spend` sums every attempt, so the burn report never loses the cost of a retried call.
    """

    trading_date: date
    case_id: str | None
    isin: str
    outcome: T1Outcome
    verdict: T1Verdict | None
    proposed_action_kind: ProposedActionKind | None
    exit_strategy: str | None
    exit_triggered: bool
    journal_entry_id: int
    token_spend: TokenSpend
    model: str
    attempts: int
    rejection: str | None = None

    @property
    def escalated(self) -> bool:
        """Whether the decision was handed up — a break to A7, or a rejection to the human."""
        return self.outcome in (T1Outcome.BROKEN, T1Outcome.ESCALATED)


class T1Reviewer:
    """§5.4's T1 tier: a strong-model review over an evidence bundle, verdict schema-validated.

    What it does: on `review()`, snapshots the bundle, asks the metered model for a `T1Verdict`
    (retrying a malformed answer up to `max_attempts`), validates the proposed action against the
    ratified exit menu in code, and journals the decision with its model and token/rupee cost.
    What it assumes: the caller owns the transaction (the `Journal` never commits), the `MeteredLLM`
    is built on the same dated price card the burn report reads, and `clock` is injected (B10).
    What it never does: place an order, touch a rail, or read a malformed answer as a verdict — a
    break that cannot be parsed is escalated to the human, never guessed.
    """

    __slots__ = ("_clock", "_journal", "_llm", "_max_attempts", "_model")

    def __init__(
        self,
        llm: MeteredLLM,
        journal: Journal,
        *,
        clock: Clock | None = None,
        model: str = T1_MODEL,
        max_attempts: int = 2,
    ) -> None:
        if max_attempts < 1:
            raise ValueError(f"max_attempts must be at least 1, got {max_attempts}")
        self._llm = llm
        self._journal = journal
        self._clock = SystemClock() if clock is None else clock
        self._model = model
        self._max_attempts = max_attempts

    def __repr__(self) -> str:
        return f"{type(self).__name__}(model={self._model!r}, max_attempts={self._max_attempts})"

    def review(self, request: T1Request) -> T1Result:
        """Review one flagged holding and record the verdict (§5.4).

        Retries a malformed model answer up to `max_attempts`, then escalates it; validates the
        proposed action against the ratified menu before it could reach A7; and journals the
        decision — HOLD for an intact/weakened thesis, ESCALATE carrying the exit directive for a
        break, ESCALATE to the human for a malformed answer or an out-of-policy action.
        """
        bundle = request.built.bundle
        case_id = bundle.case_id
        trading_date = bundle.trading_date
        isin = request.thesis.isin

        # Store the evidence first, so the journal entry's ref reconstructs exactly what was sent.
        ref = self._journal.snapshot(bundle).ref
        messages = build_messages(request.built.rendered_prompt)

        verdict: T1Verdict | None = None
        malformed_reason: str | None = None
        total_in = 0
        total_out = 0
        total_cost = Decimal(0)
        model_used = self._model
        attempts = 0

        for attempt in range(1, self._max_attempts + 1):
            attempts = attempt
            completion = self._llm.complete(
                messages,
                model=self._model,
                purpose=T1_PURPOSE,
                system=SYSTEM_PROMPT,
                case_id=case_id,
                on=trading_date,
            )
            spend = _spend_of(completion)
            total_in += spend.tokens_in
            total_out += spend.tokens_out
            total_cost += spend.cost_inr
            model_used = completion.priced.model
            try:
                candidate = parse_verdict(completion.text)
                check_covers_conditions(candidate, request.thesis)
                if candidate.isin != isin:
                    raise MalformedVerdictError(
                        f"the verdict names isin {candidate.isin} but the holding is {isin}"
                    )
            except MalformedVerdictError as error:
                malformed_reason = str(error)
                _LOG.warning(
                    "t1.malformed",
                    case_id=case_id,
                    isin=isin,
                    trading_date=trading_date.isoformat(),
                    attempt=attempt,
                    reason=malformed_reason,
                )
                continue
            verdict = candidate
            break

        token_spend = TokenSpend(tokens_in=total_in, tokens_out=total_out, cost_inr=total_cost)

        if verdict is None:
            return self._escalate_malformed(
                request,
                ref=ref,
                reason=malformed_reason or "no answer",
                token_spend=token_spend,
                model=model_used,
                attempts=attempts,
            )

        try:
            validate_action(verdict, request.thesis, request.exit_menu)
        except PolicyViolationError as error:
            return self._escalate_policy(
                request,
                verdict=verdict,
                ref=ref,
                reason=str(error),
                token_spend=token_spend,
                model=model_used,
                attempts=attempts,
            )

        return self._record_verdict(
            request,
            verdict=verdict,
            ref=ref,
            token_spend=token_spend,
            model=model_used,
            attempts=attempts,
        )

    # ── the endings ──────────────────────────────────────────────────────────────────────────────

    def _record_verdict(
        self,
        request: T1Request,
        *,
        verdict: T1Verdict,
        ref: str,
        token_spend: TokenSpend,
        model: str,
        attempts: int,
    ) -> T1Result:
        """Journal an in-policy verdict: HOLD if nothing broke, else ESCALATE with the exit."""
        bundle = request.built.bundle
        action = verdict.proposed_action
        evaluations = _evaluations(verdict)
        worst = verdict.worst

        if worst is Verdict.BROKEN:
            outcome = T1Outcome.BROKEN
            decision = Decision.ESCALATE
            exit_triggered = action.kind is ProposedActionKind.EXIT
            broken_ids = ", ".join(v.id for v in verdict.broken)
            rationale = (
                f"T1 BROKEN on {broken_ids}: {verdict.summary} — proposed "
                f"{_action_phrase(action.kind, action.exit_strategy)}: {action.rationale}"
            )
        else:
            outcome = T1Outcome.WEAKENED if worst is Verdict.WEAKENED else T1Outcome.INTACT
            decision = Decision.HOLD
            exit_triggered = False
            rationale = f"T1 {worst.value}: {verdict.summary} — {action.rationale}"

        payload = _action_payload(action)
        entry = self._journal.append(
            JournalEntry(
                ts=self._clock.now(),
                trading_date=bundle.trading_date,
                case_id=bundle.case_id,
                actor=Actor.T1,
                decision=decision,
                isin=request.thesis.isin,
                sleeve=Sleeve.CORE,
                evidence_snapshot_ref=ref,
                break_conditions_evaluated=evaluations,
                rationale=rationale,
                model=model,
                tokens=token_spend,
                payload=payload,
            )
        )
        _LOG.info(
            "t1.verdict",
            case_id=bundle.case_id,
            isin=request.thesis.isin,
            trading_date=bundle.trading_date.isoformat(),
            outcome=outcome.value,
            action=action.kind.value,
            exit_triggered=exit_triggered,
            entry_id=entry.id,
            cost_inr=str(token_spend.cost_inr),
        )
        return T1Result(
            trading_date=bundle.trading_date,
            case_id=bundle.case_id,
            isin=request.thesis.isin,
            outcome=outcome,
            verdict=verdict,
            proposed_action_kind=action.kind,
            exit_strategy=None if action.exit_strategy is None else action.exit_strategy.value,
            exit_triggered=exit_triggered,
            journal_entry_id=entry.id,
            token_spend=token_spend,
            model=model,
            attempts=attempts,
        )

    def _escalate_policy(
        self,
        request: T1Request,
        *,
        verdict: T1Verdict,
        ref: str,
        reason: str,
        token_spend: TokenSpend,
        model: str,
        attempts: int,
    ) -> T1Result:
        """Journal an ESCALATE for a valid verdict whose action the policy gate rejected.

        The verdict is recorded (it was valid); the action is not, so it never reaches A7. The human
        decides — which is why the reason names the exact rule the proposal breached.
        """
        bundle = request.built.bundle
        rationale = (
            f"T1 verdict accepted but proposed action rejected before rails: {reason} "
            f"(verdict: {verdict.summary})"
        )
        payload = _action_payload(verdict.proposed_action)
        payload["rejection"] = reason
        entry = self._journal.append(
            JournalEntry(
                ts=self._clock.now(),
                trading_date=bundle.trading_date,
                case_id=bundle.case_id,
                actor=Actor.T1,
                decision=Decision.ESCALATE,
                isin=request.thesis.isin,
                sleeve=Sleeve.CORE,
                evidence_snapshot_ref=ref,
                break_conditions_evaluated=_evaluations(verdict),
                rationale=rationale,
                model=model,
                tokens=token_spend,
                payload=payload,
            )
        )
        _LOG.warning(
            "t1.action_rejected",
            case_id=bundle.case_id,
            isin=request.thesis.isin,
            trading_date=bundle.trading_date.isoformat(),
            reason=reason,
            entry_id=entry.id,
        )
        return T1Result(
            trading_date=bundle.trading_date,
            case_id=bundle.case_id,
            isin=request.thesis.isin,
            outcome=T1Outcome.ESCALATED,
            verdict=verdict,
            proposed_action_kind=None,
            exit_strategy=None,
            exit_triggered=False,
            journal_entry_id=entry.id,
            token_spend=token_spend,
            model=model,
            attempts=attempts,
            rejection=reason,
        )

    def _escalate_malformed(
        self,
        request: T1Request,
        *,
        ref: str,
        reason: str,
        token_spend: TokenSpend,
        model: str,
        attempts: int,
    ) -> T1Result:
        """Journal an ESCALATE when the model would not return a parseable verdict after retries.

        No break-condition evaluations are recorded — none parsed — but the token cost of the failed
        attempts is, because it is spend that happened. The human is handed the raw failure.
        """
        bundle = request.built.bundle
        rationale = (
            f"T1 model returned a malformed verdict after {attempts} attempt(s); escalated to the "
            f"human. Last reason: {reason}"
        )
        entry = self._journal.append(
            JournalEntry(
                ts=self._clock.now(),
                trading_date=bundle.trading_date,
                case_id=bundle.case_id,
                actor=Actor.T1,
                decision=Decision.ESCALATE,
                isin=request.thesis.isin,
                sleeve=Sleeve.CORE,
                evidence_snapshot_ref=ref,
                rationale=rationale,
                model=model,
                tokens=token_spend,
                payload={"malformed": reason, "attempts": str(attempts)},
            )
        )
        _LOG.error(
            "t1.escalated_malformed",
            case_id=bundle.case_id,
            isin=request.thesis.isin,
            trading_date=bundle.trading_date.isoformat(),
            attempts=attempts,
            reason=reason,
            entry_id=entry.id,
        )
        return T1Result(
            trading_date=bundle.trading_date,
            case_id=bundle.case_id,
            isin=request.thesis.isin,
            outcome=T1Outcome.ESCALATED,
            verdict=None,
            proposed_action_kind=None,
            exit_strategy=None,
            exit_triggered=False,
            journal_entry_id=entry.id,
            token_spend=token_spend,
            model=model,
            attempts=attempts,
            rejection=reason,
        )


def _spend_of(completion: MeteredCompletion) -> TokenSpend:
    """The token/rupee cost of one metered call, as the journal records it."""
    return completion.priced.token_spend


def _evaluations(verdict: T1Verdict) -> tuple[BreakConditionEvaluation, ...]:
    """The per-condition evaluations for the journal line, one per verdict."""
    return tuple(
        BreakConditionEvaluation(id=v.id, verdict=v.verdict, observed=v.observed)
        for v in verdict.verdicts
    )


def _action_phrase(kind: ProposedActionKind, exit_strategy: ExitStrategy | None) -> str:
    """A one-line description of a proposed action for a journal rationale."""
    if kind is ProposedActionKind.EXIT and exit_strategy is not None:
        return f"EXIT ({exit_strategy.value})"
    return kind.value


def _action_payload(action: ProposedAction) -> dict[str, str]:
    """The strings-only journal payload for a proposed action (no float, no enum object)."""
    payload: dict[str, str] = {
        "proposed_action": action.kind.value,
        "action_rationale": action.rationale,
    }
    if action.exit_strategy is not None:
        payload["exit_strategy"] = action.exit_strategy.value
    return payload
