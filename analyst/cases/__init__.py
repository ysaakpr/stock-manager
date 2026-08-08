"""A1: case service — case CRUD, the §5.1 lifecycle, versioned §5.2 policy sets, SIP, case views.

A case is the unit everything else in `analyst/` hangs off: a thesis belongs to one, an order
carries one, a journal entry names one. This package owns what a case *is* and what may happen to
it, in three layers:

* `lifecycle.py` — the §5.1 state machine as pure rules. `DRAFT → INTERVIEW → PROPOSAL → RATIFIED
  → FUNDED(paper|real) → ACTIVE ⇄ SUSPENDED → CLOSED`, and the funding guard that refuses real
  money on a B9 fixture ratification.
* `policies.py` — the §5.2 ratified policy set: seven policies, one document, one ratification,
  frozen once approved. A change is `revise()`, which produces the next version in `PROPOSAL`.
* `service.py` — those rules against `case_` and `policy_set`, with every transition journaled
  (invariant #9) in the same transaction that makes it.

Two properties this package exists to guarantee, both proved in `tests/unit/test_cases.py`:

* **No state changes silently.** `CaseService` is the only writer of `case_.state`, and it
  journals every move it makes.
* **No policy changes at all.** A ratified `PolicySet` is immutable in the model and append-only
  in the database; an edit is a new version awaiting its own ratification, and real money runs
  only on a `HUMAN` one.
"""

from analyst.cases.lifecycle import (
    CLOSED_STATES,
    LEGAL_TRANSITIONS,
    PRE_FUNDING_STATES,
    TRADING_STATES,
    CaseState,
    FundingMode,
    FundingRefusedError,
    IllegalTransitionError,
    LifecycleError,
    check_funding,
    check_transition,
)
from analyst.cases.policies import (
    POLICY_FIELDS,
    CapitalPlan,
    CashPolicy,
    ExitMenu,
    ExitStrategy,
    HorizonAndBenchmarks,
    MonitoringCadence,
    PolicyError,
    PolicySet,
    PolicyStatus,
    PolicyVersionError,
    Ratification,
    RatificationKind,
    RatificationMismatchError,
    RiskRails,
    RotationDial,
    T2Cadence,
    TriggerSensitivity,
    policy_digest,
)
from analyst.cases.service import (
    PENDING_POLICY_KEY,
    CaseError,
    CaseNotFoundError,
    CaseRecord,
    CaseService,
    CaseSummary,
    CrossCaseExposure,
    DuplicateCaseError,
    NoPolicyError,
    SipInstalment,
    UnknownPolicyVersionError,
)

__all__ = [
    "CLOSED_STATES",
    "LEGAL_TRANSITIONS",
    "PENDING_POLICY_KEY",
    "POLICY_FIELDS",
    "PRE_FUNDING_STATES",
    "TRADING_STATES",
    "CapitalPlan",
    "CaseError",
    "CaseNotFoundError",
    "CaseRecord",
    "CaseService",
    "CaseState",
    "CaseSummary",
    "CashPolicy",
    "CrossCaseExposure",
    "DuplicateCaseError",
    "ExitMenu",
    "ExitStrategy",
    "FundingMode",
    "FundingRefusedError",
    "HorizonAndBenchmarks",
    "IllegalTransitionError",
    "LifecycleError",
    "MonitoringCadence",
    "NoPolicyError",
    "PolicyError",
    "PolicySet",
    "PolicyStatus",
    "PolicyVersionError",
    "Ratification",
    "RatificationKind",
    "RatificationMismatchError",
    "RiskRails",
    "RotationDial",
    "SipInstalment",
    "T2Cadence",
    "TriggerSensitivity",
    "UnknownPolicyVersionError",
    "check_funding",
    "check_transition",
    "policy_digest",
]
