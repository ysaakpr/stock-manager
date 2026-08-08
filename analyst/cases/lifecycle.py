"""A1: the §5.1 case lifecycle — which states exist, which moves are legal, and what guards them.

```
DRAFT → INTERVIEW → PROPOSAL → RATIFIED → FUNDED(paper | real) → ACTIVE ⇄ SUSPENDED → CLOSED
```

Pure rules, no database and no clock: everything here is a function of a state and the facts about
a case, so an illegal transition raises whether or not Postgres is anywhere nearby. `service.py`
is the same rules against `case_`, plus journaling.

Two things this module does that a plain edge table would not:

* **It distinguishes "not an edge" from "an edge you are not entitled to today."** `RATIFIED →
  FUNDED` is a legal edge, but `FUNDED(real)` additionally requires a `HUMAN` ratification record;
  a B9 `FIXTURE` ratification is refused by `FundingRefusedError` naming the case and the kind.
  Both refusals raise, and the messages say which one happened, because "invalid transition" costs
  an operator the twenty minutes the message could have saved.
* **It states what CLOSED means.** `CLOSED` has no outgoing edge at all. A case that was closed and
  is wanted again is a new case with its own ratification — reopening one would let a policy set
  ratified for a set of market conditions come back years later without anyone re-reading it.

The edges are exactly the arrows §5.1 draws. Notably absent, and absent on purpose: no edge back
from `PROPOSAL` to `INTERVIEW` (a proposal that needs rework is revised and re-ratified — that is
what `PolicySet.revise` is for), and no edge from `DRAFT` to `CLOSED` (an unfunded case has no
money and no history to close over; `CaseService.delete` removes it instead).

Funding mode is a property of a `FUNDED` case, not a state of its own, matching
`case_.funding_mode`. It selects which `Broker` is injected and nothing else (invariant #5) —
there is no `if paper:` branch downstream, and this module is where the distinction is allowed to
be visible at all, because ratification is exactly where paper and real differ in *governance*.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Final

from analyst.cases.policies import RatificationKind

__all__ = [
    "CLOSED_STATES",
    "LEGAL_TRANSITIONS",
    "PRE_FUNDING_STATES",
    "TRADING_STATES",
    "CaseState",
    "FundingMode",
    "FundingRefusedError",
    "IllegalTransitionError",
    "LifecycleError",
    "check_funding",
    "check_transition",
]


class CaseState(StrEnum):
    """Where a case has got to in §5.1. Mirrors `case_.state`'s CHECK constraint."""

    DRAFT = "DRAFT"
    """Created, nothing elicited yet. The only state a case can be deleted from."""

    INTERVIEW = "INTERVIEW"
    """A2 is eliciting capital plan, horizon, theme, risk appetite and exclusions (§5.1)."""

    PROPOSAL = "PROPOSAL"
    """A complete §5.2 policy set plus theses is drafted and awaiting one ratification."""

    RATIFIED = "RATIFIED"
    """A human (or, for tests only, B9's fixture) approved the proposal. No money yet."""

    FUNDED = "FUNDED"
    """Capital is committed, in the mode `case_.funding_mode` records. Not yet trading."""

    ACTIVE = "ACTIVE"
    """Trading. The daily loop makes decisions for this case."""

    SUSPENDED = "SUSPENDED"
    """Paused — a drawdown review, a data problem, or the human's instruction. No new orders."""

    CLOSED = "CLOSED"
    """Terminal. Positions are out and the case is history; nothing leaves this state."""


class FundingMode(StrEnum):
    """Which broker a funded case runs against. Mirrors `case_.funding_mode`'s CHECK.

    The *only* difference between paper and real is which `Broker` implementation is injected
    (invariant #5). It is recorded on the case rather than inferred anywhere so that the evidence
    pack, the journal and the reconciliation all agree about which one a decision ran under.
    """

    PAPER = "PAPER"
    """`SimBroker`. The default, and where every case starts (decision #8)."""

    REAL = "REAL"
    """`KiteBroker`. Reachable only by graduation, and only on a HUMAN ratification."""


#: Every move §5.1 draws, and nothing else. Anything absent raises, naming both states.
#:
#: Two edges are worth their reason in writing:
#:
#: * `ACTIVE ⇄ SUSPENDED` — the only bidirectional pair in the lifecycle. A drawdown review or a
#:   data-integrity pause must be reversible without re-ratifying, or every incident would end a
#:   case.
#: * `SUSPENDED → CLOSED` and `ACTIVE → CLOSED` — closing is reachable from both trading states.
#:   §5.1 draws the arrow off the `ACTIVE ⇄ SUSPENDED` pair, and a case that must be wound down
#:   while suspended is the common case, not the exception.
LEGAL_TRANSITIONS: Final[Mapping[CaseState, frozenset[CaseState]]] = {
    CaseState.DRAFT: frozenset({CaseState.INTERVIEW}),
    CaseState.INTERVIEW: frozenset({CaseState.PROPOSAL}),
    CaseState.PROPOSAL: frozenset({CaseState.RATIFIED}),
    CaseState.RATIFIED: frozenset({CaseState.FUNDED}),
    CaseState.FUNDED: frozenset({CaseState.ACTIVE}),
    CaseState.ACTIVE: frozenset({CaseState.SUSPENDED, CaseState.CLOSED}),
    CaseState.SUSPENDED: frozenset({CaseState.ACTIVE, CaseState.CLOSED}),
    CaseState.CLOSED: frozenset(),
}

#: States with no outgoing edge. Derived from the table so the two cannot disagree.
CLOSED_STATES: Final[frozenset[CaseState]] = frozenset(
    state for state, allowed in LEGAL_TRANSITIONS.items() if not allowed
)

#: States before any capital is committed. A case here has nothing at risk, which is why the
#: policy set may still be replaced wholesale rather than revised into a new version.
PRE_FUNDING_STATES: Final[frozenset[CaseState]] = frozenset(
    {CaseState.DRAFT, CaseState.INTERVIEW, CaseState.PROPOSAL, CaseState.RATIFIED}
)

#: States in which the daily loop may act on a case. `SUSPENDED` is deliberately not one: a
#: suspended case is still monitored and still journaled, but it places no orders.
TRADING_STATES: Final[frozenset[CaseState]] = frozenset({CaseState.ACTIVE})


class LifecycleError(Exception):
    """Base for every lifecycle failure, so callers can catch the family."""


class IllegalTransitionError(LifecycleError):
    """A move the §5.1 state machine does not permit.

    Always names the case and both states: a state-machine error that says only "invalid
    transition" is unactionable in a log from an overnight wave.
    """

    def __init__(
        self, case_id: str, from_state: CaseState, to_state: CaseState, reason: str
    ) -> None:
        self.case_id = case_id
        self.from_state = from_state
        self.to_state = to_state
        super().__init__(
            f"case {case_id}: {from_state.value} -> {to_state.value} is not a legal §5.1 "
            f"transition ({reason})"
        )


class FundingRefusedError(LifecycleError):
    """`FUNDED` was requested without the ratification that mode of funding requires.

    Separate from `IllegalTransitionError` because the edge itself is legal and the *entitlement*
    is missing — the two need different fixes, and a caller that catches "illegal transition"
    broadly must not swallow "you tried to fund real money on a test fixture".
    """


def check_transition(case_id: str, from_state: CaseState, to_state: CaseState) -> None:
    """Raise `IllegalTransitionError` unless `to_state` is reachable from `from_state`.

    What it does: consults `LEGAL_TRANSITIONS` and nothing else.
    What it assumes: the caller has already loaded the case, so `case_id` is real — this is a rule
    check, not a lookup.
    What it never does: consider funding mode. `check_funding` owns that, because the two
    refusals are different and both messages matter.
    """
    if to_state in LEGAL_TRANSITIONS[from_state]:
        return
    allowed = sorted(state.value for state in LEGAL_TRANSITIONS[from_state])
    raise IllegalTransitionError(
        case_id,
        from_state,
        to_state,
        f"{from_state.value} is terminal and nothing leaves it"
        if from_state in CLOSED_STATES
        else f"legal from {from_state.value}: {', '.join(allowed)}",
    )


def check_funding(case_id: str, mode: FundingMode, ratification_kind: RatificationKind) -> None:
    """Raise `FundingRefusedError` unless `ratification_kind` may back funding in `mode`.

    What it does: enforces B9's rule in the one place funding happens — real money runs only on a
    `HUMAN` ratification, and a `FIXTURE` ratification is refused however the case reached
    `RATIFIED`.
    What it assumes: `ratification_kind` came from the case's *current* ratified policy set, read
    from `policy_set`, not from whatever the caller believes was approved.
    What it never does: offer a way through. There is no force parameter, no environment variable
    and no test hook: graduating a case to real money is reserved to the human (AGENTIC_CONTEXT
    §3.6), and a fixture ratification is paper-only forever (B9).
    """
    if mode is FundingMode.PAPER or ratification_kind.funds_real_money:
        return
    raise FundingRefusedError(
        f"case {case_id}: FUNDED(real) requires a HUMAN ratification record, but the current "
        f"policy set was ratified as {ratification_kind.value} — B9's reference ratification is "
        "valid for paper and tests only. A person must ratify this policy set before real money "
        "funds it (AGENTIC_CONTEXT §3.6)."
    )
